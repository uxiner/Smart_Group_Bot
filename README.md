<h1 align="center">🤖 Smart Group Bot · 独立维护 fork</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/aiogram-3.x-0066CC.svg" alt="aiogram">
</p>

> **本仓库是 [uxiner/Smart_Group_Bot](https://github.com/uxiner/Smart_Group_Bot)**，
> 即上游 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)
> 的**独立维护 fork**：MIT 许可、公开可自行部署。你可以照文档部署、改代码、再分发，
> 也可以向上游提 issue / PR。上游原版说明（完整功能、架构、部署细节）见
> [`docs/README.upstream.md`](docs/README.upstream.md)。

上游是一个**大模型驱动的群聊管理机器人**：把「聊天陪伴」和「群组治理」放在同一条消息管线里——既能自然参与
群聊、调用技能查资料，也能做内容审核、入群验证、爆破防护、民主投票封禁，全部配置在 Telegram Mini App 里完成。

本 fork 在它之上加了一批自研功能，并修了一批部署面与配置面的坑。下面每条都标了**默认值**还是
**写死的常量**：标「默认」的是运营参数，可以在 Mini App 的「运营参数」页改（怎么配见
[docs/configuration.md](docs/configuration.md)，字段与上下界见
[docs/configuration-reference.md](docs/configuration-reference.md)）；**协议、安全与算法层的常量不提供开关**，例如 Telegram
单条消息 4096 的上限、`/av` 私聊每小时 10 次限流、头衔长度的 16 字上界、活跃度得分公式里的
系数——这些是刻意固定的不变量，改动它们需要改代码而不是改配置。

---

## 与上游原版的区别（本 fork 的增量）

### 1️⃣ 成员积分与自助命令（全新）

上游没有「成员激励」这一层。本 fork 加了一套积分经济。**下面所有数字都是 schema 默认值**，
可以在 Mini App 的「运营参数」页改，怎么配见
[docs/configuration.md](docs/configuration.md)，字段与上下界见
[docs/configuration-reference.md](docs/configuration-reference.md)。

| 能力 | 说明（默认值） |
|---|---|
| 每日签到 | `/checkin` 连签第 N 天得 N 分，默认 10 分封顶 |
| 查看自己的 | `/points` 可用积分 + 连签天数；`/me` 积分 / 签到 / 违规 / 封禁 |
| 积分榜 | `/rank` 本群 Top10；`/rank week` 本周（周一 0 点起）所得积分 |
| 群内检索 | `/find <关键词>` 在保留期内搜历史消息，默认 7 天 |
| 漏判举报 | 回复目标消息后 `/report [补充]`，让审核模型复核并转给管理员 |
| 签到提醒 | 默认 9 / 12 / 15 / 18 点各一条，10 分钟后自动删除 |

<details>
<summary>实现细节（积分怎么记账、提醒按钮怎么防作弊）</summary>

- **断签只回落到 1 分，历史积分不清零**：连签天数决定当天得分（连签第 N 天得 N 分，
  上限可配），断签后从 1 分重新开始，此前赚到的分一分不少。
- **三本流水互不污染**：`member_checkins`（签到所得）/ `member_point_awards`（奖励所得）/ `member_point_spends`（消费）；可用积分只有一个定义 `available_from_ledgers()` = 签到 + 奖励 − 消费，所有页面都走它。
- **奖励绝不伪装成签到行**：连续签到天数是从签到日期集合倒推的，伪造一行就会把用户的连击算坏。
- 所有加减分由 `ref` 唯一索引保证幂等，重试不会重复记账。
- **签到提醒**（`bot/tools/checkin_reminder.py`）：文案按时段不同；按钮回调数据是固定常量、**不带用户 ID**，点击者身份只由 `callback.from_user` 决定，**没法替别人签到**；成功/已签过都用轻提示回执，不在群里另发消息；「今日已签到 N 人 + 名单」随点击刷新（最多 20 个昵称，昵称一律 HTML 转义）。
- 「🛒 积分商店」是 **URL 深链**（`t.me/<bot>?start=shop_<群号>`，`<bot>` 运行时用 `get_me()` 取，取不到就只留签到按钮）：菜单发到**私聊**，不在群里刷屏；私聊菜单按该用户在那个群的可用积分渲染、**不扣分**；`shop_` 与入群验证的 `verify…` 前缀互不干扰。
- 提醒**同一时段只发一条**（`(群, 时段)` 唯一键先占位再发送，发送失败释放占位以便重试）；命令与按钮共用同一个回执渲染函数，两边文案不会不一致。
- `checkin_reminder.slots` 的默认值是 `[9, 12, 15, 18]`，但它**只约束 CLI 接受哪些
  `--slot`**——工具本身不判断「现在几点」，**真正几点发由部署侧 crontab 决定**。改了 slots
  必须同步改 crontab，否则新时段不会真的被触发（可复制的样例见
  `bot/tools/checkin_reminder.py` 的模块 docstring）。

