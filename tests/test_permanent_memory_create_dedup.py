"""B9-1：永久备忘新建去重的原子性。

覆盖三件事：

1. **启动迁移**：已有库里的 ``(group_id, content)`` 重复行在建唯一索引之前被收敛
   为一行（保留 ``id`` 最大的一行），迁移幂等，无重复的库一行不动，唯一键按群隔离。
   索引与行数都用 ``PRAGMA``/SQL 读回，不只看代码。
2. **写入路径原子性**：并发同内容新建不可能产生第二行。竞态注入点放在父代理 v3
   实测的窗口——**去重 SELECT 已经返回、写锁尚未取得**（写锁在 ``commit()`` 里才
   获取）。注入用独立的 ``sqlite3`` 连接，绕开进程内 asyncio 写锁。
3. **B8 已验收语义不回退**：``replace`` 的同内容复用、目标删除后重建、多条命中，
   以及顺序幂等正控制。

真 ``init_db`` + 真 SQLite；Mini App 路径走真 ``VerifyWebServer`` + 真 HTTP；
自然语言路径走真 ``MemoryService.add_permanent_memory``。只替换认证旁路，
不 mock service、不 mock outcome。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.config import BotConfig
from bot.config import Settings
from bot.db import sqlite_session as ss
from bot.db.engine import init_db
from bot.db.models import AuthorizedGroup, Group, GroupPermanentMemory
from bot.services.memory import MemoryService
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN = 42
GROUP_A = -1001234567890
GROUP_B = -1009999999999

UNIQUE_INDEX = "ix_group_permanent_memories_group_content"

_LEGACY_TABLE_SQL = """
CREATE TABLE group_permanent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL,
    content TEXT,
    created_by BIGINT,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAF-dedup",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner"}, separators=(",", ":")
        ),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _index_rows(db_path: str) -> list[tuple[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "PRAGMA index_list('group_permanent_memories')"
        ).fetchall()
        return [(str(row[1]), int(row[2])) for row in rows]
    finally:
        conn.close()


def _index_columns(db_path: str, name: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_info('{name}')").fetchall()
        return [str(row[2]) for row in rows]
    finally:
        conn.close()


def _legacy_rows(db_path: str) -> list[tuple[int, int, str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, group_id, content, created_by FROM group_permanent_memories"
            " ORDER BY id"
        ).fetchall()
        return [(int(r[0]), int(r[1]), str(r[2]), int(r[3] or 0)) for r in rows]
    finally:
        conn.close()


class _MigrationTestCase(unittest.IsolatedAsyncioTestCase):
    """在「已有库」上跑启动迁移：先用原生 sqlite3 造出 B9 之前的历史形状。"""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engines: list = []

    async def asyncTearDown(self) -> None:
        for engine in self.engines:
            await engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(f"{self.db_path}{suffix}")
            except FileNotFoundError:
                pass

    def seed_legacy(self, rows: list[tuple[int, str, int]]) -> None:
        """造一个「B9 之前的库」：有行、没有唯一索引。"""

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(_LEGACY_TABLE_SQL)
            for group_id, content, created_by in rows:
                conn.execute(
                    "INSERT INTO group_permanent_memories"
                    " (group_id, content, created_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                    (group_id, content, created_by),
                )
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn(
            UNIQUE_INDEX,
            [name for name, _unique in _index_rows(self.db_path)],
            "夹具必须从没有唯一索引的历史形状开始",
        )

    async def migrate(self):
        url = f"sqlite+aiosqlite:///{self.db_path}"
        engine, session_factory = await init_db(url)
        self.engines.append(engine)
        return engine, session_factory


class PermanentMemoryMigrationTests(_MigrationTestCase):
    async def test_duplicates_are_collapsed_and_the_unique_key_is_created(self) -> None:
        """重复行收敛为 id 最大的一行，随后唯一索引真的存在且列正确。"""

        self.seed_legacy(
            [
                (GROUP_A, "重复内容", 11),
                (GROUP_A, "另一条", 12),
                (GROUP_A, "重复内容", 13),
                (GROUP_A, "重复内容", 14),
            ]
        )
        await self.migrate()

        rows = _legacy_rows(self.db_path)
        self.assertEqual(
            [(group_id, content) for _id, group_id, content, _by in rows],
            [(GROUP_A, "另一条"), (GROUP_A, "重复内容")],
        )
        # 保留的是 id 最大的一行（与 message_vectors 的 MAX(rowid) 语义一致）。
        kept = [row for row in rows if row[2] == "重复内容"]
        self.assertEqual([row[0] for row in kept], [4])
        self.assertEqual(kept[0][3], 14)

        self.assertIn(UNIQUE_INDEX, [name for name, _u in _index_rows(self.db_path)])
        self.assertEqual(
            _index_columns(self.db_path, UNIQUE_INDEX), ["group_id", "content"]
        )
        unique_flags = {name: flag for name, flag in _index_rows(self.db_path)}
        self.assertEqual(unique_flags[UNIQUE_INDEX], 1)

    async def test_migration_is_idempotent_across_restarts(self) -> None:
        """第二次启动不删行、不报错，索引保持不变。"""

        self.seed_legacy(
            [
                (GROUP_A, "重复内容", 11),
                (GROUP_A, "重复内容", 13),
                (GROUP_B, "重复内容", 21),
                (GROUP_B, "重复内容", 22),
            ]
        )
        await self.migrate()
        first = _legacy_rows(self.db_path)
        self.assertEqual(len(first), 2)

        await self.migrate()
        self.assertEqual(_legacy_rows(self.db_path), first)
        self.assertIn(UNIQUE_INDEX, [name for name, _u in _index_rows(self.db_path)])

        # 再启动一次（第三次）：依然一行不删、不报错。
        await self.migrate()
        self.assertEqual(_legacy_rows(self.db_path), first)

    async def test_a_library_without_duplicates_keeps_every_row(self) -> None:
        """无重复的库：一行不删（用 id 与内容对账，不只看行数）。"""

        self.seed_legacy(
            [
                (GROUP_A, "内容一", 1),
                (GROUP_A, "内容二", 2),
                (GROUP_B, "内容一", 3),
            ]
        )
        before = _legacy_rows(self.db_path)
        await self.migrate()
        self.assertEqual(_legacy_rows(self.db_path), before)
        self.assertIn(UNIQUE_INDEX, [name for name, _u in _index_rows(self.db_path)])

    async def test_the_unique_key_is_per_group(self) -> None:
        """同内容不同群必须都活下来——唯一键含 group_id。"""

        self.seed_legacy(
            [
                (GROUP_A, "共享内容", 1),
                (GROUP_B, "共享内容", 2),
            ]
        )
        await self.migrate()
        rows = _legacy_rows(self.db_path)
        self.assertEqual(
            sorted((group_id, content) for _i, group_id, content, _b in rows),
            sorted([(GROUP_A, "共享内容"), (GROUP_B, "共享内容")]),
        )

    async def test_a_fresh_database_gets_the_unique_key(self) -> None:
        """全新库由 create_all 建索引。"""

        await self.migrate()
        self.assertIn(UNIQUE_INDEX, [name for name, _u in _index_rows(self.db_path)])


class _RaceInjectedTestCase(unittest.IsolatedAsyncioTestCase):
    """带「去重 SELECT 之后、写锁之前」注入钩子的基类。"""

    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN,
            config_master_key="permanent-memory-dedup-test-key",
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
            legacy_config_path="/tmp/nonexistent-permanent-memory-dedup.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(token=BOT_TOKEN)
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()
        self._patched = False
        self.race_events: list[tuple[str, str]] = []
        await self._seed_groups()

    async def asyncTearDown(self) -> None:
        self._uninstall_race_hook()
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(f"{self.db_path}{suffix}")
            except FileNotFoundError:
                pass

    async def _seed_groups(self) -> None:
        async with self.session_factory() as session:
            session.add_all(
                [
                    AuthorizedGroup(
                        group_id=GROUP_A, authorized_by=SUPER_ADMIN, bot_present=True
                    ),
                    AuthorizedGroup(
                        group_id=GROUP_B, authorized_by=SUPER_ADMIN, bot_present=True
                    ),
                    Group(id=GROUP_A, title="合成群 A", settings={}),
                    Group(id=GROUP_B, title="合成群 B", settings={}),
                ]
            )
            await session.commit()

    # ---- 竞态钩子：外部 writer 在「去重 SELECT 已返回、写锁未取」时插入同一行 ----

    def _install_race_hook(self, *, group_id: int, content: str, created_by: int) -> None:
        """在 ``scalar``/``execute`` 的 group_permanent_memories SELECT 返回后注入。

        HTTP 路径的查重走 ``AsyncSession.scalar``，服务路径走 ``execute``：两个都
        挂钩，否则会拿到「注入没发生」的假绿（父代理 v1 的教训）。
        """

        real_execute = ss.SQLiteSafeAsyncSession.execute
        real_scalar = ss.SQLiteSafeAsyncSession.scalar
        self._original_methods = {
            "execute": real_execute,
            "scalar": real_scalar,
        }
        state = {"done": False}

        def _inject(stmt: str) -> None:
            if state["done"] or "group_permanent_memories" not in stmt:
                return
            if not stmt.strip().upper().startswith("SELECT"):
                return
            state["done"] = True
            try:
                conn = sqlite3.connect(self.db_path, timeout=1.0)
                conn.execute(
                    "INSERT INTO group_permanent_memories"
                    " (group_id, content, created_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                    (group_id, content, created_by),
                )
                conn.commit()
                conn.close()
                self.race_events.append(("inject", "ok"))
            except Exception as exc:  # pragma: no cover - 只用于诊断
                self.race_events.append(("inject", f"{type(exc).__name__}: {exc}"))

        async def traced_execute(self_session, statement, *args, **kwargs):
            result = await real_execute(self_session, statement, *args, **kwargs)
            try:
                stmt = str(statement)
            except Exception:  # pragma: no cover - 诊断用
                stmt = "<unprintable>"
            self.race_events.append(("execute", " ".join(stmt.split())[:90]))
            _inject(stmt)
            return result

        async def traced_scalar(self_session, statement, *args, **kwargs):
            result = await real_scalar(self_session, statement, *args, **kwargs)
            try:
                stmt = str(statement)
            except Exception:  # pragma: no cover - 诊断用
                stmt = "<unprintable>"
            self.race_events.append(("scalar", " ".join(stmt.split())[:90]))
            _inject(stmt)
            return result

        ss.SQLiteSafeAsyncSession.execute = traced_execute
        ss.SQLiteSafeAsyncSession.scalar = traced_scalar
        self._patched = True

    def _uninstall_race_hook(self) -> None:
        """把类方法逐字还回去。

        不能靠 ``importlib.reload``：``async_sessionmaker`` 在 ``init_db`` 时就抓住了
        旧的类对象，重载只会造出一个新类，补丁仍留在被实际使用的那个类上。
        """

        if not self._patched:
            return
        ss.SQLiteSafeAsyncSession.execute = self._original_methods["execute"]
        ss.SQLiteSafeAsyncSession.scalar = self._original_methods["scalar"]
        self._patched = False

    # ---- 读取真值 ----

    async def _rows(self, *, group_id: int | None = None):
        async with self.session_factory() as session:
            sql = (
                "SELECT id, group_id, content, created_by FROM group_permanent_memories"
            )
            params: dict = {}
            if group_id is not None:
                sql += " WHERE group_id = :group_id"
                params["group_id"] = group_id
            sql += " ORDER BY id"
            result = await session.execute(text(sql), params)
            return [
                (int(r[0]), int(r[1]), str(r[2]), int(r[3] or 0)) for r in result.all()
            ]

    async def _assert_write_lock_available(self, timeout: float = 1.0) -> None:
        """真实独立 writer 在同一事件循环里限时完成，证明写锁已归还。"""

        import asyncio

        async def _write() -> None:
            async with self.session_factory() as session:
                await session.execute(
                    text("DELETE FROM group_permanent_memories WHERE id = -1")
                )
                await session.commit()

        await asyncio.wait_for(_write(), timeout)

    def _headers(self, user_id: int = SUPER_ADMIN) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _service(self) -> MemoryService:
        return MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            SimpleNamespace(),
            session_factory=self.session_factory,
        )


class PermanentMemoryCreateDedupHttpTests(_RaceInjectedTestCase):
    async def test_concurrent_same_content_create_produces_exactly_one_row(self) -> None:
        """核心断言：父代理 v3 的窗口里，HTTP 200 且库里只有一行。"""

        content = "并发同内容"
        self._install_race_hook(group_id=GROUP_A, content=content, created_by=999)
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_A}/memories",
            headers=self._headers(),
            json={"content": content},
        )
        body = await response.json()

        self.assertEqual(
            [detail for kind, detail in self.race_events if kind == "inject"],
            ["ok"],
            "竞态注入必须真的发生过，否则这个用例是假绿",
        )
        self.assertEqual(response.status, 200)
        self.assertIs(body.get("ok"), True)

        rows = await self._rows(group_id=GROUP_A)
        self.assertEqual(len(rows), 1, f"并发同内容必须只有一行，实际 {rows!r}")
        self.assertEqual(rows[0][2], content)
        # 返回的就是库里那一行（竞态方是 999，本请求读到的是它）。
        self.assertEqual(body["memory"]["id"], rows[0][0])
        self.assertEqual(body["memory"]["content"], content)

        await self._assert_write_lock_available()

    async def test_sequential_same_content_create_stays_idempotent(self) -> None:
        """正控制：顺序两次同内容仍是 1 行、同一个 id。"""

        content = "顺序同内容"
        first = await self.client.post(
            f"/api/v1/groups/{GROUP_A}/memories",
            headers=self._headers(),
            json={"content": content},
        )
        second = await self.client.post(
            f"/api/v1/groups/{GROUP_A}/memories",
            headers=self._headers(),
            json={"content": content},
        )
        first_body = await first.json()
        second_body = await second.json()

        self.assertEqual([first.status, second.status], [200, 200])
        self.assertEqual(first_body["memory"]["id"], second_body["memory"]["id"])
        self.assertEqual(len(await self._rows(group_id=GROUP_A)), 1)

    async def test_same_content_in_another_group_is_a_separate_row(self) -> None:
        content = "跨群同内容"
        for group_id in (GROUP_A, GROUP_B):
            response = await self.client.post(
                f"/api/v1/groups/{group_id}/memories",
                headers=self._headers(),
                json={"content": content},
            )
            self.assertEqual(response.status, 200)

        self.assertEqual(len(await self._rows(group_id=GROUP_A)), 1)
        self.assertEqual(len(await self._rows(group_id=GROUP_B)), 1)
        self.assertNotEqual(
            (await self._rows(group_id=GROUP_A))[0][0],
            (await self._rows(group_id=GROUP_B))[0][0],
        )

    async def test_create_still_rejects_blank_content(self) -> None:
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_A}/memories",
            headers=self._headers(),
            json={"content": "   "},
        )
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "empty_memory")
        self.assertEqual(await self._rows(), [])


