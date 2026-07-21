# 诊断契约（diagnostics）

`ragkernel doctor` 的输出是**对外接口**——监控、K8s probe、CI、以及将来的 Web dashboard
都会消费它。本文是该接口的契约。字段结构改动要走 `schema_version`。

```bash
ragkernel doctor                  # 人读
ragkernel doctor --json           # 机器读
ragkernel doctor --offline        # 跳过所有网络检查
ragkernel doctor --strict         # warning 也算致命（只改退出码）
ragkernel doctor --verbose        # 主机名、耗时、异常详情
```

---

## CheckResult

| 字段 | 说明 |
|---|---|
| `id` | 稳定标识，JSON key，如 `provider.auth`。**发布后不许改名**，见下 |
| `category` | `runtime` \| `storage` \| `provider` \| `model` \| `security`。分组用它，**不要解析 `id` 前缀** |
| `title` | 人读标题 |
| `status` | `passed` \| `failed` \| `skipped` —— 通过了吗 |
| `severity` | `none` \| `warning` \| `error` —— 有多严重 |
| `summary` | 一句话结论 |
| `fix` | **可直接复制执行的命令**。只是第一步 remediation，不承诺完整解决问题。说明文字放 `summary`，文档链接放 `meta.docs_url` |
| `duration_ms` | 该项耗时 |
| `meta` | 结构化附加数据，各检查自定义 |

### status 与 severity 是两个正交维度

把「有没有通过」和「有多严重」塞进一个字段，消费方迟早要打补丁。所以：

| status | severity | 例 |
|---|---|---|
| `passed` | `none` | sqlite-vec 可加载 |
| `failed` | `warning` | 模型未缓存——确实没通过，但系统健康 |
| `failed` | `error` | `data/` 不可写 |
| `skipped` | `none` | `--offline` 跳过；或前置条件不满足 |

代码层强制的不变量（`CheckResult.__post_init__`，违反即 `ValueError`）：

- `passed` / `skipped` 的 severity 恒为 `none`
- `failed` 的 severity 必须是 `warning` 或 `error`
- `passed` **不许带 `fix`** —— 消费方可以假设 `fix != null` 一定意味着有事要做
- `skipped` **必须在 `summary` 里说明原因** —— 只说跳过不说为什么等于没说

`severity` 永远不会是 `null`，也永远不会是 `"ok"`。

### severity ≠ 退出码等级

`severity` 描述**这件事有多严重**；退出码由 **doctor 的 exit policy** 决定，是两个概念。
今天 policy 恰好把 `failed/error` 判为 UNHEALTHY，但将来出现「暂时限流」这类
`error` 而不该判定系统不可用的检查时，改的是 **policy**，不是去篡改 severity。

### id 稳定性

dashboard 里 `where check_id='provider.auth'` 的历史统计会因改名断掉。规则：

- 可以**新增** id（含更细的 `provider.auth.timeout`）
- **不许重命名**已发布的 id。确需迁移时新旧并存一个大版本，并在 `DEPRECATED_CHECK_IDS`
  登记 `{旧 id: 新 id}`
- `schema_version` 只在**字段结构**不兼容变化时 +1；新增 id、新增字段都不算

---

## 健康判定与退出码

```
0  HEALTHY     全部通过
1  DEGRADED    有 failed，但都是 warning——功能缺失，系统健康
2  UNHEALTHY   有 failed/error
3  UNKNOWN     required 的检查没跑成（被跳过，或完全缺席），无法判定
```

一刀切「任意失败即非零」会误伤：`docker build` 里在模型下载**之前**跑 doctor，
「模型未缓存」只该是 DEGRADED。CI 因此可以写：

```bash
ragkernel doctor || [ $? -le 1 ]     # 容忍 degraded
```

**UNKNOWN 单独一档**，是为了不把「没测」谎报成「健康」——`--offline` 下 provider
全部跳过时若退 0，部署脚本会以为 provider 没问题。「没跑成」包含两种：被跳过，以及
**完全缺席**（一个 required 的 id 根本没进结果——比如 policy 列了个还没实现的检查）。
判定只看 policy 里 `required` 的项，所以 `--skip models` 之类可选项不会莫名把整体顶成 3。

