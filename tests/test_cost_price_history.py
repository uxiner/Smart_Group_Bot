"""金额必须在**发出那一刻**冻结，之后改价不许追改历史（v70 第 1/2/3/4/5/6 条）。

父代理在冻结树 ``230124e`` 上实测到的缺口：v1 单价 1 开一次 attempt、切到 v2
单价 10 再完成、同日同群同模型再来一次 v2 请求，真实 flush → reset → 全新会话
查回来只有一条 ``attempts=2 / prompt=200 / output=200`` —— **历史单价版本已经
分辨不出来**，报表只能拿"最新价"去回算，于是改一次价就把过去的花费一起改掉。

这里的每一条都走真实台账函数 + 真实 SQLite + 真实 flush，不 mock 聚合：

* 发送时冻结版本与条目，结算只认这份快照；
* 同一台账行里 known 与 unknown 各自留痕，"已计价部分"不许被当成完整成本；
* 缺价 / 缺用量 / 旧数据没有元数据 → 明确的未计价覆盖，不猜价、不回填；
* 币种各算各的，绝不跨币种相加，没有币种就落独立的未知桶；
* 主台账 ``llm_cost_daily`` 与计价附表 ``llm_cost_priced_daily`` 是同一次
  attempt 的两个维度，**attempts 不许相加**（相减才是 legacy）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from sqlalchemy import select, text

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily, LlmCostPricedDaily
from bot.services import group_costs
from bot.services import pricing
from bot.services import usage_scope as scopes

# 合成群 id：沿用测试套件既有的两个假值（见 test_public_artifact_scan 的
# SYNTHETIC_IDS），不另外引入新的 -100… 数字 —— 公开产物扫描里每一个不在白名单内的
# 群 id 都可能是真实绑定，合成 fixture 就用已经登记过的那两个。
GROUP_A = -100900000001
GROUP_B = -100900000002


def _entry(
    model: str = "m1",
    *,
    provider: str = "fixture",
    currency: str = "USD",
    input_price: str = "1",
    output_price: str = "2",
    cache_read: str | None = None,
    cache_write: str | None = None,
) -> pricing.PriceEntry:
    return pricing.PriceEntry(
        provider_key=provider,
        model=model,
        currency=currency,
        input_per_million=input_price,
        output_per_million=output_price,
        cache_read_per_million=cache_read,
        cache_write_per_million=cache_write,
    )


def _catalog(version: int, *entries: pricing.PriceEntry) -> pricing.PriceCatalog:
    return pricing.PriceCatalog(version=version, entries=tuple(entries))


class _LedgerBase(unittest.IsolatedAsyncioTestCase):
    """真实临时库 + 真实 group_costs；价格目录按用例换版本。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_url = f"sqlite+aiosqlite:///{self._db_path}"
        self.engine, self.session_factory = await init_db(self.db_url)
        group_costs.reset()
        group_costs.configure(self.session_factory)
        # 价格目录的来源是**进程级**装配（生产里接的是 settings_api 的缓存），
        # 用例结束后必须还原，否则会把别的测试的价格目录换掉。
        self._previous_source = pricing.catalog_source()
        self.catalog = _catalog(1, _entry())
        pricing.configure_catalog_source(lambda: self.catalog)

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

    # -- 记录 ---------------------------------------------------------------

    def _record(
        self,
        *,
        group_id: int | None = GROUP_A,
        stage: str = "main",
        provider: str = "fixture",
        model: str = "m1",
        usage: dict[str, object] | None = None,
        purpose: str = scopes.PURPOSE_OPTIONAL,
    ) -> None:
        scope = scopes.usage_scope(
            kind=(
                scopes.SCOPE_GROUP if group_id is not None else scopes.SCOPE_PRIVATE
            ),
            group_id=group_id,
            purpose=purpose,
        )
        with scope:
            attempt = group_costs.record_attempt(
                stage=stage, provider=provider, model=model
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage=(
                    {"prompt_tokens": 100, "completion_tokens": 20}
                    if usage is None
                    else usage
                ),
            )

    # -- 读取 ---------------------------------------------------------------

    async def _report(self, *, group_id: int = GROUP_A, days: int = 7) -> dict:
        async with self.session_factory() as session:
            return await group_costs.query_group_costs_report(
                session, group_id=group_id, days=days
            )

    async def _priced_rows(self) -> list[LlmCostPricedDaily]:
        async with self.session_factory() as session:
            return list(
                (await session.scalars(select(LlmCostPricedDaily))).all()
            )

    @staticmethod
    def _currency(report: dict, currency: str) -> dict:
        for bucket in report["currencies"]:
            if bucket["currency"] == currency:
                return bucket
        raise AssertionError(
            f"报表里没有 {currency!r} 这个币种桶："
            f"{[b['currency'] for b in report['currencies']]}"
        )

    @staticmethod
    def _versions(bucket: dict) -> dict[int, dict]:
        return {int(v["price_version"]): v for v in bucket["price_versions"]}


