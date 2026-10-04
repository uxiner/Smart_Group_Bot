from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Iterable
from typing import Any, Callable

from bot.config import (
    load_bootstrap_settings,
    log_enforcement_switch_state,
    validate_bootstrap_settings,
)
from bot.db.engine import init_db
from bot.handlers import admin, commands, group, membership, private_chat
from bot.loader import create_bot, dp
from bot.middlewares.command_cleanup import ManagementCommandCleanupMiddleware
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.member_roster import MemberRosterMiddleware
from bot.middlewares.update_dedup import DurableInboxUpdateDedupMiddleware
from bot.middlewares.verification_gate import PendingVerificationGateMiddleware
from bot.services import llm_metrics, memory_holder, model_limits
from bot.services.archive_vector import SQLiteArchiveVectorRecallProvider
from bot.services.authz import warm_privileged_operator_cache
from bot.services.join_verification import (
    JoinVerificationSweeper,
    VERIFICATION_PROVIDERS,
    flush_kick_cleanup_tasks,
    verification_service_ready,
    warn_if_bot_cannot_verify,
)
from bot.services.group_summary import (
    GROUP_SUMMARY_SCHEDULER,
    group_summary_config,
    init_group_summary_scheduler,
)
from bot.services.llm import _LLM_PRIORITY_GATE
from bot.services.request_priority import ExecutionPriority
from bot.services.group_permissions import (
    GroupPermissionService,
    init_group_permission_service,
)
from bot.services.llm import LLMService, close_llm_clients, flush_llm_request_tasks
from bot.services.memory import MemoryService
from bot.services.patrol import PatrolService, init_patrol_service
from bot.services.point_shop import ShopExpiryService
from bot.services.private_chat import (
    private_history_retention_days,
    run_private_chat_history_maintenance,
)
from bot.services.raid_guard import RaidGuardService, init_raid_guard_service
from bot.services.proactive import ProactiveTopicService
from bot.services.privileged_tasks import flush_privileged_tasks
from bot.services.resource_health import (
    run_resource_watchdog,
    start_hard_loop_watchdog,
)
from bot.services.search_memory import (
    run_search_record_maintenance,
    search_record_retention_days,
)
# 第 4 期：长期记忆的写入与维护（两条后台巡检 + 留存天数读取）
from bot.services.long_term_memory import (
    memory_deleted_retention_days,
    run_long_term_memory_extraction,
    run_long_term_memory_maintenance,
)
from bot.services.runtime_config import (
    RuntimeConfig,
    RuntimeConfigManager,
    hcaptcha_key_configuration_issue,
    turnstile_key_configuration_issue,
)
from bot.services.scheduled_messages import ScheduledMessageService
from bot.services.skills.service import flush_skill_execution_tasks
from bot.services.telegram_cleanup import TelegramCleanupScheduler
from bot.services.vote_ban import flush_vote_ban_tasks, restore_vote_ban_tasks
from bot.services.update_delivery import (
    flush_update_delivery_tasks,
    resolve_webhook_config,
    run_update_delivery,
)
from bot.utils.bot_identity import set_bot_identity
from bot.utils.command_catalog import build_bot_commands
from bot.utils.logging_setup import configure_logging
from bot.utils.telegram import (
    configure_telegram_cleanup_scheduler,
    flush_telegram_background_tasks,
)

configure_logging()
log = logging.getLogger(__name__)

