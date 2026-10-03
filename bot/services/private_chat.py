"""1 对 1 私聊（DM）：准入判定 + 用量配额 + 回复组装 + 提示文案。

用户口径（2026-10-03 拍板）：

1. **准入**：只有「最高管理员授权开通 Smart_Bot 的群组」里的成员能用私聊。
   非成员只回一条固定引导语，**不进模型、不花钱**。最高管理员始终豁免。
2. **配额**：每人 20 条/天 + 全局 200 条/天（先跑一周看用量再调）。
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

#: 每人每天的私聊配额
DEFAULT_PER_USER_DAILY_LIMIT = 20
#: 每天所有私聊合计的配额（真正的成本闸门）
DEFAULT_GLOBAL_DAILY_LIMIT = 200

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
    """``user_id -> 是否授权群成员`` 的 TTL 缓存（纯内存，进程重启即空）。"""

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
        self._entries: dict[int, tuple[bool, float]] = {}

    def get(self, user_id: int) -> bool | None:
        entry = self._entries.get(int(user_id))
        if entry is None:
            return None
        allowed, expires_at = entry
        if expires_at <= self._clock():
            self._entries.pop(int(user_id), None)
            return None
        return allowed

    def put(self, user_id: int, allowed: bool) -> None:
        if len(self._entries) >= self.max_users and int(user_id) not in self._entries:
            # 容量满了先清最老的一批：准入结果过期即失效，清掉只会多打一次 API
            self._entries.clear()
        self._entries[int(user_id)] = (bool(allowed), self._clock() + self.ttl_seconds)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_member_cache = MemberAccessCache()


def member_cache() -> MemberAccessCache:
    """模块级共享缓存（测试里可以换成自己的实例）。"""

    return _member_cache


def _is_member_status(member: Any) -> bool:
    """``ChatMember`` → 是否算「在群里」。

    ``restricted`` 要看 ``is_member``：被禁言但还在群 = 成员；被踢但状态还没
    刷新成 ``kicked`` 的旧数据不算。
    """

    status = str(getattr(member, "status", "") or "").lower()
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def confirm_authorized_group_member(
    bot: Any,
    session: Any,
    settings: Any,
    user_id: int,
    *,
    cache: MemberAccessCache | None = None,
) -> bool | None:
    """判定「这个私聊用户是不是某个已授权群的成员」。

    返回 ``True`` / ``False`` / ``None``：``None`` 表示**无法确认**（Telegram
    侧查询异常），调用方应回一条「稍后再试」，既不进模型也不计配额——按不确定
    就放行会变成免费代理，按不确定就拒绝会误伤真人，所以单独给一条退路。
    """

    uid = int(user_id)
    if is_super_admin_user_id(uid, settings):
        return True

    store = cache if cache is not None else _member_cache
    hit = store.get(uid)
    if hit is not None:
        return hit

    try:
        groups = await list_authorized_groups(session)
    except Exception as exc:  # 数据库异常同样视为「无法确认」
        log.warning("private chat: 授权群查询失败 | user=%s | error=%s", uid, exc)
        return None

    unknown = False
    for row in groups:
        try:
            member = await bot.get_chat_member(chat_id=int(row.group_id), user_id=uid)
        except Exception as exc:
            log.warning(
                "private chat: getChatMember 失败 | group=%s user=%s | error=%s",
                row.group_id,
                uid,
                exc,
            )
            unknown = True
            continue
        if _is_member_status(member):
            store.put(uid, True)
            return True

    if unknown:
        # 有群没查成 → 不下结论，也不缓存（下一次重试）
        return None

    store.put(uid, False)
    return False


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


def local_day_key(now: Any | None = None) -> str:
    """本地自然日键（``YYYY-MM-DD``，与签到表同口径）。"""

    if now is None:
        day = local_today()
    else:
        day = local_today(now)
    return str(day)


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


async def consume_daily_quota(
    session: Any,
    *,
    user_id: int,
    per_user_limit: int = DEFAULT_PER_USER_DAILY_LIMIT,
    global_limit: int = DEFAULT_GLOBAL_DAILY_LIMIT,
    day: str | None = None,
    stamp: Any | None = None,
) -> QuotaOutcome:
    """先扣再用：返回是否放行。

    两道闸门在同一次事务里扣：任何一道超限就整体回滚（这轮不吃配额）。所以
    「先扣了但没回」不会留下脏计数，也不会出现「用户 A 花掉了全局额度的一半」。
    """

    uid = int(user_id)
    stamp = stamp or now_shanghai_naive()
    key = str(day or local_day_key(stamp))

    user_used = await _bump(session, user_id=uid, day=key, stamp=stamp)
    global_used = await _bump(
        session, user_id=GLOBAL_COUNTER_USER_ID, day=key, stamp=stamp
    )

    if user_used > int(per_user_limit):
        await session.rollback()
        return QuotaOutcome(
            allowed=False,
            reason="user_limit",
            user_used=user_used - 1,
            per_user_limit=int(per_user_limit),
            global_used=global_used - 1,
            global_limit=int(global_limit),
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
        )

    await session.commit()
    return QuotaOutcome(
        allowed=True,
        reason="ok",
        user_used=user_used,
        per_user_limit=int(per_user_limit),
        global_used=global_used,
        global_limit=int(global_limit),
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
) -> list[dict[str, Any]]:
    """组装私聊这一轮要送模型的消息。

    结构照搬群聊的 ``CasualService``（同一套人格/安全围栏），只在**末尾多插一段
    ``[PRIVATE_CHAT]``**：告诉模型这是一对一私聊、对方就一个人、每条都要回、
    不要提群里的规则与成员。成员可控正文一律走 ``user`` 角色 + 不可信围栏。
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
                "Answer what they actually asked, in a natural, concise, friendly tone.\n"
                "Do not mention group rules, group members, moderation, or that you also "
                "live in a group, unless the person asks about it first.\n"
                "Reply with plain text only: no JSON, no code fences, no headings."
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
