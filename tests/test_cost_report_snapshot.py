"""报表的双表读必须在**同一个数据库快照**里（v71 合同 A）。

父代理在冻结树 ``31bda38`` 上实测：在真实 ``query_group_costs()`` 完成主表
SELECT 之后、``query_group_costs_priced()`` 开始计价表 SELECT 之前，另一个会话
真实提交一整批新请求，报表就混了两代 ——

* 已有群：``main_attempts=5 / priced_table_attempts=6`` 却
  ``unmatched_priced_attempts=0``（两张表来自前后两代，对账对不出来）；
* 空群：``attempts=0 / priced_attempts=1 / USD=43000 / amount_complete=True``
  （在主表读到"没有"之后，计价表读到了刚提交的那一行）。

根因是 pysqlite/aiosqlite **不在 SELECT 前真正 BEGIN**：SQLAlchemy 那一层的
``async with session.begin()`` 对它是 no-op，两条 SELECT 各拿一个快照。
本文件用真实 SQLite + 真实 SQL + 真实提交把窗口钉死：

* 窗口由 SQLAlchemy 的 ``after_cursor_execute`` 事件确定 —— 主表 SELECT 真的
  执行完之后，才在**另一个线程用另一条独立连接**（``sqlite3`` + ``BEGIN
  IMMEDIATE``）提交完整一批；不 sleep、不 mock 查询、不 mock 提交；
* 结果允许是"写入前"或"写入后"，但**同一份报表不许一半一半**；
* 另有一个负控证明这个窗口是真的存在的（绕开 getter 直接分两次读，就会混），
  以及一个正控证明注入器真的写进去了、而且写在前面的批次报表读得到。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock

from sqlalchemy import event, func, select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily, LlmCostPricedDaily
from bot.services import group_costs
from bot.utils.timezone import now_shanghai_naive

# 合成群 id：沿用测试套件既有的两个假值（见 test_public_artifact_scan 的
# SYNTHETIC_IDS），不另外引入新的 -100… 数字。
GROUP_A = -100900000001
GROUP_B = -100900000002

PROVIDER = "fixture"
PURPOSE = "optional"
STAGE = "chat"
AMOUNT = 43000

_INSERT_MAIN = (
    "INSERT INTO llm_cost_daily "
    "(usage_date, scope, group_id, purpose, stage, provider, model, attempts, successes) "
    "VALUES (:d, 'group', :g, :purpose, :stage, :provider, :model, :attempts, :attempts)"
)
_INSERT_PRICED = (
    "INSERT INTO llm_cost_priced_daily "
    "(usage_date, scope, group_id, purpose, stage, provider, model, "
    " price_version, currency, attempts, priced_attempts, amount_micro) "
    "VALUES (:d, 'group', :g, :purpose, :stage, :provider, :model, "
    " 1, 'USD', :attempts, :attempts, :amount)"
)
_BUMP_MAIN = (
    "UPDATE llm_cost_daily SET attempts = attempts + :delta, "
    "successes = successes + :delta "
    "WHERE usage_date = :d AND group_id = :g AND purpose = :purpose "
    "AND stage = :stage AND provider = :provider AND model = :model"
)
_BUMP_PRICED = (
    "UPDATE llm_cost_priced_daily SET attempts = attempts + :delta, "
    "priced_attempts = priced_attempts + :delta, amount_micro = amount_micro + :amount "
    "WHERE usage_date = :d AND group_id = :g AND purpose = :purpose "
    "AND stage = :stage AND provider = :provider AND model = :model"
)


def _commit_in_own_connection(db_path: str, statements: list[tuple[str, dict]]) -> None:
    """另一条独立连接上的**真实**事务：BEGIN IMMEDIATE → 一批写 → COMMIT。"""

    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        for sql, params in statements:
            conn.execute(sql, params)
        conn.execute("COMMIT")
    finally:
        conn.close()


class _RaceHarness:
    """在主表 SELECT 真的执行完之后，用另一个线程/另一条连接提交一批。"""

    def __init__(self, engine: Any, db_path: str) -> None:
        self._engine = engine
        self._db_path = db_path
        self.fired = 0
        self.writer_errors: list[str] = []

    def install(self, trigger: str, batch: Callable[[], None]) -> None:
        @event.listens_for(self._engine.sync_engine, "after_cursor_execute")
        def _on_after_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ) -> None:  # noqa: ANN001 - SQLAlchemy 事件签名
            if self.fired:
                return
            if trigger not in (statement or ""):
                return
            self.fired += 1
            thread = threading.Thread(target=self._run_batch, args=(batch,))
            thread.start()
            thread.join(timeout=20)
            if thread.is_alive():  # pragma: no cover - 死锁才会走到
                self.writer_errors.append("注入的写入线程没有在 20s 内结束")

        self._remove = lambda: event.remove(
            self._engine.sync_engine, "after_cursor_execute", _on_after_cursor_execute
        )

    def _run_batch(self, batch: Callable[[], None]) -> None:
        try:
            batch()
        except BaseException as exc:  # noqa: BLE001 - 探针要如实上报
            self.writer_errors.append(f"{type(exc).__name__}: {exc}")

    def uninstall(self) -> None:
        remover = getattr(self, "_remove", None)
        if remover is not None:
            remover()
            self._remove = None


class CostReportSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.engine, self.session_factory = await init_db(self.db_url)
        group_costs.reset()
        self.today = now_shanghai_naive().date().isoformat()
        self.harness = _RaceHarness(self.engine, self.db_path)

    async def asyncTearDown(self) -> None:
        self.harness.uninstall()
        await self.engine.dispose()
        group_costs.reset()
        try:
            os.unlink(self.db_path)
        except OSError:  # pragma: no cover
            pass

    # ---- 脚手架 ---------------------------------------------------------

    async def _seed(
        self,
        group_id: int,
        model: str,
        *,
        attempts: int = 1,
        amount_micro: int = AMOUNT,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                LlmCostDaily(
                    usage_date=self.today,
                    scope="group",
                    group_id=group_id,
                    purpose=PURPOSE,
                    stage=STAGE,
                    provider=PROVIDER,
                    model=model,
                    attempts=attempts,
                    successes=attempts,
                )
            )
            session.add(
                LlmCostPricedDaily(
                    usage_date=self.today,
                    scope="group",
                    group_id=group_id,
                    purpose=PURPOSE,
                    stage=STAGE,
                    provider=PROVIDER,
                    model=model,
                    price_version=1,
                    currency="USD",
                    attempts=attempts,
                    priced_attempts=attempts,
                    amount_micro=amount_micro,
                    price_snapshot={"input_per_million": "1"},
                )
            )
            await session.commit()

    def _batch_insert(self, group_id: int, model: str, *, attempts: int = 1) -> Callable[[], None]:
        params = {
            "d": self.today,
            "g": group_id,
            "purpose": PURPOSE,
            "stage": STAGE,
            "provider": PROVIDER,
            "model": model,
            "attempts": attempts,
            "amount": AMOUNT,
        }
        return lambda: _commit_in_own_connection(
            self.db_path, [(_INSERT_MAIN, params), (_INSERT_PRICED, params)]
        )

    def _batch_bump(self, group_id: int, model: str, *, delta: int = 1) -> Callable[[], None]:
        params = {
            "d": self.today,
            "g": group_id,
            "purpose": PURPOSE,
            "stage": STAGE,
            "provider": PROVIDER,
            "model": model,
            "delta": delta,
            "amount": AMOUNT * delta,
        }
        return lambda: _commit_in_own_connection(
            self.db_path, [(_BUMP_MAIN, params), (_BUMP_PRICED, params)]
        )

    async def _report(self, group_id: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            return await group_costs.query_group_costs_report(
                session, group_id=group_id, days=7
            )

    def _assert_one_generation(self, report: dict[str, Any]) -> None:
        reconciliation = report["reconciliation"]
        self.assertEqual(
            reconciliation["main_attempts"],
            reconciliation["priced_table_attempts"],
            f"报表混了两代：{reconciliation}",
        )
        self.assertEqual(reconciliation["unmatched_priced_attempts"], 0, reconciliation)
        self.assertEqual(reconciliation["legacy_attempts"], 0, reconciliation)

    # ---- 竞态臂 ---------------------------------------------------------

    async def test_empty_group_first_identity_never_half_applied(self) -> None:
        """空群的首条新身份：要么整批都在，要么整批都不在。"""

        self.harness.install("FROM llm_cost_daily", self._batch_insert(GROUP_B, "m-new"))
        report = await self._report(GROUP_B)
        self.harness.uninstall()

        self.assertEqual(self.harness.fired, 1, "注入器没有命中主表 SELECT")
        self.assertEqual(self.harness.writer_errors, [])
        self._assert_one_generation(report)
        self.assertIn(
            (report["reconciliation"]["main_attempts"], report["totals"]["amount_by_currency"]),
            [(0, {}), (1, {"USD": AMOUNT})],
            f"报表只看到一代的一半：{report['reconciliation']} {report['totals']}",
        )
        self.assertTrue(report["amount_complete"])

    async def test_existing_group_new_identity_never_half_applied(self) -> None:
        """已有群：主表与计价表必须同时看到（或同时看不到）新身份。"""

        await self._seed(GROUP_A, "m-old")
        self.harness.install("FROM llm_cost_daily", self._batch_insert(GROUP_A, "m-new"))
        report = await self._report(GROUP_A)
        self.harness.uninstall()

        self.assertEqual(self.harness.fired, 1, "注入器没有命中主表 SELECT")
        self.assertEqual(self.harness.writer_errors, [])
        self._assert_one_generation(report)
        attempts = report["reconciliation"]["main_attempts"]
        self.assertIn(attempts, (1, 2))
        self.assertEqual(
            report["totals"]["amount_by_currency"],
            {"USD": AMOUNT * attempts},
            f"次数与金额不是同一代：{report['totals']}",
        )

    async def test_same_identity_update_never_half_applied(self) -> None:
        """同一个身份的更新（attempts 1→2、金额 43000→86000）也不能只看到一半。"""

        await self._seed(GROUP_A, "m-old")
        self.harness.install("FROM llm_cost_daily", self._batch_bump(GROUP_A, "m-old"))
        report = await self._report(GROUP_A)
        self.harness.uninstall()

        self.assertEqual(self.harness.fired, 1, "注入器没有命中主表 SELECT")
        self.assertEqual(self.harness.writer_errors, [])
        self._assert_one_generation(report)
        attempts = report["reconciliation"]["main_attempts"]
        self.assertIn(attempts, (1, 2))
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT * attempts})

    # ---- 控制臂 ---------------------------------------------------------

    async def test_control_batch_written_before_the_report_is_visible(self) -> None:
        """正控：先真实提交、再读报表 —— 新身份必须两条表都在。"""

        _commit_in_own_connection(
            self.db_path,
            [
                (
                    _INSERT_MAIN,
                    {
                        "d": self.today,
                        "g": GROUP_B,
                        "purpose": PURPOSE,
                        "stage": STAGE,
                        "provider": PROVIDER,
                        "model": "m-new",
                        "attempts": 1,
                        "amount": AMOUNT,
                    },
                ),
                (
                    _INSERT_PRICED,
                    {
                        "d": self.today,
                        "g": GROUP_B,
                        "purpose": PURPOSE,
                        "stage": STAGE,
                        "provider": PROVIDER,
                        "model": "m-new",
                        "attempts": 1,
                        "amount": AMOUNT,
                    },
                ),
            ],
        )
        report = await self._report(GROUP_B)
        self.assertFalse(self.harness.fired, "正控不该触发注入器")
        self._assert_one_generation(report)
        self.assertEqual(report["reconciliation"]["main_attempts"], 1)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": AMOUNT})
        self.assertTrue(report["amount_complete"])

    async def test_control_the_window_is_real_without_the_snapshot(self) -> None:
        """负控：绕开 getter、自己分两次读，这个窗口**确实**会混两代。

        没有这条，就无法区分"getter 挡住了"和"窗口根本没发生"。
        """

        await self._seed(GROUP_A, "m-old")
        async with self.session_factory() as session:
            rows = await group_costs.query_group_costs(session, group_id=GROUP_A, days=7)
            _commit_in_own_connection(
                self.db_path, [( _INSERT_MAIN, {
                    "d": self.today, "g": GROUP_A, "purpose": PURPOSE, "stage": STAGE,
                    "provider": PROVIDER, "model": "m-new", "attempts": 1, "amount": AMOUNT,
                }), (_INSERT_PRICED, {
                    "d": self.today, "g": GROUP_A, "purpose": PURPOSE, "stage": STAGE,
                    "provider": PROVIDER, "model": "m-new", "attempts": 1, "amount": AMOUNT,
                })]
            )
            priced = await group_costs.query_group_costs_priced(
                session, group_id=GROUP_A, days=7
            )

        main_attempts = sum(int(row["attempts"]) for row in rows)
        priced_attempts = sum(int(row["attempts"]) for row in priced)
        self.assertEqual(main_attempts, 1)
        self.assertEqual(
            priced_attempts,
            2,
            "两次独立读居然没混代 —— 说明这个测试没能制造出窗口",
        )


    async def test_caller_transaction_is_never_committed_by_the_report(self) -> None:
        """调用方已在写事务里 → 报表**什么都不做**，更不许替它 COMMIT（正控）。

        "已有事务就跳过"是对的：两次读本来就在同一个快照里。但如果我们顺手
        COMMIT，就会把调用方尚未完成的写提前落库 —— 这条用真实 SQLite 钉住：
        报表跑完之后，另一条连接必须还看不见那个未提交的写。
        """

        await self._seed(GROUP_A, "m-old")

        async def _count_group_b() -> int:
            async with self.session_factory() as session:
                total = await session.scalar(
                    select(func.count())
                    .select_from(LlmCostDaily)
                    .where(LlmCostDaily.group_id == GROUP_B)
                )
                return int(total or 0)

        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    LlmCostDaily(
                        usage_date=self.today,
                        scope="group",
                        group_id=GROUP_B,
                        purpose=PURPOSE,
                        stage=STAGE,
                        provider=PROVIDER,
                        model="m-pending",
                        attempts=1,
                        successes=1,
                    )
                )
                await session.flush()
                report = await group_costs.query_group_costs_report(
                    session, group_id=GROUP_A, days=7
                )
                self.assertEqual(report["reconciliation"]["main_attempts"], 1)
                self.assertEqual(
                    await _count_group_b(),
                    0,
                    "报表把调用方未提交的写提交掉了",
                )
            async with self.session_factory() as fresh:
                visible = await fresh.scalar(
                    select(func.count())
                    .select_from(LlmCostDaily)
                    .where(LlmCostDaily.group_id == GROUP_B)
                )
            self.assertEqual(int(visible or 0), 1, "调用方自己提交后应当可见")


class _FakeDriverConnection:
    def __init__(self) -> None:
        self.in_transaction = False


class _FakeRawConnection:
    def __init__(self, driver: _FakeDriverConnection) -> None:
        self.driver_connection = driver


class _FakeConnection:
    dialect = SimpleNamespace(name="sqlite")

    def __init__(self, driver: _FakeDriverConnection) -> None:
        self._driver = driver

    async def get_raw_connection(self) -> _FakeRawConnection:
        return _FakeRawConnection(self._driver)


class _FakeSession:
    """只认 BEGIN / COMMIT 的假会话，用来钉住"放锁失败不许盖掉读取异常"。"""

    def __init__(
        self, *, commit_error: BaseException | None = None
    ) -> None:
        self.statements: list[str] = []
        self._driver = _FakeDriverConnection()
        self._connection = _FakeConnection(self._driver)
        self._commit_error = commit_error

    async def connection(self) -> _FakeConnection:
        return self._connection

    async def execute(self, statement: Any) -> None:
        sql = str(statement)
        self.statements.append(sql)
        if sql == "BEGIN":
            self._driver.in_transaction = True
            return
        if sql == "COMMIT":
            if self._commit_error is not None:
                raise self._commit_error
            self._driver.in_transaction = False
            return
        raise AssertionError(f"报表的快照代码发了预期外的语句：{sql}")


class ReportSnapshotFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_failure_is_not_masked_by_the_commit(self) -> None:
        """读失败 → 原异常照抛，放锁失败（这里故意让 COMMIT 也炸）不能顶替它。"""

        session = _FakeSession(commit_error=RuntimeError("放锁失败"))

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("读主表失败")

        with mock.patch.object(group_costs, "query_group_costs", _boom):
            with self.assertRaises(ValueError) as ctx:
                await group_costs.query_group_costs_report(session, group_id=GROUP_A, days=7)

        self.assertEqual(str(ctx.exception), "读主表失败")
        self.assertEqual(session.statements, ["BEGIN", "COMMIT"])

    async def test_commit_failure_on_the_happy_path_is_visible(self) -> None:
        """正常读完但放锁失败 → 如实抛出，不静默吞掉。"""

        session = _FakeSession(commit_error=RuntimeError("放锁失败"))

        async def _rows(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        with mock.patch.object(group_costs, "query_group_costs", _rows):
            with mock.patch.object(group_costs, "query_group_costs_priced", _rows):
                with self.assertRaises(RuntimeError) as ctx:
                    await group_costs.query_group_costs_report(
                        session, group_id=GROUP_A, days=7
                    )

        self.assertEqual(str(ctx.exception), "放锁失败")
        self.assertEqual(session.statements, ["BEGIN", "COMMIT"])


class _FakeSessionWithoutConnection:
    """取连接就炸：必须**明确失败**，不许当成"安全跳过"继续读。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def connection(self) -> Any:
        raise RuntimeError("连接池取不到连接")

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


