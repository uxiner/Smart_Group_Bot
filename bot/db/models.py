from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.timezone import now_shanghai_naive


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # TG chat_id
    title: Mapped[str] = mapped_column(String(255), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    moderation_rules: Mapped[list[ModerationRule]] = relationship(back_populates="group")
    violations: Mapped[list[Violation]] = relationship(back_populates="group")
    context_summary: Mapped[GroupContextSummary | None] = relationship(back_populates="group", uselist=False)
    permanent_memories: Mapped[list[GroupPermanentMemory]] = relationship(back_populates="group")


class GroupApiModelQuerySecret(Base):
    """Encrypted OpenAI-compatible API credential scoped to one group."""

    __tablename__ = "group_api_model_query_secrets"

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GroupContextSummary(Base):
    __tablename__ = "group_context_summaries"

    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    group: Mapped[Group] = relationship(back_populates="context_summary")


class GroupSummary(Base):
    """后台群摘要（2026-10-04 第②项）：只加不改，与旧的热历史压缩完全独立。

    ``group_context_summaries`` 是第 2 期 legacy compaction 的表（会删除热历史），
    本表是**新方案**：原文/归档/私聊原文一条都不删，摘要只是"旧内容的低信任资料"，
    由前台按需读取。

    ``version`` 与 ``covered_through_key`` 一起做**原子发布**（CAS）：迟到任务的
    ``version`` 更小或覆盖水位更旧时不得覆盖新摘要；前台只读已发布的这一行。
    """

    __tablename__ = "group_summaries"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=0)
    #: 覆盖的归档消息范围：**行 id** 水位（单调、比较安全），key 只作人类可读展示。
    covered_from_id: Mapped[int] = mapped_column(Integer, default=0)
    covered_through_id: Mapped[int] = mapped_column(Integer, default=0)
    covered_from_key: Mapped[str] = mapped_column(String(128), default="")
    covered_through_key: Mapped[str] = mapped_column(String(128), default="")
    covered_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 源在被截断时不许声称"完整原文"（前台据此加免责声明）。
    source_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GroupPermanentMemory(Base):
    __tablename__ = "group_permanent_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    group: Mapped[Group] = relationship(back_populates="permanent_memories")


class ModerationRule(Base):
    __tablename__ = "moderation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    rule_type: Mapped[str] = mapped_column(String(32))  # keyword, regex, llm
    pattern: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(32), default="warn")  # warn, delete, ban
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 正则/关键词规则的扫描范围（组合值，'+' 连接）：
    #   ``message``            —— 只扫用户自己写的正文（默认）
    #   ``message+quote``      —— 再并入被引用/转发消息的正文
    #   ``message+vision``     —— 再并入机器人自己生成的图片描述（[image-vision]）
    #   ``message+quote+vision`` —— 三者都扫
    # 老数据/老规则一律按 ``message`` 处理（默认值即旧行为）。语义（llm）规则
    # 始终看到完整文本，不受该字段影响。
    scan_scope: Mapped[str] = mapped_column(
        String(32), default="message", server_default="message"
    )

    group: Mapped[Group] = relationship(back_populates="moderation_rules")

    __table_args__ = (
        Index("ix_moderation_rules_group_enabled_id", "group_id", "enabled", "id"),
    )


class KeywordReply(Base):
    """Per-group keyword auto replies, managed from the Mini App.

    match_type: "contains" (substring), "exact" (whole message), or "regex".
    Replies support the shared safe Markdown renderer and optional inline
    buttons; pin_message optionally pins the sent reply and auto_delete opts
    it into the global "keyword" retention category.
    """

    __tablename__ = "keyword_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    keyword: Mapped[str] = mapped_column(String(255), default="")
    match_type: Mapped[str] = mapped_column(String(16), default="contains")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    # Shared template-button schema: [{text, action, value, row, style?}, ...].
    # JSON keeps the feature extensible without a join table for a small,
    # ordered collection capped by the settings API.
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    pin_message: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete: Mapped[bool] = mapped_column(Boolean, default=True)
    disable_link_preview: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )


class ScheduledMessage(Base):
    """Per-group timed announcements, managed from the Mini App.

    schedule_type: "daily" fires once per day at HH:MM (Asia/Shanghai,
    schedule_time); "interval" fires every interval_minutes. next_run_at /
    last_run_at bookkeeping follows the patrol convention (compare against
    the computed due instant so restarts never double-fire).
    """

    __tablename__ = "scheduled_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    schedule_type: Mapped[str] = mapped_column(String(16), default="daily")
    schedule_time: Mapped[str] = mapped_column(String(5), default="09:00")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    pin_message: Mapped[bool] = mapped_column(Boolean, default=False)
    unpin_previous: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    disable_link_preview: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    # Python-side default keeps created_at in Asia/Shanghai naive time; the
    # due computation compares it against now_shanghai_naive().
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )


class ScheduledMessageOccurrence(Base):
    """Durable delivery attempt for one computed schedule occurrence.

    The unique occurrence key and live lease prevent concurrent workers from
    normally publishing the same due event. Delivery is intentionally
    at-least-once across process crashes or lease expiry: an old worker that
    loses its lease cannot complete the row, while a later worker may replay it.
    Failed attempts retain the row with exponential backoff until completion can
    advance ``ScheduledMessage.last_run_at`` atomically.
    """

    __tablename__ = "scheduled_message_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scheduled_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_scheduled_message_occurrence_unique",
            "scheduled_message_id",
            "occurrence_at",
            unique=True,
        ),
        Index(
            "ix_scheduled_message_occurrence_recovery",
            "next_attempt_at",
            "lease_until",
            "occurrence_at",
        ),
    )


