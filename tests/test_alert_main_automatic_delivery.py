"""真实 main 装配下的**后台自动投递**（正式回归，父探针的正式化）。

真实 ``Settings(_env_file=None)`` + 清空环境、真实 init_db（main 的 init_db 返回
观察包装、``session_factory`` 仍是真会话）、真实 ``_initialize_runtime_services``、
真实消费者（由 main 装配启动）、真实共享 ``price_catalog_provider`` +
``save_budget``/``reserve``/``settle`` 生成两条阈值事件。

只把最外层的 ``send_message`` 换成可编排的替身：bot 引用**只**经由 main 的
create_bot 路径设置；不 patch 全局 bot、不 patch recipient/_send、不手动
drain/recover、不手动改全局 hook。正常关停仍走 main 自己的 finally。

三臂：显式 4242（发 2 条、持久 sent）、显式 0（发 0 条、持久 failed 且错误可
观察）、默认 Settings（无 super_admin 字段，等价于 0）。每臂独立临时库、环境
完整还原；等待全部是**有界轮询真实持久状态**，不用固定 sleep 冒充路径。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import bot.__main__ as main_module
from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import BudgetAlertEvent
from bot.services import budget_alerts, group_budget, verify_web

_GROUP = -1001234567890


async def _idle(*args, **kwargs):
    await asyncio.Event().wait()


class _IdleService:
    """与本任务无关的 service：构造成功，monitor/loop 类方法挂住，其余空转。"""

    def __init__(self, *args, **kwargs):
        self.webhook_route_error = None
        self.webhook_runtime_failure = asyncio.Event()

    def __getattr__(self, name):
        if name in ("monitor", "run_forever"):
            return _idle

        async def _noop(*a, **k):
            return None

        return _noop


class _Dispatcher(dict):
    def include_router(self, *args, **kwargs):
        return None


class AutomaticDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sb-alert-auto-")
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "main.db")
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.db_path}"
        )
        self._saved_env = dict(os.environ)
        os.environ.clear()
        self.addCleanup(self._restore_env)
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        self.addCleanup(main_module.budget_alert_consumer_holder.clear)
        self.addCleanup(group_budget.configure_alert_enqueuer, None)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)

    async def asyncTearDown(self) -> None:
        for consumer in list(main_module.budget_alert_consumer_holder):
            await consumer.stop()
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        group_budget.configure_alert_enqueuer(None)
        group_budget.reset_shared_service()
        await self.engine.dispose()

    # ------------------------------------------------------------------
    def _settings(self, **overrides) -> Settings:
        """默认 Settings **不传** super_admin 字段（走模型缺省 0）。"""

        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{self.db_path}",
            **overrides,
        )

    async def _rows(self) -> list:
        async with self.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(BudgetAlertEvent).order_by(BudgetAlertEvent.id)
                    )
                ).all()
            )

    async def _create_real_events(self) -> None:
        """真实 save_budget + reserve + settle：一条 warn、一条 pause。"""

        from bot.web.settings_api import price_catalog_provider

        service = group_budget.shared_service(
            self.session_factory, price_catalog_provider
        )
        await service.save_budget(
            _GROUP, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=1,
        )
        service.invalidate()
        result = await service.reserve(
            group_id=_GROUP, purpose="optional", stage="main",
            provider="fixture", model="m", prompt_tokens=1, max_output_tokens=2,
        )
        self.assertTrue(result.admitted, result.reason)
        await service.settle(
            result.reservation_id,
            usage={
                "prompt_tokens": 1, "completion_tokens": 2,
                "cached_tokens": 0, "cache_creation_input_tokens": 0,
            },
        )

    async def _run_main(self, settings: Settings) -> dict:
        """跑一次真实 main 到正常关停。

        外部 polling 协程里造真实预算事件，然后**有界轮询真实持久状态**：
        有收件人等"两条都 sent"，无收件人等"两条都 failed"（不存在的事件
        不会被当成已送达去等）。
        """

        expect_admin = int(getattr(settings, "super_admin_id", 0) or 0) > 0
        sent: list[dict] = []

        async def close():
            return None

        async def dispose():
            await self.engine.dispose()

        async def delivery(**kwargs):
            await self._create_real_events()
            async with asyncio.timeout(20):
                while True:
                    rows = await self._rows()
                    expected = (
                        budget_alerts.STATUS_SENT
                        if expect_admin
                        else budget_alerts.STATUS_FAILED
                    )
                    if len(rows) == 2 and all(str(r.status) == expected for r in rows):
                        return
                    await asyncio.sleep(0.02)

        async def send_message(*args, **kwargs):
            sent.append(
                {
                    "chat_id": args[0] if args else None,
                    "text": args[1] if len(args) > 1 else "",
                }
            )
            return SimpleNamespace(message_id=len(sent))

        bot = SimpleNamespace(
            send_message=send_message,
            me=AsyncMock(
                return_value=SimpleNamespace(
                    id=1, username="fixture", full_name="F", first_name="F"
                )
            ),
            session=SimpleNamespace(close=close),
        )
        replacements = {
            "load_bootstrap_settings": lambda: settings,
            "validate_bootstrap_settings": lambda _s: None,
            "init_db": AsyncMock(
                return_value=(
                    SimpleNamespace(dispose=dispose), self.session_factory
                )
            ),
            "configure_logging": lambda *a, **kw: None,
            "_prefetch_model_context_metadata": AsyncMock(),
            "dp": _Dispatcher(),
            "create_bot": lambda _s: bot,
            "_build_group_summary_scheduler": lambda **kw: None,
            "_register_update_middlewares": lambda *a: None,
            "_publish_bot_command_menu": AsyncMock(),
            "restore_vote_ban_tasks": AsyncMock(),
            "log_process_identity": lambda: None,
            "log_enforcement_switch_state": lambda _s: None,
            "run_update_delivery": delivery,
            "_arm_forced_exit_watchdog": lambda *a: SimpleNamespace(
                set=lambda: None
            ),
        }
        idle_services = (
            "TelegramCleanupScheduler", "ProactiveTopicService",
            "JoinVerificationSweeper", "PatrolService",
            "ScheduledMessageService", "GroupPermissionService",
            "ShopExpiryService", "RaidGuardService",
        )
        with ExitStack() as stack:
            for name, value in replacements.items():
                stack.enter_context(patch.object(main_module, name, value))
            for name in idle_services:
                stack.enter_context(patch.object(main_module, name, _IdleService))
            stack.enter_context(
                patch.object(verify_web, "VerifyWebServer", _IdleService)
            )
            stack.enter_context(
                patch.object(
                    main_module.MemoryService, "run_archive_maintenance", _idle
                )
            )
            stack.enter_context(
                patch.object(
                    main_module.model_limits.PeriodicModelMetadataRefresh,
                    "start",
                    lambda self: asyncio.create_task(_idle()),
                )
            )
            async with asyncio.timeout(40):
                await main_module.main()
        # 正常关停之后：本次 owned 消费者已停、holder 回基线、bot 引用回基线。
        self.assertEqual(
            len(main_module.budget_alert_consumer_holder), 0,
            "正常关停后 holder 没回基线",
        )
        self.assertIsNone(main_module._BUDGET_ALERT_BOT[0], "bot 引用没回基线")
        return {"sent": sent}

    # ------------------------------------------------------------------
    async def test_configured_super_admin_receives_both_messages(self) -> None:
        result = await self._run_main(self._settings(super_admin_id=4242))
        self.assertEqual(len(result["sent"]), 2, result["sent"])
        for item in result["sent"]:
            self.assertEqual(int(item["chat_id"]), 4242)
        rows = await self._rows()
        self.assertEqual(len(rows), 2, [r.summary for r in rows])
        for row in rows:
            self.assertEqual(str(row.status), budget_alerts.STATUS_SENT, row.summary)
            self.assertIsNotNone(row.sent_at)

    async def test_explicit_zero_super_admin_sends_nothing_but_stays_visible(self) -> None:
        result = await self._run_main(self._settings(super_admin_id=0))
        self.assertEqual(result["sent"], [], "没有收件人却发出去了")
        rows = await self._rows()
        self.assertEqual(len(rows), 2, [r.summary for r in rows])
        for row in rows:
            self.assertEqual(str(row.status), budget_alerts.STATUS_FAILED)
            self.assertIn("no super_admin_id", str(row.last_error))

    async def test_default_settings_have_no_super_admin_field(self) -> None:
        settings = self._settings()
        self.assertEqual(
            int(getattr(settings, "super_admin_id", 0) or 0), 0,
            "默认 Settings 应当没有可用的超管收件人",
        )
        result = await self._run_main(settings)
        self.assertEqual(result["sent"], [])
        rows = await self._rows()
        self.assertEqual(len(rows), 2, [r.summary for r in rows])
        for row in rows:
            self.assertNotEqual(str(row.status), budget_alerts.STATUS_SENT)
            self.assertIn("no super_admin_id", str(row.last_error))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()