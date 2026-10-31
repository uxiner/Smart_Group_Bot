"""Actual producers -> privileged router/renderer, with external transports only.

No network, model, business database or cleanup queue is needed for observation
reads. Telegram evidence stops at the real aiogram BaseSession request boundary;
no always-success replacement for the delivery helper is used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User

from bot.handlers import self_check as handler
from bot.services import context_observability as context_obs
from bot.services import dm_search, private_tts
from bot.services import self_check as check
from bot.services import tool_observability as tool_obs
from bot.services.context_gate import assemble_context_within_budget
from bot.services.doubao_tts import DoubaoTTSService
from bot.services.skills import webfetch
from bot.services.skills.base import SkillContext
from bot.services.skills.service import SkillService
from bot.services.skills.websearch import WebSearchSkill
from bot.utils import telegram as telegram_helpers

CANARY = "SECRET-CANARY-raw-query-content-user-id-918273-token"
OWNER = 900001
EPOCH = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
URL = f"https://example.test/article?token={CANARY}"
FETCH = (200, f"<p>{CANARY}</p>", URL, "text/html")


def resources():
    return {"ok": True, "fatal": False, "system": {"rss_bytes": 1024}, "resources": {}}


def now():
    return EPOCH


def assembly(**changes):
    return assemble_context_within_budget(
        **{
            "system": [{"role": "system", "content": "fixed"}],
            "current_turn": [{"role": "user", "content": CANARY}],
            "history": [{"role": "user", "content": CANARY + "中" * 2000}] * 2,
            "budget_tokens": 1024,
            **changes,
        }
    )


def report(**changes):
    return check.build_report(
        snapshot_provider=resources, now=now, environ={}, **changes
    )


def pathway(result, name):
    return result["tool_observations"]["pathways"][name]


def update(*, user_id=OWNER, chat_type="private", is_bot=False):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=EPOCH,
            chat=Chat(id=OWNER if chat_type == "private" else -1000001, type=chat_type),
            from_user=None
            if user_id is None
            else User(id=user_id, is_bot=is_bot, first_name=CANARY),
            text="/selfcheck",
        ),
    )


class Transport(BaseSession):
    """Real Telegram method boundary; reject any unexpected call, retain counts."""

    def __init__(self, *, fail_text=False):
        super().__init__()
        self.calls = []
        self.fail_text = fail_text

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method))
        if name == "GetMe":
            return User(id=42, is_bot=True, first_name="Bot", username="OfflineBot")
        if name == "GetChat":
            return Chat(
                id=OWNER, type="private", has_restricted_voice_and_video_messages=True
            )
        if name == "SendMessage" and self.fail_text:
            raise RuntimeError(CANARY)
        if name not in {"SendMessage", "SendAudio"}:
            raise AssertionError(f"unexpected Telegram method: {name}")
        return Message(
            message_id=100 + len(self.calls),
            date=EPOCH,
            chat=Chat(id=OWNER, type="private"),
            text=method.text if name == "SendMessage" else None,
        ).as_(bot)

    def counts(self, name):
        return sum(method_name == name for method_name, _ in self.calls)


class EdgeTransport:
    def __init__(self, *, fail_from=0):
        self.calls = 0
        self.fail_from = fail_from

    def communicate(self, text, voice, **kwargs):
        self.calls += 1
        index = self.calls

        async def stream():
            if self.fail_from and index >= self.fail_from:
                raise RuntimeError(CANARY)
            yield {"type": "audio", "data": b"ID3-offline-synthetic"}

        return SimpleNamespace(stream=stream)


def tts_service():
    return DoubaoTTSService(
        SimpleNamespace(
            doubao_tts_enabled=True,
            doubao_tts_provider="edge",
            doubao_tts_speaker="zh-CN-XiaoxiaoNeural",
            doubao_tts_audio_format="mp3",
            doubao_tts_max_text_length=5,
        )
    )


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden in selfcheck integration")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(check, "resource_health_snapshot", resources)
    check.reset_cache()
    tool_obs.reset()
    context_obs.reset_context_observations()
    private_tts.voice_restriction_cache().clear()
    yield
    check.reset_cache()
    tool_obs.reset()
    context_obs.reset_context_observations()
    private_tts.voice_restriction_cache().clear()


class ProducerToCommandTests(unittest.IsolatedAsyncioTestCase):
    async def group_fetch(self, transport=None):
        with patch.object(
            webfetch, "fetch_text", transport or AsyncMock(return_value=FETCH)
        ):
            return await SkillService(SimpleNamespace())._run_tool(
                name="webfetch", arguments={"url": URL}, context=SkillContext()
            )

    async def search(self, *, error=None):
        with patch.object(
            WebSearchSkill,
            "_search_once",
            **({"side_effect": error} if error else {"return_value": []}),
        ):
            return await dm_search.run_search(CANARY)

    async def command(self, incoming=None, *, super_admin_id=OWNER):
        session = Transport()
        self.last_command_session = session
        bot = Bot(token="42:OFFLINE_OBSERVABILITY", session=session)
        self.addAsyncCleanup(bot.session.close)
        dispatcher = Dispatcher()
        handler.router._parent_router = None
        dispatcher.include_router(handler.router)
        dispatcher["settings"] = SimpleNamespace(
            super_admin_id=super_admin_id, bot=SimpleNamespace()
        )
        await dispatcher.feed_update(bot, incoming or update())
        messages = [
            method.text for name, method in session.calls if name == "SendMessage"
        ]
        self.assertEqual(session.counts("SendMessage"), 1)
        self.assertTrue(
            all(name in {"GetMe", "SendMessage"} for name, _ in session.calls)
        )
        return messages[0]

    async def test_empty_actual_snapshots_are_unknown_in_production_router(self):
        before = tool_obs.snapshot(), context_obs.snapshot_context_observations()
        text = await self.command()
        normalized = report()
        self.assertEqual(normalized["tool_observations"]["state"], "not_observed")
        self.assertEqual(normalized["context_observations"]["state"], "not_observed")
        self.assertEqual(
            before, (tool_obs.snapshot(), context_obs.snapshot_context_observations())
        )
        self.assertIn("暂无观测", text)
        self.assertNotIn(CANARY, text)

    async def test_real_tool_and_trim_samples_reach_command_without_resampling_tools(
        self,
    ):
        self.assertTrue((await self.group_fetch()).ok)
        await self.search()
        assembled = assembly()
        before = (
            copy.deepcopy(tool_obs.snapshot()),
            context_obs.snapshot_context_observations(),
        )
        with (
            patch.object(
                webfetch, "fetch_text", side_effect=AssertionError("no fetch")
            ),
            patch.object(
                WebSearchSkill, "_search_once", side_effect=AssertionError("no search")
            ),
            patch("edge_tts.Communicate", side_effect=AssertionError("no synthesis")),
            patch(
                "bot.services.context_gate.assemble_context_within_budget",
                side_effect=AssertionError("no assembly"),
            ),
            patch.object(tool_obs, "reset", side_effect=AssertionError("no reset")),
            patch.object(
                context_obs,
                "reset_context_observations",
                side_effect=AssertionError("no reset"),
            ),
        ):
            text = await self.command()
            second = await self.command()
        self.assertEqual(text, second)
        self.assertEqual(
            before, (tool_obs.snapshot(), context_obs.snapshot_context_observations())
        )
        normalized = report()
        self.assertEqual(pathway(normalized, "group_skill")["cumulative_calls"], 1)
        self.assertEqual(pathway(normalized, "private_search")["cumulative_calls"], 1)
        self.assertEqual(
            normalized["context_observations"]["latest"]["used_tokens"],
            assembled.used_tokens,
        )
        self.assertIn("估算", text)
        self.assertIn("全局", text)
        self.assertNotIn(CANARY, text)
        self.assertLessEqual(len(text), 3500)

    async def test_failed_timeout_and_cancelled_real_entrypoints_are_not_success(self):
        await self.group_fetch(AsyncMock(side_effect=RuntimeError(CANARY)))
        await self.search(error=TimeoutError(CANARY))
        cancelled = asyncio.CancelledError(CANARY)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.group_fetch(AsyncMock(side_effect=cancelled))
        self.assertIs(caught.exception, cancelled)
        result = report()
        group = pathway(result, "group_skill")
        self.assertEqual(group["cumulative_outcomes"]["success"], 0)
        self.assertEqual(group["cumulative_outcomes"]["failure"], 1)
        self.assertEqual(group["cumulative_outcomes"]["cancelled"], 1)
        self.assertEqual(
            pathway(result, "private_search")["cumulative_outcomes"]["timeout"], 1
        )
        text = await self.command()
        self.assertIn("timeout", text)
        self.assertIn("cancelled", text)
        self.assertNotIn(CANARY, text)

    async def test_window_64_is_distinct_from_cumulative_actual_calls(self):
        with patch.object(
            webfetch, "fetch_text", AsyncMock(return_value=FETCH)
        ) as backend:
            service = SkillService(SimpleNamespace())
            for _ in range(70):
                await service._run_tool(
                    name="webfetch", arguments={"url": URL}, context=SkillContext()
                )
        self.assertEqual(backend.await_count, 70)
        row = pathway(report(), "group_skill")
        self.assertEqual(row["cumulative_calls"], 70)
        self.assertEqual(row["recent_calls"], 64)
        self.assertEqual(row["cumulative_outcomes"]["success"], 70)
        self.assertEqual(row["recent_outcomes"]["success"], 64)
        self.assertGreaterEqual(
            row["cumulative_duration_seconds"], row["recent_duration_seconds"]
        )
        text = await self.command()
        self.assertIn("64", text)
        self.assertIn("累计", text)
        self.assertNotIn("实时成功", text)

    async def test_tts_actual_send_audio_is_success_and_selfcheck_does_not_send_voice(
        self,
    ):
        session = Transport()
        bot = Bot(token="42:OFFLINE_OBSERVABILITY", session=session)
        self.addAsyncCleanup(bot.session.close)
        message = update().message.as_(bot)
        edge = EdgeTransport()
        receipt = private_tts.DeliveryReceipt()
        with patch("edge_tts.Communicate", edge.communicate):
            outcome = await private_tts.deliver_private_reply(
                message,
                text="abcde",
                delivery="voice",
                service=tts_service(),
                receipt=receipt,
            )
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.medium, "audio")
        self.assertEqual(
            (edge.calls, session.counts("GetChat"), session.counts("SendAudio")),
            (1, 1, 1),
        )
        self.assertEqual(receipt.parts, ["abcde"])
        self.assertEqual(
            pathway(report(), "private_tts")["cumulative_outcomes"]["success"], 1
        )
        await self.command()
        self.assertEqual(session.counts("SendAudio"), 1)

    async def test_partial_and_successful_fallback_are_uncertain_not_voice_success(
        self,
    ):
        for fail_from, fail_text, expected_parts, sends in (
            (2, True, ["abcde"], 1),
            (1, False, ["abcdefghi"], 0),
        ):
            with self.subTest(fail_from=fail_from, fail_text=fail_text):
                tool_obs.reset()
                private_tts.voice_restriction_cache().clear()
                session = Transport(fail_text=fail_text)
                bot = Bot(token="42:OFFLINE_OBSERVABILITY", session=session)
                self.addAsyncCleanup(bot.session.close)
                message = update().message.as_(bot)
                edge = EdgeTransport(fail_from=fail_from)
                receipt = private_tts.DeliveryReceipt()

                async def send_text(text, book, message=message):
                    await telegram_helpers.answer_with_auto_delete(
                        message, text, auto_delete_seconds=0
                    )
                    book.add(text)

                with patch("edge_tts.Communicate", edge.communicate):
                    outcome = await private_tts.deliver_private_reply(
                        message,
                        text="abcdefghi",
                        delivery="voice",
                        service=tts_service(),
                        receipt=receipt,
                        send_text=send_text,
                    )
                self.assertTrue(outcome.delivered)
                self.assertEqual(outcome.complete, not fail_text)
                self.assertEqual(receipt.parts, expected_parts)
                self.assertEqual(session.counts("SendAudio"), sends)
                self.assertEqual(session.counts("SendMessage"), 1)
                row = pathway(report(), "private_tts")
                self.assertEqual(row["cumulative_outcomes"]["ambiguous"], 1)
                self.assertEqual(row["cumulative_outcomes"]["success"], 0)
                check.reset_cache()
                text = await self.command()
                self.assertIn("ambiguous", text)
                self.assertNotIn(CANARY, text)

    async def test_context_provider_failure_preserves_actual_tool_sample_in_router(
        self,
    ):
        await self.group_fetch()
        with patch.object(check, "context_snapshot", side_effect=RuntimeError(CANARY)):
            normalized = report()
            text = await self.command()
        self.assertEqual(pathway(normalized, "group_skill")["cumulative_calls"], 1)
        self.assertFalse(normalized["context_observations"]["sampling_ok"])
        self.assertIn("RSS", text)
        self.assertIn("累计", text)
        self.assertNotIn(CANARY, text)

    async def test_snapshot_cancellation_identity_through_real_router(self):
        for alias in ("tool_snapshot", "context_snapshot"):
            check.reset_cache()
            cancelled = asyncio.CancelledError(CANARY)
            with (
                self.subTest(alias=alias),
                patch.object(check, alias, side_effect=cancelled),
                self.assertRaises(asyncio.CancelledError) as caught,
            ):
                await self.command()
            self.assertIs(caught.exception, cancelled)
            self.assertEqual(self.last_command_session.counts("SendMessage"), 0)

    async def test_all_denials_precede_both_snapshot_reads(self):
        for incoming, configured in (
            (update(user_id=OWNER + 1), OWNER),
            (update(user_id=None), OWNER),
            (update(is_bot=True), OWNER),
            (update(chat_type="supergroup"), OWNER),
            (update(), 0),
        ):
            with (
                self.subTest(configured=configured),
                patch.object(
                    check, "tool_snapshot", side_effect=AssertionError("must not read")
                ) as tool,
                patch.object(
                    check,
                    "context_snapshot",
                    side_effect=AssertionError("must not read"),
                ) as context,
                patch.object(
                    check,
                    "resource_health_snapshot",
                    side_effect=AssertionError("must not read"),
                ) as resource,
            ):
                text = await self.command(incoming, super_admin_id=configured)
                tool.assert_not_called()
                context.assert_not_called()
                resource.assert_not_called()
                self.assertIn(
                    text, (handler.DENIED_NOTICE, handler.PRIVATE_ONLY_NOTICE)
                )


class SnapshotAndRenderTests(unittest.TestCase):
    def test_context_exact_latest_and_fixed_layer_cumulative_numbers(self):
        first = assembly()
        second = assembly(system=[{"role": "system", "content": "中" * 2000}])
        result = report()
        section = result["context_observations"]
        self.assertEqual(section["observation_count"], 2)
        self.assertEqual(section["latest"]["used_tokens"], second.used_tokens)
        self.assertEqual(section["latest"]["budget_tokens"], second.budget_tokens)
        self.assertTrue(section["latest"]["over_budget"])
        self.assertEqual(set(section["trim_rows"]), set(context_obs.TRIM_LAYERS))
        for layer, row in section["trim_rows"].items():
            for field in ("dropped_messages", "truncated_messages", "dropped_tokens"):
                self.assertEqual(
                    row["latest"][field],
                    sum(getattr(t, field) for t in second.trims if t.layer == layer),
                )
                self.assertEqual(
                    row["cumulative"][field],
                    sum(
                        getattr(t, field)
                        for a in (first, second)
                        for t in a.trims
                        if t.layer == layer
                    ),
                )
        text = check.render_self_check_text(result)
        self.assertIn("sampled_at_unix_ns", text)
        self.assertIn(
            str(context_obs.snapshot_context_observations().latest.sampled_at_unix_ns),
            text,
        )
        self.assertNotIn(CANARY, text)

    def test_each_fixed_trim_layer_counts_actual_assembly(self):
        for layer in context_obs.TRIM_LAYERS:
            context_obs.reset_context_observations()
            actual = (
                assembly(
                    history=[],
                    **{
                        layer: [
                            {"role": "user", "content": CANARY + "中" * 1500},
                            {"role": "user", "content": CANARY + "中" * 1500},
                        ]
                    },
                )
                if layer != "history"
                else assembly()
            )
            counts = report()["context_observations"]["trim_rows"][layer]["latest"]
            self.assertGreater(counts["dropped_messages"], 0)
            self.assertGreater(counts["truncated_messages"], 0)
            self.assertEqual(
                counts["dropped_tokens"],
                sum(t.dropped_tokens for t in actual.trims if t.layer == layer),
            )

    def test_renderer_ordinary_exception_isolated_but_lifecycle_propagates(self):
        assembly()
        normalized = report()
        for seam in ("_render_tool_observations", "_render_context_observations"):
            with patch.object(check, seam, side_effect=RuntimeError(CANARY)):
                text = check.render_self_check_text(normalized)
            self.assertIn("RSS", text)
            self.assertNotIn(CANARY, text)
            if seam == "_render_tool_observations":
                self.assertIn("估算", text)
            for error in (asyncio.CancelledError(CANARY), KeyboardInterrupt(CANARY)):
                with (
                    patch.object(check, seam, side_effect=error),
                    self.assertRaises(type(error)) as caught,
                ):
                    check.render_self_check_text(normalized)
                self.assertIs(caught.exception, error)

    def test_single_provider_failure_preserves_resource_and_other_section(self):
        assembly()
        with patch.object(check, "tool_snapshot", side_effect=RuntimeError(CANARY)):
            result = report()
        self.assertTrue(result["sampling_ok"])
        self.assertTrue(result["overall_ok"])
        self.assertFalse(result["tool_observations"]["sampling_ok"])
        self.assertEqual(result["context_observations"]["state"], "observed")
        text = check.render_self_check_text(result)
        self.assertIn("RSS", text)
        self.assertNotIn(CANARY, text)
        with patch.object(check, "context_snapshot", side_effect=RuntimeError(CANARY)):
            result = report()
        self.assertTrue(result["sampling_ok"])
        self.assertFalse(result["context_observations"]["sampling_ok"])

    def test_resource_provider_failure_does_not_erase_context(self):
        assembly()
        with patch.object(
            check, "resource_health_snapshot", side_effect=RuntimeError(CANARY)
        ):
            result = check.build_report(now=now, environ={})
        self.assertFalse(result["sampling_ok"])
        self.assertEqual(result["context_observations"]["state"], "observed")
        self.assertNotIn(CANARY, check.render_self_check_text(result))

    def test_malformed_snapshot_numeric_and_labels_fail_closed_without_leak(self):
        assembly()
        actual = context_obs.snapshot_context_observations()
        bad_contexts = [
            None,
            {"state": CANARY},
            replace(actual, latest=replace(actual.latest, used_tokens=CANARY)),
            replace(actual, observation_count=-1),
        ]
        bad_tools = [
            None,
            {"events": CANARY, "aggregates": CANARY},
            {
                "events": [{"scope": CANARY}],
                "aggregates": [{"scope": CANARY, "calls": CANARY}],
            },
        ]
        for sample in bad_contexts:
            with self.subTest(sample_type=type(sample).__name__):
                result = report(context_snapshot_provider=lambda sample=sample: sample)
                self.assertEqual(
                    result["context_observations"]["state"], "sampling_failed"
                )
                self.assertNotIn(CANARY, json.dumps(result))
                self.assertNotIn(CANARY, check.render_self_check_text(result))
        for sample in bad_tools:
            with self.subTest(sample_type=type(sample).__name__):
                result = report(tool_snapshot_provider=lambda sample=sample: sample)
                self.assertIn(
                    result["tool_observations"]["state"],
                    ("sampling_failed", "not_observed"),
                )
                if result["tool_observations"]["state"] == "sampling_failed":
                    self.assertFalse(result["tool_observations"]["sampling_ok"])
                else:
                    self.assertTrue(
                        all(
                            row["cumulative_calls"] == 0
                            for row in result["tool_observations"]["pathways"].values()
                        )
                    )
                self.assertNotIn(CANARY, json.dumps(result))
                self.assertEqual(result["context_observations"]["state"], "observed")

    def test_provider_cancellation_and_lifecycle_identity_propagate(self):
        for provider_key in ("tool_snapshot_provider", "context_snapshot_provider"):
            for error in (
                asyncio.CancelledError(CANARY),
                KeyboardInterrupt(CANARY),
                SystemExit(CANARY),
            ):
                with self.subTest(
                    provider_key=provider_key, error_type=type(error).__name__
                ):
                    provider = Mock(side_effect=error)
                    with self.assertRaises(type(error)) as caught:
                        report(**{provider_key: provider})
                    self.assertIs(caught.exception, error)

    def test_rendered_bound_whitelists_even_direct_report_injection(self):
        result = report()
        result.update(
            checked_at=CANARY * 1000,
            build_revision=CANARY * 1000,
            timezone=CANARY,
            uptime_basis=CANARY,
        )
        result["tool_observations"] = {
            "state": "observed",
            "pathways": [{"scope": CANARY, "cumulative_calls": 10**1000}] * 1000,
        }
        result["context_observations"] = {
            "state": CANARY,
            "latest": {"used_tokens": CANARY},
            "trim_rows": [CANARY] * 1000,
        }
        text = check.render_self_check_text(result)
        self.assertLessEqual(len(text), 3500)
        self.assertNotIn(CANARY, text)
        self.assertIn("RSS", text)

    def test_final_formatter_escapes_markup_from_numeric_seam(self):
        """正向防御：即使数值/分段渲染缝返回标记，最终文本也必须转义它。

        与时间戳白名单负向用例互补——那条证明非白名单值不进入渲染输入，
        这条证明任何进入渲染的字符串都会在最终拼装处被 HTML 转义。
        """

        result = report()
        with patch.object(
            check, "_render_metrics", return_value="<metric-canary &>"
        ) as seam:
            text = check.render_self_check_text(result)
        self.assertTrue(seam.called)
        self.assertIn("&lt;metric-canary &amp;&gt;", text)
        self.assertNotIn("<metric-canary &>", text)

    def test_cache_scopes_provider_expiry_reset_and_detached_reports(self):
        assembly()
        clock = [10.0]

        def monotonic():
            return clock[0]

        tool = Mock(wraps=tool_obs.snapshot)
        context = Mock(wraps=context_obs.snapshot_context_observations)
        kwargs = {
            "snapshot_provider": resources,
            "now": now,
            "monotonic": monotonic,
            "environ": {},
            "tool_snapshot_provider": tool,
            "context_snapshot_provider": context,
        }
        first = check.self_check_report(**kwargs)
        first["context_observations"]["latest"]["used_tokens"] = -1
        context_obs.reset_context_observations()
        cached = check.self_check_report(**kwargs)
        self.assertEqual(cached["context_observations"]["state"], "observed")
        self.assertGreaterEqual(
            cached["context_observations"]["latest"]["used_tokens"], 0
        )
        self.assertEqual((tool.call_count, context.call_count), (1, 1))
        self.assertEqual(cached["checked_at"], first["checked_at"])
        clock[0] += check.CACHE_TTL_SECONDS + 0.01
        expired = check.self_check_report(**kwargs)
        self.assertEqual(expired["context_observations"]["state"], "not_observed")
        self.assertEqual((tool.call_count, context.call_count), (2, 2))
        check.reset_cache()
        check.self_check_report(**kwargs)
        self.assertEqual((tool.call_count, context.call_count), (3, 3))
        other = Mock(wraps=tool_obs.snapshot)
        check.self_check_report(**{**kwargs, "tool_snapshot_provider": other})
        self.assertEqual(other.call_count, 1)
        self.assertEqual(set(check._CACHE), {"monotonic", "report"})

    def test_cache_reset_never_resets_observations(self):
        assembly()
        before = context_obs.snapshot_context_observations(), tool_obs.snapshot()
        check.reset_cache()
        check.render_self_check_text(report())
        self.assertEqual(
            before, (context_obs.snapshot_context_observations(), tool_obs.snapshot())
        )
