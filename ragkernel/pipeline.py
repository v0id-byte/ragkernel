"""摄取编排:connector → 垂直 split/classify → store → (embed)。

四个入口都落到这里:CLI ingest / webapp 上传 / watch 落盘 / 反馈回填(ingest_record)。
幂等:整文件 sha256 命中且 status='embedded' 即跳过。
"""

import json
import time
from pathlib import Path

from . import config, connectors, store
from .chunking import compose_context, model_hint, seg, split_note
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
    """按垂直层拆片 + 打分类 + 前置上下文前缀;table 类连接器(ATOMIC)整条一片。

    PRECHUNKED 连接器（如 CAD）：每 Page 即一片，直接用 page.title/page.meta，
    **不走垂直层 split、不打故障案例默认分类、不加型号上下文前缀、不过 on_ingest**——
    连接器已产出自足文本与实体溯源元数据，垂直层无关（不干扰 equipment 维修手册能力）。
    """
    v = get_vertical()
    atomic = getattr(mod, "ATOMIC", False)
    prechunked = getattr(mod, "PRECHUNKED", False)
    min_c, max_c = _chunk_params()
    doc_row = dict(db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    model = "" if prechunked else model_hint(doc_row.get("filename") or "")
    chunk_dicts: list[dict] = []
    idx = 0
    for page in pages:
        if not page.text.strip():
            continue
        if prechunked:
            pieces = [(page.title, page.text, dict(page.meta or {}))]
        elif atomic:
            # 工单/反馈是完整案例，整条一片、统一归为「故障案例」（不按字段细分类）
            pieces = [(f"记录 {page.page_no or idx + 1}", page.text, {"category": "故障案例", "by": "rule"})]
        else:
            pieces = v.split(page.text, page.page_no)
            if pieces is None:
                pieces = [(t, b, {}) for t, b in split_note(page.text, min_c, max_c)]
        for title, body, meta in pieces:
            if not body.strip():
                continue
            if prechunked:
                text = body  # 连接器已产出自足文本，不再前置型号/章节上下文
            else:
                if model:
                    meta = {**meta, "model": model}
                # 确定性上下文前缀（型号·章节·表标题）前置进正文——同时进 embedding 与 BM25
                ctx = compose_context(model, meta)
                text = (ctx + "\n" + body) if ctx else body
            head = (title + "\n" + text) if title else text
            chunk_dicts.append({
                "document_id": doc_id,
                "chunk_index": idx,
                "title": title or None,
                "page_no": page.page_no,
                "text": text,
                "text_seg": seg(head),
                "meta_json": json.dumps(meta, ensure_ascii=False) if meta else None,
                "content_hash": store.content_hash(doc_id, idx, text),
            })
            idx += 1
    return chunk_dicts if prechunked else v.on_ingest(doc_row, chunk_dicts)


def _settle_reupload(db, sha: str, owner_id: int | None) -> tuple[dict | None, bool]:
    """重传同一文件时的归属 / 上架判定。返回 (已存在的行, 是否拒绝本次摄取)。

    判定只看**这篇文档现在归谁**，不看谁在调用——恢复上架的规则：

        文档 owner    调用方              恢复上架
        NULL          CLI/watch/脚本      ✅
        NULL          任意登录用户        ✅（并认领 owner）
        本人          本人                ✅
        他人          任意登录用户        ❌
        他人          CLI/watch/脚本      ❌  ← 关键

    最后一行是要害：owner_id=None（CLI/watch）**不是**"受信任的本地调用"，它只授予
    "认领无主文档"的能力，不授予"覆盖他人决定"的能力。否则把文件往被监视目录一丢，
    别人归档过的文档就自动上架了——watch 目录会变成一条隐藏的权限入口。

    并发保护：check → update 之间会被另一个上传交错，故整段放在 BEGIN IMMEDIATE 里。
    **只包状态转换**——算 sha 在事务外，parse/chunk/embed 也在事务外，否则一个大 PDF
    会把 documents 锁住几十秒，把并发上传和检索一起拖死。
    """
    own_tx = not db.in_transaction
    if own_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        row = store.get_document(db, sha)
        if not row:
            return None, False
        owner, archived = row["owner_id"], row["archived_at"]
        # Lifecycle invariant：归档是人的决定，只有本人/无主才能由重传撤销
        if archived and not (owner is None or (owner_id is not None and owner == owner_id)):
            return dict(row), True
        if archived:
            db.execute("UPDATE documents SET archived_at=NULL WHERE id=?", (row["id"],))
        if owner is None and owner_id:  # Ownership invariant：只回填，不覆盖
            db.execute("UPDATE documents SET owner_id=? WHERE id=? AND owner_id IS NULL",
                       (owner_id, row["id"]))
        return dict(store.get_document(db, sha)), False
    finally:
        if own_tx:
            db.commit()


def ingest_file(path, db=None, do_embed: bool = True, on_stage=None, *,
                owner_id: int | None = None) -> dict:
    """摄取单个文件。on_stage(stage, payload) 用于进度回调(webapp SSE)。

    owner_id 关键字限定、默认 None：CLI / watch / 脚本摄取产出无主文档（仅管理员可处置），
    web 上传由 webapp 传入上传者。见 _settle_reupload 的判定表。
    """
    path = Path(path)
    db = db or store.connect()
    mod = connectors.loader_for(path)
    if mod is None:
        raise ValueError(f"不支持的文件类型 {path.suffix}(支持 {sorted(connectors.supported_exts())})")

    t0 = time.time()
    sha = store.file_sha256(path)
    existing, blocked = _settle_reupload(db, sha, owner_id)
    if blocked:
        store.log_ingestion(db, existing["id"], path.name, "skipped", ms=int((time.time() - t0) * 1000))
        if on_stage:
            on_stage("archived_by_other", {"document_id": existing["id"], "filename": path.name})
        return {"document_id": existing["id"], "skipped": True, "chunks": 0, "blocked": True}
    if existing and existing["status"] == "embedded":
        store.log_ingestion(db, existing["id"], path.name, "skipped", ms=int((time.time() - t0) * 1000))
        if on_stage:
            on_stage("skip", {"document_id": existing["id"], "filename": path.name})
        return {"document_id": existing["id"], "skipped": True, "chunks": 0}

    # 原生 CAD（暴露 load_bundle 的连接器）：单次解析 + 文档/chunks/工程实体原子写入。
    if hasattr(mod, "load_bundle"):
        return _ingest_cad_file(path, mod, db, sha, do_embed, on_stage, t0, owner_id)

    if on_stage:
        on_stage("loading", {"filename": path.name})
    pages = mod.load(path)
    paged = [p for p in pages if p.page_no]
    src_kind = getattr(mod, "SOURCE_KIND", "upload")
    doc_id, _ = store.upsert_document(
        db, filename=path.name, sha256=sha, source_path=str(path),
        mime=getattr(mod, "MIME", ""), page_count=len(paged) or len(pages), source_kind=src_kind,
        owner_id=owner_id,
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


def _ingest_cad_file(path, mod, db, sha: str, do_embed: bool, on_stage, t0: float,
                     owner_id: int | None = None) -> dict:
    """CAD 摄取路径：load_bundle 一次解析 → 原子写入 → (事务外) embed。

    超限 → status='rejected'（保留一个说明性 document 实体 + 一条状态 chunk，不假装成功）。
    embed 失败 → status='embedding_failed'（结构化实体已落库、向量未完成，结构化工具仍可读）。
    """
    if on_stage:
        on_stage("loading", {"filename": path.name})
    bundle = mod.load_bundle(path)
    pages = bundle.pages
    smeta = bundle.source_metadata or {}
    aborted = bool(smeta.get("aborted"))
    paged = [p for p in pages if p.page_no]
    doc_fields = dict(
        filename=path.name, sha256=sha, source_path=str(path),
        mime=getattr(mod, "MIME", ""), page_count=len(paged) or len(pages),
        source_kind=getattr(mod, "SOURCE_KIND", "upload"), owner_id=owner_id,
    )
    if on_stage:
        on_stage("chunking", {"filename": path.name})
    res = store.ingest_cad_atomic(
        db, doc_fields,
        build_chunks=lambda doc_id: _build_chunks(db, doc_id, pages, mod),
        entities=bundle.engineering_entities,
        status="rejected" if aborted else "chunked",
    )
    doc_id = res["document_id"]
    if on_stage:
        on_stage("chunked", {"document_id": doc_id, "chunks": res["chunks"],
                             "added": res["added"], "removed": res["removed"]})

    if aborted:
        reason = smeta.get("abort_reason")
        store.log_ingestion(db, doc_id, path.name, "rejected", chunks=res["chunks"],
                            ms=int((time.time() - t0) * 1000))
        if on_stage:
            on_stage("rejected", {"document_id": doc_id, "reason": reason})
        return {"document_id": doc_id, "skipped": False, "chunks": res["chunks"],
                "rejected": True, "reason": reason}

    if do_embed:
        if on_stage:
            on_stage("embedding", {"document_id": doc_id})
        try:
            embed_missing(db)
            _mark_embedded(db)
        except Exception as e:  # 结构化层已提交；仅向量索引失败——如实标记，不谎报成功
            store.set_status(db, doc_id, "embedding_failed")
            store.log_ingestion(db, doc_id, path.name, "embedding_failed", chunks=res["chunks"],
                                ms=int((time.time() - t0) * 1000))
            if on_stage:
                on_stage("embedding_failed", {"document_id": doc_id, "error": str(e)})
            return {"document_id": doc_id, "skipped": False, "chunks": res["chunks"],
                    "embedding_failed": True}
        if on_stage:
            on_stage("done", {"document_id": doc_id, "chunks": res["chunks"]})
    store.log_ingestion(db, doc_id, path.name, "ingested", chunks=res["chunks"],
                        added=res["added"], removed=res["removed"], ms=int((time.time() - t0) * 1000))
    return {"document_id": doc_id, "skipped": False, "chunks": res["chunks"]}


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
