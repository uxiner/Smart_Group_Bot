# Smart Group Bot

大模型驱动的 Telegram 群管理机器人：既能在群里自然回复、调用技能查资料，也能做内容审核、
入群验证、爆破防护和民主投票封禁。Python 项目，可以用 Docker 自行部署。它不是 Telegram
官方机器人，也不提供模型服务——模型要接你自己的供应商。

## 这是什么，和上游什么关系

- 本项目基于 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)
  的**独立维护 fork**，保留上游的基础功能，并在此之上加了这个 fork 自己的定制与修复
- 当前维护者是 GitHub [@uxiner](https://github.com/uxiner)。本仓库维护定制功能与缺陷修复，
  上游的新版本按需评估后再合并，改动经验证才发布
- 本仓库**不承诺**与上游始终同步，也没有企业级保障或服务等级协议
- 遇到这个 fork 自身的问题，请提到[本仓库的 Issues](https://github.com/uxiner/Smart_Group_Bot/issues)，
  不要交给上游处理
- 业务数据存在你自己服务器的项目目录 `data/` 里，第三方密钥经主密钥加密后入库，本项目没有
  自建的云端服务；但为了生成回复和做内容审核，相关消息内容会发送给你选定的模型供应商，
  检索类功能也会访问它对应的第三方站点
- 许可 MIT。上游原作者的版权声明原样保留在 [`LICENSE`](LICENSE)，边界见[许可说明](docs/licensing.md)

## 不想自己部署？

维护者已部署「小爱同学」：[@xatongxue_bot](https://t.me/xatongxue_bot)。如果你不愿自己部署，或在部署过程中遇到困难，可以使用这个现成服务。

请联系 [@uxiner](https://t.me/uxiner)，说明希望使用的群组与所需功能，由维护者为相应群组授权。**仅把机器人拉进群，不会自动开通服务。**

## 相对上游的新特色

| 特色 | 说明 |
| --- | --- |
| 成员积分 | 每日签到、积分榜、积分商店（头衔、置顶求助、抽奖），缺勤不清零 |
| 周活跃激励 | 按每周发言与活跃天数发积分并生成活跃榜；**需要你自己配定时任务**，不会开箱自动发 |
| 审核增强 | 带对话上下文的审核、低置信度判定先质询、申诉复核、成员可举报漏判 |
| 审核证据与人工放行 | 每条命中可发一张证据卡到频道，带「人工放行 / 放行收回」按钮；频道默认不启用 |
| 运营看板 | `/health`、`/modstats`、`/cost` 看命中、误伤率与 token 成本，限管理员查看 |
| 运营参数可配置 | 新增一整页运营参数：积分与奖励、活跃激励、签到提醒、对外文案；多数改完就生效，少数容量项要重启 |
| 个人长期记忆 | 上游只有管理员维护的群组记忆；本 fork 增加自动提炼的个人记忆，成员用 `/memory` 管理 |

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

机器可以按 2 核 4GB 起步，再按群数量和流量往上加；这是起步建议，不是最低门槛。
容器时区默认按中国时间，需要别的时区就在 `.env` 里改 `TZ`。

### 第一步：写好 .env

```bash
git clone https://github.com/uxiner/Smart_Group_Bot.git
cd Smart_Group_Bot
cp .env.example .env
openssl rand -hex 32        # 生成 CONFIG_MASTER_KEY；没有 openssl 就用系统的安全随机数工具生成等长随机串
```

然后编辑 `.env`，至少把下表填上：

| 变量 | 填什么 |
| --- | --- |
| `BOT_TOKEN` | BotFather 给的机器人令牌 |
| `SUPER_ADMIN_ID` | 你自己的 Telegram 数字用户 ID |
| `CONFIG_MASTER_KEY` | 上一步生成的主密钥，生成后不要改 |
| `MINIAPP_PUBLIC_BASE_URL` | HTTPS 源站，如 `https://bot.example.com`（不带 `/settings`）|
| `APP_UID` / `APP_GID` | 数据目录的属主，本教程两项都填示例值 `10001` |

`APP_UID` 与 `APP_GID` 要**持久写进 `.env`**，不要只在命令行临时传一次，否则后面
`docker compose ps`、`logs` 这些命令会因为缺变量直接报错。

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

首次构建要拉依赖、编译镜像，耗时取决于网络与机器，这里不给保证。起来之后先用
`docker compose ps` 看 `bot` 是否 `healthy`，再直接探一次后端接口：

```bash
curl -fsS http://127.0.0.1:8480/healthz
```

能返回就说明后端就绪；部署在服务器上时也可以从外面访问 `https://你的域名/healthz` 做同样检查。

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

1. 把机器人拉进目标群，设为**群管理员**，并授上删消息、限制成员（封禁）、置顶消息这些权限
2. 机器人是群管理员时，保持 BotFather 里的隐私模式开启也能收到普通群消息。只有当某个场景下
   它不是管理员却仍需要读普通消息时，才去 BotFather 用 `/setprivacy` 关掉隐私模式
3. 用**最高管理员**账号**私聊**机器人发 `/settings`，点它回的「打开设置中心」按钮。
   必须从 Telegram 里打开，直接用浏览器访问 `/settings` 只有一个空壳页面
4. 在「模型」页添加 API 供应商：配置名称自取，协议填 `gemini`、`openai`、`deepseek` 之类，
   走第三方中转一般填 `openai_compatible`，再填 API Base URL 与 API Key；
   然后选主模型并填上模型名。供应商和主模型这两项是必填；视觉、决策、审核这类角色留空会沿用
   上级模型，但沿用不等于兼容——视觉角色要主模型支持图片输入。向量角色要单独看：它必须使用
   **支持嵌入接口的供应商和模型**，只换模型名解决不了供应商本身不做嵌入的问题；默认值里的
   `text-embedding-004` 是 Gemini 的嵌入模型，跟你的供应商不兼容时，就在这个角色里单独配
   它的供应商和模型名
5. 在「权限封禁」页授权目标群，可以一次授权多个群
6. 想让群管理员管自己的群，再在这一页把他加为该群的「群管理员」——**Telegram 里的群管理员
   身份不会自动带来后台权限**；获授权的群管理员只管自己那几个群，看不到也改不了全局设置
7. 在已授权的群里 @机器人说句话，或发 `/checkin`，机器人有实际回复，才算真的跑通

积分商店里的自定义头衔还要求 Telegram 侧允许机器人管理成员标签；不满足时这次购买会失败并
把积分退回，商店里会给出提示。

### 常见问题

- 机器人完全不回消息：按顺序排查——① 目标群是否已在「权限封禁」页授权 ② 机器人是否已设为
  群管理员并授上删消息、限制成员、置顶 ③ 设置中心里主模型与 API Key 是否配好
  ④ `docker compose logs --tail 100 bot` 里的报错
- 设置中心打不开：确认 `MINIAPP_PUBLIC_BASE_URL` 是 `https://你的域名`（不带 `/settings`），
  反代指向 `127.0.0.1:8480`，且 80/443 端口没有被防火墙挡住
- 签到提醒、周报没发：这两个依赖 cron 调用容器内工具，机器人本身不会自己排程

## 日常维护

- 改运行参数：最高管理员私聊机器人发 `/settings`，见[配置手册](docs/configuration.md)。
  大部分设置保存后下一次动作就生效；界面上标了「需重启」的项要由部署者重启容器
- 改 `.env` 用 `docker compose up -d bot` **重新创建容器以加载新值**（`restart` 不会重新读取
  `.env`）；改代码用 `docker compose up -d --build bot` **重建镜像并重新创建容器**，
  只 `git pull` 不会换掉镜像里的代码。已有部署升级时不要照新部署去改 `data/` 目录的属主，
  沿用现有属主即可
- 周报、签到提醒、活跃结算要自己用 `crontab -e` 配 cron，按实际项目路径和服务器时区设定，
  示例见 [Fork 进阶参考](docs/fork-reference.md#定时任务需要部署者自己配-cron)
- 功能细则、命令速查、升级与开发说明在 [Fork 进阶参考](docs/fork-reference.md)，
  配置逐字段细节在[配置参考手册](docs/configuration-reference.md)，
  上游原版说明在[上游存档](docs/README.upstream.md)
- 备份：数据库是 SQLite 的 WAL 模式，直接复制文件会拿到不一致快照，要用 SQLite 的在线备份接口；
  连同 `.env` 里的主密钥一起存好
- 升级版本：先按上面备份，`git pull` 后执行 `docker compose up -d --build bot`，
  再看 `docker compose logs --tail 50 bot` 确认起来了；升级不会清空已有配置
- 许可 MIT，边界见[许可说明](docs/licensing.md) 与 [`LICENSE`](LICENSE)；上游 MIT 全文与原版权声明原样保留