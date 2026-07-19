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


# ── Codex 评审修复的回归测试 ──────────────────────────────────────

def test_nested_assembly_transform_and_volume(ingest, entities):
    """嵌套 + 旋转装配：world bbox 与 location_matrix 同序一致（转换乘序修复），
    且子装配体积并入父级 instance_summed_volume（嵌套漏算修复）。"""
    import itertools
    doc_id = ingest("nested_rotated_assembly.step")["document_id"]
    rows = [parse_json_fields(e) for e in entities(doc_id)]
    top = next(e for e in rows if e["entity_type"] == "assembly" and e["parent_uid"] == "document")
    pin_proto = next(e for e in rows if e["entity_type"] == "part")
    pin_occ = next(e for e in rows if e["entity_type"] == "component_instance")
    # 嵌套子装配体积并入父级（Pin = 4*4*20 = 320）
    assert abs(top["geometry"]["instance_summed_volume"]["value"] - 320.0) < 1e-2
    # world bbox = location_matrix 作用于本地 bbox（含 90° 旋转的非交换变换也一致）
    M = pin_occ["geometry"]["location_matrix"]
    lb = pin_proto["properties"]["local_bounding_box"]["value"]
    corners = itertools.product([lb[0], lb[3]], [lb[1], lb[4]], [lb[2], lb[5]])
    tc = [[M[i][0] * x + M[i][1] * y + M[i][2] * z + M[i][3] for i in range(3)] for x, y, z in corners]
    exp = [min(c[k] for c in tc) for k in range(3)] + [max(c[k] for c in tc) for k in range(3)]
    wb = pin_occ["geometry"]["world_bounding_box"]
    assert all(abs(a - b) < 1e-3 for a, b in zip(exp, wb))


def test_multi_body_normal(ingest, entities):
    doc_id = ingest("multi_body.step")["document_id"]
    parts = [e for e in entities(doc_id) if e["entity_type"] == "part"]
    assert len(parts) == 3


def test_multi_body_root_limit_enforced(ingest, monkeypatch):
    """多自由根零件也须受实体上限约束（根循环不绕过 _check）。"""
    from ragkernel.cad import limits as L
    monkeypatch.setattr(L, "load_limits", lambda: {**L.DEFAULTS, "max_entities": 1})
    res = ingest("multi_body.step")
    assert res.get("rejected") is True


def test_step_component_count(ingest, toolbox):
    """component_count 对 STEP 返回 组件实例数/原型数（非只给 STL 键的 null）。"""
    doc_id = ingest("assembly_named_colored.step")["document_id"]
    out = json.loads(toolbox.query_geometry(doc_id, "document", ["component_count"]))
    cc = out["results"]["component_count"]
    assert cc.get("component_instance_count") == 3
    assert cc.get("part_prototype_count") == 2


def test_assembly_tree_chunk_uid_dereferenceable(ingest, cad_db, toolbox):
    """装配树片的 cad_entity_uid 指向真实实体，get_cad_entity 可解引用（非合成 assembly-tree）。"""
    res = ingest("assembly_named_colored.step")
    rows = cad_db.execute("SELECT meta_json FROM chunks WHERE document_id=? AND title LIKE '%装配树%'",
                          (res["document_id"],)).fetchall()
    assert rows
    uid = json.loads(rows[0]["meta_json"])["cad_entity_uid"]
    assert uid != "assembly-tree"
    out = json.loads(toolbox.get_cad_entity(res["document_id"], uid))
    assert "error" not in out and out["entity_type"] == "assembly"
