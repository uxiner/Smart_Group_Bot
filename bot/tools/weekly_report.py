"""群健康周报：把上一周的审核质量与活跃度发进每个授权群。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.weekly_report [天数] [--dry-run]

由定时任务调用。发不出去只打印错误，不影响机器人本体。

发送之前先做**上周活跃激励结算**（``settle_weekly_activity``）：算分、发奖、把
"上周活跃榜"拼进周报正文。结算按 (用户, ISO 周) 幂等，重复跑不会重复加分；
``--dry-run`` 连积分也不动，只验证文案。

**发送本身也幂等**（B-26）：``weekly_report_posts`` 表上的 ``(target_id, week_key)``
唯一索引 + ``INSERT ... ON CONFLICT DO NOTHING + RETURNING``。cron 与上一次运行重叠、
手工补跑、手动重跑都只发一条；发失败会把占位删掉（可以重发）。私发超管的成本摘要
用 ``target_id=0`` 走同一套，不再无条件每周发一次。
"""

from __future__ import annotations

import asyncio
import sys

import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import WeeklyReportPost
from bot.services.activity import render_activity_lines, settle_weekly_activity
from bot.services.cost_report import render_cost_digest
from bot.services.quality_report import authorized_group_ids, render_group_quality
from bot.utils.timezone import now_shanghai_naive
log = logging.getLogger(__name__)

DEFAULT_DAYS = 7

#: 私发超管的成本摘要用的 ``target_id``（0 不是合法群 id，不会和群冲突）。
COST_DIGEST_TARGET_ID = 0


