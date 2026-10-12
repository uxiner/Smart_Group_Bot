from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from functools import wraps
from typing import Annotated, Any, Literal

from aiohttp import web
from aiohttp.web_exceptions import HTTPRequestEntityTooLarge
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
)
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services import policy_runtime
from bot.config import Settings
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupMember,
    GroupPermanentMemory,
    KeywordReply,
    ModerationExemption,
    ModerationReplayResult,
    ModerationReplayRun,
    ModerationReplaySample,
    ModerationRule,
    ReplyMute,
    ScheduledMessage,
    SpeechStyleSample,
    UserWarning,
)
from bot.services.at_reply import is_at_reply_enabled, set_at_reply_enabled
from bot.services.api_model_query import (
    clear_group_api_model_query_secret,
    get_api_model_query_config,
    group_api_model_query_secret_exists,
    normalize_api_model_query_api_key,
    normalize_api_model_query_base_url,
    replace_group_api_model_query_secret,
    set_api_model_query_config,
)
from bot.services.ban_audit import record_ban_event
from bot.services.runtime_config_history import (
    RuntimeConfigHistoryNotFoundError,
    RuntimeConfigHistoryNotRestorableError,
    build_diff,
    discover_secret_paths,
)
from bot.services.doubao_tts import normalize_tts_mode, set_tts_mode
from bot.services import memory_transparency
from bot.services.long_term_memory import (
    CORRECTION_CONFLICT,
    CORRECTION_CORRECTED,
    CORRECTION_INVALID,
    CORRECTION_OPERATOR_ADMIN,
    CORRECTION_UNCHANGED,
    CORRECTION_UNCONFIRMED,
    REASON_BAD_ARGUMENT,
    REASON_EMPTY,
    REASON_SENSITIVE,
    REASON_TOO_LONG,
    MemorySessionCleanupUnsafeError,
    correct_fact,
)
from bot.services.memory_source import expand_fact_source

#: C3 人工纠错的对外文案：只给结论 + 稳定 ID/修订号，**不回显**正文或来源原文。
MEMORY_CORRECTION_UNCONFIRMED_MESSAGE = (
    "未能确认这次修改是否已生效（可能已经写入）。"
    "请显式重新查看这条事实的最新内容，不要自动重试。"
)
MEMORY_CORRECTION_CONFLICT_MESSAGE = (
    "这条事实在你查看之后又变了（可能已被再次确认、纠错、删除、到期或屏蔽）。"
    "请显式重新查看最新内容后再改；你的草稿保留在本地。"
)
_MEMORY_CORRECTION_INVALID_MESSAGES = {
    REASON_EMPTY: "新内容不能为空。",
    REASON_TOO_LONG: "新内容太长了（一条长期事实最多 200 个字）。",
    REASON_SENSITIVE: (
        "新内容含敏感信息（密码、证件号、银行卡、长数字串等），不能写入长期记忆。"
    ),
    REASON_BAD_ARGUMENT: "纠错参数无效。",
}
from bot.services.memory import _pm_create_session
from bot.services.group_scoped_writes import update_group_scoped_row
from bot.services.permanent_memory_writes import (
    PM_WRITE_DELETED,
    PM_WRITE_NO_CHANGE,
    PM_WRITE_NOT_FOUND,
    PM_WRITE_UNAVAILABLE,
    PM_WRITE_UNCONFIRMED,
    PM_WRITE_UNCONFIRMED_MESSAGE,
    PM_WRITE_UPDATED,
    PM_WRITE_UNAVAILABLE_MESSAGE,
    delete_permanent_memory_row,
    update_permanent_memory_row,
)
from bot.services.group_scoped_writes import (
    GROUP_ROW_DELETED,
    GROUP_ROW_NO_CHANGE,
    GROUP_ROW_NOT_FOUND,
    GROUP_ROW_UNCONFIRMED,
    delete_group_scoped_row,
)
from bot.services import moderation_replay
from bot.services.proactive import (
    get_cooldown_task_state,
    set_cooldown_task_enabled,
)
from bot.services.join_screening import (
    add_global_ban,
    is_globally_banned,
    list_global_bans,
    list_join_screening_exemptions,
    remove_global_ban,
    remove_join_screening_exemption,
)
from bot.services.join_verification import (
    activate_manual_unban_recoveries,
    activate_manual_unban_recovery,
    ban_member as enforce_ban_member,
    close_private_challenge_messages,
    complete_leased_join_verification,
    delete_join_verification,
    delete_verification_prompts,
    get_join_verification,
    join_verification_policy,
    lease_join_verification_for_unban,
    lease_join_verifications_for_user_unban,
    reconcile_moderation_ban_after_lost_lease,
    release_moderation_restriction_after_exemption,
    restore_member_permissions,
    turnstile_verification_configured,
    unban_member as enforce_unban_member,
    verification_provider,
    verification_release_blocked_by_ban,
    verification_restriction_required,
    verification_service_ready,
)
from bot.services.update_delivery import (
    clear_privileged_operator_group,
    mark_privileged_operator,
    unmark_privileged_operator,
)
from bot.services.request_priority import privileged_request_scope
from bot.services.group_permissions import (
    GROUP_PERMISSIONS_SETTINGS_KEY,
    PERMISSION_FIELDS,
    fetch_telegram_default_permissions,
    get_group_permission_service,
    normalize_group_permission_config,
    permission_field_document,
    repair_group_permissions_config,
    resolve_group_permissions,
)
from bot.services import group_costs, group_onboarding, pricing
from bot.services.group_budget import BudgetConflict, BudgetError, shared_service
from bot.services.strict_values import StrictValueError, strict_int
from bot.services.message_templates import normalize_template_buttons
from bot.services.member_identity import member_identity_document
from bot.services.moderation import normalize_scan_scope
from bot.services import operations_runs
from bot.services.operations_runs import OperationsError
from bot.services.operations_task_status import (
    EXTERNAL_SCHEDULE_UNKNOWN,
    TASK_TITLES,
    collect_operations_task_status,
)
from bot.services import task_policy
from bot.services.patrol import get_patrol_service, parse_schedule_time, patrol_policy
from bot.services.runtime_config import (
    HISTORY_PAGE_LIMIT,
    HISTORY_PAGE_MAX_LIMIT,
    RuntimeConfigConflictError,
    RuntimeConfigEncryptionError,
    RuntimeConfigManager,
)
from bot.services.speech_style import (
    STYLE_SETTINGS_KEY,
    get_style_state,
    set_style_target,
)
from bot.utils.security import clean_multiline_text, clean_text
from bot.utils.timezone import now_shanghai_naive
from bot.web.auth import require_authenticated_user, require_super_admin

log = logging.getLogger(__name__)


async def _run_privileged_group_fanout(
    group_ids: list[int],
    operation: Callable[[int], Awaitable[bool]],
) -> list[bool]:
    semaphore = asyncio.Semaphore(4)

    async def run_one(group_id: int) -> bool:
        async with semaphore:
            try:
                with privileged_request_scope():
                    async with asyncio.timeout(45.0):
                        return bool(await operation(int(group_id)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.info(
                    "web privileged group operation failed | group=%s",
                    group_id,
                    exc_info=True,
                )
                return False

    return list(await asyncio.gather(*(run_one(group_id) for group_id in group_ids)))

_GROUP_SETTING_FIELDS = {
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "tts_mode",
    "join_verification_enabled",
    "join_verification_provider",
    "welcome_message",
    "welcome_buttons",
    "welcome_disable_link_preview",
    GROUP_PERMISSIONS_SETTINGS_KEY,
    "patrol_enabled",
    "raid_guard_enabled",
    "raid_guard_pin_message",
    "raid_guard_join_threshold",
    "raid_guard_window_seconds",
    "raid_guard_lockdown_seconds",
    "raid_guard_lookback_seconds",
    "raid_guard_challenge_timeout_seconds",
    "call_admin_enabled",
    "call_admin_pin_message",
    "call_admin_targets",
    "vote_ban_enabled",
    "vote_ban_pin_message",
    "vote_ban_threshold",
    "vote_ban_duration_seconds",
    "vote_ban_trigger_limit",
    "vote_ban_trigger_window_seconds",
    "proactive_enabled",
    "proactive_task_brief",
    "mimic_target_user_id",
    "mimic_target_user_name",
    "mimic_profile_text",
    "api_model_query_enabled",
    "api_model_query_base_url",
    "api_model_query_http_timeout_sec",
    "api_model_query_check_timeout_sec",
}
# Per-group raid-guard numeric overrides: key -> (min, max); null = inherit.
_RAID_GUARD_GROUP_INT_FIELDS = {
    "raid_guard_join_threshold": (2, 1000),
    "raid_guard_window_seconds": (5, 3600),
    "raid_guard_lockdown_seconds": (60, 86400),
    "raid_guard_lookback_seconds": (0, 86400),
    "raid_guard_challenge_timeout_seconds": (60, 86400),
}
# Per-group vote-ban numeric overrides, same null-inherits convention.
_VOTE_BAN_GROUP_INT_FIELDS = {
    "vote_ban_threshold": (2, 1000),
    "vote_ban_duration_seconds": (60, 86400),
    "vote_ban_trigger_limit": (1, 1000),
    "vote_ban_trigger_window_seconds": (60, 604800),
}
_SCHEDULED_TASKS_KEY = "scheduled_tasks"
_COOLDOWN_TASK_KEY = "cooldown_topic"
_GROUP_UPDATE_LOCKS: dict[int, asyncio.Lock] = {}
#: 每个群更新锁的当前持有/等待者计数。引用归零后立刻把锁从字典里摘掉，
#: 免得这个模块级字典随授权群数单调增长（A-10）。
_GROUP_UPDATE_LOCK_WAITERS: dict[int, int] = {}
_JSON_BODY_TIMEOUT_SECONDS = 5.0
#: D3-22 / P4-8：请求体大小上界。**默认值与今天完全一致**——``verify_web``
#: 建 app 时用的就是 ``client_max_size=_WEB_MAX_REQUEST_BYTES = 1 MiB``，这里只是把
#: 同一条上界在 handler 里显式再挡一次（并在超限时立刻拒绝，不去缓冲正文），
#: 同时把 aiohttp 抛的 ``HTTPRequestEntityTooLarge``（纯文本 413）换成与其它接口
#: 一致的 JSON 错误信封。真正的「流式」上界由 aiohttp 的 ``client_max_size`` 兜底
#: （没有 Content-Length 的分块请求由它拦住）。
_JSON_BODY_MAX_BYTES = 1024 * 1024
_JSON_BODY_CANCEL_GRACE_SECONDS = 0.1
_JSON_BODY_ORPHAN_LIMIT = 32
_JSON_BODY_ORPHANS: set[asyncio.Task[Any]] = set()
_JSON_BODY_TASKS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_LOOKUP_TIMEOUT_SECONDS = 5.0


def member_identity_limits() -> dict[str, float | int]:
    """成员身份解析的上游预算与缓存容量（现取配置）。

    **缓存 TTL 不在这里**：``_MEMBER_IDENTITY_CACHE_TTL_SECONDS`` 与
    ``_NEGATIVE_CACHE_TTL_SECONDS`` 属鉴权邻接，只允许收紧、不得放大，保持常量。
    """

    resources = policy_runtime.resources_policy()
    return {
        "lookup_timeout_seconds": resources.member_identity_lookup_timeout_seconds,
        "lookup_concurrency": resources.member_identity_lookup_concurrency,
        "cache_max_entries": resources.member_identity_cache_max_entries,
    }
_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS = 0.2
_MEMBER_IDENTITY_REQUEST_LOOKUP_LIMIT = 8
_MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS = 4.0
_MEMBER_IDENTITY_CACHE_TTL_SECONDS = 300.0
_MEMBER_IDENTITY_NEGATIVE_CACHE_TTL_SECONDS = 30.0
_MEMBER_IDENTITY_CACHE_MAX_ENTRIES = 4096
_MEMBER_IDENTITY_LOOKUP_CONCURRENCY = 8
_MEMBER_IDENTITY_RPC_TASK_LIMIT = 32
_MEMBER_IDENTITY_ORPHAN_LIMIT = 8
_MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS = 10.0
_MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = 0.0
_MEMBER_IDENTITY_CACHE: dict[
    tuple[int, int, int],
    tuple[float, tuple[str, str, bool] | None],
] = {}
_MEMBER_IDENTITY_INFLIGHT: dict[
    tuple[int, int, int],
    asyncio.Task[tuple[str, str, bool] | None],
] = {}
_MEMBER_IDENTITY_ORPHANS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_RPC_TASKS: set[asyncio.Task[Any]] = set()
_MEMBER_IDENTITY_LOOKUP_LOOP: asyncio.AbstractEventLoop | None = None
_MEMBER_IDENTITY_LOOKUP_SEMAPHORE: asyncio.Semaphore | None = None

#: D3-18：群管理员授权的 Telegram 侧交叉校验缓存。
#: ``(group_id, user_id) -> (过期时刻, 是否仍是管理员)``。只在「问到了 Telegram」
#: 时才写入；问不到（超时/限流/异常）不写，下个请求重试。
_ADMIN_REVALIDATION_TTL_SECONDS = 300.0
_ADMIN_REVALIDATION_CACHE_MAX = 2048
_ADMIN_REVALIDATION_CACHE: dict[tuple[int, int], tuple[float, bool]] = {}


def _forget_admin_revalidation(group_id: int, user_id: int) -> None:
    """授权记录变更时立刻作废该 (群, 人) 的交叉校验结论（D3-18）。"""

    _ADMIN_REVALIDATION_CACHE.pop((int(group_id), int(user_id)), None)


class _RuntimeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictInt = Field(ge=1)
    config: dict[str, Any]
    secret_changes: dict[str, dict[str, str]] = Field(default_factory=dict)


class _RuntimeSettingsDiff(BaseModel):
    """``POST /api/v1/settings/diff`` 的请求体：只算差异，**不写库**。

    ``extra="forbid"`` 是刻意的：多塞一个 ``group_id`` 之类不该被静默忽略——
    悄悄忽略会让调用方以为"带上了"其实没带上。
    """

    model_config = ConfigDict(extra="forbid")

    revision: StrictInt = Field(ge=1)
    config: dict[str, Any]
    secret_changes: dict[str, dict[str, str]] = Field(default_factory=dict)


class _RuntimeSettingsRestore(BaseModel):
    """``POST /api/v1/settings/history/{revision}/restore`` 的请求体。

    ``revision`` 是**调用方看到的当前 revision**（乐观锁），路径上的那个是**被
    回滚到**的目标 revision——两个不同的东西，混用会让"回滚"变成"乱跳"。
    """

    model_config = ConfigDict(extra="forbid")

    revision: StrictInt = Field(ge=1)


class _GroupSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    av_enabled: StrictBool | None = None
    mute_all_replies: StrictBool | None = None
    at_reply_mode: StrictBool | None = None
    tts_mode: Literal["off", "on", "always"] | None = None
    # None clears the group override and inherits the global default.
    join_verification_enabled: StrictBool | None = None
    join_verification_provider: (
        Literal["turnstile", "hcaptcha", "turnstile_hcaptcha"] | None
    ) = None
    welcome_message: str | None = Field(default=None, max_length=4000)
    welcome_buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    welcome_disable_link_preview: StrictBool | None = None
    default_permissions: dict[str, Any] | None = None
    patrol_enabled: StrictBool | None = None
    raid_guard_enabled: StrictBool | None = None
    raid_guard_pin_message: StrictBool | None = None
    raid_guard_join_threshold: StrictInt | None = Field(default=None, ge=2, le=1000)
    raid_guard_window_seconds: StrictInt | None = Field(default=None, ge=5, le=3600)
    raid_guard_lockdown_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    raid_guard_lookback_seconds: StrictInt | None = Field(
        default=None, ge=0, le=86400
    )
    raid_guard_challenge_timeout_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    # None inherits the global default; targets [] or missing = all admins.
    call_admin_enabled: StrictBool | None = None
    call_admin_pin_message: StrictBool | None = None
    call_admin_targets: list[Annotated[StrictInt, Field(gt=0)]] | None = Field(
        default=None,
        max_length=100,
    )
    vote_ban_enabled: StrictBool | None = None
    vote_ban_pin_message: StrictBool | None = None
    vote_ban_threshold: StrictInt | None = Field(default=None, ge=2, le=1000)
    vote_ban_duration_seconds: StrictInt | None = Field(
        default=None, ge=60, le=86400
    )
    vote_ban_trigger_limit: StrictInt | None = Field(default=None, ge=1, le=1000)
    vote_ban_trigger_window_seconds: StrictInt | None = Field(
        default=None, ge=60, le=604800
    )
    proactive_enabled: StrictBool | None = None
    proactive_task_brief: str | None = Field(default=None, max_length=240)
    mimic_target_user_id: StrictInt | None = Field(default=None, ge=0)
    mimic_target_user_name: str | None = Field(default=None, max_length=80)
    mimic_profile_text: str | None = Field(default=None, max_length=1200)
    api_model_query_enabled: StrictBool | None = None
    api_model_query_base_url: str | None = Field(default=None, max_length=1000)
    api_model_query_http_timeout_sec: StrictFloat | StrictInt | None = Field(
        default=None, ge=1.0, le=300.0
    )
    api_model_query_check_timeout_sec: StrictFloat | StrictInt | None = Field(
        default=None, ge=1.0, le=600.0
    )


class _GroupApiModelQuerySecretChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["replace", "clear"]
    value: str = Field(default="", max_length=1024)


class _RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["keyword", "regex", "llm"]
    pattern: str = Field(min_length=1, max_length=1000)
    action: Literal["warn", "delete", "ban"] = "warn"
    enabled: StrictBool = True
    # 正则/关键词规则的扫描范围（语义规则忽略该字段）：
    # message（默认，只扫用户正文）/ +quote（并入被引用正文）/
    # +vision（并入机器人图片描述）/ 三者可组合。
    scan_scope: Literal[
        "message",
        "message+quote",
        "message+vision",
        "message+quote+vision",
    ] = "message"


class _KeywordReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=255)
    match_type: Literal["contains", "exact", "regex"] = "contains"
    reply_text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    pin_message: StrictBool = False
    auto_delete: StrictBool = True
    disable_link_preview: StrictBool = True
    enabled: StrictBool = True


class _KeywordReplyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str | None = Field(default=None, min_length=1, max_length=255)
    match_type: Literal["contains", "exact", "regex"] | None = None
    reply_text: str | None = Field(default=None, min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    pin_message: StrictBool | None = None
    auto_delete: StrictBool | None = None
    disable_link_preview: StrictBool | None = None
    enabled: StrictBool | None = None


class _ScheduledMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    schedule_type: Literal["daily", "interval"] = "daily"
    schedule_time: str = Field(default="09:00", max_length=5)
    interval_minutes: StrictInt = Field(default=60, ge=5, le=10080)
    pin_message: StrictBool = False
    unpin_previous: StrictBool = False
    auto_delete: StrictBool = False
    disable_link_preview: StrictBool = True
    enabled: StrictBool = True


class _ScheduledMessageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    buttons: list[dict[str, Any]] | None = Field(default=None, max_length=12)
    schedule_type: Literal["daily", "interval"] | None = None
    schedule_time: str | None = Field(default=None, max_length=5)
    interval_minutes: StrictInt | None = Field(default=None, ge=5, le=10080)
    pin_message: StrictBool | None = None
    unpin_previous: StrictBool | None = None
    auto_delete: StrictBool | None = None
    disable_link_preview: StrictBool | None = None
    enabled: StrictBool | None = None


class _RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["keyword", "regex", "llm"] | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=1000)
    action: Literal["warn", "delete", "ban"] | None = None
    enabled: StrictBool | None = None
    scan_scope: (
        Literal[
            "message",
            "message+quote",
            "message+vision",
            "message+quote+vision",
        ]
        | None
    ) = None


class _MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)


class _UserIdCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt = Field(ge=1)


class _AuthorizedGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: StrictInt
    title: str = Field(default="", max_length=255)


class _AdminCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt
    role: str = Field(default="admin", min_length=1, max_length=32)


class _GlobalBanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictInt
    reason: str = Field(default="", max_length=1000)


class _APIError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        #: 逐字段的校验细节（只放**用户输入 + schema**产生的信息，不放内部状态）。
        self.details = details


