"""New-member join screening: name + bio checked against group moderation rules."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import (
    AuthorizedGroup,
    Group,
    JoinVerification,
    MemberPointAward,
    MemberPointSpend,
    UserWarning,
    Violation,
)
from bot.services.admin_status import invalidate_admin_status_cache
from bot.services.authz import (
    is_group_authorized,
    is_super_admin_user_id,
    set_group_bot_present,
)
from bot.services.callback_auth import is_group_admin_or_higher
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.join_screening import (
    build_join_profile_text,
    is_globally_banned,
    is_join_screening_exempt,
    mark_profile_screened,
    moderation_rules_fingerprint,
    profile_screen_signature,
    screen_member_profile_verbose,
)
from bot.services.checkin import (
    CHALLENGE_SKIP_COST,
    SPEND_REASON_CHALLENGE,
    spend_points,
)
from bot.services.join_verification import (
    PATROL_VERIFY_CALLBACK_DATA,
    RAID_VERIFY_CALLBACK_DATA,
    TERMINAL_LEASE_SECONDS,
    VERIFICATION_CALLBACK_APPEAL,
    VERIFICATION_CALLBACK_APPROVE,
    VERIFICATION_CALLBACK_PREFIX,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_SPEND,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    VERIFICATION_KIND_PATROL,
    VERIFICATION_KIND_RAID,
    VERIFICATION_STATUS_ENFORCING,
    VERIFICATION_STATUS_PENDING,
    VERIFICATION_STATUS_PREPARING,
    VERIFICATION_STATUS_RELEASING,
    VERIFICATION_STATUS_UNBANNING,
    BanEnforcementResult,
    ban_member,
    ban_member_result,
    build_profile_screening_ban_notice,
    build_group_prompt_keyboard,
    build_verification_callback_data,
    build_group_prompt_text,
    build_verification_progress_text,
    build_private_deep_link,
    claim_join_verification,
    close_private_challenge_message,
    chat_member_is_present,
    complete_leased_join_verification,
    commit_prepared_join_verification,
    delete_join_verification,
    delete_verification_prompt,
    enforce_ban_with_policy_reconciliation_result,
    extend_pending_verification_deadlines,
    get_join_verification,
    join_verification_ready,
    join_verification_policy,
    join_verification_lease_is_current,
    manual_unban_generation_is_active,
    spoiler_display_name,
    kick_member,
    lease_expired_join_verification,
    lease_join_verification_for_unban,
    mark_group_banned,
    mark_profile_screening_group_ban,
    parse_verification_callback_data,
    prepare_join_verification,
    reconcile_moderation_ban_after_lost_lease_result,
    reconcile_stale_verification_restriction,
    refresh_pending_join_verification,
    renew_join_verification_lease,
    renew_prepared_join_verification,
    restore_member_permissions,
    resume_group_verification_recovery,
    rollback_group_ban,
    restrict_new_member,
    shield_abort_prepared_join_verification,
    telegram_group_is_unreachable_error,
    upsert_join_verification,
    verification_deadline_passed,
    verification_release_blocked_by_ban,
    verification_restriction_required,
    verification_timeout_seconds_for_kind,
)
from bot.services.llm import LLMService
from bot.services.message_templates import (
    card_field,
    render_progress_notice,
)
from bot.services.ban_audit import record_ban_event
from bot.services.moderation_context import build_moderation_context
from bot.services.moderation import ModerationService
from bot.services.patrol import mark_group_member_left, track_group_member
from bot.services.point_shop import (
    challenge_spend_ref,
    refund_points,
    refund_ref,
)
from bot.services.raid_guard import (
    RAID_REMOVE_CALLBACK_DATA,
    RaidRemovalResult,
    get_raid_guard_service,
    remove_raid_challenged_users,
)
from bot.services.privileged_tasks import submit_privileged_task
from bot.services.recent_messages import (
    clear_member_join_marker,
    delete_messages_since_join,
    mark_member_join,
    member_join_marker,
    retract_removed_member_residue,
)
from bot.services.request_priority import privileged_request_scope
from bot.services.update_completion import request_current_update_retry
from bot.services.update_delivery import unmark_privileged_operator
from bot.services.welcome import send_group_welcome
from bot.utils.bot_identity import get_bot_identity
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive

router = Router()
log = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingMemberJoinSecurity:
    job_key: str
    version: int
    latest_update_id: int
    event: ChatMemberUpdated
    settings: Settings


_PENDING_MEMBER_JOIN_SECURITY: dict[tuple[int, int], _PendingMemberJoinSecurity] = {}
_MEMBER_JOIN_JOB_SEQUENCE = 0


async def _ack_security_callback(
    callback: CallbackQuery,
    text: str,
    *,
    show_alert: bool = False,
) -> None:
    async def _answer() -> None:
        # CallbackQuery.answer() returns a TelegramMethod (awaitable, not a
        # coroutine); create_task requires a genuine coroutine.
        await callback.answer(text, show_alert=show_alert)

    with privileged_request_scope():
        task = asyncio.create_task(
            _answer(),
            name="security-callback-ack",
        )
    done, _pending = await asyncio.wait({task}, timeout=2.0)
    if task in done:
        try:
            await task
        except Exception:
            log.debug("security callback acknowledgement failed", exc_info=True)
        return
    task.cancel()

    def consume(done: asyncio.Task[object]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(consume)


async def _publish_raid_removal_result(
    callback: CallbackQuery,
    *,
    group_id: int,
    prompt_message_id: int,
    result: RaidRemovalResult,
) -> None:
    removed_count = len(result.removed_user_ids)
    failed_count = len(result.failed_user_ids)
    if failed_count == 0:
        try:
            async with asyncio.timeout(5.0):
                await callback.bot.edit_message_reply_markup(
                    chat_id=group_id,
                    message_id=prompt_message_id,
                    reply_markup=None,
                )
        except Exception:
            log.debug(
                "raid bulk-remove keyboard cleanup failed | group=%s message=%s",
                group_id,
                prompt_message_id,
                exc_info=True,
            )
    if result.pending_count == 0:
        status = "已完成"
        current = "该批追溯用户已全部处理"
    elif failed_count:
        status = "待重试"
        current = f"已移除 {removed_count} 人，仍有 {failed_count} 人待重试"
    else:
        status = "已完成"
        current = f"已移除 {removed_count} 名被追溯用户"
    details = [card_field("已移除", f"<code>{removed_count}</code> 人")]
    if failed_count:
        details.append(card_field("待重试", f"<code>{failed_count}</code> 人"))
    text = render_progress_notice(
        f"爆破防护批量移除 · {status}",
        completed="已提交批量移除",
        current=current,
        next_step=("等待后台重试" if failed_count else "无需进一步操作"),
        details=details,
    )
    message = callback.message
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(text, parse_mode="HTML")
        else:
            await callback.bot.send_message(group_id, text, parse_mode="HTML")
    except Exception:
        log.exception(
            "raid bulk-remove result delivery failed | group=%s message=%s",
            group_id,
            prompt_message_id,
        )


async def _fetch_user_bio(event: ChatMemberUpdated, user_id: int) -> str:
    """Bio is only exposed via a full getChat on the user's private chat."""
    try:
        chat = await event.bot.get_chat(user_id)
        return str(getattr(chat, "bio", "") or "")
    except Exception as exc:
        log.info("join screening bio fetch failed | user=%s error=%s", user_id, exc)
        return ""


async def _join_member_still_present(
    event: ChatMemberUpdated,
    user_id: int,
    *,
    stage: str,
) -> bool:
    """Confirm queued join work still targets a current chat member.

    An inconclusive Telegram lookup is retryable and must fail the durable
    security job instead of silently admitting or mutating a stale user.
    """

    present = await chat_member_is_present(event.bot, int(event.chat.id), int(user_id))
    if present is None:
        raise RuntimeError(
            f"membership could not be confirmed before join security stage {stage}"
        )
    if not present:
        log.info(
            "join security stopped | reason=member_left stage=%s group=%s user=%s",
            stage,
            event.chat.id,
            user_id,
        )
    return present


