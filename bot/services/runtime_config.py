from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import (
    ActivityPolicyConfig,
    CheckinReminderPolicyConfig,
    DisplayPolicyConfig,
    EconomyPolicyConfig,
    EmbedConfig,
    ModelConfig,
    ModerationConfig,
    PrivateChatPolicyConfig,
    ProviderProfile,
    ResourcesPolicyConfig,
    Settings,
    _build_chat_config,
    _build_embed_config,
    _build_litellm_model,
    _collect_provider_profiles,
    _load_raw_env,
    _parse_fallbacks,
    _resolve_provider_profile,
)
from bot.db.models import RuntimeConfigRecord, RuntimeConfigSecret
from bot.utils.budget import (
    BUSINESS_CONTEXT_TOKENS_MAX,
    BUSINESS_CONTEXT_TOKENS_MIN,
    CONTEXT_RESERVE_TOKENS_MAX,
    CONTEXT_RESERVE_TOKENS_MIN,
    DEFAULT_GROUP_HISTORY_MAX_MESSAGES,
    GROUP_HISTORY_MAX_MESSAGES_MAX,
    GROUP_HISTORY_MAX_MESSAGES_MIN,
    validate_business_budget,
    validate_group_history_max_messages,
)
from bot.services import policy_runtime
from bot.utils.prompts import load_prompt_defaults, set_runtime_prompts

log = logging.getLogger(__name__)

#: 运行时配置文档的 schema 版本。抬到 2 是因为 F-014：退役"owner 优先"语义的
#: 一次性提示词迁移需要一个只执行一次的标记，而这个版本号正好是持久化文档里现成
#: 的字段，不必新增列。
CONFIG_SCHEMA_VERSION = 2

#: 第②项后台摘要的全部运行时字段名（apply / legacy 导入 / 顶层同步共用一份清单）。
_GROUP_SUMMARY_FIELD_NAMES: tuple[str, ...] = (
    "group_summary_enabled",
    "group_summary_recent_raw_messages",
    "group_summary_max_tokens",
    "group_summary_batch_max_messages",
    "group_summary_batch_max_input_tokens",
    "group_summary_global_concurrency",
    "group_summary_per_group_concurrency",
    "group_summary_deadline_seconds",
    "group_summary_queue_wait_seconds",
    "group_summary_min_refresh_seconds",
    "group_summary_failure_backoff_seconds",
    "group_summary_failure_backoff_max_seconds",
    "group_summary_pending_capacity",
    "group_summary_trigger_messages",
    "group_summary_trigger_budget_ratio",
)
#: F-014：已经退役的「owner 优先」提示词标记。只重写命中这些标记的提示词，
#: 其余自定义内容（含明确的管理员语义）一个字都不动——宁可漏改，不可误伤。
_RETIRED_OWNER_PRIORITY_PROMPT_MARKERS: dict[str, tuple[str, ...]] = {
    "decision": ("If [SENDER_IS_OWNER]=yes: as long as",),
    "persona": (
        "The owner's instructions have the highest priority.",
        "Always prioritize the owner's messages.",
        "you still prioritize the owner",
    ),
    "casual": (
        "more likely to prioritize the owner's messages",
        "prioritize responding, be soft and affectionate",
    ),
}
_STATIC_SECRET_PATHS = (
    "verification.turnstile_secret_key",
    "verification.hcaptcha_secret_key",
    "tts.app_key",
    "tts.access_key",
    "movie_info.tmdb_read_access_token",
    "movie_info.imdb_api_key",
    "movie_info.imdb_aws_access_key_id",
    "movie_info.imdb_aws_secret_access_key",
    "movie_info.imdb_aws_session_token",
)

