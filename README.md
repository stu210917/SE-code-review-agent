# 代码审查 Agent（code-review-agent）

> 基于 **ReAct 循环**的代码审查 Agent：自己规划步骤、调用工具收集证据，最后产出结构化审查报告。
> **零第三方依赖**（纯 Python 标准库）；没有 API Key 时自动降级到**离线 Mock 模式**，照样跑通完整流程。

```bash
python run.py review examples/ --mock     # 无需联网、无需密钥，立刻看到效果
```

```
  思考  先了解目录结构，确定审查范围
步骤 01 调用工具 list_files {"path": "examples", "pattern": "*"}
  思考  对 examples/buggy_sample.py 做 AST 静态分析，拿到确定性证据
步骤 02 调用工具 analyze_code   ✓ critical=1, major=14, minor=8, info=5
  思考  精读 examples/buggy_sample.py，核对静态分析的上下文
步骤 03 调用工具 read_file
  思考  搜索高危写法，避免遗漏
步骤 04 调用工具 search_code
  思考  已收集到足够的静态证据，收敛并输出最终结论

审查结果：7/100（待改进）    步数 5    工具调用 4
问题统计：严重 1  重要 14  次要 8  提示 8

 1. [CRITICAL] 使用 `eval()` 执行动态代码
    位置 examples/buggy_sample.py:59   类别 security
    建议：改用 `ast.literal_eval()` 解析字面量…
 2. [MAJOR] 疑似硬编码凭据：`DB_PASSWORD`
    位置 examples/buggy_sample.py:25   类别 security
    建议：改为从环境变量或配置中心读取…

完整报告共 31 条，见 docs/example-report.md
```

---

## 快速开始

要求 **Python 3.9+**（实测 3.13 / 3.14），**不需要安装任何第三方库**。

```bash
python run.py review examples/ --mock                 # 1) 离线审查整个目录
python run.py review examples/ --out report.md        # 2) 导出 Markdown 报告
python run.py review . --base-url https://api.deepseek.com/v1 \
       --api-key sk-xxx --model deepseek-chat         # 3) 接入真实大模型
```

* 也可用 `python -m codereview review ...`。
* `python run.py web --open` 打开 Web 界面，实时看 Agent 的每一步。
* **Windows 编码提示**：程序输出已强制 UTF-8；要保存结果请用 `--out`，不要用 `>`（PowerShell 会写成 UTF-16）。

---

## 能发现什么

`analyze_code` 用 `ast` 做**确定性**检查，共 26 条规则（[`examples/buggy_sample.py`](examples/buggy_sample.py) 一份反面教材命中 21 条）：

| 类别 | 规则 |
| --- | --- |
| **安全** | `eval/exec` 动态执行、`os.system`、`subprocess(shell=True)`、`pickle.loads`、`yaml.load` 未指定 Loader、硬编码口令/密钥 |
| **缺陷** | 裸 `except:`、`except ...: pass`、可变默认参数、函数重复定义、`open()` 未用 `with`、可变类属性 |
| **可维护性** | 圈复杂度 >10、函数过长、参数过多、未使用导入、通配符导入、`global` 状态 |
| **风格** | `== None`、`range(len(x))`、lambda 赋值、缺文档、长行、TODO |

同时输出度量：代码/注释行数、函数与类数量、最大与平均圈复杂度、文档字符串覆盖率。

**防误报是硬指标**：测试断言 [`examples/clean_sample.py`](examples/clean_sample.py)（良好实践对照代码）**零 critical / major** 问题——宁可少报，也不制造噪音。

---

## 工具

| 工具 | 说明 | 默认启用 |
| --- | --- | --- |
| `list_files` / `read_file` / `search_code` | 目录浏览、带行号读文件、正则搜索 | ✅ |
| `analyze_code` | 26 条规则的 AST 静态分析（单文件或整个目录） | ✅ |
| `check_syntax` | 批量编译检查（只编译、不执行） | ✅ |
| `run_code` | 受限执行：超时 + 最小化环境变量 + 不经 shell | ⛔ 需 `--allow-exec` |

所有文件访问都经过 `Tool.resolve_path()` 沙箱校验，`../../etc/passwd`、`C:\Windows\...` 一律拒绝（防提示注入）。参数写错时，报错信息会带上正确用法，模型下一轮即可自纠。