</details>

### 2️⃣ 周活跃激励（全新）

让「在群里好好聊天」也有回报，而不只是签到。**以下阈值与奖励向量都是默认值**
（`activity.*`，可在运营参数页改）：

- 每条合格发言按 `(群, 用户, 本地自然日)` 日累计，每天 `max_daily_messages` 条封顶（默认 20，写入端 SQL 封顶，刷屏无收益）
- 每周结算最近一个完整自然周：**得分 = 发言数 + 2 × 活跃天数 + 被回复次数**；门槛是活跃 ≥3 天且发言 ≥10 条
- 名次奖励由一个向量给定，默认 `[25, 12, 12, 4, 4, 4, 4, 4, 4, 4]`（10 个名次、每周共 77 分），榜单长度与总额都从这个向量派生，不另存一份
- 结果随周报发进群里；采集与结算都在后台任务里跑（独立 session），**不阻塞群消息的回复路径**；`bot/tools/activity_award.py` 支持手动补跑与 `--dry-run`

### 3️⃣ 积分商店（全新）

价格与时长都是 `economy.*` 的**默认值**，可改：

| 商品 | 默认价格 | 默认时长 |
|---|---|---|
| 自定义头衔（Telegram 原生 member tag） | 30 分 | 7 天 |
| 同上 · 长租 | 80 分 | 30 天 |
| 置顶自己的求助 | 20 分 | 6 小时 |
| 抽奖一次 | 5 分 | 即时开奖 |

- **头衔** `/tag 文字`（加 `30天` 买长租）：1–16 字（`tag_max_length`，上界就是 16）、不能带表情、不能与他人重名、不能出现「管理员/官方/客服」这类易冒充的词；**只卖给普通成员**（管理员头衔归群设置管，接口会返回成功但并不生效）；**续费往后顺延**，到期自动清除
- **置顶**：回复自己的一条消息后发 `/top`，静默置顶 6 小时（不弹全群通知），到时自动取消；每人同时最多 1 条
- **抽奖** `/draw`：默认 5 分一次、每天最多 10 次；默认权重换算成概率是 39% 谢谢参与 / 20% 3 分 / 16% 5 分 / 10% 8 分 / 10% 12 分 / 4% 40 分 / 1% 100 分，**期望值正好 6.00 分/次**（玩家长期每次净赚 1 分，属净发放；随机数用 `secrets`）。权重是 `economy.lottery_prizes` 的相对权重，总权重是对表求和的**派生值**，所以改权重不会和总额打架
- **失败自动退款**：扣分先入消费流水，Telegram 侧失败就写一笔退款流水（`shop-refund:<原ref>`，幂等），**不会出现「钱扣了东西没拿到」**；退款本身失败会记日志报警
- 到期清理由进程内的常驻服务默认每 5 分钟跑一轮（`economy.expiry_check_seconds`）；`bot/tools/shop_expire.py`（幂等、可 `--dry-run`）用于手工补跑。权益状态记在 `member_entitlements`（`kind='tag'|'pin'`）

### 4️⃣ 审核增强（在上游原版之上改）

