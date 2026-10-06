# Fork 进阶参考（功能细则、定时任务、升级与开发）

> **这份文档是进阶参考，不是部署必读。** 只想把机器人跑起来，请看
> [README](../README.md) 的部署一节和[配置手册](./configuration.md)；
> 配置项的默认值、上下界与实现细节在[配置参考手册](./configuration-reference.md)。
> 本文保留本 fork 相对上游各项功能的完整口径、命令速查、定时任务配置，以及升级、
> 备份与开发测试的注意事项。

- 本仓库：<https://github.com/uxiner/Smart_Group_Bot>
- 上游：<https://github.com/Hamster-Prime/Smart_Group_Bot>（原版完整说明见
  [`README.upstream.md`](./README.upstream.md)）

---

## 功能细则

### 成员积分与自助命令

**以下阈值与奖励向量都是默认值**（可在设置中心「运营参数」页改）：

- `/checkin` 连签第 N 天得 N 分，断签从 1 分重新开始，此前赚到的分不清零；上限默认 10 分
- 可用积分只有一个定义：签到所得 + 奖励所得 − 消费所得
- 奖励绝不伪装成签到行：连续签到天数由签到日期集合倒推，伪造一行会把连击算坏
- 所有加减分由 `ref` 唯一索引保证幂等，重试不会重复记账
- `/report` 回复被漏判的消息后发送，机器人立刻让审核模型复核，并把结果与原文转交管理员
- `/shop` 是 **URL 深链**（`t.me/<bot>?start=shop_<群号>`，`<bot>` 运行时用 `get_me()` 取）：
  菜单发到私聊、不在群里刷屏，按该用户在该群的可用积分渲染且不扣分；`shop_` 与入群验证的
  `verify…` 前缀互不干扰

### 周活跃激励

让「在群里好好聊天」也有回报，而不只是签到。**以下阈值与奖励向量都是默认值**
（`activity.*`，可运营参数页改）：

- 每条合格发言按 `(群, 用户, 本地自然日)` 日累计，每天 `max_daily_messages` 条封顶（默认 20），
  刷屏无收益
- 每周结算最近一个完整自然周：**得分 = 发言数 + 2 × 活跃天数 + 被回复次数**；
  门槛是活跃 ≥3 天且发言 ≥10 条
- 名次奖励由一个向量给定，默认 `[25, 12, 12, 4, 4, 4, 4, 4, 4, 4]`（10 个名次、每周共 77 分）；
  榜单长度与总额都从这个向量派生，不另存一份
- 结果随周报发进群里；采集与结算都在后台任务里跑（独立 session），不阻塞群消息的回复路径

### 积分商店

价格与时长都是 `economy.*` 的**默认值**，可改：

| 商品 | 默认价格 | 默认时长 |
|---|---|---|
| 自定义头衔（Telegram 原生 member tag） | 30 分 | 7 天 |
| 同上 · 长租 | 80 分 | 30 天 |
| 置顶自己的求助 | 20 分 | 6 小时 |
| 抽奖一次 | 5 分 | 即时开奖 |

- **头衔** `/tag 文字`（加 `30天` 买长租）：1–16 字（`tag_max_length`，上界就是 16）、不能带表情、
  不能与他人重名、不能出现「管理员/官方/客服」这类易冒充的词；**只卖给普通成员**
  （管理员头衔归群设置管，接口会返回成功但并不生效）；续费往后顺延，到期自动清除。
  头衔依赖 `setChatMemberTag`，客户端不支持或 Telegram 拒绝时**自动退款**并提示
- **置顶**：回复自己的一条消息后发 `/top`，静默置顶 6 小时（不弹全群通知），到时自动取消；
  每人同时最多 1 条
- **抽奖** `/draw`：默认 5 分一次、每天最多 10 次；默认权重换算成概率是 39% 谢谢参与 /
  20% 3 分 / 16% 5 分 / 10% 8 分 / 10% 12 分 / 4% 40 分 / 1% 100 分，
  **期望值正好 6.00 分/次**（玩家长期每次净赚 1 分，属净发放；随机数用 `secrets`）。
  权重是 `economy.lottery_prizes` 的相对权重，总权重是对求和的**派生值**，改权重不会和总额打架
