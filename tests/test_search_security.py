"""Retrieval invariant 的回归测试：已归档 / rejected 的文档绝不进入检索结果。

这是本次生命周期改动的**安全边界**——不变式收口在 search._ACTIVE，测试与它同属一个提交。
全部走纯 BM25（qvec=None），不需要嵌入模型，CI 可跑。
"""

import time

from ragkernel import search, store

Q = "液压泵异响排查步骤"
TEXT = "液压泵异响排查步骤：先查油位，再查联轴器同心度。"


def _hits(db, query=Q):
    return search.hybrid_search(db, query, qvec=None, k=5, reranker=None)


def test_archived_excluded_from_search(db, doc):
    """归档后立刻退出检索——归档不是打个标签，是真的下架。"""
    doc_id = doc(TEXT)
    assert [r["document_id"] for r in _hits(db)] == [doc_id]

    store.set_archived(db, doc_id, int(time.time()))
    assert _hits(db) == []

    store.set_archived(db, doc_id, None)  # 可逆
    assert [r["document_id"] for r in _hits(db)] == [doc_id]


def test_rejected_status_excluded(db, doc):
    """CAD 超限被拒的文档只剩一条说明性状态 chunk，不该被当正文召回。"""
    doc_id = doc(TEXT, status="rejected")
    assert _hits(db) == []
    assert store.get_document_by_id(db, doc_id)["status"] == "rejected"


def test_chunked_still_searchable(db, doc):
    """未 embed 的 chunked 文档仍必须可被 BM25 召回。

    把「不变式只排除 rejected、不收紧成 status='embedded'」这条推理钉成测试：
    纯 BM25 部署（不配 embedding provider）与 do_embed=False 的测试路径都依赖它。
    """
    doc_id = doc(TEXT, status="chunked")
    assert [r["document_id"] for r in _hits(db)] == [doc_id]


def test_embedding_failed_still_searchable(db, doc):
    """向量没建成但正文在——降级召回是既有设计意图，不该被顺手砍掉。"""
    doc_id = doc(TEXT, status="embedding_failed")
    assert [r["document_id"] for r in _hits(db)] == [doc_id]


def test_null_status_still_searchable(db, doc):
    """status 为 NULL 的历史行不该被误滤——SQL 里 `NULL != 'rejected'` 求值为 NULL 而非 TRUE，
    靠 COALESCE 兜住。这条测的就是那个 COALESCE。"""
    doc_id = doc(TEXT)
    db.execute("UPDATE documents SET status=NULL WHERE id=?", (doc_id,))
    db.commit()
    assert [r["document_id"] for r in _hits(db)] == [doc_id]


def test_caller_scope_still_applies(db, doc):
    """调用方注入的 where 与不变式是 AND 关系：能进一步收窄，不能放宽。"""
    keep = doc(TEXT, filename="keep.md")
    other = doc(TEXT, filename="other.md")

    rows = search.hybrid_search(db, Q, qvec=None, k=5, reranker=None,
                                where="c.document_id = ?", params=(keep,))
    assert [r["document_id"] for r in rows] == [keep]

    # 已归档的文档，即使调用方明确点名，也进不来
    store.set_archived(db, other, int(time.time()))
    rows = search.hybrid_search(db, Q, qvec=None, k=5, reranker=None,
                                where="c.document_id = ?", params=(other,))
    assert rows == []
