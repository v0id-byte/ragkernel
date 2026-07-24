"""ragkernel —— 本地优先的企业 RAG 内核。"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# 版本唯一源是 pyproject.toml 的 [project].version，这里只做派生——两处手维护迟早对不上，
# 而发布链路（tag == pyproject == manifest 三方一致）要求版本号有且只有一个可信来源。
# 代价：editable 安装下改了 pyproject 要重跑 `uv sync` 才刷新 metadata。升级流程本就跑
# uv sync，不构成问题；直接从源码树跑（未安装）退化成占位版本，此时靠 doctor 报的 commit 定位。
try:
    __version__ = _pkg_version("ragkernel")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
