"""可插拔垂直层的接口。本期实现 NullVertical(全空)+ equipment(设备维修)。
具体垂直是独立模块,实现该协议、不碰内核。

五个 hook 覆盖:拆片(split)/ 分类打标(在 split 或 on_ingest 里写 meta)/ 检索后处理(post_retrieve)/
自定义工具(extra_tools)/ prompt 片段(system_fragment)。
  split(page_text, page_no) -> [(title, body, meta)] | None   # None = 用内核默认 split_note（手册细分）
  classify(text)            -> str | None                     # 单条记录（工单/反馈）的分类；None=不打标
  extra_tools(toolbox)      -> (anthropic specs, name->handler)  # 收 toolbox,复用其 db/检索
  post_retrieve(query, chunks) -> chunks
  on_ingest(doc, chunks)    -> chunks
  system_fragment()         -> str
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Vertical(Protocol):
    name: str

    def system_fragment(self) -> str: ...

    def split(self, page_text: str, page_no: int | None) -> list[tuple[str, str, dict]] | None: ...

    def classify(self, text: str) -> str | None: ...

    def extra_tools(self, toolbox) -> tuple[list[dict], dict]: ...

    def post_retrieve(self, query: str, chunks: list) -> list: ...

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]: ...


class NullVertical:
    """无操作垂直层:每个 hook 恒等/为空。vertical: none 时使用。"""

    name = "none"

    def system_fragment(self) -> str:
        return ""

    def split(self, page_text: str, page_no: int | None):
        return None  # → 内核默认 split_note

    def classify(self, text: str) -> str | None:
        return None

    def extra_tools(self, toolbox) -> tuple[list[dict], dict]:
        return [], {}

    def post_retrieve(self, query: str, chunks: list) -> list:
        return chunks

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]:
        return chunks
