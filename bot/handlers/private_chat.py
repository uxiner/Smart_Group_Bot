"""1 对 1 私聊处理器：文字闲聊 + 图片理解。

用户口径（2026-10-03）：只有已授权群的成员能私聊（普通成员 100 条/天、群管理员
500 条/天，各自的全局上限 20000 / 100000 条/天，最高管理员不设限）；私聊不做群规
审核（NSFW 也放开，群里那条底线不受影响）；私聊正文**只落私聊自己的表**
（``private_chat_messages``，不进群归档/记忆/向量），按 token 预算装配后作为历史，
所以机器人重启不失忆、也能记住很久以前说过的话。

**注册顺序**：这个 router 必须排在 ``group.router`` **之前**——群消息处理器用的是
``F.text | F.photo | ...`` 这种宽泛过滤，且靠函数体里 ``is_group()`` 早退，谁先注册
谁先吃消息。排它前面才能保证私聊消息不会被群处理器吃掉。
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.services.authz import list_authorized_groups
from bot.services.context_gate import context_token_budget
from bot.services.dm_search import answer_with_search
from bot.services.group_public_context import (
    load_group_titles,
    load_user_public_group_context,
)
from bot.services.long_term_memory import (
    load_private_chat_facts,
    memory_facts_enabled,
    memory_recall_limit,
)
from bot.services.private_chat import (
    ACCESS_UNKNOWN_NOTICE,
    BUSY_NOTICE,
    DM_VISION_PROMPT,
    MEDIA_UNSUPPORTED_NOTICE,
    NOT_MEMBER_NOTICE,
    TIER_ADMIN,
    QuotaOutcome,
    build_private_chat_messages,
    consume_daily_quota,
    history_store,
    last_contact_record,
    load_private_history,
    local_day_key,
    notice_throttle,
    private_history_token_budget,
    quota_notice,
    record_contact,
    record_private_turn,
    resolve_access,
)
from bot.services.search_memory import SCOPE_PRIVATE, load_search_records

log = logging.getLogger(__name__)

router = Router(name="private_chat")

#: 单条回复的切割长度（Telegram 上限 4096，留一点余量）
MAX_REPLY_CHARS = 3800
#: 认图的总预算（秒）：下载 + 视觉调用一起算
VISION_BUDGET_SEC = 30.0

_IMAGE_DOCUMENT_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def _reply_llm(settings: Settings):
    """按当前生效配置构造 LLMService（与其它 handler 的常规做法一致）。

    每个角色都显式传参：漏传的角色会静默塌到 ``main``，那样这条路就会悄悄
    用错模型。私聊回复走 ``main`` 阶段，开不开 thinking 由该角色的
    ``request_params`` 决定，这里不做任何覆盖。
    """

    from bot.services.llm import LLMService

    bot_cfg = settings.bot
    return LLMService(
        bot_cfg.main_model,
        bot_cfg.decision_model,
        bot_cfg.compress_model,
        moderation=bot_cfg.moderation_model,
        vision=bot_cfg.vision_model,
        embed=bot_cfg.embed_model,
        skill=bot_cfg.skill_model,
        max_context_tokens=bot_cfg.max_context_tokens,
        context_window_mode=getattr(bot_cfg, "context_window_mode", None),
    )


def _message_text(message: Message) -> str:
    return str(message.text or message.caption or "").strip()


def _has_media(message: Message) -> bool:
    return any(
        getattr(message, attr, None)
        for attr in (
            "photo",
            "document",
            "sticker",
            "animation",
            "video",
            "video_note",
            "voice",
            "audio",
        )
    )


def _image_file_info(message: Message):
    """图片类消息 → ``(file_id, mime, size)``；不是图片就返回 None。"""

    from bot.handlers.group import _extract_image_file_info

    info = _extract_image_file_info(message)
    if info:
        return info
    document = getattr(message, "document", None)
    mime = str(getattr(document, "mime_type", "") or "")
    if document and mime in _IMAGE_DOCUMENT_MIMES:
        return (document.file_id, mime, int(getattr(document, "file_size", 0) or 0))
    return None


async def _image_description(message: Message, llm) -> str:
    from bot.handlers.group import _build_vision_data_uri

    info = _image_file_info(message)
    if not info:
        return ""
    try:
        async with asyncio.timeout(VISION_BUDGET_SEC):
            data_uri = await _build_vision_data_uri(message, *info)
            if not data_uri:
                return ""
            return str(await llm.vision_describe(data_uri, DM_VISION_PROMPT) or "").strip()
    except TimeoutError:
        log.warning("private chat: 认图超预算 | user=%s", message.from_user.id if message.from_user else 0)
        return ""
    except Exception as exc:
        log.warning("private chat: 认图失败 | error=%s", exc)
        return ""


def _split_for_telegram(text: str, *, limit: int = MAX_REPLY_CHARS) -> list[str]:
    """按行切分长回复；单行超长时硬切。"""

    body = str(text or "").strip()
    if not body:
        return []
    chunks: list[str] = []
    current = ""
    for line in body.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


async def _send_notice(message: Message, text: str, key: str) -> None:
    """节流后的提示：同一个人同一种提示每小时只说一次。"""

    user = message.from_user
    if user is None or not notice_throttle().allow(user.id, key):
        return
    try:
        await message.answer(text, parse_mode=None)
    except Exception as exc:
        log.warning("private chat: 提示发送失败 | user=%s | error=%s", user.id, exc)


async def _send_reply(message: Message, text: str) -> None:
    for chunk in _split_for_telegram(text):
        await message.answer(chunk, parse_mode=None)


def _stored_user_turn(text: str, image_description: str) -> str:
    """这一轮用户消息**进历史**的正文（内存缓冲与库共用同一段文字）。

    图片消息存 ``[图片内容] <视觉描述>``——就是这段描述交给模型，历史里也存同一段，
    这样「读库」和「读内存兜底」装配出来的上下文一字不差。正文与配文都为空时（纯图片
    且认图没给出描述）留一个 ``[图片]`` 占位，避免这一轮在历史里凭空消失。
    """

    body = str(text or "").strip() or "[图片]"
    description = str(image_description or "").strip()
    if description:
        return f"{body}\n[图片内容] {description}"
    return body


@router.message(F.chat.type == ChatType.PRIVATE)
async def on_private_message(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return

    text = _message_text(message)
    if text.startswith("/"):
        # 命令交给命令路由（本 router 排在它们后面一个层级，这里只是兜底不抢）
        return
    if not text and not _has_media(message):
        return

    # 1) 准入：只有已授权群的成员能用；档位同时决定配额阶梯。
    verdict = await resolve_access(message.bot, session, settings, user.id)
    await session.commit()
    if verdict.allowed is None:
        await _send_notice(message, ACCESS_UNKNOWN_NOTICE, "access_unknown")
        return
    if not verdict.allowed:
        await _send_notice(message, NOT_MEMBER_NOTICE, "not_member")
        return

    # 2) 配额：先扣再用（每人 + 本档全局两道闸门）。最高管理员不设限、不计数。
    outcome: QuotaOutcome | None = None
    if verdict.is_super:
        # 超管不设限，但仍记一笔联系：亲密度考勤必须有真实数据（失败不影响回复）
        await record_contact(session, user_id=user.id)
    else:
        try:
            outcome = await consume_daily_quota(
                session,
                user_id=user.id,
                is_admin=verdict.is_admin,
            )
        except Exception as exc:
            await session.rollback()
            log.warning("private chat: 配额判定失败 | user=%s | error=%s", user.id, exc)
            await _send_notice(message, BUSY_NOTICE, "busy")
            return
        if not outcome.allowed:
            log.info(
                "private chat: 配额用尽 | user=%s | 档=%s | reason=%s | 今日=%s/%s 本档全局=%s/%s",
                user.id,
                verdict.tier,
                outcome.reason,
                outcome.user_used,
                outcome.per_user_limit,
                outcome.global_used,
                outcome.global_limit,
            )
            await _send_notice(message, quota_notice(outcome), "quota")
            return

    # 3) 媒体：只处理图片；其它类型回一句说明（不占模型）。
    image_description = ""
    if _has_media(message):
        if _image_file_info(message) is None:
            await _send_notice(message, MEDIA_UNSUPPORTED_NOTICE, "media")
            return
        llm = _reply_llm(settings)
        image_description = await _image_description(message, llm)
        if not image_description and not text:
            await _send_notice(message, BUSY_NOTICE, "busy")
            return

    # 4) 组装 + 调用（model 走 main 阶段；stage 标签只为用量看板好区分）。
    #    历史优先读库（重启不失忆），按 token 预算装配；读不到才退回内存兜底。
    history = await load_private_history(
        session,
        user.id,
        budget_tokens=private_history_token_budget(settings),
        fallback=history_store(),
    )
    # 亲密度考勤：只有最高管理员才给这段真实记录（拿不到就是空串）。
    last_contact = ""
    if verdict.is_super:
        last_contact = await last_contact_record(session, user_id=user.id)

    # 第 3 期：两层只读的资料注入。
    # 1) 这个人以前搜过的结果留档（带「搜索于 …，距今 …」，过期会标注）；
    search_records = await load_search_records(
        session,
        scope=SCOPE_PRIVATE,
        scope_id=user.id,
    )
    # 2) 他在**已授权群里公开**说过 / 公开讨论过的内容（方向只允许「群 → 私聊」）。
    #    准入判定已经逐个 getChatMember 确认过哪些群他在里面，直接用那个结果；
    #    最高管理员准入豁免（判定过程一个查询都不打），这里按「所有授权群」算。
    group_ids = verdict.group_ids
    if verdict.is_super:
        try:
            group_ids = tuple(
                int(row.group_id) for row in await list_authorized_groups(session)
            )
        except Exception as exc:  # 拿不到群列表就不注入群聊公开记录
            log.warning(
                "private chat: 超管可见群列表查询失败（本次不注入群聊公开记录） | error=%s",
                exc,
            )
            group_ids = ()
    group_titles = await load_group_titles(session, group_ids)
    group_public_records = await load_user_public_group_context(
        query=text,
        group_ids=group_ids,
        titles=group_titles,
    )
    # 3) 第 4 期：长期记忆——本人 private 事实 **加上** 该用户可访问群的 group 事实
    #    （方向仍只允许「群 → 私聊」；可访问群用的就是上面准入判定确认过的那批）。
    #    相关才注入：没有命中就返回空，不硬塞。总开关关掉时一个字节都不读。
    long_term_facts = []
    if memory_facts_enabled(settings):
        long_term_facts = await load_private_chat_facts(
            session,
            user_id=user.id,
            group_ids=group_ids,
            query=text,
            limit=memory_recall_limit(settings),
            titles=group_titles,
        )

    messages = build_private_chat_messages(
        text,
        history=history,
        sender_user_id=user.id,
        sender_username=str(user.username or ""),
        sender_is_owner=verdict.is_super,
        sender_is_tg_admin=verdict.is_admin,
        image_description=image_description,
        last_contact=last_contact,
        search_records=search_records,
        group_public_records=group_public_records,
        long_term_facts=long_term_facts,
        group_titles=group_titles,
        budget_tokens=context_token_budget(settings),
    )
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    llm = _reply_llm(settings)
    try:
        answer = await answer_with_search(
            llm,
            messages,
            stage="dm",
            user_text=text,
            settings=settings,
            session=session,
            scope_id=user.id,
        )
        reply = str(answer.text or "").strip()
    except Exception as exc:
        await session.rollback()
        log.warning("private chat: 回复失败 | user=%s | error=%s", user.id, exc)
        await _send_notice(message, BUSY_NOTICE, "busy")
        return
    if not reply:
        await _send_notice(message, BUSY_NOTICE, "busy")
        return

    try:
        await _send_reply(message, reply)
    except Exception as exc:
        log.warning("private chat: 发送失败 | user=%s | error=%s", user.id, exc)
        return

    store = history_store()
    user_turn = _stored_user_turn(text, image_description)
    store.append(user.id, "user", user_turn)
    store.append(user.id, "assistant", reply)
    # 落库：这一轮两行（用户 + 回复）。幂等键来自入站 message_id，Telegram 重投递
    # 同一轮不会写出第二份；写失败只记日志，绝不影响这次已经发出去的回复。
    await record_private_turn(
        session,
        user_id=user.id,
        user_content=user_turn,
        assistant_content=reply,
        message_id=getattr(message, "message_id", None),
    )
    log.info(
        "private chat: 已回复 | user=%s | 档=%s | 今日=%s | 本档全局=%s | 日=%s | chars=%d",
        user.id,
        verdict.tier,
        f"{outcome.user_used}/{outcome.per_user_limit}" if outcome else "不限",
        f"{outcome.global_used}/{outcome.global_limit}" if outcome else "不限",
        local_day_key(),
        len(reply),
    )
    if answer.searches:
        log.info(
            "private chat: 本轮联网检索 %d 次 | user=%s | 保险丝已触发=%s",
            answer.searches,
            user.id,
            answer.exhausted,
        )
