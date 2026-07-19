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

_conv: dict[bool, object] = {}


def _converter(force_ocr: bool = False):
    """普通转换器复用文本层 + 图片区域 OCR；force_ocr=True 整页当图片 OCR（救扫描/图片型 PDF）。"""
    if force_ocr not in _conv:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_table_structure = True   # TableFormer：还原行列/合并单元格
        opts.do_ocr = True               # 扫描/图片区域走 OCR；born-digital 用文本层
        opts.ocr_options = RapidOcrOptions(force_full_page_ocr=force_ocr)  # 中英文 RapidOCR（离线 ONNX）
        _conv[force_ocr] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _conv[force_ocr]


def _pages_markdown(doc) -> list[Page]:
    """把 DoclingDocument 的有类型元素按页汇成 markdown：标题→'#'、表格→markdown 表、其余→段落。
    喂给 md_to_blocks 时结构已干净，表格是真 `| |` 表、阅读顺序已修。"""
    from collections import defaultdict

    from docling_core.types.doc import DocItemLabel, TableItem

    buckets: dict[int, list[str]] = defaultdict(list)
    for item, _level in doc.iterate_items():
        prov = item.prov[0] if getattr(item, "prov", None) else None
        page = prov.page_no if prov else 1
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
            buckets[page].append("#" * h + " " + txt)
        else:
            buckets[page].append(txt)
    return [Page(text="\n\n".join(parts), page_no=pg) for pg, parts in sorted(buckets.items()) if parts]


def load(path) -> list[Page]:
    path = Path(path)
    try:
        doc = _converter().convert(str(path)).document
        pages = _pages_markdown(doc)
        # 文本稀疏（扫描件/整页图片型 PDF，无文本层）→ 整页强制 OCR 重试
        npages = doc.num_pages() or 1
        if sum(len(p.text) for p in pages) < 40 * npages:
            doc = _converter(force_ocr=True).convert(str(path)).document
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
