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


class GroupArchiveState(Base):
    """归档**内容变更**的单调计数器（摘要来源完整性保护的唯一依据）。

    只在"已归档内容被**修改或删除**"时 +1；**新消息插入不动它**（否则每条新消息都会让
    已覆盖的摘要失效）。一次主键读写即可判定"摘要覆盖的来源是否还是原样"，不需要在
    回复/审核热路径里遍历或哈希整个归档。

    与发布同事务：摘要发布用这个计数器做 SQL 条件（标量子查询），所以"生成期间删改"
    不可能被带进新摘要，也不存在 check→publish 的 TOCTOU。
    """

    __tablename__ = "group_archive_state"

    # 刻意**不设**外键：归档行可以存在于没有 ``groups`` 行的群（维护/导入场景），
    # 计数器必须对这些群同样可写，否则删除/编辑会因为 FK 失败而绕过保护。
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    content_revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


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
    #: 生成这份摘要时的 :class:`GroupArchiveState` 计数；``-1`` = 没有保护数据
    #: （本特性之前发布的旧摘要）→ 前台按"失效"处理，由 worker 重建。
    source_revision: Mapped[int] = mapped_column(Integer, default=-1)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WeeklyReportPost(Base):
    """周报的**发送台账**：一行 = 一个目标在某个 ISO 周收到过一次周报。

    ``bot.tools.weekly_report`` 以前对群消息**完全没有幂等**：cron 与上一次运行重叠、
    运维手工补跑、手动重跑，都会让**每个授权群收到重复周报**，而 exit code 仍是 0、
    数据上无法察觉。积分结算那半边是幂等的（``(用户, ISO 周)`` 唯一键），只有发消息
    这一半不是。

    ``(target_id, week_key)`` 上的唯一索引就是幂等键，写入走
    ``INSERT ... ON CONFLICT DO NOTHING + RETURNING``（与 ``checkin_reminder_posts``
    同一套模式）。``target_id`` 为 0 表示"私发超管的成本摘要"——它同样需要幂等。
    ``message_id`` 在发送成功后才回填。
    """

    __tablename__ = "weekly_report_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 群 id；``0`` = 私发超管的成本摘要（不是群，所以不与群 id 冲突）
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: ISO 周，形如 ``2026-W41``（发送时刻的 ISO 周，不依赖 ``days`` 参数）
    week_key: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 发送成功前为 0；发送成功后回填 Telegram message_id
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ux_weekly_report_posts_target_week",
            "target_id",
            "week_key",
            unique=True,
        ),
    )


class GroupSummaryFailureState(Base):
    """摘要调度器的**失败退避台账**（B-25）。

    退避阶梯（``failure_backoff_seconds * 2**(k-1)``，封顶
    ``failure_backoff_max_seconds``）与「退避到什么时候」以前只活在
    ``GroupSummaryScheduler`` 的两个进程内 dict 里，于是**每次重启都从
    ``failure_backoff_seconds`` 重新爬阶梯**——容器滚动重启等于给所有持续失败的群
    发一次「立即重试」，永远到不了上限。

    ``backoff_until`` 存的是**墙钟**时间（不是 ``time.monotonic``）：进程重启之后
    仍然可以和 ``now_shanghai_naive()`` 比较。成功发布后对应行被删除。
    """

    __tablename__ = "group_summary_failure_states"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    backoff_until: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GroupPermanentMemory(Base):
    __tablename__ = "group_permanent_memories"

    __table_args__ = (
        # 群内内容唯一：Mini App 的"先查重再插入"挡不住并发（查重在写锁之外），
        # 唯一索引才是最终保证。已有的库由 bot/db/engine.py 的启动迁移补齐。
        Index(
            "ix_group_permanent_memories_group_content",
            "group_id",
            "content",
            unique=True,
        ),
    )

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
    # 双击确认状态机：第一次点「人工放行 / 确认封禁」只把意图落库（pending_action
    # 存 "rel"/"ban"，pending_at 存点击时间），第二次同键且在
    # moderation.review_confirm_seconds 窗口内才执行；切换按钮会改写为新的
    # pending，仍需它自己的第二次点击。执行/拒绝/失败后清空。历史行为 NULL。
    pending_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pending_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
        # F-032：审核质量报表的每群 8 次窗口查询都按 (群, created_at) 过滤，
        # 而这张表原来只有上面两个索引 —— 每次窗口统计都是全表扫。
        Index(
            "ix_violations_group_created_at",
            "group_id",
            "created_at",
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


class GroupOnboardingRequest(Base):
    """新群接入申请：与 ``AuthorizedGroup`` **严格分离**的一条待审记录。

    为什么单独一张表而不是给 ``authorized_groups`` 加状态列：授权是"现在就能用"，
    申请是"还想用"。把两者放同一行，任何一次把状态从 pending 翻到 approved 的
    半成品写入，都会同时变成"群已经能用了"——而审批链路上还挂着一次尚未完成的
    Telegram 身份核验。这里 pending 行对授权路径**完全不可见**。

    同群仅一条有效 pending 由**部分唯一索引**保证（不是 SELECT→INSERT 裸竞态）：
    两个并发提交只有一个能落库，另一个拿到同一条（幂等）。
    """

    __tablename__ = "group_onboarding_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 公开不透明标识（URL 与响应里用它，不暴露自增 id 与全局申请量）。
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    applicant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 申请人勾选的期望功能（目录内取值，见 group_onboarding.KNOWN_FEATURES）。
    requested_features: Mapped[list] = mapped_column(JSON, default=list)
    preset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    #: 这条申请在**被批准的那一刻**是否授予了群授权。
    #:
    #: 它是一条**历史记录**，不是「现在还在授权」—— 超管随时可以撤权、bot
    #: 随时可能被踢。把它当成当前状态读，就会出现"撤销被一条旧批准静默
    #: 顶掉"的事故（因为重放批准会重跑 authorize_group）。当前状态请查
    #: ``authorized_groups.bot_present``。
    granted_group_authorization: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )

    __table_args__ = (
        Index("ix_group_onboarding_request_id", "request_id", unique=True),
        Index("ix_group_onboarding_group_created", "group_id", "created_at"),
        Index("ix_group_onboarding_applicant_created", "applicant_id", "created_at"),
        Index(
            "ux_group_onboarding_open_request",
            "group_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
    )


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


class LlmCostDaily(Base):
    """按天 × 归属 × 用途 × 阶段 × **实际**供应商/模型的明细台账。

    与 :class:`LlmUsageDaily` 的口径**不同**，两者不是同一份账：

    * ``llm_usage_daily`` —— 历史表，只记「成功调用」，键是 (日, 阶段)。
      看板 / 周报一直按这个口径读，本轮**不改**它。
    * ``llm_cost_daily`` —— 本轮新增。每次真实发出供应商请求都是一个
      ``attempt``，重试与回退各记各的；还记失败 / 超时 / 空响应 / 用量未知。
      键包含归属与实际模型，所以「按群」「按用途」「按真实供应商」都能读。

    两者相加会**重复计数**（同一次成功调用两边都有一份），所以任何报表都必须
    明确声明读的是哪一张；本轮的新报表只读这张表。

    ``group_id`` 对非群归属固定为 0：SQLite 的唯一索引把 NULL 视为互不相等，
    用 0 才能让 (日, 归属, 群, ...) 的唯一约束真正生效。"0 不是群"由
    ``usage_scope`` 在写入前保证 —— ``kind='group'`` 的行永远不会是 0。
    """

    __tablename__ = "llm_cost_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    #: group / private / private_user / system
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: safety / optional / background_optional
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    successes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    timeouts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    empty_responses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parse_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 发出过请求但**没有** usage 可用（失败/超时/上游漏报）。金额未知，不是 0。
    usage_unknown: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 有 usage 时才有值；None = 未知，绝不用 0 顶替。
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ux_llm_cost_daily_key",
            "usage_date",
            "scope",
            "group_id",
            "purpose",
            "stage",
            "provider",
            "model",
            unique=True,
        ),
        Index("ix_llm_cost_daily_group_day", "group_id", "usage_date"),
    )


