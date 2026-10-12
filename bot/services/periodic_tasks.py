"""进程内周期任务 runner（B13b / C4 第二子批）。

只有**按群执行来源 = internal** 的群才由这里的 runner 负责：签到提醒与周报周期
计划。三条口径：

1. **每轮现读持久策略**（``bot/services/task_policy.py``）——管理员在 Mini App 里
   预览并确认来源切换或开启周计划之后**下一个 tick 就生效**，不需要重启进程，也
   不需要通知已经跑着的 worker 重建计划。
2. **切换只锚定未来。** 只用 ``active_from`` 之后的时段（:func:`due_slots`）；开启
   周计划的锚点是**严格未来**的 ``next_occurrence``。所以切换生效后只可能发"下一
   个时段"，不会补旧时段，也不会因为发布/确认立刻补上一周。
3. **与外部 CLI 共用同一套幂等与回执。** 两条链读同一处来源选择，并且共用
   ``checkin_reminder_posts`` 的 ``(group_id, slot_key)`` 占位与
   ``operations_business_receipts`` 的出站保护回执：任何一侧发过，另一侧都不会
   再发第二条。

签到 runner **默认什么都不做**：签到提醒的默认来源是 ``external``（现场 cron 在
UTC 1/4/7/10 = 上海 9/12/15/18 触发 CLI）。只有管理员把某个群切到 ``internal``
之后，本模块才接管那个群。

周报周期计划默认 ``off``：没有 ``plan_enabled`` 的行时 runner 一个 run 都不建。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services import task_policy
from bot.services.background_health import record_background_failure
from bot.services.operations_guard import PersistedGuard
from bot.services.checkin_reminder import (
    CHECKIN_OUTCOME_ALREADY_DELIVERED,
    CHECKIN_OUTCOME_MARK_FAILED,
    CHECKIN_OUTCOME_SENT,
    backfill_durable_auto_delete,
    reap_stale_reminder_slots,
    reminder_auto_delete_seconds,
    reminder_slots,
    send_checkin_slot,
)
from bot.services.quality_report import authorized_group_ids
from bot.utils.telegram import schedule_message_auto_delete_durable
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

#: 后台任务名（日志与状态里显示的就是它）。
CHECKIN_RUNNER_NAME = "checkin_reminder_internal"
WEEKLY_PLAN_RUNNER_NAME = "weekly_report_internal_plan"

#: 轮询节奏。签到每 60 秒看一次「当前时段是否到点」，周计划每 30 秒看一次锚点；
#: 真正发不发由持久策略与占位/回执决定，节奏只影响发现延迟。
CHECKIN_TICK_SECONDS = 60.0
PLAN_TICK_SECONDS = 30.0

#: 周报周期计划使用的参数（与运营中心人工确认时一致）。
WEEKLY_PLAN_PARAMS: dict[str, Any] = {"days": 7}


async def _bot_username(bot: object) -> str:
    """机器人的 @username（商店深链要用）；取不到返回空串，绝不因此不发提醒。"""

    getter = getattr(bot, "get_me", None)
    if not callable(getter):
        return ""
    try:
        me = await getter()
    except Exception:
        log.warning("periodic runner get_me failed; shop button omitted", exc_info=True)
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


class CheckinInternalRunner:
    """签到提醒的内部执行来源（每轮现读策略，只处理 internal 的群）。"""

    def __init__(
        self,
        *,
        bot: object,
        session_factory: async_sessionmaker[AsyncSession],
        tick_seconds: float | None = None,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.tick_seconds = max(
            5.0,
            float(CHECKIN_TICK_SECONDS if tick_seconds is None else tick_seconds),
        )
        self._username = ""
        self._stopped = False

    def stop(self) -> None:
        """请求退出循环（进程关停时由 supervisor 取消任务，这里是兜底）。"""

        self._stopped = True

    async def run_once(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """跑一轮：返回本轮的 (group_id, slot, status) 记录，供测试/日志核对。"""

        moment = now if isinstance(now, datetime) else now_shanghai_naive()
        slots = reminder_slots()
        results: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            group_ids = [int(value) for value in await authorized_group_ids(session)]
            if not group_ids:
                return []
            views = await task_policy.load_task_policies(
                session, task_id="checkin_reminder", group_ids=group_ids
            )
            internal = [
                group_id
                for group_id in group_ids
                if views[group_id].source == task_policy.SOURCE_INTERNAL
            ]
            if not internal:
                return []
            # 与 CLI/运营中心同一套占位清理：进程死在 claim 与 send 之间留下的空
            # 占位要腾出来（有出站回执的时段绝不回收）。
            released = await reap_stale_reminder_slots(session)
            if released:
                log.info(
                    "checkin internal runner released %d stale placeholder(s)",
                    len(released),
                )
            username = self._username or await _bot_username(self.bot)
            self._username = username
            for group_id in internal:
                view = views[group_id]
                # B13b 返工（R-B1）：把"本群签到来源仍是 internal、且还是这一版
                # 策略"编进真实出站边界（占位 claim / dispatch claim）的 WHERE。
                # 这不是预检查：管理员在本轮读了策略之后切换来源，谓词会在持有
                # writer 锁的那条语句里失效，于是这条提醒根本不会出站。
                guard = PersistedGuard.for_automatic_entry(
                    group_id=group_id,
                    task_id="checkin_reminder",
                    policy_revision=int(view.revision),
                )
                due = task_policy.due_slots(
                    now=moment,
                    slots=slots,
                    active_from=view.active_from or moment,
                )
                for slot in due:
                    try:
                        outcome = await send_checkin_slot(
                            session,
                            self.bot,
                            group_id=group_id,
                            slot=slot,
                            auto_delete_seconds=reminder_auto_delete_seconds(),
                            queue_auto_delete=schedule_message_auto_delete_durable,
                            backfill_auto_delete=backfill_durable_auto_delete,
                            session_factory=self.session_factory,
                            bot_username=username,
                            # 常驻 runner 与运营中心一样是"发完还要记账"的路径：
                            # 出站前先抢持久 dispatching 保护，进程死在中间也不会
                            # 让别的入口再发一条。
                            protect_outbound=True,
                            guard=guard,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.exception(
                            "checkin internal send failed | group=%s slot=%s",
                            group_id,
                            slot,
                        )
                        await session.rollback()
                        results.append(
                            {
                                "group_id": group_id,
                                "slot": int(slot),
                                "status": "error",
                                "reason": type(exc).__name__,
                            }
                        )
                        continue
                    results.append(
                        {
                            "group_id": group_id,
                            "slot": int(slot),
                            "status": str(outcome.status),
                            "reason": str(getattr(outcome, "reason", "") or ""),
                            "message_id": int(
                                getattr(outcome, "message_id", 0) or 0
                            ),
                        }
                    )
                    if outcome.status == CHECKIN_OUTCOME_SENT:
                        log.info(
                            "checkin internal sent | group=%s slot=%s", group_id, slot
                        )
                    elif outcome.status == CHECKIN_OUTCOME_ALREADY_DELIVERED:
                        log.info(
                            "checkin internal skipped (already delivered) | "
                            "group=%s slot=%s reason=%s",
                            group_id,
                            slot,
                            getattr(outcome, "reason", ""),
                        )
                    elif outcome.status == CHECKIN_OUTCOME_MARK_FAILED:
                        log.warning(
                            "checkin internal ledger backfill failed | group=%s slot=%s",
                            group_id,
                            slot,
                        )
        return results

    async def run_forever(self) -> None:
        log.info(
            "checkin internal runner started | tick=%ss", self.tick_seconds
        )
        failures = 0
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("checkin internal pass failed")
                failures = record_background_failure(
                    service=CHECKIN_RUNNER_NAME,
                    previous_failures=failures,
                    error=exc,
                )
            else:
                failures = 0
            await asyncio.sleep(self.tick_seconds)


class WeeklyPlanRunner:
    """周报周期计划：锚点到点就按**当时**的策略入队一次真实执行。"""

    def __init__(
        self,
        *,
        bot: object,
        settings: Any,
        session_factory: async_sessionmaker[AsyncSession],
        manager: Any,
        tick_seconds: float | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.manager = manager
        self.tick_seconds = max(
            5.0, float(PLAN_TICK_SECONDS if tick_seconds is None else tick_seconds)
        )
        self._stopped = False
        #: 入队失败的连续次数（健康页用；成功一次就归零）。
        self._plan_failures = 0

    def stop(self) -> None:
        self._stopped = True

    async def run_once(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """跑一轮：返回本轮处理过的计划（含 ``scheduled`` 是否真的建了 run）。"""

        moment = now if isinstance(now, datetime) else now_shanghai_naive()
        results: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            plans = await task_policy.due_plans(session, now=moment)
            if not plans:
                return []
            authorized = {
                int(value) for value in await authorized_group_ids(session)
            }
            for view in plans:
                if view.group_id not in authorized:
                    # 机器人已经不在/不该管的群：跳过（也不动它的锚点）。
                    continue
                if view.plan_weekday is None or view.plan_minute is None:
                    # 落库的计划不完整（理论上写不进来）：跳过，等管理员重新预览确认。
                    log.warning(
                        "weekly plan row without day/time | group=%s revision=%s",
                        view.group_id,
                        view.revision,
                    )
                    continue
                next_anchor = task_policy.next_occurrence(
                    moment, int(view.plan_weekday), int(view.plan_minute)
                )
                # B13b 返工（R-B3）：锚点只有落在执行容差窗口内才算"这一期到点"。
                # 超出窗口（进程停机很久、或入队一直失败）说明**上一期没跑上**，
                # 重新上线就补发上一周是错的：跳过并把锚点持久推进到严格未来，
                # 而且不产生任何业务副作用（不入队、不发分、不发消息）。
                fire_state = task_policy.plan_fire_state(
                    anchor_at=view.anchor_at, now=moment
                )
                if fire_state != "due":
                    advanced = await task_policy.advance_anchor(
                        session,
                        group_id=view.group_id,
                        task_id=view.task_id,
                        expected_revision=view.revision,
                        next_anchor=next_anchor,
                    )
                    await session.commit()
                    log.warning(
                        "weekly plan window not executed | state=%s group=%s "
                        "anchor=%s next=%s advanced=%s",
                        fire_state,
                        view.group_id,
                        view.anchor_at,
                        next_anchor,
                        advanced,
                    )
                    results.append(
                        {
                            "group_id": view.group_id,
                            "task_id": view.task_id,
                            "scheduled": False,
                            "run_key": "",
                            "next_anchor_at": next_anchor,
                            "anchor_advanced": bool(advanced),
                            "state": fire_state,
                        }
                    )
                    continue
                outcome = await self._enqueue(view, moment)
                scheduled = bool(outcome.get("scheduled"))
                # B13b 返工（R-B2/R-B4）：锚点只有在"这一期真的被消费掉"时才推进。
                #   * 建了 run（含稳定键去重命中的重复）→ 推进；
                #   * 明确无工作（预览没有目标）→ 推进；
                #   * 计划已被关闭/改期（plan_changed）→ 不推进（那不是本期的活干完）；
                #   * 入队抛错/结果不确定 → 不推进：保留失败证据，容差窗口内下一轮
                #     重试，绝不把"数据库/入队临时故障"显示成正常 fired 并无声消耗当期。
                advance = bool(outcome.get("advance"))
                from bot.services import operations_runs as operations_kernel

                recovery = str(outcome.get("recovery") or "")
                if scheduled and advance:
                    self._plan_failures = 0
                elif not advance:
                    error = outcome.get("error")
                    if (
                        not isinstance(error, BaseException)
                        and recovery == operations_kernel.PLAN_RECOVERY_UNRESOLVED
                    ):
                        # R-B4：已经开始过、结果无法确认——不重跑、不补发，但必须
                        # 留下健康失败证据，不能因为"没抛异常"就当成正常收尾。
                        error = RuntimeError(
                            str(outcome.get("detail") or "plan run unresolved")
                        )
                    if isinstance(error, BaseException):
                        self._plan_failures = record_background_failure(
                            service=WEEKLY_PLAN_RUNNER_NAME,
                            previous_failures=self._plan_failures,
                            error=error,
                        )
                    log.error(
                        "weekly plan enqueue not confirmed | group=%s task=%s "
                        "reason=%s detail=%s",
                        view.group_id,
                        view.task_id,
                        outcome.get("reason"),
                        outcome.get("detail"),
                    )
                advanced = False
                if advance:
                    # 锚点推进用 CAS（**不升 revision**：推进是 scheduler 自己的游标，
                    # 升版本会把刚入队 run 的守卫钉死）。管理员中途改过计划时 CAS
                    # 失败，说明这份计划已经被新的策略取代，本轮就此停手、不重试。
                    advanced = await task_policy.advance_anchor(
                        session,
                        group_id=view.group_id,
                        task_id=view.task_id,
                        expected_revision=view.revision,
                        next_anchor=next_anchor,
                    )
                    await session.commit()
                    if not advanced:
                        log.warning(
                            "weekly plan anchor not advanced (policy changed) | "
                            "group=%s task=%s",
                            view.group_id,
                            view.task_id,
                        )
                results.append(
                    {
                        "group_id": view.group_id,
                        "task_id": view.task_id,
                        "scheduled": scheduled,
                        "run_key": str(outcome.get("run_key", "") or ""),
                        "next_anchor_at": next_anchor,
                        "anchor_advanced": bool(advanced),
                        "state": (
                            recovery
                            if scheduled and recovery
                            else (
                                "fired"
                                if scheduled
                                else (
                                    "no_work"
                                    if advance
                                    else (
                                        # B13b 返工（R-B2）：计划在真实入队边界上被
                                        # 关闭/改期，是"这一期作废"，不是入队故障。
                                        "plan_changed"
                                        if str(outcome.get("reason") or "")
                                        == "plan_changed"
                                        else "enqueue_unconfirmed"
                                    )
                                )
                            )
                        ),
                    }
                )
                log.info(
                    "weekly plan fired | group=%s scheduled=%s next=%s",
                    view.group_id,
                    outcome.get("scheduled"),
                    next_anchor,
                )
        return results

    async def _enqueue(self, view: task_policy.TaskPolicyView, moment: datetime) -> dict[str, Any]:
        from bot.services import operations_runs

        try:
            return await operations_runs.enqueue_scheduled_run(
                self.session_factory,
                self.settings,
                self.manager,
                task_id=view.task_id,
                group_id=view.group_id,
                params=dict(WEEKLY_PLAN_PARAMS),
                bot=self.bot,
                now=moment,
                # R-B2：把这次触发依据的**计划身份**（revision + 锚点）传下去，
                # 由内核在真实入队边界上以带谓词的真实 DML 复核。
                plan=view,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "weekly plan enqueue failed | group=%s task=%s",
                view.group_id,
                view.task_id,
            )
            # R-B4：入队抛错**不等于**没有执行记录。异常可能发生在真实 commit
            # 之后（回执丢失）、也可能发生在建成 run 之后（响应丢失）。用同一
            # 个稳定计划键对账一次，按"从未开始 / 在跑 / 已结束 / 结果未知"
            # 分类：只有确实查不到记录才按"明确未入队"处理（不推进锚点、保留
            # 失败证据、容差窗口内重试）。
            run_key = task_policy.plan_run_key(
                group_id=view.group_id,
                task_id=view.task_id,
                anchor_at=view.anchor_at,
            )
            try:
                outcome = await operations_runs.reconcile_scheduled_run(
                    self.session_factory,
                    task_id=view.task_id,
                    group_id=view.group_id,
                    run_key=run_key,
                    bot=self.bot,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "weekly plan reconciliation failed | group=%s task=%s run_key=%s",
                    view.group_id,
                    view.task_id,
                    run_key,
                )
                return {
                    "scheduled": False,
                    "advance": False,
                    "run_key": run_key,
                    "status": "enqueue_failed",
                    "reason": "enqueue_failed",
                    "detail": (
                        "入队失败且对账也失败：这一期不推进锚点、不重复建记录，"
                        "下一轮继续用同一个稳定计划键对账"
                    ),
                    "error": exc,
                }
            outcome = dict(outcome)
            outcome["error"] = exc
            outcome["run_key"] = str(outcome.get("run_key") or run_key)
            return outcome

    async def run_forever(self) -> None:
        log.info("weekly plan runner started | tick=%ss", self.tick_seconds)
        failures = 0
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("weekly plan pass failed")
                failures = record_background_failure(
                    service=WEEKLY_PLAN_RUNNER_NAME,
                    previous_failures=failures,
                    error=exc,
                )
            else:
                failures = 0
            await asyncio.sleep(self.tick_seconds)


__all__ = [
    "CHECKIN_RUNNER_NAME",
    "CHECKIN_TICK_SECONDS",
    "CheckinInternalRunner",
    "PLAN_TICK_SECONDS",
    "WEEKLY_PLAN_PARAMS",
    "WEEKLY_PLAN_RUNNER_NAME",
    "WeeklyPlanRunner",
]
