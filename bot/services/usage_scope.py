"""用量归属上下文：每一次模型尝试属于谁、为什么。

为什么用上下文而不是把 group_id 一路传下去：真实的调用链是
``handler → reply_mode → llm.chat → _chat_with_fallbacks →
_chat_completion_response_with_retries → provider``，中间夹着重试、回退、工具
循环与后台调度。参数要传对每一层就得改每一层的签名，而漏掉一层的默认后果是
**把某个群的用量记到别人头上** —— 这比不记更坏。上下文让"当前这条消息属于哪
个群"成为一个显式、单一、且可以断言的事实。

三个刻意为之的设计：

* **默认是 system，不是任何群**。拿不到归属时宁可进 system 桶，也不猜一个群；
  猜错会凭空造出一个不存在的群账单。
* **``group_id=0`` 不是群**。``kind='group'`` 必须给一个真实的负数群标识，
  非群归属在库里用 0 只是唯一索引的技术占位。
* **用途与阶段分开**。``stage='vision'`` 既可能是娱乐看图，也可能是审核取证；
  只看 stage 就停功能会误停安全审核，所以 purpose 是一等公民，并且显式传入
  永远压过按 stage 的默认值。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Iterator

from bot.services.strict_values import (
    StrictValueError,
    strict_int,
    strict_telegram_id,
)

log = logging.getLogger(__name__)

SCOPE_GROUP = "group"
SCOPE_PRIVATE = "private"
SCOPE_PRIVATE_USER = "private_user"
SCOPE_SYSTEM = "system"

SCOPE_KINDS: tuple[str, ...] = (
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    SCOPE_PRIVATE_USER,
    SCOPE_SYSTEM,
)

PURPOSE_SAFETY = "safety"
PURPOSE_OPTIONAL = "optional"
PURPOSE_BACKGROUND_OPTIONAL = "background_optional"

PURPOSES: tuple[str, ...] = (
    PURPOSE_SAFETY,
    PURPOSE_OPTIONAL,
    PURPOSE_BACKGROUND_OPTIONAL,
)

#: 后台私聊类任务的匿名聚合桶名。**刻意不含 user id**：后台作业不该把用量
#: 归到「发起它的那位管理员」头上 —— 那会让一个群的成本显示在另一个人的私聊里。
ANONYMOUS_BUCKET = "anonymous"

#: 安全用途阶段：这些是审核/验证/执法路径上的判断，预算永远不得停它们。
SAFETY_STAGES: frozenset[str] = frozenset(
    {
        "moderation",
        "decision",
        "moderation_vision",
        "verification_policy",
        "profile_verdict",
        "profile_screening",
        "admission",
        "join_verification",
        "patrol",
        "raid_guard",
        "vote_ban",
        "challenge_prompt",
        "challenge_prepare",
        "risk_check",
    }
)

#: 后台可选阶段：离线跑、不面向当前用户，但仍不该被当成「回复」记账。
#: 注意 ``synopsis``（/av 的 AI 概览）**不在**这里：那是用户当场触发的可选功能，
#: 按 optional 记账才对，放进 background 会把它算成后台作业。
BACKGROUND_OPTIONAL_STAGES: frozenset[str] = frozenset(
    {
        "group_summary",
        "summary",
        "replay",
        "moderation_replay",
        "memory_extract",
        "dm_search",
        "compress",
        "index",
        "weekly_report",
    }
)


class ScopeInvalid(ValueError):
    """归属上下文参数不合法。"""


@dataclass(frozen=True, slots=True)
class UsageScope:
    kind: str
    group_id: int | None
    purpose: str
    stage: str = ""
    #: 只有 ``kind='private_user'`` 有意义；后台任务一律 anonymous。
    bucket: str = ANONYMOUS_BUCKET

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "group_id": self.group_id,
            "purpose": self.purpose,
            "stage": self.stage,
        }


#: 默认归属的用途是**未定**（空串），不是 safety：默认桶里什么都有可能被调用，
#: 在 ``record_attempt`` 拿到真实 stage 之前先给它贴上 safety，等于让一条普通
#: 群消息的回复被当成安全调用记账。
SYSTEM_SCOPE = UsageScope(
    kind=SCOPE_SYSTEM, group_id=None, purpose="", stage=""
)


_CURRENT: ContextVar[UsageScope] = ContextVar("llm_usage_scope", default=SYSTEM_SCOPE)


def default_purpose_for_stage(stage: str) -> str:
    name = str(stage or "").strip().lower()
    if name in SAFETY_STAGES:
        return PURPOSE_SAFETY
    if name in BACKGROUND_OPTIONAL_STAGES:
        return PURPOSE_BACKGROUND_OPTIONAL
    return PURPOSE_OPTIONAL


def current_usage_scope() -> UsageScope:
    return _CURRENT.get()


def effective_purpose(scope: UsageScope, stage: str) -> str:
    """这条 attempt 的最终用途：调用方没钉死就按**实际 stage** 推断。

    归属在 update 入口建立时还不知道这次要调什么模型，所以那里不能定用途；
    真正的判定点在每次尝试拿到 stage 的那一刻。把这个推迟到最后一刻，审核
    路径才不会因为「上下文先建、stage 后定」而被算成可选功能。
    """

    return resolve_purpose(scope.purpose or None, stage)


def resolve_purpose(purpose: Any, stage: str) -> str:
    if purpose is None or not str(purpose).strip():
        return default_purpose_for_stage(stage)
    normalized = str(purpose).strip().lower()
    if normalized not in PURPOSES:
        raise ScopeInvalid(f"未知用途：{normalized}")
    return normalized


def build_scope(
    *,
    kind: Any = SCOPE_SYSTEM,
    group_id: Any = None,
    purpose: Any = None,
    stage: Any = "",
    user_id: Any = None,
    bucket: Any = None,
) -> UsageScope:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in SCOPE_KINDS:
        raise ScopeInvalid(f"未知归属类型：{normalized_kind}")

    resolved_group: int | None = None
    if normalized_kind == SCOPE_GROUP:
        # 群归属必须给一个**真实的** Telegram 群标识（负数）。0 是"未知"的
        # 危险替代品：它会让一条无法归属的用量看起来像某个群的账单。
        if group_id is None:
            raise ScopeInvalid("群归属必须提供 group_id。")
        # 严格解析：``int(-100900000002.75)`` 会返回 ``-100900000002``，
        # 于是用量被记到一个**并不存在**的群上，而截断掉的小数没有任何痕迹。
        try:
            resolved_group = strict_telegram_id(group_id, field="group_id")
        except StrictValueError as exc:
            raise ScopeInvalid("group_id 格式无效。") from exc
        if resolved_group >= 0:
            raise ScopeInvalid("group_id 必须是群标识（负数），不能是 0 或用户 id。")
    elif group_id is not None:
        raise ScopeInvalid(f"{normalized_kind} 归属不接受 group_id。")

    resolved_stage = str(stage or "").strip().lower()[:32]
    # 没显式给用途就留空，等 ``effective_purpose`` 拿到真实 stage 再定。
    resolved_purpose = (
        "" if purpose is None or not str(purpose).strip() else resolve_purpose(purpose, resolved_stage)
    )

    resolved_bucket = ANONYMOUS_BUCKET
    if normalized_kind == SCOPE_PRIVATE_USER:
        if bucket is not None:
            candidate = str(bucket).strip().lower()
            if candidate not in {"", ANONYMOUS_BUCKET, "self"}:
                raise ScopeInvalid(
                    "private_user 后台归属只能匿名聚合，不接受具名用户桶。"
                )
        # user_id 存在**只是兼容入参**：它不会被写进 bucket。
        if user_id is not None:
            try:
                strict_int(user_id, field="user_id", minimum=1)
            except StrictValueError as exc:
                raise ScopeInvalid("user_id 格式无效。") from exc
    elif bucket not in (None, "", ANONYMOUS_BUCKET):
        raise ScopeInvalid(f"{normalized_kind} 归属不接受 bucket。")

    return UsageScope(
        kind=normalized_kind,
        group_id=resolved_group,
        purpose=resolved_purpose,
        stage=resolved_stage,
        bucket=resolved_bucket,
    )


@contextmanager
def usage_scope(
    *,
    kind: Any = SCOPE_SYSTEM,
    group_id: Any = None,
    purpose: Any = None,
    stage: Any = "",
    user_id: Any = None,
    bucket: Any = None,
) -> Iterator[UsageScope]:
    """把一次归属绑定到当前任务；退出时**一定**还原（包括异常与取消）。"""

    scope = build_scope(
        kind=kind,
        group_id=group_id,
        purpose=purpose,
        stage=stage,
        user_id=user_id,
        bucket=bucket,
    )
    token: Token[UsageScope] = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


@contextmanager
def lenient_scope(**kwargs: Any) -> Iterator[UsageScope]:
    """后台作业用的宽松版：归属算不出来就退到 system，**不**让作业失败。

    直接调 :func:`usage_scope` 仍然严格（那里传错 group_id 是调用方的 bug，
    必须当场炸）。但后台调度器绑归属时不一样：摘要/索引/回放是因为拿到的
    群 id 不合法才失败的话，用户看到的是「摘要停了」而不是「这个 id 不是群」，
    而账只是少一栏数字 —— 代价完全不成比例。所以这一条只降级归属，不降级作业。

    用途仍会按 stage 推断，退到 system 桶也不会让安全用途变成可选用途。
    """

    try:
        scope = build_scope(**kwargs)
    except ScopeInvalid:
        log.info(
            "usage scope unavailable, attributing to system | kind=%s",
            kwargs.get("kind"),
        )
        scope = SYSTEM_SCOPE
    token: Token[UsageScope] = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


@contextmanager
def stage_scope(
    stage: Any,
    *,
    group_id: Any = None,
    kind: Any = SCOPE_GROUP,
    purpose: Any = None,
    user_id: Any = None,
    bucket: Any = None,
) -> Iterator[UsageScope]:
    """只给一个 stage 用的便捷入口：用途按 stage 推断，显式值仍然优先。"""

    with usage_scope(
        kind=kind,
        group_id=group_id,
        purpose=purpose,
        stage=stage,
        user_id=user_id,
        bucket=bucket,
    ) as scope:
        yield scope


def with_stage(scope: UsageScope, stage: Any) -> UsageScope:
    """在**已有**归属上补一个更细的 stage（不改 kind/group/purpose）。"""

    return replace(scope, stage=str(stage or "").strip().lower()[:32])


def with_purpose(scope: UsageScope, purpose: Any) -> UsageScope:
    """在已有归属上显式改用途（例如把娱乐看图和审核取证区分开）。"""

    return replace(scope, purpose=resolve_purpose(purpose, scope.stage))


def set_current(scope: UsageScope) -> Token[UsageScope]:
    return _CURRENT.set(scope)


def reset_current(token: Token[UsageScope]) -> None:
    _CURRENT.reset(token)


def reset() -> None:
    """恢复到默认 system 归属（测试与运维用）。"""

    _CURRENT.set(SYSTEM_SCOPE)