# RagKernel

### Verifiable engineering knowledge for humans and AI agents.

Turn engineering documents and CAD models into structured, searchable, and **verifiable** knowledge.

`Technical documents` · `Engineering entities` · `Hybrid retrieval` · `Claim verification` · `MCP` · `Native STEP/STL`

> RagKernel is a verifiable engineering **knowledge engine** for building evidence-grounded systems over documents, CAD models, and equipment data. Additional engineering formats are planned behind the same ingestion contract.

## Why RagKernel?

RagKernel started as a personal tool.

While developing embedded systems and designing PCBs, I repeatedly searched through hundreds of pages of datasheets, reference manuals, and technical documents. Existing RAG systems could retrieve relevant text, but they often lost engineering context and the connection between an answer and its original evidence.

I also found that general-purpose AI assistants often struggled with engineering documents. They could produce plausible answers from datasheets, but those answers were not always grounded in the actual specification or supported by clear evidence.

Traditional RAG pipelines flatten engineering documents into text chunks. RagKernel preserves engineering **structure, evidence, provenance, and geometry** — enabling humans and AI agents to retrieve engineering knowledge that is traceable and verifiable.

## Capabilities

### Ingestion
A unified ingestion interface routes each engineering source through the appropriate structured backend. Drop files in the web UI, a watched folder, or the CLI — idempotent, auto-indexed.
- **Documents** — PDF (Docling + RapidOCR layout/table/OCR, real page numbers), DOCX / PPTX / HTML (MarkItDown), Markdown / TXT, CSV / XLSX (tickets).
- **CAD models** — native **STEP / STL** (optional `[cad]` extra) — *one ingestion backend among the sources, not the identity*.

### Knowledge Representation *(the core)*
Engineering structure is preserved wherever the source and parser expose it, rather than being reduced to undifferentiated text.
- **Element-aware document chunks** — fault-code / pinout / spec / BOM tables split one-row-per-chunk (header prepended, self-contained), procedures kept whole, engineering dimensions (Ø8 / M4 / 45°) preserved, each chunk carrying `section-path / fault_code / pin / connector / model / dimension_type` metadata.
- **Structured engineering entities** — CAD assemblies, parts, solids and geometry as first-class entities addressed by a stable `entity_uid`.
- **Prototype / occurrence assemblies** — the assembly tree with prototype-vs-instance and world transforms (OpenCASCADE XDE).
- **Hybrid index** — every unit indexed for both lexical (BM25) and dense (vector) retrieval.
- **Compounding knowledge** — resolved tickets/feedback fold back in as retrievable cases; the KB gets sharper with use.

### Guarantees
What sets RagKernel apart is not only what it retrieves, but what it refuses to invent.

> **Evidence is retrieved before an answer is generated.**
> **Computed engineering values expose provenance and validity.**
> **Exact and approximate geometry are never conflated.**
> **Unknown remains unknown.**

- **Traceable evidence** — retrieval results carry stable document and chunk identifiers, plus source page numbers when the format and parser provide them.
- **Explicit provenance** — CAD measurements distinguish `brep_computed`, `mesh_computed`, and `file_declared` values.
- **Validity-aware geometry** — invalid or non-volumetric meshes return an explicit invalid state instead of a misleading volume.
- **Honest boundaries** — unsupported features such as hole recognition, native DWG/Parasolid parsing, and full GD&T are reported as unsupported rather than inferred.

### Retrieval
- **Hybrid search** — BM25 + vector fused by RRF.
- **Reranker** — local cross-encoder (`bge-reranker-v2-m3`) over the fused candidates.
- **Metadata filter** — `search_by_field` for exact lookup by fault code, pin, connector, model, dimension type…
- **Traceable citations** — retrieved evidence is tagged with document and chunk identifiers, plus source page numbers when the source format and parser provide them (non-paginated formats carry none); multi-page tables may currently inherit the table's starting page. If it isn't in the corpus, it says so.

