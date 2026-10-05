from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.utils.budget import validate_business_budget

log = logging.getLogger(__name__)

try:
    from dotenv import dotenv_values
except ModuleNotFoundError:
    def dotenv_values(path: str | Path) -> dict[str, str]:
        values: dict[str, str] = {}
        file_path = Path(path)
        if not file_path.exists():
            return values
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key:
                values[key] = value
        return values


class ProviderProfile(BaseModel):
    provider: str
    api_key: str | None = None
    api_base: str | None = None
    stream: bool = False
    chat_endpoint: Literal["chat_completions", "responses"] = "chat_completions"
    endpoint_path: str = "/chat/completions"


class ChatEndpointConfig(BaseModel):
    model: str = "gemini/gemini-2.0-flash"
    provider: str = ""
    api_key: str | None = None
    api_base: str | None = None
    stream: bool = False
    chat_endpoint: Literal["chat_completions", "responses"] = "chat_completions"
    endpoint_path: str = "/chat/completions"
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout_sec: float = 12.0
    retry_attempts: int = 2
    retry_backoff_sec: float = 0.8
    retry_timeout_multiplier: float = 1.35
    # Total wall-clock budget across all attempts and fallbacks for one call.
    # 0 = use the built-in per-stage default.
    total_deadline_sec: float = 0.0
    # Provider-specific JSON parameters. The request builder protects the
    # routing and transport fields owned by this config.
    request_params: dict[str, Any] = Field(default_factory=dict)


class ModelConfig(ChatEndpointConfig):
    fallbacks: list[ChatEndpointConfig] = Field(default_factory=list)


class EmbedEndpointConfig(BaseModel):
    model: str = "gemini/text-embedding-004"
    provider: str = ""
    api_key: str | None = None
    api_base: str | None = None
    endpoint_path: str = "/v1beta/models"
    timeout_sec: float = 10.0
    retry_attempts: int = 2
    retry_backoff_sec: float = 0.8
    retry_timeout_multiplier: float = 1.25
    # Total wall-clock budget across all attempts and fallbacks for one call.
    # 0 = use the built-in per-stage default.
    total_deadline_sec: float = 0.0
    # Provider-specific JSON parameters for this concrete embedding model.
    request_params: dict[str, Any] = Field(default_factory=dict)


class EmbedConfig(EmbedEndpointConfig):
    fallbacks: list[EmbedEndpointConfig] = Field(default_factory=list)


#: 顶层（env / 扁平字段）显式设置时覆盖到 ``settings.bot`` 的字段清单。
TOP_LEVEL_BUDGET_FIELD_NAMES: tuple[str, ...] = (
    "context_budget_tokens",
    "context_reserve_tokens",
    "group_history_max_messages",
)


def apply_top_level_budget_overrides(settings: Any) -> None:
    """把顶层（env / 扁平配置）里**显式设置过**的预算与摘要字段同步到 ``settings.bot``。

    ``load_settings`` 调用它；单独抽出来是因为"env 种子能不能传到 bot 配置"本身是
    一条必须在测试里直接锁住的契约（``[bot]`` TOML 路径由 runtime_config 的旧导入覆盖）。
    """

    explicit = getattr(settings, "model_fields_set", set())
    for top_level in (*TOP_LEVEL_BUDGET_FIELD_NAMES, *GROUP_SUMMARY_SETTING_NAMES):
        if top_level in explicit:
            setattr(settings.bot, top_level, getattr(settings, top_level))


