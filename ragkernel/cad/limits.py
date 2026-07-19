"""CAD 摄取上限 + 干净中止。

超限**不静默截断**：backend 捕获 CADLimitExceeded → 产出一个标记 aborted 的 CADDocument
（仅一个 document 实体 + 一条说明性状态 chunk），pipeline 据此把 documents.status 置 'rejected'。
"""

from __future__ import annotations

DEFAULTS = {
    "max_file_mb": 250,               # 超过直接拒（load 前预检）
    "max_triangles": 5_000_000,       # STL：process=False 快载后立即检查
    "max_entities": 10_000,           # 实体总数（walk 期间累计）
    "max_assembly_depth": 64,         # 装配递归深度
    "max_seconds": 120,               # 软上限，在循环边界检查（OCP C++ 调用不可中途打断）
    "ext_ref_depth": 0,               # 外部引用递归深度（0=不跟随）
    "max_stl_component_chunks": 16,   # STL 连通组件最多产出多少片 chunk
    "face_component_max_triangles": 200_000,  # 超过则跳过 face-connected 组件分析（split 成本）
}


class CADLimitExceeded(Exception):
    def __init__(self, reason: str, observed=None, limit=None):
        self.reason = reason
        self.observed = observed
        self.limit = limit
        super().__init__(reason)

    def as_meta(self) -> dict:
        m = {"aborted": True, "abort_reason": self.reason}
        if self.observed is not None:
            m["observed"] = self.observed
        if self.limit is not None:
            m["limit"] = self.limit
        return m


def load_limits() -> dict:
    from .. import config

    c = (config.settings().get("cad") or {})
    return {k: c.get(k, v) for k, v in DEFAULTS.items()}
