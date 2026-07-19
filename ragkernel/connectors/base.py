"""连接器公共类型。每个连接器把一个文件读成 [Page]，喂给共享的 split_note→seg→store 管线。"""

from dataclasses import dataclass, field

# 元素类型：分块器按此路由（表格按行 / 工序整片 / 键值逐条 / 正文按大小）。
ELEMENT_TYPES = ("heading", "prose", "table", "procedure", "kv", "figure")


@dataclass
class Page:
    text: str
    page_no: int | None = None  # 分页格式（PDF）才有；DOCX/MD/TXT 为 None


@dataclass
class Block:
    """有类型的版面元素——分块的统一契约。

    Phase 1：由 chunking.md_to_blocks() 从扁平 markdown 还原（page/bbox 缺省）。
    Phase 2：由 layout 解析器（Docling 等）直接产出，带真实 page/bbox/表格结构。
    分块器只认 Block，故换解析引擎不动分块逻辑。
    """

    element_type: str                       # ELEMENT_TYPES 之一
    text: str                               # 元素纯文本（table 用 table_md）
    page: int | None = None                 # 真实页码（Phase 2 才有）
    table_md: str | None = None             # element_type=='table' 时的 markdown 表
    section_path: tuple[str, ...] = ()       # 章节面包屑，如 ("3 故障排查","3.2 故障代码表")
    meta: dict = field(default_factory=dict)  # 领域抽取键：fault_code/pin_label/part_no…


@dataclass
class LoadedDoc:
    filename: str
    sha256: str
    mime: str
    pages: list[Page]
    meta: dict = field(default_factory=dict)