class VoteBanSession(Base):
    """Democratic vote-ban: one active poll per (group, target).

    Any member replies to a message with /voteban to open a poll against the
    replied-to user; approvals at or above the per-group threshold ban the
    target in that group. The prompt message carries live buttons, so no
    auto-delete timer is scheduled while status is "active"; the finalized
    (edited) outcome notice joins the "vote" retention category. Expiry timers
    are in-memory (raid-guard convention): a restart drops them, but the next
    button press lazily finalizes an overdue session. Group admins may resolve
    a poll early through dedicated buttons; the resolution columns record who
    cancelled or banned so recovery and audit stay truthful after a crash.
    """

    __tablename__ = "vote_ban_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    target_display: Mapped[str] = mapped_column(String(255), default="")
    target_username: Mapped[str] = mapped_column(String(255), default="")
    starter_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    starter_display: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="command")
    target_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=5)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # Persist outstanding ownership of the bot-managed pin. It starts true only
    # when this poll requested pinning and is cleared after an exact unpin.
    # Old rows default false so an upgrade never manages pins it did not create.
    pin_message: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
    )
    # active / enforcing / passed / failed / expired / cancelled
    status: Mapped[str] = mapped_column(String(16), default="active")
    # "" = resolved by vote threshold / timeout; "admin_ban" / "admin_cancel"
    # = a group admin resolved the poll early via its admin buttons;
    # "manual_unban" = a newer explicit unban cancelled open enforcement;
    # "unban_finalized" marks its Telegram prompt as durably closed.
    resolution: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )
    resolver_user_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    resolver_display: Mapped[str] = mapped_column(
        String(255), default="", server_default=""
    )
    # Lease timestamp for the Telegram ban side effect.  If a worker dies
    # while status is ``enforcing``, another worker may safely retry the
    # idempotent ban after the lease becomes stale.
    enforcing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_vote_ban_group_status", "group_id", "status"),
        Index("ix_vote_ban_status_deadline", "status", "deadline_at"),
        # One open/enforcing poll per (group, target), enforced at the DB level so
        # two concurrent /voteban commands cannot both open a session.
        Index(
            "ix_vote_ban_open_target",
            "group_id",
            "target_user_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'enforcing')"),
            postgresql_where=text("status IN ('active', 'enforcing')"),
        ),
        {"sqlite_autoincrement": True},
    )


class VoteBanVote(Base):
    __tablename__ = "vote_ban_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vote_ban_sessions.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_vote_ban_vote_session_user", "session_id", "user_id", unique=True),
    )


class VoteBanQuotaBucket(Base):
    """Persistent fixed-window quota for opening democratic vote-ban polls."""

    __tablename__ = "vote_ban_quota_buckets"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
    )
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
    )


class BanAuditEvent(Base):
    """Append-only facts about ban/unban decisions and Telegram outcomes.

    ``group_id == 0`` denotes a global policy entry.  Current policy state
    remains in ``GlobalBan`` / ``UserWarning``; this table preserves who,
    why, source, and actual outcome for the bot's trusted knowledge context.
    """

    __tablename__ = "ban_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_display: Mapped[str] = mapped_column(String(255), default="")
    target_username: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(16), default="ban")
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    actor_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_display: Mapped[str] = mapped_column(String(255), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="succeeded")
    reference_type: Mapped[str] = mapped_column(String(32), default="")
    reference_id: Mapped[int] = mapped_column(BigInteger, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_ban_audit_group_created", "group_id", "created_at"),
        Index("ix_ban_audit_group_id_desc", "group_id", "id"),
        Index(
            "ix_ban_audit_group_target_created",
            "group_id",
            "target_user_id",
            "created_at",
        ),
        Index(
            "ix_ban_audit_reference",
            "reference_type",
            "reference_id",
        ),
    )


