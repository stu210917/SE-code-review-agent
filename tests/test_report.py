# -*- coding: utf-8 -*-
"""报告渲染测试：重点验证对「模型输出不规范」的容错能力。"""

from __future__ import annotations

import json
import unittest

from codereview.report import Issue, ReviewReport


class IssueNormalizationTests(unittest.TestCase):
    def test_severity_aliases_are_normalized(self) -> None:
        self.assertEqual(Issue(title="t", severity="HIGH").severity, "critical")
        self.assertEqual(Issue(title="t", severity="中等").severity, "major")
        self.assertEqual(Issue(title="t", severity="warning").severity, "minor")
        self.assertEqual(Issue(title="t", severity="随便什么").severity, "minor")

    def test_line_accepts_string(self) -> None:
        self.assertEqual(Issue(title="t", line="42").line, 42)

    def test_bad_line_degrades_to_zero(self) -> None:
        self.assertEqual(Issue(title="t", line="第十二行").line, 0)
        self.assertEqual(Issue(title="t", line=None).line, 0)

    def test_unknown_category_falls_back(self) -> None:
        self.assertEqual(Issue(title="t", category="外星问题").category, "maintainability")

    def test_location_formatting(self) -> None:
        self.assertEqual(Issue(title="t", file="a.py", line=3).location, "a.py:3")
        self.assertEqual(Issue(title="t", file="a.py").location, "a.py")
        self.assertEqual(Issue(title="t").location, "（未指明位置）")

    def test_from_dict_accepts_alternative_field_names(self) -> None:
        issue = Issue.from_dict({"message": "标题", "path": "x.py", "line": 2, "fix": "这样改"})
        self.assertIsNotNone(issue)
        self.assertEqual(issue.title, "标题")
        self.assertEqual(issue.file, "x.py")
        self.assertEqual(issue.suggestion, "这样改")

    def test_from_dict_returns_none_without_title(self) -> None:
        self.assertIsNone(Issue.from_dict({"severity": "major"}))
        self.assertIsNone(Issue.from_dict("不是字典"))


class ReportConstructionTests(unittest.TestCase):
    def test_none_payload_degrades(self) -> None:
        report = ReviewReport.from_final_answer(None, {})
        self.assertTrue(report.degraded)
        self.assertIn("结构化结论", report.summary)

    def test_empty_payload_degrades(self) -> None:
        report = ReviewReport.from_final_answer({}, {})
        self.assertTrue(report.degraded)

    def test_issues_as_single_dict_is_accepted(self) -> None:
        report = ReviewReport.from_final_answer({"summary": "s", "issues": {"title": "单条问题"}}, {})
        self.assertEqual(len(report.issues), 1)

    def test_issues_of_wrong_type_is_ignored(self) -> None:
        report = ReviewReport.from_final_answer({"summary": "s", "issues": "不是列表"}, {})
        self.assertEqual(report.issues, [])

    def test_garbage_issue_entries_are_skipped(self) -> None:
        report = ReviewReport.from_final_answer(
            {"summary": "s", "issues": [{"title": "好的"}, "垃圾", 42, {"no_title": 1}]}, {}
        )
        self.assertEqual(len(report.issues), 1)

    def test_score_is_clamped(self) -> None:
        self.assertEqual(ReviewReport.from_final_answer({"summary": "s", "score": 150}, {}).score, 100)
        self.assertEqual(ReviewReport.from_final_answer({"summary": "s", "score": -20}, {}).score, 0)
        self.assertEqual(ReviewReport.from_final_answer({"summary": "s", "score": "85.7"}, {}).score, 85)
        self.assertEqual(ReviewReport.from_final_answer({"summary": "s", "score": "很高"}, {}).score, 0)

    def test_issues_are_sorted_by_severity(self) -> None:
        report = ReviewReport.from_final_answer({
            "summary": "s",
            "issues": [
                {"title": "小问题", "severity": "info", "file": "b.py", "line": 1},
                {"title": "致命问题", "severity": "critical", "file": "a.py", "line": 9},
                {"title": "中等问题", "severity": "minor", "file": "a.py", "line": 2},
            ],
        }, {})
        self.assertEqual([issue.severity for issue in report.issues], ["critical", "minor", "info"])

    def test_counts_and_grade(self) -> None:
        report = ReviewReport.from_final_answer({
            "summary": "s", "score": 92,
            "issues": [{"title": "a", "severity": "major"}, {"title": "b", "severity": "major"}],
        }, {})
        self.assertEqual(report.counts["major"], 2)
        self.assertEqual(report.counts["critical"], 0)
        self.assertEqual(report.grade, "优秀")
        self.assertEqual(ReviewReport(score=75).grade, "中等")
        self.assertEqual(ReviewReport(score=59).grade, "待改进")

    def test_highlights_string_becomes_list(self) -> None:
        report = ReviewReport.from_final_answer({"summary": "s", "highlights": "只有一条"}, {})
        self.assertEqual(report.highlights, ["只有一条"])


class ReportRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = ReviewReport.from_final_answer(
            {
                "summary": "整体结构清晰，但存在两处明显缺陷。",
                "score": 72,
                "issues": [
                    {"file": "app.py", "line": 12, "severity": "critical", "category": "security",
                     "title": "eval 执行外部输入", "detail": "会把字符串当代码执行。",
                     "suggestion": "改用 ast.literal_eval。"},
                    {"file": "app.py", "line": 30, "severity": "minor", "category": "style",
                     "title": "使用 == None", "detail": "应使用 is None。", "suggestion": "改为 is None。"},
                ],
                "highlights": ["分层清晰"],
                "next_steps": ["补充测试"],
            },
            {"model": "test-model", "mode": "mock", "steps": 3, "tool_calls": 2,
             "tools_used": ["analyze_code"], "duration_s": 1.5, "generated_at": "2026-01-01 00:00:00",
             "trace": [{"step": 1, "action": "analyze_code", "arguments": '{"path": "app.py"}', "result": "完成"}]},
        )

    def test_markdown_structure(self) -> None:
        markdown = self.report.to_markdown()
        self.assertIn("# 代码审查报告", markdown)
        self.assertIn("## 一、总体评价", markdown)
        self.assertIn("| 严重度 | 数量 |", markdown)
        self.assertIn("eval 执行外部输入", markdown)
        self.assertIn("`app.py:12`", markdown)
        self.assertIn("72/100", markdown)
        self.assertIn("分层清晰", markdown)
        self.assertIn("analyze_code", markdown)

    def test_json_round_trip(self) -> None:
        payload = json.loads(self.report.to_json())
        self.assertEqual(payload["score"], 72)
        self.assertEqual(payload["counts"]["critical"], 1)
        self.assertEqual(len(payload["issues"]), 2)
        self.assertEqual(payload["issues"][0]["severity_label"], "严重")
        self.assertEqual(payload["issues"][0]["location"], "app.py:12")

    def test_empty_report_renders_gracefully(self) -> None:
        markdown = ReviewReport(summary="没问题", score=100).to_markdown()
        self.assertIn("未发现需要记录的问题", markdown)

    def test_degraded_section_is_rendered(self) -> None:
        markdown = ReviewReport(score=0, degraded=True, degraded_reason="步数耗尽").to_markdown()
        self.assertIn("降级说明", markdown)
        self.assertIn("步数耗尽", markdown)


if __name__ == "__main__":
    unittest.main()
