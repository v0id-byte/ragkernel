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
  status TEXT DEFAULT 'pending',
  meta_json TEXT,
  created_at INTEGER
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
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text_seg, content='chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text_seg) VALUES (new.id, new.text_seg);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text_seg) VALUES('delete', old.id, old.text_seg);
END;
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "ragkernel.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")  # 上传写入时会话仍可并发读
    db.execute("PRAGMA busy_timeout=5000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA)
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


def upsert_document(
    db: sqlite3.Connection, filename: str, sha256: str, source_path: str = "",
    mime: str = "", page_count: int = 0, meta_json: str = "",
) -> tuple[int, bool]:
    """按 sha256 幂等。返回 (document_id, existed)。"""
    row = get_document(db, sha256)
    if row:
        return row["id"], True
    cur = db.execute(
        "INSERT INTO documents(filename,source_path,mime,sha256,page_count,status,meta_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (filename, source_path or None, mime or None, sha256, page_count or None,
         "pending", meta_json or None, int(time.time())),
    )
    db.commit()
    return cur.lastrowid, False


def set_status(db: sqlite3.Connection, document_id: int, status: str) -> None:
    db.execute("UPDATE documents SET status=? WHERE id=?", (status, document_id))
    db.commit()


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
    """级联删除：chunks_vec → chunks → documents。返回删掉的 chunk 数。"""
    cids = [r["id"] for r in db.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
    for cid in cids:
        db.execute("DELETE FROM chunks_vec WHERE chunk_id=?", (cid,))
        db.execute("DELETE FROM chunks WHERE id=?", (cid,))
    db.execute("DELETE FROM documents WHERE id=?", (document_id,))
    db.commit()
    return len(cids)


def list_documents(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT d.id, d.filename, d.page_count, d.status, d.created_at, "
        "  (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS chunks "
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
    }
    return out
