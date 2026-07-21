"""provider 三步链（config → network → auth）的单测。

全程 mock 网络/HTTP，绝不打真实 provider（也就绝不烧用户的 MiniMax 额度）。
每个测试用 RAGKERNEL_DATA_DIR 隔离，且断言 doctor 只读——不创建 settings.db。
"""

import socket
import ssl
import urllib.error

from ragkernel.checks import provider as P
from ragkernel.checks.provider import (
    AnthropicProbe,
    OpenAICompatibleProbe,
    check_provider_auth,
    check_provider_config,
    check_provider_network,
)

_KEY_ENVS = ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "VLLM_API_KEY", "OPENAI_API_KEY")


def _set_provider(monkeypatch, tmp_path, prov: dict, env: dict | None = None):
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))  # 空目录，无 settings.db
    monkeypatch.setattr("ragkernel.config.settings", lambda: {"provider": prov})
    for k in _KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)


# ------------------------------------------------------------------ config

def test_config_anthropic_missing_key_is_error(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "model": "MiniMax-M3",
        "base_url": "https://api.minimaxi.com/anthropic", "api_key_env": "MINIMAX_API_KEY"})
    r = check_provider_config()
    assert r.status == "failed" and r.severity == "error"
    assert "MINIMAX_API_KEY" in r.summary   # 报清「期望来源」而非只说缺 key
    assert r.meta["source"]["api_key"] is None
    assert r.fix


def test_config_reports_key_source_and_masks(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "model": "MiniMax-M3",
        "base_url": "https://x", "api_key_env": "MINIMAX_API_KEY"},
        env={"MINIMAX_API_KEY": "sk-supersecret-1234"})
    r = check_provider_config()
    assert r.status == "passed"
    assert r.meta["source"]["api_key"] == "env:MINIMAX_API_KEY"
    assert "sk-supersecret-1234" not in r.summary   # 绝不明文回显
    assert "1234" in r.summary                        # 只露末四位


def test_config_openai_missing_key_passes(monkeypatch, tmp_path):
    """本地 openai 兼容（vLLM/Ollama）忽略 key，缺 key 不算错。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "model": "Qwen3-32B",
        "base_url": "http://localhost:8000/v1", "api_key_env": "VLLM_API_KEY"})
    r = check_provider_config()
    assert r.status == "passed"
    assert "EMPTY" in r.summary


def test_config_is_readonly_never_creates_settings_db(monkeypatch, tmp_path):
    """doctor 只读：读 provider 配置不能顺手把 data/settings.db 建出来。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "model": "m", "base_url": "https://x",
        "api_key_env": "MINIMAX_API_KEY"}, env={"MINIMAX_API_KEY": "k"})
    check_provider_config()
    assert not (tmp_path / "settings.db").exists()


def test_config_unknown_kind_fails(monkeypatch, tmp_path):
    """kind: opeani 这类 typo——backends 把一切非 openai 当 anthropic，会静默走错后端。
    有 key、看似配好，却在真 generate 时炸。必须在 config 层拦下。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "opeani", "model": "m", "base_url": "https://x",
        "api_key_env": "MINIMAX_API_KEY"}, env={"MINIMAX_API_KEY": "k"})
    r = check_provider_config()
    assert r.status == "failed" and r.severity == "error"
    assert "opeani" in r.summary


def test_config_unsupported_scheme_fails(monkeypatch, tmp_path):
    """htps:// 这类 scheme typo——否则被当非 https 落到 80 端口裸连、看似通。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "model": "m", "base_url": "htps://api.openai.com/v1",
        "api_key_env": "VLLM_API_KEY"})
    r = check_provider_config()
    assert r.status == "failed"
    assert "scheme" in r.summary


# ------------------------------------------------------------------ network

class _FakeCM:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_raw(monkeypatch, *, conn=None, wrap=None):
    """替换 socket.create_connection 与 ssl 上下文，避免真实连接。"""
    monkeypatch.setattr(P, "_proxy_for", lambda scheme, host: None)
    calls = {}

    def fake_conn(addr, timeout):
        calls["addr"] = addr
        if conn:
            conn(addr)
        return _FakeCM()
    monkeypatch.setattr(P.socket, "create_connection", fake_conn)

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname):
            calls["sni"] = server_hostname
            if wrap:
                wrap(server_hostname)
            return _FakeCM()
    monkeypatch.setattr(P.ssl, "create_default_context", lambda: FakeCtx())
    return calls


