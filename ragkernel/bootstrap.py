"""`ragkernel setup` —— 交互式初始化向导。

覆盖：provider（先展示当前状态再问改不改）· 首个管理员（**排在模型下载之前**：
短交互前置、长阻塞后置，别让用户等完 2GB 下载才发现密码两次不一致）· 模型下载
（默认 N，「装成功」= runtime ready 不是模型就位）· MCP token（默认不签，`--with-token` 才签）。

几条硬规矩：
- provider 落 **DB 覆盖**（`config.set_provider_override`），不碰 settings.yaml——
  与 /admin 设置页同一条路径，DB 覆盖天然优先，也不必正则改注释密集的 yaml。
- **密钥绝不走 argv**（`ps` 可见）：API key 读 `RAGKERNEL_SETUP_API_KEY`，
  管理员密码读 `RAGKERNEL_SETUP_ADMIN_PASSWORD` 或交互 getpass。
- `--yes` **缺凭证就 fail-fast 非零退出**，不静默跳过——否则 CI 显示成功、服务启动即挂。
  「存在」判定必须是**非空**（`RAGKERNEL_SETUP_API_KEY=""` 是常见 CI 事故）。
- 并发用 `.ragkernel/setup.lock` **文件锁**（不用 SQLite 锁——首次装时 auth.db 可能还不存在）。
- 明文 token 只在交互式 tty 打印；`--yes`/非 tty 默认脱敏，需 `--show-token` 才出。
"""

import getpass
import os
import sys

STEPS = ("provider", "admin", "models", "token")

_PRESETS = {
    "minimax": dict(kind="anthropic", base_url="https://api.minimaxi.com/anthropic",
                    model="MiniMax-M3", api_key_env="MINIMAX_API_KEY", label="MiniMax（默认，零成本）"),
    "claude": dict(kind="anthropic", base_url="", model="claude-sonnet-5",
                   api_key_env="ANTHROPIC_API_KEY", label="官方 Claude"),
    "local": dict(kind="openai", base_url="http://localhost:8000/v1", model="Qwen3-32B-AWQ",
                  api_key_env="VLLM_API_KEY", label="本地 OpenAI 兼容（vLLM/Ollama）"),
}


class SetupError(SystemExit):
    """带非零退出码的向导失败。"""

    def __init__(self, msg: str):
        print(f"ERROR: {msg}", file=sys.stderr)
        super().__init__(1)


def _env(name: str) -> str:
    """环境变量的**非空**取值——空串视同未设（常见 CI 事故：变量声明了但没注入 secret）。"""
    return (os.environ.get(name) or "").strip()


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ------------------------------------------------------------------ 交互原语

def _ask(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt}{f'（{default}）' if default else ''}：").strip()
    return raw or default


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]：").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _mask(key: str) -> str:
    return f"····{key[-4:]}" if len(key) >= 4 else "已配置"


# ------------------------------------------------------------------ steps

