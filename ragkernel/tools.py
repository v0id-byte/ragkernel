"""检索工具集：文档检索 / 读取 / 列表。引用标注 [D<doc>#<chunk> p.<page>]。

单租户 MVP：无 tier/scope/gate。where seam 保留在 search 层（默认空），
供将来多租户注入 tenant_id=? 与垂直层 post_retrieve 使用。
"""

import json
import re

from . import config, embed, rerank, search, store
from .verticals import get_vertical


def _norm_sql(col: str) -> str:
    """SQL 侧把码值归一（去大小写/分隔符）：E-42 / E42 / e42 视为同一。"""
    expr = col
    for ch in ("-", ".", ":", "_", " "):
        expr = f"replace({expr}, '{ch}', '')"
    return f"lower({expr})"


def _norm_key(s: str) -> str:
    return re.sub(r"[-.:_ ]", "", s or "").lower()


def _pin_key(s: str) -> str:
    """针脚归一：保连接器:针脚结构（CN1-12→cn1:12，不撞 CN11-2→cn11:2）。"""
    return re.sub(r"[-_ ]+", ":", (s or "").strip()).lower()


_UID = {"type": "string", "minLength": 1, "maxLength": 1024}  # 稳定 entity_uid，非内部自增 id
_CAD_GEOM_ENUM = [
    "bounding_box", "extents", "volume", "surface_area", "centroid", "center_of_mass",
    "solid_count", "face_count", "edge_count", "face_type_histogram",
    "cylindrical_face_count", "is_watertight", "is_volume", "component_count",
]
_CAD_TOOL_SPECS = [
    {
        "name": "inspect_cad_document",
        "description": "查看某个 CAD 文档（STEP/STL）的总览：格式、单位块（源单位 vs 计算单位）、实体计数、总体包围盒、警告。返回结构化 JSON。非 CAD 文档会明确说明。",
        "input_schema": {"type": "object", "properties": {"document_id": {"type": "integer"}},
                         "required": ["document_id"]},
    },
    {
        "name": "list_cad_entities",
        "description": "列出某 CAD 文档的工程实体（document/assembly/part/component_instance/mesh...）。可按 entity_type 过滤。返回每个实体的 entity_uid/type/name/parent。",
        "input_schema": {"type": "object", "properties": {
            "document_id": {"type": "integer"},
            "entity_type": {"type": "string", "description": "可选：只列某类型"},
            "limit": {"type": "integer"}}, "required": ["document_id"]},
    },
    {
        "name": "get_cad_entity",
        "description": "取某个工程实体的完整信息（属性/几何/溯源）。entity_uid 取自 list_cad_entities / 检索结果，是稳定标识（非内部行号）。",
        "input_schema": {"type": "object", "properties": {
            "document_id": {"type": "integer"}, "entity_uid": _UID}, "required": ["document_id", "entity_uid"]},
    },
    {
        "name": "get_assembly_tree",
        "description": "取某 CAD 文档的装配树（document→assembly→component_instance→原型 part 的嵌套结构，含各实例世界包围盒）。",
        "input_schema": {"type": "object", "properties": {"document_id": {"type": "integer"}},
                         "required": ["document_id"]},
    },
    {
        "name": "query_geometry",
        "description": "查询某实体的指定几何属性（仅限白名单：bounding_box/extents/volume/surface_area/centroid/center_of_mass/solid_count/face_count/edge_count/face_type_histogram/cylindrical_face_count/is_watertight/is_volume/component_count）。每个值带 value/unit/来源方法/表示/质量/有效性——据此区分精确 vs 近似。注意：无 hole_count（本 MVP 不做孔识别）。",
        "input_schema": {"type": "object", "properties": {
            "document_id": {"type": "integer"}, "entity_uid": _UID,
            "properties": {"type": "array", "items": {"type": "string", "enum": _CAD_GEOM_ENUM}}},
            "required": ["document_id", "entity_uid"]},
    },
    {
        "name": "compare_cad_entities",
        "description": "并排比较多个实体的几何属性（同一属性白名单）。用于「哪个零件体积最大」等问题。",
        "input_schema": {"type": "object", "properties": {
            "document_id": {"type": "integer"},
            "entity_uids": {"type": "array", "items": _UID},
            "properties": {"type": "array", "items": {"type": "string", "enum": _CAD_GEOM_ENUM}}},
            "required": ["document_id", "entity_uids"]},
    },
    {
        "name": "search_engineering_objects",
        "description": "在所有 CAD 文档的工程对象中混合检索（可按 entity_type 过滤）。返回精简匹配（document_id/entity_uid/chunk_id/引用/摘要）；细节再用 get_cad_entity 取。",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "entity_type": {"type": "string", "description": "可选：part/assembly/mesh/component_instance..."},
            "k": {"type": "integer"}}, "required": ["query"]},
    },
]