class Violation(Base):
    __tablename__ = "violations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    rule_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("moderation_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_text: Mapped[str] = mapped_column(Text, default="")
    action_taken: Mapped[str] = mapped_column(String(32), default="warn")
    # Telegram message ids are unique within a chat.  Durable webhook retries
    # can dispatch the same update more than once, so the pair below is the
    # stable idempotency key for every moderation side effect derived from one
    # source message.  NULL keeps legacy/manual rows unrestricted.
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 观测列：这一行对应的"累计命中次数"（含本次）。有两种来源，都取自现有
    # 现成计数，不另造数字：
    # - ban 计数路径（_apply_counted_moderation_ban）：add_warning 返回的
    #   UserWarning.count（本群+本用户，就是 warn_threshold 比较的那个计数器）。
    #   幂等重放靠"该列非 NULL"判断这条是否已经计过数，所以这条链路不能改口径。
    # - 其它路径（challenge/warn/delete/NSFW 守卫）：该用户在本群+本规则下的
    #   violations 行数，用来观察同一规则上的复犯。
    # 历史行是 NULL（这一列是后加的）。
    warning_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 判定细节：置信度与模型给的理由。误伤率报表要能回答"这次命中到底有多确定"，
    # 靠日志不够（日志会滚），必须落库。历史行是 NULL（这一列是后加的）。
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # F-060：这一列的定义必须和迁移 DDL 完全一致，否则新库（create_all）与升级库
    # （_sqlite_ensure_column）会长出不同的 schema。两者统一为**可空 + 库级默认 ''**：
    #   * SQLite 无法给已存在的表加 NOT NULL（ALTER 不支持），要做到 NOT NULL 就得
    #     整表重建——对一个纯观测列来说风险远大于收益；
    #   * server_default 不能省：老代码/迁移测试会用裸 SQL 插入 violations。
    # 写入路径（ModerationService.record_violation）始终写字符串，不会真的留 NULL；
    # 读取侧一律 ``str(x or "")``。
    verdict_reason: Mapped[str | None] = mapped_column(
        String(120), nullable=True, default="", server_default=""
    )
    # NULL means a threshold/sender-chat ban has not completed its Telegram +
    # audit persistence stage.  False is an attempted but unconfirmed ban.
    ban_enforced: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Notification delivery is a separate retryable stage.  The unavoidable
    # send-success/process-crash-before-commit window is intentionally tiny.
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 证据卡投递到审核日志频道时，那条频道消息的 message_id（人工放行/收回
    # 时用来就地编辑状态行）。频道未启用或投递失败时为 NULL（历史行也是 NULL）。
    log_channel_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 人工复核状态：none / released / revoked。server_default 不能省：老代码
    # 会用裸 SQL 插入 violations，NOT NULL 而没有库级默认值会直接撞约束。
    review_state: Mapped[str] = mapped_column(
        String(16), default="none", server_default="none"
    )
    # 执行人工放行/收回的操作者与时间。历史行为 NULL。
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped[Group] = relationship(back_populates="violations")

    __table_args__ = (
        Index(
            "ix_violations_group_user_ban",
            "group_id",
            "user_id",
            "ban_enforced",
        ),
        Index(
            "ix_violations_group_source_message",
            "group_id",
            "source_message_id",
            unique=True,
        ),
    )


class UserWarning(Base):
    __tablename__ = "user_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_warn_group_user", "group_id", "user_id", unique=True),)


class BotScreening(Base):
    """Per-group screening progress for bot senders.

    A bot's messages are moderated until it accumulates the configured number
    of clean messages, then it is whitelisted and skipped permanently.
    Violations reset the counter.
    """

    __tablename__ = "bot_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    whitelisted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_bot_screening_group_bot", "group_id", "bot_id", unique=True),)


class ModerationExemption(Base):
    __tablename__ = "moderation_exemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_exempt_group_user", "group_id", "user_id", unique=True),)


class ReplyMute(Base):
    __tablename__ = "reply_mutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_reply_mute_group_user", "group_id", "user_id", unique=True),)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("authorized_groups.group_id", ondelete="CASCADE"),
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(32), default="admin")

    __table_args__ = (Index("ix_admin_group_user", "group_id", "user_id", unique=True),)


class AuthorizedGroup(Base):
    __tablename__ = "authorized_groups"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    authorized_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Keep the durable authorization intent when Telegram reports that the bot
    # left or was kicked.  Inactive rows are excluded from normal authorization
    # and global fan-out queries, but can be reactivated by a later
    # ``my_chat_member`` join without resurrecting a manually deleted grant.
    bot_present: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RuntimeConfigRecord(Base):
    """Validated global runtime configuration stored as one JSON document."""

    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RuntimeConfigSecret(Base):
    """Encrypted secret values referenced by the runtime configuration."""

    __tablename__ = "runtime_config_secrets"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GlobalBan(Base):
    """Ban registry; enforced on join and on every message."""

    __tablename__ = "global_bans"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    # manual / spam_command / join_screening / profile_screening /
    # moderation_challenge_timeout
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JoinScreeningExemption(Base):
    """Users unbanned via /unban: skip name/bio screening afterwards."""

    __tablename__ = "join_screening_exemptions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JoinVerification(Base):
    """Pending provider verification; the user stays restricted until passing.

    kind="join": issued on member join; missing the deadline kicks (the user
    may rejoin and retry). kind="moderation": issued when a message is judged
    violating with low confidence under a ban rule; ``ban_on_timeout`` records
    whether missing the deadline may permanently ban the member.
    kind="patrol": issued when the profile patrol flags a member; missing the
    deadline kicks without banning (the user may rejoin). kind="raid": issued
    by the raid guard's retroactive sweep after a join-flood lockdown; same
    kick-without-ban timeout semantics as patrol.

    No secret token: the Mini App submits Telegram-signed initData, so the
    verified user identity comes from the signature, keyed by user_id here.
    """

    __tablename__ = "join_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(
        String(32), default="join", server_default="join"
    )
    # Snapshot the provider selected when this challenge was issued. This
    # keeps an in-flight page valid when the global or group default changes.
    provider: Mapped[str] = mapped_column(
        String(32), default="turnstile", server_default="turnstile"
    )
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Only meaningful for kind="moderation".  Default safely against accidental
    # escalation: legacy/unspecified rows release instead of banning on timeout.
    ban_on_timeout: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # Latest private-chat challenge message (chat id == user_id). Terminal
    # transitions rewrite/delete it so a stale WebApp button cannot keep
    # reopening the challenge page after the record is consumed.
    private_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_join_verification_group_user", "group_id", "user_id", unique=True),
        Index("ix_join_verifications_status_lease", "status", "lease_until"),
        Index("ix_join_verifications_status_deadline", "status", "deadline_at"),
        {"sqlite_autoincrement": True},
    )


