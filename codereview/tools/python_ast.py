"""Python 代码静态分析工具（Agent 的「确定性大脑」）。

为什么要有它？
--------------
大模型直接读代码容易「看走眼」：漏掉一个裸 ``except``，或者把 ``== None``
当成小问题。这里用 :mod:`ast` 做**确定性**检查，把可量化的结论交给模型，
模型只负责判断误报、解释影响、给出改法——这样审查结果既快又稳。

规则一览（``rule`` 字段）
------------------------
============================  ==========  ==========================================
规则 ID                        默认严重度   含义
============================  ==========  ==========================================
``syntax-error``              critical    语法错误，代码根本无法运行
``eval-exec``                 critical    使用 eval/exec 执行动态代码，存在注入风险
``hardcoded-secret``          major       硬编码口令/密钥
``pickle-loads``              major       反序列化不可信数据
``yaml-load-unsafe``          major       yaml.load 未指定 SafeLoader
``os-system``                 major       命令注入风险
``subprocess-shell``          major       shell=True 命令注入风险
``bare-except``               major      裸 except 会吞掉 KeyboardInterrupt/SystemExit
``except-pass``               major      异常被静默吞掉，故障难以定位
``mutable-default``           major      可变默认参数在多次调用间共享
``high-complexity``           major      圈复杂度过高，难以测试与维护
``long-function``             minor      函数过长
``too-many-args``             minor      参数过多
``redefined-function``        major      同名函数被覆盖，前面的定义永远不会生效
``unused-import``             minor      导入但未使用
``star-import``               minor      通配符导入污染命名空间
``none-comparison``           minor      用 == / != 比较 None
``broad-except``              minor      捕获过宽的 Exception
``global-statement``          minor      使用 global，副作用难以追踪
``open-without-with``         minor      文件未用 with 管理，异常时可能泄漏
``mutable-class-attr``        minor      类属性是可变对象，实例间共享
``range-len``                 info       建议改用 enumerate / zip
``lambda-assignment``         info      给 lambda 起名不如直接 def
``todo-comment``              info       遗留的 TODO/FIXME
``missing-docstring``         info       公共函数/类缺少文档字符串
``long-line``                 info       单行过长
============================  ==========  ==========================================
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import ToolExecutionError
from ..utils import read_text
from .base import Tool, ToolResult
from .fs import iter_files

MAX_FINDINGS = 40
MAX_FILES_PER_CALL = 20
LONG_LINE_THRESHOLD = 120
LONG_FUNCTION_THRESHOLD = 50
COMPLEXITY_THRESHOLD = 10
TOO_MANY_ARGS_THRESHOLD = 6

#: 判定为「决策点」的 AST 节点（决定圈复杂度）
_DECISION_TYPES: tuple[type, ...] = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.Assert, ast.IfExp,
)
_match_case = getattr(ast, "match_case", None)
if _match_case is not None:  # Python 3.10+
    _DECISION_TYPES += (_match_case,)
_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_SECRET_NAME_RE = re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)", re.I)
_SECRET_PLACEHOLDER_RE = re.compile(r"^(|none|null|xxx+|\*+|<.*>|change[_-]?me|your[_-]?\w*|placeholder|example.*)$", re.I)
_TODO_RE = re.compile(r"#.*\b(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.I)
_SAFE_PICKLE_HINT = ("safe_load", "json.loads")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    """一条静态分析结论。"""

    rule: str
    line: int
    severity: str
    category: str
    title: str
    detail: str
    suggestion: str
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FunctionInfo:
    """函数级指标。"""

    name: str
    line: int
    end_line: int
    length: int
    complexity: int
    args: int
    has_docstring: bool
    is_async: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisReport:
    """单个文件的完整分析结果，也是 ``analyze_code`` 的返回结构。"""

    path: str
    language: str = "python"
    syntax_ok: bool = True
    metrics: dict[str, Any] = field(default_factory=dict)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "syntax_ok": self.syntax_ok,
            "metrics": self.metrics,
            "functions": [f.to_dict() for f in self.functions],
            "classes": self.classes,
            "findings": [f.to_dict() for f in self.findings],
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #
def cyclomatic_complexity(node: ast.AST) -> int:
    """计算圈复杂度：1 + 决策点数量。

    不进入嵌套函数（嵌套函数会单独统计），避免重复计数。
    """
    complexity = 1
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _FUNCTION_TYPES) and child is not node:
                continue
            if isinstance(child, _DECISION_TYPES):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1 + len(child.ifs)
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            stack.append(child)
    return complexity


def _count_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = node.args
    return len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs) + (1 if args.vararg else 0) + (1 if args.kwarg else 0)


def _call_name(node: ast.AST) -> str:
    """把 ``os.path.join`` / ``eval`` 这类调用还原成点分名字。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _decorator_names(node: ast.ClassDef | ast.FunctionDef) -> list[str]:
    names = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        names.append(_call_name(target))
    return [n for n in names if n]


