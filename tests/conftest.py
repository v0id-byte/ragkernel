"""CAD 测试公共夹具。

夹具是**提交进仓库**的程序化产物（tests/fixtures/cad/），阅读端测试因此不依赖写入端正确性，
无 cad extra 时（reader 依赖缺失）相关测试用 importorskip 干净跳过而非报错。
每个测试用 RAGKERNEL_DATA_DIR 隔离到临时库，绝不碰用户 KB。
"""

import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "cad"


@pytest.fixture
def fx():
    def _fx(name: str) -> str:
        return str(FIX / name)
    return _fx


@pytest.fixture
def db(tmp_path, monkeypatch):
    """隔离到临时库的连接。不依赖任何 CAD extra，非 CAD 测试也用它。"""
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    from ragkernel import store
    conn = store.connect()
    yield conn
    conn.close()


@pytest.fixture
def cad_db(db):
    """`db` 的别名——CAD 测试沿用原名。"""
    return db


@pytest.fixture
def doc(db):
    """插一篇可被 BM25 检索到的文档（doc + 一个 chunk）。返回 document_id。"""
    from ragkernel import chunking, store

    def _doc(text: str, *, sha: str = "", filename: str = "t.md",
             status: str = "chunked", owner_id: int | None = None) -> int:
        sha = sha or store.content_hash(text, filename)
        doc_id, _ = store.upsert_document(db, filename=filename, sha256=sha, owner_id=owner_id)
        store.upsert_chunks(db, doc_id, [{
            "document_id": doc_id, "chunk_index": 0, "title": filename, "page_no": None,
            "text": text, "text_seg": chunking.seg(text), "meta_json": None,
            "content_hash": store.content_hash(doc_id, text),
        }])
        store.set_status(db, doc_id, status)
        return doc_id

    return _doc


@pytest.fixture
def ingest(cad_db):
    from ragkernel import pipeline

    def _ingest(name: str) -> dict:
        return pipeline.ingest_file(str(FIX / name), db=cad_db, do_embed=False)

    return _ingest


@pytest.fixture
def toolbox(cad_db):
    from ragkernel.tools import Toolbox
    return Toolbox(db=cad_db)


@pytest.fixture
def entities(cad_db):
    from ragkernel import store

    def _get(document_id, entity_type=None):
        return store.get_engineering_entities(cad_db, document_id, entity_type)

    return _get


def parse_json_fields(row: dict) -> dict:
    """把一行 engineering_entities 的 *_json 解析成 dict，便于断言。"""
    out = dict(row)
    for k in ("properties_json", "geometry_json", "provenance_json", "assembly_path_json"):
        if out.get(k):
            out[k[:-5]] = json.loads(out[k])
    return out
