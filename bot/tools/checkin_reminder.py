"""签到提醒：按 ``--slot`` 给每个授权群发一条带按钮的提醒。

按钮一行两个：「✅ 一键签到」（callback，固定常量 ``checkin:v1``）｜
「🛒 积分商店」（**URL 深链** ``https://t.me/<bot>?start=shop_<群号>``，把商店菜单引到
私聊，不在群里刷屏；``<bot>`` 运行时用 ``get_me()`` 取，取不到就只留签到按钮）。
正文里带「今日已签到 N 人」和按签到先后排好的已签到名单（最多 20 个昵称，
超出折成「…等 N 人」）。

用法（容器内）：

    docker compose exec bot python -m bot.tools.checkin_reminder --slot 9
    docker compose exec bot python -m bot.tools.checkin_reminder --slot 9 --dry-run

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

**B13b/C4 按群执行来源**：签到提醒的默认来源是 ``external``（就是上面的 cron），
但管理员可以在 Mini App 的运营区把**某个群**预览并确认切到 ``internal``——那之后
这个群由进程内 runner 负责，本 CLI **不再处理它**（只处理仍是 external 的群；一个
都没有就直接退出 0）。切换只锚定未来：``active_from`` 之前的时段不会被补发，内外
两侧共用同一套占位与出站回执，不会各发一条。**准入不是一次预检查**：本 CLI 用读
来源那一刻的策略版本构造持久守卫，并把它编进真实占位语句（``INSERT ... WHERE``）；
管理员在"读了来源"与"真正占位"之间切换时，这条语句自己会拒绝，本入口一个字都不发
（见 ``bot/services/operations_guard.py`` 的 ``for_automatic_entry``）。

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
from bot.services.runtime_config import RuntimeConfigManager
from bot.services import task_policy
from bot.services.operations_guard import PersistedGuard
from bot.services.checkin import local_today
from bot.services.task_policy import slot_moment
from bot.services.checkin_reminder import (
    AUTO_DELETE_BACKFILLED,
    AUTO_DELETE_UNCONFIRMED,
    CHECKIN_OUTCOME_ALREADY_DELIVERED,
    CHECKIN_OUTCOME_MARK_FAILED,
    CHECKIN_OUTCOME_SEND_FAILED,
    CHECKIN_OUTCOME_SENT,
    backfill_durable_auto_delete,
    normalize_slot,
    reap_stale_reminder_slots,
    reminder_auto_delete_seconds,
    reminder_slots,
    render_checkin_reminder,
    send_checkin_slot,
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

log = logging.getLogger(__name__)

_USAGE = (
    "用法：python -m bot.tools.checkin_reminder --slot "
    + "|".join(str(slot) for slot in reminder_slots())
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


async def external_slot_bindings(
    session, *, slot: int, day
) -> tuple[list[PersistedGuard], list[int]]:
    """B13b/C4：这个时段该由外部入口处理的群，以及**该次判定的准入守卫**。

    返回 ``(应处理的群及其守卫, 因切换生效晚于该时段而跳过的群)``。默认所有群归
    进程内 runner（``internal``），只有管理员显式切到 ``external`` 的群才归这里；
    两侧共用同一个 ``active_from`` 与同一套占位/出站回执，谁都不补切换之前的旧时段。

    B13b 返工（R-B1）：守卫**必须由这次判定所依据的同一份策略视图**构造，并且
    交给 ``send_checkin_slot`` 编进真实占位语句。不能在发送前重新读一次策略来造
    守卫：管理员刚刚把这个群切回 ``internal`` 时，新读到的 revision 已经 +1，用它
    造守卫反而会放行，等于旧来源继续执行新效果。
    """

    group_ids = await authorized_group_ids(session)
    if not group_ids:
        return [], []
    views = await task_policy.load_task_policies(
        session, task_id="checkin_reminder", group_ids=group_ids
    )
    moment = slot_moment(day, slot)
    guards: list[PersistedGuard] = []
    skipped: list[int] = []
    for group_id in group_ids:
        view = views[group_id]
        if view.source != task_policy.SOURCE_EXTERNAL:
            continue
        if view.active_from is not None and moment < view.active_from:
            skipped.append(group_id)
            continue
        guards.append(
            PersistedGuard.for_automatic_entry(
                group_id=group_id,
                task_id="checkin_reminder",
                policy_revision=int(view.revision),
            )
        )
    return guards, skipped


async def external_slot_scope(
    session, *, slot: int, day
) -> tuple[list[int], list[int]]:
    """只读兼容视图：返回 ``(应处理的群, 跳过的群)``。

    真正的执行入口用 :func:`external_slot_bindings`（要拿守卫）；这里只是群列表的
    便捷读法，本身不做出站决策。
    """

    guards, skipped = await external_slot_bindings(session, slot=slot, day=day)
    return [int(guard.group_id) for guard in guards], skipped


async def _post(slot: int, *, dry_run: bool = False) -> int:
    """给所有授权群发一条 slot 时段的提醒；返回进程退出码。"""

    settings = Settings()
    engine, session_factory = await init_db(settings.database_url)
    # 提醒时段、自动删除秒数、按钮文案都是运行时配置，**必须**走现有的
    # RuntimeConfigManager 初始化再读——不能直接吃 schema 默认值，否则改了
    # Mini App 里的 checkin_reminder.* 之后 CLI 仍按旧值发。
    runtime_config = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
    )
    await runtime_config.initialize()
    sent = 0
    failed = 0
    try:
        async with session_factory() as session:
            group_ids = await authorized_group_ids(session)
            if not group_ids:
                print("没有授权群，跳过")
                return 0
            if slot not in reminder_slots():
                # 运行时配置可能在进程启动之后被改过：这里再按生效值确认一次。
                print(
                    f"--slot {slot} 不在当前生效的提醒时段里："
                    f"{'/'.join(str(s) for s in reminder_slots())}"
                )
                return 1
            # B13b/C4：这个 CLI 就是**外部执行来源**，也是签到提醒的**默认来源**
            # （`task_policy.DEFAULT_SOURCE["checkin_reminder"] == "external"`，现场
            # cron 在 UTC 1/4/7/10 = 上海 9/12/15/18 触发它）。只有管理员在 Mini App
            # 里把某些群显式切到 `internal` 之后，**那些群**才不再由本入口处理。
            external, skipped_stale = await external_slot_bindings(
                session, slot=slot, day=local_today()
            )
            if not external and not skipped_stale:
                print(
                    "本时段没有归外部入口处理的群（签到默认由本 CLI 执行；"
                    "这些群已被管理员切到进程内 runner）：这个入口什么都不做。"
                )
                return 0
            if skipped_stale:
                print(
                    f"已跳过 {len(skipped_stale)} 个刚切到外部执行的群："
                    f"{slot}:00 这个时段在切换生效之前，不补发。"
                )
            if not external:
                print("没有需要外部执行的群（下一个时段之前无事可做）。")
                return 0
            key = slot_key(local_today(), slot)
            if dry_run:
                # 只渲染不发送、不占位：验证文案/目标群时用它，别拿真群当试验场
                for guard in external:
                    group_id = int(guard.group_id)
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
                for guard in external:
                    group_id = int(guard.group_id)
                    # 发送链（占位 → 发送 → 回填 → 排自动删除）与运营任务中心
                    # 共用同一个服务原语：任何一侧都不会绕过占位幂等。
                    outcome = await send_checkin_slot(
                        session,
                        bot,
                        group_id=group_id,
                        slot=slot,
                        auto_delete_seconds=reminder_auto_delete_seconds(),
                        queue_auto_delete=schedule_message_auto_delete_durable,
                        backfill_auto_delete=backfill_durable_auto_delete,
                        session_factory=session_factory,
                        bot_username=bot_username,
                        # R-B1：把这次判定的准入守卫编进真实占位语句。管理员在
                        # "本 CLI 读了来源"与"真正占位"之间把这个群切回 internal
                        # 时，占位语句会拒绝插入 ⇒ 本入口一个字都不发。
                        guard=guard,
                    )
                    if (
                        outcome.status == CHECKIN_OUTCOME_SEND_FAILED
                        and str(outcome.reason) == "guard_blocked"
                    ):
                        # 来源已被管理员切换/撤权：这一期不归本入口了（进程内
                        # runner 或下一时段接手）。不算失败，也不重试补发。
                        print(
                            f"来源已切换，本群不再由本入口处理 | "
                            f"group={group_id} | {key}"
                        )
                        continue
                    if outcome.status == CHECKIN_OUTCOME_ALREADY_DELIVERED:
                        print(f"本时段已发过，跳过 | group={group_id} | {key}")
                        continue
                    if outcome.status == CHECKIN_OUTCOME_SENT:
                        if outcome.auto_delete == AUTO_DELETE_UNCONFIRMED:
                            print(
                                f"提醒已发出但自动删除既没排队也没补写 | group={group_id}"
                            )
                        elif outcome.auto_delete == AUTO_DELETE_BACKFILLED:
                            print(
                                f"提醒已发出，自动删除已直接补写持久表 | group={group_id}"
                            )
                        sent += 1
                        print(
                            f"已发送提醒 | group={group_id} | {key}"
                            f" | 今日已签到 {outcome.checked_in} 人"
                        )
                        continue
                    # send_failed / mark_failed：都算失败（退出码 1）。mark_failed
                    # 说明消息**已经出站**，只是台账回填失败——绝不重发、也不撤占位。
                    failed += 1
                    if outcome.status == CHECKIN_OUTCOME_MARK_FAILED:
                        print(
                            f"发送失败 | group={group_id} | 台账回填失败"
                            f"（消息可能已经发出）| {outcome.error}"
                        )
                    else:
                        print(f"发送失败 | group={group_id} | {outcome.error}")
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
                print(f"--slot 只允许 {'/'.join(str(s) for s in reminder_slots())}：{args[index + 1]}")
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
