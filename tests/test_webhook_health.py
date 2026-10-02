from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiogram import Bot, Dispatcher

from bot.services.verify_web import (
    _QueuedWebhookUpdate,
    _WebhookUpdateQueue,
    _redact_health_diagnostics,
)


class WebhookHealthBookkeepingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = Bot(token="42:TEST_TOKEN")
        self.queue = _WebhookUpdateQueue(
            dispatcher=Dispatcher(),
            bot=self.bot,
            session_factory=None,
            secret_token="s" * 32,
            worker_count=1,
        )

    async def asyncTearDown(self) -> None:
        await self.queue.stop()
        await self.bot.session.close()

    async def test_non_ascii_secret_header_is_rejected_without_raising(self) -> None:
        """F-045：非 ASCII 的 secret 头是 401，不是未鉴权的 TypeError/500。

        ``secrets.compare_digest`` 对非 ASCII 的 str 会抛 TypeError；aiohttp 按
        latin-1 解码请求头，任何 ≥0x80 的字节都会变成非 ASCII 字符。
        """

        self.assertFalse(
            self.queue.verify_secret(
                SimpleNamespace(
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": "tökén-\x80",
                    }
                )
            )
        )
        self.assertFalse(
            self.queue.verify_secret(
                SimpleNamespace(
                    headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 31}
                )
            )
        )
        self.assertFalse(
            self.queue.verify_secret(SimpleNamespace(headers={}))
        )
        self.assertTrue(
            self.queue.verify_secret(
                SimpleNamespace(
                    headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 32}
                )
            )
        )

    async def test_same_poison_update_does_not_mark_transport_fatal(self) -> None:
        for _ in range(10):
            self.queue._record_business_failure(701, "poison")
        self.assertIsNone(self.queue.fatal_issue())
        self.assertEqual(
            self.queue.health_snapshot()["consecutive_distinct_failures"],
            1,
        )

    async def test_three_distinct_failures_degrade_health_until_success(self) -> None:
        for update_id in (701, 702, 703):
            self.queue._record_business_failure(update_id, "systemic")
        self.assertIsNotNone(self.queue.fatal_issue())
        self.assertFalse(self.queue.health_snapshot()["ok"])

        self.queue._record_durable_success()
        self.assertIsNone(self.queue.fatal_issue())
        self.assertTrue(self.queue.health_snapshot()["ok"])

    async def test_durable_completion_maps_are_pruned_on_finish(self) -> None:
        loop = asyncio.get_running_loop()
        self.queue._completed_update_ids.update(
            {index: loop.time() for index in range(10)}
        )
        self.queue._seen_update_ids.update(
            {index: loop.time() for index in range(10)}
        )
        result = loop.create_future()
        queued = _QueuedWebhookUpdate(
            update={"update_id": 999},
            update_id=999,
            enqueued_at=loop.time(),
            result=result,
        )
        self.queue._inflight_updates[999] = result

        with patch("bot.services.verify_web._WEBHOOK_DEDUP_MAX_UPDATES", 3):
            await self.queue._finish_queued_update(queued, succeeded=True)

        self.assertLessEqual(len(self.queue._completed_update_ids), 3)
        self.assertLessEqual(len(self.queue._seen_update_ids), 3)


class HealthRedactionTests(unittest.TestCase):
    """F-009：未鉴权的 /healthz 不能把内部异常文本回给匿名客户端。"""

    def test_free_form_diagnostics_become_presence_flags(self) -> None:
        payload = _redact_health_diagnostics(
            {
                "ok": False,
                "issue": "only 1/4 ordinary webhook workers are alive",
                "last_recovery_error": (
                    "OperationalError: no such table: webhook_inbox_updates"
                ),
                "last_business_error": None,
                "recovery_failures": 3,
                "oldest_active_age_seconds": 1.5,
                "accepted_privileged_updates": 0,
                "resources": {
                    "memory": {
                        "write_worker_error": (
                            "OperationalError: /data/bot.db is locked"
                        ),
                        "write_failures": 2,
                    },
                    "sqlite_writer": {"ok": True, "locked": False},
                },
            }
        )

        self.assertIs(payload["issue"], True)
        self.assertIs(payload["last_recovery_error"], True)
        self.assertIsNone(payload["last_business_error"])
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["recovery_failures"], 3)
        self.assertEqual(payload["oldest_active_age_seconds"], 1.5)
        self.assertEqual(payload["accepted_privileged_updates"], 0)
        self.assertIs(payload["resources"]["memory"]["write_worker_error"], True)
        self.assertEqual(payload["resources"]["memory"]["write_failures"], 2)
        self.assertIs(payload["resources"]["sqlite_writer"]["ok"], True)

        dumped = json.dumps(payload)
        self.assertNotIn("OperationalError", dumped)
        self.assertNotIn("bot.db", dumped)
        self.assertNotIn("webhook_inbox_updates", dumped)

    def test_non_string_values_are_untouched(self) -> None:
        payload = _redact_health_diagnostics(
            {
                "queue_size": 4,
                "fatal": False,
                "ratio": 0.25,
                "waiter_ids": [1, 2],
                "nothing": None,
            }
        )

        self.assertEqual(
            payload,
            {
                "queue_size": 4,
                "fatal": False,
                "ratio": 0.25,
                "waiter_ids": [1, 2],
                "nothing": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
