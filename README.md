<h1 align="center">🤖 Smart Group Bot · 个人部署分支</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/aiogram-3.x-0066CC.svg" alt="aiogram">
  <img src="https://img.shields.io/badge/tests-2688%20passed-brightgreen.svg" alt="Tests">
</p>

> **本仓库是 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot) 的个人部署分支**，
> 服务一个真实运营的私有 Telegram 大群。上游原版说明（完整功能、架构、部署细节）见
> [`docs/README.upstream.md`](docs/README.upstream.md)。

上游是一个**大模型驱动的群聊管理机器人**：把「聊天陪伴」和「群组治理」放在同一条消息管线里——既能自然参与
群聊、调用技能查资料，也能做内容审核、入群验证、爆破防护、民主投票封禁，全部配置在 Telegram Mini App 里完成。

本分支在它之上加了一批自研功能，**不向上游开 PR**（这是长期运营的部署分支，不是准备并回上游的特性分支）。

---

## 与原版的主要区别

### 1️⃣ 成员积分与自助命令（全新）

上游没有「成员激励」这一层。本分支加了一套积分经济：

| 能力 | 说明 |
|---|---|
| 每日签到 | `/checkin` 连续第 N 天得 N 分（10 分封顶）；断签从 1 分重来，**历史积分不清零** |
| 查看自己的 | `/points` 可用积分 + 连续天数；`/me` 积分 / 签到 / 违规 / 封禁一览 |
| 积分榜 | `/rank` 本群 Top10；`/rank week` 只看本周 |
| 群内检索 | `/find <关键词>` 在保留期聊天记录里搜历史消息 |
| 漏判举报 | 回复目标消息后 `/report [补充]`，立刻让审核模型复核并把结论转给管理员 |
| 签到提醒 | 每天 **9:00 / 12:00 / 15:00 / 18:00** 在群里发一条，带「一键签到 / 积分商店」按钮，10 分钟后自动删除 |

<details>
<summary>实现细节（积分怎么记账、提醒按钮怎么防作弊）</summary>

- **三本流水互不污染**：`member_checkins`（签到所得）/ `member_point_awards`（奖励所得）/ `member_point_spends`（消费）；可用积分只有一个定义 `available_from_ledgers()` = 签到 + 奖励 − 消费，所有页面都走它。
- **奖励绝不伪装成签到行**：连续签到天数是从签到日期集合倒推的，伪造一行就会把用户的连击算坏。
- 所有加减分由 `ref` 唯一索引保证幂等，重试不会重复记账。
- **签到提醒**（`bot/tools/checkin_reminder.py`）：文案按时段不同；按钮回调数据是固定常量、**不带用户 ID**，点击者身份只由 `callback.from_user` 决定，**没法替别人签到**；成功/已签过都用轻提示回执，不在群里另发消息；「今日已签到 N 人 + 名单」随点击刷新（最多 20 个昵称，昵称一律 HTML 转义）。
- 「🛒 积分商店」是 **URL 深链**（`t.me/<bot>?start=shop_<群号>`，`<bot>` 运行时用 `get_me()` 取，取不到就只留签到按钮）：菜单发到**私聊**，不在群里刷屏；私聊菜单按该用户在那个群的可用积分渲染、**不扣分**；`shop_` 与入群验证的 `verify…` 前缀互不干扰。
- 提醒**同一时段只发一条**（`(群, 时段)` 唯一键先占位再发送，发送失败释放占位以便重试）；命令与按钮共用同一个回执渲染函数，两边文案不会不一致。

</details>

### 2️⃣ 周活跃激励（全新）

让「在群里好好聊天」也有回报，而不只是签到：

- 每条合格发言按 `(群, 用户, 本地自然日)` 日累计，**每天 20 条封顶**（写入端 SQL 封顶，刷屏无收益）
- 每周结算最近一个完整自然周：**得分 = 发言数 + 2 × 活跃天数 + 被回复次数**，门槛「活跃 ≥3 天且发言 ≥10 条」
- 前 10 名发 **25 / 12 / 4 分**（每周共 77 分），结果随周报发进群里
- 采集与结算都在后台任务里跑（独立 session），**不阻塞群消息的回复路径**；`bot/tools/activity_award.py` 支持手动补跑与 `--dry-run`

