"""ragkernel CLI：ingest / embed / ask / serve / watch / models / stats。"""

import argparse
import sys

from . import config


def cmd_ingest(path: str, no_embed: bool):
    from . import pipeline

    results = pipeline.ingest_path(path, do_embed=not no_embed, on_stage=lambda s, d: print(f"  [{s}] {d}"))
    n = sum(r["chunks"] for r in results)
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"完成：{len(results)} 个文件（跳过 {skipped}），共 {n} 块。")


def cmd_embed():
    from . import pipeline, store

    db = store.connect()
    n = pipeline.embed_missing(db, on_progress=lambda done, total: print(f"  {done}/{total}"))
    pipeline._mark_embedded(db)
    print("嵌入已是最新。" if n == 0 else f"嵌入完成 {n} 块。")


def cmd_stats():
    from . import store

    for k, v in store.stats(store.connect()).items():
        print(f"  {k}: {v}")


def cmd_models():
    from . import models

    print("预载本地模型（首次下载 ~2GB，之后走缓存）…")
    results = models.download()
    for r in results:
        if r.status == "error":
            print(f"  ✗ {r.role}：{r.error}")
        elif r.status == "downloaded":
            print(f"  ✓ {r.role} 已下载（{r.name}）")
        else:
            print(f"  ✓ {r.role} 已就绪（{r.name}）")
    # `ragkernel models` 既是预载命令、也是 doctor 的修复命令——失败必须非零退出，
    # 否则自动化（含 install 向导）会把失败当成功，到首次使用才发现模型缺失。
    bad = [r for r in results if r.status in ("error", "missing", "incomplete")]
    if bad:
        print(f"模型未就绪（{len(bad)} 个失败）。", file=sys.stderr)
        sys.exit(1)
    print("模型就绪。")


def cmd_ask(question: str):
    from . import agent, audit

    aud = audit.Audit(client="cli")
    answer, _, tb, model = agent.ask(
        question, audit=aud, on_tool=lambda name, inp: print(f"  ↳ {name} {inp}", file=sys.stderr)
    )
    print(f"\n{answer}\n")
    if tb.touched:
        refs = sorted({t["ref"] for t in tb.touched})
        print(f"—— 触达 {len(refs)} 处引用 · 模型 {model}")


def cmd_watch(folder: str):
    from . import watch

    watch.run(folder)


def cmd_users(args):
    import getpass

    from . import auth

    if args.users_cmd == "add":
        if args.invite:
            result = auth.create_user(args.username, None, is_admin=args.admin)
            print(f"已建待激活用户 {args.username}（id={result['id']}{'，管理员' if args.admin else ''}）")
            print(f"建号口令：{result['setup_code']}（有默认有效期，交给 {args.username} 本人，"
                  "对方在登录页输入用户名后会看到设密码界面，需要这个口令）")
        else:
            password = getpass.getpass("密码：")
            result = auth.create_user(args.username, password, is_admin=args.admin)
            print(f"已建用户 {args.username}（id={result['id']}{'，管理员' if args.admin else ''}）")
    elif args.users_cmd == "list":
        for u in auth.list_users():
            flags = "，".join(f for f in [
                "管理员" if u["is_admin"] else "", "已禁用" if not u["is_active"] else ""
            ] if f)
            print(f"  {u['id']:>3}  {u['username']}" + (f"（{flags}）" if flags else ""))
    elif args.users_cmd == "deactivate":
        auth.set_active(args.user_id, False)
        print(f"已禁用用户 {args.user_id}")
    elif args.users_cmd == "activate":
        auth.set_active(args.user_id, True)
        print(f"已启用用户 {args.user_id}")


def cmd_version(args):
    """对标 `docker version` / `kubectl version`——企业支持对话的第一句话。
    （doctor 是体检，version 是身份证，两者不重叠。）"""
    import json as _json

    from . import update

    s = update.summary()
    if args.json:
        # 给机器的同一份数据：Ansible / CMDB / K8s operator 拿它做资产盘点，
        # 比让运维正则匹配人读输出可靠得多
        print(_json.dumps({
            "version": s["server"]["version"], "commit": s["server"]["commit"],
            "schema": s["server"]["schema"], "runtime": s["runtime"]["mode"],
            "channel": s["update"]["channel"], "update_available": s["update"]["available"],
            "latest": s["update"]["latest"],
        }, ensure_ascii=False))
        return

    rows = [("RagKernel", s["server"]["version"]), ("Commit", s["server"]["commit"] or "-"),
            ("Schema", str(s["server"]["schema"])), ("Runtime", s["runtime"]["mode"]),
            ("Channel", s["update"]["channel"])]
    if s["update"]["disabled"]:
        rows.append(("Update", "检查已关闭"))
    elif s["update"]["available"]:
        rows.append(("Update", f"v{s['update']['latest']} 可用"))
    elif s["update"]["error"]:
        rows.append(("Update", "无法获取版本清单"))
    elif not s["update"]["checked_at"]:
        # summary 只读缓存（绝不联网），全新安装时缓存是空的。此时说「已是最新」是撒谎——
        # 我们从没查过。这条状态必须和「查过且确实最新」区分开。
        rows.append(("Update", "尚未检查（ragkernel update）"))
    else:
        rows.append(("Update", "已是最新"))
    for k, v in rows:
        print(f"{k:<10} {v}")


