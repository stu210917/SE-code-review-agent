# -*- coding: utf-8 -*-
"""LLM 层测试：离线 Mock 的端到端行为、重试策略、响应解析容错。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codereview.agent import ReviewAgent
from codereview.config import Config
from codereview.errors import LLMRequestError, LLMResponseError
from codereview.llm import (
    BaseLLM,
    LLMResponse,
    MockLLM,
    OpenAICompatLLM,
    RetryingLLM,
    _score_from_counts,
    create_llm,
)
from codereview.tools import build_default_registry

BUGGY = '''\
"""待审查模块。"""

import json
import os

TOKEN = "sk-hardcoded-123456"


def merge(target={}, source=None):
    """合并字典。"""
    try:
        target.update(source)
    except:
        pass
    if source == None:
        return target
    return eval("target")
'''


class ScriptedLLM(BaseLLM):
    name = "scripted"

    def __init__(self, failures: int = 0, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    def chat(self, messages, *, temperature=None, max_tokens=None) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMRequestError(f"第 {self.calls} 次失败", retryable=self.retryable)
        return LLMResponse(content='{"final_answer": {}}', model="scripted")


class RetryTests(unittest.TestCase):
    def test_retryable_error_is_retried_until_success(self) -> None:
        delays: list[float] = []
        llm = RetryingLLM(ScriptedLLM(failures=2), max_retries=3, sleeper=delays.append, jitter=0)
        response = llm.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.attempts, 3)
        self.assertEqual(len(delays), 2)
        self.assertEqual(delays[0], 1.0)          # backoff_base ** 0
        self.assertAlmostEqual(delays[1], 1.5)    # backoff_base ** 1

    def test_non_retryable_error_fails_fast(self) -> None:
        delays: list[float] = []
        llm = RetryingLLM(ScriptedLLM(failures=5, retryable=False), max_retries=3,
                          sleeper=delays.append, jitter=0)
        with self.assertRaises(LLMRequestError):
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(delays, [])

    def test_gives_up_after_max_retries(self) -> None:
        delays: list[float] = []
        llm = RetryingLLM(ScriptedLLM(failures=99), max_retries=2, sleeper=delays.append, jitter=0)
        with self.assertRaises(LLMRequestError):
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(delays), 2)

    def test_retry_callback_is_invoked(self) -> None:
        events: list[tuple[int, float, str]] = []
        llm = RetryingLLM(
            ScriptedLLM(failures=1), max_retries=2, sleeper=lambda _: None, jitter=0,
            on_retry=lambda attempt, delay, exc: events.append((attempt, delay, str(exc))),
        )
        llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 1)


class FactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_api_key_falls_back_to_mock(self) -> None:
        llm = create_llm(Config(workspace=self.root, api_key=""))
        self.assertEqual(llm.name, "mock")
        self.assertIsInstance(llm.inner, MockLLM)

    def test_api_key_selects_openai_compatible(self) -> None:
        llm = create_llm(Config(workspace=self.root, api_key="sk-test"))
        self.assertIsInstance(llm.inner, OpenAICompatLLM)

    def test_missing_key_raises_when_constructing_online_client(self) -> None:
        with self.assertRaises(LLMResponseError):
            OpenAICompatLLM(Config(workspace=self.root, api_key=""))


class ResponseParsingTests(unittest.TestCase):
    """各家 OpenAI 兼容服务的响应格式略有差异，解析层必须容错。"""

    def test_standard_response(self) -> None:
        response = OpenAICompatLLM._parse_response(
            json.dumps({"model": "gpt-4o-mini", "choices": [{"message": {"content": " 你好 "}}],
                        "usage": {"total_tokens": 12}}),
            42,
        )
        self.assertEqual(response.content, "你好")
        self.assertEqual(response.total_tokens, 12)
        self.assertEqual(response.latency_ms, 42)

    def test_content_as_parts_list(self) -> None:
        response = OpenAICompatLLM._parse_response(
            json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]}), 1
        )
        self.assertEqual(response.content, "ab")

    def test_error_payload_is_raised(self) -> None:
        with self.assertRaises(LLMResponseError):
            OpenAICompatLLM._parse_response(json.dumps({"error": {"message": "余额不足"}}), 1)

    def test_missing_choices_is_raised(self) -> None:
        with self.assertRaises(LLMResponseError):
            OpenAICompatLLM._parse_response(json.dumps({"ok": True}), 1)

    def test_empty_content_is_raised(self) -> None:
        with self.assertRaises(LLMResponseError):
            OpenAICompatLLM._parse_response(json.dumps({"choices": [{"message": {"content": ""}}]}), 1)

    def test_reasoning_only_response_gives_actionable_hint(self) -> None:
        with self.assertRaises(LLMResponseError) as ctx:
            OpenAICompatLLM._parse_response(
                json.dumps({"choices": [{"message": {"content": None, "reasoning_content": "思考中…"}}]}), 1
            )
        self.assertIn("推理内容", str(ctx.exception))

    def test_invalid_json_is_raised(self) -> None:
        with self.assertRaises(LLMResponseError):
            OpenAICompatLLM._parse_response("not json at all", 1)


class ScoringTests(unittest.TestCase):
    """评分函数必须单调、有界，并且在问题很多时仍保留区分度。"""

    @staticmethod
    def counts(critical=0, major=0, minor=0, info=0) -> dict[str, int]:
        return {"critical": critical, "major": major, "minor": minor, "info": info}

    def test_perfect_score_for_clean_code(self) -> None:
        self.assertEqual(_score_from_counts(self.counts()), 100)

    def test_score_is_bounded(self) -> None:
        for counts in (self.counts(critical=50), self.counts(major=99), self.counts(info=1000)):
            score = _score_from_counts(counts)
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_score_decreases_with_severity(self) -> None:
        self.assertGreater(
            _score_from_counts(self.counts(minor=1)),
            _score_from_counts(self.counts(major=1)),
        )
        self.assertGreater(
            _score_from_counts(self.counts(major=1)),
            _score_from_counts(self.counts(critical=1)),
        )

    def test_very_bad_code_still_keeps_resolution(self) -> None:
        """指数衰减的意义：10 个 major 和 30 个 major 不应都是 0 分。"""
        moderate = _score_from_counts(self.counts(major=10))
        terrible = _score_from_counts(self.counts(major=30))
        self.assertGreater(moderate, 0)
        self.assertGreater(moderate, terrible)


class MockLLMEndToEndTests(unittest.TestCase):
    """离线 Mock 模式的完整链路：Agent + 工具 + 报告，全程不联网。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app.py").write_text(BUGGY, encoding="utf-8")
        self.config = Config(workspace=self.root, mock=True, max_steps=12)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mock_drives_a_complete_review(self) -> None:
        agent = ReviewAgent(self.config, registry=build_default_registry(self.config))
        result = agent.review(["app.py"])

        self.assertFalse(result.error, result.error)
        self.assertFalse(result.report.degraded, result.report.degraded_reason)
        rules = {issue.rule for issue in result.report.issues}
        self.assertIn("hardcoded-secret", rules)
        self.assertIn("bare-except", rules)
        self.assertIn("mutable-default", rules)
        self.assertGreater(result.tool_calls, 0)

    def test_mock_follows_the_reconnaissance_plan(self) -> None:
        agent = ReviewAgent(self.config)
        result = agent.review(["app.py"])
        actions = [step["action"] for step in result.trace]
        # 先侦察、再分析、后搜索 —— 顺序体现「先看结构再下结论」的提示词约束
        self.assertEqual(actions[0], "list_files")
        self.assertIn("analyze_code", actions)
        self.assertIn("search_code", actions)

    def test_mock_reports_no_issues_on_clean_code(self) -> None:
        """干净代码必须被判定为「没问题」——单独放一个目录，避免受同目录问题文件影响。"""
        clean = '''\
"""干净模块。"""


def add(left: int, right: int) -> int:
    """返回两数之和。"""
    return left + right
'''
        clean_dir = self.root / "clean_project"
        clean_dir.mkdir()
        (clean_dir / "clean.py").write_text(clean, encoding="utf-8")

        agent = ReviewAgent(self.config)
        result = agent.review(["clean_project"])
        serious = [i for i in result.report.issues if i.severity in ("critical", "major")]
        self.assertEqual(serious, [], f"干净文件被误报：{[(i.rule, i.title) for i in serious]}")
        self.assertEqual(result.report.counts["critical"], 0)

    def test_targeting_a_file_does_not_review_its_siblings(self) -> None:
        """点名审查一个文件时，不应把同目录的兄弟文件也算进结论。"""
        good_dir = self.root / "mixed"
        good_dir.mkdir()
        (good_dir / "good.py").write_text('"""好文件。"""\n\n\ndef ok():\n    """没事。"""\n    return 1\n',
                                          encoding="utf-8")
        (good_dir / "terrible.py").write_text(BUGGY, encoding="utf-8")

        result = ReviewAgent(self.config).review(["mixed/good.py"])
        analysed = {issue.file for issue in result.report.issues}
        self.assertNotIn("mixed/terrible.py", analysed)

    def test_mock_output_is_valid_protocol_json(self) -> None:
        """Mock 也必须遵守与真实模型相同的 JSON 协议。"""
        llm = MockLLM(self.config)
        response = llm.chat([{"role": "user", "content": '<TASK_CONTEXT>{"targets": ["app.py"]}</TASK_CONTEXT>'}])
        payload = json.loads(response.content)
        self.assertIn("action", payload)
        self.assertIn("action_input", payload)

    def test_agent_does_not_restart_plan_after_compression(self) -> None:
        """回归测试：上下文被压缩后，Agent 不能把已经做过的侦察重跑一遍。

        这是把 ``memory_token_budget`` 调小就能稳定复现的真实缺陷。
        """
        config = Config(workspace=self.root, mock=True, max_steps=14,
                        memory_token_budget=700, max_observation_chars=900)
        result = ReviewAgent(config).review(["app.py"])

        actions = [step["action"] for step in result.trace]
        self.assertEqual(actions.count("list_files"), 1, f"侦察动作被重复执行：{actions}")
        self.assertEqual(len(actions), len(set(actions)), f"存在重复调用：{actions}")
        self.assertFalse(result.report.degraded, result.report.degraded_reason)

    def test_static_findings_survive_even_if_compressed_away(self) -> None:
        """静态分析证据在工具返回时就已入库，不因上下文压缩而丢失。"""
        config = Config(workspace=self.root, mock=True, max_steps=14,
                        memory_token_budget=600, max_observation_chars=700)
        result = ReviewAgent(config).review(["app.py"])
        rules = {issue.rule for issue in result.report.issues}
        ast_rules = rules - {"keyword-search", ""}
        self.assertTrue(ast_rules, f"压缩后仍应保留 AST 分析结论，实际只有：{rules}")


if __name__ == "__main__":
    unittest.main()
