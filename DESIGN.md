# 代码审查 Agent —— 设计文档

> 项目：`code-review-agent` ｜ 版本 `codereview.__version__ = "1.0.0"`
> 运行环境：Python 3.9+，**零第三方依赖**（仅标准库）
> 配套文档：[README.md](README.md)（快速上手与使用说明） ｜ 样例报告见 [docs/example-report.md](docs/example-report.md) ｜ 自审查结果见 [docs/self-review.md](docs/self-review.md)

---

## 一、项目概述与选题

### 1.1设计目标

1. **开箱即跑**：`clone` 下来不需要 API Key、不需要 `pip install`，`python run.py review examples/ --mock` 就能看到完整的一次审查。
2. **机制可见**：不引入 LangChain / OpenAI SDK，把「LLM 调用、ReAct 循环、记忆压缩、错误降级」这些被框架封装掉的机制原样暴露在源码里。
3. **确定性兜底**：关键结论由 AST 规则引擎产出（`AnalyzeCodeTool`），大模型只负责判断误报、解释影响、给出改法——不让「模型今天心情不好」决定报告质量。
4. **可观测**：每一步思考/工具调用/观察都以 `AgentEvent` 流式抛出，CLI 与 Web 共用同一套事件。

### 1.2 总体能力

* 6 个工具：`list_files`、`read_file`、`search_code`、`analyze_code`、`check_syntax`，以及默认**不注册**的危险工具 `run_code`；
* 26 条 AST 规则（`codereview/tools/python_ast.py` 模块文档中的规则表），覆盖安全、缺陷、可维护性、风格四类；
* 一份报告三种出口：终端彩色摘要、Markdown（`ReviewReport.to_markdown`）、JSON（`ReviewReport.to_json`）；四个子命令（`codereview/cli.py`）为 `review` / `chat` / `tools` / `web`，其中 `chat` 用 `session=memory` 实现多轮上下文记忆。

---

## 二、总体架构

### 2.1 分层结构

```
┌──────────────────────────────────────────────────────────────────────────┐
│  展示层   codereview/cli.py（review / chat / tools / web 四个子命令）      │
│           codereview/web.py（http.server 单页应用 + JobStore 任务表）      │
│           ← 两者都只消费 AgentEvent，不直接调用 LLM 或工具                 │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  on_event 回调 / review() 返回值
┌───────────────────────────────▼──────────────────────────────────────────┐
│  Agent 主循环   codereview/agent.py :: ReviewAgent                        │
│    review() → 思考 parse_action() → 行动 registry.execute() → 反思        │
│    → 交付 _finish() / 降级 _degrade()                                     │
│    证据库 _findings（_record_findings 入库、_merge_findings 补齐）         │
└───┬───────────────┬───────────────────┬──────────────────┬───────────────┘
    │               │                   │                  │
┌───▼─────────┐ ┌───▼──────────────┐ ┌──▼──────────────┐ ┌─▼─────────────┐
│ LLM 层       │ │  工具层           │ │  记忆层         │ │  报告层       │
│  llm.py     │ │  tools/base.py   │ │  memory.py      │ │  report.py    │
│ BaseLLM     │ │  Tool(抽象)      │ │  Memory         │ │  Issue        │
│ ├ OpenAI-   │ │  ToolRegistry    │ │  ├ history      │ │  ReviewReport │
│ │ CompatLLM │ │  build_default_  │ │  ├ build_       │ │  ├ to_markdown│
│ ├ MockLLM   │ │  registry        │ │  │  messages()  │ │  └ to_json    │
│ └ Retrying- │ │  ├ fs.py（3 个）  │ │  ├ render_      │ │  SEVERITY_    │
│   LLM       │ │  ├ python_ast.py │ │  │  ledger()    │ │  LABEL 等常量 │
│ create_llm()│ │  └ exec_tool.py  │ │  └ trace        │ │               │
└───┬─────────┘ └───┬──────────────┘ └──┬──────────────┘ └───────────────┘
    │               │                   │
┌───▼───────────────▼───────────────────▼──────────────────────────────────┐
│  基础设施层                                                                │
│  config.py（Config.from_env：命令行 > 环境变量 > .env > 默认值）           │
│  errors.py（CodeReviewError 异常树，明确区分可重试 / 不可重试）            │
│  prompts.py（全部提示词与 JSON 协议）  utils.py（token 估算、JSON 修复…）  │
└──────────────────────────────────────────────────────────────────────────┘
```

依赖方向自上而下**单向**：展示层 → Agent → （LLM / 工具 / 记忆 / 报告）→ 基础设施。`agent.py` 只依赖各层的抽象（`BaseLLM`、`ToolRegistry`、`Memory`、`ReviewReport`），不依赖任何具体实现；`prompts.py` / `utils.py` / `errors.py` 不反向依赖上层。

### 2.2 一次审查的数据流

```
用户：python run.py review examples/ --mock
  │
  ├─ cli.cmd_review → _build_config(args) → Config.from_env(...)       # 配置层
  ├─ build_default_registry(config) → ToolRegistry（5 个工具）          # 工具层装配
  ├─ ReviewAgent(config, on_event=ConsoleReporter(...))                 # 事件订阅
  └─ agent.review(["examples/"])
       ├─ Memory.set_system(build_system_prompt(registry.tools()))      # 角色+工具清单+Few-shot
       ├─ Memory.set_task(build_task_prompt(...))                       # 含 <TASK_CONTEXT>
       └─ while step < config.max_steps:
            messages = memory.build_messages()   # 系统 + 任务 + 台账 + 摘要 + 近期消息
            response = llm.chat(messages)        # RetryingLLM 内部做退避重试
            action   = parse_action(response.content)
            result   = registry.execute(action.tool, action.arguments)
            _record_findings(result)             # 证据入库（先于任何压缩）
            memory.add_observation(...) / add_trace(...)
  │
  └─ ReviewReport.from_final_answer(...) → to_markdown() / to_json() → --out 落盘
```

