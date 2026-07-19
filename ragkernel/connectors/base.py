"""连接器公共类型。每个连接器把一个文件读成 [Page]，喂给共享的 split_note→seg→store 管线。"""

from dataclasses import dataclass, field

# 元素类型：分块器按此路由（表格按行 / 工序整片 / 键值逐条 / 尺寸标注 / 正文按大小）。
ELEMENT_TYPES = ("heading", "prose", "table", "procedure", "kv", "figure", "dimension")


@dataclass
class Page:
    text: str
    page_no: int | None = None  # 分页格式（PDF）才有；DOCX/MD/TXT 为 None
    # 以下两项供 PRECHUNKED 连接器（如 CAD）使用：本页即一片、连接器自己定边界与元数据。
    # 默认空 → 既有连接器完全不受影响。
    title: str | None = None    # 直接作为该 chunk 的标题（如 "【STEP · Part: OutputShaft】"）
    meta: dict = field(default_factory=dict)  # 直接写入该片 meta_json（如 CAD 实体溯源键）


@dataclass
class Block:
    """有类型的版面元素——分块的统一契约。`chunk_blocks()` 只认 Block。

    现状（Phase 1 + Phase 2 Option B）：Block 由 `chunking.md_to_blocks()` 从 markdown 还原。
    Phase 2 的 layout 解析器（Docling）走的是「逐页干净 markdown 的 Page」→ md_to_blocks，
    因此 **page 由 Page.page_no 承载、bbox 暂未贯通**（markdown 回环丢了坐标）。
    后续升级（Phase 2.5）：layout 直接 Docling Item→Block、填 page/bbox/source_item_id，
    支撑「答案来自第 14 页某区域 [x1,y1,x2,y2]」的区域级证据（届时 chunk_blocks 不动）。
    """

    element_type: str                       # ELEMENT_TYPES 之一
    text: str                               # 元素纯文本（table 用 table_md）
    page: int | None = None                 # 页码（当前由 Page.page_no 承载）
    table_md: str | None = None             # element_type=='table' 时的 markdown 表
    section_path: tuple[str, ...] = ()       # 章节面包屑，如 ("3 故障排查","3.2 故障代码表")
    meta: dict = field(default_factory=dict)  # 领域抽取键：fault_code/pin_label/part_no…
    # bbox/source_item_id 待 Phase 2.5 直出 Block 时填（见类注释）——现不虚设未填字段。


@dataclass
class LoadedDoc:
    filename: str
    sha256: str
    mime: str
    pages: list[Page]
    meta: dict = field(default_factory=dict)


@dataclass
class ConnectorResult:
    """PRECHUNKED 连接器 `load_bundle()` 的返回：一次解析同时产出检索层与结构化工程层。

    pages：喂现有 chunk/embed/检索管线（PRECHUNKED 时每 Page 即一片）。
    engineering_entities：存 `engineering_entities` 表的 store-ready 行（*_json 已是字符串）。
    source_metadata：文档级元数据（单位块 / warnings / aborted+abort_reason 等）。
    """

    pages: list[Page]
    engineering_entities: list[dict] = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)
