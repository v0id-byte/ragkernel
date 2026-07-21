"""Provider 检查：config → network → auth 三步分层归因。

分层是为了让「LLM 用不了」不再是一句笼统的报错，而是精确到哪一层：
- config：配置齐不齐、key 从哪来（企业最常见的坑是「配了但来源对不上」）；
- network：纯 socket + TLS，**不碰 LLM SDK**——SDK 缺 key 会直接 raise，
  若网络检查经由 SDK，没填 key 的用户会看到「网络不通」这种完全错误的归因；
- auth：零成本端点验证凭证。**doctor 绝不悄悄计费**——只用 GET /models 这类
  不产生 token 的端点；没有这种端点就跳过（留给 setup 做一次带最小推理的完整验证）。

依赖按**字段**判定、不按上一步的 result 短路：没填 key 时 network 照样能测
（它只要 base_url），于是用户只看到一条 config 错误、而网络归因仍然准确。
"""

import os
import socket
import ssl
import urllib.error
import urllib.request
from time import monotonic
from urllib.parse import urlparse

from ..diagnostics.runner import CheckSpec
from ..diagnostics.schema import CheckResult, failed, passed, skipped

CATEGORY = "provider"
_TIMEOUT = 6
FIX = "ragkernel setup --only provider"


def _prov() -> dict:
    from .. import config

    # readonly：doctor 必须零副作用，不能因为读 provider 覆盖就创建 data/settings.db
    return config.provider(readonly=True)


def _endpoint(prov: dict) -> tuple[str, str | None, int, str]:
    """(scheme, host, port, base_url)。base_url 为空时回落到 SDK 的默认 host。"""
    base = (prov.get("base_url") or "").strip()
    kind = prov.get("kind", "anthropic")
    if not base:
        if kind == "openai":
            base = "https://api.openai.com/v1"   # openai 约定 base 含 /v1
        else:
            base = "https://api.anthropic.com"
    u = urlparse(base)
    scheme = u.scheme or "https"
    port = u.port or (443 if scheme == "https" else 80)
    return scheme, u.hostname, port, base


_KINDS = ("anthropic", "openai")
_SCHEMES = ("http", "https")


def _key(prov: dict) -> str:
    # 与 backends.py 同一套取值：DB 覆盖的明文 key 优先，其次 $api_key_env
    return (prov.get("api_key") or os.environ.get(prov.get("api_key_env", ""), "") or "").strip()


def _mask(key: str) -> str:
    return f"····{key[-4:]}" if len(key) >= 4 else "已配置"


def _config_problem(prov: dict) -> str | None:
    """配置本身是否会被运行时误解——这类「配了但配错」比缺配更隐蔽。"""
    kind = prov.get("kind", "anthropic")
    if kind not in _KINDS:
        # backends.get_backend 把一切非 openai 当 anthropic：kind=opeani 会静默走错后端
        return f"未知 kind：{kind!r}（应为 anthropic 或 openai）"
    base = (prov.get("base_url") or "").strip()
    if base:
        scheme = urlparse(base).scheme
        if scheme and scheme not in _SCHEMES:
            # htps:// 这类 typo 会被当成非 https、误落到 80 端口裸连
            return f"base_url 的 scheme 不支持：{scheme!r}（应为 http 或 https）"
    return None


def _sanitize_proxy(url: str) -> str:
    """代理 URL 可能带 user:pass@ 凭证——doctor --json 可分享，绝不能回显。"""
    try:
        u = urlparse(url)
    except ValueError:
        return "proxy"
    if u.hostname:
        return f"{u.scheme}://{u.hostname}{f':{u.port}' if u.port else ''}"
    return "proxy"


# ------------------------------------------------------------------ config

