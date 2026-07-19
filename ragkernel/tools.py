"""检索工具集：文档检索 / 读取 / 列表。引用标注 [D<doc>#<chunk> p.<page>]。

单租户 MVP：无 tier/scope/gate。where seam 保留在 search 层（默认空），
供将来多租户注入 tenant_id=? 与垂直层 post_retrieve 使用。
"""

import json

from . import config, embed, rerank, search, store
from .verticals import get_vertical


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
            })

    # ── 工具实现 ──────────────────────────────────────────────

    # 可按 meta_json 字段过滤检索的白名单（垂直层拆片时写入这些键）。
    FILTERABLE = ("category", "element_type", "fault_code", "pin_label", "model", "table_subtype")

    def _search(self, query: str, k: int, where: str = "", params: tuple = ()) -> str:
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
        win = self._restore_win()
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
        return self._search(query, k, f"json_extract(c.meta_json,'$.{field}') = ?", (value,))

    def read_document(self, document_id: int) -> str:
        rows = self.db.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index", (int(document_id),)
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

    # ── 工具注册 ──────────────────────────────────────────────

    def specs_and_handlers(self):
        """返回 (anthropic 工具定义, name→callable)，并入垂直层 extra_tools。"""
        specs = [
            {
                "name": "search_documents",
                "description": "在企业知识库（已上传的文档）中混合检索相关片段。返回内容带 [D<文档>#<块> p.<页> · 分类] 引用标记。可选 category 只在某一分类里检索。",
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
        # 垂直层可追加工具（NullVertical 返回空）
        v_specs, v_handlers = self.vertical.extra_tools(self)
        specs += list(v_specs)
        handlers.update(v_handlers)
        return specs, handlers
