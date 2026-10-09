"""简单 Web 界面（仅用标准库 ``http.server``，不引入 Flask/FastAPI）。

为什么值得单独做
----------------
命令行适合「我自己用」，而评审演示更适合「打开浏览器就能看」。这里用
一个单页应用把 Agent 的**执行轨迹实时展示**出来：调用哪个工具、观察到什么、
在哪一步收敛——这正是 Agent 类项目最难向别人解释的部分。

接口
----
========================  ==========================================================
方法 / 路径                说明
========================  ==========================================================
``GET  /``                单页界面
``GET  /api/config``      当前配置（已脱敏）与工具清单
``POST /api/review``      创建审查任务，立即返回 ``job_id``（后台线程执行）
``GET  /api/job/<id>``    查询任务进度：事件流 + 最终报告
========================  ==========================================================

安全说明：默认只监听 ``127.0.0.1``，且所有文件访问都受工作区沙箱约束；
这是教学演示用的本地界面，**没有做鉴权**，请勿直接暴露到公网。
"""

from __future__ import annotations

import json
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .agent import AgentEvent, ReviewAgent
from .config import Config
from .tools.base import build_default_registry

MAX_BODY_BYTES = 64 * 1024


class JobStore:
    """内存中的任务表（进程退出即清空；教学场景够用）。"""

    def __init__(self, limit: int = 50) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._limit = limit

    def create(self, targets: list[str], focus: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._jobs) >= self._limit:
                # 简单的 FIFO 淘汰，避免长时间运行后内存膨胀
                for key in list(self._jobs)[: len(self._jobs) - self._limit + 1]:
                    self._jobs.pop(key, None)
            self._jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "targets": targets,
                "focus": focus,
                "events": [],
                "report": None,
                "error": "",
            }
        return job_id

    def add_event(self, job_id: str, event: AgentEvent) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["events"].append({"kind": event.kind, "step": event.step, **event.data})

    def finish(self, job_id: str, report: dict[str, Any] | None, error: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "error" if error else "done"
            job["report"] = report
            job["error"] = error

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            # 拷贝一份，避免序列化时被后台线程修改
            return json.loads(json.dumps(job, ensure_ascii=False, default=str))


def _run_job(config: Config, store: JobStore, job_id: str, targets: list[str], focus: str) -> None:
    """后台线程：跑一次审查，把事件写进任务表。"""
    try:
        agent = ReviewAgent(config, on_event=lambda event: store.add_event(job_id, event))
        result = agent.review(targets, focus=focus)
        store.finish(job_id, result.report.to_dict(), error=result.error)
    except Exception as exc:  # pragma: no cover - 后台线程必须兜住所有异常
        store.finish(job_id, None, error=f"{type(exc).__name__}: {exc}")


class ReviewRequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理。"""

    server_version = "CodeReviewAgent/1.0"

    # 由 :func:`serve` 注入
    config: Config
    store: JobStore

    # ------------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆写基类
        if getattr(self.config, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
        elif path == "/api/config":
            registry = build_default_registry(self.config)
            self._send_json({
                "config": self.config.describe(),
                "tools": [
                    {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                    for tool in registry
                ],
            })
        elif path.startswith("/api/job/"):
            job = self.store.get(path.rsplit("/", 1)[-1])
            if job is None:
                self._send_json({"error": "任务不存在"}, status=404)
            else:
                self._send_json(job)
        else:
            self._send_json({"error": "未找到该路径"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - 基类约定
        path = urlparse(self.path).path
        if path != "/api/review":
            self._send_json({"error": "未找到该路径"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json({"error": "请求体为空或过大"}, status=400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"请求体不是合法 JSON：{exc}"}, status=400)
            return

        targets = payload.get("targets") or ["."]
        if isinstance(targets, str):
            targets = [item for item in targets.replace(",", " ").split() if item]
        targets = [str(item) for item in targets][:20] or ["."]
        focus = str(payload.get("focus") or "")

        job_id = self.store.create(targets, focus)
        threading.Thread(
            target=_run_job, args=(self.config, self.store, job_id, targets, focus), daemon=True
        ).start()
        self._send_json({"job_id": job_id, "targets": targets})

    # ------------------------------------------------------------------ #
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(config: Config, *, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> None:
    """启动 Web 服务（阻塞直到 Ctrl+C）。"""
    handler = type("BoundHandler", (ReviewRequestHandler,), {"config": config, "store": JobStore()})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"Web 界面已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# 前端（单文件，无外部依赖）
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>代码审查 Agent</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --line:#334155; --text:#e2e8f0; --dim:#94a3b8;
          --accent:#38bdf8; --critical:#f87171; --major:#fbbf24; --minor:#22d3ee; --info:#94a3b8; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; line-height:1.6; }
  header { padding:20px 28px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:14px; flex-wrap:wrap; }
  h1 { font-size:19px; margin:0; }
  .badge { font-size:12px; padding:3px 10px; border-radius:999px; background:#0b3a53; color:var(--accent);
           border:1px solid #155e75; }
  main { display:grid; grid-template-columns: 1fr 1fr; gap:18px; padding:18px 28px 48px; align-items:start; }
  @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }
  .card h2 { font-size:14px; margin:0 0 12px; color:var(--dim); font-weight:600;
             text-transform:uppercase; letter-spacing:.06em; }
  label { display:block; font-size:13px; color:var(--dim); margin:10px 0 4px; }
  input[type=text] { width:100%; padding:9px 11px; border-radius:7px; border:1px solid var(--line);
                     background:#0b1220; color:var(--text); font-size:14px; }
  button { margin-top:14px; padding:10px 20px; border:0; border-radius:7px; background:var(--accent);
           color:#04212f; font-weight:700; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  #log { font-family: Consolas, "Cascadia Mono", monospace; font-size:12.5px; max-height:420px;
         overflow:auto; white-space:pre-wrap; word-break:break-word; }
  .ev { padding:3px 0; border-bottom:1px dashed #24344d; }
  .ev b { color:var(--accent); }
  .score { font-size:34px; font-weight:800; }
  .counts { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 4px; }
  .pill { font-size:12px; padding:3px 10px; border-radius:999px; border:1px solid var(--line); }
  .issue { border-left:3px solid var(--line); padding:8px 0 8px 12px; margin:12px 0; }
  .issue.critical { border-color:var(--critical); } .issue.major { border-color:var(--major); }
  .issue.minor { border-color:var(--minor); } .issue.info { border-color:var(--info); }
  .issue h3 { font-size:14.5px; margin:0 0 4px; }
  .loc { font-family:Consolas, monospace; font-size:12.5px; color:var(--accent); }
  .muted { color:var(--dim); font-size:13px; }
  .error { color:var(--critical); }
  pre { background:#0b1220; padding:12px; border-radius:8px; overflow:auto; font-size:12.5px; }
</style>
</head>
<body>
<header>
  <h1>🔍 代码审查 Agent</h1>
  <span class="badge" id="mode">加载中…</span>
  <span class="muted" id="modelInfo"></span>
</header>

<main>
  <section class="card">
    <h2>① 发起审查</h2>
    <label>审查目标（相对工作区，用空格或逗号分隔）</label>
    <input type="text" id="targets" value="." placeholder="例如 src/ main.py">
    <label>额外关注点（可选）</label>
    <input type="text" id="focus" placeholder="例如 重点看异常处理与资源泄漏">
    <button id="runBtn">开始审查</button>
    <p class="muted" id="hint">Agent 会先侦察仓库结构，再调用 AST 分析工具，最后给出结论。</p>
  </section>

  <section class="card">
    <h2>② 执行轨迹（实时）</h2>
    <div id="log"><span class="muted">等待开始…</span></div>
  </section>

  <section class="card" style="grid-column: 1 / -1;">
    <h2>③ 审查报告</h2>
    <div id="report"><span class="muted">尚未生成报告。</span></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let timer = null;

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

fetch("/api/config").then(r => r.json()).then(data => {
  $("mode").textContent = data.config.mode;
  $("modelInfo").textContent = data.config.model + " · 工作区 " + data.config.workspace
    + " · 工具 " + data.tools.map(t => t.name).join(", ");
}).catch(() => { $("mode").textContent = "无法读取配置"; });

function renderEvents(events) {
  const log = $("log");
  log.innerHTML = events.map(ev => {
    if (ev.kind === "thought_text") return `<div class="ev">💭 ${esc(ev.text)}</div>`;
    if (ev.kind === "action") return `<div class="ev"><b>步骤 ${ev.step}</b> 🔧 调用 <b>${esc(ev.tool)}</b> `
      + `<span class="muted">${esc(JSON.stringify(ev.arguments))}</span></div>`;
    if (ev.kind === "observation") return `<div class="ev">　　${ev.ok ? "✅" : "❌"} `
      + `<span class="muted">${esc(ev.summary)}</span></div>`;
    if (ev.kind === "retry") return `<div class="ev">　　⟳ 第 ${ev.attempt} 次重试（${ev.delay}s）：`
      + `<span class="error">${esc(ev.error)}</span></div>`;
    if (ev.kind === "correction") return `<div class="ev">　　⚠ 输出不合协议，请求自我纠正</div>`;
    if (ev.kind === "compressed") return `<div class="ev">　　⇩ 上下文压缩，折叠 ${ev.dropped} 条历史</div>`;
    if (ev.kind === "degrade") return `<div class="ev">　　⚠ <span class="error">降级交付：${esc(ev.reason)}</span></div>`;
    if (ev.kind === "start") return `<div class="ev">▶ 可用工具：${esc((ev.tools || []).join(", "))}</div>`;
    if (ev.kind === "final") return `<div class="ev">■ 收敛：${ev.issues} 条问题，评分 ${ev.score}</div>`;
    return "";
  }).join("");
  log.scrollTop = log.scrollHeight;
}

function renderReport(report) {
  if (!report) { $("report").innerHTML = '<span class="muted">没有报告。</span>'; return; }
  const c = report.counts || {};
  const issues = (report.issues || []).map((issue, i) => `
    <div class="issue ${esc(issue.severity)}">
      <h3>${i + 1}. [${esc(issue.severity_label)}] ${esc(issue.title)}</h3>
      <div class="loc">${esc(issue.location)} · ${esc(issue.category_label)}</div>
      ${issue.detail ? `<div>${esc(issue.detail)}</div>` : ""}
      ${issue.suggestion ? `<div class="muted">建议：${esc(issue.suggestion)}</div>` : ""}
    </div>`).join("");
  $("report").innerHTML = `
    <div class="score">${report.score}<span class="muted" style="font-size:15px">/100 · ${esc(report.grade)}</span></div>
    <div class="counts">
      <span class="pill" style="color:var(--critical)">严重 ${c.critical || 0}</span>
      <span class="pill" style="color:var(--major)">重要 ${c.major || 0}</span>
      <span class="pill" style="color:var(--minor)">次要 ${c.minor || 0}</span>
      <span class="pill" style="color:var(--info)">提示 ${c.info || 0}</span>
      <span class="pill">步数 ${report.meta.steps} · 工具 ${report.meta.tool_calls} · ${report.meta.duration_s}s</span>
    </div>
    <p>${esc(report.summary)}</p>
    ${report.meta && report.meta.note ? `<p class="muted">统计口径说明：${esc(report.meta.note)}</p>` : ""}
    ${report.degraded ? `<p class="error">降级报告：${esc(report.degraded_reason)}</p>` : ""}
    ${issues || '<p class="muted">未发现需要记录的问题。</p>'}
    <details><summary class="muted">查看 Markdown / JSON</summary>
      <pre>${esc(JSON.stringify(report, null, 2))}</pre></details>`;
}

function poll(jobId) {
  fetch("/api/job/" + jobId).then(r => r.json()).then(job => {
    renderEvents(job.events || []);
    if (job.status === "running") { timer = setTimeout(() => poll(jobId), 600); return; }
    $("runBtn").disabled = false;
    if (job.status === "error") { $("report").innerHTML = `<p class="error">运行失败：${esc(job.error)}</p>`; return; }
    renderReport(job.report);
  }).catch(err => {
    $("runBtn").disabled = false;
    $("report").innerHTML = `<p class="error">查询任务失败：${esc(err)}</p>`;
  });
}

$("runBtn").onclick = () => {
  if (timer) clearTimeout(timer);
  const targets = $("targets").value.split(/[\\s,]+/).filter(Boolean);
  const focus = $("focus").value;
  $("runBtn").disabled = true;
  $("log").innerHTML = '<span class="muted">正在启动…</span>';
  $("report").innerHTML = '<span class="muted">审查中，请稍候…</span>';
  fetch("/api/review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets: targets.length ? targets : ["."], focus })
  }).then(r => r.json()).then(data => {
    if (data.error) { $("runBtn").disabled = false; $("report").innerHTML = `<p class="error">${esc(data.error)}</p>`; return; }
    poll(data.job_id);
  }).catch(err => {
    $("runBtn").disabled = false;
    $("report").innerHTML = `<p class="error">请求失败：${esc(err)}</p>`;
  });
};

$("targets").addEventListener("keydown", e => { if (e.key === "Enter") $("runBtn").click(); });
</script>
</body>
</html>
"""

__all__ = ["serve", "JobStore", "ReviewRequestHandler", "INDEX_HTML"]