class PermanentMemoryAddDedupServiceTests(_RaceInjectedTestCase):
    async def test_concurrent_same_content_add_returns_the_existing_row(self) -> None:
        """自然语言路径同样只有一行，且返回既有行、created=False。"""

        content = "服务层并发同内容"
        self._install_race_hook(group_id=GROUP_A, content=content, created_by=999)
        row, created = await self._service().add_permanent_memory(
            GROUP_A, content, created_by=SUPER_ADMIN
        )

        injects = [d for k, d in self.race_events if k == "inject"]
        self.assertEqual(injects, ["ok"], "竞态注入必须真的发生过")
        self.assertFalse(created, "并发冲突时必须读回既有行，不能声称新建")
        self.assertIsNotNone(row)

        rows = await self._rows(group_id=GROUP_A)
        self.assertEqual(len(rows), 1, f"服务层并发同内容必须只有一行，实际 {rows!r}")
        self.assertEqual(int(row.id), rows[0][0])
        self.assertEqual(rows[0][2], content)
        await self._assert_write_lock_available()

    async def test_add_is_idempotent_sequentially(self) -> None:
        content = "服务层顺序同内容"
        service = self._service()
        first, created_first = await service.add_permanent_memory(
            GROUP_A, content, created_by=SUPER_ADMIN
        )
        second, created_second = await service.add_permanent_memory(
            GROUP_A, content, created_by=SUPER_ADMIN
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(int(first.id), int(second.id))
        self.assertEqual(len(await self._rows(group_id=GROUP_A)), 1)

    async def test_blank_content_is_still_rejected_without_writing(self) -> None:
        row, created = await self._service().add_permanent_memory(
            GROUP_A, "   ", created_by=SUPER_ADMIN
        )
        self.assertIsNone(row)
        self.assertFalse(created)
        self.assertEqual(await self._rows(), [])


class PermanentMemoryReplaceRegressionTests(_RaceInjectedTestCase):
    """B8 已验收的 replace 语义在唯一索引下不得回退。"""

    async def _seed(self, group_id: int, *contents: str) -> list[int]:
        async with self.session_factory() as session:
            rows = [
                GroupPermanentMemory(
                    group_id=group_id, content=content, created_by=SUPER_ADMIN
                )
                for content in contents
            ]
            session.add_all(rows)
            await session.commit()
            return [int(row.id) for row in rows]

    async def test_replace_reuses_an_existing_same_content_row(self) -> None:
        ids = await self._seed(GROUP_A, "旧内容", "目标内容")
        outcome = await self._service().replace_permanent_memory_outcome(
            GROUP_A, target="旧内容", new_content="目标内容", created_by=SUPER_ADMIN
        )

        self.assertEqual(outcome.status, "updated")
        self.assertFalse(outcome.created_new, "同内容既有行必须复用，不能新建")
        self.assertEqual(outcome.memory_id, ids[1])
        self.assertEqual(outcome.content, "目标内容")
        rows = await self._rows(group_id=GROUP_A)
        self.assertEqual([row[2] for row in rows], ["目标内容"])

    async def test_replace_recreates_the_row_when_the_target_was_the_only_copy(
        self,
    ) -> None:
        """目标行与「同内容既有行」是同一行时，删除后重建不得撞唯一索引。"""

        await self._seed(GROUP_A, "同名内容")
        outcome = await self._service().replace_permanent_memory_outcome(
            GROUP_A,
            target="同名内容",
            new_content="同名内容",
            created_by=SUPER_ADMIN,
        )

        self.assertEqual(outcome.status, "updated")
        rows = await self._rows(group_id=GROUP_A)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "同名内容")

    async def test_replace_collapses_multiple_targets_without_a_unique_violation(
        self,
    ) -> None:
        """多条命中且目标被清空后，仍能落成一行。"""

        # 唯一索引上线后不可能再有同内容重复行；多条命中改用「都含关键词的不同内容」。
        await self._seed(GROUP_A, "重复目标甲", "重复目标乙", "另一个目标")
        outcome = await self._service().replace_permanent_memory_outcome(
            GROUP_A, target="目标", new_content="合并后的内容", created_by=SUPER_ADMIN
        )

        self.assertEqual(outcome.status, "updated")
        rows = await self._rows(group_id=GROUP_A)
        self.assertEqual([row[2] for row in rows], ["合并后的内容"])

    async def test_replace_reports_not_found_and_invalid_content_unchanged(self) -> None:
        await self._seed(GROUP_A, "保留内容")
        service = self._service()

        missing = await service.replace_permanent_memory_outcome(
            GROUP_A, target="不存在的关键词", new_content="新内容", created_by=SUPER_ADMIN
        )
        invalid = await service.replace_permanent_memory_outcome(
            GROUP_A, target="保留内容", new_content="   ", created_by=SUPER_ADMIN
        )

        self.assertEqual(missing.status, "not_found")
        self.assertEqual(invalid.status, "invalid_new_content")
        self.assertEqual(
            [row[2] for row in await self._rows(group_id=GROUP_A)], ["保留内容"]
        )

    async def test_replace_into_another_group_does_not_leak(self) -> None:
        await self._seed(GROUP_A, "甲群内容")
        await self._seed(GROUP_B, "乙群内容")
        await self._service().replace_permanent_memory_outcome(
            GROUP_A, target="甲群内容", new_content="乙群内容", created_by=SUPER_ADMIN
        )

        self.assertEqual(
            [row[2] for row in await self._rows(group_id=GROUP_A)], ["乙群内容"]
        )
        self.assertEqual(
            [row[2] for row in await self._rows(group_id=GROUP_B)], ["乙群内容"]
        )


