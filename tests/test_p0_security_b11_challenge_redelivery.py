"""修复批 P0-1 / B-11 残留：Telegram 重投递下不再重复发一次静音。

复现的原缺陷（``GAP-D1`` §3.3 残余项 + ``AUDIT-B`` B-11 的残留）：

``GAP-D1`` §3.3 已经用**真实 aiosqlite + 真实 ``begin_moderation_challenge``** 实测
证明 DB 层完全幂等——重投递只产生 1 条 ``join_verifications``、不延 deadline、不重发
质询卡、不重复扣分。但同时实测到残留：

    B11-3：第 1 次后 restrict_chat_member 调用次数 = 1
           第 2 次后 restrict_chat_member 调用次数 = 2     ← 重复静音
    B11-7：并发两次 -> restrict_chat_member 2 次；send_message 1 次

根因在 ``join_verification.py`` 的**重复分支**：那一行 ``restrict_new_member`` 在
「行已存在且状态是 pending/enforcing」时**也会执行**。而这一行只有在静音成功、
``commit_prepared_join_verification`` 跑完之后才会到达那个状态（静音失败会被
compensate 掉，压根到不了），所以这行静音**恒为多余**。

讽刺的是源码在重复分支里特意做了「静音前后各校验一次 generation」，就是为了防
``/unban`` 竞态——却没意识到重复分支根本不需要再静音一次。去掉这次网络 await 之后，
前后两次校验之间不再有可插入的 await，竞态窗口随之消失。

本文件走**真实 aiosqlite + 真实 ``begin_moderation_challenge``**（与 GAP-D1 同款），
Telegram 侧只用 ``AsyncMock`` 替身记调用次数：**不联网、不用 token**。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import JoinVerification
from bot.services import join_verification as jv
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100777
USER_ID = 901

SETTINGS = SimpleNamespace(
    moderation=SimpleNamespace(enabled=True, challenge_timeout_seconds=600),
    join_verification_turnstile_site_key="site",
    join_verification_turnstile_secret_key="secret",
    join_verification_hcaptcha_site_key="",
    join_verification_hcaptcha_secret_key="",
    join_verification_public_base_url="https://verify.example.com",
)


class _Bot:
    """只记调用次数的 Telegram 替身。"""

    def __init__(self) -> None:
        self.restrict_calls: list[tuple] = []
        self.sent_messages: list[tuple] = []

        async def restrict_chat_member(group_id, user_id, **kwargs):
            self.restrict_calls.append((int(group_id), int(user_id)))
            return True

        async def send_message(chat_id, text, **kwargs):
            self.sent_messages.append((int(chat_id), str(text)))
            return SimpleNamespace(message_id=555)

        self.restrict_chat_member = AsyncMock(side_effect=restrict_chat_member)
        self.send_message = AsyncMock(side_effect=send_message)


class ModerationChallengeRedeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        jv._MODERATION_CHALLENGE_LOCKS.clear()
        self.bot = _Bot()

    async def asyncTearDown(self) -> None:
        jv._MODERATION_CHALLENGE_LOCKS.clear()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _challenge(self):
        async with self.session_factory() as session:
            return await jv.begin_moderation_challenge(
                bot=self.bot,
                session=session,
                settings=SETTINGS,
                group_id=GROUP_ID,
                user_id=USER_ID,
                display_name="群友",
                bot_username="bot",
                reason="命中群规",
                rule_action="ban",
            )

    async def _rows(self):
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(
                        JoinVerification.id,
                        JoinVerification.user_id,
                        JoinVerification.kind,
                        JoinVerification.status,
                        JoinVerification.deadline_at,
                    )
                )
            ).all()

    # ---- 核心回归 --------------------------------------------------------

    async def test_redelivery_does_not_mute_a_second_time(self) -> None:
        """核心回归：同一条消息重投递 → 只静音一次。"""

        first = await self._challenge()
        self.assertTrue(first)
        self.assertEqual(len(self.bot.restrict_calls), 1)

        second = await self._challenge()  # Telegram 重投递
        self.assertTrue(second)
        # 修前这里是 2（GAP-D1 B11-3 实测值）。
        self.assertEqual(len(self.bot.restrict_calls), 1)

    async def test_redelivery_keeps_one_row_one_deadline_one_prompt(self) -> None:
        """DB 层幂等不能被这次改动破坏（GAP-D1 B11-1/2/4 的口径）。"""

        await self._challenge()
        rows_after_first = await self._rows()
        await self._challenge()
        rows_after_second = await self._rows()

        self.assertEqual(len(rows_after_first), 1)
        self.assertEqual(rows_after_first, rows_after_second)
        self.assertEqual(len(self.bot.sent_messages), 1)

    async def test_concurrent_redelivery_mutes_once(self) -> None:
        """并发重投递（两个协程同时进）也只静音一次（GAP-D1 B11-7）。"""

        import asyncio

        results = await asyncio.gather(self._challenge(), self._challenge())

        self.assertEqual(results, [True, True])
        self.assertEqual(len(await self._rows()), 1)
        # 修前这里是 2。
        self.assertEqual(len(self.bot.restrict_calls), 1)
        self.assertEqual(len(self.bot.sent_messages), 1)

    async def test_third_redelivery_still_silent(self) -> None:
        for _ in range(3):
            self.assertTrue(await self._challenge())
        self.assertEqual(len(self.bot.restrict_calls), 1)
        self.assertEqual(len(self.bot.sent_messages), 1)

    # ---- 不能把功能一起修没 ----------------------------------------------

    async def test_first_call_still_mutes_and_prompts(self) -> None:
        self.assertTrue(await self._challenge())
        self.assertEqual(len(self.bot.restrict_calls), 1)
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertEqual(len(await self._rows()), 1)

    async def test_non_ban_action_is_still_refused(self) -> None:
        async with self.session_factory() as session:
            handled = await jv.begin_moderation_challenge(
                bot=self.bot,
                session=session,
                settings=SETTINGS,
                group_id=GROUP_ID,
                user_id=USER_ID,
                display_name="群友",
                bot_username="bot",
                reason="命中群规",
                rule_action="delete",
            )
        self.assertFalse(handled)
        self.assertEqual(self.bot.restrict_calls, [])
        self.assertEqual(self.bot.sent_messages, [])

    async def test_expired_challenge_row_is_not_treated_as_duplicate(self) -> None:
        """非 pending/enforcing 的既有行（另一种 kind）不当作重复挑战。"""

        async with self.session_factory() as session:
            handled = await jv.begin_moderation_challenge(
                bot=self.bot,
                session=session,
                settings=SETTINGS,
                group_id=GROUP_ID,
                user_id=USER_ID + 1,
                display_name="群友",
                bot_username="bot",
                reason="命中群规",
                rule_action="ban",
            )
        self.assertTrue(handled)
        self.assertEqual(len(self.bot.restrict_calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
