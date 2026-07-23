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
- 并发用 `.ragkernel/locks/setup.lock` **文件锁**（不用 SQLite 锁——首次装时 auth.db 可能还不存在）。
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


def _provider_usable(prov: dict, cur_key: str) -> bool:
    """provider 是否可直接工作。openai 可用 "EMPTY"（本地 vLLM 忽略 key）；anthropic 需要 key。"""
    if prov.get("kind") == "openai":
        return True
    return bool(cur_key)


# ------------------------------------------------------------------ steps

def _step_provider(args) -> None:
    from . import config

    prov = config.provider(readonly=True)
    override = config.get_provider_override_ro()
    env_name = prov.get("api_key_env", "")
    cur_key = prov.get("api_key") or _env(env_name)
    configured = bool(override) or bool(cur_key)

    if args.yes or not _interactive():
        preset_key = args.provider
        new_key = _env("RAGKERNEL_SETUP_API_KEY")
        acting = bool(preset_key or new_key or args.base_url is not None or args.model)
        if not acting:
            # 无改动意图：当前 provider 可用就保持；不可用（anthropic 缺 key）则 fail-fast——
            # 别让「装成功但 LLM 一调就挂」。要显式推迟就 --skip provider。
            if _provider_usable(prov, cur_key):
                print("provider：保持现状（当前配置可用）")
                return
            raise SetupError("当前 provider 不可用（anthropic 缺 API key）；设 RAGKERNEL_SETUP_API_KEY "
                             "或 --provider，或 --skip provider 显式推迟 provider 配置。")

        preset = _PRESETS[preset_key] if preset_key else None
        kind = preset["kind"] if preset else prov.get("kind", "anthropic")
        base = args.base_url if args.base_url is not None else (preset["base_url"] if preset else prov.get("base_url", ""))
        model = args.model or (preset["model"] if preset else prov.get("model"))

        # 只有**端点**（kind/base_url）变了才算切换：换端点会让旧 key 失效。只改 model 用
        # 同一端点同一 key 是合法的、不必重输；同一预设幂等重跑也不算切换（endpoint 没变）。
        endpoint_changed = (kind, base) != (prov.get("kind"), prov.get("base_url"))
        if endpoint_changed:
            if new_key:
                key_to_write = new_key
            elif kind == "openai":
                # 切到 openai 没给新 key：写 "EMPTY" 清掉旧云端 key（与 OpenAIBackend 的占位一致），
                # 绝不把 MiniMax/Claude 的 key 当 bearer 发给新端点。
                key_to_write = "EMPTY"
            else:
                raise SetupError("切换 provider 需要新的 API key：设 RAGKERNEL_SETUP_API_KEY"
                                 "（避免沿用上一个 provider 的 key）")
        else:
            # 同端点：给了新 key 就更新，否则保留已存；anthropic 端点连旧 key 都没有才 fail-fast
            if kind != "openai" and not new_key and not cur_key:
                raise SetupError("当前 provider 缺凭证：设 RAGKERNEL_SETUP_API_KEY（或 --skip provider）")
            key_to_write = new_key or None  # None = set_provider_override 保留已存 key
        config.set_provider_override(kind, base, model, int(prov.get("max_tokens", 8000)), key_to_write)
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
        # kind 必须是受支持值——get_backend 把一切非 openai 当 anthropic，typo 会静默走错后端
        kind = _ask("kind (anthropic/openai)", prov.get("kind", "anthropic"))
        while kind not in ("anthropic", "openai"):
            print("  kind 只能是 anthropic 或 openai。")
            kind = _ask("kind (anthropic/openai)", prov.get("kind", "anthropic"))
        base = _ask("base_url", prov.get("base_url", ""))
        model = _ask("model", prov.get("model", ""))
    else:
        preset = _PRESETS[keys[int(choice) - 1]] if choice.isdigit() and 1 <= int(choice) <= len(keys) else _PRESETS["minimax"]
        kind = preset["kind"]
        base = _ask("base_url", preset["base_url"])
        model = _ask("model", preset["model"])

    # 端点（kind/base）变了才算切换——旧 key 会失效；只改 model 不算
    switching = (kind, base) != (prov.get("kind"), prov.get("base_url"))
    hint = "（留空保持现有 key）" if (cur_key and not switching) else ""
    entered = getpass.getpass(f"API key{hint}：").strip()
    api_key = entered or None  # None = set_provider_override 保留已存 key
    if not entered and not cur_key and kind != "openai":
        print("  ⚠️  未配置 key，anthropic provider 无法工作——稍后可 `ragkernel setup --only provider` 补。")
    elif not entered and cur_key and switching:
        # 切换了 provider 却没输新 key——旧 key 多半不匹配新服务，明确提示（不阻断，交互用户自决）
        print("  ⚠️  切换了 provider 但未输入新 key，将沿用旧 key（很可能与新服务不匹配）。")

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


def _active_admins() -> list[dict]:
    """只算**启用中**的管理员。被停用的 admin 登录不了（auth 只认 is_active=1），
    拿它当「已有管理员」跳过建号，会让部署没有可用管理员。"""
    from . import auth

    return [u for u in auth.list_users() if u["is_admin"] and u["is_active"]]


