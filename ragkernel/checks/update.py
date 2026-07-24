"""版本更新检查。

**severity 由 manifest 决定，不写死**：普通更新是 warning，manifest 声明
`security: critical`（CVE 修复）时升成 error。「有新版本可用」和「你正在跑一个有已知
漏洞的版本」是两件事，写死 warning 会让后者永远淹没在前者里。
diagnostics/schema.py 本来就把 severity（事实）与 required（策略）设计成正交两维。

**但无论如何不进 DEFAULT_POLICY.required**：--offline 下本项会 skipped，而按
「required 缺席或 skipped → UNKNOWN」的判定，每台离线机器都会被误报成 UNKNOWN。
severity 升级只影响 degraded / unhealthy 的判定，不影响这一点。
"""

from ..diagnostics.runner import CheckSpec
from ..diagnostics.schema import CheckResult, failed, passed, skipped

CATEGORY = "update"


def check_update() -> CheckResult:
    from .. import update

    st = update.check()

    if st.disabled:
        return skipped("update", CATEGORY, "版本更新",
                       "已在配置中关闭（update.check=false）")
    if st.error:
        # 查不到版本不是系统故障——网络不通、内网无 endpoint 都是正常部署形态
        return skipped("update", CATEGORY, "版本更新",
                       f"无法获取版本清单：{st.error}", endpoint=st.endpoint)
    if not st.latest:
        return skipped("update", CATEGORY, "版本更新", "版本清单为空", endpoint=st.endpoint)

    if not st.available:
        return passed("update", CATEGORY, "版本更新",
                      f"已是最新（{st.current}）", current=st.current, latest=st.latest)

    critical = (st.manifest or {}).get("security") == "critical"
    gate = update.can_upgrade(st.manifest)
    fix = "ragkernel upgrade" if gate.allowed else None
    if not gate.allowed:
        summary = f"有新版本 {st.latest}，但当前不能直升：{gate.reason}"
    elif critical:
        summary = f"安全更新 {st.latest} 可用（本机 {st.current}）——建议尽快升级"
    else:
        summary = f"有新版本 {st.latest}（本机 {st.current}）"

    return failed("update", CATEGORY, "版本更新", summary,
                  severity="error" if critical else "warning", fix=fix,
                  current=st.current, latest=st.latest, security=(st.manifest or {}).get("security"),
                  blocked=not gate.allowed)


CHECKS = [
    CheckSpec("update", CATEGORY, "版本更新", check_update, network=True),
]
