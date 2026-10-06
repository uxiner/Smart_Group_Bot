# 常量分类：哪些抽成了配置、哪些刻意保持固定

这份文件回答一个具体问题：**全仓的模块级常量被逐条判定过了吗？哪些是运营参数（已抽
成可配），哪些是安全 / 协议 / 算法不变量（保持固定），哪些是"抽不出来也不该抽"。**

结论先说清楚，以免被误读成"全盘无遗漏"：

* **本轮实现并接上真实消费者的**：见
  [`configuration-fields.json`](./configuration-fields.json) 的 153 个字段（20 个 restart、133 个 hot；每一项都
  带 `read_consumers`，且 `tests/test_configurable_policy_catalog.py` 会验证登记的
  读侧符号真的存在于那个文件里）。
* **逐条判定后判定为"保持固定"的**：见下面各表，附判定理由。
* **本轮明确没有逐条判定、只做了抽样或依赖上游代码正确性的**：见最后一节
  「本轮无法验证的点」。

判定口径
--------

| 类别 | 判据 |
| --- | --- |
| `运营` | 部署者会按业务需要调整的数值/文案，且调整不会削弱任何安全或正确性保证。已抽成可配。 |
| `安全` | 身份、授权、SSRF、签名、验签、重放、nonce、租约、终态、退款幂等标识、密钥处理。**不做开关。** |
| `协议` | Telegram / HTTP / 回调载荷的硬上限与解析契约（消息长度、callback_data、start payload 前缀、CSP、请求体上限、UTF-16 偏移）。**不做开关。** |
| `算法` | 打分公式、排序、随机、融合系数、去重、裁剪优先级。改了就不是"同一套东西"了。 |
| `文案` | 纯用户可见文字（不含部署身份）。可配的已在 `display` 段抽走；其余是协议性固定文案（错误说明、命令用法）。 |
| `已贯通` | 已经有活的运行时配置读侧，本轮只确认没有硬写死覆盖。 |

## 一、已抽成配置（`hot`，改完下一次动作生效）

| 段 | 代表字段 | 读侧 |
| --- | --- | --- |
| `private_chat` | 四档配额、输入/回复长度、识图预算、内存兜底轮数、准入缓存 TTL、检索保险丝、语音段数 | `bot/services/private_chat.py`、`bot/handlers/private_chat.py`、`bot/services/dm_search.py`、`bot/services/private_tts.py` |
| `economy` | 签到封顶 / 榜位 / 违规窗口、质询免除价、头衔与置顶价与时长、抽奖价与每日次数、奖池、头衔长度上限、到期扫描四项 | `bot/services/checkin.py`、`bot/services/point_shop.py` |
| `activity` | 有效发言长度、每日封顶、参与门槛、每周奖励向量 | `bot/services/activity.py` |
| `checkin_reminder` | 时段、问候语、自动删除、名单上限、空占位宽限 | `bot/services/checkin_reminder.py`、`bot/tools/checkin_reminder.py` |
| `display` | 显示名、语音条标题、两个按钮文案、检索唤醒前缀、四条私聊提示 | `bot/services/dm_search.py`、`bot/services/private_tts.py`、`bot/services/checkin.py`、`bot/services/checkin_reminder.py`、`bot/handlers/private_chat.py` |
| `resources`（hot 部分） | LLM 阶段预算表、Telegram 各级准入超时、群待回复预算、管理员告警四项、送审整形四项、TTS 段数与超时、AV 查询预算与名字缓存、判定历史预算、长期记忆八项、检索留档两项、向量归档七项 | 见机器目录的 `read_consumers` |
| `moderation`（部署绑定） | 证据频道 id、交接对象 | `bot/handlers/group.py` |

## 二、需要重启的进程级资源（`resources` 的 `restart` 段）

模块级 `asyncio.Semaphore` / `ReservedCapacityGate` / `PriorityAiohttpSession` /
tokenizer 线程槽位在 import 或建 Bot 时固化。热替换会漏掉正在执行的请求、泄漏
slot，或让新旧两个闸门同时存在，所以它们只在 `bot/__main__._initialize_runtime_services()`
里 `apply_startup_resources()` 装配一次。API 与 Mini App 会通过
`restart_required_paths` / `restart_pending` 明确提示。

LLM 准入闸门的保留容量不变式（`background ≤ normal − 2`、`normal ≤ noncritical <
total`）在 schema 里强校验，`tests/test_startup_resources.py` 另有一条用例**真的用
非默认容量跑一次准入**，并验证有活动请求时重配被拒绝、slot 不泄漏。

## 三、逐条判定为「保持固定」

### 3.1 安全 / 身份 / 授权