def _error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: Any | None = None,
) -> web.Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return web.json_response(
        {"ok": False, "error": error},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _success_response(payload: dict[str, Any]) -> web.Response:
    return web.json_response(
        {"ok": True, **payload},
        headers={"Cache-Control": "no-store"},
    )


def _group_scoped_delete_response(
    outcome: Any,
    *,
    not_found: tuple[str, str],
    log_label: str,
    group_id: int,
    row_id: int,
) -> web.Response:
    """把一个本群行删除结果映射成 HTTP 响应。

    ``deleted`` 只在本事务真的删掉了那一行时才为 ``true``；预读之后被别人删掉的
    情况是 ``no_change``，而不是又一次「删除成功」。结果不可确认时既不说成功也不
    说没删除，只给 503 未确认。
    """

    if outcome.status == GROUP_ROW_NOT_FOUND:
        raise _APIError(404, not_found[0], not_found[1])
    if outcome.status == GROUP_ROW_UNCONFIRMED:
        log.warning(
            "%s unconfirmed | group_id=%s | row_id=%s | reason=%s",
            log_label,
            group_id,
            row_id,
            getattr(outcome, "detail", "") or "unknown",
        )
        raise _APIError(503, "memory_write_unconfirmed", PM_WRITE_UNCONFIRMED_MESSAGE)
    if outcome.status == GROUP_ROW_DELETED:
        return _success_response(
            {"deleted": True, "changed": True, "status": GROUP_ROW_DELETED}
        )
    return _success_response(
        {"deleted": False, "changed": False, "status": GROUP_ROW_NO_CHANGE}
    )


def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return json.loads(exc.json(include_url=False))


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


@asynccontextmanager
async def _group_update_guard(group_id: int) -> AsyncIterator[asyncio.Lock]:
    """Serialize group-settings writes per group without leaking lock objects.

    The lock table is module level, so a plain ``setdefault`` grows it forever
    (one entry per group ever touched, never released on authorization
    removal).  Entries are dropped as soon as the last holder/waiter leaves.

    Deliberately **not** an LRU: evicting a lock that is still held would let
    two concurrent writers take two different locks and defeat the protection.
    """

    key = int(group_id)
    lock = _GROUP_UPDATE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _GROUP_UPDATE_LOCKS[key] = lock
    _GROUP_UPDATE_LOCK_WAITERS[key] = _GROUP_UPDATE_LOCK_WAITERS.get(key, 0) + 1
    try:
        async with lock:
            yield lock
    finally:
        remaining = _GROUP_UPDATE_LOCK_WAITERS.get(key, 0) - 1
        if remaining > 0:
            _GROUP_UPDATE_LOCK_WAITERS[key] = remaining
            return
        _GROUP_UPDATE_LOCK_WAITERS.pop(key, None)
        # Only drop the object this holder actually used; a newer entry may
        # already have replaced it.
        if _GROUP_UPDATE_LOCKS.get(key) is lock:
            _GROUP_UPDATE_LOCKS.pop(key, None)


def _track_json_body_orphan(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume_background_task(task)
        return
    _JSON_BODY_ORPHANS.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        _JSON_BODY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_finished)


async def _read_request_json(request: web.Request) -> Any:
    """Read one JSON body with a hard caller-visible deadline and a size bound.

    ``asyncio.wait_for`` can wait indefinitely for a coroutine that suppresses
    cancellation.  The request handler must still return 408 at the configured
    deadline; a misbehaving body reader is retired separately and bounded.

    ``Content-Length`` 在开读之前就被检查（D3-22）：超限直接 413，不会把正文读进
    进程内存；没有 Content-Length 的分块请求由 aiohttp 的 ``client_max_size``
    （同一条 1 MiB 上界）兜底。
    """

    declared = getattr(request, "content_length", None)
    if declared is not None and int(declared) > max(0, int(_JSON_BODY_MAX_BYTES)):
        raise _APIError(
            413,
            "request_too_large",
            "请求体过大，请缩小后再试。",
        )
    if len(_JSON_BODY_TASKS) >= _JSON_BODY_ORPHAN_LIMIT:
        raise _APIError(
            503,
            "request_body_overloaded",
            "请求体读取任务暂时过载，请稍后重试。",
        )
    task = asyncio.create_task(request.json(), name="settings-json-body")
    _JSON_BODY_TASKS.add(task)

    def _retire(done: asyncio.Task[Any]) -> None:
        _JSON_BODY_TASKS.discard(done)
        _JSON_BODY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_retire)
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(_JSON_BODY_TIMEOUT_SECONDS)),
        )
    except asyncio.CancelledError:
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(_JSON_BODY_CANCEL_GRACE_SECONDS)),
        )
        if task not in done:
            _track_json_body_orphan(task)
        else:
            _consume_background_task(task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=max(0.0, float(_JSON_BODY_CANCEL_GRACE_SECONDS)),
    )
    if task not in done:
        _track_json_body_orphan(task)
    else:
        _consume_background_task(task)
    raise _APIError(408, "request_timeout", "请求体读取超时，请重试。")


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await _read_request_json(request)
    except HTTPRequestEntityTooLarge as exc:
        # D3-22：aiohttp 的流式上界命中。换成与其它接口一致的 JSON 错误信封。
        raise _APIError(
            413,
            "request_too_large",
            "请求体过大，请缩小后再试。",
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _APIError(400, "invalid_json", "请求体必须是有效的 JSON。") from exc
    if not isinstance(body, dict):
        raise _APIError(400, "invalid_body", "请求体必须是 JSON 对象。")
    return body


def _setting_bool(settings_data: dict[str, Any], key: str) -> bool:
    value = settings_data.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
    return bool(value)


def _setting_int(settings_data: dict[str, Any], key: str) -> int | None:
    value = settings_data.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _public_group_settings(settings_data: dict[str, Any]) -> dict[str, Any]:
    proactive_state = get_cooldown_task_state(settings_data)
    style_state = get_style_state(settings_data)
    api_model_query = get_api_model_query_config(settings_data)
    try:
        welcome_buttons = normalize_template_buttons(
            settings_data.get("welcome_buttons")
        )
    except ValueError:
        welcome_buttons = []
    try:
        default_permissions = (
            normalize_group_permission_config(
                settings_data.get(GROUP_PERMISSIONS_SETTINGS_KEY)
            )
            if settings_data.get(GROUP_PERMISSIONS_SETTINGS_KEY) is not None
            else None
        )
    except ValueError:
        default_permissions = None
    return {
        "av_enabled": _setting_bool(settings_data, "av_enabled"),
        "mute_all_replies": _setting_bool(settings_data, "mute_all_replies"),
        "at_reply_mode": is_at_reply_enabled(settings_data),
        "tts_mode": normalize_tts_mode(settings_data),
        "join_verification_enabled": (
            _setting_bool(settings_data, "join_verification_enabled")
            if settings_data.get("join_verification_enabled") is not None
            else None
        ),
        "join_verification_provider": (
            str(settings_data["join_verification_provider"]).strip().lower()
            if str(settings_data.get("join_verification_provider") or "").strip().lower()
            in {"turnstile", "hcaptcha", "turnstile_hcaptcha"}
            else None
        ),
        "welcome_message": str(settings_data.get("welcome_message") or ""),
        "welcome_buttons": welcome_buttons,
        "welcome_disable_link_preview": (
            True
            if settings_data.get("welcome_disable_link_preview") is None
            else _setting_bool(settings_data, "welcome_disable_link_preview")
        ),
        GROUP_PERMISSIONS_SETTINGS_KEY: default_permissions,
        "patrol_enabled": (
            _setting_bool(settings_data, "patrol_enabled")
            if settings_data.get("patrol_enabled") is not None
            else None
        ),
        "raid_guard_enabled": (
            _setting_bool(settings_data, "raid_guard_enabled")
            if settings_data.get("raid_guard_enabled") is not None
            else None
        ),
        "raid_guard_pin_message": (
            _setting_bool(settings_data, "raid_guard_pin_message")
            if settings_data.get("raid_guard_pin_message") is not None
            else None
        ),
        **{
            key: _setting_int(settings_data, key)
            for key in _RAID_GUARD_GROUP_INT_FIELDS
        },
        "call_admin_enabled": (
            _setting_bool(settings_data, "call_admin_enabled")
            if settings_data.get("call_admin_enabled") is not None
            else None
        ),
        "call_admin_pin_message": (
            _setting_bool(settings_data, "call_admin_pin_message")
            if settings_data.get("call_admin_pin_message") is not None
            else None
        ),
        "call_admin_targets": sorted({
            int(item)
            for item in (settings_data.get("call_admin_targets") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        }),
        "vote_ban_enabled": (
            _setting_bool(settings_data, "vote_ban_enabled")
            if settings_data.get("vote_ban_enabled") is not None
            else None
        ),
        "vote_ban_pin_message": (
            _setting_bool(settings_data, "vote_ban_pin_message")
            if settings_data.get("vote_ban_pin_message") is not None
            else None
        ),
        **{
            key: _setting_int(settings_data, key)
            for key in _VOTE_BAN_GROUP_INT_FIELDS
        },
        # None means the group inherits the global default.
        "proactive_enabled": (
            bool(proactive_state.get("enabled"))
            if "enabled" in proactive_state
            else None
        ),
        "proactive_task_brief": str(proactive_state.get("task_brief") or "").strip(),
        "mimic_target_user_id": int(style_state.get("target_user_id") or 0),
        "mimic_target_user_name": str(style_state.get("target_user_name") or ""),
        "mimic_profile_text": str(style_state.get("profile_text") or ""),
        "mimic_sample_count": int(style_state.get("sample_count") or 0),
        "mimic_distilled_at_count": int(style_state.get("distilled_at_count") or 0),
        "api_model_query_enabled": api_model_query.enabled,
        "api_model_query_base_url": api_model_query.base_url,
        "api_model_query_http_timeout_sec": api_model_query.http_timeout_sec,
        "api_model_query_check_timeout_sec": api_model_query.check_timeout_sec,
        "api_model_query_api_key_configured": api_model_query.api_key_configured,
    }


def _group_revision(settings_data: dict[str, Any]) -> str:
    public = _public_group_settings(settings_data)
    editable = {
        key: public.get(key)
        for key in sorted(_GROUP_SETTING_FIELDS)
    }
    # The key itself is never returned, but replacing it must still invalidate
    # an already-open Mini App revision so concurrent saves cannot race.
    editable["_api_model_query_secret_version"] = get_api_model_query_config(
        settings_data
    ).secret_version
    encoded = json.dumps(
        editable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _group_document(
    group_id: int,
    title: str | None,
    settings_data: dict[str, Any] | None,
) -> dict[str, Any]:
    stored = settings_data if isinstance(settings_data, dict) else {}
    return {
        "id": int(group_id),
        "title": str(title or ""),
        "settings": _public_group_settings(stored),
        "revision": _group_revision(stored),
    }


def _rule_document(rule: ModerationRule) -> dict[str, Any]:
    return {
        "id": int(rule.id),
        "rule_type": str(rule.rule_type or "keyword"),
        "pattern": str(rule.pattern or ""),
        "action": str(rule.action or "warn"),
        "enabled": bool(rule.enabled),
        # 老规则/老数据没有该列时按默认 message 返回，Mini App 读到的永远是合法值。
        "scan_scope": normalize_scan_scope(getattr(rule, "scan_scope", None)),
    }


def _replay_sample_document(
    row: ModerationReplaySample, *, now: datetime | None = None
) -> dict[str, Any]:
    """回放样本的对外文档。

    只回**已脱敏**的正文（写入时已抹掉 Telegram id / @username / 电话号码），
    不带 user_id、message_id、群标题等任何 Telegram 标识。``expired`` 让前端
    明确区分"没样本"和"样本都在留存期外"。

    标注三件套分工明确：

    * ``gold_label`` / ``label_state`` —— **人工金标**（只有 ``labelled`` 才
      参与 FP/FN）；
    * ``label_source`` / ``label_reviewed_by`` / ``label_reviewed_at`` ——
      答案键的来源（谁、什么时候）；
    * ``provenance`` —— **机器侧信号**（导入来源的 ``review_state``、置信度、
      机器建议的标签）。它在界面上只作 provenance 展示，**任何统计都不读它**。
    """

    moment = now or now_shanghai_naive()
    expires_at = getattr(row, "expires_at", None)
    reviewed_at = getattr(row, "label_reviewed_at", None)
    return {
        "id": int(row.id),
        "group_id": int(row.group_id),
        "source_kind": str(row.source_kind or ""),
        "source_violation_id": (
            None if row.source_violation_id is None else int(row.source_violation_id)
        ),
        "redacted_text": str(row.redacted_text or ""),
        "redacted_context": str(row.redacted_context or ""),
        "gold_label": str(row.gold_label) if row.gold_label else None,
        "label_state": str(getattr(row, "label_state", "") or "unlabelled"),
        "label_source": str(row.label_source or ""),
        "label_reviewed_by": int(getattr(row, "label_reviewed_by", 0) or 0),
        "label_reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        "provenance": dict(getattr(row, "provenance", None) or {}),
        "created_by": int(row.created_by or 0),
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": bool(expires_at is not None and expires_at <= moment),
    }


def _replay_snapshot_for_audience(
    run: ModerationReplayRun, *, can_manage_global: bool
) -> dict[str, Any]:
    """按**角色矩阵**裁剪基线快照。

    回放是"按群授权"的：样本、规则、统计都属于本群，群管理员当然能看。但
    ``baseline_snapshot.prompt`` 是**全局**审核提示词 —— 那是超管级配置，可能
    含私有运维设定（第三方服务约定、内部流程、话术）。把它塞进一个"按群授权"的
    响应里，等于让任意一个群管理员读到全局配置。

    所以：没有全局权限时，``prompt`` 一律置空，只留 12 位摘要
    （够证明"基线没变"，不足以还原任何内容），并用 ``prompt_visible=False`` 明确
    告诉前端"这不是空，是不给看"。规则列表（本群的）是照常给的。

    有全局权限时原样返回，并带 ``prompt_visible=True``。
    """

    snapshot = dict(run.baseline_snapshot or {})
    if can_manage_global:
        snapshot["prompt_visible"] = True
        return snapshot
    digest = hashlib.sha256(str(snapshot.get("prompt") or "").encode("utf-8")).hexdigest()
    return {
        "group_id": snapshot.get("group_id"),
        "captured_at": snapshot.get("captured_at"),
        "rules": list(snapshot.get("rules") or []),
        "prompt": "",
        "prompt_visible": False,
        "prompt_digest": digest[:12],
    }


def _replay_run_document(row: ModerationReplayRun) -> dict[str, Any]:
    stats = dict(row.stats or {})
    return {
        "id": int(row.id),
        "group_id": int(row.group_id),
        "status": str(row.status or ""),
        "baseline_version": str(row.baseline_version or ""),
        "baseline_digest": str(row.baseline_digest or ""),
        "candidate_version": str(row.candidate_version or ""),
        "candidate_digest": str(row.candidate_digest or ""),
        # 建 run 那一刻冻结的模型绑定（**非密钥**）。结果因此能说清"是谁跑的"。
        "model_digest": str(getattr(row, "model_digest", "") or ""),
        "model_binding": dict(getattr(row, "model_binding", None) or {}),
        # 保真度边界：样本已脱敏、无原始上下文、**不等价于生产回放**。
        "fidelity": dict(getattr(row, "fidelity", None) or {}),
        "cancel_requested": bool(getattr(row, "cancel_requested", False)),
        "attempt": int(getattr(row, "attempt", 0) or 0),
        "created_by": int(row.created_by or 0),
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        # 失败原因只落稳定的错误类型名，不含 provider 原文。
        "error": str(row.error or ""),
        "idempotency_key": str(row.idempotency_key or ""),
        "stats": stats,
        "changed_count": int(stats.get("changed_count") or 0),
        # ``stats.total`` 的扁平别名：样本池可能有几百条，而单次执行只评估了其中
        # 一部分，前端需要一眼看出"这次到底算了几条"。
        "evaluated": int(stats.get("total") or 0),
        "results_purged": int(stats.get("results_purged") or 0),
    }


def _replay_bounds_document(
    *, sample_pool: int, document: dict[str, Any], sample_limit: int | None = None
) -> dict[str, Any]:
    """执行的界：让管理员分得清"全评完了"和"提前停了"。

    ``status`` / ``error`` / ``stats.total`` 都能表达这件事，但都要前端自己推：
    ``status="completed"`` 既可能是"25 条全评完了"，也可能是"样本池只有 25 条"。
    所以这里把三件事直接算好给它：样本池多大、单次上限多少、实际评了几条。

    * ``truncated`` —— 撞到了条数上限（池子比上限大，且确实评满了上限条数）；
    * ``stopped_early`` —— 没有评满：墙钟预算用尽、取消、或 worker 中断；
    * ``deadline_exceeded`` —— 撞到了墙钟预算（``error`` 是 deadline 码），
      **已算出的结果行保留**；
    * ``complete`` —— 正常评完（没有提前停）。

    上界常量从 ``moderation_replay`` **在调用时**读（不是 import 绑定）：改配置、
    以及测试打桩走的是同一条路。
    """

    limit = int(
        sample_limit
        if sample_limit is not None
        else moderation_replay.REPLAY_INLINE_SAMPLE_LIMIT
    )
    evaluated = int(document.get("evaluated") or 0)
    run_error = str(document.get("error") or "")
    deadline_exceeded = run_error in (
        moderation_replay.RUN_ERROR_DEADLINE,
        moderation_replay.RUN_ERROR_INLINE_DEADLINE,
        "inline_deadline_exceeded",
    )
    cancelled = str(document.get("status") or "") == moderation_replay.RUN_CANCELLED
    interrupted = str(document.get("status") or "") == moderation_replay.RUN_INTERRUPTED
    return {
        "sample_pool": int(sample_pool),
        "sample_limit": limit,
        "deadline_seconds": float(moderation_replay.REPLAY_RUN_DEADLINE_SECONDS),
        "evaluated": evaluated,
        "truncated": bool(
            int(sample_pool) > limit and evaluated >= limit
        ),
        "deadline_exceeded": bool(deadline_exceeded),
        "stopped_early": bool(
            cancelled or interrupted or deadline_exceeded or evaluated < int(sample_pool)
        ),
        "cancelled": bool(cancelled),
        "interrupted": bool(interrupted),
        "complete": bool(str(document.get("status") or "") == "completed"),
    }


def _replay_result_document(
    row: ModerationReplayResult, sample: ModerationReplaySample | None
) -> dict[str, Any]:
    def side(prefix: str) -> dict[str, Any]:
        return {
            "verdict": str(getattr(row, f"{prefix}_verdict", "") or ""),
            "conclusive": bool(getattr(row, f"{prefix}_conclusive", False)),
            "confidence": getattr(row, f"{prefix}_confidence", None),
            "match_source": str(getattr(row, f"{prefix}_match_source", "") or ""),
            "latency_ms": getattr(row, f"{prefix}_latency_ms", None),
            "error": str(getattr(row, f"{prefix}_error", "") or ""),
            "usage": dict(getattr(row, f"{prefix}_usage", None) or {}),
        }

    document: dict[str, Any] = {
        "id": int(row.id),
        "run_id": int(row.run_id),
        "group_id": int(row.group_id),
        "sample_id": int(row.sample_id),
        "baseline": side("baseline"),
        "candidate": side("candidate"),
        "changed": bool(row.changed),
    }
    if sample is not None:
        document["sample"] = {
            "id": int(sample.id),
            "source_kind": str(sample.source_kind or ""),
            # 金标只有人工确认过才是非空；``label_state`` 让前端不必靠猜。
            "gold_label": str(sample.gold_label) if sample.gold_label else None,
            "label_state": str(getattr(sample, "label_state", "") or "unlabelled"),
            "label_source": str(sample.label_source or ""),
            # 机器侧信号（导入来源的 review_state / 置信度 / 机器建议标签）。
            # **明确不是答案键**：任何 FP/FN 都不读它。
            "provenance": dict(getattr(sample, "provenance", None) or {}),
            "redacted_text": str(sample.redacted_text or ""),
        }
    return document


def _keyword_reply_document(row: KeywordReply) -> dict[str, Any]:
    try:
        buttons = normalize_template_buttons(getattr(row, "buttons", None))
    except ValueError:
        buttons = []
    return {
        "id": int(row.id),
        "keyword": str(row.keyword or ""),
        "match_type": str(row.match_type or "contains"),
        "reply_text": str(row.reply_text or ""),
        "buttons": buttons,
        "pin_message": bool(row.pin_message),
        "auto_delete": bool(row.auto_delete),
        "disable_link_preview": (
            True
            if getattr(row, "disable_link_preview", None) is None
            else bool(getattr(row, "disable_link_preview"))
        ),
        "enabled": bool(row.enabled),
    }


def _scheduled_message_document(row: ScheduledMessage) -> dict[str, Any]:
    try:
        buttons = normalize_template_buttons(getattr(row, "buttons", None))
    except ValueError:
        buttons = []
    return {
        "id": int(row.id),
        "text": str(row.text or ""),
        "buttons": buttons,
        "schedule_type": str(row.schedule_type or "daily"),
        "schedule_time": str(row.schedule_time or "09:00"),
        "interval_minutes": int(row.interval_minutes or 60),
        "pin_message": bool(row.pin_message),
        "unpin_previous": bool(row.unpin_previous),
        "auto_delete": bool(row.auto_delete),
        "disable_link_preview": (
            True
            if getattr(row, "disable_link_preview", None) is None
            else bool(getattr(row, "disable_link_preview"))
        ),
        "enabled": bool(row.enabled),
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else "",
    }


def _validated_keyword(keyword: str, match_type: str) -> str:
    cleaned = clean_text(keyword, max_len=255).strip()
    if not cleaned:
        raise _APIError(400, "empty_keyword", "关键词不能为空。")
    if match_type == "regex":
        try:
            re.compile(cleaned)
        except re.error as exc:
            raise _APIError(400, "invalid_keyword_regex", f"正则表达式无效：{exc}") from exc
    return cleaned


def _validated_schedule_time(raw: str) -> str:
    parsed = parse_schedule_time(raw)
    if parsed is None:
        raise _APIError(
            400,
            "invalid_schedule_time",
            "发送时间必须是 HH:MM 格式（例如 09:00）。",
        )
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


def _validated_template_buttons(value: object) -> list[dict[str, Any]]:
    try:
        return normalize_template_buttons(value)
    except ValueError as exc:
        raise _APIError(400, "invalid_template_buttons", str(exc)) from exc


def _memory_document(memory: GroupPermanentMemory) -> dict[str, Any]:
    return {
        "id": int(memory.id),
        "content": str(memory.content or ""),
        "created_by": int(memory.created_by or 0),
        "created_at": memory.created_at.isoformat() if memory.created_at else "",
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else "",
    }


def _member_identity(member: GroupMember | None, user_id: int) -> dict[str, Any]:
    full_name = str(getattr(member, "full_name", "") or "").strip()
    username = str(getattr(member, "username", "") or "").strip().lstrip("@")
    identity = member_identity_document(
        user_id,
        full_name=full_name,
        username=username,
    )
    # Keep an explicit human label even when Telegram no longer exposes the
    # profile; the stable ID remains a separate field for operations.
    if not full_name and not username:
        identity["display_name"] = f"用户 {user_id}"
    return identity


def _member_identity_lookup_semaphore() -> asyncio.Semaphore:
    """Return a bounded semaphore tied to the active aiohttp event loop."""
    global _MEMBER_IDENTITY_LOOKUP_LOOP, _MEMBER_IDENTITY_LOOKUP_SEMAPHORE
    loop = asyncio.get_running_loop()
    if (
        _MEMBER_IDENTITY_LOOKUP_LOOP is not loop
        or _MEMBER_IDENTITY_LOOKUP_SEMAPHORE is None
    ):
        # Tests and graceful restarts may create a new event loop in the same
        # process. Never reuse an asyncio primitive or in-flight Task across
        # loops; completed value-cache entries remain safe.
        _MEMBER_IDENTITY_LOOKUP_LOOP = loop
        _MEMBER_IDENTITY_LOOKUP_SEMAPHORE = asyncio.Semaphore(
            member_identity_limits()["lookup_concurrency"]
        )
        _MEMBER_IDENTITY_INFLIGHT.clear()
    return _MEMBER_IDENTITY_LOOKUP_SEMAPHORE


def _cache_member_identity(
    key: tuple[int, int, int],
    value: tuple[str, str, bool] | None,
) -> None:
    now = time.monotonic()
    ttl = (
        _MEMBER_IDENTITY_CACHE_TTL_SECONDS
        if value is not None
        else _MEMBER_IDENTITY_NEGATIVE_CACHE_TTL_SECONDS
    )
    _MEMBER_IDENTITY_CACHE[key] = (now + ttl, value)
    if len(_MEMBER_IDENTITY_CACHE) <= member_identity_limits()["cache_max_entries"]:
        return
    for cached_key, (expires_at, _cached_value) in list(
        _MEMBER_IDENTITY_CACHE.items()
    ):
        if expires_at <= now:
            _MEMBER_IDENTITY_CACHE.pop(cached_key, None)
    while len(_MEMBER_IDENTITY_CACHE) > member_identity_limits()["cache_max_entries"]:
        _MEMBER_IDENTITY_CACHE.pop(next(iter(_MEMBER_IDENTITY_CACHE)), None)


def _track_member_identity_orphan(task: asyncio.Task[Any]) -> None:
    global _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL
    if task.done():
        _consume_background_task(task)
        return
    _MEMBER_IDENTITY_ORPHANS.add(task)
    if len(_MEMBER_IDENTITY_ORPHANS) >= _MEMBER_IDENTITY_ORPHAN_LIMIT:
        _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL = max(
            _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL,
            time.monotonic() + _MEMBER_IDENTITY_CIRCUIT_COOLDOWN_SECONDS,
        )
        log.error(
            "settings Telegram lookup circuit opened | orphan_count=%d",
            len(_MEMBER_IDENTITY_ORPHANS),
        )

    def _finished(done: asyncio.Task[Any]) -> None:
        _MEMBER_IDENTITY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_finished)


class _TelegramLookupOverloaded(RuntimeError):
    pass


def _member_identity_lookup_capacity_available() -> bool:
    for task in tuple(_MEMBER_IDENTITY_RPC_TASKS):
        if task.done():
            _MEMBER_IDENTITY_RPC_TASKS.discard(task)
            _MEMBER_IDENTITY_ORPHANS.discard(task)
            _consume_background_task(task)
    if time.monotonic() < _MEMBER_IDENTITY_CIRCUIT_OPEN_UNTIL:
        return False
    if len(_MEMBER_IDENTITY_ORPHANS) >= _MEMBER_IDENTITY_ORPHAN_LIMIT:
        return False
    return len(_MEMBER_IDENTITY_RPC_TASKS) < _MEMBER_IDENTITY_RPC_TASK_LIMIT


async def _await_member_identity_lookup(
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """Run a Telegram lookup without releasing its permit before it stops."""

    if not _member_identity_lookup_capacity_available():
        raise _TelegramLookupOverloaded("settings Telegram lookup circuit is open")

    async def _run_with_permit() -> Any:
        # The permit belongs to the real Telegram operation.  If the SDK or a
        # test double suppresses cancellation, it stays held until that child
        # actually exits instead of admitting unbounded replacement calls.
        async with _member_identity_lookup_semaphore():
            return await operation()

    task = asyncio.create_task(_run_with_permit(), name="settings-telegram-lookup")
    _MEMBER_IDENTITY_RPC_TASKS.add(task)

    def _retire_rpc(done: asyncio.Task[Any]) -> None:
        _MEMBER_IDENTITY_RPC_TASKS.discard(done)
        _MEMBER_IDENTITY_ORPHANS.discard(done)
        _consume_background_task(done)

    task.add_done_callback(_retire_rpc)
    timeout = (
        member_identity_limits()["lookup_timeout_seconds"]
        if timeout_seconds is None
        else max(0.0, float(timeout_seconds))
    )
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=timeout,
        )
    except asyncio.CancelledError:
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS,
        )
        if task not in done:
            _track_member_identity_orphan(task)
        else:
            _consume_background_task(task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=_MEMBER_IDENTITY_CANCEL_GRACE_SECONDS,
    )
    if task not in done:
        _track_member_identity_orphan(task)
    else:
        _consume_background_task(task)
    raise TimeoutError


async def flush_member_identity_tasks(*, timeout_seconds: float = 2.0) -> None:
    """Bound settings-page Telegram lookups before the Bot session closes."""

    deadline = asyncio.get_running_loop().time() + max(
        0.0, float(timeout_seconds)
    )
    pending: set[asyncio.Task[Any]] = set()
    while True:
        tasks = {
            task
            for task in (
                *_MEMBER_IDENTITY_INFLIGHT.values(),
                *_MEMBER_IDENTITY_RPC_TASKS,
                *_MEMBER_IDENTITY_ORPHANS,
                *_JSON_BODY_TASKS,
                *_JSON_BODY_ORPHANS,
            )
            if not task.done()
        }
        if not tasks:
            pending = set()
            break
        for task in tasks:
            task.cancel()
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining <= 0.0:
            pending = tasks
            break
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in done:
            _consume_background_task(task)
        if pending or asyncio.get_running_loop().time() >= deadline:
            break
        # Cancelling an outer identity task can expose its cancellation-
        # resistant RPC child only after the first snapshot was taken.
        await asyncio.sleep(0)
    for task in pending:
        if task in _MEMBER_IDENTITY_RPC_TASKS:
            _track_member_identity_orphan(task)
        elif task in _JSON_BODY_ORPHANS:
            _track_json_body_orphan(task)
    if pending:
        log.error(
            "%d settings API background tasks ignored shutdown cancellation",
            len(pending),
        )


async def _lookup_member_identity(
    bot_obj: Any,
    group_id: int,
    user_id: int,
) -> tuple[str, str, bool] | None:
    """Deduplicate, bound and time-limit Telegram profile lookups."""
    key = (id(bot_obj), int(group_id), int(user_id))
    cached = _MEMBER_IDENTITY_CACHE.get(key)
    if cached is not None:
        if cached[0] > time.monotonic():
            return cached[1]
        _MEMBER_IDENTITY_CACHE.pop(key, None)

    task = _MEMBER_IDENTITY_INFLIGHT.get(key)
    if task is None:
        lookup = getattr(bot_obj, "get_chat_member", None)
        if not callable(lookup):
            return None
        if (
            len(_MEMBER_IDENTITY_INFLIGHT) >= _MEMBER_IDENTITY_RPC_TASK_LIMIT
            or not _member_identity_lookup_capacity_available()
        ):
            log.warning(
                "member identity lookup rejected by circuit | group=%s user=%s",
                group_id,
                user_id,
            )
            return None

        async def _fetch() -> tuple[str, str, bool] | None:
            profile_data: tuple[str, str, bool] | None = None
            try:
                chat_member = await _await_member_identity_lookup(
                    lambda: lookup(int(group_id), int(user_id)),
                )
                profile = getattr(chat_member, "user", None)
                if profile is not None:
                    full_name = str(getattr(profile, "full_name", "") or "")[:255]
                    username = str(getattr(profile, "username", "") or "")[:255]
                    if full_name or username:
                        profile_data = (
                            full_name,
                            username,
                            bool(getattr(profile, "is_bot", False)),
                        )
            except TimeoutError:
                log.debug(
                    "member identity lookup timed out | group=%s user=%s",
                    group_id,
                    user_id,
                )
            except _TelegramLookupOverloaded:
                log.warning(
                    "member identity lookup circuit open | group=%s user=%s",
                    group_id,
                    user_id,
                )
                return None
            except Exception:
                log.debug(
                    "member identity lookup failed | group=%s user=%s",
                    group_id,
                    user_id,
                    exc_info=True,
                )
            _cache_member_identity(key, profile_data)
            return profile_data

        task = asyncio.create_task(
            _fetch(),
            name=f"member-identity:{int(group_id)}:{int(user_id)}",
        )
        _MEMBER_IDENTITY_INFLIGHT[key] = task

        def _retire(done: asyncio.Task[tuple[str, str, bool] | None]) -> None:
            if _MEMBER_IDENTITY_INFLIGHT.get(key) is done:
                _MEMBER_IDENTITY_INFLIGHT.pop(key, None)

        task.add_done_callback(_retire)
    return await asyncio.shield(task)


async def _group_member_map(
    session_factory: async_sessionmaker[AsyncSession],
    group_id: int,
    user_ids: list[int],
    *,
    bot_obj: Any | None = None,
    persist: bool = True,
) -> dict[int, GroupMember]:
    """Resolve roster rows for ``user_ids``, enriching missing names from Telegram.

    ``persist=False`` keeps a read-only caller (the policy list endpoints) from
    opening a write transaction: the enrichment only labels that one response
    and is thrown away afterwards.  A GET request must not create rows, and on
    SQLite every write transaction competes with moderation writes for the lock.
    """
    ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    if not ids:
        return {}
    # Take a short DB snapshot first.  Live Telegram lookups can take seconds
    # and must never hold one of the application's scarce DB connections.
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(GroupMember).where(
                    GroupMember.group_id == int(group_id),
                    GroupMember.user_id.in_(ids),
                )
            )
        ).all()
    members = {int(row.user_id): row for row in rows}
    if bot_obj is None or not callable(getattr(bot_obj, "get_chat_member", None)):
        return members
    missing_ids = [
        user_id
        for user_id in ids
        if user_id not in members
        or not (
            str(getattr(members[user_id], "full_name", "") or "").strip()
            or str(getattr(members[user_id], "username", "") or "").strip()
        )
    ]
    if not missing_ids:
        return members

    # A policy table may contain thousands of historical users.  Only enrich a
    # small page per request and keep one absolute budget for the whole batch.
    lookup_ids = missing_ids[: max(0, int(_MEMBER_IDENTITY_REQUEST_LOOKUP_LIMIT))]
    lookup_tasks = {
        user_id: asyncio.create_task(
            _lookup_member_identity(bot_obj, int(group_id), user_id),
            name=f"settings-member-page:{int(group_id)}:{user_id}",
        )
        for user_id in lookup_ids
    }
    done: set[asyncio.Task[tuple[str, str, bool] | None]] = set()
    pending: set[asyncio.Task[tuple[str, str, bool] | None]] = set()
    if lookup_tasks:
        try:
            done, pending = await asyncio.wait(
                set(lookup_tasks.values()),
                timeout=max(0.0, float(_MEMBER_IDENTITY_REQUEST_BUDGET_SECONDS)),
            )
        except asyncio.CancelledError:
            for task in lookup_tasks.values():
                task.cancel()
            await asyncio.gather(*lookup_tasks.values(), return_exceptions=True)
            raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    profiles_by_id: dict[int, tuple[str, str, bool]] = {}
    for user_id, task in lookup_tasks.items():
        if task not in done or task.cancelled():
            continue
        try:
            profile_data = task.result()
        except Exception:
            continue
        if profile_data is not None:
            profiles_by_id[user_id] = profile_data
    if not profiles_by_id:
        return members

    if not persist:
        # Read-only caller: label this response without touching the database.
        # The rows here are detached (their session already closed) and are
        # never added to one, so the merge below cannot reach disk.
        for user_id, (full_name, username, is_bot) in profiles_by_id.items():
            row = members.get(user_id)
            if row is None:
                members[user_id] = GroupMember(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    full_name=full_name,
                    username=username,
                    is_bot=is_bot,
                    left=False,
                )
                continue
            if full_name and not str(row.full_name or "").strip():
                row.full_name = full_name
            if username and not str(row.username or "").strip():
                row.username = username
        return members

    # Open a fresh, short write transaction only after every network wait has
    # ended.  Re-read rows to merge safely with the roster middleware.
    async with session_factory() as session:
        current_rows = (
            await session.scalars(
                select(GroupMember).where(
                    GroupMember.group_id == int(group_id),
                    GroupMember.user_id.in_(ids),
                )
            )
        ).all()
        members = {int(row.user_id): row for row in current_rows}
        changed = False
        for user_id, profile_data in profiles_by_id.items():
            full_name, username, is_bot = profile_data
            row = members.get(user_id)
            if row is None:
                row = GroupMember(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    full_name=full_name,
                    username=username,
                    is_bot=is_bot,
                    left=False,
                )
                session.add(row)
                members[user_id] = row
                changed = True
                continue
            if full_name and not str(row.full_name or "").strip():
                row.full_name = full_name
                changed = True
            if username and not str(row.username or "").strip():
                row.username = username
                changed = True
            if bool(row.is_bot) != is_bot:
                row.is_bot = is_bot
                changed = True
        if not changed:
            return members
        try:
            await session.commit()
        except Exception:
            # Roster middleware or another settings request may have inserted
            # the same member concurrently. Identity display is best-effort;
            # a conflict must not fail the whole policy-list response.
            await session.rollback()
            current_rows = (
                await session.scalars(
                    select(GroupMember).where(
                        GroupMember.group_id == int(group_id),
                        GroupMember.user_id.in_(ids),
                    )
                )
            ).all()
            members = {int(row.user_id): row for row in current_rows}
        return members


