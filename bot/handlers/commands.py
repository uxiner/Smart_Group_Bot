from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import timedelta

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    WebAppInfo,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.utils.timezone import now_shanghai_naive
from bot.db.models import Admin, AuthorizedGroup, Group, GroupMember
from bot.services.moderation_context import build_moderation_context
from bot.services.authz import (
    ensure_group_admin_permission,
    ensure_group_authorized,
    ensure_super_admin,
    is_group_admin_authorized,
    is_group_authorized,
    is_super_admin_user_id,
)
from bot.services import memory_holder
from bot.services.checkin import (
    CHALLENGE_SKIP_COST,
    CHECKIN_CALLBACK_DATA,
    MemberProfile,
    RankBoard,
    build_rank_board,
    member_profile,
    record_checkin,
    render_checkin_receipt,
    render_checkin_toast,
    summarize,
)
from bot.services.checkin_reminder import (
    find_reminder_slot,
    parse_shop_start_payload,
    render_checkin_reminder,
    today_checkin_roster,
)
from bot.services.av_search import (
    AVDetail,
    AVQuerySession,
    AVQuerySessionStore,
    AVSearchItem,
    AVSearchService,
    is_av_code_query,
)
from bot.services.av_image_lookup import (
    AV_NO_IMAGE_MARKER,
    AV_VISION_MAX_IMAGE_BYTES,
    AV_VISION_PROMPT,
    AV_VISION_TIMEOUT_SEC,
    AVPrivateRateLimiter,
    build_av_image_data_uri,
    extract_av_actor,
    extract_av_code,
    rate_limit_minutes,
    run_with_hard_deadline,
    select_av_image_file,
)
from bot.services.join_verification import (
    maybe_send_private_verification,
    parse_private_verify_group_id,
)
from bot.services.llm import LLMService
from bot.services.member_identity import member_display_name
from bot.services.group_settings import acquire_group_settings_write_intent
from bot.services.message_templates import render_action_notice, render_data_brief
from bot.services.point_shop import (
    buy_member_tag,
    buy_pin,
    play_lottery,
    render_balance,
    render_shop_menu,
    resolve_pin_target,
)
from bot.services.skills import SkillService
from bot.services.skills.platform_common import fetch_bytes
from bot.utils.command_catalog import build_help_text
from bot.utils.project_info import (
    PROJECT_DEVELOPER,
    PROJECT_DEVELOPER_CONTACT,
    PROJECT_LICENSE,
    PROJECT_NAME,
    PROJECT_REPOSITORY_URL,
)
from bot.utils.telegram import (
    answer_with_auto_delete,
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
    is_group,
    preserve_delete_button,
    typing_action,
)

router = Router()
log = logging.getLogger(__name__)
_AV_SEARCH_PAGE_SIZE = 6
_AV_SEED_PAGE_SIZE = 1
_LIST_PAGE_SIZE = 5
_AV_SESSION_STORE = AVQuerySessionStore(ttl_seconds=15 * 60, max_sessions=256)
_AV_GROUP_ENABLE_KEY = "av_enabled"

# ---------------------------------------------------------------------------
# 私聊 /av（识图反查 + 文字查询）限流
#
# 私聊以前只给最高管理员用，就是为了防止有人把机器人当作免费的外部搜索代理。
# 现在放开给「任何跟机器人私聊过的用户」，代价是每人每小时最多
# _AV_PRIVATE_HOURLY_LIMIT 次（识图 + 文字查询合计）。计数只在内存里，
# 进程重启清零——单实例部署可以接受，也避免为了限流去写数据库。
# 群里只有「/av + 图片」识图走这个桶（见 _handle_group_av_image）；群里的文字
# /av 查询保持现状、不计数（群内仍按 groups.settings.av_enabled 与授权群判断）。
# ---------------------------------------------------------------------------
_AV_PRIVATE_HOURLY_LIMIT = 10
_AV_PRIVATE_RATE_WINDOW_SECONDS = 3600.0
_AV_PRIVATE_RATE_LIMITER = AVPrivateRateLimiter(
    limit=_AV_PRIVATE_HOURLY_LIMIT,
    window_seconds=_AV_PRIVATE_RATE_WINDOW_SECONDS,
)

_AV_RATE_LIMITED_TEMPLATE = "太频繁了，请 {minutes} 分钟后再试。"
_AV_VISION_NO_CODE_TEXT = "没读出编号，请直接把番号发给我。"
_AV_VISION_NO_CONTENT_TEXT = "这张图没认出作品信息，请直接把番号发给我。"
_AV_VISION_FAILED_TEXT = "图片识别失败，请直接把番号发给我。"
_AV_IMAGE_TOO_LARGE_TEXT = "这张图太大了，请发小一点的图，或直接把番号发给我。"

#: 群里删图（识图前）的超时：删除是尽力而为，不能因为 Telegram 卡住就不识图了。
_AV_GROUP_DELETE_TIMEOUT_SEC = 5.0

#: ``/av`` / ``/av@SomeBot``；群里图片配文用它判断「这条图是被 /av 请求的」。
_AV_COMMAND_PREFIX_RE = re.compile(r"^\s*/av(?:@[\w_]+)?(?:\s|$)", re.IGNORECASE)
#: 裸命令（没有参数）：只有裸 ``/av`` 回复图片才当识图请求，``/av SONE-342`` 不当。
_AV_BARE_COMMAND_RE = re.compile(r"^\s*/av(?:@[\w_]+)?\s*$", re.IGNORECASE)


def _av_rate_limited_text(retry_after_seconds: int) -> str:
    return _AV_RATE_LIMITED_TEMPLATE.format(
        minutes=rate_limit_minutes(retry_after_seconds)
    )


def _av_command_text(message: Message) -> str:
    """``/av`` 命令可能写在 text 或图片 caption 上（aiogram 的 Command 两者都认）。"""

    return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")


def _av_vision_notice(text: str, requester_name: str = "") -> str:
    """识图相关的一句话提示；群里会带上发起者名字，私聊保持原样（多一个字节都不行）。"""

    if requester_name:
        return f"<b>AV 识图</b>\n<b>{requester_name}</b>：{text}"
    return f"<b>AV 识图</b>\n{text}"


def _av_requester_name(message: Message) -> str:
    """群里报「谁的识图结果」用；取不到名字就退化成 @用户名 / 数字 ID。"""

    user = getattr(message, "from_user", None)
    if user is None:
        return "有人"
    name = member_display_name(
        int(getattr(user, "id", 0) or 0),
        full_name=getattr(user, "full_name", "") or "",
        username=getattr(user, "username", "") or "",
    )
    return html.escape(_truncate_text(name, 40))


def _group_av_image_request(message: Message) -> tuple[Message, Message] | None:
    """群里「/av + 图片」的两种入口；不是这种请求返回 ``None``。

    返回 ``(图片消息, /av 命令消息)``——配文触发时两者是同一条消息，回复触发时
    图片是 ``reply_to_message``、命令是当前这条。

    - 配文触发：自己能发的图配 ``/av``（同一群、同一条消息），普通聊天里的图片
      根本不满足这个条件，所以不会被碰；
    - 回复触发：只认**裸** ``/av``（``/av SONE-342`` 回复图片仍然按文字查询走，
      免得误删别人刚发的图）；
    - 只认图片/图片文档，别的类型（视频、贴纸……）一律不当识图请求。
    """

    if not _AV_COMMAND_PREFIX_RE.match(_av_command_text(message)):
        return None

    if select_av_image_file(message) is not None:
        return message, message

    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    if not _AV_BARE_COMMAND_RE.match(_av_command_text(message)):
        return None
    if select_av_image_file(reply) is None:
        return None
    return reply, message


def _av_private_fetch_blocked(callback: CallbackQuery, message: Message) -> int | None:
    """私聊里「点按钮触发外部抓取」也受同一小时配额约束。

    这里**只查不计数**：翻页、看种子这类纯展示不受影响，只有真的要去源站抓详情
    （``fetch_detail``）才拦；返回 ``None`` = 放行，否则是建议等待秒数。
    群里（群授权 + 群开关另有约束）一律不拦。
    """

    if _av_message_is_group(message):
        return None
    user = callback.from_user
    blocked, retry_after = _AV_PRIVATE_RATE_LIMITER.blocked(
        int(user.id) if user is not None else 0
    )
    return retry_after if blocked else None


_LEGACY_ACTION_RESPONSE_RE = re.compile(
    r"^\s*<b>(?P<title>[^<>\n]+)</b>(?:\n+)?(?P<body>[\s\S]*?)\s*$"
)


def _build_start_text() -> str:
    """Render the deterministic public self-introduction for /start."""
    return (
        f"<b>{html.escape(PROJECT_NAME)} · 智能群管机器人</b>\n"
        "欢迎使用。\n\n"
        "<b>核心功能</b>\n"
        "永久记忆（管理员自然语言维护）\n"
        "内容审核\n"
        "智能闲聊\n\n"
        "<b>开源信息</b>\n"
        f"完全开源（{html.escape(PROJECT_LICENSE)}）\n"
        f"源码仓库：{html.escape(PROJECT_REPOSITORY_URL)}\n"
        f"开发者：<code>{html.escape(PROJECT_DEVELOPER)}</code>\n"
        f"联系方式：<code>{html.escape(PROJECT_DEVELOPER_CONTACT)}</code>\n\n"
        "<b>快速开始</b>\n"
        "发送 /help 查看完整命令。"
    )


def _render_action_response(text: str) -> str:
    """Give legacy command replies the shared action-first treatment.

    Commands still return a few short strings from service layers.  Keeping
    this adapter at the send boundary lets those replies gain the selected
    layout without duplicating parsing and escaping rules in every command.
    Fully rendered notices and data briefs already contain blockquotes and
    intentionally pass through unchanged.
    """
    rendered = str(text or "").strip()
    if not rendered or "<blockquote" in rendered.lower():
        return rendered
    match = _LEGACY_ACTION_RESPONSE_RE.match(rendered)
    if match:
        title = html.unescape(match.group("title").strip()) or "操作结果"
        return render_action_notice(title, action=match.group("body").strip())
    return render_action_notice("操作结果", action=rendered)


async def _answer(
    message: Message,
    settings: Settings,
    text: str,
    *,
    auto_delete_seconds: int | None = None,
    **kwargs: object,
) -> None:
    formatted_text = _render_action_response(text)
    await answer_with_auto_delete(
        message,
        formatted_text,
        auto_delete_seconds=(
            configured_auto_delete_seconds(settings, "management")
            if auto_delete_seconds is None
            else auto_delete_seconds
        ),
        **kwargs,
    )


async def _ensure_group_row(session: AsyncSession, group_id: int, title: str) -> Group:
    if session.in_transaction():
        await session.commit()
    await acquire_group_settings_write_intent(session, group_id)
    row = await session.get(Group, group_id)
    if row:
        if title and row.title != title:
            row.title = title
        if row.settings is None:
            row.settings = {}
        return row

    try:
        async with session.begin_nested():
            row = Group(id=group_id, title=title or "", settings={})
            session.add(row)
            await session.flush()
            return row
    except IntegrityError:
        row = await session.get(Group, group_id)
        if row:
            if title and row.title != title:
                row.title = title
            if row.settings is None:
                row.settings = {}
            return row

    row = Group(id=group_id, title=title or "", settings={})
    session.add(row)
    return row