### 2.3 为什么是「依赖倒置 + 注册表」，而不是 if / else 分支

最直觉的写法是在主循环里写一串字符串比较分支：

```python
# 反面写法（本项目没有采用）
if action.tool == "list_files":
    result = list_files(action.arguments)
elif action.tool == "read_file":   # ……每个工具再来一段
    result = read_file(action.arguments)
```

这种写法在本项目里会同时踩四个坑：

1. **循环依赖**：`agent.py` 需要 import 每个具体工具，而工具又需要 `Config`、`errors`，主循环被具体实现绑死；`build_default_registry` 因此采用**延迟导入**（函数体内 `from .fs import ...`），专门避免「工具 → 注册表 → 工具」的环。
2. **Prompt 与执行会漂移**：系统提示词里的工具清单必须与可执行的分支完全一致。注册表让 `describe_tools(tools)` 直接从 `tool.parameters`（JSON-Schema 风格）渲染说明，且 `Tool.validate` 用**同一份定义**做运行时校验——「一份定义，两处生效」。
3. **危险工具无法收口**：`run_code` 的危险性必须集中在装配处决策。`build_default_registry` 里一句 `if config.allow_exec: tools.append(RunCodeTool(config))` 就完成了「默认不注册」，而不是在主循环里到处写 `if not allow_exec: raise`。
4. **不可测试**：注册表可以在测试里塞入假工具、假 LLM。`tests/test_agent.py` 用 `ScriptedLLM` 按剧本喂回复、`tests/test_tools.py` 断言注册表内容，都不需要改主循环一行。

依赖倒置体现在三个注入点：

* `ReviewAgent.__init__(config, *, llm=None, registry=None, on_event=None)`——LLM 与工具集都可替换；`llm` 缺省时才走 `create_llm(config, on_retry=self._on_retry)`。
* `create_llm(config, on_retry)`——按 `config.effective_mock` 选择 `MockLLM` 或 `OpenAICompatLLM`，外层统一包 `RetryingLLM`；调用方只认 `BaseLLM.chat`。
* `ToolRegistry`——`register` / `get` / `execute` / `names` / `tools` / `describe`，新工具只要继承 `Tool` 并写 `name`、`description`、`parameters`、`run`，无需修改 Agent。

---

## 三、Agent 循环设计

### 3.1 四阶段：思考 → 行动 → 反思 → 交付

`ReviewAgent.review` 的 `while step < self.config.max_steps` 循环，每个回合包含：

| 阶段 | 代码位置 | 做什么 |
| --- | --- | --- |
| ① 思考 | `self.llm.chat(messages)` → `parse_action(response.content)` | 把提示视图发给 LLM，解析出一个动作；`ParsedAction.kind` 为 `"tool"` 或 `"final"` |
| ② 行动 | `self.registry.execute(action.tool, action.arguments)` | 执行工具；任何异常都变成一条失败观察，绝不抛出 |
| ③ 反思 | `memory.add_observation(...)`、`memory.add_trace(...)`、`_record_findings(result)` | 写入记忆与轨迹、检测重复调用（`call_signatures`）、累计 `counters` |
| ④ 交付 | `_finish(...)` / `_degrade(...)` | 构造 `ReviewReport`，返回 `AgentResult` |

循环期间的每一步都通过 `_emit(kind, step, **data)` 抛出 `AgentEvent`，事件种类包括 `start`、`thought`、`thought_text`、`action`、`observation`、`correction`、`loop_detected`、`compressed`、`retry`、`degrade`、`final`。`_emit` 内部用 `try/except` 包住回调——**展示层的问题不该影响 Agent**。

把 `review()` 的分支画全，四阶段与三个出口一目了然：

```
review(targets, focus, language, instructions, session, follow_up)
  ├─ session 为空 → 新建 Memory：set_system(build_system_prompt(...)) + set_task(build_task_prompt(...))
  │  否则复用 session；follow_up 非空时追加 memory.add_user(follow_up, kind="follow_up")
  ├─ self._findings.clear()；counters / call_signatures / nudged 初始化；emit("start")
  │
  └─ while step < config.max_steps:                     # 默认 12
       ├─ ① 思考：response = self.llm.chat(messages)
       │    ├─ 抛 LLMError ─────────────────────────────► _finish_with_error()  → 出口 D
       │    └─ add_assistant(content)；emit("thought", latency_ms=...)
       │
       ├─ action = parse_action(response.content)
       │    └─ 抛 ActionParseError
       │         ├─ corrections <= MAX_CORRECTIONS(=2)：add_correction(build_correction_prompt(...))
       │         │                                       → emit("correction") → continue
       │         └─ 超限 ───────────────────────────────► _degrade()            → 出口 C
       │
       ├─ action.kind == "final" ──────────────────────► _finish()             → 出口 A
       │
       ├─ 死循环检查：signature = f"{tool}:{json.dumps(arguments, sort_keys=True)}"
       │    └─ repeats > config.repeat_action_limit(=2)：首次 add_correction → continue；
       │       再犯（signature 已在 nudged 中）──────────► _degrade()            → 出口 C
       │
       ├─ ② 行动：result = registry.execute(tool, arguments)；emit("action")
       ├─ ③ 反思：_record_findings(result)                # 证据入库（先于任何压缩）
       │          memory.add_observation(tool, result.output or result.error, ok=result.ok)
       │          memory.add_trace({...})；emit("observation", summary=result.summary)  # trace 即台账数据源
       └─ messages = memory.build_messages()；if last_stats.compressed: emit("compressed")
  └─ 循环自然结束（步数耗尽）────────────────────────► _degrade(MaxStepsExceeded) → 出口 C

出口 A：_finish()            → ReviewReport.from_final_answer + _merge_findings 补齐证据库
出口 C：_degrade(reason)     → 以 self._findings 为问题清单 + degraded=True
出口 D：_finish_with_error() → _degrade(reason="LLM 调用失败…") 并回填 result.error
```

