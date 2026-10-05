# 私聊自主文字/语音（DM TTS）交付说明

- **分支**：`feat/private-tts-autonomous`
- **基线**：`bc2b4972a631cd18b1383d5308e8883af02273fa`
- **版本**：第三版（第二轮验收 2 个剩余阻断项修正 + 一处文档事实更正；
  首版 `947ffd8` 见 §0.1，第二版 `632f61a` 见 §0.2）
- **解释器**：`/Users/ismoka/Desktop/DSH/Smart_Bot_FullAudit/.venv-d1/bin/python`（Python 3.12.13）
- **未 push、未部署、未改任何 `.env`／生产密钥、未碰工作区外文件**

---

---

## 0.1 第二版返工：父代理独立验收的 5 个阻断项

### 0.2 首版 `947ffd8` 的 5 个阻断项

独立验收用可执行探针复现。本节逐条给出
**复现输入 → 首版行为 → 现在的行为**，全部在本分支修掉。

### 阻断项 1：已投递回执丢失 → 误退款 / 失记忆

父代理的原始探针（第 1 段 `answer_voice` 成功、第 2 段返回 `ok=False`、`send_text` 抛
`RuntimeError`）：

```
首版：RAISED RuntimeError network        ← 明明已经回上话，调用方却看到失败
现在：returned (no raise): delivered=True medium=voice complete=False sent_segments=1
```

**根因**：投递编排里任何一次异常都整段上抛，handler 的 `except Exception` 接住后
`give_the_quota_back('send_failed')` 并跳过历史——而对方明明已经收到了一条语音。

**修法**：

* 新增 `DeliveryReceipt`，**由调用方持有**；每确认一次 Telegram 送达就**立刻**累积一条
  （`_send_voice_segment` / `_send_audio_segment` 在 `await` 成功之后马上 `receipt.add`），
  与后续成败无关、与抛不抛异常无关。
* `_send_reply(message, text, receipt)` 改成**逐段上账**：长回复第 2 段失败时第 1 段已经
  送达，现在这件事对调用方是可见的。
* `deliver_private_reply` 对**普通异常一律不外抛**，只如实回报；`PrivateDeliveryOutcome` 增加
  `complete` 字段。
* handler 新增 `finalize(medium, complete=...)`：**回执非空 = 回上话**（不退配额、落历史），
  回执为空才退款。`complete=True`（每段都送到了）写全文；`complete=False` **只写已送达的
  那部分**，绝不把没播出、连文字兜底也失败的尾巴写成助手说过的话。
* 取消：handler `except asyncio.CancelledError` 先按回执结账（已送达就落历史、不退款），
  **然后重抛**——取消照旧不被吞掉，但也**擦不掉已送达的状态**。
* 合成/文字兜底的普通异常都在编排里收敛成失败结构（见阻断项 5），不再整段上抛。

**行为变更（刻意，已在代码注释与本文档标注）**：首版「取消一律不退款」改为
**按回执判断**——取消前一条都没发出去就退款（本来也该退，否则用户白丢配额），取消前已送达
则不退。这与原 brief「完全没发送仍走原退款；取消不可吞掉」一致。

### 阻断项 2：真实 Telegram 隐私错误未识别

```
is_voice_privacy_rejection("Bad Request: user restricted receiving of voice note messages")
首版：False        现在：True
```

该用户在真机上 `sendVoice` 实际返回过的原文已进白名单（并补了更宽的
`voice note messages` 兜底）。原有的**非隐私排除清单一个没动**（`too many requests`、
`retry after`、`bot was blocked`、`chat not found`、`message to be replied not found` …）。
新增回归：预读 `getChat` 失败 + `sendVoice` 返回该真实错误 → 走**真 MP3** 音频文件路径。

### 阻断项 3：引用/否定解析可误强制；持续指示被静默丢弃

| 输入 | 首版 | 现在 |
|---|---|---|
| `「他说，别发语音，用文字。」这句话是什么意思？` | `text`（错，误强制） | `None` |
| `"别发语音，用文字"这句话是什么意思？` | `text`（错） | `None` |
| `用语音吧，算了随你选` | `voice`（错，撤销无效） | `None` |
| `用 文字 回复` | `None`（错） | `text` |
| `需要文字` | `None`（首版碰巧对，但 `需` 是 typo） | `None`（typo 已删） |

**解析修法**（顺序本身就是语义）：**先挖引号**（`「」『』""''` 六种，成对挖掉、未闭合从开口
删到句尾）**再去空白**（中文指令里的空格没有语义）**最后才按标点切句**——首版先切句，
把引号里的逗号当成了句界，直接丢掉引号状态。撤销/自主选择的优先级提到否定与肯定之上。

**持续指示不再被静默降级**：这是首版最该改的一条。

