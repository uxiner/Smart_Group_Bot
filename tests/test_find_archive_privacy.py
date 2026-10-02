"""F-016：/find 不得把「已被审核删除」的消息再检索出来。

审查结论：``recall_archive`` 只按 ``group_id + 保留期`` 过滤，不排除动作已执行
的违规消息；任何成员都能把已经被删掉的广告正文重新检索回群里（带发送者昵称），
既泄漏用户数据，也抵消了删除处置的可见效果。

本文件锁定修好之后的口径：

- 普通成员（默认 ``include_disposed=False``）检索不到已被审核删除的消息，
  负例覆盖「锚点」与「上下文邻居」两条路径，以及按 ``message_key`` 精确展开；
- 正常检索仍然可用（正例）：没被处置的消息照常返回；
- 只是 ``warn``（消息还在群里）的命中不影响检索；
- 只有本群管理员能用 ``/find --all`` 走"含已删除内容"的审计口径，并且每次
  都会留下一条 WARNING 审计日志；普通成员用 ``--all`` 会被权限校验挡下。

所有 Telegram 调用都是 mock，不需要网络。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import BotConfig, Settings
from bot.db.engine import init_db
from bot.db.models import Group, Violation
from bot.handlers import commands
from bot.services.memory import MemoryService

GROUP_ID = -10077
OTHER_GROUP_ID = -10088
DELETED_MESSAGE_ID = 2
WARNED_MESSAGE_ID = 3


class _LLM:
    class main:
        model = "test/model"


def _settings() -> Settings:
    return Settings(_env_file=None)


def _message(text: str, *, user_id: int = 777):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=GROUP_ID, type="supergroup", title="t"),
        from_user=SimpleNamespace(id=user_id, first_name="成员", last_name=""),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class DisposedArchiveRecallTests(unittest.IsolatedAsyncioTestCase):
    """真库（sqlite）验证 recall 的过滤口径。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.memory = MemoryService(
            BotConfig(),
            _LLM(),  # type: ignore[arg-type]
            session_factory=self.session_factory,
        )

    async def asyncTearDown(self) -> None:
        await self.memory.shutdown(timeout_seconds=1.0)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _archive_range(self, group_id: int, count: int) -> None:
        self.memory._archive_last_pruned_at[group_id] = time.monotonic()
        base = datetime.now(timezone.utc) - timedelta(minutes=count)
        for message_id in range(1, count + 1):
            await self.memory.archive_message(
                group_id,
                "user",
                f"ordinary message {message_id}",
                message_id=str(message_id),
                telegram_message_id=message_id,
                created_at=base + timedelta(minutes=message_id),
                sender_id=message_id,
                sender_display_name=f"sender-{message_id}",
                message_type="text",
                raw_text=f"ordinary message {message_id}",
            )

    async def _record_violation(
        self, group_id: int, message_id: int, action: str
    ) -> None:
        async with self.session_factory() as session:
            if await session.get(Group, group_id) is None:
                session.add(Group(id=group_id, title="t", settings={}))
            session.add(
                Violation(
                    group_id=group_id,
                    user_id=message_id,
                    action_taken=action,
                    source_message_id=message_id,
                    message_text="加V 领取优惠券",
                )
            )
            await session.commit()

    async def test_member_query_never_returns_a_deleted_message(self) -> None:
        """负例：被审核删除的消息不出现在默认检索结果里。"""

        await self._archive_range(GROUP_ID, 4)
        await self._record_violation(GROUP_ID, DELETED_MESSAGE_ID, "delete")

        rows = await self.memory.recall_archive(
            GROUP_ID,
            query="ordinary message",
            before_after=0,
            limit=10,
        )
        keys = {row["message_key"] for row in rows}

        self.assertNotIn(f"{GROUP_ID}:{DELETED_MESSAGE_ID}", keys)
        # 正例：没被处置的消息照常返回
        self.assertIn(f"{GROUP_ID}:1", keys)
        self.assertIn(f"{GROUP_ID}:4", keys)
        self.assertTrue(rows)

    async def test_context_neighbours_of_a_deleted_message_are_filtered_too(
        self,
    ) -> None:
        """负例：已删除消息即使作为"上下文邻居"也不许回显。"""

        await self._archive_range(GROUP_ID, 5)
        await self._record_violation(GROUP_ID, DELETED_MESSAGE_ID, "delete")

        rows = await self.memory.recall_archive(
            GROUP_ID,
            message_keys=[f"{GROUP_ID}:4"],
            before_after=2,
            limit=4,
        )
        keys = {row["message_key"] for row in rows}

        self.assertNotIn(f"{GROUP_ID}:{DELETED_MESSAGE_ID}", keys)
        self.assertIn(f"{GROUP_ID}:4", keys)

    async def test_explicit_message_key_cannot_resurrect_a_deleted_message(
        self,
    ) -> None:
        """负例：拿 message_key 精确展开也拿不到已删除内容。"""

        await self._archive_range(GROUP_ID, 3)
        await self._record_violation(GROUP_ID, DELETED_MESSAGE_ID, "delete")

        rows = await self.memory.recall_archive(
            GROUP_ID,
            message_keys=[f"{GROUP_ID}:{DELETED_MESSAGE_ID}"],
            before_after=0,
            limit=3,
        )

        self.assertEqual(rows, [])

    async def test_warned_but_present_messages_stay_searchable(self) -> None:
        """只是 warn（消息还在群里）的命中不该被隐藏，避免误伤检索。"""

        await self._archive_range(GROUP_ID, 4)
        await self._record_violation(GROUP_ID, WARNED_MESSAGE_ID, "warn")

        rows = await self.memory.recall_archive(
            GROUP_ID,
            query="ordinary message",
            before_after=0,
            limit=10,
        )
        keys = {row["message_key"] for row in rows}

        self.assertIn(f"{GROUP_ID}:{WARNED_MESSAGE_ID}", keys)

    async def test_admin_audit_scope_can_include_disposed_content(self) -> None:
        """管理员显式口径（include_disposed=True）仍能查到已删除内容。"""

        await self._archive_range(GROUP_ID, 3)
        await self._record_violation(GROUP_ID, DELETED_MESSAGE_ID, "delete")

        rows = await self.memory.recall_archive(
            GROUP_ID,
            message_keys=[f"{GROUP_ID}:{DELETED_MESSAGE_ID}"],
            before_after=0,
            limit=3,
            include_disposed=True,
        )

        self.assertEqual(
            [row["message_key"] for row in rows],
            [f"{GROUP_ID}:{DELETED_MESSAGE_ID}"],
        )


class FindCommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """命令层：默认口径与 --all 的权限 + 审计日志。"""

    async def _run(
        self,
        text: str,
        *,
        admin: bool = False,
        hits: list[dict] | None = None,
    ):
        answered: list[str] = []

        async def _answer(_msg, _settings, body, **_kwargs):
            answered.append(body)

        recall = AsyncMock(return_value=hits or [])
        stub = SimpleNamespace(recall_archive=recall)
        admin_check = AsyncMock(return_value=admin)
        with (
            patch.object(commands, "_answer", side_effect=_answer),
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(commands, "ensure_group_admin_permission", new=admin_check),
            patch.object(
                commands.memory_holder, "get_optional", return_value=stub
            ),
        ):
            await commands.cmd_find(_message(text), _FakeSession(), _settings())
        return answered, recall, admin_check

    async def test_member_query_uses_the_disposed_free_scope(self) -> None:
        _answered, recall, admin_check = await self._run("/find cn2")

        recall.assert_awaited_once()
        self.assertFalse(recall.await_args.kwargs["include_disposed"])
        admin_check.assert_not_awaited()

    async def test_all_flag_is_rejected_for_non_admins(self) -> None:
        _answered, recall, admin_check = await self._run("/find --all cn2", admin=False)

        admin_check.assert_awaited_once()
        recall.assert_not_awaited()

    async def test_all_flag_for_admins_is_audited(self) -> None:
        with self.assertLogs("bot.handlers.commands", level="WARNING") as logs:
            _answered, recall, admin_check = await self._run(
                "/find --all cn2", admin=True
            )

        admin_check.assert_awaited_once()
        recall.assert_awaited_once()
        self.assertTrue(recall.await_args.kwargs["include_disposed"])
        self.assertTrue(
            any("/find --all" in line and "operator=777" in line for line in logs.output),
            logs.output,
        )

    async def test_usage_mentions_the_deleted_message_policy(self) -> None:
        answered, recall, _admin = await self._run("/find")

        body = "".join(answered)
        self.assertIn("/find", body)
        self.assertIn("已被审核删除", body)
        recall.assert_not_awaited()
