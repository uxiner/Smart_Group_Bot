"""A-14：审核动作的去重键必须按目标用户，不能退化成"按违规事件 ID"。

``on_moderation_action`` 在把审核动作交给 CRITICAL lane 之前会用
``moderation-action:{group_id}:{target_hint}`` 做 per-user 单飞。原来的
``target_hint`` 初值是 ``violation_id``，于是 ``violation_hint`` 缺失或不属本群
时，键会退化成违规事件主键——同一群的多个违规事件（以及伪造的 callback_data）
各自拿到互不相同的键，绕过单飞去重，在授权校验之后、锁之前就占住特权队列。

真正执行时 :3288 同样会以"审核事件不存在或不属于当前群"拒绝，所以这条改动
只是把那次拒绝提前，同时不再分配特权任务。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import Group, Violation
from bot.handlers import group
from bot.services.authz import authorize_group
from bot.services.privileged_tasks import PrivilegedTaskSubmission


class ModerationActionDedupKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="test", settings={}))
            session.add(Group(id=-101, title="other", settings={}))
            await authorize_group(session, -100, 1)
            await authorize_group(session, -101, 1)
            await session.commit()

        self.settings = SimpleNamespace(
            super_admin_id=1,
            moderation=SimpleNamespace(warn_threshold=3),
            bot=SimpleNamespace(auto_delete_categories=[], auto_delete_seconds=0),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _callback(self, violation_id: int, *, group_id: int = -100) -> SimpleNamespace:
        return SimpleNamespace(
            data=f"mact:ban:{violation_id}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=group_id, type="supergroup"),
                message_id=555,
                html_text="<b>内容审核</b>",
                reply_markup=group._build_moderation_action_keyboard(violation_id),
            ),
            from_user=SimpleNamespace(id=9, username=None, full_name="Operator"),
            bot=SimpleNamespace(
                ban_chat_member=AsyncMock(),
                unban_chat_member=AsyncMock(),
                edit_message_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    async def _seed_violation(self, *, group_id: int = -100, user_id: int = 42) -> int:
        async with self.session_factory() as session:
            violation = Violation(
                group_id=group_id,
                user_id=user_id,
                message_text="bad",
                action_taken="warn",
            )
            session.add(violation)
            await session.commit()
            return int(violation.id)

    async def _invoke(self, callback: SimpleNamespace) -> list[str]:
        keys: list[str] = []
        submission = PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="job-1",
            lane="critical",
            queue_depth=0,
            reason="",
        )

        def _submit(**kwargs: object) -> PrivilegedTaskSubmission:
            keys.append(str(kwargs["key"]))
            return submission

        async with self.session_factory() as session:
            with (
                patch.object(group, "is_group_admin_or_higher", new=AsyncMock(return_value=True)),
                patch.object(group, "submit_privileged_task", new=_submit),
            ):
                await group.on_moderation_action(
                    callback,
                    settings=self.settings,
                    session=session,
                    session_factory=self.session_factory,
                )
        return keys

    async def test_key_is_per_target_user(self) -> None:
        violation_id = await self._seed_violation(user_id=42)
        callback = self._callback(violation_id)

        keys = await self._invoke(callback)

        self.assertEqual(keys, [f"moderation-action:-100:42"])

    async def test_two_events_for_one_user_share_one_dedup_key(self) -> None:
        first = await self._seed_violation(user_id=42)
        second = await self._seed_violation(user_id=42)

        self.assertNotEqual(first, second)
        keys_one = await self._invoke(self._callback(first))
        keys_two = await self._invoke(self._callback(second))

        self.assertEqual(keys_one, keys_two)

    async def test_event_from_another_group_allocates_no_privileged_task(self) -> None:
        violation_id = await self._seed_violation(group_id=-100, user_id=42)
        # 同一个事件 ID 出现在另一个群的按钮上：归属复验必须失败。
        callback = self._callback(violation_id, group_id=-101)

        keys = await self._invoke(callback)

        self.assertEqual(
            keys,
            [],
            "解析不出目标用户时不得再分配 CRITICAL lane 任务（去重键会退化成事件 ID）",
        )
        self.assertIn("不属于当前群", callback.answer.await_args.args[0])
        callback.bot.ban_chat_member.assert_not_awaited()

    async def test_unknown_event_allocates_no_privileged_task(self) -> None:
        callback = self._callback(987654)

        keys = await self._invoke(callback)

        self.assertEqual(keys, [])
        self.assertIn("不属于当前群", callback.answer.await_args.args[0])
