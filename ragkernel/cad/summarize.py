"""CADDocument → list[Page]：每个实体摘要一片，自足、可读、带溯源 meta。

防索引爆炸：只对 document / assembly-tree / part / mesh / body(组件) 出片，
**不为 component_instance / face / edge / triangle 出片**（实例在装配树片里汇总）。
Page.meta 带 cad_entity_type / cad_entity_uid / cad_format / category（可过滤 + 干净引用尾）。
"""

from __future__ import annotations

from ..connectors.base import Page
from .base import CADDocument, EngineeringEntity


def _fmt_num(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.4g}" if x else "0"
    return str(x)


def _fmt_box(box) -> str:
    """接受 flat-6 [xmin..zmax]（STEP）、2x3 [[min],[max]]（STL bounds）或 3-el extents。"""
    if not box:
        return "—"
    try:
        if isinstance(box[0], (list, tuple)):       # 2x3
            ext = [box[1][i] - box[0][i] for i in range(3)]
        elif len(box) == 6:                          # flat min/max
            ext = [box[3] - box[0], box[4] - box[1], box[5] - box[2]]
        elif len(box) == 3:                          # already extents
            ext = box
        else:
            return str(box)
    except Exception:
        return str(box)
    return " × ".join(_fmt_num(v) for v in ext)


def _mv_str(m: dict | None, unit_hint: str = "") -> str:
    if not m:
        return "—"
    v = m.get("value")
    if v is None:
        return f"无效（{m.get('warning', m.get('validity', 'invalid'))}）"
    unit = m.get("unit")
    us = f" {unit}" if unit else ""
    return f"{_fmt_num(v)}{us}"


def _category(entity_type: str) -> str:
    return {
        "document": "CAD模型",
        "assembly": "CAD装配",
        "part": "CAD零件",
        "mesh": "CAD网格",
        "body": "CAD网格组件",
    }.get(entity_type, "CAD对象")


def _page(doc: CADDocument, entity_type: str, entity_uid: str, title: str, text: str,
          extra_meta: dict | None = None) -> Page:
    meta = {
        "element_type": "cad",
        "cad_format": doc.format,
        "cad_entity_type": entity_type,
        "cad_entity_uid": entity_uid,
        "category": _category(entity_type),
    }
    if extra_meta:
        meta.update(extra_meta)
    return Page(text=text.strip(), page_no=None, title=title, meta=meta)


# ── STEP ─────────────────────────────

def _step_part_page(doc, e: EngineeringEntity, occ_count: int) -> Page:
    p = e.properties
    g = e.geometry
    hist = g.get("face_type_histogram", {})
    hist_str = "，".join(f"{k} {v}" for k, v in hist.items()) or "—"
    apath = " / ".join(e.assembly_path) if e.assembly_path else (e.name or e.entity_uid)
    lines = [
        f"零件 {e.name or e.entity_uid}",
        f"装配路径：{apath}",
        f"包围盒(本地)：{_fmt_box(p.get('local_bounding_box', {}).get('value'))} mm",
        f"精确体积：{_mv_str(p.get('volume'))}（BREP 精确计算）",
        f"表面积：{_mv_str(p.get('surface_area'))}",
        f"实体/面/边/顶点：{g.get('solids','—')}/{g.get('faces','—')}/{g.get('edges','—')}/{g.get('vertices','—')}",
        f"面类型：{hist_str}",
        f"圆柱曲面：{g.get('cylindrical_face_count', 0)} 个"
        f"（注意：圆柱曲面数 ≠ 确认孔数；本 MVP 不做孔特征识别）",
    ]
    if g.get("color_rgb"):
        lines.append(f"颜色 RGB：{g['color_rgb']}")
    lines.append(f"来源实体：{e.entity_uid}")
    lines.append("计算方式：OpenCASCADE BREP 精确几何")
    if occ_count:
        lines.append(f"本原型在装配中共有 {occ_count} 个实例，各实例世界包围盒见 get_assembly_tree / get_cad_entity。")
    title = f"【STEP · 零件 {e.name or e.entity_uid}】"
    return _step_page_wrap(doc, e, title, "\n".join(lines))


def _step_page_wrap(doc, e, title, text):
    return _page(doc, e.entity_type, e.entity_uid, title, text)


