"""摄取编排:connector → 垂直 split/classify → store → (embed)。

四个入口都落到这里:CLI ingest / webapp 上传 / watch 落盘 / 反馈回填(ingest_record)。
幂等:整文件 sha256 命中且 status='embedded' 即跳过。
"""

import json
import time
from pathlib import Path

from . import config, connectors, store
from .chunking import seg, split_note
from .verticals import get_vertical


def _chunk_params() -> tuple[int, int]:
    ch = config.settings().get("chunking") or {}
    return int(ch.get("min_chars", 200)), int(ch.get("max_chars", 3000))


def embed_missing(db=None, batch: int = 64, on_progress=None) -> int:
    """给所有尚无向量的 chunk 补齐 embedding。返回本次嵌入的 chunk 数。"""
    from . import embed as embedder

    db = db or store.connect()
    rows = store.missing_embeddings(db)
    if not rows:
        return 0
    for i in range(0, len(rows), batch):
        b = rows[i : i + batch]
        vecs = embedder.embed([f"{r['title'] or ''}\n{r['text']}"[:6000] for r in b])
        store.store_embeddings(db, [r["id"] for r in b], vecs)
        if on_progress:
            on_progress(min(i + batch, len(rows)), len(rows))
    return len(rows)


def _mark_embedded(db) -> None:
    db.execute(
        "UPDATE documents SET status='embedded' WHERE status='chunked' AND id NOT IN "
        "(SELECT c.document_id FROM chunks c WHERE c.id NOT IN (SELECT chunk_id FROM chunks_vec))"
    )
    db.commit()


def _build_chunks(db, doc_id: int, pages, mod) -> list[dict]:
    """按垂直层拆片 + 打分类;table 类连接器(ATOMIC)整条一片。"""
    v = get_vertical()
    atomic = getattr(mod, "ATOMIC", False)
    min_c, max_c = _chunk_params()
    chunk_dicts: list[dict] = []
    idx = 0
    for page in pages:
        if not page.text.strip():
            continue
        if atomic:
            # 工单/反馈是完整案例，整条一片、统一归为「故障案例」（不按字段细分类）
            pieces = [(f"记录 {page.page_no or idx + 1}", page.text, {"category": "故障案例", "by": "rule"})]
        else:
            pieces = v.split(page.text, page.page_no)
            if pieces is None:
                pieces = [(t, b, {}) for t, b in split_note(page.text, min_c, max_c)]
        for title, body, meta in pieces:
            if not body.strip():
                continue
            head = (title + "\n" + body) if title else body
            chunk_dicts.append({
                "document_id": doc_id,
                "chunk_index": idx,
                "title": title or None,
                "page_no": page.page_no,
                "text": body,
                "text_seg": seg(head),
                "meta_json": json.dumps(meta, ensure_ascii=False) if meta else None,
                "content_hash": store.content_hash(doc_id, idx, body),
            })
            idx += 1
    doc_row = dict(db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    return v.on_ingest(doc_row, chunk_dicts)


def ingest_file(path, db=None, do_embed: bool = True, on_stage=None) -> dict:
    """摄取单个文件。on_stage(stage, payload) 用于进度回调(webapp SSE)。"""
    path = Path(path)
    db = db or store.connect()
    mod = connectors.loader_for(path)
    if mod is None:
        raise ValueError(f"不支持的文件类型 {path.suffix}(支持 {sorted(connectors.supported_exts())})")

    t0 = time.time()
    sha = store.file_sha256(path)
    existing = store.get_document(db, sha)
    if existing and existing["status"] == "embedded":
        store.log_ingestion(db, existing["id"], path.name, "skipped", ms=int((time.time() - t0) * 1000))
        if on_stage:
            on_stage("skip", {"document_id": existing["id"], "filename": path.name})
        return {"document_id": existing["id"], "skipped": True, "chunks": 0}

    if on_stage:
        on_stage("loading", {"filename": path.name})
    pages = mod.load(path)
    paged = [p for p in pages if p.page_no]
    src_kind = getattr(mod, "SOURCE_KIND", "upload")
    doc_id, _ = store.upsert_document(
        db, filename=path.name, sha256=sha, source_path=str(path),
        mime=getattr(mod, "MIME", ""), page_count=len(paged) or len(pages), source_kind=src_kind,
    )

    if on_stage:
        on_stage("chunking", {"document_id": doc_id, "filename": path.name})
    chunk_dicts = _build_chunks(db, doc_id, pages, mod)
    added, removed = store.upsert_chunks(db, doc_id, chunk_dicts)
    store.set_status(db, doc_id, "chunked")
    if on_stage:
        on_stage("chunked", {"document_id": doc_id, "chunks": len(chunk_dicts),
                             "added": added, "removed": removed})

    if do_embed:
        if on_stage:
            on_stage("embedding", {"document_id": doc_id})
        embed_missing(db)
        _mark_embedded(db)
        if on_stage:
            on_stage("done", {"document_id": doc_id, "chunks": len(chunk_dicts)})
    store.log_ingestion(db, doc_id, path.name, "ingested", chunks=len(chunk_dicts),
                        added=added, removed=removed, ms=int((time.time() - t0) * 1000))
    return {"document_id": doc_id, "skipped": False, "chunks": len(chunk_dicts)}


def ingest_record(title: str, text: str, meta: dict | None = None,
                  source: str = "feedback", db=None, do_embed: bool = True) -> dict:
    """回填闭环:把一条工单/反馈/案例合成单片文档入库并立即嵌入,当场可检索。"""
    db = db or store.connect()
    v = get_vertical()
    ts = int(time.time())
    safe_title = (title or "").strip().replace("\n", " ")[:24]
    filename = f"案例-{safe_title}-{ts}" if safe_title else f"案例-{ts}"
    sha = store.content_hash("record", source, text, ts)
    cat = (meta or {}).get("category") or v.classify(text) or "故障案例"
    doc_id, _ = store.upsert_document(
        db, filename=filename, sha256=sha, mime="text/case", page_count=1, source_kind=source,
    )
    chunk_meta = {**(meta or {}), "category": cat, "by": source}
    chunk = {
        "document_id": doc_id, "chunk_index": 0, "title": title or None, "page_no": None,
        "text": text, "text_seg": seg((title + "\n" + text) if title else text),
        "meta_json": json.dumps(chunk_meta, ensure_ascii=False),
        "content_hash": store.content_hash(doc_id, 0, text),
    }
    added, _ = store.upsert_chunks(db, doc_id, [chunk])
    store.set_status(db, doc_id, "chunked")
    if do_embed:
        embed_missing(db)
        _mark_embedded(db)
    store.log_ingestion(db, doc_id, filename, "feedback", chunks=1, added=added, ms=0)
    return {"document_id": doc_id, "category": cat, "filename": filename}


def ingest_path(path, do_embed: bool = True, on_stage=None) -> list[dict]:
    """摄取一个文件或整个目录(按扩展名过滤)。目录情形下末尾统一 embed 一次。"""
    path = Path(path)
    db = store.connect()
    exts = connectors.supported_exts()
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.suffix.lower() in exts)
    else:
        files = [path]
    results = []
    for f in files:
        results.append(ingest_file(f, db=db, do_embed=False, on_stage=on_stage))
    if do_embed:
        if on_stage:
            on_stage("embedding", {"n_files": len(files)})
        embed_missing(db)
        _mark_embedded(db)
        if on_stage:
            on_stage("done", {"n_files": len(files)})
    return results