* 持续词（`以后|今后|从今以后|往后|之后|长期|一直|从此|永久`）在句中出现 → 该句是**持续**
  指示；不出现 → **仅本轮**。
* 持续状态由 `load_owner_delivery_state(session, user_id)` 从**现有私聊历史**折叠出来，
  **不新增任何表、任何全局配置、不加每轮 LLM 调用、不改模型**。
* 折叠只扫 `private_chat_messages` 里 `role='user'` 且 `user_id` 是他本人的行——那是可信来源
  （他自己在私聊里打的字）。群公开资料、检索留档**根本不在这张表里**（方向本来就不允许
  「群 → 私聊」的写入）；助手自己说过的话被 `role='assistant'` 排除。图片描述是我们生成的，
  会被 `[图片内容]` 切掉，不算他的原话。
* 正序折叠、后写覆盖先写 → **最新一次有效修改/解除优先**；一次性指示不参与折叠。
* 解除：`以后都不用语音了` / `取消固定媒介` / `恢复原样`（解除动词 + 持续或「固/原来」范围）。
* `resolve_delivery_directive(text, persistent, is_super)` 是唯一的合并入口：
  `is_super=False` **直接返回空**（正文自称超管不产生任何效力）；
  优先级 `release > autonomy > 本轮 one_shot > 历史 persistent`。
* `release_persistent` 清空持续状态但**本轮的指示仍生效**（`以后都不用语音了，用文字吧`
  → 本轮文字，之后自主）。

### 阻断项 4：畸形/重复控制信封会泄露或被朗读

| 输入 | 首版 | 现在 |
|---|---|---|
| `[[DM_DELIVERY: voice]
你好` | 未剥离，`malformed=False`，正文含控制壳 | `text`/`你好`/`malformed=True` |
| `[[DM_DELIVERY: voice]]
[[DM_DELIVERY: text]]
你好` | `voice`，正文含第二个信封（会被朗读、落库） | `text`/`你好`/`malformed=True` |

**修法**：从「一条正则匹配整条回复」改成**控制区**语义——从开头连续吃掉所有「壳行」，
剩下的才是正文；壳行超过一行 / 各行声明冲突 / 形态畸形，一律**退回文字**（安全落地）。
「壳后面还跟着正文」的行**不算壳**（`[[DM_DELIVERY: voice]] 是啥意思？`、代码块里提到它、
正文中间出现）→ 全部原样保留，不会为了消毒删掉用户/模型需要讨论的文本。
没有正文就返回空正文，**绝不假造正文**。

### 阻断项 5：合成异常没有承诺的文字兜底

`_synthesize_segment` 现在把**普通异常**收敛成失败结构（`synthesis_exception:<Type>`），
由编排照常走「未送达部分 → 正文文字兜底」；`CancelledError` 照原样透传（取消不能被吞成
「合成失败」，否则会发出一条用户根本没要求的兜底文字）。与阻断项 1 的回执统一处理。

### 0.3 第三轮返工：`632f61a` 独立验收的 2 个剩余阻断项 + 1 处事实更正

#### 阻断项 A：最新的持续指示本轮被旧历史压过

父代理探针：

```
resolve_delivery_directive(text='以后都用文字回复', persistent='voice', is_super=True)
第二版：('voice', 'history')      ← 刚下的新指示被旧历史压过去，要等下一轮才生效
第三版：('text', 'turn')
```

**根因**：`resolve_delivery_directive` 只检查了 `turn.one_shot`，**没有检查
`turn.persistent`**——本轮新下的持续指示根本没进本轮的判定，于是历史里的旧值赢了。

**修法**：合并顺序改成
`release > autonomy > 本轮 one_shot > **本轮 persistent** > 历史 persistent > 无`。
本轮新下的持续指示**立即生效**；同轮既给了一次性又给了持续时，一次性管这一轮、持续管以后
（`以后都用语音，这次用文字` → 本轮文字）。

#### 阻断项 A（附）：同句里被自己撤销掉的新指示仍被写进持续态

```
fold_owner_delivery_state(['以后都用语音，算了随你选'])
第二版：voice      ← 前半句已被后半句撤销，却还是落了状态
第三版：''
```

**修法**：`fold_owner_delivery_state` 里，`autonomy`（随你选）的那一轮**不写任何状态**。

**两类撤销的语义保持明确区分**：

* `autonomy`（「算了随你选」）只让**那一轮**不写任何状态，**既有的旧持续态原样保留**——
  下次没说别的还是照旧。它是「这次交给你」，不是「取消固定」。
* `release_persistent`（「以后都不用语音了」「取消固定媒介」）才**清空**旧持续态。

#### 阻断项 B：提问 / 条件 / 转述仍会误升为硬规则

```
parse_owner_delivery_instruction('用语音吗？')
第二版：voice  →  resolve 给 ('voice', 'turn')     ← 把提问当成了下达命令
第三版：None   →  resolve 给 ('', '')              ← 不确定就交回自主
```