class LlmCostPricedDaily(Base):
    """**发送时计价**的伴随聚合：主台账的另一个维度，不是第二份 attempt。

    ``llm_cost_daily`` 记的是用量（attempt / 成功 / 失败 / token），它的键里
    **没有**价格版本，因为一次 attempt 的价格是"发出那一刻"的事。这张表把同一
    次 attempt 按「当时的目录版本 + 币种」再聚合一遍，于是：

    * 金额只在**发出时**取的价上算（``price_snapshot`` 冻结该 provider/model
      的四段单价），改价不会追改历史；
    * 已知与未知分开记：``priced_attempts`` 是能算出金额的那些，
      ``unknown_attempts`` 是"缺 usage / 缺价 / 用量坏掉"的那些。
      **未知不会被已知吞掉** —— 有未知时 `amount_micro` 只是"已计价部分"，
      报表不能把它当完整总成本；
    * 旧数据在这张表里**没有行**：报表用主表的 ``attempts`` 减去本表的
      ``attempts`` 得到 legacy（历史无价格元数据），既不回填假价格，也不假装
      已覆盖。

    ``currency`` 为空串表示"发送时不知道币种"（缺价 / 缺目录）—— 它是**独立**的
    未知桶，绝不折成 USD。``price_version`` 为 0 表示当时没有价格元数据。

    与 ``llm_cost_daily`` 的关系是同一批 attempt 的**不同维度**：两张表的
    ``attempts`` 都数同一次发出，任何报表都**不能**把它们相加来"补全"次数。
    """

    __tablename__ = "llm_cost_priced_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    #: 发出时的目录版本；0 = 当时没有价格元数据（与"这一版里没有这个条目"不同）。
    price_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 发出时该条目的币种；"" = 不知道（独立未知桶，不猜 USD）。
    currency: Mapped[str] = mapped_column(String(8), default="", server_default="")

    #: 冻结的条目（四段单价是十进制字符串）；缺价时为 NULL。
    price_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 其中**能算出金额**的 attempt 数（0 单价 + 已知 usage 也算已计价）。
    priced_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 其中金额未知的 attempt 数 = ``unknown_usage_attempts`` + ``unknown_price_attempts``。
    unknown_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 未知原因之一是"用量缺失/坏掉"（结算时判定，事后再看行已经分不出来）。
    unknown_usage_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 未知原因之二是"没有价格条目 / 实际用到的组件缺单价"。
    unknown_price_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 已计价部分之和（整数微单位）。有 unknown_attempts 时**不是**完整成本。
    amount_micro: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ux_llm_cost_priced_daily_key",
            "usage_date",
            "scope",
            "group_id",
            "purpose",
            "stage",
            "provider",
            "model",
            "price_version",
            "currency",
            unique=True,
        ),
        Index("ix_llm_cost_priced_daily_group_day", "group_id", "usage_date"),
    )


