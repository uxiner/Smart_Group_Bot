"""按群执行来源与周期计划：**唯一真源**（第三优先 B13b / C4）。

为什么要独立一张表（``group_task_policies``）而不是复用 ``Group.settings`` JSON：

1. 切换来源与开启周期计划都是**管理员确认后的持久事实**，必须能 CAS（比较并
   交换）：确认那一刻的 ``revision`` 写进预览令牌，确认时用同一条 SQL 的
   ``WHERE revision = :expected`` 抢占，另一名管理员先改过就整体拒绝，绝不做
   "读-改-写"。
2. 真正不可逆的副作用入口（发消息 / 写积分 / 发布摘要）用
   :func:`bot.services.operations_guard.guard_predicate` 把
   ``group_task_policies.revision`` 编进 DML 自己的 ``WHERE``，所以确认之后
   管理员又改了来源或计划，旧确认会被**数据库写入边界**挡住，而不是靠一次
   提前的 SELECT。

本模块只做读、纯计算与 CAS 写入，**不 commit**（调用方在同一事务里写完审计与
业务台账再 commit）。

口径（来自 A 的正式 brief / roadmap，不是本模块自己发明）：

* 签到提醒：默认 ``external``（上海 9/12/15/18 的宿主 cron 仍在跑），可以切到
  ``internal``（程序内 runner）。切换**只对未来的时段生效**，不补旧时段。
* 商店到期：默认 ``internal``（进程内 ``ShopExpiryService`` 保持既有 300 秒成功
  / 900 秒失败节奏），可以切到 ``external``（旧 ``*/10`` 入口）。
* 定时消息、群摘要：本来就是 internal-only，不可切换。
* 周活跃结算：与周报共用同一积分 ref，不新增独立自动结算。
* 周报：手动为主，周期计划**默认关闭**；只有管理员显式选了周内某天与时刻并
  确认后才有 internal 计划，且下一次计划严格在未来（开启不会立刻补上一周）。
* 现网没有周报 cron：所以周报的 ``source`` 固定 ``internal``，不假装有外部计划。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import GroupTaskPolicy, GroupTaskPolicyEvent
from bot.utils.timezone import now_shanghai_naive, to_shanghai_datetime

log = logging.getLogger(__name__)

#: 来源取值：程序内 runner / 外部宿主计划。
SOURCE_INTERNAL = "internal"
SOURCE_EXTERNAL = "external"

#: 每个任务**允许**落在哪些来源上。元组长度 1 表示不可切换（界面上不给切换
#: 按钮，写接口也会拒绝），因为现网确实没有那种外部入口。
SOURCE_SWITCHABLE: dict[str, tuple[str, ...]] = {
    "scheduled_messages": (SOURCE_INTERNAL,),
    "group_summary": (SOURCE_INTERNAL,),
    "checkin_reminder": (SOURCE_INTERNAL, SOURCE_EXTERNAL),
    "activity_award": (SOURCE_INTERNAL,),
    "weekly_report": (SOURCE_INTERNAL,),
    "shop_expire": (SOURCE_INTERNAL, SOURCE_EXTERNAL),
}

#: 没有策略行时的默认来源＝**改造前的真实行为**（原样保留，方便对照）。
DEFAULT_SOURCE: dict[str, str] = {
    "scheduled_messages": SOURCE_INTERNAL,
    "group_summary": SOURCE_INTERNAL,
    "checkin_reminder": SOURCE_EXTERNAL,
    "activity_award": SOURCE_INTERNAL,
    "weekly_report": SOURCE_INTERNAL,
    "shop_expire": SOURCE_INTERNAL,
}

#: 支持周期计划的任务（当前只有周报；计划由 internal runner 触发）。
PLAN_TASKS: tuple[str, ...] = ("weekly_report",)

#: 有按群策略的任务集合（顺序与只读状态聚合的 ``TASK_IDS`` 一致，有回归钉住）。
POLICY_TASK_IDS: tuple[str, ...] = tuple(SOURCE_SWITCHABLE)

#: 来源重新生效后，时段晚于 ``active_from`` 才算"到点"的容差窗口（秒）。
#: 窗口之外（进程停机很久之后重启）**不补发**过时段。
SLOT_DUE_GRACE_SECONDS = 3600

#: 周期计划到点后的**执行容差窗口**（秒）。B13b 返工（R-B3）：锚点在这个窗口内
#: 才算"到点、可以执行"（吸收轮询与重启延迟）；已经超出窗口的锚点属于**上一个
#: 周期没跑上**，只能跳过并把锚点推进到严格未来，绝不因为重新上线就补发上一周。
#: 这个值与状态页共用（见 :func:`plan_fire_state`），不是只藏在 runner 里的常量。
PLAN_DUE_TOLERANCE_SECONDS = 900

#: 写结果码（HTTP 层映射成 409 / 400）。
POLICY_OK = "ok"
POLICY_REVISION_CONFLICT = "revision_conflict"
POLICY_SOURCE_NOT_SWITCHABLE = "source_not_switchable"
POLICY_INVALID_SOURCE = "invalid_source"
POLICY_PLAN_UNSUPPORTED = "plan_unsupported"
POLICY_INVALID_PLAN = "invalid_plan"


@dataclass(frozen=True, slots=True)
class TaskPolicyView:
    """一个群、一个任务的**生效**策略（缺行时给出默认值 + ``exists=False``）。"""

    group_id: int
    task_id: str
    source: str
    source_default: str
    source_options: tuple[str, ...]
    source_switchable: bool
    plan_supported: bool
    plan_enabled: bool
    plan_weekday: int | None
    plan_minute: int | None
    anchor_at: datetime | None
    active_from: datetime | None
    revision: int
    exists: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "task_id": self.task_id,
            "source": self.source,
            "source_default": self.source_default,
            "source_options": list(self.source_options),
            "source_switchable": self.source_switchable,
            "plan_supported": self.plan_supported,
            "plan_enabled": self.plan_enabled,
            "plan_weekday": self.plan_weekday,
            "plan_minute": self.plan_minute,
            "anchor_at": _stamp(self.anchor_at),
            "active_from": _stamp(self.active_from),
            "revision": self.revision,
            "exists": self.exists,
        }


def policy_payload(view: TaskPolicyView) -> dict[str, Any]:
    """API 形状的策略视图：时间一律带 ``Asia/Shanghai`` 偏移，外部登记恒为未知。"""

    document = view.as_dict()
    document["anchor_at"] = _iso(view.anchor_at)
    document["active_from"] = _iso(view.active_from)
    document["runner_task_name"] = (
        _INTERNAL_RUNNERS.get(view.task_id) if view.source == SOURCE_INTERNAL else None
    )
    document["external_schedule_known"] = False
    document["external_schedule_reason"] = "external_schedule_unknown"
    return document


@dataclass(frozen=True, slots=True)
class PolicyWriteOutcome:
    ok: bool
    code: str
    detail: str
    view: TaskPolicyView | None = None


def _stamp(value: datetime | None) -> str | None:
    """ISO 字符串（上海 naive 口径；前端自己加 ``Asia/Shanghai`` 文案）。"""

    return value.isoformat() if isinstance(value, datetime) else None


def _require_task(task_id: str) -> str:
    key = str(task_id or "")
    if key not in SOURCE_SWITCHABLE:
        raise ValueError(f"unknown task for policy: {task_id!r}")
    return key


def source_options(task_id: str) -> tuple[str, ...]:
    return SOURCE_SWITCHABLE[_require_task(task_id)]


def default_source(task_id: str) -> str:
    return DEFAULT_SOURCE[_require_task(task_id)]


def is_switchable(task_id: str) -> bool:
    return len(source_options(task_id)) > 1


def plan_supported(task_id: str) -> bool:
    return _require_task(task_id) in PLAN_TASKS


def normalize_source(task_id: str, value: object) -> str:
    """把落库/入参的来源收敛进该任务允许的取值；非法一律回落到默认值。"""

    allowed = source_options(task_id)
    text = str(value or "").strip().lower()
    return text if text in allowed else default_source(task_id)


def _view_from_row(group_id: int, task_id: str, row: GroupTaskPolicy | None) -> TaskPolicyView:
    options = source_options(task_id)
    if row is None:
        return TaskPolicyView(
            group_id=int(group_id),
            task_id=task_id,
            source=default_source(task_id),
            source_default=default_source(task_id),
            source_options=options,
            source_switchable=len(options) > 1,
            plan_supported=plan_supported(task_id),
            plan_enabled=False,
            plan_weekday=None,
            plan_minute=None,
            anchor_at=None,
            active_from=None,
            revision=0,
            exists=False,
        )
    return TaskPolicyView(
        group_id=int(group_id),
        task_id=task_id,
        source=normalize_source(task_id, row.source),
        source_default=default_source(task_id),
        source_options=options,
        source_switchable=len(options) > 1,
        plan_supported=plan_supported(task_id),
        plan_enabled=bool(row.plan_enabled),
        plan_weekday=row.plan_weekday,
        plan_minute=row.plan_minute,
        anchor_at=row.anchor_at,
        active_from=row.active_from,
        revision=int(row.revision or 0),
        exists=True,
    )


async def load_policies(session: AsyncSession, *, group_id: int) -> dict[str, TaskPolicyView]:
    """本群六类任务的生效策略（缺行的任务给默认值）。"""

    gid = int(group_id)
    rows = (
        await session.scalars(
            select(GroupTaskPolicy).where(GroupTaskPolicy.group_id == gid)
        )
    ).all()
    by_task = {str(row.task_id): row for row in rows}
    views: dict[str, TaskPolicyView] = {}
    for task_id in POLICY_TASK_IDS:
        row = by_task.get(task_id)
        # 只在**属于本任务**时用行；别的任务 id 的历史行忽略。
        views[task_id] = _view_from_row(gid, task_id, row if row is not None else None)
    return views


async def load_policy(session: AsyncSession, *, group_id: int, task_id: str) -> TaskPolicyView:
    """单任务生效策略。"""

    key = _require_task(task_id)
    gid = int(group_id)
    row = await session.scalar(
        select(GroupTaskPolicy).where(
            GroupTaskPolicy.group_id == gid, GroupTaskPolicy.task_id == key
        )
    )
    if row is not None and str(row.task_id) != key:
        row = None
    return _view_from_row(gid, key, row)


async def resolve_sources(
    session: AsyncSession, *, task_id: str, group_ids: Iterable[int]
) -> dict[int, str]:
    """给 runner / CLI 用：一批群的**生效来源**（缺行＝默认来源）。

    这是"runner 与 CLI 消费同一处来源选择"的实现点：两边都调它，不各自判断。
    """

    key = _require_task(task_id)
    ids = sorted({int(g) for g in group_ids})
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(GroupTaskPolicy).where(
                GroupTaskPolicy.task_id == key, GroupTaskPolicy.group_id.in_(ids)
            )
        )
    ).all()
    found = {int(row.group_id): normalize_source(key, row.source) for row in rows}
    return {gid: found.get(gid, default_source(key)) for gid in ids}


async def load_task_policies(
    session: AsyncSession, *, task_id: str, group_ids: Iterable[int]
) -> dict[int, TaskPolicyView]:
    """给 runner / CLI 用：一批群在**同一个任务**上的生效策略（缺行＝默认值）。

    :func:`resolve_sources` 只给来源字符串；需要 ``active_from``（"切换只锚定未来"）
    的调用方读这里。两边读的是同一张表、同一套默认值，所以 runner 与 CLI 不会对
    "这个群归谁管"得出不同结论。
    """

    key = _require_task(task_id)
    ids = sorted({int(group) for group in group_ids})
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(GroupTaskPolicy).where(
                GroupTaskPolicy.task_id == key, GroupTaskPolicy.group_id.in_(ids)
            )
        )
    ).all()
    found = {
        int(row.group_id): row for row in rows if str(row.task_id) == key
    }
    return {group: _view_from_row(group, key, found.get(group)) for group in ids}


async def policy_revision(session: AsyncSession, *, group_id: int, task_id: str) -> int:
    """当前按群策略版本；没有行时是 ``0``（与守卫谓词的 ``coalesce(...,0)`` 对齐）。"""

    key = _require_task(task_id)
    value = await session.scalar(
        select(GroupTaskPolicy.revision).where(
            GroupTaskPolicy.group_id == int(group_id), GroupTaskPolicy.task_id == key
        )
    )
    return int(value or 0)


async def managed_group_ids(session: AsyncSession, *, task_id: str) -> set[int]:
    """有**持久来源记录**的群（不管现在还授不授权）。

    B13b 返工 v2（R-B1）：``authorized_group_ids`` 会把"已撤权的群"整个过滤掉，
    于是一个**明确被切到 external、随后被撤权**的群在自动入口眼里就退化成
    "从来没有过策略的历史群"，被当作 legacy 全量处理——这是父 v4 复现的漏口。

    "这个部署有没有在管这件事"由持久来源记录决定：只要该任务下存在任何一行
    ``GroupTaskPolicy``，扫描范围就必须是显式的授权群集合，而不是无约束全量。
    只有一行策略记录都没有的老库才保留改造前的 legacy 口径。
    """

    key = _require_task(task_id)
    rows = await session.scalars(
        select(GroupTaskPolicy.group_id).where(GroupTaskPolicy.task_id == key)
    )
    return {int(value) for value in rows.all()}


async def due_plans(
    session: AsyncSession, *, now: datetime | None = None
) -> list[TaskPolicyView]:
    """到点的周期计划（internal runner 每轮只读这里）。"""

    moment = now or now_shanghai_naive()
    rows = (
        await session.scalars(
            select(GroupTaskPolicy)
            .where(
                GroupTaskPolicy.task_id.in_(list(PLAN_TASKS)),
                GroupTaskPolicy.plan_enabled.is_(True),
                GroupTaskPolicy.anchor_at.is_not(None),
                GroupTaskPolicy.anchor_at <= moment,
            )
            .order_by(GroupTaskPolicy.anchor_at, GroupTaskPolicy.id)
        )
    ).all()
    views: list[TaskPolicyView] = []
    for row in rows:
        task_id = str(row.task_id)
        if task_id not in PLAN_TASKS:
            continue
        views.append(_view_from_row(int(row.group_id), task_id, row))
    return views


def next_occurrence(now: datetime, weekday: int, minute: int) -> datetime:
    """下一次"周内第 weekday 天（0=周一）、本地 HH:MM"的**严格未来**时刻。

    严格未来是"开启计划不会立刻补上一周"的实现点：调用方把返回值存进
    ``anchor_at``，runner 只在 ``now >= anchor_at`` 时才触发。
    """

    day = int(weekday) % 7
    minutes = max(0, min(1439, int(minute)))
    base = now.replace(second=0, microsecond=0)
    days_ahead = (day - base.weekday()) % 7
    candidate = (base + timedelta(days=days_ahead)).replace(
        hour=minutes // 60, minute=minutes % 60
    )
    if candidate <= base:
        candidate += timedelta(days=7)
    return candidate


def slot_moment(day: date, slot: int) -> datetime:
    """某个自然日某个时段的计划时刻（Asia/Shanghai naive，分钟固定 0）。"""

    return datetime(day.year, day.month, day.day, int(slot) % 24, 0, 0)


def plan_fire_state(
    *,
    anchor_at: datetime | None,
    now: datetime,
    tolerance_seconds: int = PLAN_DUE_TOLERANCE_SECONDS,
) -> str:
    """周期计划锚点相对"现在"的执行状态：``waiting`` / ``due`` / ``missed``。

    * ``waiting``：锚点还没到（或没有锚点）；
    * ``due``：锚点到点且仍在容差窗口内 —— 这一轮**应该**执行一次；
    * ``missed``：锚点已经超出容差窗口（进程停机很久、上一次入队一直失败）——
      执行上一周期是错误的补发，只能跳过并推进到严格未来。

    调度（:mod:`bot.services.periodic_tasks`）与只读状态页共用这一个函数，避免
    "页面说还有几秒、runner 却认为已经错过"这类口径分裂。
    """

    if anchor_at is None:
        return "waiting"
    window = timedelta(seconds=max(0, int(tolerance_seconds)))
    if anchor_at > now:
        return "waiting"
    if now < anchor_at + window:
        return "due"
    return "missed"


def plan_run_key(*, group_id: int, task_id: str, anchor_at: datetime) -> str:
    """某个周期计划某一次锚点的**稳定** run_key。

    B13b 返工（R-B4）：入队的 run_key 不能再是每次随机值，否则"提交成功但 ACK
    未知"的重试无法对账，会重复建 run/重复发分。同一个 (计划任务, 群, 锚点) 永远
    映射到同一个键：第一次真正落库，之后的重试都命中唯一约束并被识别为"已经入队"。
    """

    material = (
        f"{str(task_id).strip()}:{int(group_id)}:"
        f"{anchor_at.replace(microsecond=0).isoformat()}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def slot_moment_from_key(slot_key: str) -> datetime | None:
    """``2026-10-01:9`` → 计划时刻；解析不出来返回 ``None``（不猜）。"""

    text = str(slot_key or "")
    day_text, _, slot_text = text.partition(":")
    if not day_text or not slot_text:
        return None
    try:
        day = date.fromisoformat(day_text)
        slot = int(slot_text)
    except ValueError:
        return None
    if not 0 <= slot <= 23:
        return None
    return slot_moment(day, slot)


def due_slots(
    *,
    now: datetime,
    slots: Iterable[int],
    active_from: datetime | None = None,
    grace_seconds: int = SLOT_DUE_GRACE_SECONDS,
) -> list[int]:
    """当前到点的时段。

    两个条件缺一不可：①计划时刻已经到点、且还在容差窗口内（过期太久的时段
    不补发）；②计划时刻不早于 ``active_from``（来源刚切换生效，**旧时段不补**）。
    """

    window = timedelta(seconds=max(0, int(grace_seconds)))
    result: list[int] = []
    for slot in sorted({int(value) for value in slots}):
        at = slot_moment(now.date(), slot)
        if active_from is not None and at < active_from:
            continue
        if at <= now < at + window:
            result.append(slot)
    return result


def _merge_anchor_view(
    view: TaskPolicyView,
    *,
    source: str,
    revision: int,
    active_from: datetime,
    plan_enabled: bool,
    plan_weekday: int | None,
    plan_minute: int | None,
    anchor_at: datetime | None,
) -> TaskPolicyView:
    return TaskPolicyView(
        group_id=view.group_id,
        task_id=view.task_id,
        source=normalize_source(view.task_id, source),
        source_default=view.source_default,
        source_options=view.source_options,
        source_switchable=view.source_switchable,
        plan_supported=view.plan_supported,
        plan_enabled=plan_enabled,
        plan_weekday=plan_weekday,
        plan_minute=plan_minute,
        anchor_at=anchor_at,
        active_from=active_from,
        revision=int(revision),
        exists=True,
    )


def _policy_snapshot(view: TaskPolicyView) -> dict[str, Any]:
    """审计用的小快照（不进个人数据、不放 params）。"""

    return {
        "source": view.source,
        "plan_enabled": bool(view.plan_enabled),
        "plan_weekday": view.plan_weekday,
        "plan_minute": view.plan_minute,
        "anchor_at": _stamp(view.anchor_at),
    }


async def _append_event(
    session: AsyncSession,
    *,
    view: TaskPolicyView,
    action: str,
    before: dict[str, Any],
    detail: str,
    actor_user_id: int,
    now: datetime,
) -> None:
    session.add(
        GroupTaskPolicyEvent(
            group_id=view.group_id,
            task_id=view.task_id,
            actor_user_id=int(actor_user_id or 0),
            action=action,
            revision=int(view.revision),
            before=before,
            after=_policy_snapshot(view),
            detail=str(detail)[:300],
            created_at=now,
        )
    )


async def _cas_write(
    session: AsyncSession,
    *,
    row: GroupTaskPolicy | None,
    view: TaskPolicyView,
    values: dict[str, Any],
    expected_revision: int,
    now: datetime,
) -> bool:
    """同一条 SQL 的 CAS 抢占（插行或按 revision 更新）；``False``＝被别人抢先。"""

    expected = int(expected_revision)
    payload = dict(values)
    if row is None:
        statement = (
            sqlite_insert(GroupTaskPolicy)
            .values(
                group_id=view.group_id,
                task_id=view.task_id,
                revision=1,
                updated_by=int(payload.pop("updated_by", 0) or 0),
                updated_at=now,
                **payload,
            )
            .on_conflict_do_nothing(index_elements=["group_id", "task_id"])
        )
        result = await session.execute(statement)
        return bool(result.rowcount == 1)
    statement = (
        update(GroupTaskPolicy)
        .where(
            GroupTaskPolicy.id == int(row.id),
            GroupTaskPolicy.revision == expected,
        )
        .values(
            revision=GroupTaskPolicy.revision + 1,
            updated_at=now,
            **payload,
        )
    )
    result = await session.execute(statement)
    return bool(result.rowcount == 1)


async def _load_row(
    session: AsyncSession, *, group_id: int, task_id: str
) -> GroupTaskPolicy | None:
    return await session.scalar(
        select(GroupTaskPolicy).where(
            GroupTaskPolicy.group_id == int(group_id), GroupTaskPolicy.task_id == task_id
        )
    )


async def switch_source(
    session: AsyncSession,
    *,
    group_id: int,
    task_id: str,
    source: str,
    expected_revision: int,
    actor_user_id: int = 0,
    now: datetime | None = None,
) -> PolicyWriteOutcome:
    """把本群某任务的执行来源切到 ``source``（CAS，不 commit）。"""

    key = _require_task(task_id)
    moment = now or now_shanghai_naive()
    wanted = str(source or "").strip().lower()
    allowed = source_options(key)
    row = await _load_row(session, group_id=int(group_id), task_id=key)
    current = _view_from_row(int(group_id), key, row)
    if len(allowed) <= 1:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_SOURCE_NOT_SWITCHABLE,
            detail="这个任务只有一种执行来源，不能切换",
            view=current,
        )
    if wanted not in allowed:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_INVALID_SOURCE,
            detail="不支持的执行来源",
            view=current,
        )
    if int(expected_revision) != current.revision:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_REVISION_CONFLICT,
            detail="这个群的策略刚刚被改过，请重新预览",
            view=current,
        )
    if current.source == wanted:
        # 幂等：来源没变就不写、不升版本（避免无意义地把在途确认作废）。
        return PolicyWriteOutcome(ok=True, code=POLICY_OK, detail="来源未变化", view=current)

    before = _policy_snapshot(current)
    ok = await _cas_write(
        session,
        row=row,
        view=current,
        values={
            "source": wanted,
            "active_from": moment,
            "updated_by": int(actor_user_id or 0),
        },
        expected_revision=int(expected_revision),
        now=moment,
    )
    if not ok:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_REVISION_CONFLICT,
            detail="这个群的策略刚刚被改过，请重新预览",
            view=current,
        )
    updated = _merge_anchor_view(
        current,
        source=wanted,
        revision=current.revision + 1,
        active_from=moment,
        plan_enabled=current.plan_enabled,
        plan_weekday=current.plan_weekday,
        plan_minute=current.plan_minute,
        anchor_at=current.anchor_at,
    )
    await _append_event(
        session,
        view=updated,
        action="source_switch",
        before=before,
        detail=f"执行来源：{current.source} → {wanted}（只影响未来，不补历史）",
        actor_user_id=int(actor_user_id or 0),
        now=moment,
    )
    return PolicyWriteOutcome(ok=True, code=POLICY_OK, detail="执行来源已更新", view=updated)


async def set_weekly_plan(
    session: AsyncSession,
    *,
    group_id: int,
    enabled: bool,
    weekday: int | None,
    minute: int | None,
    expected_revision: int,
    task_id: str = "weekly_report",
    actor_user_id: int = 0,
    now: datetime | None = None,
) -> PolicyWriteOutcome:
    """开启/关闭周期计划（CAS，不 commit）。

    开启**必须**显式给出周内日与时刻（否则拒绝，界面不许预填一个"看起来已启用"
    的值）；下一次计划严格在未来。
    """

    key = _require_task(task_id)
    moment = now or now_shanghai_naive()
    row = await _load_row(session, group_id=int(group_id), task_id=key)
    current = _view_from_row(int(group_id), key, row)
    if key not in PLAN_TASKS:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_PLAN_UNSUPPORTED,
            detail="这个任务不支持周期计划",
            view=current,
        )
    if int(expected_revision) != current.revision:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_REVISION_CONFLICT,
            detail="这个群的策略刚刚被改过，请重新预览",
            view=current,
        )

    want_enabled = bool(enabled)
    if want_enabled:
        if weekday is None or minute is None:
            return PolicyWriteOutcome(
                ok=False,
                code=POLICY_INVALID_PLAN,
                detail="必须明确选择周内哪一天与几点（不会替你预填）",
                view=current,
            )
        try:
            day = int(weekday)
            minutes = int(minute)
        except (TypeError, ValueError):
            return PolicyWriteOutcome(
                ok=False,
                code=POLICY_INVALID_PLAN,
                detail="周内日与时刻必须是整数",
                view=current,
            )
        if not 0 <= day <= 6 or not 0 <= minutes <= 1439:
            return PolicyWriteOutcome(
                ok=False,
                code=POLICY_INVALID_PLAN,
                detail="周内日必须是 0-6、时刻必须在 00:00-23:59 之间",
                view=current,
            )
        anchor = next_occurrence(moment, day, minutes)
        values = {
            "plan_enabled": True,
            "plan_weekday": day,
            "plan_minute": minutes,
            "anchor_at": anchor,
            "active_from": moment,
            "updated_by": int(actor_user_id or 0),
        }
        if not current.exists:
            values["source"] = current.source
        action = "plan_enable"
        detail = f"周期计划开启：每周第 {day + 1} 天 {minutes // 60:02d}:{minutes % 60:02d}（Asia/Shanghai）"
        next_anchor: datetime | None = anchor
    else:
        anchor = None
        values = {
            "plan_enabled": False,
            "plan_weekday": None,
            "plan_minute": None,
            "anchor_at": None,
            "active_from": moment,
            "updated_by": int(actor_user_id or 0),
        }
        if not current.exists:
            values["source"] = current.source
        action = "plan_disable"
        detail = "周期计划已关闭"
        next_anchor = None

    if current.plan_enabled == want_enabled and current.exists:
        if not want_enabled or (
            current.plan_weekday == values.get("plan_weekday")
            and current.plan_minute == values.get("plan_minute")
        ):
            return PolicyWriteOutcome(ok=True, code=POLICY_OK, detail="计划未变化", view=current)

    before = _policy_snapshot(current)
    ok = await _cas_write(
        session,
        row=row,
        view=current,
        values=values,
        expected_revision=int(expected_revision),
        now=moment,
    )
    if not ok:
        return PolicyWriteOutcome(
            ok=False,
            code=POLICY_REVISION_CONFLICT,
            detail="这个群的策略刚刚被改过，请重新预览",
            view=current,
        )
    updated = _merge_anchor_view(
        current,
        source=current.source,
        revision=current.revision + 1,
        active_from=moment,
        plan_enabled=want_enabled,
        plan_weekday=int(values["plan_weekday"]) if want_enabled else None,
        plan_minute=int(values["plan_minute"]) if want_enabled else None,
        anchor_at=next_anchor,
    )
    await _append_event(
        session,
        view=updated,
        action=action,
        before=before,
        detail=detail,
        actor_user_id=int(actor_user_id or 0),
        now=moment,
    )
    return PolicyWriteOutcome(ok=True, code=POLICY_OK, detail=detail, view=updated)


async def advance_anchor(
    session: AsyncSession,
    *,
    group_id: int,
    next_anchor: datetime,
    expected_revision: int,
    task_id: str = "weekly_report",
) -> bool:
    """runner 把已触发的锚点推进到下一次（**不升 revision**）。

    不升版本是刻意的：推进锚点是 scheduler 自己的游标，不是策略变化。如果这里升
    版本，刚按当前版本入队的 run 会被自己的守卫谓词挡住（自伤）。真正的策略变化
    （来源切换 / 计划改动）才升版本，那时 CAS 会因为 revision 不匹配而失败，本轮
    停止。
    """

    key = _require_task(task_id)
    statement = (
        update(GroupTaskPolicy)
        .where(
            GroupTaskPolicy.group_id == int(group_id),
            GroupTaskPolicy.task_id == key,
            GroupTaskPolicy.revision == int(expected_revision),
            GroupTaskPolicy.plan_enabled.is_(True),
        )
        .values(anchor_at=next_anchor)
    )
    result = await session.execute(statement)
    return bool(result.rowcount == 1)


async def admit_plan_run(
    session: AsyncSession,
    *,
    group_id: int,
    expected_revision: int,
    expected_anchor_at: datetime,
    task_id: str = "weekly_report",
) -> bool:
    """B13b 返工（R-B2）：把"这次入队依据的计划身份"编进一条**真实 DML 的 WHERE**。

    ``UPDATE group_task_policies SET anchor_at = <同一个值>``，条件是
    ``revision = expected_revision AND plan_enabled = 1 AND anchor_at = expected_anchor``。

    为什么要写成一句 no-op UPDATE 而不是 SELECT：``SELECT`` 不持有 SQLite 的
    writer 锁，读完到插入 run 之间，另一个合法 writer 完全可以把计划关掉/改期
    （父 v1 探针的 ``B-weekly-disable-before-real-enqueue`` 就插在这个窗口里，
    旧 tick 于是拿**关闭后的新 revision** 去入队并真的发了一周）。这条 UPDATE 是
    真实写：它在持有 writer 锁的前提下求值谓词，并且锁一直持到本事务 commit，
    于是"确认计划身份"与"插入这次 run"之间没有任何可被抢占的窗口。写回的是同一个
    ``anchor_at``（推进游标仍由 :func:`advance_anchor` 在业务 run 入队成功后做，
    保持"推进锚点"与"策略变化"两件事分开）。

    返回 ``True`` = 计划身份仍然成立，调用方可以继续在**同一事务**里插入 run。
    """

    key = _require_task(task_id)
    if not isinstance(expected_anchor_at, datetime):
        return False
    statement = (
        update(GroupTaskPolicy)
        .where(
            GroupTaskPolicy.group_id == int(group_id),
            GroupTaskPolicy.task_id == key,
            GroupTaskPolicy.revision == int(expected_revision),
            GroupTaskPolicy.plan_enabled.is_(True),
            GroupTaskPolicy.anchor_at == expected_anchor_at,
        )
        .values(anchor_at=expected_anchor_at)
    )
    result = await session.execute(statement)
    return bool(result.rowcount == 1)


# --------------------------------------------------------- 预览 / 确认 / 审计

#: 策略令牌的任务名前缀：复用运营内核的单次令牌（同一套签名、有效期、角色绑定），
#: 但永远不会和真实任务 id 撞车。
POLICY_TASK_PREFIX = "policy."

WEEKDAY_LABELS: tuple[str, ...] = (
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
)

#: 每个任务在两个来源下"到底是谁在跑"的实话（前端要原样展示给管理员）。
_INTERNAL_RUNNERS: dict[str, str] = {
    "checkin_reminder": "checkin_reminder_internal",
    "shop_expire": "shop-expiry-runner",
}

_SOURCE_EFFECTS: dict[tuple[str, str], str] = {
    ("checkin_reminder", SOURCE_INTERNAL): (
        "本群签到提醒改由机器人进程内 runner 执行：从下一个时段开始生效，"
        "不补发切换之前的时段；与外部入口共用占位与出站回执，不会重复发送。"
    ),
    ("checkin_reminder", SOURCE_EXTERNAL): (
        "本群签到提醒交回外部 CLI（宿主 cron）：只有 cron 真的在对应时刻调用时才"
        "会发送。程序不读宿主的 crontab，外部登记情况未知，需要在宿主侧确认。"
        "切换不补发已经过去的时段。"
    ),
    ("shop_expire", SOURCE_INTERNAL): (
        "本群商店到期处理由进程内 runner 按既有节奏执行（成功 300 秒一轮 / "
        "失败 900 秒一轮）。"
    ),
    ("shop_expire", SOURCE_EXTERNAL): (
        "本群商店到期处理交给外部入口（旧的 */10 CLI）：在宿主 cron 调用它之前"
        "不会处理本群。程序不读宿主的 crontab，外部登记情况未知。"
    ),
}


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return to_shanghai_datetime(value).isoformat()


def _plan_effects(*, enabled: bool, weekday: int | None, minute: int | None, anchor: datetime | None) -> str:
    if not enabled:
        return (
            "关闭本群周报的周期计划：以后只在你手动预览并确认之后才结算和发送。"
        )
    label = WEEKDAY_LABELS[int(weekday)] if weekday is not None else ""
    clock = "%02d:%02d" % (int(minute or 0) // 60, int(minute or 0) % 60)
    when = _iso(anchor) or ""
    return (
        f"从下一个 {label} {clock}（Asia/Shanghai，绝对时刻 {when}）开始，"
        "每周自动按**当时**的活跃数据结算积分并发送周报；只影响未来，"
        "开启不会立刻补发上一周。已入账的积分按原 ref 幂等，不会重复发放。"
    )


def _weekday_ok(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 6 else None


def _minute_ok(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 1439 else None


async def preview_policy(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    *,
    group_id: int,
    task_id: str,
    mode: str,
    source: object = None,
    enabled: object = None,
    weekday: object = None,
    minute: object = None,
    actor_user_id: int = 0,
    actor_role: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """只读预览：算清楚"改成什么、什么时候生效、影响谁"，并签发单次确认令牌。

    **不写库、不调模型、不发送、不动积分**；只有可确认的变更才签发令牌。
    """

    from bot.services.operations_runs import (
        OperationsError,
        issue_preview_token,
        preview_ttl_seconds,
    )

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    key = str(task_id or "").strip()
    if key not in POLICY_TASK_IDS:
        raise OperationsError(404, "policy_task_not_found", "没有这个运营任务")
    async with session_factory() as session:
        current = await load_policy(session, group_id=int(group_id), task_id=key)

    mode = str(mode or "").strip().lower()
    if mode == "source":
        if not current.source_switchable:
            raise OperationsError(
                409, POLICY_SOURCE_NOT_SWITCHABLE, "这个任务只有一种执行来源，不能切换"
            )
        wanted = str(source or "").strip().lower()
        if wanted not in current.source_options:
            raise OperationsError(400, POLICY_INVALID_SOURCE, "不支持的执行来源")
        proposed = {"source": wanted}
        effects = [_SOURCE_EFFECTS.get((key, wanted), "变更本群的执行来源。")]
        detail = (
            "来源没有变化，不需要确认"
            if wanted == current.source
            else f"执行来源将从 {current.source} 改为 {wanted}（只影响未来）"
        )
        confirmable = wanted != current.source
        next_anchor = None
    elif mode == "plan":
        if not current.plan_supported:
            raise OperationsError(409, POLICY_PLAN_UNSUPPORTED, "这个任务没有周期计划")
        if not isinstance(enabled, bool):
            raise OperationsError(400, POLICY_INVALID_PLAN, "enabled 必须是布尔值")
        day = _weekday_ok(weekday)
        clock = _minute_ok(minute)
        if enabled and (day is None or clock is None):
            # 界面不许预填一个"看起来已启用"的值：必须显式选周内日与时刻。
            raise OperationsError(
                400, POLICY_INVALID_PLAN, "开启周期计划必须明确选择周内日与时刻"
            )
        next_anchor = (
            next_occurrence(moment, int(day), int(clock)) if enabled else None
        )
        proposed = {
            "plan_enabled": bool(enabled),
            "plan_weekday": day if enabled else None,
            "plan_minute": clock if enabled else None,
        }
        effects = [
            _plan_effects(
                enabled=bool(enabled), weekday=day, minute=clock, anchor=next_anchor
            )
        ]
        unchanged = (
            bool(enabled) == bool(current.plan_enabled)
            and (not enabled or (day == current.plan_weekday and clock == current.plan_minute))
        )
        confirmable = not unchanged
        detail = (
            "周期计划没有变化，不需要确认"
            if unchanged
            else ("开启周报周期计划（只影响未来）" if enabled else "关闭周报周期计划")
        )
    else:
        raise OperationsError(400, "invalid_policy_mode", "mode 只能是 source 或 plan")

    token = ""
    expires_at = None
    ttl = preview_ttl_seconds(settings)
    if confirmable:
        token, binding = issue_preview_token(
            settings,
            task_id=f"{POLICY_TASK_PREFIX}{key}",
            group_id=int(group_id),
            actor_user_id=int(actor_user_id),
            actor_role=str(actor_role or ""),
            guard_revision=int(current.revision),
            params={
                "mode": mode,
                "expect_revision": int(current.revision),
                **proposed,
            },
            target_keys=(),
            policy_revision=int(current.revision),
            now=moment,
        )
        expires_at = _iso(binding.expires_at)
    return {
        "task_id": key,
        "mode": mode,
        "group_id": int(group_id),
        "current": current.as_dict(),
        "proposed": proposed,
        "effects": list(effects),
        "detail": detail,
        "confirmable": bool(confirmable),
        "reason": "" if confirmable else "no_change",
        "next_anchor_at": _iso(next_anchor),
        "timezone": "Asia/Shanghai",
        "external_schedule_known": False,
        "external_schedule_reason": "external_schedule_unknown",
        "token": token,
        "expires_at": expires_at,
        "ttl_seconds": int(ttl),
        "read_only": True,
    }


async def confirm_policy(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    *,
    token: object,
    group_id: int,
    actor_user_id: int,
    actor_role: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """确认一次策略变更：令牌校验 → 同一条 SQL 的 CAS → 审计 → 持久读回。"""

    from bot.services.operations_runs import OperationsError, parse_preview_token

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    binding = parse_preview_token(settings, token, now=moment)
    raw_task = str(binding.task_id)
    if not raw_task.startswith(POLICY_TASK_PREFIX):
        raise OperationsError(409, "policy_token_mismatch", "这个令牌不是策略变更的令牌")
    key = raw_task[len(POLICY_TASK_PREFIX) :]
    if key not in POLICY_TASK_IDS:
        raise OperationsError(404, "policy_task_not_found", "没有这个运营任务")
    if int(binding.group_id) != int(group_id):
        raise OperationsError(403, "preview_group_mismatch", "这个令牌不是本群的")
    if int(binding.actor_user_id) != int(actor_user_id):
        raise OperationsError(403, "preview_actor_mismatch", "这个令牌不是你的")
    if actor_role and binding.actor_role and str(binding.actor_role) != str(actor_role):
        # 角色变了（例如已经不是超管）：不认旧令牌，要求重新预览。
        raise OperationsError(403, "preview_role_changed", "你的角色变了，请重新预览")

    params = dict(binding.params or {})
    mode = str(params.get("mode") or "")
    expected = int(params.get("expect_revision") or 0)
    async with session_factory() as session:
        if mode == "source":
            outcome = await switch_source(
                session,
                group_id=int(group_id),
                task_id=key,
                source=params.get("source"),
                expected_revision=expected,
                actor_user_id=int(actor_user_id),
                now=moment,
            )
            action = "执行来源变更"
        elif mode == "plan":
            outcome = await set_weekly_plan(
                session,
                group_id=int(group_id),
                task_id=key,
                enabled=bool(params.get("plan_enabled")),
                weekday=params.get("plan_weekday"),
                minute=params.get("plan_minute"),
                expected_revision=expected,
                actor_user_id=int(actor_user_id),
                now=moment,
            )
            action = "周期计划变更"
        else:
            raise OperationsError(400, "invalid_policy_mode", "mode 只能是 source 或 plan")

        if not outcome.ok:
            await session.rollback()
            status = {
                POLICY_REVISION_CONFLICT: 409,
                POLICY_SOURCE_NOT_SWITCHABLE: 409,
                POLICY_PLAN_UNSUPPORTED: 409,
                POLICY_INVALID_SOURCE: 400,
                POLICY_INVALID_PLAN: 400,
            }.get(outcome.code, 409)
            raise OperationsError(status, outcome.code, outcome.detail)
        await session.commit()
        saved = await load_policy(session, group_id=int(group_id), task_id=key)
    return {
        "policy": policy_payload(saved),
        "confirmed": True,
        "action": action,
        "detail": outcome.detail,
    }


async def read_policy_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    limit: int = 20,
    cursor: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """本群策略变更审计：有界 keyset 分页，只读、只本群、不返回原异常文本。"""

    from bot.db.models import GroupTaskPolicyEvent as Event

    size = max(1, min(int(limit), 50))
    wanted = str(task_id or "").strip() or None
    async with session_factory() as session:
        query = select(Event).where(Event.group_id == int(group_id))
        if wanted:
            query = query.where(Event.task_id == wanted)
        if cursor is not None:
            query = query.where(Event.id < int(cursor))
        rows = (
            await session.scalars(query.order_by(Event.id.desc()).limit(size + 1))
        ).all()
    has_more = len(rows) > size
    visible = list(rows)[:size]
    return {
        "group_id": int(group_id),
        "task_id": wanted,
        "limit": size,
        "has_more": has_more,
        "next_cursor": int(visible[-1].id) if has_more and visible else None,
        "items": [
            {
                "id": int(row.id),
                "task_id": str(row.task_id),
                "action": str(row.action),
                "actor_user_id": int(row.actor_user_id or 0),
                "revision": int(row.revision or 0),
                "before": dict(row.before or {}),
                "after": dict(row.after or {}),
                "detail": str(row.detail or ""),
                "created_at": _iso(row.created_at),
            }
            for row in visible
        ],
    }


__all__ = [
    "DEFAULT_SOURCE",
    "PLAN_TASKS",
    "POLICY_INVALID_PLAN",
    "POLICY_INVALID_SOURCE",
    "POLICY_OK",
    "POLICY_PLAN_UNSUPPORTED",
    "POLICY_REVISION_CONFLICT",
    "POLICY_SOURCE_NOT_SWITCHABLE",
    "POLICY_TASK_IDS",
    "SLOT_DUE_GRACE_SECONDS",
    "SOURCE_EXTERNAL",
    "SOURCE_INTERNAL",
    "SOURCE_SWITCHABLE",
    "PolicyWriteOutcome",
    "TaskPolicyView",
    "advance_anchor",
    "default_source",
    "due_plans",
    "due_slots",
    "is_switchable",
    "load_policies",
    "load_policy",
    "load_task_policies",
    "POLICY_TASK_PREFIX",
    "policy_payload",
    "WEEKDAY_LABELS",
    "confirm_policy",
    "preview_policy",
    "read_policy_audit",
    "next_occurrence",
    "normalize_source",
    "plan_fire_state",
    "plan_run_key",
    "admit_plan_run",
    "managed_group_ids",
    "plan_supported",
    "policy_revision",
    "resolve_sources",
    "set_weekly_plan",
    "slot_moment",
    "slot_moment_from_key",
    "source_options",
    "switch_source",
]
