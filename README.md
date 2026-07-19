# ragkernel

本地优先的**企业 RAG 内核**：混合检索（BM25 + 向量 + RRF + 重排）+ 带引用的抗幻觉问答 + 傻瓜式自动索引。
首个垂直层做**设备维修 / 售后故障知识库**（拆片段 + 分类 + 标来源 + 工单/反馈回填闭环）。

- **全本地检索**：`bge-m3` 向量 + `bge-reranker-v2-m3` 重排 + `sqlite-vec`，语料不出机。
- **provider 无关 / 可私有化**：`kind: anthropic`（Claude / MiniMax）或 `kind: openai`（vLLM / Ollama / Xinference 本地模型，如 Qwen3-32B）。
- **傻瓜式索引**：拖文件进网页、丢进监听文件夹、或 CLI 批量——自动分片 + 分类 + 向量化。
- **元素级拆片（维修手册 / 数据手册友好）**：按元素类型拆——**故障码 / 针脚 / 参数 / 备件表一行一片**（表头前置、行内自足）、拆装工序整片不拆、键值逐条、**工程尺寸标注**（Ø8 / M4 / 45° / 12±0.1）独立成片；每片带 `章节路径 / 故障码 / 针脚 / 连接器 / 型号 / 尺寸类型` 元数据与型号·章节上下文前缀，故障码/料号/针脚等标识符保留整词供精确检索；可用 `search_by_field` 按字段过滤。
- **引用优先、抗幻觉**：答案只依据检索到的文档，每条事实标 `[D<文档>#<块> p.<页> · 分类]`；查不到就直说。
- **可插拔垂直层**：`verticals/` 换一个模块（正则 + 关键词表 + prompt）即换行业，内核不动。
- **工单/反馈闭环**：导入 CSV/Excel 工单；答完「记录处理结果」→ 新故障案例入库、立即可检索，KB 越用越准。
- **原生 CAD 摄取（STEP / STL，可选 `ragkernel[cad]`）**：读装配树、零件名称/颜色、**精确 BREP 体积/表面积/包围盒**、面类型直方图；STL 读网格有效性（watertight / 有效体积）与几何度量。每个数值带 `来源方法 / 表示 / 质量 / 有效性`——**精确几何 vs 网格近似 vs 文件声明** 绝不混淆。见下文「原生 CAD 摄取」。
- **统计面板**：文件 / 片段 / 索引 / 提问 / 来源构成 / 分类分布 / 摄取历史，均有记录。

> 单租户 MVP。多租户/RBAC、多模态拍照报障属后续。

> **能力范围（诚实）**：精细解析**技术 PDF 里的文本、表格、针脚定义、电气图与机械尺寸图的文字标注、扫描内容**，产真实页码引用；**并原生读取 STEP/STL 的可验证几何**（装配树、精确 BREP 体积/包围盒、网格有效性）。**不读** DWG / SLDPRT / Parasolid 原生几何、不重建参数化特征树、不做完整 GD&T 与孔特征识别（圆柱曲面 ≠ 确认孔）。技术图纸做到「乱码/空 → 可读文本+表格+尺寸标注+页码」，不做「机器看懂几何」。

## 快速开始（本地开发，mac 走 MPS 加速）

```bash
cd ragkernel
cp .env.example .env          # 默认走 MiniMax，填 MINIMAX_API_KEY（或改 settings.yaml 切 Claude/本地）
uv sync
uv run ragkernel models       # 首次下载本地嵌入/重排模型（~2GB，仅一次）
uv run ragkernel serve        # 打开 http://127.0.0.1:8360
```

网页里拖手册/工单进去 → 自动索引 → 提问 → 得到带引用（含分类）的答案 → 「记录处理结果」回填。

## CLI

```bash
ragkernel ingest --path ./docs      # 摄取文件或整个目录（PDF/DOCX/MD/TXT/CSV/XLSX + STEP/STL，幂等）
ragkernel embed                     # 补齐缺失向量
ragkernel ask "主轴报E-42怎么处理"    # 命令行问答
ragkernel watch --dir ./inbox       # 监听落盘文件夹，自动索引
ragkernel stats                     # 知识库统计
ragkernel serve                     # 启动 Web
```