#: 第②项后台摘要的顶层/``[bot]`` 字段名（env 种子与 TOML 导入共用一份清单）。
GROUP_SUMMARY_SETTING_NAMES: tuple[str, ...] = (
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


class BotConfig(BaseModel):
    token: str = ""
    parse_mode: str = "HTML"
    disable_link_preview: bool = True
    drop_pending_updates: bool = False
    inbound_debounce_seconds: float = 5.0
    reply_batch_timeout_seconds: float = 45.0
    enable_typing: bool = True
    enable_streaming: bool = True
    # Bot API 10.1+ rich messages (sendRichMessage with Rich Markdown).
    enable_rich_messages: bool = True
    stream_chunk_size: int = 36
    stream_edit_interval_sec: float = 1.0
    auto_delete_seconds: int = 0
    # Deprecated compatibility alias for integrations that still set minutes.
    auto_delete_minutes: int = 0
    auto_delete_categories: list[str] = Field(
        default_factory=lambda: ["management", "moderation"]
    )
    # Per-category retention overrides (seconds); 0/missing inherits
    # auto_delete_seconds.
    auto_delete_category_seconds: dict[str, int] = Field(default_factory=dict)
    # Per-category cleanup mode: missing/"timer" schedules the delayed
    # delete; "button" attaches an inline delete button instead.
    auto_delete_category_mode: dict[str, str] = Field(default_factory=dict)
    decision_context_items: int = 5
    proactive_default_enabled: bool = False
    proactive_idle_minutes: int = 180
    proactive_jitter_minutes: int = 60
    proactive_check_interval_seconds: float = 60.0
    proactive_quiet_hours_start: int = 0
    proactive_quiet_hours_end: int = 9
    proactive_retry_minutes: int = 30
    main_model: ModelConfig = ModelConfig()
    vision_model: ModelConfig = ModelConfig()
    decision_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=512,
        timeout_sec=6.0,
    )
    moderation_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.1,
        max_tokens=1024,
        timeout_sec=8.0,
    )
    compress_model: ModelConfig = ModelConfig(
        model="gemini/gemini-2.0-flash",
        temperature=0.3,
        max_tokens=1024,
        timeout_sec=12.0,
    )
    # Tool-calling (skills) stage route; ``None`` = reuse ``main_model``.
    skill_model: ModelConfig | None = None
    embed_model: EmbedConfig = EmbedConfig()
    # 上下文模式（2026-10-04 生产事故修复）：
    #   * ``auto``（默认）：自动**发现**模型真实窗口（网关 ``/models`` 优先，其次直连
    #     厂商注册表，都没有才用 ``max_context_tokens`` 保守降级）。发现值只用于
    #     "模型比业务预算更小就跟着更小"——它**不会**让每轮去填满百万窗口。
    #   * ``fixed``：不查元数据，``max_context_tokens`` 就是模型侧的硬上限。
    # 两种模式都还要再叠**业务预算 272Ki**（见下一项）。
    context_window_mode: Literal["auto", "fixed"] = "auto"
    # 模型侧窗口的**兼容字段**：``auto`` 下作为"查不到任何元数据"时的保守降级值，
    # ``fixed`` 下作为硬上限。它不等于每轮的业务预算（见下面两项）。
    max_context_tokens: int = 278528
    # 每轮**业务总预算**（运行时可读写，默认 272Ki = 278528）：覆盖 system/人设 +
    # 工具定义 + 记忆/召回 + 历史 + 本轮 + 工具结果 + 输出预留。显式配置不被隐藏常量
    # 截断（文档建议不超过 272Ki）；与模型真实窗口取小。
    context_budget_tokens: int = Field(default=278528, ge=1024, le=16_000_000)
    # 业务预算里留给**输出/工具余量**的部分（默认 32Ki = 32768），必须小于业务总预算；
    # 实际输出需求更大时按 ``max_tokens`` 进一步增加预留（不重复扣）。
    context_reserve_tokens: int = Field(default=32768, ge=1024, le=8_000_000)
    # 群聊单次装配/读取的**条数安全上限**（默认最近 1000 条）。
    group_history_max_messages: int = Field(default=1000, ge=1, le=20_000)
    max_output_tokens: int = 2048
    # Two-tier group memory: a bounded hot window plus a lossless, per-group
    # archive used by relevance-based recall.  The archive is the source of
    # truth; context compaction is retained only as an opt-in legacy feature.
    memory_recent_messages: int = 500
    memory_retention_days: int = 7
    memory_archive_max_messages_per_group: int = 50000
    memory_recall_enabled: bool = True
    memory_recall_max_results: int = 8
    memory_automatic_compaction: bool = False
    # One-to-one private chat history: rows persist in ``private_chat_messages``
    # and every turn is assembled by token budget.  278528 = 272K is the legacy
    # depth target and the upper bound of the per-turn business budget; in ``auto``
    # mode a *smaller* discovered model window tightens it further (a 1M/4M window
    # never loosens it).  Retention mirrors ``memory_retention_days``.
    private_chat_history_token_budget: int = 278528
    private_chat_history_retention_days: int = 30
    # 群聊回复的历史：不再按条数（``memory_recent_messages``）取，而是按 token 预算
    # 从 ``group_message_archive`` 里装配（数据来源与删除/保留策略都没变）。
    # ``group_history_reserve_tokens`` 是留给「系统提示词/人设 + 本轮消息 + 记忆召回
    # + 回复预留」的余量：装配历史 + 余量 ≤ 生效窗口，这是硬闸门。
    # 生效窗口 = min(模型真实窗口, 这里的 272Ki 业务预算)；模型更小就跟着更小。
    # 条数安全上限另见 ``GROUP_HISTORY_MAX_MESSAGES``（最近 1000 条）。
    group_history_token_budget: int = 278528
    group_history_reserve_tokens: int = 32768
    # 第 3 期：检索结果留档（``search_result_records``）。
    # - ``search_record_retention_days``：留档保留期（默认 30 天，夹取 1..365），
    #   由后台巡检清理；
    # - ``search_freshness_*_hours``：各类信息的新鲜窗口（价格 24h / 新闻 48h /
    #   事实 7d）；再次注入留档时，超出窗口的会标注「可能已过期」。
    search_record_retention_days: int = 30
    search_freshness_price_hours: int = 24
    search_freshness_news_hours: int = 48
    search_freshness_fact_hours: int = 168
    # 第 3 期：方向规则（隐私红线）。群 → 私聊允许；私聊 → 群**默认禁止**。
    # 这个开关是给「以后用户显式授权」留的位置：默认 False 时群聊装配上下文的任何
    # 路径都不读 ``private_chat_messages``；本期**不实现**打开后的读取逻辑。
    group_can_read_private_history: bool = False
    # 第 4 期：长期记忆（``user_facts``）。机器人从群聊/私聊里提炼稳定的结构化事实
    # （身份/偏好/关系/禁忌/技能/事件），去重合并后按需注入。全部可运行时覆盖。
    # - ``memory_facts_enabled``：总开关（关掉 = 不提炼、不注入、不写）；
    # - ``memory_extract_*``：后台被动提炼的开关/节奏/每次最小新增条数/每日次数上限/
    #   单批最大条数（每日上限 0 = 不限）；
    # - ``memory_tool_*``：模型主动调 ``remember`` 工具的开关与每作用域每日条数上限；
    # - ``memory_recall_limit``：每次注入最多几条；
    # - ``memory_event_ttl_days``：``category='event'`` 的过期天数；
    # - ``memory_deleted_retention_days``：被删除/被替代的事实保留多少天后物理清理。
    memory_facts_enabled: bool = True
    memory_extract_enabled: bool = True
    memory_extract_interval_minutes: int = 30
    memory_extract_min_messages: int = 20
    memory_extract_daily_cap: int = 48
    memory_extract_batch_max: int = 200
    memory_tool_enabled: bool = True
    memory_tool_daily_cap: int = 30
    memory_recall_limit: int = 8
    memory_event_ttl_days: int = 30
    memory_deleted_retention_days: int = 30
    # B-39 / P4-9：每日提炼次数台账（进程内 dict）的保留天数。默认 1 = 只留当天，
    # 与改前**额度口径完全一致**（读侧只按自然日过滤，历史条目本来就不参与判定）。
    # 调大只是让运维在进程内多留几天分作用域计数便于排查。
    memory_extract_ledger_retention_days: int = 1

    # 第②项：后台群摘要（默认关闭；与 legacy 热历史压缩完全独立，不会自动打开旧开关）。
    # 原文/归档/私聊一条都不删，摘要只是"旧内容的低信任资料"，前台只读已发布的摘要。
    group_summary_enabled: bool = False
    group_summary_recent_raw_messages: int = Field(default=200, ge=20, le=10_000)
    group_summary_max_tokens: int = Field(default=4096, ge=256, le=32_768)
    group_summary_batch_max_messages: int = Field(default=200, ge=10, le=2_000)
    group_summary_batch_max_input_tokens: int = Field(
        default=16_384, ge=1_024, le=1_000_000
    )
    group_summary_global_concurrency: int = Field(default=2, ge=1, le=8)
    # 每群并发是**硬安全约束**：只能是 1（不是"每用户 1"）。
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
    def _validate_business_budget(self) -> "BotConfig":
        """业务预算的显式校验：预留必须小于总预算，0 不能关闭门禁。"""

        error = validate_business_budget(
            self.context_budget_tokens,
            self.context_reserve_tokens,
        )
        if error:
            raise ValueError(error)
        if (
            self.group_summary_failure_backoff_max_seconds
            < self.group_summary_failure_backoff_seconds
        ):
            raise ValueError(
                "摘要失败退避上限必须不小于起点"
                "（group_summary_failure_backoff_max_seconds）"
            )
        return self


