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

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import MemberEntitlement, MemberPointAward, MemberPointSpend
from bot.services.background_health import record_background_failure
from bot.services.checkin import available_points, local_today, spend_points
from bot.utils.timezone import now_shanghai_naive

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


def check_tag_text(raw: object) -> TagCheck:
    """校验头衔文字；不合格时给一句用户看得懂的原因（不扣分）。"""

    text = str(raw or "").strip()
    if not text:
        return TagCheck(False, reason="头衔不能是空白，请输入 1-16 个字。")
    if len(text) > TAG_MAX_LENGTH:
        return TagCheck(
            False,
            reason=f"头衔最多 {TAG_MAX_LENGTH} 个字，你这条有 {len(text)} 个字。",
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
    days: int = TAG_DAYS_7D
    price: int = TAG_PRICE_7D
    error: str = ""


def tag_price(days: int) -> int:
    """7 天 30 分、30 天 80 分。"""

    return TAG_PRICE_30D if int(days) >= TAG_DAYS_30D else TAG_PRICE_7D


def parse_tag_request(raw: object) -> TagRequest:
    """解析 ``/tag`` 后面的文字：末尾的 ``30天`` / ``30d`` / ``--30`` 表示买长租。"""

    body = str(raw or "").strip()
    days = TAG_DAYS_7D
    parts = body.split()
    if len(parts) == 1 and parts[0].lower() in TAG_LONG_MARKERS:
        # 只写了时长没写头衔：按"忘了写文字"提示，不要真把头衔设成 "30天"
        return TagRequest(
            days=TAG_DAYS_30D,
            price=TAG_PRICE_30D,
            error="请先写头衔文字，例如：/tag 摸鱼冠军 30天",
        )
    if len(parts) > 1 and parts[-1].lower() in TAG_LONG_MARKERS:
        days = TAG_DAYS_30D
        body = " ".join(parts[:-1])
    check = check_tag_text(body)
    if not check.ok:
        return TagRequest(days=days, price=tag_price(days), error=check.reason)
    return TagRequest(text=check.text, days=days, price=tag_price(days))


# ---------------------------------------------------------------------------
# 抽奖奖池（概率是定死的，不要自行调整）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LotteryPrize:
    """奖池里的一项：``weight`` 是万分比。"""

    points: int
    weight: int
    label: str


#: 60% 谢谢参与 / 25% 3 分 / 10% 12 分 / 4% 40 分 / 1% 120 分
LOTTERY_TABLE: tuple[LotteryPrize, ...] = (
    LotteryPrize(0, 6000, "谢谢参与"),
    LotteryPrize(3, 2500, "3 分"),
    LotteryPrize(12, 1000, "12 分"),
    LotteryPrize(40, 400, "40 分"),
    LotteryPrize(120, 100, "120 分"),
)
LOTTERY_TOTAL_WEIGHT = 10000


def expected_lottery_value() -> float:
    """长期期望值（≈4.75 分/次，略低于 5 分成本：抽奖是积分回收口）。"""

    return sum(prize.points * prize.weight for prize in LOTTERY_TABLE) / LOTTERY_TOTAL_WEIGHT


def draw_prize(randbelow=None) -> LotteryPrize:
    """按奖池权重开一次奖；随机源默认是加密安全的 ``secrets.randbelow``。"""

    roller = randbelow if randbelow is not None else secrets.randbelow
    roll = int(roller(LOTTERY_TOTAL_WEIGHT))
    if roll < 0:
        roll = 0
    accumulated = 0
    for prize in LOTTERY_TABLE:
        accumulated += prize.weight
        if roll < accumulated:
            return prize
    # 只有随机源返回 >= 10000 时才可能走到这里
    return LOTTERY_TABLE[-1]


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


def purchase_stamp(now: datetime) -> str:
    """本次购买的时间戳：base36 秒（6 位）+ 毫秒（2 位）。

    一起购买的时间戳必须**稳定**（同一笔交易重试时用同一个 ref，唯一索引才能挡住
    重复扣分）且**唯一**（同一个人前后两次购买不能撞 key）。毫秒精度足够：
    同一个人在同一毫秒里下两单是不可能的。
    """

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    seconds = _base36(int(moment.timestamp()))
    millis = _base36(int(moment.microsecond // 1000)).rjust(2, "0")
    return f"{seconds}{millis}"


def tag_spend_ref(
    *, group_id: int, user_id: int, days: int, stamp: str
) -> str:
    sku = "shop-tag-30d" if int(days) >= TAG_DAYS_30D else "shop-tag-7d"
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
    try:
        async with session.begin_nested():
            session.add(
                MemberPointAward(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    points=amount,
                    reason=str(reason or "")[:64],
                    ref=str(ref)[:64],
                )
            )
    except IntegrityError:
        return False
    return True


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
    """写入/更新一条生效中的权益（一人一项，靠唯一索引兜底）。"""

    row = await active_entitlement(
        session, group_id=group_id, user_id=user_id, kind=kind
    )
    if row is None:
        try:
            async with session.begin_nested():
                session.add(
                    MemberEntitlement(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        kind=str(kind),
                        payload=str(payload)[:255],
                        ref=str(ref)[:64],
                        created_at=now,
                        expires_at=expires_at,
                    )
                )
            return
        except IntegrityError:
            # 并发插入（同一瞬间两个人各买一次是不可能的，但代码要能扛）
            row = await active_entitlement(
                session, group_id=group_id, user_id=user_id, kind=kind
            )
    if row is None:
        return
    row.payload = str(payload)[:255]
    row.ref = str(ref)[:64]
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
        f"机器人会把那条消息置顶 {PIN_HOURS} 小时（{PIN_PRICE} 分）。"
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
    """``/tag <文字>``：校验 → 扣分 → 设头衔 → 记到期时间；Telegram 失败就退款。"""

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

    stamp = purchase_stamp(moment)
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
    # 先落库扣分，再调 Telegram：网络慢或失败时不会有人趁机动别人的账户
    await session.commit()

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
        refunded = await _refund_and_reload(
            session, group_id=gid, user_id=uid, points=request.price, ref=ref
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

    bookkeeping_ok = True
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
        await session.commit()
    except Exception:
        # 头衔已经在群里生效了，不能退款；这里只把记账失败喊出来（到期清理会漏掉它）
        bookkeeping_ok = False
        log.exception(
            "shop tag entitlement write failed | group=%s user=%s ref=%s", gid, uid, ref
        )

    remaining = await available_points(session, group_id=gid, user_id=uid)
    when = expires_at.strftime("%Y-%m-%d %H:%M")
    tail = "" if bookkeeping_ok else "\n（到期记录写入异常，已通知管理员）"
    return ShopReply(
        "ok",
        f"<b>头衔已生效</b>\n「{escape(request.text)}」有效期到 {when}（{request.days} 天）。\n"
        f"当前可用 {remaining} 分。到期机器人会自动清除，想续费直接再发一次 /tag。{tail}",
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
) -> bool:
    """退款并提交；退款本身失败也要记日志（绝不能让用户钱没了东西也没有）。"""

    try:
        refunded = await refund_points(
            session,
            group_id=group_id,
            user_id=user_id,
            points=points,
            original_ref=ref,
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
    """``/top``：把自己的一条消息置顶 6 小时；失败退款。"""

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
    if existing is not None and existing.expires_at is not None:
        left = minutes_left(existing.expires_at, moment)
        return ShopReply(
            "already_pinned",
            f"你已经有 1 条置顶了，还有 {left} 分钟到期。等它到期后可以再买一次，"
            f"这次没有扣分。当前可用 {available} 分。",
            available=available,
            expires_at=existing.expires_at,
        )
    if available < PIN_PRICE:
        return ShopReply(
            "insufficient",
            _insufficient_text(price=PIN_PRICE, available=available),
            available=available,
        )

    stamp = purchase_stamp(moment)
    ref = pin_spend_ref(group_id=gid, user_id=uid, stamp=stamp)
    charged = await spend_points(
        session,
        group_id=gid,
        user_id=uid,
        points=PIN_PRICE,
        reason=SPEND_REASON_PIN,
        ref=ref,
    )
    if not charged:
        return ShopReply(
            "replay",
            "这笔购买已经处理过了，请重新发一次 /top。",
            available=await available_points(session, group_id=gid, user_id=uid),
        )
    await session.commit()

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
            session, group_id=gid, user_id=uid, points=PIN_PRICE, ref=ref
        )
        remaining = await available_points(session, group_id=gid, user_id=uid)
        head = (
            f"置顶失败（可能机器人没有置顶权限），已退回 {PIN_PRICE} 分。"
            if refunded
            else "置顶失败，退款也失败了，请联系管理员核账。"
        )
        return ShopReply(
            "telegram_failed",
            f"{head}当前可用 {remaining} 分。",
            available=remaining,
            refunded=refunded,
        )

    expires_at = moment + timedelta(hours=PIN_HOURS)
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
        await session.commit()
    except Exception:
        log.exception(
            "shop pin entitlement write failed | group=%s user=%s ref=%s", gid, uid, ref
        )

    remaining = await available_points(session, group_id=gid, user_id=uid)
    when = expires_at.strftime("%Y-%m-%d %H:%M")
    return ShopReply(
        "ok",
        f"<b>置顶成功</b>\n你的这条消息会一直置顶到 {when}（{PIN_HOURS} 小时后自动取消）。\n"
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


async def play_lottery(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    now: datetime | None = None,
    randbelow=None,
) -> ShopReply:
    """``/draw``：5 分一次，每天最多 10 次；中奖的分写奖励流水（不伪造签到）。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    gid, uid = int(group_id), int(user_id)
    available = await available_points(session, group_id=gid, user_id=uid)
    day = lottery_day(moment)

    used = await lottery_draws_today(session, group_id=gid, user_id=uid, day=day)
    if used >= LOTTERY_DAILY_LIMIT:
        return ShopReply(
            "daily_limit",
            f"今天已经抽了 {used} 次，每天最多 {LOTTERY_DAILY_LIMIT} 次，明天再来吧。"
            f"当前可用 {available} 分，这次没有扣分。",
            available=available,
        )
    if available < LOTTERY_PRICE:
        return ShopReply(
            "insufficient",
            _insufficient_text(price=LOTTERY_PRICE, available=available),
            available=available,
        )

    stamp = purchase_stamp(moment)
    ref = lottery_spend_ref(group_id=gid, user_id=uid, day=day, stamp=stamp)
    charged = await spend_points(
        session,
        group_id=gid,
        user_id=uid,
        points=LOTTERY_PRICE,
        reason=SPEND_REASON_LOTTERY,
        ref=ref,
    )
    if not charged:
        return ShopReply(
            "replay",
            "这次抽奖已经处理过了，请重新发一次 /draw。",
            available=await available_points(session, group_id=gid, user_id=uid),
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
                session, group_id=gid, user_id=uid, points=LOTTERY_PRICE, ref=ref
            )
            remaining = await available_points(session, group_id=gid, user_id=uid)
            head = (
                f"开奖时出了点问题，已退回 {LOTTERY_PRICE} 分。"
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
    net = int(prize.points) - LOTTERY_PRICE
    net_text = f"+{net}" if net >= 0 else str(net)
    left = max(0, LOTTERY_DAILY_LIMIT - (used + 1))
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
        f"当前可用 <b>{int(available)}</b> 分。积分靠每天在群里发 /checkin 签到攒。\n"
        "\n"
        f"<b>① 自定义头衔 · {TAG_DAYS_7D} 天 —— {TAG_PRICE_7D} 分</b>\n"
        "用法：发 /tag 你的头衔\n"
        "例：/tag 摸鱼冠军\n"
        "\n"
        f"<b>② 自定义头衔 · {TAG_DAYS_30D} 天 —— {TAG_PRICE_30D} 分</b>\n"
        "用法：发 /tag 你的头衔 30天\n"
        "例：/tag 摸鱼冠军 30天\n"
        "\n"
        f"<b>③ 置顶自己的求助 · {PIN_HOURS} 小时 —— {PIN_PRICE} 分</b>\n"
        "用法：长按你自己发的那条消息 → 回复 → 发送 /top\n"
        "\n"
        f"<b>④ 抽奖一次 —— {LOTTERY_PRICE} 分</b>\n"
        "用法：发 /draw（每人每天最多 10 次）\n"
        "奖池：谢谢参与、3 分、12 分、40 分、120 分\n"
        "\n"
        f"<i>头衔 1-{TAG_MAX_LENGTH} 个字、不能带表情、不能和别人重名；到期会自动清除。"
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
    limit: int = _SHOP_EXPIRY_BATCH_LIMIT,
) -> list[MemberEntitlement]:
    """到期（``expires_at <= now``）的权益，按到期时间从早到晚。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    rows = await session.execute(
        select(MemberEntitlement)
        .where(MemberEntitlement.expires_at <= moment)
        .order_by(MemberEntitlement.expires_at, MemberEntitlement.id)
        .limit(max(1, int(limit)))
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


async def _revoke(bot: object, row: MemberEntitlement) -> tuple[bool, str]:
    """把一条到期权益在 Telegram 侧撤掉；返回 (成功?, 说明)。"""

    gid, uid = int(row.group_id), int(row.user_id)
    try:
        if str(row.kind) == KIND_TAG:
            await _set_member_tag(bot, gid, uid, "")
            return True, "已清除头衔"
        if str(row.kind) == KIND_PIN:
            await _unpin_message(bot, gid, int(row.payload or 0))
            return True, "已取消置顶"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # 用户退群、消息被删、机器人被撤权都会走到这里。记下来，但行照样删：
        # 否则一条永远失败的记录会让每次扫描都卡在同一个地方。
        log.warning(
            "shop expiry revoke failed | kind=%s group=%s user=%s payload=%s error=%s",
            row.kind,
            gid,
            uid,
            row.payload,
            exc,
        )
        return False, f"Telegram 侧失败：{type(exc).__name__}"
    return True, f"未知权益类型 {row.kind}，已跳过"


async def _notify_expiry(bot: object, item: ExpiredItem) -> bool:
    """先私聊提醒，私聊失败就发到群里；都失败只记日志。"""

    send = getattr(bot, "send_message", None)
    if not callable(send):
        return False
    text = expiry_notice(item)
    for chat_id in (item.user_id, item.group_id):
        try:
            async with asyncio.timeout(_TELEGRAM_CALL_TIMEOUT_SECONDS):
                await send(chat_id=int(chat_id), text=text)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug(
                "shop expiry notice failed | chat=%s kind=%s",
                chat_id,
                item.kind,
                exc_info=True,
            )
    log.warning(
        "shop expiry notice undeliverable | group=%s user=%s kind=%s",
        item.group_id,
        item.user_id,
        item.kind,
    )
    return False


async def expire_due_entitlements(
    session: AsyncSession,
    *,
    bot: object,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
    limit: int = _SHOP_EXPIRY_BATCH_LIMIT,
) -> list[ExpiryOutcome]:
    """扫描到期的权益 → 清头衔 / 取消置顶 → 删行。

    **幂等**：Telegram 侧的两个操作（把 tag 设成空、取消置顶）本身就是幂等的，
    而行只有在撤下动作跑过之后才删。所以进程重启、重复执行、一次处理多条都安全。
    ``dry_run=True`` 时只返回"打算做什么"，不碰 Telegram 也不改数据库。
    """

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    rows = await due_entitlements(session, now=moment, limit=limit)
    outcomes: list[ExpiryOutcome] = []
    for row in rows:
        item = _expired_item(row)
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
        ok, detail = await _revoke(bot, row)
        await session.delete(row)
        # 只有真的撤下来了才提醒"已清除"，否则等于骗用户
        notified = await _notify_expiry(bot, item) if (notify and ok) else False
        outcomes.append(
            ExpiryOutcome(
                item=item,
                ok=ok,
                action=item.label,
                detail=detail,
                notified=notified,
            )
        )
    if outcomes and not dry_run:
        await session.commit()
    return outcomes


class ShopExpiryService:
    """常驻的到期清理循环（运维也可以手动跑 ``python -m bot.tools.shop_expire``）。"""

    def __init__(
        self,
        *,
        bot: object,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = _SHOP_EXPIRY_CHECK_SECONDS,
        batch_limit: int = _SHOP_EXPIRY_BATCH_LIMIT,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))
        self.batch_limit = max(1, int(batch_limit))

    async def run_once(self, *, now: datetime | None = None) -> list[ExpiryOutcome]:
        async with self.session_factory() as session:
            return await expire_due_entitlements(
                session,
                bot=self.bot,
                now=now,
                limit=self.batch_limit,
            )

    async def run_forever(self) -> None:
        log.info("shop expiry service started")
        consecutive_failures = 0
        while True:
            try:
                async with asyncio.timeout(_SHOP_EXPIRY_PASS_DEADLINE_SECONDS):
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
    "check_tag_text",
    "contains_emoji",
    "draw_prize",
    "due_entitlements",
    "expire_due_entitlements",
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
    "tag_price",
    "tag_spend_ref",
    "tag_text_taken",
    "upsert_entitlement",
]
