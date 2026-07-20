"""运行时检查：Python 版本。

这一层零外部依赖——不碰 provider、不碰 huggingface、不碰网络，
所以在任何坏掉的环境里都能跑出结果。
"""

import sys

from ..diagnostics.runner import CheckSpec
from ..diagnostics.schema import CheckResult, failed, passed

CATEGORY = "runtime"

# 与 pyproject.toml 的 requires-python 保持一致
MIN_PYTHON = (3, 12)
MAX_PYTHON_EXCL = (3, 14)  # onnxruntime(RapidOCR) 暂无 cp314 轮子


def check_python() -> CheckResult:
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    managed = "uv" in sys.prefix or "uv" in sys.base_prefix

    if (v.major, v.minor) < MIN_PYTHON:
        return failed(
            "python", CATEGORY, "Python 版本",
            f"{cur}——需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            fix="uv sync --python 3.12", version=cur,
        )
    if (v.major, v.minor) >= MAX_PYTHON_EXCL:
        return failed(
            "python", CATEGORY, "Python 版本",
            f"{cur}——需要 < {MAX_PYTHON_EXCL[0]}.{MAX_PYTHON_EXCL[1]}"
            "（onnxruntime 暂无该版本轮子）",
            fix="uv sync --python 3.12", version=cur,
        )
    return passed(
        "python", CATEGORY, "Python 版本",
        f"{cur}{'（uv 托管）' if managed else ''}",
        version=cur, uv_managed=managed, executable=sys.executable,
    )


CHECKS = [
    CheckSpec("python", CATEGORY, "Python 版本", check_python, minimal=True),
]