| 能力 | 一句话 |
|---|---|
| 上下文感知 | 审核时带上「这句话是在什么对话里说的」，少把正常回复误判成违规 |
| 置信度治理 | 边缘判定不直接处置，改走**质询卡片**；成员可花 2 积分免除 |
| 申诉与复核 | 被处置者可在卡片上申诉，管理员一键复核 |
| 质量报表 | `/modstats` 命中构成与**误伤率**；`/health` 看今日运营状态 |
| 名单管理 | `/exemptlist` 豁免与回复静默名单（翻页、移除）、`/unaiexempt` |
| 规则扫描范围 | 每条规则可选扫「本人正文 / 被引用正文 / 机器人图片描述」 |
| 引用广告连坐 | 引用广告命中 ban 时，被引用的原作者一并处置（有豁免与 7 天限制） |
| 管理员也受审核 | 除最高管理员外，管理员违规照常删消息 + @警示（不质询、不禁言） |
| 审核证据 → 频道 | 每条命中发证据卡到「审核日志」频道，带「人工放行 / 放行收回」按钮 |

<details>
<summary>各条完整口径（细则）</summary>

- **上下文感知**：`moderation_context.py` 把「这句话是在什么对话里说的」连同前文一起交给审核模型，减少误判。
- **置信度治理**：边缘判定不再直接处置，改走**质询卡片**；成员可花 2 积分免除质询。
- **质量报表**：`/modstats`（命中构成、边缘判定、**误伤率**）；`/health`（今日命中、待完成质询、归档量、当前模型通道）。
- **名单管理**：`/exemptlist`（豁免 / 回复静默名单，可翻页、一键移除）、`/unaiexempt`（取消某用户的 AI 审核豁免）。
- **规则扫描范围**：每条正则/关键词规则可选 `message`（默认，只扫成员自己写的正文）/ `message+quote`（并入被引用正文）/ `message+vision`（并入机器人图片描述）/ 组合；机器人图片描述默认**不参与**正则，避免「描述购物界面 → 秒杀/优惠券命中 → 误删」（Mini App 可改）。
- **引用广告连坐**：引用/转发命中 ban 规则且高置信度时，**被引用消息的原作者**一并处置（删消息 + 记违规 + 质询）；限真实用户、管理员/群主/豁免跳过、同一条只处置一次、超过 `moderation.quoted_author_max_age_seconds`（默认 7 天）不追溯、警示式引用双方都不处理；`moderation.punish_quoted_author_enabled` **默认关闭（opt-in）**。
- **管理员也受审核**：除**最高管理员**（完全豁免）外的管理员/群主不再整段跳过——照常判定，命中后只**删消息 + 群内 @警示 + 记违规（delete）**，不质询/不封禁/不禁言/不累计警告；NSFW 图同样删图 + @警告但不质询；手动豁免名单仍完全跳过；`moderation.admin_moderation_enabled` **默认关闭（opt-in）**。
- **管理员违规证据私聊**：管理员违规时把完整证据（对象/身份/时间/规则/置信度/理由/送审原文/已执行/回链，带图附图片）私聊最高管理员；best-effort 不影响群内处置，同一人 10 分钟内 ≥5 次合并成一条；`moderation.admin_alert_super_admin_enabled` 默认开启；**只有频道投递真的开着**（`log_channel_enabled` 且 `log_channel_id` 填了频道）这条私聊路径才被取代——默认频道未配置，所以默认走的还是私聊。
- **审核证据 → 频道 + 人工放行**：群里**所有**被处置的命中都往「审核日志」频道发一条完整证据卡，**每条单独发**，每张卡带「人工放行 / 放行收回」按钮，只有最高管理员可点。**放行**：`review_state=released` + 立即解除该成员限制（作废质询超时封禁，不添加永久豁免）+ 频道新发 `🟢 人工放行 · 待调整规则` 交接消息（@your_bot mention，由 `moderation.review_handover_mention` 配置，留空则不 @ 任何人）。**收回**：按该 case 的**原始处置**重新施加限制（challenge → 重新禁言 + 重新质询；ban → 重新封禁；delete/warn → 无限制可恢复），结果写进频道状态行与 `🔴 放行收回 · 无需调整` 交接消息；最高管理员与手动豁免名单跳过。开关 `moderation.log_channel_enabled`（默认开）/ `log_channel_id`（默认 `0`，即未配置——所以**默认状态下这一整条其实没启用**，见下一节）；关掉回到私聊老路径；**不新增 LLM 调用**。

#### ⚠️ 审核日志频道：默认未配置，以及怎么改

