"""``/points`` 近 30 天积分流水的回归测试（P01–P11 + 返工 F01–F06）。

覆盖四层：

1. 只读聚合层（``bot.services.points_history``）：30 天窗口与边界、时区规范化、
   三本真实台账（``member_checkins`` / ``member_point_awards`` /
   ``member_point_spends``）的正负与摘要、一致读（一次查询一个快照）、
   冻结水位、SQL 分页与完整读取等价、理由映射、HTML 转义与长度上限、
   有界会话描述符（不保存页面文本 / 完整历史 / 余额）。
2. 命令层（``cmd_points``）：走**真实**送达链（``_answer`` →
   ``_render_action_response`` → ``answer_with_auto_delete`` → ``message.answer``），
   断言最终发出的正文里折叠实体、摘要位置与长度；保留首轮已有的余额/签到子串；
   只读；机器人与无效身份被拒。
3. 翻页回调层：callback_data 短且不含身份、只编辑原消息、绑定原查询人 / 原群 /
   原消息 / 冻结时刻 / 高水位、过期与授权撤销后不再编辑、编辑失败可重试。
4. 有界状态：TTL 真删 + 条数上限，单会话持久状态不随流水条数增长。

身份与群 id 全部来自 ``tests/test_public_artifact_scan.py`` 的 ``SYNTHETIC_IDS``，
不引入任何真实群 / 用户。
"""

import asyncio
import copy
import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    AuthorizedGroup,
    MemberCheckin,
    MemberPointAward,
    MemberPointSpend,
)
from bot.handlers import commands
from bot.services import checkin as checkin_service
from bot.services import points_history as ph
from bot.services.activity import AWARD_REASON_WEEKLY_ACTIVITY
from bot.services.checkin import SPEND_REASON_CHALLENGE
from bot.services.point_shop import (
    AWARD_REASON_LOTTERY,
    AWARD_REASON_REFUND,
    SPEND_REASON_LOTTERY,
    SPEND_REASON_PIN,
    SPEND_REASON_TAG,
)

# 合成身份（见 tests/test_public_artifact_scan.py 的 SYNTHETIC_IDS）
GROUP = -1009999999001
OTHER_GROUP = -1009999999002
USER = 700000001
OTHER_USER = 700000002
#: 服务层测试固定注入的时刻；命令层用 _frozen_clock() 冻结同一个时刻。
NOW = datetime(2026, 9, 29, 12, 0, 0)


def _day(offset: int = 0) -> datetime:
    """以冻结时刻为基准的第 offset 天（本地时间）。"""

    return NOW + timedelta(days=offset)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        SimpleNamespace(
            __api_method__="editMessageText",
            __url__="https://api.telegram.org/boteditMessageText",
        ),
        message,
    )


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        SimpleNamespace(
            __api_method__="editMessageText",
            __url__="https://api.telegram.org/boteditMessageText",
        ),
        "Forbidden: message can't be edited",
    )


class _FakeOutbound:
    """``message.answer()`` 的返回值：只提供 ``message_id``。"""

    def __init__(self, message_id):
        self.message_id = message_id


class _FakeMessage:
    """最小可用 Message：只实现 ``_answer``/``answer_with_auto_delete`` 真正用的接口。"""

    def __init__(
        self,
        text="/points",
        *,
        chat_id=GROUP,
        chat_type="supergroup",
        user_id=USER,
        message_id=101,
        is_bot=False,
        sent_id=900,
        full_name="测试成员",
    ):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = (
            None
            if user_id is None
            else SimpleNamespace(id=user_id, is_bot=is_bot, full_name=full_name)
        )
        self.message_id = message_id
        self.reply_markup = None
        self.sent_id = sent_id
        self.answers = []
        self.edits = []
        self.answer_failure = None
        self.edit_failures = []
        #: 发送时刻的事务探针（用来证明发送前已经放掉读连接）。
        self.session_probe = None
        #: 发送时刻的**驱动层**事务探针（比 ORM 记账更硬：拿到的是 sqlite3 连接）。
        self.driver_probe = None
        self.transaction_state_at_answer = []
        self.driver_state_at_answer = []
        self.transaction_state_at_edit = []
        self.driver_state_at_edit = []

    def card_view(self) -> "_FakeMessage":
        """回调里看到的「已发出那张卡」的替身。

        真实链路里 ``message.answer`` 返回的是**另一条**消息，消息 id 由 Telegram
        分配；``callback.message`` 就是那条消息。这里用浅拷贝得到同一个 id，同时
        共享 ``answers``/``edits`` 列表，所以断言仍然写在原消息对象上。
        """

        card = copy.copy(self)
        card.message_id = self.sent_id
        return card

    @property
    def last_answer(self) -> str:
        return self.answers[-1][0]

    @property
    def last_reply_markup(self):
        return self.answers[-1][1].get("reply_markup")

    async def answer(self, text, **kwargs):
        if self.session_probe is not None:
            self.transaction_state_at_answer.append(bool(self.session_probe()))
        if self.driver_probe is not None:
            self.driver_state_at_answer.append(bool(self.driver_probe.in_transaction))
        if self.answer_failure is not None:
            raise self.answer_failure
        self.answers.append((text, dict(kwargs)))
        if self.sent_id is None:
            return _FakeOutbound(None)
        return _FakeOutbound(self.sent_id + len(self.answers) - 1)

    async def edit_text(self, text, **kwargs):
        if self.edit_failures:
            failure = self.edit_failures.pop(0)
            if failure is not None:
                raise failure
        if self.session_probe is not None:
            self.transaction_state_at_edit.append(bool(self.session_probe()))
        if self.driver_probe is not None:
            self.driver_state_at_edit.append(bool(self.driver_probe.in_transaction))
        self.edits.append((text, dict(kwargs)))
        return self


class _FakeCallback:
    """最小可用 CallbackQuery。"""

    def __init__(self, *, data, message, user_id=USER, is_bot=False, answer_failure=None):
        self.data = data
        self.message = message
        self.from_user = (
            None
            if user_id is None
            else SimpleNamespace(id=user_id, is_bot=is_bot, full_name="测试成员")
        )
        self.answers = []
        self.answer_failure = answer_failure
        #: 回执时刻的事务探针（回执也是网络：不许占着读连接发 alert）。
        self.tx_probe = None
        self.transaction_state_at_answer = []

    @property
    def alerts(self):
        return [text for text, _ in self.answers if text]

    async def answer(self, text=None, show_alert=False, **kwargs):
        if self.tx_probe is not None:
            self.transaction_state_at_answer.append(bool(self.tx_probe()))
        if self.answer_failure is not None:
            raise self.answer_failure
        self.answers.append((text, show_alert))
        return True


def _next_callback_data(message: _FakeMessage) -> str:
    """首屏键盘里最后一个按钮的 callback_data（单页时没有键盘）。"""

    keyboard = message.last_reply_markup
    if keyboard is None:
        raise AssertionError("这一页没有翻页键盘")
    return keyboard.inline_keyboard[0][-1].callback_data


