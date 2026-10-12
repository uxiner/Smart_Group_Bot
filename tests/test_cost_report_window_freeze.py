"""一次报表必须冻结**同一个时间窗**（v72 合同 A）。

父代理在 ``e659320`` 上实测：真实 ``init_db`` + 真实 ``record_attempt``/``record_result``
+ ``flush`` 在 2026-01-10 播种一次调用（USD 43 微单位），然后在 ``days=1`` 的报表里把
``group_costs.now_shanghai_naive`` 的**第一次**读返回 ``2026-01-10 23:59:59``、
**后续**读返回 ``2026-01-11 00:00:01``：

* 主表按 Jan 10 读到了那一行；
* 计价表按 Jan 11 读 → 读不到；
* builder 的 ``window_start`` 又写成 Jan 11。

结果 ``attempts=1 / priced=0 / legacy=1 / amounts 空 / window_start=2026-01-11``：
一笔**完整的同事务计价行**被误报成"旧数据"，报表窗口也不符合它自己的内容。

修法：getter 入口只读一次"今天"、只夹紧一次 ``days``，主表过滤 / 计价表过滤 /
builder 的 ``window_start`` 全用同一个冻结值（``today`` 是**一次调用内**的冻结，
不是跨报表的全局缓存）。

这里全部走真实 SQLite + 真实台账函数播种，用**调用侧 effect 时钟**钉住午夜，
不 sleep、不等真午夜、不改记账日期：

* 跨午夜：第一次读在午夜前、后续在午夜后 → 只许用第一次那个日期；
* 稳定午夜前 / 稳定午夜后：两个正控（后者证明窗口真的跟着时钟走，而不是钉死在数据上）；
* ``days=7`` / ``days=90``：窗口起点与过滤边界用同一个冻结基准（差一天的行必须排除）；
* 夹紧：``days=0`` 等价于 1，``days=1000`` 等价于 90。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from typing import Any
from unittest import mock

from bot.db.engine import init_db
from bot.services import group_costs
from bot.services import pricing
from bot.services import usage_scope as scopes

# 合成群 id：沿用测试套件已登记的两个假值（见 test_public_artifact_scan 的
# SYNTHETIC_IDS），不另外引入新的 -100… 数字。
GROUP_A = -100900000001
GROUP_B = -100900000002

PROVIDER = "fixture"
MODEL = "m1"
# 单价：输入 1 微/百万 token、输出 2 微/百万 token；100 输入 + 20 输出 = 140 微。
PROMPT_TOKENS = 100
COMPLETION_TOKENS = 20
AMOUNT = 140


class _Clock:
    """调用侧 effect 时钟：前 n 个值依次返回，之后一直返回最后一个。"""

    def __init__(self, *values: datetime) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def _catalog() -> pricing.PriceCatalog:
    return pricing.PriceCatalog(
        version=1,
        entries=(
            pricing.PriceEntry(
                provider_key=PROVIDER,
                model=MODEL,
                currency="USD",
                input_per_million="1",
                output_per_million="2",
            ),
        ),
    )


class _WindowBase(unittest.IsolatedAsyncioTestCase):
    """真实临时库 + 真实台账函数；价格目录按用例装配并还原。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_url = f"sqlite+aiosqlite:///{self._db_path}"
        self.engine, self.session_factory = await init_db(self.db_url)
        group_costs.reset()
        group_costs.configure(self.session_factory)
        self._previous_source = pricing.catalog_source()
        pricing.configure_catalog_source(_catalog)

    async def asyncTearDown(self) -> None:
        pricing.configure_catalog_source(self._previous_source)
        try:
            await group_costs.flush(force=True)
        except BaseException:  # pragma: no cover - 清理不该掩盖真正的失败
            pass
        group_costs.reset()
        group_costs.configure(None)  # type: ignore[arg-type]
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # ---- 写入（走真实台账函数，日期由调用侧时钟决定）------------------------

    async def _record_at(
        self, when: datetime, *, group_id: int = GROUP_A, completions: int = COMPLETION_TOKENS
    ) -> None:
        with mock.patch.object(group_costs, "now_shanghai_naive", lambda: when):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP,
                group_id=group_id,
                purpose=scopes.PURPOSE_OPTIONAL,
            ):
                attempt = group_costs.record_attempt(
                    stage="main", provider=PROVIDER, model=MODEL
                )
                group_costs.record_result(
                    attempt_id=attempt,
                    usage={
                        "prompt_tokens": PROMPT_TOKENS,
                        "completion_tokens": completions,
                    },
                )
            await group_costs.flush(force=True)

    # ---- 读取 -------------------------------------------------------------

    async def _report(
        self, *, days: int, clock: _Clock, group_id: int = GROUP_A
    ) -> dict[str, Any]:
        with mock.patch.object(group_costs, "now_shanghai_naive", clock):
            async with self.session_factory() as session:
                return await group_costs.query_group_costs_report(
                    session, group_id=group_id, days=days
                )

    def _assert_one_generation(self, report: dict[str, Any]) -> None:
        reconciliation = report["reconciliation"]
        self.assertEqual(
            reconciliation["main_attempts"],
            reconciliation["priced_table_attempts"],
            f"报表混了两代：{reconciliation}",
        )
        self.assertEqual(reconciliation["unmatched_priced_attempts"], 0)


