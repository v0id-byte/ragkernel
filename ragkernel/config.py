"""读取 config/settings.yaml 与 .env。单库、单租户，无 tier/persona。"""

import os
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


def provider() -> dict:
    """LLM provider 配置：{base_url, model, api_key_env, max_tokens}。"""
    prov = dict(settings().get("provider") or {})
    prov.setdefault("model", "claude-sonnet-5")
    prov.setdefault("api_key_env", "ANTHROPIC_API_KEY")
    prov.setdefault("max_tokens", 8000)
    return prov
