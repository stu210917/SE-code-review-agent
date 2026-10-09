"""code-review-agent —— 基于 ReAct 循环的代码审查 Agent。

    from codereview import Config, ReviewAgent, build_default_registry

    config = Config.from_env(mock=True)
    agent = ReviewAgent(config)
    result = agent.review(["examples/buggy_sample.py"])
    print(result.report.to_markdown())

模块地图
--------
===================  ==========================================================
模块                 职责
===================  ==========================================================
``config``           配置加载与校验（CLI > 环境变量 > .env > 默认值）
``llm``              LLM 接入（OpenAI 兼容 + 离线 Mock + 重试装饰器）
``prompts``          全部提示词与 JSON 协议定义
``memory``           上下文记忆与压缩
``agent``            ReAct 主循环（思考 → 行动 → 反思 → 交付）
``tools``            工具层（文件、搜索、AST 分析、语法检查、受限执行）
``report``           报告数据结构与 Markdown/JSON 渲染
``cli`` / ``web``    命令行界面与 Web 界面
===================  ==========================================================
"""

from __future__ import annotations

from .agent import AgentEvent, AgentResult, ReviewAgent, parse_action
from .config import Config
from .errors import (
    ActionParseError,
    AgentError,
    CodeReviewError,
    ConfigError,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    MaxStepsExceeded,
    PathSecurityError,
    ToolArgumentError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
)
from .llm import BaseLLM, LLMResponse, MockLLM, OpenAICompatLLM, RetryingLLM, create_llm
from .memory import Memory, Message
from .report import Issue, ReviewReport
from .tools import Tool, ToolRegistry, ToolResult, build_default_registry

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 核心
    "Config",
    "ReviewAgent",
    "AgentResult",
    "AgentEvent",
    "parse_action",
    # 记忆与报告
    "Memory",
    "Message",
    "ReviewReport",
    "Issue",
    # LLM
    "BaseLLM",
    "LLMResponse",
    "OpenAICompatLLM",
    "MockLLM",
    "RetryingLLM",
    "create_llm",
    # 工具
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "build_default_registry",
    # 异常
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
