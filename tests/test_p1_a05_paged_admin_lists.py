"""A-05：``/warnings``、``/authlist`` 及翻页回调必须分页，不再全表加载。

硬证据（报告原文）：``grep -c "\\.limit(" bot/handlers/admin.py`` → 0。
警告名单查询没有任何 ``LIMIT``，一次把整群 ``UserWarning`` 全量实例化，翻页只是
对已加载列表切片；``_build_warning_list_page`` 的 ``banned_count`` 还在 Python 层
再遍历一次全表。``/authlist`` 同样一次性加载**全部**已授权群（含已失效的），
Python 里切片。

本文件直接盯 SQL：任何针对 ``user_warnings`` / ``authorized_groups`` 的
``SELECT`` 都必须带 ``LIMIT``，整表一次都不许。
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event

from bot.db.engine import init_db
from bot.db.models import UserWarning
from bot.handlers import admin
from bot.services.authz import authorize_group

# SQLAlchemy renders LIMIT/OFFSET as bind parameters, so accept both forms.
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\?|\d+)", re.IGNORECASE)


class PagedAdminListTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = -100
    TOTAL = 23

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.statements: list[str] = []
        self.parameters: list[object] = []
        event.listen(
            self.engine.sync_engine,
            "before_cursor_execute",
            self._record,
        )
        async with self.session_factory() as session:
            await authorize_group(session, self.GROUP_ID, 1)
            session.add_all(
                UserWarning(
                    group_id=self.GROUP_ID,
                    user_id=1000 + index,
                    count=index,
                    is_banned=index < 3,
                )
                for index in range(self.TOTAL)
            )
            await session.commit()

        self.settings = SimpleNamespace(
            super_admin_id=1,
            moderation=SimpleNamespace(warn_threshold=3),
        )

    async def asyncTearDown(self) -> None:
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._record)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _record(self, _conn, _cursor, statement, params, _ctx, _many) -> None:
        self.statements.append(" ".join(statement.split()))
        self.parameters.append(params)

    def _unbounded(self, table: str) -> list[str]:
        """SELECT <table> ... that would read the whole table in one go."""

        return [
            statement
            for statement in self.statements
            if table in statement
            and statement.upper().lstrip().startswith("SELECT")
            and "COUNT(" not in statement.upper()
            and not _LIMIT_RE.search(statement)
        ]

    @staticmethod
    def _warnings_message() -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=PagedAdminListTests.GROUP_ID, type="supergroup"),
        )

    @staticmethod
    def _warnings_callback(page: int) -> SimpleNamespace:
        return SimpleNamespace(
            data=f"wpl:{page}",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(
                    id=PagedAdminListTests.GROUP_ID, type="supergroup"
                ),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    async def _seed_authorized_groups(self, total: int) -> None:
        async with self.session_factory() as session:
            for index in range(total):
                await authorize_group(session, -900 - index, 1)
            await session.commit()

    async def test_warnings_command_loads_one_page_only(self) -> None:
        self.statements.clear()
        self.parameters.clear()
        with (
            patch.object(
                admin, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(
                admin,
                "ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch.object(admin, "_answer", new=AsyncMock()) as answer,
        ):
            async with self.session_factory() as session:
                await admin.cmd_warnings(
                    self._warnings_message(), session, self.settings
                )

        self.assertEqual(
            self._unbounded("user_warnings"),
            [],
            f"/warnings 仍有整群全表 SELECT：{self.statements}",
        )
        self.assertTrue(
            any(
                "LIMIT" in statement.upper() and "user_warnings" in statement
                for statement in self.statements
            ),
            f"/warnings 必须发出带 LIMIT 的分页查询，实际 SQL：{self.statements}",
        )
        body = answer.await_args.args[2]
        self.assertIn(f"23</code> 人", body)
        self.assertIn(f"3</code> 人", body)  # 已封禁数取自 count(...) 而不是全表遍历
        self.assertIn("1 / 5", body)

    async def test_warnings_paging_stays_paged_and_reports_real_total(self) -> None:
        self.statements.clear()
        self.parameters.clear()
        callback = self._warnings_callback(2)
        with patch.object(
            admin, "_callback_user_can_manage_rules", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await admin.on_warnings_paging(callback, self.settings, session=session)

        self.assertEqual(
            self._unbounded("user_warnings"),
            [],
            f"翻页回调仍在整群全表加载：{self.statements}",
        )
        callback.message.edit_text.assert_awaited_once()
        body = callback.message.edit_text.await_args.args[0]
        self.assertIn("23</code> 人", body)
        self.assertIn("3 / 5", body)

    async def test_out_of_range_warning_page_is_clamped(self) -> None:
        callback = self._warnings_callback(99)
        with patch.object(
            admin, "_callback_user_can_manage_rules", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await admin.on_warnings_paging(callback, self.settings, session=session)

        body = callback.message.edit_text.await_args.args[0]
        self.assertIn("5 / 5", body)

    async def test_authlist_loads_one_page_only(self) -> None:
        await self._seed_authorized_groups(17)
        self.statements.clear()
        self.parameters.clear()
        with patch.object(admin, "ensure_super_admin", new=AsyncMock(return_value=True)):
            with patch.object(admin, "_answer", new=AsyncMock()) as answer:
                message = SimpleNamespace(
                    chat=SimpleNamespace(id=1, type="private"),
                    text="/authlist",
                )
                async with self.session_factory() as session:
                    await admin.cmd_authlist(message, session, self.settings)

        self.assertEqual(
            self._unbounded("authorized_groups"),
            [],
            f"/authlist 仍在整表加载：{self.statements}",
        )
        self.assertTrue(
            any(
                "authorized_groups" in statement
                and "LIMIT" in statement.upper()
                and "COUNT(" not in statement.upper()
                for statement in self.statements
            ),
            f"/authlist 必须发出带 LIMIT 的分页查询，实际 SQL：{self.statements}",
        )
        body = answer.await_args.args[2]
        # 1 个测试授权群 + 17 个种子群 = 18。
        self.assertIn("18</code> 个", body)
        self.assertIn("1 / 4", body)

    async def test_authlist_paging_stays_paged(self) -> None:
        await self._seed_authorized_groups(17)
        self.statements.clear()
        self.parameters.clear()
        callback = SimpleNamespace(
            data="atl:1",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1, type="private"),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        with patch.object(
            admin, "_callback_user_is_super_admin", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                await admin.on_authlist_paging(callback, self.settings, session=session)

        unbounded = [
            statement
            for statement in self.statements
            if "authorized_groups" in statement
            and statement.upper().lstrip().startswith("SELECT")
            and "COUNT(" not in statement.upper()
            and not _LIMIT_RE.search(statement)
        ]
        self.assertEqual(
            unbounded,
            [],
            f"/authlist 翻页回调仍在整表加载：{self.statements}",
        )
        body = callback.message.edit_text.await_args.args[0]
        self.assertIn("18</code> 个", body)
        self.assertIn("2 / 4", body)
