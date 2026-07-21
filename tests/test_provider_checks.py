"""provider 三步链（config → network → auth）的单测。

全程 mock 网络/HTTP，绝不打真实 provider（也就绝不烧用户的 MiniMax 额度）。
每个测试用 RAGKERNEL_DATA_DIR 隔离，且断言 doctor 只读——不创建 settings.db。
"""

import socket
import ssl
import urllib.error

import pytest

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
    """provider 没有零成本鉴权端点时跳过（留给 setup 做完整验证），绝不降级去计费。"""
    p = AnthropicProbe("https://api.minimaxi.com/anthropic", "key")

    def raise_nozerocost():
        raise P._NoZeroCostEndpoint
    monkeypatch.setattr(p, "_probe", raise_nozerocost)
    r = p.check_auth()
    assert r.status == "skipped"
    assert r.meta["verified"] is False


def test_auth_network_error_defers_to_network_check(monkeypatch):
    """鉴权时网络不通，不在这层报——避免和 provider·network 出两条错误。"""
    p = OpenAICompatibleProbe("http://x/v1", "key")

    def raise_url():
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(p, "_probe", raise_url)
    r = p.check_auth()
    assert r.status == "skipped"
    assert "网络" in r.summary


def test_openai_probe_404_means_no_zerocost(monkeypatch):
    p = OpenAICompatibleProbe("http://x/v1", "key")
    monkeypatch.setattr(p, "_get_status", lambda url, h: 404)
    with pytest.raises(P._NoZeroCostEndpoint):
        p._probe()


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
