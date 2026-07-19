"""读取 config/settings.yaml 与 .env。单库、单租户，无 tier/persona。"""

import os
import sqlite3
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_env():
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def settings() -> dict:
    p = CONFIG_DIR / "settings.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def data_dir() -> Path:
    d = ROOT / settings().get("data_dir", "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _settings_db() -> sqlite3.Connection:
    """后台「AI 服务提供方」设置页写的运行时覆盖——独立小库，风格同 auth.db/audit.db。"""
    db = sqlite3.connect(data_dir() / "settings.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS provider_override(
          id INTEGER PRIMARY KEY CHECK (id = 1),
          kind TEXT, base_url TEXT, model TEXT, api_key TEXT, max_tokens INTEGER,
          updated_at INTEGER
        );
    """)
    return db


def get_provider_override() -> dict:
    row = _settings_db().execute("SELECT * FROM provider_override WHERE id=1").fetchone()
    return dict(row) if row else {}


def set_provider_override(kind: str, base_url: str, model: str, max_tokens: int, api_key: str | None):
    """api_key=None（表单留空）= 不改已存的密钥，只更新其余字段。"""
    db = _settings_db()
    final_key = api_key if api_key else get_provider_override().get("api_key")
    db.execute(
        "INSERT INTO provider_override(id, kind, base_url, model, api_key, max_tokens, updated_at) "
        "VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "kind=excluded.kind, base_url=excluded.base_url, model=excluded.model, "
        "api_key=excluded.api_key, max_tokens=excluded.max_tokens, updated_at=excluded.updated_at",
        (kind, base_url, model, final_key, max_tokens, int(time.time())),
    )
    db.commit()


def clear_provider_override():
    db = _settings_db()
    db.execute("DELETE FROM provider_override WHERE id=1")
    db.commit()


def provider() -> dict:
    """LLM provider 配置：{base_url, model, api_key_env, max_tokens[, api_key]}。

    先取 config/settings.yaml 的默认值，再叠加后台设置页存的运行时覆盖（若有）。
    override 里的 api_key 是明文密钥（不是 api_key_env 那层间接），backends.py 里优先用它。
    """
    prov = dict(settings().get("provider") or {})
    prov.setdefault("kind", "anthropic")  # anthropic | openai
    prov.setdefault("model", "claude-sonnet-5")
    prov.setdefault("api_key_env", "ANTHROPIC_API_KEY")
    prov.setdefault("max_tokens", 8000)
    override = get_provider_override()
    for k in ("kind", "base_url", "model"):
        if override.get(k):
            prov[k] = override[k]
    if override.get("max_tokens"):
        prov["max_tokens"] = override["max_tokens"]
    if override.get("api_key"):
        prov["api_key"] = override["api_key"]
    return prov
