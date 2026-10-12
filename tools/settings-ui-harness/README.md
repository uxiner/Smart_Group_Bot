# 设置中心 UI 本地预览 harness

**这是开发工具，不是发布页面的一部分。**

- 生产镜像只 `COPY bot` 和 `COPY prompt`（见 `Dockerfile`），`tools/` 不会进镜像。
- `bot/web/static/` 里没有任何测试模式、演示模式或绕过鉴权的入口。
- 进程只绑定 `127.0.0.1`；传入非 loopback 地址会直接拒绝启动。
- 只读 `fixtures/settings.json`（合成数据），不读 `.env`、不连生产数据库、不请求 Telegram。
- `/settings-assets/*` 直接从 `bot/web/static/` 取真实文件，页面结构由真实的
  `bot/web/static/index.html` 生成（仅把 `telegram.org` 的 SDK 换成 loopback 桩，
  避免产生任何外网请求）。
- `tests/test_settings_night_crystal.py::ReleaseHygieneTests` 会持续检查上面这几条。

## 启动

```bash
python3 tools/settings-ui-harness/harness.py --port 8781
# 打开 http://127.0.0.1:8781/settings
```

可用开关：

| 开关 | 作用 |
| --- | --- |
| `--group-admin` | 以普通群管理员身份进入（`can_manage_global = false`），验证越权时页面只剩群组页 |
| `--fail-save` | 所有写操作返回 503，用于验证保存失败与草稿保留 |
| `--no-groups` | 群组列表返回 503，用于验证错误态 |
| `--conflict-save` | 第一次 `PUT /api/v1/settings` 返回 409（revision 冲突），之后恢复正常 |
| `--replay-fail` | 下一轮回放在执行时以 `failed / run_failed` 结束（验失败态不被冒充成"已完成"） |
| `--replay-saturate` | 下一次登记回放返回 429 `replay_busy`（`error.details.capacity` 带占用） |
| `--restore-conflict` | 下一次 `POST /api/v1/settings/history/{rev}/restore` 返回 409 |
| `--no-change-preview` | `POST /api/v1/settings/diff` 永远回空 diff +「无变化」摘要（验空结果**不是**错误） |
| `--pending-request` | 预置合成的待审批申请（超管审批队列用） |
| `--queue-pages N` | 预置 N 页 ×50 条 pending（默认 1 页），用来验真实分页与截断提示 |
| `--fail-queue-read` | **下一次**队列 GET 回 503（页面首次取数就会命中；自动化想"先正常加载、再下毒"请用下面的端点） |
| `--real-page` | **逐字**提供 `index.html`（保留 `https://telegram.org` 的 SDK 标签），供浏览器检查用请求拦截挡掉 CDN、再注入签名 `window.Telegram` |

另外有几个只用于自动化的端点：

| 端点 | 作用 |
| --- | --- |
| `GET /harness/requests` | 请求日志（含查询串，翻页证据 `?limit=&before=` 就在这里） |
| `GET /harness/last-restore` | 最近一次**真正执行**的回滚响应原样 |
| `POST /harness/reset` | 把内存状态复位回 fixture |
| `POST /harness/fail-next-queue-read` | 让**下一次**队列 GET 回 503。用户已经打好草稿之后再调用它，才能验证"读失败草稿还在" |
| `POST /harness/block-restore/{rev}` / `POST /harness/unblock-restore` | 让某一版的 `restore-preview` 答 400 `history_revision_not_restorable`（消息只含路径） |

harness 是 `HTTP/1.1` keep-alive 的，所以**每个请求体都在路由之前就被抽干**
（`Handler._drain_request_body`）。任何在读完 `Content-Length` 之前就 return 的
分支（403 / 404 / 409 / 503）都会把请求体留在 socket 上：下一条请求会先读到那些
残留字节，把它们当成请求行，于是回一个 `400 Bad request version` 并掐断连接 ——
一个"故意失败"的接口会连带把后面所有读回都弄脏。`--fail-save` 就是最容易触发它的
开关，`checks/replay-history.mjs` 用同一条 keep-alive 连接（`maxSockets:1`，
比对 `localPort`）钉住这条回归。

