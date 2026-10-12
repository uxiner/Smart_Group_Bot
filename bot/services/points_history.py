"""近 30 天积分流水：只读合成三本**真实**台账的一条时间线。

数据来源只有三张现表，不新增台账、不改余额定义：

* :class:`~bot.db.models.MemberCheckin`      —— 签到收入（+）
* :class:`~bot.db.models.MemberPointAward`   —— 奖励 / 退款收入（+）
* :class:`~bot.db.models.MemberPointSpend`   —— 消费支出（-）

可用积分仍然只由 :func:`bot.services.checkin.available_from_ledgers` 解释
（签到 + 奖励 - 消费），这里的「净变动」只是**窗口内**的收入减支出，绝不是
当前余额。

窗口口径：Asia/Shanghai 的今天及之前 29 个自然日，起日 00:00 到**首次查询
时刻**；查询时刻之后的行被排除。查询时刻在第一次查询时冻结，翻页读的始终是
同一个窗口。

一致读（F01）
-------------

三张表的筛选、聚合和分页都在**同一个读事务**里完成：先按
:data:`_HISTORY_SQL_HEAD` 拼出三表 ``UNION ALL`` 的 CTE，再用一条聚合 SQL 拿
到总额、一条分页 SQL 拿到当前页。没有真实 ``BEGIN`` 时 SQLite 的每条 SELECT
各拿一个快照，中间提交的新行会让「余额头部 / 30 天摘要 / 三表高水位 / 当前页」
来自不同两代；所以这里复用 usage report 已经验证过的
:func:`bot.services.group_costs._open_report_read_snapshot`（同一份实现，不改
通用 engine）显式发 ``BEGIN``，并在**任何 Telegram 发送/编辑之前**结束事务，
不长期持锁等网络。快照建不起来就抛
:class:`PointsHistorySnapshotUnavailable`，绝不静默退回不一致读。

有界状态（F02）
---------------

会话里只留一个**有界描述符**：群/人、原消息、冻结时刻、窗口、三表高水位、
固定摘要和分页元信息。页面文本、完整历史、逐页渲染结果一概不存；翻页时按
冻结的水位（``id <= high_water``）现查现渲染，所以

* 单会话持久状态不随流水条数 N 增长（见
  :meth:`PointsHistorySession.bounded_state_units`，及其上界
  :data:`MAX_STATE_UNITS`）；
* 分页期间新落库的流水不会进旧查询（水位 + 窗口都冻结），新 ``/points`` 才刷新。

每页最多 :data:`MAX_PAGE_ENTRIES` 笔，排序为「时间倒序 → 三表固定顺序 → id
倒序」，每一笔都恰好出现在一页上，不做截断、也不是「只看最近 20 笔」。

时钟口径与写入方一致：三张表的 ``created_at`` 都是本地（Asia/Shanghai）朴素
时间（见 :func:`bot.utils.timezone.now_shanghai_naive` 与各 writer），所以这里
按朴素本地时间比较。

本模块只读：不 INSERT / UPDATE / DELETE 任何一行。
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta
from typing import Any, NoReturn

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    TextClause,
    bindparam,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import MemberCheckin, MemberPointAward, MemberPointSpend
from bot.services.activity import AWARD_REASON_WEEKLY_ACTIVITY
from bot.services.checkin import SPEND_REASON_CHALLENGE
from bot.services.group_costs import _open_report_read_snapshot
from bot.services.message_templates import truncate_telegram_text
from bot.services.point_shop import (
    AWARD_REASON_LOTTERY,
    AWARD_REASON_REFUND,
    SPEND_REASON_LOTTERY,
    SPEND_REASON_PIN,
    SPEND_REASON_TAG,
)
from bot.utils.timezone import now_shanghai_naive, to_shanghai_naive

_log = logging.getLogger(__name__)

__all__ = [
    "COLLAPSE_ABOVE_ENTRIES",
    "EMPTY_HISTORY_TEXT",
    "HISTORY_WINDOW_DAYS",
    "MAX_ENTRY_LINE_UNITS",
    "MAX_PAGE_ENTRIES",
    "MAX_STATE_UNITS",
    "MESSAGE_SAFE_UNITS",
    "PointsHistoryEntry",
    "PointsHistoryPage",
    "PointsHistoryReadError",
    "PointsHistoryReadUnreleased",
    "PointsHistorySession",
    "PointsHistorySessionStore",
    "PointsHistorySnapshot",
    "PointsHistorySnapshotUnavailable",
    "PointsHistorySummary",
    "PointsHistoryWindow",
    "ReadLease",
    "build_history_snapshot",
    "clamp_page",
    "consistent_read",
    "freeze_history",
    "label_for_reason",
    "legacy_checkin_entry",
    "load_history_page",
    "open_history",
    "page_count_for",
    "render_entry_line",
    "render_history_header",
    "render_range_text",
    "resolve_window",
    "sort_entries",
]

#: 流水窗口：今天及之前 29 个自然日（共 30 天）。
HISTORY_WINDOW_DAYS = 30

#: 每页最多几笔。
MAX_PAGE_ENTRIES = 20

#: 超过几笔就折叠（不折叠时不加 ``expandable``）。
COLLAPSE_ABOVE_ENTRIES = 5

#: 单条消息的安全上限（Telegram 的硬上限是 4096 UTF-16 code unit）。
MESSAGE_SAFE_UNITS = 3800

#: 单笔流水行的硬上限：20 行 × 140 + 头部/标签 < 3800，单笔撑不爆一页。
MAX_ENTRY_LINE_UNITS = 140

#: 单个会话描述符的持久状态上限（不随流水条数增长）。
MAX_STATE_UNITS = 1500

#: 空流水时折叠区里的说明文案。
EMPTY_HISTORY_TEXT = "近 30 天暂无积分记录。"

_CHECKIN_LABEL = "每日签到"
_AWARD_FALLBACK_LABEL = "积分奖励"
_SPEND_FALLBACK_LABEL = "积分消费"

#: 已知奖励原因 → 中文（常量直接取自各 writer，不写字面量）。
_AWARD_LABELS = {
    AWARD_REASON_WEEKLY_ACTIVITY: "每周活跃奖励",
    AWARD_REASON_REFUND: "商店退款",
    AWARD_REASON_LOTTERY: "抽奖奖励",
}

#: 已知消费原因 → 中文。
_SPEND_LABELS = {
    SPEND_REASON_CHALLENGE: "免除质询扣分",
    SPEND_REASON_TAG: "购买自定义头衔",
    SPEND_REASON_PIN: "置顶求助",
    SPEND_REASON_LOTTERY: "抽奖消耗",
}

#: 三表在同一时刻的稳定展示顺序。
_SOURCE_DISPLAY_ORDER = {"checkin": 0, "award": 1, "spend": 2}
_SOURCE_NAMES = tuple(_SOURCE_DISPLAY_ORDER)

#: 被截断的 HTML 实体残片（``&`` / ``&#`` / ``&amp`` 之类）。
_PARTIAL_ENTITY_RE = re.compile(r"&[A-Za-z#0-9]{0,7}$")


class PointsHistoryReadError(RuntimeError):
    """流水读出来了但不可信（坏时间戳等）：同样明确失败，不猜、不补零。"""


class PointsHistorySnapshotUnavailable(PointsHistoryReadError):
    """无法建立或无法确认一致的读快照；抛出时读事务**已确认释放**。

    这不是「可以退回旧读法」的可选路径：退回就等于各条 SELECT 各拿一个快照，
    卡片上的余额、摘要、高水位和当前页可能来自不同两代。宁可明确失败。

    收到它的调用方可以安全地回一条友好提示：连接与读快照都已经交还
    （见 :func:`consistent_read` 的契约）。
    """


class PointsHistoryReadUnreleased(PointsHistoryReadError):
    """本次读事务**没能确认释放**：不许再发消息、编辑或注册翻页状态。

    拿不到驱动层状态、COMMIT 与兜底 ROLLBACK 都失败时走到这里。宁可整条查询
    静默失败，也不能一边占着连接和旧读快照一边去调 Telegram。
    """


def _utf16_units(value: str) -> int:
    """Telegram 计长口径：UTF-16 code unit（emoji 算 2）。"""

    return len(str(value).encode("utf-16-le")) // 2


def _safe_truncate_html(value: str, max_units: int) -> str:
    """UTF-16 安全截断，并去掉被截断的 HTML 实体残片。"""

    if _utf16_units(value) <= max_units:
        return value
    cut = truncate_telegram_text(value, max_units, suffix="")
    return _PARTIAL_ENTITY_RE.sub("", cut)


def label_for_reason(source: str, reason: str) -> str:
    """把内部 reason 映射成中文；未知原因只给通用文案，**不复述**内部值。"""

    if source == "checkin":
        return _CHECKIN_LABEL
    if source == "award":
        return _AWARD_LABELS.get(str(reason or ""), _AWARD_FALLBACK_LABEL)
    if source == "spend":
        return _SPEND_LABELS.get(str(reason or ""), _SPEND_FALLBACK_LABEL)
    return _AWARD_FALLBACK_LABEL


def _parse_local_date(value: Any) -> date | None:
    """``YYYY-MM-DD`` → date；解析不出来返回 None（不猜）。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_local_datetime(value: Any) -> datetime | None:
    """SQLite 文本时间 → 朴素本地 datetime；解析不出来返回 None。"""

    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PointsHistoryWindow:
    """一次查询的冻结窗口（朴素本地时间）。"""

    start: datetime
    until: datetime
    today: date


