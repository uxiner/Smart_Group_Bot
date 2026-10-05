"""1 对 1 私聊处理器：文字闲聊 + 图片理解。

用户口径（2026-10-03）：只有已授权群的成员能私聊（普通成员 100 条/天、群管理员
500 条/天，各自的全局上限 20000 / 100000 条/天，最高管理员不设限）；私聊不做群规
审核（NSFW 也放开，群里那条底线不受影响）；私聊正文**只落私聊自己的表**
（``private_chat_messages``，不进群归档/记忆/向量），按 token 预算装配后作为历史，
所以机器人重启不失忆、也能记住很久以前说过的话。

**注册顺序**：这个 router 必须排在 ``group.router`` **之前**——群消息处理器用的是
``F.text | F.photo | ...`` 这种宽泛过滤，且靠函数体里 ``is_group()`` 早退，谁先注册
谁先吃消息。排它前面才能保证私聊消息不会被群处理器吃掉。

**文字还是语音（2026-10-06）**：和群聊一样由模型**自主**决定这一条发文字还是发语音——不是
关键词命中才准发语音，也不是每条都强制语音，更没有固定随机概率。做法是在同一次主回复的
最前面带一行严格信封 ``[[DM_DELIVERY: text|voice]]``（传输标记，**发给用户前剥掉、
绝不落库**）。私聊这条路径当前是 plain chat（``answer_with_search`` → ``llm.chat``，
一次调用出正文），这里沿用它，不为每条私聊增加一次工具循环或一次额外的 LLM 分类调用。最高管理员本轮的明确
文字/语音指示按**真实鉴权结果**（``verdict.is_super``）优先于自主选择；正文里自称超管
不算数。投递、拒收降级、配额与取消的语义全在 :mod:`bot.services.private_tts`。
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
from bot.services.doubao_tts import DoubaoTTSService
from bot.services.group_public_context import (
    load_group_titles,
    load_user_public_group_context,
)
from bot.services.long_term_memory import (
    load_private_chat_facts,
    memory_facts_enabled,
    memory_recall_limit,
)
from bot.services import policy_runtime
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
    refund_daily_quota,
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
from bot.services.private_tts import (
    DeliveryReceipt,
    build_private_tts_preference,
    deliver_private_reply,
    load_owner_delivery_state,
    parse_dm_delivery,
    resolve_delivery_directive,
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
        business_context_tokens=getattr(bot_cfg, "context_budget_tokens", None),
        context_reserve_tokens=getattr(bot_cfg, "context_reserve_tokens", None),
    )


def _tts_service(settings: Settings):
    """私聊这一轮能不能用语音。构造失败 / 没配 / 全局关掉 → ``None``（纯文字）。

    私聊没有自己的 TTS 开关，口径就是**全局那一个**：``DoubaoTTSService(settings)`` 的
    ``available`` 已经把 ``doubao_tts_enabled`` 与供应商凭据一起判完了。所以不需要任何
    新配置项，也就不会影响群里现有的 ``tts_mode`` 行为。
    """

    try:
        service = DoubaoTTSService(settings)
    except Exception as exc:
        log.warning("private chat: 语音服务构造失败（本轮按纯文字处理） | error=%s", exc)
        return None
    return service if service.available else None


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


def not_member_notice() -> str:
    """「你不是授权群成员」提示（可配文案；回落到模块常量）。"""

    return policy_runtime.display_policy().private_not_member_notice or (
        NOT_MEMBER_NOTICE
    )


def unsupported_media_notice() -> str:
    """「私聊只支持文字/图片」提示（可配文案；回落到模块常量）。"""

    return policy_runtime.display_policy().private_media_unsupported_notice or (
        MEDIA_UNSUPPORTED_NOTICE
    )


async def _image_description(message: Message, llm) -> str:
    from bot.handlers.group import _build_vision_data_uri

    info = _image_file_info(message)
    if not info:
        return ""
    # 预算现取：一次认图从下载到视觉调用共用同一个值，途中改配置不会让
    # "下载用 30s、调用用 5s" 这种拼接。
    budget = policy_runtime.private_chat_policy().vision_budget_seconds
    try:
        async with asyncio.timeout(budget):
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


def _split_for_telegram(
    text: str, *, limit: int | None = MAX_REPLY_CHARS
) -> list[str]:
    """按行切分长回复；单行超长时硬切。

    ``limit=None`` = 现取运行时配置。显式传值只给测试/内部调用。
    """

    limit = int(
        limit
        if limit is not None
        else policy_runtime.private_chat_policy().reply_max_chars
    )
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


async def _send_reply(
    message: Message,
    text: str,
    receipt: DeliveryReceipt | None = None,
) -> None:
    """把正文分段发出去，**每确认一条送达就立刻记回执**。

    长回复会被切成多条，第 2 条失败时第 1 条其实已经发到对方那里了。第一版这里是「要么
    全部成功、要么整段当失败」，于是调用方退配额 + 历史缺失，而对方明明已经收到了一条。
    现在分段逐条上账：抛异常只说明**还没发的那部分**没发出去。
    """

    for chunk in _split_for_telegram(text):
        await message.answer(chunk, parse_mode=None)
        if receipt is not None:
            receipt.add(chunk)


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
        await _send_notice(
            message, not_member_notice(), "not_member"
        )
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

    # B-04：配额是"先扣再用"，而扣减在 consume_daily_quota 内部就 commit 了，
    # 后面的 rollback() 撤不回来。真正回上话才算"用掉"，下面每一条没能回上话的
    # 出口都要显式退还，否则模型故障期间用户会白白耗光当日配额。
    async def give_the_quota_back(reason: str) -> None:
        if outcome is None or not outcome.allowed:
            return  # 超限分支 consume_daily_quota 自己回滚过；超管根本不计数
        log.info(
            "private chat: 本轮没回上话，退还配额 | user=%s | reason=%s",
            user.id,
            reason,
        )
        await refund_daily_quota(
            session,
            user_id=user.id,
            is_admin=verdict.is_admin,
        )

    # 3) 媒体：只处理图片；其它类型回一句说明（不占模型）。
    image_description = ""
    if _has_media(message):
        if _image_file_info(message) is None:
            await give_the_quota_back("unsupported_media")
            await _send_notice(
                message, unsupported_media_notice(), "media"
            )
            return
        llm = _reply_llm(settings)
        image_description = await _image_description(message, llm)
        if not image_description and not text:
            await give_the_quota_back("no_image_description")
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

    # 私聊语音：和群聊一样由模型**自主**决定这一条发文字还是发语音——不是关键词命中
    # 才准发语音，也不是每条都强制语音。
    #   * 全局 TTS 不可用时不给模型任何提示，它就自然回文字，也不会说「只能打字」；
    #   * 最高管理员本轮的明确指示按**真实鉴权结果**（verdict.is_super）才生效。
    #     正文里自称「我是最高管理员」不算数——那只是正文，不是鉴权结论。
    #   * 指示分「本轮」与「持续」：「这次用文字」只管这一轮；「以后都用语音」是持续
    #     指示，由**他自己私聊历史里的原话**折叠出来（不新增任何表 / 全局配置），最新一次
    #     有效修改或解除优先。群公开资料、检索留档、助手自己说过的话都不参与。
    tts_service = _tts_service(settings)
    owner_directive = ""
    directive_source = ""
    if verdict.is_super:
        try:
            persistent_directive = await load_owner_delivery_state(session, user.id)
        except Exception as exc:  # 读不到就当没有持续指示，绝不猜
            log.warning(
                "private chat: 读取最高管理员媒介偏好失败（本轮按自主选择） | user=%s | error=%s",
                user.id,
                exc,
            )
            persistent_directive = ""
        owner_directive, directive_source = resolve_delivery_directive(
            text=text,
            persistent=persistent_directive,
            is_super=True,
        )
    tts_preference = build_private_tts_preference(
        service_ready=tts_service is not None,
        owner_directive=owner_directive,
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
        tts_preference=tts_preference,
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
        # 投递信封：模型在最前面带一行 [[DM_DELIVERY: text|voice]]。它是传输标记，
        # **绝不发出去、绝不落库**。畸形信封剥掉壳退回文字，正文一个字都不丢。
        plan = parse_dm_delivery(str(answer.text or ""))
    except Exception as exc:
        await session.rollback()
        log.warning("private chat: 回复失败 | user=%s | error=%s", user.id, exc)
        await give_the_quota_back("model_failed")
        await _send_notice(message, BUSY_NOTICE, "busy")
        return

    reply = plan.text
    delivery = plan.delivery
    delivery_source = "model"
    if owner_directive:
        # 最高管理员的媒介指示优先于模型的自主选择。``directive_source`` 说清这是
        # 本轮说的（turn）、历史里持续有效的（history）、还是被撤销了（autonomy/released）。
        delivery = owner_directive
        delivery_source = f"owner:{directive_source}"
    if not reply:
        await give_the_quota_back("empty_reply")
        await _send_notice(message, BUSY_NOTICE, "busy")
        return

    log.info(
        "private chat: 投递选择 | user=%s | medium=%s | 依据=%s | chars=%d | 信封畸形=%s",
        user.id,
        delivery,
        delivery_source,
        len(reply),
        plan.malformed,
    )

    # 回执由这里持有：每确认一次 Telegram 送达就立刻记一笔。它决定「算不算回上话」
    # （配额）和「历史写什么」，与后面抛不抛异常、是不是取消**全都无关**。
    receipt = DeliveryReceipt()

    async def _send_text(body: str, book: DeliveryReceipt) -> None:
        await _send_reply(message, body, book)

    async def finalize(medium: str, *, complete: bool) -> bool:
        """把这一轮的账结完：回上话就落历史（不退配额），否则退款。

        ``complete`` 为真表示请求的每一段都送到了 → 历史写全文（正文原样，保留 markdown
        等只在屏幕上好看的东西）；为假表示只送出了一部分 → 历史**只写已送达的那部分**，
        绝不把没播出、连文字兜底也失败的尾巴写成助手说过的话。
        """

        if not receipt.delivered:
            return False
        store = history_store()
        user_turn = _stored_user_turn(text, image_description)
        store.append(user.id, "user", user_turn)
        # 落进历史的是**对方真正看到/听到的那段正文**（信封、提示词、合成报错都不算）。
        store.append(user.id, "assistant", reply if complete else receipt.text)
        # 落库：这一轮两行（用户 + 回复）。幂等键来自入站 message_id，Telegram 重投递
        # 同一轮不会写出第二份；写失败只记日志，绝不影响这次已经发出去的回复。
        await record_private_turn(
            session,
            user_id=user.id,
            user_content=user_turn,
            assistant_content=reply if complete else receipt.text,
            message_id=getattr(message, "message_id", None),
        )
        log.info(
            "private chat: 已回复 | user=%s | 档=%s | 今日=%s | 本档全局=%s | 日=%s | chars=%d | 媒介=%s",
            user.id,
            verdict.tier,
            f"{outcome.user_used}/{outcome.per_user_limit}" if outcome else "不限",
            f"{outcome.global_used}/{outcome.global_limit}" if outcome else "不限",
            local_day_key(),
            len(reply if complete else receipt.text),
            medium,
        )
        return True

    try:
        delivery_outcome = await deliver_private_reply(
            message,
            text=reply,
            delivery=delivery,
            service=tts_service,
            send_text=_send_text,
            receipt=receipt,
            uid=str(user.id),
        )
    except asyncio.CancelledError:
        # 取消照原样抛出，但不能凭取消把「已经送出去了」抹掉：先按回执结账再重抛。
        if await finalize("cancelled", complete=False):
            log.info(
                "private chat: 投递被取消，但已有 %d 段送达（照常计费、照常落历史）",
                len(receipt.parts),
            )
        else:
            await give_the_quota_back("cancelled_before_delivery")
        raise
    except Exception as exc:  # pragma: no cover - 投递编排对普通异常不再外抛
        log.warning("private chat: 投递异常 | user=%s | error=%s", user.id, exc)
        if not await finalize("exception", complete=False):
            await give_the_quota_back("send_failed")
        return
    if not await finalize(
        delivery_outcome.medium, complete=delivery_outcome.complete
    ):
        # 一条都没发出去 = 这一轮没回上话，按原语义退还当日配额。
        log.info(
            "private chat: 本轮没有任何可见回复 | user=%s | 选中=%s | error=%s",
            user.id,
            delivery,
            delivery_outcome.error,
        )
        await give_the_quota_back("delivery_failed")
        return
    if answer.searches:
        log.info(
            "private chat: 本轮联网检索 %d 次 | user=%s | 保险丝已触发=%s",
            answer.searches,
            user.id,
            answer.exhausted,
        )
