"""读取 config/settings.yaml 与 .env。单库、单租户，无 tier/persona。"""

import os
import sqlite3
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# 数据库兼容性标记。**声明值，不是迁移驱动**——store/auth 的 _migrate 靠 PRAGMA 内省，
# 幂等且不看这个数；它存在只为让「这个版本的数据形态」可被外部比较：manifest 用它声明
# 目标版本要求，升级前的兼容性闸门拿它和本机比。改动数据形态（加表/改列语义）时手动 +1。
SCHEMA_VERSION = 1

# ── .ragkernel/ 部署元数据 ──────────────────────────────────
# 分层是为了不长成状态垃圾桶：state/ 是要持久的事实，cache/ 删了能重建，locks/ 是并发控制。
# 早期版本平铺在 .ragkernel/ 下（install.json、setup.lock），所以读取端一律回落到旧路径，
# 写入端只写新路径；install.sh 负责把存量文件迁过来。
#
# 路径一律**调用时**从 ROOT 算，不缓存成模块常量——测试靠 monkeypatch ROOT 隔离到 tmp_path，
# import 期定死的常量会绕过它，把测试写进真实仓库目录。
_RK_KINDS = ("state", "cache", "locks")


def rk_dir() -> Path:
    return ROOT / ".ragkernel"


def rk_path(kind: str, name: str, *, create: bool = False) -> Path:
    """.ragkernel/<kind>/<name>。create=True 才建目录——doctor 这类零副作用场景绝不能建。"""
    assert kind in _RK_KINDS, kind
    d = rk_dir() / kind
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / name


def rk_read_path(kind: str, name: str) -> Path | None:
    """读取端：新路径优先，回落旧的平铺路径；都没有返回 None。不产生任何副作用。"""
    new = rk_path(kind, name)
    if new.exists():
        return new
    legacy = rk_dir() / name
    return legacy if legacy.exists() else None


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
    # RAGKERNEL_DATA_DIR 可覆盖数据目录（供评测/测试隔离到临时库，不污染用户 KB）。
    override = os.environ.get("RAGKERNEL_DATA_DIR")
    d = Path(override) if override else ROOT / settings().get("data_dir", "data")
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


def get_provider_override_ro() -> dict:
    """只读版：库不存在就返回空、**绝不创建**（`_settings_db`/`data_dir` 会 mkdir + 建表）。
    供 doctor 等必须零副作用的场景——诊断系统不该改变被诊断的系统。"""
    override = os.environ.get("RAGKERNEL_DATA_DIR")
    d = Path(override) if override else ROOT / settings().get("data_dir", "data")
    p = d / "settings.db"
    if not p.exists():
        return {}
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM provider_override WHERE id=1").fetchone()
        return dict(row) if row else {}
    except sqlite3.OperationalError:  # 库在但表还没建
        return {}
    finally:
        db.close()


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


def provider(readonly: bool = False) -> dict:
    """LLM provider 配置：{base_url, model, api_key_env, max_tokens[, api_key]}。

    先取 config/settings.yaml 的默认值，再叠加后台设置页存的运行时覆盖（若有）。
    override 里的 api_key 是明文密钥（不是 api_key_env 那层间接），backends.py 里优先用它。

    readonly=True：读覆盖时绝不创建 settings.db（供 doctor 等零副作用场景）。
    """
    prov = dict(settings().get("provider") or {})
    prov.setdefault("kind", "anthropic")  # anthropic | openai
    prov.setdefault("model", "claude-sonnet-5")
    prov.setdefault("api_key_env", "ANTHROPIC_API_KEY")
    prov.setdefault("max_tokens", 8000)
    override = get_provider_override_ro() if readonly else get_provider_override()
    for k in ("kind", "model"):
        if override.get(k):
            prov[k] = override[k]
    # base_url 特殊：空串是**有意义**的（官方 Claude 用空 base_url = SDK 默认 host）。
    # 用 `is not None` 区分「显式设空」与「NULL/未设」——否则选官方 Claude 时，
    # 已存的非空 base_url（如上一个 MiniMax 覆盖）清不掉，请求仍打到旧 host。
    if override.get("base_url") is not None:
        prov["base_url"] = override["base_url"]
    if override.get("max_tokens"):
        prov["max_tokens"] = override["max_tokens"]
    if override.get("api_key"):
        prov["api_key"] = override["api_key"]
    return prov
