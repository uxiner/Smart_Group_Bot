"""审核送审的两道成本闸（F-021）：``ModerationAdmissionGate``（整形）与
``ModerationCallBudget``（硬上限）。

为什么需要
----------
每条可审消息都会触发一次审核模型调用（边缘 ban 判定再加一次；每张图再加一次视觉
调用）。全局 8 槽信号量只限制**并发**、不限制**总量**：一个成员在几秒内连发 N 条
消息，就能在瞬间吃掉全部审核槽并把 N 次调用强加给上游，成本随消息条数线性放大；
pending-reply debounce 只合并出站回复，不合并审核。

口径（比"省钱"更重要）
--------------------
* **整形闸绝不用精度换成本**：超限只影响"什么时候送审"，不影响"送不送"、更不影响
  "送什么"。整形闸里没有、也不会有"超限就跳过大模型、退化成本地正则"的分支。
* **绝不丢消息**：每条消息都会拿到一次完整的模型判定，只是送审时刻被整形。
* **正常聊天完全不受影响**：桶初始是满的（``burst`` 条零延迟），一个成员要在
  ``spacing_seconds`` 秒内发超过 ``burst`` 条才会开始等待；单条消息的额外延迟
  上限是 ``max_wait_seconds``。
* 判定为**非成员触发**的路径（人工申诉复核、资料巡检、入群筛查、/report）默认
  完全不经过这两道闸，见 ``ModerationService.evaluate`` 的 ``sender_id``。

代价与背压（写清楚，不藏）
------------------------
* 延迟：单个成员超预算后每条最多多等 ``max_wait_seconds``；只影响这个成员自己，
  不会把别人的审核往后排。典型取值（burst=3 / spacing=4s / max_wait=6s /
  max_waiters=3）下，一个人在 1 秒内连发 6 条：前 3 条零延迟，第 4 条等 4s，
  第 5、6 条各等 6s（截断），此后的消息因为等待名额已满而直接立刻送审。
* 积压：同时**等待**中的审核总数全局封顶 ``max_waiters``。超过就不再排队、直接
  立刻送审（``bypassed``，记 WARNING），因此最多只有 ``max_waiters`` 个更新 worker
  可能被这个闸停住；``WEBHOOK_MAX_CONCURRENT_UPDATES`` 是 8，剩下的继续服务其他
  人——积压不会把通道堵死。等待本身也发生在审核协程里、不额外持有任何锁。
* 内存：每个 key 只保留一个很小的状态对象，字典到 ``max_keys`` 先清空闲条目
  （与 ``bot.services.av_query_limits.AVPrivateRateLimiter`` 同样的做法）。
* 总调用数**在整形闸这一层不变**：它只把成本从"同一瞬间的并发峰值"摊到时间轴上。
  真正的总量下降靠下面那道**硬上限**。
* 安全代价：被整形的第 4 条及以后的消息会晚 4-6 秒被处置（违规内容多停留几秒）。
  这与 pending-reply debounce（``inbound_debounce_seconds`` 默认 5s）同一量级，
  且本地确定性命中的消息零延迟、不排队。

成本硬上限（``ModerationCallBudget``）
-------------------------------------
整形闸只推迟送审时刻、**不限量**：成员发 N 条就是 N 次模型调用，只是被摊到时间轴
上；过载时整形等于失效，成本放大面原封不动。所以补一道**每群每小时 N 次**的总量
硬上限。

* **默认不限（``per_hour=0``）**——生产行为与今天逐字一致，不配就等于没有这道闸。
* 作用域与整形闸一致：**只作用于成员触发的送审**（``sender_id > 0``）。申诉复核、
  资料巡检、入群筛查、``/report`` 这些人工/低频路径不受影响，否则一次人工复核就
  可能被别人的刷屏挤掉。
* **降级口径 = 选报告里的 ①（只跑本地确定性规则并记为"未送审"）**。没有选 ② 的
  有界等待队列：等待队列解决的是"稍后还有额度吗"，而额度是按小时窗口重置的，
  排队只会把这条消息的处置推迟到窗口切换、同时占住一个更新 worker；对一个**每
  小时**的桶来说这两头都不划算。
* **绝不静默放行**：降级时返回的 verdict 与"模型调用失败""模型不可用"**完全同一个
  口径**——``conclusive=False``。于是调用方不会把它当成"已审查通过"写进缓存，也不会
  给它记"干净"；本地确定性规则（关键词/正则，置信度 1.0）在这之前已经全部跑完，
  真命中会**在到达这段代码之前**就返回并照常处置。所以"既没过本地规则、又没送审"
  这条静默放行路径不存在：超限时消息要么被本地规则抓住，要么被明确标记为**未送审**。
* 安全代价（写明）：开了上限之后，一个超限窗口里靠语义规则才能识别的违规会漏过。
  这是显式配置的取舍，不是默认行为。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: 桶容量：一个成员在 ``spacing_seconds`` 窗口内的前 ``burst`` 条消息零延迟送审。
#: 取 3 与仓库既有的 ``TELEGRAM_AUTH_CANDIDATE_BURST``（update_delivery.py）同一
#: 量级：正常群聊里连续 3 条已经算"刷屏"。
DEFAULT_BURST = 3
#: 令牌补充间隔 = 超预算后每条审核之间的最小间隔。4s/条 = 15 条/分钟/成员，远高于
#: 真人聊天速率（AV 私聊限流是每小时 10 次，/report 与 @admin 是 60s 冷却），只掐
#: "机器人式连发"，不会碰到正常群友。
DEFAULT_SPACING_SECONDS = 4.0
#: 单条消息为了排队最多多等多久。审核阶段预算是 35s，而且阶段 deadline 从**调用
#: 那一刻**起算（llm._chat_with_fallbacks 里 ``deadline = loop.time() + deadline_sec``），
#: 所以 6s 的排队不会挤掉审核自己的预算；同时保证一个更新 worker 最多被停 6s。
DEFAULT_MAX_WAIT_SECONDS = 6.0
#: 全局同时等待的审核数上限：留足 worker 给其他人，避免积压把通道堵死。
DEFAULT_MAX_WAITERS = 3
#: 状态字典上限（超过先清空闲 key）。
DEFAULT_MAX_KEYS = 4096
#: 同一个 key 的"整形饱和"告警最小间隔，避免刷屏式连发把日志也刷爆。
_BYPASS_WARN_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """一次送审准入的结果（只用于观测与测试，调用方不需要做分支）。"""

    #: 桶为空（本次本应等待/整形）
    shaped: bool = False
    #: 实际等待秒数（0 表示立刻放行）
    waited_seconds: float = 0.0
    #: 因为等待名额已满（或不允许等待）而没有真的等，直接立刻送审
    bypassed: bool = False
    #: 理论需要的等待秒数（截断前）
    planned_delay: float = 0.0

    @property
    def delayed(self) -> bool:
        return self.waited_seconds > 0


class _KeyState:
    """一个 (群, 成员) 的 GCRA 状态：``tat`` = 下一个可送审时刻。"""

    __slots__ = ("tat", "last_seen", "warned_at")

    def __init__(self, tat: float, last_seen: float) -> None:
        self.tat = tat
        self.last_seen = last_seen
        self.warned_at = 0.0


class ModerationAdmissionGate:
    """按 (group_id, user_id) 的审核准入整形（GCRA + 有界等待）。

    纯内存、无锁：状态更新都是同步的（没有 await 断层），等待只发生在
    ``asyncio.sleep`` 上，取消时会正确归还"等待名额"。
    """

    def __init__(
        self,
        *,
        burst: int = DEFAULT_BURST,
        spacing_seconds: float = DEFAULT_SPACING_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        max_waiters: int = DEFAULT_MAX_WAITERS,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.burst = max(1, int(burst))
        self.spacing_seconds = max(0.0, float(spacing_seconds))
        self.max_wait_seconds = max(0.0, float(max_wait_seconds))
        self.max_waiters = max(0, int(max_waiters))
        self.max_keys = max(16, int(max_keys))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        # 桶容量换算成时间容忍度：前 burst 条消息的 tat 落后于 now 也不会产生延迟。
        self._tolerance = (self.burst - 1) * self.spacing_seconds
        self._states: dict[tuple[int, int], _KeyState] = {}
        self._waiters = 0
        # 观测计数（测试与排查用）
        self.shaped_total = 0
        self.waited_total = 0
        self.bypassed_total = 0
        self.max_observed_wait = 0.0

    # -- 状态 ------------------------------------------------------------- #

    def _state_for(self, key: tuple[int, int], now: float) -> _KeyState | None:
        """取（必要时新建）桶；**字典已达硬上限且腾不出位置时返回 ``None``**。

        B-05：原来这里是「满了先清一次，然后无论清没清出位置都插入新 key」，
        ``max_keys`` 只是「触发一次清理的机会」，不是内存上界。返回 ``None`` 时
        调用方按「放弃整形」处理（bypass），与模块顶部「绝不丢消息 / 绝不拒绝」的
        承诺一致——上限守住的是内存，不是准入。
        """

        state = self._states.get(key)
        if state is None:
            if len(self._states) >= self.max_keys:
                self._prune(now)
                if len(self._states) >= self.max_keys:
                    self.bypassed_total += 1
                    log.warning(
                        "审核整形：状态桶已达上限且无法回收，本次跳过整形（判定不变）| "
                        "keys=%d/%d group=%s user=%s",
                        len(self._states),
                        self.max_keys,
                        key[0],
                        key[1],
                    )
                    return None
            # 新 key 的桶是**满的**：tat 落后 now 一个容忍度 = 前 burst 条零延迟。
            state = _KeyState(tat=now - self._tolerance, last_seen=now)
            self._states[key] = state
        state.last_seen = now
        return state

    def _prune(self, now: float) -> None:
        cutoff = now - max(
            self.spacing_seconds * (self.burst + 1),
            self.max_wait_seconds,
            self.spacing_seconds,
        )
        stale = [key for key, state in self._states.items() if state.last_seen <= cutoff]
        for key in stale:
            self._states.pop(key, None)

    def _plan(self, state: _KeyState, now: float) -> float:
        """排定本次送审时刻，返回需要等待的秒数（已按 ``max_wait_seconds`` 截断）。"""

        slot = state.tat if state.tat > now - self._tolerance else now - self._tolerance
        delay = slot - now
        if delay <= 0:
            delay = 0.0
        elif delay > self.max_wait_seconds:
            # 排队时间有上限：宁可少整形，也不能让一条消息（和一个更新 worker）
            # 无限期地等下去。
            delay = self.max_wait_seconds
            slot = now + delay
        state.tat = slot + self.spacing_seconds
        return delay

    def _warn_bypass(self, state: _KeyState, key: tuple[int, int], delay: float) -> None:
        now = self._clock()
        if now - state.warned_at < _BYPASS_WARN_INTERVAL_SECONDS:
            return
        state.warned_at = now
        log.warning(
            "审核整形：等待名额已满，本次直接送审（判定不变，只是不再整形）| "
            "group=%s user=%s planned_delay=%.1fs waiters=%d/%d",
            key[0],
            key[1],
            delay,
            self._waiters,
            self.max_waiters,
        )

    # -- 主入口 ----------------------------------------------------------- #

    async def acquire(self, group_id: int, user_id: int) -> AdmissionOutcome:
        """给一次审核申请准入。

        **不会拒绝**：返回即代表可以立刻送审（可能已经等过 delay 秒）。超预算时唯一
        的区别是"等了多久"或"是否放弃了整形"，判定内容与判定质量完全不变。
        """

        try:
            key = (int(group_id), int(user_id))
        except (TypeError, ValueError):
            return AdmissionOutcome()
        if key[0] == 0 or key[1] <= 0:
            # 没有可信发送者（系统/频道/未知）：不整形，保持原行为。
            return AdmissionOutcome()

        now = self._clock()
        state = self._state_for(key, now)
        if state is None:
            # B-05：桶已满且回收不了 → 放弃整形（不等待、不拒绝），判定内容不变。
            return AdmissionOutcome(bypassed=True)
        delay = self._plan(state, now)
        if delay <= 0:
            return AdmissionOutcome()

        self.shaped_total += 1
        if delay > self.max_observed_wait:
            self.max_observed_wait = delay
        if self.max_waiters <= 0 or self._waiters >= self.max_waiters:
            self.bypassed_total += 1
            if state is not None:
                self._warn_bypass(state, key, delay)
            return AdmissionOutcome(shaped=True, bypassed=True, planned_delay=delay)

        self._waiters += 1
        try:
            await self._sleep(delay)
        except asyncio.CancelledError:
            # 更新被取消/停机：名额一定要还回去，否则等待名额会被慢慢耗光。
            raise
        finally:
            self._waiters -= 1
        self.waited_total += 1
        log.info(
            "审核整形：为摊平连发成本延后送审 | group=%s user=%s delay=%.1fs waiters=%d/%d",
            key[0],
            key[1],
            delay,
            self._waiters,
            self.max_waiters,
        )
        return AdmissionOutcome(shaped=True, waited_seconds=delay, planned_delay=delay)

    # -- 观测 ------------------------------------------------------------- #

    def snapshot(self) -> dict[str, float]:
        """整形闸的当前状态（测试/排查用）。"""

        return {
            "tracked_keys": float(len(self._states)),
            "waiters": float(self._waiters),
            "shaped_total": float(self.shaped_total),
            "waited_total": float(self.waited_total),
            "bypassed_total": float(self.bypassed_total),
            "max_observed_wait": float(self.max_observed_wait),
            "burst": float(self.burst),
            "spacing_seconds": float(self.spacing_seconds),
            "max_wait_seconds": float(self.max_wait_seconds),
            "max_waiters": float(self.max_waiters),
        }

    def reset(self) -> None:
        """清空状态（测试与热重载用）。"""

        self._states.clear()
        self._waiters = 0
        self.shaped_total = 0
        self.waited_total = 0
        self.bypassed_total = 0
        self.max_observed_wait = 0.0


#: 进程级共享的审核准入闸（一个进程一份；测试通过 ModerationService 注入替换）。
MODERATION_ADMISSION = ModerationAdmissionGate()


#: 成本硬上限的默认窗口长度（秒）：固定窗口 = 整点滚动，每 ``window_seconds``
#: 重新计一次。取 1 小时对齐运维口径（"每群每小时最多 N 次"）。
DEFAULT_CALL_CAP_WINDOW_SECONDS = 3600.0
#: 跟踪的群数上限（超过先清过期窗口，再拒绝新群进入统计——超限群按"本次允许"
#: 处理，见 ``try_consume``：内存上界不能变成"顺手把人放过去"）。
DEFAULT_CALL_CAP_MAX_GROUPS = 4096
#: 同一个群的降级告警最小间隔，避免刷屏式连发把日志也刷爆。
_CALL_CAP_WARN_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    """一次额度判定的结果（只用于观测与测试，调用方不需要做分支）。"""

    #: 本次是否拿到了额度（False = 上限已用尽，本次不送审）
    allowed: bool = True
    #: 上限是不是配了的（False = 没配，本次不受任何限制）
    capped: bool = False
    #: 本窗口已用次数（没配上限时也照常计数，便于观测）
    used: int = 0
    #: 本次生效的上限（0 = 不限）
    limit: int = 0

    @property
    def exhausted(self) -> bool:
        return not self.allowed


class _BudgetState:
    """一个群在当前固定窗口里的已用次数。"""

    __slots__ = ("window", "used", "warned_at")

    def __init__(self, window: int) -> None:
        self.window = window
        self.used = 0
        self.warned_at = 0.0


class ModerationCallBudget:
    """每群固定窗口内的审核模型调用**总量硬上限**。

    与 :class:`ModerationAdmissionGate` 的区别只有一条，但很关键：整形闸只推迟
    送审时刻、**不限量**，所以成本放大面（成员发 N 条 = N 次调用）原封不动；这里
    限的是**总量**。

    纯内存、无锁：``try_consume`` 是同步的（没有 await 断层），单进程部署下够用。
    """

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_CALL_CAP_WINDOW_SECONDS,
        max_groups: int = DEFAULT_CALL_CAP_MAX_GROUPS,
        warn_interval_seconds: float = _CALL_CAP_WARN_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_groups = max(16, int(max_groups))
        self.warn_interval_seconds = max(0.0, float(warn_interval_seconds))
        self._clock = clock or time.monotonic
        self._states: dict[int, _BudgetState] = {}
        # 观测计数（测试与排查用）
        self.consumed_total = 0
        self.skipped_total = 0
        self.uncapped_total = 0

    # -- 状态 ------------------------------------------------------------- #

    def _window(self, now: float) -> int:
        return int(now // self.window_seconds)

    def _state_for(self, group_id: int, window: int) -> _BudgetState | None:
        state = self._states.get(group_id)
        if state is None:
            if len(self._states) >= self.max_groups:
                self._prune(window)
                if len(self._states) >= self.max_groups:
                    # 内存上界不能变成"顺手放过去"：没有状态就当这一次不参与统计。
                    return None
            state = _BudgetState(window=window)
            self._states[group_id] = state
        if state.window != window:
            state.window = window
            state.used = 0
            state.warned_at = 0.0
        return state

    def _prune(self, window: int) -> None:
        for group_id in [
            group_id
            for group_id, state in self._states.items()
            if state.window < window
        ]:
            self._states.pop(group_id, None)

    # -- 主入口 ----------------------------------------------------------- #

    def try_consume(self, group_id: int, limit: int) -> BudgetOutcome:
        """申请一次审核模型调用额度。

        ``limit <= 0`` = **不限**：直接放行（默认口径，生产行为与加这道闸之前
        逐字一致），但照样计数，方便运维先看数据再决定要不要开。
        """

        try:
            gid = int(group_id)
            cap = int(limit or 0)
        except (TypeError, ValueError):
            return BudgetOutcome()
        if gid == 0:
            # 没有可信的群 id（拿不到就不参与限流，也不参与统计）。
            return BudgetOutcome()

        now = self._clock()
        window = self._window(now)
        state = self._state_for(gid, window)
        if state is None:
            return BudgetOutcome(allowed=True, capped=cap > 0, limit=cap)

        if cap <= 0:
            state.used += 1
            self.uncapped_total += 1
            return BudgetOutcome(allowed=True, capped=False, used=state.used, limit=0)

        if state.used >= cap:
            self.skipped_total += 1
            self._warn(state=state, gid=gid, used=state.used, cap=cap, now=now)
            return BudgetOutcome(allowed=False, capped=True, used=state.used, limit=cap)

        state.used += 1
        self.consumed_total += 1
        return BudgetOutcome(allowed=True, capped=True, used=state.used, limit=cap)

    def _warn(self, *, state: _BudgetState, gid: int, used: int, cap: int, now: float) -> None:
        if now - state.warned_at < self.warn_interval_seconds:
            return
        state.warned_at = now
        log.warning(
            "审核未送审（成本上限）：本群本窗口的模型调用额度已用尽，本次只跑本地"
            "确定性规则（判定标记为 conclusive=False，不静默放行）| group=%s used=%d/%d",
            gid,
            used,
            cap,
        )

    # -- 观测 ------------------------------------------------------------- #

    def snapshot(self) -> dict[str, float]:
        """上限闸的当前状态（测试/排查用）。"""

        return {
            "tracked_groups": float(len(self._states)),
            "consumed_total": float(self.consumed_total),
            "skipped_total": float(self.skipped_total),
            "uncapped_total": float(self.uncapped_total),
            "window_seconds": float(self.window_seconds),
            "max_groups": float(self.max_groups),
        }

    def reset(self) -> None:
        """清空状态（测试与热重载用）。"""

        self._states.clear()
        self.consumed_total = 0
        self.skipped_total = 0
        self.uncapped_total = 0


#: 进程级共享的成本上限账本（一个进程一份；测试通过 ModerationService 注入替换）。
MODERATION_CALL_BUDGET = ModerationCallBudget()


__all__ = [
    "AdmissionOutcome",
    "BudgetOutcome",
    "DEFAULT_BURST",
    "DEFAULT_CALL_CAP_MAX_GROUPS",
    "DEFAULT_CALL_CAP_WINDOW_SECONDS",
    "DEFAULT_MAX_WAITERS",
    "DEFAULT_MAX_WAIT_SECONDS",
    "DEFAULT_SPACING_SECONDS",
    "MODERATION_ADMISSION",
    "MODERATION_CALL_BUDGET",
    "ModerationAdmissionGate",
    "ModerationCallBudget",
]
