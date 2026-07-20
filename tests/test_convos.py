"""历史会话的持久化边界（PR #7 Codex 评审的三条 + 冷启动水合竞争）。"""

import threading

import pytest

from ragkernel import convos


@pytest.fixture
def convo_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    return convos.connect()


# ── 不往已软删的会话里追加 ─────────────────────────────────────

def test_append_turn_creates_and_appends(convo_db):
    assert convos.append_turn("c1", 1, "问题一", "答案一") is True
    assert convos.append_turn("c1", 1, "问题二", "答案二") is True
    conv = convos.get_conv("c1", 1)
    assert len(conv["turns"]) == 2
    assert conv["title"] == "问题一"  # 标题只在建行时定一次


def test_append_turn_refuses_deleted_conversation(convo_db):
    """删掉当前会话时它的 /api/ask worker 可能还在跑——线程停不下来，只能在写入侧挡。"""
    convos.append_turn("c1", 1, "问题一", "答案一")
    convos.delete_conv("c1", 1)

    assert convos.append_turn("c1", 1, "worker 收尾写的", "答案二") is False

    # 消息没落库、死会话的 turns 也没被刷大
    rows = convo_db.execute("SELECT COUNT(*) FROM messages WHERE conv_id='c1'").fetchone()[0]
    assert rows == 0
    row = convo_db.execute("SELECT turns, deleted FROM conversations WHERE id='c1'").fetchone()
    assert row["deleted"] == 1 and row["turns"] == 1

    assert convos.get_conv("c1", 1) is None
    assert convos.list_convs(1) == []


def test_deleted_conversation_stays_deleted_in_listing(convo_db):
    """回归：被拒的追加不能让会话在列表里'复活'。"""
    convos.append_turn("c1", 1, "问题", "答案")
    convos.delete_conv("c1", 1)
    convos.append_turn("c1", 1, "又一轮", "又一答")
    assert [c["id"] for c in convos.list_convs(1)] == []


# ── build_history 必须严格交替且以 user 开头 ───────────────────

def _roles(history):
    return [m["role"] for m in history]


def test_history_alternates_for_normal_turns(convo_db):
    h = convos.build_history([{"question": "q1", "answer": "a1"}, {"question": "q2", "answer": "a2"}])
    assert _roles(h) == ["user", "assistant", "user", "assistant"]


def test_image_only_question_keeps_user_turn(convo_db):
    """纯图片提问库里存的 question 是空串。只加 assistant 会让历史以 assistant 开头，
    严格的 Anthropic 兼容后端直接报错。"""
    h = convos.build_history([{"question": "", "answer": "看图答案", "has_image": True}])
    assert _roles(h) == ["user", "assistant"]
    assert h[0]["content"] == convos.PHOTO_QUESTION


def test_image_only_then_text_has_no_adjacent_assistants(convo_db):
    h = convos.build_history([
        {"question": "", "answer": "看图答案", "has_image": True},
        {"question": "那这个型号呢", "answer": "型号答案"},
    ])
    assert _roles(h) == ["user", "assistant", "user", "assistant"]


def test_answerless_turn_dropped_whole(convo_db):
    """答案为空的失败轮次整轮丢弃，否则会连出两条 user。"""
    h = convos.build_history([
        {"question": "问了但没答上", "answer": ""},
        {"question": "q2", "answer": "a2"},
    ])
    assert _roles(h) == ["user", "assistant"]
    assert h[0]["content"] == "q2"


def test_turn_with_neither_question_nor_image_dropped(convo_db):
    assert convos.build_history([{"question": "", "answer": "a", "has_image": False}]) == []


@pytest.mark.parametrize("turns", [
    [{"question": "", "answer": "a1", "has_image": True}],
    [{"question": "", "answer": "a1", "has_image": True}, {"question": "", "answer": "a2", "has_image": True}],
    [{"question": "q1", "answer": ""}, {"question": "", "answer": "a2", "has_image": True}],
])
def test_history_always_starts_with_user_and_alternates(convo_db, turns):
    roles = _roles(convos.build_history(turns))
    assert not roles or roles[0] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))


# ── 冷启动水合：同一 sid 只许建一个会话对象 ────────────────────

def test_cold_hydration_is_serialized(tmp_path, monkeypatch):
    """两个标签页在重启后同时提问，不能各建一套 Toolbox/Audit 和各自的 busy 锁——
    那样两个 /api/ask worker 就能并发改同一段历史。Audit 一构造就写一条 sessions 记录，
    所以必须在**构造前**收口，"先建后弃"会留下幽灵审计记录。
    """
    monkeypatch.setenv("RAGKERNEL_DATA_DIR", str(tmp_path))
    from ragkernel import audit, auth, webapp

    uid = auth.create_user("alice", "pw-alice-123")["id"]
    convos.append_turn("sid-1", uid, "问题", "答案")

    results, start = [], threading.Barrier(6)

    def worker():
        start.wait()
        with webapp.app.test_request_context():
            from flask import g
            g.user = {"id": uid, "username": "alice", "is_admin": 0}
            results.append(webapp.resolve_session("sid-1"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(r is results[0] for r in results), "同一 sid 必须共用一个会话对象"
    assert len({id(r["busy"]) for r in results}) == 1, "busy 锁必须只有一把"

    n = audit.connect().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert n == 1, f"只该写一条 audit sessions，实际 {n} 条（幽灵审计记录）"
    assert not webapp._hydrating, "水合锁表不该残留"