## LLM provider（`config/settings.yaml` 的 `provider`）

| 场景 | kind | base_url | model | key |
|---|---|---|---|---|
| MiniMax（默认，零成本） | anthropic | `https://api.minimaxi.com/anthropic` | MiniMax-M3 | MINIMAX_API_KEY |
| 官方 Claude | anthropic | 留空 | claude-sonnet-5 | ANTHROPIC_API_KEY |
| 本地私有化 | openai | `http://localhost:8000/v1` | Qwen3-32B-AWQ | 任意非空 |

**私有化本地部署**：本地引擎几乎都是 OpenAI 兼容，用 `kind: openai`。可靠多步 tool-use 的甜点是 **32B**（Qwen3-32B AWQ/INT4，单张 ~48G 卡 L40S/A6000），预算档 14B/24G（RTX 4090）。嵌入+重排本来就全本地。serving 推荐 vLLM。

## Docker 一键起

```bash
export MINIMAX_API_KEY=...      # 或 ANTHROPIC_API_KEY
docker compose -f docker/docker-compose.yml up
```

首启下载模型到 `models` 卷，之后秒起；数据持久化到 `./_data`。

## 原生 CAD 摄取（STEP / STL）

把 CAD/3D 模型里**可验证的结构化工程信息**变成统一的工程实体与可检索片段——用户或 Agent 可问：整体尺寸、包围盒/体积/表面积、装配含几个零件、有哪些实体/面/圆柱面、STL 是否封闭、某零件名称/颜色/材料、某尺寸是精确还是近似、答案来自哪个文件/装配节点/几何实体。

**安装**（重二进制依赖，作可选 extra、缺失不影响内核启动）：

```bash
uv sync --extra cad          # 或 pip install 'ragkernel[cad]'
ragkernel ingest --path model.step   # .step/.stp/.stl 自动路由，无需额外命令
```

**实际支持与提取字段**

| 格式 | 读取路径 | 提取 |
|---|---|---|
| **STEP / STP** | OpenCASCADE **XDE/XCAF** | 装配树、零件/装配名称、原型 vs 实例（component_instance + 世界变换）、颜色/图层、solids/shells/faces/edges/vertices 计数、**精确 BREP 体积 + 表面积**、**AddOptimal 精确包围盒**（本地 / 世界）、面类型直方图（plane/cylinder/cone/sphere/torus/bspline…）、**圆柱曲面计数**、源单位 vs 计算单位 |
| **STL**（ASCII/二进制） | trimesh | 顶点/三角面数、bounding box/extents、表面积、质心、**watertight**、**有效体积判定（`is_volume`，绕组一致）**、**顶点连通体 + 面连通组件两种计数**、编码类型；单位恒 **unknown** |

**精确 vs 近似 vs 声明（每个数值都带来源与有效性，绝不混淆）**

- `brep_computed`（representation=`brep`）—— STEP BREP 精确几何（体积/面积/包围盒；包围盒记 `algorithm=AddOptimal, use_triangulation=false, tight=true`）。
- `mesh_computed`（representation=`mesh`）—— STL 网格积分近似（体积仅在 `is_volume=true` 时给值，否则 `null` + `validity=invalid`；**非封闭/绕组错误的网格绝不给看似精确的可靠体积**）。
- `file_declared` / `file_record_count` —— 文件显式声明（名称/颜色/源单位）或记录读数（三角面数）。
- **单位诚实**：STEP 记录 `源单位 (FileUnits)` 与 `计算单位 mm`（1 inch 文件 → 读回 25.4mm 且标注已转换）；**STL 单位恒 `unknown`，绝不默认 mm**。

**结构化工具（Agent / MCP，返回结构化 JSON，均以稳定 `entity_uid` 寻址）**：`inspect_cad_document` · `list_cad_entities` · `get_cad_entity` · `get_assembly_tree` · `query_geometry`（属性白名单）· `compare_cad_entities` · `search_engineering_objects`。

