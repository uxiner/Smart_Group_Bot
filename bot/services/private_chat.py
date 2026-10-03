"""1 对 1 私聊（DM）：准入判定 + 用量配额 + 回复组装 + 提示文案。

用户口径（2026-10-03 拍板）：

1. **准入**：只有「最高管理员授权开通 Smart_Bot 的群组」里的成员能用私聊。
   非成员只回一条固定引导语，**不进模型、不花钱**。最高管理员始终豁免。
2. **配额（阶梯）**：普通成员 100 条/天、群管理员 500 条/天；两组各有自己的全局
   上限（20000 / 100000 条/天，互不占用）；最高管理员不设限、不计数。
3. **内容**：私聊不做群规审核，NSFW 也放开——群里那条「任何群都不允许发
   NSFW 图/视频」的底线**只针对群聊通道**，这里一个字都没动。
4. **不落库**：私聊正文与图片内容不进群归档/记忆/向量；只记「条数」用于配额。

设计取舍：

* 私聊**必回**，所以这条路径不经过 ``decision`` 阶段（省一次调用），模型走
  ``main`` 阶段（``LLMService.chat`` 的默认标签）。
* 准入结果按 TTL 缓存，避免每条消息都打一次 ``getChatMember``。
* 配额落在 ``private_chat_usage`` 表（进程重启不清零），全局行用 ``user_id=0``。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.db.models import PrivateChatUsage
from bot.services.authz import is_super_admin_user_id, list_authorized_groups
from bot.services.checkin import local_today
from bot.services.reply_output import (
    REPLY_OUTPUT_AWARENESS,
    REPLY_OUTPUT_PROTOCOL,
    REPLY_RICH_FORMATTING,
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

#: 私聊历史的保留轮数（只在内存里，进程重启即空）
HISTORY_MAX_TURNS = 12
#: 私聊正文的长度上限（与群聊口径一致）
PRIVATE_INPUT_LIMIT = 1000

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
        self._entries: dict[int, tuple[str, float]] = {}

    def get(self, user_id: int) -> str | None:
        entry = self._entries.get(int(user_id))
        if entry is None:
            return None
        tier, expires_at = entry
        if expires_at <= self._clock():
            self._entries.pop(int(user_id), None)
            return None
        return tier

    def put(self, user_id: int, tier: str) -> None:
        if len(self._entries) >= self.max_users and int(user_id) not in self._entries:
            # 容量满了先清最老的一批：准入结果过期即失效，清掉只会多打一次 API
            self._entries.clear()
        self._entries[int(user_id)] = (str(tier), self._clock() + self.ttl_seconds)

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
    """一次私聊准入判定的结果：放行与否 + 档位（档位决定配额阶梯）。"""

    allowed: bool | None  # None = 无法确认
    tier: str
    detail: str = ""

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
    """

    uid = int(user_id)
    if is_super_admin_user_id(uid, settings):
        return AccessVerdict(True, TIER_SUPER)

    store = cache if cache is not None else _member_cache
    hit = store.get(uid)
    if hit is not None:
        if hit == TIER_NONE:
            return AccessVerdict(False, TIER_NONE)
        return AccessVerdict(True, hit)

    try:
        groups = await list_authorized_groups(session)
    except Exception as exc:  # 数据库异常同样视为「无法确认」
        log.warning("private chat: 授权群查询失败 | user=%s | error=%s", uid, exc)
        return AccessVerdict(None, TIER_UNKNOWN, "authorized group lookup failed")

    unknown = False
    best = TIER_NONE
    for row in groups:
        try:
            member = await bot.get_chat_member(chat_id=int(row.group_id), user_id=uid)
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
        if tier == TIER_ADMIN:
            store.put(uid, TIER_ADMIN)
            return AccessVerdict(True, TIER_ADMIN)
        if tier == TIER_MEMBER:
            best = TIER_MEMBER

    if best == TIER_MEMBER:
        # 还有群没查成时先不缓存：下次重查有可能升档（普通成员 → 管理员）
        if not unknown:
            store.put(uid, TIER_MEMBER)
        return AccessVerdict(True, TIER_MEMBER)

    if unknown:
        # 有群没查成 → 不下结论，也不缓存（下一次重试）
        return AccessVerdict(None, TIER_UNKNOWN, "telegram lookup failed")

    store.put(uid, TIER_NONE)
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
# 私聊历史（只在内存，不落库）
# ---------------------------------------------------------------------------


class PrivateHistoryStore:
    """每个用户保留最近 ``HISTORY_MAX_TURNS`` 轮的对话文本。

    刻意不落库：私聊内容不进归档/记忆/向量（用户口径 4）。进程重启历史清空，
    代价是重启后的第一条可能缺上下文，可以接受。
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
) -> list[dict[str, Any]]:
    """组装私聊这一轮要送模型的消息。

    结构照搬群聊的 ``CasualService``（同一套人格/安全围栏），末尾多插两段：

    * ``[PRIVATE_CHAT]``：一对一私聊的机制说明（对方就一个人、每条都要回、不要提
      群规与成员）。
    * ``[PRIVATE CHAT MODE]``：私聊风格，**明确覆盖**任务模板里的群聊极简要求，让
      「小爱同学」在私聊里能说、爱问、爱起外号。

    ``sender_is_owner`` 由调用方按 ``settings.super_admin_id`` 传入（最高管理员），
    私聊里同样触发亲密档；成员可控正文一律走 ``user`` 角色 + 不可信围栏。
    """

    normalized = clean_multiline_text(str(text or ""), max_len=PRIVATE_INPUT_LIMIT)
    description = clean_multiline_text(
        str(image_description or ""), max_len=PRIVATE_INPUT_LIMIT
    )

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_defended_system(with_persona(get_prompt("casual"))),
        },
    ]
    if history:
        messages.extend(sanitize_history_for_llm(history, max_items=len(history)))
    recent = format_recent_group_context(history, max_items=8)
    if recent:
        messages.append({"role": "system", "content": recent})
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
                "- Use blank lines only when the content genuinely needs structure."
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
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted_multiline(
                    "user_message", body, max_len=PRIVATE_INPUT_LIMIT * 2
                ),
            }
        )
    else:
        messages.append(
            {
                "role": "user",
                "content": wrap_untrusted_multiline(
                    "user_message", normalized, max_len=PRIVATE_INPUT_LIMIT
                ),
            }
        )
    return messages
