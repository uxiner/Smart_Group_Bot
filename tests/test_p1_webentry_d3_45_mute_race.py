"""修复批 P1-3 / D3-45：``/mute`` 的唯一索引竞态不再让管理员零反馈。

修前（``bot/handlers/admin.py`` ``cmd_mute``）先 ``SELECT`` 查 ``ReplyMute`` 再
``session.add()`` + ``commit()``，中间**无锁、无 upsert**，而表上有唯一索引::

    4057:    result = await session.execute(stmt)
    4058:    existing = result.scalar_one_or_none()
    ...
    4070:    session.add(ReplyMute(...))
    4077:    await session.commit()
    # bot/db/models.py
    Index("ix_reply_mute_group_user", "group_id", "user_id", unique=True)

``SELECT`` 不进 ``SQLiteSafeAsyncSession`` 的写锁（锁只在 flush/commit 拿）。两个
管理员（或同一管理员双击）同时对同一用户 ``/mute``，两条协程的 SELECT 都在对方
INSERT 前完成，后提交者的 ``IntegrityError`` 在 handler 内部抛出 → 后面的
``_answer`` **永不执行** → 管理员完全无反馈，只剩一条未处理异常栈；用户其实已被
先提交者静默。

本文件走**真实 DB**（真 ``init_db`` + 真 ``SQLiteSafeAsyncSession``），用「第二条
协程的 SELECT 看到的是过期快照」精确复现那个交错。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import ReplyMute
from bot.handlers import admin


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.super_admin_id = 777
    return settings


def _message(target_id: int, actor_id: int) -> SimpleNamespace:
    target = SimpleNamespace(
        id=target_id,
        full_name="目标用户",
        is_bot=False,
        username=None,
    )
    return SimpleNamespace(
        text="/mute",
        message_id=4242,
        chat=SimpleNamespace(id=-100123, type="supergroup", title="group"),
        from_user=SimpleNamespace(id=actor_id, is_bot=False),
        reply_to_message=SimpleNamespace(from_user=target),
    )


class _StaleSnapshotSession:
    """Proxy that hides an already-committed row from the next SELECT.

    This is exactly the interleaving of the race: the second coroutine's SELECT
    completed before the first one's INSERT, so it legitimately sees nothing.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._stale = True

    def execute(self, statement: object, *args: object, **kwargs: object):
        if self._stale and str(statement).startswith("SELECT reply_mutes"):
            self._stale = False
            return _awaitable(_EmptyResult())
        return self._inner.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str):  # pragma: no cover - plain delegation
        return getattr(self._inner, name)


class _EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None


def _awaitable(value: object):
    async def _inner() -> object:
        return value

    return _inner()


class MuteUniqueIndexRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _run_mute(self, message: object, session: object) -> list[str]:
        answers: list[str] = []
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._answer",
                new=AsyncMock(side_effect=lambda _m, _s, text, **_: answers.append(text)),
            ),
        ):
            await admin.cmd_mute(message, session, _settings())
        return answers

    async def _rows(self) -> list[int]:
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(ReplyMute.user_id).order_by(ReplyMute.user_id)
                    )
                )
                .scalars()
                .all()
            )

    async def test_concurrent_mute_answers_both_admins_and_keeps_one_row(self) -> None:
        first_answers: list[str] = []
        second_answers: list[str] = []
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._answer",
                new=AsyncMock(
                    side_effect=lambda _m, _s, text, **_: first_answers.append(text)
                ),
            ),
        ):
            async with self.session_factory() as session:
                await admin.cmd_mute(_message(555, 1001), session, _settings())

        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._answer",
                new=AsyncMock(
                    side_effect=lambda _m, _s, text, **_: second_answers.append(text)
                ),
            ),
        ):
            async with self.session_factory() as stale:
                await admin.cmd_mute(
                    _message(555, 1002), _StaleSnapshotSession(stale), _settings()
                )

        # 修前：第二次抛 IntegrityError，异常逃出 handler，second_answers 为空。
        self.assertEqual(len(first_answers), 1)
        self.assertIn("已加入静默名单", first_answers[0])
        self.assertEqual(len(second_answers), 1)
        self.assertIn("已在静默名单", second_answers[0])
        self.assertEqual(await self._rows(), [555])

    async def test_sequential_mute_still_reports_the_existing_row(self) -> None:
        async with self.session_factory() as session:
            answers = await self._run_mute(_message(556, 1001), session)
        self.assertIn("已加入静默名单", answers[0])

        async with self.session_factory() as session:
            answers = await self._run_mute(_message(556, 1002), session)
        self.assertIn("已在静默名单", answers[0])
        self.assertEqual(await self._rows(), [556])

    async def test_stale_session_is_usable_after_the_rollback(self) -> None:
        """回滚之后同一个 session 还能继续用（不是 aborted transaction）。"""

        async with self.session_factory() as session:
            await self._run_mute(_message(557, 1001), session)

        async with self.session_factory() as stale:
            proxy = _StaleSnapshotSession(stale)
            answers = await self._run_mute(_message(557, 1002), proxy)
            self.assertIn("已在静默名单", answers[0])
            count = (
                await stale.execute(select(func.count()).select_from(ReplyMute))
            ).scalar_one()
        self.assertEqual(int(count), 1)
