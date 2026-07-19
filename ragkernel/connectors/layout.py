"""版面解析连接器（Phase 2）：Docling + RapidOCR 把 PDF → **逐页干净 markdown** 的 Page，
带真实页码——修 MarkItDown 的乱序 / 无 OCR / 表格塌成空格子，并让 `p.<页>` 引用生效。

产出逐页 markdown 后，复用 Phase 1 的 md_to_blocks→chunk_blocks（表格按行 / 工序整片 / 键值逐条），
故换解析引擎不动分块逻辑。中文 OCR 用 **RapidOCR**（= PaddleOCR 模型的 ONNX，Apache，CPU 友好）。

依赖重（GB 级、首次下模型），故惰性导入；转换失败优雅回退 MarkItDown(richdoc) → pypdf，
保证不因单一依赖失效而不可用。Office/HTML 仍走 richdoc（MarkItDown 擅长处），本连接器只接 PDF。
"""

from pathlib import Path

from . import pdf as _pdf
from . import richdoc as _richdoc
from .base import Page

EXTS = {".pdf"}
MIME = "text/markdown"

_conv: dict[str, object] = {}


def _converter(mode: str = "off"):
    """三档，都保留 TableFormer 表结构：
      off    —— born-digital 快路径：只用文本层、不跑 OCR（最快，矢量文本不被 OCR 误改）。
      region —— do_ocr 但只 OCR 位图/图片区域、**保留文本层**（图纸带栅格标注、局部扫描）。
      full   —— 整页强制 OCR（扫描件/整页图片型 PDF，几乎无文本层）。
    对矢量图纸不整页 OCR 是刻意的——OCR 会把清晰的文本层误识成 0/O、1/I、错小数点。"""
    if mode not in _conv:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_table_structure = True
        opts.do_ocr = mode != "off"
        if mode != "off":
            opts.ocr_options = RapidOcrOptions(force_full_page_ocr=(mode == "full"))  # 中英 RapidOCR ONNX
        _conv[mode] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _conv[mode]


def _pages_markdown(doc) -> list[Page]:
    """把 DoclingDocument 的有类型元素按页汇成 markdown：标题→'#'、表格→markdown 表、其余→段落。
    喂给 md_to_blocks 时结构已干净，表格是真 `| |` 表、阅读顺序已修。

    维护跨页 heading 栈：每页开头继承上文当前的章节路径，让**跨页/续表**的行不丢 section_path
    （如 §3.5 CN1 端子表跨到下一页、寄存器章节续页）。"""
    from docling_core.types.doc import DocItemLabel, TableItem

    buckets: dict[int, list[str]] = {}
    stack: list[tuple[int, str]] = []  # 当前活跃章节 (level, title)
    for item, _level in doc.iterate_items():
        prov = item.prov[0] if getattr(item, "prov", None) else None
        page = prov.page_no if prov else 1
        if page not in buckets:  # 新页开头继承上文章节路径
            buckets[page] = ["#" * min(lvl, 6) + " " + t for lvl, t in stack]
        if isinstance(item, TableItem):
            md = item.export_to_markdown(doc)
            if md.strip():
                buckets[page].append(md)
            continue
        txt = (getattr(item, "text", "") or "").strip()
        if not txt:
            continue
        lbl = getattr(item, "label", None)
        if lbl in (DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE):
            h = min(max(getattr(item, "level", 1) or 1, 1), 6)
            while stack and stack[-1][0] >= h:
                stack.pop()
            stack.append((h, txt))
            buckets[page].append("#" * h + " " + txt)
        else:
            buckets[page].append(txt)
    return [Page(text="\n\n".join(parts), page_no=pg) for pg, parts in sorted(buckets.items()) if parts]


def load(path) -> list[Page]:
    path = Path(path)
    try:
        doc = _converter("off").convert(str(path)).document  # 快路径：born-digital 免 OCR
        pages = _pages_markdown(doc)
        npages = doc.num_pages() or 1
        chars = sum(len(p.text) for p in pages)
        if chars < 5 * npages:        # 几乎无文本层（扫描件/整页图片）→ 整页强制 OCR
            doc = _converter("full").convert(str(path)).document
            pages = _pages_markdown(doc) or pages
        elif chars < 40 * npages:     # 文本稀疏（图纸带栅格标注/局部扫描）→ 区域 OCR，保文本层
            doc = _converter("region").convert(str(path)).document
            pages = _pages_markdown(doc) or pages
        if pages:
            return pages
        raise ValueError("空解析结果")
    except Exception as e:
        print(f"[docling] 解析 {path.name} 失败，回退 MarkItDown：{type(e).__name__}: {e}")
        try:
            return _richdoc.load(path)
        except Exception as e2:
            print(f"[docling] MarkItDown 也失败，回退 pypdf：{type(e2).__name__}: {e2}")
            return _pdf.load(path)