- **失败自动退款**：扣分先入消费流水，Telegram 侧失败就写一笔退款流水（`shop-refund:<原ref>`，
  幂等），不会出现「钱扣了东西没拿到」；退款本身失败会记日志报警
- 到期清理由进程内的常驻服务默认每 5 分钟跑一轮（`economy.expiry_check_seconds`）；
  `bot/tools/shop_expire.py`（幂等、可 `--dry-run`）用于手工补跑。
  权益状态记在 `member_entitlements`（`kind='tag'|'pin'`）

### 审核增强

| 能力 | 一句话 |
|---|---|
| 上下文感知 | 审核时带上「这句话是在什么对话里说的」，少把正常回复误判成违规 |
| 置信度治理 | 边缘判定不直接处置，改走**质询卡片**；成员可花 2 积分免除 |
| 申诉与复核 | 被处置者可在卡片上申诉，管理员一键复核 |
| 质量报表 | `/modstats` 命中构成与**误伤率**；`/health` 看今日运营状态 |
| 名单管理 | `/exemptlist` 豁免与回复静默名单（翻页、移除）、`/unaiexempt` |
| 规则扫描范围 | 每条规则可选扫「本人正文 / 被引用正文 / 机器人图片描述」 |
| 引用广告连坐 | 引用广告命中 ban 时，被引用的原作者一并处置（有豁免与 7 天限制，默认关闭） |
| 管理员也受审核 | 除最高管理员外，管理员违规照常删消息 + @警示（默认关闭） |
| 审核证据 → 频道 | 每条命中发证据卡到「审核日志」频道，带「人工放行 / 放行收回」按钮 |

细则：

- **上下文感知**：`moderation_context.py` 把「这句话是在什么对话里说的」连同前文一起交给
  审核模型，减少误判
- **置信度治理**：边缘判定不再直接处置，改走质询卡片；成员可花 2 积分免除质询
- **规则扫描范围**：每条正则/关键词规则可选 `message`（默认，只扫成员自己写的正文）/
  `message+quote`（并入被引用正文）/ `message+vision`（并入机器人图片描述）/ 组合；
  机器人图片描述默认**不参与**正则，避免「描述购物界面 → 秒杀/优惠券命中 → 误删」
- **引用广告连坐**：引用/转发命中 ban 规则且高置信度时，被引用消息的**原作者**一并处置
  （删消息 + 记违规 + 质询）；限真实用户，管理员/群主/豁免跳过，同一条只处置一次，
  超过 `moderation.quoted_author_max_age_seconds`（默认 7 天）不追溯；
  `moderation.punish_quoted_author_enabled` **默认关闭（opt-in）**
- **管理员也受审核**：除**最高管理员**（完全豁免）外的管理员/群主不再整段跳过——照常判定，
  命中后只**删消息 + 群内 @警示 + 记违规（delete）**，不质询/不封禁/不禁言/不累计警告；
  NSFW 图同样删图 + @警告但不质询；`moderation.admin_moderation_enabled` **默认关闭（opt-in）**
- **管理员违规证据私聊**：管理员违规时把完整证据（对象/身份/时间/规则/置信度/理由/送审原文/
  已执行/回链，带图附图片）私聊最高管理员；best-effort 不影响群内处置，同一人 10 分钟内
  ≥5 次合并成一条；`moderation.admin_alert_super_admin_enabled` 默认开启；只有频道投递
  真的开着（`log_channel_enabled` 且 `log_channel_id` 填了频道）这条私聊路径才被取代——
  默认频道未配置，所以默认走的还是私聊
- **审核证据 → 频道 + 人工放行**：群里**所有**被处置的命中都往「审核日志」频道发一条完整
  证据卡，**每条单独发**，每张卡带「人工放行 / 放行收回」按钮，只有最高管理员可点。
  **放行**：`review_state=released` + 立即解除该成员限制 + 频道新发交接消息（由
  `moderation.review_handover_mention` 配置 @ 谁，留空则不 @ 任何人）。
  **收回**：按该 case 的**原始处置**重新施加限制（challenge → 重新禁言 + 重新质询；
  ban → 重新封禁；delete/warn → 无限制可恢复），结果写进频道状态行；
  最高管理员与手动豁免名单跳过。开关 `moderation.log_channel_enabled`（默认开）/
  `log_channel_id`（默认 `0`，即未配置）；关掉回到私聊老路径；**不新增 LLM 调用**