**明确不做（本 MVP）**：DWG / SLDPRT / SLDASM / Parasolid 原生读取（坚持转换器路线：先导出 STEP/DXF，不伪造原生支持）、参数化特征树 / 草图约束、完整 GD&T/PMI、**孔特征识别**（只报圆柱曲面数，`hole_detection.supported=false`）、OBB、设计意图、由几何推断制造工艺。IGES/OBJ/PLY/DXF 已留接口，属后续。

**依赖与平台**：`trimesh`（MIT，仅需 numpy）；`cadquery-ocp-novtk`（OCP bindings Apache-2.0 + 内含 OpenCASCADE LGPL-2.1；无头/离线友好，去 VTK）——pip 轮子覆盖 macOS arm64/x86_64、Linux x86_64/aarch64、Windows，Python 3.10–3.14，无需 conda/编译。平台若无 novtk 轮子可回退 `cadquery-ocp`（含 VTK）。

> **Results assist document and model retrieval and must be verified in the source CAD system before manufacturing or safety-critical use.** 本功能用于文档与模型检索，制造/安全关键用途前须在原始 CAD 系统中复核。

## 验证

```bash
uv run python scripts/smoke_test.py          # 端到端：摄取→嵌入→检索→（有 key 时）带引用问答
uv run python scripts/eval_retrieval.py      # Recall@k / MRR，rerank on/off 对比
# 原生 CAD（需 cad extra）
uv run --extra cad python scripts/gen_cad_fixtures.py   # 程序化生成 CAD 测试夹具（提交进 tests/fixtures/cad/）
uv run --extra cad --extra dev pytest tests/            # CAD 单元 + 检索 QA + 结构化值检查（无 extra 则干净跳过）
uv run --extra cad python scripts/cad_benchmark.py      # CAD 摄取性能：冷启/耗时/峰值内存/无索引爆炸
```

## 结构

```
ragkernel/
  chunking.py embed.py rerank.py search.py store.py   # 检索内核（全本地）
  backends.py                                          # Anthropic / OpenAI 兼容生成后端
  connectors/  cad.py                                  # PDF/DOCX/MD/TXT/CSV/XLSX + STEP/STL → 统一管线
  cad/  step_backend.py mesh_backend.py normalize.py   # 原生 CAD：OCP/XDE + trimesh → 工程实体 + 检索片（可选 extra）
  pipeline.py                                          # 摄取编排 + 自动嵌入 + 反馈回填 + CAD 原子写入(load_bundle)
  tools.py agent.py                                    # 带引用、可按分类过滤、含 CAD 结构化工具的 agentic 问答
  verticals/  equipment.py                             # 可插拔垂直层（设备维修；CAD 工具在核心 Toolbox，不占垂直层）
  webapp.py static/index.html                          # 上传 + 带引用聊天 + 记录处理结果 + 仪表盘
config/settings.yaml                                   # provider / 检索 / 上传 / 垂直层 配置
```

## 致谢 / Acknowledgments

- **[Docling](https://github.com/docling-project/docling)**（IBM，MIT License）+ **[RapidOCR](https://github.com/RapidAI/RapidOCR)**（Apache-2.0，PP-OCRv6 中文 ONNX）—— PDF 版面/表格结构分析 + OCR，修中文图纸/扫描件识别度、产真实页码引用。
- **[MarkItDown](https://github.com/microsoft/markitdown)**（Microsoft，MIT License）—— Word / PPT / HTML → Markdown 的文档转换（PDF 已交给 Docling）。
- **[BAAI bge-m3 / bge-reranker-v2-m3](https://huggingface.co/BAAI)** —— 本地向量与重排模型。
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** —— SQLite 向量检索扩展。
- **[jieba](https://github.com/fxsjy/jieba)** —— 中文分词。
- **[trimesh](https://github.com/mikedh/trimesh)**（MIT License）—— STL 网格几何与有效性判定（可选 `ragkernel[cad]`）。
- **[Open CASCADE Technology](https://dev.opencascade.org/) via [OCP](https://github.com/CadQuery/OCP)**（OCP bindings Apache-2.0 + OCCT LGPL-2.1）—— STEP 装配/BREP 精确几何解析（可选 `ragkernel[cad]`）。
