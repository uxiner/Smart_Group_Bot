"""A-09 / A-10：settings_api 的建行冲突要回 409，群更新锁不能无界增长。

* A-09：``create_authorized_group_api`` 与 ``_create_user_row`` 的 ``commit()``
  没有捕获 ``IntegrityError``，并发为同一个 ``(group_id, user_id)`` 建行时冲突
  冒泡到装饰器兜底 ``except Exception`` → 500 ``internal_error``；而同文件的
  ``create_group_admin_api`` / ``put_group_settings`` 都显式回 409。
* A-10：``_GROUP_UPDATE_LOCKS`` 是模块级字典且全程没有回收，键空间随"曾经被
  改过设置的群"单调增长。
"""
from __future__ import annotations

import asyncio
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
from sqlalchemy.exc import IntegrityError

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    AuthorizedGroup,
    Group,
    ModerationExemption,
    ReplyMute,
    UserWarning,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.web import settings_api

BOT_TOKEN = "42:TEST_TOKEN"

_ROW_MODELS = (AuthorizedGroup, Group, ModerationExemption, ReplyMute, UserWarning)


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-a09-a10-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class _MiniAppTestCase(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="a09-a10-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8481
        self.settings.join_verification_public_base_url = self.settings.miniapp_public_base_url
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-a09-a10.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _headers(user_id: int = 42) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _patch_commit_integrity_error(self) -> Any:
        """Make the next commit that flushes a target row fail like SQLite does.

        The real race (two admins creating the same row at once) is a
        cross-connection interleaving; what A-09 is about is the *error
        mapping*, and this reproduces the exact ``IntegrityError`` the unique
        index raises at commit time without the timing dance.
        """

        session_class = self.session_factory.class_
        original = session_class.commit

        async def _commit(self: Any, *args: object, **kwargs: object) -> object:
            pending = list(self.new) + list(self.dirty)
            if any(isinstance(row, _ROW_MODELS) for row in pending):
                raise IntegrityError(
                    "INSERT",
                    {},
                    Exception("UNIQUE constraint failed: ix_model_unique"),
                )
            return await original(self, *args, **kwargs)

        return patch.object(session_class, "commit", new=_commit)


class CreateRowConflictReturns409Tests(_MiniAppTestCase):
    async def test_authorized_group_conflict_is_409_not_500(self) -> None:
        with self._patch_commit_integrity_error():
            response = await self.client.post(
                "/api/v1/authorized-groups",
                headers=self._headers(),
                json={"group_id": -4242, "title": "冲突群"},
            )

        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "authorized_group_conflict")

    async def test_user_policy_row_conflict_is_409_not_500(self) -> None:
        with self._patch_commit_integrity_error():
            response = await self.client.post(
                "/api/v1/groups/-4242/reply-mutes",
                headers=self._headers(),
                json={"user_id": 900001},
            )

        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "user_policy_conflict")

    async def test_happy_path_still_201_after_mapping_change(self) -> None:
        response = await self.client.post(
            "/api/v1/authorized-groups",
            headers=self._headers(),
            json={"group_id": -4242, "title": "正常群"},
        )

        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])


class GroupUpdateLockIsReclaimedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings_api._GROUP_UPDATE_LOCKS.clear()
        settings_api._GROUP_UPDATE_LOCK_WAITERS.clear()

    async def asyncTearDown(self) -> None:
        settings_api._GROUP_UPDATE_LOCKS.clear()
        settings_api._GROUP_UPDATE_LOCK_WAITERS.clear()

    async def test_lock_table_returns_to_empty(self) -> None:
        for group_id in range(-1, -21, -1):
            async with settings_api._group_update_guard(group_id):
                self.assertIn(group_id, settings_api._GROUP_UPDATE_LOCKS)

        self.assertEqual(
            settings_api._GROUP_UPDATE_LOCKS,
            {},
            "锁表必须回落为空，否则键空间随改过设置的群单调增长（A-10）",
        )
        self.assertEqual(settings_api._GROUP_UPDATE_LOCK_WAITERS, {})

    async def test_concurrent_writers_still_share_one_lock(self) -> None:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        observed: list[asyncio.Lock] = []

        async def _hold() -> None:
            async with settings_api._group_update_guard(-777) as lock:
                observed.append(lock)
                first_entered.set()
                await release_first.wait()

        async def _wait_for_lock() -> None:
            await first_entered.wait()
            async with settings_api._group_update_guard(-777) as lock:
                observed.append(lock)

        holder = asyncio.create_task(_hold())
        waiter = asyncio.create_task(_wait_for_lock())
        await first_entered.wait()
        await asyncio.sleep(0)
        # The waiter has asked for the lock but must not have entered yet.
        self.assertEqual(len(observed), 1)
        self.assertIn(-777, settings_api._GROUP_UPDATE_LOCKS)

        release_first.set()
        await asyncio.gather(holder, waiter)

        self.assertEqual(len(observed), 2)
        self.assertIs(
            observed[0],
            observed[1],
            "重叠持有者必须共用同一个锁对象；按 LRU 淘汰正在持有的锁会让并发保护失效",
        )
        self.assertEqual(settings_api._GROUP_UPDATE_LOCKS, {})

    async def test_lock_is_released_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            async with settings_api._group_update_guard(-888):
                raise RuntimeError("put_group_settings blew up")

        self.assertEqual(settings_api._GROUP_UPDATE_LOCKS, {})
        self.assertEqual(settings_api._GROUP_UPDATE_LOCK_WAITERS, {})