## 审核回放 + 配置历史路由（合成数据）

这两块页面以前**没有任何路由**，所以只能靠读源码"证明"它们存在。现在
`harness.py` 在内存里模拟了后端合并后的契约（合成数据，见
`replay_fixtures.py`）：

- 回放：`POST /runs` 登记即返回（`queued`/`running`），`GET /runs/{id}` 是轮询
  端点，`POST /runs/{id}/cancel`、`POST /runs/{id}/execute` 都是真的调用，
  `PATCH /samples/{id}/label` 是**唯一**能给样本打金标的入口（机器导入只写
  `provenance.suggested_label`），`fidelity` / `bounds` / `stats` 的形状与后端一致
  （`deadline_seconds = 1800`、`*_confirmed_*`、`*_inconclusive*`、
  `*_error_breakdown`、`labeled_total`）。
- 配置历史：`POST /settings/diff`、`GET /settings/history?limit=&before=`（越界
  400 `invalid_history_page`，**不夹取**）、`GET /settings/history/{rev}/restore-preview`、
  `POST /settings/history/{rev}/restore`。
- 角色矩阵：`--group-admin` 只被授权一个群，全局接口一律 403，回放的
  `prompt` 置空 + `prompt_visible:false`，提交 `candidate.prompt` 403
  `replay_prompt_forbidden`。
- `GET /api/v1/settings` 回的是**带脱敏标记**的文档
  （`<redacted:current>` / `<redacted:url-userinfo>`），把这份文档原样 PUT 回去
  是无损的。

Telegram 桩按官方文档（Bot API 8.0+）的真实字段形状实现：
`safeAreaInset` / `contentSafeAreaInset` 是 `{top, bottom, left, right}` 对象，
`viewportHeight` / `viewportStableHeight` 是数字；`__setInsets` / `__setViewport`
是给 `checks/safe-area.mjs` 用的驱动口（桩在 `tools/` 下，不随发布页出去）。

## 新群接入审批队列 + 按群预算编辑器（合成数据）

这两块页面在上一轮**没有真实入口**：源码里有 renderer、有取数函数，但
`ALL_NAV_ITEMS` 里没有 `onboarding`，`switchTab` 的 `navItems().some(...)` 直接
拒绝 —— 字符串全绿证明不了入口存在。所以 harness 现在也模拟这两块的真实契约：

- 审批队列：`GET /api/v1/onboarding/requests?status=&limit=&offset=` 按
  `total`/`rows` 真实分页（`--queue-pages` 决定几页 ×50），
  `POST /api/v1/onboarding/requests/{id}/approve|reject` 真的改状态；
  重放一条已终结的申请回 `already_decided: true` 且**不复活授权**。
  非超管（`--group-admin`）一律 403 —— 连"有没有 pending"都不泄漏。
- 按群预算：`GET /api/v1/groups/{id}/budget` 本群管理员可读，
  `PUT` 仅超管且走**真实 revision CAS**（过期 revision 回 409
  `group_revision_conflict`，`--fail-save` 回 503）。

## 浏览器验收脚本

`checks/*.mjs` 里的老脚本需要 **Playwright** + 本机 Chrome；本轮新增的
`checks/replay-history.mjs` 用 **puppeteer-core** + 本机 Chrome（从
`PUPPETEER_ROOT` 解析，默认 `/tmp/cdpprobe`；两者都只是驱动，仓库里没有
`node_modules`，也不会进镜像）。

脚本从 `playwright` 解析该包，所以要在装过它的目录里跑（仓库里没有 `node_modules`，
`node_modules/` 也在 `.gitignore` 里）：

```bash
# 任意目录
mkdir -p /tmp/ui-verify && cd /tmp/ui-verify
npm init -y && npm i playwright        # channel: "chrome"，不会额外下载浏览器
CHECKS=<repo>/tools/settings-ui-harness/checks
```

### 审批队列 + 预算编辑器

五个实例各测一种形态：