#### 审核日志频道：默认未配置，以及怎么改

**默认的审核日志频道 id 是 `0`（= 未配置），`log_channel_enabled` 默认开着，但配成 `0` 时
频道路由整体关闭。** 也就是说：**不改任何配置的全新部署不会向任何频道投递命中审核的消息
证据**（群 id、用户 id、昵称、送审原文全文、判定理由……），而是回退到「私聊最高管理员」的
老路径。要启用频道投递，先把 `log_channel_id` 填成**你自己的**频道 id。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `moderation.log_channel_enabled` | `true` | 关掉后回到私聊最高管理员 |
| `moderation.log_channel_id` | `0` | 证据频道 id，`0` = 未配置 |
| `moderation.review_handover_mention` | `""` | 规则调整交接 @ 谁，空 = 不 @ |

覆盖入口，从推荐到不推荐：

1. **设置中心「审核验证」页（推荐）** — 上表这三个字段在界面上都有控件：
   `证据投递到频道` 开关、`证据频道 ID` 输入框、`交接对象` 输入框在同一段里。
   保存即热生效，不需要重启
2. **API `PUT /api/v1/settings`** — 鉴权走 `Authorization` 头，方案名是 `tma`，后面的凭据是
   Telegram 打开 Mini App 时下发的 WebApp `initData` 字符串；不是 Bearer token，
   本仓库也没有别的鉴权方案。该接口**仅最高管理员**可调用。三步流程：
   1. `GET /api/v1/settings`，从响应里取 `revision` 与 `config`——两者与 `ok` 平级、
      直接在响应顶层（响应里没有 `data` 这一层包裹）
   2. 在**完整的** `config` 上改 `moderation.log_channel_id`，余字段原样保留
   3. `PUT /api/v1/settings`，请求体只有三个顶层字段：`revision`、`config`、
      `secret_changes`（本例不需要密钥变更，可传空对象）
3. **`config.toml` 的 `[moderation]` 段** — **仅**在数据库里还没有 `runtime_config` 行时
   做一次性导入；一旦导入完成该文件即被忽略
4. **环境变量** — **无效**。写 `MODERATION__LOG_CHANNEL_ID` 不会被读取
5. **直接改 `runtime_config` 表** — **不要这么做**。手改会绕过 schema 校验、乐观锁与密钥
   操作接口（密钥另存并加密），设置中心下次保存就会把改动覆盖掉

API 语义备忘：`PUT` 是**整份替换**而不是嵌套 patch（漏掉的字段会回到默认值，必须先 `GET`
再回传整份）；`config` 里密钥字段恒为空串，**空串表示「不变」而不是「清空」**，要清空只能用
`secret_changes`（`clear` / `replace` / `keep`）；`revision` 对不上返回
**409 `revision_conflict`**；未知键或取值非法返回 **400**。

### 运营看板与周报

- `/cost [天数]`：token 用量、**缓存命中率**、思考 token、超时 / 空响应 / 解析失败，
  并按阶段（审核 / 决策 / 技能 / 视觉）拆分。**群管理员及以上可用**
- `bot/services/llm_metrics.py`：进程内用量累加器，60 秒惰性落盘；主回复路径不写库、不抛异常
- `bot/tools/weekly_report.py`：每周把「群健康 + 活跃榜」发进各授权群；成本摘要只私发给
  最高管理员（不在群里晒运营花销）

### 工程质量与加固

- **管理命令自动清理**：`ManagementCommandCleanupMiddleware` 在 5 秒后删掉群里的
  `/ban`、`/mute` 等管理命令行（只碰管理命令，不动成员命令），走持久队列、重启不漏删
