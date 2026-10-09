"""Prompt 设计集中地。

本模块把所有会发给 LLM 的文本集中在一处，好处是：

* **可审阅**：Agent 的行为几乎完全由这里决定，评审时不需要翻遍源码；
* **可迭代**：调整提示词不会牵动业务逻辑，便于做 A/B 对比；
* **可测试**：``tests/test_prompts.py`` 可以直接断言协议片段是否存在。

协议选择说明
------------
我们没有使用 OpenAI 的 ``tools`` / function-calling 字段，而是采用
「**结构化 JSON 文本协议**」（ReAct 变体）：

1. 换一家模型（DeepSeek / 通义 / Ollama / 本地部署）时不必重写调用层；
2. 模型一旦输出不合法，可以用纯文本方式纠正它（见 ``build_correction_prompt``），
   而原生 function-calling 的格式错误通常直接由 SDK 抛出，Agent 无从补救；
3. 推理轨迹（thought）与动作（action）同时可见，便于做可观测性和报告。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

#: 任务上下文的机器可读标记，供离线 Mock 与日志解析使用
TASK_CONTEXT_OPEN = "<TASK_CONTEXT>"
TASK_CONTEXT_CLOSE = "</TASK_CONTEXT>"

ACTION_KEY = "action"
FINAL_KEY = "final_answer"

SYSTEM_PROMPT = """你是一名资深代码审查工程师（Senior Code Reviewer），负责对一个代码仓库进行**静态审查**，\
发现潜在的 Bug、代码异味与安全风险，并给出可执行的改进建议。

# 你可以使用的工具
{tools}

# 工作流程（必须遵守）
1. **先侦察，再下结论**。在给出任何结论之前，必须先用 `list_files` 了解仓库结构，\
再用 `read_file` / `analyze_code` / `search_code` 获取证据。
2. **证据驱动**。每一条问题都必须来自你实际读到的代码或工具输出，禁止凭经验臆测。\
引用问题时要给出 `文件路径:行号`。
3. **先自动分析，再人工阅读**。`analyze_code` 会返回确定性的静态分析结果（复杂度、异常处理、\
可变默认参数等），请优先使用它的结论，再结合 `read_file` 的原文判断误报。
4. **控制预算**。不要重复读取同一个文件；每次工具调用只获取你真正需要的信息。
5. **审查完成后立刻收敛**。当你已经覆盖了主要文件、或连续两次工具调用都没有新信息时，\
输出最终结论，不要无限探索。

# 输出格式（极其重要）
你**每次回复只能输出一个 JSON 对象**，不要输出任何解释性文字、不要用 Markdown 代码块包裹。

调用工具时：
{{"thought": "我为什么要调用这个工具（一句话）", "action": "工具名", "action_input": {{"参数名": "参数值"}}}}

给出最终结论时：
{{"thought": "我已经掌握了足够信息", "final_answer": {{
  "summary": "整体评价，2-4 句话，包含代码整体质量判断",
  "score": 0-100 的整数,
  "issues": [
    {{
      "file": "相对于仓库根目录的路径",
      "line": 行号（整数）,
      "severity": "critical | major | minor | info",
      "category": "bug | security | performance | maintainability | style | test",
      "title": "问题标题（不超过 30 字）",
      "detail": "问题说明：为什么这是问题、在什么情况下会出错",
      "suggestion": "具体的修改建议，最好给出修改后的代码片段"
    }}
  ],
  "highlights": ["做得好的地方"],
  "next_steps": ["建议接下来做的事"]
}}}}

# 严重度标准
- `critical`：会导致程序崩溃、数据损坏或存在安全漏洞（如 SQL 注入、eval 执行不可信输入）。
- `major`：逻辑错误、未处理的异常路径、资源泄漏、并发问题。
- `minor`：可维护性问题（重复代码、命名不当、函数过长、硬编码）。
- `info`：风格建议、文档缺失、可选优化。

# 禁止事项
- 不要编造文件中不存在的代码。
- 不要在 `final_answer` 之外输出任何文字。
- 不要输出 JSON 注释或尾随逗号。
- 如果代码质量确实很好，就诚实地少报问题——**不要为了凑数而制造问题**。
"""

TOOL_CALL_EXAMPLE = """示例 1（侦察仓库结构）：
{"thought": "先了解仓库里有哪些文件，再决定审查顺序", "action": "list_files", "action_input": {"path": ".", "pattern": "*.py"}}

示例 2（读取具体文件）：
{"thought": "需要看 utils.py 的实现细节", "action": "read_file", "action_input": {"path": "src/utils.py"}}

示例 3（搜索危险模式）：
{"thought": "确认是否存在 eval 或裸 except", "action": "search_code", "action_input": {"pattern": "eval\\\\(|except\\\\s*:", "path": "."}}