### 3️⃣ 积分商店（全新）

| 商品 | 价格 | 时长 |
|---|---|---|
| 自定义头衔（Telegram 原生 member tag） | 30 分 | 7 天 |
| 同上 · 长租 | 80 分 | 30 天 |
| 置顶自己的求助 | 20 分 | 6 小时 |
| 抽奖一次 | 5 分 | 即时开奖 |

- **头衔** `/tag 文字`（加 `30天` 买长租）：1–16 字、不能带表情、不能与他人重名、不能出现「管理员/官方/客服」这类易冒充的词；**只卖给普通成员**（管理员头衔归群设置管，接口会返回成功但并不生效）；**续费往后顺延**，到期自动清除
- **置顶**：回复自己的一条消息后发 `/top`，静默置顶 6 小时（不弹全群通知），到时自动取消；每人同时最多 1 条
- **抽奖** `/draw`：5 分一次、每天最多 10 次；概率 39% 谢谢参与 / 20% 3 分 / 16% 5 分 / 10% 8 分 / 10% 12 分 / 4% 40 分 / 1% 100 分，**期望值正好 6.00 分/次**（玩家长期每次净赚 1 分，属净发放；随机数用 `secrets`）
- **失败自动退款**：扣分先入消费流水，Telegram 侧失败就写一笔退款流水（`shop-refund:<原ref>`，幂等），**不会出现「钱扣了东西没拿到」**；退款本身失败会记日志报警
- 到期清理 `bot/tools/shop_expire.py`（幂等、可 `--dry-run`）由宿主 cron 定时跑；权益状态记在 `member_entitlements`（`kind='tag'|'pin'`）

### 4️⃣ 审核增强（在原版审核之上改）

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
- **引用广告连坐**：引用/转发命中 ban 规则且高置信度时，**被引用消息的原作者**一并处置（删消息 + 记违规 + 质询）；限真实用户、管理员/群主/豁免跳过、同一条只处置一次、超过 7 天不追溯、警示式引用双方都不处理；`moderation.punish_quoted_author_enabled` **默认关闭（opt-in）**。
- **管理员也受审核**：除**最高管理员**（完全豁免）外的管理员/群主不再整段跳过——照常判定，命中后只**删消息 + 群内 @警示 + 记违规（delete）**，不质询/不封禁/不禁言/不累计警告；NSFW 图同样删图 + @警告但不质询；手动豁免名单仍完全跳过；`moderation.admin_moderation_enabled` **默认关闭（opt-in）**。
- **管理员违规证据私聊**：管理员违规时把完整证据（对象/身份/时间/规则/置信度/理由/送审原文/已执行/回链，带图附图片）私聊最高管理员；best-effort 不影响群内处置，同一人 10 分钟内 ≥5 次合并成一条；`moderation.admin_alert_super_admin_enabled` 默认开启；**频道投递开启时（默认）这条私聊路径被取代**。
- **审核证据 → 频道 + 人工放行**：群里**所有**被处置的命中都往「审核日志」频道发一条完整证据卡，**每条单独发**，每张卡带「人工放行 / 放行收回」按钮，只有最高管理员可点。**放行**：`review_state=released` + 立即解除该成员限制（作废质询超时封禁，不添加永久豁免）+ 频道新发 `🟢 人工放行 · 待调整规则` 交接消息（@your_bot mention，由 `moderation.review_handover_mention` 配置，留空则不 @ 任何人）。**收回**：按该 case 的**原始处置**重新施加限制（challenge → 重新禁言 + 重新质询；ban → 重新封禁；delete/warn → 无限制可恢复），结果写进频道状态行与 `🔴 放行收回 · 无需调整` 交接消息；最高管理员与手动豁免名单跳过。开关 `moderation.log_channel_enabled`（默认开）/ `log_channel_id`；关掉回到私聊老路径；**不新增 LLM 调用**。

#### ⚠️ 审核日志频道：默认 ID 与怎么改（C4-02）

**默认的审核日志频道 id 是 `-1000000000001`，且 `log_channel_enabled` 默认就是开的。**
也就是说：**不改任何配置的全新部署，会把命中审核的消息证据（群 id、用户 id、昵称、送审原文全文、判定理由，命中带图时还会在频道里追发图片）投递到这个频道。** fork 本项目之前请先确认那是不是你自己的频道，不是的话必须改。

