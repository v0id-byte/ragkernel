"""CADDocument → engineering_entities 的 store-ready 行（*_json 已序列化）。

provenance **只含** 文件名/格式/sha/parser/warnings —— **绝不含服务端绝对路径**
（防主机目录泄漏、迁移失效、MCP 输出/embedding 暴露内部路径）。
"""

from __future__ import annotations

import json

from .base import CADDocument


def _prov_base(doc: CADDocument, content_sha256: str) -> dict:
    return {
        "source_filename": doc.filename,
        "source_format": doc.format,
        "content_sha256": content_sha256,
        "parser": doc.metadata.get("parser", {}),
    }


def to_rows(doc: CADDocument, content_sha256: str) -> list[dict]:
    base = _prov_base(doc, content_sha256)
    rows = []
    for e in doc.entities:
        prov = {**base, **(e.provenance or {})}
        if doc.warnings and "warnings" not in prov:
            prov["warnings"] = doc.warnings
        geom = dict(e.geometry or {})
        if e.location_matrix is not None:
            geom["location_matrix"] = e.location_matrix
        if e.geometry_frame:
            geom["geometry_frame"] = e.geometry_frame
        rows.append({
            "entity_uid": e.entity_uid,
            "entity_type": e.entity_type,
            "parent_uid": e.parent_uid,
            "prototype_uid": e.prototype_uid,
            "name": e.name,
            "assembly_path_json": json.dumps(list(e.assembly_path), ensure_ascii=False) if e.assembly_path else None,
            "properties_json": json.dumps(e.properties, ensure_ascii=False) if e.properties else None,
            "geometry_json": json.dumps(geom, ensure_ascii=False) if geom else None,
            "provenance_json": json.dumps(prov, ensure_ascii=False),
            "confidence": e.confidence,
        })
    return rows