class GroupMember(Base):
    """Known members per group, maintained from joins/leaves and messages.

    The Bot API cannot enumerate chat members, so the patrol scanner walks
    this roster instead. Rows are upserted on join and on every message
    (cheap: only when the visible profile changed) and marked left on leave;
    a stale row is harmless — the scanner skips users who are no longer in
    the group when Telegram rejects the restriction call.
    """

    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    left: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last profile signature (incl. rules fingerprint) that passed the patrol
    # in THIS group; per-group so multi-group patrols cannot thrash the cache.
    patrol_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_group_member_group_user", "group_id", "user_id", unique=True),
    )


class UserProfileScreen(Base):
    """Last screened profile signature per group/user pair.

    The signature includes the group's moderation-rule fingerprint, so a
    process-wide user-only key lets two groups continually overwrite each
    other's cached verdict.  Keep the cache scoped to the policy owner.
    """

    __tablename__ = "user_profile_screens"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64), default="")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WebhookInboxUpdate(Base):
    """Durable acceptance/dedup record for Telegram webhook updates."""

    __tablename__ = "webhook_inbox_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    auth_candidate: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_webhook_inbox_recovery",
            "priority",
            "auth_candidate",
            "completed_at",
            "dead_lettered_at",
            "next_attempt_at",
            "lease_until",
        ),
        Index(
            "ix_webhook_inbox_completed_retention",
            "completed_at",
        ),
        Index(
            "ix_webhook_inbox_dead_letter_retention",
            "dead_lettered_at",
        ),
    )


class TelegramDeleteJob(Base):
    """Durable delayed deletion for an outgoing Telegram message.

    A unique message key makes scheduling idempotent.  ``lease_until`` keeps a
    claimed row recoverable after a process crash without holding a database
    transaction across the Telegram API call.
    """

    __tablename__ = "telegram_delete_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_telegram_delete_jobs_message",
            "chat_id",
            "message_id",
            unique=True,
        ),
        Index(
            "ix_telegram_delete_jobs_recovery",
            "due_at",
            "lease_until",
        ),
    )


class PatrolRun(Base):
    """Per-group patrol bookkeeping: last completed run and its summary."""

    __tablename__ = "patrol_runs"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scanned: Mapped[int] = mapped_column(Integer, default=0)
    last_violations: Mapped[int] = mapped_column(Integer, default=0)
    running: Mapped[bool] = mapped_column(Boolean, default=False)


class SpeechStyleSample(Base):
    """Raw utterances collected from the persona-mimic target user."""

    __tablename__ = "speech_style_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_style_sample_group_user_id", "group_id", "user_id", "id"),
    )


class MessageVector(Base):
    """Active per-group dialogue history row."""

    __tablename__ = "message_vectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    role: Mapped[str] = mapped_column(String(16), default="user")
    importance_score: Mapped[float] = mapped_column(default=0.0)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    message_type: Mapped[str] = mapped_column(String(64), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    # 系统按 Telegram 身份写入的身份快照（is_owner / trusted_source）。历史重建时
    # 身份与信任只认这里，绝不从正文的 ``[id: … is_owner: …]`` 前缀推断（F-002）：
    # 正文是成员可控的，写自己的真实 id 也能骗过任何一致性校验。
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_message_vectors_group_created", "group_id", "created_at"),
        Index("ix_message_vectors_group_row", "group_id", "id"),
    )


class GroupMessageArchive(Base):
    """Lossless, group-scoped source record for long-horizon memory recall.

    ``MessageVector`` remains the compact working-memory/index row.  This table
    deliberately keeps Telegram identity, reply topology, sender snapshots and
    raw metadata separately so retention or retrieval work never has to infer
    those details from a rendered prompt string.
    """

    __tablename__ = "group_message_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_key: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    role: Mapped[str] = mapped_column(String(16), default="user")
    direction: Mapped[str] = mapped_column(String(16), default="inbound")

    sender_kind: Mapped[str] = mapped_column(String(32), default="unknown")
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_username: Mapped[str] = mapped_column(String(255), default="")
    sender_first_name: Mapped[str] = mapped_column(String(255), default="")
    sender_last_name: Mapped[str] = mapped_column(String(255), default="")
    sender_display_name: Mapped[str] = mapped_column(String(255), default="")
    sender_is_bot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sender_is_premium: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sender_language_code: Mapped[str] = mapped_column(String(32), default="")

    sender_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_chat_type: Mapped[str] = mapped_column(String(32), default="")
    sender_chat_title: Mapped[str] = mapped_column(String(255), default="")
    author_signature: Mapped[str] = mapped_column(String(255), default="")

    message_type: Mapped[str] = mapped_column(String(64), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    derived_text: Mapped[str] = mapped_column(Text, default="")

    sent_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_sender_name: Mapped[str] = mapped_column(String(255), default="")
    reply_to_content: Mapped[str] = mapped_column(Text, default="")

    message_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_group_id: Mapped[str] = mapped_column(String(128), default="")
    media_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    forward_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_group_message_archive_group_message_key",
            "group_id",
            "message_key",
            unique=True,
        ),
        Index(
            "ix_group_message_archive_group_sent_id",
            "group_id",
            "sent_at",
            "id",
        ),
        Index(
            "ix_group_message_archive_group_telegram_message",
            "group_id",
            "telegram_message_id",
        ),
        Index(
            "ix_group_message_archive_group_reply_to_message",
            "group_id",
            "reply_to_message_id",
        ),
    )


