"""命令行界面：``review`` / ``chat`` / ``tools`` / ``web`` 四个子命令。

设计原则
--------
* **CI 友好**：``--json`` 输出机器可读结果，``--fail-on`` 让流水线在发现严重问题时失败；
* **实时反馈**：Agent 的每一步（思考/调用工具/观察）都会流式打印，
  而不是等全部跑完才吐结果——这是 Agent 类应用可观测性的基本要求；
* **优雅降级**：任何 :class:`CodeReviewError` 都会被翻译成人类可读的提示与退出码，
  不打印 Python 堆栈吓唬用户（``--debug`` 时才打印）。

退出码约定
----------
====  ==========================================
码值   含义
====  ==========================================
0     审查完成且未触发 ``--fail-on`` 阈值
1     运行期错误（LLM 不可用、内部异常等）
2     配置错误（参数非法、工作区不存在）
3     发现了达到 ``--fail-on`` 阈值的问题（供 CI 使用）
4     未收敛但已降级交付（如达到 --max-steps），报告仍可用
130   用户中断（Ctrl+C）
====  ==========================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .agent import AgentEvent, ReviewAgent
from .config import Config
from .errors import CodeReviewError, ConfigError
from .report import SEVERITY_LABEL, ReviewReport
from .tools.base import build_default_registry
from .utils import enable_utf8_output, strip_ansi

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_THRESHOLD = 3
EXIT_DEGRADED = 4
EXIT_INTERRUPTED = 130

SEVERITY_LEVELS = ("critical", "major", "minor", "info")


# --------------------------------------------------------------------------- #
# 终端着色
# --------------------------------------------------------------------------- #
class Style:
    """极简 ANSI 着色器；管道/重定向时自动关闭颜色。"""

    CODES = {
        "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36", "grey": "90",
    }

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __getattr__(self, name: str) -> Callable[[str], str]:
        if name.startswith("_"):  # 避免干扰 copy/pickle 等协议的属性探测
            raise AttributeError(name)
        code = self.CODES.get(name)

        def wrapper(text: str) -> str:
            if not self.enabled or code is None:
                return str(text)
            return f"\x1b[{code}m{text}\x1b[0m"

        return wrapper


def make_style(config: Config) -> Style:
    enabled = bool(config.color) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    return Style(enabled)


SEVERITY_STYLE = {"critical": "red", "major": "yellow", "minor": "cyan", "info": "grey"}


# --------------------------------------------------------------------------- #
# 事件渲染
# --------------------------------------------------------------------------- #
class ConsoleReporter:
    """把 AgentEvent 渲染成终端上的实时进度。"""

    def __init__(self, style: Style, *, verbose: bool = False, stream: Any = None) -> None:
        self.style = style
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.started = time.perf_counter()
        self.tool_calls = 0
        self.retries = 0

    def _print(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def __call__(self, event: AgentEvent) -> None:
        style = self.style
        step = f"{event.step:02d}" if event.step else "--"
        if event.kind == "start":
            tools = ", ".join(event.data.get("tools", []))
            self._print(f"{style.dim('可用工具')}: {tools}")
            self._print(style.dim(f"上下文预算约 {event.data.get('prompt_tokens', 0)} tokens，开始推理…\n"))
        elif event.kind == "thought_text":
            self._print(f"  {style.cyan('思考')}  {event.data.get('text', '')}")
        elif event.kind == "action":
            self.tool_calls += 1
            arguments = json.dumps(event.data.get("arguments", {}), ensure_ascii=False)
            self._print(f"{style.bold('步骤 ' + step)} {style.magenta('调用工具')} {event.data.get('tool')} "
                        f"{style.dim(arguments[:160])}")
        elif event.kind == "observation":
            mark = style.green("✓") if event.data.get("ok") else style.red("✗")
            self._print(f"        {mark} {style.dim(str(event.data.get('summary', ''))[:180])}")
            if self.verbose and event.data.get("output"):
                for line in str(event.data["output"]).splitlines()[:20]:
                    self._print(style.dim(f"        │ {line}"))
        elif event.kind == "retry":
            self.retries += 1
            self._print(style.yellow(f"        ⟳ 第 {event.data.get('attempt')} 次重试"
                                     f"（{event.data.get('delay')}s 后）：{event.data.get('error', '')[:120]}"))
        elif event.kind == "correction":
            self._print(style.yellow(f"        ⚠ 模型输出不合协议，已请求自我纠正：{event.data.get('error', '')[:120]}"))
        elif event.kind == "loop_detected":
            self._print(style.yellow(f"        ⚠ 检测到重复调用 `{event.data.get('tool')}`，已提示模型换策略"))
        elif event.kind == "compressed":
            self._print(style.dim(f"        ⇩ 上下文压缩：折叠 {event.data.get('dropped')} 条历史，"
                                  f"当前约 {event.data.get('prompt_tokens')} tokens"))
        elif event.kind == "degrade":
            self._print(style.yellow(f"\n⚠ 降级交付：{event.data.get('reason')}"))
        elif event.kind == "final" and self.verbose:
            self._print(style.dim(f"        → 收敛：{event.data.get('issues')} 条问题，评分 {event.data.get('score')}"))


def print_banner(config: Config, style: Style, stream: Any = None) -> None:
    stream = stream or sys.stdout
    info = config.describe()
    print(style.bold(f"代码审查 Agent v{__version__}"), file=stream)
    print(f"  模型    : {info['model']}  ({info['mode']})", file=stream)
    print(f"  接口    : {info['base_url']}", file=stream)
    print(f"  密钥    : {info['api_key']}", file=stream)
    print(f"  工作区  : {info['workspace']}", file=stream)
    print(f"  步数上限: {info['max_steps']}    上下文预算: {info['memory_token_budget']} tokens"
          f"    代码执行: {'开启' if info['allow_exec'] else '关闭'}", file=stream)
    if config.effective_mock:
        print(style.yellow("  提示    : 未检测到 API Key，已启用离线 Mock 模式（结论来自 AST 静态分析规则引擎）"),
              file=stream)
    print(file=stream)


def print_report(report: ReviewReport, style: Style) -> None:
    counts = report.counts
    print()
    print(style.bold("─" * 72))
    print(style.bold(f"审查结果：{report.score}/100（{report.grade}）")
          + style.dim(f"    耗时 {report.meta.get('duration_s', 0)}s"
                      f"    步数 {report.meta.get('steps', 0)}"
                      f"    工具调用 {report.meta.get('tool_calls', 0)}"))
    print(style.bold("─" * 72))
    if report.summary:
        print(report.summary)
        print()
    summary_line = "  ".join(
        getattr(style, SEVERITY_STYLE[level])(f"{SEVERITY_LABEL[level]} {counts[level]}") for level in SEVERITY_LEVELS
    )
    print(f"问题统计：{summary_line}")
    print()

    for index, issue in enumerate(report.issues, start=1):
        colorize = getattr(style, SEVERITY_STYLE[issue.severity])
        print(f"{index:>2}. {colorize(f'[{issue.severity.upper()}]')} {style.bold(issue.title)}")
        print(f"    {style.dim('位置')} {issue.location}   {style.dim('类别')} {issue.category}")
        if issue.detail:
            for line in _wrap(issue.detail, 88):
                print(f"    {line}")
        if issue.suggestion:
            for line in _wrap(f"建议：{issue.suggestion}", 88):
                print(f"    {style.green(line)}")
        print()

    if report.highlights:
        print(style.bold("值得肯定"))
        for item in report.highlights:
            print(f"  + {item}")
        print()
    if report.next_steps:
        print(style.bold("后续建议"))
        for index, item in enumerate(report.next_steps, start=1):
            print(f"  {index}. {item}")
        print()
    if report.degraded:
        print(style.yellow(f"注意：本次为降级报告 —— {report.degraded_reason}"))
        print()
    print(style.dim(f"完整 Markdown 报告可用 --out report.md 导出；共 {len(report.issues)} 条问题。"))


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行（中文按 2 列计），让终端输出整齐。"""
    from .utils import display_width

    lines: list[str] = []
    current = ""
    current_width = 0
    for char in text:
        char_width = 2 if display_width(char) == 2 else 1
        if current_width + char_width > width:
            lines.append(current)
            current, current_width = "", 0
        current += char
        current_width += char_width
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_review(args: argparse.Namespace) -> int:
    config = _build_config(args)
    style = make_style(config)

    # --json 且不落盘时，stdout 必须是**纯 JSON**（供管道/CI 直接消费），
    # 因此把横幅与进度这类「诊断信息」改写到 stderr。
    json_only = bool(args.json) and not args.out
    diagnostics = sys.stderr if json_only else sys.stdout

    print_banner(config, style, stream=diagnostics)
    reporter = ConsoleReporter(style, verbose=config.verbose, stream=diagnostics)
    agent = ReviewAgent(config, on_event=reporter)

    try:
        result = agent.review(
            args.targets,
            focus=args.focus or "",
            language=args.language,
            extra_instructions=args.instructions or "",
        )
    except KeyboardInterrupt:
        print("\n已中断。")
        return EXIT_INTERRUPTED

    report = result.report

    # 1) 终端展示
    if json_only:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_report(report, style)

    # 2) 落盘：按文件扩展名决定格式，显式 .md/.json 优先
    if args.out:
        suffix = Path(args.out).suffix.lower()
        use_json = suffix == ".json" or (args.json and suffix not in (".md", ".markdown"))
        content = report.to_json() if use_json else report.to_markdown()
        _write_output(args.out, content, style)

    if result.outcome == "error":
        print(style.red(f"运行错误：{result.error}"))
        return EXIT_RUNTIME_ERROR
    if result.outcome == "degraded":
        print(style.yellow(f"注意：本次未能完全收敛，报告为降级输出 —— {result.report.degraded_reason}"))
        return EXIT_DEGRADED
    return _apply_threshold(report, args.fail_on, style)