def _user_policy_document(
    row: Any,
    member: GroupMember | None = None,
) -> dict[str, Any]:
    user_id = int(row.user_id)
    payload = {"user_id": user_id, **_member_identity(member, user_id)}
    if isinstance(row, UserWarning):
        payload.update({"count": int(row.count or 0), "is_banned": bool(row.is_banned)})
    if hasattr(row, "created_by"):
        payload["created_by"] = int(row.created_by or 0)
    if hasattr(row, "created_at"):
        payload["created_at"] = row.created_at.isoformat() if row.created_at else ""
    return payload


def _ban_document(
    row: UserWarning,
    member: GroupMember | None = None,
) -> dict[str, Any]:
    """Expose group-ban state without leaking global moderation thresholds."""
    user_id = int(row.user_id)
    return {
        "user_id": user_id,
        "is_banned": bool(row.is_banned),
        **_member_identity(member, user_id),
    }


async def _set_group_banned_after_telegram(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
) -> UserWarning:
    """Upsert only the ban flag, preserving concurrent warning increments."""
    updated = await session.execute(
        update(UserWarning)
        .where(
            UserWarning.group_id == int(group_id),
            UserWarning.user_id == int(user_id),
        )
        .values(is_banned=True)
    )
    if int(updated.rowcount or 0) == 0:
        try:
            async with session.begin_nested():
                session.add(
                    UserWarning(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        count=0,
                        is_banned=True,
                    )
                )
                await session.flush()
        except IntegrityError:
            updated = await session.execute(
                update(UserWarning)
                .where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                )
                .values(is_banned=True)
            )
            if int(updated.rowcount or 0) != 1:
                raise
    row = await session.scalar(
        select(UserWarning).where(
            UserWarning.group_id == int(group_id),
            UserWarning.user_id == int(user_id),
        )
    )
    if row is None:
        raise RuntimeError("group ban row was not persisted")
    return row


def _set_proactive_task_brief(
    settings_data: dict[str, Any],
    task_brief: str,
) -> dict[str, Any]:
    updated = dict(settings_data)
    raw_tasks = updated.get(_SCHEDULED_TASKS_KEY)
    tasks = dict(raw_tasks) if isinstance(raw_tasks, dict) else {}
    raw_state = tasks.get(_COOLDOWN_TASK_KEY)
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    if task_brief:
        state["task_brief"] = task_brief
    else:
        state.pop("task_brief", None)
    tasks[_COOLDOWN_TASK_KEY] = state
    updated[_SCHEDULED_TASKS_KEY] = tasks
    return updated


def _apply_group_settings(
    stored_settings: dict[str, Any] | None,
    update: _GroupSettingsUpdate,
    settings: Settings,
) -> dict[str, Any]:
    updated = dict(stored_settings or {})
    fields = update.model_fields_set
    null_fields = sorted(
        name
        for name in fields
        if name not in {
            "proactive_enabled",
            "join_verification_enabled",
            "join_verification_provider",
            "welcome_message",
            "welcome_buttons",
            "welcome_disable_link_preview",
            GROUP_PERMISSIONS_SETTINGS_KEY,
            "patrol_enabled",
            "raid_guard_enabled",
            "raid_guard_pin_message",
            *_RAID_GUARD_GROUP_INT_FIELDS,
            "call_admin_enabled",
            "call_admin_pin_message",
            "call_admin_targets",
            "vote_ban_enabled",
            "vote_ban_pin_message",
            *_VOTE_BAN_GROUP_INT_FIELDS,
        }
        and getattr(update, name) is None
    )
    if null_fields:
        raise _APIError(
            400,
            "invalid_group_settings",
            f"群设置不能为 null：{', '.join(null_fields)}",
        )

    if "av_enabled" in fields:
        updated["av_enabled"] = bool(update.av_enabled)
    if "mute_all_replies" in fields:
        if update.mute_all_replies:
            updated["mute_all_replies"] = True
        else:
            updated.pop("mute_all_replies", None)
    if "at_reply_mode" in fields:
        updated = set_at_reply_enabled(updated, bool(update.at_reply_mode))
    if "tts_mode" in fields:
        updated = set_tts_mode(updated, str(update.tts_mode))
    if "join_verification_enabled" in fields:
        if update.join_verification_enabled is None:
            updated.pop("join_verification_enabled", None)
        else:
            updated["join_verification_enabled"] = bool(update.join_verification_enabled)
    if "join_verification_provider" in fields:
        if update.join_verification_provider is None:
            updated.pop("join_verification_provider", None)
        else:
            updated["join_verification_provider"] = str(
                update.join_verification_provider
            )
    if "welcome_message" in fields:
        welcome_text = clean_multiline_text(
            str(update.welcome_message or ""), max_len=4000
        ).strip()
        if welcome_text:
            updated["welcome_message"] = welcome_text
        else:
            updated.pop("welcome_message", None)
    if "welcome_buttons" in fields:
        welcome_buttons = _validated_template_buttons(update.welcome_buttons or [])
        if welcome_buttons:
            updated["welcome_buttons"] = welcome_buttons
        else:
            updated.pop("welcome_buttons", None)
    if "welcome_disable_link_preview" in fields:
        if update.welcome_disable_link_preview is None:
            updated.pop("welcome_disable_link_preview", None)
        else:
            updated["welcome_disable_link_preview"] = bool(
                update.welcome_disable_link_preview
            )
    if GROUP_PERMISSIONS_SETTINGS_KEY in fields:
        if update.default_permissions is None:
            updated.pop(GROUP_PERMISSIONS_SETTINGS_KEY, None)
        else:
            try:
                updated[GROUP_PERMISSIONS_SETTINGS_KEY] = (
                    normalize_group_permission_config(update.default_permissions)
                )
            except ValueError as exc:
                raise _APIError(
                    400,
                    "invalid_default_permissions",
                    str(exc),
                ) from exc
    if fields & {"join_verification_enabled", "join_verification_provider"}:
        verification_enabled, provider = join_verification_policy(settings, updated)
        provider_selected = (
            "join_verification_provider" in fields
            and update.join_verification_provider is not None
        )
        if (
            verification_enabled or provider_selected
        ) and not turnstile_verification_configured(settings, provider):
            raise _APIError(
                400,
                "verification_provider_unavailable",
                "该验证服务尚未由最高管理员完成配置，暂时不能为本群启用。",
            )
    if "patrol_enabled" in fields:
        if update.patrol_enabled is None:
            updated.pop("patrol_enabled", None)
        else:
            if update.patrol_enabled and not verification_service_ready(
                settings, verification_provider(settings)
            ):
                raise _APIError(
                    400,
                    "verification_provider_unavailable",
                    "真人质询验证服务未配置，暂时不能启用巡检。",
                )
            updated["patrol_enabled"] = bool(update.patrol_enabled)
    if "raid_guard_enabled" in fields:
        if update.raid_guard_enabled is None:
            updated.pop("raid_guard_enabled", None)
        else:
            if update.raid_guard_enabled and not verification_service_ready(
                settings, verification_provider(settings)
            ):
                raise _APIError(
                    400,
                    "verification_provider_unavailable",
                    "真人质询验证服务未配置，暂时不能启用爆破防护。",
                )
            updated["raid_guard_enabled"] = bool(update.raid_guard_enabled)
    if "raid_guard_pin_message" in fields:
        if update.raid_guard_pin_message is None:
            updated.pop("raid_guard_pin_message", None)
        else:
            updated["raid_guard_pin_message"] = bool(update.raid_guard_pin_message)
    for key in _RAID_GUARD_GROUP_INT_FIELDS:
        if key not in fields:
            continue
        value = getattr(update, key)
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = int(value)
    if "call_admin_enabled" in fields:
        if update.call_admin_enabled is None:
            updated.pop("call_admin_enabled", None)
        else:
            updated["call_admin_enabled"] = bool(update.call_admin_enabled)
    if "call_admin_pin_message" in fields:
        if update.call_admin_pin_message is None:
            updated.pop("call_admin_pin_message", None)
        else:
            updated["call_admin_pin_message"] = bool(update.call_admin_pin_message)
    if "call_admin_targets" in fields:
        targets = sorted(
            {int(item) for item in (update.call_admin_targets or []) if int(item) > 0}
        )
        # An empty selection means "mention all admins" (the default), so it
        # is not persisted.
        if targets:
            updated["call_admin_targets"] = targets
        else:
            updated.pop("call_admin_targets", None)
    if "vote_ban_enabled" in fields:
        if update.vote_ban_enabled is None:
            updated.pop("vote_ban_enabled", None)
        else:
            updated["vote_ban_enabled"] = bool(update.vote_ban_enabled)
    if "vote_ban_pin_message" in fields:
        if update.vote_ban_pin_message is None:
            updated.pop("vote_ban_pin_message", None)
        else:
            updated["vote_ban_pin_message"] = bool(update.vote_ban_pin_message)
    for key in _VOTE_BAN_GROUP_INT_FIELDS:
        if key not in fields:
            continue
        value = getattr(update, key)
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = int(value)
    if "proactive_enabled" in fields:
        inherited = update.proactive_enabled is None
        updated = set_cooldown_task_enabled(
            updated,
            enabled=(
                bool(settings.bot.proactive_default_enabled)
                if inherited
                else bool(update.proactive_enabled)
            ),
            config=settings.bot,
        )
        if inherited:
            tasks = dict(updated.get(_SCHEDULED_TASKS_KEY) or {})
            state = dict(tasks.get(_COOLDOWN_TASK_KEY) or {})
            state.pop("enabled", None)
            tasks[_COOLDOWN_TASK_KEY] = state
            updated[_SCHEDULED_TASKS_KEY] = tasks
    if "proactive_task_brief" in fields:
        task_brief = str(update.proactive_task_brief or "").strip()
        updated = _set_proactive_task_brief(updated, task_brief)

    api_model_query_fields = {
        "api_model_query_enabled",
        "api_model_query_base_url",
        "api_model_query_http_timeout_sec",
        "api_model_query_check_timeout_sec",
    }
    if fields & api_model_query_fields:
        api_config = get_api_model_query_config(updated)
        try:
            if "api_model_query_enabled" in fields:
                api_config = replace(
                    api_config, enabled=bool(update.api_model_query_enabled)
                )
            if "api_model_query_base_url" in fields:
                api_config = replace(
                    api_config,
                    base_url=normalize_api_model_query_base_url(
                        update.api_model_query_base_url
                    ),
                )
            if "api_model_query_http_timeout_sec" in fields:
                api_config = replace(
                    api_config,
                    http_timeout_sec=float(update.api_model_query_http_timeout_sec),
                )
            if "api_model_query_check_timeout_sec" in fields:
                api_config = replace(
                    api_config,
                    check_timeout_sec=float(update.api_model_query_check_timeout_sec),
                )
            updated = set_api_model_query_config(updated, api_config)
        except (TypeError, ValueError) as exc:
            raise _APIError(
                400,
                "invalid_api_model_query_settings",
                str(exc) or "模型 API 查询配置无效。",
            ) from exc

    style_fields = {
        "mimic_target_user_id",
        "mimic_target_user_name",
        "mimic_profile_text",
    }
    if fields & style_fields:
        state = get_style_state(updated)
        previous_target = int(state.get("target_user_id") or 0)
        target_id = (
            int(update.mimic_target_user_id or 0)
            if "mimic_target_user_id" in fields
            else previous_target
        )
        target_changed = target_id != previous_target
        target_name = (
            clean_text(str(update.mimic_target_user_name or ""), max_len=80)
            if "mimic_target_user_name" in fields
            else "" if target_changed else str(state.get("target_user_name") or "")
        )
        if target_changed:
            updated = set_style_target(
                updated,
                user_id=target_id,
                user_name=target_name,
            )
            state = get_style_state(updated)
        else:
            state["target_user_name"] = target_name
        if "mimic_profile_text" in fields:
            state["profile_text"] = clean_multiline_text(
                str(update.mimic_profile_text or ""),
                max_len=1200,
            ).strip()
        updated = dict(updated)
        updated[STYLE_SETTINGS_KEY] = state
    return updated


