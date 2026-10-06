# Smart Group Bot

大模型驱动的 Telegram 群管理机器人：既能在群里自然回复、调用技能查资料，也能做内容审核、
入群验证、爆破防护和民主投票封禁。Python 项目，可以用 Docker 自行部署。它不是 Telegram
官方机器人，也不提供模型服务——模型要接你自己的供应商。

## 这是什么，和上游什么关系

- 本项目基于 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)
  的**独立维护 fork**，保留上游的基础功能，并在此之上加了这个 fork 自己的定制与修复
- 当前维护者是 GitHub [@uxiner](https://github.com/uxiner)。本仓库维护定制功能与缺陷修复，
  上游的新版本按需评估后再合并，改动经验证才发布
- 本仓库**不承诺**与上游始终同步，也没有企业级保障、服务等级协议或私有运维通道
- 遇到这个 fork 自身的问题，请提到[本仓库的 Issues](https://github.com/uxiner/Smart_Group_Bot/issues)，
  不要交给上游处理
- 业务数据留在你自己的服务器上：聊天记录、积分、审核名单都存在项目目录的 `data/` 里，
  第三方密钥经主密钥加密后入库；本项目没有自建的云端服务
- 许可 MIT。上游原作者的版权声明原样保留在 [`LICENSE`](LICENSE)，边界见[许可说明](docs/licensing.md)

## 相对上游的新特色

| 特色 | 说明 |
| --- | --- |
| 成员积分 | 每日签到、积分榜、积分商店（头衔、置顶求助、抽奖），缺勤不清零 |
| 周活跃激励 | 按每周发言与活跃天数发积分并生成活跃榜；**需要你自己配定时任务**，不会开箱自动发 |
| 审核增强 | 带对话上下文的审核、低把握转人工质询、申诉复核、成员可举报漏判 |
| 审核证据与人工放行 | 每条命中可发一张证据卡到频道，带「人工放行 / 放行收回」按钮；频道默认不启用 |
| 运营看板 | `/health`、`/modstats`、`/cost` 看审核命中、误伤率与 token 成本 |
| 设置中心 | 私聊机器人发 `/settings` 打开的 Mini App，配模型、运营参数与群组设置 |
| 群级权限隔离 | 可以授权多个群；群管理员只管自己获授权的群，看不到也改不了全局设置 |
| 多媒体与检索（可选） | 语音回复、音乐检索回退；`/av` 影片检索需要按群显式开启 |
| 长期记忆 | 跨天保留成员的稳定事实（口味、身份、约定），成员可自行查看和删除 |

## 从零部署

推荐路径是**一台 Linux 服务器 + Docker Compose**，下面的命令都在项目目录里执行。
整体五步：准备环境 → 写 `.env` → 启动容器 → 配 HTTPS 反代 → 在 Telegram 里授权与首次配置。

### 部署前要准备

- 一台 Linux 服务器，装好 Docker Engine、Compose 插件和 Git
- 一个解析到这台服务器的公网域名，以及能给域名签证书的 HTTPS 反代（下面用 Caddy 举例）
- 服务器能出网访问 `api.telegram.org` 和你的模型 API
- 在 [@BotFather](https://t.me/BotFather) 用 `/newbot` 建好机器人，拿到令牌
- 你自己的 Telegram 数字用户 ID（正整数）
- 一个可用的模型供应商：API 地址、API Key、模型名称

资源只给个参考不是门槛：2 核 4G 的小机器够单人使用，实际响应快慢主要看模型供应商。
容器时区默认按中国时间，需要别的时区就在 `.env` 里改 `TZ`。首次 `docker compose up --build`
要拉依赖并编译镜像，视网络情况通常几分钟。

### 第一步：写好 .env

```bash
git clone https://github.com/uxiner/Smart_Group_Bot.git
cd Smart_Group_Bot
cp .env.example .env
openssl rand -hex 32        # 生成 CONFIG_MASTER_KEY，宿主没有 openssl 就用任意密码生成器
```

然后编辑 `.env`，至少填下面五项（`APP_UID` 与 `APP_GID` 写同一个数）：

| 变量 | 填什么 |
| --- | --- |
| `BOT_TOKEN` | BotFather 给的机器人令牌 |
| `SUPER_ADMIN_ID` | 你自己的 Telegram 数字用户 ID |
| `CONFIG_MASTER_KEY` | 上一步生成的主密钥，生成后不要改 |
| `MINIAPP_PUBLIC_BASE_URL` | HTTPS 源站，例如 `https://bot.example.com`（不带 `/settings`）|
| `APP_UID` / `APP_GID` | 数据目录的属主，这里用示例值 `10001` |

`.env` 其余项保留模板原样即可：数据库默认就是 SQLite，`WEBHOOK_URL` 与 `WEBHOOK_SECRET`
留空走长轮询，不需要去 Telegram 配 webhook。主密钥要**和数据库一起备份**：换掉它，库里存的
第三方密钥就读不出来了。`.env` 已被 Git 忽略，不要把它提交出去，也不要把令牌写进代码。

### 第二步：启动

```bash
mkdir -p data
sudo chown 10001:10001 data
docker compose up -d --build bot
docker compose logs --tail 50 bot
```

首次构建会拉依赖、编译镜像，等它跑完即可。`docker compose ps` 应当看到 `bot` 处于
healthy 或 up 状态，日志里出现监听地址与启动完成字样就说明起来了。

### 第三步：配 HTTPS 反代

设置中心与入群验证页面由 Telegram 通过 HTTPS 打开，所以要把域名反代到本机 `127.0.0.1:8480`。
这个端口**不需要**对公网开放。

先按[官方文档](https://caddyserver.com/docs/install)装好 Caddy，把域名指向这台服务器并放开
80/443，再把下面内容写进 `/etc/caddy/Caddyfile`（域名换成你自己的）：

```caddyfile
bot.example.com {
    reverse_proxy 127.0.0.1:8480
}
```

```bash
sudo systemctl reload caddy
```

### 第四步：在 Telegram 里完成授权与首次配置

1. 把机器人拉进目标群，设为**群管理员**，至少给「封禁用户」；要置顶、删消息、限制成员就一并给上
2. 在 BotFather 用 `/setprivacy` 关掉隐私模式——不开的话机器人收不到群里普通消息，自动回复和
   逐条审核都无从谈起；这也意味着机器人能读到群里的普通消息，请确认这符合你的使用场景
3. 用**最高管理员**账号**私聊**机器人发 `/settings`，点它回的「打开设置中心」按钮。
   必须从 Telegram 里打开，直接用浏览器访问 `/settings` 只有一个空壳页面
4. 在「模型」页添加 API 供应商：配置名称自取，协议填 `gemini`、`openai`、`deepseek` 之类，
   走第三方中转一般填 `openai_compatible`，再填 API Base URL 与 API Key；
   然后选定主模型并填上模型名。其余角色留空会自动继承主模型，只有一个例外：向量模型留空时
   只继承供应商、不继承模型名，主模型的供应商不支持嵌入接口的话要另填一个支持 embedding 的模型名
5. 在「权限封禁」页授权目标群，可以一次授权多个群
6. 想让群管理员管自己的群，再在这一页把他加为该群的「群管理员」——**Telegram 里的群管理员
   身份不会自动带来后台权限**
7. 在群里 @机器人说句话，或发 `/checkin`，确认能用

设置中心里能看到刚授权的群、「模型」页的主模型也选好了，就算部署成功。

### 常见问题

- 机器人完全不回消息：先确认已在 BotFather 关掉隐私模式，再确认机器人是该群的群管理员，
  最后看 `docker compose logs bot` 里的报错
- 设置中心打不开：确认 `MINIAPP_PUBLIC_BASE_URL` 是 `https://你的域名`（不带 `/settings`），
  反代指向 `127.0.0.1:8480`，且 80/443 端口没有被防火墙挡住
- 签到提醒、周报没发：这两个依赖 cron 调用容器内工具，机器人本身不会自己排程

## 日常维护

- 改运行参数：最高管理员私聊机器人发 `/settings`，见[配置手册](docs/configuration.md)。
  大部分设置保存后下一次动作就生效；界面上标了「需重启」的项要由部署者重启容器
- 改 `.env` 用 `docker compose up -d bot` 重建（`restart` 不会重新读取 `.env`）；
  改代码用 `docker compose up -d --build bot`，只 `git pull` 不会换掉镜像里的代码。
  已有部署升级要沿用 `data/` 现在的属主，别对新目录跑 `chown -R`
- 周报、签到提醒、活跃结算靠你配置的 cron 触发，最低限度加这两行就行（完整示例见进阶参考）：

  ```cron
  0 9 * * 1 cd /opt/Smart_Group_Bot && docker compose exec -T bot python -m bot.tools.weekly_report
  0 9  * * * cd /opt/Smart_Group_Bot && docker compose exec -T bot python -m bot.tools.checkin_reminder --slot 9
  ```

- 功能细则、命令速查、升级与开发说明在 [Fork 进阶参考](docs/fork-reference.md)，
  配置逐字段细节在[配置参考手册](docs/configuration-reference.md)，
  上游原版说明在[上游存档](docs/README.upstream.md)
- 备份：数据库是 SQLite 的 WAL 模式，直接复制文件会拿到不一致快照，要用 SQLite 的在线备份接口；
  连同 `.env` 里的主密钥一起存好
- 升级版本：先按上面备份，`git pull` 后执行 `docker compose up -d --build bot` 重建，
  再看 `docker compose logs --tail 50 bot` 确认起来了；升级不会清空已有配置
- 许可 MIT，边界见[许可说明](docs/licensing.md) 与 [`LICENSE`](LICENSE)；上游 MIT 全文与原版权声明原样保留