```bash
python3 tools/settings-ui-harness/harness.py --port 8811 --pending-request --queue-pages 2
python3 tools/settings-ui-harness/harness.py --port 8813 --pending-request --group-admin
python3 tools/settings-ui-harness/harness.py --port 8814 --pending-request
python3 tools/settings-ui-harness/harness.py --port 8815 --pending-request --fail-save
python3 tools/settings-ui-harness/harness.py --port 8816 --budget-seed \
  '{"enabled":true,"currency":"USD","attempt":9,"token":20,"operation_limits":{"tts":3}}'

node $CHECKS/onboarding.mjs    # 真实点导航 / 分页 / 草稿 / 503 / 409 / 角色 / 布局 / 非零额度
```

8816 必须带 `--budget-seed`：只 seed 一份**空**预算是不够的。真实 GET 把限额放在
`budget.limits.*` 里，旧前端从顶层 `budget.daily_attempt_limit` 读——在全 null 的空
快照上恰好"看起来对"，于是"重新读取后额度全空、只改一个字段就把别的额度清掉"这个
真实缺陷一直绿着过去。seed 用真实合同的键名（见 `_budget_document`）；`money_covers`
**不在**回包里，那是合同而不是缺口。

它证明的是**状态机**而不是字符串：真实指针点 `[data-nav="onboarding"]` →
pending 真的渲染成 50 张卡片 → 打字 → 点「刷新」重建 DOM 后草稿仍在 → 切页往返
仍在 → 注入 503 后错误可见、草稿可见、决策按钮被禁用 → 批准后**服务端读回**里
不再有这条 → 批准与拒绝连点只发一次 POST → `--group-admin` 实例里**一个全局队列
请求都没发**。预算那半边同样：`"12abc"` 被拒且**零 PUT**、保存后服务端读回
`revision` 真的 +1、409 保留草稿且不覆盖别人的改动、503 保留草稿、
普通群管理员看不到编辑表单；8816 上还验**真实的嵌套合同**——非零 seed 读回
`limits.daily_attempt_limit=9 / daily_token_limit=20 / operation_limits.tts=3` 都显示在
表单里、只改 token 保存后 9 与 tts 3 **原样保留**（而不是被清成 null / `{}`）、
0 读回仍是 0、金额校验模型那格是文本输入并以数组提交。最后 360/390/1280 三档各验横向
溢出、44px 触控目标与零 JS 异常，截图落在 `SHOTS`（默认 `/tmp/dsh-onboarding-shots`）。

本机实跑 **174 项全过 exit 0**。v68 起这个驱动还多两段：

* **K 段（半坏预算快照 = 读取失败）**：`POST /harness/budget-corrupt {"mode": …}`
  让**下一次**预算读取回一份半坏快照，模式有 `revision_null` / `revision_false` /
  `revision_string` / `token_invalid` / `token_float` / `token_missing` /
  `operations_list` / `enabled_string` / `wrong_group` / `no_limits`。前端必须
  按读取失败处理：状态未知、**0 个可编辑表单**、错误指向"回包不可信"、**0 次
  PUT**、注入只生效一次，随后一次真实读取恢复可编辑。旧实现在
  `revision_null` / `revision_false` / `revision_string` / `token_invalid` /
  `token_float` 上都会放出可保存表单（`Number(null)` 是 0）。
* **L 段（价格目录角色 + 读失败）**：这里只为验"超管能从模型页拿到卡片、普通群
  管理员连请求都不发"。价格目录的**权威**行为（版本 CAS、409、
  "200 不算确认"）在真实宿主驱动上验 —— 内存 harness 里的价格路由只有真实
  形状，不再造第二套语义。

### 真实后端 + 合成库的浏览器驱动（比内存 harness 更硬的一层）

内存 harness 是**假后端**，它自己发明过预算回包形状（顶层 `daily_*`、默认
`currency="USD"`），先后把"重新读取后额度全空、只改一个字段就把别的额度清掉"和
"不涉及金额的额度根本存不下"两个真实缺陷盖了过去。所以本轮另加一层**跑产品自己
代码**的驱动：`real_api_host.py` 起的是真实的 `VerifyWebServer` + 真实 `init_db`
（临时 SQLite），Telegram 走桩不联网，只绑 `127.0.0.1`；播种也走真实
`group_budget.shared_service().save_budget(...)`。

