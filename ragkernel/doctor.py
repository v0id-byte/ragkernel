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

    p = config.rk_read_path("state", "install.json")
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # 合法 JSON 但不是对象（[]、null、"str"，手写坏了很常见）也要按损坏处理——
    # 否则 render_text 立刻 .get() 崩掉，JSON 模式也会违反 install 必为对象的契约。
    return data if isinstance(data, dict) else {}


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


def _public_check(r: CheckResult, verbose: bool) -> dict:
    d = r.to_dict()
    if not verbose:
        # exception repr 可能含本地路径或配置派生值（未来 provider 检查的异常里可能有
        # base_url 之类）——doctor --json > issue.json 贴 GitHub 是可预期用法，默认不该带。
        # --verbose 才是文档承诺显示异常细节的模式。exception_type 是裸类名、已出现在
        # summary 里，保留它便于非 verbose 下 triage。
        d["meta"] = {k: v for k, v in d["meta"].items() if k != "exception"}
    return d


def _update_info() -> dict:
    """与 install 段并列的版本上下文。客户把 `doctor --json` 发过来做支持时，
    版本、渠道、endpoint、上次检查时间就都在里面了，不必再来回问一轮。

    走 cached_status 而非 check：doctor 是只读诊断，渲染报告这一步绝不能联网
    （--offline 承诺不碰网络）。要不要联网由 checks/update.py 那条检查按 network 标志决定。"""
    try:
        from . import update

        st = update.cached_status()
        return {"current": st.current, "latest": st.latest, "available": st.available,
                "channel": st.channel, "endpoint": st.endpoint,
                "checked_at": st.checked_at, "disabled": st.disabled, "error": st.error}
    except Exception as e:  # noqa: BLE001 —— 诊断输出绝不能因为版本查询而整份失败
        return {"error": f"{type(e).__name__}: {e}"}


def render_json(results: list[CheckResult], policy: HealthPolicy, *, verbose: bool) -> str:
    return json.dumps({
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        # 监控会收集多台机器的输出，没有时间戳无法排序
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": _host_info(verbose),
        "install": _installed_at(),
        "update": _update_info(),
        "summary": {
            "status": policy.evaluate(results),
            "exit_code": policy.exit_code(results),
            "exit_policy": "strict" if policy.strict else "default",
        },
        "checks": [_public_check(r, verbose) for r in results],
    }, ensure_ascii=False, indent=2)


def main(args) -> int:
    if getattr(args, "update", False) and not args.offline:
        # 强制刷新版本缓存后再出报告——支持场景里要的是"现在"的结论，不是 6 小时前的。
        # **但 --offline 一票否决**：air-gap 机器上跑 `doctor --update --offline` 收集
        # 支持信息是可预期用法，这里发一次 GET 会等满超时，且直接违反 --offline 的契约。
        from . import update as _update

        _update.check(force=True)

    results = diagnostics.run(offline=args.offline)
    policy = HealthPolicy(required=diagnostics.DEFAULT_POLICY.required, strict=args.strict)

    out = render_json if args.json else render_text
    print(out(results, policy, verbose=args.verbose))
    return policy.exit_code(results)
