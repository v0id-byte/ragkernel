# RagKernel Documentation

> 部署、配置、能力边界、CLI 与架构的完整文档。文档正文为中文；项目入口见根目录 [README](../README.md)。

## Getting started

| 文档 | 内容 |
|---|---|
| [安装与部署](installation.md) | 一键安装器与全部变体、手动安装、Docker、平台与 Python 要求 |
| [配置与 setup](configuration.md) | provider 配置与优先级、`ragkernel setup` 分步选项、密钥存储定位 |

## Concepts

| 文档 | 内容 |
|---|---|
| [能力范围](capabilities.md) | 支持的格式矩阵、知识表示、检索、验证保证与当前边界 |
| [架构总览](architecture/overview.md) | 引擎分层与模块对应、代码地图 |
| [文档生命周期与归属权限](architecture/document-lifecycle.md) | active → archived → deleted、检索隔离、权限模型 |
| [Web 界面](web-ui.md) | 摄取、仪表盘、管理控制台 |

## Reference

| 文档 | 内容 |
|---|---|
| [CLI 命令参考](cli.md) | 全部子命令 + 端到端验证与评测脚本 |
| [诊断契约](diagnostics.md) | `ragkernel doctor` 的输出契约、退出码、JSON 结构 |

## Advanced

| 文档 | 内容 |
|---|---|
| [原生 CAD 摄取（STEP/STL）](cad.md) | 格式与字段、精确/近似/声明、CAD 工具、明确不做 |
| [设计原则与理念](design-principles.md) | 取舍依据与基本立场 |
| [致谢](acknowledgments.md) | 依赖的开源项目与许可证 |
