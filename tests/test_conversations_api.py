"""会话接口：翻看历史不该被记成一次新会话。"""

import pytest

from ragkernel import audit, auth, convos, webapp


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    webapp.app.config["TESTING"] = True
    webapp._sessions.clear()
    uid = auth.create_user("alice", "pw-alice-123")["id"]
    token = auth.issue_token(uid)
    yield webapp.app.test_client(), {"Authorization": "Bearer " + token}, uid
    webapp._sessions.clear()


def _sessions_count():
    return audit.connect().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def test_replay_does_not_create_audit_session(api):
    """GET /api/conversations/<id> 曾经调 resolve_session → _hydrate → audit.Audit()，
    而 Audit 一构造就 INSERT 一条 sessions 记录。于是「翻一眼历史」也被计入会话数，
    把仪表盘刷虚（query_stats 直接 COUNT(*) sessions）。"""
    client, hdr, uid = api
    convos.append_turn("sid-1", uid, "问题", "答案")
    before = _sessions_count()

    for _ in range(5):
        r = client.get("/api/conversations/sid-1", headers=hdr)
        assert r.status_code == 200

    assert _sessions_count() == before, "被动回放不该新增审计会话"
    assert "sid-1" not in webapp._sessions, "回放不该顺手水合出内存会话"


def test_replay_still_reports_resumable(api):
    client, hdr, uid = api
    convos.append_turn("sid-1", uid, "问题", "答案")
    j = client.get("/api/conversations/sid-1", headers=hdr).json
    # 水合是惰性的：会话还在就一定能续聊，真正建 Toolbox/Audit 推迟到 /api/ask
    assert j["resumable"] is True
    assert [t["question"] for t in j["turns"]] == ["问题"]


def test_other_users_conversation_is_404(api):
    client, hdr, _ = api
    other = auth.create_user("bob", "pw-bob-12345")["id"]
    convos.append_turn("sid-bob", other, "别人的问题", "别人的答案")
    assert client.get("/api/conversations/sid-bob", headers=hdr).status_code == 404


def test_deleted_conversation_is_404(api):
    client, hdr, uid = api
    convos.append_turn("sid-1", uid, "问题", "答案")
    assert client.delete("/api/conversations/sid-1", headers=hdr).status_code == 200
    assert client.get("/api/conversations/sid-1", headers=hdr).status_code == 404
