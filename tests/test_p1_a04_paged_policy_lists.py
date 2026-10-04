"""A-04：per-group 用户策略列表必须有分页上限，且 GET 不得写库。

``/warnings``、``/bans``、``/moderation-exemptions``、``/reply-mutes`` 走
``_list_user_rows`` / ``list_group_bans``，报告实测这两处**完全没有 LIMIT**，
一次 ``select(...).all()`` 把整群记录拉进内存再序列化成单个 JSON；同文件的全局
端点 ``_global_registry_query`` 却有明确的 ``limit = min(500, ...)``，对比鲜明。

更关键的是这些 **GET 端点会顺带发起实时 Telegram RPC 和数据库写事务**：
``_group_member_map`` 拿到结果后 ``session.add(row)`` 并 ``commit()``。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, func, select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    AuthorizedGroup,
    Group,
    GroupMember,
    ModerationExemption,
    ReplyMute,
    UserWarning,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
GROUP_ID = -5150
ROW_COUNT = 640


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-a04-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class PagedPolicyListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=42,
            config_master_key="a04-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8482
        self.settings.join_verification_public_base_url = self.settings.miniapp_public_base_url
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-a04.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
            # Every list request would burn shared Telegram quota through this.
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status="member",
                    user=SimpleNamespace(
                        id=1, full_name="Live", username="live", is_bot=False
                    ),
                )
            ),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

        self.statements: list[str] = []
        event.listen(
            self.engine.sync_engine, "before_cursor_execute", self._record
        )

        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=GROUP_ID, authorized_by=42))
            session.add(Group(id=GROUP_ID, title="压力测试群", settings={}))
            session.add_all(
                UserWarning(
                    group_id=GROUP_ID,
                    user_id=700000 + index,
                    count=index,
                    is_banned=index < 20,
                )
                for index in range(ROW_COUNT)
            )
            session.add_all(
                ReplyMute(
                    group_id=GROUP_ID,
                    user_id=800000 + index,
                    created_by=42,
                )
                for index in range(5)
            )
            session.add(
                ModerationExemption(group_id=GROUP_ID, user_id=900001, created_by=42)
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        event.remove(
            self.engine.sync_engine, "before_cursor_execute", self._record
        )
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _record(self, _conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any) -> None:
        self.statements.append(" ".join(statement.split()))

    @staticmethod
    def _headers(user_id: int = 42) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _writes(self) -> list[str]:
        verbs = ("INSERT", "UPDATE", "DELETE", "REPLACE")
        return [
            statement
            for statement in self.statements
            if statement.upper().split(" ", 1)[0] in verbs
        ]

    def _unbounded_reads(self, table: str) -> list[str]:
        return [
            statement
            for statement in self.statements
            if f"FROM {table}" in statement
            and statement.upper().startswith("SELECT")
            and "COUNT(" not in statement.upper()
            and "LIMIT" not in statement.upper()
        ]

    async def test_warnings_list_is_capped_and_cursor_paged(self) -> None:
        self.statements.clear()
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/warnings", headers=self._headers()
        )

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(
            len(body["warnings"]),
            100,
            "默认窗口必须是有限的一页，而不是整群 640 行",
        )
        self.assertEqual(body["next_offset"], 100)
        self.assertEqual(self._unbounded_reads("user_warnings"), [])

    async def test_bans_list_is_capped_and_cursor_paged(self) -> None:
        self.statements.clear()
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/bans", headers=self._headers()
        )

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertLessEqual(len(body["bans"]), 20)
        self.assertIsNone(body["next_offset"])
        self.assertEqual(self._unbounded_reads("user_warnings"), [])

    async def test_limit_is_clamped_to_500(self) -> None:
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/warnings?limit=99999",
            headers=self._headers(),
        )

        body = await response.json()
        self.assertEqual(len(body["warnings"]), 500)
        self.assertEqual(body["next_offset"], 500)

    async def test_offset_walks_the_whole_list_without_gaps(self) -> None:
        seen: list[int] = []
        offset = 0
        while True:
            response = await self.client.get(
                f"/api/v1/groups/{GROUP_ID}/warnings?limit=500&offset={offset}",
                headers=self._headers(),
            )
            body = await response.json()
            seen.extend(int(item["user_id"]) for item in body["warnings"])
            if body["next_offset"] is None:
                break
            offset = int(body["next_offset"])

        self.assertEqual(len(seen), ROW_COUNT)
        self.assertEqual(len(set(seen)), ROW_COUNT)

    async def test_invalid_pagination_is_rejected(self) -> None:
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/warnings?limit=abc",
            headers=self._headers(),
        )

        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "invalid_pagination")

    async def test_policy_list_get_does_not_write_to_the_database(self) -> None:
        for path in ("warnings", "bans", "moderation-exemptions", "reply-mutes"):
            self.statements.clear()
            await self.client.get(
                f"/api/v1/groups/{GROUP_ID}/{path}", headers=self._headers()
            )
            self.assertEqual(
                self._writes(),
                [],
                f"GET /{path} 产生了写事务：{self.statements}",
            )
            roster_reads = [
                statement
                for statement in self.statements
                if "FROM group_members" in statement
            ]
            self.assertTrue(
                roster_reads
                and all("user_id IN" in statement for statement in roster_reads),
                f"GET /{path} 的 roster 读必须按本页 user_id 收窄：{self.statements}",
            )

    async def test_get_list_does_not_persist_roster_identity(self) -> None:
        # No group_members rows exist for these policy users, so a persisting
        # identity lookup would have created them.
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/warnings?limit=3", headers=self._headers()
        )

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(len(body["warnings"]), 3)
        async with self.session_factory() as session:
            stored = int(
                await session.scalar(
                    select(func.count()).select_from(GroupMember).where(
                        GroupMember.group_id == GROUP_ID
                    )
                )
                or 0
            )
        self.assertEqual(
            stored,
            0,
            "只读列表端点不得把 Telegram 补全结果写回 group_members",
        )

    async def test_display_name_is_still_resolved_for_the_response(self) -> None:
        # The label is still produced for this response; it just is not stored.
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/warnings?limit=3", headers=self._headers()
        )

        body = await response.json()
        self.assertIn(body["warnings"][0]["display_name"], ("Live", "live"))
        self.assertEqual(
            self.bot.get_chat_member.await_count,
            3,
            "只补本页缺名字的人；补全预算本来就按请求封顶",
        )


class MiniAppPagesPolicyListsTests(unittest.TestCase):
    def test_frontend_walks_the_cursor_for_paged_policy_lists(self) -> None:
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "bot"
            / "web"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        loader = source.split("async function loadPagedGroupResource", 1)[1].split(
            "async function loadGroupResources", 1
        )[0]

        self.assertIn("limit=500&offset=${offset}", loader)
        self.assertIn("result?.next_offset", loader)
        for prop in ("warnings", "bans", "exemptions", "reply_mutes"):
            self.assertIn(f'"{prop}"', source.split("PAGED_GROUP_RESOURCES", 1)[1].split("]", 1)[0])
        self.assertIn(
            "loadPagedGroupResource(url, responseKey)",
            source,
        )
