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
    p = HealthPolicy(required={"models"})
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    assert p.evaluate(r) == "degraded"
    assert p.exit_code(r) == 1


def test_error_is_unhealthy():
    p = HealthPolicy(required={"storage"})
    r = [failed("storage", "storage", "t", "not writable", severity="error")]
    assert p.evaluate(r) == "unhealthy"
    assert p.exit_code(r) == 2


def test_missing_required_check_is_unknown_not_healthy():
    """policy 声称需要一项、但它根本没进 results（还没实现/没注册）——不能静默
    返回 healthy。这是 DEFAULT_POLICY 曾把 provider.* 列为必需却没实现时的真实 bug。"""
    p = HealthPolicy(required={"provider.auth"})
    r = [passed("python", "runtime", "t")]  # provider.auth 完全缺席
    assert p.evaluate(r) == "unknown"
    assert p.exit_code(r) == 3


def test_non_required_failure_still_counts():
    """required 只决定「没跑成→unknown」，不决定「哪些失败算数」。
    非必需项（models）失败也应让系统 degraded——这是 plan 明确要的行为，
    锁死它，避免以后被「只统计 required 的失败」的改动带偏。"""
    p = HealthPolicy(required=set())
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    assert p.evaluate(r) == "degraded"


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
    p = HealthPolicy(required={"models"}, strict=True)
    r = [failed("models", "model", "t", "not cached", severity="warning")]
    assert p.evaluate(r) == "unhealthy"


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
    # offline：provider 检查会发真实网络请求，测试里一律跳过
    for r in run(offline=True):
        assert r.duration_ms is not None and r.duration_ms >= 0


def test_check_order_is_stable():
    """doctor 的输出就是产品体验，顺序不能因为重构 import 就漂移。"""
    assert [r.id for r in run(offline=True)] == [r.id for r in run(offline=True)]


# ---------------------------------------------------------------- 依赖方向

def test_checks_do_not_import_huggingface():
    """诊断层不能依赖模型下载 SDK——否则离线部署时诊断跟着炸，
    而那正是最需要它的时刻。

    必须在子进程里断言：同一进程中别的测试早就 import 过 huggingface_hub，
    在本进程查 sys.modules 只会得到一个永远 skip 的假测试。
    """
    code = (
        "import sys;"
        "import ragkernel.checks.runtime, ragkernel.checks.storage, "
        "ragkernel.checks.provider, ragkernel.checks.models;"
        "import ragkernel.diagnostics;"
        "leaked=[m for m in sys.modules if 'huggingface' in m or 'torch' in m];"
        "print(leaked);"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"诊断层泄漏了重依赖: {r.stdout.strip()}"


def test_default_policy_only_requires_existing_checks():
    """DEFAULT_POLICY 不能列还没实现的 id——否则「缺席 required → unknown」会把
    每台干净机器误报成 UNKNOWN。provider.* 在 provider 检查落地那个 PR 才加入。"""
    from ragkernel.diagnostics import run

    registered = {r.id for r in run(offline=True)}  # offline 也会返回全部 id（网络项为 skipped）
    assert DEFAULT_POLICY.required <= registered, (
        f"required 里有未注册的检查: {DEFAULT_POLICY.required - registered}"
    )
    assert {"python", "sqlite", "storage"} <= DEFAULT_POLICY.required


# ---------------------------------------------------------------- checks: storage

def test_storage_write_check_catches_unwritable_parent(tmp_path, monkeypatch):
    """父目录不可创建子目录时，doctor 必须报 failed。
    os.access 会对「parent 其实是个文件」这类情况撒谎（文件可写 ≠ 能在其下建目录），
    所以改成实际写临时文件——这个用例正是 os.access 会放行、实写会抓到的场景。"""
    parent_is_a_file = tmp_path / "not-a-dir"
    parent_is_a_file.write_text("i am a file, not a directory")
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(parent_is_a_file / "data"))

    from ragkernel.checks.storage import check_storage

    r = check_storage()
    assert r.status == "failed", "把文件当父目录，实写会失败，应报 failed"
    assert "不可写" in r.summary


def test_storage_ok_when_parent_writable(tmp_path, monkeypatch):
    """data 目录尚未创建、但父目录可写：passed 且标注 exists=False（不实际建目录）。"""
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path / "data"))

    from ragkernel.checks.storage import check_storage

    r = check_storage()
    assert r.status == "passed"
    assert r.meta["exists"] is False
    # doctor 只读：探测不应真的把 data 目录建出来
    assert not (tmp_path / "data").exists()
