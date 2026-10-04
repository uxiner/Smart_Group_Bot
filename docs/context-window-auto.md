# 上下文上限按实际模型自动匹配（2026-10-04 生产热修）

## 事故

2026-10-04 11:42–11:46，group history=2003：

* 网关 `GET /v1/models` 宣告主模型 `context_length=1000000`、`max_output_tokens=128000`；
* 但全项目把 `max_context_tokens = 278528`（272K）当**全局硬上限**；
* 最终闸门对 ≥100K 字符的载荷退回"一字符一 token"的估算，**并且标成 `exact=True`**，
  同一份载荷在装配阶段（CJK 感知口径：CJK 1 token/字，其它 ~3 字符/token）判定"装得下"，
  在闸门里却是 `783800/278528`；
* 主模型与备用**都没有发出 HTTP**，直接 `skipping_model`；最后由 `force_reply` 的硬编码
  话术「我在，直接说就好~」顶上，四条群友消息得到同一句假回答。

## 口径

**上限 = 该 endpoint 自己的实际窗口**，不再有一个全局固定值，也不再有人为的 2M 上限：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 网关 `/models` 元数据 | 按**原样 id** 精确匹配（先整个 id，再退一步只去掉 `provider/` 前缀）；绝不猜别名 |
| 2 | litellm 模型注册表 | **只在没有 `api_base`（直连厂商）时**采信；走自建网关时不采信，避免"凭别名冒认" |
| 3 | 保守降级 | `max_context_tokens`（默认 272K）。明确日志：`source=conservative_default` |

* **auto + 可信元数据 = 真实窗口原样**：网关自报 3M/4M 就按 3M/4M 装配与放行
  （`CONTEXT_WINDOW_MAX = 2M` 只约束兼容字段/保守降级值/fixed 模式）。
  装配入口（统一闸门 / 群聊历史 / 私聊历史 / MemoryService）与最终闸门都没有 2M 的人为上界。
* 总窗口与输入上限严格区分：元数据给了 `max_input_tokens` / `inputTokenLimit` 就直接用
  （**不再重复减输出**）；只给了总窗口才 `窗口 − 本次输出预留`，只减一次。
* 每个 stage / 每个 fallback 各用各的 limit：主模型 1M 不会被未知备用拖小；主模型失败后
  进入较小窗口的备用时，载荷会**重新裁剪**适配该候选，而不是整条跳过。
* 未知 ≠ 无限：查不到元数据时仍是保守降级值（默认 272K，按兼容区间夹取）。
* `decision` / `moderation` 等短上下文链路不变大。

## 缓存与取数（`bot/services/model_limits.py`）

* 只查**已配置且已带认证**的 endpoint：必须有 `api_base` + key，否则跳过（不探测第三方）。
* 键 `(provider, api_base, model)`；**成功 TTL 6 小时**，**失败/未命中负缓存 5 分钟**。
* **周期刷新**（`PeriodicModelMetadataRefresh`，默认 5 分钟一轮，挂在
  `background_tasks` 上随应用一起关停）：缓存 fresh 时这一轮**零网络**；成功记录过期就重取，
  负缓存过期就重试。单轮失败/回调失败只记日志，循环绝不退出（否则会被当致命错误）。
  所以"6 小时 TTL / 5 分钟重试"是真的兑现，而不是只有启动那一次。
* **刷新失败绝不覆盖已有的成功记录**：网关 503、或 `/models` 只回了部分条目时，保留原值
  （`resolve` 标注 `expired_cache`，下一轮继续重试），不会让主模型 1M 突然退化回 272K。
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
* `home_work2api` 宣告的 1,000,000（以及将来可能的 3M/4M）是否真的能吞下接近该规模的请求
  （本修复只保证**按它自报的上限放行**，不保证网关侧真的做完了对应规模的验收）。
* 长历史（2000 条）下的真实首字节延迟与事件循环占用（保守估算在事件循环上做正则扫描，
  估算本身未 off-thread；按 1M 窗口时单次约数十毫秒量级，实测需复核）。
* 周期刷新的真实节奏与网关压力：默认 5 分钟一轮、缓存 fresh 零网络；到点重试的行为由
  单测覆盖（亚秒 TTL + MockTransport），但生产网关上的实际表现需实测。
* `MemoryService.reconfigure` 在"窗口变化"时的真实开销（只重算预算并收敛已加载投影，
  不清历史、不改留存）；本机只验证了语义与留存字段不变。