**修法**：问句判据只有一条——**疑问词贴不贴媒介词**，而不是「有没有问号」：

| 输入 | 疑问词位置 | 判定 |
|---|---|---|
| `用语音吗？` / `改成语音行吗？` / `语音好不好` | 贴媒介词 | 问句 → 不作数 |
| `用语音吧？` | `吧` 紧贴媒介词**且句尾有问号** | 问句 → 不作数 |
| `这次用语音说给我听好吗`（含带 `？` 的写法） | 不贴（中间隔了「说给我听」） | **真指示** → 作数 |
| `用语音吧，拜托了` | `吧` 贴媒介词但**句尾没问号** | **真指示** → 作数 |

条件句（`如果/假如/要是/万一/…会怎么样`）与转述句（`他说…`）一律跳过。

实现上有一个坑：切句符会把 `？` 一起吃掉，`用语音吧？` 切完只剩 `用语音吧`，光看正文
分不出问句还是命令。所以 `_clauses()` 现在返回 `(小句, 是否以问号收尾)`。

**总原则：不给不确定文本硬规则权**——判不准就退回模型自主，这本来就是私聊的默认行为。

#### 文档事实更正（不改方案、不改实现）

此前 DELIVERY.md 写「通道不支持 function calling」，这是**错的**：那只是当时对私聊裸
tools 的一次探测，不能推广到全链路——群聊的 `answer_with_skill` 工具路径一直正常工作。
已更正为：「私聊当前走 plain chat 路径（`answer_with_search` → `llm.chat`），沿用该路径用
一次回复信封，避免增加工具循环 / 额外分类调用」。**一次 LLM 调用 + 一个信封的实现保持不变**，
群链路、模型、供应商均未改动。

---

## 1. 做了什么（一句话）

私聊现在和群聊一样，**由机器人自己**决定这一条发文字还是发语音；最高管理员本轮的明确指示按
**真实鉴权结果**优先；语音条被 Telegram 明确因语音隐私拒收时，私聊局部降级成**真 MP3 音频文件**；
任何环节失败都回正文文字并如实上报，绝不谎称语音已发。

群聊侧**一个字都没改**：`bot/handlers/group.py`、`bot/services/doubao_tts.py`、群配置、
审核链路、`tts_mode` 语义全部保持原样。

---

## 2. 改动文件

| 文件 | 性质 | 行数 |
|---|---|---|
| `bot/services/private_tts.py` | **新增**（第三版微调） | 1191 |
| `tests/test_private_chat_tts.py` | **新增**（第三版扩充） | 2190 |
| `bot/handlers/private_chat.py` | 改动 | +138 / −30 |
| `bot/services/private_chat.py` | 改动 | +11 / −0 |

第三轮相对第二版的改动：

```
 DELIVERY.md                    | 124 ++++++++++++++++++++----
 bot/handlers/private_chat.py   |   4 +-
 bot/services/private_tts.py    | 104 ++++++++++++++++++++++--
 tests/test_private_chat_tts.py | 300 +++++++++++++++++++++++++++++++++++++++++
 4 files changed, 491 insertions(+), 41 deletions(-)
```

**群侧字节不变**（对 `632f61a` 逐文件核对）：

```
UNCHANGED  bot/handlers/group.py
UNCHANGED  bot/services/doubao_tts.py
UNCHANGED  bot/services/skills/doubao_tts.py
UNCHANGED  bot/handlers/admin.py
UNCHANGED  bot/services/skills/service.py
UNCHANGED  bot/services/dm_search.py
```

第二版新增了 `bot/services/private_tts.py` 对 `bot.db.models.PrivateChatMessage` 的**只读**
导入（折叠持续指示要读历史），没有引入任何群表，也没有写入路径。

零新增配置项、零库表迁移、零依赖升级、零生产参数变更。

### 2.1 `bot/handlers/private_chat.py`

- 新增 `_tts_service(settings)`：按**全局** `DoubaoTTSService(settings).available` 决定这一轮能不能
  用语音；构造失败/没配/全局关掉一律 `None`（纯文字）。私聊**不引入自己的开关**，所以
  群里的 `tts_mode` 与全局 `doubao_tts_enabled` 语义完全不受影响。
- `owner_directive = parse_owner_delivery_instruction(text) if verdict.is_super else ""`
  ——**只在真实鉴权结果是超管时才解析**。正文里自称「我是最高管理员」连解析都不进。
- 把 `tts_preference` 传给 `build_private_chat_messages(...)`。
- `answer_with_search` 之后：解析投递信封 → 合并 owner 指示 → 交给
  `private_tts.deliver_private_reply(...)` 投递 → 只有 `delivered=True` 才算「回上话」，
  否则按原语义 `give_the_quota_back(...)`。
