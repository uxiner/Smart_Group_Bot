# 配置参考手册（逐字段细节）

> **这份文档是开发者与运维的参考，不是首次部署必读。** 想把机器人跑起来、想知道
> 「该配什么、谁能改、改完什么时候生效」，请读
> **[配置手册（部署与日常管理）](./configuration.md)**。本文保留每一段配置的完整
> 细节：三层结构与生效判定、每个字段的默认值与上下界、真实读侧消费者、保持固定的
> 不变量，以及从旧版本迁移的注意事项。内容来自原 `configuration.md`，只增加了这段
> 注记并修正了文件改名后的相对链接。

机器可读版本在 [`docs/configuration-fields.json`](./configuration-fields.json)
（由 `python -m bot.tools.config_catalog` 从 schema 生成；
`tests/test_configurable_policy_catalog.py` 会保证它不与代码漂移）。
每条记录都带 `path` / `default` / `bounds` / `reload_kind` / `read_consumers` /
`api_role` / `test_node`。

---

## 三层配置

| 层 | 载体 | 改完生效 | 谁能改 |
| --- | --- | --- | --- |
| ① 部署引导（bootstrap） | `.env` / 进程环境变量 | **必须重启** | 有部署权限的人 |
| ② 热配置 | `runtime_config` 表（Mini App / API） | 保存即生效或**需重启**（见下） | 仅最高管理员 |
| ③ 群级覆盖 | `group_settings`（群组页） | 保存即生效 | 该群管理员 / 最高管理员 |

### ① 部署引导（`.env`）

只有**进程启动时必须知道**的东西在这里：Telegram token、最高管理员 id、配置加密
主密钥、数据库地址、Mini App 监听与公网地址、webhook。模板见
[`.env.example`](../.env.example)，所有敏感项默认为空。

`config.toml` 是**一次性导入源**：只有当 `runtime_config` 表里还没有记录时，
`RuntimeConfigManager.initialize()` 才会把它读进去，之后该文件被忽略。
它不是第三套优先级，别拿它当"热配置通道"。

### ② 热配置（运行时配置）

Mini App 的每个面板都对应 `runtime_config` 文档里的一段。保存走
`PUT /api/v1/settings`，带 `revision` 做乐观并发控制；`extra="forbid"`，多余的键
直接 **400 `validation_error`**（不是 422——这些路由跑在 aiohttp 上，不经过 FastAPI 的
校验器）。

几条必须按实现来理解的语义：

* **`config` 是整份替换，不是嵌套 patch。** 后端只对提交的 payload 做
  `RuntimeConfig.model_validate()`，**不与库里的现值做深合并**：漏掉的字段会回到 schema
  默认值，而不是保留旧值。所以调用方必须先 `GET` 整份文档、改完再整份 `PUT` 回去
  （内置的 Mini App 正是这么做的）。`revision` 对不上返回 **409 `revision_conflict`**。
* **secret 字段是只写的。** 响应里这些字段恒为空串，`configured_secrets` 只回**键名**。
  密钥改动走 `secret_changes`：`keep` 保留、`clear` 清空、`replace` 换新值（`replace`
  必须带非空 `value`，否则 400 `invalid_settings`）。**空串表示「不变」，永远不是
  「清空」**——想清空只能显式发 `clear`。预览（`POST /api/v1/settings/diff`）与真保存
  用**同一个**解析器与**同一个**上限判断，同样的坏输入在两边得到同样的 400
  `invalid_settings`（见下面"变更历史 / 差异预览 / 回滚"）。
* **自由 JSON 与 URL 里的凭据同样不回显明文。** `models.*.request_params` 里的
  凭据值（`extra_headers` 之类）与带凭据的 `api_base` 在响应里是
  `<redacted:current>` / `<redacted:url-query>` 标记，**空值不标记**。把这份文档
  原样 `PUT` 回去是无损的：后端在校验之前把标记换回**当前**值。想换值就写新值，
  想清空就把列表写成 `[]`。
* **接口鉴权走 Telegram WebApp 的 `initData`**：`Authorization` 头使用的方案名是 `tma`，
  凭据是 Telegram 打开 Mini App 时下发的 WebApp `initData` 字符串。本仓库没有别的鉴权方案，
  且该接口仅最高管理员可调用。

**只有最高管理员能读写全局配置。** 群管理员拿不到 `/api/v1/settings`（后端
`require_super_admin`），前端也只渲染"群组设置"页——所以群管理员既看不到也改不了
全局经济、资源与交接字段。他们能改的仍然只有该群的 `group_settings`。

#### 变更历史 / 差异预览 / 回滚

`runtime_config` 是单行"当前值"，每次保存整体覆盖。查"这条规则昨天谁改的"靠
`runtime_config_history`——**每次成功落库的 save / restore 都在同一个事务里追加一行**
（历史插入失败会连主表一起回滚，绝不会出现"配置改了却查不到是谁改的"）。表与索引
由 `init_db()` 建，老库平滑升级。`initialize()` **不写历史、也不改 revision**：首次导入
与老字段归一化都是内部迁移，不是运维操作。

同样**只给最高管理员**（`require_super_admin`，群管理员 403）的六个端点：