---

## 配置

优先级：**命令行参数 > 环境变量 > `.env` > 内置默认值**。模板见 [`.env.example`](.env.example)。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CR_API_KEY` | *(空)* | 留空即自动进入离线 Mock 模式 |
| `CR_BASE_URL` | `https://api.openai.com/v1` | 任意 OpenAI 兼容端点 |
| `CR_MODEL` | `gpt-4o-mini` | 模型名 |
| `CR_MAX_STEPS` | `12` | 最大推理步数（超过则降级交付） |
| `CR_MEMORY_TOKEN_BUDGET` | `6000` | 上下文预算，超出后自动压缩早期观察 |
| `CR_MAX_RETRIES` | `3` | LLM 请求重试次数（指数退避 + 抖动） |
| `CR_ALLOW_EXEC` | `false` | 是否注册 `run_code`（危险工具） |

服务商参考：DeepSeek `https://api.deepseek.com/v1`｜通义千问 `https://dashscope.aliyuncs.com/compatible-mode/v1`｜Kimi `https://api.moonshot.cn/v1`｜本地 Ollama `http://localhost:11434/v1`

---

## 命令行

```bash
python run.py review <目标...> [选项]   # 审查文件或目录
python run.py chat  [目标...]           # 多轮交互（共享上下文记忆）
python run.py tools                     # 列出工具及其参数
python run.py web [--port 8000]         # 启动 Web 界面
```

常用选项：`--mock` 强制离线｜`--focus "重点看并发安全"`｜`--out report.md|report.json`｜`--json`（stdout 只输出 JSON）｜`--fail-on critical|major|minor|none`｜`--max-steps N`｜`--allow-exec`｜`--verbose`

**作为 CI 门禁**：

```bash
python run.py review src/ --json --fail-on major
```

退出码：`0` 正常｜`1` 运行错误｜`2` 配置错误｜`3` 命中 `--fail-on` 阈值｜`4` 未收敛但已降级交付｜`130` 用户中断。

---

## 项目结构

```
code-review-agent/
├── run.py                  # 统一入口（免安装即可运行）
├── README.md / DESIGN.md   # 使用说明 / 架构设计文档
├── codereview/             # 主体代码 17 个模块 / 约 5300 行
│   ├── agent.py llm.py memory.py prompts.py report.py
│   ├── cli.py web.py config.py errors.py utils.py
│   └── tools/              # 6 个工具 + 基类 + 注册表
├── examples/               # buggy_sample.py（反面教材）/ clean_sample.py（对照组）
├── tests/                  # 149 个单元测试
└── docs/                   # 报告样例 + 自审查报告
```

---

## 测试与报告样例

```bash
python -m unittest discover -s tests -t .     # 149 个用例，约 0.3 秒，零依赖
```

关键测试用「假 LLM」（按剧本返回预设内容）驱动，不联网、不抖动、毫秒级完成。

* [`docs/example-report.md`](docs/example-report.md) —— 对反面教材的完整审查（31 条问题）
* [`docs/example-report.json`](docs/example-report.json) —— 同一份报告的 JSON 形式
* [`docs/self-review.md`](docs/self-review.md) —— 它审查自己源码的结果（37 条，critical 0）；据此重构后最高圈复杂度 33 → 20

---

## 设计文档与已知限制

架构分层、Agent 循环、记忆压缩、Prompt 工程、错误处理与测试策略详见 [**DESIGN.md**](DESIGN.md)。

已知限制：AST 分析只覆盖 Python；没有跨函数数据流/污点分析；`run_code` 不是真沙箱；Web 界面无鉴权（仅本地演示）。

---

## 提交物

| 提交物 | 位置 |
| --- | --- |
| ① 源代码 | 本目录（打包为「学号姓名.zip」提交到 `001Homework1`） |
| ② README / Design 文档 | [`README.md`](README.md)、[`DESIGN.md`](DESIGN.md) |

> [`examples/buggy_sample.py`](examples/buggy_sample.py) 中的「硬编码口令」「eval 执行」等均为**故意写坏的反面教材**，密钥全是虚构字符串，请勿在真实环境运行。
