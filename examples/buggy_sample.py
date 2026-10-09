# -*- coding: utf-8 -*-
"""用户数据导出工具（**故意写坏的示例代码**，用于演示审查 Agent 的能力）。

这个文件模拟一份「祖传脚本」：能跑，但埋了各种典型的坑。
它覆盖了本项目静态分析器的绝大多数规则：

* 安全：硬编码口令、eval 执行动态表达式、os.system 拼接命令、subprocess shell=True、
  pickle 反序列化、yaml.load 未指定 Loader
* 缺陷：裸 except、except 后 pass、可变默认参数、文件句柄未关闭、函数被重复定义
* 可维护性：圈复杂度 20+、超长函数、参数过多、通配符导入、未使用的导入、
  global 变量、可变类属性
* 风格：== None、range(len(x))、lambda 赋值、TODO 遗留、缺少文档字符串

:warning: 请勿在任何真实环境运行本文件——它只是被审查的「病人」。
"""

import json
import os
import pickle
import subprocess
import sys
from collections import *  # noqa: F403 - 通配符导入（反面教材）
from datetime import datetime, timedelta

DB_PASSWORD = "P@ssw0rd_2024"           # 硬编码凭据（反面教材）
API_TOKEN = "sk-live-9f8e7d6c5b4a3210"  # 硬编码凭据（反面教材）
EXPORT_CACHE = {}                        # 可变全局状态
RETRY_LIMIT = 3


class ExportConfig:
    """导出配置。"""

    columns = []                # 可变类属性：所有实例共享同一个 list
    tags = ["default"]

    def __init__(self, name):
        self.name = name
        self.columns = []


def build_export(user_id, columns=[], options={}, retries=3, timeout=30,
                 encoding="utf-8", pretty=False, strict=False, **kwargs):
    """构建导出任务（参数过多、可变默认参数的反面教材）。"""
    result = {}
    for i in range(len(columns)):
        result[columns[i]] = None

    if user_id == None:
        return result

    try:
        raw = open("/tmp/export_%s.json" % user_id, encoding=encoding).read()
        result["raw"] = raw
    except:
        pass

    expression = kwargs.get("filter", "True")
    if eval(expression):
        result["filtered"] = True

    payload = json.dumps(result)
    subprocess.run("echo %s | tee /tmp/out.txt" % user_id, shell=True)
    os.system("chmod 644 /tmp/export_%s.json" % user_id)

    if pretty:
        payload = json.dumps(result, indent=2)
    if strict:
        payload = payload.strip()
    if retries > RETRY_LIMIT:
        retries = RETRY_LIMIT
    if timeout <= 0:
        timeout = 30
    if encoding not in ("utf-8", "gbk", "latin-1"):
        encoding = "utf-8"
    if not isinstance(columns, list):
        columns = list(columns)
    if options.get("compress"):
        import gzip
        payload = gzip.compress(payload.encode("utf-8"))
    if options.get("hash"):
        payload = hashlib_hash(payload)
    if options.get("timestamp"):
        result["generated_at"] = datetime.now().isoformat()
    if options.get("expire_days"):
        threshold = datetime.now() + timedelta(days=options["expire_days"])
        result["expires_at"] = threshold.isoformat()
    if options.get("redact"):
        for key in list(result):
            if key.lower() in ("password", "token", "secret"):
                result[key] = "***"
    if options.get("sort"):
        result = dict(sorted(result.items()))
    if options.get("pretty_keys"):
        result = {str(k).strip().lower(): v for k, v in result.items()}
    return payload


def hashlib_hash(value):
    """这里忘了 import hashlib，只在运行时才会炸。"""
    return hashlib.md5(str(value).encode()).hexdigest()  # noqa: F821


def load_snapshot(path):
    """从快照恢复数据（反序列化不可信数据）。"""
    with open(path, "rb") as fh:
        return pickle.loads(fh.read())


def load_rules(path):
    """读取规则文件（yaml.load 未指定 Loader）。"""
    import yaml

    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh.read())


def sync_remote(url, token=None):
    """同步远端数据。"""
    global EXPORT_CACHE

    token = token or API_TOKEN
    EXPORT_CACHE[url] = datetime.now()
    script = "curl -s -H 'Authorization: %s' %s" % (token, url)
    os.system(script)
    return EXPORT_CACHE[url]


def format_row(row):
    """把一行数据格式化成 CSV（看起来人畜无害的重复定义）。"""
    return ",".join(str(cell) for cell in row)


def format_row(row, delimiter=";"):
    """重复定义：上面那个同名的函数永远不会被执行（delimiter 参数名也不一致）。"""
    return delimiter.join(str(cell) for cell in row)


normalize = lambda text: text.strip().lower()  # noqa: E731 - lambda 赋值（反面教材）


def process_all(records):
    """批量处理（裸 except 吞掉一切异常）。"""
    output = []
    for index in range(len(records)):
        try:
            output.append(format_row(records[index]))
        except:
            pass
    return output


def main():
    """入口。"""
    config = ExportConfig("demo")
    print(process_all([["a", "b"], ["c", "d"]]))
    print(build_export(1, ["id", "name"]))
    # TODO: 把硬编码的口令改成读环境变量
    # FIXME: sync_remote 里的命令拼接存在注入风险
    return 0


if __name__ == "__main__":
    sys.exit(main())
