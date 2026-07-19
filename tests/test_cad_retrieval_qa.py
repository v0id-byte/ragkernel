"""CAD 检索 QA + 结构化值检查。

检索走 BM25（qvec=None、reranker=None）→ 不需要嵌入/重排模型，CI 友好、快。
既查 Recall（expect/absent 子串），也查结构化值（数值/单位/接地/精确 vs 近似/拒答/同名冲突）。
"""

import json

import pytest

pytest.importorskip("trimesh")
pytest.importorskip("OCP")

from ragkernel import search, store  # noqa: E402
from conftest import parse_json_fields  # noqa: E402


def _bm25(db, query, k=8):
    return search.hybrid_search(db, query, qvec=None, k=k, reranker=None)


def _texts(rows):
    return "\n".join(r["text"] or "" for r in rows)


@pytest.fixture
def corpus(ingest):
    """把一组 CAD 夹具灌进同一临时库（BM25 可检索）。返回 name->document_id。"""
    ids = {}
    for name in ("box.step", "box_variant.step", "assembly_named_colored.step",
                 "cylinder_hole.step", "box_10x20x30.stl", "two_bodies.stl"):
        ids[name] = ingest(name)["document_id"]
    return ids


# ── 检索 QA（Recall）──────────────────────────────────────────

def test_qa_part_dimensions(corpus, cad_db):
    rows = _bm25(cad_db, "SmallBlock 零件 包围盒 尺寸")
    assert "SmallBlock" in _texts(rows)


def test_qa_watertight(corpus, cad_db):
    rows = _bm25(cad_db, "STL 模型 是否封闭 watertight")
    assert "watertight" in _texts(rows)


def test_qa_assembly_part_count(corpus, cad_db):
    rows = _bm25(cad_db, "装配 有几个 零件原型")
    assert "零件原型：2" in _texts(rows)


def test_qa_cylindrical_faces(corpus, cad_db):
    rows = _bm25(cad_db, "有哪些 圆柱曲面")
    txt = _texts(rows)
    assert "圆柱曲面" in txt
    # 不得把圆柱面说成确认的孔
    assert "确认孔数" not in txt or "≠ 确认孔数" in txt


def test_qa_stl_units_unknown(corpus, cad_db):
    rows = _bm25(cad_db, "STL 的 单位 是什么")
    assert "单位：未知" in _texts(rows)


# ── 结构化值检查 ──────────────────────────────────────────────

def test_largest_volume_by_compare(ingest, toolbox, entities):
    doc_id = ingest("assembly_named_colored.step")["document_id"]
    parts = [parse_json_fields(e) for e in entities(doc_id) if e["entity_type"] == "part"]
    uids = [p["entity_uid"] for p in parts]
    out = json.loads(toolbox.compare_cad_entities(doc_id, uids, ["volume"]))
    vols = {c["name"]: c["values"]["volume"]["value"] for c in out["entities"]}
    assert max(vols, key=vols.get) == "BigPlate"       # 40*20*10 > 10*10*10


def test_exact_vs_approximate_same_geometry(ingest, entities):
    """同一 10×20×30 盒子：STEP=brep_computed（精确），STL=mesh_computed（近似）——方法标签必须可区分。"""
    step_id = ingest("box.step")["document_id"]
    stl_id = ingest("box_10x20x30.stl")["document_id"]
    step_part = next(parse_json_fields(e) for e in entities(step_id) if e["entity_type"] == "part")
    stl_mesh = next(parse_json_fields(e) for e in entities(stl_id) if e["entity_type"] == "mesh")
    step_vol = step_part["properties"]["volume"]
    stl_vol = stl_mesh["properties"]["volume"]
    assert abs(step_vol["value"] - stl_vol["value"]) < 1e-3   # 同几何、同体积值
    assert step_vol["source_method"] == "brep_computed"
    assert stl_vol["source_method"] == "mesh_computed"
    assert step_vol["unit"] == "mm3" and stl_vol["unit"] is None   # STEP 有单位、STL 无


def test_refusal_nonexistent_entity(ingest, toolbox):
    doc_id = ingest("box.step")["document_id"]
    out = json.loads(toolbox.query_geometry(doc_id, "no-such-uid", ["volume"]))
    assert "error" in out


def test_refusal_non_cad_document(toolbox):
    out = json.loads(toolbox.inspect_cad_document(999999))
    assert "error" in out
    assert "CAD" in out["error"]


def test_refusal_hole_count_property(ingest, toolbox, entities):
    """孔识别不支持：query_geometry 不接受 hole_count（白名单外）。"""
    doc_id = ingest("cylinder_hole.step")["document_id"]
    part = next(e for e in entities(doc_id) if e["entity_type"] == "part")
    out = json.loads(toolbox.query_geometry(doc_id, part["entity_uid"], ["hole_count"]))
    assert "error" in out


def test_same_name_conflict_grounding(ingest, entities):
    """两文档都含名为 'Box' 的零件但尺寸不同 → 按 document_id 接地，各自体积互不串。"""
    a = ingest("box.step")["document_id"]          # 10×20×30 → 6000
    b = ingest("box_variant.step")["document_id"]  # 5×5×5   → 125
    va = parse_json_fields(next(e for e in entities(a) if e["entity_type"] == "part"))
    vb = parse_json_fields(next(e for e in entities(b) if e["entity_type"] == "part"))
    assert va["name"] == vb["name"] == "Box"       # 同名
    assert abs(va["properties"]["volume"]["value"] - 6000.0) < 1e-3
    assert abs(vb["properties"]["volume"]["value"] - 125.0) < 1e-3


def test_conflict_retrieval_distinguishes_documents(ingest, cad_db):
    ingest("box.step")
    ingest("box_variant.step")
    rows = _bm25(cad_db, "Box 零件 包围盒")
    doc_ids = {r["document_id"] for r in rows if "Box" in (r["text"] or "")}
    assert len(doc_ids) >= 2      # 两个不同文档的同名零件都被检索到、可按文档区分


def test_search_engineering_objects_scoped_to_cad(ingest, cad_db, toolbox, tmp_path, monkeypatch):
    """search_engineering_objects 只应返回 CAD 片——普通文档片（entity_uid=null）不得混入。"""
    import numpy as np
    from ragkernel import embed, pipeline
    md = tmp_path / "note.md"
    md.write_text("# Box 规格\n这个 Box 重 5kg、蓝色，是普通文档不是 CAD。\n", encoding="utf-8")
    pipeline.ingest_file(str(md), db=cad_db, do_embed=False)   # 非 CAD 文档，含 "Box"
    ingest("box.step")                                          # CAD 文档，含零件 "Box"
    # 桩掉嵌入避免加载模型；无向量时走 BM25，检验 where 作用域过滤
    monkeypatch.setattr(embed, "embed", lambda texts: np.zeros((len(texts), 1024), dtype="float32"))
    out = json.loads(toolbox.search_engineering_objects("Box"))
    assert out["matches"]                                       # 有命中
    assert all(m["cad_entity_type"] is not None for m in out["matches"])   # 全为 CAD 片，无 null