- 历史（内存 + `private_chat_messages`）落的是**真正给用户看到/听到的那段正文**。
- 最终日志加了 `媒介=`；新增 `private chat: 投递选择 | … | 依据=model|owner | 信封畸形=…` 一行。

### 2.2 `bot/services/private_chat.py`

- `build_private_chat_messages(...)` 新增 `tts_preference: str = ""` 参数；非空时追加成一段
  system 块。**不传就是空串、一个字节都不注入**（既有调用方与既有行为逐字不变）。

### 2.3 `bot/services/private_tts.py`（新模块）

| 成员 | 职责 |
|---|---|
| `parse_dm_delivery(raw)` | 拆出 `(delivery, text, malformed)` |
| `parse_owner_delivery_instruction(text)` | 纯解析媒介指示，**不认人**（权限由 handler 的真实鉴权给） |
| `build_private_tts_preference(...)` | 给模型的能力/选择/信封说明 + owner 指示块 |
| `is_voice_privacy_rejection(detail)` | 只认「语音隐私」白名单措辞，先排掉限流/封禁/网络 |
| `VoiceRestrictionCache` / `chat_restricts_voice_messages(...)` | 主动降级：先读会话的语音限制 |
| `deliver_private_reply(...)` | 投递编排 + 如实回报 `PrivateDeliveryOutcome` |
| `_synthesize_segment(...)` | 语音走 `synthesize_voice_payload`（与群聊同一条）；音频走 `synthesize(audio_format="mp3")` |

---

## 3. 关键设计与取舍

### 3.1 为什么用「同一次主回复里的输出信封」

**事实口径（更正）**：私聊当前走的是 **plain chat** 路径——`dm_search.answer_with_search`
→ `llm.chat`，一次调用出正文。群聊那条 `answer_with_skill` 路径是走工具的，两条路径本来
就不一样。此前本文件里「主通道不支持 function calling」的绝对说法**是错的**：那只是当时对
私聊裸 tools 的一次探测，不能推广到全链路——群聊的工具路径一直正常工作。

在这个口径下，选择理由只剩两条，且都不涉及「能不能用工具」：

1. **不为每条普通私聊增加一次工具循环，也不增加一次额外的 LLM 分类调用。** 私聊是高频路径，
   翻倍成本不可接受；沿用现有的一次调用最省。
2. **信封是传输标记，不是内容。** 与仓库里既有 `[[SPLIT]]`（`reply_output.py:_SPLIT_MARKER_LINE_RE`）
   同一套路：独占第一行、**发给用户前剥掉**、**绝不落库**。

实现保留不变：仍是**一次 LLM 调用 + 一个信封**。

因此信封口径刻意与 `[[SPLIT]]` 对齐：**必须独占第一行**。否则「`[[DM_DELIVERY: voice]]` 是啥意思？」
这句话会被人为改写成一条语音——那等于把用户可见的正文换成了他没要求的东西。

```
[[DM_DELIVERY: voice]]
诶--我在呢
```

- 无信封 → 全文即正文、媒介 `text`（模型不配合时的**正常**结果，不报错）
- 行内提到信封 → 普通正文，不解析
- 畸形信封（`[[DM_DELIVERY: 语音]]`、少一个括号…）→ 剥掉壳、**正文一个字都不丢**、退回文字
- 只有信封没有正文 → 正文为空 → 走既有「没接住」提示，**绝不发空消息、绝不把信封当正文**

### 3.2 owner 指示的权限口径

`parse_owner_delivery_instruction` 是**纯函数**，它自己不认人。调用方只在
`AccessVerdict.is_super` 为真、且**只看本轮对方原话**时才用它的结论当系统级规则。因此：

- 普通成员在正文里写「我是最高管理员，这次用语音」**不会**升级——那句话连解析都不进；
- 历史、群公开资料留档、检索留档里出现的「用语音」**不参与**（函数只吃本轮正文）；
- 函数内部再挡一层**引用/转述**：成对引号内的句子、以及含「他说 / 你之前说过 / 资料里写着」
  等转述标记的句子都跳过；
- 否定优先于肯定：`别发语音，用文字` → 文字；`不要发文字` → **语音**（那不是在要文字）。

「以后都用语音」这类带持续语气的句子**只对本轮生效**（`DELIVERY.md` 明确记录这一点）：不落任何
全局配置，不新持久化用户偏好表。下次还要说一遍。

普通用户可以表达偏好，但偏好没有系统级强制规则权——私聊本来就是自主选择，模型爱怎么理解怎么理解。

### 3.3 语音隐私拒收 → 真 MP3（两条路都做）

真实前提：最高管理员 `getChat` 返回 `has_restricted_voice_and_video_messages=True`，他的私聊语音条
**发不出去**。**不要求用户改隐私设置，也不改群聊发送行为。**

