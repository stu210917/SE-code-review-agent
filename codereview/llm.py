"""LLM 接入层。

三个实现，同一个接口（``BaseLLM.chat``），可以互相替换：

* :class:`OpenAICompatLLM` —— 走 OpenAI 兼容的 ``/chat/completions``，
  可直接对接 OpenAI、DeepSeek、通义千问、Kimi、智谱、vLLM、Ollama 等；
* :class:`MockLLM` —— **离线规则版**，不联网、不需要 key，用确定性策略走完
  同一套 JSON 协议，保证任何人 clone 下来就能跑通演示与回归测试；
* :class:`RetryingLLM` —— 装饰器式包装，负责指数退避重试与可观测的回调。

重试只针对「可重试」错误（网络异常、429、5xx）；格式错误属于模型的锅，
交给 Agent 用 ``build_correction_prompt`` 自我纠正，重试没有意义。
"""

from __future__ import annotations

import json
import math
import random
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Config
from .errors import LLMRequestError, LLMResponseError
from .memory import LEDGER_PREFIX
from .prompts import extract_task_context
from .utils import estimate_tokens, extract_json_objects, mask_strings_and_comments

#: Mock 模式默认搜索的高危模式（正则）
RISKY_PATTERN = r"eval\(|exec\(|except\s*:|except Exception|==\s*None|TODO|FIXME|pickle\.loads|subprocess|os\.system"

RetryCallback = Callable[[int, float, Exception], None]


@dataclass
class LLMResponse:
    """一次模型调用的结果（含可观测性信息）。"""

    content: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 1
    raw: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