def _step_document_page(doc: CADDocument) -> Page:
    doc_ent = next((e for e in doc.entities if e.entity_type == "document"), None)
    u = doc.units or {}
    g = (doc_ent.geometry if doc_ent else {}) or {}
    src = "，".join(u.get("source_length_units") or []) or "未知"
    lines = [
        f"CAD 文档 {doc.filename}",
        "格式：STEP",
        f"源单位：{src}；计算单位：{u.get('calculation_length_unit') or '未知'}"
        f"（已转换：{'是' if u.get('unit_conversion_applied') else '否'}）",
        f"零件原型：{g.get('part_prototype_count', 0)}；装配节点：{g.get('assembly_count', 0)}；"
        f"组件实例：{g.get('component_instance_count', 0)}",
    ]
    if doc_ent:
        ob = doc_ent.properties.get("overall_bounding_box", {}).get("value")
        lines.append(f"总体包围盒：{_fmt_box(ob)} mm")
    if doc.warnings:
        lines.append("警告：" + "；".join(doc.warnings))
    return _page(doc, "document", "document", f"【STEP · 文档 {doc.filename}】", "\n".join(lines))


def _assembly_tree_page(doc: CADDocument) -> Page | None:
    asms = [e for e in doc.entities if e.entity_type == "assembly"]
    if not asms:
        return None
    by_parent: dict = {}
    for e in doc.entities:
        by_parent.setdefault(e.parent_uid, []).append(e)
    lines = ["装配树："]

    def walk(uid, depth):
        for child in by_parent.get(uid, []):
            if child.entity_type == "assembly":
                lines.append("  " * depth + f"▸ 装配 {child.name or child.entity_uid}")
                walk(child.entity_uid, depth + 1)
            elif child.entity_type == "component_instance":
                wb = child.geometry.get("world_bounding_box")
                lines.append("  " * depth + f"• 实例 {child.name or ''} → 原型 {child.prototype_uid}"
                             + (f"，世界包围盒 {_fmt_box(wb)} mm" if wb else ""))
                walk(child.entity_uid, depth + 1)

    walk("document", 0)
    # cad_entity_uid 指向真实可解引用的根装配实体（而非合成 "assembly-tree"），
    # 使 search_engineering_objects 命中后 get_cad_entity(document_id, entity_uid) 可用。
    root_asm = next((e for e in asms if e.parent_uid == "document"), asms[0])
    return _page(doc, "assembly", root_asm.entity_uid, f"【STEP · 装配树 {doc.filename}】",
                 "\n".join(lines), extra_meta={"cad_entity_type": "assembly"})


# ── STL ─────────────────────────────

def _stl_mesh_page(doc: CADDocument, e: EngineeringEntity) -> Page:
    p = e.properties
    g = e.geometry
    vol = p.get("volume", {})
    vol_line = ("体积：无效（网格未构成有效体积，不作可靠值）"
                if vol.get("value") is None
                else f"体积：{_fmt_num(vol.get('value'))}（网格积分近似；单位未知）")
    fcc = g.get("face_connected_component_count")
    lines = [
        f"网格 {e.name or e.entity_uid}",
        f"包围盒：{_fmt_box(p.get('bounding_box', {}).get('value') or p.get('extents', {}).get('value'))}"
        f"（单位未知——STL 不可靠编码物理单位）",
        f"表面积：{_fmt_num(p.get('surface_area', {}).get('value'))}（网格近似）",
        vol_line,
        f"是否封闭(watertight)：{'是' if g.get('is_watertight') else '否'}",
        f"绕组一致：{'是' if g.get('is_winding_consistent') else '否'}",
        f"构成有效体积(is_volume)：{'是' if g.get('is_volume') else '否'}",
        f"顶点(文件/拓扑)：{g.get('vertex_count_file','—')} / {g.get('vertex_count_topological','—')}",
        f"三角面：{g.get('triangle_count','—')}",
        f"连通组件：顶点连通体 {g.get('vertex_connected_body_count','—')} 个；"
        f"面连通组件 {fcc if fcc is not None else '（超阈值跳过）'} 个",
        "单位：未知（STL 不携带单位）",
        f"来源实体：{e.entity_uid}",
    ]
    title = f"【STL · 网格 {e.name or e.entity_uid}】"
    return _page(doc, e.entity_type, e.entity_uid, title, "\n".join(lines))


