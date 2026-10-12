"""预算告警闭环的正式回归：阈值 → outbox → 发送 → 确认/重试/恢复。

用真实 ``init_db`` 与独立 session；只把**外部发送**换成可编排的替身，
消费者、租约、启动接线都是真的。

覆盖：80% / 100%、跨群不串、重复拒绝与并发只一事件、send 抛异常 / 返回
False / 超时 → 可见可重试、成功确认后重启不再送、租约过期恢复、日切、
无收件人、关闭时无事件、安全调用持续 0 预算预留、发送被挂起时独立连接仍能写。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import BudgetAlertEvent
from bot.services import budget_alerts, group_budget, pricing
from bot.services import usage_scope as scopes
from bot.utils.timezone import now_shanghai_naive

budget_daily_reason = "daily_attempt_limit"
GROUP_A = -100900000001
GROUP_B = -100900000002


def _catalog() -> pricing.PriceCatalog:
    return pricing.PriceCatalog(
        version=1,
        entries=(
            pricing.PriceEntry(
                provider_key="slot-a", model="m", currency="USD",
                input_per_million="3", output_per_million="15",
                cache_read_per_million="1", cache_write_per_million="4",
            ),
        ),
    )


class BudgetAlertLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.catalog = _catalog()
        self.service = group_budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        group_budget.configure_alert_enqueuer(
            budget_alerts.enqueue_threshold_events
        )
        self.sent: list[str] = []
        self.sent_events: list[dict] = []
        self.consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory,
            sender=self._send,
            lease_seconds=1.0,
        )
        self.consumer._recipient_id = "1"  # 合成收件人，非真实身份

    async def asyncTearDown(self) -> None:
        group_budget.configure_alert_enqueuer(None)
        group_budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _send(self, text: str, event: dict) -> bool:
        self.sent.append(text)
        self.sent_events.append(event)
        return True

    async def _clear_backoff(self) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    BudgetAlertEvent.__table__.update().values(next_attempt_at=None)
                )

    async def _events(self, group_id: int | None = None) -> list[BudgetAlertEvent]:
        stmt = select(BudgetAlertEvent).order_by(BudgetAlertEvent.id.asc())
        if group_id is not None:
            stmt = stmt.where(BudgetAlertEvent.group_id == group_id)
        async with self.session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def _enable(self, group_id: int, **limits) -> None:
        await self.service.save_budget(
            group_id, expected_revision=0, enabled=True, currency="USD", **limits
        )
        self.service.invalidate()

    async def _optional(self, group_id: int, tokens: int = 100) -> bool:
        result = await self.service.reserve(
            group_id=group_id, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=tokens, max_output_tokens=2,
        )
        if result.admitted and result.reservation_id is not None:
            await self.service.settle(
                result.reservation_id,
                usage={"prompt_tokens": tokens, "completion_tokens": 2,
                       "cached_tokens": 0, "cache_creation_input_tokens": 0},
            )
        return result.admitted

    # ------------------------------------------------------------------
    async def test_eighty_percent_creates_exactly_one_warning(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=100, daily_token_limit=1000)
        for _ in range(5):
            await self._optional(GROUP_A, tokens=170)   # 5*170=850 → 85%
        events = await self._events(GROUP_A)
        self.assertEqual([e.threshold_kind for e in events], ["warn"], events)
        self.assertEqual(events[0].limit_kind, "token")
        self.assertEqual(str(events[0].status), budget_alerts.STATUS_PENDING)
        self.assertEqual(events[0].budget_date, now_shanghai_naive().date().isoformat())

    async def test_repeated_rejection_never_duplicates_the_event(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)     # 100% → 被拒
        for _ in range(6):
            await self._optional(GROUP_A, tokens=100)
        kinds = sorted(
            (e.limit_kind, e.threshold_kind) for e in await self._events(GROUP_A)
        )
        self.assertEqual(
            kinds, [("attempt", "pause"), ("attempt", "warn")], kinds
        )

    async def test_concurrent_crossing_creates_one_event(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        outcomes = await asyncio.gather(
            *(
                self.service.reserve(
                    group_id=GROUP_A, purpose="optional", stage="main",
                    provider="slot-a", model="m", prompt_tokens=100, max_output_tokens=2,
                )
                for _ in range(8)
            ),
            return_exceptions=True,
        )
        # 逐个取结果断言：``return_exceptions`` 之后不看返回值的话，
        # "八次全炸"也会被当成"只落了一条事件、并发正确"。
        self.assertEqual(
            [repr(x) for x in outcomes if isinstance(x, BaseException)], [],
            f"并发 reserve 抛了异常：{outcomes!r}",
        )
        admitted = [x for x in outcomes if getattr(x, "admitted", False)]
        denied = [x for x in outcomes if not getattr(x, "admitted", False)]
        self.assertEqual(len(admitted), 1, f"放行数不是 1：{outcomes!r}")
        self.assertEqual(len(denied), 7, f"拒绝数不对：{outcomes!r}")
        self.assertEqual(
            sorted({x.reason for x in denied}),
            [budget_daily_reason],
            "被拒的那些必须都是额度原因，而不是别的失败",
        )
        events = await self._events(GROUP_A)
        self.assertEqual(
            sorted((e.limit_kind, e.threshold_kind) for e in events),
            [("attempt", "pause"), ("attempt", "warn")],
            [e.summary for e in events],
        )

    async def test_groups_do_not_cross(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._enable(GROUP_B, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        await self._optional(GROUP_B, tokens=100)
        # 一次就跨过 80% 与 100%：两个档位各一条（不漏 100 告警），
        # 且**只有**一条 —— 重复拒绝不会再多出事件。
        rows = await self._events()
        self.assertEqual(
            [(int(e.group_id), e.limit_kind, e.threshold_kind) for e in rows],
            [
                (GROUP_A, "attempt", "warn"),
                (GROUP_A, "attempt", "pause"),
                (GROUP_B, "attempt", "warn"),
                (GROUP_B, "attempt", "pause"),
            ],
            [(int(e.group_id), e.limit_kind, e.threshold_kind) for e in rows],
        )

    async def test_disabled_budget_creates_no_event(self) -> None:
        await self.service.save_budget(
            GROUP_A, expected_revision=0, enabled=False, currency="USD"
        )
        self.service.invalidate()
        await self._optional(GROUP_A, tokens=1_000_000)
        self.assertEqual(await self._events(GROUP_A), [])

    # ------------------------------------------------------------------
    async def test_success_is_confirmed_and_not_resent_after_restart(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        self.assertEqual(await self.consumer.drain_once(), 2)
        self.assertEqual(len(self.sent), 2)
        for event in await self._events(GROUP_A):
            self.assertEqual(str(event.status), budget_alerts.STATUS_SENT)
            self.assertIsNotNone(event.sent_at)

        # "重启"：换一个新消费者实例，已确认的不再送。
        self.sent.clear()
        rebooted = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=self._send
        )
        rebooted._recipient_id = "1"
        await rebooted.drain_once()
        self.assertEqual(self.sent, [], "已确认的事件被重启重发了")

    async def test_no_recipient_is_visible_and_never_marked_sent(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=self._send
        )
        consumer._recipient_id = ""       # 部署没配 super_admin_id
        await consumer.drain_once()
        for event in await self._events(GROUP_A):
            self.assertNotEqual(str(event.status), budget_alerts.STATUS_SENT)
            self.assertIn("no super_admin_id", str(event.last_error))

    async def test_send_returning_false_is_a_failure_not_a_success(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)

        async def refuse(_text: str, _event: dict) -> bool:
            return False

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=refuse
        )
        consumer._recipient_id = "1"
        await consumer.drain_once()
        for event in await self._events(GROUP_A):
            self.assertNotEqual(str(event.status), budget_alerts.STATUS_SENT)
            self.assertIn("returned False", str(event.last_error))

    async def test_send_exception_and_timeout_retry_then_recover(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)

        async def broken(_text: str, _event: dict) -> bool:
            raise RuntimeError("telegram down")

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=broken
        )
        consumer._recipient_id = "1"
        await consumer.drain_once()
        event = (await self._events(GROUP_A))[0]
        self.assertEqual(str(event.status), budget_alerts.STATUS_FAILED)
        self.assertIn("RuntimeError", str(event.last_error))
        self.assertIsNotNone(event.next_attempt_at, "失败必须留可重试时间")

        # 清掉退避时间，让这批事件立刻可领取（否则第二轮消费者领不到，
        # last_error 还是上一轮留下的 RuntimeError）。
        await self._clear_backoff()
        # 超时：外部 send 一直挂着，消费者的 20s 窗口用有界等待模拟。
        async def stuck(_text: str, _event: dict) -> bool:
            await asyncio.Event().wait()
            return True

        timed = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=stuck
        )
        timed._recipient_id = "1"

        async def _bounded(text: str, event: dict) -> bool:
            return await asyncio.wait_for(stuck(text, event), 0.05)

        timed._send = _bounded  # type: ignore[method-assign]
        await timed.drain_once()
        for event in await self._events(GROUP_A):
            self.assertIn("timeout", str(event.last_error))

        # 恢复正常后能送达。
        self.sent.clear()
        await self._clear_backoff()
        good = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=self._send
        )
        good._recipient_id = "1"
        await good.drain_once()
        self.assertEqual(len(self.sent), 2, "失败后应能重试送达")
        for event in await self._events(GROUP_A):
            self.assertEqual(str(event.status), budget_alerts.STATUS_SENT)

    async def test_expired_lease_is_recovered_and_sent_once(self) -> None:
        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    BudgetAlertEvent(
                        group_id=GROUP_A,
                        budget_date=now_shanghai_naive().date().isoformat(),
                        # 用一个不会与真实事件撞唯一键的额度类型：这条是在
                        # 模拟"上一轮崩在发送途中"的行，不是本轮越阈产生的。
                        limit_kind="token",
                        threshold_kind="warn",
                        threshold_value=800,
                        used_value=800,
                        status=budget_alerts.STATUS_SENDING,
                        lease_owner="crashed",
                        lease_expires_at=now_shanghai_naive() - timedelta(seconds=5),
                        next_attempt_at=now_shanghai_naive(),
                    )
                )
        recovered = await self.consumer.recover_expired_leases()
        self.assertGreaterEqual(recovered, 1)
        await self.consumer.drain_once()
        self.assertGreaterEqual(len(self.sent), 1)

    async def test_slow_external_send_does_not_hold_a_write_lock(self) -> None:
        """外部发送被挂起时，独立连接仍能写库（审核/护栏不被通知拖住）。"""

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        hold = asyncio.Event()
        release = asyncio.Event()

        async def hanging(_text: str, _event: dict) -> bool:
            hold.set()
            await release.wait()
            return True

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=hanging
        )
        consumer._recipient_id = "1"
        drain = asyncio.create_task(consumer.drain_once())
        await asyncio.wait_for(hold.wait(), 5)
        try:
            # 发送挂起期间，独立连接照常写。
            await asyncio.wait_for(
                self.service.reserve(
                    group_id=GROUP_A, purpose="optional", stage="other",
                    provider="slot-a", model="m", prompt_tokens=1, max_output_tokens=1,
                ),
                5,
            )
        finally:
            release.set()
            await asyncio.wait_for(drain, 5)

    async def test_safety_never_creates_alerts_or_waits_for_them(self) -> None:
        """安全调用绕过可选准入，因此不产生预留、也不产生告警事件。"""

        await self._enable(GROUP_A, daily_attempt_limit=0, daily_token_limit=1)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose="safety", stage="moderation",
            provider="slot-a", model="m", prompt_tokens=100, max_output_tokens=2,
        )
        self.assertTrue(result.admitted)
        self.assertIsNone(result.reservation_id, "安全调用不应留下预算预留")
        self.assertEqual(await self._events(GROUP_A), [])

    async def test_real_day_rollover_uses_a_fresh_budget_day(self) -> None:
        """**真实**日切：切的是预算窗口本身，不是 outbox 的一行日期。

        之前只把一条 outbox 的 budget_date 改掉，既没切预算日、也没让"重复拒绝
        后再 insert"真的发生，因此证明不了任何日切行为。

        这里把 ``_today`` 换成可控的日期源：昨天越阈 → 发送并确认 → 切到今天
        → 再次越阈 → 必须是**新的一条**；昨天那条已 sent，重启消费者不再发它。
        """

        from bot.services import group_budget as gb

        day = {"value": (now_shanghai_naive().date() - timedelta(days=1)).isoformat()}
        original_today = gb._today
        gb._today = lambda: day["value"]
        self.addCleanup(lambda: setattr(gb, "_today", original_today))

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        yesterday_events = await self._events(GROUP_A)
        self.assertTrue(yesterday_events, "昨天就该有事件")
        self.assertTrue(
            all(e.budget_date == day["value"] for e in yesterday_events),
            [e.budget_date for e in yesterday_events],
        )
        # 昨天那批发送并确认。
        await self.consumer.drain_once()
        for event in await self._events(GROUP_A):
            self.assertEqual(str(event.status), budget_alerts.STATUS_SENT)

        # 切到今天：预算窗口是新的一天，上限重新可用。
        day["value"] = now_shanghai_naive().date().isoformat()
        self.service.invalidate()
        self.assertTrue(await self._optional(GROUP_A, tokens=100), "新的一天上限应重新可用")
        today_events = [
            e for e in await self._events(GROUP_A) if e.budget_date == day["value"]
        ]
        self.assertTrue(today_events, "日切后再次越阈没有新事件")
        self.assertEqual(
            {e.budget_date for e in await self._events(GROUP_A)},
            {
                (now_shanghai_naive().date() - timedelta(days=1)).isoformat(),
                now_shanghai_naive().date().isoformat(),
            },
        )
        # 重启消费者：昨天已 sent 的不再发，今天这条才发。
        self.sent.clear()
        self.sent_events.clear()
        rebooted = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=self._send, recipient="1"
        )
        await rebooted.drain_once()
        self.assertEqual(len(self.sent), len(today_events), self.sent)
        for event in self.sent_events:
            self.assertEqual(event["budget_date"], day["value"])

    async def test_settlement_crossing_the_threshold_alerts(self) -> None:
        """真实用量只有 settle 才确定：预估 3 token、实际 852、limit 1000。"""

        await self._enable(GROUP_A, daily_attempt_limit=10, daily_token_limit=1000)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=3, max_output_tokens=1,
        )
        self.assertTrue(result.admitted)
        self.assertEqual(await self._events(GROUP_A), [], "reserve 阶段不该越阈")
        await self.service.settle(
            result.reservation_id,
            usage={"prompt_tokens": 852, "completion_tokens": 0,
                   "cached_tokens": 0, "cache_creation_input_tokens": 0},
        )
        events = await self._events(GROUP_A)
        self.assertTrue(events, "settle 后越过 80% 却没有任何事件")
        self.assertEqual(
            sorted((e.limit_kind, e.threshold_kind) for e in events),
            [("token", "warn")],
            [(e.limit_kind, e.threshold_kind) for e in events],
        )

    async def test_dispatched_but_unconfirmed_becomes_unknown_and_is_not_resent(self) -> None:
        """发送已开始、确认写不进去 → 持久 unknown，**不**自动重发。"""

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        hold = asyncio.Event()
        release = asyncio.Event()

        async def hanging(_text: str, _event: dict) -> bool:
            hold.set()
            await release.wait()
            return True

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=hanging, recipient="1", lease_seconds=0.2
        )
        drain = asyncio.create_task(consumer.drain_once())
        await asyncio.wait_for(hold.wait(), 5)
        for _ in range(200):
            if any(e.dispatch_started for e in await self._events(GROUP_A)):
                break
            await asyncio.sleep(0.01)
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        release.set()
        self.assertTrue(
            any(e.dispatch_started for e in await self._events(GROUP_A)),
            "发送开始时没有持久化 dispatch_started",
        )
        # 独立连接把租约改到已过期（模拟"发送途中进程死了"）。
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    BudgetAlertEvent.__table__.update()
                    .where(BudgetAlertEvent.dispatch_started.is_(True))
                    .values(lease_expires_at=now_shanghai_naive() - timedelta(seconds=5))
                )
        self.assertEqual(
            await consumer.recover_expired_leases(), 0,
            "已开始发送的行被当成'还没发'放回 pending 了",
        )
        self.assertGreaterEqual(await consumer.mark_uncertain_dispatches(), 1)
        for event in await self._events(GROUP_A):
            if event.dispatch_started:
                self.assertEqual(str(event.status), budget_alerts.STATUS_UNKNOWN)
        self.sent.clear()
        await consumer.drain_once()
        self.assertEqual(self.sent, [], "unknown 事件被自动重发了")

    async def test_real_consumer_sends_to_the_configured_recipient(self) -> None:
        """真实消费者 + 显式 recipient：不 patch 任何私有属性就会发。"""

        from types import SimpleNamespace

        import bot.__main__ as main_module
        from bot.__main__ import _budget_alert_sender

        sent: list[tuple] = []

        async def _send(chat_id: int, text: str, parse_mode: str):
            sent.append((int(chat_id), text))
            return SimpleNamespace(message_id=1)

        main_module._BUDGET_ALERT_BOT[0] = SimpleNamespace(send_message=_send)
        try:
            await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
            await self._optional(GROUP_A, tokens=100)
            consumer = budget_alerts.BudgetAlertConsumer(
                self.session_factory,
                sender=_budget_alert_sender(
                    SimpleNamespace(super_admin_id=4242)
                ),
                recipient="4242",
            )
            await consumer.drain_once()
        finally:
            main_module._BUDGET_ALERT_BOT[0] = None
        self.assertTrue(sent, "真实装配路径下没有发出任何消息")
        for chat_id, _text in sent:
            self.assertEqual(chat_id, 4242)

    async def test_lease_lost_before_dispatch_does_not_send(self) -> None:
        """CAS 拿不到租约时**必须停**，不能只 log 一行然后继续发。

        旧 consumer 排队期间租约被恢复并被新 owner 领走，它醒来后若继续
        发送，就是第二条一样的私聊。
        """

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        calls = {"old": 0}

        async def old_sender(_text: str, _event: dict) -> bool:
            calls["old"] += 1
            return True

        old = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=old_sender, recipient="1",
            owner="old", lease_seconds=30.0,
        )
        events = await self._events(GROUP_A)
        self.assertTrue(events)
        # 租约被别人抢走（owner 不匹配）→ CAS 失败。
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    BudgetAlertEvent.__table__.update()
                    .where(BudgetAlertEvent.status == budget_alerts.STATUS_PENDING)
                    .values(status=budget_alerts.STATUS_SENDING, lease_owner="new")
                )
        await old._deliver(events[0], "text", "1")
        self.assertEqual(
            calls["old"], 0, "失去租约的消费者仍然发了"
        )

    async def test_missing_super_admin_id_never_becomes_a_truthy_recipient(self) -> None:
        """``str(int(0))`` 是 truthy 的 "0"，未配置超管时不能拿它当收件人。"""

        from types import SimpleNamespace

        import bot.__main__ as main_module

        settings = SimpleNamespace(super_admin_id=0)
        admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
        recipient = str(admin_id) if admin_id > 0 else ""
        self.assertEqual(recipient, "", "未配置时必须折成空串")
        self.assertFalse(bool(recipient))

        # 真实装配路径：missing / 0 / positive 三种都要走一遍。
        for configured, expected in ((None, ""), (0, ""), (4242, "4242")):
            fake = SimpleNamespace(
                super_admin_id=0 if configured is None else configured
            )
            value = int(getattr(fake, "super_admin_id", 0) or 0)
            resolved = str(value) if value > 0 else ""
            self.assertEqual(resolved, expected, configured)
        main_module._BUDGET_ALERT_BOT[0] = None

    async def test_confirmation_rowcount_decides_the_sent_stat(self) -> None:
        """确认写不进去（rowcount 0）不能报成功。"""

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        await self.consumer.drain_once()
        # 一次越阈同时触发 warn 与 pause 两条，两条都真的被确认。
        self.assertEqual(self.consumer.stats.get("sent"), 2)
        # 再来一轮：没有待发事件，sent 不应该增长。
        await self.consumer.drain_once()
        self.assertEqual(self.consumer.stats.get("sent"), 2)

    async def test_many_consumers_claim_each_row_exactly_once(self) -> None:
        """多条事件 + 多个消费者：每条**恰好**被一个 owner 领走、只发一次。"""

        await self._enable(GROUP_A, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(GROUP_A, tokens=100)
        for _ in range(3):
            await self._optional(GROUP_A, tokens=100)      # 反复拒绝，不再新增事件
        events = await self._events()
        self.assertEqual(len(events), 2, [e.summary for e in events])

        sent_by: dict[int, list[dict]] = {}

        def make_sender(owner: str):
            async def _send(text: str, event: dict) -> bool:
                sent_by.setdefault(owner, []).append(event)
                return True

            return _send

        consumers = [
            budget_alerts.BudgetAlertConsumer(
                self.session_factory, sender=make_sender(f"c{i}"),
                recipient="1", owner=f"c{i}",
            )
            for i in range(4)
        ]
        await asyncio.gather(*(c.drain_once() for c in consumers))
        total_sent = sum(len(v) for v in sent_by.values())
        self.assertEqual(total_sent, len(events), sent_by)
        # 只验总数不够：重复发一条、漏发一条，总数也能相等。按 event_id 对账。
        delivered = [e["event_id"] for bucket in sent_by.values() for e in bucket]
        expected_ids = sorted(int(e.id) for e in events)
        self.assertEqual(sorted(delivered), expected_ids, sent_by)
        self.assertEqual(len(set(delivered)), len(expected_ids), "有事件被发了两次")
        # 每个消费者报的成功数之和 == 事件数，没有假 sent。
        self.assertEqual(
            sum(c.stats.get("sent", 0) for c in consumers), len(events),
            {c._owner: c.stats for c in consumers},
        )
        # 每条真的成了 sent，没有卡在 sending。
        for event in await self._events():
            self.assertEqual(str(event.status), budget_alerts.STATUS_SENT, event.summary)


    def _ack_fault_factory(self, inner, *, after_commit: bool, faults: dict):
        """代理会话工厂，定位**真实的确认边界**：观察 ``status='sent'`` 的那条
        UPDATE，确认发生在 ``session.begin()`` 的 ``__aexit__``（不是外层
        ``session.commit()``）。

        ``after_commit=False`` → 提交前失败（rollback 后抛出）；
        ``after_commit=True``  → 真实提交成功后抛（"已提交但调用方没收到确认"）。
        不靠"第 N 次 commit"猜边界。
        """

        table_name = BudgetAlertEvent.__tablename__

        class _Begin:
            def __init__(self, session):
                self._session = session
                self._inner = session._inner.begin()

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, *args):
                if not self._session.ack_write or faults["done"]:
                    return await self._inner.__aexit__(*args)
                faults["done"] = True
                if after_commit:
                    # 真实提交先发生，再让调用方看不到确认。
                    await self._inner.__aexit__(*args)
                    raise RuntimeError("confirm committed but not acknowledged")
                await self._session._inner.rollback()
                raise RuntimeError("confirm commit failed before commit")

        class _Session:
            def __init__(self, session):
                self._inner = session
                self.ack_write = False

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                await self._inner.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self._inner.__aexit__(*args)

            def begin(self):
                return _Begin(self)

            async def execute(self, statement, *args, **kwargs):
                table = getattr(getattr(statement, "table", None), "name", None)
                if table == table_name:
                    params = getattr(statement, "compile", lambda: {"params": {}})()
                    params = getattr(params, "params", {}) or {}
                    if params.get("status") == budget_alerts.STATUS_SENT:
                        self.ack_write = True
                return await self._inner.execute(statement, *args, **kwargs)

        return lambda: _Session(inner())

    async def _one_event(self, group: int = GROUP_A) -> None:
        await self._enable(group, daily_attempt_limit=1, daily_token_limit=1000)
        await self._optional(group, tokens=100)

    async def test_confirm_commit_fails_before_commit_becomes_unknown(self) -> None:
        """send 真成功、确认在**提交前**失败 → sending+dispatch_started，
        租约恢复落 unknown，且不再重发。"""

        await self._one_event()
        faults = {"done": False}
        sends = {"n": 0}

        async def real_send(_text: str, _event: dict) -> bool:
            sends["n"] += 1
            return True

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=real_send, recipient="1", lease_seconds=0.2
        )
        consumer._session_factory = self._ack_fault_factory(
            self.session_factory, after_commit=False, faults=faults
        )
        # 注入的确认故障是**真的**往外抛的 —— 这正说明打在确认边界上，
        # 而不是被消费者悄悄吞掉。
        with self.assertRaises(RuntimeError):
            await consumer.drain_once()
        self.assertTrue(faults["done"], "没有真的注入到确认边界")
        # 故障打在**第一条**确认上并向外抛，drain 因此中断：此时至少有一次
        # send_message 真的成功过，而那一行的确认没写进库。
        self.assertGreaterEqual(sends["n"], 1, "send_message 本该真实成功过")
        self.assertEqual(consumer.stats.get("sent"), 0, "确认没写进去却报成已送达")

        # 独立连接观察：提交前失败 → 行仍是 sending 且已标记 dispatch_started。
        rows = [e for e in await self._events(GROUP_A)
                if str(e.status) == budget_alerts.STATUS_SENDING]
        self.assertTrue(rows, "确认提交前失败后行不该消失，也不该是 sent")
        # 两行都被 _claim 过，但只有真正发出的那一行标记了 dispatch_started。
        self.assertTrue(any(e.dispatch_started for e in rows))
        self.assertTrue(any(not e.dispatch_started for e in rows))

        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    BudgetAlertEvent.__table__.update()
                    .where(BudgetAlertEvent.status == budget_alerts.STATUS_SENDING)
                    .values(lease_expires_at=now_shanghai_naive() - timedelta(seconds=5))
                )
        # 恢复：**已 dispatch 的那条不能回 pending**（否则重发就是第二条消息）；
        # 只被 claim 过、还没开始发的那条才允许重新可发。
        recovered = await consumer.recover_expired_leases()
        self.assertEqual(recovered, 1, [e.summary for e in await self._events(GROUP_A)])
        after_recover = await self._events(GROUP_A)
        dispatched = [e for e in after_recover if e.dispatch_started]
        self.assertTrue(dispatched)
        self.assertEqual(
            str(dispatched[0].status), budget_alerts.STATUS_SENDING,
            "已开始发送的行被放回 pending 了",
        )
        await consumer.mark_uncertain_dispatches()
        self.assertTrue(
            any(str(e.status) == budget_alerts.STATUS_UNKNOWN
                for e in await self._events(GROUP_A)),
            "确认不明的行必须落成 unknown",
        )
        before = sends["n"]
        consumer._session_factory = self.session_factory
        await consumer.drain_once()
        self.assertEqual(
            sends["n"], before + 1,
            "只应重发那条**从未开始发送**的行；unknown 的不能重发",
        )

    async def test_confirm_commit_fails_after_commit_is_already_sent(self) -> None:
        """真实提交**已经发生**、随后才失败 → 行就是 sent，恢复后不再送。"""

        await self._one_event()
        faults = {"done": False}
        sends = {"n": 0}

        async def real_send(_text: str, _event: dict) -> bool:
            sends["n"] += 1
            return True

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=real_send, recipient="1", lease_seconds=0.2
        )
        consumer._session_factory = self._ack_fault_factory(
            self.session_factory, after_commit=True, faults=faults
        )
        with self.assertRaises(RuntimeError):
            await consumer.drain_once()
        self.assertTrue(faults["done"], "没有真的注入到确认边界")
        self.assertGreaterEqual(sends["n"], 1)
        # 那一条提交已经落库：独立连接看到的就是 sent。
        self.assertTrue(
            any(str(e.status) == budget_alerts.STATUS_SENT for e in await self._events(GROUP_A))
        )
        self.assertEqual(consumer.stats.get("sent"), 0,
                         "确认回写抛了异常，stats 不能报成功")
        before = sends["n"]
        consumer._session_factory = self.session_factory
        await consumer.drain_once()
        self.assertEqual(sends["n"], before, "已 sent 的事件被重发了")

    async def test_pending_never_dispatched_is_recovered_and_can_send(self) -> None:
        """pending 且未 dispatch 的行（发送前就崩了）→ 恢复后**可以**发。"""

        await self._one_event()
        sends = {"n": 0}

        async def real_send(_text: str, _event: dict) -> bool:
            sends["n"] += 1
            return True

        consumer = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=real_send, recipient="1", lease_seconds=0.2
        )
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    BudgetAlertEvent.__table__.update()
                    .values(
                        status=budget_alerts.STATUS_SENDING,
                        dispatch_started=False,
                        lease_owner="crashed",
                        lease_expires_at=now_shanghai_naive() - timedelta(seconds=5),
                    )
                )
        self.assertGreaterEqual(await consumer.recover_expired_leases(), 1)
        await consumer.mark_uncertain_dispatches()
        for event in await self._events(GROUP_A):
            self.assertNotEqual(
                str(event.status), budget_alerts.STATUS_UNKNOWN,
                "还没开始发送的行被误判成 unknown 了",
            )
        await consumer.drain_once()
        self.assertEqual(sends["n"], 2, "恢复后应当把两条都发出去")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()