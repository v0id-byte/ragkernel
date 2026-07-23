# 安装与部署

> 一键安装器的全部变体、手动安装、Docker，以及平台与依赖要求。

[← Back to documentation](README.md)

## 一键安装

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

## 安装器变体

```bash
RAGKERNEL_REF=v0.4.0 sh install.sh                       # pin 版本（接受 branch / tag / commit）
RAGKERNEL_DIR=/opt/ragkernel sh install.sh               # 装到系统目录（企业机器）
sh install.sh --cad                                      # 一并装原生 CAD extra（STEP/STL）
sh install.sh --no-setup                                 # 只装环境、不进向导
```

> 生产部署请 pin 具体 `--ref`（回答「raw.githubusercontent 被替换怎么办」——固定 commit/tag 即可复现）。
>
> 若安装在**无交互终端**下完成（管道 / CI / `docker RUN` / 非 tty SSH），安装器不会进配置向导、只打印下一步命令；此时手动执行 `cd ~/ragkernel && uv run ragkernel setup` 即可（自定义了 `RAGKERNEL_DIR` 就 `cd` 到对应目录）。

## 手动安装（高级用户）

```bash
cd ragkernel
cp .env.example .env          # 默认走 MiniMax，填 MINIMAX_API_KEY（或改 settings.yaml 切 Claude/本地）
uv sync                       # 文档、检索、Web 与 MCP
# 需要原生 CAD 时改用下面这条即可（一并装核心 + STEP/STL 后端，不必先跑上面那条）：
uv sync --extra cad
uv run ragkernel setup        # 交互初始化：provider / 管理员 / 模型 / MCP token（或手动逐步配）
uv run ragkernel models       # 首次下载本地嵌入/重排模型（~2GB，仅一次）
uv run ragkernel serve        # 打开 http://127.0.0.1:8360
```

> 命令统一以 `uv run ragkernel …` 给出（在 `uv` 环境下即取即用）；若已激活虚拟环境，可省略 `uv run`。

网页里拖手册 / 工单 / CAD 进去 → 自动索引 → 提问 → 得到带引用（含分类、页码）的答案 → 「记录处理结果」回填。

## Docker 一键起

```bash
export MINIMAX_API_KEY=...      # 或 ANTHROPIC_API_KEY
docker compose -f docker/docker-compose.yml up
```

首启预载模型到 `models` 卷（失败不阻断，首个请求会重试），之后秒起；数据持久化到 `./_data`。

## 依赖与平台

当前项目要求 **Python 3.12–3.13**（`pyproject.toml` `requires-python = ">=3.12,<3.14"`）。CAD extra 用 `trimesh`（MIT，仅需 numpy）与 `cadquery-ocp-novtk`（OCP bindings Apache-2.0 + 内含 OpenCASCADE LGPL-2.1；无头/离线友好，去 VTK）——是否有适配当前 OS/架构/Python 的预编译轮子以安装时解析结果为准；平台若无 novtk 轮子可回退 `cadquery-ocp`（含 VTK）。

装完下一步：[配置 provider](configuration.md) · [CLI 命令](cli.md) · [自查 doctor](diagnostics.md)
