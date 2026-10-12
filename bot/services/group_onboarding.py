"""新群接入申请：申请 / 审批 / 撤回的状态机（**不含** HTTP 与 Telegram 调用）。

这个模块只做三件事，并且把它们和「实际授权」分开：

1. **申请**：把一条 pending 写进 ``group_onboarding_requests``。它对
   ``AuthorizedGroup`` 不可见 —— 提交申请**不等于**开通。
2. **审批**：approve 在**调用方给的同一个事务**里同时完成状态 CAS 与
   ``authorize_group`` + ``authorize_group_admin``。外部核验（Telegram 实时身份）
   由调用方在**事务之外**先做完，所以这里永远不持写事务等网络。
3. **查询**：申请人只看自己的，超管看全部；分页有界。

设计取舍：

* **单群仅一条有效 pending 用数据库唯一约束兜住**（``ux_group_onboarding_open_request``
  部分唯一索引），不是 ``SELECT`` → ``INSERT`` 的裸竞态：并发提交必然有一条撞
  约束，我们把它当成幂等信号，返回已存在的那条。
* **授权授予失败必须把状态一起回滚**。所以 approve 的一切副作用都挂在调用方
  的事务上，本模块自己不 ``commit``——它没有资格决定"半个状态"落库。
* **终态不可回退**：rejected / cancelled 批准不了；已批准的重复 approve 幂等
  返回同���结果，不重复授予、不报错（超管手滑点两次是常态）。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Admin, AuthorizedGroup, GroupOnboardingRequest
from bot.services.authz import authorize_group, authorize_group_admin
from bot.services.strict_values import (
    StrictValueError,
    strict_int,
    strict_telegram_id,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
)
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED}
)

#: 申请人可选的功能目录。**只描述"想要什么"**，不代表已经开通。
FEATURE_MODERATION = "moderation"
FEATURE_CASUAL_CHAT = "casual_chat"
FEATURE_TOOLS = "tools"
FEATURE_VISION = "vision"
FEATURE_SUMMARY = "summary"
FEATURE_PROACTIVE = "proactive"
FEATURE_TTS = "tts"
FEATURE_JOIN_VERIFICATION = "join_verification"

KNOWN_FEATURES: tuple[str, ...] = (
    FEATURE_MODERATION,
    FEATURE_CASUAL_CHAT,
    FEATURE_TOOLS,
    FEATURE_VISION,
    FEATURE_SUMMARY,
    FEATURE_PROACTIVE,
    FEATURE_TTS,
    FEATURE_JOIN_VERIFICATION,
)
MAX_FEATURES = 12

PRESET_MODERATION_ID = "moderation_only"
PRESET_COMPANION_ID = "companion"
PRESET_COMMUNITY_ID = "community_ops"
PRESET_IDS: tuple[str, ...] = (
    PRESET_MODERATION_ID,
    PRESET_COMPANION_ID,
    PRESET_COMMUNITY_ID,
)

MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 20
MAX_REASON_LENGTH = 500
_REQUEST_ID_BYTES = 16

#: 每人每分钟的「检查 + 提交」次数。**失败调用同样计次**：一次失败请求背后是
#: 一次真实的 Telegram 调用，任意群 id 的无效尝试也必须付这个代价，否则
#: 刷 group_id 就能把 Telegram 与数据库当免费 API 用。
RATE_LIMIT_MAX_CALLS = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_TRACKED = 4096

#: Telegram 侧读的超时。拿不到就是「不可核验」，不是「放行」。
TELEGRAM_LOOKUP_TIMEOUT_SECONDS = 5.0

#: 每个期望功能**真正需要**的 bot 管理权限。
#:
#: 这里刻意只列功能自身需要的权限：不要求 ``can_invite_users`` 之类的无关权限
#: （bot 加群不靠邀请成员），也不要求所有功能共用的全集 —— 否则一个只需要删消息
#: 做审核的群，会因为没有"邀请用户"权限被判定成无法工作。
FEATURE_REQUIRED_BOT_PERMISSIONS: dict[str, tuple[str, ...]] = {
    FEATURE_MODERATION: ("can_delete_messages",),
    FEATURE_JOIN_VERIFICATION: ("can_restrict_members",),
    FEATURE_CASUAL_CHAT: (),
    FEATURE_TOOLS: (),
    FEATURE_VISION: (),
    FEATURE_SUMMARY: (),
    FEATURE_PROACTIVE: (),
    FEATURE_TTS: (),
}

#: 可读可不读的权限字段（``ChatMemberAdministrator`` 上不是每个版本都有）。
_BOT_PERMISSION_FIELDS = (
    "can_delete_messages",
    "can_restrict_members",
    "can_pin_messages",
    "can_invite_users",
    "can_manage_chat",
)


class OnboardingError(RuntimeError):
    """接入申请的基类错误。"""


class OnboardingInvalid(OnboardingError):
    """入参不合法（群 id / 功能 / 预设 / 分页）。"""

    status_code = 400
    code = "invalid_onboarding_request"
    message = "接入申请参数无效。"


class OnboardingNotFound(OnboardingError):
    """申请不存在。"""

    status_code = 404
    code = "request_not_found"
    message = "接入申请不存在。"


class OnboardingForbidden(OnboardingError):
    """操作者对该申请没有权限。"""

    status_code = 403
    code = "onboarding_forbidden"
    message = "你没有该操作的操作权限。"


class OnboardingConflict(OnboardingError):
    """状态机冲突（终态再翻转、并发被别人处理）。"""

    status_code = 409
    code = "request_state_conflict"
    message = "接入申请状态已变化，请刷新后重试。"


class OnboardingPendingConflict(OnboardingError):
    """该群已有一条**别人**提交的待审申请。

    刻意不带任何来自那条申请的字段（request_id / 申请人 / 期望功能 / 预设 /
    理由一个都不给）：群管理员身份只说明他能在群里管事，不说明他有权读
    另一个人提交的接入申请，而 request_id 还是能直接拿去 cancel 的句柄。
    提示语也必须与"有没有别人申请过"无关 —— 否则它本身就是一条侧信道。
    """

    status_code = 409
    code = "already_pending"
    message = "该群已有一条待审申请，请等待最高管理员审理。"


class OnboardingRateLimited(OnboardingError):
    """该用户在窗口内的「检查 + 提交」次数用尽。"""

    status_code = 429
    code = "rate_limited"
    message = "操作过于频繁，请稍后再试。"


class UnsupportedChatType(OnboardingError):
    """不是群组 / 超级群（私聊、频道都不接受）。"""

    status_code = 400
    code = "unsupported_chat_type"
    message = "只支持群组或超级群，不支持私聊或频道。"


class BotNotInGroup(OnboardingError):
    """bot 不在这个群里（还没拉进来，或已被踢）。"""

    status_code = 400
    code = "bot_not_in_group"
    message = "机器人还不在这个群里，请先把机器人拉进群。"


class NotGroupAdministrator(OnboardingError):
    """Telegram 权威名单里没有这个人。"""

    status_code = 403
    code = "not_group_administrator"
    message = "你不是该群的 Telegram 管理员。"


class TelegramUnverifiable(OnboardingError):
    """Telegram 侧**问不成**：超时 / 限流 / 异常。

    单独成类而不是复用 Forbidden：这一态必须**拒绝**，且拒绝理由要能让人分清
    「你不是管理员」和「我们没问成」—— 前者永远不该重试，后者该重试。
    """

    status_code = 503
    code = "telegram_unverifiable"
    message = "暂时无法向 Telegram 核实，请稍后重试。"


class TelegramGroupMissing(OnboardingError):
    """Telegram 权威地回答「没有这个群 / bot 不在其中」。"""

    status_code = 404
    code = "group_not_found"
    message = "Telegram 上没有这个群，或机器人不在其中。"


@dataclass(frozen=True, slots=True)
class TelegramGroupFacts:
    """一次 Telegram 权威核验的结果（成功或失败都显式表达）。"""

    group_id: int
    #: False = 这次没问成。任何放权判定都必须先看这个。
    verified: bool
    exists: bool
    chat_type: str
    bot_present: bool
    bot_status: str
    bot_permissions: Mapping[str, bool]

    @property
    def is_group_chat(self) -> bool:
        return self.chat_type in {"group", "supergroup"}

    @property
    def bot_is_administrator(self) -> bool:
        return self.bot_status in {"creator", "administrator"}

    def missing_permissions(self, features: Any = ()) -> list[str]:
        """这些功能真正缺哪些 bot 管理权限（没有就是空列表）。"""

        wanted: list[str] = []
        for feature in normalize_features(features):
            for name in FEATURE_REQUIRED_BOT_PERMISSIONS.get(feature, ()):
                if name not in wanted:
                    wanted.append(name)
        return [name for name in wanted if not bool(self.bot_permissions.get(name))]


@dataclass(frozen=True, slots=True)
class ApproveOutcome:
    """approve 的结果（供 API 层写响应 / 记审计）。

    ``group_authorized`` / ``applicant_granted`` 的语义是「**这一次调用**是否
    授予了授权」。已经批准过的申请重放时它们恒为 ``False``：那条历史决定不再
    产生任何写入。

    「现在到底还在不在授权」由 ``currently_*`` 单独回答 —— 把两者混成一个字段
    正是"撤销被静默顶掉"的根因。
    """

    request_id: str
    group_id: int
    applicant_id: int
    status: str
    already_decided: bool
    group_authorized: bool
    applicant_granted: bool
    currently_authorized: bool = False
    currently_bot_present: bool = False
    currently_applicant_admin: bool = False

    def document(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "group_id": self.group_id,
            "applicant_id": self.applicant_id,
            "status": self.status,
            "already_decided": self.already_decided,
            "group_authorized": self.group_authorized,
            "applicant_granted": self.applicant_granted,
            "current": {
                "authorized": self.currently_authorized,
                "bot_present": self.currently_bot_present,
                "applicant_admin": self.currently_applicant_admin,
            },
        }


def normalize_group_id(value: Any) -> int:
    """把请求里的群 id 解析成一个**真实且未被改写**的群标识。

    只接受**负数**：Telegram 的群组 / 超级群 / 频道 chat id 一律为负，用户 id
    为正；``0`` 既不是合法群 id 也不是"未知"，拿它占位会让未归属的用量 / 申请
    看起来像某个群。

    这里**不**校验 chat 类型（group / supergroup / channel 的区别）：那只能由
    Telegram ``getChat`` 的权威答案给出，本服务在事务外做，不在这里猜。

    解析走 :func:`strict_telegram_id` 而不是 ``int()``：``int(-1001234567890.75)``
    返回 ``-1001234567890``，于是我们拿着一个**并不存在**的群去查库、发
    Telegram、授权 —— 而截断掉的小数谁都看不见，看起来只是一次成功操作。
    """

    try:
        group_id = strict_telegram_id(value, field="group_id")
    except StrictValueError as exc:
        raise OnboardingInvalid("group_id 格式无效。") from exc
    if group_id >= 0:
        raise OnboardingInvalid("group_id 必须是群标识（负数），不能是 0 或用户 id。")
    return group_id


def normalize_features(raw: Any) -> list[str]:
    """校验并去重期望功能列表。未知功能直接拒绝（不静默丢弃）。"""

    if raw is None:
        return []
    if isinstance(raw, str) or not hasattr(raw, "__iter__"):
        raise OnboardingInvalid("desired_features 必须是数组。")
    items = list(raw)
    # 上界作用在**原始长度**上：去重之后再判会让"重复灌 10 万项"变成一条合法的
    # 单元素请求，先白跑一遍遍历。这里要的是"单次请求的工作量有界"。
    if len(items) > MAX_FEATURES:
        raise OnboardingInvalid("功能项过多。")
    features: list[str] = []
    for item in items:
        name = str(item or "").strip().lower()
        if not name:
            continue
        if name not in KNOWN_FEATURES:
            raise OnboardingInvalid(f"未知功能：{name}")
        if name not in features:
            features.append(name)
    return features


def normalize_preset_id(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text not in PRESET_IDS:
        raise OnboardingInvalid(f"未知预设：{text}")
    return text


def normalize_status(raw: Any) -> str | None:
    """状态查询参数的**唯一**归一化入口。

    rows 与 total 必须走同一个函数：曾经 list 做了 strip/lower 而 count 没有，
    于是 ``?status=PENDING`` 会返回 ``rows.length=1, total=0`` —— 一个自相
    矛盾的分页响应会让"下一页"永远翻不到东西。
    """

    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if not normalized:
        return None
    if normalized not in STATUSES:
        raise OnboardingInvalid(f"未知状态：{normalized}")
    return normalized


def normalize_reason(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[:MAX_REASON_LENGTH]


def normalize_page_limit(limit: Any, default: int = DEFAULT_PAGE_LIMIT) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise OnboardingInvalid("limit 格式无效。") from exc
    if value < 1 or value > MAX_PAGE_LIMIT:
        raise OnboardingInvalid(f"limit 必须在 1..{MAX_PAGE_LIMIT} 之间。")
    return value


def new_request_id() -> str:
    return secrets.token_hex(_REQUEST_ID_BYTES)


# ---------------------------------------------------------------------------
# 频率护栏
# ---------------------------------------------------------------------------
_RATE_WINDOWS: dict[int, deque[float]] = {}
#: 每个用户最多保留多少条时间戳。**有界**是这里的第一属性：一个被拒绝的请求
#: 不该在内存里留下任何东西，否则「狂点提交」本身就成了一个内存放大器
#: （十万次拒绝 = 十万个 float + deque 节点）。
#:
#: 代价是：窗口后半段的拒绝不再单独计时。但因为窗口只保留前
#: ``RATE_LIMIT_MAX_CALLS`` 条就已经足够判定「超额」，而任何**被记下的**调用
#: 仍然会让后续请求被拒，所以限额效果不变。
_RATE_WINDOW_MAX_ENTRIES = RATE_LIMIT_MAX_CALLS * 2


def consume_rate_limit(
    user_id: int,
    limit: int | None = RATE_LIMIT_MAX_CALLS,
    now: float | None = None,
) -> int:
    """记一次「检查 / 提交」，返回窗口内剩余次数；用尽抛 :class:`OnboardingRateLimited`。

    滑窗按**时间戳队列**而不是固定计数器：固定窗口的边界会让一个人在一个窗口
    的最后一刻和下一个窗口的第一刻各打满 10 次（实际 20 次 / 边界秒）。

    队列长度有上界，且**先判定后追加**：超限的请求直接抛，一条时间戳都不留。
    """

    try:
        bounded = RATE_LIMIT_MAX_CALLS if limit is None else strict_int(
            limit, field="limit"
        )
    except StrictValueError as exc:
        raise OnboardingInvalid("限次上限无效。") from exc
    if bounded < 1 or bounded > RATE_LIMIT_MAX_CALLS:
        raise OnboardingInvalid(f"限次上限必须在 1..{RATE_LIMIT_MAX_CALLS} 之间。")
    moment = time.monotonic() if now is None else float(now)
    key = strict_int(user_id, field="user_id")
    window = _RATE_WINDOWS.get(key)
    if window is None:
        # 新用户要先有位置。回收必须**先逐个窗口地**做：
        #   1. 每个窗口按时间戳逐条丢掉已过期的；
        #   2. 只删**变空**的桶。
        # 删掉别人**仍在有效期**的窗口等于给所有人开绕过口子，而放任增长
        # 又是一台可以被人为放大的内存机器。
        #
        # 曾经这里是「len<=MAX 早退 / len>=MAX 直接拒新用户」两个条件对撞：
        # 表满且全部过期时，前者让清理根本跑不到，后者把新用户永久拒掉 ——
        # 一个再也走不出来的死路。
        if len(_RATE_WINDOWS) >= RATE_LIMIT_MAX_TRACKED:
            _reclaim_rate_limit_capacity(moment)
        if len(_RATE_WINDOWS) >= RATE_LIMIT_MAX_TRACKED:
            raise OnboardingRateLimited(
                "限次服务繁忙，请稍后再试。"
            )
        window = deque()
        _RATE_WINDOWS[key] = window
    cutoff = moment - RATE_LIMIT_WINDOW_SECONDS
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) >= bounded:
        # 超限：**不追加**。见 ``_RATE_WINDOW_MAX_ENTRIES`` 的说明。
        raise OnboardingRateLimited(OnboardingRateLimited.message)
    window.append(moment)
    _prune_rate_limit_tracking(moment)
    return bounded - len(window)


def _reclaim_rate_limit_capacity(moment: float) -> None:
    """容量紧张时回收：逐窗口**逐条**去掉过期时间戳，只删变空的桶。

    必须逐条而不是"看最老的那条"：一个窗口里可能只剩最老的一条过期，其余
    仍然有效；按头部整体判断会白占容量或撤销别人仍在窗口内的配额。

    刻意**不**删任何仍有效的配额 —— 那是给所有人开绕过口子。
    """

    cutoff = moment - RATE_LIMIT_WINDOW_SECONDS
    for key, window in list(_RATE_WINDOWS.items()):
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            _RATE_WINDOWS.pop(key, None)


def _prune_rate_limit_tracking(moment: float) -> None:
    """常规回收：只删**已经空掉**的窗口，不碰任何有效配额。"""

    for key, window in list(_RATE_WINDOWS.items()):
        if not window:
            _RATE_WINDOWS.pop(key, None)


def reset_rate_limit() -> None:
    """清空限次表（测试与运维用）。"""

    _RATE_WINDOWS.clear()


def rate_limit_snapshot(user_id: int) -> int:
    window = _RATE_WINDOWS.get(int(user_id))
    return len(window) if window else 0


# ---------------------------------------------------------------------------
# Telegram 权威核验（全部只读、全部在事务之外）
# ---------------------------------------------------------------------------
async def _telegram_call(awaitable: Any, *, what: str, group_id: int) -> Any:
    try:
        async with asyncio.timeout(TELEGRAM_LOOKUP_TIMEOUT_SECONDS):
            return await awaitable
    except Exception as exc:
        if _telegram_says_missing(exc):
            raise TelegramGroupMissing(TelegramGroupMissing.message) from exc
        log.info(
            "onboarding telegram lookup unavailable | what=%s group=%s",
            what,
            group_id,
            exc_info=True,
        )
        raise TelegramUnverifiable(
            TelegramUnverifiable.message
        ) from exc


def _telegram_says_missing(exc: BaseException) -> bool:
    """Telegram 是不是**确定地**回答了「没有这个群」。

    与「我们没问成」必须分开：前者是权威结论（404），后者是瞬时故障（503）。
    混成一种会让「bot 不在这个群」被当成网络抖动反复重试，或者反过来。
    """

    if isinstance(exc, asyncio.TimeoutError):
        return False
    name = type(exc).__name__.lower()
    if "chatnotfound" in name or "chatinvite" in name or "chatnotfound" in name:
        return True
    text = str(exc).lower()
    return "chat not found" in text or "chat_not_found" in text


async def _bot_identity(bot_obj: Any, *, group_id: int) -> int | None:
    get_me = getattr(bot_obj, "get_me", None)
    if not callable(get_me):
        return None
    me = await _telegram_call(get_me(), what="get_me", group_id=group_id)
    try:
        return int(getattr(me, "id", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def _permissions_of(member: Any) -> dict[str, bool]:
    return {
        name: bool(getattr(member, name, False))
        for name in _BOT_PERMISSION_FIELDS
    }


async def inspect_group(bot_obj: Any, group_id: int) -> TelegramGroupFacts:
    """向 Telegram 问清「这个群是什么、bot 在不在、bot 有什么权限」。

    问不成就抛 :class:`TelegramUnverifiable` —— 调用方必须据此拒绝，**不得**
    退回本地表当作已核验。这里刻意不缓存：申请与审批都是低频操作，而缓存会让
    「刚被踢出群 / 刚被撤权」在一段时间内继续放行。
    """

    normalized = normalize_group_id(group_id)
    get_chat = getattr(bot_obj, "get_chat", None)
    if not callable(get_chat):
        raise TelegramUnverifiable("当前部署无法向 Telegram 核实群信息。")
    chat = await _telegram_call(
        get_chat(normalized), what="get_chat", group_id=normalized
    )
    chat_type = str(getattr(chat, "type", "") or "").strip().lower()

    bot_id = await _bot_identity(bot_obj, group_id=normalized)
    get_member = getattr(bot_obj, "get_chat_member", None)
    bot_present = False
    bot_status = ""
    permissions: dict[str, bool] = {}
    if bot_id is not None and callable(get_member):
        member = await _telegram_call(
            get_member(normalized, bot_id), what="get_chat_member", group_id=normalized
        )
        bot_status = str(getattr(member, "status", "") or "").strip().lower()
        bot_present = bot_status not in {"left", "kicked", ""}
        permissions = _permissions_of(member)
    return TelegramGroupFacts(
        group_id=normalized,
        verified=True,
        exists=True,
        chat_type=chat_type,
        bot_present=bot_present,
        bot_status=bot_status,
        bot_permissions=permissions,
    )


async def telegram_administrator_status(
    bot_obj: Any,
    group_id: int,
    user_id: int,
) -> bool | None:
    """``True`` = 现在还是群管理员；``False`` = 权威名单里没有；``None`` = 没问成。

    三态而不是二值：把「查不到」和「确认没有」混成同一个答案，会在 Telegram
    抖动时把管理员锁死，也会在权刚发生时继续放权 —— 两个方向都是错的。
    调用方**不得**把 ``None`` 当成 ``True``（放权方向），也不得当成 ``False``
    （把抖动当成撤权）。
    """

    normalized = normalize_group_id(group_id)
    get_admins = getattr(bot_obj, "get_chat_administrators", None)
    if not callable(get_admins):
        return None
    members = await _telegram_call(
        get_admins(normalized), what="get_chat_administrators", group_id=normalized
    )
    for member in members or []:
        member_user = getattr(member, "user", None)
        if member_user is None:
            continue
        try:
            if int(getattr(member_user, "id", 0) or 0) != int(user_id):
                continue
        except (TypeError, ValueError):
            continue
        status = str(getattr(member, "status", "") or "").strip().lower()
        return status in {"creator", "administrator"}
    return False


async def require_live_group_administrator(
    bot_obj: Any,
    group_id: int,
    user_id: int,
) -> TelegramGroupFacts:
    """申请/审批前的**唯一**身份判定。

    一次调用同时确认三件事：这个 id 是真的群、bot 在群里、这个人现在是管理员。
    任何一条不成立都抛异常，**没有**任何一条走"查不到就当没事"。
    """

    facts = await inspect_group(bot_obj, group_id)
    if not facts.is_group_chat:
        raise UnsupportedChatType(UnsupportedChatType.message)
    if not facts.bot_present:
        raise BotNotInGroup(BotNotInGroup.message)
    status = await telegram_administrator_status(bot_obj, group_id, user_id)
    if status is None:
        raise TelegramUnverifiable(TelegramUnverifiable.message)
    if not status:
        raise NotGroupAdministrator(NotGroupAdministrator.message)
    return facts


def request_document(row: GroupOnboardingRequest) -> dict[str, Any]:
    """申请行的对外文档（只含申请人自己与审理人都需要的字段）。"""

    created = row.created_at
    reviewed = row.reviewed_at
    return {
        "request_id": str(row.request_id),
        "group_id": int(row.group_id),
        "applicant_id": int(row.applicant_id),
        "requested_features": list(row.requested_features or []),
        "preset_id": str(row.preset_id) if row.preset_id else None,
        "status": str(row.status or ""),
        "revision": int(row.revision or 1),
        "reason": str(row.reason or ""),
        "created_at": created.isoformat() if isinstance(created, datetime) else "",
        "reviewed_at": reviewed.isoformat() if isinstance(reviewed, datetime) else None,
        "reviewed_by": (
            int(row.reviewed_by) if row.reviewed_by is not None else None
        ),
        "group_authorized": bool(
            getattr(row, "granted_group_authorization", False)
        ),
    }


async def get_request(
    session: AsyncSession,
    request_id: str,
) -> GroupOnboardingRequest:
    row = await find_request(session, request_id)
    if row is None:
        raise OnboardingNotFound("申请不存在。")
    return row


async def find_request(
    session: AsyncSession,
    request_id: str,
) -> GroupOnboardingRequest | None:
    """按**公开标识**取一行。

    刻意不用 ``session.get``：那里的主键是自增 ``id``，用公开标识查必须显式
    走 ``request_id`` 列——否则"拿 URL 里的标识查申请"会静默地退化成"按自增 id
    查"，那正是把 URL 标识换成顺序枚举漏洞的最省事写法。
    """

    stmt = select(GroupOnboardingRequest).where(
        GroupOnboardingRequest.request_id == str(request_id or "")
    )
    return (await session.scalars(stmt)).first()


@dataclass(frozen=True, slots=True)
class OpenRequestView:
    """申请人在某个群上的「当前待审申请」视图。

    ``foreign_pending=True`` 只说「有别人的」，**不带任何字段**：那一条的
    request_id 是能直接拿去 cancel/approve 的句柄，申请人 id 与他填的期望
    功能也不是他有权知道的事。
    """

    own: dict[str, Any] | None
    foreign_pending: bool


async def open_request_view(
    session: AsyncSession,
    group_id: int,
    applicant_id: int,
) -> OpenRequestView:
    """查这个群当前的有效申请，只把**自己的**那条交给调用方。"""

    row = await find_open_request(session, normalize_group_id(group_id))
    if row is None:
        return OpenRequestView(own=None, foreign_pending=False)
    if int(row.applicant_id) == int(applicant_id):
        return OpenRequestView(own=request_document(row), foreign_pending=False)
    return OpenRequestView(own=None, foreign_pending=True)


async def find_open_request(
    session: AsyncSession,
    group_id: int,
) -> GroupOnboardingRequest | None:
    stmt = select(GroupOnboardingRequest).where(
        GroupOnboardingRequest.group_id == int(group_id),
        GroupOnboardingRequest.status == STATUS_PENDING,
    )
    return (await session.scalars(stmt)).first()


async def list_requests(
    session: AsyncSession,
    *,
    applicant_id: int | None = None,
    status: str | None = None,
    limit: Any = None,
    offset: Any = 0,
) -> list[GroupOnboardingRequest]:
    """按角色取审理列表。

    ``applicant_id=None`` 是**超管视角**（全体队列）；传具体 id 就只能看到
    那个人自己提交的。调用方必须把会话身份算好再传进来——服务层没有别的办法
    知道请求是谁发的，也**不允许**从请求体里取身份。
    """

    page_limit = normalize_page_limit(limit)
    try:
        page_offset = max(0, int(offset or 0))
    except (TypeError, ValueError) as exc:
        raise OnboardingInvalid("offset 格式无效。") from exc
    stmt = select(GroupOnboardingRequest)
    if applicant_id is not None:
        stmt = stmt.where(GroupOnboardingRequest.applicant_id == int(applicant_id))
    if status is not None:
        normalized = str(status).strip().lower()
        if normalized not in STATUSES:
            raise OnboardingInvalid(f"未知状态：{normalized}")
        stmt = stmt.where(GroupOnboardingRequest.status == normalized)
    stmt = (
        stmt.order_by(
            GroupOnboardingRequest.status.asc(),
            GroupOnboardingRequest.id.desc(),
        )
        .offset(page_offset)
        .limit(page_limit)
    )
    return list((await session.scalars(stmt)).all())


async def count_requests(
    session: AsyncSession,
    *,
    applicant_id: int | None = None,
    status: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(GroupOnboardingRequest)
    if applicant_id is not None:
        stmt = stmt.where(GroupOnboardingRequest.applicant_id == int(applicant_id))
    if status is not None:
        stmt = stmt.where(GroupOnboardingRequest.status == str(status))
    return int(await session.scalar(stmt) or 0)


async def submit_request(
    session: AsyncSession,
    *,
    group_id: int,
    applicant_id: int,
    requested_features: Any = None,
    preset_id: Any = None,
) -> tuple[GroupOnboardingRequest, bool]:
    """提交申请；同群已有有效 pending 时幂等返回那一条。

    返回 ``(row, created)``。``created=False`` 表示这是一次重复提交——调用方
    应当按"已受理"而不是"新建"来响应，但仍然**不**授权任何东西。
    """

    normalized_group = normalize_group_id(group_id)
    try:
        normalized_applicant = strict_int(
            applicant_id, field="applicant_id", minimum=1
        )
    except StrictValueError as exc:
        raise OnboardingInvalid("applicant_id 格式无效。") from exc
    if normalized_applicant <= 0:
        raise OnboardingInvalid("applicant_id 无效。")
    features = normalize_features(requested_features)
    preset = normalize_preset_id(preset_id)

    row = GroupOnboardingRequest(
        request_id=new_request_id(),
        group_id=normalized_group,
        applicant_id=normalized_applicant,
        requested_features=features,
        preset_id=preset,
        status=STATUS_PENDING,
        revision=1,
        created_at=now_shanghai_naive(),
    )
    try:
        # SAVEPOINT 而不是裸 flush：唯一约束冲突必须只回滚**这一条** insert，
        # 调用方的外层事务（可能还带着别的审批写入）必须继续可用。
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await find_open_request(session, normalized_group)
        if existing is None:
            raise
        # 唯一索引只说明"这个群已经有 pending"。**是不是同一个人**要在这里
        # 判：同一个人重试是幂等，别人的申请则是冲突 —— 而且不能把那条申请
        # 的任何内容带进返回值。
        if int(existing.applicant_id) != normalized_applicant:
            raise OnboardingPendingConflict(
                OnboardingPendingConflict.message
            )
        return existing, False
    return row, True


async def _transition(
    session: AsyncSession,
    request_id: str,
    *,
    to_status: str,
    reviewed_by: int,
    reason: str = "",
) -> GroupOnboardingRequest:
    """pending → 终态的 CAS。``rowcount != 1`` 一律当冲突，不做静默兜底。"""

    moment = now_shanghai_naive()
    result = await session.execute(
        update(GroupOnboardingRequest)
        .where(
            GroupOnboardingRequest.request_id == str(request_id),
            GroupOnboardingRequest.status == STATUS_PENDING,
        )
        .values(
            status=to_status,
            revision=GroupOnboardingRequest.revision + 1,
            reviewed_at=moment,
            reviewed_by=int(reviewed_by),
            reason=reason,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise OnboardingConflict("申请状态已变化，请刷新后重试。")
    row = await find_request(session, request_id)
    if row is None:
        raise OnboardingNotFound("申请不存在。")
    await session.refresh(row)
    return row


async def current_authorization(
    session: AsyncSession,
    group_id: int,
    *,
    applicant_id: int | None = None,
) -> dict[str, Any]:
    """这个群**现在**的授权状态（只读）。

    与「申请曾经被批准过」是两件事：超管随时可以撤权，bot 也随时可能被踢。
    界面必须能把两者分开显示，否则"已批准"会被读成"现在还在服务"。
    """

    normalized = normalize_group_id(group_id)
    authorized = await session.get(AuthorizedGroup, normalized)
    present = authorized is not None and bool(authorized.bot_present)
    if applicant_id is None:
        # 查最近一条**已批准**的申请：否则调用方忘了传 applicant_id 时会返回
        # False，明明 Admin 行就在库里，界面却显示"申请人没有管理权"。
        stmt = (
            select(GroupOnboardingRequest.applicant_id)
            .where(
                GroupOnboardingRequest.group_id == normalized,
                GroupOnboardingRequest.status == STATUS_APPROVED,
            )
            .order_by(GroupOnboardingRequest.revision.desc())
            .limit(1)
        )
        applicant_id = (await session.scalars(stmt)).first()
    applicant_admin = False
    if applicant_id is not None:
        stmt = select(func.count()).select_from(Admin).where(
            Admin.group_id == normalized,
            Admin.user_id == int(applicant_id),
        )
        applicant_admin = bool(
            present and int(await session.scalar(stmt) or 0) > 0
        )
    return {
        "group_id": normalized,
        "authorized": present,
        "bot_present": present,
        "applicant_admin": applicant_admin,
    }


async def approve_request(
    session: AsyncSession,
    *,
    request_id: str,
    reviewer_id: int,
    reason: str = "",
) -> ApproveOutcome:
    """批准申请：状态翻转 + 群授权 + 申请人本群后台权，**同一个事务**。

    调用方负责事务边界（``async with session.begin():``）与 Telegram 实时身份
    核验——本函数**不持任何网络**，所以核验不会把写锁挂在那儿。

    已经 approved 的重复调用是幂等的：返回同一结果，不再重复授予。
    """

    request_ref = str(request_id or "")
    row = await get_request(session, request_ref)
    status = str(row.status or "")
    if status == STATUS_APPROVED:
        # **只读**：已批准的申请是一个历史事实，重放它不允许产生任何新的
        # 授权写入。之前这里会重跑 authorize_group/authorize_group_admin，
        # 于是"超管撤权 → 有人点两下重放旧批准"就能把撤销顶掉 —— 明确的
        # 当前决定被一条几个月前的记录压过去，而界面上看不出发生过任何事。
        #
        # 要恢复服务，走既有的显式授权命令，或者让群重新提交一条申请再审。
        live = await current_authorization(session, int(row.group_id))
        return ApproveOutcome(
            request_id=request_ref,
            group_id=int(row.group_id),
            applicant_id=int(row.applicant_id),
            status=STATUS_APPROVED,
            already_decided=True,
            # 这两个字段是"**这一次调用**是否授予了授权"，不是"现在是否
            # 还在授权"。历史决定与当前状态必须能被分别读出来。
            group_authorized=False,
            applicant_granted=False,
            currently_authorized=bool(live["authorized"]),
            currently_bot_present=bool(live["bot_present"]),
            currently_applicant_admin=bool(live["applicant_admin"]),
        )
    if status != STATUS_PENDING:
        raise OnboardingConflict("该申请已终结，无法批准。")

    updated = await _transition(
        session,
        request_ref,
        to_status=STATUS_APPROVED,
        reviewed_by=int(reviewer_id),
        reason=normalize_reason(reason),
    )
    authorized = await authorize_group(session, int(updated.group_id), int(reviewer_id))
    granted = await authorize_group_admin(
        session, int(updated.group_id), int(updated.applicant_id), "admin"
    )
    updated.granted_group_authorization = True
    await session.flush()
    log.info(
        "onboarding approved | request=%s group=%s applicant=%s reviewer=%s",
        request_ref,
        int(updated.group_id),
        int(updated.applicant_id),
        int(reviewer_id),
    )
    live = await current_authorization(
        session, int(updated.group_id), applicant_id=int(updated.applicant_id)
    )
    return ApproveOutcome(
        request_id=request_ref,
        group_id=int(updated.group_id),
        applicant_id=int(updated.applicant_id),
        status=STATUS_APPROVED,
        already_decided=False,
        group_authorized=bool(authorized),
        applicant_granted=bool(granted),
        currently_authorized=bool(live["authorized"]),
        currently_bot_present=bool(live["bot_present"]),
        currently_applicant_admin=bool(live["applicant_admin"]),
    )


async def reject_request(
    session: AsyncSession,
    *,
    request_id: str,
    reviewer_id: int,
    reason: str = "",
) -> GroupOnboardingRequest:
    """拒绝申请。拒绝是终态：之后再 approve 必须失败。"""

    return await _transition(
        session,
        str(request_id or ""),
        to_status=STATUS_REJECTED,
        reviewed_by=int(reviewer_id),
        reason=normalize_reason(reason),
    )


async def cancel_request(
    session: AsyncSession,
    *,
    request_id: str,
    actor_id: int,
    actor_is_super_admin: bool = False,
) -> GroupOnboardingRequest:
    """撤回申请：申请人本人或超管可撤 pending，其它情况一律拒绝。"""

    row = await get_request(session, str(request_id or ""))
    if not actor_is_super_admin and int(row.applicant_id) != int(actor_id):
        raise OnboardingForbidden("你只能撤回自己提交的申请。")
    return await _transition(
        session,
        str(request_id or ""),
        to_status=STATUS_CANCELLED,
        reviewed_by=int(actor_id),
        reason="",
    )

# ---------------------------------------------------------------------------
# 首次预设（first-run presets）
# ---------------------------------------------------------------------------
#: 预设**唯一**能写的群设置键。
#:
#: 刻意只有三个，且都必须是 ``_GroupSettingsUpdate`` **真实支持**的键 ——
#: 用一个不存在的群级开关去"假装已启用"，比什么都不做更糟：界面会说
#: 「已开启」，实际没有这回事。明确排除：审核规则/动作/阈值（安全口径不归预设
#: 碰）、全局模型、隐私留存、供应商密钥、付费权益、娱乐 AV。
PRESET_WRITABLE_KEYS: frozenset[str] = frozenset(
    {"mute_all_replies", "at_reply_mode", "tts_mode"}
)
#: 当前三个预设**实际**会写的键。``PRESET_WRITABLE_KEYS`` 是上界（白名单），
#: 这个是实际集合；差集非空说明有键列进了白名单却没有任何预设会写它，
#: 值得看一眼是不是漏了预设，或者白名单该收窄。
PRESET_ACTIVE_KEYS: frozenset[str] = frozenset({"mute_all_replies", "at_reply_mode"})


@dataclass(frozen=True, slots=True)
class PresetPatch:
    key: str
    value: Any
    effect: str


@dataclass(frozen=True, slots=True)
class PresetDefinition:
    preset_id: str
    title: str
    summary: str
    features: tuple[str, ...]
    patches: tuple[PresetPatch, ...]
    risks: tuple[str, ...]
    #: 「待准备」而不是「已就绪」的功能（缺权限/缺配置时如实说）。
    pending_notes: tuple[str, ...]


PRESET_CATALOG: dict[str, PresetDefinition] = {
    PRESET_MODERATION_ID: PresetDefinition(
        preset_id=PRESET_MODERATION_ID,
        title="审核型",
        summary="只做静默审核与处置，不主动发言；先把群规与权限配齐再考虑回复。",
        features=(FEATURE_MODERATION,),
        patches=(
            PresetPatch(
                key="mute_all_replies",
                value=True,
                effect="关闭所有主动/被 @ 回复，只保留审核与处置",
            ),
            PresetPatch(
                key="at_reply_mode",
                value=False,
                effect="被 @ 也不回复（与上面同向，不依赖另一处开关）",
            ),
        ),
        risks=(
            "本群的 AI 回复通道会在完成配置前保持静默（包括被 @）。",
            "既有群规与处置动作完全不受影响；按群规执行的审核、巡查等后台动作不受这条开关影响。",
        ),
        pending_notes=("入群真人验证需要管理员先完成验证服务配置。",),
    ),
    PRESET_COMPANION_ID: PresetDefinition(
        preset_id=PRESET_COMPANION_ID,
        title="陪伴型",
        summary="开启可回复，但默认只在被 @ 时应答，避免打扰。",
        features=(FEATURE_MODERATION, FEATURE_CASUAL_CHAT),
        patches=(
            PresetPatch(
                key="at_reply_mode",
                value=True,
                effect="被 @ 时正常回复",
            ),
            PresetPatch(
                key="mute_all_replies",
                value=False,
                effect="取消「全部静默」，恢复按规则应答",
            ),
        ),
        risks=(
            "被 @ 的消息会真调用模型并产生用量；未配置预算时不受额度限制。",
            # 刻意**不**写「语音播报保持关闭」：本预设根本不改 tts_mode，
            # 群里本来就开着的话，那句话是在说谎。
            "本预设不修改语音播报：当前设置与继承关系保持不变，需要改变请在群设置里单独开启。",
            "本预设只改回复方式，不会停掉按群规执行的审核、巡查等后台动作。",
        ),
        pending_notes=("语音播报不在本预设范围内，需要时请单独配置。",),
    ),
    PRESET_COMMUNITY_ID: PresetDefinition(
        preset_id=PRESET_COMMUNITY_ID,
        title="社区运营型",
        summary="在陪伴型基础上说明可用的运营功能；权限或配置没齐的只标「待准备」。",
        features=(
            FEATURE_MODERATION,
            FEATURE_CASUAL_CHAT,
            FEATURE_SUMMARY,
            FEATURE_PROACTIVE,
        ),
        patches=(
            PresetPatch(
                key="at_reply_mode",
                value=True,
                effect="被 @ 时正常回复",
            ),
            PresetPatch(
                key="mute_all_replies",
                value=False,
                effect="取消「全部静默」",
            ),
        ),
        risks=(
            "群摘要与主动话题需要本群单独开启，没配齐时只是「待准备」，不会被假装开启。",
            "本预设只改回复方式，不会停掉按群规执行的审核、巡查等后台动作。",
            "本预设不修改语音播报：当前设置与继承关系保持不变。",
            "娱乐 /av 与自动对外推送**不在**预设范围内，本预设不会开启它们。",
        ),
        pending_notes=(
            "群摘要需要配置压缩模型与摘要开关。",
            "主动话题需要配置主动任务开关。",
        ),
    ),
}


def readiness_features_from_query(values: Any) -> list[str]:
    """解析 ``?features=a&features=b``。

    刻意**不接受**逗号分隔的单个串：``features=moderation,vision`` 到底是
    一个未知功能名还是两个功能，取决于实现 —— 那正是"用户拼错一个逗号就得到
    一个看不懂的错误"的原因。重复参数没有歧义。
    """

    if values is None:
        return []
    if isinstance(values, str):
        # 单值时只接受"没有逗号的一个合法名"。
        if "," in values:
            raise OnboardingInvalid(
                "features 请用重复参数：?features=a&features=b"
            )
        values = [values]
    collected: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if text not in KNOWN_FEATURES:
            raise OnboardingInvalid(f"未知功能：{text}")
        if text not in collected:
            collected.append(text)
    if len(collected) > MAX_FEATURES:
        raise OnboardingInvalid("功能项过多。")
    return collected


def features_for_preset(preset_id: Any) -> list[str]:
    """预设 → 它宣称的期望功能（申请页与就绪页用同一份口径）。"""

    if preset_id is None or not str(preset_id).strip():
        return []
    return list(preset_definition(preset_id).features)


def preset_definition(preset_id: Any) -> PresetDefinition:
    normalized = normalize_preset_id(preset_id)
    if normalized is None or normalized not in PRESET_CATALOG:
        raise OnboardingInvalid("请选择一个有效的预设。")
    return PRESET_CATALOG[normalized]


def preset_patch_document(definition: PresetDefinition) -> list[dict[str, Any]]:
    return [
        {"key": patch.key, "value": patch.value, "effect": patch.effect}
        for patch in definition.patches
    ]


def preset_document(preset_id: Any) -> dict[str, Any]:
    definition = preset_definition(preset_id)
    return {
        "preset_id": definition.preset_id,
        "title": definition.title,
        "summary": definition.summary,
        "features": list(definition.features),
        "patch": preset_patch_document(definition),
        "risks": list(definition.risks),
        "pending_notes": list(definition.pending_notes),
    }


def preset_diff_document(
    definition: PresetDefinition,
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    """本群现值 → 候选值。

    没有 diff 的 preview 只是一份静态目录：管理员看不出"我开了什么会被改掉"。
    """

    from bot.services.at_reply import is_at_reply_enabled
    from bot.services.doubao_tts import normalize_tts_mode

    def read(key: str) -> Any:
        if key == "at_reply_mode":
            return bool(is_at_reply_enabled(current))
        if key == "tts_mode":
            return normalize_tts_mode(current)
        return current.get(key)

    rows: list[dict[str, Any]] = []
    for patch in definition.patches:
        before = read(patch.key)
        rows.append(
            {
                "key": patch.key,
                "current": before,
                "next": patch.value,
                "changes": _differs(before, patch.value),
                "effect": patch.effect,
            }
        )
    return rows


def _differs(current: Any, nxt: Any) -> bool:
    if isinstance(current, bool) or isinstance(nxt, bool):
        return bool(current) is not bool(nxt)
    return current != nxt


def preset_patch_body(definition: PresetDefinition) -> dict[str, Any]:
    """预设 → ``PUT /groups/{id}/settings`` 那种 ``settings`` 补丁。

    只含白名单键，且**过一遍真实的 ``_GroupSettingsUpdate``**：预设不是另一条
    写入路径，它必须和普通保存走同一份校验，否则将来有人给白名单加一个键却忘了
    同步校验，这里就会写出一个普通保存拒绝的文档。
    """

    body: dict[str, Any] = {}
    for patch in definition.patches:
        if patch.key not in PRESET_WRITABLE_KEYS:
            raise OnboardingInvalid(f"预设试图写入非白名单键：{patch.key}")
        body[patch.key] = patch.value
    return body


# ---------------------------------------------------------------------------
# 本群就绪体检
# ---------------------------------------------------------------------------
STATE_CONFIGURED = "configured"
STATE_PERMISSION_MISSING = "permission_missing"
STATE_PROVIDER_MISSING = "provider_missing"
STATE_UNVERIFIED = "unverified"
#: 配置齐备、但**本群开关没开**。和 configured 分开，是因为
#: 「验证服务配好了」与「这个群开了新人验证」是两件事，揉成一个词就会让
#: 面板显示成「已开启」而实际没开。
STATE_DISABLED = "disabled"

READINESS_STATES: tuple[str, ...] = (
    STATE_CONFIGURED,
    STATE_DISABLED,
    STATE_PERMISSION_MISSING,
    STATE_PROVIDER_MISSING,
    STATE_UNVERIFIED,
)

#: 未指定 features 时的默认口径：安全审核能力。刻意**不含** embed / summary ——
#: 一个只做审核的群不该因为没用向量库就被判成不就绪。
DEFAULT_READINESS_FEATURES: tuple[str, ...] = (FEATURE_MODERATION,)

#: 每个能力**真正**需要的群管理权限。刻意不含 ``can_invite_users``：
#: bot 加群不靠邀请成员，要求这个权限只会把一个完全可用的群判成不就绪。
CAPABILITY_REQUIRED_BOT_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "moderation": ("can_delete_messages",),
    "verification": ("can_restrict_members",),
}

_CONFIGURED_NOTE = "配置完整（仅配置检查，未做远端连通性测试）。"


@dataclass(frozen=True, slots=True)
class CapabilityReadiness:
    """一个能力在**四个互不替代**的维度上的结论。

    之前只有一个 ``state``，于是"本地参数齐备"、"Telegram 事实已核实"、
    "本群开关已开启"被揉成一个词 —— 最要命的是 ``facts=None``（问不成）
    时它仍然是 ``configured``，于是一个什么都没验证过的群显示成全绿。

    现在四个维度各自独立呈现，``state`` 只作为给界面用的**汇总**。
    """

    capability: str
    #: 本地参数齐备？（不蕴含任何远端连通性结论）
    configured: bool
    #: Telegram 侧事实已核实？（bot 在群、权限已读到）
    verified: bool = False
    #: 本群开关已开启？``None`` 表示该能力没有群级开关概念。
    enabled: bool | None = None
    #: 该能力依赖的外部服务是否配好（与"本群是否开启"分开）。
    service_configured: bool | None = None
    state: str = STATE_CONFIGURED
    detail: str = ""
    required_bot_permissions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "configured": bool(self.configured),
            "verified": bool(self.verified),
            "enabled": self.enabled,
            "service_configured": self.service_configured,
            "state": self.state,
            "detail": self.detail,
            "required_bot_permissions": list(self.required_bot_permissions),
            "blockers": list(self.blockers),
        }


def _chat_readiness(name: str, cfg: Any) -> CapabilityReadiness:
    """按**配置本身**判定一个对话类模型能力，绝不真的去连供应商。

    每次打开面板就打五家供应商既慢、又把「打开面板」变成一次计费行为，所以
    这里只做配置自洽性检查，返回的 detail 一律说清「没有远端连通性测试」。
    """

    from bot.services.llm import LLMService

    required = CAPABILITY_REQUIRED_BOT_PERMISSIONS.get(name, ())
    if cfg is None:
        return CapabilityReadiness(
            capability=name,
            configured=False,
            state=STATE_PROVIDER_MISSING,
            detail="尚未配置模型。",
            required_bot_permissions=required,
            blockers=(STATE_PROVIDER_MISSING,),
        )
    candidates = [
        candidate
        for candidate in LLMService._chat_candidates(cfg)
        if str(getattr(candidate, "model", "") or "").strip()
    ]
    if not candidates:
        return CapabilityReadiness(
            capability=name,
            configured=False,
            state=STATE_PROVIDER_MISSING,
            detail="尚未配置模型。",
            required_bot_permissions=required,
            blockers=(STATE_PROVIDER_MISSING,),
        )
    if any(not LLMService.chat_configuration_issue(c) for c in candidates):
        return CapabilityReadiness(
            capability=name,
            configured=True,
            state=STATE_CONFIGURED,
            detail=_CONFIGURED_NOTE,
            required_bot_permissions=required,
        )
    return CapabilityReadiness(
        capability=name,
        configured=False,
        state=STATE_PROVIDER_MISSING,
        detail="缺少该模型所需的供应商凭据（仅配置检查，未做远端连通性测试）。",
        required_bot_permissions=required,
        blockers=(STATE_PROVIDER_MISSING,),
    )


def _embed_readiness(cfg: Any) -> CapabilityReadiness:
    from bot.services.llm import LLMService

    if cfg is None:
        return CapabilityReadiness(
            capability="embed",
            configured=False,
            state=STATE_PROVIDER_MISSING,
            detail="尚未配置嵌入模型。",
            blockers=(STATE_PROVIDER_MISSING,),
        )
    candidates = [
        candidate
        for candidate in LLMService._embed_candidates(cfg)
        if str(getattr(candidate, "model", "") or "").strip()
    ]
    if not candidates:
        return CapabilityReadiness(
            capability="embed",
            configured=False,
            state=STATE_PROVIDER_MISSING,
            detail="尚未配置嵌入模型。",
            blockers=(STATE_PROVIDER_MISSING,),
        )
    if any(not LLMService.chat_configuration_issue(c) for c in candidates):
        return CapabilityReadiness(
            capability="embed",
            configured=True,
            state=STATE_CONFIGURED,
            detail=_CONFIGURED_NOTE,
        )
    return CapabilityReadiness(
        capability="embed",
        configured=False,
        state=STATE_PROVIDER_MISSING,
        detail="缺少嵌入模型所需的供应商凭据（仅配置检查，未做远端连通性测试）。",
        blockers=(STATE_PROVIDER_MISSING,),
    )


def group_readiness_document(
    *,
    settings: Any,
    facts: TelegramGroupFacts | None,
    features: Any = DEFAULT_READINESS_FEATURES,
    group_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """本群功能就绪快照。

    四条规则：

    1. ``facts is None``（Telegram 问不成）→ 整体 ``unverified``、``ready=False``。
       曾经这里只是把 ``verified`` 写在旁边的小字里，六条能力仍然是
       ``configured``、整体仍然 ``ready`` —— 一个什么都没验证过的群显示成全绿。
    2. 每个能力把 configured / verified / enabled 分开呈现。
    3. 阻断只按**本群选了哪些功能**算：只做审核的群不该因为没配向量库
       而显示成不就绪；但所有能力都列出来。
    4. 永不为了面板真的去调一次模型 —— 打开面板不是一次隐式压测。
    """

    stored = group_settings if isinstance(group_settings, dict) else {}
    verified = facts is not None
    selected = normalize_features(features)

    bot_cfg = getattr(settings, "bot", None)
    capabilities: list[CapabilityReadiness] = []
    if bot_cfg is not None:
        decision = getattr(bot_cfg, "decision_model", None)
        moderation = getattr(bot_cfg, "moderation_model", None) or decision
        capabilities.append(_chat_readiness("main", getattr(bot_cfg, "main_model", None)))
        capabilities.append(_chat_readiness("decision", decision))
        capabilities.append(_chat_readiness("moderation", moderation))
        capabilities.append(
            _chat_readiness(
                "vision",
                getattr(bot_cfg, "vision_model", None)
                or getattr(bot_cfg, "main_model", None),
            )
        )
        capabilities.append(_embed_readiness(getattr(bot_cfg, "embed_model", None)))

    capabilities.append(
        _verification_readiness(settings, stored, verified=verified, facts=facts)
    )
    # 摘要/主动/语音一律读**真实消费者**判定的启用态：
    #   摘要  → group_summary_config(settings).enabled（真实配置在 settings.bot 上）
    #   主动  → proactive.is_cooldown_task_enabled(群设置, 全局默认)
    #   语音  → doubao_tts.is_tts_tool_enabled(群 tts_mode)
    # 此前自己读根对象的 group_summary_enabled（层级猜错），并新增了一个
    # 没有任何消费者的 summary_enabled 群键 —— 两者都是读不存在的事实。
    capabilities.append(
        _consumed_switch_readiness(
            "summary",
            configured=_model_actually_usable(
                getattr(bot_cfg, "compress_model", None)
            ),
            enabled=summary_enabled_for_group(stored, settings),
        )
    )
    capabilities.append(
        _consumed_switch_readiness(
            "proactive",
            configured=_model_actually_usable(getattr(bot_cfg, "main_model", None)),
            enabled=proactive_enabled_for_group(stored, settings),
        )
    )
    # tts **无条件**列出。此前只在 stored 里有 tts_mode 时才追加，而缺失的
    # 能力在聚合阶段被静默跳过 —— 于是"选了 tts 但它是默认关闭"这种最常见的
    # 新群状态，既不列能力也不阻断，直接 ready。缺失不是"没问题"，是"没开"。
    #
    # configured 走真实语音后端（不是 main 模型）；enabled 走真实消费判定
    # （当前默认 off），两者都不因为"键不存在"而变成 true。
    backend_available, backend_detail = tts_backend_status(settings)
    capabilities.append(
        _consumed_switch_readiness(
            "tts",
            configured=backend_available,
            enabled=tts_enabled_for_group(stored),
            detail_hint=str(stored.get("tts_mode") or ""),
            unavailable_detail=backend_detail,
        )
    )


    # bot 权限不足 → 对应能力降级为 permission_missing（配置再全也用不了）。
    if verified and facts is not None:
        adjusted: list[CapabilityReadiness] = []
        for capability in capabilities:
            missing = [
                name
                for name in capability.required_bot_permissions
                if not bool(facts.bot_permissions.get(name))
            ]
            if missing:
                adjusted.append(
                    CapabilityReadiness(
                        capability=capability.capability,
                        configured=capability.configured,
                        verified=True,
                        enabled=capability.enabled,
                        service_configured=capability.service_configured,
                        state=STATE_PERMISSION_MISSING,
                        detail="机器人缺少本能力需要的群管理权限：" + "、".join(missing),
                        required_bot_permissions=capability.required_bot_permissions,
                        blockers=(STATE_PERMISSION_MISSING,),
                    )
                )
            else:
                adjusted.append(
                    replace(
                        capability,
                        verified=True,
                        blockers=capability.blockers,
                    )
                )
        capabilities = adjusted

    if not verified:
        # 问不成时**每一项**都是 unverified：局部报 configured 会让界面把
        # "没查到" 读成 "有"。
        capabilities = [
            CapabilityReadiness(
                capability=item.capability,
                configured=item.configured,
                verified=False,
                enabled=item.enabled,
                service_configured=item.service_configured,
                state=STATE_UNVERIFIED,
                detail="尚未向 Telegram 核实，无法判断该能力现在是否可用。",
                required_bot_permissions=item.required_bot_permissions,
                blockers=(STATE_UNVERIFIED,),
            )
            for item in capabilities
        ]

    capability_by_name = {item.capability: item for item in capabilities}
    blockers: list[str] = []
    if not verified:
        blockers.append(STATE_UNVERIFIED)
    for feature in selected:
        for capability_name in _FEATURE_CAPABILITIES.get(feature, ()):
            item = capability_by_name.get(capability_name)
            if item is None:
                # 所选功能依赖的能力**根本不存在**：这是"没有就绪"而不是
                # "没有阻断"。静默 continue 会让缺能力一律显示为 ready。
                token = f"{capability_name}:missing"
                if token not in blockers:
                    blockers.append(token)
                continue
            for blocker in item.blockers:
                token = f"{capability_name}:{blocker}"
                if token not in blockers:
                    blockers.append(token)

    missing_permissions = facts.missing_permissions(selected) if facts else []
    return {
        "capabilities": [item.document() for item in capabilities],
        "blockers": blockers,
        "state": STATE_UNVERIFIED if not verified else (
            STATE_CONFIGURED if not blockers else STATE_PROVIDER_MISSING
        ),
        "ready": bool(verified and not blockers),
        "selected_features": list(selected),
        "remote_probe": False,
        "remote_probe_note": "本结果只做配置与权限检查，不会真的调用任何模型或供应商。",
        "chat_type": facts.chat_type if facts is not None else "",
        "bot_present": bool(facts.bot_present) if facts is not None else None,
        "bot_status": facts.bot_status if facts is not None else "",
        "bot_is_administrator": bool(facts.bot_is_administrator)
        if facts is not None
        else None,
        "missing_bot_permissions": missing_permissions,
    }


#: 每个期望功能依赖哪些能力。**只**用于决定"哪些阻断算阻断"。
_FEATURE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    FEATURE_MODERATION: ("moderation",),
    FEATURE_CASUAL_CHAT: ("main", "decision"),
    FEATURE_TOOLS: ("main",),
    FEATURE_VISION: ("vision",),
    FEATURE_SUMMARY: ("summary",),
    FEATURE_PROACTIVE: ("proactive",),
    FEATURE_TTS: ("tts",),
    FEATURE_JOIN_VERIFICATION: ("verification",),
}


def _verification_readiness(
    settings: Any,
    group_settings: dict[str, Any],
    *,
    verified: bool,
    facts: TelegramGroupFacts | None,
) -> CapabilityReadiness:
    """入群验证：服务配好了 ≠ 这个群开了新人验证。

    两件事必须分开报。``service_configured`` 回答「全局验证服务有没有密钥」，
    ``enabled`` 回答「**这个群**开没开」。把前者当成后者，面板就会在一个
    完全没开新人验证的群里显示"已开启"。
    """

    from bot.services.join_verification import (
        join_verification_policy,
        verification_service_ready,
    )

    provider = str(group_settings.get("join_verification_provider") or "").strip()
    service_ready = bool(verification_service_ready(settings, provider or None))
    enabled, effective_provider = join_verification_policy(settings, group_settings)
    required = CAPABILITY_REQUIRED_BOT_PERMISSIONS["verification"]
    if not service_ready:
        state = STATE_PROVIDER_MISSING
        detail = "入群验证服务尚未配置密钥。"
        blockers: tuple[str, ...] = (STATE_PROVIDER_MISSING,)
    elif not enabled:
        state = STATE_DISABLED
        detail = "验证服务已配置，但**本群未开启**新人验证。"
        blockers = (STATE_DISABLED,)
    else:
        state = STATE_CONFIGURED
        detail = f"本群已开启新人验证（provider={effective_provider}）。"
        blockers = ()
    return CapabilityReadiness(
        capability="verification",
        configured=service_ready,
        verified=verified and facts is not None,
        enabled=bool(enabled),
        service_configured=service_ready,
        state=state,
        detail=detail,
        required_bot_permissions=required,
        blockers=blockers,
    )


def _consumed_switch_readiness(
    capability: str,
    *,
    configured: bool,
    enabled: bool,
    detail_hint: str = "",
    unavailable_detail: str = "",
) -> CapabilityReadiness:
    """按**真实消费者**读到的启用态判定，不再引入影子开关。"""

    state = STATE_CONFIGURED if enabled else STATE_DISABLED
    if not configured:
        state = STATE_PROVIDER_MISSING
    return CapabilityReadiness(
        capability=capability,
        configured=configured,
        verified=False,
        enabled=enabled,
        state=state,
        detail=(
            unavailable_detail
            if not configured
            else (
                f"本群已开启（{detail_hint}）。"
                if enabled and detail_hint
                else (
                    "本群已开启。"
                    if enabled
                    else "本群未启此功能（继承全局默认）。"
                )
            )
        ),
        blockers=() if enabled and configured else (state,),
    )


def summary_enabled_for_group(stored: dict[str, Any], settings: Any) -> bool:
    """群摘要是否真的在跑 —— 走**真实消费者** ``group_summary_config``。

    之前自己读根对象的 ``group_summary_enabled``，而真实配置在 ``settings.bot``
    上：明明开了也被报成关闭。也没必要自己造 ``summary_enabled`` 群键 —— 那个键
    没有任何消费者，读它等于读一个不存在的事实。
    """

    from bot.services.group_summary import group_summary_config

    return bool(group_summary_config(settings).enabled)


def proactive_enabled_for_group(stored: dict[str, Any], settings: Any) -> bool:
    """主动话题是否真的在跑 —— 走**真实消费者** ``is_cooldown_task_enabled``。"""

    from bot.services.proactive import is_cooldown_task_enabled

    bot_cfg = getattr(settings, "bot", None)
    return bool(
        is_cooldown_task_enabled(
            stored,
            default_enabled=bool(getattr(bot_cfg, "proactive_enabled", False)),
        )
    )


def tts_enabled_for_group(stored: dict[str, Any]) -> bool:
    """本群语音是否会被触发 —— 走**真实判定** ``is_tts_tool_enabled``。"""

    from bot.services.doubao_tts import is_tts_tool_enabled

    return bool(is_tts_tool_enabled(stored.get("tts_mode")))


def tts_backend_status(settings: Any) -> tuple[bool, str]:
    """真实 TTS 后端现在能不能用（连同一句能直接展示的原因）。

    直接问 ``DoubaoTTSService(settings).available`` —— 它已经覆盖了
    Edge 音色、全局开关、以及豆包的 app_id / app_key / access_key /
    resource_id / speaker 全部必填项。**不**在这里重造一套判据：重造出来
    的版本一定会和真实发送路径漂移。
    """

    from bot.services.doubao_tts import DoubaoTTSService

    service = DoubaoTTSService(settings)
    if service.available:
        return True, "语音后端可用。"
    if not service.enabled:
        return False, "语音后端已在全局关闭。"
    if service.edge_voice:
        return False, "语音后端未配置有效音色。"
    missing = [
        name
        for name, value in (
            ("api_base", service.api_base),
            ("app_id", service.app_id),
            ("access_key", service.access_key),
            ("resource_id", service.resource_id),
            ("speaker", service.speaker),
        )
        if not value
    ]
    if missing:
        return False, "语音后端缺少凭据：" + "、".join(missing) + "。"
    return False, "语音后端未配置可用。"


def _model_actually_usable(cfg: Any) -> bool:
    """模型配置对象**存在**不等于可用：模型名或凭据为空时它根本跑不起来。

    之前用 ``bool(cfg)``：``ModelConfig(model="", api_key="")`` 也是真对象，
    于是摘要没有任何可用模型仍被报成 configured → ready。
    """

    from bot.services.llm import LLMService

    if cfg is None:
        return False
    try:
        candidates = [
            candidate
            for candidate in LLMService._chat_candidates(cfg)
            if str(getattr(candidate, "model", "") or "").strip()
        ]
    except Exception:  # noqa: BLE001 - 形状异常即不可用
        return False
    return bool(candidates) and any(
        not LLMService.chat_configuration_issue(candidate)
        for candidate in candidates
    )