def _step_admin(args) -> None:
    from . import auth

    admins = _active_admins()
    if admins:
        print(f"管理员：已存在（{admins[0]['username']}{' 等' if len(admins) > 1 else ''}），跳过。")
        return

    taken = {u["username"] for u in auth.list_users()}  # 含被停用的——create_user 会撞 UNIQUE
    default_user = args.admin_user or _env("USER") or "admin"
    if args.yes or not _interactive():
        username = args.admin_user or default_user
        if username in taken:
            # 常见：停用首个 admin 后重跑，默认 $USER 与那个停用账号撞名 → 否则 IntegrityError 未捕获
            raise SetupError(f"用户名「{username}」已存在（可能是被停用的账号）；用 --admin-user 换个名，"
                             f"或 `ragkernel users activate <id>` 启用原账号。")
        password = _env("RAGKERNEL_SETUP_ADMIN_PASSWORD")
        if not password:
            raise SetupError("--yes 需要管理员密码：设 RAGKERNEL_SETUP_ADMIN_PASSWORD（或 --skip admin）")
    else:
        username = _ask("管理员用户名", default_user)
        while not username or username in taken:
            print(f"  用户名「{username}」不能用（空或已存在，可能被停用）；换一个，"
                  "或先 `ragkernel users activate <id>` 启用原账号。")
            username = _ask("管理员用户名", "")
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

    results = models.download()
    for r in results:
        mark = "✓" if r.status in ("cached", "downloaded") else "✗"
        print(f"  {mark} {r.role} {r.status}{f'：{r.error}' if r.error else ''}")
    # 用户明确选了下载，就不能把失败当成功走到 _wrapup——磁盘满/断网时自动化会误判初始化成功
    bad = [r for r in results if r.status in ("error", "missing", "incomplete")]
    if bad:
        raise SetupError(f"模型下载失败（{len(bad)} 个）——见上，磁盘/网络排查后重试 `ragkernel models`。")


def _step_token(args) -> None:
    from . import auth, config

    if not (args.with_token or (args.only and "token" in _selected(args))):
        return  # 默认不签发（安装动作不该顺手发长期凭证）

    admins = _active_admins()  # 必须是启用中的——停用的 admin，user_id_by_username 返回 None，签发会失败
    if not admins:
        print("MCP token：无启用中的管理员，跳过（先建/启用管理员）。")
        return
    user = admins[0]["username"]
    uid = auth.user_id_by_username(user)
    try:
        token = auth.issue_token(uid, ttl_days=365, label="claude-code", token_kind="agent")
    except Exception:
        # 用户明确 --with-token 却没签出来，不能静默返回让 run() 退 0——自动化会把「没拿到凭证」当成功
        raise SetupError(
            f"MCP token 签发失败：label「claude-code」可能已存在。先 "
            f"`ragkernel token revoke claude-code --user {user}` 再重试（该用户若已有可用 token 则无需重签，可 --skip token）。")

    # --yes 是自动化模式：即便从 pty（CI 常见）跑、_interactive() 为真，也默认脱敏，
    # 别把长效凭证漏进终端/CI 日志。只有显式 --show-token 才打印完整值。
    show = args.show_token or (_interactive() and not args.yes)
    mcfg = config.settings().get("mcp") or {}
    # 与 cmd_mcp 同源：env 覆盖优先，否则 yaml，否则默认——否则打印的 URL 与实际服务端口不符
    host = os.environ.get("RAGKERNEL_MCP_HOST") or mcfg.get("host", "127.0.0.1")
    port = int(os.environ.get("RAGKERNEL_MCP_PORT") or mcfg.get("port", 8765))
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
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        # --only admn 这类 typo 会被静默转成空步骤列表 → 假成功退 0；必须拒绝未知名字
        unknown = want - set(STEPS)
        if unknown:
            raise SetupError(f"--only 含未知步骤：{', '.join(sorted(unknown))}（可选：{', '.join(STEPS)}）")
        steps = [s for s in steps if s in want]
    if args.skip:
        drop = {s.strip() for s in args.skip.split(",") if s.strip()}
        unknown = drop - set(STEPS)
        if unknown:
            raise SetupError(f"--skip 含未知步骤：{', '.join(sorted(unknown))}（可选：{', '.join(STEPS)}）")
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

    # 过渡期同时持有新旧两把锁：滚动升级时可能还有**旧代码的 setup 正在跑**（比如卡在
    # 2GB 模型下载），它只认平铺的 .ragkernel/setup.lock。只锁新路径的话，两个进程会同时
    # 进入临界区并发改 auth/provider。加锁顺序固定「先旧后新」，新旧进程都从旧锁开始竞争，
    # 不会死锁。等不再支持从布局迁移前的版本升级时，legacy 那把可以去掉。
    locks = []
    for path in (config.rk_dir() / "setup.lock",
                 config.rk_path("locks", "setup.lock", create=True)):
        try:
            fp = open(path, "w")
        except OSError:  # 旧路径所在目录不可写等——不因过渡期兼容而挡住正常安装
            continue
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fp.close()
            for held in locks:
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()
            print("另一个 setup 进程正在运行；请等待其结束后重试。", file=sys.stderr)
            return 1
        locks.append(fp)

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
        for fp in reversed(locks):
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
