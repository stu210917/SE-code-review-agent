"""上下文记忆（Context Memory）。

Agent 的「记忆」分两层，这是本项目架构上的一个关键设计：

* **完整历史（:attr:`Memory.history`）** —— 每一步的推理与工具输出都原样保留，
  用于生成审查报告、回放轨迹、做失败复盘；它不受上下文窗口限制。
* **提示视图（:meth:`Memory.build_messages`）** —— 真正发给 LLM 的消息列表。
  当它超出 token 预算时，会把较早的观察**折叠成摘要**（而不是直接丢弃），
  保证模型既能看到「我做过什么」，又不会撑爆窗口。

压缩是**确定性**的（按行截断 + 计数），不额外调用 LLM：
这样既省钱，又让测试可以断言压缩结果，避免「压缩本身引入不确定性」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .utils import estimate_tokens, truncate

#: 摘要里每条记录保留的最大字符数
DIGEST_LINE_CHARS = 160

#: 进度台账的前缀标记。Agent 会用它识别「哪些动作真的执行过」，
#: 因此这个字符串是协议的一部分，不要随意更改。
LEDGER_PREFIX = "[进度]"

#: 台账里最多列出多少条记录
LEDGER_MAX_ENTRIES = 10


@dataclass
class Message:
    """一条对话消息（比 OpenAI 的原始 dict 多带若干可观测字段）。"""

    role: str
    content: str
    kind: str = "text"  # system / task / assistant / observation / correction
    tool: str = ""
    ok: bool = True
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.content)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    def digest_line(self) -> str:
        """摘要行：``- [tool] 首行内容``，让人和模型都能快速扫过。"""
        first_line = self.content.strip().splitlines()[0] if self.content.strip() else "（空）"
        label = self.tool or self.kind
        status = "" if self.ok else " [失败]"
        return f"- [{label}]{status} {truncate(first_line, DIGEST_LINE_CHARS)}"


@dataclass
class MemoryStats:
    """一次 ``build_messages`` 的统计信息，用于日志与测试。"""

    messages: int = 0
    prompt_tokens: int = 0
    dropped: int = 0
    compressed: bool = False
    budget: int = 0
    history: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "prompt_tokens": self.prompt_tokens,
            "dropped": self.dropped,
            "compressed": self.compressed,
            "budget": self.budget,
            "history": self.history,
        }


class Memory:
    """对话记忆 + 上下文压缩。"""

    def __init__(self, token_budget: int = 6000, *, keep_recent: int = 8, max_observation_chars: int = 6000) -> None:
        if token_budget < 500:
            raise ValueError("token_budget 过小，至少需要 500")
        self.token_budget = token_budget
        self.keep_recent = keep_recent
        self.max_observation_chars = max_observation_chars
        self.system_prompt: str = ""
        self.task_prompt: str = ""
        self.history: list[Message] = []
        #: 每一步的完整轨迹（供报告与回放使用，不参与压缩）
        self.trace: list[dict[str, Any]] = []
        self.last_stats = MemoryStats()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def set_system(self, content: str) -> None:
        self.system_prompt = content

    def set_task(self, content: str) -> None:
        self.task_prompt = content

    def add(self, message: Message) -> Message:
        self.history.append(message)
        return message

    def add_user(self, content: str, *, kind: str = "text") -> Message:
        return self.add(Message(role="user", content=content, kind=kind))

    def add_assistant(self, content: str) -> Message:
        return self.add(Message(role="assistant", content=content, kind="assistant"))

    def add_observation(self, tool: str, content: str, *, ok: bool = True) -> Message:
        """记录一次工具调用结果。超长输出在**写入时**就截断，避免污染后续所有轮次。"""
        body = truncate(content, self.max_observation_chars)
        status = "" if ok else " [ERROR]"
        return self.add(
            Message(role="user", content=f"Observation from `{tool}`{status}:\n{body}",
                    kind="observation", tool=tool, ok=ok)
        )

    def add_correction(self, content: str) -> Message:
        """把「格式纠正」作为一条系统提醒塞回去。"""
        return self.add(Message(role="user", content=content, kind="correction"))

    def add_trace(self, step: dict[str, Any]) -> None:
        self.trace.append(step)

    def clear(self) -> None:
        self.history.clear()
        self.trace.clear()
        self.last_stats = MemoryStats()

    # ------------------------------------------------------------------ #
    # 读取（带压缩）
    # ------------------------------------------------------------------ #
    def build_messages(self) -> list[dict[str, str]]:
        """构造发给 LLM 的消息列表，必要时压缩早期观察。"""
        head: list[Message] = []
        if self.system_prompt:
            head.append(Message(role="system", content=self.system_prompt, kind="system"))
        if self.task_prompt:
            head.append(Message(role="user", content=self.task_prompt, kind="task"))

        base_tokens = sum(message.tokens for message in head)
        recent = self.history[-self.keep_recent :] if self.keep_recent > 0 else []
        older = self.history[: len(self.history) - len(recent)]

        recent_tokens = sum(message.tokens for message in recent)
        remaining = self.token_budget - base_tokens - recent_tokens

        # 从「较新」向「较旧」回填，装不下的一律进摘要
        kept: list[Message] = []
        dropped: list[Message] = []
        for message in reversed(older):
            if remaining - message.tokens >= 0:
                kept.append(message)
                remaining -= message.tokens
            else:
                dropped.append(message)
        kept.reverse()
        dropped.reverse()

        messages: list[Message] = list(head)
        # 进度台账：由**永不压缩**的 trace 生成，保证「我做过什么」不会因为
        # 上下文压缩而丢失（否则 Agent 会忘记已完成的步骤并重新开始）。
        ledger = self.render_ledger()
        if ledger:
            messages.append(Message(role="user", content=ledger, kind="ledger"))
        if dropped:
            digest = self._render_digest(dropped)
            messages.append(Message(role="user", content=digest, kind="digest"))
        messages.extend(kept)
        messages.extend(recent)

        payload = [message.to_dict() for message in messages]
        self.last_stats = MemoryStats(
            messages=len(payload),
            prompt_tokens=sum(estimate_tokens(item["content"]) for item in payload),
            dropped=len(dropped),
            compressed=bool(dropped),
            budget=self.token_budget,
            history=len(self.history),
        )
        return payload

    def _render_digest(self, dropped: Sequence[Message]) -> str:
        """把被挤出去的观察折叠成一段摘要。"""
        lines = [f"[上下文压缩] 为控制窗口，以下 {len(dropped)} 条较早期的交互已被折叠为摘要："]
        lines.extend(message.digest_line() for message in dropped)
        lines.append("如需重新查看某个文件的完整内容，请再次调用相应工具。")
        return "\n".join(lines)

    def render_ledger(self) -> str:
        """渲染「已完成动作」台账。

        这是本项目踩过的一个真实坑：早期版本只依赖消息历史来判断
        「哪些工具已经调用过」，结果上下文一旦压缩、早期的 assistant 消息被折叠，
        Agent 就误以为还没开始工作，把整套计划重跑一遍，白白烧掉步数预算。
        修法是让台账来自**不参与压缩**的 ``trace``，并把它作为一条独立消息
        常驻在提示里（同时也让真实模型知道「这些事已经做过了」）。

        台账中的每一行都是合法 JSON，因此可以被程序化解析。
        """
        if not self.trace:
            return ""
        entries = self.trace[-LEDGER_MAX_ENTRIES:]
        lines = [f"{LEDGER_PREFIX} 你已完成 {len(self.trace)} 次工具调用，请勿重复同样的调用："]
        for entry in entries:
            arguments = entry.get("arguments") or "{}"
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            marker = "" if entry.get("ok", True) else "  # 该调用失败，请换一种方式"
            action = json.dumps(str(entry.get("action", "")), ensure_ascii=False)
            lines.append(f'{{"action": {action}, "action_input": {arguments}}}{marker}')
        if len(self.trace) > len(entries):
            lines.append(f"（更早的 {len(self.trace) - len(entries)} 次调用已省略）")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def observations(self) -> Iterable[Message]:
        return (message for message in self.history if message.kind == "observation")

    def last_observation(self) -> Message | None:
        for message in reversed(self.history):
            if message.kind == "observation":
                return message
        return None

    def signature(self) -> str:
        """近期消息指纹，用于检测「模型在原地打转」。"""
        recent = self.history[-4:]
        return "|".join(f"{message.role}:{message.content[:60]}" for message in recent)

    def __len__(self) -> int:
        return len(self.history)


__all__ = ["Memory", "Message", "MemoryStats", "LEDGER_PREFIX", "LEDGER_MAX_ENTRIES"]
