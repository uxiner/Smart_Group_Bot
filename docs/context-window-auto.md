# 上下文：模型窗口自动发现 + 每轮 272Ki 业务预算（2026-10-04 生产热修）

## 事故

2026-10-04 11:42–11:46，group history=2003：

* 网关 `GET /v1/models` 宣告主模型 `context_length=1000000`、`max_output_tokens=128000`；
* 但全项目把 `max_context_tokens = 278528`（272K）当**全局硬上限**；
* 最终闸门对 ≥100K 字符的载荷退回"一字符一 token"的估算，**并且标成 `exact=True`**，
  同一份载荷在装配阶段（CJK 感知口径：CJK 1 token/字，其它 ~3 字符/token）判定"装得下"，
  在闸门里却是 `783800/278528`；
* 主模型与备用**都没有发出 HTTP**，直接 `skipping_model`；最后由 `force_reply` 的硬编码
  话术「我在，直接说就好~」顶上，四条群友消息得到同一句假回答。

## 口径（最终：模型窗口发现与业务预算分离）

**每一轮的业务预算是固定的 272Ki = 278528**，与模型自报的窗口无关。它覆盖
system/人设 + 工具定义 + 记忆召回 + 检索留档 + 群/私聊历史 + 本轮消息 + 工具结果
**以及输出预留**——不是只限制历史：

```
每候选总有效窗口 = min(模型真实窗口, 272Ki)
输入硬上限      = 总有效窗口 − 32Ki(32768)  ⇒  正常恒为 245760
```

* 模型真实窗口 1M / 4M → 业务窗口仍是 272Ki（**不把百万窗口每轮填满**）；
* 模型真实窗口更小（例如 128K）→ 保持模型那个更小的值（小模型只更紧）；
* 模型未知 → 保守降级值参与同一个 min（未知 ≠ 无限）；每个 fallback 按自己的窗口裁剪，
  未知备用不会把已知主模型压小；
* 本次输出需求更大（`max_tokens > 32Ki`）→ 预留取它，输入上限进一步收紧。

### 运行时可配置（推荐值即默认值）

| 配置项 | 默认 | 含义 | 校验 |
| --- | --- | --- | --- |
| `bot.context_budget_tokens` | 278528（272Ki，**推荐**） | 每轮业务总窗口 | 1024..16,000,000；0 非法（不能关掉门禁） |
| `bot.context_reserve_tokens` | 32768（32Ki） | 输出/工具预留（默认输入上限 245760） | 1024..8,000,000，且**必须小于**总预算 |
| `bot.group_history_max_messages` | 1000 | 群历史单次读取条数 | 1..20,000 |

* 三个值都**运行时可读写**（`/settings` 与运行时配置 API），启动、apply/reconfigure 与
  回读走同一条 `apply_to_settings`；旧库缺字段只补推荐默认，显式配置保留
  （旧字段 `group_history_reserve_tokens` 会被镜像到 `context_reserve_tokens`）。
* 配置是权威：显式配大不会被隐藏常量截断（与模型真实窗口取小）；文档建议总预算不超过
  272Ki，但那是建议不是硬编码上限。
* 预留只扣一次：本次实际输出需求（该角色 `max_tokens`）大于配置预留时按它进一步收紧。
* 非法配置（0/负数/预留 ≥ 总预算）在配置层被 pydantic 明确拒绝；万一运行时仍拿到非法值，
  兜底是**收紧**到留 1024 输入，而不是关掉门禁。

**模型窗口的发现**（只影响"是否更紧"，不放松业务预算）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 网关 `/models` 元数据 | 按**原样 id** 精确匹配（先整个 id，再退一步只去掉 `provider/` 前缀）；绝不猜别名。解析值 1M/4M **原样记录、原样出现在日志/快照**，不做隐藏截断 |
| 2 | litellm 模型注册表 | **只在没有 `api_base`（直连厂商）时**采信；走自建网关时不采信，避免"凭别名冒认" |
| 3 | 保守降级 | `max_context_tokens`（默认 272K）。明确日志：`source=conservative_default` |

* 总窗口与显式输入上限严格区分：元数据给了 `max_input_tokens` / `inputTokenLimit` 就
  直接用它（**不再重复减输出**），再与业务输入上限取小。