class _RawFailingConnection:
    dialect = SimpleNamespace(name="sqlite")

    async def get_raw_connection(self) -> Any:
        raise RuntimeError("驱动连接拿不到")


class _FakeSessionWithoutRaw:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def connection(self) -> Any:
        return _RawFailingConnection()

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


class _NoStateDriverConnection:
    """底层驱动连接**没有** in_transaction —— 状态确定不了。"""


class _StateRaisingDriverConnection:
    @property
    def in_transaction(self) -> bool:
        raise RuntimeError("事务状态读不出来")


class _DriverConnection:
    def __init__(self, in_transaction: bool) -> None:
        self.in_transaction = in_transaction


class _ConnectionReturning:
    def __init__(self, driver: Any, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self._driver = driver

    async def get_raw_connection(self) -> Any:
        return SimpleNamespace(driver_connection=self._driver)


class _FakeSessionWithDriver:
    def __init__(self, driver: Any, *, dialect: str = "sqlite") -> None:
        self.statements: list[str] = []
        self._connection = _ConnectionReturning(driver, dialect)

    async def connection(self) -> Any:
        return self._connection

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


class ReportSnapshotUnavailableTests(unittest.IsolatedAsyncioTestCase):
    """快照状态建立不了/确定不了 → 明确抛出，**不**退回不一致读（v72 合同 B）。

    改之前这三条都是 ``except Exception: return False``：调用方拿到的报表照样是
    ``amount_complete=True``，错误整个消失。现在必须走 API 失败路径。
    """

    def _patch_reads(self, calls: list[str]) -> Any:
        async def _rows(*args: Any, **kwargs: Any) -> list[Any]:
            calls.append("read")
            return []

        return mock.patch.multiple(
            group_costs,
            query_group_costs=_rows,
            query_group_costs_priced=_rows,
        )

    async def _expect_unavailable(
        self, session: Any, *, expect_in_message: str
    ) -> group_costs.ReportSnapshotUnavailable:
        calls: list[str] = []
        with self._patch_reads(calls):
            with self.assertRaises(group_costs.ReportSnapshotUnavailable) as ctx:
                await group_costs.query_group_costs_report(
                    session, group_id=GROUP_A, days=7
                )
        self.assertIn(expect_in_message, str(ctx.exception))
        self.assertEqual(calls, [], "确定不了快照状态时不许继续读任何一张表")
        self.assertEqual(session.statements, [], "失败路径不许发出任何语句")
        return ctx.exception

    async def test_connection_acquisition_failure_is_explicit(self) -> None:
        exc = await self._expect_unavailable(
            _FakeSessionWithoutConnection(), expect_in_message="无法取得数据库连接"
        )
        self.assertIsInstance(exc.__cause__, RuntimeError)

    async def test_raw_connection_failure_is_explicit(self) -> None:
        exc = await self._expect_unavailable(
            _FakeSessionWithoutRaw(), expect_in_message="无法取得底层驱动连接"
        )
        self.assertIsInstance(exc.__cause__, RuntimeError)

    async def test_missing_transaction_state_is_explicit(self) -> None:
        session = _FakeSessionWithDriver(_NoStateDriverConnection())
        await self._expect_unavailable(session, expect_in_message="in_transaction")

    async def test_raising_transaction_state_is_explicit(self) -> None:
        exc = await self._expect_unavailable(
            _FakeSessionWithDriver(_StateRaisingDriverConnection()),
            expect_in_message="读取底层连接事务状态失败",
        )
        self.assertIsInstance(exc.__cause__, RuntimeError)

    async def test_existing_transaction_skips_without_touching_it(self) -> None:
        """已有事务 → 跳过（正控）：不发 BEGIN/COMMIT，读照常进行。"""

        session = _FakeSessionWithDriver(_DriverConnection(True))
        calls: list[str] = []
        with self._patch_reads(calls):
            report = await group_costs.query_group_costs_report(
                session, group_id=GROUP_A, days=7
            )
        self.assertEqual(session.statements, [], "已有事务时不许插手事务")
        self.assertEqual(calls, ["read", "read"])
        self.assertEqual(report["reconciliation"]["main_attempts"], 0)

    async def test_non_sqlite_dialect_skips_without_begin(self) -> None:
        """非 SQLite 交给 SQLAlchemy 自己开事务（正控）：这里不该插 BEGIN。"""

        session = _FakeSessionWithDriver(_DriverConnection(False), dialect="postgresql")
        calls: list[str] = []
        with self._patch_reads(calls):
            await group_costs.query_group_costs_report(session, group_id=GROUP_A, days=7)
        self.assertEqual(session.statements, [])
        self.assertEqual(calls, ["read", "read"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
