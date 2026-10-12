"""积分商店：头衔（7 天 / 30 天）、置顶求助（6 小时）、抽奖（5 分）。

**只复用已有的三本流水，不另起一套积分体系**：可用积分永远等于
``bot.services.checkin.available_from_ledgers(签到 + 奖励 − 消费)``，扣分写
``member_point_spends``，退分/发奖写 ``member_point_awards``，两边都靠唯一索引
（``(group_id, user_id, ref)``）保证"同一笔交易只记一次账"。**绝不伪造签到行**：
连续签到天数是从 ``member_checkins`` 的日期集合倒着数出来的，塞假签到会直接把
用户的连击算错。

几条容易写错、这里刻意钉死的不变量：

1. **先扣分，再调 Telegram**。Telegram 那边失败（没权限、消息没了、头衔被拒）就
   自动退款：往奖励流水插一行 ``shop-refund:<原 ref>``，用户的钱不会凭空消失。
2. **续费是往后延，不是重新开始**。旧头衔还没到期时再买，从旧到期时间往后加时长。
3. **扣分用稳定且唯一的 ref**（``shop-tag-7d:<群>:<人>:<时间戳>``）。重复执行同一笔
   购买会被唯一索引挡住，不会重复扣分。
4. 所有 Telegram 调用都包在 try/except 里并且有超时：商店里的任何异常都只影响
   这一条请求，不会把群里正常的审核/回复流程带崩。

到期清理（清头衔 / 取消置顶）的实现在本文件，入口有两个：常驻的
:class:`ShopExpiryService`（随机器人启动）和运维用的 CLI ``bot.tools.shop_expire``
（``--dry-run`` 自检）。两边走的是同一个 :func:`expire_due_entitlements`。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Collection, Mapping, Sequence

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import (
    AuthorizedGroup,
    MemberEntitlement,
    MemberPointAward,
    MemberPointSpend,
)
from bot.services.background_health import record_background_failure
from bot.services import policy_runtime
from bot.services.checkin import available_points, local_today, spend_points
from bot.services.policy_runtime import EconomySnapshot, pinned_section
from bot.services.operations_guard import (
    PersistedGuard,
    guard_predicate,
    verify_guard,
)
from bot.services.operations_receipts import (
    BusinessReceipt,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
    TASK_SHOP_EXPIRE,
    ambiguous_send_failure,
    claim_business_dispatch,
    definitely_not_delivered,
    release_business_dispatch,
    load_receipt,
    receipt_key,
    record_receipt,
)
from bot.utils.timezone import now_shanghai_naive, now_shanghai_naive_precise

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 商品、价格与时长 —— 价格是产品定好的，改这里等于改价，不要动
# ---------------------------------------------------------------------------

TAG_PRICE_7D = 30
TAG_DAYS_7D = 7
TAG_PRICE_30D = 80
TAG_DAYS_30D = 30

PIN_PRICE = 20
PIN_HOURS = 6

LOTTERY_PRICE = 5
LOTTERY_DAILY_LIMIT = 10

#: 权益类型（写进 member_entitlements.kind）
KIND_TAG = "tag"
KIND_PIN = "pin"

#: 流水上的 reason（入库便于审计，不是给用户看的）
SPEND_REASON_TAG = "shop_tag"
SPEND_REASON_PIN = "shop_pin"
SPEND_REASON_LOTTERY = "shop_lottery"
AWARD_REASON_REFUND = "shop_refund"
AWARD_REASON_LOTTERY = "shop_lottery_prize"

#: 头衔硬限制（Telegram 原生规则）：0-16 个字符、不能含 emoji
TAG_MAX_LENGTH = 16
#: 防冒充小黑名单：命中任意一个就拒绝（大小写不敏感）
TAG_BLOCKLIST = ("管理员", "群主", "admin", "官方", "客服", "机器人", "bot")
#: 头衔文字后面的时长标记：``/tag 文字 30天`` 买 30 天的长租
TAG_LONG_MARKERS = ("30天", "30d", "--30")

_TELEGRAM_CALL_TIMEOUT_SECONDS = 10.0
_SHOP_EXPIRY_CHECK_SECONDS = 300.0
_SHOP_EXPIRY_PASS_DEADLINE_SECONDS = 120.0
_SHOP_EXPIRY_BATCH_LIMIT = 200
#: 到期撤销失败后的重试间隔：把 expires_at 往后推这么多，下次扫描再试。
#: 既不会每轮都撞同一条失败记录，也不会让付费头衔永远留在群里没人管（F-052）。
_SHOP_EXPIRY_RETRY_SECONDS = 900

def _econ():
    """当前生效的经济参数快照（未绑定时 = schema 默认值 = 改造前逐字相同）。"""

    return policy_runtime.economy_policy()


_ADMIN_STATUSES = ("administrator", "creator")
#: 取消置顶时这些报错说明"本来就没置顶"，当成功处理
_ALREADY_UNPINNED_MARKERS = (
    "message is not pinned",
    "message not found",
    "message to unpin not found",
    "message to be unpinned not found",
    "message id invalid",
)


# ---------------------------------------------------------------------------
# 头衔文字校验
# ---------------------------------------------------------------------------

#: emoji 用 unicode 区段判断，不要用正则硬编码几个常见的表情。
#: 宁可少收几个花哨符号，也不要放过一个表情让 Telegram 拒掉整次购买。
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x20E3, 0x20E3),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1F0FF),
    (0x1F100, 0x1F1FF),
    (0x1F200, 0x1FAFF),
    (0xFE00, 0xFE0F),
    (0x200D, 0x200D),
)


def contains_emoji(text: object) -> bool:
    """文字里有没有 emoji（按 unicode 区段判断）。"""

    for char in str(text or ""):
        code = ord(char)
        for start, end in _EMOJI_RANGES:
            if start <= code <= end:
                return True
    return False


@dataclass(frozen=True, slots=True)
class TagCheck:
    """一次头衔文字校验的结果。"""

    ok: bool
    text: str = ""
    reason: str = ""


def check_tag_text(
    raw: object, *, policy: EconomySnapshot | None = None
) -> TagCheck:
    """校验头衔文字；不合格时给一句用户看得懂的原因（不扣分）。"""

    econ = policy or _econ()
    text = str(raw or "").strip()
    if not text:
        return TagCheck(False, reason="头衔不能是空白，请输入 1-16 个字。")
    if len(text) > econ.tag_max_length:
        return TagCheck(
            False,
            reason=f"头衔最多 {econ.tag_max_length} 个字，你这条有 {len(text)} 个字。",
        )
    if contains_emoji(text):
        return TagCheck(False, reason="头衔里不能有表情符号。")
    if any(unicodedata.category(char).startswith("C") for char in text):
        return TagCheck(False, reason="头衔里不能有换行或控制字符。")
    lowered = text.lower()
    for word in TAG_BLOCKLIST:
        if word in lowered:
            return TagCheck(
                False,
                reason=f"头衔里不能包含「{word}」，容易让人误以为是管理员或官方。",
            )
    return TagCheck(True, text=text)


@dataclass(frozen=True, slots=True)
class TagRequest:
    """``/tag`` 的参数：头衔文字 + 买几天，以及对应的价钱。"""

    text: str = ""
    days: int = 0      # 0 = 用当前配置里的默认档位（见 parse_tag_request）
    price: int = 0
    error: str = ""


def tag_price(days: int, *, policy: EconomySnapshot | None = None) -> int:
    """长租档（``days >= tag_days_30d``）与短租档的价格。"""

    econ = policy or _econ()
    return econ.tag_price_30d if int(days) >= econ.tag_days_30d else econ.tag_price_7d


def parse_tag_request(raw: object, *, policy: EconomySnapshot | None = None) -> TagRequest:
    """解析 ``/tag`` 后面的文字：末尾的 ``30天`` / ``30d`` / ``--30`` 表示买长租。

    一次解析只用一份快照：菜单里显示的价与实际扣的价永远一致。
    """

    econ = policy or _econ()
    body = str(raw or "").strip()
    days = econ.tag_days_7d
    parts = body.split()
    if len(parts) == 1 and parts[0].lower() in TAG_LONG_MARKERS:
        # 只写了时长没写头衔：按"忘了写文字"提示，不要真把头衔设成 "30天"
        return TagRequest(
            days=econ.tag_days_30d,
            price=econ.tag_price_30d,
            error="请先写头衔文字，例如：/tag 摸鱼冠军 30天",
        )
    if len(parts) > 1 and parts[-1].lower() in TAG_LONG_MARKERS:
        days = econ.tag_days_30d
        body = " ".join(parts[:-1])
    check = check_tag_text(body, policy=econ)
    if not check.ok:
        return TagRequest(
            days=days, price=tag_price(days, policy=econ), error=check.reason
        )
    return TagRequest(
        text=check.text, days=days, price=tag_price(days, policy=econ)
    )


# ---------------------------------------------------------------------------
# 抽奖奖池（概率是定死的，不要自行调整）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LotteryPrize:
    """奖池里的一项：``weight`` 是万分比。"""

    points: int
    weight: int
    label: str


#: 39% 谢谢参与 / 20% 3 分 / 16% 5 分 / 10% 8 分 / 10% 12 分 / 4% 40 分 / 1% 100 分
#: （2026-10-01 管理员指定：**期望值正好 6.00 分/次**。抽奖成本 5 分 → 玩家长期平均
#: 每次净赚 1.00 分、每天 10 次约净赚 10 分；仍是净发放，要回收只需上调"谢谢参与"。）
LOTTERY_TABLE: tuple[LotteryPrize, ...] = (
    LotteryPrize(0, 3900, "谢谢参与"),
    LotteryPrize(3, 2000, "3 分"),
    LotteryPrize(5, 1600, "5 分"),
    LotteryPrize(8, 1000, "8 分"),
    LotteryPrize(12, 1000, "12 分"),
    LotteryPrize(40, 400, "40 分"),
    LotteryPrize(100, 100, "100 分"),
)
LOTTERY_TOTAL_WEIGHT = 10000


def lottery_table(
    *, policy: EconomySnapshot | None = None
) -> tuple[LotteryPrize, ...]:
    """当前奖池。``weight`` 是**相对权重**，总权重由表内求和派生（不另存第二份）。"""

    econ = policy or _econ()
    return tuple(
        LotteryPrize(points=item.points, weight=item.weight, label=item.label)
        for item in econ.lottery_prizes
    )


def expected_lottery_value(*, policy: EconomySnapshot | None = None) -> float:
    """长期期望值（默认配置下正好 6.00 分/次）。"""

    econ = policy or _econ()
    total = econ.lottery_total_weight
    if not total:
        return 0.0
    table = lottery_table(policy=econ)
    return sum(prize.points * prize.weight for prize in table) / total


def draw_prize(randbelow=None, *, policy: EconomySnapshot | None = None) -> LotteryPrize:
    """按奖池权重开一次奖；随机源默认是加密安全的 ``secrets.randbelow``。

    随机算法逐字未变：仍是「一次 ``randbelow(总权重)`` + 前缀和」，只是权重表来自
    快照而不是模块常量。财务幂等（``lottery_spend_ref`` / ``lottery_prize_ref``）
    完全不受影响。
    """

    econ = policy or _econ()
    table = lottery_table(policy=econ)
    roller = randbelow if randbelow is not None else secrets.randbelow
    roll = int(roller(econ.lottery_total_weight))
    if roll < 0:
        roll = 0
    accumulated = 0
    for prize in table:
        accumulated += prize.weight
        if roll < accumulated:
            return prize
    # 只有随机源返回 >= 总权重 时才可能走到这里
    return table[-1]


# ---------------------------------------------------------------------------
# 幂等键（ref）：稳定、唯一、且短到能塞进现有的 String(64) 列
# ---------------------------------------------------------------------------

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    number = int(value)
    if number <= 0:
        return "0"
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 36)
        digits.append(_BASE36_DIGITS[remainder])
    return "".join(reversed(digits))


def _purchase_now() -> datetime:
    """购买幂等键用的时钟：**不截断微秒**。

    :func:`now_shanghai_naive` 带着 ``.replace(microsecond=0)``，用它生成的
    stamp 毫秒位恒为 ``00``（毫秒那一段是死代码），同一秒内的第二笔同款购买会
    落到同一个 ref，被 :func:`spend_points` 的 ``ON CONFLICT DO NOTHING`` 挡成
    「这笔购买已经处理过了」。全局 ``now_shanghai`` 承担"自然日 / 整分"语义，
    不能动，所以只在这里换成不截断的时钟。
    """

    return now_shanghai_naive_precise()


def purchase_stamp(now: datetime | None = None) -> str:
    """本次购买的时间戳：base36 秒（6 位）+ 微秒（4 位）。

    一起购买的时间戳必须**稳定**（同一笔交易重试时用同一个 ref，唯一索引才能挡住
    重复扣分）且**唯一**（同一个人前后两次购买不能撞 key）。所以它是 ``now`` 的
    **纯函数**——不要在这里掺任何进程内计数器，那会把"同一笔交易重试"也变成新 ref。

    旧实现是「秒 + 毫秒」，但生产路径的时钟 :func:`now_shanghai_naive` 抹掉了
    微秒，毫秒位因此恒为 ``00``，实际只有 1 秒精度——它 docstring 里"同一个人
    在同一毫秒里下两单是不可能的"这个前提从未成立，同一秒内的第二笔 ``/tag``
    会被误判成重投。现在改用不截断的时钟并直接编码微秒。
    """

    moment = now if isinstance(now, datetime) else _purchase_now()
    seconds = _base36(int(moment.timestamp()))
    micros = _base36(int(moment.microsecond)).rjust(4, "0")
    return f"{seconds}{micros}"


def tag_spend_ref(
    *, group_id: int, user_id: int, days: int, stamp: str
) -> str:
    sku = "shop-tag-30d" if int(days) >= _econ().tag_days_30d else "shop-tag-7d"
    return f"{sku}:{int(group_id)}:{int(user_id)}:{stamp}"


def pin_spend_ref(*, group_id: int, user_id: int, stamp: str) -> str:
    return f"shop-pin-6h:{int(group_id)}:{int(user_id)}:{stamp}"


def lottery_spend_ref(*, group_id: int, user_id: int, day: str, stamp: str) -> str:
    """抽奖的消费 ref 里带上本地自然日：每日次数上限直接按前缀 COUNT 就能算出来。"""

    return f"lottery:{int(group_id)}:{int(user_id)}:{day}:{stamp}"


def lottery_prize_ref(*, group_id: int, user_id: int, stamp: str) -> str:
    return f"lottery-prize:{int(group_id)}:{int(user_id)}:{stamp}"


def refund_ref(original_ref: str) -> str:
    """退款流水的 ref：``shop-refund:<原 ref>``（同样靠唯一索引保证只退一次）。"""

    return f"shop-refund:{original_ref}"


def challenge_spend_ref(verification_id: int, attempt: int = 0) -> str:
    """「花积分免除质询」的消费 ref（幂等键）。

    ``attempt=0`` 就是历史格式 ``challenge:<质询ID>``；``attempt>0`` 加一个后缀，
    用于**上一次扣分已经被退款**之后的重试：唯一索引让同一个 ref 只能扣一次，
    没有新 ref 的话成员退了款也再点不动这个按钮（还会误报"积分不足"）。
    """

    base = f"challenge:{int(verification_id)}"
    return base if int(attempt) <= 0 else f"{base}#{int(attempt)}"


def lottery_day(now: object = None) -> str:
    """抽奖次数按本地（Asia/Shanghai）自然日算，格式与 ref 里的片段一致。"""

    return local_today(now).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 入账：退款 / 发奖（append-only，幂等）
# ---------------------------------------------------------------------------


async def award_points(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    reason: str,
    ref: str,
) -> bool:
    """往奖励流水加一笔；同一 ref 已存在时返回 False（不抛异常）。"""

    amount = int(points)
    if amount <= 0:
        return False
    # 与 spend_points（bot/services/checkin.py）完全同构：原生
    # ``ON CONFLICT DO NOTHING``，不包 ``begin_nested()``。pysqlite / aiosqlite
    # 只在第一条 DML 之前才发出 BEGIN，SAVEPOINT 不是 DML，所以最外层的
    # ``RELEASE SAVEPOINT`` 按 SQLite 语义就是 COMMIT——调用方的
    # ``session.rollback()`` 撤不回这里的写入（GAP-D1 §3.1 实测）。
    # 后果：_refund_and_reload 里「退款 + 权益还原」不再是原子的，退款先落库、
    # 权益还原再抛错时，用户白拿退款还留着权益，UI 却报「退款也失败了」。
    # 幂等仍由唯一索引 (group_id, user_id, ref) 保证，rowcount==1 才是"真发了"。
    statement = (
        sqlite_insert(MemberPointAward)
        .values(
            group_id=int(group_id),
            user_id=int(user_id),
            points=amount,
            reason=str(reason or "")[:64],
            ref=str(ref)[:64],
            # F-050 同口径：Core 的 INSERT 不会套用 Python 侧列 default，
            # 而 server_default 在 SQLite 上是 UTC 的 CURRENT_TIMESTAMP，
            # 所以 created_at 必须显式带上本地（Asia/Shanghai）朴素时间。
            created_at=now_shanghai_naive(),
        )
        .on_conflict_do_nothing(index_elements=["group_id", "user_id", "ref"])
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def refund_points(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    original_ref: str,
) -> bool:
    """退款：把扣掉的分加回去（``shop-refund:<原 ref>``，重复调用不会退两次）。"""

    return await award_points(
        session,
        group_id=group_id,
        user_id=user_id,
        points=points,
        reason=AWARD_REASON_REFUND,
        ref=refund_ref(original_ref),
    )


# ---------------------------------------------------------------------------
# 生效中的权益（member_entitlements）
# ---------------------------------------------------------------------------

#: 同一个人同一类权益的购买锁。余额检查、续费时长计算和随后的事务性写入必须
#: 是一个整体：两个并发的 /top 都会在各自事务里看到"还没有生效中的置顶"。
_PURCHASE_LOCKS: dict[tuple[int, int, str], asyncio.Lock] = {}


def _purchase_lock(*, group_id: int, user_id: int, kind: str) -> asyncio.Lock:
    """取"这个人这类权益"的购买锁（进程内；同一个群同一个人串行购买）。

    锁覆盖"读旧状态 → 扣分 → 落权益 → 提交"全程，所以第二个请求进来时看到的是
    第一个请求已经提交的结果（生效中的置顶 / 已经延长的到期时间）。
    """

    key = (int(group_id), int(user_id), str(kind))
    lock = _PURCHASE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PURCHASE_LOCKS[key] = lock
    return lock


async def active_entitlement(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
) -> MemberEntitlement | None:
    """取某人某类商品的生效中权益（没有就是 None）。"""

    rows = await session.execute(
        select(MemberEntitlement)
        .where(
            MemberEntitlement.group_id == int(group_id),
            MemberEntitlement.user_id == int(user_id),
            MemberEntitlement.kind == str(kind),
        )
        .limit(1)
    )
    return rows.scalars().first()


async def tag_text_taken(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    text: str,
) -> bool:
    """这个头衔是不是已经被**别的**群友占了（大小写不敏感）。

    自己续费用同一个头衔是允许的，所以把自己排除掉。
    """

    wanted = str(text or "").strip().lower()
    if not wanted:
        return False
    rows = await session.execute(
        select(MemberEntitlement.id)
        .where(
            MemberEntitlement.group_id == int(group_id),
            MemberEntitlement.kind == KIND_TAG,
            MemberEntitlement.user_id != int(user_id),
            func.lower(MemberEntitlement.payload) == wanted,
        )
        .limit(1)
    )
    return rows.first() is not None


async def upsert_entitlement(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
    payload: str,
    ref: str,
    expires_at: datetime,
    now: datetime,
) -> None:
    """写入/更新一条生效中的权益（一人一项，靠唯一索引兜底）。

    到期时间**只允许往前走**：续费时长已经付过钱，任何以竞态前状态算出来的旧值
    都不许把它覆盖回去（F-006）。调用方的"读旧到期时间 → 算新到期时间 → 写回"
    由 :func:`_purchase_lock` 串行化；这里再做一层单调保护，保证晚到的写入不会
    缩短已付费的有效期。
    """

    row = await active_entitlement(
        session, group_id=group_id, user_id=user_id, kind=kind
    )
    if row is None:
        # 和 award_points 同理：插入分支不能包 begin_nested()，否则这一行的
        # INSERT 会被 SAVEPOINT 的 RELEASE 提前提交，调用方 rollback 撤不回来
        # （"权益已落盘、扣分回滚" = 白送）。ON CONFLICT DO NOTHING 不抛异常、
        # 不改变事务边界，幂等由唯一索引 (group_id, user_id, kind) 保证。
        inserted = await session.execute(
            sqlite_insert(MemberEntitlement)
            .values(
                group_id=int(group_id),
                user_id=int(user_id),
                kind=str(kind),
                payload=str(payload)[:255],
                ref=str(ref)[:64],
                created_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["group_id", "user_id", "kind"])
        )
        if int(getattr(inserted, "rowcount", 0) or 0) == 1:
            return
        # 并发插入（同一个人的两次购买本该被 _purchase_lock 串行化，这里兜底）
        row = await active_entitlement(
            session, group_id=group_id, user_id=user_id, kind=kind
        )
    if row is None:
        return
    row.payload = str(payload)[:255]
    row.ref = str(ref)[:64]
    current = row.expires_at
    if current is None or expires_at >= current:
        row.expires_at = expires_at


async def next_expiry(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
    duration: timedelta,
    now: datetime,
) -> datetime:
    """下一次到期的时刻：还在有效期内就**从旧到期时间往后延**，不重置成"从现在起"。"""

    existing = await active_entitlement(
        session, group_id=group_id, user_id=user_id, kind=kind
    )
    base = now
    if existing is not None and existing.expires_at is not None:
        current = existing.expires_at
        if current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        if current > now:
            base = current
    return base + duration


def minutes_left(expires_at: datetime, now: datetime) -> int:
    """还剩几分钟（向上取整，至少 1 分钟）。"""

    seconds = (expires_at - now).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 59) // 60))


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    """购买前的一条权益快照，用于 Telegram 调用失败时精确回滚（F-053）。

    ``exists=False`` 表示买之前这个人没有这件权益；续费时要连旧到期时间一起
    还原，不能简单删行——那会把上一笔已经付过钱的有效期一起吞掉。
    """

    exists: bool = False
    payload: str = ""
    ref: str = ""
    expires_at: datetime | None = None


async def entitlement_snapshot(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
) -> EntitlementSnapshot:
    """读取当前权益，供 :func:`restore_entitlement` 回滚用。"""

    row = await active_entitlement(
        session, group_id=group_id, user_id=user_id, kind=kind
    )
    if row is None:
        return EntitlementSnapshot()
    return EntitlementSnapshot(
        exists=True,
        payload=str(row.payload or ""),
        ref=str(row.ref or ""),
        expires_at=row.expires_at,
    )


async def restore_entitlement(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
    snapshot: EntitlementSnapshot,
) -> None:
    """把权益写回 ``snapshot`` 的样子（回滚半途失败的购买，F-053）。

    扣费与权益现在同一个事务提交；Telegram 侧失败时两件事必须一起撤销：
    退款（:func:`refund_points`）+ 这里的权益还原，由调用方在同一次 commit 里落库。
    """

    row = await active_entitlement(
        session, group_id=group_id, user_id=user_id, kind=kind
    )
    if row is None:
        if not snapshot.exists:
            return
        session.add(
            MemberEntitlement(
                group_id=int(group_id),
                user_id=int(user_id),
                kind=str(kind),
                payload=str(snapshot.payload)[:255],
                ref=str(snapshot.ref)[:64],
                expires_at=snapshot.expires_at,
            )
        )
        return
    if not snapshot.exists:
        await session.delete(row)
        return
    row.payload = str(snapshot.payload)[:255]
    row.ref = str(snapshot.ref)[:64]
    row.expires_at = snapshot.expires_at


# ---------------------------------------------------------------------------
# Telegram 侧调用（全部有超时 + 异常转换，绝不把异常甩给调用方之外的流程）
# ---------------------------------------------------------------------------


async def _set_member_tag(bot: object, chat_id: int, user_id: int, tag: str) -> None:
    method = getattr(bot, "set_chat_member_tag", None)
    if not callable(method):
        raise RuntimeError("Telegram 客户端不支持设置头衔")
    async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
        await method(chat_id=int(chat_id), user_id=int(user_id), tag=str(tag or ""))


async def _pin_message(bot: object, chat_id: int, message_id: int) -> None:
    method = getattr(bot, "pin_chat_message", None)
    if not callable(method):
        raise RuntimeError("Telegram 客户端不支持置顶")
    async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
        await method(
            chat_id=int(chat_id),
            message_id=int(message_id),
            disable_notification=True,
        )


async def _unpin_message(bot: object, chat_id: int, message_id: int) -> bool:
    """取消置顶；"本来就没置顶 / 消息已删"当成功（到期清理要能继续往下走）。"""

    if int(message_id) <= 0:
        return False
    method = getattr(bot, "unpin_chat_message", None)
    if not callable(method):
        raise RuntimeError("Telegram 客户端不支持取消置顶")
    try:
        async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
            await method(chat_id=int(chat_id), message_id=int(message_id))
        return True
    except TelegramBadRequest as exc:
        detail = " ".join(str(exc).replace("_", " ").lower().split())
        if any(marker in detail for marker in _ALREADY_UNPINNED_MARKERS):
            log.debug(
                "shop pin already gone | group=%s message=%s", chat_id, message_id
            )
            return True
        raise


async def _member_is_admin_or_owner(
    bot: object, group_id: int, user_id: int
) -> bool | None:
    """True=管理员/群主，False=普通成员，None=查不出来（查不出来就不卖，不扣分）。"""

    getter = getattr(bot, "get_chat_member", None)
    if not callable(getter):
        return None
    try:
        async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
            member = await getter(chat_id=int(group_id), user_id=int(user_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "shop member status lookup failed | group=%s user=%s",
            group_id,
            user_id,
            exc_info=True,
        )
        return None
    status = str(getattr(member, "status", "") or "").lower()
    return status.rsplit(".", 1)[-1] in _ADMIN_STATUSES


# ---------------------------------------------------------------------------
# 购买结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShopReply:
    """一次商店操作的结果：``status`` 给测试和日志看，``text`` 直接发给用户。"""

    status: str
    text: str
    available: int = 0
    refunded: bool = False
    expires_at: datetime | None = None


def _insufficient_text(*, price: int, available: int) -> str:
    return (
        f"积分不够：这个要 {price} 分，你当前可用 {available} 分，"
        f"还差 {max(0, price - available)} 分。发 /checkin 每天签到可以攒分。"
    )


def _tag_usage_text() -> str:
    return (
        "用法：在群里发 /tag 你的头衔\n"
        "例：/tag 摸鱼冠军（30 分，7 天）\n"
        "例：/tag 摸鱼冠军 30天（80 分，30 天）\n"
        "头衔 1-16 个字，不能带表情，不能和别人重名。"
    )


def _pin_usage_text() -> str:
    return (
        "用法：先长按你自己发的那条消息 → 回复 → 发送 /top，"
        f"机器人会把那条消息置顶 {_econ().pin_hours} 小时（{_econ().pin_price} 分）。"
    )


# ---------------------------------------------------------------------------
# 买头衔
# ---------------------------------------------------------------------------


async def buy_member_tag(
    session: AsyncSession,
    *,
    bot: object,
    group_id: int,
    user_id: int,
    raw_text: object,
    now: datetime | None = None,
) -> ShopReply:
    """``/tag <文字>``：校验 → 扣分 → 设头衔 → 记到期时间；Telegram 失败就退款。

    同一个人同一时刻只允许一笔头衔购买在跑：否则两个并发请求都会基于"竞态前的
    到期时间"算续费时长，第二笔买到的天数会被第一笔的写入覆盖掉（F-006）。
    """

    # 一次购买钉一份价格快照：菜单、扣费、退款、回执全部用同一份。
    with pinned_section("economy"):
        async with _purchase_lock(group_id=group_id, user_id=user_id, kind=KIND_TAG):
            return await _buy_member_tag_locked(
                session,
                bot=bot,
                group_id=group_id,
                user_id=user_id,
                raw_text=raw_text,
                now=now,
            )


async def _buy_member_tag_locked(
    session: AsyncSession,
    *,
    bot: object,
    group_id: int,
    user_id: int,
    raw_text: object,
    now: datetime | None = None,
) -> ShopReply:
    """``buy_member_tag`` 的加锁实现体（调用方必须已经持有购买锁）。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    gid, uid = int(group_id), int(user_id)
    request = parse_tag_request(raw_text)
    if request.error:
        return ShopReply(
            "invalid_tag",
            request.error + "\n" + _tag_usage_text(),
            available=await available_points(session, group_id=gid, user_id=uid),
        )

    available = await available_points(session, group_id=gid, user_id=uid)
    if await tag_text_taken(session, group_id=gid, user_id=uid, text=request.text):
        return ShopReply(
            "duplicate_tag",
            f"「{escape(request.text)}」已经被群里其他人用了，换一个吧。"
            f"当前可用 {available} 分，这次没有扣分。",
            available=available,
        )
    is_admin = await _member_is_admin_or_owner(bot, gid, uid)
    if is_admin is None:
        return ShopReply(
            "unknown_member",
            f"暂时查不到你的群成员身份，稍等一下再试。当前可用 {available} 分，这次没有扣分。",
            available=available,
        )
    if is_admin:
        return ShopReply(
            "admin_blocked",
            "管理员和群主的头衔由群设置统一管理，积分商店只给普通成员开放。"
            f"当前可用 {available} 分，这次没有扣分。",
            available=available,
        )
    if available < request.price:
        return ShopReply(
            "insufficient",
            _insufficient_text(price=request.price, available=available),
            available=available,
        )

    # 幂等键的时钟不能是 ``moment``：它按"整秒"口径取（now_shanghai_naive
    # 抹掉微秒），同一秒内的第二笔会落进同一个 ref。``now=None`` 时让
    # purchase_stamp 自己去拿不截断的时钟。
    stamp = purchase_stamp(now)
    ref = tag_spend_ref(group_id=gid, user_id=uid, days=request.days, stamp=stamp)
    try:
        expires_at = await next_expiry(
            session,
            group_id=gid,
            user_id=uid,
            kind=KIND_TAG,
            duration=timedelta(days=request.days),
            now=moment,
        )
    except Exception:
        log.exception("shop tag expiry lookup failed | group=%s user=%s", gid, uid)
        return ShopReply(
            "failed",
            f"商店暂时不可用，请稍后再试。当前可用 {available} 分，这次没有扣分。",
            available=available,
        )

    # F-053：先取一份"购买前"的权益快照。扣费和权益行要在**同一个事务**里提交，
    # Telegram 侧失败时再用这份快照把权益精确还原（续费场景不能简单删行）。
    previous_entitlement = await entitlement_snapshot(
        session, group_id=gid, user_id=uid, kind=KIND_TAG
    )

    charged = await spend_points(
        session,
        group_id=gid,
        user_id=uid,
        points=request.price,
        reason=SPEND_REASON_TAG,
        ref=ref,
    )
    if not charged:
        return ShopReply(
            "replay",
            "这笔购买已经处理过了，请重新发一次 /tag。",
            available=await available_points(session, group_id=gid, user_id=uid),
        )
    # 先落库扣分 + 权益，再调 Telegram：网络慢或失败时不会有人趁机动别人的账户；
    # 两行在同一个事务里提交，所以不存在"扣了分但权益行没写成"的崩溃窗口。
    try:
        await upsert_entitlement(
            session,
            group_id=gid,
            user_id=uid,
            kind=KIND_TAG,
            payload=request.text,
            ref=ref,
            expires_at=expires_at,
            now=moment,
        )
    except Exception:
        log.exception(
            "shop tag entitlement write failed | group=%s user=%s ref=%s", gid, uid, ref
        )
        # 权益行没写进事务，但扣费还在**同一个未提交事务**里：退款后一起提交，
        # 用户净扣 0，且不会留下"已扣费但永不过期"的头衔。
        refunded = await _refund_and_reload(
            session, group_id=gid, user_id=uid, points=request.price, ref=ref
        )
        remaining = await available_points(session, group_id=gid, user_id=uid)
        head = (
            f"商店记账失败，已退回 {request.price} 分。"
            if refunded
            else "商店记账失败，退款也失败了，请联系管理员核账。"
        )
        return ShopReply(
            "failed",
            f"{head}当前可用 {remaining} 分，请稍后再试。",
            available=remaining,
            refunded=refunded,
        )
    try:
        await session.commit()
    except Exception:
        # 提交失败时 SQLAlchemy 已经回滚整个事务：扣费同样没有落库，
        # 这里**绝不能退款**，否则等于凭空给分。
        log.exception("shop tag commit failed | group=%s user=%s ref=%s", gid, uid, ref)
        # F-053 验收修补：「这次没有扣分」这句话必须与显示的余额一致。提交一旦失败，
        # 这里显式回滚，让会话内被扣分的对象过期、余额从库里重新读——原来直接读
        # 会话状态，会读出回滚前的值（消息说没扣分、数字却显示已经扣了）。
        try:
            await session.rollback()
        except Exception:
            log.warning("shop tag commit failure rollback failed", exc_info=True)
        remaining = await available_points(session, group_id=gid, user_id=uid)
        return ShopReply(
            "failed",
            f"商店暂时不可用，这次没有扣分。当前可用 {remaining} 分，请稍后再试。",
            available=remaining,
        )

    try:
        await _set_member_tag(bot, gid, uid, request.text)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "shop tag set failed | group=%s user=%s ref=%s",
            gid,
            uid,
            ref,
            exc_info=True,
        )
        # 退款 + 权益还原同一笔事务：不会留下"钱退了、到期行还在"。
        refunded = await _refund_and_reload(
            session,
            group_id=gid,
            user_id=uid,
            points=request.price,
            ref=ref,
            revert_kind=KIND_TAG,
            revert_to=previous_entitlement,
        )
        remaining = await available_points(session, group_id=gid, user_id=uid)
        if refunded:
            head = f"头衔没设置成功，已退回 {request.price} 分。"
        else:
            head = "头衔没设置成功，退款也失败了，请联系管理员核账。"
        return ShopReply(
            "telegram_failed",
            f"{head}当前可用 {remaining} 分。头衔被 Telegram 拒绝时可以换个短一点的文字再试。",
            available=remaining,
            refunded=refunded,
        )

    remaining = await available_points(session, group_id=gid, user_id=uid)
    when = expires_at.strftime("%Y-%m-%d %H:%M")
    return ShopReply(
        "ok",
        f"<b>头衔已生效</b>\n「{escape(request.text)}」有效期到 {when}（{request.days} 天）。\n"
        f"当前可用 {remaining} 分。到期机器人会自动清除，想续费直接再发一次 /tag。",
        available=remaining,
        expires_at=expires_at,
    )


