"""doctor 表现层：渲染、JSON 契约、退出码。

检查逻辑本身在 test_diagnostics.py，这里只管「结果列表 → 用户看到什么 / CI 拿到什么」。
"""

import argparse
import json

import pytest

from ragkernel import doctor
from ragkernel.diagnostics.schema import (
    DIAGNOSTICS_SCHEMA_VERSION,
    HealthPolicy,
    failed,
    passed,
    skipped,
)


def _args(**kw):
    base = dict(json=False, offline=False, strict=False, verbose=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _results():
    return [
        passed("python", "runtime", "Python 版本", "3.12.8"),
        failed("provider.auth", "provider", "provider · auth", "401 未授权",
               severity="error", fix="ragkernel setup --only provider"),
        failed("models", "model", "embedding 模型", "未缓存", severity="warning",
               fix="ragkernel models"),
        skipped("provider.smoke", "provider", "provider · smoke", "auth 未通过"),
    ]


def _required_passing():
    """DEFAULT_POLICY 要求的检查全部 passed。断言退出码的测试必须包含它们，
    否则「缺席 required → unknown」会盖过被测行为。provider.auth 不在 required 里
    （尽力而为），故不必列。"""
    return [
        passed("python", "runtime", "Python 版本", "3.12.8"),
        passed("sqlite", "storage", "sqlite-vec 扩展", "可加载"),
        passed("storage", "storage", "数据目录", "可写"),
        passed("provider.config", "provider", "provider · config", "anthropic · MiniMax-M3"),
        passed("provider.network", "provider", "provider · network", "TCP/TLS 可达"),
    ]


# ---------------------------------------------------------------- 文本渲染

def test_text_shows_symbol_per_severity():
    out = doctor.render_text(_results(), HealthPolicy(required=set()), verbose=False)
    assert "✓ Python 版本" in out
    assert "✗ provider · auth" in out
    assert "! embedding 模型" in out
    assert "- provider · smoke" in out


def test_text_shows_fix_commands():
    out = doctor.render_text(_results(), HealthPolicy(required=set()), verbose=False)
    assert "→ ragkernel setup --only provider" in out
    assert "→ ragkernel models" in out


def test_text_reports_exit_status():
    out = doctor.render_text(_results(), HealthPolicy(required=set()), verbose=False)
    assert "exit 2 UNHEALTHY" in out


def test_text_alignment_handles_cjk_width():
    """CJK 占两列，按字符数对齐会歪。摘要必须起始于同一列。"""
    results = [
        passed("a", "runtime", "Python 版本", "AAA"),
        passed("b", "storage", "sqlite-vec 扩展", "BBB"),
    ]
    lines = doctor.render_text(results, HealthPolicy(required=set()), verbose=False).splitlines()
    body = [ln for ln in lines if ln.startswith("✓")]

    def col(line, needle):
        return doctor._width(line[: line.index(needle)])

    assert col(body[0], "AAA") == col(body[1], "BBB")


def test_manual_install_reported_not_errored(tmp_path, monkeypatch):
    """手动安装没有 install.json，应显示「未知」而不是报错。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    out = doctor.render_text([passed("a", "runtime", "t")],
                             HealthPolicy(required=set()), verbose=False)
    assert "未知（手动安装）" in out


def test_install_manifest_is_reported(tmp_path, monkeypatch):
    """v1 的平铺路径——存量安装升级前还是这个样子，必须照常读得到。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    (tmp_path / ".ragkernel").mkdir()
    (tmp_path / ".ragkernel" / "install.json").write_text(
        json.dumps({"ref": "v0.4.0", "installer": "install.sh"}), encoding="utf-8")

    out = doctor.render_text([passed("a", "runtime", "t")],
                             HealthPolicy(required=set()), verbose=False)
    assert "v0.4.0" in out


def test_install_manifest_read_from_state_dir(tmp_path, monkeypatch):
    """install.sh 迁移后指纹落在 .ragkernel/state/ 下，doctor 要跟着走新路径。"""
    from ragkernel import config

    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    config.rk_path("state", "install.json", create=True).write_text(
        json.dumps({"schema_version": 2, "ref": "v0.5.0", "installer": "install.sh"}),
        encoding="utf-8")

    out = doctor.render_text([passed("a", "runtime", "t")],
                             HealthPolicy(required=set()), verbose=False)
    assert "v0.5.0" in out


def test_corrupt_install_manifest_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    (tmp_path / ".ragkernel").mkdir()
    (tmp_path / ".ragkernel" / "install.json").write_text("{not json", encoding="utf-8")

    out = doctor.render_text([passed("a", "runtime", "t")],
                             HealthPolicy(required=set()), verbose=False)
    assert "未知（手动安装）" in out


# ---------------------------------------------------------------- JSON 契约

def test_json_carries_schema_version_and_timestamp():
    """监控收集多台输出，没时间戳无法排序；没版本号消费方只能猜。"""
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=False))
    assert d["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert d["generated_at"].endswith("+00:00")


def test_json_redacts_hostname_by_default():
    """doctor --json > issue.json 贴 GitHub 是可预期用法，内网主机名会泄露组织结构。"""
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=False))
    assert d["host"]["hostname"] is None