class ModerationConfig(BaseModel):
    enabled: bool = True
    warn_threshold: int = 3
    # 违规判定置信度 >= 该值时直接按规则动作处理；低于该值时 warn/delete
    # 仍按原动作处理，只有 ban 规则要求真人质询。
    high_confidence_threshold: float = Field(default=0.9, allow_inf_nan=False)
    # 低置信度 ban 规则质询限时（秒），超时自动封禁。
    challenge_timeout_seconds: int = 600
    # 其他 bot（如 guest 模式广告机）发送的消息也进入内容审核；
    # 连续 bot_screening_message_count 条干净后加入白名单不再审核。
    bot_screening_enabled: bool = True
    bot_screening_message_count: int = 5
    # 群内公开发布露骨色情图片（色情/裸露）→ 删图 + 群内 @警告 + 质询。
    # 判定复用审核链路本来就有的那次视觉调用（不新增模型调用）。
    # F-024 / 用户裁定（2026-10）：默认**关闭**（opt-in）。这是对用户可见的执法
    # 行为，必须由运维显式开启；开启状态会在启动日志里逐条列出（见
    # ``log_enforcement_switch_state``）。关闭后不做判定（提示词里也不加 NSFW
    # 要求）也不做任何处置——即旧版本行为。
    # 生产环境必须显式打开，否则 NSFW 图片处置不再发生（详见 README 与
    # FIX-p2-product.md 的"生产侧需要显式做哪些配置"）。
    nsfw_image_guard_enabled: bool = False
    # 广告经「引用/转发」再次传播时，被引用那条消息的原作者同样按规则处置
    # （删除其消息 + 记违规 + 既有质询/禁言流程）。
    # F-024 / 用户裁定（2026-10）：默认**关闭**（opt-in）。默认开启等于升级后静默
    # 扩大执法范围（最长 7 天禁言），所以由运维显式选择；关闭后行为与旧版完全
    # 一致。
    # 生产环境必须显式打开（运行时配置项 ``moderation.punish_quoted_author_enabled``）。
    punish_quoted_author_enabled: bool = False
    # 被引用消息超过该时长（秒）就不再追溯原作者，只记日志。默认 7 天。
    quoted_author_max_age_seconds: int = 7 * 24 * 60 * 60
    # 管理员/群主不再豁免日常审核：照常判定，命中后只删消息 + 群内 @警示 + 记违规，
    # 不质询/不封禁/不禁言/不累计警告。
    # F-024 / 用户裁定（2026-10）：默认**关闭**（opt-in，即回到"整段跳过"的旧
    # 行为）——对管理员/群主开始执法属于可见的策略变更，应由运维显式决定。
    # 生产环境必须显式打开（运行时配置项 ``moderation.admin_moderation_enabled``）。
    admin_moderation_enabled: bool = False
    # 管理员命中违规时，私聊最高管理员一份完整证据（best-effort，不刷屏）。默认开启。
    admin_alert_super_admin_enabled: bool = True
    # 审核命中证据投递到「审核日志」频道（取代私聊最高管理员）：群里所有被处置
    # 的命中都单独发一条完整证据卡（带「人工放行 / 放行收回」按钮）。默认开启；
    # 关掉后回到私聊最高管理员的老路径（含 10 分钟聚合抑制）。
    log_channel_enabled: bool = True
    # 证据频道 id。**默认值就是下面那个频道**（有意为之，不是占位符）；把「未配置」
    # 的语义留给 0：``_admin_log_channel_id`` 把 0 视为"不可用"，此时频道投递整体
    # 关闭、回退私聊最高管理员的老路径（含 10 分钟聚合抑制）。
    #
    # 覆盖入口（按代码实际能力，与 README 的披露一致）：
    #   * 运行时配置（**主要入口**）：Mini App 后端 ``PUT /api/v1/settings``，
    #     body ``{"config": {"moderation": {"log_channel_id": <id>}}, "revision": <n>}``，
    #     落库到 ``runtime_config`` 表并热生效（``apply_to_settings``）。
    #     注意 Mini App 界面**目前没有**这个控件，需要用 API 或直接改库。
    #   * ``config.toml`` 的 ``[moderation]`` 段：**仅**在 ``runtime_config`` 行还不
    #     存在时做一次性导入（之后该文件被忽略）。
    #   * 环境变量：**无效**。``ModerationConfig`` 是普通 ``BaseModel``，``Settings``
    #     没有 ``env_nested_delimiter``，也没有扁平的 ``moderation_log_channel_id``
    #     字段，所以 ``MODERATION__LOG_CHANNEL_ID`` 读不到（tests/
    #     test_moderation_log_channel_docs.py 把这条钉住）。
    log_channel_id: int = -1004337744233
    # 证据卡上的「人工放行 / 确认封禁」必须**按两次**才生效：两次点击的间隔必须
    # <= 该窗口（秒），第一次点击只 arm（落库 pending_action/pending_at），第二次
    # 同键点击才真正执行。窗口过期后重新按两次。默认 300 秒。
    review_confirm_seconds: int = 300


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    super_admin_id: int = 0
    config_master_key: str = ""
    miniapp_public_base_url: str = ""
    miniapp_listen_host: str = ""
    miniapp_listen_port: int = 0
    webhook_url: str = ""
    webhook_secret: str = ""

    @field_validator("super_admin_id", mode="before")
    @classmethod
    def _empty_env_int_as_zero(cls, value: object) -> object:
        # .env templates ship these as blank lines; treat "" as unset.
        if isinstance(value, str) and not value.strip():
            return 0
        return value

    # Role -> provider profile name + model.
    main_provider_name: str = ""
    main_model: str = "gemini-2.0-flash"
    main_fallbacks: str = ""
    main_timeout_sec: float = 12.0
    # Total budget across all attempts and fallbacks; 0 = built-in default.
    main_total_deadline_sec: float = 0.0

    vision_provider_name: str = ""
    vision_model: str = ""
    vision_fallbacks: str = ""
    vision_timeout_sec: float = 15.0
    vision_total_deadline_sec: float = 0.0

    decision_provider_name: str = ""
    decision_model: str = ""
    decision_fallbacks: str = ""
    decision_timeout_sec: float = 6.0
    decision_total_deadline_sec: float = 0.0

    moderation_provider_name: str = ""
    moderation_model: str = ""
    moderation_fallbacks: str = ""
    moderation_timeout_sec: float = 8.0
    moderation_total_deadline_sec: float = 0.0

    compress_provider_name: str = ""
    compress_model: str = ""
    compress_fallbacks: str = ""
    compress_timeout_sec: float = 12.0
    compress_total_deadline_sec: float = 0.0

    embed_provider_name: str = ""
    embed_model: str = "text-embedding-004"
    embed_fallbacks: str = ""
    embed_timeout_sec: float = 10.0
    embed_total_deadline_sec: float = 0.0
    llm_retry_attempts: int = 2
    llm_retry_backoff_sec: float = 0.8
    llm_retry_timeout_multiplier: float = 1.35

    max_context_tokens: int = 278528
    # Kept in sync with ``bot.context_window_mode``; ``auto`` = match the model.
    context_window_mode: Literal["auto", "fixed"] = "auto"
    # Business budget knobs (kept in sync with ``bot.*``): total window / output
    # reserve / group-history row cap.  Defaults are the recommended values; the
    # runtime config is authoritative and explicit values are never truncated.
    context_budget_tokens: int = 278528
    context_reserve_tokens: int = 32768
    group_history_max_messages: int = 1000
    # Group summary knobs (kept in sync with ``bot.*``); the runtime config wins.
    # ``GROUP_SUMMARY_SETTING_NAMES`` below is the single list used by the
    # legacy TOML loader and the top-level -> ``bot`` sync.
    group_summary_enabled: bool = False
    group_summary_recent_raw_messages: int = 200
    group_summary_max_tokens: int = 4096
    group_summary_batch_max_messages: int = 200
    group_summary_batch_max_input_tokens: int = 16_384
    group_summary_global_concurrency: int = 2
    group_summary_per_group_concurrency: int = 1
    group_summary_deadline_seconds: float = 15.0
    group_summary_queue_wait_seconds: float = 30.0
    group_summary_min_refresh_seconds: float = 60.0
    group_summary_failure_backoff_seconds: float = 60.0
    group_summary_failure_backoff_max_seconds: float = 3600.0
    group_summary_pending_capacity: int = 1000
    group_summary_trigger_messages: int = 200
    group_summary_trigger_budget_ratio: float = 0.85
    max_output_tokens: int = 2048
    bot_inbound_debounce_seconds: float = 5.0
    bot_reply_batch_timeout_seconds: float = 45.0
    bot_enable_typing: bool = True
    bot_enable_streaming: bool = True
    bot_enable_rich_messages: bool = True
    bot_stream_chunk_size: int = 36
    bot_stream_edit_interval_sec: float = 1.0
    bot_auto_delete_seconds: int = 0
    # Legacy one-time migration input. Runtime settings use seconds.
    bot_auto_delete_minutes: int = 0
    bot_decision_context_items: int = 5
    bot_memory_recent_messages: int = 500
    bot_memory_retention_days: int = 7
    bot_memory_archive_max_messages_per_group: int = 50000
    bot_memory_recall_enabled: bool = True
    bot_memory_recall_max_results: int = 8
    bot_memory_automatic_compaction: bool = False
    # Private-chat history (persisted rows + token-budget assembly).
    bot_private_chat_history_token_budget: int = 278528
    bot_private_chat_history_retention_days: int = 30
    # Group-chat history (archive rows assembled by token budget).
    bot_group_history_token_budget: int = 278528
    bot_group_history_reserve_tokens: int = 32768
    # 第 3 期：检索留档保留期 / 各类新鲜窗口 / 私聊→群方向开关（默认关闭）
    bot_search_record_retention_days: int = 30
    bot_search_freshness_price_hours: int = 24
    bot_search_freshness_news_hours: int = 48
    bot_search_freshness_fact_hours: int = 168
    bot_group_can_read_private_history: bool = False
    # 第 4 期：长期记忆（user_facts）提炼/工具/召回/过期/留存
    bot_memory_facts_enabled: bool = True
    bot_memory_extract_enabled: bool = True
    bot_memory_extract_interval_minutes: int = 30
    bot_memory_extract_min_messages: int = 20
    bot_memory_extract_daily_cap: int = 48
    bot_memory_extract_batch_max: int = 200
    bot_memory_tool_enabled: bool = True
    bot_memory_tool_daily_cap: int = 30
    bot_memory_recall_limit: int = 8
    bot_memory_event_ttl_days: int = 30
    bot_memory_deleted_retention_days: int = 30
    bot_proactive_default_enabled: bool = False
    bot_proactive_idle_minutes: int = 180
    bot_proactive_jitter_minutes: int = 60
    bot_proactive_check_interval_seconds: float = 60.0
    bot_proactive_quiet_hours_start: int = 0
    bot_proactive_quiet_hours_end: int = 9
    bot_proactive_retry_minutes: int = 30
    skill_sticker_file_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    doubao_tts_enabled: bool = False
    doubao_tts_http_timeout_sec: float = 20.0
    doubao_tts_max_text_length: int = 500
    doubao_tts_api_base: str = "https://openspeech.bytedance.com"
    doubao_tts_app_id: str = ""
    doubao_tts_app_key: str = ""
    doubao_tts_access_key: str = ""
    doubao_tts_resource_id: str = "seed-tts-2.0"
    doubao_tts_model: str = ""
    doubao_tts_speaker: str = ""
    doubao_tts_audio_format: str = "ogg_opus"
    doubao_tts_sample_rate: int = 48000
    doubao_tts_bit_rate: int = 96000
    doubao_tts_emotion: str = ""
    doubao_tts_emotion_scale: int = 4
    doubao_tts_speech_rate: int = 0
    doubao_tts_loudness_rate: int = 0
    doubao_tts_silence_duration_ms: int = 0

    music_api_enabled: bool = True
    music_api_http_timeout_sec: float = 15.0
    music_api_base_url: str = "https://music-api.gdstudio.xyz/api.php"
    music_api_default_source: str = "kuwo"
    music_api_stable_sources: str = "kuwo,netease,joox,bilibili"

    movie_info_enabled: bool = False
    movie_info_http_timeout_sec: float = 6.0
    movie_info_max_results: int = 6
    movie_info_default_language: str = "zh-CN"
    movie_info_default_region: str = "CN"
    movie_info_tmdb_read_access_token: str = ""
    movie_info_imdb_data_set_id: str = ""
    movie_info_imdb_revision_id: str = ""
    movie_info_imdb_asset_id: str = ""
    movie_info_imdb_api_key: str = ""
    movie_info_imdb_aws_access_key_id: str = ""
    movie_info_imdb_aws_secret_access_key: str = ""
    movie_info_imdb_aws_session_token: str = ""

    # Firecrawl-backed web search (websearch skill backend).
    firecrawl_api_key: str = ""
    firecrawl_api_base: str = "https://api.firecrawl.dev"
    firecrawl_timeout_sec: float = 20.0
    firecrawl_search_timeout_sec: float = 18.0

    av_enabled: bool = True
    av_http_timeout_sec: float = 15.0
    av_max_results: int = 18
    #: 私聊里封面之后补发的样例图张数（默认 4、硬上限 5、0=关闭）。
    av_dm_sample_count: int = 4
    #: 私聊详情里内联的下载地址条数（默认 3、硬上限 5、0=关闭该块）。
    av_inline_seed_count: int = 3
    #: 私聊详情里的 AI「题材与看点概述」；默认关（开启会调用一次 LLM）。
    av_ai_synopsis_enabled: bool = False
    av_javbus_base_url: str = "https://www.javbus.com"
    av_madouqu_base_url: str = "https://madouqu.com"
    av_dmm_base_url: str = "https://www.dmm.co.jp"
    av_fc2_base_url: str = "https://adult.contents.fc2.com"
    #: 识图之前先用第三方**帧级**索引反查番号（画面截图命中率高；封面仍旧走读文字）。
    #: F-025：默认**关闭**。开启后每张待识别图片的原始字节都会以 multipart POST
    #: 因此必须由运维显式打开，且必须同时显式配置 endpoint。
    #: 第三方反查入口。F-025：不再内置默认值（原来写死 https://avscan.cc/search），
    #: 空 = 未配置 = 不外发；这样"没人做过决定"的部署默认不会泄漏用户图片。
    #: 相似度阈值：实测真命中 ≥90%、假候选 ≤77%。

    # 入群验证：新成员先全员禁言，私聊 bot 获取链接并通过
    # 通过所选真人验证服务后恢复权限。
    join_verification_enabled: bool = False
    join_verification_timeout_seconds: int = 600
    join_verification_check_interval_seconds: float = 30.0
    join_verification_provider: Literal[
        "turnstile", "hcaptcha", "turnstile_hcaptcha"
    ] = "turnstile"
    join_verification_turnstile_site_key: str = ""
    join_verification_turnstile_secret_key: str = ""
    join_verification_hcaptcha_site_key: str = ""
    join_verification_hcaptcha_secret_key: str = ""
    # 验证页面对外可访问的地址（反代/隧道后的 https 地址）。
    join_verification_public_base_url: str = ""
    # 默认只监听回环：这个 HTTP 服务承载 /verify、/settings 与**免鉴权**的
    # /healthz，监听 0.0.0.0 会把它暴露给同网段/公网的任何主机（F-017）。
    # 容器部署需要在容器内被宿主机端口映射命中，可显式设
    # MINIAPP_LISTEN_HOST=0.0.0.0（docker-compose 已代劳），启动日志会给出
    # 明确的对外暴露告警。
    join_verification_listen_host: str = "127.0.0.1"
    join_verification_listen_port: int = 8480

    # 资料自动巡检：按名单批量复查所有已知成员的名字/简介。
    patrol_enabled: bool = False
    patrol_schedule_time: str = "04:30"
    patrol_batch_size: int = 500
    patrol_batch_pause_seconds: float = 5.0
    patrol_fetch_bio: bool = True
    patrol_challenge_timeout_seconds: int = 600
    patrol_check_interval_seconds: float = 60.0

    # 爆破防护：短窗口内大量入群自动锁群，并追溯质询爆破前入群的成员。
    raid_guard_enabled: bool = False
    raid_guard_pin_message: bool = True
    raid_guard_join_threshold: int = 8
    raid_guard_window_seconds: int = 60
    raid_guard_lockdown_seconds: int = 600
    raid_guard_lookback_seconds: int = 300
    raid_guard_challenge_timeout_seconds: int = 600

    # 呼叫管理员：群成员发送 @admin 时 @ 全部（或选定）群管理员。
    call_admin_enabled: bool = True
    call_admin_pin_message: bool = False
    call_admin_cooldown_seconds: int = 60

    # 骚扰民主投票封禁：回复消息发起投票，达到阈值即封禁被回复用户。
    vote_ban_enabled: bool = False
    vote_ban_pin_message: bool = True
    vote_ban_threshold: int = 5
    vote_ban_duration_seconds: int = 1800
    vote_ban_trigger_limit: int = 3
    vote_ban_trigger_window_seconds: int = 3600

    bot: BotConfig = BotConfig()
    moderation: ModerationConfig = ModerationConfig()


