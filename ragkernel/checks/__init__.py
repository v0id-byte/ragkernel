"""按领域分文件的检查实现。

每个模块导出自己的 CHECKS 列表，由 diagnostics.runner 拼接。
分文件是为了不让它长成一个两千行的 checks.py。

硬约束：这里不许 import huggingface_hub 或任何模型下载 SDK——
只能经 models.get_cache_status()。否则离线部署时诊断层会跟着炸，
而那正是最需要诊断的时刻。
"""