### Agent
- **MCP Server** — read-only retrieval exposed to agents (stdio + HTTP, token auth, tiered rate limiting).
- **Structured JSON tools** — `search` / `read` / `list` plus 7 CAD tools (`inspect_cad_document`, `list_cad_entities`, `get_cad_entity`, `get_assembly_tree`, `query_geometry`, `compare_cad_entities`, `search_engineering_objects`). Entity-level operations use stable `entity_uid` values rather than internal database row IDs.
- **Claim verification** — `verify_engineering_claim` checks a claim against scoped evidence and returns *supported / contradicted / unsupported* with real page citations — so agents don't write code on a wrong pin capability or spec.
- **Three entrances** — CLI, Web, MCP; humans and AI agents over the same knowledge.

## Quick Start

```bash
cd ragkernel
cp .env.example .env          # 默认走 MiniMax，填 MINIMAX_API_KEY（或改 settings.yaml 切 Claude/本地）
uv sync                       # 文档、检索、Web 与 MCP
# 需要原生 CAD 时改用下面这条即可（一并装核心 + STEP/STL 后端，不必先跑上面那条）：
uv sync --extra cad
uv run ragkernel models       # 首次下载本地嵌入/重排模型（~2GB，仅一次）
uv run ragkernel serve        # 打开 http://127.0.0.1:8360
```

> 命令统一以 `uv run ragkernel …` 给出（在 `uv` 环境下即取即用）；若已激活虚拟环境，可省略 `uv run`。

网页里拖手册 / 工单 / CAD 进去 → 自动索引 → 提问 → 得到带引用（含分类、页码）的答案 → 「记录处理结果」回填。

## Architecture

```
        Documents   ·   CAD Models   ·   Equipment Data
              \              |              /
               +-------------+-------------+
                             |
                     RagKernel Engine
         ingest · structure · index · retrieve · verify
                             |
        +--------------------+--------------------+
        |                    |                    |
  Structured           Hybrid Search         Verifiable
   Entities           + Rerank + Filter       Citations
        |                    |                    |
        +--------------------+--------------------+
                             |
          Humans (CLI · Web)   ·   AI Agents (MCP)
```

---

## 原生 CAD 摄取（STEP / STL）

把 CAD/3D 模型里**可验证的结构化工程信息**变成统一的工程实体与可检索片段——用户或 Agent 可问：整体尺寸、包围盒/体积/表面积、装配含几个零件、有哪些实体/面/圆柱面、STL 是否封闭、某零件名称/颜色/材料、某尺寸是精确还是近似、答案来自哪个文件/装配节点/几何实体。**CAD 只是 RagKernel 目前支持的一类工程数据源。**

**安装**（重二进制依赖，作可选 extra、缺失不影响内核启动）：

```bash
uv sync --extra cad                        # 一并装核心 + STEP/STL 后端
uv run ragkernel ingest --path model.step  # .step/.stp/.stl 自动路由，无需额外命令
```

**实际支持与提取字段**

| 格式 | 读取路径 | 提取 |
|---|---|---|
| **STEP / STP** | OpenCASCADE **XDE/XCAF** | 装配树、零件/装配名称、原型 vs 实例（component_instance + 世界变换）、颜色/图层、solids/shells/faces/edges/vertices 计数、**精确 BREP 体积 + 表面积**、**AddOptimal 精确包围盒**（本地 / 世界）、面类型直方图（plane/cylinder/cone/sphere/torus/bspline…）、**圆柱曲面计数**、源单位 vs 计算单位 |
| **STL**（ASCII/二进制） | trimesh | 顶点/三角面数、bounding box/extents、表面积、质心、**watertight**、**有效体积判定**（`is_volume`；要求网格满足封闭性、面方向等体积条件，不只是绕组一致）、**顶点连通体 + 面连通组件两种计数**、编码类型；单位恒 **unknown** |

**精确 vs 近似 vs 声明（每个数值都带来源与有效性，绝不混淆）**

- `brep_computed`（representation=`brep`）—— STEP BREP 精确几何（体积/面积/包围盒；包围盒记 `algorithm=AddOptimal, use_triangulation=false, tight=true`）。
- `mesh_computed`（representation=`mesh`）—— STL 网格积分近似（体积仅在 `is_volume=true` 时给值，否则 `null` + `validity=invalid`；**非封闭/绕组错误的网格绝不给看似精确的可靠体积**）。
- `file_declared` / `file_record_count` —— 文件显式声明（名称/颜色/源单位）或记录读数（三角面数）。
- **单位诚实**：STEP 记录 `源单位 (FileUnits)` 与 `计算单位 mm`（1 inch 文件 → 读回 25.4mm 且标注已转换）；**STL 单位恒 `unknown`，绝不默认 mm**。

