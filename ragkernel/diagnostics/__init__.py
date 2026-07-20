"""诊断引擎：纯检查逻辑，不 print、不 sys.exit。

doctor 是表现层（渲染 + 退出码），bootstrap 是变更层，两者都只依赖这里。
bootstrap 不 import doctor——那会把 CLI 渲染器拖进初始化流程。
"""

from .runner import CheckSpec, run
from .schema import (
    DEFAULT_POLICY,
    DEPRECATED_CHECK_IDS,
    DIAGNOSTICS_SCHEMA_VERSION,
    EXIT_DEGRADED,
    EXIT_HEALTHY,
    EXIT_UNHEALTHY,
    EXIT_UNKNOWN,
    CheckResult,
    HealthPolicy,
    HealthStatus,
    Severity,
    Status,
    failed,
    passed,
    skipped,
)

__all__ = [
    "CheckResult", "CheckSpec", "HealthPolicy", "HealthStatus", "Severity", "Status",
    "DEFAULT_POLICY", "DEPRECATED_CHECK_IDS", "DIAGNOSTICS_SCHEMA_VERSION",
    "EXIT_HEALTHY", "EXIT_DEGRADED", "EXIT_UNHEALTHY", "EXIT_UNKNOWN",
    "run", "passed", "failed", "skipped",
]