class BaseLLM(ABC):
    """LLM 客户端接口。"""

    name = "base"

    @abstractmethod
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """给定对话历史，返回模型的一次回复。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# OpenAI 兼容实现
# --------------------------------------------------------------------------- #
class OpenAICompatLLM(BaseLLM):
    """最小可用的 OpenAI 兼容客户端（仅依赖标准库 ``urllib``）。"""

    name = "openai-compatible"

    def __init__(self, config: Config) -> None:
        if not config.api_key:
            raise LLMResponseError("缺少 api_key，无法使用在线模式（请设置 CR_API_KEY 或改用 --mock）")
        self.config = config

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
                "User-Agent": "code-review-agent/1.0",
            },
        )

        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # 4xx / 5xx
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # pragma: no cover - 读取错误流失败时忽略
                pass
            retryable = exc.code in (408, 409, 425, 429) or exc.code >= 500
            hint = "（API Key 或模型名可能有误）" if exc.code in (401, 403, 404) else ""
            raise LLMRequestError(
                f"HTTP {exc.code} {exc.reason}{hint}：{detail}",
                status=exc.code,
                retryable=retryable,
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMRequestError(f"网络不可达：{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise LLMRequestError(f"请求超时（{self.config.timeout}s）", retryable=True) from exc
        except OSError as exc:  # 极少数情况下的底层 socket 错误
            raise LLMRequestError(f"底层网络错误：{exc}", retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse_response(text, latency_ms)

    @staticmethod
    def _parse_response(text: str, latency_ms: int) -> LLMResponse:
        """解析响应体，对各家实现的细微差异做兼容。"""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"响应不是合法 JSON：{text[:300]}") from exc

        if isinstance(data, dict) and data.get("error"):
            message = data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"])
            raise LLMResponseError(f"服务端返回错误：{message}")

        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(f"响应缺少 choices 字段：{text[:300]}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # 部分兼容层返回 [{type: text, text: ...}]
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if reasoning:
                raise LLMResponseError(
                    "模型只返回了推理内容（reasoning_content）而没有正文，请更换为对话型模型"
                )
            raise LLMResponseError("模型返回了空内容")

        return LLMResponse(
            content=content.strip(),
            model=data.get("model", ""),
            usage=data.get("usage") or {},
            latency_ms=latency_ms,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# 离线 Mock 实现
# --------------------------------------------------------------------------- #
class MockLLM(BaseLLM):
    """离线规则版 LLM：不联网，用确定性策略驱动同一套 Agent 协议。

    它不是「假装有模型」，而是一个**可解释的基线**：

    * 从 ``<TASK_CONTEXT>`` 读出审查目标，并**根据上一步的观察动态规划**下一步
      （先 ``list_files`` 侦察，再从结果里挑出体量最大的 Python 文件逐个分析）；
    * 复用真实工具（``analyze_code`` 的 AST 结果）作为结论依据，不编造问题；
    * 输出与真实模型完全相同的 JSON 协议，因此 Agent 主循环无需任何特判。

    用途：无 API Key 的课堂演示、CI 回归测试、以及对比「真实模型 vs 规则基线」。
    """

    name = "mock"

    #: 最多深入分析多少个源码文件（控制演示耗时与上下文长度）
    max_files_analyzed = 3

    def __init__(self, config: Config | None = None) -> None:
        self.config = config

    # -------------------------------------------------------------- #
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        executed = collect_executed_actions(messages)
        step = self._next_step(messages, executed)

        if step is None:
            context = extract_task_context(messages)
            payload: dict[str, Any] = {
                "thought": "已收集到足够的静态证据，收敛并输出最终结论",
                "final_answer": self._finalize(messages, context),
            }
        else:
            tool, arguments, thought = step
            payload = {"thought": thought, "action": tool, "action_input": arguments}

        content = json.dumps(payload, ensure_ascii=False)
        prompt_text = "".join(str(m.get("content", "")) for m in messages)
        return LLMResponse(
            content=content,
            model="mock-rule-based-v1",
            usage={
                "prompt_tokens": estimate_tokens(prompt_text),
                "completion_tokens": estimate_tokens(content),
                "total_tokens": estimate_tokens(prompt_text) + estimate_tokens(content),
            },
            latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )

    # -------------------------------------------------------------- #
    def _next_step(
        self,
        messages: Sequence[dict[str, Any]],
        executed: Sequence[tuple[str, dict[str, Any]]],
    ) -> tuple[str, dict[str, Any], str] | None:
        """基于「已执行的动作 + 已有观察」决定下一步；返回 ``None`` 表示可以收敛。

        这正是 ReAct 循环里「用观察修正计划」的最小实现：计划不是一次性写死的，
        而是每轮根据 ``list_files`` 的真实结果重新推导。

        四个阶段各自独立成方法（侦察 → 分析 → 精读 → 搜索），
        顺序即优先级——这是本工具对自审查报告中「圈复杂度过高」意见的落实。
        """
        context = extract_task_context(messages)
        targets = [str(t) for t in (context.get("targets") or ["."])] or ["."]
        scope = _scope_dir(targets)
        names = [name for name, _ in executed]

        # 阶段 1：侦察目录结构
        if "list_files" not in names:
            return ("list_files", {"path": scope, "pattern": "*"}, "先了解目录结构，确定审查范围")

        candidates = self._candidate_files(messages, targets)
        analyzed = [str(args.get("path", "")) for name, args in executed if name == "analyze_code"]

        for step in (
            self._plan_analysis(scope, candidates, analyzed),   # 阶段 2
            self._plan_reading(candidates, executed),           # 阶段 3
            self._plan_search(scope, targets, names),           # 阶段 4
        ):
            if step is not None:
                return step
        return None

    @staticmethod
    def _candidate_files(messages: Sequence[dict[str, Any]], targets: Sequence[str]) -> list[str]:
        """决定要分析哪些文件。

        用户**明确点名**的文件优先；只有目标本身是目录时，才去分析目录里发现的文件。
        「让你看这个文件」不应该扩大到它的兄弟文件——这是一条重要的行为边界。
        """
        ranked = _discover_python_files(messages)
        sizes = dict(ranked)
        explicit = [target for target in targets if target.lower().endswith((".py", ".pyw"))]
        if explicit:
            return sorted(explicit, key=lambda path: (-sizes.get(path, 0), path))
        return [path for path, _ in ranked]

    def _plan_analysis(
        self,
        scope: str,
        candidates: Sequence[str],
        analyzed: Sequence[str],
    ) -> tuple[str, dict[str, Any], str] | None:
        """阶段 2：逐个做 AST 静态分析（体量大的优先）。"""
        pending = [path for path in candidates if path not in analyzed]
        if pending and len(analyzed) < self.max_files_analyzed:
            path = pending[0]
            return ("analyze_code", {"path": path}, f"对 {path} 做 AST 静态分析，拿到确定性证据")
        if not candidates and not analyzed:
            # 一个 .py 都没发现时，改成对目标本身分析一次（工具会解释原因）
            return ("analyze_code", {"path": scope}, f"{scope} 下没有直接的 .py 文件，先整体分析一次")
        return None

    @staticmethod
    def _plan_reading(
        candidates: Sequence[str],
        executed: Sequence[tuple[str, dict[str, Any]]],
    ) -> tuple[str, dict[str, Any], str] | None:
        """阶段 3：精读最关键的那个文件，核对静态分析的上下文。"""
        if not candidates:
            return None
        read_paths = [str(args.get("path", "")) for name, args in executed if name == "read_file"]
        if candidates[0] in read_paths:
            return None
        return ("read_file", {"path": candidates[0]}, f"精读 {candidates[0]}，核对静态分析的上下文")

    @staticmethod
    def _plan_search(
        scope: str,
        targets: Sequence[str],
        executed_names: Sequence[str],
    ) -> tuple[str, dict[str, Any], str] | None:
        """阶段 4：搜索高危写法，防止纯 AST 规则遗漏。"""
        if "search_code" in executed_names:
            return None
        explicit = [target for target in targets if target.lower().endswith((".py", ".pyw"))]
        search_path = explicit[0] if len(explicit) == 1 else scope
        return ("search_code", {"pattern": RISKY_PATTERN, "path": search_path}, "搜索高危写法，避免遗漏")

    def _finalize(self, messages: Sequence[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
        """把工具观察聚合成最终报告结构。

        拆成三个纯函数（收集 AST 结论 / 补充搜索命中 / 汇总评分），
        既便于单测，也避免出现一个「什么都干」的巨型函数——
        这一点正是本工具在自审查 ``docs/self-review.md`` 里给出的建议。
        """
        issues, covered_lines, files_analyzed = _collect_static_findings(messages)
        issues.extend(_collect_search_issues(messages, covered_lines, _issue_keys(issues)))

        counts = _severity_counts(issues)
        score = _score_from_counts(counts)

        # 文件数取自**不受压缩影响**的进度台账，而不是可能已被折叠的观察，
        # 否则概述里报出的文件数会随上下文压缩而变小。
        ledger = collect_executed_actions(messages)
        analyzed_files = {str(args.get("path", "")) for name, args in ledger if name == "analyze_code"}
        analyzed_files |= set(files_analyzed)
        file_count = len({path for path in analyzed_files if path}) or len(context.get("targets") or [])

        return {
            "summary": _build_summary(file_count, counts, score),
            "score": score,
            "issues": issues,
            "highlights": _build_highlights(counts),
            "next_steps": _build_next_steps(counts),
        }


# --------------------------------------------------------------------------- #
# 重试包装
# --------------------------------------------------------------------------- #
class RetryingLLM(BaseLLM):
    """给任意 ``BaseLLM`` 加上指数退避重试。"""

    def __init__(
        self,
        inner: BaseLLM,
        *,
        max_retries: int = 3,
        backoff_base: float = 1.5,
        sleeper: Callable[[float], None] = time.sleep,
        on_retry: RetryCallback | None = None,
        jitter: float = 0.3,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleeper
        self._on_retry = on_retry
        self._jitter = jitter
        self.name = inner.name

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.inner.chat(messages, temperature=temperature, max_tokens=max_tokens)
                response.attempts = attempt
                return response
            except LLMRequestError as exc:
                if not exc.retryable or attempt > self.max_retries:
                    raise
                delay = self.backoff_base ** (attempt - 1) + random.uniform(0, self._jitter)
                if self._on_retry:
                    self._on_retry(attempt, delay, exc)
                self._sleep(delay)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_llm(config: Config, on_retry: RetryCallback | None = None) -> BaseLLM:
    """按配置造出可用的 LLM 客户端（自动降级到 Mock）。"""
    inner: BaseLLM = MockLLM(config) if config.effective_mock else OpenAICompatLLM(config)
    return RetryingLLM(
        inner,
        max_retries=config.max_retries,
        backoff_base=config.backoff_base,
        on_retry=on_retry,
    )


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def collect_executed_actions(messages: Sequence[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """还原「真正执行过」的 ``(工具名, 参数)`` 列表。

    只扫描两类消息，避免把观察或纠正提示里出现的 JSON 误当成已执行动作：

    * ``assistant`` 消息 —— 模型刚刚发出的动作；
    * 记忆台账（以 ``[进度]`` 开头）—— 由 Agent 的 trace 生成、不受上下文压缩影响。
    """
    actions: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        role = message.get("role")
        content = str(message.get("content", ""))
        if role != "assistant" and not content.lstrip().startswith(LEDGER_PREFIX):
            continue
        for payload in extract_json_objects(content):
            if not isinstance(payload, dict):
                continue
            name = payload.get("action")
            if not isinstance(name, str) or not name:
                continue
            arguments = payload.get("action_input")
            arguments = arguments if isinstance(arguments, dict) else {}
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
            actions.append((name, arguments))
    return actions


#: 匹配 list_files 输出里的一行文件：``  path/to/file.py  [123 B]``
_LISTED_FILE_RE = re.compile(r"^\s*(?P<path>\S+\.py)\s*(?:\[\s*(?P<size>\d+)\s*B\s*\])?\s*$")


def _discover_python_files(messages: Sequence[dict[str, Any]], limit: int = 8) -> list[tuple[str, int]]:
    """从 ``list_files`` 的观察里解析出 ``(Python 文件, 字节数)``，按体积从大到小排序。

    「先看大文件」是有意的启发式：代码量大的文件通常集中了更多问题，
    在有限的步数预算下收益最高。这也是给真实模型的示范性策略。
    """
    found: dict[str, int] = {}
    for message in messages:
        content = str(message.get("content", ""))
        if "list_files" not in content:
            continue
        for line in content.splitlines():
            match = _LISTED_FILE_RE.match(line)
            if not match:
                continue
            path = match.group("path").strip()
            size = int(match.group("size") or 0)
            found[path] = max(found.get(path, 0), size)
    return sorted(found.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _iter_json_payloads(messages: Sequence[dict[str, Any]]):
    """从工具观察里提取 JSON 负载（容错：允许前后有说明文字）。"""
    for message in messages:
        if message.get("role") != "user":
            continue
        yield from extract_json_objects(str(message.get("content", "")))


def _search_hits(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 ``search_code`` 的观察文本，提取真正的代码命中。

    两道过滤，避免「文档里举例出现的 eval(」这类噪音：

    1. 用 :func:`mask_strings_and_comments` 屏蔽字符串与注释后再匹配关键字；
    2. 展示时仍使用原始行文本，保证证据可回溯。
    """
    hits: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if "search_code" not in content:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("Observation", "-", "共", "```", "{")):
                continue
            parts = line.split(":", 2)
            if len(parts) != 3 or not parts[1].strip().isdigit():
                continue
            text = parts[2].strip()
            keyword = _match_keyword(mask_strings_and_comments(text))
            if not keyword:
                continue  # 关键字只出现在字符串或注释里，不算真命中
            hits.append(
                {
                    "file": parts[0].strip(),
                    "line": int(parts[1]),
                    "text": text,
                    "keyword": keyword,
                }
            )
    return hits[:40]


