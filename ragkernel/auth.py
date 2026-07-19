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
  created_at INTEGER, expires_at INTEGER, last_seen_at INTEGER
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


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.data_dir() / "auth.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
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


def issue_token(user_id: int) -> str:
    token = secrets.token_hex(32)
    now = int(time.time())
    db = connect()
    db.execute(
        "INSERT INTO tokens(token_hash, user_id, created_at, expires_at, last_seen_at) VALUES(?,?,?,?,?)",
        (_hash_token(token), user_id, now, now + _token_ttl_days() * 86400, now),
    )
    db.commit()
    return token


def revoke_token(token: str):
    db = connect()
    db.execute("DELETE FROM tokens WHERE token_hash=?", (_hash_token(token),))
    db.commit()


def resolve_token(token: str) -> dict | None:
    """有效 token → 对应用户（连带 is_active 检查）；顺手刷新 last_seen_at。"""
    db = connect()
    now = int(time.time())
    row = db.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash=? AND t.expires_at > ? AND u.is_active=1",
        (_hash_token(token), now),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE tokens SET last_seen_at=? WHERE token_hash=?", (now, _hash_token(token)))
    db.commit()
    return dict(row)


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
