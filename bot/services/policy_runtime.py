"""热配置读侧：把 ``apply_to_settings`` 写好的 ``Settings`` 变成**不可变快照**。

为什么需要这一层
----------------

新增的运营参数散落在几十个模块里，绝大多数是**纯函数模块 / 模块级单例**
（``bot.services.activity``、``bot.services.point_shop``、``bot.services.dm_search``…），
它们既没有依赖注入的 ``Settings``，也不该为了读一个整数就把整个签名改成异步。

所以这里只做一件事：在 ``RuntimeConfigManager`` 初始化/保存之后把**同一个**
``Settings`` 对象绑进来，暴露若干个**冻结的快照 dataclass**。消费者在**每次
动作开始时取一次快照**，然后整条流程（显示 → 扣费 → 退款 → 回执）都用这同一份。

契约（三条，缺一不可）
----------------------

1. **唯一存储源**仍是 ``runtime_config`` 表 + ``Settings``。本模块不缓存值，
   每次调用都现读绑定进来的 ``Settings``；热更不需要任何额外通知。
2. **没有绑定时**（单测、纯函数直调）返回 ``bot.config`` 里那些策略模型的
   **默认值**——也就是"没配置"时今天的行为。默认值与运行时 schema 的默认值
   由 ``tests/test_configurable_policy_catalog.py`` 逐字段钉住。
3. **一次操作只取一次快照**。价格在中途被管理员改掉时，正在进行的那笔交易
   仍然用同一份价格算到底，绝不会出现"显示 30、扣费 80"这种错账。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.config import (
    ActivityPolicyConfig,
    CheckinReminderPolicyConfig,
    DisplayPolicyConfig,
    EconomyPolicyConfig,
    PrivateChatPolicyConfig,
    ResourcesPolicyConfig,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from bot.config import Settings

log = logging.getLogger(__name__)

#: 绑进来的 ``Settings``。``RuntimeConfigManager`` 在 initialize()/save() 的
#: ``apply_to_settings`` 之后调用 :func:`bind`；测试可用 :func:`unbind` 复位。
_settings: "Settings | None" = None


def bind(settings: "Settings") -> None:
    """绑定当前生效的 ``Settings``（热更后的同一个对象，无需重新绑定）。"""

    global _settings
    _settings = settings


def unbind() -> None:
    """解除绑定，回到"未配置"状态（测试用）。"""

    global _settings
    _settings = None


def bound_settings() -> "Settings | None":
    return _settings


# ---------------------------------------------------------------------------
# 快照类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrivateChatSnapshot:
    per_user_daily_limit: int
    admin_per_user_daily_limit: int
    global_daily_limit: int
    admin_global_daily_limit: int
    input_max_chars: int
    reply_max_chars: int
    vision_budget_seconds: float
    memory_turns: int
    access_ttl_seconds: float
    search_daily_limit: int
    voice_max_segments: int


@dataclass(frozen=True, slots=True)
class LotteryPrize:
    points: int
    weight: int
    label: str


@dataclass(frozen=True, slots=True)
class EconomySnapshot:
    checkin_daily_point_cap: int
    checkin_rank_limit: int
    checkin_violation_window_days: int
    challenge_skip_cost: int
    tag_price_7d: int
    tag_days_7d: int
    tag_price_30d: int
    tag_days_30d: int
    pin_price: int
    pin_hours: int
    lottery_price: int
    lottery_daily_limit: int
    lottery_prizes: tuple[LotteryPrize, ...]
    tag_max_length: int
    expiry_check_seconds: float
    expiry_pass_deadline_seconds: float
    expiry_batch_limit: int
    expiry_retry_seconds: float

    @property
    def lottery_total_weight(self) -> int:
        """总权重由奖池派生——只有这一个来源。"""

        return sum(prize.weight for prize in self.lottery_prizes)


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    min_message_text_length: int
    max_daily_messages: int
    min_active_days: int
    min_weekly_messages: int
    weekly_reward_points: tuple[int, ...]

    @property
    def weekly_top_n(self) -> int:
        return len(self.weekly_reward_points)

    @property
    def weekly_total_points(self) -> int:
        return sum(self.weekly_reward_points)


@dataclass(frozen=True, slots=True)
class CheckinReminderSnapshot:
    slots: tuple[int, ...]
    slot_greetings: tuple[tuple[int, str], ...]
    auto_delete_seconds: int
    roster_max_names: int
    stale_grace_seconds: int

    def greeting_for(self, hour: int) -> str:
        for key, text in self.slot_greetings:
            if key == int(hour):
                return text
        return ""


@dataclass(frozen=True, slots=True)
class DisplaySnapshot:
    bot_display_name: str
    private_voice_title: str
    checkin_button_text: str
    shop_button_text: str
    search_query_prefixes: tuple[str, ...]
    private_not_member_notice: str
    private_limit_notice: str
    private_global_limit_notice: str
    private_media_unsupported_notice: str


@dataclass(frozen=True, slots=True)
class ModerationPolicySnapshot:
    """审核段里这一轮关注的两个部署绑定字段。"""

    log_channel_id: int
    review_handover_mention: str

    @property
    def log_channel_configured(self) -> bool:
        return self.log_channel_id != 0


# ---------------------------------------------------------------------------
# 取快照
# ---------------------------------------------------------------------------


def _private_chat_view() -> PrivateChatPolicyConfig:
    settings = _settings
    if settings is None:
        return PrivateChatPolicyConfig()
    view = getattr(settings, "private_chat", None)
    return view if isinstance(view, PrivateChatPolicyConfig) else PrivateChatPolicyConfig()


def _economy_view() -> EconomyPolicyConfig:
    settings = _settings
    if settings is None:
        return EconomyPolicyConfig()
    view = getattr(settings, "economy", None)
    return view if isinstance(view, EconomyPolicyConfig) else EconomyPolicyConfig()


def _activity_view() -> ActivityPolicyConfig:
    settings = _settings
    if settings is None:
        return ActivityPolicyConfig()
    view = getattr(settings, "activity", None)
    return view if isinstance(view, ActivityPolicyConfig) else ActivityPolicyConfig()


def _checkin_reminder_view() -> CheckinReminderPolicyConfig:
    settings = _settings
    if settings is None:
        return CheckinReminderPolicyConfig()
    view = getattr(settings, "checkin_reminder", None)
    if isinstance(view, CheckinReminderPolicyConfig):
        return view
    return CheckinReminderPolicyConfig()


def _display_view() -> DisplayPolicyConfig:
    settings = _settings
    if settings is None:
        return DisplayPolicyConfig()
    view = getattr(settings, "display", None)
    return view if isinstance(view, DisplayPolicyConfig) else DisplayPolicyConfig()


def private_chat_policy() -> PrivateChatSnapshot:
    """私聊配额/长度/预算的当前快照。一次私聊流程只取一次。"""

    pinned = _pinned("private_chat")
    if pinned is not None:
        return pinned  # type: ignore[return-value]
    view = _private_chat_view()
    return PrivateChatSnapshot(
        per_user_daily_limit=int(view.per_user_daily_limit),
        admin_per_user_daily_limit=int(view.admin_per_user_daily_limit),
        global_daily_limit=int(view.global_daily_limit),
        admin_global_daily_limit=int(view.admin_global_daily_limit),
        input_max_chars=int(view.input_max_chars),
        reply_max_chars=int(view.reply_max_chars),
        vision_budget_seconds=float(view.vision_budget_seconds),
        memory_turns=int(view.memory_turns),
        access_ttl_seconds=float(view.access_ttl_seconds),
        search_daily_limit=int(view.search_daily_limit),
        voice_max_segments=int(view.voice_max_segments),
    )


def economy_policy() -> EconomySnapshot:
    """签到/商店的价格、时长与奖池快照。一笔交易只取一次。"""

    pinned = _pinned("economy")
    if pinned is not None:
        return pinned  # type: ignore[return-value]
    view = _economy_view()
    prizes: list[LotteryPrize] = []
    for entry in view.lottery_prizes:
        points, weight, label = entry
        prizes.append(
            LotteryPrize(points=int(points), weight=int(weight), label=str(label))
        )
    return EconomySnapshot(
        checkin_daily_point_cap=int(view.checkin_daily_point_cap),
        checkin_rank_limit=int(view.checkin_rank_limit),
        checkin_violation_window_days=int(view.checkin_violation_window_days),
        challenge_skip_cost=int(view.challenge_skip_cost),
        tag_price_7d=int(view.tag_price_7d),
        tag_days_7d=int(view.tag_days_7d),
        tag_price_30d=int(view.tag_price_30d),
        tag_days_30d=int(view.tag_days_30d),
        pin_price=int(view.pin_price),
        pin_hours=int(view.pin_hours),
        lottery_price=int(view.lottery_price),
        lottery_daily_limit=int(view.lottery_daily_limit),
        lottery_prizes=tuple(prizes),
        tag_max_length=int(view.tag_max_length),
        expiry_check_seconds=float(view.expiry_check_seconds),
        expiry_pass_deadline_seconds=float(view.expiry_pass_deadline_seconds),
        expiry_batch_limit=int(view.expiry_batch_limit),
        expiry_retry_seconds=float(view.expiry_retry_seconds),
    )


def activity_policy() -> ActivitySnapshot:
    """活跃激励快照。一次「记录一条消息」/「结算一周」只取一次。"""

    pinned = _pinned("activity")
    if pinned is not None:
        return pinned  # type: ignore[return-value]
    view = _activity_view()
    return ActivitySnapshot(
        min_message_text_length=int(view.min_message_text_length),
        max_daily_messages=int(view.max_daily_messages),
        min_active_days=int(view.min_active_days),
        min_weekly_messages=int(view.min_weekly_messages),
        weekly_reward_points=tuple(int(item) for item in view.weekly_reward_points),
    )


def checkin_reminder_policy() -> CheckinReminderSnapshot:
    """签到提醒快照。渲染一条提醒时取一次。"""

    pinned = _pinned("checkin_reminder")
    if pinned is not None:
        return pinned  # type: ignore[return-value]
    view = _checkin_reminder_view()
    return CheckinReminderSnapshot(
        slots=tuple(int(hour) for hour in view.slots),
        slot_greetings=tuple(
            (int(hour), str(text)) for hour, text in sorted(view.slot_greetings.items())
        ),
        auto_delete_seconds=int(view.auto_delete_seconds),
        roster_max_names=int(view.roster_max_names),
        stale_grace_seconds=int(view.stale_grace_seconds),
    )


def display_policy() -> DisplaySnapshot:
    """品牌与文案快照。构造一条消息 / 一个按钮时取一次。"""

    pinned = _pinned("display")
    if pinned is not None:
        return pinned  # type: ignore[return-value]
    view = _display_view()
    return DisplaySnapshot(
        bot_display_name=str(view.bot_display_name),
        private_voice_title=str(view.private_voice_title),
        checkin_button_text=str(view.checkin_button_text),
        shop_button_text=str(view.shop_button_text),
        search_query_prefixes=tuple(str(item) for item in view.search_query_prefixes),
        private_not_member_notice=str(view.private_not_member_notice),
        private_limit_notice=str(view.private_limit_notice),
        private_global_limit_notice=str(view.private_global_limit_notice),
        private_media_unsupported_notice=str(view.private_media_unsupported_notice),
    )


def resources_policy() -> ResourcesPolicyConfig:
    """高级资源/预算的当前视图。

    注意语义：``reload_kind == "restart"`` 的字段在**这里**读到的是"下次重启
    才会被装配进模块级闸门"的目标值；想确认当前进程里真正生效的容量，用
    :func:`bot.services.startup_resources.resource_health_report`。
    """

    settings = _settings
    if settings is None:
        return ResourcesPolicyConfig()
    view = getattr(settings, "resources", None)
    if isinstance(view, ResourcesPolicyConfig):
        return view
    return ResourcesPolicyConfig()


def moderation_handover_policy() -> ModerationPolicySnapshot:
    """审核交接的部署绑定字段（频道 id / 交接对象）。"""

    settings = _settings
    moderation = getattr(settings, "moderation", None) if settings else None
    return ModerationPolicySnapshot(
        log_channel_id=int(getattr(moderation, "log_channel_id", 0) or 0),
        review_handover_mention=str(
            getattr(moderation, "review_handover_mention", "") or ""
        ).strip(),
    )


# ---------------------------------------------------------------------------
# 消费者登记表
# ---------------------------------------------------------------------------
#
# ``docs/configuration.md`` 与机器可读目录（``docs/configuration-fields.json``）
# 都从这里生成 / 校验：一张新字段如果没登记读侧，就**不算完成**——
# ``tests/test_configurable_policy_catalog.py`` 会因此失败。

#: 字段路径 → 真实读侧（``file:symbol``，用 ``:`` 分隔多个）。
CONSUMER_REGISTRY: dict[str, tuple[str, ...]] = {
    # --- 私聊 ---------------------------------------------------------------
    "private_chat.per_user_daily_limit": ("bot/services/private_chat.py:quota_limits"),
    "private_chat.admin_per_user_daily_limit": (
        "bot/services/private_chat.py:quota_limits",
    ),
    "private_chat.global_daily_limit": ("bot/services/private_chat.py:quota_limits"),
    "private_chat.admin_global_daily_limit": (
        "bot/services/private_chat.py:quota_limits",
    ),
    "private_chat.input_max_chars": ("bot/services/private_chat.py:normalize_input"),
    "private_chat.reply_max_chars": ("bot/handlers/private_chat.py:split_for_telegram"),
    "private_chat.vision_budget_seconds": (
        "bot/handlers/private_chat.py:describe_image",
    ),
    "private_chat.memory_turns": ("bot/services/private_chat.py:remember_exchange"),
    "private_chat.access_ttl_seconds": (
        "bot/services/private_chat.py:MemberAccessCache.__init__",
    ),
    "private_chat.search_daily_limit": ("bot/services/dm_search.py:SearchBudget"),
    "private_chat.voice_max_segments": ("bot/services/private_tts.py:build_segments"),
    # --- 经济 ---------------------------------------------------------------
    "economy.checkin_daily_point_cap": ("bot/services/checkin.py:award_for_streak",),
    "economy.checkin_rank_limit": ("bot/services/checkin.py:build_rank",),
    "economy.checkin_violation_window_days": (
        "bot/services/checkin.py:build_rank",
    ),
    "economy.challenge_skip_cost": ("bot/services/checkin.py:challenge_skip_cost",),
    "economy.tag_price_7d": ("bot/services/point_shop.py:buy_tag",),
    "economy.tag_days_7d": ("bot/services/point_shop.py:buy_tag",),
    "economy.tag_price_30d": ("bot/services/point_shop.py:buy_tag",),
    "economy.tag_days_30d": ("bot/services/point_shop.py:buy_tag",),
    "economy.pin_price": ("bot/services/point_shop.py:buy_pin",),
    "economy.pin_hours": ("bot/services/point_shop.py:buy_pin",),
    "economy.lottery_price": ("bot/services/point_shop.py:draw_lottery",),
    "economy.lottery_daily_limit": ("bot/services/point_shop.py:draw_lottery",),
    "economy.lottery_prizes": ("bot/services/point_shop.py:draw_prize",),
    "economy.tag_max_length": ("bot/services/point_shop.py:validate_tag_text",),
    "economy.expiry_check_seconds": (
        "bot/services/point_shop.py:expiry_worker",
    ),
    "economy.expiry_pass_deadline_seconds": (
        "bot/services/point_shop.py:expiry_worker",
    ),
    "economy.expiry_batch_limit": ("bot/services/point_shop.py:expiry_worker",),
    "economy.expiry_retry_seconds": ("bot/services/point_shop.py:expiry_worker",),
    # --- 活跃 ---------------------------------------------------------------
    "activity.min_message_text_length": (
        "bot/services/activity.py:is_countable_message",
    ),
    "activity.max_daily_messages": (
        "bot/services/activity.py:record_message_activity",
    ),
    "activity.min_active_days": ("bot/services/activity.py:meets_threshold",),
    "activity.min_weekly_messages": ("bot/services/activity.py:meets_threshold",),
    "activity.weekly_reward_points": (
        "bot/services/activity.py:reward_points_for_rank",
    ),
    # --- 签到提醒 -----------------------------------------------------------
    "checkin_reminder.slots": ("bot/services/checkin_reminder.py:normalize_slot",),
    "checkin_reminder.slot_greetings": (
        "bot/services/checkin_reminder.py:render_checkin_reminder",
    ),
    "checkin_reminder.auto_delete_seconds": (
        "bot/services/checkin_reminder.py:render_checkin_reminder",
    ),
    "checkin_reminder.roster_max_names": (
        "bot/services/checkin_reminder.py:render_checkin_roster",
    ),
    "checkin_reminder.stale_grace_seconds": (
        "bot/services/checkin_reminder.py:release_reminder_slot",
    ),
    # --- 显示 ---------------------------------------------------------------
    "display.bot_display_name": ("bot/services/dm_search.py:build_search_query",),
    "display.private_voice_title": (
        "bot/services/private_tts.py:send_private_voice",
    ),
    "display.checkin_button_text": (
        "bot/services/checkin.py:build_checkin_keyboard",
    ),
    "display.shop_button_text": (
        "bot/services/checkin_reminder.py:build_checkin_reminder_keyboard",
    ),
    "display.search_query_prefixes": (
        "bot/services/dm_search.py:build_search_query",
    ),
    "display.private_not_member_notice": (
        "bot/handlers/private_chat.py:not_member_notice",
    ),
    "display.private_limit_notice": (
        "bot/handlers/private_chat.py:quota_notice",
    ),
    "display.private_global_limit_notice": (
        "bot/handlers/private_chat.py:quota_notice",
    ),
    "display.private_media_unsupported_notice": (
        "bot/handlers/private_chat.py:unsupported_media_notice",
    ),
    # --- 审核（部署绑定） ---------------------------------------------------
    "moderation.log_channel_id": (
        "bot/handlers/group.py:_admin_log_channel_id",
    ),
    "moderation.review_handover_mention": (
        "bot/handlers/group.py:_send_review_handover",
    ),
    # --- 资源（启动装配 / 热读） ---------------------------------------------
    "resources.llm_request_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.llm_request_noncritical_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.llm_request_normal_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.llm_request_background_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.llm_tokenizer_concurrency": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.telegram_total_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.telegram_noncritical_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.telegram_normal_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.pending_reply_execution_capacity": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.tts_synthesis_concurrency": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.tts_transcode_concurrency": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.tts_private_concurrency": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.av_query_concurrency": (
        "bot/services/startup_resources.py:apply_startup_resources",
    ),
    "resources.llm_stage_deadlines": ("bot/services/llm.py:stage_deadline_seconds",),
    "resources.telegram_critical_admission_timeout_seconds": (
        "bot/services/telegram_session.py:PriorityAiohttpSession",
    ),
    "resources.telegram_high_admission_timeout_seconds": (
        "bot/services/telegram_session.py:PriorityAiohttpSession",
    ),
    "resources.telegram_normal_admission_timeout_seconds": (
        "bot/services/telegram_session.py:PriorityAiohttpSession",
    ),
    "resources.telegram_privileged_timeout_seconds": (
        "bot/services/telegram_session.py:PriorityAiohttpSession",
    ),
    "resources.pending_reply_timeout_seconds": (
        "bot/handlers/group.py:deliver_pending_replies",
    ),
    "resources.admin_alert_window_seconds": (
        "bot/handlers/group.py:send_admin_violation_alert",
    ),
    "resources.admin_alert_aggregate_after": (
        "bot/handlers/group.py:send_admin_violation_alert",
    ),
    "resources.admin_alert_state_limit": (
        "bot/handlers/group.py:send_admin_violation_alert",
    ),
    "resources.admin_alert_text_limit": (
        "bot/handlers/group.py:send_admin_violation_alert",
    ),
    "resources.moderation_throttle_burst": (
        "bot/services/moderation_throttle.py:admit",
    ),
    "resources.moderation_throttle_spacing_seconds": (
        "bot/services/moderation_throttle.py:admit",
    ),
    "resources.moderation_throttle_max_wait_seconds": (
        "bot/services/moderation_throttle.py:admit",
    ),
    "resources.moderation_throttle_max_waiters": (
        "bot/services/moderation_throttle.py:admit",
    ),
    "resources.tts_max_segments_per_message": (
        "bot/services/doubao_tts.py:build_segments",
    ),
    "resources.tts_transcode_timeout_seconds": (
        "bot/services/doubao_tts.py:transcode",
    ),
    "resources.tts_max_http_timeout_seconds": (
        "bot/services/doubao_tts.py:request",
    ),
    "resources.av_query_deadline_seconds": (
        "bot/services/av_search.py:run_av_query_bounded",
    ),
    "resources.av_query_admission_timeout_seconds": (
        "bot/services/av_search.py:run_av_query_bounded",
    ),
    "resources.av_star_name_cache_max": (
        "bot/services/av_search.py:star_name_cache",
    ),
    "resources.decision_history_token_budget": (
        "bot/services/group_context.py:decision_history_budget",
    ),
    "resources.decision_history_max_messages": (
        "bot/services/group_context.py:decision_history_budget",
    ),
    "resources.memory_max_facts_per_extraction": (
        "bot/services/long_term_memory.py:parse_extraction",
    ),
    "resources.memory_extract_input_token_limit": (
        "bot/services/long_term_memory.py:build_extraction_input",
    ),
    "resources.memory_extract_scope_limit": (
        "bot/services/long_term_memory.py:iter_extraction_scopes",
    ),
    "resources.memory_candidate_row_limit": (
        "bot/services/long_term_memory.py:rank_candidates",
    ),
    "resources.memory_private_group_fanout": (
        "bot/services/long_term_memory.py:render_private_facts",
    ),
    "resources.memory_tool_subject_daily_cap": (
        "bot/services/long_term_memory.py:remember_fact",
    ),
    "resources.memory_maintenance_interval_seconds": (
        "bot/services/long_term_memory.py:start_maintenance",
    ),
    "resources.search_prune_interval_seconds": (
        "bot/services/search_memory.py:prune_loop",
    ),
    "resources.search_record_recall_limit": (
        "bot/services/search_memory.py:render_search_record_messages",
    ),
    "resources.archive_batch_size": (
        "bot/services/archive_vector.py:SQLiteArchiveVectorRecallProvider",
    ),
    "resources.archive_backfill_per_pass": (
        "bot/services/archive_vector.py:backfill_once",
    ),
    "resources.archive_scan_limit": ("bot/services/archive_vector.py:scan_once",),
    "resources.archive_candidate_limit": (
        "bot/services/archive_vector.py:recall",
    ),
    "resources.archive_query_timeout_seconds": (
        "bot/services/archive_vector.py:recall",
    ),
    "resources.archive_maintenance_interval_seconds": (
        "bot/services/archive_vector.py:index_once",
    ),
    "resources.archive_indexing_lease_seconds": (
        "bot/services/archive_vector.py:acquire_lease",
    ),
}


def consumer_paths(field_path: str) -> tuple[str, ...]:
    """取某个字段登记的读侧（``file:symbol``）。未登记返回空元组。"""

    return CONSUMER_REGISTRY.get(str(field_path), ())


# ---------------------------------------------------------------------------
# 一次操作钉一份快照
# ---------------------------------------------------------------------------
#
# 「显示 → 扣费 → 退款 → 回执」必须用同一份价格：管理员在交易进行到一半时改价，
# 不能出现"菜单显示 30 分、实际扣了 80 分"这种错账。
#
# ``ContextVar`` 正好合用：asyncio 每个 Task 都拷贝一份上下文，所以在一个 handler
# 任务里钉的快照不会漏给别的请求。嵌套调用复用最外层那份。

_SECTION_SNAPSHOTS: ContextVar[dict[str, object] | None] = ContextVar(
    "smart_group_bot_policy_snapshots", default=None
)

_SNAPSHOT_BUILDERS = {
    "private_chat": private_chat_policy,
    "economy": economy_policy,
    "activity": activity_policy,
    "checkin_reminder": checkin_reminder_policy,
    "display": display_policy,
}


def _pinned(section: str):
    """当前操作钉住的那份快照（没钉返回 None）。"""

    stack = _SECTION_SNAPSHOTS.get()
    if stack is not None:
        return stack.get(section)
    return None


@contextmanager
def pinned_section(section: str):
    """把某一段的快照钉在**当前操作**上，整条流程只读这一份。

    用法::

        with policy_runtime.pinned_section("economy") as econ:
            ...  # 显示、扣费、退款、回执全部用 econ，不再回查配置
    """

    stack = _SECTION_SNAPSHOTS.get()
    if stack is not None and section in stack:
        yield stack[section]
        return
    builder = _SNAPSHOT_BUILDERS.get(section)
    if builder is None:
        raise KeyError(f"unknown policy section: {section}")
    snapshot = builder()
    pinned = dict(stack or {})
    pinned[section] = snapshot
    token = _SECTION_SNAPSHOTS.set(pinned)
    try:
        yield snapshot
    finally:
        _SECTION_SNAPSHOTS.reset(token)