示例 4（收敛，给出结论）：
{"thought": "已覆盖全部核心文件，可以给出结论", "final_answer": {"summary": "...", "score": 72, "issues": [], "highlights": [], "next_steps": []}}
"""

CORRECTION_PROMPT = """你上一条回复不符合协议，无法被解析。

解析错误：{error}
你的原始输出（节选）：
{raw}

请只输出**一个合法的 JSON 对象**，且必须满足：
- 要么包含 `"action"` 和 `"action_input"` 字段（调用工具）；
- 要么包含 `"final_answer"` 字段（给出最终结论）。
不要输出 Markdown 代码块标记，不要输出任何额外文字，不要使用尾随逗号。
"""


def build_system_prompt(tools: Sequence[Any], extra: str = "") -> str:
    """渲染系统提示词。

    包含三段：角色与工作流、工具清单、Few-shot 示例。
    :param tools: 工具对象序列，需要具备 ``name`` / ``description`` / ``parameters`` 属性。
    :param extra: 追加在末尾的额外指令（例如「本次只审查 diff」）。
    """
    prompt = SYSTEM_PROMPT.format(tools=describe_tools(tools))
    prompt += "\n# 输出示例（Few-shot）\n" + TOOL_CALL_EXAMPLE
    if extra:
        prompt += "\n# 本次任务的额外要求\n" + extra + "\n"
    return prompt


def describe_tools(tools: Iterable[Any]) -> str:
    """把工具注册表渲染成模型可读的清单（含 JSON Schema 风格的参数说明）。"""
    lines: list[str] = []
    for tool in tools:
        params = getattr(tool, "parameters", {}) or {}
        signature_parts: list[str] = []
        for name, spec in params.items():
            type_name = spec.get("type", "any") if isinstance(spec, dict) else "any"
            required = not (isinstance(spec, dict) and spec.get("required") is False)
            default = spec.get("default") if isinstance(spec, dict) else None
            piece = f"{name}: {type_name}"
            if not required:
                piece += f"（可选，默认 {default!r}）"
            desc = spec.get("description") if isinstance(spec, dict) else None
            if desc:
                piece += f" —— {desc}"
            signature_parts.append(piece)
        signature = ", ".join(signature_parts)
        lines.append(f"- `{tool.name}({signature})`：{tool.description}")
    return "\n".join(lines) if lines else "（本次没有可用工具）"


def build_task_prompt(
    targets: Sequence[str],
    *,
    workspace: str,
    focus: str = "",
    language: str = "auto",
    extra_context: str = "",
) -> str:
    """构造本轮任务的用户消息。

    末尾附带 ``<TASK_CONTEXT>`` 结构化块：它既方便人肉排查，也是离线 Mock 的规划依据，
    并且让日志可以被程序化解析。
    """
    target_list = "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(targets)) or "  （整个工作区）"
    focus_line = f"\n重点关注：{focus}" if focus else ""
    context_block = {
        "targets": list(targets),
        "workspace": workspace,
        "language": language,
        "focus": focus,
    }
    return f"""请对以下目标进行代码审查：
{target_list}

工作区根目录：{workspace}
语言偏好：{language}{focus_line}
{extra_context}
请按「先侦察 → 再分析 → 后结论」的顺序工作，最终输出 `final_answer`。

{TASK_CONTEXT_OPEN}{json.dumps(context_block, ensure_ascii=False)}{TASK_CONTEXT_CLOSE}"""


def build_correction_prompt(error: str, raw: str) -> str:
    """当模型输出无法解析时，喂回去让它自我修复（比直接重试更省 token）。"""
    return CORRECTION_PROMPT.format(error=error, raw=raw[:800])


def encode_task_context(context: dict[str, Any]) -> str:
    return f"{TASK_CONTEXT_OPEN}{json.dumps(context, ensure_ascii=False)}{TASK_CONTEXT_CLOSE}"


def extract_task_context(messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从消息历史里还原 ``<TASK_CONTEXT>`` 内容，找不到时返回空字典。"""
    for message in messages:
        content = message.get("content") or ""
        if TASK_CONTEXT_OPEN not in content:
            continue
        try:
            start = content.index(TASK_CONTEXT_OPEN) + len(TASK_CONTEXT_OPEN)
            end = content.index(TASK_CONTEXT_CLOSE, start)
            return json.loads(content[start:end])
        except (ValueError, json.JSONDecodeError):
            continue
    return {}


__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_CALL_EXAMPLE",
    "CORRECTION_PROMPT",
    "TASK_CONTEXT_OPEN",
    "TASK_CONTEXT_CLOSE",
    "build_system_prompt",
    "build_task_prompt",
    "build_correction_prompt",
    "describe_tools",
    "encode_task_context",
    "extract_task_context",
]
