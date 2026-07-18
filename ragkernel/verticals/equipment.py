"""设备维修 / 售后故障知识库垂直层。

拆片:手册按故障条目/字段标记细分(不合并),每片按内容打分类;工单/反馈整条一片(故障案例)。
分类:故障现象 / 故障原因 / 处理步骤 / 安全警告 / 参数规格 / 保养维护 / 备件 / 故障案例 / 其他。
换行业只改本文件(正则 + 关键词表 + prompt),内核一行不动。
"""

import re

from . import register

# 条目/字段边界:markdown 标题、故障代码、报警码、字段标签、编号列表
_ENTRY = re.compile(
    r"^(?:"
    r"#{1,6}\s+.+"
    r"|故障代码[:：]?\s*\S.*"
    r"|(?:报警|错误|故障)[码代号].*"
    r"|E-?\d+\b.*"
    r"|(?:现象|症状|原因|处理|措施|方法|步骤|解决|操作|警告|注意|备注)[:：].*"
    r"|\d+[.、]\s*\S.*"
    r"|[一二三四五六七八九十]+[、.]\s*\S.*"
    r")$",
    re.M,
)

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
            "涉及具体型号时,优先引用同型号设备的既往处理记录(故障案例)。"
        )

    def split(self, page_text: str, page_no: int | None):
        text = (page_text or "").strip()
        if not text:
            return []
        marks = list(_ENTRY.finditer(text))
        if not marks:
            paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            if not paras:
                return None
            return [(self._title(p), p, {"category": classify(p), "by": "rule"}) for p in paras]
        pieces: list[tuple[str, str]] = []
        head = text[: marks[0].start()].strip()
        if head:
            pieces.append(("", head))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.start() : end].strip()
            title = re.sub(r"^#{1,6}\s+", "", m.group(0)).strip()[:60]
            pieces.append((title, body))
        return [(t, b, {"category": classify(b), "by": "rule"}) for t, b in pieces if b]

    def classify(self, text: str) -> str:
        return classify(text)

    def _title(self, body: str) -> str:
        return body.splitlines()[0].strip()[:60] if body else ""

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
        ]
        return specs, {"list_categories": list_categories, "search_by_category": search_by_category}

    def post_retrieve(self, query: str, chunks: list) -> list:
        return chunks

    def on_ingest(self, doc: dict, chunks: list[dict]) -> list[dict]:
        return chunks


register("equipment", Equipment)