class SendTimeFreezeTests(_LedgerBase):
    """发送时冻结版本与条目；改价不追改历史。"""

    async def test_inflight_attempt_keeps_the_price_it_was_sent_with(self) -> None:
        """在飞请求的价格在**发出那一刻**定下：中途改目录不影响它。"""

        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m1"
            )
            # 请求已经在飞：此刻目录被改到 v2（单价翻一百倍）。
            self.catalog = _catalog(2, _entry(input_price="100", output_price="200"))
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": 10, "completion_tokens": 1},
            )
        await group_costs.flush(force=True)
        report = await self._report()
        usd = self._currency(report, "USD")
        versions = self._versions(usd)
        self.assertEqual(sorted(versions), [1])
        # v1：10 输入 × 1 + 1 输出 × 2 = 12 微；按 v2 算会是 1200 + 200。
        self.assertEqual(usd["amount_micro"], 12)
        self.assertEqual(versions[1]["amount_micro"], 12)
        self.assertEqual(report["amount_complete"], True)

    async def test_two_versions_on_the_same_ledger_row_are_both_kept(self) -> None:
        """v1 发一次、改 v2 再发一次：两条金额各自正确，且都不被 v3 改写。"""

        self.catalog = _catalog(1, _entry(input_price="1", output_price="2"))
        self._record(usage={"prompt_tokens": 100, "completion_tokens": 20})
        await group_costs.flush(force=True)
        self.catalog = _catalog(2, _entry(input_price="10", output_price="20"))
        self._record(usage={"prompt_tokens": 100, "completion_tokens": 20})
        await group_costs.flush(force=True)
        # 历史已经落库：再改到 v3（天价）也不许动它。
        self.catalog = _catalog(3, _entry(input_price="999", output_price="999"))

        report = await self._report()
        usd = self._currency(report, "USD")
        versions = self._versions(usd)
        self.assertEqual(sorted(versions), [1, 2])
        self.assertEqual(versions[1]["amount_micro"], 100 * 1 + 20 * 2)
        self.assertEqual(versions[2]["amount_micro"], 100 * 10 + 20 * 20)
        self.assertEqual(usd["amount_micro"], 140 + 1400)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": 1540})
        self.assertEqual(report["cross_currency_total"], None)
        # 主台账的两条 attempt 没有被金额维度翻倍。
        self.assertEqual(report["totals"]["attempts"], 2)
        self.assertEqual(
            report["reconciliation"],
            {
                "main_attempts": 2,
                "priced_table_attempts": 2,
                "legacy_attempts": 0,
                "unmatched_priced_attempts": 0,
            },
        )

    async def test_snapshot_keeps_the_prices_that_were_used(self) -> None:
        """落库的是**当时那一版的那一条**，不是"最新价"的引用。"""

        self.catalog = _catalog(7, _entry(input_price="3", output_price="4"))
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 5})
        await group_costs.flush(force=True)
        rows = await self._priced_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(int(row.price_version), 7)
        self.assertEqual(str(row.currency), "USD")
        snapshot = row.price_snapshot
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["input_per_million"], "3")
        self.assertEqual(snapshot["output_per_million"], "4")
        self.assertEqual(int(row.amount_micro), 10 * 3 + 5 * 4)

    async def test_restart_reads_the_same_amounts_back(self) -> None:
        """flush → 关引擎 → 用**同一个库文件**重开：金额与版本不变。"""

        self.catalog = _catalog(4, _entry(input_price="2", output_price="3"))
        self._record(usage={"prompt_tokens": 30, "completion_tokens": 4})
        await group_costs.flush(force=True)
        before = await self._report()

        await self.engine.dispose()
        self.engine, self.session_factory = await init_db(self.db_url)
        group_costs.configure(self.session_factory)
        after = await self._report()

        self.assertEqual(
            after["totals"]["amount_by_currency"],
            before["totals"]["amount_by_currency"],
        )
        self.assertEqual(after["currencies"], before["currencies"])
        self.assertEqual(after["reconciliation"], before["reconciliation"])