**默认的审核日志频道 id 是 `0`（= 未配置），`log_channel_enabled` 默认开着，但配成 `0` 时频道路由整体关闭。**
也就是说：**不改任何配置的全新部署不会向任何频道投递命中审核的消息证据（群 id、用户 id、昵称、送审原文全文、判定理由…），而是回退到「私聊最高管理员」的老路径。** 要启用频道投递，先把 `log_channel_id` 填成**你自己的**频道 id。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `moderation.log_channel_enabled` | `true` | 关掉后回到私聊最高管理员 |
| `moderation.log_channel_id` | `0` | 证据频道 id，`0` = 未配置 |
| `moderation.review_handover_mention` | `""` | 规则调整交接 @ 谁，空 = 不 @ |

怎么改（覆盖入口，从推荐到不推荐）：

1. **Mini App「审核验证」页（推荐）** — 上表这三个字段在界面上都有控件：`证据投递到频道`
   开关、`证据频道 ID` 输入框、`交接对象` 输入框在同一段里。保存即热生效，不需要重启。
2. **API `PUT /api/v1/settings`** — 流程见下面三步。鉴权走 `Authorization` 头，方案名是
   `tma`，后面的凭据是 Telegram 打开 Mini App 时下发的 WebApp `initData` 字符串；不是
   Bearer token，本仓库也没有别的鉴权方案。该接口**仅最高管理员**可调用。
3. **`config.toml` 的 `[moderation]` 段** — **仅**在数据库里还没有 `runtime_config` 行时
   做一次性导入；一旦导入完成该文件即被忽略，之后只认数据库里的值。
4. **环境变量** — **无效**。`ModerationConfig` 不是 `BaseSettings`，`Settings` 也没有
   `env_nested_delimiter`，写 `MODERATION__LOG_CHANNEL_ID` 不会被读取。
5. **直接改 `runtime_config` 表** — **不要这么做**。`runtime_config.payload` 存的是
   非密钥 JSON，手改会绕过 schema 校验、`revision` 乐观锁与密钥操作接口（密钥另存
   `runtime_config_secrets` 并加密），Mini App 下次保存就会把你的改动覆盖掉。

API 流程（三步，无可复制示例——凭据与整份配置文档都不适合写进 README）：

1. `GET /api/v1/settings`，从响应里取 `revision` 与 `config`——两者**与 `ok` 平级、直接在
   响应顶层**（响应里没有 `data` 这一层包裹）。
2. 在**完整的** `config` 上改 `moderation.log_channel_id`，其余字段原样保留。
3. `PUT /api/v1/settings`，请求体只有三个顶层字段：`revision`（第 1 步取到的值）、
   `config`（改完的整份文档）、`secret_changes`（本例不需要密钥变更，可传空对象）。

几条语义，改之前值得知道：

- `PUT` 是**整份替换**，不是嵌套 patch：漏掉的字段会**回到 schema 默认值**，不会保留旧值，
  所以必须先 `GET` 再把整份文档回传。
- `config` 里密钥字段恒为空串，而**空串表示「不变」而不是「清空」**；要清空只能用
  `secret_changes`（`clear` / `replace` / `keep`），响应只回 `configured_secrets`
  （**键名**），绝不回密钥值。
- `revision` 对不上（别人先保存过）返回 **409 `revision_conflict`**；未知键或取值非法返回
  **400**（`extra="forbid"`）。保存成功后若改到了需要重启的字段，响应里的
  `restart_pending` 会列出字段名（只给名字，不给值）。

</details>

### 5️⃣ 运营看板与周报（全新）

- `/cost [天数]`：token 用量、**缓存命中率**、思考 token、超时 / 空响应 / 解析失败，并按阶段（审核 / 决策 / 技能 / 视觉）拆分
- `bot/services/llm_metrics.py`：进程内用量累加器，60 秒惰性落盘；主回复路径**不写库、不抛异常**
- `bot/tools/weekly_report.py`：每周把「群健康 + 活跃榜」发进各授权群；**成本摘要只私发给最高管理员**（不在群里晒运营花销）

### 6️⃣ 工程质量与加固（在上游原版之上）