```bash
# 终端 1：真实后端（必须用装了 aiohttp 的解释器；系统 python3 会 ModuleNotFoundError）
PYB=<装了 aiohttp 的 venv>/bin/python
"$PYB" tools/settings-ui-harness/real_api_host.py --port 8821
# 等它打印 READY http://127.0.0.1:8821 …（约 9 秒）

# 终端 2
REAL_API=http://127.0.0.1:8821 node $CHECKS/real-api-budget.mjs
```

宿主只多挂四个**只读**合成端点回**原始行**：
`GET /__synthetic/db/budget`（`group_budgets` + `group_budget_days` 全列，JSON 列显式
解析）、`GET /__synthetic/db/onboarding`、`GET /__synthetic/db/prices`
（`model_price_catalog` 全部版本行，`payload` 显式解析 —— 页面自己读的是模块缓存，
拿它当"写成功"的证据等于再问同一支 API）、`GET /__synthetic/hello`，另有
`POST /__synthetic/reset`（会连 `model_price_catalog` 一起清掉再按 seed 重播，
否则上一臂写出的版本会让下一臂的 `expected_version` 对不上）。
**驱动不信接口自称成功**：每改一次都去读库里的行对账。

本机实跑 **244 项全过 exit 0**（v68 为 186；v69 新增 P 段），抓到的真实缺陷包括 `.toast` 的
`pointer-events: auto` —— 提示条钉在右下角，保存成功后 3.6 秒内正好压住 390px
表单上的"保存/重新读取"，下一次点击被提示条吞掉（**不发请求、无报错、无反馈**），
点开"冲突面板"自然永远等不到。回归在
`tests/test_settings_admin_frontend.py::ToastLayerNeverSwallowsClicksTests`。

v68 又加了两段（都逐条读原始 DB，不看接口自称）：

* **M 段**：10 个半坏预算回包（按真实嵌套 `limits` 形状拼），断言状态未知、
  0 个可编辑表单、0 次 PUT。
* **N 段**：种子目录读回 → 只改一个字段保存后**别的字段原样保留**（库里对账）→
  注入读失败为"未知态"（不画表单、0 PUT）→ 抢跑一次真写入造成 409（两个显式
  选择、**不自动重试**）→ 点"保留我的改动并重基"后**我改的字段上去、我没碰的
  字段用服务器值**（三方合并，断言库里 `input=99` 且 `output=44`）→ 十进制字符串
  原样落库（`"1.50"` 的数值等于 1.5）、`"0"` 是明确免费而不是 `null`、清空是
  `null` → 普通群管理员既看不到卡片也不发价格请求、宿主侧 403。

v69 又加了 **P 段**（价格目录的三方合并生命周期，五组臂，全部对账原始 `payload`）：

* **P1 本地删除不被复活**：我移除唯一一行（草稿 0 行）→ 另一会话改同一行的 output →
  保存遇 409 → 明确选"保留我的改动并重基" → 必须仍是 **0 行**、文案写明
  `你在本地删掉的 …没有被复活`、重基本身 **0 次 PUT** → 我再显式保存 → 库里确实没有
  该行（版本 +1，全程只多 2 次 PUT）。
* **P2 服务器已删的不被复活**：我只改 input → 另一会话清空目录 → 409 → 重基后 **0 行**
  （文案 `服务器已经删掉的 …没有替你加回来`）→ 保存后库仍空。旧实现会把我那行加回来。
* **P3a 正控**：我的新增行 + 别人的新增行重基后都在（界面 2 行、库里 3 行）。
* **P3b 改身份**：把 `model` 改掉 = 删旧 + 增新，重基后只剩新身份一行（旧实现新旧并存）。
* **P4 父的精确复现**：ACK 未回时我把刚提交的 `31` **改回 `1`**（这步旧实现会丢）→
  另一会话改 `55/44` → 我把 output 改成 `66` → 409 → 重基后 `input` 必须是 `1`、
  `output` 是 `66` → 显式保存 → 库里 `1/66`。**P5 是对照**（ACK 期间 `31 → 32`，
  旧实现也过）：用来证明驱动确实能看见"编辑被保留"，P4 的红不是驱动看不见。

