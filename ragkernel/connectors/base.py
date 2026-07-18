"""连接器公共类型。每个连接器把一个文件读成 [Page]，喂给共享的 split_note→seg→store 管线。"""

from dataclasses import dataclass, field


@dataclass
class Page:
    text: str
    page_no: int | None = None  # 分页格式（PDF）才有；DOCX/MD/TXT 为 None


@dataclass
class LoadedDoc:
    filename: str
    sha256: str
    mime: str
    pages: list[Page]
    meta: dict = field(default_factory=dict)
