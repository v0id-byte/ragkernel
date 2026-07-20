# 文档生命周期与归属权限

企业知识库真正会被问的第一个问题不是"能不能多认一种格式"，而是**谁可以看到、修改、删除这些工程知识**。
本文描述 ragkernel 的文档生命周期模型、归属权限，以及三条必须被守住的不变式。

## 三条不变式

评审涉及文档、检索、摄取的改动时，不必逐行读实现，只问一句：**这个改动会不会破坏下面三条之一？**

### 1. Retrieval invariant — 已归档的文档绝不出现在检索结果中

> *Archived documents must never enter retrieval results.*

- **收口点**：[`search.py`](../../ragkernel/search.py) 的 `_ACTIVE` / `_scope()`。它无条件与调用方注入的
  `where` 做 AND，调用方只能进一步收窄、无法放宽。
- **为什么在 search.py 而不是各调用点**：调用方侧的规则 fail-open。`tools.py` 已有三处检索调用各自
  手工拼 `where`；将来任何人新增第四个入口忘了加过滤，**不会报错**，只会静默引用一篇本已下架的
  资料——没有异常、没有日志，只有一条不该出现的引用。这类不变式必须在最低层保证。
- **旁路风险**：不经 `hybrid_search` 的通路必须单独设防，目前有两条，都已收口：
  - CAD 结构化层 → [`store.get_engineering_entities` / `get_engineering_entity`](../../ragkernel/store.py)
    （六个 CAD 工具全部走这两个函数）
  - `tools.read_document` 直接读 `chunks` → 自带未归档条件
- **测试**：[`tests/test_search_security.py`](../../tests/test_search_security.py)、
  [`tests/test_mcp_archive.py`](../../tests/test_mcp_archive.py)

### 2. Ownership invariant — `owner_id` 不会被后续摄取覆盖

> *`owner_id` cannot be overwritten by later ingestion.*

只允许 `NULL → 具体值` 的回填（无主文档被首个具名上传者认领）。一旦有主，任何重传、CLI、watch
都不能改写。写在 `store._upsert_document_tx` 与 `store.set_owner`（后者带 `AND owner_id IS NULL` 兜底）。

**测试**：`tests/test_store_lifecycle.py::test_owner_never_overwritten`

### 3. Lifecycle invariant — 索引维护动作不改变可用状态

> *Index maintenance must not change availability state.*

reindex / repair / rebuild-vector 这类**机器动作**绝不能清掉 `archived_at`。归档是**人的决定**。
否则会出现"管理员白天归档、夜里自动 reindex 全部恢复"这种毫无征兆的 bug。

重传是**用户动作**，可以按下面的判定表撤销归档——这与本条不冲突，注意区分。

**测试**：`tests/test_ownership.py::test_index_maintenance_keeps_archive_policy`

## 两台正交的状态机

文档状态不是一个字段，而是两台独立的状态机。这个建模比把一切塞进 `status` 强得多：

| | 字段 | 含义 | 由谁推进 |
|---|---|---|---|
| **摄取状态**<br>ingestion state | `documents.status` | 这份资料被处理到什么程度 | 管道自动推进，**机器的事实** |
| **可用状态**<br>availability state | `documents.archived_at` | 这份资料是否还该被知识库使用 | 人决定，**治理的决策** |

两者正交：一篇 `embedded` 的文档可以是 `archived`；一篇 `chunked` 的文档可以是 `active`。

```
                             ┌─ rejected           （超限，仅留一条说明性状态 chunk → 不可检索）
uploaded ─→ chunked ─┬─→ embedded
                     └─→ embedding_failed          （结构化可读、BM25 可召回、无向量）

  status 为 chunked / embedded / embedding_failed 的文档：
        archived_at IS NULL  ──→  active    （参与检索）
        archived_at 非 NULL  ──→  archived  （退出检索、数据保留、可恢复）
                                      │
                                      └──→ deleted（仅管理员：级联删索引 + 删原件 + 写审计）
```

检索排除的是 `archived` 与 `rejected`，**不是** `status != 'embedded'`：未 embed 的 `chunked` 文档在
纯 BM25 部署（未配 embedding provider）下必须仍可检索，`embedding_failed` 的 chunk 也是有效正文。

## 归档 vs 删除

