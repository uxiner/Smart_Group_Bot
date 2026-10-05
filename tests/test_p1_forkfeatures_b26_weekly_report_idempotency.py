"""修复批 P1-2 / B-26：周报群消息与成本摘要都必须幂等。

复现的原缺陷（``AUDIT-B`` B-26）：

``bot/tools/weekly_report.py`` 的发消息循环里**没有任何去重**。积分结算那半边是幂等的
（``settle_weekly_activity`` → ``(用户, ISO 周)`` 唯一键），但群消息没有台账，于是
cron 与上一次运行重叠、手工补跑、运维手动重跑 → **每个授权群收到重复周报**，
而 exit code 仍是 0、数据上无法察觉。成本摘要对 ``super_admin_id`` 是无条件私发、
无去重。

修法：新增 ``weekly_report_posts`` 表（``(target_id, week_key)`` 唯一索引），
复用 ``checkin_reminder_posts`` 的 ``INSERT ... ON CONFLICT DO NOTHING + RETURNING``
占位协议：先占位再发、发失败撤占位。成本摘要用 ``target_id=0`` 走同一套。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import WeeklyReportPost
from bot.tools.weekly_report import (
    COST_DIGEST_TARGET_ID,
    claim_report_slot,
    current_week_key,
    mark_report_sent,
    release_report_slot,
)

GROUP_A = -100111
GROUP_B = -100222
WEEK = "2026-W41"
NEXT_WEEK = "2026-W42"


class WeeklyReportIdempotencyTests(unittest.IsolatedAsyncioTestCase):
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

    async def _rows(self) -> list[tuple[int, str, int]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        WeeklyReportPost.target_id,
                        WeeklyReportPost.week_key,
                        WeeklyReportPost.message_id,
                    )
                )
            ).all()
        return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]

    async def test_same_group_same_week_is_claimed_only_once(self) -> None:
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK)
            )
            await session.commit()
        # 第二次运行（cron 重叠 / 手工重跑）
        async with self.session_factory() as session:
            self.assertFalse(
                await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK),
                "同一群同一周只允许一条周报",
            )
        self.assertEqual(await self._rows(), [(GROUP_A, WEEK, 0)])

    async def test_other_groups_and_other_weeks_are_independent(self) -> None:
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK)
            )
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_B, week_key=WEEK)
            )
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_A, week_key=NEXT_WEEK)
            )
            await session.commit()
        self.assertEqual(len(await self._rows()), 3)

    async def test_release_lets_the_next_run_resend(self) -> None:
        """发失败不能被记成"已发过"——占位必须能撤。"""

        async with self.session_factory() as session:
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK)
            )
            await session.commit()
        async with self.session_factory() as session:
            await release_report_slot(session, target_id=GROUP_A, week_key=WEEK)
            await session.commit()
        self.assertEqual(await self._rows(), [])
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK),
                "撤掉占位后必须能重发",
            )

    async def test_sent_message_id_is_recorded(self) -> None:
        async with self.session_factory() as session:
            await claim_report_slot(session, target_id=GROUP_A, week_key=WEEK)
            await mark_report_sent(
                session, target_id=GROUP_A, week_key=WEEK, message_id=4242
            )
        self.assertEqual(await self._rows(), [(GROUP_A, WEEK, 4242)])

    async def test_cost_digest_uses_its_own_idempotency_slot(self) -> None:
        """成本摘要以前是无条件私发；现在用 target_id=0 走同一套。"""

        self.assertEqual(COST_DIGEST_TARGET_ID, 0)
        async with self.session_factory() as session:
            self.assertTrue(
                await claim_report_slot(
                    session, target_id=COST_DIGEST_TARGET_ID, week_key=WEEK
                )
            )
            await session.commit()
        async with self.session_factory() as session:
            self.assertFalse(
                await claim_report_slot(
                    session, target_id=COST_DIGEST_TARGET_ID, week_key=WEEK
                ),
                "成本摘要同一周只发一次",
            )

    async def test_week_key_is_iso_week_of_the_run(self) -> None:
        # 同一 ISO 周内的任意一天都得到同一个键（周一到周日）
        self.assertEqual(current_week_key(datetime(2026, 10, 5, 9, 0)), "2026-W41")
        self.assertEqual(current_week_key(datetime(2026, 10, 11, 23, 59)), "2026-W41")
        # 跨周就换键
        self.assertEqual(current_week_key(datetime(2026, 10, 12, 0, 1)), "2026-W42")
        # 无参数时用本地墙钟，不抛
        self.assertRegex(current_week_key(), r"^\d{4}-W\d{2}$")


class WeeklyReportSendLoopTests(unittest.IsolatedAsyncioTestCase):
    """端到端：跑两次 ``_send_reports``，第二个群/超管只收到一次。"""

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

    async def test_two_runs_send_each_target_exactly_once(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from bot.tools import weekly_report as wr

        sent_to: list[int] = []

        class _Bot:
            def __init__(self, token: str) -> None:
                self.session = SimpleNamespace(close=AsyncMock())

            async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001
                sent_to.append(int(chat_id))
                return SimpleNamespace(message_id=len(sent_to))

        async def _render_quality(session, *, group_id, days, activity_lines):  # noqa: ANN001
            return f"<b>审核质量 · 近{days}天</b> 群 {group_id}"

        async def _cost_digest(session, *, days):  # noqa: ANN001
            return "成本摘要"

        async def _groups(session):  # noqa: ANN001
            return [GROUP_A, GROUP_B]

        async def _settle(session, *, group_id, award):  # noqa: ANN001
            return SimpleNamespace(lines=[])

        settings = SimpleNamespace(
            bot=SimpleNamespace(token="TOKEN"),
            bot_token="TOKEN",
            super_admin_id=999,
            database_url="",
        )

        async def _fake_init_db(url):  # noqa: ANN001
            return self.engine, self.session_factory

        def _no_activity(_result):  # noqa: ANN001
            return []

        with patch.object(
            wr, "Settings", return_value=settings
        ), patch.object(wr, "init_db", new=_fake_init_db), patch.object(
            wr, "Bot", _Bot
        ), patch.object(
            wr, "authorized_group_ids", new=_groups
        ), patch.object(
            wr, "render_group_quality", new=_render_quality
        ), patch.object(
            wr, "render_cost_digest", new=_cost_digest
        ), patch.object(
            wr, "settle_weekly_activity", new=_settle
        ), patch.object(
            wr, "render_activity_lines", new=_no_activity
        ):
            self.assertEqual(await wr._send_reports(7), 0)
            self.assertEqual(await wr._send_reports(7), 0)

        self.assertEqual(
            sorted(sent_to),
            sorted([GROUP_A, GROUP_B, 999]),
            "跑两次只发一次：每个群 + 超管成本摘要各自只出现一次（第二次全被幂等挡掉）",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