async def _refund_and_reload(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    ref: str,
    revert_kind: str = "",
    revert_to: EntitlementSnapshot | None = None,
) -> bool:
    """退款并提交；退款本身失败也要记日志（绝不能让用户钱没了东西也没有）。

    ``revert_kind``/``revert_to`` 同时给出时，同一笔事务里把权益也还原成购买前
    的样子（F-053）：扣费与权益是一个事务提交的，撤销就必须一起撤销，否则会留下
    "钱退了、权益行还在"或"钱没退、权益被删"的不一致。
    """

    try:
        refunded = await refund_points(
            session,
            group_id=group_id,
            user_id=user_id,
            points=points,
            original_ref=ref,
        )
        if revert_kind and revert_to is not None:
            await restore_entitlement(
                session,
                group_id=group_id,
                user_id=user_id,
                kind=revert_kind,
                snapshot=revert_to,
            )
        await session.commit()
        return refunded
    except Exception:
        log.exception(
            "shop refund failed | group=%s user=%s ref=%s points=%s",
            group_id,
            user_id,
            ref,
            points,
        )
        try:
            await session.rollback()
        except Exception:
            log.exception("shop refund rollback failed | ref=%s", ref)
        return False


# ---------------------------------------------------------------------------
# 置顶求助
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinTarget:
    """``/top`` 回复的那条消息（纯数据，方便单测）。"""

    message_id: int = 0
    sender_id: int = 0
    is_bot: bool = False
    is_channel: bool = False

    @property
    def missing(self) -> bool:
        return int(self.message_id) <= 0


