"""SQLite 存储：单库、文档中心。FTS5(jieba) + sqlite-vec。

documents（一行一个文件）+ chunks（一行一个分块）。幂等按 document_id：
重摄取某文档时，本次未出现的旧 chunk（含其向量）删除。删文档级联删 chunks_vec→chunks→documents。
connect() 保持函数形态——将来多租户按 tenant 参数化库路径的接缝。
"""

import hashlib
import sqlite3
import time

import sqlite_vec

from . import config

EMBED_DIM = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  source_path TEXT,
  mime TEXT,
  sha256 TEXT UNIQUE NOT NULL,
  page_count INTEGER,
  status TEXT DEFAULT 'pending',           -- 摄取状态（机器的事实）：pending|chunked|embedded|rejected|embedding_failed
  source_kind TEXT DEFAULT 'upload',       -- upload | ticket_import | feedback
  meta_json TEXT,
  created_at INTEGER,
  owner_id INTEGER,                        -- auth.db users.id；NULL = 历史遗留/脚本导入，仅管理员可处置
  archived_at INTEGER                      -- 可用状态（人的决策）：NULL = 在架；非 NULL = 已归档（退出检索、数据保留）
);
CREATE TABLE IF NOT EXISTS ingestion_log(
  id INTEGER PRIMARY KEY,
  document_id INTEGER,
  filename TEXT,
  action TEXT,                              -- ingested | skipped | feedback
  chunks INTEGER, added INTEGER, removed INTEGER,
  ms INTEGER, ts INTEGER
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  title TEXT,
  page_no INTEGER,
  text TEXT NOT NULL,
  text_seg TEXT NOT NULL,
  meta_json TEXT,
  content_hash TEXT UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id, chunk_index);