| | 归档 | 删除 |
|---|---|---|
| 谁能做 | 本人 / 管理员 | **仅管理员** |
| 检索 | 退出 | 退出 |
| 数据 | 全部保留 | 索引 + 向量 + 工程实体 + `data/uploads` 下的原件一并抹除 |
| 可逆 | ✅ | ❌ |
| 入口 | 侧栏文档卡右下角 | `/admin` 知识库管理，二次确认 |

删除时原件只在 `data/uploads` 内才会被删——watch 目录、脚本摄取的库外源文件绝不动
（`webapp._remove_source_file` 的路径护栏）。索引与文件两步各自成败分开上报
（`index_removed` / `source_removed`），DB 删成功而文件没删掉时管理员要能看出磁盘留了垃圾。

## 归档是生命周期可见性，不是访问控制

> *Archive controls lifecycle visibility, not access control.*

普通用户在文档列表里**能看到他人归档的文档**（只是没有操作按钮）。这是刻意的：企业知识库里
"这份资料被下架了"本身是需要共知的信息，否则用户会反复重传同一份文件。

**不要把归档当私有化机制用。** 真要做"谁能看见哪些文档"，那是另一个正交的 ACL 特性。

## 权限矩阵

`owner_id IS NULL` = 历史遗留或脚本/watch 导入，无上传者记录 → **仅管理员可处置**。

| 调用者 | 文档 owner | 归档 / 恢复 | 硬删除 |
|---|---|---|---|
| 本人 | 自己 | ✅ | ❌ |
| 普通用户 | 他人 | ❌ | ❌ |
| 普通用户 | 无主 | ❌ | ❌ |
| 管理员 | 任意（含无主） | ✅ | ✅ |

权限一律**由后端算好随文档下发**（`can_manage` / `can_delete`），列表接口不回传 `owner_id`——
前端不该拿到推导权限的原料，将来加角色时也只需改一处。

### 重传已归档文件的判定

唯一准则是**这篇文档现在归谁**，不是谁在调用：

| 文档 owner | 调用方 | 恢复上架 |
|---|---|---|
| 无主 | CLI / watch / 脚本 | ✅ |
| 无主 | 任意登录用户 | ✅（并认领 owner） |
| 本人 | 本人 | ✅ |
| 他人 | 任意登录用户 | ❌ |
| **他人** | **CLI / watch / 脚本** | ❌ |

最后一行是要害：`owner_id=None`（CLI / watch）**不是**"受信任的本地调用"。它只授予"认领无主文档"
的能力，不授予"覆盖他人决定"的能力——否则把文件往被监视目录一丢，别人归档过的文档就自动上架了，
**watch 目录会变成一条隐藏的权限入口**。

## 审计

管理员操作写进 `audit.db` 的 `events`（`session_id` 为 NULL——管理动作不属于任何问答会话）：
`document_archived` / `document_unarchived` / `document_deleted`。

- 硬删除**先快照元数据再删库**。审计 payload 绝不能在行删掉之后回读——今天或许"恰好"还持有
  删除前的 Row，但那是巧合，谁改一下 `delete_document` 的内部实现，审计就静默地只剩一个 id，
  而且没有任何测试会失败。
- operator **同时存 `operator_id` 与当时的 `operator_name` 快照**，像 git commit 的 author：
  id 用于将来关联，name 快照保证用户改名后历史不被污染、销号后仍可解读。
- 不给 `documents` 加 `deleted_at` / `deleted_by` 墓碑列——行都删了，墓碑无处安放。

## 已知取舍

- **404 / 403 的枚举差异**：对不存在的 id 返 404、对他人文档返 403，理论上可枚举出"哪些 id 存在
  但不属于我"。当前**接受**：产品模型是共享知识库，`GET /api/documents` 本来就把全部文档列给每个
  登录用户，文档存在性不是秘密。
  **触发重评的条件**：一旦引入 workspace / 租户隔离、列表接口不再对所有人返回全部文档，这两个
  状态码必须统一成 404。
- **同名文件在磁盘上互相覆盖**：上传落盘用 `Path(filename).name`，两个同名不同内容的文件会覆盖
  彼此，却产生两条不同 sha256 的记录。因此删掉其中一条可能删掉另一条依赖的字节。管理面板的 sha
  列就是为此存在。将来配一个 orphan uploads 清理任务。
- **`owner_id` 是 INTEGER，直指 `auth.db` 的 `users.id`**。将来上多租户 / SSO 时会想换成
  `TEXT`(uuid) + `tenant_id`；因为接口层不回传裸 `owner_id`，届时换类型不影响前端契约。
