"""修复批 P1-2 / B-27：签到提醒 claim→send 之间的空占位不再永久堵死时段。

复现的原缺陷（``AUDIT-B`` B-27）：

占位协议本身是对的（先占位再发、发失败撤占位），问题在**时序**：

1. ``claim_reminder_slot`` + ``commit`` 之后是 ``bot.send_message``（网络调用）。
   进程在这两步之间被 SIGKILL / OOM / 容器驱逐（正是 cron 撞上重启的典型场景）
   → 库里留下一行 ``message_id=0`` 的占位，而 ``(group_id, slot_key)`` 唯一索引让
   ``claim_reminder_slot`` **永远**返回 False：当天该时段永远不再发，无补发路径。
2. send 成功 + ``mark_reminder_sent`` 之后，自动删除排队失败只 ``log.error``，
   那条写着「本条提醒 10 分钟后自动删除」的消息会**永久**留在群里。

修法：占位表加 ``delivered_at``；下一轮运行先 ``reap_stale_reminder_slots`` 清理
「超宽限期且从未送达」的空占位（腾出来的时段本轮就能补发）；自动删除排队失败时
直接往同一张持久表 ``telegram_delete_jobs`` 补一行。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import select, update

from bot.db.engine import init_db
from bot.db.models import CheckinReminderPost, TelegramDeleteJob
from bot.services.checkin_reminder import (
    REMINDER_AUTO_DELETE_SECONDS,
    STALE_REMINDER_GRACE_SECONDS,
    backfill_durable_auto_delete,
    claim_reminder_slot,
    mark_reminder_sent,
    reap_stale_reminder_slots,
    release_reminder_slot,
)
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100777
KEY = "2026-10-03:9"


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _slots(self) -> list[tuple[int, str, int]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        CheckinReminderPost.group_id,
                        CheckinReminderPost.slot_key,
                        CheckinReminderPost.message_id,
                    )
                )
            ).all()
        return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]

    async def _age_slot(self, seconds: int) -> None:
        """把占位行的 created_at 往前推（模拟「上一轮进程崩在那儿」）。"""

        async with self.session_factory() as session:
            await session.execute(
                update(CheckinReminderPost)
                .where(CheckinReminderPost.group_id == GROUP_ID)
                .values(created_at=now_shanghai_naive() - timedelta(seconds=seconds))
            )
            await session.commit()

    async def _claim(self) -> bool:
        async with self.session_factory() as session:
            claimed = await claim_reminder_slot(session, group_id=GROUP_ID, key=KEY)
            await session.commit()
        return claimed


class StaleSlotTests(_DbCase):
    async def test_crash_between_claim_and_send_leaves_a_reapable_slot(self) -> None:
        """崩溃场景：占位在、message_id=0、delivered_at=NULL。"""

        self.assertTrue(await self._claim())
        self.assertEqual(await self._slots(), [(GROUP_ID, KEY, 0)])

        # 崩溃后，**同一天重跑**会被唯一索引挡住（这正是原缺陷）
        self.assertFalse(await self._claim(), "占位在 → 当天不再发")

        # 宽限期之内不动它（"刚 claim 正在发"不能被误删）
        await self._age_slot(30)
        async with self.session_factory() as session:
            self.assertEqual(await reap_stale_reminder_slots(session), [])
        self.assertEqual(await self._slots(), [(GROUP_ID, KEY, 0)])

        # 超过宽限期 → 清理并可补发
        await self._age_slot(STALE_REMINDER_GRACE_SECONDS + 60)
        async with self.session_factory() as session:
            released = await reap_stale_reminder_slots(session)
        self.assertEqual(released, [(GROUP_ID, KEY)])
        self.assertEqual(await self._slots(), [])
        self.assertTrue(await self._claim(), "空占位清掉后必须能补发")

    async def test_delivered_slots_are_never_reaped(self) -> None:
        """已送达的占位即使很久以前建的也不能动。"""

        self.assertTrue(await self._claim())
        async with self.session_factory() as session:
            await mark_reminder_sent(
                session, group_id=GROUP_ID, key=KEY, message_id=555
            )
            await session.commit()
        await self._age_slot(30 * 24 * 3600)
        async with self.session_factory() as session:
            self.assertEqual(await reap_stale_reminder_slots(session), [])
        self.assertEqual(await self._slots(), [(GROUP_ID, KEY, 555)])

    async def test_zero_message_id_without_delivery_stamp_is_reaped(self) -> None:
        """只有 delivered_at 才是判据；message_id=0 + delivered 标记的行不动。"""

        self.assertTrue(await self._claim())
        async with self.session_factory() as session:
            await session.execute(
                update(CheckinReminderPost)
                .where(CheckinReminderPost.group_id == GROUP_ID)
                .values(delivered_at=now_shanghai_naive())
            )
            await session.commit()
        await self._age_slot(STALE_REMINDER_GRACE_SECONDS + 60)
        async with self.session_factory() as session:
            self.assertEqual(await reap_stale_reminder_slots(session), [])
        self.assertEqual(await self._slots(), [(GROUP_ID, KEY, 0)])

    async def test_released_slot_keeps_working_in_the_same_run(self) -> None:
        """清占位 + 重新 claim 必须落在同一轮里完成（补发路径真实可用）。"""

        self.assertTrue(await self._claim())
        await self._age_slot(STALE_REMINDER_GRACE_SECONDS + 60)
        async with self.session_factory() as session:
            await reap_stale_reminder_slots(session)
            reclaimed = await claim_reminder_slot(
                session, group_id=GROUP_ID, key=KEY
            )
            await session.commit()
        self.assertTrue(reclaimed)
        self.assertEqual(await self._slots(), [(GROUP_ID, KEY, 0)])

    async def test_release_still_works_for_the_ordinary_failure_path(self) -> None:
        """回归：可捕获异常时的撤占位路径不受影响。"""

        self.assertTrue(await self._claim())
        async with self.session_factory() as session:
            await release_reminder_slot(session, group_id=GROUP_ID, key=KEY)
            await session.commit()
        self.assertEqual(await self._slots(), [])
        self.assertTrue(await self._claim())


class AutoDeleteBackfillTests(_DbCase):
    async def test_backfill_persists_the_delete_job(self) -> None:
        due = now_shanghai_naive() + timedelta(seconds=REMINDER_AUTO_DELETE_SECONDS)
        self.assertTrue(
            await backfill_durable_auto_delete(
                self.session_factory, chat_id=GROUP_ID, message_id=777, due_at=due
            )
        )
        async with self.session_factory() as session:
            rows = (await session.execute(select(TelegramDeleteJob))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0].chat_id), GROUP_ID)
        self.assertEqual(int(rows[0].message_id), 777)

    async def test_backfill_is_idempotent_and_never_extends_the_deadline(self) -> None:
        later = now_shanghai_naive() + timedelta(hours=2)
        earlier = now_shanghai_naive() + timedelta(seconds=60)
        await backfill_durable_auto_delete(
            self.session_factory, chat_id=GROUP_ID, message_id=777, due_at=later
        )
        await backfill_durable_auto_delete(
            self.session_factory, chat_id=GROUP_ID, message_id=777, due_at=earlier
        )
        async with self.session_factory() as session:
            rows = (await session.execute(select(TelegramDeleteJob))).scalars().all()
        self.assertEqual(len(rows), 1, "同一消息只保留一条删除任务")
        stored = rows[0].due_at.replace(tzinfo=None)
        self.assertLessEqual(stored, earlier.replace(tzinfo=None) + timedelta(seconds=1))

    async def test_backfill_rejects_a_message_without_an_id(self) -> None:
        self.assertFalse(
            await backfill_durable_auto_delete(
                self.session_factory, chat_id=GROUP_ID, message_id=0, due_at=None
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
