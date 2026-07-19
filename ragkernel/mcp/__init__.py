"""MCP Server：把 RagKernel 的只读检索能力暴露给 Agent（Claude Code / Codex 等）。

薄层——遍历 `Toolbox.specs_and_handlers()` 动态注册工具，鉴权复用 `auth` 的 agent token。
主传输 streamable-http（远程瘦客户端），stdio 为本地/air-gap 副产物。
"""

from .server import build_server, run_stdio

__all__ = ["build_server", "run_stdio"]