`AgentResult` 是统一返回值，带 `report`、`steps`、`tool_calls`、`duration_s`、`error`、`trace`，以及**本次运行的 `memory`**——`chat` 子命令就是把上一轮的 `result.memory` 作为下一轮的 `session` 传回去，从而实现多轮上下文记忆（`ok` 属性则同时要求「无 error」且「报告未降级」）。

### 3.2 为什么用 JSON 文本协议，而不是 OpenAI function calling

`prompts.py` 的模块文档给出了三条理由，这里展开：

1. **可移植**：`OpenAICompatLLM` 走的是 OpenAI 兼容的 `/chat/completions`（`config.chat_completions_url`），一次对接 DeepSeek、通义千问、智谱、Kimi、vLLM、Ollama。原生 `tools` 字段在各家兼容层上的支持度参差不齐（尤其本地小模型），一旦换模型就要重写调用层。
2. **可补救**：本项目把「解析失败」当成**可纠正的对话事件**而非致命异常。`ActionParseError` 的文本会被 `build_correction_prompt(str(exc), response.content)` 回灌，模型下一轮自己改对；而原生 function calling 的格式错误通常直接由 SDK 抛出，Agent 无从插手。
3. **可观测**：`thought` 与 `action` 同处一段 JSON 文本，因此可以原样打印、原样存档。CLI 的思考行、Web 的轨迹面板、报告附录的 `trace` 表都来自这里——推理过程对评审者是可见的。

协议本身做了大量容错（`parse_action`）：

* `strip_code_fence` 去掉 ```json 围栏；
* 直接 `json.loads`，失败则 `repair_json`（修尾随逗号、中文全角引号）；
* 再失败则 `extract_json_objects` 做括号配对扫描，取字段最多的那个对象；
* `action` / `tool` / `name` 三个键名都接受；`action_input` / `arguments` / `parameters` 三个键名都接受；
* `FINAL_ALIASES = {"final_answer", "final", "finish", ...}` 让模型用口语化动词收尾也能被识别；
* 顶层直接给出 `summary` / `issues` / `score` 时，兜底当成 `final_answer`。

模型的一次合法回复与一次不合法回复分别长这样：

```jsonc
// ✅ 调用工具：thought 用一句话说明「为什么」，action 是注册表中的工具名
{"thought": "先了解仓库结构，确定审查范围", "action": "list_files", "action_input": {"path": "examples/", "pattern": "*"}}

// ✅ 给出结论：thought 与 final_answer 平级
{"thought": "已收集到足够的静态证据，收敛并输出最终结论",
 "final_answer": {"summary": "...", "score": 0, "issues": [], "highlights": [], "next_steps": []}}

// ❌ 会被 parse_action 拒绝：action_input 不是对象
{"action": "read_file", "action_input": "examples/buggy_sample.py"}
// → ActionParseError: action_input 必须是 JSON 对象，收到 str；正确写法：{...}
// → 该错误文本经 build_correction_prompt 回灌，模型下一轮自行改正
```

注意 `thought` 不是装饰：它既是 CLI/Web 展示「模型在想什么」的素材，也被 `memory.add_trace` 原样存进轨迹表（`agent._meta` 会把每一步的 `thought` / `action` / `arguments` / `result` / `ok` 一起交给 `ReviewReport`，最终渲染成报告附录里的轨迹表）。

### 3.3 三道保险：让 Agent不会崩

| 保险 | 触发场景 | 处理方式 | 代码位置 |
| --- | --- | --- | --- |
| 第一道：工具错误降级为观察 | 工具抛 `ToolArgumentError` / `PathSecurityError` / `ToolExecutionError` / 任意异常 | `Tool.safe_run` 捕获后返回 `ToolResult.failure(...)`；`ToolRegistry.execute` **永不抛出**，未知工具名也返回失败结果 | `tools/base.py` |
| 第二道：协议错误自纠 | `parse_action` 抛 `ActionParseError` | `MAX_CORRECTIONS = 2`：未超限则 `memory.add_correction(build_correction_prompt(...))` 让模型自我修复；超限才降级 | `agent.py` |
| 第三道：步数 / 死循环熔断后降级交付 | 达到 `max_steps`，或同一 `工具+参数` 签名重复超过 `repeat_action_limit`（默认 2）且已被提醒过 | `_degrade(...)` 把 `self._findings` 里的静态分析结论整理成一份**可用报告**（`degraded=True` + `degraded_reason`），而不是抛异常让用户两手空空 | `agent.py` |

死循环检测的细节：用 `signature = f"{action.tool}:{json.dumps(action.arguments, sort_keys=True)}"` 计数；`repeats > config.repeat_action_limit` 时先发一条纠正提示（并把签名记入 `nudged`），**再犯**才判定死循环并降级。这样「模型只是想再确认一次」不会被误杀。

第三道保险的意义在 `_finish_with_error` 里最明显：LLM 重试耗尽时（`LLMError`），依然走 `_degrade` 保留已获得的静态分析结论，只是把 `result.error` 一并填上，CLI 据此返回退出码 1。

---

## 四、工具系统

### 4.1 六个工具

工具定义集中在 `codereview/tools/`，全部继承 `Tool` 并声明 `name` / `description` / `parameters`（JSON-Schema 风格）/ `run`。

| 工具 | 类 | 主要参数（`parameters` 中的键） | 用途 | 默认注册 |
| --- | --- | --- | --- | --- |
| `list_files` | `ListFilesTool` | `path`、`pattern`、`max_results`、`show_size` | 递归列出文件清单（跳过 `SKIP_DIRS` 噪音目录），带扩展名分布与字节数，用于「先侦察」 | ✅ |
| `read_file` | `ReadFileTool` | `path`、`start_line`、`end_line`、`max_lines` | 带行号读取文本，可分段；拒绝二进制与超过 `max_file_bytes` 的大文件 | ✅ |
| `search_code` | `SearchCodeTool` | `pattern`、`path`、`file_pattern`、`max_results`、`ignore_case` | 正则搜索，返回 `文件:行号: 内容`；`path` 指向单文件时只搜该文件 | ✅ |
| `analyze_code` | `AnalyzeCodeTool` | `path`、`max_findings` | AST 静态分析（`PythonAnalyzer`）：复杂度、函数长度、文档覆盖率等度量 + 26 条规则的问题清单 | ✅ |
| `check_syntax` | `CheckSyntaxTool` | `path`、`max_files` | 批量 `compile()` 语法检查（**只编译不执行**），等价 `python -m compileall` 的核心部分 | ✅ |
| `run_code` | `RunCodeTool` | `path`、`args`、`timeout` | 在受限工作目录、超时与最小化环境变量下运行单个 `.py`，返回退出码与 stdout/stderr | ❌ 需 `--allow-exec` |

`build_default_registry(config)` 的装配逻辑：

```python
tools = [ListFilesTool(config), ReadFileTool(config), SearchCodeTool(config),
         AnalyzeCodeTool(config), CheckSyntaxTool(config)]
