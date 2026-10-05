"""P3-4 / F-031：``/rank`` 不能再被任何成员反复触发全历史聚合。

复现的原缺陷
------------
``bot/handlers/commands.py::cmd_rank`` 之前无冷却、无缓存：每一次调用都跑一遍
``build_rank_board`` —— 全历史聚合（签到流水 + 奖励流水 + 消费流水，``/rank week``
还要再按本周窗口过滤一遍）。任何成员连发 N 次就是 N 次全表聚合，数据库负载与处理
耗时随触发次数线性放大。同文件的 ``cmd_report`` 早就有
``_REPORT_COOLDOWN_SECONDS = 90``，``/rank`` 是漏网的那一个。

修复
----
两道闸，作用域不同、互不替代（实现处的注释写了为什么这么切）：

* **冷却**按 ``(群, 成员)``（``_RANK_COOLDOWN_SECONDS = 10``）：窗口内再发只回一句
  友好提示，**不聚合**、不查库。
* **短缓存**按 ``(群, 模式, 成员)``（``_RANK_CACHE_TTL_SECONDS = 60``）：冷却过去
  之后同一个人再发仍然不聚合，直接复用上一次的结果。

缓存键**必须带 ``user_id``**：``RankBoard.caller_rank`` 是调用者自己的名次（他可能
根本不在 Top10 里），跨成员共用一份榜会把 A 的名次报给 B。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import Group
from bot.handlers import commands
from bot.services.checkin import local_today, record_checkin

GROUP_ID = -100


def _day(offset: int = 0) -> datetime:
    """本周二 12:00 + offset 天（``record_checkin`` 的 ``now=`` 注入点）。"""

    today = local_today()
    monday = today - timedelta(days=today.weekday())
    tuesday = monday + timedelta(days=1)
    return datetime(tuesday.year, tuesday.month, tuesday.day, 12, 0, 0) + timedelta(
        days=offset
    )


def _settings():
    from bot.config import Settings

    return Settings(_env_file=None)


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubBoard:
    """只需要能被缓存住的占位对象（字典上界用例只验容量，不看内容）。"""

    mode = "all"
    entries: tuple = ()
    caller_rank = 1
    caller_points = 0
    caller_available = 0
    members = 0
    has_data = False


class RankCooldownCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=GROUP_ID, title="测试群", settings={}))
            await session.commit()
        for user_id in range(1, 6):
            async with self.session_factory() as session:
                await record_checkin(
                    session,
                    group_id=GROUP_ID,
                    user_id=user_id,
                    display_name=f"用户{user_id}",
                    now=_day(0),
                )
                await session.commit()

        # 模块级状态是进程级的，每个用例都要从干净状态起步。
        commands._rank_cooldown.clear()
        commands._rank_cache.clear()
        self.clock = _FakeClock()
        self.aggregations = 0
        self._real_board = commands.build_rank_board

    async def asyncTearDown(self) -> None:
        commands._rank_cooldown.clear()
        commands._rank_cache.clear()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    def _message(self, text: str, *, user_id: int = 1) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=GROUP_ID, type="supergroup"),
            from_user=SimpleNamespace(id=user_id, full_name="用户1", is_bot=False),
        )

    async def _run(self, text: str, *, user_id: int = 1) -> list[str]:
        answers: list[str] = []
        message = self._message(text, user_id=user_id)
        real_board = self._real_board

        async def fake_answer(message, settings, body, **kwargs):
            answers.append(body)

        async def counting_board(*args, **kwargs):
            self.aggregations += 1
            return await real_board(*args, **kwargs)

        with (
            patch.object(commands, "time", SimpleNamespace(monotonic=self.clock)),
            patch.object(commands, "_answer", side_effect=fake_answer),
            patch.object(
                commands,
                "ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch.object(commands, "build_rank_board", side_effect=counting_board),
        ):
            async with self.session_factory() as session:
                await commands.cmd_rank(message, session, _settings())
        return answers

    async def test_second_call_inside_the_cooldown_does_not_aggregate(self) -> None:
        """① 冷却期内第二次调用不重新聚合，返回友好提示。"""

        first = await self._run("/rank")
        second = await self._run("/rank")

        self.assertEqual(self.aggregations, 1, "冷却期内不得再聚合")
        self.assertIn("<b>本群积分榜</b>", first[-1])
        self.assertNotIn("<b>本群积分榜</b>", second[-1])
        self.assertIn("请等", second[-1])
        self.assertIn("秒后再发", second[-1])

    async def test_cooldown_is_per_member(self) -> None:
        first = await self._run("/rank", user_id=1)
        other = await self._run("/rank", user_id=2)

        self.assertEqual(self.aggregations, 2, "一个人的冷却不能卡住别人")
        self.assertIn("<b>本群积分榜</b>", first[-1])
        self.assertIn("<b>本群积分榜</b>", other[-1])

    async def test_call_works_again_after_the_cooldown(self) -> None:
        """② 冷却期过后正常。"""

        await self._run("/rank")
        self.clock.advance(commands._RANK_COOLDOWN_SECONDS + 1.0)
        again = await self._run("/rank")

        self.assertIn("<b>本群积分榜</b>", again[-1])

    async def test_cache_hit_serves_the_same_board_without_aggregating(self) -> None:
        """③ 缓存命中不改变输出内容（冷却之后、缓存窗口之内）。"""

        first = await self._run("/rank")
        self.clock.advance(commands._RANK_COOLDOWN_SECONDS + 1.0)
        second = await self._run("/rank")

        self.assertEqual(self.aggregations, 1, "缓存窗口内不得再聚合")
        self.assertEqual(first[-1], second[-1], "缓存命中必须逐字相同")

    async def test_cache_is_per_mode(self) -> None:
        """``/rank`` 与 ``/rank week`` 是两个口径，不能互相复用。"""

        await self._run("/rank")
        self.clock.advance(commands._RANK_COOLDOWN_SECONDS + 1.0)
        week = await self._run("/rank week")

        self.assertEqual(self.aggregations, 2)
        self.assertIn("<b>本周积分榜</b>", week[-1])

    async def test_cache_is_per_member(self) -> None:
        """名次是调用者自己的，缓存不能跨成员复用。"""

        await self._run("/rank", user_id=1)
        self.clock.advance(commands._RANK_COOLDOWN_SECONDS + 1.0)
        other = await self._run("/rank", user_id=3)

        self.assertIn("你：第 3 名", other[-1])

    async def test_cache_expires(self) -> None:
        await self._run("/rank")
        self.clock.advance(commands._RANK_CACHE_TTL_SECONDS + 1.0)
        await self._run("/rank")

        self.assertEqual(self.aggregations, 2, "缓存过期后必须重新聚合")

    async def test_cache_and_cooldown_dictionaries_stay_bounded(self) -> None:
        """长期运行不能把两个字典撑爆。"""

        for index in range(commands._RANK_CACHE_MAX_ENTRIES + 50):
            commands._rank_store_board(
                -1000 - index,
                week=False,
                user_id=index,
                board=_StubBoard(),
                now=self.clock.now,
            )
        self.assertLessEqual(
            len(commands._rank_cache), commands._RANK_CACHE_MAX_ENTRIES
        )

        for index in range(commands._RANK_COOLDOWN_MAX_ENTRIES + 50):
            commands._rank_cooldown[(-2000 - index, index)] = self.clock.now
        commands._rank_prune(self.clock.now)
        self.assertLessEqual(
            len(commands._rank_cooldown), commands._RANK_COOLDOWN_MAX_ENTRIES
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