def _is_group_av_enabled(group_settings: dict | None) -> bool:
    settings_dict = group_settings if isinstance(group_settings, dict) else {}
    value = settings_dict.get(_AV_GROUP_ENABLE_KEY)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _truncate_text(text: str, max_len: int) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _source_name(source: str) -> str:
    val = (source or "").strip().lower()
    if val == "javbus":
        return "JAVBUS"
    if val == "madouqu":
        return "MADOUQU"
    if val == "dmm":
        return "DMM"
    if val == "fc2":
        return "FC2"
    return val.upper() or "UNKNOWN"


def _parse_int(value: str, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _moderation_llm(settings: Settings):
    """LLMService for the moderation stage only.

    Every role keyword is passed on purpose: a role left out silently collapses
    onto `main` (see the platform notes), so an omitted `skill=` would make this
    probe verify the wrong route.
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


def _build_skill_service(settings: Settings) -> SkillService:
    llm = LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )
    sticker_pool = [
        x.strip()
        for x in (settings.skill_sticker_file_ids or "").split(",")
        if x and x.strip()
    ]
    return SkillService(llm, settings=settings, default_sticker_file_ids=sticker_pool)


def _build_memory_list_page(items: list[object], *, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(items)
    total_pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _LIST_PAGE_SIZE
    end = min(start + _LIST_PAGE_SIZE, total)

    if not items:
        return render_data_brief("永久记忆", empty="当前没有保存的永久记忆。"), None

    lines: list[str] = []
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(items[start:end], start=start + 1):
        memory_id = int(getattr(item, "id", 0) or 0)
        preview = html.escape(_truncate_text(str(getattr(item, "content", "") or ""), 120))
        lines.append(f"<b>{idx}.</b> <code>#{memory_id}</code>　{preview}")
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"删除记忆 #{memory_id}",
                    callback_data=f"lmd:{memory_id}:{page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="上一页", callback_data=f"lml:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="下一页", callback_data=f"lml:{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)
    return (
        render_data_brief(
            "永久记忆",
            metadata={
                "总数": f"<code>{total}</code> 条",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
            footer="使用下方按钮删除对应记录。",
        ),
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def _callback_user_can_manage_memories(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    msg = callback.message
    if not msg or not msg.chat or msg.chat.type not in ("group", "supergroup"):
        await callback.answer("消息已失效", show_alert=True)
        return False
    authorized = await is_group_authorized(session, int(msg.chat.id))
    await session.commit()
    if not authorized:
        await callback.answer("当前群组未授权", show_alert=True)
        return False

    user = callback.from_user
    if user and is_super_admin_user_id(user.id, settings):
        return True
    if not user:
        await callback.answer("无法识别操作者", show_alert=True)
        return False
    locally_authorized = await is_group_admin_authorized(
        session,
        msg.chat.id,
        user.id,
    )
    await session.commit()
    if locally_authorized:
        return True

    await callback.answer("仅群管理员可操作该列表", show_alert=True)
    return False


async def _ensure_callback_group_authorized(
    callback: CallbackQuery,
    message: Message,
    session: AsyncSession,
) -> bool:
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", "") not in {"group", "supergroup"}:
        return True
    authorized = await is_group_authorized(session, int(chat.id))
    await session.commit()
    if authorized:
        return True
    await callback.answer("当前群组未授权", show_alert=True)
    return False


def _av_session_owner_ok(session: AVQuerySession, user_id: int) -> bool:
    if session.owner_user_id <= 0:
        return True
    return session.owner_user_id == user_id


async def _ensure_av_callback_scope(
    callback: CallbackQuery,
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> bool:
    if message.chat and message.chat.type in ("group", "supergroup"):
        group_row = await _ensure_group_row(
            session,
            message.chat.id,
            message.chat.title or "",
        )
        group_av_enabled = _is_group_av_enabled(group_row.settings)
        await session.commit()
        if not group_av_enabled:
            await callback.answer("当前群组未启用 AV 查询", show_alert=True)
            return False
        return True

    await session.commit()
    user = callback.from_user
    if user is None:
        await callback.answer("无法确认操作人，请重新 /av", show_alert=True)
        return False
    # 私聊里的 /av 已经放开给普通用户（受 _AV_PRIVATE_RATE_LIMITER 限流），
    # 按钮自然也得跟着放开；会话归属由调用方的 _av_session_owner_ok 兜底。
    return True


def _build_av_search_page(
    session: AVQuerySession,
    *,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(session.results)
    total_pages = max(1, (total + _AV_SEARCH_PAGE_SIZE - 1) // _AV_SEARCH_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _AV_SEARCH_PAGE_SIZE
    end = min(start + _AV_SEARCH_PAGE_SIZE, total)

    lines: list[str] = []
    for idx, item in enumerate(session.results[start:end], start=start + 1):
        code = html.escape(item.code or "-")
        title = html.escape(_truncate_text(item.title or "-", 56))
        source = html.escape(_source_name(item.source))
        date = html.escape(item.date or "-")
        lines.append(f"<b>{idx}.</b> <code>{code}</code> · <code>{source}</code>")
        lines.append(f"{title}")
        lines.append(f"<code>{date}</code>")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(session.results[start:end], start=start):
        source_key = (item.source or "").lower()
        if source_key == "javbus":
            source_short = "J"
        elif source_key == "madouqu":
            source_short = "M"
        elif source_key == "dmm":
            source_short = "D"
        elif source_key == "fc2":
            source_short = "F"
        else:
            source_short = "?"
        label = item.code or _truncate_text(item.title, 20)
        btn_text = _truncate_text(f"{idx + 1}. [{source_short}] {label}", 60)
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"avd:{session.token}:{idx}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"avs:{session.token}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"avs:{session.token}:{page + 1}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)

    return (
        render_data_brief(
            "AV 搜索结果",
            metadata={
                "关键词": f"<code>{html.escape(_truncate_text(session.query, 80))}</code>",
                "结果": f"<code>{total}</code> 条",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
            footer="使用下方按钮查看详情。",
        ),
        InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


def _build_av_detail_caption(detail: AVDetail) -> str:
    metadata: dict[str, object] = {
        "来源": f"<code>{html.escape(_source_name(detail.source))}</code>",
        "番号": f"<code>{html.escape(detail.code or '未知')}</code>",
    }
    if detail.date:
        metadata["发行日期"] = f"<code>{html.escape(detail.date)}</code>"
    if detail.runtime:
        metadata["时长"] = html.escape(detail.runtime)
    if detail.score:
        metadata["评分"] = f"<code>{html.escape(detail.score)}</code>"
    if detail.studio:
        metadata["制作商"] = html.escape(_truncate_text(detail.studio, 60))
    if detail.publisher:
        metadata["发行商"] = html.escape(_truncate_text(detail.publisher, 60))
    if detail.series:
        metadata["系列"] = html.escape(_truncate_text(detail.series, 60))
    if detail.actors:
        metadata["演员"] = html.escape(_truncate_text(" / ".join(detail.actors), 80))
    if detail.genres:
        metadata["类型"] = html.escape(_truncate_text(" / ".join(detail.genres), 80))
    if detail.seeds:
        metadata["种子"] = f"<code>{len(detail.seeds)}</code> 条（可翻页）"
    else:
        metadata["种子"] = "无"
    if detail.url:
        metadata["详情页"] = (
            f'<a href="{html.escape(detail.url, quote=True)}">打开详情页</a>'
        )
    return render_data_brief(
        "影片详情",
        metadata=metadata,
        items=f"<b>{html.escape(_truncate_text(detail.title or '-', 70))}</b>",
    )


def _build_av_detail_text(detail: AVDetail) -> str:
    metadata: dict[str, object] = {
        "来源": f"<code>{html.escape(_source_name(detail.source))}</code>",
        "番号": f"<code>{html.escape(detail.code or '未知')}</code>",
    }

    if detail.date:
        metadata["发行日期"] = f"<code>{html.escape(detail.date)}</code>"
    if detail.runtime:
        metadata["时长"] = html.escape(detail.runtime)
    if detail.score:
        metadata["评分"] = f"<code>{html.escape(detail.score)}</code>"
    if detail.director:
        metadata["导演"] = html.escape(detail.director)
    if detail.studio:
        metadata["制作商"] = html.escape(detail.studio)
    if detail.publisher:
        metadata["发行商"] = html.escape(detail.publisher)
    if detail.series:
        metadata["系列"] = html.escape(detail.series)
    if detail.actors:
        metadata["演员"] = html.escape(" / ".join(detail.actors))
    if detail.genres:
        metadata["类型"] = html.escape(" / ".join(detail.genres))

    if detail.seeds:
        metadata["种子"] = f"<code>{len(detail.seeds)}</code> 条（点下方按钮浏览）"
    else:
        metadata["种子"] = "无"
    if detail.url:
        metadata["详情页"] = (
            f'<a href="{html.escape(detail.url, quote=True)}">打开详情页</a>'
        )
    item_lines = [f"<b>{html.escape(detail.title or '-')}</b>"]
    if detail.summary:
        item_lines.extend(
            ["", html.escape(_truncate_text(detail.summary, 360))]
        )
    return render_data_brief("影片详情", metadata=metadata, items=item_lines)


def _build_av_detail_keyboard(
    *,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if detail.seeds:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"浏览种子 ({len(detail.seeds)})",
                    callback_data=f"avm:{session.token}:{result_idx}:0",
                )
            ]
        )
    if detail.url.startswith("http"):
        rows.append([InlineKeyboardButton(text="打开详情页", url=detail.url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_av_seed_page(
    *,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
    page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    if not detail.seeds:
        return render_data_brief("种子列表", empty="当前没有可用种子。"), None

    total = len(detail.seeds)
    total_pages = max(1, (total + _AV_SEED_PAGE_SIZE - 1) // _AV_SEED_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _AV_SEED_PAGE_SIZE
    end = min(start + _AV_SEED_PAGE_SIZE, total)

    seed = detail.seeds[start] if start < len(detail.seeds) else None
    lines: list[str] = []
    if seed:
        title = html.escape(_truncate_text(seed.title or "Magnet", 120))
        size = html.escape(seed.size or "-")
        date = html.escape(seed.date or "-")
        lines.append(f"<b>{start + 1}.</b> {title}")
        lines.append(f"大小　<code>{size}</code>　日期　<code>{date}</code>")
        lines.append(f"<code>{html.escape(_truncate_text(seed.magnet or '-', 260))}</code>")
    else:
        lines.append("当前没有可用种子。")

    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"avm:{session.token}:{result_idx}:{page - 1}",
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"avm:{session.token}:{result_idx}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="返回详情",
                callback_data=f"avd:{session.token}:{result_idx}",
            )
        ]
    )

    if detail.url.startswith("http"):
        rows.append([InlineKeyboardButton(text="打开详情页", url=detail.url)])

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return (
        render_data_brief(
            "种子列表",
            metadata={
                "番号": f"<code>{html.escape(detail.code or '未知')}</code>",
                "来源": f"<code>{html.escape(_source_name(detail.source))}</code>",
                "页码": f"<code>{page + 1} / {total_pages}</code>",
            },
            items=lines,
        ),
        keyboard,
    )


async def _download_cover_input_file(cover_url: str, referer: str = "") -> BufferedInputFile | None:
    url = (cover_url or "").strip()
    if not url.startswith("http"):
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    try:
        status, raw, _final_url, ctype = await fetch_bytes(
            url,
            headers=headers,
            timeout_sec=15.0,
            allowed_content_types=("image/",),
            max_response_bytes=15 * 1024 * 1024,
        )
        if status >= 400 or not raw:
            return None
        ext = ".jpg"
        if "png" in ctype:
            ext = ".png"
        elif "webp" in ctype:
            ext = ".webp"
        return BufferedInputFile(raw, filename=f"av_cover{ext}")
    except Exception:
        log.exception("failed to download cover: %s", url)
        return None


async def _edit_message_as_photo(
    *,
    message: Message,
    cover_url: str,
    caption: str,
    keyboard: InlineKeyboardMarkup | None,
    referer: str = "",
) -> bool:
    if not cover_url:
        return False

    try:
        media = InputMediaPhoto(media=cover_url, caption=caption, parse_mode="HTML")
        await message.edit_media(media=media, reply_markup=keyboard)
        return True
    except TelegramBadRequest as exc:
        # Some sources block Telegram fetch; retry by uploading bytes.
        log.warning("edit_media by url failed: %s", exc)
    except Exception:
        log.exception("edit_media by url failed")

    file_obj = await _download_cover_input_file(cover_url, referer=referer)
    if not file_obj:
        return False

    try:
        media = InputMediaPhoto(media=file_obj, caption=caption, parse_mode="HTML")
        await message.edit_media(media=media, reply_markup=keyboard)
        return True
    except Exception:
        log.exception("edit_media by uploaded file failed")
        return False


async def _edit_av_detail_in_place(
    *,
    message: Message,
    detail: AVDetail,
    keyboard: InlineKeyboardMarkup | None,
    allow_media: bool = True,
    text: str | None = None,
) -> bool:
    caption = _build_av_detail_caption(detail)
    detail_text = text if text is not None else _build_av_detail_text(detail)

    # 群内（allow_media=False）只允许文字：绝不把封面编辑回群里。
    if not allow_media:
        try:
            await message.edit_text(
                detail_text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return True
        except Exception:
            log.exception("failed to edit message as group text detail")
            return False

    # If current message is photo, edit caption first to keep same media message.
    if getattr(message, "photo", None):
        try:
            await message.edit_caption(caption=caption, reply_markup=keyboard)
            return True
        except Exception:
            log.debug("edit_caption for detail failed, will try media refresh")

    # Try switching message to photo+caption (URL first, then uploaded bytes).
    media_ok = await _edit_message_as_photo(
        message=message,
        cover_url=detail.cover_url,
        caption=caption,
        keyboard=keyboard,
        referer=detail.url,
    )
    if media_ok:
        return True

    # Photo message cannot be edited via edit_text.
    if getattr(message, "photo", None):
        return False

    # Fallback to text edit for non-media messages.
    try:
        await message.edit_text(detail_text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("failed to edit message as text detail")
        return False


async def _edit_av_seed_in_place(
    *,
    message: Message,
    detail: AVDetail,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> bool:
    # Keep seed browsing in the same message.
    # 群内不出现任何照片：历史的图片消息也一律改回文字（不新增私聊分支）。
    if not _av_message_is_group(message) and getattr(message, "photo", None):
        try:
            await message.edit_caption(caption=text, reply_markup=keyboard)
            return True
        except Exception:
            log.debug("edit_caption for seed failed, will try media refresh")
        media_ok = await _edit_message_as_photo(
            message=message,
            cover_url=detail.cover_url,
            caption=text,
            keyboard=keyboard,
            referer=detail.url,
        )
        if media_ok:
            return True
        return False

    try:
        await message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("failed to edit seed page in place")
        return False


# ---------------------------------------------------------------------------
# /av 封面走私聊：群里只留文字
#
# 为什么：群是社区群，封面属于 NSFW，不能在群里出现；标题/番号/详情这类文字没问题，
# 所以群内路径照旧发文字 + 按钮，封面单独发给发起者的私聊。Telegram 不允许机器人
# 主动私聊没跟它说过话的人，那种情况下群里换成 t.me/<bot>?start=av 深链按钮，
# 由用户自己点开私聊按「开始」后再回群里重新查询 —— 绝不把图发回群里兜底。
# ---------------------------------------------------------------------------

#: 私聊封面发送结果（只用来决定群里那一行提示怎么写）。
_AV_COVER_NO_COVER = "no_cover"
_AV_COVER_SENT = "sent"
_AV_COVER_PRIVATE_BLOCKED = "private_blocked"
_AV_COVER_FAILED = "failed"

_AV_COVER_SENT_HINT = "封面已私聊发给你。"
_AV_COVER_PRIVATE_BLOCKED_HINT = (
    "封面需要先打开与机器人的私聊并按「开始」，再回群里重新查询；"
    "文本结果照旧留在群里。"
)
_AV_COVER_FAILED_HINT = "封面这次没能发出去；文本结果照旧留在群里。"

#: 「打开私聊」深链按钮的文案。
_AV_PRIVATE_OPEN_BUTTON_TEXT = "打开私聊"


def _av_message_is_group(message: Message) -> bool:
    """这条消息是不是群消息（群内路径一律不许出现照片）。"""

    chat = getattr(message, "chat", None)
    return bool(chat is not None and getattr(chat, "type", "") in ("group", "supergroup"))


def _build_av_cover_caption(detail: AVDetail) -> str:
    """私聊封面的说明：只要番号/作品名，不带按钮也不带分页。"""

    code = (detail.code or "").strip()
    title = _truncate_text((detail.title or "").strip(), 120)
    if code and title:
        return f"{code} · {title}"
    return code or title or "AV 封面"


async def _resolve_bot_username(bot: object) -> str:
    """运行时取 bot 用户名（私聊深链用）。取不到就返回空串，调用方退化成纯文字提示。"""

    try:
        me = await bot.get_me()
    except Exception:
        log.exception("failed to resolve bot username for the AV private-chat deep link")
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


def _av_detail_keyboard_with_private_link(
    keyboard: InlineKeyboardMarkup | None,
    *,
    bot_username: str,
) -> InlineKeyboardMarkup | None:
    """在详情键盘末尾补一行「打开私聊」URL 按钮；取不到用户名时保持原样。"""

    username = (bot_username or "").strip().lstrip("@")
    if not username:
        return keyboard
    rows = list(keyboard.inline_keyboard) if keyboard is not None else []
    rows.append(
        [
            InlineKeyboardButton(
                text=_AV_PRIVATE_OPEN_BUTTON_TEXT,
                url=f"https://t.me/{username}?start=av",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_av_cover_to_sender_dm(
    *,
    bot: object,
    sender_user_id: int,
    detail: AVDetail,
) -> str:
    """把封面私聊发给发起者，返回 ``_AV_COVER_*`` 结果码。

    顺序沿用原来的「先试 URL、失败再上传字节」；任何失败都只记日志，
    绝不把图发回群里兜底。
    """

    cover_url = (detail.cover_url or "").strip()
    if not cover_url or sender_user_id <= 0:
        return _AV_COVER_NO_COVER

    caption = _build_av_cover_caption(detail)
    try:
        await bot.send_photo(chat_id=sender_user_id, photo=cover_url, caption=caption)
        return _AV_COVER_SENT
    except TelegramForbiddenError:
        # 对方从没跟 bot 说过话 / 拉黑了 bot：只能请他自己点开私聊。
        log.info("av cover dm blocked | user=%s", sender_user_id)
        return _AV_COVER_PRIVATE_BLOCKED
    except TelegramBadRequest as exc:
        # 有些来源的图片 Telegram 拉不到，退回上传字节。
        log.warning("av cover dm by url failed: %s", exc)
    except Exception:
        log.exception("failed to send av cover to dm | user=%s", sender_user_id)

    file_obj = await _download_cover_input_file(cover_url, referer=detail.url)
    if file_obj is not None:
        try:
            await bot.send_photo(chat_id=sender_user_id, photo=file_obj, caption=caption)
            return _AV_COVER_SENT
        except TelegramForbiddenError:
            log.info("av cover dm blocked on upload | user=%s", sender_user_id)
            return _AV_COVER_PRIVATE_BLOCKED
        except Exception:
            log.exception("failed to send av cover to dm by upload | user=%s", sender_user_id)

    return _AV_COVER_FAILED


async def _send_av_detail_in_group(
    *,
    message: Message,
    session: AVQuerySession,
    detail: AVDetail,
    detail_text: str,
    keyboard: InlineKeyboardMarkup | None,
    in_place: bool,
) -> bool:
    """群内详情：只发文字 + 按钮，封面走私聊，提示并进详情文本末尾。"""

    sender_user_id = int(session.owner_user_id or 0)
    if sender_user_id <= 0 and message.from_user is not None:
        sender_user_id = int(message.from_user.id)

    outcome = await _send_av_cover_to_sender_dm(
        bot=message.bot,
        sender_user_id=sender_user_id,
        detail=detail,
    )

    reply_markup = keyboard
    text = detail_text
    if outcome == _AV_COVER_SENT:
        text = f"{detail_text}\n\n<i>{_AV_COVER_SENT_HINT}</i>"
    elif outcome == _AV_COVER_PRIVATE_BLOCKED:
        text = f"{detail_text}\n\n<i>{_AV_COVER_PRIVATE_BLOCKED_HINT}</i>"
        reply_markup = _av_detail_keyboard_with_private_link(
            keyboard,
            bot_username=await _resolve_bot_username(message.bot),
        )
    elif outcome == _AV_COVER_FAILED:
        text = f"{detail_text}\n\n<i>{_AV_COVER_FAILED_HINT}</i>"

    if in_place:
        return await _edit_av_detail_in_place(
            message=message,
            detail=detail,
            keyboard=reply_markup,
            allow_media=False,
            text=text,
        )

    try:
        await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("failed to send av detail text in group")
        return False


async def _send_av_detail(
    *,
    message: Message,
    session: AVQuerySession,
    result_idx: int,
    detail: AVDetail,
    in_place: bool = False,
    header: str = "",
) -> bool:
    caption = _build_av_detail_caption(detail)
    detail_text = _build_av_detail_text(detail)
    if header:
        # 群里那条 /av 命令（和图片）已经删了，结果是一条独立消息，用 header 说明
        # 是谁问的——绝不 reply 到已删消息上（否则会显示「回复的内容已删除」）。
        detail_text = f"{header}\n{detail_text}"
    keyboard = _build_av_detail_keyboard(session=session, result_idx=result_idx, detail=detail)

    # 群内路径：文字与按钮照旧留群里，封面只走私聊（group path never sends media）。
    if _av_message_is_group(message):
        return await _send_av_detail_in_group(
            message=message,
            session=session,
            detail=detail,
            detail_text=detail_text,
            keyboard=keyboard,
            in_place=in_place,
        )

    if in_place:
        return await _edit_av_detail_in_place(message=message, detail=detail, keyboard=keyboard)

    # 以下带图路径只剩私聊（最高管理员的诊断入口）：私聊 chat 就是发起者本人。
    sent_photo: Message | None = None
    if detail.cover_url:
        try:
            sent_photo = await message.answer_photo(
                photo=detail.cover_url,
                caption=caption,
                reply_markup=keyboard,
            )
            return True
        except TelegramBadRequest as exc:
            # Some source URLs are blocked for Telegram fetch or return non-image content.
            log.warning("send_photo by url failed: %s", exc)
        except Exception:
            log.exception("failed to send av cover photo: %s", detail.cover_url)

    if detail.cover_url and not sent_photo:
        file_obj = await _download_cover_input_file(detail.cover_url, referer=detail.url)
        if file_obj:
            try:
                sent_photo = await message.answer_photo(
                    photo=file_obj,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return True
            except Exception:
                log.exception("failed to send av cover photo by upload")

    if not sent_photo:
        sent = await message.answer(
            detail_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    return False



# ---------------------------------------------------------------------------
# 私聊商店入口（签到提醒上的「🛒 积分商店」深链）
#
# 为什么走私聊：菜单发在群里会把群刷乱（老板反馈）。Telegram 又不允许机器人主动
# 私聊没跟它说过话的人，所以群里只放一个 URL 深链按钮（``t.me/<bot>?start=shop_<群>``），
# 点一下等于那个人自己给机器人发了 /start —— 这是唯一对所有人都可用的做法。
#
# payload 用独立的 ``shop_`` 前缀（见 checkin_reminder.parse_shop_start_payload），
# 与入群验证的 ``verify…`` 前缀不重叠：``/start verify…`` 的既有行为一个字都不动。
# ---------------------------------------------------------------------------

#: 私聊菜单末尾的说明：这三个动作需要群上下文，仍然在群里用。
_PRIVATE_SHOP_FOOTER = (
    "\n\n"
    "<i>头衔 /tag、置顶 /top、抽奖 /draw 仍然在群里发：这些操作需要群上下文，"
    "它们在群里的回执本来就会自动删除，不会占屏。</i>"
)

_PRIVATE_SHOP_UNAVAILABLE_TEXT = "积分商店暂时不可用，请稍后再试（这次没有扣分）。"


async def _user_is_group_member(
    session: AsyncSession, *, group_id: int, user_id: int
) -> bool:
    """成员表里有没有这个人的**在群**记录（``left`` 的不算）。

    Bot API 列不出群成员，所以成员名单就是 ``group_members`` 这张现成的表。
    """

    row = (
        await session.execute(
            select(GroupMember.id)
            .where(
                GroupMember.group_id == int(group_id),
                GroupMember.user_id == int(user_id),
                GroupMember.left.is_not(True),
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _handle_private_shop_start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    payload: str,
) -> bool:
    """私聊 ``/start shop_<群号>``：在**私聊**里回商店菜单。

    - 返回 True = 这个 payload 是商店的（已经给出答复，调用方不要再走欢迎文案）；
      返回 False = 不是商店 payload（调用方继续走原来的入群验证 / 欢迎文案）。
      所以这个分支不可能截胡 ``/start verify…``；
    - 菜单正文与群里的 ``/shop`` 同源（``render_shop_menu``），可用积分取
      「这个人**在那个群**的可用积分」（只读，不扣分；积分不够也能看）；
    - payload 解析失败、群没授权、查不到这个人在群里：都只在私聊友好提示，
      **不发菜单、不扣分**；
    - 任何异常只记日志 + 提示失败：``/start`` 是入口，绝不能抛出去。
    """

    group_id = parse_shop_start_payload(payload)
    if group_id is None:
        return False
    user = getattr(message, "from_user", None)
    if user is None or bool(getattr(user, "is_bot", False)):
        return True
    try:
        if not await is_group_authorized(session, group_id):
            await _answer(
                message,
                settings,
                "这个群还没有授权使用积分商店，请联系群管理员。",
            )
            return True
        if not await _user_is_group_member(
            session, group_id=group_id, user_id=int(user.id)
        ):
            await _answer(
                message,
                settings,
                "没有查到你是这个群的成员：请先在群里说句话，再点一次商店按钮。",
            )
            return True
        outcome = await summarize(
            session, group_id=group_id, user_id=int(user.id)
        )
        await session.commit()
        await _answer(
            message,
            settings,
            render_shop_menu(available=outcome.available_points)
            + _PRIVATE_SHOP_FOOTER,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "[%s] private shop menu failed | user=%s", group_id, user.id
        )
        await _answer(message, settings, _PRIVATE_SHOP_UNAVAILABLE_TEXT)
    return True


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    # New members arrive here via the group prompt's deep link; hand out the
    # exact group's one-time challenge instead of the generic welcome.
    if message.chat and message.chat.type == "private":
        command_parts = str(message.text or "").split(maxsplit=1)
        payload = command_parts[1] if len(command_parts) == 2 else ""
        # 商店深链先认（shop_ 前缀与 verify… 不重叠，认不出来就返回 False 继续往下）
        if await _handle_private_shop_start(message, session, settings, payload):
            return
        if await maybe_send_private_verification(
            message,
            session,
            settings,
            group_id=parse_private_verify_group_id(payload),
        ):
            return
    await _answer(message, settings, _build_start_text())


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    await _answer(message, settings, build_help_text())


_VOTEBAN_USAGE = (
    "<b>命令用法</b>\n"
    "回复目标用户的消息后发送 /voteban [举报理由]\n\n"
    "对被回复用户发起民主投票封禁；达到本群设定票数后立即封禁。"
)


@router.message(Command("voteban"))
async def cmd_voteban(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: object | None = None,
) -> None:
    """Open a vote through the same quota-enforcing service used by the AI skill."""
    from bot.services.vote_ban import start_vote_ban

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    if getattr(message, "reply_to_message", None) is None:
        await _answer(message, settings, _VOTEBAN_USAGE)
        return
    reason = str(message.text or "").partition(" ")[2].strip()
    result = await start_vote_ban(
        message,
        session,
        settings,
        reason_override=reason,
        trigger_source="command",
        session_factory=session_factory,
    )
    if not result.ok:
        await _answer(message, settings, result.telegram_text)


@router.message(Command("settings"))
async def cmd_settings(
    message: Message,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    user_id = int(getattr(message.from_user, "id", 0) or 0)
    allowed = bool(user_id and is_super_admin_user_id(user_id, settings))
    if not allowed and user_id and session is not None:
        allowed = bool(await session.scalar(
            select(Admin.id)
            .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
            .where(
                Admin.user_id == user_id,
                AuthorizedGroup.bot_present.is_(True),
            )
            .limit(1)
        ))
    if not allowed:
        await _answer(message, settings, "你没有可管理的已授权群组。")
        return
    if not message.chat or message.chat.type != "private":
        await _answer(
            message,
            settings,
            "<b>设置中心</b>\n请私聊机器人后使用 /settings。",
            retry_tls_record_error=True,
        )
        return
    base_url = settings.miniapp_public_base_url.strip().rstrip("/")
    if not base_url:
        await _answer(
            message,
            settings,
            "<b>设置中心不可用</b>\n请先配置 MINIAPP_PUBLIC_BASE_URL 并重启。",
            auto_delete_seconds=0,
            retry_tls_record_error=True,
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="打开设置中心",
                    web_app=WebAppInfo(url=f"{base_url}/settings"),
                )
            ]
        ]
    )
    await _answer(
        message,
        settings,
        "<b>Bot 设置中心</b>",
        auto_delete_seconds=0,
        reply_markup=keyboard,
        retry_tls_record_error=True,
    )


@router.message(Command("lm"))
async def cmd_lm(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    # Authorization queries have completed. Release their connection before
    # any memory lookup, LLM intent parsing, typing heartbeat or Telegram send.
    await session.commit()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>永久记忆</b>\n请在群内使用 /lm。", auto_delete_seconds=0)
        return

    args = (message.text or "").partition(" ")[2].strip()
    memory = memory_holder.get()
    if not args or args.lower() in {"list", "ls"}:
        items = await memory.list_permanent_memories(message.chat.id, limit=200)
        text, keyboard = _build_memory_list_page(items, page=0)
        await _answer(
            message,
            settings,
            text,
            auto_delete_seconds=0,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    user_id = int(getattr(message.from_user, "id", 0) or 0)
    sender_username = (getattr(message.from_user, "username", "") or "").strip()
    sender_is_owner = bool(user_id and is_super_admin_user_id(user_id, settings))
    sender_is_tg_admin = True
    skill = _build_skill_service(settings)

    request_text = ""
    normalized = args.lower()
    if normalized.startswith("add "):
        content = args[4:].strip()
        if not content:
            await _answer(
                message,
                settings,
                "<b>/lm 用法</b>\n"
                "/lm：查看永久记忆\n"
                "/lm add &lt;内容&gt;\n"
                "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;",
            )
            return
        request_text = f"添加一条永久记忆：{content}"
    elif normalized.startswith("replace "):
        payload = args[8:].strip()
        parts = re.split(r"\s*(?:=>|->|→)\s*", payload, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            await _answer(
                message,
                settings,
                "<b>/lm replace 用法</b>\n"
                "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;",
            )
            return
        request_text = f"把永久记忆 {parts[0].strip()} 改成 {parts[1].strip()}"
    else:
        await _answer(
            message,
            settings,
            "<b>/lm 用法</b>\n"
            "/lm：查看永久记忆\n"
            "/lm add &lt;内容&gt;\n"
            "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;\n\n"
            "删除请直接使用 /lm 列表里的按钮。",
        )
        return

    async with typing_action(message, enabled=settings.bot.enable_typing):
        result = await skill.run_skill(
            "memory_manage",
            {"request_text": request_text},
            session=None,
            session_factory=session_factory,
            sender_user_id=user_id,
            sender_username=sender_username,
            sender_is_owner=sender_is_owner,
            sender_is_tg_admin=sender_is_tg_admin,
            message=message,
            chat_id=message.chat.id,
            current_user_text=request_text,
        )
    await _answer(message, settings, result.summary)


@router.message(Command("compact"))
async def cmd_compact(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    # Authorization queries have completed. Release their connection before
    # the compression LLM call, typing heartbeat or Telegram send.
    await session.commit()
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await _answer(message, settings, "<b>上下文压缩</b>\n请在目标群内使用 /compact。")
        return

    memory = memory_holder.get()
    if not bool(getattr(memory, "automatic_compaction_enabled", True)):
        await _answer(
            message,
            settings,
            "<b>原文记忆模式已启用</b>\n"
            "当前群使用最近消息窗口 + 原始档案按需召回，不再用摘要替代旧消息，因此无需执行 /compact。",
        )
        return
    async with typing_action(message, enabled=settings.bot.enable_typing):
        result = await memory.compact_now(message.chat.id)

    status = str(result.get("status", ""))
    if status == "ok":
        await _answer(
            message,
            settings,
            "<b>上下文压缩完成</b>\n"
            f"已把 {int(result.get('compacted_messages', 0))} 条临时对话历史压缩进背景摘要。",
        )
    elif status == "empty":
        await _answer(message, settings, "<b>上下文压缩</b>\n当前群没有可压缩的临时对话历史。")
    elif status == "db_locked":
        await _answer(message, settings, "<b>上下文压缩失败</b>\n数据库暂时繁忙，历史已保留，请稍后重试。")
    else:
        await _answer(message, settings, "<b>上下文压缩失败</b>\n压缩模型未返回摘要，历史已保留，请稍后重试。")


@router.callback_query(F.data.startswith("lml:"))
async def on_memory_list_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /lm", show_alert=True)
        return
    if not await _callback_user_can_manage_memories(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    page = _parse_int(callback.data.split(":")[1], default=0)
    items = await memory_holder.get().list_permanent_memories(msg.chat.id, limit=200)
    text, keyboard = _build_memory_list_page(items, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.answer("列表刷新失败，请重试 /lm", show_alert=True)
            return
    except Exception:
        await callback.answer("列表刷新失败，请重试 /lm", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("lmd:"))
async def on_memory_delete(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /lm", show_alert=True)
        return
    if not await _callback_user_can_manage_memories(callback, session, settings):
        return

    msg = callback.message
    if not msg or not msg.chat:
        await callback.answer("消息已失效", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return
    memory_id = _parse_int(parts[1], default=0)
    page_hint = _parse_int(parts[2], default=0)
    if memory_id <= 0:
        await callback.answer("参数错误", show_alert=True)
        return

    deleted = await memory_holder.get().delete_permanent_memory(msg.chat.id, f"#{memory_id}")
    if not deleted:
        await callback.answer("记忆不存在或已删除", show_alert=True)
        return

    items = await memory_holder.get().list_permanent_memories(msg.chat.id, limit=200)
    if not items:
        await msg.edit_text(
            render_data_brief("永久记忆", empty="当前没有保存的永久记忆。"),
            reply_markup=None,
        )
        await callback.answer(f"已删除记忆 #{memory_id}")
        return

    total_pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = min(max(page_hint, 0), total_pages - 1)
    text, keyboard = _build_memory_list_page(items, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except Exception:
        await callback.answer("删除成功，但列表刷新失败，请重试 /lm", show_alert=True)
        return
    await callback.answer(f"已删除记忆 #{memory_id}")


async def _send_av_query_result(
    message: Message,
    settings: Settings,
    *,
    query: str,
    header: str = "",
) -> None:
    """跑一次「编号直查 / 演员名或关键词搜索」，结果照旧走现有按钮。

    私聊识图与群内识图共用它；``header`` 只在群内识图时非空（那条命令和图片已经
    删了，结果必须是一条独立消息并写明是谁问的）。分页/详情按钮就是 ``cmd_av``
    那一套（``avs:``/``avd:``），点选行为与文字查询完全一致；封面是否私聊发送由
    :func:`_send_av_detail` 按 chat 类型自己判断，这里不掺和。
    """

    svc = AVSearchService(settings)
    if not svc.enabled:
        await _answer(
            message,
            settings,
            "<b>AV 查询</b>\n当前已禁用。",
            auto_delete_seconds=0,
        )
        return

    owner_user_id = int(message.from_user.id) if message.from_user else 0
    cleaned = _truncate_text(query, 120)
    if not cleaned:
        return

    if is_av_code_query(cleaned):
        detail = await svc.lookup_by_code(cleaned)
        if detail:
            item = AVSearchItem(
                source=detail.source,
                title=detail.title,
                code=detail.code,
                url=detail.url,
                cover_url=detail.cover_url,
                date=detail.date,
                summary=detail.summary,
            )
            av_session = _AV_SESSION_STORE.create(
                owner_user_id=owner_user_id,
                query=cleaned,
                results=[item],
            )
            av_session.details[0] = detail
            sent_ok = await _send_av_detail(
                message=message,
                session=av_session,
                result_idx=0,
                detail=detail,
                header=header,
            )
            if not sent_ok:
                await _answer(
                    message,
                    settings,
                    "<b>AV 查询</b>\n详情发送失败，请稍后重试。",
                    auto_delete_seconds=0,
                )
            return

    results = await svc.search(cleaned)
    if not results:
        empty_brief = render_data_brief(
            "AV 搜索结果",
            metadata={"关键词": f"<code>{html.escape(cleaned)}</code>"},
            empty="未找到匹配内容。",
        )
        await _answer(
            message,
            settings,
            f"{header}\n{empty_brief}" if header else empty_brief,
            auto_delete_seconds=0,
        )
        return

    av_session = _AV_SESSION_STORE.create(
        owner_user_id=owner_user_id,
        query=cleaned,
        results=results,
    )
    text, keyboard = _build_av_search_page(av_session, page=0)
    if header:
        text = f"{header}\n{text}"
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


def _av_llm(settings: Settings) -> LLMService:
    """构造一次性的 LLMService；识图只碰 ``vision`` 角色（vision_describe）。"""

    return LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
    )


async def _av_vision_text(data_uri: str, settings: Settings, *, user_id: int) -> str:
    """一次识图 = 一次 ``vision_describe``；超时/失败只记日志并返回空串，绝不抛。

    用硬超时（``run_with_hard_deadline``）：视觉链路卡住时也必须到点返回，把
    「请直接发番号」的降级话术说出去，而不是把处理器挂在那里。
    """

    try:
        llm = _av_llm(settings)
        text = await run_with_hard_deadline(
            llm.vision_describe(data_uri, AV_VISION_PROMPT),
            timeout_seconds=AV_VISION_TIMEOUT_SEC,
        )
    except TimeoutError:
        log.warning("【AV 识图】识别超时 | user=%s", user_id)
        return ""
    except Exception as exc:
        log.warning("【AV 识图】识别失败 | user=%s | error=%s", user_id, exc)
        return ""
    return str(text or "").strip()


async def _dispatch_av_vision_result(
    message: Message,
    settings: Settings,
    vision_text: str,
    *,
    header: str = "",
) -> None:
    """识图结果 → 编号直查 / 演员名搜索 / 明说没读出编号（私聊与群内共用）。"""

    if AV_NO_IMAGE_MARKER in vision_text:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_VISION_NO_CONTENT_TEXT, header),
            auto_delete_seconds=0,
        )
        return

    code = extract_av_code(vision_text)
    if code:
        log.info("【AV 识图】命中编号 | code=%s", code)
        async with typing_action(message, enabled=settings.bot.enable_typing):
            await _send_av_query_result(
                message, settings, query=code, header=header
            )
        return

    actor = extract_av_actor(vision_text)
    if actor:
        log.info("【AV 识图】只读到演员名 | actor=%s", actor)
        async with typing_action(message, enabled=settings.bot.enable_typing):
            await _send_av_query_result(
                message, settings, query=actor, header=header
            )
        return

    await _answer(
        message,
        settings,
        _av_vision_notice(_AV_VISION_NO_CODE_TEXT, header),
        auto_delete_seconds=0,
    )


async def _delete_av_group_message(target: Message, *, label: str) -> None:
    """尽力删掉群里那条消息；删失败（权限不足 / 超 48 小时 / 已删）只记日志。

    这是本功能的核心顺序要求：**先删图，再识图**。所以这里绝不允许把异常抛出去，
    否则一次删除失败就会让 NSFW 图留在群里、连识别也不做了。
    """

    try:
        await run_with_hard_deadline(
            target.delete(),
            timeout_seconds=_AV_GROUP_DELETE_TIMEOUT_SEC,
        )
    except Exception as exc:
        log.warning(
            "【群内识图】删除%s消息失败，继续识图 | message_id=%s | error=%s",
            label,
            getattr(target, "message_id", 0),
            exc,
        )
        return
    log.info(
        "【群内识图】已删除%s消息 | message_id=%s",
        label,
        getattr(target, "message_id", 0),
    )


async def _delete_av_group_images(
    *,
    image_message: Message,
    command_message: Message,
) -> None:
    """删图片消息，并在 ``/av`` 命令行不是同一条时一并删掉；两条互相独立。"""

    await _delete_av_group_message(image_message, label="图片")
    if command_message is image_message:
        return
    image_id = int(getattr(image_message, "message_id", 0) or 0)
    command_id = int(getattr(command_message, "message_id", 0) or 0)
    if image_id and image_id == command_id:
        return
    await _delete_av_group_message(command_message, label="/av 命令")


async def _handle_group_av_image(
    message: Message,
    *,
    settings: Settings,
    image_message: Message,
    command_message: Message,
) -> None:
    """群里「/av + 图片」→ **先删图，再识图**，结果只发文字（封面仍旧只走私聊）。

    顺序是硬要求：第一步就把图片消息（以及不是同一条的 ``/av`` 命令行）删掉，
    之后才去做视觉识别与查询——这样即使识别失败、超时、查不到，图也已经不在群里。
    删除失败不影响后面的流程（只记日志）。

    调用方（``cmd_av``）已经做完了授权 / 群开关判断并 commit 过 DB，所以这里不再
    碰 session——删图与识图之间不持有 SQLite 写锁。
    """

    user = message.from_user
    if user is None:
        return

    # 全局开关关掉时不动用户的图：既然不会识图，就不该删。
    svc = AVSearchService(settings)
    if not svc.enabled:
        await _answer(
            message,
            settings,
            "<b>AV 查询</b>\n当前已禁用。",
            auto_delete_seconds=0,
        )
        return

    requester_name = _av_requester_name(message)
    # 结果正文的抬头：命令与图片都删了，得说明这条独立消息是谁问的。
    result_header = f"<b>{requester_name}</b> 的识图结果"

    # ① 先删图（连带 /av 命令行）。删除失败只记日志，继续识图。
    await _delete_av_group_images(
        image_message=image_message,
        command_message=command_message,
    )

    # ② 限流：与私聊查询共用一个桶（每人每小时 10 次）。超限不调模型、不搜索。
    allowed, retry_after = _AV_PRIVATE_RATE_LIMITER.allow(int(user.id))
    if not allowed:
        await _answer(
            message,
            settings,
            _av_vision_notice(_av_rate_limited_text(retry_after), requester_name),
            auto_delete_seconds=0,
        )
        return

    # ③ 再识图：下载 → 一次 vision_describe（硬超时）→ 抽编号/演员名 → 查详情。
    data_uri = await build_av_image_data_uri(image_message)
    if not data_uri:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_VISION_FAILED_TEXT, requester_name),
            auto_delete_seconds=0,
        )
        return

    async with typing_action(message, enabled=settings.bot.enable_typing):
        vision_text = await _av_vision_text(data_uri, settings, user_id=int(user.id))

    if not vision_text:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_VISION_FAILED_TEXT, requester_name),
            auto_delete_seconds=0,
        )
        return

    await _dispatch_av_vision_result(
        message, settings, vision_text, header=result_header
    )


async def _handle_private_av_image(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """私聊图片 → 识图反查番号（一次识图 = 一次 vision 调用，没有其它模型调用）。

    流程：挑一档图片 → 限流 → 下载成 data URI → ``vision_describe``（20s 硬超时）
    → 抽编号 → 命中就直查详情，只读到演员名就给出候选列表，都没有就直说。
    任何失败都只记日志 + 回一句「请直接发番号」，绝不抛到上层。
    """

    # Telegram I/O 与模型调用之前先放掉 SQLite 写锁。
    await session.commit()

    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", "") != ChatType.PRIVATE:
        # 群里绝不走这条路径（群内行为一个字都不动）。
        return

    user = message.from_user
    info = select_av_image_file(message)
    if user is None or info is None:
        return

    _file_id, _mime, declared_size = info
    if declared_size > AV_VISION_MAX_IMAGE_BYTES:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_IMAGE_TOO_LARGE_TEXT),
            auto_delete_seconds=0,
        )
        return

    allowed, retry_after = _AV_PRIVATE_RATE_LIMITER.allow(int(user.id))
    if not allowed:
        await _answer(
            message,
            settings,
            _av_vision_notice(_av_rate_limited_text(retry_after)),
            auto_delete_seconds=0,
        )
        return

    data_uri = await build_av_image_data_uri(message)
    if not data_uri:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_VISION_FAILED_TEXT),
            auto_delete_seconds=0,
        )
        return

    async with typing_action(message, enabled=settings.bot.enable_typing):
        vision_text = await _av_vision_text(data_uri, settings, user_id=int(user.id))

    if not vision_text:
        await _answer(
            message,
            settings,
            _av_vision_notice(_AV_VISION_FAILED_TEXT),
            auto_delete_seconds=0,
        )
        return

    await _dispatch_av_vision_result(message, settings, vision_text)


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.photo | F.document.mime_type.startswith("image/"),
)
async def on_private_av_image(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """私聊发图片（配 ``/av`` 或不配）→ 识图反查番号。

    注册在 ``cmd_av`` **前面**：带 ``/av`` 说明文字的图片也走识图，而不是落到
    文字用法分支。过滤器只匹配私聊照片与 ``image/*`` 文档，所以群里的照片、
    私聊里的 PDF 都不会被这个处理器吞掉（``select_av_image_file`` 再兜一层底）。
    """

    await _handle_private_av_image(message, session, settings)


@router.message(Command("av"))
async def cmd_av(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await ensure_group_authorized(message, session, settings):
        return

    is_group_chat = bool(
        message.chat and message.chat.type in ("group", "supergroup")
    )
    if not is_group_chat:
        # 私聊没有「群授权 / 群开关」的概念。以前这里只放最高管理员，是为了
        # 避免变成免费的外部搜索代理；现在放开给任何跟机器人私聊过的用户，
        # 代价是每人每小时 _AV_PRIVATE_HOURLY_LIMIT 次（见下方限流）。
        await session.commit()
        if message.from_user is None:
            return

    # 群里「/av + 图片」的两种入口都在这里识别；不是这种请求就是 None（普通聊天里的
    # 图片不会被碰）。配文触发时命令与图片是同一条消息，回复触发时是两条。
    group_image_request = (
        _group_av_image_request(message) if is_group_chat else None
    )

    # /av 命令也可能写在图片 caption 上（aiogram 的 Command 过滤器认 text 也认 caption）。
    args = _av_command_text(message).partition(" ")[2].strip()
    if not args and group_image_request is None:
        await session.commit()
        usage_lines = (
            "<b>AV 查询用法</b>\n"
            "1. /av WANZ-530（按番号直查并展示详情+种子）\n"
            "2. /av 推川悠里（按演员名查询并弹出可选列表）\n"
            "3. /av 人妻 NTR（按关键词查询并弹出可选列表）\n\n"
            "支持来源：JAVBUS / MADOUQU / DMM / FC2\n"
            "支持 FC2 编号：/av FC2-PPV-4863846\n\n"
            "默认状态：<b>关闭</b>（每个群独立）\n"
            "需最高管理员在目标群发送 /av enable 后可使用\n\n"
        )
        if is_group_chat:
            # 群里的用法文案保持原样，一个字都不改。
            usage_lines += "私聊仅最高管理员可查询。\n\n"
        else:
            usage_lines += (
                "私聊可以直接发图片（配 /av 或不配）识图反查番号；"
                "文字查询与识图合计每人每小时最多 "
                f"{_AV_PRIVATE_HOURLY_LIMIT} 次。\n\n"
            )
        usage_lines += (
            "<b>最高管理员命令（群内）</b>\n"
            "4. /av enable（启用本群 AV 查询）\n"
            "5. /av disable（停用本群 AV 查询）"
        )
        await _answer(message, settings, usage_lines)
        return

    args_norm = args.strip().lower()
    if args_norm in {"enable", "disable"}:
        if not message.chat or message.chat.type not in ("group", "supergroup"):
            await _answer(
                message,
                settings,
                "<b>AV 开关</b>\n请在目标群内发送：/av enable 或 /av disable",
                auto_delete_seconds=0,
            )
            return
        if not await ensure_super_admin(message, settings):
            return

        group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
        group_settings = dict(group_row.settings or {})
        target_enabled = args_norm == "enable"
        previous = _is_group_av_enabled(group_settings)
        group_settings[_AV_GROUP_ENABLE_KEY] = target_enabled
        group_row.settings = group_settings
        # Persist and release the SQLite write lock before Telegram I/O.
        await session.commit()

        if previous == target_enabled:
            status_line = "状态未变化"
        else:
            status_line = "已更新"
        state_text = "已启用" if target_enabled else "已停用"
        await _answer(
            message,
            settings,
            "<b>AV 开关</b>\n"
            f"<b>群ID</b>: {message.chat.id}\n"
            f"<b>结果</b>: {state_text}（{status_line}）",
        )
        return

    if is_group_chat:
        group_row = await _ensure_group_row(session, message.chat.id, message.chat.title or "")
        group_av_enabled = _is_group_av_enabled(group_row.settings)
        await session.commit()
        if not group_av_enabled:
            # 未启用就只回提示：不删任何图片（不越权动用户的图）。
            await _answer(
                message,
                settings,
                "<b>AV 查询</b>\n当前群组未启用该功能，请最高管理员发送 /av enable。",
            )
            return
        if group_image_request is not None:
            image_message, command_message = group_image_request
            await _handle_group_av_image(
                message,
                settings=settings,
                image_message=image_message,
                command_message=command_message,
            )
            return
    if not is_group_chat:
        # 私聊放开给普通用户，但每人每小时最多 N 次（识图 + 文字查询合计）。
        # 超限时既不建服务对象、也不发起任何外部请求/模型调用。
        user = message.from_user
        allowed, retry_after = _AV_PRIVATE_RATE_LIMITER.allow(
            int(user.id) if user is not None else 0
        )
        if not allowed:
            await _answer(
                message,
                settings,
                f"<b>AV 查询</b>\n{_av_rate_limited_text(retry_after)}",
                auto_delete_seconds=0,
            )
            return
    svc = AVSearchService(settings)
    if not svc.enabled:
        await _answer(message, settings, "<b>AV 查询</b>\n当前已禁用。")
        return

    owner_user_id = message.from_user.id if message.from_user else 0
    query = _truncate_text(args, 120)
    is_code_query = is_av_code_query(query)

    async with typing_action(message, enabled=settings.bot.enable_typing):
        if is_code_query:
            detail = await svc.lookup_by_code(query)
            if detail:
                item = AVSearchItem(
                    source=detail.source,
                    title=detail.title,
                    code=detail.code,
                    url=detail.url,
                    cover_url=detail.cover_url,
                    date=detail.date,
                    summary=detail.summary,
                )
                av_session = _AV_SESSION_STORE.create(
                    owner_user_id=owner_user_id,
                    query=query,
                    results=[item],
                )
                av_session.details[0] = detail
                sent_ok = await _send_av_detail(
                    message=message,
                    session=av_session,
                    result_idx=0,
                    detail=detail,
                )
                if not sent_ok:
                    await _answer(
                        message,
                        settings,
                        "<b>AV 查询</b>\n详情发送失败，请稍后重试。",
                    )
                return

        results = await svc.search(query)

    if not results:
        await _answer(
            message,
            settings,
            render_data_brief(
                "AV 搜索结果",
                metadata={"关键词": f"<code>{html.escape(query)}</code>"},
                empty="未找到匹配内容。",
            ),
        )
        return

    av_session = _AV_SESSION_STORE.create(
        owner_user_id=owner_user_id,
        query=query,
        results=results,
    )
    text, keyboard = _build_av_search_page(av_session, page=0)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("avs:"))
async def on_av_search_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return
    token = parts[1]
    page = _parse_int(parts[2], default=0)
    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可操作该列表", show_alert=True)
        return

    text, keyboard = _build_av_search_page(av_session, page=page)
    try:
        await msg.edit_text(text, reply_markup=preserve_delete_button(msg, keyboard), disable_web_page_preview=True)
    except Exception:
        await msg.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data.startswith("avd:"))
async def on_av_detail_select(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("参数错误", show_alert=True)
        return

    token = parts[1]
    idx = _parse_int(parts[2], default=-1)
    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return
    if idx < 0 or idx >= len(av_session.results):
        await callback.answer("目标不存在", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可查看详情", show_alert=True)
        return

    detail = av_session.details.get(idx)
    if not detail:
        # 私聊里这次点击会去源站抓详情（= 一次外部查询）：超限的人不许再触发，
        # 但不计数（只查），免得正常用户点几下按钮就被算成超额查询。
        retry_after = _av_private_fetch_blocked(callback, msg)
        if retry_after is not None:
            await callback.answer(_av_rate_limited_text(retry_after), show_alert=True)
            return
        item = av_session.results[idx]
        svc = AVSearchService(settings)
        async with typing_action(msg, enabled=settings.bot.enable_typing):
            detail = await svc.fetch_detail(item)
        if not detail:
            await callback.answer("详情抓取失败，请稍后重试", show_alert=True)
            return
        av_session.details[idx] = detail

    ok = await _send_av_detail(
        message=msg,
        session=av_session,
        result_idx=idx,
        detail=detail,
        in_place=True,
    )
    if not ok:
        await callback.answer("详情更新失败，请重试", show_alert=True)
        return
    await callback.answer("已更新详情")


@router.callback_query(F.data.startswith("avm:"))
async def on_av_seed_paging(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    if not callback.data:
        await callback.answer()
        return
    if session is None:
        await callback.answer("会话未就绪，请重新 /av", show_alert=True)
        return
    msg = callback.message
    if not msg:
        await callback.answer("消息已失效", show_alert=True)
        return
    if not await _ensure_callback_group_authorized(callback, msg, session):
        return
    if not await _ensure_av_callback_scope(callback, msg, session, settings):
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("参数错误", show_alert=True)
        return

    token = parts[1]
    idx = _parse_int(parts[2], default=-1)
    page = _parse_int(parts[3], default=0)

    av_session = _AV_SESSION_STORE.get(token)
    if not av_session:
        await callback.answer("查询已过期，请重新 /av 搜索", show_alert=True)
        return
    if idx < 0 or idx >= len(av_session.results):
        await callback.answer("目标不存在", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not _av_session_owner_ok(av_session, user_id):
        await callback.answer("仅发起查询的人可浏览种子", show_alert=True)
        return

    detail = av_session.details.get(idx)
    if not detail:
        # 私聊里这次点击会去源站抓详情（= 一次外部查询）：超限的人不许再触发，
        # 但不计数（只查），免得正常用户点几下按钮就被算成超额查询。
        retry_after = _av_private_fetch_blocked(callback, msg)
        if retry_after is not None:
            await callback.answer(_av_rate_limited_text(retry_after), show_alert=True)
            return
        item = av_session.results[idx]
        svc = AVSearchService(settings)
        async with typing_action(msg, enabled=settings.bot.enable_typing):
            detail = await svc.fetch_detail(item)
        if not detail:
            await callback.answer("详情抓取失败，请稍后重试", show_alert=True)
            return
        av_session.details[idx] = detail

    if not detail.seeds:
        await callback.answer("无种子信息", show_alert=True)
        return

    text, keyboard = _build_av_seed_page(
        session=av_session,
        result_idx=idx,
        detail=detail,
        page=page,
    )
    ok = await _edit_av_seed_in_place(
        message=msg,
        detail=detail,
        text=text,
        keyboard=keyboard,
    )
    if not ok:
        await callback.answer("更新失败，请重新 /av", show_alert=True)
        return
    await callback.answer()


# --------------------------------------------------------- member self-service
# Telegram command names are ASCII-only (the "/" menu and Command() both reject
# non-latin), so these are /report, /find and /health rather than Chinese names.

_REPORT_COOLDOWN_SECONDS = 90
_report_cooldown: dict[tuple[int, int], float] = {}
_REPORT_USAGE = (
    "<b>/report 用法</b>\n"
    "回复要举报的消息后发送 /report [补充说明]\n\n"
    "机器人会先让审核模型立刻复核这条消息，再把复核结果和消息内容一起转给管理员。"
)


def _report_is_throttled(group_id: int, user_id: int) -> int:
    """Seconds to wait before this member may report again (0 = allowed)."""

    key = (int(group_id), int(user_id))
    now = time.monotonic()
    elapsed = now - _report_cooldown.get(key, 0.0)
    remaining = _REPORT_COOLDOWN_SECONDS - elapsed
    return int(remaining) + 1 if remaining > 0 else 0


@router.message(Command("report"))
async def cmd_report(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Report a message the moderation stage let through."""

    from bot.services.call_admin import send_member_report_notice
    from bot.services.moderation import ModerationService

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        await _answer(message, settings, _REPORT_USAGE)
        return
    reported = str(
        getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
    ).strip()
    if not reported:
        await _answer(
            message,
            settings,
            "这条消息没有可复核的文字。图片、贴纸或纯媒体请直接 @admin 请管理员查看。",
        )
        return

    group_id = int(message.chat.id)
    reporter_id = int(getattr(message.from_user, "id", 0) or 0)
    reporter_name = " ".join(
        part
        for part in (
            getattr(message.from_user, "first_name", "") or "",
            getattr(message.from_user, "last_name", "") or "",
        )
        if part
    ) or str(reporter_id)
    reason = str(message.text or "").partition(" ")[2].strip()

    wait = _report_is_throttled(group_id, reporter_id)
    if wait:
        await _answer(message, settings, f"刚举报过，请等 {wait} 秒后再发。")
        return

    group_row = await session.get(Group, group_id)
    group_settings = dict(group_row.settings or {}) if group_row is not None else {}

    # Force one fresh semantic check. The whole point of the report is that the
    # pipeline already passed this message, so a cached "clean" verdict must not
    # short-circuit it.
    check_summary = ""
    try:
        # 举报的是"已经放过去"的消息：把它的上下文一起送审，避免只看一句就下结论
        report_context, _block = await build_moderation_context(
            session, group_id=group_id, anchor_text=reported, exclude_text=reported
        )
        moderation = ModerationService(settings.moderation, _moderation_llm(settings))
        verdict = await moderation.evaluate(
            session, group_id, reported, context="\n".join(report_context)
        )
        if verdict.violated:
            confidence = (
                f"，置信 {verdict.confidence:.2f}" if verdict.confidence else ""
            )
            check_summary = f"模型判定「违规」{confidence}｜{verdict.reason}"
        elif verdict.conclusive:
            check_summary = f"模型判定「未违规」｜{verdict.reason}"
        else:
            check_summary = "模型复核没有给出明确结论（可能漏判，请人工判断）"
    except Exception:
        log.warning("[%s] /report re-check failed", group_id, exc_info=True)
        check_summary = "模型复核暂时不可用，请管理员人工判断"
    await session.commit()

    sent = await send_member_report_notice(
        message.bot,
        session,
        settings,
        group_id=group_id,
        reporter_id=reporter_id,
        reporter_name=reporter_name,
        reported_text=reported,
        reason=reason,
        check_summary=check_summary,
        group_settings=group_settings,
    )
    if sent:
        _report_cooldown[(group_id, reporter_id)] = time.monotonic()
        log.info(
            "member report accepted | group=%s user=%s violated=%s",
            group_id,
            reporter_id,
            check_summary,
        )
        await _answer(message, settings, "<b>已受理举报</b>\n已转交管理员复核，请等管理员处理。")
        return
    await _answer(
        message,
        settings,
        "暂时没能通知到管理员，请直接 @admin 描述一下情况。",
    )


# 签到：群友发的那条命令 2 秒后清掉，回执 5 秒后消失（用户指定）。
# 时长改动只动这两行。
CHECKIN_COMMAND_DELETE_SECONDS = 2
CHECKIN_RECEIPT_SECONDS = 5


async def _schedule_checkin_command_cleanup(message: Message, group_id: int) -> None:
    """删掉群友发的那条 /checkin 命令本身。

    走 durable 调度器（写入 telegram_delete_jobs），所以重启不会把这条待删任务丢掉；
    调度器不健康时只记日志——签到已经记上了，不能因为清理失败就报错给用户。
    """

    try:
        accepted = await schedule_message_auto_delete_durable(
            message, CHECKIN_COMMAND_DELETE_SECONDS
        )
    except Exception:
        log.warning(
            "[%s] checkin command cleanup scheduling failed", group_id, exc_info=True
        )
        return
    if not accepted:
        log.warning(
            "[%s] checkin command cleanup rejected | message=%s",
            group_id,
            getattr(message, "message_id", "?"),
        )


@router.message(Command("checkin"))
async def cmd_checkin(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """每日签到：一个成员在一个群里一天只能记一次分。

    去重靠 member_checkins 的唯一索引（见 bot/services/checkin.py），所以重复点
    不会多加分；回执文案把"今天签过了"和"签到成功"分开说清楚。
    """

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    user = getattr(message, "from_user", None)
    if user is None or bool(getattr(user, "is_bot", False)):
        return

    group_id = int(message.chat.id)
    outcome = await record_checkin(
        session,
        group_id=group_id,
        user_id=int(user.id),
        display_name=str(getattr(user, "full_name", "") or ""),
    )
    await session.commit()
    if outcome.already:
        log.info("checkin repeated | group=%s user=%s", group_id, user.id)
        await _schedule_checkin_command_cleanup(message, group_id)
        await _answer(
            message,
            settings,
            render_checkin_receipt(outcome),
            auto_delete_seconds=CHECKIN_RECEIPT_SECONDS,
        )
        return
    log.info(
        "checkin recorded | group=%s user=%s total=%s streak=%s",
        group_id,
        user.id,
        outcome.total_points,
        outcome.streak,
    )
    await _schedule_checkin_command_cleanup(message, group_id)
    await _answer(
        message,
        settings,
        render_checkin_receipt(outcome),
        auto_delete_seconds=CHECKIN_RECEIPT_SECONDS,
    )


async def _refresh_checkin_reminder_roster(
    callback: CallbackQuery,
    session: AsyncSession,
    *,
    group_id: int,
) -> None:
    """把提醒里的「今日已签到 N 人 + 已签到名单」刷成最新（best-effort）。

    只改**这条提醒自己**：靠 ``checkin_reminder_posts`` 按 (群, 消息) 反查它属于
    哪个时段，再用同一个渲染函数重算文案（人数与名单一次查全，绝不只刷新一半）。
    找不到台账（不是提醒消息、或老消息）就直接跳过。任何失败只记日志、不重试——
    签到已经落库了，编辑失败不能反过来影响签到，也不许往群里补发消息。
    """

    message = getattr(callback, "message", None)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if message is None or message_id <= 0:
        return
    try:
        slot = await find_reminder_slot(
            session, group_id=group_id, message_id=message_id
        )
        if slot is None:
            return
        roster = await today_checkin_roster(session, group_id=group_id)
        await message.edit_text(
            render_checkin_reminder(
                slot=slot, checked_in=roster.count, names=roster.names
            ),
            parse_mode="HTML",
            reply_markup=getattr(message, "reply_markup", None),
        )
    except Exception:
        log.warning(
            "[%s] checkin reminder roster refresh failed | message=%s",
            group_id,
            message_id,
            exc_info=True,
        )


@router.callback_query(F.data == CHECKIN_CALLBACK_DATA)
async def on_checkin_button(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    """「✅ 一键签到」按钮：走与 /checkin 完全相同的签到逻辑。

    点击者身份**只**取自 ``callback.from_user``——callback_data 是固定常量，
    消息作者也不能当成点击者，否则任何人都能替别人签到。
    回执只用 answer_callback_query 的轻提示，不再往群里发消息（避免刷屏）。
    """

    if session is None:
        await callback.answer("会话未就绪，请稍后再试")
        return
    clicked = getattr(callback, "from_user", None)
    if clicked is None or bool(getattr(clicked, "is_bot", False)):
        await callback.answer("请由群成员本人点击签到")
        return
    chat = getattr(getattr(callback, "message", None), "chat", None)
    if chat is None or str(getattr(chat, "type", "")) not in ("group", "supergroup"):
        await callback.answer("一键签到只能在群里使用")
        return

    group_id = int(chat.id)
    if not await is_group_authorized(session, group_id):
        await callback.answer("本群尚未授权，暂时无法签到")
        return

    outcome = await record_checkin(
        session,
        group_id=group_id,
        user_id=int(clicked.id),
        display_name=str(getattr(clicked, "full_name", "") or ""),
    )
    await session.commit()
    # 先给 toast 回执，再 best-effort 刷新提醒上的人数：先回执用户才不会等编辑结果
    await callback.answer(render_checkin_toast(outcome))
    if outcome.already:
        log.info("checkin button repeated | group=%s user=%s", group_id, clicked.id)
        return
    log.info(
        "checkin button recorded | group=%s user=%s total=%s streak=%s",
        group_id,
        clicked.id,
        outcome.total_points,
        outcome.streak,
    )
    await _refresh_checkin_reminder_roster(callback, session, group_id=group_id)


@router.message(Command("points"))
async def cmd_points(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """查看自己的签到积分（只读，不签到）。"""

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    user = getattr(message, "from_user", None)
    if user is None:
        return

    outcome = await summarize(
        session, group_id=int(message.chat.id), user_id=int(user.id)
    )
    await session.commit()
    today_line = (
        f"今日已签到（连续 {outcome.streak} 天，下次可得 +{outcome.next_award} 分）"
        if outcome.already
        else f"今日未签到，发送 /checkin 可得 +{outcome.next_award} 分"
    )
    spent_line = (
        f"｜累计获得 {outcome.total_points} 分，已消耗 {outcome.spent_points} 分"
        if outcome.spent_points
        else ""
    )
    await _answer(
        message,
        settings,
        "<b>我的积分</b>\n"
        f"可用 <b>{outcome.available_points}</b> 分{spent_line}\n"
        f"连续 {outcome.streak} 天｜共签到 {outcome.total_days} 天\n"
        f"{today_line}\n"
        f"广告质询时可用 {CHALLENGE_SKIP_COST} 积分直接免除。",
    )


_RANK_MEDALS = ("🥇", "🥈", "🥉")


def _rank_wants_week(message: Message) -> bool:
    """``/rank week`` 看本周榜；其它参数（含 ``/rank@bot week``）只看有没有 week。"""

    text = str(getattr(message, "text", "") or "")
    argument = text.partition(" ")[2].strip().lower()
    return bool(argument) and argument.split()[0] == "week"


def _render_rank_board(board: RankBoard) -> str:
    """积分榜文案：前三名挂奖牌，末尾一行总是自己的名次（不在榜内也显示）。"""

    if board.mode == "week":
        title = "本周积分榜"
        empty = "本群本周还没有人签到，发送 /checkin 抢第一。"
    else:
        title = "本群积分榜"
        empty = "本群还没有人签到，发送 /checkin 抢第一。"

    if not board.has_data:
        return f"<b>{title}</b>\n{empty}"

    lines = [f"<b>{title}</b>"]
    for index, entry in enumerate(board.entries):
        medal = _RANK_MEDALS[index] if index < len(_RANK_MEDALS) else f"{index + 1}."
        lines.append(f"{medal} {html.escape(entry.display_name)} · {entry.points} 分")

    mine = f"你：第 {board.caller_rank} 名 · 可用 {board.caller_available} 分"
    if board.mode == "week":
        mine += f"（本周 +{board.caller_points} 分）"
    lines.append(mine)
    return "\n".join(lines)


def _render_member_profile(profile: MemberProfile) -> str:
    """个人档案文案：积分、签到、违规、封禁一次说清；被封禁时加粗提醒。"""

    spent = (
        f"（累计获得 {profile.total_points} 分，已消耗 {profile.spent_points} 分）"
        if profile.spent_points
        else ""
    )
    today_line = (
        f"今日已签到，明天可得 +{profile.next_award} 分。"
        if profile.signed_today
        else f"今日还没签到，发送 /checkin 可得 +{profile.next_award} 分。"
    )
    lines = [
        "<b>我的档案</b>",
        f"可用积分：<b>{profile.available_points}</b> 分{spent}",
        f"连续签到：{profile.streak} 天｜累计签到：{profile.total_days} 天",
        today_line,
        f"违规记录：累计 {profile.warning_count} 次"
        f"｜近 {profile.window_days} 天被审核命中 {profile.recent_violations} 次",
    ]
    if profile.banned:
        lines.append("<b>你目前在封禁名单里，请联系管理员处理。</b>")
    return "\n".join(lines)


@router.message(Command("rank"))
async def cmd_rank(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """本群积分榜 Top10（只读）：默认按可用积分，``/rank week`` 按本周获得积分。"""

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    user = getattr(message, "from_user", None)
    if user is None:
        return

    board = await build_rank_board(
        session,
        group_id=int(message.chat.id),
        user_id=int(user.id),
        week=_rank_wants_week(message),
    )
    await session.commit()
    await _answer(message, settings, _render_rank_board(board))


@router.message(Command("me"))
async def cmd_me(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """个人档案（只读）：积分、签到、违规与封禁状态。"""

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    user = getattr(message, "from_user", None)
    if user is None:
        return

    profile = await member_profile(
        session, group_id=int(message.chat.id), user_id=int(user.id)
    )
    await session.commit()
    await _answer(message, settings, _render_member_profile(profile))


@router.message(Command("find"))
async def cmd_find(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Search this group's retained archive (the same recall the bot uses)."""

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    query = str(message.text or "").partition(" ")[2].strip()
    if len(query) < 2:
        await _answer(
            message,
            settings,
            "<b>/find 用法</b>\n/find &lt;关键词&gt;\n\n"
            "在当前群的保留期聊天记录里找你需要的消息（默认 5 条）。",
        )
        return

    memory = memory_holder.get_optional()
    if memory is None:
        await _answer(message, settings, "记忆服务还没就绪，稍后再试。")
        return
    await session.commit()
    try:
        hits = await memory.recall_archive(
            int(message.chat.id), query=query, limit=5
        )
    except Exception:
        log.warning("[%s] /find recall failed", message.chat.id, exc_info=True)
        await _answer(message, settings, "搜索暂时不可用，稍后再试。")
        return

    if not hits:
        await _answer(
            message,
            settings,
            f"没找到与「{html.escape(query)}」匹配的消息（只搜当前群、保留期内的记录）。",
        )
        return

    lines = [f"<b>群内搜索</b> · 「{html.escape(query)}」"]
    for index, row in enumerate(hits, start=1):
        content = " ".join(
            str(
                row.get("content") or row.get("raw_text") or row.get("derived_text") or ""
            ).split()
        )
        if not content:
            content = f"[{row.get('message_type') or '非文本消息'}]"
        body = content if len(content) <= 90 else content[:89] + "…"
        when = str(row.get("sent_at") or "")[5:16]
        who = str(
            row.get("sender_name")
            or row.get("sender_display_name")
            or row.get("sender_username")
            or "未知"
        )
        lines.append(f"{index}. <code>{when}</code> {html.escape(who)}：{html.escape(body)}")
    lines.append("\n<i>最多列出 5 条，按相关度排序。</i>")
    await _answer(message, settings, "\n".join(lines), disable_web_page_preview=True)


@router.message(Command("health"))
async def cmd_health(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """One-command operations snapshot for this group (admins)."""

    from sqlalchemy import text

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return
    if not await ensure_group_authorized(message, session, settings):
        return
    if not await ensure_group_admin_permission(message, session, settings):
        return
    group_id = int(message.chat.id)
    await session.commit()

    # violations.created_at is UTC while the archive's sent_at is Shanghai
    # local: window each on its own clock instead of feeding both one boundary.
    utc_cutoff = now_shanghai_naive().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(hours=8)
    try:
        rule_rows = (
            await session.execute(
                text(
                    "select rule_id, count(*) from violations "
                    "where group_id = :gid and created_at >= :cutoff "
                    "group by rule_id order by rule_id"
                ),
                {"gid": group_id, "cutoff": utc_cutoff},
            )
        ).all()
        pending_rows = (
            await session.execute(
                text(
                    "select kind, count(*) from join_verifications "
                    "where group_id = :gid and status = 'pending' group by kind"
                ),
                {"gid": group_id},
            )
        ).all()
        archive_rows = (
            await session.execute(
                text(
                    "select count(*) from group_message_archive where group_id = :gid"
                ),
                {"gid": group_id},
            )
        ).scalar_one()
        today_senders = (
            await session.execute(
                text(
                    "select count(distinct sender_id) from group_message_archive "
                    "where group_id = :gid and sent_at >= :since"
                ),
                {"gid": group_id, "since": utc_cutoff + timedelta(hours=8)},
            )
        ).scalar_one()
    except Exception:
        log.warning("[%s] /health query failed", group_id, exc_info=True)
        await _answer(message, settings, "统计暂时不可用，稍后再试。")
        return
    await session.commit()

    rule_map = {1: "语义审核", 4: "本地正则"}
    hits = "、".join(
        f"{rule_map.get(int(rid), '规则' + str(rid))} {int(count)} 次"
        for rid, count in rule_rows
    ) or "0 次"
    pending = "、".join(f"{kind} {int(count)}" for kind, count in pending_rows) or "无"
    memory_ready = "正常" if memory_holder.get_optional() is not None else "未就绪"
    routing = "、".join(
        f"{stage}={getattr(getattr(settings.bot, attr), 'model', '?')}"
        for stage, attr in (
            ("闲聊", "main_model"),
            ("决策", "decision_model"),
            ("审核", "moderation_model"),
            ("技能", "skill_model"),
            ("看图", "vision_model"),
            ("压缩", "compress_model"),
        )
    )
    await _answer(
        message,
        settings,
        "<b>运行状态</b>\n"
        f"今日审核命中：{hits}\n"
        f"待完成质询：{pending}\n"
        f"今日活跃成员：{int(today_senders)} 人\n"
        f"归档消息：{int(archive_rows)} 条\n"
        f"记忆服务：{memory_ready}\n"
        f"<i>{html.escape(routing)}</i>",
        auto_delete_seconds=0,
    )


# ---------------------------------------------------------------------------
# 积分商店（/shop、/tag、/top、/draw）
#
# 设计约束：这里只做"取参数 → 调服务 → 发回执"，扣分/退款/幂等/到期清理全在
# bot.services.point_shop 里（那套逻辑 CLI 也要用）。商店里任何异常都只影响这一条
# 请求，所以每个处理器都自己兜住异常，绝不让它冒泡去影响审核/回复/签到。
# ---------------------------------------------------------------------------

#: 「我的积分」按钮的 callback_data（只有这一个按钮，点了真的会返回余额）
SHOP_BALANCE_CALLBACK = "shop:points"

_SHOP_MENU_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="我的积分", callback_data=SHOP_BALANCE_CALLBACK)]
    ]
)

_SHOP_UNAVAILABLE_TEXT = "商店暂时不可用，请稍后再试（这次没有扣分）。"


def _shop_command_argument(message: Message) -> str:
    """命令后面的全部参数：``/tag 摸鱼冠军`` → ``摸鱼冠军``（``/tag@bot 文字`` 同理）。"""

    text = str(getattr(message, "text", "") or "")
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _shop_caller(message: Message) -> object | None:
    """发命令的人；机器人/匿名身份返回 None（不参与商店）。"""

    user = getattr(message, "from_user", None)
    if user is None or bool(getattr(user, "is_bot", False)):
        return None
    return user


async def _shop_ready(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> object | None:
    """群内 + 已授权 + 真人发言，三条都满足才返回调用者。"""

    if not is_group(message):
        await _answer(message, settings, "该命令仅可在群内使用。")
        return None
    if not await ensure_group_authorized(message, session, settings):
        return None
    return _shop_caller(message)


@router.message(Command("shop"))
async def cmd_shop(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """积分商店：价目表 + 每件商品的确切用法 + 「我的积分」按钮。"""

    user = await _shop_ready(message, session, settings)
    if user is None:
        return
    group_id = int(message.chat.id)
    try:
        outcome = await summarize(
            session, group_id=group_id, user_id=int(user.id)
        )
        await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] shop menu failed | user=%s", group_id, user.id)
        await _answer(message, settings, _SHOP_UNAVAILABLE_TEXT)
        return
    await _answer(
        message,
        settings,
        render_shop_menu(available=outcome.available_points),
        reply_markup=_SHOP_MENU_BUTTON,
    )


@router.message(Command("tag"))
async def cmd_tag(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """自定义头衔：``/tag 文字``（30 分 7 天）、``/tag 文字 30天``（80 分 30 天）。"""

    user = await _shop_ready(message, session, settings)
    if user is None:
        return
    group_id = int(message.chat.id)
    try:
        reply = await buy_member_tag(
            session,
            bot=getattr(message, "bot", None),
            group_id=group_id,
            user_id=int(user.id),
            raw_text=_shop_command_argument(message),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] shop tag failed | user=%s", group_id, user.id)
        await _answer(message, settings, _SHOP_UNAVAILABLE_TEXT)
        return
    await _answer(message, settings, reply.text)


@router.message(Command("top"))
async def cmd_top(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """置顶自己的求助：回复自己的一条消息再发 /top（20 分 6 小时）。"""

    user = await _shop_ready(message, session, settings)
    if user is None:
        return
    group_id = int(message.chat.id)
    try:
        reply = await buy_pin(
            session,
            bot=getattr(message, "bot", None),
            group_id=group_id,
            user_id=int(user.id),
            target=resolve_pin_target(message),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] shop pin failed | user=%s", group_id, user.id)
        await _answer(message, settings, _SHOP_UNAVAILABLE_TEXT)
        return
    await _answer(message, settings, reply.text)


@router.message(Command("draw"))
async def cmd_draw(
    message: Message,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """抽奖：5 分一次，每人每天最多 10 次（本地自然日）。"""

    user = await _shop_ready(message, session, settings)
    if user is None:
        return
    group_id = int(message.chat.id)
    try:
        reply = await play_lottery(
            session,
            group_id=group_id,
            user_id=int(user.id),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] shop draw failed | user=%s", group_id, user.id)
        await _answer(message, settings, _SHOP_UNAVAILABLE_TEXT)
        return
    await _answer(message, settings, reply.text)


@router.callback_query(F.data == SHOP_BALANCE_CALLBACK)
async def on_shop_balance(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession | None = None,
) -> None:
    """「我的积分」按钮：真的查一次余额再弹出来。"""

    message = callback.message
    chat = getattr(message, "chat", None)
    if session is None or chat is None:
        await callback.answer("会话未就绪，请重新发送 /shop", show_alert=True)
        return
    if str(getattr(chat, "type", "") or "") not in ("group", "supergroup"):
        await callback.answer("该按钮仅在群内可用", show_alert=True)
        return
    if not await is_group_authorized(session, int(chat.id)):
        await callback.answer("当前群未授权，请联系最高管理员。", show_alert=True)
        return
    user = callback.from_user
    if user is None or bool(getattr(user, "is_bot", False)):
        await callback.answer()
        return
    try:
        outcome = await summarize(
            session, group_id=int(chat.id), user_id=int(user.id)
        )
        await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] shop balance failed | user=%s", chat.id, user.id)
        await callback.answer("查询失败，请稍后再试", show_alert=True)
        return
    await callback.answer(
        render_balance(available=outcome.available_points, streak=outcome.streak),
        show_alert=True,
    )
