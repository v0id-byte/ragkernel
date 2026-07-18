"""分块与中文分词：markdown 标题切块 + jieba 分词（喂 FTS5）。"""

import re

import jieba

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def seg(text: str) -> str:
    """jieba 搜索引擎粒度分词，喂 FTS5（存储侧与查询侧必须同一函数）。"""
    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


def split_note(text: str, min_chars: int = 200, max_chars: int = 3000) -> list[tuple[str, str]]:
    """按 markdown 标题切块，返回 [(title, body)]。小块并入前块，大块按段落再切。

    无标题的纯文本（PDF/TXT 抽出的正文）走 fall-through：整段进 (「」, body)，
    再由 max_chars 段落切分兜底。
    """
    pieces: list[tuple[str, str]] = []
    last_end = 0
    last_title = ""
    for m in _HEADING.finditer(text):
        body = text[last_end : m.start()].strip()
        if body:
            pieces.append((last_title, body))
        last_title = m.group(2).strip()
        last_end = m.end()
    tail = text[last_end:].strip()
    if tail:
        pieces.append((last_title, tail))

    merged: list[tuple[str, str]] = []
    for title, body in pieces:
        if merged and len(body) < min_chars:
            pt, pb = merged[-1]
            merged[-1] = (pt, pb + "\n\n" + (f"## {title}\n" if title else "") + body)
        else:
            merged.append((title, body))

    out: list[tuple[str, str]] = []
    for title, body in merged:
        if len(body) <= max_chars:
            out.append((title, body))
            continue
        paras = re.split(r"\n\n+", body)
        buf = ""
        for p in paras:
            if buf and len(buf) + len(p) > max_chars:
                out.append((title, buf.strip()))
                buf = p
            else:
                buf += "\n\n" + p
        if buf.strip():
            out.append((title, buf.strip()))
    return out