### `--strict` 的确切语义

```
strict:  failed + warning  →  判为 UNHEALTHY
never:   skipped           →  永不变成 failed
```

`--strict` **只改退出码判定，不改写 `severity`**。改写会让 JSON 输出撒谎——消费方看到的
应该始终是事实。因此 `doctor --offline --strict` 仍然是 **exit 3**，不是 2。

### HealthPolicy

`required` 不是 `CheckResult` 的字段，因为它不是检查的属性，而是
「结果 + 执行上下文 + 策略」的产物：同一条 `provider.auth`，K8s readiness 视为必需，
开发机上未必。策略独立于事实：

```python
from ragkernel.diagnostics import HealthPolicy

policy = HealthPolicy(required={"python", "sqlite", "storage"}, strict=False)
status = policy.evaluate(results)      # healthy | degraded | unhealthy | unknown
code   = policy.exit_code(results)
```

**`required` 的确切作用范围：它只决定「没跑成 → UNKNOWN」，不决定「哪些失败算数」。**
一个 `required` 的检查若被跳过（`--offline`）或完全缺席（还没实现/没注册），无法判定，
返回 `unknown`。这正是 K8s vs 开发机的差异所在：某项被跳过时，把它列为 required 的
策略会 `unknown`（不就绪），没列的策略照常放行。

但**失败**（`status == "failed"`）一律按 `severity` 计入健康，与 required 无关——
一个真的失败了的检查代表真的有问题，在哪台机器上都算。所以非必需的 `models` 未缓存
（`failed/warning`）会让系统 `degraded`，这是刻意的：`required` 管「有没有测」，
`severity` 管「测出来多严重」，两者不重叠。想让某项不影响健康，应让**该检查**返回
合适的 `status/severity`，而不是靠把它移出 `required`。

`DEFAULT_POLICY` 只列**当前已实现**的检查。声称需要一个还不存在的 id，会因为
「缺席 required → unknown」把每台干净机器误报成 UNKNOWN；新检查随其所在 PR 一起
按需加入 required。当前 required = `{python, sqlite, storage, provider.config, provider.network}`。

## provider 三步链与「doctor 绝不计费」

`provider.config → provider.network → provider.auth` 分层归因，让「LLM 用不了」精确到哪一层：

- **config**（required，纯逻辑）：配置齐不齐、key 从哪来、**配了但配错没有**。`meta.source` 报清
  每个字段来自 DB 覆盖还是 yaml、key 来自 override 还是 `env:<名字>`——企业最常见的坑是「配了但
  来源对不上」（`api_key_env` 指向的变量名写错）。还拦截两类会被运行时静默误解的错配：未知
  `kind`（`opeani` 会被 `get_backend` 当成 anthropic 走错后端）、不支持的 `base_url` scheme
  （`htps://` 会被当非 https 落到 80 端口）。
- **network**（required）：纯 socket + TLS，**不碰 LLM SDK**（SDK 缺 key 会 raise，会把「没填 key」
  误报成「网络不通」）；`https` 必带 SNI + 证书校验；不支持的 scheme 直接判失败、不当 http 裸连。
  措辞只说「TCP/TLS 可达」，不说「API 可达」——路径写错（`/v1`→`/v2`）时 TCP/TLS 照样通，那留给
  auth 暴露。检测到代理时改走代理测真实路径；代理返回 **407** 是代理鉴权失败（没到目标）→ 判失败，
  不当连通；代理 URL 里的 `user:pass@` 凭证在输出前一律脱敏。