- **管理命令自动清理**：`ManagementCommandCleanupMiddleware` 在 5 秒后删掉群里的 `/ban`、`/mute` 等管理命令行（只碰管理命令，不动成员命令），走持久队列、重启不漏删
- **路由完整性回归**：[`tests/test_router_route_integrity.py`](tests/test_router_route_integrity.py) 防止「helper 插在装饰器与处理器之间」导致**整个群机器人静默失效**（真实事故，已固化为回归）
- **测试**：[`tests/`](tests/) 里既有 `unittest.TestCase` 用例，也有 pytest 风格的用例
  （例如 [`tests/test_tools_bootstrap_settings.py`](tests/test_tools_bootstrap_settings.py)
  用了 fixture 与 parametrize），所以**用 pytest 跑全套**，别用 `unittest discover`
  （它不会收集那两个纯 pytest 模块，也不会加载
  [`tests/conftest.py`](tests/conftest.py) 里的环境隔离）：

  ```bash
  pip install pytest          # 测试依赖；运行时依赖见 requirements.lock
  python -m pytest tests
  ```

  这里**不写「多少条用例全绿」**——测试数每次提交都在变，写死的数字只会变成误导；要看就自己跑一次。
- 事务边界与幂等测试、`prompt/`（决策 / 审核 / 人格 / 闲聊）按实际运营调过、`docker-compose.yml` 与 `requirements.lock` 有本地调整

<details>
<summary>媒体内容策略与 /av 链路</summary>

- **群内 NSFW 一条底线**：公开露骨图片 → 删图 + 群内 @警告（2 分钟自删）+ 质询；图片**复用审核那次视觉调用，零额外成本**（视频会额外看一次封面缩略图，那一次是真花 token 的）。带 `/av` 的图不经此流程；违规记账失败也**不会挡住删图**，图一定先离开群。开关 `moderation.nsfw_image_guard_enabled`，**默认关闭（opt-in）**。
- **图片一律不进群**：`/av` 的封面只发发起者私聊，群内只留文字（仍是「种子 N 条」+ 按钮，由逐字回归测试守住）。
- **群里「`/av` + 图片」**：**先删图再识别**（删除失败也继续识别），避免违规图停留。
- **私聊 `/av`**：支持发图识图反查番号（视觉读编号/演员名 → 自动查详情）；每人每小时 10 次限流是**代码常量，不提供配置开关**；只有演员名时退化为按演员检索。
- **私聊 `/av` 附发样例图**：封面之外再补 3~4 张（默认 4、上限 5、设 0 关闭），**只在私聊**。
- **私聊 `/av` 下载地址 + 制作信息**：详情里内联前 N 条磁力链（默认 3、硬上限 5、`av.inline_seed_count=0` 关闭），每条两行「大小 · 日期 · 标题」+ `<code>磁力</code>`；整条放不下时按 N→N−1→…→1 减少条数，**磁力链本身不截断**。片商 / 发行商 / 导演 / 系列 / 演员 / 时长 / 类型单独成「制作信息」块。
- **（可选，默认关）AI 题材概述**：`av.ai_synopsis_enabled=true` 时用**已抓到的字段**生成 1~3 句题材与看点，正文标注「AI 概述，非官方剧情」；独立 stage `synopsis` 便于用量拆分，失败 / 超时只跳过这一块。**不做 ed2k、不做价格、不抓官方剧情**（实测拿不到）。

</details>

---

## 命令速查（本 fork 新增；上游原有命令不变）

| 命令 | 作用 | 权限 |
|---|---|---|
| `/checkin` | 每日签到得积分（默认 10 分封顶） | 所有人 |
| `/points` | 可用积分 + 连续签到天数 | 所有人 |
| `/me` | 积分 + 签到 + 违规 + 封禁状态 | 所有人 |
| `/rank`、`/rank week` | 本群积分榜 Top10（总榜 / 本周） | 所有人 |
| `/find <关键词>` | 搜索群内保留期消息（默认 7 天） | 所有人 |
| `/report` | 回复漏判消息后举报，触发模型复核 | 所有人 |
| `/shop` | 积分商店：价目表与每件商品怎么用 | 所有人 |
| `/tag <文字>`、`/tag <文字> 30天` | 积分头衔（默认 30 分 7 天 / 80 分 30 天） | 所有人 |
| `/top` | 回复自己的一条消息后发送（默认 20 分置顶 6 小时） | 所有人 |
| `/draw` | 抽奖（默认 5 分一次，每天最多 10 次） | 所有人 |
| `/health` | 本群今日审核命中、待质询、归档量、模型通道 | 管理员 |
| `/modstats [天数]` | 审核质量报表（命中构成、误伤率） | 管理员 |
| `/cost [天数]` | 成本与健康看板（token / 缓存 / 异常） | 管理员 |
| `/exemptlist` | 审核豁免与静默名单（翻页 / 移除） | 管理员 |
| `/unaiexempt` | 取消某用户的 AI 审核豁免 | 管理员 |