class MemberActivityDaily(Base):
    """每周活跃激励的日累计：一行 = 一个成员在一个本地自然日（Asia/Shanghai）。

    为什么按天滚动累计而不是等周末再算：群里消息不重放，周末再统计要么扫
    ``group_message_archive``（只留 7 天，覆盖不了完整自然周），要么什么都没了。
    所以每条合格消息落到这里做一次 UPSERT。

    ``messages`` 在**写入时**就按每天的条数上限封顶：防刷屏的要求落在数据层，之后
    不管怎么聚合都不会把刷屏算成贡献。上限是运行时配置
    ``runtime_config.activity.max_daily_messages``（默认 20），读侧
    ``bot.services.activity.record_message_activity``；改动它**只影响之后的写入**，
    已经落库的历史行不会被回算。``activity_date`` 与
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

    ``delivered_at``（B-27）是「占位真的变成了消息」的证据。进程在 claim 与 send
    之间被 SIGKILL / OOM / 容器驱逐时只会留下 ``message_id=0`` 的空占位，而唯一
    索引会让该时段永远不再发、且没有补发路径；``reap_stale_reminder_slots`` 靠
    「``message_id=0`` 且 ``delivered_at IS NULL`` 且 ``created_at`` 早于宽限期」
    把这类空占位清掉并补发。
    """

    __tablename__ = "checkin_reminder_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # "YYYY-MM-DD:slot"（本地自然日 + 本地时段 9/12/15/18）
    slot_key: Mapped[str] = mapped_column(String(32), nullable=False)
    # 发送成功前为 0；发送成功后回填 Telegram message_id
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 发送成功并回填 message_id 的时刻；空占位为 NULL（B-27 的判定依据）
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    #: 单调修订号（C3）：**任何**影响编辑/可见性的写入都必须 +1（再次确认、替代、
    #: 到期、optout 删除、软删、人工纠错），人工纠错用 (id, revision, status=active)
    #: 做 CAS。老库由 init_db 的幂等迁移补列并显式回填 1（不是 NULL、不是时间戳）。
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    #: 写入时从**一致归档行**核验出的可信话题(thread) id；无法核验/旧行为 NULL，
    #: 绝不用正文或模型输出里猜出来的 thread（C3 深链只认这一列 + 归档复核）。
    source_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 人工纠错的操作者与时间：修正版标"人工纠正"，原始来源关系仍留在旧行；
    #: 未纠错的行保持 NULL（不捏造）。
    corrected_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class UserFactCorrection(Base):
    """C3：人工纠错审计。一行 = 一次**真实提交成功**的人工修正。

    只在事务提交成功后才有意义（与修正版、旧行的 ``superseded`` 同一事务）。审计
    里保留作用域、新旧 stable id 与修订号、操作者与时间、原因/结果：只读管理面、
    ``/memory`` 自身作用域与重启后的排障都靠它，任何其它群/私聊都读不到。
    """

    __tablename__ = "user_fact_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 与 user_facts 相同的硬边界：'private' | 'group'
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_user_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 被修正的旧 stable id 及它当时的修订号（CAS 期望值）
    old_fact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 修正版的新 stable id 及修订号（提交成功后才写这里）
    new_fact_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    new_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    operator_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 'admin'（群管理面）| 'self'（本人在 /memory 自管）| 'superadmin'
    operator_kind: Mapped[str] = mapped_column(
        String(16), default="admin", nullable=False
    )
    #: 固定动作/结果码，方便审计与回归断言
    action: Mapped[str] = mapped_column(String(16), default="correct", nullable=False)
    result: Mapped[str] = mapped_column(
        String(32), default="corrected", nullable=False
    )
    old_text: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    new_text: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_user_fact_corrections_scope_fact",
            "scope",
            "scope_id",
            "old_fact_id",
        ),
        Index("ix_user_fact_corrections_created_at", "created_at"),
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


# --------------------------------------------------------------------------- #
# 审核回放（dry-run）：把候选规则/提示词放在**已授权、已脱敏、可追溯**的样本上
# 跑一遍，和"当前生效口径"逐条对比，**只出数字，不碰线上**。
#
# 这三张表存在的唯一理由是：改审核口径这件事不能靠"上线看看会不会出事"。
# 所以它们承载的正文是**脱敏后的副本**（`redacted_text`），带 TTL，见
# ``bot/services/moderation_replay.py`` 的留存策略；回放全程不删消息、不禁言、
# 不扣分、不发审核卡，也不写 violations。
# --------------------------------------------------------------------------- #