**结构化工具（Agent / MCP，返回结构化 JSON）**：`inspect_cad_document` · `list_cad_entities` · `get_cad_entity` · `get_assembly_tree` · `query_geometry`（属性白名单）· `compare_cad_entities` · `search_engineering_objects`。**实体级操作**（`get_cad_entity` / `query_geometry` / `compare_cad_entities`）以稳定 `entity_uid` 寻址；**文档总览与装配树操作**（`inspect_cad_document` / `list_cad_entities` / `get_assembly_tree`）用 `document_id`。

**明确不做（本 MVP）**：DWG / SLDPRT / SLDASM / Parasolid 原生读取（坚持转换器路线：先导出 STEP/DXF，不伪造原生支持）、参数化特征树 / 草图约束、完整 GD&T/PMI、**孔特征识别**（只报圆柱曲面数，`hole_detection.supported=false`）、OBB、设计意图、由几何推断制造工艺。IGES/OBJ/PLY/DXF 已留接口，属后续。

**依赖与平台**：当前项目要求 **Python 3.12–3.13**（`pyproject.toml` `requires-python = ">=3.12,<3.14"`）。CAD extra 用 `trimesh`（MIT，仅需 numpy）与 `cadquery-ocp-novtk`（OCP bindings Apache-2.0 + 内含 OpenCASCADE LGPL-2.1；无头/离线友好，去 VTK）——是否有适配当前 OS/架构/Python 的预编译轮子以安装时解析结果为准；平台若无 novtk 轮子可回退 `cadquery-ocp`（含 VTK）。

> **Results assist document and model retrieval and must be verified in the source CAD system before manufacturing or safety-critical use.** 本功能用于文档与模型检索，制造/安全关键用途前须在原始 CAD 系统中复核。

## LLM provider（`config/settings.yaml` 的 `provider`）

| 场景 | kind | base_url | model | key |
|---|---|---|---|---|
| MiniMax（默认，零成本） | anthropic | `https://api.minimaxi.com/anthropic` | MiniMax-M3 | MINIMAX_API_KEY |
| 官方 Claude | anthropic | 留空 | claude-sonnet-5 | ANTHROPIC_API_KEY |
| 本地私有化 | openai | `http://localhost:8000/v1` | Qwen3-32B-AWQ | 任意非空 |

**私有化本地部署**：本地引擎几乎都是 OpenAI 兼容，用 `kind: openai`。可靠多步 tool-use 的甜点是 **32B**（Qwen3-32B AWQ/INT4，单张 ~48G 卡 L40S/A6000），预算档 14B/24G（RTX 4090）。嵌入 + 重排本来就全本地。serving 推荐 vLLM。

## Docker 一键起

```bash
export MINIMAX_API_KEY=...      # 或 ANTHROPIC_API_KEY
docker compose -f docker/docker-compose.yml up
```

首启下载模型到 `models` 卷，之后秒起；数据持久化到 `./_data`。

## CLI

> `uv` 环境下命令前缀 `uv run`（如 `uv run ragkernel ingest …`）；下例为简洁省略，激活虚拟环境后可直接用 `ragkernel`。

```bash
ragkernel ingest --path ./docs      # 摄取文件或整个目录（PDF/DOCX/MD/TXT/CSV/XLSX + STEP/STL，幂等）
ragkernel embed                     # 补齐缺失向量
ragkernel ask "主轴报E-42怎么处理"    # 命令行问答
ragkernel watch --dir ./inbox       # 监听落盘文件夹，自动索引
ragkernel stats                     # 知识库统计
ragkernel serve                     # 启动 Web
ragkernel mcp serve                 # 启动 MCP Server（把只读检索暴露给 Agent）
ragkernel token new --user <name>   # 签发 agent token（MCP 鉴权用，只显示一次）
```

