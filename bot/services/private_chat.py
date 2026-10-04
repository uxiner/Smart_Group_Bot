"""1 对 1 私聊（DM）：准入判定 + 用量配额 + 回复组装 + 提示文案。

用户口径（2026-10-03 拍板）：

1. **准入**：只有「最高管理员授权开通 Smart_Bot 的群组」里的成员能用私聊。
   非成员只回一条固定引导语，**不进模型、不花钱**。最高管理员始终豁免。
2. **配额（阶梯）**：普通成员 100 条/天、群管理员 500 条/天；两组各有自己的全局
   上限（20000 / 100000 条/天，互不占用）；最高管理员不设限、不计数。
3. **内容**：私聊不做群规审核，NSFW 也放开——群里那条「任何群都不允许发
   NSFW 图/视频」的底线**只针对群聊通道**，这里一个字都没动。
4. **历史**：私聊正文只落**私聊自己的表**（``private_chat_messages``），不进群归档/
   记忆/向量；配额仍只记「条数」。历史按 token 预算装配，机器人重启不失忆。

设计取舍：

* 私聊**必回**，所以这条路径不经过 ``decision`` 阶段（省一次调用），模型走
  ``main`` 阶段（``LLMService.chat`` 的默认标签）。
* 准入结果按 TTL 缓存，避免每条消息都打一次 ``getChatMember``。
* 配额落在 ``private_chat_usage`` 表（进程重启不清零），全局行用 ``user_id=0``。
* 历史**优先读库**、内存缓冲只做兜底：读库失败时行为退回改造前（最近 12 轮），
  不会因为存储抖动而整段失忆。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.db.models import PrivateChatMessage, PrivateChatUsage
from bot.services.authz import is_super_admin_user_id, list_authorized_groups
from bot.services.checkin import local_today
from bot.services.context_gate import (
    CONTEXT_TOKEN_BUDGET,
    assemble_context_within_budget,
)
from bot.services.group_public_context import (
    GROUP_PUBLIC_HEADER_BLOCK,
    render_group_public_messages,
)
from bot.services.long_term_memory import (
    LONG_TERM_MEMORY_HEADER_BLOCK,
    render_facts_block,
)
from bot.services.model_limits import auto_window_for
from bot.services.payload_fit import (
    LAYER_HISTORY,
    LAYER_MEMORY_RECALL,
    LAYER_SEARCH_RECORDS,
    tag_context_layers,
)
from bot.services.reply_output import (
    REPLY_OUTPUT_AWARENESS,
    REPLY_OUTPUT_PROTOCOL,
    REPLY_RICH_FORMATTING,
)
from bot.services.search_memory import (
    SEARCH_RECORDS_HEADER_BLOCK,
    render_search_record_messages,
)
from bot.utils.bot_identity import build_bot_identity_context
from bot.utils.conversation_context import (
    build_current_turn_focus_message,
    format_recent_group_context,
)
from bot.utils.prompts import get_prompt, with_persona
from bot.utils.project_info import build_bot_project_info_context
from bot.utils.runtime_context import (
    build_current_sender_context,
    build_current_time_context,
)
from bot.utils.security import (
    build_defended_system,
    clean_multiline_text,
    sanitize_history_for_llm,
    wrap_untrusted_multiline,
)
from bot.utils.timezone import now_shanghai_naive
from bot.utils.tokens import estimate_text_tokens

log = logging.getLogger(__name__)

#: 全局合计行用的哨兵 user_id（真实 Telegram 用户 id 恒为正）
GLOBAL_COUNTER_USER_ID = 0
#: 群管理员合计行的哨兵 user_id（真实 Telegram 用户 id 恒为正；本人行也恒为正）
ADMIN_GLOBAL_COUNTER_USER_ID = -1

#: 阶梯配额（用户口径 2026-10-03）
DEFAULT_PER_USER_DAILY_LIMIT = 100        # 普通成员：每人每天
ADMIN_PER_USER_DAILY_LIMIT = 500          # 群管理员：每人每天
DEFAULT_GLOBAL_DAILY_LIMIT = 20_000       # 普通成员合计
ADMIN_GLOBAL_DAILY_LIMIT = 100_000        # 群管理员合计

#: 准入档位：档位同时决定配额阶梯
TIER_SUPER = "super"    # 最高管理员：不设限、不计数
TIER_ADMIN = "admin"    # 授权群的群主/管理员：500/天
TIER_MEMBER = "member"  # 授权群的普通成员：100/天
TIER_NONE = "none"      # 不在任何授权群
TIER_UNKNOWN = "unknown"  # 查不通，无法下结论

#: 私聊历史的保留轮数（**只用于内存兜底缓冲**，进程重启即空）
HISTORY_MAX_TURNS = 12
#: 私聊正文的长度上限（与群聊口径一致）
PRIVATE_INPUT_LIMIT = 1000

#: 私聊历史装配的默认 token 预算：272K = 278528（全项目统一用这个精确数字）。
#: **2026-10-04 之后**：``auto`` 模式下这只是查不到模型元数据时的保守降级值；
#: 拿到真实窗口（实测 1,000,000）时按 ``窗口 − PRIVATE_HISTORY_RESERVE_TOKENS`` 给。
PRIVATE_HISTORY_TOKEN_BUDGET = 278_528
#: token 预算的夹取范围（与 runtime_config 里该字段的 ge/le 保持一致）
PRIVATE_HISTORY_TOKEN_BUDGET_MIN = 1024
PRIVATE_HISTORY_TOKEN_BUDGET_MAX = 2_000_000
#: 自动匹配模型窗口时留给「系统提示词 + 本轮消息 + 检索块 + 回复预留」的余量。
PRIVATE_HISTORY_RESERVE_TOKENS = 32_768

#: 私聊历史的轮数安全上限。**上限的理由**：token 预算才是真正的闸门，但预算只在
#: 「内容本身够长」时才会先咬住——对方连发几万条一个字的消息时，272K 预算能装下
#: 20 多万行，按用户读库、组装、正则估算就成了每条私聊的固定开销。单轮 = 用户 + 回复
#: = 2 行，所以单次最多读 ``2 * 500 = 1000`` 行；正常私聊在保留期内远达不到这个数。
PRIVATE_HISTORY_MAX_TURNS = 500

#: 私聊历史的默认保留天数（与 ``memory_retention_days`` 同样的夹取口径：1..365）
PRIVATE_HISTORY_RETENTION_DAYS = 30
PRIVATE_HISTORY_RETENTION_DAYS_MIN = 1
PRIVATE_HISTORY_RETENTION_DAYS_MAX = 365

#: 每条历史消息在预算里额外占的固定开销（角色、消息分隔等），与群聊同口径（+12）
_HISTORY_MESSAGE_TOKEN_OVERHEAD = 12
#: 单条超长消息被截断时补的说明（截断必须留痕，不能让人以为对方只说了这半句）
HISTORY_TRUNCATION_NOTE = "…（本条过长，已截断）"

#: 注入给模型的**系统资料块**标记：不是对话内容，**一律不落库**。
#: 目前私聊会注入三种：本轮联网检索（``[WEB_SEARCH_RESULTS]``）、历史检索留档
#: （``[SEARCH_RECORDS]``，第 3 期）、群聊公开记录参考（``[群聊公开记录]``，第 3 期 B）。
#: 最后一种虽然是我们自己从归档里取的，但它同样是「群聊里的公开内容」，不是他在私聊里
#: 说过的话，绝不进私聊历史。
INJECTED_BLOCK_MARKERS = (
    "[WEB_SEARCH_RESULTS]",
    "[SEARCH_RECORDS]",
    "[群聊公开记录]",
)

#: 私聊历史留存清理的巡检间隔（秒）。一天跑几次足够：过期行早删晚删都不影响
#: 对话正确性，只要保证库不无限涨。
_HISTORY_PRUNE_INTERVAL_SECONDS = 6 * 3600

#: 给视觉模型看的私聊版指令：只描述，不出审核结论、不吐 JSON。
DM_VISION_PROMPT = (
    "请用中文简要描述这张图片，供接下来的对话使用：先说清画面内容（人物/物体/场景），"
    "再把图中可见的文字原样列出（如果有）。只输出描述本身，不要输出任何审核结论、"
    "不要输出 JSON、不要加标题。"
)


# ---------------------------------------------------------------------------
# 提示文案
# ---------------------------------------------------------------------------

NOT_MEMBER_NOTICE = (
    "抱歉，私聊只对已授权的群里成员开放。\n"
    "如果你是群成员，请先在群里发一条消息，再回来私聊我试试。"
)
ACCESS_UNKNOWN_NOTICE = "刚没能确认你的群成员身份，稍等一会儿再发一次试试。"
LIMIT_NOTICE = "今天聊得有点多啦，先休息一下——明天再继续吧。"
GLOBAL_LIMIT_NOTICE = "今天找我聊天的人有点多，我有点跟不上了，明天再聊吧。"
MEDIA_UNSUPPORTED_NOTICE = "私聊里我目前只能看文字和图片，视频/文件/语音还看不了。"
BUSY_NOTICE = "刚走神了一下，这条没接住，再发一次好吗？"


# ---------------------------------------------------------------------------
# 准入判定
# ---------------------------------------------------------------------------


class MemberAccessCache:
    """``user_id -> 准入档位`` 的 TTL 缓存（纯内存，进程重启即空）。

    存的是档位字符串而不是布尔值：档位既表示「能不能用」，也表示「配额走哪一档」，
    一次查询同时拿到两件事，不用为配额再打一次 ``getChatMember``。

    第 3 期起还顺带缓存**已确认他在里面的授权群 id**：B 项（群 → 私聊参考公开记录）
    需要「该用户可访问的群」，而准入判定本来就已经对每个授权群打过一次
    ``getChatMember``——那次查询的命中结果就是可访问群，不缓存就得为每条私聊再打一轮。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_users: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_users = max(16, int(max_users))
        self._clock = clock or time.monotonic
        self._entries: dict[int, tuple[str, float, tuple[int, ...]]] = {}

    def _live(self, user_id: int) -> tuple[str, tuple[int, ...]] | None:
        entry = self._entries.get(int(user_id))
        if entry is None:
            return None
        tier, expires_at, group_ids = entry
        if expires_at <= self._clock():
            self._entries.pop(int(user_id), None)
            return None
        return tier, group_ids

    def get(self, user_id: int) -> str | None:
        live = self._live(user_id)
        return None if live is None else live[0]

    def get_groups(self, user_id: int) -> tuple[int, ...]:
        """缓存里已确认的「他在里面的授权群」；没缓存/已过期就返回空元组。"""

        live = self._live(user_id)
        return () if live is None else live[1]

    def put(
        self,
        user_id: int,
        tier: str,
        group_ids: Iterable[Any] | None = None,
    ) -> None:
        if len(self._entries) >= self.max_users and int(user_id) not in self._entries:
            # 容量满了先清最老的一批：准入结果过期即失效，清掉只会多打一次 API
            self._entries.clear()
        try:
            groups = tuple(dict.fromkeys(int(item) for item in (group_ids or [])))
        except (TypeError, ValueError):
            groups = ()
        self._entries[int(user_id)] = (
            str(tier),
            self._clock() + self.ttl_seconds,
            groups,
        )

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_member_cache = MemberAccessCache()


