# FIX-p4：低危存量修复（第四批）

> 批次：P4（低危存量 · 只动「改法唯一、爆炸半径小」的条目）
> 起点：`af5df1b`（P0/P1/P2 已合并的集成点，= 生产线上代码）
> 硬门槛：**不得改变生产行为**。新增开关/配置默认值必须与今天完全一致；不改热点
> 路径判定；不改数据库 schema；验证诚实（写不出确定性用例的必须写明原因，禁止
> 空洞断言与 skip/xfail）。

本批共 11 个编号条目（任务书标题写「12 条」，正文列出的是 P4-1…P4-11 共 11 条，
其中 P4-5 合并了 A-24=D3-20、P4-6 合并了 A-27=D3-23），11 条全部完成，
一条一提交。

---

## 0. 结论速览

| 编号 | 审计号 | 主题 | 提交 | 状态 |
|---|---|---|---|---|
| P4-1 | A-17 | 日志字段净化 | `41e6328` | 完成（12 条新用例，旧代码红） |
| P4-2 | A-16 | 正文预览字数可配 | `e4b8cb7` | 完成（12 条新用例，旧代码红） |
| P4-3 | A-22 | 后台任务异常不静默 | `aa1c0df` | 完成（8 条新用例，旧代码 5 红） |
| P4-4 | A-23 | `configure_logging` 原子替换 | `84c24bc` | 完成（1 条新用例，旧代码红） |
| P4-5 | A-24=D3-20 | 路径参数 → 400 | `90fa07c` | 完成（6 条 + 11 subtest，旧代码 8 红） |
| P4-6 | A-27=D3-23 | 内部异常不回客户端 | `602a4c7` | 完成（5 条新用例，旧代码 4 红） |
| P4-7 | A-19 | Markdown 占位符不可伪造 | `546e73d` | 完成（14 条 + 33 subtest，旧代码 14 红） |
| P4-8 | D3-22 | 请求体上界 + 413 信封 | `caa714b` | 完成（9 条新用例，旧代码 5 红） |
| P4-9 | B-39 | 台账清理 + O(1) 计数 | `74fcb3e` | 完成（15 条新用例，旧代码 10 红） |
| P4-10 | C2-06 | 空洞用例 | `9c3b9c6` | 完成（变异验证：改坏被测逻辑确实红） |
| P4-11 | D3-41/43/44 | 配置面注释 | `c063fed` | 完成（14 条 + 68 subtest 守卫） |

**关于「在旧代码上会红」**：11 条里 9 条给出了在旧代码上确实失败的执行输出
（见每条的「旧代码上的真实表现」），P4-10 用变异注入证明，P4-11 的断言核对的是
**本来就一致**的部分（作用是防止再次漂移，不是修 bug——详见该条）。

---

## 主题 A：日志与可观测性（不碰业务路径）

### P4-1（A-17）日志字段未净化控制字符

**文件:行**：`bot/utils/logging_setup.py:67-101`（新增 `sanitize_log_field`）、
`bot/middlewares/logging_mw.py:43-56`（三个字段改走净化）

**现象**：`LoggingMiddleware` 把「群名 / 用户名 / 正文预览」原样写进
`log.info("【入口】收到消息 | 聊天=%s | 用户=%s | 类型=%s | 内容=%s")`。
这三个字段 100% 由用户控制：`\r` / `\n` / `\x00` 能把一条记录伪造成多条，
ESC（`\x1b`）能让终端按 ANSI 序列重绘，字段里的 `|` 能伪造出额外一列。
排障与审计都因此失真（看到「某组件写了这条错误」其实可能是用户伪造的）。

**改动机制**：
- `sanitize_log_field(value)`：C0/C1 控制字符转成**可见的等价写法**
  （`\n` / `\r` / `\t` / `\xNN`），ESC 落在同一字符集合里，转义掉 ESC 即让 ANSI
  序列退化成普通字面量；字段内嵌的 `|` 转成 `\|`；空串原样返回，非字符串按
  `str()`（与 `%s` 一致）。**保留原文字符形态**，不是占位符堆。
- `LoggingMiddleware` 的 `chat` / `name` / `preview` 三个字段改走净化；正文原有
  的 `replace("\n","\\n")` / `replace("|","/")` 行为与 `strip()` 顺序**一字未动**，
  净化在其之后进行，所以默认路径下普通消息的日志与改前逐字相同。

**新增用例**：`tests/test_p4_log_sanitization.py`（12 条）
- `SanitizeLogFieldTests`：换行/回车/NUL/TAB/ESC/DEL/C1、`|` 转义、干净文本
  恒等、非字符串 `str()` 语义。
- `LoggingMiddlewareSanitizationTests`：用户名伪造记录、群名伪造记录、caption
  控制字符、`|` 伪造列、**普通消息日志与改前逐字相同**。
  判定用 `assertLogs(...).output` 的**行数**（真实 handler 记录被 `\n` 拆行），
  不是 `assertTrue` 之类。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_log_sanitization.py -q -p no:randomly
............                                                             [100%]
12 passed in 3.99s
```

**旧代码上的真实表现**（用旧中间件直接跑，字段照原样写进 LogRecord）：

```
username-forge lines: 2
    '【入口】收到消息 | 聊天=群 | 用户=evil\n2026-01-01 00:00:00 | ERROR | db | 流=x | 群=-1 | 类型=text | 内容=-'
title-forge lines: 2
    '【入口】收到消息 | 聊天=群\r\n2026-01-01 00:00:00 | 严重 | db | 流=x | 用户=zhangsan | 类型=text | 内容=-'
caption lines: 2
    '【入口】收到消息 | 聊天=群 | 用户=zhangsan | 类型=text | 内容=首行\r\x00\x1b[2K伪造\x1b[0m'