def test_network_https_success_reports_tls_and_sni(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "base_url": "https://api.example.com/anthropic",
        "model": "m", "api_key_env": "X"})
    calls = _mock_raw(monkeypatch)
    r = check_provider_network()
    assert r.status == "passed"
    assert calls["addr"] == ("api.example.com", 443)
    assert calls["sni"] == "api.example.com"   # SNI 回归点：必须传 server_hostname
    assert "TCP/TLS" in r.summary
    assert r.meta["tls"] is True


def test_network_http_local_skips_tls(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "base_url": "http://localhost:8000/v1",
        "model": "m", "api_key_env": "X"})
    tls_used = {"called": False}

    def _no_tls():
        tls_used["called"] = True
        raise AssertionError("http 不该建 TLS")
    monkeypatch.setattr(P, "_proxy_for", lambda s, h: None)
    monkeypatch.setattr(P.socket, "create_connection", lambda addr, timeout: _FakeCM())
    monkeypatch.setattr(P.ssl, "create_default_context", _no_tls)

    r = check_provider_network()
    assert r.status == "passed"
    assert tls_used["called"] is False
    assert "无 TLS" in r.summary


def test_network_dns_failure(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "base_url": "https://nonexistent.invalid",
        "model": "m", "api_key_env": "X"})

    def boom(addr):
        raise socket.gaierror("Name or service not known")
    _mock_raw(monkeypatch, conn=boom)
    r = check_provider_network()
    assert r.status == "failed"
    assert "DNS" in r.summary


def test_network_tls_cert_failure_is_not_a_pass(monkeypatch, tmp_path):
    """证书主机名不匹配（企业 MITM）必须报失败，不能因为 TCP 通就谎报可达。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "base_url": "https://wrong.host",
        "model": "m", "api_key_env": "X"})

    def bad_cert(sni):
        raise ssl.SSLCertVerificationError("hostname mismatch")
    _mock_raw(monkeypatch, wrap=bad_cert)
    r = check_provider_network()
    assert r.status == "failed"
    assert "证书" in r.summary


def test_network_connection_refused(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "base_url": "http://localhost:9/v1",
        "model": "m", "api_key_env": "X"})
    monkeypatch.setattr(P, "_proxy_for", lambda s, h: None)

    def refuse(addr, timeout):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(P.socket, "create_connection", refuse)
    r = check_provider_network()
    assert r.status == "failed"


def test_network_unparseable_base_url(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "base_url": "not-a-url", "model": "m", "api_key_env": "X"})
    r = check_provider_network()
    assert r.status == "failed"


def test_network_unsupported_scheme_fails(monkeypatch, tmp_path):
    """htps:// 不能当 http 裸连放行——SDK 根本用不了这个 URL。"""
    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "base_url": "htps://api.openai.com/v1", "model": "m", "api_key_env": "X"})
    monkeypatch.setattr(P, "_proxy_for", lambda s, h: None)
    r = check_provider_network()
    assert r.status == "failed"
    assert "scheme" in r.summary


def test_network_proxy_407_is_failure_not_reachable(monkeypatch, tmp_path):
    """407 来自代理（要鉴权），不是目标响应——说明根本没到目标，不能算连通。
    否则 network 通过、auth 因非必需被跳过，doctor 会在 provider 实际不可达时报 healthy。"""
    import urllib.error

    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "base_url": "https://api.example.com", "model": "m", "api_key_env": "X"})
    monkeypatch.setattr(P, "_proxy_for", lambda s, h: "http://user:secret@corp-proxy:3128")

    def raise_407(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 407, "Proxy Auth Required", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", raise_407)

    r = check_provider_network()
    assert r.status == "failed"
    assert "407" in r.summary
    # 代理凭证绝不泄漏（summary 与 meta 都不能含）
    assert "secret" not in r.summary
    assert "secret" not in str(r.meta)


def test_network_proxy_credentials_sanitized_on_success(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "base_url": "https://api.example.com", "model": "m", "api_key_env": "X"})
    monkeypatch.setattr(P, "_proxy_for", lambda s, h: "http://alice:hunter2@corp-proxy:3128")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: None)  # 成功

    r = check_provider_network()
    assert r.status == "passed"
    assert "hunter2" not in str(r.summary) + str(r.meta)
    assert r.meta["proxy"] == "http://corp-proxy:3128"