class GroupMessageArchiveEmbedding(Base):
    """Durable semantic-index projection for one archive message.

    ``GroupMessageArchive`` is the source of truth for message content and
    metadata.  This table is deliberately a replaceable projection: a source
    edit, embedding-model change, or a failed provider call can invalidate the
    row without touching the raw archive.  ``space_id`` identifies the exact
    embedding space (provider/model/endpoint), while ``status`` lets a
    write-behind worker retry indexing without blocking message ingestion.
    """

    __tablename__ = "group_message_archive_embeddings"

    archive_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("group_message_archive.id", ondelete="CASCADE"),
        primary_key=True,
    )
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    space_id: Mapped[str] = mapped_column(String(128), default="")
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    encoding: Mapped[str] = mapped_column(String(16), default="f16le")
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)
    embedding_norm: Mapped[float | None] = mapped_column(nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    last_error: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_group_message_archive_embeddings_group_status_retry",
            "group_id",
            "status",
            "next_attempt_at",
            "archive_id",
        ),
        Index(
            "ix_group_message_archive_embeddings_group_space_status",
            "group_id",
            "space_id",
            "status",
            "archive_id",
        ),
        Index(
            "ix_group_message_archive_embeddings_group_message_key",
            "group_id",
            "message_key",
            unique=True,
        ),
    )


class StickerLibraryRecord(Base):
    __tablename__ = "sticker_library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    file_id: Mapped[str] = mapped_column(String(255))
    emoji: Mapped[str] = mapped_column(String(32), default="")
    set_name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    seen_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="group_message")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_sticker_group_file", "group_id", "file_id", unique=True),)

class MemberCheckin(Base):
    """每日签到：一行 = 一个成员在一个本地自然日的一次签到。

    "一天只能签一次"靠表上的唯一索引保证，不靠"先查到再插入"这种有竞态的写法：
    并发点两次只有一条能落库，另一条撞 IntegrityError，我们把它翻译成"今天签过了"。
    积分就是这张表的聚合（COUNT/SUM），没有会跟真实记录飘掉的计数器。
    """

    __tablename__ = "member_checkins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # 本地（Asia/Shanghai）自然日 YYYY-MM-DD。用 UTC 会让 00:00-08:00 的签到
    # 落到前一天，跨天判断直接错。
    checkin_date: Mapped[str] = mapped_column(String(10))
    # 预留：以后要做连续奖励/管理员补分，改这里即可，不用动表结构。
    points: Mapped[int] = mapped_column(Integer, default=1)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    # F-050：时钟口径 = 本地（Asia/Shanghai）朴素时间，和 checkin_date 同口径。
    # Python 侧 default 负责应用写入；server_default 只作为裸 SQL 插入的兜底
    # （它是 SQLite CURRENT_TIMESTAMP = UTC，数值上会差 8 小时，不要依赖它）。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_member_checkins_day",
            "group_id",
            "user_id",
            "checkin_date",
            unique=True,
        ),
        # F-030：每日提醒与签到名单只按 (group_id, checkin_date) 过滤，而上面
        # 唯一索引的前缀是 (group_id, user_id, ...)，按日期过滤命中不了它——
        # 只能扫该群全部历史签到行（这张表只追加、永不清理，群越大越久越慢）。
        Index(
            "ix_member_checkins_group_day",
            "group_id",
            "checkin_date",
        ),
    )

class MemberPointSpend(Base):
    """积分消费台账：一行 = 一次扣分（目前只有"消耗积分免除质询"）。

    和 member_checkins 一样是 append-only：可用积分 = 签到的 SUM(points) 减去
    这张表的 SUM(points)。不设"余额"列，避免余额和流水对不上。
    """

    __tablename__ = "member_point_spends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default="")
    # 幂等键：同一次质询只能扣一次（重复点按钮时唯一索引会挡住第二条）
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # F-050：与 member_checkins / member_entitlements / member_point_awards 统一为
    # 本地（Asia/Shanghai）朴素时间。扣分走 Core
    # ``INSERT ... SELECT``（checkin.spend_points），Python 侧 default 不会自动生效，
    # 那里必须显式带上 created_at；server_default（UTC）只作裸 SQL 兜底。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_member_point_spends_ref",
            "group_id",
            "user_id",
            "ref",
            unique=True,
        ),
    )


class LlmUsageDaily(Base):
    """按天、按阶段的 LLM 用量台账：token、缓存、超时、空响应、解析失败。

    写入路径见 ``bot.services.llm_metrics``：主路径只在内存里加整数，
    60 秒的惰性定时器把批次并进这张表（累加式 upsert），所以回复延迟不受影响。
    ``usage_date`` 是 Asia/Shanghai 的自然日，和 ``member_checkins.checkin_date``
    同口径——否则日报 / 周报会对不上。
    """

    __tablename__ = "llm_usage_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 命中前缀缓存的 token（网关的 cached_tokens / cache_read_input_tokens）
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 写进缓存的 token（cache_creation_input_tokens）
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 思考 token（completion_thinking_tokens）；关掉思考时应恒为 0
    thinking_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    empty_responses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    timeouts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 模型有输出但不是合法 JSON（审核用），与空响应分开计
    parse_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ux_llm_usage_daily_day_stage", "usage_date", "stage", unique=True),
    )