# Common vendor names people type that map onto litellm's native provider ids.
_PROVIDER_ALIASES = {
    "google": "gemini",
    "claude": "anthropic",
    "minimaxi": "minimax",
    "kimi": "moonshot",
    "moonshotai": "moonshot",
    "doubao": "volcengine",
    "ark": "volcengine",
    "qwen": "dashscope",
    "alibaba": "dashscope",
    "grok": "xai",
}


def _canonical_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _litellm_supports_provider(provider: str) -> bool:
    try:
        import litellm

        known = {str(getattr(item, "value", item)) for item in litellm.provider_list}
    except Exception:
        return True
    return provider in known


def _build_litellm_model(provider: str, model: str, *, api_base: str | None = None) -> str:
    """Build LiteLLM model string from provider + model name.

    Most native LiteLLM providers, including `anthropic`, use `<provider>/<model>`.
    OpenAI-compatible gateways — explicit (`openai_compatible`) or implied by a
    provider name litellm has no native adapter for while a custom api_base is
    configured — are normalized to the `openai/<model>` prefix.
    """
    provider_norm = _canonical_provider(provider)
    model_norm = (model or "").strip()
    if not model_norm:
        return model_norm
    if "/" in model_norm:
        return model_norm
    if provider_norm == "openai_compatible":
        return f"openai/{model_norm}"
    if (
        provider_norm
        and str(api_base or "").strip()
        and not _litellm_supports_provider(provider_norm)
    ):
        return f"openai/{model_norm}"
    if provider_norm:
        return f"{provider_norm}/{model_norm}"
    return model_norm


def _load_raw_env(env_file: str = ".env") -> dict[str, str]:
    """Load env vars from .env + process env, process env takes precedence."""
    file_vars: dict[str, str] = {}
    p = Path(env_file)
    if p.exists():
        loaded = dotenv_values(p)
        file_vars = {k: str(v) for k, v in loaded.items() if k and v is not None}
    merged = {**file_vars, **os.environ}
    return {k.upper(): str(v) for k, v in merged.items() if k}