if config.allow_exec:
    tools.append(RunCodeTool(config))
return ToolRegistry(tools)
```

### 4.2 严重度分级

规则表里的 `severity` 不是随手标的，它遵循一条明确政策：

| 级别 | 判定标准 | 典型规则 |
| --- | --- | --- |
| `critical` | 会导致程序无法运行、数据损坏或**安全漏洞** | `syntax-error`、`eval-exec` |
| `major` | 逻辑错误、异常被吞、资源泄漏，以及**可维护性严重受损** | `bare-except`、`mutable-default`、`high-complexity` |
| `minor` | 可维护性与潜在缺陷 | `unused-import`、`none-comparison`、`open-without-with` |
| `info` | 风格与文档建议 | `long-line`、`todo-comment`、`missing-docstring` |

这条政策是**被自审查逼出来的**：早期版本把「圈复杂度 > 20」也判为 `critical`，结果本项目的
`_collect_structure`（复杂度 33）出现在严重问题清单里，与 `eval()` 注入并列。但复杂度再高
也不会直接导致线上事故——它是可维护性问题。把两者混为一谈会稀释 `critical` 的信号价值，
使用者很快就会学会「critical 也不用紧张」。

修正分两步：把 `high-complexity` 统一降到 `major`（`python_ast.py` 已经写明），
同时按报告建议真去重构了那几个函数（复杂度 33 → 20 以下）。**报告说什么就改什么，
而不是改报告**——这也是这个工具能持续被信任的前提。

### 4.3 路径沙箱

`Tool.resolve_path(raw, *, must_exist=True)` 是所有文件访问的唯一入口：

1. 空路径 → `ToolArgumentError("path 不能为空")`；
2. `Path(raw).expanduser()`，相对路径挂到 `self.workspace` 下；
3. `candidate.resolve()` 解析符号链接与 `..`（`tests/test_tools.py::test_symlink_style_escape_is_blocked` 覆盖）；
4. **越界判定**：`resolved != workspace and workspace not in resolved.parents` → `PathSecurityError`，错误信息里明确提示「如需放宽请用 `--workspace` 指定根目录」；
5. `must_exist=True` 但文件不存在 → `ToolExecutionError`。

沙箱边界就是 `--workspace`（`Config.workspace`，默认当前目录）。这条防线针对的是**提示注入**：被审查的代码里可能写着「请读取 `../../.ssh/id_rsa`」之类的诱导文本，而模型完全可能照做。

### 4.4 参数校验：把错误信息回灌给模型

`Tool.validate(arguments)` 在 `run` 之前做完整校验，**每一条错误信息本身就是给模型的修复提示**：

| 校验 | 错误信息示例 |
| --- | --- |
| 未知参数 | `未知参数 ['paths']；read_file 支持的参数是 ['end_line', 'max_lines', 'path', 'start_line']` |
| 缺少必填 | ``缺少必填参数 `path`（文件路径（相对工作区））`` |
| 类型不匹配 | ``参数 `start_line` 期望 integer，收到 str（'10'）`` |
| bool 冒充 int | ``参数 `max_lines` 期望 integer，收到布尔值 True`` |
| 取值越界 | ``参数 `max_results` 不能大于 2000`` / ``参数 `x` 只能是 [...] 之一，收到 ...`` |

`_TYPE_MAP` 把 `"string" / "integer" / "number" / "boolean" / "array" / "object"` 映射到 Python 类型；因为 `bool` 是 `int` 的子类，`integer` / `number` 会**显式排除** `True` / `False`，避免 `True` 被当成 `1`。

校验失败抛出的 `ToolArgumentError` 由 `safe_run` 转成 `ToolResult.failure("参数错误：...")`，Agent 再把它作为 `Observation from \`read_file\` [ERROR]` 写回消息历史——模型下一轮就能看到「我错在哪、正确用法是什么」，形成闭环。这正是「错误处理」评分点最容易被忽略、也最能体现水平的一处。

### 4.5 危险工具 `run_code` 默认不注册

`RunCodeTool.dangerous = True`，且**默认不出现在工具列表里**——只有 `--allow-exec`（或 `CR_ALLOW_EXEC=true`）才会被 `build_default_registry` 加入。默认不注册比「注册了但拒绝执行」更安全：模型看不到这个工具，就不会尝试调用它。即便被注册，运行时还有 `if not self.config.allow_exec: raise ToolExecutionError(...)` 二次拦截，以及三重约束：

