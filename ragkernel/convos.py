"""历史会话：把对话持久化下来，刷新/重启后还能回放并续聊。独立的 convos.db。

设计取舍：这里存的是「事实」（问了什么、答了什么、引了哪些证据），不是 agent 的运行态。
agent 的 messages 里混着各家 SDK 的 block 对象与工具调用方言（见 backends.py），
存下来既不好序列化，换个 provider 也会读不回来。恢复上下文时按纯文本重建即可。
"""

import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
  title TEXT, last_question TEXT,
  created_at INTEGER, updated_at INTEGER,
  turns INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, conv_id TEXT NOT NULL, ts INTEGER,
  question TEXT, answer TEXT, citations_json TEXT, model TEXT,
  has_image INTEGER DEFAULT 0, latency_ms INTEGER, type TEXT DEFAULT 'qa');
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);
"""

TITLE_MAX = 60
# 纯图片提问的 question 是空串（图片不入库）。重建历史时拿它占位，保住 user/assistant 交替。
PHOTO_QUESTION = "（看图提问）"


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "convos.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def append_turn(conv_id: str, user_id: int, question: str, answer: str,
                citations: list | None = None, model: str = "",
                has_image: bool = False, latency_ms: int | None = None) -> bool:
    """记一轮问答。会话行不存在则建（标题取首个问题），存在则只刷新预览/时间/计数。

    整体一个事务：conversations.turns 与 messages 行数必须同生同死，
    否则列表里的轮数会永久对不上实际条数。

    会话已被软删则整轮丢弃（返回 False）。用户删掉当前会话时，它的 /api/ask worker 可能
    还在跑——线程停不下来，只能在写入这一侧挡：否则这轮会挂在一个 deleted=1 的会话下，
    列表和详情都看不到（两处都过滤 deleted=0），却实实在在留在库里，与「删除」的承诺相悖，
    连带把死会话的 turns 越刷越大。
    """
    now = int(time.time())
    title = (question or "").strip()[:TITLE_MAX]
    db = connect()
    with db:
        cur = db.execute(
            "INSERT INTO conversations(id, user_id, title, last_question, created_at, updated_at, turns) "
            "VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET last_question=excluded.last_question, "
            "  updated_at=excluded.updated_at, turns=turns+1 "  # 有意不含 title：标题只在建行时定一次
            "  WHERE conversations.deleted=0",
            (conv_id, user_id, title, title, now, now),
        )
        if not cur.rowcount:  # 冲突且 WHERE 不成立 = 会话已删，消息也不能落
            return False
        db.execute(
            "INSERT INTO messages(conv_id, ts, question, answer, citations_json, model, has_image, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (conv_id, now, question, answer,
             json.dumps(citations or [], ensure_ascii=False, default=str),
             model or None, 1 if has_image else 0, latency_ms),
        )
    return True


def get_conv(conv_id: str, user_id: int) -> dict | None:
    """会话详情。不属于该用户的一律当作不存在。"""
    db = connect()
    row = db.execute(
        "SELECT * FROM conversations WHERE id=? AND user_id=? AND deleted=0", (conv_id, user_id)
    ).fetchone()
    if not row:
        return None
    rows = list(db.execute(
        "SELECT question, answer, citations_json, model, has_image, ts FROM messages "
        "WHERE conv_id=? ORDER BY id", (conv_id,)
    ))
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "turns": [
            {
                "question": r["question"] or "",
                "answer": r["answer"] or "",
                "citations": json.loads(r["citations_json"] or "[]"),
                "model": r["model"] or "",
                "has_image": bool(r["has_image"]),
                "ts": r["ts"],
            }
            for r in rows
        ],
    }


def build_history(turns: list[dict], max_messages: int = 20) -> list[dict]:
    """把存下来的问答还原成 agent 能吃的纯文本 history。

    只保留对话线索，丢掉工具调用细节 —— 模型需要的是「上次聊到哪」，不是重看一遍检索过程。
    这个形状 Anthropic / OpenAI 两种方言都认，所以中途换 provider 也不会炸。

    **必须严格 user/assistant 交替、且以 user 开头**：逐条判断"有就加"会在两种情况下塌掉
    —— 纯图片提问库里存的 question 是空串（图片本身不入库），只加 assistant 就让历史以
    assistant 开头或出现相邻 assistant；答案为空的失败轮次则会连出两条 user。严格的
    Anthropic 兼容后端对这两种都直接报错。所以这里按「轮」而非按「条」来放。
    """
    history: list[dict] = []
    for m in turns[-(max_messages // 2):]:
        answer = (m.get("answer") or "").strip()
        if not answer:
            continue  # 没答出来的轮次整轮丢弃，留下孤立的 user 只会打断交替
        question = (m.get("question") or "").strip()
        if not question:
            if not m.get("has_image"):
                continue  # 既无问题也无图片，这轮没有任何 user 内容可还原
            question = PHOTO_QUESTION  # 保住轮次结构，也保住"这轮是看图问的"这点上下文
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
    return history


def list_convs(user_id: int, q: str = "", limit: int = 100) -> list[dict]:
    """会话列表。q 非空时按问题/答案/引用过滤 —— 引用也要搜，因为型号往往只出现在证据里。"""
    db = connect()
    sql = ("SELECT id, title, last_question, updated_at, turns FROM conversations "
           "WHERE user_id=? AND deleted=0")
    args: list = [user_id]
    if q:
        sql += (" AND id IN (SELECT conv_id FROM messages WHERE question LIKE ? "
                "OR answer LIKE ? OR citations_json LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in db.execute(sql, args)]


def rename(conv_id: str, user_id: int, title: str) -> bool:
    db = connect()
    with db:
        cur = db.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=? AND deleted=0",
                         (title.strip()[:80], conv_id, user_id))
    return cur.rowcount > 0


def delete_conv(conv_id: str, user_id: int) -> bool:
    """会话行软删留痕，消息硬删 —— 否则删过的对话正文会永久堆在库里。"""
    db = connect()
    with db:
        cur = db.execute("UPDATE conversations SET deleted=1 WHERE id=? AND user_id=? AND deleted=0",
                         (conv_id, user_id))
        if cur.rowcount:
            db.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
    return cur.rowcount > 0