#: ``json_schema_extra`` 标记：改完**下一次动作**就生效（热读）。
_HOT: dict[str, Any] = {"reload_kind": "hot"}
#: ``json_schema_extra`` 标记：绑在模块级 Semaphore / gate / aiohttp Session 上，
#: 只在启动时装配一次，**改完必须重启**（见 ``bot.services.startup_resources``）。
_RESTART: dict[str, Any] = {"reload_kind": "restart"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    provider: str = Field(default="gemini", min_length=1, max_length=64)
    api_key: str = ""
    api_base: str = ""
    stream: bool = False
    chat_endpoint: Literal["auto", "chat_completions", "responses"] = "auto"

    @field_validator("name", "provider", mode="before")
    @classmethod
    def _clean_identifier(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("api_key", "api_base", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()


class ModelFallbackConfig(StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=255)
    # Provider-specific JSON for this concrete fallback model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


_REQUEST_PARAMS_MAX_BYTES = 64 * 1024


def _normalize_request_params(value: object) -> dict[str, Any]:
    """Validate and copy one provider-specific JSON object.

    Runtime settings arrive from an untrusted Mini App request.  Normalizing
    through the stdlib JSON codec guarantees that values are JSON-compatible
    before they are persisted and bounds the size of one parameter object.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("request_params 必须是 JSON 对象")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("request_params 必须只包含 JSON 可序列化值") from exc
    if len(encoded.encode("utf-8")) > _REQUEST_PARAMS_MAX_BYTES:
        raise ValueError("request_params 不能超过 64 KiB")
    return normalized


class ChatRoleConfig(StrictModel):
    provider: str = ""
    model: str = ""
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, allow_inf_nan=False)
    max_tokens: int = Field(default=2048, ge=1, le=2_000_000)
    timeout_sec: float = Field(default=12.0, ge=1.0, le=600.0, allow_inf_nan=False)
    # 单次调用（含同模型重试与整条回退链）的总时限；0 = 使用内置默认。
    total_deadline_sec: float = Field(
        default=0.0, ge=0.0, le=3600.0, allow_inf_nan=False
    )
    # Provider-specific JSON for this role's concrete primary model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


class EmbedRoleConfig(StrictModel):
    provider: str = ""
    model: str = Field(default="text-embedding-004", min_length=1, max_length=255)
    fallbacks: list[ModelFallbackConfig] = Field(default_factory=list)
    timeout_sec: float = Field(default=10.0, ge=1.0, le=600.0, allow_inf_nan=False)
    # 单次调用（含同模型重试与整条回退链）的总时限；0 = 使用内置默认。
    total_deadline_sec: float = Field(
        default=0.0, ge=0.0, le=3600.0, allow_inf_nan=False
    )
    # Provider-specific JSON for this role's concrete embedding model only.
    request_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _clean_provider(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("request_params", mode="before")
    @classmethod
    def _clean_request_params(cls, value: object) -> dict[str, Any]:
        return _normalize_request_params(value)


class ModelSettingsConfig(StrictModel):
    providers: list[ProviderConfig] = Field(
        default_factory=lambda: [ProviderConfig(name="gemini", provider="gemini")]
    )
    main: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            provider="gemini",
            model="gemini-2.0-flash",
            temperature=0.7,
            max_tokens=2048,
            timeout_sec=12.0,
        )
    )
    vision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.7,
            max_tokens=2048,
            timeout_sec=15.0,
        )
    )
    decision: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=512,
            timeout_sec=6.0,
        )
    )
    moderation: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.1,
            max_tokens=1024,
            timeout_sec=8.0,
        )
    )
    compress: ChatRoleConfig = Field(
        default_factory=lambda: ChatRoleConfig(
            temperature=0.3,
            max_tokens=1024,
            timeout_sec=12.0,
        )
    )
    # Tool-calling (skills) stage.  ``None`` keeps the legacy behaviour of
    # reusing ``main``; pointing it at a tool-capable provider stops the
    # stage from burning budget on an endpoint that cannot call tools.
    skill: ChatRoleConfig | None = None
    embed: EmbedRoleConfig = Field(default_factory=EmbedRoleConfig)
    retry_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_sec: float = Field(default=0.8, ge=0.0, le=60.0, allow_inf_nan=False)
    retry_timeout_multiplier: float = Field(default=1.35, ge=1.0, le=10.0, allow_inf_nan=False)


class BotBehaviorConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")

    parse_mode: str = Field(default="HTML", max_length=32)
    disable_link_preview: bool = True
    # Deprecated compatibility field. Automatic lifecycle operations must
    # never discard Telegram's backlog; keep accepting old payloads while
    # normalizing every read/write to the safe value.
    drop_pending_updates: bool = False
    inbound_debounce_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    reply_batch_timeout_seconds: float = Field(default=45.0, ge=5.0, le=120.0)
    enable_typing: bool = True
    # D3-40：富文本（粗体/斜体/代码块等）渲染总开关。此前它只存在于
    # ``bot/config.py`` 的 ``load_settings()`` 里，而那个函数**全仓无生产调用点**，
    # 于是它既无 env 生效路径、又不在 schema 里，``getattr(..., False)`` 恒真——
    # 唯一一个「既无 UI、又无 env、又无 DB」的行为开关。这里补齐三方接线
    # （schema + apply + Mini App），**不**去把 ``load_settings()`` 改成被调用。
    enable_rich_messages: bool = True
    enable_streaming: bool = True
    stream_chunk_size: int = Field(default=36, ge=8, le=4096)
    stream_edit_interval_sec: float = Field(default=1.0, ge=0.3, le=30.0)
    auto_delete_seconds: int = Field(default=0, ge=0, le=604800)
    auto_delete_categories: list[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ]
    ] = Field(default_factory=lambda: ["management", "moderation"])
    # Per-category retention overrides (seconds); 0 or missing inherits
    # auto_delete_seconds.
    auto_delete_category_seconds: dict[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ],
        int,
    ] = Field(default_factory=dict)
    # Per-category cleanup mode: "timer" (default, delayed delete) or
    # "button" (inline delete button; mutually exclusive with the timer).
    # Only "button" entries are persisted.
    auto_delete_category_mode: dict[
        Literal[
            "reply",
            "management",
            "moderation",
            "media",
            "proactive",
            "keyword",
            "scheduled",
            "welcome",
            "call_admin",
            "vote",
        ],
        Literal["timer", "button"],
    ] = Field(default_factory=dict)
    # Accepted only while reading records written before the seconds migration.
    auto_delete_minutes: int | None = Field(default=None, ge=0, le=10080, exclude=True)
    decision_context_items: int = Field(default=5, ge=0, le=20)
    # 上下文模式：``auto``（默认）= 自动发现模型真实窗口（网关 /models 优先，其次直连
    # 厂商注册表，都没有才退回 ``max_context_tokens`` 保守降级），发现值只用于"模型比
    # 业务预算小就跟着小"；``fixed`` = 不查元数据，``max_context_tokens`` 即模型侧上限。
    # 两种模式都再叠固定的 272Ki 业务预算（每轮总窗口）。老配置没有这个字段 → 迁移成 auto。
    context_window_mode: Literal["auto", "fixed"] = "auto"
    max_context_tokens: int = Field(default=278528, ge=1024, le=2_000_000)
    # 每轮业务总预算 / 输出工具预留 / 群历史单次读取条数（运行时可读写）。
    # 默认是推荐值（272Ki / 32Ki / 1000）；显式配置不被隐藏常量截断，文档建议
    # 业务总预算不超过 272Ki。预留必须小于总预算，0 不能关掉门禁。
    context_budget_tokens: int = Field(
        default=278528,
        ge=BUSINESS_CONTEXT_TOKENS_MIN,
        le=BUSINESS_CONTEXT_TOKENS_MAX,
    )
    context_reserve_tokens: int = Field(
        default=32768,
        ge=CONTEXT_RESERVE_TOKENS_MIN,
        le=CONTEXT_RESERVE_TOKENS_MAX,
    )
    group_history_max_messages: int = Field(
        default=DEFAULT_GROUP_HISTORY_MAX_MESSAGES,
        ge=GROUP_HISTORY_MAX_MESSAGES_MIN,
        le=GROUP_HISTORY_MAX_MESSAGES_MAX,
    )
    max_output_tokens: int = Field(default=2048, ge=256, le=2_000_000)
    memory_recent_messages: int = Field(default=500, ge=50, le=2000)
    memory_retention_days: int = Field(default=7, ge=1, le=365)
    memory_archive_max_messages_per_group: int = Field(
        default=50000,
        ge=1000,
        le=1_000_000,
    )
    memory_recall_enabled: bool = True
    memory_recall_max_results: int = Field(default=8, ge=1, le=20)
    memory_automatic_compaction: bool = False
    # 第②项：后台群摘要（默认关闭；验收后可显式开启；与 legacy 热压缩独立）。
    group_summary_enabled: bool = False
    group_summary_recent_raw_messages: int = Field(default=200, ge=20, le=10_000)
    group_summary_max_tokens: int = Field(default=4096, ge=256, le=32_768)
    group_summary_batch_max_messages: int = Field(default=200, ge=10, le=2_000)
    group_summary_batch_max_input_tokens: int = Field(
        default=16_384, ge=1_024, le=1_000_000
    )
    group_summary_global_concurrency: int = Field(default=2, ge=1, le=8)
    # 每群并发是硬安全约束：只能 1。
    group_summary_per_group_concurrency: int = Field(default=1, ge=1, le=1)
    group_summary_deadline_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    group_summary_queue_wait_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    group_summary_min_refresh_seconds: float = Field(
        default=60.0, ge=0.0, le=86_400.0
    )
    group_summary_failure_backoff_seconds: float = Field(
        default=60.0, ge=1.0, le=86_400.0
    )
    group_summary_failure_backoff_max_seconds: float = Field(
        default=3600.0, ge=1.0, le=86_400.0
    )
    group_summary_pending_capacity: int = Field(default=1000, ge=1, le=100_000)
    group_summary_trigger_messages: int = Field(default=200, ge=1, le=100_000)
    group_summary_trigger_budget_ratio: float = Field(
        default=0.85, ge=0.1, le=1.0
    )

    @model_validator(mode="after")
    def _validate_business_budget(self) -> "BotBehaviorConfig":
        """显式校验：预留必须小于业务总预算、条数在合法区间，0 不能关闭门禁。"""

        error = validate_business_budget(
            self.context_budget_tokens,
            self.context_reserve_tokens,
        ) or validate_group_history_max_messages(self.group_history_max_messages)
        if error:
            raise ValueError(error)
        if (
            self.group_summary_failure_backoff_max_seconds
            < self.group_summary_failure_backoff_seconds
        ):
            raise ValueError("摘要失败退避上限必须不小于起点")
        return self

    # Private-chat history: 278528 = 272K legacy depth target and the per-turn
    # business ceiling.  A *smaller* discovered model window tightens it further;
    # a 1M/4M window never loosens it (the turn is not filled to the model window).
    private_chat_history_token_budget: int = Field(
        default=278528,
        ge=1024,
        le=2_000_000,
    )
    private_chat_history_retention_days: int = Field(default=30, ge=1, le=365)
    # Group-chat history: assembled from the group archive by token budget (at most
    # the most recent ``GROUP_HISTORY_MAX_MESSAGES`` = 1000 rows) instead of "most
    # recent N messages".  ``reserve_tokens`` is the headroom kept for the fixed
    # prompt parts; assembled history + reserve must stay within the effective
    # window = min(discovered model window, 272Ki business budget).  The hard gate
    # lives in bot.services.group_context.
    group_history_token_budget: int = Field(default=278528, ge=1024, le=2_000_000)
    group_history_reserve_tokens: int = Field(default=32768, ge=1024, le=1_000_000)
    # 第 3 期：检索结果留档（search_result_records）。
    # retention 默认 30 天；新鲜窗口默认 价格 24h / 新闻 48h / 事实 7d(=168h)。
    # 超出窗口的留档在注入时会标注「可能已过期」，怎么用由模型自己判断。
    search_record_retention_days: int = Field(default=30, ge=1, le=365)
    search_freshness_price_hours: int = Field(default=24, ge=1, le=8760)
    search_freshness_news_hours: int = Field(default=48, ge=1, le=8760)
    search_freshness_fact_hours: int = Field(default=168, ge=1, le=8760)
    # 第 3 期方向规则：群 → 私聊允许；私聊 → 群**默认禁止**。默认 False 时群聊装配
    # 上下文的任何路径都不读 ``private_chat_messages``；本期不实现打开后的读取逻辑
    # （开启需要用户显式授权，届时再补读取器、授权校验与审计）。
    # 第 4 期：长期记忆（``user_facts``）——从对话里提炼的稳定事实。
    # 全部可运行时覆盖；语义见 ``bot.services.long_term_memory`` 与 ``bot/config.py``。
    memory_facts_enabled: bool = True
    memory_extract_enabled: bool = True
    memory_extract_interval_minutes: int = Field(default=30, ge=5, le=1440)
    memory_extract_min_messages: int = Field(default=20, ge=5, le=500)
    #: 每天最多提炼次数（0 = 不限，防成本失控）
    memory_extract_daily_cap: int = Field(default=48, ge=0, le=500)
    memory_extract_batch_max: int = Field(default=200, ge=20, le=1000)
    memory_tool_enabled: bool = True
    #: 每个作用域每天模型主动写（remember 工具）的条数上限（0 = 不限）
    memory_tool_daily_cap: int = Field(default=30, ge=0, le=200)
    memory_recall_limit: int = Field(default=8, ge=1, le=20)
    #: category='event' 的过期天数
    memory_event_ttl_days: int = Field(default=30, ge=1, le=365)
    #: 被删除/被替代的事实保留多少天后物理清理
    memory_deleted_retention_days: int = Field(default=30, ge=1, le=365)
    #: B-39 / P4-9：每日提炼台账的保留期（天）。默认 **1 = 只留当天**。
    #:
    #: MEM-001 补的缺口：``BotConfig`` 早有同名字段、``long_term_memory`` 也早有读侧
    #: ``memory_extract_ledger_retention_days(settings)``，但这一段 schema 里**没有**它，
    #: 于是 Mini App 存不了、``apply_to_settings`` 也写不下去，读侧永远拿到默认值。
    #: 这里只补齐闭环，不重做已有实现。
    memory_extract_ledger_retention_days: int = Field(default=1, ge=1, le=365)
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = Field(default=180, ge=180, le=43200)
    proactive_jitter_minutes: int = Field(default=60, ge=0, le=1440)
    proactive_check_interval_seconds: float = Field(default=60.0, ge=15.0, le=3600.0)
    proactive_quiet_hours_start: int = Field(default=0, ge=0, le=23)
    proactive_quiet_hours_end: int = Field(default=9, ge=0, le=23)
    proactive_retry_minutes: int = Field(default=30, ge=5, le=1440)

    @field_validator("drop_pending_updates", mode="before")
    @classmethod
    def _preserve_pending_updates(cls, _value: object) -> bool:
        return False

    @model_validator(mode="after")
    def _migrate_auto_delete_minutes(self) -> BotBehaviorConfig:
        if self.auto_delete_seconds <= 0 and self.auto_delete_minutes:
            self.auto_delete_seconds = int(self.auto_delete_minutes) * 60
        self.auto_delete_categories = list(dict.fromkeys(self.auto_delete_categories))
        # Zero means "inherit the global seconds": store only real overrides.
        cleaned: dict[str, int] = {}
        for category, seconds in self.auto_delete_category_seconds.items():
            value = int(seconds)
            if value < 0 or value > 604800:
                raise ValueError("分类自动删除时间必须在 0-604800 秒之间")
            if value > 0:
                cleaned[category] = value
        self.auto_delete_category_seconds = cleaned
        # "timer" is the default: store only "button" entries.
        self.auto_delete_category_mode = {
            category: mode
            for category, mode in self.auto_delete_category_mode.items()
            if mode == "button"
        }
        return self


class ModerationSettingsConfig(StrictModel):
    enabled: bool = True
    warn_threshold: int = Field(default=3, ge=1, le=100)
    high_confidence_threshold: float = Field(default=0.9, ge=0.0, le=1.0, allow_inf_nan=False)
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    bot_screening_enabled: bool = True
    bot_screening_message_count: int = Field(default=5, ge=1, le=100)
    # 群内露骨色情图片处置（删图 + @警告 + 质询）。
    # F-024 / 用户裁定（2026-10）：默认关闭（opt-in）；关掉后视觉提示词不再要求
    # NSFW 判定，也不会有任何处置动作。开启状态会在启动日志里逐条列出，避免
    # "升级后行为静默改变"。生产环境需显式打开。
    nsfw_image_guard_enabled: bool = False
    # 引用/转发广告：除转发者外，被引用消息的原作者同样处置。
    # F-024 / 用户裁定（2026-10）：默认关闭（opt-in）；默认开启等于升级后静默
    # 扩大执法范围。生产环境需显式打开。
    punish_quoted_author_enabled: bool = False
    # 被引用消息的追溯上限（秒）。默认 7 天；更老的消息只记日志不处置。
    quoted_author_max_age_seconds: int = Field(
        default=7 * 24 * 60 * 60, ge=0, le=365 * 24 * 60 * 60
    )
    # 管理员/群主不再豁免日常审核：照常判定，命中后只删消息 + 群内 @警示 +
    # 记违规，不质询/不封禁/不禁言/不累计。
    # F-024 / 用户裁定（2026-10）：默认关闭（opt-in，即回到"整段跳过"的旧行为）。
    # 生产环境需显式打开。
    admin_moderation_enabled: bool = False
    # 管理员命中违规时私聊最高管理员一份完整证据（best-effort、限流合并）。
    admin_alert_super_admin_enabled: bool = True
    # 审核命中证据投递频道（取代私聊最高管理员）：每条命中单独发一条证据卡，
    # 带「人工放行 / 放行收回」按钮。默认开启；关掉回到私聊老路径（含聚合）。
    log_channel_enabled: bool = True
    # 证据频道 id。**默认 0 = 未配置**：公开 fork 开箱即用时不会向任何频道投递
    # （含任何私人频道），命中证据按既有 fallback 路径回退私聊最高管理员。
    # 部署者用 Mini App / `PUT /api/v1/settings` 显式写入本字段后热生效。
    # ``config.toml`` 的 ``[moderation]`` 段只在 ``runtime_config`` 行还不存在时
    # 做一次性导入；环境变量对本段无效（``ModerationConfig`` 是普通 ``BaseModel``，
    # ``Settings`` 没有 ``env_nested_delimiter``，也没有扁平字段）。
    log_channel_id: int = 0
    # 审核规则交接时 @ 的对象（人工审核侧的对端 bot / 管理员）。**默认空 = 不
    # @任何人**，只发交接文案；空值时绝不构造 mention 实体（否则会退化成
    # ``rfind('')`` 这种"在开头造出一个假 mention"的 bug）。
    # 必须是合法 Telegram 用户名 ``@name``（5-32 位字母数字下划线、字母开头）；
    # 含换行 / HTML 的一律拒绝。
    review_handover_mention: str = ""
    # 「人工放行 / 确认封禁」双击确认窗口（秒）：第一次点击只 arm，第二次同键
    # 点击且间隔 <= 该窗口才真正执行。默认 300。
    review_confirm_seconds: int = Field(default=300, ge=1, le=86400)
    # F-021：每群每小时最多多少次审核模型调用。**0 = 不限**（默认，生产行为与
    # 今天逐字一致）。超限时只跑本地确定性规则并记为"未送审"（conclusive=False），
    # 与"模型调用失败"同口径，绝不静默放行。作用域：仅成员触发的送审。
    llm_call_cap_per_hour: int = Field(default=0, ge=0, le=1000000)

    @field_validator("log_channel_id", mode="before")
    @classmethod
    def _validate_log_channel_id(cls, value: object) -> int:
        try:
            channel_id = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("审核日志频道 id 必须是整数（0 = 未配置）") from exc
        if channel_id == 0:
            return 0
        if not (
            _TELEGRAM_CHANNEL_ID_MIN <= channel_id <= _TELEGRAM_CHANNEL_ID_MAX
        ):
            raise ValueError(
                "审核日志频道 id 必须是 0（未配置）或以 -100 开头的频道 id"
            )
        return channel_id

    @field_validator("review_handover_mention", mode="before")
    @classmethod
    def _validate_handover_mention(cls, value: object) -> str:
        mention = str(value or "").strip()
        if not mention:
            return ""
        if not re.fullmatch(_TELEGRAM_MENTION_PATTERN, mention):
            raise ValueError(
                "审核交接对象必须是合法的 Telegram 用户名（例如 @your_bot）"
            )
        return mention


class PatrolSettingsConfig(StrictModel):
    """Daily profile patrol over the known-member roster."""

    enabled: bool = False
    schedule_time: str = Field(default="04:30", max_length=5)
    batch_size: int = Field(default=500, ge=10, le=5000)
    batch_pause_seconds: float = Field(default=5.0, ge=0.0, le=600.0, allow_inf_nan=False)
    fetch_bio: bool = True
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    check_interval_seconds: float = Field(default=60.0, ge=15.0, le=3600.0, allow_inf_nan=False)

    @field_validator("schedule_time", mode="before")
    @classmethod
    def _validate_schedule_time(cls, value: object) -> str:
        text = str(value or "").strip()
        parts = text.split(":")
        if len(parts) == 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                hour = minute = -1
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        raise ValueError("巡检时间必须是 HH:MM 格式（例如 04:30）")


class RaidGuardSettingsConfig(StrictModel):
    """Join-flood lockdown with retroactive human challenges."""

    enabled: bool = False
    pin_message: bool = True
    join_threshold: int = Field(default=8, ge=2, le=1000)
    window_seconds: int = Field(default=60, ge=5, le=3600)
    lockdown_seconds: int = Field(default=600, ge=60, le=86400)
    lookback_seconds: int = Field(default=300, ge=0, le=86400)
    challenge_timeout_seconds: int = Field(default=600, ge=60, le=86400)


class CallAdminSettingsConfig(StrictModel):
    """@admin trigger: report a situation by pinging the group's admins."""

    enabled: bool = True
    pin_message: bool = False
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class VoteBanSettingsConfig(StrictModel):
    """Democratic vote-ban defaults; groups may override in the Mini App."""

    enabled: bool = False
    pin_message: bool = True
    vote_threshold: int = Field(default=5, ge=2, le=1000)
    duration_seconds: int = Field(default=1800, ge=60, le=86400)
    trigger_limit: int = Field(default=3, ge=1, le=1000)
    trigger_window_seconds: int = Field(default=3600, ge=60, le=604800)


class VerificationSettingsConfig(StrictModel):
    enabled: bool = False
    timeout_seconds: int = Field(default=600, ge=60, le=86400)
    check_interval_seconds: float = Field(default=30.0, ge=5.0, le=3600.0)
    provider: Literal["turnstile", "hcaptcha", "turnstile_hcaptcha"] = "turnstile"
    turnstile_site_key: str = Field(default="", max_length=255)
    turnstile_secret_key: str = Field(default="", max_length=1024)
    hcaptcha_site_key: str = Field(default="", max_length=255)
    hcaptcha_secret_key: str = Field(default="", max_length=1024)

    @field_validator(
        "turnstile_site_key",
        "turnstile_secret_key",
        "hcaptcha_site_key",
        "hcaptcha_secret_key",
        mode="before",
    )
    @classmethod
    def _clean_challenge_key(cls, value: object) -> str:
        return str(value or "").strip()


def turnstile_key_configuration_issue(
    site_key: str,
    secret_key: str,
) -> str:
    site = str(site_key or "").strip()
    secret = str(secret_key or "").strip()
    if site and secret and site == secret:
        return "Turnstile Secret Key 不能与 Site Key 相同；请填写 Cloudflare 控制台中的 Secret Key"
    return ""


def hcaptcha_key_configuration_issue(
    site_key: str,
    secret_key: str,
) -> str:
    site = str(site_key or "").strip()
    secret = str(secret_key or "").strip()
    if site and secret and site == secret:
        return "hCaptcha Secret Key 不能与 Site Key 相同；请填写 hCaptcha 控制台中的 Secret Key"
    return ""


class TTSSettingsConfig(StrictModel):
    enabled: bool = False
    # F-022: 显式 provider 选择（``doubao`` / ``edge``）。留空（默认）= 沿用隐式
    # 兼容口径：豆包凭据齐全走豆包，否则按 ``speaker`` 的形状判断是不是 Edge 音色。
    provider: Literal["", "doubao", "edge"] = ""
    http_timeout_sec: float = Field(default=20.0, ge=1.0, le=300.0)
    max_text_length: int = Field(default=500, ge=1, le=10000)
    api_base: str = Field(default="https://openspeech.bytedance.com", max_length=1000)
    app_id: str = Field(default="", max_length=255)
    app_key: str = Field(default="", max_length=1024)
    access_key: str = Field(default="", max_length=1024)
    resource_id: str = Field(default="seed-tts-2.0", max_length=255)
    model: str = Field(default="", max_length=255)
    speaker: str = Field(default="", max_length=255)
    audio_format: str = Field(default="ogg_opus", max_length=64)
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    bit_rate: int = Field(default=96000, ge=8000, le=512000)
    emotion: str = Field(default="", max_length=64)
    emotion_scale: int = Field(default=4, ge=1, le=5)
    speech_rate: int = Field(default=0, ge=-100, le=100)
    loudness_rate: int = Field(default=0, ge=-100, le=100)
    silence_duration_ms: int = Field(default=0, ge=0, le=10000)


class MusicSettingsConfig(StrictModel):
    enabled: bool = True
    http_timeout_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    base_url: str = Field(default="https://music-api.gdstudio.xyz/api.php", max_length=1000)
    default_source: str = Field(default="kuwo", max_length=64)
    stable_sources: list[str] = Field(default_factory=lambda: ["kuwo", "netease", "joox", "bilibili"])


class MovieInfoSettingsConfig(StrictModel):
    enabled: bool = False
    http_timeout_sec: float = Field(default=6.0, ge=1.0, le=6.0)
    max_results: int = Field(default=6, ge=1, le=20)
    default_language: str = Field(default="zh-CN", max_length=32)
    default_region: str = Field(default="CN", max_length=16)
    tmdb_read_access_token: str = Field(default="", max_length=2048)
    imdb_data_set_id: str = Field(default="", max_length=255)
    imdb_revision_id: str = Field(default="", max_length=255)
    imdb_asset_id: str = Field(default="", max_length=255)
    imdb_api_key: str = Field(default="", max_length=2048)
    imdb_aws_access_key_id: str = Field(default="", max_length=255)
    imdb_aws_secret_access_key: str = Field(default="", max_length=2048)
    imdb_aws_session_token: str = Field(default="", max_length=4096)

    @field_validator(
        "tmdb_read_access_token",
        "imdb_data_set_id",
        "imdb_revision_id",
        "imdb_asset_id",
        "imdb_api_key",
        "imdb_aws_access_key_id",
        "imdb_aws_secret_access_key",
        "imdb_aws_session_token",
        mode="before",
    )
    @classmethod
    def _strip_provider_value(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("default_language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2})?", text):
            raise ValueError("影片默认语言必须类似 zh-CN 或 en")
        language, *region = text.split("-", 1)
        return language.lower() + (f"-{region[0].upper()}" if region else "")

    @field_validator("default_region", mode="before")
    @classmethod
    def _normalize_region(cls, value: object) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z]{2}", text):
            raise ValueError("影片默认地区必须是两字母代码")
        return text.upper()


class AVSettingsConfig(StrictModel):
    enabled: bool = True
    http_timeout_sec: float = Field(default=15.0, ge=1.0, le=300.0)
    max_results: int = Field(default=18, ge=1, le=100)
    #: 私聊里封面之后补发的样例图张数（默认 4、硬上限 5、0=关闭）。
    dm_sample_count: int = Field(default=4, ge=0, le=5)
    #: 私聊详情里内联的下载地址条数（默认 3、硬上限 5、0=关闭该块）。
    inline_seed_count: int = Field(default=3, ge=0, le=5)
    #: 私聊详情里的 AI「题材与看点概述」；默认关（开启后每查一次多一次 LLM 调用）。
    ai_synopsis_enabled: bool = False
    javbus_base_url: str = Field(default="https://www.javbus.com", max_length=1000)
    madouqu_base_url: str = Field(default="https://madouqu.com", max_length=1000)
    dmm_base_url: str = Field(default="https://www.dmm.co.jp", max_length=1000)
    fc2_base_url: str = Field(default="https://adult.contents.fc2.com", max_length=1000)


class StickerSettingsConfig(StrictModel):
    fallback_file_ids: list[str] = Field(default_factory=list)


#: 审核命中证据的频道 id 合法区间：Telegram 频道/超级群 chat id 一律是
#: ``-100…`` 开头的 13 位负数。0 单独表示"未配置"。
_TELEGRAM_CHANNEL_ID_MIN = -1_009_999_999_999
_TELEGRAM_CHANNEL_ID_MAX = -1_000_000_000_000
#: Telegram 用户名的合法形状（5-32 位字母数字下划线，必须字母开头）。
_TELEGRAM_MENTION_PATTERN = r"^@[A-Za-z][A-Za-z0-9_]{3,31}$"


class PrivateChatSettingsConfig(StrictModel):
    """1 对 1 私聊的运营参数。

    唯一存储源 = ``runtime_config.private_chat``；读侧 = ``bot.config.
    PrivateChatPolicyConfig``（``apply_to_settings`` 写下去）→
    ``bot.services.policy_runtime.private_chat_policy()`` 快照 → 消费者。
    """

    per_user_daily_limit: int = Field(default=100, ge=1, le=100_000)
    admin_per_user_daily_limit: int = Field(default=500, ge=1, le=100_000)
    global_daily_limit: int = Field(default=20_000, ge=1, le=10_000_000)
    admin_global_daily_limit: int = Field(default=100_000, ge=1, le=10_000_000)
    input_max_chars: int = Field(
        default=1000, ge=1, le=4096, json_schema_extra=_HOT
    )
    reply_max_chars: int = Field(
        default=3800, ge=512, le=4096, json_schema_extra=_HOT
    )
    vision_budget_seconds: float = Field(
        default=30.0, ge=5.0, le=120.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    memory_turns: int = Field(default=12, ge=1, le=200, json_schema_extra=_HOT)
    #: 上界 60 = 今天真实生效的值。**只能调小**：TTL 调大等于让已被移出授权群
    #: 的人继续用私聊，是一个越权窗口，不能当运营参数放开。
    access_ttl_seconds: float = Field(
        default=60.0, ge=1.0, le=60.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    search_daily_limit: int = Field(default=2000, ge=1, le=100_000, json_schema_extra=_HOT)
    voice_max_segments: int = Field(default=6, ge=1, le=20, json_schema_extra=_HOT)

    @model_validator(mode="after")
    def _validate_tier_relations(self) -> "PrivateChatSettingsConfig":
        if self.global_daily_limit < self.per_user_daily_limit:
            raise ValueError("普通成员全局上限不能小于单人上限")
        if self.admin_global_daily_limit < self.admin_per_user_daily_limit:
            raise ValueError("群管理员全局上限不能小于单人上限")
        return self


class LotteryPrizeConfig(StrictModel):
    """奖池的一项：``payout`` 是中奖积分，``weight`` 是相对权重。"""

    payout: int = Field(default=0, ge=0, le=1_000_000)
    weight: int = Field(default=1, ge=1, le=1_000_000)
    label: str = Field(default="", max_length=32)


class EconomySettingsConfig(StrictModel):
    """签到 / 积分商店的运营参数。价格、时长、奖池都在这里，是财务口径的唯一起点。"""

    checkin_daily_point_cap: int = Field(default=10, ge=1, le=100)
    checkin_rank_limit: int = Field(default=10, ge=1, le=100)
    checkin_violation_window_days: int = Field(default=30, ge=1, le=365)
    #: 下界 1：0 = 免费绕过审核质询，不是运营旋钮。
    challenge_skip_cost: int = Field(default=2, ge=1, le=1000)

    tag_price_7d: int = Field(default=30, ge=0, le=100_000)
    tag_days_7d: int = Field(default=7, ge=1, le=3650)
    tag_price_30d: int = Field(default=80, ge=0, le=100_000)
    tag_days_30d: int = Field(default=30, ge=1, le=3650)
    pin_price: int = Field(default=20, ge=0, le=100_000)
    pin_hours: int = Field(default=6, ge=1, le=720)
    lottery_price: int = Field(default=5, ge=1, le=100_000)
    lottery_daily_limit: int = Field(default=10, ge=1, le=1000)
    lottery_prizes: list[LotteryPrizeConfig] = Field(
        default_factory=lambda: [
            LotteryPrizeConfig(payout=0, weight=3900, label="谢谢参与"),
            LotteryPrizeConfig(payout=3, weight=2000, label="3 分"),
            LotteryPrizeConfig(payout=5, weight=1600, label="5 分"),
            LotteryPrizeConfig(payout=8, weight=1000, label="8 分"),
            LotteryPrizeConfig(payout=12, weight=1000, label="12 分"),
            LotteryPrizeConfig(payout=40, weight=400, label="40 分"),
            LotteryPrizeConfig(payout=100, weight=100, label="100 分"),
        ],
        min_length=1,
        max_length=32,
    )
    #: Telegram 原生头衔长度上限（协议常量，不可放开）。
    tag_max_length: int = Field(default=16, ge=1, le=16)

    expiry_check_seconds: float = Field(
        default=300.0, ge=30.0, le=86_400.0, allow_inf_nan=False
    )
    expiry_pass_deadline_seconds: float = Field(
        default=120.0, ge=10.0, le=3600.0, allow_inf_nan=False
    )
    expiry_batch_limit: int = Field(default=200, ge=1, le=5000)
    expiry_retry_seconds: float = Field(
        default=900.0, ge=30.0, le=86_400.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _validate_lottery_table(self) -> "EconomySettingsConfig":
        if sum(prize.weight for prize in self.lottery_prizes) <= 0:
            raise ValueError("奖池总权重必须大于 0")
        if self.lottery_price <= 0:
            raise ValueError("抽奖价格必须大于 0")
        return self

    @property
    def lottery_total_weight(self) -> int:
        """总权重由奖池派生——不另存一份，避免两个来源互相矛盾。"""

        return sum(prize.weight for prize in self.lottery_prizes)


class ActivitySettingsConfig(StrictModel):
    """每周活跃激励的运营参数。"""

    min_message_text_length: int = Field(default=2, ge=1, le=100)
    max_daily_messages: int = Field(default=20, ge=1, le=500)
    min_active_days: int = Field(default=3, ge=1, le=365)
    min_weekly_messages: int = Field(default=10, ge=1, le=100_000)
    #: 每周奖励向量（下标 0 = 第 1 名）。榜单长度与周总额都从它派生。
    weekly_reward_points: list[int] = Field(
        default_factory=lambda: [25, 12, 12, 4, 4, 4, 4, 4, 4, 4],
        min_length=1,
        max_length=100,
    )

    @field_validator("weekly_reward_points", mode="after")
    @classmethod
    def _bound_reward_points(cls, value: list[int]) -> list[int]:
        for points in value:
            if int(points) < 0 or int(points) > 100_000:
                raise ValueError("每周奖励积分必须在 0-100000 之间")
        if sum(int(item) for item in value) <= 0:
            raise ValueError("每周奖励总额必须大于 0")
        return [int(item) for item in value]

    @property
    def weekly_top_n(self) -> int:
        return len(self.weekly_reward_points)

    @property
    def weekly_total_points(self) -> int:
        return sum(int(item) for item in self.weekly_reward_points)


class CheckinReminderSettingsConfig(StrictModel):
    """签到提醒的运营参数。"""

    slots: list[int] = Field(
        default_factory=lambda: [9, 12, 15, 18], min_length=1, max_length=12
    )
    slot_greetings: dict[int, str] = Field(
        default_factory=lambda: {9: "早上好", 12: "中午好", 15: "下午好", 18: "晚上好"}
    )
    auto_delete_seconds: int = Field(default=600, ge=0, le=86_400)
    roster_max_names: int = Field(default=20, ge=1, le=200)
    stale_grace_seconds: int = Field(default=900, ge=60, le=86_400)

    @field_validator("slots", mode="after")
    @classmethod
    def _normalize_slots(cls, value: list[int]) -> list[int]:
        slots = sorted({int(hour) for hour in value})
        for hour in slots:
            if hour < 0 or hour > 23:
                raise ValueError("提醒时段必须是 0-23 的整点小时")
        return slots

    @field_validator("slot_greetings", mode="before")
    @classmethod
    def _coerce_greeting_keys(cls, value: object) -> dict[int, str]:
        if not isinstance(value, dict):
            raise ValueError("时段问候语必须是 {小时: 文案} 的映射")
        result: dict[int, str] = {}
        for raw_hour, raw_text in value.items():
            try:
                hour = int(raw_hour)
            except (TypeError, ValueError) as exc:
                raise ValueError("时段问候语的键必须是整点小时") from exc
            text = str(raw_text or "").strip()
            if not text:
                raise ValueError("时段问候语不能为空")
            if len(text) > 32:
                raise ValueError("时段问候语最多 32 个字符")
            if "<" in text or ">" in text or "&" in text:
                raise ValueError("时段问候语不能包含 HTML 标签或实体")
            result[hour] = text
        return result

    @model_validator(mode="after")
    def _validate_greetings(self) -> "CheckinReminderSettingsConfig":
        unknown = sorted(set(self.slot_greetings) - set(self.slots))
        if unknown:
            raise ValueError(
                "时段问候语的键 " + "、".join(str(hour) for hour in unknown) + " 不在提醒时段里"
            )
        return self


class DisplaySettingsConfig(StrictModel):
    """用户可见的品牌与文案。协议标识（callback_data / start payload）刻意不在这里。"""

    bot_display_name: str = Field(default="助手", min_length=1, max_length=32)
    private_voice_title: str = Field(default="语音回复", min_length=1, max_length=64)
    checkin_button_text: str = Field(default="✅ 一键签到", min_length=1, max_length=64)
    shop_button_text: str = Field(default="🛒 积分商店", min_length=1, max_length=64)
    search_query_prefixes: list[str] = Field(
        default_factory=lambda: ["诶", "嗯哼", "呀", "欸"], max_length=32
    )
    private_not_member_notice: str = Field(default="", max_length=2000)
    private_limit_notice: str = Field(default="", max_length=2000)
    private_global_limit_notice: str = Field(default="", max_length=2000)
    private_media_unsupported_notice: str = Field(default="", max_length=2000)

    @field_validator(
        "bot_display_name",
        "private_voice_title",
        "checkin_button_text",
        "shop_button_text",
        mode="before",
    )
    @classmethod
    def _clean_label(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("search_query_prefixes", mode="before")
    @classmethod
    def _clean_prefixes(cls, value: object) -> list[str]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("检索唤醒前缀必须是字符串列表")
        prefixes: list[str] = []
        for raw in value:
            text = str(raw or "").strip()
            if not text:
                continue
            if len(text) > 16:
                raise ValueError("检索唤醒前缀最多 16 个字符")
            if any(ch in text for ch in "<>&\n\r"):
                raise ValueError("检索唤醒前缀不能包含换行或 HTML 标记")
            if text not in prefixes:
                prefixes.append(text)
        return prefixes

    @model_validator(mode="after")
    def _ensure_fallbacks(self) -> "DisplaySettingsConfig":
        # 提示文案在旧库/旧 payload 里可能是空串（新增字段还没写过），
        # 这里回落到中性默认，绝不出现"提示语变成空消息"。
        defaults = DisplayPolicyConfig()
        for name, fallback in (
            ("private_not_member_notice", defaults.private_not_member_notice),
            ("private_limit_notice", defaults.private_limit_notice),
            (
                "private_global_limit_notice",
                defaults.private_global_limit_notice,
            ),
            (
                "private_media_unsupported_notice",
                defaults.private_media_unsupported_notice,
            ),
        ):
            if not str(getattr(self, name) or "").strip():
                setattr(self, name, fallback)
        return self


class ResourceSettingsConfig(StrictModel):
    """高级资源与进程级预算。

    这里是"进程资源"而不是"运营参数"：默认全部等于今天真实生效的值，不配就
    逐字等于改造前。生效时机分两种，见 ``bot.services.startup_resources``：

    * ``json_schema_extra["reload_kind"] == "restart"``：模块级 Semaphore /
      gate / aiohttp Session 在 import 或建 Bot 时固化，**只在启动时装配一次**；
      改完必须重启，API 与 UI 会回 ``restart_fields``。
    * ``hot``：每次动作现取（超时、批量、阈值这类），改完下一次动作生效。
    """

    # --- 启动时装配（restart） -------------------------------------------------
    llm_request_capacity: int = Field(
        default=8, ge=2, le=64, json_schema_extra=_RESTART
    )
    llm_request_noncritical_capacity: int = Field(
        default=7, ge=1, le=64, json_schema_extra=_RESTART
    )
    llm_request_normal_capacity: int = Field(
        default=4, ge=1, le=64, json_schema_extra=_RESTART
    )
    llm_request_background_capacity: int = Field(
        default=2, ge=1, le=64, json_schema_extra=_RESTART
    )
    llm_tokenizer_concurrency: int = Field(
        default=2, ge=1, le=8, json_schema_extra=_RESTART
    )
    telegram_total_capacity: int = Field(
        default=64, ge=8, le=512, json_schema_extra=_RESTART
    )
    telegram_noncritical_capacity: int = Field(
        default=60, ge=4, le=512, json_schema_extra=_RESTART
    )
    telegram_normal_capacity: int = Field(
        default=44, ge=2, le=512, json_schema_extra=_RESTART
    )
    pending_reply_execution_capacity: int = Field(
        default=4, ge=1, le=32, json_schema_extra=_RESTART
    )
    tts_synthesis_concurrency: int = Field(
        default=3, ge=1, le=8, json_schema_extra=_RESTART
    )
    tts_transcode_concurrency: int = Field(
        default=2, ge=1, le=8, json_schema_extra=_RESTART
    )
    tts_private_concurrency: int = Field(
        default=2, ge=1, le=8, json_schema_extra=_RESTART
    )
    av_query_concurrency: int = Field(
        default=3, ge=1, le=8, json_schema_extra=_RESTART
    )

    # --- 热读（每次动作现取）--------------------------------------------------
    llm_stage_deadlines: dict[str, float] = Field(
        default_factory=lambda: {
            "decision": 35.0,
            "moderation": 35.0,
            "embed": 60.0,
            "compress": 90.0,
            "vision": 90.0,
            "main": 120.0,
            "skill": 120.0,
            "synopsis": 20.0,
            "group_summary": 15.0,
        }
    )
    telegram_critical_admission_timeout_seconds: float = Field(
        default=1.5, ge=0.1, le=60.0, allow_inf_nan=False
    )
    telegram_high_admission_timeout_seconds: float = Field(
        default=4.0, ge=0.1, le=60.0, allow_inf_nan=False
    )
    telegram_normal_admission_timeout_seconds: float = Field(
        default=15.0, ge=0.1, le=60.0, allow_inf_nan=False
    )
    telegram_privileged_timeout_seconds: float = Field(
        default=8.0, ge=0.1, le=60.0, allow_inf_nan=False
    )
    pending_reply_timeout_seconds: float = Field(
        default=45.0, ge=5.0, le=120.0, allow_inf_nan=False
    )
    admin_alert_window_seconds: float = Field(
        default=600.0, ge=10.0, le=86_400.0, allow_inf_nan=False
    )
    admin_alert_aggregate_after: int = Field(default=5, ge=2, le=1000)
    admin_alert_state_limit: int = Field(default=512, ge=16, le=65_536)
    admin_alert_text_limit: int = Field(default=900, ge=100, le=4000)
    moderation_throttle_burst: int = Field(default=3, ge=1, le=64)
    moderation_throttle_spacing_seconds: float = Field(
        default=4.0, ge=0.1, le=120.0, allow_inf_nan=False
    )
    moderation_throttle_max_wait_seconds: float = Field(
        default=6.0, ge=0.1, le=120.0, allow_inf_nan=False
    )
    moderation_throttle_max_waiters: int = Field(default=3, ge=1, le=256)
    tts_max_segments_per_message: int = Field(default=6, ge=1, le=20)
    tts_transcode_timeout_seconds: float = Field(
        default=30.0, ge=5.0, le=300.0, allow_inf_nan=False
    )
    tts_max_http_timeout_seconds: float = Field(
        default=60.0, ge=5.0, le=600.0, allow_inf_nan=False
    )
    av_query_deadline_seconds: float = Field(
        default=45.0, ge=5.0, le=300.0, allow_inf_nan=False
    )
    av_query_admission_timeout_seconds: float = Field(
        default=2.0, ge=0.1, le=60.0, allow_inf_nan=False
    )
    av_star_name_cache_max: int = Field(default=512, ge=64, le=8192)
    decision_history_token_budget: int = Field(default=8192, ge=512, le=1_000_000)
    decision_history_max_messages: int = Field(default=80, ge=1, le=1000)
    memory_max_facts_per_extraction: int = Field(default=6, ge=1, le=20)
    memory_extract_input_token_limit: int = Field(
        default=12_000, ge=512, le=200_000
    )
    memory_extract_scope_limit: int = Field(default=20, ge=1, le=200)
    memory_candidate_row_limit: int = Field(default=200, ge=10, le=5000)
    memory_private_group_fanout: int = Field(default=3, ge=1, le=20)
    memory_tool_subject_daily_cap: int = Field(default=5, ge=1, le=200)
    memory_maintenance_interval_seconds: int = Field(
        default=21_600, ge=300, le=86_400
    )
    search_prune_interval_seconds: int = Field(default=21_600, ge=300, le=86_400)
    search_record_recall_limit: int = Field(default=5, ge=1, le=50)
    archive_batch_size: int = Field(default=8, ge=1, le=512)
    archive_backfill_per_pass: int = Field(default=64, ge=1, le=5000)
    archive_scan_limit: int = Field(default=4096, ge=100, le=100_000)
    archive_candidate_limit: int = Field(default=64, ge=1, le=5000)
    archive_query_timeout_seconds: float = Field(
        default=8.0, ge=0.5, le=120.0, allow_inf_nan=False
    )
    archive_maintenance_interval_seconds: float = Field(
        default=5.0, ge=0.5, le=600.0, allow_inf_nan=False
    )
    archive_indexing_lease_seconds: float = Field(
        default=120.0, ge=5.0, le=3600.0, allow_inf_nan=False
    )

    # --- webhook / 轮询 / 出站发送 --------------------------------------------
    # 这些是 **webhook 服务的资源预算**。VerifyWebServer 与 aiohttp session 在启动
    # 时构造一次（worker 数、队列容量、连接池上限都固化在对象里），所以除少数
    # "每轮现取"的 durable-inbox 维护旋钮外一律 restart。默认逐字等于改造前。
    webhook_max_concurrent_updates: int = Field(
        default=8, ge=1, le=256, json_schema_extra=_RESTART
    )
    #: 三条独立车道：管理员交互 / 入群风控 / 鉴权。**互不挤占**——入群洪峰不能
    #: 把管理员的 /ban、/unban 堵在队列里。改一个必须仍然 ≤ 总容量。
    webhook_critical_concurrent_updates: int = Field(
        default=4, ge=1, le=256, json_schema_extra=_RESTART
    )
    webhook_security_concurrent_updates: int = Field(
        default=4, ge=1, le=256, json_schema_extra=_RESTART
    )
    webhook_auth_concurrent_updates: int = Field(
        default=2, ge=1, le=256, json_schema_extra=_RESTART
    )
    webhook_critical_queue_capacity: int = Field(
        default=64, ge=1, le=100_000, json_schema_extra=_RESTART
    )
    webhook_security_queue_capacity: int = Field(
        default=128, ge=1, le=100_000, json_schema_extra=_RESTART
    )
    webhook_auth_queue_capacity: int = Field(
        default=64, ge=1, le=100_000, json_schema_extra=_RESTART
    )
    #: 端到端预算。普通更新 150s 必须盖住"防抖 + 同步审核/索引 + 群回复 worker
    #: 的整段预算"（后者群级可配、上界 120s），否则请求会在 handler 跑完之前被掐断。
    webhook_update_timeout_seconds: float = Field(
        default=150.0, ge=5.0, le=1800.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    webhook_critical_update_timeout_seconds: float = Field(
        default=60.0, ge=1.0, le=1800.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    webhook_security_update_timeout_seconds: float = Field(
        default=120.0, ge=1.0, le=1800.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    webhook_auth_update_timeout_seconds: float = Field(
        default=45.0, ge=1.0, le=1800.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    #: HTTP 响应超时必须 ≥ 最大的端到端预算，否则响应先于 handler 结束就断。
    webhook_http_response_timeout_seconds: float = Field(
        default=155.0, ge=5.0, le=3600.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    #: durable inbox 的租约。**必须不小于最大的端到端预算**：租约先到期的话，
    #: 恢复循环会把一个仍在执行的 update 交给第二个 worker，造成重复处理。
    webhook_inbox_lease_seconds: float = Field(
        default=300.0, ge=5.0, le=3600.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    #: durable inbox 的维护旋钮：每轮现取，热生效。
    webhook_inbox_recovery_batch: int = Field(default=64, ge=1, le=5000)
    webhook_inbox_retry_max_seconds: float = Field(
        default=300.0, ge=1.0, le=3600.0, allow_inf_nan=False
    )
    webhook_inbox_cleanup_interval_seconds: float = Field(
        default=60.0, ge=5.0, le=3600.0, allow_inf_nan=False
    )
    webhook_inbox_cleanup_batch: int = Field(default=256, ge=1, le=10_000)
    #: 轮询（未启用 webhook 时的兜底传输）的三个超时。session 启动时建一次。
    polling_timeout_seconds: int = Field(
        default=15, ge=1, le=300, json_schema_extra=_HOT
    )
    polling_http_timeout_seconds: int = Field(
        default=30, ge=1, le=600, json_schema_extra=_HOT
    )
    polling_request_timeout_seconds: float = Field(
        default=35.0, ge=1.0, le=600.0, allow_inf_nan=False, json_schema_extra=_HOT
    )
    #: 同一会话的并发发送上限。**每次发送现建信号量**（不是模块级长寿命闸门），
    #: 所以热生效是安全的：没有 slot 会被漏掉或泄漏。
    telegram_send_chat_parallel: int = Field(default=3, ge=1, le=32, json_schema_extra=_HOT)

    @field_validator("llm_stage_deadlines", mode="before")
    @classmethod
    def _validate_stage_deadlines(cls, value: object) -> dict[str, float]:
        if not isinstance(value, dict) or not value:
            raise ValueError("LLM 阶段 deadline 必须是非空的 {阶段: 秒} 映射")
        result: dict[str, float] = {}
        for raw_stage, raw_seconds in value.items():
            stage = str(raw_stage or "").strip()
            if not stage or len(stage) > 32:
                raise ValueError("LLM 阶段名非法")
            try:
                seconds = float(raw_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"阶段 {stage} 的 deadline 必须是秒数") from exc
            if not 1.0 <= seconds <= 900.0:
                raise ValueError(f"阶段 {stage} 的 deadline 必须在 1-900 秒之间")
            result[stage] = seconds
        return result

    @model_validator(mode="after")
    def _validate_reserved_capacity(self) -> "ResourceSettingsConfig":
        """保留容量的不变式：关键名额 ≥1、背景 ≤ 普通 − 2、三级容量严格递减。"""

        total = self.llm_request_capacity
        noncritical = self.llm_request_noncritical_capacity
        normal = self.llm_request_normal_capacity
        background = self.llm_request_background_capacity
        if not (1 <= background <= normal - 2):
            raise ValueError("LLM 背景容量必须满足 1 ≤ background ≤ normal − 2")
        if not (1 <= normal <= noncritical):
            raise ValueError("LLM 普通容量必须满足 1 ≤ normal ≤ noncritical")
        if not (1 <= noncritical < total):
            raise ValueError("LLM 非关键容量必须满足 1 ≤ noncritical < total（至少留 1 个关键名额）")
        if not (2 <= self.telegram_normal_capacity <= self.telegram_noncritical_capacity):
            raise ValueError("Telegram 普通容量必须落在 2..noncritical 之间")
        if not (
            self.telegram_noncritical_capacity < self.telegram_total_capacity
        ):
            raise ValueError("Telegram 非关键容量必须小于总容量（至少留 1 个关键名额）")
        if self.archive_indexing_lease_seconds < self.archive_query_timeout_seconds:
            raise ValueError("归档索引租约不能短于查询 deadline，否则会重复写")
        self._validate_webhook_budget()
        return self

    def _validate_webhook_budget(self) -> None:
        """webhook 预算的关联约束。

        这些不是"风格偏好"，每一条都对应一个真实的故障模式：

        * **租约 ≥ 最大的端到端预算**：否则 durable inbox 的恢复循环会在一个
          update 还在执行时把租约判为过期，交给第二个 worker → 同一条 update
          被处理两次（管理员回调会重复执行动作）。
        * **租约 ≥ 最大重试退避**：否则一次合法重试会被恢复循环判成僵尸。
        * **HTTP 响应超时 ≥ 最大的端到端预算**：否则 handler 还在跑，HTTP 层已经
          断开并让 Telegram 重投。
        * **每条车道的并发 ≤ 总并发、队列容量 ≥ 车道并发**：否则要么配置自相矛盾
          （队列比 worker 还小，等于立刻丢更新），要么车道比总池还大，"独立车道"
          的隔离承诺不成立。
        """

        lanes = (
            ("critical", self.webhook_critical_concurrent_updates),
            ("security", self.webhook_security_concurrent_updates),
            ("auth", self.webhook_auth_concurrent_updates),
        )
        queues = (
            ("critical", self.webhook_critical_queue_capacity),
            ("security", self.webhook_security_queue_capacity),
            ("auth", self.webhook_auth_queue_capacity),
        )
        for name, workers in lanes:
            if workers > self.webhook_max_concurrent_updates:
                raise ValueError(
                    f"webhook {name} 车道的并发不能超过总并发 "
                    f"{self.webhook_max_concurrent_updates}"
                )
        for (name, workers), (_qname, capacity) in zip(lanes, queues):
            if capacity < workers:
                raise ValueError(
                    f"webhook {name} 车道的队列容量 {capacity} 不能小于它的并发 {workers}"
                )
        longest = max(
            self.webhook_update_timeout_seconds,
            self.webhook_critical_update_timeout_seconds,
            self.webhook_security_update_timeout_seconds,
            self.webhook_auth_update_timeout_seconds,
        )
        if self.webhook_inbox_lease_seconds < longest:
            raise ValueError(
                f"durable inbox 租约 {self.webhook_inbox_lease_seconds}s 不能短于最大的"
                f"端到端预算 {longest}s，否则仍在执行的 update 会被第二个 worker 抢走"
            )
        if self.webhook_inbox_lease_seconds < self.webhook_inbox_retry_max_seconds:
            raise ValueError(
                "durable inbox 租约不能短于最大重试退避，否则一次合法重试会被判成僵尸"
            )
        if self.webhook_http_response_timeout_seconds < longest:
            raise ValueError(
                "webhook HTTP 响应超时应不小于最大的端到端预算"
            )


class LoggingSettingsConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    third_party_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    color: Literal["on", "off", "auto"] = "on"
    to_file: bool = False
    file_path: str = Field(default="data/bot.log", max_length=1000)
    file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024 * 1024)
    file_backup_count: int = Field(default=3, ge=1, le=100)
    # A-16 / P4-1：入口日志里消息正文的预览字数。默认 100 = 改之前的硬编码行为
    # （`LoggingMiddleware` 里 `raw_text[:100]`），**不配 = 日志逐字不变**。
    # 调小只留更短的前缀；0 = 完全不记录正文，只记长度 + 内容哈希前缀。
    message_preview_chars: int = Field(default=100, ge=0, le=1000)


class PromptSettingsConfig(StrictModel):
    decision: str = ""
    moderation: str = ""
    casual: str = ""
    manage_intent: str = ""
    compress: str = ""
    skill_tools: str = ""
    sticker_decision: str = ""
    reply_mode: str = ""
    persona: str = ""
    proactive_topic: str = ""
    style_distill: str = ""
    av_synopsis: str = ""

    @classmethod
    def defaults(cls) -> PromptSettingsConfig:
        return cls(**load_prompt_defaults())

    @model_validator(mode="after")
    def _validate_moderation_template(self) -> PromptSettingsConfig:
        template = self.moderation or load_prompt_defaults()["moderation"]
        if "{rules_json}" not in template:
            raise ValueError("审核 Prompt 必须包含 {rules_json} 占位符")
        try:
            template.format(rules_json="[]")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "审核 Prompt 的花括号或占位符无效；JSON 示例需使用双花括号"
            ) from exc
        return self


class RuntimeConfig(StrictModel):
    schema_version: int = Field(default=CONFIG_SCHEMA_VERSION, ge=1)
    models: ModelSettingsConfig = Field(default_factory=ModelSettingsConfig)
    bot: BotBehaviorConfig = Field(default_factory=BotBehaviorConfig)
    moderation: ModerationSettingsConfig = Field(default_factory=ModerationSettingsConfig)
    patrol: PatrolSettingsConfig = Field(default_factory=PatrolSettingsConfig)
    raid_guard: RaidGuardSettingsConfig = Field(default_factory=RaidGuardSettingsConfig)
    call_admin: CallAdminSettingsConfig = Field(default_factory=CallAdminSettingsConfig)
    vote_ban: VoteBanSettingsConfig = Field(default_factory=VoteBanSettingsConfig)
    verification: VerificationSettingsConfig = Field(default_factory=VerificationSettingsConfig)
    tts: TTSSettingsConfig = Field(default_factory=TTSSettingsConfig)
    music: MusicSettingsConfig = Field(default_factory=MusicSettingsConfig)
    movie_info: MovieInfoSettingsConfig = Field(default_factory=MovieInfoSettingsConfig)
    av: AVSettingsConfig = Field(default_factory=AVSettingsConfig)
    stickers: StickerSettingsConfig = Field(default_factory=StickerSettingsConfig)
    logging: LoggingSettingsConfig = Field(default_factory=LoggingSettingsConfig)
    prompts: PromptSettingsConfig = Field(default_factory=PromptSettingsConfig.defaults)
    private_chat: PrivateChatSettingsConfig = Field(
        default_factory=PrivateChatSettingsConfig
    )
    economy: EconomySettingsConfig = Field(default_factory=EconomySettingsConfig)
    activity: ActivitySettingsConfig = Field(default_factory=ActivitySettingsConfig)
    checkin_reminder: CheckinReminderSettingsConfig = Field(
        default_factory=CheckinReminderSettingsConfig
    )
    display: DisplaySettingsConfig = Field(default_factory=DisplaySettingsConfig)
    resources: ResourceSettingsConfig = Field(default_factory=ResourceSettingsConfig)

    @model_validator(mode="after")
    def _validate_model_references(self) -> RuntimeConfig:
        names = [provider.name for provider in self.models.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        known = set(names)
        if not known:
            raise ValueError("at least one model provider is required")
        if not self.models.main.provider or self.models.main.provider not in known:
            raise ValueError("main model provider must reference an existing provider")
        if not self.models.main.model:
            raise ValueError("main model name is required")
        for role_name in ("vision", "decision", "moderation", "compress"):
            role = getattr(self.models, role_name)
            if role.provider and role.provider not in known:
                raise ValueError(f"{role_name} model provider does not exist: {role.provider}")
            for fallback in role.fallbacks:
                if fallback.provider not in known:
                    raise ValueError(f"{role_name} fallback provider does not exist: {fallback.provider}")
        for fallback in self.models.main.fallbacks:
            if fallback.provider not in known:
                raise ValueError(f"main fallback provider does not exist: {fallback.provider}")
        if self.models.embed.provider and self.models.embed.provider not in known:
            raise ValueError(f"embed model provider does not exist: {self.models.embed.provider}")
        for fallback in self.models.embed.fallbacks:
            if fallback.provider not in known:
                raise ValueError(f"embed fallback provider does not exist: {fallback.provider}")
        return self

    def restart_pending_paths(self, applied: dict[str, Any] | None = None) -> list[str]:
        """当前保存值与**进程实际在跑的值**不一致的 restart 字段（只给字段名）。

        基准是"本进程启动时装配进去的那一份"，**不是上一次保存**：

        * 先把 LLM 容量 8 改成 16 → 提示 ``resources.llm_request_capacity``；
        * 再改一个热字段 → 提示**不会**被清掉（进程里仍然是 8）；
        * 改回 8（= 实际运行值）→ 该字段的提示自动消失。

        只回字段名，不回任何值（值里可能混着 secret）。
        """

        baseline = applied if applied is not None else applied_restart_values()
        desired = _extract_restart_values(self)
        return sorted(
            path
            for path in RESTART_REQUIRED_PATHS
            if desired.get(path) != baseline.get(path)
        )

    def secret_paths(self) -> set[str]:
        paths = set(_STATIC_SECRET_PATHS)
        paths.update(f"providers.{provider.name}.api_key" for provider in self.models.providers)
        return paths

    def extract_secrets(self) -> dict[str, str]:
        values = {
            "verification.turnstile_secret_key": self.verification.turnstile_secret_key,
            "verification.hcaptcha_secret_key": self.verification.hcaptcha_secret_key,
            "tts.app_key": self.tts.app_key,
            "tts.access_key": self.tts.access_key,
            "movie_info.tmdb_read_access_token": (
                self.movie_info.tmdb_read_access_token
            ),
            "movie_info.imdb_api_key": self.movie_info.imdb_api_key,
            "movie_info.imdb_aws_access_key_id": (
                self.movie_info.imdb_aws_access_key_id
            ),
            "movie_info.imdb_aws_secret_access_key": (
                self.movie_info.imdb_aws_secret_access_key
            ),
            "movie_info.imdb_aws_session_token": (
                self.movie_info.imdb_aws_session_token
            ),
        }
        values.update(
            {
                f"providers.{provider.name}.api_key": provider.api_key
                for provider in self.models.providers
            }
        )
        return {path: value for path, value in values.items() if value}

    def with_secrets(self, secrets: dict[str, str]) -> RuntimeConfig:
        clone = self.model_copy(deep=True)
        clone.verification.turnstile_secret_key = secrets.get(
            "verification.turnstile_secret_key", ""
        )
        clone.verification.hcaptcha_secret_key = secrets.get(
            "verification.hcaptcha_secret_key", ""
        )
        clone.tts.app_key = secrets.get("tts.app_key", "")
        clone.tts.access_key = secrets.get("tts.access_key", "")
        clone.movie_info.tmdb_read_access_token = secrets.get(
            "movie_info.tmdb_read_access_token", ""
        ).strip()
        clone.movie_info.imdb_api_key = secrets.get(
            "movie_info.imdb_api_key", ""
        ).strip()
        clone.movie_info.imdb_aws_access_key_id = secrets.get(
            "movie_info.imdb_aws_access_key_id", ""
        ).strip()
        clone.movie_info.imdb_aws_secret_access_key = secrets.get(
            "movie_info.imdb_aws_secret_access_key", ""
        ).strip()
        clone.movie_info.imdb_aws_session_token = secrets.get(
            "movie_info.imdb_aws_session_token", ""
        ).strip()
        for provider in clone.models.providers:
            provider.api_key = secrets.get(f"providers.{provider.name}.api_key", "")
        return clone

    def storage_payload(self) -> dict[str, Any]:
        clone = self.with_secrets({})
        return clone.model_dump(mode="json")

    def public_payload(self) -> dict[str, Any]:
        return self.storage_payload()

    @staticmethod
    def _fallback_spec(items: list[ModelFallbackConfig]) -> str:
        return ",".join(
            f"{item.provider}:{item.model}" if item.model else item.provider
            for item in items
        )

    @staticmethod
    def _fallback_request_params(items: list[ModelFallbackConfig]) -> list[dict[str, Any]]:
        return [dict(item.request_params) for item in items]

    def _provider_profiles(self) -> dict[str, ProviderProfile]:
        profiles: dict[str, ProviderProfile] = {}
        for item in self.models.providers:
            provider, api_base, endpoint, endpoint_path = _resolve_provider_profile(
                item.provider,
                item.api_base,
                None if item.chat_endpoint == "auto" else item.chat_endpoint,
            )
            profiles[item.name] = ProviderProfile(
                provider=provider,
                api_key=item.api_key or None,
                api_base=api_base,
                stream=item.stream,
                chat_endpoint=endpoint,
                endpoint_path=endpoint_path,
            )
        return profiles

    def apply_to_settings(
        self,
        settings: Settings,
        *,
        apply_prompts: bool = True,
    ) -> None:
        verification = self.verification
        challenge_keys: list[tuple[str, str]] = []
        if verification.provider in {"turnstile", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    ("Turnstile Site Key", verification.turnstile_site_key),
                    ("Turnstile Secret Key", verification.turnstile_secret_key),
                )
            )
        if verification.provider in {"hcaptcha", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    ("hCaptcha Site Key", verification.hcaptcha_site_key),
                    ("hCaptcha Secret Key", verification.hcaptcha_secret_key),
                )
            )
        if verification.enabled:
            missing = [
                label
                for label, value in (
                    *challenge_keys,
                    ("MINIAPP_PUBLIC_BASE_URL", settings.miniapp_public_base_url),
                )
                if not str(value or "").strip()
            ]
            if missing:
                raise ValueError("启用入群验证前需配置：" + "、".join(missing))
        profiles = self._provider_profiles()
        models = self.models
        main = models.main

        def effective_chat(role: ChatRoleConfig, *, parent: ChatRoleConfig) -> tuple[str, str]:
            return role.provider or parent.provider, role.model or parent.model

        vision_provider, vision_model = effective_chat(models.vision, parent=main)
        decision_provider, decision_model = effective_chat(models.decision, parent=main)
        moderation_parent = models.decision.model_copy(deep=True)
        moderation_parent.provider = decision_provider
        moderation_parent.model = decision_model
        moderation_provider, moderation_model = effective_chat(
            models.moderation,
            parent=moderation_parent,
        )
        compress_provider, compress_model = effective_chat(models.compress, parent=main)
        skill_role = models.skill or main
        skill_provider, skill_model = effective_chat(skill_role, parent=main)
        embed_provider = models.embed.provider or main.provider

        common_retry = {
            "retry_attempts": models.retry_attempts,
            "retry_backoff_sec": models.retry_backoff_sec,
            "retry_timeout_multiplier": models.retry_timeout_multiplier,
        }
        settings.bot.main_model = _build_chat_config(
            profiles=profiles,
            provider_name=main.provider,
            model_name=main.model,
            temperature=main.temperature,
            max_tokens=main.max_tokens,
            timeout_sec=main.timeout_sec,
            total_deadline_sec=main.total_deadline_sec,
            fallback_spec=self._fallback_spec(main.fallbacks),
            request_params=main.request_params,
            fallback_request_params=self._fallback_request_params(main.fallbacks),
            **common_retry,
        )
        settings.bot.vision_model = _build_chat_config(
            profiles=profiles,
            provider_name=vision_provider,
            model_name=vision_model,
            temperature=models.vision.temperature,
            max_tokens=models.vision.max_tokens,
            timeout_sec=models.vision.timeout_sec,
            total_deadline_sec=models.vision.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.vision.fallbacks),
            request_params=models.vision.request_params,
            fallback_request_params=self._fallback_request_params(models.vision.fallbacks),
            **common_retry,
        )
        settings.bot.decision_model = _build_chat_config(
            profiles=profiles,
            provider_name=decision_provider,
            model_name=decision_model,
            temperature=models.decision.temperature,
            max_tokens=models.decision.max_tokens,
            timeout_sec=models.decision.timeout_sec,
            total_deadline_sec=models.decision.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.decision.fallbacks),
            request_params=models.decision.request_params,
            fallback_request_params=self._fallback_request_params(models.decision.fallbacks),
            **common_retry,
        )
        settings.bot.moderation_model = _build_chat_config(
            profiles=profiles,
            provider_name=moderation_provider,
            model_name=moderation_model,
            temperature=models.moderation.temperature,
            max_tokens=models.moderation.max_tokens,
            timeout_sec=models.moderation.timeout_sec,
            total_deadline_sec=models.moderation.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.moderation.fallbacks),
            request_params=models.moderation.request_params,
            fallback_request_params=self._fallback_request_params(models.moderation.fallbacks),
            **common_retry,
        )
        settings.bot.compress_model = _build_chat_config(
            profiles=profiles,
            provider_name=compress_provider,
            model_name=compress_model,
            temperature=models.compress.temperature,
            max_tokens=models.compress.max_tokens,
            timeout_sec=models.compress.timeout_sec,
            total_deadline_sec=models.compress.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.compress.fallbacks),
            request_params=models.compress.request_params,
            fallback_request_params=self._fallback_request_params(models.compress.fallbacks),
            **common_retry,
        )
        settings.bot.skill_model = _build_chat_config(
            profiles=profiles,
            provider_name=skill_provider,
            model_name=skill_model,
            temperature=skill_role.temperature,
            max_tokens=skill_role.max_tokens,
            timeout_sec=skill_role.timeout_sec,
            total_deadline_sec=skill_role.total_deadline_sec,
            fallback_spec=self._fallback_spec(skill_role.fallbacks),
            request_params=skill_role.request_params,
            fallback_request_params=self._fallback_request_params(skill_role.fallbacks),
            **common_retry,
        )
        settings.bot.embed_model = _build_embed_config(
            profiles=profiles,
            provider_name=embed_provider,
            model_name=models.embed.model,
            timeout_sec=models.embed.timeout_sec,
            total_deadline_sec=models.embed.total_deadline_sec,
            fallback_spec=self._fallback_spec(models.embed.fallbacks),
            request_params=models.embed.request_params,
            fallback_request_params=self._fallback_request_params(models.embed.fallbacks),
            **common_retry,
        )

        bot = self.bot
        settings.bot.parse_mode = bot.parse_mode
        settings.bot.disable_link_preview = bot.disable_link_preview
        settings.bot.drop_pending_updates = bot.drop_pending_updates
        settings.bot.inbound_debounce_seconds = bot.inbound_debounce_seconds
        settings.bot.reply_batch_timeout_seconds = bot.reply_batch_timeout_seconds
        settings.bot.enable_typing = bot.enable_typing
        settings.bot.enable_rich_messages = bot.enable_rich_messages
        settings.bot.enable_streaming = bot.enable_streaming
        settings.bot.stream_chunk_size = bot.stream_chunk_size
        settings.bot.stream_edit_interval_sec = bot.stream_edit_interval_sec
        settings.bot.auto_delete_seconds = bot.auto_delete_seconds
        settings.bot.auto_delete_minutes = bot.auto_delete_seconds // 60
        settings.bot.auto_delete_categories = list(bot.auto_delete_categories)
        settings.bot.auto_delete_category_seconds = dict(
            bot.auto_delete_category_seconds
        )
        settings.bot.auto_delete_category_mode = dict(
            bot.auto_delete_category_mode
        )
        settings.bot.decision_context_items = bot.decision_context_items
        settings.bot.context_window_mode = bot.context_window_mode
        settings.bot.max_context_tokens = bot.max_context_tokens
        settings.bot.context_budget_tokens = bot.context_budget_tokens
        settings.bot.context_reserve_tokens = bot.context_reserve_tokens
        settings.bot.group_history_max_messages = bot.group_history_max_messages
        for _name in _GROUP_SUMMARY_FIELD_NAMES:
            setattr(settings.bot, _name, getattr(bot, _name))
        settings.bot.max_output_tokens = bot.max_output_tokens
        settings.bot.memory_recent_messages = bot.memory_recent_messages
        settings.bot.memory_retention_days = bot.memory_retention_days
        settings.bot.memory_archive_max_messages_per_group = (
            bot.memory_archive_max_messages_per_group
        )
        settings.bot.memory_recall_enabled = bot.memory_recall_enabled
        settings.bot.memory_recall_max_results = bot.memory_recall_max_results
        settings.bot.memory_automatic_compaction = (
            bot.memory_automatic_compaction
        )
        settings.bot.private_chat_history_token_budget = (
            bot.private_chat_history_token_budget
        )
        settings.bot.private_chat_history_retention_days = (
            bot.private_chat_history_retention_days
        )
        settings.bot.group_history_token_budget = bot.group_history_token_budget
        settings.bot.group_history_reserve_tokens = bot.group_history_reserve_tokens
        settings.bot.search_record_retention_days = bot.search_record_retention_days
        settings.bot.search_freshness_price_hours = bot.search_freshness_price_hours
        settings.bot.search_freshness_news_hours = bot.search_freshness_news_hours
        settings.bot.search_freshness_fact_hours = bot.search_freshness_fact_hours
        # 第 4 期：长期记忆（user_facts）
        settings.bot.memory_facts_enabled = bot.memory_facts_enabled
        settings.bot.memory_extract_enabled = bot.memory_extract_enabled
        settings.bot.memory_extract_interval_minutes = (
            bot.memory_extract_interval_minutes
        )
        settings.bot.memory_extract_min_messages = bot.memory_extract_min_messages
        settings.bot.memory_extract_daily_cap = bot.memory_extract_daily_cap
        settings.bot.memory_extract_batch_max = bot.memory_extract_batch_max
        settings.bot.memory_tool_enabled = bot.memory_tool_enabled
        settings.bot.memory_tool_daily_cap = bot.memory_tool_daily_cap
        settings.bot.memory_recall_limit = bot.memory_recall_limit
        settings.bot.memory_event_ttl_days = bot.memory_event_ttl_days
        settings.bot.memory_deleted_retention_days = bot.memory_deleted_retention_days
        # MEM-001：读侧 ``memory_extract_ledger_retention_days()`` 一直在读这个字段，
        # 但这里之前从没写过，所以它在运行期恒为默认值。
        settings.bot.memory_extract_ledger_retention_days = (
            bot.memory_extract_ledger_retention_days
        )
        settings.bot.proactive_default_enabled = bot.proactive_default_enabled
        settings.bot.proactive_idle_minutes = bot.proactive_idle_minutes
        settings.bot.proactive_jitter_minutes = bot.proactive_jitter_minutes
        settings.bot.proactive_check_interval_seconds = bot.proactive_check_interval_seconds
        settings.bot.proactive_quiet_hours_start = bot.proactive_quiet_hours_start
        settings.bot.proactive_quiet_hours_end = bot.proactive_quiet_hours_end
        settings.bot.proactive_retry_minutes = bot.proactive_retry_minutes
        settings.max_context_tokens = bot.max_context_tokens
        settings.context_window_mode = bot.context_window_mode
        settings.context_budget_tokens = bot.context_budget_tokens
        settings.context_reserve_tokens = bot.context_reserve_tokens
        settings.group_history_max_messages = bot.group_history_max_messages
        for _name in _GROUP_SUMMARY_FIELD_NAMES:
            setattr(settings, _name, getattr(bot, _name))
        settings.max_output_tokens = bot.max_output_tokens

        settings.moderation = ModerationConfig(**self.moderation.model_dump())
        settings.patrol_enabled = self.patrol.enabled
        settings.patrol_schedule_time = self.patrol.schedule_time
        settings.patrol_batch_size = self.patrol.batch_size
        settings.patrol_batch_pause_seconds = self.patrol.batch_pause_seconds
        settings.patrol_fetch_bio = self.patrol.fetch_bio
        settings.patrol_challenge_timeout_seconds = self.patrol.challenge_timeout_seconds
        settings.patrol_check_interval_seconds = self.patrol.check_interval_seconds
        settings.raid_guard_enabled = self.raid_guard.enabled
        settings.raid_guard_pin_message = self.raid_guard.pin_message
        settings.raid_guard_join_threshold = self.raid_guard.join_threshold
        settings.raid_guard_window_seconds = self.raid_guard.window_seconds
        settings.raid_guard_lockdown_seconds = self.raid_guard.lockdown_seconds
        settings.raid_guard_lookback_seconds = self.raid_guard.lookback_seconds
        settings.raid_guard_challenge_timeout_seconds = (
            self.raid_guard.challenge_timeout_seconds
        )
        settings.call_admin_enabled = self.call_admin.enabled
        settings.call_admin_pin_message = self.call_admin.pin_message
        settings.call_admin_cooldown_seconds = self.call_admin.cooldown_seconds
        settings.vote_ban_enabled = self.vote_ban.enabled
        settings.vote_ban_pin_message = self.vote_ban.pin_message
        settings.vote_ban_threshold = self.vote_ban.vote_threshold
        settings.vote_ban_duration_seconds = self.vote_ban.duration_seconds
        settings.vote_ban_trigger_limit = self.vote_ban.trigger_limit
        settings.vote_ban_trigger_window_seconds = self.vote_ban.trigger_window_seconds
        settings.join_verification_enabled = self.verification.enabled
        settings.join_verification_timeout_seconds = self.verification.timeout_seconds
        settings.join_verification_check_interval_seconds = self.verification.check_interval_seconds
        settings.join_verification_provider = self.verification.provider
        settings.join_verification_turnstile_site_key = self.verification.turnstile_site_key
        settings.join_verification_turnstile_secret_key = self.verification.turnstile_secret_key
        settings.join_verification_hcaptcha_site_key = self.verification.hcaptcha_site_key
        settings.join_verification_hcaptcha_secret_key = self.verification.hcaptcha_secret_key

        tts = self.tts
        settings.doubao_tts_enabled = tts.enabled
        settings.doubao_tts_provider = tts.provider
        settings.doubao_tts_http_timeout_sec = tts.http_timeout_sec
        settings.doubao_tts_max_text_length = tts.max_text_length
        settings.doubao_tts_api_base = tts.api_base
        settings.doubao_tts_app_id = tts.app_id
        settings.doubao_tts_app_key = tts.app_key
        settings.doubao_tts_access_key = tts.access_key
        settings.doubao_tts_resource_id = tts.resource_id
        settings.doubao_tts_model = tts.model
        settings.doubao_tts_speaker = tts.speaker
        settings.doubao_tts_audio_format = tts.audio_format
        settings.doubao_tts_sample_rate = tts.sample_rate
        settings.doubao_tts_bit_rate = tts.bit_rate
        settings.doubao_tts_emotion = tts.emotion
        settings.doubao_tts_emotion_scale = tts.emotion_scale
        settings.doubao_tts_speech_rate = tts.speech_rate
        settings.doubao_tts_loudness_rate = tts.loudness_rate
        settings.doubao_tts_silence_duration_ms = tts.silence_duration_ms

        settings.music_api_enabled = self.music.enabled
        settings.music_api_http_timeout_sec = self.music.http_timeout_sec
        settings.music_api_base_url = self.music.base_url
        settings.music_api_default_source = self.music.default_source
        settings.music_api_stable_sources = ",".join(self.music.stable_sources)
        movie_info = self.movie_info
        if movie_info.enabled:
            tmdb_ready = bool(movie_info.tmdb_read_access_token)
            imdb_ready = all(
                (
                    movie_info.imdb_data_set_id,
                    movie_info.imdb_revision_id,
                    movie_info.imdb_asset_id,
                    movie_info.imdb_api_key,
                    movie_info.imdb_aws_access_key_id,
                    movie_info.imdb_aws_secret_access_key,
                )
            )
            if not (tmdb_ready or imdb_ready):
                raise ValueError(
                    "启用影片信息查询前，需配置 TMDB Read Access Token，"
                    "或完整的 IMDb AWS Data Exchange 凭据"
                )
        settings.movie_info_enabled = movie_info.enabled
        settings.movie_info_http_timeout_sec = movie_info.http_timeout_sec
        settings.movie_info_max_results = movie_info.max_results
        settings.movie_info_default_language = movie_info.default_language
        settings.movie_info_default_region = movie_info.default_region
        settings.movie_info_tmdb_read_access_token = (
            movie_info.tmdb_read_access_token
        )
        settings.movie_info_imdb_data_set_id = movie_info.imdb_data_set_id
        settings.movie_info_imdb_revision_id = movie_info.imdb_revision_id
        settings.movie_info_imdb_asset_id = movie_info.imdb_asset_id
        settings.movie_info_imdb_api_key = movie_info.imdb_api_key
        settings.movie_info_imdb_aws_access_key_id = (
            movie_info.imdb_aws_access_key_id
        )
        settings.movie_info_imdb_aws_secret_access_key = (
            movie_info.imdb_aws_secret_access_key
        )
        settings.movie_info_imdb_aws_session_token = (
            movie_info.imdb_aws_session_token
        )
        settings.av_enabled = self.av.enabled
        settings.av_http_timeout_sec = self.av.http_timeout_sec
        settings.av_max_results = self.av.max_results
        settings.av_dm_sample_count = self.av.dm_sample_count
        settings.av_inline_seed_count = self.av.inline_seed_count
        settings.av_ai_synopsis_enabled = self.av.ai_synopsis_enabled
        settings.av_javbus_base_url = self.av.javbus_base_url
        settings.av_madouqu_base_url = self.av.madouqu_base_url
        settings.av_dmm_base_url = self.av.dmm_base_url
        settings.av_fc2_base_url = self.av.fc2_base_url
        settings.skill_sticker_file_ids = ",".join(self.stickers.fallback_file_ids)
        settings.private_chat = PrivateChatPolicyConfig(
            **self.private_chat.model_dump()
        )
        settings.economy = EconomyPolicyConfig(
            **_economy_policy_payload(self.economy)
        )
        settings.activity = ActivityPolicyConfig(**self.activity.model_dump())
        settings.checkin_reminder = CheckinReminderPolicyConfig(
            **self.checkin_reminder.model_dump()
        )
        settings.display = DisplayPolicyConfig(**self.display.model_dump())
        settings.resources = ResourcesPolicyConfig(**self.resources.model_dump())
        if apply_prompts:
            set_runtime_prompts(self.prompts.model_dump())


class RuntimeConfigConflictError(RuntimeError):
    pass


class RuntimeConfigEncryptionError(RuntimeError):
    pass


class SecretCipher:
    def __init__(self, master_key: str) -> None:
        raw = str(master_key or "").strip()
        self._fernet: Fernet | None = None
        if raw:
            derived = hashlib.sha256(raw.encode("utf-8")).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if not self._fernet:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is required before saving secret settings"
            )
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not self._fernet:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is required to decrypt stored settings"
            )
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise RuntimeConfigEncryptionError(
                "stored settings cannot be decrypted with CONFIG_MASTER_KEY"
            ) from exc


ConfigAppliedCallback = Callable[[RuntimeConfig], Awaitable[None] | None]


def _payload_schema_version(payload: Any) -> int:
    """读持久化文档的 schema 版本；缺失或非法一律当成最老的 1。"""

    if not isinstance(payload, dict):
        return 1
    try:
        value = int(payload.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _normalize_deprecated_runtime_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Normalize retired settings before strict schema validation.

    ``reasoning_effort`` used to be a first-class role field.  The first
    request-JSON revision then added role defaults and a global model override
    map.  Both older shapes are expanded into concrete primary/fallback model
    entries before the strict model rejects the retired fields.
    """

    normalized = dict(payload)
    changed = False

    # F-014：运行时提示词是数据库持久化的，光靠改 prompt/*.md 无法退役旧库里的
    # 老规则。这里做**一次性**迁移：只重写"命中退役标记"的那几个提示词，其余
    # 自定义提示词一个字都不动。幂等靠 schema_version：迁移完就把 payload 的版本
    # 抬到当前值，之后（包括管理员自己改过的内容）绝不会被反复覆盖——这是上一版
    # 迁移被回滚的原因，不能再犯。
    if _payload_schema_version(payload) < CONFIG_SCHEMA_VERSION:
        prompts_payload = normalized.get("prompts")
        if isinstance(prompts_payload, dict):
            migratable = dict(prompts_payload)
            defaults = load_prompt_defaults()
            prompts_changed = False
            for name, markers in _RETIRED_OWNER_PRIORITY_PROMPT_MARKERS.items():
                value = str(migratable.get(name) or "")
                if value and any(marker in value for marker in markers):
                    migratable[name] = defaults[name]
                    prompts_changed = True
                    log.warning(
                        "Retired owner-priority wording removed from runtime prompt | prompt=%s",
                        name,
                    )
            if prompts_changed:
                normalized["prompts"] = migratable
                changed = True
        # 版本号只在真的一次性迁移点上抬升；没命中退役标记的老库同样抬升，
        # 这样每个库只会走一次这段逻辑，不会在每次读取时重复判断。
        normalized["schema_version"] = CONFIG_SCHEMA_VERSION
        changed = True

    bot_payload = payload.get("bot")
    if not (
        isinstance(bot_payload, dict)
        and "drop_pending_updates" in bot_payload
        and bot_payload["drop_pending_updates"] is False
    ):
        normalized_bot = dict(bot_payload) if isinstance(bot_payload, dict) else {}
        normalized_bot["drop_pending_updates"] = False
        normalized["bot"] = normalized_bot
        changed = True

    # 2026-10-04 事故修复：上下文上限从"固定 272K 硬上限"改成"自动发现模型窗口 +
    # 每轮 272Ki 业务预算"（发现值只用于比业务预算更紧，不放松 272Ki）。
    # 老库里没有 ``context_window_mode``，这里**一次性**补成 ``auto``（真正迁移既有
    # 部署的语义），并且只在缺字段时写：管理员之后显式改成 ``fixed`` 绝不会被覆盖。
    migration_bot = dict(normalized.get("bot") or {})
    if str(migration_bot.get("context_window_mode") or "").strip().lower() not in {
        "auto",
        "fixed",
    }:
        migration_bot["context_window_mode"] = "auto"
        normalized["bot"] = migration_bot
        changed = True
        log.warning(
            "Context window mode migrated to 'auto' (discover the real model window); "
            "the per-turn business budget stays 272Ki and max_context_tokens is only "
            "the conservative fallback for unknown models"
        )

    # 旧库只配了 ``group_history_reserve_tokens``（迁移前唯一的余量字段）时，把它镜像到
    # 新的 ``context_reserve_tokens``：**显式运维配置必须保留**。写回之后不再触发。
    mirror_bot = dict(normalized.get("bot") or {})
    if (
        "context_reserve_tokens" not in mirror_bot
        and str(mirror_bot.get("group_history_reserve_tokens") or "").strip()
    ):
        mirror_bot["context_reserve_tokens"] = mirror_bot["group_history_reserve_tokens"]
        normalized["bot"] = mirror_bot
        changed = True

    # The former Sub2API credential was global and therefore cannot be safely
    # migrated to any particular group. All groups intentionally start with
    # the replacement API model query feature disabled.
    if "sub2api" in normalized:
        normalized.pop("sub2api", None)
        changed = True

    # D3-35：``bot.group_can_read_private_history`` 是一个「可配置、无消费者」的旋钮
    # ——getter 写好了、UI 上是个可点的 toggle，但 ``bot/`` 里零调用方（打开后读取
    # 逻辑从未实现）。与其留一个假开关，不如从严格 schema 里移除；老库里已落库的
    # 那一行在这里**一次性**剥离（``extra="forbid"`` 否则会让 initialize() 抛错）。
    strip_bot = dict(normalized.get("bot") or {})
    if "group_can_read_private_history" in strip_bot:
        strip_bot.pop("group_can_read_private_history", None)
        normalized["bot"] = strip_bot
        changed = True
        log.warning(
            "Retired setting removed: bot.group_can_read_private_history "
            "(the group->private-history read path was never implemented)"
        )

    models_payload = normalized.get("models")
    if isinstance(models_payload, dict):
        migrated_models = dict(models_payload)
        models_changed = False

        # Build enough provider metadata to recognize the final LiteLLM model
        # keys used by the previous global override editor.  Raw model and
        # provider/model aliases remain valid fallbacks when metadata is old or
        # incomplete.
        provider_profiles: dict[str, dict[str, str]] = {}
        raw_providers = models_payload.get("providers")
        if isinstance(raw_providers, list):
            for raw_provider in raw_providers:
                if not isinstance(raw_provider, dict):
                    continue
                name = str(raw_provider.get("name") or "").strip().lower()
                if name:
                    provider_profiles[name] = {
                        "provider": str(raw_provider.get("provider") or "").strip().lower(),
                        "api_base": str(raw_provider.get("api_base") or "").strip(),
                    }

        legacy_overrides_raw = models_payload.get("model_overrides")
        legacy_overrides: dict[str, dict[str, Any]] = {}
        if isinstance(legacy_overrides_raw, dict):
            for raw_key, raw_params in legacy_overrides_raw.items():
                key = str(raw_key or "").strip().lower()
                if key and isinstance(raw_params, dict):
                    legacy_overrides[key] = dict(raw_params)

        missing = object()

        role_parents = {
            "vision": "main",
            "decision": "main",
            "moderation": "decision",
            "compress": "main",
        }

        def effective_role_ref(role_name: str) -> tuple[str, str]:
            role = models_payload.get(role_name)
            role = role if isinstance(role, dict) else {}
            if role_name == "embed":
                main_provider, _ = effective_role_ref("main")
                return (
                    str(role.get("provider") or main_provider).strip().lower(),
                    str(role.get("model") or "text-embedding-004").strip(),
                )
            parent_name = role_parents.get(role_name)
            if parent_name:
                parent_provider, parent_model = effective_role_ref(parent_name)
            else:
                parent_provider, parent_model = "", ""
            provider = str(role.get("provider") or parent_provider).strip().lower()
            model = str(role.get("model") or parent_model).strip()
            return provider, model

        def override_for(provider_ref: object, model_name: object) -> dict[str, Any] | object:
            model = str(model_name or "").strip()
            provider_name = str(provider_ref or "").strip().lower()
            if not model or not legacy_overrides:
                return missing
            profile = provider_profiles.get(provider_name, {})
            protocol = str(profile.get("provider") or provider_name).strip().lower()
            api_base = profile.get("api_base") or None
            keys: list[str] = []
            try:
                resolved = _build_litellm_model(protocol, model, api_base=api_base)
            except Exception:
                resolved = ""
            if resolved:
                keys.append(str(resolved).strip().lower())
            keys.append(model.lower())
            if provider_name:
                keys.append(f"{provider_name}/{model}".lower())
            if protocol:
                keys.append(f"{protocol}/{model}".lower())
            for key in keys:
                if key and key in legacy_overrides:
                    return dict(legacy_overrides[key])
            return missing

        for role_name in ("main", "vision", "decision", "moderation", "compress", "embed"):
            role_payload = models_payload.get(role_name)
            if not isinstance(role_payload, dict):
                continue
            migrated_role = dict(role_payload)
            role_changed = False
            effective_provider, effective_model = effective_role_ref(role_name)

            if "reasoning_effort" in migrated_role:
                old_effort = migrated_role.pop("reasoning_effort")
                if (
                    "request_params" not in migrated_role
                    or migrated_role.get("request_params") is None
                ):
                    migrated_role["request_params"] = {"reasoning_effort": old_effort}
                role_changed = True

            role_default = migrated_role.get("request_params")
            if not isinstance(role_default, dict):
                role_default = {}
            primary_override = override_for(
                effective_provider,
                effective_model,
            )
            if primary_override is not missing:
                migrated_role["request_params"] = primary_override
                role_changed = True

            raw_fallbacks = migrated_role.get("fallbacks")
            if isinstance(raw_fallbacks, list):
                migrated_fallbacks: list[Any] = []
                for raw_fallback in raw_fallbacks:
                    if not isinstance(raw_fallback, dict):
                        migrated_fallbacks.append(raw_fallback)
                        continue
                    migrated_fallback = dict(raw_fallback)
                    fallback_model = migrated_fallback.get("model") or effective_model
                    fallback_override = override_for(
                        migrated_fallback.get("provider"),
                        fallback_model,
                    )
                    # Prior role defaults applied to every fallback. Copy them
                    # only when the old payload had no concrete fallback value;
                    # current documents already contain this field.
                    if (
                        "request_params" not in migrated_fallback
                        or migrated_fallback.get("request_params") is None
                    ) and role_default:
                        migrated_fallback["request_params"] = dict(role_default)
                        role_changed = True
                    if fallback_override is not missing:
                        migrated_fallback["request_params"] = fallback_override
                        role_changed = True
                    migrated_fallbacks.append(migrated_fallback)
                if migrated_fallbacks != raw_fallbacks:
                    migrated_role["fallbacks"] = migrated_fallbacks
                    role_changed = True

            if role_changed:
                migrated_models[role_name] = migrated_role
                models_changed = True

        if "model_overrides" in migrated_models:
            migrated_models.pop("model_overrides", None)
            models_changed = True
        if models_changed:
            normalized["models"] = migrated_models
            changed = True
    return (normalized, True) if changed else (payload, False)


class RuntimeConfigManager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        legacy_config_path: str = "config.toml",
        legacy_raw_env: dict[str, str] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.legacy_config_path = legacy_config_path
        self.legacy_raw_env = legacy_raw_env
        self._cipher = SecretCipher(settings.config_master_key)
        self._config: RuntimeConfig | None = None
        self._revision = 0
        self._lock = asyncio.Lock()
        self._on_applied: ConfigAppliedCallback | None = None
        #: 本进程启动时实际装配的 restart 值（基准；只存非 secret 的量）。
        self._applied_restart: dict[str, Any] = {}
        #: 当前"保存值 ≠ 运行值"的 restart 字段名（每次保存重算，不只比上次）。
        self._restart_pending: list[str] = []

    @property
    def config(self) -> RuntimeConfig:
        if self._config is None:
            raise RuntimeError("runtime config manager is not initialized")
        return self._config

    @property
    def revision(self) -> int:
        return self._revision

    def set_apply_callback(self, callback: ConfigAppliedCallback | None) -> None:
        self._on_applied = callback

    async def _load_secret_map(self, session: AsyncSession) -> dict[str, str]:
        result = await session.execute(select(RuntimeConfigSecret))
        rows = list(result.scalars().all())
        if rows and not self._cipher.configured:
            raise RuntimeConfigEncryptionError(
                "CONFIG_MASTER_KEY is missing but encrypted settings already exist"
            )
        return {row.name: self._cipher.decrypt(row.ciphertext) for row in rows}

    async def _write_secret_map(
        self,
        session: AsyncSession,
        secrets: dict[str, str],
        *,
        updated_by: int,
    ) -> None:
        result = await session.execute(select(RuntimeConfigSecret))
        existing = {row.name: row for row in result.scalars().all()}
        for name, row in existing.items():
            if name not in secrets:
                await session.delete(row)
        for name, value in secrets.items():
            row = existing.get(name)
            ciphertext = self._cipher.encrypt(value)
            if row is None:
                session.add(
                    RuntimeConfigSecret(
                        name=name,
                        ciphertext=ciphertext,
                        updated_by=updated_by,
                    )
                )
            else:
                row.ciphertext = ciphertext
                row.updated_by = updated_by

    async def initialize(self) -> RuntimeConfig:
        async with self._lock:
            async with self.session_factory() as session:
                row = await session.get(RuntimeConfigRecord, 1)
                if row is None:
                    config = build_legacy_runtime_config(
                        self.legacy_config_path,
                        settings=self.settings,
                        raw_env=self.legacy_raw_env,
                    )
                    # Resolve provider endpoints before committing the imported document.
                    config.apply_to_settings(
                        self.settings.model_copy(deep=True),
                        apply_prompts=False,
                    )
                    secrets = config.extract_secrets()
                    session.add(
                        RuntimeConfigRecord(
                            id=1,
                            schema_version=CONFIG_SCHEMA_VERSION,
                            revision=1,
                            payload=config.storage_payload(),
                            updated_by=0,
                        )
                    )
                    if secrets:
                        await self._write_secret_map(session, secrets, updated_by=0)
                    await session.commit()
                    revision = 1
                    log.info("Imported legacy runtime settings into the database")
                else:
                    raw_payload, payload_normalized = (
                        _normalize_deprecated_runtime_payload(row.payload or {})
                    )
                    config = RuntimeConfig.model_validate(raw_payload)
                    retired_secret = await session.execute(
                        delete(RuntimeConfigSecret).where(
                            RuntimeConfigSecret.name == "sub2api.api_key"
                        )
                    )
                    retired_secret_deleted = int(retired_secret.rowcount or 0) > 0
                    secrets = await self._load_secret_map(session)
                    config = config.with_secrets(secrets)
                    revision = int(row.revision or 1)
                    if payload_normalized or retired_secret_deleted:
                        # This is an internal safety migration, not an operator
                        # edit. Preserve the revision so an already-open admin
                        # page does not encounter a needless conflict.
                        if payload_normalized:
                            row.payload = raw_payload
                            row.schema_version = CONFIG_SCHEMA_VERSION
                        await session.commit()
                        log.info(
                            "Normalized deprecated runtime settings without "
                            "changing runtime config revision"
                        )

            config.apply_to_settings(self.settings)
            # 本进程"实际装配的 restart 值"就在这一刻定下来：之后的每次保存都拿它
            # 当基准，所以 restart_pending 不会因为一次热字段保存就被清空。
            self._applied_restart = record_applied_restart_values(config)
            # 热更之后，所有没有依赖注入的服务都从这里现取快照。
            policy_runtime.bind(self.settings)
            self._config = config
            self._revision = revision
            return config

    async def _notify_applied(self, config: RuntimeConfig) -> None:
        callback = self._on_applied
        if callback is None:
            return
        try:
            result = callback(config)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Persistence already committed. Report the save as successful and
            # keep the database/live Settings revision coherent; a failed
            # component-specific refresh is logged for operator action.
            log.exception("runtime config post-apply callback failed")

    async def save(
        self,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        updated_by: int,
        secret_changes: dict[str, dict[str, str]] | None = None,
    ) -> RuntimeConfig:
        async with self._lock:
            current = self.config
            normalized_payload, _ = _normalize_deprecated_runtime_payload(payload)
            candidate = RuntimeConfig.model_validate(normalized_payload)
            allowed_paths = candidate.secret_paths()
            secrets = current.extract_secrets()

            # Non-empty secret fields are accepted for non-browser callers, but
            # the Mini App normally uses the explicit replace/clear protocol.
            secrets.update(candidate.extract_secrets())
            for path, change in (secret_changes or {}).items():
                if path not in allowed_paths:
                    raise ValueError(f"unknown secret setting: {path}")
                action = str((change or {}).get("action") or "").strip().lower()
                if action == "keep":
                    continue
                if action == "clear":
                    secrets.pop(path, None)
                    continue
                if action == "replace":
                    value = str((change or {}).get("value") or "").strip()
                    if not value:
                        raise ValueError(f"replacement secret is empty: {path}")
                    secrets[path] = value
                    continue
                raise ValueError(f"invalid secret action for {path}")
            secrets = {
                path: value
                for path, value in secrets.items()
                if path in allowed_paths and value
            }
            candidate = candidate.with_secrets(secrets)
            turnstile_issue = turnstile_key_configuration_issue(
                candidate.verification.turnstile_site_key,
                candidate.verification.turnstile_secret_key,
            )
            current_turnstile_issue = turnstile_key_configuration_issue(
                current.verification.turnstile_site_key,
                current.verification.turnstile_secret_key,
            )
            unchanged_legacy_issue = bool(
                current_turnstile_issue
                and turnstile_issue
                and candidate.verification.turnstile_site_key
                == current.verification.turnstile_site_key
                and candidate.verification.turnstile_secret_key
                == current.verification.turnstile_secret_key
            )
            if turnstile_issue and not unchanged_legacy_issue:
                raise ValueError(turnstile_issue)
            hcaptcha_issue = hcaptcha_key_configuration_issue(
                candidate.verification.hcaptcha_site_key,
                candidate.verification.hcaptcha_secret_key,
            )
            current_hcaptcha_issue = hcaptcha_key_configuration_issue(
                current.verification.hcaptcha_site_key,
                current.verification.hcaptcha_secret_key,
            )
            unchanged_hcaptcha_issue = bool(
                current_hcaptcha_issue
                and hcaptcha_issue
                and candidate.verification.hcaptcha_site_key
                == current.verification.hcaptcha_site_key
                and candidate.verification.hcaptcha_secret_key
                == current.verification.hcaptcha_secret_key
            )
            if hcaptcha_issue and not unchanged_hcaptcha_issue:
                raise ValueError(hcaptcha_issue)
            candidate.apply_to_settings(
                self.settings.model_copy(deep=True),
                apply_prompts=False,
            )

            async with self.session_factory() as session:
                expected = int(expected_revision)
                result = await session.execute(
                    update(RuntimeConfigRecord)
                    .where(
                        RuntimeConfigRecord.id == 1,
                        RuntimeConfigRecord.revision == expected,
                    )
                    .values(
                        payload=candidate.storage_payload(),
                        schema_version=CONFIG_SCHEMA_VERSION,
                        revision=expected + 1,
                        updated_by=int(updated_by),
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    actual = await session.scalar(
                        select(RuntimeConfigRecord.revision).where(
                            RuntimeConfigRecord.id == 1
                        )
                    )
                    raise RuntimeConfigConflictError(
                        f"runtime config revision changed: expected {expected}, got {int(actual or 0)}"
                    )
                await self._write_secret_map(session, secrets, updated_by=int(updated_by))
                await session.commit()
                revision = expected + 1

            candidate.apply_to_settings(self.settings)
            policy_runtime.bind(self.settings)
            self._restart_pending = candidate.restart_pending_paths(
                self._applied_restart or applied_restart_values()
            )
            self._config = candidate
            self._revision = revision
            await self._notify_applied(candidate)
            return candidate

    def api_document(self) -> dict[str, Any]:
        config = self.config
        return {
            "revision": self.revision,
            "config": config.public_payload(),
            "configured_secrets": sorted(config.extract_secrets()),
            "bootstrap": {
                "public_base_url": self.settings.miniapp_public_base_url,
                "listen_host": self.settings.miniapp_listen_host,
                "listen_port": self.settings.miniapp_listen_port,
                "database_url": _redact_database_url(self.settings.database_url),
                "master_key_configured": self._cipher.configured,
            },
            # 契约分两半：``restart_required_paths`` = 全部需要重启的字段；
            # ``restart_pending`` = **刚才这次保存**里真的改了的那几个。
            "restart_required_paths": list(RESTART_REQUIRED_PATHS),
            "restart_pending": list(self._restart_pending),
        }


def _economy_policy_payload(config: EconomySettingsConfig) -> dict[str, Any]:
    """运行时 ``economy`` 段 → ``bot.config.EconomyPolicyConfig`` 的入参。

    读侧把奖池摊成 ``(payout, weight, label)`` 三元组（点服务里已有 ``LotteryPrize``
    这样的 frozen dataclass，用元组可以避免两套结构互相 import）。
    """

    payload = config.model_dump()
    payload["lottery_prizes"] = [
        (prize.payout, prize.weight, prize.label) for prize in config.lottery_prizes
    ]
    return payload


def _iter_model_fields(model: type[BaseModel], prefix: str = "") -> list[tuple[str, Any]]:
    yield prefix.rstrip("."), model
    for name, field in model.model_fields.items():
        annotation = field.annotation
        child_prefix = f"{prefix}{name}."
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield from _iter_model_fields(annotation, child_prefix)
        else:
            yield f"{prefix}{name}".rstrip("."), field


def _restart_required_paths() -> list[str]:
    """列出所有"改完必须重启"的字段路径（模块级 Semaphore / gate / Session）。

    单一来源 = 字段自己的 ``json_schema_extra["reload_kind"]``；``bot.parse_mode``
    是历史遗留（Bot 构造时固化），显式补上。返回值排序稳定，UI 与 API 都用它。
    """

    paths = {"bot.parse_mode"}
    for name, field in _iter_model_fields(ResourceSettingsConfig, "resources."):
        extra = field.json_schema_extra if isinstance(field, FieldInfo) else None
        if isinstance(extra, dict) and extra.get("reload_kind") == "restart":
            paths.add(name)
    return sorted(paths)


#: 需要重启才生效的字段（只含字段名，不含任何值）。
RESTART_REQUIRED_PATHS: tuple[str, ...] = tuple(_restart_required_paths())

#: **本进程启动时真正装配进去的** restart 字段值。
#:
#: 这是"标签与行为"一致性的关键：标了 ``_RESTART`` 的字段在运行期不会被偷偷改掉，
#: 它们的读侧一律走这里，而不是 ``apply_to_settings`` 写下来的"期望值"。运维在
#: Mini App 里改这些字段，保存是成功的（表里存的是期望值，API 照样回
#: ``restart_pending``），但**下一次动作读到的仍是当前进程真正在跑的那个数**，
#: 直到重启。启动路径用 :func:`record_applied_restart_values` 写入一次；没写过就
#: 回落到 schema 默认值（等价于"干净进程"）。
_APPLIED_RESTART_VALUES: dict[str, Any] = {}


def _iter_restart_value_paths() -> list[tuple[str, str]]:
    """restart 字段的 ``(路径, 段名)``；``bot.parse_mode`` 的段名留空。"""

    result: list[tuple[str, str]] = [("bot.parse_mode", "")]
    for path in RESTART_REQUIRED_PATHS:
        if path == "bot.parse_mode":
            continue
        section, _, _name = path.rpartition(".")
        result.append((path, section))
    return result


def _extract_restart_values(config: "RuntimeConfig") -> dict[str, Any]:
    """从一份配置里取出所有 restart 字段的值。

    对**缺字段的替身**保持宽容（``getattr`` + None 跳过）：单测会用只带
    ``resources`` 的轻量替身调启动装配，不该因此炸掉——真正需要完整文档的地方
    永远传真的 ``RuntimeConfig``。
    """

    values: dict[str, Any] = {}
    for path, section in _iter_restart_value_paths():
        if path == "bot.parse_mode":
            bot = getattr(config, "bot", None)
            if bot is not None:
                values[path] = getattr(bot, "parse_mode", None)
            continue
        holder = getattr(config, section, None)
        if holder is None:
            continue
        _sec, _, name = path.rpartition(".")
        if hasattr(holder, name):
            values[path] = getattr(holder, name)
    return values


def record_applied_restart_values(config: "RuntimeConfig") -> dict[str, Any]:
    """记下"本进程启动时装配的 restart 值"（启动路径调用一次）。

    只含非 secret 的标量/容量；``restart_pending`` 与各读侧都以它为基准。
    """

    global _APPLIED_RESTART_VALUES
    # 合并而不是整体替换：调用方可能只带部分段（单测的轻量替身），不该把别的
    # 已装配字段的基线抹掉。
    merged = dict(_APPLIED_RESTART_VALUES)
    merged.update(_extract_restart_values(config))
    _APPLIED_RESTART_VALUES = merged
    return dict(_APPLIED_RESTART_VALUES)


def applied_restart_values() -> dict[str, Any]:
    """本进程实际在跑的 restart 字段值（缺项回落到读侧视图的默认值）。"""

    if _APPLIED_RESTART_VALUES:
        return dict(_APPLIED_RESTART_VALUES)
    from bot.config import ResourcesPolicyConfig as _Defaults

    defaults = _Defaults()
    values: dict[str, Any] = {"bot.parse_mode": "HTML"}
    for path, _section in _iter_restart_value_paths():
        if path == "bot.parse_mode":
            continue
        _sec, _, name = path.rpartition(".")
        values[path] = getattr(defaults, name, None)
    return values


def applied_restart_value(path: str, fallback: Any = None) -> Any:
    """单个 restart 字段的运行值。"""

    return applied_restart_values().get(path, fallback)


def override_applied_restart_values(values: dict[str, Any]) -> dict[str, Any]:
    """**测试专用**：直接设定「本进程装配的 restart 值」，不做 schema 校验。

    生产路径永远走 :func:`record_applied_restart_values`（喂进来的是已校验的
    ``RuntimeConfig``）。这里之所以要留一道缝：单测要把租约压到**毫秒级**才能
    复现「租约到期后恢复循环把仍在执行的 update 交给第二个 worker」这条路径，
    而 schema 正确地禁止了小于 5 秒的租约——测试要的不是"一个合法的配置"，
    而是"一个这样的进程状态"。把它显式命名为 override_，并在调用点写明理由，
    比让测试去 patch 一个不存在的分支诚实。
    """

    global _APPLIED_RESTART_VALUES
    merged = applied_restart_values()
    merged.update(values)
    _APPLIED_RESTART_VALUES = merged
    return dict(_APPLIED_RESTART_VALUES)


def _redact_database_url(url: str) -> str:
    value = str(url or "")
    if "://" not in value or "@" not in value:
        return value
    scheme, rest = value.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        username = credentials.split(":", 1)[0]
        credentials = f"{username}:***"
    return f"{scheme}://{credentials}@{host}"


def _legacy_fallbacks(
    raw: str,
    configured: list[object] | None = None,
) -> list[ModelFallbackConfig]:
    configured = configured or []
    result: list[ModelFallbackConfig] = []
    for index, (provider, model) in enumerate(_parse_fallbacks(raw)):
        source = configured[index] if index < len(configured) else None
        result.append(
            ModelFallbackConfig(
                provider=provider,
                model=model or "",
                request_params=dict(getattr(source, "request_params", {}) or {}),
            )
        )
    return result


def _env_bool(
    name: str,
    default: bool,
    *,
    values: dict[str, str] | None = None,
) -> bool:
    raw = (values or {}).get(name, os.getenv(name))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_int(
    name: str,
    default: int,
    *,
    values: dict[str, str] | None = None,
) -> int:
    try:
        raw = (values or {}).get(name, os.getenv(name, str(default)))
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_choice(
    name: str,
    default: str,
    allowed: set[str],
    *,
    values: dict[str, str],
) -> str:
    value = str(values.get(name, default) or default).strip()
    normalized = value.upper() if default.isupper() else value.lower()
    return normalized if normalized in allowed else default


def _apply_legacy_toml(settings: Settings, config_path: str) -> None:
    path = Path(config_path)
    if not path.exists():
        return
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except Exception as exc:
        raise ValueError(
            f"failed to read legacy TOML settings: {path}"
        ) from exc
    bot_data = data.get("bot") if isinstance(data, dict) else None
    if isinstance(bot_data, dict):
        for key, target in (
            ("main_model", "main_model"),
            ("vision_model", "vision_model"),
            ("decision_model", "decision_model"),
            ("moderation_model", "moderation_model"),
            ("compress_model", "compress_model"),
        ):
            raw = bot_data.get(key)
            if isinstance(raw, dict):
                setattr(settings.bot, target, ModelConfig(**raw))
        raw_embed = bot_data.get("embed_model")
        if isinstance(raw_embed, dict):
            settings.bot.embed_model = EmbedConfig(**raw_embed)
        if "parse_mode" in bot_data:
            settings.bot.parse_mode = str(bot_data["parse_mode"] or "HTML")
        if "disable_link_preview" in bot_data:
            settings.bot.disable_link_preview = bool(
                bot_data["disable_link_preview"]
            )
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bool(bot_data["drop_pending_updates"])
        if "reply_batch_timeout_seconds" in bot_data:
            settings.bot.reply_batch_timeout_seconds = min(
                120.0,
                max(5.0, float(bot_data["reply_batch_timeout_seconds"])),
            )
        for key in (
            "memory_recent_messages",
            "memory_retention_days",
            "memory_archive_max_messages_per_group",
            "memory_recall_max_results",
            "private_chat_history_token_budget",
            "private_chat_history_retention_days",
            "group_history_token_budget",
            "group_history_reserve_tokens",
            "search_record_retention_days",
            "search_freshness_price_hours",
            "search_freshness_news_hours",
            "search_freshness_fact_hours",
            # 第 4 期：长期记忆（user_facts）
            "memory_extract_interval_minutes",
            "memory_extract_min_messages",
            "memory_extract_daily_cap",
            "memory_extract_batch_max",
            "memory_tool_daily_cap",
            "memory_recall_limit",
            "memory_event_ttl_days",
            "memory_deleted_retention_days",
        ):
            if key in bot_data:
                setattr(settings.bot, key, int(bot_data[key]))
        for key in (
            "enable_rich_messages",
            "memory_recall_enabled",
            "memory_automatic_compaction",
            # 第 4 期：长期记忆的两个开关
            "memory_facts_enabled",
            "memory_extract_enabled",
            "memory_tool_enabled",
        ):
            if key in bot_data:
                setattr(settings.bot, key, bool(bot_data[key]))
        legacy_mode = str(bot_data.get("context_window_mode") or "").strip().lower()
        if legacy_mode in {"auto", "fixed"}:
            settings.bot.context_window_mode = legacy_mode
        for key in (
            "context_budget_tokens",
            "context_reserve_tokens",
            "group_history_max_messages",
            "group_summary_recent_raw_messages",
            "group_summary_max_tokens",
            "group_summary_batch_max_messages",
            "group_summary_batch_max_input_tokens",
            "group_summary_global_concurrency",
            "group_summary_per_group_concurrency",
            "group_summary_pending_capacity",
            "group_summary_trigger_messages",
        ):
            if key in bot_data:
                setattr(settings.bot, key, int(bot_data[key]))
        for key in (
            "group_summary_enabled",
        ):
            if key in bot_data:
                setattr(settings.bot, key, bool(bot_data[key]))
        for key in (
            "group_summary_deadline_seconds",
            "group_summary_queue_wait_seconds",
            "group_summary_min_refresh_seconds",
            "group_summary_failure_backoff_seconds",
            "group_summary_failure_backoff_max_seconds",
            "group_summary_trigger_budget_ratio",
        ):
            if key in bot_data:
                setattr(settings.bot, key, float(bot_data[key]))
    moderation_data = data.get("moderation") if isinstance(data, dict) else None
    if isinstance(moderation_data, dict):
        settings.moderation = ModerationConfig(**moderation_data)


def build_legacy_runtime_config(
    config_path: str = "config.toml",
    *,
    settings: Settings | None = None,
    raw_env: dict[str, str] | None = None,
) -> RuntimeConfig:
    """Build the one-time import document from old env/TOML settings."""
    settings = settings or Settings()
    _apply_legacy_toml(settings, config_path)
    raw_env = _load_raw_env() if raw_env is None else raw_env
    # A malformed legacy provider must stop the one-time import. Persisting a
    # fallback document here would make the corrected env impossible to import
    # on the next start because the database row would already exist.
    legacy_profiles = _collect_provider_profiles(raw_env)

    providers = [
        ProviderConfig(
            name=name,
            provider=profile.provider,
            api_key=profile.api_key or "",
            api_base=profile.api_base or "",
            stream=profile.stream,
            chat_endpoint=profile.chat_endpoint,
        )
        for name, profile in legacy_profiles.items()
    ]
    if not providers:
        providers = [ProviderConfig(name="gemini", provider="gemini")]

    known_names = {provider.name for provider in providers}
    main_provider = settings.main_provider_name.strip().lower()
    if main_provider not in known_names:
        main_provider = providers[0].name

    def role_provider(raw: str, parent: str = main_provider) -> str:
        value = str(raw or "").strip().lower()
        return value if value in known_names else parent

    model_settings = ModelSettingsConfig(
        providers=providers,
        main=ChatRoleConfig(
            provider=main_provider,
            model=settings.main_model.strip() or "gemini-2.0-flash",
            fallbacks=_legacy_fallbacks(
                settings.main_fallbacks,
                settings.bot.main_model.fallbacks,
            ),
            temperature=settings.bot.main_model.temperature,
            max_tokens=max(1, settings.max_output_tokens),
            timeout_sec=settings.main_timeout_sec,
            total_deadline_sec=max(0.0, settings.main_total_deadline_sec),
            request_params=dict(settings.bot.main_model.request_params),
        ),
        vision=ChatRoleConfig(
            provider=role_provider(settings.vision_provider_name),
            model=settings.vision_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.vision_fallbacks,
                settings.bot.vision_model.fallbacks,
            ),
            temperature=settings.bot.vision_model.temperature,
            max_tokens=settings.bot.vision_model.max_tokens,
            timeout_sec=settings.vision_timeout_sec,
            total_deadline_sec=max(0.0, settings.vision_total_deadline_sec),
            request_params=dict(settings.bot.vision_model.request_params),
        ),
        decision=ChatRoleConfig(
            provider=role_provider(settings.decision_provider_name),
            model=settings.decision_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.decision_fallbacks,
                settings.bot.decision_model.fallbacks,
            ),
            temperature=settings.bot.decision_model.temperature,
            max_tokens=settings.bot.decision_model.max_tokens,
            timeout_sec=settings.decision_timeout_sec,
            total_deadline_sec=max(0.0, settings.decision_total_deadline_sec),
            request_params=dict(settings.bot.decision_model.request_params),
        ),
        moderation=ChatRoleConfig(
            provider=role_provider(settings.moderation_provider_name),
            model=settings.moderation_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.moderation_fallbacks,
                settings.bot.moderation_model.fallbacks,
            ),
            temperature=settings.bot.moderation_model.temperature,
            max_tokens=settings.bot.moderation_model.max_tokens,
            timeout_sec=settings.moderation_timeout_sec,
            total_deadline_sec=max(0.0, settings.moderation_total_deadline_sec),
            request_params=dict(settings.bot.moderation_model.request_params),
        ),
        compress=ChatRoleConfig(
            provider=role_provider(settings.compress_provider_name),
            model=settings.compress_model.strip(),
            fallbacks=_legacy_fallbacks(
                settings.compress_fallbacks,
                settings.bot.compress_model.fallbacks,
            ),
            temperature=settings.bot.compress_model.temperature,
            max_tokens=settings.bot.compress_model.max_tokens,
            timeout_sec=settings.compress_timeout_sec,
            total_deadline_sec=max(0.0, settings.compress_total_deadline_sec),
            request_params=dict(settings.bot.compress_model.request_params),
        ),
        embed=EmbedRoleConfig(
            provider=role_provider(settings.embed_provider_name),
            model=settings.embed_model.strip() or "text-embedding-004",
            fallbacks=_legacy_fallbacks(
                settings.embed_fallbacks,
                settings.bot.embed_model.fallbacks,
            ),
            timeout_sec=settings.embed_timeout_sec,
            total_deadline_sec=max(0.0, settings.embed_total_deadline_sec),
            request_params=dict(settings.bot.embed_model.request_params),
        ),
        retry_attempts=settings.llm_retry_attempts,
        retry_backoff_sec=settings.llm_retry_backoff_sec,
        retry_timeout_multiplier=settings.llm_retry_timeout_multiplier,
    )

    return RuntimeConfig(
        models=model_settings,
        bot=BotBehaviorConfig(
            parse_mode=settings.bot.parse_mode,
            disable_link_preview=settings.bot.disable_link_preview,
            drop_pending_updates=settings.bot.drop_pending_updates,
            inbound_debounce_seconds=settings.bot_inbound_debounce_seconds,
            reply_batch_timeout_seconds=(
                settings.bot_reply_batch_timeout_seconds
                if "bot_reply_batch_timeout_seconds"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.reply_batch_timeout_seconds
            ),
            enable_typing=settings.bot_enable_typing,
            enable_rich_messages=(
                settings.bot_enable_rich_messages
                if "bot_enable_rich_messages" in getattr(settings, "model_fields_set", set())
                else settings.bot.enable_rich_messages
            ),
            enable_streaming=settings.bot_enable_streaming,
            stream_chunk_size=settings.bot_stream_chunk_size,
            stream_edit_interval_sec=settings.bot_stream_edit_interval_sec,
            auto_delete_seconds=(
                int(settings.bot_auto_delete_seconds)
                if "bot_auto_delete_seconds" in getattr(settings, "model_fields_set", set())
                else int(settings.bot_auto_delete_seconds)
                or max(0, int(settings.bot_auto_delete_minutes)) * 60
            ),
            auto_delete_categories=list(settings.bot.auto_delete_categories),
            auto_delete_category_seconds=dict(
                settings.bot.auto_delete_category_seconds
            ),
            auto_delete_category_mode=dict(settings.bot.auto_delete_category_mode),
            decision_context_items=settings.bot_decision_context_items,
            context_window_mode=settings.bot.context_window_mode,
            max_context_tokens=settings.max_context_tokens,
            context_budget_tokens=settings.bot.context_budget_tokens,
            context_reserve_tokens=settings.bot.context_reserve_tokens,
            group_history_max_messages=settings.bot.group_history_max_messages,
            **{
                _name: getattr(settings.bot, _name)
                for _name in _GROUP_SUMMARY_FIELD_NAMES
            },
            max_output_tokens=settings.max_output_tokens,
            memory_recent_messages=(
                settings.bot_memory_recent_messages
                if "bot_memory_recent_messages"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recent_messages
            ),
            memory_retention_days=(
                settings.bot_memory_retention_days
                if "bot_memory_retention_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_retention_days
            ),
            memory_archive_max_messages_per_group=(
                settings.bot_memory_archive_max_messages_per_group
                if "bot_memory_archive_max_messages_per_group"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_archive_max_messages_per_group
            ),
            memory_recall_enabled=(
                settings.bot_memory_recall_enabled
                if "bot_memory_recall_enabled"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recall_enabled
            ),
            memory_recall_max_results=(
                settings.bot_memory_recall_max_results
                if "bot_memory_recall_max_results"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recall_max_results
            ),
            memory_automatic_compaction=(
                settings.bot_memory_automatic_compaction
                if "bot_memory_automatic_compaction"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_automatic_compaction
            ),
            private_chat_history_token_budget=(
                settings.bot_private_chat_history_token_budget
                if "bot_private_chat_history_token_budget"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.private_chat_history_token_budget
            ),
            private_chat_history_retention_days=(
                settings.bot_private_chat_history_retention_days
                if "bot_private_chat_history_retention_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.private_chat_history_retention_days
            ),
            group_history_token_budget=(
                settings.bot_group_history_token_budget
                if "bot_group_history_token_budget"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.group_history_token_budget
            ),
            group_history_reserve_tokens=(
                settings.bot_group_history_reserve_tokens
                if "bot_group_history_reserve_tokens"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.group_history_reserve_tokens
            ),
            search_record_retention_days=(
                settings.bot_search_record_retention_days
                if "bot_search_record_retention_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.search_record_retention_days
            ),
            search_freshness_price_hours=(
                settings.bot_search_freshness_price_hours
                if "bot_search_freshness_price_hours"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.search_freshness_price_hours
            ),
            search_freshness_news_hours=(
                settings.bot_search_freshness_news_hours
                if "bot_search_freshness_news_hours"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.search_freshness_news_hours
            ),
            search_freshness_fact_hours=(
                settings.bot_search_freshness_fact_hours
                if "bot_search_freshness_fact_hours"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.search_freshness_fact_hours
            ),
            # 第 4 期：长期记忆（user_facts）
            memory_facts_enabled=(
                settings.bot_memory_facts_enabled
                if "bot_memory_facts_enabled"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_facts_enabled
            ),
            memory_extract_enabled=(
                settings.bot_memory_extract_enabled
                if "bot_memory_extract_enabled"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_extract_enabled
            ),
            memory_extract_interval_minutes=(
                settings.bot_memory_extract_interval_minutes
                if "bot_memory_extract_interval_minutes"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_extract_interval_minutes
            ),
            memory_extract_min_messages=(
                settings.bot_memory_extract_min_messages
                if "bot_memory_extract_min_messages"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_extract_min_messages
            ),
            memory_extract_daily_cap=(
                settings.bot_memory_extract_daily_cap
                if "bot_memory_extract_daily_cap"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_extract_daily_cap
            ),
            memory_extract_batch_max=(
                settings.bot_memory_extract_batch_max
                if "bot_memory_extract_batch_max"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_extract_batch_max
            ),
            memory_tool_enabled=(
                settings.bot_memory_tool_enabled
                if "bot_memory_tool_enabled"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_tool_enabled
            ),
            memory_tool_daily_cap=(
                settings.bot_memory_tool_daily_cap
                if "bot_memory_tool_daily_cap"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_tool_daily_cap
            ),
            memory_recall_limit=(
                settings.bot_memory_recall_limit
                if "bot_memory_recall_limit"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_recall_limit
            ),
            memory_event_ttl_days=(
                settings.bot_memory_event_ttl_days
                if "bot_memory_event_ttl_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_event_ttl_days
            ),
            memory_deleted_retention_days=(
                settings.bot_memory_deleted_retention_days
                if "bot_memory_deleted_retention_days"
                in getattr(settings, "model_fields_set", set())
                else settings.bot.memory_deleted_retention_days
            ),
            proactive_default_enabled=settings.bot_proactive_default_enabled,
            proactive_idle_minutes=settings.bot_proactive_idle_minutes,
            proactive_jitter_minutes=settings.bot_proactive_jitter_minutes,
            proactive_check_interval_seconds=settings.bot_proactive_check_interval_seconds,
            proactive_quiet_hours_start=settings.bot_proactive_quiet_hours_start,
            proactive_quiet_hours_end=settings.bot_proactive_quiet_hours_end,
            proactive_retry_minutes=settings.bot_proactive_retry_minutes,
        ),
        moderation=ModerationSettingsConfig(**settings.moderation.model_dump()),
        patrol=PatrolSettingsConfig(
            enabled=settings.patrol_enabled,
            schedule_time=settings.patrol_schedule_time,
            batch_size=settings.patrol_batch_size,
            batch_pause_seconds=settings.patrol_batch_pause_seconds,
            fetch_bio=settings.patrol_fetch_bio,
            challenge_timeout_seconds=settings.patrol_challenge_timeout_seconds,
            check_interval_seconds=settings.patrol_check_interval_seconds,
        ),
        raid_guard=RaidGuardSettingsConfig(
            enabled=settings.raid_guard_enabled,
            pin_message=settings.raid_guard_pin_message,
            join_threshold=settings.raid_guard_join_threshold,
            window_seconds=settings.raid_guard_window_seconds,
            lockdown_seconds=settings.raid_guard_lockdown_seconds,
            lookback_seconds=settings.raid_guard_lookback_seconds,
            challenge_timeout_seconds=settings.raid_guard_challenge_timeout_seconds,
        ),
        call_admin=CallAdminSettingsConfig(
            enabled=settings.call_admin_enabled,
            pin_message=settings.call_admin_pin_message,
            cooldown_seconds=settings.call_admin_cooldown_seconds,
        ),
        vote_ban=VoteBanSettingsConfig(
            enabled=settings.vote_ban_enabled,
            pin_message=settings.vote_ban_pin_message,
            vote_threshold=settings.vote_ban_threshold,
            duration_seconds=settings.vote_ban_duration_seconds,
            trigger_limit=settings.vote_ban_trigger_limit,
            trigger_window_seconds=settings.vote_ban_trigger_window_seconds,
        ),
        verification=VerificationSettingsConfig(
            enabled=settings.join_verification_enabled,
            timeout_seconds=settings.join_verification_timeout_seconds,
            check_interval_seconds=settings.join_verification_check_interval_seconds,
            provider=settings.join_verification_provider,
            turnstile_site_key=settings.join_verification_turnstile_site_key,
            turnstile_secret_key=settings.join_verification_turnstile_secret_key,
            hcaptcha_site_key=settings.join_verification_hcaptcha_site_key,
            hcaptcha_secret_key=settings.join_verification_hcaptcha_secret_key,
        ),
        tts=TTSSettingsConfig(
            enabled=settings.doubao_tts_enabled,
            provider=str(getattr(settings, "doubao_tts_provider", "") or "").strip().lower(),
            http_timeout_sec=settings.doubao_tts_http_timeout_sec,
            max_text_length=settings.doubao_tts_max_text_length,
            api_base=settings.doubao_tts_api_base,
            app_id=settings.doubao_tts_app_id,
            app_key=settings.doubao_tts_app_key,
            access_key=settings.doubao_tts_access_key,
            resource_id=settings.doubao_tts_resource_id,
            model=settings.doubao_tts_model,
            speaker=settings.doubao_tts_speaker,
            audio_format=settings.doubao_tts_audio_format,
            sample_rate=settings.doubao_tts_sample_rate,
            bit_rate=settings.doubao_tts_bit_rate,
            emotion=settings.doubao_tts_emotion,
            emotion_scale=settings.doubao_tts_emotion_scale,
            speech_rate=settings.doubao_tts_speech_rate,
            loudness_rate=settings.doubao_tts_loudness_rate,
            silence_duration_ms=settings.doubao_tts_silence_duration_ms,
        ),
        music=MusicSettingsConfig(
            enabled=settings.music_api_enabled,
            http_timeout_sec=settings.music_api_http_timeout_sec,
            base_url=settings.music_api_base_url,
            default_source=settings.music_api_default_source,
            stable_sources=[
                value.strip()
                for value in settings.music_api_stable_sources.split(",")
                if value.strip()
            ],
        ),
        movie_info=MovieInfoSettingsConfig(
            enabled=settings.movie_info_enabled,
            http_timeout_sec=settings.movie_info_http_timeout_sec,
            max_results=settings.movie_info_max_results,
            default_language=settings.movie_info_default_language,
            default_region=settings.movie_info_default_region,
            tmdb_read_access_token=settings.movie_info_tmdb_read_access_token,
            imdb_data_set_id=settings.movie_info_imdb_data_set_id,
            imdb_revision_id=settings.movie_info_imdb_revision_id,
            imdb_asset_id=settings.movie_info_imdb_asset_id,
            imdb_api_key=settings.movie_info_imdb_api_key,
            imdb_aws_access_key_id=settings.movie_info_imdb_aws_access_key_id,
            imdb_aws_secret_access_key=(
                settings.movie_info_imdb_aws_secret_access_key
            ),
            imdb_aws_session_token=settings.movie_info_imdb_aws_session_token,
        ),
        av=AVSettingsConfig(
            enabled=settings.av_enabled,
            http_timeout_sec=settings.av_http_timeout_sec,
            max_results=settings.av_max_results,
            dm_sample_count=settings.av_dm_sample_count,
            inline_seed_count=settings.av_inline_seed_count,
            ai_synopsis_enabled=settings.av_ai_synopsis_enabled,
            javbus_base_url=settings.av_javbus_base_url,
            madouqu_base_url=settings.av_madouqu_base_url,
            dmm_base_url=settings.av_dmm_base_url,
            fc2_base_url=settings.av_fc2_base_url,
        ),
        stickers=StickerSettingsConfig(
            fallback_file_ids=[
                value.strip()
                for value in settings.skill_sticker_file_ids.split(",")
                if value.strip()
            ]
        ),
        logging=LoggingSettingsConfig(
            level=_env_choice(
                "LOG_LEVEL",
                "INFO",
                {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
                values=raw_env,
            ),
            third_party_level=_env_choice(
                "LOG_THIRD_PARTY_LEVEL",
                "WARNING",
                {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
                values=raw_env,
            ),
            color=_env_choice(
                "LOG_COLOR",
                "on",
                {"on", "off", "auto"},
                values=raw_env,
            ),
            to_file=_env_bool("LOG_TO_FILE", False, values=raw_env),
            file_path=(
                str(raw_env.get("LOG_FILE_PATH", "data/bot.log")).strip()
                or "data/bot.log"
            ),
            file_max_bytes=max(
                1024,
                _env_int(
                    "LOG_FILE_MAX_BYTES",
                    5 * 1024 * 1024,
                    values=raw_env,
                ),
            ),
            file_backup_count=max(
                1,
                _env_int(
                    "LOG_FILE_BACKUP_COUNT",
                    3,
                    values=raw_env,
                ),
            ),
            message_preview_chars=max(
                0,
                _env_int(
                    "LOG_MESSAGE_PREVIEW_CHARS",
                    100,
                    values=raw_env,
                ),
            ),
        ),
        prompts=PromptSettingsConfig.defaults(),
    )
