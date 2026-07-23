"""版本发现与升级编排 —— 生命周期管理层。

分层边界（见 docs/releasing.md）：

    update.py    lock · maintenance · state machine · manifest · audit
        ↓  只传参数、只看退出码
    install.sh   git · uv sync · extras · install.json 指纹

install.sh **不知道本模块的存在**——它同时被 Docker build、CI、离线安装器复用，
一旦被升级逻辑污染，那三条路径全部跟着遭殃。

本模块**必须保持纯 Python**（不 import flask、不 import webapp）：webapp / cli / MCP
都调用它，它谁也不认。否则 `ragkernel upgrade` 这个纯 CLI 动作会被迫拖起整个 Web 应用，
未来的 updater daemon 也没法复用。tests/test_update.py 里有断言钉住这条。

**不做 execv 自替换。** 这个代码库到处是函数内惰性导入，在服务存活期间 uv sync 换掉
.venv/ 会让下一次惰性导入炸；加上 SSE 问答流、ingest 长任务、sqlite 半完成事务，
热替换在知识库系统里不可接受。流程固定为：置维护态 → drain → 换代码 → 退出 → 外部拉起。
"""

import fcntl
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__ as CURRENT
from . import config

# 官方 channel manifest。用自有域名而非 raw.githubusercontent，是为了让托管位置日后可迁移
# （CDN、镜像、灰度）而不必让每个已装实例改配置——endpoint 一旦散落到成千上万台机器上就改不动了。
# 内网客户把 update.endpoint 指向自家地址即可完全绕开。
DEFAULT_ENDPOINT = "https://ragkernel.dev/releases/stable.json"
_TIMEOUT = 8
_UA = f"ragkernel/{CURRENT}"


class UpdateError(Exception):
    """升级失败。"""


class UpdateStateError(UpdateError):
    """非法状态迁移。异常恢复靠状态可信，所以这是硬错误而不是警告。"""


class UpdateRefused(UpdateError):
    """当前部署形态不允许自更新（docker）。携带该给用户的命令。"""

    def __init__(self, message: str, command: str = ""):
        super().__init__(message)
        self.command = command


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cfg() -> dict:
    return config.settings().get("update") or {}


# ── 运行形态 ────────────────────────────────────────────────

def runtime_mode() -> str:
    """docker → systemd → process。**顺序即优先级，docker 必须排第一**：容器里跑
    systemd supervisor 的部署会同时命中两个判据，而此时正确答案永远是 docker
    （容器内 git pull 无论如何都是错的，正确做法是重建镜像）。"""
    if Path("/.dockerenv").exists():
        return "docker"
    if os.getenv("INVOCATION_ID"):  # systemd 拉起的单元才有
        return "systemd"
    return "process"


DOCKER_UPDATE_COMMAND = "docker compose -f docker/docker-compose.yml pull && docker compose -f docker/docker-compose.yml up -d"


def update_command(mode: str | None = None) -> str | None:
    """该形态下用户该敲的升级命令。由服务端给全，前端不拼命令。"""
    mode = mode or runtime_mode()
    if mode == "docker":
        return DOCKER_UPDATE_COMMAND
    return None


def can_self_update(mode: str | None = None) -> bool:
    return (mode or runtime_mode()) != "docker"


# ── semver ─────────────────────────────────────────────────

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    """只取三元组。非 semver 返回 None——调用方一律当作「无法比较」，不报错：
    fork / 企业 mirror 用自己的 tag 规则是常态，不该因此炸掉版本检查。"""
    if not v:
        return None
    m = _SEMVER.match(str(v).lstrip("v"))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(latest: str | None, current: str | None) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


# ── manifest 获取（ETag + TTL 双层）──────────────────────────

@dataclass
class UpdateStatus:
    current: str = CURRENT
    latest: str | None = None
    available: bool = False
    channel: str = "stable"
    endpoint: str = ""
    checked_at: str | None = None
    notes_url: str | None = None
    manifest: dict = field(default_factory=dict)
    error: str | None = None
    disabled: bool = False   # 配置里关掉了版本检查（离线/内网部署）

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("manifest")  # 完整 manifest 给需要的调用方单独取，不塞进摘要
        return d