| 常量 | 位置 | 为什么固定 |
| --- | --- | --- |
| SSRF URL 校验与 host allowlist、DNS 钉扎 | `bot/services/skills/platform_common.py` | 放行 host / scheme / 凭据等于开放 SSRF。 |
| 最高管理员判定 | `bot/services/authz.py`、`bot/web/auth.py` | 身份不是运营参数。 |
| 入群验证的 nonce 宽限、准备中 / 终态租约、解封恢复宽限 | `bot/services/join_verification.py` | 终态与租约是并发正确性的前提。 |
| 管理身份复验缓存 TTL | `bot/web/settings_api.py` | 缩短会放大越权窗口；它已经在 300s 且不可配是有意的。 |
| 私聊准入缓存 TTL 上界 | `bot/services/private_chat.py` | 可配，但**上界被钉在 60 秒**（今天的值），只能收紧。 |
| 退款 / 消费幂等 `ref` 键与唯一索引 | `bot/services/point_shop.py`、`bot/db/models.py` | 财务正确性；改了就可能重复发/重复扣。 |
| 资源健康看门狗 fatal 阈值与进程退出 | `bot/services/resource_health.py` | 放松等于关掉看门狗。 |
| 上下文预算门禁（预留 < 总预算、0 不能关） | `bot/utils/budget.py` | 这是安全不变量，不是容量旋钮。 |

### 3.2 协议硬上限

| 常量 | 位置 |
| --- | --- |
| Telegram 单条消息 4096 / 流式安全线 3800 / 富文本 32000 | `bot/utils/telegram.py` |
| 回调前缀（`mact` / `mrev`）、`checkin:v1`、`shop_` 深链前缀、`verify` 载荷 | `bot/handlers/group.py`、`bot/services/checkin.py`、`bot/services/checkin_reminder.py`、`bot/services/join_verification.py` |
| webhook / Mini App 请求体上限、CSP、安全响应头 | `bot/services/verify_web.py`、`bot/web/app.py` |
| 实体偏移的 UTF-16 规则 | `bot/handlers/group.py` |

其中与运营相关的两个派生值**已可配但协议上界不可放开**：`private_chat.reply_max_chars`
（`le=4096`）与 `economy.tag_max_length`（`le=16`）。

### 3.3 算法不变量

得分公式（`messages + 2×active_days + replies_received`）、RRF 融合常数、裁剪优先级、
去重规则、NSFW 判定标记、抽奖的"一次 `randbelow` + 前缀和"、速率限制窗口的滚动口径。

### 3.4 头衔防冒充词表

`TAG_BLOCKLIST`（管理员 / 群主 / admin / 官方 / 客服 / 机器人 / bot）与
`TAG_LONG_MARKERS` **保持源码常量，不提供配置**。做成可配等于允许部署者删掉防冒充
词；`tag_max_length` 可配但 `le=16`。

## 四、抽样判定或本轮未动的模块

以下模块本轮**没有**逐条判定，只做了抽样或依赖其既有测试：

| 模块 | 本轮做了什么 | 没做什么 |
| --- | --- | --- |
| `bot/handlers/admin.py` | — | 6 项运营常量（列表分页、封禁范围 TTL、批量并发等）未抽离 |
| `bot/handlers/group.py` 其余部分 | 抽走管理员告警四项、待回复预算、交接 mention、判定历史预算 | 视觉图像字节上限、证据标题截断、活动防抖等未抽离 |
| `bot/handlers/commands.py` / `membership.py` | 抽走质询免除价的读侧 | 分页大小、冷却、缓存 TTL 等未抽离 |
| `bot/utils/telegram.py` | — | 11 项运营常量（按会话并发、typing 超时、流式增量上限等）未抽离 |
| `bot/web/settings_api.py` | 权限模型已确认（`@authenticated` = 最高管理员 / `@any_admin` = 群管理员） | 成员身份查询的缓存与限流（约 15 项）未抽离 |
| `bot/services/update_delivery.py` | 抽走轮询三项超时 + webhook 连接池上限（`resources.polling_*` / `webhook_max_concurrent_updates`） | 删除 webhook 的退避表、看门狗间隔、探测重试等运维常量未抽离 |
| `bot/services/verify_web.py` | 抽走 webhook 三车道并发与队列容量、四级端到端预算、HTTP 响应超时、durable inbox 租约/恢复批量/重试退避/清理间隔与批量 | 限流窗口与令牌桶（`_VERIFICATION_USER_RATE_LIMIT` 等）、durable inbox 保留期与 DLQ 保留期、dedup 上限等未抽离——其中保留期属数据生命周期，限流属安全策略 |
| `bot/services/memory.py` / `model_limits.py` / `context_gate.py` | 确认既有字段已贯通、确认无硬写死覆盖 | 压缩/归档相关的约 35 项运营常量未抽离 |
| `bot/services/join_verification.py` | 确认既有策略已可配；确认 nonce / 租约 / 终态是常量 | 7 项 Telegram 调用容量与重试常量未抽离 |
| `bot/services/scheduled_messages.py` / `telegram_cleanup.py` | — | 构造器默认参数约 17 项未抽离 |

