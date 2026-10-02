"""私聊运维告警（F-027/F-028/F-029 的"告警"出口）的行为测试。

判罚准确第一：失败降级不许静默，但也不许把一次数据库抖动变成告警风暴。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services import ops_alert


def _settings(super_admin_id: int = 501) -> SimpleNamespace:
    return SimpleNamespace(super_admin_id=super_admin_id)


class OpsAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ops_alert.reset_alert_cooldowns()

    def tearDown(self) -> None:
        ops_alert.reset_alert_cooldowns()

    async def test_alert_is_private_messaged_to_the_super_admin(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())

        sent = await ops_alert.alert_super_admin(
            bot,
            _settings(501),
            kind="global_ban_lookup",
            summary="封禁策略读取失败，本次放行",
            fields={"chat": -100, "user": 7},
        )

        self.assertTrue(sent)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 501)
        self.assertIn("封禁策略读取失败", kwargs["text"])
        self.assertIn("-100", kwargs["text"])
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_dynamic_fields_are_html_escaped(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())

        await ops_alert.alert_super_admin(
            bot,
            _settings(),
            kind="global_ban_lookup",
            summary="<b>not markup</b>",
            fields={"detail": "<script>x</script>"},
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    async def test_cooldown_suppresses_a_second_alert_but_still_logs(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        first = await ops_alert.alert_super_admin(
            bot, _settings(), kind="global_ban_lookup", summary="第一次"
        )
        with patch.object(ops_alert.log, "warning") as warning:
            second = await ops_alert.alert_super_admin(
                bot, _settings(), kind="global_ban_lookup", summary="第二次"
            )

        self.assertTrue(first)
        self.assertFalse(second)
        bot.send_message.assert_awaited_once()
        rendered = " ".join(str(call.args) for call in warning.call_args_list)
        self.assertIn("cooldown", rendered)
        self.assertIn("第二次", rendered)

    async def test_different_kinds_alert_independently(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())

        await ops_alert.alert_super_admin(
            bot, _settings(), kind="global_ban_lookup", summary="a"
        )
        second = await ops_alert.alert_super_admin(
            bot, _settings(), kind="verification_gate_lookup", summary="b"
        )

        self.assertTrue(second)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_missing_admin_id_is_reported_not_raised(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())

        with patch.object(ops_alert.log, "warning") as warning:
            sent = await ops_alert.alert_super_admin(
                bot, _settings(0), kind="global_ban_lookup", summary="x"
            )

        self.assertFalse(sent)
        bot.send_message.assert_not_awaited()
        self.assertIn("super_admin_id", " ".join(str(c) for c in warning.call_args_list))

    async def test_missing_send_capability_is_reported_not_raised(self) -> None:
        sent = await ops_alert.alert_super_admin(
            SimpleNamespace(), _settings(), kind="global_ban_lookup", summary="x"
        )

        self.assertFalse(sent)

    async def test_delivery_failure_is_logged_and_does_not_raise(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("telegram down"))
        )

        with patch.object(ops_alert.log, "exception") as logged:
            sent = await ops_alert.alert_super_admin(
                bot, _settings(), kind="global_ban_lookup", summary="x"
            )

        self.assertFalse(sent)
        logged.assert_called_once()

    async def test_slow_delivery_is_bounded_and_reported(self) -> None:
        async def never_returns(**_kwargs):
            import asyncio

            await asyncio.sleep(30)

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=never_returns))

        with patch.object(ops_alert, "ALERT_TIMEOUT_SECONDS", 0.01):
            sent = await ops_alert.alert_super_admin(
                bot, _settings(), kind="global_ban_lookup", summary="x"
            )

        self.assertFalse(sent)

    async def test_suppressed_alert_still_carries_the_detail_in_the_log(self) -> None:
        """告警被冷却压掉时，细节仍必须留在日志里（不许瞒着）。"""

        bot = SimpleNamespace(send_message=AsyncMock())
        await ops_alert.alert_super_admin(
            bot, _settings(), kind="raid_admin_lookup", summary="第一次"
        )
        with patch.object(ops_alert.log, "warning") as warning:
            await ops_alert.alert_super_admin(
                bot,
                _settings(),
                kind="raid_admin_lookup",
                summary="群管理员名单读取失败",
                fields={"chat": -100},
            )

        rendered = " ".join(str(c) for c in warning.call_args_list)
        self.assertIn("群管理员名单读取失败", rendered)
        self.assertIn("-100", rendered)


if __name__ == "__main__":
    unittest.main()
