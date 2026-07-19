"""STEP 后端（OpenCASCADE / OCP 的 XDE / XCAF 路径）。

诚实要点：
- **单位**：读文件声明单位（base.FileUnits，三序列版）+ 显式把转换目标单位设为 mm（Interface_Static，
  全局态 → 加锁），记录 source vs calculation；确认不了就 unknown，绝不写未经核实的 mm。
- **prototype vs occurrence**：一个零件被实例化 N 次 → 一个 part 原型（本地度量、按原型 memoize）
  + N 个 component_instance（世界变换 + world bbox）。装配总体积明确标为「按实例求和」。
- **bbox** 用 BRepBndLib.AddOptimal（不用三角化、不用 shape tolerance），provenance 记全参数与 tight 标志；
  仍是 BREP 度量（representation=brep），绝不写成 mesh。
- **圆柱面 ≠ 孔**：只报 cylindrical_face_count 与曲面类型直方图；hole_detection.supported=false。
- **GD&T/材料** 缺失是常态 → 不虚构（materials 多为空、GD&T 本 MVP 不解析）。

模块顶层 import OCP：仅由 connectors/cad 在 load_bundle 内**惰性** `from . import step_backend`，
故 OCP 缺失时不影响内核启动（connector 捕获 ImportError 给安装提示）。
"""

from __future__ import annotations

import threading

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TColStd import TColStd_SequenceOfAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (
    XCAFDoc_ColorCurv,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_DocumentTool,
)

from .base import CADDocument, EngineeringEntity, mv
from .limits import CADLimitExceeded

# Interface_Static 是全局、非线程安全态 → 读 STEP 的临界区加锁（STEP 摄取重且稀，串行可接受）。
_STEP_LOCK = threading.Lock()

_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "revolution",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "extrusion",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "offset",
    GeomAbs_SurfaceType.GeomAbs_OtherSurface: "other",
}

_COUNT_ENUMS = [
    ("solids", TopAbs_ShapeEnum.TopAbs_SOLID),
    ("shells", TopAbs_ShapeEnum.TopAbs_SHELL),
    ("faces", TopAbs_ShapeEnum.TopAbs_FACE),
    ("edges", TopAbs_ShapeEnum.TopAbs_EDGE),
    ("vertices", TopAbs_ShapeEnum.TopAbs_VERTEX),
]


# ── 低层几何度量（对一个 TopoDS_Shape）─────────────────────────────

def _counts(shape) -> dict:
    out = {}
    for key, enum in _COUNT_ENUMS:
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, enum, m)  # 去重计数（Explorer 会重复计共享边/点）
        out[key] = m.Extent()
    return out


def _bbox(shape) -> list | None:
    bb = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bb, False, False)  # use_triangulation=False, use_shape_tolerance=False
    if bb.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
    return [xmin, ymin, zmin, xmax, ymax, zmax]


def _extents(box: list | None) -> list | None:
    if not box:
        return None
    return [box[3] - box[0], box[4] - box[1], box[5] - box[2]]


def _volume_area(shape) -> tuple[float, float]:
    vp = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vp)
    sp = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, sp)
    return vp.Mass(), sp.Mass()


def _face_histogram(shape) -> dict:
    hist: dict[str, int] = {}
    exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        t = BRepAdaptor_Surface(face).GetType()
        key = _SURFACE_NAMES.get(t, "other")
        hist[key] = hist.get(key, 0) + 1
        exp.Next()
    return hist


def _matrix(trsf) -> list:
    return [[trsf.Value(i, j) for j in range(1, 5)] for i in range(1, 4)]


# ── XCAF 标签工具 ─────────────────────────────

def _entry(label) -> str:
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def _name_of(label) -> str | None:
    nm = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), nm):
        return TCollection_AsciiString(nm.Get()).ToCString()
    return None


def _color_of(shape_tool, color_tool, label):
    try:
        shp = shape_tool.GetShape_s(label)
    except Exception:
        return None
    col = Quantity_Color()
    for t in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
        if color_tool.GetColor(shp, t, col):
            return [round(col.Red(), 4), round(col.Green(), 4), round(col.Blue(), 4)]
    return None


# ── 单位块 ─────────────────────────────

def _read_units(base_reader) -> dict:
    try:
        ln = TColStd_SequenceOfAsciiString()
        an = TColStd_SequenceOfAsciiString()
        so = TColStd_SequenceOfAsciiString()
        base_reader.FileUnits(ln, an, so)
        src = [ln.Value(i).ToCString() for i in range(1, ln.Length() + 1)]
        scale = float(base_reader.SystemLengthUnit())
        block = {
            "source_length_units": src,
            "system_length_unit_scale": scale,
            "calculation_length_unit": "mm",
            "calculation_unit_source": "configured_transfer_unit",
            "unit_conversion_applied": bool(src and src[0].strip().lower() not in ("millimetre", "millimeter", "mm")),
        }
        if len({u.strip().lower() for u in src}) > 1:
            block["warning"] = "multiple_length_units"
        return block
    except Exception:
        return {
            "source_length_units": [],
            "calculation_length_unit": None,
            "warning": "unable_to_confirm_transfer_length_unit",
        }


