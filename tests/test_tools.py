# -*- coding: utf-8 -*-
"""工具层测试：注册表、参数校验、路径沙箱、错误处理。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codereview.config import Config
from codereview.errors import PathSecurityError, ToolArgumentError
from codereview.tools import (
    ListFilesTool,
    ReadFileTool,
    RunCodeTool,
    build_default_registry,
)

SAMPLE = '''\
"""示例模块。"""

import os


def add(a, b):
    """返回两数之和。"""
    return a + b


def broken(:
    pass
'''


class ToolTestCase(unittest.TestCase):
    """公共脚手架：建一个临时工作区。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "good.py").write_text(SAMPLE.split("def broken")[0], encoding="utf-8")
        (self.root / "pkg" / "bad.py").write_text(SAMPLE, encoding="utf-8")
        (self.root / "notes.txt").write_text("TODO: 记得写文档\n", encoding="utf-8")
        self.config = Config(workspace=self.root, mock=True)
        self.registry = build_default_registry(self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class RegistryTests(ToolTestCase):
    def test_default_tools_registered(self) -> None:
        names = self.registry.names()
        self.assertIn("list_files", names)
        self.assertIn("read_file", names)
        self.assertIn("search_code", names)
        self.assertIn("analyze_code", names)
        self.assertIn("check_syntax", names)

    def test_dangerous_tool_hidden_by_default(self) -> None:
        """run_code 属于危险工具，默认不应注册。"""
        self.assertNotIn("run_code", self.registry.names())

        config = Config(workspace=self.root, mock=True, allow_exec=True)
        self.assertIn("run_code", build_default_registry(config).names())

    def test_unknown_tool_returns_failure_not_exception(self) -> None:
        result = self.registry.execute("does_not_exist", {})
        self.assertFalse(result.ok)
        self.assertIn("没有名为", result.error)

    def test_describe_mentions_parameters(self) -> None:
        text = self.registry.describe()
        self.assertIn("read_file", text)
        self.assertIn("path", text)


class PathSandboxTests(ToolTestCase):
    """路径沙箱是「提示注入」场景下的最后一道防线。"""

    def test_relative_path_inside_workspace_is_allowed(self) -> None:
        result = self.registry.execute("read_file", {"path": "notes.txt"})
        self.assertTrue(result.ok)
        self.assertIn("TODO", result.output)

    def test_parent_escape_is_blocked(self) -> None:
        result = self.registry.execute("read_file", {"path": "../../etc/passwd"})
        self.assertFalse(result.ok)
        self.assertIn("安全拦截", result.error)

    def test_absolute_path_outside_workspace_is_blocked(self) -> None:
        outside = "C:/Windows/win.ini" if os.name == "nt" else "/etc/hosts"
        result = self.registry.execute("read_file", {"path": outside})
        self.assertFalse(result.ok)
        self.assertIn("安全拦截", result.error)

    def test_missing_file_reports_clear_error(self) -> None:
        result = self.registry.execute("read_file", {"path": "nope.py"})
        self.assertFalse(result.ok)
        self.assertIn("路径不存在", result.error)


class ArgumentValidationTests(ToolTestCase):
    def test_missing_required_argument(self) -> None:
        result = self.registry.execute("read_file", {})
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.error)

    def test_unknown_argument_is_rejected_with_hint(self) -> None:
        result = self.registry.execute("read_file", {"path": "notes.txt", "typo": 1})
        self.assertFalse(result.ok)
        self.assertIn("未知参数", result.error)
        self.assertIn("path", result.error)  # 提示里带上正确参数名，帮助模型自纠

    def test_wrong_type_is_rejected(self) -> None:
        result = self.registry.execute("read_file", {"path": "notes.txt", "start_line": "第一行"})
        self.assertFalse(result.ok)
        self.assertIn("期望 integer", result.error)

    def test_out_of_range_is_rejected(self) -> None:
        result = self.registry.execute("read_file", {"path": "notes.txt", "max_lines": 99999})
        self.assertFalse(result.ok)
        self.assertIn("不能大于", result.error)

    def test_bool_is_not_accepted_as_integer(self) -> None:
        with self.assertRaises(ToolArgumentError):
            ReadFileTool(self.config).validate({"path": "notes.txt", "start_line": True})


