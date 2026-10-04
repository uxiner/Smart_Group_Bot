"""A-07 残留：生产写入函数不得依赖 SQLite SAVEPOINT 语义。

pysqlite / aiosqlite 的 legacy 事务控制只在**第一条 DML 之前**发出 ``BEGIN``，
而 ``SAVEPOINT`` 不是 DML，所以驱动此刻处于"没有事务"状态；``RELEASE`` 一个
没有 ``BEGIN`` 包裹的最外层 savepoint，按 SQLite 语义**就是 COMMIT**
（仓库自证见 ``bot/services/checkin.py`` F-053 注释，机制与实测见 GAP-D1 §3.1）。

后果：``async with session.begin_nested():`` 里写的行一旦正常 ``RELEASE``，
调用方随后的 ``session.rollback()`` 撤不回来，磁盘上已经落库。

P0 批（D2-02）已按"原生 ``ON CONFLICT DO NOTHING`` + ``rowcount`` 判定"处理
``award_points`` / ``upsert_entitlement``。本文件把同一条不变量固化成回归：
**任何生产写入函数在调用方 rollback 之后不得留痕**。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import MemberCheckin, VoteBanSession, VoteBanVote
from bot.services.checkin import record_checkin
from bot.services.vote_ban import (
    open_vote_session,
    record_vote,
    resolve_vote_ban_config,
)
from bot.utils.timezone import now_shanghai_naive


def _vote_ban_config(threshold: int = 3) -> object:
    settings = SimpleNamespace(
        vote_ban_threshold=threshold,
        vote_ban_duration_seconds=600,
        vote_ban_pin_message=False,
        vote_ban_trigger_limit=3,
        vote_ban_trigger_window_seconds=3600,
    )
    return resolve_vote_ban_config(settings, {})


async def _count(session_factory: object, model: object) -> int:
    async with session_factory() as session:  # type: ignore[attr-defined]
        return int(
            await session.scalar(select(func.count()).select_from(model)) or 0
        )


class ProductionWritesLeaveNoTraceAfterRollbackTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_record_checkin_is_undoable(self) -> None:
        async with self.session_factory() as session:
            outcome = await record_checkin(
                session,
                group_id=-100,
                user_id=4242,
                display_name="成员",
            )
            self.assertFalse(outcome.already)
            self.assertGreater(outcome.points_awarded, 0)
            await session.rollback()

        self.assertEqual(
            await _count(self.session_factory, MemberCheckin),
            0,
            "record_checkin 依赖 SAVEPOINT：调用方 rollback 之后签到行仍在库里",
        )

    async def test_open_vote_session_is_undoable(self) -> None:
        async with self.session_factory() as session:
            record = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="骚扰消息",
                config=_vote_ban_config(),
            )
            self.assertIsNotNone(record)
            self.assertGreater(int(record.id), 0)
            await session.rollback()

        self.assertEqual(
            await _count(self.session_factory, VoteBanSession),
            0,
            "open_vote_session 依赖 SAVEPOINT：调用方 rollback 之后投票会话仍在库里",
        )
        self.assertEqual(
            await _count(self.session_factory, VoteBanVote),
            0,
            "发起人首票同样必须随外层事务一起撤回",
        )

    async def test_record_vote_is_undoable(self) -> None:
        async with self.session_factory() as session:
            record = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="骚扰消息",
                config=_vote_ban_config(),
            )
            session_id = int(record.id)
            await session.commit()

        async with self.session_factory() as session:
            self.assertTrue(await record_vote(session, session_id, 11))
            await session.rollback()

        self.assertEqual(
            await _count(self.session_factory, VoteBanVote),
            1,
            "record_vote 依赖 SAVEPOINT：调用方 rollback 之后这一票撤不回来"
            "（取消投票 / 撤销投票在事务层没有退路）",
        )


class SavepointFreeWritesKeepTheirSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """同一批改动的正向语义：改了实现不能顺手改掉行为。"""

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

    async def test_same_day_checkin_is_still_rejected_as_already(self) -> None:
        async with self.session_factory() as session:
            first = await record_checkin(session, group_id=-100, user_id=4242)
            await session.commit()
        async with self.session_factory() as session:
            second = await record_checkin(session, group_id=-100, user_id=4242)
            await session.commit()

        self.assertFalse(first.already)
        self.assertTrue(second.already)
        self.assertEqual(second.points_awarded, 0)
        self.assertEqual(await _count(self.session_factory, MemberCheckin), 1)

    async def test_checkin_streak_and_totals_survive(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        async with self.session_factory() as session:
            session.add(
                MemberCheckin(
                    group_id=-100,
                    user_id=4242,
                    checkin_date=yesterday.isoformat(),
                    points=1,
                    display_name="成员",
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            outcome = await record_checkin(
                session, group_id=-100, user_id=4242, display_name="成员"
            )
            await session.commit()

        self.assertEqual(outcome.streak, 2)
        self.assertEqual(outcome.total_days, 2)
        self.assertEqual(outcome.points_awarded, 2)

    async def test_one_active_vote_session_per_target(self) -> None:
        config = _vote_ban_config()
        async with self.session_factory() as session:
            first = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="",
                config=config,
            )
            self.assertIsNotNone(first)
            await session.commit()

        async with self.session_factory() as session:
            duplicate = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=99,
                starter_display="另一个人",
                reason="",
                config=config,
            )
            self.assertIsNone(duplicate)
            await session.commit()

        self.assertEqual(await _count(self.session_factory, VoteBanSession), 1)
        self.assertEqual(await _count(self.session_factory, VoteBanVote), 1)

    async def test_duplicate_ballot_is_rejected_and_closed_poll_rejects(self) -> None:
        config = _vote_ban_config()
        async with self.session_factory() as session:
            record = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=555,
                target_display="骚扰者",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="",
                config=config,
            )
            session_id = int(record.id)
            await session.commit()

        async with self.session_factory() as session:
            self.assertFalse(await record_vote(session, session_id, 10))
            self.assertTrue(await record_vote(session, session_id, 11))
            await session.commit()

        self.assertEqual(await _count(self.session_factory, VoteBanVote), 2)

        async with self.session_factory() as session:
            session.add(
                VoteBanSession(
                    group_id=-100,
                    target_user_id=666,
                    status="cancelled",
                    threshold=3,
                    deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                )
            )
            await session.commit()
        async with self.session_factory() as session:
            reopened = await open_vote_session(
                session,
                group_id=-100,
                target_user_id=666,
                target_display="其他人",
                target_username="",
                starter_user_id=10,
                starter_display="发起人",
                reason="",
                config=config,
            )
            self.assertIsNotNone(reopened)
            # 上一条已关闭的会话让开了部分唯一索引，新会话可以正常收票。
            self.assertTrue(await record_vote(session, int(reopened.id), 11))
            await session.commit()