def _assign_parts(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    """把 ``Assign`` / ``AnnAssign`` 统一成 ``(目标列表, 右值)``。"""
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


def _is_mutable_literal(node: ast.AST) -> bool:
    """判断节点是否是「可变字面量」：``[]`` / ``{}`` / ``set()`` / ``list()`` 等。"""
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    return isinstance(node, ast.Call) and _call_name(node.func) in ("list", "dict", "set")


class PythonAnalyzer:
    """把一份 Python 源码转成结构化审查结论。"""

    def __init__(self, source: str, path: str) -> None:
        self.source = source
        self.path = path
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self.notes: list[str] = []
        self.functions: list[FunctionInfo] = []
        self.classes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def analyze(self) -> AnalysisReport:
        metrics: dict[str, Any] = self._line_metrics()
        try:
            tree = ast.parse(self.source, filename=self.path)
        except SyntaxError as exc:
            self.findings.append(
                Finding(
                    rule="syntax-error",
                    line=exc.lineno or 1,
                    severity="critical",
                    category="bug",
                    title="语法错误，文件无法导入",
                    detail=f"{exc.msg}（第 {exc.lineno} 行，第 {exc.offset} 列）。该文件在执行前就会抛 SyntaxError。",
                    suggestion="修复语法错误；建议在 CI 中加入 `python -m compileall` 或本工具做编译期拦截。",
                    code=self._line(exc.lineno or 1),
                )
            )
            metrics.update({"syntax_ok": False, "functions": 0, "classes": 0, "imports": 0,
                            "max_complexity": 0, "avg_complexity": 0.0, "max_function_length": 0,
                            "docstring_coverage": 0.0, "print_calls": 0})
            return AnalysisReport(path=self.path, syntax_ok=False, metrics=metrics,
                                  findings=self.findings, notes=["语法错误，已跳过其余规则。"])
        self.tree = tree
        self.parents = self._build_parents(tree)

        self._collect_structure(tree)
        self._check_imports(tree)
        self._check_calls(tree)
        self._check_assignments(tree)
        self._check_comparisons(tree)
        self._check_exceptions(tree)
        self._check_quality(tree)

        metrics.update(self._compute_metrics())
        self.findings.sort(key=lambda f: ({"critical": 0, "major": 1, "minor": 2, "info": 3}[f.severity], f.line))
        return AnalysisReport(path=self.path, metrics=metrics, functions=self.functions,
                              classes=self.classes, findings=self.findings[:MAX_FINDINGS],
                              notes=self.notes)

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        return parents

    def _line(self, number: int) -> str:
        if 1 <= number <= len(self.lines):
            return self.lines[number - 1].strip()[:200]
        return ""

    def _segment(self, node: ast.AST) -> str:
        """取代码片段（最多 3 行），给报告做证据。"""
        start = getattr(node, "lineno", 1)
        end = min(getattr(node, "end_lineno", start) or start, start + 2)
        return "\n".join(self._line(n) for n in range(start, end + 1))

    def _add(self, **kwargs: Any) -> None:
        if len(self.findings) < MAX_FINDINGS * 3:  # 内部软上限，最终再截断
            self.findings.append(Finding(**kwargs))

    def _line_metrics(self) -> dict[str, Any]:
        blank = comment = code = 0
        in_docstring = False
        for line in self.lines:
            stripped = line.strip()
            if not stripped:
                blank += 1
                continue
            if stripped.startswith("#"):
                comment += 1
                continue
            # 粗略识别多行字符串（文档字符串）块
            quotes = stripped.count('"""') + stripped.count("'''")
            if in_docstring:
                comment += 1
                if quotes % 2 == 1:
                    in_docstring = False
                continue
            if quotes % 2 == 1 and (stripped.startswith(('"""', "'''", 'r"""', "r'''"))):
                in_docstring = True
                comment += 1
                continue
            code += 1
        return {
            "total_lines": len(self.lines),
            "code_lines": code,
            "comment_lines": comment,
            "blank_lines": blank,
            "syntax_ok": True,
        }

    # ------------------------------------------------------------------ #
    # 结构收集 + 复杂度
    # ------------------------------------------------------------------ #
    def _collect_structure(self, tree: ast.Module) -> None:
        """遍历 AST 收集函数/类结构，并触发「结构类」规则。

        这个函数原先是一个 100 多行、圈复杂度 33 的巨型分支块——
        正是本工具在自审查里给出的「提取方法」建议。现在拆成
        :meth:`_visit_function` / :meth:`_visit_class` /
        :meth:`_check_function_metrics` / :meth:`_check_mutable_defaults`
        等小方法，每个方法只负责一件事。
        """
        self._module_functions: dict[str, int] = {}
        self._doc_targets = 0
        self._doc_hits = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_function(node, tree)
            elif isinstance(node, ast.ClassDef):
                self._visit_class(node)
        self._report_missing_docstrings()

    # ---- 函数 ---------------------------------------------------------- #
    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.Module) -> None:
        info = FunctionInfo(
            name=node.name,
            line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
            length=(getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1,
            complexity=cyclomatic_complexity(node),
            args=_count_args(node),
            has_docstring=ast.get_docstring(node) is not None,
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        self.functions.append(info)
        self._count_docstring(node.name, info.has_docstring)
        self._check_redefinition(node, tree)
        self._check_function_metrics(node, info)
        self._check_mutable_defaults(node)

    def _count_docstring(self, name: str, has_docstring: bool) -> None:
        """统计公共 API 的文档覆盖率（下划线开头视为内部实现）。"""
        if name.startswith("_"):
            return
        self._doc_targets += 1
        self._doc_hits += 1 if has_docstring else 0

    def _check_redefinition(self, node: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.Module) -> None:
        """模块级同名函数会被后者覆盖，前者永远不会生效。"""
        if self.parents.get(node) is not tree:
            return
        previous = self._module_functions.get(node.name)
        if previous is None:
            self._module_functions[node.name] = node.lineno
            return
        self._add(
            rule="redefined-function", line=node.lineno, severity="major", category="bug",
            title=f"函数 `{node.name}` 被重复定义",
            detail=f"模块级函数 `{node.name}` 在第 {previous} 行已定义，"
                   "此处覆盖了前一个定义，先前的实现永远不会被调用。",
            suggestion="删除或重命名其中一个定义；若是有意重载，请改用不同的函数名。",
            code=self._segment(node),
        )

    def _check_function_metrics(self, node: ast.FunctionDef | ast.AsyncFunctionDef, info: FunctionInfo) -> None:
        """复杂度 / 长度 / 参数个数三条「度量类」规则。"""
        if info.complexity > COMPLEXITY_THRESHOLD:
            self._add(
                rule="high-complexity", line=node.lineno, severity="major", category="maintainability",
                title=f"`{node.name}` 圈复杂度过高（{info.complexity}）",
                detail=f"圈复杂度 {info.complexity}，超过阈值 {COMPLEXITY_THRESHOLD}。"
                       "分支越多，路径覆盖测试的成本呈指数上升，遗漏分支的概率也随之增加。",
                suggestion="用「提取方法」把独立分支拆成小函数，或用早返回（early return）替代多层嵌套；"
                           "目标是把每个函数压到 10 以下。",
                code=self._segment(node),
            )
        if info.length > LONG_FUNCTION_THRESHOLD:
            self._add(
                rule="long-function", line=node.lineno, severity="minor", category="maintainability",
                title=f"`{node.name}` 过长（{info.length} 行）",
                detail=f"函数体 {info.length} 行，超过 {LONG_FUNCTION_THRESHOLD} 行的经验阈值，"
                       "通常意味着它承担了多个职责。",
                suggestion="按职责拆分为若干私有函数，每个函数只做一件事。",
                code=self._segment(node),
            )
        if info.args > TOO_MANY_ARGS_THRESHOLD:
            self._add(
                rule="too-many-args", line=node.lineno, severity="minor", category="maintainability",
                title=f"`{node.name}` 参数过多（{info.args} 个）",
                detail=f"共 {info.args} 个参数，调用方容易传错顺序，可读性差。",
                suggestion="把相关参数聚合成 dataclass/NamedTuple，或使用关键字参数并设置默认值。",
                code=self._segment(node),
            )

    def _check_mutable_defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """可变默认参数在「函数定义时」只求值一次，会被所有调用共享。"""
        defaults = list(node.args.defaults) + [item for item in node.args.kw_defaults if item is not None]
        for default in defaults:
            if not _is_mutable_literal(default):
                continue
            self._add(
                rule="mutable-default", line=getattr(default, "lineno", node.lineno),
                severity="major", category="bug",
                title=f"`{node.name}` 使用了可变默认参数",
                detail="默认值在函数定义时只求值一次，所有调用共享同一个对象："
                       "函数内对它的修改会「记住」到下一次调用，产生难以复现的串数据 Bug。",
                suggestion=f"改为 `def {node.name}(..., arg=None)`，并在函数体内 `if arg is None: arg = []`。",
                code=self._segment(node),
            )

    # ---- 类 ------------------------------------------------------------ #
    def _visit_class(self, node: ast.ClassDef) -> None:
        self.classes.append({
            "name": node.name,
            "line": node.lineno,
            "bases": [_call_name(base) for base in node.bases],
            "methods": sum(
                1 for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            "has_docstring": ast.get_docstring(node) is not None,
            "decorators": _decorator_names(node),
        })
        self._count_docstring(node.name, ast.get_docstring(node) is not None)
        self._check_class_attributes(node)

    def _check_class_attributes(self, node: ast.ClassDef) -> None:
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or not isinstance(statement.value, (ast.List, ast.Dict, ast.Set)):
                continue
            self._add(
                rule="mutable-class-attr", line=statement.lineno, severity="minor", category="bug",
                title=f"`{node.name}` 的类属性是可变对象",
                detail="类属性在所有实例间共享，通过实例修改它会影响其他实例（这是新手最容易踩的坑之一）。",
                suggestion="改为在 `__init__` 中初始化实例属性；确实需要共享时使用不可变类型或显式声明。",
                code=self._segment(statement),
            )

    def _report_missing_docstrings(self) -> None:
        """文档覆盖率写进 notes；缺少文档的公共函数只提示一次，避免刷屏。"""
        if not self._doc_targets:
            return
        self.notes.append(f"公共 API 文档字符串覆盖率 {self._doc_hits}/{self._doc_targets}")
        for function in self.functions:
            if function.name.startswith("_") or function.has_docstring or function.length < 5:
                continue
            self._add(
                rule="missing-docstring", line=function.line, severity="info", category="style",
                title=f"`{function.name}` 缺少文档字符串",
                detail="公共函数没有说明用途、参数与返回值，调用方只能读实现。",
                suggestion="补一行 docstring 说明用途；参数复杂时按 Google/NumPy 风格补充参数说明。",
            )
            break

    # ------------------------------------------------------------------ #
    # 导入检查
    # ------------------------------------------------------------------ #
    def _check_imports(self, tree: ast.Module) -> None:
        """检查未使用的导入与通配符导入。

        同样按自审查建议做了拆分：收集与判定分离，每个辅助函数只做一件事。
        """
        imported, star_lines = self._collect_imports(tree)
        used = self._collect_used_names(tree) | self._collect_exported_names(tree)

        for name, (line, source_text) in sorted(imported.items(), key=lambda item: item[1][0]):
            if name in used:
                continue
            self._add(
                rule="unused-import", line=line, severity="minor", category="maintainability",
                title=f"未使用的导入 `{name}`",
                detail=f"`{source_text}` 在本文件中没有被引用，属于死代码，会让读者误以为存在依赖。",
                suggestion="删除该导入；若属于包的对外导出，请把它加入 `__all__`。",
                code=self._line(line),
            )

        for line in star_lines:
            self._add(
                rule="star-import", line=line, severity="minor", category="maintainability",
                title="使用了通配符导入 `from ... import *`",
                detail="通配符导入会污染命名空间，覆盖同名变量，且让静态分析无法判断名字来源。",
                suggestion="改为显式导入需要使用的名字。",
                code=self._line(line),
            )

    @staticmethod
    def _collect_imports(tree: ast.Module) -> tuple[dict[str, tuple[int, str]], list[int]]:
        """收集 ``{绑定名: (行号, 原始语句)}`` 与通配符导入所在行。"""
        imported: dict[str, tuple[int, str]] = {}
        star_lines: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = (alias.asname or alias.name).split(".")[0]
                    suffix = f" as {alias.asname}" if alias.asname else ""
                    imported[bound] = (node.lineno, f"import {alias.name}{suffix}")
            elif isinstance(node, ast.ImportFrom):
                # `from __future__ import annotations` 是编译器指令，永远不算「未使用」
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        star_lines.append(node.lineno)
                        continue
                    bound = alias.asname or alias.name
                    suffix = f" as {alias.asname}" if alias.asname else ""
                    imported[bound] = (node.lineno, f"from {node.module} import {alias.name}{suffix}")
        return imported, star_lines

    @staticmethod
    def _collect_used_names(tree: ast.Module) -> set[str]:
        """收集文件里出现过的所有名字（属性访问取其根名字）。"""
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                root: ast.AST = node
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    used.add(root.id)
        return used

    @staticmethod
    def _collect_exported_names(tree: ast.Module) -> set[str]:
        """``__all__`` 中列出的名字视为已使用（包对外接口的常见写法）。"""
        exported: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(target, "id", "") == "__all__" for target in node.targets):
                continue
            for element in getattr(node.value, "elts", []):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    exported.add(element.value)
        return exported

    # ------------------------------------------------------------------ #
    # 调用检查（安全）
    # ------------------------------------------------------------------ #
    def _check_calls(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name in ("eval", "exec"):
                self._add(
                    rule="eval-exec", line=node.lineno, severity="critical", category="security",
                    title=f"使用 `{name}()` 执行动态代码",
                    detail=f"`{name}()` 会把字符串当代码执行。一旦字符串里混入用户输入，"
                           "攻击者即可执行任意代码（RCE），这是最典型的注入漏洞。",
                    suggestion="改用 `ast.literal_eval()` 解析字面量，或使用受限的解析器/规则表替代动态求值。",
                    code=self._segment(node),
                )
            elif name in ("os.system", "os.popen"):
                self._add(
                    rule="os-system", line=node.lineno, severity="major", category="security",
                    title=f"使用 `{name}()` 执行系统命令",
                    detail="命令字符串会被 shell 解释，拼接用户输入时会产生命令注入。",
                    suggestion="改用 `subprocess.run([...], shell=False)` 并以列表传递参数，杜绝 shell 解析。",
                    code=self._segment(node),
                )
            elif name in ("subprocess.run", "subprocess.call", "subprocess.Popen",
                          "subprocess.check_output", "subprocess.check_call"):
                for keyword in node.keywords:
                    if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        self._add(
                            rule="subprocess-shell", line=node.lineno, severity="major", category="security",
                            title="`subprocess` 使用了 `shell=True`",
                            detail="`shell=True` 会经由系统 shell 解析命令，拼接外部输入时存在命令注入风险；"
                                   "在 Windows 上还会带来引号与路径转义的额外坑。",
                            suggestion="去掉 `shell=True`，把命令与参数写成列表传入。",
                            code=self._segment(node),
                        )
            elif name in ("pickle.loads", "pickle.load", "marshal.loads", "dill.loads"):
                self._add(
                    rule="pickle-loads", line=node.lineno, severity="major", category="security",
                    title=f"反序列化不可信数据（`{name}`）",
                    detail="pickle 反序列化过程会执行任意 `__reduce__`，等价于执行任意代码；"
                           "只要数据源可被污染，这就是一个 RCE 入口。",
                    suggestion="改用 `json.loads` 等纯数据格式；确需 pickle 时请加签名校验并限制数据来源。",
                    code=self._segment(node),
                )
            elif name in ("yaml.load", "yaml.unsafe_load"):
                has_loader = any(keyword.arg == "Loader" for keyword in node.keywords)
                if not has_loader:
                    self._add(
                        rule="yaml-load-unsafe", line=node.lineno, severity="major", category="security",
                        title="`yaml.load` 未指定 Loader",
                        detail="旧版 PyYAML 的 `yaml.load` 默认使用不安全 Loader，可被构造成任意对象实例化。",
                        suggestion="改用 `yaml.safe_load(...)`，或显式传入 `Loader=yaml.SafeLoader`。",
                        code=self._segment(node),
                    )
            elif name == "open" and not self._inside_with(node):
                self._add(
                    rule="open-without-with", line=node.lineno, severity="minor", category="bug",
                    title="`open()` 未使用 `with` 管理",
                    detail="手动 open 时若中途抛异常，文件句柄不会关闭，长期运行会耗尽文件描述符"
                           "（此处为启发式判断，若已在 with 语句内则忽略）。",
                    suggestion="改为 `with open(...) as fh:`，由上下文管理器保证关闭。",
                    code=self._segment(node),
                )
            elif name == "range":
                if len(node.args) == 1 and isinstance(node.args[0], ast.Call) and _call_name(node.args[0].func) == "len":
                    self._add(
                        rule="range-len", line=node.lineno, severity="info", category="style",
                        title="`for i in range(len(x))` 写法不够 Pythonic",
                        detail="需要索引时可读性更好的写法是 `enumerate`；只需元素时直接迭代即可。",
                        suggestion="改为 `for i, item in enumerate(x):`。",
                        code=self._segment(node),
                    )

    def _inside_with(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                return True
            current = self.parents.get(current)
        return False

    # ------------------------------------------------------------------ #
    # 赋值检查（硬编码密钥）
    # ------------------------------------------------------------------ #
    def _check_assignments(self, tree: ast.Module) -> None:
        """检查赋值语句：硬编码凭据、lambda 赋值。"""
        for node in ast.walk(tree):
            targets, value = _assign_parts(node)
            if not targets or value is None:
                continue

            # ---- 规则 1：硬编码口令/密钥 ----
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literal = value.value.strip()
                looks_like_secret = len(literal) >= 6 and not _SECRET_PLACEHOLDER_RE.match(literal)
                if looks_like_secret:
                    for target in targets:
                        name = _call_name(target)
                        if not name or not _SECRET_NAME_RE.search(name):
                            continue
                        self._add(
                            rule="hardcoded-secret", line=node.lineno, severity="major", category="security",
                            title=f"疑似硬编码凭据：`{name}`",
                            detail="口令/密钥写进源码后，会随 Git 历史永久留存，任何拿到仓库的人都能直接使用；"
                                   "轮换密钥的成本也远高于一开始就外置。",
                            suggestion="改为从环境变量或配置中心读取（如 `os.environ[\"APP_TOKEN\"]`），"
                                       "并把 `.env` 加入 `.gitignore`；已泄漏的密钥请立即轮换。",
                            # 注意：证据里**不回显明文**，避免报告本身泄漏凭据
                            code=f'{name} = "***"  # 第 {node.lineno} 行，字面量已隐去',
                        )

            # ---- 规则 2：把 lambda 赋值给变量 ----
            if isinstance(value, ast.Lambda):
                for target in targets:
                    name = _call_name(target)
                    if not name:
                        continue
                    self._add(
                        rule="lambda-assignment", line=node.lineno, severity="info", category="style",
                        title=f"把 lambda 赋值给 `{name}`",
                        detail="lambda 无法写文档字符串、无法被 traceback 正确命名，调试体验差。",
                        suggestion=f"改为 `def {name}(...):` 定义普通函数。",
                        code=self._segment(node),
                    )

    # ------------------------------------------------------------------ #
    # 比较 / 异常 / 全局
    # ------------------------------------------------------------------ #
    def _check_comparisons(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant) and comparator.value is None:
                    operator = "==" if isinstance(op, ast.Eq) else "!="
                    self._add(
                        rule="none-comparison", line=node.lineno, severity="minor", category="style",
                        title=f"用 `{operator}` 比较 `None`",
                        detail="`None` 是单例，应该用身份比较。自定义 `__eq__` 的类型可能在 `==` 上返回意外结果。",
                        suggestion=f"改为 `is None` / `is not None`。",
                        code=self._line(node.lineno),
                    )

    def _check_exceptions(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                self._add(
                    rule="bare-except", line=node.lineno, severity="major", category="bug",
                    title="裸 `except:` 捕获所有异常",
                    detail="裸 except 会连 `KeyboardInterrupt`（Ctrl+C）和 `SystemExit` 一起吞掉，"
                           "导致程序无法正常退出，真实故障被掩盖。",
                    suggestion="明确捕获预期异常类型（如 `except ValueError:`）；确实要兜底时用 `except Exception:` 并记录日志。",
                    code=self._segment(node),
                )
            elif _call_name(node.type) == "Exception":
                self._add(
                    rule="broad-except", line=node.lineno, severity="minor", category="maintainability",
                    title="捕获过宽的 `Exception`",
                    detail="捕获所有 Exception 会把编码错误（如 TypeError、NameError）也当成业务异常处理掉。",
                    suggestion="缩小到具体异常类型；如需兜底，请在日志中带上 `exc_info=True` 以便定位。",
                    code=self._segment(node),
                )
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                self._add(
                    rule="except-pass", line=node.lineno, severity="major", category="bug",
                    title="异常被静默忽略（`except ...: pass`）",
                    detail="出错时既不处理也不记录，问题会在很远的地方以更难排查的形式爆发。",
                    suggestion="至少记录日志（`logger.warning(..., exc_info=True)`）；确实可以忽略时，"
                               "请写注释说明为什么可以忽略。",
                    code=self._segment(node),
                )

    def _check_quality(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                self._add(
                    rule="global-statement", line=node.lineno, severity="minor", category="maintainability",
                    title=f"使用 `global` 修改全局变量 {', '.join(node.names)}",
                    detail="全局可变状态让调用顺序影响结果，单元测试之间会互相污染。",
                    suggestion="把状态封装进类或通过参数传递；需要单例时用显式的配置对象。",
                    code=self._line(node.lineno),
                )

        for number, line in enumerate(self.lines, start=1):
            if len(line) > LONG_LINE_THRESHOLD:
                self._add(
                    rule="long-line", line=number, severity="info", category="style",
                    title=f"第 {number} 行过长（{len(line)} 字符）",
                    detail=f"超过 {LONG_LINE_THRESHOLD} 字符，在分屏对比和代码评审时都需要横向滚动。",
                    suggestion="按括号换行或提取中间变量，让每行保持在 100-120 字符以内。",
                    code=line.strip()[:120],
                )
                break  # 只报第一处，避免刷屏

        for number, line in enumerate(self.lines, start=1):
            match = _TODO_RE.search(line)
            if match:
                self._add(
                    rule="todo-comment", line=number, severity="info", category="maintainability",
                    title=f"遗留 {match.group(1).upper()} 标记",
                    detail=f"待办事项：{match.group(2).strip() or '（未填写说明）'}。"
                           "长期滞留的 TODO 会变成无人认领的技术债。",
                    suggestion="登记到 issue 跟踪系统并注明负责人，或在本轮迭代内解决。",
                    code=line.strip()[:160],
                )

    # ------------------------------------------------------------------ #
    def _compute_metrics(self) -> dict[str, Any]:
        complexities = [f.complexity for f in self.functions] or [0]
        lengths = [f.length for f in self.functions] or [0]
        public = [f for f in self.functions if not f.name.startswith("_")]
        documented = [f for f in public if f.has_docstring]
        imports = sum(
            len(node.names)
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        )
        prints = sum(
            1 for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and _call_name(node.func) == "print"
        )
        return {
            "functions": len(self.functions),
            "classes": len(self.classes),
            "imports": imports,
            "print_calls": prints,
            "max_complexity": max(complexities),
            "avg_complexity": round(sum(complexities) / len(complexities), 2),
            "max_function_length": max(lengths),
            "docstring_coverage": round(len(documented) / len(public), 2) if public else 1.0,
        }


# --------------------------------------------------------------------------- #
# 工具封装
# --------------------------------------------------------------------------- #
class AnalyzeCodeTool(Tool):
    """对 Python 文件（或目录）做 AST 静态分析。"""

    name = "analyze_code"
    description = (
        "对 Python 源码做确定性的 AST 静态分析，返回代码度量（复杂度、函数长度、注释率）"
        "与问题清单（严重度/类别/行号/修改建议）。支持单个文件或整个目录。"
    )
    parameters = {
        "path": {"type": "string", "description": "要分析的 .py 文件或目录（相对工作区）"},
        "max_findings": {"type": "integer", "description": "最多返回多少条问题", "required": False, "default": MAX_FINDINGS, "min": 1, "max": 200},
    }

    def run(self, path: str, max_findings: int = MAX_FINDINGS) -> ToolResult:
        target = self.resolve_path(path)
        files: list[Path]
        if target.is_dir():
            files = list(iter_files(target, "*.py", limit=MAX_FILES_PER_CALL))
            if not files:
                return ToolResult.failure(f"目录 {self.display_path(target)} 下没有 .py 文件")
        else:
            if target.suffix.lower() != ".py":
                raise ToolExecutionError(
                    f"analyze_code 目前只支持 Python 文件（收到 {target.suffix or '无扩展名'}）；"
                    "其他语言请改用 read_file + search_code 进行人工审查"
                )
            files = [target]

        reports: list[AnalysisReport] = []
        for file in files:
            try:
                source = read_text(file)
            except OSError as exc:
                reports.append(AnalysisReport(path=self.display_path(file), notes=[f"读取失败：{exc}"]))
                continue
            report = PythonAnalyzer(source, self.display_path(file)).analyze()
            reports.append(report)

        merged_findings: list[dict[str, Any]] = []
        for report in reports:
            for finding in report.to_dict()["findings"]:
                # 每条问题都带上文件路径，这样降级报告与合并报告都能直接定位
                finding.setdefault("file", report.path)
                merged_findings.append(finding)
        severity_rank = {"critical": 0, "major": 1, "minor": 2, "info": 3}
        merged_findings.sort(key=lambda f: (severity_rank.get(f.get("severity", "minor"), 9), str(f.get("file", ""))))
        truncated_findings = len(merged_findings) > max_findings
        merged_findings = merged_findings[:max_findings]

        counts = {level: 0 for level in severity_rank}
        for finding in merged_findings:
            counts[finding.get("severity", "minor")] = counts.get(finding.get("severity", "minor"), 0) + 1

        if len(reports) == 1:
            payload: dict[str, Any] = reports[0].to_dict()
            payload["findings"] = merged_findings
        else:
            payload = {
                "path": self.display_path(target),
                "language": "python",
                "files_analyzed": [report.path for report in reports],
                "metrics": _aggregate_metrics(reports),
                "findings": merged_findings,
                "notes": [note for report in reports for note in report.notes],
            }
        payload["severity_counts"] = counts
        payload["findings_truncated"] = truncated_findings
        if truncated_findings:
            payload["notes"] = list(payload.get("notes", [])) + [
                f"问题超过 {max_findings} 条，已按严重度截断；请分文件重新调用以获取完整清单。"
            ]

        head = (
            f"已完成 {len(reports)} 个文件的静态分析："
            f"critical={counts['critical']}, major={counts['major']}, minor={counts['minor']}, info={counts['info']}"
            f"（语言：python，分析器：AST 规则引擎）"
        )
        text, trimmed = _render_within_budget(head, payload, self.config.max_observation_chars)
        return ToolResult.success(
            text,
            files=len(reports), findings=len(merged_findings),
            severity_counts=counts, path=self.display_path(target), trimmed=trimmed,
        )


def _aggregate_metrics(reports: Sequence[AnalysisReport]) -> dict[str, Any]:
    """把多文件指标汇总成目录级概览。"""
    if not reports:
        return {}
    totals = {"total_lines": 0, "code_lines": 0, "comment_lines": 0, "blank_lines": 0, "functions": 0, "classes": 0}
    for report in reports:
        for key in totals:
            totals[key] += int(report.metrics.get(key, 0) or 0)
    max_complexity = max((int(r.metrics.get("max_complexity", 0) or 0) for r in reports), default=0)
    totals["max_complexity"] = max_complexity
    totals["files"] = len(reports)
    return totals


#: 极限压缩时保留的字段（丢掉 detail/suggestion/code 这些长文本，保住「哪里有什么问题」）
_ABBREVIATED_FIELDS = ("file", "line", "severity", "category", "title", "rule")


def _abbreviate(finding: dict[str, Any]) -> dict[str, Any]:
    return {key: finding[key] for key in _ABBREVIATED_FIELDS if key in finding}


def _render_within_budget(head: str, payload: dict[str, Any], budget: int) -> tuple[str, bool]:
    """在观察预算内渲染分析结果，且**保证输出始终是合法 JSON**。

    为什么不直接 ``truncate(json.dumps(...))``：把 JSON 从中间截断会让调用方
    （无论是真实大模型还是离线 Mock）拿到半截文本、解析失败，从而**丢掉全部结论**——
    这正是本项目开发过程中真实踩到的坑。

    因此这里改为「分级降级」，牺牲顺序明确写死为：

    1. 可读性（缩进）；
    2. 函数级明细（functions / classes / notes）；
    3. 每条问题的长文本（detail / suggestion / code）；
    4. 代码度量（metrics）—— 它是锦上添花，不该挤掉问题清单；
    5. 问题条数（但**至少保留 1 条**）。

    只有在预算小到荒谬时，问题清单才会被完全放弃。
    """
    findings = list(payload.get("findings") or [])
    abbreviated = [_abbreviate(finding) for finding in findings]
    # 逐层瘦身：先去明细，再去度量
    without_details = {key: value for key, value in payload.items() if key not in ("functions", "classes", "notes")}
    lean = {key: value for key, value in without_details.items() if key != "metrics"}

    def render(body: dict[str, Any], indent: int | None = None) -> str:
        return head + "\n" + json.dumps(body, ensure_ascii=False, indent=indent)

    def with_meta(body: dict[str, Any], keep: int, drop_details: bool) -> dict[str, Any]:
        candidate = dict(body)
        source = abbreviated if drop_details else findings
        candidate["findings"] = source[:keep]
        if keep < len(findings):
            candidate["findings_truncated"] = True
            candidate["findings_total"] = len(findings)
            candidate["note"] = (
                f"为控制在 {budget} 字符的观察预算内，仅返回最严重的前 {keep} 条问题"
                + ("（已省略详细说明）" if drop_details else "")
                + "；如需完整清单，请分文件调用 analyze_code 或调大 CR_MAX_OBSERVATION_CHARS。"
            )
        return candidate

    # 信息量由全到简依次尝试；问题清单的优先级始终高于度量信息
    plans: list[tuple[dict[str, Any], bool, int | None]] = [
        (payload, False, 2),
        (payload, False, None),
        (without_details, False, None),
        (without_details, True, None),
        (lean, False, None),
        (lean, True, None),
    ]
    for body, drop_details, indent in plans:
        text = render(with_meta(body, len(findings), drop_details), indent)
        if len(text) <= budget:
            return text, drop_details

    # 仍然放不下：减少问题条数，但至少保留 1 条
    keep = len(findings)
    while keep > 1:
        keep = max(1, keep - max(1, keep // 3))
        text = render(with_meta(lean, keep, True))
        if len(text) <= budget:
            return text, True

    minimal = {
        "path": payload.get("path", ""),
        "language": payload.get("language", "python"),
        "syntax_ok": payload.get("syntax_ok", True),
        "metrics": payload.get("metrics", {}),
        "severity_counts": payload.get("severity_counts", {}),
        "findings": [],
        "findings_truncated": True,
        "findings_total": len(findings),
        "note": "观察预算过小，仅保留度量信息；请调大 CR_MAX_OBSERVATION_CHARS 或分文件调用。",
    }
    return render(minimal), True


__all__ = [
    "AnalyzeCodeTool",
    "PythonAnalyzer",
    "AnalysisReport",
    "Finding",
    "FunctionInfo",
    "cyclomatic_complexity",
]