| 端点 | 作用 |
| --- | --- |
| `POST /api/v1/settings/diff` | 保存预览：body `{"revision", "config", "secret_changes"}`，返回脱敏差异 + 摘要 + 会变成 pending 的重启字段 + 快照字节数。**不写库**；过期 `revision` 不拒绝，只在响应里回 `stale`。 |
| `GET /api/v1/settings/history?limit=&before=` | 版本列表（**分页**）：`revision` / `parent_revision` / `operation` / `summary` / `updated_by` / `updated_at` / `restore_source_revision` / `restart_pending`。**没有任何配置值**。 |
| `GET /api/v1/settings/history/{revision}` | 那一版的**脱敏**快照 + 每个 secret 路径的"配没配"状态。 |
| `GET /api/v1/settings/history/{revision}/diff?against=<other>` | **两个历史版本**之间的脱敏差异（`old` = against，`new` = 目标）。 |
| `GET /api/v1/settings/history/{revision}/restore-preview` | **回滚预览**：*当前配置 → 真正的回滚候选*（目标版非密钥字段 + 当前密钥 + 当前不透明值），不是两个历史版本之间的差异。 |
| `POST /api/v1/settings/history/{revision}/restore` | 回滚非密钥字段，body `{"revision"}` 是**当前** revision（乐观锁），返回新的 `api_document()` 外加 `restore: {source_revision, retained_secrets}`。 |

##### 预览与保存是**同一条**代码路径

`POST /settings/diff` 与 `PUT /settings` 共用 `RuntimeConfigManager._prepare_candidate` +
`_prepare_revision`。同一份请求体，两边得到**逐字相同**的候选、差异、摘要与快照字节
数，失败时抛**同一个**异常类型，于是拿到**同一个**错误码。以前预览自己抄了一份
`secret_changes` 解析器（只认 `replace` / `clear`）且完全跳过体积上限，于是会出现
"预览通过、保存 400"。`restore-preview` 与 `restore` 同样共用
`_restore_candidate`，不存在"预览说能回滚、点下去失败"。

`secret_changes` 的解析规则（`resolve_secret_changes`，两条路径共用）：

| 情况 | 结果 |
| --- | --- |
| `{"action": "keep"}` | 不动这个密钥。 |
| `{"action": "clear"}` | 清空并从密钥表删除该行（前提是**槽位仍在候选配置里**）。 |
| `{"action": "replace", "value": "…"}` | 写入新值；`value` 去空白后为空 → 拒绝。 |
| 路径不在候选配置的 `secret_paths()` 里（例如供应商已被删） | 拒绝，`unknown secret setting`。 |
| 动作不在 `{keep, clear, replace}` 里（含空串） | 拒绝，`invalid secret action`。 |

`replaced_paths` 与"配没配发生变化"的路径一起构成历史里的 `secret_paths_changed`，
**只有路径名**。

##### 回滚的四条语义

* **密钥一个都不动。** 历史里根本没有密钥（只有状态标记），回滚时合并**当前**密钥表，
  `runtime_config_secrets` 一个字节都不重写。回滚配置绝不能顺手清掉 API Key。
* **"从未落库"的槽位取当前值，取不到就明确拒绝**（见下面"自由 JSON 与 URL 凭据"）。
  绝不把这些槽位当成空值清掉。
* **永远前向产生新 revision**，并写一条 `operation="restore"`、`restore_source_revision`
  指向目标的历史。历史只追加、绝不倒写。
* **与保存走同一条校验与落库路径**（严格 schema + Site/Secret 配对 + `apply_to_settings`
  干跑 + 快照上限 + 乐观锁），过不了整笔回滚。

稳定错误码：

| 码 | HTTP | 出现在 | 含义 |
| --- | --- | --- | --- |
| `revision_conflict` | 409 | save / restore | `expected_revision` 过期。 |
| `history_revision_not_found` | 404 | `GET /history/{r}`、`/diff`、`/restore-preview`、`POST /restore` | 目标版本不存在**或**那一行 JSON 损坏。 |
| `history_revision_not_restorable` | 400 | `GET /history/{r}/restore-preview`、`POST /history/{r}/restore` | 该版本里有"从未落库、当前也取不到"的槽位。`message` 只列**路径名**，可直接展示。 |
| `invalid_settings` | 400 | `PUT /settings`、`POST /settings/diff`、`POST /history/{r}/restore` | 字段校验失败、`secret_changes` 非法、**快照超过 1 MiB**。预览与保存**同码**。 |
| `secret_storage_unavailable` | 400 | save / restore / restore-preview | 主密钥缺失或密文解不开。 |
| `invalid_history_revision` | 400 | 路径/查询里的 `revision`、`against` | 版本号不是正整数。 |
| `invalid_history_page` | 400 | `GET /history` | `limit` 不在 1…100，或 `before` 不是正整数。 |
| `super_admin_required` | 403 | 全部六个 | 群管理员 / 未鉴权。 |

内部异常原文、SQL 与库路径一律不出现在响应里。

##### 脱敏：两道锁 + 值形状

**第一道**：`storage_payload()` 把已登记的 secret 路径清空——但它完全依赖 `with_secrets`
手工列举每个密钥字段，以后加了带密钥的字段却忘了登记，就会**静默**把明文写进库。

**第二道**（`runtime_config_history.redact_payload`）是**三层**，从外到内：

1. **显式 secret 路径**：整个值替换成状态标记，**与值的类型无关**。
2. **键名启发式**（`api_key` / `api-keys` / `secret(s)` / `token` / `password` /
   `passwd` / `ciphertext` / `authorization` / `access_key`）：对**字符串、列表、字典**
   生效，**整个值**替换成 `<redacted:current>`。**列表和字典不是绕过的门**——
   `models.main.request_params.extra_headers = ["Bearer …"]` 曾经因为"键名不像密钥 +
   值不是字符串"而整条绕过脱敏，哨兵原样进了历史。对**数字 / 布尔 / null 不生效**：
   `resources.decision_history_token_budget` 是**数量**，把它换成字符串标记会让回滚时的
   严格 schema 直接报 `Input should be a valid integer`（"为了脱敏把配置改坏"）。