def _print_update_status(st, gate) -> None:
    print(f"当前版本  {st.current}")
    print(f"最新版本  {st.latest or '?'}（渠道 {st.channel}）")
    if st.notes_url:
        print(f"变更说明  {st.notes_url}")
    for b in (gate.blockers if gate else []):
        print(f"  ✗ {b['message']}")
    for w in (gate.warnings if gate else []):
        print(f"  ! {w['message']}")


def cmd_update(args):
    """只检查，只读。真正执行是 `ragkernel upgrade`——`update` 一词在 apt/brew 语境里
    就是「刷新元数据」，让它动代码会违反肌肉记忆。"""
    from . import update

    st = update.check(force=True)
    if st.disabled:
        print("版本检查已在配置中关闭（update.check=false）。")
        return
    if st.error:
        print(f"无法获取版本清单：{st.error}")
        sys.exit(1)
    if not st.available:
        print(f"已是最新（{st.current}）。")
        return

    _print_update_status(st, update.can_upgrade(st.manifest))
    mode = update.runtime_mode()
    if not update.can_self_update(mode):
        print(f"\n本实例由 {mode} 管理，请执行：\n  {update.update_command(mode)}")
    else:
        print("\n升级：ragkernel upgrade")


def cmd_upgrade(args):
    from . import audit, update

    st = update.check(force=True)
    if st.error:
        print(f"无法获取版本清单：{st.error}")
        return 1

    target = args.to or st.manifest.get("tag") or (f"v{st.latest}" if st.latest else None)
    gate = update.can_upgrade(st.manifest)
    strategy = (st.manifest or {}).get("upgrade_strategy") or {}

    _print_update_status(st, gate)
    if strategy:
        changes = [n for n, k in (("需要重启", "restart_required"),
                                  ("需要迁移数据库", "migration_required")) if strategy.get(k)]
        if changes:
            print(f"本次升级  {' · '.join(changes)}")
        if strategy.get("estimated_downtime_seconds"):
            print(f"预计停机  ~{strategy['estimated_downtime_seconds']} 秒")

    if not gate.allowed:
        print("\n不能升级（见上）。")
        return 1
    if args.dry_run:
        print("\n--dry-run：未做任何改动。")
        return 0

    mode = update.runtime_mode()
    if not update.can_self_update(mode):
        print(f"\n本实例由 {mode} 管理，不做自更新。请执行：\n  {update.update_command(mode)}")
        return 1
    if mode == "process":
        print("\n注意：升级完成后进程会退出，需要你手动重启（systemd 部署可自动拉起）。")
    if not args.yes:
        ans = input(f"\n升级到 {target}？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    aud = audit.Audit(client="cli")
    ctl = update.UpdateController(audit_log=aud)
    try:
        res = ctl.apply(target, to_version=st.latest,
                        on_event=lambda e: print(f"  [{e['stage']}] {e['message']}"))
    except update.UpdateRefused as e:
        print(f"\n{e}\n  {e.command}")
        return 1
    except update.UpdateError as e:
        print(f"\n升级失败：{e}")
        print("服务仍处于维护态，需人工确认后再开门。")
        return 1

    print(f"\n代码已更新到 {target}（update_id {res['update_id']}）。")
    if res["restart_handled_by"] == "systemd":
        print("退出进程，交给 systemd 拉起…")
        sys.exit(0)
    if res["restart_handled_by"] == "manual-service":
        # 本进程只是运维敲的 CLI，真正跑旧代码的是那个被管理的服务——不点名的话
        # 用户很容易以为「升级完了」，而服务还在用内存里的旧代码。
        print("⚠ 检测到本机由 systemd 管理 ragkernel 服务，但本次升级不是它跑的。")
        print(f"  代码已换，**服务仍在跑旧代码**，需要重启它才生效：\n  {res['restart_command']}")
        print("  在此之前服务保持维护态（/api/ask 与 /api/upload 返回 503）。")
        return 0
    print(f"请重启进程使其生效：{res['restart_command']}")
    return 0


def cmd_mcp(args):
    import os

    if args.warm:  # 只预热 embed/rerank，不发 provider 请求；stdio 下走 stderr（stdout 是协议流）
        from . import embed, rerank

        print("预热 embedding / rerank 模型…", file=sys.stderr)
        embed.embed(["warmup"])
        rerank.get()
    if args.transport == "stdio":
        from .mcp import run_stdio

        run_stdio()
    else:
        mcfg = config.settings().get("mcp", {}) or {}
        host = os.environ.get("RAGKERNEL_MCP_HOST") or args.host or mcfg.get("host", "127.0.0.1")
        port = int(os.environ.get("RAGKERNEL_MCP_PORT") or args.port or mcfg.get("port", 8765))
        from .mcp.http import run_http

        run_http(host, port)


def cmd_token(args):
    from . import auth

    if args.token_cmd == "new":
        uid = auth.user_id_by_username(args.user)
        if not uid:
            print(f"没有这个用户：{args.user}（先 `ragkernel users add {args.user}`）")
            return
        try:
            tok = auth.issue_token(uid, ttl_days=args.days, label=args.label, token_kind="agent")
        except Exception:
            print(f"签发失败：label「{args.label}」可能已存在——先 revoke 或换个 label")
            return
        print(f"agent token（user={args.user} · label={args.label or '无'} · {args.days} 天）：\n\n{tok}\n")
        print("⚠️  只显示这一次。贴进 Agent 配置的 Authorization: Bearer；服务端只存哈希，丢了只能重发。")
    elif args.token_cmd == "list":
        rows = auth.list_tokens(args.user)
        if not rows:
            print("（没有 agent token）")
            return
        for r in rows:
            import time as _t

            exp = _t.strftime("%Y-%m-%d", _t.localtime(r["expires_at"])) if r["expires_at"] else "?"
            seen = _t.strftime("%Y-%m-%d", _t.localtime(r["last_seen_at"])) if r["last_seen_at"] else "-"
            print(f"  {r['id_short']}  {r['username']:<12} label={r['label'] or '-':<16} 过期 {exp}  最近 {seen}")
    elif args.token_cmd == "revoke":
        if args.user:  # label 撤销必须带 --user
            uid = auth.user_id_by_username(args.user)
            res = auth.revoke_agent_token(user_id=uid, label=args.target)
        else:          # 否则按 hash 前缀
            res = auth.revoke_agent_token(hash_prefix=args.target)
        if res.get("deleted"):
            print(f"已撤销 {res['id_short']}")
        else:
            extra = f"（候选：{', '.join(res['matches'])}）" if res.get("matches") else ""
            print(f"未撤销：{res.get('error')}{extra}")


def main():
    config.load_env()
    ap = argparse.ArgumentParser(prog="ragkernel", description="本地优先的企业 RAG 内核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="摄取文件或目录（幂等，可反复跑）")
    p.add_argument("--path", required=True, help="文件或目录")
    p.add_argument("--no-embed", action="store_true")

    sub.add_parser("embed", help="补齐缺失的向量")
    sub.add_parser("stats", help="知识库统计")
    sub.add_parser("models", help="预载/下载本地模型")
    sub.add_parser("serve", help="启动 Web 服务（上传 + 带引用问答）")

    p = sub.add_parser("setup", help="交互式初始化：provider / 首个管理员 / 模型 / MCP token")
    p.add_argument("--yes", action="store_true", help="全默认、不提问（CI）；缺凭证会非零退出")
    p.add_argument("--only", help="只跑这些步骤（逗号分隔：provider,admin,models,token）")
    p.add_argument("--skip", help="跳过这些步骤")
    p.add_argument("--reset-provider", action="store_true", help="清除 DB 覆盖，退回 settings.yaml")
    p.add_argument("--with-token", action="store_true", help="签发 MCP agent token（默认不签）")
    p.add_argument("--provider", choices=["minimax", "claude", "local"], help="非交互指定 provider 预设")
    p.add_argument("--base-url", default=None, help="覆盖 provider base_url")
    p.add_argument("--model", default=None, help="覆盖 provider model")
    p.add_argument("--admin-user", default=None, help="管理员用户名（默认 $USER）")
    p.add_argument("--no-models", action="store_true", help="不下载模型")
    p.add_argument("--show-token", action="store_true", help="非交互下也打印完整 token（默认脱敏）")

    p = sub.add_parser("version", help="版本 / commit / schema / 运行形态")
    p.add_argument("--json", action="store_true", help="机器可读输出（供 CMDB / Ansible 盘点）")

    sub.add_parser("update", help="检查有没有新版本（只读，不改任何东西）")

    p = sub.add_parser("upgrade", help="升级到新版本")
    p.add_argument("--to", default=None, help="指定目标 ref（默认走渠道最新）")
    p.add_argument("--dry-run", action="store_true", help="只说明将要发生什么，不做改动")
    p.add_argument("--yes", action="store_true", help="不询问直接执行")

    p = sub.add_parser("doctor", help="环境自查（装完/出问题时跑）")
    p.add_argument("--json", action="store_true", help="机器可读输出（供监控/K8s probe）")
    p.add_argument("--update", action="store_true", help="强制刷新版本检查后再出报告")
    p.add_argument("--offline", action="store_true", help="跳过所有网络检查")
    p.add_argument("--strict", action="store_true", help="把 warning 也当致命（只改退出码，不改 severity）")
    p.add_argument("--verbose", action="store_true", help="显示主机名、耗时与异常详情")

    p = sub.add_parser("ask", help="命令行问一个问题")
    p.add_argument("question")

    p = sub.add_parser("watch", help="监听落盘文件夹，自动索引")
    p.add_argument("--dir", required=True)

    p = sub.add_parser("users", help="用户账号管理")
    users_sub = p.add_subparsers(dest="users_cmd", required=True)
    pa = users_sub.add_parser("add", help="新建用户")
    pa.add_argument("username")
    pa.add_argument("--admin", action="store_true", help="建为管理员账号")
    pa.add_argument("--invite", action="store_true", help="不设密码，生成一次性建号口令，交给用户本人首登时自己设密码")
    users_sub.add_parser("list", help="列出用户")
    pd = users_sub.add_parser("deactivate", help="禁用用户")
    pd.add_argument("user_id", type=int)
    pe = users_sub.add_parser("activate", help="启用用户")
    pe.add_argument("user_id", type=int)

    p = sub.add_parser("mcp", help="MCP Server（把只读检索暴露给 Agent）")
    mcp_sub = p.add_subparsers(dest="mcp_cmd", required=True)
    ps = mcp_sub.add_parser("serve", help="启动 MCP Server")
    ps.add_argument("--transport", choices=["http", "stdio"], default="http", help="默认 http（远程）；stdio 供本地/air-gap")
    ps.add_argument("--host", default=None, help="默认 127.0.0.1；绑 0.0.0.0 会提示需 HTTPS")
    ps.add_argument("--port", type=int, default=None, help="默认 8765")
    ps.add_argument("--warm", action="store_true", help="启动前预热 embedding/rerank 模型（不发 provider 请求）")

    p = sub.add_parser("token", help="agent token 管理（MCP 鉴权用的个人访问令牌）")
    token_sub = p.add_subparsers(dest="token_cmd", required=True)
    tn = token_sub.add_parser("new", help="签发 agent token（只显示一次）")
    tn.add_argument("--user", required=True, help="令牌归属的用户名")
    tn.add_argument("--label", default=None, help="标签，便于识别与撤销（如 claude-code）")
    tn.add_argument("--days", type=int, default=365, help="有效天数，默认 365")
    tl = token_sub.add_parser("list", help="列出 agent token")
    tl.add_argument("--user", default=None, help="只看某用户")
    tr = token_sub.add_parser("revoke", help="撤销 agent token")
    tr.add_argument("--user", default=None, help="按 label 撤销时必填")
    tr.add_argument("target", help="label（配 --user）或 token hash 前缀（≥8 位）")

    args = ap.parse_args()
    if args.cmd == "ingest":
        cmd_ingest(args.path, args.no_embed)
    elif args.cmd == "embed":
        cmd_embed()
    elif args.cmd == "stats":
        cmd_stats()
    elif args.cmd == "models":
        cmd_models()
    elif args.cmd == "ask":
        cmd_ask(args.question)
    elif args.cmd == "watch":
        cmd_watch(args.dir)
    elif args.cmd == "users":
        cmd_users(args)
    elif args.cmd == "mcp":
        cmd_mcp(args)
    elif args.cmd == "token":
        cmd_token(args)
    elif args.cmd == "version":
        cmd_version(args)
    elif args.cmd == "update":
        cmd_update(args)
    elif args.cmd == "upgrade":
        sys.exit(cmd_upgrade(args) or 0)
    elif args.cmd == "setup":
        from . import bootstrap

        sys.exit(bootstrap.run(args))
    elif args.cmd == "doctor":
        from . import doctor

        sys.exit(doctor.main(args))
    elif args.cmd == "serve":
        from . import webapp

        webapp.main()


if __name__ == "__main__":
    main()