def resolve_pin_target(message: object) -> PinTarget:
    """从被回复的消息里抽出置顶目标；没回复就是空目标。"""

    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return PinTarget()
    from_user = getattr(reply, "from_user", None)
    sender_chat = getattr(reply, "sender_chat", None)
    sender_id = int(getattr(from_user, "id", 0) or 0)
    return PinTarget(
        message_id=int(getattr(reply, "message_id", 0) or 0),
        sender_id=sender_id,
        is_bot=bool(getattr(from_user, "is_bot", False)),
        is_channel=bool(sender_chat is not None and from_user is None),
    )


async def buy_pin(
    session: AsyncSession,
    *,
    bot: object,
    group_id: int,
    user_id: int,
    target: PinTarget,
    now: datetime | None = None,
) -> ShopReply:
    """``/top``：把自己的一条消息置顶 6 小时；失败退款。

    同一个人同一时刻只允许一笔置顶购买在跑：否则两个并发请求（双击 /top）都会
    看到"还没有生效中的置顶"，各自扣一次 20 分，只得到一个置顶（F-006）。
    """

    with pinned_section("economy"):
        async with _purchase_lock(group_id=group_id, user_id=user_id, kind=KIND_PIN):
            return await _buy_pin_locked(
                session,
                bot=bot,
                group_id=group_id,
                user_id=user_id,
                target=target,
                now=now,
            )


