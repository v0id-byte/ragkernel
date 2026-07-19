"""原生 CAD 连接器（STEP/STL）。

PRECHUNKED + load_bundle：一次解析同时产出检索层（Page）与结构化工程层（engineering_entities 行）。
重依赖（trimesh / OpenCASCADE-OCP）在 load_bundle 内**惰性导入**——本模块被 connectors 包在
import 期加载不会触发它们，缺失只在真正摄取 CAD 文件时报清晰安装提示。
"""

from pathlib import Path

from .base import ConnectorResult, Page

EXTS = {".step", ".stp", ".stl"}
MIME = "model/cad"
SOURCE_KIND = "cad_import"
PRECHUNKED = True  # 每 Page 即一片；pipeline 直接用 page.title/page.meta，不走垂直层 split（垂直层无关）

_STEP_HINT = (
    "STEP 摄取需要 OpenCASCADE（OCP）：安装可选依赖 `pip install 'ragkernel[cad]'`"
    "（或 `uv sync --extra cad`）后重试。"
)


def _parser_meta_trimesh() -> dict:
    try:
        import trimesh
        return {"name": "trimesh", "version": getattr(trimesh, "__version__", None)}
    except Exception:
        return {"name": "trimesh", "version": None}


def _parser_meta_ocp() -> dict:
    import importlib.metadata as md
    for pkg in ("cadquery-ocp-novtk", "cadquery-ocp"):
        try:
            return {"name": pkg, "version": md.version(pkg)}
        except Exception:
            continue
    return {"name": "cadquery-ocp", "version": None}


def _aborted_doc(fname: str, fmt: str, ex):
    from ..cad.base import CADDocument, EngineeringEntity

    doc = CADDocument(fname, fmt, metadata={"parser": {}, **ex.as_meta()})
    doc.warnings.append(ex.reason)
    doc.add(EngineeringEntity(
        entity_uid="document", entity_type="document", name=fname, parent_uid=None,
        source_format=fmt,
        provenance={"aborted": True, "abort_reason": ex.reason,
                    "observed": ex.observed, "limit": ex.limit},
        confidence="unknown",
    ))
    return doc


def load_bundle(path) -> ConnectorResult:
    from .. import store
    from ..cad import normalize, summarize
    from ..cad.limits import CADLimitExceeded, load_limits

    path = Path(path)
    ext = path.suffix.lower()
    fmt = "stl" if ext == ".stl" else "step"
    limits = load_limits()
    sha = store.file_sha256(path)

    try:
        if ext == ".stl":
            from ..cad import mesh_backend  # trimesh 惰性导入在其内部
            doc = mesh_backend.load_stl(path, limits, _parser_meta_trimesh())
        elif ext in (".step", ".stp"):
            try:
                from ..cad import step_backend  # 模块顶层 import OCP → 缺失在此抛
            except ImportError as e:
                raise ImportError(_STEP_HINT) from e
            doc = step_backend.load_step(path, limits, _parser_meta_ocp())
        else:
            raise ValueError(f"CAD connector 不支持的扩展名 {ext}")
    except CADLimitExceeded as ex:
        doc = _aborted_doc(path.name, fmt, ex)

    rows = normalize.to_rows(doc, sha)
    pages = summarize.to_pages(doc)
    smeta = {"units": doc.units, "warnings": doc.warnings, **doc.metadata}
    return ConnectorResult(pages=pages, engineering_entities=rows, source_metadata=smeta)


def load(path) -> list[Page]:
    """兼容纯 load 调用；pipeline 优先走 load_bundle（同时落工程实体）。"""
    return load_bundle(path).pages