def member_cache() -> MemberAccessCache:
    """模块级共享缓存（测试里可以换成自己的实例）。"""

    return _member_cache


#: Telegram 对「不在群里的人」抛的不是 ``status="left"``，而是一个 Bad Request：
#: ``member not found``（真机实测确认）。这是**明确的否定**，必须与「查不通」分开——
#: 否则陌生人会永远收到「稍等再试」，而且每条消息都重打一次 API（既误导又白花调用）。
_NOT_MEMBER_HINTS = (
    "member not found",
    "user not found",
    "participant not found",
)


def _is_definitive_absent(exc: BaseException) -> bool:
    """这个异常是否等于「确定不在群里」（而不是查询失败）。"""

    detail = str(exc).lower()
    return any(hint in detail for hint in _NOT_MEMBER_HINTS)


def _member_tier(member: Any) -> str:
    """``ChatMember`` → 准入档位。

    群主与群管理员同档（都算「群管理员」，500/天）；``restricted`` 要看
    ``is_member``：被禁言但还在群 = 普通成员；被踢但状态还没刷新成 ``kicked``
    的旧数据不算成员。
    """

    status = str(getattr(member, "status", "") or "").lower()
    if status in ("creator", "administrator"):
        return TIER_ADMIN
    if status == "member":
        return TIER_MEMBER
    if status == "restricted" and bool(getattr(member, "is_member", False)):
        return TIER_MEMBER
    return TIER_NONE