async def _buy_pin_locked(
    session: AsyncSession,
    *,
    bot: object,
    group_id: int,
    user_id: int,
    target: PinTarget,
    now: datetime | None = None,
) -> ShopReply:
    """``buy_pin`` 的加锁实现体（调用方必须已经持有购买锁）。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    gid, uid = int(group_id), int(user_id)
    available = await available_points(session, group_id=gid, user_id=uid)

    if target.missing:
        return ShopReply("not_reply", _pin_usage_text(), available=available)
    if target.is_bot or target.is_channel or target.sender_id <= 0:
        return ShopReply(
            "unsupported_target",
            f"只能置顶群里普通成员自己发的消息，机器人和频道的消息不支持。这次没有扣分。"
            f"当前可用 {available} 分。",
            available=available,
        )
    if target.sender_id != uid:
        return ShopReply(
            "not_yours",
            f"只能置顶你自己的消息：请回复你自己发的那条消息再发送 /top。这次没有扣分。"
            f"当前可用 {available} 分。",
            available=available,
        )

    existing = await active_entitlement(
        session, group_id=gid, user_id=uid, kind=KIND_PIN
    )
    # D2-05：只认**还没到期**的置顶。``active_entitlement`` 只看「行存在」，而到期
    # 清理每 300s 才跑一轮——刚过期的置顶行会在库里滞留最长约 5 分钟。旧判定只看
    # ``expires_at is not None``，于是这 5 分钟里用户被无谓挡下一个购买窗口，还收到
    # 一句自相矛盾的「还有 0 分钟到期」。口径与 ``next_expiry`` 一致：
    # ``expires_at > moment`` 才算生效中；过期行放行，交给 ``upsert_entitlement`` 覆盖。
    if (
        existing is not None
        and existing.expires_at is not None
        and existing.expires_at > moment
    ):
        left = minutes_left(existing.expires_at, moment)
        return ShopReply(
            "already_pinned",
            f"你已经有 1 条置顶了，还有 {left} 分钟到期。等它到期后可以再买一次，"
            f"这次没有扣分。当前可用 {available} 分。",
            available=available,
            expires_at=existing.expires_at,
        )
    if available < _econ().pin_price:
        return ShopReply(
            "insufficient",
            _insufficient_text(price=_econ().pin_price, available=available),
            available=available,
        )

    # 幂等键的时钟不能是 ``moment``：它按"整秒"口径取（now_shanghai_naive
    # 抹掉微秒），同一秒内的第二笔会落进同一个 ref。``now=None`` 时让
    # purchase_stamp 自己去拿不截断的时钟。
    stamp = purchase_stamp(now)
    ref = pin_spend_ref(group_id=gid, user_id=uid, stamp=stamp)
    expires_at = moment + timedelta(hours=_econ().pin_hours)
    # F-053：购买前先留一份权益快照（这里正常情况下为空，但置顶行可能刚好被
    # 到期清理漏掉，保留快照让回滚是精确的）。
    previous_entitlement = await entitlement_snapshot(
        session, group_id=gid, user_id=uid, kind=KIND_PIN
    )
    charged = await spend_points(
        session,
        group_id=gid,
        user_id=uid,
        points=_econ().pin_price,
        reason=SPEND_REASON_PIN,
        ref=ref,
    )
    if not charged:
        return ShopReply(
            "replay",
            "这笔购买已经处理过了，请重新发一次 /top。",
            available=await available_points(session, group_id=gid, user_id=uid),
        )
    # 扣分与权益同一个事务提交：崩溃/重启不会留下"扣了分但没有到期行"。
    try:
        await upsert_entitlement(
            session,
            group_id=gid,
            user_id=uid,
            kind=KIND_PIN,
            payload=str(target.message_id),
            ref=ref,
            expires_at=expires_at,
            now=moment,
        )
    except Exception:
        log.exception(
            "shop pin entitlement write failed | group=%s user=%s ref=%s", gid, uid, ref
        )
        refunded = await _refund_and_reload(
            session, group_id=gid, user_id=uid, points=_econ().pin_price, ref=ref
        )
        remaining = await available_points(session, group_id=gid, user_id=uid)
        head = (
            f"商店记账失败，已退回 {_econ().pin_price} 分。"
            if refunded
            else "商店记账失败，退款也失败了，请联系管理员核账。"
        )
        return ShopReply(
            "failed",
            f"{head}当前可用 {remaining} 分，请稍后再试。",
            available=remaining,
            refunded=refunded,
        )
    try:
        await session.commit()
    except Exception:
        # 提交失败 = 整个事务（含扣费）已回滚，退款反而会凭空加分。
        log.exception("shop pin commit failed | group=%s user=%s ref=%s", gid, uid, ref)
        remaining = await available_points(session, group_id=gid, user_id=uid)
        return ShopReply(
            "failed",
            f"商店暂时不可用，这次没有扣分。当前可用 {remaining} 分，请稍后再试。",
            available=remaining,
        )

    try:
        await _pin_message(bot, gid, target.message_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "shop pin failed | group=%s user=%s message=%s ref=%s",
            gid,
            uid,
            target.message_id,
            ref,
            exc_info=True,
        )
        refunded = await _refund_and_reload(
            session,
            group_id=gid,
            user_id=uid,
            points=_econ().pin_price,
            ref=ref,
            revert_kind=KIND_PIN,
            revert_to=previous_entitlement,
        )
        remaining = await available_points(session, group_id=gid, user_id=uid)
        head = (
            f"置顶失败（可能机器人没有置顶权限），已退回 {_econ().pin_price} 分。"
            if refunded
            else "置顶失败，退款也失败了，请联系管理员核账。"
        )
        return ShopReply(
            "telegram_failed",
            f"{head}当前可用 {remaining} 分。",
            available=remaining,
            refunded=refunded,
        )

    remaining = await available_points(session, group_id=gid, user_id=uid)
    when = expires_at.strftime("%Y-%m-%d %H:%M")
    return ShopReply(
        "ok",
        f"<b>置顶成功</b>\n你的这条消息会一直置顶到 {when}（{_econ().pin_hours} 小时后自动取消）。\n"
        f"当前可用 {remaining} 分。",
        available=remaining,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# 抽奖
# ---------------------------------------------------------------------------


async def lottery_draws_today(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    day: str,
) -> int:
    """今天已经抽了几次（数消费流水里当天前缀的行，天然幂等、不受退款影响）。"""

    prefix = f"lottery:{int(group_id)}:{int(user_id)}:{day}:"
    total = (
        await session.execute(
            select(func.count())
            .select_from(MemberPointSpend)
            .where(
                MemberPointSpend.group_id == int(group_id),
                MemberPointSpend.user_id == int(user_id),
                MemberPointSpend.ref.like(prefix + "%"),
            )
        )
    ).scalar()
    return int(total or 0)


def lottery_daily_guard(*, group_id: int, user_id: int, day: str):
    """D2-04：把「当日抽奖次数 < 上限」写成扣分语句的 WHERE 条件。

    与 :func:`lottery_draws_today` 同一口径（数消费流水里当天前缀的行），所以
    **退款行不计入次数**（退款 ref 是 ``shop-refund:lottery:...``，不匹配前缀），
    「当日次数用掉了就是用掉了」这条语义不变。
    """

    prefix = f"lottery:{int(group_id)}:{int(user_id)}:{day}:"
    today_rows = (
        select(func.count())
        .select_from(MemberPointSpend)
        .where(
            MemberPointSpend.group_id == int(group_id),
            MemberPointSpend.user_id == int(user_id),
            MemberPointSpend.ref.like(prefix + "%"),
        )
        .scalar_subquery()
    )
    return today_rows < int(_econ().lottery_daily_limit)


async def play_lottery(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    now: datetime | None = None,
    randbelow=None,
) -> ShopReply:
    """``/draw``：5 分一次，每天最多 10 次；中奖的分写奖励流水（不伪造签到）。

    D2-04：每日次数上限**不再**是"先数一次、再判一次"。两次 ``/draw`` 之间没有锁
    也没有条件写，25 个并发请求会各自读到同一份 ``used`` 计数并全部通过检查，把
    10 次/天的上限变成 25 次（抽奖期望值是净赚 +1.00 分/次，见 :data:`LOTTERY_TABLE`
    的注释，所以这是能直接兑换成头衔的积分泄漏）。现在把"当日行数 < 上限"写成
    :func:`spend_points` 的 ``extra_guard``，与余额守卫在**同一条** ``INSERT ...
    SELECT`` 里判完，整条语句跑在 SQLite 的写锁之下。
    """

    with pinned_section("economy") as econ:
        return await _play_lottery(
            session,
            econ=econ,
            group_id=group_id,
            user_id=user_id,
            now=now,
            randbelow=randbelow,
        )


async def _play_lottery(
    session: AsyncSession,
    *,
    econ: EconomySnapshot,
    group_id: int,
    user_id: int,
    now: datetime | None = None,
    randbelow=None,
) -> ShopReply:
    """``play_lottery`` 的实现体；``econ`` 是本次抽奖钉住的那份快照。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    gid, uid = int(group_id), int(user_id)
    available = await available_points(session, group_id=gid, user_id=uid)
    day = lottery_day(moment)

    # 快速路径：已经用满就直接回话，不必再走写语句。真正的闸门在下面的扣分里。
    used = await lottery_draws_today(session, group_id=gid, user_id=uid, day=day)
    if used >= _econ().lottery_daily_limit:
        return ShopReply(
            "daily_limit",
            f"今天已经抽了 {used} 次，每天最多 {_econ().lottery_daily_limit} 次，明天再来吧。"
            f"当前可用 {available} 分，这次没有扣分。",
            available=available,
        )
    if available < _econ().lottery_price:
        return ShopReply(
            "insufficient",
            _insufficient_text(price=_econ().lottery_price, available=available),
            available=available,
        )

    # 幂等键的时钟不能是 ``moment``：它按"整秒"口径取（now_shanghai_naive
    # 抹掉微秒），同一秒内的第二笔会落进同一个 ref。``now=None`` 时让
    # purchase_stamp 自己去拿不截断的时钟。
    stamp = purchase_stamp(now)
    ref = lottery_spend_ref(group_id=gid, user_id=uid, day=day, stamp=stamp)
    charged = await spend_points(
        session,
        group_id=gid,
        user_id=uid,
        points=_econ().lottery_price,
        reason=SPEND_REASON_LOTTERY,
        ref=ref,
        extra_guard=lottery_daily_guard(group_id=gid, user_id=uid, day=day),
    )
    if not charged:
        # rowcount==0：这一行没插进去，但驱动已经为这条 INSERT 发过 BEGIN，
        # 连接上留着一个握着 RESERVED 锁的写事务。不显式收掉的话，它会一直
        # 占到调用方关闭 session 为止，把并发的其它 /draw 全堵在 busy_timeout 上。
        await session.rollback()
        # 余额守卫和次数守卫都在同一条语句里判完了，rowcount==0 有三种可能：
        # 今日次数已满（并发下别人抢先）、余额被别处扣光、同一 ref 已经扣过。
        # 逐个说清楚，别一律报「已经处理过了」——那句话会让人以为被重复扣过款。
        after = await lottery_draws_today(session, group_id=gid, user_id=uid, day=day)
        if after >= _econ().lottery_daily_limit:
            return ShopReply(
                "daily_limit",
                f"今天已经抽了 {after} 次，每天最多 {_econ().lottery_daily_limit} 次，明天再来吧。"
                f"当前可用 {available} 分，这次没有扣分。",
                available=available,
            )
        remaining_now = await available_points(session, group_id=gid, user_id=uid)
        if remaining_now < _econ().lottery_price:
            return ShopReply(
                "insufficient",
                _insufficient_text(price=_econ().lottery_price, available=remaining_now),
                available=remaining_now,
            )
        return ShopReply(
            "replay",
            "这次抽奖已经处理过了，请重新发一次 /draw。",
            available=remaining_now,
        )
    await session.commit()

    prize = draw_prize(randbelow=randbelow)
    if prize.points > 0:
        try:
            await award_points(
                session,
                group_id=gid,
                user_id=uid,
                points=prize.points,
                reason=AWARD_REASON_LOTTERY,
                ref=lottery_prize_ref(group_id=gid, user_id=uid, stamp=stamp),
            )
            await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "shop lottery prize failed | group=%s user=%s ref=%s", gid, uid, ref
            )
            refunded = await _refund_and_reload(
                session, group_id=gid, user_id=uid, points=_econ().lottery_price, ref=ref
            )
            remaining = await available_points(session, group_id=gid, user_id=uid)
            head = (
                f"开奖时出了点问题，已退回 {_econ().lottery_price} 分。"
                if refunded
                else "开奖时出了点问题，退款也失败了，请联系管理员核账。"
            )
            return ShopReply(
                "failed",
                f"{head}当前可用 {remaining} 分。",
                available=remaining,
                refunded=refunded,
            )

    remaining = await available_points(session, group_id=gid, user_id=uid)
    net = int(prize.points) - _econ().lottery_price
    net_text = f"+{net}" if net >= 0 else str(net)
    # 重新数一次而不是用 `used + 1`：并发下 `used` 是这一笔之前的旧读数，
    # 报出去的「今天还能抽 N 次」会偏大。
    used_now = await lottery_draws_today(session, group_id=gid, user_id=uid, day=day)
    left = max(0, _econ().lottery_daily_limit - used_now)
    return ShopReply(
        "ok",
        f"🎲 抽奖结果：{prize.label}（本次净 {net_text} 分，当前可用 {remaining} 分）\n"
        f"今天还能抽 {left} 次。",
        available=remaining,
    )