class ModerationReplaySample(Base):
    """一条回放样本：脱敏后的消息正文（+ 可选上下文）与可选的人工标注。

    ``gold_label`` 为 NULL 表示**没有可信答案**：这类样本照样进回放（用来只看
    判定差异），但绝不参与 FP/FN —— 见 ``compute_summary`` 的公式。

    **金标只能由人工显式确认产生**（``label_state == 'labelled'``）。机器给的
    置信度、``violations.review_state`` 这类信号一律落在 ``provenance`` 里，
    在界面上是"provenance"而不是答案键；导入一条机器命中**不会**得到金标。
    """

    __tablename__ = "moderation_replay_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    #: violation_release（管理员人工放行=确认误伤）/ violation_confirmed /
    #: manual（管理员手写，用于引文/图片描述/上下文来源这些 violations 里根本没有的
    #: 边界样本）。
    source_kind: Mapped[str] = mapped_column(String(24), default="manual")
    #: 导入来源的 ``violations.id``；手写样本为 NULL。
    source_violation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("violations.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 脱敏后的正文：没有 Telegram user id、没有 @username、没有电话号码，
    #: 长度按送审口径（``clean_text`` 的 1200 字符）截断。
    redacted_text: Mapped[str] = mapped_column(Text, default="")
    redacted_context: Mapped[str] = mapped_column(Text, default="")
    #: violation | benign | NULL（无标注）
    gold_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: unlabelled | labelled。只有人工显式确认/修正过才是 labelled。
    label_state: Mapped[str] = mapped_column(
        String(16), default="unlabelled", server_default="unlabelled", nullable=False
    )
    #: 标注来源：admin_ban（管理员在证据卡上按过"确认封禁 · 判定准确"）/
    #: manual_confirm（回放里显式确认或修正）。**没有任何机器信号会出现在这里。**
    label_source: Mapped[str] = mapped_column(String(24), default="", server_default="")
    #: 谁、什么时候把这条样本定成金标（``label_state='labelled'`` 时才有值）。
    label_reviewed_by: Mapped[int] = mapped_column(BigInteger, default=0)
    label_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: **非金标**的来源信息：导入来源的 ``review_state`` / ``confidence`` /
    #: ``action_taken``，以及机器信号"建议"的标签（明确标成 suggestion）。
    #: 界面上只作为 provenance 展示，**任何统计都不读它**。
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    #: 留存上界。过期样本不进新回放，由 ``purge_expired_samples`` 清理。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_moderation_replay_samples_group_created",
            "group_id",
            "created_at",
        ),
        # 同一群同一条 violation 只导一次：重复导入天然幂等。
        Index(
            "ix_moderation_replay_samples_group_violation",
            "group_id",
            "source_violation_id",
            unique=True,
        ),
        Index(
            "ix_moderation_replay_samples_expires_at",
            "expires_at",
        ),
    )


class ModerationReplayRun(Base):
    """一次回放：冻结的基线 + 一份版本化的候选提案 + 统计结果。

    ``baseline_snapshot`` 在建 run 的那一刻冻结"当时生效的群规 + 审核提示词"，
    之后管理员怎么改群规/改 prompt 都不影响这一轮 —— 否则"改配置时顺手跑回放"
    会得到一份既不是旧口径也不是新口径的对照结果。
    """

    __tablename__ = "moderation_replay_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    baseline_version: Mapped[str] = mapped_column(String(64), default="")
    #: sha256(canonical_json(baseline_snapshot))，可与快照逐字节对账。
    baseline_digest: Mapped[str] = mapped_column(String(64), default="")
    baseline_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_version: Mapped[str] = mapped_column(String(64), default="")
    #: sha256(canonical_json({rules, prompt}))。
    candidate_digest: Mapped[str] = mapped_column(String(64), default="")
    candidate_rules: Mapped[list] = mapped_column(JSON, default=list)
    #: 空 = 候选沿用当前生效的提示词（只改规则）。否则必须是能过
    #: ``PromptSettingsConfig._validate_moderation_template`` 的模板。
    candidate_prompt: Mapped[str] = mapped_column(Text, default="")
    #: 建 run 那一刻冻结的**模型绑定**：provider / model / 请求参数 / api_base 摘要，
    #: 外加 ``digest``。执行时**只读这一份**，绝不回 ``settings`` 里现取 ——
    #: 否则"结果说模型 A 跑的"与"其实是模型 B 跑的"可以同时为真。
    #: **绝不包含任何 key / 价格。**
    model_binding: Mapped[dict] = mapped_column(JSON, default=dict)
    #: ``sha256(canonical_json(model_binding))``，可直接与上面那份逐字节对账。
    model_digest: Mapped[str] = mapped_column(String(64), default="")
    #: 回放的**保真度边界**（结果解读必须一起看）：
    #: ``model_digest`` / ``redaction_applied`` / ``context_absent`` /
    #: ``equivalent_to_production_replay``（恒为 False：样本没有原始上下文）。
    fidelity: Mapped[dict] = mapped_column(JSON, default=dict)
    #: queued | running | completed | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued", nullable=False
    )
    #: 取消**请求**：置位后 worker 会在两次模型调用之间看到它并停下。
    #: 单独一列而不是"读 status=='cancelled'"：这样"已请求取消但还没停下来"
    #: 与"已经停下来了"两件事在库里是可区分的。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    #: 执行租约：owner token + 过时间。CAS 抢占 ``queued -> running`` 时写入，
    #: 写完**立刻提交并释放连接**：模型调用期间**不持有任何写锁**。
    #: 租约过期的 running 行由下一次抢占接管；重启时由
    #: ``sweep_interrupted_runs`` 标记为 ``interrupted``。
    lease_owner: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 第几次执行（接管 lease 的接管者会 +1）：结果能说清"这一轮跑了几次尝试"。
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai_naive,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 客户端提供的幂等键（≤ 64 字符）：同键同候选 → 返回同一个 run；
    #: 同键不同候选 → 409，绝不静默覆盖。
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_moderation_replay_runs_group_created", "group_id", "created_at"),
        Index("ix_moderation_replay_runs_idempotency_key", "idempotency_key", unique=True),
    )