def _match_keyword(masked_line: str) -> str:
    """在「已屏蔽字符串与注释」的代码骨架上查找关键字；找不到时返回空串。"""
    for keyword in ("eval(", "exec(", "os.system", "pickle.loads", "subprocess",
                    "except Exception", "except:", "== None", "TODO", "FIXME"):
        if keyword in masked_line:
            return keyword
    return ""


# --------------------------------------------------------------------------- #
# Mock 的结论聚合（拆成小函数，便于单测与复用）
# --------------------------------------------------------------------------- #
def _issue_keys(issues: Sequence[dict[str, Any]]) -> set[tuple[str, int, str]]:
    return {(str(i.get("file", "")), int(i.get("line") or 0), str(i.get("title", ""))) for i in issues}


def _collect_static_findings(
    messages: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[tuple[str, int]], list[str]]:
    """从 ``analyze_code`` 的观察中提取问题清单。"""
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    covered_lines: set[tuple[str, int]] = set()
    files_analyzed: list[str] = []

    for payload in _iter_json_payloads(messages):
        if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
            continue
        path = str(payload.get("path", ""))
        files_analyzed.append(path)
        for finding in payload["findings"]:
            if not isinstance(finding, dict):
                continue
            line = int(finding.get("line") or 0)
            key = (path, line, str(finding.get("title", "")))
            if key in seen:
                continue
            seen.add(key)
            covered_lines.add((path, line))
            issues.append(
                {
                    "file": path,
                    "line": line,
                    "severity": finding.get("severity", "minor"),
                    "category": finding.get("category", "maintainability"),
                    "title": finding.get("title", "代码问题"),
                    "detail": finding.get("detail", ""),
                    "suggestion": finding.get("suggestion", ""),
                    "rule": finding.get("rule", ""),
                }
            )
    return issues, covered_lines, files_analyzed


