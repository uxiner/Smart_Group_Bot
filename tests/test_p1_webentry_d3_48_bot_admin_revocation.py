"""修复批 P1-3 / D3-48：``/mute`` 与 ``/proactive`` 的群管理员授权要向 Telegram 复核。

权限只查本地 ``admins`` 表，从不回 Telegram::

    # bot/handlers/admin.py:4015 / :4212（修前）
    if not await ensure_group_admin_permission(message, session, settings): return
    # bot/services/authz.py:287-289
    ok = await is_group_admin_authorized(session, message.chat.id, user.id)

而 ``/authadmin`` 写入的 ``admins`` 行**没有任何自动回收路径**（全仓
``deauthorize_group_admin`` 只有 ``/unauthadmin`` 一个调用点）。Telegram 群主撤销
其管理员身份后，该用户仍能 ``/mute`` 掉任何人、改主动话题。

修法（与 P0 批的 D3-18 同一口径）：``ensure_group_admin_permission`` 增加可选的
``revalidate_telegram=``，在本地表放行**之后**追加一次 ``getChatAdministrators``
复验，带 4s 超时，返回三态 True/False/None；只有 ``/mute`` 与 ``/proactive`` 这两个
有直接成员级副作用的入口打开。

本文件走真 SQLite（真 ``init_db`` + 真授权行），只把 Bot API 换成可控替身。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import Admin
from bot.services.authz import ensure_group_admin_permission


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.super_admin_id = 777
    return settings


def _message(admins: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-100123, type="supergroup", title="group"),
        from_user=SimpleNamespace(id=555, is_bot=False, username="former_admin"),
        bot=SimpleNamespace(get_chat_administrators=AsyncMock(return_value=admins)),
        answer=AsyncMock(),
    )


class BotCommandAdminRevalidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.services.authz import authorize_group, authorize_group_admin

        async with self.session_factory() as session:
            await authorize_group(session, -100123)
            await authorize_group_admin(session, -100123, 555)
            await session.commit()
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _check(self, message: SimpleNamespace, *, revalidate: bool) -> bool:
        async with self.session_factory() as session:
            return await ensure_group_admin_permission(
                message, session, _settings(), revalidate_telegram=revalidate
            )

    async def test_local_row_alone_no_longer_grants_the_side_effect(self) -> None:
        message = _message([])  # Telegram 权威名单里已经没有人了
        self.assertFalse(await self._check(message, revalidate=True))
        self.assertTrue(message.answer.await_count)

    async def test_still_an_administrator_is_allowed(self) -> None:
        message = _message(
            [SimpleNamespace(user=SimpleNamespace(id=555), status="administrator")]
        )
        self.assertTrue(await self._check(message, revalidate=True))

    async def test_creator_is_allowed(self) -> None:
        message = _message(
            [SimpleNamespace(user=SimpleNamespace(id=555), status="creator")]
        )
        self.assertTrue(await self._check(message, revalidate=True))

    async def test_lookup_failure_degrades_to_the_local_table(self) -> None:
        """问不到 ≠ 确认没有：Telegram 抖动不能把管理员锁在门外。"""

        message = _message([])
        message.bot.get_chat_administrators = AsyncMock(
            side_effect=RuntimeError("flood wait")
        )
        self.assertTrue(await self._check(message, revalidate=True))

    async def test_slow_lookup_is_bounded_by_the_timeout(self) -> None:
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(30)

        message = _message([])
        message.bot.get_chat_administrators = AsyncMock(side_effect=hang)
        with patch(
            "bot.services.authz._TELEGRAM_ADMIN_REVALIDATION_TIMEOUT_SECONDS", 0.05
        ):
            self.assertTrue(await self._check(message, revalidate=True))

    async def test_opt_in_is_off_by_default_for_every_other_command(self) -> None:
        message = _message([])
        self.assertTrue(await self._check(message, revalidate=False))
        message.bot.get_chat_administrators.assert_not_awaited()

    async def test_no_local_admin_row_is_rejected_without_any_telegram_call(
        self,
    ) -> None:
        message = _message(
            [SimpleNamespace(user=SimpleNamespace(id=555), status="administrator")]
        )
        async with self.session_factory() as session:
            for row in (
                await session.execute(
                    select(Admin).where(
                        Admin.group_id == -100123, Admin.user_id == 555
                    )
                )
            ).scalars().all():
                await session.delete(row)
            await session.commit()
        self.assertFalse(await self._check(message, revalidate=True))
        message.bot.get_chat_administrators.assert_not_awaited()

    def test_the_two_reported_entrypoints_opt_in(self) -> None:
        import inspect

        from bot.handlers import admin

        for name in ("cmd_mute", "cmd_proactive"):
            source = inspect.getsource(getattr(admin, name))
            self.assertIn(
                "revalidate_telegram=True",
                source,
                f"{name} 必须打开 Telegram 交叉校验",
            )
