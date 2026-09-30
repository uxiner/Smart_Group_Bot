<h1 align="center">🤖 Smart Group Bot · 个人部署分支</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/aiogram-3.x-0066CC.svg" alt="aiogram">
  <img src="https://img.shields.io/badge/tests-1917%20passed-brightgreen.svg" alt="Tests">
</p>

> **本仓库是 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot) 的个人部署分支**，
> 服务一个真实运营的私有 Telegram 大群。`main` = 上游 `main` + 一批自研功能：
> **成员积分与自助**、**审核质量与成本看板**、**周活跃激励**。
> 上游原版说明（完整功能、架构、部署细节）保存在 [`docs/README.upstream.md`](docs/README.upstream.md)。

**上游项目是什么**：一个由大模型驱动的 Telegram 群聊智能管理机器人，把「聊天陪伴」和「群组治理」合并进同一条消息管线——既能自然参与群聊、调用技能查资料，也能完成内容审核、入群验证、爆破防护和民主投票封禁；全部运行配置在 Telegram Mini App 内可视化完成。

本分支**不向上游提交 PR**，它是给一个群长期运营用的部署分支（见文末「与上游同步」）。

---

## 与原版的主要区别

### 1️⃣ 成员积分体系与自助命令（全新）

上游没有「群成员激励」这一层。本分支加了一整套积分经济：

| 能力 | 说明 |
|---|---|
| 每日签到 | `/checkin`：连续第 N 天得 N 分（10 分封顶）；断签从 1 分重来，**历史积分不清零** |
| 查看积分 | `/points`（可用积分 + 连续天数）、`/me`（积分 + 签到 + 违规 + 封禁状态一览） |
| 积分榜 | `/rank`（本群 Top10）、`/rank week`（只看本周获得的积分） |
| 群内检索 | `/find <关键词>`：在本群保留期的聊天记录里搜索历史消息 |
| 漏判举报 | 回复目标消息后 `/report [补充]`：立刻让审核模型复核，并把结论与原文转给管理员 |

设计要点：

- **三本流水互不污染**：`member_checkins`（签到所得）/ `member_point_awards`（奖励所得）/ `member_point_spends`（消费）。
  **可用积分只有一个定义**：`available_from_ledgers()` = 签到 + 奖励 − 消费，任何页面都走它。
- **奖励绝不伪装成签到行**：连续签到天数是从签到日期集合倒推出来的，伪造一行就会把用户的连击算坏。
- 所有加减分都由 `ref` 唯一索引保证幂等，重试/重复执行不会重复记账。

### 2️⃣ 周活跃激励（全新）

让「在群里好好聊天」也有回报，而不只是签到：

- 每条合格发言按 `(群, 用户, 本地自然日)` 日累计，**每天 20 条封顶**（写入端用 SQL 封顶，刷屏无收益）
- 每周结算最近一个完整自然周：**得分 = 发言数 + 2 × 活跃天数 + 被回复次数**；门槛「活跃 ≥3 天且发言 ≥10 条」
- 前 10 名发 **25 / 12 / 4 分**（每周共 77 分），结果随周报发进群里；`bot/tools/activity_award.py` 支持手动补跑与 `--dry-run`
- 采集与结算都在后台任务里跑（独立 session），**不阻塞群消息的回复路径**

### 3️⃣ 审核增强（在原版审核之上改）

| 能力 | 说明 |
|---|---|
| 上下文感知 | `moderation_context.py`：把「这句话是在什么对话里说的」连同前文一起交给审核模型，减少把正常回复误判成违规 |
| 置信度治理 | 边缘判定不再直接处置，改走**质询卡片**；成员可花 2 积分免除质询 |
| 申诉与复核 | 被处置者可在卡片上申诉，管理员一键复核 |
| 质量报表 | `/modstats`：命中构成、边缘判定、**误伤率**；`/health`：今日命中、待完成质询、归档量、当前模型通道 |
| 名单管理 | `/exemptlist`（豁免 / 回复静默名单，可翻页、一键移除）、`/unaiexempt`（取消某用户的 AI 审核豁免） |

### 4️⃣ 运营看板与周报（全新）

- `/cost [天数]`：token 用量、**缓存命中率**、思考 token、超时 / 空响应 / 解析失败，并按阶段（审核 / 决策 / 技能 / 视觉）拆分
- `bot/services/llm_metrics.py`：进程内用量累加器，60 秒惰性落盘；主回复路径**不写库、不抛异常**
- `bot/tools/weekly_report.py`：每周把「群健康 + 活跃榜」发进各授权群；**成本摘要只私发给最高管理员**（不在群里晒运营花销）

### 5️⃣ 运维与工程质量（在原版之上加固）

- **管理命令自动清理**：`ManagementCommandCleanupMiddleware` 在 5 秒后删掉群里的 `/ban`、`/mute` 等管理命令行（只碰管理命令，不动成员命令），避免群里堆一屏命令噪声；走持久队列，重启也不漏删
- **测试规模**：上游 100 个测试文件 → 本分支 **110 个文件、1917 条用例全绿**（新增签到 / 积分 / 活跃激励 / 质量与成本报表 / 审核上下文 / 申诉 / 路由完整性等）
- **路由完整性回归测试**：`tests/test_router_route_integrity.py`——防止「helper 函数插在装饰器与处理器之间」导致**整个群机器人静默失效**（真实事故，已固化为回归）
- 事务边界与幂等测试、`prompt/`（决策 / 审核 / 人格 / 闲聊）按实际运营调过、`docker-compose.yml` 与 `requirements.lock` 有本地调整

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
| `/health` | 本群今日审核命中、待质询、归档量、模型通道 | 管理员 |
| `/modstats [天数]` | 审核质量报表（命中构成、误伤率） | 管理员 |
| `/cost [天数]` | 成本与健康看板（token / 缓存 / 异常） | 管理员 |
| `/exemptlist` | 审核豁免与静默名单（翻页 / 移除） | 管理员 |
| `/unaiexempt` | 取消某用户的 AI 审核豁免 | 管理员 |

新增数据表：`member_checkins`、`member_point_spends`、`member_point_awards`、`member_activity_daily`、`llm_usage_daily`。

---

## 与上游同步

- `main` 是本分支自己的线：上游 `main` + 本地定制，历史按上线顺序提交
- 拉上游更新：`git fetch upstream && git merge upstream/main`，冲突在本分支解决
- **不向上游开 PR**：这是部署分支，不是准备并回上游的特性分支
- 密钥永不入库：凭据只放在未跟踪的 `.env` 里（`config.toml` 不含任何凭据）

## 部署

沿用上游的 Docker 部署方式（细节见 [`docs/README.upstream.md`](docs/README.upstream.md) 的「快速开始」）：

```bash
cp .env.example .env      # 填 BOT_TOKEN、最高管理员 ID、加密主密钥
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d --build
```

- 数据库是 SQLite（**WAL 模式**）：备份请用 SQLite 在线备份 API（`VACUUM INTO` / `sqlite3.backup()`），**不要直接 `cp` 数据文件**
- 生产机上的代码更新走「备份 → 拷贝已测产物 → 逐文件哈希核对 → 重启 → 验活」，不是 `git pull`
- 周报与活跃榜发奖是定时任务（`python -m bot.tools.weekly_report`），由宿主机 cron 触发

## 许可

MIT，版权归上游 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)；本分支的本地改动同样以 MIT 发布。
