"""STL 后端（trimesh）。

诚实要点：
- **内存有序 dual-load**：先 process=False 拿文件忠实计数（并在此检查三角面上限，早于昂贵的合并），
  释放后再 process=True 拿拓扑（watertight/volume/组件）——控峰值内存。
- **体积门槛用 is_volume（绕组一致）而非仅 is_watertight**；无效则 volume=None、validity=invalid。
- **两种组件计数**：vertex_connected_body_count（body_count，廉价）与 face_connected_component_count
  （split，成本高、超阈值跳过），不把 body_count 笼统当"连通组件数"。
- **单位恒 unknown**：STL 不可靠编码物理单位，绝不默认 mm。
"""

from __future__ import annotations

import gc
import os
import struct

from .base import CADDocument, EngineeringEntity, mv

_INSTALL_HINT = (
    "STL 摄取需要 trimesh：安装可选依赖 `pip install 'ragkernel[cad]'`"
    "（或 `uv sync --extra cad`）后重试。"
)


def _require_trimesh():
    try:
        import trimesh
        return trimesh
    except ImportError as e:  # 惰性导入：缺失只在真正摄取 CAD 时报清晰提示，不阻断内核启动
        raise ImportError(_INSTALL_HINT) from e


def stl_encoding(path) -> str:
    """ASCII vs binary：'solid' 魔数 **加** 84+50*n 尺寸公式双重判定
    （某些 binary 写入器也在 80 字节头里写 'solid'，单看关键字会误判）。"""
    with open(path, "rb") as f:
        head = f.read(84)
    size = os.path.getsize(path)
    if head[:5].lower() == b"solid":
        if len(head) >= 84:
            n = struct.unpack("<I", head[80:84])[0]
            if size == 84 + n * 50:
                return "binary"
        return "ascii"
    return "binary"


def _units_unknown() -> dict:
    return {
        "unit": None,
        "source_method": "unknown",
        "warning": "STL does not reliably encode physical units",
    }


def _mesh_props(mesh, uid: str, is_vol: bool) -> dict:
    """一个网格（整体或组件）的 MeasuredValue 属性集（长度单位恒 None）。"""
    props = {
        "extents": mv([float(x) for x in mesh.extents], None, "mesh_computed", "mesh",
                      quality="high", source_entity=uid),
        "bounding_box": mv([[float(x) for x in row] for row in mesh.bounds.tolist()], None,
                           "mesh_computed", "mesh", quality="high", source_entity=uid),
        "surface_area": mv(float(mesh.area), None, "mesh_computed", "mesh",
                           quality="medium", source_entity=uid),
        "centroid": mv([float(x) for x in mesh.centroid], None, "mesh_computed", "mesh",
                       quality="medium", source_entity=uid),
    }
    if is_vol:
        props["volume"] = mv(abs(float(mesh.volume)), None, "mesh_computed", "mesh",
                             quality="medium", validity="valid", source_entity=uid,
                             warning="unit_unknown_stl")
    else:
        props["volume"] = mv(None, None, "mesh_computed", "mesh",
                             quality="low", validity="invalid", source_entity=uid,
                             warning="mesh_does_not_define_valid_volume")
    return props