3. **值形状与 URL**（任何位置的标量字符串，与键名无关）：认证方案前缀
   （`Bearer …` / `Basic …`）与请求头整行（`Authorization: …` / `X-*-Token: …` /
   `Cookie: …`）整串替换；任何字符串里 URL 的 **userinfo** 与**敏感 query 参数**
   就地替换成 `https://<redacted:url-userinfo>@host/v1?token=<redacted:url-query>`，
   host / path / 非敏感 query 参数名保留（管理员仍看得出地址指向哪、带没带 token 参数）。

被替换掉的值不是掩码而是**状态标记**——掩码会把长度和前后缀漏出去。`summary` 只有
**数量 + 路径**，永远不含值；`secret_paths_changed` 只有路径名。差异引擎与脱敏共用
同一套判定，所以**差异响应里也不会出现被抹掉的明文**。

##### 自由 JSON 与 URL 凭据的回滚策略

`request_params` 是运营随便塞的 JSON，键名可以叫 `extra_headers`；供应商的 `api_base`
可以是 `https://user:pw@host/v1?token=…`。这类值**不进历史**（第 2、3 层已抹掉），
于是回滚只有两条诚实的走法，**没有第三条**：

* **(a) 保留当前值**（默认）：`restore_opaque_values()` 取**当前**配置里同一个路径的
  值。历史里有 A、当前是 B → 回滚后是 **B**，既不是 A 也不是空。
* **(b) 明确拒绝**：当前取不到（路径不存在，或当前那个位置本身也是占位）→ 抛
  `RuntimeConfigHistoryNotRestorableError`，消息里只列**路径名**，UI 直接显示
  `history_revision_not_restorable`。

**"悄悄清空"不是可选项。** 让一次回滚把管理员今天还在用的凭据清空（头列表变空、URL
里的 token 消失），比"回滚失败"危险得多——后者是管理员看得见的结果。代价要说清楚：
一条曾经把凭据写进 `extra_headers` 的历史版本，**回滚不了那个字段**，只保留当前值
（历史详情里该路径的状态显示为 `current`）。同一条规则也适用于提示词里那条**真的带了
凭据**的 URL；**不带凭据的普通链接（`https://docs.example.com/api?page=2`）逐字保留**，
回滚不受影响——脱敏的代价被刻意压到最窄。

`GET /api/v1/settings` 的编辑表单用同一套标记（`<redacted:current>` /
`<redacted:url-query>`，**空值不标记**），`PUT /api/v1/settings` 与
`POST /api/v1/settings/diff` 在校验之前把它们换回当前值，所以**把这份文档原样回传是
无损的**：运营不动它就保留原值，填了新值就写新值，清空列表就真的清空。
（`runtime_config.payload` 那一行存的是**当前生效配置**，仍含运营填写的自由 JSON 与
`api_base`；上面的保证覆盖的是**历史表、历史/差异/回滚预览三个端点与日志**。）

##### 供应商移除时的密钥保留策略

回滚到一个**供应商更少**的旧版本时，那几把 API Key 在运行时没有槽位了。策略是
**保留，绝不删除**：

* 回滚**不写**密钥表（`secrets=None`），密文与 `updated_by` 逐字不变。
* 一次保存**只删**"槽位仍在候选配置里、且这次被明确清空"的密钥行。供应商被从配置里
  删掉时，`providers.<name>.api_key` 不再是任何候选的合法槽位，于是它的密文**留在
  库里**——运行时看不见它，但它没被销毁。
* 想真正删掉一把 Key：先把供应商加回来（槽位回来），再用 `{"action": "clear"}` 删。
* 保留是**可见**的：回滚响应带 `restore.retained_secrets`，运维不会误以为这把 Key
  没了并重新填一遍。（这个判断只读密钥表的**路径名**、不解密，所以主密钥缺失时历史
  列表照常可用。）

端到端保证：**回滚 → 任意一次保存 → 重启**，当前密钥既**不会回退**（回滚从不重写密钥
表）也**不会被静默清空**（见上一条）。

##### 差异的诚实性

差异按点分路径逐字段列出，**列表不会塌成"整体变了"**：列表元素优先按身份字段
（`name` / `id` / `key`）成路径（`models.providers[gemini].api_base`），没有身份字段
才退化成下标（`economy.weekly_reward_points[3]`）。输出按路径排序（确定性），单个字符串
截断到 200 字符并标 `truncated`，条目数封顶 500。

**两侧状态相同且没有显式替换的密钥/不透明路径不产生任何条目。** 一次什么都没改的保存
预览必须给出空 `diff`（早期版本会给出 10 条"伪变更"，包括
`api_key cleared -> cleared`），摘要为 `新增 0 / 修改 0 / 移除 0：无变化`。
已知的诚实边界：**两侧都还"已配置"的密钥，在只比 payload 快照时看不出值被换过**——
替换与否由 `secret_paths_changed`（来自 keep/clear/replace 协议）单独记录。

##### 分页契约

历史表**只增不删**（见下一节），所以 `GET /history` **必须有界**：

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `20` | 1…100 的整数 | 这一页最多几行。越界一律 400 `invalid_history_page`（**不夹取**）。 |
| `before` | 无（= 最新一页） | ≥ 1 的整数 | 只返回 `revision < before` 的行。 |