# ---------------------------------------------------------------------------
# 商店文案
# ---------------------------------------------------------------------------


def render_shop_menu(*, available: int) -> str:
    """``/shop`` 的价目表 + 每件商品的确切用法（按钮只有一个：我的积分）。"""

    return (
        "<b>积分商店</b>\n"
        f"当前可用 <b>{int(available)}</b> 分。积分靠签到攒：每天在群里发 /checkin，"
        "或点群里的签到提醒按钮。\n"
        "\n"
        f"<b>① 自定义头衔 · {_econ().tag_days_7d} 天 —— {_econ().tag_price_7d} 分</b>\n"
        "用法：发 /tag 你的头衔\n"
        "例：/tag 摸鱼冠军\n"
        "\n"
        f"<b>② 自定义头衔 · {_econ().tag_days_30d} 天 —— {_econ().tag_price_30d} 分</b>\n"
        "用法：发 /tag 你的头衔 30天\n"
        "例：/tag 摸鱼冠军 30天\n"
        "\n"
        f"<b>③ 置顶自己的求助 · {_econ().pin_hours} 小时 —— {_econ().pin_price} 分</b>\n"
        "用法：长按你自己发的那条消息 → 回复 → 发送 /top\n"
        "\n"
        f"<b>④ 抽奖一次 —— {_econ().lottery_price} 分</b>\n"
        "用法：发 /draw（每人每天最多 10 次）\n"
        "奖池：谢谢参与 39%、3 分 20%、5 分 16%、8 分 10%、12 分 10%、40 分 4%、100 分 1%\n"
        "\n"
        f"<i>头衔 1-{_econ().tag_max_length} 个字、不能带表情、不能和别人重名；到期会自动清除。"
        "还没到期就再买一次，时间会往后接着算，不会白花钱。</i>"
    )


def render_balance(*, available: int, streak: int = 0) -> str:
    """「我的积分」按钮点下去的答案。"""

    tail = f"｜连续签到 {int(streak)} 天" if int(streak) > 0 else ""
    return f"当前可用 {int(available)} 分{tail}。在群里发 /shop 看价目表。"


# ---------------------------------------------------------------------------
# 到期清理：清头衔 / 取消置顶（CLI 与常驻服务共用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpiredItem:
    """一条到期的权益（打印/日志用）。"""

    kind: str
    group_id: int
    user_id: int
    payload: str
    expires_at: datetime
    ref: str = ""

    @property
    def label(self) -> str:
        if self.kind == KIND_TAG:
            return f"清除头衔「{self.payload}」"
        if self.kind == KIND_PIN:
            return f"取消置顶（消息 {self.payload}）"
        return f"未知权益 {self.kind}"


@dataclass(frozen=True, slots=True)
class ExpiryOutcome:
    """一条到期权益的处理结果。"""

    item: ExpiredItem
    ok: bool
    action: str = ""
    detail: str = ""
    notified: bool = False
    #: 到期提醒的**结果未知**（超时/断连，可能已送达）或按既有证据被主动跳过。
    #: ``True`` 时调用方不得把这个目标当成"可以安全重试的通知"。
    notice_unknown: bool = False
    #: R2（B13a 返工 v3）：写边界持久复核不通过（授权被撤 / 发起人已不是管理员 /
    #: 持久配置已变更 / 本 run 已被取消）。``True`` 时这条权益**一个字都没动**：
    #: 没撤销、没删行、没推后、没发提醒，调用方必须要求重新预览后确认。
    guard_blocked: bool = False


def expiry_notice(item: ExpiredItem) -> str:
    """到期提醒（先私聊，私聊失败就发群里）。"""

    if item.kind == KIND_TAG:
        return (
            f"🔔 你的自定义头衔「{escape(item.payload)}」已到期，机器人已经帮你清除了。"
            "想继续挂头衔，在群里发 /tag 头衔文字 再买一次。"
        )
    return "🔔 你置顶的求助消息已到期，机器人已经取消置顶了。还想继续置顶，回复那条消息再发一次 /top。"


async def due_entitlements(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    group_id: int | None = None,
    group_ids: Sequence[int] | None = None,
    exclude_group_ids: Sequence[int] | None = None,
) -> list[MemberEntitlement]:
    """到期（``expires_at <= now``）的权益，按到期时间从早到晚。

    ``group_id`` / ``group_ids`` 是可选的**范围收窄**（运营任务中心里"只处理本群
    当前到期项"、以及 B13b 按群执行来源筛选用）；不给就是原来的全量口径。

    ``group_ids`` 是**空集合**时返回空列表：那是"本群都不是内部执行来源"的真实
    意思，绝不能退化成"没有限制"从而把别群也处理掉。

    ``exclude_group_ids``（B13b 返工 v3）是从全量口径里**排除**这些群。内部服务要
    同时覆盖"internal 群"和"既没有授权身份、也没有来源策略的纯历史群"，而后者无法
    逐个枚举，只能反过来排除已治理的群——这样纯历史群仍然按改造前的全量语义处理，
    同时 ``limit`` 也不会被已切走/已撤权群的到期行吃光。
    """

    limit = _econ().expiry_batch_limit if limit is None else int(limit)
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    statement = select(MemberEntitlement).where(MemberEntitlement.expires_at <= moment)
    if group_id is not None:
        statement = statement.where(MemberEntitlement.group_id == int(group_id))
    elif group_ids is not None:
        narrowed = sorted({int(value) for value in group_ids})
        if not narrowed:
            return []
        statement = statement.where(MemberEntitlement.group_id.in_(narrowed))
    if exclude_group_ids is not None:
        excluded = sorted({int(value) for value in exclude_group_ids})
        if excluded:
            statement = statement.where(
                MemberEntitlement.group_id.not_in(excluded)
            )
    rows = await session.execute(
        statement.order_by(MemberEntitlement.expires_at, MemberEntitlement.id).limit(
            max(1, int(limit))
        )
    )
    return list(rows.scalars().all())


def _expired_item(row: MemberEntitlement) -> ExpiredItem:
    return ExpiredItem(
        kind=str(row.kind),
        group_id=int(row.group_id),
        user_id=int(row.user_id),
        payload=str(row.payload or ""),
        expires_at=row.expires_at,
        ref=str(row.ref or ""),
    )


async def _revoke(bot: object, item: ExpiredItem) -> tuple[bool, str]:
    """把一条到期权益在 Telegram 侧撤掉；返回 (成功?, 说明)。

    R5：入参改成**标量快照**（``ExpiredItem``）而不是 ORM 行——撤销要走网络，不能
    依赖一个已经 rollback 过的会话实例（异步会话里惰性刷新会直接抛 MissingGreenlet）。
    """

    gid, uid = int(item.group_id), int(item.user_id)
    try:
        if str(item.kind) == KIND_TAG:
            await _set_member_tag(bot, gid, uid, "")
            return True, "已清除头衔"
        if str(item.kind) == KIND_PIN:
            await _unpin_message(bot, gid, int(item.payload or 0))
            return True, "已取消置顶"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # 用户退群、消息被删、机器人被撤权都会走到这里。失败只记日志并返回
        # False：调用方保留权益行、推后到期时间来重试（F-052），因为记录删掉
        # 就再也没人会把还挂在群里的头衔撤下来了。
        log.warning(
            "shop expiry revoke failed | kind=%s group=%s user=%s payload=%s error=%s",
            item.kind,
            gid,
            uid,
            item.payload,
            exc,
        )
        return False, f"Telegram 侧失败：{type(exc).__name__}"
    return True, f"未知权益类型 {item.kind}，已跳过"


async def _drop_snapshot(
    session: AsyncSession,
    *,
    row_id: int,
    expires_at: datetime,
    guard: "PersistedGuard | None" = None,
) -> bool:
    """D2-01：只有当这一行的 ``expires_at`` **仍然等于**扫描读到它时的那个值才删。

    带 ``id`` + ``expires_at`` 谓词，作用域和 :func:`_defer_snapshot` 一样，
    返回 ``False`` 表示"这一行已经不是扫描看到的那一版了"（窗口内被续期或删掉）。
    R5：用标量快照，避免在 rollback 之后访问已经过期的 ORM 实例。

    R2（返工 v3）：``guard`` 非空时把持久复核（本群授权 + 发起人本群当前权 +
    持久配置 revision + 本 run 未被取消）加进**同一条 DELETE 的 WHERE**——
    撤销已经发生过，但"删掉这一行"这个不可逆业务写必须和复核在同一个事务/同一句
    谓词里，不能在两者之间再开一个 await 窗口。
    """

    stmt = delete(MemberEntitlement).where(
        MemberEntitlement.id == int(row_id),
        MemberEntitlement.expires_at == expires_at,
    )
    if guard is not None:
        stmt = stmt.where(guard_predicate(guard))
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def _defer_snapshot(
    session: AsyncSession,
    *,
    row_id: int,
    expires_at: datetime,
    retry_at: datetime,
    guard: "PersistedGuard | None" = None,
) -> bool:
    """D2-01：撤销失败后的"推后重试"同样必须带 ``expires_at`` 谓词。

    原来的 ``row.expires_at = moment + 900s`` 是一次无条件 UPDATE，会把窗口内
    刚续上的 7 天直接覆写成 15 分钟——用户已经付过钱了。

    R2（返工 v3）：``guard`` 非空时同样把持久复核写进这一句 UPDATE 的 WHERE。
    """

    stmt = (
        update(MemberEntitlement)
        .where(
            MemberEntitlement.id == int(row_id),
            MemberEntitlement.expires_at == expires_at,
        )
        .values(expires_at=retry_at)
    )
    if guard is not None:
        stmt = stmt.where(guard_predicate(guard))
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def _reassert_entitlement(
    bot: object, session: AsyncSession, *, group_id: int, user_id: int, kind: str
) -> None:
    """扫描按陈旧快照把头衔/置顶撤掉了，但这一行其实已经被续期。

    不补这一次写回的话：库里写着"有效期到 7 天后"，Telegram 侧却空着，而扫描
    下次不会再看到它（``expires_at`` 已经不在到期集合里），用户刚付的钱就凭空
    消失。按库里**当前**的 payload 重放同一个动作（幂等），失败只记日志。

    R5：先把当前值读成标量并结束这个只读事务，再做 Telegram 往返——撤销是按陈旧
    快照发生的，这里不能再压着一个数据库事务跨网络。
    """

    fresh = await active_entitlement(
        session, group_id=int(group_id), user_id=int(user_id), kind=str(kind)
    )
    if fresh is None:
        # 行已经被并发地删掉了（例如另一个扫描实例）：没有可恢复的目标。
        return
    fresh_kind = str(fresh.kind)
    fresh_group = int(fresh.group_id)
    fresh_user = int(fresh.user_id)
    fresh_payload = str(fresh.payload or "")
    fresh_expires = fresh.expires_at
    await session.rollback()
    try:
        if fresh_kind == KIND_TAG:
            await _set_member_tag(bot, fresh_group, fresh_user, fresh_payload)
        elif fresh_kind == KIND_PIN:
            await _pin_message(bot, fresh_group, int(fresh_payload or 0))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "shop expiry re-assert failed after concurrent renewal | kind=%s "
            "group=%s user=%s payload=%s",
            fresh_kind,
            fresh_group,
            fresh_user,
            fresh_payload,
            exc_info=True,
        )
        return
    log.warning(
        "shop expiry skipped a concurrently renewed entitlement | kind=%s group=%s "
        "user=%s payload=%s expires_at=%s",
        fresh_kind,
        fresh_group,
        fresh_user,
        fresh_payload,
        fresh_expires,
    )