class MemberActivityDaily(Base):
    """每周活跃激励的日累计：一行 = 一个成员在一个本地自然日（Asia/Shanghai）。

    为什么按天滚动累计而不是等周末再算：群里消息不重放，周末再统计要么扫
    ``group_message_archive``（只留 7 天，覆盖不了完整自然周），要么什么都没了。
    所以每条合格消息落到这里做一次 UPSERT。

    ``messages`` 在**写入时**就按 ``MAX_DAILY_MESSAGES``（20）封顶：防刷屏的要求
    落在数据层，之后不管怎么聚合都不会把刷屏算成贡献。``activity_date`` 与
    ``member_checkins.checkin_date`` 同口径（本地自然日 ``YYYY-MM-DD``）。
    """

    __tablename__ = "member_activity_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activity_date: Mapped[str] = mapped_column(String(10))
    # 当天有效消息条数（写入时已按 20 封顶）
    messages: Mapped[int] = mapped_column(Integer, default=0)
    # 当天"别人回复我"的次数
    replies_received: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次发言时的展示名：榜单要显示昵称，不能指望每个人都签到过
    display_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        # UPSERT 的冲突目标；也是"一个人一天一行"的唯一保证
        Index(
            "ix_member_activity_daily_day",
            "group_id",
            "user_id",
            "activity_date",
            unique=True,
        ),
        # 周结算按 (群, 日期区间) 聚合
        Index(
            "ix_member_activity_daily_group_date",
            "group_id",
            "activity_date",
            "user_id",
        ),
    )


class PrivateChatUsage(Base):
    """1 对 1 私聊的日用量计数：一行 = 一个用户在一个本地自然日。

    只记**条数**（不记内容），用于「每人每天」和「全局每天」两道配额闸门。
    全局行用 ``user_id = 0`` 表示（真实 Telegram 用户 id 恒为正，不会撞车）。
    ``usage_date`` 与 ``member_checkins.checkin_date`` 同口径（本地自然日
    ``YYYY-MM-DD``），所以跨零点自动换行，不需要额外清理任务。
    """

    __tablename__ = "private_chat_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 0 = 全局合计行；其余为真实 Telegram 用户 id
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_date: Mapped[str] = mapped_column(String(10))
    messages: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        # UPSERT 的冲突目标；也是「一个人一天一行」的唯一保证
        Index("ix_private_chat_usage_user_day", "user_id", "usage_date", unique=True),
    )


class PrivateChatMessage(Base):
    """1 对 1 私聊的对话正文：一行 = 一条 ``user`` 或 ``assistant`` 消息。

    和 ``private_chat_usage``（只记条数、用于配额）是两张表：那张是账本，这张是内容。
    首版（2026-10-03）私聊正文只在进程内存里留最近 12 轮，机器人一重启就全忘；
    现在改成落库 + 按 token 预算装配，重启不失忆，也能记住很久以前说过的话。

    - **幂等**：Telegram 会重投递同一条 update（网络抖动、容器重启、webhook 重试），
      ``(user_id, message_key)`` 上的唯一约束 + ``ON CONFLICT DO NOTHING`` 保证同一轮
      重复写入不会产生重复行。``message_key`` 形如 ``u:<message_id>``（用户那条消息）
      和 ``a:<message_id>``（机器人这一轮的回复）：一轮两行共用一个来源 message_id。
    - **只存对话**：注入给模型的系统资料块（``[WEB_SEARCH_RESULTS]`` 等）不是对话内容，
      不落库（见 ``bot.services.private_chat`` 的写入路径）。
    - ``(user_id, id)`` 复合索引服务「按用户取最近 N 条」的固定查询：``user_id`` 等值
      + ``id`` 倒序，扫索引就能拿到尾巴，不需要给全表排序。
    - ``created_at`` 有独立索引：留存清理按时间删过期行。
    """

    __tablename__ = "private_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 真实 Telegram 用户 id（恒为正；私聊没有群维度）
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: 'user' | 'assistant'
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
        index=True,
    )
    #: 幂等键（同一轮重投递只落一行）
    message_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "message_key",
            name="uq_private_chat_messages_user_key",
        ),
        # 「取这个用户最近 N 条」的驱动索引（user_id 等值 + id 倒序）
        Index("ix_private_chat_messages_user_id_desc", "user_id", "id"),
    )


class MemberEntitlement(Base):
    """积分商店里"生效中"的权益：一行 = 一个人在一个群里的一件在租商品。

    目前有两种：``kind='tag'``（自定义头衔，``payload`` 是头衔文字）和
    ``kind='pin'``（置顶求助，``payload`` 是被置顶的消息 ID 字符串）。

    - ``(group_id, user_id, kind)`` 上的唯一索引保证"一人一项"：同一个人、同一个群里
      只能有一个生效中的头衔和一个生效中的置顶，续费是把这一行往后延，不是再插一行。
    - ``expires_at`` 上有索引：``bot.tools.shop_expire`` 每次只扫到期的行。
    - 到期处理完就把这一行删掉，所以表里只有"现在还有效"的权益——查重复头衔、
      查"我是不是已经有置顶"都只看这张表，不需要再按时间过滤。

    时间一律是本地（Asia/Shanghai）朴素时间，和 ``member_checkins.checkin_date``、
    ``member_activity_daily.activity_date`` 同口径。
    """

    __tablename__ = "member_entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 'tag' | 'pin'
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 头衔文字，或置顶消息的 message_id（字符串）
    payload: Mapped[str] = mapped_column(String(255), default="")
    #: 买下这件商品时那条消费流水的 ref，退款/审计时对得上
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # "一人一项"：续费走 UPDATE，不是再插一行
        Index(
            "ix_member_entitlements_slot",
            "group_id",
            "user_id",
            "kind",
            unique=True,
        ),
        # 到期扫描的驱动索引
        Index("ix_member_entitlements_expires", "expires_at"),
    )


