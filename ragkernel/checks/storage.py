"""存储检查：sqlite-vec 扩展可加载、数据目录可写。

两项都刻意做成只读探测——doctor 不能有副作用：
- sqlite 用内存库验证扩展能力，不去 open/建表 用户的 ragkernel.db
- data 目录用 config 的同一套路径计算，但不 mkdir（config.data_dir() 会建目录）
"""

import os
import sqlite3
import tempfile
from pathlib import Path

from ..diagnostics.runner import CheckSpec
from ..diagnostics.schema import CheckResult, failed, passed

CATEGORY = "storage"


def _data_dir_path() -> Path:
    """与 config.data_dir() 同样的解析规则，但不创建目录（保持 doctor 只读）。"""
    from .. import config

    override = os.environ.get("RAGKERNEL_DATA_DIR")
    if override:
        return Path(override)
    return config.ROOT / config.settings().get("data_dir", "data")


def check_sqlite() -> CheckResult:
    """验证 sqlite-vec 能加载。

    用 :memory: 而不是真实库——我们要测的是「这个 Python 的 sqlite 支不支持加载扩展」，
    而系统自带的 macOS Python 恰恰缺 enable_load_extension，是最常见的翻车点。
    """
    import sqlite_vec

    db = sqlite3.connect(":memory:")
    try:
        if not hasattr(db, "enable_load_extension"):
            return failed(
                "sqlite", CATEGORY, "sqlite-vec 扩展",
                "当前 Python 的 sqlite3 不支持加载扩展"
                "（系统自带的 macOS Python 常见如此）",
                fix="uv sync --python 3.12",
            )
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        ver = db.execute("SELECT vec_version()").fetchone()[0]
        return passed("sqlite", CATEGORY, "sqlite-vec 扩展",
                      f"可加载（vec {ver}）", vec_version=ver)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError) as e:
        return failed(
            "sqlite", CATEGORY, "sqlite-vec 扩展",
            f"加载失败：{e}",
            fix="uv sync --python 3.12",
            exception_type=type(e).__name__,
        )
    finally:
        db.close()


def check_storage() -> CheckResult:
    """数据目录存在且可写。不存在不算错——首次安装本来就还没建。"""
    d = _data_dir_path()

    if not d.exists():
        parent = d.parent
        if not parent.exists():
            return failed(
                "storage", CATEGORY, "数据目录",
                f"父目录不存在：{parent}",
                fix=f"mkdir -p {d}", path=str(d),
            )
        if not os.access(parent, os.W_OK):
            return failed(
                "storage", CATEGORY, "数据目录",
                f"父目录不可写，无法创建 {d}",
                fix=f"chown -R $USER {parent}", path=str(d),
            )
        return passed("storage", CATEGORY, "数据目录",
                      f"尚未创建（首次运行时自动建）：{d}", path=str(d), exists=False)

    if not d.is_dir():
        return failed("storage", CATEGORY, "数据目录",
                      f"路径存在但不是目录：{d}", fix=f"rm {d}", path=str(d))

    # os.access 在 root 下会撒谎（root 对只读目录也报可写），实际写一下更可靠
    try:
        with tempfile.NamedTemporaryFile(dir=d, prefix=".ragkernel-writetest-"):
            pass
    except OSError as e:
        return failed(
            "storage", CATEGORY, "数据目录",
            f"不可写：{d}（{e.strerror or e}）",
            fix=f"chown -R $USER {d}", path=str(d), exception_type=type(e).__name__,
        )

    return passed("storage", CATEGORY, "数据目录", f"可写：{d}", path=str(d), exists=True)


CHECKS = [
    CheckSpec("storage", CATEGORY, "数据目录", check_storage, minimal=True),
    CheckSpec("sqlite", CATEGORY, "sqlite-vec 扩展", check_sqlite, minimal=True),
]
