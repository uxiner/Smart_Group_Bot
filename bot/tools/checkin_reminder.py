"""签到提醒：按 ``--slot`` 给每个授权群发一条带按钮的提醒。

按钮一行两个：「✅ 一键签到」（callback，固定常量 ``checkin:v1``）｜
「🛒 积分商店」（**URL 深链** ``https://t.me/<bot>?start=shop_<群号>``，把商店菜单引到
私聊，不在群里刷屏；``<bot>`` 运行时用 ``get_me()`` 取，取不到就只留签到按钮）。
正文里带「今日已签到 N 人」和按签到先后排好的已签到名单（最多 20 个昵称，
超出折成「…等 N 人」）。

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
from datetime import timedelta

from aiogram import Bot

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.checkin import local_today
from bot.services.checkin_reminder import (
    REMINDER_AUTO_DELETE_SECONDS,
    REMINDER_SLOTS,
    backfill_durable_auto_delete,
    build_checkin_reminder_keyboard,
    claim_reminder_slot,
    mark_reminder_sent,
    normalize_slot,
    reap_stale_reminder_slots,
    release_reminder_slot,
    render_checkin_reminder,
    shop_start_payload,
    slot_key,
    today_checkin_roster,
)
from bot.services.quality_report import authorized_group_ids
from bot.services.telegram_cleanup import TelegramCleanupScheduler
from bot.utils.telegram import (
    configure_telegram_cleanup_scheduler,
    schedule_message_auto_delete_durable,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_USAGE = (
    "用法：python -m bot.tools.checkin_reminder --slot "
    + "|".join(str(slot) for slot in REMINDER_SLOTS)
    + " [--dry-run]"
)


async def _bot_username(bot: object) -> str:
    """机器人的 @username（商店深链要用）；取不到就返回空串。

    失败只记日志、不抛：少一个商店按钮是小事，让整条签到提醒发不出去是大事。
    """

    getter = getattr(bot, "get_me", None)
    if not callable(getter):
        return ""
    try:
        me = await getter()
    except Exception:
        log.warning(
            "checkin reminder get_me failed; shop button omitted", exc_info=True
        )
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


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
                    roster = await today_checkin_roster(session, group_id=group_id)
                    print(
                        f"---- dry-run | group={group_id} | slot={slot} | {key} ----"
                    )
                    print(
                        render_checkin_reminder(
                            slot=slot, checked_in=roster.count, names=roster.names
                        )
                    )
                    print(
                        f"（商店按钮深链 payload：{shop_start_payload(group_id)}；"
                        "运行时用 get_me() 的 @username 拼成 t.me 链接）"
                    )
                return 0

            token = str(
                getattr(settings.bot, "token", "") or settings.bot_token or ""
            ).strip()
            if not token:
                print("没有配置 bot token，无法发送签到提醒")
                return 1

            bot = Bot(token)
            # 「🛒 积分商店」按钮是私聊深链，需要机器人的 @username。**运行时取**
            # （不硬编码）：取不到（网络抖动 / 机器人没设 username）就退化成只有
            # 一个签到按钮，绝不因为少个用户名让整条提醒发不出去。
            bot_username = await _bot_username(bot)
            if not bot_username:
                print("取不到机器人用户名，本次提醒不含商店按钮")
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
                # B-27：上一轮进程若在 claim 与 send 之间被 SIGKILL / OOM / 容器驱逐，
                # 库里会留下一行 message_id=0 的空占位，让该时段**永远**不再发、
                # 且没有补发路径。先把超宽限期的空占位清掉，下面的 claim 就能正常
                # 认领并补发。宽限期远大于一次「claim → 读名单 → 发消息」的耗时。
                released = await reap_stale_reminder_slots(session)
                if released:
                    print(f"已清理 {len(released)} 个未送达的空占位，本轮将补发")
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
                        roster = await today_checkin_roster(
                            session, group_id=group_id
                        )
                        sent_message = await bot.send_message(
                            group_id,
                            render_checkin_reminder(
                                slot=slot,
                                checked_in=roster.count,
                                names=roster.names,
                            ),
                            parse_mode="HTML",
                            reply_markup=build_checkin_reminder_keyboard(
                                bot_username=bot_username, group_id=group_id
                            ),
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
                            # B-27：调度器不健康时那条「10 分钟后自动删除」会留在群里
                            # 永远不掉。直接往同一张持久表补一行，由常驻进程的清理
                            # worker 到点执行（绝不重发已经发出去的消息）。
                            backfilled = await backfill_durable_auto_delete(
                                session_factory,
                                chat_id=int(group_id),
                                message_id=int(
                                    getattr(sent_message, "message_id", 0) or 0
                                ),
                                due_at=now_shanghai_naive()
                                + timedelta(seconds=REMINDER_AUTO_DELETE_SECONDS),
                            )
                            if not backfilled:
                                print(
                                    f"提醒已发出但自动删除既没排队也没补写 | group={group_id}"
                                )
                            else:
                                print(
                                    f"提醒已发出，自动删除已直接补写持久表 | group={group_id}"
                                )
                        sent += 1
                        print(
                            f"已发送提醒 | group={group_id} | {key}"
                            f" | 今日已签到 {roster.count} 人"
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