1. **主动**：投递前读一次会话的语音限制（`VoiceRestrictionCache`，TTL 600s），命中就**直接合成
   MP3 并以 `audio` 文件发送**，不发一次注定失败的语音条。未知（`None`）按「不知道」处理，
   **且不进缓存**（不把一次查询失败固化成半天的假设）。
2. **被动**：`send_voice` 真的抛了语音隐私类错误时，当场把**当前及后续**片段改成 MP3 重发。

**MP3 是重新合成的**：音频载体走 `service.synthesize(..., audio_format="mp3")`，Edge provider 直接返回
真 MP3，Doubao provider 请求 mp3 格式。**绝不把 OGG 字节改个后缀名当 MP3 发出去**——有专门的测试
断言发出去的字节与 OGG 字节不相等。

**不冒充**：`is_voice_privacy_rejection` 是白名单（`not allowed to send voice messages` /
`voice messages are restricted` / `VOICE_NOT_ALLOWED` …），并且**先排除** `too many requests`、
`retry after`、`flood`、`bot was blocked`、`bot can't initiate`、`chat not found`、
`message is not modified`、`message to be replied not found` 等。宁可漏判（多走一次文字兜底），
也不误判（把一次普通故障说成隐私拒收，再白合成一遍 MP3）。

`TelegramForbiddenError` 与 `TelegramBadRequest` 在 aiogram 里是**兄弟类**（不是子类），
两者都显式捕获，不让 Forbidden 掉进通用重试分支。

### 3.4 配额与取消的语义

- 「发生可见回复才计配额」：`delivered=True` 才写历史、才不退款。
- 「完全没发送仍走原退款」：`delivered=False` → 原有 `give_the_quota_back("delivery_failed")`。
- 「取消不可吞掉」：`asyncio.CancelledError` 在 3.12 是 `BaseException` 而非 `Exception`，
  投递层每条 `except Exception` 都不会吞它，handler 的 `except Exception` 同理；合成里抛出的
  `CancelledError` 直接透传，**不触发文字兜底、不退款**（有专门测试断言）。
- 部分投递（第 2 段挂掉）已经算「回上话」（配额不退），但**只补发没发出去的那几段**，
  已经播出的绝不重播。

### 3.5 不阻塞、不加锁

- 合成走 `DoubaoTTSService` 自带的并发闸门（合成 3 / 转码 2，各带 2s 有界等待）。
- 私聊侧再加**自己的**准入闸门（容量 2、2s 有界等待）：私聊连发吃不满群聊那边的合成额度，
  等不到就直接让位（回文字），**不给群审核留下跨用户持锁**，也不提高自己的执行优先级
  （私聊 update 本身就是 `NORMAL`，不进 `privileged_request_scope`）。
- 只在真的要发语音时才打 `getChat`，且有 TTL 缓存，不会每人每条都多打一次 API。

### 3.6 单条回复只一种媒介

同一回复默认**一种**主要媒介：完整语音投递后不再补一份文字。只有**失败兜底/部分投递**才补文字
（此时补的也只是没播出去的那几段）。长文遵守现有分段与资源上限：段数超过
`MAX_PRIVATE_TTS_SEGMENTS = 6`（与群聊 `_TTS_MAX_SEGMENTS_PER_MESSAGE` 同口径）直接走文字，
**不会无限合成**。

### 3.7 隔离

- 信封、控制元数据、提示词、合成错误**都不落私聊历史**（落库的只有真正可见/可听那段正文）。
- **完整送达**写全文（保留 markdown 等只在屏幕上好看的东西）；**部分送达只写已送达的那部分**。
- 私聊正文**不进入**群归档 / 群记忆 / 长期记忆的任何写入路径——本特性没有任何一条写入群表的代码。
- 有源码级护栏测试（`StructuralIsolationTests`）：`group.py` 不得出现 `private_tts` /
  `DM_DELIVERY`；`doubao_tts.py` 不得出现私聊相关符号；`private_tts.py` 不得触碰群表/群加载器。

---

## 4. 测试（TDD，真跑）

### 4.1 基线复现（先失败，再实现）

两层都做了，如实记录原始输出：

**第一层——纯净基线（实现文件全部撤走，只留新测试）**

```
$ python -m pytest tests/test_private_chat_tts.py -q -p no:randomly
ERROR: ImportError: cannot import name 'private_tts' from 'bot.services'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 4.13s
=== pytest exit: 2 ===
```

**第二层——只有新模块、handler 仍是基线**（更能说明「行为没接线」）：

