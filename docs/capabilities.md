# 能力范围

> RagKernel 当前支持的工程知识处理能力、保证边界与不做的事。

状态：MVP / 持续演进中

[← Back to documentation](README.md)

## 文档摄取

统一的摄取接口把每种工程数据源路由到对应的结构化后端。Web UI 拖拽、监听文件夹或 CLI 三条路都走同一条管线——幂等、自动索引。

| 格式 | 支持状态 | 后端 / 说明 |
|---|---|---|
| PDF | 支持 | Docling + RapidOCR，版面/表格/OCR，真实页码 |
| DOCX / PPTX / HTML | 支持 | MarkItDown |
| Markdown / TXT | 支持 | |
| CSV / XLSX | 支持 | 工单 |
| STEP / STP | 支持 | OpenCASCADE XDE，可选 `[cad]` extra，见 [cad.md](cad.md) |
| STL | 支持 | trimesh，单位恒 unknown |
| DWG / SLDPRT / SLDASM / Parasolid | 当前不支持 | 无原生读取；坚持转换器路线（先导出 STEP/DXF），不伪造原生支持 |
| IGES / OBJ / PLY / DXF | 当前不支持 | 接口已留，属后续 |

原生 CAD 只是目前支持的一类工程数据源，**不是产品身份**。

## 知识表示（核心）

工程结构在源文件与解析器暴露的范围内被保留，而不是压平成无差别文本。

- **元素感知的文档切片** —— 故障码 / 针脚 / 规格 / BOM 表按一行一片切分（表头前置、片段自足），操作步骤保持完整，工程尺寸（Ø8 / M4 / 45°）原样保留；每片携带 `section-path / fault_code / pin / connector / model / dimension_type` 元数据。
- **结构化工程实体** —— CAD 装配、零件、实体与几何作为一等实体，用稳定的 `entity_uid` 寻址。
- **原型 / 实例装配（prototype / occurrence）** —— 装配树保留 prototype-vs-instance 与世界变换（OpenCASCADE XDE）。
- **混合索引** —— 每个单元同时建立词法（BM25）与稠密（向量）索引。
- **知识复利** —— 已解决的工单/反馈回填为可检索案例，知识库越用越准。

## 检索

| 能力 | 说明 |
|---|---|
| 混合检索 | BM25 + 向量，RRF 融合 |
| 重排 | 本地 cross-encoder（`bge-reranker-v2-m3`）对融合候选重排 |
| 元数据过滤 | `search_by_field` 按故障码 / 针脚 / 连接器 / 型号 / 尺寸类型精确查 |
| 可追溯引用 | 检索结果带文档与片段标识；页码在源格式与解析器提供时给出 |

**页码的诚实说明**：非分页格式不携带页码；跨页长表的行**当前可能统一引用表格起始页**。语料里没有的，就说没有。

## 验证与保证

RagKernel 的区别不只在于检索到什么，更在于它拒绝编造什么。

> **先检索证据，再生成答案。**
> **计算出的工程数值必须暴露来源与有效性。**
> **精确几何与近似几何绝不混为一谈。**
> **不知道就是不知道。**

- **可追溯证据** —— 检索结果携带稳定的文档与片段标识，格式与解析器支持时附源页码。
- **显式 provenance** —— CAD 测量区分 `brep_computed` / `mesh_computed` / `file_declared` 三类来源。
- **有效性感知的几何** —— 无效或非体积网格返回明确的 invalid 状态，而不是一个误导性的体积值。
- **诚实边界** —— 孔特征识别、DWG/Parasolid 原生解析、完整 GD&T 等未支持能力如实报告为不支持，绝不推断。

`verify_engineering_claim` 对给定 claim 在限定证据范围内做核验，返回 **supported / contradicted / unsupported** 并附真实页码引用——这是**基于证据的 claim 核验**，用途是让 Agent 不至于按错误的针脚能力或规格去写代码。

## Agent 接口

- **MCP Server** —— 只读检索暴露给 Agent（stdio + HTTP，token 鉴权，分档限流）。
- **结构化 JSON 工具** —— `search` / `read` / `list`，外加 7 个 CAD 工具（`inspect_cad_document`、`list_cad_entities`、`get_cad_entity`、`get_assembly_tree`、`query_geometry`、`compare_cad_entities`、`search_engineering_objects`）。实体级操作用稳定 `entity_uid`，不用内部数据库行 ID。
- **三个入口** —— CLI、Web、MCP；人与 AI Agent 共享同一份知识。

## 运维（部署与诊断）

从源码检出到一个可运行、可信任的部署，属于工程设计的一部分——`documents → structured knowledge → retrieval + agents → deployment + diagnostics`。

- **可复现部署** —— 一行 bootstrap 装好运行环境与依赖，再交棒给引导式初始化；完全非交互、CI/容器友好。见 [installation.md](installation.md)。
- **引导式初始化**（`ragkernel setup`）—— provider 配置、初始管理员、可选本地模型，以及 MCP token 等集成。密钥不走 argv；并发运行有文件锁；`--yes` 在 CI 下缺凭证直接失败，而不是半配置地继续。见 [configuration.md](configuration.md)。
- **自诊断**（`ragkernel doctor`）—— 分层健康检查（运行时 · 存储 · provider 配置→网络→鉴权 · 模型缓存），**只读**，带 JSON 输出供自动化、监控与就绪探针使用。鉴权校验优先选零成本端点，尽可能避免产生计费请求。见 [diagnostics.md](diagnostics.md)。

## 当前不做 / 已知边界

精细解析**技术 PDF 里的文本、表格、针脚定义、电气图与机械尺寸图的文字标注、扫描内容**，产真实页码引用；**并原生读取 STEP/STL 的可验证几何**（装配树、精确 BREP 体积/包围盒、网格有效性）。**不读** DWG / SLDPRT / Parasolid 原生几何、不重建参数化特征树、不做完整 GD&T 与孔特征识别（圆柱曲面 ≠ 确认孔）。技术图纸做到「乱码/空 → 可读文本+表格+尺寸标注+页码」，不做「机器看懂几何」。**PDF 页码引用以解析器提供的元素 provenance 为准；当前跨页长表的行可能统一引用表格起始页，区域级 bbox 引用与 Docling Item → Block 直通尚未实现。** 单租户 MVP；多租户/RBAC 属后续。