class ModerationReplayResult(Base):
    """一条样本 × 一轮回放的对照结果（基线一侧 + 候选一侧）。

    ``*_verdict`` 是**三值**：violation / benign / error。第三个值的存在是这条
    设计的核心：模型抛异常、超时、输出不可解析、空输出都必须显式落成 error +
    ``conclusive=False``，绝不允许被折成"审核通过"混进通过率。
    """

    __tablename__ = "moderation_replay_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("moderation_replay_runs.id", ondelete="CASCADE")
    )
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    sample_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("moderation_replay_samples.id", ondelete="CASCADE")
    )
    baseline_verdict: Mapped[str] = mapped_column(String(16), default="")
    baseline_conclusive: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_match_source: Mapped[str] = mapped_column(String(24), default="")
    baseline_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_error: Mapped[str] = mapped_column(String(32), default="", server_default="")
    baseline_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_verdict: Mapped[str] = mapped_column(String(16), default="")
    candidate_conclusive: Mapped[bool] = mapped_column(Boolean, default=False)
    candidate_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_match_source: Mapped[str] = mapped_column(String(24), default="")
    candidate_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_error: Mapped[str] = mapped_column(String(32), default="", server_default="")
    candidate_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 基线判定与候选判定不同（见 ``run_replay`` 的定义）。
    changed: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_moderation_replay_results_run_sample", "run_id", "sample_id"),
        Index(
            "ix_moderation_replay_results_group_run",
            "group_id",
            "run_id",
        ),
    )


class RuntimeConfigHistory(Base):
    """全局运行时配置的**变更历史**（一行 = 一次成功落库的 save / restore）。

    为什么必须单独一张表：``runtime_config`` 是单行"当前值"，被下一次保存整体
    覆盖。运营问"这条规则昨天是谁改的"时，库里已经没有任何线索了。审计链一旦
    断了，事后追责和事故复盘都做不了，所以每次成功保存都在**同一个事务**里
    追加一行（见 ``RuntimeConfigManager._commit_revision``：历史插入失败会连主表
    一起回滚，绝不允许"配置改了但查不到谁改的"）。

    三条硬约束（写进 schema，不靠应用层自觉）：

    * ``revision`` **唯一** —— 一个 revision 只能由一次变更产生；重复插入说明
      写入路径出了 bug，必须让事务炸掉而不是悄悄记两遍。
    * ``payload_redacted`` 只存**脱敏**快照：所有 secret 路径被替换成状态标记
      （``<redacted:configured>`` / ``<redacted:empty>``），不是掩码——掩码会把
      长度和前后缀漏出去。
    * ``summary`` 只有**数量 + 路径**，永远不含值；``secret_paths_changed`` 只有
      路径名，没有密钥内容。

    ``parent_revision`` = 变更前那一版的 revision（= 乐观锁的 expected 值）；
    ``restore_source_revision`` 只在 ``operation == "restore"`` 时有值。
    """

    __tablename__ = "runtime_config_history"

    __table_args__ = (
        # 一个 revision 只允许有一条历史：重复写入 = 写入路径有 bug。
        Index("ix_runtime_config_history_revision", "revision", unique=True),
        # 历史列表默认按时间倒序翻页。
        Index("ix_runtime_config_history_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_redacted: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 配置状态发生变化的 secret 路径名（**只有名字**，永远没有值）。
    secret_paths_changed: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )
    #: "save" | "restore"
    operation: Mapped[str] = mapped_column(String(16), default="save", nullable=False)
    #: 人类可读的一行摘要：只有计数和路径，绝不含值。
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 仅 ``operation == "restore"`` 时有值 = 被回滚到的那一版 revision。
    restore_source_revision: Mapped[int | None] = mapped_column(Integer, default=None)


# --------------------------------------------------------------------------- #
# 审核回放 · 金标审计
#
# 金标（gold label）决定 FP/FN，也就是"这轮改动到底准不准"的唯一答案键。
# 一旦金标能被机器信号改写，FP/FN 就变成模型自己给自己打分。所以每一次
# 金标的**设置 / 修正 / 清除**都必须留一行：谁、什么时候、从什么改成了什么。
# 这张表只存标签本身与元数据，**永远不存样本正文**（正文有 TTL，见样本表）。
# --------------------------------------------------------------------------- #


class ModerationReplayLabelAudit(Base):
    """一条样本的标注变更（append-only）。

    为什么必须有它：``moderation_replay_samples.gold_label`` 是**当前值**，被改一次
    就覆盖了。"这条样本当初是谁标的、后来有没有被改过"在库里会彻底消失，于是
    一份 FP/FN 报告说不清它的答案键是怎么来的 —— 那份报告也就不可复核。
    """

    __tablename__ = "moderation_replay_label_audits"

    __table_args__ = (
        Index("ix_moderation_replay_label_audits_sample", "sample_id", "changed_at"),
        Index("ix_moderation_replay_label_audits_group", "group_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("moderation_replay_samples.id", ondelete="CASCADE")
    )
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id"))
    #: violation | benign | NULL（清回无标注）。
    previous_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    new_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: admin_confirm | admin_correct | admin_clear | manual_create
    action: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    #: 操作人（``Admin.user_id`` / ``super_admin_id``），0 = 系统。
    actor_id: Mapped[int] = mapped_column(BigInteger, default=0)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive, server_default=func.now()
    )


class GroupBudget(Base):
    """单群可选功能的每日预算护栏（默认关闭）。

    只管**可选**与**后台可选**用途。safety 用途永远绕过这里的准入 —— 让审核
    因为预算不足而停下来，等于用省钱换掉了安全。

    默认值全部是"未设置"（NULL / enabled=False）：没有显式配置就不存在任何
    额度，也就不存在"看起来在保护但其实没保护"的假绿灯。

    ``daily_money_micro_limit`` 只在计价完整（provider_key/model 的四项单价
    都配齐且同币种）时才允许设置 —— 否则宁可不给 hard cap，也不能给一个算不
    出来的"金额上限"。
    """

    __tablename__ = "group_budgets"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    currency: Mapped[str] = mapped_column(String(8), default="")

    daily_attempt_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_token_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_money_micro_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: feature -> 每日可操作次数。非 LLM（TTS / 媒体 / 搜索）没有可靠账单字段，
    #: 用次数配额而不是假装有金额。
    operation_limits: Mapped[dict] = mapped_column(JSON, default=dict)

    #: 预警/暂停阈值（0..1）。80% 一次预警、100% 暂停，是本轮的设计默认值，
    #: 不是现网策略；改它只影告警与暂停，不影响已记的用量。
    warn_ratio: Mapped[float] = mapped_column(Float, default=0.8, server_default="0.8")
    pause_ratio: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")

    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive, onupdate=now_shanghai_naive
    )


