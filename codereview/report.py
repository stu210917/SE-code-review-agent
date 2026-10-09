"""审查报告的数据结构与渲染。

模型只负责产出「结论」；「结论如何变成一份可交付的报告」由这里决定。
这样做的好处是：即使模型某次输出的字段略有出入（少个 ``suggestion``、
``line`` 写成字符串），报告依然能稳定生成——**渲染层负责容错**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .utils import SEVERITY_ORDER, normalize_severity, severity_rank

SEVERITY_LABEL = {
    "critical": "严重",
    "major": "重要",
    "minor": "次要",
    "info": "提示",
}
SEVERITY_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}
CATEGORY_LABEL = {
    "bug": "缺陷",
    "security": "安全",
    "performance": "性能",
    "maintainability": "可维护性",
    "style": "风格",
    "test": "测试",
}


@dataclass
class Issue:
    """一条审查意见。"""

    title: str
    severity: str = "minor"
    file: str = ""
    line: int = 0
    category: str = "maintainability"
    detail: str = ""
    suggestion: str = ""
    rule: str = ""

    def __post_init__(self) -> None:
        self.severity = normalize_severity(self.severity)
        self.category = (self.category or "maintainability").strip().lower()
        if self.category not in CATEGORY_LABEL:
            self.category = "maintainability"
        try:
            self.line = int(self.line or 0)
        except (TypeError, ValueError):
            self.line = 0
        self.title = (self.title or "未命名问题").strip()
        self.file = (self.file or "").strip().replace("\\", "/")

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "（未指明位置）"

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "severity": self.severity,
            "severity_label": SEVERITY_LABEL[self.severity],
            "category": self.category,
            "category_label": CATEGORY_LABEL[self.category],
            "file": self.file,
            "line": self.line,
            "location": self.location,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "rule": self.rule,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Issue | None":
        """从模型输出构造 Issue；无法使用时返回 ``None``（而不是抛异常）。"""
        if not isinstance(payload, dict):
            return None
        title = payload.get("title") or payload.get("message") or payload.get("name")
        if not title:
            return None
        return cls(
            title=str(title),
            severity=str(payload.get("severity", "minor")),
            category=str(payload.get("category", "maintainability")),
            file=str(payload.get("file", "") or payload.get("path", "")),
            line=payload.get("line", 0),
            detail=str(payload.get("detail", "") or payload.get("description", "")),
            suggestion=str(payload.get("suggestion", "") or payload.get("fix", "")),
            rule=str(payload.get("rule", "")),
        )


@dataclass
class ReviewReport:
    """一次代码审查的完整结果。"""

    summary: str = ""
    score: int = 0
    issues: list[Issue] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    degraded_reason: str = ""

    # ------------------------------------------------------------------ #
    @classmethod
    def from_final_answer(cls, payload: Any, meta: dict[str, Any] | None = None) -> "ReviewReport":
        """从模型的 ``final_answer`` 构造报告，尽可能容错。"""
        meta = dict(meta or {})
        if not isinstance(payload, dict):
            return cls(
                summary="模型未按协议返回结构化结论，请查看附录中的执行轨迹。",
                issues=[], meta=meta, degraded=True,
                degraded_reason=f"final_answer 不是对象，而是 {type(payload).__name__}",
            )

        raw_issues = payload.get("issues")
        if isinstance(raw_issues, dict):
            raw_issues = [raw_issues]
        if not isinstance(raw_issues, list):
            raw_issues = []

        issues: list[Issue] = []
        for item in raw_issues:
            issue = Issue.from_dict(item)
            if issue is not None:
                issues.append(issue)
        issues.sort(key=lambda issue: (issue.rank, issue.file, issue.line))

        score = payload.get("score", 0)
        try:
            score = max(0, min(100, int(float(score))))
        except (TypeError, ValueError):
            score = 0

        return cls(
            summary=str(payload.get("summary", "") or "").strip(),
            score=score,
            issues=issues,
            highlights=_as_str_list(payload.get("highlights")),
            next_steps=_as_str_list(payload.get("next_steps")),
            meta=meta,
            degraded=not issues and not payload.get("summary"),
            degraded_reason="" if (issues or payload.get("summary")) else "模型返回的结论为空",
        )

    # ------------------------------------------------------------------ #
    @property
    def counts(self) -> dict[str, int]:
        counts = {level: 0 for level in SEVERITY_ORDER}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts

    @property
    def grade(self) -> str:
        if self.score >= 90:
            return "优秀"
        if self.score >= 80:
            return "良好"
        if self.score >= 70:
            return "中等"
        if self.score >= 60:
            return "及格"
        return "待改进"

    @property
    def files_reviewed(self) -> list[str]:
        return sorted({issue.file for issue in self.issues if issue.file}) or list(self.meta.get("files", []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "score": self.score,
            "grade": self.grade,
            "counts": self.counts,
            "issues": [issue.to_dict() for issue in self.issues],
            "highlights": self.highlights,
            "next_steps": self.next_steps,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "meta": self.meta,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    # ------------------------------------------------------------------ #
    def to_markdown(self) -> str:
        """渲染成可直接提交/粘贴的 Markdown 报告。"""
        counts = self.counts
        lines: list[str] = ["# 代码审查报告", ""]
        lines.append(self._meta_line())
        lines.append("")

        lines.append("## 一、总体评价")
        lines.append("")
        lines.append(self.summary or "（模型未给出总体评价）")
        lines.append("")
        lines.append(f"**综合评分：{self.score}/100（{self.grade}）**")
        lines.append("")
        if self.meta.get("note"):
            # 模型结论之外补充的条目必须显式说明，否则「概述里的数量」与
            # 「下面统计的数量」对不上，读者会以为报告自相矛盾。
            lines.append(f"> **统计口径说明**：{self.meta['note']}")
            lines.append("")

        lines.append("## 二、问题统计")
        lines.append("")
        lines.append("| 严重度 | 数量 |")
        lines.append("| --- | ---: |")
        for level in ("critical", "major", "minor", "info"):
            lines.append(f"| {SEVERITY_ICON[level]} {SEVERITY_LABEL[level]}（{level}） | {counts[level]} |")
        lines.append(f"| **合计** | **{len(self.issues)}** |")
        lines.append("")

        lines.append("## 三、问题清单")
        lines.append("")
        if not self.issues:
            lines.append("未发现需要记录的问题。")
            lines.append("")
        for index, issue in enumerate(self.issues, start=1):
            lines.append(
                f"### {index}. {SEVERITY_ICON[issue.severity]} [{SEVERITY_LABEL[issue.severity]}] {issue.title}"
            )
            lines.append("")
            lines.append(f"- **位置**：`{issue.location}`")
            lines.append(f"- **类别**：{CATEGORY_LABEL[issue.category]}（{issue.category}）")
            lines.append(f"- **严重度**：{SEVERITY_LABEL[issue.severity]}（{issue.severity}）")
            if issue.rule:
                lines.append(f"- **规则**：`{issue.rule}`")
            if issue.detail:
                lines.append(f"- **说明**：{issue.detail}")
            if issue.suggestion:
                lines.append(f"- **建议**：{issue.suggestion}")
            lines.append("")

        if self.highlights:
            lines.append("## 四、值得肯定的地方")
            lines.append("")
            lines.extend(f"- {item}" for item in self.highlights)
            lines.append("")

        if self.next_steps:
            lines.append("## 五、后续建议")
            lines.append("")
            lines.extend(f"{i}. {item}" for i, item in enumerate(self.next_steps, start=1))
            lines.append("")

        if self.degraded:
            lines.append("## 六、降级说明")
            lines.append("")
            lines.append(f"> 本次报告为**降级输出**：{self.degraded_reason}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(self._appendix())
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _meta_line(self) -> str:
        parts = []
        if self.meta.get("generated_at"):
            parts.append(f"生成时间：{self.meta['generated_at']}")
        if self.meta.get("model"):
            parts.append(f"模型：`{self.meta['model']}`")
        if self.meta.get("mode"):
            parts.append(f"模式：{self.meta['mode']}")
        if self.meta.get("targets"):
            targets = self.meta["targets"]
            shown = "、".join(f"`{t}`" for t in targets[:5])
            parts.append(f"审查目标：{shown}")
        return "> " + " ｜ ".join(parts) if parts else ""

    def _appendix(self) -> str:
        meta = self.meta
        lines = ["## 附录：执行轨迹", ""]
        lines.append(f"- 推理步数：{meta.get('steps', 0)}")
        lines.append(f"- 工具调用：{meta.get('tool_calls', 0)} 次"
                     + (f"（{', '.join(meta.get('tools_used', []))}）" if meta.get("tools_used") else ""))
        lines.append(f"- 耗时：{meta.get('duration_s', 0)} 秒")
        if meta.get("total_tokens"):
            lines.append(f"- Token 消耗：约 {meta['total_tokens']}")
        if meta.get("llm_attempts"):
            lines.append(f"- LLM 重试次数：{meta['llm_attempts']}")
        if meta.get("compressions"):
            lines.append(f"- 上下文压缩触发：{meta['compressions']} 次")
        steps = meta.get("trace") or []
        if steps:
            lines.append("")
            lines.append("| 步骤 | 动作 | 参数摘要 | 结果 |")
            lines.append("| ---: | --- | --- | --- |")
            for step in steps:
                lines.append(
                    f"| {step.get('step', '')} | `{step.get('action', '')}` | "
                    f"{_escape_table(str(step.get('arguments', ''))[:80])} | {_escape_table(str(step.get('result', ''))[:80])} |"
                )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
def _as_str_list(value: Any) -> list[str]:
    """把模型返回的各种形态（str / list / None）统一成非空字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _escape_table(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sort_issues(issues: Iterable[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda issue: (issue.rank, issue.file, issue.line))


__all__ = ["Issue", "ReviewReport", "SEVERITY_LABEL", "SEVERITY_ICON", "CATEGORY_LABEL", "timestamp", "sort_issues"]
