"""`ragkernel setup` 向导单测。

交互分支难测，所以核心逻辑都走非交互路径（automation 用的也是这条），
monkeypatch _interactive→False 后确定性执行。每个测试用 RAGKERNEL_DATA_DIR 隔离，
不碰真实 KB / auth.db。
"""

import fcntl
from types import SimpleNamespace

import pytest

from ragkernel import bootstrap


def _args(**kw):
    base = dict(yes=False, only=None, skip=None, reset_provider=False, with_token=False,
                provider=None, base_url=None, model=None, admin_user=None,
                no_models=False, show_token=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _noninteractive(monkeypatch):
    monkeypatch.setattr(bootstrap, "_interactive", lambda: False)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    for v in ("RAGKERNEL_SETUP_API_KEY", "RAGKERNEL_SETUP_ADMIN_PASSWORD", "MINIMAX_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


# ------------------------------------------------------------------ 原语

def test_env_empty_string_is_unset(monkeypatch):
    monkeypatch.setenv("X", "   ")
    assert bootstrap._env("X") == ""
    monkeypatch.setenv("X", "v")
    assert bootstrap._env("X") == "v"


def test_selected_respects_only_and_skip():
    assert bootstrap._selected(_args(only="provider,admin")) == ["provider", "admin"]
    assert bootstrap._selected(_args(skip="token")) == ["provider", "admin", "models"]
    assert bootstrap._selected(_args(only="models", skip="models")) == []


def test_selected_rejects_unknown_step_names():
    """--only admn 这类 typo 原先静默变空步骤 → 假成功退 0；必须拒绝。"""
    with pytest.raises(SystemExit) as ei:
        bootstrap._selected(_args(only="admn"))
    assert ei.value.code == 1
    with pytest.raises(SystemExit):
        bootstrap._selected(_args(skip="modles"))


# ------------------------------------------------------------------ provider

def test_provider_yes_keeps_usable_current(isolated, monkeypatch, capsys):
    """无改动意图 + 当前可用（有 key）→ 保持现状。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-existing")
    bootstrap._step_provider(_args(yes=True))
    assert "保持现状" in capsys.readouterr().out


def test_provider_yes_fails_when_current_unusable(isolated):
    """无改动意图 + 当前 anthropic 缺 key → fail-fast，别装成功却一调 LLM 就挂。"""
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_provider(_args(yes=True))
    assert ei.value.code == 1


def test_provider_yes_same_preset_reapply_no_key_ok(isolated, monkeypatch):
    """幂等重跑 --provider minimax（当前就是 minimax、有 key）不该要求重输 key。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-existing")
    bootstrap._step_provider(_args(yes=True, provider="minimax"))  # 不抛
    from ragkernel import config
    assert config.provider(readonly=True)["model"] == "MiniMax-M3"


def test_provider_yes_switch_to_openai_clears_stale_key(isolated, monkeypatch):
    """MiniMax→local(openai) 无新 key：清掉旧云端 key（写 EMPTY），别当 bearer 发给新端点。"""
    from ragkernel import config

    config.set_provider_override("anthropic", "https://api.minimaxi.com/anthropic",
                                 "MiniMax-M3", 8000, "sk-cloud-secret")
    monkeypatch.delenv("RAGKERNEL_SETUP_API_KEY", raising=False)
    bootstrap._step_provider(_args(yes=True, provider="local"))

    eff = config.provider(readonly=True)
    assert eff["kind"] == "openai"
    assert eff.get("api_key") == "EMPTY"           # 旧云端 key 被清掉
    assert eff.get("api_key") != "sk-cloud-secret"


def test_provider_yes_fail_fast_setting_anthropic_without_key(isolated):
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_provider(_args(yes=True, provider="claude"))
    assert ei.value.code == 1


def test_provider_yes_sets_override_with_env_key(isolated, monkeypatch):
    monkeypatch.setenv("RAGKERNEL_SETUP_API_KEY", "sk-test-1234")
    bootstrap._step_provider(_args(yes=True, provider="minimax"))

    from ragkernel import config
    prov = config.provider(readonly=True)
    assert prov["kind"] == "anthropic" and prov["model"] == "MiniMax-M3"
    assert prov["base_url"] == "https://api.minimaxi.com/anthropic"
    assert prov.get("api_key") == "sk-test-1234"


def test_provider_yes_switching_requires_fresh_key(isolated, monkeypatch):
    """MiniMax→Claude 不能沿用旧 key：--yes --provider claude 无新 key → fail-fast。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-old")  # 现有 key
    monkeypatch.delenv("RAGKERNEL_SETUP_API_KEY", raising=False)
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_provider(_args(yes=True, provider="claude"))
    assert ei.value.code == 1


def test_provider_yes_model_flag_acts(isolated, monkeypatch, capsys):
    """--yes --model X 是显式改动，不该打印「保持现状」。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-mm")  # 现有 key，非切换
    bootstrap._step_provider(_args(yes=True, model="MiniMax-M3-Pro"))
    assert "保持现状" not in capsys.readouterr().out

    from ragkernel import config
    assert config.provider(readonly=True)["model"] == "MiniMax-M3-Pro"


def test_provider_interactive_menu_sets_override(isolated, monkeypatch):
    """交互分支：首装无覆盖 → 直接进菜单，选 1（MiniMax）+ getpass key。"""
    monkeypatch.setattr(bootstrap, "_interactive", lambda: True)
    monkeypatch.setattr(bootstrap, "_connectivity_check", lambda: None)  # 别打网络
    answers = iter(["1", "", ""])  # 序号=1，base_url/model 用默认
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a: "sk-interactive")

    bootstrap._step_provider(_args())
    from ragkernel import config
    prov = config.provider(readonly=True)
    assert prov["model"] == "MiniMax-M3" and prov.get("api_key") == "sk-interactive"


def test_provider_interactive_keeps_when_declined(isolated, monkeypatch):
    """已配置时先展示状态，回答「不改」→ 保持不动。"""
    from ragkernel import config

    config.set_provider_override("openai", "http://x/v1", "keepme", 8000, "k")
    monkeypatch.setattr(bootstrap, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")  # 要修改吗 → n
    bootstrap._step_provider(_args())
    assert config.provider(readonly=True)["model"] == "keepme"


def test_prompt_password_retries_until_match(monkeypatch):
    answers = iter(["", "a", "b", "secret", "secret"])  # 空 → 不一致 → 一致
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a: next(answers))
    assert bootstrap._prompt_password() == "secret"


def test_provider_never_touches_settings_yaml(isolated, monkeypatch):
    """provider 落 DB 覆盖，不改 settings.yaml。比对文件内容本身，不看 git 状态
    （否则会被其它未提交改动干扰）。"""
    from ragkernel import config

    yaml_path = config.ROOT / "config" / "settings.yaml"
    before = yaml_path.read_text()
    monkeypatch.setenv("RAGKERNEL_SETUP_API_KEY", "sk-x")
    bootstrap._step_provider(_args(yes=True, provider="local"))
    assert yaml_path.read_text() == before


def test_override_empty_base_url_clears_prior(isolated):
    """选官方 Claude（空 base_url = SDK 默认 host）必须能清掉上一个非空覆盖。"""
    from ragkernel import config

    config.set_provider_override("anthropic", "https://api.minimaxi.com/anthropic",
                                 "MiniMax-M3", 8000, "k1")
    assert config.provider(readonly=True)["base_url"] == "https://api.minimaxi.com/anthropic"

    config.set_provider_override("anthropic", "", "claude-sonnet-5", 8000, None)
    assert config.provider(readonly=True)["base_url"] == ""  # 旧的清掉了


# ------------------------------------------------------------------ admin

def test_admin_skips_when_admin_exists(isolated, capsys):
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_admin(_args(yes=True))
    assert "已存在" in capsys.readouterr().out


def test_admin_yes_fail_fast_without_password(isolated):
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_admin(_args(yes=True, admin_user="alice"))
    assert ei.value.code == 1


def test_admin_yes_creates_with_env_password(isolated, monkeypatch):
    monkeypatch.setenv("RAGKERNEL_SETUP_ADMIN_PASSWORD", "s3cret")
    bootstrap._step_admin(_args(yes=True, admin_user="alice"))

    from ragkernel import auth
    users = auth.list_users()
    assert any(u["username"] == "alice" and u["is_admin"] for u in users)


def test_admin_creates_when_only_deactivated_admin_exists(isolated, monkeypatch):
    """停用的 admin 登录不了——不能拿它当「已有管理员」跳过建号，否则部署没有可用管理员。"""
    from ragkernel import auth

    r = auth.create_user("olddisabled", "pw", is_admin=True)
    auth.set_active(r["id"], False)
    monkeypatch.setenv("RAGKERNEL_SETUP_ADMIN_PASSWORD", "newpw")

    bootstrap._step_admin(_args(yes=True, admin_user="newadmin"))
    active_admins = [u for u in auth.list_users() if u["is_admin"] and u["is_active"]]
    assert any(u["username"] == "newadmin" for u in active_admins)


def test_admin_yes_username_collision_fails_clearly(isolated, monkeypatch):
    """停用首个 admin 后重跑、默认名与停用账号撞名 → 明确报错，不是未捕获的 IntegrityError。"""
    from ragkernel import auth

    r = auth.create_user("dupe", "pw", is_admin=True)
    auth.set_active(r["id"], False)  # 停用，但用户名仍占用
    monkeypatch.setenv("RAGKERNEL_SETUP_ADMIN_PASSWORD", "pw")

    with pytest.raises(SystemExit) as ei:
        bootstrap._step_admin(_args(yes=True, admin_user="dupe"))
    assert ei.value.code == 1


# ------------------------------------------------------------------ models

def test_models_no_models_flag_skips(isolated, capsys):
    bootstrap._step_models(_args(no_models=True))
    assert "跳过下载" in capsys.readouterr().out


def test_models_yes_defaults_to_not_downloading(isolated, monkeypatch, capsys):
    """默认 N——「装成功」= runtime ready，不是 2GB 就位。"""
    monkeypatch.setattr("ragkernel.models.get_cache_status",
                        lambda: [SimpleNamespace(role="embedding", status="missing")])
    called = {"download": False}
    monkeypatch.setattr("ragkernel.models.download",
                        lambda: called.__setitem__("download", True) or [])
    bootstrap._step_models(_args(yes=True))
    assert called["download"] is False
    assert "暂不下载" in capsys.readouterr().out


def test_models_download_error_raises(isolated, monkeypatch):
    """用户明确选了下载，失败就不能把它当成功走到 _wrapup。"""
    monkeypatch.setattr("ragkernel.models.get_cache_status",
                        lambda: [SimpleNamespace(role="embedding", status="missing")])
    monkeypatch.setattr("ragkernel.models.download",
                        lambda: [SimpleNamespace(role="embedding", status="error", error="disk full")])
    monkeypatch.setattr(bootstrap, "_interactive", lambda: True)
    monkeypatch.setattr(bootstrap, "_confirm", lambda *a, **k: True)  # 用户答 yes
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_models(_args())
    assert ei.value.code == 1


# ------------------------------------------------------------------ token

def test_token_not_issued_by_default(isolated, capsys):
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_token(_args(with_token=False))
    assert capsys.readouterr().out == ""
    assert auth.list_tokens() == []


def test_token_masked_when_noninteractive(isolated, capsys):
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_token(_args(with_token=True, show_token=False))
    out = capsys.readouterr().out
    assert "完整值未打印" in out
    assert "Bearer" not in out          # 非 tty 不打印片段
    assert len(auth.list_tokens()) == 1  # 但确实签发了


def test_token_shown_with_show_token(isolated, capsys):
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_token(_args(with_token=True, show_token=True))
    assert "Bearer" in capsys.readouterr().out


def test_token_masked_when_yes_even_from_tty(isolated, monkeypatch, capsys):
    """--yes 是自动化模式：即便从 pty（CI 常见）跑，也默认脱敏，别漏长效凭证进日志。"""
    monkeypatch.setattr(bootstrap, "_interactive", lambda: True)  # 假装 tty
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_token(_args(with_token=True, yes=True))
    out = capsys.readouterr().out
    assert "Bearer" not in out and "完整值未打印" in out


def test_token_skips_when_no_active_admin(isolated, capsys):
    """所有 admin 都被停用时，不能签发到一个登录不了的账号。"""
    from ragkernel import auth

    r = auth.create_user("disabled", "pw", is_admin=True)
    auth.set_active(r["id"], False)
    bootstrap._step_token(_args(with_token=True))
    assert "无启用中的管理员" in capsys.readouterr().out
    assert auth.list_tokens() == []


def test_token_output_honors_mcp_env_overrides(isolated, monkeypatch, capsys):
    """打印的 Agent 配置 URL 要用 RAGKERNEL_MCP_HOST/PORT，与 cmd_mcp 启服务同源。"""
    monkeypatch.setenv("RAGKERNEL_MCP_HOST", "10.0.0.5")
    monkeypatch.setenv("RAGKERNEL_MCP_PORT", "9999")
    from ragkernel import auth

    auth.create_user("boss", "pw", is_admin=True)
    bootstrap._step_token(_args(with_token=True, show_token=True))
    assert "10.0.0.5:9999" in capsys.readouterr().out


def test_token_duplicate_label_raises_not_silent(isolated):
    """--with-token 却签不出（label 已存在）不能静默返回让 run() 退 0——自动化会当成功。"""
    from ragkernel import auth

    r = auth.create_user("boss", "pw", is_admin=True)
    auth.issue_token(r["id"], ttl_days=1, label="claude-code", token_kind="agent")  # 先占 label
    with pytest.raises(SystemExit) as ei:
        bootstrap._step_token(_args(with_token=True))
    assert ei.value.code == 1


def test_provider_interactive_manual_rejects_bad_kind(isolated, monkeypatch):
    """交互手动填时 kind typo（opneai）被拒、重问，直到合法值。"""
    monkeypatch.setattr(bootstrap, "_interactive", lambda: True)
    monkeypatch.setattr(bootstrap, "_connectivity_check", lambda: None)
    answers = iter(["4", "opneai", "openai", "http://x/v1", "m"])  # 手动 → 错 kind → 对 kind → base → model
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a: "k")

    bootstrap._step_provider(_args())
    from ragkernel import config
    assert config.provider(readonly=True)["kind"] == "openai"


# ------------------------------------------------------------------ run() 编排

def test_reset_provider_short_circuits(isolated, capsys):
    from ragkernel import config

    config.set_provider_override("openai", "http://x/v1", "m", 8000, "k")
    rc = bootstrap.run(_args(reset_provider=True))
    assert rc == 0
    assert config.get_provider_override_ro() == {}


def test_concurrent_setup_is_blocked(isolated, monkeypatch, tmp_path):
    """第二个 setup 拿不到 .ragkernel/setup.lock 就退出——不用 SQLite 锁（首次装 auth.db 可能还没有）。"""
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    (tmp_path / ".ragkernel").mkdir()
    held = open(tmp_path / ".ragkernel" / "setup.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = bootstrap.run(_args(only="provider"))
        assert rc == 1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_full_run_yes_creates_admin_keeps_provider(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr("ragkernel.config.ROOT", tmp_path)
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-usable")  # provider 可用，provider 步骤才能保持现状
    monkeypatch.setenv("RAGKERNEL_SETUP_ADMIN_PASSWORD", "pw12345")
    rc = bootstrap.run(_args(yes=True, no_models=True))
    assert rc == 0

    from ragkernel import auth
    assert any(u["is_admin"] for u in auth.list_users())
