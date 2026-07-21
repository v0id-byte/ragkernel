"""诊断结果契约。

这个 schema 是对外接口——doctor CLI、--json、K8s probe、未来的 Web dashboard 都消费它，
所以字段语义在 docs/diagnostics.md 里成文，改动要走版本号。

两个核心设计：
1. status（通过了吗）与 severity（有多严重）是正交的两个维度，不能合并成一个字段。
   模型没缓存是 failed + warning：确实没通过，但系统是健康的。
2. required 不在这里——它不是检查的属性，而是「结果 + 执行上下文 + 策略」的产物。
   同一条 provider.auth，K8s readiness 视为必需，开发机上未必。见 HealthPolicy。
"""

from dataclasses import dataclass, field
from typing import Any, Literal

# 字段结构（而非 id 集合）发生不兼容变化时才 +1。新增字段不算不兼容——
# 消费方必须忽略未知字段，见 docs/diagnostics.md。
DIAGNOSTICS_SCHEMA_VERSION = 1

Status = Literal["passed", "failed", "skipped"]
Severity = Literal["none", "warning", "error"]

# 系统整体状态：与单项的 Status 是两个 namespace，不要混用
HealthStatus = Literal["healthy", "degraded", "unhealthy", "unknown"]

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_UNHEALTHY = 2
EXIT_UNKNOWN = 3

EXIT_BY_STATUS: dict[str, int] = {
    "healthy": EXIT_HEALTHY,
    "degraded": EXIT_DEGRADED,
    "unhealthy": EXIT_UNHEALTHY,
    "unknown": EXIT_UNKNOWN,
}


@dataclass
class CheckResult:
    """单项检查的事实。不含策略判断（required/退出码都由 HealthPolicy 决定）。"""

    id: str  # "provider.auth"——稳定标识，JSON key，发布后不许改名
    category: str  # runtime | storage | provider | model | security
    title: str  # 人读标题
    status: Status
    severity: Severity = "none"
    summary: str = ""
    fix: str | None = None  # 可直接复制执行的命令。只是第一步 remediation，不承诺完整解决
    duration_ms: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 契约进代码，不只活在文档里——否则迟早有人构造出 passed+error 这种自相矛盾的结果
        if self.status in ("passed", "skipped") and self.severity != "none":
            raise ValueError(f"{self.status} result must have severity='none', got {self.severity!r}")
        if self.status == "failed" and self.severity == "none":
            raise ValueError("failed result must have severity 'warning' or 'error'")
        if self.status == "passed" and self.fix is not None:
            # 消费方可以假设 fix != None 一定意味着有事要做
            raise ValueError("passed result cannot carry a fix")
        if self.status == "skipped" and not self.summary:
            raise ValueError("skipped result must explain why in summary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "status": self.status,
            "severity": self.severity,
            "summary": self.summary,
            "fix": self.fix,
            "duration_ms": self.duration_ms,
            "meta": self.meta,
        }


def passed(id: str, category: str, title: str, summary: str = "", **meta) -> CheckResult:
    return CheckResult(id=id, category=category, title=title, status="passed",
                       summary=summary, meta=meta)


def failed(id: str, category: str, title: str, summary: str, *,
           severity: Severity = "error", fix: str | None = None, **meta) -> CheckResult:
    return CheckResult(id=id, category=category, title=title, status="failed",
                       severity=severity, summary=summary, fix=fix, meta=meta)


def skipped(id: str, category: str, title: str, reason: str, **meta) -> CheckResult:
    return CheckResult(id=id, category=category, title=title, status="skipped",
                       summary=reason, meta=meta)


@dataclass
class HealthPolicy:
    """把「事实」翻译成「系统状态」的策略。

    分离出来是为了让同一批 CheckResult 能按不同上下文评估——开发机、CI、K8s readiness
    对「什么算必需」的看法不同。将来加 production-policy.yaml 也只是多一个策略实例。
    """

    required: set[str]
    strict: bool = False  # 只改退出码，不改写 severity（改写会让 JSON 输出撒谎）

    def evaluate(self, results: list[CheckResult]) -> HealthStatus:
        # required 的检查必须真的跑过并有结论，否则无法判定、不能谎报健康。
        # 两种「没跑成」都算 unknown：
        #   - 被跳过（present 但 skipped，如 --offline 下的网络检查）；
        #   - 完全缺席（根本没注册/没进 results）——尤其隐蔽：policy 声称需要某项、
        #     但它还没实现时，只查 skipped 永远不触发，会静默返回 healthy。
        # 只看 required：--skip models 之类可选项被跳过，不该把整体顶成 unknown。
        by_id = {r.id: r for r in results}
        for rid in self.required:
            r = by_id.get(rid)
            if r is None or r.status == "skipped":
                return "unknown"

        # 失败按 severity 计入健康，与 required 无关：非必需项（如 models 未缓存）
        # 失败也应让系统 degraded——required 只决定「没跑成→unknown」，不决定
        # 「哪些失败算数」。详见 docs/diagnostics.md。
        failures = [r for r in results if r.status == "failed"]
        if not failures:
            return "healthy"
        if self.strict:
            # strict 下 warning 也算致命——但注意这里改的是判定，不是 r.severity
            return "unhealthy"
        return "unhealthy" if any(r.severity == "error" for r in failures) else "degraded"

    def exit_code(self, results: list[CheckResult]) -> int:
        return EXIT_BY_STATUS[self.evaluate(results)]


# 只列**当前已实现**的检查。声称需要一个还不存在的 id，会因为上面
# 「缺席 required → unknown」的逻辑，把每台干净机器都误报成 UNKNOWN。
# 注意 provider.auth **不在** required 里：它是尽力而为的（很多 provider 没有
# 零成本鉴权端点、doctor 又绝不计费，此时 auth 会 skipped）。真失败（401）仍会
# 按 severity 计入健康；只是「没验证成」不该把整体顶成 unknown。
DEFAULT_POLICY = HealthPolicy(required={
    "python",
    "sqlite",
    "storage",
    "provider.config",
    "provider.network",
})

# id 改名时的兼容映射（旧 id → 新 id）。发布过的 id 不许直接重命名——
# dashboard 里 where check_id='provider.auth' 的历史统计会断掉。
DEPRECATED_CHECK_IDS: dict[str, str] = {}
