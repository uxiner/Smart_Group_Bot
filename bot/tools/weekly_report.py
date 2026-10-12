"""群健康周报：把上一周的审核质量与活跃度发进每个授权群。

用法（容器内）：

    docker exec smart_group_bot python -m bot.tools.weekly_report [天数] [--dry-run]

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

from aiogram import Bot

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.activity import (
    last_complete_week,
    render_activity_lines,
    settle_weekly_activity,
)
from bot.services.cost_report import render_cost_digest
from bot.services.quality_report import authorized_group_ids, render_group_quality
from bot.utils.timezone import now_shanghai_naive
from bot.services.weekly_report import (
    COST_DIGEST_TARGET_ID,
    REPORT_OUTCOME_ALREADY_DELIVERED,
    REPORT_OUTCOME_DELIVERED,
    REPORT_OUTCOME_IN_FLIGHT,
    claim_report_slot,
    current_week_key,
    deliver_group_report,
    mark_report_sent,
    previous_iso_week_key,
    release_report_slot,
)
log = logging.getLogger(__name__)

DEFAULT_DAYS = 7

#: 这些名字从 ``bot.services.weekly_report`` 转出：投递链（占位/释放/回填/发送）
#: 已经抽到共享服务，CLI 与运营任务中心共用同一份实现。这里保留同名再导出，
#: 既有调用方（含测试）不需要改导入路径。
__all__ = (
    "COST_DIGEST_TARGET_ID",
    "DEFAULT_DAYS",
    "claim_report_slot",
    "current_week_key",
    "mark_report_sent",
    "release_report_slot",
    "deliver_group_report",
    "main",
)


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
    now = now_shanghai_naive()
    week_key = current_week_key(now)
    # R1/R4（返工 v2）：台账键是"发送周"，正文与出站证据都用上一完整业务周。
    # 手动入口（运营任务中心）传的就是业务周，CLI 这里显式映射到同一个业务周键，
    # 两个入口互相挡得住，不会同一周发两遍。
    business_key = previous_iso_week_key(week_key)
    week_start, week_end = last_complete_week(now)
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
                    main_text = await render_group_quality(
                        session,
                        group_id=group_id,
                        days=days,
                        activity_lines=boards[group_id],
                        # R4：主统计＝上一完整业务周的精确边界。
                        week_start=week_start,
                        week_end=week_end,
                    )
                    # 旧 `--days` 滚动口径保留，但降级为**明确注明**的附加段，
                    # 不允许再冒充"完整周"。
                    rolling_text = await render_group_quality(
                        session,
                        group_id=group_id,
                        days=days,
                        activity_lines=None,
                    )
                    texts[group_id] = (
                        f"{main_text}\n\n"
                        f"<b>附：滚动近 {days} 天口径（仅供参考，不是完整自然周）</b>\n"
                        f"{rolling_text}"
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
                print(texts[group_id].replace("审核质量 ·", "群健康周报 ·", 1))
            for group_id in sorted(failed_groups):
                print(f"---- dry-run | group={group_id} ----")
                print("（本群渲染失败，已跳过；详见日志）")
            print("---- dry-run | 成本摘要（私发超管）----")
            print(cost_text)
            return 0
        bot = Bot(token)
        try:
            for group_id in deliverable:
                # B-26：先占位再发。重叠运行 / 手工重跑都不会让同一个群在同一周
                # 收到两条周报。投递链与运营任务中心共用 deliver_group_report。
                outcome = await deliver_group_report(
                    session_factory,
                    bot,
                    target_id=int(group_id),
                    week_key=week_key,
                    business_key=business_key,
                    text=texts[group_id],
                    wrap=lambda body: body.replace("审核质量 ·", "群健康周报 ·", 1),
                )
                if outcome.status in (
                    REPORT_OUTCOME_ALREADY_DELIVERED,
                    REPORT_OUTCOME_IN_FLIGHT,
                ):
                    print(f"本周已发过，跳过 | group={group_id} | week={week_key}")
                    continue
                if outcome.status == REPORT_OUTCOME_DELIVERED:
                    sent += 1
                    print(f"已发送周报 | group={group_id}")
                    continue
                failed += 1
                log.warning(
                    "weekly report send failed | group=%s | %s",
                    group_id,
                    outcome.error,
                )
                print(f"发送失败 | group={group_id} | {outcome.error}")
            admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
            if admin_id > 0:
                # 成本摘要同样幂等（B-26：以前是无条件私发）。
                digest = await deliver_group_report(
                    session_factory,
                    bot,
                    target_id=COST_DIGEST_TARGET_ID,
                    week_key=week_key,
                    business_key=business_key,
                    chat_id=admin_id,
                    text=cost_text,
                )
                if digest.status == REPORT_OUTCOME_DELIVERED:
                    print(f"已私发成本摘要 | admin={admin_id}")
                elif digest.status in (
                    REPORT_OUTCOME_ALREADY_DELIVERED,
                    REPORT_OUTCOME_IN_FLIGHT,
                ):
                    print(f"本周已私发过成本摘要，跳过 | week={week_key}")
                else:
                    failed += 1
                    log.warning(
                        "weekly cost digest failed | admin=%s | %s",
                        admin_id,
                        digest.error,
                    )
                    print(f"成本摘要私发失败 | {digest.error}")
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