@dataclass(frozen=True, slots=True)
class PointsHistoryEntry:
    """一笔流水。``points`` 带符号：收入为正，消费为负。"""

    source: str
    entry_id: int
    at: datetime
    date_only: bool
    points: int
    label: str


@dataclass(frozen=True, slots=True)
class PointsHistorySummary:
    """窗口内的收支（``spend`` 是正的额度，``net = income - spend``）。"""

    income: int
    spend: int
    net: int
    count: int


@dataclass(frozen=True, slots=True)
class PointsHistorySnapshot:
    """一次读取的结果：窗口 + 当前页行 + 全窗口摘要 + 三表高水位。

    ``entries`` 只放**当前页**的行（生产路径从不全量加载历史）；
    ``summary`` 覆盖整个窗口，``page`` 是实际生效的页号。
    """

    window: PointsHistoryWindow
    entries: tuple[PointsHistoryEntry, ...]
    summary: PointsHistorySummary
    high_water: tuple[tuple[str, int], ...]
    page: int = 0


@dataclass(frozen=True, slots=True)
class PointsHistoryPage:
    """翻页读到的一页（含该时刻重新核对的摘要，用于完整性校验）。"""

    page: int
    summary: PointsHistorySummary
    entries: tuple[PointsHistoryEntry, ...]


