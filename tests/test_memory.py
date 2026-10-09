# -*- coding: utf-8 -*-
"""记忆与上下文压缩测试。"""

from __future__ import annotations

import json
import unittest

from codereview.memory import LEDGER_PREFIX, Memory


class MemoryBasicsTests(unittest.TestCase):
    def test_system_and_task_are_placed_first(self) -> None:
        memory = Memory(2000)
        memory.set_system("你是审查员")
        memory.set_task("审查 app.py")
        memory.add_observation("read_file", "1| import os")

        messages = memory.build_messages()
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("审查 app.py", messages[1]["content"])
        self.assertIn("read_file", messages[-1]["content"])

    def test_observation_is_truncated_on_write(self) -> None:
        memory = Memory(2000, max_observation_chars=50)
        message = memory.add_observation("read_file", "x" * 500)
        self.assertIn("已截断", message.content)
        self.assertLess(len(message.content), 200)

    def test_error_observation_is_labelled(self) -> None:
        memory = Memory(2000)
        message = memory.add_observation("read_file", "路径不存在", ok=False)
        self.assertIn("[ERROR]", message.content)
        self.assertFalse(message.ok)

    def test_last_observation_and_signature(self) -> None:
        memory = Memory(2000)
        memory.add_observation("list_files", "a.py")
        memory.add_observation("read_file", "1| x")
        self.assertIn("1| x", memory.last_observation().content)
        self.assertIn("read_file", memory.signature())


class CompressionTests(unittest.TestCase):
    """上下文压缩是长期运行 Agent 的关键，必须可断言。"""

    def _fill(self, memory: Memory, count: int, size: int = 400) -> list[str]:
        contents = []
        for index in range(count):
            body = f"文件 {index}.py 的内容：" + ("y" * size)
            contents.append(body)
            memory.add_observation("read_file", body)
        return contents

    def test_no_compression_when_under_budget(self) -> None:
        memory = Memory(100_000)
        memory.set_system("sys")
        memory.set_task("task")
        self._fill(memory, 5)
        memory.build_messages()
        self.assertFalse(memory.last_stats.compressed)
        self.assertEqual(memory.last_stats.dropped, 0)

    def test_compression_happens_when_over_budget(self) -> None:
        memory = Memory(1200, keep_recent=2)
        memory.set_system("sys")
        memory.set_task("task")
        self._fill(memory, 20)
        messages = memory.build_messages()
        stats = memory.last_stats

        self.assertTrue(stats.compressed)
        self.assertGreater(stats.dropped, 0)
        self.assertLessEqual(stats.prompt_tokens, memory.token_budget + 400)
        # 摘要必须出现在提示里，且说明发生了什么
        self.assertTrue(any("上下文压缩" in m["content"] for m in messages))

    def test_most_recent_observations_are_kept_verbatim(self) -> None:
        memory = Memory(1200, keep_recent=2)
        memory.set_system("sys")
        memory.set_task("task")
        contents = self._fill(memory, 20)
        messages = memory.build_messages()
        joined = "\n".join(m["content"] for m in messages)

        # 最后两条应当原样保留（而不是被折叠）
        self.assertIn(contents[-1], joined)
        self.assertIn(contents[-2], joined)

    def test_full_history_is_never_lost(self) -> None:
        """压缩只影响「发给模型的视图」，完整历史必须保留下来供报告使用。"""
        memory = Memory(1000, keep_recent=2)
        memory.set_system("sys")
        memory.set_task("task")
        self._fill(memory, 15)
        memory.build_messages()
        self.assertEqual(len(memory), 15)
        self.assertEqual(len(list(memory.observations())), 15)

    def test_digest_mentions_tool_names(self) -> None:
        memory = Memory(1000, keep_recent=1)
        memory.set_system("s")
        memory.set_task("t")
        self._fill(memory, 15)
        messages = memory.build_messages()
        digest = next(m["content"] for m in messages if "上下文压缩" in m["content"])
        self.assertIn("[read_file]", digest)

    def test_rejects_tiny_budget(self) -> None:
        with self.assertRaises(ValueError):
            Memory(100)


class ProgressLedgerTests(unittest.TestCase):
    """回归测试：上下文压缩后，Agent 不能忘记自己已经做过什么。

    这是开发过程中真实踩到的坑——早期版本只从消息历史推断进度，
    压缩一旦折叠掉早期的 assistant 消息，Agent 就会把整个计划重跑一遍。
    """

    def _memory_with_trace(self) -> Memory:
        memory = Memory(1000, keep_recent=1)
        memory.set_system("sys")
        memory.set_task("task")
        memory.add_trace({
            "step": 1,
            "action": "list_files",
            "arguments": '{"path": ".", "pattern": "*"}',
            "result": "共 3 个文件",
            "ok": True,
        })
        memory.add_trace({
            "step": 2,
            "action": "analyze_code",
            "arguments": '{"path": "a.py"}',
            "result": "发现 2 处问题",
            "ok": True,
        })
        return memory

    def test_ledger_is_absent_without_trace(self) -> None:
        memory = Memory(2000)
        memory.set_system("s")
        memory.set_task("t")
        self.assertEqual(memory.render_ledger(), "")
        self.assertFalse(any(m["content"].startswith(LEDGER_PREFIX) for m in memory.build_messages()))

    def test_ledger_lists_every_executed_action(self) -> None:
        memory = self._memory_with_trace()
        ledger = memory.render_ledger()
        self.assertTrue(ledger.startswith(LEDGER_PREFIX))
        self.assertIn("list_files", ledger)
        self.assertIn("analyze_code", ledger)

    def test_ledger_lines_are_valid_json(self) -> None:
        """台账每一行都要是合法 JSON，程序才能据此还原「已执行动作」。"""
        memory = self._memory_with_trace()
        lines = [line for line in memory.render_ledger().splitlines() if line.startswith("{")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            payload = json.loads(line)
            self.assertIn("action", payload)
            self.assertIsInstance(payload["action_input"], dict)

    def test_ledger_survives_context_compression(self) -> None:
        memory = self._memory_with_trace()
        for index in range(15):  # 塞爆上下文，强制压缩
            memory.add_observation("read_file", f"文件 {index} 的内容：" + "y" * 400)

        messages = memory.build_messages()
        self.assertTrue(memory.last_stats.compressed, "该场景应当触发压缩")
        ledger_messages = [m for m in messages if m["content"].startswith(LEDGER_PREFIX)]
        self.assertEqual(len(ledger_messages), 1, "压缩后仍必须保留一份进度台账")
        self.assertIn("list_files", ledger_messages[0]["content"])

    def test_failed_action_is_marked_in_ledger(self) -> None:
        memory = self._memory_with_trace()
        memory.add_trace({
            "step": 3, "action": "read_file", "arguments": '{"path": "missing.py"}',
            "result": "失败：路径不存在", "ok": False,
        })
        self.assertIn("该调用失败", memory.render_ledger())


if __name__ == "__main__":
    unittest.main()