```
$ python -m pytest tests/test_private_chat_tts.py -q -p no:randomly
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_model_chosen_voice_reaches_telegram_as_a_voice_note
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_owner_instruction_overrides_the_model_choice
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_owner_can_also_force_voice_when_the_model_chose_text
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_ordinary_member_claiming_to_be_the_owner_gains_no_power
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_quoted_owner_text_is_not_an_instruction
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_owner_delegating_leaves_the_decision_to_the_model
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_envelope_is_never_shown_to_the_user
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_envelope_is_never_stored_in_private_history
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_tts_off_means_no_medium_prompt_is_injected
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_tts_available_teaches_the_model_it_can_send_voice
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_quota_is_kept_when_voice_was_audible
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_quota_is_refunded_when_nothing_was_delivered
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_partial_voice_delivery_still_counts_as_answered
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_cancellation_is_not_converted_into_a_quota_refund
FAILED tests/test_private_chat_tts.py::HandlerDeliveryTests::test_search_still_runs_with_tts_on
FAILED tests/test_private_chat_tts.py::PreferenceBlockTests::test_build_messages_only_injects_when_asked
FAILED tests/test_private_chat_tts.py::PreferenceBlockTests::test_member_text_never_reaches_the_system_role
FAILED tests/test_private_chat_tts.py::HistoryPersistenceTests::test_voice_turn_persists_only_the_spoken_body
FAILED tests/test_private_chat_tts.py::HistoryPersistenceTests::test_malformed_envelope_is_not_persisted_either
FAILED tests/test_private_chat_tts.py::HistoryPersistenceTests::test_a_failed_voice_turn_still_persists_the_text_body
FAILED tests/test_private_chat_tts.py::HistoryPersistenceTests::test_private_turn_never_leaks_into_a_group_archive
22 failed, 46 passed, 41 subtests passed in 9.01s
=== pytest exit: 1 ===
```

> 失败形态说明：handler 层的用例以 `AttributeError: _tts_service` 形式失败（基线根本没有这个接线），
> 不是断言失败。这是真实且诚实的基线形态。

**实现后**（见 §4.2）：`68 passed`，退出码 0。

### 4.2 实现后：新增测试 + 定向回归（全部真实执行）

```
$ python -m pytest tests/test_private_chat_tts.py tests/test_private_chat.py \
    tests/test_private_chat_history.py tests/test_dm_search.py -q -p no:randomly
183 passed, 63 warnings, 61 subtests passed in 48.64s
=== exit 0 ===
```

TTS / 隐私 / 路由 / 输出协议 相邻回归：

```
$ python -m pytest tests/test_tts.py tests/test_p3_f022_edge_tts_and_provider.py \
    tests/test_speech_style.py tests/test_context_privacy.py tests/test_find_archive_privacy.py \
    tests/test_long_term_memory_privacy.py tests/test_router_route_integrity.py \
    tests/test_reply_output.py tests/test_context_gate.py tests/test_group_reply_targets.py \
    tests/test_private_chat_tts.py -q -p no:randomly
204 passed, 52 warnings, 58 subtests passed in 39.65s
=== exit 0 ===
```

提示词 / Telegram / 入口 / 优先级相邻回归：

```
$ python -m pytest tests/test_runtime_prompt_blocks.py tests/test_runtime_prompt_contexts.py \
    tests/test_prompt_retirement_migration.py tests/test_telegram_send.py \
    tests/test_update_delivery.py tests/test_context_reserve_safety.py \
    tests/test_transaction_boundaries.py tests/test_message_entrypoint.py tests/test_loader.py \
    tests/test_request_priority.py tests/test_resource_health.py -q -p no:randomly
173 passed, 26 subtests passed in 16.91s
=== exit 0 ===
```

群 / 配置 / 投递进度 相邻回归：

```
$ python -m pytest tests/test_group_reply_targets.py tests/test_group_context_budget.py \
    tests/test_group_channel_sender.py tests/test_group_unavailable_notice.py \
    tests/test_skill_service.py tests/test_runtime_config.py tests/test_configurable_budgets.py \
    tests/test_deployment_config.py tests/test_p4_config_surface_docs.py \
    tests/test_reply_progress.py tests/test_pending_reply_debounce.py \
    tests/test_telegram_task_lifecycle.py -q -p no:randomly
231 passed, 82 subtests passed in 57.64s
=== exit 0 ===
```

按用户口径**没有**每次跑全量。

### 4.3 必测项覆盖对照