def resolve_window(now: datetime | None = None) -> PointsHistoryWindow:
    """把「现在」规范化成本地时间并算出 30 天窗口。

    ``now`` 允许是 aware（会被规范到 Asia/Shanghai）；朴素时间按本地时间解释，
    与写入方一致。调用方应当把**同一个**冻结时刻同时传给本函数和
    :func:`bot.services.checkin.summarize` 的 ``now=``，避免跨午夜时两套时钟。
    """

    moment = to_shanghai_naive(now) if now is not None else now_shanghai_naive()
    today = moment.date()
    start_day = today - timedelta(days=HISTORY_WINDOW_DAYS - 1)
    return PointsHistoryWindow(
        start=datetime.combine(start_day, day_time.min),
        until=moment,
        today=today,
    )


def page_count_for(count: int) -> int:
    """总笔数 → 总页数（空流水也算 1 页，用来显示「暂无记录」）。"""

    total = max(0, int(count))
    if total == 0:
        return 1
    return max(1, math.ceil(total / MAX_PAGE_ENTRIES))


def clamp_page(page: int, count: int) -> int:
    """把请求页夹到 ``0..page_count-1``（越界/负数都友好处理）。"""

    try:
        requested = int(page)
    except (TypeError, ValueError):
        requested = 0
    return max(0, min(requested, page_count_for(count) - 1))


# ---------------------------------------------------------------------------
# 一致读
# ---------------------------------------------------------------------------


async def _driver_connection(session: AsyncSession) -> Any | None:
    """拿到并缓存底层驱动连接，用来**确认**事务真的结束了。

    SQLAlchemy 那一层的 ``session.in_transaction()`` 只是它自己的记账：显式
    ``COMMIT`` 之后它仍然可能是 True，连接也还挂在会话上。判断「连接和读快照
    已经交还」只能看驱动连接自己的 ``in_transaction``。拿不到就返回 None
    （调用方按「未释放」处理，不假装成功）。
    """

    try:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
    except Exception:
        return None
    return getattr(raw, "driver_connection", None)


@dataclass(slots=True)
class ReadLease:
    """一次读快照的租约：谁开的、怎么收尾、有没有**确认**收尾。

    ``caller_owned`` 表示进入时驱动层已经有事务（调用方自己的会话）：那时本次
    什么都不开，也绝不替调用方 COMMIT/ROLLBACK 它的写——但也不会因此声称
    「已确认可发送」，:class:`consistent_read` 会以未释放状态 fail closed。
    """

    session: AsyncSession
    driver: Any = None
    caller_owned: bool = False
    owned: bool = False
    degraded: bool = False
    release_cancelled: BaseException | None = None
    confirmed: bool = False
    _done: bool = False

    @property
    def driver_busy(self) -> bool:
        """驱动层是否仍开着事务；拿不到状态按「仍开着」算（fail closed）。"""

        return bool(getattr(self.driver, "in_transaction", True))

    async def _attempt(self, awaitable: Any) -> bool:
        """执行一个收尾步骤；失败（含取消）只记录，绝不从这里逃出去。"""

        try:
            await awaitable
            return True
        except asyncio.CancelledError as exc:
            if self.release_cancelled is None:
                self.release_cancelled = exc
            return False
        except Exception:
            _log.warning("points history read release step failed", exc_info=True)
            return False

    async def _clear_session_state(self) -> None:
        """清掉 SQLAlchemy 自己的事务记账，让连接真的回到池里。

        只有会话里**没有待提交的写**时才动它：ROLLBACK 会丢弃调用方挂起的写，
        那不是我们的东西（中间件的会话由中间件自己提交）。
        """

        session = self.session
        if session.new or session.dirty or session.deleted:
            return
        if session.in_transaction():
            await self._attempt(session.rollback())

    async def _release(self) -> bool:
        if self.caller_owned:
            # 进入时事务就是调用方的，而且驱动层已经有未提交的写：本次读不接管它
            # ——不 COMMIT、不 ROLLBACK（那会替调用方提交或丢掉它的写），也**不**
            # 声称「已确认可以发送/编辑」。交回「未释放」由调用方（中间件）自己
            # 处理：它可以照常提交或回滚，原待写完整保留。
            return False
        if self.driver is None:
            return False
        if self.driver_busy:
            if self.session.new or self.session.dirty or self.session.deleted:
                # 会话里有调用方挂起、可能已被 flush 的写：显式 COMMIT 会把它一起
                # 提交掉，ROLLBACK 又会丢掉它。两种都不是我们的东西——直接按
                # 「没能确认释放」处理，调用方保持沉默即可（进得来这一步就说明
                # 进入时驱动层没有事务，正常调用方不会同时留着待写入）。
                return False
            # 首选项目惯例的显式 COMMIT；失败再兜底 ROLLBACK（只回滚读事务，
            # 不 flush、不提交任何写）。
            if not await self._attempt(self.session.execute(text("COMMIT"))):
                self.degraded = True
        if self.driver_busy:
            self.degraded = True
            await self._attempt(self.session.rollback())
        if self.driver_busy:
            return False
        await self._clear_session_state()
        if self.driver_busy or self.session.in_transaction():
            return False
        return True

    async def release(self) -> bool:
        """结束本次读事务；返回是否**确认**释放。幂等。"""

        if self._done:
            return self.confirmed
        self._done = True
        self.confirmed = bool(await self._release())
        return self.confirmed