> 扣住响应（`holdPutMs`）必须把**所有**相关 PUT 都算进去：早先只匹配
> `/api/v1/groups/<id>/budget`，价格目录的 PUT 其实没被扣住、几十毫秒就回来了，
> 于是"ACK 期间打字"变成打给一个已经重绘过的编辑器 → P4/P5 假红。
> 现在预算与价格两种 PUT 都扣。

> 驱动里的"点"一律走 `clickVisible`：先把目标滚到视口**居中**再做
> `elementFromPoint` 命中测试。`page.click` 会把元素对齐到视口顶端，中心正好落到
> sticky 顶栏底下，事件落在 `HEADER.topbar` 上 —— 表现是"点了没反应、无报错、
> 无请求"，很容易被误判成产品缺陷。命中失败时记一条**失败**并继续（不要抛异常：
> 抛出去会让整轮提前结束，把前面已经失败的断言一起吞掉）。

v73 又加了 `checks/real-api-check.mjs`（真实后端 + 合成库，**33 项**）：申请人点
「检查就绪情况」后页面真的列出诊断条目，而不是只剩一行标题。它按申请人身份
（非超管进页面就是申请人视图，不需要点导航）走四条臂：未选预设用默认集合、
选中 `companion` 按该预设真实功能、翻转机器人权限后诊断必须写"缺少…"而不是"就绪"、
非本群管理员 403 时**一个条目都不渲染**；每一步都回读
`GET /__synthetic/db/onboarding` 对账"只读、没写申请"。为验缺权那条臂，宿主新增
`POST /__synthetic/telegram-permissions`（`{"permissions": {"can_delete_messages": false}}`，
未知键/非布尔值 400；`POST /__synthetic/reset` 会把它复位）。

v74 把 `checks/real-api-check.mjs` 从 33 项扩到 **80 项**，加的是**真实文字几何**
臂（不是对容器矩形或源码字符串断言）：就绪清单原先用 flex 三列，390px 媒体查询把
`.readiness-state` 的 `min-width` 归零，`provider_missing` / `permission_missing`
这类长状态码就**画出自己的盒**压到能力名上（父在 360/390px 视觉确认过）。现在
`.readiness-item` 是 grid（窄屏 ≤480px 状态与能力名各占一行），几何臂对每个
`.readiness-item` 的每个子元素同时取 `getBoundingClientRect()` 与
`Range.selectNodeContents` 的墨迹矩形，逐条要求：墨迹不交叉、墨迹不越出自己的盒、
页面无横向溢出（`scrollWidth <= innerWidth`）。两臂各宽度都跑：申请人 9001
@ 360/390/414/768/1280，超管 42 @ 360/390/1280（超管臂先在页面内点**第一个可见**
的 `[data-nav="onboarding"]` —— 走的是同一份 document 级处理器 —— 再断言
`[data-role="onboarding-queue-panel"]` 存在，证明验的是管理员向导共用同一份诊断
布局）。`permission_missing` 由 `POST /__synthetic/telegram-permissions` 翻转
`can_delete_messages` 真实构造。几何结果写进 `$SHOTS/readiness-geometry.json`。

> `POST /__synthetic/reset` 还顺手清掉**进程级限次表**（`reset_rate_limit()`）：检查
> 端点的限次是内存里按申请人 10 次/60 秒，删合成行清不掉它。本脚本一轮要打 9 次
> check，不清的话**连跑第二轮**从第 10 次起就被 429 拒掉，看上去像产品缺陷。清的是
> 限次表而不是限次逻辑：`tests/test_real_api_host_check.py` 有一条回归先把限次用满
> （第 11 次必须 429 `rate_limited`）再 reset，然后必须又能查。