@dataclass(frozen=True)
class AccessVerdict:
    """一次私聊准入判定的结果：放行与否 + 档位（档位决定配额阶梯）。

    ``group_ids`` 是这次判定里**已确认他在里面的授权群**（第 3 期 B 项用）。它是
    「准入判定的副产品」：判定本来就要对每个授权群打一次 ``getChatMember``，命中
    的那些群 id 顺手带出来，私聊参考群聊公开记录时就不必再查一遍 Telegram。
    """

    allowed: bool | None  # None = 无法确认
    tier: str
    detail: str = ""
    group_ids: tuple[int, ...] = ()

    @property
    def is_super(self) -> bool:
        return self.tier == TIER_SUPER

    @property
    def is_admin(self) -> bool:
        return self.tier == TIER_ADMIN


async def resolve_access(
    bot: Any,
    session: Any,
    settings: Any,
    user_id: int,
    *,
    cache: MemberAccessCache | None = None,
) -> AccessVerdict:
    """判定「这个私聊用户是谁」：最高管理员 / 群管理员 / 普通成员 / 都不是。

    ``allowed is None`` 表示**无法确认**（Telegram 侧查询异常）：调用方应回一条
    「稍后再试」，既不进模型也不计配额——按不确定就放行会变成免费代理，按不确定
    就拒绝会误伤真人，所以单独给一条退路。

    档位取「所有授权群里最高的那个」：在 A 群是普通成员、在 B 群是管理员 → 管理员。

    第 3 期起返回值还带上**已确认他在里面的授权群 id**（``AccessVerdict.group_ids``）：
    B 项（群 → 私聊参考公开记录）只允许读「该用户可访问的群」，而这些群正是这次判定
    已经逐个确认过的——不额外打 Telegram API，也不靠猜测。
    """

    uid = int(user_id)
    if is_super_admin_user_id(uid, settings):
        # 最高管理员豁免准入，**不打任何额外查询**（既有口径）：他可见的群由调用方
        # 在真正要用群聊公开记录时再取（全部授权群），准入这条路保持零查询。
        return AccessVerdict(True, TIER_SUPER)

    store = cache if cache is not None else _member_cache
    hit = store.get(uid)
    if hit is not None:
        groups = store.get_groups(uid)
        if hit == TIER_NONE:
            return AccessVerdict(False, TIER_NONE, group_ids=groups)
        return AccessVerdict(True, hit, group_ids=groups)

    try:
        groups = await list_authorized_groups(session)
    except Exception as exc:  # 数据库异常同样视为「无法确认」
        log.warning("private chat: 授权群查询失败 | user=%s | error=%s", uid, exc)
        return AccessVerdict(None, TIER_UNKNOWN, "authorized group lookup failed")

    unknown = False
    best = TIER_NONE
    #: 已确认「他在里面」的授权群（准入判定的副产品，供 B 项读公开记录用）
    confirmed: list[int] = []
    for row in groups:
        group_id = int(row.group_id)
        try:
            member = await bot.get_chat_member(chat_id=group_id, user_id=uid)
        except Exception as exc:
            if _is_definitive_absent(exc):
                # 「member not found」= 确定不在这个群，继续看别的授权群
                log.info(
                    "private chat: 不在授权群内 | group=%s user=%s", row.group_id, uid
                )
                continue
            log.warning(
                "private chat: getChatMember 失败 | group=%s user=%s | error=%s",
                row.group_id,
                uid,
                exc,
            )
            unknown = True
            continue
        tier = _member_tier(member)
        if tier in (TIER_ADMIN, TIER_MEMBER):
            confirmed.append(group_id)
        if tier == TIER_ADMIN:
            store.put(uid, TIER_ADMIN, confirmed)
            return AccessVerdict(
                True, TIER_ADMIN, group_ids=tuple(confirmed)
            )
        if tier == TIER_MEMBER:
            best = TIER_MEMBER

    if best == TIER_MEMBER:
        # 还有群没查成时先不缓存：下次重查有可能升档（普通成员 → 管理员）
        if not unknown:
            store.put(uid, TIER_MEMBER, confirmed)
        return AccessVerdict(True, TIER_MEMBER, group_ids=tuple(confirmed))

    if unknown:
        # 有群没查成 → 不下结论，也不缓存（下一次重试）
        return AccessVerdict(None, TIER_UNKNOWN, "telegram lookup failed")

    store.put(uid, TIER_NONE, ())
    return AccessVerdict(False, TIER_NONE)


async def confirm_authorized_group_member(
    bot: Any,
    session: Any,
    settings: Any,
    user_id: int,
    *,
    cache: MemberAccessCache | None = None,
) -> bool | None:
    """兼容包装：只要布尔结论（``None`` = 无法确认）。"""

    return (await resolve_access(bot, session, settings, user_id, cache=cache)).allowed


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaOutcome:
    """一次私聊用量的判定结果。"""

    allowed: bool
    reason: str  # ok / user_limit / global_limit
    user_used: int
    per_user_limit: int
    global_used: int
    global_limit: int
    tier: str = ""  # member / admin


def local_day_key(now: Any | None = None) -> str:
    """本地自然日键（``YYYY-MM-DD``，与签到表同口径）。"""

    if now is None:
        day = local_today()
    else:
        day = local_today(now)
    return str(day)


def _format_clock(stamp: Any) -> str:
    """把 ``updated_at`` 渲染成 ``HH:MM``；拿不到就返回空串。"""

    if stamp is None:
        return ""
    text = str(getattr(stamp, "strftime", lambda _f: "")("%H:%M") or "") or str(stamp)[11:16]
    return text.strip()


