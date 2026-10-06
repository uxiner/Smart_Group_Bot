"""启动时装配进程级资源（并发闸门 / 准入上限 / aiohttp 连接池）。

**热更与重启必须诚实分开。**

* 标量（价格、配额、超时、批量、阈值）走 :mod:`bot.services.policy_runtime`：
  每次动作现取，改完下一次动作生效。
* 这里的东西是**模块级单例**——``asyncio.Semaphore`` / ``ReservedCapacityGate`` /
  ``PriorityAiohttpSession`` / tokenizer 线程槽位。它们在 import 或建 Bot 时固化，
  **热替换会漏掉正在执行的请求、泄漏 slot，或让"旧闸门 + 新容量"同时存在**。
  所以它们只在**启动时**装配一次，改完必须重启；``api_document()`` 与 Mini App
  用 ``runtime_config.RESTART_REQUIRED_PATHS`` 列出这些字段名。

调用点：``bot.__main__._initialize_runtime_services``——在
``RuntimeConfigManager.initialize()`` 之后、**任何**模型预取 / Bot 构造 / 收发
消息之前执行一次。

四条不变式
----------

1. **可重复调用是安全的**：同样的值再调一次是 no-op（幂等），不新建资源、不让已
   持有的 slot 变成孤儿。
2. **部分占用也算忙。** 一个容量 3 的闸门被拿掉 1 个名额时 ``_value`` 是 2 而不是
   0；只看 ``_value <= 0`` 会放行换闸门，于是"旧闸门上还挂着 1 个持有者 + 新闸门
   满容量 5"= 有效并发 6，比配置的还大。判定看的是**有效持有数**
   （``capacity - available``）与等待者，任一大于 0 就拒绝。
3. **装配是原子的**：探测全部闸门 → 任一"需要变更且忙"的闸门整体拒绝 → 预构造所有
   新对象 → **在没有 await 的连续段里**统一提交。拒绝时进程里的对象、容量计数、
   tokenizer 槽位一个都不动。
4. **只对真的��变更的闸门报忙**：某个闸门当前没在跑、而这次配置也不改它，就不该
   因为"别的闸门忙"而被牵连。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

from bot.services import policy_runtime

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from bot.services.runtime_config import RuntimeConfig

log = logging.getLogger(__name__)

#: 串行化并发的装配调用：两个线程同时 ``apply`` 会各自探测、各自提交。
_APPLY_LOCK = threading.Lock()


class StartupResourceBusy(RuntimeError):
    """还有请求在跑时试图重配进程级闸门。"""


@dataclass(frozen=True, slots=True)
class StartupResourceReport:
    """一次装配的结果。``changed`` 为空 = 配置与当前进程已一致。"""

    changed: tuple[str, ...]
    restart_required: tuple[str, ...]

    def __bool__(self) -> bool:  # pragma: no cover - 便于调用点写断言
        return bool(self.changed)


@dataclass(frozen=True, slots=True)
class GateProbe:
    """一个闸门在某一瞬间的占用情况（既用于拒绝，也用于诊断）。"""

    name: str
    capacity: int
    available: int
    active: int
    waiters: int

    @property
    def busy(self) -> bool:
        """有任何持有者或等待者就算忙——**部分占用也算**。"""

        return self.active > 0 or self.waiters > 0

    def describe(self) -> str:
        # 刻意分三个数：旧实现把 ``_value``（= 剩余许可）标成 in_flight，占满时
        # 会打印 in_flight=0，完全误导排查。
        return (
            f"{self.name}(active={self.active}, available={self.available}, "
            f"waiters={self.waiters}, capacity={self.capacity})"
        )


def probe_semaphore(*, name: str, semaphore: Any, capacity: int) -> GateProbe:
    """探测一个 ``asyncio.Semaphore``。

    ``active = capacity - available``：这是把"部分占用"算出来的关键。只看
    ``available`` 不够（拿掉 1 个名额时它还有 2，看着"很空"）；只看 ``waiters``
    也不够（持有者不会进等待队列）。
    """

    try:
        available = int(getattr(semaphore, "_value"))
    except (TypeError, ValueError):  # pragma: no cover - 防御
        available = int(capacity)
    waiters_raw = getattr(semaphore, "_waiters", None)
    try:
        waiters = sum(1 for item in (waiters_raw or ()) if not item.done())
    except TypeError:  # pragma: no cover - 防御
        waiters = 0
    return GateProbe(
        name=name,
        capacity=int(capacity),
        available=available,
        # available 只会 <= capacity；出现负差说明我们对 capacity 的认知过时，
        # 宁可当成"忙"也不要放行一次错误的替换。
        active=max(0, int(capacity) - available),
        waiters=waiters,
    )


def probe_thread_slots(*, name: str, slots: Any, capacity: int) -> GateProbe:
    """探测 tokenizer 的 ``threading.BoundedSemaphore``（没有"等待者"概念）。"""

    try:
        available = int(getattr(slots, "_value"))
    except (TypeError, ValueError):  # pragma: no cover - 防御
        available = int(capacity)
    return GateProbe(
        name=name,
        capacity=int(capacity),
        available=available,
        active=max(0, int(capacity) - available),
        waiters=0,
    )


def tokenizer_active_count() -> int:
    """当前有多少分词线程在工作（llm 模块自己维护的活跃表，最权威）。"""

    from bot.services import llm as llm_module

    try:
        return len(llm_module._LLM_TOKENIZER_ACTIVE_STARTED)
    except Exception:  # pragma: no cover - 统计表不可用时按空闲处理
        return 0


@dataclass(frozen=True, slots=True)
class _GateSpec:
    """一个模块级闸门：容量常量 + 信号量属性，够装配与探测用了。"""

    label: str
    path: str
    module_path: str
    capacity_attr: str
    semaphore_attr: str
    thread_slots: bool = False

    def module(self):
        return import_module(self.module_path)

    def capacity(self) -> int:
        return int(getattr(self.module(), self.capacity_attr))

    def semaphore(self) -> Any:
        return getattr(self.module(), self.semaphore_attr)

    def probe(self) -> GateProbe:
        if self.thread_slots:
            return probe_thread_slots(
                name=self.label, slots=self.semaphore(), capacity=self.capacity()
            )
        return probe_semaphore(
            name=self.label, semaphore=self.semaphore(), capacity=self.capacity()
        )

    def build(self, value: int) -> Any:
        import asyncio

        if self.thread_slots:
            return threading.BoundedSemaphore(int(value))
        return asyncio.Semaphore(int(value))

    def commit(self, capacity: int, semaphore: Any) -> None:
        module = self.module()
        setattr(module, self.capacity_attr, int(capacity))
        setattr(module, self.semaphore_attr, semaphore)


#: 除了 LLM 主闸门以外的所有模块级闸门。
_GATE_SPECS: tuple[_GateSpec, ...] = (
    _GateSpec(
        "llm_tokenizer",
        "resources.llm_tokenizer_concurrency",
        "bot.services.llm",
        "_LLM_TOKENIZER_THREAD_CAPACITY",
        "_LLM_TOKENIZER_THREAD_SLOTS",
        thread_slots=True,
    ),
    _GateSpec(
        "pending_reply",
        "resources.pending_reply_execution_capacity",
        "bot.handlers.group",
        "_PENDING_REPLY_EXECUTION_CAPACITY",
        "_PENDING_REPLY_EXECUTION_SEMAPHORE",
    ),
    _GateSpec(
        "tts_synthesis",
        "resources.tts_synthesis_concurrency",
        "bot.services.doubao_tts",
        "_TTS_SYNTHESIS_CONCURRENCY",
        "_TTS_SYNTHESIS_SEMAPHORE",
    ),
    _GateSpec(
        "tts_transcode",
        "resources.tts_transcode_concurrency",
        "bot.services.doubao_tts",
        "_TTS_TRANSCODE_CONCURRENCY",
        "_TTS_TRANSCODE_SEMAPHORE",
    ),
    _GateSpec(
        "tts_private",
        "resources.tts_private_concurrency",
        "bot.services.private_tts",
        "PRIVATE_TTS_CONCURRENCY",
        "_private_tts_semaphore",
    ),
    _GateSpec(
        "av_query",
        "resources.av_query_concurrency",
        "bot.services.av_search",
        "_AV_QUERY_CONCURRENT",
        "_AV_QUERY_SEMAPHORE",
    ),
)

_LLM_SEMAPHORE_SPEC = _GateSpec(
    "llm_request_semaphore",
    "resources.llm_request_capacity",
    "bot.services.llm",
    "_LLM_REQUEST_CAPACITY",
    "_LLM_REQUEST_SEMAPHORE",
)


def llm_priority_gate_probe() -> GateProbe:
    """探测 ``ReservedCapacityGate``：它自己知道 active / waiting，比信号量准。"""

    from bot.services import llm as llm_module

    snapshot = llm_module._LLM_PRIORITY_GATE.snapshot()
    capacity = int(llm_module._LLM_PRIORITY_GATE.total_capacity)
    active = sum(
        int(snapshot.get(key) or 0)
        for key in ("active_critical", "active_high", "active_normal", "active_background")
    )
    waiters = sum(
        int(snapshot.get(key) or 0)
        for key in (
            "waiting_critical",
            "waiting_high",
            "waiting_normal",
            "waiting_background",
        )
    )
    return GateProbe(
        name="llm_priority_gate",
        capacity=capacity,
        active=active,
        waiters=waiters,
        available=max(0, capacity - active),
    )


def _desired_capacities(resources: Any) -> dict[str, int]:
    return {
        "llm_request_semaphore": int(resources.llm_request_capacity),
        "llm_priority_gate": int(resources.llm_request_capacity),
        "llm_tokenizer": int(resources.llm_tokenizer_concurrency),
        "pending_reply": int(resources.pending_reply_execution_capacity),
        "tts_synthesis": int(resources.tts_synthesis_concurrency),
        "tts_transcode": int(resources.tts_transcode_concurrency),
        "tts_private": int(resources.tts_private_concurrency),
        "av_query": int(resources.av_query_concurrency),
    }


def _check_llm_reserved_capacity(resources: Any) -> None:
    """保留容量的不变式。配置层已经校验过，这里再挡一次并给人话。"""

    total = int(resources.llm_request_capacity)
    noncritical = int(resources.llm_request_noncritical_capacity)
    normal = int(resources.llm_request_normal_capacity)
    background = int(resources.llm_request_background_capacity)
    if not (1 <= background <= normal - 2):
        raise ValueError("LLM 背景容量必须满足 1 ≤ background ≤ normal − 2")
    if not (1 <= normal <= noncritical < total):
        raise ValueError(
            "LLM 容量必须满足 1 ≤ normal ≤ noncritical < total（至少留 1 个关键名额）"
        )


def apply_startup_resources(config: "RuntimeConfig") -> StartupResourceReport:
    """按已保存的运行时配置装配进程级资源。

    在 ``RuntimeConfigManager.initialize()`` 之后、任何预取/收发之前调用一次。
    同样的配置再调一次是 no-op（``changed`` 为空）。

    有活动请求时抛 :class:`StartupResourceBusy`，而且**一个对象都不改**——先探测
    全部闸门，再预构造，最后在没有 await 的连续段里统一提交。
    """

    import asyncio

    from bot.services import llm as llm_module
    from bot.services.request_priority import ReservedCapacityGate
    from bot.services.runtime_config import RESTART_REQUIRED_PATHS

    resources = config.resources
    _check_llm_reserved_capacity(resources)

    with _APPLY_LOCK:
        wanted = _desired_capacities(resources)
        specs = (_LLM_SEMAPHORE_SPEC, *_GATE_SPECS)
        gate_specs = {spec.label: spec for spec in specs}
        gate_capacity = llm_module._LLM_PRIORITY_GATE.total_capacity

        # ---- 第 1 遍：只探测，不改任何东西 ---------------------------------
        probes: list[GateProbe] = [llm_priority_gate_probe()]
        probes.extend(spec.probe() for spec in specs)

        # ---- 只对"真的要换 且 忙"的闸门报忙 --------------------------------
        busy: list[GateProbe] = []
        for probe in probes:
            desired = wanted.get(probe.name)
            if desired is None or desired == probe.capacity:
                continue  # 这次不改它 → 它忙不忙与本次无关
            if probe.busy:
                busy.append(probe)
        if busy:
            raise StartupResourceBusy(
                "还有活动请求，拒绝重配："
                + "；".join(probe.describe() for probe in busy)
            )

        # ---- 第 2 遍：预构造（仍然不改任何引用）----------------------------
        staged: list[tuple[_GateSpec, int, Any]] = []
        for spec in specs:
            target = wanted[spec.label]
            if target != spec.capacity():
                staged.append((spec, target, spec.build(target)))
        new_gate = (
            ReservedCapacityGate(
                total_capacity=resources.llm_request_capacity,
                noncritical_capacity=resources.llm_request_noncritical_capacity,
                normal_capacity=resources.llm_request_normal_capacity,
                background_capacity=resources.llm_request_background_capacity,
            )
            if resources.llm_request_capacity != gate_capacity
            else None
        )

        # tokenizer 的活跃数会被别的线程改变：提交前再确认一次。
        if any(spec.thread_slots for spec, _target, _obj in staged):
            active_now = tokenizer_active_count()
            if active_now:
                raise StartupResourceBusy(
                    f"llm_tokenizer(active={active_now}, waiters=0) 还有分词线程在跑，"
                    "拒绝重配分词槽位"
                )

        # ---- 第 3 遍：统一提交。**这一段里没有 await** ----------------------
        changed: list[str] = []
        if new_gate is not None:
            llm_module._LLM_PRIORITY_GATE = new_gate
            changed.extend(
                (
                    "resources.llm_request_capacity",
                    "resources.llm_request_noncritical_capacity",
                    "resources.llm_request_normal_capacity",
                    "resources.llm_request_background_capacity",
                )
            )
        for spec, target, semaphore in staged:
            spec.commit(target, semaphore)
            changed.append(gate_specs[spec.label].path)

    # 装配成功 = "本进程现在跑的就是这份配置"，**无论这次有没有改动**。把 restart
    # 字段的运行基线同步过去，否则 ``restart_pending`` 与各冷读侧会一直对照着上一次
    # 启动时的旧值。（webhook 的 worker/队列容量与 Telegram 三个容量就是靠这一步成为
    # "已装配"的；no-op 调用同样要重基线，否则测试里"恢复默认"之后基线还停在旧值。）
    from bot.services.runtime_config import record_applied_restart_values

    record_applied_restart_values(config)
    if changed:
        log.warning(
            "启动资源已按运行时配置装配（这些字段改完需要重启才生效） | %s",
            "、".join(sorted(set(changed))),
        )
    return StartupResourceReport(
        changed=tuple(sorted(set(changed))),
        restart_required=RESTART_REQUIRED_PATHS,
    )


def gate_snapshot() -> dict[str, dict[str, int]]:
    """每个闸门当前的 active / available / waiters（诊断用，可核验）。

    刻意分三个数：只看"剩余许可"会在占满时打印 0，只看 waiters 又看不到持有者。
    """

    from bot.services import llm as llm_module

    snapshot = {"llm_priority_gate": llm_priority_gate_probe()}
    for spec in _GATE_SPECS:
        snapshot[spec.label] = spec.probe()
    snapshot["llm_request_semaphore"] = probe_semaphore(
        name=_LLM_SEMAPHORE_SPEC.label,
        semaphore=llm_module._LLM_REQUEST_SEMAPHORE,
        capacity=llm_module._LLM_REQUEST_CAPACITY,
    )
    return {
        name: {
            "capacity": probe.capacity,
            "active": probe.active,
            "available": probe.available,
            "waiters": probe.waiters,
        }
        for name, probe in snapshot.items()
    }


def resource_health_report() -> dict[str, Any]:
    """当前进程级资源的实际容量（诊断用；不读配置，只读模块真值）。"""

    from bot.handlers import group as group_module
    from bot.services import av_search, doubao_tts, llm as llm_module, private_tts

    return {
        "llm": {
            "capacity": llm_module._LLM_REQUEST_CAPACITY,
            "priority_gate": llm_module._LLM_PRIORITY_GATE.snapshot(),
            "tokenizer_threads": llm_module._LLM_TOKENIZER_THREAD_CAPACITY,
            "tokenizer_active": tokenizer_active_count(),
        },
        "pending_reply": group_module._PENDING_REPLY_EXECUTION_CAPACITY,
        "tts_synthesis": doubao_tts._TTS_SYNTHESIS_CONCURRENCY,
        "tts_transcode": doubao_tts._TTS_TRANSCODE_CONCURRENCY,
        "tts_private": private_tts.PRIVATE_TTS_CONCURRENCY,
        "av_query": av_search._AV_QUERY_CONCURRENT,
    }


def telegram_session_limits() -> dict[str, float | int]:
    """Telegram 出站闸门参数（建 ``PriorityAiohttpSession`` 时读一次）。

    这些都是 restart 字段，所以读的是**本进程启动时装配的那一份**
    （:func:`runtime_config.applied_restart_values`），不是 Mini App 里刚保存的
    期望值——保存成功 ≠ 立刻生效，页面会把它列进 ``restart_pending``。
    """

    from bot.services.runtime_config import applied_restart_value

    resources = policy_runtime.resources_policy()
    return {
        # 三个容量是 restart：连接池与准入闸门在建 Session 时固化。
        "total_capacity": int(
            applied_restart_value("resources.telegram_total_capacity", 64)
        ),
        "noncritical_capacity": int(
            applied_restart_value("resources.telegram_noncritical_capacity", 60)
        ),
        "normal_capacity": int(
            applied_restart_value("resources.telegram_normal_capacity", 44)
        ),
        # 四个超时是 hot：每次请求现读（``PriorityAiohttpSession`` 内部调用）。
        "privileged_timeout_seconds": float(resources.telegram_privileged_timeout_seconds),
        "critical_admission_timeout_seconds": float(
            resources.telegram_critical_admission_timeout_seconds
        ),
        "high_admission_timeout_seconds": float(
            resources.telegram_high_admission_timeout_seconds
        ),
        "normal_admission_timeout_seconds": float(
            resources.telegram_normal_admission_timeout_seconds
        ),
    }
