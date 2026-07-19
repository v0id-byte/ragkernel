"""STL 后端结构化断言：extents/area/volume(is_volume 门槛)/watertight/组件/单位 unknown/无索引爆炸。"""

import json

import pytest

pytest.importorskip("trimesh")

from ragkernel.cad import mesh_backend  # noqa: E402
from conftest import parse_json_fields  # noqa: E402


def _mesh_entity(entities_fn, doc_id):
    rows = [parse_json_fields(e) for e in entities_fn(doc_id)]
    return next(e for e in rows if e["entity_type"] == "mesh")


def test_watertight_box_geometry(ingest, entities):
    doc_id = ingest("box_10x20x30.stl")["document_id"]
    m = _mesh_entity(entities, doc_id)
    g, p = m["geometry"], m["properties"]
    assert g["is_watertight"] is True
    assert g["is_volume"] is True
    assert g["triangle_count"] == 12
    assert g["vertex_connected_body_count"] == 1
    # extents 10×20×30（顺序无关）
    ext = sorted(round(v) for v in p["extents"]["value"])
    assert ext == [10, 20, 30]
    # 体积 6000，标为网格计算、单位未知（不是 BREP 精确）
    vol = p["volume"]
    assert abs(vol["value"] - 6000.0) < 1e-6
    assert vol["source_method"] == "mesh_computed"
    assert vol["unit"] is None
    assert vol["validity"] == "valid"


def test_units_unknown(fx):
    """STL 单位必须 unknown，绝不默认 mm。"""
    from ragkernel import connectors
    res = connectors.loader_for("x.stl").load_bundle(fx("box_10x20x30.stl"))
    assert res.source_metadata["units"]["unit"] is None
    assert "does not reliably encode" in res.source_metadata["units"]["warning"]


def test_ascii_binary_detection(fx):
    assert mesh_backend.stl_encoding(fx("box_10x20x30.stl")) == "binary"
    assert mesh_backend.stl_encoding(fx("box_10x20x30_ascii.stl")) == "ascii"


def test_non_watertight_no_reliable_volume(ingest, entities):
    doc_id = ingest("box_open.stl")["document_id"]
    m = _mesh_entity(entities, doc_id)
    assert m["geometry"]["is_watertight"] is False
    assert m["geometry"]["is_volume"] is False
    # 非有效体积：volume 必须 None + invalid，绝不给看似精确的可靠值
    assert m["properties"]["volume"]["value"] is None
    assert m["properties"]["volume"]["validity"] == "invalid"


def test_two_connected_components(ingest, entities):
    doc_id = ingest("two_bodies.stl")["document_id"]
    m = _mesh_entity(entities, doc_id)
    assert m["geometry"]["vertex_connected_body_count"] == 2
    assert m["geometry"]["face_connected_component_count"] == 2
    # 组件实体也应产出（≥2）
    bodies = [e for e in entities(doc_id) if e["entity_type"] == "body"]
    assert len(bodies) == 2


def test_watertight_but_bad_winding(ingest, entities):
    """封闭但绕组不一致 → is_volume=False，可靠体积应为 None（watertight ≠ 有效体积）。"""
    doc_id = ingest("bad_winding.stl")["document_id"]
    m = _mesh_entity(entities, doc_id)
    assert m["geometry"]["is_watertight"] is True
    assert m["geometry"]["is_winding_consistent"] is False
    assert m["geometry"]["is_volume"] is False
    assert m["properties"]["volume"]["value"] is None


def test_no_per_triangle_chunk_explosion(ingest, cad_db):
    """12 三角面的盒子绝不能产生 ~12 个 chunk（不得每三角面一片/一嵌入）。"""
    res = ingest("box_10x20x30.stl")
    n_chunks = cad_db.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?",
                              (res["document_id"],)).fetchone()[0]
    assert res["chunks"] == n_chunks
    assert n_chunks <= 3  # document + mesh（本例无多组件）