def _cache_read() -> dict:
    p = config.rk_read_path("cache", "update-cache.json")
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_write(data: dict) -> None:
    _atomic_write(config.rk_path("cache", "update-cache.json", create=True), data)


def _fetch(endpoint: str, etag: str | None) -> tuple[dict | None, str | None]:
    """(manifest, etag)。304 时返回 (None, etag) —— 表示「没变，用缓存里的」。

    匿名 GET，不带实例 ID、不带任何遥测。UA 里的版本号会暴露给 endpoint 方，
    这点在 docs/configuration.md 写明，且 update.check=false 可彻底关掉。
    """
    req = urllib.request.Request(endpoint, headers={"User-Agent": _UA})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8")), r.headers.get("ETag") or etag
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def check(force: bool = False) -> UpdateStatus:
    """查 manifest。**任何失败都不抛**——版本检查坏掉不该影响任何主流程，
    错误只落进缓存的 error 字段，UI 据此静默不显示 banner。"""
    cfg = _cfg()
    endpoint = (cfg.get("endpoint") or "").strip() or DEFAULT_ENDPOINT
    channel = cfg.get("channel", "stable")
    if cfg.get("check", True) is False or channel == "none":
        return UpdateStatus(channel=channel, endpoint=endpoint, disabled=True)

    cache = _cache_read()
    manifest = cache.get("manifest") or {}
    etag = cache.get("etag")
    checked_at = cache.get("checked_at")
    error = cache.get("error")

    if not force and manifest and _is_fresh(checked_at, cfg):
        return _status(manifest, channel, endpoint, checked_at, error)

    try:
        fresh, new_etag = _fetch(endpoint, etag)
        if fresh is not None:
            manifest = fresh
        etag, error = new_etag, None
    except Exception as e:  # noqa: BLE001 —— 网络/解析的所有失败在这里等价
        error = f"{type(e).__name__}: {e}"

    checked_at = _now()
    _cache_write({"checked_at": checked_at, "etag": etag, "manifest": manifest, "error": error})
    return _status(manifest, channel, endpoint, checked_at, error)


def cached_status() -> UpdateStatus:
    """只读缓存，**绝不联网**。

    给 `doctor --json`、`ragkernel serve` 启动提示这类「顺带报一下版本」的场景用。
    它们不该因为缓存过期就发起网络请求——`doctor --offline` 承诺不碰网络，
    而启动提示卡在 8 秒超时上是不可接受的。
    """
    cfg = _cfg()
    endpoint = (cfg.get("endpoint") or "").strip() or DEFAULT_ENDPOINT
    channel = cfg.get("channel", "stable")
    if cfg.get("check", True) is False or channel == "none":
        return UpdateStatus(channel=channel, endpoint=endpoint, disabled=True)
    cache = _cache_read()
    return _status(cache.get("manifest") or {}, channel, endpoint,
                   cache.get("checked_at"), cache.get("error"))


def _is_fresh(checked_at: str | None, cfg: dict) -> bool:
    if not checked_at:
        return False
    try:
        then = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_h = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    return age_h < float(cfg.get("interval_hours", 6))


def _status(manifest: dict, channel: str, endpoint: str,
            checked_at: str | None, error: str | None) -> UpdateStatus:
    latest = manifest.get("version")
    return UpdateStatus(
        latest=latest, available=is_newer(latest, CURRENT), channel=channel,
        endpoint=endpoint, checked_at=checked_at, notes_url=manifest.get("notes_url"),
        manifest=manifest, error=error,
    )


# ── 兼容性闸门 ──────────────────────────────────────────────

@dataclass
class UpgradeCheckResult:
    """结构化而非 (bool, reason)：阻塞原因会并存（Python 版本不够 **且** schema 跨度太大），
    单字符串只能报一个；前端也要按 type 做差异化渲染。"""

    allowed: bool
    blockers: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "blockers": self.blockers, "warnings": self.warnings}

    @property
    def reason(self) -> str:
        return "；".join(b["message"] for b in self.blockers)


