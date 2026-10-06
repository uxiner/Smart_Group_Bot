"""``restart_pending`` 的端到端：真 manager + 真 SQLite 连续保存 / CAS 冲突 / 重启。

父代理探针 E 只比较了"两个 dict 的 diff"，不足以证明真实链路。这里跑完整的
``RuntimeConfigManager``：初始化 → 保存 → 再保存 → 冲突 → 模拟重启，并断言：

* 改完 LLM 容量后，**第二次只改热字段**的保存**不会**把提示清掉（探针 E 的正解）；
* 保存成"实际运行值"后提示自动消失（不是单调集合）；
* 保存同一个值 / 只保存热值都不产生提示；
* CAS 冲突（过期 revision）不污染 pending，库里的文档与 pending 都不动；
* "重启"（新 manager 从库里加载）之后 pending 被清空，并且新的保存又重新算。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import (
    RuntimeConfigConflictError,
    RuntimeConfigManager,
    applied_restart_values,
    record_applied_restart_values,
)


class RestartPendingEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._restore = []

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _manager(self) -> RuntimeConfigManager:
        settings = Settings(
            _env_file=None,
            bot_token="42:TEST",
            super_admin_id=42,
            config_master_key="restart-pending-e2e-key",
        )
        settings.bot.token = "42:TEST"
        manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=settings,
            legacy_config_path="/tmp/nonexistent-restart-pending.toml",
            legacy_raw_env={},
        )
        self._restore.append((manager.settings, manager))
        return manager

    @staticmethod
    def _bump(resources: dict) -> dict:
        return {
            **resources,
            "llm_request_capacity": 16,
            "llm_request_noncritical_capacity": 14,
            "llm_request_normal_capacity": 8,
            "llm_request_background_capacity": 3,
        }

    async def test_pending_survives_a_later_hot_only_save(self) -> None:
        manager = self._manager()
        await manager.initialize()
        self.assertEqual(manager.api_document()["restart_pending"], [])

        payload = manager.api_document()["config"]
        payload["resources"] = self._bump(payload["resources"])
        saved = await manager.save(
            payload, expected_revision=manager.revision, updated_by=42
        )
        first_pending = saved.restart_pending_paths(manager._applied_restart)
        self.assertIn("resources.llm_request_capacity", first_pending)

        # 只改热字段的第二次保存
        payload2 = saved.storage_payload()
        payload2["resources"]["moderation_throttle_burst"] = 4
        saved2 = await manager.save(
            payload2, expected_revision=manager.revision, updated_by=42
        )
        second_pending = saved2.restart_pending_paths(manager._applied_restart)
        self.assertEqual(
            second_pending,
            first_pending,
            "只改热字段不该把「还没重启」的提示清掉",
        )
        self.assertEqual(
            manager.api_document()["restart_pending"],
            list(first_pending),
        )
        # 进程里真正在跑的还是默认值
        self.assertEqual(
            applied_restart_values()["resources.llm_request_capacity"], 8
        )

    async def test_saving_the_running_value_again_clears_the_hint(self) -> None:
        manager = self._manager()
        await manager.initialize()
        payload = manager.api_document()["config"]
        payload["resources"] = self._bump(payload["resources"])
        await manager.save(
            payload, expected_revision=manager.revision, updated_by=42
        )
        self.assertIn(
            "resources.llm_request_capacity", manager.api_document()["restart_pending"]
        )

        # 改回进程实际在跑的 8
        current = manager.api_document()["config"]
        current["resources"] = manager._applied_restart
        current["resources"] = {
            key: value
            for key, value in current["resources"].items()
        }
        current = manager.api_document()["config"]
        current["resources"].update(
            {
                "llm_request_capacity": 8,
                "llm_request_noncritical_capacity": 7,
                "llm_request_normal_capacity": 4,
                "llm_request_background_capacity": 2,
            }
        )
        await manager.save(
            current, expected_revision=manager.revision, updated_by=42
        )
        self.assertEqual(
            manager.api_document()["restart_pending"],
            [],
            "改回实际运行值就该消除提示（不是单调集合）",
        )

    async def test_saving_the_same_value_produces_no_hint(self) -> None:
        manager = self._manager()
        await manager.initialize()
        payload = manager.api_document()["config"]
        await manager.save(
            payload, expected_revision=manager.revision, updated_by=42
        )
        self.assertEqual(manager.api_document()["restart_pending"], [])

    async def test_cas_conflict_does_not_pollute_pending_or_the_stored_doc(self) -> None:
        manager = self._manager()
        await manager.initialize()
        payload = manager.api_document()["config"]
        payload["resources"] = self._bump(payload["resources"])
        await manager.save(
            payload, expected_revision=manager.revision, updated_by=42
        )
        pending_before = list(manager.api_document()["restart_pending"])
        config_before = manager.api_document()["config"]
        revision_before = manager.revision

        stale = dict(config_before)
        stale["resources"] = {
            **config_before["resources"],
            "llm_request_capacity": 24,
            "llm_request_noncritical_capacity": 20,
            "llm_request_normal_capacity": 12,
            "llm_request_background_capacity": 4,
        }
        with self.assertRaises(RuntimeConfigConflictError):
            await manager.save(
                stale, expected_revision=revision_before - 1, updated_by=42
            )

        document = manager.api_document()
        self.assertEqual(document["revision"], revision_before, "冲突不能改 revision")
        self.assertEqual(document["config"], config_before, "冲突不能改库里的文档")
        self.assertEqual(document["restart_pending"], pending_before)

    async def test_a_restart_clears_pending_and_rebases_the_baseline(self) -> None:
        manager = self._manager()
        await manager.initialize()
        payload = manager.api_document()["config"]
        payload["resources"] = self._bump(payload["resources"])
        await manager.save(
            payload, expected_revision=manager.revision, updated_by=42
        )
        self.assertIn(
            "resources.llm_request_capacity", manager.api_document()["restart_pending"]
        )

        # 「重启」：新 manager 从库里加载
        restarted = self._manager()
        await restarted.initialize()
        self.assertEqual(
            restarted.api_document()["restart_pending"],
            [],
            "重启之后新值已生效，提示应当清空",
        )
        self.assertEqual(
            applied_restart_values()["resources.llm_request_capacity"],
            16,
            "重启后基准应当变成真正在跑的值",
        )
        # 重启之后再保存一次同样的值，不应又冒出提示
        document = restarted.api_document()["config"]
        await restarted.save(
            document, expected_revision=restarted.revision, updated_by=42
        )
        self.assertEqual(restarted.api_document()["restart_pending"], [])

    async def test_a_save_after_restart_again_reports_a_new_difference(self) -> None:
        first = self._manager()
        await first.initialize()
        payload = first.api_document()["config"]
        payload["resources"] = self._bump(payload["resources"])
        await first.save(
            payload, expected_revision=first.revision, updated_by=42
        )

        restarted = self._manager()
        await restarted.initialize()
        self.assertEqual(restarted.api_document()["restart_pending"], [])

        # 重启之后再想调回 8：这是**新的**待重启差异
        document = restarted.api_document()["config"]
        document["resources"].update(
            {
                "llm_request_capacity": 8,
                "llm_request_noncritical_capacity": 7,
                "llm_request_normal_capacity": 4,
                "llm_request_background_capacity": 2,
            }
        )
        saved = await restarted.save(
            document, expected_revision=restarted.revision, updated_by=42
        )
        self.assertIn(
            "resources.llm_request_capacity",
            saved.restart_pending_paths(restarted._applied_restart),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
