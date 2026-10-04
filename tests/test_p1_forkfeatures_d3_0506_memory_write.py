"""修复批 P1-2 / D3-05 + D3-06：长期记忆提炼的游标越界与非原子写。

复现的原缺陷（``GAP-D3`` §2.1）：

* **D3-05 游标越界**：``_extract_with_session`` 用**未裁剪批次**的 ``max_row_id``
  推进游标，而 ``_trim_input_to_token_limit`` 为压进 12000 token 会从**最旧一端**
  整条丢弃消息。被丢掉的行既没送模型、游标又过去了 → **永久漏提炼**，且无任何日志。
* **D3-06(a) 非原子写**：命中同一 fingerprint 时 ``confirm_count`` 用了 SQL 原子
  表达式，``confidence`` 却是先 SELECT 出来在 Python 里加好再写回（read-modify-write），
  并发命中会丢一次 bump。
* **D3-06(b) 越界回滚**：异常分支 ``await session.rollback()`` 回滚**整笔会话**；
  ``remember`` 工具在没有 ``session_factory`` 时会把调用方的**共享** session 传进来，
  一次 ``record_fact`` 失败会连带回滚调用方的其它写入。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import GroupMessageArchive, MemoryExtractCursor, UserFact
from bot.services import long_term_memory as ltm

GROUP_ID = -100777
SENDER_ID = 924
NOW = datetime(2026, 10, 3, 21, 40, 0)


def _settings(**overrides) -> SimpleNamespace:
    bot = SimpleNamespace(
        memory_facts_enabled=True,
        memory_extract_enabled=True,
        memory_extract_interval_minutes=30,
        memory_extract_min_messages=20,
        memory_extract_daily_cap=48,
        memory_extract_batch_max=200,
        memory_event_ttl_days=30,
        memory_recall_limit=8,
        memory_deleted_retention_days=30,
    )
    for key, value in overrides.items():
        setattr(bot, key, value)
    return SimpleNamespace(bot=bot)


class _StubLLM:
    """提炼替身：返回固定 JSON，并把每批收到的正文长度记下来。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.batches: list[str] = []

    async def generate(self, system: str, user_text: str) -> str:
        self.batches.append(user_text)
        return self._payload


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

    async def _archive(self, text: str, index: int, *, role: str = "user") -> int:
        is_bot = role != "user"
        async with self.session_factory() as session:
            row = GroupMessageArchive(
                group_id=GROUP_ID,
                message_key=f"{GROUP_ID}:{index}",
                telegram_message_id=index,
                role=role,
                direction="outbound" if is_bot else "inbound",
                sender_kind="bot" if is_bot else "member",
                sender_id=0 if is_bot else SENDER_ID,
                sender_display_name="群友",
                message_type="text",
                content=text,
                raw_text=text,
                sender_is_bot=is_bot,
                sent_at=NOW,
                ingested_at=NOW,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _archive_bot(self, text: str, index: int) -> int:
        return await self._archive(text, index, role="assistant")

    async def _cursor_value(self) -> int:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryExtractCursor.last_row_id).where(
                        MemoryExtractCursor.scope == ltm.SCOPE_GROUP,
                        MemoryExtractCursor.scope_id == GROUP_ID,
                    )
                )
            ).first()
            return int(row[0]) if row is not None else 0


