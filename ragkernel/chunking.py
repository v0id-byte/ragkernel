"""分块与中文分词：markdown → 有类型元素(Block) → 元素级分块。

拆片思路（调研定：原子单元 = 技师会用手指点的最小自足答案）：
  表格   → 一行一片（表头前置到每行，行内自足）
  工序   → 整片不拆（步骤+内联警告不能拆散）
  键值   → 逐条一片（P1—VCC+5V 一条一片，可答「电机P3是什么线」）
  正文   → 按标题 + 大小切（split_note）

Phase 1 在扁平 markdown 上重建结构（md_to_blocks）；Phase 2 换真·版面解析器时，
连接器直接产出 Block，chunk_blocks 一行不动。
"""

import re
from pathlib import Path

import jieba

from .connectors.base import Block

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
_MD_ROW = re.compile(r"^\s*\|.*\|\s*$")                       # markdown 表格行
_ORDERED = re.compile(r"^\s*(?:\d{1,3}|[一二三四五六七八九十]+)\s*[.、)]\s*\S")  # 编号步骤
_KV = re.compile(r"^\s*([^\n:：|]{1,24})[：:]\s*(\S.*)$")      # 键：值
_KV_DASH = re.compile(r"^\s*([A-Za-z]{1,6}\d{0,3})\s*[-—]{1,3}\s*(\S.*)$")  # P1—VCC+5V
# 标识符：型号/故障码/料号/针脚——字母数字被标点连接、或字母+数字混合的短串
_IDENT = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)+|[A-Za-z]+\d+|\d+[A-Za-z]+")


def seg(text: str) -> str:
    """jieba 搜索引擎粒度分词 + 保留标识符整 token，喂 FTS5。

    jieba/unicode61 会把 `E-42`、`XH2.54-8P` 切散，故额外把标识符原样(小写)与去标点
    形式(e42)追加进分词串，让 BM25 能精确命中故障码/料号/针脚。存储侧与查询侧同一函数。
    """
    base = [t for t in jieba.cut_for_search(text) if t.strip()]
    extra: list[str] = []
    for tok in _IDENT.findall(text):
        low = tok.lower()
        extra.append(low)
        joined = re.sub(r"[.\-\s]", "", low)
        if joined and joined != low:
            extra.append(joined)
    return " ".join(base + extra)


_VERSION = re.compile(r"^(?:v|rev|ver|版)?\d+(?:\.\d+)*$", re.I)  # V2 / Rev3 / 1.9 / 版2


def model_hint(filename: str) -> str:
    """从文件名抽设备型号（如 `GM28-EC2860H - 图纸1.pdf` → `GM28-EC2860H`），供上下文前缀。

    只取字母数字混合、长度≥4 的型号样 token；跳过年份(2024)与版本(V2/Rev3/1.9)，
    避免把版本号/年份误当型号。取不到就返回空（宁缺毋滥）。
    """
    stem = Path(filename).stem
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", stem):
        if len(tok) < 4 or not re.search(r"\d", tok):
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", tok) or _VERSION.match(tok):  # 年份/版本
            continue
        # 去掉版本/年份样 hyphen 段(Rev3/V2/2024)后核心仍需含数字，避免 "Document-Rev3"/"Report-2024" 词+版本/年份
        core = "-".join(
            s for s in tok.split("-") if not re.fullmatch(r"(?:v|rev|ver)\d+|(?:19|20)\d{2}", s, re.I)
        )
        if len(core) < 4 or not re.search(r"\d", core):
            continue
        if tok.lower() in ("pdf", "docx", "html"):
            continue
        return tok
    return ""


# 工程尺寸/标注模式（Ø8 · R2 · M4 · 45° · 12±0.1 · 4×M3 · 24mm · 10-20mm）——绝不当噪声删。
_DIMENSION = re.compile(
    r"[Ø⌀RrMm]\s*\d+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*(?:mm|cm|μm|um|nm|in|°|N·?m|kg|g)"
    r"|\d+(?:\.\d+)?\s*±\s*\d+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?\s*(?:mm|cm|°)?",
    re.I,
)


