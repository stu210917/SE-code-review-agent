# 代码审查报告

> 生成时间：2026-10-07 18:38:25 ｜ 模型：`mock` ｜ 模式：mock（离线演示） ｜ 审查目标：`examples/`

## 一、总体评价

本次共完成 2 个文件的静态审查，发现 critical 1 项、major 14 项、minor 8 项、info 8 项。整体评分 7/100。（离线 Mock 基线：结论完全来自 AST 静态分析与正则搜索，未经过大模型润色。）

**综合评分：7/100（待改进）**

## 二、问题统计

| 严重度 | 数量 |
| --- | ---: |
| 🔴 严重（critical） | 1 |
| 🟠 重要（major） | 14 |
| 🟡 次要（minor） | 8 |
| 🔵 提示（info） | 8 |
| **合计** | **31** |

## 三、问题清单

### 1. 🔴 [严重] 使用 `eval()` 执行动态代码

- **位置**：`examples/buggy_sample.py:59`
- **类别**：安全（security）
- **严重度**：严重（critical）
- **规则**：`eval-exec`

### 2. 🟠 [重要] 疑似硬编码凭据：`DB_PASSWORD`

- **位置**：`examples/buggy_sample.py:25`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`hardcoded-secret`

### 3. 🟠 [重要] 疑似硬编码凭据：`API_TOKEN`

- **位置**：`examples/buggy_sample.py:26`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`hardcoded-secret`

### 4. 🟠 [重要] `build_export` 圈复杂度过高（21）

- **位置**：`examples/buggy_sample.py:42`
- **类别**：可维护性（maintainability）
- **严重度**：重要（major）
- **规则**：`high-complexity`

### 5. 🟠 [重要] `build_export` 使用了可变默认参数

- **位置**：`examples/buggy_sample.py:42`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`mutable-default`

### 6. 🟠 [重要] 裸 `except:` 捕获所有异常

- **位置**：`examples/buggy_sample.py:55`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`bare-except`

### 7. 🟠 [重要] 异常被静默忽略（`except ...: pass`）

- **位置**：`examples/buggy_sample.py:55`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`except-pass`

### 8. 🟠 [重要] `subprocess` 使用了 `shell=True`

- **位置**：`examples/buggy_sample.py:63`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`subprocess-shell`

### 9. 🟠 [重要] 使用 `os.system()` 执行系统命令

- **位置**：`examples/buggy_sample.py:64`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`os-system`

### 10. 🟠 [重要] 反序列化不可信数据（`pickle.loads`）

- **位置**：`examples/buggy_sample.py:107`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`pickle-loads`

### 11. 🟠 [重要] `yaml.load` 未指定 Loader

- **位置**：`examples/buggy_sample.py:115`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`yaml-load-unsafe`

### 12. 🟠 [重要] 使用 `os.system()` 执行系统命令

- **位置**：`examples/buggy_sample.py:125`
- **类别**：安全（security）
- **严重度**：重要（major）
- **规则**：`os-system`

### 13. 🟠 [重要] 函数 `format_row` 被重复定义

- **位置**：`examples/buggy_sample.py:134`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`redefined-function`

### 14. 🟠 [重要] 裸 `except:` 捕获所有异常

- **位置**：`examples/buggy_sample.py:148`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`bare-except`

### 15. 🟠 [重要] 异常被静默忽略（`except ...: pass`）

- **位置**：`examples/buggy_sample.py:148`
- **类别**：缺陷（bug）
- **严重度**：重要（major）
- **规则**：`except-pass`

### 16. 🟡 [次要] 使用了通配符导入 `from ... import *`

- **位置**：`examples/buggy_sample.py:22`
- **类别**：可维护性（maintainability）
- **严重度**：次要（minor）
- **规则**：`star-import`

### 17. 🟡 [次要] `ExportConfig` 的类属性是可变对象

- **位置**：`examples/buggy_sample.py:34`
- **类别**：缺陷（bug）
- **严重度**：次要（minor）
- **规则**：`mutable-class-attr`

### 18. 🟡 [次要] `ExportConfig` 的类属性是可变对象

- **位置**：`examples/buggy_sample.py:35`
- **类别**：缺陷（bug）
- **严重度**：次要（minor）
- **规则**：`mutable-class-attr`

### 19. 🟡 [次要] `build_export` 过长（55 行）

- **位置**：`examples/buggy_sample.py:42`
- **类别**：可维护性（maintainability）
- **严重度**：次要（minor）
- **规则**：`long-function`

### 20. 🟡 [次要] `build_export` 参数过多（9 个）

- **位置**：`examples/buggy_sample.py:42`
- **类别**：可维护性（maintainability）
- **严重度**：次要（minor）
- **规则**：`too-many-args`

### 21. 🟡 [次要] 用 `==` 比较 `None`

- **位置**：`examples/buggy_sample.py:49`
- **类别**：风格（style）
- **严重度**：次要（minor）
- **规则**：`none-comparison`

