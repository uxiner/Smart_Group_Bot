# 配置指南

这份文档说明这个 bot 的**三层配置**：哪一层改完什么时候生效、每个字段的默认值与
上下界、谁能编辑、真实的读侧消费者在哪、以及从旧版本迁移过来要注意什么。

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
直接 422。所有 secret 字段单独走 `secret_changes`（`keep` / `clear` / `replace`），
响应只回 `configured_secrets` 的**键名**，绝不回值。

**只有最高管理员能读写全局配置。** 群管理员拿不到 `/api/v1/settings`（后端
`require_super_admin`），前端也只渲染"群组设置"页——所以群管理员既看不到也改不了
全局经济、资源与交接字段。他们能改的仍然只有该群的 `group_settings`。

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

人设提示词本身已经有运行时编辑（Mini App 的 Prompts 页），本页**不会**再造第二份
人格。fresh install 不绑定任何具体人设。

### `resources` — 高级资源与进程级预算（最高管理员）

默认全部等于今天真实生效的值，不配就逐字等于改造前。

**需要重启**（`reload_kind: "restart"`，共 20 项，进程级闸门与服务构造）：

`llm_request_capacity` 8 · `llm_request_noncritical_capacity` 7 ·
`llm_request_normal_capacity` 4 · `llm_request_background_capacity` 2 ·
`llm_tokenizer_concurrency` 2 · `telegram_total_capacity` 64 ·
`telegram_noncritical_capacity` 60 · `telegram_normal_capacity` 44 ·
`pending_reply_execution_capacity` 4 · `tts_synthesis_concurrency` 3 ·
`tts_transcode_concurrency` 2 · `tts_private_concurrency` 2 ·
`av_query_concurrency` 3
（外加历史遗留的 `bot.parse_mode`）

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
`telegram_send_chat_parallel`）：webhook 服务的 worker 数、队列容量、连接池上限都在
`VerifyWebServer` / aiohttp session 构造时固化，所以这一段是 **restart**；durable
inbox 的维护旋钮（恢复批量、重试退避上限、清理间隔与批量）每轮现取，**热生效**。

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
   旧版本把品牌名**硬编码**在源码里（人设提示词、语音条标题、检索称呼前缀都指向
   同一个具体别名）。本轮把它们抽成了 `display` 段，公开树的默认值是**中性的**
   （`bot_display_name = "助手"`、`private_voice_title = "语音回复"`、
   `search_query_prefixes = ["诶","嗯哼","呀","欸"]`）。升级到这一版之后：

   * **人设提示词本身没有被改动**——`prompts.persona` 仍是原来那份，产品的说话风格、
     称呼与互动方式保持原样；
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