async def _notify_expiry_result(bot: object, item: ExpiredItem) -> tuple[bool, bool]:
    """先私聊提醒，私聊失败就发到群里。

    返回 ``(是否已送出, 结果是否未知)``。与"只返回 bool"的旧实现相比，超时/断连
    这类**歧义失败**会被单独标出来：消息可能已经送达，调用方据此落一条 ``unknown``
    出站证据并禁止自动重发（R5），而不是把它当"没发出去"重试。
    """

    send = getattr(bot, "send_message", None)
    if not callable(send):
        return False, False
    text = expiry_notice(item)
    ambiguous = False
    for chat_id in (item.user_id, item.group_id):
        try:
            async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
                await send(chat_id=int(chat_id), text=text)
            return True, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if chat_id == item.user_id and not definitely_not_delivered(exc):
                # R5：私聊结果未知（超时/连接重置/网络中断）时**绝不能换收件人再发**。
                # 第一条消息可能已经送达，群发会让同一次到期提醒变成两条，并且把
                # 第一发的未知结果洗成"已送出"。只有明确被 Telegram 拒收（4xx）才
                # 允许 fallback。
                log.warning(
                    "shop expiry private notice result unknown | group=%s user=%s kind=%s",
                    item.group_id,
                    item.user_id,
                    item.kind,
                )
                return False, True
            if ambiguous_send_failure(exc):
                ambiguous = True
            log.debug(
                "shop expiry notice failed | chat=%s kind=%s",
                chat_id,
                item.kind,
                exc_info=True,
            )
    if ambiguous:
        log.warning(
            "shop expiry notice result unknown | group=%s user=%s kind=%s",
            item.group_id,
            item.user_id,
            item.kind,
        )
        return False, True
    log.warning(
        "shop expiry notice undeliverable | group=%s user=%s kind=%s",
        item.group_id,
        item.user_id,
        item.kind,
    )
    return False, False


async def _notify_expiry(bot: object, item: ExpiredItem) -> bool:
    """兼容入口：只关心"有没有送出去"（``unknown`` 也算没送出去）。"""

    notified, _ = await _notify_expiry_result(bot, item)
    return notified


#: 出站证据里的安全文案（不含异常原文/权益内容）。
EXPIRY_NOTICE_SENT_DETAIL = "expiry notice delivered"
EXPIRY_NOTICE_UNKNOWN_DETAIL = "expiry notice result unknown (timeout)"
EXPIRY_NOTICE_DISPATCH_DETAIL = "expiry notice outbound dispatch claim"


def entitlement_expiry_key(
    *,
    group_id: int,
    user_id: int,
    kind: str,
    expires_at: datetime,
    ref: str = "",
) -> str:
    """权益**版本**化业务键：同一版权益（群, 用户, 类型, 到期时刻, 购买 ref）才是同一笔到期动作。

    续期会改 ``expires_at``，因此"同一个用户下一次到期"天然是另一个键，不会被旧
    证据误伤；而"同一版权益重复扫描"始终命中同一条证据。
    R5：到期时刻带**微秒**并且带上购买 ``ref`` —— 旧实现只取到秒，同一秒内两笔不同
    订单（续期/换单）会塌成同一个键，把"另一版权益"误判成"同一版已发送"。
    """

    return receipt_key(
        int(group_id),
        int(user_id),
        str(kind),
        expires_at.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        str(ref or ""),
    )


async def expiry_notice_receipt(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    kind: str,
    expires_at: object,
    ref: str = "",
) -> BusinessReceipt | None:
    """读这一版权益的到期提醒出站证据（``None`` = 没有证据）。"""

    if not isinstance(expires_at, datetime):
        return None
    return await load_receipt(
        session,
        task_id=TASK_SHOP_EXPIRE,
        group_id=int(group_id),
        business_key=entitlement_expiry_key(
            group_id=group_id,
            user_id=user_id,
            kind=kind,
            expires_at=expires_at,
            ref=ref,
        ),
    )


async def _claim_expiry_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    item: ExpiredItem,
    expires_at: datetime,
    guard: "PersistedGuard | None" = None,
) -> str:
    """在**真实出站之前**把"这一版权益的到期提醒"claim 成 protected 并提交。

    R5：旧实现只有"发送成功之后补写证据"，进程死在发送与写库之间、或者写库本身
    失败，下一轮扫描就会把同一条提醒再发一遍（v5/v8 实测）。这里用
    :func:`claim_business_dispatch` 的 ``ON CONFLICT DO NOTHING`` 抢占同一个业务
    键：抢到的执行者才发送，抢不到说明已经有证据（sent/unknown）或者另一个执行者
    正在发，都不重复。

    返回 ``claimed`` / ``protected`` / ``blocked`` / ``failed``；``blocked`` 是 R2
    （返工 v3）的写边界持久复核不通过（授权被撤 / 发起人已不是管理员 / 持久配置
    已变更 / 本 run 已被取消），``failed`` 表示写不进数据库，两种情况都**绝不能
    发送**（没有保护的外发无法在崩溃后收敛），而且都不产生任何副作用。
    """

    key = entitlement_expiry_key(
        group_id=int(item.group_id),
        user_id=int(item.user_id),
        kind=str(item.kind),
        expires_at=expires_at,
        ref=str(item.ref or ""),
    )
    try:
        async with session_factory() as side_session:
            claimed = await claim_business_dispatch(
                side_session,
                task_id=TASK_SHOP_EXPIRE,
                group_id=int(item.group_id),
                business_key=key,
                detail=EXPIRY_NOTICE_DISPATCH_DETAIL,
                guard=guard,
            )
            await side_session.commit()
    except Exception:
        log.warning(
            "shop expiry dispatch claim failed | group=%s user=%s kind=%s",
            item.group_id,
            item.user_id,
            item.kind,
            exc_info=True,
        )
        return "failed"
    if claimed is not None:
        return "claimed"
    if guard is None:
        return "protected"
    # 抢不到要么是别人握着保护，要么是复核不通过；复核查询自己出错时同样按
    # 保守协议"不放行"（也不发送）。
    try:
        async with session_factory() as side_session:
            allowed = await verify_guard(side_session, guard)
    except Exception:
        log.warning(
            "shop expiry dispatch guard verify failed | group=%s user=%s kind=%s",
            item.group_id,
            item.user_id,
            item.kind,
            exc_info=True,
        )
        return "blocked"
    return "protected" if allowed else "blocked"


async def _release_expiry_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    item: ExpiredItem,
    expires_at: datetime,
) -> bool:
    """放掉**本次自己抢到**的出站保护（只删 ``dispatching``）。

    只在"这次调用确定没有发出任何提醒"时调用：提醒阶段排在业务台账收口之后，
    撤销失败（把行推后重试）或者行已被续期（不删不提醒）时都没有走到发送，
    因此保留保护只会让这条到期提醒**永远**发不出去。``release_business_dispatch``
    只删 ``dispatching``，已确认的 ``sent``/``unknown`` 证据永远不会被删掉。
    """

    key = entitlement_expiry_key(
        group_id=int(item.group_id),
        user_id=int(item.user_id),
        kind=str(item.kind),
        expires_at=expires_at,
        ref=str(item.ref or ""),
    )
    try:
        async with session_factory() as side_session:
            await release_business_dispatch(
                side_session,
                task_id=TASK_SHOP_EXPIRE,
                group_id=int(item.group_id),
                business_key=key,
            )
            await side_session.commit()
    except Exception:
        log.warning(
            "shop expiry dispatch release failed | group=%s user=%s kind=%s",
            item.group_id,
            item.user_id,
            item.kind,
            exc_info=True,
        )
        return False
    return True


async def _record_expiry_receipt_quietly(
    session_factory: async_sessionmaker[AsyncSession] | None,
    session: AsyncSession,
    *,
    item: ExpiredItem,
    expires_at: datetime,
    state: str,
    detail: str,
) -> None:
    """落一条出站证据；失败只记日志（绝不改变对外结论，也绝不重发）。

    有 ``session_factory`` 时用**独立事务**写：发送结果未知时，外层这一条权益的
    事务可能整体回滚（撤下失败/续期竞争），证据必须活下来，否则下一轮扫描会把
    同一条到期提醒再发一遍。
    """

    key = entitlement_expiry_key(
        group_id=int(item.group_id),
        user_id=int(item.user_id),
        kind=str(item.kind),
        expires_at=expires_at,
        ref=str(item.ref or ""),
    )
    try:
        if session_factory is None:
            await record_receipt(
                session,
                task_id=TASK_SHOP_EXPIRE,
                group_id=int(item.group_id),
                business_key=key,
                state=state,
                detail=detail,
            )
            return
        async with session_factory() as side_session:
            await record_receipt(
                side_session,
                task_id=TASK_SHOP_EXPIRE,
                group_id=int(item.group_id),
                business_key=key,
                state=state,
                detail=detail,
            )
            await side_session.commit()
    except Exception:
        log.warning(
            "shop expiry receipt write failed | group=%s user=%s kind=%s state=%s",
            item.group_id,
            item.user_id,
            item.kind,
            state,
            exc_info=True,
        )


async def _admit_expiry_write(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    row_id: int,
    expires_at: datetime,
    guard: "PersistedGuard",
) -> bool:
    """撤销**之前**的持久准入：用一条带谓词的真实 UPDATE 抢到 writer 锁并复核。

    B13b 返工（R-B1）：自动来源入口过去只在"通知保护 claim"里带守卫，于是
    ``notify=False`` 或已有回执时 ``protected`` 恒为 ``legacy``，撤头衔/取消置顶
    与随后那条 DELETE 完全不做复核——父探针就是在这个窗口里把来源切走，旧来源仍
    完成了 1 次撤销 + 1 条通知。

    这里写回同一个 ``expires_at``：它是一次**真实写入**（在 SQLite writer 锁下求值
    ``guard_predicate``，锁一直持到 commit），所以"此刻还算不算合法来源"与"接下来
    要不要真的撤销"之间没有可被抢占的窗口。返回 ``False`` = 复核不通过或这行已经
    不是扫描看到的那一版 ⇒ 调用方一个副作用都不许产生。
    """

    try:
        async with session_factory() as side_session:
            result = await side_session.execute(
                update(MemberEntitlement)
                .where(
                    MemberEntitlement.id == int(row_id),
                    MemberEntitlement.expires_at == expires_at,
                    guard_predicate(guard),
                )
                .values(expires_at=expires_at)
            )
            admitted = int(getattr(result, "rowcount", 0) or 0) == 1
            await side_session.commit()
            return admitted
    except Exception:
        log.warning(
            "shop expiry admission write failed | group=%s row=%s",
            int(getattr(guard, "group_id", 0) or 0),
            int(row_id),
            exc_info=True,
        )
        return False


