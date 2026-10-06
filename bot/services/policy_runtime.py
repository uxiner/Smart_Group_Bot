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
    AdminOpsPolicyConfig,
    GroupOpsPolicyConfig,
    TelegramSendPolicyConfig,
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




def admin_ops_policy() -> AdminOpsPolicyConfig:
    """管理端名单通知 / 分页 / 特权批处理的操作节奏（现取配置）。"""

    settings = _settings
    if settings is None:
        return AdminOpsPolicyConfig()
    view = getattr(settings, "admin_ops", None)
    return view if isinstance(view, AdminOpsPolicyConfig) else AdminOpsPolicyConfig()


def group_ops_policy() -> GroupOpsPolicyConfig:
    """群内视觉判定输入上限与活跃度写库节奏（现取配置）。"""

    settings = _settings
    if settings is None:
        return GroupOpsPolicyConfig()
    view = getattr(settings, "group_ops", None)
    return view if isinstance(view, GroupOpsPolicyConfig) else GroupOpsPolicyConfig()


def telegram_send_policy() -> TelegramSendPolicyConfig:
    """出站发送的分片 / typing / 流式节流预算（现取配置）。"""

    settings = _settings
    if settings is None:
        return TelegramSendPolicyConfig()
    view = getattr(settings, "telegram_send", None)
    return (
        view
        if isinstance(view, TelegramSendPolicyConfig)
        else TelegramSendPolicyConfig()
    )


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
    "private_chat.per_user_daily_limit": ("bot/services/private_chat.py:quota_limits",),
    "private_chat.admin_per_user_daily_limit": (
        "bot/services/private_chat.py:quota_limits",
    ),
    "private_chat.global_daily_limit": ("bot/services/private_chat.py:quota_limits",),
    "private_chat.admin_global_daily_limit": (
        "bot/services/private_chat.py:quota_limits",
    ),
    "private_chat.input_max_chars": ("bot/services/private_chat.py:build_private_chat_messages",),
    "private_chat.reply_max_chars": ("bot/handlers/private_chat.py:_split_for_telegram",),
    "private_chat.vision_budget_seconds": (
        "bot/handlers/private_chat.py:_image_description",
    ),
    "private_chat.memory_turns": ("bot/services/private_chat.py:PrivateHistoryStore",),
    "private_chat.access_ttl_seconds": (
        "bot/services/private_chat.py:MemberAccessCache",
    ),
    "private_chat.search_daily_limit": ("bot/services/dm_search.py:SearchBudget",),
    "private_chat.voice_max_segments": ("bot/services/private_tts.py:max_private_tts_segments",),
    # --- 经济 ---------------------------------------------------------------
    "economy.checkin_daily_point_cap": ("bot/services/checkin.py:award_for_streak",),
    "economy.checkin_rank_limit": ("bot/services/checkin.py:build_rank",),
    "economy.checkin_violation_window_days": (
        "bot/services/checkin.py:build_rank",
    ),
    "economy.challenge_skip_cost": ("bot/services/checkin.py:challenge_skip_cost",),
    "economy.tag_price_7d": ("bot/services/point_shop.py:buy_member_tag",),
    "economy.tag_days_7d": ("bot/services/point_shop.py:buy_member_tag",),
    "economy.tag_price_30d": ("bot/services/point_shop.py:buy_member_tag",),
    "economy.tag_days_30d": ("bot/services/point_shop.py:buy_member_tag",),
    "economy.pin_price": ("bot/services/point_shop.py:buy_pin",),
    "economy.pin_hours": ("bot/services/point_shop.py:buy_pin",),
    "economy.lottery_price": ("bot/services/point_shop.py:play_lottery",),
    "economy.lottery_daily_limit": ("bot/services/point_shop.py:play_lottery",),
    "economy.lottery_prizes": ("bot/services/point_shop.py:draw_prize",),
    "economy.tag_max_length": ("bot/services/point_shop.py:check_tag_text",),
    "economy.expiry_check_seconds": (
        "bot/services/point_shop.py:ShopExpiryService",
    ),
    "economy.expiry_pass_deadline_seconds": (
        "bot/services/point_shop.py:ShopExpiryService",
    ),
    "economy.expiry_batch_limit": ("bot/services/point_shop.py:ShopExpiryService",),
    "economy.expiry_retry_seconds": ("bot/services/point_shop.py:ShopExpiryService",),
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
    "checkin_reminder.slots": ("bot/services/checkin_reminder.py:reminder_slots",),
    "checkin_reminder.slot_greetings": (
        "bot/services/checkin_reminder.py:reminder_auto_delete_seconds",
    ),
    "checkin_reminder.auto_delete_seconds": (
        "bot/services/checkin_reminder.py:reminder_auto_delete_seconds",
    ),
    "checkin_reminder.roster_max_names": (
        "bot/services/checkin_reminder.py:render_checkin_roster",
    ),
    "checkin_reminder.stale_grace_seconds": (
        "bot/services/checkin_reminder.py:reap_stale_reminder_slots",
    ),
    # --- 显示 ---------------------------------------------------------------
    "display.bot_display_name": ("bot/services/dm_search.py:build_search_query",),
    "display.private_voice_title": (
        "bot/services/private_tts.py:audio_title",
    ),
    "display.checkin_button_text": (
        "bot/services/checkin.py:checkin_button_text",
    ),
    "display.shop_button_text": (
        "bot/services/checkin_reminder.py:shop_button_text",
    ),
    "display.search_query_prefixes": (
        "bot/services/dm_search.py:build_search_query",
    ),
    "display.private_not_member_notice": (
        "bot/handlers/private_chat.py:not_member_notice",
    ),
    "display.private_limit_notice": (
        "bot/services/private_chat.py:quota_notice",
    ),
    "display.private_global_limit_notice": (
        "bot/services/private_chat.py:quota_notice",
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
    # Telegram 三个容量**不由** apply_startup_resources 装配：它们在建
    # ``PriorityAiohttpSession`` 时固化，而 Session 是 ``create_bot`` 建的。真实
    # 调用链是 telegram_session_limits() → PriorityAiohttpSession.__init__ →
    # bot/loader.py create_bot。登记成 apply_startup_resources 会指向一个根本不读
    # 这三个字段的函数——"有登记"不等于"读侧是真的"。
    "resources.telegram_total_capacity": (
        "bot/services/startup_resources.py:telegram_session_limits",
        "bot/services/telegram_session.py:PriorityAiohttpSession",
        "bot/loader.py:create_bot",
    ),
    "resources.telegram_noncritical_capacity": (
        "bot/services/startup_resources.py:telegram_session_limits",
        "bot/services/telegram_session.py:PriorityAiohttpSession",
        "bot/loader.py:create_bot",
    ),
    "resources.telegram_normal_capacity": (
        "bot/services/startup_resources.py:telegram_session_limits",
        "bot/services/telegram_session.py:PriorityAiohttpSession",
        "bot/loader.py:create_bot",
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
        "bot/services/startup_resources.py:telegram_session_limits",
    ),
    "resources.telegram_high_admission_timeout_seconds": (
        "bot/services/startup_resources.py:telegram_session_limits",
    ),
    "resources.telegram_normal_admission_timeout_seconds": (
        "bot/services/startup_resources.py:telegram_session_limits",
    ),
    "resources.telegram_privileged_timeout_seconds": (
        "bot/services/startup_resources.py:telegram_session_limits",
    ),
    "resources.pending_reply_timeout_seconds": (
        "bot/handlers/group.py:_pending_reply_timeout_seconds",
    ),
    "resources.admin_alert_window_seconds": (
        "bot/handlers/group.py:_admin_alert_limits",
    ),
    "resources.admin_alert_aggregate_after": (
        "bot/handlers/group.py:_admin_alert_limits",
    ),
    "resources.admin_alert_state_limit": (
        "bot/handlers/group.py:_admin_alert_limits",
    ),
    "resources.admin_alert_text_limit": (
        "bot/handlers/group.py:_admin_alert_limits",
    ),
    "resources.moderation_throttle_burst": (
        "bot/services/moderation_throttle.py:throttle_limits",
    ),
    "resources.moderation_throttle_spacing_seconds": (
        "bot/services/moderation_throttle.py:throttle_limits",
    ),
    "resources.moderation_throttle_max_wait_seconds": (
        "bot/services/moderation_throttle.py:throttle_limits",
    ),
    "resources.moderation_throttle_max_waiters": (
        "bot/services/moderation_throttle.py:throttle_limits",
    ),
    "resources.tts_max_segments_per_message": (
        "bot/services/doubao_tts.py:max_segments_per_message",
    ),
    "resources.tts_transcode_timeout_seconds": (
        "bot/services/doubao_tts.py:tts_limits",
    ),
    "resources.tts_max_http_timeout_seconds": (
        "bot/services/doubao_tts.py:tts_limits",
    ),
    "resources.av_query_deadline_seconds": (
        "bot/services/av_search.py:av_query_limits",
    ),
    "resources.av_query_admission_timeout_seconds": (
        "bot/services/av_search.py:av_query_limits",
    ),
    "resources.av_star_name_cache_max": (
        "bot/services/av_search.py:av_query_limits",
    ),
    "resources.decision_history_token_budget": (
        "bot/services/group_context.py:decision_history_budget",
    ),
    "resources.decision_history_max_messages": (
        "bot/services/group_context.py:decision_history_budget",
    ),
    "resources.memory_max_facts_per_extraction": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_extract_input_token_limit": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_extract_scope_limit": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_candidate_row_limit": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_private_group_fanout": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_tool_subject_daily_cap": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.memory_maintenance_interval_seconds": (
        "bot/services/long_term_memory.py:memory_limits",
    ),
    "resources.search_prune_interval_seconds": (
        "bot/services/search_memory.py:search_memory_limits",
    ),
    "resources.search_record_recall_limit": (
        "bot/services/search_memory.py:search_memory_limits",
    ),
    "resources.archive_batch_size": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    "resources.archive_backfill_per_pass": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    "resources.archive_scan_limit": ("bot/services/archive_vector.py:archive_limits",),
    "resources.archive_candidate_limit": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    "resources.archive_query_timeout_seconds": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    "resources.archive_maintenance_interval_seconds": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    "resources.archive_indexing_lease_seconds": (
        "bot/services/archive_vector.py:archive_limits",
    ),
    # --- webhook / 轮询 / 出站发送 -------------------------------------------
    "resources.webhook_max_concurrent_updates": (
        "bot/services/update_delivery.py:polling_limits",
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_critical_concurrent_updates": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_security_concurrent_updates": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_auth_concurrent_updates": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_critical_queue_capacity": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_security_queue_capacity": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_auth_queue_capacity": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_update_timeout_seconds": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_critical_update_timeout_seconds": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_security_update_timeout_seconds": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_auth_update_timeout_seconds": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_http_response_timeout_seconds": (
        "bot/services/verify_web.py:webhook_budget",
    ),
    "resources.webhook_inbox_lease_seconds": (
        "bot/services/verify_web.py:inbox_limits",
    ),
    "resources.webhook_inbox_recovery_batch": (
        "bot/services/verify_web.py:inbox_limits",
    ),
    "resources.webhook_inbox_retry_max_seconds": (
        "bot/services/verify_web.py:inbox_limits",
    ),
    "resources.webhook_inbox_cleanup_interval_seconds": (
        "bot/services/verify_web.py:inbox_limits",
    ),
    "resources.webhook_inbox_cleanup_batch": (
        "bot/services/verify_web.py:inbox_limits",
    ),
    "resources.polling_timeout_seconds": (
        "bot/services/update_delivery.py:polling_limits",
    ),
    "resources.polling_http_timeout_seconds": (
        "bot/services/update_delivery.py:polling_limits",
    ),
    "resources.polling_request_timeout_seconds": (
        "bot/services/update_delivery.py:polling_limits",
    ),
    "resources.telegram_send_chat_parallel": (
        "bot/utils/telegram.py:chat_send_parallel",
    ),
    # --- 管理端操作节奏 -----------------------------------------------------
    "admin_ops.roster_notice_auto_delete_seconds": (
        "bot/handlers/admin.py:roster_notice_auto_delete_seconds",
    ),
    "admin_ops.list_page_size": ("bot/handlers/admin.py:list_page_size",),
    "admin_ops.privileged_group_concurrency": ("bot/handlers/admin.py:admin_ops",),
    "admin_ops.privileged_group_deadline_seconds": ("bot/handlers/admin.py:admin_ops",),
    "admin_ops.privileged_job_deadline_seconds": ("bot/handlers/admin.py:admin_ops",),
    # --- 群内视觉 / 活跃度 --------------------------------------------------
    "group_ops.vision_image_max_bytes": ("bot/handlers/group.py:group_ops",),
    "group_ops.vision_download_timeout_seconds": ("bot/handlers/group.py:group_ops",),
    "group_ops.vision_text_max_chars": ("bot/handlers/group.py:group_ops",),
    "group_ops.reply_targets_max_chars": ("bot/handlers/group.py:group_ops",),
    "group_ops.nsfw_warning_auto_delete_seconds": ("bot/handlers/group.py:group_ops",),
    "group_ops.activity_debounce_seconds": ("bot/handlers/group.py:group_ops",),
    "group_ops.activity_max_attempts": ("bot/handlers/group.py:group_ops",),
    # --- 出站发送 -----------------------------------------------------------
    "telegram_send.send_total_deadline_seconds": (
        "bot/utils/telegram.py:send_budget",
    ),
    "telegram_send.stream_max_incremental_edits": (
        "bot/utils/telegram.py:send_budget",
    ),
    "telegram_send.stream_max_pacing_seconds": (
        "bot/utils/telegram.py:send_budget",
    ),
    "telegram_send.typing_send_timeout_seconds": (
        "bot/utils/telegram.py:send_budget",
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
    "admin_ops": admin_ops_policy,
    "group_ops": group_ops_policy,
    "telegram_send": telegram_send_policy,
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