def check_provider_config() -> CheckResult:
    from .. import config

    prov = _prov()
    override = config.get_provider_override_ro()  # 只读，不创建 settings.db
    kind = prov.get("kind", "anthropic")
    env_name = prov.get("api_key_env", "")

    # 先查「配了但配错」——unknown kind / 不支持的 scheme 会让后面几层看似通过、
    # 真正 generate 时才炸。这类必须在 config 层拦下。
    problem = _config_problem(prov)
    if problem:
        return failed("provider.config", CATEGORY, "provider · config", problem, fix=FIX,
                      kind=kind, model=prov.get("model"))
    key = _key(prov)

    key_from_override = bool(prov.get("api_key"))
    key_from_env = not key_from_override and bool(os.environ.get(env_name, "").strip())
    source = {
        "kind": "override" if override.get("kind") else "yaml",
        "model": "override" if override.get("model") else "yaml",
        "base_url": "override" if override.get("base_url") else "yaml",
        "api_key": "override(db)" if key_from_override
                   else (f"env:{env_name}" if key_from_env else None),
    }
    meta = {"source": source, "override_active": bool(override),
            "kind": kind, "model": prov.get("model")}

    if not key:
        if kind == "openai":
            # 本地 vLLM/Ollama 忽略 key（SDK 用 "EMPTY" 占位）；远程端点才需要
            return passed("provider.config", CATEGORY, "provider · config",
                          f"{kind} · {prov.get('model')}（本地 openai 兼容可用 EMPTY key；"
                          f"远程端点需在 {env_name} 配 key）", **meta)
        # anthropic：缺 key 后端会直接 raise，是硬错误。报清「期望来源」而非只说「缺 key」
        return failed("provider.config", CATEGORY, "provider · config",
                      f"API key 未取到——期望来源：环境变量 {env_name}"
                      f"（由 settings.yaml 的 api_key_env 指定）；当前该变量为空，DB 覆盖未启用",
                      fix=FIX, missing=["api_key"], **meta)

    src = source["api_key"]
    return passed("provider.config", CATEGORY, "provider · config",
                  f"{kind} · {prov.get('model')} · key {_mask(key)}（{src}）"
                  f"{'  ← data/settings.db 覆盖' if meta['override_active'] else ''}",
                  **meta)


# ------------------------------------------------------------------ network

def _proxy_for(scheme: str, host: str) -> str | None:
    proxies = urllib.request.getproxies()
    if urllib.request.proxy_bypass(host):
        return None
    return proxies.get(scheme) or proxies.get("all")


def check_provider_network() -> CheckResult:
    prov = _prov()
    scheme, host, port, base = _endpoint(prov)
    if not host:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"base_url 解析不出主机：{prov.get('base_url')!r}", fix=FIX)
    if scheme not in _SCHEMES:
        # 不支持的 scheme（如 htps://）不能当 http 裸连放行——SDK 根本用不了这个 URL
        return failed("provider.network", CATEGORY, "provider · network",
                      f"base_url 的 scheme 不支持：{scheme!r}（应为 http 或 https）", fix=FIX)

    proxy = _proxy_for(scheme, host)
    if proxy:
        # 直连 socket 会绕过代理、在只能走代理的企业网里造成假故障——改走代理测真实路径
        return _reachable_via_proxy(base, host, proxy)
    return _reachable_raw(scheme, host, port)


def _reachable_raw(scheme: str, host: str, port: int) -> CheckResult:
    t0 = monotonic()
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT) as sock:
            if scheme == "https":
                # 必须传 server_hostname 做 SNI + 证书校验，否则 MITM/证书不匹配也会「成功」
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
        ms = int((monotonic() - t0) * 1000)
        # 诚实措辞：只证明 TCP/TLS 通，不证明 endpoint 路径对——路径错留给 auth 层暴露
        detail = f"{host} TCP/TLS 可达（{ms}ms）" if scheme == "https" \
            else f"{host}:{port} TCP 可达（{ms}ms，无 TLS）"
        return passed("provider.network", CATEGORY, "provider · network", detail,
                      host=host, port=port, tls=(scheme == "https"), proxy_used=False)
    except socket.gaierror as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"DNS 解析失败：{host}（{e}）", fix=FIX, host=host)
    except ssl.SSLCertVerificationError as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"TLS 证书校验失败：{host}（{getattr(e, 'verify_message', None) or e}）",
                      fix=FIX, host=host)
    except ssl.SSLError as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"TLS 握手失败：{host}（{e}）", fix=FIX, host=host)
    except (ConnectionRefusedError, TimeoutError) as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"无法连接 {host}:{port}（{e}）", fix=FIX, host=host)
    except OSError as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"连接 {host}:{port} 失败（{e}）", fix=FIX, host=host)


