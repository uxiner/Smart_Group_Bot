"""修复批 P1-3 / C3-05：``/healthz`` 的 DB 健康项必须真的执行一条查询。

修前的 ``database_pool_health_snapshot``（``bot/db/engine.py``）只读连接池**计数器**
（checkedout / pool_size / overflow / utilization），一次 SQL 都不发::

    # bot/db/engine.py
    pool = engine.sync_engine.pool
    ...
    return {"ok": ratio < 0.80, "fatal": ratio >= 0.95, "checked_out": checked_out, ...}

后果：DB 文件损坏、磁盘满、SQLite 锁死、WAL 异常时 ``/healthz`` 仍然返回 **200** ——
对最需要它的故障是瞎的；compose 的 healthcheck（``docker-compose.yml``）也依赖它，
所以编排同样发现不了。

本文件走**真 SQLite**（``init_db`` 真的建库、真的起引擎），只把「把库文件写坏」
这一步当作故障注入。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bot.db.engine import init_db
from bot.services.resource_health import (
    resource_health_snapshot,
    unregister_resource_health_provider,
)


def _database_health() -> dict:
    return resource_health_snapshot()["resources"]["database_pool"]


class DatabaseLivenessProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        unregister_resource_health_provider("database_pool")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    async def test_healthy_database_reports_a_successful_probe(self) -> None:
        health = _database_health()
        self.assertTrue(health["probe_ok"], health)
        self.assertTrue(health["ok"], health)
        self.assertEqual(health["error"], "")
        # 计数器仍然保留（排障要看），但 ok 不再只由它们决定。
        self.assertIn("checked_out", health)

    async def test_unreadable_database_file_flips_health_to_false(self) -> None:
        self.assertTrue(_database_health()["probe_ok"])
        # 故障注入：把主库文件连同 WAL/SHM 一起搬走，只留一个同名的目录占位。
        # 进程还活着、连接池计数器一切正常，但它已经打不开自己的库——这正是
        # compose `user:` 覆盖镜像非 root 用户（C3-01）时最容易踩到的形态。
        for suffix in ("-wal", "-shm"):
            os.remove(self.path + suffix)
        os.remove(self.path)
        os.mkdir(self.path)
        self.addCleanup(os.rmdir, self.path)
        health = _database_health()
        self.assertFalse(health["probe_ok"], health)
        self.assertFalse(health["ok"], health)
        # 探测失败不等于「资源耗尽」，不触发立刻重启路径。
        self.assertFalse(health["fatal"], health)
        self.assertIn("OperationalError", health["error"])

    async def test_write_ahead_log_io_error_flips_health_to_false(self) -> None:
        """WAL 侧 I/O 错误必须让探测翻红——这是 C3-05 点名的场景之一。

        故障注入用「查询报 ``disk I/O error``」本身，而不是往 ``-wal`` 里写垃圾字节：
        后者在不同 SQLite 构建上判定不同——生产镜像里的构建把它当作「无效 WAL」直接
        忽略（``SELECT 1`` 根本不碰 WAL，探测照样成功），macOS 上的构建才会抛错。
        这里要钉死的是「I/O 错误必须被翻译成 ``probe_ok=False`` 且错误原样带出」，
        真文件的两种损坏形态由本文件另外两条用例覆盖（主库搬走 / 主库缺失）。
        """

        self.assertTrue(_database_health()["probe_ok"])
        failure = sqlite3.OperationalError("disk I/O error")
        with patch("sqlite3.connect", side_effect=failure):
            health = _database_health()
        self.assertFalse(health["probe_ok"], health)
        self.assertFalse(health["ok"], health)
        self.assertIn("disk I/O error", health["error"])

    async def test_missing_database_file_flips_health_to_false(self) -> None:
        os.remove(self.path)
        health = _database_health()
        self.assertFalse(health["probe_ok"], health)
        self.assertFalse(health["ok"], health)
