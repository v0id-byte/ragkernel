"""CAD 统一契约：EngineeringEntity / CADDocument + MeasuredValue（三正交 provenance 字段）。

MeasuredValue 把「来源方法 / 表示 / 质量 / 有效性」拆成正交字段，绝不把
"数据来源" 和 "计算精度" 混为一谈：
  source_method  : file_declared | file_record_count | brep_computed | mesh_computed | heuristic | unknown
  representation : metadata | brep | mesh | derived | unknown
  quality        : high | medium | low | unknown
  validity       : valid | invalid | not_applicable | unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 给 UI/摘要的显示映射（不改变结构化字段，只是可读化）。
METHOD_DISPLAY = {
    "brep_computed": "精确 BREP 计算",
    "mesh_computed": "网格近似计算",
    "file_declared": "文件显式声明",
    "file_record_count": "文件记录读数",
    "heuristic": "启发式推断",
    "unknown": "未知",
}


def mv(value, unit, source_method, representation,
       quality="high", validity="valid", source_entity=None, **extra) -> dict:
    """构造一个 MeasuredValue。extra 可带 algorithm / tight / use_triangulation / warning 等。"""
    d = {
        "value": value,
        "unit": unit,
        "source_method": source_method,
        "representation": representation,
        "quality": quality,
        "validity": validity,
    }
    if source_entity is not None:
        d["source_entity"] = source_entity
    d.update(extra)
    return d


@dataclass
class EngineeringEntity:
    entity_uid: str                         # 稳定标识（STEP=XDE label entry；STL=mesh/component 路径）
    entity_type: str                        # document|assembly|component_instance|part|body|solid|mesh|material|layer...
    name: str | None = None
    parent_uid: str | None = None
    prototype_uid: str | None = None        # component_instance -> 其 part 原型
    occurrence_uid: str | None = None
    location_matrix: list[list[float]] | None = None  # 4x3（世界变换）——仅 component_instance
    geometry_frame: str | None = None       # local | world | None
    assembly_path: tuple[str, ...] = ()
    source_format: str = ""
    properties: dict = field(default_factory=dict)    # MeasuredValue 集合（volume/area/...）
    geometry: dict = field(default_factory=dict)       # bbox/counts/面直方图/孔检测说明等
    provenance: dict = field(default_factory=dict)     # 文件名/格式/sha/parser/warnings —— 无绝对路径
    confidence: str = "unknown"


@dataclass
class CADDocument:
    filename: str
    format: str                             # step | stl
    units: dict = field(default_factory=dict)          # 单位块（STEP 源/计算单位；STL 恒 unknown）
    entities: list[EngineeringEntity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)       # counts / aborted / abort_reason / parser 等

    def add(self, ent: EngineeringEntity) -> EngineeringEntity:
        self.entities.append(ent)
        return ent