def test_sanitize_proxy_strips_credentials():
    assert P._sanitize_proxy("http://user:pass@proxy:8080") == "http://proxy:8080"
    assert P._sanitize_proxy("http://proxy:8080") == "http://proxy:8080"


# ------------------------------------------------------------------ auth（零成本，绝不计费）

def _probe_returning(status):
    def _p():
        return status
    return _p


def test_auth_200_is_valid_and_never_charges(monkeypatch):
    p = OpenAICompatibleProbe("http://x/v1", "key")
    monkeypatch.setattr(p, "_probe", _probe_returning(200))
    r = p.check_auth()
    assert r.status == "passed"
    assert r.meta["charged_request"] is False   # doctor 绝不计费
    assert r.meta["verified"] is True


def test_auth_401_is_error(monkeypatch):
    p = OpenAICompatibleProbe("http://x/v1", "badkey")
    monkeypatch.setattr(p, "_probe", _probe_returning(401))
    r = p.check_auth()
    assert r.status == "failed" and r.severity == "error"
    assert "401" in r.summary
    assert r.meta["charged_request"] is False


def test_auth_no_zerocost_endpoint_is_skipped_not_charged(monkeypatch):
    """openai 兼容的 /v1/models 是标准端点：404 说明 base 路径写错（如 /v2），
    绝不能当「无端点」静默跳过——真 generate 会用同一 base 一样失败。→ 硬错误。"""
    p = OpenAICompatibleProbe("http://x/v2", "key")
    monkeypatch.setattr(p, "_get_status", lambda url, h: 404)
    r = p.check_auth()
    assert r.status == "failed" and r.severity == "error"
    assert "路径" in r.summary
    assert r.meta["charged_request"] is False   # 仍然绝不计费


def test_anthropic_404_is_warning_not_silent_pass(monkeypatch):
    """anthropic 兼容端点可用性不一：404 降级为 warning（degraded），
    既不误判 unhealthy、也不静默放行成 healthy。"""
    p = AnthropicProbe("https://api.minimaxi.com/anthropic", "key")
    monkeypatch.setattr(p, "_get_status", lambda url, h: 404)
    r = p.check_auth()
    assert r.status == "failed" and r.severity == "warning"
    assert r.meta["charged_request"] is False


def test_auth_network_error_defers_to_network_check(monkeypatch):
    """鉴权时网络不通，不在这层报——避免和 provider·network 出两条错误。"""
    p = OpenAICompatibleProbe("http://x/v1", "key")

    def raise_url():
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(p, "_probe", raise_url)
    r = p.check_auth()
    assert r.status == "skipped"
    assert "网络" in r.summary


def test_auth_selects_probe_by_kind(monkeypatch, tmp_path):
    from ragkernel.diagnostics.schema import passed as _passed

    _set_provider(monkeypatch, tmp_path, {
        "kind": "openai", "model": "m", "base_url": "http://localhost:8000/v1",
        "api_key_env": "VLLM_API_KEY"})
    seen = {}

    def fake_check(self):
        seen["cls"] = type(self).__name__
        return _passed("provider.auth", "provider", "t")
    monkeypatch.setattr(OpenAICompatibleProbe, "check_auth", fake_check)

    check_provider_auth()
    assert seen["cls"] == "OpenAICompatibleProbe"


def test_auth_anthropic_missing_key_skips(monkeypatch, tmp_path):
    _set_provider(monkeypatch, tmp_path, {
        "kind": "anthropic", "model": "m", "base_url": "https://x",
        "api_key_env": "MINIMAX_API_KEY"})  # 无 key
    r = check_provider_auth()
    assert r.status == "skipped"
    assert "API key" in r.summary
