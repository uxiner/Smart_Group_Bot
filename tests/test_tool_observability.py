"""Offline contract tests at the three real, decorated tool boundaries.

Only external fetch/search/Edge/Telegram transports are replaced in normal-path
cases. Fault-result tests explicitly inject malformed skill results. Paired
``__wrapped__`` calls keep orchestration and externally visible effects honest.
"""

from __future__ import annotations

import asyncio
import json
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services import dm_search, private_tts
from bot.services import tool_observability as telemetry
from bot.services.doubao_tts import DoubaoTTSService
from bot.services.skills import webfetch
from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.service import SkillService
from bot.services.skills.websearch import WebSearchSkill

SECRET = "private-query-token-424242-secret"
URL = f"https://example.test/page?token={SECRET}"
FETCH_RESPONSE = (
    200,
    "<title>Example</title><p>Public article body.</p>",
    URL,
    "text/html",
)
SEARCH_ROWS = [{"title": "Example", "href": URL, "body": SECRET}]


def assert_event(test, scope, operation, outcome, error):
    events = telemetry.snapshot()["events"]
    test.assertEqual(len(events), 1)
    event = events[0]
    test.assertEqual(
        {key: event[key] for key in ("scope", "operation", "outcome", "error")},
        {"scope": scope, "operation": operation, "outcome": outcome, "error": error},
    )
    test.assertEqual(
        set(event), {"scope", "operation", "outcome", "error", "duration_seconds"}
    )
    test.assertTrue(math.isfinite(event["duration_seconds"]))
    test.assertGreaterEqual(event["duration_seconds"], 0)
    test.assertNotIn(SECRET, json.dumps(telemetry.snapshot()))


def tts_service(*, segment_length=500):
    # Real splitting, normalization, synthesis orchestration and provider path;
    # only edge_tts.Communicate.stream is replaced below, at its network edge.
    return DoubaoTTSService(
        SimpleNamespace(
            doubao_tts_enabled=True,
            doubao_tts_provider="edge",
            doubao_tts_speaker="zh-CN-XiaoxiaoNeural",
            doubao_tts_audio_format="mp3",
            doubao_tts_max_text_length=segment_length,
        )
    )


def telegram_message():
    sent = SimpleNamespace(chat=SimpleNamespace(id=424242), message_id=17)
    return SimpleNamespace(
        chat=SimpleNamespace(id=424242),
        bot=SimpleNamespace(
            get_chat=AsyncMock(
                return_value=SimpleNamespace(
                    has_restricted_voice_and_video_messages=True
                )
            )
        ),
        answer_voice=AsyncMock(return_value=sent),
        answer_audio=AsyncMock(return_value=sent),
    )


class EdgeTransport:
    def __init__(self, *, fail_from=0, error=None):
        self.calls = []
        self.fail_from = fail_from
        self.error = error

    def communicate(self, text, voice, **kwargs):
        self.calls.append((text, voice, kwargs))
        index = len(self.calls)

        async def stream():
            if self.error is not None:
                raise self.error
            if self.fail_from and index >= self.fail_from:
                raise RuntimeError(SECRET)
            yield {"type": "audio", "data": b"ID3-offline-audio"}

        return SimpleNamespace(stream=stream)