def _finish_failed_read(
    exc: BaseException,
    lease: ReadLease,
    *,
    confirmed: bool,
    fallback: BaseException,
) -> NoReturn:
    """读流程失败（或取消）后的统一收尾语义，必须在 ``lease.release()`` 之后调用。

    - 原错误本身就是取消：保留**原对象**（identity）继续传播。
    - 原错误是普通错误、收尾阶段才引入取消：清理完成后按**原对象**传播那个取消，
      不能降级成普通失败提示，更不能静默吞掉取消。
    - 否则：没确认释放 → :class:`PointsHistoryReadUnreleased`；确认了才用
      ``fallback``（获取阶段用统一的不可用异常，不外泄底层异常原文）。
    """

    if not isinstance(exc, Exception):
        raise exc
    if lease.release_cancelled is not None:
        raise lease.release_cancelled from exc
    if not confirmed:
        raise PointsHistoryReadUnreleased(
            "points history read transaction could not be released"
        ) from exc
    raise fallback from exc


@contextlib.asynccontextmanager
async def consistent_read(session: AsyncSession) -> AsyncIterator[ReadLease]:
    """在**真实**读事务里执行若干条只读 SELECT，退出前**确认**结束它。

    复用 usage report 的实现：pysqlite/aiosqlite 默认不在 SELECT 前发 ``BEGIN``，
    SQLAlchemy 的 ``begin()`` 对它是 no-op，所以必须显式发。会话本来就在事务里
    时不重复发（那时本来就在同一快照里），也**不**替调用方提交。

    契约：正常退出、或因为其它异常退出时，本次读事务都**已经确认释放**——驱动层
    与 SQLAlchemy 层都没有残留事务，调用方可以安全地接着调 Telegram。两类例外：
    释放无法确认（抛 :class:`PointsHistoryReadUnreleased`），或进入时调用方**自己
    已经开着带未提交写的事务**（同样抛 ``PointsHistoryReadUnreleased``：本次读不
    接管它、也不声称可以发送，那个事务留给调用方/中间件自己提交或回滚）。取消
    （``CancelledError``）一律按原对象传播——包括「普通错误 + 收尾取消」的组合。
    """

    lease = ReadLease(session=session)
    try:
        # 注意：拿驱动连接本身也可能被取消，所以它也在受保护的获取范围内，
        # 不能放在 try 外面提前退出。
        lease.driver = await _driver_connection(session)
        lease.caller_owned = lease.driver is not None and bool(
            getattr(lease.driver, "in_transaction", False)
        )
        lease.owned = bool(await _open_report_read_snapshot(session))
    except BaseException as exc:
        confirmed = await lease.release()
        _finish_failed_read(
            exc,
            lease,
            confirmed=confirmed,
            # 获取阶段的任何普通失败都 fail closed：不把底层异常原文交给调用方/用户。
            fallback=PointsHistorySnapshotUnavailable(
                "points history read snapshot unavailable"
            ),
        )
    try:
        yield lease
    except BaseException as exc:
        confirmed = await lease.release()
        _finish_failed_read(exc, lease, confirmed=confirmed, fallback=exc)
    confirmed = await lease.release()
    if lease.release_cancelled is not None:
        # 释放阶段收到取消：连接已确认交还，但取消要按原对象传播。
        raise lease.release_cancelled
    if not confirmed:
        raise PointsHistoryReadUnreleased(
            "points history read transaction could not be released"
        )
    if lease.degraded:
        # 主路径 COMMIT 没成功、靠兜底 ROLLBACK 才交还连接：这一代读取不再当作
        # 可信结果发出去（也不注册翻页状态），只让调用方回友好失败提示。
        raise PointsHistorySnapshotUnavailable(
            "points history read snapshot released via fallback"
        )


# ---------------------------------------------------------------------------
# SQL：三表 UNION ALL → 聚合 / 分页
# ---------------------------------------------------------------------------

_HISTORY_SQL_HEAD = """
WITH combined AS (
    SELECT c.created_at AS at_ts,
           c.id         AS entry_id,
           0            AS source_rank,
           COALESCE(c.points, 0) AS points,
           'checkin'    AS source,
           ''           AS reason
      FROM member_checkins AS c
     WHERE c.group_id = :group_id
       AND c.user_id = :user_id
       AND c.created_at IS NOT NULL
       AND c.created_at >= :start_ts
       AND c.created_at <= :until_ts
       AND c.id <= :checkin_high_water
    UNION ALL
    SELECT a.created_at,
           a.id,
           1,
           COALESCE(a.points, 0),
           'award',
           COALESCE(a.reason, '')
      FROM member_point_awards AS a
     WHERE a.group_id = :group_id
       AND a.user_id = :user_id
       AND a.created_at >= :start_ts
       AND a.created_at <= :until_ts
       AND a.id <= :award_high_water
    UNION ALL
    SELECT s.created_at,
           s.id,
           2,
           -ABS(COALESCE(s.points, 0)),
           'spend',
           COALESCE(s.reason, '')
      FROM member_point_spends AS s
     WHERE s.group_id = :group_id
       AND s.user_id = :user_id
       AND s.created_at >= :start_ts
       AND s.created_at <= :until_ts
       AND s.id <= :spend_high_water
)
"""

