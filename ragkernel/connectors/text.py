"""纯文本连接器。无标题时 split_note 走段落切分兜底。"""

from pathlib import Path

from .base import Page

EXTS = {".txt"}
MIME = "text/plain"


def load(path) -> list[Page]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [Page(text=text, page_no=None)]