* 每个 stage / 每个 fallback 各用各的 limit；主模型失败后进入更小的备用时，载荷会
  **重新裁剪**适配该候选，而不是整条跳过。
* 工具循环每一轮都**重新计量**：预留只减一次，不按轮次累加，也不把预算塞到零余量。
* 群聊单轮最多装配**最近 1000 条**（`GROUP_HISTORY_MAX_MESSAGES`），归档读取按页从新到旧、
  "条数或预算先到即停"——不会读出几万条再切片。不删归档、不开自动压缩、不动审核/留存。
* `decision` / `moderation` 等短上下文链路不变大。

## 缓存与取数（`bot/services/model_limits.py`）

* 只查**已配置且已带认证**的 endpoint：必须有 `api_base` + key，否则跳过（不探测第三方）。
* 键 `(provider, api_base, model)`；**成功 TTL 6 小时**，**失败/未命中负缓存 5 分钟**。
* **周期刷新**（`PeriodicModelMetadataRefresh`，默认 5 分钟一轮，挂在
  `background_tasks` 上随应用一起关停）：缓存 fresh 时这一轮**零网络**；成功记录过期就重取，
  负缓存过期就重试。单轮失败/回调失败只记日志，循环绝不退出（否则会被当致命错误）。
  所以"6 小时 TTL / 5 分钟重试"是真的兑现，而不是只有启动那一次。
* **刷新失败绝不覆盖已有的成功记录**：网关 503、或 `/models` 只回了部分条目时，保留原值
  （`resolve` 标注 `expired_cache`，下一轮继续重试），不会让已知窗口退化成保守降级。
* 单次查询 4 秒超时、最多 3 个并发；启动最多等 6 秒，等不到就 `shield` 到后台继续，
  拿到后经 `_MemoryContextWindowSync` 把新窗口套到 MemoryService 预算上（不重启也生效）。
* 路由/模型变更时（`bot/__main__.py` 的配置应用回调）`force=True` 重取一次，不阻塞配置应用。
* 日志只打印 provider / host（`redact_base` 去掉内嵌凭据）/ 原样 model id / 来源 / 数值；
  **绝不打印 api_key**。主链路读缓存是纯同步的，绝不查网络。

## 最终闸门（`bot/services/llm.py` + `bot/services/payload_fit.py`）

* 计量一致：保守上界改用与装配链路同一个 `estimate_text_tokens`（可加：工具定义 + 每条消息），
  且**永远标 `exact=False`**；只有真实分词器（离线程 + 1 秒有界超时）成功才算 exact。
* 超限不再"整条 skip"：按 **最老历史 → 检索留档 → 记忆召回** 裁到能发；核心系统块、本轮
  消息永不裁。只有固定层本身装不下时才诚实失败（不发请求）。
* **工具协议整对不可拆**（`is_tool_protocol_message`）：`assistant(tool_calls)` 与它的
  `tool` 结果**不依赖调用方有没有标 `core`**，任何层标记都不能把它们整条丢掉；超长工具结果
  只截断正文。配对完整是硬约束：孤儿消息会被 OpenAI 兼容接口 400，也会让模型以为工具没跑过
  而重复执行副作用。
* 层标记（`_ctx_layer`）只在进程内使用；发请求前 `strip_internal_message_keys` 把所有
  下划线开头的内部键清掉（严格的 OpenAI 兼容网关会拒绝未知字段），且不改写原载荷
  （每个 fallback 都要拿原始载荷重新裁）。
* `tag_rendered_history` 依赖 `sanitize_history_for_llm` 的"一对一"渲染；已用真实函数锁住该
  假设（用例会先炸）。一旦上游加了过滤，映射退化为"全部标 core"（不裁），而不是猜着错裁。
* 失败不再假装成功：群聊强制回复分支改为诚实提示
  `REPLY_UNAVAILABLE_NOTICE = "抱歉，这次没能生成回答，请稍后再试一次。"`，
  不虚称已签到/已排程/已完成任何副作用。

## 兼容与迁移

* 新增 `context_window_mode`（`auto` 默认 / `fixed` 逃生舱）。老库缺这个字段时由
  `_normalize_deprecated_runtime_payload` **一次性**补 `auto`（幂等，只在缺字段时写，
  不会覆盖管理员显式选择的 `fixed`）。
