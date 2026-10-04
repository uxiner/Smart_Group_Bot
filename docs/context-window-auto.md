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

**上限 = 该 endpoint 自己的实际窗口**，不再有一个全局固定值：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 网关 `/models` 元数据 | 按**原样 id** 精确匹配（先整个 id，再退一步只去掉 `provider/` 前缀）；绝不猜别名 |
| 2 | litellm 模型注册表 | **只在没有 `api_base`（直连厂商）时**采信；走自建网关时不采信，避免"凭别名冒认" |
| 3 | 保守降级 | `max_context_tokens`（默认 272K）。明确日志：`source=conservative_default` |

* 总窗口与输入上限严格区分：元数据给了 `max_input_tokens` / `inputTokenLimit` 就直接用
  （**不再重复减输出**）；只给了总窗口才 `窗口 − 本次输出预留`，只减一次。
* 每个 stage / 每个 fallback 各用各的 limit：主模型 1M 不会被未知备用拖小；主模型失败后
  进入较小窗口的备用时，载荷会**重新裁剪**适配该候选，而不是整条跳过。
* `decision` / `moderation` 等短上下文链路不变大。

## 缓存与取数（`bot/services/model_limits.py`）

* 只查**已配置且已带认证**的 endpoint：必须有 `api_base` + key，否则跳过（不探测第三方）。
* 键 `(provider, api_base, model)`；**成功 TTL 6 小时**，**失败/未命中负缓存 5 分钟**。
* 单次查询 4 秒超时、最多 3 个并发；启动最多等 6 秒，等不到就 `shield` 到后台继续。
* 路由/模型变更时（`bot/__main__.py` 的配置应用回调）`force=True` 重取一次，不阻塞配置应用。
* 日志只打印 provider / host（`redact_base` 去掉内嵌凭据）/ 原样 model id / 来源 / 数值；
  **绝不打印 api_key**。主链路读缓存是纯同步的，绝不查网络。

## 最终闸门（`bot/services/llm.py` + `bot/services/payload_fit.py`）

* 计量一致：保守上界改用与装配链路同一个 `estimate_text_tokens`（可加：工具定义 + 每条消息），
  且**永远标 `exact=False`**；只有真实分词器（离线程 + 1 秒有界超时）成功才算 exact。
* 超限不再"整条 skip"：按 **最老历史 → 检索留档 → 记忆召回** 裁到能发；核心系统块、本轮
  消息、工具 `assistant(tool_calls)` + `tool` 配对永不破坏（超长工具结果只截断正文）。
  只有固定层本身装不下时才诚实失败（不发请求）。
* 层标记（`_ctx_layer`）只在进程内使用；发请求前 `strip_internal_message_keys` 把所有
  下划线开头的内部键清掉（严格的 OpenAI 兼容网关会拒绝未知字段），且不改写原载荷
  （每个 fallback 都要拿原始载荷重新裁）。
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
* `home_work2api` 宣告的 1,000,000 是否真的能吞下接近百万 token 的请求（本修复只保证
  **按它自报的上限放行**，不保证网关侧真的做完了百万 token 验收）。
* 长历史（2000 条）下的真实首字节延迟与事件循环占用（保守估算在事件循环上做正则扫描，
  估算本身未 off-thread；按 1M 窗口时单次约数十毫秒量级，实测需复核）。
