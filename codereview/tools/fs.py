"""文件系统工具：目录浏览、文件读取、代码搜索。

这三个工具构成 Agent 的「眼睛」，全部受 :meth:`Tool.resolve_path` 沙箱约束，
只能访问 ``--workspace`` 指定的根目录。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterator, Sequence

from ..errors import ToolArgumentError, ToolExecutionError
from ..utils import guess_language, is_probably_text, read_text, relpath, truncate
from .base import Tool, ToolResult

#: 遍历时跳过的目录（既省时间，也避免把第三方依赖当成待审查代码）
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", ".env", "dist", "build", ".idea", ".vscode",
    ".next", "target", "bin", "obj", ".tox", ".eggs", "site-packages",
}

#: 单文件搜索大小上限，超过则跳过（避免把压缩包/数据文件读进内存）
MAX_SEARCH_FILE_BYTES = 2_000_000


def iter_files(
    root: Path,
    pattern: str = "*",
    *,
    limit: int | None = None,
    skip_dirs: set[str] | None = None,
) -> Iterator[Path]:
    """递归遍历目录，按文件名 glob 过滤，并跳过常见噪音目录。

    比 ``Path.rglob`` 多做两件事：可控的目录剪枝、以及稳定的字典序输出
    （稳定顺序让 LLM 的上下文可复现，也让测试可断言）。
    """
    skip_dirs = SKIP_DIRS if skip_dirs is None else skip_dirs
    count = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in skip_dirs or entry.name.startswith("."):
                    continue
                stack.append(entry)
            elif fnmatch.fnmatch(entry.name, pattern):
                yield entry
                count += 1
                if limit is not None and count >= limit:
                    return


class ListFilesTool(Tool):
    """列出目录下的文件，用于先侦察仓库结构。"""

    name = "list_files"
    description = "列出工作区内某个目录（递归）的文件清单，支持通配符过滤，用于了解仓库结构。"
    parameters = {
        "path": {"type": "string", "description": "目录路径（相对工作区），默认当前目录", "required": False, "default": "."},
        "pattern": {"type": "string", "description": "文件名通配符，如 *.py、*.js", "required": False, "default": "*"},
        "max_results": {"type": "integer", "description": "最多返回多少个文件", "required": False, "default": 200, "min": 1, "max": 2000},
        "show_size": {"type": "boolean", "description": "是否显示文件大小", "required": False, "default": True},
    }

    def run(self, path: str = ".", pattern: str = "*", max_results: int = 200, show_size: bool = True) -> ToolResult:
        root = self.resolve_path(path)
        if not root.is_dir():
            raise ToolExecutionError(f"{self.display_path(root)} 不是目录（如需读单个文件请用 read_file）")

        files = list(iter_files(root, pattern, limit=max_results + 1))
        truncated = len(files) > max_results
        files = files[:max_results]

        if not files:
            return ToolResult.success(
                f"目录 {self.display_path(root)} 下没有匹配 {pattern} 的文件。",
                count=0, path=self.display_path(root),
            )

        lines = [f"目录 {self.display_path(root)} 下匹配 `{pattern}` 的文件共 {len(files)} 个"
                 + ("（已达上限，结果被截断）" if truncated else "") + "："]
        total_bytes = 0
        by_suffix: dict[str, int] = {}
        for file in files:
            try:
                size = file.stat().st_size
            except OSError:
                size = 0
            total_bytes += size
            suffix = file.suffix.lower() or "(无扩展名)"
            by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
            shown = f"  {relpath(file, self.workspace)}"
            lines.append(f"{shown}  [{size} B]" if show_size else shown)

        lines.append("")
        lines.append("扩展名分布：" + "，".join(f"{k}×{v}" for k, v in sorted(by_suffix.items(), key=lambda kv: -kv[1])))
        lines.append(f"合计 {total_bytes} 字节。建议优先审查体量最大的源码文件。")
        return ToolResult.success(
            truncate("\n".join(lines), self.config.max_observation_chars),
            count=len(files), truncated=truncated, path=self.display_path(root),
        )


class ReadFileTool(Tool):
    """读取文件内容（带行号），是问题定位的证据来源。"""

    name = "read_file"
    description = "读取工作区内某个文本文件的内容，返回带行号的代码，可指定起止行。"
    parameters = {
        "path": {"type": "string", "description": "文件路径（相对工作区）"},
        "start_line": {"type": "integer", "description": "起始行号，从 1 开始", "required": False, "default": 1, "min": 1},
        "end_line": {"type": "integer", "description": "结束行号（含），默认到最后一行", "required": False},
        "max_lines": {"type": "integer", "description": "单次最多返回多少行", "required": False, "default": 400, "min": 1, "max": 3000},
    }

    def run(self, path: str, start_line: int = 1, end_line: int | None = None, max_lines: int = 400) -> ToolResult:
        file_path = self.resolve_path(path)
        if file_path.is_dir():
            raise ToolExecutionError(f"{self.display_path(file_path)} 是目录（请用 list_files）")

        size = file_path.stat().st_size
        if size > self.config.max_file_bytes:
            raise ToolExecutionError(
                f"文件过大（{size} 字节 > 上限 {self.config.max_file_bytes}），"
                "请用 read_file 的 start_line/end_line 分段读取，或先用 search_code 定位"
            )
        if not is_probably_text(file_path):
            raise ToolExecutionError(f"{self.display_path(file_path)} 看起来是二进制文件，已跳过")

        content = read_text(file_path)
        all_lines = content.splitlines()
        total = len(all_lines)

        start = max(1, start_line)
        stop = total if end_line is None else min(end_line, total)
        if start > total:
            raise ToolExecutionError(f"start_line={start} 超出文件总行数 {total}")
        if stop < start:
            raise ToolExecutionError(f"end_line={stop} 小于 start_line={start}")

        window = all_lines[start - 1 : stop]
        clipped = len(window) > max_lines
        window = window[:max_lines]

        header = (
            f"文件 {self.display_path(file_path)}（{guess_language(file_path)}，共 {total} 行 / {size} 字节）"
            f"，显示第 {start}-{start + len(window) - 1} 行："
        )
        body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(window))
        if clipped:
            body += f"\n... [本次仅显示前 {max_lines} 行，请用 start_line 继续读取]"
        return ToolResult.success(
            truncate(f"{header}\n{body}", self.config.max_observation_chars),
            lines=len(window), total_lines=total, path=self.display_path(file_path),
        )


class SearchCodeTool(Tool):
    """在仓库内做正则搜索，用于验证「某个写法是否广泛存在」。"""

    name = "search_code"
    description = (
        "按正则搜索代码，返回 `文件:行号: 内容` 列表，用于定位高危写法或统计出现次数。"
        "path 可以是目录（递归搜索），也可以是单个文件（只搜该文件）。"
    )
    parameters = {
        "pattern": {"type": "string", "description": "Python 正则表达式，如 eval\\(|except\\s*:"},
        "path": {"type": "string", "description": "搜索根目录或单个文件（相对工作区）", "required": False, "default": "."},
        "file_pattern": {"type": "string", "description": "只搜索匹配该通配符的文件（path 为目录时生效）", "required": False, "default": "*"},
        "max_results": {"type": "integer", "description": "最多返回多少条命中", "required": False, "default": 50, "min": 1, "max": 500},
        "ignore_case": {"type": "boolean", "description": "是否忽略大小写", "required": False, "default": False},
    }

    def run(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str = "*",
        max_results: int = 50,
        ignore_case: bool = False,
    ) -> ToolResult:
        target = self.resolve_path(path)
        # path 指向单个文件时只搜这个文件——「我让你看这个文件」不该扩大到整个目录
        files: list[Path] = [target] if target.is_file() else list(iter_files(target, file_pattern))
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ToolArgumentError(f"正则表达式非法（{exc}）：{pattern!r}") from exc

        hits: list[str] = []
        scanned = 0
        skipped = 0
        for file in files:
            try:
                if file.stat().st_size > MAX_SEARCH_FILE_BYTES or not is_probably_text(file):
                    skipped += 1
                    continue
                text = read_text(file)
            except OSError:
                skipped += 1
                continue
            scanned += 1
            relative = relpath(file, self.workspace)
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(hits) > max_results:
                        break
            if len(hits) > max_results:
                break

        where = self.display_path(target)
        truncated = len(hits) > max_results
        hits = hits[:max_results]
        if not hits:
            return ToolResult.success(
                f"在 {where} 下扫描 {scanned} 个文件，未找到匹配 /{pattern}/ 的内容"
                + (f"（跳过 {skipped} 个二进制或超大文件）" if skipped else "") + "。",
                count=0, scanned=scanned, path=where,
            )
        head = (
            f"在 {where} 下扫描 {scanned} 个文件，匹配 /{pattern}/ 的命中 {len(hits)} 条"
            + ("（已截断）" if truncated else "") + "："
        )
        return ToolResult.success(
            truncate("\n".join([head] + hits), self.config.max_observation_chars),
            count=len(hits), scanned=scanned, truncated=truncated, path=where,
        )


__all__ = ["ListFilesTool", "ReadFileTool", "SearchCodeTool", "iter_files", "SKIP_DIRS"]