class CoverageHonestyTests(_LedgerBase):
    """已知与未知各留各的痕：不许让"已计价部分"冒充完整成本。"""

    async def _amounts(self) -> tuple[int, dict]:
        await group_costs.flush(force=True)
        report = await self._report()
        return report["totals"]["amount_by_currency"].get("USD", 0), report

    async def test_zero_price_with_known_usage_is_a_known_free_call(self) -> None:
        self.catalog = _catalog(
            1, _entry(input_price="0", output_price="0")
        )
        self._record(usage={"prompt_tokens": 500, "completion_tokens": 50})
        await group_costs.flush(force=True)
        report = await self._report()
        usd = self._currency(report, "USD")
        self.assertEqual(usd["amount_micro"], 0)
        self.assertEqual(usd["priced_attempts"], 1)
        self.assertEqual(usd["unknown_attempts"], 0)
        self.assertEqual(report["amount_complete"], True)

    async def test_missing_price_is_an_unknown_bucket_not_a_zero_dollar_row(self) -> None:
        """目录里没有这个模型 → 落在**币种未知**的桶里，不折算成 USD。"""

        self.catalog = _catalog(1, _entry(model="m1"))
        self._record(model="model-not-priced", usage={"prompt_tokens": 100})
        await group_costs.flush(force=True)
        report = await self._report()
        unknown = self._currency(report, "")
        self.assertEqual(unknown["amount_micro"], 0)
        self.assertEqual(unknown["attempts"], 1)
        self.assertEqual(unknown["unknown_attempts"], 1)
        self.assertEqual(unknown["unknown_price_attempts"], 1)
        self.assertEqual(unknown["unknown_usage_attempts"], 0)
        # 没有 USD 桶：这次调用的钱**没有**被算进任何货币。
        self.assertEqual(
            [bucket["currency"] for bucket in report["currencies"]], [""]
        )
        self.assertEqual(report["totals"]["amount_by_currency"], {})
        self.assertEqual(report["amount_complete"], False)

    async def test_chat_usage_without_output_segment_is_not_a_complete_amount(self) -> None:
        """上游只报输入：不能按"输出 0"算出一个完整金额。"""

        self.catalog = _catalog(1, _entry())
        self._record(usage={"prompt_tokens": 100})
        amount, report = await self._amounts()
        self.assertEqual(amount, 0)
        usd = self._currency(report, "USD")
        self.assertEqual(usd["priced_attempts"], 0)
        self.assertEqual(usd["unknown_attempts"], 1)
        self.assertEqual(usd["unknown_usage_attempts"], 1)
        self.assertEqual(report["amount_complete"], False)

    async def test_embedding_needs_input_only(self) -> None:
        """嵌入只有输入段：它有金额，而且不是"缺输出"的未知。"""

        self.catalog = _catalog(1, _entry())
        self._record(stage="embed", usage={"prompt_tokens": 50})
        await group_costs.flush(force=True)
        report = await self._report()
        usd = self._currency(report, "USD")
        self.assertEqual(usd["amount_micro"], 50)
        self.assertEqual(usd["priced_attempts"], 1)
        self.assertEqual(usd["unknown_attempts"], 0)
        self.assertEqual(report["amount_complete"], True)

    async def test_known_and_unknown_do_not_overwrite_each_other(self) -> None:
        """同桶 known/unknown 三种顺序：金额只算 known，未知照数。"""

        self.catalog = _catalog(1, _entry(input_price="10", output_price="10"))
        # 1) 同一批里 known 先、unknown 后
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 1})
        self._record(usage={"prompt_tokens": "abc", "completion_tokens": 1})
        await group_costs.flush(force=True)
        # 2) 另一批里 unknown 先、known 后
        self._record(usage={"prompt_tokens": 1, "completion_tokens": "??"})
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 1})
        await group_costs.flush(force=True)
        report = await self._report()
        usd = self._currency(report, "USD")
        self.assertEqual(usd["priced_attempts"], 2)
        self.assertEqual(usd["unknown_attempts"], 2)
        self.assertEqual(usd["unknown_usage_attempts"], 2)
        # 每一次 known 都是 10×10 + 1×10 = 110 微，两次共 220。
        self.assertEqual(usd["amount_micro"], 220)
        self.assertEqual(usd["amount_is_partial"], True)
        self.assertEqual(usd["complete"], False)
        self.assertEqual(report["amount_complete"], False)
        # 主台账仍如实记 4 次 attempt（两张表不许相加）。
        self.assertEqual(report["totals"]["attempts"], 4)
        self.assertEqual(report["reconciliation"]["priced_table_attempts"], 4)

    async def test_only_the_components_actually_used_need_a_price(self) -> None:
        """没用到缓存就不需要缓存单价；用到了缺价才是未知。"""

        self.catalog = _catalog(
            1, _entry(input_price="1", output_price="1")
        )  # cache_read / cache_write 都是 None
        self._record(usage={"prompt_tokens": 100, "completion_tokens": 10})
        await group_costs.flush(force=True)
        report = await self._report()
        self.assertEqual(self._currency(report, "USD")["amount_micro"], 110)
        self.assertEqual(report["amount_complete"], True)

        # 这次上游报了缓存读：实际用到的组件没有单价 → 这次不可计价。
        self._record(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cached_tokens": 60,
            }
        )
        await group_costs.flush(force=True)
        report = await self._report()
        usd = self._currency(report, "USD")
        self.assertEqual(usd["amount_micro"], 110)
        self.assertEqual(usd["priced_attempts"], 1)
        self.assertEqual(usd["unknown_attempts"], 1)
        self.assertEqual(usd["unknown_price_attempts"], 1)

    async def test_cache_prices_are_charged_on_their_own_components(self) -> None:
        """缓存读是输入的一部分、缓存写是额外一段 —— 不重复也不漏。"""

        self.catalog = _catalog(
            1,
            _entry(
                input_price="10",
                output_price="1",
                cache_read="1",
                cache_write="20",
            ),
        )
        self._record(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cached_tokens": 60,
                "cache_write_tokens": 5,
            }
        )
        await group_costs.flush(force=True)
        report = await self._report()
        # 未缓存输入 40×10 + 缓存读 60×1 + 缓存写 5×20 + 输出 10×1 = 400+60+100+10
        self.assertEqual(self._currency(report, "USD")["amount_micro"], 570)
        self.assertEqual(report["amount_complete"], True)