def _stl_component_page(doc: CADDocument, e: EngineeringEntity) -> Page:
    p = e.properties
    g = e.geometry
    vol = p.get("volume", {})
    vol_line = ("体积：无效" if vol.get("value") is None
                else f"体积：{_fmt_num(vol.get('value'))}（网格近似；单位未知）")
    lines = [
        f"网格组件 {e.name or e.entity_uid}",
        f"包围盒：{_fmt_box(p.get('bounding_box', {}).get('value') or p.get('extents', {}).get('value'))}（单位未知）",
        f"表面积：{_fmt_num(p.get('surface_area', {}).get('value'))}（网格近似）",
        vol_line,
        f"是否封闭：{'是' if g.get('is_watertight') else '否'}",
        f"三角面：{g.get('triangle_count','—')}",
        f"来源实体：{e.entity_uid}",
    ]
    return _page(doc, e.entity_type, e.entity_uid, f"【STL · 组件 {e.name or e.entity_uid}】", "\n".join(lines))


def _stl_document_page(doc: CADDocument) -> Page:
    doc_ent = next((e for e in doc.entities if e.entity_type == "document"), None)
    g = (doc_ent.geometry if doc_ent else {}) or {}
    ext = (doc_ent.properties.get("extents", {}).get("value") if doc_ent else None)
    fcc = g.get("face_connected_component_count")
    lines = [
        f"CAD 文档 {doc.filename}",
        "格式：STL",
        f"整体尺寸(extents)：{' × '.join(_fmt_num(v) for v in ext) if ext else '—'}（单位未知——STL 不携带单位）",
        f"三角面：{g.get('triangle_count','—')}",
        f"是否封闭：{'是' if g.get('is_watertight') else '否'}；构成有效体积：{'是' if g.get('is_volume') else '否'}",
        f"连通组件：顶点连通体 {g.get('vertex_connected_body_count','—')} 个；"
        f"面连通组件 {fcc if fcc is not None else '（跳过）'} 个",
        f"编码：{g.get('encoding','—')}",
    ]
    if doc.warnings:
        lines.append("警告：" + "；".join(doc.warnings))
    return _page(doc, "document", "document", f"【STL · 文档 {doc.filename}】", "\n".join(lines))


# ── 中止/失败 ─────────────────────────────

def _aborted_page(doc: CADDocument) -> Page:
    reason = doc.metadata.get("abort_reason", "unknown")
    obs = doc.metadata.get("observed")
    lim = doc.metadata.get("limit")
    detail = f"（观测 {obs} / 上限 {lim}）" if obs is not None else ""
    text = (f"该 CAD 文件未完成摄取：{reason}{detail}。"
            "已保留本说明记录但未做几何提取——请调整文件或系统上限后重试。")
    return _page(doc, "document", "document", f"【CAD · 未完成摄取 {doc.filename}】", text,
                 extra_meta={"category": "摄取失败"})


# ── 入口 ─────────────────────────────

def to_pages(doc: CADDocument) -> list[Page]:
    if doc.metadata.get("aborted"):
        return [_aborted_page(doc)]

    pages: list[Page] = []
    if doc.format == "stl":
        pages.append(_stl_document_page(doc))
        for e in doc.entities:
            if e.entity_type == "mesh":
                pages.append(_stl_mesh_page(doc, e))
            elif e.entity_type == "body":
                pages.append(_stl_component_page(doc, e))
        return pages

    # STEP
    pages.append(_step_document_page(doc))
    tree = _assembly_tree_page(doc)
    if tree:
        pages.append(tree)
    # 每原型的实例数（用于零件片提示）
    occ_by_proto: dict = {}
    for e in doc.entities:
        if e.entity_type == "component_instance" and e.prototype_uid:
            occ_by_proto[e.prototype_uid] = occ_by_proto.get(e.prototype_uid, 0) + 1
    for e in doc.entities:
        if e.entity_type == "part":
            pages.append(_step_part_page(doc, e, occ_by_proto.get(e.entity_uid, 0)))
    return pages