def current_week_key(now: datetime | None = None) -> str:
    """本次运行所属的 ISO 周（``2026-W41``）——周报幂等键的一半。"""

    stamp = now or now_shanghai_naive()
    iso = stamp.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def claim_report_slot(
    session,
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


async def release_report_slot(session, *, target_id: int, week_key: str) -> None:
    """发送失败：删掉占位，下一轮还能重发。"""

    await session.execute(
        delete(WeeklyReportPost).where(
            WeeklyReportPost.target_id == int(target_id),
            WeeklyReportPost.week_key == str(week_key),
        )
    )


async def mark_report_sent(
    session, *, target_id: int, week_key: str, message_id: int
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


async def _send_reports(days: int, *, dry_run: bool = False) -> int:
    settings = Settings()
    token = str(getattr(settings.bot, "token", "") or settings.bot_token or "").strip()
    if not token:
        print("没有配置 bot token，无法发送周报")
        return 1
    engine, session_factory = await init_db(settings.database_url)
    sent = 0
    #: 真正**发送失败**的次数。幂等跳过的目标不算失败——否则一次正常的手工重跑
    #: 会因为"什么都没发"而返回 1，把 cron 的告警语义弄脏。
    failed = 0
    week_key = current_week_key()
    try:
        async with session_factory() as session:
            group_ids = await authorized_group_ids(session)
            texts: dict[int, str] = {}
            boards: dict[int, list[str]] = {}
            failed_groups: set[int] = set()
            for group_id in group_ids:
                # 结算与发奖必须发生在渲染之前：榜单要显示本周实际到账的分。
                # --dry-run 只渲染不发（连积分也不动），验证文案时不会改任何人的钱包。
                try:
                    result = await settle_weekly_activity(
                        session, group_id=group_id, award=not dry_run
                    )
                    if not dry_run:
                        await session.commit()
                    boards[group_id] = render_activity_lines(result)
                except Exception:  # 单群结算失败不影响其它群，也不影响内容块
                    await session.rollback()
                    log.exception("weekly activity settle failed | group=%s", group_id)
                    boards[group_id] = []
                # F-026：渲染块过去**没有** try/except，于是任何一个群渲染失败就
                # 带着整个循环（以及所有群）一起炸掉，所有群都收不到周报。
                # 与上面的结算块对齐：单群失败只跳过该群并如实记录。
                try:
                    texts[group_id] = await render_group_quality(
                        session,
                        group_id=group_id,
                        days=days,
                        activity_lines=boards[group_id],
                    )
                except Exception:
                    await session.rollback()
                    log.exception("weekly quality render failed | group=%s", group_id)
                    failed_groups.add(group_id)
            cost_text = await render_cost_digest(session, days=days)
        if not group_ids:
            print("没有授权群，跳过")
            return 0
        deliverable = [gid for gid in group_ids if gid not in failed_groups]
        if dry_run:
            # 只渲染不发送：验证文案/HTML 正确性时用它，别拿真群当试验场
            for group_id in deliverable:
                print(f"---- dry-run | group={group_id} ----")
                print(texts[group_id].replace("审核质量 · 近", "群健康周报 · 近", 1))
            for group_id in sorted(failed_groups):
                print(f"---- dry-run | group={group_id} ----")
                print("（本群渲染失败，已跳过；详见日志）")
            print("---- dry-run | 成本摘要（私发超管）----")
            print(cost_text)
            return 0
        bot = Bot(token)
        try:
            for group_id in deliverable:
                text = texts[group_id].replace(
                    "审核质量 · 近", "群健康周报 · 近", 1
                )
                # B-26：先占位再发。重叠运行 / 手工重跑都不会让同一个群在同一周
                # 收到两条周报。
                async with session_factory() as claim_session:
                    claimed = await claim_report_slot(
                        claim_session, target_id=int(group_id), week_key=week_key
                    )
                    if claimed:
                        await claim_session.commit()
                if not claimed:
                    print(f"本周已发过，跳过 | group={group_id} | week={week_key}")
                    continue
                try:
                    message = await bot.send_message(group_id, text, parse_mode="HTML")
                    sent += 1
                    print(f"已发送周报 | group={group_id}")
                except Exception as exc:  # 单个群失败不影响其它群
                    failed += 1
                    log.warning("weekly report send failed | group=%s | %s", group_id, exc)
                    print(f"发送失败 | group={group_id} | {exc}")
                    # 发失败就撤掉占位，"没发出去"不能被记成"已发过"。
                    async with session_factory() as release_session:
                        await release_report_slot(
                            release_session,
                            target_id=int(group_id),
                            week_key=week_key,
                        )
                        await release_session.commit()
                    continue
                async with session_factory() as mark_session:
                    await mark_report_sent(
                        mark_session,
                        target_id=int(group_id),
                        week_key=week_key,
                        message_id=int(getattr(message, "message_id", 0) or 0),
                    )
            admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
            if admin_id > 0:
                # 成本摘要同样幂等（B-26：以前是无条件私发）。
                async with session_factory() as claim_session:
                    claimed = await claim_report_slot(
                        claim_session,
                        target_id=COST_DIGEST_TARGET_ID,
                        week_key=week_key,
                    )
                    if claimed:
                        await claim_session.commit()
                if claimed:
                    try:
                        message = await bot.send_message(admin_id, cost_text)
                        print(f"已私发成本摘要 | admin={admin_id}")
                        async with session_factory() as mark_session:
                            await mark_report_sent(
                                mark_session,
                                target_id=COST_DIGEST_TARGET_ID,
                                week_key=week_key,
                                message_id=int(getattr(message, "message_id", 0) or 0),
                            )
                    except Exception as exc:
                        failed += 1
                        log.warning("weekly cost digest failed | admin=%s | %s", admin_id, exc)
                        print(f"成本摘要私发失败 | {exc}")
                        async with session_factory() as release_session:
                            await release_report_slot(
                                release_session,
                                target_id=COST_DIGEST_TARGET_ID,
                                week_key=week_key,
                            )
                            await release_session.commit()
                else:
                    print(f"本周已私发过成本摘要，跳过 | week={week_key}")
            else:
                print("未配置最高管理员，跳过成本摘要")
        finally:
            await bot.session.close()
    finally:
        await engine.dispose()
    #: 退出码语义（B-26 与 F-026 合并后的口径）：
    #: - 只因为「本周已发过」而跳过的目标**不算**失败（cron 重叠 / 手工重跑不该告警）；
    #: - 真发失败（failed）算失败；
    #: - 有群连报表都渲染不出来（failed_groups）且这次**一份都没发出去**才算失败；
    #:   部分群投递成功时返回 0，避免把「个别群炸了」变成每周一次的告警噪音。
    return 1 if failed or (failed_groups and sent == 0) else 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = any(arg in {"--dry-run", "-n"} for arg in args)
    numbers = [arg for arg in args if not arg.startswith("-")]
    days = DEFAULT_DAYS
    if numbers:
        try:
            days = max(1, min(90, int(numbers[0])))
        except ValueError:
            days = DEFAULT_DAYS
    return asyncio.run(_send_reports(days, dry_run=dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