def test_json_includes_hostname_when_verbose():
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=True))
    assert d["host"]["hostname"]


def test_json_summary_uses_its_own_enum():
    """summary.status 是系统状态，CheckResult.status 是单项事实，两个 namespace。"""
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=False))
    assert d["summary"]["status"] in {"healthy", "degraded", "unhealthy", "unknown"}
    assert d["summary"]["status"] not in {"passed", "failed", "skipped"}


def test_json_records_exit_policy():
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set(), strict=True),
                                      verbose=False))
    assert d["summary"]["exit_policy"] == "strict"


def test_json_severity_never_null_or_ok():
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=False))
    for c in d["checks"]:
        assert c["severity"] in {"none", "warning", "error"}
        assert c["status"] in {"passed", "failed", "skipped"}


def test_json_every_check_has_category():
    d = json.loads(doctor.render_json(_results(), HealthPolicy(required=set()), verbose=False))
    assert all(c["category"] for c in d["checks"])


def test_json_redacts_exception_repr_unless_verbose():
    """崩溃 check 的 exception repr 可能含本地路径/配置派生值。
    doctor --json > issue.json 贴 GitHub 是可预期用法，默认不该带；--verbose 才带。"""
    r = [failed("x", "runtime", "崩溃项", "检查崩溃：RuntimeError", severity="error",
                exception="RuntimeError('/home/alice/ragkernel/secret')",
                exception_type="RuntimeError")]

    default = json.loads(doctor.render_json(r, HealthPolicy(required=set()), verbose=False))
    assert "exception" not in default["checks"][0]["meta"]
    # 裸类名保留：它已经出现在 summary 里，且便于非 verbose 下 triage
    assert default["checks"][0]["meta"]["exception_type"] == "RuntimeError"

    verbose = json.loads(doctor.render_json(r, HealthPolicy(required=set()), verbose=True))
    assert "exception" in verbose["checks"][0]["meta"]


def test_json_redaction_does_not_mutate_original_meta():
    """to_dict() 返回的是 self.meta 的引用——过滤时必须建新 dict，不能就地删。"""
    r = failed("x", "runtime", "t", "boom", severity="error",
               exception="X", exception_type="X")
    doctor.render_json([r], HealthPolicy(required=set()), verbose=False)
    assert "exception" in r.meta


def test_non_object_install_manifest_treated_as_missing(tmp_path, monkeypatch):
    """合法 JSON 但不是对象（[] / null）不能让 doctor 崩，也不能违反 install 必为对象的契约。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    (tmp_path / ".ragkernel").mkdir()
    (tmp_path / ".ragkernel" / "install.json").write_text("[]", encoding="utf-8")

    out = doctor.render_text([passed("a", "runtime", "t")],
                             HealthPolicy(required=set()), verbose=False)
    assert "未知（手动安装）" in out  # 不崩

    d = json.loads(doctor.render_json([passed("a", "runtime", "t")],
                                      HealthPolicy(required=set()), verbose=False))
    assert isinstance(d["install"], dict)


# ---------------------------------------------------------------- 端到端

def test_main_returns_policy_exit_code(capsys, monkeypatch):
    results = _required_passing() + [
        failed("provider.auth", "provider", "provider · auth", "401", severity="error",
               fix="ragkernel setup --only provider"),
    ]
    monkeypatch.setattr(doctor.diagnostics, "run", lambda **kw: results)
    assert doctor.main(_args()) == 2
    assert "exit 2 UNHEALTHY" in capsys.readouterr().out


def test_main_json_mode_is_parseable(capsys, monkeypatch):
    monkeypatch.setattr(doctor.diagnostics, "run", lambda **kw: _results())
    doctor.main(_args(json=True))
    json.loads(capsys.readouterr().out)  # 不抛异常即通过


def test_ragkernel_dir_is_gitignored():
    """install.json / setup.lock 落在 .ragkernel/，必须被忽略，否则每台安装机
    的 git status 都是脏的。注意 git 不支持行尾注释——写成 `.ragkernel/  # 说明`
    会变成字面量模式而静默失效。"""
    import subprocess

    from ragkernel import config

    r = subprocess.run(
        ["git", "-C", str(config.ROOT), "check-ignore", "-q", ".ragkernel/install.json"],
        capture_output=True,
    )
    assert r.returncode == 0, ".ragkernel/ 未被 .gitignore 覆盖"


def test_healthy_environment_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(doctor.diagnostics, "run", lambda **kw: _required_passing())
    assert doctor.main(_args()) == 0
