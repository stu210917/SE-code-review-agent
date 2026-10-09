# -*- coding: utf-8 -*-
"""Agent 主循环测试。

用 ``ScriptedLLM`` 按剧本喂给 Agent 预设回复，从而在不联网的前提下
精确验证四类行为：正常收敛、协议自纠、死循环熔断、异常降级。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from codereview.agent import ReviewAgent, parse_action
from codereview.config import Config
from codereview.errors import ActionParseError
from codereview.llm import BaseLLM, LLMResponse

MODULE_SOURCE = '''\
"""演示模块。"""


def collect(items=[], flag=True):
    """有可变默认参数的函数。"""
    items.append(flag)
    return items
'''


class ScriptedLLM(BaseLLM):
    """按剧本返回预设内容的假 LLM（记录调用次数，便于断言）。"""

    name = "scripted"

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None) -> LLMResponse:
        self.seen_messages.append([dict(m) for m in messages])
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResponse(content=reply, model="scripted", usage={"total_tokens": 42})


def tool_call(name: str, **arguments: Any) -> str:
    return json.dumps({"thought": f"调用 {name}", "action": name, "action_input": arguments}, ensure_ascii=False)


def final_answer(**payload: Any) -> str:
    return json.dumps({"thought": "收敛", "final_answer": payload}, ensure_ascii=False)


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app.py").write_text(MODULE_SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_config(self, **overrides: Any) -> Config:
        base: dict[str, Any] = {"workspace": self.root, "mock": True, "max_steps": 6,
                                "memory_token_budget": 2000, "max_observation_chars": 4000}
        base.update(overrides)
        return Config(**base)

    def make_agent(self, replies: Sequence[str], **overrides: Any):
        config = self.make_config(**overrides)
        llm = ScriptedLLM(replies)
        return ReviewAgent(config, llm=llm), llm


class NormalFlowTests(AgentTestCase):
    def test_tool_then_final(self) -> None:
        agent, llm = self.make_agent([
            tool_call("analyze_code", path="app.py"),
            final_answer(
                summary="整体尚可，但存在可变默认参数。",
                score=78,
                issues=[{
                    "file": "app.py", "line": 4, "severity": "major", "category": "bug",
                    "title": "可变默认参数", "detail": "多次调用会共享同一个 list。",
                    "suggestion": "改为 items=None 再在函数体内初始化。",
                }],
                highlights=["函数有文档字符串"],
                next_steps=["补充单元测试"],
            ),
        ])

        result = agent.review(["app.py"])

        self.assertEqual(llm.calls, 2)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.report.score, 78)
        self.assertEqual(len(result.report.issues), 1)
        self.assertEqual(result.report.issues[0].severity, "major")
        self.assertIn("app.py", result.report.issues[0].location)
        # 轨迹要被记录下来，供报告附录使用
        self.assertEqual(result.trace[0]["action"], "analyze_code")
        self.assertTrue(result.trace[0]["ok"])

    def test_final_answer_without_issues_falls_back_to_static_findings(self) -> None:
        """模型说「没问题」但静态分析发现了问题：不能让真问题被漏掉。"""
        agent, _ = self.make_agent([
            tool_call("analyze_code", path="app.py"),
            final_answer(summary="看起来不错。", score=95, issues=[]),
        ])
        result = agent.review(["app.py"])
        rules = {issue.rule for issue in result.report.issues}
        self.assertIn("mutable-default", rules)

    def test_markdown_report_renders(self) -> None:
        agent, _ = self.make_agent([
            tool_call("read_file", path="app.py"),
            final_answer(summary="通过", score=90, issues=[]),
        ])
        markdown = agent.review(["app.py"]).report.to_markdown()
        self.assertIn("# 代码审查报告", markdown)
        self.assertIn("附录：执行轨迹", markdown)
        self.assertIn("read_file", markdown)


class ProtocolRecoveryTests(AgentTestCase):
    def test_recovers_from_non_json_reply(self) -> None:
        agent, llm = self.make_agent([
            "好的，我先看看这个文件。（这里本该是 JSON，但模型忘了）",
            tool_call("read_file", path="app.py"),
            final_answer(summary="完成", score=88, issues=[]),
        ])
        result = agent.review(["app.py"])

        self.assertEqual(result.steps, 3)
        self.assertTrue(result.ok)
        # 纠正提示必须被写回给模型，让它有机会自我修复
        correction = json.dumps(llm.seen_messages[1], ensure_ascii=False)
        self.assertIn("不符合协议", correction)

    def test_too_many_bad_replies_degrades_but_still_returns_report(self) -> None:
        agent, _ = self.make_agent(["不是 JSON"] * 5)
        result = agent.review(["app.py"])
        self.assertTrue(result.report.degraded)
        self.assertIn("不合协议", result.report.degraded_reason)

    def test_accepts_code_fenced_and_noisy_output(self) -> None:
        fenced = "```json\n" + tool_call("list_files", path=".") + "\n```"
        noisy = "我的计划如下：\n" + tool_call("read_file", path="app.py") + "\n请稍等。"
        agent, _ = self.make_agent([fenced, noisy, final_answer(summary="ok", score=70, issues=[])])
        result = agent.review(["app.py"])
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual([step["action"] for step in result.trace], ["list_files", "read_file"])


class LoopAndLimitTests(AgentTestCase):
    def test_repeated_identical_call_is_broken(self) -> None:
        agent, llm = self.make_agent([tool_call("list_files", path=".")] * 10, max_steps=10)
        result = agent.review(["."])

        self.assertTrue(result.report.degraded)
        self.assertIn("死循环", result.report.degraded_reason)
        # 熔断后不该无限调用：限制在「重复上限 + 提示 + 熔断」几步之内
        self.assertLess(llm.calls, 6)

    def test_max_steps_produces_degraded_report_with_salvaged_issues(self) -> None:
        agent, _ = self.make_agent(
            [tool_call("analyze_code", path="app.py")] * 10,
            max_steps=1,
            repeat_action_limit=10,
        )
        result = agent.review(["app.py"])

        self.assertTrue(result.report.degraded)
        self.assertIn("最大推理步数", result.report.degraded_reason)
        self.assertTrue(result.report.issues, "降级报告也应带上已获得的静态分析结论")
        self.assertEqual(result.report.issues[0].rule, "mutable-default")
        self.assertEqual(result.report.score, 0)
        self.assertIn("降级", result.report.to_markdown())


class ToolFailureTests(AgentTestCase):
    def test_tool_error_becomes_observation_not_crash(self) -> None:
        agent, _ = self.make_agent([
            tool_call("read_file", path="does_not_exist.py"),
            final_answer(summary="文件不存在，无法继续。", score=50, issues=[]),
        ])
        result = agent.review(["."])

        self.assertEqual(result.tool_calls, 1)
        self.assertFalse(result.trace[0]["ok"])
        self.assertIn("失败", result.trace[0]["result"])
        self.assertTrue(result.report.summary)

    def test_unknown_tool_name_is_tolerated(self) -> None:
        agent, _ = self.make_agent([
            tool_call("magic_tool", foo="bar"),
            final_answer(summary="工具不可用，改用已有信息。", score=60, issues=[]),
        ])
        result = agent.review(["."])
        self.assertFalse(result.trace[0]["ok"])
        self.assertTrue(result.report.summary)


class LLMFailureTests(AgentTestCase):
    def test_llm_failure_is_reported_without_losing_static_findings(self) -> None:
        class BrokenLLM(BaseLLM):
            name = "broken"

            def chat(self, messages, *, temperature=None, max_tokens=None):
                from codereview.errors import LLMRequestError

                raise LLMRequestError("网络不可达", retryable=False)

        config = self.make_config()
        agent = ReviewAgent(config, llm=BrokenLLM())
        result = agent.review(["app.py"])

        self.assertTrue(result.error)
        self.assertIn("网络不可达", result.error)
        self.assertTrue(result.report.degraded)


class ParseActionTests(unittest.TestCase):
    """协议解析的边界情况。"""

    def test_plain_tool_call(self) -> None:
        action = parse_action('{"thought": "t", "action": "read_file", "action_input": {"path": "a.py"}}')
        self.assertEqual(action.kind, "tool")
        self.assertEqual(action.tool, "read_file")
        self.assertEqual(action.arguments, {"path": "a.py"})

    def test_final_answer(self) -> None:
        action = parse_action('{"thought": "t", "final_answer": {"score": 80, "issues": []}}')
        self.assertEqual(action.kind, "final")
        self.assertEqual(action.final["score"], 80)

    def test_final_answer_via_action_alias(self) -> None:
        action = parse_action('{"action": "final_answer", "action_input": {"score": 61}}')
        self.assertEqual(action.kind, "final")
        self.assertEqual(action.final["score"], 61)

    def test_tolerates_trailing_comma(self) -> None:
        action = parse_action('{"action": "list_files", "action_input": {"path": ".",},}')
        self.assertEqual(action.tool, "list_files")

    def test_tolerates_surrounding_prose(self) -> None:
        action = parse_action('让我先看看结构：\n{"action": "list_files", "action_input": {}}\n完毕。')
        self.assertEqual(action.tool, "list_files")

    def test_defaults_missing_action_input(self) -> None:
        action = parse_action('{"action": "list_files"}')
        self.assertEqual(action.arguments, {})

    def test_empty_final_answer_is_kept(self) -> None:
        action = parse_action('{"final_answer": {}}')
        self.assertEqual(action.kind, "final")
        self.assertEqual(action.final, {})

    def test_invalid_output_raises(self) -> None:
        with self.assertRaises(ActionParseError):
            parse_action("完全不是 JSON")

    def test_action_input_must_be_object(self) -> None:
        with self.assertRaises(ActionParseError):
            parse_action('{"action": "read_file", "action_input": "app.py"}')


if __name__ == "__main__":
    unittest.main()
