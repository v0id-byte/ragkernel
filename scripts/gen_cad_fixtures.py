"""程序化生成 CAD 测试夹具（合法、可复现、无来源不明的商业 CAD 文件）。

产物提交进 tests/fixtures/cad/：这样阅读端测试不依赖写入端正确性、无 cad extra 也能收集。
需要 cad extra（trimesh + cadquery-ocp-novtk）：`uv run --extra cad python scripts/gen_cad_fixtures.py`
"""

from __future__ import annotations

import os
import sys

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "cad")
OUT = os.path.abspath(OUT)


def _stl_fixtures():
    import numpy as np
    import trimesh

    # 1) 10×20×30 封闭长方体（binary + ascii）
    box = trimesh.creation.box(extents=[10.0, 20.0, 30.0])
    box.export(os.path.join(OUT, "box_10x20x30.stl"), file_type="stl")
    box.export(os.path.join(OUT, "box_10x20x30_ascii.stl"), file_type="stl_ascii")

    # 2) 非封闭：删一个三角面
    openbox = trimesh.creation.box(extents=[10.0, 20.0, 30.0])
    keep = np.ones(len(openbox.faces), dtype=bool)
    keep[0] = False
    openbox.update_faces(keep)
    openbox.export(os.path.join(OUT, "box_open.stl"), file_type="stl")

    # 3) 两个独立连通组件
    a = trimesh.creation.box(extents=[10, 10, 10])
    b = trimesh.creation.box(extents=[10, 10, 10])
    b.apply_translation([50, 0, 0])
    two = trimesh.util.concatenate([a, b])
    two.export(os.path.join(OUT, "two_bodies.stl"), file_type="stl")

    # 4) watertight 但绕组不一致（is_volume=False）：只翻转部分相邻面的顶点序。
    #    翻转全部面只会让法向朝内、仍一致；需翻转子集制造不一致。逐策略验证目标组合。
    bad = _make_bad_winding(trimesh, np)
    bad.export(os.path.join(OUT, "bad_winding.stl"), file_type="stl")

    return {
        "box_10x20x30.stl": "binary watertight box",
        "box_10x20x30_ascii.stl": "ascii watertight box",
        "box_open.stl": "non-watertight (face removed)",
        "two_bodies.stl": "2 connected components",
        "bad_winding.stl": "watertight but winding-inconsistent (is_volume=False)",
    }


def _make_bad_winding(trimesh, np):
    """构造 watertight=True, is_winding_consistent=False, is_volume=False 的网格。"""
    for subset in (
        [0, 1],              # 翻一个面的两个三角
        [0, 2, 4],
        [0, 1, 2, 3],
        list(range(0, 12, 2)),
    ):
        m = trimesh.creation.box(extents=[10.0, 20.0, 30.0])
        faces = m.faces.copy()
        for i in subset:
            faces[i] = faces[i][::-1]
        m = trimesh.Trimesh(vertices=m.vertices.copy(), faces=faces, process=False)
        # round-trip 过 STL 再判定（导出即测试所见）
        blob = m.export(file_type="stl")
        rt = trimesh.load_mesh(trimesh.util.wrap_as_stream(blob), file_type="stl")
        if rt.is_watertight and (not rt.is_winding_consistent) and (not rt.is_volume):
            return m
    raise RuntimeError("无法构造目标 bad-winding 网格；请调整翻转子集策略")


