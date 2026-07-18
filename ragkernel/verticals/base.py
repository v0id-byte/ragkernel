"""可插拔垂直层的接口。本期只实现 NullVertical（全空）；具体垂直（合同条款抽取/风险标注等）
是将来独立模块，实现该协议、不碰内核。

四个 hook 覆盖三种机制（自定义工具 / prompt 片段 / 检索后抽取）+ 入库标注：
  system_fragment  → 拼进中立 system prompt 末尾
  extra_tools      → 并入 Toolbox 的 (anthropic specs, name→handler)
  post_retrieve    → 对 hybrid_search 命中做标注/过滤/重排（合同风险抽取的天然落点）
  on_ingest        → 入库前给 chunk 打垂直 meta_json（后续可经 where seam 过滤）
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Vertical(Protocol):
    name: str

    def system_fragment(self) -> str: ...

    def extra_tools(self) -> tuple[list[dict], dict]: ...

    def post_retrieve(self, query: str, chunks: list) -> list: ...

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]: ...


class NullVertical:
    """无操作垂直层：每个 hook 恒等/为空。vertical: none 时使用。"""

    name = "none"

    def system_fragment(self) -> str:
        return ""

    def extra_tools(self) -> tuple[list[dict], dict]:
        return [], {}

    def post_retrieve(self, query: str, chunks: list) -> list:
        return chunks

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]:
        return chunks