* `max_context_tokens` / `private_chat_history_token_budget` / `group_history_token_budget`
  全部保留（兼容保存），在 `auto` 模式下只作为保守降级值；只有 `fixed` 模式或"查不到
  任何元数据"时才按其装配，保持迁移前的深度口径。
* `/settings` 只把原先的"最大上下文 Token"输入改成"固定上限（仅 fixed 生效）"，
  并新增一个模式选择；没有给用户任何**新的**固定上限输入。

## 仍未验证（本机跑不了真实依赖，父代理在 VPS 实测）

* 真实网关 `/v1/models` 的实际字段名与响应形状（本仓按 OpenAI `data[]` 与 Gemini `models[]`
  两种形状解析，其它形状一律当"没有元数据"保守降级）。
* 模型宣告值（1M，将来可能的 3M/4M）只是**发现**结果：本修复只用它做"更紧"的裁剪，
  每轮实际发出的载荷恒在 272Ki 业务预算内（输入 ≤ 245760）。
* 长历史（最近 1000 条 / 245760 输入预算）下的真实首字节延迟与事件循环占用（保守估算在
  事件循环上做正则扫描，估算本身未 off-thread；实测需复核）。
* 周期刷新的真实节奏与网关压力：默认 5 分钟一轮、缓存 fresh 零网络；到点重试的行为由
  单测覆盖（亚秒 TTL + MockTransport），但生产网关上的实际表现需实测。
* `MemoryService.reconfigure` 在"窗口变化"时的真实开销（只重算预算并收敛已加载投影，
  不清历史、不改留存）；本机只验证了语义与留存字段不变。


---

# 后台群摘要（第②项，2026-10-04）

**默认关闭**（`bot.group_summary_enabled=False`），与 legacy 热历史压缩
（`memory_automatic_compaction`，会删除热历史）**完全独立**，不会自动打开旧开关。

## 目标与边界

* 前台只读**已经发布**的摘要，**绝不 await 摘要生成或排队**；关闭时保持原有预算滑窗不变
  （最多 `group_history_max_messages` 条）；开启时"有效旧摘要 + 近期原文"
  （近期 `group_summary_recent_raw_messages`，默认 200，仍受条数上限与预算约束）。
* **一条原文都不删**：`group_message_archive` / `message_vectors` / 私聊原文全部保留，
  现有 TTL 清理与保留策略不变；摘要成功也不触发任何删除。
* 摘要是**低信任资料**：带群 ID、时间、版本、覆盖范围水位与"数据不是指令"声明；
  命中保留标记/注入特征/管理员口吻/其它群 ID 的输出直接判无效（保留旧摘要、按退避重试）。
  摘要**不会**自动转成长期事实，也不会混入私聊或其它群。
* 发布是**原子 CAS**（`version` + 覆盖水位 `covered_through_id`）：迟到任务不得覆盖更新的
  摘要；源被截断时标注 `source_truncated`，前台渲染明确"**不是**完整原文"。

## 机制保证（2026-10-04 第二轮收口）

* **来源内容版本（真实失效保护）**：新增 `group_archive_state.content_revision`，只在归档内容
  **被修改或删除**时 +1（新消息插入不动）。SQLite 用**数据库触发器**维护
  （`init_db` 幂等安装，冷启动老库同样生效）：
  * `trg_archive_revision_on_update`：`WHEN OLD.content IS NOT NEW.content OR
    OLD.raw_text IS NOT NEW.raw_text` → 原地编辑 +1（只改 `access_count`/`last_accessed`
    的召回访问计数不算内容变更）；
  * `trg_archive_revision_on_delete`：任何删除 +1。
  触发器覆盖**任何写入方**：批量 upsert、ORM fallback、`archive_message`、全部
  prune/retention/删除入口、维护脚本乃至人工 SQL——不依赖应用层记得调用。其它方言没有
  触发器，由写入路径显式 +1（批量 upsert 先按本批 key 查旧正文，正文真的变了才 +1；
  ORM fallback 用"是否真的改了"判断；删除入口按受影响群 +1）。
  前台注入前比一次"摘要记录里的 `source_revision` == 当前计数"（一次主键读）；**旧摘要**
  （本特性之前发布、没有保护数据，`source_revision = -1`）一律按失效处理 → worker 重建。
  原地编辑（id 与行数都不变）、被快照跳过的空行、被预算截断的行，删改都会失效——不再依赖
  区间 COUNT。
