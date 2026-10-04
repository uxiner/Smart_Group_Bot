"""修复批 P1-3 / D3-52：``membership`` 四兄弟闭包里那个漏掉的 rollback。

``_enforce_pending_moderation_challenge`` 里有**四个**同类闭包，三个都在做策略读
之前先 ``session.rollback()`` 归一化，只有 patrol 分支的那个漏了::

    # bot/handlers/membership.py
    :969  async def preserve_existing_ban():  await session.rollback(); ...
    :1019 if is_patrol:
    :1020     async def preserve_ban():       blocked = await ...   # ← 无 rollback
    :1082 async def preserve_timeout_ban():   await session.rollback(); ...

而 ``bot/services/join_verification.py:1415-1418`` 明确写了这个要求::

    async def preserve_ban() -> bool:
        # Completion loss normally rolls back itself. A cancellation/DB error
        # may leave an aborted transaction, so normalize it before every
        # authoritative policy read used by the Telegram cleanup retry loop.

该闭包被 ``_ensure_kick_unbanned_result`` 在一次 kick 中**最多调用 4 次**（3 轮 ×
前置检查 + ``finish_confirmed_unban`` 后置检查）。若前一次检查的 ``session.commit()``
因取消/DB 错误留下 aborted 事务，第 2 次策略读直接抛 ``PendingRollbackError``，被
``:3106-3113`` 吞成 ``ok=False`` → 走"durable retry"失败路径，一次本可完成的 timeout
kick 被判失败并回队。

本文件用「策略读之前若没 rollback 过就抛 ``PendingRollbackError``」的 session 替身
把这个契约钉死。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import PendingRollbackError

from bot.db.engine import init_db
from bot.handlers import membership
from bot.services.join_verification import (
    VERIFICATION_KIND_PATROL,
    upsert_join_verification,
)
from bot.utils.timezone import now_shanghai_naive


class _AbortedUntilRollbackSession:
    """Mimic a session left mid-abort: policy reads blow up until rollback()."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        # The handler's own work runs on a healthy transaction; the abort below is
        # what the *first* preserve_ban's commit leaves behind, exactly like a
        # cancelled/DB-failed commit would.
        self.aborted = False
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False
        await self._inner.rollback()

    async def commit(self) -> None:
        self.aborted = True
        await self._inner.commit()

    def __getattr__(self, name: str):
        inner_attr = getattr(self._inner, name)
        if name not in {"execute", "scalar", "scalars", "get"}:
            return inner_attr

        async def guarded(*args: object, **kwargs: object):
            if self.aborted:
                raise PendingRollbackError("transaction is aborted")
            return await inner_attr(*args, **kwargs)

        return guarded


def _settings():
    from bot.config import Settings

    return Settings(_env_file=None)


class PatrolPreserveBanRollbackTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_patrol_preserve_ban_rolls_back_before_the_policy_read(self) -> None:
        captured: dict[str, object] = {}
        session_proxy: _AbortedUntilRollbackSession

        async def fake_kick(_bot, _group_id, _user_id, *, preserve_ban):
            captured["closure"] = preserve_ban
            # _ensure_kick_unbanned_result calls this up to 4 times per kick
            # (3 rounds x pre-check + a post-check after finish_confirmed_unban).
            captured["first"] = await preserve_ban()
            # The first call's commit leaves the session aborted, exactly like a
            # cancelled / DB-failed commit would. The second read therefore has
            # to normalize the transaction first.
            captured["second"] = await preserve_ban()
            # The real retry loop normalizes the transaction again before the
            # caller resumes; mirror that so the rest of the handler runs.
            session_proxy.aborted = False
            return True

        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=931,
                deadline_at=now_shanghai_naive() - timedelta(minutes=30),
                kind=VERIFICATION_KIND_PATROL,
                reason="资料巡检超时",
                display_name="巡检对象",
            )
            await session.commit()
            from bot.services.join_verification import get_join_verification

            record = await get_join_verification(session, -100, 931)

            proxy = _AbortedUntilRollbackSession(session)
            session_proxy = proxy
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100),
                bot=SimpleNamespace(
                    ban_chat_member=AsyncMock(),
                    unban_chat_member=AsyncMock(return_value=True),
                    restrict_chat_member=AsyncMock(return_value=True),
                ),
            )
            with patch(
                "bot.handlers.membership.kick_member", new=fake_kick
            ), patch(
                "bot.handlers.membership.retract_removed_member_residue",
                new=AsyncMock(),
            ), patch(
                "bot.handlers.membership.close_private_challenge_message",
                new=AsyncMock(),
            ):
                await membership._enforce_pending_moderation_challenge(
                    event,
                    proxy,
                    _settings(),
                    record,
                    display_name="巡检对象",
                )

        self.assertIn("closure", captured)
        # 修前：第 2 次调用直接抛 PendingRollbackError，fake_kick 冒泡出去。
        # 修后：两次都正常返回，proxy 至少归一化过两次。
        self.assertFalse(captured["first"])
        self.assertFalse(captured["second"])
        self.assertGreaterEqual(proxy.rollbacks, 2)

    async def test_the_three_sibling_closures_share_the_same_contract(self) -> None:
        """对照：另外三个闭包的 rollback 已经在基线上，这里只是钉住不回归。"""

        import inspect

        source = inspect.getsource(membership._enforce_pending_moderation_challenge)
        for name in (
            "preserve_existing_ban",
            "preserve_ban",
            "preserve_timeout_ban",
            "timeout_restriction_required",
        ):
            marker = f"async def {name}("
            self.assertIn(marker, source)
            body = source.split(marker, 1)[1].split("\n\n", 1)[0]
            self.assertIn(
                "await session.rollback()",
                body,
                f"{name} 必须在策略读之前先 rollback 归一化",
            )