新增数据表：`member_checkins`、`member_point_spends`、`member_point_awards`、`member_activity_daily`、`member_entitlements`、`llm_usage_daily`。

---

## 与上游同步

- 本 fork 的 `main` = 上游 `main` + 本地定制，按上线顺序提交
- 拉上游更新：`git remote add upstream https://github.com/Hamster-Prime/Smart_Group_Bot.git`，
  然后 `git fetch upstream && git merge upstream/main`，冲突在本 fork 解决
- **密钥永不入库**：凭据只在未跟踪的 `.env` 与数据库（加密存储）里，`config.toml` 不含凭据
- 对上游也有用的改动，欢迎直接向上游提 issue / PR；本 fork 的定制默认留在这边

## 配置

配置分三层。**部署与日常管理看
[docs/configuration.md](docs/configuration.md)**（要配什么、谁能改、改完何时生效）；
每一项的默认值、上下界、真实消费者与迁移注意事项见
**[docs/configuration-reference.md](docs/configuration-reference.md)**（开发者参考），
机器可读目录见
[`docs/configuration-fields.json`](docs/configuration-fields.json)（由
`python -m bot.tools.config_catalog` 从 schema 生成，测试保证它不与代码漂移）。

| 层 | 载体 | 改完生效 | 谁能改 |
| --- | --- | --- | --- |
| ① 部署引导 | `.env` / 进程环境变量 | 必须重启 | 有部署权限的人 |
| ② 热配置 | `runtime_config`（Mini App / API） | 保存即生效，少数需重启 | 仅最高管理员 |
| ③ 群级覆盖 | `group_settings`（群组页） | 保存即生效 | 该群管理员 / 最高管理员 |

几条容易踩错的口径：

- **`config.toml` 只在首次导入时读**：`runtime_config` 表里还没有记录时才导入一次，之后
  该文件被忽略，升级也不会重新导入。它不是第三套优先级。
- **密钥字段是只写的**：响应里恒为空串，`configured_secrets` 只回**键名**。改动密钥走
  `secret_changes` 的 `keep` / `clear` / `replace`，**空串表示「不变」，清空只能用
  `clear`**。
- **`restart_pending` 只列字段名**：保存成功 ≠ 立刻生效——标了 `restart` 的字段读的是本进程
  启动时装配的那一份，重启才换。字段清单以 `runtime_config.RESTART_REQUIRED_PATHS` 为
  单一来源，API 与界面上都能看到。
- **签到提醒的 `slots` 只是 CLI 白名单**：它约束 `--slot` 接受哪些值，**真正几点发由部署侧
  crontab 决定**，改完必须同步改 crontab。
- **公开部署的默认状态是中性**：审核证据频道 `log_channel_id = 0`（未配置）、交接对象为空、
  对外显示名为中性词。请在 Mini App 里显式写入你自己的部署值。

