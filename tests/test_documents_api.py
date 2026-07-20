"""文档生命周期的 HTTP 面：权限矩阵、被堵上的删除洞、硬删除与审计。

Flask test_client 的 remote_addr 默认就是 127.0.0.1，require_admin_ip 始终放行本机，
无需 patch 白名单。
"""

import json
import time

import pytest

from ragkernel import auth, store, webapp


@pytest.fixture
def api(tmp_path, monkeypatch):
    """隔离数据目录的 test_client + 造好的用户。返回 (client, tokens, db)。"""
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    webapp.app.config["TESTING"] = True

    users, tokens = {}, {}
    for name, is_admin in (("alice", False), ("bob", False), ("root", True)):
        uid = auth.create_user(name, "pw-" + name, is_admin=is_admin)["id"]
        users[name] = uid
        tokens[name] = auth.issue_token(uid)

    db = store.connect()
    (tmp_path / "uploads").mkdir(exist_ok=True)
    yield webapp.app.test_client(), tokens, users, db
    db.close()


def _hdr(tok):
    return {"Authorization": "Bearer " + tok}


def _mkdoc(db, tmp_path=None, owner_id=None, sha="s1", filename="a.md", source_path=None):
    doc_id, _ = store.upsert_document(db, filename=filename, sha256=sha,
                                      source_path=source_path or "", owner_id=owner_id)
    return doc_id


# ── 被堵上的权限洞 ───────────────────────────────────────────

def test_user_delete_route_is_gone(api):
    """原先 DELETE /api/documents/<id> 只挂 require_auth，任何登录用户能硬删任何文档。
    整条路由已移除——这条是那个洞的回归测试。"""
    client, tokens, users, db = api
    doc_id = _mkdoc(db, owner_id=users["alice"])

    r = client.delete(f"/api/documents/{doc_id}", headers=_hdr(tokens["alice"]))
    assert r.status_code == 404
    assert store.get_document_by_id(db, doc_id) is not None


def test_non_admin_cannot_hard_delete(api):
    client, tokens, users, db = api
    doc_id = _mkdoc(db, owner_id=users["alice"])

    r = client.delete(f"/admin/api/documents/{doc_id}", headers=_hdr(tokens["alice"]))
    assert r.status_code == 403
    assert store.get_document_by_id(db, doc_id) is not None


# ── 权限矩阵 ─────────────────────────────────────────────────

@pytest.mark.parametrize("actor,owner,archived,action,expect", [
    ("alice", "alice", False, "archive",   200),
    ("alice", "alice", True,  "unarchive", 200),
    ("alice", "bob",   False, "archive",   403),
    ("alice", None,    False, "archive",   403),   # 无主的历史文档：普通用户不可动
    ("root",  "bob",   False, "archive",   200),
    ("root",  None,    True,  "unarchive", 200),   # 管理员可处置无主文档
])
def test_permission_matrix(api, actor, owner, archived, action, expect):
    client, tokens, users, db = api
    doc_id = _mkdoc(db, owner_id=users[owner] if owner else None)
    if archived:
        store.set_archived(db, doc_id, int(time.time()))

    r = client.post(f"/api/documents/{doc_id}/{action}", headers=_hdr(tokens[actor]))
    assert r.status_code == expect

    if expect == 200:
        want_archived = action == "archive"
        assert (store.get_document_by_id(db, doc_id)["archived_at"] is not None) is want_archived


def test_archive_missing_document_404(api):
    client, tokens, _, _ = api
    assert client.post("/api/documents/999/archive", headers=_hdr(tokens["alice"])).status_code == 404


# ── 列表视图 ─────────────────────────────────────────────────

def test_document_list_carries_permissions_not_raw_owner(api):
    client, tokens, users, db = api
    mine = _mkdoc(db, owner_id=users["alice"], sha="s1", filename="mine.md")
    theirs = _mkdoc(db, owner_id=users["bob"], sha="s2", filename="theirs.md")
    store.set_archived(db, theirs, int(time.time()))

    docs = {d["id"]: d for d in client.get("/api/documents", headers=_hdr(tokens["alice"])).json["documents"]}

    # 归档是生命周期可见性、不是访问控制：他人归档的文档仍然可见，只是没有操作权
    assert set(docs) == {mine, theirs}
    assert docs[mine]["can_manage"] is True and docs[mine]["archived"] is False
    assert docs[theirs]["can_manage"] is False and docs[theirs]["archived"] is True
    assert all(d["can_delete"] is False for d in docs.values())
    # 前端不该拿到推导权限的原料
    assert all("owner_id" not in d and "source_path" not in d for d in docs.values())


def test_admin_sees_owner_names(api):
    client, tokens, users, db = api
    owned = _mkdoc(db, owner_id=users["alice"], sha="s1")
    orphan = _mkdoc(db, owner_id=None, sha="s2", filename="legacy.pdf")

    docs = {d["id"]: d for d in client.get("/admin/api/documents", headers=_hdr(tokens["root"])).json["documents"]}
    assert docs[owned]["owner"] == "alice"
    assert docs[orphan]["owner"] is None
    assert all("source_path" not in d for d in docs.values())


# ── 硬删除 ───────────────────────────────────────────────────

def test_admin_delete_removes_index_and_source_file(api, tmp_path):
    client, tokens, users, db = api
    src = tmp_path / "uploads" / "manual.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    doc_id = _mkdoc(db, owner_id=users["alice"], source_path=str(src))

    r = client.delete(f"/admin/api/documents/{doc_id}", headers=_hdr(tokens["root"]))

    assert r.status_code == 200
    assert r.json["index_removed"] is True and r.json["source_removed"] is True
    assert store.get_document_by_id(db, doc_id) is None
    assert not src.exists()


def test_admin_delete_never_touches_files_outside_uploads(api, tmp_path):
    """watch 目录 / 脚本摄取的源文件在库外，路径护栏必须拦住。"""
    client, tokens, users, db = api
    outside = tmp_path / "watched" / "spec.md"
    outside.parent.mkdir()
    outside.write_text("keep me")
    doc_id = _mkdoc(db, owner_id=users["alice"], source_path=str(outside))

    r = client.delete(f"/admin/api/documents/{doc_id}", headers=_hdr(tokens["root"]))

    assert r.json["index_removed"] is True and r.json["source_removed"] is False
    assert outside.exists() and outside.read_text() == "keep me"


def test_admin_delete_writes_audit_snapshot(api, tmp_path):
    """审计 payload 必须是删除**前**的快照——行删掉之后就无从回读了。"""
    client, tokens, users, db = api
    doc_id = _mkdoc(db, owner_id=users["alice"], sha="deadbeef", filename="manual.pdf")

    client.delete(f"/admin/api/documents/{doc_id}", headers=_hdr(tokens["root"]))

    from ragkernel import audit
    row = audit.connect().execute(
        "SELECT payload_json FROM events WHERE kind='document_deleted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    p = json.loads(row["payload_json"])
    assert p["filename"] == "manual.pdf" and p["sha256"] == "deadbeef"
    assert p["owner_id"] == users["alice"]
    # operator 同时存 id 与当时的用户名快照：改名不污染历史，销号仍可解读
    assert p["operator_id"] == users["root"] and p["operator_name"] == "root"


def test_admin_delete_missing_document_404(api):
    client, tokens, _, _ = api
    assert client.delete("/admin/api/documents/999", headers=_hdr(tokens["root"])).status_code == 404
