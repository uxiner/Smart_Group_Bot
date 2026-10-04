"""``DbSessionMiddleware`` 的提交失败处理（A-01）。

中间件在 handler 返回后统一 ``commit()``。``commit()`` 抛 ``IntegrityError``
时它会先 ``rollback()``——而 commit 失败意味着**整笔事务**失败，回滚丢掉的是
这次 update 里 handler 累积的**全部**写入，不只是 ``groups`` 那一行。旧实现
只凭错误串里出现 ``UNIQUE constraint failed: groups.id`` 就把异常吞掉并正常
返回，于是用户收到「已生效」，库里其它改动却被静默丢弃。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from sqlalchemy.exc import IntegrityError

from bot.db.engine import init_db
from bot.db.models import Group, MemberCheckin
from bot.middlewares.db import DbSessionMiddleware


class _Event:
    """中间件只把它原样传给 handler，用不到任何字段。"""


class DbSessionMiddlewareCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.middleware = DbSessionMiddleware(self.session_factory)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _rows(self, group_id: int) -> tuple[list[str], list[int]]:
        from sqlalchemy import select

        async with self.session_factory() as session:
            groups = list(
                (
                    await session.execute(
                        select(Group.title).where(Group.id == group_id)
                    )
                )
                .scalars()
                .all()
            )
            checkins = list(
                (
                    await session.execute(
                        select(MemberCheckin.user_id).where(
                            MemberCheckin.group_id == group_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return groups, checkins

    async def test_a_pure_group_insert_race_is_still_swallowed(self) -> None:
        """良性竞态不能被改坏：两个 update 抢建同一个群，第二个照旧被吞掉。"""

        async def handler(event, data):
            session = data["session"]
            session.add(Group(id=-100, title="先到先得", settings={}))
            return "ok"

        result = await self.middleware(handler, _Event(), {})

        self.assertEqual(result, "ok")
        self.assertEqual(await self._rows(-100), (["先到先得"], []))

    async def test_other_unique_violations_still_propagate(self) -> None:
        async with self.session_factory() as setup:
            setup.add(
                MemberCheckin(
                    group_id=-100, user_id=7, checkin_date="2026-10-05", points=1
                )
            )
            await setup.commit()

        async def handler(event, data):
            # 同一个本地自然日的第二次签到：撞 UNIQUE(member_checkins)
            data["session"].add(
                MemberCheckin(
                    group_id=-100, user_id=7, checkin_date="2026-10-05", points=1
                )
            )
            return "ok"

        with self.assertRaises(IntegrityError):
            await self.middleware(handler, _Event(), {})

    async def test_a_group_race_does_not_swallow_the_rest_of_the_transaction(self) -> None:
        """同事务里还挂着别的写入时，不许把异常当良性竞态吞掉。

        旧实现在这里会正常返回：handler 已经把「已生效」发出去了，而这一笔
        ``member_checkins`` 被整笔回滚静默丢弃，库里查无此事。
        """

        async with self.session_factory() as setup:
            # 另一个 update 已经把群建好了 → 这次 insert 必然撞 UNIQUE(groups.id)
            setup.add(Group(id=-100, title="先到先得", settings={}))
            await setup.commit()

        async def handler(event, data):
            session = data["session"]
            session.add(Group(id=-100, title="撞车", settings={}))
            session.add(
                MemberCheckin(
                    group_id=-100, user_id=7, checkin_date="2026-10-05", points=1
                )
            )
            return "ok"

        with self.assertRaises(IntegrityError):
            await self.middleware(handler, _Event(), {})

        # 整笔事务真的回滚了：群标题没被改掉，签到行也没留下
        self.assertEqual(await self._rows(-100), (["先到先得"], []))

    async def test_a_dirty_group_alongside_other_objects_is_not_swallowed(self) -> None:
        """已存在的群 + 同事务里的其它对象，同样不算良性竞态。"""

        async with self.session_factory() as setup:
            setup.add(Group(id=-100, title="旧标题", settings={}))
            await setup.commit()

        async def handler(event, data):
            session = data["session"]
            data["session"].add(
                MemberCheckin(
                    group_id=-100, user_id=9, checkin_date="2026-10-05", points=1
                )
            )
            row = await session.get(Group, -100)
            row.title = "新标题"
            # 制造一个 groups.id 冲突：handler 里再插一行同 id 的群
            session.add(Group(id=-100, title="撞车", settings={}))
            return "ok"

        with self.assertRaises(IntegrityError):
            await self.middleware(handler, _Event(), {})

        titles, checkins = await self._rows(-100)
        self.assertEqual(titles, ["旧标题"], "整笔事务都该回滚，标题不能被改掉")
        self.assertEqual(checkins, [], "同事务的签到行也不能留下")
