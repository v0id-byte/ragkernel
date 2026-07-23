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
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_URL = "https://ragkernel.dev/schemas/manifest-v1.json"
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


def build(tag: str, *, channel: str, repo: str, published_at: str | None) -> dict:
    if not tag.startswith("v"):
        _fail(f"tag 必须形如 v0.3.0，收到 {tag!r}")
    tag_version = tag[1:]

    proj = _pyproject()["project"]
    pyproject_version = proj["version"]

    # 断言 ①：tag == pyproject。发布链路的第一道闸门。
    if tag_version != pyproject_version:
        _fail(f"版本不一致：tag {tag} → {tag_version}，但 pyproject 是 {pyproject_version}。"
              f"打 tag 前先改 pyproject 的 version。")

    rel = yaml.safe_load((ROOT / "release.yaml").read_text(encoding="utf-8")) or {}
    for key in ("min_upgradable_from", "min_client_version", "upgrade_strategy"):
        if key not in rel:
            _fail(f"release.yaml 缺 {key}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
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
    if manifest["version"] != _pyproject()["project"]["version"]:
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
