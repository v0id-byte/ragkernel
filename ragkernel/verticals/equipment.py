"""设备维修 / 售后故障知识库垂直层。

拆片:手册按故障条目/字段标记细分(不合并),每片按内容打分类;工单/反馈整条一片(故障案例)。
分类:故障现象 / 故障原因 / 处理步骤 / 安全警告 / 参数规格 / 保养维护 / 备件 / 故障案例 / 其他。
换行业只改本文件(正则 + 关键词表 + prompt),内核一行不动。
"""

import re

from .. import config
from ..chunking import chunk_blocks, md_to_blocks
from . import register

# 领域精确键抽取:故障码 / 针脚——写进 meta 供 BM25 精确命中 + 按字段过滤。
_FAULT = re.compile(r"E-?\d{1,4}|F\d{2,5}|报警\s*\d{1,4}|Err?\.?\s*\d{1,4}")
_PIN = re.compile(r"\bP\d{1,3}\b")


def _enrich(meta: dict, body: str) -> None:
    """给一片补领域精确键（故障码/针脚），供混合检索精确命中与元数据过滤。"""
    fc = _FAULT.search(body)
    if fc:
        meta["fault_code"] = fc.group(0)
    if meta.get("element_type") in ("kv", "table"):
        pin = _PIN.search(body)
        if pin:
            meta["pin_label"] = pin.group(0)


# 分类关键词首命中表(安全警告优先——安全相关必须先被识别)
_RULES = [
    ("安全警告", ["警告", "危险", "断电", "触电", "防护", "严禁", "注意安全", "高温", "高压"]),
    ("处理步骤", ["处理", "步骤", "更换", "检查", "排除", "维修", "复位", "重启", "操作", "解决", "调整"]),
    ("故障现象", ["现象", "症状", "报警", "报错", "故障代码", "异常", "不工作", "停机", "无法", "显示"]),
    ("故障原因", ["原因", "由于", "导致", "因为", "造成"]),
    ("参数规格", ["参数", "额定", "型号", "电压", "电流", "扭矩", "规格", "功率", "转速"]),
    ("保养维护", ["保养", "润滑", "定期", "维护周期", "清洁", "加油", "紧固"]),
    ("备件", ["备件", "物料号", "零件号", "BOM", "配件"]),
]

CATEGORIES = [c for c, _ in _RULES] + ["故障案例", "其他"]


def classify(text: str) -> str:
    for cat, kws in _RULES:
        if any(kw in text for kw in kws):
            return cat
    return "其他"


class Equipment:
    name = "equipment"

    def system_fragment(self) -> str:
        return (
            "【设备维修 / 售后场景】你是设备维修与售后故障知识助手。"
            "优先给出可执行的故障处理步骤,并引用手册页码或相似的历史工单/反馈案例;"
            "凡涉及安全警告(断电/防护/危险)务必置于最前提醒;"
            "查不到就如实说明,绝不臆测维修方案——错误维修可能造成安全事故。"
            "可先调用 list_categories 看有哪些分类,再用 search_by_category 在'处理步骤'/'故障现象'等类别里精准检索;"
            "遇到明确的故障码/针脚/型号时,用 search_by_field(如 field='fault_code',value='E-42')精确定位那一条;"
            "涉及具体型号时,优先引用同型号设备的既往处理记录(故障案例)。"
        )

    def split(self, page_text: str, page_no: int | None):
        """markdown → 有类型元素 → 元素级分块（表格按行/工序整片/键值逐条/正文按大小），
        每片打分类 + 补领域精确键。内核只认 (title, body, meta) 三元组，故拆片智能全在此。"""
        text = (page_text or "").strip()
        if not text:
            return []
        ch = config.settings().get("chunking") or {}
        min_c, max_c = int(ch.get("min_chars", 200)), int(ch.get("max_chars", 3000))
        out = []
        for title, body, meta in chunk_blocks(md_to_blocks(text), min_c, max_c):
            m = dict(meta)
            m["category"] = classify(body)
            m["by"] = "rule"
            _enrich(m, body)
            out.append((title, body, m))
        return out

    def classify(self, text: str) -> str:
        return classify(text)

    def extra_tools(self, toolbox):
        def list_categories() -> str:
            rows = toolbox.db.execute(
                "SELECT json_extract(meta_json,'$.category') c, COUNT(*) n FROM chunks "
                "WHERE meta_json IS NOT NULL GROUP BY c ORDER BY n DESC"
            ).fetchall()
            toolbox.audit("tool:list_categories", {})
            if not rows:
                return "（暂无分类信息——先上传手册/工单）"
            return "\n".join(f"{r['c'] or '未分类'}: {r['n']} 片" for r in rows)

        def search_by_category(query: str, category: str, k: int = 8) -> str:
            return toolbox.search_documents(query, k=k, category=category)

        def search_by_field(query: str, field: str, value: str, k: int = 8) -> str:
            return toolbox.search_by_meta(query, field, value, k=k)

        specs = [
            {
                "name": "list_categories",
                "description": "列出知识库里的内容分类(故障现象/处理步骤/安全警告/故障案例等)及各类片段数。先看有哪些类,再按类检索。",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "search_by_category",
                "description": "只在指定分类里检索(如 category='处理步骤' 只查处理步骤)。分类取自 list_categories。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "k": {"type": "integer"},
                    },
                    "required": ["query", "category"],
                },
            },
            {
                "name": "search_by_field",
                "description": (
                    "按元数据字段精确过滤检索。field 可选:element_type(table/procedure/kv/prose)、"
                    "fault_code(故障码如 E-42)、pin_label(针脚如 P3)、model(设备型号)、"
                    "table_subtype(故障码表/针脚表/参数表/备件表)。例:field='fault_code',value='E-42' 只查该故障码那行。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                        "k": {"type": "integer"},
                    },
                    "required": ["query", "field", "value"],
                },
            },
        ]
        return specs, {
            "list_categories": list_categories,
            "search_by_category": search_by_category,
            "search_by_field": search_by_field,
        }

    def post_retrieve(self, query: str, chunks: list) -> list:
        return chunks

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]:
        return chunks


register("equipment", Equipment)