def _collect_search_issues(
    messages: Sequence[dict[str, Any]],
    covered_lines: set[tuple[str, int]],
    seen: set[tuple[str, int, str]],
) -> list[dict[str, Any]]:
    """把搜索命中补成 info 级问题；已被 AST 规则覆盖的行不再重复报。"""
    issues: list[dict[str, Any]] = []
    for hit in _search_hits(messages):
        if (hit["file"], hit["line"]) in covered_lines:
            continue
        title = f"高风险关键字：{hit['keyword']}"
        key = (hit["file"], hit["line"], title)
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            {
                "file": hit["file"],
                "line": hit["line"],
                "severity": "info",
                "category": "security" if hit["keyword"] in ("eval(", "exec(", "os.system", "pickle.loads") else "maintainability",
                "title": title,
                "detail": f"搜索到 `{hit['keyword']}`：{hit['text']}。请人工确认这里是否安全。",
                "suggestion": "人工复核该处调用：确认输入可信，必要时替换为安全替代方案并补充校验。",
                "rule": "keyword-search",
            }
        )
    return issues


def _severity_counts(issues: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for issue in issues:
        level = str(issue.get("severity", "minor"))
        counts[level] = counts.get(level, 0) + 1
    return counts


def _score_from_counts(counts: dict[str, int]) -> int:
    """按严重度加权扣分，并用指数衰减映射到 0-100。

    为什么不用 ``100 - Σ权重``：那样只要问题稍多就会**撞到 0 分**，
    分数失去区分度（一份 5 个 major 的代码和一份 30 个 major 的代码都是 0 分）。
    指数衰减让分数平滑趋近 0，同时保留「很差 / 极差」之间的差别。
    """
    penalty = 18 * counts["critical"] + 8 * counts["major"] + 3 * counts["minor"] + counts["info"]
    return int(round(100 * math.exp(-penalty / 60.0)))


def _build_summary(file_count: int, counts: dict[str, int], score: int) -> str:
    return (
        f"本次共完成 {file_count} 个文件的静态审查，"
        f"发现 critical {counts['critical']} 项、major {counts['major']} 项、minor {counts['minor']} 项、"
        f"info {counts['info']} 项。整体评分 {score}/100。"
        "（离线 Mock 基线：结论完全来自 AST 静态分析与正则搜索，未经过大模型润色。）"
    )


def _build_highlights(counts: dict[str, int]) -> list[str]:
    highlights = []
    if not counts["critical"]:
        highlights.append("未发现会导致崩溃或安全漏洞的严重问题，代码可以正常编译与导入。")
    highlights.append("项目结构可被工具链完整解析，便于接入 CI 做持续审查。")
    return highlights


def _build_next_steps(counts: dict[str, int]) -> list[str]:
    steps = ["优先修复 critical / major 级别问题，并为每个修复补充回归测试。"]
    if counts["critical"] or counts["major"]:
        steps.append("对安全类问题（如动态执行、命令拼接、硬编码凭据）做一次专项复查。")
    steps.append("在 CI 中加入 `analyze_code` 与语法检查，防止问题回流。")
    return steps


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


def _scope_dir(targets: Sequence[str]) -> str:
    """推断扫描范围：目标是文件时取其所在目录。"""
    first = targets[0] if targets else "."
    if "." in _basename(first) and not first.endswith(("/", "\\")):
        directory = first.replace("\\", "/").rsplit("/", 1)[0] if "/" in first.replace("\\", "/") else "."
        return directory or "."
    return first


__all__ = [
    "BaseLLM",
    "LLMResponse",
    "OpenAICompatLLM",
    "MockLLM",
    "RetryingLLM",
    "create_llm",
    "collect_executed_actions",
    "RISKY_PATTERN",
]
