"""工具包：Agent 与外部世界交互的唯一通道。

对外只暴露 :class:`Tool` / :class:`ToolResult` / :class:`ToolRegistry` 与
:func:`build_default_registry`，具体工具按需注册——这样新增一个工具
既不需要改 Agent 主循环，也不需要改 Prompt（工具清单是自动生成的）。
"""

from __future__ import annotations

from .base import Tool, ToolRegistry, ToolResult, build_default_registry
from .exec_tool import CheckSyntaxTool, RunCodeTool
from .fs import ListFilesTool, ReadFileTool, SearchCodeTool, iter_files
from .python_ast import AnalyzeCodeTool, PythonAnalyzer

__all__ = [
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "build_default_registry",
    "ListFilesTool",
    "ReadFileTool",
    "SearchCodeTool",
    "AnalyzeCodeTool",
    "CheckSyntaxTool",
    "RunCodeTool",
    "PythonAnalyzer",
    "iter_files",
]
