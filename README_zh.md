# RagKernel

### 面向人类与 AI Agent 的可验证工程知识引擎。

[![License: BSL 1.1](https://img.shields.io/badge/license-BSL%201.1-blue.svg)](LICENSE) · [English](README.md) · [完整文档](docs/README.md)

`技术文档` · `工程实体` · `混合检索` · `Claim 核验` · `MCP` · `原生 STEP/STL`

> RagKernel 把工程文档、CAD 模型与设备数据变成结构化、可检索、**可验证**的知识底座；更多工程格式在同一套摄取契约下逐步接入。

![RagKernel evidence-grounded engineering chat — a fault-code answer where every field cites its source document, section, and page](docs/assets/screenshot-chat.png)

<sub>自然语言提问 → 先检索证据、再生成回答，每条结论都能追回源文档与章节。语料里没有的，它会直说。</sub>

## 快速开始

一行安装（装 uv → clone → 依赖 → 进配置向导）。它要碰数据库、API key 与你的文档，**建议先下载审阅再执行**：

```bash
curl -fsSL https://raw.githubusercontent.com/v0id-byte/ragkernel/main/install.sh -o install.sh
less install.sh          # 审阅
sh install.sh
```

或直接管道执行：

```bash
curl -fsSL https://raw.githubusercontent.com/v0id-byte/ragkernel/main/install.sh | sh
```

安装器会引导：运行环境（uv / Python 3.12 / 依赖）→ LLM provider → 本地模型 → 管理员账号 → MCP 集成。装完随时 `ragkernel doctor` 自查。

手动安装（高级用户）：

```bash
uv sync                       # 需要原生 CAD 时用 uv sync --extra cad
uv run ragkernel setup        # 交互初始化：provider / 管理员 / 模型 / MCP token
uv run ragkernel models       # 首次下载本地嵌入/重排模型（~2GB，仅一次）
uv run ragkernel serve        # 打开 http://127.0.0.1:8360
```

网页里拖手册 / 工单 / CAD 进去 → 自动索引 → 提问 → 得到带引用（含分类、页码）的答案 → 「记录处理结果」回填。

安装器变体、Docker、非交互/CI 安装 → [docs/installation.md](docs/installation.md)。

## 为什么做 RagKernel

做嵌入式系统与 PCB 设计时，我反复在几百页的数据手册和参考手册里翻查。传统 RAG 把工程资料压平成文本片段——工程上下文丢了，答案与原始证据之间的链接也断了。RagKernel 保留文档结构、证据链、provenance 与几何信息，让 AI 的输出不仅相关，而且**可追溯、可验证**。

## 能力

- **工程文档摄取** —— PDF（Docling + RapidOCR，真实页码）、DOCX / PPTX / HTML、Markdown / TXT、CSV / XLSX 工单。
- **原生 CAD 摄取（STEP / STL）** —— 装配树、精确 BREP 几何、网格有效性判定；可选 `[cad]` extra，详见 [docs/cad.md](docs/cad.md)。
- **元素感知切片** —— 规格 / 故障码 / 针脚表一行一片，操作步骤保持完整，工程尺寸原样保留，每片带结构化元数据。
- **混合检索** —— BM25 + 向量 RRF 融合，本地 cross-encoder 重排，支持按字段精确过滤（`search_by_field`）。
- **可追溯引用** —— 每条结果带稳定的文档与片段标识；源格式与解析器提供时附页码。
- **基于证据的 claim 核验** —— `verify_engineering_claim` 返回 *supported / contradicted / unsupported* 并附真实页码。
- **显式 provenance** —— CAD 测量区分 `brep_computed` / `mesh_computed` / `file_declared`；无效网格返回 invalid 状态，而不是一个误导性的体积。
- **MCP Server** —— 只读检索暴露给 Agent（stdio + HTTP，token 鉴权，分档限流），与 CLI、Web 共享同一份知识。
- **开箱可运维** —— 一行部署、引导式 `ragkernel setup`、只读 `ragkernel doctor`（带 JSON 输出供监控）。

完整能力矩阵与格式支持 → [docs/capabilities.md](docs/capabilities.md)。

## 文档

完整文档在 [`docs/`](docs/README.md)。

| 文档 | 内容 |
|---|---|
| [安装与部署](docs/installation.md) | 安装器变体、手动安装、Docker、平台要求 |
| [配置与 setup](docs/configuration.md) | provider 配置、优先级规则、`ragkernel setup` |
| [能力范围](docs/capabilities.md) | 格式矩阵、检索、保证与当前边界 |
| [CLI 命令参考](docs/cli.md) | 全部子命令 + 验证与评测脚本 |
| [架构总览](docs/architecture/overview.md) | 引擎分层、代码地图、文档生命周期 |
| [原生 CAD](docs/cad.md) | STEP/STL 格式、精确 vs 近似几何、CAD 工具 |
| [诊断契约](docs/diagnostics.md) | `ragkernel doctor` 输出契约、退出码、JSON 结构 |
| [Web 界面](docs/web-ui.md) | 摄取、仪表盘、管理控制台 |
| [设计原则](docs/design-principles.md) | 原则与理念 |

## 能力范围（诚实）

精细解析**技术 PDF 里的文本、表格、针脚定义、电气图与机械尺寸图的文字标注、扫描内容**，产真实页码引用；**并原生读取 STEP/STL 的可验证几何**（装配树、精确 BREP 体积/包围盒、网格有效性）。**不读** DWG / SLDPRT / Parasolid 原生几何、不重建参数化特征树、不做完整 GD&T 与孔特征识别（圆柱曲面 ≠ 确认孔）。**PDF 页码引用以解析器提供的元素 provenance 为准；当前跨页长表的行可能统一引用表格起始页。** 单租户 MVP；多租户/RBAC 属后续。

## 许可

RagKernel 采用 **Business Source License 1.1（源码可见协议）**。
允许个人、教育、研究与企业内部使用，可自托管、修改，并可基于本项目提供咨询、集成与定制开发；面向第三方的商业化托管 / 托管代运营服务需另行获得商业授权。
到 Change Date（**2029-07-21**）后，各已发布版本自动转为 **Apache License 2.0**。完整条款见 [LICENSE](LICENSE)。

由 v0id-byte 创建与维护，版权归 Liuhaoran Qin 所有（© 2026）。
