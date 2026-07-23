"""升级子系统：状态机、闸门、恢复、执行层。

每个用例都对应一个具体的翻车场景，不是为覆盖率而写——这套东西平时不跑，
只在升级那一刻跑，而那一刻出错的代价最高。
"""

import json
import os
import subprocess
import sys

import pytest

from ragkernel import capabilities, config, update


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把 .ragkernel/ 隔离到 tmp_path，绝不碰真实仓库的升级状态。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 分层边界


def test_update_module_does_not_pull_in_flask():
    """update.py 必须是纯 Python：webapp / cli / MCP 都调用它，它谁也不认。
    否则 `ragkernel upgrade` 这个纯 CLI 动作会被迫拖起整个 Web 应用。

    必须在**干净的子进程**里验——本进程里别的测试早就 import 过 webapp 了，
    直接查 sys.modules 会永远通过，是个假测试。
    """
    code = ("import sys, ragkernel.update, ragkernel.capabilities; "
            "sys.exit(1 if 'flask' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"update/capabilities 间接 import 了 flask\n{r.stderr}"


# ---------------------------------------------------------------- 运行形态


def test_docker_wins_over_systemd(root, monkeypatch):
    """容器里跑 systemd supervisor 会同时命中两个判据，此时正确答案永远是 docker——
    容器内 git pull 无论如何都是错的。判定顺序即优先级，不可调换。"""
    monkeypatch.setattr(update.Path, "exists", lambda self: str(self) == "/.dockerenv")
    monkeypatch.setenv("INVOCATION_ID", "deadbeef")
    assert update.runtime_mode() == "docker"


def test_systemd_detected_without_docker(root, monkeypatch):
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.setenv("INVOCATION_ID", "deadbeef")
    assert update.runtime_mode() == "systemd"


def test_plain_process_is_default(root, monkeypatch):
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert update.runtime_mode() == "process"


def test_docker_refuses_and_hands_back_a_command(root):
    """Docker 形态不自更新，但必须把用户该敲的命令给全——前端不拼命令。"""
    with pytest.raises(update.UpdateRefused) as e:
        update.DockerExecutor().apply("v9.9.9", on_event=lambda m: None)
    assert "docker compose" in e.value.command
    assert update.can_self_update("docker") is False


# ---------------------------------------------------------------- semver


@pytest.mark.parametrize("latest,current,expected", [
    ("0.10.0", "0.9.0", True),    # 字符串比较会判错，必须按三元组
    ("0.9.0", "0.10.0", False),
    ("1.0.0", "1.0.0", False),
    ("v0.4.0", "0.3.0", True),    # 带 v 前缀也要能比
    ("nightly", "0.3.0", False),  # 非 semver 一律「无更新」，不报错
    (None, "0.3.0", False),
])
def test_version_comparison(latest, current, expected):
    assert update.is_newer(latest, current) is expected


# ---------------------------------------------------------------- 状态机


def test_illegal_transition_raises_and_does_not_persist(root):
    """completed → updating 这类穿越一旦落盘，异常恢复就没法判断现场了。"""
    update.transition("draining", update_id="u_x")
    update.transition("updating")
    update.transition("restarting")
    update.transition("completed")

    with pytest.raises(update.UpdateStateError):
        update.transition("updating")
    assert update.read_state()["state"] == "completed"


def test_legal_happy_path(root):
    for s in ("draining", "updating", "restarting", "completed", "idle"):
        update.transition(s)
    assert update.read_state()["state"] == "idle"


def test_corrupt_state_file_falls_back_to_idle(root):
    """半截 JSON（崩在写盘途中）不能让恢复逻辑自己也崩。"""
    p = config.rk_path("state", "update.json", create=True)
    p.write_text("{not json", encoding="utf-8")
    assert update.read_state()["state"] == "idle"


def test_state_write_is_atomic(root):
    """写状态用 tmp + rename：升级状态存在的意义就是崩溃后能恢复。"""
    update.transition("draining", update_id="u_1")
    p = config.rk_path("state", "update.json")
    assert json.loads(p.read_text(encoding="utf-8"))["update_id"] == "u_1"
    assert not p.with_suffix(".json.tmp").exists()


def test_event_payload_is_the_shared_protocol():
    """Web/CLI/桌面端渲染同一份载荷；结构由 update.py 定义，各前端不另造。"""
    e = update.event("u_1", "updating", "sync", "正在同步依赖", 0.6)
    assert set(e) == {"update_id", "state", "stage", "message", "progress", "ts"}


# ---------------------------------------------------------------- 兼容性闸门


def _manifest(**over) -> dict:
    m = {
        "version": "9.9.9",
        "requires": {"python": ">=3.12", "ragkernel_schema": config.SCHEMA_VERSION,
                     "min_upgradable_from": "0.0.1"},
        "upgrade_strategy": {"restart_required": True, "migration_required": False},
    }
    m.update(over)
    return m


def test_gate_allows_a_normal_upgrade():
    assert update.can_upgrade(_manifest()).allowed


def test_gate_blocks_on_schema_gap():
    """跨了不止一代数据形态：中间版本的迁移没跑过，直升会把库带到没人验证过的状态。"""
    m = _manifest(requires={"ragkernel_schema": config.SCHEMA_VERSION + 5,
                            "min_upgradable_from": "0.0.1"})
    r = update.can_upgrade(m)
    assert not r.allowed
    assert [b["type"] for b in r.blockers] == ["schema"]


def test_gate_blocks_when_too_old_to_jump():
    m = _manifest(requires={"ragkernel_schema": config.SCHEMA_VERSION,
                            "min_upgradable_from": "99.0.0"})
    r = update.can_upgrade(m)
    assert not r.allowed
    assert any(b["type"] == "min_upgradable_from" for b in r.blockers)


def test_gate_reports_multiple_blockers_at_once():
    """阻塞原因会并存——单字符串 reason 只能报一个，前端也没法按 type 差异化渲染。"""
    m = _manifest(requires={"ragkernel_schema": config.SCHEMA_VERSION + 5,
                            "min_upgradable_from": "99.0.0"})
    r = update.can_upgrade(m)
    assert {b["type"] for b in r.blockers} == {"schema", "min_upgradable_from"}
    assert "；" in r.reason


def test_gate_warns_about_migration_and_security():
    m = _manifest(security="critical",
                  upgrade_strategy={"restart_required": True, "migration_required": True})
    r = update.can_upgrade(m)
    assert r.allowed
    assert {w["type"] for w in r.warnings} == {"migration", "security"}


def test_gate_rejects_empty_manifest():
    assert not update.can_upgrade({}).allowed


# ---------------------------------------------------------------- 缓存 / ETag


def test_check_sends_if_none_match_and_honours_304(root, monkeypatch):
    """第二次检查带 ETag，服务端 304 时不重下 manifest。GitHub 未鉴权 API 是
    60 req/hr/IP，多实例同 NAT 出口很容易打满。"""
    calls: list[str | None] = []

    def fake_fetch(endpoint, etag):
        calls.append(etag)
        if etag is None:
            return _manifest(version="9.9.9"), 'W/"abc"'
        return None, etag  # 304

    monkeypatch.setattr(update, "_fetch", fake_fetch)
    monkeypatch.setattr(update, "_is_fresh", lambda *a: False)  # 逼它每次都发请求

    first = update.check()
    assert first.latest == "9.9.9" and calls == [None]

    second = update.check()
    assert calls == [None, 'W/"abc"']
    assert second.latest == "9.9.9", "304 后必须沿用缓存里的 manifest"


def test_check_uses_cache_within_ttl(root, monkeypatch):
    hits = []
    monkeypatch.setattr(update, "_fetch",
                        lambda e, t: (hits.append(1), (_manifest(), "e"))[1])
    update.check()
    update.check()
    assert len(hits) == 1, "TTL 内不该再发请求"


def test_network_failure_is_silent(root, monkeypatch):
    """版本检查坏掉不该影响任何主流程——错误只落进缓存，UI 静默不显示 banner。"""
    def boom(endpoint, etag):
        raise OSError("no route to host")

    monkeypatch.setattr(update, "_fetch", boom)
    st = update.check(force=True)
    assert st.error and "no route to host" in st.error
    assert st.available is False


def test_check_disabled_by_config(root, monkeypatch):
    """离线 / 内网部署必须能彻底关掉联网查版本。"""
    monkeypatch.setattr(update, "_cfg", lambda: {"check": False})
    monkeypatch.setattr(update, "_fetch", lambda e, t: pytest.fail("不该发请求"))
    st = update.check(force=True)
    assert st.disabled and not st.available


# ---------------------------------------------------------------- 启动恢复


def test_recover_completes_when_version_matches(root):
    update.transition("draining", update_id="u_ok", to_version=update.CURRENT)
    update.transition("updating")
    update.transition("restarting")
    update.enter_maintenance(reason="update", update_id="u_ok")

    out = update.recover_update_state()
    assert out["action"] == "completed"
    assert update.read_state()["state"] == "completed"


def test_recover_fails_when_restart_did_not_take_effect(root):
    """进程起来了、跑的还是老版本 —— 换代码没生效，不能记成成功。"""
    update.transition("draining", update_id="u_bad", to_version="99.9.9")
    update.transition("updating")
    update.transition("restarting")

    out = update.recover_update_state()
    assert out["action"] == "failed" and out["reason"] == "version_mismatch"


def test_recover_keeps_maintenance_after_a_crashed_upgrade(root):
    """git 换代码完成、restart 之前 crash——此时 migration 尚未跑完。
    只看「pid 不存在」就删掉 maintenance 正常开门，等于把半迁移的库投入服务。"""
    update.transition("draining", update_id="u_crash", to_version="99.9.9")
    update.transition("updating")
    update.enter_maintenance(reason="update", update_id="u_crash")

    out = update.recover_update_state()
    assert out["action"] == "failed" and out["reason"] == "interrupted"
    assert out["maintenance"] == "kept"
    assert update.maintenance(), "升级没走完，维护态必须保留并提示人工介入"


def test_recover_clears_stale_maintenance_from_dead_process(root):
    """升级正常结束但进程已不在：残留的维护态会让服务第二天还在 503。"""
    update.transition("draining", update_id="u_done", to_version=update.CURRENT)
    update.transition("updating")
    update.transition("restarting")
    update.enter_maintenance(reason="update", update_id="u_done")

    # 伪造一个不存在的 pid
    p = config.rk_path("state", "maintenance.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    data["pid"] = 999999
    p.write_text(json.dumps(data), encoding="utf-8")

    out = update.recover_update_state()
    assert out["maintenance"] == "cleared"
    assert update.maintenance() == {}


def test_recover_is_a_noop_on_a_clean_start(root):
    assert update.recover_update_state() == {}


def test_recover_audits_what_it_did(root):
    """支持场景要能从 update_id 反查——恢复动作本身也得留痕。"""
    logged = []
    update.transition("draining", update_id="u_a", to_version="99.9.9")
    update.transition("updating")
    update.recover_update_state(audit_log=lambda k, p: logged.append((k, p)))
    assert logged[0][0] == "update_recovered"
    assert logged[0][1]["update_id"] == "u_a"


# ---------------------------------------------------------------- 执行层


def test_local_executor_passes_cad_extra_through(root, monkeypatch):
    """不回读 extras 的话，升级会悄悄把 CAD extra 卸掉。"""
    config.rk_path("state", "install.json", create=True).write_text(
        json.dumps({"schema_version": 2, "extras": ["cad"]}), encoding="utf-8")
    (root / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    seen = {}

    class FakeProc:
        stdout = iter(["==> 更新分支：main\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(update.subprocess, "Popen",
                        lambda cmd, **kw: (seen.update(cmd=cmd), FakeProc())[1])
    update.LocalExecutor().apply("v9.9.9", on_event=lambda m: None)
    assert "--cad" in seen["cmd"]
    assert "--ref" in seen["cmd"] and "v9.9.9" in seen["cmd"]


def test_local_executor_treats_exit_3_as_failure(root, monkeypatch):
    """install.sh 退出 3 = 代码未变更。不是错误，但绝不能当成功——
    报成功会让状态机把「什么都没发生」记成 completed。"""
    (root / "install.sh").write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")

    class FakeProc:
        stdout = iter([])

        def wait(self):
            return 3

    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    with pytest.raises(update.UpdateError, match="代码未变更"):
        update.LocalExecutor().apply("v9.9.9", on_event=lambda m: None)


def test_missing_installer_is_reported_clearly(root):
    with pytest.raises(update.UpdateError, match="install.sh"):
        update.LocalExecutor().apply("v9.9.9", on_event=lambda m: None)


# ---------------------------------------------------------------- 编排


def _fake_executor(monkeypatch, *, fail=False, mode="systemd"):
    class E(update.UpdateExecutor):
        def apply(self, target, *, on_event):
            on_event("同步依赖…")
            if fail:
                raise update.UpdateError("boom")
            return {"extras": []}

    monkeypatch.setattr(update, "executor_for", lambda m=None: E())
    # 必须 pin 运行形态：apply() 现在会在置维护态之前拒绝 docker，而仓库里有 Dockerfile——
    # 谁在容器里跑 pytest，真实 runtime_mode() 就是 docker，这些用例会莫名其妙全挂。
    monkeypatch.setattr(update, "runtime_mode", lambda: mode)


def test_apply_runs_the_full_sequence(root, monkeypatch):
    _fake_executor(monkeypatch)
    events = []

    res = update.UpdateController().apply("v9.9.9", to_version="9.9.9", on_event=events.append)

    assert res["restart_required"] and res["restart_handled_by"] == "systemd"
    assert [e["state"] for e in events] == ["draining", "updating", "updating", "restarting"]
    assert {e["update_id"] for e in events} == {res["update_id"]}, "所有事件共享同一个 update_id"
    assert update.read_state()["state"] == "restarting"
    assert update.maintenance(), "换了代码但还没重启，此时对外必须仍是维护态"


def test_apply_keeps_maintenance_on_failure(root, monkeypatch):
    _fake_executor(monkeypatch, fail=True)
    with pytest.raises(update.UpdateError):
        update.UpdateController().apply("v9.9.9", to_version="9.9.9")

    st = update.read_state()
    assert st["state"] == "failed" and "boom" in st["error"]
    assert update.maintenance(), "升级没走完，不能假装无事发生地开门"


def test_apply_refuses_docker_before_entering_maintenance(root, monkeypatch):
    """**拒绝要发生在置维护态之前。** 否则 docker 形态下调用 apply()（Web API 或任何
    没先问 can_self_update 的调用方）会走完「置维护态 → 被拒 → 转 failed 且维护态保留」，
    把服务卡在 503 上——而这个动作本就不该开始。"""
    monkeypatch.setattr(update, "runtime_mode", lambda: "docker")
    with pytest.raises(update.UpdateRefused) as e:
        update.UpdateController().apply("v9.9.9", to_version="9.9.9")

    assert "docker compose" in e.value.command
    assert update.maintenance() == {}, "被拒绝的升级不该留下维护态"
    assert update.read_state()["state"] == "idle", "根本没开始，状态不该动"


def test_summary_never_touches_the_network(root, monkeypatch):
    """summary 是 /api/system/info、ragkernel version、MCP get_server_info 的共同数据源，
    三者都要求即时返回——Web 端点卡 8 秒超时不可接受，agent 反复调更不能每次出网。"""
    monkeypatch.setattr(update, "_fetch", lambda e, t: pytest.fail("summary 不得联网"))
    s = update.summary()
    assert s["server"]["version"] == update.CURRENT


def test_oversized_manifest_is_rejected(root, monkeypatch):
    """endpoint 是本模块唯一的外部输入面：被劫持或指错地址时不该把内存读爆。"""
    class FakeResp:
        headers = {}

        def read(self, n=-1):
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    st = update.check(force=True)
    assert st.error and "拒绝解析" in st.error
    assert st.available is False, "拒绝解析后不能报出可升级"


def test_apply_refuses_when_another_upgrade_is_in_flight(root, monkeypatch):
    _fake_executor(monkeypatch)
    update.transition("draining", update_id="u_other")
    with pytest.raises(update.UpdateError, match="进行中"):
        update.UpdateController().apply("v9.9.9", to_version="9.9.9")


def test_apply_recovers_from_a_previous_terminal_state(root, monkeypatch):
    """上一轮结束在 completed/failed，下一次升级要能正常开始。"""
    _fake_executor(monkeypatch)
    update.transition("draining", update_id="u_old")
    update.transition("failed")

    res = update.UpdateController().apply("v9.9.9", to_version="9.9.9")
    assert res["update_id"] != "u_old"


def test_apply_audits_with_update_id(root, monkeypatch):
    _fake_executor(monkeypatch)
    logged = []
    res = update.UpdateController(audit_log=lambda k, p: logged.append((k, p))).apply(
        "v9.9.9", to_version="9.9.9")
    assert logged[-1][0] == "update_applied"
    assert logged[-1][1]["update_id"] == res["update_id"]


def test_audit_failure_does_not_break_the_upgrade(root, monkeypatch):
    _fake_executor(monkeypatch)

    def bad_audit(kind, payload):
        raise RuntimeError("audit db locked")

    res = update.UpdateController(audit_log=bad_audit).apply("v9.9.9", to_version="9.9.9")
    assert res["restart_required"]


# ---------------------------------------------------------------- capabilities


def test_capabilities_are_probed_not_hardcoded(monkeypatch):
    """只装基础依赖时 CAD 必须报 false——写死等于宣称一个没装的能力，
    agent 照着调就是一次必然失败的工具调用。"""
    monkeypatch.setattr(capabilities, "_probe", lambda m: False)
    caps = capabilities.detect(force=True)
    assert caps["cad"]["step"] is False and caps["cad"]["stl"] is False
    assert caps["ocr"]["enabled"] is False and caps["ocr"]["provider"] is None


def test_capabilities_are_nested_for_future_grading():
    caps = capabilities.detect(force=True)
    assert isinstance(caps["cad"], dict)
    assert caps["cad"]["topology"] is False, "拓扑抽取未支持，要诚实声明"


def test_capabilities_are_cached(monkeypatch):
    """OCP 的 import 很重，而 get_server_info 是 agent 会反复调的只读工具。"""
    calls = []
    monkeypatch.setattr(capabilities, "_probe", lambda m: (calls.append(m), True)[1])
    capabilities.detect(force=True)
    n = len(calls)
    for _ in range(10):
        capabilities.detect()
    assert len(calls) == n, "TTL 内不该重复探测"


# ---------------------------------------------------------------- 摘要


def test_summary_shape_is_the_client_contract(root, monkeypatch):
    monkeypatch.setattr(update, "_cfg", lambda: {"check": False})
    s = update.summary()
    assert set(s) == {"server", "update", "runtime", "state"}
    assert s["server"]["version"] == update.CURRENT
    assert s["server"]["schema"] == config.SCHEMA_VERSION
    assert s["runtime"]["mode"] in ("docker", "systemd", "process")


def test_summary_hands_docker_users_the_command(root, monkeypatch):
    monkeypatch.setattr(update, "_cfg", lambda: {"check": False})
    monkeypatch.setattr(update, "runtime_mode", lambda: "docker")
    s = update.summary()
    assert s["runtime"]["can_self_update"] is False
    assert "docker compose" in s["runtime"]["update_command"]


# ---------------------------------------------------------------- doctor 集成


def test_update_check_is_not_required_by_policy():
    """**这条最重要**：update 是 network=True 的检查，--offline 下会 skipped。
    一旦进了 required，按「required 缺席或 skipped → UNKNOWN」的判定，
    每台离线机器都会被误报成 UNKNOWN。schema.py 里写着这条警告。"""
    from ragkernel import diagnostics

    assert "update" not in diagnostics.DEFAULT_POLICY.required


def test_update_check_is_registered():
    from ragkernel.diagnostics.runner import _registry

    assert "update" in {s.id for s in _registry()}


def test_update_check_is_marked_as_network():
    """没标 network 的话 --offline 会真的去联网，离线部署每次 doctor 都要等超时。"""
    from ragkernel.diagnostics.runner import _registry

    spec = next(s for s in _registry() if s.id == "update")
    assert spec.network is True


def test_available_update_degrades_but_never_unhealthy(root, monkeypatch):
    """有新版本只是 degraded：能用、但该升。把它算成 unhealthy 会让 K8s 探针
    在每次上游发版时踢掉所有 pod。"""
    from ragkernel.checks import update as check_mod
    from ragkernel.diagnostics.schema import HealthPolicy, passed

    monkeypatch.setattr(update, "check", lambda force=False: update.UpdateStatus(
        latest="99.0.0", available=True, manifest=_manifest(version="99.0.0")))
    r = check_mod.check_update()
    assert r.status == "failed" and r.severity == "warning"

    policy = HealthPolicy(required=set())
    assert policy.evaluate([passed("x", "runtime", "t"), r]) == "degraded"


def test_security_critical_escalates_to_unhealthy(root, monkeypatch):
    """「有新版本可用」和「你在跑一个有已知漏洞的版本」是两件事——
    写死 warning 会让后者永远淹没在前者里。"""
    from ragkernel.checks import update as check_mod
    from ragkernel.diagnostics.schema import HealthPolicy, passed

    monkeypatch.setattr(update, "check", lambda force=False: update.UpdateStatus(
        latest="99.0.0", available=True,
        manifest=_manifest(version="99.0.0", security="critical")))
    r = check_mod.check_update()
    assert r.severity == "error"
    assert HealthPolicy(required=set()).evaluate([passed("x", "runtime", "t"), r]) == "unhealthy"


def test_check_disabled_shows_as_skipped_not_failed(root, monkeypatch):
    """离线部署主动关掉版本检查，不该在体检报告里显示成故障。"""
    from ragkernel.checks import update as check_mod

    monkeypatch.setattr(update, "check",
                        lambda force=False: update.UpdateStatus(disabled=True))
    assert check_mod.check_update().status == "skipped"


def test_network_error_shows_as_skipped_not_failed(root, monkeypatch):
    """内网无 endpoint、防火墙拦截都是正常部署形态，不是系统故障。"""
    from ragkernel.checks import update as check_mod

    monkeypatch.setattr(update, "check",
                        lambda force=False: update.UpdateStatus(error="OSError: unreachable"))
    assert check_mod.check_update().status == "skipped"


def test_doctor_json_carries_update_section(root, monkeypatch):
    """客户把 doctor --json 发过来做支持时，版本上下文要已经在里面，不必再问一轮。"""
    from ragkernel import doctor
    from ragkernel.diagnostics.schema import HealthPolicy, passed

    monkeypatch.setattr(update, "_cfg", lambda: {"check": False})
    d = json.loads(doctor.render_json([passed("a", "runtime", "t")],
                                      HealthPolicy(required=set()), verbose=False))
    assert set(d["update"]) >= {"current", "latest", "channel", "endpoint", "checked_at"}
    assert d["update"]["current"] == update.CURRENT


def test_doctor_json_never_touches_the_network(root, monkeypatch):
    """doctor --offline 承诺不碰网络。渲染报告时若走 check()，缓存一过期就会发起
    网络请求——离线机器上要等满超时，且直接违反 --offline 的契约。"""
    from ragkernel import doctor
    from ragkernel.diagnostics.schema import HealthPolicy, passed

    monkeypatch.setattr(update, "_fetch", lambda e, t: pytest.fail("doctor 渲染不得联网"))
    # 缓存里有过期数据 —— check() 在这种情况下会去 fetch，cached_status() 不会
    update._cache_write({"checked_at": "2020-01-01T00:00:00Z", "etag": None,
                         "manifest": _manifest(version="99.0.0"), "error": None})

    d = json.loads(doctor.render_json([passed("a", "runtime", "t")],
                                      HealthPolicy(required=set()), verbose=False))
    assert d["update"]["latest"] == "99.0.0"


def test_cached_status_does_not_fetch(root, monkeypatch):
    monkeypatch.setattr(update, "_fetch", lambda e, t: pytest.fail("不该联网"))
    assert update.cached_status().latest is None


def test_doctor_json_survives_a_broken_update_lookup(root, monkeypatch):
    """诊断输出绝不能因为版本查询挂了就整份失败——那正是最需要报告的时候。"""
    from ragkernel import doctor
    from ragkernel.diagnostics.schema import HealthPolicy, passed

    monkeypatch.setattr(update, "cached_status", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    d = json.loads(doctor.render_json([passed("a", "runtime", "t")],
                                      HealthPolicy(required=set()), verbose=False))
    assert "error" in d["update"]


# ---------------------------------------------------------------- 维护态中间件


@pytest.fixture
def client(root):
    from ragkernel import webapp

    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_maintenance_blocks_heavy_endpoints(client, root):
    update.enter_maintenance(reason="update", update_id="u_1", detail="升级至 v9.9.9")
    r = client.post("/api/ask", json={})
    assert r.status_code == 503
    body = r.get_json()
    assert body["error"] == "maintenance" and body["update_id"] == "u_1"


def test_maintenance_keeps_health_and_info_open(client, root):
    """运维与 K8s 探针绝不能在升级窗口里变瞎——那正是最需要它们的时刻。"""
    update.enter_maintenance(reason="update", update_id="u_1")
    assert client.get("/health").status_code == 200


def test_no_maintenance_means_no_interference(client, root):
    """没在维护时，中间件必须完全透明——它挡的是 503，不是把正常请求也拦下。"""
    r = client.post("/api/ask", json={})
    assert r.status_code != 503


def test_maintenance_records_owner_and_pid(root):
    """crash 后残留文件要能被识别回收，owner 留给未来的 updater daemon 区分持有者。"""
    update.enter_maintenance(reason="update", update_id="u_1", detail="升级至 v9.9.9")
    mt = update.maintenance()
    assert mt["owner"] == "update-controller" and mt["pid"] == os.getpid()
    update.exit_maintenance()
    assert update.maintenance() == {}
