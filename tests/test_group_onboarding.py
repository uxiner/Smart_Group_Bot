"""新群接入申请的状态机（Task 1）：数据层合同，不碰 HTTP 与 Telegram。

被钉死的不变量：

* **申请 ≠ 授权**：提交申请只写 ``group_onboarding_requests``，
  ``authorized_groups`` 一行都不许多；只有 approve 才同时授予群授权与该申请人
  本群后台管理权。
* **单群仅一条有效 pending**：靠**数据库唯一约束**（部分唯一索引）而不是
  ``SELECT`` → ``INSERT`` 的裸竞态，所以并发提交只有一条落库、另一条拿到同一条。
* **终态不可回退**：已拒绝 / 已撤回 / 已批准的申请都批准不了。
* **幂等**：同一条重复 approve 不报错也不重复授予；重复提交不产生第二条。
* **原子**：状态翻转与授权授予在**同一个事务**里；授予失败时状态必须原样回滚，
  绝不留下「显示已批准但群没授权」的中间态。
* **可选预设白名单**：preset_id 只能是目录里的三个，任意其它字符串直接拒绝。
* **分页有界**：limit 上界 100，且拒绝 0 / 负数。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from bot.db.engine import init_db
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    GroupOnboardingRequest,
)
from bot.services import authz, group_onboarding as onboarding
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009876543210
APPLICANT_ID = 7
OTHER_APPLICANT_ID = 8
REVIEWER_ID = 42


class OnboardingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _submit(
        self,
        *,
        group_id: int = GROUP_ID,
        applicant_id: int = APPLICANT_ID,
        features: object = None,
        preset_id: str | None = None,
    ) -> tuple[GroupOnboardingRequest, bool]:
        async with self.session_factory() as session:
            async with session.begin():
                return await onboarding.submit_request(
                    session,
                    group_id=group_id,
                    applicant_id=applicant_id,
                    requested_features=onboarding.normalize_features(
                        ["moderation"] if features is None else features
                    ),
                    preset_id=preset_id,
                )

    async def _approved_count(self) -> int:
        async with self.session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuthorizedGroup)
                    .where(AuthorizedGroup.group_id == GROUP_ID)
                )
                or 0
            )

    # ------------------------------------------------------------------
    # 表与索引
    # ------------------------------------------------------------------
    async def test_schema_has_one_pending_per_group_unique_index(self) -> None:
        async with self.session_factory() as session:
            ddl = (
                await session.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type='index' "
                        "AND name='ux_group_onboarding_open_request'"
                    )
                )
            ).first()
        self.assertIsNotNone(ddl, "缺少单群有效申请的唯一索引")
        assert ddl is not None
        self.assertIn("UNIQUE", str(ddl[0]).upper())
        self.assertIn("pending", str(ddl[0]))

    async def test_partial_index_actually_rejects_a_second_pending_row(self) -> None:
        """绕过服务层直接插两行 pending 也要被数据库挡住。"""

        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    GroupOnboardingRequest(
                        request_id="r1",
                        group_id=GROUP_ID,
                        applicant_id=APPLICANT_ID,
                        requested_features=["moderation"],
                        status=onboarding.STATUS_PENDING,
                    )
                )
            with self.assertRaises(IntegrityError):
                async with self.session_factory() as second:
                    async with second.begin():
                        second.add(
                            GroupOnboardingRequest(
                                request_id="r2",
                                group_id=GROUP_ID,
                                applicant_id=OTHER_APPLICANT_ID,
                                requested_features=["moderation"],
                                status=onboarding.STATUS_PENDING,
                            )
                        )

    async def test_terminal_rows_do_not_block_a_new_pending(self) -> None:
        for status in (
            onboarding.STATUS_REJECTED,
            onboarding.STATUS_CANCELLED,
            onboarding.STATUS_APPROVED,
        ):
            async with self.session_factory() as session:
                async with session.begin():
                    session.add(
                        GroupOnboardingRequest(
                            request_id=f"done-{status}",
                            group_id=GROUP_ID,
                            applicant_id=APPLICANT_ID,
                            requested_features=["moderation"],
                            status=status,
                        )
                    )
        row, created = await self._submit()
        self.assertTrue(created)
        self.assertEqual(row.status, onboarding.STATUS_PENDING)

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    async def test_submit_does_not_authorize_the_group(self) -> None:
        row, created = await self._submit()
        self.assertTrue(created)
        self.assertEqual(row.status, onboarding.STATUS_PENDING)
        self.assertEqual(row.group_id, GROUP_ID)
        self.assertEqual(row.applicant_id, APPLICANT_ID)
        self.assertEqual(await self._approved_count(), 0)
        async with self.session_factory() as session:
            self.assertEqual(int(await session.scalar(select(func.count(Admin.id))) or 0), 0)

    async def test_same_applicant_retry_is_idempotent(self) -> None:
        """同一个人重试 = 幂等（网络抖了、页面刷新了）。"""

        first, created_first = await self._submit(
            features=["moderation"], preset_id=onboarding.PRESET_COMMUNITY_ID
        )
        second, created_second = await self._submit(
            features=["moderation"],
            preset_id=onboarding.PRESET_COMMUNITY_ID,
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.request_id, second.request_id)

    async def test_different_applicant_conflict_leaks_nothing(self) -> None:
        """同群已有别人的 pending 时，第二个人拿到的必须是**稳定通用**冲突。

        原实现把别人的整条申请（含 request_id / 申请人 / 填的功能 / 预设 /
        理由）原样返回给第二个人，只因为"两人碰巧都是 Telegram 群管理员"。
        群管理员身份说明他能在群里管事，不说明他有权读另一个人接入申请；
        而 request_id 是可以直接拿去 cancel/approve 的句柄。
        """

        first, _ = await self._submit(features=["moderation"])
        with self.assertRaises(onboarding.OnboardingPendingConflict) as caught:
            await self._submit(
                applicant_id=OTHER_APPLICANT_ID,
                features=["vision"],
                preset_id=onboarding.PRESET_COMPANION_ID,
            )
        rendered = str(caught.exception)
        self.assertNotIn(first.request_id, rendered)
        self.assertNotIn(str(OTHER_APPLICANT_ID), rendered)
        self.assertNotIn(onboarding.PRESET_COMPANION_ID, rendered)
        self.assertEqual(caught.exception.code, "already_pending")

    async def test_different_applicant_conflict_is_detected_under_concurrency(
        self,
    ) -> None:
        """并发时也要分辨，不能只"先查后插"。"""

        results = await asyncio.gather(
            *(
                self._submit(applicant_id=APPLICANT_ID + index)
                for index in range(8)
            ),
            return_exceptions=True,
        )
        created = [item for item in results if isinstance(item, tuple)]
        conflicts = [
            item
            for item in results
            if isinstance(item, onboarding.OnboardingPendingConflict)
        ]
        self.assertEqual(len(created), 1, [type(r).__name__ for r in results])
        self.assertEqual(len(conflicts), 7)
        winner_id = created[0][0].applicant_id
        for conflict in conflicts:
            self.assertNotIn(str(winner_id), str(conflict))

    async def test_same_applicant_concurrent_retry_is_still_idempotent(self) -> None:
        results = await asyncio.gather(
            *(self._submit() for _ in range(6)),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sum(1 for _row, flag in results if flag), 1)
        self.assertEqual(len({row.request_id for row, _f in results}), 1)

    async def test_concurrent_submits_create_exactly_one_pending(self) -> None:
        results = await asyncio.gather(
            *(
                self._submit(features=["moderation"])
                for _ in range(8)
            )
        )
        created_flags = [flag for _row, flag in results]
        self.assertEqual(sum(1 for flag in created_flags if flag), 1)
        self.assertEqual(len({row.request_id for row, _flag in results}), 1)
        async with self.session_factory() as session:
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GroupOnboardingRequest)
                    .where(GroupOnboardingRequest.group_id == GROUP_ID)
                )
                or 0
            )
        self.assertEqual(total, 1)

    async def test_other_group_has_its_own_pending(self) -> None:
        first, _ = await self._submit()
        second, created = await self._submit(group_id=OTHER_GROUP_ID)
        self.assertTrue(created)
        self.assertNotEqual(first.request_id, second.request_id)

    async def test_invalid_group_id_is_rejected(self) -> None:
        for bad in (0, -0, 1000, "abc", None):
            with self.assertRaises(onboarding.OnboardingInvalid):
                onboarding.normalize_group_id(bad)

    async def test_unknown_feature_and_preset_are_rejected(self) -> None:
        with self.assertRaises(onboarding.OnboardingInvalid):
            onboarding.normalize_features(["moderation", "root-shell"])
        with self.assertRaises(onboarding.OnboardingInvalid):
            onboarding.normalize_preset_id("give-me-everything")
        self.assertEqual(onboarding.normalize_preset_id(""), None)

    async def test_feature_list_is_deduplicated_and_bounded(self) -> None:
        features = onboarding.normalize_features(
            ["moderation", "moderation", "casual_chat", "vision"]
        )
        self.assertEqual(features, ["moderation", "casual_chat", "vision"])
        with self.assertRaises(onboarding.OnboardingInvalid):
            onboarding.normalize_features(["moderation"] * 200)

    # ------------------------------------------------------------------
    # 批准 / 拒绝 / 撤回
    # ------------------------------------------------------------------
    async def _approve(self, request_id: str, *, reviewer_id: int = REVIEWER_ID):
        async with self.session_factory() as session:
            async with session.begin():
                return await onboarding.approve_request(
                    session, request_id=request_id, reviewer_id=reviewer_id
                )

    async def test_approve_grants_group_and_applicant_backend_rights(self) -> None:
        row, _ = await self._submit()
        outcome = await self._approve(row.request_id)
        self.assertEqual(outcome.status, onboarding.STATUS_APPROVED)
        self.assertFalse(outcome.already_decided)
        self.assertTrue(outcome.group_authorized)
        self.assertTrue(outcome.applicant_granted)
        async with self.session_factory() as session:
            self.assertEqual(await self._approved_count(), 1)
            admin = await session.scalar(
                select(Admin).where(
                    Admin.group_id == GROUP_ID,
                    Admin.user_id == APPLICANT_ID,
                )
            )
            self.assertIsNotNone(admin)
            stored = await onboarding.find_request(session, row.request_id)
            assert stored is not None
            self.assertEqual(stored.reviewed_by, REVIEWER_ID)
            self.assertIsNotNone(stored.reviewed_at)
            self.assertEqual(stored.revision, 2)

    async def test_approve_never_grants_global_admin(self) -> None:
        row, _ = await self._submit()
        await self._approve(row.request_id)
        async with self.session_factory() as session:
            admins = (await session.scalars(select(Admin))).all()
        self.assertEqual([int(a.user_id) for a in admins], [APPLICANT_ID])
        self.assertEqual({int(a.group_id) for a in admins}, {GROUP_ID})

    async def test_repeated_approve_is_idempotent(self) -> None:
        row, _ = await self._submit()
        first = await self._approve(row.request_id)
        second = await self._approve(row.request_id)
        self.assertFalse(first.already_decided)
        self.assertTrue(second.already_decided)
        self.assertEqual(second.status, onboarding.STATUS_APPROVED)
        async with self.session_factory() as session:
            self.assertEqual(await self._approved_count(), 1)
            self.assertEqual(
                int(await session.scalar(select(func.count(Admin.id))) or 0), 1
            )

    async def test_concurrent_approve_grants_once(self) -> None:
        row, _ = await self._submit()

        async def attempt() -> onboarding.ApproveOutcome:
            async with self.session_factory() as session:
                try:
                    async with session.begin():
                        return await onboarding.approve_request(
                            session, request_id=row.request_id, reviewer_id=REVIEWER_ID
                        )
                except onboarding.OnboardingConflict:
                    async with session_factory_read(self.session_factory) as session:
                        return await onboarding.approve_request(
                            session,
                            request_id=row.request_id,
                            reviewer_id=REVIEWER_ID,
                        )

        outcomes = await asyncio.gather(*(attempt() for _ in range(6)))
        self.assertTrue(all(outcome.status == onboarding.STATUS_APPROVED for outcome in outcomes))
        self.assertEqual(await self._approved_count(), 1)
        async with self.session_factory() as session:
            self.assertEqual(
                int(await session.scalar(select(func.count(Admin.id))) or 0), 1
            )

    async def test_rejected_request_cannot_be_approved(self) -> None:
        row, _ = await self._submit()
        async with self.session_factory() as session:
            async with session.begin():
                rejected = await onboarding.reject_request(
                    session,
                    request_id=row.request_id,
                    reviewer_id=REVIEWER_ID,
                    reason="配额已满",
                )
        self.assertEqual(rejected.status, onboarding.STATUS_REJECTED)
        async with self.session_factory() as session:
            async with session.begin():
                with self.assertRaises(onboarding.OnboardingConflict):
                    await onboarding.approve_request(
                        session, request_id=row.request_id, reviewer_id=REVIEWER_ID
                    )
        self.assertEqual(await self._approved_count(), 0)

    async def test_cancelled_request_cannot_be_approved(self) -> None:
        row, _ = await self._submit()
        async with self.session_factory() as session:
            async with session.begin():
                cancelled = await onboarding.cancel_request(
                    session,
                    request_id=row.request_id,
                    actor_id=APPLICANT_ID,
                    actor_is_super_admin=False,
                )
        self.assertEqual(cancelled.status, onboarding.STATUS_CANCELLED)
        async with self.session_factory() as session:
            async with session.begin():
                with self.assertRaises(onboarding.OnboardingConflict):
                    await onboarding.approve_request(
                        session, request_id=row.request_id, reviewer_id=REVIEWER_ID
                    )

    async def test_cancelled_row_does_not_block_a_fresh_pending(self) -> None:
        row, _ = await self._submit()
        async with self.session_factory() as session:
            async with session.begin():
                await onboarding.cancel_request(
                    session,
                    request_id=row.request_id,
                    actor_id=APPLICANT_ID,
                    actor_is_super_admin=False,
                )
        fresh, created = await self._submit()
        self.assertTrue(created)
        self.assertNotEqual(fresh.request_id, row.request_id)

    async def test_stranger_cannot_cancel_another_applicants_request(self) -> None:
        row, _ = await self._submit()
        async with self.session_factory() as session:
            async with session.begin():
                with self.assertRaises(onboarding.OnboardingForbidden):
                    await onboarding.cancel_request(
                        session,
                        request_id=row.request_id,
                        actor_id=OTHER_APPLICANT_ID,
                        actor_is_super_admin=False,
                    )
        async with self.session_factory() as session:
            stored = await onboarding.find_request(session, row.request_id)
            assert stored is not None
        self.assertEqual(stored.status, onboarding.STATUS_PENDING)

    async def test_cancel_conflicts_on_a_terminal_request(self) -> None:
        row, _ = await self._submit()
        await self._approve(row.request_id)
        async with self.session_factory() as session:
            async with session.begin():
                with self.assertRaises(onboarding.OnboardingConflict):
                    await onboarding.cancel_request(
                        session,
                        request_id=row.request_id,
                        actor_id=REVIEWER_ID,
                        actor_is_super_admin=True,
                    )

    async def test_failed_grant_rolls_the_status_back(self) -> None:
        """授权授予失败时，状**必须**留在 pending —— 不允许出现"已批准但没授权"。"""

        row, _ = await self._submit()

        async def boom(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("authorization storage unavailable")

        original = onboarding.authorize_group
        onboarding.authorize_group = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                await self._approve(row.request_id)
        finally:
            onboarding.authorize_group = original  # type: ignore[assignment]

        async with self.session_factory() as session:
            stored = await onboarding.find_request(session, row.request_id)
            assert stored is not None
        self.assertEqual(stored.status, onboarding.STATUS_PENDING)
        self.assertEqual(stored.revision, 1)
        self.assertEqual(await self._approved_count(), 0)
        # 事务回滚之后同一群仍然是那一条 pending：重新提交幂等拿到**同一条**，
        # 不会因为回滚凭空长出第二条让审理队列出现幽灵。
        again, created = await self._submit()
        self.assertFalse(created)
        self.assertEqual(again.request_id, row.request_id)

    async def test_replaying_an_old_approval_never_revokes_revocation(self) -> None:
        """显式撤销必须压过一条**旧**批准。

        超管撤权是当前的、明确的决定；重放三个月前那条"已批准"记录不该把它
        顶掉 —— 那等于让任何人只要点两下按钮就能撤销管理员的撤权动作，而且
        界面上看不出发生过任何事。要恢复服务，走既有的显式授权命令，或让群
        重新提交一条申请再审批。
        """

        row, _ = await self._submit()
        await self._approve(row.request_id)

        # 超管显式撤销群授权与申请人后台权。
        async with self.session_factory() as session:
            async with session.begin():
                await authz.deauthorize_group(session, GROUP_ID)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            self.assertEqual(
                int(await session.scalar(select(func.count(Admin.id))) or 0), 0
            )

        outcome = await self._approve(row.request_id)
        self.assertTrue(outcome.already_decided)
        self.assertFalse(outcome.group_authorized)
        self.assertFalse(outcome.applicant_granted)
        async with self.session_factory() as session:
            self.assertIsNone(
                await session.get(AuthorizedGroup, GROUP_ID),
                "重放旧批准不得复活 AuthorizedGroup",
            )
            self.assertEqual(
                int(await session.scalar(select(func.count(Admin.id))) or 0), 0
            )
            # 历史决定与审计原样保留。
            stored = await onboarding.find_request(session, row.request_id)
            self.assertEqual(stored.status, onboarding.STATUS_APPROVED)
            self.assertEqual(int(stored.reviewed_by), REVIEWER_ID)
            self.assertEqual(int(stored.revision), 2)

    async def test_replayed_approval_does_not_reinsert_a_revoked_admin(self) -> None:
        """只撤销某个 Admin 而保留群授权时，重放批准不得补回那个 Admin。"""

        row, _ = await self._submit()
        await self._approve(row.request_id)
        async with self.session_factory() as session:
            async with session.begin():
                await authz.deauthorize_group_admin(
                    session, GROUP_ID, APPLICANT_ID
                )
        outcome = await self._approve(row.request_id)
        self.assertFalse(outcome.applicant_granted)
        async with self.session_factory() as session:
            self.assertEqual(
                int(await session.scalar(select(func.count(Admin.id))) or 0), 0
            )
            # 群授权本身仍在（这次没人撤销它）。
            self.assertIsNotNone(await session.get(AuthorizedGroup, GROUP_ID))

    async def test_replayed_approval_does_not_mark_a_left_bot_as_present(self) -> None:
        """bot 离群（bot_present=False）后，重放批准不得把它标回 present。"""

        row, _ = await self._submit()
        await self._approve(row.request_id)
        async with self.session_factory() as session:
            async with session.begin():
                await authz.set_group_bot_present(session, GROUP_ID, present=False)
        outcome = await self._approve(row.request_id)
        self.assertFalse(outcome.group_authorized)
        async with self.session_factory() as session:
            authorized = await session.get(AuthorizedGroup, GROUP_ID)
            self.assertIsNotNone(authorized)
            self.assertFalse(bool(authorized.bot_present))
            # 授权演员也不该被这次重放改写。
            self.assertEqual(int(authorized.authorized_by), REVIEWER_ID)

    async def test_replayed_approval_never_overwrites_the_original_audit(self) -> None:
        """重复 approve 不得改写 reviewer / 时间 / reason / revision。"""

        row, _ = await self._submit()
        first = await self._approve(row.request_id, )
        async with self.session_factory() as session:
            before = await onboarding.find_request(session, row.request_id)
            snapshot = (
                int(before.reviewed_by or 0),
                before.reviewed_at,
                str(before.reason or ""),
                int(before.revision),
            )
        await self._approve(row.request_id, reviewer_id=OTHER_APPLICANT_ID)
        async with self.session_factory() as session:
            after = await onboarding.find_request(session, row.request_id)
        self.assertEqual(
            (
                int(after.reviewed_by or 0),
                after.reviewed_at,
                str(after.reason or ""),
                int(after.revision),
            ),
            snapshot,
            "重放批准改写了历史审计",
        )
        self.assertTrue(first.already_decided is False)

    async def test_concurrent_replay_and_revocation_never_silently_reauthorizes(
        self,
    ) -> None:
        """并发「旧批准重放」与「显式撤销」：终态不得被悄悄复权。"""

        row, _ = await self._submit()
        await self._approve(row.request_id)

        async def revoke() -> None:
            async with self.session_factory() as session:
                async with session.begin():
                    await authz.deauthorize_group(session, GROUP_ID)

        async def replay() -> onboarding.ApproveOutcome:
            async with self.session_factory() as session:
                async with session.begin():
                    return await onboarding.approve_request(
                        session, request_id=row.request_id, reviewer_id=REVIEWER_ID
                    )

        results = await asyncio.gather(revoke(), replay(), return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException) and not isinstance(item, Exception):
                raise item
        async with self.session_factory() as session:
            authorized = await session.get(AuthorizedGroup, GROUP_ID)
            admins = int(await session.scalar(select(func.count(Admin.id))) or 0)
        # 两种终局都可以（取决于谁先拿到写锁），但**授权状态必须自洽**：
        # 有授权 ⇒ bot_present 为真；有 Admin ⇒ 有授权。绝不出现"行被重建了
        # 但没人知道它是被复活的"。
        if authorized is None:
            self.assertEqual(admins, 0)
        else:
            self.assertEqual(admins, 1)
            self.assertTrue(bool(authorized.bot_present))
        # 无论哪种终局，重放的返回值都不能声称"这次授予了授权"。
        for item in results:
            if isinstance(item, onboarding.ApproveOutcome):
                self.assertFalse(item.group_authorized)
                self.assertFalse(item.applicant_granted)

    async def test_current_authorization_is_reported_separately_from_the_decision(
        self,
    ) -> None:
        """历史「已批准」与当前「是否真的开通」必须能分别读出来。"""

        row, _ = await self._submit()
        await self._approve(row.request_id)
        async with self.session_factory() as session:
            live = await onboarding.current_authorization(session, GROUP_ID)
        self.assertTrue(live["authorized"])
        self.assertTrue(live["bot_present"])
        async with self.session_factory() as session:
            async with session.begin():
                await authz.set_group_bot_present(session, GROUP_ID, present=False)
            live = await onboarding.current_authorization(session, GROUP_ID)
        self.assertFalse(live["authorized"])
        self.assertFalse(live["bot_present"])

    async def test_unknown_request_id_raises_not_found(self) -> None:
        async with self.session_factory() as session:
            with self.assertRaises(onboarding.OnboardingNotFound):
                await onboarding.get_request(session, "does-not-exist")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def test_listing_is_scoped_to_the_applicant(self) -> None:
        mine, _ = await self._submit()
        theirs, _ = await self._submit(group_id=OTHER_GROUP_ID, applicant_id=OTHER_APPLICANT_ID)
        async with self.session_factory() as session:
            rows = await onboarding.list_requests(session, applicant_id=APPLICANT_ID)
        self.assertEqual([row.request_id for row in rows], [mine.request_id])
        self.assertNotIn(theirs.request_id, {row.request_id for row in rows})
        async with self.session_factory() as session:
            everyone = await onboarding.list_requests(session, applicant_id=None)
        self.assertEqual(len(everyone), 2)

    async def test_listing_page_limit_is_bounded(self) -> None:
        for index in range(3):
            await self._submit(group_id=GROUP_ID - index)
        async with self.session_factory() as session:
            with self.assertRaises(onboarding.OnboardingInvalid):
                await onboarding.list_requests(session, limit=0)
            with self.assertRaises(onboarding.OnboardingInvalid):
                await onboarding.list_requests(session, limit=101)
            rows = await onboarding.list_requests(session, limit=onboarding.MAX_PAGE_LIMIT)
        self.assertLessEqual(len(rows), 3)

    async def test_request_document_never_exposes_other_groups(self) -> None:
        row, _ = await self._submit()
        document = onboarding.request_document(row)
        self.assertEqual(document["group_id"], GROUP_ID)
        self.assertEqual(document["status"], onboarding.STATUS_PENDING)
        self.assertNotIn("authorized_by", document)
        self.assertIn("created_at", document)

    # ------------------------------------------------------------------
    # 迁移 / 重启
    # ------------------------------------------------------------------
    async def test_restart_on_an_existing_database_is_non_destructive(self) -> None:
        row, _ = await self._submit()
        await self._approve(row.request_id)
        await self.engine.dispose()
        # 模拟重启：重新对同一个库文件跑一遍既有 init_db。
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            stored = await onboarding.find_request(session, row.request_id)
            assert stored is not None
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, onboarding.STATUS_APPROVED)
            self.assertEqual(int(await session.scalar(select(func.count(Admin.id))) or 0), 1)
            group = await session.get(Group, GROUP_ID)
            self.assertIsNone(group)  # 接入申请从不凭空创建群设置行

    async def test_created_at_uses_the_shanghai_clock(self) -> None:
        row, _ = await self._submit()
        self.assertIsNotNone(row.created_at)
        self.assertEqual(
            row.created_at.date().isoformat(),
            now_shanghai_naive().date().isoformat(),
        )


class session_factory_read:
    """给并发 approve 测试用的读会话（每次进入新建一个连接）。"""

    def __init__(self, factory) -> None:
        self._factory = factory

    async def __aenter__(self):
        self._session = self._factory()
        await self._session.__aenter__()
        return self._session

    async def __aexit__(self, *exc_info) -> None:
        await self._session.__aexit__(*exc_info)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()