def load_stl(path, limits: dict, parser_meta: dict) -> CADDocument:
    from .limits import CADLimitExceeded  # 局部导入避免与 backend 惰性风格冲突

    trimesh = _require_trimesh()
    path = str(path)
    fname = os.path.basename(path)
    enc = stl_encoding(path)
    doc = CADDocument(fname, "stl", units=_units_unknown(),
                      metadata={"parser": parser_meta, "stl_encoding": enc})

    size = os.path.getsize(path)
    max_bytes = int(limits["max_file_mb"]) * 1024 * 1024
    if size > max_bytes:
        raise CADLimitExceeded("file_size_exceeded", size, max_bytes)

    # ① 快载（不合并顶点）：文件忠实的三角面/顶点数、bounds/extents。
    m_raw = trimesh.load_mesh(path, process=False)
    n_tri = int(len(m_raw.faces))
    n_vert_file = int(len(m_raw.vertices))
    if n_tri > int(limits["max_triangles"]):
        raise CADLimitExceeded("triangle_limit_exceeded", n_tri, int(limits["max_triangles"]))
    file_bounds = [[float(x) for x in row] for row in m_raw.bounds.tolist()]
    file_extents = [float(x) for x in m_raw.extents]
    del m_raw
    gc.collect()

    # ② 拓扑载入（合并共点顶点）：watertight/winding/volume/组件才有意义。
    m = trimesh.load_mesh(path)
    n_vert_topo = int(len(m.vertices))
    is_wt = bool(m.is_watertight)
    is_wc = bool(m.is_winding_consistent)
    is_vol = bool(m.is_volume)
    vbc = int(m.body_count)

    # face-connected 组件（split 会物化子网格，成本高）——超阈值跳过并如实告警。
    fcc = None
    comps = None
    if n_tri <= int(limits["face_component_max_triangles"]):
        comps = list(m.split(only_watertight=False))
        fcc = len(comps)
    else:
        doc.warnings.append("face_component_analysis_skipped_due_to_size")

    # ── 整体 mesh 实体 ──
    mesh_uid = "mesh"
    mesh_geom = {
        "is_watertight": is_wt,
        "is_winding_consistent": is_wc,
        "is_volume": is_vol,
        "triangle_count": n_tri,
        "vertex_count_file": n_vert_file,
        "vertex_count_topological": n_vert_topo,
        "vertex_connected_body_count": vbc,
        "face_connected_component_count": fcc,
        "encoding": enc,
    }
    if fcc is None:
        mesh_geom["face_connected_component_count_note"] = "skipped_due_to_size"
    if not is_vol:
        doc.warnings.append("mesh_does_not_define_valid_volume")

    mesh_ent = EngineeringEntity(
        entity_uid=mesh_uid, entity_type="mesh", name=fname, parent_uid="document",
        geometry_frame="local", source_format="stl",
        properties=_mesh_props(m, mesh_uid, is_vol), geometry=mesh_geom,
        confidence="mesh_computed",
    )

    # ── 组件实体（只在多于一个组件时；封顶，超限如实截断告警）──
    comp_ents: list[EngineeringEntity] = []
    if comps is not None and fcc and fcc > 1:
        cap = int(limits["max_stl_component_chunks"])
        for i, cm in enumerate(comps[:cap]):
            cuid = f"mesh/component/{i}"
            cvol = bool(cm.is_volume)
            comp_ents.append(EngineeringEntity(
                entity_uid=cuid, entity_type="body", name=f"component {i}", parent_uid=mesh_uid,
                geometry_frame="local", source_format="stl",
                properties=_mesh_props(cm, cuid, cvol),
                geometry={"is_watertight": bool(cm.is_watertight), "is_volume": cvol,
                          "triangle_count": int(len(cm.faces))},
                confidence="mesh_computed",
            ))
        if fcc > cap:
            doc.warnings.append(f"component_chunks_truncated:{fcc}>{cap}")

    # ── document 实体（聚合）──
    doc_geom = {
        "triangle_count": n_tri,
        "vertex_count_file": n_vert_file,
        "vertex_connected_body_count": vbc,
        "face_connected_component_count": fcc,
        "is_watertight": is_wt,
        "is_volume": is_vol,
        "encoding": enc,
    }
    doc_ent = EngineeringEntity(
        entity_uid="document", entity_type="document", name=fname, parent_uid=None,
        geometry_frame="local", source_format="stl",
        properties={
            "extents": mv(file_extents, None, "mesh_computed", "mesh", quality="high",
                          source_entity="document"),
            "bounding_box": mv(file_bounds, None, "mesh_computed", "mesh", quality="high",
                               source_entity="document"),
        },
        geometry=doc_geom,
        confidence="mesh_computed",
    )

    doc.entities = [doc_ent, mesh_ent, *comp_ents]
    doc.metadata.update({
        "entity_count": len(doc.entities),
        "triangle_count": n_tri,
        "component_count": vbc,
    })
    return doc
