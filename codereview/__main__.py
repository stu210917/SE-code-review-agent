"""支持 ``python -m codereview`` 方式调用。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
