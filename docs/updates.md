# 版本发现与升级

RagKernel **默认只提醒，不自动升级**。生产库不会被悄悄换掉——升级永远是一个明确的人为动作。

发布侧（打 tag、CI、manifest 怎么生成）见 [releasing.md](releasing.md)。本文是运维侧。

## 命令

```bash
ragkernel version              # 我是谁、跑在哪（支持对话的第一句话）
ragkernel version --json       # 同一份数据给机器：CMDB / Ansible / K8s operator
ragkernel update               # 检查有没有新版本，只读
ragkernel upgrade --dry-run    # 说清将要发生什么，不做任何改动
ragkernel upgrade              # 真的升级
ragkernel doctor --update      # 强制刷新版本检查后出体检报告
```

`update` 与 `upgrade` 分开是照 apt/brew 的肌肉记忆：`update` 在那个语境里就是"刷新元数据"，
让它动代码会违反预期。

## 三种部署形态，行为不同

判定顺序即优先级，**docker 排第一**——容器里跑 systemd supervisor 会同时命中两个判据，
而此时正确答案永远是 docker（容器内 `git pull` 无论如何都是错的，镜像才是交付物）。

| 形态 | 判据 | 行为 |
|---|---|---|
| docker | `/.dockerenv` 存在 | **不自更新**，只提醒并给出 `docker compose pull && up -d` |
| systemd | `INVOCATION_ID` 存在 | 完整闭环：换代码 → 退出 → `Restart=always` 拉起 |
| process | 其余 | 换代码后进程退出，**需要你手动重启** |

生产建议走 systemd，这是"一键升级"闭环的前提。

## 升级过程中发生什么

```
取锁 → 置维护态 → drain 在途请求 → 换代码（install.sh）→ 退出进程 → 外部拉起 → 启动恢复
```

**不做进程内热替换。** 这个代码库到处是函数内惰性导入，在服务存活期间 `uv sync` 换掉
`.venv/` 会让下一次惰性导入炸；加上 SSE 问答流、ingest 长任务、sqlite 半完成事务，
热替换在知识库系统里不可接受。

维护窗口内**所有写请求**（POST / PUT / PATCH / DELETE）返回 503，**所有读请求照常**——
运维与探针绝不能在升级时变瞎，那正是最需要它们的时刻。探针请用 `/health`。

按方法判定而不是列举路径：`/api/feedback` 会摄取并立刻嵌入、`/admin/api/*` 会改库，
逐条列举的清单只会随着路由增加越来越不全。唯一例外是 `/api/auth/*`，管理员要能进来看状态。

### 状态与关联键

升级状态落在 `.ragkernel/state/update.json`，状态迁移受一张显式表约束，非法迁移直接抛错。
没有这张表，异常恢复就变成"猜当前状态可能是什么"。

每次升级生成一个 `update_id`，进度事件、审计日志、状态文件、日志行全带它。企业支持场景里
从「客户说升级失败」到「查出是 sync 阶段挂的」，就靠这一个 id 串起来：

```bash
ragkernel doctor --json | jq .update      # 版本上下文
sqlite3 data/audit.db "select * from events where kind like 'update%'"
```

### 崩溃后怎么恢复

启动时**先恢复升级状态、再处理维护态**——顺序有语义。考虑这个故障：git 换代码完成、
restart 之前 crash，此时 DB 迁移尚未跑完。若只看到"pid 不存在"就删掉维护态正常开门，
等于把一个半迁移状态的库直接投入服务。

| 崩溃时的状态 | 恢复后 | 维护态 |
|---|---|---|
| `restarting`，且当前版本 == 目标版本 | `completed` | 清除 |
| `restarting`，但版本没变（换代码没生效） | `failed` | **保留** |
| `updating` / `draining` / `downloading` | `failed` | **保留** |

判定为 `failed` 时维护态**故意保留**，服务启动会打印提示，需人工确认后清除：

```bash
rm .ragkernel/state/maintenance.json
```

> **PID 复用的已知局限**：Linux PID 会回绕，昨天的 ragkernel 1234 可能是今天的 nginx 1234，
> 此时会误判成"维护仍在进行"而保留残留态。加固做法是同时记 `boot_id`，`same boot + same pid`
> 才可信。当前版本只做存活探测，因为**真正的判据是 update state 而不是 pid**，且误判方向
> 偏保守：宁可多留一会儿维护态，也不会误清一个真在跑的升级。

## 升级前的兼容性闸门

`ragkernel upgrade` 在动任何东西**之前**先判能不能升，而不是"下载 → 安装 → 失败 →
服务挂了再来查"。阻塞原因会并存，全部列出：

- 目标版本要求的数据形态跨度过大（需先升到中间版本）
- 本机版本低于目标版本的 `min_upgradable_from`
- 已经是最新

`--yes` 只跳过确认提示，**不跳过闸门**。

## 配置

```yaml
update:
  check: true            # false = 彻底不联网查版本（离线 / 内网部署）
  channel: stable        # stable | none
  endpoint: ""           # 留空走官方 manifest；企业内网填自家地址，完全绕开 GitHub
  interval_hours: 6
  auto_install: false    # 预留，当前版本不实现
```

### 隐私

版本检查是**匿名 GET**：不带实例 ID、不带任何遥测、不上报任何知识库内容。

唯一会暴露给 endpoint 方的是 `User-Agent: ragkernel/<版本号>` 和你的出口 IP。不接受这一点
就把 `check` 设为 `false`，或把 `endpoint` 指向自建地址——两种方式都能做到零外联。

缓存在 `.ragkernel/cache/update-cache.json`，带 ETag：TTL 决定"要不要发请求"，
ETag 决定"发了之后要不要传数据"。

## 回滚

`update.json` 记着 `from_commit`：

```bash
sh install.sh --update --ref <from_commit> --dir .
```

**回滚前先备份 `data/`。** 升级会自动迁移数据库（迁移是幂等的、connect 时跑），但回滚到
老代码遇到新 schema 可能出问题。当前版本不实现自动回滚。

## 当前版本不做的事

| | 说明 |
|---|---|
| 自动安装 | `auto_install` 只占位 |
| 迁移预演 | 闸门只比对声明的 schema 版本号，不预演迁移。完整方案是升级前跑 `check_migration` 并在不满足时 `Upgrade blocked: backup required` |
| 独立 updater daemon | Web 进程当前仍持有执行权限，靠 `UpdateExecutor` 抽象留了迁移接缝 |
| manifest 签名 | `signature` / `key_id` 字段已占位，加签不必升 schema |
| 自动回滚 | 见上 |
