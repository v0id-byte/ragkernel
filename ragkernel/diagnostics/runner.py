"""检查编排：固定顺序、统一计时、异常兜底。

doctor 的职责不是证明代码没 bug，而是在代码有 bug 时依然告诉用户哪里坏了。
所以单个 check 抛异常绝不能让整个 run() 挂掉——那恰恰是最需要诊断的时刻。
"""

from dataclasses import dataclass
from time import monotonic
from typing import Callable

from .schema import CheckResult


@dataclass(frozen=True)
class CheckSpec:
    """把 id/category/title 放在 spec 上，异常兜底时才能填出一条完整的 CheckResult。"""

    id: str
    category: str
    title: str
    fn: Callable[[], CheckResult]
    network: bool = False  # --offline 时跳过
    minimal: bool = False  # bootstrap 预检只跑这些


def _registry() -> list[CheckSpec]:
    # 顺序显式固定：doctor 的输出就是产品体验，不能因为 import 顺序变了就漂移。
    # 函数内导入，保持 cli.py 那套「重依赖惰性加载」的约定。
    from ..checks import models, provider, runtime, storage

    return [*runtime.CHECKS, *storage.CHECKS, *provider.CHECKS, *models.CHECKS]


def run(*, offline: bool = False, minimal: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []
    for spec in _registry():
        if minimal and not spec.minimal:
            continue

        t0 = monotonic()
        if offline and spec.network:
            r = CheckResult(
                id=spec.id, category=spec.category, title=spec.title,
                status="skipped", summary="--offline：跳过网络检查",
            )
        else:
            try:
                r = spec.fn()
            except Exception as e:
                # 异常类型进 meta 供 --verbose 显示；不放 traceback，避免泄漏路径
                r = CheckResult(
                    id=spec.id, category=spec.category, title=spec.title,
                    status="failed", severity="error",
                    summary=f"检查本身崩溃：{type(e).__name__}",
                    meta={"exception_type": type(e).__name__, "exception": repr(e)},
                )
        r.duration_ms = int((monotonic() - t0) * 1000)
        results.append(r)
    return results