class MemberPointAward(Base):
    """积分奖励流水：一行 = 一次发放（目前只有"每周活跃激励"）。

    **为什么不用 member_checkins 发奖**：连续签到天数是从签到日期集合里倒着推的，
    塞一行假的签到会直接把连击算错（用户能看见自己的连续天数）。奖励是另一条
    独立流水，和签到互不影响。

    ``ref`` 是幂等键（``weekly-activity:2026-W40:<user_id>``）：同一周同一个人
    只会有一行，重复结算靠唯一索引 + ON CONFLICT DO NOTHING 挡住，不抛异常。
    """

    __tablename__ = "member_point_awards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default="")
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_member_point_awards_ref",
            "group_id",
            "user_id",
            "ref",
            unique=True,
        ),
        # 审计"这周发了多少分"用
        Index(
            "ix_member_point_awards_group_created",
            "group_id",
            "created_at",
            "user_id",
        ),
    )


class SearchResultRecord(Base):
    """一次联网检索的**入档**记录：一行 = 某个作用域下某次检索的结果摘要。

    第 3 期新增。检索结果以前是「用完即丢」——同一句「5090 现在多少钱」隔半小时
    再问一次，模型手上既没有上次的结果，也无从知道「上次查到的是什么时间的事」，
    于是要么重新烧一次检索，要么把三天前的价格当现价说出来。这里把结果留档，
    再用的时候**带上时间戳与「距今多久」**。

    - ``scope`` / ``scope_id``：``private`` + 真实用户 id，或 ``group`` + 群 id。
      两个作用域**互不可见**（私聊的记录不会进群聊的 prompt，见 C 项隐私红线）。
    - ``digest``：结果摘要。**截断保存**（见 ``bot.services.search_memory`` 的上限），
      整篇塞进上下文既贵又没用。
    - ``sources``：JSON 数组，每项 ``{"title": ..., "url": ...}``，供带链接引用。
    - ``kind``：``price`` / ``news`` / ``fact`` / ``unknown``，决定新鲜窗口
      （价格 24h、新闻 48h、事实 7d），过期记录注入时会标注「可能已过期」。
    - ``outcome``：``ok`` / ``empty``。空结果也留一行，是为了让「这问题刚问过、
      当时就是查不到」这件事本身可复用，避免反复搜同一句话。
    - ``(scope, scope_id, id)`` 复合索引服务「取某作用域最近 N 条」的固定查询；
      ``created_at`` 独立索引给留存清理（默认 30 天）用；``(scope, scope_id, query,
      created_at)`` 给幂等窗口内的「同一句问话」查找用。
    """

    __tablename__ = "search_result_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 'private'（私聊）| 'group'（群聊）
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 私聊 = user_id，群聊 = group_id
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 检索用的查询词（已清理过称呼/客套）
    query: Mapped[str] = mapped_column(String(512), default="")
    #: 结果摘要（截断保存）
    digest: Mapped[str] = mapped_column(Text, default="")
    #: [{"title": ..., "url": ...}, ...]
    sources: Mapped[list] = mapped_column(JSON, default=list)
    #: 'price' | 'news' | 'fact' | 'unknown'
    kind: Mapped[str] = mapped_column(String(16), default="unknown")
    #: 'ok' | 'empty'
    outcome: Mapped[str] = mapped_column(String(16), default="empty")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        # 「取某个作用域最近 N 条」的驱动索引（scope/scope_id 等值 + id 倒序）
        Index(
            "ix_search_result_records_scope_scope_id_id",
            "scope",
            "scope_id",
            "id",
        ),
        # 留存清理按时间删
        Index("ix_search_result_records_created_at", "created_at"),
        # 幂等窗口内「同一句话」的查找（写入前去重）
        Index(
            "ix_search_result_records_scope_query_created",
            "scope",
            "scope_id",
            "query",
            "created_at",
        ),
    )


class CheckinReminderPost(Base):
    """签到提醒的发送台账：一行 = 一个群在一个时段发出去的那条提醒。

    ``slot_key`` 形如 ``2026-10-01:9``（**本地**自然日 + 本地时段），
    ``(group_id, slot_key)`` 上的唯一索引就是"同一时段只发一条"的幂等键：
    定时任务重试、运维手动重跑、cron 重复触发都靠它挡住第二条。

    先占位再发送：占位成功才发消息，发失败就把占位删掉（见
    ``bot.services.checkin_reminder.release_reminder_slot``），这样"没发出去"
    不会被记成"已发过"。``message_id`` 在发送成功后才回填，供审计与按钮回调
    反查"这条提醒是哪个时段发的"。
    """

    __tablename__ = "checkin_reminder_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # "YYYY-MM-DD:slot"（本地自然日 + 本地时段 9/12/15/18）
    slot_key: Mapped[str] = mapped_column(String(32), nullable=False)
    # 发送成功前为 0；发送成功后回填 Telegram message_id
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_checkin_reminder_posts_slot",
            "group_id",
            "slot_key",
            unique=True,
        ),
        # 按钮回调按 (群, 消息) 反查这条提醒是哪个时段发的（更新"今日已签到 N 人"用）
        Index(
            "ix_checkin_reminder_posts_message",
            "group_id",
            "message_id",
        ),
    )