# ── 本地度量 → 一个 part 原型实体 ─────────────────────────────

def _local_part_metrics(shape, uid: str) -> tuple[dict, dict]:
    counts = _counts(shape)
    box = _bbox(shape)
    vol, area = _volume_area(shape)
    hist = _face_histogram(shape)
    valid_solid = vol is not None and vol > 0
    props = {
        "local_bounding_box": mv(box, "mm", "brep_computed", "brep",
                                 quality="high", source_entity=uid,
                                 algorithm="BRepBndLib.AddOptimal",
                                 use_triangulation=False, use_shape_tolerance=False, tight=True),
        "extents": mv(_extents(box), "mm", "brep_computed", "brep", quality="high", source_entity=uid),
        "volume": mv(vol if valid_solid else None, "mm3", "brep_computed", "brep",
                     quality="high" if valid_solid else "low",
                     validity="valid" if valid_solid else "invalid", source_entity=uid,
                     **({} if valid_solid else {"warning": "non_positive_brep_volume"})),
        "surface_area": mv(area, "mm2", "brep_computed", "brep", quality="high", source_entity=uid),
    }
    cyl = hist.get("cylinder", 0)
    geom = {
        **counts,
        "face_type_histogram": hist,
        "cylindrical_face_count": cyl,
        "hole_count": None,
        "hole_detection": {"supported": False,
                           "reason": "MVP does not perform topology-based hole recognition"},
    }
    return props, geom


class _Walker:
    """一次 STEP 的装配遍历：part 原型按 proto_uid memoize；occurrence 逐个产 component_instance。"""

    def __init__(self, doc: CADDocument, shape_tool, color_tool, limits: dict, t_start: float):
        import time
        self._time = time
        self.doc = doc
        self.st = shape_tool
        self.ct = color_tool
        self.limits = limits
        self.t_start = t_start
        self.seen_protos: dict[str, EngineeringEntity] = {}

    def _check(self, depth: int):
        if depth > int(self.limits["max_assembly_depth"]):
            raise CADLimitExceeded("assembly_depth_exceeded", depth, int(self.limits["max_assembly_depth"]))
        if len(self.doc.entities) > int(self.limits["max_entities"]):
            raise CADLimitExceeded("entity_limit_exceeded", len(self.doc.entities), int(self.limits["max_entities"]))
        if self._time.monotonic() - self.t_start > float(self.limits["max_seconds"]):
            raise CADLimitExceeded("time_limit_exceeded", None, float(self.limits["max_seconds"]))

    def ensure_part(self, label, proto_uid: str) -> EngineeringEntity:
        if proto_uid in self.seen_protos:
            return self.seen_protos[proto_uid]
        shape = self.st.GetShape_s(label)
        props, geom = _local_part_metrics(shape, proto_uid)
        color = _color_of(self.st, self.ct, label)
        if color:
            geom["color_rgb"] = color
        ent = EngineeringEntity(
            entity_uid=proto_uid, entity_type="part", name=_name_of(label),
            parent_uid=None, geometry_frame="local", source_format="step",
            properties=props, geometry=geom, confidence="brep_computed",
        )
        self.seen_protos[proto_uid] = ent
        self.doc.add(ent)
        return ent

    def emit_assembly(self, label, uid: str, parent_uid, depth: int, world_trsf, apath) -> EngineeringEntity:
        self._check(depth)
        asm = EngineeringEntity(
            entity_uid=uid, entity_type="assembly", name=_name_of(label),
            parent_uid=parent_uid, geometry_frame="world", assembly_path=tuple(apath),
            source_format="step", confidence="brep_computed",
        )
        self.doc.add(asm)
        comps = TDF_LabelSequence()
        self.st.GetComponents_s(label, comps)
        world_boxes: list = []
        summed_vol = 0.0
        have_vol = False
        for j in range(1, comps.Length() + 1):
            self._check(depth)
            comp = comps.Value(j)
            ref = TDF_Label()
            self.st.GetReferredShape_s(comp, ref)
            comp_shape = self.st.GetShape_s(comp)  # 已带父级内的放置
            comp_world_trsf = world_trsf.Multiplied(comp_shape.Location().Transformation())
            occ_uid = f"{uid}#{_entry(comp).split(':')[-1]}"
            if self.st.IsAssembly_s(ref):
                sub = self.emit_assembly(ref, occ_uid, uid, depth + 1, comp_world_trsf,
                                         apath + [_name_of(ref) or _entry(ref)])
                wbox = sub.geometry.get("overall_world_bounding_box")
                if wbox:
                    world_boxes.append(wbox)
            else:
                proto_uid = _entry(ref)
                proto = self.ensure_part(ref, proto_uid)
                world_shape = comp_shape.Moved(TopLoc_Location(world_trsf))
                wbox = _bbox(world_shape)
                inst = EngineeringEntity(
                    entity_uid=occ_uid, entity_type="component_instance", name=proto.name,
                    parent_uid=uid, prototype_uid=proto_uid, occurrence_uid=occ_uid,
                    location_matrix=_matrix(comp_world_trsf), geometry_frame="world",
                    source_format="step",
                    geometry={"world_bounding_box": wbox, "prototype_uid": proto_uid},
                    confidence="brep_computed",
                )
                self.doc.add(inst)
                if wbox:
                    world_boxes.append(wbox)
                pv = proto.properties.get("volume", {})
                if pv.get("value") is not None:
                    summed_vol += pv["value"]
                    have_vol = True
        asm.geometry["overall_world_bounding_box"] = _union_boxes(world_boxes)
        if have_vol:
            asm.geometry["instance_summed_volume"] = mv(
                summed_vol, "mm3", "brep_computed", "brep", quality="medium",
                validity="valid", source_entity=uid,
                warning="sum_over_occurrences_of_prototype_volume")
        return asm