async def _reconcile_stale_restriction(
    event: ChatMemberUpdated,
    session: AsyncSession,
    *,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> bool:
    if session_factory is not None:
        return await reconcile_stale_verification_restriction(
            event.bot,
            session_factory,
            int(event.chat.id),
            int(user_id),
        )

    # Direct helper/unit invocations do not always carry a factory. Keep the
    # same latest-intent semantics without sharing a transaction across the
    # Telegram call.
    await session.rollback()
    blocked = await verification_release_blocked_by_ban(
        session,
        group_id=int(event.chat.id),
        user_id=int(user_id),
    )
    current = await get_join_verification(session, int(event.chat.id), int(user_id))
    current_status = (
        str(current.status or VERIFICATION_STATUS_PENDING) if current is not None else ""
    )
    await session.commit()
    if blocked:
        return await ban_member(event.bot, int(event.chat.id), int(user_id))
    if current_status in {
        VERIFICATION_STATUS_PREPARING,
        VERIFICATION_STATUS_PENDING,
        VERIFICATION_STATUS_ENFORCING,
    }:
        return True
    return await restore_member_permissions(event.bot, int(event.chat.id), int(user_id))


def _build_llm(settings: Settings) -> LLMService:
    return LLMService(
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


async def _ban_and_notify(
    event: ChatMemberUpdated,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    reason: str,
    preserve_ban: Callable[[], Awaitable[bool]] | None = None,
    restriction_required: Callable[[], Awaitable[bool]] | None = None,
    publish_notice: bool = True,
) -> BanEnforcementResult:
    # Snapshot before the ban: the resulting leave update clears the live
    # join marker concurrently, and the post-ban residue sweep still needs
    # the membership window.
    residue_marker = member_join_marker(int(event.chat.id), int(user_id))
    if preserve_ban is None:
        enforcement = await ban_member_result(
            event.bot,
            int(event.chat.id),
            int(user_id),
        )
    else:
        enforcement = await enforce_ban_with_policy_reconciliation_result(
            event.bot,
            int(event.chat.id),
            int(user_id),
            preserve_ban,
            restriction_required,
        )
    if enforcement.final_banned is not True:
        if enforcement.final_banned is False:
            log.info(
                "join screening ban cancelled by latest release policy | "
                "group=%s user=%s",
                event.chat.id,
                user_id,
            )
        else:
            log.error(
                "join screening ban not confirmed | group=%s user=%s "
                "retryable=%s unreachable=%s operator_action=%s",
                event.chat.id,
                user_id,
                enforcement.retryable,
                enforcement.group_unreachable,
                enforcement.operator_action_required,
            )
        return enforcement
    # revoke_messages only hides history from the banned account itself; the
    # spam they raced in before the ban — plus the join announcement and the
    # "X was removed" notice — stay visible to everyone else.
    await retract_removed_member_residue(
        event.bot,
        int(event.chat.id),
        int(user_id),
        marker=residue_marker,
    )
    if publish_notice:
        await _publish_profile_screening_ban_notice(
            event,
            settings,
            user_id=user_id,
            display_name=display_name,
            reason=reason,
        )
    return enforcement


async def _publish_profile_screening_ban_notice(
    event: ChatMemberUpdated,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    reason: str,
    prompt_message_id: int = 0,
    require_existing_prompt: bool = False,
) -> None:
    notice = build_profile_screening_ban_notice(
        user_id=user_id,
        display_name=display_name,
        reason=reason,
    )
    auto_delete_seconds = configured_auto_delete_seconds(settings, "moderation")
    prompt_message_id = int(prompt_message_id or 0)
    if prompt_message_id > 0:
        try:
            edited = await event.bot.edit_message_text(
                chat_id=int(event.chat.id),
                message_id=prompt_message_id,
                text=notice,
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            log.warning(
                "join screening prompt update failed; no standalone notice sent | "
                "group=%s user=%s message=%s",
                event.chat.id,
                user_id,
                prompt_message_id,
                exc_info=True,
            )
            await delete_verification_prompt(
                event.bot,
                int(event.chat.id),
                prompt_message_id,
            )
            return
        else:
            try:
                await schedule_message_auto_delete_durable(
                    edited if not isinstance(edited, bool) else None,
                    auto_delete_seconds,
                )
            except Exception:
                # The outcome is already visible. A cleanup scheduling failure
                # must never delete it or create a duplicate fallback notice.
                log.exception(
                    "join screening prompt auto-delete scheduling failed | "
                    "group=%s user=%s message=%s",
                    event.chat.id,
                    user_id,
                    prompt_message_id,
                )
            return
    if require_existing_prompt:
        log.warning(
            "join screening outcome has no verification prompt to update | "
            "group=%s user=%s",
            event.chat.id,
            user_id,
        )
        return
    try:
        sent = await event.bot.send_message(
            event.chat.id,
            notice,
            parse_mode="HTML",
        )
    except Exception:
        log.exception("join screening notice failed | group=%s", event.chat.id)
        return
    # This is a moderation outcome ("审核通知"): honor the group's
    # auto-delete retention.
    try:
        await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
    except Exception:
        log.exception(
            "join screening notice auto-delete scheduling failed | group=%s user=%s",
            event.chat.id,
            user_id,
        )


def _invalidate_admin_cache(event: ChatMemberUpdated) -> None:
    # Promotion must invalidate a cached non-admin denial immediately.
    # Positive admin grants are never cached by admin_status.
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is not None:
        invalidate_admin_status_cache(event.chat.id, user.id)
        unmark_privileged_operator(user.id, group_id=event.chat.id)


async def _start_join_verification(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    display_name: str,
    provider: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    require_current_membership: bool = False,
) -> None:
    """Durably prepare, mute, prompt, then activate a join challenge.

    The short-lived ``preparing`` lease is committed before the first Telegram
    side effect.  A crash after muting therefore leaves recoverable state for
    the sweeper instead of a permanently restricted member with no challenge.
    """
    group_id = event.chat.id
    # This handler awaited network calls (bio fetch, screening LLM) since its
    # last record check; a raid retro sweep may have issued a challenge for
    # this member meanwhile. Upserting kind="join" would clobber it and break
    # its shared challenge button, so the existing record wins. Commit first:
    # the raid record was written by another session and a stale SQLite
    # snapshot from before those awaits would hide it.
    await session.commit()
    existing = await get_join_verification(session, group_id, user_id)
    if existing is not None:
        existing_status = str(existing.status or VERIFICATION_STATUS_PENDING)
        if (
            existing.kind != VERIFICATION_KIND_JOIN
            or existing_status != VERIFICATION_STATUS_PENDING
        ):
            log.info(
                "join verification skipped | reason=existing_%s_%s group=%s user=%s",
                existing.kind,
                existing_status,
                group_id,
                user_id,
            )
            return

        # A duplicate/rejoin already has a durable pending work item. Keep that
        # generation authoritative until the replacement prompt is successfully
        # sent and CAS-refreshed. Setup failure therefore preserves the old
        # challenge instead of turning it into an orphaned mute.
        old_prompt_message_id = int(existing.prompt_message_id or 0)
        old_deadline = existing.deadline_at
        existing_id = int(existing.id)
        deadline = now_shanghai_naive() + timedelta(
            seconds=settings.join_verification_timeout_seconds
        )
        prompt_message_id = 0
        refreshed = False

        async def _commit_refresh() -> bool:
            won = await refresh_pending_join_verification(
                session,
                verification_id=existing_id,
                deadline_at=old_deadline,
                kind=VERIFICATION_KIND_JOIN,
                new_deadline_at=deadline,
                prompt_message_id=prompt_message_id,
                provider=provider,
                display_name=display_name,
            )
            if not won:
                await session.rollback()
                return False
            await session.commit()
            return True

        async def _cleanup_failed_refresh() -> None:
            await session.rollback()
            recovery = await prepare_join_verification(
                session,
                group_id=group_id,
                user_id=user_id,
                deadline_at=deadline,
                display_name=display_name,
                prompt_message_id=prompt_message_id,
                provider=provider,
            )
            if recovery is not None:
                # The old generation disappeared while this refresh was in
                # flight. Own the now-empty unique key before restoring so a
                # concurrent new challenge cannot be accidentally unmuted.
                await session.commit()
                await shield_abort_prepared_join_verification(
                    event.bot,
                    session,
                    prepared=recovery,
                    prompt_message_id=prompt_message_id,
                )
            else:
                await session.rollback()
            if prompt_message_id and recovery is None:
                await delete_verification_prompt(
                    event.bot,
                    group_id,
                    prompt_message_id,
                )
            if recovery is None:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )

        # ``existing`` has now been reduced to immutable ids/deadlines. End
        # the duplicate-record read transaction before restricting the member
        # and sending a replacement prompt; the CAS refresh below opens a new
        # short transaction only after those Telegram calls finish.
        await session.commit()
        try:
            if require_current_membership and not await _join_member_still_present(
                event,
                user_id,
                stage="duplicate_challenge_restrict",
            ):
                return
            if not await restrict_new_member(event.bot, group_id, user_id):
                return
            # Anything posted between the join and this mute skipped
            # verification entirely; retract it before the fresh prompt.
            await delete_messages_since_join(event.bot, group_id, user_id)
            current = await get_join_verification(session, group_id, user_id)
            current_owned = bool(
                current is not None
                and int(current.id) == existing_id
                and str(current.status or VERIFICATION_STATUS_PENDING)
                == VERIFICATION_STATUS_PENDING
                and current.deadline_at == old_deadline
                and current.kind == VERIFICATION_KIND_JOIN
            )
            await session.commit()
            if not current_owned:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
                return
            sent = await event.bot.send_message(
                group_id,
                build_group_prompt_text(
                    user_id=user_id,
                    display_name=display_name,
                    timeout_seconds=settings.join_verification_timeout_seconds,
                ),
                parse_mode="HTML",
                reply_markup=build_group_prompt_keyboard(user_id),
            )
            prompt_message_id = int(getattr(sent, "message_id", 0) or 0)
            refresh_task = asyncio.create_task(
                _commit_refresh(),
                name=f"join-verification-refresh:{group_id}:{user_id}",
            )
            try:
                refreshed = await asyncio.shield(refresh_task)
            except asyncio.CancelledError:
                try:
                    refreshed = await refresh_task
                except Exception:
                    refreshed = False
                    log.exception(
                        "join verification refresh failed while cancellation was pending | "
                        "group=%s user=%s",
                        group_id,
                        user_id,
                    )
                raise
            if not refreshed:
                await _cleanup_failed_refresh()
                return
        except asyncio.CancelledError:
            if not refreshed:
                await asyncio.shield(_cleanup_failed_refresh())
            raise
        except Exception:
            log.exception(
                "join verification refresh failed | group=%s user=%s",
                group_id,
                user_id,
            )
            if not refreshed:
                await asyncio.shield(_cleanup_failed_refresh())
            return
        if old_prompt_message_id and old_prompt_message_id != prompt_message_id:
            await delete_verification_prompt(
                event.bot,
                group_id,
                old_prompt_message_id,
            )
        log.info("join verification refreshed | group=%s user=%s", group_id, user_id)
        return

    old_prompt_message_id = 0
    deadline = now_shanghai_naive() + timedelta(
        seconds=settings.join_verification_timeout_seconds
    )
    prepared = await prepare_join_verification(
        session,
        group_id=group_id,
        user_id=user_id,
        deadline_at=deadline,
        display_name=display_name,
        prompt_message_id=old_prompt_message_id,
        provider=provider,
    )
    if prepared is None:
        await session.rollback()
        return
    # The recovery lease must be durable before the first Telegram side effect.
    await session.commit()

    text = build_group_prompt_text(
        user_id=user_id,
        display_name=display_name,
        timeout_seconds=settings.join_verification_timeout_seconds,
    )
    prompt_message_id = 0
    activated = False
    try:
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="challenge_prepare",
        ):
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                restore_permissions=False,
            )
            return
        renewed = await renew_prepared_join_verification(session, prepared=prepared)
        if renewed is None:
            await session.rollback()
            return
        await session.commit()
        prepared = renewed
        if not await restrict_new_member(event.bot, group_id, user_id):
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
            )
            return
        # Anything posted between the join and this mute skipped verification
        # entirely; retract it before the challenge prompt appears.
        await delete_messages_since_join(event.bot, group_id, user_id)

        renewed = await renew_prepared_join_verification(session, prepared=prepared)
        if renewed is None:
            await session.rollback()
            await _reconcile_stale_restriction(
                event,
                session,
                user_id=user_id,
                session_factory=session_factory,
            )
            return
        await session.commit()
        prepared = renewed
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="challenge_prompt",
        ):
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
            return

        sent = await event.bot.send_message(
            group_id,
            text,
            parse_mode="HTML",
            reply_markup=build_group_prompt_keyboard(user_id),
        )
        prompt_message_id = int(getattr(sent, "message_id", 0) or 0)

        activation_task = asyncio.create_task(
            commit_prepared_join_verification(
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
                deadline_at=deadline,
            ),
            name=f"join-verification-activate:{group_id}:{user_id}",
        )
        try:
            activated = await asyncio.shield(activation_task)
        except asyncio.CancelledError:
            try:
                activated = await activation_task
            except Exception:
                activated = False
                log.exception(
                    "join verification activation failed while cancellation was pending | "
                    "group=%s user=%s",
                    group_id,
                    user_id,
                )
            raise
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
            return
    except asyncio.CancelledError:
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
        raise
    except Exception:
        log.exception(
            "join verification setup failed | group=%s user=%s",
            group_id,
            user_id,
        )
        if not activated:
            compensated = await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=prepared,
                prompt_message_id=prompt_message_id,
            )
            if not compensated:
                await _reconcile_stale_restriction(
                    event,
                    session,
                    user_id=user_id,
                    session_factory=session_factory,
                )
        return
    if old_prompt_message_id and old_prompt_message_id != prompt_message_id:
        await delete_verification_prompt(
            event.bot,
            group_id,
            old_prompt_message_id,
        )
    log.info("join verification issued | group=%s user=%s", group_id, user_id)


