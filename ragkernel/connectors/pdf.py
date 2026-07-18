"""PDF 连接器（pypdf，纯 python 轻量；保留 page_no 供引用溯源）。

质量不够（复杂版式/表格/扫描件）时的升级路径：pdfplumber / PyMuPDF / OCR —— 见 verticals。
"""

from .base import Page

EXTS = {".pdf"}
MIME = "application/pdf"


def load(path) -> list[Page]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for i, pg in enumerate(reader.pages, 1):
        try:
            txt = pg.extract_text() or ""
        except Exception:
            txt = ""
        pages.append(Page(text=txt, page_no=i))
    return pages