class UserFact(Base):
    """第 4 期：从群聊/私聊里提炼出来的**稳定结构化事实**（长期记忆）。

    和 ``GroupPermanentMemory``（后台网页手工维护的永久记忆）是两张表：那张是运维
    手写的群级备忘，机器人不写不读；这张是机器人自己从对话里提炼、去重、可过期、
    可被用户自己删除的「关于人」的认识。

    - ``scope`` / ``scope_id``：口径与 ``search_result_records`` 完全一致
      （``private`` + user_id，或 ``group`` + group_id）。**两个作用域互不可见**：
      群聊侧的读取路径永远不会读 ``scope='private'`` 的行（第 1 验收项红线）。
    - ``subject_user_id``：这条事实**关于谁**。``0`` 表示「关于本群整体」的公共事实。
    - ``fact_text``：归一化后的一句话（上限 200 字符，超长截断）。
    - ``category``：``identity`` / ``preference`` / ``relationship`` / ``event`` /
      ``taboo`` / ``skill`` / ``other``。
    - ``confidence``：0–100 整数（被动提炼默认 60，模型主动写默认 70）。
    - ``source_kind``：``passive``（后台提炼）| ``tool``（模型主动调 remember）。
    - ``evidence_excerpt``：原文片段（上限 200 字符，**必填**——没有出处的事实不
      许入库）。这一列也是「模型编了但输入里没有」时的兜底证据。
    - ``fingerprint`` = ``sha1(scope|scope_id|subject_user_id|归一化文本)``：同一
      作用域、同一个人、同一句话只会有一行（靠唯一索引兜底，不靠先查后插的竞态写法）。
    - ``confirm_count`` / ``last_confirmed_at``：同一事实再次被提炼到就 +1 并刷新时间，
      **不新增行**。
    - ``expires_at``：可空。``category='event'`` 默认 ``now + memory_event_ttl_days``
      天，其余为 NULL（不过期）。到期的 event 由巡检标 ``deleted``（不物理删，用户
      还能查到「这条过期了」）。
    - ``status``：``active`` | ``superseded``（被语义互斥的新事实替代）| ``deleted``
      （用户删除 / 过期）。用户删过的行**不许复活**。
    """

    __tablename__ = "user_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 'private'（私聊）| 'group'（群聊）
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 私聊 = user_id，群聊 = group_id
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 这条事实关于谁；0 = 本群整体的公共事实
    subject_user_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 归一化后的一句话事实（≤ 200 字符）
    fact_text: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: identity | preference | relationship | event | taboo | skill | other
    category: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: 'passive'（后台提炼）| 'tool'（模型主动写）
    source_kind: Mapped[str] = mapped_column(
        String(16), default="passive", nullable=False
    )
    #: 来源群的 telegram_message_id（私聊来源没有这个 id 时为 NULL）
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 原文片段（≤ 200 字符，必填）
    evidence_excerpt: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: sha1(scope|scope_id|subject_user_id|归一化文本)，去重用
    fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    confirm_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 'active' | 'superseded' | 'deleted'
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 替代这条事实的新事实 id（自引用，可空）
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        # 幂等键：同一作用域 + 同一个人 + 同一句话只有一行
        Index(
            "ix_user_facts_scope_subject_fingerprint",
            "scope",
            "scope_id",
            "subject_user_id",
            "fingerprint",
            unique=True,
        ),
        # 读取（status='active'）的驱动索引
        Index(
            "ix_user_facts_scope_subject_status",
            "scope",
            "scope_id",
            "subject_user_id",
            "status",
        ),
        Index("ix_user_facts_created_at", "created_at"),
        # 过期清理（event 到期）与留存清理的驱动索引
        Index("ix_user_facts_expires_at", "expires_at"),
        Index("ix_user_facts_last_confirmed_at", "last_confirmed_at"),
    )


class MemoryExtractCursor(Base):
    """第 4 期：每个作用域「被动提炼已经处理到哪一行」的游标。

    一行 = 一个作用域（``scope`` + ``scope_id``）。``last_row_id`` 是已处理到的
    ``group_message_archive.id`` / ``private_chat_messages.id``：只处理 ``id >
    last_row_id`` 的行，所以重复跑同一批不会重复提炼，失败时**不前移**就是天然的
    重试队列（下一轮重试同一批，绝不在一次调用里 while 重试）。
    """

    __tablename__ = "memory_extract_cursors"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: 已处理到的归档/私聊行 id（0 = 还没处理过）
    last_row_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        onupdate=now_shanghai_naive,
        server_default=func.now(),
    )


class MemoryOptout(Base):
    """第 4 期：用户级的长期记忆开关（``/memory off`` / ``/memory on``）。

    一行 = 一个明确说「别记我」的用户。语义是**双向**的：

    * 不再从 ``user_id`` 发的消息里提炼事实（提炼时按发送者过滤掉）；
    * 他已经入库的 active 事实**一律不再注入**（``/memory off`` 时软删
      ``status='deleted'``；读取路径也会按本表再过滤一次，防止漏网）。

    ``reason`` 只用于审计（≤ 60 字符），不参与任何判定。
    """

    __tablename__ = "memory_optouts"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    reason: Mapped[str] = mapped_column(String(60), default="", nullable=False)