def _path_int(
    request: web.Request,
    key: str,
    *,
    code: str = "invalid_user_id",
    message: str = "用户 ID 无效。",
) -> int:
    """把 ``{id}`` 这类路径参数解析成 int，非法/缺失一律 400（D3-20 / A-24）。

    改前多个 handler 直接对 ``request.match_info`` 做裸 ``int()``，非数字会让
    ``ValueError`` 冒到装饰器的兜底 ``except Exception`` → 500 "internal_error"：
    明明是客户端把 URL 打错了，却回一个「服务器炸了」，既误导管理员，也让
    真正的 5xx 混在这条噪声里。这里是全文件**唯一**的路径整数解析入口。
    """

    try:
        return int(request.match_info[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise _APIError(400, code, message) from exc


def _group_id(request: web.Request) -> int:
    return _path_int(
        request, "id", code="invalid_group_id", message="群 ID 无效。"
    )


def _onboarding_error_response(exc: Exception) -> web.Response:
    """把接入服务的领域异常映射成稳定的 HTTP 语义。

    状态码与错误码挂在**异常类**上，而不是散在每个 handler 的 if 里：这样
    「不可核验 = 503」这类判定只有一个来源，不会出现某个分支忘了写 503 而
    变成 500 —— 而 500 会让客户端以为「再重试一次也许行」，实际是权限问题，
    于是申请被无限重试。
    """

    status = int(getattr(exc, "status_code", 500) or 500)
    code = str(getattr(exc, "code", "onboarding_error"))
    message = str(getattr(exc, "message", "") or "接入请求处理失败。")
    if status >= 500:
        log.info("onboarding request degraded | code=%s", code, exc_info=True)
    return _error_response(status, code, message)


#: 进程内缓存。**权威副本在数据库**（model_price_catalog），进程重启后由
#: :func:`restore_price_catalog` 读回来 —— 曾经只有这个模块级变量，于是重启
#: 之后价格目录回到空，一个已启用的金额上限立刻失去计价依据。
_PRICE_CATALOG = pricing.PriceCatalog(version=1)
_PRICE_STORE: Any = None


def configure_price_store(session_factory: Any) -> None:
    global _PRICE_STORE
    _PRICE_STORE = session_factory


def price_catalog_provider() -> pricing.PriceCatalog:
    return _PRICE_CATALOG


# 发出时的**计价冻结**要拿"此刻的目录"：把它接给 pricing，让
# ``pricing.freeze_price`` 只用权威缓存（不查库、不猜价）。接的是**函数**而不是
# 目录对象本身 —— 改价会替换 `_PRICE_CATALOG`，冻结的必须是最新那一版。
pricing.configure_catalog_source(price_catalog_provider)


def set_price_catalog(catalog: pricing.PriceCatalog) -> None:
    global _PRICE_CATALOG
    _PRICE_CATALOG = catalog


async def restore_price_catalog() -> pricing.PriceCatalog:
    """从数据库恢复**最后一版**价格目录。启动时装配。"""

    global _PRICE_CATALOG
    if _PRICE_STORE is None:
        return _PRICE_CATALOG
    from sqlalchemy import select

    from bot.db.models import ModelPriceCatalog

    async with _PRICE_STORE() as session:
        row = (
            await session.execute(
                select(ModelPriceCatalog).order_by(ModelPriceCatalog.version.desc())
            )
        ).scalars().first()
    if row is None:
        return _PRICE_CATALOG
    try:
        entries = tuple(
            pricing.PriceEntry(**item) for item in (row.payload or [])
        )
    except (TypeError, pricing.PricingError):
        log.warning("price catalog restore failed | version=%s", row.version)
        return _PRICE_CATALOG
    _PRICE_CATALOG = pricing.PriceCatalog(version=int(row.version), entries=entries)
    return _PRICE_CATALOG


async def _persist_price_catalog(
    catalog: pricing.PriceCatalog,
    *,
    expected_version: int,
    actor_id: int = 0,
) -> None:
    """目录按版本**只增不改**：改价不追改已发生的账，重启也能读回。

    这里是价格目录写路径的**期望版本 CAS**：在同一事务里先把「库里的最新
    版本」（``MAX(version)``）读出来跟调用方给的 ``expected_version`` 比，
    不相等就 409 —— 绝不拿进程内 ``_PRICE_CATALOG`` 缓存当依据，否则一个持有
    旧草稿的会话会把别人刚提交的改动静默覆盖回去。真正的并发间隙由主键冲突
    兜底：两个会话同时读到同一版本时，先提交的成功，后提交的撞主键，同样必须
    翻译成 409 ``price_version_conflict`` 而不是冒成 500。

    空库没有行时把最新版本视为 1（与模块级默认 ``PriceCatalog(version=1)``
    一致）：此时 ``expected_version=1`` 通过，插入的是版本 2。
    """

    if _PRICE_STORE is None:
        raise _APIError(
            503, "price_store_unavailable", "价格目录存储不可用。"
        )

    from bot.db.models import ModelPriceCatalog

    async with _PRICE_STORE() as session:
        async with session.begin():
            durable_latest = (
                await session.execute(
                    select(func.max(ModelPriceCatalog.version))
                )
            ).scalar()
            current_version = 1 if durable_latest is None else int(durable_latest)
            if int(expected_version) != current_version:
                raise _APIError(
                    409,
                    "price_version_conflict",
                    f"价格目录已被其他会话更新（服务端当前版本 {current_version}，"
                    f"客户端期望版本 {int(expected_version)}），请刷新后重试。",
                )
            # 版本号由 CAS 推出：durable latest == expected_version → 新版本 +1。
            session.add(
                ModelPriceCatalog(
                    version=int(catalog.version),
                    payload=[entry.document() for entry in catalog.entries],
                    updated_by=int(actor_id),
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                detail = str(getattr(exc, "orig", exc))
                await session.rollback()
                raise _APIError(
                    409,
                    "price_version_conflict",
                    f"价格目录已被其他会话抢先写入（客户端期望版本 "
                    f"{int(expected_version)}），请刷新后重试。",
                ) from RuntimeError(detail)


def _bounded_days(raw: object) -> int:
    if raw is None or raw == "":
        return 7
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise _APIError(400, "invalid_days", "days 必须是整数。") from exc
    if days < 1 or days > 90:
        raise _APIError(400, "invalid_days", "days 必须在 1..90 之间。")
    return days


def _strict_revision(raw: Any) -> int:
    """revision 必须是无损整数：``2.75`` 曾经被截成 2，乐观锁直接失效。"""

    if raw is None or raw == "":
        return 0
    try:
        return strict_int(raw, field="revision", minimum=0)
    except StrictValueError as exc:
        raise _APIError(400, "invalid_revision", "revision 必须是非负整数。") from exc


def _opaque_error_ref(exc: BaseException, *, code: str, where: str) -> str:
    """把内部异常细节换成**通用文案 + 一个能在服务端日志里对上的编号**。

    A-27 / D3-23：``str(exc)`` 直接进响应体时，管理员会看到
    ``runtime config revision changed: expected 5, got 7`` /
    ``CONFIG_MASTER_KEY is required before saving secret settings`` 这类内部原文
    ——暴露了内部实现（revision 数值、配置项的 env 变量名），对排障也没有帮助。
    现在详情只进服务端日志，客户端拿到的文案里带一个 ``ref=`` 编号，管理员把这个
    编号报给运维就能在日志里定位到同一次失败。
    """

    ref = secrets.token_hex(4)
    log.error(
        "Mini App settings API internal error | ref=%s code=%s where=%s type=%s",
        ref,
        code,
        where,
        type(exc).__name__,
        exc_info=exc,
    )
    return ref


def _opaque_error_message(exc: BaseException, *, code: str, where: str, text: str) -> str:
    return f"{text}（错误编号 {_opaque_error_ref(exc, code=code, where=where)}）"


def register_settings_routes(
    app: web.Application,
    *,
    bot: Any,
    bot_token: str,
    settings: Settings,
    manager: RuntimeConfigManager,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register super-admin-only runtime and per-group settings routes."""

    Handler = Callable[[web.Request, Any], Awaitable[web.StreamResponse]]

    # 回放**在 HTTP 请求之外**跑：创建端点立刻返回 run id，客户端轮询/取消。
    # 有界性（全局并发、单群并发、排队深度）都在执行器里，超了就是稳定的 429。
    # ``settings`` 是被 ``RuntimeConfigManager.apply_to_settings`` 就地更新的活对象，
    # 所以这里用 provider 惰性取而不是在建 run 时抓一份：建 run 时冻结的是**绑定
    # 摘要**（``model_binding``/``model_digest``），执行时才去构造真正的客户端，
    # 两者的漂移由 ``fidelity.model_binding_drift`` 显式说出来。
    _replay_executor = moderation_replay.ReplayExecutor(
        session_factory,
        lambda: settings,
    )
    # TTL 清理与中断回收的**真实**执行者：启动跑一次，之后周期性跑，进程结束前
    # 一定停下来。没有它，"样本到期不参与新回放"就只是**不参与**，正文还躺在
    # 库里 —— 而样本是别人的发言（哪怕已脱敏），那正是这张表最不该长期持有的东西。
    _replay_maintenance = moderation_replay.ReplayMaintenance(session_factory)

    async def _replay_startup(_app: web.Application) -> None:
        """应用启动：回收上一轮遗留的 ``running`` + 跑一次 TTL 清理。

        为什么是**应用启动**而不是"第一次提交时顺手做"：回收扫描要写库，而任何
        HTTP 请求此刻都可能正握着 SQLite 的（全局）写锁 —— 自己等自己等满
        ``busy_timeout`` 之后抛错。启动这一步跑在**任何请求之前**，是本进程里
        唯一一处可以安全写库的时刻。失败不许挡住服务：它只是"这一轮没回收成"，
        下一轮维护会再来一次（``ensure_ready`` 失败不会伪造 ready）。
        """

        try:
            await _replay_executor.ensure_ready()
            await _replay_maintenance.run_once()
            _replay_maintenance.start()
        except Exception:  # noqa: BLE001 - 维护绝不能把服务拖不起来
            log.warning("replay: 启动维护失败 error=replay_startup_failed")

    async def _replay_cleanup(_app: web.Application) -> None:
        """应用关闭：停掉周期任务并关停执行器。

        关停之后没有任何后台任务还会碰数据库（``shutdown`` 还会在关闭写连接
        之前把在飞的每一轮收成终态），也不再有新提交被接受。
        """

        try:
            await _replay_maintenance.stop()
        except Exception:  # noqa: BLE001 - 关停不追究原因
            log.warning("replay: 停止周期维护失败")
        try:
            await _replay_executor.shutdown()
        except Exception:  # noqa: BLE001 - 同上
            log.warning("replay: 关闭后台执行器失败")

    app.on_startup.append(_replay_startup)
    app.on_cleanup.append(_replay_cleanup)

    async def _operations_startup(_app: web.Application) -> None:
        """应用启动：把运营任务中心写侧内核绑到本进程真实的库与配置上。

        执行期（``_execute_run``）需要能自己开事务；绑定真实的 ``session_factory``
        意味着"手动执行"和"HTTP 请求"读的是同一个库，而不是另起一个引擎。
        """

        operations_runs.ensure_tasks_loaded()
        operations_runs.bind_execution_runtime(
            settings=settings, session_factory=session_factory
        )

    app.on_startup.append(_operations_startup)

    def authenticated(
        handler: Handler,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                user = await require_super_admin(
                    request,
                    bot_token=bot_token,
                    super_admin_id=settings.super_admin_id,
                )
                with privileged_request_scope():
                    return await handler(request, user)
            except web.HTTPException:
                raise
            except _APIError as exc:
                return _error_response(
                    exc.status, exc.code, exc.message, details=exc.details
                )
            except ValidationError as exc:
                return _error_response(
                    400,
                    "validation_error",
                    "配置校验失败。",
                    details=_validation_details(exc),
                )
            except RuntimeConfigConflictError as exc:
                # A-27 / D3-23：原文（expected/got 的 revision 数值）只进日志。
                return _error_response(
                    409,
                    "revision_conflict",
                    _opaque_error_message(
                        exc,
                        code="revision_conflict",
                        where="authenticated.put_settings",
                        text="配置已被其他会话更新，请刷新后重试。",
                    ),
                )
            except group_onboarding.OnboardingError as exc:
                # 审批路由同样跑接入状态机；领域异常必须按它自己的语义返回，
                # 不能掉进下面的 500 —— 「已拒绝的申请批准不了」是 409，不是
                # 「服务器炸了」，否则超管会一直重试一个永远不会成功的请求。
                return _onboarding_error_response(exc)
            except Exception as exc:
                return _error_response(
                    500,
                    "internal_error",
                    _opaque_error_message(
                        exc,
                        code="internal_error",
                        where="authenticated",
                        text="服务器处理请求失败。",
                    ),
                )

        return wrapped

    def any_admin(
        handler: Handler,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                user = await require_authenticated_user(request, bot_token=bot_token)
                with privileged_request_scope():
                    return await handler(request, user)
            except web.HTTPException:
                raise
            except _APIError as exc:
                return _error_response(
                    exc.status, exc.code, exc.message, details=exc.details
                )
            except ValidationError as exc:
                return _error_response(
                    400,
                    "validation_error",
                    "配置校验失败。",
                    details=_validation_details(exc),
                )
            except group_onboarding.OnboardingError as exc:
                return _onboarding_error_response(exc)
            except RuntimeConfigEncryptionError as exc:
                # A-27 / D3-23：原文会写出 CONFIG_MASTER_KEY 这个 env 变量名。
                return _error_response(
                    400,
                    "secret_storage_unavailable",
                    _opaque_error_message(
                        exc,
                        code="secret_storage_unavailable",
                        where="any_admin",
                        text="密钥服务暂时不可用，请联系最高管理员。",
                    ),
                )
            except Exception as exc:
                return _error_response(
                    500,
                    "internal_error",
                    _opaque_error_message(
                        exc,
                        code="internal_error",
                        where="any_admin",
                        text="服务器处理请求失败。",
                    ),
                )

        return wrapped

    async def _allowed_group_ids(user_id: int) -> set[int] | None:
        if settings.super_admin_id and int(user_id) == int(settings.super_admin_id):
            return None
        stmt = (
            select(Admin.group_id)
            .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
            .where(
                Admin.user_id == int(user_id),
                AuthorizedGroup.bot_present.is_(True),
            )
        )
        async with session_factory() as session:
            return {int(value) for value in (await session.scalars(stmt)).all()}

    async def _telegram_admin_revalidated(
        group_id: int, user_id: int
    ) -> bool | None:
        """向 Telegram 交叉校验「这个人现在还是不是该群的管理员」（D3-18）。

        返回三态，区别很重要：

        * ``True`` —— Telegram 权威名单里确实有他，可以放行；
        * ``False`` —— Telegram 权威名单里**没有**他（已被撤权 / 已退群），必须拒绝；
        * ``None`` —— 这次**没问成**（无 bot 能力 / 超时 / 限流 / 异常），调用方按
          「离线降级」处理，继续沿用本地 ``Admin`` 表的判断。

        三态而不是二值，是因为把「查不到」和「确认没有」混为一谈会在两种方向上出错：
        混成「确认没有」会在 Telegram 抖动时把管理员锁在面板外（报告明确点名的回归
        风险），混成「还有」就等于本条 finding 白修。

        结果按 (群, 人) 缓存一个短 TTL，避免每个请求都打一次 ``getChatAdministrators``。
        """
        cache_key = (int(group_id), int(user_id))
        now = time.monotonic()
        cached = _ADMIN_REVALIDATION_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        get_admins = getattr(bot, "get_chat_administrators", None)
        if not callable(get_admins):
            return None
        try:
            members = await _await_member_identity_lookup(
                lambda: get_admins(group_id),
                timeout_seconds=5.0,
            )
        except Exception:
            log.info(
                "admin revalidation unavailable | group=%s user=%s", group_id, user_id
            )
            return None
        is_admin = False
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
            if status in {"creator", "administrator"}:
                is_admin = True
            break
        _ADMIN_REVALIDATION_CACHE[cache_key] = (
            now + _ADMIN_REVALIDATION_TTL_SECONDS,
            is_admin,
        )
        if len(_ADMIN_REVALIDATION_CACHE) > _ADMIN_REVALIDATION_CACHE_MAX:
            _ADMIN_REVALIDATION_CACHE.clear()
        return is_admin

    async def _require_group_access(group_id: int, user_id: int) -> None:
        allowed = await _allowed_group_ids(user_id)
        if allowed is not None and group_id not in allowed:
            raise _APIError(403, "group_access_denied", "你没有该群的管理权限。")
        if allowed is None:
            return  # 最高管理员：不受本判定约束（与既有行为一致）。
        # 本地表说「有权限」，但它可能已经过期：用户在 Telegram 侧被撤权或已退群时，
        # 超管清理掉那行 `Admin` 之前，Mini App 的**全部**权限（封禁/解封成员、篡改
        # 群规与关键词回复、改群设置、触发全群巡检）都还在。`bot_present` 只表示
        # 「bot 在不在群里」，**不表示「人还是不是管理员」」（D3-18）。
        if await _telegram_admin_revalidated(group_id, user_id) is False:
            raise _APIError(403, "group_access_denied", "你在该群的管理权限已被撤销。")

    async def _ensure_group_row(session: AsyncSession, group_id: int) -> Group:
        row = await session.get(Group, group_id)
        if row is None:
            row = Group(id=group_id, title="", settings={})
            session.add(row)
            await session.flush()
        return row

    @any_admin
    async def get_session(_request: web.Request, user: Any) -> web.Response:
        user_id = int(user.id)
        is_super = bool(
            settings.super_admin_id and user_id == int(settings.super_admin_id)
        )
        allowed = await _allowed_group_ids(user_id)
        group_ids = [] if allowed is None else sorted(allowed)
        if not is_super and not group_ids:
            raise _APIError(403, "admin_required", "你没有任何已授权群的管理权限。")
        return _success_response(
            {
                "session": {
                    "user_id": user_id,
                    "role": "super_admin" if is_super else "group_admin",
                    "can_manage_global": is_super,
                    "group_ids": group_ids,
                }
            }
        )

    @authenticated
    async def get_settings(_request: web.Request, _user: Any) -> web.Response:
        return _success_response(manager.api_document())

    @authenticated
    async def put_settings(request: web.Request, user: Any) -> web.Response:
        body = await _json_object(request)
        update = _RuntimeSettingsUpdate.model_validate(body)
        try:
            await manager.save(
                update.config,
                expected_revision=update.revision,
                updated_by=int(user.id),
                secret_changes=update.secret_changes,
            )
        except ValidationError:
            raise
        except RuntimeConfigConflictError:
            raise
        except RuntimeConfigEncryptionError as exc:
            # A-27 / D3-23：原文会写出 CONFIG_MASTER_KEY 这个 env 变量名。
            raise _APIError(
                400,
                "secret_storage_unavailable",
                _opaque_error_message(
                    exc,
                    code="secret_storage_unavailable",
                    where="put_settings.encryption",
                    text="密钥服务暂时不可用，请联系最高管理员。",
                ),
            ) from exc
        except ValueError as exc:
            raise _APIError(400, "invalid_settings", str(exc)) from exc
        return _success_response(manager.api_document())

    def _history_page_params(
            request: web.Request,
        ) -> tuple[int, int | None]:
        """``GET /api/v1/settings/history`` 的 ``limit`` / ``before`` 查询参数。

        越界一律 **400 + 稳定码 ``invalid_history_page``**，刻意不"悄悄夹到上限"：
        夹取会让调用方以为自己翻了 1000 行、实际只拿到 20 行，而 UI 的"还有更多"
        提示就是错的。默认 :data:`HISTORY_PAGE_LIMIT`，硬上限
        :data:`HISTORY_PAGE_MAX_LIMIT`。
        """

        raw_limit = request.query.get("limit")
        if raw_limit is None or str(raw_limit).strip() == "":
            limit = HISTORY_PAGE_LIMIT
        else:
            try:
                limit = int(str(raw_limit).strip())
            except (TypeError, ValueError) as exc:
                raise _APIError(
                    400, "invalid_history_page", "limit 必须是整数。"
                ) from exc
        if limit < 1 or limit > HISTORY_PAGE_MAX_LIMIT:
            raise _APIError(
                400,
                "invalid_history_page",
                f"limit 必须在 1 到 {HISTORY_PAGE_MAX_LIMIT} 之间。",
            )
        raw_before = request.query.get("before")
        before: int | None = None
        if raw_before is not None and str(raw_before).strip() != "":
            try:
                before = int(str(raw_before).strip())
            except (TypeError, ValueError) as exc:
                raise _APIError(
                    400, "invalid_history_page", "before 必须是 revision 整数。"
                ) from exc
            if before < 1:
                raise _APIError(
                    400, "invalid_history_page", "before 必须是正整数 revision。"
                )
        return limit, before

    def _history_not_found(exc: BaseException, *, where: str) -> _APIError:
        """历史缺失/损坏 → 稳定的 404，内部细节只进日志。

        缺失与损坏走同一个码：调用方能做的处理完全一样（"这一版回滚不了，
        重新加载"），分开给码只会让前端多写一个永远用不上的分支。
        """

        return _APIError(
            404,
            "history_revision_not_found",
            _opaque_error_message(
                exc,
                code="history_revision_not_found",
                where=where,
                text="该历史版本不存在或已损坏，请刷新后重试。",
            ),
        )

    def _secret_storage_unavailable(exc: BaseException, *, where: str) -> _APIError:
        return _APIError(
            400,
            "secret_storage_unavailable",
            _opaque_error_message(
                exc,
                code="secret_storage_unavailable",
                where=where,
                text="密钥服务暂时不可用，请联系最高管理员。",
            ),
        )

    def _invalid_settings(exc: BaseException, *, where: str) -> _APIError:
        """字段级校验失败：稳定码 ``invalid_settings`` + 逐字段 details。

        details 保留（管理员要知道自己哪个字段填错了），但**原文**只从用户输入
        与 schema 里来，不含内部状态；内部异常一律走 :func:`_opaque_error_message`。
        """

        if isinstance(exc, ValidationError):
            return _APIError(
                400,
                "invalid_settings",
                "配置校验失败。",
                details=_validation_details(exc),
            )
        return _APIError(400, "invalid_settings", str(exc))

    def _history_not_restorable(exc: BaseException, *, where: str) -> _APIError:
        """回滚/回滚预览里"这一版没法安全回滚" → 稳定 400。

        原文里只有**路径名**（哪几个字段取不到当前值），不含任何配置值或内部
        状态，所以可以逐字回给管理员——他需要知道去把哪个字段填上。
        """

        return _APIError(400, "history_revision_not_restorable", str(exc))

    @authenticated
    async def post_settings_diff(request: web.Request, _user: Any) -> web.Response:
        """保存前的**差异预览**：这次改动会碰到哪些字段、哪些要重启。不写库。

        与 ``PUT /api/v1/settings`` **共用同一条解析与判断路径**：同一份请求体
        在这里被接受/拒绝的结论、差异条目、快照字节数，与真保存逐字相同，失败
        时也是**同一个**错误码 ``invalid_settings``（超限时同样如此）。
        """

        body = await _json_object(request)
        update = _RuntimeSettingsDiff.model_validate(body)
        try:
            preview = await manager.preview(
                update.config,
                secret_changes=update.secret_changes,
            )
        except ValidationError as exc:
            raise _invalid_settings(exc, where="post_settings_diff") from exc
        except ValueError as exc:
            raise _invalid_settings(exc, where="post_settings_diff") from exc
        return _success_response(
            {
                "revision": int(preview["revision"]),
                "expected_revision": int(update.revision),
                # 预览没有写入语义，不做乐观锁拒绝；前端拿这个字段提示"你看到的
                # 已经不是最新版了"。
                "stale": int(preview["revision"]) != int(update.revision),
                "diff": preview["diff"],
                "summary": preview["summary"],
                "changed_secret_paths": preview["changed_secret_paths"],
                "snapshot_bytes": preview["snapshot_bytes"],
                "snapshot_limit_bytes": preview["snapshot_limit_bytes"],
                "restart_pending": preview["restart_pending"],
            }
        )

    @authenticated
    async def list_settings_history(request: web.Request, _user: Any) -> web.Response:
        """分页历史列表（默认最新 20 行）。响应带稳定的 ``page`` 翻页契约。"""

        limit, before = _history_page_params(request)
        return _success_response(await manager.history_page(limit=limit, before=before))

    @authenticated
    async def get_settings_history_revision(
        request: web.Request, _user: Any
    ) -> web.Response:
        target = _path_int(
            request,
            "revision",
            code="invalid_history_revision",
            message="历史版本号无效。",
        )
        try:
            row = await manager.history_row(target)
        except RuntimeConfigHistoryNotFoundError as exc:
            raise _history_not_found(exc, where="get_settings_history_revision") from exc
        return _success_response(
            {
                "revision": int(row.revision),
                "parent_revision": int(row.parent_revision),
                "operation": str(row.operation),
                "summary": str(row.summary),
                "updated_by": int(row.updated_by or 0),
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                "restore_source_revision": (
                    int(row.restore_source_revision)
                    if row.restore_source_revision is not None
                    else None
                ),
                "secret_paths_changed": list(row.secret_paths_changed or []),
                # 脱敏快照 + 逐路径的"配没配"，**没有任何密钥值**。
                "config": row.payload_redacted,
                "secrets": manager.history_secret_metadata(row),
            }
        )

    @authenticated
    async def get_settings_history_diff(
        request: web.Request, _user: Any
    ) -> web.Response:
        target = _path_int(
            request,
            "revision",
            code="invalid_history_revision",
            message="历史版本号无效。",
        )
        against_raw = str(request.query.get("against") or "").strip()
        try:
            against = int(against_raw)
        except (TypeError, ValueError) as exc:
            raise _APIError(
                400,
                "invalid_history_revision",
                "请提供要对比的历史版本号（against）。",
            ) from exc
        try:
            base = await manager.history_row(against)
            other = await manager.history_row(target)
        except RuntimeConfigHistoryNotFoundError as exc:
            raise _history_not_found(exc, where="get_settings_history_diff") from exc
        secret_paths = manager.config.secret_paths() | discover_secret_paths(
            base.payload_redacted, manager.config.secret_paths()
        ) | discover_secret_paths(
            other.payload_redacted, manager.config.secret_paths()
        )
        return _success_response(
            {
                "revision": int(other.revision),
                "against": int(base.revision),
                "diff": build_diff(
                    base.payload_redacted, other.payload_redacted, secret_paths
                ),
            }
        )

    @authenticated
    async def get_settings_history_restore_preview(
        request: web.Request, _user: Any
    ) -> web.Response:
        """回滚预览：**当前配置 → 真正的回滚候选**，不是两个历史版本之间的差异。

        管理员点"回滚"拿到的文档是 ``目标版本非密钥字段 + 当前密钥 + 当前不透明
        值``，所以这条差异算的正是那一份——原来的 ``?against=`` 只能比两个历史
        版本，看不出"回滚之后我现在这份配置会变成什么样"。

        失败与真正回滚**同码**（``history_revision_not_restorable`` /
        ``history_revision_not_found``），因此"预览能回滚、点下去失败"不会发生。
        """

        target = _path_int(
            request,
            "revision",
            code="invalid_history_revision",
            message="历史版本号无效。",
        )
        try:
            return _success_response(await manager.preview_restore(target))
        except RuntimeConfigHistoryNotFoundError as exc:
            raise _history_not_found(
                exc, where="get_settings_history_restore_preview"
            ) from exc
        except RuntimeConfigHistoryNotRestorableError as exc:
            raise _history_not_restorable(
                exc, where="get_settings_history_restore_preview"
            ) from exc
        except RuntimeConfigEncryptionError as exc:
            raise _secret_storage_unavailable(
                exc, where="get_settings_history_restore_preview"
            ) from exc
        except ValidationError as exc:
            raise _invalid_settings(
                exc, where="get_settings_history_restore_preview"
            ) from exc
        except ValueError as exc:
            raise _invalid_settings(
                exc, where="get_settings_history_restore_preview"
            ) from exc

    @authenticated
    async def post_settings_history_restore(
        request: web.Request, user: Any
    ) -> web.Response:
        target = _path_int(
            request,
            "revision",
            code="invalid_history_revision",
            message="历史版本号无效。",
        )
        body = await _json_object(request)
        update = _RuntimeSettingsRestore.model_validate(body)
        try:
            await manager.restore(
                target,
                expected_revision=update.revision,
                updated_by=int(user.id),
            )
        except RuntimeConfigHistoryNotFoundError as exc:
            raise _history_not_found(exc, where="post_settings_history_restore") from exc
        except RuntimeConfigHistoryNotRestorableError as exc:
            # 与回滚预览同码：管理员在预览阶段就能拿到同一条可执行的提示。
            raise _history_not_restorable(
                exc, where="post_settings_history_restore"
            ) from exc
        except RuntimeConfigConflictError:
            # 乐观锁失败：原文里的 revision 数值只进日志（A-27 / D3-23）。
            raise
        except RuntimeConfigEncryptionError as exc:
            # 原文会写出 CONFIG_MASTER_KEY 这个 env 变量名。
            raise _secret_storage_unavailable(
                exc, where="post_settings_history_restore"
            ) from exc
        except ValidationError as exc:
            raise _invalid_settings(exc, where="post_settings_history_restore") from exc
        except ValueError as exc:
            raise _invalid_settings(exc, where="post_settings_history_restore") from exc
        document = manager.api_document()
        # ``retained_secrets``：密钥表里有、当前配置里没有对应槽位的那几条（典型
        # 场景：回滚到供应商更少的旧版本）。它们**没有被删除**，只是运行时不再使用
        # ——必须告诉运维，否则他会以为这把 Key 没了并重新填一遍。
        return _success_response(
            {
                **document,
                "restore": {
                    "source_revision": int(target),
                    "retained_secrets": await manager.retained_secret_paths(),
                },
            }
        )

    @authenticated
    async def list_authorized_groups_api(_request: web.Request, _user: Any) -> web.Response:
        async with session_factory() as session:
            rows = (await session.execute(
                select(AuthorizedGroup, Group.title)
                .outerjoin(Group, Group.id == AuthorizedGroup.group_id)
                .order_by(AuthorizedGroup.created_at.desc())
            )).all()
        return _success_response({
            "authorized_groups": [
                {
                    "group_id": int(row.group_id),
                    "title": str(title or ""),
                    "authorized_by": int(row.authorized_by or 0),
                    "bot_present": bool(row.bot_present),
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row, title in rows
            ]
        })

    @authenticated
    async def create_authorized_group_api(request: web.Request, user: Any) -> web.Response:
        body = _AuthorizedGroupCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            row = await session.get(AuthorizedGroup, body.group_id)
            if row is None:
                row = AuthorizedGroup(group_id=body.group_id, authorized_by=int(user.id))
                session.add(row)
            elif not bool(row.bot_present):
                row.bot_present = True
                row.authorized_by = int(user.id)
            group = await session.get(Group, body.group_id)
            if group is None:
                session.add(Group(id=body.group_id, title=clean_text(body.title, max_len=255), settings={}))
            elif body.title:
                group.title = clean_text(body.title, max_len=255)
            try:
                await session.commit()
            except IntegrityError as exc:
                # 并发为同一个群建行时唯一索引（models.py 的 ix_*_unique）会撞。
                # 兄弟接口 create_group_admin_api / put_group_settings 都显式回
                # 409；这里原来让异常冒泡到装饰器的兜底 except Exception，管理员
                # 看到的是 500 \"internal_error\"——看起来像授权失败，实际已经成功。
                await session.rollback()
                raise _APIError(
                    409,
                    "authorized_group_conflict",
                    "该群刚刚被并发授权，请刷新后重试。",
                ) from exc
        return _success_response({"created": True})

    @authenticated
    async def delete_authorized_group_api(request: web.Request, _user: Any) -> web.Response:
        group_id = _group_id(request)
        async with session_factory() as session:
            row = await session.get(AuthorizedGroup, group_id)
            if row is not None:
                await session.delete(row)
            await session.execute(delete(Admin).where(Admin.group_id == group_id))
            await session.commit()
        clear_privileged_operator_group(group_id)
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_group_admins_api(request: web.Request, user: Any) -> web.Response:
        # Group admins may read this list too: the Mini App call-admin picker
        # renders it for every group they manage. Mutations stay super-admin.
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(Admin)
                .where(Admin.group_id == group_id)
                .order_by(Admin.id)
            )).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
        )
        admins = [
            {
                "user_id": int(row.user_id),
                "role": str(row.role or "admin"),
                "display_name": _member_identity(
                    members.get(int(row.user_id)),
                    int(row.user_id),
                )["display_name"],
            }
            for row in rows
        ]
        return _success_response({"admins": admins})

    @any_admin
    async def list_telegram_admins_api(request: web.Request, user: Any) -> web.Response:
        """Live Telegram admins for the call-admin target picker.

        Falls back to the locally authorized admin list (with roster display
        names) when the Telegram lookup is unavailable.
        """
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        admins: list[dict[str, Any]] = []
        get_admins = getattr(bot, "get_chat_administrators", None)
        if callable(get_admins):
            try:
                members = await _await_member_identity_lookup(
                    lambda: get_admins(group_id),
                    timeout_seconds=5.0,
                )
                for member in members or []:
                    member_user = getattr(member, "user", None)
                    if member_user is None or getattr(member_user, "is_bot", False):
                        continue
                    user_id = int(member_user.id)
                    full_name = str(getattr(member_user, "full_name", "") or "").strip()
                    username = str(getattr(member_user, "username", "") or "").strip()
                    admins.append({
                        "user_id": user_id,
                        "display_name": full_name or (f"@{username}" if username else str(user_id)),
                        "username": username,
                    })
            except Exception:
                log.info("telegram admin list failed | group=%s", group_id)
                admins = []
        if not admins:
            async with session_factory() as session:
                rows = (await session.execute(
                    select(Admin.user_id, GroupMember.full_name, GroupMember.username)
                    .outerjoin(
                        GroupMember,
                        (GroupMember.group_id == Admin.group_id)
                        & (GroupMember.user_id == Admin.user_id),
                    )
                    .where(Admin.group_id == group_id)
                    .order_by(Admin.id)
                )).all()
            for user_id, full_name, username in rows:
                clean_username = str(username or "").strip()
                display = str(full_name or "").strip() or (
                    f"@{clean_username}" if clean_username else str(int(user_id))
                )
                admins.append({
                    "user_id": int(user_id),
                    "display_name": display,
                    "username": clean_username,
                })
        return _success_response({"admins": admins})

    @authenticated
    async def create_group_admin_api(request: web.Request, _user: Any) -> web.Response:
        group_id = _group_id(request)
        body = _AdminCreate.model_validate(await _json_object(request))
        async with session_factory() as session:
            authorized = await session.get(AuthorizedGroup, group_id)
            if authorized is None or not bool(authorized.bot_present):
                raise _APIError(404, "group_not_found", "该群尚未授权。")
            row = await session.scalar(select(Admin).where(
                Admin.group_id == group_id,
                Admin.user_id == body.user_id,
            ))
            if row is None:
                row = Admin(group_id=group_id, user_id=body.user_id, role=body.role)
                session.add(row)
            else:
                row.role = body.role
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None or not bool(authorized.bot_present):
                    raise _APIError(
                        404,
                        "group_not_found",
                        "该群授权状态刚刚发生变化。",
                    ) from exc
                raise _APIError(
                    409,
                    "admin_conflict",
                    "管理员授权发生并发冲突，请重试。",
                ) from exc
        mark_privileged_operator(body.user_id, group_id=group_id)
        # 新授权/改角色：作废旧的交叉校验结论，别让被撤销过的用户被负缓存卡住。
        _forget_admin_revalidation(group_id, body.user_id)
        return _success_response({"created": True})

    @authenticated
    async def delete_group_admin_api(request: web.Request, _user: Any) -> web.Response:
        group_id = _group_id(request)
        user_id = _path_int(request, "user_id")
        async with session_factory() as session:
            row = await session.scalar(select(Admin).where(
                Admin.group_id == group_id,
                Admin.user_id == user_id,
            ))
            if row is not None:
                await session.delete(row)
            await session.commit()
        unmark_privileged_operator(user_id, group_id=group_id)
        _forget_admin_revalidation(group_id, user_id)
        return _success_response({"deleted": row is not None})

    def _page_window(request: web.Request) -> tuple[int, int]:
        """Clamped ``limit``/``offset`` shared by every paged list endpoint."""

        try:
            limit = min(500, max(1, int(request.query.get("limit", "100"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError) as exc:
            raise _APIError(400, "invalid_pagination", "分页参数无效。") from exc
        return limit, offset

    def _global_registry_query(request: web.Request) -> tuple[int, int, str]:
        limit, offset = _page_window(request)
        query = clean_text(str(request.query.get("query", "")), max_len=200)
        return limit, offset, query

    @authenticated
    async def list_global_bans_api(request: web.Request, _user: Any) -> web.Response:
        limit, offset, query = _global_registry_query(request)
        async with session_factory() as session:
            rows = await list_global_bans(
                session,
                limit=limit,
                offset=offset,
                query=query,
            )
        return _success_response({"global_bans": [
            {
                "user_id": int(row.user_id),
                "reason": str(row.reason or ""),
                "source": str(row.source or "manual"),
                "created_by": int(row.created_by or 0),
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in rows
        ], "next_offset": offset + len(rows) if len(rows) == limit else None})

    @authenticated
    async def list_global_exemptions_api(
        request: web.Request,
        _user: Any,
    ) -> web.Response:
        limit, offset, query = _global_registry_query(request)
        async with session_factory() as session:
            rows = await list_join_screening_exemptions(
                session,
                limit=limit,
                offset=offset,
                query=query,
            )
        return _success_response({
            "global_exemptions": [
                {
                    "user_id": int(row.user_id),
                    "created_by": int(row.created_by or 0),
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row in rows
            ],
            "next_offset": offset + len(rows) if len(rows) == limit else None,
        })

    @authenticated
    async def delete_global_exemption_api(
        request: web.Request,
        _user: Any,
    ) -> web.Response:
        target_id = _path_int(request, "user_id")
        async with session_factory() as session:
            removed = await remove_join_screening_exemption(session, target_id)
            await session.commit()
        return _success_response({"deleted": bool(removed)})

    @authenticated
    async def create_global_ban_api(request: web.Request, user: Any) -> web.Response:
        body = _GlobalBanCreate.model_validate(await _json_object(request))
        if settings.super_admin_id and body.user_id == int(settings.super_admin_id):
            raise _APIError(400, "cannot_ban_owner", "不能封禁最高管理员。")
        telegram_ban_member = getattr(bot, "ban_chat_member", None)
        if not callable(telegram_ban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 封禁接口。")
        previous_warning: tuple[int, bool] | None = None
        async with session_factory() as session:
            authorization_rows = (
                await session.execute(
                    select(
                        AuthorizedGroup.group_id,
                        AuthorizedGroup.bot_present,
                    )
                )
            ).all()
            journal_group_ids = [int(group_id) for group_id, _ in authorization_rows]
            group_ids = [
                int(group_id)
                for group_id, bot_present in authorization_rows
                if bool(bot_present)
            ]
            active_group_set = set(group_ids)
            recoveries = await lease_join_verifications_for_user_unban(
                session,
                body.user_id,
                group_ids=journal_group_ids,
                manual_unban=False,
            )
            await add_global_ban(
                session,
                body.user_id,
                reason=clean_multiline_text(body.reason, max_len=1000).strip() or "手动封禁",
                source="manual",
                created_by=int(user.id),
            )
            prompts = tuple(
                (item.group_id, item.prompt_message_id)
                for item in recoveries
                if item.prompt_message_id > 0
                and int(item.group_id) in active_group_set
            )
            private_prompts = tuple(
                (item.user_id, item.private_message_id)
                for item in recoveries
                if item.private_message_id > 0
            )
            await session.commit()
        recovery_by_group = {int(item.group_id): item for item in recoveries}

        async def complete_recovery(group_id: int) -> bool:
            recovery = recovery_by_group.get(int(group_id))
            if recovery is None:
                return True
            async with session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if completed:
                    await completion_session.commit()
                    return True
                await completion_session.rollback()
                return False

        async def ban_one(group_id: int) -> bool:
            async with session_factory() as policy_session:
                if not await is_globally_banned(policy_session, body.user_id):
                    if await enforce_unban_member(bot, group_id, body.user_id):
                        await restore_member_permissions(bot, group_id, body.user_id)
                        await complete_recovery(group_id)
                    return False
            enforced = await enforce_ban_member(bot, group_id, body.user_id)
            if not enforced:
                return False
            async with session_factory() as policy_session:
                still_banned = await is_globally_banned(
                    policy_session,
                    body.user_id,
                )
            if still_banned:
                await complete_recovery(group_id)
                return True
            if await enforce_unban_member(bot, group_id, body.user_id):
                await restore_member_permissions(bot, group_id, body.user_id)
                await complete_recovery(group_id)
            return False

        outcomes = await _run_privileged_group_fanout(group_ids, ban_one)
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        succeeded = sum(outcomes)
        failures = len(outcomes) - succeeded
        if failures:
            raise _APIError(
                502,
                "telegram_ban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群封禁失败，请重试。",
            )
        return _success_response({
            "banned_groups": succeeded,
            "total_groups": len(group_ids),
            "deferred_groups": max(0, len(journal_group_ids) - len(group_ids)),
        })

    @authenticated
    async def delete_global_ban_api(request: web.Request, user: Any) -> web.Response:
        target_id = _path_int(request, "user_id")
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        async with session_factory() as session:
            authorization_rows = (
                await session.execute(
                    select(
                        AuthorizedGroup.group_id,
                        AuthorizedGroup.bot_present,
                    )
                )
            ).all()
            journal_group_ids = [int(group_id) for group_id, _ in authorization_rows]
            group_ids = [
                int(group_id)
                for group_id, bot_present in authorization_rows
                if bool(bot_present)
            ]
            active_group_set = set(group_ids)
            recoveries = await lease_join_verifications_for_user_unban(
                session,
                target_id,
                group_ids=journal_group_ids,
            )
            removed = await remove_global_ban(
                session,
                target_id,
                operator_id=int(user.id),
            )
            await session.execute(
                delete(UserWarning).where(
                    UserWarning.user_id == target_id,
                    UserWarning.group_id.in_(journal_group_ids),
                )
            )
            await session.commit()
        activate_manual_unban_recoveries(recoveries)
        prompts = tuple(
            (item.group_id, item.prompt_message_id)
            for item in recoveries
            if item.prompt_message_id > 0
            and int(item.group_id) in active_group_set
        )
        private_prompts = tuple(
            (item.user_id, item.private_message_id)
            for item in recoveries
            if item.private_message_id > 0
        )
        recovery_by_group = {int(item.group_id): item for item in recoveries}
        restored_group_ids: set[int] = set()

        async def unban_one(group_id: int) -> bool:
            async def preserve_ban() -> bool:
                async with session_factory() as session:
                    return await verification_release_blocked_by_ban(
                        session,
                        group_id=group_id,
                        user_id=target_id,
                    )

            if not await enforce_unban_member(
                bot,
                group_id,
                target_id,
                preserve_ban=preserve_ban,
            ):
                return False
            if not await restore_member_permissions(bot, group_id, target_id):
                # Keep the recovery journal for the verification sweeper.
                return True
            restored_group_ids.add(int(group_id))
            recovery = recovery_by_group.get(int(group_id))
            if recovery is not None:
                async with session_factory() as completion_session:
                    completed = await complete_leased_join_verification(
                        completion_session,
                        verification_id=int(recovery.verification_id),
                        lease_until=recovery.lease_until,
                        status="unbanning",
                    )
                    if completed:
                        await completion_session.commit()
                    else:
                        await completion_session.rollback()
            return True

        outcomes = await _run_privileged_group_fanout(group_ids, unban_one)
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        succeeded = sum(outcomes)
        restored = len(restored_group_ids)
        failures = (len(outcomes) - succeeded) + (succeeded - restored)
        if failures:
            raise _APIError(
                502,
                "telegram_unban_partial",
                f"已处理 {succeeded}/{len(group_ids)} 个群，但仍有群解封失败，请重试。",
            )
        return _success_response({
            "removed": removed,
            "unbanned_groups": succeeded,
            "restored_groups": restored,
            "total_groups": len(group_ids),
            "deferred_groups": max(0, len(journal_group_ids) - len(group_ids)),
        })

    @any_admin
    async def get_groups(_request: web.Request, user: Any) -> web.Response:
        allowed = await _allowed_group_ids(int(user.id))
        stmt = (
            select(
                AuthorizedGroup.group_id,
                Group.title,
                Group.settings,
            )
            .select_from(AuthorizedGroup)
            .outerjoin(Group, Group.id == AuthorizedGroup.group_id)
            .where(AuthorizedGroup.bot_present.is_(True))
            .order_by(AuthorizedGroup.created_at.desc())
        )
        if allowed is not None:
            if not allowed:
                return _success_response({"groups": []})
            stmt = stmt.where(AuthorizedGroup.group_id.in_(allowed))
        async with session_factory() as session:
            result = await session.execute(stmt)
            groups = [
                _group_document(group_id, title, group_settings)
                for group_id, title, group_settings in result.all()
            ]
        return _success_response(
            {"groups": groups, "permission_fields": permission_field_document()}
        )

    @any_admin
    async def get_group_default_permissions(
        request: web.Request,
        user: Any,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            group = await session.get(Group, group_id)
            stored = (
                (group.settings or {}).get(GROUP_PERMISSIONS_SETTINGS_KEY)
                if group is not None and isinstance(group.settings, dict)
                else None
            )
        configured = stored is not None
        repaired = False
        repair_reason = ""
        if stored is not None:
            try:
                config = normalize_group_permission_config(stored)
            except ValueError as exc:
                # A previously complete document can become incomplete when a
                # newer Bot API adds permission fields. Let the group admin
                # reopen and save it instead of trapping the Mini App behind a
                # 500 response. Current Telegram values fill newly introduced
                # fields while compatible base/window choices are retained.
                configured = False
                repaired = True
                repair_reason = str(exc)
                config = None
        else:
            config = None
        if config is None:
            try:
                base = await _await_member_identity_lookup(
                    lambda: fetch_telegram_default_permissions(bot, group_id),
                    timeout_seconds=5.0,
                )
            except Exception as exc:
                raise _APIError(
                    502,
                    "telegram_permissions_unavailable",
                    "无法读取当前 Telegram 群默认权限，请确认 bot 仍在群内。",
                ) from exc
            config = repair_group_permissions_config(
                stored,
                fallback_base=base,
            )
        resolved = resolve_group_permissions(config)
        service = get_group_permission_service()
        return _success_response(
            {
                "default_permissions": config,
                "permission_fields": permission_field_document(),
                "configured": configured,
                "repaired": repaired,
                "repair_reason": repair_reason,
                "effective": resolved.permissions,
                "active_window_ids": list(resolved.active_window_ids),
                "status": service.status(group_id) if service is not None else {},
            }
        )

    @any_admin
    async def put_group_settings(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))

        request_body = await _json_object(request)
        unknown_request = set(request_body) - {
            "revision",
            "settings",
            "api_model_query_secret_change",
        }
        if unknown_request:
            raise _APIError(
                400,
                "unknown_group_settings_request",
                f"包含不允许的群设置请求字段：{', '.join(sorted(unknown_request))}",
            )
        expected_revision = str(request_body.get("revision") or "").strip()
        body = request_body.get("settings")
        raw_secret_change = request_body.get("api_model_query_secret_change")
        secret_change: _GroupApiModelQuerySecretChange | None = None
        if raw_secret_change is not None:
            if not isinstance(raw_secret_change, dict):
                raise _APIError(
                    400,
                    "invalid_api_model_query_secret_change",
                    "模型 API Key 变更格式无效。",
                )
            secret_change = _GroupApiModelQuerySecretChange.model_validate(
                raw_secret_change
            )
            if secret_change.action == "replace":
                try:
                    normalized_api_key = normalize_api_model_query_api_key(
                        secret_change.value
                    )
                except ValueError as exc:
                    raise _APIError(
                        400,
                        "invalid_api_model_query_secret",
                        str(exc),
                    ) from exc
                if not normalized_api_key:
                    raise _APIError(
                        400,
                        "invalid_api_model_query_secret",
                        "模型 API Key 不能为空。",
                    )
        if not expected_revision or not isinstance(body, dict):
            raise _APIError(
                400,
                "group_revision_required",
                "群设置请求缺少 revision 或 settings。",
            )
        unknown = set(body) - _GROUP_SETTING_FIELDS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise _APIError(
                400,
                "unknown_group_settings",
                f"包含不允许修改的群设置：{names}",
            )
        settings_update = _GroupSettingsUpdate.model_validate(body)
        if not settings_update.model_fields_set and secret_change is None:
            raise _APIError(400, "empty_group_settings", "至少需要提供一个群设置。")

        permissions_changed = (
            GROUP_PERMISSIONS_SETTINGS_KEY in settings_update.model_fields_set
        )
        async with _group_update_guard(group_id):
            async with session_factory() as session:
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None or not bool(authorized.bot_present):
                    raise _APIError(404, "group_not_found", "该群未授权或不存在。")
                group = await session.get(Group, group_id)
                created_group = group is None
                if group is None:
                    group = Group(id=group_id, title="", settings={})
                    session.add(group)
                stored_settings = dict(group.settings or {})
                actual_revision = _group_revision(stored_settings)
                if actual_revision != expected_revision:
                    raise _APIError(
                        409,
                        "group_revision_conflict",
                        "群设置已被其他会话更新，请刷新后重试。",
                    )
                previous_style_target = int(
                    get_style_state(stored_settings).get("target_user_id") or 0
                )
                updated_settings = _apply_group_settings(
                    stored_settings,
                    settings_update,
                    settings,
                )
                api_model_query_fields = {
                    "api_model_query_enabled",
                    "api_model_query_base_url",
                    "api_model_query_http_timeout_sec",
                    "api_model_query_check_timeout_sec",
                }
                api_model_query_change_requested = bool(
                    settings_update.model_fields_set & api_model_query_fields
                    or secret_change is not None
                )
                if api_model_query_change_requested:
                    stored_api_config = get_api_model_query_config(stored_settings)
                    api_key_configured = await group_api_model_query_secret_exists(
                        session, group_id
                    )
                    if secret_change is not None:
                        if secret_change.action == "replace":
                            await replace_group_api_model_query_secret(
                                session,
                                group_id=group_id,
                                api_key=secret_change.value,
                                master_key=settings.config_master_key,
                                updated_by=int(user.id),
                            )
                            api_key_configured = True
                        else:
                            await clear_group_api_model_query_secret(
                                session, group_id=group_id
                            )
                            api_key_configured = False
                        stored_api_config = replace(
                            stored_api_config,
                            secret_version=stored_api_config.secret_version + 1,
                        )
                    updated_api_config = replace(
                        get_api_model_query_config(updated_settings),
                        api_key_configured=api_key_configured,
                        secret_version=stored_api_config.secret_version,
                    )
                    if updated_api_config.enabled and not updated_api_config.base_url:
                        raise _APIError(
                            400,
                            "api_model_query_not_configured",
                            "开启模型 API 查询前请先填写 Base URL。",
                        )
                    if updated_api_config.enabled and not api_key_configured:
                        raise _APIError(
                            400,
                            "api_model_query_key_required",
                            "开启模型 API 查询前请先配置 API Key。",
                        )
                    updated_settings = set_api_model_query_config(
                        updated_settings, updated_api_config
                    )
                current_style_target = int(
                    get_style_state(updated_settings).get("target_user_id") or 0
                )
                if current_style_target != previous_style_target:
                    await session.execute(
                        delete(SpeechStyleSample).where(
                            SpeechStyleSample.group_id == group_id
                        )
                    )
                if created_group:
                    group.settings = updated_settings
                    try:
                        await session.commit()
                    except IntegrityError as exc:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        ) from exc
                else:
                    changed = await session.execute(
                        update(Group)
                        .where(
                            Group.id == group_id,
                            Group.settings == stored_settings,
                        )
                        .values(settings=updated_settings)
                        .execution_options(synchronize_session=False)
                    )
                    if int(changed.rowcount or 0) != 1:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        )
                    await session.commit()
                document = _group_document(group.id, group.title, updated_settings)
        permission_apply: dict[str, Any] | None = None
        if permissions_changed:
            service = get_group_permission_service()
            config = document["settings"].get(GROUP_PERMISSIONS_SETTINGS_KEY)
            if service is not None and config is not None:
                applied = await service.apply_group_now(group_id, config)
                permission_apply = {
                    **service.status(group_id),
                    "applied": bool(applied),
                }
            elif service is not None:
                service.forget_group(group_id)
                permission_apply = {"applied": False, "removed": True}
            else:
                permission_apply = {"applied": False, "service_unavailable": True}
        return _success_response(
            {"group": document, "permission_apply": permission_apply}
        )

    @any_admin
    async def list_rules(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(ModerationRule)
                .where(ModerationRule.group_id == group_id)
                .order_by(ModerationRule.id)
            )).all()
        return _success_response({"rules": [_rule_document(row) for row in rows]})

    @any_admin
    async def create_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _RuleCreate.model_validate(await _json_object(request))
        row = ModerationRule(
            group_id=group_id,
            rule_type=body.rule_type,
            pattern=clean_multiline_text(body.pattern, max_len=1000).strip(),
            action=body.action,
            enabled=body.enabled,
            scan_scope=normalize_scan_scope(body.scan_scope),
        )
        if not row.pattern:
            raise _APIError(400, "empty_rule", "群规内容不能为空。")
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response({"rule": _rule_document(row)})

    @any_admin
    async def update_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        rule_id = _path_int(
            request, "rule_id", code="invalid_rule_id", message="群规 ID 无效。"
        )
        body = _RuleUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_rule_update", "至少需要修改一个字段。")
        def prepare(row: Any) -> None:
            if "rule_type" in body.model_fields_set:
                row.rule_type = str(body.rule_type)
            if "pattern" in body.model_fields_set:
                row.pattern = clean_multiline_text(str(body.pattern or ""), max_len=1000).strip()
                if not row.pattern:
                    raise _APIError(400, "empty_rule", "群规内容不能为空。")
            if "action" in body.model_fields_set:
                row.action = str(body.action)
            if "enabled" in body.model_fields_set:
                row.enabled = bool(body.enabled)
            if "scan_scope" in body.model_fields_set:
                row.scan_scope = normalize_scan_scope(body.scan_scope)

        outcome = await update_group_scoped_row(
            session_factory, model=ModerationRule, group_id=group_id, row_id=rule_id,
            prepare=prepare, document_factory=_rule_document,
        )
        if outcome.status == "not_found":
            raise _APIError(404, "rule_not_found", "群规不存在。")
        if outcome.status == "unavailable":
            raise _APIError(409, "resource_unavailable", "这条记录已不可用，未确认本次修改。请重新查看列表。")
        if outcome.status != "updated":
            log.warning("group resource update unconfirmed | reason=%s", outcome.detail)
            raise _APIError(503, "resource_write_unconfirmed", "未能确认这次修改是否已生效。请重新查看列表后再决定是否重试。")
        return _success_response({"status": "updated", "confirmed": True, "rule": outcome.document})

    @any_admin
    async def delete_rule(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        rule_id = _path_int(
            request, "rule_id", code="invalid_rule_id", message="群规 ID 无效。"
        )
        outcome = await delete_group_scoped_row(
            session_factory,
            model=ModerationRule,
            group_id=group_id,
            row_id=rule_id,
            operation="delete_rule",
        )
        return _group_scoped_delete_response(
            outcome,
            not_found=("rule_not_found", "群规不存在。"),
            log_label="moderation rule delete",
            group_id=group_id,
            row_id=rule_id,
        )

    @any_admin
    async def list_keyword_replies(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(KeywordReply)
                .where(KeywordReply.group_id == group_id)
                .order_by(KeywordReply.id)
            )).all()
        return _success_response(
            {"keyword_replies": [_keyword_reply_document(row) for row in rows]}
        )

    @any_admin
    async def create_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _KeywordReplyCreate.model_validate(await _json_object(request))
        reply_text = clean_multiline_text(body.reply_text, max_len=4000).strip()
        if not reply_text:
            raise _APIError(400, "empty_reply_text", "回复内容不能为空。")
        row = KeywordReply(
            group_id=group_id,
            keyword=_validated_keyword(body.keyword, body.match_type),
            match_type=body.match_type,
            reply_text=reply_text,
            buttons=_validated_template_buttons(body.buttons),
            pin_message=body.pin_message,
            auto_delete=body.auto_delete,
            disable_link_preview=body.disable_link_preview,
            enabled=body.enabled,
            created_by=int(user.id),
        )
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response({"keyword_reply": _keyword_reply_document(row)})

    @any_admin
    async def update_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        body = _KeywordReplyUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_update", "至少需要修改一个字段。")
        def prepare(row: Any) -> None:
            if "match_type" in body.model_fields_set:
                row.match_type = str(body.match_type)
            if "keyword" in body.model_fields_set:
                row.keyword = _validated_keyword(
                    str(body.keyword or ""), str(row.match_type)
                )
            elif "match_type" in body.model_fields_set:
                row.keyword = _validated_keyword(str(row.keyword), str(row.match_type))
            if "reply_text" in body.model_fields_set:
                row.reply_text = clean_multiline_text(
                    str(body.reply_text or ""), max_len=4000
                ).strip()
                if not row.reply_text:
                    raise _APIError(400, "empty_reply_text", "回复内容不能为空。")
            if "buttons" in body.model_fields_set:
                row.buttons = _validated_template_buttons(body.buttons or [])
            if "pin_message" in body.model_fields_set:
                row.pin_message = bool(body.pin_message)
            if "auto_delete" in body.model_fields_set:
                row.auto_delete = bool(body.auto_delete)
            if "disable_link_preview" in body.model_fields_set:
                row.disable_link_preview = bool(body.disable_link_preview)
            if "enabled" in body.model_fields_set:
                row.enabled = bool(body.enabled)

        outcome = await update_group_scoped_row(
            session_factory, model=KeywordReply, group_id=group_id, row_id=entry_id,
            prepare=prepare, document_factory=_keyword_reply_document,
        )
        if outcome.status == "not_found":
            raise _APIError(404, "keyword_reply_not_found", "关键词回复不存在。")
        if outcome.status == "unavailable":
            raise _APIError(409, "resource_unavailable", "这条记录已不可用，未确认本次修改。请重新查看列表。")
        if outcome.status != "updated":
            log.warning("group resource update unconfirmed | reason=%s", outcome.detail)
            raise _APIError(503, "resource_write_unconfirmed", "未能确认这次修改是否已生效。请重新查看列表后再决定是否重试。")
        return _success_response({"status": "updated", "confirmed": True, "keyword_reply": outcome.document})

    @any_admin
    async def delete_keyword_reply(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        outcome = await delete_group_scoped_row(
            session_factory,
            model=KeywordReply,
            group_id=group_id,
            row_id=entry_id,
            operation="delete_keyword_reply",
        )
        return _group_scoped_delete_response(
            outcome,
            not_found=("keyword_reply_not_found", "关键词回复不存在。"),
            log_label="keyword reply delete",
            group_id=group_id,
            row_id=entry_id,
        )

    @any_admin
    async def list_scheduled_messages(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(ScheduledMessage)
                .where(ScheduledMessage.group_id == group_id)
                .order_by(ScheduledMessage.id)
            )).all()
        return _success_response(
            {"scheduled_messages": [_scheduled_message_document(row) for row in rows]}
        )

    @any_admin
    async def create_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _ScheduledMessageCreate.model_validate(await _json_object(request))
        text = clean_multiline_text(body.text, max_len=4000).strip()
        if not text:
            raise _APIError(400, "empty_message_text", "消息内容不能为空。")
        row = ScheduledMessage(
            group_id=group_id,
            text=text,
            buttons=_validated_template_buttons(body.buttons),
            schedule_type=body.schedule_type,
            schedule_time=_validated_schedule_time(body.schedule_time),
            interval_minutes=int(body.interval_minutes),
            pin_message=body.pin_message,
            unpin_previous=body.unpin_previous,
            auto_delete=body.auto_delete,
            disable_link_preview=body.disable_link_preview,
            enabled=body.enabled,
            # Anchor at creation so a daily entry created after today's HH:MM
            # starts tomorrow instead of firing immediately.
            last_run_at=now_shanghai_naive(),
            created_by=int(user.id),
        )
        async with session_factory() as session:
            await _ensure_group_row(session, group_id)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _success_response(
            {"scheduled_message": _scheduled_message_document(row)}
        )

    @any_admin
    async def update_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        body = _ScheduledMessageUpdate.model_validate(await _json_object(request))
        if not body.model_fields_set:
            raise _APIError(400, "empty_update", "至少需要修改一个字段。")
        def prepare(row: Any) -> None:
            if "text" in body.model_fields_set:
                row.text = clean_multiline_text(str(body.text or ""), max_len=4000).strip()
                if not row.text:
                    raise _APIError(400, "empty_message_text", "消息内容不能为空。")
            if "buttons" in body.model_fields_set:
                row.buttons = _validated_template_buttons(body.buttons or [])
            if "schedule_type" in body.model_fields_set:
                row.schedule_type = str(body.schedule_type)
            if "schedule_time" in body.model_fields_set:
                row.schedule_time = _validated_schedule_time(str(body.schedule_time or ""))
            if "interval_minutes" in body.model_fields_set:
                row.interval_minutes = int(body.interval_minutes or 60)
            if "pin_message" in body.model_fields_set:
                row.pin_message = bool(body.pin_message)
            if "unpin_previous" in body.model_fields_set:
                row.unpin_previous = bool(body.unpin_previous)
            if "auto_delete" in body.model_fields_set:
                row.auto_delete = bool(body.auto_delete)
            if "disable_link_preview" in body.model_fields_set:
                row.disable_link_preview = bool(body.disable_link_preview)
            if "enabled" in body.model_fields_set:
                was_enabled = bool(row.enabled)
                row.enabled = bool(body.enabled)
                # Re-enabling anchors the next run to now instead of firing
                # immediately off a stale last_run_at; an edit that keeps the
                # entry enabled must not postpone the schedule.
                if row.enabled and not was_enabled:
                    row.last_run_at = now_shanghai_naive()

        outcome = await update_group_scoped_row(
            session_factory, model=ScheduledMessage, group_id=group_id, row_id=entry_id,
            prepare=prepare, document_factory=_scheduled_message_document,
        )
        if outcome.status == "not_found":
            raise _APIError(404, "scheduled_message_not_found", "定时消息不存在。")
        if outcome.status == "unavailable":
            raise _APIError(409, "resource_unavailable", "这条记录已不可用，未确认本次修改。请重新查看列表。")
        if outcome.status != "updated":
            log.warning("group resource update unconfirmed | reason=%s", outcome.detail)
            raise _APIError(503, "resource_write_unconfirmed", "未能确认这次修改是否已生效。请重新查看列表后再决定是否重试。")
        return _success_response({"status": "updated", "confirmed": True, "scheduled_message": outcome.document})

    @any_admin
    async def delete_scheduled_message(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        entry_id = _path_int(
            request, "entry_id", code="invalid_entry_id", message="记录 ID 无效。"
        )
        outcome = await delete_group_scoped_row(
            session_factory,
            model=ScheduledMessage,
            group_id=group_id,
            row_id=entry_id,
            operation="delete_scheduled_message",
        )
        return _group_scoped_delete_response(
            outcome,
            not_found=("scheduled_message_not_found", "定时消息不存在。"),
            log_label="scheduled message delete",
            group_id=group_id,
            row_id=entry_id,
        )

    @any_admin
    async def read_memory_transparency(request: web.Request, user: Any) -> web.Response:
        raw_group = request.match_info.get("id", "")
        if not re.fullmatch(r"-?[1-9][0-9]*", raw_group) or len(raw_group) > 20:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。")
        group_id = _group_id(request)
        if not -9223372036854775808 <= group_id <= 9223372036854775807:
            raise _APIError(400, "invalid_group_id", "群 ID 无效。")
        await _require_group_access(group_id, int(user.id))
        section = request.match_info.get("section", "")
        allowed = {"limit", "cursor"}
        if section == "facts":
            allowed |= {"subject", "category"}
        elif section == "searches":
            allowed |= {"kind"}
        elif section == "summary":
            allowed = set()
        else:
            raise _APIError(404, "memory_section_not_found", "记忆分节不存在。")
        query = request.query
        if any(key not in allowed or len(query.getall(key)) != 1 for key in query):
            raise _APIError(400, "invalid_memory_query", "记忆筛选参数无效。")
        def number(key, default, minimum, maximum):
            raw = query.get(key)
            if raw is None:
                return default
            if not re.fullmatch(r"0|[1-9][0-9]*", raw) or len(raw) > 19:
                raise _APIError(400, "invalid_memory_query", "记忆筛选参数无效。")
            value = int(raw)
            if not minimum <= value <= maximum:
                raise _APIError(400, "invalid_memory_query", "记忆筛选参数无效。")
            return value
        limit = number("limit", 20, 1, 50)
        cursor = number("cursor", None, 1, 9223372036854775807)
        subject = number("subject", None, 0, 9223372036854775807)
        category = query.get("category")
        kind = query.get("kind")
        if category is not None and category not in {"identity", "preference", "relationship", "event", "taboo", "skill", "other"}:
            raise _APIError(400, "invalid_memory_query", "记忆筛选参数无效。")
        if kind is not None and kind not in {"price", "news", "fact", "unknown"}:
            raise _APIError(400, "invalid_memory_query", "记忆筛选参数无效。")
        try:
            if section == "facts":
                result = await memory_transparency.facts(session_factory, group_id, limit=limit, cursor=cursor, subject=subject, category=category)
            elif section == "searches":
                result = await memory_transparency.searches(session_factory, settings, group_id, limit=limit, cursor=cursor, kind=kind)
            else:
                result = await memory_transparency.summary(session_factory, settings, group_id)
        except Exception:
            log.warning("memory transparency unavailable | section=%s", section)
            raise _APIError(503, "memory_read_unavailable", "记忆读取失败；当前状态不可验证，请显式刷新。") from None
        return _success_response({"group_id": group_id, "section": section, **result})

    def _memory_fact_path_id(request: web.Request) -> int:
        """``{fact_id}`` 稳定 ID：严格十进制正整数（拒 ``01``/``+1``/``1.0``/全角）。"""

        raw = request.match_info.get("fact_id", "")
        if not re.fullmatch(r"[1-9][0-9]*", raw or "") or len(raw) > 19:
            raise _APIError(400, "invalid_fact_id", "事实 ID 无效。")
        value = int(raw)
        if value > 9223372036854775807:
            raise _APIError(400, "invalid_fact_id", "事实 ID 无效。")
        return value

    @any_admin
    async def read_memory_fact_source(
        request: web.Request, user: Any
    ) -> web.Response:
        """C3 只读：按稳定 ID 显式展开**本群**来源原文（受控、三态可区分）。

        只读、不调模型、不写库、不发送；作用域强制为本群 ``group``，
        所以跨群/私聊 ID 与「不存在」同形（不返回任何暗示私有内容的计数）。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        fact_id = _memory_fact_path_id(request)
        try:
            async with session_factory() as session:
                expansion = await expand_fact_source(
                    session, scope="group", scope_id=group_id, fact_id=fact_id
                )
        except Exception:
            # 读取故障 fail-closed：明确「不可验证」，不猜、不拿 evidence_excerpt 兜底。
            log.warning(
                "memory source unavailable | group_id=%s | fact_id=%s",
                group_id,
                fact_id,
            )
            raise _APIError(
                503,
                "memory_source_unavailable",
                "来源读取失败；当前状态不可验证，请显式刷新后再试。",
            ) from None
        return _success_response(
            {
                "group_id": group_id,
                "fact_id": fact_id,
                "status": expansion.status,
                "available": expansion.available,
                "reason": expansion.reason or None,
                "revision": expansion.revision,
                "corrected": expansion.corrected,
                "source_kind": expansion.source_kind,
                "message_id": expansion.message_id,
                "thread_id": expansion.thread_id,
                "url": expansion.url or None,
                "link_kind": expansion.link_kind or None,
                "text": expansion.text if expansion.available else "",
                "truncated": expansion.truncated,
            }
        )

    @any_admin
    async def correct_memory_fact(request: web.Request, user: Any) -> web.Response:
        """C3 人工纠错：一条原子事务 + revision CAS；不自动重试、不补偿 commit。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        fact_id = _memory_fact_path_id(request)
        body = await _json_object(request)
        if set(body) - {"text", "expected_revision"}:
            raise _APIError(400, "invalid_correction", "纠错参数无效。")
        text = body.get("text")
        revision = body.get("expected_revision")
        if (
            not isinstance(text, str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
            or revision > 9223372036854775807
        ):
            raise _APIError(400, "invalid_correction", "纠错参数无效。")
        try:
            async with session_factory() as session:
                outcome = await correct_fact(
                    session,
                    scope="group",
                    scope_id=group_id,
                    subject_user_id=None,
                    fact_id=fact_id,
                    expected_revision=revision,
                    new_text=text,
                    operator_user_id=int(user.id),
                    operator_kind=CORRECTION_OPERATOR_ADMIN,
                )
        except MemorySessionCleanupUnsafeError:
            # 清理无法证明完成 ⇒ 残留 DML 可能还在会话上。绝不正常返回让中间件补写。
            log.warning(
                "memory fact correction cleanup unsafe | group_id=%s | fact_id=%s",
                group_id,
                fact_id,
            )
            raise _APIError(
                503, "memory_write_unconfirmed", MEMORY_CORRECTION_UNCONFIRMED_MESSAGE
            ) from None
        except Exception:
            log.warning(
                "memory fact correction unavailable | group_id=%s | fact_id=%s",
                group_id,
                fact_id,
            )
            raise _APIError(
                503, "memory_write_unconfirmed", MEMORY_CORRECTION_UNCONFIRMED_MESSAGE
            ) from None
        if outcome.status == CORRECTION_INVALID:
            raise _APIError(
                400,
                "invalid_correction_text",
                _MEMORY_CORRECTION_INVALID_MESSAGES.get(
                    outcome.reason, "这条内容不能写入长期记忆。"
                ),
                details={"reason": outcome.reason or REASON_BAD_ARGUMENT},
            )
        if outcome.status == CORRECTION_CONFLICT:
            raise _APIError(
                409,
                "memory_fact_conflict",
                MEMORY_CORRECTION_CONFLICT_MESSAGE,
                details={
                    "conflict": outcome.conflict or "conflict",
                    "reason": outcome.reason or None,
                    "previous_fact_id": outcome.previous_fact_id,
                    "previous_revision": outcome.previous_revision,
                },
            )
        if outcome.status == CORRECTION_UNCONFIRMED:
            raise _APIError(
                503, "memory_write_unconfirmed", MEMORY_CORRECTION_UNCONFIRMED_MESSAGE
            )
        if outcome.status == CORRECTION_UNCHANGED:
            return _success_response(
                {
                    "status": "unchanged",
                    "fact_id": fact_id,
                    "revision": outcome.previous_revision or revision,
                    "confirmed": False,
                    "message": "内容没有变化，这次没有改动任何记忆。",
                }
            )
        if outcome.status != CORRECTION_CORRECTED or not outcome.confirmed:
            # commit 已经返回但读回不一致：写很可能是 durable 的，绝不承诺"没有效果"。
            raise _APIError(
                503, "memory_write_unconfirmed", MEMORY_CORRECTION_UNCONFIRMED_MESSAGE
            )
        log.info(
            "memory fact corrected | group_id=%s | old_fact_id=%s | old_revision=%s"
            " | new_fact_id=%s | revision=%s | operator=%s | operator_kind=%s",
            group_id,
            outcome.previous_fact_id,
            outcome.previous_revision,
            outcome.fact_id,
            outcome.revision,
            int(user.id),
            CORRECTION_OPERATOR_ADMIN,
        )
        return _success_response(
            {
                "status": "corrected",
                "confirmed": True,
                "fact_id": outcome.fact_id,
                "revision": outcome.revision,
                "previous_fact_id": outcome.previous_fact_id,
                "previous_revision": outcome.previous_revision,
                "corrected": True,
                "message": (
                    f"已更新这条长期事实（新 ID {outcome.fact_id} · 修订 "
                    f"{outcome.revision}）；旧版本保留为被替换并写入审计，"
                    "聊天原文与来源关系未被改动。"
                ),
            }
        )

    @any_admin
    async def list_memories(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        async with session_factory() as session:
            rows = (await session.scalars(
                select(GroupPermanentMemory)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id)
            )).all()
        return _success_response({"memories": [_memory_document(row) for row in rows]})

    @any_admin
    async def group_operations_task_status(
        request: web.Request, user: Any
    ) -> web.Response:
        """只读：本群六类运营任务的执行来源与可证明的状态。

        只做读取与纯计算，不初始化任务、不补跑、不发送、不写库；外部 cron 的登记
        情况不在程序可观测范围内，一律以 ``null`` + ``reason`` 返回，绝不猜。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            task_status = await collect_operations_task_status(
                session_factory, group_id=group_id
            )
        except _APIError:
            raise
        except Exception:
            # 明确不可用 > 静默给「全绿」。
            log.exception(
                "operations task status unavailable | group_id=%s", group_id
            )
            raise _APIError(
                503,
                "operations_status_unavailable",
                "运营任务状态暂时不可用，请稍后重试。",
            )
        return _success_response({"task_status": task_status})

    # ------------------------------------------------------------------
    # B13a：运营任务中心（写侧）。预览只读；确认是单次令牌 + 原子守卫；
    # run 是持久审计；取消/重试/对账都不越过"未知外部结果"的硬边界。
    # ------------------------------------------------------------------

    def _operations_actor_role(user: Any) -> str:
        if settings.super_admin_id and int(getattr(user, "id", 0) or 0) == int(
            settings.super_admin_id
        ):
            return "superadmin"
        return "admin"

    def _operations_path_text(request: web.Request, name: str, *, code: str) -> str:
        raw = str(request.match_info.get(name) or "").strip()
        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", raw):
            raise _APIError(404, code, "没有这次执行记录")
        return raw

    def _operations_query_int(
        request: web.Request, name: str, *, default: int, minimum: int, maximum: int
    ) -> int:
        raw = request.query.get(name)
        if raw is None or raw == "":
            return int(default)
        if not re.fullmatch(r"[0-9]{1,19}", str(raw)):
            raise _APIError(400, "invalid_query", f"{name} 必须是整数")
        value = int(raw)
        if value < minimum or value > maximum:
            raise _APIError(400, "invalid_query", f"{name} 超出允许范围")
        return value

    @any_admin
    async def operations_task_catalog(request: web.Request, user: Any) -> web.Response:
        """本群可用的六类任务（只读目录；只为 B13b 的前端提供能力清单）。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        operations_runs.ensure_tasks_loaded()
        tasks = [
            {"task_id": task_id, "title": operations_runs.get_task(task_id).title}
            for task_id in operations_runs.registered_task_ids()
        ]
        return _success_response(
            {
                "tasks": tasks,
                "preview_ttl_seconds": operations_runs.preview_ttl_seconds(settings),
                "actor_role": _operations_actor_role(user),
            }
        )

    @any_admin
    async def preview_operations_task(request: web.Request, user: Any) -> web.Response:
        """只读预览：不写审计、不占位、不发消息、不调模型、不动积分。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"task_id", "params"}
        if unknown:
            raise _APIError(400, "invalid_preview", "预览请求包含未知字段")
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise _APIError(400, "invalid_preview", "task_id 必须是字符串")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise _APIError(400, "invalid_preview", "params 必须是对象")
        try:
            preview = await operations_runs.preview_task(
                session_factory,
                settings,
                manager,
                task_id=task_id.strip(),
                group_id=group_id,
                actor_user_id=int(user.id),
                actor_role=_operations_actor_role(user),
                params=params,
                bot=bot,
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"preview": preview})

    @any_admin
    async def confirm_operations_task(request: web.Request, user: Any) -> web.Response:
        """确认执行：单次令牌 + 写事务内复核守卫；ack 不确定时如实回报未知。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"task_id", "token"}
        if unknown:
            raise _APIError(400, "invalid_confirmation", "确认请求包含未知字段")
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise _APIError(400, "invalid_confirmation", "token 必须是字符串")
        try:
            payload = await operations_runs.confirm_run(
                session_factory,
                settings,
                manager,
                token=token.strip(),
                actor_user_id=int(user.id),
                actor_role=_operations_actor_role(user),
                bot=bot,
                group_id=group_id,
                task_id=str(body.get("task_id") or "").strip() or None,
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        except Exception:
            # 提交结果不确定：既不声称成功，也绝不声称"没执行"（更不会自动补偿重试）。
            log.exception(
                "operations confirm unconfirmed | group_id=%s | actor=%s",
                group_id,
                int(user.id),
            )
            raise _APIError(
                503,
                "operations_confirm_unconfirmed",
                "这次确认的结果无法确认：可能已经受理。请不要重复提交，先刷新执行记录核对。",
            )
        return _success_response({"run": payload})

    @any_admin
    async def list_operations_runs(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        limit = _operations_query_int(
            request, "limit", default=20, minimum=1, maximum=50
        )
        cursor = request.query.get("cursor") or None
        if cursor is not None and not re.fullmatch(r"[0-9]{1,19}", str(cursor)):
            raise _APIError(400, "invalid_query", "cursor 必须是整数")
        payload = await operations_runs.list_runs(
            session_factory,
            group_id=group_id,
            limit=limit,
            cursor=None if cursor is None else int(cursor),
        )
        return _success_response(payload)

    @any_admin
    async def read_operations_run(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_key = _operations_path_text(
            request, "run_id", code="operations_run_not_found"
        )
        wait = str(request.query.get("wait") or "")
        try:
            if wait in ("1", "true", "yes"):
                payload = await operations_runs.await_run(
                    session_factory, run_key=run_key, group_id=group_id
                )
            else:
                payload = await operations_runs.read_run(
                    session_factory, run_key=run_key, group_id=group_id
                )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"run": payload})

    @any_admin
    async def cancel_operations_run(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_key = _operations_path_text(
            request, "run_id", code="operations_run_not_found"
        )
        try:
            payload = await operations_runs.request_cancel(
                session_factory, run_key=run_key, group_id=group_id
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"run": payload})

    @any_admin
    async def retry_operations_run(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_key = _operations_path_text(
            request, "run_id", code="operations_run_not_found"
        )
        try:
            payload = await operations_runs.retry_run(
                session_factory,
                settings,
                manager,
                run_key=run_key,
                group_id=group_id,
                actor_user_id=int(user.id),
                actor_role=_operations_actor_role(user),
                bot=bot,
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"run": payload})

    @any_admin
    async def reconcile_operations_run(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_key = _operations_path_text(
            request, "run_id", code="operations_run_not_found"
        )
        try:
            payload = await operations_runs.reconcile_run(
                session_factory, run_key=run_key, group_id=group_id
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"run": payload})

    # ------------------------------------------------------------------
    # B13b：按群执行来源与周期计划。读是只读；变更是"预览 → 明确确认 → CAS 持久
    # 读回"，令牌绑定用户/群/参数/版本/有效期；审计有界分页且只本群。
    # ------------------------------------------------------------------

    @any_admin
    async def read_operations_policy(request: web.Request, user: Any) -> web.Response:
        """只读：本群六类任务的执行来源与周期计划（含默认值、可切换性、下次时刻）。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        try:
            async with session_factory() as session:
                views = await task_policy.load_policies(session, group_id=group_id)
        except _APIError:
            raise
        except Exception:
            log.exception("operations policy unavailable | group_id=%s", group_id)
            raise _APIError(
                503,
                "policy_unavailable",
                "执行来源与周期计划暂时读不到，请稍后重试。",
            )
        tasks = []
        for task_id in task_policy.POLICY_TASK_IDS:
            document = task_policy.policy_payload(views[task_id])
            document["title"] = TASK_TITLES.get(task_id, task_id)
            document["timezone"] = "Asia/Shanghai"
            tasks.append(document)
        return _success_response(
            {
                "group_id": group_id,
                "timezone": "Asia/Shanghai",
                "external_schedule_known": False,
                "external_schedule_reason": EXTERNAL_SCHEDULE_UNKNOWN,
                "tasks": tasks,
            }
        )

    @any_admin
    async def preview_operations_policy(
        request: web.Request, user: Any
    ) -> web.Response:
        """只读预览：不写库、不调模型、不发送、不动积分；只有会真变的才给令牌。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {
            "task_id",
            "mode",
            "source",
            "enabled",
            "weekday",
            "minute",
        }
        if unknown:
            raise _APIError(400, "invalid_preview", "预览请求包含未知字段")
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise _APIError(400, "invalid_preview", "task_id 必须是字符串")
        mode = body.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise _APIError(400, "invalid_preview", "mode 必须是字符串")
        try:
            preview = await task_policy.preview_policy(
                session_factory,
                settings,
                group_id=group_id,
                task_id=task_id.strip(),
                mode=mode.strip(),
                source=body.get("source"),
                enabled=body.get("enabled"),
                weekday=body.get("weekday"),
                minute=body.get("minute"),
                actor_user_id=int(user.id),
                actor_role=_operations_actor_role(user),
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        return _success_response({"preview": preview})

    @any_admin
    async def confirm_operations_policy(
        request: web.Request, user: Any
    ) -> web.Response:
        """确认来源/计划变更：令牌校验 + 同一条 SQL 的 CAS；ack 未知不假成功。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"token"}
        if unknown:
            raise _APIError(400, "invalid_confirmation", "确认请求包含未知字段")
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise _APIError(400, "invalid_confirmation", "token 必须是字符串")
        try:
            payload = await task_policy.confirm_policy(
                session_factory,
                settings,
                token=token.strip(),
                group_id=group_id,
                actor_user_id=int(user.id),
                actor_role=_operations_actor_role(user),
            )
        except OperationsError as exc:
            raise _APIError(exc.status, exc.code, exc.message, details=exc.details)
        except Exception:
            log.exception(
                "operations policy confirm unconfirmed | group_id=%s | actor=%s",
                group_id,
                int(user.id),
            )
            raise _APIError(
                503,
                "policy_confirm_unconfirmed",
                "这次确认的结果无法确认：可能已经生效。请不要重复提交，先刷新执行来源核对。",
            )
        return _success_response(payload)

    @any_admin
    async def read_operations_policy_audit(
        request: web.Request, user: Any
    ) -> web.Response:
        """本群策略变更审计（有界 keyset 分页，只读，不返回别群数据）。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        limit = _operations_query_int(
            request, "limit", default=20, minimum=1, maximum=50
        )
        cursor = request.query.get("cursor") or None
        if cursor is not None and not re.fullmatch(r"[0-9]{1,19}", str(cursor)):
            raise _APIError(400, "invalid_query", "cursor 必须是整数")
        task_filter = request.query.get("task_id") or None
        if task_filter is not None and not re.fullmatch(
            r"[0-9a-zA-Z_-]{1,64}", str(task_filter)
        ):
            raise _APIError(400, "invalid_query", "task_id 不合法")
        payload = await task_policy.read_policy_audit(
            session_factory,
            group_id=group_id,
            limit=limit,
            cursor=None if cursor is None else int(cursor),
            task_id=task_filter,
        )
        return _success_response(payload)

    @any_admin
    async def create_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _MemoryCreate.model_validate(await _json_object(request))
        content = clean_multiline_text(body.content, max_len=4000).strip()
        if not content:
            raise _APIError(400, "empty_memory", "永久记忆不能为空。")
        async with _pm_create_session(session_factory) as session:
            await _ensure_group_row(session, group_id)
            existing = await session.scalar(
                select(GroupPermanentMemory).where(
                    GroupPermanentMemory.group_id == group_id,
                    GroupPermanentMemory.content == content,
                )
            )
            if existing is not None:
                return _success_response({"memory": _memory_document(existing)})
            row = GroupPermanentMemory(
                group_id=group_id,
                content=content,
                created_by=int(user.id),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # 并发同内容新建：唯一索引挡下了第二行。读回既有行按幂等形状返回，
                # 而不是把 IntegrityError 变成 500。读不到才说明是别的完整性错误。
                await session.rollback()
                existing = await session.scalar(
                    select(GroupPermanentMemory).where(
                        GroupPermanentMemory.group_id == group_id,
                        GroupPermanentMemory.content == content,
                    )
                )
                if existing is None:
                    raise
                return _success_response({"memory": _memory_document(existing)})
            await session.refresh(row)
        return _success_response({"memory": _memory_document(row)})

    @any_admin
    async def update_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        memory_id = _path_int(
            request, "memory_id", code="invalid_memory_id", message="记忆 ID 无效。"
        )
        body = _MemoryCreate.model_validate(await _json_object(request))
        content = clean_multiline_text(body.content, max_len=4000).strip()
        if not content:
            raise _APIError(400, "empty_memory", "永久记忆不能为空。")
        # 会话生命周期交给服务层的有界 helper：commit 正常返回之后的 close 故障
        # 不能把已经确认的写入改写成 5xx（数据真值与资源归还分开上报）。
        outcome = await update_permanent_memory_row(
            session_factory,
            group_id=group_id,
            memory_id=memory_id,
            content=content,
            document_factory=_memory_document,
        )
        if outcome.status == PM_WRITE_NOT_FOUND:
            raise _APIError(404, "memory_not_found", "永久记忆不存在。")
        if outcome.status == PM_WRITE_UNAVAILABLE:
            log.warning(
                "permanent memory update unavailable | group_id=%s | memory_id=%s",
                group_id,
                memory_id,
            )
            raise _APIError(409, "memory_unavailable", PM_WRITE_UNAVAILABLE_MESSAGE)
        if outcome.status == PM_WRITE_UNCONFIRMED:
            log.warning(
                "permanent memory update unconfirmed | group_id=%s | memory_id=%s | reason=%s",
                group_id,
                memory_id,
                outcome.detail or "unknown",
            )
            raise _APIError(503, "memory_write_unconfirmed", PM_WRITE_UNCONFIRMED_MESSAGE)
        return _success_response(
            {"memory": outcome.document, "status": PM_WRITE_UPDATED}
        )

    @any_admin
    async def delete_memory(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        memory_id = _path_int(
            request, "memory_id", code="invalid_memory_id", message="记忆 ID 无效。"
        )
        # 初始本群预读只用来判 404（他群同形，不区分、不泄漏）；真正的删除是
        # 一条 `WHERE group_id AND id` 的原子 DML，按真实 rowcount 判 0/1。
        outcome = await delete_permanent_memory_row(
            session_factory, group_id=group_id, memory_id=memory_id
        )
        if outcome.status == PM_WRITE_NOT_FOUND:
            raise _APIError(404, "memory_not_found", "永久记忆不存在。")
        if outcome.status == PM_WRITE_UNCONFIRMED:
            log.warning(
                "permanent memory delete unconfirmed | group_id=%s | memory_id=%s | reason=%s",
                group_id,
                memory_id,
                outcome.detail or "unknown",
            )
            raise _APIError(503, "memory_write_unconfirmed", PM_WRITE_UNCONFIRMED_MESSAGE)
        if outcome.status == PM_WRITE_DELETED:
            return _success_response(
                {"deleted": True, "changed": True, "status": PM_WRITE_DELETED}
            )
        return _success_response(
            {"deleted": False, "changed": False, "status": PM_WRITE_NO_CHANGE}
        )

    async def _list_user_rows(
        request: web.Request,
        user: Any,
        model: type[Any],
        key: str,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        # A-04: 这些列表曾经完全没有 LIMIT，一次把整群记录拉进内存再序列化成
        # 单个 JSON；与同文件全局端点的 limit<=500 对齐。响应多带一个
        # next_offset，调用方按游标翻页即可拿全量。
        limit, offset = _page_window(request)
        async with session_factory() as session:
            stmt = select(model).where(model.group_id == group_id).order_by(model.user_id)
            if model is UserWarning:
                stmt = stmt.where(
                    or_(UserWarning.count > 0, UserWarning.is_banned.is_(True))
                )
            rows = (await session.scalars(stmt.offset(offset).limit(limit))).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
            # 只读端点不写库：只读语义的 GET 不该开写事务和锁。
            persist=False,
        )
        return _success_response(
            {
                key: [
                    _user_policy_document(row, members.get(int(row.user_id)))
                    for row in rows
                ],
                "next_offset": offset + len(rows) if len(rows) == limit else None,
            }
        )

    @any_admin
    async def list_warnings(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, UserWarning, "warnings")

    @any_admin
    async def clear_warning(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        async with session_factory() as session:
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            ))
            if row is not None and row.is_banned:
                raise _APIError(
                    409,
                    "user_is_banned",
                    "该用户已被群内封禁，请先执行解封。",
                )
            if row is not None:
                await session.delete(row)
            await session.commit()
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_group_bans(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        # A-04: 同 _list_user_rows —— 分页 + 只读，不再一次拉全群封禁历史。
        limit, offset = _page_window(request)
        async with session_factory() as session:
            rows = (await session.scalars(
                select(UserWarning)
                .where(
                    UserWarning.group_id == group_id,
                    UserWarning.is_banned == True,  # noqa: E712
                )
                .order_by(UserWarning.count.desc(), UserWarning.user_id.asc())
                .offset(offset)
                .limit(limit)
            )).all()
        members = await _group_member_map(
            session_factory,
            group_id,
            [int(row.user_id) for row in rows],
            bot_obj=bot,
            persist=False,
        )
        return _success_response(
            {
                "bans": [
                    _ban_document(row, members.get(int(row.user_id)))
                    for row in rows
                ],
                "next_offset": offset + len(rows) if len(rows) == limit else None,
            }
        )

    @any_admin
    async def create_group_ban(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _UserIdCreate.model_validate(await _json_object(request))
        if settings.super_admin_id and body.user_id == int(settings.super_admin_id):
            raise _APIError(400, "cannot_ban_owner", "不能封禁最高管理员。")
        ban_member = getattr(bot, "ban_chat_member", None)
        if not callable(ban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 封禁接口。")
        prompts: set[tuple[int, int]] = set()
        private_prompts: set[tuple[int, int]] = set()
        async with session_factory() as session:
            if await is_globally_banned(session, body.user_id):
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "该用户的封禁状态只能由最高管理员处理。",
                )
            verification = await get_join_verification(
                session, group_id, body.user_id
            )
            if verification is not None and int(verification.prompt_message_id or 0) > 0:
                prompts.add((group_id, int(verification.prompt_message_id)))
            if verification is not None and int(
                getattr(verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(body.user_id), int(verification.private_message_id))
                )
            recovery = await lease_join_verification_for_unban(
                session,
                group_id,
                body.user_id,
                manual_unban=False,
            )
            if recovery is None:
                await session.rollback()
                raise _APIError(
                    503,
                    "group_ban_recovery_unavailable",
                    "无法建立封禁恢复工单，请立即重试。",
                )
            await session.commit()
        try:
            result = await enforce_ban_member(bot, group_id, body.user_id)
            if not result:
                raise RuntimeError("Telegram returned false")
        except Exception as exc:
            try:
                async with session_factory() as session:
                    await record_ban_event(
                        session,
                        group_id=group_id,
                        target_user_id=body.user_id,
                        action="ban",
                        source="miniapp_group",
                        outcome="failed",
                        reason="Mini App 管理员手动封禁",
                        actor_user_id=int(user.id),
                        details={"telegram_error": type(exc).__name__},
                    )
                    await session.commit()
            except Exception:
                log.exception(
                    "web group ban failure audit write failed | group=%s user=%s",
                    group_id,
                    body.user_id,
                )
            raise _APIError(502, "telegram_ban_failed", "Telegram 群内封禁失败，请稍后重试。") from exc
        async with session_factory() as session:
            claimed = await complete_leased_join_verification(
                session,
                verification_id=int(recovery.verification_id),
                lease_until=recovery.lease_until,
                status="unbanning",
            )
            if not claimed:
                await session.rollback()

                async def preserve_latest_ban() -> bool:
                    async with session_factory() as policy_session:
                        return bool(
                            await verification_release_blocked_by_ban(
                                policy_session,
                                group_id=group_id,
                                user_id=body.user_id,
                            )
                        )

                async def preserve_latest_restriction() -> bool:
                    async with session_factory() as policy_session:
                        return bool(
                            await verification_restriction_required(
                                policy_session,
                                group_id=group_id,
                                user_id=body.user_id,
                            )
                        )

                try:
                    await reconcile_moderation_ban_after_lost_lease(
                        bot,
                        group_id,
                        body.user_id,
                        preserve_latest_ban,
                        restriction_required=preserve_latest_restriction,
                    )
                except Exception:
                    log.exception(
                        "web group ban superseded-state reconciliation failed | "
                        "group=%s user=%s",
                        group_id,
                        body.user_id,
                    )
                raise _APIError(
                    409,
                    "group_ban_state_changed",
                    "封禁期间权限策略已被其他操作更新，已按最新策略校准。",
                )
            row = await _set_group_banned_after_telegram(
                session,
                group_id=group_id,
                user_id=body.user_id,
            )
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=body.user_id,
                action="ban",
                source="miniapp_group",
                outcome="succeeded",
                reason="Mini App 管理员手动封禁",
                actor_user_id=int(user.id),
            )
            await session.commit()
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        return _success_response({"ban": _ban_document(row)})

    @any_admin
    async def delete_group_ban(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        prompts: set[tuple[int, int]] = set()
        private_prompts: set[tuple[int, int]] = set()
        async with session_factory() as session:
            if await is_globally_banned(session, target_id):
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "该用户的封禁状态只能由最高管理员处理。",
                )
            row = await session.scalar(select(UserWarning).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == target_id,
            ))
            warning_snapshot = (
                (
                    int(row.id),
                    max(0, int(row.count or 0)),
                    bool(row.is_banned),
                )
                if row is not None and bool(row.is_banned)
                else None
            )
            verification = await get_join_verification(session, group_id, target_id)
            if verification is not None and int(verification.prompt_message_id or 0) > 0:
                prompts.add((group_id, int(verification.prompt_message_id)))
            if verification is not None and int(
                getattr(verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(target_id), int(verification.private_message_id))
                )
            recovery = await lease_join_verification_for_unban(
                session,
                group_id,
                target_id,
            )
            await session.commit()
        activate_manual_unban_recovery(recovery)
        unban_member = getattr(bot, "unban_chat_member", None)
        if not callable(unban_member):
            raise _APIError(503, "telegram_unavailable", "当前无法调用 Telegram 解封接口。")
        try:
            result = await enforce_unban_member(bot, group_id, target_id)
            if not result:
                raise RuntimeError("Telegram returned false")
        except Exception as exc:
            raise _APIError(502, "telegram_unban_failed", "Telegram 群内解封失败，请稍后重试。") from exc
        async with session_factory() as session:
            if await is_globally_banned(session, target_id):
                await session.rollback()
                try:
                    await enforce_ban_member(bot, group_id, target_id)
                except Exception:
                    log.exception(
                        "web group unban global-race reban failed | group=%s user=%s",
                        group_id,
                        target_id,
                    )
                raise _APIError(
                    409,
                    "group_ban_locked",
                    "解封期间该用户被加入全局封禁名单，已保留封禁状态。",
                )
            if warning_snapshot is None:
                current = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                    )
                )
                claimed = current is None or not bool(current.is_banned)
            else:
                warning_id, previous_count, was_banned = warning_snapshot
                deleted = await session.execute(
                    delete(UserWarning).where(
                        UserWarning.id == warning_id,
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                        UserWarning.count == previous_count,
                        UserWarning.is_banned.is_(was_banned),
                    )
                )
                claimed = int(deleted.rowcount or 0) == 1
            if not claimed:
                await session.rollback()
                current = await session.scalar(
                    select(UserWarning).where(
                        UserWarning.group_id == group_id,
                        UserWarning.user_id == target_id,
                    )
                )
                current_banned = current is not None and bool(current.is_banned)
                await session.rollback()
                if current_banned:
                    try:
                        await enforce_ban_member(bot, group_id, target_id)
                    except Exception:
                        log.exception(
                            "web group unban concurrent-state reban failed | group=%s user=%s",
                            group_id,
                            target_id,
                        )
                raise _APIError(
                    409,
                    "group_ban_state_changed",
                    "解封期间警告或封禁状态已被其他管理操作更新，请刷新后重试。",
                )
            current_verification = await get_join_verification(
                session, group_id, target_id
            )
            if (
                current_verification is not None
                and int(current_verification.prompt_message_id or 0) > 0
            ):
                prompts.add(
                    (group_id, int(current_verification.prompt_message_id))
                )
            if current_verification is not None and int(
                getattr(current_verification, "private_message_id", 0) or 0
            ) > 0:
                private_prompts.add(
                    (int(target_id), int(current_verification.private_message_id))
                )
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                try:
                    await enforce_ban_member(bot, group_id, target_id)
                except Exception:
                    log.exception(
                        "web group unban database-failure reban failed | group=%s user=%s",
                        group_id,
                        target_id,
                    )
                raise _APIError(
                    500,
                    "group_unban_state_failed",
                    "Telegram 已解封，但数据库更新失败；已尝试恢复封禁，请重试。",
                ) from exc
        await delete_verification_prompts(bot, prompts)
        await close_private_challenge_messages(bot, private_prompts)
        restored = await restore_member_permissions(bot, group_id, target_id)
        if restored and recovery is not None:
            async with session_factory() as completion_session:
                completed = await complete_leased_join_verification(
                    completion_session,
                    verification_id=int(recovery.verification_id),
                    lease_until=recovery.lease_until,
                    status="unbanning",
                )
                if completed:
                    await completion_session.commit()
                else:
                    await completion_session.rollback()
        try:
            async with session_factory() as session:
                await record_ban_event(
                    session,
                    group_id=group_id,
                    target_user_id=target_id,
                    action="unban",
                    source="miniapp_group",
                    outcome="succeeded",
                    reason="Mini App 管理员解除群内封禁",
                    actor_user_id=int(user.id),
                    details={"permissions_restored": bool(restored)},
                )
                await session.commit()
        except Exception:
            log.exception(
                "web group unban audit write failed | group=%s user=%s",
                group_id,
                target_id,
            )
        return _success_response({
            "deleted": warning_snapshot is not None,
            "restored": bool(restored),
        })

    async def _create_user_row(
        request: web.Request,
        user: Any,
        model: type[Any],
        key: str,
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = _UserIdCreate.model_validate(await _json_object(request))
        recovery = None
        released = True
        async with session_factory() as session:
            row = await session.scalar(select(model).where(
                model.group_id == group_id,
                model.user_id == body.user_id,
            ))
            if row is None:
                row = model(group_id=group_id, user_id=body.user_id, created_by=int(user.id))
                session.add(row)
            if model is ModerationExemption:
                recovery = await lease_join_verification_for_unban(
                    session,
                    group_id,
                    body.user_id,
                    manual_unban=False,
                )
                if recovery is None:
                    await session.rollback()
                    raise _APIError(
                        503,
                        "exemption_recovery_unavailable",
                        "无法建立豁免恢复工单，请立即重试。",
                    )
            try:
                await session.commit()
            except IntegrityError as exc:
                # 同 _create_user_row 的其它兄弟接口：并发为同一个
                # (group_id, user_id) 建行时唯一索引会撞，这里必须回 409 而不是
                # 冒泡成 500（A-09）。
                await session.rollback()
                raise _APIError(
                    409,
                    "user_policy_conflict",
                    "该用户刚刚被并发修改，请刷新后重试。",
                ) from exc
            await session.refresh(row)
            if recovery is not None:
                activate_manual_unban_recovery(recovery)
                released = await release_moderation_restriction_after_exemption(
                    bot,
                    session,
                    recovery,
                )
        payload = {key: _user_policy_document(row)}
        if model is ModerationExemption:
            payload["restriction_reconciled"] = bool(released)
        return _success_response(payload)

    async def _delete_user_row(
        request: web.Request,
        user: Any,
        model: type[Any],
    ) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        target_id = _path_int(request, "user_id")
        async with session_factory() as session:
            row = await session.scalar(select(model).where(
                model.group_id == group_id,
                model.user_id == target_id,
            ))
            if row is not None:
                await session.delete(row)
            await session.commit()
        return _success_response({"deleted": row is not None})

    @any_admin
    async def list_exemptions(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, ModerationExemption, "exemptions")

    @any_admin
    async def create_exemption(request: web.Request, user: Any) -> web.Response:
        return await _create_user_row(request, user, ModerationExemption, "exemption")

    @any_admin
    async def delete_exemption(request: web.Request, user: Any) -> web.Response:
        return await _delete_user_row(request, user, ModerationExemption)

    @any_admin
    async def list_reply_mutes(request: web.Request, user: Any) -> web.Response:
        return await _list_user_rows(request, user, ReplyMute, "reply_mutes")

    @any_admin
    async def create_reply_mute(request: web.Request, user: Any) -> web.Response:
        return await _create_user_row(request, user, ReplyMute, "reply_mute")

    @any_admin
    async def delete_reply_mute(request: web.Request, user: Any) -> web.Response:
        return await _delete_user_row(request, user, ReplyMute)

    @any_admin
    async def get_patrol_status(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        service = get_patrol_service()
        if service is None:
            raise _APIError(503, "patrol_unavailable", "巡检服务尚未启动。")
        status = await service.status(group_id)
        async with session_factory() as session:
            group = await session.get(Group, group_id)
            group_settings = dict(group.settings or {}) if group else {}
        status["enabled"] = patrol_policy(settings, group_settings)
        return _success_response({"patrol": status})

    @any_admin
    async def trigger_patrol(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        service = get_patrol_service()
        if service is None:
            raise _APIError(503, "patrol_unavailable", "巡检服务尚未启动。")
        if not settings.moderation.enabled:
            raise _APIError(400, "moderation_disabled", "内容审核已关闭，无法巡检。")
        if not verification_service_ready(settings, verification_provider(settings)):
            raise _APIError(
                400,
                "verification_provider_unavailable",
                "真人质询验证服务未配置，暂时不能巡检。",
            )
        # Fire-and-forget: a full scan takes far longer than the Mini App's
        # 10-minute initData window allows a request to wait.
        result = service.start_manual_patrol(group_id)
        if not result.get("started"):
            raise _APIError(409, "patrol_already_running", "该群巡检正在进行中。")
        log.info("manual patrol triggered | group=%s operator=%s", group_id, user.id)
        return _success_response({"patrol": result})

    # ----------------------------------------------------------------- #
    # 审核回放（dry-run）
    #
    # 全部 ``@any_admin`` + ``_require_group_access``：样本是**群成员的发言**
    # （已脱敏），跨群可见等于把 A 群的发言摆到 B 群管理员面前。
    #
    # 边界：这套端点只读 dry-run 表。**没有任何一条路径**把候选规则/提示词写进
    # ``moderation_rules`` 或运行时配置——"确认回放"只是一条只读结果，不会
    # 发布任何群规。
    # ----------------------------------------------------------------- #
    def _replay_error(exc: moderation_replay.ReplayError) -> _APIError:
        # 服务层的错误码原样透出（稳定、机器可读），文案已经是给管理员看的中文短句。
        return _APIError(exc.status, exc.code, exc.message)

    def _can_manage_global(user: Any) -> bool:
        """回放的角色矩阵：**全局**审核提示词只交给超管。

        回放本身是按群授权的（样本/群规/统计都是本群的），但候选/基线里的
        ``prompt`` 是**全局**审核口径 —— 那是超管级配置。所以本文件里凡是会
        碰到提示词的地方，都必须先过这一关：

        * 非超管**不得**提交候选提示词（403 ``replay_prompt_forbidden``）；
        * 非超管拿到的快照里 ``prompt`` 一律置空（只剩 12 位摘要）。
        """

        return bool(
            settings.super_admin_id and int(user.id) == int(settings.super_admin_id)
        )

    @any_admin
    async def import_replay_samples(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"kinds", "limit"}
        if unknown:
            raise _APIError(
                400,
                "unknown_replay_import_request",
                f"包含不允许的请求字段：{', '.join(sorted(unknown))}",
            )
        raw_kinds = body.get("kinds")
        if raw_kinds is None:
            kinds = (
                moderation_replay.SOURCE_KIND_RELEASE,
                moderation_replay.SOURCE_KIND_CONFIRMED,
            )
        elif isinstance(raw_kinds, list):
            kinds = tuple(str(item) for item in raw_kinds)
        else:
            raise _APIError(400, "replay_import_kinds_invalid", "导入类型无效。")
        try:
            limit = int(body.get("limit", 200))
        except (TypeError, ValueError) as exc:
            raise _APIError(400, "replay_limit_invalid", "导入条数无效。") from exc
        async with session_factory() as session:
            try:
                result = await moderation_replay.import_samples_from_violations(
                    session,
                    group_id,
                    limit=limit,
                    kinds=kinds,
                    created_by=int(user.id),
                )
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            sample_ids = list(result.get("sample_ids") or [])
            samples = (
                list(
                    (
                        await session.scalars(
                            select(ModerationReplaySample)
                            .where(ModerationReplaySample.id.in_(sample_ids))
                            .order_by(ModerationReplaySample.id)
                        )
                    ).all()
                )
                if sample_ids
                else []
            )
            await session.commit()
        log.info(
            "moderation replay samples imported | group=%s operator=%s imported=%s skipped=%s",
            group_id,
            user.id,
            result["imported"],
            result["skipped"],
        )
        return _success_response(
            {
                "imported": int(result["imported"]),
                "skipped": int(result["skipped"]),
                "ttl_days": int(result["ttl_days"]),
                "samples": [_replay_sample_document(row) for row in samples],
            }
        )

    @any_admin
    async def create_replay_sample(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"text", "context", "gold_label"}
        if unknown:
            raise _APIError(
                400,
                "unknown_replay_sample_request",
                f"包含不允许的请求字段：{', '.join(sorted(unknown))}",
            )
        text = str(body.get("text") or "")
        if not text.strip():
            raise _APIError(400, "replay_sample_empty", "样本内容不能为空。")
        async with session_factory() as session:
            try:
                row = await moderation_replay.create_manual_sample(
                    session,
                    group_id,
                    text=text,
                    context=str(body.get("context") or ""),
                    gold_label=body.get("gold_label"),
                    created_by=int(user.id),
                )
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            await session.commit()
            await session.refresh(row)
            document = _replay_sample_document(row)
        return _success_response({"sample": document})

    @any_admin
    async def label_replay_sample(request: web.Request, user: Any) -> web.Response:
        """**人工确认/修正/清除**一条样本的金标。

        这是唯一能给样本打金标的入口：导入只会写 provenance（机器信号），机器信号
        永远不是答案键。每次变更都会留一行审计（谁、何时、从什么改成什么），所以
        一份 FP/FN 报告能说清它的答案键是怎么来的。

        ``{"gold_label": "violation" | "benign" | null}``；``null`` = 清回无标注。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        sample_id = _path_int(
            request, "sample_id", code="invalid_sample_id", message="样本 ID 无效。"
        )
        body = await _json_object(request)
        unknown = set(body) - {"gold_label"}
        if unknown:
            raise _APIError(
                400,
                "unknown_replay_label_request",
                f"包含不允许的请求字段：{', '.join(sorted(unknown))}",
            )
        if "gold_label" not in body:
            raise _APIError(
                400, "replay_gold_label_required", "必须提供 gold_label 字段。"
            )
        raw_label = body.get("gold_label")
        if raw_label is not None and not isinstance(raw_label, str):
            raise _APIError(
                400, "replay_gold_label_invalid", "标注只能是 violation 或 benign。"
            )
        async with session_factory() as session:
            try:
                row = await moderation_replay.set_sample_label(
                    session,
                    group_id,
                    sample_id,
                    label=raw_label,
                    actor_id=int(user.id),
                )
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            await session.commit()
            document = _replay_sample_document(row)
            audits = await moderation_replay.list_label_audits(session, sample_id)
            history = [
                {
                    "id": int(item.id),
                    "previous_label": (
                        str(item.previous_label) if item.previous_label else None
                    ),
                    "new_label": str(item.new_label) if item.new_label else None,
                    "action": str(item.action or ""),
                    "actor_id": int(item.actor_id or 0),
                    "changed_at": item.changed_at.isoformat() if item.changed_at else "",
                }
                for item in audits
            ]
        log.info(
            "moderation replay sample labelled | group=%s operator=%s sample=%s",
            group_id,
            user.id,
            sample_id,
        )
        return _success_response({"sample": document, "label_history": history})

    @any_admin
    async def list_replay_samples(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        limit, offset = _page_window(request)
        include_expired = str(request.query.get("include_expired", "")).lower() in {
            "1",
            "true",
            "yes",
        }
        async with session_factory() as session:
            rows = await moderation_replay.list_samples(
                session,
                group_id,
                limit=limit,
                offset=offset,
                include_expired=include_expired,
            )
        return _success_response(
            {
                "samples": [_replay_sample_document(row) for row in rows],
                "ttl_days": moderation_replay.SAMPLE_TTL_DAYS,
            }
        )

    @any_admin
    async def delete_replay_sample(request: web.Request, user: Any) -> web.Response:
        """手动删除样本。**连同它的结果行一起删**，并重算受影响 run 的统计。

        返回里的 ``deleted_results`` 与 ``recomputed_runs`` 是给前端的明确交代：
        "这条样本的判定记录也没了，报告的统计已经按剩下的结果重算过"。不交代的话，
        管理员会以为历史还在。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        sample_id = _path_int(
            request, "sample_id", code="invalid_sample_id", message="样本 ID 无效。"
        )
        async with session_factory() as session:
            sample = await session.get(ModerationReplaySample, sample_id)
            run_ids = (
                {
                    int(value)
                    for value in (
                        await session.scalars(
                            select(ModerationReplayResult.run_id)
                            .where(ModerationReplayResult.sample_id == int(sample_id))
                            .distinct()
                        )
                    ).all()
                }
                if sample is not None
                else set()
            )
            try:
                deleted = await moderation_replay.delete_sample(
                    session, group_id, sample_id
                )
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            if not deleted:
                raise _APIError(404, "replay_sample_not_found", "样本不存在。")
            await session.commit()
            stats_by_run = {}
            for run_id in sorted(run_ids):
                refreshed = await session.get(ModerationReplayRun, run_id)
                if refreshed is not None:
                    stats_by_run[str(run_id)] = dict(refreshed.stats or {})
        return _success_response(
            {
                "deleted": True,
                "deleted_results": True,
                "recomputed_runs": sorted(run_ids),
                "stats": stats_by_run,
            }
        )

    async def _start_replay_run(
        run: ModerationReplayRun,
        *,
        user_id: int,
        reservation: Any = None,
    ) -> tuple[ModerationReplayRun, bool]:
        """把一条 ``queued`` 的 run 交给后台执行器，返回 ``(run, accepted)``。

        ``reservation`` 是 :meth:`ReplayExecutor.reserve` 发的那张名额票：带着它
        调用 :meth:`submit` 就**跳过**二次容量判定，于是"检查通过"到"真的排上"
        之间没有 ``await``，没有被别人抢走名额的窗口。

        ``accepted=False`` 意味着连预留都没拿到 —— 调用方**必须**把它翻成稳定的
        ``replay_busy``（429），而不是回 200 然后留一条没人会驱动的 ``queued``。
        """

        accepted = await _replay_executor.submit(
            int(run.id), group_id=int(run.group_id), reservation=reservation
        )
        if accepted:
            return run, True
        return run, False

    @any_admin
    async def create_replay_run(request: web.Request, user: Any) -> web.Response:
        """登记一轮回放并**立刻返回**（执行在后台，可轮询、可取消）。

        生命周期：

        1. ``execute=true``（默认）→ 登记后立刻排进后台，返回时 run 通常还是
           ``queued`` 或刚变 ``running``；客户端轮询 ``GET /runs/{id}``。
        2. ``execute=false`` → 只登记（``queued``），用 ``POST /runs/{id}/execute``
           真正开跑。**不存在"永远排不上"的死状态**。
        3. 没有容量 → 429 ``replay_busy``（与 ``/execute`` **同一个错误码**），
           且**不留孤儿 run**：容量是在外层写事务里用"预留"来的，拿不到就把
           这一笔回滚掉；客户端用同一个 ``idempotency_key`` 重试是安全的。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        can_global = _can_manage_global(user)
        body = await _json_object(request)
        unknown = set(body) - {"candidate", "idempotency_key", "execute"}
        if unknown:
            raise _APIError(
                400,
                "unknown_replay_run_request",
                f"包含不允许的请求字段：{', '.join(sorted(unknown))}",
            )
        execute = body.get("execute", True)
        if not isinstance(execute, bool):
            raise _APIError(400, "replay_execute_invalid", "execute 必须是布尔值。")
        candidate = body.get("candidate")
        # 角色矩阵：候选提示词是**全局**口径的产物，只有超管能提交。
        if isinstance(candidate, dict) and str(candidate.get("prompt") or "").strip():
            if not can_global:
                raise _APIError(
                    403,
                    "replay_prompt_forbidden",
                    "只有超级管理员可以提交候选审核提示词。",
                )
        # 启动回收必须排在**任何外层写事务之前**：它用另一个会话做 UPDATE，而
        # 下面 create_run 的 flush 已经把 SQLite 的（全局）写锁握在手里了。
        # 放在事务里 = 自己等自己等满 busy_timeout（5s）后抛错。
        await _replay_executor.ensure_ready()
        reservation: Any = None
        try:
            async with session_factory() as session:
                try:
                    run, created = await moderation_replay.create_run(
                        session,
                        group_id,
                        candidate=candidate,
                        created_by=int(user.id),
                        idempotency_key=str(body.get("idempotency_key") or ""),
                        settings=settings,
                    )
                    # 有界性：先**预留**一个名额，**没有就不落库**。预留是纯内存
                    # 的、不 await 的原子步骤，所以从这一刻起这个名额就是这次请求
                    # 的了 —— 提交时不会"检查通过、结果被抢"。回滚掉再报 429，
                    # 保证"被拒绝"不会留下一条永远 queued 的孤儿 run；同一个
                    # idempotency_key 稍后重试也不会撞上已存在的半成品。
                    if bool(execute and created):
                        reservation = _replay_executor.reserve(group_id)
                        if reservation is None:
                            await session.rollback()
                            raise _APIError(
                                429,
                                "replay_busy",
                                "回放队列已满，请稍后再试。",
                                details={"capacity": _replay_executor.capacity_snapshot()},
                            )
                    await session.commit()
                    await session.refresh(run)
                    document = _replay_run_document(run)
                    sample_pool = await moderation_replay.count_active_samples(
                        session, group_id
                    )
                    snapshot = _replay_snapshot_for_audience(
                        run, can_manage_global=can_global
                    )
                    candidate_rules = list(run.candidate_rules or [])
                    candidate_prompt = (
                        str(run.candidate_prompt or "") if can_global else ""
                    )
                except moderation_replay.ReplayError as exc:
                    await session.rollback()
                    raise _replay_error(exc) from exc
            accepted = False
            # 同键重复提交 → 复用既有 run，不重复排队。
            if bool(execute and created):
                run, accepted = await _start_replay_run(
                    run, user_id=int(user.id), reservation=reservation
                )
                if not accepted:
                    # 预留还在手里（submit 没消费它）→ 归还，否则容量永久少一个。
                    raise _APIError(
                        429,
                        "replay_busy",
                        "回放队列已满，请稍后再试。",
                        details={"capacity": _replay_executor.capacity_snapshot()},
                    )
                document = _replay_run_document(run)
        finally:
            if reservation is not None:
                # 幂等：submit 已消费时这里是 no-op；任何异常路径都会把名额还回去。
                reservation.release()
        bounds = _replay_bounds_document(
            sample_pool=sample_pool,
            document=document,
            sample_limit=moderation_replay.REPLAY_RUN_SAMPLE_LIMIT,
        )
        document["sample_pool"] = sample_pool
        log.info(
            "moderation replay run | group=%s operator=%s run=%s created=%s "
            "accepted=%s status=%s sample_pool=%s",
            group_id,
            user.id,
            document["id"],
            created,
            accepted,
            document["status"],
            sample_pool,
        )
        return _success_response(
            {
                "run": document,
                "created": created,
                "accepted": accepted,
                "executed": bool(execute and created and accepted),
                "bounds": bounds,
                "capacity": _replay_executor.capacity_snapshot(),
                "baseline_snapshot": snapshot,
                "candidate_rules": candidate_rules,
                "candidate_prompt": candidate_prompt,
                "candidate_prompt_visible": bool(can_global),
            }
        )

    @any_admin
    async def execute_replay_run(request: web.Request, user: Any) -> web.Response:
        """把一条 ``queued`` 的回放真正排进后台执行。

        这是 ``execute=false`` 的**真实后续入口**：没有它，"先登记再执行"就永远
        停在 queued。已结束/已取消的 run → 409（状态码稳定，前端据此提示）。

        **容量契约与 ``POST /runs`` 完全一致**：没有容量就是 429
        ``replay_busy`` + ``details.capacity``，绝不回 200 ``accepted:false``。
        run 保持 ``queued``（它本来就是客户端显式登记的），客户端拿同一个
        ``idempotency_key`` 重试是安全的 —— 关键是**拒绝必须是稳定的错误码**，
        而不是一句"没排上"。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_id = _path_int(
            request, "run_id", code="invalid_run_id", message="回放 ID 无效。"
        )
        async with session_factory() as session:
            try:
                run = await moderation_replay.get_run(session, group_id, run_id)
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            status = str(run.status or "")
            if status != moderation_replay.RUN_QUEUED:
                raise _APIError(
                    409,
                    "replay_not_queued",
                    f"回放当前状态为 {status}，只有 queued 可以启动。",
                )
            # 回滚会让 ORM 行过期；先把要用的值取成**纯 int**，否则后面读属性
            # 会在没有 greenlet 的上下文里触发惰性刷新（MissingGreenlet）。
            run_id = int(run.id)
            run_group_id = int(run.group_id)
            await session.rollback()
        # 同 POST /runs：启动回收排在**任何**写事务之外（本处的 session 已经回滚
        # 并关闭），它用的是另一个会话做 UPDATE。
        await _replay_executor.ensure_ready()
        # 与 POST /runs 同一个"预留 → 提交"两步：预留是同步且不 await 的，
        # 所以"检查容量"到"真的排上"之间没有窗口被别人抢走名额。没有名额 =
        # 稳定的 429 replay_busy（**不是** 200 + accepted:false）。
        reservation = _replay_executor.reserve(run_group_id)
        if reservation is None:
            raise _APIError(
                429,
                "replay_busy",
                "回放队列已满，请稍后再试。",
                details={"capacity": _replay_executor.capacity_snapshot()},
            )
        try:
            accepted = await _replay_executor.submit(
                run_id, group_id=run_group_id, reservation=reservation
            )
            if not accepted:
                raise _APIError(
                    429,
                    "replay_busy",
                    "回放队列已满，请稍后再试。",
                    details={"capacity": _replay_executor.capacity_snapshot()},
                )
        finally:
            reservation.release()
        refreshed = await moderation_replay.get_run_any(session_factory, run_id)
        run = refreshed if refreshed is not None else run
        document = _replay_run_document(run)
        log.info(
            "moderation replay execute | group=%s operator=%s run=%s accepted=%s",
            group_id,
            user.id,
            run_id,
            accepted,
        )
        return _success_response(
            {
                "run": document,
                "accepted": accepted,
                "capacity": _replay_executor.capacity_snapshot(),
            }
        )

    @any_admin
    async def list_replay_runs(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        limit, offset = _page_window(request)
        async with session_factory() as session:
            rows = await moderation_replay.list_runs(
                session, group_id, limit=limit, offset=offset
            )
        return _success_response(
            {
                "runs": [_replay_run_document(row) for row in rows],
                "capacity": _replay_executor.capacity_snapshot(),
            }
        )

    @any_admin
    async def get_replay_run(request: web.Request, user: Any) -> web.Response:
        """**轮询端点**：状态 + 统计 + 结果（按当前角色裁剪）。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        can_global = _can_manage_global(user)
        run_id = _path_int(
            request, "run_id", code="invalid_run_id", message="回放 ID 无效。"
        )
        limit, offset = _page_window(request)
        async with session_factory() as session:
            try:
                run = await moderation_replay.get_run(session, group_id, run_id)
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            result_rows = await moderation_replay.list_results(
                session, run_id, limit=limit, offset=offset
            )
            sample_ids = [int(row.sample_id) for row in result_rows]
            samples = (
                list(
                    (
                        await session.scalars(
                            select(ModerationReplaySample).where(
                                ModerationReplaySample.id.in_(sample_ids)
                            )
                        )
                    ).all()
                )
                if sample_ids
                else []
            )
            # 必须在会话还活着的时候数：跑完之后"池子里本来有多少条"是判断
            # 有没有撞到条数上限的唯一依据。
            sample_pool = await moderation_replay.count_active_samples(
                session, group_id
            )
            document = _replay_run_document(run)
            snapshot = _replay_snapshot_for_audience(run, can_manage_global=can_global)
            candidate_rules = list(run.candidate_rules or [])
            candidate_prompt = str(run.candidate_prompt or "") if can_global else ""
            by_id = {int(item.id): item for item in samples}
        document["sample_pool"] = sample_pool
        return _success_response(
            {
                "run": document,
                # 与 POST 同一套"这次到底评了几条 / 有没有提前停"的口径，轮询时
                # 不必自己再推一遍。
                "bounds": _replay_bounds_document(
                    sample_pool=sample_pool,
                    document=document,
                    sample_limit=moderation_replay.REPLAY_RUN_SAMPLE_LIMIT,
                ),
                "capacity": _replay_executor.capacity_snapshot(),
                "baseline_snapshot": snapshot,
                "candidate_rules": candidate_rules,
                "candidate_prompt": candidate_prompt,
                "candidate_prompt_visible": bool(can_global),
                "results": [
                    _replay_result_document(row, by_id.get(int(row.sample_id)))
                    for row in result_rows
                ],
                "result_count": len(result_rows),
            }
        )

    @any_admin
    async def cancel_replay_run(request: web.Request, user: Any) -> web.Response:
        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        run_id = _path_int(
            request, "run_id", code="invalid_run_id", message="回放 ID 无效。"
        )
        async with session_factory() as session:
            try:
                # 先确认这条 run 属于本群，避免用别的群的 run_id 探测存在性。
                await moderation_replay.get_run(session, group_id, run_id)
                run = await moderation_replay.cancel_run(session, run_id)
            except moderation_replay.ReplayError as exc:
                raise _replay_error(exc) from exc
            await session.commit()
            document = _replay_run_document(run)
        log.info(
            "moderation replay cancelled | group=%s operator=%s run=%s",
            group_id,
            user.id,
            run_id,
        )
        return _success_response({"run": document})

    # ------------------------------------------------------------------
    # 新群接入（onboarding）
    #
    # 这一组路由**刻意不**走 `any_admin`：申请入口面向的是「还没拿到任何服务
    # 权限的群管理员」，`any_admin` 会把整段 handler 套进
    # `privileged_request_scope()`（CRITICAL 资源优先级）——那是超管级待遇，
    # 不该给一个刚刚提交申请的人。认证本身仍然必须是合法 initData。
    # ------------------------------------------------------------------
    configure_price_store(session_factory)
    # 共享实例：与启动装配的 guard 是**同一个**，保存预算才能立刻对 LLM 入口生效。
    budget_service = shared_service(session_factory, price_catalog_provider)

    def onboarding_user(
        handler: Handler,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                user = await require_authenticated_user(
                    request, bot_token=bot_token
                )
                # 故意不进入 privileged_request_scope()。
                return await handler(request, user)
            except web.HTTPException:
                raise
            except _APIError as exc:
                return _error_response(
                    exc.status, exc.code, exc.message, details=exc.details
                )
            except group_onboarding.OnboardingError as exc:
                return _onboarding_error_response(exc)
            except Exception as exc:
                return _error_response(
                    500,
                    "internal_error",
                    _opaque_error_message(
                        exc,
                        code="internal_error",
                        where="onboarding_user",
                        text="服务器处理请求失败。",
                    ),
                )

        return wrapped

    def _is_super_admin(user: Any) -> bool:
        return bool(
            settings.super_admin_id and int(user.id) == int(settings.super_admin_id)
        )

    def _request_id(request: web.Request) -> str:
        return str(request.match_info.get("request_id") or "").strip()

    @onboarding_user
    async def get_onboarding_session(
        _request: web.Request, user: Any
    ) -> web.Response:
        """申请人的**最小**入口。

        ``/api/v1/session`` 对"没有任何已授权群"的普通用户返回 403（那是既有
        合同，不该为了接一个新页面去放宽）。所以单独给一个入口：它只回答
        "你是谁、你能不能提交申请"，**不**返回任何群列表或全局配置。
        """

        allowed = await _allowed_group_ids(int(user.id))
        return _success_response(
            {
                "session": {
                    "user_id": int(user.id),
                    "role": "super_admin" if allowed is None else (
                        "group_admin" if allowed else "applicant"
                    ),
                    "can_manage_global": allowed is None,
                    "group_ids": sorted(allowed or []),
                    "can_apply": True,
                }
            }
        )

    @onboarding_user
    async def post_onboarding_check(
        request: web.Request, user: Any
    ) -> web.Response:
        """申请前的**只读**体检：授权状态 / bot 权限 / 功能就绪 / 精确阻断。

        绝不返回全局模型 endpoint、apiKey 或别的群的数据 —— 它服务的是一个
        还没被授权的群管理员，他此刻对全局配置没有任何读取权。
        """

        body = await _json_object(request)
        unknown = set(body) - {"group_id", "desired_features", "preset_id"}
        if unknown:
            raise _APIError(
                400,
                "unknown_onboarding_check_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        group_id = group_onboarding.normalize_group_id(body.get("group_id"))
        features = group_onboarding.normalize_features(body.get("desired_features"))
        preset_id = group_onboarding.normalize_preset_id(body.get("preset_id"))
        if preset_id is not None:
            # 选了预设：就按**该预设真实需要的功能**体检。此前这里把预设归一化
            # 之后直接丢掉，于是选了"审核 + 摘要"的预设仍按空集合判定 —— 什么
            # 阻断都算不出来，面板标题下面一条都没有。
            definition = group_onboarding.preset_definition(preset_id)
            if not features:
                features = list(definition.features)
        elif not features:
            # 没选预设也没指名功能：用与就绪面板**同一个**默认集合，否则
            # selected_features 为空，任何依赖都判不出缺失。
            features = list(group_onboarding.DEFAULT_READINESS_FEATURES)
        # 限次在**核验之前**扣：失败调用同样是一次真实的 Telegram 调用。
        group_onboarding.consume_rate_limit(int(user.id))

        facts = await group_onboarding.require_live_group_administrator(
            bot, group_id, int(user.id)
        )
        async with session_factory() as session:
            authorized = await session.get(AuthorizedGroup, group_id)
        pending_view = await _await_open_request_view(
            session_factory, group_id, int(user.id)
        )
        # 本群**已存**的群级开关：就绪要按它区分"开了但没配好"与"压根没开"。
        # 只读本群、读不到就是空设置，不会碰到全局配置或别的群。
        stored_settings = await _group_settings_document(session_factory, group_id)
        return _success_response(
            {
                "group_id": group_id,
                "chat_type": facts.chat_type,
                "verified": facts.verified,
                "bot_present": facts.bot_present,
                "bot_status": facts.bot_status,
                "bot_is_administrator": facts.bot_is_administrator,
                "applicant_is_administrator": True,
                "authorized": bool(
                    authorized is not None and bool(authorized.bot_present)
                ),
                "preset_id": preset_id,
                "requested_features": features,
                "missing_bot_permissions": facts.missing_permissions(features),
                # 复用就绪面板**同一份**只读判定（能力 / 阻断 / 缺失权限）。它是
                # 纯配置检查：不连任何供应商、不应用预设、也不授权本群。这里
                # 刻意**不**放宽任何写权限 —— 体检通过不代表有权改配置。
                "readiness": group_onboarding.group_readiness_document(
                    settings=settings,
                    facts=facts,
                    features=features,
                    group_settings=stored_settings,
                ),
                # 只回**调用者自己**那条；有别人的待审申请时只回一个布尔。
                # 曾经这里直接把该群的 pending 整条文档回给任何一个来 check
                # 的管理员 —— 里面含 request_id（可直接 cancel/approve 的句柄）
                # 和另一个人的 applicant_id / 期望功能。
                **pending_view,
            }
        )

    @onboarding_user
    async def post_onboarding_request(
        request: web.Request, user: Any
    ) -> web.Response:
        """提交申请。**不授权**：只写一条 pending。"""

        body = await _json_object(request)
        unknown = set(body) - {"group_id", "desired_features", "preset_id"}
        if unknown:
            raise _APIError(
                400,
                "unknown_onboarding_request_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        group_id = group_onboarding.normalize_group_id(body.get("group_id"))
        features = group_onboarding.normalize_features(body.get("desired_features"))
        preset_id = group_onboarding.normalize_preset_id(body.get("preset_id"))
        group_onboarding.consume_rate_limit(int(user.id))

        # 事务之外的权威核验：申请人和 bot 的身份都由 Telegram 现答。
        await group_onboarding.require_live_group_administrator(
            bot, group_id, int(user.id)
        )
        async with session_factory() as session:
            async with session.begin():
                row, created = await group_onboarding.submit_request(
                    session,
                    group_id=group_id,
                    applicant_id=int(user.id),
                    requested_features=features,
                    preset_id=preset_id,
                )
                document = group_onboarding.request_document(row)
        return _success_response({"request": document, "created": created})


    @onboarding_user
    async def get_onboarding_requests(
        request: web.Request, user: Any
    ) -> web.Response:
        """审理列表：超管看全部，普通已认证用户**只看自己提交的**。

        ``actor_id`` 查询参数被显式忽略 —— 身份只来自会话，body 里的自称
        「我是 12345 号」永远不生效。
        """

        limit = request.query.get("limit")
        # 归一化只做一次，rows 与 total 共用同一个值：曾经 list 做了 strip/lower
        # 而 count 没有，于 ?status=PENDING 会返回 rows=1 / total=0。
        status = group_onboarding.normalize_status(request.query.get("status"))
        offset = request.query.get("offset", "0")
        scope = None if _is_super_admin(user) else int(user.id)
        async with session_factory() as session:
            rows = await group_onboarding.list_requests(
                session,
                applicant_id=scope,
                status=status,
                limit=limit,
                offset=offset,
            )
            total = await group_onboarding.count_requests(
                session,
                applicant_id=scope,
                status=status,
            )
        return _success_response(
            {
                "requests": [group_onboarding.request_document(r) for r in rows],
                "total": total,
                "scope": "all" if scope is None else "self",
            }
        )

    @onboarding_user
    async def cancel_onboarding_request(
        request: web.Request, user: Any
    ) -> web.Response:
        async with session_factory() as session:
            async with session.begin():
                row = await group_onboarding.cancel_request(
                    session,
                    request_id=_request_id(request),
                    actor_id=int(user.id),
                    actor_is_super_admin=_is_super_admin(user),
                )
                document = group_onboarding.request_document(row)
        return _success_response({"request": document})

    @authenticated
    async def approve_onboarding_request(
        request: web.Request, user: Any
    ) -> web.Response:
        """超管审批。**核验在事务之外**先做完，再进一个短事务一次性落授权。"""

        request_id = _request_id(request)
        body = await _json_object(request) if request.can_read_body else {}
        unknown = set(body) - {"reason"}
        if unknown:
            raise _APIError(
                400,
                "unknown_onboarding_decision_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        reason = group_onboarding.normalize_reason(body.get("reason"))

        async with session_factory() as session:
            row = await group_onboarding.get_request(session, request_id)
            group_id = int(row.group_id)
            applicant_id = int(row.applicant_id)
            existing_status = str(row.status or "")
            if existing_status == group_onboarding.STATUS_PENDING:
                # 复核申请人**此刻**仍是管理员：申请时是管理员不等于现在还是。
                await group_onboarding.require_live_group_administrator(
                    bot, group_id, applicant_id
                )
            # 已终结（含已批准）的申请**不再核验、也不再授权**：它是一条历史
            # 决定。核验与都改变不了"不写入"这个事实，所以连 Telegram
            # 都不必去问 —— 问了反而会在 Telegram 故障时把一次纯读取变成 503。
        # 事务到这里已经关闭（读事务已结束），下面才开始写。
        async with session_factory() as session:
            async with session.begin():
                outcome = await group_onboarding.approve_request(
                    session,
                    request_id=request_id,
                    reviewer_id=int(user.id),
                    reason=reason,
                )
                row = await group_onboarding.get_request(session, request_id)
                document = group_onboarding.request_document(row)
        return _success_response(
            {
                "request": document,
                "approved": True,
                "already_decided": outcome.already_decided,
                # 本次调用是否授予了授权（重放旧批准时恒为 false）
                "group_authorized": outcome.group_authorized,
                "applicant_granted": outcome.applicant_granted,
                # 现在是否还在服务（与上面两个独立）
                "current": {
                    "authorized": outcome.currently_authorized,
                    "bot_present": outcome.currently_bot_present,
                    "applicant_admin": outcome.currently_applicant_admin,
                },
            }
        )

    @authenticated
    async def reject_onboarding_request(
        request: web.Request, user: Any
    ) -> web.Response:
        request_id = _request_id(request)
        body = await _json_object(request) if request.can_read_body else {}
        unknown = set(body) - {"reason"}
        if unknown:
            raise _APIError(
                400,
                "unknown_onboarding_decision_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        async with session_factory() as session:
            async with session.begin():
                row = await group_onboarding.reject_request(
                    session,
                    request_id=request_id,
                    reviewer_id=int(user.id),
                    reason=group_onboarding.normalize_reason(body.get("reason")),
                )
                document = group_onboarding.request_document(row)
        return _success_response({"request": document, "rejected": True})

    # ------------------------------------------------------------------
    # 按群成本与预算
    # ------------------------------------------------------------------
    @any_admin
    async def get_group_costs(request: web.Request, user: Any) -> web.Response:
        """本群用量明细 + **发出时冻结**的金额（按币种，绝不跨币种相加）。

        两张表口径不同：``llm_usage_daily`` 只记成功调用、按 (日, 阶段) 聚合；
        本接口读的是明细表 ``llm_cost_daily``，金额来自它的伴随维度
        ``llm_cost_priced_daily``（同一次 attempt 的另一个维度，**attempts 不许
        相加**）。任何一处相加都会重复计数。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        days = _bounded_days(request.query.get("days"))
        async with session_factory() as session:
            try:
                report = await group_costs.query_group_costs_report(
                    session, group_id=group_id, days=days
                )
            except group_costs.ReportSnapshotUnavailable as exc:
                # 建立不了一致的读快照就不给数字：宁可说"读不到"，也不返回
                # 可能把两代拼在一起的金额与次数（前端会显示"状态未知"）。
                raise _APIError(
                    503,
                    "cost_report_snapshot_unavailable",
                    "暂时无法为成本报表建立一致的读快照，本群用量与金额状态未知。",
                ) from exc
        payload = dict(report)
        payload["group_id"] = group_id
        payload["source_note"] = report["note"]
        return _success_response(payload)

    @any_admin
    async def get_group_budget(request: web.Request, user: Any) -> web.Response:
        """本群预算状态。**没启用就说没启用**，绝不显示成正在保护。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        snapshot = await budget_service.snapshot(group_id)
        return _success_response({"budget": snapshot})

    @authenticated
    async def put_group_budget(request: web.Request, user: Any) -> web.Response:
        """仅超管修改预算（expected revision）。"""

        group_id = _group_id(request)
        body = await _json_object(request)
        unknown = set(body) - {
            "revision",
            "enabled",
            "currency",
            "daily_attempt_limit",
            "daily_token_limit",
            "daily_money_micro_limit",
            "operation_limits",
            "money_covers",
        }
        if unknown:
            raise _APIError(
                400,
                "unknown_budget_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        # enabled 必须是**真的布尔**。``enabled="false"`` 被 JSON 当成字符串，
        # 曾经用 bool(...) 转换后变成 True —— 一条明确说"关闭"的请求把护栏打开了。
        raw_enabled = body.get("enabled")
        if not isinstance(raw_enabled, bool):
            raise _APIError(
                400, "invalid_budget_field", "enabled 必须是布尔值 true / false。"
            )
        revision = _strict_revision(body.get("revision"))
        operations = body.get("operation_limits") or {}
        if not isinstance(operations, dict):
            raise _APIError(
                400, "invalid_budget_field", "operation_limits 必须是对象。"
            )
        for feature, value in operations.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _APIError(
                    400,
                    "invalid_budget_field",
                    f"operation_limits[{feature}] 必须是非负整数。",
                )
        covers_raw = body.get("money_covers") or []
        if not isinstance(covers_raw, list):
            raise _APIError(400, "invalid_budget_field", "money_covers 必须是数组。")
        covers: list[tuple[str, str]] = []
        for item in covers_raw:
            if not isinstance(item, dict) or set(item) != {"provider_key", "model"}:
                raise _APIError(
                    400, "invalid_budget_field", "money_covers 项格式无效。"
                )
            covers.append((str(item["provider_key"]), str(item["model"])))
        try:
            row = await budget_service.save_budget(
                group_id,
                expected_revision=revision,
                enabled=raw_enabled,
                currency=str(body.get("currency") or ""),
                daily_attempt_limit=body.get("daily_attempt_limit"),
                daily_token_limit=body.get("daily_token_limit"),
                daily_money_micro_limit=body.get("daily_money_micro_limit"),
                operation_limits=operations,
                money_covers=covers,
                updated_by=int(user.id),
            )
        except BudgetConflict as exc:
            raise _APIError(
                409, "budget_revision_conflict", "预算配置已被其他会话更新，请刷新后重试。"
            ) from exc
        except BudgetError as exc:
            raise _APIError(400, "invalid_budget", str(exc)) from exc
        return _success_response({"budget": (await budget_service.snapshot(group_id)), "revision": int(row.revision)})

    @authenticated
    async def get_price_catalog(_request: web.Request, _user: Any) -> web.Response:
        return _success_response(
            {"catalog": price_catalog_provider().document()}
        )

    @authenticated
    async def put_price_catalog(request: web.Request, user: Any) -> web.Response:
        """仅超管编辑价格目录。改价 = 新版本，历史账不追改。

        HTTP 契约（**期望版本 CAS**）：

        * 请求体必须是对象，且只允许 ``expected_version`` 与 ``entries``
          两个键；出现任何其它键（包括旧版容忍的顶层 ``currency``）→
          400 ``unknown_price_field``。
        * ``expected_version`` 必须**显式提供**，且是无损整数 ≥1：缺省、
          ``null``、布尔、浮点（含 ``3.0``）、数字字符串、``NaN``、负数、``0``
          → 400 ``invalid_expected_version``（不走 ``int()`` 截断）。
        * ``entries`` 校验口径不变：必须是数组、逐项过
          ``pricing.PriceEntry``，未知条目键 / 负数 / 空 provider_key / 空
          model / 坏币种 → 400 ``invalid_price_catalog``；重复
          ``(provider_key, model)`` → 400 ``duplicate_price_entry``。
        * CAS 以**数据库**为准：在插入新行的同一事务里读
          ``SELECT MAX(version) FROM model_price_catalog``（空库视为 1）。
          ``expected_version`` 与服务端最新版本不一致 → 409
          ``price_version_conflict``（消息同时给出服务端当前版本与客户端期望
          版本）；即便校验通过，插入时输掉主键竞争也翻译成同一个 409。
        * 成功时新版本 = ``expected_version + 1``，更新进程内缓存并返回
          ``{"catalog": <最新 document>}``；只 INSERT，绝不 UPDATE/DELETE
          历史行。
        """

        body = await _json_object(request)
        unknown = set(body) - {"entries", "expected_version"}
        if unknown:
            raise _APIError(
                400,
                "unknown_price_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        if "expected_version" not in body:
            raise _APIError(
                400,
                "invalid_expected_version",
                "expected_version 必须显式提供，且为大于等于 1 的整数。",
            )
        raw_expected = body["expected_version"]
        # strict_int 会容忍「整串数字」的字符串，但契约要求字面量必须是
        # JSON 整数本身 —— 字符串版本是另一套序列化约定，静默接受迟早对不上。
        if isinstance(raw_expected, str):
            raise _APIError(
                400,
                "invalid_expected_version",
                "expected_version 必须显式提供，且为大于等于 1 的整数。",
            )
        try:
            expected_version = strict_int(
                raw_expected, field="expected_version", minimum=1
            )
        except StrictValueError as exc:
            raise _APIError(
                400,
                "invalid_expected_version",
                "expected_version 必须显式提供，且为大于等于 1 的整数。",
            ) from exc
        raw_entries = body.get("entries")
        if not isinstance(raw_entries, list):
            raise _APIError(400, "invalid_price_catalog", "entries 必须是数组。")
        try:
            entries = tuple(pricing.PriceEntry(**item) for item in raw_entries)
        except (TypeError, pricing.PricingError) as exc:
            raise _APIError(400, "invalid_price_catalog", str(exc)) from exc
        # 重复 (provider_key, model) 明确拒绝：``find`` 只取第一条，重复项
        # 会被静默丢掉一条，而界面上看着"配置了两条"。
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            token = (entry.provider_key, entry.model)
            if token in seen:
                raise _APIError(
                    400,
                    "duplicate_price_entry",
                    f"重复的价格条目：{entry.provider_key} / {entry.model}",
                )
            seen.add(token)
        new_version = expected_version + 1
        catalog = pricing.PriceCatalog(version=new_version, entries=entries)
        await _persist_price_catalog(
            catalog, expected_version=expected_version, actor_id=int(user.id)
        )
        set_price_catalog(catalog)
        log.info(
            "price catalog updated | version=%d entries=%d by=%s",
            new_version,
            len(entries),
            user.id,
        )
        return _success_response({"catalog": price_catalog_provider().document()})

    async def _group_settings_document(
        factory: async_sessionmaker[AsyncSession],
        group_id: int,
    ) -> dict[str, Any]:
        """本群当前存的群设置（预设 diff 与就绪的群级开关都要读它）。"""

        async with factory() as session:
            group = await session.get(Group, group_id)
        return dict((group.settings or {}) if group is not None else {})

    async def _await_open_request_view(
        factory: async_sessionmaker[AsyncSession],
        group_id: int,
        user_id: int,
    ) -> dict[str, Any]:
        """check 响应里的「当前待审申请」——只含调用者自己的那一条。"""

        async with factory() as session:
            view = await group_onboarding.open_request_view(
                session, group_id, user_id
            )
        return {
            "open_request": view.own,
            "foreign_pending": view.foreign_pending,
        }

    # ------------------------------------------------------------------
    # 就绪体检与首次预设（本群管理权）
    # ------------------------------------------------------------------
    async def _live_group_facts(
        group_id: int,
        user_id: int,
    ) -> group_onboarding.TelegramGroupFacts:
        return await group_onboarding.require_live_group_administrator(
            bot, group_id, int(user_id)
        )

    @any_admin
    async def get_group_readiness(request: web.Request, user: Any) -> web.Response:
        """本群功能就绪：按**实际必需**的配置与权限判定，不做远端连通性测试。

        读一次 Telegram（群类型 / bot 在群 / bot 权限），随后全部是本地配置判定。
        反复打开面板不会产生任何模型调用 —— 就绪面板不是一次隐式压测。
        """

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        # ``?features=a&features=b``：不接受逗号串（见 readiness_features_from_query）。
        raw_features = request.query.getall("features", [])
        requested = group_onboarding.readiness_features_from_query(raw_features)
        if raw_features and not requested:
            # 显式写了 ?features= 却什么功能都没给：那是拼错了，不该静默
            # 退回默认集合（那会让用户以为自己筛的是"全部"）。
            raise _APIError(
                400, "invalid_features", "features 不能为空，请用 ?features=<名称>。"
            )
        features = requested or list(group_onboarding.DEFAULT_READINESS_FEATURES)
        try:
            facts = await _live_group_facts(group_id, int(user.id))
        except group_onboarding.TelegramUnverifiable:
            # 核验失败**不**降级成本地表：给一份明确标注「未核验」的快照。
            # 关键是不能让它仍然 ready —— 那等于把"什么都没查到"显示成全绿。
            facts = None
        stored_settings = await _group_settings_document(session_factory, group_id)
        return _success_response(
            {
                "group_id": group_id,
                "verified": facts is not None,
                "features": features,
                "readiness": group_onboarding.group_readiness_document(
                    settings=settings,
                    facts=facts,
                    features=features,
                    group_settings=stored_settings,
                ),
            }
        )

    async def _strict_group_admin_verified(
        group_id: int, user_id: int
    ) -> None:
        """本轮新增的群操作在动手前**实时**复核操作者身份。

        既有的 ``_require_group_access`` 沿用旧缓存，且 Telegram 问不成时按
        ``None`` 离线降级 —— 那对读接口够用，"改本群设置"不够。计划明确
        要求：管理操作重新检查实时身份，网络错误不能离线放权。

        超管不受此限制：他管理的群由 ``AuthorizedGroup`` 授权决定，Telegram
        侧的身份与这条管理权无关，把他在群里的角色当成授权来源只会误锁。
        """

        if settings.super_admin_id and int(user_id) == int(settings.super_admin_id):
            # 超管管理的群由 AuthorizedGroup 授权决定，与他在 Telegram 群里的
            # 角色无关；把后者当授权来源只会把超管锁在自己授权的群外面。
            return
        await group_onboarding.require_live_group_administrator(
            bot, group_id, int(user_id)
        )

    @any_admin
    async def post_group_preset_preview(
        request: web.Request, user: Any
    ) -> web.Response:
        """只算不改：preview 与 apply 的差异必须为零，否则预览就是在骗人。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        await _strict_group_admin_verified(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"preset_id", "revision"}
        if unknown:
            raise _APIError(
                400,
                "unknown_preset_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        definition = group_onboarding.preset_definition(body.get("preset_id"))
        stored_settings = await _group_settings_document(session_factory, group_id)
        # preview 是**只读**：核验问不成时照常返回，但明确标 unverified ——
        # 绝不因为"问不到"就把只读页面打成 403（apply 仍然是 503/403）。
        # 反过来也不能放宽写：普通委派管理员在这条路径上仍然要实时核验。
        try:
            facts = await _live_group_facts(group_id, int(user.id))
        except group_onboarding.OnboardingError:
            facts = None
        return _success_response(
            {
                "group_id": group_id,
                "verified": facts is not None,
                **group_onboarding.preset_document(definition.preset_id),
                "current_revision": _group_revision(stored_settings),
                "diff": group_onboarding.preset_diff_document(
                    definition, stored_settings
                ),
                "protected_unchanged": True,
                "readiness": group_onboarding.group_readiness_document(
                    settings=settings,
                    facts=facts,
                    features=definition.features,
                    group_settings=stored_settings,
                ),
            }
        )

    @any_admin
    async def post_group_preset_apply(request: web.Request, user: Any) -> web.Response:
        """显式应用预设：走与普通群设置保存**同一份**校验、revision 与写锁。"""

        group_id = _group_id(request)
        await _require_group_access(group_id, int(user.id))
        await _strict_group_admin_verified(group_id, int(user.id))
        body = await _json_object(request)
        unknown = set(body) - {"preset_id", "revision"}
        if unknown:
            raise _APIError(
                400,
                "unknown_preset_field",
                f"包含不允许的字段：{', '.join(sorted(unknown))}",
            )
        definition = group_onboarding.preset_definition(body.get("preset_id"))
        patch = group_onboarding.preset_patch_body(definition)
        expected_revision = str(body.get("revision") or "").strip()
        if not expected_revision:
            raise _APIError(
                400,
                "group_revision_required",
                "预设应用请求缺少 revision。",
            )
        unknown_keys = set(patch) - _GROUP_SETTING_FIELDS
        if unknown_keys:
            # 预设白名单与真实群设置字段必须同步；漂了就明确报错而不是静默跳过。
            raise _APIError(
                400,
                "preset_patch_unsupported",
                f"预设包含当前不支持的群设置：{', '.join(sorted(unknown_keys))}",
            )
        settings_update = _GroupSettingsUpdate.model_validate(patch)
        async with _group_update_guard(group_id):
            async with session_factory() as session:
                authorized = await session.get(AuthorizedGroup, group_id)
                if authorized is None or not bool(authorized.bot_present):
                    raise _APIError(404, "group_not_found", "该群未授权或不存在。")
                group = await session.get(Group, group_id)
                created_group = group is None
                if group is None:
                    group = Group(id=group_id, title="", settings={})
                    session.add(group)
                stored_settings = dict(group.settings or {})
                if _group_revision(stored_settings) != expected_revision:
                    raise _APIError(
                        409,
                        "group_revision_conflict",
                        "群设置已被其他会话更新，请刷新后重试。",
                    )
                updated_settings = _apply_group_settings(
                    stored_settings, settings_update, settings
                )
                if created_group:
                    group.settings = updated_settings
                    try:
                        await session.commit()
                    except IntegrityError as exc:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        ) from exc
                else:
                    changed = await session.execute(
                        update(Group)
                        .where(
                            Group.id == group_id,
                            Group.settings == stored_settings,
                        )
                        .values(settings=updated_settings)
                        .execution_options(synchronize_session=False)
                    )
                    if int(changed.rowcount or 0) != 1:
                        await session.rollback()
                        raise _APIError(
                            409,
                            "group_revision_conflict",
                            "群设置已被其他会话更新，请刷新后重试。",
                        )
                    await session.commit()
                document = _group_document(group.id, group.title, updated_settings)
        log.info(
            "onboarding preset applied | group=%s preset=%s operator=%s",
            group_id,
            definition.preset_id,
            user.id,
        )
        return _success_response(
            {
                "group": document,
                **group_onboarding.preset_document(definition.preset_id),
            }
        )

    app.router.add_get("/api/v1/session", get_session)
    # 接入申请。注册在 settings 之前只是排版整洁：这些路径与既有路由不重叠。
    app.router.add_get("/api/v1/onboarding/session", get_onboarding_session)
    app.router.add_post("/api/v1/onboarding/check", post_onboarding_check)
    app.router.add_post("/api/v1/onboarding/requests", post_onboarding_request)
    app.router.add_get("/api/v1/onboarding/requests", get_onboarding_requests)
    app.router.add_post(
        "/api/v1/onboarding/requests/{request_id}/cancel",
        cancel_onboarding_request,
    )
    app.router.add_post(
        "/api/v1/onboarding/requests/{request_id}/approve",
        approve_onboarding_request,
    )
    app.router.add_post(
        "/api/v1/onboarding/requests/{request_id}/reject",
        reject_onboarding_request,
    )
    app.router.add_get("/api/v1/groups/{id}/readiness", get_group_readiness)
    app.router.add_get("/api/v1/groups/{id}/costs", get_group_costs)
    app.router.add_get("/api/v1/groups/{id}/budget", get_group_budget)
    app.router.add_put("/api/v1/groups/{id}/budget", put_group_budget)
    app.router.add_get("/api/v1/prices", get_price_catalog)
    app.router.add_put("/api/v1/prices", put_price_catalog)
    app.router.add_post(
        "/api/v1/groups/{id}/preset-preview", post_group_preset_preview
    )
    app.router.add_post(
        "/api/v1/groups/{id}/preset-apply", post_group_preset_apply
    )
    app.router.add_get("/api/v1/settings", get_settings)
    app.router.add_put("/api/v1/settings", put_settings)
    # 配置变更历史。注册顺序有讲究：aiohttp 的 UrlDispatcher **按注册顺序**匹配，
    # 动态段 ``{revision}`` 一旦先于更具体的 ``{revision}/diff`` 注册就会吃掉后续
    # 路径（历史上就因为这个出现过"点差异返回整份配置"的怪现象）。这里把带子路径
    # 的三条排在前面，由 tests/test_runtime_config_history.py 里的
    # test_specific_diff_route_is_not_shadowed_by_the_generic_one 盯着。
    app.router.add_post("/api/v1/settings/diff", post_settings_diff)
    app.router.add_get("/api/v1/settings/history", list_settings_history)
    app.router.add_get(
        "/api/v1/settings/history/{revision}/diff", get_settings_history_diff
    )
    app.router.add_get(
        "/api/v1/settings/history/{revision}/restore-preview",
        get_settings_history_restore_preview,
    )
    app.router.add_post(
        "/api/v1/settings/history/{revision}/restore", post_settings_history_restore
    )
    app.router.add_get(
        "/api/v1/settings/history/{revision}", get_settings_history_revision
    )
    app.router.add_get("/api/v1/authorized-groups", list_authorized_groups_api)
    app.router.add_post("/api/v1/authorized-groups", create_authorized_group_api)
    app.router.add_delete("/api/v1/authorized-groups/{id}", delete_authorized_group_api)
    app.router.add_get("/api/v1/groups/{id}/admins", list_group_admins_api)
    app.router.add_get("/api/v1/groups/{id}/telegram-admins", list_telegram_admins_api)
    app.router.add_post("/api/v1/groups/{id}/admins", create_group_admin_api)
    app.router.add_delete("/api/v1/groups/{id}/admins/{user_id}", delete_group_admin_api)
    app.router.add_get("/api/v1/global-bans", list_global_bans_api)
    app.router.add_post("/api/v1/global-bans", create_global_ban_api)
    app.router.add_delete("/api/v1/global-bans/{user_id}", delete_global_ban_api)
    app.router.add_get("/api/v1/global-exemptions", list_global_exemptions_api)
    app.router.add_delete(
        "/api/v1/global-exemptions/{user_id}",
        delete_global_exemption_api,
    )
    app.router.add_get("/api/v1/groups", get_groups)
    app.router.add_get(
        "/api/v1/groups/{id}/default-permissions",
        get_group_default_permissions,
    )
    app.router.add_put("/api/v1/groups/{id}/settings", put_group_settings)
    app.router.add_get("/api/v1/groups/{id}/rules", list_rules)
    app.router.add_post("/api/v1/groups/{id}/rules", create_rule)
    app.router.add_patch("/api/v1/groups/{id}/rules/{rule_id}", update_rule)
    app.router.add_delete("/api/v1/groups/{id}/rules/{rule_id}", delete_rule)
    app.router.add_get("/api/v1/groups/{id}/keyword-replies", list_keyword_replies)
    app.router.add_post("/api/v1/groups/{id}/keyword-replies", create_keyword_reply)
    app.router.add_patch(
        "/api/v1/groups/{id}/keyword-replies/{entry_id}", update_keyword_reply
    )
    app.router.add_delete(
        "/api/v1/groups/{id}/keyword-replies/{entry_id}", delete_keyword_reply
    )
    app.router.add_get(
        "/api/v1/groups/{id}/scheduled-messages", list_scheduled_messages
    )
    app.router.add_post(
        "/api/v1/groups/{id}/scheduled-messages", create_scheduled_message
    )
    app.router.add_patch(
        "/api/v1/groups/{id}/scheduled-messages/{entry_id}", update_scheduled_message
    )
    app.router.add_delete(
        "/api/v1/groups/{id}/scheduled-messages/{entry_id}", delete_scheduled_message
    )
    app.router.add_get("/api/v1/groups/{id}/memories", list_memories)
    app.router.add_get(
        "/api/v1/groups/{id}/operations/task-status",
        group_operations_task_status,
    )
    # B13a：运营任务中心（写侧）。目录/预览/确认/执行记录/取消/重试/对账。
    app.router.add_get(
        "/api/v1/groups/{id}/operations/tasks", operations_task_catalog
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/preview", preview_operations_task
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/runs", confirm_operations_task
    )
    app.router.add_get(
        "/api/v1/groups/{id}/operations/runs", list_operations_runs
    )
    app.router.add_get(
        "/api/v1/groups/{id}/operations/runs/{run_id}", read_operations_run
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/runs/{run_id}/cancel", cancel_operations_run
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/runs/{run_id}/retry", retry_operations_run
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/runs/{run_id}/reconcile",
        reconcile_operations_run,
    )
    # B13b：按群执行来源与周期计划（读 / 预览 / 确认 / 审计）。
    app.router.add_get(
        "/api/v1/groups/{id}/operations/policy", read_operations_policy
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/policy/preview", preview_operations_policy
    )
    app.router.add_post(
        "/api/v1/groups/{id}/operations/policy/confirm", confirm_operations_policy
    )
    app.router.add_get(
        "/api/v1/groups/{id}/operations/policy/audit", read_operations_policy_audit
    )
    app.router.add_get("/api/v1/groups/{id}/memory-transparency/{section}", read_memory_transparency)
    # C3：稳定 ID 的人工纠错与受控原文展开（都只作用于路径里的本群作用域）。
    app.router.add_get(
        "/api/v1/groups/{id}/memory-transparency/facts/{fact_id}/source",
        read_memory_fact_source,
    )
    app.router.add_post(
        "/api/v1/groups/{id}/memory-transparency/facts/{fact_id}/correction",
        correct_memory_fact,
    )
    app.router.add_post("/api/v1/groups/{id}/memories", create_memory)
    app.router.add_patch("/api/v1/groups/{id}/memories/{memory_id}", update_memory)
    app.router.add_delete("/api/v1/groups/{id}/memories/{memory_id}", delete_memory)
    app.router.add_get("/api/v1/groups/{id}/warnings", list_warnings)
    app.router.add_delete("/api/v1/groups/{id}/warnings/{user_id}", clear_warning)
    app.router.add_get("/api/v1/groups/{id}/bans", list_group_bans)
    app.router.add_post("/api/v1/groups/{id}/bans", create_group_ban)
    app.router.add_delete("/api/v1/groups/{id}/bans/{user_id}", delete_group_ban)
    app.router.add_get("/api/v1/groups/{id}/moderation-exemptions", list_exemptions)
    app.router.add_post("/api/v1/groups/{id}/moderation-exemptions", create_exemption)
    app.router.add_delete("/api/v1/groups/{id}/moderation-exemptions/{user_id}", delete_exemption)
    app.router.add_get("/api/v1/groups/{id}/reply-mutes", list_reply_mutes)
    app.router.add_post("/api/v1/groups/{id}/reply-mutes", create_reply_mute)
    app.router.add_delete("/api/v1/groups/{id}/reply-mutes/{user_id}", delete_reply_mute)
    app.router.add_get("/api/v1/groups/{id}/patrol", get_patrol_status)
    app.router.add_post("/api/v1/groups/{id}/patrol", trigger_patrol)
    app.router.add_post(
        "/api/v1/groups/{id}/moderation-replay/samples/import",
        import_replay_samples,
    )
    app.router.add_post(
        "/api/v1/groups/{id}/moderation-replay/samples", create_replay_sample
    )
    app.router.add_get(
        "/api/v1/groups/{id}/moderation-replay/samples", list_replay_samples
    )
    app.router.add_delete(
        "/api/v1/groups/{id}/moderation-replay/samples/{sample_id}",
        delete_replay_sample,
    )
    # 人工金标（确认/修正/清除）：注册在 DELETE 之前，注册顺序无关
    # （aiohttp 按方法+路径匹配），但把它放在 samples 区块里便于阅读。
    app.router.add_patch(
        "/api/v1/groups/{id}/moderation-replay/samples/{sample_id}/label",
        label_replay_sample,
    )
    app.router.add_post(
        "/api/v1/groups/{id}/moderation-replay/runs", create_replay_run
    )
    app.router.add_get("/api/v1/groups/{id}/moderation-replay/runs", list_replay_runs)
    app.router.add_get(
        "/api/v1/groups/{id}/moderation-replay/runs/{run_id}", get_replay_run
    )
    # ``execute=false`` 的真实后续入口：把已登记的 queued run 排进后台。
    # 注册顺序有讲究：``{run_id}/execute`` 必须排在 ``{run_id}`` 之前注册，
    # 否则通用路由会先吃掉它（与 settings/history 的教训同源）。
    app.router.add_post(
        "/api/v1/groups/{id}/moderation-replay/runs/{run_id}/execute",
        execute_replay_run,
    )
    app.router.add_post(
        "/api/v1/groups/{id}/moderation-replay/runs/{run_id}/cancel",
        cancel_replay_run,
    )


__all__ = ["register_settings_routes"]
