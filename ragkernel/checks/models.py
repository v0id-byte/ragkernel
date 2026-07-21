"""模型检查：本地 embedding / reranker 是否就绪。

非 required——模型没缓存是「功能缺失但系统健康」（docker build 里模型下载之前跑
doctor 是正常场景），只该 degraded 不该 unhealthy。经 models.get_cache_status()
纯 FS 探测，模块导入不触发 huggingface_hub / torch，符合 checks 层的依赖约束。
"""

from ..diagnostics.runner import CheckSpec
from ..diagnostics.schema import CheckResult, failed, passed

CATEGORY = "model"


def _human(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def check_models() -> CheckResult:
    from .. import models

    results = models.get_cache_status()
    unready = [r for r in results if r.status in ("missing", "incomplete", "error")]

    if not unready:
        detail = " · ".join(f"{r.role} {r.name}（{_human(r.size_bytes)}）" for r in results)
        return passed("models", CATEGORY, "本地模型", f"已缓存 · {detail}",
                      models=[r.role for r in results])

    # 有未就绪的——warning（degraded），配可直接执行的修复命令
    parts = "、".join(f"{r.role}:{r.status}" for r in unready)
    return failed("models", CATEGORY, "本地模型",
                  f"未就绪（{parts}）——首次使用会自动下载 ~2GB",
                  severity="warning", fix="ragkernel models",
                  unready=[{"role": r.role, "status": r.status, "name": r.name} for r in unready])


CHECKS = [
    CheckSpec("models", CATEGORY, "本地模型", check_models),
]
