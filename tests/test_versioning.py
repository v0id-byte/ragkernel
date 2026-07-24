"""版本来源与 .ragkernel 目录布局。

这两件事撑着整条发布链路（tag → CI → release → manifest → installer），
错了不会在功能测试里暴露，只会在升级时把用户带到错的版本上，所以单独钉住。
"""

import importlib.util
import json
import tomllib

import pytest

import ragkernel
from ragkernel import config


def _load_gen_manifest():
    """scripts/ 不是包，按路径加载。"""
    path = config.ROOT / "scripts" / "gen_manifest.py"
    spec = importlib.util.spec_from_file_location("gen_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ---------------------------------------------------------------- 版本单一来源


def test_version_matches_pyproject():
    """__version__ 派生自安装元数据，唯一源是 pyproject。两者不一致 = 发布链路的可信根断了。

    注意这条在 editable 安装下依赖 metadata 是新鲜的——改完 pyproject 版本号要
    `uv sync` 才刷新。CI 里 release workflow 会再断言一次 tag 也等于它。
    """
    pv = tomllib.loads((config.ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert ragkernel.__version__ == pv


def test_version_is_not_hardcoded_in_source():
    """防回退：有人图省事把 __version__ 改回字面量，单一来源就悄悄失效了。"""
    src = (config.ROOT / "ragkernel" / "__init__.py").read_text(encoding="utf-8")
    assert "_pkg_version" in src, "__version__ 必须由 importlib.metadata 派生"


# ---------------------------------------------------------------- 目录布局


def test_rk_path_has_no_side_effect_by_default(tmp_path, monkeypatch):
    """doctor 等只读场景会拿路径去探测；拿一下就把目录建出来的话，诊断本身改变了被诊断的系统。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    p = config.rk_path("state", "update.json")
    assert not p.parent.exists()

    config.rk_path("state", "update.json", create=True)
    assert p.parent.is_dir()


def test_rk_read_path_prefers_new_over_legacy(tmp_path, monkeypatch):
    """两个路径同时存在时必须读新的——迁移期间读到过期的 v1 指纹会让升级判断用错版本。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    legacy = tmp_path / ".ragkernel" / "install.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")

    assert config.rk_read_path("state", "install.json") == legacy

    new = config.rk_path("state", "install.json", create=True)
    new.write_text("new", encoding="utf-8")
    assert config.rk_read_path("state", "install.json") == new


def test_rk_read_path_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    assert config.rk_read_path("state", "install.json") is None


def test_rk_path_rejects_unknown_kind(tmp_path, monkeypatch):
    """布局是契约，不是自由字符串——写错 kind 应当立刻炸，而不是默默造出 .ragkernel/stat/。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    with pytest.raises(AssertionError):
        config.rk_path("stat", "install.json")


def test_rk_paths_are_computed_at_call_time(tmp_path, monkeypatch):
    """路径若在 import 期定死，monkeypatch ROOT 就失效，测试会写进真实仓库目录。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    assert config.rk_path("locks", "setup.lock").is_relative_to(tmp_path)


# ---------------------------------------------------------------- manifest 生成


def _current_tag() -> str:
    return "v" + ragkernel.__version__


def test_manifest_matches_current_version():
    gm = _load_gen_manifest()
    m = gm.build(_current_tag(), channel="stable", repo="v0id-byte/ragkernel", published_at=None)
    assert m["version"] == ragkernel.__version__
    assert m["requires"]["ragkernel_schema"] == config.SCHEMA_VERSION


def test_schema_version_parse_tolerates_trailing_comment(tmp_path, monkeypatch):
    """`SCHEMA_VERSION = 2  # 加了 xxx 表` 是很自然的写法，而这里一炸就是发版当场失败。"""
    gm = _load_gen_manifest()
    fake = tmp_path / "ragkernel"
    fake.mkdir()
    (fake / "config.py").write_text("SCHEMA_VERSION = 7  # 加了 foo 表\n", encoding="utf-8")
    monkeypatch.setattr(gm, "ROOT", tmp_path)
    assert gm._schema_version() == 7


def test_manifest_rejects_tag_pyproject_mismatch():
    """发布链路第一道闸门：tag 与 pyproject 不一致必须发不出去。"""
    gm = _load_gen_manifest()
    with pytest.raises(SystemExit):
        gm.build("v99.0.0", channel="stable", repo="x/y", published_at=None)


def test_manifest_rejects_malformed_tag():
    gm = _load_gen_manifest()
    with pytest.raises(SystemExit):
        gm.build("0.1.0", channel="stable", repo="x/y", published_at=None)


def test_verify_catches_tampered_manifest():
    """「有人手改 stable.json 写成 0.4.0，而 Release 还是 0.3.0」——这是整条信任链上
    唯一无法靠客户端补救的错误，所以产物本身要再验一次，不能只验输入的 tag。"""
    gm = _load_gen_manifest()
    tag = _current_tag()
    m = gm.build(tag, channel="stable", repo="x/y", published_at=None)

    m["version"] = "99.0.0"
    with pytest.raises(SystemExit):
        gm.verify(m, tag)


def test_manifest_rejects_bad_security_value(tmp_path, monkeypatch):
    """release.yaml 每次发布手改，`security: high`（不在枚举里）能过「字段存在」检查，
    然后发出一份违反自家 schema 的 manifest——企业侧校验器会拒收整个渠道。"""
    gm = _load_gen_manifest()
    _with_release_yaml(gm, tmp_path, monkeypatch, security="high")
    with pytest.raises(SystemExit):
        gm.build(_current_tag(), channel="stable", repo="x/y", published_at=None)


def test_manifest_rejects_quoted_boolean(tmp_path, monkeypatch):
    """YAML 里 `restart_required: "true"` 是字符串不是布尔。"""
    gm = _load_gen_manifest()
    _with_release_yaml(gm, tmp_path, monkeypatch,
                       upgrade_strategy={"restart_required": "true", "migration_required": False})
    with pytest.raises(SystemExit):
        gm.build(_current_tag(), channel="stable", repo="x/y", published_at=None)


def _with_release_yaml(gm, tmp_path, monkeypatch, **over):
    """在临时 ROOT 里放一份被改坏的 release.yaml，其余文件从真实仓库软链过来。"""
    import yaml

    rel = yaml.safe_load((config.ROOT / "release.yaml").read_text(encoding="utf-8"))
    rel.update(over)
    (tmp_path / "release.yaml").write_text(yaml.safe_dump(rel), encoding="utf-8")
    (tmp_path / "pyproject.toml").symlink_to(config.ROOT / "pyproject.toml")
    (tmp_path / "ragkernel").symlink_to(config.ROOT / "ragkernel")
    monkeypatch.setattr(gm, "ROOT", tmp_path)


def test_prerelease_version_normalisation():
    """semver 写 v0.2.0-rc.1，pyproject / importlib.metadata 里是规范化的 0.2.0rc1。
    裸字符串比较会把一次完全正确的发布判成版本不一致。"""
    gm = _load_gen_manifest()
    assert gm._same_version("0.2.0-rc.1", "0.2.0rc1")
    assert gm._same_version("0.2.0", "0.2.0")
    assert not gm._same_version("0.2.0", "0.3.0")


def test_manifest_satisfies_published_schema():
    """生成器与 docs/schemas/manifest-v1.json 必须同步演进——schema 加了必填字段
    而生成器没跟上，企业自建 endpoint 照 schema 校验就会跟官方产物对不上。"""
    gm = _load_gen_manifest()
    schema = json.loads((config.ROOT / "docs" / "schemas" / "manifest-v1.json").read_text(encoding="utf-8"))
    m = gm.build(_current_tag(), channel="stable", repo="x/y", published_at=None)

    missing = [k for k in schema["required"] if k not in m]
    assert not missing, f"manifest 缺 schema 要求的字段：{missing}"

    for section in ("requires", "upgrade_strategy"):
        want = schema["properties"][section]["required"]
        assert not [k for k in want if k not in m[section]], f"{section} 缺字段"

    assert m["schema_version"] == schema["properties"]["schema_version"]["const"]
