"""修复批 P0-1 / D3-18：群管理员授权要向 Telegram 交叉校验（授权不回收）。

复现的原缺陷（``GAP-D3`` D3-18，``F-047`` / ``B-07`` 的同族设计缺口）::

    # bot/web/settings_api.py
    stmt = (select(Admin.group_id)
        .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
        .where(Admin.user_id == int(user_id), AuthorizedGroup.bot_present.is_(True)))
    # -> {int(v) for v in (await session.scalars(stmt)).all()}

群管理员授权**完全依赖本地 ``Admin`` 表**，从不回 Telegram 核对当前
``getChatAdministrators``；新鲜度只由 ``AuthorizedGroup.bot_present`` 提供，而它表示
「bot 是否在群」，**不表示「人是否还是管理员」**。

后果：Telegram 侧已被撤权/退群的用户，在超管清理 ``Admin`` 行之前仍保有**完整
Mini App 权限**——封禁/解封群成员、篡改群规与关键词回复、改群设置、触发全群巡检
（消耗 LLM 额度并对全体成员发质询）。35 条 ``any_admin`` 端点全部依赖这一判断。

本文件走**真实端到端**：真 ``VerifyWebServer`` + 真签名 initData + 真 SQLite，
只把 Telegram 侧换成假客户端（``get_chat_administrators``）。**不打真实 Telegram API**。
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
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, Group
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.web import settings_api as settings_api_module

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ADMIN_ID = 77
OTHER_ADMIN_ID = 78
GROUP_ID = -1005550001


def _signed_init_data(user_id: int, *, bot_token: str = BOT_TOKEN) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-d3-18-test",
        "user": json.dumps({"id": user_id, "first_name": "Admin"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _member(user_id: int, status: str = "administrator") -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(
            id=user_id, is_bot=False, full_name=f"u{user_id}", username=""
        ),
        status=status,
    )


def _cache() -> dict:
    """修前没有这个缓存；用 ``getattr`` 兜住，好让用例在修前也能跑到断言处
    （否则红的原因会变成 AttributeError，而不是「本该 403 却返回 200」）。"""

    return getattr(settings_api_module, "_ADMIN_REVALIDATION_CACHE", {})


def _clear_cache() -> None:
    cache = getattr(settings_api_module, "_ADMIN_REVALIDATION_CACHE", None)
    if cache is not None:
        cache.clear()


class AdminAuthorizationRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        _clear_cache()

        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key="d3-18-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8480
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-d3-18.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()

        # 「当前 Telegram 侧的管理员名单」——改它就等于模拟撤权/退群。
        self.telegram_admins: list[SimpleNamespace] = [
            _member(GROUP_ADMIN_ID, "creator"),
            _member(OTHER_ADMIN_ID),
        ]
        self.telegram_error: Exception | None = None
        self.admins_rpc = AsyncMock(side_effect=self._get_admins)
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            get_chat_administrators=self.admins_rpc,
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
        self.client = self._make_client(server)
        await self.client.start_server()

        # 先提交 Group / AuthorizedGroup，再写依赖它们的 Admin 行（外键）。
        async with self.session_factory() as session:
            session.add(Group(id=GROUP_ID, title="测试群", settings={}))
            session.add(AuthorizedGroup(group_id=GROUP_ID, bot_present=True))
            await session.commit()
        async with self.session_factory() as session:
            session.add(Admin(group_id=GROUP_ID, user_id=GROUP_ADMIN_ID, role="admin"))
            session.add(Admin(group_id=GROUP_ID, user_id=OTHER_ADMIN_ID, role="admin"))
            await session.commit()

    def _make_client(self, server: VerifyWebServer):
        from aiohttp.test_utils import TestClient, TestServer

        return TestClient(TestServer(server.build_app()))

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        _clear_cache()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _get_admins(self, group_id: int):
        if self.telegram_error is not None:
            raise self.telegram_error
        return list(self.telegram_admins)

    @staticmethod
    def _headers(user_id: int) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def _admins_endpoint(self, user_id: int, status: int | None = None):
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/admins", headers=self._headers(user_id)
        )
        if status is not None:
            self.assertEqual(response.status, status, await response.text())
        return response

    # ---- 核心回归 --------------------------------------------------------

    async def test_current_admin_has_access(self) -> None:
        """前置：授权正常时群管理员能读该群（确认用例本身是通的）。"""

        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)

    async def test_revoked_admin_loses_mini_app_access(self) -> None:
        """核心回归：Telegram 侧已撤权 → 立刻 403，不等超管清理本地 ``Admin`` 行。"""

        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)

        # 本地 ``Admin`` 行**仍然在**（这正是原缺陷的前提：只有超管清理它才会消失）。
        # Telegram 侧已把这个人从管理员名单里去掉。
        self.telegram_admins = [_member(OTHER_ADMIN_ID)]
        _clear_cache()

        response = await self._admins_endpoint(GROUP_ADMIN_ID, status=403)
        self.assertEqual((await response.json())["error"]["code"], "group_access_denied")

    async def test_admin_who_left_the_group_loses_access(self) -> None:
        """已退群（完全不在 Telegram 名单里）同样 403。"""

        self.telegram_admins = []
        _clear_cache()

        await self._admins_endpoint(GROUP_ADMIN_ID, status=403)

    async def test_mutating_endpoint_is_also_blocked(self) -> None:
        """变更类端点同样被挡住（不只是只读列表）。"""

        self.telegram_admins = []
        _clear_cache()

        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/rules",
            headers=self._headers(GROUP_ADMIN_ID),
            json={"pattern": "测试规则", "rule_type": "keyword"},
        )
        self.assertEqual(response.status, 403, await response.text())
        self.assertEqual((await response.json())["error"]["code"], "group_access_denied")

    async def test_other_admin_is_unaffected(self) -> None:
        """同一群里另一位仍在 Telegram 名单上的管理员不受牵连。"""

        self.telegram_admins = [_member(OTHER_ADMIN_ID)]
        _clear_cache()

        await self._admins_endpoint(OTHER_ADMIN_ID, status=200)
        await self._admins_endpoint(GROUP_ADMIN_ID, status=403)

    # ---- 不能把功能一起修没 ----------------------------------------------

    async def test_super_admin_is_unaffected(self) -> None:
        """最高管理员不受 Telegram 交叉校验约束（与既有行为一致）。"""

        self.telegram_admins = []
        _clear_cache()

        response = await self.client.get("/api/v1/groups", headers=self._headers(SUPER_ADMIN_ID))
        self.assertEqual(response.status, 200, await response.text())

    async def test_telegram_outage_degrades_instead_of_locking_admins_out(self) -> None:
        """离线降级：问不到 Telegram 时按本地表放行，不把管理员锁在面板外。

        这是相对报告「变更类端点 fail-closed」建议的**刻意偏离**，理由见
        FIX-security.md：把「查不到」和「确认没有」混成同一结果会在两个方向出错。
        """

        self.telegram_error = RuntimeError("flood wait")
        _clear_cache()

        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)

    async def test_outage_does_not_poison_the_cache(self) -> None:
        """问不到时**不写缓存**，恢复后立刻能拿到权威结论。"""

        self.telegram_error = RuntimeError("flood wait")
        _clear_cache()
        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)
        self.assertNotIn(
            (GROUP_ID, GROUP_ADMIN_ID), _cache()
        )

        self.telegram_error = None
        self.telegram_admins = []
        await self._admins_endpoint(GROUP_ADMIN_ID, status=403)

    async def test_verdict_is_cached_for_the_ttl(self) -> None:
        _clear_cache()
        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)
        first = self.admins_rpc.await_count
        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)
        self.assertEqual(self.admins_rpc.await_count, first)

    async def test_grant_invalidates_a_stale_negative_verdict(self) -> None:
        """先撤权（缓存负结论）→ 超管重新授权 → 不能被负缓存卡住。"""

        self.telegram_admins = []
        _clear_cache()
        await self._admins_endpoint(GROUP_ADMIN_ID, status=403)
        # 负结论也缓存：否则被撤权的人每发一个请求就换一次 Telegram RPC。
        self.assertEqual(
            _cache()[(GROUP_ID, GROUP_ADMIN_ID)][1],
            False,
        )

        self.telegram_admins = [_member(GROUP_ADMIN_ID, "creator")]
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/admins",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"user_id": GROUP_ADMIN_ID, "role": "admin"},
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertNotIn(
            (GROUP_ID, GROUP_ADMIN_ID), _cache()
        )

        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)

    async def test_revoke_invalidates_a_stale_positive_verdict(self) -> None:
        """反向也要成立：删掉授权记录后，缓存里的「仍是管理员」立刻作废。"""

        self.telegram_admins = [_member(GROUP_ADMIN_ID, "creator")]
        _clear_cache()
        await self._admins_endpoint(GROUP_ADMIN_ID, status=200)
        self.assertEqual(
            _cache()[(GROUP_ID, GROUP_ADMIN_ID)][1],
            True,
        )

        response = await self.client.delete(
            f"/api/v1/groups/{GROUP_ID}/admins/{GROUP_ADMIN_ID}",
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertNotIn(
            (GROUP_ID, GROUP_ADMIN_ID), _cache()
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