class GroupBudgetDay(Base):
    """单群单自然日（Asia/Shanghai）的累计计数器，预留的 CAS 落点。

    预留必须在**一个条件 UPDATE** 里完成，而不是「SELECT 总量 → 判断 →
    INSERT」：后者在并发下会让 20 条同时看到「还剩 1」而全部通过。
    条件 UPDATE 由 SQLite 在写锁内原子求值，跨进程同样成立。
    """

    __tablename__ = "group_budget_days"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    budget_date: Mapped[str] = mapped_column(String(10), primary_key=True)

    used_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    used_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    used_money_micro: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 已发出但结果未知（超时/迟到）的 attempt：不退还预留。
    unknown_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    #: 操作次数（按 feature 分列会需要动态表，所以整列 JSON 累加）。
    used_operations: Mapped[dict] = mapped_column(JSON, default=dict)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive, onupdate=now_shanghai_naive
    )

    __table_args__ = (Index("ix_group_budget_days_date", "budget_date"),)


class GroupBudgetReservation(Base):
    """一次可选模型尝试的预留：发出去之前落库，结果未知时**不**退。

    为什么单独一张表而不是在计数器上做减法：预留有三种归宿（settled /
    released / unknown），只有 ``released``（发送前取消）才允许把额度还回去。
    把状态记在行上，才能在重启后区分「确实没发出去」和「发出去了但不知道结果」
    —— 后者若被当成前者退还，就等于白送额度。
    """

    __tablename__ = "group_budget_reservations"

    reservation_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    budget_date: Mapped[str] = mapped_column(String(10), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), default="optional")
    stage: Mapped[str] = mapped_column(String(32), default="")
    provider: Mapped[str] = mapped_column(String(48), default="")
    model: Mapped[str] = mapped_column(String(128), default="")

    reserved_prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reserved_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reserved_money_micro: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 计费采用**尝试时**的价格快照：改价不能追改已发生的账。
    price_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 完整的**不可变**价格快照（provider_key/model/币种/四段单价）。
    #:
    #: 只存版本号是不够的：目录改了又改回来时，按版本号去查会拿到另一份价格，
    #: 于是"发送时的价"被"结算时那一版的价"替换掉。这里把当时用到的四段单价
    #: 原样冻在行上，结算只读它。
    price_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="")

    status: Mapped[str] = mapped_column(String(16), default="reserved", server_default="reserved")
    actual_money_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_group_budget_reservations_open", "group_id", "budget_date", "status"),
        Index(
            "ix_group_budget_reservations_lease",
            "status",
            "lease_expires_at",
        ),
    )


class ModelPriceCatalog(Base):
    """价格目录的**版本化**持久化副本。

    只放在进程内存里是不够的：重启之后目录回到空，一个已经启用的金额上限会
    立刻失去计价依据，而界面上还显示"上限已设置"。这里按版本只增不改 ——
    改价 = 新版本，历史结算仍读各自预留行上冻结的快照。
    """

    __tablename__ = "model_price_catalog"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )


class LlmCostFlushBatch(Base):
    """一次台账落盘的**幂等标记**。

    提交成功之后进程被取消、或客户端在"提交已生效但响应没回来"时重试，都会让
    同一批数据**再写一次** —— 于是 attempts 翻倍、token 翻倍。标记与计数在
    **同一个事务**里落：要么都成，要么都没成。重试看到标记已存在就整批跳过，
    于是"提交后取消"不会重复扣，"提交前失败"仍然可以补记
    """

    __tablename__ = "llm_cost_flush_batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )


class BudgetAlertEvent(Base):
    """预算告警的**持久 outbox**：先落库，后发送。

    幂等键是 (群, Asia/Shanghai 自然日, 额度类型, 阈值档) —— 所以"一天里
    某群的 attempt 上限越过 80%"只可能产生**一条**事件，不管它被触发多少次、
    并发多少路。内存 cooldown（``ops_alert`` 那套）做不到这件事：进程一重启
    就忘了，而且它按 kind 去重，跨群也会互相吞。

    发送**不在**本事务里：Telegram 是网络调用，放在预算短事务里会让安全审核
    去等一个外部 IO。消费端在写事务外取事件、拿到租约、发消息、成功后才回写
    确认；失败留在表里可见并可重试。

    Telegram 没有幂等发送 API，所以"发送成功但回写确认前崩溃"这个窗口无法
    证明绝对不重复。本实现把它显式标成 ``unknown`` 并**不再重发**（宁可少一条，
    也不无穷 spam），而不是假装 exactly-once。
    """

    __tablename__ = "budget_alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    budget_date: Mapped[str] = mapped_column(String(10), nullable=False)
    #: attempt / token / money / operation
    limit_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: operation 额度时具体是哪个功能（tts / search / …）。其余额度类型为空串。
    #:
    #: 它是**独立一列并进唯一键**，不是拼进 ``limit_kind``：拼接要么被 String(16)
    #: 截断、让两个 feature 碰撞，要么得另设长度上限 —— 而项目当前**没有**任何
    #: feature 长度校验（``save_budget`` 只检查非空与非负整数），凭空定一个上限
    #: 就会静默把长 feature 裁成同一个身份。故用 Text：SQLite 不强制长度，
    #: 任何合法 feature 都原样进库、原样进索引，不截断、不碰撞。
    feature: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: warn（默认 80%）/ pause（100%）
    threshold_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_value: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    #: pending / sending / sent / failed / unknown
    status: Mapped[str] = mapped_column(String(8), default="pending", server_default="pending")
    #: 「已经开始把消息交给 Telegram」。崩溃在这个点之后，重启**不能**自动重发：
    #: 消息可能已经送到管理员，重发就是第二条一样的私聊。租约恢复据此区分
    #: 「还没发」与「发了但确认不明」。
    dispatch_started: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str] = mapped_column(String(32), default="", server_default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    summary: Mapped[str] = mapped_column(String(300), default="", server_default="")
    last_error: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ux_budget_alert_daily_threshold",
            "group_id", "budget_date", "limit_kind", "feature", "threshold_kind",
            unique=True,
        ),
        Index("ix_budget_alert_pending", "status", "next_attempt_at"),
    )


class OperationsRun(Base):
    """运营任务中心的一次**真实执行请求**（第三优先 B13a / C4 写侧）。

    这张表是「确认」的持久证据：一行 = 一次被确认过的执行请求。它存在的理由是
    把三件容易混淆的事分开：

    * ``preview`` 只读，**不写这张表**；
    * ``confirm`` 写一行 ``accepted`` 并返回 ``run_key``，**不代表任务已经完成**；
    * 任务是否真的做完，只看 ``status`` 与 ``OperationsRunItem`` 的逐项结果，
      以及业务台账（``checkin_reminder_posts`` / ``weekly_report_posts`` /
      ``GroupSummary`` …）的对账结论。

    ``token_id`` 上的唯一索引是**单次确认**的硬防线：同一个预览令牌无论被确认
    多少次、被谁确认，都只可能插入一行；第二次确认读到既有行就返回**同一个 run**，
    不会再执行一次业务动作。

    ``guard_revision`` 是确认那一刻的运行时配置版本，只作审计证据；真正的守卫
    复核发生在实际写入的同一个事务/写锁窗口里（见 ``bot/services/operations_runs.py``）。

    审计是**只追加**的：本模块没有任何删除/清理路径，也不猜测保留期。
    """

    __tablename__ = "operations_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 对外稳定 run_id（随机 hex），审计与回执都用它。
    run_key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    #: 预览令牌 id：单次确认幂等键。
    token_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: admin | superadmin（确认那一刻的角色快照）
    actor_role: Mapped[str] = mapped_column(String(16), default="", server_default="")
    #: 规范参数（只放任务参数，绝不放令牌/密钥/私聊内容）
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 确认时的运行时配置版本（证据，不是守卫）
    guard_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 确认那一刻的**按群策略版本**（B13b）：来源切换/周期计划改动后，
    #: 旧确认的实际不可逆准入会被守卫谓词挡住。0 = 该任务当时没有策略行。
    policy_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: manual | retry | schedule（schedule = 周期计划自动触发）
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    #: 重试来源 run（同一业务身份的重试）
    retry_of_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 确认时预览里选中的**目标快照**（稳定 ``item_key`` 列表）。
    #: 执行只允许处理这些键，绝不用"当前集合"重新算出别的目标。
    target_keys: Mapped[list] = mapped_column(JSON, default=list)
    #: accepted|running|succeeded|partial|failed|cancelled|unknown
    status: Mapped[str] = mapped_column(String(16), default="accepted", server_default="accepted")
    #: 机器可读原因（no_work/disabled/backoff/queue_full/... ）
    reason: Mapped[str] = mapped_column(String(48), default="", server_default="")
    detail: Mapped[str] = mapped_column(String(300), default="", server_default="")
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    items_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_succeeded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: 「未知外部结果」——绝不允许被安全重试自动重发。
    items_unknown: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_operations_runs_group_created", "group_id", "created_at"),
        Index("ix_operations_runs_task_status", "task_id", "status"),
    )


class OperationsRunItem(Base):
    """run 的**逐项结果**：一行 = 一个稳定业务键的处理结果。

    ``item_key`` 是稳定身份（例如 ``scheduled:entry=12``、
    ``checkin:group=..:slot=2026-10-01:9``、``weekly:week=2026-W40``、
    ``shop:entitlement=33``、``summary:group=..``）。同一 run 内 ``(run_id,
    item_key)`` 唯一，所以重复写同一项只会更新那一行，不会产生第二条记录。

    ``resendable`` 是**写侧结论**，不是 UI 建议：
    * ``True``  = 明确尚未出站 / 业务已证明幂等且可恢复（允许安全重试）；
    * ``False`` = 已知成功、可能已出站、或外部结果未知（**禁止**自动重发，
      管理员确认也不覆盖这条硬边界）。

    ``detail`` 只允许安全文案：绝不写异常原文、令牌、密钥、私聊内容或原文文本。
    """

    __tablename__ = "operations_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("operations_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 规范目标（群/条目/时段/周等），审计用。
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    #: succeeded|skipped|failed|unknown|cancelled
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    reason: Mapped[str] = mapped_column(String(48), default="", server_default="")
    detail: Mapped[str] = mapped_column(String(300), default="", server_default="")
    #: 真实出站 message_id（0 = 没有/未知）。
    external_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    resendable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)

    __table_args__ = (
        UniqueConstraint("run_id", "item_key", name="ux_operations_run_items_run_key"),
        Index("ix_operations_run_items_run_status", "run_id", "status"),
    )