class CursorAdvanceTests(_DbCase):
    """D3-05：游标只能推进到「真正送进模型」的最后一行。"""

    async def test_cursor_advances_when_nothing_was_trimmed(self) -> None:
        ids = [await self._archive(f"短消息 {index}", index) for index in range(3)]
        llm = _StubLLM("[]")
        async with self.session_factory() as session:
            await ltm._extract_with_session(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                llm=llm,
                settings=_settings(),
                now=NOW,
            )
        self.assertEqual(await self._cursor_value(), max(ids))
        self.assertIn("短消息 2", llm.batches[0], "没被裁剪时整批都应送进模型")

    async def test_cursor_stops_at_the_last_distillable_row(self) -> None:
        """游标不许越过「本批实际可提炼」的最后一行。

        批次尾部常见**不可提炼**的行（机器人自己的回复、非文本、``/memory off`` 的人）。
        原实现用未裁剪批次的 ``max_row_id`` 前移，游标因此越过了这些行——它们本来
        该被跳过（下一轮再判一次），而不是和「已提炼」混为一谈。
        """

        first = await self._archive("第一条成员消息", 1)
        second = await self._archive("第二条成员消息", 2)
        bot_row = await self._archive_bot("机器人自己的回复", 3)
        self.assertGreater(bot_row, second)
        async with self.session_factory() as session:
            await ltm._extract_with_session(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                llm=_StubLLM("[]"),
                settings=_settings(),
                now=NOW,
            )
        self.assertEqual(
            await self._cursor_value(),
            second,
            "游标必须停在最后一条真正送进模型的行上",
        )
        self.assertGreater(first, 0)

    async def test_trimming_is_logged_instead_of_silent(self) -> None:
        """超预算丢弃必须有 INFO 日志（原来是静默的，运维侧毫无信号）。"""

        big = "显卡行情" * 800  # 每行约 3.2k token：单行装得下、整批 32k 装不下
        ids = [await self._archive(f"{big} #{index}", index) for index in range(10)]
        with self.assertLogs("bot.services.long_term_memory", level="INFO") as captured:
            async with self.session_factory() as session:
                await ltm._extract_with_session(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    llm=_StubLLM("[]"),
                    settings=_settings(),
                    now=NOW,
                )
        self.assertTrue(
            any("超 token 预算" in line for line in captured.output),
            f"必须留下 INFO 日志，实际={captured.output}",
        )
        self.assertEqual(await self._cursor_value(), max(ids))

    async def test_all_ineligible_batch_still_advances_the_cursor(self) -> None:
        """回归：全是机器人消息时游标照常前移（否则每轮都重复统计）。"""

        async with self.session_factory() as session:
            row = GroupMessageArchive(
                group_id=GROUP_ID,
                message_key=f"{GROUP_ID}:99",
                role="assistant",
                direction="outbound",
                sender_kind="bot",
                sender_id=0,
                sender_is_bot=True,
                message_type="text",
                content="机器人自己的回复",
                raw_text="机器人自己的回复",
                telegram_message_id=99,
                sent_at=NOW,
                ingested_at=NOW,
            )
            session.add(row)
            await session.commit()
            bot_row_id = int(row.id)
            await ltm._extract_with_session(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                llm=_StubLLM("[]"),
                settings=_settings(),
                now=NOW,
            )
        self.assertEqual(await self._cursor_value(), bot_row_id)


class ConfirmAtomicityTests(_DbCase):
    """D3-06(a)：``confidence`` 的 bump 必须是 SQL 原子表达式。"""

    async def _record(self, confidence: int = 40) -> int:
        async with self.session_factory() as session:
            fact_id = await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="他喜欢喝咖啡",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=confidence,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="我喜欢喝咖啡",
                now=NOW,
            )
        return fact_id

    async def _row(self) -> UserFact:
        async with self.session_factory() as session:
            return (
                await session.execute(select(UserFact).where(UserFact.fact_text == "他喜欢喝咖啡"))
            ).scalars().one()

    async def test_repeat_confirm_bumps_both_counters(self) -> None:
        fact_id = await self._record(40)
        for round_index, expected_confidence in enumerate((45, 50, 55), start=1):
            async with self.session_factory() as session:
                again = await ltm.record_fact(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=SENDER_ID,
                    fact_text="他喜欢喝咖啡",
                    category=ltm.CATEGORY_PREFERENCE,
                    confidence=40,
                    source_kind=ltm.SOURCE_PASSIVE,
                    evidence_excerpt="我喜欢喝咖啡",
                    now=NOW,
                )
            self.assertEqual(again, fact_id, "幂等：不新增行")
            row = await self._row()
            # 首写 confirm_count=1，之后每命中一次 +1；confidence 每命中一次 +5（封顶 100）
            self.assertEqual(row.confirm_count, round_index + 1)
            self.assertEqual(row.confidence, expected_confidence)

    async def test_confidence_never_exceeds_the_cap(self) -> None:
        await self._record(98)
        async with self.session_factory() as session:
            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="他喜欢喝咖啡",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=40,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="我喜欢喝咖啡",
                now=NOW,
            )
        row = await self._row()
        self.assertEqual(row.confidence, 100, "func.min(100, …) 在 SQLite 侧必须真的封顶")

    async def test_bump_is_computed_in_sql_not_in_python(self) -> None:
        """结构断言：``confidence`` 必须是 SQL 侧表达式，不能是 Python 算好的字面量。

        read-modify-write 在并发下会丢 bump：两个事务都读到同一个 ``old_confidence``，
        各自加 5 写回同一个值。这里直接抓真实发出的 SQL。
        """

        from sqlalchemy import event

        await self._record(40)
        statements: list[str] = []

        async with self.session_factory() as session:

            @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
            def _capture(conn, cursor, statement, parameters, context, executemany):
                if "UPDATE user_facts" in statement:
                    statements.append(statement)

            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="他喜欢喝咖啡",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=40,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="我喜欢喝咖啡",
                now=NOW,
            )
        self.assertEqual(len(statements), 1, "确认分支只发一条 UPDATE")
        sql = statements[0]
        self.assertIn("confirm_count", sql)
        self.assertIn("min(", sql.lower(), "confidence 的 bump 必须在 SQL 侧封顶")
        self.assertIn("confidence", sql.lower())