def can_upgrade(manifest: dict) -> UpgradeCheckResult:
    """升级**之前**判能不能升，而不是「下载 → 安装 → 失败 → 服务挂了再来查」。

    本期只比对声明值，不做迁移预演（见 docs/updates.md 的 migration preflight）。
    """
    blockers: list[dict] = []
    warnings: list[dict] = []

    if not manifest:
        return UpgradeCheckResult(False, [{"type": "manifest", "message": "没有可用的版本清单"}])

    latest = manifest.get("version")
    if not is_newer(latest, CURRENT):
        blockers.append({"type": "version", "current": CURRENT, "required": latest,
                         "message": f"已是最新（本机 {CURRENT}，清单 {latest}）"})

    req = manifest.get("requires") or {}

    want_schema = req.get("ragkernel_schema")
    if isinstance(want_schema, int) and want_schema > config.SCHEMA_VERSION + 1:
        # 跨了不止一代数据形态：中间版本的迁移没跑过，直升会把库带到没人验证过的状态
        blockers.append({"type": "schema", "current": config.SCHEMA_VERSION, "required": want_schema,
                         "message": f"数据形态跨度过大（本机 v{config.SCHEMA_VERSION} → 目标 v{want_schema}），"
                                    "需要先升级到中间版本"})

    floor = req.get("min_upgradable_from")
    if floor and parse_version(CURRENT) and parse_version(floor) and parse_version(CURRENT) < parse_version(floor):
        blockers.append({"type": "min_upgradable_from", "current": CURRENT, "required": floor,
                         "message": f"本机 {CURRENT} 过旧，需先升到 {floor} 及以上"})

    strategy = manifest.get("upgrade_strategy") or {}
    if strategy.get("migration_required"):
        warnings.append({"type": "migration", "message": "本次升级会迁移数据库，建议先备份 data/"})
    if manifest.get("security") == "critical":
        warnings.append({"type": "security", "message": "本版含安全修复，建议尽快升级"})

    return UpgradeCheckResult(not blockers, blockers, warnings)


# ── 状态机 ──────────────────────────────────────────────────

# 合法迁移显式成表：没有它，异常恢复就变成「猜当前状态可能是什么」。
# completed → updating、idle → restarting 这类穿越一旦出现，现场就没法诊断了。
#
# checking / downloading 目前没有代码会进入：查版本是只读的后台动作，不是升级的一个阶段
# （让它写状态会与在途升级抢同一份 update.json）；git 拉取由 install.sh 一步做完，没有
# 独立的下载阶段。两个状态保留在表里是因为它们是对外契约的一部分——未来若改成先下载
# 制品再安装，迁移路径已经定义好，不必动契约。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "idle":        {"checking", "draining"},
    "checking":    {"downloading", "idle", "failed"},
    "downloading": {"draining", "failed"},
    "draining":    {"updating", "failed"},
    "updating":    {"restarting", "failed"},
    "restarting":  {"completed", "failed"},
    "completed":   {"idle"},
    "failed":      {"idle"},
}

_IN_FLIGHT = {"checking", "downloading", "draining", "updating"}