_TOTALS_SQL = text(
    _HISTORY_SQL_HEAD
    + """
SELECT COALESCE(SUM(CASE WHEN points > 0 THEN points ELSE 0 END), 0) AS income,
       COALESCE(SUM(CASE WHEN points < 0 THEN -points ELSE 0 END), 0) AS spend,
       COUNT(*) AS total
  FROM combined
"""
).bindparams(
    bindparam("group_id", type_=BigInteger),
    bindparam("user_id", type_=BigInteger),
    bindparam("start_ts", type_=DateTime),
    bindparam("until_ts", type_=DateTime),
    bindparam("checkin_high_water", type_=BigInteger),
    bindparam("award_high_water", type_=BigInteger),
    bindparam("spend_high_water", type_=BigInteger),
)

_PAGE_SQL = text(
    _HISTORY_SQL_HEAD
    + """
SELECT at_ts, entry_id, source, points, reason
  FROM combined
 ORDER BY at_ts DESC, source_rank ASC, entry_id DESC
 LIMIT :row_limit OFFSET :row_offset
"""
).bindparams(
    bindparam("group_id", type_=BigInteger),
    bindparam("user_id", type_=BigInteger),
    bindparam("start_ts", type_=DateTime),
    bindparam("until_ts", type_=DateTime),
    bindparam("checkin_high_water", type_=BigInteger),
    bindparam("award_high_water", type_=BigInteger),
    bindparam("spend_high_water", type_=BigInteger),
    bindparam("row_limit", type_=Integer),
    bindparam("row_offset", type_=Integer),
)


def high_water_map(high_water: Sequence[tuple[str, int]]) -> dict[str, int]:
    """``(("checkin", 3), ...)`` → ``{"checkin": 3, ...}``（缺的按 0）。"""

    return {name: int(value or 0) for name, value in high_water}


def _sql_params(
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
    high_water: Sequence[tuple[str, int]],
) -> dict[str, Any]:
    waters = high_water_map(high_water)
    return {
        "group_id": int(group_id),
        "user_id": int(user_id),
        "start_ts": window.start,
        "until_ts": window.until,
        "checkin_high_water": waters.get("checkin", 0),
        "award_high_water": waters.get("award", 0),
        "spend_high_water": waters.get("spend", 0),
    }


async def read_high_water(
    session: AsyncSession, *, group_id: int, user_id: int
) -> tuple[tuple[str, int], ...]:
    """三表的当前最大 id（本群本人）。与分页筛选一起冻结，新账进不了旧查询。"""

    waters: list[tuple[str, int]] = []
    for name, model in (
        ("checkin", MemberCheckin),
        ("award", MemberPointAward),
        ("spend", MemberPointSpend),
    ):
        value = (
            await session.execute(
                select(func.max(model.id)).where(
                    model.group_id == group_id,
                    model.user_id == user_id,
                )
            )
        ).scalar()
        waters.append((name, int(value or 0)))
    return tuple(waters)


async def read_totals(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
    high_water: Sequence[tuple[str, int]],
) -> PointsHistorySummary:
    """一条聚合 SQL 拿到窗口内的收入/支出/笔数（在与分页同一个快照里）。"""

    row = (
        await session.execute(
            _TOTALS_SQL,
            _sql_params(
                group_id=group_id,
                user_id=user_id,
                window=window,
                high_water=high_water,
            ),
        )
    ).one()
    income = int(row.income or 0)
    spend = int(row.spend or 0)
    count = int(row.total or 0)
    return PointsHistorySummary(income=income, spend=spend, net=income - spend, count=count)


async def read_page_entries(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
    high_water: Sequence[tuple[str, int]],
    page: int,
) -> tuple[PointsHistoryEntry, ...]:
    """一条分页 SQL 拿当前页（``LIMIT``/``OFFSET``），不读全历史。"""

    index = max(0, int(page))
    params = _sql_params(
        group_id=group_id,
        user_id=user_id,
        window=window,
        high_water=high_water,
    )
    params["row_limit"] = MAX_PAGE_ENTRIES
    params["row_offset"] = index * MAX_PAGE_ENTRIES
    rows = (await session.execute(_PAGE_SQL, params)).all()

    entries: list[PointsHistoryEntry] = []
    for row in rows:
        at = _parse_local_datetime(row.at_ts)
        if at is None:
            raise PointsHistoryReadError(
                f"积分流水里有无法解析的写入时间（source={row.source!r}）"
            )
        source = str(row.source or "")
        entries.append(
            PointsHistoryEntry(
                source=source,
                entry_id=int(row.entry_id or 0),
                at=at,
                date_only=False,
                points=int(row.points or 0),
                label=label_for_reason(source, str(row.reason or "")),
            )
        )
    return tuple(entries)


async def open_history(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    now: datetime | None = None,
    page: int = 0,
    window: PointsHistoryWindow | None = None,
) -> PointsHistorySnapshot:
    """一次查询的全部口径：窗口、摘要、三表高水位、当前页。

    **调用方负责一致读**：要么已经在 :func:`consistent_read` 里（``/points``
    的余额与流水就是同一个快照），要么自己承担各条 SELECT 各拿一个快照的后果。
    """

    frozen = window or resolve_window(now)
    high_water = await read_high_water(
        session, group_id=group_id, user_id=user_id
    )
    summary = await read_totals(
        session,
        group_id=group_id,
        user_id=user_id,
        window=frozen,
        high_water=high_water,
    )
    index = clamp_page(page, summary.count)
    entries: tuple[PointsHistoryEntry, ...] = ()
    if summary.count:
        entries = await read_page_entries(
            session,
            group_id=group_id,
            user_id=user_id,
            window=frozen,
            high_water=high_water,
            page=index,
        )
    return PointsHistorySnapshot(
        window=frozen,
        entries=entries,
        summary=summary,
        high_water=high_water,
        page=index,
    )