响应多一个 `page` 对象：`{limit, before, count, has_more, next_before}`。`history` 在
页内按 revision **升序**；第一页取**最新**的 `limit` 行；把 `next_before` 作为下一页的
`before` 就能继续往更早翻，直到 `has_more` 为 false——整条链**不重不漏**覆盖全部行。
`next_before` 为 `null` 表示没有更早的行了。

##### 历史保留/清理：**本批未实现，且表不会自己变小**

这一条必须写在最前面，否则读者会以为这个表有某种自限机制——**它没有**。

* **增长形态：每次成功保存或回滚，永久追加一整份脱敏配置快照。** 追加写、只增不
  改、从不重写旧行。全新部署的默认配置脱敏后约 **80 KB**（其中约 65 KB 是提示词），
  所以粗算就是 **每次保存约 +80 KB**；保存 1 万次就是几百 MB。
* **没有 TTL，没有条数上限，没有后台清理任务。** 本批明确**不做**保留策略。分页
  解决的是"读端不能被撑爆"，**不解决**"写端会一直变大"。
* **唯一的刹车是"单份快照不能超过 1 MiB"**（`MAX_SNAPSHOT_BYTES`）。超限时**保存直接
  失败**，返回 400 `invalid_settings`（**预览同码**）；数据库**一个字节都不写**，
  `runtime_config` 保持逐字不变，也**不会**留下"主表已更新、历史缺失"的中间态。
  之所以选"失败"而不是"截断"或"跳过历史"：截断的快照会让"回滚到这一版"悄悄回滚到
  **半份**配置；跳过历史则直接破坏上面那条审计不变量。
  1 MiB 相对默认配置留了 10 倍以上余量，**任何正常部署都碰不到它**——它是病态输入
  的兜底，不是配额。（HTTP 上还有一层更早的闸：请求体本身就被 `_JSON_BODY_MAX_BYTES`
  限制在 1 MiB，所以这一层是**存储侧**的后备，不依赖传输层。）
* **1 MiB 量的口径是"列里真正存下来的那个字节串"。** `payload_redacted` 是 SQLAlchemy 的
  `JSON` 列，用序列化器**默认参数**写文本，也就是 `ensure_ascii=True`：非 ASCII 字符
  被转成 `\uXXXX`，**一个中文字符占 6 字节而不是 UTF-8 的 3 字节**。所以
  `snapshot_size_bytes()` 也按 `ensure_ascii=True` 量。曾经按 `ensure_ascii=False` 量，
  于是"1 MiB 上限"实际能存下约 2 MiB——180 000 个中字符量成 540 048 字节（放行），
  真正落库是 1 080 048 字节（超限），上限形同虚设。**这不是把上限调大，是把口径修对**；
  仓库里有 `SELECT` 回读比对守着"量的 == 存的"。
* **要补保留策略需要什么**（后续单独一批做）：一个按 `updated_at` 的删除入口 +
  一个"至少保留最近 N 版/最近 N 天"的硬保护（否则删过头就没有可回滚的版本了）+
  一个运维可见的"历史已清理"提示。
  在这一批落地之前，运维需要自己盯着这张表：想控制体积，唯一安全的动作是**少保存**
  （或把提示词改短），不要手工 `DELETE` 掉还想回滚的版本。

#### 热更 vs 重启

判定口径只有一条：**这个值在启动时被写进了某个长寿命对象吗？**

* 写进了 —— 就是 `restart`。准入 gate、aiohttp Session 的连接池、webhook 的
  worker 池与有界队列、tokenizer 线程槽位。它们换掉之前，进程里已经在跑的东西会
  被漏掉或泄漏。
* **没有**写进任何长寿命对象、每次操作现读 —— 就是 `hot`。各级超时、durable inbox
  的租约与保留期、批量与阈值。

口径必须与真实行为一致：曾经有一批字段被标成 `restart` 却每次现读（webhook 的各级
超时、inbox 租约、轮询超时、Telegram 的准入超时），结果是「保存成功」与「实际生效」
对不上号，管理员无法信任 `restart_pending`。现在它们一律标 `hot`；真正冷的只有容量类。
标 `restart` 的字段，读侧读的是**本进程装配的那一份**
（`runtime_config.applied_restart_values()`），保存只更新期望值并把它列进
`restart_pending`，重启才换。

这是最重要的一条区分，不要含糊：

* **标量**（价格、配额、超时、批量、阈值、条数）——**热生效**。每个动作开始时现取
  一次快照，管理员保存后下一次动作就用新值。
* **进程级闸门**（`asyncio.Semaphore` / `ReservedCapacityGate` /
  `PriorityAiohttpSession` / tokenizer 线程槽位）——**需要重启**。它们在 import 或
  建 Bot 时固化，热替换会漏掉正在执行的请求、泄漏 slot，或者让新旧两个闸门同时
  存在。它们只在 `bot.__main__._initialize_runtime_services()` 里
  `apply_startup_resources()` 装配一次，排在**模型元数据预取、建 Bot、任何收发消息
  之前**。

  这些字段各自带 `json_schema_extra={"reload_kind": "restart"}`，清单由
  `runtime_config.RESTART_REQUIRED_PATHS` 统一生成，API 的
  `restart_required_paths` 与 `restart_pending` 都会回它（**只给字段名，不回值**）。
  Mini App 上这些输入框带"需重启"标记，保存后的 toast 与概览页会逐条列出刚刚改动
  的那几项。

  重复调用 `apply_startup_resources()` 是安全的：配置没变就是 no-op；闸门里还有
  在跑的请求时它直接抛 `StartupResourceBusy`，**不会**硬拆闸门。