def _token_of(message: _FakeMessage) -> str:
    data = _next_callback_data(message)
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "phs":
        raise AssertionError(f"callback_data 形状不对：{data!r}")
    return parts[1]


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._ref_counter = 0
        # 每个测试用全新的有界会话表，避免跨测试串状态。
        commands._POINTS_HISTORY_STORE = ph.PointsHistorySessionStore(
            ttl_seconds=commands._POINTS_HISTORY_TTL_SECONDS,
            max_sessions=commands._POINTS_HISTORY_MAX_SESSIONS,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # ------------------------------------------------------------------ 工具

    def _frozen_clock(self, moment: datetime = NOW) -> ExitStack:
        """冻结产品代码里的「现在」。

        ``points_history``、``commands``、``checkin`` 都是
        ``from bot.utils.timezone import now_shanghai_naive`` 的直接名字绑定，
        所以要分别替换各自的模块属性（patch ``bot.utils.timezone`` 对它们无效）。
        """

        stack = ExitStack()
        stack.enter_context(patch.object(ph, "now_shanghai_naive", lambda: moment))
        stack.enter_context(patch.object(commands, "now_shanghai_naive", lambda: moment))
        stack.enter_context(
            patch.object(checkin_service, "now_shanghai_naive", lambda: moment)
        )
        return stack

    def _ref(self, prefix: str = "ref") -> str:
        self._ref_counter += 1
        return f"{prefix}-{self._ref_counter}"

    @asynccontextmanager
    async def _session(self):
        async with self.session_factory() as session:
            yield session

    async def _authorize(self, group_id: int = GROUP, *, bot_present: bool = True) -> None:
        async with self.session_factory() as session:
            session.add(
                AuthorizedGroup(
                    group_id=group_id, authorized_by=1, bot_present=bot_present
                )
            )
            await session.commit()

    async def _set_authorized(
        self, group_id: int = GROUP, *, bot_present: bool
    ) -> None:
        """真实持久化地改变授权状态（不是 mock 常量）。"""

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(AuthorizedGroup).where(
                        AuthorizedGroup.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    AuthorizedGroup(
                        group_id=group_id, authorized_by=1, bot_present=bot_present
                    )
                )
            else:
                row.bot_present = bot_present
            await session.commit()

    async def _checkin(
        self,
        at: datetime,
        *,
        points: int = 1,
        group_id: int = GROUP,
        user_id: int = USER,
        display_name: str = "测试成员",
    ) -> int:
        """直接写真实表（``created_at`` 显式赋值，与真实 writer 同口径）。

        注意 ``member_checkins`` 有 ``(group_id, user_id, checkin_date)`` 唯一索引：
        同一个人同一天只能有一笔。返回插入行的 id。
        """

        async with self.session_factory() as session:
            row = MemberCheckin(
                group_id=group_id,
                user_id=user_id,
                checkin_date=at.date().isoformat(),
                points=points,
                display_name=display_name,
                created_at=at,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
            await session.commit()
        return new_id

    async def _award(
        self,
        at: datetime,
        *,
        points: int = 1,
        reason: str = AWARD_REASON_WEEKLY_ACTIVITY,
        ref: str | None = None,
        group_id: int = GROUP,
        user_id: int = USER,
    ) -> int:
        async with self.session_factory() as session:
            row = MemberPointAward(
                group_id=group_id,
                user_id=user_id,
                points=points,
                reason=reason,
                ref=ref or self._ref("award"),
                created_at=at,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
            await session.commit()
        return new_id

    async def _spend(
        self,
        at: datetime,
        *,
        points: int = 2,
        reason: str = SPEND_REASON_PIN,
        ref: str | None = None,
        group_id: int = GROUP,
        user_id: int = USER,
    ) -> int:
        async with self.session_factory() as session:
            row = MemberPointSpend(
                group_id=group_id,
                user_id=user_id,
                points=points,
                reason=reason,
                ref=ref or self._ref("spend"),
                created_at=at,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
            await session.commit()
        return new_id

    async def _snapshot(self, *, page: int = 0, user_id: int = USER, group_id: int = GROUP):
        async with self.session_factory() as session:
            async with ph.consistent_read(session):
                return await ph.open_history(
                    session,
                    group_id=group_id,
                    user_id=user_id,
                    now=NOW,
                    page=page,
                )

    async def _full_snapshot(
        self, *, user_id: int = USER, group_id: int = GROUP, now: datetime = NOW
    ):
        async with self.session_factory() as session:
            return await ph.build_history_snapshot(
                session, group_id=group_id, user_id=user_id, now=now
            )

    async def _ledger_digest(self) -> str:
        """三本台账全部字节的摘要：只读性证明用。"""

        digest = hashlib.sha256()
        async with self.session_factory() as session:
            for model in (MemberCheckin, MemberPointSpend, MemberPointAward):
                rows = (
                    (await session.execute(select(model).order_by(model.id)))
                    .scalars()
                    .all()
                )
                for row in rows:
                    values = tuple(
                        getattr(row, column.name)
                        for column in model.__table__.columns
                    )
                    digest.update(repr(values).encode("utf-8"))
                    digest.update(b"\x00")
        return digest.hexdigest()

    def _message(self, text: str = "/points", **kwargs) -> _FakeMessage:
        return _FakeMessage(text, **kwargs)

    async def _run_command(self, message: _FakeMessage, *, authorized: bool = True):
        """跑真实 ``cmd_points``（不 patch ``_answer``）。"""

        patches = [
            patch.object(
                commands, "ensure_group_authorized", AsyncMock(return_value=authorized)
            )
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self._frozen_clock():
                async with self.session_factory() as session:
                    if message.session_probe is None:
                        message.session_probe = session.in_transaction
                    await commands.cmd_points(message, session, _settings())
        return message


# ---------------------------------------------------------------------------
# 窗口与边界
# ---------------------------------------------------------------------------


class WindowTests(_DbTestCase):
    def test_window_is_thirty_natural_days_ending_today(self) -> None:
        window = ph.resolve_window(NOW)
        self.assertEqual(window.today, NOW.date())
        self.assertEqual(window.start, datetime(2026, 8, 31, 0, 0, 0))
        self.assertEqual(window.until, NOW)
        span = (window.today - window.start.date()).days
        self.assertEqual(span, ph.HISTORY_WINDOW_DAYS - 1)

    def test_aware_now_is_normalized_to_shanghai(self) -> None:
        utc = datetime(2026, 9, 29, 4, 0, 0, tzinfo=timezone.utc)
        window = ph.resolve_window(utc)
        self.assertEqual(window.until, datetime(2026, 9, 29, 12, 0, 0))
        self.assertEqual(window.today, datetime(2026, 9, 29).date())

    async def test_naive_now_is_interpreted_as_local_time(self) -> None:
        """朴素时刻按本地时间解释（与 writer 的 ``now_shanghai_naive`` 同口径）。"""

        await self._award(datetime(2026, 9, 29, 11, 0, 0), points=5)
        naive = await self._full_snapshot(now=datetime(2026, 9, 29, 12, 0, 0))
        aware = await self._full_snapshot(
            now=datetime(2026, 9, 29, 4, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(naive.entries, aware.entries)
        self.assertEqual(naive.summary, aware.summary)
        # 反向控制：若把朴素时刻当 UTC（= 本地 20:00）就会多算 8 小时窗口。
        self.assertEqual(naive.window.until, aware.window.until)

    async def test_boundaries_include_day30_and_exclude_day31_and_future(self) -> None:
        inside_edge = ph.resolve_window(NOW).start  # 第 30 天 00:00
        day31 = inside_edge - timedelta(seconds=1)
        future = NOW + timedelta(seconds=1)

        await self._award(inside_edge, points=1)  # 起日 00:00：包含
        await self._award(day31, points=100)  # 第 31 天：排除
        await self._award(future, points=1000)  # 未来：排除
        await self._award(NOW, points=2)  # 查询时刻本身：包含

        snapshot = await self._snapshot()
        self.assertEqual(snapshot.summary.income, 3)
        self.assertEqual(snapshot.summary.count, 2)
        self.assertEqual(snapshot.summary.net, 3)

        # 完整读取（诊断路径）必须给出同一集合。
        full = await self._full_snapshot()
        self.assertEqual(full.summary, snapshot.summary)
        self.assertEqual(
            [entry.points for entry in full.entries], [2, 1]
        )

    def test_legacy_row_without_created_at_falls_back_to_checkin_date(self) -> None:
        window = ph.resolve_window(NOW)
        # 当前 schema 不允许 NULL created_at：这条兼容路径留给更早的库结构，
        # 所以先把这个前提钉死，再用纯函数直接验证退化的渲染口径。
        self.assertFalse(MemberCheckin.__table__.c.created_at.nullable)

        row = SimpleNamespace(id=7, points=3, checkin_date="2026-09-20")
        entry = ph.legacy_checkin_entry(row, window=window)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(entry.date_only)
        self.assertEqual(entry.at, datetime(2026, 9, 20, 0, 0, 0))
        self.assertEqual(entry.points, 3)
        self.assertIn("（仅日期）", ph.render_entry_line(entry))

        # 坏数据与窗口外的行都不猜。
        self.assertIsNone(
            ph.legacy_checkin_entry(
                SimpleNamespace(id=8, points=3, checkin_date="not-a-date"), window=window
            )
        )
        self.assertIsNone(
            ph.legacy_checkin_entry(
                SimpleNamespace(id=9, points=3, checkin_date="2026-08-30"), window=window
            )
        )


# ---------------------------------------------------------------------------
# 一致读（F01）
# ---------------------------------------------------------------------------


class SnapshotCoherenceTests(_DbTestCase):
    async def test_full_read_is_single_generation_under_interleaved_writes(self) -> None:
        """F01：签到读完之后有独立事务提交新账，整张快照必须仍是同一代。"""

        await self._checkin(_day(0) - timedelta(hours=2), points=1)
        await self._award(_day(0) - timedelta(hours=1), points=9)
        baseline = await self._snapshot()
        self.assertEqual(baseline.summary.income, 10)
        self.assertEqual(baseline.summary.count, 2)

        original = ph._load_award_entries
        injected = {"done": False}

        async def interleaving(session, *, group_id, user_id, window):
            result = await original(
                session, group_id=group_id, user_id=user_id, window=window
            )
            if not injected["done"]:
                injected["done"] = True
                async with self.session_factory() as other:
                    other.add(
                        MemberCheckin(
                            group_id=GROUP,
                            user_id=USER,
                            checkin_date=_day(-1).date().isoformat(),
                            points=3,
                            created_at=_day(-1),
                        )
                    )
                    other.add(
                        MemberPointAward(
                            group_id=GROUP,
                            user_id=USER,
                            points=7,
                            reason=AWARD_REASON_REFUND,
                            ref="late-award",
                            created_at=_day(0),
                        )
                    )
                    other.add(
                        MemberPointSpend(
                            group_id=GROUP,
                            user_id=USER,
                            points=2,
                            reason=SPEND_REASON_PIN,
                            ref="late-spend",
                            created_at=_day(0),
                        )
                    )
                    await other.commit()
            return result

        with patch.object(ph, "_load_award_entries", interleaving):
            mixed = await self._full_snapshot()

        self.assertTrue(injected["done"])
        # 10（同一代）而不是 15（签到世代 + 奖励世代的混合）。
        self.assertEqual(mixed.summary.income, 10)
        self.assertEqual(mixed.summary.spend, 0)
        self.assertEqual(mixed.summary.net, 10)
        self.assertEqual(mixed.summary.count, 2)
        self.assertEqual(len(mixed.entries), 2)

        # 控制组：新的一次查询看到完整的一代。
        after = await self._full_snapshot()
        self.assertEqual(after.summary.income, 20)
        self.assertEqual(after.summary.spend, 2)
        self.assertEqual(after.summary.net, 18)
        # 4 笔原有 + 1 笔后插入的签到 = 5 笔。
        self.assertEqual(after.summary.count, 5)

    async def test_command_card_is_single_generation_under_interleaved_writes(
        self,
    ) -> None:
        """F01：命令层卡片（余额头部 + 摘要 + 当前页）也必须同代。"""

        await self._authorize()
        await self._checkin(_day(0) - timedelta(hours=2), points=1)
        await self._award(_day(0) - timedelta(hours=1), points=9)

        original = ph.read_totals
        injected = {"done": False}

        async def injecting_totals(session, **kwargs):
            if not injected["done"]:
                injected["done"] = True
                async with self.session_factory() as other:
                    other.add(
                        MemberCheckin(
                            group_id=GROUP,
                            user_id=USER,
                            checkin_date=_day(-1).date().isoformat(),
                            points=3,
                            created_at=_day(-1),
                        )
                    )
                    other.add(
                        MemberPointAward(
                            group_id=GROUP,
                            user_id=USER,
                            points=7,
                            reason=AWARD_REASON_REFUND,
                            ref="late-commander-award",
                            created_at=_day(0),
                        )
                    )
                    other.add(
                        MemberPointSpend(
                            group_id=GROUP,
                            user_id=USER,
                            points=2,
                            reason=SPEND_REASON_PIN,
                            ref="late-commander-spend",
                            created_at=_day(0),
                        )
                    )
                    await other.commit()
            return await original(session, **kwargs)

        message = self._message()
        with patch.object(ph, "read_totals", injecting_totals):
            await self._run_command(message)

        body = message.last_answer
        self.assertTrue(injected["done"])
        # 余额头部与 30 天摘要都来自新账之前的那一代。
        self.assertIn("可用 <b>10</b> 分", body)
        self.assertIn("收入 <b>+10</b> 分｜支出 <b>0</b> 分｜净变动 <b>+10</b> 分｜共 2 笔", body)
        # 新账一笔都不许混进来。注意不能裸查 "-2"：时间戳里的 "09-29" 会误伤，
        # 所以按渲染出来的「signed 数字 + 分」比对。
        for leaked in ("+7 分", "+3 分", "-2 分"):
            self.assertNotIn(leaked, body)
        self.assertEqual(body.count("分 · "), 2)

        # 控制组：再查一次就看到新一代。
        second = self._message()
        await self._run_command(second)
        self.assertIn("可用 <b>18</b> 分", second.last_answer)
        self.assertIn("共 5 笔", second.last_answer)

    async def test_midnight_crossing_uses_one_frozen_clock(self) -> None:
        """跨午夜两个方向：窗口跟着冻结时刻走，边界不重不漏。"""

        await self._award(datetime(2026, 8, 31, 23, 59), points=100)
        await self._award(datetime(2026, 9, 29, 23, 59, 58), points=5)
        await self._award(datetime(2026, 9, 29, 23, 59, 59), points=7)

        before = await self._full_snapshot(now=datetime(2026, 9, 29, 23, 59, 59))
        after = await self._full_snapshot(now=datetime(2026, 9, 30, 0, 0, 0))

        self.assertEqual(before.window.start, datetime(2026, 8, 31, 0, 0, 0))
        self.assertEqual(before.summary.income, 112)
        self.assertEqual(before.summary.count, 3)

        self.assertEqual(after.window.start, datetime(2026, 9, 1, 0, 0, 0))
        self.assertEqual(after.summary.income, 12)
        self.assertEqual(after.summary.count, 2)

        # 同一个冻结时刻既算窗口也算「今天」：23:59:59 那笔属于当天。
        self.assertEqual(before.window.today, datetime(2026, 9, 29).date())
        self.assertEqual(after.window.today, datetime(2026, 9, 30).date())

    async def test_full_read_matches_the_paginated_sql_path(self) -> None:
        """两条读取路径（完整读取 vs SQL 分页）必须完全等价。"""

        for index in range(45):
            await self._award(
                _day(-(index % 29)) - timedelta(minutes=index + 1),
                points=1 + index % 3,
                ref=self._ref("equiv"),
            )
        await self._spend(_day(-3), points=4, ref=self._ref("equiv-spend"))
        await self._checkin(_day(-4), points=2)

        full = await self._full_snapshot()
        expected = [ph.render_entry_line(entry) for entry in full.entries]

        collected = []
        for page in range(ph.page_count_for(full.summary.count)):
            snapshot = await self._snapshot(page=page)
            self.assertEqual(snapshot.summary, full.summary)
            collected.extend(ph.render_entry_line(entry) for entry in snapshot.entries)

        self.assertEqual(collected, expected)
        self.assertEqual(len(collected), full.summary.count)


# ---------------------------------------------------------------------------
# 三本账与摘要
# ---------------------------------------------------------------------------


class LedgerTests(_DbTestCase):
    async def test_three_ledgers_summary_and_signed_entries(self) -> None:
        await self._checkin(_day(-2), points=2)
        await self._award(_day(-1), points=9, reason=AWARD_REASON_WEEKLY_ACTIVITY)
        await self._award(_day(-1) + timedelta(minutes=1), points=6, reason=AWARD_REASON_LOTTERY)
        await self._spend(_day(0), points=5, reason=SPEND_REASON_LOTTERY)

        snapshot = await self._snapshot()
        self.assertEqual(
            snapshot.summary, ph.PointsHistorySummary(income=17, spend=5, net=12, count=4)
        )
        by_source = {entry.source: entry for entry in snapshot.entries}
        self.assertEqual(set(by_source), {"checkin", "award", "spend"})
        self.assertEqual(by_source["checkin"].points, 2)
        self.assertEqual(by_source["spend"].points, -5)
        self.assertIn("+2 分", ph.render_entry_line(by_source["checkin"]))
        self.assertIn("-5 分", ph.render_entry_line(by_source["spend"]))

    async def test_net_change_is_not_the_current_balance(self) -> None:
        """30 天净变化只是窗口内收支；窗口外的历史不能被说成「当前余额」。"""

        await self._award(_day(-90), points=500)  # 窗口外：仍算余额，不进摘要
        await self._award(_day(-1), points=4)
        await self._spend(_day(0), points=1)

        snapshot = await self._snapshot()
        self.assertEqual(snapshot.summary.net, 3)
        self.assertEqual(snapshot.summary.count, 2)

        async with self.session_factory() as session:
            outcome = await checkin_service.summarize(
                session, group_id=GROUP, user_id=USER, now=NOW
            )
        self.assertEqual(outcome.available_points, 503)
        self.assertNotEqual(snapshot.summary.net, outcome.available_points)

    async def test_refund_and_original_spend_are_separate_rows(self) -> None:
        await self._spend(_day(-1), points=30, reason=SPEND_REASON_TAG)
        await self._award(_day(0), points=30, reason=AWARD_REASON_REFUND)

        snapshot = await self._snapshot()
        self.assertEqual(snapshot.summary.income, 30)
        self.assertEqual(snapshot.summary.spend, 30)
        self.assertEqual(snapshot.summary.net, 0)
        self.assertEqual(snapshot.summary.count, 2)
        labels = sorted(entry.label for entry in snapshot.entries)
        self.assertEqual(labels, ["商店退款", "购买自定义头衔"])

    async def test_same_timestamp_orders_by_source_then_id_desc(self) -> None:
        at = _day(-1)
        checkin_id = await self._checkin(at, points=1)
        first_award = await self._award(at, points=2, ref="order-a")
        second_award = await self._award(at, points=3, ref="order-b")
        spend_id = await self._spend(at, points=1, ref="order-s")

        snapshot = await self._snapshot()
        self.assertEqual(
            [(entry.source, entry.points) for entry in snapshot.entries],
            [("checkin", 1), ("award", 3), ("award", 2), ("spend", -1)],
        )
        # 同刻三表的稳定顺序：source（签到 → 奖励 → 消费），同表内 id 倒序。
        # entry_id 是表内 id（三表会撞号），所以比对必须带上 source。
        expected_keys = [
            ("checkin", checkin_id),
            ("award", second_award),
            ("award", first_award),
            ("spend", spend_id),
        ]
        self.assertEqual(
            [(entry.source, entry.entry_id) for entry in snapshot.entries], expected_keys
        )
        self.assertLess(first_award, second_award)

        # 同一页里每笔只出现一次，且顺序稳定可复现。
        again = await self._snapshot()
        self.assertEqual(
            [(entry.source, entry.entry_id) for entry in again.entries], expected_keys
        )

    async def test_high_water_freeze_blocks_new_rows_during_paging(self) -> None:
        for index in range(25):
            await self._award(
                _day(-1) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("water"),
            )
        frozen = await self._snapshot(page=1)
        self.assertEqual(frozen.summary.count, 25)
        self.assertEqual(len(frozen.entries), 5)
        self.assertEqual(frozen.page, 1)

        for index in range(10):
            await self._award(NOW - timedelta(seconds=index + 1), points=99, ref=self._ref("late"))

        async with self.session_factory() as session:
            async with ph.consistent_read(session):
                page = await ph.load_history_page(
                    session,
                    group_id=GROUP,
                    user_id=USER,
                    window=frozen.window,
                    high_water=frozen.high_water,
                    page=0,
                )
        # 冻结水位把新账挡在外面：同一查询的摘要与页边界都不变。
        self.assertEqual(page.summary, frozen.summary)
        self.assertEqual(len(page.entries), ph.MAX_PAGE_ENTRIES)
        self.assertTrue(all(entry.points == 1 for entry in page.entries))

        fresh = await self._snapshot()
        self.assertEqual(fresh.summary.count, 35)

    def test_summary_uses_clean_signs(self) -> None:
        entry_counts = ph.PointsHistorySummary(income=0, spend=0, net=0, count=0)
        snapshot = ph.PointsHistorySnapshot(
            window=ph.resolve_window(NOW),
            entries=(),
            summary=entry_counts,
            high_water=(),
        )
        header = ph.render_history_header(snapshot)
        summary_line = header.splitlines()[-1]
        self.assertIn("收入 <b>0</b> 分", summary_line)
        self.assertIn("支出 <b>0</b> 分", summary_line)
        self.assertIn("净变动 <b>0</b> 分", summary_line)
        self.assertNotIn("+0", summary_line)
        self.assertNotIn("-0", summary_line)

        positive = ph.render_history_header(
            ph.PointsHistorySnapshot(
                window=ph.resolve_window(NOW),
                entries=(),
                summary=ph.PointsHistorySummary(income=7, spend=3, net=4, count=2),
                high_water=(),
            )
        )
        self.assertIn("收入 <b>+7</b> 分｜支出 <b>-3</b> 分｜净变动 <b>+4</b> 分｜共 2 笔", positive)
        self.assertEqual(ph._signed(0), "0")
        self.assertEqual(ph._signed(-3), "-3")


class IsolationTests(_DbTestCase):
    async def test_other_user_and_other_group_rows_are_invisible(self) -> None:
        await self._award(_day(-1), points=5, user_id=USER, group_id=GROUP)
        await self._award(_day(-1), points=1000, user_id=OTHER_USER, group_id=GROUP)
        await self._award(_day(-1), points=2000, user_id=USER, group_id=OTHER_GROUP)
        await self._spend(_day(0), points=3000, user_id=OTHER_USER, group_id=OTHER_GROUP)

        snapshot = await self._snapshot()
        self.assertEqual(snapshot.summary.income, 5)
        self.assertEqual(snapshot.summary.spend, 0)
        self.assertEqual(snapshot.summary.count, 1)

        waters = ph.high_water_map(snapshot.high_water)
        other = await self._snapshot(user_id=OTHER_USER, group_id=OTHER_GROUP)
        other_waters = ph.high_water_map(other.high_water)
        # 高水位也是按 (群, 人) 各自的，不会串。
        self.assertNotEqual(waters["award"], other_waters["award"])
        self.assertEqual(other.summary.spend, 3000)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


class RenderTests(_DbTestCase):
    async def test_empty_history_still_shows_summary_and_old_balance(self) -> None:
        await self._authorize()
        await self._award(_day(-200), points=42)  # 只在余额里
        message = self._message()
        await self._run_command(message)

        body = message.last_answer
        self.assertIn("可用 <b>42</b> 分", body)
        self.assertIn("共 0 笔", body)
        self.assertIn(ph.EMPTY_HISTORY_TEXT, body)
        # 摘要在折叠区外，正文里仍然有一个 blockquote（不折叠）。
        self.assertIn("<blockquote>", body)
        self.assertLess(body.index("共 0 笔"), body.index("<blockquote>"))
        self.assertIsNone(message.last_reply_markup)
        self.assertNotIn("第 1 / 1 页", body)

    async def test_short_history_is_not_collapsed_above_five_is(self) -> None:
        snapshot_five = ph.PointsHistorySnapshot(
            window=ph.resolve_window(NOW),
            entries=(),
            summary=ph.PointsHistorySummary(income=5, spend=0, net=5, count=5),
            high_water=(),
        )
        record = ph.freeze_history(snapshot=snapshot_five, head="<b>我的积分</b>")
        self.assertFalse(record.is_collapsed())
        rendered = record.render(0, ["a", "b", "c", "d", "e"])
        self.assertIn("<blockquote>", rendered)
        self.assertNotIn("expandable", rendered)

        snapshot_six = ph.PointsHistorySnapshot(
            window=ph.resolve_window(NOW),
            entries=(),
            summary=ph.PointsHistorySummary(income=6, spend=0, net=6, count=6),
            high_water=(),
        )
        record_six = ph.freeze_history(snapshot=snapshot_six, head="<b>我的积分</b>")
        self.assertTrue(record_six.is_collapsed())
        rendered_six = record_six.render(0, ["a", "b", "c", "d", "e", "f"])
        self.assertIn("<blockquote expandable>", rendered_six)
        self.assertEqual(rendered_six.count("<blockquote"), 1)
        self.assertEqual(rendered_six.count("</blockquote>"), 1)

    async def test_summary_stays_outside_the_blockquote(self) -> None:
        await self._authorize()
        for index in range(6):
            await self._award(
                _day(-1) - timedelta(minutes=index + 1),
                points=2,
                ref=self._ref("outside"),
            )
        message = self._message()
        await self._run_command(message)
        body = message.last_answer
        head, quote = body.split("<blockquote expandable>", 1)
        self.assertIn("近 30 天流水", head)
        self.assertIn("收入 <b>+12</b> 分", head)
        self.assertNotIn("近 30 天流水", quote)
        self.assertTrue(quote.endswith("</blockquote>"))

    async def test_unknown_reason_never_echoes_internal_values(self) -> None:
        await self._award(
            _day(-1),
            points=3,
            reason="internal-secret-reason",
            ref="secret-ref-700000001",
        )
        await self._spend(_day(0), points=1, reason="", ref="another-secret-ref")

        snapshot = await self._snapshot()
        lines = [ph.render_entry_line(entry) for entry in snapshot.entries]
        joined = "\n".join(lines)
        self.assertIn("积分奖励", joined)
        self.assertIn("积分消费", joined)
        for leaked in (
            "internal-secret-reason",
            "secret-ref-700000001",
            "another-secret-ref",
        ):
            self.assertNotIn(leaked, joined)
        self.assertEqual(ph.label_for_reason("award", ""), "积分奖励")
        self.assertEqual(ph.label_for_reason("spend", "unknown"), "积分消费")

    async def test_reason_labels_match_the_real_writer_constants(self) -> None:
        expected = {
            ("award", AWARD_REASON_WEEKLY_ACTIVITY): "每周活跃奖励",
            ("award", AWARD_REASON_REFUND): "商店退款",
            ("award", AWARD_REASON_LOTTERY): "抽奖奖励",
            ("spend", SPEND_REASON_CHALLENGE): "免除质询扣分",
            ("spend", SPEND_REASON_TAG): "购买自定义头衔",
            ("spend", SPEND_REASON_PIN): "置顶求助",
            ("spend", SPEND_REASON_LOTTERY): "抽奖消耗",
        }
        for (source, reason), label in expected.items():
            self.assertEqual(ph.label_for_reason(source, reason), label, reason)
        # 真实常量本身也不能变（写错常量就是写错文案）。
        self.assertEqual(AWARD_REASON_WEEKLY_ACTIVITY, "weekly_activity")
        self.assertEqual(AWARD_REASON_REFUND, "shop_refund")
        self.assertEqual(AWARD_REASON_LOTTERY, "shop_lottery_prize")
        self.assertEqual(SPEND_REASON_CHALLENGE, "moderation_challenge")
        self.assertEqual(SPEND_REASON_TAG, "shop_tag")
        self.assertEqual(SPEND_REASON_PIN, "shop_pin")
        self.assertEqual(SPEND_REASON_LOTTERY, "shop_lottery")

    async def test_dynamic_text_is_html_escaped(self) -> None:
        entry = ph.PointsHistoryEntry(
            source="award",
            entry_id=1,
            at=NOW,
            date_only=False,
            points=1,
            label="<b>加粗</b> & <script>alert(1)</script>",
        )
        line = ph.render_entry_line(entry)
        self.assertNotIn("<b>", line)
        self.assertNotIn("<script>", line)
        self.assertIn("&lt;b&gt;", line)
        self.assertIn("&amp;", line)

    async def test_long_label_and_partial_entity_are_truncated_safely(self) -> None:
        long_label = "奖励" * 200
        entry = ph.PointsHistoryEntry(
            source="award",
            entry_id=1,
            at=NOW,
            date_only=False,
            points=5,
            label=long_label,
        )
        line = ph.render_entry_line(entry)
        self.assertLessEqual(ph._utf16_units(line), ph.MAX_ENTRY_LINE_UNITS)
        self.assertNotRegex(line, r"&[A-Za-z#0-9]{0,7}$")

        entity_label = "&" * 200
        entity_line = ph.render_entry_line(
            ph.PointsHistoryEntry(
                source="award",
                entry_id=2,
                at=NOW,
                date_only=False,
                points=5,
                label=entity_label,
            )
        )
        self.assertNotRegex(entity_line, r"&[A-Za-z#0-9]{0,7}$")
        self.assertLessEqual(ph._utf16_units(entity_line), ph.MAX_ENTRY_LINE_UNITS)

        emoji_label = "🥇" * 80
        emoji_line = ph.render_entry_line(
            ph.PointsHistoryEntry(
                source="award",
                entry_id=3,
                at=NOW,
                date_only=False,
                points=5,
                label=emoji_label,
            )
        )
        self.assertLessEqual(ph._utf16_units(emoji_line), ph.MAX_ENTRY_LINE_UNITS)
        self.assertEqual(
            ph._utf16_units("🥇"), len("🥇".encode("utf-16-le")) // 2
        )

    async def test_frozen_state_is_bounded_and_holds_no_page_text(self) -> None:
        """F02：会话里不保存页面文本 / 完整历史，状态量与流水条数无关。"""

        for index in range(120):
            await self._award(
                _day(-(index % 29)) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("bounded"),
            )
        snapshot = await self._snapshot()
        self.assertEqual(snapshot.summary.count, 120)
        # 生产读取只拿当前页，绝不 fetchall 全历史。
        self.assertEqual(len(snapshot.entries), ph.MAX_PAGE_ENTRIES)

        record = ph.freeze_history(snapshot=snapshot, head="<b>我的积分</b>")
        self.assertEqual(record.page_bodies, ())
        self.assertEqual(record.entry_count(0), 0)
        self.assertEqual(record.page_item_count(0), ph.MAX_PAGE_ENTRIES)
        self.assertEqual(record.page_item_count(5), 20)
        self.assertEqual(record.page_count, 6)
        self.assertLess(record.bounded_state_units(), ph.MAX_STATE_UNITS)

        small = ph.PointsHistorySnapshot(
            window=snapshot.window,
            entries=(),
            summary=ph.PointsHistorySummary(income=3, spend=0, net=3, count=3),
            high_water=(),
        )
        small_record = ph.freeze_history(snapshot=small, head="<b>我的积分</b>")
        # 状态量只跟固定元信息有关：3 笔和 120 笔的会话大小只差摘要里的位数。
        self.assertLessEqual(
            abs(record.bounded_state_units() - small_record.bounded_state_units()), 8
        )
        # 极端对照：100 万笔（5 万页）的会话描述符依然远低于上限——状态量不随 N 增长。
        huge = ph.freeze_history(
            snapshot=ph.PointsHistorySnapshot(
                window=snapshot.window,
                entries=(),
                summary=ph.PointsHistorySummary(
                    income=10**6, spend=0, net=10**6, count=10**6
                ),
                high_water=(),
            ),
            head="<b>我的积分</b>",
        )
        self.assertEqual(huge.page_count, 50_000)
        self.assertLess(huge.bounded_state_units(), ph.MAX_STATE_UNITS)
        self.assertIn("LIMIT", ph._PAGE_SQL.text)
        self.assertIn("OFFSET", ph._PAGE_SQL.text)


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


class PaginationTests(_DbTestCase):
    async def test_twenty_entries_fit_one_page_and_twentyone_make_two(self) -> None:
        for index in range(20):
            await self._award(
                _day(-1) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("twenty"),
            )
        one = await self._snapshot()
        self.assertEqual(one.summary.count, 20)
        self.assertEqual(len(one.entries), 20)
        self.assertEqual(ph.page_count_for(one.summary.count), 1)
        self.assertEqual(one.page, 0)

        # 反例测试里被夹到合法范围的越界页：第 5 页仍然落在第 1 页（20 笔只有 1 页）。
        clamped = await self._snapshot(page=4)
        self.assertEqual(clamped.page, 0)
        self.assertEqual(len(clamped.entries), 20)

        await self._award(_day(0), points=1, ref=self._ref("twenty-one"))
        two = await self._snapshot()
        self.assertEqual(two.summary.count, 21)
        self.assertEqual(ph.page_count_for(two.summary.count), 2)
        self.assertEqual(len(two.entries), 20)
        second = await self._snapshot(page=1)
        self.assertEqual(second.page, 1)
        self.assertEqual(len(second.entries), 1)
        # 越界页夹回最后一页。
        self.assertEqual((await self._snapshot(page=99)).page, 1)
        self.assertEqual((await self._snapshot(page=-3)).page, 0)

    async def test_every_page_matches_the_full_read_exactly(self) -> None:
        for index in range(53):
            await self._award(
                _day(-(index % 29)) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("exact"),
            )
        await self._spend(_day(-2), points=7, ref=self._ref("exact-spend"))

        full = await self._full_snapshot()
        defined = [(entry.source, entry.entry_id, entry.points) for entry in full.entries]
        seen: list[tuple[str, int, int]] = []
        for page in range(ph.page_count_for(full.summary.count)):
            snapshot = await self._snapshot(page=page)
            seen.extend(
                (entry.source, entry.entry_id, entry.points)
                for entry in snapshot.entries
            )
        self.assertEqual(len(seen), full.summary.count)
        # entry_id 只是表内 id：三本账之间会撞号，所以唯一性必须带上 source。
        self.assertEqual(len(set(seen)), full.summary.count)
        self.assertEqual(seen, defined)

    async def test_command_page_argument_is_friendly(self) -> None:
        self.assertIsNone(commands._points_page_argument(self._message("/points")))
        self.assertEqual(commands._points_page_argument(self._message("/points 2")), 1)
        self.assertEqual(commands._points_page_argument(self._message("/points  3 ")), 2)
        self.assertIsNone(commands._points_page_argument(self._message("/points abc")))
        self.assertIsNone(commands._points_page_argument(self._message("/points@bot")))
        self.assertEqual(commands._points_page_argument(self._message("/points@bot 4")), 3)
        # 多写了参数只取第一段（不报错、不把整串塞给 int）。
        self.assertEqual(commands._points_page_argument(self._message("/points 2 3")), 1)
        # 负数与垃圾参数由 clamp 统一兜住。
        self.assertEqual(ph.clamp_page(-1, 0), 0)
        self.assertEqual(ph.clamp_page(1000, 5), 0)
        self.assertEqual(ph.clamp_page(1000, 41), 2)
        self.assertEqual(ph.clamp_page("x", 41), 0)

    async def test_large_ledger_pages_stay_within_the_safe_length(self) -> None:
        await self._authorize()
        for index in range(120):
            await self._award(
                _day(-(index % 29)) - timedelta(minutes=index + 1),
                points=1 + index % 9,
                ref=self._ref("long"),
            )
        message = self._message("/points 4")
        await self._run_command(message)
        body = message.last_answer
        self.assertIn("<blockquote expandable>", body)
        self.assertIn("第 4 / 6 页", body)
        self.assertLessEqual(ph._utf16_units(body), ph.MESSAGE_SAFE_UNITS)
        self.assertLessEqual(ph._utf16_units(body), 4096)
        self.assertIsNotNone(message.last_reply_markup)


# ---------------------------------------------------------------------------
# 命令层
# ---------------------------------------------------------------------------


class PointsCommandTests(_DbTestCase):
    async def test_command_keeps_existing_balance_lines_and_adds_folded_history(
        self,
    ) -> None:
        await self._authorize()
        await self._checkin(_day(-1), points=1)
        await self._checkin(NOW - timedelta(hours=1), points=1)
        for index in range(4):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=2,
                ref=self._ref("fold"),
            )
        await self._spend(_day(0), points=2, reason=SPEND_REASON_LOTTERY)

        message = self._message()
        await self._run_command(message)
        body = message.last_answer

        # 首轮已有的子串一条都不能少（不得回归）。
        self.assertIn("<b>我的积分</b>", body)
        self.assertIn("可用 <b>", body)
        self.assertIn(" 分｜累计获得 ", body)
        self.assertIn("连续 ", body)
        self.assertIn("｜共签到 ", body)
        self.assertIn("今日已签到", body)
        self.assertIn("广告质询时可用 ", body)
        # 新增：30 天摘要 + 折叠流水。
        self.assertIn("近 30 天流水", body)
        self.assertIn("<blockquote expandable>", body)
        self.assertIn("中国时间", body)
        self.assertEqual(body.count("<blockquote"), 1)

    async def test_command_delivers_via_real_chain_with_expandable_blockquote(
        self,
    ) -> None:
        """折叠实体必须真的走到 ``message.answer``，而不是只在字符串里存在。"""

        await self._authorize()
        for index in range(6):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("chain"),
            )
        message = self._message()
        observed = {}
        original = commands.answer_with_auto_delete

        async def spy(msg, text, **kwargs):
            observed["text"] = text
            observed["kwargs"] = kwargs
            return await original(msg, text, **kwargs)

        with patch.object(commands, "answer_with_auto_delete", spy):
            await self._run_command(message)

        self.assertIn("blockquote expandable", observed["text"])
        # 含 <blockquote 的正文被 _render_action_response 原样透传，不会被再包一层
        # legacy action notice（否则摘要会被塞进引用块，违反「摘要在外」）。
        self.assertEqual(
            commands._render_action_response(observed["text"]), observed["text"]
        )
        # 摘要不在折叠块里。
        head = observed["text"].split("<blockquote expandable>", 1)[0]
        self.assertIn("收入 <b>+6</b> 分", head)
        self.assertIn("净变动", head)
        # 只有一条消息被发出去（不刷屏），且正文就是最终送达的正文。
        self.assertEqual(len(message.answers), 1)
        self.assertEqual(message.answers[0][0], observed["text"])
        # 读连接已经交还：发送时刻不在事务里。
        self.assertEqual(message.transaction_state_at_answer, [False])

    async def test_command_rejects_bot_identity(self) -> None:
        """F06：机器人身份不查账、不回卡片，也不发任何群消息。"""

        await self._authorize()
        await self._award(_day(-1), points=5)
        message = self._message(is_bot=True)
        await self._run_command(message)
        self.assertEqual(message.answers, [])
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

        # 没有 from_user 时同样拒绝。
        anonymous = self._message(user_id=None)
        await self._run_command(anonymous)
        self.assertEqual(anonymous.answers, [])
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

    async def test_command_rejects_private_chat(self) -> None:
        await self._authorize()
        message = self._message(chat_type="private")
        await self._run_command(message)
        self.assertEqual(len(message.answers), 1)
        self.assertIn("仅可在群内使用", message.last_answer)
        self.assertNotIn("近 30 天流水", message.last_answer)

    async def test_command_rejects_unauthorized_group_without_history(self) -> None:
        # 没有 AuthorizedGroup 行：走真实 ensure_group_authorized。
        await self._award(_day(-1), points=5)
        message = self._message()
        with self._frozen_clock():
            async with self.session_factory() as session:
                await commands.cmd_points(message, session, _settings())
        self.assertEqual(len(message.answers), 1)
        body = message.last_answer
        self.assertIn("未授权", body)
        self.assertNotIn("近 30 天流水", body)
        self.assertNotIn("我的积分", body)
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

    async def test_command_second_page_argument_opens_that_page(self) -> None:
        await self._authorize()
        for index in range(30):
            await self._award(
                _day(-1) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("arg"),
            )
        message = self._message("/points 2")
        await self._run_command(message)
        body = message.last_answer
        self.assertIn("第 2 / 2 页", body)
        self.assertIn("收入 <b>+30</b> 分｜支出 <b>0</b> 分｜净变动 <b>+30</b> 分｜共 30 笔", body)
        # 第 2 页只有 10 笔。
        self.assertEqual(body.split("<blockquote expandable>", 1)[1].count("分 · "), 10)

        # 越界 / 垃圾参数友好处理：夹到合法页，不报错、不空回执。
        for raw in ("/points 99", "/points 0", "/points -1", "/points abc"):
            message = self._message(raw)
            await self._run_command(message)
            self.assertIn("第 ", message.last_answer)

    async def test_reads_do_not_change_any_ledger_bytes(self) -> None:
        await self._authorize()
        await self._checkin(_day(-1), points=1)
        await self._award(_day(-1), points=4, reason=AWARD_REASON_LOTTERY)
        await self._spend(_day(0), points=2, reason=SPEND_REASON_TAG)

        before = await self._ledger_digest()
        message = self._message()
        await self._run_command(message)
        # 第二页入口（不存在时跳过）也要读而不写。
        second = self._message("/points 2")
        await self._run_command(second)
        after = await self._ledger_digest()
        self.assertEqual(before, after)

        # 翻页回调同样只读（授权行已经在上面写过一次，不能重复插）。
        for index in range(25):
            await self._award(
                _day(-1) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("readonly"),
            )
        message = self._message()
        await self._run_command(message)
        token = _token_of(message)
        before = await self._ledger_digest()
        callback = _FakeCallback(message=message.card_view(), data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(await self._ledger_digest(), before)

    async def test_auto_delete_policy_is_unchanged(self) -> None:
        await self._authorize()
        await self._award(_day(-1), points=1)
        message = self._message()
        observed = {}
        original = commands.answer_with_auto_delete

        async def spy(msg, text, **kwargs):
            observed["kwargs"] = kwargs
            return await original(msg, text, **kwargs)

        with patch.object(commands, "answer_with_auto_delete", spy):
            await self._run_command(message)

        self.assertEqual(
            observed["kwargs"].get("auto_delete_seconds"),
            commands.configured_auto_delete_seconds(_settings(), "management"),
        )
        # /points 的清理约定与 /checkin 各自独立，谁都没被顺手改掉。
        self.assertEqual(commands.CHECKIN_COMMAND_DELETE_SECONDS, 2)
        self.assertEqual(commands.CHECKIN_RECEIPT_SECONDS, 5)
        self.assertLess(ph.MESSAGE_SAFE_UNITS, 4096)

    async def test_help_and_catalog_mention_the_history(self) -> None:
        from bot.utils.command_catalog import build_bot_commands, build_help_text

        help_text = build_help_text()
        self.assertIn("/points", help_text)
        self.assertIn("近 30 天", help_text)

        entries = dict(build_bot_commands())
        self.assertIn("points", entries)
        self.assertIn("近 30 天", entries["points"])
        # 命令菜单里的命令名不变（参数形式不进菜单）。
        self.assertNotIn("points [", " ".join(entries))


# ---------------------------------------------------------------------------
# 翻页回调
# ---------------------------------------------------------------------------


class CallbackTests(_DbTestCase):
    async def _paging_fixture(
        self, *, entries: int = 25, message_id: int = 101
    ) -> tuple[_FakeMessage, str]:
        """发一次真实的 /points（25 笔 → 2 页），返回消息与 token。"""

        await self._authorize()
        for index in range(entries):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("paging"),
            )
        message = self._message(message_id=message_id)
        await self._run_command(message)
        # 回调里看到的是**发送出去的那张卡**：同一个 message_id，共享 answers/edits。
        return message.card_view(), _token_of(message)

    async def test_callback_data_is_short_and_carries_no_identity(self) -> None:
        message, token = await self._paging_fixture()
        keyboard = message.last_reply_markup
        self.assertIsNotNone(keyboard)
        datas = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertTrue(datas)
        for data in datas:
            self.assertLessEqual(len(data.encode("utf-8")), 64)
            self.assertEqual(len(data.split(":")), 3)
            self.assertNotIn(str(USER), data)
            self.assertNotIn(str(OTHER_USER), data)
            self.assertNotIn(str(GROUP), data)
        self.assertEqual(len(token), 8)

    async def test_paging_edits_the_original_message_only(self) -> None:
        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.message_id, message.message_id)
        self.assertEqual(record.owner_user_id, USER)
        self.assertEqual(record.group_id, GROUP)

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)

        self.assertEqual(len(message.edits), 1)
        body, kwargs = message.edits[0]
        self.assertIn("第 2 / 2 页", body)
        self.assertIn("近 30 天流水", body)
        self.assertEqual(kwargs.get("disable_web_page_preview"), True)
        self.assertEqual(callback.answers[0][0], None)
        self.assertEqual(record.applied_page, 1)
        self.assertEqual(record.applied_seq, 1)
        # 回执只通过 callback.answer，绝不自动补发群消息。
        self.assertEqual(len(message.answers), 1)

    async def test_previous_page_button(self) -> None:
        message, token = await self._paging_fixture()
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        # 第 2 页出现「上一页」：callback_data 指回 0。
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        keyboard = commands._points_history_keyboard(record, page_index=1)
        datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"phs:{token}:0", datas)

        back = _FakeCallback(message=message, data=f"phs:{token}:0")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(back, _settings(), session)
        self.assertEqual(len(message.edits), 2)
        self.assertIn("第 1 / 2 页", message.edits[-1][0])
        self.assertEqual(record.applied_page, 0)
        self.assertEqual(record.applied_seq, 2)

    async def test_same_page_reclick_is_idempotent_and_silent(self) -> None:
        """重复点同一页不弹错误：真的再编辑一次（结果相同），回执静默。"""

        message, token = await self._paging_fixture()
        for _ in range(2):
            callback = _FakeCallback(message=message, data=f"phs:{token}:1")
            async with self.session_factory() as session:
                await commands.on_points_history_paging(callback, _settings(), session)
            self.assertEqual(callback.alerts, [])
        self.assertEqual(len(message.edits), 2)
        self.assertEqual(message.edits[0][0], message.edits[1][0])
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        self.assertEqual(record.applied_seq, 2)

    async def test_other_user_cannot_page(self) -> None:
        message, token = await self._paging_fixture()
        callback = _FakeCallback(
            message=message, data=f"phs:{token}:1", user_id=OTHER_USER
        )
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["只能查看自己的积分流水"])

    async def test_bot_clicker_is_rejected(self) -> None:
        message, token = await self._paging_fixture()
        callback = _FakeCallback(message=message, data=f"phs:{token}:1", is_bot=True)
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["消息已失效，请重新 /points"])

    async def test_cross_group_click_is_rejected(self) -> None:
        message, token = await self._paging_fixture()
        foreign = _FakeMessage(chat_id=OTHER_GROUP, message_id=message.message_id)
        callback = _FakeCallback(message=foreign, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(foreign.edits, [])
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["查询已失效，请重新 /points"])

    async def test_other_message_replay_is_rejected(self) -> None:
        """F04：同一个 token 配另一条消息（同群同人）也不能编辑。"""

        message, token = await self._paging_fixture()
        other = _FakeMessage(message_id=202)
        callback = _FakeCallback(message=other, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(other.edits, [])
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["查询已失效，请重新 /points"])

    async def test_missing_message_id_is_rejected(self) -> None:
        """F04：会话拿不到真实消息 id 时不能注册翻页状态。"""

        await self._authorize()
        for index in range(25):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("noid"),
            )
        message = self._message(sent_id=None)
        await self._run_command(message)
        # 消息照样发出去（只是键盘点了也没用），但绝不能注册可用状态。
        self.assertEqual(len(message.answers), 1)
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

    async def test_send_failure_leaves_no_usable_state(self) -> None:
        await self._authorize()
        for index in range(25):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("fail"),
            )
        message = self._message()
        message.answer_failure = RuntimeError("synthetic transport failure")
        with self.assertRaises(RuntimeError):
            await self._run_command(message)
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

    async def test_unknown_token_and_malformed_data(self) -> None:
        await self._authorize()
        message = self._message()
        for data, expected in (
            # 只有前缀（键盘不会这么发）也按「查不到这份状态」处理，给明确回执。
            ("phs:", "查询已失效，请重新 /points"),
            ("phs", "查询已失效，请重新 /points"),
            ("phs:deadbeef", "查询已失效，请重新 /points"),
            ("phs:deadbeef:1", "查询已过期，请重新 /points"),
            ("phs:deadbeef:1:2", "查询已失效，请重新 /points"),
            ("other:deadbeef:1", "查询已过期，请重新 /points"),
        ):
            callback = _FakeCallback(message=message, data=data)
            async with self.session_factory() as session:
                await commands.on_points_history_paging(callback, _settings(), session)
            if expected is None:
                self.assertEqual(callback.answers, [(None, False)])
            else:
                self.assertEqual(callback.alerts, [expected])
        self.assertEqual(message.edits, [])

    async def test_missing_session(self) -> None:
        message, token = await self._paging_fixture()
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        await commands.on_points_history_paging(callback, _settings(), None)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["会话未就绪，请重新 /points"])

    async def test_revoked_authorization_is_rejected_persistently(self) -> None:
        """真实撤销授权（写库），而不是 mock 常量。"""

        message, token = await self._paging_fixture()
        await self._set_authorized(bot_present=False)

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["当前群未授权，请重新 /points"])
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        self.assertEqual(record.applied_page, -1)

        # 恢复授权后同一个按钮可以继续用（状态没有被销毁）。
        await self._set_authorized(bot_present=True)
        again = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(again, _settings(), session)
        self.assertEqual(len(message.edits), 1)

    async def test_authorization_check_failure_fails_closed(self) -> None:
        message, token = await self._paging_fixture()

        async def broken(session, group_id):
            raise RuntimeError("synthetic authorization failure")

        with patch.object(commands, "is_group_authorized", broken):
            callback = _FakeCallback(message=message, data=f"phs:{token}:1")
            async with self.session_factory() as session:
                await commands.on_points_history_paging(
                    callback, _settings(), session
                )
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["当前群未授权，请重新 /points"])

    async def test_expiry_and_cap_prune_the_store(self) -> None:
        await self._authorize()
        for index in range(25):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("expiry"),
            )
        snapshot = await self._snapshot()
        record = ph.freeze_history(snapshot=snapshot, head="<b>我的积分</b>")
        store = ph.PointsHistorySessionStore(ttl_seconds=10, max_sessions=2)
        tokens = []
        for index in range(4):
            item = ph.freeze_history(snapshot=snapshot, head=f"<b>我的积分</b>{index}")
            tokens.append(
                store.create(item, owner_user_id=USER, group_id=GROUP, now=float(index))
            )
        self.assertEqual(len(store), 2)
        self.assertIsNone(store.get(tokens[0], now=3.0))
        self.assertIsNotNone(store.get(tokens[3], now=3.0))
        # TTL 到点真删。
        self.assertIsNone(store.get(tokens[3], now=13.0))
        self.assertEqual(len(store), 0)

        # 真实产品的 store 也必须有界（TTL + 条数上限 + 状态量上限）。
        self.assertEqual(
            commands._POINTS_HISTORY_STORE.max_sessions,
            commands._POINTS_HISTORY_MAX_SESSIONS,
        )
        self.assertEqual(
            commands._POINTS_HISTORY_STORE.ttl_seconds,
            commands._POINTS_HISTORY_TTL_SECONDS,
        )
        self.assertGreaterEqual(commands._POINTS_HISTORY_TTL_SECONDS, 60)
        self.assertLessEqual(commands._POINTS_HISTORY_TTL_SECONDS, 3600)
        self.assertLessEqual(
            commands._POINTS_HISTORY_MAX_SESSIONS * ph.MAX_STATE_UNITS, 512 * 1024
        )
        # 会话里绝不缓存余额 / 完整历史：只有冻结的摘要与窗口。
        self.assertFalse(hasattr(record, "entries"))
        self.assertEqual(record.page_bodies, ())

    async def test_failed_edit_keeps_state_and_retry_really_edits(self) -> None:
        """F03：编辑失败不能记成成功；再点同一页必须真的重试一次编辑。"""

        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        message.edit_failures = [RuntimeError("synthetic transport failure")]

        first = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(first, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(first.alerts, ["刷新失败，请重新 /points"])
        self.assertEqual(record.applied_page, -1)
        self.assertEqual(record.applied_seq, 0)

        second = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(second, _settings(), session)
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(second.alerts, [])
        self.assertEqual(record.applied_page, 1)
        self.assertEqual(record.applied_seq, 1)

    async def test_not_modified_is_success_and_silent(self) -> None:
        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        message.edit_failures = [_bad_request("Bad Request: message is not modified")]

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, [])
        # 屏幕上已经是这一页：等同成功。
        self.assertEqual(record.applied_page, 1)
        self.assertEqual(record.applied_seq, 1)

    async def test_deleted_message_gets_safe_receipt(self) -> None:
        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        message.edit_failures = [_bad_request("Bad Request: message to edit not found")]

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["消息已失效，请重新 /points"])
        self.assertEqual(record.applied_page, -1)

    async def test_forbidden_gets_safe_receipt(self) -> None:
        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        message.edit_failures = [_forbidden()]

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["这条消息已无法编辑，请重新 /points"])
        self.assertEqual(record.applied_page, -1)

    async def test_transport_failure_gets_safe_receipt_and_keeps_state(self) -> None:
        message, token = await self._paging_fixture()
        message.edit_failures = [_bad_request("Bad Request: unknown transport glitch")]
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["刷新失败，请重新 /points"])

    async def test_invalid_page_number_is_rejected_before_editing(self) -> None:
        message, token = await self._paging_fixture()
        for data in (f"phs:{token}:x", f"phs:{token}:-1", f"phs:{token}:"):
            callback = _FakeCallback(message=message, data=data)
            async with self.session_factory() as session:
                await commands.on_points_history_paging(
                    callback, _settings(), session
                )
            self.assertEqual(callback.alerts, ["查询已失效，请重新 /points"])
        self.assertEqual(message.edits, [])

    async def test_receipt_failure_does_not_break_the_route(self) -> None:
        message, token = await self._paging_fixture()
        message.edit_failures = [_forbidden()]
        callback = _FakeCallback(
            message=message,
            data=f"phs:{token}:1",
            answer_failure=RuntimeError("synthetic receipt failure"),
        )
        async with self.session_factory() as session:
            # 回执失败不能把异常抛回路由。
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])

    async def test_summary_mismatch_fails_closed(self) -> None:
        """完整性保护：冻结摘要与现读摘要不一致时拒绝编辑。"""

        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        # 伪造一个对不上的冻结摘要（模拟被篡改/两代口径）。
        record.summary = ph.PointsHistorySummary(income=1, spend=0, net=1, count=25)

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self.session_factory() as session:
            await commands.on_points_history_paging(callback, _settings(), session)
        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["查询已失效，请重新 /points"])

    async def test_waiting_for_lock_revalidates_expiry(self) -> None:
        """F05：排队等锁期间会话过期就不得继续编辑。"""

        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        await record.lock.acquire()
        entered = asyncio.Event()

        async def precheck(session, group_id):
            entered.set()
            return True

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        with patch.object(commands, "is_group_authorized", precheck):
            async with self.session_factory() as session:
                task = asyncio.create_task(
                    commands.on_points_history_paging(callback, _settings(), session)
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                # 时钟补丁要活到任务真正跑起来（release() 只是把等待者排进事件循环，
                # 任务本身要等到下一次 await 才会继续）。
                with patch.object(ph.time, "time", lambda: record.created_at + 601.0):
                    record.lock.release()
                    await asyncio.wait_for(task, timeout=5)

        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["查询已过期，请重新 /points"])

    async def test_waiting_for_lock_revalidates_authorization(self) -> None:
        """F05：预检通过之后授权失效（等锁期间发生）也不得继续编辑。"""

        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        await record.lock.acquire()
        entered = asyncio.Event()
        state = {"allowed": True, "calls": 0}

        async def checking(session, group_id):
            state["calls"] += 1
            if state["calls"] == 1:
                entered.set()
                return True
            return state["allowed"]

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        with patch.object(commands, "is_group_authorized", checking):
            async with self.session_factory() as session:
                task = asyncio.create_task(
                    commands.on_points_history_paging(callback, _settings(), session)
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                state["allowed"] = False
                record.lock.release()
                await asyncio.wait_for(task, timeout=5)

        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["当前群未授权，请重新 /points"])
        self.assertEqual(record.applied_page, -1)

    async def test_waiting_for_lock_revalidates_the_store_entry(self) -> None:
        """F05：等锁期间被 TTL/容量淘汰（token 已不指向同一对象）也不编辑。"""

        message, token = await self._paging_fixture()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        await record.lock.acquire()
        entered = asyncio.Event()

        async def precheck(session, group_id):
            entered.set()
            return True

        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        with patch.object(commands, "is_group_authorized", precheck):
            async with self.session_factory() as session:
                task = asyncio.create_task(
                    commands.on_points_history_paging(callback, _settings(), session)
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                commands._POINTS_HISTORY_STORE.discard(token)
                record.lock.release()
                await asyncio.wait_for(task, timeout=5)

        self.assertEqual(message.edits, [])
        self.assertEqual(callback.alerts, ["查询已过期，请重新 /points"])

    async def test_concurrent_pages_apply_in_order_without_stale_overwrite(
        self,
    ) -> None:
        await self._authorize()
        for index in range(45):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("concurrent"),
            )
        message = self._message()
        await self._run_command(message)
        token = _token_of(message)
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        self.assertEqual(record.page_count, 3)

        card = message.card_view()
        first = _FakeCallback(message=card, data=f"phs:{token}:1")
        second = _FakeCallback(message=card, data=f"phs:{token}:2")

        # 真实链路上每个 update 由中间件各给一个 session（AsyncSession 不能并发共用）。
        # 授权也走真实只读检查（本用例已经写过 AuthorizedGroup 行）；两个任务里并发
        # patch.object 同一个属性会互相覆盖：后退出的一方会把另一方存下的 mock 恢复
        # 回去，于是补丁永久留在模块上（踩过一次）。
        async def dispatch(callback):
            async with self.session_factory() as session:
                await commands.on_points_history_paging(callback, _settings(), session)

        await asyncio.gather(dispatch(first), dispatch(second))

        self.assertEqual(record.applied_seq, 2)
        self.assertEqual(len(message.edits), 2)
        self.assertIn(
            f"第 {record.applied_page + 1} / 3 页", message.edits[-1][0]
        )
        self.assertEqual(len(set(entry for entry in [
            message.edits[0][0].split("第 ", 1)[1].split(" / 3 页", 1)[0],
            message.edits[1][0].split("第 ", 1)[1].split(" / 3 页", 1)[0],
        ])), 2)