class CurrencyIsolationTests(_LedgerBase):
    """币种各算各的，未知币种是独立桶。"""

    async def test_two_currencies_are_reported_side_by_side(self) -> None:
        self.catalog = _catalog(
            1,
            _entry(model="m1", provider="fixture", currency="USD", input_price="1", output_price="1"),
            _entry(model="m2", provider="alt", currency="CNY", input_price="7", output_price="7"),
        )
        self._record(provider="fixture", model="m1", usage={"prompt_tokens": 10, "completion_tokens": 0})
        self._record(provider="alt", model="m2", usage={"prompt_tokens": 3, "completion_tokens": 0})
        await group_costs.flush(force=True)
        report = await self._report()
        self.assertEqual(
            sorted(bucket["currency"] for bucket in report["currencies"]),
            ["CNY", "USD"],
        )
        self.assertEqual(report["totals"]["amount_by_currency"], {"CNY": 21, "USD": 10})
        # 没有"合计金额"这种东西：跨币种相加等于编一个汇率。
        self.assertIsNone(report["cross_currency_total"])
        self.assertNotIn("amount_micro", report["totals"])
        for bucket in report["currencies"]:
            self.assertEqual(bucket["complete"], True)

    async def test_other_groups_and_private_usage_stay_out_of_the_group_report(self) -> None:
        self.catalog = _catalog(1, _entry(input_price="1", output_price="1"))
        self._record(group_id=GROUP_A, usage={"prompt_tokens": 10, "completion_tokens": 0})
        self._record(group_id=GROUP_B, usage={"prompt_tokens": 100, "completion_tokens": 0})
        # 私聊（没有群归属）同样不许混进按群报表。
        scope = scopes.usage_scope(kind=scopes.SCOPE_PRIVATE)
        with scope:
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m1"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": 1000, "completion_tokens": 0},
            )
        await group_costs.flush(force=True)
        report_a = await self._report(group_id=GROUP_A)
        report_b = await self._report(group_id=GROUP_B)
        self.assertEqual(report_a["totals"]["amount_by_currency"], {"USD": 10})
        self.assertEqual(report_a["totals"]["attempts"], 1)
        self.assertEqual(report_b["totals"]["amount_by_currency"], {"USD": 100})
        self.assertEqual(report_b["totals"]["attempts"], 1)


