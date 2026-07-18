"""Markdown 连接器：正文直接进 split_note（它就是按 markdown 标题切的）。"""

from pathlib import Path

from .base import Page

EXTS = {".md", ".markdown"}
MIME = "text/markdown"


def load(path) -> list[Page]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [Page(text=text, page_no=None)]