```bash
# 终端 1：起 harness（四个实例各测一种形态）
cd <repo>
python3 tools/settings-ui-harness/harness.py --port 8781 &
python3 tools/settings-ui-harness/harness.py --port 8792 --fail-save &
python3 tools/settings-ui-harness/harness.py --port 8793 --group-admin &
python3 tools/settings-ui-harness/harness.py --port 8794 --no-groups &
python3 tools/settings-ui-harness/harness.py --port 8795 --conflict-save &

# 终端 2
cd /tmp/ui-verify

# 1) 布局：360 / 390 / 430 / 1280 / 1680 × 全部页面 + 展开群卡片/分组/高级面板
node $CHECKS/verify.mjs

# 2) 交互：草稿、保存成功/失败、折叠态、对话框焦点与 ESC、抽屉焦点陷阱
node $CHECKS/interact.mjs                                   # 26 项
FAIL_HARNESS=http://127.0.0.1:8792 node $CHECKS/interact.mjs # 29 项（多 3 条保存失败路径）

# 3) Telegram 安全区：真实 SDK 形状 + 发事件 + 读计算样式
node $CHECKS/safe-area.mjs

# 4) 文案真伪：热生效承诺、方向指代、原文保留策略
node $CHECKS/copy-truth.mjs

# 5) 对比度 + 角色可见性 + 错误态
node $CHECKS/roles.mjs
GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/roles.mjs
ERROR_HARNESS=http://127.0.0.1:8794 node $CHECKS/roles.mjs

# 6) 截图像素抽样（确认深色主题真的渲染出来了）
SHOTS=/tmp/dsh-ui-shots node $CHECKS/verify.mjs   # verify.mjs 会顺便截图
node $CHECKS/pixels.mjs /tmp/dsh-ui-shots/*.png

# 7) 保存按钮点击回归（改字段 → 真鼠标点击保存）
node $CHECKS/save-click-probe.mjs 8781

# 8) 审核回放 + 配置历史（本轮新增，需要 puppeteer-core + 本机 Chrome）：
#    登记→轮询→完成 / queued 可执行 / 取消 / 429 饱和与幂等重试 / 失败态 /
#    换群不串数据 / 人工金标确认-修正-清除 / 样本导入删除 / 历史真分页 /
#    脱敏标记不被抹掉 / 无变化预览 / 保存 503 保留草稿 / 回滚预览-真回滚-读回 /
#    预览后改配置被挡住 / 回滚 409 保留草稿 / 角色负例 / 360·390·430+桌面无横向溢出
#    另外三处是专门针对"驱动自己制造假象"的，改脚本前先读这段：
#      · 0b) 窄屏抽屉可达 —— 不走 gotoTab，纯**真实指针**（mouse.move/down/up）点
#        **可见**控件，并先用 elementFromPoint 钉住"抽屉关着时导航项在视口外、
#        指针够不着"，360·390·430 各走一遍开关与关闭。gotoTab 自带的 DOM 兜底
#        一旦生效，报告里会单独打出 ⚠ 明说，经过它的断言不算抽屉证据。
#      · 保存前预览必须等**终态**再读：`[data-save-preview]` 在 loading 相位也存在，
#        只等这个属性会读到"正在计算差异"那一帧，错误区当然是空的。
#      · 输入一律**逐键**打（每键 ≥45ms），保存按钮用**真实指针**点。别改回
#        page.type() / ElementHandle.type()：它们按 requestAnimationFrame 给字符
#        让路，而发布页从抽屉切页时正好把 content.focus()（app.js 的无障碍"焦点移
#        进正文"）排在同一条 rAF 队列上，焦点会在打字中途被挪走、输入停在一个前缀
#        （实测会停在 "Markdow"）。那是驱动太快，不是产品截断输入。
#      · **切页之后必须先等页面自己那条 rAF 队列排空，再去碰输入框**
#        （waitForNavigationRafSettled：runs 比点击时多、且 pending===0）。
#        上一条那个 rAF 抢焦点是**每条腿都会发生的**——从抽屉切到 Bot 页后立刻
#        focus() 输入框开始打字，那条 content.focus() 同样会落在打字中途。真人点完
#        导航要抬手、再去点输入框，碰不到下一帧；脚本是毫秒级就上手，于是实测停在
#        "M"（14a 曾经红的就是这一条）。判据是"页面那一帧跑完了没有"，不是"等够
#        多少毫秒"。改脚本前请读 replay-history.mjs 顶部那段注释。
#        修好之后 14a 会**在打字当下**就断言完整的 MarkdownV2（同一个节点、
#        #content 一次没重渲染、焦点还在框上），所以真截断输入的产品不可能再靠
#        "最后读回来对了"蒙混过关。
#    （本脚本用 puppeteer-core 而不是 playwright：不需要外下载浏览器）
python3 tools/settings-ui-harness/harness.py --port 8801 --real-page &
python3 tools/settings-ui-harness/harness.py --port 8803 --group-admin &
python3 tools/settings-ui-harness/harness.py --port 8804 --replay-saturate &
python3 tools/settings-ui-harness/harness.py --port 8805 --replay-fail &
python3 tools/settings-ui-harness/harness.py --port 8806 --fail-save &
python3 tools/settings-ui-harness/harness.py --port 8807 --restore-conflict &
python3 tools/settings-ui-harness/harness.py --port 8808 --no-change-preview &
SHOTS=/tmp/dsh-replay-history-shots \
  node $CHECKS/replay-history.mjs            # 需要 PUPPETEER_ROOT（默认 /tmp/cdpprobe）

# 9) 运营参数页：保存成功 / 503 保留草稿 / 409 冲突可重载 / 群管理员禁全局 /
#    移动端 360·390·430 真实进入该页 / 44px 触达 / 焦点可见 / blur 不吞保存
#    （每项都读回合成 API 的真实落库值与请求日志，不只检查 DOM 里有输入框）
node $CHECKS/operations.mjs
FAIL_HARNESS=http://127.0.0.1:8792 node $CHECKS/operations.mjs
CONFLICT_HARNESS=http://127.0.0.1:8795 node $CHECKS/operations.mjs
GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/operations.mjs
```