pipe title: '【入口】收到消息 | 聊天=群 | 用户=root | 类型=system | 用户=zhangsan | 类型=text | 内容=-'
normal: '【入口】收到消息 | 聊天=群 | 用户=zhangsan | 类型=text | 内容=在吗'
```

（`\n` / `\r` / `\x00` / ESC 与伪造列都原样进了日志；`normal` 一行与改后完全一致，
这就是「默认行为不变」的证据。）

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_p1_webentry_c2_05_middlewares_and_av_limit.py tests/test_logging_security.py -q -p no:randomly
...................                                                      [100%]
19 passed in 13.13s
```

---

### P4-2（A-16）消息正文前 100 字明文进 INFO 日志

**文件:行**：`bot/services/runtime_config.py:757-761`（schema）、
`:2524-2531`（env 引导）、`bot/utils/logging_setup.py:103-126`
（`message_preview_chars()` / `redacted_message_preview()`）与 `:521-531、:544-552`
（两条路径发布当前值）、`bot/middlewares/logging_mw.py:57-63`、
`bot/web/static/app.js:1237`（Mini App 控件）

**现象**：预览字数 `100` 是写死的，运维既不能调小也不能关掉——生产日志长期留着一份
用户原文。

**改动机制**：
- `LoggingSettingsConfig.message_preview_chars`：`ge=0, le=1000`，**默认 100 =
  改之前的 `raw_text[:100]`**，所以不配 = 日志逐字不变。
- env 引导支持 `LOG_MESSAGE_PREVIEW_CHARS`（与 `LOG_FILE_MAX_BYTES` 同一套写法）。
- `configure_logging` 在 config 路径与 env 路径上都把当前值发布到模块访问器
  （Mini App 改设置 → `apply` 回调 → `configure_logging(force=True, config=...)`
  → 下一条消息即生效）。
- 值为 0 时不记录正文，只记 `<N字 #sha256前8位>`（能回答「多长」「是不是同一条」，
  但正文一个字都不落盘）。空正文在任何预算下都是 `-`（与今天一致）。
- Mini App「日志」页补上同名控件（bounds 与 schema 一致）。

**新增用例**：`tests/test_p4_message_preview_chars.py`（12 条）
- schema 默认 = 100、env 引导默认 = 100 / 覆盖生效、Mini App 控件与 bounds 一致；
- 运行期：默认 100（`content == text[:100]` 且长度恰为 100）、调小到 10、
  调小到 0（正文不出现 + 长度 + 哈希）、空正文仍是 `-`、env 路径 7 字。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_message_preview_chars.py -q -p no:randomly
............                                                             [100%]
12 passed in 4.56s
```

（首轮运行有 2 条红，来自**用例自身**的两处笔误：schema bounds 用 `_asdict`
取不到 `annotated_types` 的 `ge/le`（同文件既有 D3-39 用例也这样写，实际是
`continue` 跳过），以及 300 字的周期串让「第 11~20 字」与「第 1~10 字」相同；
已改成取 `field.metadata[i].ge/le` 与直接比对内容字段长度。）

**受影响的既有用例**（含 P4-1/P4-4 的新用例）：

```
$ .venv/bin/python -m pytest tests/test_p4_log_sanitization.py tests/test_p4_message_preview_chars.py tests/test_p1_webentry_c2_05_middlewares_and_av_limit.py tests/test_logging_security.py tests/test_runtime_config.py tests/test_p1_forkfeatures_d3_39_40_42_config_surface.py -q -p no:randomly
........................................................................ [ 83%]
..............                          [100%]
86 passed, 33 subtests passed in 42.11s
```

---

### P4-3（A-22）后台任务异常被静默吞掉

**文件:行**：`bot/utils/telegram.py:111-165`（`_background_task_label` +
`_observe_telegram_background_task`）、`:98-101`（已报告去重用的
`WeakSet`）、`:1800-1815`（`typing_action` 的输入状态发送）

**现象**：`_observe_telegram_background_task` 是所有 detached Telegram 任务
（删除按钮挂载、定时清理、投递确认……）的统一回收点，改前是
`try: task.exception() except (asyncio.CancelledError, Exception): pass`。
后台任务炸了进程里**没有任何痕迹**，排障只看得到「群里那句话没出现」。

**改动机制**：
- 失败时 `log.exception` 打出完整 traceback + 任务名（名字由发起处给出：
  `delete-button:<chat>:<msg>`、`typing-send:<chat>`、`typing:<chat>`，即
  「发起位置」）+ 异常类型。
- 边界也补齐：取任务异常本身失败、任务抛 `BaseException`，都不再有静默出口。
- 同一个失败**只记一条**（`weakref.WeakSet`）：观察点可能走到两次（done 回调 +
  关停时 `flush_telegram_background_tasks`）。弱引用，任务回收后条目自动消失。
- `typing_action` 里 `except Exception: return False`（输入状态失败）补一条
  warning + 任务名。**成败语义不变**：输入状态是装饰性的，失败只影响这一次心跳
  （返回 False，worker 随即收工），不会把调用方的上下文炸掉。
- **不加重试**（重试属于行为变化）。

**新增用例**：`tests/test_p4_background_task_logging.py`（8 条）
- 失败被记录（名字/错误类型/traceback/`_boom` 帧都在）、只记一次、记账与健康度
  口径不变、成功任务不记、取消任务不记、`BaseException` 任务不把观察器自己炸掉、
  输入状态失败有痕迹且上下文管理器照常退出 / 健康时不记日志。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_background_task_logging.py -q -p no:randomly
........                                                             [100%]
8 passed in 4.65s
```

**旧代码上的真实表现**：