| 要求 | 用例 |
|---|---|
| 模型自主 voice | `test_model_chosen_voice_reaches_telegram_as_a_voice_note`、`VoiceDeliveryTests::test_model_can_choose_voice` |
| 模型自主 text | `test_model_chosen_text_stays_text`、`test_model_can_choose_text` |
| 超管文字/语音优先 | `test_owner_instruction_overrides_the_model_choice`、`test_owner_can_also_force_voice_when_the_model_chose_text` |
| 普通成员自称超管无权限升级 | `test_ordinary_member_claiming_to_be_the_owner_gains_no_power` |
| 引用/历史资料不构成指示 | `test_quoted_owner_text_is_not_an_instruction`、`OwnerDirectiveTests::test_quoted_or_reported_speech_is_not_an_instruction`、`test_delegating_back_yields_no_rule` |
| TTS 不可用 | `test_tts_off_means_no_medium_prompt_is_injected`、`UnavailableTests::*` |
| 语音隐私拒收 → 真 MP3 | `test_restricted_chat_sends_a_real_mp3_audio_file`、`test_live_rejection_switches_the_rest_to_a_real_mp3`、`test_mp3_is_not_an_ogg_renamed` |
| 非隐私失败走文字兜底 | `test_voice_send_failure_falls_back_to_text`、`test_forbidden_does_not_turn_into_an_audio_file`、`VoicePrivacyDetectionTests::test_unrelated_failures_never_pose_as_voice_privacy` |
| 完整/部分投递 | `test_complete_multi_segment_delivery_sends_no_text`、`test_only_the_unsent_segments_are_repeated_as_text`、`test_first_segment_failure_falls_back_to_the_whole_body` |
| 配额 | `test_quota_is_kept_when_voice_was_audible`、`test_quota_is_refunded_when_nothing_was_delivered`、`test_partial_voice_delivery_still_counts_as_answered` |
| 取消不被吞掉 | `test_cancellation_is_not_converted_into_a_quota_refund`、`test_cancellation_is_never_swallowed_by_the_text_fallback`、`test_cancellation_during_synthesis_propagates` |
| 内部格式不展示不落库 | `test_envelope_is_never_shown_to_the_user`、`test_envelope_is_never_stored_in_private_history`、`HistoryPersistenceTests::*` |
| 搜索仍工作 | `test_search_still_runs_with_tts_on` |
| 私聊历史隔离 | `test_private_turn_never_leaks_into_a_group_archive`、`test_member_text_never_reaches_the_system_role` |
| 群聊行为不变 | `StructuralIsolationTests::*`（`group.py` 无 `private_tts`/`DM_DELIVERY`；`doubao_tts.py` 无私聊符号；`private_tts.py` 不碰群表）+ 群侧相邻回归全绿 |
| 畸形格式稳健 | `DeliveryEnvelopeTests::test_malformed_marker_keeps_the_body_and_falls_back_to_text` 等 |
| 不无限合成 | `test_too_many_segments_never_synthesizes_without_limit` |

未跳过任何有效断言，没有 `xfail`，没有删除任何断言。

### 4.4 原始摘要 / 退出码（第三版最终结果）

| 测试组 | 数量 | 退出码 |
|---|---|---|
| 新增 `tests/test_private_chat_tts.py`（第三版） | **140 passed** | 0 |
| 私聊 handler / 历史 / dm_search + TTS + 隐私 + 路由 + 输出协议 + speech_style | 215 passed | 0 |
| 长期记忆隐私 / 归档隐私 / 上下文闸门 / 群回复目标 / 群上下文预算 / 提示词块 ×3 / Telegram 发送 / update 投递 / 事务边界 / 消息入口 / 请求优先级 / 资源健康 / 可配预算 / 上下文预留安全 | 259 passed | 0 |

（第二版的 113 例全部仍在，无一删除、无一 `xfail`、无一弱化断言。）

第三轮新增断言（父代理两轮要求逐条对应）：

| 要求 | 用例 |
|---|---|
| 最新持续指示本轮生效，不被旧历史压过 | `LatestStandingOrderTests::*`（8 个，含父代理原始探针、无历史、反向覆盖、一次性优先、最新撤销） |
| 跨真实 handler + 真临时库持久化 | `LatestStandingOrderHandlerTests::*`（5 个，**真临时库**）：新持续态本轮生效、写入历史后下一轮无需重复、旧历史兜底、问句不强制、被撤销的持续态不留给下一轮 |
| 同句内撤销不写状态；autonomy 与 release 区分 | `RevokedOrderFoldingTests::*`（5 个） |
| 提问 / 条件 / 转述不误升硬规则；礼貌指示仍是真指示 | `QuestionIsNotAnInstructionTests::*`（10 个，含父代理原始探针、`吗/行吗/好不好/吧？` 变体、条件句、以及不贴媒介词的礼貌指示） |

## 5. 失败 / 未验证内容（如实报告）

### 5.1 明确**没有**验证的

1. **没有连生产、没有发任何真实 Telegram 测试消息、没有用真实用户/群消息**。全部用合成
   `Bot`/`Message` + 临时 SQLite（`tempfile.mkstemp` + `init_db`），音频字节是字面量
   `b"OGG-OPUS-BYTES"` / `b"ID3-MP3-BYTES"`。真实供应商 + Telegram 生产兼容探针留给父代理。
