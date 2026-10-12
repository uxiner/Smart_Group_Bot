"""B13a 返工 v3（R2）：真实副作用准入边界上的**共享持久守卫**。

任务起点的复核（``_AdapterBase._execution_guard``）与真实副作用之间仍然隔着若干
``await``（渲染正文、排队、读统计、等锁）。父独立验收的 v10/v11 探针证明：只要
在这段时间里撤掉发起人的管理员身份、或者另一位管理员提交了新的持久配置，起点
复核就已经过期，而真实写入/发布/出站照样发生。

所以这里把"此刻还算不算合法执行"做成**可以进 SQL 谓词**的持久条件，只有五条：

* 群仍在授权列表里（``AuthorizedGroup.bot_present``）；
* 发起人此刻仍是本群管理员（真实超管按 ``settings.super_admin_id`` 豁免，与
  :func:`bot.services.operations_tasks._actor_still_authorized` 同一口径；无法复核
  的 actor 直接拒绝）；
* 持久运行时配置 revision 仍等于确认那一刻的快照（``runtime_config.revision``，
  记录缺失按 0 处理）；
* 这个 run 没有被持久化取消（``operations_runs.cancel_requested``）；
* **按群策略 revision 仍等于确认那一刻的快照**（``group_task_policies.revision``，
  没有策略行按 0；B13b/C4：来源切换或周期计划改动后，旧确认必须在真实不可逆准入
  被挡住，而不是靠一次提前的 SELECT）。

同一个谓词有两种**真正的**用法（返工 v4 更正：只有这两种才算线性化点）：

* **数据库写入边界**：把 :func:`guard_predicate` 直接编进**那条 DML 自己**
  （``INSERT ... SELECT ... WHERE <guard>``，见
  :func:`bot.services.activity._write_award`）。这样"还算不算合法执行"和"这一行
  到底写不写"是同一条语句、同一次 writer 准入：SQLite 写锁由该 DML 的
  ``session.execute`` 抢到之后才求值，撤销/改配置/取消只要在那之前 commit 就已
  生效。
* **网络出站边界**：把它交给
  :func:`bot.services.operations_receipts.claim_business_dispatch` 的 ``guard``
  参数，谓词同样进到**同一条 claim SQL** 里（``INSERT ... SELECT ... WHERE``），
  于是"抢到出站保护"就等于"这一刻仍然合法"，抢不到就绝不外发。

**反面（务必不要这样写）**：先 ``verify_guard(session, guard)`` 求一个 SELECT、
再去执行别的 DML —— 这**不是**原子的。``await session.execute(<DML>)`` 自己会去等
SQLite 的 writer 锁，等待期间另一个合法 writer 完全可以先 commit 撤销/改配置/取消；
父独立验收的 v13 探针就是把撤销 commit 放在这个等待窗口里，于是旧实现的守卫形同
虚设。:func:`verify_guard` 只适合做**提前退出**（省一次写），不能当写边界的准入。

本模块只做判定，不写库、不改状态：``guard is None``（legacy CLI、常驻 runner、
自动摘要等没有运营中心 run 上下文的路径）一律视为通过，既有行为不变。

**B13b 返工（R-B1）**：按群来源策略执行的四条自动路径（内部签到 runner、外部签到
CLI、内部商店到期服务、外部商店 CLI）过去传 ``guard=None``，于是"读了来源"与
"真正外发/撤销"之间的窗口里切换来源仍然会被旧来源执行。它们改用
:meth:`PersistedGuard.for_automatic_entry`：只绑定策略 revision + 群资格（不比对
运行时配置 revision，也没有 run 取消条件），并且同样**编进那条 DML / claim 的
WHERE**，不是提前 SELECT。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Admin,
    AuthorizedGroup,
    GroupTaskPolicy,
    OperationsRun,
    RuntimeConfigRecord,
)

__all__ = ["PersistedGuard", "guard_predicate", "verify_guard"]


@dataclass(frozen=True)
class PersistedGuard:
    """一次运营中心确认所绑定、可在 SQL 里复核的持久执行条件。"""

    group_id: int
    actor_user_id: int
    super_admin_id: int
    #: ``None`` 表示"这一条不需要比对运行时配置 revision"（自动来源入口没有运营中心
    #: run 上下文，运行时配置漂移与"该不该由本来源执行"无关）。人工/计划 run 仍然传
    #: 具体整数，比对口径逐字不变。
    guard_revision: int | None
    run_key: str
    actor_role: str = ""
    task_id: str = ""
    policy_revision: int = 0
    #: 计划触发（内部 scheduler）的 run 没有人工发起人：发起人条件不适用，其余
    #: 条件（群授权 / 运行时 revision / 取消 / 策略 revision）照样在准入处比对。
    scheduled: bool = False
    #: 自动来源入口（内部 runner / 外部 CLI 的签到、商店到期）没有 OperationsRun，
    #: 也就没有"本 run 是否被取消"这一条；置 False 只跳过取消条件，其它条件不变。
    check_run_cancel: bool = True

    @classmethod
    def for_automatic_entry(
        cls,
        *,
        group_id: int,
        task_id: str,
        policy_revision: int,
        guard_revision: int | None = None,
    ) -> "PersistedGuard":
        """自动来源入口（无 run 上下文）的守卫：只绑定**按群持久策略 revision**、
        群当前资格（``AuthorizedGroup.bot_present``）与显式传入的运行时 revision。

        这不是"预检查"：它必须被编进真正不可逆的那条 DML / claim 的 WHERE 里
        （签到时段占位、出站 dispatch claim、商店撤销前的准入写与 DELETE），否则
        来源切换仍能在"读了策略"与"真正写入"之间的窗口里被旧来源执行。
        """

        return cls(
            group_id=int(group_id),
            actor_user_id=0,
            super_admin_id=0,
            guard_revision=guard_revision,
            run_key="",
            actor_role="",
            task_id=str(task_id),
            policy_revision=int(policy_revision),
            scheduled=True,
            check_run_cancel=False,
        )

    @classmethod
    def from_context(cls, context: Any) -> "PersistedGuard":
        """从 :class:`bot.services.operations_runs.RunContext` 构造（只读属性）。"""

        settings = getattr(context, "settings", None)
        return cls(
            group_id=int(getattr(context, "group_id", 0) or 0),
            actor_user_id=int(getattr(context, "actor_user_id", 0) or 0),
            super_admin_id=int(getattr(settings, "super_admin_id", 0) or 0),
            guard_revision=int(getattr(context, "guard_revision", 0) or 0),
            run_key=str(getattr(context, "run_key", "") or ""),
            actor_role=str(getattr(context, "actor_role", "") or ""),
            task_id=str(getattr(context, "task_id", "") or ""),
            policy_revision=int(getattr(context, "policy_revision", 0) or 0),
            # 与 operations_runs.SOURCE_SCHEDULE 同一个值；这里用字面量避免把
            # 内核模块拖进 import 环。
            scheduled=str(getattr(context, "source", "") or "") == "schedule",
        )

    def describe(self) -> str:
        """只用于日志/回执，不带任何凭据或消息正文。"""

        return (
            f"group={int(self.group_id)} actor={int(self.actor_user_id)}"
            f" revision={self.guard_revision} run={self.run_key}"
            f" policy={int(self.policy_revision)}"
        )


def guard_predicate(guard: PersistedGuard) -> ColumnElement[bool]:
    """把守卫条件编译成 SQL 谓词（可放进 DML 或 claim 语句里）。"""

    group_id = int(guard.group_id)
    conditions: list[ColumnElement[bool]] = [
        exists(
            select(AuthorizedGroup.group_id).where(
                AuthorizedGroup.group_id == group_id,
                AuthorizedGroup.bot_present.is_(True),
            )
        )
    ]

    actor = int(guard.actor_user_id or 0)
    if guard.scheduled:
        # 计划触发的 run 没有发起人：发起人/管理员条件不适用（来源本身是可持久
        # 复核的策略，已由下面的 policy revision 条件覆盖）。
        pass
    elif actor <= 0:
        # 无法复核发起人（旧数据/系统路径）：不放进 SQL 就等于放行，所以直接拒绝。
        conditions.append(false())
    elif int(guard.super_admin_id or 0) != actor:
        conditions.append(
            exists(
                select(Admin.id)
                .join(AuthorizedGroup, AuthorizedGroup.group_id == Admin.group_id)
                .where(
                    Admin.group_id == group_id,
                    Admin.user_id == actor,
                    AuthorizedGroup.bot_present.is_(True),
                )
            )
        )

    # 记录缺失按 revision 0 处理：确认时快照是 0、库里仍没有记录，才算没变过。
    # ``guard_revision is None`` 只出现在自动来源入口（没有 run 上下文）：这一条
    # 不适用，跳过而不是拿 0 去比（那边真正的绑定是 policy revision + 群资格）。
    if guard.guard_revision is not None:
        conditions.append(
            func.coalesce(
                select(RuntimeConfigRecord.revision)
                .where(RuntimeConfigRecord.id == 1)
                .scalar_subquery(),
                0,
            )
            == int(guard.guard_revision or 0)
        )

    run_key = str(guard.run_key or "").strip()
    if not guard.check_run_cancel:
        # 自动来源入口没有 run，也就没有"本 run 被取消"这一条。
        pass
    elif not run_key:
        # 没有 run 上下文就不该用这个守卫：宁可拒绝，也不要无谓地放行。
        conditions.append(false())
    else:
        conditions.append(
            ~exists(
                select(OperationsRun.id).where(
                    OperationsRun.run_key == run_key,
                    OperationsRun.cancel_requested.is_(True),
                )
            )
        )

    # B13b/C4：按群策略（执行来源 / 周期计划）版本。只有确认时带了任务身份才比对；
    # 没有策略行的群是 0，与确认时快照 0 相等 ⇒ legacy 路径行为不变。
    policy_task = str(guard.task_id or "").strip()
    if policy_task:
        conditions.append(
            func.coalesce(
                select(GroupTaskPolicy.revision)
                .where(
                    GroupTaskPolicy.group_id == group_id,
                    GroupTaskPolicy.task_id == policy_task,
                )
                .scalar_subquery(),
                0,
            )
            == int(guard.policy_revision or 0)
        )

    return and_(*conditions)


async def verify_guard(session: AsyncSession, guard: PersistedGuard | None) -> bool:
    """求值守卫；``guard is None`` 表示这条路径不需要守卫。

    **这是一次普通 SELECT，不持有 SQLite writer 锁**，因此它的结论只代表"求值的
    那一刻"。它**不能**用来保证随后的 DML 一定合法：DML 自己的
    ``session.execute`` 还要去等 writer 锁，别的 writer 可以在等待期间 commit 撤销
    （父 v13 探针即此）。需要真正准入保证时，请把 :func:`guard_predicate` 编进那条
    DML / claim 语句；本函数只用于提前退出或纯读场景。
    """

    if guard is None:
        return True
    return bool(await session.scalar(select(guard_predicate(guard))))