### 22. 🟡 [次要] `open()` 未使用 `with` 管理

- **位置**：`examples/buggy_sample.py:53`
- **类别**：缺陷（bug）
- **严重度**：次要（minor）
- **规则**：`open-without-with`

### 23. 🟡 [次要] 使用 `global` 修改全局变量 EXPORT_CACHE

- **位置**：`examples/buggy_sample.py:120`
- **类别**：可维护性（maintainability）
- **严重度**：次要（minor）
- **规则**：`global-statement`

### 24. 🔵 [提示] 高风险关键字：os.system

- **位置**：`examples/buggy_sample.py:7`
- **类别**：安全（security）
- **严重度**：提示（info）
- **规则**：`keyword-search`
- **说明**：搜索到 `os.system`：* 安全：硬编码口令、eval 执行动态表达式、os.system 拼接命令、subprocess shell=True、。请人工确认这里是否安全。
- **建议**：人工复核该处调用：确认输入可信，必要时替换为安全替代方案并补充校验。

### 25. 🔵 [提示] 高风险关键字：== None

- **位置**：`examples/buggy_sample.py:12`
- **类别**：可维护性（maintainability）
- **严重度**：提示（info）
- **规则**：`keyword-search`
- **说明**：搜索到 `== None`：* 风格：== None、range(len(x))、lambda 赋值、TODO 遗留、缺少文档字符串。请人工确认这里是否安全。
- **建议**：人工复核该处调用：确认输入可信，必要时替换为安全替代方案并补充校验。

### 26. 🔵 [提示] 高风险关键字：subprocess

- **位置**：`examples/buggy_sample.py:20`
- **类别**：可维护性（maintainability）
- **严重度**：提示（info）
- **规则**：`keyword-search`
- **说明**：搜索到 `subprocess`：import subprocess。请人工确认这里是否安全。
- **建议**：人工复核该处调用：确认输入可信，必要时替换为安全替代方案并补充校验。

### 27. 🔵 [提示] `for i in range(len(x))` 写法不够 Pythonic

- **位置**：`examples/buggy_sample.py:46`
- **类别**：风格（style）
- **严重度**：提示（info）
- **规则**：`range-len`

### 28. 🔵 [提示] 把 lambda 赋值给 `normalize`

- **位置**：`examples/buggy_sample.py:139`
- **类别**：风格（style）
- **严重度**：提示（info）
- **规则**：`lambda-assignment`

### 29. 🔵 [提示] `for i in range(len(x))` 写法不够 Pythonic

- **位置**：`examples/buggy_sample.py:145`
- **类别**：风格（style）
- **严重度**：提示（info）
- **规则**：`range-len`

### 30. 🔵 [提示] 遗留 TODO 标记

- **位置**：`examples/buggy_sample.py:158`
- **类别**：可维护性（maintainability）
- **严重度**：提示（info）
- **规则**：`todo-comment`

### 31. 🔵 [提示] 遗留 FIXME 标记

- **位置**：`examples/buggy_sample.py:159`
- **类别**：可维护性（maintainability）
- **严重度**：提示（info）
- **规则**：`todo-comment`

## 四、值得肯定的地方

- 项目结构可被工具链完整解析，便于接入 CI 做持续审查。

## 五、后续建议

1. 优先修复 critical / major 级别问题，并为每个修复补充回归测试。
2. 对安全类问题（如动态执行、命令拼接、硬编码凭据）做一次专项复查。
3. 在 CI 中加入 `analyze_code` 与语法检查，防止问题回流。

---

## 附录：执行轨迹

- 推理步数：6
- 工具调用：5 次（list_files, analyze_code, read_file, search_code）
- 耗时：0.02 秒
- Token 消耗：约 26714
- 上下文压缩触发：1 次

| 步骤 | 动作 | 参数摘要 | 结果 |
| ---: | --- | --- | --- |
| 1 | `list_files` | {"path": "examples/", "pattern": "*"} | 目录 examples 下匹配 `*` 的文件共 2 个： |
| 2 | `analyze_code` | {"path": "examples/buggy_sample.py"} | 已完成 1 个文件的静态分析：critical=1, major=15, minor=8, info=5（语言：python，分析器：AST 规则引擎） |
| 3 | `analyze_code` | {"path": "examples/clean_sample.py"} | 已完成 1 个文件的静态分析：critical=0, major=0, minor=0, info=0（语言：python，分析器：AST 规则引擎） |
| 4 | `read_file` | {"path": "examples/buggy_sample.py"} | 文件 examples/buggy_sample.py（python，共 164 行 / 5316 字节），显示第 1-164 行： |
| 5 | `search_code` | {"pattern": "eval\\(\|exec\\(\|except\\s*:\|except Exception\|==\\s*None\|TODO\|FIXME\| | 在 examples 下扫描 2 个文件，匹配 /eval\(\|exec\(\|except\s*:\|except Exception\|==\s*None\|TOD |