class LegacyCoverageTests(_LedgerBase):
    """旧数据没有发送时元数据 → 明确"未计价覆盖"，不回算、不猜币种。"""

    async def _seed_legacy_row(self) -> None:
        async with self.session_factory() as session:
            session.add(
                LlmCostDaily(
                    usage_date=group_costs.window_start(1),
                    scope="group",
                    group_id=GROUP_A,
                    purpose="optional",
                    stage="main",
                    provider="fixture",
                    model="m1",
                    attempts=3,
                    successes=3,
                    prompt_tokens=300,
                    output_tokens=30,
                )
            )
            await session.commit()

    async def test_legacy_rows_are_unknown_and_never_backfilled(self) -> None:
        await self._seed_legacy_row()
        # 目录里**有**这个模型的当前价：也不许拿去回算历史。
        self.catalog = _catalog(5, _entry(input_price="9", output_price="9"))
        report = await self._report()
        unknown = self._currency(report, "")
        self.assertEqual(unknown["legacy_attempts"], 3)
        self.assertEqual(unknown["amount_micro"], 0)
        self.assertEqual(unknown["complete"], False)
        self.assertEqual(report["totals"]["amount_by_currency"], {})
        self.assertEqual(report["amount_complete"], False)
        self.assertEqual(
            report["reconciliation"],
            {
                "main_attempts": 3,
                "priced_table_attempts": 0,
                "legacy_attempts": 3,
                "unmatched_priced_attempts": 0,
            },
        )
        # 明细行上也如实写着"这一次没有金额"，而不是 0 元。
        self.assertEqual(report["rows"][0]["amounts"], [])

        # 再改一次价：历史依旧不动，也不会凭空多出计价行。
        self.catalog = _catalog(6, _entry(input_price="1000", output_price="1000"))
        again = await self._report()
        self.assertEqual(again["currencies"], report["currencies"])
        self.assertEqual(await self._priced_rows(), [])

    async def test_new_usage_after_legacy_is_priced_while_legacy_stays_unknown(self) -> None:
        await self._seed_legacy_row()
        self.catalog = _catalog(5, _entry(input_price="2", output_price="2"))
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 5})
        await group_costs.flush(force=True)
        report = await self._report()
        self.assertEqual(self._currency(report, "USD")["amount_micro"], 30)
        self.assertEqual(self._currency(report, "")["legacy_attempts"], 3)
        self.assertEqual(report["amount_complete"], False)
        self.assertEqual(report["totals"]["attempts"], 4)

    async def test_old_database_without_the_priced_table_is_upgraded(self) -> None:
        """旧库升级路径：建表由 ``init_db`` 补齐，随后计价照常落库。"""

        async with self.session_factory() as session:
            await session.execute(text("DROP TABLE llm_cost_priced_daily"))
            await session.commit()
        await self.engine.dispose()
        self.engine, self.session_factory = await init_db(self.db_url)
        group_costs.configure(self.session_factory)
        self.catalog = _catalog(2, _entry(input_price="1", output_price="1"))
        self._record(usage={"prompt_tokens": 4, "completion_tokens": 6})
        await group_costs.flush(force=True)
        report = await self._report()
        self.assertEqual(self._currency(report, "USD")["amount_micro"], 10)
        self.assertEqual(report["amount_complete"], True)