async def load_history_page(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
    high_water: Sequence[tuple[str, int]],
    page: int,
) -> PointsHistoryPage:
    """按**冻结的**窗口与水位置读一页（翻页用），顺带重新核对摘要。

    摘要不一致说明冻结的口径已经和库里对不上（不该发生：水位把新账挡在外面，
    而账表只追加），调用方应当 fail closed 而不是把两代数字拼在一起。
    """

    summary = await read_totals(
        session,
        group_id=group_id,
        user_id=user_id,
        window=window,
        high_water=high_water,
    )
    index = clamp_page(page, summary.count)
    entries: tuple[PointsHistoryEntry, ...] = ()
    if summary.count:
        entries = await read_page_entries(
            session,
            group_id=group_id,
            user_id=user_id,
            window=window,
            high_water=high_water,
            page=index,
        )
    return PointsHistoryPage(page=index, summary=summary, entries=entries)


# ---------------------------------------------------------------------------
# 完整读取（诊断/测试用；生产翻页只走上面的 SQL 分页）
# ---------------------------------------------------------------------------


async def _load_checkin_entries(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
) -> list[PointsHistoryEntry]:
    entries: list[PointsHistoryEntry] = []

    rows = (
        await session.execute(
            select(
                MemberCheckin.id,
                MemberCheckin.points,
                MemberCheckin.created_at,
            ).where(
                MemberCheckin.group_id == group_id,
                MemberCheckin.user_id == user_id,
                MemberCheckin.created_at.is_not(None),
                MemberCheckin.created_at >= window.start,
                MemberCheckin.created_at <= window.until,
            )
        )
    ).all()
    for row in rows:
        entries.append(
            PointsHistoryEntry(
                source="checkin",
                entry_id=int(row.id),
                at=to_shanghai_naive(row.created_at),
                date_only=False,
                points=int(row.points or 0),
                label=_CHECKIN_LABEL,
            )
        )

    # 旧异常行没有 created_at：退回 checkin_date（只有日期），仍然要满足同一个
    # 窗口（00:00 起、不晚于查询时刻）。这类行极少，逐行在 Python 里比较。
    legacy_rows = (
        await session.execute(
            select(
                MemberCheckin.id,
                MemberCheckin.points,
                MemberCheckin.checkin_date,
            ).where(
                MemberCheckin.group_id == group_id,
                MemberCheckin.user_id == user_id,
                MemberCheckin.created_at.is_(None),
                MemberCheckin.checkin_date >= window.start.date().isoformat(),
                MemberCheckin.checkin_date <= window.today.isoformat(),
            )
        )
    ).all()
    for row in legacy_rows:
        entry = legacy_checkin_entry(row, window=window)
        if entry is not None:
            entries.append(entry)
    return entries


def legacy_checkin_entry(
    row: object, *, window: PointsHistoryWindow
) -> PointsHistoryEntry | None:
    """没有 ``created_at`` 的旧签到行 → 按 ``checkin_date`` 生成一笔。

    当前 schema 把 ``member_checkins.created_at`` 声明成 NOT NULL（见
    :class:`bot.db.models.MemberCheckin`），所以正常库里 INSERT 不出这类行；这条
    兼容路径留给更早的库结构：只用日期，渲染时明确写「仅日期」，绝不猜时分秒。
    坏数据（解析不出日期）返回 None，不猜。
    """

    day = _parse_local_date(getattr(row, "checkin_date", None))
    if day is None:
        return None
    at = datetime.combine(day, day_time.min)
    if not (window.start <= at <= window.until):
        return None
    return PointsHistoryEntry(
        source="checkin",
        entry_id=int(getattr(row, "id", 0) or 0),
        at=at,
        date_only=True,
        points=int(getattr(row, "points", 0) or 0),
        label=_CHECKIN_LABEL,
    )


async def _load_award_entries(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
) -> list[PointsHistoryEntry]:
    rows = (
        await session.execute(
            select(
                MemberPointAward.id,
                MemberPointAward.points,
                MemberPointAward.reason,
                MemberPointAward.created_at,
            ).where(
                MemberPointAward.group_id == group_id,
                MemberPointAward.user_id == user_id,
                MemberPointAward.created_at >= window.start,
                MemberPointAward.created_at <= window.until,
            )
        )
    ).all()
    return [
        PointsHistoryEntry(
            source="award",
            entry_id=int(row.id),
            at=to_shanghai_naive(row.created_at),
            date_only=False,
            points=int(row.points or 0),
            label=label_for_reason("award", str(row.reason or "")),
        )
        for row in rows
    ]


