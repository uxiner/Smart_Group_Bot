"""周报投递的共享服务（B13a）：占位 → 渲染 → 发送 → 回填。

CLI（``bot.tools.weekly_report``）与运营任务中心必须走**同一段**投递链：
``weekly_report_posts`` 上的 ``(target_id, week_key)`` 唯一索引 + ``INSERT ...
ON CONFLICT DO NOTHING + RETURNING`` 占位发生在发送之前，发失败才把占位删掉
（"没发出去"不能被记成"已发过"）。任何一侧都不许另写一套锁。

对外只暴露结构化结果：调用方负责打印与退出码，本模块不 print。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import WeeklyReportPost
from bot.services.operations_guard import PersistedGuard, verify_guard
from bot.services.operations_receipts import (
    BusinessReceipt,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
    TASK_WEEKLY_REPORT,
    ambiguous_send_failure,
    claim_business_dispatch,
    definitely_not_delivered,
    load_receipt,
    record_receipt,
    release_business_dispatch,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

#: 私发超管的成本摘要用的 ``target_id``（0 不是合法群 id，不会和群冲突）。
COST_DIGEST_TARGET_ID = 0

#: ``(target_id, week_key)`` 台账的三种状态。
REPORT_SLOT_MISSING = "missing"
REPORT_SLOT_PLACEHOLDER = "placeholder"
REPORT_SLOT_DELIVERED = "delivered"

REPORT_OUTCOME_DELIVERED = "delivered"
REPORT_OUTCOME_ALREADY_DELIVERED = "already_delivered"
REPORT_OUTCOME_IN_FLIGHT = "in_flight"
REPORT_OUTCOME_RENDER_FAILED = "render_failed"
REPORT_OUTCOME_SEND_FAILED = "send_failed"
REPORT_OUTCOME_MARK_FAILED = "mark_failed"


def current_week_key(now: datetime | None = None) -> str:
    """本次运行所属的 ISO 周（``2026-W41``）——周报幂等键的一半。"""

    stamp = now or now_shanghai_naive()
    iso = stamp.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def previous_iso_week_key(week_key: str) -> str:
    """把 ISO 周键映射到**上一个完整周**（``2026-W41`` → ``2026-W40``）。

    R1（返工 v2）：CLI 从 v1 起用 ``current_week_key()``（**发送周**）当台账键，
    而运营任务中心用的是"上一个完整业务周"。两者描述的是同一条周报，必须落到
    **同一个出站证据/保护业务键**，否则手动发完 CLI 还会再发一次（父探针 v7 实测
    同群 ``send_count=2``）。CLI 因此显式传 ``business_key=`` 这个映射结果。
    """

    try:
        year_text, week_text = str(week_key).split("-W", 1)
        monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    except (ValueError, AttributeError):
        return str(week_key)
    iso = (monday - timedelta(days=7)).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def report_business_key(week_key: str, *, business_key: str | None = None) -> str:
    """台账键 → **业务周**键（出站证据与出站保护都用它）。

    调用方明确给出业务周（运营中心）时用它；没给就按调用方自己的台账语义
    （legacy CLI 的发送周）原样使用 —— 转换必须由 CLI 显式做，不做隐式猜测。
    """

    return str(week_key) if business_key is None else str(business_key)


async def claim_report_slot(
    session: AsyncSession,
    *,
    target_id: int,
    week_key: str,
) -> bool:
    """为 ``(target_id, week_key)`` 占位；True = 这个目标本周还没收到过。

    占位发生在**发送之前**并由调用方立刻 commit：已有该行时 SQLite 不返回行 →
    False，于是重叠运行 / 手工重跑都只有一个能发。发失败要调
    :func:`release_report_slot` 把占位删掉（"没发出去"不能被记成"已发过"）。
    """

    statement = (
        sqlite_insert(WeeklyReportPost)
        .values(
            target_id=int(target_id),
            week_key=str(week_key),
            message_id=0,
        )
        .on_conflict_do_nothing(index_elements=["target_id", "week_key"])
        .returning(WeeklyReportPost.id)
    )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    return claimed is not None


async def release_report_slot(
    session: AsyncSession, *, target_id: int, week_key: str
) -> None:
    """发送失败：删掉占位，下一轮还能重发。"""

    await session.execute(
        delete(WeeklyReportPost).where(
            WeeklyReportPost.target_id == int(target_id),
            WeeklyReportPost.week_key == str(week_key),
        )
    )


async def mark_report_sent(
    session: AsyncSession, *, target_id: int, week_key: str, message_id: int
) -> None:
    """发送成功后回填 Telegram message_id（供审计）。"""

    await session.execute(
        update(WeeklyReportPost)
        .where(
            WeeklyReportPost.target_id == int(target_id),
            WeeklyReportPost.week_key == str(week_key),
        )
        .values(message_id=int(message_id))
    )
    await session.commit()


async def report_slot_state(
    session: AsyncSession, *, target_id: int, week_key: str
) -> str:
    """台账状态：missing / placeholder（占位但没回填 = 别的发送者可能正在发）/ delivered。

    **占位不是成功**：只有回填过的 ``message_id`` 才算"这一周真的发出去过"。
    """

    row = (
        await session.execute(
            select(WeeklyReportPost.message_id)
            .where(
                WeeklyReportPost.target_id == int(target_id),
                WeeklyReportPost.week_key == str(week_key),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return REPORT_SLOT_MISSING
    return REPORT_SLOT_DELIVERED if int(row or 0) > 0 else REPORT_SLOT_PLACEHOLDER


async def report_slot_message_id(
    session: AsyncSession, *, target_id: int, week_key: str
) -> int:
    """台账里回填过的 ``message_id``（0 = 占位未回填）；用于 legacy 证据回填。"""

    row = (
        await session.execute(
            select(WeeklyReportPost.message_id)
            .where(
                WeeklyReportPost.target_id == int(target_id),
                WeeklyReportPost.week_key == str(week_key),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


#: 出站证据里的安全文案（不含异常原文/正文）。
WEEKLY_SENT_DETAIL = "weekly report delivered"
#: 出站**之前**写入的保护（R1 返工 v2）：值是保护，不是"已发送"的证明。
WEEKLY_DISPATCH_DETAIL = "weekly report outbound dispatch claim"


async def report_outbound_receipt(
    session: AsyncSession,
    *,
    target_id: int,
    week_key: str,
    business_key: str | None = None,
) -> BusinessReceipt | None:
    """读这条周报的出站证据（``None`` = 没有证据）。

    ``sent`` 与 ``unknown`` 都表示"这一周已经有过真实出站动作"，预览与执行都必须
    据此阻断重发（R1）：令牌泄漏、进程重启、换入口都不能把它绕过去。
    """

    return await load_receipt(
        session,
        task_id=TASK_WEEKLY_REPORT,
        group_id=int(target_id),
        business_key=report_business_key(week_key, business_key=business_key),
    )


async def _record_outbound_quietly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    week_key: str,
    state: str,
    message_id: int = 0,
    detail: str = "",
    business_key: str | None = None,
) -> bool:
    """用**独立事务**补写一条出站证据；失败只记日志（调用方已经按保守结论回报）。"""

    try:
        async with session_factory() as session:
            await record_receipt(
                session,
                task_id=TASK_WEEKLY_REPORT,
                group_id=int(target_id),
                business_key=report_business_key(week_key, business_key=business_key),
                state=state,
                external_message_id=int(message_id or 0),
                detail=detail,
            )
            await session.commit()
        return True
    except Exception:
        log.exception(
            "weekly report outbound receipt record failed | target=%s | week=%s",
            target_id,
            week_key,
        )
        return False


async def _claim_dispatch_quietly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    business_key: str,
    guard: "PersistedGuard | None" = None,
) -> str:
    """出站**之前**在独立事务里抢下这条业务周键的保护（R1 返工 v2）。

    返回 ``claimed``（抢到，可以发）/ ``protected``（已有出站证据或保护：不许发）/
    ``blocked``（R2 返工 v3：授权/发起人/持久配置/取消的**写边界复核**没通过，
    一行都没写、也不许发）/ ``failed``（**保护写不进去**：按保守协议也不许发）。

    这里用 ``claim_business_dispatch`` 而**不是** ``record_receipt``：父探针把
    "发送后记账失败"故障注入在后者名字上，保护必须在那种故障下依然留在库里。

    ``guard`` 非空时复核条件进入**同一句 INSERT…SELECT 的 WHERE**（R2 返工 v3），
    所以"还合法"与"占住出站保护"是同一次原子写入，不在两次 await 之间留窗口。
    """

    try:
        async with session_factory() as session:
            claim = await claim_business_dispatch(
                session,
                task_id=TASK_WEEKLY_REPORT,
                group_id=int(target_id),
                business_key=str(business_key),
                detail=WEEKLY_DISPATCH_DETAIL,
                guard=guard,
            )
            await session.commit()
        if claim is not None:
            return "claimed"
        if guard is not None:
            # 抢不到有两种原因：别人已经拿着这条键的保护，或**写边界复核不通过**
            # （授权被撤、发起人不是管理员了、持久配置变了、run 已被取消）。
            # 复核查询本身出错时按保守协议回 failed（同样不外发）。
            async with session_factory() as session:
                if not await verify_guard(session, guard):
                    return "blocked"
        return "protected"
    except Exception:
        log.exception(
            "weekly report dispatch claim failed | target=%s | key=%s",
            target_id,
            business_key,
        )
        return "failed"


async def _release_dispatch_quietly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    business_key: str,
) -> bool:
    """只有**能证明没有出站**时才撤销保护（``sent``/``unknown`` 永不删）。"""

    try:
        async with session_factory() as session:
            await release_business_dispatch(
                session,
                task_id=TASK_WEEKLY_REPORT,
                group_id=int(target_id),
                business_key=str(business_key),
            )
            await session.commit()
        return True
    except Exception:
        log.exception(
            "weekly report dispatch release failed | target=%s | key=%s",
            target_id,
            business_key,
        )
        return False


@dataclass(frozen=True)
class GroupReportOutcome:
    """一次「给某个目标发本周周报」的结果。

    ``outbound_unknown``：外部结果**未知**（投递异常可能是"其实发出去了、回包丢了"，
    或者"消息发出去了但回填台账失败"）。写侧据此禁止安全重发。
    """

    target_id: int
    week_key: str
    status: str
    reason: str = ""
    message_id: int = 0
    outbound_unknown: bool = False
    error: str = ""


async def _release_quietly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: int,
    week_key: str,
) -> bool:
    """删占位；失败只记日志（占位留着只会让这一周少发一条，不会误发）。"""

    try:
        async with session_factory() as session:
            await release_report_slot(
                session, target_id=int(target_id), week_key=str(week_key)
            )
            await session.commit()
        return True
    except Exception:
        log.exception(
            "weekly report release failed | target=%s | week=%s", target_id, week_key
        )
        return False


async def deliver_group_report(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Any,
    *,
    target_id: int,
    week_key: str,
    chat_id: int | None = None,
    render: Callable[[AsyncSession], Awaitable[str]] | None = None,
    text: str | None = None,
    wrap: Callable[[str], str] | None = None,
    business_key: str | None = None,
    guard: PersistedGuard | None = None,
) -> GroupReportOutcome:
    """给单个目标投递本周周报；返回结构化结果。

    ``render``：``async (session) -> str`` 的真实渲染回调（运营任务中心用它）；
    ``text``：已经渲染好的正文（CLI 的预渲染路径用它，保持既有打印与 dry-run 行为）；
    ``wrap``：可选的正文改写（CLI 把「审核质量 · 近」换成「群健康周报 · 近」）；
    ``chat_id``：真正投递到的会话（不传就是 ``target_id``）——成本摘要在台账上用
    ``target_id=0`` 占位，但消息要私发给超管，所以两者必须能分开；
    ``business_key``：出站证据/保护用的**业务周**键。不传＝台账键就是业务周
    （运营任务中心的口径）；legacy CLI 传 ``previous_iso_week_key(发送周)``，
    这样手动发的 W40 与 CLI 发的 W41 挡的是**同一条**周报（R1 返工 v2）。
    ``guard``：R2 返工 v3 的**写边界**复核（群授权 + 发起人当前权限 + 持久配置
    revision + run 未被取消）。非空时复核条件进入抢占出站保护的**同一句
    INSERT…SELECT**，抢占成功的那一刻就是线性化点；复核不通过或复核查询出错都
    不会发送（回 ``guard_blocked`` / ``dispatch_claim_failed``）。

    顺序是**先占位再渲染、渲染完成后再落出站保护、最后才发送**：渲染失败可以安全
    重试（保护还没落），保护落下之后任何"认不出来的异常"都按"可能已出站"处理。
    """

    target = int(target_id)
    key = str(week_key)
    out_key = report_business_key(key, business_key=business_key)
    chat = target if chat_id is None else int(chat_id)
    async with session_factory() as claim_session:
        # R1：先看出站证据。占位可能因为"发送成功但回填失败"而丢失或被回收，
        # 只凭占位判断就会把同一周再发一遍。
        receipt = await report_outbound_receipt(
            claim_session, target_id=target, week_key=key, business_key=out_key
        )
        claimed = False
        state = REPORT_SLOT_MISSING
        if receipt is None:
            claimed = await claim_report_slot(
                claim_session, target_id=target, week_key=key
            )
            if claimed:
                await claim_session.commit()
            else:
                state = await report_slot_state(
                    claim_session, target_id=target, week_key=key
                )
                if state == REPORT_SLOT_DELIVERED:
                    # legacy 兼容：台账已经回填过 message_id，说明这条台账键的周报
                    # 真的发出去了。按声明的周键映射补一条业务周证据，免得另一个
                    # 入口（手动）看不到它又发一遍。
                    legacy_message_id = await report_slot_message_id(
                        claim_session, target_id=target, week_key=key
                    )
                    await record_receipt(
                        claim_session,
                        task_id=TASK_WEEKLY_REPORT,
                        group_id=target,
                        business_key=out_key,
                        state=RECEIPT_STATE_SENT,
                        external_message_id=legacy_message_id,
                        detail="weekly report delivered (legacy ledger backfill)",
                    )
                    await claim_session.commit()
                    receipt = await report_outbound_receipt(
                        claim_session,
                        target_id=target,
                        week_key=key,
                        business_key=out_key,
                    )
    if receipt is not None:
        confirmed = receipt.state == RECEIPT_STATE_SENT
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=(
                REPORT_OUTCOME_ALREADY_DELIVERED
                if confirmed
                else REPORT_OUTCOME_IN_FLIGHT
            ),
            reason=(
                "outbound_receipt_confirmed"
                if confirmed
                else "outbound_receipt_unknown"
            ),
            message_id=int(receipt.external_message_id or 0),
            outbound_unknown=not confirmed,
        )
    if not claimed:
        if state == REPORT_SLOT_DELIVERED:
            return GroupReportOutcome(
                target_id=target,
                week_key=key,
                status=REPORT_OUTCOME_ALREADY_DELIVERED,
                reason="week_already_delivered",
            )
        # 占位在、message_id 还是 0：可能是别的进程正在发，也可能是上次崩在
        # 发送与回填之间。**两种都不能断言"没发出去"**，所以这里不重发。
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_IN_FLIGHT,
            reason="slot_placeholder_present",
            outbound_unknown=True,
        )

    body = text
    if body is None and render is not None:
        try:
            async with session_factory() as render_session:
                body = await render(render_session)
        except Exception as exc:
            await _release_quietly(session_factory, target_id=target, week_key=key)
            log.warning("weekly report render failed | target=%s | %s", target, exc)
            return GroupReportOutcome(
                target_id=target,
                week_key=key,
                status=REPORT_OUTCOME_RENDER_FAILED,
                reason="render_error",
                error=f"{type(exc).__name__}: {exc}",
            )
    if body is None:
        await _release_quietly(session_factory, target_id=target, week_key=key)
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_RENDER_FAILED,
            reason="no_content",
        )
    if wrap is not None:
        body = wrap(body)

    # R1（返工 v2）：正文已经渲染好，接下来是**唯一会真实出站**的一步。出站前先
    # 在独立事务里落一条"这条业务周正在出站"的保护：进程死在发送与记账之间、
    # 发送后记账失败、取消、认不出来的网络异常，保护都留在库里，任何入口（含
    # legacy CLI 与 reaper）看到保护就不再发第二条。
    dispatch = await _claim_dispatch_quietly(
        session_factory, target_id=target, business_key=out_key, guard=guard
    )
    if dispatch == "blocked":
        # R2（返工 v3）：写边界复核没通过（撤权/换配置/取消）——一行都没写、也没发。
        await _release_quietly(session_factory, target_id=target, week_key=key)
        log.warning("weekly report dispatch blocked by guard | target=%s", target)
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_SEND_FAILED,
            reason="guard_blocked",
            outbound_unknown=False,
        )
    if dispatch == "protected":
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_IN_FLIGHT,
            reason="outbound_dispatch_protected",
            outbound_unknown=True,
        )
    if dispatch == "failed":
        # 保护写不进去就不发送：宁可这一周少发一条，也不发一条没有保护的消息。
        await _release_quietly(session_factory, target_id=target, week_key=key)
        log.warning("weekly report dispatch claim unavailable | target=%s", target)
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_SEND_FAILED,
            reason="dispatch_claim_failed",
            outbound_unknown=False,
        )

    try:
        message = await bot.send_message(chat, body, parse_mode="HTML")
    except Exception as exc:
        if definitely_not_delivered(exc):
            # Telegram 明确拒收（4xx）：能证明没有出站 → 撤销保护 + 删占位重试。
            await _release_dispatch_quietly(
                session_factory, target_id=target, business_key=out_key
            )
            await _release_quietly(session_factory, target_id=target, week_key=key)
            log.warning("weekly report send rejected | target=%s | %s", target, exc)
            return GroupReportOutcome(
                target_id=target,
                week_key=key,
                status=REPORT_OUTCOME_SEND_FAILED,
                reason="send_rejected",
                outbound_unknown=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        # 其余异常一律**不能证明**没有出站：保留保护 + 落 unknown 证据。
        await _record_outbound_quietly(
            session_factory,
            target_id=target,
            week_key=key,
            business_key=out_key,
            state=RECEIPT_STATE_UNKNOWN,
            detail="weekly report send result unknown",
        )
        log.warning("weekly report send result unknown | target=%s | %s", target, exc)
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_SEND_FAILED,
            reason=(
                "send_timeout_unknown"
                if ambiguous_send_failure(exc)
                else "send_result_unknown"
            ),
            outbound_unknown=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    message_id = int(getattr(message, "message_id", 0) or 0)
    try:
        async with session_factory() as mark_session:
            # R1：出站证据与台账回填在**同一个事务**里提交（成功路径只有一个
            # commit）；提交失败时下面立刻用独立事务补写证据。
            await record_receipt(
                mark_session,
                task_id=TASK_WEEKLY_REPORT,
                group_id=target,
                business_key=out_key,
                state=RECEIPT_STATE_SENT,
                external_message_id=message_id,
                detail=WEEKLY_SENT_DETAIL,
            )
            await mark_report_sent(
                mark_session,
                target_id=target,
                week_key=key,
                message_id=message_id,
            )
    except Exception as exc:
        # 消息已经出站：不回退占位（回退等于允许再发一条）；并立刻补一条独立事务
        # 的出站证据，避免占位被回收/被别的入口重算后重发。
        await _record_outbound_quietly(
            session_factory,
            target_id=target,
            week_key=key,
            business_key=out_key,
            state=RECEIPT_STATE_SENT,
            message_id=message_id,
            detail=WEEKLY_SENT_DETAIL,
        )
        log.warning("weekly report mark sent failed | target=%s | %s", target, exc)
        return GroupReportOutcome(
            target_id=target,
            week_key=key,
            status=REPORT_OUTCOME_MARK_FAILED,
            reason="mark_failed",
            message_id=message_id,
            outbound_unknown=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    return GroupReportOutcome(
        target_id=target,
        week_key=key,
        status=REPORT_OUTCOME_DELIVERED,
        reason="delivered",
        message_id=message_id,
    )
