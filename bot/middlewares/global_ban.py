from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import UserWarning, Violation
from bot.services.authz import is_group_authorized, is_super_admin_user_id
from bot.services.ban_audit import record_ban_event
from bot.services.join_screening import is_globally_banned
from bot.services.join_verification import (
    enforce_ban_with_policy_reconciliation_result,
    reconcile_moderation_ban_after_lost_lease,
    verification_restriction_required,
)
from bot.services.ops_alert import alert_super_admin
from bot.services.recent_messages import retract_removed_member_residue
from bot.services.update_completion import request_current_update_retry

log = logging.getLogger(__name__)

#: Delays between immediate re-reads of the ban policy. Two extra attempts absorb
#: a single SQLite "database is locked" hiccup; total added latency is bounded at
#: 0.3s and this only runs on the failure path. A retry here costs a database
#: read, not a model call, so it does not add a cost-board entry.
_BAN_LOOKUP_RETRY_DELAYS: tuple[float, ...] = (0.1, 0.2)
#: One alert per kind per cooldown window (see ``bot.services.ops_alert``).
_BAN_ALERT_KIND = "global_ban_lookup_failed"


async def _confirm_pending_local_bans(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    user_id: int,
) -> int:
    """Promote durable unconfirmed violation attempts after a later re-ban.

    The CAS makes concurrent messages harmless: only the worker that changes a
    row from False to True appends the success audit.  The update and audit share
    one transaction, so a database failure keeps the inbox update retryable.
    """

    async with session_factory() as session:
        authorized = await is_group_authorized(session, int(group_id))
        globally_banned = authorized and await is_globally_banned(
            session,
            int(user_id),
        )
        locally_banned = bool(
            authorized
            and await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                    UserWarning.is_banned.is_(True),
                )
            )
        )
        if not globally_banned and not locally_banned:
            await session.commit()
            return -1
        pending = list(
            await session.scalars(
                select(Violation).where(
                    Violation.group_id == int(group_id),
                    Violation.user_id == int(user_id),
                    Violation.ban_enforced.is_(False),
                )
            )
        )
        confirmed = 0
        for violation in pending:
            result = await session.execute(
                update(Violation)
                .where(
                    Violation.id == int(violation.id),
                    Violation.ban_enforced.is_(False),
                )
                .values(ban_enforced=True)
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:
                continue
            confirmed += 1
            await record_ban_event(
                session,
                group_id=group_id,
                target_user_id=user_id,
                action="ban",
                source="ban_reconciliation",
                outcome="succeeded",
                reason="后续消息触发持久封禁重试并确认 Telegram 已封禁",
                evidence=str(violation.message_text or ""),
                reference_type="violation",
                reference_id=int(violation.id),
            )
        await session.commit()
        return confirmed


async def _durable_ban_policy_exists(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    user_id: int,
) -> bool:
    async with session_factory() as session:
        if not await is_group_authorized(session, int(group_id)):
            return False
        if await is_globally_banned(session, int(user_id)):
            return True
        return bool(
            await session.scalar(
                select(UserWarning.id).where(
                    UserWarning.group_id == int(group_id),
                    UserWarning.user_id == int(user_id),
                    UserWarning.is_banned.is_(True),
                )
            )
        )


class GlobalBanEnforcementMiddleware(BaseMiddleware):
    """Blocks every group message from globally or locally banned users.

    Registered as an OUTER middleware on the message observer, so it runs for
    every incoming group message — commands, media, polls, locations — even
    when no handler matches. It opens its own short-lived session because
    outer middlewares run before DbSessionMiddleware. Enforcement deletes the
    message and re-bans the user in that chat. Unauthorized groups are
    skipped entirely.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _lookup_ban_policy(
        self,
        chat_id: int,
        user_id: int,
    ) -> tuple[bool, bool]:
        """Return ``(banned, authorized)``, re-reading a few times on failure.

        A failed read is usually a locked/full SQLite writer rather than a real
        outage, so immediate re-reads recover the *correct* verdict without any
        side effect. A persistent failure propagates to the caller, which then
        leaves every permission untouched (F-027).
        """

        failure: Exception | None = None
        attempts = 1 + len(_BAN_LOOKUP_RETRY_DELAYS)
        for attempt in range(attempts):
            try:
                async with self.session_factory() as session:
                    authorized = await is_group_authorized(session, int(chat_id))
                    globally_banned = authorized and await is_globally_banned(
                        session,
                        int(user_id),
                    )
                    locally_banned = False
                    scalar = getattr(session, "scalar", None)
                    if authorized and callable(scalar):
                        locally_banned = bool(
                            await scalar(
                                select(UserWarning.id).where(
                                    UserWarning.group_id == int(chat_id),
                                    UserWarning.user_id == int(user_id),
                                    UserWarning.is_banned.is_(True),
                                )
                            )
                        )
                return bool(globally_banned or locally_banned), bool(authorized)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                failure = exc
                if attempt < len(_BAN_LOOKUP_RETRY_DELAYS):
                    log.warning(
                        "global ban check failed; retrying | chat=%s user=%s "
                        "attempt=%d/%d",
                        chat_id,
                        user_id,
                        attempt + 1,
                        attempts,
                        exc_info=True,
                    )
                    await asyncio.sleep(_BAN_LOOKUP_RETRY_DELAYS[attempt])
        assert failure is not None
        raise failure

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if (
            chat is None
            or getattr(chat, "type", "") not in ("group", "supergroup")
            or user is None
            or getattr(event, "sender_chat", None) is not None
        ):
            return await handler(event, data)

        settings = data.get("settings")
        if settings is not None and is_super_admin_user_id(user.id, settings):
            return await handler(event, data)

        try:
            banned, authorized = await self._lookup_ban_policy(chat.id, user.id)
        except Exception as failure:
            # F-027（判罚准确第一）：封禁策略读不出来时**不改变任何人的权限、不做
            # 任何处置**。这里选择放行，而不是 fail-closed 拦人：
            #   * fail-closed 会误伤——删除一个正常群友的消息、在全群被挡在外面，
            #     这正是"宁可漏判，不可误伤"禁止的取舍；
            #   * 放行最多是漏判（被全局封禁的人多活一条消息），而且失败是**可观测**的。
            #
            # 立即重试已在 _lookup_ban_policy 里做过（数据库读，零模型成本）；
            # 这里补上明确的错误日志 + 私聊最高管理员告警。
            #
            # **不请求重放**：本条 update 已经按正常流程交给 handler，再重放一次会
            # 让同一条消息被回复/审核两次——重复处置本身也是误伤。
            log.error(
                "global ban policy is unreadable; passing the message through "
                "unchanged (no permission or message change, super admin alerted) "
                "| chat=%s user=%s message=%s error=%s",
                chat.id,
                user.id,
                getattr(event, "message_id", 0),
                failure,
            )
            await alert_super_admin(
                getattr(event, "bot", None),
                settings,
                kind=_BAN_ALERT_KIND,
                summary=(
                    "封禁策略读取失败（已重试仍失败）：本次不改变任何权限、不做任何"
                    "处置，消息按正常流程放行。"
                ),
                fields={
                    "chat": chat.id,
                    "user": user.id,
                    "message": getattr(event, "message_id", 0),
                    "error": f"{type(failure).__name__}: {failure}"[:160],
                },
            )
            return await handler(event, data)

        # The outer middleware owns a private session.  Never keep it checked
        # out while the complete downstream LLM/Telegram handler runs.
        if not authorized or not banned:
            return await handler(event, data)

        log.info("[%s] blocked message from durably banned user %s", chat.id, user.id)
        try:
            await event.delete()
        except Exception:
            pass
        # A banned rejoin may have raced several messages in before this one;
        # sweep the residue from the same fresh-join window and (for a fresh
        # rejoin) arm deletion of the "X was removed" notice the imminent ban
        # will post. Runs before the ban, so the live join marker is intact.
        try:
            await retract_removed_member_residue(event.bot, int(chat.id), int(user.id))
        except Exception:
            log.debug(
                "banned-user residue sweep failed | chat=%s user=%s",
                chat.id,
                user.id,
                exc_info=True,
            )
        try:
            async def preserve_ban() -> bool:
                return await _durable_ban_policy_exists(
                    self.session_factory,
                    group_id=int(chat.id),
                    user_id=int(user.id),
                )

            async def restriction_required() -> bool:
                async with self.session_factory() as policy_session:
                    required = await verification_restriction_required(
                        policy_session,
                        group_id=int(chat.id),
                        user_id=int(user.id),
                    )
                    await policy_session.commit()
                    return required

            enforcement = await enforce_ban_with_policy_reconciliation_result(
                event.bot,
                int(chat.id),
                int(user.id),
                preserve_ban,
                restriction_required,
            )
            final_banned = enforcement.final_banned
            if final_banned is None:
                if not enforcement.retryable:
                    # Missing rights, an unbannable target, or an unreachable
                    # group is deterministic: replaying the update cannot help
                    # and would needlessly demote webhook to polling. The
                    # durable policy stays and can be enforced after the group
                    # setup changes.
                    log.warning(
                        "[%s] durable ban cannot be enforced until group setup "
                        "changes; completing update without retry | user=%s "
                        "unreachable=%s operator_action=%s",
                        chat.id,
                        user.id,
                        enforcement.group_unreachable,
                        enforcement.operator_action_required,
                    )
                else:
                    log.warning(
                        "[%s] durable ban enforcement was not confirmed | user=%s",
                        chat.id,
                        user.id,
                    )
                    request_current_update_retry()
            elif final_banned and await preserve_ban():
                # F-027 验收修补（原为未定义的 ``locally_banned``，基线里就存在、
                # 只是从来走不到这条分支）：「本地是否还有持久封禁策略」在本作用域里
                # 的唯一权威来源就是上面那个 ``preserve_ban`` 闭包（查 DB 的
                # ``_durable_ban_policy_exists``），语义与原来的意图一致。
                try:
                    confirmed = await _confirm_pending_local_bans(
                        self.session_factory,
                        group_id=int(chat.id),
                        user_id=int(user.id),
                    )
                    if confirmed < 0:
                        reconciled = await reconcile_moderation_ban_after_lost_lease(
                            event.bot,
                            int(chat.id),
                            int(user.id),
                            preserve_ban,
                            restriction_required=restriction_required,
                        )
                        if not reconciled:
                            log.error(
                                "[%s] revoked durable ban could not be reconciled | user=%s",
                                chat.id,
                                user.id,
                            )
                except Exception:
                    log.exception(
                        "[%s] durable ban confirmation persistence failed | user=%s",
                        chat.id,
                        user.id,
                    )
                    if await preserve_ban():
                        request_current_update_retry()
                    else:
                        await reconcile_moderation_ban_after_lost_lease(
                            event.bot,
                            int(chat.id),
                            int(user.id),
                            preserve_ban,
                            restriction_required=restriction_required,
                        )
        except Exception:
            log.exception("[%s] global ban enforcement failed | user=%s", chat.id, user.id)
            request_current_update_retry()
        return None