async def _expire_row(
    session: AsyncSession,
    bot: object,
    item: ExpiredItem,
    row_id: int,
    *,
    moment: datetime,
    notify: bool,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    guard: "PersistedGuard | None" = None,
) -> ExpiryOutcome:
    """处理**一条**到期权益：撤下 → 删行/推后 → 通知（带持久出站证据）。

    R5：事务被切成三段，任何 Telegram 往返都在**没有未结束的数据库事务**下发生：

    1. 读这一版权益的出站证据，然后释放扫描的只读事务；
    2. （新路径）出站**保护**在独立事务里 commit → 撤销（网络）→ 删行/推后在短事务
       里 commit，业务台账先收口；
    3. 提醒（网络）→ 结果（sent/unknown）落独立事务。

    旧实现把这三段全塞在调用方同一个 session 里：扫描的读事务跨着整个网络往返，
    发送期间独立写入者拿不到写锁（v8 实测独立 UPDATE 直接 OperationalError、receipt
    写 5 秒超时被吞、``operations_business_receipts`` 一行都没有），而"发送成功才补写
    证据"又让 `send → 证据` 之间的崩溃必然重发。

    入参是**标量快照**（``ExpiredItem`` + ``row_id``），因为第 1 阶段的 rollback 会
    让 ORM 实例过期，异步会话里再读属性会抛 MissingGreenlet。
    """

    snapshot_expires_at = item.expires_at
    # CAS 快照：这一行必须在"到期时间还是刚读到的那个值"时才允许被删/被推后。
    receipt = await expiry_notice_receipt(
        session,
        group_id=item.group_id,
        user_id=item.user_id,
        kind=item.kind,
        expires_at=snapshot_expires_at,
        ref=item.ref,
    )
    protected = "legacy"
    if session_factory is not None:
        # 只读事务到此结束：后面任何网络往返都不再持有 SQLite 的读/写锁。
        await session.rollback()
        if notify and receipt is None:
            protected = await _claim_expiry_dispatch(
                session_factory,
                item=item,
                expires_at=snapshot_expires_at,
                guard=guard,
            )
        if guard is not None and protected != "blocked":
            # R-B1：无论有没有走通知保护 claim（``notify=False``、已有回执），撤销
            # 这条不可逆动作之前都必须先拿到带谓词的写准入。
            admitted = await _admit_expiry_write(
                session_factory,
                row_id=row_id,
                expires_at=snapshot_expires_at,
                guard=guard,
            )
            if not admitted:
                protected = "blocked"
    if protected == "blocked":
        # R2（返工 v3）：写边界持久复核不通过（授权被撤 / 发起人已不是管理员 /
        # 持久配置已变更 / 本 run 已被取消）。撤销是网络动作、删行/推后是业务写
        # 入，两者**一个都不做**：这条权益保持原样，用户重新预览后确认即可再来。
        await session.rollback()
        return ExpiryOutcome(
            item=item,
            ok=False,
            action=item.label,
            detail=(
                "写边界复核未通过（授权被撤、发起人已不是管理员、持久配置已变更"
                "或本 run 已被取消）：本次未撤销权益、未删除记录、也未发送提醒，"
                "请重新预览后确认"
            ),
            notified=False,
            notice_unknown=False,
            guard_blocked=True,
        )
    ok, detail = await _revoke(bot, item)
    if ok:
        removed = await _drop_snapshot(
            session,
            row_id=row_id,
            expires_at=snapshot_expires_at,
            guard=guard,
        )
        if not removed:
            # 窗口内被人续期/改过了：我们刚才那次撤销是基于陈旧快照做的，
            # 这一行已经不属于我们。不删、不推后、不提醒，并把 Telegram 侧
            # 按库里**当前**的值补回去（否则用户刚付的钱换来的头衔凭空消失，
            # 而库里还写着有效期到 7 天后，没有任何东西会再把它设回来）。
            ok = False
            detail = "权益在扫描期间被续期或改动，本次未撤销"
            # 这一句 DELETE 命中 0 行但也开了写事务：先收口再走网络。
            await session.rollback()
            await _reassert_entitlement(
                bot,
                session,
                group_id=item.group_id,
                user_id=item.user_id,
                kind=item.kind,
            )
    else:
        # 撤不下来就不能删行（F-052）：头衔/置顶还在 Telegram 侧生效，删掉记录
        # 等于既没清干净、也没有任何东西再重试——付费头衔会永远留在群里。
        # 把到期时间往后推一个重试间隔：下次扫描接着撤，同时避免这条记录
        # 每一轮都占住批量的最前面（due_entitlements 按 expires_at 排序）。
        retry_at = moment + timedelta(seconds=_econ().expiry_retry_seconds)
        deferred = await _defer_snapshot(
            session,
            row_id=row_id,
            expires_at=snapshot_expires_at,
            retry_at=retry_at,
            guard=guard,
        )
        if not deferred:
            # 同上：这一行已经被续期，不能拿 900 秒的重试间隔去覆盖已付的时长。
            ok = False
            detail = "权益在扫描期间被续期或改动，重试推后未生效"
        else:
            log.warning(
                "shop expiry revoke deferred | kind=%s group=%s user=%s payload=%s "
                "retry_at=%s detail=%s",
                item.kind,
                item.group_id,
                item.user_id,
                item.payload,
                retry_at,
                detail,
            )
    if session_factory is not None:
        # 业务台账先收口：下面的提醒发送不压着任何写事务，独立写入者随时可以提交。
        await session.commit()
    # 只有真的撤下来了才提醒"已清除"，否则等于骗用户
    notified = False
    notice_unknown = False
    if notify and ok:
        if receipt is not None and receipt.state == RECEIPT_STATE_SENT:
            notified = True
            detail = f"{detail}；已有出站证据确认到期提醒已发送，不再重复通知"
        elif receipt is not None and receipt.state == RECEIPT_STATE_UNKNOWN:
            notified = bool(int(receipt.external_message_id or 0))
            notice_unknown = True
            detail = (
                f"{detail}；上次到期提醒结果未知（可能已送达），按证据不自动重发，"
                "需人工核对"
            )
        elif receipt is not None:
            # dispatching：已经有执行者（或本进程上一轮）接管了这次提醒。
            notice_unknown = True
            detail = f"{detail}；已有出站保护（正在发送或结果未知），不自动重发"
        elif protected == "failed":
            notice_unknown = True
            detail = f"{detail}；无法写入出站保护，本次不发送提醒（禁止无保护外发）"
        elif protected == "protected":
            notice_unknown = True
            detail = f"{detail}；另一执行者已接管本次到期提醒，不重复发送"
        else:
            delivered, unknown = await _notify_expiry_result(bot, item)
            notified = delivered
            if delivered:
                await _record_expiry_receipt_quietly(
                    session_factory,
                    session,
                    item=item,
                    expires_at=snapshot_expires_at,
                    state=RECEIPT_STATE_SENT,
                    detail=EXPIRY_NOTICE_SENT_DETAIL,
                )
            elif unknown:
                notice_unknown = True
                await _record_expiry_receipt_quietly(
                    session_factory,
                    session,
                    item=item,
                    expires_at=snapshot_expires_at,
                    state=RECEIPT_STATE_UNKNOWN,
                    detail=EXPIRY_NOTICE_UNKNOWN_DETAIL,
                )
                detail = (
                    f"{detail}；到期提醒结果未知（可能已送达），已记录证据，不自动重发"
                )
    if (
        session_factory is not None
        and protected == "claimed"
        and not notified
        and not notice_unknown
    ):
        # 这次调用**确定没有发出**任何到期提醒：撤销失败转推后、行已被续期不提醒、
        # 或者提醒被明确拒收。把**自己刚抢到**的保护放掉，否则这条到期提醒会被自己
        # 留下的 dispatching 永久挡住。上一轮遗留的 dispatching/sent/unknown 证据不在
        # 这里删——无法证明它没发出去，保守保留。
        await _release_expiry_dispatch(
            session_factory, item=item, expires_at=snapshot_expires_at
        )
    return ExpiryOutcome(
        item=item,
        ok=ok,
        action=item.label,
        detail=detail,
        notified=notified,
        notice_unknown=notice_unknown,
    )


async def expire_due_entitlements(
    session: AsyncSession,
    *,
    bot: object,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
    limit: int | None = None,
    group_id: int | None = None,
    group_ids: Sequence[int] | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    guard: "PersistedGuard | None" = None,
    guards: Mapping[int, "PersistedGuard"] | None = None,
    governed_groups: Collection[int] | None = None,
    allow_unmanaged_legacy: bool = True,
    exclude_group_ids: Sequence[int] | None = None,
) -> list[ExpiryOutcome]:
    """扫描到期的权益 → 清头衔 / 取消置顶 → 删行。

    **幂等**：Telegram 侧的两个操作（把 tag 设成空、取消置顶）本身就是幂等的，
    而行只有在撤下动作真的跑成功之后才删。撤不下来时保留这一行并把 ``expires_at``
    推后一个重试间隔，下次扫描接着撤（F-052）——否则付费头衔会永远留在群里，
    既没有记录也没有重试。进程重启、重复执行、一次处理多条都安全。
    ``dry_run=True`` 时只返回"打算做什么"，不碰 Telegram 也不改数据库。

    ``group_id`` 与 :func:`due_entitlements` 一样是可选的**范围收窄**（运营任务
    中心"shop 只处理本群当前到期项"用）；不给就是原来的全量口径，常驻
    ``ShopExpiryService`` 与 CLI 的行为逐字不变。

    ``guards``（B13b 返工 R-B1）：**按群**的自动来源守卫（同一次来源判定的快照）。
    自动入口（内部服务 / 外部 CLI）必须用它而不是单个 ``guard``：每一行用它自己
    群的策略 revision 去复核，行所属的群不在映射里就 fail-closed 跳过。

    ``governed_groups`` / ``allow_unmanaged_legacy``（B13b 返工 v3，父 v5）：逐**群**
    决定"映射里没有守卫"意味着什么。

    * 行所属群在 ``governed_groups`` 里（有 ``AuthorizedGroup`` 行，或有持久来源
      策略）= 这个部署管理过它。缺守卫只可能是"已经不在授权/来源快照里"（例如切到
      external 之后又被撤权）⇒ **fail-closed 跳过**，绝不当成无约束的 legacy。
    * 群不在 ``governed_groups`` 里 = 既没有身份也没有策略的纯历史群 ⇒ 保持改造前
      的 legacy 口径（``guard=None``，由 claim/回执保护），除非调用方显式传
      ``allow_unmanaged_legacy=False``（外部 CLI 就不处理历史群，改造前也不处理）。

    **不能用"整个部署有没有来源记录"的全局开关来近似**：父 v5 的混库里，只要有一个
    群被切到 external，同库的纯历史群就会被一起漏掉（v2 的回退）。范围和逐群判定由
    :func:`shop_source_plan` 给出；读策略失败直接抛，绝不在读不到来源时按全量跑。

    **D2-01：删/推后都带 ``expires_at`` 谓词**。一轮扫描的事务边界是"读完到期行
    → 逐条 Telegram 往返（每条最多 10s 超时，一轮最多 200 条）→ 循环后统一
    commit"，而 :func:`buy_member_tag` 的续期不在任何锁的保护范围内，窗口内
    完全可能先写完并 commit。按 ORM 快照做 ``session.delete(row)`` 生成的是
    ``DELETE ... WHERE id = ?``（没有 ``expires_at``），会把刚付过钱的续期整行
    删掉；撤销失败分支的 ``row.expires_at = now + 900s`` 更是无条件 UPDATE，
    把已付的 7 天压成 15 分钟。所以每条都把"读这一行时的到期时间"当 CAS 谓词
    带下来，只在行确实还是当初读到的那一版时才动它；每条处理完立刻 commit，
    把陈旧快照的窗口从"整轮"压到"一条"。
    """

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    governed = (
        None
        if governed_groups is None
        else frozenset(int(value) for value in governed_groups)
    )
    rows = await due_entitlements(
        session,
        now=moment,
        limit=limit,
        group_id=group_id,
        group_ids=group_ids,
        exclude_group_ids=exclude_group_ids,
    )
    # R5：先把整批行的标量快照读出来，再开始逐条处理。``_expire_row`` 在真实出站
    # 之前会 ``rollback`` 这个会话（释放 SQLite 读写锁），一旦回滚，同会话里其余
    # ORM 实例全部过期——之后 ``row.kind`` 之类的属性访问会触发惰性刷新，在异步
    # 会话里直接抛 MissingGreenlet。循环里只碰纯 Python 值就没有这个问题。
    planned = [(int(row.id), _expired_item(row)) for row in rows]
    outcomes: list[ExpiryOutcome] = []
    for row_id, item in planned:
        # R-B1：自动来源入口传进来的是**按群**守卫（同一次来源判定的快照）。没有
        # 策略/授权记录的"历史群"（既不在授权列表、也没有来源策略）保持改造前的
        # legacy 口径（``guard=None``，由 claim/回执保护），因为它们从来就不在可
        # 切换范围内；只要这个群在判定那一刻有授权记录，它的守卫就在映射里，撤销
        # 与 DELETE 都会在真实写边界复核。
        row_guard: PersistedGuard | None
        if guards is None:
            row_guard = guard
        else:
            row_guard = guards.get(int(item.group_id))
            if row_guard is None:
                managed = governed is not None and int(item.group_id) in governed
                if managed or not allow_unmanaged_legacy:
                    # 有身份/有策略的群（或调用方明确不允许 legacy）：缺守卫就是
                    # "已撤权/不归本入口"，绝不能降级成无约束的 legacy 处理。
                    log.info(
                        "shop expiry skipped group outside the source snapshot | group=%s",
                        int(item.group_id),
                    )
                    continue
                log.info(
                    "shop expiry legacy group without source identity or policy | group=%s",
                    int(item.group_id),
                )
        if dry_run:
            outcomes.append(
                ExpiryOutcome(
                    item=item,
                    ok=True,
                    action="dry-run",
                    detail=f"到期于 {item.expires_at:%Y-%m-%d %H:%M}，将{item.label}",
                )
            )
            continue
        outcomes.append(
            await _expire_row(
                session,
                bot,
                item,
                row_id,
                moment=moment,
                notify=notify,
                session_factory=session_factory,
                guard=row_guard,
            )
        )
        # 每条一次 commit：陈旧快照的窗口压到"一条"，而且后面某条抛异常时，
        # 前面已经撤干净的结果不会被整批回滚掉。
        await session.commit()
    return outcomes