def _env_truthy(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on", "y", "t"}


_API_BASE_SUFFIX_RULES: tuple[
    tuple[str, str, str, Literal["chat_completions", "responses"]],
    ...,
] = (
    ("/v1/chat/completions", "/chat/completions", "openai", "chat_completions"),
    ("/chat/completions", "/chat/completions", "openai", "chat_completions"),
    ("/v1/responses", "/responses", "openai", "responses"),
    ("/responses", "/responses", "openai", "responses"),
    ("/v1/messages", "/v1/messages", "anthropic", "chat_completions"),
    ("/messages", "/messages", "anthropic", "chat_completions"),
    ("/v1beta/models", "/models", "gemini", "chat_completions"),
    ("/v1/models", "/models", "gemini", "chat_completions"),
)


def _normalize_chat_endpoint(provider: str, raw_value: str | None) -> Literal["chat_completions", "responses"]:
    provider_norm = (provider or "").strip().lower()
    value = (raw_value or "").strip().lower()

    if not value:
        return "responses" if provider_norm == "openai" else "chat_completions"
    if value in {"/chat/completions", "chat/completions", "chat_completions", "chat-completions", "chat"}:
        return "chat_completions"
    if value in {"/responses", "responses", "response"}:
        if provider_norm not in {"openai", "openai_compatible"}:
            raise ValueError(
                f"MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT=/responses is only supported for openai/openai_compatible, got {provider!r}"
            )
        return "responses"
    raise ValueError(
        f"invalid MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT value: {raw_value!r}; expected /chat/completions or /responses"
    )


def _normalize_api_base(raw_value: str | None) -> str | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _default_endpoint_path(provider: str, chat_endpoint: Literal["chat_completions", "responses"]) -> str:
    provider_norm = (provider or "").strip().lower()
    if provider_norm == "anthropic":
        return "/v1/messages"
    if provider_norm == "gemini":
        return "/v1beta/models"
    if chat_endpoint == "responses":
        return "/responses"
    return "/chat/completions"


def _infer_provider_profile_from_api_base(
    provider: str,
    raw_api_base: str | None,
) -> tuple[str, str | None, Literal["chat_completions", "responses"] | None, str | None]:
    provider_norm = _canonical_provider(provider)
    api_base = _normalize_api_base(raw_api_base)
    if not api_base:
        return provider_norm, None, None, None

    lower_base = api_base.lower()
    for match_suffix, strip_suffix, inferred_provider, inferred_endpoint in _API_BASE_SUFFIX_RULES:
        if not lower_base.endswith(match_suffix):
            continue
        normalized_base = api_base[: -len(strip_suffix)].rstrip("/") or None
        if inferred_provider == "openai" and provider_norm == "openai_compatible":
            return provider_norm, normalized_base, inferred_endpoint, match_suffix
        return inferred_provider, normalized_base, inferred_endpoint, match_suffix

    return provider_norm, api_base, None, None


def _resolve_provider_profile(
    provider: str,
    raw_api_base: str | None,
    raw_chat_endpoint: str | None,
) -> tuple[str, str | None, Literal["chat_completions", "responses"], str]:
    effective_provider, api_base, inferred_endpoint, inferred_path = _infer_provider_profile_from_api_base(
        provider,
        raw_api_base,
    )
    if inferred_endpoint is not None:
        if raw_chat_endpoint:
            explicit_endpoint = _normalize_chat_endpoint(effective_provider, raw_chat_endpoint)
            if explicit_endpoint != inferred_endpoint:
                raise ValueError(
                    "MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT conflicts with the endpoint suffix in "
                    f"MODEL_PROVIDER_<NAME>_API_BASE: {raw_chat_endpoint!r} vs {raw_api_base!r}"
                )
        return effective_provider, api_base, inferred_endpoint, inferred_path or _default_endpoint_path(
            effective_provider,
            inferred_endpoint,
        )
    chat_endpoint = _normalize_chat_endpoint(effective_provider, raw_chat_endpoint)
    return effective_provider, api_base, chat_endpoint, _default_endpoint_path(effective_provider, chat_endpoint)


def _collect_provider_profiles(raw_env: dict[str, str]) -> dict[str, ProviderProfile]:
    """
    Parse provider profiles from env:
    MODEL_PROVIDER_<NAME>_PROVIDER
    MODEL_PROVIDER_<NAME>_API_KEY
    MODEL_PROVIDER_<NAME>_API_BASE
    MODEL_PROVIDER_<NAME>_STREAM
    MODEL_PROVIDER_<NAME>_CHAT_ENDPOINT
    """
    pattern = re.compile(
        r"^MODEL_PROVIDER_([A-Z0-9_]+)_(PROVIDER|API_KEY|API_BASE|STREAM|CHAT_ENDPOINT)$"
    )
    grouped: dict[str, dict[str, str]] = {}

    for key, value in raw_env.items():
        m = pattern.match(key)
        if not m:
            continue
        name = m.group(1).lower()
        field = m.group(2).lower()
        grouped.setdefault(name, {})[field] = (value or "").strip()

    profiles: dict[str, ProviderProfile] = {}
    for name, fields in grouped.items():
        provider = (fields.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError(f"MODEL_PROVIDER_{name.upper()}_PROVIDER is required")
        api_key = (fields.get("api_key") or "").strip() or None
        effective_provider, api_base, chat_endpoint, endpoint_path = _resolve_provider_profile(
            provider,
            fields.get("api_base"),
            fields.get("chat_endpoint"),
        )
        profiles[name] = ProviderProfile(
            provider=effective_provider,
            api_key=api_key,
            api_base=api_base,
            stream=_env_truthy(fields.get("stream")),
            chat_endpoint=chat_endpoint,
            endpoint_path=endpoint_path,
        )

    return profiles


def _parse_fallbacks(raw: str) -> list[tuple[str, str | None]]:
    """
    Parse fallback specs:
    "name1:model1,name2:model2,name3"
    """
    specs: list[tuple[str, str | None]] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        if ":" in token:
            provider_name, model_name = token.split(":", 1)
            provider_name = provider_name.strip().lower()
            model_name = model_name.strip() or None
        else:
            provider_name = token.strip().lower()
            model_name = None
        if not provider_name:
            raise ValueError(f"invalid fallback item: {item!r}")
        specs.append((provider_name, model_name))
    return specs


def _get_profile(profiles: dict[str, ProviderProfile], provider_name: str) -> ProviderProfile:
    key = (provider_name or "").strip().lower()
    if not key:
        raise ValueError("provider name cannot be empty")
    profile = profiles.get(key)
    if not profile:
        raise ValueError(f"provider profile not found: {provider_name}")
    return profile


def _build_chat_config(
    *,
    profiles: dict[str, ProviderProfile],
    provider_name: str,
    model_name: str,
    temperature: float,
    max_tokens: int,
    timeout_sec: float,
    retry_attempts: int,
    retry_backoff_sec: float,
    retry_timeout_multiplier: float,
    fallback_spec: str,
    total_deadline_sec: float = 0.0,
    request_params: Mapping[str, Any] | None = None,
    fallback_request_params: Sequence[Mapping[str, Any] | None] | None = None,
) -> ModelConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    profile = _get_profile(profiles, provider_name)
    resolved_model = _build_litellm_model(
        profile.provider,
        model_name,
        api_base=profile.api_base,
    )
    cfg = ModelConfig(
        model=resolved_model,
        provider=profile.provider,
        api_key=profile.api_key,
        api_base=profile.api_base,
        stream=profile.stream,
        chat_endpoint=profile.chat_endpoint,
        endpoint_path=profile.endpoint_path,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        retry_attempts=retry_attempts,
        retry_backoff_sec=retry_backoff_sec,
        retry_timeout_multiplier=retry_timeout_multiplier,
        total_deadline_sec=max(0.0, total_deadline_sec),
        # Parameters belong to this concrete primary model.  They are never
        # inherited by fallback endpoints.
        request_params=dict(request_params or {}),
        fallbacks=[],
    )

    for index, (fb_provider_name, fb_model_name) in enumerate(_parse_fallbacks(fallback_spec)):
        fb_profile = _get_profile(profiles, fb_provider_name)
        fallback_model_name = fb_model_name or model_name
        fallback_model = _build_litellm_model(
            fb_profile.provider,
            fallback_model_name,
            api_base=fb_profile.api_base,
        )
        params = {}
        if fallback_request_params is not None and index < len(fallback_request_params):
            params = dict(fallback_request_params[index] or {})
        cfg.fallbacks.append(
            ChatEndpointConfig(
                model=fallback_model,
                provider=fb_profile.provider,
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
                stream=fb_profile.stream,
                chat_endpoint=fb_profile.chat_endpoint,
                endpoint_path=fb_profile.endpoint_path,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                retry_attempts=retry_attempts,
                retry_backoff_sec=retry_backoff_sec,
                retry_timeout_multiplier=retry_timeout_multiplier,
                total_deadline_sec=max(0.0, total_deadline_sec),
                request_params=params,
            )
        )
    return cfg


def _build_embed_config(
    *,
    profiles: dict[str, ProviderProfile],
    provider_name: str,
    model_name: str,
    timeout_sec: float,
    retry_attempts: int,
    retry_backoff_sec: float,
    retry_timeout_multiplier: float,
    fallback_spec: str,
    total_deadline_sec: float = 0.0,
    request_params: Mapping[str, Any] | None = None,
    fallback_request_params: Sequence[Mapping[str, Any] | None] | None = None,
) -> EmbedConfig:
    if not provider_name:
        raise ValueError("provider name is required")
    if not model_name:
        raise ValueError("model name is required")

    profile = _get_profile(profiles, provider_name)
    cfg = EmbedConfig(
        model=_build_litellm_model(profile.provider, model_name, api_base=profile.api_base),
        provider=profile.provider,
        api_key=profile.api_key,
        api_base=profile.api_base,
        endpoint_path=profile.endpoint_path,
        timeout_sec=timeout_sec,
        retry_attempts=retry_attempts,
        retry_backoff_sec=retry_backoff_sec,
        retry_timeout_multiplier=retry_timeout_multiplier,
        total_deadline_sec=max(0.0, total_deadline_sec),
        request_params=dict(request_params or {}),
        fallbacks=[],
    )

    for index, (fb_provider_name, fb_model_name) in enumerate(_parse_fallbacks(fallback_spec)):
        fb_profile = _get_profile(profiles, fb_provider_name)
        params = {}
        if fallback_request_params is not None and index < len(fallback_request_params):
            params = dict(fallback_request_params[index] or {})
        cfg.fallbacks.append(
            EmbedEndpointConfig(
                model=_build_litellm_model(
                    fb_profile.provider,
                    fb_model_name or model_name,
                    api_base=fb_profile.api_base,
                ),
                provider=fb_profile.provider,
                api_key=fb_profile.api_key,
                api_base=fb_profile.api_base,
                endpoint_path=fb_profile.endpoint_path,
                timeout_sec=timeout_sec,
                retry_attempts=retry_attempts,
                retry_backoff_sec=retry_backoff_sec,
                retry_timeout_multiplier=retry_timeout_multiplier,
                total_deadline_sec=max(0.0, total_deadline_sec),
                request_params=params,
            )
        )
    return cfg


def load_settings(config_path: str = "config.toml") -> Settings:
    """Load settings from TOML config + .env."""
    toml_data: dict = {}
    p = Path(config_path)
    if p.exists():
        with open(p, "rb") as f:
            toml_data = tomllib.load(f)

    settings = Settings()

    # Apply TOML overrides.
    if "bot" in toml_data:
        bot_data = toml_data["bot"]
        if "main_model" in bot_data:
            settings.bot.main_model = ModelConfig(**bot_data["main_model"])
        if "vision_model" in bot_data:
            settings.bot.vision_model = ModelConfig(**bot_data["vision_model"])
        if "decision_model" in bot_data:
            settings.bot.decision_model = ModelConfig(**bot_data["decision_model"])
        if "moderation_model" in bot_data:
            settings.bot.moderation_model = ModelConfig(**bot_data["moderation_model"])
        if "compress_model" in bot_data:
            settings.bot.compress_model = ModelConfig(**bot_data["compress_model"])
        if "embed_model" in bot_data:
            settings.bot.embed_model = EmbedConfig(**bot_data["embed_model"])
        if "parse_mode" in bot_data:
            settings.bot.parse_mode = bot_data["parse_mode"]
        if "disable_link_preview" in bot_data:
            settings.bot.disable_link_preview = bool(
                bot_data["disable_link_preview"]
            )
        if "drop_pending_updates" in bot_data:
            settings.bot.drop_pending_updates = bot_data["drop_pending_updates"]
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
            # 第 4 期：长期记忆（见 BotConfig 的字段说明）
            "memory_extract_interval_minutes",
            "memory_extract_min_messages",
            "memory_extract_daily_cap",
            "memory_extract_batch_max",
            "memory_tool_daily_cap",
            "memory_recall_limit",
            "memory_event_ttl_days",
            "memory_deleted_retention_days",
            # B-39 / P4-9：台账保留期（只在 [bot] 段可配，不进 runtime_config/UI）。
            "memory_extract_ledger_retention_days",
        ):
            if key in bot_data:
                setattr(settings.bot, key, int(bot_data[key]))
        for key in (
            "memory_recall_enabled",
            "memory_automatic_compaction",
            "group_can_read_private_history",
            # 第 4 期：长期记忆的两个开关
            "memory_facts_enabled",
            "memory_extract_enabled",
            "memory_tool_enabled",
        ):
            if key in bot_data:
                setattr(settings.bot, key, bool(bot_data[key]))
        # 上下文模式：``auto``（自动发现模型窗口，默认）/ ``fixed``（不查元数据，兼容旧配置）
        mode = str(bot_data.get("context_window_mode") or "").strip().lower()
        if mode in {"auto", "fixed"}:
            settings.bot.context_window_mode = mode

    if "moderation" in toml_data:
        settings.moderation = ModerationConfig(**toml_data["moderation"])
    settings.moderation.high_confidence_threshold = min(
        1.0, max(0.0, float(settings.moderation.high_confidence_threshold))
    )
    settings.moderation.challenge_timeout_seconds = max(
        60, int(settings.moderation.challenge_timeout_seconds)
    )
    settings.moderation.bot_screening_message_count = min(
        100, max(1, int(settings.moderation.bot_screening_message_count))
    )
    settings.moderation.punish_quoted_author_enabled = bool(
        settings.moderation.punish_quoted_author_enabled
    )
    settings.moderation.quoted_author_max_age_seconds = max(
        0, int(settings.moderation.quoted_author_max_age_seconds)
    )
    settings.moderation.admin_moderation_enabled = bool(
        settings.moderation.admin_moderation_enabled
    )
    settings.moderation.admin_alert_super_admin_enabled = bool(
        settings.moderation.admin_alert_super_admin_enabled
    )
    settings.moderation.log_channel_enabled = bool(
        settings.moderation.log_channel_enabled
    )
    settings.moderation.log_channel_id = int(
        settings.moderation.log_channel_id or 0
    )
    settings.moderation.review_confirm_seconds = max(
        1, int(settings.moderation.review_confirm_seconds)
    )

    settings.bot.token = settings.bot_token
    settings.bot.inbound_debounce_seconds = max(0.0, float(settings.bot_inbound_debounce_seconds))
    if "bot_reply_batch_timeout_seconds" in getattr(settings, "model_fields_set", set()):
        settings.bot.reply_batch_timeout_seconds = min(
            120.0,
            max(5.0, float(settings.bot_reply_batch_timeout_seconds)),
        )
    settings.bot.enable_typing = settings.bot_enable_typing
    settings.bot.enable_streaming = settings.bot_enable_streaming
    settings.bot.enable_rich_messages = settings.bot_enable_rich_messages
    settings.bot.stream_chunk_size = max(8, settings.bot_stream_chunk_size)
    settings.bot.stream_edit_interval_sec = max(0.3, settings.bot_stream_edit_interval_sec)
    configured_auto_delete_seconds = int(settings.bot_auto_delete_seconds or 0)
    explicit_seconds = "bot_auto_delete_seconds" in getattr(
        settings, "model_fields_set", set()
    )
    if not explicit_seconds and configured_auto_delete_seconds <= 0 and settings.bot_auto_delete_minutes > 0:
        configured_auto_delete_seconds = int(settings.bot_auto_delete_minutes) * 60
    settings.bot.auto_delete_seconds = max(0, configured_auto_delete_seconds)
    settings.bot.auto_delete_minutes = settings.bot.auto_delete_seconds // 60
    settings.bot.decision_context_items = min(20, max(0, settings.bot_decision_context_items))
    settings.bot.memory_recent_messages = min(
        2000, max(50, int(settings.bot_memory_recent_messages))
    )
    settings.bot.memory_retention_days = min(
        365, max(1, int(settings.bot_memory_retention_days))
    )
    settings.bot.private_chat_history_token_budget = min(
        2_000_000,
        max(1024, int(settings.bot_private_chat_history_token_budget)),
    )
    settings.bot.private_chat_history_retention_days = min(
        365, max(1, int(settings.bot_private_chat_history_retention_days))
    )
    settings.bot.group_history_token_budget = min(
        2_000_000,
        max(1024, int(settings.bot_group_history_token_budget)),
    )
    settings.bot.group_history_reserve_tokens = min(
        1_000_000,
        max(1024, int(settings.bot_group_history_reserve_tokens)),
    )
    # 第 3 期：检索留档保留期与新鲜窗口（与 runtime_config 的 ge/le 一致）
    settings.bot.search_record_retention_days = min(
        365, max(1, int(settings.bot_search_record_retention_days))
    )
    settings.bot.search_freshness_price_hours = min(
        8760, max(1, int(settings.bot_search_freshness_price_hours))
    )
    settings.bot.search_freshness_news_hours = min(
        8760, max(1, int(settings.bot_search_freshness_news_hours))
    )
    settings.bot.search_freshness_fact_hours = min(
        8760, max(1, int(settings.bot_search_freshness_fact_hours))
    )
    settings.bot.group_can_read_private_history = bool(
        settings.bot_group_can_read_private_history
    )
    # 第 4 期：长期记忆（与 runtime_config 的 ge/le 一致）
    settings.bot.memory_facts_enabled = bool(settings.bot_memory_facts_enabled)
    settings.bot.memory_extract_enabled = bool(settings.bot_memory_extract_enabled)
    settings.bot.memory_tool_enabled = bool(settings.bot_memory_tool_enabled)
    settings.bot.memory_extract_interval_minutes = min(
        1440, max(5, int(settings.bot_memory_extract_interval_minutes))
    )
    settings.bot.memory_extract_min_messages = min(
        500, max(5, int(settings.bot_memory_extract_min_messages))
    )
    settings.bot.memory_extract_daily_cap = min(
        500, max(0, int(settings.bot_memory_extract_daily_cap))
    )
    settings.bot.memory_extract_batch_max = min(
        1000, max(20, int(settings.bot_memory_extract_batch_max))
    )
    settings.bot.memory_tool_daily_cap = min(
        200, max(0, int(settings.bot_memory_tool_daily_cap))
    )
    settings.bot.memory_recall_limit = min(
        20, max(1, int(settings.bot_memory_recall_limit))
    )
    settings.bot.memory_event_ttl_days = min(
        365, max(1, int(settings.bot_memory_event_ttl_days))
    )
    settings.bot.memory_deleted_retention_days = min(
        365, max(1, int(settings.bot_memory_deleted_retention_days))
    )
    settings.bot.memory_archive_max_messages_per_group = min(
        1_000_000,
        max(1000, int(settings.bot_memory_archive_max_messages_per_group)),
    )
    settings.bot.memory_recall_enabled = bool(settings.bot_memory_recall_enabled)
    settings.bot.memory_recall_max_results = min(
        20, max(1, int(settings.bot_memory_recall_max_results))
    )
    settings.bot.memory_automatic_compaction = bool(
        settings.bot_memory_automatic_compaction
    )
    settings.bot.proactive_default_enabled = settings.bot_proactive_default_enabled
    settings.bot.proactive_idle_minutes = max(180, int(settings.bot_proactive_idle_minutes))
    settings.bot.proactive_jitter_minutes = max(0, int(settings.bot_proactive_jitter_minutes))
    settings.bot.proactive_check_interval_seconds = max(
        15.0, float(settings.bot_proactive_check_interval_seconds)
    )
    settings.bot.proactive_quiet_hours_start = min(23, max(0, int(settings.bot_proactive_quiet_hours_start)))
    settings.bot.proactive_quiet_hours_end = min(23, max(0, int(settings.bot_proactive_quiet_hours_end)))
    settings.bot.proactive_retry_minutes = max(5, int(settings.bot_proactive_retry_minutes))

    settings.join_verification_timeout_seconds = max(
        60, int(settings.join_verification_timeout_seconds)
    )
    settings.join_verification_check_interval_seconds = max(
        5.0, float(settings.join_verification_check_interval_seconds)
    )
    if settings.join_verification_provider not in {
        "turnstile",
        "hcaptcha",
        "turnstile_hcaptcha",
    }:
        settings.join_verification_provider = "turnstile"
    settings.join_verification_listen_port = min(
        65535, max(1, int(settings.join_verification_listen_port))
    )
    if settings.join_verification_enabled:
        challenge_keys: list[tuple[str, str]] = []
        if settings.join_verification_provider in {"hcaptcha", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    (
                        "JOIN_VERIFICATION_HCAPTCHA_SITE_KEY",
                        settings.join_verification_hcaptcha_site_key,
                    ),
                    (
                        "JOIN_VERIFICATION_HCAPTCHA_SECRET_KEY",
                        settings.join_verification_hcaptcha_secret_key,
                    ),
                )
            )
        if settings.join_verification_provider in {"turnstile", "turnstile_hcaptcha"}:
            challenge_keys.extend(
                (
                    (
                        "JOIN_VERIFICATION_TURNSTILE_SITE_KEY",
                        settings.join_verification_turnstile_site_key,
                    ),
                    (
                        "JOIN_VERIFICATION_TURNSTILE_SECRET_KEY",
                        settings.join_verification_turnstile_secret_key,
                    ),
                )
            )
        missing = [
            name
            for name, value in (
                *challenge_keys,
                ("JOIN_VERIFICATION_PUBLIC_BASE_URL", settings.join_verification_public_base_url),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "JOIN_VERIFICATION_ENABLED=true requires " + ", ".join(missing)
            )

    # New provider registry + role binding.
    raw_env = _load_raw_env()
    profiles = _collect_provider_profiles(raw_env)
    if not profiles:
        raise ValueError(
            "no provider profiles found, define MODEL_PROVIDER_<NAME>_PROVIDER/API_KEY/API_BASE in .env"
        )

    main_provider_name = settings.main_provider_name.strip().lower()
    if not main_provider_name:
        raise ValueError("MAIN_PROVIDER_NAME is required")
    main_model_name = settings.main_model.strip()
    if not main_model_name:
        raise ValueError("MAIN_MODEL is required")

    vision_provider_name = (settings.vision_provider_name or main_provider_name).strip().lower()
    decision_provider_name = (settings.decision_provider_name or main_provider_name).strip().lower()
    moderation_provider_name = (
        settings.moderation_provider_name or decision_provider_name or main_provider_name
    ).strip().lower()
    compress_provider_name = (settings.compress_provider_name or main_provider_name).strip().lower()
    embed_provider_name = (settings.embed_provider_name or main_provider_name).strip().lower()

    vision_model_name = (settings.vision_model or main_model_name).strip()
    decision_model_name = (settings.decision_model or main_model_name).strip()
    moderation_model_name = (settings.moderation_model or decision_model_name).strip()
    compress_model_name = (settings.compress_model or main_model_name).strip()
    embed_model_name = (settings.embed_model or "text-embedding-004").strip()

    settings.bot.main_model = _build_chat_config(
        profiles=profiles,
        provider_name=main_provider_name,
        model_name=main_model_name,
        temperature=settings.bot.main_model.temperature,
        max_tokens=max(1, settings.max_output_tokens),
        timeout_sec=max(1.0, float(settings.main_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.main_total_deadline_sec)),
        fallback_spec=settings.main_fallbacks,
        request_params=settings.bot.main_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.main_model.fallbacks
        ],
    )
    settings.bot.vision_model = _build_chat_config(
        profiles=profiles,
        provider_name=vision_provider_name,
        model_name=vision_model_name,
        temperature=settings.bot.vision_model.temperature,
        max_tokens=settings.bot.vision_model.max_tokens,
        timeout_sec=max(1.0, float(settings.vision_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.vision_total_deadline_sec)),
        fallback_spec=settings.vision_fallbacks,
        request_params=settings.bot.vision_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.vision_model.fallbacks
        ],
    )
    settings.bot.decision_model = _build_chat_config(
        profiles=profiles,
        provider_name=decision_provider_name,
        model_name=decision_model_name,
        temperature=settings.bot.decision_model.temperature,
        max_tokens=settings.bot.decision_model.max_tokens,
        timeout_sec=max(1.0, float(settings.decision_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.decision_total_deadline_sec)),
        fallback_spec=settings.decision_fallbacks,
        request_params=settings.bot.decision_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.decision_model.fallbacks
        ],
    )
    settings.bot.moderation_model = _build_chat_config(
        profiles=profiles,
        provider_name=moderation_provider_name,
        model_name=moderation_model_name,
        temperature=settings.bot.moderation_model.temperature,
        max_tokens=settings.bot.moderation_model.max_tokens,
        timeout_sec=max(1.0, float(settings.moderation_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.moderation_total_deadline_sec)),
        fallback_spec=settings.moderation_fallbacks,
        request_params=settings.bot.moderation_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.moderation_model.fallbacks
        ],
    )
    settings.bot.compress_model = _build_chat_config(
        profiles=profiles,
        provider_name=compress_provider_name,
        model_name=compress_model_name,
        temperature=settings.bot.compress_model.temperature,
        max_tokens=settings.bot.compress_model.max_tokens,
        timeout_sec=max(1.0, float(settings.compress_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.compress_total_deadline_sec)),
        fallback_spec=settings.compress_fallbacks,
        request_params=settings.bot.compress_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.compress_model.fallbacks
        ],
    )
    settings.bot.embed_model = _build_embed_config(
        profiles=profiles,
        provider_name=embed_provider_name,
        model_name=embed_model_name,
        timeout_sec=max(1.0, float(settings.embed_timeout_sec)),
        retry_attempts=max(1, int(settings.llm_retry_attempts)),
        retry_backoff_sec=max(0.0, float(settings.llm_retry_backoff_sec)),
        retry_timeout_multiplier=max(1.0, float(settings.llm_retry_timeout_multiplier)),
        total_deadline_sec=max(0.0, float(settings.embed_total_deadline_sec)),
        fallback_spec=settings.embed_fallbacks,
        request_params=settings.bot.embed_model.request_params,
        fallback_request_params=[
            item.request_params for item in settings.bot.embed_model.fallbacks
        ],
    )

    settings.bot.max_context_tokens = settings.max_context_tokens
    # ``context_window_mode`` 只在顶层（env / 顶层 TOML 键）显式设置时才覆盖 ``[bot]``
    # 的选择，否则 ``[bot] context_window_mode = "fixed"`` 会被默认值无声改回 auto。
    if "context_window_mode" in getattr(settings, "model_fields_set", set()):
        settings.bot.context_window_mode = settings.context_window_mode
    # 业务预算三项 + 第②项摘要的全部字段同理：顶层（env）显式设置才覆盖 ``[bot]``。
    apply_top_level_budget_overrides(settings)
    budget_error = validate_business_budget(
        settings.bot.context_budget_tokens,
        settings.bot.context_reserve_tokens,
    )
    if budget_error:
        raise ValueError(
            f"业务预算配置非法：{budget_error}"
            "（请修正 bot.context_budget_tokens / bot.context_reserve_tokens）"
        )
    settings.bot.max_output_tokens = settings.max_output_tokens
    settings.bot.main_model.max_tokens = max(1, settings.max_output_tokens)

    return settings


