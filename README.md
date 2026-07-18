# ragkernel

本地优先的**企业 RAG 内核**：混合检索（BM25 + 向量 + RRF + 重排）+ 带条款级引用的问答 + 傻瓜式自动索引。

- **全本地检索**：`bge-m3` 向量 + `bge-reranker-v2-m3` 重排 + `sqlite-vec`，语料不出机。
- **provider 无关**：一行 `base_url` 在 Claude / MiniMax / 本地 OpenAI 兼容模型间切换。
- **傻瓜式索引**：拖文件进网页、丢进监听文件夹、或 CLI 批量——自动分块+向量化。
- **引用优先、抗幻觉**：答案只依据检索到的文档，每条事实标 `[D<文档>#<块> p.<页>]`；查不到就直说。
- **可插拔垂直层**：合同/法律等垂直逻辑作为独立模块挂进 `verticals/`，不碰内核。

> 单租户 MVP。多租户/RBAC、具体垂直逻辑、语气模仿均不在本期（语气模仿是另一条产品线）。

## 快速开始（本地开发，mac 走 MPS 加速）

```bash
cd ragkernel
cp .env.example .env          # 填 ANTHROPIC_API_KEY（或改 settings.yaml 用 MiniMax）
uv sync
uv run ragkernel models       # 首次下载本地模型（~2GB，仅一次）
uv run ragkernel serve        # 打开 http://127.0.0.1:8360
```

网页里拖一个 PDF/DOCX/MD/TXT 进去 → 自动索引 → 提问 → 得到带引用的答案。

## CLI

```bash
ragkernel ingest --path ./docs      # 摄取文件或整个目录（幂等）
ragkernel embed                     # 补齐缺失向量
ragkernel ask "年假有多少天"         # 命令行问答
ragkernel watch --dir ./inbox       # 监听落盘文件夹，自动索引
ragkernel stats                     # 知识库统计
ragkernel serve                     # 启动 Web
```

## Docker 一键起

```bash
export ANTHROPIC_API_KEY=sk-...
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
  connectors/                                          # PDF/DOCX/MD/TXT → 统一管线
  pipeline.py                                          # 摄取编排 + 自动嵌入
  tools.py agent.py                                    # 带引用的 agentic 问答
  verticals/                                           # 可插拔垂直层（NullVertical）
  webapp.py static/index.html                          # 上传 + 带引用聊天 UI
config/settings.yaml                                   # provider / 检索 / 上传 配置
```

配置 provider：编辑 `config/settings.yaml` 的 `provider.base_url`（留空=官方 Claude；填 `https://api.minimaxi.com/anthropic`=MiniMax），并在 `.env` 提供对应 key。