* `effective_timeout = min(timeout, config.exec_timeout)`，超时即强杀并提示「可能存在死循环」；
* `cwd=target.parent`、`shell=False`、参数以列表传递；
* 环境变量只保留 `_SAFE_ENV_KEYS`（`PATH`、`SYSTEMROOT`、`TEMP`、`PYTHONIOENCODING` 等）并限定 `PYTHONPATH`，避免把当前进程的密钥泄漏给被测代码。

`python run.py tools` 会在末尾提示这一点。

---

## 五、上下文记忆与压缩

### 5.1 两层设计：完整历史 vs 提示视图

`Memory`（`codereview/memory.py`）刻意把「记什么」和「发给模型什么」分开：

| 层 | 载体 | 是否受预算限制 | 用途 |
| --- | --- | --- | --- |
| 完整历史 | `Memory.history: list[Message]` | ❌ 永不丢弃 | 生成报告、回放轨迹、失败复盘；`Message` 额外携带 `kind` / `tool` / `ok` / `tokens` 元信息 |
| 完整轨迹 | `Memory.trace: list[dict]` | ❌ 永不压缩 | 步骤级事实（`step` / `thought` / `action` / `arguments` / `result` / `ok`），进度台账与报告附录的**唯一**依据 |
| 提示视图 | `Memory.build_messages()` 的返回值 | ✅ 受 `token_budget` 限制 | 真正发给 LLM 的 `list[dict]` |

`build_messages` 的装配顺序：`system` → `task`（含 `<TASK_CONTEXT>`）→ **进度台账** → （有丢弃时）压缩摘要 → 早期幸存的 `older` → 最近 `keep_recent` 条 `recent`。预算计算方式是 `remaining = token_budget - base_tokens - recent_tokens`，然后**从新到旧**回填 `older`，装不下的进 `dropped`。

此外，`Memory.add_observation` 在**写入时**就用 `truncate(content, self.max_observation_chars)` 截断超长输出，而不是在渲染时——避免一条巨型观察污染之后所有轮次。

### 5.2 确定性压缩

`build_messages` 只做两件确定的事：

1. **丢弃**超出预算的早期消息，并把它们折叠为 `_render_digest(dropped)`：一段 `[上下文压缩] 为控制窗口，以下 N 条较早期的交互已被折叠为摘要：` 开头的清单，每行由 `Message.digest_line()` 生成，形如 `- [read_file] 文件 a.py（python，共 120 行）`，失败项带 ` [失败]` 标记。
2. **统计**至 `MemoryStats`：`messages` / `prompt_tokens` / `dropped` / `compressed` / `budget` / `history`，通过 `last_stats` 暴露给 Agent（触发 `compressed` 事件）与测试。

**不额外调用 LLM 做摘要**是刻意的：省钱、省时，更重要的是让测试可以断言压缩结果，避免「压缩本身引入不确定性」。token 估算用 `utils.estimate_tokens`（CJK 按 1 token/字，其余 4 字符/token），误差 20% 以内足够做预算控制，且免去 tokenizer 依赖。

### 5.3 进度台账：一个真实踩过的坑

**现象。** 早期版本判断「哪些工具已经调用过」时，直接扫描消息历史里的 assistant 消息。把 `memory_token_budget` 调小（例如 700）就能稳定复现：上下文触发压缩 → 早期的 assistant 动作消息被折叠成摘要 → 依赖消息历史的进度推断**看到的是空计划** → Agent 以为还没开工，把整套侦察流程（`list_files` → `analyze_code` × N → `read_file` → `search_code`）原样重跑一遍，白白烧掉步数预算，最后常常撞上 `max_steps` 而输出降级报告。对离线 `MockLLM` 尤其致命：它的 `_next_step` 完全靠这一步规划。

**修法。** 让台账来自**不参与压缩**的 `Memory.trace`：

* `Memory.render_ledger()` 从 `self.trace`（不是 `history`）取最后 `LEDGER_MAX_ENTRIES = 10` 条，渲染成以 `LEDGER_PREFIX = "[进度]"` 开头的消息；
* 每一行都是**合法 JSON**（`{"action": "read_file", "action_input": {"path": "a.py"}}`），因此既能给人看，也能被程序化解析；失败调用追加 `# 该调用失败，请换一种方式`；
* `build_messages` 把这个台账作为一条**独立消息常驻提示**（且排在压缩摘要之前），所以它永远不会被折掉；
* `MockLLM` 侧对应的是 `collect_executed_actions(messages)`：它**只扫描两类消息**——`role == "assistant"` 的消息，以及 `content.lstrip().startswith(LEDGER_PREFIX)` 的台账消息——从而不会把观察文本或纠正提示里出现的 JSON 误当成「已执行动作」，也正好绕开了被压缩掉的历史。

`LEDGER_PREFIX` 因此是**协议的一部分**：改这个字符串会同时打断 Agent 的进度推断与 Mock 的规划，源码注释里对此有明确警告。回归测试把这条经验钉死了——`tests/test_memory.py::test_ledger_survives_context_compression` 要求塞爆上下文后提示里**必须且只能**有一份 `[进度]` 消息且仍能找到 `list_files`；`test_ledger_lines_are_valid_json` / `test_failed_action_is_marked_in_ledger` 要求台账行可解析、失败项带标记。

### 5.4 证据库：静态分析结论在工具返回时就入库

同一类问题的另一半：既然**消息**会被压缩，结论就不能等到最后再从消息历史里「回捞」。`ReviewAgent` 因此维护一个独立的证据库：

