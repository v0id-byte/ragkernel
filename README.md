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
- **统计面板**：文件 / 片段 / 索引 / 提问 / 来源构成 / 分类分布 / 摄取历史，均有记录。

> 单租户 MVP。多租户/RBAC、多模态拍照报障属后续。

> **能力范围（诚实）**：精细解析**技术 PDF 里的文本、表格、针脚定义、电气图与机械尺寸图的文字标注、扫描内容**，产真实页码引用。**不是**原生 CAD 解析——不读 DWG/DXF 几何、不重建装配拓扑或公差配合语义。图纸做到「乱码/空 → 可读文本+表格+尺寸标注+页码」，不做「机器看懂几何」。

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
ragkernel ingest --path ./docs      # 摄取文件或整个目录（PDF/DOCX/MD/TXT/CSV/XLSX，幂等）
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

## 验证

```bash
uv run python scripts/smoke_test.py          # 端到端：摄取→嵌入→检索→（有 key 时）带引用问答
uv run python scripts/eval_retrieval.py      # Recall@k / MRR，rerank on/off 对比
```

## 结构

```
ragkernel/
  chunking.py embed.py rerank.py search.py store.py   # 检索内核（全本地）
  backends.py                                          # Anthropic / OpenAI 兼容生成后端
  connectors/                                          # PDF/DOCX/MD/TXT/CSV/XLSX → 统一管线
  pipeline.py                                          # 摄取编排 + 自动嵌入 + 反馈回填(ingest_record)
  tools.py agent.py                                    # 带引用、可按分类过滤的 agentic 问答
  verticals/  equipment.py                             # 可插拔垂直层（设备维修）
  webapp.py static/index.html                          # 上传 + 带引用聊天 + 记录处理结果 + 仪表盘
config/settings.yaml                                   # provider / 检索 / 上传 / 垂直层 配置
```

## 致谢 / Acknowledgments

- **[Docling](https://github.com/docling-project/docling)**（IBM，MIT License）+ **[RapidOCR](https://github.com/RapidAI/RapidOCR)**（Apache-2.0，PP-OCRv6 中文 ONNX）—— PDF 版面/表格结构分析 + OCR，修中文图纸/扫描件识别度、产真实页码引用。
- **[MarkItDown](https://github.com/microsoft/markitdown)**（Microsoft，MIT License）—— Word / PPT / HTML → Markdown 的文档转换（PDF 已交给 Docling）。
- **[BAAI bge-m3 / bge-reranker-v2-m3](https://huggingface.co/BAAI)** —— 本地向量与重排模型。
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** —— SQLite 向量检索扩展。
- **[jieba](https://github.com/fxsjy/jieba)** —— 中文分词。
