# 配置与 `ragkernel setup`

## provider 配置优先级（**真实行为**，不是理想模型）

不是简单的「环境变量 > DB > YAML」——两类字段规则不同：

| 字段 | 来源（高→低） |
|---|---|
| `kind` / `model` / `max_tokens` | `data/settings.db` 的 `provider_override` 行 → `config/settings.yaml`（**环境变量不参与**） |
| `base_url` | `provider_override.base_url`（含**空串**=SDK 默认 host） → `settings.yaml`（NULL/未设才回退） |
| `api_key` | `provider_override.api_key`（明文，DB） → `$<api_key_env>`（`.env` / 环境变量） |
| `host` / `port` | `RAGKERNEL_HOST` / `RAGKERNEL_PORT`（env） → `settings.yaml` |
| `data_dir` | `RAGKERNEL_DATA_DIR`（env） → `settings.yaml` |
| MCP `host` / `port` | `RAGKERNEL_MCP_HOST` / `RAGKERNEL_MCP_PORT`（env） → 硬编码 127.0.0.1:8765 |

**代码里 `DB > yaml`，所以「改了 yaml 不生效」时先看有没有 DB 覆盖**（`ragkernel setup` 和
`/admin` 设置页都写这条）。清除覆盖、退回 yaml：`ragkernel setup --reset-provider`。

> `base_url` 的空串是有意义的：官方 Claude 用空 `base_url` = SDK 默认 host。所以覆盖层用
> 「键存在且非 NULL」判断是否套用，而非「非空」——否则选官方 Claude 时清不掉上一个非空覆盖。

### 密钥存储的定位（企业须知）

`data/settings.db` 里的 provider 覆盖**面向单节点部署**，`api_key` 以**明文**存储，安全边界
等同该文件的文件系统权限（`.gitignore` 已忽略 `data/`）。多节点 / 生产部署请改用环境变量
（`.env` / systemd `EnvironmentFile` / 容器 secret）或外部密钥管理服务，并用
`ragkernel setup --skip provider` 跳过向导写库这一步。

---

## `ragkernel setup`

交互式初始化。默认按顺序跑：`provider → admin → models → token`（token 默认不签）。

```
ragkernel setup                    # 交互向导
ragkernel setup --yes              # 非交互（CI）；缺凭证非零退出
ragkernel setup --only provider    # 只跑某几步（逗号分隔）
ragkernel setup --skip models      # 跳过某几步
ragkernel setup --reset-provider   # 清 DB 覆盖，回退 yaml
ragkernel setup --with-token       # 顺带签发 MCP agent token
```

### 几条设计约定

- **provider 落 DB 覆盖**，不碰 `settings.yaml`（与 `/admin` 同一条路径）。
- **管理员排在模型下载之前**：短交互前置、长阻塞后置——别让用户等完 2GB 下载才发现密码两次不一致。
- **模型下载默认 N**：「装成功」= runtime ready，不是 2GB 就位（时长 / 网络不可预测）；
  首次使用会自动下，或随时 `ragkernel models`。
- **MCP token 默认不签**（`--with-token` 才签）：安装动作不该顺手发长期凭证（IT 安装 / 开发使用 /
  安全审核常是不同角色）。明文 token 只在交互式 tty 打印，`--yes` / 非 tty 默认脱敏，需 `--show-token`。

### 密钥绝不走 argv

命令行参数 `ps` 可见，所以密钥只从环境变量读：

| 用途 | 环境变量 |
|---|---|
| provider API key | `RAGKERNEL_SETUP_API_KEY` |
| 管理员密码 | `RAGKERNEL_SETUP_ADMIN_PASSWORD`（或交互 getpass） |

### `--yes` 非交互策略

| 步骤 | `--yes` 行为 | 缺前置 / 边界 |
|---|---|---|
| provider | 有 `--provider` / `--base-url` / `--model` / `RAGKERNEL_SETUP_API_KEY` 才动；否则「当前可用就保持、不可用就报错」 | 无改动意图但当前 anthropic 缺 key → **非零退出**（想推迟就 `--skip provider`）。**换端点**（kind/base_url 变）必须带新 key——anthropic 缺 key → 非零退出；切到 openai 缺 key → 写 `EMPTY` 清掉旧云端 key，绝不沿用。**只改 model**（同端点同 key）不算切换、不必重输 key；同预设幂等重跑也不要求重输 |
| admin | **无启用中管理员**时用 `$USER`（或 `--admin-user`）+ 环境密码创建（被停用的 admin 不算） | 无 `RAGKERNEL_SETUP_ADMIN_PASSWORD` → 非零退出；用户名与既有账号（含被停用的）撞名 → 明确报错（换名或 `users activate`），不是未捕获 IntegrityError |
| models | **默认不下载**（`--no-models` 亦可显式跳过）；用户明确选下载后**失败即非零退出** | 磁盘满/断网 → fail |
| token | 默认不签；`--with-token` 才签，**`--yes` 一律脱敏**（即便从 pty），仅 `--show-token` 打印完整值。URL 取 `RAGKERNEL_MCP_HOST/PORT` | label `claude-code` 已存在、签不出 → **非零退出**（先 revoke 或 `--skip token`），不静默当成功 |

`--only` / `--skip` 里的**未知步骤名**（如 `--only admn`）会**非零退出**，不静默变成空步骤假成功。

**「存在」判定是「非空」**：`RAGKERNEL_SETUP_API_KEY=""` 视同未设（声明了变量但没注入 secret
是常见 CI 事故）。`--yes` 缺必需凭证一律 fail-fast，不静默跳过——否则 CI 显示成功、服务启动即挂。

### 并发

向导启动即对 `.ragkernel/setup.lock` 上文件锁（`flock`），第二个进程拿不到就退出。用文件锁
而非 SQLite 锁——首次安装时 `auth.db` 可能还不存在。