def load_bootstrap_settings() -> Settings:
    """Load only values required before the database-backed config is available.

    Runtime options are applied by ``RuntimeConfigManager`` after the database
    is initialized. Legacy join-verification web variables remain accepted so
    existing deployments can migrate without changing their reverse proxy in
    the same release.
    """
    settings = Settings()
    settings.bot.token = settings.bot_token.strip()

    public_base_url = (
        settings.miniapp_public_base_url
        or settings.join_verification_public_base_url
    ).strip().rstrip("/")
    listen_host = (
        settings.miniapp_listen_host
        or settings.join_verification_listen_host
        or "127.0.0.1"
    ).strip()
    listen_port = min(
        65535,
        max(
            1,
            int(
                settings.miniapp_listen_port
                or settings.join_verification_listen_port
                or 8480
            ),
        ),
    )

    settings.miniapp_public_base_url = public_base_url
    settings.miniapp_listen_host = listen_host
    settings.miniapp_listen_port = listen_port
    # Existing verification code consumes these aliases. They now describe the
    # shared Mini App server instead of verification-specific infrastructure.
    settings.join_verification_public_base_url = public_base_url
    settings.join_verification_listen_host = listen_host
    settings.join_verification_listen_port = listen_port
    return settings


def log_process_identity() -> None:
    """C3-01：把进程实际以谁的身份运行写进启动日志，root 时明确告警。

    ``Dockerfile`` 写了 ``USER app:app``，但 compose 的 ``user:`` 会覆盖它，而
    README 建议的 ``APP_UID="$(id -u)"`` 在 root 宿主上就是 0。compose 侧已改成
    「必须显式给出」（空值直接报错），但非零的 uid 也可能不是部署者想要的，所以
    这里始终打印一次实际身份。
    """

    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    uid = int(getuid()) if callable(getuid) else -1
    gid = int(getgid()) if callable(getgid) else -1
    if uid == 0:
        log.warning(
            "进程以 **root**(uid=0/gid=%d) 运行：镜像里的 `USER app:app` 被覆盖了"
            "（compose 的 user: 或容器运行参数）。容器内 root + 可写的 ./data 绑定卷"
            "+ 对外端口，请确认这是有意为之，否则显式设置 APP_UID/APP_GID。",
            gid,
        )
        return
    log.info("进程身份：uid=%d gid=%d（非 root）", uid, gid)