async def _load_spend_entries(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    window: PointsHistoryWindow,
) -> list[PointsHistoryEntry]:
    rows = (
        await session.execute(
            select(
                MemberPointSpend.id,
                MemberPointSpend.points,
                MemberPointSpend.reason,
                MemberPointSpend.created_at,
            ).where(
                MemberPointSpend.group_id == group_id,
                MemberPointSpend.user_id == user_id,
                MemberPointSpend.created_at >= window.start,
                MemberPointSpend.created_at <= window.until,
            )
        )
    ).all()
    return [
        PointsHistoryEntry(
            source="spend",
            entry_id=int(row.id),
            at=to_shanghai_naive(row.created_at),
            date_only=False,
            points=-abs(int(row.points or 0)),
            label=label_for_reason("spend", str(row.reason or "")),
        )
        for row in rows
    ]


def sort_entries(entries: Sequence[PointsHistoryEntry]) -> tuple[PointsHistoryEntry, ...]:
    """时间倒序 → 三表固定顺序 → id 倒序（SQL 分页用的是同一口径）。"""

    ordered = list(entries)
    ordered.sort(key=lambda item: item.entry_id, reverse=True)
    ordered.sort(key=lambda item: _SOURCE_DISPLAY_ORDER.get(item.source, len(_SOURCE_NAMES)))
    ordered.sort(key=lambda item: item.at, reverse=True)
    return tuple(ordered)


def summarize_entries(
    entries: Sequence[PointsHistoryEntry],
) -> PointsHistorySummary:
    """纯函数版摘要（与 SQL 聚合同口径：只看正/负号，不看来源）。"""

    income = sum(item.points for item in entries if item.points > 0)
    spend = sum(-item.points for item in entries if item.points < 0)
    return PointsHistorySummary(
        income=income, spend=spend, net=income - spend, count=len(entries)
    )