* `self._findings: dict[tuple[str, int, str], Issue]`，键为 `(file, line, title)`；
* 每轮工具执行成功后立即调用 `_record_findings(result)`：从 `result.output` 里 `extract_json_objects` 扫描所有 `{"findings": [...]}` 负载，用 `Issue.from_dict` 转成 `Issue`，并以 `setdefault` **去重入库**（先到的严重度/描述优先，不被后续同位置条目覆盖）；
* 交付时 `_finish` 调用 `_merge_findings(report.issues)` 补齐模型没提到的条目，并在 `report.meta["note"]` 里注明「另有 N 条问题由静态分析（AST 规则引擎）补充」；
* 降级时 `_degrade` 直接以 `self._findings.values()` 为问题清单，按 `(issue.rank, issue.file, issue.line)` 排序后交给 `ReviewReport`。

判重的细节值得单独说：`_merge_findings` 按**位置**（`file` + `line`）判重，而不是按标题。模型用自己的话描述同一处问题时，标题几乎不可能与规则名（如 `mutable-default`）逐字相同；若按标题判重，同一个 Bug 会在报告里出现两遍。

一句话总结这条经验：**证据要在产生时收集，而不是在结论时回捞。** 对应的测试是 `tests/test_llm.py::test_static_findings_survive_even_if_compressed_away`（预算压到 600 仍要求报告里存在 AST 规则结论）。

---

## 六、Prompt 工程

### 6.1 集中管理

所有发给模型的文本都在 `codereview/prompts.py` 一个文件里，连协议键名也定义成常量（`ACTION_KEY = "action"`、`FINAL_KEY = "final_answer"`，以及任务上下文标记 `TASK_CONTEXT_OPEN` / `TASK_CONTEXT_CLOSE`）。好处是可审阅（Agent 的行为几乎完全由这里决定）、可迭代（调提示词不牵动业务逻辑）、可测试（协议片段可被直接断言）。

### 6.2 系统提示词的六个段落

`SYSTEM_PROMPT` 是一段模板，由 `build_system_prompt(tools, extra)` 渲染：

| 段落 | 内容要点 |
| --- | --- |
| 角色 | 「你是一名资深代码审查工程师（Senior Code Reviewer）」，负责**静态审查**，找 Bug / 异味 / 安全风险并给出可执行建议 |
| 工具清单 | `{tools}` 占位符由 `describe_tools(tools)` 填充；它遍历注册表，把每个工具的 `parameters` 渲染成 `- \`read_file(path: string —— ..., start_line: integer（可选，默认 1）)\`：说明` |
| 工作流（必须遵守） | ① 先侦察再下结论（必须先 `list_files`，再用 `read_file` / `analyze_code` / `search_code` 取证）；② 证据驱动，禁止臆测，引用要给 `文件路径:行号`；③ 先自动分析再人工阅读（优先用 `analyze_code` 的确定性结论，再用原文判断误报）；④ 控制预算，不重复读同一文件；⑤ 覆盖主要文件后立刻收敛 |
| 输出格式（极其重要） | 「每次回复**只能输出一个 JSON 对象**」；给出工具调用与 `final_answer` 两种模板，字段含 `summary`、`score`、`issues[{file,line,severity,category,title,detail,suggestion}]`、`highlights`、`next_steps` |
| 严重度标准 | `critical`（崩溃/数据损坏/安全漏洞，如 SQL 注入、eval 执行不可信输入）、`major`（逻辑错误、未处理异常、资源泄漏、并发问题）、`minor`（可维护性：重复代码、命名、函数过长、硬编码）、`info`（风格、文档、可选优化） |
| 禁止事项 | 不编造文件中不存在的代码；不在 `final_answer` 之外输出文字；不用 JSON 注释或尾随逗号；**代码质量好就诚实少报，不要为了凑数而制造问题** |

`build_system_prompt` 还会追加 `# 输出示例（Few-shot）` + `TOOL_CALL_EXAMPLE`，以及可选的 `# 本次任务的额外要求` + `extra`（来自 CLI 的 `--instructions`）。

### 6.3 Few-shot：四个示例

`TOOL_CALL_EXAMPLE` 覆盖了循环的四个典型节点，全部是可直接复制的合法 JSON：

1. 侦察仓库结构：`{"thought": "先了解仓库里有哪些文件，再决定审查顺序", "action": "list_files", "action_input": {"path": ".", "pattern": "*.py"}}`
2. 读取具体文件：`action: "read_file"`, `{"path": "src/utils.py"}`
3. 搜索危险模式：`action: "search_code"`, `{"pattern": "eval\\(|except\\s*:", "path": "."}`
4. 收敛给结论：`{"thought": "...", "final_answer": {"summary": "...", "score": 72, "issues": [], "highlights": [], "next_steps": []}}`

第 4 个示例特意把 `issues` 留空，向模型示范「没问题就是没问题」。

### 6.4 `<TASK_CONTEXT>` 结构化注入

`build_task_prompt(targets, *, workspace, focus, language, extra_context)` 生成本轮的用户消息：先是一段人类可读的说明（编号列出目标、工作区根目录、语言偏好、可选的重点关注），末尾附上一个结构化块：

```
<TASK_CONTEXT>{"targets": ["examples/"], "workspace": "D:\\test\\code-review-agent", "language": "auto", "focus": ""}</TASK_CONTEXT>
```

标记常量是 `TASK_CONTEXT_OPEN` / `TASK_CONTEXT_CLOSE`，配套 `encode_task_context` / `extract_task_context`。这个块一举三得：方便人肉排查、让日志可被程序化解析、并且是**离线 MockLLM 的规划依据**（`MockLLM._next_step` 用 `extract_task_context(messages)` 读出 `targets`，进而用 `_scope_dir` 推断扫描范围）。Mock 与真实模型共享同一套 Prompt 与协议，因此主循环里没有任何 `if mock:` 特判。

### 6.5 纠正提示