class ObserverStorageTests(unittest.TestCase):
    def setUp(self):
        telemetry.reset()

    def tearDown(self):
        telemetry.reset()

    def test_constants_singleton_module_helpers_and_detached_snapshot(self):
        self.assertIsInstance(telemetry.observer, telemetry.ToolObservability)
        self.assertEqual(telemetry.MAX_EVENTS, 64)
        self.assertEqual(telemetry.MAX_COUNTER, 2**63 - 1)
        telemetry.observer.record("group_skill", "webfetch", "success", 0.25)
        first = telemetry.snapshot()
        self.assertEqual(first["aggregates"][0]["calls"], 1)
        first["events"][0]["scope"] = SECRET
        first["aggregates"][0]["calls"] = -10
        first["events"].clear()
        current = telemetry.snapshot()
        self.assertEqual(len(current["events"]), 1)
        self.assertEqual(current["aggregates"][0]["calls"], 1)
        self.assertEqual(current["aggregates"][0]["duration_seconds"], 0.25)
        telemetry.reset()
        self.assertEqual(telemetry.snapshot(), {"events": [], "aggregates": []})
        self.assertEqual(len(current["events"]), 1)

    def test_eviction_keeps_latest_64_but_not_just_64_aggregate_calls(self):
        local = telemetry.ToolObservability()
        for value in range(80):
            local.record("group_skill", "webfetch", "success", float(value))
        snap = local.snapshot()
        self.assertEqual(len(snap["events"]), 64)
        self.assertEqual(
            [row["duration_seconds"] for row in snap["events"]], list(range(16, 80))
        )
        self.assertEqual(snap["aggregates"][0]["calls"], 80)
        self.assertEqual(snap["aggregates"][0]["duration_seconds"], sum(range(80)))

    def test_untrusted_labels_and_objects_never_become_retained_payloads(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("untrusted value must not be stringified")

            def __eq__(self, other):
                raise AssertionError("untrusted equality must not be invoked")

        class StringSubclass(str):
            pass

        local = telemetry.ToolObservability()
        for value in (SECRET, Hostile(), StringSubclass("success"), None, {}, []):
            local.record(value, value, value, value, value)
        snap = local.snapshot()
        self.assertEqual(len(snap["aggregates"]), 1)
        for row in snap["events"]:
            self.assertEqual(
                row,
                {
                    "scope": "other",
                    "operation": "other",
                    "outcome": "other",
                    "error": "other",
                    "duration_seconds": 0,
                },
            )
            self.assertTrue(
                all(
                    type(value) in (str, int, float, bool, type(None))
                    for value in row.values()
                )
            )
        self.assertNotIn(SECRET, json.dumps(snap))

    def test_scope_operation_cross_product_is_fixed_and_counters_saturate(self):
        local = telemetry.ToolObservability()
        local.record("group_skill", "webfetch", "success", 1)
        key = ("group_skill", "webfetch", "success", "none")
        # Impossible to reach by iteration; inject only into this test-owned
        # observer, never the production singleton or public snapshots.
        local._aggregates[key]["calls"] = telemetry.MAX_COUNTER
        local._aggregates[key]["duration_seconds"] = telemetry.MAX_DURATION_SECONDS
        local.record("group_skill", "webfetch", "success", 3)
        row = local.snapshot()["aggregates"][0]
        self.assertEqual(row["calls"], telemetry.MAX_COUNTER)
        self.assertEqual(row["duration_seconds"], telemetry.MAX_DURATION_SECONDS)
        for index in range(1000):
            local.record(f"scope-{index}", f"operation-{index}", "success", 0)
        local.record("private_search", "deliver_private_reply", "success", 0)
        local.record("private_tts", "webfetch", "success", 0)
        snap = local.snapshot()
        self.assertLessEqual(len(snap["aggregates"]), telemetry.MAX_AGGREGATES)
        self.assertEqual(len(snap["aggregates"]), 4)
        self.assertEqual(snap["events"][-1]["operation"], "other")
        self.assertEqual(snap["events"][-2]["operation"], "other")

    def test_duration_values_are_finite_nonnegative_and_bounded(self):
        local = telemetry.ToolObservability()
        for value in (
            -1,
            float("nan"),
            float("inf"),
            -float("inf"),
            10**1000,
            True,
            SECRET,
        ):
            local.record("group_skill", "webfetch", "success", value)
        snap = local.snapshot()
        for row in snap["events"] + snap["aggregates"]:
            self.assertTrue(math.isfinite(row["duration_seconds"]))
            self.assertGreaterEqual(row["duration_seconds"], 0)
            self.assertLessEqual(
                row["duration_seconds"], telemetry.MAX_DURATION_SECONDS
            )


class RealEntrypointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        telemetry.reset()
        self.service = SkillService(SimpleNamespace())
        self.cache_patch = patch.object(
            private_tts, "_voice_restriction_cache", private_tts.VoiceRestrictionCache()
        )
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(telemetry.reset)

    async def fetch(self, *, function=None, context=None, arguments=None):
        function = function or self.service._run_tool
        return await function(
            name="webfetch",
            arguments=arguments if arguments is not None else {"url": URL},
            context=context if context is not None else SkillContext(),
        )

    async def deliver(self, *, function=None, message=None, receipt=None, **kwargs):
        return await (function or private_tts.deliver_private_reply)(
            message if message is not None else telegram_message(),
            text=kwargs.pop("text", SECRET),
            delivery=kwargs.pop("delivery", "voice"),
            service=kwargs.pop("service", tts_service()),
            receipt=receipt,
            **kwargs,
        )

    def test_all_real_entrypoints_expose_original_and_metadata(self):
        for function in (
            SkillService._run_tool,
            dm_search.run_search,
            private_tts.deliver_private_reply,
        ):
            self.assertTrue(callable(function.__wrapped__))
            self.assertEqual(function.__name__, function.__wrapped__.__name__)
            self.assertEqual(function.__module__, function.__wrapped__.__module__)

    async def test_registered_webfetch_success_and_business_failure(self):
        for response, expected in (
            (FETCH_RESPONSE, "success"),
            ((404, "", URL, "text/html"), "failure"),
        ):
            telemetry.reset()
            with patch.object(
                webfetch, "fetch_text", AsyncMock(return_value=response)
            ) as transport:
                result = await self.fetch()
            self.assertEqual(result.ok, expected == "success")
            self.assertEqual(transport.await_count, 1)
            assert_event(
                self,
                "group_skill",
                "webfetch",
                expected,
                "none" if result.ok else "business",
            )

    async def test_real_webfetch_transport_timeout_and_unknown_name(self):
        with patch.object(
            webfetch, "fetch_text", AsyncMock(side_effect=TimeoutError(SECRET))
        ):
            result = await self.fetch()
        self.assertEqual(result.error, "timeout")
        assert_event(self, "group_skill", "webfetch", "timeout", "timeout")
        telemetry.reset()
        result = await self.service._run_tool(
            name=SECRET, arguments={"query": SECRET}, context=SkillContext()
        )
        self.assertFalse(result.ok)
        assert_event(self, "group_skill", "other", "failure", "business")

    async def test_real_group_deadline_cancels_fetch_without_duplicate_completion(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def transport(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        self.service.tool_timeout_seconds = 0.01
        with patch.object(webfetch, "fetch_text", transport):
            result = await self.fetch()
            await asyncio.wait_for(stopped.wait(), timeout=1)
        self.assertTrue(entered.is_set())
        self.assertEqual(result.error, "tool_timeout")
        assert_event(self, "group_skill", "webfetch", "timeout", "timeout")

    async def test_private_search_real_skill_success_empty_query_and_backend_timeout(
        self,
    ):
        with patch.object(
            WebSearchSkill, "_search_once", return_value=SEARCH_ROWS
        ) as backend:
            result = await dm_search.run_search(SECRET, max_results=2)
        self.assertTrue(result.ok)
        self.assertEqual(backend.call_count, 1)
        assert_event(self, "private_search", "websearch", "success", "none")
        telemetry.reset()
        with patch.object(
            WebSearchSkill,
            "_search_once",
            side_effect=AssertionError("must not search"),
        ) as backend:
            result = await dm_search.run_search("")
        self.assertEqual(result.error, "empty_query")
        backend.assert_not_called()
        assert_event(self, "private_search", "websearch", "failure", "business")
        telemetry.reset()
        with patch.object(
            WebSearchSkill, "_search_once", side_effect=TimeoutError(SECRET)
        ) as backend:
            result = await dm_search.run_search(SECRET)
        self.assertEqual(result.error, "search_timeout")
        self.assertGreater(backend.call_count, 0)
        assert_event(self, "private_search", "websearch", "timeout", "timeout")

    async def test_private_search_real_firecrawl_backend_is_used_once(self):
        rows = [{"title": "Public", "url": URL, "snippet": SECRET}]
        settings = SimpleNamespace(firecrawl_api_key=SECRET)
        with patch.object(
            WebSearchSkill, "_firecrawl_search", AsyncMock(return_value=rows)
        ) as backend:
            result = await dm_search.run_search(SECRET, settings=settings)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["search_kind"], "firecrawl")
        self.assertEqual(backend.await_count, 1)
        assert_event(self, "private_search", "websearch", "success", "none")

    async def test_cancellation_before_scheduling_has_no_call_and_no_event(self):
        with (
            patch.object(
                webfetch, "fetch_text", AsyncMock(return_value=FETCH_RESPONSE)
            ) as fetch_transport,
            patch.object(
                WebSearchSkill, "_search_once", return_value=SEARCH_ROWS
            ) as search_transport,
        ):
            tasks = [
                asyncio.create_task(self.fetch()),
                asyncio.create_task(dm_search.run_search(SECRET)),
                asyncio.create_task(self.deliver()),
            ]
            for task in tasks:
                task.cancel()
            for task in tasks:
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(telemetry.snapshot(), {"events": [], "aggregates": []})
        fetch_transport.assert_not_awaited()
        search_transport.assert_not_called()

    async def test_group_cancellation_after_entry_preserves_payload_and_cleanup(
        self,
    ):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def transport(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        with patch.object(webfetch, "fetch_text", transport):
            task = asyncio.create_task(self.fetch())
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel(SECRET)
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            await asyncio.wait_for(stopped.wait(), timeout=1)
        self.assertEqual(caught.exception.args, (SECRET,))
        assert_event(self, "group_skill", "webfetch", "cancelled", "cancelled")

    async def test_private_search_cancellation_after_backend_entry_is_not_swallowed(
        self,
    ):
        entered = asyncio.Event()
        cancelled = asyncio.CancelledError(SECRET)

        async def backend(*args, **kwargs):
            entered.set()
            raise cancelled

        with patch.object(WebSearchSkill, "_firecrawl_search", backend):
            with self.assertRaises(asyncio.CancelledError) as caught:
                await dm_search.run_search(
                    SECRET, settings=SimpleNamespace(firecrawl_api_key=SECRET)
                )
        self.assertTrue(entered.is_set())
        self.assertIs(caught.exception, cancelled)
        assert_event(self, "private_search", "websearch", "cancelled", "cancelled")

    async def test_malformed_result_faults_are_classified_without_replacement(self):
        # Deliberate fault injection only: normal tests use registered real skills.
        cases = [
            (
                SkillRunResult(False, "webfetch", SECRET, error="tool_failed"),
                "failure",
                "exception",
            ),
            (
                SkillRunResult(
                    False, "webfetch", SECRET, error="tool_outcome_ambiguous"
                ),
                "ambiguous",
                "uncertain",
            ),
            (
                SkillRunResult(True, "webfetch", SECRET, payload={"complete": False}),
                "ambiguous",
                "uncertain",
            ),
            (
                SkillRunResult(
                    True, "webfetch", SECRET, payload={"voice_complete": False}
                ),
                "ambiguous",
                "uncertain",
            ),
            (
                SkillRunResult(True, "webfetch", SECRET, error="partial_send"),
                "ambiguous",
                "uncertain",
            ),
            (object(), None, None),
        ]
        for result, outcome, error in cases:
            telemetry.reset()
            with (
                self.subTest(outcome=outcome, error=error),
                patch.object(
                    self.service.skills["webfetch"],
                    "run",
                    AsyncMock(return_value=result),
                ),
            ):
                actual = await self.fetch()
            self.assertIs(actual, result)
            if outcome:
                assert_event(self, "group_skill", "webfetch", outcome, error)
            else:
                self.assertEqual(telemetry.snapshot()["events"], [])

    async def test_repeated_concurrent_calls_keep_exact_counts_and_64_events(self):
        with patch.object(
            webfetch, "fetch_text", AsyncMock(return_value=FETCH_RESPONSE)
        ) as transport:
            results = await asyncio.gather(*(self.fetch() for _ in range(72)))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(transport.await_count, 72)
        snap = telemetry.snapshot()
        self.assertEqual(len(snap["events"]), 64)
        self.assertEqual(len(snap["aggregates"]), 1)
        self.assertEqual(snap["aggregates"][0]["calls"], 72)
        self.assertNotIn(SECRET, json.dumps(snap))

    async def test_paired_group_and_search_preserve_results_boundary_counts_and_flags(
        self,
    ):
        group_runs = []
        for function in (
            self.service._run_tool,
            SkillService._run_tool.__wrapped__.__get__(self.service),
        ):
            context = SkillContext(
                handled=True, delivery_confirmed=True, tts_sent=True, tts_text=SECRET
            )
            with patch.object(
                webfetch, "fetch_text", AsyncMock(return_value=FETCH_RESPONSE)
            ) as transport:
                result = await self.fetch(function=function, context=context)
            group_runs.append((result, transport.await_count, context))
        self.assertEqual(group_runs[0], group_runs[1])
        search_runs = []
        for function in (dm_search.run_search, dm_search.run_search.__wrapped__):
            with patch.object(
                WebSearchSkill, "_search_once", return_value=SEARCH_ROWS
            ) as backend:
                result = await function(SECRET, max_results=2)
            search_runs.append((result, backend.call_args_list))
        self.assertEqual(search_runs[0], search_runs[1])

    async def test_search_caller_retries_do_not_double_count_logical_operation(self):
        observations = []
        for function, broken_recorder in (
            (dm_search.run_search, False),
            (dm_search.run_search, True),
            (dm_search.run_search.__wrapped__, False),
        ):
            telemetry.reset()
            messages = [{"role": "user", "content": f"帮我查 {SECRET}"}]
            original_messages = [dict(item) for item in messages]
            budget = dm_search.SearchBudget(limit=10)
            llm = SimpleNamespace(chat=AsyncMock(side_effect=["", "Offline answer"]))
            original_record = telemetry.observer.record
            with (
                patch.object(dm_search, "run_search", function),
                patch.object(
                    WebSearchSkill,
                    "_firecrawl_search",
                    AsyncMock(side_effect=RuntimeError(SECRET)),
                ) as firecrawl,
                patch.object(
                    WebSearchSkill, "_search_once", side_effect=[[], SEARCH_ROWS]
                ) as ddgs,
                patch.object(
                    telemetry.observer,
                    "record",
                    side_effect=RuntimeError("recorder failed")
                    if broken_recorder
                    else original_record,
                ) as recorder,
            ):
                answer = await dm_search.answer_with_search(
                    llm,
                    messages,
                    settings=SimpleNamespace(firecrawl_api_key=SECRET),
                    budget=budget,
                    max_results=2,
                )
            self.assertEqual(messages, original_messages)
            self.assertEqual(answer.text, "Offline answer")
            self.assertEqual(answer.searches, 1)
            self.assertFalse(answer.exhausted)
            self.assertEqual(answer.recorded, 0)
            self.assertEqual(budget.used, 1)
            self.assertGreater(firecrawl.await_count, 0)
            self.assertEqual(ddgs.call_count, 2)
            self.assertEqual(llm.chat.await_count, 2)
            first, retry = llm.chat.await_args_list
            # 这条断言的不变量是"同一次逻辑检索被重试"，不是两次入参逐字相同：本线的
            # 兜底重试会在第二次调用前追加一条系统提示，用户消息与检索结果块必须一致。
            def _user_messages(call):
                return [m for m in call.args[0] if m.get("role") == "user"]

            self.assertEqual(_user_messages(first), _user_messages(retry))
            self.assertIn(dm_search.RESULT_BLOCK, json.dumps(first.args))
            observations.append(
                (answer, firecrawl.await_count, ddgs.call_count, budget.used)
            )
            if function is dm_search.run_search.__wrapped__ or broken_recorder:
                self.assertEqual(telemetry.snapshot()["events"], [])
            else:
                assert_event(self, "private_search", "websearch", "success", "none")
                self.assertEqual(telemetry.snapshot()["aggregates"][0]["calls"], 1)
            self.assertEqual(recorder.call_count, int(function is dm_search.run_search))
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0], observations[2])

    async def test_recorder_failure_preserves_exact_skill_result_identity(self):
        # Fault-result injection lets identity, not merely value equality, be
        # checked at the existing boundary; no real skill.run is replaced elsewhere.
        result = SkillRunResult(True, "webfetch", SECRET, payload={"token": SECRET})
        with (
            patch.object(
                self.service.skills["webfetch"], "run", AsyncMock(return_value=result)
            ),
            patch.object(
                telemetry.observer, "record", side_effect=RuntimeError(SECRET)
            ) as recorder,
        ):
            self.assertIs(await self.fetch(), result)
        self.assertEqual(recorder.call_count, 1)
        self.assertEqual(telemetry.snapshot()["events"], [])

    async def test_tts_success_uses_real_synthesis_and_telegram_once(self):
        transport = EdgeTransport()
        message = telegram_message()
        receipt = private_tts.DeliveryReceipt()
        with patch("edge_tts.Communicate", transport.communicate):
            result = await self.deliver(
                message=message, receipt=receipt, delivery=" VoIcE "
            )
        self.assertTrue(result.delivered)
        self.assertTrue(result.complete)
        self.assertEqual(result.error, "")
        self.assertEqual(result.medium, "audio")
        self.assertEqual(receipt.text, SECRET)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(message.answer_audio.await_count, 1)
        message.answer_voice.assert_not_awaited()
        assert_event(self, "private_tts", "deliver_private_reply", "success", "none")

    async def test_plain_text_and_invalid_delivery_never_count_as_tts(self):
        for delivery in ("text", " TEXT ", "audio", "", None):
            receipt = private_tts.DeliveryReceipt()

            async def send_text(text, book):
                book.add(text)

            with (
                self.subTest(delivery=delivery),
                patch("edge_tts.Communicate") as transport,
            ):
                result = await self.deliver(
                    delivery=delivery, receipt=receipt, send_text=send_text
                )
            self.assertTrue(result.delivered)
            self.assertEqual(result.medium, "text")
            transport.assert_not_called()
        self.assertEqual(telemetry.snapshot(), {"events": [], "aggregates": []})

    async def test_tts_failures_and_delivered_fallback_are_never_false_success(self):
        for text_sink_works, expected in ((False, "failure"), (True, "ambiguous")):
            telemetry.reset()
            receipt = private_tts.DeliveryReceipt()
            text_calls = []

            async def send_text(text, book, calls=text_calls, works=text_sink_works):
                calls.append(text)
                if not works:
                    raise RuntimeError(SECRET)
                book.add(text)

            transport = EdgeTransport(fail_from=1)
            with patch("edge_tts.Communicate", transport.communicate):
                result = await self.deliver(receipt=receipt, send_text=send_text)
            self.assertEqual(result.delivered, text_sink_works)
            self.assertTrue(result.error)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(text_calls, [SECRET])
            assert_event(
                self,
                "private_tts",
                "deliver_private_reply",
                expected,
                "uncertain" if text_sink_works else "business",
            )

    async def test_partial_tts_paired_baseline_retains_receipt_no_duplicate_segments(
        self,
    ):
        observations = []
        for function in (
            private_tts.deliver_private_reply,
            private_tts.deliver_private_reply.__wrapped__,
        ):
            message = telegram_message()
            transport = EdgeTransport(fail_from=2)
            receipt = private_tts.DeliveryReceipt()
            text_calls = []

            async def send_text(text, book, calls=text_calls):
                calls.append(text)
                raise RuntimeError(SECRET)

            with patch("edge_tts.Communicate", transport.communicate):
                result = await self.deliver(
                    function=function,
                    message=message,
                    receipt=receipt,
                    text="abcdefghi",
                    service=tts_service(segment_length=5),
                    send_text=send_text,
                )
            observations.append(
                (
                    result,
                    receipt.parts,
                    len(transport.calls),
                    message.answer_audio.await_count,
                    text_calls,
                )
            )
        self.assertEqual(observations[0], observations[1])
        result, parts, synthesis_count, send_count, text_calls = observations[0]
        self.assertTrue(result.delivered)
        self.assertFalse(result.complete)
        self.assertEqual(parts, ["abcde"])
        self.assertEqual((synthesis_count, send_count, text_calls), (2, 1, ["fghi"]))
        assert_event(
            self, "private_tts", "deliver_private_reply", "ambiguous", "uncertain"
        )

    async def test_recorder_failure_does_not_change_tts_effects_or_fallback(self):
        observations = []
        for function in (
            private_tts.deliver_private_reply,
            private_tts.deliver_private_reply.__wrapped__,
        ):
            message = telegram_message()
            receipt = private_tts.DeliveryReceipt()
            transport = EdgeTransport()
            with (
                patch("edge_tts.Communicate", transport.communicate),
                patch.object(
                    telemetry.observer, "record", side_effect=ValueError(SECRET)
                ),
            ):
                result = await self.deliver(
                    function=function, message=message, receipt=receipt
                )
            observations.append(
                (
                    result,
                    receipt.parts,
                    len(transport.calls),
                    message.answer_audio.await_count,
                )
            )
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0][2:], (1, 1))
        self.assertEqual(telemetry.snapshot()["events"], [])

    async def test_tts_cancellation_retains_prior_delivery_with_broken_recorder(self):
        observations = []
        for function in (
            private_tts.deliver_private_reply,
            private_tts.deliver_private_reply.__wrapped__,
        ):
            telemetry.reset()
            cancelled = asyncio.CancelledError(SECRET)
            message = telegram_message()
            sent = message.answer_audio.return_value
            message.answer_audio.side_effect = [sent, cancelled]
            transport = EdgeTransport()
            receipt = private_tts.DeliveryReceipt()
            with (
                patch("edge_tts.Communicate", transport.communicate),
                patch.object(
                    telemetry.observer, "record", side_effect=RuntimeError(SECRET)
                ) as recorder,
            ):
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await self.deliver(
                        function=function,
                        message=message,
                        receipt=receipt,
                        text="abcdefghi",
                        service=tts_service(segment_length=5),
                    )
            self.assertIs(caught.exception, cancelled)
            observations.append(
                (receipt.parts, len(transport.calls), message.answer_audio.await_count)
            )
            self.assertEqual(
                recorder.call_count, int(function is private_tts.deliver_private_reply)
            )
        self.assertEqual(observations, [(["abcde"], 2, 2)] * 2)
        self.assertEqual(telemetry.snapshot()["events"], [])

    async def test_business_base_exception_identity_bypasses_broken_recorder(self):
        class BusinessAbort(BaseException):
            pass

        abort = BusinessAbort(SECRET)

        class InvalidChatId:
            def __int__(self):
                raise abort

        message = telegram_message()
        message.chat.id = InvalidChatId()
        with patch.object(
            telemetry.observer, "record", side_effect=RuntimeError("recorder failed")
        ) as recorder:
            with self.assertRaises(BusinessAbort) as caught:
                await self.deliver(message=message)
        self.assertIs(caught.exception, abort)
        self.assertEqual(caught.exception.args, (SECRET,))
        recorder.assert_not_called()
        message.bot.get_chat.assert_not_awaited()
        message.answer_voice.assert_not_awaited()
        message.answer_audio.assert_not_awaited()
        self.assertEqual(telemetry.snapshot(), {"events": [], "aggregates": []})

    async def test_tts_exception_and_timeout_identity_even_when_recording_fails(self):
        # Invalid external message metadata raises before the existing helper's
        # get_chat try/except. This exercises real entrypoint exception escape,
        # without replacing the delivery function or its classification result.
        for error, outcome, category in (
            (RuntimeError(SECRET), "failure", "exception"),
            (TimeoutError(SECRET), "timeout", "timeout"),
            (asyncio.CancelledError(SECRET), "cancelled", "cancelled"),
        ):
            for broken_recorder in (False, True):
                telemetry.reset()

                class InvalidChatId:
                    def __init__(self, exception):
                        self.exception = exception

                    def __int__(self):
                        raise self.exception

                message = telegram_message()
                message.chat.id = InvalidChatId(error)
                original_record = telemetry.observer.record
                with (
                    self.subTest(
                        error=type(error).__name__, broken_recorder=broken_recorder
                    ),
                    patch.object(
                        telemetry.observer,
                        "record",
                        side_effect=RuntimeError("recorder failed")
                        if broken_recorder
                        else original_record,
                    ),
                ):
                    with self.assertRaises(type(error)) as caught:
                        await self.deliver(message=message)
                self.assertIs(caught.exception, error)
                message.answer_audio.assert_not_awaited()
                if broken_recorder:
                    self.assertEqual(telemetry.snapshot()["events"], [])
                else:
                    assert_event(
                        self, "private_tts", "deliver_private_reply", outcome, category
                    )
