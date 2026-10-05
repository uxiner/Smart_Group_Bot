#!/usr/bin/env python3
"""Loopback-only UI harness for the Smart_Bot settings Mini App.

This is a **development tool**. It is not part of the release page, it is not
imported by ``bot/``, and the production image never copies ``tools/`` (see the
Dockerfile). It only ever binds ``127.0.0.1``, serves the committed synthetic
fixture, and simulates the settings API in memory. It never reads ``.env``,
never opens the production database, and never talks to Telegram.

Usage::

    python3 tools/settings-ui-harness/harness.py --port 8781

Then open http://127.0.0.1:8781/settings

Flags:
    --group-admin   serve the group-admin session (can_manage_global = false)
    --fail-save     make every PUT answer 503 so the error path can be seen
    --no-groups     make /api/v1/groups fail so the error state can be seen
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bot" / "web" / "static"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings.json"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

TELEGRAM_STUB = """/* Loopback harness stub: never contacted by the real page.
   Field names and types follow the official Telegram WebApp docs (Bot API 8.0+):
     safeAreaInset        -> SafeAreaInset        { top, bottom, left, right }
     contentSafeAreaInset -> ContentSafeAreaInset { top, bottom, left, right }
     viewportHeight / viewportStableHeight -> numbers (CSS pixels)
   __setInsets / __setViewport are harness-only drivers for the browser checks. */