def cmd_chat(args: argparse.Namespace) -> int:
    """交互模式：多轮追问共享同一段上下文记忆。"""
    config = _build_config(args)
    style = make_style(config)
    print_banner(config, style)
    print(style.bold("交互模式") + "：直接描述你的要求，Agent 会保留上下文记忆。")
    print(style.dim("命令：:target <路径...> 切换审查目标 ｜ :report 查看上次报告 ｜ :reset 清空记忆 ｜ :exit 退出"))
    print()

    reporter = ConsoleReporter(style, verbose=config.verbose)
    agent = ReviewAgent(config, on_event=reporter)

    targets = list(args.targets or ["."])
    session = None
    last_report: ReviewReport | None = None
    print(f"当前审查目标：{', '.join(targets)}\n")

    while True:
        try:
            line = input(style.bold("审查> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return EXIT_OK
        if not line:
            continue
        if line in (":exit", ":quit", ":q"):
            return EXIT_OK
        if line == ":help":
            print(":target <路径...> 切换目标 ｜ :report 打印上次报告 ｜ :reset 清空记忆 ｜ :exit 退出")
            continue
        if line == ":report":
            if last_report:
                print_report(last_report, style)
            else:
                print("还没有生成过报告。")
            continue
        if line == ":reset":
            session = None
            last_report = None
            print("记忆已清空。")
            continue
        if line.startswith(":target"):
            parts = line.split()[1:]
            if parts:
                targets = parts
                print(f"审查目标已切换为：{', '.join(targets)}")
            else:
                print("用法：:target src/app.py src/utils.py")
            continue

        try:
            result = agent.review(targets, session=session, follow_up=line)
        except KeyboardInterrupt:
            print("\n已中断本轮。")
            continue
        session = result.memory
        last_report = result.report
        print_report(result.report, style)
        if result.error:
            print(style.red(f"运行错误：{result.error}"))


def cmd_tools(args: argparse.Namespace) -> int:
    config = _build_config(args)
    style = make_style(config)
    registry = build_default_registry(config)
    print(style.bold(f"已注册工具（{len(registry)} 个）"))
    print(registry.describe())
    if not config.allow_exec:
        print(style.dim("\n提示：run_code 属于危险工具，默认不注册；需要时加 --allow-exec 开启。"))
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    config = _build_config(args)
    style = make_style(config)
    print_banner(config, style)
    try:
        serve(config, host=args.host, port=args.port, open_browser=args.open)
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except OSError as exc:
        print(style.red(f"无法启动 Web 服务：{exc}"))
        return EXIT_RUNTIME_ERROR
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _write_output(path: str, content: str, style: Style) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 报告文件不带 ANSI 颜色码，便于粘贴到文档/issue
        target.write_text(strip_ansi(content), encoding="utf-8")
    except OSError as exc:
        print(style.red(f"写入 {path} 失败：{exc}"))
        return
    print(style.green(f"报告已写入 {target.resolve()}"))


def _apply_threshold(report: ReviewReport, threshold: str | None, style: Style) -> int:
    if not threshold or threshold == "none":
        return EXIT_OK
    counts = report.counts
    triggered = False
    if threshold == "critical":
        triggered = counts["critical"] > 0
    elif threshold == "major":
        triggered = counts["critical"] + counts["major"] > 0
    elif threshold == "minor":
        triggered = len(report.issues) > 0
    if triggered:
        print(style.red(f"发现达到 `{threshold}` 阈值的问题，按 --fail-on 约定返回退出码 {EXIT_THRESHOLD}。"))
        return EXIT_THRESHOLD
    return EXIT_OK


def _build_config(args: argparse.Namespace) -> Config:
    """把命令行参数转成 :class:`Config`（显式参数优先于环境变量）。"""
    overrides = {
        "model": getattr(args, "model", None),
        "base_url": getattr(args, "base_url", None),
        "api_key": getattr(args, "api_key", None),
        "max_steps": getattr(args, "max_steps", None),
        "temperature": getattr(args, "temperature", None),
        "timeout": getattr(args, "timeout", None),
        "max_retries": getattr(args, "max_retries", None),
        "workspace": Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None,
        "allow_exec": True if getattr(args, "allow_exec", False) else None,
        "verbose": True if getattr(args, "verbose", False) else None,
        "color": False if getattr(args, "no_color", False) else None,
    }
    if getattr(args, "mock", False):
        overrides["mock"] = True
    return Config.from_env(**overrides)


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _add_common_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("LLM 接入")
    group.add_argument("--mock", action="store_true",
                       help="强制使用离线 Mock 模式（不联网、不需要 API Key）")
    group.add_argument("--model", help="模型名，如 gpt-4o-mini / deepseek-chat / qwen-plus")
    group.add_argument("--base-url", dest="base_url", help="OpenAI 兼容接口地址，如 https://api.deepseek.com/v1")
    group.add_argument("--api-key", dest="api_key", help="API Key（也可用环境变量 CR_API_KEY / OPENAI_API_KEY）")
    group.add_argument("--temperature", type=float, help="采样温度，默认 0.2")

    group = parser.add_argument_group("Agent 行为")
    group.add_argument("--max-steps", dest="max_steps", type=int, help="最大推理步数，默认 12")
    group.add_argument("--timeout", type=float, help="单次 LLM 请求超时秒数，默认 60")
    group.add_argument("--max-retries", dest="max_retries", type=int, help="LLM 请求失败的重试次数，默认 3")
    group.add_argument("--workspace", help="工作区根目录（安全沙箱边界），默认当前目录")
    group.add_argument("--allow-exec", dest="allow_exec", action="store_true",
                       help="注册 run_code 工具（会真正执行代码，默认关闭）")
    group.add_argument("--verbose", "-v", action="store_true", help="输出工具返回的详细内容")
    group.add_argument("--no-color", dest="no_color", action="store_true", help="关闭彩色输出")
    group.add_argument("--debug", action="store_true", help="出错时打印完整 Python 堆栈")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codereview",
        description="代码审查 Agent —— 基于 ReAct 循环，自动调用 AST 静态分析与代码搜索工具完成审查。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python run.py review examples/                      # 审查整个目录
  python run.py review src/app.py --out report.md     # 导出 Markdown 报告
  python run.py review . --json --fail-on major       # CI：发现严重问题即失败
  python run.py review . --mock                       # 离线演示（无需 API Key）
  python run.py chat examples/buggy_sample.py         # 多轮交互
  python run.py web                                   # 启动 Web 界面
""",
    )
    parser.add_argument("--version", action="version", version=f"code-review-agent {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    review = subparsers.add_parser("review", help="审查文件或目录")
    review.add_argument("targets", nargs="+", help="要审查的文件/目录（相对工作区）")
    review.add_argument("--focus", help="额外关注点，如「重点看并发安全」")
    review.add_argument("--language", default="auto", help="目标语言提示，默认 auto")
    review.add_argument("--instructions", help="追加到系统提示词的额外要求")
    review.add_argument("--out", "-o", help="把报告写入文件（按扩展名自动选择 .md / .json）")
    review.add_argument("--json", action="store_true", help="直接输出 JSON（便于程序消费）")
    review.add_argument("--fail-on", dest="fail_on", choices=["critical", "major", "minor", "none"],
                        default="none", help="达到该阈值时返回退出码 3（CI 用）")
    _add_common_options(review)
    review.set_defaults(func=cmd_review)

    chat = subparsers.add_parser("chat", help="交互式多轮审查")
    chat.add_argument("targets", nargs="*", help="初始审查目标，默认当前目录")
    _add_common_options(chat)
    chat.set_defaults(func=cmd_chat)

    tools = subparsers.add_parser("tools", help="列出可用工具及其参数")
    _add_common_options(tools)
    tools.set_defaults(func=cmd_tools)

    web = subparsers.add_parser("web", help="启动简单 Web 界面")
    web.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    web.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    web.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    _add_common_options(web)
    web.set_defaults(func=cmd_web)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口，返回进程退出码。"""
    # 先切换输出编码：Windows 上管道/重定向默认走本地代码页，会把中文写坏
    enable_utf8_output()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args) or EXIT_OK)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except CodeReviewError as exc:
        print(f"运行错误：{exc}", file=sys.stderr)
        if getattr(args, "debug", False):
            raise
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # pragma: no cover - 兜底
        if getattr(args, "debug", False):
            raise
        print(f"未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("可加 --debug 查看完整堆栈。", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


__all__ = ["main", "build_parser", "Style", "ConsoleReporter", "print_report"]
