# 发布流程

RagKernel 的升级系统信任一条链：

```
tag  →  CI 断言  →  GitHub Release  →  manifest  →  客户端
```

客户端只信 manifest，manifest 只信 CI。**这条链上唯一无法靠客户端补救的错误，是 manifest
里的版本号与实际发布的版本不一致**——升级器会照着它把所有客户带到错的版本上。所以版本号
有且只有一个源，并在 CI 里做三方断言。

## 版本的唯一来源

`pyproject.toml` 的 `[project].version`。其余位置一律派生：

- `ragkernel.__version__` —— 由 `importlib.metadata` 从安装元数据读出（见 `ragkernel/__init__.py`）
- `.ragkernel/state/install.json` 的 `version` —— `install.sh` 从 pyproject 读
- manifest 的 `version` —— `scripts/gen_manifest.py` 从 pyproject 读

**不要**在任何地方手写版本号字面量。`tests/test_versioning.py` 会在 `__version__` 被改回
字面量时失败。

## 发布一个版本

1. **改 `pyproject.toml` 的 version**（唯一要手改版本号的地方）
2. **更新 `release.yaml`** —— 只放推导不出来、必须由发布方判断的东西：

   | 字段 | 什么时候要改 |
   |---|---|
   | `min_upgradable_from` | 本版不再支持从某个旧版直升时 |
   | `min_client_version` | 服务端接口变化、旧的独立客户端不再兼容时 |
   | `security` | 本版含 CVE 修复 → 填 `critical`，客户端 doctor 会把提示升成 error |
   | `upgrade_strategy` | 本版是否需要重启 / 迁移，预计停机多久 |

3. 若数据形态变了（加表、改列语义），把 `ragkernel/config.py` 的 `SCHEMA_VERSION` +1
4. `git tag vX.Y.Z && git push origin vX.Y.Z`

CI 接手后：断言版本 → 跑测试 → git-cliff 生成 changelog → 生成 manifest → 建 Release →
把 manifest 推到 `releases` 分支。

## 三方断言

`scripts/gen_manifest.py` 在两处比对：

- `build()` —— `tag[1:] == pyproject.version`，对不上直接非零退出
- `verify()` —— 对**产物**再比一次（写盘后还会回读再验），中间任何一步改动版本号都会现形

本地可随时自查，不必等 CI：

```bash
uv run python scripts/gen_manifest.py --tag v0.2.0 --check-only
```

CI 刻意把这一步排在 `uv sync` 之前——版本对不上就没必要花几分钟装 torch。

## manifest 发布在哪

客户端要一个**与 tag 无关的固定 URL** 才能问「当前渠道是哪个版本」，Release 资产的
URL 每个 tag 都变，用不了。所以 workflow 额外把 manifest 推到 `releases` 分支，
由 GitHub Pages 托管：

```
https://v0id-byte.github.io/ragkernel/releases/stable.json
```

**为什么是 Pages 而不是 `raw.githubusercontent`**：日后把自有域名绑成 Pages 的
custom domain 时，`*.github.io` 的旧地址会 301 到新域名，已经装在客户机器上的实例
不改配置也能继续查到。`raw` 没有这个重定向能力——endpoint 一旦散出去就永远改不动了。

企业客户可以把 `update.endpoint` 指向自建地址完全绕开 GitHub、自控灰度节奏，格式照
[`docs/schemas/manifest-v1.json`](schemas/manifest-v1.json) 校验即可。

## 渠道与预发布

tag 里带连字符（`v0.2.0-rc.1`、`v0.2.0-beta.1`）即视为预发布：

| tag | 渠道文件 | GitHub Release |
|---|---|---|
| `v0.2.0` | `releases/stable.json` | 正式 |
| `v0.2.0-rc.1` | `releases/prerelease.json` | 标记为 prerelease |

**预发布绝不能落到 stable**：客户端只查渠道 manifest，一旦 `stable.json` 指向 rc，
所有稳定渠道的实例都会被引导升上去。发布分支上每个渠道各占一个文件，发一次 rc
不会动 `stable.json`。

> 预发布的版本号在 semver 与 PEP 440 里写法不同（`0.2.0-rc.1` vs `0.2.0rc1`），
> 三方断言按 PEP 440 规范化后比较，两种写法都能对上。

## changelog

`cliff.toml` 驱动 git-cliff，从上一个 tag 自动生成，靠的是提交信息的 conventional
commits 前缀（`feat:` / `fix:` / `docs:` …）。`chore` / `ci` / `build` 不进 release notes。

自动生成不是为了省事，是因为人工写 notes 是发布流程里第一个被跳过的步骤——跳过一次，
manifest 的 `notes_url` 就开始撒谎。

## install.sh 的退出码

`install.sh` 会被 `ragkernel upgrade` 以程序方式调用，退出码是契约：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数错误 |
| 3 | `--update` 被要求但代码未变更（脏工作区 / 非默认分支 / detached HEAD）；依赖仍已 sync |
| 1 | 其余失败 |

**3 不是失败，但也绝不是成功**——报 0 会让升级状态机把「什么都没发生」记成 completed。
