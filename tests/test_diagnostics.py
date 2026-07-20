"""诊断契约的回归测试。

这个 schema 是对外接口（doctor CLI / --json / K8s probe / 未来 dashboard），
所以不变量必须是可执行的断言，不能只写在文档里。
每个测试用 RAGKERNEL_DATA_DIR 隔离到临时目录，绝不碰用户 KB。
"""

import subprocess
import sys

import pytest

from ragkernel.diagnostics import run
from ragkernel.diagnostics.runner import CheckSpec
from ragkernel.diagnostics.schema import (
    DEFAULT_POLICY,
    CheckResult,
    HealthPolicy,
    failed,
    passed,
    skipped,
)


# ---------------------------------------------------------------- 不变量

def test_passed_cannot_carry_error_severity():
    """status 与 severity 正交，但不许自相矛盾。"""
    with pytest.raises(ValueError, match="severity='none'"):
        CheckResult(id="x", category="c", title="t", status="passed", severity="error")


def test_skipped_cannot_carry_severity():
    with pytest.raises(ValueError, match="severity='none'"):
        CheckResult(id="x", category="c", title="t", status="skipped",
                    severity="warning", summary="why")


def test_failed_must_carry_severity():
    with pytest.raises(ValueError, match="warning.*error"):
        CheckResult(id="x", category="c", title="t", status="failed", severity="none")


def test_passed_cannot_carry_fix():
    """消费方可以假设 fix != None 一定意味着有事要做。"""
    with pytest.raises(ValueError, match="cannot carry a fix"):
        CheckResult(id="x", category="c", title="t", status="passed", fix="do something")


def test_skipped_must_explain_why():
    """只说「跳过」不说原因，等于没说。"""
    with pytest.raises(ValueError, match="explain why"):
        CheckResult(id="x", category="c", title="t", status="skipped")


def test_helpers_build_valid_results():
    assert passed("a", "c", "t").severity == "none"
    assert failed("a", "c", "t", "boom").severity == "error"
    assert failed("a", "c", "t", "boom", severity="warning").status == "failed"
    assert skipped("a", "c", "t", "because").summary == "because"


# ---------------------------------------------------------------- 退出码策略

def _policy(**kw):
    return HealthPolicy(required={"req"}, **kw)


def test_all_passed_is_healthy():
    assert _policy().evaluate([passed("req", "c", "t")]) == "healthy"
    assert _policy().exit_code([passed("req", "c", "t")]) == 0


def test_warning_only_is_degraded_not_unhealthy():
    """模型没缓存这类：确实没通过，但系统健康。
    一刀切「任意 ✗ 非零」会误伤 docker build 里模型下载之前的 doctor。"""
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    assert _policy().evaluate(r) == "degraded"
    assert _policy().exit_code(r) == 1


def test_error_is_unhealthy():
    r = [failed("storage", "storage", "t", "not writable", severity="error")]
    assert _policy().evaluate(r) == "unhealthy"
    assert _policy().exit_code(r) == 2


def test_skipped_required_check_is_unknown_not_healthy():
    """「没测」不能谎报成「健康」——否则部署脚本以为 provider 没问题。"""
    r = [skipped("req", "c", "t", "--offline")]
    assert _policy().evaluate(r) == "unknown"
    assert _policy().exit_code(r) == 3


def test_skipped_optional_check_does_not_trigger_unknown():
    """--skip models 不该把整体顶成 unknown。"""
    r = [passed("req", "c", "t"), skipped("optional", "c", "t", "--skip")]
    assert _policy().evaluate(r) == "healthy"


def test_strict_promotes_warning_to_unhealthy():
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    assert _policy(strict=True).evaluate(r) == "unhealthy"


def test_strict_never_turns_skipped_into_failed():
    """--offline --strict 仍是 UNKNOWN(3)，不是 UNHEALTHY(2)。
    「没测」永远不该因为 strict 就变成「失败」。"""
    r = [skipped("req", "c", "t", "--offline")]
    assert _policy(strict=True).evaluate(r) == "unknown"
    assert _policy(strict=True).exit_code(r) == 3


def test_strict_does_not_mutate_severity():
    """strict 改的是退出码策略，不是事实。改写 severity 会让 JSON 输出撒谎。"""
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    _policy(strict=True).evaluate(r)
    assert r[0].severity == "warning"


# ---------------------------------------------------------------- runner

def test_crashing_check_becomes_failed_and_others_still_run(monkeypatch):
    """doctor 的职责不是证明代码没 bug，而是在代码有 bug 时依然告诉用户哪里坏了。"""
    def boom() -> CheckResult:
        raise RuntimeError("kaboom")

    specs = [
        CheckSpec("crash", "runtime", "崩溃项", boom),
        CheckSpec("fine", "runtime", "正常项", lambda: passed("fine", "runtime", "正常项")),
    ]
    monkeypatch.setattr("ragkernel.diagnostics.runner._registry", lambda: specs)

    results = run()
    assert [r.id for r in results] == ["crash", "fine"]

    crash = results[0]
    assert crash.status == "failed" and crash.severity == "error"
    assert crash.meta["exception_type"] == "RuntimeError"
    assert "kaboom" in crash.meta["exception"]
    # 后面的检查照常跑完，没有被异常打断
    assert results[1].status == "passed"


def test_offline_skips_network_checks(monkeypatch):
    specs = [
        CheckSpec("net", "provider", "网络项", lambda: passed("net", "provider", "网络项"),
                  network=True),
        CheckSpec("local", "runtime", "本地项", lambda: passed("local", "runtime", "本地项")),
    ]
    monkeypatch.setattr("ragkernel.diagnostics.runner._registry", lambda: specs)

    results = run(offline=True)
    assert results[0].status == "skipped" and "offline" in results[0].summary
    assert results[1].status == "passed"


def test_minimal_runs_only_preflight_subset(monkeypatch):
    specs = [
        CheckSpec("pre", "runtime", "预检项", lambda: passed("pre", "runtime", "预检项"),
                  minimal=True),
        CheckSpec("full", "model", "完整项", lambda: passed("full", "model", "完整项")),
    ]
    monkeypatch.setattr("ragkernel.diagnostics.runner._registry", lambda: specs)

    assert [r.id for r in run(minimal=True)] == ["pre"]


def test_every_result_is_timed():
    for r in run():
        assert r.duration_ms is not None and r.duration_ms >= 0


def test_check_order_is_stable():
    """doctor 的输出就是产品体验，顺序不能因为重构 import 就漂移。"""
    assert [r.id for r in run()] == [r.id for r in run()]


# ---------------------------------------------------------------- 依赖方向

def test_checks_do_not_import_huggingface():
    """诊断层不能依赖模型下载 SDK——否则离线部署时诊断跟着炸，
    而那正是最需要它的时刻。

    必须在子进程里断言：同一进程中别的测试早就 import 过 huggingface_hub，
    在本进程查 sys.modules 只会得到一个永远 skip 的假测试。
    """
    code = (
        "import sys;"
        "import ragkernel.checks.runtime, ragkernel.checks.storage;"
        "import ragkernel.diagnostics;"
        "leaked=[m for m in sys.modules if 'huggingface' in m or 'torch' in m];"
        "print(leaked);"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"诊断层泄漏了重依赖: {r.stdout.strip()}"


def test_default_policy_covers_core_checks():
    assert {"python", "sqlite", "storage"} <= DEFAULT_POLICY.required
