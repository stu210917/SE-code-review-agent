#!/usr/bin/env python3
"""项目统一入口（无需安装即可运行）。

用法::

    python run.py review examples/ --mock
    python run.py review src/ --out report.md
    python run.py chat examples/buggy_sample.py
    python run.py web

等价于 ``python -m codereview ...``；写这个文件是为了让评审老师
clone 下来就能直接 ``python run.py review .`` 跑起来，不必先 pip install。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许在未安装包的情况下直接运行（把仓库根目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codereview.cli import main  # noqa: E402  (必须在 sys.path 调整之后导入)

if __name__ == "__main__":
    sys.exit(main())