#### 一次操作只取一份快照

价格、批量这类东西在一次交易的中途被改掉，不应该让用户看到"菜单写 30 分、实际扣
了 80 分"。所以 `policy_runtime.pinned_section("economy")` 会在操作入口钉一份
不可变快照（`ContextVar`，asyncio 每个 Task 各自一份），显示 → 扣费 → 退款 → 回执
整条流程都读同一份。作用域最外层优先，嵌套调用复用最外层那份。

---

## 字段目录

下面按段列出本轮新增的可配字段。`reload` 省略即"热生效"。

### `moderation` — 部署绑定（最高管理员）

| 字段 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `log_channel_id` | `0` | `0` 或 `-1009999999999 … -1000000000000` | 审核命中证据频道。**默认 0 = 未配置**：公开部署开箱即用时不会向任何频道（含任何私人频道）投递，命中证据按既有 fallback 路径私聊最高管理员。部署者在 Mini App 审核面板或 `PUT /api/v1/settings` 显式写入后热生效。`config.toml` 只在首次导入时读；环境变量对本段无效。 |
| `review_handover_mention` | `""` | 空串或 `@[A-Za-z][A-Za-z0-9_]{3,31}` | 规则调整交接时 @ 的对象。**默认空 = 不 @ 任何人**，只发交接文案。空值时不会构造 mention 实体（不会退化成"对空串做 `rfind`"那种凭空造一个假 mention 的 bug）。含换行 / HTML 的一律拒绝。 |

既有字段（`enabled` / `warn_threshold` / `nsfw_image_guard_enabled` …）的语义与
默认值一字未改：`nsfw_image_guard_enabled`、`punish_quoted_author_enabled`、
`admin_moderation_enabled` 仍然是**默认关闭的 opt-in** 执法开关，公开树不会因为
升级就扩大执法范围。

> **机器目录只覆盖这一段里的两个字段。** `configuration-fields.json` 的
> `moderation.*` 条目只有 `log_channel_id` 与 `review_handover_mention`
> （`bot/tools/config_catalog.py:56-59` 里写死的 `MODERATION_FIELDS`）；上面提到的
> opt-in 开关、`log_channel_enabled`、`admin_alert_super_admin_enabled` 都不在目录里，
> 它们的默认值以 `bot/services/runtime_config.py` 的 schema 为准。

### `private_chat` — 私聊额度（最高管理员）

| 字段 | 默认 | 范围 | 单位 |
| --- | --- | --- | --- |
| `per_user_daily_limit` | `100` | 1 … 100000 | 条/天/人 |
| `admin_per_user_daily_limit` | `500` | 1 … 100000 | 条/天/人 |
| `global_daily_limit` | `20000` | 1 … 10000000 | 条/天/全网 |
| `admin_global_daily_limit` | `100000` | 1 … 10000000 | 条/天/全网 |
| `input_max_chars` | `1000` | 1 … 4096 | 字符 |
| `reply_max_chars` | `3800` | 512 … 4096 | 字符（Telegram 协议上限 4096） |
| `vision_budget_seconds` | `30.0` | 5 … 120 | 秒 |
| `memory_turns` | `12` | 1 … 200 | 轮（**仅内存兜底缓冲**；真正的历史按 token 预算读库） |
| `access_ttl_seconds` | `60.0` | 1 … **60** | 秒 |
| `search_daily_limit` | `2000` | 1 … 100000 | 次/天 |
| `voice_max_segments` | `6` | 1 … 20 | 段 |

两组档位互相独立：管理员花掉的额度不占普通成员的全局计数。关联校验：全局上限不得
小于同档单人上限。

`access_ttl_seconds` 的上界**就是今天的值 60**，不是"忘了设上界"。准入缓存里除了
档位还带着"已确认他在哪些授权群里"，被踢出群之后这个集合要尽快过期；调大 TTL
等于放大越权窗口，所以这个旋钮只允许收紧。TTL 变小会**立刻**对存量条目生效
（按当前 TTL 重新判龄），不会让已被移出群的成员"复活"。

`search_daily_limit` 热改**不会**重置当天已消耗的次数：计数只按自然日滚动。

### `economy` — 签到与积分商店（最高管理员）

| 字段 | 默认 | 范围 |
| --- | --- | --- |
| `checkin_daily_point_cap` | `10` | 1 … 100 |
| `checkin_rank_limit` | `10` | 1 … 100 |
| `checkin_violation_window_days` | `30` | 1 … 365 |
| `challenge_skip_cost` | `2` | **1** … 1000 |
| `tag_price_7d` / `tag_days_7d` | `30` / `7` | 0 … 100000 / 1 … 3650 |
| `tag_price_30d` / `tag_days_30d` | `80` / `30` | 0 … 100000 / 1 … 3650 |
| `pin_price` / `pin_hours` | `20` / `6` | 0 … 100000 / 1 … 720 |
| `lottery_price` / `lottery_daily_limit` | `5` / `10` | 1 … 100000 / 1 … 1000 |
| `lottery_prizes` | 见下 | 1 … 32 档，每档 `payout` 0…1000000、`weight` 1…1000000、`label` ≤32 字 |
| `tag_max_length` | `16` | 1 … **16** |
| `expiry_check_seconds` | `300.0` | 30 … 86400 |
| `expiry_pass_deadline_seconds` | `120.0` | 10 … 3600 |
| `expiry_batch_limit` | `200` | 1 … 5000 |
| `expiry_retry_seconds` | `900.0` | 30 … 86400 |