(function () {
  var listeners = {};
  var webApp = {
    initData: "harness=1",
    initDataUnsafe: { user: { id: 42, first_name: "Fixture", language_code: "zh" } },
    version: "8.0",
    platform: "harness",
    colorScheme: "dark",
    themeParams: { bg_color: "#0C1220", text_color: "#EEF3FC", hint_color: "#A8B5CC", link_color: "#71D9EF", button_color: "#71D9EF", button_text_color: "#062430" },
    isExpanded: true,
    isActive: true,
    isFullscreen: false,
    viewportHeight: 0,
    viewportStableHeight: 0,
    safeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    ready: function () {},
    expand: function () {},
    close: function () {},
    requestFullscreen: function () {},
    exitFullscreen: function () {},
    setHeaderColor: function () {},
    setBackgroundColor: function () {},
    disableVerticalSwipes: function () {},
    enableClosingConfirmation: function () {},
    onEvent: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    offEvent: function (name, fn) {
      listeners[name] = (listeners[name] || []).filter(function (item) { return item !== fn; });
    },
    emit: function (name, payload) {
      (listeners[name] || []).slice().forEach(function (fn) { fn(payload); });
    },
    __setInsets: function (safe, content) {
      webApp.safeAreaInset = Object.assign({ top: 0, bottom: 0, left: 0, right: 0 }, safe || {});
      webApp.contentSafeAreaInset = Object.assign({ top: 0, bottom: 0, left: 0, right: 0 }, content || {});
      webApp.emit("safeAreaChanged", webApp.safeAreaInset);
      webApp.emit("contentSafeAreaChanged", webApp.contentSafeAreaInset);
    },
    __setViewport: function (height, stableHeight) {
      webApp.viewportHeight = height;
      webApp.viewportStableHeight = stableHeight;
      webApp.emit("viewportChanged", { isStateStable: true });
    },
  };
  window.Telegram = { WebApp: webApp };
})();
"""

RESOURCE_TYPES = {
    "rules": "rules",
    "memories": "memories",
    "warnings": "warnings",
    "bans": "bans",
    "moderation-exemptions": "exemptions",
    "reply-mutes": "reply_mutes",
    "keyword-replies": "keyword_replies",
    "scheduled-messages": "scheduled_messages",
}


class State:
    def __init__(self, fixture: dict, *, group_admin: bool, fail_save: bool, no_groups: bool):
        self.fixture = fixture
        self.group_admin = group_admin
        self.fail_save = fail_save
        self.no_groups = no_groups
        self.settings = deepcopy(fixture["settings"])
        self.groups = deepcopy(fixture["groups"]["groups"])
        self.resources = deepcopy(fixture["resources"])
        self.next_id = 9000
        self.requests: list[str] = []

    def group_resources(self, group_id: str) -> dict:
        key = str(group_id).lstrip("-")
        return self.resources.get(key, {})

    def bump_revision(self) -> int:
        self.settings["revision"] = int(self.settings["revision"]) + 1
        return self.settings["revision"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[harness] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), CONTENT_TYPES[".json"])

    def _error(self, status: int, message: str, code: str = "harness_error") -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # -- verbs ------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = dict(
            pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
        )
        state = self.state
        state.requests.append(f"{method} {path}")

        try:
            if path == "/" or path == "/settings":
                return self._send(200, self._index_html(), CONTENT_TYPES[".html"])
            if path == "/harness/telegram-stub.js":
                return self._send(200, TELEGRAM_STUB.encode("utf-8"), CONTENT_TYPES[".js"])
            if path.startswith("/settings-assets/"):
                return self._asset(path[len("/settings-assets/"):])
            if path == "/harness/requests":
                return self._json({"requests": state.requests})
            if path == "/harness/reset":
                fresh = json.loads(FIXTURE.read_text(encoding="utf-8"))
                state.__init__(  # noqa: PLC2801 - deliberate in-place reset for the dev harness
                    fresh,
                    group_admin=state.group_admin,
                    fail_save=state.fail_save,
                    no_groups=state.no_groups,
                )
                return self._json({"reset": True})
            if path.startswith("/api/v1/"):
                return self._api(method, path[len("/api/v1/"):], query)
            self._error(404, f"harness has no route for {path}", "not_found")
        except BrokenPipeError:  # pragma: no cover - browser aborted a request
            pass
        except Exception as error:  # pragma: no cover - harness bug surface
            self._error(500, f"harness error: {error}", "harness_crash")

    def _index_html(self) -> bytes:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # Swap the CDN Telegram SDK for a local stub: the harness must not make
        # any network request at all, and the production page keeps its own tag.
        html = html.replace(
            '<script src="https://telegram.org/js/telegram-web-app.js"></script>',
            '<script src="/harness/telegram-stub.js"></script>',
        )
        return html.encode("utf-8")

    def _asset(self, name: str) -> None:
        if "/" in name or ".." in name:
            return self._error(400, "bad asset path", "bad_path")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._error(404, f"no asset {name}", "not_found")
        self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    # -- simulated API ----------------------------------------------------
    def _api(self, method: str, route: str, query: dict) -> None:
        state = self.state
        segments = [segment for segment in route.split("/") if segment]

        if route == "session" and method == "GET":
            key = "group_admin_session" if state.group_admin else "session"
            return self._json({"session": state.fixture[key]})

        if route == "settings":
            if method == "GET":
                if state.group_admin:
                    return self._error(403, "只有最高管理员可以查看全局配置", "forbidden")
                return self._json(state.settings)
            if method in {"PUT", "PATCH"}:
                if state.fail_save:
                    return self._error(503, "模拟保存失败：后端拒绝了这次写入", "runtime_config_unavailable")
                body = self._body()
                if not body.get("config"):
                    return self._error(400, "缺少 config", "bad_request")
                state.settings["config"] = body["config"]
                state.bump_revision()
                return self._json(state.settings)

        if route == "groups" and method == "GET":
            if state.no_groups:
                return self._error(503, "模拟群组列表加载失败", "database_unavailable")
            return self._json({"groups": state.groups})

        group_match = re.fullmatch(r"groups/(-?\d+)(?:/(.*))?", route)
        if group_match:
            group_id, tail = group_match.group(1), group_match.group(2)
            group = next((item for item in state.groups if str(item["id"]) == str(group_id)), None)
            if group is None:
                return self._error(404, f"harness 没有群 {group_id}", "not_found")
            if tail == "settings":
                if method == "GET":
                    return self._json(group)
                if method in {"PUT", "PATCH"}:
                    if state.fail_save:
                        return self._error(503, "模拟群设置保存失败", "group_settings_unavailable")
                    body = self._body()
                    group["settings"] = {**group["settings"], **(body.get("settings") or {})}
                    group["revision"] = str(int(group["revision"]) + 1)
                    return self._json(
                        {
                            "settings": group["settings"],
                            "group": {"id": group["id"], "revision": group["revision"]},
                        }
                    )
            if tail == "default-permissions" and method == "GET":
                return self._json(
                    {
                        "default_permissions": group["settings"].get("default_permissions") or {
                            "can_send_messages": True,
                            "can_send_polls": True,
                            "can_invite_users": True,
                            "can_pin_messages": True,
                        },
                        "configured": False,
                        "repaired": False,
                        "permission_fields": [
                            {"key": "can_send_messages", "label": "发送消息"},
                            {"key": "can_send_polls", "label": "发送投票"},
                            {"key": "can_invite_users", "label": "邀请用户"},
                            {"key": "can_pin_messages", "label": "置顶消息"},
                        ],
                    }
                )
            if tail == "telegram-admins" and method == "GET":
                return self._json(state.fixture["telegram_admins"])
            if tail == "admins" and method == "GET":
                return self._json(state.fixture["admins"])
            if tail == "patrol" and method == "GET":
                return self._json({"last_run_at": "", "checked": 0, "flagged": 0})

            resource = RESOURCE_TYPES.get(tail or "")
            if resource:
                bucket = state.group_resources(group_id)
                return self._resource(method, group_id, resource, tail or "", query)

        if route == "authorized-groups" and method == "GET":
            return self._json(state.fixture["authorized_groups"])
        if route in {"global-bans", "global-exemptions"}:
            key = route.replace("-", "_")
            if method == "GET":
                return self._json(state.fixture[key])
            if method == "POST":
                state.next_id += 1
                payload = dict(state.fixture[key][key][0])
                payload.update({field: value for field, value in self._body().items() if field in payload})
                state.fixture[key][key] = [payload, *state.fixture[key][key]]
                return self._json(payload)
        if route == "admins" and method == "GET":
            return self._json(state.fixture["admins"])

        self._error(404, f"harness has no API route for {method} /api/v1/{route}", "not_found")

    def _resource(self, method: str, group_id: str, resource: str, tail: str, query: dict) -> None:
        state = self.state
        bucket = state.group_resources(group_id)
        items = bucket.setdefault(resource, [])

        if method == "GET":
            if tail != resource:
                target = next((item for item in items if str(item.get("id")) == tail), None)
                if target is None:
                    return self._error(404, "记录不存在", "not_found")
                return self._json(target)
            start = int(query.get("offset") or 0)
            limit = int(query.get("limit") or 500)
            window = items[start:start + limit]
            payload = {resource: window, "next_offset": start + limit if start + limit < len(items) else None}
            return self._json(payload)

        if method == "POST":
            if state.fail_save:
                return self._error(503, "模拟即时操作失败", "resource_unavailable")
            state.next_id += 1
            payload = {"id": state.next_id, **self._body()}
            items.append(payload)
            return self._json(payload, 201)

        if method in {"PUT", "PATCH"}:
            if state.fail_save:
                return self._error(503, "模拟即时操作失败", "resource_unavailable")
            body = self._body()
            target = next((item for item in items if str(item.get("id")) == tail), None)
            if target is None:
                return self._json({"id": state.next_id, **body})
            target.update(body)
            return self._json(target)

        if method == "DELETE":
            if state.fail_save:
                return self._error(503, "模拟即时操作失败", "resource_unavailable")
            state.resources[str(group_id).lstrip("-")][resource] = [
                item for item in items if str(item.get("id")) != tail
            ]
            return self._json({})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--host", default="127.0.0.1", help="ignored unless it is a loopback address")
    parser.add_argument("--group-admin", action="store_true", help="serve the group-admin session")
    parser.add_argument("--fail-save", action="store_true", help="make every write answer 503")
    parser.add_argument("--no-groups", action="store_true", help="make the group list fail")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("refusing to bind a non-loopback address; the harness is loopback-only", file=sys.stderr)
        return 2
    if not FIXTURE.is_file():
        print(f"missing fixture {FIXTURE}; run make_fixture.py first", file=sys.stderr)
        return 2

    Handler.state = State(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        group_admin=args.group_admin,
        fail_save=args.fail_save,
        no_groups=args.no_groups,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "群管理员（无全局权限）" if args.group_admin else "最高管理员"
    print(f"settings UI harness on http://{args.host}:{args.port}/settings  [session: {mode}]")
    if args.fail_save:
        print("  writes are failing on purpose (--fail-save)")
    if args.no_groups:
        print("  the group list is failing on purpose (--no-groups)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