def _step_provider(args) -> None:
    from . import config

    prov = config.provider(readonly=True)
    override = config.get_provider_override_ro()
    env_name = prov.get("api_key_env", "")
    cur_key = prov.get("api_key") or _env(env_name)
    configured = bool(override) or bool(cur_key)

    # 非交互：只有显式 --provider 或提供了新 key 时才动；否则保持现状
    if args.yes or not _interactive():
        preset_key = args.provider
        new_key = _env("RAGKERNEL_SETUP_API_KEY")
        if not preset_key and not new_key:
            print("provider：保持现状（未指定 --provider，也无 RAGKERNEL_SETUP_API_KEY）")
            return
        preset = _PRESETS[preset_key] if preset_key else None
        kind = preset["kind"] if preset else prov.get("kind", "anthropic")
        base = args.base_url if args.base_url is not None else (preset["base_url"] if preset else prov.get("base_url", ""))
        model = args.model or (preset["model"] if preset else prov.get("model"))
        # anthropic 缺 key（现存的也没有）→ fail-fast
        if kind != "openai" and not new_key and not cur_key:
            raise SetupError("--yes 配置 provider 需要凭证：设 RAGKERNEL_SETUP_API_KEY（或 --skip provider）")
        config.set_provider_override(kind, base, model, int(prov.get("max_tokens", 8000)),
                                     new_key or None)
        print(f"provider：已设为 {kind} · {model}")
        return

    # 交互：先展示当前状态
    if configured:
        src = "data/settings.db 运行时覆盖" if override else f"settings.yaml + env:{env_name}"
        print("当前 provider：")
        print(f"  kind {prov.get('kind')} · model {prov.get('model')} · key {_mask(cur_key) if cur_key else '未配置'}")
        print(f"  来源 {src}")
        if not _confirm("要修改吗?", default=False):
            return
    else:
        print("未发现 provider 覆盖配置，当前使用 config/settings.yaml 默认值（MiniMax）。")

    print("选择 LLM provider：")
    keys = list(_PRESETS)
    for i, k in enumerate(keys, 1):
        print(f"  {i} {_PRESETS[k]['label']}")
    print(f"  {len(keys) + 1} 手动填")
    choice = _ask("序号", "1")

    if choice == str(len(keys) + 1):  # 手动
        kind = _ask("kind (anthropic/openai)", prov.get("kind", "anthropic"))
        base = _ask("base_url", prov.get("base_url", ""))
        model = _ask("model", prov.get("model", ""))
    else:
        preset = _PRESETS[keys[int(choice) - 1]] if choice.isdigit() and 1 <= int(choice) <= len(keys) else _PRESETS["minimax"]
        kind = preset["kind"]
        base = _ask("base_url", preset["base_url"])
        model = _ask("model", preset["model"])

    hint = "（留空保持现有 key）" if cur_key else ""
    entered = getpass.getpass(f"API key{hint}：").strip()
    api_key = entered or None  # None = set_provider_override 保留已存 key
    if not entered and not cur_key and kind != "openai":
        print("  ⚠️  未配置 key，anthropic provider 无法工作——稍后可 `ragkernel setup --only provider` 补。")

    config.set_provider_override(kind, base, model, int(prov.get("max_tokens", 8000)), api_key)
    eff = config.provider(readonly=True)
    print(f"✓ provider 已存为运行时覆盖（data/settings.db，与 /admin 同一条）。"
          f"config/settings.yaml 未改动。\n  生效：{eff.get('kind')} · {eff.get('model')} · "
          f"{eff.get('base_url') or '(SDK 默认 host)'}")

    _connectivity_check()


def _connectivity_check() -> None:
    """配完 provider 顺手做一次连通性自检，失败不致命（只提示）。"""
    from .checks import provider as pv

    print("连通性自检：")
    for fn in (pv.check_provider_config, pv.check_provider_network, pv.check_provider_auth):
        try:
            r = fn()
        except Exception as e:  # 自检本身不该中断向导
            print(f"  ? {fn.__name__}：{type(e).__name__}")
            continue
        sym = {"passed": "✓", "failed": "✗", "skipped": "-"}.get(r.status, "?")
        print(f"  {sym} {r.title}  {r.summary}")


def _step_admin(args) -> None:
    from . import auth

    admins = [u for u in auth.list_users() if u["is_admin"]]
    if admins:
        print(f"管理员：已存在（{admins[0]['username']}{' 等' if len(admins) > 1 else ''}），跳过。")
        return

    default_user = args.admin_user or _env("USER") or "admin"
    if args.yes or not _interactive():
        username = args.admin_user or default_user
        password = _env("RAGKERNEL_SETUP_ADMIN_PASSWORD")
        if not password:
            raise SetupError("--yes 需要管理员密码：设 RAGKERNEL_SETUP_ADMIN_PASSWORD（或 --skip admin）")
    else:
        username = _ask("管理员用户名", default_user)
        password = _prompt_password()

    auth.create_user(username, password, is_admin=True)
    print(f"✓ 已创建管理员 {username}")


def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("管理员密码：")
        if not p1:
            print("  密码不能为空。")
            continue
        if p1 != getpass.getpass("再输一遍："):
            print("  两次不一致，重来。")
            continue
        return p1


