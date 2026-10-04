from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Group

log = logging.getLogger(__name__)


def _is_benign_group_race(pending: list[Any], detail: str) -> bool:
    """这次 ``IntegrityError`` 是不是「两个 update 抢着建同一个群」的良性竞态？

    ``commit()`` 失败意味着**整个 session 事务**失败，回滚丢掉的是这次 update 里
    handler 累积的**全部**写入（群设置、管理员、名册、积分流水……），而不只是
    ``groups`` 那一行。handler 在此之前已经把「已生效」的 Telegram 回复发出去
    了，所以吞掉异常等于制造「外部已承诺、内部已回滚」的状态不一致。

    判据（比「错误串里有没有 groups.id」严格得多）：这次事务里**待提交的改动
    必须只有 ``Group`` 行**。只要同事务还挂着别的东西，就不是"两个 update 抢建
    同一个群"，而是某个唯一约束真的被违反了——必须上抛，让 update 按既有机制
    重试，而不是静默丢数据。

    ``pending`` 必须在 ``commit()`` **之前**取：sessionmaker 是 ``autoflush=False``，
    所以那一刻 ``new/dirty/deleted`` 正好是整笔事务的全貌；等 commit 失败之后
    再看，SQLAlchemy 已经把状态清干净了。
    """

    if "UNIQUE constraint failed: groups.id" not in detail:
        return False
    if not pending:
        return False
    return all(isinstance(obj, Group) for obj in pending)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            result = await handler(event, data)
            # autoflush=False ⇒ commit 之前 new/dirty/deleted 就是整笔事务的全貌。
            pending = list(session.new) + list(session.dirty) + list(session.deleted)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                detail = str(getattr(exc, "orig", exc))
                # Allow benign race when two fresh updates concurrently create same
                # group row — but only when that insert was the *whole* transaction.
                # Anything else sharing this transaction must not be discarded.
                if _is_benign_group_race(pending, detail):
                    log.warning("ignored duplicate groups.id race during commit")
                else:
                    raise
            return result