**下界是安全约束，不是笔误：**

* `challenge_skip_cost` 不得为 `0`——那等于"免费绕过审核质询"。
* `tag_max_length` 不得大于 `16`——Telegram 原生头衔长度上限。
* `lottery_prizes` 的权重之和必须 `> 0`。

**总权重是派生的，不另存一份。** 奖池里 `weight` 是相对权重，
`EconomySnapshot.lottery_total_weight` 直接对表求和，所以不存在"表改了、总权重
忘了改"这种错账。随机算法（一次 `randbelow(总权重)` + 前缀和）逐字未变，财务幂等
键（`lottery_spend_ref` / `lottery_prize_ref`）也完全不受影响。

**头衔防冒充词表与长租标记固定在代码里**（`bot/services/point_shop.py` 的
`TAG_BLOCKLIST` / `TAG_LONG_MARKERS`），不提供配置开关——配置不应该能删掉防冒充
词。

**已购买的权益不追改。** `expires_at` 与收据在购买那一刻定死；改价只影响之后的新
购买。

### `activity` — 每周活跃激励（最高管理员）

| 字段 | 默认 | 范围 |
| --- | --- | --- |
| `min_message_text_length` | `2` | 1 … 100 |
| `max_daily_messages` | `20` | 1 … 500 |
| `min_active_days` | `3` | 1 … 365 |
| `min_weekly_messages` | `10` | 1 … 100000 |
| `weekly_reward_points` | `[25,12,12,4,4,4,4,4,4,4]` | 1 … 100 项，每项 0 … 100000，合计 > 0 |

榜单长度（`weekly_top_n`）与周奖励总额（`weekly_total_points`）**都从这个向量
派生**，不再单独存一份会互相矛盾的数。得分公式
（`messages + 2 × active_days + replies_received`）不随配置变化。

结算时取一次快照：发奖写账整轮用同一份向量，不受中途改配置影响，也不影响已经发
出去的奖励（`weekly-activity:<iso week>:<user_id>` 上的唯一索引仍然是唯一的幂等
保证）。

### `checkin_reminder` — 签到提醒（最高管理员）

| 字段 | 默认 | 范围 |
| --- | --- | --- |
| `slots` | `[9,12,15,18]` | 1 … 12 项，每项 0 … 23，自动去重排序 |
| `slot_greetings` | `{9:"早上好",12:"中午好",15:"下午好",18:"晚上好"}` | 键必须 ⊆ `slots`；值 ≤32 字且不含 HTML |
| `auto_delete_seconds` | `600` | 0 … 86400（**0 = 不自动删除**） |
| `roster_max_names` | `20` | 1 … 200 |
| `stale_grace_seconds` | `900` | 60 … 86400 |

> **与 cron 的关系（重要）**：`slots` 只约束"命令/CLI 接受哪些 `--slot`"。**真正几点
> 发由外部 cron 决定**——`bot/tools/checkin_reminder.py` 的 docstring 里有可直接
> 复制的样例 crontab（每个时段一行，`CRON_TZ=Asia/Shanghai` 或 UTC 换算两种写法）。
> 改了 `slots` 之后，部署侧必须同步改 crontab，否则只是放宽/收紧命令行校验而已。
> 防重复提醒的幂等占位（`checkin_reminder_posts` 唯一索引）不随本段变化。

### `display` — 对外文案（最高管理员）

| 字段 | 默认 |
| --- | --- |
| `bot_display_name` | `助手` |
| `private_voice_title` | `语音回复` |
| `checkin_button_text` | `✅ 一键签到` |
| `shop_button_text` | `🛒 积分商店` |
| `search_query_prefixes` | `["诶","嗯哼","呀","欸"]` |
| `private_not_member_notice` | 抱歉，私聊只对已授权的群里成员开放… |
| `private_limit_notice` | 今天聊得有点多啦… |
| `private_global_limit_notice` | 今天找我聊天的人有点多… |
| `private_media_unsupported_notice` | 私聊里我目前只能看文字和图片… |

`search_query_prefixes` 会与 `bot_display_name` 一起参与"从问句里剥掉称呼"，
所以换品牌不用改代码。**协议标识不在这里**：`checkin:v1` 这个 callback_data、
`shop_<群号>` 这个深链前缀都是解析契约，配置改它们会让线上按钮失效。

人设提示词本身已经有运行时编辑（Mini App 的 Prompts 页，`prompts.persona` 在 12 个可编辑
提示词里），本页**不会**再造第二份人格。

但**「显示层中性」不等于「开箱中性人设」**，这两件事必须分开看：

- `display` 段的默认值确实是中性的（`bot_display_name = "助手"`），这一层不绑定任何品牌；
- `prompts` 段的默认值直接来自仓库里的 `prompt/*.md`（`PromptSettingsConfig.defaults()` →
  `load_prompt_defaults()`），其中 `prompt/persona.md` **是一份具体的、有名字的人设模板**。
  fresh install 之后它就是默认人设，除非部署者在 Prompts 页改掉。

也就是说，公开部署者应当在 Prompts 页按自己的产品改写这份人设模板——本页既不替你改它，
也不声称它已经是中性的。

### `resources` — 高级资源与进程级预算（最高管理员）

