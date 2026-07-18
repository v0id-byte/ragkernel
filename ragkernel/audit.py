"""审计：谁来问过什么（会话 + 事件）。单库外的独立 audit.db。"""

import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, started_at INTEGER, client TEXT,
  ip TEXT, fingerprint TEXT, user_agent TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, session_id INTEGER, ts INTEGER, kind TEXT, payload_json TEXT);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "audit.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class Audit:
    """一个会话一条 session 记录；实例本身可作 audit(kind, payload) 回调。"""

    def __init__(self, client: str = "cli", ip: str = "", fingerprint: str = "", user_agent: str = ""):
        self.db = connect()
        cur = self.db.execute(
            "INSERT INTO sessions(started_at, client, ip, fingerprint, user_agent) VALUES(?,?,?,?,?)",
            (int(time.time()), client, ip or None, fingerprint or None, user_agent or None),
        )
        self.session_id = cur.lastrowid
        self.db.commit()

    def __call__(self, kind: str, payload: dict):
        self.db.execute(
            "INSERT INTO events(session_id, ts, kind, payload_json) VALUES(?,?,?,?)",
            (self.session_id, int(time.time()), kind, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        self.db.commit()


def query_stats(days: int = 14) -> dict:
    """提问量统计：总数 / 会话数 / 近 N 天按天。"""
    db = connect()
    total = db.execute("SELECT COUNT(*) FROM events WHERE kind='question'").fetchone()[0]
    sessions = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    rows = db.execute(
        "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d, COUNT(*) n "
        "FROM events WHERE kind='question' AND ts >= strftime('%s','now',? ) "
        "GROUP BY d ORDER BY d",
        (f"-{int(days)} days",),
    ).fetchall()
    return {"total": total, "sessions": sessions, "by_day": [{"d": r["d"], "n": r["n"]} for r in rows]}


def recent_visits(limit: int = 40) -> str:
    db = connect()
    rows = list(db.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)))
    if not rows:
        return "（还没有访问记录）"
    out = []
    for s in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
        out.append(f"◆ {when} · via {s['client']} · IP {s['ip'] or '?'}")
        for e in db.execute("SELECT kind, payload_json FROM events WHERE session_id=? ORDER BY id", (s["id"],)):
            p = json.loads(e["payload_json"])
            if e["kind"] == "question":
                out.append(f"    问：{p.get('question', '')}")
            elif e["kind"] == "answer":
                out.append(f"    答（{p.get('model', '')}）：{(p.get('summary') or '')[:80]}")
            elif e["kind"].startswith("tool:"):
                out.append(f"    {e['kind'][5:]}：{p.get('query', '')}")
    return "\n".join(out)