class PricedBatchBoundaryTests(_LedgerBase):
    """计价行与主台账在**同一个冻结批次事务**里：提交边界不许只落一半。"""

    async def test_committed_then_cancelled_batch_does_not_double_price(self) -> None:
        self.catalog = _catalog(1, _entry(input_price="1", output_price="1"))
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 2})

        real_factory = self.session_factory
        controller = {"armed": True}

        class _SessionProxy:
            """真会话、真 commit；提交成功之后才抛 CancelledError。"""

            def __init__(self, session):
                self._session = session

            async def __aenter__(self):
                await self._session.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await self._session.__aexit__(*exc)

            def __getattr__(self, name):
                attr = getattr(self._session, name)
                if name == "commit" and controller["armed"]:

                    async def _commit_then_cancel():
                        controller["armed"] = False
                        await attr()
                        raise asyncio.CancelledError()

                    return _commit_then_cancel
                return attr

        def _factory():
            return _SessionProxy(real_factory())

        group_costs.configure(_factory)  # type: ignore[arg-type]
        with self.assertRaises(asyncio.CancelledError):
            await group_costs.flush(force=True)

        # 数据库里其实**已经**提交了：这条新到的用量属于下一批，不能被并进旧批。
        group_costs.configure(real_factory)
        self._record(usage={"prompt_tokens": 17, "completion_tokens": 3})
        await group_costs.flush(force=True)

        report = await self._report()
        self.assertEqual(report["totals"]["attempts"], 2)
        self.assertEqual(report["rows"][0]["prompt_tokens"], 27)
        self.assertEqual(report["rows"][0]["output_tokens"], 5)
        usd = self._currency(report, "USD")
        self.assertEqual(usd["priced_attempts"], 2)
        self.assertEqual(usd["amount_micro"], (10 + 2) + (17 + 3))
        self.assertEqual(
            report["reconciliation"],
            {
                "main_attempts": 2,
                "priced_table_attempts": 2,
                "legacy_attempts": 0,
                "unmatched_priced_attempts": 0,
            },
        )

    async def test_committed_priced_rows_survive_a_marker_conflict(self) -> None:
        """同一批重放两次：主表与计价表**一起**跳过，不会只跳一条。"""

        self.catalog = _catalog(1, _entry(input_price="1", output_price="1"))
        self._record(usage={"prompt_tokens": 10, "completion_tokens": 2})
        await group_costs.flush(force=True)
        batch = group_costs.snapshot()
        priced = group_costs.priced_snapshot()
        self.assertEqual(batch, {})
        self.assertEqual(priced, {})
        rows = await self._priced_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0].attempts), 1)


class PriceCatalogSourceTests(unittest.IsolatedAsyncioTestCase):
    """没有装配目录来源时**不许猜价**：一律当成"缺价"。"""

    async def asyncSetUp(self) -> None:
        self._previous_source = pricing.catalog_source()
        pricing.configure_catalog_source(None)

    async def asyncTearDown(self) -> None:
        pricing.configure_catalog_source(self._previous_source)

    async def test_missing_source_is_an_unknown_price_not_a_guess(self) -> None:
        freeze = pricing.freeze_price("fixture", "m1")
        self.assertFalse(freeze.has_entry)
        self.assertEqual(freeze.currency, "")
        estimate = freeze.estimate(prompt_tokens=100, output_tokens=10)
        self.assertFalse(estimate.known)
        self.assertIsNone(estimate.micro)
        self.assertEqual(estimate.unknown_components, pricing.COMPONENTS)

    async def test_broken_source_does_not_take_the_request_down(self) -> None:
        def _boom():
            raise RuntimeError("catalog store exploded")

        pricing.configure_catalog_source(_boom)
        freeze = pricing.freeze_price("fixture", "m1")
        self.assertFalse(freeze.has_entry)
        self.assertIsNone(freeze.estimate(prompt_tokens=1).micro)
