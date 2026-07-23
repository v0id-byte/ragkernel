# 原生 CAD 摄取（STEP / STL）

> 支持的格式与提取字段、精确/近似/声明三类来源、Agent 结构化工具，以及明确不做的范围。

[← Back to documentation](README.md)

把 CAD/3D 模型里**可验证的结构化工程信息**变成统一的工程实体与可检索片段——用户或 Agent 可问：整体尺寸、包围盒/体积/表面积、装配含几个零件、有哪些实体/面/圆柱面、STL 是否封闭、某零件名称/颜色/材料、某尺寸是精确还是近似、答案来自哪个文件/装配节点/几何实体。**CAD 只是 RagKernel 目前支持的一类工程数据源。**

## 安装

重二进制依赖，作可选 extra、缺失不影响内核启动：

```bash
uv sync --extra cad                        # 一并装核心 + STEP/STL 后端
uv run ragkernel ingest --path model.step  # .step/.stp/.stl 自动路由，无需额外命令
```

## 实际支持与提取字段

| 格式 | 读取路径 | 提取 |
|---|---|---|
| **STEP / STP** | OpenCASCADE **XDE/XCAF** | 装配树、零件/装配名称、原型 vs 实例（component_instance + 世界变换）、颜色/图层、solids/shells/faces/edges/vertices 计数、**精确 BREP 体积 + 表面积**、**AddOptimal 精确包围盒**（本地 / 世界）、面类型直方图（plane/cylinder/cone/sphere/torus/bspline…）、**圆柱曲面计数**、源单位 vs 计算单位 |
| **STL**（ASCII/二进制） | trimesh | 顶点/三角面数、bounding box/extents、表面积、质心、**watertight**、**有效体积判定**（`is_volume`；要求网格满足封闭性、面方向等体积条件，不只是绕组一致）、**顶点连通体 + 面连通组件两种计数**、编码类型；单位恒 **unknown** |

## 精确 vs 近似 vs 声明

每个数值都带来源与有效性，绝不混淆：

- `brep_computed`（representation=`brep`）—— STEP BREP 精确几何（体积/面积/包围盒；包围盒记 `algorithm=AddOptimal, use_triangulation=false, tight=true`）。
- `mesh_computed`（representation=`mesh`）—— STL 网格积分近似（体积仅在 `is_volume=true` 时给值，否则 `null` + `validity=invalid`；**非封闭/绕组错误的网格绝不给看似精确的可靠体积**）。
- `file_declared` / `file_record_count` —— 文件显式声明（名称/颜色/源单位）或记录读数（三角面数）。
- **单位诚实**：STEP 记录 `源单位 (FileUnits)` 与 `计算单位 mm`（1 inch 文件 → 读回 25.4mm 且标注已转换）；**STL 单位恒 `unknown`，绝不默认 mm**。

## 结构化工具（Agent / MCP，返回结构化 JSON）

`inspect_cad_document` · `list_cad_entities` · `get_cad_entity` · `get_assembly_tree` · `query_geometry`（属性白名单）· `compare_cad_entities` · `search_engineering_objects`。

**实体级操作**（`get_cad_entity` / `query_geometry` / `compare_cad_entities`）以稳定 `entity_uid` 寻址；**文档总览与装配树操作**（`inspect_cad_document` / `list_cad_entities` / `get_assembly_tree`）用 `document_id`。

## 明确不做（本 MVP）

DWG / SLDPRT / SLDASM / Parasolid 原生读取（坚持转换器路线：先导出 STEP/DXF，不伪造原生支持）、参数化特征树 / 草图约束、完整 GD&T/PMI、**孔特征识别**（只报圆柱曲面数，`hole_detection.supported=false`）、OBB、设计意图、由几何推断制造工艺。IGES/OBJ/PLY/DXF 已留接口，属后续。

## 依赖与平台

当前项目要求 **Python 3.12–3.13**（`pyproject.toml` `requires-python = ">=3.12,<3.14"`）。CAD extra 用 `trimesh`（MIT，仅需 numpy）与 `cadquery-ocp-novtk`（OCP bindings Apache-2.0 + 内含 OpenCASCADE LGPL-2.1；无头/离线友好，去 VTK）——是否有适配当前 OS/架构/Python 的预编译轮子以安装时解析结果为准；平台若无 novtk 轮子可回退 `cadquery-ocp`（含 VTK）。

> **Results assist document and model retrieval and must be verified in the source CAD system before manufacturing or safety-critical use.** 本功能用于文档与模型检索，制造/安全关键用途前须在原始 CAD 系统中复核。
