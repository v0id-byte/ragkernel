"""STEP 后端结构化断言：装配树/名称/计数/精确 bbox+体积+面积/颜色/单位/prototype-occurrence/graceful fail。"""

import json

import pytest

pytest.importorskip("OCP")

from conftest import parse_json_fields  # noqa: E402


def _by_type(entities_fn, doc_id):
    rows = [parse_json_fields(e) for e in entities_fn(doc_id)]
    out = {}
    for r in rows:
        out.setdefault(r["entity_type"], []).append(r)
    return out


def test_single_box_exact_metrics(ingest, entities):
    doc_id = ingest("box.step")["document_id"]
    parts = _by_type(entities, doc_id)["part"]
    assert len(parts) == 1
    p = parts[0]
    assert p["name"] == "Box"
    vol = p["properties"]["volume"]
    assert abs(vol["value"] - 6000.0) < 1e-3     # 10*20*30
    assert vol["source_method"] == "brep_computed"
    assert vol["unit"] == "mm3"
    assert vol["validity"] == "valid"
    ext = sorted(round(v) for v in p["properties"]["extents"]["value"])
    assert ext == [10, 20, 30]
    assert p["geometry"]["solids"] == 1
    assert p["geometry"]["faces"] == 6


def test_bbox_provenance_records_algorithm(ingest, entities):
    doc_id = ingest("box.step")["document_id"]
    p = _by_type(entities, doc_id)["part"][0]
    bb = p["properties"]["local_bounding_box"]
    assert bb["algorithm"] == "BRepBndLib.AddOptimal"
    assert bb["use_triangulation"] is False
    assert bb["use_shape_tolerance"] is False
    assert bb["tight"] is True
    assert bb["representation"] == "brep"     # 绝不是 mesh


def test_assembly_tree_and_names(ingest, entities):
    doc_id = ingest("assembly_named_colored.step")["document_id"]
    t = _by_type(entities, doc_id)
    assert len(t["assembly"]) == 1
    assert t["assembly"][0]["name"] == "Gearbox"
    part_names = sorted(p["name"] for p in t["part"])
    assert part_names == ["BigPlate", "SmallBlock"]
    assert len(t["component_instance"]) == 3


def test_prototype_occurrence(ingest, entities):
    """SmallBlock 实例化两次：一个 prototype、两个 occurrence、世界包围盒不同。"""
    doc_id = ingest("assembly_named_colored.step")["document_id"]
    t = _by_type(entities, doc_id)
    small = next(p for p in t["part"] if p["name"] == "SmallBlock")
    occ = [i for i in t["component_instance"] if i["prototype_uid"] == small["entity_uid"]]
    assert len(occ) == 2
    boxes = [tuple(i["geometry"]["world_bounding_box"]) for i in occ]
    assert boxes[0] != boxes[1]                # 两实例世界位置不同
    # 每个 occurrence 有 location_matrix（世界变换）
    assert all(i["geometry"]["location_matrix"] for i in occ)
    # 装配总体积按实例求和（明确标注）
    asm = t["assembly"][0]
    summed = asm["geometry"]["instance_summed_volume"]
    assert "sum_over_occurrences" in summed["warning"]


def test_colors_roundtrip(ingest, entities):
    doc_id = ingest("assembly_named_colored.step")["document_id"]
    t = _by_type(entities, doc_id)
    small = next(p for p in t["part"] if p["name"] == "SmallBlock")
    rgb = small["geometry"]["color_rgb"]
    assert round(rgb[0]) == 1 and round(rgb[1]) == 0 and round(rgb[2]) == 0  # 红


def test_cylindrical_faces_not_holes(ingest, entities):
    """圆柱曲面数 ≠ 确认孔数：报 cylindrical_face_count，但 hole_detection.supported=False。"""
    doc_id = ingest("cylinder_hole.step")["document_id"]
    p = _by_type(entities, doc_id)["part"][0]
    assert p["geometry"]["cylindrical_face_count"] >= 2
    assert p["geometry"]["hole_count"] is None
    assert p["geometry"]["hole_detection"]["supported"] is False


def test_inch_units_converted(ingest, entities, cad_db):
    doc_id = ingest("inch_box.step")["document_id"]
    p = _by_type(entities, doc_id)["part"][0]
    ext = [round(v, 1) for v in p["properties"]["extents"]["value"]]
    assert ext == [25.4, 25.4, 25.4]           # 1 inch → 25.4 mm
    doc_ent = _by_type(entities, doc_id)["document"][0]
    ub = doc_ent["geometry"]["unit_block"]
    assert ub["source_length_units"] == ["INCH"]
    assert ub["calculation_length_unit"] == "mm"
    assert ub["unit_conversion_applied"] is True


def test_graceful_malformed(ingest, cad_db):
    res = ingest("malformed.step")
    assert res.get("rejected") is True
    status = cad_db.execute("SELECT status FROM documents WHERE id=?",
                            (res["document_id"],)).fetchone()[0]
    assert status == "rejected"
    # 保留一个 aborted document 记录，而非静默无痕
    from ragkernel import store
    ents = store.get_engineering_entities(cad_db, res["document_id"])
    doc = next(e for e in ents if e["entity_type"] == "document")
    assert json.loads(doc["provenance_json"])["aborted"] is True


def test_no_per_face_or_occurrence_chunk_explosion(ingest, cad_db):
    """装配（2 原型 + 3 实例 + 多面）只应产 document+tree+每原型片，不按面/实例爆炸。"""
    res = ingest("assembly_named_colored.step")
    n = cad_db.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?",
                       (res["document_id"],)).fetchone()[0]
    assert n == 4  # document + assembly-tree + SmallBlock + BigPlate


def test_provenance_has_no_absolute_path(ingest, cad_db):
    res = ingest("box.step")
    ents = cad_db.execute("SELECT provenance_json FROM engineering_entities WHERE document_id=?",
                          (res["document_id"],)).fetchall()
    for (pj,) in ents:
        prov = json.loads(pj)
        assert "source_path" not in prov
        assert "/" not in (prov.get("source_filename") or "")   # 只有文件名，无路径分隔
        assert prov["source_filename"] == "box.step"


def test_entity_uid_addressing(ingest, toolbox, entities):
    """公开工具以稳定 entity_uid 寻址（非内部自增 id）。"""
    doc_id = ingest("box.step")["document_id"]
    part = _by_type(entities, doc_id)["part"][0]
    out = json.loads(toolbox.get_cad_entity(doc_id, part["entity_uid"]))
    assert out["entity_uid"] == part["entity_uid"]
    assert out["name"] == "Box"
