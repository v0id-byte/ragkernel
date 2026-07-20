"""归属透传 + 重传判定表（Ownership / Lifecycle invariant）。

用 markdown 走非 CAD 摄取路径，do_embed=False，不需要任何模型。
"""

import time

import pytest

from ragkernel import pipeline, store

TEXT = "# 液压泵手册\n\n异响排查：先查油位，再查联轴器同心度。\n"


@pytest.fixture
def md(tmp_path):
    p = tmp_path / "pump.md"
    p.write_text(TEXT, encoding="utf-8")
    return str(p)


def _ingest(db, path, owner_id=None):
    return pipeline.ingest_file(path, db=db, do_embed=False, owner_id=owner_id)


def test_owner_recorded_on_upload(db, md):
    doc_id = _ingest(db, md, owner_id=5)["document_id"]
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 5


def test_cli_ingest_leaves_document_unowned(db, md):
    """CLI / watch / 脚本摄取产出无主文档——仅管理员可处置。"""
    doc_id = _ingest(db, md)["document_id"]
    assert store.get_document_by_id(db, doc_id)["owner_id"] is None


# 重传判定表：唯一准则是「这篇文档现在归谁」，不是「谁在调用」
@pytest.mark.parametrize("doc_owner,uploader,restored", [
    (None, None, True),    # 无主 + CLI/watch      → 恢复
    (None, 5,    True),    # 无主 + 登录用户        → 恢复并认领
    (5,    5,    True),    # 本人 + 本人            → 恢复
    (5,    9,    False),   # 他人 + 另一个登录用户  → 保持归档
    (5,    None, False),   # 他人 + CLI/watch      → 保持归档（watch 不是权限后门）
])
def test_reupload_decision_table(db, md, doc_owner, uploader, restored):
    doc_id = _ingest(db, md, owner_id=doc_owner)["document_id"]
    store.set_archived(db, doc_id, int(time.time()))

    res = _ingest(db, md, owner_id=uploader)

    assert res["document_id"] == doc_id
    row = store.get_document_by_id(db, doc_id)
    assert (row["archived_at"] is None) is restored
    assert res.get("blocked", False) is not restored


def test_blocked_reupload_does_not_claim_owner(db, md):
    """被拒的重传不该顺手改归属。"""
    doc_id = _ingest(db, md, owner_id=5)["document_id"]
    store.set_archived(db, doc_id, int(time.time()))

    _ingest(db, md, owner_id=9)
    assert store.get_document_by_id(db, doc_id)["owner_id"] == 5


def test_reupload_claims_unowned_document(db, md):
    doc_id = _ingest(db, md)["document_id"]
    store.set_archived(db, doc_id, int(time.time()))

    _ingest(db, md, owner_id=7)
    row = store.get_document_by_id(db, doc_id)
    assert row["owner_id"] == 7 and row["archived_at"] is None


def test_index_maintenance_keeps_archive_policy(db, md):
    """Lifecycle invariant：索引维护动作不得改变可用状态。

    归档是**人的决定**；重传是用户动作（可按判定表撤销归档），但 reindex / repair /
    rebuild-vector 这类维护是机器动作，绝不能顺手把文档恢复上架——否则「管理员白天归档、
    夜里自动 reindex 全恢复」这类 bug 会毫无征兆。今天的维护路径就是 embed_missing +
    _mark_embedded，这条测试把它们钉住；将来新增 reindex 命令时同样适用。
    """
    doc_id = _ingest(db, md, owner_id=5)["document_id"]
    store.set_archived(db, doc_id, 999)

    pipeline._mark_embedded(db)

    assert store.get_document_by_id(db, doc_id)["archived_at"] == 999


def test_archived_reupload_by_owner_restores_and_reindexes(db, md):
    """本人重传已归档且已 embed 的文档：恢复上架，而不是被 embedded 跳过分支静默吞掉。"""
    doc_id = _ingest(db, md, owner_id=5)["document_id"]
    store.set_status(db, doc_id, "embedded")
    store.set_archived(db, doc_id, int(time.time()))

    res = _ingest(db, md, owner_id=5)

    assert res.get("blocked", False) is False
    assert store.get_document_by_id(db, doc_id)["archived_at"] is None