默认全部等于当前代码里真实生效的值，不配就是默认值。Mini App 上这一段在「运营参数」页
底部那个可折叠的「高级资源」面板里。

**需要重启的字段**：清单的**单一来源**是 `runtime_config.RESTART_REQUIRED_PATHS`
（`bot/services/runtime_config.py:2779`），它由每个字段自己的
`json_schema_extra["reload_kind"] == "restart"` 生成，外加历史遗留的 `bot.parse_mode`
（它在 Bot 构造时固化）。API 返回的 `restart_required_paths`、
[`configuration-fields.json`](./configuration-fields.json) 的 `restart_required_paths`
与 Mini App 的「需重启」标记都直接取它——**本文档不重写这个数量**，以那几处为准。
按组划分：

- **LLM 容量**：`llm_request_capacity`（8）、`llm_request_noncritical_capacity`（7）、
  `llm_request_normal_capacity`（4）、`llm_request_background_capacity`（2）
- **tokenizer 并发**：`llm_tokenizer_concurrency`（2）
- **待回复执行**：`pending_reply_execution_capacity`（4）
- **Telegram 三级容量**：`telegram_total_capacity`（64）、
  `telegram_noncritical_capacity`（60）、`telegram_normal_capacity`（44）
- **TTS 三级并发**：`tts_synthesis_concurrency`（3）、`tts_transcode_concurrency`（2）、
  `tts_private_concurrency`（2）
- **AV 查询并发**：`av_query_concurrency`（3）
- **webhook 总并发与三条车道**：`webhook_max_concurrent_updates`，以及 `auth` /
  `critical` / `security` 三条车道各自的 `*_concurrent_updates` 与 `*_queue_capacity`

保留容量的不变式在 schema 里强校验，改非法组合会被直接拒绝：

* `1 ≤ background ≤ normal − 2`（普通回复永远至少保留 2 个名额）
* `1 ≤ normal ≤ noncritical < total`（**至少留 1 个关键名额**）
* `2 ≤ telegram_normal ≤ telegram_noncritical < telegram_total`

**热生效**（每次动作现取）：`llm_stage_deadlines`（默认
decision/moderation 35、embed 60、compress/vision 90、main/skill 120、synopsis 20、
group_summary 15）、Telegram 各级准入超时、群待回复预算、管理员告警窗口/合并阈值/
状态上限/截断字数、送审整形的 burst/间隔/等待上限、TTS 段数与超时、AV 查询预算
与名字缓存、判定阶段历史预算、长期记忆各项、检索留档清理间隔与召回条数、向量归档
批大小/回填/扫描/候选/查询超时/维护间隔/索引租约。

`llm_stage_deadlines` 是**单一来源**：角色上显式设置的 `total_deadline_sec` 仍然
优先（0/未设置才用这里），`group_summary` 不再有第二份硬编码表。取消/超时/持有
语义一个字没改。

`archive_indexing_lease_seconds` 必须不小于 `archive_query_timeout_seconds`
（schema 校验），否则一个慢查询会在租约过期后被第二个 worker 抢走同一批消息，
造成重复写。

**webhook / 轮询 / 出站发送**（`resources.webhook_*` / `polling_*` /
`telegram_send_chat_parallel`）：这一段**不是**整段 restart，也不是整段 hot，**按项区分**：

- **restart**：webhook 服务的 worker 数与队列容量（`webhook_max_concurrent_updates`
  与三条车道各自的 `*_concurrent_updates` / `*_queue_capacity`），它们在
  `VerifyWebServer` / aiohttp session 构造时固化；
- **hot**：durable inbox 的租约、恢复批量、退避上限、清理间隔与批量，以及各级超时
  （`webhook_*_update_timeout_seconds`、`webhook_http_response_timeout_seconds`）与探测
  参数——这些每轮现取；
- **hot**：`polling_timeout_seconds` / `polling_http_timeout_seconds` /
  `polling_request_timeout_seconds`，它们只在**未启用 webhook** 的兜底轮询下生效；
- **hot**：`telegram_send_chat_parallel`，每次发送现取。

逐项的 `reload_kind` 以 [`configuration-fields.json`](./configuration-fields.json) 为准。

关联约束在 schema 里强校验，每一条都对应一个真实故障模式：

| 约束 | 不满足会怎样 |
| --- | --- |
| `webhook_inbox_lease_seconds` ≥ 最大的 update 端到端预算 | 租约先到期 → 恢复循环把**仍在执行**的 update 交给第二个 worker → 管理员的 /ban、/unban 回调**被执行两次** |
| `webhook_inbox_lease_seconds` ≥ `webhook_inbox_retry_max_seconds` | 一次合法重试被恢复循环判成僵尸 |
| `webhook_http_response_timeout_seconds` ≥ 最大的端到端预算 | handler 还在跑，HTTP 层已断开 → Telegram 重投 |
| 每条车道并发 ≤ `webhook_max_concurrent_updates` | "独立车道"的隔离承诺不成立（车道比总池还大） |
| 每条车道队列容量 ≥ 该车道并发 | 队列比 worker 还小 = 立刻丢更新 |

`webhook_max_concurrent_updates` 同时是 webhook 连接池的上限；`polling_*` 只在
**未启用 webhook** 的兜底轮询下生效。

---

## 数据库层开关（新群接入与按群成本）

这一层的状态**不在**运行时配置里（没有 env、没有 config.toml 条目），而是按群
存在数据库中，并且**默认全部关闭 / 未设置**。把它们放进 runtime_config 会让一个
「没开的功能」变成每次启动都要加载的热配置。