```
$ cd <baseline> && python -m pytest tests/test_p4_background_task_logging.py -q -p no:randomly
FAILED ...::test_a_failed_task_logs_its_name_error_and_traceback
FAILED ...::test_bookkeeping_is_unchanged_by_the_new_logging
FAILED ...::test_observer_does_not_swallow_a_base_exception_task
FAILED ...::test_the_failure_is_recorded_exactly_once
FAILED ...::test_a_failing_typing_send_is_logged_and_still_returns_cleanly
  AssertionError: no logs of level WARNING or higher triggered on bot.utils.telegram
5 failed, 3 passed in 4.83s
```

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_telegram_task_lifecycle.py tests/test_background_health.py tests/test_p4_background_task_logging.py tests/test_auto_delete_modes.py tests/test_p4_log_sanitization.py tests/test_p4_message_preview_chars.py -q -p no:randomly
...................................................                      [100%]
51 passed in 18.23s
```

---

### P4-4（A-23）`configure_logging` 替换 root handler 未持锁

**文件:行**：`bot/utils/logging_setup.py:467-482`（新增 `configure_logging` 外壳 +
`_configure_logging_locked`）

**现象**：改前只有开头（reaper 检查）和结尾（发布全局状态）两小段持
`_LOGGING_STATE_LOCK`，中间「建 listener → `root.handlers.clear()` /
`addHandler` → 关旧 handler」是裸的。两个线程同时 `force=True`（例如 Mini App 热
更新设置撞上关停期的重配置）会各自建一套 listener 往同一个 stdout 写，旧 sink 的
半行和新 sink 的半行拼成一条**从未发生过**的记录；被 `close()` 掉的还可能是另一套
正在用的 handler。

**改动机制**：把 `force` 分支之后的整段（退役旧 sink → 建 pipeline → 摘挂 handler
→ 发布状态）搬进 `_configure_logging_locked()`，由 `configure_logging` 在
`_LOGGING_STATE_LOCK` 内调用。锁是 `RLock`，函数体里的 `shutdown_logging()` 会重入
它；reaper 线程只在 `finally` 里短暂取锁，不会与本函数互等。函数体中原有的两处
`with _LOGGING_STATE_LOCK:` 保留（锁内为空操作），差异最小。

**新增用例**：`tests/test_p4_configure_logging_lock.py`（1 条）
- A 线程停在「listener 已起、root handler 还没换」的临界区中间（靠 patch
  `_BoundedQueueListener.start` 精确停靠），B 线程的 `configure_logging` **必须**
  被挡在门外；释放后收尾状态仍健康（root 上只剩一个 handler、listener 活着、
  `logging_resource_health_snapshot()["ok"]` 为真）。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_configure_logging_lock.py -q -p no:randomly
.                                                                        [100%]
1 passed in 2.09s
```

**旧代码上的真实表现**（B 线程根本不需要这把锁，几毫秒就装完了）：

```
$ cd <baseline> && python -m pytest tests/test_p4_configure_logging_lock.py -q -p no:randomly
>       self.assertTrue(blocked, "第二个 configure_logging 没有被锁挡住：替换不是原子的")
E       AssertionError: False is not true : 第二个 configure_logging 没有被锁挡住：替换不是原子的
1 failed in 0.13s
```

