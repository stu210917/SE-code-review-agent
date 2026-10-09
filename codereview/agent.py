"""Agent 主循环：输入 → 推理 → 工具调用 → 输出。

一个回合（turn）的生命周期
--------------------------
::

    ┌─────────────┐
    │  1. 思考     │  build_messages() → LLM.chat() → 解析 JSON
    │  (think)    │  解析失败 → 注入纠正提示，让模型自我修复（最多 2 次）
    └──────┬──────┘
           │ 得到 {"action": ..., "action_input": ...}
    ┌──────▼──────┐
    │  2. 行动     │  ToolRegistry.execute() —— 任何异常都变成一条失败的 Observation
    │  (act)      │
    └──────┬──────┘
           │ 得到 Observation
    ┌──────▼──────┐
    │  3. 反思     │  写入记忆、检测重复调用（防打转）、累计统计
    │  (reflect)  │
    └──────┬──────┘
           │ 得到 {"final_answer": {...}} 或步数耗尽
    ┌──────▼──────┐
    │  4. 交付     │  ReportBuilder → Markdown / JSON
    └─────────────┘

三道保险让 Agent「不会崩」：

1. **工具层**：工具内部异常 → 失败观察，模型自行换策略；
2. **协议层**：模型输出不合协议 → 纠正提示 + 有限次重试；
3. **循环层**：超过 ``max_steps`` 或原地打转 → **降级报告**，把已经拿到的
   AST 分析结果整理成报告，而不是抛异常让用户两手空空。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Config
from .errors import ActionParseError, LLMError, MaxStepsExceeded
from .llm import BaseLLM, create_llm
from .memory import Memory
from .prompts import build_correction_prompt, build_system_prompt, build_task_prompt
from .report import Issue, ReviewReport, timestamp
from .tools.base import ToolRegistry, ToolResult, build_default_registry
from .utils import extract_json_objects, repair_json, strip_code_fence, truncate

#: 模型有时会用这些词表示「我结束了」
FINAL_ALIASES = {"final_answer", "final", "finish", "finished", "done", "answer", "submit", "conclude", "end"}

#: 一次审查中允许模型输出几次不合协议的内容
MAX_CORRECTIONS = 2


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class AgentEvent:
    """运行期事件，供 CLI / Web 实时展示（也是可观测性的接口）。"""

    kind: str  # start / step / thought / action / observation / correction / retry / final / degrade
    step: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 便于调试
        return f"<AgentEvent {self.kind} step={self.step} {self.data}>"


@dataclass
class ParsedAction:
    """模型一次回复的解析结果。"""

    kind: str  # "tool" 或 "final"
    thought: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    final: Any = None


@dataclass
class AgentResult:
    """一次审查的完整产出。"""

    report: ReviewReport
    steps: int = 0
    tool_calls: int = 0
    duration_s: float = 0.0
    error: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: 本次运行使用的记忆对象；交互模式下把它传回给下一轮即可实现多轮上下文
    memory: "Memory | None" = None
    #: 运行结局：``ok`` 正常收敛 ｜ ``degraded`` 降级交付（报告仍可用）｜ ``error`` 彻底失败
    outcome: str = "ok"

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def to_dict(self) -> dict[str, Any]:
        payload = self.report.to_dict()
        payload["run"] = {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "duration_s": round(self.duration_s, 2),
            "outcome": self.outcome,
            "error": self.error,
        }
        return payload


# --------------------------------------------------------------------------- #
# 协议解析
# --------------------------------------------------------------------------- #
def parse_action(raw: str) -> ParsedAction:
    """把模型的一段文本解析成动作。

    依次尝试：去代码块围栏 → 直接 ``json.loads`` → 修复尾随逗号 → 括号配对扫描。
    全部失败才抛 :class:`ActionParseError`，此时错误信息会被写回给模型让它自纠。
    """
    text = strip_code_fence(raw or "")
    payload: Any = None

    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload = json.loads(repair_json(text))
            except json.JSONDecodeError:
                candidates = extract_json_objects(text)
                if candidates:
                    # 取字段最多的那个对象，通常就是真正的动作
                    payload = max(candidates, key=lambda item: len(item) if isinstance(item, dict) else 0)

    if payload is None:
        raise ActionParseError(f"输出中找不到合法的 JSON 对象：{(raw or '')[:200]!r}")
    if not isinstance(payload, dict):
        raise ActionParseError(f"顶层结构必须是 JSON 对象，收到 {type(payload).__name__}")

    thought = str(payload.get("thought", "") or payload.get("reasoning", "") or "")

    if "final_answer" in payload:
        final = payload["final_answer"]
        if final is None or final == {}:
            final = {key: value for key, value in payload.items() if key not in ("final_answer", "thought")}
        return ParsedAction(kind="final", thought=thought, final=final)

    action = payload.get("action") or payload.get("tool") or payload.get("name")
    if isinstance(action, str) and action.strip():
        normalized = action.strip().lower()
        arguments = payload.get("action_input", payload.get("arguments", payload.get("parameters", {})))
        if normalized in FINAL_ALIASES:
            return ParsedAction(kind="final", thought=thought, final=arguments)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ActionParseError(
                f"action_input 必须是 JSON 对象，收到 {type(arguments).__name__}；"
                '正确写法：{"thought": "...", "action": "%s", "action_input": {...}}' % action
            )
        return ParsedAction(kind="tool", thought=thought, tool=action.strip(), arguments=arguments)

    # 兜底：模型直接给出了结论字段
    if any(key in payload for key in ("summary", "issues", "score")):
        return ParsedAction(kind="final", thought=thought, final=payload)

    raise ActionParseError(
        "JSON 里既没有 `action` 也没有 `final_answer` 字段；"
        '请输出 {"thought": "...", "action": "工具名", "action_input": {...}} '
        '或 {"thought": "...", "final_answer": {...}}'
    )


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ReviewAgent:
    """代码审查 Agent。"""

    def __init__(
        self,
        config: Config,
        *,
        llm: BaseLLM | None = None,
        registry: ToolRegistry | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry if registry is not None else build_default_registry(config)
        self._on_event = on_event
        self.retry_count = 0
        #: 静态分析证据库：在工具返回的那一刻就落地，不依赖（会被压缩的）消息历史。
        #: 这是本项目的另一条重要经验——**证据要在产生时收集，而不是在结论时回捞**。
        self._findings: dict[tuple[str, int, str], Issue] = {}
        self.llm = llm if llm is not None else create_llm(config, on_retry=self._on_retry)

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def _emit(self, kind: str, step: int = 0, **data: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(AgentEvent(kind=kind, step=step, data=data))
        except Exception:  # pragma: no cover - 展示层的问题不该影响 Agent
            pass

    def _on_retry(self, attempt: int, delay: float, exc: Exception) -> None:
        self.retry_count += 1
        self._emit("retry", attempt=attempt, delay=round(delay, 2), error=str(exc)[:200])

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def review(
        self,
        targets: Sequence[str],
        *,
        focus: str = "",
        language: str = "auto",
        extra_instructions: str = "",
        session: Memory | None = None,
        follow_up: str = "",
    ) -> AgentResult:
        """对 ``targets`` 执行一轮完整审查。

        :param targets: 相对工作区的文件或目录列表。
        :param focus: 额外关注点（会写进 Prompt）。
        :param language: 目标语言提示，默认自动。
        :param session: 复用一段既有记忆（交互模式下实现「上下文记忆」）。
        :param follow_up: 追加到既有记忆的用户追问；仅在传入 ``session`` 时有意义。
        """
        started = time.perf_counter()
        targets = list(targets) or ["."]
        self._findings.clear()

        if session is None:
            memory = Memory(
                self.config.memory_token_budget,
                max_observation_chars=self.config.max_observation_chars,
            )
            memory.set_system(build_system_prompt(self.registry.tools(), extra_instructions))
            memory.set_task(
                build_task_prompt(targets, workspace=str(self.config.workspace), focus=focus, language=language)
            )
        else:
            memory = session
            if follow_up:
                memory.add_user(follow_up, kind="follow_up")

        counters: dict[str, Any] = {
            "tool_calls": 0,
            "tools_used": [],
            "corrections": 0,
            "total_tokens": 0,
            "compressions": 0,
        }
        call_signatures: dict[str, int] = {}
        nudged: set[str] = set()
        step = 0
        messages = memory.build_messages()
        self._emit("start", tools=self.registry.names(), prompt_tokens=memory.last_stats.prompt_tokens)

        while step < self.config.max_steps:
            step += 1
            counters["compressions"] += int(memory.last_stats.compressed)

            # ---------- 1. 思考 ----------
            try:
                response = self.llm.chat(messages)
            except LLMError as exc:
                return self._finish_with_error(memory, targets, started, step, counters, exc)

            counters["total_tokens"] += response.total_tokens or 0
            memory.add_assistant(response.content)
            self._emit("thought", step=step, raw=response.content[:400], latency_ms=response.latency_ms)

            # ---------- 解析 ----------
            try:
                action = parse_action(response.content)
            except ActionParseError as exc:
                counters["corrections"] += 1
                self._emit("correction", step=step, error=str(exc)[:200])
                if counters["corrections"] > MAX_CORRECTIONS:
                    return self._degrade(
                        memory, targets, started, step, counters,
                        reason=f"模型连续 {counters['corrections']} 次输出不合协议：{exc}",
                    )
                memory.add_correction(build_correction_prompt(str(exc), response.content))
                messages = memory.build_messages()
                continue

            if action.thought:
                self._emit("thought_text", step=step, text=action.thought[:300])

            # ---------- 4. 交付（最终结论） ----------
            if action.kind == "final":
                return self._finish(memory, targets, started, step, counters, action)

            # ---------- 2. 行动 ----------
            signature = f"{action.tool}:{json.dumps(action.arguments, sort_keys=True, ensure_ascii=False)}"
            call_signatures[signature] = call_signatures.get(signature, 0) + 1
            repeats = call_signatures[signature]

            if repeats > self.config.repeat_action_limit:
                self._emit("loop_detected", step=step, tool=action.tool, repeats=repeats)
                if signature in nudged:
                    return self._degrade(
                        memory, targets, started, step, counters,
                        reason=f"模型在 `{action.tool}` 上重复调用 {repeats} 次且没有新信息，判定为死循环",
                    )
                nudged.add(signature)
                memory.add_correction(
                    f"你已经用完全相同的参数调用过 `{action.tool}` {repeats} 次，结果不会改变。"
                    "请换一个工具、换一组参数，或直接输出 final_answer 给出结论。"
                )
                messages = memory.build_messages()
                continue

            self._emit("action", step=step, tool=action.tool, arguments=action.arguments)
            result = self.registry.execute(action.tool, action.arguments)
            counters["tool_calls"] += 1
            if action.tool not in counters["tools_used"]:
                counters["tools_used"].append(action.tool)
            self._record_findings(result)

            # ---------- 3. 反思 ----------
            memory.add_observation(action.tool, result.output or result.error, ok=result.ok)
            memory.add_trace({
                "step": step,
                "thought": action.thought,
                "action": action.tool,
                "arguments": json.dumps(action.arguments, ensure_ascii=False),
                "result": result.summary,
                "ok": result.ok,
            })
            self._emit(
                "observation", step=step, tool=action.tool, ok=result.ok,
                summary=result.summary, output=truncate(result.output or result.error, 400),
            )
            messages = memory.build_messages()
            if memory.last_stats.compressed:
                self._emit("compressed", step=step, **memory.last_stats.to_dict())

        # ---------- 步数耗尽 → 降级 ----------
        return self._degrade(
            memory, targets, started, step, counters,
            reason=f"已达到最大推理步数 {self.config.max_steps}，为避免无限循环而提前收敛",
            exc=MaxStepsExceeded("max_steps reached", steps=step),
        )

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #
    def _meta(
        self,
        memory: Memory,
        targets: Sequence[str],
        started: float,
        step: int,
        counters: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        meta = {
            "generated_at": timestamp(),
            "model": self.llm.name if self.config.effective_mock else self.config.model,
            "mode": "mock（离线演示）" if self.config.effective_mock else "live（真实 LLM）",
            "targets": list(targets),
            "workspace": str(self.config.workspace),
            "steps": step,
            "tool_calls": counters["tool_calls"],
            "tools_used": counters["tools_used"],
            "total_tokens": counters["total_tokens"],
            "llm_attempts": self.retry_count,
            "compressions": counters["compressions"],
            "duration_s": round(time.perf_counter() - started, 2),
            "trace": memory.trace,
        }
        meta.update(extra)
        return meta

    def _finish(
        self,
        memory: Memory,
        targets: Sequence[str],
        started: float,
        step: int,
        counters: dict[str, Any],
        action: ParsedAction,
    ) -> AgentResult:
        meta = self._meta(memory, targets, started, step, counters)
        report = ReviewReport.from_final_answer(action.final, meta)

        # 静态分析是确定性证据，不允许因为「模型没提」或「上下文被压缩」而凭空消失。
        # 这里把证据库里尚未出现在结论中的条目补进报告，并明确标注来源。
        merged = self._merge_findings(report.issues)
        if merged:
            report.issues = sorted(
                report.issues + merged, key=lambda issue: (issue.rank, issue.file, issue.line)
            )
            report.meta["note"] = (
                f"问题清单中有 {len(merged)} 条由静态分析（AST 规则引擎）在模型结论之外补充，"
                "用于确保确定性证据不因上下文压缩或模型遗漏而丢失；"
                "因此下方统计与清单的条目数会多于概述文字中提到的数量。"
            )
        duration = time.perf_counter() - started
        self._emit("final", step=step, score=report.score, issues=len(report.issues))
        return AgentResult(
            report=report, steps=step, tool_calls=counters["tool_calls"],
            duration_s=duration, trace=memory.trace, memory=memory, outcome="ok",
        )

    def _degrade(
        self,
        memory: Memory,
        targets: Sequence[str],
        started: float,
        step: int,
        counters: dict[str, Any],
        *,
        reason: str,
        exc: Exception | None = None,
    ) -> AgentResult:
        """降级交付：把已经拿到的静态分析结果整理成一份可用报告。"""
        issues = sorted(self._findings.values(), key=lambda issue: (issue.rank, issue.file, issue.line))
        counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
        for issue in issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        summary = (
            f"本次审查未能正常收敛（{reason}）。以下结论来自已经完成的静态分析，"
            f"共 {len(issues)} 条（严重 {counts['critical']} / 重要 {counts['major']} / "
            f"次要 {counts['minor']} / 提示 {counts['info']}），可作为初步参考；"
            "建议提高 --max-steps 或更换模型后重新运行以获得完整结论。"
        )
        meta = self._meta(memory, targets, started, step, counters, degraded=True)
        report = ReviewReport(
            summary=summary, score=0, issues=issues, meta=meta,
            degraded=True, degraded_reason=reason,
        )
        self._emit("degrade", step=step, reason=reason, issues=len(issues))
        return AgentResult(
            report=report, steps=step, tool_calls=counters["tool_calls"],
            duration_s=time.perf_counter() - started,
            error="" if exc is None else f"{type(exc).__name__}: {exc}",
            trace=memory.trace, memory=memory, outcome="degraded",
        )

    def _finish_with_error(
        self,
        memory: Memory,
        targets: Sequence[str],
        started: float,
        step: int,
        counters: dict[str, Any],
        exc: Exception,
    ) -> AgentResult:
        """LLM 彻底不可用（重试耗尽）时的收尾：保留已获得的信息。"""
        reason = f"LLM 调用失败：{type(exc).__name__}: {exc}"
        result = self._degrade(memory, targets, started, step, counters, reason=reason, exc=exc)
        result.error = reason
        result.outcome = "error"
        return result

    def _record_findings(self, result: ToolResult) -> None:
        """把工具返回的静态分析结论存入证据库（工具执行成功时立即调用）。"""
        if not result.ok or not result.output:
            return
        for payload in extract_json_objects(result.output):
            if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
                continue
            default_path = str(payload.get("path", ""))
            for finding in payload["findings"]:
                if not isinstance(finding, dict):
                    continue
                enriched = dict(finding)
                enriched.setdefault("file", default_path)
                issue = Issue.from_dict(enriched)
                if issue is None:
                    continue
                self._findings.setdefault((issue.file, issue.line, issue.title), issue)

    def _merge_findings(self, existing: Sequence[Issue]) -> list[Issue]:
        """返回证据库中尚未被模型结论覆盖的条目。

        判重按**位置**（文件 + 行号）而不是标题：模型用自己的话描述了同一处问题，
        标题几乎不可能和规则名逐字相同；按标题判重会导致同一个 Bug 被报两遍。
        """
        def key_of(issue: Issue) -> tuple[str, int, str]:
            return (issue.file, issue.line, "") if issue.line else (issue.file, 0, issue.title)

        known = {key_of(issue) for issue in existing}
        merged: list[Issue] = []
        for issue in self._findings.values():
            key = key_of(issue)
            if key in known:
                continue
            known.add(key)
            merged.append(issue)
        return merged


__all__ = ["ReviewAgent", "AgentEvent", "AgentResult", "ParsedAction", "parse_action", "FINAL_ALIASES"]