- **auth**（**非 required，尽力而为**）：只用零成本端点（`GET /models`）验证凭证，**doctor 绝不发计费请求**。
  - `200` → 凭证有效；`401/403` → `failed/error`（key 无效/过期）。
  - `404` **不静默跳过**——真 SDK generate 会用同一个 `base_url` 一样失败。openai 兼容的 `/v1/models`
    是标准端点，404 判 `error`（base 路径大概率写错，如 `/v2`）；anthropic 兼容端点可用性不一，
    404 只降级 `warning`（degraded），既不误判 unhealthy 也不静默放行 healthy。
  - 网络错 → `skipped`（归因让给 provider · network，避免两条错误）；其它异常状态（5xx 等）→ `skipped`
    （确实无法判定）。因 auth 非 required，skipped 不会把整体顶成 unknown；但上面的 `failed` 仍按
    severity 计入健康。

依赖按**字段**判定、不按上一步 result 短路：没填 key 时 network 照跑（它只要 base_url），
用户于是只看到**一条** config 错误、而网络归因仍准确。

## 本地模型检查

`models`（**非 required**）探测 embedding / reranker 是否就绪，经 `models.get_cache_status()`
纯文件系统扫描 HF 缓存、不 import `huggingface_hub`/`torch`。关键：**判完整性不判目录存在**——
HF 缓存目录在下载中断后照样在、里头一堆指向缺失 blob 的悬空软链，只判目录会让 doctor 报 ✓
而运行时加载才炸。跟随软链验证 config + tokenizer + 权重三者齐全，区分 `missing`（从没装）/
`incomplete`（装了一半）/ `error`。未就绪 → `warning`（degraded，非 unhealthy）：模型没缓存是
功能缺失、不是系统坏了（`docker build` 里模型下载之前跑 doctor 是正常场景）。

---

## `--json` 输出

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-20T16:30:11+00:00",
  "host": {
    "hostname": null,
    "platform": "linux-x86_64",
    "ragkernel_version": "0.1.0",
    "commit": "a3f91c2"
  },
  "install": { "ref": "v0.4.0", "installed_at": "…", "installer": "install.sh" },
  "summary": { "status": "unhealthy", "exit_code": 2, "exit_policy": "default" },
  "checks": [
    {
      "id": "provider.auth",
      "category": "provider",
      "title": "provider · auth",
      "status": "failed",
      "severity": "error",
      "summary": "401 —— API key 无效或已过期",
      "fix": "ragkernel setup --only provider",
      "duration_ms": 412,
      "meta": {}
    }
  ]
}
```

### 消费方约定

- **必须忽略未知字段。** 将来会新增（如 `gpu`、`memory`），加字段不改 `schema_version`，
  严格校验会自己炸掉。
- **`summary.status` 用独立枚举** `healthy|degraded|unhealthy|unknown`，与单项的
  `passed|failed|skipped` 是两个 namespace，不要互相比较。
- **`host.hostname` 默认为 `null`**，只有 `--verbose` 才填。因为
  `doctor --json > issue.json` 贴到 issue 是可预期用法，而内网主机名会泄露组织结构。
- **`generated_at` 必读**：收集多节点输出时靠它排序。
- `install` 在手动安装（没跑过 `install.sh`）时是空对象，不是错误。

---

## 加一个新检查

1. 在 `ragkernel/checks/<领域>.py` 里写函数，返回 `CheckResult`（用
   `passed()` / `failed()` / `skipped()` helper）。
2. 追加到该模块的 `CHECKS` 列表，用 `CheckSpec` 声明 `id`/`category`/`title`，
   网络检查标 `network=True`，属于 bootstrap 预检的标 `minimal=True`。
3. 只有当它**应当影响整体健康判定**时，才去 `DEFAULT_POLICY.required` 里登记
   —— 默认不影响，这样新增检查不会意外改变别人 CI 的退出码。

不用管异常：`runner.run()` 会把任何异常兜成一条 `failed/error`，并把异常类型放进
`meta.exception_type`。**doctor 的职责不是证明代码没 bug，而是在代码有 bug 时依然告诉
用户哪里坏了**——所以单个检查崩溃不会中断其余检查。

约束：`checks/` **不许 import `huggingface_hub`** 或任何模型下载 SDK，只能经
`models.get_cache_status()`。否则离线部署时诊断层会跟着炸，而那正是最需要它的时刻。