class Toolbox:
    """一个会话一个实例：持有单库连接、touched 引用轨迹、当前垂直层。"""

    def __init__(self, db=None, audit=None):
        self.db = db or store.connect()
        self.audit = audit or (lambda kind, payload: None)
        self.vertical = get_vertical()
        self.touched: list[dict] = []  # 本轮触达的引用，供前端引用面板
        self.current_question = ""
        self._fname: dict[int, str] = {}

    # ── 检索配置：reranker 与候选池 ─────────────────────────────

    def _reranker(self):
        rr = (config.settings().get("retrieval") or {}).get("rerank") or {}
        if not rr.get("enabled"):
            return None
        return rerank.get(rr.get("model", "BAAI/bge-reranker-v2-m3"))

    def _candidates(self) -> int:
        rr = (config.settings().get("retrieval") or {}).get("rerank") or {}
        return int(rr.get("candidates", 40))

    def _restore_win(self) -> int:
        return int((config.settings().get("retrieval") or {}).get("restore_window", 1))

    def _filename(self, doc_id: int) -> str:
        if doc_id not in self._fname:
            row = self.db.execute("SELECT filename FROM documents WHERE id=?", (doc_id,)).fetchone()
            self._fname[doc_id] = row["filename"] if row else "?"
        return self._fname[doc_id]

    def _category(self, r):
        try:
            mj = r["meta_json"]
        except (IndexError, KeyError):
            return None
        if not mj:
            return None
        try:
            return json.loads(mj).get("category")
        except Exception:
            return None

    def _cite(self, r) -> str:
        page = f" p.{r['page_no']}" if r["page_no"] else ""
        title = f" · {r['title']}" if r["title"] else ""
        cat = self._category(r)
        cat_s = f" · {cat}" if cat else ""
        return f"[D{r['document_id']}#{r['id']}{page}] {self._filename(r['document_id'])}{title}{cat_s}"

    def _restore_window(self, document_id: int, chunk_index: int, window: int) -> str:
        """细检索 + 粗还原：命中块后补回同文档相邻 chunk 的正文，还原上下文。"""
        rows = self.db.execute(
            "SELECT text FROM chunks WHERE document_id=? AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index",
            (document_id, chunk_index - window, chunk_index + window),
        ).fetchall()
        return "\n".join(r["text"] for r in rows)

    def _track(self, rows) -> None:
        for r in rows:
            self.touched.append({
                "ref": f"D{r['document_id']}#{r['id']}",
                "document_id": r["document_id"],
                "chunk_id": r["id"],
                "filename": self._filename(r["document_id"]),
                "page": r["page_no"],
                "category": self._category(r),
                "snippet": (r["text"] or "")[:200],   # 前端引用悬浮预览用
            })

    # ── 工具实现 ──────────────────────────────────────────────

    # 可按 meta_json 字段过滤检索的白名单（垂直层拆片时写入这些键）。
    FILTERABLE = (
        "category", "element_type", "fault_code", "pin_label", "pin_normalized",
        "connector", "model", "table_subtype", "dimension_type",
        "cad_entity_type", "cad_format",   # 原生 CAD：按实体类型/格式过滤检索
    )

    # query_geometry 允许查询的几何属性白名单（禁止任意 key 拼进 SQL/JSON path）。
    CAD_GEOM_PROPS = (
        "bounding_box", "extents", "volume", "surface_area", "centroid", "center_of_mass",
        "solid_count", "face_count", "edge_count", "face_type_histogram",
        "cylindrical_face_count", "is_watertight", "is_volume", "component_count",
    )

    def _search(self, query: str, k: int, where: str = "", params: tuple = (), restore: bool = True) -> str:
        qvec = embed.embed([query])[0]
        rows = search.hybrid_search(
            self.db, query, qvec, k=k, where=where, params=params,
            reranker=self._reranker(), candidates=self._candidates(),
        )
        rows = self.vertical.post_retrieve(query, rows)  # 垂直层检索后 hook
        self._track(rows)
        self.audit("tool:search", {"query": query, "where": where, "hits": [r["id"] for r in rows]})
        if not rows:
            return "（无结果）"
        win = self._restore_win() if restore else 0
        parts = []
        for r in rows:
            body = self._restore_window(r["document_id"], r["chunk_index"], win) if win > 0 else r["text"]
            parts.append(self._cite(r) + "\n" + (body or r["text"]))
        return "\n────\n".join(parts)

    def search_documents(self, query: str, k: int = 8, category: str | None = None) -> str:
        where, params = ("", ())
        if category:
            where, params = "json_extract(c.meta_json,'$.category') = ?", (category,)
        return self._search(query, k, where, params)

    def search_by_meta(self, query: str, field: str, value: str, k: int = 8) -> str:
        """只在某个 meta 字段=某值的片里检索（如 field='fault_code' value='E-42'）。"""
        if field not in self.FILTERABLE:
            return f"（不支持的过滤字段 {field}；可用：{', '.join(self.FILTERABLE)}）"
        col = f"json_extract(c.meta_json,'$.{field}')"
        # 字段过滤=精确定位那一条 → 一律关掉 restore_window，避免把邻近的 E42/E-420 行拉进同一引用。
        # 多值 token 字段（一片多针脚/多尺寸）：按 token 边界匹配。
        if field in ("pin_label", "pin_normalized"):
            pcol = "json_extract(c.meta_json,'$.pin_normalized')"
            return self._search(query, k, f"(' '||lower({pcol})||' ') LIKE ?", (f"% {_pin_key(value)} %",), restore=False)
        if field == "dimension_type":
            return self._search(query, k, f"(' '||lower({col})||' ') LIKE ?", (f"% {value.strip().lower()} %",), restore=False)
        # 码值字段：先原样精确（E-42 只配 E-42，不误配同库里 distinct 的 E42），无命中再归一回退（容 E42 找 E-42）。
        if field in ("fault_code", "connector"):
            exact = self._search(query, k, f"{col} = ?", (value,), restore=False)
            return exact if exact != "（无结果）" else self._search(
                query, k, f"{_norm_sql(col)} = ?", (_norm_key(value),), restore=False
            )
        return self._search(query, k, f"{col} = ?", (value,), restore=False)

    def collect_evidence(self, claim: str, document_ids: list[int] | None = None,
                         model: str | None = None, top_k: int = 8,
                         max_chars: int = 24000) -> list[dict]:
        """给 verify 收集**有范围过滤的原子证据**（绝不整篇 read_document 灌进 LLM）。
        document_ids/model 只作检索范围过滤（走 hybrid_search 的 where seam，两路都过滤）。
        返回 [{"id":"E1","ref":"[D..#.. p.. · 分类]","text":...}]，各带真实引用；总量截到 max_chars。"""
        clauses, ps = [], []
        if document_ids:
            ph = ",".join("?" * len(document_ids))
            clauses.append(f"c.document_id IN ({ph})")
            ps.extend(int(d) for d in document_ids)
        if model:
            clauses.append("json_extract(c.meta_json,'$.model') = ?")
            ps.append(model)
        where = " AND ".join(clauses)
        qvec = embed.embed([claim])[0]
        rows = search.hybrid_search(
            self.db, claim, qvec, k=top_k, where=where, params=tuple(ps),
            reranker=self._reranker(), candidates=self._candidates(),
        )
        rows = self.vertical.post_retrieve(claim, rows)
        self._track(rows)
        self.audit("tool:collect_evidence", {"claim": claim, "where": where, "hits": [r["id"] for r in rows]})
        out, used = [], 0
        for i, r in enumerate(rows, 1):
            text = r["text"] or ""
            if used + len(text) > max_chars:
                text = text[: max(0, max_chars - used)]
            if not text:
                break
            out.append({"id": f"E{i}", "ref": self._cite(r), "text": text})
            used += len(text)
        return out

    def reset_call_state(self) -> None:
        """一次工具调用结束后清引用轨迹。MCP 长生命周期 session 复用同一 Toolbox，
        不清 touched 会无限增长（Web 侧本来就每问一轮后 clear，此处给个正式入口）。"""
        self.touched.clear()

    def read_document(self, document_id: int) -> str:
        # Retrieval invariant：本函数直接读 chunks，绕开了 search._ACTIVE，故自己挡一次。
        # 写成单一路径（把未归档条件并进 WHERE），不分"不存在直接返回 / 已归档另走一支"——
        # 两条分支迟早会在返回文案上分叉，变成一个存在性预言机。
        rows = self.db.execute(
            "SELECT c.* FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.document_id=? AND d.archived_at IS NULL ORDER BY c.chunk_index",
            (int(document_id),),
        ).fetchall()
        self._track(rows)
        self.audit("tool:read_document", {"document_id": document_id, "n": len(rows)})
        if not rows:
            return "（没有这个文档，或它还没被索引）"
        return "\n\n".join(self._cite(r) + "\n" + r["text"] for r in rows)

    def list_documents(self) -> str:
        docs = store.list_documents(self.db)
        self.audit("tool:list_documents", {"n": len(docs)})
        if not docs:
            return "（知识库为空——先上传文档）"
        return "\n".join(
            f"D{d['id']} · {d['filename']} · {d['chunks']} 块"
            + (f" · {d['page_count']} 页" if d["page_count"] else "")
            + f" · {d['status']}"
            for d in docs
        )

    # ── 原生 CAD 工具（结构化 JSON；按 entity_uid 寻址，非内部自增 id）──────────

    @staticmethod
    def _parse_entity(row: dict) -> dict:
        def j(k):
            v = row.get(k)
            return json.loads(v) if v else None
        return {
            "entity_uid": row["entity_uid"],
            "entity_type": row["entity_type"],
            "name": row["name"],
            "parent_uid": row["parent_uid"],
            "prototype_uid": row["prototype_uid"],
            "assembly_path": j("assembly_path_json") or [],
            "properties": j("properties_json") or {},
            "geometry": j("geometry_json") or {},
            "provenance": j("provenance_json") or {},
            "confidence": row["confidence"],
        }

    def _resolve_geom(self, ent: dict, key: str):
        """把白名单属性映射到实体的 properties/geometry；找不到返回 {'available': False}。"""
        p, g = ent["properties"], ent["geometry"]
        if key == "bounding_box":
            for src, k in ((p, "local_bounding_box"), (g, "world_bounding_box"),
                           (g, "overall_world_bounding_box"), (p, "bounding_box"),
                           (p, "overall_bounding_box")):
                if k in src:
                    return src[k]
        elif key == "volume":
            return p.get("volume") or g.get("instance_summed_volume") or {"available": False}
        elif key in ("extents", "surface_area", "centroid"):
            return p.get(key, {"available": False})
        elif key == "center_of_mass":
            return p.get("center_of_mass", {"available": False, "note": "not computed in MVP"})
        elif key == "solid_count":
            return g.get("solids", {"available": False})
        elif key == "face_count":
            return g.get("faces", {"available": False})
        elif key == "edge_count":
            return g.get("edges", {"available": False})
        elif key in ("face_type_histogram", "cylindrical_face_count", "is_watertight", "is_volume"):
            return g.get(key, {"available": False})
        elif key == "component_count":
            # STL：顶点连通体/面连通组件；STEP：组件实例数/零件原型数——按存在项返回，别只给 STL 键
            avail = {k: g[k] for k in (
                "vertex_connected_body_count", "face_connected_component_count",
                "component_instance_count", "part_prototype_count") if g.get(k) is not None}
            return avail or {"available": False}
        return {"available": False}

    def inspect_cad_document(self, document_id: int) -> str:
        ents = store.get_engineering_entities(self.db, int(document_id))
        if not ents:
            return json.dumps({"error": "该文档不是 CAD 文档，或未含结构化工程实体。",
                               "document_id": document_id}, ensure_ascii=False)
        by_type: dict = {}
        for e in ents:
            by_type[e["entity_type"]] = by_type.get(e["entity_type"], 0) + 1
        doc_ent = next((self._parse_entity(e) for e in ents if e["entity_type"] == "document"), None)
        row = self.db.execute("SELECT filename, status, mime FROM documents WHERE id=?",
                              (int(document_id),)).fetchone()
        out = {
            "document_id": int(document_id),
            "filename": row["filename"] if row else None,
            "status": row["status"] if row else None,
            "format": doc_ent["provenance"].get("source_format") if doc_ent else None,
            "entity_counts": by_type,
            "units": (doc_ent["geometry"].get("unit_block") if doc_ent else None),
            "geometry": doc_ent["geometry"] if doc_ent else None,
            "properties": doc_ent["properties"] if doc_ent else None,
            "warnings": doc_ent["provenance"].get("warnings") if doc_ent else None,
        }
        self.audit("tool:inspect_cad_document", {"document_id": document_id})
        return json.dumps(out, ensure_ascii=False)

    def list_cad_entities(self, document_id: int, entity_type: str | None = None, limit: int = 200) -> str:
        ents = store.get_engineering_entities(self.db, int(document_id), entity_type)
        items = [{"entity_uid": e["entity_uid"], "entity_type": e["entity_type"], "name": e["name"],
                  "parent_uid": e["parent_uid"], "prototype_uid": e["prototype_uid"]}
                 for e in ents[: int(limit)]]
        self.audit("tool:list_cad_entities", {"document_id": document_id, "type": entity_type, "n": len(items)})
        return json.dumps({"document_id": int(document_id), "total": len(ents), "entities": items},
                          ensure_ascii=False)

    def get_cad_entity(self, document_id: int, entity_uid: str) -> str:
        row = store.get_engineering_entity(self.db, int(document_id), entity_uid)
        if not row:
            return json.dumps({"error": "未找到该实体", "document_id": int(document_id),
                               "entity_uid": entity_uid}, ensure_ascii=False)
        self.audit("tool:get_cad_entity", {"document_id": document_id, "entity_uid": entity_uid})
        return json.dumps(self._parse_entity(row), ensure_ascii=False)

    def get_assembly_tree(self, document_id: int) -> str:
        ents = [self._parse_entity(e) for e in store.get_engineering_entities(self.db, int(document_id))]
        if not ents:
            return json.dumps({"error": "该文档无结构化工程实体", "document_id": int(document_id)},
                              ensure_ascii=False)
        by_parent: dict = {}
        for e in ents:
            by_parent.setdefault(e["parent_uid"], []).append(e)

        def node(e):
            children = by_parent.get(e["entity_uid"], [])
            n = {"entity_uid": e["entity_uid"], "entity_type": e["entity_type"], "name": e["name"]}
            if e["prototype_uid"]:
                n["prototype_uid"] = e["prototype_uid"]
            wb = e["geometry"].get("world_bounding_box") or e["geometry"].get("overall_world_bounding_box")
            if wb:
                n["world_bounding_box"] = wb
            if children:
                n["children"] = [node(c) for c in children]
            return n

        roots = by_parent.get(None, []) + by_parent.get("document", [])
        # 以 document 为根；document 的孩子挂 parent_uid=="document"
        tree = [node(e) for e in ents if e["entity_type"] == "document"]
        self.audit("tool:get_assembly_tree", {"document_id": document_id})
        return json.dumps({"document_id": int(document_id), "tree": tree or roots}, ensure_ascii=False)

    def query_geometry(self, document_id: int, entity_uid: str, properties: list | None = None) -> str:
        row = store.get_engineering_entity(self.db, int(document_id), entity_uid)
        if not row:
            return json.dumps({"error": "未找到该实体", "entity_uid": entity_uid}, ensure_ascii=False)
        ent = self._parse_entity(row)
        req = properties or list(self.CAD_GEOM_PROPS)
        bad = [k for k in req if k not in self.CAD_GEOM_PROPS]
        if bad:
            return json.dumps({"error": f"不支持的几何属性：{bad}",
                               "allowed": list(self.CAD_GEOM_PROPS)}, ensure_ascii=False)
        results = {k: self._resolve_geom(ent, k) for k in req}
        self.audit("tool:query_geometry", {"document_id": document_id, "entity_uid": entity_uid,
                                            "properties": req})
        return json.dumps({"document_id": int(document_id), "entity_uid": entity_uid,
                           "entity_type": ent["entity_type"], "results": results}, ensure_ascii=False)

    def compare_cad_entities(self, document_id: int, entity_uids: list, properties: list | None = None) -> str:
        req = properties or ["bounding_box", "volume", "surface_area"]
        bad = [k for k in req if k not in self.CAD_GEOM_PROPS]
        if bad:
            return json.dumps({"error": f"不支持的几何属性：{bad}",
                               "allowed": list(self.CAD_GEOM_PROPS)}, ensure_ascii=False)
        cols = []
        for uid in (entity_uids or []):
            row = store.get_engineering_entity(self.db, int(document_id), uid)
            if not row:
                cols.append({"entity_uid": uid, "error": "未找到"})
                continue
            ent = self._parse_entity(row)
            cols.append({"entity_uid": uid, "name": ent["name"], "entity_type": ent["entity_type"],
                         "values": {k: self._resolve_geom(ent, k) for k in req}})
        self.audit("tool:compare_cad_entities", {"document_id": document_id, "n": len(cols)})
        return json.dumps({"document_id": int(document_id), "properties": req, "entities": cols},
                          ensure_ascii=False)

    def search_engineering_objects(self, query: str, entity_type: str | None = None, k: int = 8) -> str:
        # 始终限定 CAD 片（element_type='cad'）——否则普通文档片也会命中、entity_uid 为 null、无法接续 get_cad_entity。
        clauses = ["json_extract(c.meta_json,'$.element_type') = 'cad'"]
        params: list = []
        if entity_type:
            clauses.append("json_extract(c.meta_json,'$.cad_entity_type') = ?")
            params.append(entity_type)
        where = " AND ".join(clauses)
        qvec = embed.embed([query])[0]
        rows = search.hybrid_search(self.db, query, qvec, k=k, where=where, params=tuple(params),
                                    reranker=self._reranker(), candidates=self._candidates())
        self._track(rows)
        matches = []
        for rank, r in enumerate(rows, 1):
            try:
                mj = json.loads(r["meta_json"]) if r["meta_json"] else {}
            except Exception:
                mj = {}
            summary = (r["text"] or "").splitlines()
            matches.append({
                "rank": rank,
                "document_id": r["document_id"],
                "chunk_id": r["id"],
                "entity_uid": mj.get("cad_entity_uid"),
                "cad_entity_type": mj.get("cad_entity_type"),
                "citation": self._cite(r),
                "summary": summary[0] if summary else "",
            })
        self.audit("tool:search_engineering_objects", {"query": query, "type": entity_type, "n": len(matches)})
        return json.dumps({"query": query, "matches": matches}, ensure_ascii=False)

    # ── 工具注册 ──────────────────────────────────────────────

    def specs_and_handlers(self):
        """返回 (anthropic 工具定义, name→callable)，并入垂直层 extra_tools。"""
        specs = [
            {
                "name": "search_documents",
                "description": "在企业知识库（已上传的文档）中混合检索相关片段。返回内容带 [D<文档>#<块> p.<页>] 引用标记，其后附文件名 · 标题 · 分类。可选 category 只在某一分类里检索。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词"},
                        "k": {"type": "integer", "description": "返回条数，默认 8"},
                        "category": {"type": "string", "description": "可选：限定分类（取自 list_categories）"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "read_document",
                "description": "按顺序读取某个文档的全部内容。document_id 取自检索结果的 D<id>。",
                "input_schema": {
                    "type": "object",
                    "properties": {"document_id": {"type": "integer"}},
                    "required": ["document_id"],
                },
            },
            {
                "name": "list_documents",
                "description": "列出知识库里现有的所有文档（文件名、块数、页数、状态）。",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        handlers = {
            "search_documents": self.search_documents,
            "read_document": self.read_document,
            "list_documents": self.list_documents,
        }
        # 原生 CAD 工具（核心 Toolbox，恒在；无 CAD 文档时优雅返回）。均以稳定 entity_uid 寻址。
        specs += _CAD_TOOL_SPECS
        handlers.update({
            "inspect_cad_document": self.inspect_cad_document,
            "list_cad_entities": self.list_cad_entities,
            "get_cad_entity": self.get_cad_entity,
            "get_assembly_tree": self.get_assembly_tree,
            "query_geometry": self.query_geometry,
            "compare_cad_entities": self.compare_cad_entities,
            "search_engineering_objects": self.search_engineering_objects,
        })
        # 垂直层可追加工具（NullVertical 返回空）
        v_specs, v_handlers = self.vertical.extra_tools(self)
        specs += list(v_specs)
        handlers.update(v_handlers)
        return specs, handlers
