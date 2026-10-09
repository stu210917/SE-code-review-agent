"""代码执行类工具：语法检查（始终可用）与受限运行（默认关闭）。

安全立场
--------
* :class:`CheckSyntaxTool` 只做**编译**，不执行任何用户代码，因此默认开启；
* :class:`RunCodeTool` 会真正执行目标文件，属于危险操作：
  默认不注册，必须显式 ``--allow-exec`` 才会出现在工具列表里，
  且执行时施加超时、工作目录限定与最小化环境变量三重约束。
  这在真实 CI 里应当进一步放进容器或沙箱中运行。
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - 受限使用，见模块文档
import sys

from ..errors import ToolExecutionError
from ..utils import read_text, truncate
from .base import Tool, ToolResult
from .fs import iter_files

#: 传给被测程序的精简环境变量（避免泄漏当前进程的密钥等敏感信息）
_SAFE_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "PYTHONIOENCODING", "HOME", "USERPROFILE")


class CheckSyntaxTool(Tool):
    """批量语法检查（等价于 ``python -m compileall`` 的核心部分）。"""

    name = "check_syntax"
    description = (
        "对工作区内的 Python 文件做语法编译检查（Compile，不执行代码），返回存在语法错误的文件与原因。"
        "适合在大范围扫描后快速确认代码能否被导入。"
    )
    parameters = {
        "path": {"type": "string", "description": "文件或目录（相对工作区）", "required": False, "default": "."},
        "max_files": {"type": "integer", "description": "最多检查多少个文件", "required": False, "default": 100, "min": 1, "max": 2000},
    }

    def run(self, path: str = ".", max_files: int = 100) -> ToolResult:
        target = self.resolve_path(path)
        if target.is_dir():
            files = list(iter_files(target, "*.py", limit=max_files))
        else:
            files = [target]

        if not files:
            return ToolResult.success(f"{self.display_path(target)} 下没有需要检查的 .py 文件。", checked=0)

        errors: list[str] = []
        checked = 0
        for file in files:
            try:
                source = read_text(file)
            except OSError as exc:
                errors.append(f"{self.display_path(file)}: 读取失败（{exc}）")
                continue
            checked += 1
            try:
                compile(source, str(file), "exec", dont_inherit=True)
            except SyntaxError as exc:
                errors.append(
                    f"{self.display_path(file)}:{exc.lineno}: {exc.msg}（第 {exc.offset} 列）"
                )
            except ValueError as exc:  # 例如源码里出现 NUL 字节
                errors.append(f"{self.display_path(file)}: 无法编译（{exc}）")

        if not errors:
            return ToolResult.success(
                f"语法检查通过：{checked} 个文件全部可以编译，未发现语法错误。", checked=checked, errors=0
            )
        head = f"语法检查发现 {len(errors)} 处问题（共检查 {checked} 个文件）："
        return ToolResult.success(
            truncate("\n".join([head] + errors), self.config.max_observation_chars),
            checked=checked, errors=len(errors),
        )


class RunCodeTool(Tool):
    """在受限条件下运行单个 Python 文件（危险操作，默认不启用）。"""

    name = "run_code"
    description = (
        "在限定的工作目录、超时（默认 10 秒）与最小化环境变量下运行一个 Python 文件，"
        "返回退出码与标准输出/错误。仅在启动时显式开启 --allow-exec 后可用。"
    )
    dangerous = True
    parameters = {
        "path": {"type": "string", "description": "要运行的 .py 文件（相对工作区）"},
        "args": {"type": "array", "description": "命令行参数列表", "required": False, "default": []},
        "timeout": {"type": "number", "description": "超时秒数", "required": False, "default": 10.0, "min": 0.5, "max": 120},
    }

    def run(self, path: str, args: list[str] | None = None, timeout: float = 10.0) -> ToolResult:
        if not self.config.allow_exec:
            raise ToolExecutionError("run_code 已被禁用（安全默认）。如需启用请加 --allow-exec 参数。")
        target = self.resolve_path(path)
        if target.is_dir() or target.suffix.lower() != ".py":
            raise ToolExecutionError(f"run_code 只能运行单个 .py 文件，收到：{self.display_path(target)}")

        effective_timeout = min(float(timeout), float(self.config.exec_timeout))
        argv = [sys.executable, str(target), *[str(a) for a in (args or [])]]
        # 最小化环境变量：避免把当前进程的密钥等信息泄漏给被测代码
        environment = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        # 只把被测文件所在目录加入 sys.path，避免它 import 到工作区外的模块
        environment["PYTHONPATH"] = str(target.parent)

        try:
            completed = subprocess.run(  # noqa: S603 - 参数以列表传递，shell=False
                argv,
                cwd=str(target.parent),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolExecutionError(f"执行超时（>{effective_timeout}s），已强制终止；可能存在死循环。") from None
        except OSError as exc:
            raise ToolExecutionError(f"无法启动解释器：{exc}") from exc

        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        lines = [f"执行 {self.display_path(target)}，退出码 {completed.returncode}，超时阈值 {effective_timeout}s。"]
        if output:
            lines.append("--- stdout ---")
            lines.append(output)
        if error:
            lines.append("--- stderr ---")
            lines.append(error)
        if not output and not error:
            lines.append("（程序没有任何输出）")
        return ToolResult.success(
            truncate("\n".join(lines), self.config.max_observation_chars),
            returncode=completed.returncode, timeout=effective_timeout,
        )


__all__ = ["CheckSyntaxTool", "RunCodeTool"]
