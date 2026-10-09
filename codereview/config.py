"""配置管理：命令行参数 > 环境变量 > .env 文件 > 内置默认值。

设计要点
--------
1. **零依赖**：自己解析 ``.env``，不引入 python-dotenv。
2. **可测试**：``Config.from_env()`` 接受显式覆盖，测试里不必污染真实环境变量。
3. **安全**：``describe()`` 输出会脱敏 api_key，避免密钥被打进日志或报告。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

ENV_PREFIX = "CR_"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


@dataclass
class Config:
    """Agent 的全部可调参数。"""

    # ---- LLM 接入 ----
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: float = 60.0

    # ---- 重试策略 ----
    max_retries: int = 3
    backoff_base: float = 1.5

    # ---- Agent 行为 ----
    max_steps: int = 12
    memory_token_budget: int = 6000
    max_observation_chars: int = 6000
    repeat_action_limit: int = 2

    # ---- 工具与沙箱 ----
    workspace: Path = field(default_factory=Path.cwd)
    allow_exec: bool = False
    exec_timeout: float = 10.0
    max_file_bytes: int = 200_000

    # ---- 输出 ----
    mock: bool = False
    verbose: bool = False
    color: bool = True

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self._validate()

    def _validate(self) -> None:
        if self.max_steps < 1:
            raise ConfigError("max_steps 必须 >= 1")
        if self.max_retries < 0:
            raise ConfigError("max_retries 不能为负数")
        if not 0.0 <= self.temperature <= 2.0:
            raise ConfigError("temperature 必须位于 [0, 2]")
        if self.timeout <= 0:
            raise ConfigError("timeout 必须为正数")
        if self.memory_token_budget < 500:
            raise ConfigError("memory_token_budget 过小（< 500），无法保留必要上下文")
        if not self.workspace.is_dir():
            raise ConfigError(f"工作区不存在或不是目录：{self.workspace}")

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | Path | None = None,
        **overrides: Any,
    ) -> "Config":
        """按优先级组装配置。

        :param env: 环境变量映射，默认取 ``os.environ``（测试时可注入假环境）。
        :param dotenv_path: ``.env`` 文件路径，默认在当前目录与项目根目录查找。
        :param overrides: 显式覆盖项，通常来自命令行参数；值为 ``None`` 时忽略。
        """
        env = dict(os.environ if env is None else env)
        merged: dict[str, str] = {}
        merged.update(_load_dotenv(dotenv_path))

        # 真实环境变量（CR_XXX）优先级高于 .env 文件
        for name in _ENV_KEYS:
            prefixed = f"{ENV_PREFIX}{name.upper()}"
            if prefixed in env:
                merged[name] = env[prefixed]

        kwargs: dict[str, Any] = {}
        for name, caster in _ENV_KEYS.items():
            raw = merged.get(name)
            if raw is None or raw == "":
                continue
            try:
                kwargs[name] = caster(raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}{name.upper()} 取值非法：{raw!r}（{exc}）") from exc

        # 兼容业界通用变量名，降低接入成本
        if "base_url" not in kwargs and env.get("OPENAI_BASE_URL"):
            kwargs["base_url"] = env["OPENAI_BASE_URL"]
        if "api_key" not in kwargs and env.get("OPENAI_API_KEY"):
            kwargs["api_key"] = env["OPENAI_API_KEY"]
        if "model" not in kwargs and env.get("OPENAI_MODEL"):
            kwargs["model"] = env["OPENAI_MODEL"]

        for key, value in overrides.items():
            if value is not None:
                kwargs[key] = value

        return cls(**kwargs)

    # ------------------------------------------------------------------ #
    # 派生属性
    # ------------------------------------------------------------------ #
    @property
    def effective_mock(self) -> bool:
        """没有 api_key 时自动降级到离线 Mock，保证开箱即用。"""
        return self.mock or not self.api_key.strip()

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def describe(self) -> dict[str, Any]:
        """用于启动横幅与报告元信息，**已脱敏**。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": _mask(self.api_key),
            "mode": "mock（离线演示）" if self.effective_mock else "live（真实 LLM）",
            "max_steps": self.max_steps,
            "memory_token_budget": self.memory_token_budget,
            "allow_exec": self.allow_exec,
            "workspace": str(self.workspace),
        }

    def with_overrides(self, **overrides: Any) -> "Config":
        """返回替换若干字段后的新配置（不可变用法，便于并行测试）。"""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)


def _mask(secret: str) -> str:
    """密钥脱敏：保留前 3 位与后 4 位。"""
    secret = (secret or "").strip()
    if not secret:
        return "(未设置)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:3]}{'*' * (len(secret) - 7)}{secret[-4:]}"


def _to_bool(raw: str) -> bool:
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError("期望布尔值（true/false/1/0/yes/no）")


def _to_path(raw: str) -> Path:
    return Path(str(raw)).expanduser()


#: 配置名 → 类型转换器。环境变量名 = ``CR_`` + 配置名大写。
_ENV_KEYS: dict[str, Any] = {
    "base_url": str,
    "api_key": str,
    "model": str,
    "temperature": float,
    "max_tokens": int,
    "timeout": float,
    "max_retries": int,
    "backoff_base": float,
    "max_steps": int,
    "memory_token_budget": int,
    "max_observation_chars": int,
    "allow_exec": _to_bool,
    "exec_timeout": float,
    "max_file_bytes": int,
    "mock": _to_bool,
    "verbose": _to_bool,
    "color": _to_bool,
    "workspace": _to_path,
}


def _load_dotenv(dotenv_path: str | Path | None = None) -> dict[str, str]:
    """解析 ``.env`` 文件。

    支持 ``KEY=VALUE``、``export KEY=VALUE``、``#`` 注释与成对引号。
    键名统一转小写；带 ``CR_`` 前缀的与不带前缀的都可识别（后者更简洁）。
    真实环境变量的优先级由调用方通过「后写入覆盖」保证。
    """
    candidates: list[Path] = []
    if dotenv_path is not None:
        candidates.append(Path(dotenv_path))
    else:
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for path in candidates:
        if not path.is_file():
            continue
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key.startswith(ENV_PREFIX.lower()):
                key = key[len(ENV_PREFIX):]
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                result[key] = value
        return result
    return {}


__all__ = ["Config", "ConfigError", "DEFAULT_BASE_URL", "DEFAULT_MODEL", "ENV_PREFIX"]