- **路由完整性回归**：[`tests/test_router_route_integrity.py`](../tests/test_router_route_integrity.py)
  防止「helper 插在装饰器与处理器之间」导致整个群机器人静默失效
- **测试**：[`tests/`](../tests/) 里既有 `unittest.TestCase` 用例，也有 pytest 风格的用例
  （例如 [`tests/test_tools_bootstrap_settings.py`](../tests/test_tools_bootstrap_settings.py)
  用了 fixture 与 parametrize），所以**用 pytest 跑全套**，别用 `unittest discover`
  （它不会收集那两个纯 pytest 模块，也不会加载 [`tests/conftest.py`](../tests/conftest.py)
  里的环境隔离）：

  ```bash
  pip install pytest          # 测试依赖；运行时依赖见 requirements.lock
  python -m pytest tests
  ```

  这里**不写「多少条用例全绿」**——测试数每次提交都在变，写死的数字只会变成误导
- 事务边界与幂等测试、`prompt/`（决策 / 审核 / 人格 / 闲聊）按实际运营调过、
  `docker-compose.yml` 与 `requirements.lock` 有本地调整

### 媒体内容策略与 /av 链路

- **群内 NSFW 一条底线**：公开露骨图片 → 删图 + 群内 @警告（2 分钟自删）+ 质询；图片**复用
  审核那次视觉调用，零额外成本**（视频会额外看一次封面缩略图）。开关
  `moderation.nsfw_image_guard_enabled`，**默认关闭（opt-in）**
- **图片一律不进群**：`/av` 的封面只发发起者私聊，群内只留文字
- **群里「`/av` + 图片」**：**先删图再识别**（删除失败也继续识别），避免违规图停留
- **私聊 `/av`**：支持发图识图反查番号（视觉读编号/演员名 → 自动查详情）；每人每小时 10 次
  限流是**代码常量，不提供配置开关**；只有演员名时退化为按演员检索
- **私聊 `/av` 附发样例图**：封面之外再补 3~4 张（默认 4、上限 5、设 0 关闭），只在私聊
- **私聊 `/av` 下载地址 + 制作信息**：详情里内联前 N 条磁力链（默认 3、硬上限 5、
  `av.inline_seed_count=0` 关闭），每条两行「大小 · 日期 · 标题」；片商 / 发行商 / 导演 /
  系列 / 演员 / 时长 / 类型单独成「制作信息」块
- **（可选，默认关）AI 题材概述**：`av.ai_synopsis_enabled=true` 时用已抓到的字段生成
  1~3 句题材与看点，正文标注「AI 概述，非官方剧情」；失败或超时只跳过这一块。
  **不做 ed2k、不做价格、不抓官方剧情**

---

## 命令速查（本 fork 新增；上游原有命令不变）

| 命令 | 作用 | 权限 |
|---|---|---|
| `/settings` | 打开设置中心（最高管理员与已授权群管理员可用） | 最高管理员 / 已授权群管理员 |
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

上游原有命令（`/help`、`/lm`、`/addrule`、`/rules`、`/av`、`/voteban`、`/ban`、`/unban`、
`/spam`、`/warnings`、`/clearwarnings`、`/raidguard`、`/aiexempt`、`/mute`、`/proactive`、
`/mimic`、`/compact`、`/authgroup`、`/unauthgroup`、`/authlist`、`/banlist`、`/authadmin`、
`/unauthadmin`、`/adminlist`、`/atreply`、`/tts`、`@admin` 等）保持不变，
逐条说明见 [`README.upstream.md`](./README.upstream.md)。

新增数据表：`member_checkins`、`member_point_spends`、`member_point_awards`、
`member_activity_daily`、`member_entitlements`、`llm_usage_daily`。

---

## 定时任务（需要部署者自己配 cron）

机器人**不会**自己决定几点发。周报、周活跃结算、签到提醒都由**宿主机 cron** 调用容器内的
工具触发；不配 cron 就没有周报和签到提醒。

用 `crontab -e` 添加下面的条目，把 `/opt/smart-group-bot` 换成你自己的**项目绝对路径**
（`pwd` 能看到）。cron 按**宿主机时区**触发，容器里的 `TZ` 不改变宿主的调度时刻，
下面的时间要按你的宿主时区换算：

