# 新群接入与按群成本：公开契约

本文件描述本轮新增的**数据面与预算面**契约。所有默认值都是关闭/未配置，
所有公开值都是中性空值 —— 这里不含任何供应商单价、预算金额、生产群 id 或
密钥。

## 1. 两张用量表不是同一份账

| 表 | 键 | 口径 | 用途 |
| --- | --- | --- | --- |
| `llm_usage_daily`（既有） | (日, 阶段) | **只记成功调用** | 成本看板 / 周报 |
| `llm_cost_daily`（本轮） | 日 × 归属 × 群 × 用途 × 阶段 × **实际** provider/model | 每次真实发出供应商请求即一条 `attempt` | 按群 / 按用途 / 按真实模型的明细 |

两者**相加会重复计数**。任何报表都必须声明读的是哪一张。本轮新增的
`GET /api/v1/groups/{id}/costs` 只读后者，并在响应里带 `source_note` 说明差异。

### 1.1 归属（scope）

`group` / `private` / `private_user` / `system`。

* `kind=group` 必须带一个**负数**群标识。`0` 不是群：它在库里只是让唯一索引
  生效的技术占位，`kind='group'` 的行永远不会等于 0。
* `private_user` 是后台作业的匿名聚合桶，**不含**发起人的 user id —— 否则
  一个群的后台成本会显示在另一个人的私聊名下。
* 推不出归属时进 `system`，不猜一个群。

### 1.2 用途（purpose）与阶段（stage）

`safety` / `optional` / `background_optional`。阶段（`moderation`、`vision`、
`skill`…）是另一层。**用途优先看显式传入**：同一个 `stage='vision'` 既可能是
娱乐看图，也可能是审核取证，按阶段一刀切会误停安全审核。

归属在 update 入口建立时**不确定用途**，推迟到每次 attempt 拿到真实 stage 的
那一刻再判定。

### 1.3 unknown 不是 0

没有 `usage` 的失败/超时落 `usage_unknown=1`token 列写 `NULL`。
填 0 会让「没有计价配置」显示成「这次免费」，预算与报表同时说谎。

## 2. 计价

* 金额用**整数 micro**（百万分之一计价货币单位）。
* 单价单位是「每百万 token 的货币单位」，两个百万在换算里抵消：
  `micro = tokens × price`。
* 缓存按网关归一化口径**只计一次**：`prompt_tokens` 含缓存命中部分，按 input
  价计的是 `prompt − cached`；`cache_write` 单独计（Anthropic 的
  `cache_creation_input_tokens` 不在 prompt 里）；thinking 属 output 内，不再加。
* 缺任何一段单价 → 该部分金额**未知**（`null`），不是 0。
* 不同币种不相加。
* `provider_key` 是**内部配置槽标识**，绝不是 API Key；成本与预算的响应里
  不出现任何 endpoint URL（网关 URL 常带查询串密钥）。
* 价格按**版本**取快照：改价只发新版本，不追改已发生的账。

## 3. 预算护栏

默认 `enabled=false`，且所有额度都未设置 —— 没有配置就没有护栏，也就没有
「看起来在保护但其实没保护」的假绿灯。

* 只管 `optional` 与 `background_optional`。`safety` **永远**绕过可选准入：
  额度用尽、护栏存储故障、日期切换中的任何一种都不许让审核停下来。
* 护栏启用时的核验失败 = **明确拒绝**（fail closed），不偷偷放行。
* 关闭时的快路径**不做每调用一次数据库读取**（启用状态是短 TTL 缓存）。
* 预留发生在网络调用**之前**，且每次 retry / fallback / 工具步**各自**预留。
* 预留用**一条条件 UPDATE** 完成，不做 `SELECT → 判断 → INSERT`。
* 金额预留用**保守上界**（发送前不知道缓存命中，假设全部输入按 input 价），
  结算时换成真实金额。
* 已发出但结果未知 → 保留占用并标 `unknown`，**不退款**。只有发送前取消才退还。
* 过期租约回收标 `unknown` 而不退还 attempt（「没发出去」不可证）。
* 金额 hard cap 只在**计价完整、币种一致**时才允许启用；否则接口直接拒绝，
  不给一个算不出来的「金额上限」。
* 非 LLM（TTS / 媒体 / 搜索）没有可靠账单字段，走**功能操作次数配额**，
  并明确标 `metered=false`（未计量 ≠ 0 元）。

日窗口是 Asia/Shanghai 的自然日，与 `llm_usage_daily`、签到台账同口径。

## 4. 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/v1/onboarding/session` | 已认证 | 申请人的最小入口（不返回群列表/全局配置） |
| POST | `/api/v1/onboarding/check` | 已认证 + 限次 | 只读体检，不授权 |
| POST | `/api/v1/onboarding/requests` | 已认证 + 限次 | 提交 pending，**不授权** |
| GET | `/api/v1/onboarding/requests` | 已认证 | 超管看全部；普通用户只看自己 |
| POST | `/api/v1/onboarding/requests/{id}/cancel` | 申请人 / 超管 | 撤回 pending |
| POST | `/api/v1/onboarding/requests/{id}/approve` | 超管 | 复核身份后一次性开通 |
| POST | `/api/v1/onboarding/requests/{id}/reject` | 超管 | 拒绝是终态 |
| GET | `/api/v1/groups/{id}/readiness` | 本群管理权 | 就绪快照，**零模型调用** |
| POST | `/api/v1/groups/{id}/preset-preview` | 本群管理权 | **零写入** |
| POST | `/api/v1/groups/{id}/preset-apply` | 本群管理权 | 带 revision，走同一份群设置校验 |
| GET | `/api/v1/groups/{id}/costs` | 本群管理权 | 读 `llm_cost_daily`，`days` 1..90 |
| GET | `/api/v1/groups/{id}/budget` | 本群管理权 | 含 `protecting` 布尔 |
| PUT | `/api/v1/groups/{id}/budget` | 超管 | expected revision |
| GET/PUT | `/api/v1/prices` | 超管 | 价格目录（按版本递增） |

### 4.1 申请入口的特别约定

* 认证只认合法 Telegram initData，**不**进入 `privileged_request_scope()`
  （那是超管级资源优先级，不该给提交申请的人）。
* 身份只认 Telegram 现答：`getChat` 类型 + bot 是否在群 + `getChatAdministrators`
  权威名单。超时/异常 = `503 telegram_unverifiable` 并拒绝，**绝不**按本地表
  离线放权。Telegram 确定「没有这个群」是 `404 group_not_found`，与 503 分开。
* 每人每分钟 10 次「检查 + 提交」，**失败也计次**。
* `actor_id` 查询参数不能覆盖会话身份。

## 5. 首次预设

预设**唯一**能写的键是 `mute_all_replies` / `at_reply_mode` / `tts_mode`，
且必须与 `_GroupSettingsUpdate` 支持的键对齐（漂了接口报错而不是静默跳过）。

明确**不在**预设范围内：审核规则/动作/阈值、全局模型、隐私留存、供应商密钥、
付费权益、娱乐 AV、自动对外推送。

三个预设（审核型 / 陪伴型 / 社区运营型）都保留原有安全审核行为；缺配置的能力
标「待准备」而不是假装开启。

## 6. 本轮明确不做

支付/充值、多人共用货币账、自动计价抓取、为不明用量或非 LLM 工具费用编造金额。