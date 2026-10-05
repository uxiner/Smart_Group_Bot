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

三条不变式
----------

1. **可重复调用是安全的**：同样的值再调一次是 no-op（幂等），不新建资源、不让已
   持有的 slot 变成孤儿。
2. **有活动任务时拒绝重配**：任何一个闸门里还有在跑的请求 / 等待者时抛
   :class:`StartupResourceBusy`——不硬拆闸门，不制造"一半旧一半新"的状态。
3. **容量关系在 schema 里已校验**（关键名额 ≥1、背景 ≤ 普通 − 2 等，见
   ``ResourceSettingsConfig._validate_reserved_capacity``）。这里再校验一次只是为了
   在装配瞬间给运维一句人话。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bot.services import policy_runtime

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from bot.services.runtime_config import RuntimeConfig

log = logging.getLogger(__name__)


class StartupResourceBusy(RuntimeError):
    """还有请求在跑时试图重配进程级闸门。"""


@dataclass(frozen=True, slots=True)
class StartupResourceReport:
    """一次装配的结果。``changed`` 为空 = 配置与当前进程已一致。"""

    changed: tuple[str, ...]
    restart_required: tuple[str, ...]

    def __bool__(self) -> bool:  # pragma: no cover - 便于调用点写断言
        return bool(self.changed)


def _busy_reason(semaphore: Any, *, name: str) -> str | None:
    """信号量里还有在跑的请求 / 等待者吗？（有就不能换闸门）"""

    try:
        value = int(getattr(semaphore, "_value"))
    except (TypeError, ValueError):
        return None
    waiters = getattr(semaphore, "_waiters", None)
    try:
        waiting = sum(1 for item in (waiters or ()) if not item.done())
    except TypeError:  # pragma: no cover - 防御
        waiting = 0
    if value <= 0 or waiting:
        return f"{name}(in_flight={value}, waiters={waiting})"
    return None


def _check_reserved_capacity(
    *, total: int, noncritical: int, normal: int, background: int
) -> None:
    if not (1 <= background <= normal - 2):
        raise ValueError("LLM 背景容量必须满足 1 ≤ background ≤ normal − 2")
    if not (1 <= normal <= noncritical < total):
        raise ValueError(
            "LLM 容量必须满足 1 ≤ normal ≤ noncritical < total（至少留 1 个关键名额）"
        )


def apply_startup_resources(config: "RuntimeConfig") -> StartupResourceReport:
    """按已保存的运行时配置装配进程级资源。

    在 ``RuntimeConfigManager.initialize()`` 之后、任何预取/收发之前调用一次。
    同样的配置再调一次是 no-op（``changed`` 为空）。有活动请求时抛
    :class:`StartupResourceBusy` 而不是硬拆闸门。
    """

    import asyncio

    from bot.handlers import group as group_module
    from bot.services import av_search, doubao_tts, llm as llm_module, private_tts
    from bot.services.request_priority import ReservedCapacityGate
    from bot.services.runtime_config import RESTART_REQUIRED_PATHS

    resources = config.resources
    changed: list[str] = []

    # --- LLM 准入闸门 ------------------------------------------------------
    _check_reserved_capacity(
        total=resources.llm_request_capacity,
        noncritical=resources.llm_request_noncritical_capacity,
        normal=resources.llm_request_normal_capacity,
        background=resources.llm_request_background_capacity,
    )
    if (
        llm_module._LLM_PRIORITY_GATE.total_capacity,
        llm_module._LLM_PRIORITY_GATE.noncritical_capacity,
        llm_module._LLM_PRIORITY_GATE.normal_capacity,
        llm_module._LLM_PRIORITY_GATE.background_capacity,
    ) != (
        resources.llm_request_capacity,
        resources.llm_request_noncritical_capacity,
        resources.llm_request_normal_capacity,
        resources.llm_request_background_capacity,
    ):
        busy = _busy_reason(llm_module._LLM_REQUEST_SEMAPHORE, name="llm_gate")
        if busy is not None:
            raise StartupResourceBusy(f"LLM 准入闸门还有活动请求，拒绝重配：{busy}")
        llm_module._LLM_REQUEST_CAPACITY = resources.llm_request_capacity
        llm_module._LLM_REQUEST_SEMAPHORE = asyncio.Semaphore(
            resources.llm_request_capacity
        )
        llm_module._LLM_PRIORITY_GATE = ReservedCapacityGate(
            total_capacity=resources.llm_request_capacity,
            noncritical_capacity=resources.llm_request_noncritical_capacity,
            normal_capacity=resources.llm_request_normal_capacity,
            background_capacity=resources.llm_request_background_capacity,
        )
        changed.extend(
            (
                "resources.llm_request_capacity",
                "resources.llm_request_noncritical_capacity",
                "resources.llm_request_normal_capacity",
                "resources.llm_request_background_capacity",
            )
        )

    if llm_module._LLM_TOKENIZER_THREAD_CAPACITY != resources.llm_tokenizer_concurrency:
        import threading

        llm_module._LLM_TOKENIZER_THREAD_CAPACITY = resources.llm_tokenizer_concurrency
        llm_module._LLM_TOKENIZER_THREAD_SLOTS = threading.BoundedSemaphore(
            resources.llm_tokenizer_concurrency
        )
        changed.append("resources.llm_tokenizer_concurrency")

    # --- 其余模块级闸门 ----------------------------------------------------
    changed.extend(
        _reconfigure_gate(
            module=group_module,
            current=int(group_module._PENDING_REPLY_EXECUTION_CAPACITY),
            capacity=resources.pending_reply_execution_capacity,
            attribute="_PENDING_REPLY_EXECUTION_SEMAPHORE",
            path="resources.pending_reply_execution_capacity",
            label="群待回复执行",
        )
    )
    for module, current, capacity, attribute, path, label in (
        (
            doubao_tts,
            int(doubao_tts._TTS_SYNTHESIS_CONCURRENCY),
            resources.tts_synthesis_concurrency,
            "_TTS_SYNTHESIS_SEMAPHORE",
            "resources.tts_synthesis_concurrency",
            "TTS 合成",
        ),
        (
            doubao_tts,
            int(doubao_tts._TTS_TRANSCODE_CONCURRENCY),
            resources.tts_transcode_concurrency,
            "_TTS_TRANSCODE_SEMAPHORE",
            "resources.tts_transcode_concurrency",
            "TTS 转码",
        ),
        (
            private_tts,
            int(private_tts.PRIVATE_TTS_CONCURRENCY),
            resources.tts_private_concurrency,
            "_private_tts_semaphore",
            "resources.tts_private_concurrency",
            "私聊 TTS",
        ),
        (
            av_search,
            int(av_search._AV_QUERY_CONCURRENCY),
            resources.av_query_concurrency,
            "_AV_QUERY_SEMAPHORE",
            "resources.av_query_concurrency",
            "AV 查询",
        ),
    ):
        changed.extend(
            _reconfigure_gate(
                module=module,
                current=current,
                capacity=capacity,
                attribute=attribute,
                path=path,
                label=label,
            )
        )

    if changed:
        log.warning(
            "启动资源已按运行时配置装配（这些字段改完需要重启才生效） | %s",
            "、".join(changed),
        )
    return StartupResourceReport(
        changed=tuple(changed), restart_required=RESTART_REQUIRED_PATHS
    )