async def expire_entitlement_target(
    session: AsyncSession,
    *,
    bot: object,
    group_id: int,
    user_id: int,
    kind: str,
    expires_at: datetime,
    notify: bool = True,
    now: datetime | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    guard: "PersistedGuard | None" = None,
) -> ExpiryOutcome | None:
    """只处理**预览里选中的那一版**权益（R5）。

    谓词是 ``(group_id, user_id, kind, expires_at)`` 全等：调用方把确认时的目标
    快照带下来，所以既不会"处理 A 却记到 B"，也不会顺带处理同群新出现的到期行
    （``partial retry`` 不增加新目标）。行已经不存在/已经被续期 → 返回 ``None``，
    由调用方按"目标已变化"回报，而不是猜一个结果。
    """

    if not isinstance(expires_at, datetime):
        return None
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    stmt = (
        select(MemberEntitlement)
        .where(
            MemberEntitlement.group_id == int(group_id),
            MemberEntitlement.user_id == int(user_id),
            MemberEntitlement.kind == str(kind),
            MemberEntitlement.expires_at == expires_at,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None
    row_id = int(row.id)
    item = _expired_item(row)
    outcome = await _expire_row(
        session,
        bot,
        item,
        row_id,
        moment=moment,
        notify=notify,
        session_factory=session_factory,
        guard=guard,
    )
    await session.commit()
    return outcome


@dataclass(frozen=True, slots=True)
class ShopSourcePlan:
    """按**群**（不是按整个部署）决定商店到期处理的范围与准入（B13b 返工 v3）。

    * ``internal`` / ``external``：**当前仍有资格**、来源分别是 internal /
      external 的群。
    * ``guards``：每个已治理群（有身份行或有来源策略）的自动入口守卫，来自同一份
      视图快照；执行端必须用它，不能在真正撤销/出站前重新读策略来造守卫。
    * ``governed``：有 ``AuthorizedGroup`` 行（含已撤权）或有来源策略的群。缺守卫时
      必须 fail-closed 的正是这个集合。
    * ``policy_groups``：有持久来源记录的群（只读兼容视图用它判断"老库全量口径"）。
    """

    internal: list[int]
    external: list[int]
    guards: dict[int, PersistedGuard]
    governed: frozenset[int]
    policy_groups: frozenset[int]


async def _identity_group_ids(session: AsyncSession) -> set[int]:
    """有 ``AuthorizedGroup`` 行的群，**包含** ``bot_present=False`` 的已撤权群。

    撤权群的行还在，说明这个部署管理过它；纯历史群连行都没有。两者必须在同一个库里
    区分开：前者缺守卫要 fail-closed，后者保持改造前的全量语义。
    """

    rows = await session.scalars(select(AuthorizedGroup.group_id))
    return {int(value) for value in rows.all()}


async def shop_source_plan(session: AsyncSession) -> ShopSourcePlan:
    """按**群**算出商店到期处理的范围与每群准入守卫（B13b/C4 + 返工 v3）。

    父 v5 复现的混库场景：来源 A（external）与一个既没有 ``AuthorizedGroup`` 身份、
    也没有来源策略的纯历史群 LEGACY 共处一库。v2 用"整库有没有任何来源记录"当全局
    开关，修好撤权 managed 群的同时把 LEGACY 也一起漏掉了。现在逐群判定：

    * 有身份行或有策略 ⇒ 已治理：归本轮处理的群（``internal``）走守卫；不归本轮的
      （external / 已撤权）在 SQL 层被排除，缺守卫时也一律 fail-closed。
    * 两者都没有 ⇒ 纯历史群：不在上述集合里，执行端仍按改造前的全量口径处理。

    读策略失败直接抛：宁可这一轮不做，也不能在"读不到来源"时按全量跑，把已经切给
    外部 cron 的群又发一遍。
    """

    from bot.services import task_policy
    from bot.services.operations_guard import PersistedGuard
    from bot.services.quality_report import authorized_group_ids

    authorized = {int(value) for value in await authorized_group_ids(session)}
    identity = await _identity_group_ids(session)
    policy_groups = set(
        await task_policy.managed_group_ids(session, task_id="shop_expire")
    )
    governed = identity | policy_groups
    views = await task_policy.load_task_policies(
        session, task_id="shop_expire", group_ids=sorted(governed | authorized)
    )
    guards = {
        group: PersistedGuard.for_automatic_entry(
            group_id=group,
            task_id="shop_expire",
            policy_revision=int(views[group].revision),
        )
        for group in sorted(governed)
    }
    internal = sorted(
        group
        for group in authorized
        if views[group].source == task_policy.SOURCE_INTERNAL
    )
    external = sorted(
        group
        for group in authorized
        if views[group].source == task_policy.SOURCE_EXTERNAL
    )
    return ShopSourcePlan(
        internal=internal,
        external=external,
        guards=guards,
        governed=frozenset(governed),
        policy_groups=frozenset(policy_groups),
    )


async def shop_group_scope(session: AsyncSession) -> tuple[list[int] | None, list[int]]:
    """只读兼容视图：``(internal_scope, external_ids)``。

    一行来源记录都没有的老库仍返回 ``internal_scope=None``（改造前的"全量"口径），
    已经有来源记录时返回显式 internal 列表——这两个口径由历史测试固定，逐字不变。

    **执行入口不要拿它做范围决策**：它表达不了"纯历史群也要处理"，真正的范围与逐群
    fail-closed 判定在 :class:`ShopSourcePlan` 与 :func:`expire_due_entitlements` 里。
    """

    plan = await shop_source_plan(session)
    scope = plan.internal if plan.policy_groups else None
    return scope, plan.external


class ShopExpiryService:
    """常驻的到期清理循环（运维也可以手动跑 ``python -m bot.tools.shop_expire``）。"""

    def __init__(
        self,
        *,
        bot: object,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float | None = None,
        batch_limit: int | None = None,
    ) -> None:
        econ = _econ()
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(
            5.0,
            float(
                econ.expiry_check_seconds
                if check_interval_seconds is None
                else check_interval_seconds
            ),
        )
        self.batch_limit = max(
            1, econ.expiry_batch_limit if batch_limit is None else int(batch_limit)
        )

    async def run_once(self, *, now: datetime | None = None) -> list[ExpiryOutcome]:
        async with self.session_factory() as session:
            # B13b/C4：只处理"内部执行来源"的群；被切到外部 cron 的群绝不在内部
            # 这一轮里被处理（默认全群 internal ⇒ 范围 None，口径逐字不变）。
            # R-B1：同时拿到**每群**准入守卫（同一次来源判定的快照），交给真实撤销
            # /DELETE 的写边界复核。
            plan = await shop_source_plan(session)
            log.info(
                "shop expiry scope | internal=%d external=%d governed=%d",
                len(plan.internal),
                len(plan.external),
                len(plan.governed),
            )
            # v3（父 v5）：范围逐**群**决定，不再用"整库有没有来源记录"的全局开关。
            # internal 群走守卫；既没有身份也没有策略的纯历史群仍按改造前的全量口径
            # 处理；已治理但不归本轮（external / 已撤权）的群在 SQL 层就排除，免得
            # 它们的到期行把 limit 吃光。
            return await expire_due_entitlements(
                session,
                bot=self.bot,
                now=now,
                limit=self.batch_limit,
                exclude_group_ids=sorted(plan.governed - set(plan.internal)),
                session_factory=self.session_factory,
                guards=plan.guards,
                governed_groups=plan.governed,
                allow_unmanaged_legacy=True,
            )

    async def run_forever(self) -> None:
        log.info("shop expiry service started")
        consecutive_failures = 0
        while True:
            try:
                async with asyncio.timeout(_econ().expiry_pass_deadline_seconds):
                    outcomes = await self.run_once()
                if outcomes:
                    log.info("shop expiry pass | expired=%d", len(outcomes))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("shop expiry pass failed")
                consecutive_failures = record_background_failure(
                    service="shop expiry service",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            await asyncio.sleep(self.check_interval_seconds)


__all__ = [
    "AWARD_REASON_LOTTERY",
    "AWARD_REASON_REFUND",
    "EntitlementSnapshot",
    "ExpiredItem",
    "ExpiryOutcome",
    "KIND_PIN",
    "KIND_TAG",
    "LOTTERY_DAILY_LIMIT",
    "LOTTERY_PRICE",
    "LOTTERY_TABLE",
    "LOTTERY_TOTAL_WEIGHT",
    "LotteryPrize",
    "PIN_HOURS",
    "PIN_PRICE",
    "PinTarget",
    "SPEND_REASON_LOTTERY",
    "SPEND_REASON_PIN",
    "SPEND_REASON_TAG",
    "ShopExpiryService",
    "ShopReply",
    "TAG_DAYS_7D",
    "TAG_DAYS_30D",
    "TAG_MAX_LENGTH",
    "TAG_PRICE_7D",
    "TAG_PRICE_30D",
    "TagCheck",
    "TagRequest",
    "active_entitlement",
    "award_points",
    "buy_member_tag",
    "buy_pin",
    "challenge_spend_ref",
    "check_tag_text",
    "contains_emoji",
    "draw_prize",
    "due_entitlements",
    "entitlement_snapshot",
    "entitlement_expiry_key",
    "expire_due_entitlements",
    "expire_entitlement_target",
    "expiry_notice_receipt",
    "expected_lottery_value",
    "expiry_notice",
    "lottery_day",
    "lottery_draws_today",
    "lottery_prize_ref",
    "lottery_spend_ref",
    "minutes_left",
    "next_expiry",
    "parse_tag_request",
    "pin_spend_ref",
    "play_lottery",
    "purchase_stamp",
    "refund_points",
    "refund_ref",
    "render_balance",
    "render_shop_menu",
    "resolve_pin_target",
    "restore_entitlement",
    "tag_price",
    "tag_spend_ref",
    "tag_text_taken",
    "upsert_entitlement",
]
