"""混合检索：FTS5 BM25 + 向量 KNN → RRF 融合 →（可选）cross-encoder 重排。

scope 由调用方以 SQL WHERE 注入（单租户 MVP 默认空；将来多租户注入 tenant_id=?，
垂直层的 post_retrieve 也挂在调用方这一侧）。重排只作用于 RRF 融合后的候选集（几十条），
reranker 由调用方注入（None = 纯 RRF，优雅回退）。
"""

import sqlite3

from .chunking import seg

RRF_K = 60


def _fts_hits(db: sqlite3.Connection, query: str, where: str, params: tuple, limit: int = 40) -> list[int]:
    tokens = seg(query).split()
    if not tokens:
        return []
    fts_q = " OR ".join(f'"{t}"' for t in tokens if '"' not in t)
    sql = (
        "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        f"WHERE chunks_fts MATCH ? {'AND ' + where if where else ''} ORDER BY f.rank LIMIT ?"
    )
    try:
        return [r["id"] for r in db.execute(sql, (fts_q, *params, limit))]
    except sqlite3.OperationalError:
        return []


def _vec_hits(db: sqlite3.Connection, qvec, where: str, params: tuple, limit: int = 40) -> list[int]:
    knn = db.execute(
        "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qvec.astype("float32").tobytes(), 200),
    ).fetchall()
    ids = [r["chunk_id"] for r in knn]
    if not ids:
        return []
    if where:
        ph = ",".join("?" * len(ids))
        allowed = {
            r["id"]
            for r in db.execute(f"SELECT id FROM chunks c WHERE c.id IN ({ph}) AND {where}", (*ids, *params))
        }
        ids = [i for i in ids if i in allowed]
    return ids[:limit]


def hybrid_search(
    db: sqlite3.Connection,
    query: str,
    qvec=None,
    k: int = 8,
    where: str = "",
    params: tuple = (),
    reranker=None,
    candidates: int = 40,
) -> list[sqlite3.Row]:
    """qvec 为 None 时退化为纯 BM25。

    reranker（有 .rerank(query, texts)->list[float]）非空时：先用 RRF 取 candidates 条候选，
    再用 cross-encoder 对 (query, chunk.text) 打分重排，取 top-k；reranker 为 None 时直接按 RRF 取 top-k。
    返回 chunks 行，按最终得分降序。
    """
    ranks: dict[int, float] = {}
    for rank, cid in enumerate(_fts_hits(db, query, where, params)):
        ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if qvec is not None:
        for rank, cid in enumerate(_vec_hits(db, qvec, where, params)):
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

    # reranker 存在时取更大的候选集，让重排有发挥空间；否则只需 top-k
    n_pool = max(candidates, k) if reranker is not None else k
    pool = sorted(ranks, key=ranks.get, reverse=True)[:n_pool]
    if not pool:
        return []
    ph = ",".join("?" * len(pool))
    rows = {r["id"]: r for r in db.execute(f"SELECT * FROM chunks WHERE id IN ({ph})", pool)}
    ordered = [rows[i] for i in pool if i in rows]

    if reranker is not None and ordered:
        try:
            scores = reranker.rerank(query, [r["text"] for r in ordered])
            ordered = [r for _, r in sorted(zip(scores, ordered), key=lambda x: x[0], reverse=True)]
        except Exception:
            # 重排失败 → 回退到 RRF 顺序，绝不因此丢结果
            pass
    return ordered[:k]