async def build_history_snapshot(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    now: datetime | None = None,
) -> PointsHistorySnapshot:
    """**完整**读取（诊断与测试用）：一次窗口内的所有流水。

    生产路径不调用它：``/points`` 与翻页都走 SQL 的 ``LIMIT``/``OFFSET`` 分页，
    从不在会话里留全量历史。这里保留它有两个用途——纯函数等价性对照，以及
    让「一致读」本身可被独立探针考验（它自己开一致读事务）。
    """

    frozen = resolve_window(now)
    async with consistent_read(session):
        high_water = await read_high_water(
            session, group_id=group_id, user_id=user_id
        )
        entries = list(
            await _load_checkin_entries(
                session, group_id=group_id, user_id=user_id, window=frozen
            )
        )
        entries.extend(
            await _load_award_entries(
                session, group_id=group_id, user_id=user_id, window=frozen
            )
        )
        entries.extend(
            await _load_spend_entries(
                session, group_id=group_id, user_id=user_id, window=frozen
            )
        )
    ordered = sort_entries(entries)
    return PointsHistorySnapshot(
        window=frozen,
        entries=ordered,
        summary=summarize_entries(ordered),
        high_water=high_water,
        page=0,
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_range_text(window: PointsHistoryWindow) -> str:
    """查询区间（带年份：30 天窗口可能跨年）。"""

    return (
        f"{window.start:%Y-%m-%d %H:%M} ~ {window.until:%Y-%m-%d %H:%M}"
    )


def _signed(value: int) -> str:
    """带符号显示，但 0 就写 0（不出现 ``+0`` / ``-0``）。"""

    return f"{value:+d}" if value else "0"


def render_entry_line(entry: PointsHistoryEntry) -> str:
    """一笔流水一行；动态文本全部 HTML 转义，整行有硬上限。"""

    if entry.date_only:
        at_text = f"{entry.at:%m-%d}（仅日期）"
    else:
        at_text = f"{entry.at:%m-%d %H:%M}"
    amount = _signed(entry.points)
    line = f"{at_text}　{amount} 分 · {html.escape(entry.label)}"
    return _safe_truncate_html(line, MAX_ENTRY_LINE_UNITS)


def render_history_header(snapshot: PointsHistorySnapshot) -> str:
    """摘要（在折叠区**外面**）。没有流水时也照常给出 0 值口径。"""

    window = snapshot.window
    summary = snapshot.summary
    return (
        f"<b>近 30 天流水</b>（{render_range_text(window)}，中国时间）\n"
        f"收入 <b>{_signed(summary.income)}</b> 分｜"
        f"支出 <b>{_signed(-summary.spend)}</b> 分｜"
        f"净变动 <b>{_signed(summary.net)}</b> 分｜共 {summary.count} 笔"
    )


# ---------------------------------------------------------------------------
# 有界会话描述符
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PointsHistorySession:
    """一次 ``/points`` 查询的**有界**描述符。

    这里只有 O(1) 的元信息：冻结窗口、固定摘要、三表高水位、冻结的头部文本和
    分页算术。页面文本与完整历史都不在会话里——翻页时按水位现查现渲染。
    """

    window: PointsHistoryWindow
    summary: PointsHistorySummary
    high_water: tuple[tuple[str, int], ...]
    head: str
    token: str = ""
    owner_user_id: int = 0
    group_id: int = 0
    message_id: int | None = None
    created_at: float = 0.0
    applied_page: int = -1
    applied_seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def page_count(self) -> int:
        """总页数（由冻结的笔数算出来，不存逐页边界）。"""

        return page_count_for(self.summary.count)

    def page_item_count(self, page: int) -> int:
        """这一页**应该**有几笔（纯算术：由冻结的笔数推出来）。"""

        index = clamp_page(page, self.summary.count)
        remaining = self.summary.count - index * MAX_PAGE_ENTRIES
        return max(0, min(MAX_PAGE_ENTRIES, remaining))

    def entry_count(self, page: int) -> int:
        """会话里这一页**已保留**的行数：F02 之后恒为 0。

        页面文本不再进会话（见 :meth:`page_bodies`），所以这里是 0；真实笔数由
        :meth:`page_item_count` 的算术和当次数据库读取给出。
        """

        return len(self.page_bodies[page]) if page < len(self.page_bodies) else 0

    def clamp(self, page: int) -> int:
        """把请求页夹到合法范围。"""

        return clamp_page(page, self.summary.count)

    @property
    def page_bodies(self) -> tuple[str, ...]:
        """恒为空：F02 之后会话里**不保存**任何页面文本。

        保留这个名字只为了让旧探针/旧断言能直接量到「持久化页面文本 = 0」，
        新代码不要读它。
        """

        return ()

    def is_collapsed(self) -> bool:
        """是否折叠（超过 :data:`COLLAPSE_ABOVE_ENTRIES` 笔）。"""

        return self.summary.count > COLLAPSE_ABOVE_ENTRIES

    def render(self, page: int, lines: Sequence[str], *, total: int | None = None) -> str:
        """用**当前读到的**行拼出这一页（头部与摘要在折叠区外）。"""

        count = self.summary.count if total is None else int(total)
        index = clamp_page(page, count)
        if count <= 0 or not lines:
            return f"{self.head}\n<blockquote>{EMPTY_HISTORY_TEXT}</blockquote>"
        tag = "<blockquote expandable>" if count > COLLAPSE_ABOVE_ENTRIES else "<blockquote>"
        rendered = f"{self.head}\n{tag}" + "\n".join(lines) + "</blockquote>"
        if page_count_for(count) > 1:
            rendered += f"\n第 {index + 1} / {page_count_for(count)} 页 · 点下方按钮翻页"
        return rendered

    def bounded_state_units(self) -> int:
        """会话里持久状态的大小（UTF-16 单元）——与流水条数无关。"""

        return _utf16_units(self.head) + 256


class PointsHistorySessionStore:
    """有界短 token 会话表：只存描述符，TTL 到点真删，条数有上限。

    这里**不是**余额缓存：每条会话只装某一次 ``/points`` 自己那份冻结描述符
    （O(1)，见 :data:`MAX_STATE_UNITS`），既不存页面文本也不存完整历史；新的
    ``/points`` 一律重读三本台账。
    """

    def __init__(self, *, ttl_seconds: int = 600, max_sessions: int = 128) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_sessions = max(1, int(max_sessions))
        self._sessions: dict[str, PointsHistorySession] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def __len__(self) -> int:
        return len(self._sessions)

    def _prune(self, now: float) -> None:
        """先按 TTL 真删过期，再按创建时间丢最旧，保证总占用有界。"""

        expired = [
            token
            for token, item in self._sessions.items()
            if now - float(item.created_at) >= self._ttl_seconds
        ]
        for token in expired:
            self._sessions.pop(token, None)
        overflow = len(self._sessions) - self._max_sessions
        if overflow > 0:
            ordered = sorted(
                self._sessions.items(), key=lambda pair: float(pair[1].created_at)
            )
            for token, _item in ordered[:overflow]:
                self._sessions.pop(token, None)

    def new_token(self) -> str:
        """一个当前没被占用的 8 位十六进制 token（callback_data 够短）。"""

        while True:
            token = secrets.token_hex(4)
            if token not in self._sessions:
                return token

    def create(
        self,
        session: PointsHistorySession,
        *,
        owner_user_id: int,
        group_id: int,
        message_id: int | None = None,
        now: float | None = None,
    ) -> str:
        """注册一条会话；``message_id`` 必须是真的发出去了的那条消息。"""

        moment = time.time() if now is None else float(now)
        self._prune(moment)
        if not session.token:
            session.token = self.new_token()
        session.owner_user_id = int(owner_user_id)
        session.group_id = int(group_id)
        if message_id is not None:
            session.message_id = int(message_id)
        session.created_at = moment
        self._sessions[session.token] = session
        self._prune(moment)
        return session.token

    def get(self, token: str, *, now: float | None = None) -> PointsHistorySession | None:
        """按 token 取会话（顺带清过期）；过期/被淘汰一律返回 None。"""

        moment = time.time() if now is None else float(now)
        self._prune(moment)
        return self._sessions.get(str(token or ""))

    def discard(self, token: str) -> None:
        self._sessions.pop(str(token or ""), None)

    def retained_state_units(self) -> int:
        """当前所有会话的持久状态总量（有界；用来证明不随 N 增长）。"""

        return sum(item.bounded_state_units() for item in self._sessions.values())


def freeze_history(*, snapshot: PointsHistorySnapshot, head: str) -> PointsHistorySession:
    """把一次查询的读取结果冻成一个有界描述符（不保存任何页面文本）。"""

    frozen_head = f"{head.rstrip()}\n\n{render_history_header(snapshot)}"
    record = PointsHistorySession(
        window=snapshot.window,
        summary=snapshot.summary,
        high_water=snapshot.high_water,
        head=frozen_head,
    )
    if record.bounded_state_units() > MAX_STATE_UNITS:
        raise PointsHistoryReadError(
            "积分流水的会话状态超出有界上限，拒绝保存不完整的状态"
        )
    return record
