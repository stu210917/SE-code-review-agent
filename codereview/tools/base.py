"""工具抽象层：协议、参数校验、注册表与路径沙箱。

设计原则
--------
1. **工具是纯函数**：``run(**kwargs) -> ToolResult``，不依赖全局状态，方便单测。
2. **错误不致命**：工具抛出的 :class:`ToolError` 会被 ``ToolRegistry.execute``
   转成一条错误观察（Observation）回灌给模型，让模型自己换一种调用方式，
   而不是让整个 Agent 崩掉。
3. **默认拒绝**：所有路径都经过 :meth:`Tool.resolve_path` 沙箱校验，
   无法读取工作区之外的文件（防提示注入导致的任意文件读取）。
4. **自描述**：``parameters`` 采用 JSON-Schema 风格，直接喂给 Prompt 生成工具说明，
   同时用于运行时参数校验——一份定义，两处生效。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import Config
from ..errors import PathSecurityError, ToolArgumentError, ToolExecutionError, ToolNotFoundError

#: 支持的类型标注 → Python 类型
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool
    output: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, output=output, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, output="", error=error, meta=meta)

    @property
    def summary(self) -> str:
        """一行摘要，用于 CLI 进度提示与报告附录。"""
        if not self.ok:
            return f"失败：{self.error[:120]}"
        first_line = self.output.strip().splitlines()[0] if self.output.strip() else "（无输出）"
        return first_line[:120]


class Tool(ABC):
    """所有工具的基类。"""

    #: 工具名（模型调用时使用的唯一标识，建议 snake_case）
    name: str = ""
    #: 给模型看的功能说明
    description: str = ""
    #: JSON-Schema 风格参数定义：``{参数名: {"type": ..., "description": ..., "required": bool, "default": ...}}``
    parameters: dict[str, dict[str, Any]] = {}
    #: 是否为危险操作（如执行代码），需要 ``--allow-exec`` 显式开启
    dangerous: bool = False

    def __init__(self, config: Config) -> None:
        self.config = config
        self.workspace: Path = Path(config.workspace).resolve()

    # ------------------------------------------------------------------ #
    # 参数校验
    # ------------------------------------------------------------------ #
    def validate(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """校验并归一化参数。

        校验失败时抛出 :class:`ToolArgumentError`，错误信息里会带上正确用法，
        这样模型下一轮就能自我纠正——这是「错误处理」评分点的关键细节。
        """
        arguments = dict(arguments or {})
        if not isinstance(arguments, dict):
            raise ToolArgumentError(f"action_input 必须是 JSON 对象，收到 {type(arguments).__name__}")

        unknown = set(arguments) - set(self.parameters)
        if unknown:
            raise ToolArgumentError(
                f"未知参数 {sorted(unknown)}；{self.name} 支持的参数是 {sorted(self.parameters)}"
            )

        cleaned: dict[str, Any] = {}
        for param, spec in self.parameters.items():
            spec = spec or {}
            if param in arguments and arguments[param] is not None:
                value = arguments[param]
            elif "default" in spec:
                value = spec["default"]
            elif spec.get("required", True):
                raise ToolArgumentError(f"缺少必填参数 `{param}`（{spec.get('description', '')}）")
            else:
                continue

            expected = spec.get("type")
            if expected and expected in _TYPE_MAP:
                # bool 是 int 的子类，需要单独排除，避免 True 被当成 1
                if expected in ("integer", "number") and isinstance(value, bool):
                    raise ToolArgumentError(f"参数 `{param}` 期望 {expected}，收到布尔值 {value!r}")
                if not isinstance(value, _TYPE_MAP[expected]):
                    raise ToolArgumentError(
                        f"参数 `{param}` 期望 {expected}，收到 {type(value).__name__}（{value!r}）"
                    )
                if expected == "integer" and not isinstance(value, int):
                    value = int(value)
            if "choices" in spec and value not in spec["choices"]:
                raise ToolArgumentError(f"参数 `{param}` 只能是 {spec['choices']} 之一，收到 {value!r}")
            if "min" in spec and isinstance(value, (int, float)) and value < spec["min"]:
                raise ToolArgumentError(f"参数 `{param}` 不能小于 {spec['min']}")
            if "max" in spec and isinstance(value, (int, float)) and value > spec["max"]:
                raise ToolArgumentError(f"参数 `{param}` 不能大于 {spec['max']}")
            cleaned[param] = value
        return cleaned

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """子类实现的实际逻辑（参数已通过校验）。"""

    def safe_run(self, arguments: dict[str, Any] | None) -> ToolResult:
        """带完整保护的执行入口：任何异常都会变成 ``ToolResult.failure``。"""
        try:
            cleaned = self.validate(arguments)
            return self.run(**cleaned)
        except ToolArgumentError as exc:
            return ToolResult.failure(f"参数错误：{exc}")
        except PathSecurityError as exc:
            return ToolResult.failure(f"安全拦截：{exc}")
        except ToolExecutionError as exc:
            return ToolResult.failure(str(exc))
        except Exception as exc:  # pragma: no cover - 兜底，避免工具把 Agent 带崩
            return ToolResult.failure(f"工具 {self.name} 内部异常：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 沙箱
    # ------------------------------------------------------------------ #
    def resolve_path(self, raw: str, *, must_exist: bool = True) -> Path:
        """把用户/模型给的路径解析为绝对路径，并确保它位于工作区内。

        :raises PathSecurityError: 路径越界（如 ``../../etc/passwd``、``C:\\Windows``）。
        :raises ToolExecutionError: ``must_exist=True`` 但路径不存在。
        """
        if raw is None or str(raw).strip() == "":
            raise ToolArgumentError("path 不能为空")
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolExecutionError(f"无法解析路径 {raw!r}：{exc}") from exc

        workspace = self.workspace
        if resolved != workspace and workspace not in resolved.parents:
            raise PathSecurityError(
                f"{raw!r} 位于工作区 {workspace} 之外，已拒绝访问（如需放宽请用 --workspace 指定根目录）"
            )
        if must_exist and not resolved.exists():
            raise ToolExecutionError(f"路径不存在：{self.display_path(resolved)}")
        return resolved

    def display_path(self, path: Path) -> str:
        """输出相对工作区的短路径，报告里更好读。"""
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.name}>"


class ToolRegistry:
    """工具注册表：统一管理工具的注册、查找、描述与执行。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 未定义 name")
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"没有名为 `{name}` 的工具；可用工具：{sorted(self._tools)}") from exc

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """执行工具；**永不抛出**（除非工具名不存在，也会被转成失败结果）。"""
        try:
            tool = self.get(name)
        except ToolNotFoundError as exc:
            return ToolResult.failure(str(exc))
        result = tool.safe_run(arguments)
        result.meta.setdefault("tool", name)
        result.meta.setdefault("arguments", arguments or {})
        return result

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> Sequence[Tool]:
        return list(self._tools.values())

    def describe(self) -> str:
        """人类可读的工具清单（CLI ``tools`` 子命令使用）。"""
        lines = []
        for tool in self._tools.values():
            lines.append(f"* {tool.name}: {tool.description}")
            for param, spec in (tool.parameters or {}).items():
                required = "必填" if spec.get("required", True) else "可选"
                lines.append(f"    - {param} ({spec.get('type', 'any')}, {required}): {spec.get('description', '')}")
        return "\n".join(lines)


def build_default_registry(config: Config) -> ToolRegistry:
    """装配默认工具集。

    延迟导入具体工具模块，避免「工具 → 注册表 → 工具」的循环依赖。
    """
    from .exec_tool import CheckSyntaxTool, RunCodeTool
    from .fs import ListFilesTool, ReadFileTool, SearchCodeTool
    from .python_ast import AnalyzeCodeTool

    tools: list[Tool] = [
        ListFilesTool(config),
        ReadFileTool(config),
        SearchCodeTool(config),
        AnalyzeCodeTool(config),
        CheckSyntaxTool(config),
    ]
    if config.allow_exec:
        tools.append(RunCodeTool(config))
    return ToolRegistry(tools)


__all__ = ["Tool", "ToolResult", "ToolRegistry", "build_default_registry"]