# ---------------------------------------------------------------------------
# 读生命周期：一致读的获取 / 释放 / 取消（第二轮 m01024）
# ---------------------------------------------------------------------------


class ReadLifecycleTests(_DbTestCase):
    """一致读的失败收尾：读事务的释放必须被**确认**，确认不了就不许发。

    故障注入全部打在真实 SQL 边界（``BEGIN`` / ``COMMIT`` / ``session.rollback``）上，
    用项目 ``init_db`` 建的真 SQLite 与真 ``AsyncSession``；每个用例都在 fixture
    teardown **之前**直接读驱动层 ``in_transaction``（比 ORM 记账硬：拿到的是
    sqlite3 连接本身）。注意 WAL 下独立 writer 能写**不能**替代驱动状态断言，
    所以两件事分开断言。
    """

    @staticmethod
    async def _driver(session):
        """驱动层连接：``sqlite3.Connection`` 的 ``in_transaction`` 才是硬事实。"""

        connection = await session.connection()
        raw = await connection.get_raw_connection()
        return raw.driver_connection

    def _pool_checked_out(self) -> int:
        """连接池里「借出去还没还」的连接数（收尾干净就该是 0）。"""

        status = self.engine.pool.status()
        return int(status.rsplit(":", 1)[1])

    async def _visible_award_ref(self, ref: str):
        """另一个会话里能不能看见这笔奖励（用来验证「调用方的写没有被我们提交」）。"""

        async with self.session_factory() as other:
            rows = (
                (
                    await other.execute(
                        select(MemberPointAward).where(MemberPointAward.ref == ref)
                    )
                )
                .scalars()
                .all()
            )
        return rows[0] if rows else None

    @asynccontextmanager
    async def _faults(
        self,
        session,
        *,
        begin_failure=None,
        commit_failure=None,
        commit_failure_after=0,
        rollback_failure=None,
        rollback_failure_after=0,
        session_commit_failure=None,
    ):
        """在真实 SQL 文本边界注入故障，并回传「确实走到过哪一步」的计数。"""

        original_execute = session.execute
        original_rollback = session.rollback
        original_commit = session.commit
        counts = {"begin": 0, "commit": 0, "rollback": 0}

        async def execute(statement, *args, **kwargs):
            sql = str(statement).strip().upper()
            if sql == "BEGIN":
                counts["begin"] += 1
                if begin_failure is not None:
                    raise begin_failure
            if sql == "COMMIT":
                counts["commit"] += 1
                if (
                    commit_failure is not None
                    and counts["commit"] > commit_failure_after
                ):
                    raise commit_failure
            return await original_execute(statement, *args, **kwargs)

        async def rollback():
            counts["rollback"] += 1
            if (
                rollback_failure is not None
                and counts["rollback"] > rollback_failure_after
            ):
                raise rollback_failure
            return await original_rollback()

        async def commit():
            if session_commit_failure is not None:
                raise session_commit_failure
            return await original_commit()

        with patch.object(session, "execute", execute), patch.object(
            session, "rollback", rollback
        ), patch.object(session, "commit", commit):
            yield counts

    def _command_env(self, message: _FakeMessage, **faults):
        """真 session + 真 ``cmd_points``；有故障时额外给驱动探针接上。"""

        @asynccontextmanager
        async def runner():
            with patch.object(
                commands, "ensure_group_authorized", AsyncMock(return_value=True)
            ), self._frozen_clock():
                async with self.session_factory() as session:
                    driver = await self._driver(session)
                    message.session_probe = session.in_transaction
                    message.driver_probe = driver
                    if faults:
                        async with self._faults(session, **faults) as counts:
                            yield session, driver, counts
                    else:
                        yield session, driver, None

        return runner()

    def _callback_env(self, callback: _FakeCallback, **faults):
        """真 session + 真翻页回调；探针接到「那张卡」上。"""

        @asynccontextmanager
        async def runner():
            async with self.session_factory() as session:
                driver = await self._driver(session)
                card = callback.message
                card.session_probe = session.in_transaction
                card.driver_probe = driver
                callback.tx_probe = session.in_transaction
                if faults:
                    async with self._faults(session, **faults) as counts:
                        yield session, driver, counts
                else:
                    yield session, driver, None

        return runner()

    async def _seed(self, entries: int = 25) -> None:
        await self._authorize()
        for index in range(entries):
            await self._award(
                _day(0) - timedelta(minutes=index + 1),
                points=1,
                ref=self._ref("lifecycle"),
            )

    async def _paging_record(self, *, entries: int = 25):
        await self._seed(entries)
        message = self._message()
        async with self._command_env(message) as (session, driver, _counts):
            await commands.cmd_points(message, session, _settings())
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 1)
            self.assertFalse(driver.in_transaction)
        return message.card_view(), _token_of(message)

    # -- 正控制 ---------------------------------------------------------------

    async def test_query_releases_the_driver_transaction_before_sending(self) -> None:
        await self._seed()
        message = self._message()
        async with self._command_env(message) as (session, driver, _counts):
            self.assertFalse(driver.in_transaction)
            await commands.cmd_points(message, session, _settings())
            # 发送那一刻：ORM 记账与驱动层都已经是「没有事务」。
            self.assertEqual(message.transaction_state_at_answer, [False])
            self.assertEqual(message.driver_state_at_answer, [False])
            self.assertFalse(driver.in_transaction)
            self.assertIn("近 30 天流水", message.last_answer)
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 1)
        self.assertEqual(self._pool_checked_out(), 0)

    # -- cmd_points 侧 -------------------------------------------------------

    async def test_release_failure_falls_back_and_never_sends_the_card(self) -> None:
        await self._seed()
        message = self._message()
        async with self._command_env(
            message, commit_failure=RuntimeError("synthetic COMMIT failure")
        ) as (session, driver, counts):
            await commands.cmd_points(message, session, _settings())
            # 真的走到了真实 COMMIT 边界，并且靠兜底 ROLLBACK 把连接交还了。
            self.assertEqual(counts["commit"], 1)
            self.assertGreaterEqual(counts["rollback"], 1)
            self.assertEqual(message.driver_state_at_answer, [False])
            self.assertEqual(len(message.answers), 1)
            self.assertIn(commands._POINTS_READ_FAILED_TEXT, message.last_answer)
            self.assertNotIn("近 30 天流水", message.last_answer)
            # 只有「成功卡片」才会注册翻页状态：失败不留可用状态。
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
            self.assertFalse(driver.in_transaction)
            # 独立 writer 仍然有界完成（这一条不能替代上面的驱动断言）。
            async with self.session_factory() as writer:
                writer.add(
                    MemberPointAward(
                        group_id=GROUP,
                        user_id=USER,
                        points=3,
                        reason=AWARD_REASON_WEEKLY_ACTIVITY,
                        ref=self._ref("writer"),
                        created_at=_day(0),
                    )
                )
                await asyncio.wait_for(writer.commit(), timeout=5)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_release_cancellation_propagates_the_original_object(self) -> None:
        await self._seed()
        message = self._message()
        cancellation = asyncio.CancelledError("synthetic release cancellation")
        async with self._command_env(message, commit_failure=cancellation) as (
            session,
            driver,
            counts,
        ):
            with self.assertRaises(asyncio.CancelledError) as caught:
                await commands.cmd_points(message, session, _settings())
            # 原取消对象按 identity 传播，没有被换成别的异常。
            self.assertIs(caught.exception, cancellation)
            self.assertEqual(message.answers, [])
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
            # 取消不改「连接已确认交还」这一事实（兜底 ROLLBACK 仍然执行）。
            self.assertGreaterEqual(counts["rollback"], 1)
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_acquire_failure_is_wrapped_and_replies_friendly(self) -> None:
        await self._seed()
        message = self._message()
        async with self._command_env(
            message, begin_failure=RuntimeError("synthetic BEGIN failure")
        ) as (session, driver, counts):
            # 获取读快照的普通失败不许逃到调用方，也不许把异常原文给用户。
            await commands.cmd_points(message, session, _settings())
            self.assertEqual(counts["begin"], 1)
            self.assertEqual(len(message.answers), 1)
            self.assertIn(commands._POINTS_READ_FAILED_TEXT, message.last_answer)
            self.assertNotIn("近 30 天流水", message.last_answer)
            self.assertEqual(message.driver_state_at_answer, [False])
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_read_failure_midway_releases_and_replies_friendly(self) -> None:
        await self._seed()
        message = self._message()
        async with self._command_env(message) as (session, driver, _counts):
            with patch.object(
                ph,
                "read_totals",
                AsyncMock(side_effect=RuntimeError("synthetic read failure")),
            ):
                await commands.cmd_points(message, session, _settings())
            self.assertEqual(message.driver_state_at_answer, [False])
            self.assertIn(commands._POINTS_READ_FAILED_TEXT, message.last_answer)
            self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_unreleasable_cleanup_is_never_reported_as_released(self) -> None:
        """所有收尾路径都失败：一致读必须明确说「没确认释放」，不许静默成功。"""

        await self._authorize()
        armed = {"rollback": True}
        async with self.session_factory() as session:
            driver = await self._driver(session)
            original_execute = session.execute
            original_rollback = session.rollback

            async def execute(statement, *args, **kwargs):
                if str(statement).strip().upper() == "COMMIT":
                    raise RuntimeError("synthetic COMMIT failure")
                return await original_execute(statement, *args, **kwargs)

            async def rollback():
                if armed["rollback"]:
                    raise RuntimeError("synthetic rollback failure")
                return await original_rollback()

            with patch.object(session, "execute", execute), patch.object(
                session, "rollback", rollback
            ):
                with self.assertRaises(ph.PointsHistoryReadUnreleased):
                    async with ph.consistent_read(session):
                        await ph.read_high_water(
                            session, group_id=GROUP, user_id=USER
                        )
                self.assertTrue(driver.in_transaction)  # 不许声称已关闭
                # 只有真的 ROLLBACK 之后连接才算交还：这里证明还能救回来。
                armed["rollback"] = False
                await session.rollback()
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_command_stays_silent_when_the_read_cannot_be_released(self) -> None:
        """读事务释放不了就什么都不发：回执本身也是网络，不能持连接等网络。"""

        await self._seed()
        message = self._message()
        armed = {"rollback": True}
        with patch.object(
            commands, "ensure_group_authorized", AsyncMock(return_value=True)
        ), self._frozen_clock():
            async with self.session_factory() as session:
                driver = await self._driver(session)
                message.session_probe = session.in_transaction
                message.driver_probe = driver
                original_execute = session.execute
                original_rollback = session.rollback

                async def execute(statement, *args, **kwargs):
                    if str(statement).strip().upper() == "COMMIT":
                        raise RuntimeError("synthetic COMMIT failure")
                    return await original_execute(statement, *args, **kwargs)

                async def rollback():
                    if armed["rollback"]:
                        raise RuntimeError("synthetic rollback failure")
                    return await original_rollback()

                with patch.object(session, "execute", execute), patch.object(
                    session, "rollback", rollback
                ):
                    await commands.cmd_points(message, session, _settings())
                    self.assertEqual(message.answers, [])  # 连回执都不发
                    self.assertEqual(message.driver_state_at_answer, [])
                    self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
                    self.assertTrue(driver.in_transaction)
                    armed["rollback"] = False
                    await session.rollback()
                self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_transient_release_failure_is_recovered_by_the_next_query(
        self,
    ) -> None:
        await self._seed()
        first = self._message()
        async with self._command_env(
            first, commit_failure=RuntimeError("synthetic transient COMMIT failure")
        ) as (session, driver, _counts):
            await commands.cmd_points(first, session, _settings())
            self.assertIn(commands._POINTS_READ_FAILED_TEXT, first.last_answer)
            self.assertFalse(driver.in_transaction)
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)

        # 同一条链路的下一次查询完全正常（临时故障没有污染会话/连接）。
        second = self._message()
        async with self._command_env(second) as (session, driver, _counts):
            await commands.cmd_points(second, session, _settings())
            self.assertIn("近 30 天流水", second.last_answer)
            self.assertEqual(second.driver_state_at_answer, [False])
            self.assertFalse(driver.in_transaction)
        self.assertEqual(len(commands._POINTS_HISTORY_STORE), 1)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_caller_owned_transaction_fails_closed_and_is_left_untouched(
        self,
    ) -> None:
        """会话本来就在事务里（调用方自己开着写）：本次读不接管，也不发任何消息。

        不 COMMIT/ROLLBACK 调用方的写（那笔写之后仍由它自己提交或回滚），但也**不**
        能据此声称「已确认可以发送」——持写锁等 Telegram 是被明确禁止的，所以
        points 在这里 fail closed：不发送、不注册翻页状态。
        """

        await self._authorize()
        message = self._message()
        with patch.object(
            commands, "ensure_group_authorized", AsyncMock(return_value=True)
        ), self._frozen_clock():
            async with self.session_factory() as session:
                pending = MemberPointAward(
                    group_id=GROUP,
                    user_id=USER,
                    points=7,
                    reason=AWARD_REASON_WEEKLY_ACTIVITY,
                    ref=self._ref("caller"),
                    created_at=_day(0),
                )
                session.add(pending)
                await session.flush()
                driver = await self._driver(session)
                self.assertTrue(driver.in_transaction)  # 进来时事务就是调用方的
                message.session_probe = session.in_transaction
                message.driver_probe = driver

                await commands.cmd_points(message, session, _settings())
                # fail closed：没有卡片、没有失败提示、没有注册翻页 token。
                self.assertEqual(message.answers, [])
                self.assertEqual(message.driver_state_at_answer, [])
                self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
                # 调用方的事务原样留着，谁开的谁负责。
                self.assertTrue(driver.in_transaction)

                # 我们的读没有替调用方提交：别人看不见那笔挂起的写。
                self.assertIsNone(await self._visible_award_ref(pending.ref))

                # 调用方自己回滚：那笔写干净撤销，读没留下任何痕迹。
                await session.rollback()
                self.assertFalse(driver.in_transaction)
                self.assertIsNone(await self._visible_award_ref(pending.ref))
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_caller_owned_transaction_can_be_committed_by_the_owner_afterwards(
        self,
    ) -> None:
        """调用方自己提交后那笔写可见：我们只是没动它，也没拦它。"""

        await self._authorize()
        message = self._message()
        with patch.object(
            commands, "ensure_group_authorized", AsyncMock(return_value=True)
        ), self._frozen_clock():
            async with self.session_factory() as session:
                pending = MemberPointAward(
                    group_id=GROUP,
                    user_id=USER,
                    points=7,
                    reason=AWARD_REASON_WEEKLY_ACTIVITY,
                    ref=self._ref("caller-commit"),
                    created_at=_day(0),
                )
                session.add(pending)
                await session.flush()
                driver = await self._driver(session)
                message.driver_probe = driver

                await commands.cmd_points(message, session, _settings())
                self.assertEqual(message.answers, [])
                self.assertIsNone(await self._visible_award_ref(pending.ref))

                await session.commit()  # 由调用方自己提交
                self.assertFalse(driver.in_transaction)
            self.assertIsNotNone(await self._visible_award_ref(pending.ref))
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_callback_with_caller_owned_transaction_stays_silent(self) -> None:
        """回调会话里调用方已有未提交写：不编辑、不 alert、不接管那个事务。

        预检授权在这里被 mock 掉，只用来隔离要测的 points 生命周期，
        **不**代表通用 authz 行为已被改过。
        """

        message, token = await self._paging_record()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self._callback_env(callback) as (session, driver, _counts):
            session.add(
                MemberPointAward(
                    group_id=GROUP,
                    user_id=USER,
                    points=5,
                    reason=AWARD_REASON_WEEKLY_ACTIVITY,
                    ref=self._ref("caller-callback"),
                    created_at=_day(0),
                )
            )
            await session.flush()
            callback.tx_probe = session.in_transaction
            self.assertTrue(driver.in_transaction)
            with patch.object(
                commands, "is_group_authorized", AsyncMock(return_value=True)
            ):
                await commands.on_points_history_paging(
                    callback, _settings(), session
                )
            self.assertEqual(message.edits, [])
            self.assertEqual(callback.answers, [])
            self.assertTrue(driver.in_transaction)  # 调用方的事务原样留着
            await session.rollback()
            self.assertFalse(driver.in_transaction)
        self.assertEqual(record.applied_page, -1)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_cleanup_cancellation_wins_over_a_body_failure(self) -> None:
        """读失败 + 收尾取消：清理后按原对象传播那个取消，不能降级成普通失败。"""

        await self._authorize()
        cancellation = asyncio.CancelledError("synthetic cleanup cancellation")
        async with self.session_factory() as session:
            driver = await self._driver(session)
            async with self._faults(session, commit_failure=cancellation) as counts:
                with self.assertRaises(asyncio.CancelledError) as caught:
                    async with ph.consistent_read(session):
                        await ph.read_high_water(
                            session, group_id=GROUP, user_id=USER
                        )
                        raise RuntimeError("synthetic reader failure")
                self.assertIs(caught.exception, cancellation)
                self.assertGreaterEqual(counts["rollback"], 1)  # 兜底真的跑了
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_cleanup_cancellation_wins_over_an_acquire_failure(self) -> None:
        """获取失败（BEGIN）+ 收尾取消：同样按原对象传播取消，而不是友好失败。"""

        cancellation = asyncio.CancelledError("synthetic cleanup cancellation")
        async with self.session_factory() as session:
            driver = await self._driver(session)
            async with self._faults(
                session,
                begin_failure=RuntimeError("synthetic BEGIN failure"),
                rollback_failure=cancellation,
            ) as counts:
                with self.assertRaises(asyncio.CancelledError) as caught:
                    async with ph.consistent_read(session):
                        pass
                self.assertIs(caught.exception, cancellation)
                self.assertEqual(counts["begin"], 1)
                self.assertGreaterEqual(counts["rollback"], 1)
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_cancellation_while_acquiring_the_driver_connection_propagates(
        self,
    ) -> None:
        """连拿驱动连接都被取消：原取消对象传播，不吞成普通失败。"""

        cancellation = asyncio.CancelledError("synthetic acquire cancellation")
        async with self.session_factory() as session:
            with patch.object(
                session, "connection", AsyncMock(side_effect=cancellation)
            ):
                with self.assertRaises(asyncio.CancelledError) as caught:
                    async with ph.consistent_read(session):
                        pass
                self.assertIs(caught.exception, cancellation)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_command_stays_silent_when_cleanup_cancels_the_read(self) -> None:
        """命令层：收尾取消 → 一条消息都不发，取消按原对象往上抛。"""

        await self._seed()
        message = self._message()
        cancellation = asyncio.CancelledError("synthetic cleanup cancellation")
        with patch.object(
            commands, "ensure_group_authorized", AsyncMock(return_value=True)
        ), self._frozen_clock():
            async with self.session_factory() as session:
                driver = await self._driver(session)
                message.session_probe = session.in_transaction
                message.driver_probe = driver
                async with self._faults(session, commit_failure=cancellation):
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await commands.cmd_points(message, session, _settings())
                    self.assertIs(caught.exception, cancellation)
                self.assertEqual(message.answers, [])
                self.assertEqual(message.driver_state_at_answer, [])
                self.assertEqual(len(commands._POINTS_HISTORY_STORE), 0)
                # 首选 COMMIT 被取消，兜底 ROLLBACK 已经把连接交还；取消照样传播。
                self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)

    # -- 翻页回调侧 -----------------------------------------------------------

    async def test_callback_paging_releases_the_read_before_editing(self) -> None:
        message, token = await self._paging_record()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self._callback_env(callback) as (session, driver, _counts):
            await commands.on_points_history_paging(callback, _settings(), session)
            self.assertEqual(len(message.edits), 1)
            self.assertEqual(message.transaction_state_at_edit, [False])
            self.assertEqual(message.driver_state_at_edit, [False])
            self.assertEqual(callback.transaction_state_at_answer, [False])
            self.assertFalse(driver.in_transaction)
        self.assertEqual(record.applied_page, 1)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_callback_page_read_release_failure_releases_before_the_alert(
        self,
    ) -> None:
        message, token = await self._paging_record()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        # 第一次 COMMIT 属于授权预检（让它成功），第二次才是读页那一次。
        async with self._callback_env(
            callback,
            commit_failure=RuntimeError("synthetic page read COMMIT failure"),
            commit_failure_after=1,
        ) as (session, driver, counts):
            await commands.on_points_history_paging(callback, _settings(), session)
            self.assertGreaterEqual(counts["commit"], 2)
            self.assertEqual(message.edits, [])
            self.assertEqual(callback.alerts, ["刷新失败，请重新 /points"])
            self.assertEqual(callback.transaction_state_at_answer, [False])
            self.assertEqual(record.applied_page, -1)  # 状态保持原样，重试会真编辑
            self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)
        self.assertIs(commands._POINTS_HISTORY_STORE.get(token), record)

    async def test_callback_authorization_read_unreleased_stays_silent(self) -> None:
        message, token = await self._paging_record()
        record = commands._POINTS_HISTORY_STORE.get(token)
        faults = {
            "commit_failure": RuntimeError("synthetic COMMIT failure"),
            "rollback_failure": RuntimeError("synthetic rollback failure"),
        }
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self._callback_env(callback, **faults) as (session, driver, _c):
            await commands.on_points_history_paging(callback, _settings(), session)
            # 授权预检的读没能确认交还：连 alert 都不发，也不编辑。
            self.assertEqual(message.edits, [])
            self.assertEqual(callback.answers, [])
            self.assertTrue(driver.in_transaction)  # 不许声称已交还
        self.assertEqual(self._pool_checked_out(), 0)
        self.assertIs(commands._POINTS_HISTORY_STORE.get(token), record)

    async def test_callback_page_read_unreleased_stays_silent(self) -> None:
        message, token = await self._paging_record()
        record = commands._POINTS_HISTORY_STORE.get(token)
        assert record is not None
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        async with self._callback_env(
            callback,
            commit_failure=RuntimeError("synthetic page read COMMIT failure"),
            commit_failure_after=1,
            rollback_failure=RuntimeError("synthetic page read rollback failure"),
            rollback_failure_after=1,
        ) as (session, driver, _c):
            await commands.on_points_history_paging(callback, _settings(), session)
            self.assertEqual(message.edits, [])
            self.assertEqual(callback.answers, [])
            self.assertTrue(driver.in_transaction)
            self.assertEqual(record.applied_page, -1)
        self.assertEqual(self._pool_checked_out(), 0)

    async def test_callback_unknown_authorization_error_fails_closed_after_release(
        self,
    ) -> None:
        message, token = await self._paging_record()
        callback = _FakeCallback(message=message, data=f"phs:{token}:1")
        with patch.object(
            commands,
            "is_group_authorized",
            AsyncMock(side_effect=RuntimeError("synthetic authz failure")),
        ):
            async with self._callback_env(callback) as (session, driver, _counts):
                await commands.on_points_history_paging(callback, _settings(), session)
                self.assertEqual(message.edits, [])
                self.assertEqual(
                    callback.alerts, ["当前群未授权，请重新 /points"]
                )
                # 未知授权错误也要在**释放之后**才回 alert（fail closed）。
                self.assertEqual(callback.transaction_state_at_answer, [False])
                self.assertFalse(driver.in_transaction)
        self.assertEqual(self._pool_checked_out(), 0)


if __name__ == "__main__":
    unittest.main()
