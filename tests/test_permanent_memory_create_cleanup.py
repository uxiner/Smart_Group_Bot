"""B9 cleanup: real flushed writes, cancellation identity and lock release.

The HTTP route body is also invoked directly for exception identity (a TCP
transport cannot preserve a Python exception object). Real HTTP checks cover
opaque ordinary errors and successful conflict readback in the B9-1 suite.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import unittest
from aiohttp.test_utils import make_mocked_request

from bot.db.models import GroupPermanentMemory
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from test_permanent_memory_create_dedup import (
    GROUP_A, SUPER_ADMIN, _RaceInjectedTestCase,
)


class PermanentMemoryCreateCleanupTests(_RaceInjectedTestCase):
    async def _invoke(self, entry: str):
        if entry == "service":
            return await self._service().add_permanent_memory(
                GROUP_A, "cleanup-target", created_by=SUPER_ADMIN
            )
        route = next(
            route for route in self.client.server.app.router.routes()
            if route.method == "POST"
            and route.resource.canonical == "/api/v1/groups/{id}/memories"
        )
        handler = inspect.unwrap(route.handler)
        request = make_mocked_request("POST", f"/api/v1/groups/{GROUP_A}/memories")
        request._match_info["id"] = str(GROUP_A)

        async def body():
            return {"content": "cleanup-target"}

        request.json = body
        return await handler(request, SimpleNamespace(id=SUPER_ADMIN))

    async def _fault_case(self, entry, original_cancel=False, close_mode="cancel",
                          rollback_fail=True, http=False, successful=False):
        original = (asyncio.CancelledError("original-private-marker")
                    if original_cancel else RuntimeError("original-private-marker"))
        cleanup_cancel = asyncio.CancelledError("cleanup-private-marker")
        real_commit = SQLiteSafeAsyncSession.commit
        real_close = SQLiteSafeAsyncSession.close
        real_rollback = SQLiteSafeAsyncSession.rollback
        state = {"target": None, "commit": 0, "rollback": 0, "close": 0}

        async def commit(session):
            if any(isinstance(obj, GroupPermanentMemory) for obj in session.new):
                state["target"] = session
                state["commit"] += 1
                await session.flush()  # acquire the real write lock, not a fake
                if not successful:
                    raise original
            return await real_commit(session)

        async def rollback(session):
            if session is state["target"]:
                state["rollback"] += 1
                if rollback_fail:
                    raise RuntimeError("rollback-private-marker")
            return await real_rollback(session)

        async def close(session):
            if session is state["target"]:
                state["close"] += 1
                if close_mode == "all_fail":
                    raise RuntimeError("close-private-marker")
                if state["close"] == 1:
                    if close_mode == "cancel":
                        raise cleanup_cancel
                    if close_mode == "error":
                        raise RuntimeError("close-private-marker")
            return await real_close(session)

        caught = None
        response = None
        try:
            with patch.object(SQLiteSafeAsyncSession, "commit", commit), \
                 patch.object(SQLiteSafeAsyncSession, "rollback", rollback), \
                 patch.object(SQLiteSafeAsyncSession, "close", close):
                try:
                    if http:
                        response = await self.client.post(
                            f"/api/v1/groups/{GROUP_A}/memories",
                            headers=self._headers(), json={"content": "cleanup-target"},
                        )
                    else:
                        response = await self._invoke(entry)
                except BaseException as exc:
                    caught = exc
                target = state["target"]
                self.assertIsNotNone(target, "fault must hit the real create session")
                self.assertEqual(state["commit"], 1, "no compensation/retry commit")
                if close_mode == "all_fail":
                    self.assertIsNotNone(caught, "unreleased resources cannot return success")
                    self.assertEqual(state["close"], 2, "bounded recovery attempts")
                    return
                # Inspect BEFORE fixture teardown and before a fresh writer.
                self.assertFalse(target.in_transaction(), "transaction must end before exit")
                if not successful:
                    self.assertEqual(state["rollback"], 2 if rollback_fail else 1)
                self.assertEqual(state["close"], 2 if close_mode in {"cancel", "error"} else 1)
                if http:
                    self.assertIsNone(caught)
                    self.assertEqual(response.status, 500)
                    body = await response.text()
                    self.assertNotIn("private-marker", body)
                    self.assertNotIn("RuntimeError", body)
                elif original_cancel:
                    self.assertIs(caught, original)
                elif close_mode == "cancel":
                    self.assertIs(caught, cleanup_cancel)
                elif not successful:
                    self.assertIs(caught, original)
                else:
                    self.assertIsNone(caught)
                await self._assert_write_lock_available(timeout=1.0)
        finally:
            # Baseline failures may strand resources. This is teardown only;
            # assertions above must observe the unassisted entry-point state.
            if state["target"] is not None:
                await real_close(state["target"])

    async def test_service_original_cancel_identity(self):
        await self._fault_case("service", original_cancel=True)

    async def test_http_body_original_cancel_identity(self):
        await self._fault_case("http", original_cancel=True)

    async def test_service_new_cleanup_cancel_identity(self):
        await self._fault_case("service")

    async def test_http_body_new_cleanup_cancel_identity(self):
        await self._fault_case("http")

    async def test_service_ordinary_error_survives_cleanup_errors(self):
        await self._fault_case("service", close_mode="error")

    async def test_http_body_ordinary_error_survives_cleanup_errors(self):
        await self._fault_case("http", close_mode="error")

    async def test_service_normal_rollback_close_after_commit_error(self):
        await self._fault_case("service", close_mode="ok", rollback_fail=False)

    async def test_http_body_normal_rollback_close_after_commit_error(self):
        await self._fault_case("http", close_mode="ok", rollback_fail=False)

    async def test_real_http_error_is_opaque_and_releases_writer(self):
        await self._fault_case("http", close_mode="error", http=True)

    async def test_service_committed_write_recovers_close_error(self):
        await self._fault_case("service", close_mode="error", successful=True)

    async def test_http_body_committed_write_recovers_close_error(self):
        await self._fault_case("http", close_mode="error", successful=True)

    async def test_service_unreleased_cannot_return_success(self):
        await self._fault_case("service", close_mode="all_fail", successful=True)

    async def test_http_body_unreleased_cannot_return_success(self):
        await self._fault_case("http", close_mode="all_fail", successful=True)


class SharedCreateScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_scope_unreleased_is_not_success(self):
        from bot.services.memory import _pm_create_session

        class Session:
            async def close(self):
                raise RuntimeError("private-marker")

        with self.assertRaisesRegex(RuntimeError, "^permanent_memory_cleanup_unreleased$"):
            async with _pm_create_session(Session):
                pass