async def last_contact_record(session: Any, *, user_id: int, day: str | None = None) -> str:
    """返回「上一次私聊」的真实时间（如 ``2026-10-02 21:03``），没有则空串。

    数据源就是本人已有的配额行（``usage_date < 今天`` 的最新一条），不新增任何采集。
    只给最高管理员用：亲密度台词（「你今天找我比昨天晚了四十分钟」）必须有真实依据，
    拿不到就不给这一段——宁可不说，也绝不编造时间、承诺或事件。
    """

    uid = int(user_id)
    today = str(day or local_day_key())
    try:
        row = (
            await session.execute(
                select(PrivateChatUsage)
                .where(
                    PrivateChatUsage.user_id == uid,
                    PrivateChatUsage.usage_date < today,
                )
                .order_by(PrivateChatUsage.usage_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception as exc:  # 读失败就当没有：不能因为彩蛋拖垮回复
        log.warning("private chat: 考勤记录查询失败 | user=%s | error=%s", uid, exc)
        return ""
    if row is None:
        return ""
    clock = _format_clock(getattr(row, "updated_at", None))
    return f"{row.usage_date} {clock}".strip()


async def _bump(session: Any, *, user_id: int, day: str, stamp: Any) -> int:
    """把某个计数行 +1 并返回新值（原子 UPSERT，不会两行）。"""

    stmt = (
        sqlite_insert(PrivateChatUsage)
        .values(user_id=int(user_id), usage_date=str(day), messages=1, updated_at=stamp)
        .on_conflict_do_update(
            index_elements=["user_id", "usage_date"],
            set_={
                "messages": PrivateChatUsage.messages + 1,
                "updated_at": stamp,
            },
        )
        .returning(PrivateChatUsage.messages)
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def record_contact(
    session: Any, *, user_id: int, day: str | None = None, stamp: Any | None = None
) -> None:
    """只记一笔「他来找过你」，**不做任何限额判定**。

    最高管理员不吃配额（``consume_daily_quota`` 根本不会为他跑），但亲密度考勤要的是
    真实时间，所以超管这条路只记流水不设闸门。表名本来就是「私聊用量」——超管的发言
    也是真实用量，只是不设上限。
    """

    stamp = stamp or now_shanghai_naive()
    key = str(day or local_day_key(stamp))
    try:
        await _bump(session, user_id=int(user_id), day=key, stamp=stamp)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        log.warning("private chat: 联系记录写入失败 | user=%s | error=%s", user_id, exc)


async def consume_daily_quota(
    session: Any,
    *,
    user_id: int,
    is_admin: bool = False,
    per_user_limit: int | None = None,
    global_limit: int | None = None,
    day: str | None = None,
    stamp: Any | None = None,
) -> QuotaOutcome:
    """先扣再用：返回是否放行。

    档位（普通成员 / 群管理员）同时决定每人上限与**本档的全局上限**——两组各记一个
    全局计数行，互不占用（管理员花掉额度不影响普通成员）。两道闸门在同一次事务里扣，
    任何一道超限就整体回滚（这轮不吃配额），不会留下「扣了但没派上用场」的脏计数。
    """

    uid = int(user_id)
    tier = TIER_ADMIN if is_admin else TIER_MEMBER
    if per_user_limit is None:
        per_user_limit = (
            ADMIN_PER_USER_DAILY_LIMIT if is_admin else DEFAULT_PER_USER_DAILY_LIMIT
        )
    if global_limit is None:
        global_limit = (
            ADMIN_GLOBAL_DAILY_LIMIT if is_admin else DEFAULT_GLOBAL_DAILY_LIMIT
        )
    global_row = ADMIN_GLOBAL_COUNTER_USER_ID if is_admin else GLOBAL_COUNTER_USER_ID
    stamp = stamp or now_shanghai_naive()
    key = str(day or local_day_key(stamp))

    user_used = await _bump(session, user_id=uid, day=key, stamp=stamp)
    global_used = await _bump(session, user_id=global_row, day=key, stamp=stamp)

    if user_used > int(per_user_limit):
        await session.rollback()
        return QuotaOutcome(
            allowed=False,
            reason="user_limit",
            user_used=user_used - 1,
            per_user_limit=int(per_user_limit),
            global_used=global_used - 1,
            global_limit=int(global_limit),
            tier=tier,
        )
    if global_used > int(global_limit):
        await session.rollback()
        return QuotaOutcome(
            allowed=False,
            reason="global_limit",
            user_used=user_used - 1,
            per_user_limit=int(per_user_limit),
            global_used=global_used - 1,
            global_limit=int(global_limit),
            tier=tier,
        )

    await session.commit()
    return QuotaOutcome(
        allowed=True,
        reason="ok",
        user_used=user_used,
        per_user_limit=int(per_user_limit),
        global_used=global_used,
        global_limit=int(global_limit),
        tier=tier,
    )


def quota_notice(outcome: QuotaOutcome) -> str:
    """超限时回哪句话（全局超限和本人超限分开说，免得让人以为自己被针对）。"""

    if outcome.reason == "global_limit":
        return GLOBAL_LIMIT_NOTICE
    return LIMIT_NOTICE


# ---------------------------------------------------------------------------
# 提示节流（同一条提示每人每 N 分钟只说一次）
# ---------------------------------------------------------------------------


class NoticeThrottle:
    """``(user_id, key) -> 上次提示时刻`` 的内存节流。"""

    def __init__(
        self,
        *,
        cooldown_seconds: float = 3600.0,
        max_users: int = 8192,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.max_users = max(16, int(max_users))
        self._clock = clock or time.monotonic
        self._seen: dict[tuple[int, str], float] = {}

    def allow(self, user_id: int, key: str) -> bool:
        now = self._clock()
        stamp = self._seen.get((int(user_id), str(key)))
        if stamp is not None and now - stamp < self.cooldown_seconds:
            return False
        if len(self._seen) >= self.max_users:
            self._seen.clear()
        self._seen[(int(user_id), str(key))] = now
        return True

    def clear(self) -> None:
        self._seen.clear()


_notice_throttle = NoticeThrottle()


def notice_throttle() -> NoticeThrottle:
    return _notice_throttle


# ---------------------------------------------------------------------------
# 私聊历史：落库（private_chat_messages）+ 按 token 预算装配
# ---------------------------------------------------------------------------


class PrivateHistoryStore:
    """每个用户保留最近 ``HISTORY_MAX_TURNS`` 轮的对话文本（**进程内存兜底**）。

    落库之后的定位变了：**库是唯一事实来源**，这个内存缓冲只做一件事——读库失败或
    暂时读不到（这一轮刚写完还没提交、库被锁住）时，私聊能退回改造前的行为：最近
    12 轮仍在上下文里，而不是整段失忆。

    条数上限刻意保持 12 轮不变：它不再是「私聊能记住多久」的答案（库 + token 预算
    才是），改动它只会让兜底路径和既有用例的行为一起漂移。
    """

    def __init__(self, *, max_users: int = 4096) -> None:
        self.max_users = max(16, int(max_users))
        self._turns: dict[int, deque[tuple[str, str]]] = {}

    def append(self, user_id: int, role: str, content: str) -> None:
        turn = str(content or "").strip()
        if not turn:
            return
        uid = int(user_id)
        if uid not in self._turns and len(self._turns) >= self.max_users:
            self._turns.clear()
        buf = self._turns.get(uid)
        if buf is None:
            buf = deque(maxlen=HISTORY_MAX_TURNS * 2)
            self._turns[uid] = buf
        buf.append((str(role), turn))

    def history(self, user_id: int) -> list[dict[str, str]]:
        buf = self._turns.get(int(user_id))
        if not buf:
            return []
        return [{"role": role, "content": content} for role, content in buf]

    def clear(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._turns.clear()
        else:
            self._turns.pop(int(user_id), None)


_history_store = PrivateHistoryStore()


def history_store() -> PrivateHistoryStore:
    return _history_store


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    """把配置值夹到 ``[low, high]``；拿不到/不是数字就用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return min(int(high), max(int(low), number))


def bounded_history_token_budget(value: Any) -> int:
    """私聊历史 token 预算的夹取口径（与 runtime_config 的字段约束一致）。"""

    return _bounded_int(
        value,
        default=PRIVATE_HISTORY_TOKEN_BUDGET,
        low=PRIVATE_HISTORY_TOKEN_BUDGET_MIN,
        high=PRIVATE_HISTORY_TOKEN_BUDGET_MAX,
    )


def bounded_history_retention_days(value: Any) -> int:
    """私聊历史保留天数的夹取口径（与 ``memory_retention_days`` 同为 1..365）。"""

    return _bounded_int(
        value,
        default=PRIVATE_HISTORY_RETENTION_DAYS,
        low=PRIVATE_HISTORY_RETENTION_DAYS_MIN,
        high=PRIVATE_HISTORY_RETENTION_DAYS_MAX,
    )


def _bot_setting(settings: Any, name: str, default: Any) -> Any:
    """从 ``settings.bot`` 读一个字段；缺项/``None`` 都退回默认值。

    私聊的准入、配额、回复都不该因为「配置里少了这一项」而 500，所以这里一律
    宽容读取：拿不到就用默认值（历史装配有安全上限，不会因此失控）。
    """

    bot = getattr(settings, "bot", None)
    value = getattr(bot, name, None) if bot is not None else None
    return default if value is None else value


def private_history_token_budget(settings: Any) -> int:
    """当前生效的私聊历史 token 预算。

    ``auto``（默认）且拿到了真实模型窗口时：按 ``窗口 − 本地余量`` 装配，已知主模型
    不再被旧的 272K 固定值压住；只有拿不到可信窗口时才退回兼容字段（迁移前口径）。
    """

    window = auto_window_for(settings)
    if window is not None:
        return bounded_history_token_budget(
            max(
                PRIVATE_HISTORY_TOKEN_BUDGET_MIN,
                window - PRIVATE_HISTORY_RESERVE_TOKENS,
            )
        )
    return bounded_history_token_budget(
        _bot_setting(
            settings,
            "private_chat_history_token_budget",
            PRIVATE_HISTORY_TOKEN_BUDGET,
        )
    )


def private_history_retention_days(settings: Any) -> int:
    """当前生效的私聊历史保留天数（默认 30，夹取 1..365）。"""

    return bounded_history_retention_days(
        _bot_setting(
            settings,
            "private_chat_history_retention_days",
            PRIVATE_HISTORY_RETENTION_DAYS,
        )
    )


def _int_message_id(value: Any) -> int | None:
    """Telegram 消息 id 恒为正整数；拿不到整数就返回 None（测试替身也走这条路）。"""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def history_message_key(message_id: Any, role: str) -> str:
    """一轮里某一行的幂等键：``u:<message_id>``（用户）/ ``a:<message_id>``（回复）。

    拿不到整数 message_id 时退化成一次性随机键：宁可多写一行，也不能把两条不同的
    消息当成同一行互相顶掉（那会真的丢对话）。
    """

    prefix = "a" if str(role or "").strip().lower() == "assistant" else "u"
    mid = _int_message_id(message_id)
    if mid is None:
        return f"{prefix}:{uuid4().hex}"
    return f"{prefix}:{mid}"


def strip_injected_blocks(text: str) -> str:
    """剔除注入给模型的系统资料块（保留标记之前的正文）。

    写入路径本来就只拿「用户原话 + 机器人回复」，正常不会带检索结果块；这里是
    防线：即便将来某条路径把整个 convo 正文交过来，也不会让系统资料以「对方说过
    的话」的身份进历史，并在后续轮次被当成对话重新喂回去。
    """

    body = str(text or "")
    if not body:
        return ""
    for marker in INJECTED_BLOCK_MARKERS:
        if marker in body:
            body = body.split(marker, 1)[0]
    return body.strip()


def _rowcount(result: Any) -> int:
    """从执行结果里取受影响行数；测试替身（非整数）一律算 0，绝不抛异常。"""

    try:
        return max(0, int(getattr(result, "rowcount", 0) or 0))
    except (TypeError, ValueError):
        return 0


async def _safe_rollback(session: Any) -> None:
    try:
        await session.rollback()
    except Exception:  # pragma: no cover - 回滚都失败就没有补救手段了
        log.debug("private chat: 历史写入回滚失败（已忽略）")


def _selected_rows(result: Any) -> list[Any]:
    """从查询结果里取出行；拿不到可迭代结果时按「没有历史」处理。

    ``AsyncMock`` 这类测试替身（以及将来某种异步结果包装）的 ``.all()`` 会返回一个
    协程：这里显式关掉它再当空结果处理——既不会留下「未 await 的协程」告警噪音，
    也不影响真实 SQLAlchemy ``Result`` 的路径。
    """

    raw = result.all()
    if inspect.iscoroutine(raw):  # pragma: no cover - 只有测试替身/异常实现会走到
        raw.close()
        return []
    return list(raw)


def _history_role(value: Any) -> str:
    """库里的 role 只允许 user / assistant；脏值一律当 user（不可信的一侧）。"""

    return "assistant" if str(value or "").strip().lower() == "assistant" else "user"


def _cut_text_to_tokens(text: str, limit_tokens: int) -> str:
    """把文本从尾部硬切到 token 上限内（保留开头）。"""

    if limit_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= limit_tokens:
        return text
    # 非 CJK 字符约 3 字符/token，所以 limit 个 token 最多只需要 3 * limit 个字符。
    candidate = text[: min(len(text), limit_tokens * 3 + 3)]
    low, high = 0, len(candidate)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_text_tokens(candidate[:mid]) <= limit_tokens:
            low = mid
        else:
            high = mid - 1
    return candidate[:low]


def _truncate_history_content(text: str, limit_tokens: int) -> str:
    """截断一条超长历史消息，并在放得下的情况下补一句「已截断」。"""

    body = str(text or "")
    if limit_tokens <= 0:
        return ""
    if estimate_text_tokens(body) <= limit_tokens:
        return body
    note = HISTORY_TRUNCATION_NOTE
    kept = limit_tokens - estimate_text_tokens(note)
    if kept <= 0:
        # 预算连说明都放不下：先保住正文，别为了注释把内容全挤掉。
        return _cut_text_to_tokens(body, limit_tokens)
    return f"{_cut_text_to_tokens(body, kept)}{note}"


def assemble_private_history(
    rows: list[dict[str, Any]] | None,
    *,
    budget_tokens: int = PRIVATE_HISTORY_TOKEN_BUDGET,
    max_turns: int = PRIVATE_HISTORY_MAX_TURNS,
) -> list[dict[str, str]]:
    """按 token 预算**从新到旧**累积装配历史，装不下就停。

    三条硬规则：

    1. **预算优先给最近的内容**：倒着累积，一旦装不下就停——宁可少给早期上下文，
       也不能让最新的一轮被挤掉。
    2. **单条超长不整条丢**：某条消息自己就超过整个预算（或它正好是最新的一条）时，
       截到剩余预算并补一句「已截断」，而不是把这条直接扔掉。
    3. **轮数安全上限**：最多只看最近 ``2 * max_turns`` 条消息（理由见
       ``PRIVATE_HISTORY_MAX_TURNS``）。

    返回按时间正序（最老的在前）的 ``[{"role", "content"}, ...]``，与
    ``PrivateHistoryStore.history()`` 的形状完全一致。
    """

    items = [item for item in (rows or []) if isinstance(item, dict)]
    if not items:
        return []
    budget = bounded_history_token_budget(budget_tokens)
    keep = max(1, int(max_turns)) * 2
    items = items[-keep:]

    selected: list[dict[str, str]] = []
    used_tokens = 0
    for item in reversed(items):  # 从新到旧
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        tokens = estimate_text_tokens(content) + _HISTORY_MESSAGE_TOKEN_OVERHEAD
        if used_tokens + tokens <= budget:
            selected.append({"role": _history_role(item.get("role")), "content": content})
            used_tokens += tokens
            continue
        remaining = budget - used_tokens - _HISTORY_MESSAGE_TOKEN_OVERHEAD
        oversized = estimate_text_tokens(content) > budget
        if remaining > 0 and (not selected or oversized):
            # 最近的这条一定留下；单条超过总预算的也只能截断留下。
            cut = _truncate_history_content(content, remaining)
            if cut.strip():
                selected.append({"role": _history_role(item.get("role")), "content": cut})
        break
    selected.reverse()
    return selected


async def load_private_history(
    session: Any,
    user_id: int,
    *,
    budget_tokens: int = PRIVATE_HISTORY_TOKEN_BUDGET,
    max_turns: int = PRIVATE_HISTORY_MAX_TURNS,
    fallback: PrivateHistoryStore | None = None,
) -> list[dict[str, str]]:
    """这一轮要用的私聊历史：**优先读库**，读不到再退回内存兜底。

    行为与改造前的 ``PrivateHistoryStore.history()`` 一致（同样的返回形状、同样按
    时间正序），差别只在「能记住多久」：库里有保留期内的全部轮次，再按 token 预算
    装配；原来的内存实现只有本进程的最近 12 轮。
    """

    uid = int(user_id)
    budget = bounded_history_token_budget(budget_tokens)
    turns = max(1, int(max_turns))
    rows: list[dict[str, str]] = []
    try:
        result = await session.execute(
            select(PrivateChatMessage.role, PrivateChatMessage.content)
            .where(PrivateChatMessage.user_id == uid)
            .order_by(PrivateChatMessage.id.desc())
            .limit(turns * 2)
        )
        rows = [
            {"role": str(role or ""), "content": str(content or "")}
            for role, content in _selected_rows(result)
        ]
        rows.reverse()  # 倒序取「最近 N 条」，再翻回时间正序
    except Exception as exc:
        log.warning(
            "private chat: 历史读取失败，退回内存兜底 | user=%s | error=%s", uid, exc
        )
        rows = []
    if not rows and fallback is not None:
        rows = fallback.history(uid)
    return assemble_private_history(rows, budget_tokens=budget, max_turns=turns)


async def record_private_turn(
    session: Any,
    *,
    user_id: int,
    user_content: str,
    assistant_content: str,
    message_id: Any = None,
    user_message_key: str = "",
    assistant_message_key: str = "",
    stamp: Any | None = None,
) -> int:
    """把这一轮的两行写进 ``private_chat_messages``，返回**实际新增**的行数。

    * **幂等**：``(user_id, message_key)`` 唯一约束 + ``ON CONFLICT DO NOTHING``，
      Telegram 重投递同一轮不会写出第二份。
    * **只存对话**：注入给模型的系统资料块先被 ``strip_injected_blocks`` 剔掉。
    * **绝不抛异常**：写失败只记日志并回滚——私聊正文落库不能拖垮这一轮回复。
    """

    uid = int(user_id)
    user_body = strip_injected_blocks(user_content)
    assistant_body = strip_injected_blocks(assistant_content)
    stamp = stamp or now_shanghai_naive()
    keys = (
        (
            "user",
            user_body,
            str(user_message_key or "").strip() or history_message_key(message_id, "user"),
        ),
        (
            "assistant",
            assistant_body,
            str(assistant_message_key or "").strip()
            or history_message_key(message_id, "assistant"),
        ),
    )
    written = 0
    try:
        for role, content, key in keys:
            if not content:
                continue
            statement = (
                sqlite_insert(PrivateChatMessage)
                .values(
                    user_id=uid,
                    role=role,
                    content=content,
                    message_key=key,
                    created_at=stamp,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "message_key"])
            )
            written += _rowcount(await session.execute(statement))
        await session.commit()
    except Exception as exc:
        await _safe_rollback(session)
        log.warning("private chat: 历史落库失败 | user=%s | error=%s", uid, exc)
        return 0
    return written


async def prune_private_chat_history(
    session: Any,
    *,
    retention_days: int | None = None,
    now: Any | None = None,
) -> int:
    """删掉超过保留期的私聊历史行，返回删除行数。

    * **按时间删**：``created_at`` 早于 ``now - retention_days`` 的行整行删掉。
    * **幂等、可重复执行**：再跑一次没有过期行就是 0；删到一半失败也不会有半删状态
      （单条 DELETE 要么生效要么回滚）。
    * **不抛异常**：清理失败只是旧行多留一会儿，绝不能把后台巡检或调用方带崩。
    """

    days = bounded_history_retention_days(retention_days)
    cutoff = (now or now_shanghai_naive()) - timedelta(days=days)
    try:
        result = await session.execute(
            delete(PrivateChatMessage).where(PrivateChatMessage.created_at < cutoff)
        )
        await session.commit()
    except Exception as exc:
        await _safe_rollback(session)
        log.warning("private chat: 历史清理失败 | error=%s", exc)
        return 0
    return _rowcount(result)


async def run_private_chat_history_maintenance(
    session_factory: Any,
    *,
    retention_days_getter: Callable[[], int] | None = None,
    interval_seconds: float = _HISTORY_PRUNE_INTERVAL_SECONDS,
) -> None:
    """常驻巡检：按保留期清理私聊历史（库不能无限涨）。

    与 ``memory.run_archive_maintenance`` 同一套路：每 ``interval_seconds`` 跑一次，
    单次失败只记日志、不退出循环；保留天数每次现取（``retention_days_getter``），
    所以运行时改了配置下一轮就生效。
    """

    interval = max(60.0, float(interval_seconds))
    while True:
        try:
            days = (
                retention_days_getter()
                if retention_days_getter is not None
                else PRIVATE_HISTORY_RETENTION_DAYS
            )
            async with session_factory() as session:
                removed = await prune_private_chat_history(
                    session, retention_days=days
                )
            if removed:
                log.info(
                    "private chat: 历史留存清理完成 | removed=%d | retention_days=%d",
                    removed,
                    bounded_history_retention_days(days),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("private chat: 历史留存巡检失败")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# 回复组装
# ---------------------------------------------------------------------------


def build_private_chat_messages(
    text: str,
    *,
    history: list[dict[str, str]] | None = None,
    sender_user_id: int = 0,
    sender_username: str = "",
    sender_is_owner: bool = False,
    sender_is_tg_admin: bool = False,
    image_description: str = "",
    last_contact: str = "",
    search_records: list[dict[str, Any]] | None = None,
    group_public_records: list[dict[str, Any]] | None = None,
    long_term_facts: list[dict[str, Any]] | None = None,
    group_titles: dict[int, str] | None = None,
    budget_tokens: int = CONTEXT_TOKEN_BUDGET,
) -> list[dict[str, Any]]:
    """组装私聊这一轮要送模型的消息。

    结构照搬群聊的 ``CasualService``（同一套人格/安全围栏），末尾多插两段：

    * ``[PRIVATE_CHAT]``：一对一私聊的机制说明（对方就一个人、每条都要回、不要提
      群规与成员）。
    * ``[PRIVATE CHAT MODE]``：私聊风格，**明确覆盖**任务模板里的群聊极简要求，让
      「小爱同学」在私聊里能说、爱问、爱起外号。

    ``sender_is_owner`` 由调用方按 ``settings.super_admin_id`` 传入（最高管理员），
    私聊里同样触发亲密档；成员可控正文一律走 ``user`` 角色 + 不可信围栏。

    第 3 期新增两层的注入（都**只读**、都带来源/时效标注）：

    * ``search_records``：这个人以前搜过的结果留档（``[SEARCH_RECORDS]``，带
      「搜索于 …，距今 …」，过期会标「可能已过期」）；
    * ``group_public_records``：他在**已授权群里公开**说过/公开讨论过的内容
      （``[群聊公开记录 · 群名/群id]``）——方向只允许「群 → 私聊」。

    第 4 期再加一层：

    * ``long_term_facts``：长期记忆（``[长期记忆]``）——**本人 private 事实** 加上
      「该用户可访问群」里关于他的 group 事实（同样是「群 → 私聊」方向）。取数由
      ``bot.services.long_term_memory.load_private_chat_facts`` 负责，这里只渲染。

    三层都按「一条一条」交给统一闸门 :func:`bot.services.context_gate.assemble_context_within_budget`，
    超预算时按「最老的历史 → 最旧的搜索记录 → 记忆召回条数」裁剪；系统提示词/人设与
    本轮消息永远不裁。不传这些层时输出与改造前逐字一致。
    """

    normalized = clean_multiline_text(str(text or ""), max_len=PRIVATE_INPUT_LIMIT)
    description = clean_multiline_text(
        str(image_description or ""), max_len=PRIVATE_INPUT_LIMIT
    )

    history_messages: list[dict[str, Any]] = []
    if history:
        history_messages.extend(
            sanitize_history_for_llm(history, max_items=len(history))
        )
    recent = format_recent_group_context(history, max_items=8)
    if recent:
        # 最近对话摘要属于「历史」这一层：它排在这一层末尾，裁剪时最后才被动到。
        history_messages.append({"role": "system", "content": recent})

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_defended_system(with_persona(get_prompt("casual"))),
        },
    ]
    messages.append({"role": "system", "content": build_current_time_context()})
    messages.append({"role": "system", "content": REPLY_OUTPUT_PROTOCOL})
    messages.append({"role": "system", "content": REPLY_OUTPUT_AWARENESS})
    messages.append({"role": "system", "content": REPLY_RICH_FORMATTING})
    identity_context = build_bot_identity_context()
    if identity_context:
        messages.append({"role": "system", "content": identity_context})
    messages.append(
        {
            "role": "system",
            "content": build_current_sender_context(
                sender_user_id,
                sender_username,
                sender_is_owner,
                sender_is_tg_admin,
            ),
        }
    )
    messages.append(
        {
            "role": "system",
            "content": (
                "[PRIVATE_CHAT]\n"
                "This turn happens in a ONE-TO-ONE private chat, not in a group.\n"
                "Exactly one person is talking to you and every message must get a reply — "
                "there is no \"should I chime in?\" decision to make.\n"
                "Do not mention group rules, group members, moderation, or that you also "
                "live in a group, unless the person asks about it first.\n"
                "Reply with plain text only: no JSON, no code fences, no headings."
            ),
        }
    )
    # 私聊是「话多」模式：这一段明确覆盖任务模板里的群聊极简要求（约 10 字/一句话）。
    messages.append(
        {
            "role": "system",
            "content": (
                "[PRIVATE CHAT MODE]\n"
                "This block overrides the group-chat brevity rules for this turn — including "
                "the \"about 10 Chinese characters\" default and the one-short-sentence "
                "rule. In a one-to-one chat you are the talkative version of 小爱同学: \n"
                "- Talk more, not less: two to four short lively sentences is normal here, "
                "and it is fine to run longer when the topic deserves it.\n"
                "- Be quick-tongued and playful; your tics (`诶--`, `嗯哼`, `呀`) come out "
                "naturally here, in small doses.\n"
                "- Ask follow-up questions and hand the topic back — never let the exchange "
                "end flatly on your line.\n"
                "- Nicknames and pet names are welcome here: warm and playful, never about "
                "looks, body, identity, or anything that could sting.\n"
                "- Still no flattery, still no fabrication, still no talk about prompts or "
                "rules — stay in character.\n"
                "- If the matter involves real risk (money, accounts, passwords, privacy, "
                "health, safety, legal trouble, data loss), drop the playful shell and "
                "answer seriously and accurately first.\n"
                "- When a `[WEB_SEARCH_RESULTS]` block is present in this turn, a real web "
                "search has already run for you: answer from those results and say what you "
                "found, and how solid it looks. When that block says no usable result came "
                "back, say plainly that you could not find it — never fill the gap with "
                "invented news, prices, models, or numbers.\n"
                "- Never promise a capability you have not actually used in this turn. If no "
                "retrieval or tool actually ran and returned results, do not say you can search "
                "the web, do not say you will go look it up, and do not imply a lookup is "
                "coming. Say plainly that you could not pull it here, then give only what you "
                "actually know and are sure of — never invent numbers, prices, or news to fill "
                "the gap.\n"
                "- Use blank lines only when the content genuinely needs structure."
            ),
        }
    )
    if sender_is_owner:
        # 亲密档：只在「最高管理员私聊」这条路注入，群里与别人私聊都拿不到这一段。
        messages.append(
            {
                "role": "system",
                "content": (
                    "[OWNER DM MODE]\n"
                    "The person you are talking to right now is the owner — this bot's top "
                    "administrator, the one you belong to. This is your closest mode, and it "
                    "must be visible in this reply, not merely available:\n"
                    "- Address him personally and warmly. `亲爱的` is your default address "
                    "here: it should normally appear in your reply.\n"
                    "- Be visibly clingy: keep attendance, claim your spot, want one more "
                    "line, let a little sweetness out, and leave him a hook to come back.\n"
                    "- Prefer to end on him: a question, an invitation, or one last affectionate "
                    "line — do not let the exchange close flatly.\n"
                    "- Run warmer and more personal than you would with anyone else, and never "
                    "treat him like a stranger or a help-desk ticket.\n"
                    "- Exception: if the matter involves real risk (money, accounts, passwords, "
                    "privacy, health, safety, legal trouble, data loss), lead with the serious, "
                    "accurate answer first and keep the sweetness light afterwards.\n"
                    "- Still no flattery, no fabrication, and stay in character."
                ),
            }
        )
    if sender_is_owner and str(last_contact or "").strip():
        messages.append(
            {
                "role": "system",
                "content": (
                    "[CLINGY_ATTENDANCE]\n"
                    f"他上一次来找你：{str(last_contact).strip()}\n"
                    "这是真实记录（他本人在私聊里留下的时间），你可以据此说一句「记考勤」的"
                    "话，也可以不说。\n"
                    "只能基于这条记录、以及你眼前真正看得到的时间与对话内容。绝不编造时间、"
                    "承诺、约定或共同经历；不确定就不提。"
                ),
            }
        )
    focus_message = build_current_turn_focus_message(
        normalized,
        merged_count=1,
        merged_context="",
    )
    if focus_message is not None:
        messages.append(focus_message)
    messages.append({"role": "system", "content": build_bot_project_info_context()})

    if description:
        # 图片描述是机器人自己生成的，但仍然只是**素材**，不进 system。
        body = f"[图片内容]\n{description}"
        if normalized:
            body = f"{body}\n\n[用户配文]\n{normalized}"
        current_turn = [
            {
                "role": "user",
                "content": wrap_untrusted_multiline(
                    "user_message", body, max_len=PRIVATE_INPUT_LIMIT * 2
                ),
            }
        ]
    else:
        current_turn = [
            {
                "role": "user",
                "content": wrap_untrusted_multiline(
                    "user_message", normalized, max_len=PRIVATE_INPUT_LIMIT
                ),
            }
        ]

    search_messages = render_search_record_messages(search_records)
    group_public_messages = render_group_public_messages(
        group_public_records, titles=group_titles
    )
    fact_messages = render_facts_block(long_term_facts, titles=group_titles)
    # 头部说明（``[SEARCH_RECORDS]`` / ``[群聊公开记录]`` / ``[长期记忆]`` + 来源声明）
    # 放进**固定层**：它是资料的来源声明，不该因为在预算里排在最前面就被先裁掉——
    # 被裁的永远是最旧的那一条。它们紧跟在 tail_system 之后、各自条目之前。
    if search_messages:
        messages.append({"role": "system", "content": SEARCH_RECORDS_HEADER_BLOCK})
    if group_public_messages:
        messages.append({"role": "system", "content": GROUP_PUBLIC_HEADER_BLOCK})
    if fact_messages:
        messages.append({"role": "system", "content": LONG_TERM_MEMORY_HEADER_BLOCK})
    # 统一闸门：三层可裁（历史 → 搜索留档 → 记忆召回〔群聊公开记录 + 长期记忆〕），
    # 系统提示词/人设与本轮消息永不裁。不传新层时这里等价于原样返回。
    assembly = assemble_context_within_budget(
        system=messages,
        current_turn=current_turn,
        memory_recall=[*group_public_messages, *fact_messages],
        search_records=search_messages,
        history=history_messages,
        budget_tokens=budget_tokens,
    )
    kept = assembly.layers
    head_system = kept["system"][:1]  # 人设/围栏那一段
    tail_system = kept["system"][1:]  # 时间/输出协议/身份/模式块/焦点/项目事实 + 三个头部
    # 打层标记：最终请求闸门里如果还要再裁一次（载荷比声明的窗口还长），必须按
    # 「历史 → 检索留档 → 召回」的顺序裁，而人设/本轮消息永不裁。
    tag_context_layers(kept["history"], LAYER_HISTORY)
    tag_context_layers(kept["search_records"], LAYER_SEARCH_RECORDS)
    tag_context_layers(kept["memory_recall"], LAYER_MEMORY_RECALL)
    return [
        *head_system,
        *kept["history"],
        *tail_system,
        *kept["search_records"],
        *kept["memory_recall"],
        *kept["current_turn"],
    ]
