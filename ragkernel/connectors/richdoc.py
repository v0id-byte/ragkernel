"""富文档连接器：用 Microsoft MarkItDown 把 PDF / Word / PPT / HTML 转成 Markdown 再入库，
保留标题/表格结构，喂给垂直层 split 效果远好于裸文本抽取。

致谢 / Acknowledgment：文档转换由 **MarkItDown**（Microsoft，MIT License）提供，
仓库 https://github.com/microsoft/markitdown 。见 README「致谢」。

转换失败时对 PDF/DOCX 优雅回退到 pypdf / python-docx（保证不因单一依赖失效而不可用）。
"""

from pathlib import Path

from . import docx as _docx
from . import pdf as _pdf
from .base import Page

EXTS = {".pdf", ".docx", ".pptx", ".ppt", ".html", ".htm"}
MIME = "text/markdown"

_md = None


def _converter():
    global _md
    if _md is None:
        from markitdown import MarkItDown

        _md = MarkItDown()
    return _md


def load(path) -> list[Page]:
    path = Path(path)
    try:
        text = (_converter().convert(str(path)).text_content or "").strip()
        if text:
            return [Page(text=text, page_no=None)]  # MarkItDown 产出整篇 md，无分页
        raise ValueError("空转换结果")
    except Exception as e:
        print(f"[markitdown] 转换 {path.name} 失败，回退：{type(e).__name__}: {e}")
        ext = path.suffix.lower()
        if ext == ".pdf":
            return _pdf.load(path)
        if ext == ".docx":
            return _docx.load(path)
        raise