def log_enforcement_switch_state(settings: Settings) -> None:
    """F-024：把「对用户可见的执法开关」的生效状态写进启动日志。

    这三个开关都是 opt-in（默认关闭）。只要有一个是开启的，就用 WARNING 明确
    列出来，因为每一个都会直接改变群成员看到的行为；全部关闭时记一条 INFO，
    说明当前是旧版行为，运维想开启去哪里开。

    代码默认值（``ModerationConfig`` / ``ModerationSettingsConfig``）与这里的文案
    必须一致：默认关闭，"生产要开"是部署侧的动作，不是代码默认值。
    """

    moderation = getattr(settings, "moderation", None)
    switches = (
        ("nsfw_image_guard", "裸露/色情图片处置（删图 + 群内 @警告 + 质询）"),
        ("punish_quoted_author", "引用/转发广告连坐原作者"),
        ("admin_moderation", "管理员/群主不再整段豁免日常审核"),
    )
    enabled = [
        (name, label)
        for name, label in switches
        if moderation is not None
        and bool(getattr(moderation, f"{name}_enabled", False))
    ]
    if not enabled:
        log.info(
            "审核处置策略：三项新增执法开关全部关闭（默认 opt-in）："
            "nsfw_image_guard / punish_quoted_author / admin_moderation。"
            "需要开启请在 /settings 的审核设置里显式打开。"
        )
        return
    log.warning(
        "审核处置策略：以下对用户可见的执法开关已开启 → %s。"
        "这些都会改变群成员看到的行为，请确认是有意开启。",
        "；".join(f"{name}=on（{label}）" for name, label in enabled),
    )


def validate_bootstrap_settings(settings: Settings) -> None:
    """Reject deployments that cannot be administered or decrypt runtime secrets.

    ``start.py`` performs a friendly local preflight, but production containers
    invoke ``python -m bot`` directly.  Keep the authoritative validation in the
    application package so every entry point fails before opening the database or
    listening on the public Mini App server.
    """

    token = str(getattr(settings.bot, "token", "") or settings.bot_token or "").strip()
    if not token or token.lower() in {"your_bot_token_here", "replace_me", "changeme"}:
        raise ValueError("BOT_TOKEN is required and must not use a template placeholder")
    if int(settings.super_admin_id or 0) <= 0:
        raise ValueError("SUPER_ADMIN_ID must be a positive Telegram user ID")
    if not str(settings.config_master_key or "").strip():
        raise ValueError("CONFIG_MASTER_KEY is required and must remain stable")

    settings.bot.token = token
