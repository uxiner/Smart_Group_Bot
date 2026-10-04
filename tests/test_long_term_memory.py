"""第 4 期：长期记忆（``user_facts``）——提炼 / 去重 / 冲突替代 / 相关召回 / 护栏。

覆盖的验收项（序号对应交付说明里的清单）：

1. **幂等**：同一事实再提炼到只 ``confirm_count += 1``，不新增行；
2. **敏感信息**：手机号/证件号/口令/他人隐私 → 入库 0 条；
3. **冲突与替代**：同类别的矛盾事实 → 旧行 ``superseded``、``superseded_by`` 指新行；
4. **过期**：``category='event'`` 到期不注入，并被标 ``deleted``；
6. **opt-out**：``/memory off`` 后不再提炼、已入库的不再注入；
7. **护栏**：``memory_extract_daily_cap`` / ``memory_tool_daily_cap`` 超限只记日志；
8. **注入规模**：条数与字符数不超上限；无关话题不注入；
9. **纯函数**：``normalize_fact_text`` / ``fact_fingerprint`` / ``is_conflicting`` /
   ``format_fact_line``；
10. **接线**：``__main__`` / ``handlers`` 里的名字能解析，并拿真 ``Settings`` 调一次取值。

（第 5 项隐私红线在 ``tests/test_long_term_memory_privacy.py``。）

数据落库用临时 SQLite（与 ``tests/test_search_records.py`` 同一套 ``init_db``），
不需要网络：所有模型调用都是本地替身。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import suppress
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, text

from bot.db.engine import init_db
from bot.db.models import (
    GroupMessageArchive,
    MemoryExtractCursor,
    MemoryOptout,
    PrivateChatMessage,
    UserFact,
)
from bot.services import long_term_memory as ltm
from bot.services.skills.base import SkillContext
from bot.services.skills.remember import RememberSkill

GROUP_ID = -100777
OTHER_GROUP_ID = -100888
SENDER_ID = 924
SENDER_NAME = "群友A"
NOW = datetime(2026, 10, 3, 21, 40, 0)
#: 一个「远到任何留存期都算过期」的时间（维护用例不依赖机器当前时钟）
ANCIENT = datetime(2000, 1, 1, 0, 0, 0)


def _settings(**overrides):
    bot = SimpleNamespace(
        memory_facts_enabled=True,
        memory_extract_enabled=True,
        memory_extract_interval_minutes=30,
        memory_extract_min_messages=20,
        memory_extract_daily_cap=48,
        memory_extract_batch_max=200,
        memory_tool_enabled=True,
        memory_tool_daily_cap=30,
        memory_recall_limit=8,
        memory_event_ttl_days=30,
        memory_deleted_retention_days=30,
        max_context_tokens=278528,
        group_history_reserve_tokens=32768,
    )
    for key, value in overrides.items():
        setattr(bot, key, value)
    return SimpleNamespace(bot=bot)


class _StubLLM:
    """本地模型替身：可以给一段固定文本、一串文本，或按输入现算。"""

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def generate(self, system: str, user_text: str) -> str:
        self.calls.append((str(system), str(user_text)))
        if callable(self._responses):
            return self._responses(user_text)
        if isinstance(self._responses, list):
            return self._responses.pop(0) if self._responses else ""
        return self._responses


class _ExplodingSession:
    """每条 SQL 都炸、连回滚都炸的假 session（「写失败不影响调用方」用）。"""

    async def execute(self, *args, **kwargs):
        raise RuntimeError("database is locked")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        raise RuntimeError("rollback unavailable")


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        ltm.reset_extraction_ledger()

    async def asyncTearDown(self) -> None:
        ltm.reset_extraction_ledger()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # -- 造数与读库的小工具 -------------------------------------------------

    async def _archive(
        self,
        content: str,
        *,
        group_id: int = GROUP_ID,
        sender_id: int = SENDER_ID,
        sender_name: str = SENDER_NAME,
        message_id: int = 1,
        role: str = "user",
        message_type: str = "text",
        sender_is_bot: bool = False,
    ) -> int:
        async with self.session_factory() as session:
            row = GroupMessageArchive(
                group_id=group_id,
                message_key=f"{group_id}:{message_id}",
                telegram_message_id=message_id,
                role=role,
                direction="inbound",
                sender_kind="member",
                sender_id=sender_id,
                sender_display_name=sender_name,
                message_type=message_type,
                content=content,
                raw_text=content,
                sender_is_bot=sender_is_bot,
                sent_at=NOW,
                ingested_at=NOW,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _private_turn(self, content: str, *, user_id: int = SENDER_ID, key: str) -> int:
        async with self.session_factory() as session:
            row = PrivateChatMessage(
                user_id=user_id, role="user", content=content, message_key=key
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _facts(self) -> list[UserFact]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(select(UserFact).order_by(UserFact.id.asc()))
            ).scalars()
            return list(rows)

    async def _cursor(self, scope: str, scope_id: int) -> int:
        async with self.session_factory() as session:
            value = (
                await session.execute(
                    select(MemoryExtractCursor.last_row_id).where(
                        MemoryExtractCursor.scope == scope,
                        MemoryExtractCursor.scope_id == scope_id,
                    )
                )
            ).scalar_one_or_none()
        return int(value or 0)

    async def _record(
        self,
        fact_text: str,
        *,
        category: str = "preference",
        scope: str = ltm.SCOPE_GROUP,
        scope_id: int = GROUP_ID,
        subject_user_id: int = SENDER_ID,
        confidence: int = 60,
        source_kind: str = ltm.SOURCE_PASSIVE,
        evidence: str = "原始出处片段",
        now: datetime | None = None,
    ) -> int:
        async with self.session_factory() as session:
            return await ltm.record_fact(
                session,
                scope=scope,
                scope_id=scope_id,
                subject_user_id=subject_user_id,
                fact_text=fact_text,
                category=category,
                confidence=confidence,
                source_kind=source_kind,
                source_message_id=42,
                evidence_excerpt=evidence,
                now=now or NOW,
            )


# ---------------------------------------------------------------------------
# 9) 纯函数
# ---------------------------------------------------------------------------


class PureFunctionTests(unittest.TestCase):
    def test_normalize_fact_text(self) -> None:
        self.assertEqual(
            ltm.normalize_fact_text("  张三   在日本   读研。 "), "张三 在日本 读研"
        )
        self.assertEqual(ltm.normalize_fact_text("喜欢喝咖啡！！！"), "喜欢喝咖啡")
        self.assertEqual(ltm.normalize_fact_text("多行\n\t事实"), "多行 事实")
        self.assertEqual(ltm.normalize_fact_text(None), "")
        self.assertEqual(len(ltm.normalize_fact_text("啊" * 400)), 200)

    def test_fact_fingerprint_is_stable_and_scoped(self) -> None:
        base = dict(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            subject_user_id=SENDER_ID,
            fact_text="喜欢喝咖啡",
        )
        first = ltm.fact_fingerprint(**base)
        # 空白与结尾标点的差异必须归一到同一个指纹（否则同一句话会存成两行）
        second = ltm.fact_fingerprint(
            **{**base, "fact_text": "  喜欢喝咖啡 。 "}
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 40)
        self.assertNotEqual(
            first, ltm.fact_fingerprint(**{**base, "subject_user_id": 999})
        )
        self.assertNotEqual(
            first, ltm.fact_fingerprint(**{**base, "scope": ltm.SCOPE_PRIVATE})
        )
        self.assertNotEqual(
            first, ltm.fact_fingerprint(**{**base, "fact_text": "喜欢喝茶"})
        )

    def test_is_conflicting(self) -> None:
        old = {"category": "identity", "fact_text": "小明在上海工作"}
        self.assertTrue(
            ltm.is_conflicting(old, {"category": "identity", "fact_text": "小明在北京工作"})
        )
        # 同类别但文本相同 = 重复，不是冲突
        self.assertFalse(ltm.is_conflicting(old, dict(old)))
        # 不同类别永不冲突
        self.assertFalse(
            ltm.is_conflicting(old, {"category": "preference", "fact_text": "小明在上海工作"})
        )
        # relationship / event / skill 一律并存
        self.assertFalse(
            ltm.is_conflicting(
                {"category": "relationship", "fact_text": "小明是他弟弟"},
                {"category": "relationship", "fact_text": "小明不是他弟弟"},
            )
        )
        # 关键词重叠不足（只有 1 个共同词：别聊）就不判冲突
        self.assertFalse(
            ltm.is_conflicting(
                {"category": "taboo", "fact_text": "别聊政治话题"},
                {"category": "taboo", "fact_text": "别聊天气"},
            )
        )
        # ORM 行与 dict 都要能收
        row = SimpleNamespace(category="preference", fact_text="李四喜欢喝茶")
        self.assertTrue(
            ltm.is_conflicting(row, {"category": "preference", "fact_text": "李四讨厌喝茶"})
        )

    def test_format_fact_line_matches_the_agreed_shape(self) -> None:
        record = {
            "scope": ltm.SCOPE_GROUP,
            "fact_text": "张三在日本读研，爱聊显卡",
            "first_seen_at": datetime(2026, 9, 12, 8, 30),
            "confirm_count": 3,
        }
        self.assertEqual(
            ltm.format_fact_line(record),
            "- [长期记忆 · 群内 · 2026-09-12 起 · 已确认 3 次] 张三在日本读研，爱聊显卡",
        )
        private = {**record, "scope": ltm.SCOPE_PRIVATE}
        self.assertIn("长期记忆 · 私聊", ltm.format_fact_line(private))
        labelled = {**record, "source_label": "显卡群"}
        self.assertIn("长期记忆 · 显卡群", ltm.format_fact_line(labelled))

    def test_contains_sensitive_fact(self) -> None:
        self.assertTrue(ltm.contains_sensitive_fact("他的手机号 13812345678"))
        self.assertTrue(ltm.contains_sensitive_fact("身份证 110101199003071234"))
        self.assertTrue(ltm.contains_sensitive_fact("他的密码是 abc12345"))
        self.assertTrue(ltm.contains_sensitive_fact("token: sk-abcdefghijklmn"))
        self.assertTrue(ltm.contains_sensitive_fact("银行卡 6222 0212 3456 7890"))
        self.assertFalse(ltm.contains_sensitive_fact("喜欢喝咖啡，显卡是 5090"))
        self.assertFalse(ltm.contains_sensitive_fact(""))
        self.assertFalse(ltm.contains_sensitive_fact("在上海读研，学计算机"))

    def test_render_facts_block_is_one_message_per_fact(self) -> None:
        records = [
            {
                "scope": ltm.SCOPE_GROUP,
                "fact_text": f"事实 {index}",
                "first_seen_at": datetime(2026, 9, 12),
                "confirm_count": 1,
            }
            for index in range(12)
        ]
        messages = ltm.render_facts_block(records)
        self.assertEqual(len(messages), ltm.MEMORY_RECALL_LIMIT)
        self.assertTrue(all(item["role"] == "system" for item in messages))
        self.assertEqual(ltm.render_facts_block([]), [])
        self.assertEqual(ltm.render_facts_block(None), [])
        # 头部说明不含任何强制措辞（第 4 期硬边界）
        for forbidden in ("必须", "务必", "MUST", "must"):
            self.assertNotIn(forbidden, ltm.LONG_TERM_MEMORY_HEADER_BLOCK)

    def test_parse_fact_items(self) -> None:
        self.assertIsNone(ltm.parse_fact_items("这不是 JSON"))
        self.assertIsNone(ltm.parse_fact_items(""))
        self.assertEqual(ltm.parse_fact_items("[]"), [])
        self.assertEqual(
            ltm.parse_fact_items('```json\n[{"fact": "x"}]\n```'), [{"fact": "x"}]
        )
        self.assertEqual(
            ltm.parse_fact_items('前缀 [{"fact": "x"}] 后缀'), [{"fact": "x"}]
        )

    def test_parse_fact_items_salvages_truncated_json(self) -> None:
        """输出撞 max_tokens 被截断时，抢救出已闭合的完整对象（别整批丢）。"""

        truncated = (
            '[{"subject_user_id": 1, "fact": "喜欢咖啡", "category": "preference", '
            '"confidence": 70, "evidence": "我爱喝咖啡"}, '
            '{"subject_user_id": 2, "fact": "住在上海", "category": "identity", '
            '"confidence": 80, "evidence": "我在上海工作"}, '
            '{"subject_user_id": 3, "fact": "养了一只猫", "category": "relatio'
        )
        salvaged = ltm.parse_fact_items(truncated)
        self.assertIsNotNone(salvaged, "截断也要能抢救，不能整批丢")
        assert salvaged is not None
        self.assertEqual([item["fact"] for item in salvaged], ["喜欢咖啡", "住在上海"])

    def test_parse_fact_items_salvage_ignores_string_braces(self) -> None:
        """证据文本里出现花括号/引号/转义也不能让扫描错位。"""

        raw = (
            '[{"fact": "写代码时会用到 {config}", "evidence": "他说 \\"用 {a} 就行\\""}, '
            '{"fact": "最近在学 Go", "evidence": "我最近在学'
        )
        salvaged = ltm.parse_fact_items(raw)
        self.assertIsNotNone(salvaged)
        assert salvaged is not None
        self.assertEqual(salvaged[0]["fact"], "写代码时会用到 {config}")

    def test_parse_fact_items_returns_none_for_unusable_output(self) -> None:
        """既没有完整数组、也抢不出**像事实的对象** → None（游标不前移的信号）。

        ``抱歉…{}`` / ``{"facts": []}`` 这类「带花括号的解释文本或包了一层的外壳」绝不能
        算解析成功——否则游标前移、这批消息被静默跳过（实测回归过）。
        """

        for bad in (
            "抱歉，我无法完成。",
            "抱歉，我无法输出 JSON。{}",
            '{"note": "这批没有什么可记的"}',
            "",
            "```json\n[{半截",
            "[{半截",
            "没有数组也没有对象",
        ):
            with self.subTest(raw=bad):
                self.assertIsNone(ltm.parse_fact_items(bad))

    def test_parse_fact_items_accepts_an_empty_array(self) -> None:
        """空数组 / 数组里没有对象 = 这批没有可记的事实（与「解析失败」区分开）。"""

        self.assertEqual(ltm.parse_fact_items("[]"), [])
        self.assertEqual(ltm.parse_fact_items("```json\n[]\n```"), [])
        self.assertEqual(ltm.parse_fact_items("[1, 2, 3]"), [])
        # 包了一层外壳的「空批」（`{"facts": []}`）取到内层空数组 → 同样是「没有可记的」
        self.assertEqual(ltm.parse_fact_items('{"facts": []}'), [])

    def test_config_getters_default_and_clamp(self) -> None:
        empty = _settings(
            memory_extract_min_messages=1,
            memory_extract_daily_cap=9999,
            memory_recall_limit=0,
            memory_event_ttl_days=0,
            memory_deleted_retention_days=0,
            memory_extract_batch_max=5,
            memory_tool_daily_cap=9999,
            memory_extract_interval_minutes=1,
        )
        self.assertEqual(ltm.memory_extract_min_messages(empty), 5)
        self.assertEqual(ltm.memory_extract_daily_cap(empty), 500)
        self.assertEqual(ltm.memory_recall_limit(empty), 1)
        self.assertEqual(ltm.memory_event_ttl_days(empty), 1)
        self.assertEqual(ltm.memory_deleted_retention_days(empty), 1)
        self.assertEqual(ltm.memory_extract_batch_max(empty), 20)
        self.assertEqual(ltm.memory_tool_daily_cap(empty), 200)
        self.assertEqual(ltm.memory_extract_interval_minutes(empty), 5)
        defaults = ltm.memory_recall_limit(SimpleNamespace())
        self.assertEqual(defaults, ltm.MEMORY_RECALL_LIMIT)
        self.assertTrue(ltm.memory_facts_enabled(SimpleNamespace()))
        self.assertTrue(ltm.memory_extract_enabled(SimpleNamespace()))
        self.assertTrue(ltm.memory_tool_enabled(SimpleNamespace()))


# ---------------------------------------------------------------------------
# 1/2/3/4) record_fact：幂等、敏感、冲突替代、过期
# ---------------------------------------------------------------------------


class RecordFactTests(_DbTestCase):
    async def test_new_fact_then_same_fact_confirms_instead_of_duplicating(self) -> None:
        first = await self._record("喜欢喝咖啡", confidence=60)
        self.assertTrue(first)
        later = NOW + timedelta(hours=3)
        second = await self._record(
            "  喜欢喝咖啡。 ", confidence=70, now=later
        )
        self.assertEqual(second, first, "同一事实必须命中同一行")
        facts = await self._facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].confirm_count, 2)
        self.assertEqual(facts[0].confidence, 65, "确认一次 +5")
        self.assertEqual(facts[0].last_confirmed_at, later)
        self.assertEqual(facts[0].first_seen_at, NOW)

    async def test_confidence_bump_is_capped_at_100(self) -> None:
        await self._record("喜欢喝咖啡", confidence=98)
        await self._record("喜欢喝咖啡", confidence=98)
        facts = await self._facts()
        self.assertEqual(facts[0].confidence, 100)

    async def test_evidence_is_required(self) -> None:
        async with self.session_factory() as session:
            stored = await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                fact_text="没有出处的事实",
                category="preference",
                confidence=60,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="",
            )
        self.assertEqual(stored, 0)
        self.assertEqual(await self._facts(), [])

    async def test_sensitive_facts_are_rejected(self) -> None:
        for text_value in (
            "他的手机号是 13812345678",
            "身份证号 110101199003071234",
            "他的密码是 hunter2hunter2",
            "银行卡 6222 0212 3456 7890",
        ):
            with self.subTest(fact=text_value):
                self.assertEqual(await self._record(text_value), 0)
        self.assertEqual(await self._facts(), [])

    async def test_deleted_facts_are_never_revived(self) -> None:
        fact_id = await self._record("喜欢喝咖啡")
        async with self.session_factory() as session:
            self.assertTrue(await ltm.soft_delete_fact(session, fact_id, now=NOW))
        self.assertEqual(await self._record("喜欢喝咖啡", now=NOW + timedelta(days=1)), 0)
        facts = await self._facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].status, ltm.STATUS_DELETED)
        self.assertEqual(facts[0].confirm_count, 1)

    async def test_conflicting_fact_supersedes_the_old_one(self) -> None:
        old_id = await self._record("小明在上海工作", category="identity")
        new_id = await self._record(
            "小明在北京工作", category="identity", now=NOW + timedelta(days=1)
        )
        self.assertNotEqual(old_id, new_id)
        facts = {fact.id: fact for fact in await self._facts()}
        self.assertEqual(facts[old_id].status, ltm.STATUS_SUPERSEDED)
        self.assertEqual(facts[old_id].superseded_by, new_id)
        self.assertEqual(facts[new_id].status, ltm.STATUS_ACTIVE)
        self.assertIsNone(facts[new_id].superseded_by)

    async def test_non_conflicting_same_category_facts_coexist(self) -> None:
        first = await self._record("喜欢喝咖啡", category="preference")
        second = await self._record("喜欢打羽毛球", category="preference")
        facts = {fact.id: fact for fact in await self._facts()}
        self.assertEqual(facts[first].status, ltm.STATUS_ACTIVE)
        self.assertEqual(facts[second].status, ltm.STATUS_ACTIVE)

    async def test_event_facts_get_an_expiry_date(self) -> None:
        event_id = await self._record("下个月去日本旅游", category="event")
        other_id = await self._record("喜欢喝咖啡", category="preference")
        facts = {fact.id: fact for fact in await self._facts()}
        self.assertIsNotNone(facts[event_id].expires_at)
        self.assertEqual(
            facts[event_id].expires_at, NOW + timedelta(days=30)
        )
        self.assertIsNone(facts[other_id].expires_at)

    async def test_opted_out_subject_is_rejected(self) -> None:
        async with self.session_factory() as session:
            await ltm.set_optout(session, SENDER_ID, now=NOW)
        self.assertEqual(await self._record("喜欢喝咖啡"), 0)
        self.assertEqual(await self._facts(), [])

    async def test_write_failure_is_swallowed(self) -> None:
        stored = await ltm.record_fact(
            _ExplodingSession(),
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            subject_user_id=SENDER_ID,
            fact_text="喜欢喝咖啡",
            category="preference",
            confidence=60,
            source_kind=ltm.SOURCE_PASSIVE,
            evidence_excerpt="出处",
        )
        self.assertEqual(stored, 0)


# ---------------------------------------------------------------------------
# FTS 影子表
# ---------------------------------------------------------------------------


class UserFactsFtsTests(_DbTestCase):
    async def test_fts_projection_tracks_user_facts(self) -> None:
        async with self.session_factory() as session:
            present = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='user_facts_fts'"
                    )
                )
            ).scalar_one_or_none()
        if not present:
            self.skipTest("本环境没有 FTS5/trigram，读取侧会退回关键词排序")
        await self._record("rtx5090 显卡很贵")
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT user_facts_fts.rowid FROM user_facts_fts "
                        "WHERE user_facts_fts MATCH :match_query"
                    ),
                    {"match_query": '"rtx5090"'},
                )
            ).all()
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# 6/7) opt-out 与护栏
# ---------------------------------------------------------------------------


class OptOutTests(_DbTestCase):
    async def test_optout_soft_deletes_active_facts_and_blocks_loading(self) -> None:
        await self._record("喜欢喝咖啡")
        async with self.session_factory() as session:
            removed = await ltm.set_optout(session, SENDER_ID, now=NOW)
        self.assertEqual(removed, 1)
        facts = await self._facts()
        self.assertEqual(facts[0].status, ltm.STATUS_DELETED)
        async with self.session_factory() as session:
            self.assertTrue(await ltm.is_opted_out(session, SENDER_ID))
            records = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                query="咖啡",
            )
        self.assertEqual(records, [])

    async def test_optout_blocks_extraction_for_private_scope(self) -> None:
        await self._private_turn("我喜欢喝咖啡", key="u:1")
        async with self.session_factory() as session:
            await ltm.set_optout(session, SENDER_ID, now=NOW)
            keep = await ltm.should_extract(
                ltm.SCOPE_PRIVATE, SENDER_ID, _settings(), session
            )
        self.assertFalse(keep)
        llm = _StubLLM(json.dumps([{"subject_user_id": SENDER_ID, "fact": "喜欢喝咖啡"}]))
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_PRIVATE,
            scope_id=SENDER_ID,
            llm=llm,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 0)
        self.assertEqual(await self._facts(), [])

    async def test_clear_optout_does_not_restore_facts(self) -> None:
        await self._record("喜欢喝咖啡")
        async with self.session_factory() as session:
            await ltm.set_optout(session, SENDER_ID, now=NOW)
            self.assertTrue(await ltm.clear_optout(session, SENDER_ID))
            self.assertFalse(await ltm.is_opted_out(session, SENDER_ID))
        facts = await self._facts()
        self.assertEqual(facts[0].status, ltm.STATUS_DELETED)

    async def test_optout_rows_survive_as_the_switch(self) -> None:
        async with self.session_factory() as session:
            await ltm.set_optout(session, SENDER_ID, reason="/memory off", now=NOW)
            row = await session.get(MemoryOptout, SENDER_ID)
        self.assertIsNotNone(row)
        self.assertEqual(row.reason, "/memory off")


class GuardrailTests(_DbTestCase):
    async def test_should_extract_needs_enough_new_messages(self) -> None:
        # 下限是 5（见 test_config_getters_default_and_clamp），所以边界用 5 来测
        settings = _settings(memory_extract_min_messages=5)
        for index in range(1, 5):
            await self._archive(f"第{index}条", message_id=index)
        async with self.session_factory() as session:
            self.assertFalse(
                await ltm.should_extract(ltm.SCOPE_GROUP, GROUP_ID, settings, session)
            )
        await self._archive("第五条", message_id=5)
        async with self.session_factory() as session:
            self.assertTrue(
                await ltm.should_extract(ltm.SCOPE_GROUP, GROUP_ID, settings, session)
            )

    async def test_should_extract_respects_the_switches(self) -> None:
        await self._archive("第一条")
        for overrides in (
            {"memory_facts_enabled": False},
            {"memory_extract_enabled": False},
        ):
            with self.subTest(overrides=overrides):
                settings = _settings(memory_extract_min_messages=1, **overrides)
                async with self.session_factory() as session:
                    self.assertFalse(
                        await ltm.should_extract(
                            ltm.SCOPE_GROUP, GROUP_ID, settings, session
                        )
                    )

    async def test_extract_daily_cap_skips_and_only_logs(self) -> None:
        # 先满足 min_messages 下限（5），才走得到「今日上限」这一层
        for index in range(1, 6):
            await self._archive(f"第{index}条", message_id=index)
        settings = _settings(
            memory_extract_min_messages=5, memory_extract_daily_cap=1
        )
        ltm.note_extraction_run(ltm.SCOPE_GROUP, OTHER_GROUP_ID, now=NOW)
        with self.assertLogs("bot.services.long_term_memory", level="INFO") as captured:
            async with self.session_factory() as session:
                keep = await ltm.should_extract(
                    ltm.SCOPE_GROUP, GROUP_ID, settings, session, now=NOW
                )
        self.assertFalse(keep)
        self.assertTrue(
            any("上限" in line for line in captured.output),
            "超护栏必须留下日志（只记日志、不抛异常）",
        )

    async def test_tool_daily_cap_skips_further_writes(self) -> None:
        settings = _settings(memory_tool_daily_cap=1)
        skill = RememberSkill(settings)
        context = SkillContext(
            session_factory=self.session_factory,
            chat_id=GROUP_ID,
            sender_user_id=SENDER_ID,
            current_user_text="我喜欢喝咖啡",
        )
        first = await skill.run({"fact": "喜欢喝咖啡"}, context)
        self.assertEqual(first.summary, "已记住")
        with self.assertLogs("bot.services.skills.remember", level="INFO") as captured:
            second = await skill.run({"fact": "喜欢打羽毛球"}, context)
        self.assertNotEqual(second.summary, "已记住")
        self.assertTrue(any("上限" in line for line in captured.output))
        facts = await self._facts()
        self.assertEqual(len(facts), 1)

    async def test_tool_respects_the_master_switch(self) -> None:
        skill = RememberSkill(_settings(memory_facts_enabled=False))
        context = SkillContext(
            session_factory=self.session_factory,
            chat_id=GROUP_ID,
            sender_user_id=SENDER_ID,
            current_user_text="我喜欢喝咖啡",
        )
        result = await skill.run({"fact": "喜欢喝咖啡"}, context)
        self.assertNotEqual(result.summary, "已记住")
        self.assertEqual(await self._facts(), [])


# ---------------------------------------------------------------------------
# 提炼
# ---------------------------------------------------------------------------


class ExtractionTests(_DbTestCase):
    def _response(self, subject: int, fact: str, evidence: str) -> str:
        return json.dumps(
            [
                {
                    "subject_user_id": subject,
                    "fact": fact,
                    "category": "preference",
                    "confidence": 72,
                    "evidence": evidence,
                }
            ],
            ensure_ascii=False,
        )

    async def test_extraction_writes_facts_and_advances_the_cursor(self) -> None:
        await self._archive("我很喜欢喝咖啡，每天都在喝", message_id=1)
        await self._archive("顺便说一句，显卡还是 5090 香", message_id=2)
        llm = _StubLLM(
            self._response(SENDER_ID, "喜欢喝咖啡，每天都在喝", "我很喜欢喝咖啡，每天都在喝")
        )
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=llm,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 1)
        facts = await self._facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].scope, ltm.SCOPE_GROUP)
        self.assertEqual(facts[0].scope_id, GROUP_ID)
        self.assertEqual(facts[0].subject_user_id, SENDER_ID)
        self.assertEqual(facts[0].category, "preference")
        self.assertEqual(facts[0].confidence, 72)
        self.assertEqual(facts[0].source_kind, ltm.SOURCE_PASSIVE)
        self.assertEqual(facts[0].source_message_id, 2)
        self.assertEqual(await self._cursor(ltm.SCOPE_GROUP, GROUP_ID), 2)
        # 输入里必须带上发送者 id 名单（模型据此填 subject_user_id）
        self.assertIn(f"{SENDER_ID}", llm.calls[0][1])
        self.assertIn(SENDER_NAME, llm.calls[0][1])

    async def test_extraction_is_idempotent_across_batches(self) -> None:
        await self._archive("我很喜欢喝咖啡", message_id=1)
        llm = _StubLLM(
            [
                self._response(SENDER_ID, "喜欢喝咖啡", "我很喜欢喝咖啡"),
                self._response(SENDER_ID, "喜欢喝咖啡。", "我真的很喜欢喝咖啡"),
            ]
        )
        settings = _settings()
        await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=llm,
            settings=settings,
            session_factory=self.session_factory,
        )
        await self._archive("我真的很喜欢喝咖啡", message_id=2)
        await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=llm,
            settings=settings,
            session_factory=self.session_factory,
        )
        facts = await self._facts()
        self.assertEqual(len(facts), 1, "同一批跑两次不该把事实翻倍")
        self.assertEqual(facts[0].confirm_count, 2)

    async def test_parse_failure_keeps_the_cursor_and_retries_next_round(self) -> None:
        await self._archive("我很喜欢喝咖啡", message_id=1)
        broken = _StubLLM("对不起，我不会输出 JSON")
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=broken,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 0)
        self.assertEqual(await self._cursor(ltm.SCOPE_GROUP, GROUP_ID), 0)
        good = _StubLLM(self._response(SENDER_ID, "喜欢喝咖啡", "我很喜欢喝咖啡"))
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=good,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 1, "游标没前移，下一轮必须重试同一批")

    async def test_model_error_keeps_the_cursor(self) -> None:
        await self._archive("我很喜欢喝咖啡", message_id=1)

        class _Boom:
            async def generate(self, system: str, user_text: str) -> str:
                raise RuntimeError("模型炸了")

        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=_Boom(),
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 0)
        self.assertEqual(await self._cursor(ltm.SCOPE_GROUP, GROUP_ID), 0)

    async def test_sensitive_and_unattributable_items_are_dropped(self) -> None:
        await self._archive("他的手机号是 13812345678", message_id=1)
        payload = json.dumps(
            [
                {
                    "subject_user_id": SENDER_ID,
                    "fact": "他的手机号是 13812345678",
                    "category": "identity",
                    "confidence": 90,
                    "evidence": "他的手机号是 13812345678",
                },
                {
                    "subject_user_id": SENDER_ID,
                    "fact": "他的登录密码是 hunter2",
                    "category": "identity",
                    "confidence": 90,
                    "evidence": "他的手机号是 13812345678",
                },
                {
                    # 别人（999 不在本批输入里）：不许记
                    "subject_user_id": 999,
                    "fact": "999 的秘密是喜欢猫",
                    "category": "preference",
                    "confidence": 90,
                    "evidence": "他的手机号是 13812345678",
                },
                {
                    # 出处不是原文片段：不许记
                    "subject_user_id": SENDER_ID,
                    "fact": "他住在一个很远的城市",
                    "category": "identity",
                    "confidence": 90,
                    "evidence": "这句话根本不在输入里",
                },
            ],
            ensure_ascii=False,
        )
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=_StubLLM(payload),
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 0)
        self.assertEqual(await self._facts(), [])
        # 游标照常前移：这批内容已经处理过，不因为「全部被拒」而卡住
        self.assertEqual(await self._cursor(ltm.SCOPE_GROUP, GROUP_ID), 1)

    async def test_extraction_skips_bots_non_text_and_opted_out_senders(self) -> None:
        await self._archive(
            "机器人自己的话", message_id=1, role="assistant", sender_id=0
        )
        await self._archive(
            "一张图片", message_id=2, message_type="photo"
        )
        await self._archive("另一个人的话", message_id=3, sender_id=555)
        async with self.session_factory() as session:
            await ltm.set_optout(session, 555, now=NOW)
        llm = _StubLLM(
            self._response(SENDER_ID, "喜欢喝咖啡", "另一个人的话")
        )
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_GROUP,
            scope_id=GROUP_ID,
            llm=llm,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 0)
        self.assertEqual(llm.calls, [], "没有可提炼的内容时不该调模型")
        self.assertEqual(await self._cursor(ltm.SCOPE_GROUP, GROUP_ID), 3)

    async def test_private_scope_extraction_reads_only_that_users_rows(self) -> None:
        await self._private_turn("我喜欢喝咖啡", key="u:1")
        await self._private_turn("别人说的话", user_id=555, key="u:1")
        llm = _StubLLM(
            self._response(SENDER_ID, "喜欢喝咖啡", "我喜欢喝咖啡")
        )
        written = await ltm.extract_facts(
            scope=ltm.SCOPE_PRIVATE,
            scope_id=SENDER_ID,
            llm=llm,
            settings=_settings(),
            session_factory=self.session_factory,
        )
        self.assertEqual(written, 1)
        self.assertNotIn("别人说的话", llm.calls[0][1])
        facts = await self._facts()
        self.assertEqual(facts[0].scope, ltm.SCOPE_PRIVATE)
        self.assertEqual(facts[0].scope_id, SENDER_ID)
        self.assertEqual(facts[0].subject_user_id, SENDER_ID)


class ExtractionRoundTests(_DbTestCase):
    async def test_round_walks_authorized_groups_and_private_users(self) -> None:
        from bot.db.models import AuthorizedGroup

        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=GROUP_ID, authorized_by=1))
            await session.commit()
        for index in range(1, 6):
            await self._archive(f"我很喜欢喝咖啡 {index}", message_id=index)
        for index in range(1, 6):
            await self._private_turn(f"我喜欢喝咖啡 {index}", key=f"u:{index}")
        llm = _StubLLM(
            lambda user_text: json.dumps(
                [
                    {
                        "subject_user_id": SENDER_ID,
                        "fact": "喜欢喝咖啡",
                        "category": "preference",
                        "confidence": 70,
                        "evidence": "喜欢喝咖啡",
                    }
                ],
                ensure_ascii=False,
            )
        )
        settings = _settings(memory_extract_min_messages=5)
        touched = await ltm.run_extraction_round(
            self.session_factory, llm=llm, settings=settings, now=NOW
        )
        self.assertEqual(touched, 2, "授权群与私聊用户各算一个作用域")
        facts = await self._facts()
        self.assertEqual({fact.scope for fact in facts}, {"group", "private"})

    async def test_round_stops_once_the_daily_cap_is_reached(self) -> None:
        from bot.db.models import AuthorizedGroup

        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=GROUP_ID, authorized_by=1))
            await session.commit()
        await self._archive("我很喜欢喝咖啡", message_id=1)
        llm = _StubLLM("[]")
        settings = _settings(
            memory_extract_min_messages=1, memory_extract_daily_cap=1
        )
        ltm.note_extraction_run(ltm.SCOPE_GROUP, OTHER_GROUP_ID, now=NOW)
        with self.assertLogs("bot.services.long_term_memory", level="INFO") as captured:
            touched = await ltm.run_extraction_round(
                self.session_factory, llm=llm, settings=settings, now=NOW
            )
        self.assertEqual(touched, 0)
        self.assertEqual(llm.calls, [])
        self.assertTrue(any("上限" in line for line in captured.output))

    async def test_extraction_loop_survives_failures(self) -> None:
        """常驻循环单次失败只记日志、不退出（照 run_search_record_maintenance）。"""

        import asyncio

        async def _boom(*args, **kwargs):
            raise RuntimeError("巡检炸了")

        task = asyncio.create_task(
            ltm.run_long_term_memory_extraction(
                self.session_factory,
                llm=_StubLLM("[]"),
                settings=_settings(),
                interval_seconds=1.0,
            )
        )
        with patch.object(ltm, "run_extraction_round", _boom):
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "单次失败不该让循环退出")
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# 8) 读取：相关才注入 + 规模
# ---------------------------------------------------------------------------


class LoadRelevantFactsTests(_DbTestCase):
    async def test_unrelated_query_injects_nothing(self) -> None:
        await self._record("喜欢喝咖啡")
        async with self.session_factory() as session:
            self.assertEqual(
                await ltm.load_relevant_facts(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=SENDER_ID,
                    query="今天天气怎么样",
                ),
                [],
            )
            self.assertEqual(
                await ltm.load_relevant_facts(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=SENDER_ID,
                    query="",
                ),
                [],
            )

    async def test_scope_is_a_hard_boundary(self) -> None:
        await self._record(
            "私聊才知道的咖啡偏好",
            scope=ltm.SCOPE_PRIVATE,
            scope_id=SENDER_ID,
            subject_user_id=SENDER_ID,
        )
        await self._record("群里公开的咖啡偏好", scope=ltm.SCOPE_GROUP)
        async with self.session_factory() as session:
            group_hits = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=[0, SENDER_ID],
                query="咖啡偏好",
            )
        self.assertEqual(
            [record["fact_text"] for record in group_hits], ["群里公开的咖啡偏好"]
        )

    async def test_group_public_facts_and_speaker_facts_both_load(self) -> None:
        await self._record("本群都在聊显卡", subject_user_id=0)
        await self._record("他喜欢显卡", subject_user_id=SENDER_ID)
        await self._record("别人的秘密", subject_user_id=555)
        async with self.session_factory() as session:
            records = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=[0, SENDER_ID],
                query="聊显卡",
            )
        self.assertEqual(len(records), 2)
        self.assertEqual({record["subject_user_id"] for record in records}, {0, SENDER_ID})

    async def test_expired_events_are_not_injected(self) -> None:
        fact_id = await self._record("下个月去日本旅游", category="event")
        async with self.session_factory() as session:
            records = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                query="去日本旅游",
                now=NOW + timedelta(days=31),
            )
        self.assertEqual(records, [])
        async with self.session_factory() as session:
            self.assertEqual(
                await ltm.expire_event_facts(session, now=NOW + timedelta(days=31)), 1
            )
        facts = {fact.id: fact for fact in await self._facts()}
        self.assertEqual(facts[fact_id].status, ltm.STATUS_DELETED)

    async def test_newer_confirmation_wins_when_relevance_ties(self) -> None:
        old = await self._record("他喜欢喝咖啡", now=NOW - timedelta(days=5))
        new = await self._record(
            "他每天都要咖啡", now=NOW - timedelta(hours=1)
        )
        async with self.session_factory() as session:
            records = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                query="咖啡",
            )
        self.assertEqual([record["id"] for record in records], [new, old])

    async def test_limit_and_size_stay_bounded(self) -> None:
        # 用非冲突类别：preference 属「冲突替代」类，20 条近似文本会互相替代只剩 1 条
        for index in range(20):
            await self._record(f"他喜欢咖啡 {index}", category="other")
        async with self.session_factory() as session:
            records = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=SENDER_ID,
                query="喜欢咖啡",
                limit=5,
            )
        self.assertEqual(len(records), 5)
        messages = ltm.render_facts_block(records, max_records=8)
        self.assertLessEqual(len(messages), 8)
        for message in messages:
            self.assertLessEqual(len(message["content"]), 200 + 80)

    async def test_private_chat_reader_merges_own_and_group_facts(self) -> None:
        await self._record(
            "私聊里说过的咖啡偏好",
            scope=ltm.SCOPE_PRIVATE,
            scope_id=SENDER_ID,
            subject_user_id=SENDER_ID,
        )
        await self._record("群里公开聊过咖啡")
        async with self.session_factory() as session:
            records = await ltm.load_private_chat_facts(
                session,
                user_id=SENDER_ID,
                group_ids=[GROUP_ID, OTHER_GROUP_ID],
                query="咖啡",
                titles={GROUP_ID: "显卡群"},
            )
        self.assertEqual(len(records), 2)
        labels = {record["source_label"] for record in records}
        self.assertEqual(labels, {"私聊", "显卡群"})

    async def test_private_chat_reader_returns_empty_on_failure(self) -> None:
        async with self.session_factory() as session:
            self.assertEqual(
                await ltm.load_private_chat_facts(
                    _ExplodingSession(),
                    user_id=SENDER_ID,
                    group_ids=[GROUP_ID],
                    query="咖啡",
                ),
                [],
            )

    async def test_load_failure_returns_empty_list(self) -> None:
        async with self.session_factory() as session:
            records = await ltm.load_relevant_facts(
                _ExplodingSession(),
                scope=ltm.SCOPE_PRIVATE,
                scope_id=SENDER_ID,
                subject_user_id=SENDER_ID,
                query="咖啡",
            )
        self.assertEqual(records, [])


# ---------------------------------------------------------------------------
# 8) 群聊注入（接进第 3 期的统一闸门）
# ---------------------------------------------------------------------------


class GroupInjectionTests(_DbTestCase):
    def _memory(self):
        return SimpleNamespace(session_factory=self.session_factory)

    async def _inject(self, query: str, *, settings=None):
        from bot.handlers import group as group_handler

        return await group_handler._inject_group_long_term_memory(
            history=[{"role": "user", "content": "群里随便聊聊"}],
            group_id=GROUP_ID,
            speaker_user_id=SENDER_ID,
            query=query,
            memory=self._memory(),
            settings=settings or _settings(),
        )

    async def test_relevant_facts_are_injected_with_the_header(self) -> None:
        await self._record("喜欢喝咖啡")
        history = await self._inject("你们谁喜欢咖啡")
        joined = "\n".join(str(item.get("content") or "") for item in history)
        self.assertIn(ltm.LONG_TERM_MEMORY_HEADER_BLOCK, joined)
        self.assertIn("喜欢喝咖啡", joined)
        self.assertIn("长期记忆 · 群内", joined)

    async def test_irrelevant_topic_injects_nothing(self) -> None:
        await self._record("喜欢喝咖啡")
        history = [{"role": "user", "content": "群里随便聊聊"}]
        from bot.handlers import group as group_handler

        merged = await group_handler._inject_group_long_term_memory(
            history=history,
            group_id=GROUP_ID,
            speaker_user_id=SENDER_ID,
            query="今天天气怎么样",
            memory=self._memory(),
            settings=_settings(),
        )
        self.assertEqual(merged, history)

    async def test_private_facts_never_reach_the_group_injection(self) -> None:
        await self._record(
            "PRIVATE_SCOPE_ONLY_aa11 喜欢喝咖啡",
            scope=ltm.SCOPE_PRIVATE,
            scope_id=SENDER_ID,
            subject_user_id=SENDER_ID,
        )
        history = await self._inject("喜欢咖啡")
        joined = "\n".join(str(item.get("content") or "") for item in history)
        self.assertNotIn("PRIVATE_SCOPE_ONLY_aa11", joined)

    async def test_master_switch_off_injects_nothing(self) -> None:
        await self._record("喜欢喝咖啡")
        history = await self._inject(
            "你们谁喜欢咖啡", settings=_settings(memory_facts_enabled=False)
        )
        joined = "\n".join(str(item.get("content") or "") for item in history)
        self.assertNotIn("喜欢喝咖啡", joined)
        self.assertNotIn(ltm.LONG_TERM_MEMORY_HEADER_BLOCK, joined)

    async def test_injection_respects_the_recall_limit(self) -> None:
        # 同上：用非冲突类别，20 条才能并存，才测得出注入条数上限
        for index in range(20):
            await self._record(f"他喜欢咖啡 {index}", category="other")
        history = await self._inject("喜欢咖啡", settings=_settings(memory_recall_limit=3))
        joined = "\n".join(str(item.get("content") or "") for item in history)
        self.assertEqual(joined.count("- [长期记忆 · 群内"), 3)


# ---------------------------------------------------------------------------
# 4/维护：留存清理
# ---------------------------------------------------------------------------


class MaintenanceTests(_DbTestCase):
    async def test_prune_removes_only_old_deleted_rows(self) -> None:
        fresh = await self._record("刚删掉的", now=NOW - timedelta(days=1))
        old = await self._record("早就删掉的", now=NOW - timedelta(days=90))
        kept = await self._record("还在用的事实", now=NOW - timedelta(days=90))
        async with self.session_factory() as session:
            await ltm.soft_delete_fact(session, fresh, now=NOW - timedelta(days=1))
            await ltm.soft_delete_fact(session, old, now=NOW - timedelta(days=90))
            removed = await ltm.prune_user_facts(
                session, retention_days=30, now=NOW
            )
        self.assertEqual(removed, 1)
        remaining = {fact.id for fact in await self._facts()}
        self.assertEqual(remaining, {fresh, kept})

    async def test_maintenance_loop_prunes_and_keeps_running(self) -> None:
        import asyncio

        fact_id = await self._record("早就删掉的", now=ANCIENT)
        async with self.session_factory() as session:
            await ltm.soft_delete_fact(session, fact_id, now=ANCIENT)
        calls: list[int] = []

        def _days() -> int:
            calls.append(1)
            return 30

        task = asyncio.create_task(
            ltm.run_long_term_memory_maintenance(
                self.session_factory,
                retention_days_getter=_days,
                interval_seconds=1.0,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.assertTrue(calls, "留存天数必须每轮现取")
        self.assertEqual(await self._facts(), [])


# ---------------------------------------------------------------------------
# remember 工具
# ---------------------------------------------------------------------------


class RememberSkillTests(_DbTestCase):
    def _context(self, *, chat_id: int = GROUP_ID, sender: int = SENDER_ID) -> SkillContext:
        return SkillContext(
            session_factory=self.session_factory,
            chat_id=chat_id,
            sender_user_id=sender,
            current_user_text="我平时很喜欢喝咖啡",
        )

    async def test_registered_in_the_skill_service(self) -> None:
        from bot.services.skills.service import SkillService

        service = SkillService(SimpleNamespace())
        self.assertIn("remember", service.available_skill_names())

    async def test_subject_is_always_the_sender(self) -> None:
        skill = RememberSkill(_settings())
        result = await skill.run(
            {"fact": "喜欢喝咖啡", "category": "preference", "confidence": 80},
            self._context(),
        )
        self.assertEqual(result.summary, "已记住")
        facts = await self._facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].subject_user_id, SENDER_ID)
        self.assertEqual(facts[0].scope, ltm.SCOPE_GROUP)
        self.assertEqual(facts[0].scope_id, GROUP_ID)
        self.assertEqual(facts[0].source_kind, ltm.SOURCE_TOOL)
        self.assertEqual(facts[0].confidence, 80)
        self.assertIn("咖啡", facts[0].evidence_excerpt)

    async def test_private_chat_scope_is_the_user_itself(self) -> None:
        skill = RememberSkill(_settings())
        result = await skill.run(
            {"fact": "喜欢喝咖啡"}, self._context(chat_id=SENDER_ID, sender=SENDER_ID)
        )
        self.assertEqual(result.summary, "已记住")
        facts = await self._facts()
        self.assertEqual(facts[0].scope, ltm.SCOPE_PRIVATE)
        self.assertEqual(facts[0].scope_id, SENDER_ID)
        self.assertEqual(facts[0].subject_user_id, SENDER_ID)

    async def test_summary_never_echoes_the_fact(self) -> None:
        skill = RememberSkill(_settings())
        result = await skill.run(
            {"fact": "他在日本读研，讨厌香菜"}, self._context()
        )
        self.assertEqual(result.summary, "已记住")
        self.assertNotIn("香菜", result.summary)
        self.assertNotIn("日本", result.summary)

    async def test_sensitive_or_empty_facts_are_skipped(self) -> None:
        skill = RememberSkill(_settings())
        context = self._context()
        for arguments in (
            {"fact": "他的手机号是 13812345678"},
            {"fact": "   "},
            {"fact": "密码是 hunter2hunter2"},
        ):
            with self.subTest(arguments=arguments):
                result = await skill.run(arguments, context)
                self.assertNotEqual(result.summary, "已记住")
        self.assertEqual(await self._facts(), [])

    async def test_daily_cap_by_scope_and_integer_schema(self) -> None:
        self.assertEqual(
            RememberSkill.parameters_schema["properties"]["fact"]["type"], "string"
        )
        self.assertIn("fact", RememberSkill.parameters_schema["required"])
        self.assertFalse(
            RememberSkill.parameters_schema["additionalProperties"],
            "工具不接受额外参数：模型连「记别人」的入口都没有",
        )


# ---------------------------------------------------------------------------
# 10) 接线
# ---------------------------------------------------------------------------


class MainWiringTests(unittest.TestCase):
    """接线处的名字必须真的能解析（上一期就因为漏 import 被抓到）。"""

    def test_main_module_exposes_the_two_runners(self) -> None:
        from bot import __main__ as main_module

        for name in (
            "run_long_term_memory_extraction",
            "run_long_term_memory_maintenance",
            "memory_deleted_retention_days",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    callable(getattr(main_module, name, None)),
                    f"__main__ 必须能解析 {name}（第 4 期的两条巡检要用）",
                )

    def test_main_module_retention_getter_reads_settings(self) -> None:
        from bot import __main__ as main_module
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.bot.memory_deleted_retention_days = 45
        self.assertEqual(main_module.memory_deleted_retention_days(settings), 45)

    def test_group_handler_exposes_the_injection(self) -> None:
        from bot.handlers import group as group_module

        self.assertTrue(
            callable(
                getattr(group_module, "_inject_group_long_term_memory", None)
            ),
            "群聊装配处必须有 _inject_group_long_term_memory",
        )
        self.assertEqual(group_module.SCOPE_GROUP, "group")

    def test_service_module_exposes_the_public_api(self) -> None:
        for name in (
            "record_fact",
            "should_extract",
            "extract_facts",
            "load_relevant_facts",
            "render_facts_block",
            "format_fact_line",
            "normalize_fact_text",
            "fact_fingerprint",
            "is_conflicting",
            "run_long_term_memory_extraction",
            "run_long_term_memory_maintenance",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(ltm, name, None)))

    def test_real_settings_instance_feeds_every_getter(self) -> None:
        """拿一个真 ``Settings`` 调一次取值：字段漏接线/漏 import 会当场炸。"""

        from bot.config import Settings
        from bot.services.long_term_memory import (
            memory_event_ttl_days,
            memory_extract_batch_max,
            memory_extract_daily_cap,
            memory_extract_enabled,
            memory_extract_interval_minutes,
            memory_extract_min_messages,
            memory_facts_enabled,
            memory_recall_limit,
            memory_tool_daily_cap,
            memory_tool_enabled,
        )

        settings = Settings(_env_file=None)
        self.assertTrue(memory_facts_enabled(settings))
        self.assertTrue(memory_extract_enabled(settings))
        self.assertTrue(memory_tool_enabled(settings))
        self.assertEqual(memory_extract_interval_minutes(settings), 30)
        self.assertEqual(memory_extract_min_messages(settings), 20)
        self.assertEqual(memory_extract_daily_cap(settings), 48)
        self.assertEqual(memory_extract_batch_max(settings), 200)
        self.assertEqual(memory_tool_daily_cap(settings), 30)
        self.assertEqual(memory_recall_limit(settings), 8)
        self.assertEqual(memory_event_ttl_days(settings), 30)
        self.assertEqual(ltm.memory_deleted_retention_days(settings), 30)
        # 运行时覆盖也要能读到（第 4 期 E 项：全部可运行时覆盖）
        settings.bot.memory_recall_limit = 3
        self.assertEqual(memory_recall_limit(settings), 3)

    def test_runtime_config_carries_the_new_fields(self) -> None:
        from bot.config import Settings
        from bot.services.runtime_config import RuntimeConfig

        settings = Settings(_env_file=None)
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "memory_facts_enabled": False,
                    "memory_extract_daily_cap": 7,
                    "memory_recall_limit": 4,
                    "memory_event_ttl_days": 60,
                    "memory_deleted_retention_days": 15,
                }
            }
        )
        config.apply_to_settings(settings, apply_prompts=False)
        self.assertFalse(settings.bot.memory_facts_enabled)
        self.assertEqual(settings.bot.memory_extract_daily_cap, 7)
        self.assertEqual(settings.bot.memory_recall_limit, 4)
        self.assertEqual(settings.bot.memory_event_ttl_days, 60)
        self.assertEqual(settings.bot.memory_deleted_retention_days, 15)

    def test_bot_behavior_config_rejects_out_of_range_values(self) -> None:
        from pydantic import ValidationError

        from bot.services.runtime_config import BotBehaviorConfig

        for payload in (
            {"memory_extract_interval_minutes": 1},
            {"memory_extract_min_messages": 1},
            {"memory_extract_daily_cap": 9999},
            {"memory_extract_batch_max": 5},
            {"memory_tool_daily_cap": 9999},
            {"memory_recall_limit": 0},
            {"memory_event_ttl_days": 0},
            {"memory_deleted_retention_days": 0},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    BotBehaviorConfig(**payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
