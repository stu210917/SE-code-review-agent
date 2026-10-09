"""统一异常体系。

把「可重试」与「不可重试」的错误显式区分开，是 Agent 稳定运行的前提：
LLM 网络抖动应该退避重试，而工具参数写错时重试一万次也没有意义。

    CodeReviewError
      ├── ConfigError            配置缺失/非法
      ├── LLMError
      │     ├── LLMRequestError  网络/限流/5xx —— 可重试
      │     └── LLMResponseError 响应格式非法 —— 不可重试，交给上层纠错
      ├── ToolError
      │     ├── ToolNotFoundError
      │     ├── ToolArgumentError
      │     ├── ToolExecutionError
      │     └── PathSecurityError 越界访问工作区之外的文件
      └── AgentError
            ├── MaxStepsExceeded 超过最大推理步数
            └── ActionParseError 模型输出无法解析成动作
"""

from __future__ import annotations


class CodeReviewError(Exception):
    """本项目所有异常的基类。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class ConfigError(CodeReviewError):
    """配置项缺失或取值非法。"""


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
class LLMError(CodeReviewError):
    """LLM 调用相关错误的基类。"""


class LLMRequestError(LLMError):
    """请求层面的失败（断网、超时、429、5xx），**可重试**。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMResponseError(LLMError):
    """响应内容无法使用（空回复、JSON 解析失败、缺少字段），**不可重试**。"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
class ToolError(CodeReviewError):
    """工具相关错误的基类。

    工具抛出的错误不会让 Agent 崩溃：``ToolRegistry.execute`` 会把它转换成
    一条 ``Observation`` 回灌给模型，让模型自行决定换一种调用方式。
    """


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class ToolArgumentError(ToolError):
    """工具参数缺失、类型不对或取值越界。"""


class ToolExecutionError(ToolError):
    """工具在执行过程中失败（文件不存在、语法错误等）。"""


class PathSecurityError(ToolError):
    """试图访问工作区之外的路径，已被沙箱拦截。"""


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class AgentError(CodeReviewError):
    """Agent 主循环相关错误。"""


class MaxStepsExceeded(AgentError):
    """达到 ``max_steps`` 仍未给出最终结论。

    这不是致命错误：上层会用已有的中间发现（findings）降级生成一份报告。
    """

    def __init__(self, message: str, *, steps: int = 0) -> None:
        super().__init__(message)
        self.steps = steps


class ActionParseError(AgentError):
    """模型输出既不是合法动作，也不是最终结论。"""


__all__ = [
    "CodeReviewError",
    "ConfigError",
    "LLMError",
    "LLMRequestError",
    "LLMResponseError",
    "ToolError",
    "ToolNotFoundError",
    "ToolArgumentError",
    "ToolExecutionError",
    "PathSecurityError",
    "AgentError",
    "MaxStepsExceeded",
    "ActionParseError",
]