class OperationsBusinessReceipt(Base):
    """一次**真实出站**的持久业务 receipt（B13a 返工 R1）。

    这张表回答的问题是 run 审计表回答不了的：**"这个业务键到底有没有发出去过"**。
    ``operations_run_items`` 只记录某一次 run 的结论，进程死在"发送"与"记账"之间、
    审计写失败、或者换个入口（常驻 runner / CLI / 运营中心）重跑时，run 审计帮不上
    忙；而没有这条证据，所有入口都会把"已经出站"的消息再发一遍。

    因此 receipt 的键是**稳定业务身份**，不是 run id：

    * ``checkin_reminder`` → ``(group_id, slot_key)``
    * ``scheduled_message`` → ``(group_id, "<entry_id>:<occurrence_at>")``
    * ``weekly_report`` → ``(group_id, "<week_key>")``
    * ``shop_expire`` → ``(group_id, "revoke:<entitlement_id>:<expires_at>")`` 与
      ``(group_id, "notify:<entitlement_id>:<expires_at>")``（撤销与通知**分开**：
      撤销可幂等重做，通知绝不能被未知结果解锁后重发）

    ``state`` 只有两态，且**永不 downgrade**：

    * ``sent``——发送方拿到了真实的出站结果（``external_message_id`` 可能仍为 0）；
    * ``unknown``——出站结果未知（超时/断连/记账失败）。**未知同样禁止重发**。

    写入必须发生在"真实出站动作返回之后、业务台账提交之前"的**独立事务**里：这样
    业务侧 commit 失败也不会把这条证据一起回滚掉。表只追加，
    :func:`bot.services.operations_receipts.clear_receipt` 只在"明确未出站"的
    恢复路径上删行。
    """

    __tablename__ = "operations_business_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 稳定业务键（含足够的作用域维度，见类 docstring）。
    business_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: sent | unknown
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 真实出站 message_id（0 = 没有/未知）。
    external_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    #: 只允许安全文案（绝不写异常原文/令牌/私聊内容）。
    detail: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai_naive
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "group_id",
            "business_key",
            name="ux_operations_receipts_identity",
        ),
        Index("ix_operations_receipts_group_task", "group_id", "task_id"),
    )


class GroupTaskPolicy(Base):
    """每个群、每类运营任务的**执行来源与周期计划**（第三优先 B13b / C4）。

    为什么单独一张表，而不是塞进 ``Group.settings``：来源切换与周期计划需要
    **可比较的版本号**才能做到"预览时钉住版本 → 确认时同一条 SQL 原子抢占 →
    切换后旧确认在真实不可逆准入被挡住"。JSON 文档没有行级版本，``revision``
    自增的 CAS 才是线性化点。

    语义（与 runner / CLI / 只读状态共用同一份真源）：

    * ``source``：这条任务的**真实执行来源**。``internal`` = 进程内 runner；
      ``external`` = 宿主 cron 调用的 CLI。默认值按任务类型给（见
      ``bot/services/task_policy.py``），一行都没有时读默认值，不写空行。
    * ``active_from``：策略**生效时刻**。切换来源不补历史：内部 runner 只处理
      ``active_from`` 之后到点的时段/周次。
    * ``plan_enabled`` / ``plan_weekday`` / ``plan_minute`` / ``anchor_at``：
      周期计划。``anchor_at`` 是**持久化的下一次计划时刻**，由与开启时同一个
      ``next_occurrence`` 函数算出；重启从它恢复，不猜 ``now + 间隔``。
    * ``revision``：每次成功写入 +1。守卫谓词要求"当前 revision == 确认时钉的
      revision"，所以确认之后改来源/计划会真实阻断旧执行的写入与出站。
    """

    __tablename__ = "group_task_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: internal | external（空串 = 跟随任务默认值）
    source: Mapped[str] = mapped_column(String(16), default="", server_default="")
    plan_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    #: 0 = 周一 … 6 = 周日（Asia/Shanghai）
    plan_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 当天 0 点后的分钟数（0..1439，Asia/Shanghai）
    plan_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 持久化的下一次计划时刻（由 next_occurrence 算出，不猜间隔）
    anchor_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 本行策略生效时刻（不补历史）
    active_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)

    __table_args__ = (
        UniqueConstraint("group_id", "task_id", name="ux_group_task_policies_identity"),
        Index("ix_group_task_policies_task_plan", "task_id", "plan_enabled"),
    )


class GroupTaskPolicyEvent(Base):
    """来源切换与周期计划变更的**只追加审计**（B13b）。

    只记安全文案与规范值：群、任务、动作、变更前后的策略快照、操作者与版本。
    绝不写令牌、密钥、异常原文或私聊内容。本表没有任何删除/清理路径。
    """

    __tablename__ = "group_task_policy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: source_switch | plan_enable | plan_disable
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    #: 变更后的策略版本
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai_naive)

    __table_args__ = (
        Index("ix_group_task_policy_events_group_created", "group_id", "created_at"),
    )