def _atomic_write(path: Path, data: dict) -> None:
    """先写临时文件再 rename。升级状态存在的意义就是崩溃后能恢复，
    半截 JSON 会让恢复逻辑读到损坏状态——那正是最需要它的时刻。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state() -> dict:
    p = config.rk_read_path("state", "update.json")
    if p is None:
        return {"schema_version": 1, "state": "idle"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "state": "idle"}
    if not isinstance(data, dict) or data.get("state") not in ALLOWED_TRANSITIONS:
        return {"schema_version": 1, "state": "idle"}
    return data


def transition(new: str, **fields) -> dict:
    """**所有**状态变更都必须走这里，代码里不许有 state["state"] = ... 的裸赋值。"""
    st = read_state()
    old = st.get("state", "idle")
    if new not in ALLOWED_TRANSITIONS.get(old, set()):
        raise UpdateStateError(f"非法状态迁移：{old} → {new}")
    st.update(fields)
    st["state"] = new
    st["schema_version"] = 1
    _atomic_write(config.rk_path("state", "update.json", create=True), st)
    return st


def new_update_id() -> str:
    """关联键：进度事件、审计、update.json、日志行全带它。企业支持场景里从
    「客户说升级失败」到「查出是 sync 阶段挂的」，就靠这一个 id 串起来。"""
    return "u_" + datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + uuid.uuid4().hex[:6]


def event(update_id: str, state: str, stage: str | None, message: str,
          progress: float | None = None) -> dict:
    """进度事件的**固定协议**，由本模块定义、各前端只做渲染。

    Web 走 SSE、CLI 走 stdout、未来桌面端走 WebSocket，但载荷结构必须同一份——
    否则三边各长一套格式，第四个客户端接入时就得写第四个解析器。
    """
    return {"update_id": update_id, "state": state, "stage": stage,
            "message": message, "progress": progress, "ts": _now()}


# ── 维护态（与锁是两件事）───────────────────────────────────
#   update.lock       并发控制——防两个升级同时跑
#   maintenance.json  服务状态——对外声明「正在维护」
# 顺序固定，锁在最外层：
#   acquire lock → write maintenance → drain → upgrade → remove maintenance → release lock

def maintenance() -> dict:
    p = config.rk_read_path("state", "maintenance.json")
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) and data.get("enabled") else {}


def enter_maintenance(*, reason: str, update_id: str, detail: str = "") -> None:
    _atomic_write(config.rk_path("state", "maintenance.json", create=True), {
        "enabled": True, "reason": reason, "owner": "update-controller",
        "pid": os.getpid(), "update_id": update_id,
        "started_at": _now(), "detail": detail,
    })


def exit_maintenance() -> None:
    p = config.rk_path("state", "maintenance.json")
    p.unlink(missing_ok=True)


def _pid_alive(pid: int | None) -> bool:
    """PID 复用的已知局限：Linux PID 会回绕，昨天的 ragkernel 1234 可能是今天的
    nginx 1234，此时会误判成「维护仍在进行」而保留残留态。加固做法是同时记
    /proc/sys/kernel/random/boot_id，same boot + same pid 才可信。

    第一版只做存活探测，因为**真正的判据是 update state 而不是 pid**（见
    recover_update_state），且误判方向偏保守：宁可多留一会儿维护态，
    也不会误清一个真在跑的升级。
    """
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于当前用户
    return True


# ── 启动恢复 ────────────────────────────────────────────────

def recover_update_state(*, audit_log=None) -> dict:
    """启动时先恢复 update 状态、**再**处理维护态。顺序是有语义的，不能只看 pid。

    考虑这个真实故障：git 换代码完成、restart 之前 crash——此时 DB migration
    尚未跑完。若只看到「pid 不存在」就删掉 maintenance 正常开门，等于把一个
    半迁移状态的库直接投入服务。

    返回本次恢复动作的摘要（无事发生时 {}）。
    """
    st = read_state()
    state = st.get("state", "idle")
    outcome: dict = {}

    if state == "restarting":
        if st.get("to_version") and st["to_version"] == CURRENT:
            st = transition("completed", finished_at=_now(), error=None)
            outcome = {"action": "completed", "update_id": st.get("update_id")}
        else:
            # 换代码没生效：进程起来了，跑的还是老版本
            st = transition("failed", finished_at=_now(),
                            error=f"重启后版本仍是 {CURRENT}，期望 {st.get('to_version')}")
            outcome = {"action": "failed", "update_id": st.get("update_id"), "reason": "version_mismatch"}
    elif state in _IN_FLIGHT:
        st = transition("failed", finished_at=_now(),
                        error=f"进程在 {state} 阶段中断")
        outcome = {"action": "failed", "update_id": st.get("update_id"), "reason": "interrupted"}

    mt = maintenance()
    if mt:
        if st.get("state") == "failed":
            # 保留维护态：上次升级没走完，不能假装无事发生地开门
            outcome["maintenance"] = "kept"
        elif not _pid_alive(mt.get("pid")):
            exit_maintenance()
            outcome["maintenance"] = "cleared"
        else:
            outcome["maintenance"] = "held"

    if outcome and audit_log is not None:
        audit_log("update_recovered", outcome)
    return outcome


# ── 执行层 ──────────────────────────────────────────────────

class UpdateExecutor:
    """只认状态机与事件回调，不认部署细节。

    before/after 钩子本期是空实现，但签名现在就要在——企业升级的标准动作是
    backup → upgrade → migration → verify，备份与升级后校验迟早要加；没有钩子时
    它们只能被硬塞进 apply() 中间，那正是最难改的位置。
    """

    def before_upgrade(self, ctx: dict) -> None:
        pass

    def apply(self, target: str, *, on_event) -> dict:
        raise NotImplementedError

    def after_upgrade(self, ctx: dict) -> None:
        pass


class LocalExecutor(UpdateExecutor):
    """调仓库内的 install.sh。**不重写 git/uv 逻辑**——浅克隆加深、脏工作区、
    tag/commit 分流、CAD extra、刷新 install.json 指纹都已在里面处理过。"""

    def _extras(self) -> list[str]:
        p = config.rk_read_path("state", "install.json")
        if p is None:
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        got = data.get("extras") if isinstance(data, dict) else None
        return got if isinstance(got, list) else []

    def apply(self, target: str, *, on_event) -> dict:
        script = config.ROOT / "install.sh"
        if not script.exists():
            raise UpdateError(f"找不到 {script}——手动安装（非 install.sh）暂不支持自更新")

        cmd = ["sh", str(script), "--update", "--ref", target, "--dir", str(config.ROOT)]
        extras = self._extras()
        if "cad" in extras:
            # 不回读 extras 的话，升级会悄悄把 CAD extra 卸掉
            cmd.append("--cad")

        env = {**os.environ, "RAGKERNEL_NO_SETUP": "1"}
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines: list[str] = []
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            lines.append(line)
            if line.startswith("==> "):
                on_event(line[4:])
        rc = proc.wait()
        tail = "\n".join(lines[-15:])

        if rc == 3:
            # install.sh 的契约：--update 被要求但代码未变更。不是失败，但绝不是成功——
            # 报成功会让状态机把「什么都没发生」记成 completed。
            raise UpdateError(f"代码未变更（工作区脏 / 非默认分支 / detached HEAD）。\n{tail}")
        if rc != 0:
            raise UpdateError(f"install.sh 退出码 {rc}\n{tail}")
        return {"extras": extras, "output_tail": tail}


class SystemdExecutor(LocalExecutor):
    """systemd 形态：换完代码退出，交给 Restart=always 拉起。

    退出动作**不在这里做**——controller 只把 restart 意图返回给调用方，由 CLI/Web
    决定何时真的退。executor 里直接 os._exit 会让测试没法跑完 apply()。
    """


class DockerExecutor(UpdateExecutor):
    """容器内 git pull 是错的：镜像才是交付物，代码是 COPY 进去的。永远 refuse。"""

    def apply(self, target: str, *, on_event) -> dict:
        raise UpdateRefused("Docker 部署由镜像管理，请重建容器", DOCKER_UPDATE_COMMAND)


def executor_for(mode: str | None = None) -> UpdateExecutor:
    return {"docker": DockerExecutor, "systemd": SystemdExecutor}.get(
        mode or runtime_mode(), LocalExecutor)()


# ── 编排 ────────────────────────────────────────────────────

class UpdateController:
    def __init__(self, *, audit_log=None):
        self.audit_log = audit_log

    def apply(self, target: str, *, to_version: str | None = None,
              on_event=None, drain=None) -> dict:
        """锁 → 维护态 → drain → 换代码 → restarting。

        drain 是可选的「等在途请求结束」回调：CLI 没有在途请求，Web 才有。
        """
        emit = on_event or (lambda _e: None)
        update_id = new_update_id()
        mode = runtime_mode()

        with _update_lock():
            st = read_state()
            if st.get("state") in _IN_FLIGHT:
                raise UpdateError(f"已有升级在进行中（{st.get('update_id')}，{st['state']}）")
            if st.get("state") in ("completed", "failed"):
                transition("idle")   # 上一轮的终态先归位，否则 idle→draining 走不通

            executor = executor_for(mode)
            ctx = {"update_id": update_id, "from_version": CURRENT,
                   "to_version": to_version, "target": target, "mode": mode}

            transition("draining", update_id=update_id, from_version=CURRENT,
                       from_commit=_commit(), to_version=to_version, target=target,
                       started_at=_now(), finished_at=None, error=None, stage="drain")
            enter_maintenance(reason="update", update_id=update_id,
                              detail=f"升级至 {to_version or target}")
            emit(event(update_id, "draining", "drain", "等待在途请求结束"))
            try:
                if drain is not None:
                    drain()

                transition("updating", stage="git")
                emit(event(update_id, "updating", "git", "获取新版本代码", 0.2))
                executor.before_upgrade(ctx)
                result = executor.apply(
                    target, on_event=lambda msg: emit(event(update_id, "updating", "sync", msg, 0.6)))
                executor.after_upgrade(ctx)

                transition("restarting", stage="restart", to_commit=_commit())
                emit(event(update_id, "restarting", "restart", "等待进程重启以生效", 0.9))
            except Exception as e:  # noqa: BLE001 —— 任何失败都要落状态，否则下次启动无从恢复
                transition("failed", finished_at=_now(), error=f"{type(e).__name__}: {e}")
                # 维护态**故意保留**：升级没走完，不能假装无事发生地开门。
                # recover_update_state 会在下次启动时再次确认并提示需人工介入。
                self._audit("update_failed", {"update_id": update_id, "error": str(e), "mode": mode})
                raise

            self._audit("update_applied", {"update_id": update_id, "from": CURRENT,
                                           "to": to_version, "mode": mode, "target": target})

        # 锁与维护态的收尾在重启后由 recover_update_state 完成——进程即将被换掉，
        # 在这里 exit_maintenance 会让「换代码了但还没重启」的窗口对外显示为正常服务。
        return {"update_id": update_id, "mode": mode,
                "restart_required": True,
                "restart_handled_by": "systemd" if mode == "systemd" else "manual",
                **result}

    def _audit(self, kind: str, payload: dict) -> None:
        if self.audit_log is not None:
            try:
                self.audit_log(kind, payload)
            except Exception:  # noqa: BLE001 —— 审计写失败不该把升级带崩
                pass


def _update_lock():
    """并发控制。与 maintenance.json 是两件事：锁防「两个升级同时跑」，
    维护态对外声明「正在维护」。"""

    class _Lock:
        def __enter__(self):
            self.fp = open(config.rk_path("locks", "update.lock", create=True), "w")
            try:
                fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.fp.close()
                raise UpdateError("另一个升级正在进行；等它结束后重试") from None
            return self

        def __exit__(self, *exc):
            fcntl.flock(self.fp, fcntl.LOCK_UN)
            self.fp.close()
            return False

    return _Lock()


def _commit() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(config.ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def summary() -> dict:
    """给 /api/system/info、ragkernel version、MCP get_server_info 用的同一份摘要。"""
    mode = runtime_mode()
    st = check()
    state = read_state()
    gate = can_upgrade(st.manifest) if st.manifest else None
    strategy = (st.manifest.get("upgrade_strategy") or {}) if st.manifest else {}
    return {
        "server": {"version": CURRENT, "commit": _commit(), "schema": config.SCHEMA_VERSION},
        "update": {
            "available": st.available, "latest": st.latest, "channel": st.channel,
            "notes_url": st.notes_url, "checked_at": st.checked_at,
            "disabled": st.disabled, "error": st.error,
            "blocked": bool(gate and not gate.allowed and st.available),
            "blockers": gate.blockers if gate else [],
            "warnings": gate.warnings if gate else [],
            "strategy": strategy,
        },
        "runtime": {"mode": mode, "can_self_update": can_self_update(mode),
                    "update_command": update_command(mode)},
        "state": {"update_id": state.get("update_id"), "state": state.get("state", "idle"),
                  "stage": state.get("stage"), "error": state.get("error")},
    }
