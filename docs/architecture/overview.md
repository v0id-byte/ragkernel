# 架构总览

> 引擎分层、代码地图，以及文档生命周期的入口。

[← Back to documentation](../README.md)

## 分层

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

引擎五个动作各自对应明确的模块：

- **ingest** —— `connectors/` 按扩展名路由到对应解析后端（PDF 走 Docling，Office/HTML 走 MarkItDown，STEP/STL 走 `cad/`）；`pipeline.py` 负责编排、幂等与自动嵌入。
- **structure** —— `chunking.py` 做元素感知切片（表格一行一片、步骤保持完整、工程尺寸原样保留）；`cad/normalize.py` 把几何后端的输出归一成统一的工程实体。
- **index** —— `embed.py` 产向量、`store.py` 落 SQLite（含 sqlite-vec 向量表与 BM25 词法索引），每个单元两套索引都建。
- **retrieve** —— `search.py` 融合 BM25 与向量（RRF）后由 `rerank.py` 本地 cross-encoder 重排，并支持按字段精确过滤。检索隔离也在这里一处收口。
- **verify** —— `mcp/verify.py` 对 claim 做证据核验；`tools.py` / `agent.py` 组织带引用的问答。

三个出口共享同一份知识：CLI 与 Web（`webapp.py`）面向人，MCP Server（`mcp/`）面向 Agent，只读。

## 文档生命周期与归属

文档不是只进不出的：每份资料都有**归属**与**生命周期**（active → archived → deleted），归档即退出检索但保留数据、可恢复，硬删除仅管理员。检索隔离在 `search.py` 一处收口，不交给调用方——详见 [文档生命周期与归属权限](document-lifecycle.md)。

## 代码地图

按「摄取 → 表示 → 检索 → 出口 → 运维」阅读，比按目录字母序更快：

```
install.sh                                             # 一键装：uv → clone → 依赖 → 交棒 ragkernel setup
ragkernel/
  chunking.py embed.py rerank.py search.py store.py   # 检索内核（全本地）
  backends.py                                          # Anthropic / OpenAI 兼容生成后端
  connectors/  cad.py                                  # PDF/DOCX/MD/TXT/CSV/XLSX + STEP/STL → 统一管线
  cad/  step_backend.py mesh_backend.py normalize.py   # 原生 CAD：OCP/XDE + trimesh → 工程实体 + 检索片（可选 extra）
  mcp/  server.py http.py verify.py                    # MCP Server（只读检索 + 工程 claim 核验，token 鉴权）
  pipeline.py                                          # 摄取编排 + 自动嵌入 + 反馈回填 + CAD 原子写入(load_bundle)
  tools.py agent.py                                    # 带引用、可按分类/字段过滤、含 CAD 结构化工具的 agentic 问答
  verticals/  equipment.py                             # 可插拔垂直层（设备维修；换一个模块即换行业，内核不动）
  bootstrap.py                                         # ragkernel setup 交互向导（provider/admin/models/token）
  doctor.py                                            # ragkernel doctor 渲染 + 四档退出码（只读）
  models.py                                            # 模型下载 + 缓存完整性探测（供 setup / doctor 复用）
  diagnostics/                                         # 诊断契约（CheckResult / HealthPolicy）+ 编排 runner
  checks/                                              # 分领域检查（runtime / storage / provider / model）——doctor 的事实来源
  webapp.py static/index.html                          # 上传 + 带引用聊天 + 记录处理结果 + 仪表盘
config/settings.yaml                                   # provider / 检索 / 上传 / 垂直层 / MCP 配置
docs/                                                  # 文档：installation · configuration · capabilities · cad · cli · architecture/
```
