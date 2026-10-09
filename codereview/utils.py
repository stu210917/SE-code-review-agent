"""通用小工具：token 估算、文本截断、表格渲染、语言识别等。

刻意不依赖任何第三方库，保证 ``pip install`` 之外零成本运行。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

# 中日韩统一表意文字 + 全角标点，粗略视为「一个字 ≈ 一个 token」
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
SEVERITY_ALIASES = {
    "critical": "critical", "blocker": "critical", "high": "critical", "严重": "critical", "致命": "critical",
    "major": "major", "medium": "major", "error": "major", "重要": "major", "中等": "major",
    "minor": "minor", "low": "minor", "warning": "minor", "warn": "minor", "次要": "minor", "轻微": "minor",
    "info": "info", "note": "info", "suggestion": "info", "提示": "info", "建议": "info",
}

# 扩展名 → 语言名，用于给 LLM 提供上下文
_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".sql": "sql", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".md": "markdown", ".html": "html", ".css": "css",
}

TEXT_SUFFIXES = set(_LANGUAGE_BY_SUFFIX) | {".txt", ".cfg", ".conf", ".env", ".gitignore", ".dockerfile"}


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    规则：CJK 字符按 1 token / 字，其余字符按 4 字符 / token。
    误差在 20% 以内，足够用于上下文窗口的预算控制，且不需要引入 tokenizer 依赖。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格（用于对齐中文表格）。"""
    return text + " " * max(0, width - display_width(text))


def truncate(text: str, limit: int, *, keep_head: bool = True) -> str:
    """按字符数截断文本，并明确标注被截断了多少字符。"""
    if limit <= 0 or len(text) <= limit:
        return text
    if keep_head:
        head = text[:limit]
        return f"{head}\n... [输出过长，已截断 {len(text) - limit} 个字符]"
    tail = text[-limit:]
    return f"... [输出过长，已省略前 {len(text) - limit} 个字符]\n{tail}"


def clip_middle(text: str, limit: int) -> str:
    """保留首尾、丢弃中间，用于把超长文件塞进 prompt。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head_len = limit * 2 // 3
    tail_len = limit - head_len
    omitted = len(text) - head_len - tail_len
    return f"{text[:head_len]}\n... [中间省略 {omitted} 个字符] ...\n{text[-tail_len:]}"


def guess_language(path: str | Path) -> str:
    """根据文件扩展名猜测编程语言。"""
    suffix = Path(path).suffix.lower()
    if not suffix:
        name = Path(path).name.lower()
        if name in ("dockerfile", "makefile"):
            return name
        return "text"
    return _LANGUAGE_BY_SUFFIX.get(suffix, "text")


def is_probably_text(path: str | Path, *, probe_bytes: int = 4096) -> bool:
    """粗略判断文件是否为文本文件（避免把图片当代码读进上下文）。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(probe_bytes)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    try:
        chunk.decode("gbk")
        return True
    except UnicodeDecodeError:
        return False


def read_text(path: str | Path) -> str:
    """以 utf-8 → gbk → latin-1 的顺序尝试解码，兼容中文 Windows 上的老文件。"""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_severity(value: str | None) -> str:
    """把模型给出的各种严重度写法归一化到四档。"""
    if not value:
        return "minor"
    return SEVERITY_ALIASES.get(str(value).strip().lower(), "minor")


def severity_rank(value: str) -> int:
    """严重度排序权重，越小越严重。"""
    return SEVERITY_ORDER.get(normalize_severity(value), 9)


def format_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """渲染成对齐的等宽文本表格（自动处理中英文混排宽度）。"""
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], display_width(cell))
    lines = ["  ".join(pad(h, w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(pad(c, w) for c, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码，便于写入日志文件。"""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def enable_utf8_output() -> None:
    """把标准输出/标准错误切换成 UTF-8 编码。

    为什么需要它：在中文 Windows 上，Python 默认按本地代码页（cp936/GBK）编码
    **管道与重定向**的输出。于是 ``python run.py review . | Select-String 漏洞``
    会因为「写的是 GBK、读的是 UTF-8」而匹配不到任何内容，用户只会觉得
    「这工具怎么没输出」。

    强制 UTF-8 对交互式控制台同样安全：Windows 的 ``_WindowsConsoleIO``
    本身就是按 UTF-8 解析后再调用 ``WriteConsoleW``，所以中文显示不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # 流已关闭或被替换成非文本流时忽略
            pass


def mask_strings_and_comments(line: str) -> str:
    """把字符串字面量与行尾注释替换成空格，只留下「代码骨架」。

    用途：判断某个关键字到底是**真的写在代码里**，还是仅仅出现在字符串/注释中
    （例如文档里举例写的 ``eval(``，或规则表里的 ``"os.system"``）。
    返回与输入**等长**的字符串，因此可以按相同下标定位原始字符。

    局限：按行处理，跨行的三引号字符串无法完整识别——这是有意为之的取舍，
    搜索阶段只做粗筛，精确判定交给 AST 分析。
    """
    out: list[str] = []
    index = 0
    length = len(line)
    quote = ""
    while index < length:
        char = line[index]
        if quote:
            if char == "\\":  # 转义：连同下一个字符一起屏蔽
                out.append(" ")
                if index + 1 < length:
                    out.append(" ")
                index += 2
                continue
            if line.startswith(quote, index):
                out.append(" " * len(quote))
                index += len(quote)
                quote = ""
                continue
            out.append(" ")
            index += 1
            continue
        if char == "#":  # 行尾注释，整段屏蔽
            out.append(" " * (length - index))
            break
        if char in ("'", '"'):
            triple = char * 3
            if line.startswith(triple, index):
                quote = triple
                out.append(" " * 3)
                index += 3
            else:
                quote = char
                out.append(" ")
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹，很多模型即使被要求不要加，仍然会加。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]  # 去掉开头的 ```
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def repair_json(text: str) -> str:
    """修复最常见的两种 JSON 毛病：尾随逗号、中文全角引号。"""
    fixed = re.sub(r",\s*([}\]])", r"\1", text)
    fixed = fixed.replace("“", '"').replace("”", '"')
    return fixed


def extract_json_objects(text: str) -> list[Any]:
    """扫描文本中所有**顶层** ``{...}`` 对象并解析（容忍前后有说明文字）。

    相比 ``re.findall(r"\\{.*\\}")``，这里做了括号配对与字符串转义处理，
    因此不会在嵌套对象或字符串内含 ``}`` 时错切。
    """
    objects: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : index + 1]
                    try:
                        objects.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        try:
                            objects.append(json.loads(repair_json(chunk)))
                        except json.JSONDecodeError:
                            pass
                    start = -1
    return objects


def relpath(path: str | Path, root: str | Path) -> str:
    """尽量输出相对于工作区的短路径，报告更易读。"""
    try:
        return os.path.relpath(str(path), str(root)).replace("\\", "/")
    except ValueError:  # 跨盘符
        return str(path).replace("\\", "/")


__all__ = [
    "SEVERITY_ORDER",
    "TEXT_SUFFIXES",
    "estimate_tokens",
    "display_width",
    "pad",
    "truncate",
    "clip_middle",
    "guess_language",
    "is_probably_text",
    "read_text",
    "normalize_severity",
    "severity_rank",
    "format_table",
    "strip_ansi",
    "enable_utf8_output",
    "mask_strings_and_comments",
    "strip_code_fence",
    "repair_json",
    "extract_json_objects",
    "relpath",
]