def compose_context(model: str, meta: dict) -> str:
    """确定性上下文前缀（Anthropic Contextual Retrieval 的零成本版）：把 型号·章节·表标题
    合成一行前缀，前置进正文——让「E-42|过温」也能被「X型号 主轴 E-42」检出。"""
    parts: list[str] = []
    if model:
        parts.append(model)
    sp = meta.get("section_path") or []
    if sp:
        parts.append(sp[-1] if isinstance(sp, (list, tuple)) else str(sp))
    tt = meta.get("table_title")
    if tt and (not sp or tt != sp[-1]):
        parts.append(str(tt))
    parts = [p for p in parts if p]
    return f"【{' · '.join(parts)}】" if parts else ""


# ── markdown → Block ──────────────────────────────────────────────────────


def _sp(section: list[tuple[int, str]]) -> tuple[str, ...]:
    return tuple(title for _lvl, title in section)


def md_to_blocks(text: str) -> list[Block]:
    """把扁平 markdown 还原成有类型元素：标题→section_path 面包屑，表格/编号步骤/键值/正文。"""
    lines = text.splitlines()
    blocks: list[Block] = []
    section: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush_prose():
        para = "\n".join(buf).strip()
        buf.clear()
        if not para:
            return
        nonblank = [l for l in para.splitlines() if l.strip()]
        kv = [l for l in nonblank if _KV.match(l) or _KV_DASH.match(l)]
        if nonblank and len(kv) >= max(1, round(len(nonblank) * 0.6)):
            for l in nonblank:  # 键值段：逐行成 kv 元素
                blocks.append(Block("kv", l.strip(), section_path=_sp(section)))
        else:
            blocks.append(Block("prose", para, section_path=_sp(section)))

    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADING.match(line)
        if h:
            flush_prose()
            level, title = len(h.group(1)), h.group(2).strip()
            while section and section[-1][0] >= level:
                section.pop()
            section.append((level, title))
            i += 1
            continue
        if _MD_ROW.match(line):
            flush_prose()
            tbl = []
            while i < len(lines) and _MD_ROW.match(lines[i]):
                tbl.append(lines[i])
                i += 1
            tmd = "\n".join(tbl)
            blocks.append(Block("table", tmd, table_md=tmd, section_path=_sp(section)))
            continue
        if _ORDERED.match(line):
            flush_prose()
            steps = []
            while i < len(lines):
                if _ORDERED.match(lines[i]):
                    steps.append(lines[i].rstrip())
                    i += 1
                elif not lines[i].strip() and i + 1 < len(lines) and _ORDERED.match(lines[i + 1]):
                    i += 1  # 步骤间空行
                else:
                    break
            if len(steps) >= 2:
                blocks.append(Block("procedure", "\n".join(steps), section_path=_sp(section)))
            else:
                buf.extend(steps)
            continue
        if not line.strip():
            flush_prose()
            i += 1
            continue
        buf.append(line)
        i += 1
    flush_prose()
    return blocks


# ── 表格：一行一片（含空表格里稀疏真内容的抢救） ───────────────────────────


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(cells: list[str]) -> bool:
    return any("-" in c for c in cells) and all((not c) or set(c) <= set("-: ") for c in cells)


def _parse_table(table_md: str) -> tuple[list[str], list[list[str]]]:
    rows = [_cells(l) for l in table_md.splitlines() if l.strip()]
    sep = next((i for i, c in enumerate(rows) if _is_sep(c)), None)
    header = rows[sep - 1] if (sep and sep >= 1 and any(rows[sep - 1])) else []
    data = [c for i, c in enumerate(rows) if not _is_sep(c) and not (header and i == sep - 1)]
    return header, data


def _row_text(header: list[str], cells: list[str]) -> str:
    idx = [j for j, c in enumerate(cells) if c]
    if not idx:
        return ""
    hdr_n = [h for h in header if h]
    if len(hdr_n) >= 2 and len(idx) >= 2:  # 有表头：键：值配对
        return " | ".join(
            (f"{header[j]}：{cells[j]}" if j < len(header) and header[j] else cells[j]) for j in idx
        )
    return " · ".join(cells[j] for j in idx)  # 无表头/稀疏：抢救非空单元