| 项 | 默认值 | 含义 |
| --- | --- | --- |
| `moderation.log_channel_enabled` | `true` | 关掉后回到「私聊最高管理员」老路径 |
| `moderation.log_channel_id` | `-1000000000001` | 证据频道 id；**配成 `0` 才表示"未配置"**，此时频道投递整体不可用、自动回退私聊老路径 |

怎么改（覆盖入口）：

1. **运行时配置（推荐，存库热生效）** — `PUT /api/v1/settings`，body 形如
   `{"config": {"moderation": {"log_channel_id": -100XXXXXXXXXX}}, "revision": <当前 revision>}`。
   取当前 `revision` 用 `GET /api/v1/settings`（需要最高管理员身份）。
   > Mini App 界面**目前没有**这个控件，只能走 API 或直接改 `runtime_config` 表。
2. **`config.toml` 的 `[moderation]` 段** — **仅**在数据库里还没有 `runtime_config` 行时
   做一次性导入；一旦导入完成该文件即被忽略，之后只认数据库里的值。
3. **环境变量** — **无效**。`ModerationConfig` 不是 `BaseSettings`，`Settings` 也没有
   `env_nested_delimiter`，写 `MODERATION__LOG_CHANNEL_ID` 不会被读取。

</details>

### 5️⃣ 运营看板与周报（全新）

- `/cost [天数]`：token 用量、**缓存命中率**、思考 token、超时 / 空响应 / 解析失败，并按阶段（审核 / 决策 / 技能 / 视觉）拆分
- `bot/services/llm_metrics.py`：进程内用量累加器，60 秒惰性落盘；主回复路径**不写库、不抛异常**
- `bot/tools/weekly_report.py`：每周把「群健康 + 活跃榜」发进各授权群；**成本摘要只私发给最高管理员**（不在群里晒运营花销）

### 6️⃣ 工程质量与加固（在原版之上）

- **管理命令自动清理**：`ManagementCommandCleanupMiddleware` 在 5 秒后删掉群里的 `/ban`、`/mute` 等管理命令行（只碰管理命令，不动成员命令），走持久队列、重启不漏删
- **路由完整性回归**：`tests/test_router_route_integrity.py` 防止「helper 插在装饰器与处理器之间」导致**整个群机器人静默失效**（真实事故，已固化为回归）
- **测试规模**：上游基线 121 个测试文件 → 本分支 **140 个测试文件、2688 条用例全绿**
- 事务边界与幂等测试、`prompt/`（决策 / 审核 / 人格 / 闲聊）按实际运营调过、`docker-compose.yml` 与 `requirements.lock` 有本地调整

<details>
<summary>媒体内容策略与 /av 链路</summary>

- **群内 NSFW 一条底线**：公开露骨图片 → 删图 + 群内 @警告（2 分钟自删）+ 质询；复用审核那次视觉调用，**零额外成本**。带 `/av` 的图不经此流程；违规记账失败也**不会挡住删图**，图一定先离开群。开关 `moderation.nsfw_image_guard_enabled`，**默认关闭（opt-in）**。
- **图片一律不进群**：`/av` 的封面只发发起者私聊，群内只留文字（仍是「种子 N 条」+ 按钮，由逐字回归测试守住）。
- **群里「`/av` + 图片」**：**先删图再识别**（删除失败也继续识别），避免违规图停留。
- **私聊 `/av`**：支持发图识图反查番号（视觉读编号/演员名 → 自动查详情），每人每小时 10 次限流；只有演员名时退化为按演员检索。
- **私聊 `/av` 附发样例图**：封面之外再补 3~4 张（默认 4、上限 5、设 0 关闭），**只在私聊**。
- **私聊 `/av` 下载地址 + 制作信息**：详情里内联前 N 条磁力链（默认 3、硬上限 5、`av.inline_seed_count=0` 关闭），每条两行「大小 · 日期 · 标题」+ `<code>磁力</code>`；整条放不下时按 N→N−1→…→1 减少条数，**磁力链本身不截断**。片商 / 发行商 / 导演 / 系列 / 演员 / 时长 / 类型单独成「制作信息」块。
- **（可选，默认关）AI 题材概述**：`av.ai_synopsis_enabled=true` 时用**已抓到的字段**生成 1~3 句题材与看点，正文标注「AI 概述，非官方剧情」；独立 stage `synopsis` 便于用量拆分，失败 / 超时只跳过这一块。**不做 ed2k、不做价格、不抓官方剧情**（实测拿不到）。