class RollbackScopeTests(_DbCase):
    """D3-06(b)：失败时只回滚自己 flush 的部分，不牵连调用方的写入。"""

    async def test_shared_session_keeps_the_callers_earlier_writes(self) -> None:
        async with self.session_factory() as session:
            # 调用方先在**同一 session** 上写好别的东西（已 flush，未 commit）。
            session.add(
                GroupMessageArchive(
                    group_id=GROUP_ID,
                    message_key=f"{GROUP_ID}:1",
                    role="user",
                    direction="inbound",
                    sender_kind="member",
                    sender_id=SENDER_ID,
                    sender_is_bot=False,
                    message_type="text",
                    content="调用方自己写的行",
                    raw_text="调用方自己写的行",
                    telegram_message_id=1,
                    sent_at=NOW,
                    ingested_at=NOW,
                )
            )
            await session.flush()
            self.assertTrue(session.in_transaction(), "入口处调用方已经开了事务")

            # 让 record_fact 在写入途中炸掉：add(UserFact) 直接抛异常。
            original_add = session.add

            def _patched_add(obj):
                if isinstance(obj, UserFact):
                    raise RuntimeError("boom")
                return original_add(obj)

            session.add = _patched_add  # type: ignore[method-assign]
            written = await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="他喜欢喝咖啡",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=40,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="我喜欢喝咖啡",
                now=NOW,
            )
            session.add = original_add  # type: ignore[method-assign]
            self.assertEqual(written, 0, "失败必须被吞掉并返回 0")
            await session.commit()

        rows = (
            await self._session_group_rows()
        )
        self.assertEqual(len(rows), 1, "调用方自己写的行必须还在（不能被一起回滚）")
        self.assertEqual(rows[0].content, "调用方自己写的行")

    async def _session_group_rows(self):
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(GroupMessageArchive).where(
                            GroupMessageArchive.group_id == GROUP_ID
                        )
                    )
                ).scalars()
            )

    async def test_own_session_is_fully_rolled_back_on_failure(self) -> None:
        """回归：事务是本函数自己的时，仍然整体回滚（不留半截写入）。"""

        async with self.session_factory() as session:
            self.assertFalse(session.in_transaction(), "入口处没有事务")

            original_add = session.add

            def _patched_add(obj):
                if isinstance(obj, UserFact):
                    raise RuntimeError("boom")
                return original_add(obj)

            session.add = _patched_add  # type: ignore[method-assign]
            written = await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="他喜欢喝咖啡",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=40,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="我喜欢喝咖啡",
                now=NOW,
            )
            session.add = original_add  # type: ignore[method-assign]
            self.assertEqual(written, 0)
            # 整体回滚之后，session 已经不在事务里。
            self.assertFalse(session.in_transaction())
        self.assertEqual(await self._session_group_rows(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