def _step_models(args) -> None:
    from . import models

    if args.no_models:
        print("模型：跳过下载（--no-models）；稍后可 `ragkernel models`。")
        return

    status = {r.role: r for r in models.get_cache_status()}
    unready = [r for r in status.values() if r.status != "cached"]
    if not unready:
        print("模型：已全部缓存。")
        return

    # 默认 N——「装成功」= runtime ready，不是 2GB 就位；时长/网络不可预测
    do = False if (args.yes or not _interactive()) else _confirm(
        f"现在下载本地模型（~2GB，缺 {len(unready)} 个）?", default=False)
    if not do:
        print("模型：暂不下载；首次使用会自动下，或随时 `ragkernel models`。")
        return

    for r in models.download():
        mark = "✓" if r.status in ("cached", "downloaded") else "✗"
        print(f"  {mark} {r.role} {r.status}{f'：{r.error}' if r.error else ''}")


def _step_token(args) -> None:
    from . import auth

    if not (args.with_token or (args.only and "token" in _selected(args))):
        return  # 默认不签发（安装动作不该顺手发长期凭证）

    admins = [u for u in auth.list_users() if u["is_admin"]]
    if not admins:
        print("MCP token：尚无用户，跳过（先建管理员）。")
        return
    user = admins[0]["username"]
    uid = auth.user_id_by_username(user)
    try:
        token = auth.issue_token(uid, ttl_days=365, label="claude-code", token_kind="agent")
    except Exception:
        print("MCP token：label「claude-code」可能已存在——先 `ragkernel token revoke` 或换 label。")
        return

    show = args.show_token or _interactive()
    from . import config
    mcfg = config.settings().get("mcp") or {}
    host, port = mcfg.get("host", "127.0.0.1"), mcfg.get("port", 8765)
    print(f"✓ 已为 {user} 签发 MCP agent token（label claude-code，365 天）")
    if show:
        print("  只显示这一次，贴进 Agent 配置的 Authorization: Bearer：\n")
        print(f'  "ragkernel": {{"url": "http://{host}:{port}/mcp",')
        print(f'                "headers": {{"Authorization": "Bearer {token}"}}}}')
    else:
        print(f"  {_mask(token)} —— 完整值未打印（非交互）；需要请加 --show-token 重签")


# ------------------------------------------------------------------ 编排

def _selected(args) -> list[str]:
    steps = list(STEPS)
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        steps = [s for s in steps if s in want]
    if args.skip:
        drop = {s.strip() for s in args.skip.split(",")}
        steps = [s for s in steps if s not in drop]
    return steps


def _preflight() -> None:
    from . import diagnostics

    results = diagnostics.run(minimal=True)
    bad = [r for r in results if r.status == "failed" and r.severity == "error"]
    for r in results:
        sym = {"passed": "✓", "failed": "✗", "skipped": "-"}.get(r.status, "?")
        print(f"  {sym} {r.title}  {r.summary}")
    if bad:
        raise SetupError("环境预检未通过（见上），先修好再 setup。")


def run(args) -> int:
    import fcntl

    from . import config

    if args.reset_provider:
        config.clear_provider_override()
        print("已清除 provider 运行时覆盖，回退到 config/settings.yaml。")
        return 0

    lockdir = config.ROOT / ".ragkernel"
    lockdir.mkdir(exist_ok=True)
    fp = open(lockdir / "setup.lock", "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("另一个 setup 进程正在运行；请等待其结束后重试。", file=sys.stderr)
        return 1

    try:
        print("== 环境预检 ==")
        _preflight()
        steps = _selected(args)
        dispatch = {"provider": _step_provider, "admin": _step_admin,
                    "models": _step_models, "token": _step_token}
        for name in steps:
            print(f"\n== {name} ==")
            dispatch[name](args)
        _wrapup(args)
        return 0
    finally:
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()


def _wrapup(args) -> None:
    print("\n== 完成 ==")
    print("API 密钥（若配置）明文存于 <repo>/data/settings.db —— 请保护该文件，不要提交 data/。")
    print("下一步：")
    print("  ragkernel ingest --path <文件或目录>   # 摄取资料")
    print("  ragkernel models                       # 预载本地模型（若跳过了）")
    print("  ragkernel serve                        # 启动 Web 服务 http://127.0.0.1:8360")
    print("  ragkernel doctor                       # 随时自查")
