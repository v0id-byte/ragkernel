"""ragkernel doctor —— 渲染 + 退出码。

只做表现层：检查逻辑全在 checks/ 里，健康判定在 HealthPolicy 里。
只读，不修改任何状态（所以也不需要 setup 那把并发锁）。
"""

import json
import platform
import socket
import subprocess
import unicodedata
from datetime import datetime, timezone

from . import diagnostics
from .diagnostics.schema import DIAGNOSTICS_SCHEMA_VERSION, CheckResult, HealthPolicy

# 符号即 severity，一眼扫过去就知道轻重
SYMBOL = {
    ("passed", "none"): "✓",
    ("failed", "warning"): "!",
    ("failed", "error"): "✗",
    ("skipped", "none"): "-",
}


def _symbol(r: CheckResult) -> str:
    return SYMBOL.get((r.status, r.severity), "?")


def _width(s: str) -> int:
    """终端显示宽度：CJK 与全角标点占两列，str.ljust 按字符数对齐会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _width(s))


def _installed_at() -> dict:
    """读 install.sh 留下的安装指纹。手动安装没有这个文件，不算错。"""
    from . import config

    p = config.ROOT / ".ragkernel" / "install.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _commit() -> str | None:
    from . import config

    try:
        out = subprocess.run(
            ["git", "-C", str(config.ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _host_info(verbose: bool) -> dict:
    from . import __version__

    return {
        # 默认不带主机名：doctor --json > issue.json 贴到 GitHub 是可预期用法，
        # 内网主机名会泄露组织结构。--verbose 才带。
        "hostname": socket.gethostname() if verbose else None,
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "ragkernel_version": __version__,
        "commit": _commit(),
    }


def render_text(results: list[CheckResult], policy: HealthPolicy, *, verbose: bool) -> str:
    lines = []
    install = _installed_at()
    if install.get("ref") or install.get("commit"):
        ref = install.get("ref") or install.get("commit", "")[:7]
        lines.append(f"安装信息  ref {ref}（installer {install.get('installer', '?')}）")
    else:
        lines.append("安装信息  未知（手动安装）")
    lines.append("")

    width = max((_width(r.title) for r in results), default=0)
    for r in results:
        dur = f"  {r.duration_ms}ms" if verbose and r.duration_ms is not None else ""
        lines.append(f"{_symbol(r)} {_pad(r.title, width)}  {r.summary}{dur}")
        if r.fix:
            lines.append(f"  {' ' * width}  → {r.fix}")
        if verbose and r.meta.get("exception"):
            lines.append(f"  {' ' * width}    {r.meta['exception']}")

    status = policy.evaluate(results)
    code = policy.exit_code(results)
    lines.append("")
    lines.append(f"exit {code} {status.upper()}")
    return "\n".join(lines)


def render_json(results: list[CheckResult], policy: HealthPolicy, *, verbose: bool) -> str:
    return json.dumps({
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        # 监控会收集多台机器的输出，没有时间戳无法排序
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": _host_info(verbose),
        "install": _installed_at(),
        "summary": {
            "status": policy.evaluate(results),
            "exit_code": policy.exit_code(results),
            "exit_policy": "strict" if policy.strict else "default",
        },
        "checks": [r.to_dict() for r in results],
    }, ensure_ascii=False, indent=2)


def main(args) -> int:
    results = diagnostics.run(offline=args.offline)
    policy = HealthPolicy(required=diagnostics.DEFAULT_POLICY.required, strict=args.strict)

    out = render_json if args.json else render_text
    print(out(results, policy, verbose=args.verbose))
    return policy.exit_code(results)