_SUBTYPE = [
    ("故障码表", ("故障码", "故障代码", "报警码", "错误码", "code", "e-")),
    ("针脚表", ("端子", "针脚", "引脚", "接口", "pin", "线序")),
    ("参数表", ("参数", "额定", "规格", "电压", "电流", "功率")),
    ("备件表", ("料号", "零件号", "物料", "bom", "备件")),
]


def _table_subtype(header: list[str], title: str) -> str:
    hay = (" ".join(header) + " " + (title or "")).lower()
    for name, kws in _SUBTYPE:
        if any(kw in hay for kw in kws):
            return name
    return ""


def _chunk_table(b: Block, tid: str, min_chars: int, out: list) -> None:
    header, data = _parse_table(b.table_md or b.text)
    title = b.section_path[-1] if b.section_path else ""
    subtype = _table_subtype(header, title)
    rows = [r for r in (_row_text(header, c) for c in data) if r]
    if not rows:
        return
    header_line = " | ".join(h for h in header if h)
    base_meta = {
        "element_type": "table", "section_path": list(b.section_path),
        "table_title": title or subtype, "table_id": tid, "table_subtype": subtype,
    }
    # 故障码/针脚/参数/备件表 → 恒按行拆（每行是独立查找目标，「E-42怎么处理」要命中 E-42 那行，
    # 不能是 E-42+E-15 一坨）。其余小表整片、大表按行。表头前置到每行→行内自足。
    per_row = len(rows) > 1 and (bool(subtype) or sum(len(r) for r in rows) >= min_chars)
    if not per_row:
        body = (header_line + "\n" if header_line else "") + "\n".join(rows)
        out.append((title or subtype or "表格", body, base_meta))
        return
    for r in rows:
        body = (header_line + "\n" if header_line else "") + r
        out.append((title or subtype or "表格行", body, dict(base_meta)))


def _trivial(body: str) -> bool:
    """去掉数字/标点后不足 2 个实义字符——图纸里孤立的页码/网格号/单字符噪声（1/0/A）。
    注意：工程尺寸（Ø8/M4/45°/12±0.1）会先被 _DIMENSION 命中留下，不走这里删。"""
    return len(re.sub(r"[\s\d.,:：;；\-–—+*/()|]", "", body)) < 2


def _chunk_prose(b: Block, min_chars: int, max_chars: int, out: list) -> None:
    for t, body in split_note(b.text, min_chars, max_chars):
        dim = _DIMENSION.search(body)
        if _trivial(body) and not dim:  # 纯噪声删；但工程尺寸必留
            continue
        sp = list(b.section_path)
        # 短的尺寸标注独立成 dimension 片（供尺寸检索/过滤）；其余按元素本类型
        et = "dimension" if (dim and len(body.strip()) <= 40) else b.element_type
        meta = {"element_type": et, "section_path": sp}
        if et == "dimension":
            meta["dimension_raw"] = dim.group(0).strip()
        out.append((t or (sp[-1] if sp else ""), body, meta))


def chunk_blocks(blocks: list[Block], min_chars: int = 200, max_chars: int = 3000) -> list[tuple[str, str, dict]]:
    """元素级分块：按 element_type 路由到对应分块器。返回 [(title, body, meta)]。"""
    out: list[tuple[str, str, dict]] = []
    tcount = 0
    for b in blocks:
        if b.element_type == "table":
            tcount += 1
            _chunk_table(b, f"{b.page or 0}-t{tcount}", min_chars, out)
        elif b.element_type == "procedure":
            body = b.text.strip()
            title = body.splitlines()[0].strip()[:60] if body else ""
            out.append((title, body, {"element_type": "procedure", "section_path": list(b.section_path)}))
        elif b.element_type == "kv":
            line = b.text.strip()
            if _trivial(line) and not _DIMENSION.search(line):
                continue
            m = _KV.match(line) or _KV_DASH.match(line)
            key = (m.group(1).strip()[:40] if m else "")
            out.append((key, line, {"element_type": "kv", "section_path": list(b.section_path)}))
        else:  # prose / heading / figure
            _chunk_prose(b, min_chars, max_chars, out)
    return out


# ── 内核默认拆片（无垂直层 / 兜底；prose 分块也复用它） ─────────────────────


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