（改后 2.09s vs 旧代码 0.13s：时间差本身就是「B 在门外等锁」的证据。）

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_logging_security.py tests/test_p4_configure_logging_lock.py tests/test_p4_message_preview_chars.py tests/test_p1_webentry_c3_01_container_user.py -q -p no:randomly
.....................                                                    [100%]
21 passed in 6.83s
```

---

## 主题 B：Web/入口健壮性（只改错误路径的返回码与文案，成功路径零变化）

### P4-5（A-24 = D3-20）`settings_api` 多处裸 `int(...)` 解析路径参数

**文件:行**：`bot/web/settings_api.py:1702-1726`（新增模块级 `_path_int` /
`_group_id`）、13 处解析点（`delete_authorized_group_api`、
`list_group_admins_api`、`list_telegram_admins_api`、`create_group_admin_api`、
`delete_group_admin_api` 的 6 处裸 `int()`，以及本来就有内联 try/except 的
`put_group_settings` / `update_rule` / `delete_rule` / `update_memory` /
`delete_memory` 5 处）

**现象**：URL 里的 `{id}` 不是数字时 `ValueError` 冒泡到装饰器兜底
`except Exception` → **500 `internal_error`**：明明是客户端把 URL 打错了，回的却是
「服务器炸了」，既误导管理员，又让真正的 5xx 混在这条噪声里。

**改动机制**：`_path_int(request, key, *, code, message)` 是全文件**唯一**的路径整数
解析入口（缺失/非数字/空白一律 400 + 可读消息），`_group_id` 是它的薄封装。
把本来各写各的内联 try/except 也统一过来，避免以后再分叉。**状态码（400）与成功
响应体一字未改**。

**新增用例**：`tests/test_p4_path_param_parsing.py`（6 条 + 11 subtest）
- D3-20 清单里的 5 个端点：非法 `{id}` → 400 + 各自 code；
- 非法次级 `{user_id}` → 400 `invalid_user_id`；
- 统一后本来就 400 的 5 个端点仍 400（群设置/群规/永久记忆）；
- 空白 `{id}`（`%20`）→ 400；
- **合法 `{id}` 的成功响应体不变**（建群授权 → 200 `{"ok":true,"created":true}`；
  管理员列表 → 200 `{"ok":true,"admins":[]}`；群规列表 → 200 `{"ok":true,"rules":[]}`）；
- 全文件只剩 1 处 `int(request.match_info`（就是 helper 自己）。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_path_param_parsing.py -q -p no:randomly
......                                                        [100%]
6 passed, 11 subtests passed in 14.99s
```

**旧代码上的真实表现**：

```
$ cd <baseline> && python -m pytest tests/test_p4_path_param_parsing.py -q -p no:randomly
SUBFAILED(endpoint='/api/v1/authorized-groups/not-a-number') ... test_non_numeric_path_ids_return_400_not_500
SUBFAILED(endpoint='/api/v1/groups/not-a-number/admins') ... test_non_numeric_path_ids_return_400_not_500
SUBFAILED(endpoint='/api/v1/groups/not-a-number/telegram-admins') ... test_non_numeric_path_ids_return_400_not_500
SUBFAILED(endpoint='/api/v1/groups/not-a-number/admins') ... test_non_numeric_path_ids_return_400_not_500
SUBFAILED(endpoint='/api/v1/groups/not-a-number/admins/900001') ... test_non_numeric_path_ids_return_400_not_500
SUBFAILED(endpoint='/api/v1/groups/-1001/admins/not-a-number') ... test_non_numeric_secondary_path_ids_return_400
FAILED ...::test_missing_or_empty_path_ids_return_400
FAILED ...::test_the_helper_is_the_single_entry_point
8 failed, 4 passed, 5 subtests passed in 15.50s
```

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_settings_api_helpers.py tests/test_settings_web.py tests/test_p1_a09_a10_settings_api_robustness.py tests/test_p1_a04_paged_policy_lists.py tests/test_p1_a06_miniapp_replay_budget.py tests/test_p0_security_d3_18_admin_revocation.py -q -p no:randomly
.....................................................................    [100%]
69 passed in 88.37s (0:01:28)
```

---

### P4-6（A-27 = D3-23）内部异常 `str(exc)` 原样回给 HTTP 客户端

**文件:行**：`bot/web/settings_api.py:1729-1755`（`_opaque_error_ref` /
`_opaque_error_message`）、`:1799-1815`（409 revision_conflict）、
`:1833-1852`（any_admin 兜底 500 + 400 secret_storage）、`:2001-2011`
（`put_settings` 的 400 secret_storage）

**现象**：管理员在 Mini App 里会看到
- 409：`runtime config revision changed: expected 5, got 7`（暴露内部 revision 数值）
- 400：`CONFIG_MASTER_KEY is required before saving secret settings` /
  `stored settings cannot be decrypted with CONFIG_MASTER_KEY`（把配置项的 **env
  变量名**告诉前端）

两者对管理员排障都没有帮助，细节却留在了浏览器里。

**改动机制**：`_opaque_error_ref(exc, code=..., where=...)` 生成一个 8 位十六进制
编号，用 `log.error(..., exc_info=exc)` 把完整 traceback 记进服务端日志；客户端拿到
的是通用中文文案 + `（错误编号 xxxxxxxx）`。三处接入：409 revision_conflict、
400 secret_storage（两处）、以及两个装饰器的兜底 500（原来只有一句
`log.exception`，现在也带 ref，运维才能把用户报的编号对上日志）。
**状态码一字未改（409/400/500 各归各位），成功响应未动。**

> 关于 `log.exception` vs `log.error(exc_info=exc)`：本条的三处调用点都在
> `except` 块内，但记录发生在**辅助函数**里；显式传 `exc_info=exc` 比依赖
> `sys.exc_info()` 的隐式传播更可靠，打出来的 traceback 与 `log.exception`
> 完全一致。

**新增用例**：`tests/test_p4_opaque_internal_errors.py`（5 条）
- 409：状态码不变、文案不含 `runtime config revision changed`/`expected`/revision
  数值、仍含「请刷新后重试」、含可核对编号、**编号能在日志里对上**且日志里有原文；
- 400：状态码不变、文案不含 `CONFIG_MASTER_KEY`、含「最高管理员」、编号可对上；
- 兜底 500：状态码不变、文案不含数据库路径与原文、编号可对上；
- 两次失败的编号互不相同；
- 成功响应里不出现「错误编号」。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_opaque_internal_errors.py -q -p no:randomly
.....                                                                    [100%]
5 passed in 15.89s
```

**旧代码上的真实表现**：

```
$ cd <baseline> && python -m pytest tests/test_p4_opaque_internal_errors.py -q -p no:randomly
FAILED ...::test_encryption_failure_keeps_400_but_hides_the_env_var_name
FAILED ...::test_revision_conflict_keeps_409_but_hides_internal_text
FAILED ...::test_two_failures_get_two_different_refs
FAILED ...::test_unexpected_failure_is_a_generic_500_with_a_log_ref
4 failed, 1 passed in 17.62s
```

顺带记录旧代码的实际返回体（用旧代码直接打一次 PUT）：

```
oversized -> 413 'Maximum request body size 1048576 exceeded.'
normal -> 200 '{"ok": true, "revision": 2, ...'
text/plain -> 400 '{"ok": false, "error": {"code": "invalid_json", ...}}'
tiny-but-valid-shape -> 409 '{"ok": false, "error": {"code": "revision_conflict",
                        "message": "runtime config revision changed: expected 1, got 2"}}'
```

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_settings_web.py tests/test_settings_api_helpers.py -q -p no:randomly
.....................................                                    [100%]
37 passed in 51.92s
```

---

### P4-7（A-19）`md_to_html` 内部占位符可被输入伪造触发 `IndexError`

**文件:行**：`bot/utils/telegram.py:1129-1153`（`_MARKDOWN_TOKEN_BASE` +
`_markdown_token_prefix()` + `_markdown_token_pattern()`）、
`:1397-1409`（`_stash` 用动态前缀）、`:1428-1441`（反替换）

**现象**：`_render_inline_markdown` 用 `\x00tgmd{index}\x00` 暂存已渲染的代码段/链接，
最后正则反替换回去。前缀写死，于是正文里只要出现 `\x00tgmd9\x00`（用户自己打出来，
或模型原样吐回来），反替换就会去取 `tokens[9]` → `IndexError`，整条消息的渲染直接
炸掉。

**改动机制**：`_markdown_token_prefix(text)` 在**输入原文**里找一个不可能出现的前缀
（`\x00tgmd` 不在 text 里就用它，否则逐个加长），据此编译反替换正则。所有占位符
都以该前缀开头，而 `source` 里除占位符就只有 text 的片段，所以输入**构造不出**能
命中的占位符。反替换再加一层下界兜底（前缀选取将来被改坏时也只会把文本原样留下，
而不是整条渲染抛异常）。

**正常 Markdown 渲染结果逐字节不变**——这一点由两组证据钉住：
1. 新用例里的 `test_normal_rendering_is_byte_identical_for_every_case` 断言 6 组
   正常输入的**完整**输出字符串；它在**旧代码上也通过**（说明期望值就是改前的行为）；
2. 既有用例 `tests/test_telegram_send.py` 的 `test_markdown_renderer_*` 等 55 条
   全部仍通过。

**新增用例**：`tests/test_p4_markdown_token_sentinel.py`（14 条 + 33 subtest）
- 伪造 `\x00tgmd0/7/9\x00` 不再抛、保留为字面文本、嵌在代码段里仍渲染成 `<code>`、
  与真标记混排时两者都在、长消息里反复伪造正常、位数很多（`\x00tgmd999999\x00`）
  正常、围栏代码块里的伪造正常；
- `_markdown_token_prefix` 的选取规则与「选中的前缀不在输入里」的性质；
- 正常渲染逐字一致 + 渲染结果里不留 `\x00`/`tgmd` 残迹。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_markdown_token_sentinel.py -q -p no:randomly
..............                                                          [100%]
14 passed, 33 subtests passed in 4.64s

$ .venv/bin/python -m pytest tests/test_telegram_send.py -q -p no:randomly
.......................................................                  [100%]
55 passed in 7.26s
```

**旧代码上的真实表现**（直接调旧 `md_to_html`）：

```
'hi \x00tgmd7\x00 there' -> RAISED IndexError list index out of range
'\x00tgmd0\x00'         -> RAISED IndexError list index out of range
'a\x00tgmd1\x00b'       -> RAISED IndexError list index out of range
```

（同一文件在旧代码上跑新用例：14 failed, 7 passed, 26 subtests passed——其中
`NormalRenderingUnchangedTests` 的 3 条在旧代码上**通过**，正是「渲染逐字不变」的
对照证据。）

---

### P4-8（D3-22）`request.json()` 无超时/无大小上界

**文件:行**：`bot/web/settings_api.py:210-220`（`_JSON_BODY_MAX_BYTES`）、
`:559-583`（`_read_request_json` 的 `Content-Length` 前置检查）、
`:612-629`（`_json_object` 映射 `HTTPRequestEntityTooLarge`）、
`:15-16`（import）

**现状（先纠正审计前提）**：读超时**本来就存在**（`_JSON_BODY_TIMEOUT_SECONDS = 5.0`
→ 408，既有用例 `JsonBodyDeadlineTests` 钉着）；大小上界也**存在**，但在 app 层
（`verify_web` 的 `client_max_size=1 MiB`），命中后 aiohttp 抛
`HTTPRequestEntityTooLarge`，由框架回一个**纯文本** 413
（`Maximum request body size 1048576 exceeded.`），与本文件其它接口统一的
`{"ok":false,"error":{...}}` 信封对不上，Mini App 只能当未知错误；而且 handler
自己看不到这条上界。

**改动机制（默认值与今天完全一致 = 1 MiB）**：
- 新增 `_JSON_BODY_MAX_BYTES = 1024 * 1024`，与 app 级 `client_max_size` 同值
  （有用例断言两者相等，让「无行为变化」可验证而不是口头承诺）；
- `_read_request_json` **开读之前**先看 `Content-Length`，超限直接 413，不把正文
  读进内存；没有 `Content-Length` 的分块请求仍由 aiohttp 的流式上界兜底；
- `_json_object` 把 aiohttp 的 `HTTPRequestEntityTooLarge` 映射成同款 413 信封。
- 读超时这条既有保证不动（用例同时钉住默认仍是 5.0 且仍回 408）。

**新增用例**：`tests/test_p4_json_body_bounds.py`（9 条）
- handler 上界 == app 级 `_WEB_MAX_REQUEST_BYTES` == 1 MiB；
- `Content-Length` 超限 → 413 且 `request.json` **一次都没被 await**（正文没进内存）；
- 恰好等于上界 → 放行；`Content-Length` 为 None → 不误杀（交给 aiohttp）；
- aiohttp 抛 `HTTPRequestEntityTooLarge` → 同款 413 信封；
- 超时仍 408（取消抵抗型读取器，硬时限）；
- 真客户端：2 MiB 请求体 → 413 + JSON 信封（且响应里**不含** aiohttp 那句纯文本）；
  普通请求 200（revision 正确 +1）；`text/plain` 仍 400 `invalid_json`。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_json_body_bounds.py -q -p no:randomly
.........                                                                 [100%]
9 passed in 11.69s
```

**旧代码上的真实表现**：

```
$ cd <baseline> && python -m pytest tests/test_p4_json_body_bounds.py -q -p no:randomly
FAILED ...::JsonBodySizeBoundTests::test_aiohttp_too_large_becomes_the_json_envelope
FAILED ...::JsonBodySizeBoundTests::test_content_length_exactly_at_the_cap_is_accepted
FAILED ...::JsonBodySizeBoundTests::test_oversized_content_length_is_rejected_without_reading
FAILED ...::JsonBodySizeBoundTests::test_the_handler_cap_equals_the_app_level_client_max_size
FAILED ...::JsonBodySizeBoundOverHttpTests::test_oversized_request_gets_413_with_the_standard_envelope
5 failed, 4 passed in 12.21s
```

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_settings_api_helpers.py tests/test_p4_json_body_bounds.py tests/test_p4_path_param_parsing.py -q -p no:randomly
........................                                              [100%]
24 passed, 11 subtests passed in 19.21s
```

---

## 主题 C：后台清理、测试质量、配置面文档（零业务影响）

### P4-9（B-39）长期记忆每日提炼台账只增不删、每轮对全表求和

**文件:行**：`bot/services/long_term_memory.py:159-167`（保留期常量）、
`:299-311`（`memory_extract_ledger_retention_days`）、`:237-242`（当日合计 + 清理
标记）、`:592-704`（`_prune_extraction_ledger` / `note_extraction_run` /
`_bump_extraction_total` / `extraction_runs_today` / `reset_extraction_ledger`）、
`:1680`（`run_extraction_round` 传 `settings`）、`bot/config.py:267-272`（新配置字段）
与 `:1100-1103`（`[bot]` 段导入白名单）

**先纠正审计前提**：审计说的「台账表」在本仓库里是**进程内 dict**
（`_DAILY_EXTRACTION_LEDGER`），不是数据库表。现象仍然成立且更具体：
- dict **只增不减**：每碰过一个 `(scope, scope_id)` 就多一个条目，进程活多久留多少个；
- 全局额度判定用的 `extraction_runs_today()`（不带参数）**每次遍历整张 dict 求和**。

**改动机制**：
- 新增 `_DAILY_EXTRACTION_TOTAL`（当日合计），记一次提炼 +1，读侧 O(1)。它与
  「遍历求和」**恒等**：每条 note 只让一个作用域的当日计数 +1，所以当日合计恒等于
  各作用域当日计数之和（有用例逐步对照旧算法）。
- `_prune_extraction_ledger()`：每个自然日扫一次（`_DAILY_EXTRACTION_PRUNED_DAY`），
  摘掉保留期之外的条目，正常路径 O(1)。保留期来自新配置
  `memory_extract_ledger_retention_days`（BotConfig，**默认 1 = 只留当天**，可在
  `config.toml` 的 `[bot]` 段配；不进 runtime_config / Mini App，因为保留期只是
  内存卫生，不值得再开一个热更入口）。
- `reset_extraction_ledger()` 一并清掉合计与清理标记（单测语义不变）。

**额度口径不变**：读侧两条路径本来都按自然日过滤，过期条目对「今天已用几次」贡献
恒为 0，所以清理不可能改变任何一次判定。

**新增用例**：`tests/test_p4_extraction_ledger.py`（15 条）
- 默认保留期 1、越界夹取、过期条目被摘、20 作用域 × 4 天 = 80 次调用后 dict 只剩
  20 条（改前会留 80 条）、保留期 3 vs 1 的窗口差异、每天只扫一次；
- **额度判定**：达到上限仍然拒绝（放行 2 次后第 3 次被拒）、未达上限仍然放行、
  `cap=0` 不拦、跨日的分作用域计数从 1 重新开始；
- **跨版本对照**（只用改前就有的调用签名，不传 `settings`）：同一串操作在旧代码与
  新代码上给出**同样的放行/拒绝序列** `[T,T,F,F,T,T,F,F]` 与同样的计数——这条在
  旧代码上**也通过**，是「口径未变」最直接的证据；
- 新的 O(1) 计数器与旧算法逐步相等（含跨日读不泄漏昨天的合计）、没有记录的日子读出
  0、`reset_extraction_ledger` 把合计也清干净。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_extraction_ledger.py -q -p no:randomly
...............                                                          [100%]
15 passed in 0.55s

$ cd <baseline> && python -m pytest tests/test_p4_extraction_ledger.py::QuotaVerdictIsIdenticalOnBothRevisionsTests -q -p no:randomly
..                                                                        [100%]
2 passed in 0.54s
```

（首轮运行有 5 条红，全部来自**用例自身**的 `range(-1, -6)` 这类空区间笔误
（`range` 从 -1 递增到 -6 是空的），以及一个把「自然日」写成回溯的用例；已修正为
递增区间与单调日期序列。）

**旧代码上的真实表现**：

```
$ cd <baseline> && python -m pytest tests/test_p4_extraction_ledger.py -q -p no:randomly
FAILED ...::ExtractionLedgerRetentionTests::test_a_wider_retention_keeps_more_days
FAILED ...::ExtractionLedgerRetentionTests::test_default_retention_is_one_day
FAILED ...::ExtractionLedgerRetentionTests::test_entries_older_than_the_retention_are_dropped
FAILED ...::ExtractionLedgerRetentionTests::test_pruning_happens_once_per_day
FAILED ...::ExtractionLedgerRetentionTests::test_retention_is_clamped
FAILED ...::ExtractionLedgerRetentionTests::test_the_ledger_stops_growing_with_the_number_of_scopes_over_time
FAILED ...::ExtractionQuotaVerdictUnchangedTests::test_a_zero_cap_never_blocks
FAILED ...::ExtractionQuotaVerdictUnchangedTests::test_below_the_cap_still_admits
FAILED ...::ExtractionQuotaVerdictUnchangedTests::test_per_scope_counts_are_unchanged_across_days
FAILED ...::ExtractionQuotaVerdictUnchangedTests::test_reaching_the_cap_still_blocks_the_next_run
（10 failed, … in 5.42s）
```

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_long_term_memory.py tests/test_p4_extraction_ledger.py tests/test_configurable_budgets.py tests/test_p1_forkfeatures_d3_0506_memory_write.py -q -p no:randomly
...........................................................           [ 52%]
..........................................                              [ 90%]
...........                                                            [100%]
112 passed, 43 subtests passed in 77.21s (0:01:17)
```

---

### P4-10（C2-06）唯一一条空洞测试

**文件:行**：`tests/test_db_reliability.py:74-99`

**现象（审计认定）**：`test_same_inode_shadow_symlink_is_not_ambiguous` 只把夹具搭
出来然后直接调用被测函数，**一句断言都没有**——删掉所有断言它照样绿。

**改动机制**（只改测试，不动被测代码）：真正检验目标行为「同名影子**是同一个
inode** 时不构成歧义」：
1. 先断言夹具前提成立（`samefile()` + `st_ino` 相同），否则这条用例什么都没测到；
2. 再断言 `_warn_about_sqlite_shadow_paths` 既不报 ERROR（`assertNoLogs`）也不
   fail closed 抛 `RuntimeError`。

**变异验证（真做过）**：把 `bot/db/engine.py` 里
`if path.exists() and sibling.samefile(path): continue` 整段去掉后本用例立刻红：

```
$ .venv/bin/python -m pytest "tests/test_db_reliability.py::DatabaseUrlTests::test_same_inode_shadow_symlink_is_not_ambiguous" -q -p no:randomly
...
                dangerous.append(item)
            except (OSError, sqlite3.DatabaseError):
                dangerous = suspicious
            if dangerous:
>               raise RuntimeError(
                    "Refusing to start with an ambiguous SQLite shadow database; "
                    "archive or explicitly migrate: "
                    + ", ".join(repr(str(item)) for item in dangerous)
                )
E               RuntimeError: Refusing to start with an ambiguous SQLite shadow database; archive or explicitly migrate: '/var/folders/.../bot.db\r'
1 failed in 4.78s
```

改回后（`git status` 确认 `bot/db/engine.py` 无残留改动）：

```
$ .venv/bin/python -m pytest tests/test_db_reliability.py -q -p no:randomly
...................                                                      [100%]
19 passed in 5.70s
```

---

### P4-11（D3-41 / D3-43 / D3-44）配置面注释、默认值与实际脱节

**前提说明**：审计给的行号（`config.py:362/279/547/138`、`runtime_config.py:392/539/287/480-483`、
`app.js:945`、`group_summary.py:217-218`）**已随 P0/P1/P2 的改动漂移**。本条按
「逐处核对」重新定位，修掉实际存在的脱节，并如实记录哪一处**没有**复现出脱节。

**实际改动（全部只改注释/说明文字；`git diff` 里非注释行只有 app.js 的一处 `title=`）**：

1. **D3-43**（`bot/services/skills/webfetch.py:35-46`）：注释里写着
   ``config.py:545-548``，而那组 `firecrawl_*` 字段早已挪走（现在在 578-586，
   本批 P4-9 又给它前面加了几行）——照着行号跳过去会指空。改为按**字段名**指路，
   并写明读侧口径：技能拿到的是**根 Settings**（不是 `settings.bot`），所以这几个是
   顶层字段。
2. **D3-41**（`bot/config.py:576-586`）：`firecrawl_*` 整块被注释成
   `# Firecrawl-backed web search (websearch skill backend).`，但
   `firecrawl_timeout_sec`(20s) 属于 **webfetch**、`firecrawl_search_timeout_sec`(18s)
   属于 **websearch**，是两个旋钮。改为逐字段注释，点明别把 20s 当检索超时。
3. **B-41**（`bot/config.py:138-148` + `config.toml:9-12`）：
   `drop_pending_updates` 在生产**不可达**——三道保险都指向 False：
   `runtime_config.BotBehaviorConfig` 的 `_preserve_pending_updates` 前置校验把任何
   写入归一成 False → `apply_to_settings` 因此永远写 False →
   `update_delivery.run_update_delivery` 仍然强制
   `polling_drop_pending_updates = False`（并打 `ignored deprecated
   drop_pending_updates=true` 的 debug 日志）；而唯一能把它设成 True 的
   `load_settings()` 从不被调用（有既有用例钉着）。按任务要求**不删代码**，在注释
   里写明现状；`config.toml` 那一行也说上话。
4. **D3-44**（`bot/services/group_summary.py:197-209`）：逐字段核对后发现
   `group_summary` 的 `default/low/high` 与 `config.py`、
   `runtime_config.py`、`app.js` 的 min/max **本来就已经一致**（13 个字段全部对齐；
   审计指向的 `group_summary.py:217-218` 没复现出真实脱节——那一行是
   `batch_max_messages` 的 `default=200, low=10, high=2_000`，与
   `config.py`/`runtime_config.py`/`app.js` 三处完全相同）。因此本条改为：
   把「四处必须同步」写进 docstring，并补一组**逐字段核对**的守卫用例，防止下次
   改动再把它们拉散。
5. **app.js:942-945**（原审计指向的行）：类别清理方式的 `title` 改为说清按钮模式的
   真实行为（不定时删除、每条消息下方附「删除消息」按钮、本行的秒数在按钮模式下不
   生效），与 `telegram.configured_auto_delete_mode` / `AUTO_DELETE_BUTTON_SENTINEL`
   的实现一致。

**新增用例**：`tests/test_p4_config_surface_docs.py`（14 条 + 68 subtest）
- 注释按字段名指路（且全仓不再有 `config.py:<行号>` 引用——这类引用必然过期）；
- firecrawl 整块注释同时提到 webfetch 与 websearch 的各自超时；两个超时默认值
  不同（20 / 18）；两个技能各自读到对应那个（跑真实技能对象，不是看源码）；
- `drop_pending_updates`：注释块存在且写了「不可达 / update_delivery /
  _preserve_pending_updates」；schema 真的把 `True` 归一成 `False`（跑真实
  `BotBehaviorConfig`）；`update_delivery` 源码里真的强制 False 且**没有**任何
  `drop_pending_updates=settings.bot` 的透传；`config.toml` 里真的写了「生产不可达」
  且值仍是 `false`；
- group_summary 13 个字段：读侧都夹取、夹取默认值 == dataclass 默认值 == `config.py`
  声明 == `runtime_config.py` schema、Mini App 的 min/max 覆盖真实夹取范围。

**真实执行输出（改后）**：

```
$ .venv/bin/python -m pytest tests/test_p4_config_surface_docs.py -q -p no:randomly
..............                                                          [100%]
14 passed, 68 subtests passed in 4.69s
```

（首轮运行有 12 条红，全部来自**用例自身**：把 `config.py:562-565` 当成真实行号
（而 P4-9 刚在这个文件前面加过字段，行号又变了——这恰好证明「按行号指路」不可靠）、
把 dataclass 字段名当成 `settings.bot` 属性名（`max_tokens` vs `max_summary_tokens`）、
`float` 走了 `int()` 解析、`WebSearchSkill` 的超时属性名是 `_search_timeout`
而非 `_timeout`、`drop_pending_updates=False` 实际出现 2 次而不是 3 次。全部已修正；
修正后本条自己的注释也改成了按字段名指路。）

**受影响的既有用例**：

```
$ .venv/bin/python -m pytest tests/test_p4_config_surface_docs.py tests/test_settings_frontend.py tests/test_deployment_config.py tests/test_group_summary.py tests/test_p0_security_b03_webfetch_ssrf.py -q -p no:randomly
............................................................................ [ 76%]
........................                          [100%]
100 passed, 91 subtests passed in 27.78s
```

---

## 全量测试

**改动前基线**（未改动的代码 + 未含本批新用例的完整套件，23 分 57 秒）：

```
$ .venv/bin/python -m pytest tests -q -p no:randomly
3479 passed, 51 warnings, 589 subtests passed in 1437.54s (0:23:57)
```

**改动后全量**（同一条命令，24 分 54 秒）：

```
$ .venv/bin/python -m pytest tests -q -p no:randomly
3575 passed, 51 warnings, 701 subtests passed in 1494.14s (0:24:54)
```

**0 失败、0 错误**；用例数 3479 → 3575 正好等于本批新增的 96 条
（12+12+8+1+6+5+9+15+14+14），subtest 589 → 701。警告数不变（51 条，改前改后
都是同一批 `RuntimeWarning`/`DeprecationWarning`，与本批改动无关）。
基线是在**改动前**的代码副本上跑的（`baseline/` 目录 = `af5df1b` 的完整工作树），
因此这两行数字是同口径的对照。

**本批 10 个测试文件**（`tests/test_p4_*.py`）汇总：

```
$ .venv/bin/python -m pytest tests/test_p4_log_sanitization.py tests/test_p4_message_preview_chars.py \
    tests/test_p4_background_task_logging.py tests/test_p4_configure_logging_lock.py \
    tests/test_p4_path_param_parsing.py tests/test_p4_opaque_internal_errors.py \
    tests/test_p4_markdown_token_sentinel.py tests/test_p4_json_body_bounds.py \
    tests/test_p4_extraction_ledger.py tests/test_p4_config_surface_docs.py -q -p no:randomly
（结果见下）
```

```
...................................................... [ 56%]
..........................................         [100%]
96 passed, 112 subtests passed in 27.54s
```

96 条 = 3479 → 3575 的增量，112 个 subtest = 589 → 701 的增量，数字对得上。
各文件的单独执行结果已逐条列在上面每个条目里。

---

## 明确不做（本批刻意不动的条目，作为留档结论）

- **热点路径判定类**：A-15（去重竞态）、A-20（@机器人子串匹配）、A-21（is_user_admin
  私聊）、A-25（sanitize_history system 透传）、A-28/A-29/A-30（审核与管理员命令）、
  B-14（清理中间件）、B-28（热路径裸调 `_config_provider`）——都落在「审核判定 /
  权限豁免 / 去重语义」这些硬门槛上，本批一律不碰。
- **记忆语义/额度类**：B-36、B-37、B-40、D3-07、D3-08、D3-09——提炼/注入/召回的语义
  本批不动（B-39 只清理内存台账与计数方式，口径已用跨版本对照钉死）。
- **模型限额与拟合类**：D3-10、D3-11、D3-12——会改变限额判定，违反硬门槛。
- **Web/验证子系统较大改动**：D3-19、D3-21、D3-24、D3-25、D3-26、D3-49——改动面
  超出「爆炸半径小」的口径。
- **需要改 DB schema**：B-38（唯一约束 + 存量去重迁移）——任务书明确禁止改 schema。
- **会影响群内表现**：D3-34（提示词文案）——改文案等于改群内表现，本批不做。
- **已实际失效**：B-13（`./bot` 挂载已被 P1 的 B-08 移除）、A-18（生产单 loop ⇒ 未
  发生）、B-29（负向记录，本就「无问题」）。

---

## 无法构造确定性用例的条目

本批 11 条**没有**「在当前环境下无法构造确定性用例」的条目：

- 9 条给出了在旧代码上确实失败的执行输出（见各条「旧代码上的真实表现」）；
- P4-10 用变异注入（把被测逻辑改坏）证明用例确实能红；
- P4-11 的断言核对的是**本来就一致**的配置面（防漂移守卫，不是修 bug），这一点在
  条目里已写明，不算「写不出用例」。

需要说明的两处**用例自身首轮失败**（已修正，不是被测代码的问题）：P4-2 的两条
（schema bounds 取值方式、周期字符串让「后 10 字」与「前 10 字」相同）与 P4-9 /
P4-11 的若干条（`range` 空区间、命名空间混用、float 走 int 解析、属性名搞错、
行数估计错），修正过程与原因都写在对应条目里，不藏着。
