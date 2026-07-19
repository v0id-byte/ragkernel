"""连接器注册表：扩展名 → 加载模块。加新格式只需写一个带 EXTS/MIME/load 的模块并在此登记。"""

from pathlib import Path

from . import layout, markdown, richdoc, table, text

# layout（Docling+RapidOCR）接管 PDF（版面+OCR+表格，回退 MarkItDown→pypdf）；
# richdoc（MarkItDown）接管 docx/pptx/html。layout 在 richdoc 之后登记，故 .pdf 归 layout。
# pdf.py/docx.py 仍在，作为各自回退（不单独登记）。
_MODULES = (richdoc, layout, markdown, text, table)
_REGISTRY: dict[str, object] = {}
for _mod in _MODULES:
    for _ext in _mod.EXTS:
        _REGISTRY[_ext] = _mod


def loader_for(path):
    """返回能处理该文件的连接器模块，或 None。"""
    return _REGISTRY.get(Path(path).suffix.lower())


def supported_exts() -> set[str]:
    return set(_REGISTRY)
