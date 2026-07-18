"""DOCX 连接器（python-docx）。Word 标题样式 → markdown '#'，好让 split_note 拿到结构。"""

from .base import Page

EXTS = {".docx"}
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def load(path) -> list[Page]:
    import docx as pydocx

    doc = pydocx.Document(str(path))
    lines: list[str] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = (p.style.name if p.style else "") or ""
        low = style.lower()
        if low.startswith("heading") or low in ("title", "subtitle"):
            digits = "".join(ch for ch in style if ch.isdigit())
            level = int(digits) if digits else (1 if low == "title" else 2)
            lines.append("#" * min(max(level, 1), 6) + " " + text)
        else:
            lines.append(text)
    return [Page(text="\n\n".join(lines), page_no=None)]