_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_CLEANUP_TIMEOUT_SECONDS = 20.0
# The reply-batch budget is configurable up to 120 seconds.  Leave enough
# room for its own bounded failure notification/cleanup instead of cancelling
# a healthy flush at the old 30-second outer limit.
_PENDING_FLUSH_TIMEOUT_SECONDS = 35.0
_WEB_SERVER_CLEANUP_TIMEOUT_SECONDS = 20.0
_CANCELLATION_GRACE_SECONDS = 1.0
_FINAL_LOOP_DRAIN_SECONDS = 2.0
_FORCED_PROCESS_EXIT_SECONDS = 5.0
_ORDERED_SHUTDOWN_HARD_LIMIT_SECONDS = 110.0


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.error(
            "Detached task finished after shutdown | task=%s error=%s",
            task.get_name(),
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


async def _cancel_tasks_bounded(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    timeout: float = _BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    all_tasks = list(tasks)
    pending_tasks = [task for task in all_tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    if not all_tasks:
        return
    done, pending = await asyncio.wait(all_tasks, timeout=timeout)
    for task in done:
        _consume_task_result(task)
    if pending:
        log.error(
            "%s shutdown exceeded %.1fs; tasks still running=%s",
            label,
            timeout,
            ",".join(sorted(task.get_name() for task in pending)),
        )
        for task in pending:
            task.add_done_callback(_consume_task_result)


async def _await_cleanup_bounded(
    awaitable: Awaitable[Any],
    *,
    label: str,
    timeout: float = _CLEANUP_TIMEOUT_SECONDS,
) -> None:
    task = asyncio.create_task(awaitable, name=f"shutdown-{label}")
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        try:
            await task
        except asyncio.CancelledError:
            log.error("%s cleanup was unexpectedly cancelled", label)
        except Exception:
            log.exception("%s cleanup failed", label)
        return

    log.error("%s cleanup exceeded %.1fs; cancelling", label, timeout)
    task.cancel()
    done, _pending = await asyncio.wait(
        {task},
        timeout=_CANCELLATION_GRACE_SECONDS,
    )
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


async def _run_with_background_supervision(
    delivery: Awaitable[str],
    *,
    background_tasks: list[asyncio.Task[Any]],
) -> str:
    """Fail the process when a supposedly permanent service exits."""
    delivery_task = asyncio.create_task(delivery, name="telegram-update-delivery")
    try:
        done, _pending = await asyncio.wait(
            {delivery_task, *background_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if delivery_task in done:
            return await delivery_task

        failed = next(task for task in done if task is not delivery_task)
        if failed.cancelled():
            detail = "was unexpectedly cancelled"
            error: BaseException | None = None
        else:
            error = failed.exception()
            detail = (
                f"failed: {error}" if error is not None else "exited unexpectedly"
            )
        await _cancel_tasks_bounded(
            [delivery_task],
            label="update delivery after background failure",
        )
        failure = RuntimeError(
            f"background service {failed.get_name()} {detail}"
        )
        if error is not None:
            raise failure from error
        raise failure
    finally:
        if not delivery_task.done():
            await _cancel_tasks_bounded(
                [delivery_task],
                label="update delivery",
            )


async def _drain_final_loop_tasks(*, timeout_seconds: float) -> None:
    current = asyncio.current_task()
    tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    for task in done:
        _consume_task_result(task)
    if pending:
        log.critical(
            "event loop closing with %d cancellation-resistant task(s): %s",
            len(pending),
            ",".join(sorted(task.get_name() for task in pending)),
        )
        # Loop.close() must not emit a warning for tasks we deliberately abandon
        # after all shared Bot/DB resources have already been closed.
        for task in pending:
            setattr(task, "_log_destroy_pending", False)


def _arm_forced_exit_watchdog(
    timeout_seconds: float = _FORCED_PROCESS_EXIT_SECONDS,
) -> threading.Event:
    stopped = threading.Event()

    def _force_exit() -> None:
        if not stopped.wait(max(0.1, float(timeout_seconds))):
            os._exit(1)

    threading.Thread(
        target=_force_exit,
        name="bot-shutdown-watchdog",
        daemon=True,
    ).start()
    return stopped


def run_async_entrypoint(
    awaitable: Awaitable[Any],
    *,
    force_exit_watchdog: bool = False,
) -> Any:
    """Run the process loop without asyncio.run's unbounded final gather."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hard_loop_watchdog = start_hard_loop_watchdog(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        if force_exit_watchdog:
            # main() already ran all ordered resource cleanup. If interpreter
            # shutdown (typically a stuck executor/provider) still cannot finish,
            # guarantee the service manager can restart this process.
            _arm_forced_exit_watchdog()
        try:
            loop.run_until_complete(
                _drain_final_loop_tasks(timeout_seconds=_FINAL_LOOP_DRAIN_SECONDS)
            )
        finally:
            asyncio.set_event_loop(None)
            try:
                loop.close()
            finally:
                hard_loop_watchdog.set()


def run_bot() -> None:
    # SQLite WAL files, runtime logs and any future secret-bearing artifacts
    # created by the service should default to owner-only permissions. Explicit
    # chmods still cover files opened during import and existing deployments.
    os.umask(0o077)
    run_async_entrypoint(main(), force_exit_watchdog=True)


def _observe_model_metadata_refresh(
    task: asyncio.Task[Any],
    *,
    on_refreshed: Callable[[], None] | None = None,
    label: str = "model context metadata refreshed",
) -> None:
    """消费一次元数据刷新任务的结果：只记日志 + 回调，绝不抛。"""

    try:
        report = task.result()
    except (asyncio.CancelledError, Exception):
        return
    if report:
        log.info("%s | sources=%s", label, report)
    if on_refreshed is not None:
        try:
            on_refreshed()
        except Exception:
            log.exception("model metadata refresh callback failed")


class _MemoryContextWindowSync:
    """把"当前可信模型窗口"同步到 ``MemoryService`` 的预算上。

    为什么需要：``MemoryService`` 的预算是**构造时**从元数据缓存取快照算出来的。
    启动预取有 6 秒上限（超时就转后台），后台周期刷新也会带来新窗口；没有这条线，
    metadata 晚到或变化后，装配预算就要等重启才生效——那正是事故里"前面认为装得下、
    最终拒绝"的温床。

    只调既有的 ``memory.reconfigure(bot_config)``：它是幂等的，**不清历史、不改留存**，
    只重新算 token 预算并把已加载的投影按新上限收敛。
    """

    def __init__(self, *, settings: Any) -> None:
        self._settings = settings
        self._llm: Any | None = None
        self._memory: Any | None = None
        self._applied: int | None = None
        self.applied_count = 0

    @property
    def applied_window(self) -> int | None:
        return self._applied

    def bind(self, *, llm: Any, memory: Any) -> None:
        """接上已构造好的 llm/memory，并用当前缓存**再套一次**预算（幂等）。"""

        self._llm = llm
        self._memory = memory
        self._applied = None
        # 构造期间晚到的元数据可能已经写进缓存，但 memory 是用更早的快照算的预算；
        # 这里强制对齐一次，把那个窗口闭合掉。
        self.sync(force=True)

    def sync(self, *_args: Any, force: bool = False) -> None:
        memory = self._memory
        if memory is None:
            # 还在启动过程中（memory 尚未构造）：bind() 会补一次。
            return
        discovered = model_limits.auto_window_for(
            getattr(self._settings, "bot", None),
            llm=self._llm,
        )
        if discovered is None:
            return
        # 以**业务有效窗口**（min(模型窗口, 272Ki)）为基准：只有它真的变了才 reconfigure
        # ——模型从 1M 变成 4M 不会改变每轮预算，也就没必要动一次历史投影。
        resolved = int(model_limits.business_total_window(discovered))
        if not force and resolved == self._applied:
            return
        previous = self._applied
        try:
            reconfigure = getattr(memory, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(getattr(self._settings, "bot", None))
        except Exception:
            log.exception(
                "memory context budget re-apply failed | window=%s", resolved
            )
            return
        self._applied = resolved
        self.applied_count += 1
        log.info(
            "memory context budget re-applied after model window change | %s -> %s",
            previous if previous is not None else "-",
            resolved,
        )


async def _prefetch_model_context_metadata(
    llm: LLMService,
    *,
    on_late_refresh: Callable[[], None] | None = None,
) -> asyncio.Task[Any] | None:
    """启动时预热模型窗口元数据（2026-10-04 事故修复）。

    只查**已配置且带认证**的 endpoint（``api_base`` + key），单次查询 4 秒超时、
    最多 3 个并发。启动最多只等 6 秒：等不到就让它继续在后台跑（``shield``），
    期间主链路按保守降级值工作，拿到后通过 ``on_late_refresh`` 把新窗口套到
    MemoryService 的预算上。日志只打印来源与数值，绝不打印 key。

    返回**还没跑完**的那个任务（否则 ``None``），交给调用方决定后续观察方式。
    """

    loop = asyncio.get_running_loop()
    refresh_limits = getattr(llm, "refresh_model_limits", None)
    if not callable(refresh_limits):
        return None
    task = loop.create_task(refresh_limits(), name="model-metadata-prefetch")
    try:
        report = await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
    except Exception as exc:  # noqa: BLE001 - 启动不能因为元数据查询失败而失败
        log.info(
            "model context metadata prefetch still running in the background | note=%s",
            type(exc).__name__,
        )
    else:
        if report:
            log.info("model context metadata prefetch | sources=%s", report)
    if not task.done():
        task.add_done_callback(
            lambda done: _observe_model_metadata_refresh(
                done,
                on_refreshed=on_late_refresh,
                label="model context metadata prefetch finished in the background",
            )
        )
    limits_fn = getattr(llm, "endpoint_limits", None)
    limits = limits_fn(getattr(llm, "main", None)) if callable(limits_fn) else None
    if limits is not None:
        budget_fn = getattr(llm, "input_token_budget", None)
        budget = budget_fn(getattr(llm, "main", None)) if callable(budget_fn) else None
        log.info(
            "main chat context window resolved | %s | business_input_budget=%s",
            limits.describe(),
            budget if budget is not None else "-",
        )
    return None if task.done() else task


async def _initialize_runtime_services(
    *,
    settings: Any,
    session_factory: Any,
    on_late_model_refresh: Callable[[], None] | None = None,
) -> tuple[RuntimeConfigManager, LLMService, MemoryService]:
    # 成本看板的记数器只累加在内存里，这里给它会话工厂，
    # 由它自己按 60 秒的节奏落盘（回复路径上不做任何数据库写入）。
    llm_metrics.configure(session_factory)

    runtime_config = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
    )
    await runtime_config.initialize()
    configure_logging(force=True, config=runtime_config.config.logging)

    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
        context_window_mode=getattr(settings.bot, "context_window_mode", None),
        business_context_tokens=getattr(settings.bot, "context_budget_tokens", None),
        context_reserve_tokens=getattr(settings.bot, "context_reserve_tokens", None),
    )
    await _prefetch_model_context_metadata(
        llm,
        on_late_refresh=on_late_model_refresh,
    )
    main_candidates = llm._chat_candidates(settings.bot.main_model)
    if main_candidates and all(
        llm.chat_configuration_issue(item) for item in main_candidates
    ):
        log.warning(
            "AI 模型尚不可用：主模型及其回退均缺少所需凭据；"
            "请由最高管理员私聊 bot 发送 /settings 完成模型配置。"
        )
    if (
        getattr(settings.bot, "context_window_mode", "auto") == "fixed"
        and settings.bot.max_context_tokens <= 4096
    ):
        log.warning(
            "上下文上限模式为 fixed 且仅 %d Token，可能小于内置提示词；"
            "请在 /settings 的 Bot 行为中把「上下文上限模式」改为 auto"
            "（auto：自动发现模型窗口，每轮业务预算仍为 272Ki）或调高固定值。",
            settings.bot.max_context_tokens,
        )
    # Semantic archive recall is only useful when an embedding provider is
    # actually configured.  This deployment has none (pipio exposes no
    # embedding model and the local sub2api gateway only serves chat models),
    # so the indexer could never succeed: every archived message queued an
    # embedding job that failed and re-queued forever
    # (group_message_archive_embeddings reached 1891/1891 status='failed',
    # retried up to 173x).  Skip building the provider entirely when recall is
    # disabled so no indexer task runs and the retry churn stops.
    vector_recall_provider = (
        SQLiteArchiveVectorRecallProvider(
            session_factory=session_factory,
            llm=llm,
            retention_days=settings.bot.memory_retention_days,
            # The local llama.cpp embedding server shares two CPU cores with the
            # archive indexer, so a recall query that lands behind an indexing
            # batch can wait several seconds.  The built-in 2.5s query deadline
            # silently returned zero hits; 8s keeps recall reliable while the
            # indexer is busy, and smaller batches keep each wait short.
            query_timeout_seconds=8.0,
            batch_size=8,
        )
        if settings.bot.memory_recall_enabled
        else None
    )
    memory = MemoryService(
        settings.bot,
        llm,
        session_factory=session_factory,
        vector_recall_provider=vector_recall_provider,
    )
    await memory.bootstrap()
    memory_holder.init(memory)
    return runtime_config, llm, memory


def _register_update_middlewares(dispatcher: Any, session_factory: Any) -> None:
    """Install middleware on every update observer that needs its services."""

    dispatcher.update.outer_middleware(
        DurableInboxUpdateDedupMiddleware(session_factory)
    )
    dispatcher.message.outer_middleware(
        GlobalBanEnforcementMiddleware(session_factory)
    )
    # A member racing the join-verification mute must not get messages through
    # (or consume profile-screening LLM capacity) before the challenge exists.
    dispatcher.message.outer_middleware(
        PendingVerificationGateMiddleware(session_factory)
    )
    # Roster tracking feeds the profile patrol; outer so every sender is seen.
    dispatcher.message.outer_middleware(MemberRosterMiddleware(session_factory))
    # Operators' own command lines are noise once served: drop them from the
    # group a few seconds later.  Outer so a rejected update is still cleaned.
    # The session factory is used to refuse deleting a member's look-alike line.
    dispatcher.message.outer_middleware(
        ManagementCommandCleanupMiddleware(session_factory)
    )
    dispatcher.message.middleware(LoggingMiddleware())
    # No throttle middleware: it silently drops rapid consecutive messages,
    # which breaks inbound batch merging and lets a fast second violating
    # message skip moderation. Burst smoothing is handled by the pending-reply
    # debounce in bot.handlers.group instead.
    dispatcher.message.middleware(DbSessionMiddleware(session_factory))
    # edited_message is a separate aiogram observer; message middleware is not
    # inherited by edit handlers that also require a database session.
    #
    # The two enforcement gates are registered for edits as well (F-008): a
    # member could post something harmless and then edit it into an advert, and
    # the edit reached only the archival handler — no ban check, no
    # verification gate, no moderation. Same order as the message observer:
    # banned senders first, then unverified ones.
    dispatcher.edited_message.outer_middleware(
        GlobalBanEnforcementMiddleware(session_factory)
    )
    dispatcher.edited_message.outer_middleware(
        PendingVerificationGateMiddleware(session_factory)
    )
    dispatcher.edited_message.middleware(DbSessionMiddleware(session_factory))
    dispatcher.callback_query.middleware(DbSessionMiddleware(session_factory))
    dispatcher.chat_member.middleware(DbSessionMiddleware(session_factory))
    dispatcher.my_chat_member.middleware(DbSessionMiddleware(session_factory))


async def _publish_bot_command_menu(bot: object) -> None:
    """Mirror the /help catalog into Telegram's "/" menu.

    The menu was maintained by hand and had drifted empty, which is how a rarely
    used command like /exemptlist gets forgotten.  Publishing from the catalog
    keeps both surfaces in step.  A failure here must never block startup - the
    bot works fine without a menu.
    """
    from aiogram.types import BotCommand

    try:
        commands = [
            BotCommand(command=name, description=description)
            for name, description in build_bot_commands()
        ]
        if not commands:
            return
        await bot.set_my_commands(commands)  # type: ignore[attr-defined]
        log.info("Telegram command menu published: %d entries", len(commands))
    except Exception as exc:
        log.warning("Telegram command menu publish failed: %s", exc)


async def main() -> None:
    settings = load_bootstrap_settings()
    validate_bootstrap_settings(settings)
    # F-025：用户图片会不会离开本服务，必须在启动日志里可见（默认不外发）。

    engine, session_factory = await init_db(settings.database_url)
    # 模型窗口元数据晚到/变化时，把新窗口套到 MemoryService 的预算上（不重启也要生效）。
    # MemoryService 的预算是构造时的快照：启动预取超时转后台、周期刷新带来新窗口，
    # 都必须走这一条线才算真的生效。
    window_sync = _MemoryContextWindowSync(settings=settings)
    try:
        runtime_config, llm, memory = await _initialize_runtime_services(
            settings=settings,
            session_factory=session_factory,
            on_late_model_refresh=window_sync.sync,
        )
    except BaseException:
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after runtime bootstrap failure",
        )
        raise
    window_sync.bind(llm=llm, memory=memory)

    # 第②项：后台群摘要调度器（进程级单例）。前台只登记、只读已发布摘要；
    # 真正的模型调用走 BACKGROUND 优先级与独立容量（≤2、与回复共享 normal=4）。
    summary_store_factory = getattr(memory, "group_summary_store", None)
    summary_scheduler = (
        init_group_summary_scheduler(
            llm=llm,
            store=summary_store_factory(),
            config_provider=lambda: group_summary_config(settings.bot),
            slot_waiter=lambda: _LLM_PRIORITY_GATE.has_waiting(
                ExecutionPriority.NORMAL
            ),
        )
        if callable(summary_store_factory)
        else None
    )

    dp["settings"] = settings
    # F-024：运行时配置已经套用到 settings，这里把"对用户可见的执法开关"的生效
    # 状态写进启动日志（opt-in 默认关闭；只要有开启就 WARNING 列出）。
    log_enforcement_switch_state(settings)
    dp["session_factory"] = session_factory
    dp["runtime_config"] = runtime_config
    warmed_operators = await warm_privileged_operator_cache(session_factory)
    if warmed_operators:
        log.info("Preloaded %d delegated privileged operator scope(s)", warmed_operators)

    _register_update_middlewares(dp, session_factory)

    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(membership.router)
    # 私聊 router 必须排在 group 之前：群处理器用的是宽泛过滤（F.text|F.photo|…）
    # 并在函数体里 is_group() 早退，谁先注册谁先吃消息，排后面就永远收不到私聊。
    dp.include_router(private_chat.router)
    dp.include_router(group.router)

    try:
        bot = create_bot(settings)
    except BaseException:
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after Bot construction failure",
        )
        raise
    telegram_cleanup: TelegramCleanupScheduler | None = None
    try:
        me = await bot.me()
        set_bot_identity(
            user_id=me.id,
            username=me.username or "",
            display_name=me.full_name or me.first_name or "",
        )
        log.info("Bot identity resolved: @%s (%s)", me.username, me.full_name)
        await _publish_bot_command_menu(bot)
        telegram_cleanup = TelegramCleanupScheduler(
            bot=bot,
            session_factory=session_factory,
        )
        configure_telegram_cleanup_scheduler(telegram_cleanup)
        await telegram_cleanup.start()
        await restore_vote_ban_tasks(
            session_factory=session_factory,
            bot=bot,
            settings=settings,
        )
    except BaseException:
        configure_telegram_cleanup_scheduler(None)
        if telegram_cleanup is not None:
            await _await_cleanup_bounded(
                telegram_cleanup.stop(),
                label="Telegram cleanup after identity/bootstrap failure",
            )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session after identity/bootstrap failure",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after identity/bootstrap failure",
        )
        raise
    background_tasks: list[asyncio.Task[Any]] = []
    try:
        proactive = ProactiveTopicService(
            settings=settings,
            bot=bot,
            memory=memory,
            session_factory=session_factory,
            llm=llm,
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=session_factory,
            check_interval_seconds=settings.join_verification_check_interval_seconds,
            settings=settings,
        )
        patrol = PatrolService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        init_patrol_service(patrol)
        scheduled_messages = ScheduledMessageService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        group_permissions = GroupPermissionService(
            bot=bot,
            session_factory=session_factory,
        )
        init_group_permission_service(group_permissions)
        # 积分商店买来的头衔/置顶到期后要自动撤掉：常驻循环跑同一套逻辑，
        # 运维也可以随时用 python -m bot.tools.shop_expire 手动补一次。
        shop_expiry = ShopExpiryService(
            bot=bot,
            session_factory=session_factory,
        )
        # Event-driven (no background loop): joins feed the detector from the
        # membership handler; challenge deadlines ride the shared sweeper.
        raid_guard = RaidGuardService(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
        )
        init_raid_guard_service(raid_guard)
        logging_config_snapshot = runtime_config.config.logging.model_dump(mode="json")

        async def apply_runtime_update(_config: RuntimeConfig) -> None:
            nonlocal logging_config_snapshot
            # Keep sends that use aiogram's default properties in sync with
            # the live Bot behavior setting.  Per-message/template paths pass
            # their own value explicitly and therefore remain authoritative.
            bot_default = getattr(bot, "default", None)
            if bot_default is not None:
                # Per-message paths use disable_web_page_preview.  Clear the
                # newer aggregate default so the two Telegram parameters can
                # never disagree when a per-item switch overrides the global
                # Bot behavior value.
                bot_default.link_preview = None
                bot_default.link_preview_is_disabled = bool(
                    getattr(settings.bot, "disable_link_preview", True)
                )
            llm.reconfigure(
                settings.bot.main_model,
                settings.bot.decision_model,
                settings.bot.compress_model,
                skill=settings.bot.skill_model,
                moderation=settings.bot.moderation_model,
                vision=settings.bot.vision_model,
                embed=settings.bot.embed_model,
                max_context_tokens=settings.bot.max_context_tokens,
                context_window_mode=getattr(settings.bot, "context_window_mode", None),
                business_context_tokens=getattr(settings.bot, "context_budget_tokens", None),
                context_reserve_tokens=getattr(settings.bot, "context_reserve_tokens", None),
            )
            # 路由/模型/上限模式变了：异步重取一次窗口元数据（不阻塞配置应用，
            # 失败就继续用保守降级值），拿到新窗口后再套一次 memory 预算。
            refresh_limits = getattr(llm, "refresh_model_limits", None)
            if callable(refresh_limits):
                try:
                    refresh_task = asyncio.get_running_loop().create_task(
                        refresh_limits(force=True),
                        name="model-metadata-refresh-after-config",
                    )
                    refresh_task.add_done_callback(
                        lambda done: _observe_model_metadata_refresh(
                            done,
                            on_refreshed=window_sync.sync,
                            label="model context metadata refreshed after config change",
                        )
                    )
                except RuntimeError:
                    pass
            # 摘要相关配置变化：唤醒后台循环（新容量对**新任务**生效，不动已入场许可）。
            summary_scheduler = GROUP_SUMMARY_SCHEDULER
            if summary_scheduler is not None:
                summary_scheduler.reconfigure()
            archive_policy_before = (
                getattr(memory, "memory_retention_days", None),
                getattr(memory, "memory_archive_max_messages_per_group", None),
            )
            memory.reconfigure(settings.bot)
            archive_policy_after = (
                getattr(memory, "memory_retention_days", None),
                getattr(memory, "memory_archive_max_messages_per_group", None),
            )
            if archive_policy_after != archive_policy_before:
                prune_archive = getattr(
                    memory,
                    "prune_expired_archive_globally",
                    None,
                )
                if callable(prune_archive):
                    await prune_archive()
            sweeper.check_interval_seconds = max(
                5.0,
                float(settings.join_verification_check_interval_seconds),
            )
            next_logging_config = runtime_config.config.logging.model_dump(mode="json")
            if next_logging_config != logging_config_snapshot:
                configure_logging(force=True, config=runtime_config.config.logging)
                logging_config_snapshot = next_logging_config

        runtime_config.set_apply_callback(apply_runtime_update)

        from bot.services.verify_web import VerifyWebServer

        webhook, webhook_fallback_reason = resolve_webhook_config(settings)
        verify_web = VerifyWebServer(
            bot=bot,
            settings=settings,
            session_factory=session_factory,
            runtime_config=runtime_config,
            # The durable inbox must remain consumable even when this boot
            # starts directly in polling mode (for example after a previous
            # webhook ACK but before its handler completed).
            webhook_dispatcher=dp,
            webhook_path=webhook.path if webhook is not None else "",
            webhook_secret=webhook.secret if webhook is not None else "",
        )
    except BaseException:
        # No runner has been scheduled yet, so construction failure cleanup is
        # limited to the shared resources that are already live.
        configure_telegram_cleanup_scheduler(None)
        await _await_cleanup_bounded(
            telegram_cleanup.stop(),
            label="Telegram cleanup after service construction failure",
        )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session after service construction failure",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine after service construction failure",
        )
        raise
    try:
        await raid_guard.restore_manual_lockdowns()
        await verify_web.start()

        if verify_web.webhook_route_error is not None:
            webhook_fallback_reason = verify_web.webhook_route_error
            webhook = None
        verification_issues = {
            "turnstile": turnstile_key_configuration_issue(
                settings.join_verification_turnstile_site_key,
                settings.join_verification_turnstile_secret_key,
            ),
            "hcaptcha": hcaptcha_key_configuration_issue(
                settings.join_verification_hcaptcha_site_key,
                settings.join_verification_hcaptcha_secret_key,
            ),
        }
        for provider, issue in verification_issues.items():
            if issue:
                log.error(
                    "%s；已暂停使用 %s 签发真人质询，请在 /settings 更正后重试。",
                    issue,
                    provider,
                )
        if any(
            verification_service_ready(settings, provider)
            for provider in VERIFICATION_PROVIDERS
        ):
            await warn_if_bot_cannot_verify(bot, settings, session_factory)
        elif settings.moderation.enabled or settings.join_verification_enabled:
            log.warning(
                "真人验证密钥或公网验证地址不完整；入群验证不会签发，"
                "低置信度 ban 群规也将回退到原群规动作。"
            )
        log.info("Bot starting...")

        # Do not let permanent runners execute while startup recovery or the
        # web listener is only partially initialized.  If either step above
        # fails, ``background_tasks`` stays empty and the common cleanup path
        # closes every resource without a runner racing it.
        background_tasks = [
            asyncio.create_task(
                telegram_cleanup.monitor(),
                name="telegram-cleanup-monitor",
            ),
            asyncio.create_task(
                proactive.run_forever(),
                name="proactive-topic-runner",
            ),
            # Persisted deadlines are drained while verification is healthy. If
            # the shared CAPTCHA config is unavailable, the sweeper preserves
            # records instead of punishing users.
            asyncio.create_task(
                sweeper.run_forever(),
                name="verification-sweeper",
            ),
            asyncio.create_task(
                patrol.run_forever(),
                name="profile-patrol-runner",
            ),
            asyncio.create_task(
                scheduled_messages.run_forever(),
                name="scheduled-message-runner",
            ),
            asyncio.create_task(
                group_permissions.run_forever(),
                name="group-permission-runner",
            ),
            asyncio.create_task(
                shop_expiry.run_forever(),
                name="shop-expiry-runner",
            ),
            asyncio.create_task(
                run_resource_watchdog(),
                name="resource-health-watchdog",
            ),
        ]
        archive_maintenance = getattr(memory, "run_archive_maintenance", None)
        if callable(archive_maintenance):
            background_tasks.append(
                asyncio.create_task(
                    archive_maintenance(),
                    name="memory-archive-maintenance",
                )
            )
        # 私聊正文落库之后也必须有人定期清理，否则库只涨不减。保留天数每轮现取，
        # /settings 里改了下一轮就生效（与 memory 归档清理同一套路）。
        background_tasks.append(
            asyncio.create_task(
                run_private_chat_history_maintenance(
                    session_factory,
                    retention_days_getter=lambda: private_history_retention_days(
                        settings
                    ),
                ),
                name="private-chat-history-maintenance",
            )
        )
        # 模型窗口元数据的**周期刷新**：成功 TTL 6h / 失败负缓存 5min 的语义靠它兑现。
        # 缓存 fresh 时这一轮零网络；只做元数据，绝不进回复/审核热路径。拿到新窗口后
        # 通过 window_sync 把 MemoryService 的预算一起更新（不重启也生效）。
        # 任务是"常驻"的：单轮失败只记日志、循环不退出（否则会被当致命错误）。
        # 后台群摘要：常驻任务（单轮失败只记日志、绝不退出），随应用一起关停。
        if summary_scheduler is not None:
            background_tasks.append(
                asyncio.create_task(
                    summary_scheduler.run(),
                    name="group-summary-scheduler",
                )
            )
        refresh_limits = getattr(llm, "refresh_model_limits", None)
        if callable(refresh_limits):
            background_tasks.append(
                model_limits.PeriodicModelMetadataRefresh(
                    refresh_limits,
                    on_refreshed=window_sync.sync,
                ).start()
            )
        # 第 3 期：检索结果留档也要定期清理（默认 30 天）。保留天数每轮现取，
        # /settings 里改了下一轮就生效（与上面两个巡检同一套路）。
        background_tasks.append(
            asyncio.create_task(
                run_search_record_maintenance(
                    session_factory,
                    retention_days_getter=lambda: search_record_retention_days(
                        settings
                    ),
                ),
                name="search-record-maintenance",
            )
        )
        # 第 4 期：长期记忆（``user_facts``）。两条巡检都只在后台跑——提炼要调模型，
        # **绝不进回复路径**；维护负责 event 过期标记与 deleted/superseded 留存清理。
        # 每轮现取配置（改 /settings 下一轮生效），单次失败只记日志、不退出循环。
        background_tasks.append(
            asyncio.create_task(
                run_long_term_memory_extraction(
                    session_factory,
                    llm=llm,
                    settings=settings,
                ),
                name="long-term-memory-extract",
            )
        )
        background_tasks.append(
            asyncio.create_task(
                run_long_term_memory_maintenance(
                    session_factory,
                    retention_days_getter=lambda: memory_deleted_retention_days(
                        settings
                    ),
                ),
                name="long-term-memory-maintenance",
            )
        )

        await _run_with_background_supervision(
            run_update_delivery(
                bot=bot,
                dispatcher=dp,
                settings=settings,
                webhook=webhook,
                fallback_reason=webhook_fallback_reason,
                enable_webhook_route=verify_web.enable_webhook_route,
                mark_webhook_active=verify_web.mark_webhook_active,
                disable_webhook_route=verify_web.disable_webhook_route,
                mark_polling_active=verify_web.mark_polling_active,
                webhook_runtime_failure=verify_web.webhook_runtime_failure,
                start_update_processor=getattr(
                    verify_web,
                    "start_update_processor",
                    None,
                ),
                durable_update_ingest=getattr(
                    verify_web,
                    "accept_polling_update",
                    None,
                ),
            ),
            background_tasks=background_tasks,
        )
    finally:
        ordered_shutdown_watchdog = _arm_forced_exit_watchdog(
            _ORDERED_SHUTDOWN_HARD_LIMIT_SECONDS
        )
        # Close the scheduler before cancelling services or disposing the DB:
        # late clients must not publish during shutdown.
        if summary_scheduler is not None:
            await _await_cleanup_bounded(
                summary_scheduler.shutdown(),
                label="group summary scheduler",
            )
        await _cancel_tasks_bounded(
            background_tasks,
            label="background services",
        )
        await _await_cleanup_bounded(
            flush_update_delivery_tasks(),
            label="update delivery control tasks",
        )
        # Transport delivery has stopped. Quiesce recovery/dispatch first, but
        # keep durable finalizers alive until every explicit detached owner has
        # finished; otherwise a graceful deploy can strand a live lease for the
        # full recovery interval.
        await _await_cleanup_bounded(
            verify_web.quiesce_update_processor(),
            label="Telegram update processor quiesce",
        )
        await _await_cleanup_bounded(
            flush_privileged_tasks(),
            label="privileged security tasks",
        )
        await _await_cleanup_bounded(
            raid_guard.shutdown(),
            label="raid guard timer tasks",
        )
        await _await_cleanup_bounded(
            patrol.shutdown(),
            label="patrol manual tasks",
        )
        # Update delivery has stopped accepting Telegram updates at this point,
        # while the shared Bot HTTP session is deliberately still open for any
        # final replies produced by the bounded pending-batch flush.
        await _await_cleanup_bounded(
            group.flush_pending_inbound_batches(),
            label="pending inbound batches",
            timeout=_PENDING_FLUSH_TIMEOUT_SECONDS,
        )
        memory_shutdown = getattr(memory, "shutdown", None)
        if callable(memory_shutdown):
            await _await_cleanup_bounded(
                memory_shutdown(),
                label="memory maintenance tasks",
            )
        await _await_cleanup_bounded(
            flush_skill_execution_tasks(),
            label="skill execution tasks",
        )
        await _await_cleanup_bounded(
            flush_llm_request_tasks(),
            label="LLM request tasks",
        )
        await _await_cleanup_bounded(
            close_llm_clients(),
            label="LiteLLM HTTP clients",
        )
        await _await_cleanup_bounded(
            flush_vote_ban_tasks(),
            label="vote-ban tasks",
        )
        await _await_cleanup_bounded(
            flush_kick_cleanup_tasks(),
            label="kick cleanup tasks",
        )
        await _await_cleanup_bounded(
            verify_web.stop_update_processor(),
            label="Telegram update processor finalizers",
        )
        await _await_cleanup_bounded(
            verify_web.stop(),
            label="Mini App web server",
            timeout=_WEB_SERVER_CLEANUP_TIMEOUT_SECONDS,
        )
        # Pending-reply flushing above may still enqueue final cleanup jobs.
        # Persist every accepted request and stop the single scheduler before
        # either its Telegram client or database engine is closed.
        await _await_cleanup_bounded(
            telegram_cleanup.stop(),
            label="Telegram cleanup scheduler",
        )
        configure_telegram_cleanup_scheduler(None)
        await _await_cleanup_bounded(
            flush_telegram_background_tasks(),
            label="Telegram delayed cleanup tasks",
        )
        await _await_cleanup_bounded(
            bot.session.close(),
            label="Bot HTTP session",
        )
        await _await_cleanup_bounded(
            engine.dispose(),
            label="database engine",
        )
        ordered_shutdown_watchdog.set()


if __name__ == "__main__":
    run_bot()
