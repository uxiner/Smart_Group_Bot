"""1 对 1 私聊处理器：文字闲聊 + 图片理解。

用户口径（2026-10-03）：只有已授权群的成员能私聊；每人 20 条/天、全局 200 条/天；
私聊不做群规审核（NSFW 也放开，群里那条底线不受影响）；私聊内容不落库、不进
归档/记忆/向量，只在内存里保留最近几轮做上下文。

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
from bot.services.private_chat import (
    ACCESS_UNKNOWN_NOTICE,
    BUSY_NOTICE,
    DM_VISION_PROMPT,
    MEDIA_UNSUPPORTED_NOTICE,
    NOT_MEMBER_NOTICE,
    build_private_chat_messages,
    confirm_authorized_group_member,
    consume_daily_quota,
    history_store,
    local_day_key,
    notice_throttle,
    quota_notice,
)

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

    # 1) 准入：只有已授权群的成员能用（超管豁免）。不确定 → 不加钱也不拒绝。
    allowed = await confirm_authorized_group_member(message.bot, session, settings, user.id)
    await session.commit()
    if allowed is None:
        await _send_notice(message, ACCESS_UNKNOWN_NOTICE, "access_unknown")
        return
    if not allowed:
        await _send_notice(message, NOT_MEMBER_NOTICE, "not_member")
        return

    # 2) 配额：先扣再用（每人 / 全局两道闸门）。
    try:
        outcome = await consume_daily_quota(session, user_id=user.id)
    except Exception as exc:
        await session.rollback()
        log.warning("private chat: 配额判定失败 | user=%s | error=%s", user.id, exc)
        await _send_notice(message, BUSY_NOTICE, "busy")
        return
    if not outcome.allowed:
        log.info(
            "private chat: 配额用尽 | user=%s | reason=%s | 今日=%s/%s 全局=%s/%s",
            user.id,
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
    history = history_store().history(user.id)
    messages = build_private_chat_messages(
        text,
        history=history,
        sender_user_id=user.id,
        sender_username=str(user.username or ""),
        sender_is_owner=False,
        sender_is_tg_admin=False,
        image_description=image_description,
    )
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        reply = str(await _reply_llm(settings).chat(messages, stage="dm") or "").strip()
    except Exception as exc:
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
    user_turn = text or "[图片]"
    if image_description:
        user_turn = f"{user_turn}\n[图片内容] {image_description}"
    store.append(user.id, "user", user_turn)
    store.append(user.id, "assistant", reply)
    log.info(
        "private chat: 已回复 | user=%s | 今日=%s/%s | 全局=%s/%s | 日=%s | chars=%d",
        user.id,
        outcome.user_used,
        outcome.per_user_limit,
        outcome.global_used,
        outcome.global_limit,
        local_day_key(),
        len(reply),
    )
