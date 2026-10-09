# -*- coding: utf-8 -*-
"""命令行界面测试（在进程内调用 ``main``，避免起子进程）。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from codereview.cli import EXIT_CONFIG_ERROR, EXIT_DEGRADED, EXIT_OK, EXIT_THRESHOLD, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUGGY_TARGET = "examples/buggy_sample.py"


def run_cli(argv: Sequence[str]) -> tuple[int, str]:
    """执行 CLI 并捕获标准输出。

    stderr 也一并捕获：其一，``main`` 会把配置错误等写到这里，
    让测试输出保持干净（否则演示时跑一遍测试就会刷出吓人的红字）；
    其二，顺便断言错误信息确实被写到了 stderr 而不是 stdout。
    """
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    last_stderr[0] = stderr.getvalue()
    return code, stdout.getvalue()


#: 最近一次 :func:`run_cli` 捕获到的 stderr（单线程测试够用）
last_stderr: list[str] = [""]


class ReviewCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mock_review_succeeds(self) -> None:
        code, output = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("代码审查 Agent", output)
        self.assertIn("审查结果", output)
        self.assertIn("问题统计", output)

    def test_json_output_is_machine_readable(self) -> None:
        code, output = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--json",
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(output)
        self.assertIn("issues", payload)
        self.assertIn("counts", payload)
        self.assertGreater(payload["counts"]["critical"] + payload["counts"]["major"], 0)
        self.assertTrue(payload["meta"]["tool_calls"])

    def test_fail_on_threshold_returns_ci_exit_code(self) -> None:
        code, _ = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--fail-on", "critical",
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_THRESHOLD)

    def test_fail_on_none_returns_zero(self) -> None:
        code, _ = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--fail-on", "none",
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)

    def test_markdown_report_is_written(self) -> None:
        target = self.out_dir / "report.md"
        code, output = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--out", str(target),
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("# 代码审查报告", content)
        self.assertNotIn("\x1b[", content)  # 落盘的报告不应带颜色码
        self.assertIn("报告已写入", output)

    def test_json_report_is_written_by_extension(self) -> None:
        target = self.out_dir / "report.json"
        code, _ = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--out", str(target),
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)
        json.loads(target.read_text(encoding="utf-8"))

    def test_clean_file_reports_no_critical_issue(self) -> None:
        code, output = run_cli([
            "review", "examples/clean_sample.py", "--mock", "--no-color", "--json",
            "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["critical"], 0)
        self.assertEqual(payload["counts"]["major"], 0)

    def test_exhausted_steps_degrades_but_still_writes_a_report(self) -> None:
        """步数耗尽不是崩溃：返回专用退出码，且报告必须已经落盘。"""
        target = self.out_dir / "degraded.md"
        code, output = run_cli([
            "review", BUGGY_TARGET, "--mock", "--no-color", "--max-steps", "2",
            "--out", str(target), "--workspace", str(PROJECT_ROOT),
        ])
        self.assertEqual(code, EXIT_DEGRADED)
        self.assertIn("未能完全收敛", output)
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("降级说明", content)
        self.assertIn("严重（critical）", content)  # 降级报告依然带上了静态分析证据


class OtherCommandsTests(unittest.TestCase):
    def test_tools_command_lists_registry(self) -> None:
        code, output = run_cli(["tools", "--mock", "--no-color", "--workspace", str(PROJECT_ROOT)])
        self.assertEqual(code, EXIT_OK)
        for name in ("analyze_code", "read_file", "search_code", "check_syntax"):
            self.assertIn(name, output)
        self.assertIn("run_code", output)  # 提示里会说明它是危险工具、默认不注册

    def test_no_arguments_prints_help(self) -> None:
        code, output = run_cli([])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("usage:", output)

    def test_bad_workspace_returns_config_error(self) -> None:
        code, output = run_cli([
            "review", ".", "--mock", "--no-color", "--workspace", "C:/definitely/not/here",
        ])
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        # 错误信息应当走 stderr，保持 stdout 干净（便于 --json 被管道消费）
        self.assertEqual(output, "")
        self.assertIn("配置错误", last_stderr[0])


if __name__ == "__main__":
    unittest.main()