### 明确**不打算**抽的

* **外部 cron 的时间表**：`checkin_reminder.slots` 只约束 CLI 接受哪些 `--slot`；
  真正几点发由部署侧 crontab 决定。把 cron 表达式做成配置既不安全（表达式的解析与
  生效时机没法与进程内配置对齐）也不必要——它本来就属于部署。文档已写明这层依赖。
* **进程内编排顺序**：装配点固定在 `_initialize_runtime_services` 里"预取之前、
  建 Bot 之前"这一个位置。做成可配会让"先收发后装配"成为可能，那正是 slot 泄漏的
  温床。

## 五、本轮无法验证的点

诚实地列出来，不要当成已通过：

1. **没有在真实 Telegram 上跑过。** 全部验证是本机单元/集成测试与静态检查。没有
   连过生产、没有读过生产 `.env`、没有连过生产数据库。
2. **webhook 侧只抽了"资源预算"，没抽限流与生命周期。** `_VERIFICATION_USER_RATE_LIMIT` /
   `_VERIFICATION_IP_RATE_LIMIT` 属安全策略（公开验证页的滥用防护），durable inbox 的
   保留期/DLQ 保留期属数据生命周期，两者都刻意留在源码常量里。删除 webhook 的退避表
   与探测重试也仍是硬编码——它们是"失败时怎么办"，不是容量旋钮。
3. **跨模块的端到端行为**（例如"改了私聊配额之后，一整轮 DM 的扣费-退款-回执在真实
   Telegram 上的表现"）只覆盖到函数级，未做端到端。
4. **UI 的移动端与键盘可达性**只做了 CSS 约束（44px 触达、`:focus-visible`、窄屏两
   列不横溢）与 `node --check` 语法检查，真实设备上的视觉回归未做。
5. **图片资源**（`docs/ui-night-crystal/*.png`）只做了文件清单与尺寸核对，没有逐像素
   读图确认里面没有部署信息。
6. **上游同步**：`upstream/main` 未合并、未逐行 diff（用户明确要求不拉最新上游）。
   本分支与 `upstream/main` 之间的差异面**未**做全量审计。

---

## 补齐记录：telegram_task_lifecycle / verify_web / join_verification 的保留理由

| 保留项 | 位置 | 理由 |
| --- | --- | --- |
| shutdown / orphan 安全防线（取消宽限、worker 停止超时、排空预算、孤儿上报年龄） | `bot/services/telegram_task_lifecycle.py`、`bot/services/update_delivery.py` 的 `_WEBHOOK_*_STOP_TIMEOUT` / `_POLLING_*_DRAIN` | 这些是**进程正确性**的边界：调大＝关机时排不干净、取消后任务泄漏；调小＝正常请求被误杀。属于安全不变量，不做成运营旋钮。 |
| durable inbox 的去重窗口（`_WEBHOOK_DEDUP_TTL_SECONDS` 60 分钟）与 dedup 上限 | `bot/services/verify_web.py` | 去重窗口决定"同一 update 的重投算不算同一次"。**可配的保留期被强制 ≥ 该窗口**（schema 校验），所以调小保留期不会让重投被当成新行执行。dedup 容量本身是内存上界，保持固定。 |
| DLQ 与未完成 inbox 的删除规则 | `bot/services/verify_web.py` 清理循环 | 只有 `completed_at` 超过保留期的**已完成**行、以及 `dead_lettered_at` 超过死信保留期的死信行才会被删；**未完成（有 lease 或未完成）永不被静默删除**。保留期可配，但这条删除规则本身不随配置变化。 |
| 入群验证的终态判定、nonce 宽限、准备中/终态租约、解封恢复宽限 | `bot/services/join_verification.py` | 并发正确性的前提：终态不可回退、租约防重复执行。已在 `KeptFixedFamiliesTests` 里逐条钉住没有被做成开关。 |
| `context_reserve_tokens` vs `group_history_reserve_tokens` | `bot/config.py`、`bot/services/runtime_config.py` | **不是缺口**：`context_reserve_tokens` 是权威值，`group_history_reserve_tokens` 是兼容字段，覆盖顺序按 `model_fields_set` 判断而不是猜默认值。两者已有 runtime/schema/apply/migration 完整链路，本轮不动，只在此澄清。 |
| search maintenance 与 archive provider 的注册点 | `bot/__main__.py` | 已在上一轮抽走（`resources.search_prune_interval_seconds`、`resources.archive_*`），本轮不重复造同类字段。 |
