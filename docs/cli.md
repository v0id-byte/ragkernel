# CLI 命令参考

> 全部 `ragkernel` 子命令，以及端到端验证与评测脚本。

[← Back to documentation](README.md)

> `uv` 环境下命令前缀 `uv run`（如 `uv run ragkernel ingest …`）；下例为简洁省略，激活虚拟环境后可直接用 `ragkernel`。

## 命令

```bash
ragkernel setup                     # 交互初始化：provider / 管理员 / 模型 / 集成（--yes 非交互；--only / --with-token）
ragkernel doctor                    # 环境自查：分层检查 + 四档退出码（--json 供监控/就绪探针；--offline 跳网络）
ragkernel ingest --path ./docs      # 摄取文件或整个目录（PDF/DOCX/MD/TXT/CSV/XLSX + STEP/STL，幂等）
ragkernel embed                     # 补齐缺失向量
ragkernel models                    # 预载本地嵌入/重排模型（~2GB，仅一次）
ragkernel ask "主轴报E-42怎么处理"    # 命令行问答
ragkernel watch --dir ./inbox       # 监听落盘文件夹，自动索引
ragkernel stats                     # 知识库统计
ragkernel serve                     # 启动 Web
ragkernel mcp serve                 # 启动 MCP Server（把只读检索暴露给 Agent）
ragkernel token new --user <name>   # 签发 agent token（MCP 鉴权用，只显示一次）
```

`setup` 的分步选项与 `doctor` 的输出契约分别见 [配置](configuration.md) 与 [诊断](diagnostics.md)。

## 验证

```bash
ragkernel doctor                             # 最快自查：运行时 / 存储 / provider / 模型缓存 → 退出码 0/1/2/3
uv run python scripts/smoke_test.py          # 端到端：摄取→嵌入→检索→（有 key 时）带引用问答
uv run python scripts/eval_retrieval.py      # Recall@k / MRR，rerank on/off 对比（隔离到 eval/eval_out，不污染 KB）
# 原生 CAD（需 cad extra）
uv run --extra cad python scripts/gen_cad_fixtures.py   # 程序化生成 CAD 测试夹具（提交进 tests/fixtures/cad/）
uv run --extra cad --extra dev pytest tests/            # CAD 单元 + 检索 QA + 结构化值检查（无 extra 则干净跳过）
uv run --extra cad python scripts/cad_benchmark.py      # CAD 摄取性能：冷启/耗时/峰值内存/无索引爆炸
```
