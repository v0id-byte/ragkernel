"""传输无关的 MCP core：动态注册 Toolbox 工具 + verify；鉴权后的 principal 经 ContextVar 传入。

低层 `mcp.server.lowlevel.Server`（不用 FastMCP——工具是数据驱动的 JSON-Schema dict）。
handler 是同步且重（embed/rerank/sqlite/LLM）→ 全走线程池 + 分级限流，绝不卡事件循环。
"""

import logging
import os
import sys
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

from .. import audit, auth, config
from ..tools import Toolbox
from . import verify

log = logging.getLogger("ragkernel.mcp")


@dataclass(frozen=True)
class AgentPrincipal:
    """鉴权后的调用方身份——带 token 标识，便于审计分辨同一用户的多个 agent（claude-code/codex/CI）。"""

    user_id: int
    username: str
    token_hash: str
    token_label: str | None = None

    @property
    def fingerprint(self) -> str:
        return self.token_hash[:8]


class SessionCtx:
    """一个 principal（=一个 agent token）一个：自带 Toolbox（含 sqlite 连接）+ 独立 audit + 合并工具表。"""

    def __init__(self, principal: AgentPrincipal):
        self.principal = principal
        aud = audit.Audit(
            client=f"mcp:{principal.token_label or principal.fingerprint}",
            user_id=principal.user_id,
        )
        self.tb = Toolbox(audit=aud)
        specs, handlers = self.tb.specs_and_handlers()
        vspec, vhandler = verify.build(self.tb)
        self.specs = list(specs) + [vspec]
        self.handlers = {**handlers, "verify_engineering_claim": vhandler}
        self.lock = threading.Lock()  # Toolbox.touched/_fname 非线程安全

    def close(self) -> None:
        try:
            self.tb.db.close()
        except Exception:
            pass


_current: ContextVar[SessionCtx] = ContextVar("ragkernel_mcp_session")

# 分级限流：模型推理串行（embed/rerank 单例非线程安全）、轻量读并发、verify(含 LLM) 串行。
_model_limiter = anyio.CapacityLimiter(1)
_db_limiter = anyio.CapacityLimiter(4)
_verify_limiter = anyio.CapacityLimiter(1)
_LIGHT = {"list_documents", "list_categories", "read_document",
          # 原生 CAD 只读结构化查询（纯 sqlite 读，不推理）→ 并发限流；
          # search_engineering_objects 会 embed，故不在此列（走串行模型限流）。
          "inspect_cad_document", "list_cad_entities", "get_cad_entity",
          "get_assembly_tree", "query_geometry", "compare_cad_entities"}


def current_ctx() -> SessionCtx:
    """读当前请求的 principal 上下文。**fail-closed**：缺失即抛错，绝不回退匿名/共享 Toolbox。"""
    try:
        return _current.get()
    except LookupError as e:
        raise RuntimeError("MCP principal 上下文缺失（鉴权未生效）") from e


def _limiter_for(name: str):
    if name == "verify_engineering_claim":
        return _verify_limiter
    if name in _LIGHT:
        return _db_limiter
    return _model_limiter


def build_server(session_provider=current_ctx) -> Server:
    """构建低层 Server。session_provider() 返回当前调用方的 SessionCtx（http=ContextVar，stdio=固定单例）。"""
    server = Server("ragkernel")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        ctx = session_provider()
        return [
            types.Tool(
                name=s["name"],
                description=s.get("description", ""),
                inputSchema=s.get("input_schema") or {"type": "object", "properties": {}},
            )
            for s in ctx.specs
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.ContentBlock]:
        ctx = session_provider()
        fn = ctx.handlers.get(name)
        if fn is None:
            raise ValueError(f"未知工具 {name}")  # SDK 转 CallToolResult(isError=True)

        def run():
            with ctx.lock:
                try:
                    return fn(**(arguments or {}))
                finally:
                    ctx.tb.reset_call_state()  # 别让引用轨迹跨调用无限增长

        try:
            out = await anyio.to_thread.run_sync(run, limiter=_limiter_for(name))
        except Exception:
            rid = uuid.uuid4().hex[:12]
            log.exception("MCP 工具 %s 执行失败 rid=%s", name, rid)  # 服务端记全 traceback
            raise RuntimeError(f"工具执行失败，错误编号 {rid}")  # 脱敏：不外泄 DB路径/URL/SQL/secret

        return [types.TextContent(type="text", text=out if isinstance(out, str) else str(out))]

    return server


def run_stdio() -> None:
    """本地/air-gap：token 从 env RAGKERNEL_TOKEN，启动解析一次，无效则 fail-closed 退出（单 principal）。"""
    config.load_env()
    token = os.environ.get("RAGKERNEL_TOKEN", "")
    rec = auth.resolve_agent_token(token) if token else None
    if not rec:
        sys.exit("RAGKERNEL_TOKEN 缺失或无效——先用 `ragkernel token new --user <name>` 生成 agent token")
    principal = AgentPrincipal(rec["id"], rec["username"], rec["token_hash"], rec.get("token_label"))
    ctx = SessionCtx(principal)
    server = build_server(session_provider=lambda: ctx)

    from mcp.server.stdio import stdio_server

    async def go():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    anyio.run(go)