def _union_boxes(boxes: list) -> list | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    zs0 = min(b[2] for b in boxes)
    xs1 = max(b[3] for b in boxes)
    ys1 = max(b[4] for b in boxes)
    zs1 = max(b[5] for b in boxes)
    return [xs0, ys0, zs0, xs1, ys1, zs1]


def _failed_doc(fname: str, parser_meta: dict, reason: str) -> CADDocument:
    doc = CADDocument(fname, "step", metadata={"parser": parser_meta, **{"aborted": True, "abort_reason": reason}})
    doc.warnings.append(reason)
    doc.add(EngineeringEntity(
        entity_uid="document", entity_type="document", name=fname, parent_uid=None,
        source_format="step", provenance={"aborted": True, "abort_reason": reason},
        confidence="unknown",
    ))
    return doc


def load_step(path, limits: dict, parser_meta: dict) -> CADDocument:
    import os
    import time

    path = str(path)
    fname = os.path.basename(path)
    size = os.path.getsize(path)
    max_bytes = int(limits["max_file_mb"]) * 1024 * 1024
    if size > max_bytes:
        raise CADLimitExceeded("file_size_exceeded", size, max_bytes)

    t_start = time.monotonic()
    doc = CADDocument(fname, "step", metadata={"parser": parser_meta})

    with _STEP_LOCK:  # Interface_Static + reader 用全局态，串行化
        Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        reader.SetLayerMode(True)
        reader.SetPropsMode(True)
        status = reader.ReadFile(path)
        if status != IFSelect_ReturnStatus.IFSelect_RetDone:
            return _failed_doc(fname, parser_meta, "step_read_failed")
        doc.units = _read_units(reader.Reader())
        rdoc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
        XCAFApp_Application.GetApplication_s().InitDocument(rdoc)
        try:
            ok = reader.Transfer(rdoc)
        except Exception:
            return _failed_doc(fname, parser_meta, "step_transfer_failed")
        if not ok:
            return _failed_doc(fname, parser_meta, "step_transfer_failed")
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(rdoc.Main())
        color_tool = XCAFDoc_DocumentTool.ColorTool_s(rdoc.Main())

        roots = TDF_LabelSequence()
        shape_tool.GetFreeShapes(roots)
        if roots.Length() == 0:
            doc.warnings.append("no_shapes_in_step")

        walker = _Walker(doc, shape_tool, color_tool, limits, t_start)
        from OCP.gp import gp_Trsf

        root_boxes: list = []
        try:
            for i in range(1, roots.Length() + 1):
                root = roots.Value(i)
                ruid = _entry(root)
                if shape_tool.IsAssembly_s(root):
                    asm = walker.emit_assembly(root, ruid, "document", 0, gp_Trsf(),
                                               [_name_of(root) or ruid])
                    if asm.geometry.get("overall_world_bounding_box"):
                        root_boxes.append(asm.geometry["overall_world_bounding_box"])
                else:  # 根部就是个简单零件（无装配）
                    part = walker.ensure_part(root, ruid)
                    part.parent_uid = "document"
                    lb = part.properties.get("local_bounding_box", {}).get("value")
                    if lb:
                        root_boxes.append(lb)
        except CADLimitExceeded:
            raise  # 交给 connector 转 aborted 文档
    # 锁外：组装 document 实体
    protos = [e for e in doc.entities if e.entity_type == "part"]
    asms = [e for e in doc.entities if e.entity_type == "assembly"]
    insts = [e for e in doc.entities if e.entity_type == "component_instance"]
    doc_ent = EngineeringEntity(
        entity_uid="document", entity_type="document", name=fname, parent_uid=None,
        geometry_frame="world", source_format="step",
        properties={"overall_bounding_box": mv(_union_boxes(root_boxes), "mm", "brep_computed",
                                                "brep", quality="high", source_entity="document")},
        geometry={
            "part_prototype_count": len(protos),
            "assembly_count": len(asms),
            "component_instance_count": len(insts),
            "unit_block": doc.units,
        },
        confidence="brep_computed",
    )
    doc.entities.insert(0, doc_ent)
    doc.metadata.update({
        "entity_count": len(doc.entities),
        "part_prototype_count": len(protos),
        "assembly_count": len(asms),
        "component_instance_count": len(insts),
    })
    return doc