def _step_fixtures():
    import math

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.Interface import Interface_Static
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_StepModelType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

    def new_doc():
        d = TDocStd_Document(TCollection_ExtendedString("XCAF"))
        XCAFApp_Application.GetApplication_s().InitDocument(d)
        return d, XCAFDoc_DocumentTool.ShapeTool_s(d.Main()), XCAFDoc_DocumentTool.ColorTool_s(d.Main())

    def write(doc, path, unit="MM"):
        Interface_Static.SetCVal_s("write.step.unit", unit)
        w = STEPCAFControl_Writer()
        w.SetNameMode(True)
        w.SetColorMode(True)
        w.Transfer(doc)
        w.Write(path)

    # 1) 单一命名长方体
    doc, st, ct = new_doc()
    lab = st.AddShape(BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape(), False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("Box"))
    write(doc, os.path.join(OUT, "box.step"))

    # 1b) 同名 "Box" 但不同尺寸（5×5×5）——用于「两模型同名零件」冲突/实体接地测试
    doc, st, ct = new_doc()
    lab = st.AddShape(BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape(), False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("Box"))
    write(doc, os.path.join(OUT, "box_variant.step"))

    # 2) 带同轴通孔的圆柱（cut）
    outer = BRepPrimAPI_MakeCylinder(10.0, 40.0).Shape()
    inner = BRepPrimAPI_MakeCylinder(4.0, 40.0).Shape()
    holed = BRepAlgoAPI_Cut(outer, inner).Shape()
    doc, st, ct = new_doc()
    lab = st.AddShape(holed, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("HollowCylinder"))
    write(doc, os.path.join(OUT, "cylinder_hole.step"))

    # 3) 命名+着色的两零件装配，SmallBlock 实例化两次（prototype/occurrence）
    doc, st, ct = new_doc()
    pa = st.AddShape(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), False)
    pb = st.AddShape(BRepPrimAPI_MakeBox(40.0, 20.0, 10.0).Shape(), False)
    TDataStd_Name.Set_s(pa, TCollection_ExtendedString("SmallBlock"))
    TDataStd_Name.Set_s(pb, TCollection_ExtendedString("BigPlate"))
    ct.SetColor(pa, Quantity_Color(1.0, 0.0, 0.0, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    ct.SetColor(pb, Quantity_Color(0.0, 0.4, 1.0, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    asm = st.NewShape()
    TDataStd_Name.Set_s(asm, TCollection_ExtendedString("Gearbox"))
    t2 = gp_Trsf()
    t2.SetTranslation(gp_Vec(50, 0, 0))
    st.AddComponent(asm, pa, TopLoc_Location(gp_Trsf()))
    st.AddComponent(asm, pa, TopLoc_Location(t2))
    st.AddComponent(asm, pb, TopLoc_Location(gp_Trsf()))
    st.UpdateAssemblies()
    write(doc, os.path.join(OUT, "assembly_named_colored.step"))

    # 3b) 嵌套装配 + 旋转（非交换变换）：Top → SubAsm → Pin，Top→SubAsm 放置含 90° 绕 Z + 平移。
    #     用于验证嵌套 occurrence 世界包围盒与 location_matrix 同序、以及子装配体积并入父级。
    doc, st, ct = new_doc()
    pin = st.AddShape(BRepPrimAPI_MakeBox(4.0, 4.0, 20.0).Shape(), False)
    TDataStd_Name.Set_s(pin, TCollection_ExtendedString("Pin"))
    sub = st.NewShape()
    TDataStd_Name.Set_s(sub, TCollection_ExtendedString("SubAsm"))
    st.AddComponent(sub, pin, TopLoc_Location(gp_Trsf()))  # Pin 在 SubAsm 原点
    top = st.NewShape()
    TDataStd_Name.Set_s(top, TCollection_ExtendedString("Top"))
    rot = gp_Trsf()
    rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.pi / 2)  # 90° 绕 Z
    trans = gp_Trsf()
    trans.SetTranslation(gp_Vec(100, 0, 0))
    top_place = trans.Multiplied(rot)  # 平移 * 旋转（不可交换）
    st.AddComponent(top, sub, TopLoc_Location(top_place))
    st.UpdateAssemblies()
    write(doc, os.path.join(OUT, "nested_rotated_assembly.step"))

    # 3c) 多个自由根零件（无装配树）：验证根级也受实体数/时长上限约束（不绕过 _check）。
    doc, st, ct = new_doc()
    for nm, dims in (("BodyA", (10., 10., 10.)), ("BodyB", (20., 5., 5.)), ("BodyC", (3., 3., 3.))):
        lab = st.AddShape(BRepPrimAPI_MakeBox(*dims).Shape(), False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(nm))
    write(doc, os.path.join(OUT, "multi_body.step"))

    # 4) 英寸单位的 1 英寸立方体：OCCT 内部单位是 mm，故建 25.4mm 立方体、以 INCH 写出
    #    → 文件声明 1.0 inch；读回目标 mm → 25.4mm（真正考验单位转换，而非 1mm 立方体）
    doc, st, ct = new_doc()
    lab = st.AddShape(BRepPrimAPI_MakeBox(25.4, 25.4, 25.4).Shape(), False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("InchCube"))
    write(doc, os.path.join(OUT, "inch_box.step"), unit="INCH")

    # 5) 畸形文件（graceful failure）——手写截断 stub，无需 OCP
    with open(os.path.join(OUT, "malformed.step"), "w") as f:
        f.write("ISO-10303-21;\nHEADER;\nGARBAGE NOT A VALID STEP FILE\n")

    return {
        "box.step": "single named box part (mm)",
        "box_variant.step": "part also named 'Box' but 5x5x5 (same-name conflict fixture)",
        "cylinder_hole.step": "cylinder with coaxial through-hole (cylindrical faces)",
        "assembly_named_colored.step": "named+colored 2-part assembly, SmallBlock x2 (prototype/occurrence)",
        "nested_rotated_assembly.step": "Top->SubAsm->Pin nested assembly with 90deg-Z rotation (non-commuting transform)",
        "multi_body.step": "three free root parts, no assembly (root-limit test)",
        "inch_box.step": "1-inch cube, declared unit INCH (reads as 25.4mm)",
        "malformed.step": "truncated ISO-10303-21 stub for graceful-failure test",
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = {}
    try:
        manifest.update(_stl_fixtures())
    except ImportError:
        print("trimesh 未安装：跳过 STL 夹具（需 cad extra）", file=sys.stderr)
    try:
        manifest.update(_step_fixtures())
    except ImportError:
        print("OCP 未安装：跳过 STEP 夹具（需 cad extra）", file=sys.stderr)
    for name, desc in sorted(manifest.items()):
        size = os.path.getsize(os.path.join(OUT, name))
        print(f"  {name:34s} {size:7d}B  {desc}")
    print(f"\n生成 {len(manifest)} 个夹具到 {OUT}")


if __name__ == "__main__":
    main()
