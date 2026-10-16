"""第三期：素材识图入库的后台任务注册表与关机收敛。

主处理链路（审核 → 文字生成 → 回复）**绝不能**等待识图。所以素材入库是一个
独立的 ``asyncio.Task``，注册在这里，随应用关停一起取消/等待。

与 :mod:`bot.services.vote_ban` 的后台任务模式保持一致：模块级注册表 +
``_consume_*_result`` 吞掉异常 + 一个 ``flush_*`` 函数交给 ``bot/__main__.py``
的关机序列调用。

记账红线：即使 worker 内部任何一步抛异常，也只在 ``_consume_result`` 里记录，
绝不向事件循环抛 "Task exception was never retrieved"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.media_ingestion import ingest_media_job

log = logging.getLogger(__name__)

#: 同一张图（``group_id`` + ``file_unique_id``）同时只跑一个入库任务。
_media_ingest_tasks: dict[tuple[int, str], asyncio.Task[Any]] = {}

#: 没有 ``file_unique_id`` 的任务（理论上极少）按对象 id 去重。
_anonymous_tasks: dict[int, asyncio.Task[Any]] = {}

#: 并发上限：素材入库是"锦上添花"，不允许它挤占审核/回复的资源。
MAX_CONCURRENT_INGEST = 8
MAX_CONCURRENT_INGEST_PER_GROUP = 2


def _consume_media_task_result(task: asyncio.Task[Any]) -> None:
    """Drop a finished task from the registries and swallow its exception."""
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - ``task.exception()`` does not raise
        log.exception("media ingest task result inspection failed")
    for key, value in list(_media_ingest_tasks.items()):
        if value is task:
            _media_ingest_tasks.pop(key, None)
    for key, value in list(_anonymous_tasks.items()):
        if value is task:
            _anonymous_tasks.pop(key, None)


def _group_running_count(group_id: int) -> int:
    return sum(
        1
        for (task_group_id, _unique_id), task in _media_ingest_tasks.items()
        if task_group_id == group_id and not task.done()
    )


def submit_media_ingest(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
    group_id: int,
    message: Message,
    llm: Any,
    file_unique_id: str = "",
    library: Any = None,
) -> bool:
    """Schedule one background ingestion job and return immediately.

    Returns ``True`` when a new task was created, ``False`` when the job was
    skipped (already running, no session factory, or concurrency cap reached).
    This function never awaits, so the caller's reply path is not delayed.

    ``library`` is an optional material-library override (a
    :class:`bot.services.sticker_library.StickerLibrary`); when omitted the
    worker uses the module singleton. Injecting it lets the caller own the
    dependency and keeps tests from patching module globals.
    """
    if session_factory is None or llm is None:
        return False
    group = int(group_id or 0)
    if group == 0:
        return False

    unique_id = (file_unique_id or "").strip()
    if unique_id:
        existing = _media_ingest_tasks.get((group, unique_id))
        if existing is not None and not existing.done():
            return False
    if len(_media_ingest_tasks) >= MAX_CONCURRENT_INGEST:
        log.info("[%s] media ingest skipped | reason=global_cap", group)
        return False
    if _group_running_count(group) >= MAX_CONCURRENT_INGEST_PER_GROUP:
        log.info("[%s] media ingest skipped | reason=group_cap", group)
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. a synchronous test double): nothing to schedule.
        return False

    async def _runner() -> None:
        await ingest_media_job(
            session_factory=session_factory,
            group_id=group,
            message=message,
            llm=llm,
            telegram_message_id=int(getattr(message, "message_id", 0) or 0),
            library=library,
        )

    task = loop.create_task(_runner(), name=f"media_ingest:{group}:{unique_id[:16] or '-'}")
    if unique_id:
        _media_ingest_tasks[(group, unique_id)] = task
    else:
        _anonymous_tasks[id(task)] = task
    task.add_done_callback(_consume_media_task_result)
    return True


async def flush_media_ingest_tasks(*, timeout_seconds: float = 15.0) -> None:
    """Cancel every live ingestion task and wait for it to converge.

    Mirrors ``flush_vote_ban_tasks``: pending tasks after the timeout are
    reported (not silently leaked) and the registries are defensively cleared.
    """
    tasks = [task for task in _media_ingest_tasks.values() if not task.done()]
    tasks.extend(task for task in _anonymous_tasks.values() if not task.done())
    if not tasks:
        _media_ingest_tasks.clear()
        _anonymous_tasks.clear()
        return

    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout_seconds)))
    for task in done:
        _consume_media_task_result(task)
    if pending:
        log.warning(
            "%d media ingest task(s) ignored shutdown cancellation",
            len(pending),
        )
        for task in pending:
            _consume_media_task_result(task)
    _media_ingest_tasks.clear()
    _anonymous_tasks.clear()


def running_media_ingest_tasks() -> int:
    """Number of live ingestion tasks (introspection/tests only)."""
    return sum(1 for task in _media_ingest_tasks.values() if not task.done()) + sum(
        1 for task in _anonymous_tasks.values() if not task.done()
    )