```cron
# 每周一早上 9:00 发上周周报（任务自带上周活跃结算）
0 9 * * 1 cd /opt/smart-group-bot && docker compose exec -T bot python -m bot.tools.weekly_report

# 签到提醒：每天 9:00 一条，小时要与设置中心「提醒时段」一致
0 9 * * * cd /opt/smart-group-bot && docker compose exec -T bot python -m bot.tools.checkin_reminder --slot 9
```

手动补跑和自检用下面这些命令（在项目目录里直接执行）：

```bash
docker compose exec -T bot python -m bot.tools.activity_award --dry-run
docker compose exec -T bot python -m bot.tools.activity_award
docker compose exec -T bot python -m bot.tools.weekly_report --dry-run
docker compose exec -T bot python -m bot.tools.shop_expire --dry-run
```

`--slot` 按**容器内时区（默认 Asia/Shanghai）**取值；工具本身不判断「现在几点」，
设置中心的 `checkin_reminder.slots` 也只是命令白名单与文案，**改了时段不会自动更新
crontab**。`bot/tools/checkin_reminder.py` 的块 docstring 里有可复制的样例 crontab。

---

## 升级已有部署

已有库**不要**照抄新部署的 `chown -R`——那会把现有数据的属主一起改掉。正确做法是沿用现有属主，
把它写进 `.env`（和新部署同一个文件，`APP_UID`、`APP_GID` 两项都要写）：

```bash
ls -nd data            # 先看清 data 现在的 uid:gid
id -u data             # 或者用这个拿 uid
id -g data             # 或者用这个拿 gid
```

把上面查到的数值填进 `.env` 的 `APP_UID` / `APP_GID`，然后重建镜像并重新创建容器：

```bash
docker compose up -d --build bot
```

升级前先备份：数据库是 SQLite **WAL 模式**，直接 `cp` 数据文件会拿到不一致的快照，
用在线备份 API（`VACUUM INTO` / `sqlite3.backup()`）。代码更新走
「备份 → 拷已测产物 → 逐文件哈希核对 → 重建 → 验活」，不是 `git pull`。

### 运行时要点

- **代码不在容器里挂载**：`bot/` 由 `Dockerfile` 的 `COPY bot ./bot` 打进镜像，
  `docker-compose.yml` 刻意**不**再挂 `./bot`。改代码一律走重建镜像；确需热改请另建一个
  **不提交**的 `docker-compose.override.yml`
- 仍然挂载的只有三样：`./data`（SQLite）、`./prompt`（`prompt/*.md` 运行时加载，作为提示词的
  默认值）、`./config.toml`（一次性导入）
- 端口默认只绑 `127.0.0.1:8480`（适合同机反向代理）；确需其他主机 / 容器直连时才显式设
  `MINIAPP_BIND_ADDRESS=0.0.0.0` 并配防火墙

---

## 与上游同步

- 本 fork 的 `main` = 上游 `main` + 本地定制，按上线顺序提交
- 拉上游更新：`git remote add upstream https://github.com/Hamster-Prime/Smart_Group_Bot.git`，
  然后 `git fetch upstream && git merge upstream/main`，冲突在本 fork 解决
- **密钥永不入库**：凭据只在未跟踪的 `.env` 与数据库（加密存储）里，`config.toml` 不含凭据
- 对上游也有用的改动，欢迎直接向上游提 issue / PR；本 fork 的定制默认留在这边

## 相关文档

| 关注点 | 文件 |
| --- | --- |
| 部署、日常管理与权限 | [`../README.md`](../README.md)、[`configuration.md`](./configuration.md) |
| 配置逐字段细节 | [`configuration-reference.md`](./configuration-reference.md) |
| 机器可读字段目录 | [`configuration-fields.json`](./configuration-fields.json) |
| 常量分类 | [`constant-classification.md`](./constant-classification.md) |
| 上游原版说明存档 | [`README.upstream.md`](./README.upstream.md) |
| 许可 | [`licensing.md`](./licensing.md)、[`../LICENSE`](../LICENSE) |