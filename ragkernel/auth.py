"""登录鉴权：users/tokens 独立小库（data/auth.db）。密码走 werkzeug 哈希，token 不透明可撤销。"""

import hashlib
import secrets
import sqlite3
import time
from functools import wraps

from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT, display_name TEXT, created_at INTEGER,
  is_active INTEGER DEFAULT 1, is_admin INTEGER DEFAULT 0,
  setup_code_hash TEXT, setup_code_expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS tokens(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
  created_at INTEGER, expires_at INTEGER, last_seen_at INTEGER,
  label TEXT, token_kind TEXT NOT NULL DEFAULT 'session',
  UNIQUE(user_id, token_kind, label)
);
"""


def _migrate(db: sqlite3.Connection):
    """待激活账号（无密码，靠一次性建号口令首登设密码）需要 password_hash 可空 + 两个新列。
    对已存在的旧库（password_hash 建表时是 NOT NULL）做一次性重建；新库靠上面的 SCHEMA 直接就是对的。
    """
    cols = {r["name"]: r for r in db.execute("PRAGMA table_info(users)")}
    if "setup_code_hash" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN setup_code_hash TEXT")
        db.execute("ALTER TABLE users ADD COLUMN setup_code_expires_at INTEGER")
        db.commit()
        cols = {r["name"]: r for r in db.execute("PRAGMA table_info(users)")}
    if cols["password_hash"]["notnull"]:
        db.executescript("""
            ALTER TABLE users RENAME TO users_old;
            CREATE TABLE users(
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              password_hash TEXT, display_name TEXT, created_at INTEGER,
              is_active INTEGER DEFAULT 1, is_admin INTEGER DEFAULT 0,
              setup_code_hash TEXT, setup_code_expires_at INTEGER
            );
            INSERT INTO users(id, username, password_hash, display_name, created_at,
                               is_active, is_admin, setup_code_hash, setup_code_expires_at)
              SELECT id, username, password_hash, display_name, created_at,
                     is_active, is_admin, setup_code_hash, setup_code_expires_at FROM users_old;
            DROP TABLE users_old;
        """)
        db.commit()
    # agent token（MCP 用）：给旧库的 tokens 补 label + token_kind 列并加 UNIQUE(user_id,token_kind,label)。
    # 旧登录 token 一律成 token_kind='session'（label 空）——天然被 MCP 的 resolve_agent_token 拒之门外。
    tcols = {r["name"] for r in db.execute("PRAGMA table_info(tokens)")}
    if "token_kind" not in tcols:
        db.executescript("""
            ALTER TABLE tokens RENAME TO tokens_old;
            CREATE TABLE tokens(
              token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
              created_at INTEGER, expires_at INTEGER, last_seen_at INTEGER,
              label TEXT, token_kind TEXT NOT NULL DEFAULT 'session',
              UNIQUE(user_id, token_kind, label)
            );
            INSERT INTO tokens(token_hash, user_id, created_at, expires_at, last_seen_at)
              SELECT token_hash, user_id, created_at, expires_at, last_seen_at FROM tokens_old;
            DROP TABLE tokens_old;
        """)
        db.commit()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "auth.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")  # MCP HTTP 每请求刷 last_seen，并发写让其等待重试而非直接报 locked
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _token_ttl_days() -> int:
    return int((config.settings().get("auth") or {}).get("token_ttl_days", 30))


def _setup_code_ttl_days() -> int:
    return int((config.settings().get("auth") or {}).get("setup_code_ttl_days", 7))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── 账号管理（CLI + 后台管理页面共用）────────────────────────────

def create_user(username: str, password: str | None, is_admin: bool = False, display_name: str = "") -> dict:
    """password=None 建为待激活账号（无密码），生成一次性建号口令交给本人首登时设密码。

    返回 {"id": int, "setup_code": str | None}——setup_code 只在创建时返回这一次，库里只存哈希。
    """
    db = connect()
    now = int(time.time())
    if password:
        pwhash, setup_code, code_hash, expires = generate_password_hash(password), None, None, None
    else:
        pwhash = None
        setup_code = secrets.token_hex(4)
        code_hash = _hash_token(setup_code)
        expires = now + _setup_code_ttl_days() * 86400
    cur = db.execute(
        "INSERT INTO users(username, password_hash, display_name, created_at, is_admin, "
        "setup_code_hash, setup_code_expires_at) VALUES(?,?,?,?,?,?,?)",
        (username, pwhash, display_name or username, now, int(is_admin), code_hash, expires),
    )
    db.commit()
    return {"id": cur.lastrowid, "setup_code": setup_code}


def list_users() -> list[dict]:
    db = connect()
    rows = db.execute(
        "SELECT id, username, display_name, is_active, is_admin, created_at, "
        "password_hash IS NULL AS needs_setup FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def usernames_by_ids(ids) -> dict[int, str]:
    """id → username。documents.owner_id 在 ragkernel.db、用户名在 auth.db，跨库没法 join，
    只能取回来在 Python 里映射。"""
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    db = connect()
    rows = db.execute(f"SELECT id, username FROM users WHERE id IN ({ph})", ids).fetchall()
    return {r["id"]: r["username"] for r in rows}


def set_active(user_id: int, active: bool):
    db = connect()
    db.execute("UPDATE users SET is_active=? WHERE id=?", (int(active), user_id))
    db.commit()


# ── 登录 / token ─────────────────────────────────────────────────

def user_status(username: str) -> dict | None:
    """两步登录第一步：账号不存在 → None；存在 → {"needs_setup": bool}（无密码=待激活，需走建号口令）。"""
    db = connect()
    row = db.execute(
        "SELECT password_hash FROM users WHERE username=? AND is_active=1", (username,)
    ).fetchone()
    if not row:
        return None
    return {"needs_setup": row["password_hash"] is None}


def authenticate(username: str, password: str) -> dict | None:
    db = connect()
    row = db.execute("SELECT * FROM users WHERE username=? AND is_active=1", (username,)).fetchone()
    if not row or not row["password_hash"] or not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


def setup_password(username: str, setup_code: str, password: str) -> dict | None:
    """校验一次性建号口令并设置密码（仅对尚未设密码的待激活账号有效）；成功返回用户 dict，失败 None。"""
    db = connect()
    now = int(time.time())
    row = db.execute(
        "SELECT * FROM users WHERE username=? AND is_active=1 AND password_hash IS NULL", (username,)
    ).fetchone()
    if not row or not row["setup_code_hash"] or not setup_code:
        return None
    if row["setup_code_hash"] != _hash_token(setup_code):
        return None
    if not row["setup_code_expires_at"] or row["setup_code_expires_at"] < now:
        return None
    db.execute(
        "UPDATE users SET password_hash=?, setup_code_hash=NULL, setup_code_expires_at=NULL WHERE id=?",
        (generate_password_hash(password), row["id"]),
    )
    db.commit()
    return dict(row)


def user_id_by_username(username: str) -> int | None:
    db = connect()
    row = db.execute("SELECT id FROM users WHERE username=? AND is_active=1", (username,)).fetchone()
    return row["id"] if row else None


def issue_token(user_id: int, ttl_days: int | None = None, *, label: str | None = None,
                token_kind: str = "session") -> str:
    """签发不透明 token（只回原始值这一次，库里只存 sha256）。
    web 登录用默认（session / 30 天 / 无 label）；MCP agent token 传 token_kind='agent' + label + 长 ttl。"""
    token = secrets.token_hex(32)
    now = int(time.time())
    days = ttl_days if ttl_days is not None else _token_ttl_days()
    db = connect()
    db.execute(
        "INSERT INTO tokens(token_hash, user_id, created_at, expires_at, last_seen_at, label, token_kind) "
        "VALUES(?,?,?,?,?,?,?)",
        (_hash_token(token), user_id, now, now + days * 86400, now, label, token_kind),
    )
    db.commit()
    return token


def revoke_token(token: str):
    db = connect()
    db.execute("DELETE FROM tokens WHERE token_hash=?", (_hash_token(token),))
    db.commit()


def resolve_token(token: str) -> dict | None:
    """有效 **session** token → 对应用户（连带 is_active 检查）；顺手刷新 last_seen_at。
    **只认 token_kind='session'**——agent token（MCP 用的长效 PAT）绝不能拿来过 @require_auth 走
    web 的上传/删除/管理接口（边界必须双向：web token 进不了 MCP，agent token 也进不了 web）。"""
    db = connect()
    now = int(time.time())
    row = db.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash=? AND t.token_kind='session' AND t.expires_at > ? AND u.is_active=1",
        (_hash_token(token), now),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE tokens SET last_seen_at=? WHERE token_hash=?", (now, _hash_token(token)))
    db.commit()
    return dict(row)


# ── Agent token（MCP 专用，只读网关鉴权）────────────────────────────

def resolve_agent_token(token: str) -> dict | None:
    """有效且 token_kind='agent' 的 token → 用户 + token 元数据（token_label/token_kind/token_hash）；
    顺手刷新 last_seen。**只认 agent kind**——web 登录 token（session）一律返回 None，进不了 MCP。"""
    db = connect()
    now = int(time.time())
    th = _hash_token(token)
    row = db.execute(
        "SELECT u.*, t.label AS token_label, t.token_kind AS token_kind, t.token_hash AS token_hash "
        "FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash=? AND t.token_kind='agent' AND t.expires_at > ? AND u.is_active=1",
        (th, now),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE tokens SET last_seen_at=? WHERE token_hash=?", (now, th))
    db.commit()
    return dict(row)


def list_tokens(username: str | None = None) -> list[dict]:
    """列出 agent token（不含原始 token；id_short=hash 前 8 位，供撤销引用）。"""
    db = connect()
    sql = ("SELECT t.token_hash, t.label, t.token_kind, t.created_at, t.expires_at, t.last_seen_at, "
           "u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token_kind='agent'")
    params: tuple = ()
    if username:
        sql += " AND u.username=?"
        params = (username,)
    sql += " ORDER BY t.created_at DESC"
    out = []
    for r in db.execute(sql, params).fetchall():
        d = dict(r)
        d["id_short"] = d.pop("token_hash")[:8]
        out.append(d)
    return out


def revoke_agent_token(user_id: int | None = None, label: str | None = None,
                       hash_prefix: str | None = None) -> dict:
    """撤销一个 agent token：要么 (user_id + label) 精确，要么 hash 前缀(≥8 位)。
    必须**恰好命中一个**——命中 0 或多个都拒绝（返回 error），避免误撤。"""
    db = connect()
    if hash_prefix:
        if len(hash_prefix) < 8:
            return {"deleted": 0, "error": "hash 前缀至少 8 位"}
        rows = db.execute(
            "SELECT token_hash FROM tokens WHERE token_kind='agent' AND token_hash LIKE ?",
            (hash_prefix + "%",),
        ).fetchall()
    elif user_id is not None and label:
        rows = db.execute(
            "SELECT token_hash FROM tokens WHERE token_kind='agent' AND user_id=? AND label=?",
            (user_id, label),
        ).fetchall()
    else:
        return {"deleted": 0, "error": "需要 (--user + label) 或 hash 前缀"}
    if not rows:
        return {"deleted": 0, "error": "未命中任何 agent token"}
    if len(rows) > 1:
        return {"deleted": 0, "error": f"命中 {len(rows)} 个，用更长前缀或加 --user 精确",
                "matches": [r["token_hash"][:8] for r in rows]}
    db.execute("DELETE FROM tokens WHERE token_hash=?", (rows[0]["token_hash"],))
    db.commit()
    return {"deleted": 1, "id_short": rows[0]["token_hash"][:8]}


# ── Flask 装饰器 ─────────────────────────────────────────────────

def require_auth(fn):
    """校验 Authorization: Bearer <token>，通过后把用户挂到 g.user。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        user = resolve_token(token) if token else None
        if not user:
            return jsonify({"error": "未登录或登录已过期"}), 401
        g.user = user
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    """需先过 require_auth；非管理员一律 403。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(g, "user", None) or not g.user.get("is_admin"):
            return jsonify({"error": "无权限"}), 403
        return fn(*args, **kwargs)

    return wrapper


def require_admin_ip(fn):
    """按 config admin.allowed_ips 白名单放行；本机回环地址始终放行，不占用配置。

    刻意用 request.remote_addr（TCP 对端地址）而非 X-Forwarded-For/CF-Connecting-IP 之类可由客户端
    随意伪造的请求头——这是访问控制而非限流，伪造请求头绕过白名单是真实的安全问题（已用
    `curl -H "X-Forwarded-For: 127.0.0.1"` 验证过：换 _client_ip() 会被直接绕过）。
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or ""
        allowed = set((config.settings().get("admin") or {}).get("allowed_ips") or [])
        if ip not in allowed and ip not in ("127.0.0.1", "::1", "localhost"):
            return jsonify({"error": "禁止访问"}), 403
        return fn(*args, **kwargs)

    return wrapper