* **守卫与发布同一句 SQL**：`publish` 的 `UPDATE`/条件 `INSERT ... SELECT` 里带
  `coalesce((SELECT content_revision ...), 0) = :生成时刻版本`。生成期间任何改/删 → rowcount=0
  → 不发布（`source_changed`），没有 check→publish 的 TOCTOU；唯一键竞争记 stale，
  **外键等其它 IntegrityError 照常上抛**。
* **入场与执行期限分离**：调度器先用 `gate.acquire_permit(BACKGROUND, timeout=queue_wait)`
  做**有界入场**（超时 → `admission_timeout`，不算执行超时、不占失败退避），拿到许可后交给
  LLM 的**实际请求任务**（`permit.consume()`/`release()`，取消不合作也不提前归还），
  模型/重试/fallback 才计入 `deadline_seconds`；同一路径不会二次 acquire（背景 2/2 全占时
  仍能完成）。边界不变：总 8 / normal 4 / background 2 / HIGH-CRITICAL 保留。
* **回复压力不算入场等待**：`slot_waiter` 为真时重置所有 pending 的入场计时并安排有界唤醒
  （≤1s），压力解除后计时从"现在"重新开始 → 压力超过 `queue_wait` 也不会被判 `queue_expired`
  或无端退避，且**不需要新通知**就能自动继续；`_next_wake_in` 不再把 0 过滤成 None。

* **真正的原子发布**：`UPDATE ... WHERE group_id=? AND version=? AND covered_through_id<?`
  （版本与水位条件都在 SQL 里）+ 行不存在时 `INSERT`（主键唯一冲突即失败）；`session.get`
  读改写只用于展示。旧摘要失效重建走 `allow_watermark_rewind + reset_coverage`：水位可
  回退但**版本 CAS 仍生效**（迟到任务不能覆盖重建结果）。
* **失效重建不复活已删除内容**：worker 生成前检查旧摘要覆盖是否仍然完整（原文被删/编辑
  → 旧正文**绝不进提示词**，从现存原文重建）；发布前**再次**确认本批依赖源未变
  （模型调用期间被删 → 不发布，`source_changed`）。前台每次读都做完整性检查（不做 TTL
  缓存短路），失效即不注入、退回预算滑窗。
* **统一扫描序**：摘要的"最近 N 条"、候选与覆盖水位**全部按归档行 id**（插入序、单调全序）；
  时间乱序/补录的行会以更大的 id 进来并被覆盖，不会被永久跳过。
* **调度推进**：运行期间新到的通知标 redirty，跑完公平重排到队尾；成功发布后若仍有
  backlog，按 `min_refresh` 排到队尾并**到点自动继续**（不需要群里再发消息）；
  "有意等待"（失败退避/同群最小刷新）只更新 `ready_at`、不算排队超期也不占执行槽
  （`queue_wait` 默认 30s 与 `min_refresh` 60s 不会互相误伤）。
* **有界状态**：pending 每群只有一份；`_backoff_until`/`_failures`/`_last_success_at`
  过期即清、容量兜底 `max(256, pending_capacity)`；观测用的 `last_success` 只留最近 200。
* **有效并发** = `min(配置, 门禁背景容量)`：门禁背景固定 2（总 8 / normal 4 / 回复 ≥2 不变），
  配置调大只显示允许到 2 并告警，不会多排任务抢槽位、每个占满 15 秒 deadline。
* **预算口径**：原文 batch + 旧摘要 + 提示词**一起**卡进 `batch_max_input_tokens`（超了从
  最旧丢并标记"不是完整原文"）；摘要输出上限走**API `max_tokens`**（不只靠事后截断）；
  `MemoryService._llm_input_budget` 与最终闸门统一按业务 `context_reserve_tokens`
  （不再重复扣 `max_output`）。摘要落后于水位时，前台在 1000 条/整次预算内尽量保留
  未覆盖原文，追平后才收敛到 `recent_raw`；摘要失败/无摘要始终走预算滑窗。

