"""streamable-http 传输（主）：ASGI bearer 中间件 → resolve_agent_token → 401 或绑 principal。

stateless 模式：每个 HTTP 请求重新过 bearer 校验，不存在跨请求 MCP session 身份漂移。
principal 经 ContextVar 传入 tool handler（anyio 在 stateless 任务派生时会拷贝当前上下文，已验证）。
仅此文件依赖 Starlette/uvicorn。
"""

import contextlib
import json
import threading

from .. import auth, config
from .server import AgentPrincipal, SessionCtx, _current, build_server, current_ctx

_sessions: dict[str, SessionCtx] = {}  # 键 = token_hash（按 token 复用，不是 user_id）
_slock = threading.Lock()


def _get_or_make_ctx(p: AgentPrincipal) -> SessionCtx:
    with _slock:
        ctx = _sessions.get(p.token_hash)
        if ctx is None:
            ctx = SessionCtx(p)
            _sessions[p.token_hash] = ctx
        return ctx


async def _json_401(send) -> None:
    body = json.dumps({"error": "invalid_or_expired_token"}).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"www-authenticate", b"Bearer"),  # 不区分不存在/过期/撤销，避免泄漏 token 状态
    ]})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """在 MCP 协议握手之前完成鉴权：无/错/非 agent token → 401，MCP 会话根本不建立。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan 等直接放行
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode()
        token = raw[7:] if raw.startswith("Bearer ") else ""
        rec = auth.resolve_agent_token(token) if token else None  # 只认 token_kind='agent'
        if not rec:
            return await _json_401(send)
        p = AgentPrincipal(rec["id"], rec["username"], rec["token_hash"], rec.get("token_label"))
        ctx = _get_or_make_ctx(p)
        tok = _current.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(tok)


def build_app():
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = build_server(session_provider=current_ctx)
    mgr = StreamableHTTPSessionManager(app=server, event_store=None, json_response=True, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mgr.run():
            yield
        with _slock:  # 关停：关掉所有缓存的 Toolbox sqlite 连接
            for ctx in _sessions.values():
                ctx.close()
            _sessions.clear()

    async def handle_mcp(scope, receive, send):
        await mgr.handle_request(scope, receive, send)

    inner = Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)
    return BearerAuthMiddleware(inner)


def run_http(host: str, port: int) -> None:
    import uvicorn

    config.load_env()
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"⚠️  MCP 绑定在 {host}（非本机回环）：明文 bearer token 会被中间人截获，"
              "生产必须经反向代理走 HTTPS。")
    uvicorn.run(build_app(), host=host, port=port, log_level="info")
