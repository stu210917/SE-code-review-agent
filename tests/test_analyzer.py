# -*- coding: utf-8 -*-
"""静态分析器测试：确保每条规则都能命中，且干净代码不误报。"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path

from codereview.tools.python_ast import PythonAnalyzer, cyclomatic_complexity
import ast

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

BUGGY = textwrap.dedent(
    '''
    import os
    import json

    PASSWORD = "hunter2secret"

    def handler(items=[], size=10, alpha=1, beta=2, gamma=3, delta=4, epsilon=5):
        try:
            open("/tmp/x").read()
        except:
            pass
        if items == None:
            return eval("1 + 1")
        for index in range(len(items)):
            print(index)
        return items

    def handler(value):
        return value

    class Cache:
        entries = []

        def get(self, key):
            return self.entries
    '''
)


def rules_of(source: str, path: str = "sample.py") -> set[str]:
    report = PythonAnalyzer(source, path).analyze()
    return {finding.rule for finding in report.findings}


class AnalyzerRuleTests(unittest.TestCase):
    """逐条规则的正向测试。"""

    def setUp(self) -> None:
        self.rules = rules_of(BUGGY)

    def test_security_rules(self) -> None:
        self.assertIn("hardcoded-secret", self.rules)
        self.assertIn("eval-exec", self.rules)

    def test_bug_rules(self) -> None:
        self.assertIn("bare-except", self.rules)
        self.assertIn("except-pass", self.rules)
        self.assertIn("mutable-default", self.rules)
        self.assertIn("open-without-with", self.rules)
        self.assertIn("redefined-function", self.rules)
        self.assertIn("mutable-class-attr", self.rules)

    def test_maintainability_rules(self) -> None:
        self.assertIn("unused-import", self.rules)  # os / json 都未使用
        self.assertIn("too-many-args", self.rules)

    def test_style_rules(self) -> None:
        self.assertIn("none-comparison", self.rules)
        self.assertIn("range-len", self.rules)
        self.assertIn("missing-docstring", self.rules)

    def test_issue_carries_line_number(self) -> None:
        report = PythonAnalyzer(BUGGY, "sample.py").analyze()
        bare = next(f for f in report.findings if f.rule == "bare-except")
        self.assertGreater(bare.line, 0)
        self.assertEqual(bare.severity, "major")

    def test_secret_literal_is_not_echoed(self) -> None:
        """报告里不能回显口令明文，否则报告本身就变成了泄漏源。"""
        report = PythonAnalyzer(BUGGY, "sample.py").analyze()
        secret = next(f for f in report.findings if f.rule == "hardcoded-secret")
        self.assertNotIn("hunter2secret", secret.code)
        self.assertNotIn("hunter2secret", secret.detail)


class AnalyzerSyntaxErrorTests(unittest.TestCase):
    """语法错误要能被捕获成 critical，而不是让分析器崩溃。"""

    def test_syntax_error_is_critical(self) -> None:
        report = PythonAnalyzer("def broken(:\n    pass\n", "broken.py").analyze()
        self.assertFalse(report.syntax_ok)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].severity, "critical")
        self.assertEqual(report.metrics["functions"], 0)


class AnalyzerMetricsTests(unittest.TestCase):
    """度量指标的准确性。"""

    def test_cyclomatic_complexity_counts_branches(self) -> None:
        simple = ast.parse("def f():\n    return 1\n").body[0]
        self.assertEqual(cyclomatic_complexity(simple), 1)

        branching = ast.parse(
            "def f(x):\n"
            "    if x:\n"
            "        return 1\n"
            "    for i in range(3):\n"
            "        if i:\n"
            "            break\n"
            "    return 0\n"
        ).body[0]
        self.assertEqual(cyclomatic_complexity(branching), 4)

    def test_nested_function_not_double_counted(self) -> None:
        node = ast.parse(
            "def outer(x):\n"
            "    def inner(y):\n"
            "        if y:\n"
            "            return 1\n"
            "        return 0\n"
            "    return inner(x)\n"
        ).body[0]
        # outer 自身只有 return，复杂度应为 1（inner 的分支不计入 outer）
        self.assertEqual(cyclomatic_complexity(node), 1)

    def test_metrics_of_buggy_source(self) -> None:
        report = PythonAnalyzer(BUGGY, "sample.py").analyze()
        self.assertEqual(report.metrics["functions"], 3)  # handler ×2 + Cache.get
        self.assertEqual(report.metrics["classes"], 1)
        self.assertGreater(report.metrics["max_complexity"], 1)
        self.assertTrue(report.syntax_ok)


class RealExampleFilesTests(unittest.TestCase):
    """直接分析仓库里的示例文件，保证「演示脚本」与「分析器」始终配套。"""

    def test_buggy_sample_triggers_many_rules(self) -> None:
        source = (EXAMPLES / "buggy_sample.py").read_text(encoding="utf-8")
        report = PythonAnalyzer(source, "examples/buggy_sample.py").analyze()
        rules = {finding.rule for finding in report.findings}
        for expected in (
            "hardcoded-secret", "eval-exec", "os-system", "subprocess-shell",
            "pickle-loads", "yaml-load-unsafe", "bare-except", "except-pass",
            "mutable-default", "redefined-function", "star-import", "global-statement",
        ):
            self.assertIn(expected, rules, f"示例文件应当触发规则 {expected}")
        severities = {finding.severity for finding in report.findings}
        self.assertIn("critical", severities)

    def test_clean_sample_has_no_serious_findings(self) -> None:
        """对照组：良好代码不应被报出 critical / major 问题（防误报回归）。"""
        source = (EXAMPLES / "clean_sample.py").read_text(encoding="utf-8")
        report = PythonAnalyzer(source, "examples/clean_sample.py").analyze()
        serious = [f for f in report.findings if f.severity in ("critical", "major")]
        self.assertEqual(serious, [], f"干净代码被误报：{[(f.rule, f.line) for f in serious]}")
        self.assertTrue(report.syntax_ok)
        self.assertGreaterEqual(report.metrics["docstring_coverage"], 0.8)


if __name__ == "__main__":
    unittest.main()