**不提供开关、也不该被「关掉」的**：鉴权与最高管理员判定、Telegram 协议硬上限（单条消息
4096、`callback_data` / start payload 前缀）、退款与财务幂等键、SSRF 校验、资源健康看门狗。
这些是安全 / 协议 / 算法不变量，做成开关只会制造「关掉之后就不安全了」的错觉，逐条见
[docs/configuration-reference.md](docs/configuration-reference.md#保持固定的参数不提供开关)。

## 部署

完整手册在上游存档 [`docs/README.upstream.md`](docs/README.upstream.md) 的「快速开始」一节；
下面是本 fork 部署前必须知道的前提与差异。

### 前提

- **机器人**：在 Telegram 里找 [@BotFather](https://t.me/BotFather)，`/newbot` 创建并拿到 `BOT_TOKEN`
- **公网 HTTPS 域名**：Telegram 会在 Mini App 里打开 `${MINIAPP_PUBLIC_BASE_URL}/settings`
  与 `/verify`，必须把它反代到后端监听地址；没有它，设置中心与入群验证都用不了
- **群管理员权限**：机器人必须被设为**群管理员**（至少「封禁用户」；置顶、删消息、限制成员
  同样依赖管理员权限），启动时会自检并在日志中告警
- **`.env` 最少四项**：`BOT_TOKEN`、`SUPER_ADMIN_ID`（你的 Telegram 数字 id，正整数）、
  `CONFIG_MASTER_KEY`（数据库里所有第三方密钥的解密钥匙，**必须与数据库一起备份**）、
  `MINIAPP_PUBLIC_BASE_URL`（上面那个公网 HTTPS 源站）

### 新部署

```bash
git clone https://github.com/uxiner/Smart_Group_Bot.git
cd Smart_Group_Bot
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 生成 CONFIG_MASTER_KEY，填进 .env
```

`data` 目录要先建好，并给容器用的**非 root** 账号。compose 的 `user:` 是
`${APP_UID:?…}:${APP_GID:?…}`（不设置直接报错，不静默回落），而 `APP_UID="$(id -u)"` 在
**root 宿主上就是 `0`**——那会让容器以 root 运行。所以这里显式给一个非零 uid/gid，
并让 `data` 的属主与之一致：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
APP_UID=10001 APP_GID=10001 docker compose up -d --build
docker compose logs -f
```

### 已有部署升级

已有库**不要**照抄上面的 `chown -R`——那会改掉现有数据的属主。正确做法是沿用现有属主：

```bash
ls -nd data                       # 先看清 data 现在的 uid:gid
APP_UID=<上面看到的 uid> APP_GID=<上面看到的 gid> docker compose up -d --build
```

升级前先备份：数据库是 SQLite **WAL 模式**，直接 `cp` 数据文件会拿到不一致的快照，用在线
备份 API（`VACUUM INTO` / `sqlite3.backup()`）。代码更新走「备份 → 拷已测产物 → 逐文件哈希
核对 → 重启 → 验活」，不是 `git pull`。

启动之后由**最高管理员私聊机器人发 `/settings`** 打开设置中心，在里面配置模型供应商、授权群与
管理员权限——这三样**没有**可直接用的默认值。完整路径见
**[docs/configuration.md](docs/configuration.md)**。

### 运行时要点

- **代码不在容器里挂载**：`bot/` 由 `Dockerfile` 的 `COPY bot ./bot` 打进镜像，`docker-compose.yml` 刻意**不**再挂 `./bot`（B-08）。早先那行 bind mount 把镜像里的代码整个盖住，于是 `docker compose build` 变成空操作、宿主机上 `git pull` 一重启就换掉生产代码，中间没有 diff 确认也没有版本 pin。改代码一律走重新构建镜像；确需热改请另建一个**不提交**的 `docker-compose.override.yml`
- 仍然挂载的只有三样：`./data`（SQLite）、`./prompt`（`prompt/*.md` 运行时加载，作为提示词的默认值）、`./config.toml`（一次性导入）
- **定时任务由宿主 cron 触发**：周报与上周活跃结算发奖（`python -m bot.tools.weekly_report`）、签到提醒（`python -m bot.tools.checkin_reminder --slot <时段>`，工具本身不判断「现在几点」，要不要发由 cron 决定）。商店到期清理有进程内常驻服务默认每 5 分钟跑一轮（`economy.expiry_check_seconds`），`python -m bot.tools.shop_expire` 主要用于手工补跑与 `--dry-run` 自检
- 端口默认只绑 `127.0.0.1:8480`（适合同机反向代理）；确需其他主机 / 容器直连时才显式设 `MINIAPP_BIND_ADDRESS=0.0.0.0` 并配防火墙

## 许可

MIT。仓库根目录的 [`LICENSE`](LICENSE) 是上游的 MIT 全文与原版权声明
（`Copyright (c) 2025 Sanite&Ava`），本 fork 原样保留、不改写；本 fork 的改动同样以 MIT
发布，不附加额外限制。许可范围、必须保留的声明、商业使用与再许可的边界，以及依赖 /
第三方服务 / 数据 / 图片各自按什么条款走，见 **[docs/licensing.md](docs/licensing.md)**。