## 触发

近期原文窗口之外的未摘要旧消息累计达到 `group_summary_trigger_messages`（默认 200），
**或**本轮装配逼近有效输入预算的 `group_summary_trigger_budget_ratio`（默认 0.85）且确有
未摘要旧消息时触发。每次只读**有界快照**（单批条数 + 单批输入 token 双上限），
不会全库读取、不会逐条压缩。

## 资源隔离（上线门禁）

摘要模型调用走 `ExecutionPriority.BACKGROUND` + 主门禁的 `background_capacity`：

```
总上限 8（不变）
normal 4（回复 + 摘要共享）   background 2（仅摘要）   noncritical 7 / CRITICAL 保留 1
⇒ 回复 + 摘要 ≤ 4，摘要 ≤ 2，普通回复永远至少保留 2 个名额
```

* 摘要**不能**用 HIGH/CRITICAL 提权，也不会为它把总门禁改成 50。
* 普通回复在等入场时，调度器**不再 claim** 新摘要（回复优先）。
* 排队只在内存（不占模型槽、不占数据库事务）；读取快照与发布各用**一个短会话**，
  **没有跨模型 await 的数据库锁**。
* 取消有界；取消不合作的 orphan 不发布迟到结果，也不提前释放自己那份并发计数。
* 停机/重配置可追踪：调度器是常驻后台任务，随 `background_tasks` 一起关停；
  运行时配置变化后 `reconfigure()` 唤醒循环，**新容量对新任务生效**，不动已入场许可。
* 重启连续：**摘要与覆盖水位在数据库里**（`group_summaries.version` +
  `covered_through_id`）；内存里的 pending 队列不持久化，但触发条件在每次 claim 时用
  "归档条数 COUNT"重新推导，所以重启后不会漏也不会重复覆盖。

## 运行时可配置（推荐值即默认值）

| 配置项 | 默认 |
| --- | --- |
| `group_summary_enabled` | `False` |
| `group_summary_recent_raw_messages` | 200 |
| `group_summary_max_tokens` | 4096 |
| `group_summary_batch_max_messages` | 200 |
| `group_summary_batch_max_input_tokens` | 16384 |
| `group_summary_global_concurrency` | 2 |
| `group_summary_per_group_concurrency` | 1（硬约束，不可放大） |
| `group_summary_deadline_seconds` | 15（入场后整体硬超时） |
| `group_summary_queue_wait_seconds` | 30（排队过期即跳过，不计入模型时限） |
| `group_summary_min_refresh_seconds` | 60 |
| `group_summary_failure_backoff_seconds` / `_max_seconds` | 60 / 3600 |
| `group_summary_pending_capacity` | 1000（容量，不是可支持群数） |
| `group_summary_trigger_messages` | 200 |
| `group_summary_trigger_budget_ratio` | 0.85 |

这些数值是**推荐起点，不是已压测承诺**；生产启用前需在 VPS 用真模型做摘要 + 聊天/审核
混合探针。

## 观测

`resource_health` 的 `group_summary` 快照暴露：pending / running / pending_capacity /
global_concurrency / per_group_concurrency / backoff_groups / next_retry_in_seconds /
每群合并与退避状态（最多 50 个）/ claimed / merged / queue_full / queue_expired /
deadline_exceeded / success / failure / skipped_reply_waiting / 最近成功版本与覆盖水位 /
`peak_model_concurrency`（摘要自身实际 LLM 峰值并发）；主门禁快照（含
`background_capacity` / `active_background` / `waiting_background`）在
`llm_resource_health_snapshot` 里。

## 生产启用 / 回滚

1. 先只改配置：`group_summary_enabled=true`（其余用默认），观察 `group_summary` 快照与
   回复延迟；`global_concurrency` 建议先 1。
2. 回滚：把 `group_summary_enabled` 改回 `false`（前台立刻回到原滑窗；
   已发布的摘要行保留但不再注入），或直接重启到上一个镜像版本。
   **不需要**动 `memory_automatic_compaction`，也不需要清理 `group_summaries`
   （它只增不减，且不删任何原文）。