def _reconfigure_gate(
    *,
    module: Any,
    current: int,
    capacity: int,
    attribute: str,
    path: str,
    label: str,
) -> tuple[str, ...]:
    """单个模块级闸门的重配；容量没变就是 no-op。"""

    if int(current) == int(capacity):
        return ()
    import asyncio

    semaphore = getattr(module, attribute)
    busy = _busy_reason(semaphore, name=label)
    if busy is not None:
        raise StartupResourceBusy(f"{label}闸门还有活动请求，拒绝重配：{busy}")
    setattr(module, attribute, asyncio.Semaphore(int(capacity)))
    return (path,)


def resource_health_report() -> dict[str, Any]:
    """当前进程级资源的实际容量（诊断用；不读配置，只读模块真值）。"""

    from bot.handlers import group as group_module
    from bot.services import av_search, doubao_tts, llm as llm_module, private_tts

    return {
        "llm": {
            "capacity": llm_module._LLM_REQUEST_CAPACITY,
            "priority_gate": llm_module._LLM_PRIORITY_GATE.snapshot(),
            "tokenizer_threads": llm_module._LLM_TOKENIZER_THREAD_CAPACITY,
        },
        "pending_reply": group_module._PENDING_REPLY_EXECUTION_CAPACITY,
        "tts_synthesis": doubao_tts._TTS_SYNTHESIS_CONCURRENCY,
        "tts_transcode": doubao_tts._TTS_TRANSCODE_CONCURRENCY,
        "tts_private": private_tts.PRIVATE_TTS_CONCURRENCY,
        "av_query": av_search._AV_QUERY_CONCURRENCY,
    }


def telegram_session_limits() -> dict[str, float | int]:
    """Telegram 出站闸门参数（建 ``PriorityAiohttpSession`` 时读一次）。"""

    resources = policy_runtime.resources_policy()
    return {
        "total_capacity": resources.telegram_total_capacity,
        "noncritical_capacity": resources.telegram_noncritical_capacity,
        "normal_capacity": resources.telegram_normal_capacity,
        "privileged_timeout_seconds": resources.telegram_privileged_timeout_seconds,
        "critical_admission_timeout_seconds": (
            resources.telegram_critical_admission_timeout_seconds
        ),
        "high_admission_timeout_seconds": (
            resources.telegram_high_admission_timeout_seconds
        ),
        "normal_admission_timeout_seconds": (
            resources.telegram_normal_admission_timeout_seconds
        ),
    }