class ListFilesTests(ToolTestCase):
    def test_lists_python_files(self) -> None:
        result = self.registry.execute("list_files", {"path": ".", "pattern": "*.py"})
        self.assertTrue(result.ok)
        self.assertIn("good.py", result.output)
        self.assertIn("bad.py", result.output)

    def test_directory_argument_reports_clear_error(self) -> None:
        result = self.registry.execute("list_files", {"path": "pkg"})
        self.assertTrue(result.ok)

        result = self.registry.execute("read_file", {"path": "pkg"})
        self.assertFalse(result.ok)
        self.assertIn("是目录", result.error)


class ReadFileTests(ToolTestCase):
    def test_line_numbers_are_included(self) -> None:
        result = self.registry.execute("read_file", {"path": "notes.txt"})
        self.assertTrue(result.ok)
        self.assertIn("1| TODO", result.output)

    def test_line_range(self) -> None:
        path = self.root / "many.txt"
        path.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
        result = self.registry.execute("read_file", {"path": "many.txt", "start_line": 5, "end_line": 7})
        self.assertTrue(result.ok)
        self.assertIn("line5", result.output)
        self.assertIn("line7", result.output)
        self.assertNotIn("line8", result.output)


class SearchCodeTests(ToolTestCase):
    def test_search_finds_matches_with_line_numbers(self) -> None:
        result = self.registry.execute("search_code", {"pattern": "TODO", "path": "."})
        self.assertTrue(result.ok)
        self.assertIn("notes.txt:1:", result.output)

    def test_search_accepts_a_single_file(self) -> None:
        """path 指向文件时只搜该文件，不要扩大到整个目录。"""
        result = self.registry.execute("search_code", {"pattern": "TODO", "path": "notes.txt"})
        self.assertTrue(result.ok)
        self.assertIn("notes.txt:1:", result.output)

        result = self.registry.execute("search_code", {"pattern": "import", "path": "pkg/good.py"})
        self.assertIn("pkg/good.py", result.output)
        self.assertNotIn("bad.py", result.output)

    def test_invalid_regex_is_reported_as_argument_error(self) -> None:
        result = self.registry.execute("search_code", {"pattern": "([unclosed"})
        self.assertFalse(result.ok)
        self.assertIn("正则表达式非法", result.error)

    def test_no_match_is_not_an_error(self) -> None:
        result = self.registry.execute("search_code", {"pattern": "ZZZ_NOT_EXIST"})
        self.assertTrue(result.ok)
        self.assertIn("未找到", result.output)


class AnalyzeCodeTests(ToolTestCase):
    def test_directory_analysis_returns_json_with_findings(self) -> None:
        result = self.registry.execute("analyze_code", {"path": "pkg"})
        self.assertTrue(result.ok)
        self.assertIn('"findings"', result.output)
        self.assertIn("syntax-error", result.output)

    def test_non_python_file_is_rejected(self) -> None:
        result = self.registry.execute("analyze_code", {"path": "notes.txt"})
        self.assertFalse(result.ok)
        self.assertIn("只支持 Python", result.error)


class CheckSyntaxTests(ToolTestCase):
    def test_reports_syntax_error(self) -> None:
        result = self.registry.execute("check_syntax", {"path": "pkg"})
        self.assertTrue(result.ok)
        self.assertIn("bad.py", result.output)
        self.assertIn("语法检查", result.output)

    def test_clean_directory_passes(self) -> None:
        result = self.registry.execute("check_syntax", {"path": "pkg/good.py"})
        self.assertTrue(result.ok)
        self.assertIn("语法检查通过", result.output)


class RunCodeGuardTests(ToolTestCase):
    def test_disabled_by_default(self) -> None:
        tool = RunCodeTool(self.config)
        result = tool.safe_run({"path": "pkg/good.py"})
        self.assertFalse(result.ok)
        self.assertIn("已被禁用", result.error)


class SandboxUnitTests(ToolTestCase):
    """直接测试 :meth:`Tool.resolve_path` 的边界行为。"""

    def test_workspace_root_itself_is_allowed(self) -> None:
        tool = ListFilesTool(self.config)
        self.assertEqual(tool.resolve_path("."), self.root.resolve())

    def test_symlink_style_escape_is_blocked(self) -> None:
        tool = ReadFileTool(self.config)
        with self.assertRaises(PathSecurityError):
            tool.resolve_path("../" * 5 + "etc/hosts")


if __name__ == "__main__":
    unittest.main()
