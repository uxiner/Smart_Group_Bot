"""审核送审的按 (群, 成员) 整形闸（F-021）。

为什么需要
----------
每条可审消息都会触发一次审核模型调用（边缘 ban 判定再加一次；每张图再加一次视觉
调用）。全局 8 槽信号量只限制**并发**、不限制**总量**：一个成员在几秒内连发 N 条
消息，就能在瞬间吃掉全部审核槽并把 N 次调用强加给上游，成本随消息条数线性放大；
pending-reply debounce 只合并出站回复，不合并审核。

口径（比"省钱"更重要）
--------------------
* **绝不用精度换成本**：超限只影响"什么时候送审"，不影响"送不送"、更不影响"送什么"。
  这里没有、也不会有"超限就跳过大模型、退化成本地正则"的分支——那是拿误判率换钱。
* **绝不丢消息**：每条消息都会拿到一次完整的模型判定，只是送审时刻被整形。
* **正常聊天完全不受影响**：桶初始是满的（``burst`` 条零延迟），一个成员要在
  ``spacing_seconds`` 秒内发超过 ``burst`` 条才会开始等待；单条消息的额外延迟
  上限是 ``max_wait_seconds``。
* 判定为**非成员触发**的路径（人工申诉复核、资料巡检、入群筛查、/report）默认
  完全不经过这个闸，见 ``ModerationService.evaluate`` 的 ``sender_id``。

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
  （与 ``bot.services.av_image_lookup.AVPrivateRateLimiter`` 同样的做法）。
* 总调用数**不变**：本批选的是报告允许的"排队/串行整形"方向，把成本从"同一瞬间的
  并发峰值"摊到时间轴上；真正的总量下降要靠"合并送审 + 命中后逐条复核归属"，那需要
  重写群消息的处置归属，风险过高，本批不做（见提交说明，如实标注）。
* 安全代价：被整形的第 4 条及以后的消息会晚 4-6 秒被处置（违规内容多停留几秒）。
  这与 pending-reply debounce（``inbound_debounce_seconds`` 默认 5s）同一量级，
  且本地确定性命中的消息零延迟、不排队。
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

    def _state_for(self, key: tuple[int, int], now: float) -> _KeyState:
        state = self._states.get(key)
        if state is None:
            if len(self._states) >= self.max_keys:
                self._prune(now)
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
        delay = self._plan(self._state_for(key, now), now)
        if delay <= 0:
            return AdmissionOutcome()

        self.shaped_total += 1
        if delay > self.max_observed_wait:
            self.max_observed_wait = delay
        state = self._states.get(key)
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


__all__ = [
    "AdmissionOutcome",
    "DEFAULT_BURST",
    "DEFAULT_MAX_WAITERS",
    "DEFAULT_MAX_WAIT_SECONDS",
    "DEFAULT_SPACING_SECONDS",
    "MODERATION_ADMISSION",
    "ModerationAdmissionGate",
]
