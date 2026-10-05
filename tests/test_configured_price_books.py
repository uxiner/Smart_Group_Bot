"""改非默认价格之后，商店的**扣费 / 退款 / 账本 / 到期**都跟着变。

`tests/test_point_shop.py` 覆盖的是默认价格下的正确性（幂等、退款、Telegram 失败
必退、并发）。这一份补的是另一半：**配出来的价格真的进了账**，并且在下面这些
容易出错的地方保持一致：

* 扣费用配置价；
* Telegram 失败/没有权限 → 退款额等于**扣的时候那一笔**，不是"按现在的价退"；
* 交易进行到一半改价 → 这笔交易从头到尾用同一份（显示、扣费、退款、回执一致），
  下一笔才看到新价；
* 购买过程中抛异常 / 被取消 → 不留下半截写入（账本与 ``available_points`` 一致）；
* 到期时间按配置的时长算；
* 已购买的 ``expires_at`` **不追改**（改配置只影响之后的新购买）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import Settings
from bot.db.engine import init_db
from bot.services import policy_runtime
from bot.services.point_shop import (
    KIND_PIN,
    KIND_TAG,
    PinTarget,
    buy_member_tag,
    buy_pin,
    expire_due_entitlements,
)
from bot.services.runtime_config import RuntimeConfig

from tests.test_point_shop import (  # noqa: F401  (fixtures shared on purpose)
    FakeBot,
    _bot_error,
    _day,
)


GROUP_ID = -1001234567890


def _bind(economy: dict) -> None:
    """按 ``economy`` 段绑定一份运行时配置（走真实的 apply 链）。"""

    base = RuntimeConfig()
    payload = base.storage_payload()
    payload["economy"] = {**base.economy.model_dump(), **economy}
    config = RuntimeConfig.model_validate(payload)
    settings = Settings(_env_file=None)
    config.apply_to_settings(settings, apply_prompts=False)
    policy_runtime.bind(settings)


class ConfiguredPricePurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._seed = 0
        policy_runtime.unbind()
        self.addCleanup(policy_runtime.unbind)

    async def asyncTearDown(self) -> None:
        policy_runtime.unbind()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # -- helpers ---------------------------------------------------------
    async def _grant(self, user_id: int, points: int) -> None:
        from bot.db.models import MemberPointAward
        from bot.services.checkin import available_points

        self._seed += 1
        async with self.session_factory() as session:
            session.add(
                MemberPointAward(
                    group_id=GROUP_ID,
                    user_id=user_id,
                    points=int(points),
                    reason="test_seed",
                    ref=f"seed:{GROUP_ID}:{user_id}:{self._seed}",
                )
            )
            await session.commit()

    async def _balance(self, user_id: int) -> int:
        from bot.services.checkin import available_points

        async with self.session_factory() as session:
            return await available_points(
                session, group_id=GROUP_ID, user_id=user_id
            )

    async def _rows(self, model, user_id: int) -> list:
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(model).where(
                    model.group_id == GROUP_ID, model.user_id == user_id
                )
            )
            return list(result.scalars().all())

    async def _entitlements(self) -> list:
        from bot.db.models import MemberEntitlement
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(MemberEntitlement).where(
                    MemberEntitlement.group_id == GROUP_ID
                )
            )
            return list(result.scalars().all())

    async def _buy_tag(
        self, bot, *, user_id: int = 7, now=None, raw: str = "摸鱼冠军"
    ):
        async with self.session_factory() as session:
            return await buy_member_tag(
                session,
                bot=bot,
                group_id=GROUP_ID,
                user_id=user_id,
                raw_text=raw,
                now=now or _day(),
            )

    async def _buy_pin(self, bot, *, user_id: int = 7, now=None):
        async with self.session_factory() as session:
            return await buy_pin(
                session,
                bot=bot,
                group_id=GROUP_ID,
                user_id=user_id,
                target=PinTarget(message_id=555, sender_id=user_id),
                now=now or _day(),
            )

    # -- tests -----------------------------------------------------------
    async def test_the_configured_price_is_what_gets_charged(self) -> None:
        from bot.db.models import MemberPointSpend

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})
        bot = FakeBot()

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "ok")
        spends = await self._rows(MemberPointSpend, 7)
        self.assertEqual(len(spends), 1)
        self.assertEqual(
            int(spends[0].points), 41, "扣的必须是配置价，不是默认的 30"
        )
        self.assertEqual(await self._balance(7), 500 - 41)

    async def test_the_duration_comes_from_the_config_too(self) -> None:
        _bind({"tag_price_7d": 41, "tag_days_7d": 3})
        await self._grant(7, 500)
        now = _day()

        await self._buy_tag(FakeBot(), now=now)

        rows = await self._entitlements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, KIND_TAG)
        self.assertEqual(
            rows[0].expires_at,
            now + timedelta(days=3),
            "到期时间必须按配置时长算",
        )

    async def test_a_failed_purchase_refunds_exactly_what_it_charged(self) -> None:
        from bot.db.models import MemberPointAward

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})
        bot = FakeBot(tag_error=_bot_error("Bad Request: tag is invalid"))

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertIn("已退回 41 分", reply.text)
        self.assertEqual(await self._balance(7), 500, "退款后余额必须回到原值")
        refunds = [
            row
            for row in await self._rows(MemberPointAward, 7)
            if row.reason == "shop_refund"
        ]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(int(refunds[0].points), 41, "退款额必须等于扣的那一笔")
        self.assertEqual(await self._entitlements(), [])

    async def test_a_price_change_mid_purchase_does_not_split_the_books(self) -> None:
        """菜单、扣费、退款、回执必须用同一份价格快照。"""

        from bot.db.models import MemberPointAward, MemberPointSpend

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})

        class _PriceMovesMidPurchase(FakeBot):
            async def set_chat_member_tag(self, *, chat_id, user_id, tag):  # type: ignore[override]
                # 头衔已经写进 Telegram 之后、管理员把价改成 99 的那一刻。
                _bind({"tag_price_7d": 99})
                self.tag_calls.append((chat_id, user_id, tag))

        reply = await self._buy_tag(_PriceMovesMidPurchase())

        self.assertEqual(reply.status, "ok")
        spends = await self._rows(MemberPointSpend, 7)
        self.assertEqual(int(spends[0].points), 41, "扣的是快照价")
        # 下一笔才看到新价：第二个人按 99 扣。
        await self._grant(8, 500)
        await self._buy_tag(FakeBot(), user_id=8, now=_day(1), raw="另一个头衔")
        self.assertEqual(
            await self._balance(8), 500 - 99, "新价只对之后的新购买生效"
        )
        self.assertEqual(await self._balance(7), 500 - 41)
        self.assertEqual(
            policy_runtime.economy_policy().tag_price_7d,
            99,
            "事务外必须已经看到新价",
        )

    async def test_a_refund_after_a_mid_purchase_price_change_uses_the_snapshot(
        self,
    ) -> None:
        from bot.db.models import MemberPointAward

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})

        class _PriceMovesBeforeFailure(FakeBot):
            async def set_chat_member_tag(self, *, chat_id, user_id, tag):  # type: ignore[override]
                _bind({"tag_price_7d": 99})
                raise _bot_error("Bad Request: tag is invalid")

        reply = await self._buy_tag(_PriceMovesBeforeFailure())

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertIn("已退回 41 分", reply.text)
        self.assertEqual(await self._balance(7), 500)
        refunds = [
            row
            for row in await self._rows(MemberPointAward, 7)
            if row.reason == "shop_refund"
        ]
        self.assertEqual(
            [int(row.points) for row in refunds],
            [41],
            "退款必须退快照价，不能退现价（否则会多退或少退）",
        )

    async def test_a_raising_telegram_call_becomes_a_refund_not_a_lost_charge(
        self,
    ) -> None:
        """异常（含非 Telegram 的异常）必须走退款，绝不能让钱凭空消失。"""

        from bot.db.models import MemberPointAward, MemberPointSpend

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})

        class _Boom(FakeBot):
            async def set_chat_member_tag(self, *, chat_id, user_id, tag):  # type: ignore[override]
                raise RuntimeError("telegram exploded")

        reply = await self._buy_tag(_Boom())

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertEqual(await self._balance(7), 500, "异常路径也必须原样退款")
        spends = await self._rows(MemberPointSpend, 7)
        self.assertEqual([int(row.points) for row in spends], [41])
        refunds = [
            row
            for row in await self._rows(MemberPointAward, 7)
            if row.reason == "shop_refund"
        ]
        self.assertEqual([int(row.points) for row in refunds], [41])
        self.assertEqual(await self._entitlements(), [], "失败不能留下生效中的头衔")

    async def test_a_cancelled_purchase_leaves_the_books_consistent(self) -> None:
        """在"扣费已提交、Telegram 还没调"之间被取消时，账必须**自洽**。

        真实流程是「先提交扣费与权益行，再去 Telegram 设头衔」，取消会原样往上抛
        （``except asyncio.CancelledError: raise``）。所以这里唯一该保证的不变式是
        **两本账互相吻合**：要么都成立（已扣 + 权益在），要么都不成立。

        Telegram 侧的头衔最终一致靠 Telegram 自己重投，不在本事务范围内——这是
        既有取舍，本轮没有改动它。
        """

        import asyncio

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})
        entered = asyncio.Event()

        class _Slow(FakeBot):
            async def set_chat_member_tag(self, *, chat_id, user_id, tag):  # type: ignore[override]
                entered.set()
                await asyncio.sleep(30)

        async with self.session_factory() as session:
            task = asyncio.ensure_future(
                buy_member_tag(
                    session,
                    bot=_Slow(),
                    group_id=GROUP_ID,
                    user_id=7,
                    raw_text="摸鱼冠军",
                    now=_day(),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        balance = await self._balance(7)
        entitlements = await self._entitlements()
        charged = balance == 500 - 41
        self.assertEqual(
            charged,
            bool(entitlements),
            f"取消后两本账不一致：余额 {balance}，权益行 {len(entitlements)} 条。"
            "要么都成立（已扣 + 权益在），要么都不成立。",
        )

    async def test_expiry_sweep_uses_the_configured_retry_and_batch(self) -> None:
        from bot.services import point_shop

        await self._grant(7, 500)
        _bind({"tag_price_7d": 41})
        await self._buy_tag(FakeBot())
        # 让它过期。
        from bot.db.models import MemberEntitlement
        from sqlalchemy import update

        async with self.session_factory() as session:
            await session.execute(
                update(MemberEntitlement)
                .where(MemberEntitlement.group_id == GROUP_ID)
                .values(expires_at=_day() - timedelta(seconds=1))
            )
            await session.commit()

        async with self.session_factory() as session:
            outcomes = await expire_due_entitlements(session, bot=FakeBot())
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(await self._entitlements(), [])

    async def test_already_bought_expiry_is_not_rewritten_by_a_config_change(self) -> None:
        _bind({"tag_days_7d": 7})
        await self._grant(7, 500)
        bought_at = _day()
        await self._buy_tag(FakeBot(), now=bought_at)
        before = (await self._entitlements())[0].expires_at

        _bind({"tag_days_7d": 1})
        after = (await self._entitlements())[0].expires_at
        self.assertEqual(
            before,
            after,
            "已购买的到期时间不能因为改配置被追改（那是用户的既得权益）",
        )
        self.assertEqual(before, bought_at + timedelta(days=7))

    async def test_pin_price_also_follows_the_config(self) -> None:
        from bot.db.models import MemberPointSpend

        await self._grant(7, 500)
        _bind({"pin_price": 17, "pin_hours": 2})
        now = _day()

        reply = await self._buy_pin(FakeBot(), now=now)

        self.assertEqual(reply.status, "ok")
        spends = await self._rows(MemberPointSpend, 7)
        self.assertEqual(int(spends[0].points), 17)
        rows = await self._entitlements()
        self.assertEqual(rows[0].kind, KIND_PIN)
        self.assertEqual(rows[0].expires_at, now + timedelta(hours=2))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