</details>

---

## 命令速查（本分支新增；上游原有命令不变）

| 命令 | 作用 | 权限 |
|---|---|---|
| `/checkin` | 每日签到得积分 | 所有人 |
| `/points` | 可用积分 + 连续签到天数 | 所有人 |
| `/me` | 积分 + 签到 + 违规 + 封禁状态 | 所有人 |
| `/rank`、`/rank week` | 本群积分榜 Top10（总榜 / 本周） | 所有人 |
| `/find <关键词>` | 搜索群内保留期消息 | 所有人 |
| `/report` | 回复漏判消息后举报，触发模型复核 | 所有人 |
| `/shop` | 积分商店：价目表与每件商品怎么用 | 所有人 |
| `/tag <文字>`、`/tag <文字> 30天` | 用积分给自己挂群内头衔（30 分 7 天 / 80 分 30 天） | 所有人 |
| `/top` | 回复自己的一条消息后发送，花 20 分置顶 6 小时 | 所有人 |
| `/draw` | 花 5 分抽奖一次（每天最多 10 次） | 所有人 |
| `/health` | 本群今日审核命中、待质询、归档量、模型通道 | 管理员 |
| `/modstats [天数]` | 审核质量报表（命中构成、误伤率） | 管理员 |
| `/cost [天数]` | 成本与健康看板（token / 缓存 / 异常） | 管理员 |
| `/exemptlist` | 审核豁免与静默名单（翻页 / 移除） | 管理员 |
| `/unaiexempt` | 取消某用户的 AI 审核豁免 | 管理员 |

新增数据表：`member_checkins`、`member_point_spends`、`member_point_awards`、`member_activity_daily`、`member_entitlements`、`llm_usage_daily`。

---

## 与上游同步

- `main` 是本分支自己的线：上游 `main` + 本地定制，按上线顺序提交
- 拉上游更新：`git fetch upstream && git merge upstream/main`，冲突在本分支解决
- **不向上游开 PR**；密钥永不入库（凭据只在未跟踪的 `.env` 里，`config.toml` 不含凭据）

## 部署

沿用上游的 Docker 方式（细节见 [`docs/README.upstream.md`](docs/README.upstream.md)）：

```bash
cp .env.example .env      # 填 BOT_TOKEN、最高管理员 ID、加密主密钥
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d --build
```

- 数据库是 SQLite（**WAL 模式**）：备份用 SQLite 在线备份 API（`VACUUM INTO` / `sqlite3.backup()`），**不要直接 `cp` 数据文件**
- **代码不在容器里挂载**：`bot/` 由 `Dockerfile` 的 `COPY bot ./bot` 打进镜像，`docker-compose.yml` 刻意**不**再挂 `./bot`（B-08）。早先那行 bind mount 把镜像里的代码整个盖住，于是 `docker compose build` 变成空操作、宿主机上 `git pull` 一重启就换掉生产代码，中间没有 diff 确认也没有版本 pin。改代码一律走重新构建镜像；确需热改请另建一个**不提交**的 `docker-compose.override.yml`
- 仍然挂载的只有三样：`./data`（SQLite）、`./prompt`（运行时加载并 seed 进 `runtime_config`）、`./config.toml`（一次性导入）
- 生产机代码更新走「备份 → 拷已测产物 → 逐文件哈希核对 → 重启 → 验活」，不是 `git pull`
- 定时任务由宿主 cron 触发：周报与活跃榜发奖（`python -m bot.tools.weekly_report`）、商店到期清理（`python -m bot.tools.shop_expire`，建议每 5–10 分钟）、签到提醒（`python -m bot.tools.checkin_reminder --slot <时段>`，工具本身不判断「现在几点」，要不要发由 cron 决定）

## 许可

MIT，版权归上游 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)；本分支的本地改动同样以 MIT 发布。
