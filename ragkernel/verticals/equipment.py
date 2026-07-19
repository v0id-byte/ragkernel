"""设备维修 / 售后故障知识库垂直层。

拆片:手册按故障条目/字段标记细分(不合并),每片按内容打分类;工单/反馈整条一片(故障案例)。
分类:故障现象 / 故障原因 / 处理步骤 / 安全警告 / 参数规格 / 保养维护 / 备件 / 故障案例 / 其他。
换行业只改本文件(正则 + 关键词表 + prompt),内核一行不动。
"""

import re

from .. import config
from ..chunking import _DIMENSION, chunk_blocks, md_to_blocks
from . import register

# 领域精确键抽取:故障码 / 针脚——写进 meta 供 BM25 精确命中 + 按字段过滤。
# 故障/报警码:E-42 E42 E42.1 · F0022 · A-03 A.90 · AL.013 Er.740 · OC1 OV2 UV · 报警12 故障码3
_FAULT = re.compile(
    r"\bE-?\d{1,4}(?:\.\d+)?\b"          # E-42 / E42 / E42.1
    r"|\bF\d{2,5}\b"                      # F0022
    r"|\bA[.\-]\d{1,3}\b"               # A-03 / A.90（需分隔符,避开 A4 这类）
    r"|\b(?:AL|Er|Err)\.?-?\d{1,4}\b"   # AL.013 / Er.740
    r"|\b(?:OC|OV|UV)\d{0,2}\b"          # OC1 / OV2 / UV
    r"|报警\s*\d{1,4}|故障码?\s*\d{1,4}",
    re.I,
)
# 针脚/端子:P3 · PA0/PB12(MCU) · GPIO46 · IO0 · U1/V1/W1(电机相端子)
_PIN = re.compile(r"\bP[A-F]?\d{1,3}\b|\b[UVW]\d{1,2}\b|\bGPIO\d{1,2}\b|\bIO\d{1,2}\b", re.I)
# 连接器-针脚:CN1-12 / X1:12 / J3-5 / TB2-4 → 归一为 connector + pin_number
_CONN_PIN = re.compile(r"\b(CN|TB|J|X|K)\s?(\d{1,2})\s?[-:_]\s?(\d{1,2})\b", re.I)


def _dim_type(raw: str) -> str:
    r = raw.strip()
    if r[:1] in "Ø⌀":
        return "diameter"
    if r[:1] in "Rr":
        return "radius"
    if r[:1] in "Mm" and not r.lower().startswith("mm"):
        return "thread"
    if "°" in r:
        return "angle"
    if "±" in r:
        return "tolerance"
    return "size" if re.search(r"[x×]", r) else "length"


def _pin_key(s: str) -> str:
    """针脚归一 token：保连接器:针脚结构（CN1-12→cn1:12, CN11-2→cn11:2 不撞），plain 针脚小写。"""
    return re.sub(r"[-_ ]+", ":", (s or "").strip()).lower()


def _enrich(meta: dict, body: str) -> None:
    """给一片补领域精确键（故障码/针脚/连接器/尺寸类型），供混合检索精确命中与元数据过滤。
    一片可能有多个针脚/多种尺寸（未拆行的 kv、含多尺寸的段落），全部收进空格分隔 token，
    让 search_by_field 能精确命中其中任一个（而非只有第一个）。"""
    fc = _FAULT.search(body)
    if fc:
        meta["fault_code"] = fc.group(0).strip()
    et = meta.get("element_type")
    if et in ("kv", "table", "dimension"):
        conns = _CONN_PIN.findall(body)   # [(prefix, num, pin), ...]
        keys, labels = [], []
        for pfx, num, pn in conns:
            conn = (pfx + num).upper()
            keys.append(_pin_key(f"{conn}:{pn}"))   # cn1:12（保结构，不撞 cn11:2）
            labels.append(f"{conn}-{pn}")
        for p in _PIN.findall(body):
            keys.append(_pin_key(p))                 # p3 / u1 / pa0
            labels.append(p)
        if conns:  # 首个连接器-针脚给 connector/pin_number（单针脚常见场景）
            meta["connector"] = (conns[0][0] + conns[0][1]).upper()
            meta["pin_number"] = conns[0][2]
        if keys:
            meta["pin_normalized"] = " ".join(dict.fromkeys(keys))
            meta["pin_label"] = " ".join(dict.fromkeys(labels))
    # 尺寸类型:收该片里所有尺寸的类型（一段多尺寸 → thread/size/angle 都要能被 search_by_field 命中）
    types = sorted({_dim_type(m.group(0)) for m in _DIMENSION.finditer(body)})
    if types:
        meta["dimension_type"] = " ".join(types)


def _row_category(meta: dict) -> str | None:
    """按元素/表子类型定分类，优先于关键词分类——否则参数表里含"防护等级/高压"的行会被
    安全词覆盖成「安全警告」、工序含"断电"会被误判，从而在 search_by_category 里漏掉。
    返回 None 表示走通用 classify（故障码表/针脚表的行内容各异，仍按关键词判）。"""
    if meta.get("element_type") == "procedure":
        return "处理步骤"
    return {"参数表": "参数规格", "备件表": "备件"}.get(meta.get("table_subtype"))


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
            m["category"] = _row_category(meta) or classify(body)
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
                    "按元数据字段精确过滤检索。field 可选:element_type(table/procedure/kv/dimension/prose)、"
                    "fault_code(故障码如 E-42/AL.013)、pin_label(针脚如 P3/PA0)、connector(连接器如 CN1)、"
                    "pin_normalized(归一针脚如 CN1:12)、dimension_type(尺寸类型 diameter/thread/radius/angle)、"
                    "model(设备型号)、table_subtype(故障码表/针脚表/参数表/备件表)。例:field='fault_code',value='E-42'。"
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