class WindowFreezeTests(_WindowBase):
    """跨午夜与两个稳定时钟正控。"""

    async def test_crossing_midnight_uses_the_frozen_date_once(self) -> None:
        """第一次读在 23:59:59、后续读在次日 00:00:01 → 全用同一个日期。"""

        await self._record_at(datetime(2026, 1, 10, 12, 0, 0))
        clock = _Clock(
            datetime(2026, 1, 10, 23, 59, 59),
            datetime(2026, 1, 11, 0, 0, 1),
        )

        report = await self._report(days=1, clock=clock)

        self.assertEqual(report["window_start"], "2026-01-10")
        self.assertEqual(report["days"], 1)
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})
        self.assertTrue(report["amount_complete"])
        # 顺带钉住"只读一次时钟"：旧实现会读 3~4 次，两次读各拿一个日期。
        self.assertEqual(clock.calls, 1, "一次报表只许读一次时钟（冻结，不缓存）")

    async def test_stable_before_midnight_is_the_control(self) -> None:
        """正控：时钟稳定在午夜前，结果与跨午夜那次**完全一致**。"""

        await self._record_at(datetime(2026, 1, 10, 12, 0, 0))
        clock = _Clock(datetime(2026, 1, 10, 23, 59, 59))

        report = await self._report(days=1, clock=clock)

        self.assertEqual(report["window_start"], "2026-01-10")
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})

    async def test_stable_after_midnight_is_the_control(self) -> None:
        """正控：时钟稳定在午夜后 → 窗口整体右移，Jan 10 的行**不在**窗口里。"""

        await self._record_at(datetime(2026, 1, 10, 12, 0, 0))
        clock = _Clock(datetime(2026, 1, 11, 0, 0, 1))

        report = await self._report(days=1, clock=clock)

        self.assertEqual(report["window_start"], "2026-01-11")
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 0)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["amount_by_currency"], {})


class WindowBoundaryTests(_WindowBase):
    """days=7 / days=90 的窗口起点与过滤边界、以及夹紧。"""

    async def _seed_edges(self, base: date, days: int) -> tuple[date, date]:
        """在窗口起点与它前一天各播一次；返回 (起点, 起点前一天)。"""

        inside = base - timedelta(days=days - 1)
        outside = inside - timedelta(days=1)
        await self._record_at(datetime(outside.year, outside.month, outside.day, 12, 0, 0))
        await self._record_at(datetime(inside.year, inside.month, inside.day, 12, 0, 0))
        return inside, outside

    async def test_days_seven_window_start_matches_the_filter(self) -> None:
        """跨午夜 + days=7：起点用冻结基准算（Jan 4），Jan 3 那一行必须排除。"""

        base = date(2026, 1, 10)
        inside, _ = await self._seed_edges(base, 7)
        clock = _Clock(
            datetime(2026, 1, 10, 23, 59, 59),
            datetime(2026, 1, 11, 0, 0, 1),
        )

        report = await self._report(days=7, clock=clock)

        self.assertEqual(report["window_start"], inside.isoformat())
        self.assertEqual(report["days"], 7)
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})
        self.assertEqual(clock.calls, 1, "窗口起点与过滤必须用同一个冻结值")

    async def test_days_ninety_window_start_matches_the_filter(self) -> None:
        """days=90：起点 = 基准 - 89 天，前一行排除（同一冻结基准）。"""

        base = date(2026, 3, 10)
        inside, _ = await self._seed_edges(base, 90)
        clock = _Clock(datetime(2026, 3, 10, 23, 59, 59), datetime(2026, 3, 11, 0, 0, 1))

        report = await self._report(days=90, clock=clock)

        self.assertEqual(report["window_start"], inside.isoformat())
        self.assertEqual(report["days"], 90)
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})
        self.assertEqual(clock.calls, 1, "窗口起点与过滤必须用同一个冻结值")

    async def test_days_are_clamped_once_for_the_whole_report(self) -> None:
        """days=0 → 1、days=1000 → 90；夹紧值同时决定过滤与 window_start。"""

        base = date(2026, 5, 20)
        await self._record_at(datetime(2026, 5, 20, 12, 0, 0))

        low = await self._report(days=0, clock=_Clock(datetime(2026, 5, 20, 23, 59, 59)))
        self.assertEqual(low["days"], 1)
        self.assertEqual(low["window_start"], base.isoformat())
        self._assert_one_generation(low)
        self.assertEqual(low["reconciliation"]["main_attempts"], 1)
        self.assertEqual(low["totals"]["amount_by_currency"], {"USD": AMOUNT})

        high = await self._report(days=1000, clock=_Clock(datetime(2026, 5, 20, 23, 59, 59)))
        self.assertEqual(high["days"], 90)
        self.assertEqual(
            high["window_start"], (base - timedelta(days=89)).isoformat()
        )
        self._assert_one_generation(high)
        self.assertEqual(high["reconciliation"]["main_attempts"], 1)

    async def test_group_other_than_the_one_reported_is_not_mixed_in(self) -> None:
        """同一个冻结窗口里，别的群的同一天行不许混进本群报表。"""

        await self._record_at(datetime(2026, 2, 1, 12, 0, 0), group_id=GROUP_A)
        await self._record_at(datetime(2026, 2, 1, 12, 0, 0), group_id=GROUP_B)

        report = await self._report(
            days=1, clock=_Clock(datetime(2026, 2, 1, 12, 0, 0)), group_id=GROUP_A
        )

        self.assertEqual(report["window_start"], "2026-02-01")
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