async def _enforce_pending_moderation_challenge(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    record: JoinVerification,
    *,
    display_name: str,
) -> None:
    """Keep an unresolved message/patrol/raid challenge intact across leave/rejoin.

    Only moderation challenges explicitly issued from a ban rule may ban on
    expiry; patrol, raid, and legacy/non-punitive moderation challenges release
    without banning, matching the sweeper's consequences for each kind.
    """
    now = now_shanghai_naive()
    expired = verification_deadline_passed(record.deadline_at, now=now)
    snapshot = _verification_snapshot(record)
    if is_super_admin_user_id(record.user_id, settings):
        lease_until = await _lease_terminal_verification(
            session,
            record,
            now=now,
            expired=expired,
            target_status=VERIFICATION_STATUS_RELEASING,
        )
        if lease_until is None:
            return
        restored = await restore_member_permissions(
            event.bot,
            record.group_id,
            record.user_id,
        )
        if not restored:
            return
        if await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_RELEASING,
        ):
            await close_private_challenge_message(
                event.bot,
                int(record.user_id),
                int(snapshot.get("private_message_id") or 0),
            )
        return

    is_patrol = record.kind in (VERIFICATION_KIND_PATROL, VERIFICATION_KIND_RAID)
    safe_release_moderation = bool(
        record.kind == VERIFICATION_KIND_MODERATION
        and not bool(getattr(record, "ban_on_timeout", False))
    )
    if safe_release_moderation:
        lease_until = await _lease_terminal_verification(
            session,
            now=now,
            record=record,
            expired=expired,
            target_status=VERIFICATION_STATUS_UNBANNING,
        )
        if lease_until is None:
            return

        async def preserve_existing_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
            await session.commit()
            return blocked

        async def no_challenge_restriction() -> bool:
            return False

        reconciliation = await reconcile_moderation_ban_after_lost_lease_result(
            event.bot,
            int(record.group_id),
            int(record.user_id),
            preserve_existing_ban,
            restriction_required=no_challenge_restriction,
        )
        if not reconciliation.ok:
            await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_UNBANNING,
            )
            return
        if await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_UNBANNING,
        ):
            await close_private_challenge_message(
                event.bot,
                int(record.user_id),
                int(snapshot.get("private_message_id") or 0),
            )
        return
    if expired:
        lease_until = await _lease_terminal_verification(
            session,
            now=now,
            record=record,
            expired=True,
            target_status=VERIFICATION_STATUS_ENFORCING,
        )
        if lease_until is None:
            return
        if is_patrol:
            async def preserve_ban() -> bool:
                # D3-52：四个同类闭包里就这一个漏了 rollback，而
                # join_verification.preserve_ban 的 docstring 明确写了这个要求
                # （"A cancellation/DB error may leave an aborted transaction, so
                # normalize it before every authoritative policy read"）。这个闭包被
                # _ensure_kick_unbanned_result 在一次 kick 里最多调用 4 次，若上一次
                # commit 因取消/DB 错误留下 aborted 事务，第 2 次策略读直接抛
                # PendingRollbackError，被 :3106-3113 吞成 ok=False → 一次本可完成的
                # timeout kick 被判失败并回队。rollback 只清未提交改动，此处本就没有
                # 待写内容。
                await session.rollback()
                blocked = await verification_release_blocked_by_ban(
                    session,
                    group_id=int(record.group_id),
                    user_id=int(record.user_id),
                )
                await session.commit()
                return blocked

            # Snapshot before the kick: its leave update clears the live join
            # marker concurrently, and the post-kick residue sweep still needs
            # the rejoin window (which includes the join service message).
            residue_marker = member_join_marker(
                int(record.group_id), int(record.user_id)
            )
            enforced = await kick_member(
                event.bot,
                record.group_id,
                record.user_id,
                preserve_ban=preserve_ban,
            )
            if enforced:
                await retract_removed_member_residue(
                    event.bot,
                    int(record.group_id),
                    int(record.user_id),
                    marker=residue_marker,
                )
                if await _complete_terminal_verification(
                    session,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                ):
                    await close_private_challenge_message(
                        event.bot,
                        int(record.user_id),
                        int(snapshot.get("private_message_id") or 0),
                    )
            else:
                requeued = await _requeue_verification(
                    session,
                    settings,
                    snapshot,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
                if requeued:
                    await restrict_new_member(
                        event.bot,
                        record.group_id,
                        record.user_id,
                    )
            return
        await mark_group_banned(
            session,
            record.group_id,
            record.user_id,
        )
        await session.commit()

        async def preserve_timeout_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
            await session.commit()
            return blocked

        async def timeout_restriction_required() -> bool:
            await session.rollback()
            required = await verification_restriction_required(
                session,
                group_id=int(record.group_id),
                user_id=int(record.user_id),
            )
            await session.commit()
            return required

        enforcement = await _ban_and_notify(
            event,
            settings,
            user_id=record.user_id,
            display_name=display_name,
            reason="消息审查真人验证超时",
            preserve_ban=preserve_timeout_ban,
            restriction_required=timeout_restriction_required,
        )
        if (
            enforcement.final_banned is False
            and enforcement.final_restricted is not True
        ):
            if await _complete_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            ):
                await close_private_challenge_message(
                    event.bot,
                    int(record.user_id),
                    int(snapshot.get("private_message_id") or 0),
                )
            return
        if enforcement.final_banned is False:
            log.info(
                "moderation timeout release kept a newer verification "
                "restriction | group=%s user=%s",
                record.group_id,
                record.user_id,
            )
            return
        if enforcement.final_banned is not True:
            deferred = await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
            log.warning(
                "moderation timeout ban unconfirmed; durable ban/enforcement retained | "
                "group=%s user=%s deferred=%s",
                record.group_id,
                record.user_id,
                deferred,
            )
            return
        if await _complete_moderation_enforcement_or_reconcile(
            event.bot,
            session,
            group_id=int(record.group_id),
            user_id=int(record.user_id),
            verification_id=int(record.id),
            lease_until=lease_until,
        ):
            await close_private_challenge_message(
                event.bot,
                int(record.user_id),
                int(snapshot.get("private_message_id") or 0),
            )
        return

    # End the read transaction before the Telegram call. A concurrent web
    # verification can then delete the row and win without being hidden by a
    # stale SQLite snapshot.
    await session.commit()
    restricted = await restrict_new_member(event.bot, record.group_id, record.user_id)
    if not restricted:
        return
    await delete_messages_since_join(
        event.bot,
        int(record.group_id),
        int(record.user_id),
    )
    current = await get_join_verification(session, record.group_id, record.user_id)
    if current is None:
        recovery = await prepare_join_verification(
            session,
            group_id=record.group_id,
            user_id=record.user_id,
            deadline_at=record.deadline_at,
            kind=record.kind,
            provider=record.provider,
            reason=record.reason,
            display_name=display_name or record.display_name,
            prompt_message_id=0,
            ban_on_timeout=bool(getattr(record, "ban_on_timeout", False)),
        )
        if recovery is not None:
            await session.commit()
            await shield_abort_prepared_join_verification(
                event.bot,
                session,
                prepared=recovery,
            )
        else:
            await session.rollback()
        return
    if (
        int(current.id) != int(record.id)
        or current.kind != record.kind
        or str(current.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
    ):
        # A newer challenge now owns this member's permissions.
        await session.commit()
        await _reconcile_stale_restriction(
            event,
            session,
            user_id=int(record.user_id),
            session_factory=None,
        )
        return
    log.info(
        "%s challenge re-enforced after rejoin | group=%s user=%s",
        record.kind,
        record.group_id,
        record.user_id,
    )


def _verification_snapshot(record: JoinVerification) -> dict[str, object]:
    return {
        "group_id": int(record.group_id),
        "user_id": int(record.user_id),
        "kind": str(record.kind),
        "provider": str(record.provider),
        "reason": str(record.reason or ""),
        "ban_on_timeout": bool(getattr(record, "ban_on_timeout", False)),
        "display_name": str(record.display_name or ""),
        "prompt_message_id": int(record.prompt_message_id or 0),
        "private_message_id": int(getattr(record, "private_message_id", 0) or 0),
    }


def _verification_retry_deadline(
    settings: Settings,
    kind: str,
):
    return now_shanghai_naive() + timedelta(
        seconds=verification_timeout_seconds_for_kind(settings, kind)
    )


async def _requeue_verification(
    session: AsyncSession,
    settings: Settings,
    snapshot: dict[str, object],
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    if status != VERIFICATION_STATUS_ENFORCING:
        raise ValueError("only punitive enforcement leases may be deferred here")
    del settings, snapshot
    return await _defer_terminal_verification(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        status=status,
    )


async def _defer_terminal_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    retry_lease = now_shanghai_naive() + timedelta(seconds=TERMINAL_LEASE_SECONDS)
    renewed = await renew_join_verification_lease(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        new_lease_until=retry_lease,
        status=status,
    )
    if not renewed:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _lease_terminal_verification(
    session: AsyncSession,
    record: JoinVerification,
    *,
    now: datetime,
    expired: bool,
    target_status: str,
) -> datetime | None:
    """Own one exact verification generation before a Telegram side effect."""
    lease_until = now + timedelta(seconds=TERMINAL_LEASE_SECONDS)
    current_status = str(record.status or VERIFICATION_STATUS_PENDING)
    if current_status == VERIFICATION_STATUS_PENDING:
        won = await claim_join_verification(
            session,
            verification_id=int(record.id),
            deadline_at=record.deadline_at,
            kind=record.kind,
            now=now,
            expired=expired,
            lease_until=lease_until,
            target_status=target_status,
        )
    elif (
        current_status
        in {VERIFICATION_STATUS_ENFORCING, VERIFICATION_STATUS_RELEASING}
        and record.lease_until is not None
        and record.lease_until <= now
    ):
        won = await lease_expired_join_verification(
            session,
            record=record,
            now=now,
            lease_until=lease_until,
            target_status=target_status,
        )
    else:
        won = False
    if not won:
        await session.rollback()
        return None
    await session.commit()
    return lease_until


async def _complete_terminal_verification(
    session: AsyncSession,
    *,
    verification_id: int,
    lease_until: datetime,
    status: str,
) -> bool:
    completed = await complete_leased_join_verification(
        session,
        verification_id=verification_id,
        lease_until=lease_until,
        status=status,
    )
    if not completed:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _ensure_moderation_recovery_owner(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
) -> bool:
    """Ensure failed reconciliation remains owned by a durable terminal row."""

    try:
        await session.rollback()
        current = await get_join_verification(session, group_id, user_id)
        if current is not None:
            status = str(current.status or VERIFICATION_STATUS_PENDING)
            await session.commit()
            return status in {
                VERIFICATION_STATUS_ENFORCING,
                VERIFICATION_STATUS_RELEASING,
                VERIFICATION_STATUS_UNBANNING,
            }

        recovery = await lease_join_verification_for_unban(
            session,
            group_id,
            user_id,
            manual_unban=False,
        )
        if recovery is None:
            await session.rollback()
            return False
        await session.commit()
        log.warning(
            "created durable moderation reconciliation recovery | "
            "group=%s user=%s verification=%s",
            group_id,
            user_id,
            recovery.verification_id,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        await session.rollback()
        log.exception(
            "failed to establish moderation reconciliation recovery | "
            "group=%s user=%s",
            group_id,
            user_id,
        )
        return False


async def _complete_moderation_enforcement_or_reconcile(
    bot: object,
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    verification_id: int,
    lease_until: datetime,
) -> bool:
    """Complete an exact moderation generation or undo its stale remote ban."""

    async def preserve_ban() -> bool:
        # Completion loss normally rolls back itself. A cancellation/DB error
        # may leave an aborted transaction, so normalize it before every
        # authoritative policy read used by the Telegram cleanup retry loop.
        await session.rollback()
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return blocked

    async def restriction_required() -> bool:
        await session.rollback()
        required = await verification_restriction_required(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return bool(required)

    async def reconcile():
        return await reconcile_moderation_ban_after_lost_lease_result(
            bot,
            group_id,
            user_id,
            preserve_ban,
            restriction_required=restriction_required,
        )

    try:
        completed = await _complete_terminal_verification(
            session,
            verification_id=verification_id,
            lease_until=lease_until,
            status=VERIFICATION_STATUS_ENFORCING,
        )
    except asyncio.CancelledError:
        reconciliation = await reconcile()
        if not reconciliation.ok:
            await _ensure_moderation_recovery_owner(
                session,
                group_id=group_id,
                user_id=user_id,
            )
        raise
    except Exception:
        reconciliation = await reconcile()
        if not reconciliation.ok:
            await _ensure_moderation_recovery_owner(
                session,
                group_id=group_id,
                user_id=user_id,
            )
        raise
    if not completed:
        reconciliation = await reconcile()
        if not reconciliation.ok and not await _ensure_moderation_recovery_owner(
            session,
            group_id=group_id,
            user_id=user_id,
        ):
            raise RuntimeError(
                "moderation enforcement reconciliation has no durable owner"
            )
    return completed


async def _verification_callback_record(
    callback: CallbackQuery,
    session: AsyncSession,
    target_user_id: int,
) -> JoinVerification | None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await _ack_security_callback(callback, "验证消息已失效", show_alert=True)
        return None

    record = await get_join_verification(session, int(chat.id), target_user_id)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if (
        record is None
        or str(record.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
        or int(record.prompt_message_id or 0) != message_id
        or verification_deadline_passed(record.deadline_at)
    ):
        # Clean up the stale keyboard immediately. This covers old prompts
        # left by a duplicate join, manual leave, or a transient terminal-edit
        # failure and prevents repeated "expired" clicks.
        await session.commit()
        await delete_verification_prompt(callback.bot, int(chat.id), message_id)
        await _ack_security_callback(
            callback,
            "验证已失效、过期或已处理",
            show_alert=True,
        )
        return None
    # Callers only need the immutable-in-practice challenge snapshot. Release
    # the SELECT transaction before bot.me(), callback.answer(), permission
    # restoration, or any other Telegram operation.
    await session.commit()
    return record


async def _edit_verification_prompt(
    callback: CallbackQuery,
    settings: Settings,
    *,
    text: str,
) -> None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if message is None or chat is None:
        return
    try:
        edited = await callback.bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )
        # The prompt is now a moderation outcome notice ("审核通知"): honor the
        # group's auto-delete retention like the other verification outcomes.
        await schedule_message_auto_delete_durable(
            edited if not isinstance(edited, bool) else None,
            configured_auto_delete_seconds(settings, "moderation"),
        )
    except Exception:
        log.debug(
            "verification admin prompt edit failed | group=%s message=%s",
            chat.id,
            message.message_id,
            exc_info=True,
        )
        await delete_verification_prompt(
            callback.bot,
            int(chat.id),
            int(message.message_id),
        )


async def _callback_bot_username(callback: CallbackQuery) -> str:
    username = get_bot_identity().username
    if username:
        return username
    try:
        me = await callback.bot.me()
    except Exception:
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


async def _handle_verification_start_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    target_user_id: int,
) -> None:
    operator = callback.from_user
    if operator is None or int(operator.id) != target_user_id:
        await callback.answer("仅受验证用户本人可点击", show_alert=True)
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    username = await _callback_bot_username(callback)
    if not username:
        await callback.answer("验证入口暂时不可用，请稍后重试", show_alert=True)
        return
    await callback.answer(
        url=build_private_deep_link(username, int(record.group_id)),
    )


async def _moderation_appeal_context(
    session: AsyncSession, group_id: int, user_id: int
) -> tuple[str, bool]:
    """(最近一次被判违规的原文, 今天是否还有其他命中)。

    命中文本存在 violations 表里，是复核唯一可靠的输入：触发时那条群消息
    已经被流水线删掉了。同一天内多次命中的账号不自动放行，避免"发广告→申诉→
    模型偶尔看走眼→原地复活"被反复利用。
    """

    rows = (
        (
            await session.execute(
                select(Violation)
                .where(
                    Violation.group_id == int(group_id),
                    Violation.user_id == int(user_id),
                )
                .order_by(Violation.id.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return "", False
    original = str(rows[0].message_text or "").strip()
    # violations.created_at 是 UTC，本地时间要减 8 小时再比
    utc_cutoff = now_shanghai_naive() - timedelta(hours=8 + 24)
    repeats = any(
        row.created_at is not None and row.created_at >= utc_cutoff
        for row in rows[1:]
    )
    return original, repeats


async def _latest_violation_id(
    session: AsyncSession, group_id: int, user_id: int
) -> int | None:
    """该成员在本群最近一次审核命中的记录号（用作审计事件的引用）。"""

    result = await session.execute(
        select(Violation.id)
        .where(
            Violation.group_id == int(group_id),
            Violation.user_id == int(user_id),
        )
        .order_by(Violation.id.desc())
        .limit(1)
    )
    row_id = result.scalar_one_or_none()
    return int(row_id) if row_id is not None else None


async def _record_moderation_cleared(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    source: str,
    reason: str = "",
    actor_user_id: int = 0,
    actor_display: str = "",
) -> None:
    """记一条"审核命中最终被判为误伤/放行"的审计事实。

    误伤率报表靠这张账本，而不是靠日志——日志会滚，报表要长期能算。
    写失败只记日志，绝不影响放行本身。
    """

    try:
        reference_id = await _latest_violation_id(session, group_id, user_id)
        await record_ban_event(
            session,
            group_id=int(group_id),
            target_user_id=int(user_id),
            action="clear",
            source=str(source or "unknown")[:64],
            outcome="cleared",
            reason=str(reason or "")[:200],
            actor_user_id=int(actor_user_id or 0),
            actor_display=str(actor_display or "")[:255],
            reference_type="violation",
            reference_id=int(reference_id or 0),
        )
    except Exception:
        log.warning(
            "moderation clear audit failed | group=%s user=%s source=%s",
            group_id,
            user_id,
            source,
            exc_info=True,
        )


async def _review_moderation_appeal(
    session: AsyncSession, settings: Settings, group_id: int, text: str
) -> tuple[object, str]:
    """用审核模型对被申诉的原文再判一次，返回 (verdict, 给人看的结论)。"""

    if not text.strip():
        return None, "找不到原始消息文本，无法自动复核"
    from bot.services.moderation import ModerationService

    # 复核的是历史消息：按这条消息在归档里的位置取它之前的对话，
    # 免得模型又只看这一句、重复第一次的误判。
    context_lines, _block = await build_moderation_context(
        session, group_id=int(group_id), anchor_text=text, exclude_text=text
    )
    try:
        moderation = ModerationService(settings.moderation, _build_llm(settings))
        verdict = await moderation.evaluate(
            session, int(group_id), text, context="\n".join(context_lines)
        )
    except Exception:
        log.warning("moderation appeal recheck failed | group=%s", group_id, exc_info=True)
        return None, "模型复核暂时不可用，请管理员人工判断"
    if verdict.violated:
        confidence = f"，置信 {verdict.confidence:.2f}" if verdict.confidence else ""
        return verdict, f"模型二次复核仍判「违规」{confidence}｜{verdict.reason}"
    if verdict.conclusive:
        return verdict, f"模型二次复核判「未违规」｜{verdict.reason}"
    # 空回复/不可解析时 conclusive=False：绝不能当成"没问题"直接放人
    return verdict, "模型二次复核没有给出明确结论，请管理员人工判断"


async def _next_challenge_spend_ref(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    verification_id: int,
) -> str:
    """挑一个这次点击真正能扣分的幂等键（F-010）。

    ``challenge:<id>`` 的唯一索引保证连点只扣一次；但它也让**已退款**的扣分
    永远无法重来：同一个 ref 再花一次只会返回 False，界面还会误报"积分不足"。
    所以取第一个"还没扣过、或扣过但已退款"的 attempt 后缀。
    """

    for attempt in range(0, 6):
        ref = challenge_spend_ref(verification_id, attempt)
        spent = await session.scalar(
            select(MemberPointSpend.id).where(
                MemberPointSpend.group_id == int(group_id),
                MemberPointSpend.user_id == int(user_id),
                MemberPointSpend.ref == ref,
            )
        )
        if spent is None:
            return ref
        refunded = await session.scalar(
            select(MemberPointAward.id).where(
                MemberPointAward.group_id == int(group_id),
                MemberPointAward.user_id == int(user_id),
                MemberPointAward.ref == refund_ref(ref),
            )
        )
        if refunded is None:
            # 已经扣过且没退：沿用同一个 ref，这次是重复点击，不该再扣一次
            return ref
    return challenge_spend_ref(verification_id, 6)


async def _challenge_release_owned(
    session: AsyncSession,
    *,
    verification_id: int,
) -> bool:
    """成员是否已被放行，或有后台工单正在/将会放行他（F-010）。

    读的是裸状态列而不是 ORM 对象：放行链路自己的每一步都已 commit，identity map
    里的旧对象可能还带着过期状态。读之前先结束上一个（只读）事务，否则 SQLite 的
    快照会让这里读到旧状态。调用点只在"花积分免除质询"（kind=moderation、
    system_points>0）之后使用，那条路径没有未提交的写入会被这次 rollback 丢掉。
    """

    try:
        await session.rollback()
    except Exception:  # pragma: no cover - 会话已坏时读状态也没意义
        log.debug("challenge release status rollback failed", exc_info=True)
    status = await session.scalar(
        select(JoinVerification.status).where(
            JoinVerification.id == int(verification_id)
        )
    )
    if status is None:
        # 记录已终态删除：release 成功（封禁分支不经过这里）
        return True
    return str(status) == VERIFICATION_STATUS_RELEASING


async def _handle_verification_spend_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    target_user_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """「消耗 N 积分免除质询」：先扣分，再走放行链路。

    扣分带幂等键 ``challenge:<质询ID>``（唯一索引），连点两次按钮只会扣一次。
    余额在点按钮的这一刻再查一遍：卡片是几分钟前发的，这期间分可能已经花掉。

    扣分之后、放行之前还有若干"只 ack 不动作"的退出分支（lease 被并发管理员
    抢走、目标已被封禁、群被取消授权……）。那些分支过去会让成员白扣分：这里在
    调用之后核对记录状态，只有确实没人放行他时才退款（``shop-refund:<ref>``
    唯一索引保证只退一次），并把这次尝试的 ref 换成新的 attempt，避免退款后
    再点一次按钮被唯一索引卡死（F-010）。
    """

    operator = callback.from_user
    if operator is None or int(operator.id) != target_user_id:
        await _ack_security_callback(
            callback, "仅被质询的成员本人可以点击", show_alert=True
        )
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    if str(record.kind or "") != VERIFICATION_KIND_MODERATION:
        await _ack_security_callback(callback, "该验证不支持积分免除", show_alert=True)
        return

    group_id = int(record.group_id)
    verification_id = int(record.id)
    cost = CHALLENGE_SKIP_COST
    spend_ref = await _next_challenge_spend_ref(
        session,
        group_id=group_id,
        user_id=target_user_id,
        verification_id=verification_id,
    )
    charged = await spend_points(
        session,
        group_id=group_id,
        user_id=target_user_id,
        points=cost,
        reason=SPEND_REASON_CHALLENGE,
        ref=spend_ref,
    )
    await session.commit()
    if not charged:
        log.info(
            "moderation challenge skip refused | group=%s user=%s cost=%s ref=%s",
            group_id,
            target_user_id,
            cost,
            spend_ref,
        )
        await _ack_security_callback(
            callback,
            f"积分不足 {cost} 分，请完成人机验证",
            show_alert=True,
        )
        return

    log.info(
        "moderation challenge skipped with points | group=%s user=%s cost=%s ref=%s",
        group_id,
        target_user_id,
        cost,
        spend_ref,
    )
    # 扣分和放行共用管理员"通过"那条链路：lease、恢复权限、终态落库、补偿重试都在那一处
    await _handle_verification_admin_callback(
        callback,
        session,
        settings,
        action=VERIFICATION_CALLBACK_APPROVE,
        target_user_id=target_user_id,
        session_factory=session_factory,
        system_override=True,
        system_points=cost,
    )

    if await _challenge_release_owned(session, verification_id=verification_id):
        return
    refunded = await refund_points(
        session,
        group_id=group_id,
        user_id=target_user_id,
        points=cost,
        original_ref=spend_ref,
    )
    await session.commit()
    log.warning(
        "moderation challenge skip did not release the member; points refunded | "
        "group=%s user=%s cost=%s refunded=%s ref=%s",
        group_id,
        target_user_id,
        cost,
        refunded,
        spend_ref,
    )


# --------------------------------------------------------------------------- #
# 申诉复核的冷却（F-023）
# --------------------------------------------------------------------------- #
#: 同一名成员两次申诉复核之间的最小间隔（秒）。
#:
#: 依据：``jv:p:<uid>`` 是成员可见按钮，每点一次都会触发一次完整的审核模型复核
#: （阶段预算 35s）；复核维持原判时质询记录仍是 pending，所以连点没有任何次数
#: 上限，一个被质询的成员就能持续占用 HIGH 更新通道（4 个 worker，同时还承载
#: chat_member / 入群验证）。一次复核 + 少量 Telegram 往返远小于 1 分钟，而正常
#: 成员在拿到结果前重复点击对本人没有任何好处，所以 60 秒只掐掉"连点/循环"，
#: 不影响任何正常使用。与 ``/report``、``@admin`` 的既有冷却同一写法：只用单调
#: 时钟的内存字典，进程重启清零。
_APPEAL_RECHECK_COOLDOWN_SECONDS = 60.0
#: 内存字典上限：满了先清已经过期的条目，避免长期运行只增不减。
_APPEAL_RECHECK_MAX_TRACKED = 4096
_APPEAL_RECHECK_AT: dict[tuple[int, int], float] = {}


def _appeal_recheck_remaining(group_id: int, user_id: int) -> float:
    """还要等多少秒才能再次复核（0 表示现在就可以）。"""

    last = _APPEAL_RECHECK_AT.get((int(group_id), int(user_id)))
    if last is None:
        return 0.0
    return max(0.0, _APPEAL_RECHECK_COOLDOWN_SECONDS - (time.monotonic() - last))


def _claim_appeal_recheck(group_id: int, user_id: int) -> float:
    """占住这次复核的冷却位；返回 >0 表示本次被冷却挡下（值为还需等待秒数）。

    冷却在**提交时**落下（不是成功后）：它是一条限速线，不是锁——模型侧失败时
    成员最多等一个冷却周期再试，而连点/循环立刻被挡住。
    """

    key = (int(group_id), int(user_id))
    remaining = _appeal_recheck_remaining(*key)
    if remaining > 0:
        return remaining
    if len(_APPEAL_RECHECK_AT) >= _APPEAL_RECHECK_MAX_TRACKED:
        cutoff = time.monotonic() - _APPEAL_RECHECK_COOLDOWN_SECONDS
        for stale in [k for k, at in _APPEAL_RECHECK_AT.items() if at <= cutoff]:
            _APPEAL_RECHECK_AT.pop(stale, None)
    _APPEAL_RECHECK_AT[key] = time.monotonic()
    return 0.0


async def _handle_verification_appeal_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    target_user_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """「复核 / 开始验证」：先按冷却去抖，再把模型复核交给后台任务（F-023）。

    判定口径与旧实现完全一致：判定为正常（模型明确说未违规、且该成员当天没有别的
    命中）→ 直接恢复发言，不打扰管理员；其余情况（仍判违规、结论不明确、一天内多次
    命中、找不到原文）一律走原有质询流程——送本人去做人机验证，超时照旧封禁。

    变的是**在哪里跑**：``jv:p:<uid>`` 是成员可见按钮，走 HIGH 更新通道，与
    ``chat_member`` / 入群验证共用 ``WEBHOOK_SECURITY_CONCURRENT_UPDATES=4`` 个
    worker，而复核维持原判时记录仍是 pending，连点没有次数上限——一个被质询的成员
    循环点击就能把这条安全通道拖住。现在：
      1. 按 ``(group_id, user_id)`` 冷却去抖（见 ``_claim_appeal_recheck``）；
      2. 复核提交到 ``policy`` lane 的后台任务（2 个独立 worker），handler 立刻回执
         并返回，不再占用更新 worker。
    """

    operator = callback.from_user
    if operator is None or int(operator.id) != target_user_id:
        await _ack_security_callback(
            callback, "仅被质询的成员本人可以点击", show_alert=True
        )
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    if str(record.kind or "") != VERIFICATION_KIND_MODERATION:
        await _ack_security_callback(callback, "该验证不支持复核", show_alert=True)
        return

    group_id = int(record.group_id)
    display_name = str(getattr(record, "display_name", "") or "")

    remaining = _claim_appeal_recheck(group_id, target_user_id)
    if remaining > 0:
        log.info(
            "appeal recheck throttled | group=%s user=%s remaining=%.0fs",
            group_id,
            target_user_id,
            remaining,
        )
        await _ack_security_callback(
            callback,
            f"刚刚已经复核过了，请 {max(1, int(remaining) + 1)} 秒后再试",
            show_alert=True,
        )
        return

    if session_factory is None:
        # workflow data 一定会注入 session_factory；真的没有时保持原有内联行为，
        # 绝不能让"复核"变成静默不做事。
        log.warning(
            "appeal recheck has no session factory; running inline | group=%s user=%s",
            group_id,
            target_user_id,
        )
        await _run_verification_appeal_recheck(
            callback,
            session,
            settings,
            group_id=group_id,
            target_user_id=target_user_id,
            display_name=display_name,
            session_factory=None,
            deferred=False,
        )
        return

    async def operation() -> None:
        async with session_factory() as work_session:
            await _run_verification_appeal_recheck(
                callback,
                work_session,
                settings,
                group_id=group_id,
                target_user_id=target_user_id,
                display_name=display_name,
                session_factory=session_factory,
                deferred=True,
            )

    submission = submit_privileged_task(
        key=f"verification-appeal:{group_id}:{target_user_id}",
        label=(
            f"verification appeal recheck for {target_user_id} in {group_id}"
        ),
        operation=operation,
        lane="policy",
        priority=0,
        timeout_seconds=120.0,
    )
    if submission.accepted:
        await _ack_security_callback(
            callback,
            "正在复核，结果马上出来…" if submission.created else "复核正在进行中…",
        )
        return

    # 队列饱和不能静默吞掉一次申诉：让这条 update 稍后重放，并如实告知点击者。
    log.error(
        "appeal recheck queue rejected; scheduling durable retry | "
        "group=%s user=%s reason=%s",
        group_id,
        target_user_id,
        submission.reason,
    )
    request_current_update_retry()
    await _ack_security_callback(
        callback,
        "复核通道正忙，本次操作会自动重试。",
        show_alert=True,
    )


async def _run_verification_appeal_recheck(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    *,
    group_id: int,
    target_user_id: int,
    display_name: str,
    session_factory: async_sessionmaker[AsyncSession] | None,
    deferred: bool,
) -> None:
    """复核体（旧内联逻辑原样搬过来，判定口径不变）。

    ``deferred=True`` 表示自己在后台任务里跑（callback 已经回执过），
    ``False`` 表示退化路径：没有 session_factory，只能在 handler 里内联跑，
    此时保持旧的回执方式。
    """

    original_text, repeats = await _moderation_appeal_context(
        session, group_id, target_user_id
    )
    await session.commit()

    verdict, check_summary = await _review_moderation_appeal(
        session, settings, group_id, original_text
    )
    await session.commit()

    if (
        verdict is not None
        and not verdict.violated
        and verdict.conclusive
        and not repeats
        and original_text
    ):
        log.info(
            "moderation recheck cleared the member | group=%s user=%s | %s",
            group_id,
            target_user_id,
            check_summary,
        )
        await _record_moderation_cleared(
            session,
            group_id=group_id,
            user_id=target_user_id,
            source="appeal_recheck",
            reason=check_summary,
            actor_user_id=int(getattr(callback.from_user, "id", 0) or 0),
        )
        await session.commit()
        await _handle_verification_admin_callback(
            callback,
            session,
            settings,
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=target_user_id,
            session_factory=session_factory,
            system_override=True,
        )
        return

    # Still a violation, or the model would not commit: keep the challenge and
    # send the member into the ordinary human verification.
    log.info(
        "moderation recheck kept the challenge | group=%s user=%s | repeats=%s | %s",
        group_id,
        target_user_id,
        repeats,
        check_summary,
    )
    await _notify_appeal_recheck_kept(
        callback,
        group_id=group_id,
        target_user_id=target_user_id,
        display_name=display_name,
        check_summary=check_summary,
        deferred=deferred,
    )


async def _notify_appeal_recheck_kept(
    callback: CallbackQuery,
    *,
    group_id: int,
    target_user_id: int,
    display_name: str,
    check_summary: str,
    deferred: bool,
) -> None:
    """复核维持原判：把本人送回人机验证。

    正常路径（``deferred=True``，复核跑在后台 policy 任务里）：在群里发一条带
    URL 按钮的通知。旧实现用 ``callback.answer(url=deep_link)`` 直接打开 Mini App，
    但后台任务运行时 callback 已经回执过，Telegram 不允许对同一次点击二次 answer，
    所以改成"点一下按钮进验证"的一条可见回执。

    退化路径（``deferred=False``，没有 session_factory 时只能内联）：保持旧行为，
    直接用 ``callback.answer(url=...)`` 打开 Mini App。

    两条路都必须在发不出去时留日志：不能让成员以为复核通过了。
    """

    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None)
    if not deferred:
        username = await _callback_bot_username(callback)
        if not username:
            await _ack_security_callback(
                callback, "验证入口暂时不可用，请稍后重试", show_alert=True
            )
            return
        await callback.answer(url=build_private_deep_link(username, int(group_id)))
        return

    bot = getattr(callback, "bot", None)
    if chat is None or bot is None or int(getattr(chat, "id", 0) or 0) == 0:
        log.warning(
            "appeal recheck kept but the group entry is gone | group=%s user=%s",
            group_id,
            target_user_id,
        )
        return
    username = await _callback_bot_username(callback)
    if not username:
        log.warning(
            "appeal recheck kept but bot username is unavailable | group=%s user=%s",
            group_id,
            target_user_id,
        )
        return

    shown = html.escape((display_name or "").strip()) or f"用户 {int(target_user_id)}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="开始验证",
                    url=build_private_deep_link(username, int(group_id)),
                )
            ]
        ]
    )
    try:
        await bot.send_message(
            chat_id=int(chat.id),
            text=(
                f"复核结果：{shown} 的这条消息仍判违规。\n"
                f"{html.escape(str(check_summary or '').strip())}\n"
                "请在质询到期前点下面的按钮完成人机验证，超时将被封禁。"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception:
        log.warning(
            "appeal recheck kept notice failed | group=%s user=%s",
            group_id,
            target_user_id,
            exc_info=True,
        )
        return
    log.info(
        "appeal recheck kept notice sent | group=%s user=%s",
        group_id,
        target_user_id,
    )


async def _handle_verification_admin_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    *,
    action: str,
    target_user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    system_override: bool = False,
    system_points: int = 0,
) -> None:
    """Apply one admin decision to a pending verification.

    ``system_override`` marks the caller as the bot itself (the false-positive
    appeal path already authenticated the member): the operator-permission gate
    is skipped, and the card/ack wording says the re-check cleared them instead
    of naming an administrator. Everything else — leasing, permission restore,
    terminal completion, compensation — stays on this one implementation.
    """
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await _ack_security_callback(callback, "验证消息已失效", show_alert=True)
        return
    if operator is None:
        await _ack_security_callback(callback, "无法识别操作者", show_alert=True)
        return
    group_id = int(chat.id)
    operator_id = int(operator.id)
    authorized = await is_group_authorized(session, group_id)
    await session.commit()
    if not authorized:
        await _ack_security_callback(callback, "当前群组未授权", show_alert=True)
        return
    if not system_override and not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=operator_id,
    ):
        await _ack_security_callback(
            callback,
            "仅群管理员及以上权限可操作",
            show_alert=True,
        )
        return

    record = await _verification_callback_record(callback, session, target_user_id)
    if record is None:
        return
    if action == VERIFICATION_CALLBACK_REJECT and is_super_admin_user_id(
        target_user_id, settings
    ):
        await _ack_security_callback(callback, "不能封禁最高管理员", show_alert=True)
        return
    safe_moderation_reject = bool(
        action == VERIFICATION_CALLBACK_REJECT
        and record.kind == VERIFICATION_KIND_MODERATION
        and not bool(getattr(record, "ban_on_timeout", False))
    )
    if action == VERIFICATION_CALLBACK_APPROVE:
        locally_banned = bool(
            await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == group_id,
                    UserWarning.user_id == target_user_id,
                    UserWarning.is_banned.is_(True),
                )
            )
        )
        globally_banned = await is_globally_banned(session, target_user_id)
        await session.commit()
        if locally_banned or globally_banned:
            await _ack_security_callback(
                callback,
                "该用户已被封禁，请先解封后再通过",
                show_alert=True,
            )
            return

    snapshot = _verification_snapshot(record)
    if safe_moderation_reject:
        terminal_status = VERIFICATION_STATUS_UNBANNING
    elif action == VERIFICATION_CALLBACK_APPROVE:
        terminal_status = VERIFICATION_STATUS_RELEASING
    else:
        terminal_status = VERIFICATION_STATUS_ENFORCING
    lease_until = await _lease_terminal_verification(
        session,
        now=now_shanghai_naive(),
        record=record,
        expired=False,
        target_status=terminal_status,
    )
    if lease_until is None:
        await _ack_security_callback(
            callback,
            "验证已由其他操作处理",
            show_alert=True,
        )
        return

    kind = str(snapshot["kind"])
    display_name = str(snapshot["display_name"] or "")
    shown = html.escape(display_name.strip() or str(target_user_id))
    lease_is_current = await join_verification_lease_is_current(
        session,
        verification_id=int(record.id),
        lease_until=lease_until,
        status=terminal_status,
    )
    await session.commit()
    if not lease_is_current:
        await _ack_security_callback(
            callback,
            "验证状态已被更高优先级的权限操作更新",
            show_alert=True,
        )
        return
    if safe_moderation_reject:
        log.warning(
            "refused moderation admin ban without ban-rule authorization | "
            "group=%s user=%s",
            group_id,
            target_user_id,
        )

        async def preserve_existing_ban() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=target_user_id,
            )
            await session.commit()
            return blocked

        async def no_challenge_restriction() -> bool:
            return False

        reconciliation = await reconcile_moderation_ban_after_lost_lease_result(
            callback.bot,
            group_id,
            target_user_id,
            preserve_existing_ban,
            restriction_required=no_challenge_restriction,
        )
        if not reconciliation.ok:
            deferred = await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_UNBANNING,
            )
            await _ack_security_callback(
                callback,
                "该质询不具备封禁授权，后台将继续重试安全放行"
                if deferred
                else "该质询不具备封禁授权，恢复状态已由后台接管",
                show_alert=True,
            )
            return
        if not await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_UNBANNING,
        ):
            await _ack_security_callback(
                callback,
                "权限已按最新策略校准，验证状态由后台继续确认",
                show_alert=True,
            )
            return
        current_state = (
            "已保留现有封禁"
            if reconciliation.final_banned is True
            else "发言权限已恢复"
        )
        released_text = build_verification_progress_text(
            kind=VERIFICATION_KIND_MODERATION,
            status="已取消",
            completed="已阻止未授权封禁",
            current=current_state,
            action=f"<b>{shown}</b> 的旧消息审查质询不具备封禁授权。",
            details="仅来源群规动作明确为 ban 的质询才允许封禁。",
        )
        await _edit_verification_prompt(callback, settings, text=released_text)
        await close_private_challenge_message(
            callback.bot,
            target_user_id,
            int(snapshot.get("private_message_id") or 0),
        )
        await _ack_security_callback(callback, "已阻止未授权封禁并安全处理")
        return
    if action == VERIFICATION_CALLBACK_APPROVE:
        restored = await restore_member_permissions(callback.bot, group_id, target_user_id)
        if not restored:
            deferred = await _defer_terminal_verification(
                session,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_RELEASING,
            )
            await _ack_security_callback(
                callback,
                "权限恢复失败，后台将继续重试放行"
                if deferred
                else "权限恢复失败，恢复工单已由后台接管",
                show_alert=True,
            )
            return
        if not await _complete_terminal_verification(
            session,
            verification_id=int(record.id),
            lease_until=lease_until,
            status=VERIFICATION_STATUS_RELEASING,
        ):
            await _reconcile_stale_restriction(
                SimpleNamespace(bot=callback.bot, chat=chat),
                session,
                user_id=target_user_id,
                session_factory=session_factory,
            )
            await _ack_security_callback(
                callback,
                "权限已按最新策略校准，验证状态由后台继续确认",
                show_alert=True,
            )
            return
        # 放行就等于"这次命中是误伤"：记进审计账本，撑起误伤率报表。
        # 花积分免除不算误伤（那是当事人自己选择的权益）。
        if system_points <= 0:
            operator = callback.from_user
            await _record_moderation_cleared(
                session,
                group_id=int(getattr(record, "group_id", 0) or 0),
                user_id=target_user_id,
                source="appeal_recheck" if system_override else "admin_review",
                reason="复核判为正常消息" if system_override else "管理员判定放行",
                actor_user_id=int(getattr(operator, "id", 0) or 0),
                actor_display=str(getattr(operator, "full_name", "") or ""),
            )
        if system_override:
            # system_points>0：这是"花积分免除质询"，文案要说清分被扣了、质询结束了
            if system_points > 0:
                completed_label = f"已消耗 {system_points} 积分免除质询"
                action_line = (
                    f"<b>{shown}</b> 消耗 {system_points} 积分免除了本次质询，"
                    "发言权限已恢复。"
                )
            else:
                completed_label = "复核为正常消息"
                action_line = (
                    f"<b>{shown}</b> 的消息经机器人复核为正常消息，已恢复发言权限。"
                )
            approved_text = build_verification_progress_text(
                kind=kind,
                status="已通过",
                completed=completed_label,
                current="发言权限已恢复",
                action=action_line,
                details="管理员无需再处理。",
            )
        else:
            approved_text = build_verification_progress_text(
                kind=kind,
                status="已通过",
                completed="已由管理员确认",
                current="发言权限已恢复",
                action=(
                    f"<b>{shown}</b> 已由管理员直接通过消息审查验证。"
                    if kind == VERIFICATION_KIND_MODERATION
                    else f"<b>{shown}</b> 已由管理员直接通过入群验证。"
                ),
                details=(
                    None
                    if kind == VERIFICATION_KIND_MODERATION
                    else "欢迎加入。"
                ),
            )
        await _edit_verification_prompt(callback, settings, text=approved_text)
        await close_private_challenge_message(
            callback.bot,
            target_user_id,
            int(snapshot.get("private_message_id") or 0),
        )
        if system_override:
            ack_line = (
                f"已消耗 {system_points} 积分，质询已免除"
                if system_points > 0
                else "复核为正常消息，已恢复发言权限"
            )
        else:
            ack_line = "已直接通过验证"
        await _ack_security_callback(callback, ack_line)
        if kind == VERIFICATION_KIND_JOIN:
            await send_group_welcome(
                callback.bot,
                session,
                settings,
                group_id=group_id,
                user_id=target_user_id,
                display_name=str(snapshot["display_name"] or ""),
            )
        return

    # An admin rejection is a ban decision for join/patrol/raid prompts and for
    # moderation prompts explicitly issued by a ban rule. Legacy or otherwise
    # unauthorized moderation prompts are released by the guard above.
    ban_state = await mark_group_banned(session, group_id, target_user_id)
    await session.commit()

    # Snapshot before the ban: the resulting leave update clears the live
    # join marker concurrently, and the post-ban residue sweep still needs
    # the membership window.
    residue_marker = member_join_marker(group_id, target_user_id)
    enforced = await ban_member(callback.bot, group_id, target_user_id)
    if not enforced:
        if kind == VERIFICATION_KIND_MODERATION:
            rolled_back = await rollback_group_ban(
                session,
                group_id,
                target_user_id,
                ban_state,
            )
            await session.commit()
            requeued = bool(
                rolled_back
                and not (ban_state and ban_state[1])
                and await _requeue_verification(
                    session,
                    settings,
                    snapshot,
                    verification_id=int(record.id),
                    lease_until=lease_until,
                    status=VERIFICATION_STATUS_ENFORCING,
                )
            )
        else:
            # Keep the durable local ban: the admin's rejection IS the ban
            # policy. The retried enforcement (sweeper kick with preserve_ban)
            # re-applies and then keeps the Telegram ban.
            rolled_back = False
            requeued = await _requeue_verification(
                session,
                settings,
                snapshot,
                verification_id=int(record.id),
                lease_until=lease_until,
                status=VERIFICATION_STATUS_ENFORCING,
            )
        log.warning(
            "verification admin ban failed | kind=%s group=%s user=%s "
            "state_restored=%s",
            kind,
            group_id,
            target_user_id,
            rolled_back,
        )
        await _ack_security_callback(
            callback,
            "封禁失败，验证已保留待处理"
            if requeued
            else "Telegram 封禁失败，群内状态已变化，请人工检查",
            show_alert=True,
        )
        return
    # revoke_messages only hides history from the banned account; the join
    # announcement, raced-in residue, and "X was removed" notice stay visible
    # to everyone else and would keep the unverified name exposed.
    await retract_removed_member_residue(
        callback.bot,
        group_id,
        target_user_id,
        marker=residue_marker,
    )
    # A rejected joiner never passed verification: spoiler the name so a spam
    # display name gets no passive exposure even in the terminal notice. The
    # numeric ID stays visible so admins can still identify the account.
    spoilered = spoiler_display_name(display_name, target_user_id)
    rejected_text = build_verification_progress_text(
        kind=kind,
        status="已拒绝",
        completed="已由管理员审核",
        current="已在当前群封禁",
        action=(
            f"<b>{shown}</b> 的消息审查验证已被管理员拒绝。"
            if kind == VERIFICATION_KIND_MODERATION
            else (
                f"<b>{spoilered}</b>（ID: <code>{target_user_id}</code>）"
                "的入群验证已被管理员拒绝。"
            )
        ),
        details="如需解封请管理员使用 <code>/unban</code> 命令。",
    )
    completed = await _complete_moderation_enforcement_or_reconcile(
        callback.bot,
        session,
        group_id=group_id,
        user_id=target_user_id,
        verification_id=int(record.id),
        lease_until=lease_until,
    )
    if not completed:
        await _ack_security_callback(
            callback,
            "操作已执行，验证状态由后台继续确认",
            show_alert=True,
        )
        return
    await _edit_verification_prompt(callback, settings, text=rejected_text)
    await close_private_challenge_message(
        callback.bot,
        target_user_id,
        int(snapshot.get("private_message_id") or 0),
    )
    await _ack_security_callback(callback, "已直接拒绝验证")