| 位置 | 字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| `group_budgets` | `enabled` | `false` | 护栏总开关。关闭时**不做每调用一次数据库读取** |
| `group_budgets` | `daily_attempt_limit` | `NULL` | 每日可选调用次数上限；未设置 = 不限 |
| `group_budgets` | `daily_token_limit` | `NULL` | 每日 token 上限；未设置 = 不限 |
| `group_budgets` | `daily_money_micro_limit` | `NULL` | 金额上限（整数 micro）。只在计价完整、币种一致时才允许设置 |
| `group_budgets` | `operation_limits` | `{}` | 功能操作次数配额（TTS / 媒体 / 搜索等非 LLM 路径） |
| `group_budgets` | `warn_ratio` / `pause_ratio` | `0.8` / `1.0` | 本轮的**设计默认阈值**，不是现网策略 |

模型角色、供应商密钥、群规、积分、审核动作与阈值**不受**本轮影响。

`/api/v1/prices` 的价格目录同样默认**空**（`version=1`，无任何条目）：没有单价
时金额一律显示为未知，而不是按 0 计。

## 保持固定的参数（不提供开关）

这些是安全 / 协议 / 算法不变量，**不是**运营参数，做成开关只会制造"关掉之后就不
安全了"的错觉：

* **身份与授权**：最高管理员判定、管理员缓存 TTL 上限、群授权列表。
* **SSRF / 抓取**：`bot/services/skills/platform_common.py` 的 URL 语法校验、
  host allowlist 与 DNS 钉扎。只允许 http/https、禁 URL 内嵌凭据、host 必须在
  allowlist 内。
* **协议硬上限**：Telegram 单条消息 4096、流式安全线 3800、webhook 请求体上限、
  callback_data / start payload 前缀、实体偏移的 UTF-16 规则。
* **租约与终态**：入群验证的 nonce 宽限、准备中/终态租约、解封恢复宽限。
* **退款与财务幂等**：`ref` 键的构造、唯一索引、"先扣再用"的退款路径。
* **资源健康看门狗**：fatal 阈值与进程退出行为。
* **上下文安全门禁**：业务总预算与输出预留的相对关系（预留必须小于总预算，0 不能
  关掉门禁）。上界取 `bot.utils.budget` 的单一来源。

---

## 从旧版本迁移

1. **不需要数据库迁移。** 新字段全部带默认值；`runtime_config` 里已有的文档在
   `RuntimeConfig.model_validate()` 时自动补齐，原有值一个字不动。
2. **旧 payload 缺新字段** → 走 schema 默认值，正是"没配置"的行为。
3. **`display` 的默认显示名会从旧人设别名变成中性词，需要你手工迁移。**
   旧版本把品牌名**硬编码**在源码里，出现在人设提示词、语音条标题与检索称呼前缀三处。
   本轮**只把显示层抽成了 `display` 段**（语音条标题、检索称呼前缀、以及文案里出现的
   显示名），公开树的默认值是**中性的**（`bot_display_name = "助手"`、
   `private_voice_title = "语音回复"`、`search_query_prefixes = ["诶","嗯哼","呀","欸"]`）。
   升级到这一版之后：

   * **人设提示词既没有被抽走、也没有被改写**——`prompts.persona` 仍是原来那一份
     （含它自己的名字与语气框架），说话风格、称呼与互动方式保持原样；抽出来的只有
     "显示名"这一层，不是人格本身；
   * 但**凡是依赖"显示名"的行为会换掉**：检索查询词的称呼剥离、私聊语音条标题，
     以及群公告/文案里出现显示名的地方。要保持升级前的观感，升级时请在 Mini App
     里按旧配置**就地写入** `display.bot_display_name`、`display.private_voice_title`
     与 `display.search_query_prefixes`（把旧别名加进前缀列表，它会被自动并入一起
     剥离）。

   本文档**不填任何具体生产名称或账号**——那是部署者自己的数据。
4. **其余私有设置同样就地保留，不要从本 fork 复制。** 审核频道 id（默认 `0`）、
   交接对象（默认空）、提示词、其它文案都是部署者自己的数据。
5. **重启一次。** 换过进程级闸门字段（`resources.*` 的 restart 段）之后必须重启；
   只改热字段不需要。
6. **`checkin_reminder.slots` 不会、也无法自动更新外部 cron。** 它只改命令行白名单
   （`--slot` 接受哪些值）与相关文案；真正几点发由部署侧 crontab 决定。改完请同步
   修改 crontab，否则新时段不会真的被触发。Mini App 的运营参数页对此有同样说明。
7. **`config.toml`** 只在首次导入时读，升级不会重新导入。

---

## 相关文件

| 关注点 | 文件 |
| --- | --- |
| strict schema、持久化、secret、revision | `bot/services/runtime_config.py` |
| 读侧视图（默认值镜像） | `bot/config.py` |
| 快照与消费者登记表 | `bot/services/policy_runtime.py` |
| 启动装配 | `bot/services/startup_resources.py` |
| API 与权限 | `bot/web/settings_api.py`、`bot/web/auth.py` |
| Mini App | `bot/web/static/app.js`、`bot/web/static/styles.css` |
| 机器可读目录生成 | `bot/tools/config_catalog.py` |
| 常量分类与「无法验证的点」 | [`docs/constant-classification.md`](./constant-classification.md) |
| 新群接入 / 用量归属 / 预算护栏的公开契约 | [`docs/onboarding-and-budget.md`](./onboarding-and-budget.md) |