def _reachable_via_proxy(base: str, host: str, proxy: str) -> CheckResult:
    safe = _sanitize_proxy(proxy)  # 绝不把 user:pass@ 写进可分享的输出
    t0 = monotonic()
    try:
        urllib.request.urlopen(urllib.request.Request(base, method="HEAD"), timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        # 407 来自**代理**（要鉴权），不是目标的响应——说明根本没到目标，不能算连通
        if e.code == 407:
            return failed("provider.network", CATEGORY, "provider · network",
                          f"代理要求鉴权（407）：{safe}——检查代理凭证",
                          fix=FIX, host=host, proxy_used=True, proxy=safe)
        pass  # 目标返回的 4xx/5xx 说明请求穿过代理到达了目标 = 连通
    except (urllib.error.URLError, OSError) as e:
        return failed("provider.network", CATEGORY, "provider · network",
                      f"经代理 {safe} 无法到达 {host}（{getattr(e, 'reason', e)}）",
                      fix=FIX, host=host, proxy_used=True, proxy=safe)
    ms = int((monotonic() - t0) * 1000)
    return passed("provider.network", CATEGORY, "provider · network",
                  f"{host} 经代理可达（{ms}ms）", host=host, proxy_used=True, proxy=safe)


# ------------------------------------------------------------------ auth（零成本，绝不计费）

class ProviderProbe:
    """让 auth 检查不必知道各家 provider 的端点细节。子类实现 _probe() 返回 HTTP 状态码。

    PATH_IS_STANDARD：该端点是不是「基本一定存在」的标准端点。openai 兼容的
    /v1/models 是标准端点，404 强烈说明 base_url 路径写错（如 /v2）→ 硬错误；
    anthropic 兼容各家端点可用性不一，404 可能是路径错也可能是真没这端点 → 只降级为
    warning，避免把一个可能正常的 provider 判成 unhealthy。
    """

    PATH_IS_STANDARD = True

    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.key = key

    def _get_status(self, url: str, headers: dict) -> int:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return urllib.request.urlopen(req, timeout=_TIMEOUT).status
        except urllib.error.HTTPError as e:
            return e.code   # 401/403/404… 都是「有响应」，交给上层判读

    def _probe(self) -> int:
        raise NotImplementedError

    def check_auth(self) -> CheckResult:
        try:
            status = self._probe()
        except (urllib.error.URLError, OSError) as e:
            # 网络问题不在这层报——provider · network 会报，这里只标未验证，避免两条错误
            return skipped("provider.auth", CATEGORY, "provider · auth",
                           f"网络不可达，鉴权未验证（详见 provider · network）：{getattr(e, 'reason', e)}",
                           verified=False)

        if status == 200:
            return passed("provider.auth", CATEGORY, "provider · auth",
                          "凭证有效（零成本端点验证）", verified=True, charged_request=False)
        if status in (401, 403):
            return failed("provider.auth", CATEGORY, "provider · auth",
                          f"鉴权失败：HTTP {status}——API key 无效或已过期",
                          fix=FIX, verified=True, charged_request=False, http_status=status)
        if status == 404:
            # 不能当「无端点」静默跳过——真 SDK generate 会用同一个 base_url 一样失败
            if self.PATH_IS_STANDARD:
                return failed("provider.auth", CATEGORY, "provider · auth",
                              "鉴权端点 404：base_url 的 API 路径/版本可能不对（如 /v2 应为 /v1）",
                              severity="error", fix=FIX, verified=False,
                              charged_request=False, http_status=404)
            return failed("provider.auth", CATEGORY, "provider · auth",
                          "鉴权端点 404，无法验证凭证——base_url 路径可能不对，"
                          "或该 provider 无 /v1/models 端点",
                          severity="warning", fix=FIX, verified=False,
                          charged_request=False, http_status=404)
        return skipped("provider.auth", CATEGORY, "provider · auth",
                       f"鉴权端点返回 HTTP {status}，未能判定", verified=False, http_status=status)


class OpenAICompatibleProbe(ProviderProbe):
    PATH_IS_STANDARD = True   # /v1/models 是 openai 兼容的标准端点，404 = base 路径错

    def _probe(self) -> int:
        # openai 约定 base 含 /v1；本地 vLLM 的 EMPTY key 也能取 /models
        headers = {"Authorization": f"Bearer {self.key or 'EMPTY'}"}
        return self._get_status(f"{self.base}/models", headers)


class AnthropicProbe(ProviderProbe):
    PATH_IS_STANDARD = False  # 各家 anthropic 兼容端点可用性不一，404 只降级为 warning

    def _probe(self) -> int:
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        return self._get_status(f"{self.base}/v1/models", headers)


def check_provider_auth() -> CheckResult:
    prov = _prov()
    kind = prov.get("kind", "anthropic")
    key = _key(prov)
    _, host, _, base = _endpoint(prov)

    if not host:
        return skipped("provider.auth", CATEGORY, "provider · auth", "base_url 无法解析，跳过")
    if not key and kind != "openai":
        return skipped("provider.auth", CATEGORY, "provider · auth", "缺少 API key，无法验证鉴权")

    probe = OpenAICompatibleProbe(base, key) if kind == "openai" else AnthropicProbe(base, key)
    return probe.check_auth()


CHECKS = [
    CheckSpec("provider.config", CATEGORY, "provider · config", check_provider_config),
    CheckSpec("provider.network", CATEGORY, "provider · network", check_provider_network,
              network=True),
    CheckSpec("provider.auth", CATEGORY, "provider · auth", check_provider_auth, network=True),
]
