"""摄取编排：connector → split_note/seg → store → (embed)。

三个入口（CLI ingest / webapp 上传 / watch 落盘）都落到 ingest_file()。
幂等：整文件 sha256 命中且 status='embedded' 即跳过。
"""

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
    """把所有 chunk 已全部嵌入的 'chunked' 文档标为 'embedded'。"""
    db.execute(
        "UPDATE documents SET status='embedded' WHERE status='chunked' AND id NOT IN "
        "(SELECT c.document_id FROM chunks c WHERE c.id NOT IN (SELECT chunk_id FROM chunks_vec))"
    )
    db.commit()


def ingest_file(path, db=None, do_embed: bool = True, on_stage=None) -> dict:
    """摄取单个文件。on_stage(stage, payload) 用于进度回调（webapp SSE）。"""
    path = Path(path)
    db = db or store.connect()
    mod = connectors.loader_for(path)
    if mod is None:
        raise ValueError(f"不支持的文件类型 {path.suffix}（支持 {sorted(connectors.supported_exts())}）")

    sha = store.file_sha256(path)
    existing = store.get_document(db, sha)
    if existing and existing["status"] == "embedded":
        if on_stage:
            on_stage("skip", {"document_id": existing["id"], "filename": path.name})
        return {"document_id": existing["id"], "skipped": True, "chunks": 0}

    if on_stage:
        on_stage("loading", {"filename": path.name})
    pages = mod.load(path)
    paged = [p for p in pages if p.page_no]
    doc_id, _ = store.upsert_document(
        db, filename=path.name, sha256=sha, source_path=str(path),
        mime=getattr(mod, "MIME", ""), page_count=len(paged) or len(pages),
    )

    if on_stage:
        on_stage("chunking", {"document_id": doc_id, "filename": path.name})
    min_c, max_c = _chunk_params()
    chunk_dicts: list[dict] = []
    idx = 0
    for page in pages:
        if not page.text.strip():
            continue
        for title, body in split_note(page.text, min_c, max_c):
            head = (title + "\n" + body) if title else body
            chunk_dicts.append({
                "document_id": doc_id,
                "chunk_index": idx,
                "title": title or None,
                "page_no": page.page_no,
                "text": body,
                "text_seg": seg(head),
                "meta_json": None,
                "content_hash": store.content_hash(doc_id, idx, body),
            })
            idx += 1

    # 垂直层入库 hook（NullVertical 恒等返回）
    doc_row = dict(store.get_document(db, sha))
    chunk_dicts = get_vertical().on_ingest(doc_row, chunk_dicts)

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
    return {"document_id": doc_id, "skipped": False, "chunks": len(chunk_dicts)}


def ingest_path(path, do_embed: bool = True, on_stage=None) -> list[dict]:
    """摄取一个文件或整个目录（按扩展名过滤）。目录情形下末尾统一 embed 一次。"""
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
