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
3  UNKNOWN     required 的检查被跳过，无法判定
```

一刀切「任意失败即非零」会误伤：`docker build` 里在模型下载**之前**跑 doctor，
「模型未缓存」只该是 DEGRADED。CI 因此可以写：

```bash
ragkernel doctor || [ $? -le 1 ]     # 容忍 degraded
```

**UNKNOWN 单独一档**，是为了不把「没测」谎报成「健康」——`--offline` 下 provider
全部跳过时若退 0，部署脚本会以为 provider 没问题。判定只看 policy 里 `required` 的项，
所以 `--skip models` 之类不会莫名把整体顶成 3。

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