-- 原生 CAD 结构化工程层（与检索用 chunks 并存、双层表示）。免迁移：IF NOT EXISTS，旧库升级即得空表。
-- 数值属性存 MeasuredValue（value/unit/source_method/representation/quality/validity）于 properties_json。
-- provenance_json 只存 文件名/格式/sha/parser，绝不存服务端绝对路径。装配拓扑用 parent_uid + prototype_uid。
CREATE TABLE IF NOT EXISTS engineering_entities(
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  entity_uid TEXT NOT NULL,            -- 稳定标识（occurrence uid / prototype 的 XDE label tag 路径），跨重摄取可解释
  entity_type TEXT NOT NULL,           -- document|assembly|component_instance|part|body|solid|shell|face|edge|mesh|material|layer
  parent_uid TEXT,
  prototype_uid TEXT,                  -- component_instance -> 其 part 原型
  name TEXT,
  assembly_path_json TEXT,
  properties_json TEXT,                -- MeasuredValue 字典集合
  geometry_json TEXT,                  -- bbox(local/world)/counts/面类型直方图/location_matrix
  provenance_json TEXT,                -- 文件名/格式/sha/parser/warnings —— 无绝对路径
  confidence TEXT,
  UNIQUE(document_id, entity_uid)
);
CREATE INDEX IF NOT EXISTS idx_eng_doc    ON engineering_entities(document_id, entity_type);
CREATE INDEX IF NOT EXISTS idx_eng_parent ON engineering_entities(document_id, parent_uid);
CREATE INDEX IF NOT EXISTS idx_eng_proto  ON engineering_entities(document_id, prototype_uid);
CREATE INDEX IF NOT EXISTS idx_eng_uid    ON engineering_entities(document_id, entity_uid);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text_seg, content='chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text_seg) VALUES (new.id, new.text_seg);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text_seg) VALUES('delete', old.id, old.text_seg);
END;
"""


def _migrate(db: sqlite3.Connection) -> None:
    """给旧库补 documents 的 owner_id / archived_at 两列。两列都可空，ALTER 即可，不必重建表。
    与 auth._migrate 同一路子：靠 PRAGMA 判断，幂等，每次 connect 只花一次 PRAGMA 的钱。"""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(documents)")}
    if "owner_id" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN owner_id INTEGER")
    if "archived_at" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN archived_at INTEGER")
    db.commit()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "ragkernel.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")  # 上传写入时会话仍可并发读
    db.execute("PRAGMA busy_timeout=5000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA)
    _migrate(db)
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING "
        f"vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}] distance_metric=cosine)"
    )
    return db


def content_hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"\x00")
    return h.hexdigest()


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def get_document(db: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM documents WHERE sha256=?", (sha256,)).fetchone()


def get_document_by_id(db: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()


def set_archived(db: sqlite3.Connection, document_id: int, ts: int | None) -> None:
    """置/清可用状态。ts=None 即恢复上架。"""
    db.execute("UPDATE documents SET archived_at=? WHERE id=?", (ts, document_id))
    db.commit()


def set_owner(db: sqlite3.Connection, document_id: int, owner_id: int) -> None:
    """认领无主文档。带 `AND owner_id IS NULL` 兜底——见 Ownership invariant，有主的绝不改写。"""
    db.execute("UPDATE documents SET owner_id=? WHERE id=? AND owner_id IS NULL", (owner_id, document_id))
    db.commit()


def _upsert_document_tx(
    db: sqlite3.Connection, filename: str, sha256: str, source_path: str = "",
    mime: str = "", page_count: int = 0, meta_json: str = "", source_kind: str = "upload",
    owner_id: int | None = None,
) -> tuple[int, bool]:
    """upsert_document 的**不提交**版本，供同一事务内组合（CAD 原子摄取）。返回 (document_id, existed)。"""
    row = get_document(db, sha256)
    if row:
        # Ownership invariant：owner 只允许 NULL → 具体值的回填，绝不被后续摄取覆盖。
        # 无主文档（CLI/watch/脚本导入的历史数据）可被首个上传者认领；已有主的一律原样保留。
        if owner_id and row["owner_id"] is None:
            db.execute("UPDATE documents SET owner_id=? WHERE id=? AND owner_id IS NULL", (owner_id, row["id"]))
        return row["id"], True
    cur = db.execute(
        "INSERT INTO documents(filename,source_path,mime,sha256,page_count,status,source_kind,meta_json,created_at,owner_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (filename, source_path or None, mime or None, sha256, page_count or None,
         "pending", source_kind, meta_json or None, int(time.time()), owner_id),
    )
    return cur.lastrowid, False


def upsert_document(
    db: sqlite3.Connection, filename: str, sha256: str, source_path: str = "",
    mime: str = "", page_count: int = 0, meta_json: str = "", source_kind: str = "upload",
    owner_id: int | None = None,
) -> tuple[int, bool]:
    """按 sha256 幂等。返回 (document_id, existed)。"""
    with db:  # 提交/回滚由上下文管理器负责
        return _upsert_document_tx(db, filename, sha256, source_path, mime, page_count,
                                   meta_json, source_kind, owner_id)


def log_ingestion(db, document_id, filename, action, chunks=0, added=0, removed=0, ms=0):
    db.execute(
        "INSERT INTO ingestion_log(document_id,filename,action,chunks,added,removed,ms,ts) VALUES(?,?,?,?,?,?,?,?)",
        (document_id, filename, action, chunks, added, removed, ms, int(time.time())),
    )
    db.commit()


def ingestion_history(db, limit: int = 50) -> list[dict]:
    rows = db.execute(
        "SELECT document_id, filename, action, chunks, ms, ts FROM ingestion_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def category_counts(db) -> list[dict]:
    rows = db.execute(
        "SELECT json_extract(meta_json,'$.category') cat, COUNT(*) n FROM chunks "
        "WHERE meta_json IS NOT NULL GROUP BY cat ORDER BY n DESC"
    ).fetchall()
    return [{"category": r["cat"] or "未分类", "n": r["n"]} for r in rows]


def set_status(db: sqlite3.Connection, document_id: int, status: str) -> None:
    db.execute("UPDATE documents SET status=? WHERE id=?", (status, document_id))
    db.commit()


def _set_status_tx(db: sqlite3.Connection, document_id: int, status: str) -> None:
    """set_status 的不提交版本，供同一事务内使用。"""
    db.execute("UPDATE documents SET status=? WHERE id=?", (status, document_id))


# ── 原生 CAD：单次解析 + 原子写入（文档/chunks/工程实体同一事务，要么全落要么全不落）────────────

def _replace_chunks_tx(db: sqlite3.Connection, document_id: int, chunks: list[dict]) -> tuple[int, int]:
    """整文档替换式写 chunk（先删旧含向量，再插新）——**不提交、无 DDL/临时表**，可安全回滚。
    仅供 CAD 原子路径；非 CAD 路径仍用带 stale-GC 的 upsert_chunks。返回 (新增, 删除)。"""
    old = [r["id"] for r in db.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
    for cid in old:
        db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
    db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))  # 触发 chunks_ad 同步 FTS
    for c in chunks:
        db.execute(
            """INSERT INTO chunks(document_id,chunk_index,title,page_no,text,text_seg,meta_json,content_hash)
               VALUES(:document_id,:chunk_index,:title,:page_no,:text,:text_seg,:meta_json,:content_hash)""",
            c,
        )
    return len(chunks), len(old)


def _replace_engineering_entities_tx(db: sqlite3.Connection, document_id: int, rows: list[dict]) -> int:
    """整文档替换式写工程实体——不提交。rows 的 *_json 字段应已是字符串（由 cad/normalize 生成）。返回写入行数。"""
    db.execute("DELETE FROM engineering_entities WHERE document_id=?", (document_id,))
    for r in rows:
        db.execute(
            """INSERT INTO engineering_entities(
                 document_id,entity_uid,entity_type,parent_uid,prototype_uid,name,
                 assembly_path_json,properties_json,geometry_json,provenance_json,confidence)
               VALUES(:document_id,:entity_uid,:entity_type,:parent_uid,:prototype_uid,:name,
                 :assembly_path_json,:properties_json,:geometry_json,:provenance_json,:confidence)""",
            {"parent_uid": None, "prototype_uid": None, "name": None,
             "assembly_path_json": None, "properties_json": None, "geometry_json": None,
             "provenance_json": None, "confidence": None, **r, "document_id": document_id},
        )
    return len(rows)


def upsert_engineering_entities(db: sqlite3.Connection, document_id: int, rows: list[dict]) -> int:
    """公开入口：单独（自带事务）替换某文档的工程实体。"""
    with db:
        return _replace_engineering_entities_tx(db, document_id, rows)


def ingest_cad_atomic(db: sqlite3.Connection, doc_fields: dict, build_chunks, entities: list[dict],
                      status: str = "chunked") -> dict:
    """CAD 原子摄取：文档 upsert + chunks 替换 + 工程实体替换 + 置状态，全部在**一个事务**内。
    build_chunks(doc_id)->list[dict] 延迟到拿到 doc_id 后再构造 chunk（chunk 需 document_id/content_hash）。
    任一步抛异常 → `with db:` 整体回滚，绝不留下「有 chunk 无实体却显示成功」。embedding 在本函数之外、事务提交后做。"""
    with db:
        doc_id, existed = _upsert_document_tx(db, **doc_fields)
        chunks = build_chunks(doc_id)
        added, removed = _replace_chunks_tx(db, doc_id, chunks)
        n_ent = _replace_engineering_entities_tx(db, doc_id, entities)
        _set_status_tx(db, doc_id, status)
    return {"document_id": doc_id, "existed": existed, "chunks": len(chunks),
            "added": added, "removed": removed, "entities": n_ent}


# Retrieval invariant（结构化侧）：CAD 工程实体走的是与文本检索完全不同的通路——不经 hybrid_search，
# 因而不受 search._ACTIVE 保护。六个 CAD 工具全部收口在下面这两个读函数上，故过滤只需写这一次。
_ACTIVE_DOC = "document_id IN (SELECT id FROM documents WHERE archived_at IS NULL)"


def get_engineering_entities(db: sqlite3.Connection, document_id: int,
                             entity_type: str | None = None) -> list[dict]:
    if entity_type:
        rows = db.execute(
            f"SELECT * FROM engineering_entities WHERE document_id=? AND entity_type=? AND {_ACTIVE_DOC} ORDER BY id",
            (document_id, entity_type),
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT * FROM engineering_entities WHERE document_id=? AND {_ACTIVE_DOC} ORDER BY id",
            (document_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_engineering_entity(db: sqlite3.Connection, document_id: int, entity_uid: str) -> dict | None:
    row = db.execute(
        f"SELECT * FROM engineering_entities WHERE document_id=? AND entity_uid=? AND {_ACTIVE_DOC}",
        (document_id, entity_uid),
    ).fetchone()
    return dict(row) if row else None


def upsert_chunks(db: sqlite3.Connection, document_id: int, chunks: list[dict]) -> tuple[int, int]:
    """幂等写入一个文档的全量 chunk；本次未出现的旧 chunk（含其向量）删除。返回 (新增, 删除)。"""
    db.execute("CREATE TEMP TABLE IF NOT EXISTS seen(hash TEXT PRIMARY KEY)")
    db.execute("DELETE FROM seen")
    added = 0
    for c in chunks:
        db.execute("INSERT OR IGNORE INTO seen(hash) VALUES(?)", (c["content_hash"],))
        cur = db.execute(
            """INSERT INTO chunks(document_id,chunk_index,title,page_no,text,text_seg,meta_json,content_hash)
               VALUES(:document_id,:chunk_index,:title,:page_no,:text,:text_seg,:meta_json,:content_hash)
               ON CONFLICT(content_hash) DO NOTHING""",
            c,
        )
        added += cur.rowcount if cur.rowcount > 0 else 0
    stale = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM chunks WHERE document_id=? AND content_hash NOT IN (SELECT hash FROM seen)",
            (document_id,),
        )
    ]
    for cid in stale:
        db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
        db.execute("DELETE FROM chunks WHERE id=?", (cid,))
    db.commit()
    return added, len(stale)


def delete_document(db: sqlite3.Connection, document_id: int) -> int:
    """级联删除：chunks_vec → chunks → engineering_entities → documents。返回删掉的 chunk 数。"""
    cids = [r["id"] for r in db.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
    for cid in cids:
        db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
        db.execute("DELETE FROM chunks WHERE id=?", (cid,))
    db.execute("DELETE FROM engineering_entities WHERE document_id=?", (document_id,))
    db.execute("DELETE FROM documents WHERE id=?", (document_id,))
    db.commit()
    return len(cids)


_DOC_COLS = ("d.id, d.filename, d.page_count, d.status, d.source_kind, d.created_at, "
             "  (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS chunks ")


def list_documents(db: sqlite3.Connection) -> list[dict]:
    """**只返回在架文档。** agent / MCP 唯一该走的入口——已归档的文档对模型不可见。"""
    rows = db.execute(
        f"SELECT {_DOC_COLS} FROM documents d WHERE d.archived_at IS NULL ORDER BY d.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_documents(db: sqlite3.Connection) -> list[dict]:
    """含已归档，并带出 owner/归档/sha/原始路径。

    **仅供 HTTP 层调用**（webapp 的 /api/documents 与 /admin/api/documents）——它们会在返回前
    按调用者身份算好 can_manage/can_delete。**不要从 tools.py / mcp 调用本函数**：那等于让模型
    看到已下架的资料，破坏 Retrieval invariant。刻意不做成 list_documents(include_archived=True)
    的布尔开关——开关可翻转且 review 时不显眼，函数名把边界写死。
    """
    rows = db.execute(
        f"SELECT {_DOC_COLS}, d.owner_id, d.archived_at, d.sha256, d.source_path "
        "FROM documents d ORDER BY d.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def missing_embeddings(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT id, title, text FROM chunks WHERE id NOT IN (SELECT chunk_id FROM chunks_vec) ORDER BY id"
    ).fetchall()


def store_embeddings(db: sqlite3.Connection, ids: list[int], vecs):
    db.executemany(
        "INSERT OR REPLACE INTO chunks_vec(chunk_id, embedding) VALUES(?,?)",
        [(i, v.astype("float32").tobytes()) for i, v in zip(ids, vecs)],
    )
    db.commit()


def stats(db: sqlite3.Connection) -> dict:
    out = {
        "documents": db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "chunks_total": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embedded": db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0],
        "chars_total": db.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks").fetchone()[0],
    }
    out["by_source_kind"] = {
        r["source_kind"]: r["n"]
        for r in db.execute("SELECT source_kind, COUNT(*) n FROM documents GROUP BY source_kind")
    }
    return out
