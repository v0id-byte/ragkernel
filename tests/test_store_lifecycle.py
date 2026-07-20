"""documents 的归属 / 归档两列：迁移、Ownership invariant、列表函数的边界。"""

import sqlite3
import time

import pytest

from ragkernel import store


def _cols(db) -> set[str]:
    return {r["name"] for r in db.execute("PRAGMA table_info(documents)")}


def test_migration_adds_columns_to_old_db(tmp_path, monkeypatch):
    """旧库（documents 无 owner_id/archived_at）连上来应自动补列且保留原有数据。"""
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    old = sqlite3.connect(tmp_path / "ragkernel.db")
    old.execute("""CREATE TABLE documents(
        id INTEGER PRIMARY KEY, filename TEXT NOT NULL, source_path TEXT, mime TEXT,
        sha256 TEXT UNIQUE NOT NULL, page_count INTEGER, status TEXT DEFAULT 'pending',
        source_kind TEXT DEFAULT 'upload', meta_json TEXT, created_at INTEGER)""")
    old.execute("INSERT INTO documents(filename, sha256, status) VALUES('legacy.pdf','abc','embedded')")
    old.commit()
    old.close()

    db = store.connect()
    assert {"owner_id", "archived_at"} <= _cols(db)
    row = store.get_document(db, "abc")
    assert row["filename"] == "legacy.pdf" and row["status"] == "embedded"
    assert row["owner_id"] is None and row["archived_at"] is None  # 存量 = 无主、在架
    db.close()


def test_migration_idempotent(db, tmp_path):
    """重复 connect 不该报错，也不该重复加列。"""
    before = _cols(db)
    again = store.connect()
    assert _cols(again) == before
    again.close()


def test_owner_backfilled_when_unowned(db):
    """无主文档可被首个具名上传者认领。"""
    doc_id, _ = store.upsert_document(db, filename="a.md", sha256="s1", owner_id=None)
    assert store.get_document_by_id(db, doc_id)["owner_id"] is None

    store.upsert_document(db, filename="a.md", sha256="s1", owner_id=7)
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 7


def test_owner_never_overwritten(db):
    """Ownership invariant：有主之后，任何后续摄取都不能改写 owner。"""
    doc_id, _ = store.upsert_document(db, filename="a.md", sha256="s1", owner_id=1)

    store.upsert_document(db, filename="a.md", sha256="s1", owner_id=2)   # 另一个用户重传
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 1

    store.upsert_document(db, filename="a.md", sha256="s1", owner_id=None)  # CLI / watch 重传
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 1

    store.set_owner(db, doc_id, 3)  # 显式 set_owner 也拦（带 owner_id IS NULL 兜底）
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 1


def test_list_documents_hides_archived_from_agent(db):
    """list_documents 是 agent/MCP 入口——已归档的必须不可见。"""
    a, _ = store.upsert_document(db, filename="a.md", sha256="s1")
    b, _ = store.upsert_document(db, filename="b.md", sha256="s2")
    store.set_archived(db, b, int(time.time()))

    assert [d["id"] for d in store.list_documents(db)] == [a]

    all_docs = {d["id"]: d for d in store.list_all_documents(db)}
    assert set(all_docs) == {a, b}
    assert all_docs[b]["archived_at"] is not None
    assert "sha256" in all_docs[b] and "owner_id" in all_docs[b]  # HTTP 层需要这些字段


def test_set_archived_roundtrip(db):
    doc_id, _ = store.upsert_document(db, filename="a.md", sha256="s1")
    store.set_archived(db, doc_id, 12345)
    assert store.get_document_by_id(db, doc_id)["archived_at"] == 12345
    store.set_archived(db, doc_id, None)
    assert store.get_document_by_id(db, doc_id)["archived_at"] is None