@router.callback_query(F.data.startswith(f"{VERIFICATION_CALLBACK_PREFIX}:"))
async def on_verification_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    parsed = parse_verification_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer("验证按钮参数错误", show_alert=True)
        return
    action, target_user_id = parsed
    if action == VERIFICATION_CALLBACK_START:
        await _handle_verification_start_callback(callback, session, target_user_id)
        return
    if action == VERIFICATION_CALLBACK_SPEND:
        # Answered by the challenged member, not an admin: this branch must sit
        # before the administrator authorization gate below.
        await _handle_verification_spend_callback(
            callback,
            session,
            settings,
            target_user_id,
            session_factory=session_factory,
        )
        return
    if action == VERIFICATION_CALLBACK_APPEAL:
        # Answered by the challenged member, not an admin: this branch must sit
        # before the administrator authorization gate below.
        await _handle_verification_appeal_callback(
            callback,
            session,
            settings,
            target_user_id,
            session_factory=session_factory,
        )
        return
    if session_factory is None:
        await _handle_verification_admin_callback(
            callback,
            session,
            settings,
            action=action,
            target_user_id=target_user_id,
        )
        return

    # Keep the callback unanswered until authorization is known. Otherwise a
    # denial can no longer be shown privately and has to spill into the group.
    # Untrusted callback data still stays in the HIGH admission lane and cannot
    # allocate a CRITICAL job before this check succeeds.
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    message_id = int(getattr(message, "message_id", 0) or 0)
    group_id = int(getattr(chat, "id", 0) or 0)
    if (
        message is None
        or chat is None
        or getattr(chat, "type", "") not in {"group", "supergroup"}
        or operator is None
        or group_id == 0
    ):
        await session.commit()
        await _ack_security_callback(
            callback,
            "验证操作消息已失效",
            show_alert=True,
        )
        return
    if not await is_group_authorized(session, group_id):
        await session.commit()
        await _ack_security_callback(callback, "当前群组未授权", show_alert=True)
        return
    await session.commit()
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(operator.id),
    ):
        await _ack_security_callback(
            callback,
            "仅群管理员及以上权限可操作",
            show_alert=True,
        )
        return
    in_transaction = getattr(session, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await session.commit()

    callback_acknowledged = asyncio.Event()

    async def operation() -> None:
        await callback_acknowledged.wait()
        async with session_factory() as work_session:
            await _handle_verification_admin_callback(
                callback,
                work_session,
                settings,
                action=action,
                target_user_id=target_user_id,
                session_factory=session_factory,
            )

    submission = submit_privileged_task(
        # Approve/reject are mutually exclusive mutations of one generation;
        # omit the action so two buttons cannot execute concurrently.
        key=f"verification-admin:{group_id}:{message_id}:{target_user_id}",
        label=(
            f"verification admin {action} for {target_user_id} "
            f"in {group_id} prompt {message_id}"
        ),
        operation=operation,
        lane="critical",
        priority=0,
        timeout_seconds=120.0,
    )
    try:
        if submission.accepted and submission.created:
            await _ack_security_callback(callback, "正在验证权限并执行…")
            return
        if submission.accepted:
            await _ack_security_callback(
                callback,
                "该验证权限操作正在执行，未重复提交。",
                show_alert=True,
            )
            return

        # Saturation must not silently drop a permission decision, but it also
        # must not move a minutes-long Telegram operation back into the HIGH
        # update worker. Keep the durable inbox row retryable instead.
        log.error(
            "verification admin queue rejected task; scheduling durable retry | "
            "group=%s user=%s reason=%s",
            group_id,
            target_user_id,
            submission.reason,
        )
        request_current_update_retry()
        await _ack_security_callback(
            callback,
            "权限任务队列正忙，本次操作会自动重试。",
            show_alert=True,
        )
    finally:
        callback_acknowledged.set()


async def _handle_shared_challenge_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    kind: str,
) -> None:
    """Shared-button challenge prompts: only mentioned members may use them.

    The callback data carries no user id (many members share one message),
    so authorization is the existence of the clicker's own pending record of
    the matching kind in this group.
    """
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("质询入口已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return

    record = await get_join_verification(session, int(chat.id), int(operator.id))
    if (
        record is None
        or str(record.status or VERIFICATION_STATUS_PENDING)
        != VERIFICATION_STATUS_PENDING
        or record.kind != kind
        or verification_deadline_passed(record.deadline_at)
    ):
        await session.commit()
        await callback.answer("仅被点名的违规成员可点击", show_alert=True)
        return

    await session.commit()
    username = await _callback_bot_username(callback)
    if not username:
        await callback.answer("质询入口暂时不可用，请稍后重试", show_alert=True)
        return
    await callback.answer(
        url=build_private_deep_link(username, int(record.group_id)),
    )


@router.callback_query(F.data == PATROL_VERIFY_CALLBACK_DATA)
async def on_patrol_verify_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _handle_shared_challenge_callback(
        callback, session, kind=VERIFICATION_KIND_PATROL
    )


@router.callback_query(F.data == RAID_VERIFY_CALLBACK_DATA)
async def on_raid_verify_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    await _handle_shared_challenge_callback(
        callback, session, kind=VERIFICATION_KIND_RAID
    )


@router.callback_query(F.data == RAID_REMOVE_CALLBACK_DATA)
async def on_raid_remove_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Administrator-only bulk removal for one raid challenge message."""
    message = callback.message
    chat = getattr(message, "chat", None)
    operator = callback.from_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        await callback.answer("质询消息已失效", show_alert=True)
        return
    if operator is None:
        await callback.answer("无法识别操作者", show_alert=True)
        return
    group_id = int(chat.id)
    authorized = await is_group_authorized(session, group_id)
    await session.commit()
    if not authorized:
        await _ack_security_callback(callback, "当前群组未授权", show_alert=True)
        return
    if not await is_group_admin_or_higher(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        user_id=int(operator.id),
    ):
        await _ack_security_callback(
            callback,
            "仅群管理员可一键移除追溯用户",
            show_alert=True,
        )
        return

    await session.commit()
    if session_factory is not None:
        prompt_message_id = int(message.message_id)
        callback_acknowledged = asyncio.Event()

        async def operation() -> None:
            await callback_acknowledged.wait()
            async with session_factory() as work_session:
                still_authorized = await is_group_authorized(work_session, group_id)
                await work_session.commit()
                still_operator = bool(
                    still_authorized
                    and await is_group_admin_or_higher(
                        bot=callback.bot,
                        session=work_session,
                        settings=settings,
                        group_id=group_id,
                        user_id=int(operator.id),
                    )
                )
                await work_session.commit()
                if not still_operator:
                    log.warning(
                        "raid bulk-remove cancelled after authorization changed | "
                        "group=%s operator=%s prompt=%s",
                        group_id,
                        operator.id,
                        prompt_message_id,
                    )
                    return
                result = await remove_raid_challenged_users(
                    bot=callback.bot,
                    session=work_session,
                    session_factory=session_factory,
                    settings=settings,
                    group_id=group_id,
                    prompt_message_id=prompt_message_id,
                    group_settings=None,
                )
            await _publish_raid_removal_result(
                callback,
                group_id=group_id,
                prompt_message_id=prompt_message_id,
                result=result,
            )

        submission = submit_privileged_task(
            key=f"raid-remove:{group_id}:{prompt_message_id}",
            label=f"raid bulk remove in {group_id} prompt {prompt_message_id}",
            operation=operation,
            lane="critical_bulk",
            priority=10,
            timeout_seconds=180.0,
        )
        try:
            if not submission.accepted:
                await _ack_security_callback(
                    callback,
                    "权限任务队列正忙，本次未受理，请再次点击。",
                    show_alert=True,
                )
            elif not submission.created:
                await _ack_security_callback(
                    callback,
                    "该批移除任务正在执行，未重复提交。",
                    show_alert=True,
                )
            else:
                await _ack_security_callback(callback, "正在验证权限并提交任务…")
        finally:
            callback_acknowledged.set()
        return

    result = await remove_raid_challenged_users(
        bot=callback.bot,
        session=session,
        settings=settings,
        group_id=group_id,
        prompt_message_id=int(message.message_id),
        group_settings=None,
    )
    # The service normally commits every lease/result transition. Keep this
    # callback boundary explicit as well so future no-op paths cannot leave a
    # final read snapshot checked out while editing/answering the Telegram UI.
    await session.commit()
    removed_count = len(result.removed_user_ids)
    failed_count = len(result.failed_user_ids)
    if failed_count == 0:
        try:
            await callback.bot.edit_message_reply_markup(
                chat_id=group_id,
                message_id=int(message.message_id),
                reply_markup=None,
            )
        except Exception:
            log.debug(
                "raid bulk-remove keyboard cleanup failed | group=%s message=%s",
                group_id,
                message.message_id,
                exc_info=True,
            )
    if result.pending_count == 0:
        await callback.answer("该批追溯用户已全部处理", show_alert=True)
    elif failed_count:
        await callback.answer(
            f"已移除 {removed_count} 人，{failed_count} 人移除失败，可稍后重试",
            show_alert=True,
        )
    else:
        await callback.answer(f"已移除 {removed_count} 名被追溯用户")


async def _process_member_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    require_current_membership: bool = False,
) -> None:
    log.info(
        "member join event | group=%s user=%s",
        event.chat.id,
        getattr(getattr(event.new_chat_member, "user", None), "id", "-"),
    )
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
    user = event.new_chat_member.user
    if user.is_bot:
        return
    # Anchor the residue-sweep window before any queued/network wait so a
    # later verification mute or screening ban can retract exactly the
    # messages this membership raced in.
    mark_member_join(int(event.chat.id), int(user.id))
    if require_current_membership and not await _join_member_still_present(
        event,
        int(user.id),
        stage="start",
    ):
        return
    if not await is_group_authorized(session, event.chat.id):
        return
    try:
        await track_group_member(
            session,
            event.chat.id,
            user_id=user.id,
            full_name=user.full_name or "",
            username=user.username or "",
            is_bot=False,
        )
        # Commit now: the rest of this handler awaits network calls (bio
        # fetch, screening LLM) and must not hold the SQLite write lock.
        await session.commit()
    except Exception:
        log.debug("join roster tracking failed | group=%s user=%s", event.chat.id, user.id, exc_info=True)
    group_id = event.chat.id
    user_id = user.id

    if is_super_admin_user_id(user.id, settings):
        record = await get_join_verification(session, group_id, user_id)
        if record is not None:
            await _enforce_pending_moderation_challenge(
                event,
                session,
                settings,
                record,
                display_name=user.full_name or "",
            )
        return

    # Banned users are removed immediately on rejoin, no screening needed.
    globally_banned = await is_globally_banned(session, user_id)
    locally_banned = bool(
        await session.scalar(
            select(UserWarning.id).where(
                UserWarning.group_id == group_id,
                UserWarning.user_id == user_id,
                UserWarning.is_banned.is_(True),
            )
        )
    )
    # End the read transaction before any Telegram API call below.
    await session.commit()
    if globally_banned or locally_banned:
        async def current_ban_policy() -> bool:
            await session.rollback()
            blocked = await verification_release_blocked_by_ban(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()
            return blocked

        async def current_restriction_required() -> bool:
            await session.rollback()
            required = await verification_restriction_required(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()
            return required

        if not await current_ban_policy():
            log.info(
                "join ban snapshot discarded | reason=manual_unban "
                "group=%s user=%s",
                group_id,
                user_id,
            )
            return
        ban_scope = "global" if globally_banned else "local"
        log.info(
            "join blocked | reason=%s_ban group=%s user=%s",
            ban_scope,
            group_id,
            user_id,
        )
        enforcement = await _ban_and_notify(
            event,
            settings,
            user_id=user_id,
            display_name=user.full_name,
            reason=(
                "该用户在全局封禁名单中"
                if globally_banned
                else "该用户在本群封禁名单中"
            ),
            preserve_ban=current_ban_policy,
            restriction_required=current_restriction_required,
        )
        if enforcement.retryable:
            raise RuntimeError("join ban enforcement requires durable retry")
        return

    if manual_unban_generation_is_active(group_id, user_id):
        log.info(
            "join security stopped | reason=recent_manual_unban group=%s user=%s",
            group_id,
            user_id,
        )
        return

    raid_guard = get_raid_guard_service()
    if raid_guard is not None:
        group = await session.get(Group, group_id)
        group_settings = dict(group.settings or {}) if group else None
        # End the read transaction before the Telegram calls inside the
        # raid-guard path so concurrent writers are not blocked.
        await session.commit()
        consumed = await raid_guard.handle_join(
            group_id=group_id,
            user_id=user_id,
            full_name=user.full_name or "",
            username=user.username or "",
            group_settings=group_settings,
        )
        if consumed:
            return

    pending = await get_join_verification(session, group_id, user_id)
    pending_status = (
        str(pending.status or VERIFICATION_STATUS_PENDING)
        if pending is not None
        else ""
    )
    if pending is not None and pending_status in {
        VERIFICATION_STATUS_PREPARING,
        VERIFICATION_STATUS_RELEASING,
        VERIFICATION_STATUS_UNBANNING,
    }:
        # A concurrent setup owns this short lease.  It will either activate
        # the challenge or compensate; after a crash the sweeper restores the
        # member and removes the preparation.  Do not clobber it on a duplicate
        # join update or treat its not-yet-valid prompt as interactive.
        await session.commit()
        log.info(
            "join handling deferred | reason=verification_%s kind=%s "
            "group=%s user=%s",
            pending_status,
            pending.kind,
            group_id,
            user_id,
        )
        return
    if (
        pending is not None
        and pending_status
        in {VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_ENFORCING}
        and pending.kind
        in (
            VERIFICATION_KIND_MODERATION,
            VERIFICATION_KIND_PATROL,
            VERIFICATION_KIND_RAID,
        )
    ):
        await _enforce_pending_moderation_challenge(
            event,
            session,
            settings,
            pending,
            display_name=user.full_name or "",
        )
        return

    # Bio lookup and profile moderation are external network operations.  Do
    # not retain the connection used for the pending-challenge read.
    await session.commit()

    async def _maybe_start_verification() -> bool:
        if manual_unban_generation_is_active(group_id, user_id):
            return False
        await session.rollback()
        if not await is_group_authorized(session, group_id):
            await session.commit()
            return False
        group = await session.get(Group, group_id)
        group_settings = group.settings if group is not None else None
        enabled, provider = join_verification_policy(settings, group_settings)
        await session.commit()
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="verification_policy",
        ):
            return False
        if enabled and join_verification_ready(settings, group_settings):
            await _start_join_verification(
                event,
                session,
                settings,
                user_id=user_id,
                display_name=user.full_name or "",
                provider=provider,
                session_factory=session_factory,
                require_current_membership=require_current_membership,
            )
            return True
        return False

    # Mute first, screen second: the challenge and its full restriction must
    # exist before the slow bio/LLM screening below, otherwise the member
    # keeps the chat's default permissions for the whole screening window and
    # can post freely before any verdict lands.
    verification_started = await _maybe_start_verification()

    async def _admit_member() -> None:
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="admission",
        ):
            return
        # Verification (when enabled) owns the admission moment: the welcome
        # is sent after the challenge passes instead of on the raw join.
        if verification_started or await _maybe_start_verification():
            return
        await send_group_welcome(
            event.bot,
            session,
            settings,
            group_id=group_id,
            user_id=user_id,
            display_name=user.full_name or "",
        )

    if not settings.moderation.enabled:
        await _admit_member()
        return

    bio = await _fetch_user_bio(event, user_id)
    if require_current_membership and not await _join_member_still_present(
        event,
        user_id,
        stage="profile_screening",
    ):
        return
    profile_text = build_join_profile_text(
        full_name=user.full_name or "",
        username=user.username or "",
        bio=bio,
    )
    if not profile_text.strip():
        await _admit_member()
        return

    moderation = ModerationService(settings.moderation, _build_llm(settings))
    exemption_rescreened = False
    while True:
        screening_result = await screen_member_profile_verbose(
            session,
            moderation,
            group_id=group_id,
            user_id=user_id,
            profile_text=profile_text,
        )
        violated, reason, conclusive = screening_result
        skipped_by_exemption = bool(
            getattr(screening_result, "skipped_by_exemption", False)
        )
        log.info(
            "join screening done | group=%s user=%s violated=%s "
            "conclusive=%s reason=%s",
            group_id,
            user_id,
            violated,
            conclusive,
            reason or "-",
        )
        # The profile LLM may take many seconds. End its read transaction and
        # re-authorize in a fresh one before any cache/global-ban/Telegram action.
        await session.rollback()
        if not await is_group_authorized(session, group_id):
            log.info(
                "join screening verdict discarded | reason=group_deauthorized "
                "group=%s user=%s",
                group_id,
                user_id,
            )
            return
        await session.commit()
        if require_current_membership and not await _join_member_still_present(
            event,
            user_id,
            stage="profile_verdict",
        ):
            return
        # A manual /unban performed while profile moderation was in flight is the
        # newer operator intent.  Re-read both the exemption and recovery row before
        # applying the old verdict or starting a new challenge.
        # Obtain SQLite's process-wide writer gate before the final exemption read.
        # Whichever of this stale screening verdict and a concurrent /unban commits
        # last becomes authoritative; an older verdict can no longer delete an
        # exemption that was created while its LLM request was in flight.
        await acquire_group_settings_write_intent(session, group_id)
        if not await is_group_authorized(session, group_id):
            await session.rollback()
            return
        if (
            manual_unban_generation_is_active(group_id, user_id)
            or await is_join_screening_exempt(session, user_id)
        ):
            await session.commit()
            log.info(
                "join screening verdict discarded | reason=manual_unban_exemption "
                "group=%s user=%s",
                group_id,
                user_id,
            )
            return
        current_verification = await get_join_verification(session, group_id, user_id)
        if current_verification is not None and str(current_verification.status or "") in {
            VERIFICATION_STATUS_RELEASING,
            VERIFICATION_STATUS_UNBANNING,
        }:
            await session.commit()
            log.info(
                "join screening verdict discarded | reason=permission_recovery_%s "
                "group=%s user=%s",
                current_verification.status,
                group_id,
                user_id,
            )
            return
        if skipped_by_exemption:
            if exemption_rescreened:
                # A second exemption flip happened during the retry.  Do not
                # admit an unchecked member; let the durable update retry (or
                # the next patrol) resolve the now-stable policy state.
                await session.rollback()
                request_current_update_retry()
                log.info(
                    "join screening deferred | reason=exemption_changed_twice "
                    "group=%s user=%s",
                    group_id,
                    user_id,
                )
                return
            # The exemption existed when screening started but was cancelled
            # before the final policy claim.  Release the writer transaction and
            # run the real profile check once so cancellation is immediately
            # effective instead of admitting an unchecked member.
            exemption_rescreened = True
            await session.rollback()
            log.info(
                "join screening restarted | reason=exemption_cancelled "
                "group=%s user=%s",
                group_id,
                user_id,
            )
            continue
        break

    if not violated:
        # Record the checked signature so on-message re-screening skips this
        # user until their visible profile or the enabled rules change. The
        # on-message signature has no bio (not available there), so store the
        # bio-less variant. Inconclusive verdicts are not cached.
        if conclusive:
            rules_fp = await moderation_rules_fingerprint(session, group_id)
            await mark_profile_screened(
                session,
                group_id,
                user_id,
                profile_hash=profile_screen_signature(
                    full_name=user.full_name or "",
                    username=user.username or "",
                    rules_fingerprint=rules_fp,
                ),
            )
        await session.commit()
        await _admit_member()
        return

    recovery = await lease_join_verification_for_unban(
        session,
        group_id,
        user_id,
        manual_unban=False,
    )
    if recovery is None:
        await session.rollback()
        log.error(
            "join profile ban recovery journal could not be created | group=%s user=%s",
            group_id,
            user_id,
        )
        return
    await mark_profile_screening_group_ban(
        session,
        group_id,
        user_id,
        reason=f"入群资料命中群规: {reason}"[:500],
        target_display=user.full_name or "",
        target_username=user.username or "",
    )
    # The group-local ban policy must be durable and the SQLite write lock released
    # before Telegram ban/notification calls.
    await session.commit()

    async def current_profile_ban_policy() -> bool:
        if manual_unban_generation_is_active(group_id, user_id):
            return False
        await session.rollback()
        if not await is_group_authorized(session, group_id):
            await session.commit()
            return False
        blocked = await verification_release_blocked_by_ban(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return blocked

    async def current_profile_restriction_required() -> bool:
        await session.rollback()
        required = await verification_restriction_required(
            session,
            group_id=group_id,
            user_id=user_id,
        )
        await session.commit()
        return required

    enforcement = await _ban_and_notify(
        event,
        settings,
        user_id=user_id,
        display_name=user.full_name,
        reason=reason,
        preserve_ban=current_profile_ban_policy,
        restriction_required=current_profile_restriction_required,
        publish_notice=False,
    )
    if enforcement.final_banned is not True:
        if enforcement.retryable:
            raise RuntimeError("profile join ban enforcement requires durable retry")
        return
    completed = await complete_leased_join_verification(
        session,
        verification_id=int(recovery.verification_id),
        lease_until=recovery.lease_until,
        status=VERIFICATION_STATUS_UNBANNING,
    )
    if completed:
        await session.commit()
        # Consume the exact recovery generation before rewriting its prompt.
        # A stale sweeper can no longer delete the terminal notice afterward.
        await _publish_profile_screening_ban_notice(
            event,
            settings,
            user_id=user_id,
            display_name=user.full_name,
            reason=reason,
            prompt_message_id=int(recovery.prompt_message_id or 0),
            require_existing_prompt=True,
        )
        await close_private_challenge_message(
            event.bot,
            user_id,
            int(getattr(recovery, "private_message_id", 0) or 0),
        )
        return
    await session.rollback()
    reconciliation = await enforce_ban_with_policy_reconciliation_result(
        event.bot,
        group_id,
        user_id,
        current_profile_ban_policy,
        current_profile_restriction_required,
    )
    if (
        reconciliation.final_banned is None
        and not await _ensure_moderation_recovery_owner(
            session,
            group_id=group_id,
            user_id=user_id,
        )
    ):
        raise RuntimeError("profile join ban reconciliation has no durable owner")


def _member_status_value(member: object) -> str:
    raw = getattr(member, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


@router.my_chat_member()
async def on_bot_membership_change(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Keep authorized-group reachability in sync with Telegram authority.

    Durable inbox replay can deliver an old membership event.  The event's
    transition is therefore only a trigger: current bot membership is fetched
    from Telegram before changing the persisted state.
    """

    if event.chat.type not in ("group", "supergroup"):
        return
    group_id = int(event.chat.id)
    authorized = await session.get(AuthorizedGroup, group_id)
    if authorized is None:
        # Being added to a group never grants authorization implicitly.
        return
    # Do not hold a database read transaction across the Telegram authority
    # check; membership updates are rare but the API timeout is still seconds.
    await session.commit()

    bot_user = getattr(getattr(event, "new_chat_member", None), "user", None)
    bot_user_id = int(getattr(bot_user, "id", 0) or 0)
    if not bot_user_id:
        return

    try:
        async with asyncio.timeout(6.0):
            current = await event.bot.get_chat_member(group_id, bot_user_id)
        current_status = _member_status_value(current)
        present = current_status not in {"left", "kicked"}
        operational = bool(
            current_status == "creator"
            or current_status == "administrator"
            and bool(getattr(current, "can_restrict_members", False))
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not telegram_group_is_unreachable_error(exc):
            log.warning(
                "bot membership refresh failed; authorization unchanged | "
                "group=%s error=%s",
                group_id,
                exc,
            )
            return
        present = False
        operational = False

    changed = await set_group_bot_present(
        session,
        group_id,
        present=present,
    )
    if not changed and not (present and operational):
        return
    if present:
        if operational:
            resumed = await resume_group_verification_recovery(session, group_id)
            refreshed = await extend_pending_verification_deadlines(
                session,
                settings=settings,
                group_id=group_id,
            )
        else:
            resumed = 0
            refreshed = 0
    else:
        resumed = 0
        refreshed = 0
    await session.commit()
    log.info(
        "authorized group bot membership changed | group=%s present=%s "
        "operational=%s recovery_resumed=%s pending_refreshed=%s",
        group_id,
        present,
        operational,
        resumed,
        refreshed,
    )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_member_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    event_update: Update | None = None,
) -> None:
    """Hand join security work to its reserved bounded lane.

    The critical administrator lane is separate, so slow profile moderation
    can no longer delay ``/ban``/``/unban``. Direct unit calls without the
    injected factory retain the synchronous implementation.
    """

    if session_factory is None:
        await _process_member_join(event, session, settings)
        return
    if event.chat.type not in ("group", "supergroup"):
        return
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is None or bool(getattr(user, "is_bot", False)):
        return
    _invalidate_admin_cache(event)
    # The queued security job may start seconds from now; the sweep window
    # must open at update receipt or the raced-in messages predate it.
    mark_member_join(int(event.chat.id), int(user.id))
    await session.commit()

    pair = (int(event.chat.id), int(user.id))
    update_id = int(getattr(event_update, "update_id", 0) or 0)
    pending = _PENDING_MEMBER_JOIN_SECURITY.get(pair)
    if pending is None:
        global _MEMBER_JOIN_JOB_SEQUENCE
        _MEMBER_JOIN_JOB_SEQUENCE += 1
        pending = _PendingMemberJoinSecurity(
            job_key=(
                f"member-join:{pair[0]}:{pair[1]}:"
                f"{update_id or _MEMBER_JOIN_JOB_SEQUENCE}"
            ),
            version=1,
            latest_update_id=update_id,
            event=event,
            settings=settings,
        )
        _PENDING_MEMBER_JOIN_SECURITY[pair] = pending
    else:
        # Exact durable replays attach their receipt to the same job. A newer
        # Telegram update replaces the snapshot and forces the active job to
        # run another generation before it can complete either receipt.
        is_new_generation = (
            update_id > pending.latest_update_id
            if update_id and pending.latest_update_id
            else pending.event is not event
        )
        if is_new_generation:
            pending.version += 1
            pending.latest_update_id = update_id
            pending.event = event
            pending.settings = settings

    async def operation() -> None:
        while True:
            generation = pending.version
            current_event = pending.event
            current_settings = pending.settings
            async with session_factory() as work_session:
                await _process_member_join(
                    current_event,
                    work_session,
                    current_settings,
                    session_factory=session_factory,
                    require_current_membership=True,
                )
            if pending.version != generation:
                continue
            if _PENDING_MEMBER_JOIN_SECURITY.get(pair) is pending:
                _PENDING_MEMBER_JOIN_SECURITY.pop(pair, None)
            return

    submission = submit_privileged_task(
        key=pending.job_key,
        label=f"member join security {int(user.id)} in {int(event.chat.id)}",
        operation=operation,
        lane="security",
        priority=0,
        timeout_seconds=180.0,
    )
    if submission.accepted:
        return

    # Fail-safe path: never silently drop a join security event. Queue
    # saturation is exceptional and may occupy this update worker, but it is
    # preferable to admitting a banned or unverified member unchecked.
    log.error(
        "join security queue rejected event; running inline | group=%s user=%s reason=%s",
        int(event.chat.id),
        int(user.id),
        submission.reason,
    )
    if _PENDING_MEMBER_JOIN_SECURITY.get(pair) is pending:
        _PENDING_MEMBER_JOIN_SECURITY.pop(pair, None)
    await _process_member_join(event, session, settings)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_member_leave(
    event: ChatMemberUpdated, session: AsyncSession, settings: Settings
) -> None:
    """Drop join verification on leave, but retain moderation challenges.

    Join verification is only relevant while the member is present. A message
    challenge must survive leave/rejoin or the sender could evade its timeout.
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
    user = getattr(getattr(event, "new_chat_member", None), "user", None)
    if user is None:
        return
    # A rejoin must open a fresh sweep window; messages sent while this
    # membership was legitimately verified are not residue.
    clear_member_join_marker(int(event.chat.id), int(user.id))
    try:
        await mark_group_member_left(session, event.chat.id, user.id)
    except Exception:
        log.debug("leave roster tracking failed | group=%s user=%s", event.chat.id, user.id, exc_info=True)
    record = await get_join_verification(session, event.chat.id, user.id)
    if record is None:
        return
    if record.kind in (
        VERIFICATION_KIND_MODERATION,
        VERIFICATION_KIND_PATROL,
        VERIFICATION_KIND_RAID,
    ):
        log.info(
            "%s challenge retained | reason=left group=%s user=%s",
            record.kind,
            event.chat.id,
            user.id,
        )
        return
    record_status = str(record.status or VERIFICATION_STATUS_PENDING)
    if record_status != VERIFICATION_STATUS_PENDING:
        log.info(
            "join verification retained | reason=terminal_%s group=%s user=%s",
            record_status,
            event.chat.id,
            user.id,
        )
        return
    prompt_message_id = int(record.prompt_message_id or 0)
    if await delete_join_verification(session, event.chat.id, user.id):
        # Commit before the Telegram call so a transient API failure cannot
        # roll back the terminal leave cleanup. The message is best-effort.
        await session.commit()
        bot = getattr(event, "bot", None)
        if bot is not None:
            await delete_verification_prompt(
                bot,
                int(event.chat.id),
                prompt_message_id,
            )
        log.info(
            "join verification cancelled | reason=left group=%s user=%s",
            event.chat.id,
            user.id,
        )


@router.chat_member()
async def on_member_status_change(
    event: ChatMemberUpdated, session: AsyncSession, settings: Settings
) -> None:
    """Catch-all for the remaining transitions (promote/demote/restrict).

    Registered after on_member_join / on_member_leave, so it only sees the
    transitions those filters do not match. Its only job is admin-status cache
    invalidation so a demoted admin loses moderation exemption on their next
    message.
    """
    if event.chat.type not in ("group", "supergroup"):
        return
    _invalidate_admin_cache(event)
