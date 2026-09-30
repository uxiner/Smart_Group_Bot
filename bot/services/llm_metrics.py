"""进程内 LLM 用量累加器：供成本看板（``/cost``、周报）读取。

设计约束（比功能更重要）：

* **绝不阻塞回复路径**。SQLite 写入放在内存累加之后，由 60 秒的惰性定时器
  落盘一次；调用方只是加几个整数。
* **绝不抛异常**。埋点在 LLM 客户端的主路径上，任何统计失败都必须被吞掉，
  否则会变成"统计把机器人搞挂了"这种最糟糕的故障。
* **不丢计数**。落盘时先把内存里的批次取走，写库成功后丢弃；写库失败就把
  批次并回内存，下一轮再试。

``usage_date`` 用 Asia/Shanghai 的自然日，和 ``member_checkins.checkin_date``
保持同一口径，日报/周报才对得上。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.db.models import LlmUsageDaily
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

#: 落盘间隔（秒）。越短越实时，越长越省写入；60 秒丢数据的窗口可以接受。
FLUSH_INTERVAL_SECONDS = 60.0

#: 计数列名（也是数据库列名），顺序无所谓。
COUNTER_FIELDS: tuple[str, ...] = (
    "calls",
    "prompt_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "thinking_tokens",
    "empty_responses",
    "timeouts",
    "failures",
    "parse_errors",
)

_RETENTION_DAYS = 3  # 内存里最多留几天（正常 60 秒就落盘了，这是兜底）

_lock = threading.Lock()
_counters: dict[tuple[str, str], dict[str, int]] = {}
_session_factory: Any = None
_last_flush = 0.0
_flushing = False


def configure(session_factory: Any) -> None:
    """由启动流程注入会话工厂。未注入时只累加、不落盘。"""
    global _session_factory
    _session_factory = session_factory


def _today() -> str:
    try:
        return now_shanghai_naive().date().isoformat()
    except Exception:  # pragma: no cover - 时区工具不可用时退回 UTC 日期
        return date.today().isoformat()


def record(stage: str, **deltas: int) -> None:
    """累加一次事件。未知字段忽略，异常一律吞掉。"""
    try:
        keep: dict[str, int] = {}
        for name, value in deltas.items():
            if name not in COUNTER_FIELDS:
                continue
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                continue  # 坏值只丢这一个字段，不能把整次调用的计数也扔掉
            if number:
                keep[name] = number
        if not keep:
            return
        key = (_today(), str(stage or "")[:24])
        with _lock:
            bucket = _counters.setdefault(key, {})
            for name, value in keep.items():
                bucket[name] = bucket.get(name, 0) + value
            _prune_locked()
        _maybe_schedule_flush()
    except Exception:  # pragma: no cover - 统计绝不冒泡
        log.debug("llm_metrics.record failed | stage=%s", stage, exc_info=True)


def record_usage(
    stage: str,
    usage: Any,
    *,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """把一次成功响应的 usage 记进账。缺字段按 0 处理。"""
    try:
        def num(*names: str) -> int:
            for name in names:
                value = getattr(usage, name, None)
                if value is None and isinstance(usage, dict):
                    value = usage.get(name)
                try:
                    if value is not None:
                        return int(value)
                except (TypeError, ValueError):
                    continue
            return 0

        in_tokens = num("prompt_tokens") or int(prompt_tokens or 0)
        out_tokens = num("completion_tokens") or int(output_tokens or 0)
        cached = max(num("cached_tokens"), num("cache_read_tokens"))
        record(
            stage,
            calls=1,
            prompt_tokens=in_tokens,
            output_tokens=out_tokens,
            cached_tokens=cached,
            # 注意名字要和 LLMService._coerce_usage 产出的字段一致
            # （cache_creation_input_tokens），否则这里会永远记成 0
            cache_write_tokens=num("cache_creation_input_tokens", "cache_write_tokens"),
            thinking_tokens=num("thinking_tokens", "reasoning_tokens"),
        )
    except Exception:  # pragma: no cover
        log.debug("llm_metrics.record_usage failed | stage=%s", stage, exc_info=True)


def _prune_locked() -> None:
    """兜底：未配置会话工厂时别让内存无限长。"""
    if len(_counters) <= 64:
        return
    cutoff = (_today(), "")
    alive = [k for k in _counters if k[0] >= cutoff[0]]
    if len(alive) == len(_counters):
        return
    oldest = sorted({k[0] for k in _counters})[: max(1, len({k[0] for k in _counters}) - _RETENTION_DAYS)]
    for day in oldest:
        for key in [k for k in _counters if k[0] == day]:
            _counters.pop(key, None)


def _maybe_schedule_flush() -> None:
    """到期就在事件循环里排一次落盘（没有循环就等下一轮）。"""
    global _last_flush, _flushing
    if _session_factory is None or _flushing:
        return
    if time.monotonic() - _last_flush < FLUSH_INTERVAL_SECONDS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _flushing = True
    try:
        loop.create_task(flush())
    except Exception:  # pragma: no cover
        _flushing = False


async def flush(*, force: bool = False) -> int:
    """把内存里的计数并进 ``llm_usage_daily``。返回落盘的行数。"""
    global _last_flush, _flushing
    batch: dict[tuple[str, str], dict[str, int]] = {}
    try:
        if _session_factory is None:
            return 0
        if not force and time.monotonic() - _last_flush < FLUSH_INTERVAL_SECONDS:
            return 0
        with _lock:
            batch = dict(_counters)
            _counters.clear()
        _last_flush = time.monotonic()
        if not batch:
            return 0
        rows = [
            {"usage_date": day, "stage": stage, **{f: int(vals.get(f, 0)) for f in COUNTER_FIELDS}}
            for (day, stage), vals in batch.items()
        ]
        async with _session_factory() as session:
            for row in rows:
                stmt = sqlite_insert(LlmUsageDaily).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["usage_date", "stage"],
                    set_={
                        field: getattr(LlmUsageDaily, field) + getattr(stmt.excluded, field)
                        for field in COUNTER_FIELDS
                    }
                    | {"updated_at": func.now()},
                )
                await session.execute(stmt)
            await session.commit()
        return len(rows)
    except Exception:
        # 写库失败不能丢计数：并回内存，下一轮再试
        try:
            with _lock:
                for key, vals in batch.items():
                    bucket = _counters.setdefault(key, {})
                    for name, value in vals.items():
                        bucket[name] = bucket.get(name, 0) + value
        except Exception:  # pragma: no cover
            pass
        log.warning("llm_metrics.flush failed", exc_info=True)
        return 0
    finally:
        _flushing = False


def snapshot() -> dict[tuple[str, str], dict[str, int]]:
    """当前内存计数（测试与自检用）。"""
    with _lock:
        return {k: dict(v) for k, v in _counters.items()}


def reset() -> None:
    """清空内存计数与配置（测试用）。"""
    global _session_factory, _last_flush, _flushing
    with _lock:
        _counters.clear()
    _session_factory = None
    _last_flush = 0.0
    _flushing = False


def window_start(days: int, *, today: date | None = None) -> str:
    """返回窗口起点日期（含当天）：``days=7`` → 今天往前 6 天。"""
    days = max(1, int(days))
    base = today or now_shanghai_naive().date()
    return (base - timedelta(days=days - 1)).isoformat()
