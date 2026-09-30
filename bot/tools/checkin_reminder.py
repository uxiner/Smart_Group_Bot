"""签到提醒：按 ``--slot`` 给每个授权群发一条带「✅ 一键签到」按钮的提醒。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.checkin_reminder --slot 9
    docker exec smart_group_bot-bot-1 python -m bot.tools.checkin_reminder --slot 9 --dry-run

``--slot`` 按**本地（Asia/Shanghai）时段**取值 9/12/15/18。本工具**不判断现在几点**：
要不要发完全由 cron 决定，它只按 ``--slot`` 发。``--dry-run`` 只打印文案与目标群，
不发消息、不写库（不占位、不发奖、不动任何数据）。

cron（VPS 是 UTC，两种任选；``--slot`` 必须按本地时段传，所以每个时段一行）：

    # A. 用 CRON_TZ，触发时间写本地时间
    CRON_TZ=Asia/Shanghai
    0 9  * * * cd /app && python -m bot.tools.checkin_reminder --slot 9
    0 12 * * * cd /app && python -m bot.tools.checkin_reminder --slot 12
    0 15 * * * cd /app && python -m bot.tools.checkin_reminder --slot 15
    0 18 * * * cd /app && python -m bot.tools.checkin_reminder --slot 18

    # B. 不用 CRON_TZ，把触发时间换算成 UTC（9/12/15/18 本地 = 1/4/7/10 UTC）
    0 1  * * * cd /app && python -m bot.tools.checkin_reminder --slot 9
    0 4  * * * cd /app && python -m bot.tools.checkin_reminder --slot 12
    0 7  * * * cd /app && python -m bot.tools.checkin_reminder --slot 15
    0 10 * * * cd /app && python -m bot.tools.checkin_reminder --slot 18

**重复执行安全**：发送前先在 ``checkin_reminder_posts`` 用 (group_id, slot_key) 占位，
``slot_key`` 形如 ``2026-10-01:9``；已有该行就跳过这个群，所以 cron 重试、手动重跑、
两个进程同时跑都不会重复发。单个群发送失败只记日志并释放占位（下次重试还能补发），
不影响其它群。

**自动删除**：提醒发出后排队 **600 秒**后删除。删除调度器是进程内的，所以本工具
会自己起一个 ``TelegramCleanupScheduler`` 把任务写进 ``telegram_delete_jobs``
（持久、重启不丢），真正的删除由常驻机器人进程里的清理 worker 到点执行；本工具
发完就退出，不会等这 10 分钟。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.checkin import local_today
from bot.services.checkin_reminder import (
    REMINDER_AUTO_DELETE_SECONDS,
    REMINDER_SLOTS,
    build_checkin_reminder_keyboard,
    claim_reminder_slot,
    count_checkins_today,
    mark_reminder_sent,
    normalize_slot,
    release_reminder_slot,
    render_checkin_reminder,
    slot_key,
)
from bot.services.quality_report import authorized_group_ids
from bot.services.telegram_cleanup import TelegramCleanupScheduler
from bot.utils.telegram import (
    configure_telegram_cleanup_scheduler,
    schedule_message_auto_delete_durable,
)

log = logging.getLogger(__name__)

_USAGE = (
    "用法：python -m bot.tools.checkin_reminder --slot "
    + "|".join(str(slot) for slot in REMINDER_SLOTS)
    + " [--dry-run]"
)


async def _post(slot: int, *, dry_run: bool = False) -> int:
    """给所有授权群发一条 slot 时段的提醒；返回进程退出码。"""

    settings = Settings()
    engine, session_factory = await init_db(settings.database_url)
    sent = 0
    failed = 0
    try:
        async with session_factory() as session:
            group_ids = await authorized_group_ids(session)
            if not group_ids:
                print("没有授权群，跳过")
                return 0
            key = slot_key(local_today(), slot)
            if dry_run:
                # 只渲染不发送、不占位：验证文案/目标群时用它，别拿真群当试验场
                for group_id in group_ids:
                    checked_in = await count_checkins_today(
                        session, group_id=group_id
                    )
                    print(
                        f"---- dry-run | group={group_id} | slot={slot} | {key} ----"
                    )
                    print(
                        render_checkin_reminder(slot=slot, checked_in=checked_in)
                    )
                return 0

            token = str(
                getattr(settings.bot, "token", "") or settings.bot_token or ""
            ).strip()
            if not token:
                print("没有配置 bot token，无法发送签到提醒")
                return 1

            bot = Bot(token)
            # 本进程是一次性的（cron 拉起、发完就退出），而"10 分钟后删除"必须是
            # **持久任务**：删除调度器是进程内的，不装它 schedule_message_auto_
            # delete_durable 只会记一条 critical 日志、什么都不写。所以这里起一个
            # 调度器把 job 落进 telegram_delete_jobs，由常驻的机器人进程里的清理
            # worker 到点执行（CLI 自己不等这 10 分钟）。
            cleanup = TelegramCleanupScheduler(
                bot=bot, session_factory=session_factory
            )
            configure_telegram_cleanup_scheduler(cleanup)
            cleanup_ready = False
            try:
                try:
                    await cleanup.start()
                    cleanup_ready = True
                except Exception:
                    log.exception("checkin reminder cleanup scheduler failed to start")
                for group_id in group_ids:
                    claimed = False
                    delivered = False
                    try:
                        claimed = await claim_reminder_slot(
                            session, group_id=group_id, key=key
                        )
                        await session.commit()
                        if not claimed:
                            print(f"本时段已发过，跳过 | group={group_id} | {key}")
                            continue
                        checked_in = await count_checkins_today(
                            session, group_id=group_id
                        )
                        sent_message = await bot.send_message(
                            group_id,
                            render_checkin_reminder(slot=slot, checked_in=checked_in),
                            parse_mode="HTML",
                            reply_markup=build_checkin_reminder_keyboard(),
                        )
                        delivered = True
                        await mark_reminder_sent(
                            session,
                            group_id=group_id,
                            key=key,
                            message_id=int(getattr(sent_message, "message_id", 0) or 0),
                        )
                        await session.commit()
                        # 10 分钟后自动删除：写 telegram_delete_jobs，重启不丢任务。
                        # 排不上队只报警，不重发已经发出去的消息。
                        queued = await schedule_message_auto_delete_durable(
                            sent_message, REMINDER_AUTO_DELETE_SECONDS
                        )
                        if not queued:
                            log.error(
                                "checkin reminder auto delete not queued | group=%s",
                                group_id,
                            )
                            print(f"提醒已发出但自动删除未排队 | group={group_id}")
                        sent += 1
                        print(
                            f"已发送提醒 | group={group_id} | {key}"
                            f" | 今日已签到 {checked_in} 人"
                        )
                    except Exception as exc:  # 单群失败不影响其它群
                        await session.rollback()
                        if claimed and not delivered:
                            # 没发出去就不该占着这个时段，撤掉占位让重试能补发
                            try:
                                await release_reminder_slot(
                                    session, group_id=group_id, key=key
                                )
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                log.exception(
                                    "checkin reminder slot release failed | group=%s",
                                    group_id,
                                )
                        failed += 1
                        log.warning(
                            "checkin reminder send failed | group=%s | %s",
                            group_id,
                            exc,
                        )
                        print(f"发送失败 | group={group_id} | {exc}")
            finally:
                # 进程全局指针必须清掉，别把一个已停止的调度器留给后面的调用方
                configure_telegram_cleanup_scheduler(None)
                if cleanup_ready:
                    try:
                        await cleanup.stop()
                    except Exception:
                        log.exception("checkin reminder cleanup scheduler stop failed")
                await bot.session.close()
    finally:
        await engine.dispose()
    if failed:
        print(f"完成：成功 {sent} 个群，失败 {failed} 个群")
        return 1
    print(f"完成：成功 {sent} 个群")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = any(arg in {"--dry-run", "-n"} for arg in args)
    slot: int | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--slot", "-s"} and index + 1 < len(args):
            slot = normalize_slot(args[index + 1])
            if slot is None:
                print(f"--slot 只允许 {'/'.join(str(s) for s in REMINDER_SLOTS)}：{args[index + 1]}")
                return 1
            index += 2
            continue
        index += 1
    if slot is None:
        print(_USAGE)
        return 1
    return asyncio.run(_post(slot, dry_run=dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
