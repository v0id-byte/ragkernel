"""生成并校验 channel manifest —— 升级链路的可信根。

    python scripts/gen_manifest.py --tag v0.3.0 --out dist/stable.json

客户端不查 GitHub 的 `releases/latest`（它隐含「非 draft 且非 prerelease」，有了
beta/rc 之后行为不可控），而是查这份 manifest。所以它必须**由 CI 生成、不许手写**：
手改 stable.json 写成 0.4.0 而 Release 还是 0.3.0，升级器会带着所有客户一起走错，
且这是整条链上唯一无法靠客户端补救的错误。

三方一致性（tag == pyproject == manifest）在这里断言。只验 tag 不够——manifest 是
第三个可能撒谎的地方。
"""

import argparse
import json
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# 指向仓库里那份 schema 的可解析地址。企业自建 endpoint 要拿它校验自己的产物，
# 写一个解析不了的 URL 等于没写。这是文档性引用、不是客户端查询的 endpoint，
# 所以用 raw 就够（迁移压力在 DEFAULT_ENDPOINT 那边，不在这里）。
SCHEMA_URL = "https://raw.githubusercontent.com/v0id-byte/ragkernel/main/docs/schemas/manifest-v1.json"
MANIFEST_SCHEMA_VERSION = 1


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _schema_version() -> int:
    """读 config.SCHEMA_VERSION 但不 import 包——CI 里生成 manifest 时未必装好了依赖，
    而 `import ragkernel.config` 会拖进 yaml/sqlite 等一串东西。"""
    src = (ROOT / "ragkernel" / "config.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("SCHEMA_VERSION"):
            # 去行尾注释再转 int：`SCHEMA_VERSION = 2  # 加了 xxx 表` 是很自然的写法，
            # 而这里一炸就是发版当场失败。
            raw = line.split("=", 1)[1].split("#", 1)[0].strip()
            try:
                return int(raw)
            except ValueError:
                _fail(f"SCHEMA_VERSION 不是整数：{raw!r}")
    _fail("ragkernel/config.py 里找不到 SCHEMA_VERSION")


def _same_version(a: str, b: str) -> bool:
    """按 PEP 440 规范化后比较。

    预发布必须这么比：semver 写 `v0.2.0-rc.1`，而 pyproject / importlib.metadata 里
    是规范化后的 `0.2.0rc1`——裸字符串比较会把一次完全正确的发布判成版本不一致。
    packaging 不可用时退回字符串比较（稳定版发布不受影响）。
    """
    if a == b:
        return True
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return False
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return False


_SECURITY_VALUES = (None, "low", "critical")


def _validate(m: dict) -> None:
    """按 docs/schemas/manifest-v1.json 校验**取值**，不只是字段在不在。

    release.yaml 是每次发布手改的，`security: high`（不在枚举里）或 YAML 把
    `restart_required: "true"` 读成字符串这类笔误，都能通过「顶层字段存在」检查，
    然后发出一份违反自家 schema 的 manifest——企业侧的校验器会直接拒收整个渠道。
    """
    if m.get("security") not in _SECURITY_VALUES:
        _fail(f"release.yaml 的 security 必须是 {_SECURITY_VALUES} 之一，收到 {m.get('security')!r}")

    strategy = m["upgrade_strategy"]
    if not isinstance(strategy, dict):
        _fail(f"upgrade_strategy 必须是对象，收到 {type(strategy).__name__}")
    for key in ("restart_required", "migration_required"):
        if not isinstance(strategy.get(key), bool):
            _fail(f"upgrade_strategy.{key} 必须是布尔值，收到 {strategy.get(key)!r}"
                  "（YAML 里加了引号会变成字符串）")
    downtime = strategy.get("estimated_downtime_seconds")
    if downtime is not None and not isinstance(downtime, int):
        _fail(f"estimated_downtime_seconds 必须是整数或留空，收到 {downtime!r}")

    req = m["requires"]
    if not isinstance(req.get("ragkernel_schema"), int):
        _fail(f"requires.ragkernel_schema 必须是整数，收到 {req.get('ragkernel_schema')!r}")
    for key in ("min_upgradable_from",):
        if not re.match(r"^\d+\.\d+\.\d+", str(req.get(key, ""))):
            _fail(f"requires.{key} 必须是 semver，收到 {req.get(key)!r}")
    mcv = m.get("min_client_version")
    if mcv is not None and not re.match(r"^\d+\.\d+\.\d+", str(mcv)):
        _fail(f"min_client_version 必须是 semver 或留空，收到 {mcv!r}")


def build(tag: str, *, channel: str, repo: str, published_at: str | None) -> dict:
    if not tag.startswith("v"):
        _fail(f"tag 必须形如 v0.3.0，收到 {tag!r}")
    tag_version = tag[1:]

    proj = _pyproject()["project"]
    pyproject_version = proj["version"]

    # 断言 ①：tag == pyproject。发布链路的第一道闸门。
    if not _same_version(tag_version, pyproject_version):
        _fail(f"版本不一致：tag {tag} → {tag_version}，但 pyproject 是 {pyproject_version}。"
              f"打 tag 前先改 pyproject 的 version。")

    rel = yaml.safe_load((ROOT / "release.yaml").read_text(encoding="utf-8")) or {}
    for key in ("min_upgradable_from", "min_client_version", "upgrade_strategy"):
        if key not in rel:
            _fail(f"release.yaml 缺 {key}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest = {
        "$schema": SCHEMA_URL,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "channel": channel,
        "version": tag_version,
        "tag": tag,
        "url": f"https://github.com/{repo}/releases/tag/{tag}",
        "notes_url": f"https://github.com/{repo}/releases/tag/{tag}",
        # published_at 是版本发布时刻，generated_at 是这份文件的生成时刻。分开记，
        # 客户问「为什么我还看到旧版本」时才分得清是 CDN、代理还是本地缓存。
        "published_at": published_at or now,
        "generated_at": now,
        "requires": {
            "python": proj["requires-python"],
            "ragkernel_schema": _schema_version(),
            "min_upgradable_from": rel["min_upgradable_from"],
        },
        "upgrade_strategy": rel["upgrade_strategy"],
        "min_client_version": rel["min_client_version"],
        "security": rel.get("security"),
        # 签名是 Phase 3，字段现在就占位——日后加签不必升 schema_version。
        "signature": None,
        "key_id": None,
    }
    _validate(manifest)
    return manifest


def verify(manifest: dict, tag: str) -> None:
    """断言 ②：写出去的 manifest 确实等于 tag / pyproject。

    build() 里已经比过一次，这里是对**产物**再比一次——中间任何一步（模板、后处理、
    人工介入）改动了版本号都会在这里现形。
    """
    expected = tag[1:]
    if manifest["version"] != expected:
        _fail(f"manifest.version={manifest['version']} != tag {tag}")
    if manifest["tag"] != tag:
        _fail(f"manifest.tag={manifest['tag']} != {tag}")
    if not _same_version(manifest["version"], _pyproject()["project"]["version"]):
        _fail("manifest.version != pyproject.version")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 channel manifest")
    ap.add_argument("--tag", required=True, help="发布 tag，形如 v0.3.0")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--repo", default="v0id-byte/ragkernel")
    ap.add_argument("--published-at", default=None, help="ISO8601；默认取当前时刻")
    ap.add_argument("--out", default=None, help="输出文件；默认打到 stdout")
    ap.add_argument("--check-only", action="store_true", help="只做三方一致性断言，不写文件")
    args = ap.parse_args()

    manifest = build(args.tag, channel=args.channel, repo=args.repo,
                     published_at=args.published_at)
    verify(manifest, args.tag)

    if args.check_only:
        print(f"OK 版本一致：tag {args.tag} == pyproject {manifest['version']} == manifest")
        return 0

    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        # 回读再验一次：写盘这一步本身也可能出错（权限、磁盘满导致截断）
        verify(json.loads(out.read_text(encoding="utf-8")), args.tag)
        print(f"manifest → {out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