`safe-area.mjs` 用桩上的 `__setInsets(safe, content)` / `__setViewport(height, stable)`
驱动**官方真实形状**（`safeAreaInset` / `contentSafeAreaInset` 是
`{top,bottom,left,right}` 对象，`viewportHeight` / `viewportStableHeight` 是数字），
发 `safeAreaChanged` / `contentSafeAreaChanged` / `fullscreenChanged` / `viewportChanged`
事件，然后同时读两处：

- `inline`：`app.js` 实际写进 `--tg-safe-*` 的 px 字符串（逐边取 max 的结果）；
- `probe`：一个 `padding: var(--safe-top) var(--safe-right) …` 的真实元素，
  读回解析后的 px（`getPropertyValue()` 对未注册的自定义属性只会返回未求值的
  `max(…)` 字符串，所以必须走真实元素）。

再逐项验证：非对称 inset 生效、四个消费者（topbar / content 左右 / toast 底部）
真的吃到了值、inset 归零不留残留、viewport 高度跟随、无效高度回落而不塌陷、
旧 SDK（无这些字段）冷启动与热事件都不报错。

`verify.mjs` 会把截图写到 `/tmp/dsh-ui-shots`（可用 `SHOTS=` 改），并把逐条
检查结果写进 `$SHOTS/report.json`（仓库里提交的那份在 `docs/ui-night-crystal/layout-check-report.json`）。

## 重新生成 fixture

`fixtures/settings.json` 已经提交，harness 只用标准库即可运行。
只有在你改了后端默认配置、想刷新基线时才需要重新生成：

```bash
# 需要一个装了项目依赖的 Python 3.12 环境
LITELLM_MODE=PRODUCTION python3 tools/settings-ui-harness/make_fixture.py
```

生成器用项目自己的默认 `RuntimeConfig` 取运行时配置（不加载 `.env`、不连数据库），
群组、名单、群规等行全部是脚本里编出来的合成数据。

## B10 PATCH confirmation fixtures (local only)

Three sibling PATCHes now return `ok:true,status:updated,confirmed:true` plus the
resource document. `POST /harness/patch-fixture {"mode":"normal|unconfirmed|malformed|unavailable"}`
sets the next sibling PATCH fixture, then resets to normal. Missing records are
404, never synthetic creates. Network failures and held ACKs are injected by
the B10 browser driver. This harness remains a local simulator, not production.
