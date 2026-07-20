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


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "convos.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def append_turn(conv_id: str, user_id: int, question: str, answer: str,
                citations: list | None = None, model: str = "",
                has_image: bool = False, latency_ms: int | None = None) -> None:
    """记一轮问答。会话行不存在则建（标题取首个问题），存在则只刷新预览/时间/计数。

    整体一个事务：conversations.turns 与 messages 行数必须同生同死，
    否则列表里的轮数会永久对不上实际条数。
    """
    now = int(time.time())
    title = (question or "").strip()[:TITLE_MAX]
    db = connect()
    with db:
        db.execute(
            "INSERT INTO conversations(id, user_id, title, last_question, created_at, updated_at, turns) "
            "VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET last_question=excluded.last_question, "
            "  updated_at=excluded.updated_at, turns=turns+1",  # 有意不含 title：标题只在建行时定一次
            (conv_id, user_id, title, title, now, now),
        )
        db.execute(
            "INSERT INTO messages(conv_id, ts, question, answer, citations_json, model, has_image, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (conv_id, now, question, answer,
             json.dumps(citations or [], ensure_ascii=False, default=str),
             model or None, 1 if has_image else 0, latency_ms),
        )


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
    """
    history: list[dict] = []
    for m in turns[-(max_messages // 2):]:
        if m.get("question"):
            history.append({"role": "user", "content": m["question"]})
        if m.get("answer"):
            history.append({"role": "assistant", "content": m["answer"]})
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