class PermanentMemoryMigrationProtocolTests(_MigrationTestCase):
    """迁移函数自身的契约：已正确时早退（返回 ``False``），需要建键时返回 ``True``。

    基线上这个符号不存在，所以本类在旧代码里是**缺符号兼容红**，不计入业务红检；
    守卫放在 ``asyncSetUp`` 里，避免整类在收集期炸掉。
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        try:
            from bot.db.engine import _sqlite_ensure_group_permanent_memory_unique_key
        except Exception as exc:  # pragma: no cover - 只在旧基线上走这里
            self.fail(f"bot.db.engine._sqlite_ensure_group_permanent_memory_unique_key 不可导入：{exc!r}")
        self.ensure_unique_key = _sqlite_ensure_group_permanent_memory_unique_key

    async def test_returns_true_when_it_creates_the_key_and_false_afterwards(self) -> None:
        self.seed_legacy([(GROUP_A, "重复内容", 1), (GROUP_A, "重复内容", 2)])
        # 刻意**不**走 init_db：那会把迁移一起跑掉，就测不到「需要建键」这一支。
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        self.engines.append(engine)

        async with engine.begin() as conn:
            self.assertTrue(await self.ensure_unique_key(conn))
        async with engine.begin() as conn:
            self.assertFalse(await self.ensure_unique_key(conn))
        self.assertEqual(len(_legacy_rows(self.db_path)), 1)

    async def test_returns_false_on_an_already_matching_library(self) -> None:
        engine, _factory = await self.migrate()
        async with engine.begin() as conn:
            self.assertFalse(await self.ensure_unique_key(conn))

    async def test_is_a_no_op_when_the_table_does_not_exist(self) -> None:
        engine, _factory = await self.migrate()
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE group_permanent_memories"))
        async with engine.begin() as conn:
            self.assertFalse(await self.ensure_unique_key(conn))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
