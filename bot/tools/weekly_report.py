"""群健康周报：把上一周的审核质量与活跃度发进每个授权群。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.weekly_report [天数] [--dry-run]

由定时任务调用。发不出去只打印错误，不影响机器人本体。

发送之前先做**上周活跃激励结算**（``settle_weekly_activity``）：算分、发奖、把
"上周活跃榜"拼进周报正文。结算按 (用户, ISO 周) 幂等，重复跑不会重复加分；
``--dry-run`` 连积分也不动，只验证文案。
"""

from __future__ import annotations

import asyncio
import sys

import logging

from aiogram import Bot

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.activity import render_activity_lines, settle_weekly_activity
from bot.services.cost_report import render_cost_digest
from bot.services.quality_report import authorized_group_ids, render_group_quality
log = logging.getLogger(__name__)

DEFAULT_DAYS = 7


async def _send_reports(days: int, *, dry_run: bool = False) -> int:
    settings = Settings()
    token = str(getattr(settings.bot, "token", "") or settings.bot_token or "").strip()
    if not token:
        print("没有配置 bot token，无法发送周报")
        return 1
    engine, session_factory = await init_db(settings.database_url)
    sent = 0
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
                try:
                    await bot.send_message(group_id, text, parse_mode="HTML")
                    sent += 1
                    print(f"已发送周报 | group={group_id}")
                except Exception as exc:  # 单个群失败不影响其它群
                    log.warning("weekly report send failed | group=%s | %s", group_id, exc)
                    print(f"发送失败 | group={group_id} | {exc}")
            admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
            if admin_id > 0:
                try:
                    await bot.send_message(admin_id, cost_text)
                    print(f"已私发成本摘要 | admin={admin_id}")
                except Exception as exc:
                    log.warning("weekly cost digest failed | admin=%s | %s", admin_id, exc)
                    print(f"成本摘要私发失败 | {exc}")
            else:
                print("未配置最高管理员，跳过成本摘要")
        finally:
            await bot.session.close()
    finally:
        await engine.dispose()
    return 0 if sent else 1


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