2. **没有在 Linux 生产依赖环境里跑过。** 本机是 macOS；`edge-tts`、`ffmpeg` 转码路径
   （mp3 → ogg_opus）与真实网络端点都未实测。语音载体走的是与群聊**同一条**已验证路径，
   但私聊侧的新调用点（`answer_voice` / `answer_audio` 的参数形状）只有替身验证过。
3. **没有跑全量测试**（用户明确口径）。全量回归留给父代理按需执行。
4. **没有验证 Edge TTS 的 mp3 分支在生产音色 `zh-CN-XiaoyiNeural` 下的真实输出**；
   代码路径是 `synthesize(audio_format="mp3")`，与群聊 `synthesize_voice_payload` 内部
   已经用到的那条 mp3 请求完全一致。
5. **真实隐私错误文案**已按父代理提供的真机原文收录（`restricted receiving of voice note
   messages`），但**没有自己连 Telegram 复现过**。白名单是白名单，其它未收录的措辞仍然
   **漏判 → 走文字兜底**，绝不会把普通故障当成隐私拒收。父代理的生产兼容探针里建议顺手
   再确认一次。
6. **`getChat` 的 `has_restricted_voice_and_video_messages` 字段**在本机只有替身验证；
   真机取值由父代理实测（任务书里给的是 `True`）。预读失败时**不进缓存**，让被动的拒收改投
   兜底正常接手（真机上正是这条路径）。

### 5.3 第二版引入的已知限制

- 持续指示的识别靠**持续词**。「你以后都这样说话吧」这类**不含媒介词**的句子不会改变媒介
  状态——只认明确的「文字/语音」表述，不猜。这是刻意保守：猜错会静默改掉一个人的回复方式。
- 持续状态的折叠窗口是最近 200 条**他本人**的用户行（`load_owner_delivery_state(limit=200)`）。
  超长历史里最老的设定会被窗口淘汰，但**最新的修改/解除一定在里面**。
- 持续指示**只对最高管理员生效**。普通成员的偏好仍然没有系统级效力（本 brief 的明确要求）。

### 5.2 已知的取舍（不是缺陷，但父代理应知情）

- ~~「以后都用语音」只对本轮生效~~ —— **第二版已修正**：带持续词（`以后/今后/从今以后/往后/
  之后/长期/一直/从此/永久`）的句子是**持续指示**，状态由**现有私聊历史**折叠出来
  （`load_owner_delivery_state`），最新一次有效修改或解除优先。**没有**新增任何表、任何全局
  配置、任何每轮 LLM 调用。
- 持续状态每次从历史重新折叠（默认最近 200 条**他本人**的用户行），所以极久远的历史不在
  折叠窗内——但「最新有效修改/解除优先」意味着只要他说过解除，最新的那条一定在窗口内。
- 普通用户的媒介偏好**不产生系统级规则**，只是语气层面；模型可以顺着他，但不会被强制。
- 段数 > 6 直接走文字（长文不合成），与群聊同口径。如果父代理希望私聊对长文更宽松，
  调 `MAX_PRIVATE_TTS_SEGMENTS` 一个常量即可。
- 私聊语音**不挂自动删除**（`auto_delete_seconds=0`）。私聊是「话多」模式，聊天记录应当留痕；
  群聊那边走 `configured_auto_delete_seconds(settings, "media")` 的口径保持原样未动。

---

## 6. 回滚建议

本特性是**单点、可整体关闭**的：

**最快回滚**（零风险，不碰 git）：

```bash
# 生产 .env 里把全局 TTS 关掉即可：私聊立刻退回纯文字，群里照旧
#   （注意这会同时关掉群聊的 TTS，因为本来就是同一个全局开关）
```

若要**只回滚私聊**、保留群聊 TTS：把 `bot/handlers/private_chat.py` 里
`_tts_service()` 改成 `return None` 即可（一行），私聊立即退回纯文字、不注入任何媒介提示。

**彻底回滚**：

```bash
git revert <本次提交>          # 或
git checkout bc2b4972a631cd18b1383d5308e8883af02273fa -- bot/handlers/private_chat.py bot/services/private_chat.py
rm bot/services/private_tts.py tests/test_private_chat_tts.py
```

无库表、无迁移、无配置项需要清理，回滚不需要任何数据修复动。

---

## 7. 上线前建议的父代理动作

1. 在隔离 Linux 容器里装全锁依赖后重跑本目录 §4.2 的四组命令。
2. 生产兼容探针：**先读 `getChat(chat_id).has_restricted_voice_and_video_messages`** 确认字段存在，
   再确认真机上「语音条被拒」的真实错误文案是否落在
   `private_tts.is_voice_privacy_rejection` 的白名单内。
3. 审核公开 fork 不含凭据（本次新增代码**不含任何密钥、URL、账号**；`DELIVERY.md` 亦然）。