## 验证

```bash
uv run python scripts/smoke_test.py          # 端到端：摄取→嵌入→检索→（有 key 时）带引用问答
uv run python scripts/eval_retrieval.py      # Recall@k / MRR，rerank on/off 对比（隔离到 eval/eval_out，不污染 KB）
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
  mcp/  server.py http.py verify.py                    # MCP Server（只读检索 + 工程 claim 核验，token 鉴权）
  pipeline.py                                          # 摄取编排 + 自动嵌入 + 反馈回填 + CAD 原子写入(load_bundle)
  tools.py agent.py                                    # 带引用、可按分类/字段过滤、含 CAD 结构化工具的 agentic 问答
  verticals/  equipment.py                             # 可插拔垂直层（设备维修；换一个模块即换行业，内核不动）
  webapp.py static/index.html                          # 上传 + 带引用聊天 + 记录处理结果 + 仪表盘
config/settings.yaml                                   # provider / 检索 / 上传 / 垂直层 / MCP 配置
```

## 能力范围（诚实）

精细解析**技术 PDF 里的文本、表格、针脚定义、电气图与机械尺寸图的文字标注、扫描内容**，产真实页码引用；**并原生读取 STEP/STL 的可验证几何**（装配树、精确 BREP 体积/包围盒、网格有效性）。**不读** DWG / SLDPRT / Parasolid 原生几何、不重建参数化特征树、不做完整 GD&T 与孔特征识别（圆柱曲面 ≠ 确认孔）。技术图纸做到「乱码/空 → 可读文本+表格+尺寸标注+页码」，不做「机器看懂几何」。**PDF 页码引用以解析器提供的元素 provenance 为准；当前跨页长表的行可能统一引用表格起始页，区域级 bbox 引用与 Docling Item → Block 直通尚未实现。** 单租户 MVP；多租户/RBAC 属后续。

## 致谢 / Acknowledgments

- **[Docling](https://github.com/docling-project/docling)**（IBM，MIT License）+ **[RapidOCR](https://github.com/RapidAI/RapidOCR)**（Apache-2.0，PP-OCRv6 中文 ONNX）—— PDF 版面/表格结构分析 + OCR，修中文图纸/扫描件识别度、产真实页码引用。
- **[MarkItDown](https://github.com/microsoft/markitdown)**（Microsoft，MIT License）—— Word / PPT / HTML → Markdown 的文档转换（PDF 已交给 Docling）。
- **[BAAI bge-m3 / bge-reranker-v2-m3](https://huggingface.co/BAAI)** —— 本地向量与重排模型。
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** —— SQLite 向量检索扩展。
- **[jieba](https://github.com/fxsjy/jieba)** —— 中文分词。
- **[Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk)**（MIT License）—— Agent 只读检索接口。
- **[trimesh](https://github.com/mikedh/trimesh)**（MIT License）—— STL 网格几何与有效性判定（可选 `ragkernel[cad]`）。
- **[Open CASCADE Technology](https://dev.opencascade.org/) via [OCP](https://github.com/CadQuery/OCP)**（OCP bindings Apache-2.0 + OCCT LGPL-2.1）—— STEP 装配/BREP 精确几何解析（可选 `ragkernel[cad]`）。

---

## Design Principles

- **Verifiable by default** — every claim traces back to a citation or a measured value.
- **Preserve structure before flattening** — tables, entities, provenance, and geometry remain structured wherever the source and parser support it.
- **Honest uncertainty** — when something can't be verified, it says so instead of guessing.
- **Provider independent** — Claude / MiniMax / local vLLM · Ollama, swappable.
- **Local-first storage and retrieval** — the corpus, index, embeddings, and reranker stay local; retrieved context is sent only to the configured generation provider. Use a local OpenAI-compatible backend for fully private operation.
- **Read-only by default** — agent tools observe knowledge, never mutate it.

## Philosophy

> RagKernel prefers evidence over confidence.
>
> When information cannot be verified, it reports uncertainty instead of guessing.
>
> Structured engineering data is preserved whenever possible rather than flattened into free text.
>
> AI agents should retrieve engineering evidence, not fabricate engineering facts.