`build_correction_prompt(error, raw)` 渲染 `CORRECTION_PROMPT`：告知「你上一条回复不符合协议，无法被解析」，附上解析错误与原始输出节选（`raw[:800]`），再明确要求「只输出一个合法 JSON 对象」并重申两条合法形态与三条禁止（不要代码块标记、不要额外文字、不要尾随逗号）。

它比「直接重试一次」更省 token，也更可能修好——因为模型拿到了具体的失败原因（例如「`action_input` 必须是 JSON 对象，收到 str」），而不是盲目重抽。同一条路径也用于**打破死循环**：重复调用时注入的是「你已经用完全相同的参数调用过 X 次，结果不会改变，请换工具/换参数/直接给结论」。

---

## 七、错误处理与鲁棒性

### 7.1 异常体系：可重试 vs 不可重试

`codereview/errors.py` 定义了一棵树，根节点是 `CodeReviewError`：

```
CodeReviewError
  ├── ConfigError            配置缺失/非法                        → CLI 退出码 2
  ├── LLMError
  │     ├── LLMRequestError  网络/限流/5xx —— 可重试（带 status、retryable 字段）
  │     └── LLMResponseError 响应格式非法 —— 不可重试，交给上层纠错
  ├── ToolError ── ToolNotFound / ToolArgument / ToolExecution / PathSecurity（越界访问）
  └── AgentError ── MaxStepsExceeded（不致命，会降级出报告） / ActionParseError
```

「可重试」的判定在 `OpenAICompatLLM.chat` 里显式完成：`HTTPError` 的 `408 / 409 / 425 / 429` 或 `code >= 500` 归为可重试，`URLError` / `TimeoutError` / `OSError` 一律可重试；而 `401 / 403 / 404` 不可重试，并在错误信息里补一句「（API Key 或模型名可能有误）」。**格式错误永远不重试**——重试一万次也不会让模型的 JSON 变合法，那属于 Agent 的纠错职责。

### 7.2 指数退避 + 抖动

`RetryingLLM` 是装饰器式包装，`create_llm` 把 `MockLLM` 或 `OpenAICompatLLM` 统一包一层，重试延迟为 `delay = self.backoff_base ** (attempt - 1) + random.uniform(0, self._jitter)`：

* `backoff_base` 默认 1.5（`Config.backoff_base`），`max_retries` 默认 3；`jitter` 默认 0.3，用来打散多个并发客户端的重试时刻，避免「惊群」；
* 每次重试前调用 `on_retry(attempt, delay, exc)`；Agent 侧 `_on_retry` 累加 `retry_count` 并抛出 `retry` 事件，CLI 打印 `⟳ 第 N 次重试（x.xxs 后）`，报告附录里落成「LLM 重试次数」；`sleeper` 可注入，测试因此不必真的等待；
* 重试耗尽后异常继续上抛给 `review()`，由 `_finish_with_error` 转成降级报告。

### 7.3 观察预算的「分级降级」

`AnalyzeCodeTool` 的观察是全场最容易撑爆上下文的东西（目录分析要产出度量 + 函数明细 + 问题清单）。`_render_within_budget(head, payload, budget)` 负责在 `config.max_observation_chars`（默认 6000）内渲染，并且**保证输出始终是合法 JSON**。

**为什么不能直接 `truncate(json.dumps(...))`。** 把 JSON 从中间截断，调用方拿到的是半截文本：`json.loads` 失败 → `extract_json_objects` 也扫不出完整对象 → 结论**全部丢失**。对真实模型而言它看到一段乱码；对离线 `MockLLM` 而言 `_collect_static_findings` 直接空手而归；更糟的是 `ReviewAgent._record_findings` 也一并落空，证据库里什么都没有。因此改成「分级降级」——宁可少给信息，也要给**结构完整**的信息。

**牺牲顺序**（写在函数文档里，实现按此排列）：

| 级别 | 牺牲什么 | 实现 |
| --- | --- | --- |
| 1 | 可读性（缩进） | 先试 `indent=2`，下一档 `indent=None`（紧凑但合法） |
| 2 | 函数级明细 | 去掉 `functions` / `classes` / `notes`（`without_details`） |
| 3 | 问题的长文本 | `_abbreviate(finding)` 只保留 `_ABBREVIATED_FIELDS = ("file", "line", "severity", "category", "title", "rule")`，丢掉 `detail` / `suggestion` / `code` |
| 4 | 代码度量 | 再去掉 `metrics`（`lean`）——它是锦上添花，不该挤掉问题清单 |
| 5 | 问题条数 | `while keep > 1: keep = max(1, keep - max(1, keep // 3))`，**至少保留 1 条** |

实现为 6 份由全到简的 `plans` 依次尝试，仍放不下才进入缩条数循环；被裁剪时会在结果里补 `findings_truncated`、`findings_total` 和一句 `note` 说明「仅返回最严重的前 N 条，如需完整清单请分文件调用或调大 `CR_MAX_OBSERVATION_CHARS`」。只有在预算小到荒谬（连 1 条带路径的问题都放不下）时，才退回只保留 `path` / `language` / `metrics` / `severity_counts` 的 `minimal` 结构，问题清单为空。

这套设计与 5.4 的证据库是**互补**的：分级降级尽量让模型看到更多；万一还是丢了，证据库保证报告里不会少。反过来说，若采用「直接截断」的写法，`_record_findings` 会因为拿不到 `findings` 而让证据库为空，`MockLLM._collect_static_findings` 也会空手而归——用户最终可能只看到 `keyword-search` 之类的正则噪音。这是本项目开发过程中真实踩到的坑，因此宁可少给信息，也要给**结构完整**的信息。

### 7.4 其他鲁棒性细节

* **报告层容错**：`ReviewReport.from_final_answer` 对模型的任意畸形输出都能产出一份报告（`issues` 是 dict 就包成 list、`score` 非法就归零、`line` 是字符串就转 int、缺 `title` 的条目直接跳过），渲染层负责兜住模型的小脾气。
* **渲染层容错**：`ReviewAgent._emit` 吞掉展示层异常；`ConsoleReporter` 可注入 `stream`，`--json` 且不落盘时把横幅与进度改写到 `stderr`，保证 stdout 是**纯 JSON**。
* **中文 Windows 兼容**：`utils.read_text` 按 `utf-8-sig → utf-8 → gbk → latin-1` 顺序解码；`is_probably_text` 用 NUL 字节与双编码试探挡掉二进制文件；报告落盘前用 `strip_ansi` 去掉颜色码。
* **退出码约定**（CI 友好）：0 正常、1 运行期错误、2 配置错误、3 命中 `--fail-on` 阈值、130 用户中断。

---

## 八、测试策略

### 8.1 规模与组织

测试位于 `tests/`，全部基于标准库 **`unittest`**，零依赖：

```bash
python -m unittest discover -s tests -t .      # 149 个用例，约 0.3 秒
```

| 文件 | 关注点 | 代表用例（`test_` 前缀省略） |
| --- | --- | --- |
| `test_agent.py` | 主循环四类行为（正常收敛、协议自纠、死循环熔断、异常降级） | `max_steps_produces_degraded_report_with_salvaged_issues`、`tool_error_becomes_observation_not_crash`、`llm_failure_is_reported_without_losing_static_findings`、`clean_directory_passes` |
| `test_llm.py` | 重试策略、响应解析兼容、Mock 规划、压缩回归 | `retryable_error_is_retried_until_success`、`non_retryable_error_fails_fast`、`targeting_a_file_does_not_review_its_siblings` |
| `test_memory.py` | 两层记忆、压缩、进度台账 | `full_history_is_never_lost`、`ledger_survives_context_compression` |
| `test_tools.py` | 注册表、沙箱、参数校验、五个工具的边界 | `parent_escape_is_blocked`、`bool_is_not_accepted_as_integer`、`dangerous_tool_hidden_by_default` |
| `test_analyzer.py` | 26 条 AST 规则与度量 | `security_rules`、`secret_literal_is_not_echoed`、`cyclomatic_complexity_counts_branches` |
| `test_report.py` | 报告容错与渲染 | `from_dict_returns_none_without_title`、`markdown_structure` |
| `test_cli.py` / `test_config.py` | 端到端 CLI 行为、配置优先级 | `fail_on_threshold_returns_ci_exit_code`、`prefixed_variable_wins_over_standard_one` |

用 `ScriptedLLM`（`test_agent.py` 中按剧本返回预设回复的假 LLM）把「模型行为」变成**可复现的输入**，从而在不联网、不花 token 的前提下精确断言主循环的每一处分支。这是本项目能对 Agent 行为做单元测试的关键。

### 8.2 干净代码对照组：防误报

`examples/clean_sample.py` 是刻意写好的「良好实践」样本（`frozen dataclass`、明确异常、日志记录、`Decimal` 金额计算）。测试对它提出**零 critical / major** 的硬要求：

* `test_llm.py::test_mock_reports_no_issues_on_clean_code`——对干净代码断言 `serious == []`，失败信息里会打印误报的 `(rule, title)`；
* `test_agent.py` 中也有同类断言（干净目录通过）。

没有对照组，一个「到处报 critical」的审查器看起来也很厉害。对照组是唯一能证明「不误报」的手段，也直接呼应系统提示词里那句「不要为了凑数而制造问题」。

### 8.3 压缩回归测试

把 5.3 与 5.4 两个真实 bug 固化成测试：

* `test_agent_does_not_restart_plan_after_compression`——`memory_token_budget=700`、`max_observation_chars=900` 的极端配置下跑完整审查，断言 `trace` 里 `list_files` 恰好出现 1 次、且无任何重复动作、报告未降级。**这就是「进度台账来自 trace」这条修复的守门人。**
* `test_static_findings_survive_even_if_compressed_away`——预算压到 600，断言报告里仍然存在 AST 规则结论（排除 `keyword-search` 噪音后非空）。**这是「证据库在工具返回时入库」的守门人。**

这两个用例不是「覆盖率凑数」，而是两条设计决策的**可执行规格**：任何人以后想「简化一下记忆实现」，都会立刻被它们拦住。

### 8.4 手工验证清单

```bash
python -m unittest discover -s tests -t .        # 144 用例全绿（约 0.8 秒）
python run.py review examples/ --mock            # 离线端到端（critical 1 / major 14 / minor 8 / info 8，共 31 条）
python run.py review examples/clean_sample.py --mock --json    # 干净文件应无严重问题
python run.py tools                              # 工具清单 + run_code 提示
python run.py review examples/buggy_sample.py --out report.md --fail-on major   # 退出码 3
```

---

## 附：设计取舍速查

| 决策 | 选择 | 代价 / 替代方案 |
| --- | --- | --- |
| 协议 | 结构化 JSON 文本 | 不如原生 function calling 严谨，靠 `parse_action` 与纠正提示补足 |
| 压缩 | 确定性折叠（不调 LLM） | 摘要信息量较低，换来可测试性与零额外开销 |
| 进度 | 台账来自 `trace` | 多一条常驻消息占用预算，换来「计划不会被重跑」 |
| 结论 | AST 证据库 + 模型结论合并 | 报告可能比模型原本想说的更长，但确定性证据不会丢 |
| 观察超限 | 分级降级（保结构） | 信息量下降，换来「永远能解析」 |
| 危险工具 | 默认不注册 | 需要用户显式 `--allow-exec`，安全性优先 |
| 依赖 | 只用标准库 | 自研 .env 解析、token 估算、HTTP 客户端，换来开箱即跑 |
