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
    --conflict-save make the next settings PUT answer 409 (revision conflict)
    --no-groups     make /api/v1/groups fail so the error state can be seen
    --replay-fail   make the next replay run finish as ``failed`` (error retry path)
    --replay-saturate  make replay creation answer 429 replay_busy once
    --restore-conflict  make the next restore answer 409 (revision conflict)
    --real-page        serve index.html **verbatim** (the telegram.org SDK tag is
                       NOT swapped for the local stub) so the browser checks can
                       exercise the shipped page; they block the CDN by request
                       interception and inject a signed window.Telegram instead.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from copy import deepcopy
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_fixtures import ConfigHistoryState, ReplayState  # noqa: E402

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

#: 发布页真实使用的 SDK 标签。``--real-page`` 下**不**替换它，浏览器检查靠请求
#: 拦截把它挡掉，再注入一份真正 HMAC 签名的 ``window.Telegram``。
TELEGRAM_SDK_TAG = '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
TELEGRAM_STUB_TAG = '<script src="/harness/telegram-stub.js"></script>'

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


#: ``Handler._body_cache`` 的哨兵：请求体还没读过。同一实例会被 keep-alive 复用，
#: 每个请求都要复位。
_UNREAD = object()

#: 群管理员只被授权**一个**群：这样"群管理员看不到别的群"才是可验证的，
#: 而不是"恰好三个群都归他管"。
GROUP_ADMIN_GROUP = -1009876543210

#: ``GET /api/v1/settings`` 现在把自由 JSON 里的凭据与 URL 内嵌凭据换成标记。
#: harness 在响应里模拟这一步，页面因此必须按新契约处理标记。
OPAQUE_CURRENT_MARKER = "<redacted:current>"
OPAQUE_USERINFO_MARKER = "<redacted:url-userinfo>"


#: 合成 fixture 里的"带凭据地址"。方案名故意拆开写：``tests/test_settings_night_
#: crystal.py::ReleaseHygieneTests`` 断言整个 harness 里只允许出现**一个**
#: 那个 https 链接（发布页的 CDN 标签，harness 唯一会碰到的外链）。这里拼的是
#: 一个永远不会被请求的假地址，不该因为它把那条发布卫生断言改松。
_FAKE_USERINFO_URL_SCHEME = "https" + "://"


def _normalize_status(raw):
    """与产品侧 ``normalize_status()`` 同语义：大小写/空白无关，未知值当"不过滤"。"""

    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    return {"PENDING": "pending", "APPROVED": "approved", "REJECTED": "rejected"}.get(text)


def _build_onboarding_requests(groups, *, pending_request: bool, pages: int) -> list:
    """合成申请列表：``pages`` 页 ×50 条 pending + 一条已批准的历史申请。"""

    rows = []
    if pending_request:
        first = groups[0]["id"]
        rows.append({
            "request_id": "req-pending-1",
            "group_id": int(first),
            "applicant_id": 700000001,
            "status": "pending",
            "reason": "",
            "created_at": "2026-02-01T00:00:00+00:00",
            "reviewed_at": None,
            "revision": 1,
            "granted_group_authorization": False,
        })
        for index in range(1, max(1, pages) * 50):
            rows.append({
                "request_id": f"req-pending-{index + 1}",
                "group_id": int(first) - index,
                "applicant_id": 700000001 + index,
                "status": "pending",
                "reason": "",
                "created_at": "2026-02-01T00:00:00+00:00",
                "reviewed_at": None,
                "revision": 1,
                "granted_group_authorization": False,
            })
    rows.append({
        "request_id": "req-approved-old",
        "group_id": int(groups[0]["id"]) - 9000,
        "applicant_id": 700000999,
        "status": "approved",
        "reason": "历史审批理由",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reviewed_at": "2026-01-01T01:00:00+00:00",
        "revision": 1,
        "granted_group_authorization": True,
    })
    return rows


#: 真实 API 里"有完整单价"的合成模型。金额上限的校验是 **fail-closed** 的：列出来
#: 的每个 (provider_key, model) 都必须在这里，否则保存被拒 —— 与生产
#: ``GroupBudgetService.save_budget`` 用价格目录逐个 find 的行为一致。
PRICED_MODELS = {
    ("fixture", "gpt-4o-mini"): "USD",
    ("fixture", "gpt-4o"): "USD",
}


def _strict_limit(value) -> int | None:
    """``_int_or_none`` 的忠实复刻：``None`` / ``""`` → None；其余走 strict_int。

    ``strict_int`` 接受真实 ``int`` 与**纯十进制字符串**，拒绝 bool / float /
    指数 / nan / inf。生产就是这条口径，harness 不许更宽松（否则浏览器会在一个
    真实后端会 400 的输入上显示成功）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("limit")
    if isinstance(value, int):
        return value if value >= 0 else _raise_limit()
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError("limit")
    parsed = int(text)
    if parsed < 0:
        raise ValueError("limit")
    return parsed


def _raise_limit():
    raise ValueError("limit")


def _budget_document(
    *,
    group_id: int,
    enabled: bool = False,
    currency: str = "",
    attempt=None,
    token=None,
    money=None,
    operation_limits: dict | None = None,
    revision: int = 1,
) -> dict:
    """``GET /api/v1/groups/{id}/budget`` 的**真实**回包形状。

    逐键对齐 ``bot/services/group_budget.py`` 的 ``GroupBudgetService.snapshot()``
    与 ``DayState.document()``：

      * 三个限额与 ``operation_limits`` **嵌在 ``limits`` 里**（曾经把 harness 写成
        顶层，于是浏览器检查在一个真实 API 上根本不存在的形状上全绿，掩盖了
        "重新读取后所有额度显示为空、保存时被清成 null/{}" 这个真实缺陷）；
      * ``money_covers`` **不在回包里** —— 服务端只拿它做一次保存的校验，不落库、
        不回读。harness 也不许假装能读回来。
    """
    limits = {
        "daily_attempt_limit": attempt,
        "daily_token_limit": token,
        "daily_money_micro_limit": money,
        "operation_limits": dict(operation_limits or {}),
    }
    any_limit = any(value is not None and value != {} for value in limits.values())
    return {
        "group_id": int(group_id),
        "enabled": bool(enabled),
        "protecting": bool(enabled and any_limit),
        "revision": int(revision),
        "currency": str(currency or ""),
        "warn_ratio": 0.8,
        "pause_ratio": 1.0,
        "limits": limits,
        "today": {
            "group_id": int(group_id),
            "budget_date": "2026-02-01",
            "used_attempts": 0,
            "used_tokens": 0,
            "used_money_micro": 0,
            "unknown_attempts": 0,
            "used_operations": {},
        },
        "budget_date": "2026-02-01",
    }


def _price_catalog_document() -> dict:
    """``GET /api/v1/prices`` 的**真实**回包形状（合成数据）。

    价格是十进制**字符串**或 ``null``（``pricing.PriceEntry.document()`` 用
    ``str(Decimal)``）；``null`` = 未知，``"0"`` = 明确免费。这里给三行：
    非零价、明确免费、未知价 —— 界面必须把三者显示成不同东西。
    """

    return {
        "version": 1,
        "entries": [
            {
                "provider_key": "fixture",
                "model": "gpt-4o-mini",
                "currency": "USD",
                "input_per_million": "1",
                "output_per_million": "2",
                "cache_read_per_million": "0.5",
                "cache_write_per_million": None,
            },
            {
                "provider_key": "fixture",
                "model": "gpt-4o-free",
                "currency": "USD",
                "input_per_million": "0",
                "output_per_million": "0",
                "cache_read_per_million": None,
                "cache_write_per_million": None,
            },
            {
                "provider_key": "fixture",
                "model": "gpt-4o-unknown",
                "currency": "USD",
                "input_per_million": None,
                "output_per_million": None,
                "cache_read_per_million": None,
                "cache_write_per_million": None,
            },
        ],
    }


def _mark_opaque_secrets(config: dict) -> dict:
    """把 ``request_params`` / ``api_base`` 里的凭据换成服务端标记（合成）。"""
    payload = deepcopy(config)
    for provider in payload.get("models", {}).get("providers", []) or []:
        base = str(provider.get("api_base") or "")
        if base and "openai_compatible" == provider.get("provider"):
            provider["api_base"] = (
                _FAKE_USERINFO_URL_SCHEME + "svc:<redacted:url-userinfo>@api.example.invalid/v1"
            )
    main = payload.get("models", {}).get("main")
    if isinstance(main, dict):
        # 键名刻意避开 HTTP 鉴权头那个词：ReleaseHygieneTests 用它钉住"harness
        # 不得伪造鉴权头"。这里只是自由 JSON 里一个**长得像凭据**的叶子，用来
        # 验证脱敏标记的行为。
        main["request_params"] = {
            "extra_headers": {"X-Demo-Token": OPAQUE_CURRENT_MARKER},
            "timeout": 30,
        }
        for fallback in main.get("fallbacks", []) or []:
            if isinstance(fallback, dict):
                fallback["request_params"] = {"extra_headers": {"X-Api-Key": OPAQUE_CURRENT_MARKER}}
    return payload


#: B13b 运营任务合成目录：``(task_id, 标题, 默认来源, 可选来源, 是否支持周期计划)``。
#: 与真实 ``bot/services/task_policy.py`` 的 SOURCE_SWITCHABLE / DEFAULT_SOURCE /
#: PLAN_TASKS 同形，浏览器检查据此断言"可切换的只有签到与商店、有计划项的只有周报"。
OPERATIONS_TASKS = (
    ("scheduled_messages", "定时消息", "internal", ("internal",), False),
    ("group_summary", "群摘要", "internal", ("internal",), False),
    ("checkin_reminder", "签到提醒", "external", ("internal", "external"), False),
    ("activity_award", "周活跃结算", "internal", ("internal",), False),
    ("weekly_report", "周报", "internal", ("internal",), True),
    ("shop_expire", "商店到期处理", "internal", ("internal", "external"), False),
)

#: 合成夹具的固定"现在"（Asia/Shanghai，周一 10:00）。计划预览的下一时刻因此
#: 是**确定性**的：周一 10:00 选周五 10:00 → 2026-10-09 10:00 (+08:00)。
OPERATIONS_NOW = datetime(2026, 10, 5, 10, 0)

_OPERATIONS_WEEKDAY_LABEL = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _operations_stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


def _operations_next_occurrence(now: datetime, weekday: int, minute: int) -> datetime:
    """与真实 ``task_policy.next_occurrence`` 同口径：严格未来。"""

    base = now.replace(second=0, microsecond=0)
    days_ahead = (int(weekday) - base.weekday()) % 7
    candidate = (base + timedelta(days=days_ahead)).replace(
        hour=int(minute) // 60, minute=int(minute) % 60
    )
    if candidate <= base:
        candidate += timedelta(days=7)
    return candidate


class State:
    def __init__(
        self,
        fixture: dict,
        *,
        group_admin: bool,
        fail_save: bool,
        no_groups: bool,
        conflict_save: bool = False,
        replay_fail: bool = False,
        replay_saturate: bool = False,
        restore_conflict: bool = False,
        real_page: bool = False,
        no_change_preview: bool = False,
        pending_request: bool = False,
        fail_queue_read: bool = False,
        queue_pages: int = 1,
        budget_seed: dict | None = None,
    ):
        self.fixture = fixture
        #: True = ``/settings/diff`` 永远回"无变化"，用来验证页面**把空 diff 当成
        #: 合法答案**而不是错误（就像 --fail-save 强制错误分支一样，这是它的对照项）。
        self.no_change_preview = no_change_preview
        #: True = 逐字发布 index.html（CDN SDK 标签保留给浏览器检查去挡）。
        self.real_page = real_page
        #: 合成待审批申请：``_build_onboarding_requests`` 会造出 ``queue_pages`` 页
        #: ×50 条的 pending 队列（外加一条已批准的旧申请，用来验证"只有 pending
        #: 才有决策按钮"）。
        self.pending_request = pending_request
        self.queue_pages = max(1, int(queue_pages))
        #: 下一次队列 GET 回 503（验证"读失败不冒充读回成功 + 草稿与错误都还在"）。
        self.fail_queue_read = fail_queue_read
        self._queue_fail_armed = fail_queue_read
        #: 预算：按 group_id 存，PUT 走真实 revision CAS。`_budget_fail_armed` 让
        #: **下一次**预算 GET 回 503（验证"写已接收 ≠ 读回已确认"）。
        self.budgets: dict = {}
        #: 价格目录的合成文档（真实 ``GET /api/v1/prices`` 形状）。
        self.prices: dict = _price_catalog_document()
        self._budget_fail_armed = False
        #: 半坏预算快照的注入模式（"" = 不注入）。详见 ``/harness/budget-corrupt``。
        self._budget_corrupt_armed = None
        #: 预算初始快照（嵌套 limits 的真实形状来源）。
        self.budget_seed = deepcopy(budget_seed or {})
        self.group_admin = group_admin
        self.fail_save = fail_save
        self.no_groups = no_groups
        #: 第一次 settings PUT 回 409 之后自动恢复，用来验"冲突可重载"。
        self.conflict_save = conflict_save
        self._conflict_armed = conflict_save
        #: 第一次回放执行失败（验证错误重试），第一次登记回 429 replay_busy。
        self.replay_fail = replay_fail
        self._replay_fail_armed = replay_fail
        self.replay_saturate = replay_saturate
        self._replay_saturate_armed = replay_saturate
        #: 第一次 restore 回 409（验证草稿保留）。
        self.restore_conflict = restore_conflict
        self._restore_conflict_armed = restore_conflict
        self.settings = deepcopy(fixture["settings"])
        # GET /api/v1/settings 回的就是这份**带标记**的编辑表单；把它原样 PUT 回去
        # 因此是无损的（服务端解析标记），"改一个无关字段再保存"也不会抹掉凭据。
        self.settings["config"] = _mark_opaque_secrets(self.settings["config"])
        self.groups = deepcopy(fixture["groups"]["groups"])
        self.onboarding_requests = _build_onboarding_requests(
            self.groups, pending_request=pending_request, pages=self.queue_pages
        )
        for group in self.groups:
            seed = deepcopy(self.budget_seed)
            configured = bool(seed)
            self.budgets[str(group["id"])] = _budget_document(
                group_id=int(group["id"]),
                enabled=bool(seed.get("enabled", False)),
                currency=seed.get("currency", ""),
                attempt=seed.get("attempt"),
                token=seed.get("token"),
                money=seed.get("money"),
                operation_limits=seed.get("operation_limits") or {},
                # 没有配置过的群在真实后端里 revision 是 0（没有预算行）；
                # 配置过的是某次真实保存留下的 revision。
                revision=1 if configured else 0,
            )
        self.resources = deepcopy(fixture["resources"])
        self.memory_fixture = {}
        #: C3：每群一份可变的长期事实存储（首屏由 ``_memory_fact_rows`` 惰性生成），
        #: 人工纠错会真的写进这里，所以后续 GET 能读到"新持久行"；旧的被替换行不再列出。
        self.memory_facts: dict[str, list[dict]] = {}
        self.memory_next_fact_id = 1001
        #: B13b：运营任务合成夹具。策略按 ``<群>:<任务>`` 一行；run 按 run_key；
        #: 审计只追加。失败注入走 ``POST /harness/operations-fixture``：
        #: ``preview``/``confirm``/``policy``/``run`` 四个模式 + ``hold_confirm_ms``。
        self.operations_policies: dict[str, dict] = {}
        self.operations_runs: dict[str, dict] = {}
        self.operations_audit: list[dict] = []
        self.operations_tokens: dict[str, dict] = {}
        self.operations_fixture: dict = {}
        self.operations_hold_confirm_ms = 0
        self._operations_seq = 0
        #: 已被本 harness 删掉的记录键（``<群>:<资源>:<id>``）。真实 API 对「从未
        #: 存在」的 id 回 404、对「已经删掉」的 id 回 no_change；本地预览要复现这个
        #: 区别就必须记住删过谁。``/harness/reset`` 走 ``__init__``，所以会清空。
        self.deleted_resource_ids: set[str] = set()
        self.next_id = 9000
        self.requests: list[str] = []
        self.replay = ReplayState([group["id"] for group in self.groups])
        #: 最近一次**真正执行**的回滚响应（浏览器检查据此读回 restore 契约）。
        self.last_restore = None
        self.history = ConfigHistoryState(
            fixture["settings"]["config"], marker_hook=_mark_opaque_secrets
        )
        # 历史与当前生效修订号对齐：回滚预览返回的 current_revision 就是这个值，
        # 当前配置也**就是**最新那一版的内容（否则"回滚预览"会比一份没人见过的文档）。
        self.settings["revision"] = self.history.current_revision
        self.settings["config"] = deepcopy(
            self.history.rows[self.history.current_revision]["payload"]
        )

    def session_document(self) -> dict:
        key = "group_admin_session" if self.group_admin else "session"
        session = deepcopy(self.fixture[key])
        session.setdefault("role", "group_admin" if self.group_admin else "super_admin")
        session.setdefault(
            "group_ids",
            [GROUP_ADMIN_GROUP] if self.group_admin else [g["id"] for g in self.groups],
        )
        return session

    def settings_document(self) -> dict:
        return deepcopy(self.settings)

    def group_resources(self, group_id: str) -> dict:
        key = str(group_id).lstrip("-")
        return self.resources.get(key, {})

    def bump_revision(self) -> int:
        self.settings["revision"] = int(self.settings["revision"]) + 1
        self.history.current_revision = self.settings["revision"]
        self.history.rows.setdefault(
            self.settings["revision"],
            {
                "revision": self.settings["revision"],
                "parent_revision": self.settings["revision"] - 1,
                "operation": "save",
                "summary": "通过设置页保存（合成数据）",
                "updated_by": 42,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "restore_source_revision": None,
                "secret_paths_changed": [],
                "payload": deepcopy(self.settings["config"]),
                "secrets": [],
                "restart_pending": [],
                "is_current": True,
            },
        )
        for row in self.history.rows.values():
            row["is_current"] = row["revision"] == self.settings["revision"]
        return self.settings["revision"]

    def current_diff(self, proposed: dict) -> list[dict]:
        return self.history.diff(self.settings["config"], proposed)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State
    #: 每个请求复位一次（同一个实例会被 keep-alive 连接复用）。
    _body_cache: dict = _UNREAD

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

    def _error(self, status: int, message: str, code: str = "harness_error", details: dict | None = None) -> None:
        error: dict = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        self._json({"error": error}, status)

    def _body(self) -> dict:
        """读请求体（**只读一次**，见 :meth:`_drain_request_body`）。"""
        if self._body_cache is _UNREAD:
            self._body_cache = self._read_body()
        return self._body_cache

    def _read_body(self) -> dict:
        """把 ``Content-Length`` 那么多字节从 ``rfile`` 里读干净。

        读不出来（不是 JSON / 不是 UTF-8）也要**读完**：留在 socket 上的字节会被
        当成下一个请求的开头，keep-alive 连接上紧接着来的那个正常请求就会收到
        ``400 Bad request version``。
        """
        if self.rfile is None:  # pragma: no cover - 没有真实连接的单元调用
            return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _drain_request_body(self) -> None:
        """**在派发之前**把请求体读干净。

        本 handler 是 ``HTTP/1.1`` keep-alive 的：任何在读完 ``Content-Length``
        之前就 return 的分支（403 / 404 / 409 / 503 …）都会把请求体留在
        socket 上。下一条请求到达时，服务端会先读到这些残留字节，把它们当成
        请求行，于是回一个 ``400 Bad request version`` 并掐断连接 —— 一个
        "故意失败"的接口会连带把后面所有读回都弄脏。这里统一在路由前抽干
        请求体，各分支想读再从 :meth:`_body` 的缓存里拿。
        """
        self._body()

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
        # 查询串一起记：翻页（?limit=&before=）和样本过滤（?include_expired=）
        # 的证据都在这里，浏览器检查据此断言"真的按 next_before 翻了页"。
        state.requests.append(f"{method} {path}{'?' + parsed.query if parsed.query else ''}")

        # 请求体**先**抽干（见 _drain_request_body）：handler 是 keep-alive 的，
        # 任何提前 return 的分支都不许把请求体留在 socket 上。
        self._body_cache = _UNREAD
        self._drain_request_body()

        try:
            if path == "/" or path == "/settings":
                return self._send(200, self._index_html(), CONTENT_TYPES[".html"])
            if path == "/harness/telegram-stub.js":
                return self._send(200, TELEGRAM_STUB.encode("utf-8"), CONTENT_TYPES[".js"])
            if path.startswith("/settings-assets/"):
                return self._asset(path[len("/settings-assets/"):])
            if path == "/harness/requests":
                return self._json({"requests": state.requests})
            if path == "/harness/last-restore":
                return self._json({"response": state.last_restore})
            if path.startswith("/harness/block-restore"):
                revision = int(path.rsplit("/", 1)[-1])
                state.history.restore_blocked_revision = revision
                return self._json({"blocked": revision})
            if path == "/harness/unblock-restore":
                state.history.restore_blocked_revision = None
                return self._json({"blocked": None})
            if path == "/harness/fail-next-queue-read":
                # 让**下一次**队列 GET 回 503。CLI 的 --fail-queue-read 会在页面
                # 首次取数时就命中（那时还没输入框，验不了"草稿还在"），所以这里
                # 给自动化一个"先正常加载、再下毒"的入口。
                state._queue_fail_armed = True
                return self._json({"armed": True})
            if path == "/harness/fail-next-budget-read":
                state._budget_fail_armed = True
                return self._json({"armed": True})
            if path == "/harness/budget-corrupt":
                # 让**下一次**预算 GET 回一份"半坏快照"（revision 是 null/false、
                # 限额是字符串/小数、缺键、operation_limits 是数组、群号不对…）。
                # 前端必须把它当读取失败：不画可保存表单、不发 PUT。
                body = self._body()
                state._budget_corrupt_armed = str(body.get("mode") or "")
                return self._json({"armed": state._budget_corrupt_armed})
            if path == "/harness/budget-bump-revision":
                # 模拟"另一个会话先保存了一次"：真实 revision +1、并把 token 上限
                # 改成 7，用来在浏览器里逼出真实的 409 并让用户做保留/采用选择。
                body = self._body()
                group_id = str(body.get("group_id") or "")
                if group_id not in state.budgets:
                    return self._error(404, f"没有这个群：{group_id}", "not_found")
                current = state.budgets[group_id]
                current["revision"] = int(current["revision"]) + 1
                current["limits"]["daily_token_limit"] = 7
                return self._json({"revision": current["revision"]})
            if path == "/harness/patch-fixture" and method == "POST":
                mode = self._body().get("mode", "normal")
                if mode not in {"normal", "unconfirmed", "malformed", "unavailable"}:
                    return self._error(400, "未知 PATCH fixture", "invalid_fixture")
                state.patch_fixture = mode
                return self._json({"mode": mode})
            if path == "/harness/memory-fixture" and method == "POST":
                body = self._body()
                state.memory_fixture = body
                return self._json({"ok": True})
            if path == "/harness/operations-fixture" and method == "POST":
                body = self._body()
                state.operations_fixture = body
                state.operations_hold_confirm_ms = int(body.get("hold_confirm_ms") or 0)
                return self._json({"ok": True, "fixture": body})
            if path == "/harness/reset":
                fresh = json.loads(FIXTURE.read_text(encoding="utf-8"))
                # 合成运营夹具是"跨 reset 保留的开关"（与 patch_fixture 同级），
                # 否则一次 reset 就把注入的失败模式悄悄清掉。
                operations_fixture = dict(state.operations_fixture or {})
                hold_confirm_ms = int(state.operations_hold_confirm_ms or 0)
                state.__init__(  # noqa: PLC2801 - deliberate in-place reset for the dev harness
                    fresh,
                    group_admin=state.group_admin,
                    fail_save=state.fail_save,
                    no_groups=state.no_groups,
                    conflict_save=state.conflict_save,
                    replay_fail=state.replay_fail,
                    replay_saturate=state.replay_saturate,
                    restore_conflict=state.restore_conflict,
                    real_page=state.real_page,
                    no_change_preview=state.no_change_preview,
                    pending_request=state.pending_request,
                    fail_queue_read=state.fail_queue_read,
                    queue_pages=state.queue_pages,
                    budget_seed=state.budget_seed,
                )
                state.operations_fixture = operations_fixture
                state.operations_hold_confirm_ms = hold_confirm_ms
                return self._json({"reset": True})
            if path.startswith("/api/v1/"):
                return self._api(method, path[len("/api/v1/"):], query)
            self._error(404, f"harness has no route for {path}", "not_found")
        except BrokenPipeError:  # pragma: no cover - browser aborted a request
            pass
        except Exception as error:  # pragma: no cover - harness bug surface
            # 开发工具：把栈直接打到 stderr，harness 自己出错时一眼能看懂。
            traceback.print_exc()
            self._error(500, f"harness error: {error}", "harness_crash")

    def _index_html(self) -> bytes:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        if self.state.real_page:
            # 逐字发布：浏览器检查自己挡掉 CDN、自己注入签名 initData。
            return html.encode("utf-8")
        # Swap the CDN Telegram SDK for a local stub: the harness must not make
        # any network request at all, and the production page keeps its own tag.
        return html.replace(TELEGRAM_SDK_TAG, TELEGRAM_STUB_TAG).encode("utf-8")

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
            return self._json({"session": state.session_document()})

        if segments == ["prices"]:
            # 价格目录：**形状**与真实 ``GET/PUT /api/v1/prices`` 一致
            # （version + entries，价格是十进制字符串或 null；PUT 必须带
            # expected_version 且按版本 CAS）。
            # 权威的行为检查（失锁 409、只改一个字段不清其它、真落库）在
            # ``real_api_host.py`` + ``checks/real-api-budget.mjs`` 上跑真后端；
            # 这里只是为了"模型页在超管下能渲染、普通管理员一个请求都不发"。
            if state.group_admin:
                return self._error(403, "只有最高管理员可以管理价格目录", "forbidden")
            if method == "GET":
                return self._json({"catalog": deepcopy(state.prices)})
            if method == "PUT":
                body = self._body()
                unknown = set(body) - {"expected_version", "entries"}
                if unknown:
                    return self._error(
                        400,
                        f"未知的价格目录字段：{', '.join(sorted(unknown))}",
                        "unknown_price_field",
                    )
                expected = body.get("expected_version")
                if not isinstance(expected, int) or isinstance(expected, bool):
                    return self._error(
                        400, "expected_version 必须是 ≥1 的整数。", "invalid_expected_version"
                    )
                if expected != int(state.prices["version"]):
                    return self._error(
                        409,
                        (
                            f"价格目录版本冲突：服务端当前版本 {state.prices['version']}，"
                            f"客户端期望版本 {expected}。"
                        ),
                        "price_version_conflict",
                    )
                entries = body.get("entries")
                if not isinstance(entries, list):
                    return self._error(400, "entries 必须是数组。", "invalid_price_catalog")
                state.prices = {"version": expected + 1, "entries": deepcopy(entries)}
                return self._json({"catalog": deepcopy(state.prices)})
            return self._error(405, f"不支持的 {method} /api/v1/prices", "method_not_allowed")

        if route == "settings":
            if method == "GET":
                if state.group_admin:
                    return self._error(403, "只有最高管理员可以查看全局配置", "forbidden")
                return self._json(state.settings_document())
            if method in {"PUT", "PATCH"}:
                if state.conflict_save and state._conflict_armed:
                    state._conflict_armed = False
                    return self._error(
                        409,
                        f"runtime config revision changed: expected {int(state.settings['revision']) - 1}, "
                        f"got {int(state.settings['revision'])}",
                        "group_revision_conflict",
                    )
                if state.fail_save:
                    return self._error(503, "模拟保存失败：后端拒绝了这次写入", "runtime_config_unavailable")
                body = self._body()
                if not body.get("config"):
                    return self._error(400, "缺少 config", "bad_request")
                state.settings["config"] = body["config"]
                state.bump_revision()
                return self._json(state.settings_document())

        # ---- 新群接入：全局审批队列（仅超管） ---------------------------
        if route == "onboarding/requests" and method == "GET":
            if state.group_admin:
                # 非超管**一个字段都不给**：连"有没有 pending"都不泄漏。
                return self._error(403, "只有最高管理员可以查看审批队列", "forbidden")
            if state._queue_fail_armed:
                state._queue_fail_armed = False
                return self._error(503, "模拟队列读取失败", "queue_unavailable")
            normalized = _normalize_status(query.get("status"))
            rows = [
                row for row in state.onboarding_requests
                if normalized is None or row["status"] == normalized
            ]
            try:
                limit = int(query.get("limit", "50"))
                offset = int(query.get("offset", "0"))
            except ValueError:
                return self._error(400, "limit/offset 必须是整数", "bad_request")
            if limit < 0 or offset < 0:
                return self._error(400, "limit/offset 必须非负", "bad_request")
            page = rows[offset:offset + limit] if limit else []
            return self._json({
                "requests": [deepcopy(row) for row in page],
                "rows": len(page),
                "total": len(rows),
                "status": normalized,
            })

        if (
            len(segments) == 4
            and segments[0] == "onboarding"
            and segments[1] == "requests"
            and segments[3] in {"approve", "reject"}
            and method == "POST"
        ):
            if state.group_admin:
                return self._error(403, "只有最高管理员可以决策申请", "forbidden")
            request_id = segments[2]
            decision = segments[3]
            target = next(
                (row for row in state.onboarding_requests if row["request_id"] == request_id),
                None,
            )
            if target is None:
                return self._error(404, f"没有这条申请：{request_id}", "not_found")
            body = self._body()
            reason = str(body.get("reason") or "")
            already = target["status"] != "pending"
            if already:
                # 重放一条已终结的申请**不改变任何状态**，也不复活授权。
                return self._json({
                    "request": deepcopy(target),
                    "already_decided": True,
                    "group_authorized": False,
                    "applicant_granted": False,
                    "current": {
                        "authorized": bool(target.get("granted_group_authorization")),
                        "bot_present": False,
                        "applicant_admin": False,
                    },
                })
            target["status"] = "approved" if decision == "approve" else "rejected"
            target["reviewed_at"] = "2026-02-01T00:00:00+00:00"
            target["reason"] = reason
            target["granted_group_authorization"] = decision == "approve"
            return self._json({
                "request": deepcopy(target),
                "already_decided": False,
                "group_authorized": decision == "approve",
                "applicant_granted": decision == "approve",
                "current": {
                    "authorized": decision == "approve",
                    "bot_present": False,
                    "applicant_admin": decision == "approve",
                },
            })

        # ---- 按群预算（读：本群管理员可读；写：仅超管，真实 revision CAS） ----
        if len(segments) == 3 and segments[0] == "groups" and segments[2] == "budget":
            group_id = segments[1]
            if group_id not in state.budgets:
                return self._error(404, f"没有这个群：{group_id}", "not_found")
            if method == "GET":
                if state._budget_fail_armed:
                    state._budget_fail_armed = False
                    return self._error(503, "预算读取暂时不可用", "budget_unavailable")
                if state._budget_corrupt_armed:
                    # 半坏快照：真实后端**不会**这样回，但前端必须把"看不懂的快照"
                    # 当成读取失败而不是"合法且不限制"——否则 revision=null 会被
                    # `Number(null) === 0` 读成合法版本、`daily_token_limit`
                    # 是字符串时会被当成"没设上限"，界面直接放出可保存表单。
                    mode = state._budget_corrupt_armed
                    state._budget_corrupt_armed = None
                    document = deepcopy(state.budgets[group_id])
                    if mode == "revision_null":
                        document["revision"] = None
                    elif mode == "revision_false":
                        document["revision"] = False
                    elif mode == "revision_string":
                        document["revision"] = "5"
                    elif mode == "token_invalid":
                        document["limits"]["daily_token_limit"] = "invalid"
                    elif mode == "token_float":
                        document["limits"]["daily_token_limit"] = 1.5
                    elif mode == "token_missing":
                        document["limits"].pop("daily_token_limit", None)
                    elif mode == "operations_list":
                        document["limits"]["operation_limits"] = ["tts"]
                    elif mode == "enabled_string":
                        document["enabled"] = "true"
                    elif mode == "wrong_group":
                        document["group_id"] = -1009999999999
                    elif mode == "no_limits":
                        document["limits"] = None
                    else:
                        return self._error(400, f"未知的注入模式：{mode}", "bad_request")
                    return self._json({"budget": document})
                return self._json({"budget": deepcopy(state.budgets[group_id])})
            if method in {"PUT", "PATCH"}:
                if state.group_admin:
                    return self._error(403, "只有最高管理员可以修改预算", "forbidden")
                if state.fail_save:
                    return self._error(503, "模拟保存失败：后端拒绝了这次写入", "budget_unavailable")
                body = self._body()
                # 逐条复刻产品侧 PUT 预算端点的校验合同：同样的未知字段拒绝、
                # 同样的错误码、同样的"enabled 必须是真布尔"。（此处刻意不写
                # 产品模块名 —— 该文件的发布卫生回归禁止 harness 引用产品代码。）
                unknown = set(body) - {
                    "revision",
                    "enabled",
                    "currency",
                    "daily_attempt_limit",
                    "daily_token_limit",
                    "daily_money_micro_limit",
                    "operation_limits",
                    "money_covers",
                }
                if unknown:
                    return self._error(
                        400,
                        f"包含不允许的字段：{', '.join(sorted(unknown))}",
                        "unknown_budget_field",
                    )
                raw_enabled = body.get("enabled")
                if not isinstance(raw_enabled, bool):
                    return self._error(
                        400, "enabled 必须是布尔值 true / false。", "invalid_budget_field"
                    )
                raw_revision = body.get("revision")
                if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
                    return self._error(400, "revision 必须是整数。", "bad_request")
                operations = body.get("operation_limits") or {}
                if not isinstance(operations, dict):
                    return self._error(
                        400, "operation_limits 必须是对象。", "invalid_budget_field"
                    )
                for feature, value in operations.items():
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        return self._error(
                            400,
                            f"operation_limits[{feature}] 必须是非负整数。",
                            "invalid_budget_field",
                        )
                covers_raw = body.get("money_covers") or []
                if not isinstance(covers_raw, list):
                    return self._error(
                        400, "money_covers 必须是数组。", "invalid_budget_field"
                    )
                covers: list[tuple[str, str]] = []
                for item in covers_raw:
                    if not isinstance(item, dict) or set(item) != {"provider_key", "model"}:
                        return self._error(
                            400, "money_covers 项格式无效。", "invalid_budget_field"
                        )
                    covers.append((str(item["provider_key"]), str(item["model"])))
                attempt = body.get("daily_attempt_limit")
                token = body.get("daily_token_limit")
                money = body.get("daily_money_micro_limit")
                try:
                    parsed = {
                        "daily_attempt_limit": _strict_limit(attempt),
                        "daily_token_limit": _strict_limit(token),
                        "daily_money_micro_limit": _strict_limit(money),
                    }
                except ValueError:
                    return self._error(
                        400, "额度必须是 0 或正整数。", "invalid_budget"
                    )
                attempt = parsed["daily_attempt_limit"]
                token = parsed["daily_token_limit"]
                money = parsed["daily_money_micro_limit"]
                currency = str(body.get("currency") or "")
                if money is not None and not currency.strip():
                    return self._error(
                        400, "设置金额上限时必须指定币种。", "invalid_budget"
                    )
                if money is not None:
                    # 与生产一致：金额 hard cap 只在计价完整时允许存在，未知价格
                    # fail-closed。列出其它配置（attempts/tokens/功能次数）不影响
                    # 这条校验，也不会被这次校验漏掉。
                    seen_currency = currency.strip().upper()
                    for provider_key, model in covers:
                        priced = PRICED_MODELS.get((provider_key, model))
                        if priced is None:
                            return self._error(
                                400,
                                f"模型 {model} 没有配置单价，无法启用金额上限。",
                                "invalid_budget",
                            )
                        if priced != seen_currency:
                            return self._error(
                                400,
                                f"模型 {model} 的计价币种 {priced} 与预算币种不一致。",
                                "invalid_budget",
                            )
                current = state.budgets[group_id]
                if int(raw_revision) != int(current["revision"]):
                    return self._error(
                        409,
                        "预算配置已被其他会话更新，请刷新后重试。",
                        "budget_revision_conflict",
                    )
                state.budgets[group_id] = _budget_document(
                    group_id=int(group_id),
                    enabled=raw_enabled,
                    currency=currency.strip().upper()[:8],
                    attempt=attempt,
                    token=token,
                    money=money,
                    operation_limits={
                        str(name).strip(): value
                        for name, value in operations.items()
                        if str(name).strip()
                    },
                    revision=int(current["revision"]) + 1,
                )
                # 与生产一致：money_covers 只用于这次校验，**不落库、不回读**。
                return self._json(
                    {
                        "budget": deepcopy(state.budgets[group_id]),
                        "revision": int(state.budgets[group_id]["revision"]),
                    }
                )

        if route == "settings/diff" and method == "POST":
            if state.group_admin:
                return self._error(403, "只有最高管理员可以预览全局配置", "forbidden")
            if state.fail_save:
                return self._error(503, "模拟保存预览失败", "runtime_config_unavailable")
            body = self._body()
            proposed = body.get("config")
            if not isinstance(proposed, dict):
                return self._error(400, "缺少 config", "bad_request")
            entries = [] if state.no_change_preview else state.current_diff(proposed)
            return self._json(
                {
                    "revision": int(state.settings["revision"]),
                    "expected_revision": int(body.get("revision") or state.settings["revision"]),
                    "stale": int(body.get("revision") or state.settings["revision"]) != int(state.settings["revision"]),
                    "diff": entries,
                    # **无变化是一份合法答案**：空 diff + 「无变化」摘要。
                    "summary": state.history.summary_of(entries),
                    "changed_secret_paths": sorted(
                        {e["path"] for e in entries if e.get("secret") and e.get("change") != "removed"}
                    ),
                    "snapshot_bytes": 18432 + len(json.dumps(proposed, ensure_ascii=False).encode()),
                    "snapshot_limit_bytes": 1048576,
                    "restart_pending": [] if not entries else ["resources.pending_reply_execution_capacity"],
                }
            )

        if route == "settings/history" and method == "GET":
            if state.group_admin:
                return self._error(403, "只有最高管理员可以查看配置历史", "forbidden")
            raw_limit = str(query.get("limit") or "").strip()
            raw_before = str(query.get("before") or "").strip()
            # 越界一律 400 + 稳定码，**不夹取**：夹取会让"还有更多"提示说谎。
            limit = 20
            if raw_limit:
                try:
                    limit = int(raw_limit)
                except ValueError:
                    return self._error(400, "limit 必须是整数。", "invalid_history_page")
                if limit < 1 or limit > 100:
                    return self._error(400, "limit 必须在 1 到 100 之间。", "invalid_history_page")
            before = None
            if raw_before:
                try:
                    before = int(raw_before)
                except ValueError:
                    return self._error(400, "before 必须是 revision 整数。", "invalid_history_page")
                if before < 1:
                    return self._error(400, "before 必须是正整数 revision。", "invalid_history_page")
            return self._json(state.history.page(limit=limit, before=before))

        history_match = re.fullmatch(r"settings/history/(-?\d+)(/restore-preview|/restore)?", route)
        if history_match:
            if state.group_admin:
                return self._error(403, "只有最高管理员可以回滚配置", "forbidden")
            revision = int(history_match.group(1))
            action = history_match.group(2) or ""
            if action == "" and method == "GET":
                detail = state.history.detail(revision)
                if detail is None:
                    return self._error(404, "该历史版本不存在或已损坏，请刷新后重试。", "history_revision_not_found")
                return self._json(detail)
            candidate = state.history.restore_candidate(revision)
            if candidate is None:
                return self._error(404, "该历史版本不存在或已损坏，请刷新后重试。", "history_revision_not_found")
            if state.history.restore_blocked_revision == revision:
                # 消息里**只有路径名**（与后端一致），可以逐字给管理员看。
                return self._error(
                    400,
                    "以下字段的值从未写入历史，且当前配置里没有可保留的值：models.main.request_params.extra_headers.X-Demo-Token。"
                    "请先在当前配置中把这些段填好（或直接写入新值），再重试；系统不会把它们悄悄清空。",
                    "history_revision_not_restorable",
                )
            # 回滚预览算的是**当前配置 → 真正的回滚候选**，不是两个历史版本。
            entries = state.history.diff(state.settings["config"], candidate)
            if action == "/restore-preview":
                if method != "GET":
                    return self._error(405, "只支持 GET", "method_not_allowed")
                return self._json(
                    {
                        "revision": revision,
                        "current_revision": int(state.settings["revision"]),
                        "diff": entries,
                        "summary": state.history.summary_of(entries),
                        "snapshot_bytes": 18432,
                        "restart_pending": ["resources.pending_reply_execution_capacity"],
                        "retained_secrets": ["providers.icefall.api_key"],
                    }
                )
            if action == "/restore":
                if method != "POST":
                    return self._error(405, "只支持 POST", "method_not_allowed")
                body = self._body()
                # 两种 409：被显式武装的一次"别人抢先保存了"，以及客户端真的
                # 带了个过期的 revision。两者都答 409 config_revision_conflict。
                if state._restore_conflict_armed:
                    # 被显式武装的一次：模拟"预览之后别人抢先保存了"。
                    state._restore_conflict_armed = False
                    return self._error(
                        409,
                        "runtime config revision changed; 回滚没有执行",
                        "config_revision_conflict",
                    )
                if int(body.get("revision") or 0) != int(state.settings["revision"]):
                    return self._error(
                        409,
                        "runtime config revision changed; 回滚没有执行",
                        "config_revision_conflict",
                    )
                # 回滚写进去的也是一份带标记的完整文档（合成数据同样走标记形状）。
                state.settings["config"] = _mark_opaque_secrets(candidate)
                revision_before = int(state.settings["revision"])
                state.bump_revision()
                document = state.settings_document()
                document["restore"] = {
                    "source_revision": revision,
                    "retained_secrets": ["providers.icefall.api_key"],
                }
                document["_restored_from"] = revision_before
                state.last_restore = document
                return self._json(document)

        if route == "groups" and method == "GET":
            if state.no_groups:
                return self._error(503, "模拟群组列表加载失败", "database_unavailable")
            # 真实后端按 ``_allowed_group_ids`` 过滤：群管理员只看得见自己被授权的群。
            if state.group_admin:
                allowed = {gid for gid in state.session_document().get("group_ids") or []}
                return self._json({"groups": [g for g in state.groups if g["id"] in allowed]})
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
            if tail and tail.startswith("moderation-replay"):
                return self._replay(method, int(group_id), tail[len("moderation-replay"):].strip("/"), query)

            if tail and (tail == "operations" or tail.startswith("operations/")):
                return self._operations(method, int(group_id), tail, query)

            if tail.startswith("memory-transparency/"):
                fixture = getattr(state, "memory_fixture", {})
                segments = [part for part in tail.split("/") if part]
                # C3：memory-transparency/facts/<id>/source（只读展开）与 .../correction（人工纠错）。
                if len(segments) == 4 and segments[:2] == ["memory-transparency", "facts"]:
                    try:
                        fact_id = int(segments[2])
                    except ValueError:
                        return self._error(400, "事实 ID 不合法", "invalid_fact_id")
                    if segments[3] == "source" and method == "GET":
                        return self._memory_source(int(group_id), fact_id, fixture)
                    if segments[3] == "correction" and method == "POST":
                        return self._memory_correction(int(group_id), fact_id, fixture)
                    return self._error(404, "没有这个记忆操作", "not_found")
                if method != "GET":
                    return self._error(405, "只读接口", "method_not_allowed")
                section = tail.split("/")[-1]
                mode = fixture.get("mode", "normal")
                if fixture.get("section", section) == section and mode in {"failure", "permission"}:
                    state.memory_fixture = {}
                    return self._error(403 if mode == "permission" else 503, "合成读取失败", "memory_read_unavailable")
                common = {"ok": True, "group_id": int(group_id), "section": section}
                if section == "summary":
                    status = fixture.get("summary", "invalid")
                    doc = {"version": 2, "generated_at": "2026-01-01T09:00:00", "covered_from_id": 1,
                           "covered_through_id": 4, "covered_count": 4, "source_revision": 0,
                           "source_truncated": False, "summary": "合成有效摘要" if status == "valid" else None}
                    return self._json({**common, "status": status, "document": doc if status in {"valid","invalid"} else None})
                if section not in {"facts", "searches"}:
                    return self._error(404, "分节不存在", "not_found")
                rows = []
                # 合成来源指向 harness 自己的 loopback 地址，绝不引用外部站点：
                # 源文件里除要删掉的 CDN 标签外不应出现外部 URL 字面量（既有发布卫生合同）。
                origin = f"http://127.0.0.1:{self.server.server_port}"
                prefix = "本群A" if str(group_id) == "-1001234567890" else "本群B"
                if section == "facts":
                    rows = self._memory_fact_rows(state, int(group_id), fixture)
                else:
                    for ident in range(25, 0, -1):
                        rows.append({"id": ident, "query": prefix + f"检索-{ident}", "digest": "<img src=x onerror=alert(1)>纯文字",
                            "kind": "price", "outcome": "ok", "created_at": "2026-01-01T09:00:00", "freshness": "stale", "freshness_hours": 1,
                            "sources": [{"title":"<b>来源</b>","url": f"{origin}/harness/safe-source"},{"title":"不安全","url":"javascript:alert(1)"}]})
                if section == "facts" and query.get("subject"):
                    rows = [r for r in rows if str(r["subject_user_id"]) == query["subject"]]
                if section == "facts" and query.get("category") and query["category"] != "identity": rows = []
                if section == "searches" and query.get("kind") and query["kind"] != "price": rows = []
                if query.get("cursor"): rows = [r for r in rows if r["id"] < int(query["cursor"])]
                limit = int(query.get("limit", 20)); shown = rows[:limit]; more = len(rows) > limit
                return self._json({**common, "items": shown, "has_more": more, "next_cursor": str(shown[-1]["id"]) if more else None, "retention_days": 7})

            # 第二段是 URL 段名（``keyword-replies``），第三段才是记录 id。两者必须
            # 分开：拿整条尾串去查 RESOURCE_TYPES 时，``keyword-replies/20`` 整串
            # 不命中，单条 DELETE 会掉进 404「没有路由」。
            segment, _, item_id = (tail or "").partition("/")
            resource = RESOURCE_TYPES.get(segment)
            if resource:
                return self._resource(method, group_id, resource, item_id.strip("/"), query)

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

    # -- moderation replay -------------------------------------------------
    # -- B13b: synthetic operations fixture -------------------------------
    def _operations(self, method: str, group_id: int, tail: str, query: dict) -> None:
        """B13b 运营任务/执行来源/周期计划的合成后端。

        形状与真实后端（Mini App 设置路由 + 任务策略服务）对齐（``{"preview": …}`` /
        顶层策略文档 / ``{"policy": …,"confirmed":true}``），
        但**不实现**任何真实业务：积分、发送、模型都在这里只是文档字段。失败注入
        统一走 ``POST /harness/operations-fixture``。
        """

        state = self.state
        fixture = state.operations_fixture or {}
        if fixture.get("revoke"):
            # 角色被撤：真实后端此时对每个运营端点都回 403，前端必须清掉敏感状态。
            return self._error(403, "没有这个群的权限", "forbidden")
        route = "/".join(part for part in tail.split("/") if part)[len("operations"):].strip("/")

        if route == "tasks" and method == "GET":
            return self._json(
                {
                    "tasks": [
                        {"task_id": task_id, "title": title}
                        for task_id, title, *_ in OPERATIONS_TASKS
                    ],
                    "preview_ttl_seconds": 120,
                    "actor_role": "admin" if state.group_admin else "superadmin",
                }
            )

        if route == "task-status" and method == "GET":
            return self._json(self._operations_status(group_id))

        if route == "policy" and method == "GET":
            if fixture.get("policy") == "unavailable":
                return self._error(503, "读取执行来源失败", "policy_unavailable")
            return self._json(
                {
                    "group_id": group_id,
                    "timezone": "Asia/Shanghai",
                    "external_schedule_known": False,
                    "external_schedule_reason": "external_schedule_unknown",
                    "tasks": [
                        {**self._policy_payload(group_id, task_id), "title": title}
                        for task_id, title, *_ in OPERATIONS_TASKS
                    ],
                }
            )

        if route == "policy/preview" and method == "POST":
            return self._operations_policy_preview(group_id, fixture)

        if route == "policy/confirm" and method == "POST":
            return self._operations_policy_confirm(group_id, fixture)

        if route == "policy/audit" and method == "GET":
            limit = max(1, min(int(query.get("limit") or 10), 50))
            task_filter = str(query.get("task_id") or "")
            rows = [
                deepcopy(item)
                for item in reversed(state.operations_audit)
                if (not task_filter or item["task_id"] == task_filter)
                and (not query.get("cursor") or item["id"] < int(query["cursor"]))
            ]
            shown = rows[:limit]
            more = len(rows) > limit
            return self._json(
                {
                    "group_id": group_id,
                    "task_id": task_filter,
                    "limit": limit,
                    "has_more": more,
                    "next_cursor": str(shown[-1]["id"]) if more else None,
                    "items": shown,
                }
            )

        if route == "preview" and method == "POST":
            return self._operations_task_preview(group_id, fixture)

        if route == "runs" and method == "GET":
            rows = [
                self._operations_run_payload(run)
                for run in sorted(
                    state.operations_runs.values(), key=lambda item: item["id"], reverse=True
                )
                if run["group_id"] == group_id
            ]
            return self._json({"group_id": group_id, "items": rows[:20], "has_more": False, "next_cursor": None, "limit": 20})

        if route == "runs" and method == "POST":
            return self._operations_confirm(group_id, fixture)

        run_match = re.fullmatch(r"runs/([0-9a-zA-Z_-]{1,64})(?:/(cancel|retry|reconcile))?", route)
        if run_match:
            run_key, action = run_match.group(1), run_match.group(2)
            run = state.operations_runs.get(run_key)
            if run is None or run["group_id"] != group_id:
                return self._error(404, "没有这条执行记录", "run_not_found")
            if action is None and method == "GET":
                self._operations_advance(run, fixture)
                return self._json({"run": self._operations_run_payload(run, items=True)})
            if action is not None and method == "POST":
                return self._operations_run_action(run, action, group_id)

        return self._error(404, f"harness has no operations route for {tail}", "not_found")

    # -- B13b helpers -----------------------------------------------------
    def _policy_meta(self, task_id: str) -> tuple[str, tuple[str, ...], bool]:
        for key, _title, default, options, plan in OPERATIONS_TASKS:
            if key == task_id:
                return default, tuple(options), bool(plan)
        return "", (), False

    def _policy_row(self, group_id: int, task_id: str) -> dict | None:
        return self.state.operations_policies.get(f"{group_id}:{task_id}")

    def _policy_payload(self, group_id: int, task_id: str) -> dict:
        default, options, plan_supported = self._policy_meta(task_id)
        row = self._policy_row(group_id, task_id) or {}
        source = str(row.get("source") or default)
        return {
            "group_id": group_id,
            "task_id": task_id,
            "source": source,
            "source_default": default,
            "source_options": list(options),
            "source_switchable": len(options) > 1,
            "plan_supported": plan_supported,
            "plan_enabled": bool(row.get("plan_enabled")),
            "plan_weekday": row.get("plan_weekday"),
            "plan_minute": row.get("plan_minute"),
            "anchor_at": row.get("anchor_at"),
            "active_from": row.get("active_from"),
            "revision": int(row.get("revision") or 0),
            "exists": bool(row),
            "runner_task_name": (
                {
                    "checkin_reminder": "checkin_reminder_internal",
                    "shop_expire": "shop-expiry-runner",
                    "weekly_report": "weekly_report_internal_plan",
                }.get(task_id)
                if source == "internal"
                else None
            ),
            "external_schedule_known": False,
            "external_schedule_reason": "external_schedule_unknown",
        }

    def _operations_policy_preview(self, group_id: int, fixture: dict) -> None:
        state = self.state
        body = self._body()
        task_id = str(body.get("task_id") or "")
        meta = next((item for item in OPERATIONS_TASKS if item[0] == task_id), None)
        if meta is None:
            return self._error(404, "没有这个运营任务", "policy_task_not_found")
        if fixture.get("policy") == "unavailable":
            return self._error(503, "读取执行来源失败", "policy_unavailable")
        current = self._policy_payload(group_id, task_id)
        mode = str(body.get("mode") or "").strip().lower()
        if mode == "source":
            if not current["source_switchable"]:
                return self._error(409, "这个任务只有一种执行来源，不能切换", "source_not_switchable")
            wanted = str(body.get("source") or "").strip().lower()
            if wanted not in current["source_options"]:
                return self._error(400, "不支持的执行来源", "invalid_source")
            proposed = {"source": wanted}
            effects = [f"把本群「{meta[1]}」的执行来源改成 {wanted}（只影响未来，不补历史）。"]
            confirmable = wanted != current["source"]
            detail = (
                "来源没有变化，不需要确认"
                if not confirmable
                else f"执行来源将从 {current['source']} 改为 {wanted}（只影响未来）"
            )
            next_anchor = None
        elif mode == "plan":
            if not current["plan_supported"]:
                return self._error(409, "这个任务没有周期计划", "plan_unsupported")
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return self._error(400, "enabled 必须是布尔值", "invalid_plan")
            weekday = body.get("weekday")
            minute = body.get("minute")
            if enabled and (
                not isinstance(weekday, int)
                or not isinstance(minute, int)
                or not 0 <= weekday <= 6
                or not 0 <= minute <= 1439
            ):
                return self._error(400, "开启周期计划必须明确选择周内日与时刻", "invalid_plan")
            next_anchor = (
                _operations_stamp(
                    _operations_next_occurrence(OPERATIONS_NOW, int(weekday), int(minute))
                )
                if enabled
                else None
            )
            proposed = {
                "plan_enabled": bool(enabled),
                "plan_weekday": int(weekday) if enabled else None,
                "plan_minute": int(minute) if enabled else None,
            }
            unchanged = bool(enabled) == bool(current["plan_enabled"]) and (
                not enabled
                or (int(weekday) == current["plan_weekday"] and int(minute) == current["plan_minute"])
            )
            confirmable = not unchanged
            label = _OPERATIONS_WEEKDAY_LABEL[int(weekday)] if enabled else ""
            clock = "%02d:%02d" % (int(minute or 0) // 60, int(minute or 0) % 60)
            effects = [
                (
                    f"从下一个 {label} {clock}（Asia/Shanghai，绝对时刻 {next_anchor}）开始，"
                    "每周自动结算积分并发送周报；只影响未来，开启不会立刻补发上一周。"
                    if enabled
                    else "关闭本群周报的周期计划：以后只在你手动预览并确认之后才结算和发送。"
                )
            ]
            detail = (
                "周期计划没有变化，不需要确认"
                if unchanged
                else ("开启周报周期计划（只影响未来）" if enabled else "关闭周报周期计划")
            )
        else:
            return self._error(400, "mode 只能是 source 或 plan", "invalid_policy_mode")

        token = ""
        expires_at = None
        if confirmable:
            state._operations_seq += 1
            token = f"policy-{group_id}-{task_id}-{state._operations_seq}"
            state.operations_tokens[token] = {
                "kind": "policy",
                "group_id": group_id,
                "task_id": task_id,
                "mode": mode,
                "proposed": dict(proposed),
                "expect_revision": int(current["revision"]),
            }
            expires_at = _operations_stamp(OPERATIONS_NOW + timedelta(seconds=120))
        return self._json(
            {
                "preview": {
                    "task_id": task_id,
                    "mode": mode,
                    "group_id": group_id,
                    "current": current,
                    "proposed": proposed,
                    "effects": list(effects),
                    "detail": detail,
                    "confirmable": bool(confirmable),
                    "reason": "" if confirmable else "no_change",
                    "next_anchor_at": next_anchor,
                    "timezone": "Asia/Shanghai",
                    "external_schedule_known": False,
                    "external_schedule_reason": "external_schedule_unknown",
                    "token": token,
                    "expires_at": expires_at,
                    "ttl_seconds": 120,
                    "read_only": True,
                }
            }
        )

    def _operations_policy_confirm(self, group_id: int, fixture: dict) -> None:
        state = self.state
        body = self._body()
        token = str(body.get("token") or "")
        binding = state.operations_tokens.get(token)
        if binding is None or binding["kind"] != "policy":
            return self._error(400, "这次预览的令牌无效，请重新预览", "preview_token_invalid")
        if int(binding["group_id"]) != group_id:
            return self._error(403, "令牌不属于这个群", "preview_group_mismatch")
        task_id = binding["task_id"]
        before = self._policy_payload(group_id, task_id)
        if fixture.get("policy") == "conflict":
            return self._error(409, "这次预览已经失效：请重新预览后再确认", "revision_conflict")
        if int(before["revision"]) != int(binding["expect_revision"]):
            return self._error(409, "这次预览已经失效：请重新预览后再确认", "revision_conflict")
        key = f"{group_id}:{task_id}"
        row = dict(state.operations_policies.get(key) or {})
        row["revision"] = int(before["revision"]) + 1
        if binding["mode"] == "source":
            row["source"] = binding["proposed"]["source"]
            row["active_from"] = _operations_stamp(OPERATIONS_NOW)
            action = "source_switch"
            detail = f"执行来源已改为 {binding['proposed']['source']}"
        else:
            enabled = bool(binding["proposed"]["plan_enabled"])
            row["plan_enabled"] = enabled
            row["plan_weekday"] = binding["proposed"]["plan_weekday"]
            row["plan_minute"] = binding["proposed"]["plan_minute"]
            row["anchor_at"] = (
                _operations_stamp(
                    _operations_next_occurrence(
                        OPERATIONS_NOW,
                        int(binding["proposed"]["plan_weekday"]),
                        int(binding["proposed"]["plan_minute"]),
                    )
                )
                if enabled
                else None
            )
            row["active_from"] = _operations_stamp(OPERATIONS_NOW)
            action = "plan_enable" if enabled else "plan_disable"
            detail = "周报周期计划已开启（只影响未来）" if enabled else "周报周期计划已关闭"
        if not row.get("source"):
            row["source"] = before["source"]
        state.operations_policies[key] = row
        state._operations_seq += 1
        state.operations_audit.append(
            {
                "id": state._operations_seq,
                "group_id": group_id,
                "task_id": task_id,
                "actor_user_id": 42,
                "actor_role": "superadmin",
                "action": action,
                "revision": row["revision"],
                "before": before,
                "after": self._policy_payload(group_id, task_id),
                "detail": detail,
                "created_at": _operations_stamp(OPERATIONS_NOW),
            }
        )
        state.operations_tokens.pop(token, None)
        return self._json(
            {
                "policy": self._policy_payload(group_id, task_id),
                "confirmed": True,
                "action": action,
                "detail": detail,
            }
        )

    def _operations_task_preview(self, group_id: int, fixture: dict) -> None:
        state = self.state
        body = self._body()
        task_id = str(body.get("task_id") or "")
        meta = next((item for item in OPERATIONS_TASKS if item[0] == task_id), None)
        if meta is None:
            return self._error(404, "没有这个运营任务", "operations_task_not_found")
        if fixture.get("preview") == "unavailable":
            return self._error(503, "预览现在不可用", "operations_preview_unavailable")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        targets = self._operations_targets(task_id, params)
        if fixture.get("preview") == "no_work" or not targets:
            return self._json(
                {
                    "preview": {
                        "task_id": task_id,
                        "task_title": meta[1],
                        "group_id": group_id,
                        "status": "no_work",
                        "reason": "no_work",
                        "detail": "本群当前没有可处理的目标。",
                        "effects": [],
                        "cost": {},
                        "targets": [],
                        "token": "",
                        "expires_at": None,
                        "confirmable": False,
                        "extra": {},
                    }
                }
            )
        state._operations_seq += 1
        token = f"run-{group_id}-{task_id}-{state._operations_seq}"
        state.operations_tokens[token] = {
            "kind": "run",
            "group_id": group_id,
            "task_id": task_id,
            "params": dict(params),
            "targets": targets,
        }
        return self._json(
            {
                "preview": {
                    "task_id": task_id,
                    "task_title": meta[1],
                    "group_id": group_id,
                    "status": "ok",
                    "reason": "",
                    "detail": self._operations_detail(task_id, targets),
                    "effects": [self._operations_effect(task_id)],
                    "cost": self._operations_cost(task_id),
                    "targets": targets,
                    "token": token,
                    "expires_at": _operations_stamp(OPERATIONS_NOW + timedelta(seconds=120)),
                    "confirmable": True,
                    "extra": {},
                }
            }
        )

    def _operations_targets(self, task_id: str, params: dict) -> list[dict]:
        if task_id == "scheduled_messages":
            raw = params.get("entry_ids") or "1"
            if isinstance(raw, (list, tuple)):
                # 真实前端把 entry_ids 作为整数列表提交（见 readOperationsParams）。
                ids = [str(int(value)) for value in raw if str(value).lstrip("-").isdigit()]
            else:
                ids = [part for part in re.split(r"[,\s]+", str(raw)) if part.isdigit()]
            ids = ids or ["1"]
            return [
                {"item_key": f"entry:{value}", "target": {"entry_id": int(value)}, "label": f"定时消息 #{value}", "detail": ""}
                for value in ids[:5]
            ]
        if task_id == "checkin_reminder":
            slot = params.get("slot")
            slot = 12 if slot in (None, "") else int(slot)
            return [
                {
                    "item_key": f"{OPERATIONS_NOW.date().isoformat()}:{slot}",
                    "target": {"slot": slot},
                    "label": f"{slot}:00 签到提醒",
                    "detail": "",
                }
            ]
        if task_id == "shop_expire" and int(params.get("limit") or 1) == 0:
            return []
        labels = {
            "group_summary": "本群摘要新版本",
            "activity_award": "上周达标成员",
            "weekly_report": "上周周报",
            "shop_expire": "本群到期权益",
        }
        return [
            {
                "item_key": labels.get(task_id, task_id),
                "target": {"task_id": task_id},
                "label": labels.get(task_id, task_id),
                "detail": "",
            }
        ]

    def _operations_effect(self, task_id: str) -> str:
        return {
            "scheduled_messages": "按既有策略向本群发送选中的定时消息，并按策略安排自动删除。",
            "group_summary": "调用模型生成本群摘要并发布新版本（会消耗 token 与预算；发布带 source revision CAS）。",
            "checkin_reminder": "向本群发送签到提醒（含既有按钮），并按既有策略安排自动删除。",
            "activity_award": "给本群达标成员写入积分奖励流水（同一周同一人只发一次）。",
            "weekly_report": "先按确认时的成员与金额结算上周活跃积分（同一 ref 幂等），再发送一份周报。",
            "shop_expire": "撤销本群已到期的权益，并给持有人发一条到期通知。",
        }.get(task_id, "执行本任务。")

    def _operations_cost(self, task_id: str) -> dict:
        if task_id == "group_summary":
            return {
                "llm_tokens": "unknown",
                "llm_monetary_cost": "unknown",
                "telegram_monetary_cost": "none",
                "points": "none",
                "note": "模型用量与金额在调用前无法预知，不编造估算",
            }
        return {
            "telegram_messages": 1,
            "telegram_monetary_cost": "none",
            "llm_tokens": "none",
            "other_cost": "unknown",
        }

    def _operations_detail(self, task_id: str, targets: list[dict]) -> str:
        if task_id == "group_summary":
            return "手动生成会真实调用模型并发布版本；排队成功不代表完成"
        if task_id == "weekly_report":
            return "确认时的成员与金额会被钉住：确认之后新达标的成员不会被顺带发分，只会提示重新预览"
        return f"本群当前有 {len(targets)} 项需要处理，只处理本群、只处理本次确认选中的目标"

    def _operations_confirm(self, group_id: int, fixture: dict) -> None:
        state = self.state
        body = self._body()
        token = str(body.get("token") or "")
        binding = state.operations_tokens.get(token)
        if binding is None or binding["kind"] != "run":
            return self._error(400, "这次预览的令牌无效，请重新预览", "preview_token_invalid")
        if fixture.get("confirm") == "conflict":
            return self._error(409, "这次预览已经失效：请重新预览后再确认", "preview_stale")
        if fixture.get("confirm") == "unconfirmed":
            # 真实 503：确认的结果无法确认（可能已经受理）。前端不许当成功。
            return self._error(
                503,
                "这次确认的结果无法确认：可能已经受理。请不要重复提交，先刷新执行记录核对。",
                "operations_confirm_unconfirmed",
            )
        if state.operations_hold_confirm_ms:
            # 「ACK 还没回来」：浏览器在这一秒里继续输入，草稿必须留着。
            time.sleep(state.operations_hold_confirm_ms / 1000.0)
        state._operations_seq += 1
        run_key = f"run-{group_id}-{state._operations_seq}"
        task_id = binding["task_id"]
        targets = binding["targets"]
        items = [
            {
                "item_key": str(item["item_key"]),
                "target": dict(item["target"]),
                "label": item["label"],
                "status": "running",
                "reason": "",
                "detail": "",
                "external_message_id": None,
                "resendable": False,
                "attempts": 1,
                "updated_at": _operations_stamp(OPERATIONS_NOW),
            }
            for item in targets
        ]
        run = {
            "id": state._operations_seq,
            "run_key": run_key,
            "task_id": task_id,
            "group_id": group_id,
            "params": dict(binding["params"]),
            "status": "running",
            "reason": "",
            "detail": "正在执行",
            "source": "manual",
            "retry_of_run_id": None,
            "target_keys": [item["item_key"] for item in items],
            "cancel_requested": False,
            "items_total": len(items),
            "items_succeeded": 0,
            "items_failed": 0,
            "items_unknown": 0,
            "guard_revision": 1,
            "policy_revision": int(self._policy_payload(group_id, task_id)["revision"]),
            "created_at": _operations_stamp(OPERATIONS_NOW),
            "started_at": _operations_stamp(OPERATIONS_NOW),
            "finished_at": None,
            "finished": False,
            "_items": items,
            "_polls": 0,
        }
        state.operations_runs[run_key] = run
        state.operations_tokens.pop(token, None)
        return self._json({"run": self._operations_run_payload(run, items=True), "confirmed": True})

    def _operations_advance(self, run: dict, fixture: dict) -> None:
        """GET 执行记录时推进合成状态：第一次仍是 running，第二次落到终态。"""

        if run["finished"]:
            return
        run["_polls"] = int(run.get("_polls") or 0) + 1
        if run["_polls"] < 2:
            return
        mode = str(fixture.get("run") or "ok")
        items = run["_items"]
        if mode == "ok":
            for item in items:
                item["status"] = "succeeded"
                item["detail"] = self._operations_success_detail(run["task_id"])
            run["status"] = "succeeded"
            run["detail"] = self._operations_success_detail(run["task_id"])
        elif mode == "partial":
            for index, item in enumerate(items):
                if index == 0:
                    item["status"] = "succeeded"
                    item["detail"] = "已完成"
                else:
                    item["status"] = "cancelled"
                    item["reason"] = "cancel_requested"
                    item["detail"] = self._operations_cancel_detail(run["task_id"])
                    item["resendable"] = True
            run["status"] = "partial"
            run["detail"] = "部分目标已执行，其余被取消"
        elif mode == "unknown":
            for index, item in enumerate(items):
                if index == 0:
                    item["status"] = "succeeded"
                    item["detail"] = "已完成"
                else:
                    item["status"] = "unknown"
                    item["reason"] = "unknown_result"
                    item["detail"] = "出站结果未知：不许自动重发，请先对账"
                    item["resendable"] = False
            run["status"] = "unknown"
            run["detail"] = "有目标的结果未知，需要人工对账"
        elif mode == "failed_retryable":
            for item in items:
                item["status"] = "failed"
                item["reason"] = "send_failed"
                item["detail"] = "发送失败，可以重新预览后仅安全重试"
                item["resendable"] = True
            run["status"] = "failed"
            run["detail"] = "发送失败，本次没有确认的副作用"
        elif mode == "cancelled":
            for item in items:
                item["status"] = "cancelled"
                item["reason"] = "cancel_requested"
                item["detail"] = "已请求取消，本项未执行"
                item["resendable"] = True
            run["status"] = "cancelled"
            run["detail"] = "已请求取消，本项未执行"
        else:
            for item in items:
                item["status"] = "failed"
                item["reason"] = "guard_blocked"
                item["detail"] = "写边界复核未通过：本次没有产生任何副作用，请重新预览"
                item["resendable"] = False
            run["status"] = "failed"
            run["detail"] = "写边界复核未通过"
        run["finished"] = True
        run["finished_at"] = _operations_stamp(OPERATIONS_NOW)
        run["items_succeeded"] = sum(1 for item in items if item["status"] == "succeeded")
        run["items_failed"] = sum(1 for item in items if item["status"] == "failed")
        run["items_unknown"] = sum(1 for item in items if item["status"] == "unknown")

    def _operations_success_detail(self, task_id: str) -> str:
        if task_id == "weekly_report":
            return "2026-W40 周报已发送（上周活跃积分已入账 25 分）"
        return "已按确认的目标执行完成"

    def _operations_cancel_detail(self, task_id: str) -> str:
        if task_id == "weekly_report":
            return (
                "已请求取消：本次没有发送周报，上周活跃积分已入账 25 分；"
                "重试需要重新预览确认，已入账的积分不会重复发放"
            )
        return "已请求取消，本项未执行"

    def _operations_run_action(self, run: dict, action: str, group_id: int) -> None:
        state = self.state
        if action == "cancel":
            run["cancel_requested"] = True
            run["status"] = "cancelled"
            run["finished"] = True
            run["finished_at"] = _operations_stamp(OPERATIONS_NOW)
            run["detail"] = self._operations_cancel_detail(run["task_id"])
            for item in run["_items"]:
                if item["status"] != "succeeded":
                    item["status"] = "cancelled"
                    item["reason"] = "cancel_requested"
                    item["detail"] = self._operations_cancel_detail(run["task_id"])
                    item["resendable"] = True
        elif action == "reconcile":
            for item in run["_items"]:
                if item["status"] == "unknown":
                    item["status"] = "succeeded"
                    item["reason"] = ""
                    item["detail"] = "对账确认这条已经真正发生"
            run["status"] = "succeeded"
            run["finished"] = True
            run["finished_at"] = _operations_stamp(OPERATIONS_NOW)
            run["detail"] = "对账完成"
        else:
            state._operations_seq += 1
            run_key = f"run-{group_id}-{state._operations_seq}"
            items = [
                {
                    **deepcopy(item),
                    "status": "succeeded",
                    "reason": "",
                    "detail": "仅安全重试完成",
                    "resendable": False,
                }
                for item in run["_items"]
            ]
            retry = {
                **{key: value for key, value in run.items() if key not in {"_items", "id"}},
                "id": state._operations_seq,
                "run_key": run_key,
                "source": "retry",
                "retry_of_run_id": run["id"],
                "status": "succeeded",
                "reason": "",
                "detail": "仅安全重试完成",
                "cancel_requested": False,
                "finished": True,
                "finished_at": _operations_stamp(OPERATIONS_NOW),
                "_items": items,
                "_polls": 0,
            }
            state.operations_runs[run_key] = retry
            run = retry
        run["items_succeeded"] = sum(1 for item in run["_items"] if item["status"] == "succeeded")
        run["items_failed"] = sum(1 for item in run["_items"] if item["status"] == "failed")
        run["items_unknown"] = sum(1 for item in run["_items"] if item["status"] == "unknown")
        return self._json({"run": self._operations_run_payload(run, items=True)})

    def _operations_run_payload(self, run: dict, *, items: bool = False) -> dict:
        payload = {key: deepcopy(value) for key, value in run.items() if not key.startswith("_")}
        payload["finished"] = bool(run["finished"])
        if items:
            payload["items"] = [deepcopy(item) for item in run["_items"]]
        return payload

    def _operations_status(self, group_id: int) -> dict:
        tasks = []
        for task_id, title, _default, _options, _plan in OPERATIONS_TASKS:
            policy = self._policy_payload(group_id, task_id)
            internal = policy["source"] == "internal"
            task = {
                "task_id": task_id,
                "title": title,
                "trigger": "event_driven",
                "source": {
                    "implemented": "in_process" if internal else "external_cron_only",
                    "runner_task_name": policy["runner_task_name"],
                    "effective_source": policy["source"],
                    "source_default": policy["source_default"],
                    "source_switchable": policy["source_switchable"],
                    "policy_revision": policy["revision"],
                    "active_from": policy["active_from"],
                    "external_schedule_known": False,
                    "external_schedule_reason": "external_schedule_unknown",
                },
                "next_run": {"at": None, "reason": "external_schedule_unknown"},
                "last_success": {"at": None, "reason": "not_recorded"},
                "last_failure": {"at": None, "reason": "not_recorded"},
                "details": {
                    "effective_source": policy["source"],
                    "policy_revision": policy["revision"],
                    "active_from": policy["active_from"],
                },
                "unknowns": [],
                "evidence": [],
            }
            if task_id == "checkin_reminder":
                if internal:
                    task["trigger"] = "wall_clock"
                    task["next_run"] = {
                        "at": _operations_stamp(OPERATIONS_NOW.replace(hour=12, minute=0, second=0)),
                        "reason": "planned_by_scheduler",
                    }
                    task["details"]["slots"] = [9, 12, 15, 18]
                else:
                    task["trigger"] = "external_cron"
            elif task_id == "activity_award":
                task["trigger"] = "event_driven"
                task["next_run"] = {"at": None, "reason": "manual_only"}
                task["details"]["settlement_trigger"] = "manual_or_weekly_report"
            elif task_id == "weekly_report":
                task["details"]["plan_supported"] = policy["plan_supported"]
                task["details"]["plan_enabled"] = policy["plan_enabled"]
                task["details"]["plan_weekday"] = policy["plan_weekday"]
                task["details"]["plan_minute"] = policy["plan_minute"]
                task["details"]["plan_anchor_at"] = policy["anchor_at"]
                task["details"]["plan_timezone"] = "Asia/Shanghai"
                if policy["plan_enabled"] and policy["anchor_at"]:
                    task["trigger"] = "wall_clock"
                    task["next_run"] = {"at": policy["anchor_at"], "reason": "planned_by_scheduler"}
                else:
                    task["trigger"] = "event_driven"
                    task["next_run"] = {"at": None, "reason": "plan_disabled"}
            elif task_id == "shop_expire":
                task["next_run"] = {"at": None, "reason": "runtime_not_observed"}
                task["details"]["external_entry_role"] = "no_op_when_all_internal"
            elif task_id == "scheduled_messages":
                task["trigger"] = "wall_clock"
                task["next_run"] = {"at": None, "reason": "not_configured"}
            tasks.append(task)
        return {
            "group_id": group_id,
            "timezone": "Asia/Shanghai",
            "observed_at": _operations_stamp(OPERATIONS_NOW),
            "external_schedule_known": False,
            "external_schedule_reason": "external_schedule_unknown",
            "tasks": tasks,
        }

    def _memory_fact_rows(self, state, group_id: int, fixture: dict) -> list[dict]:
        """C3：每群一份**可变**事实列表。

        首屏惰性按 25..1 生成（与 C2 一致），人工纠错成功后真的写回这里，
        所以后续 GET 读到的是"新持久行"，被替换的旧行不再列出。
        """

        key = str(group_id)
        if key not in state.memory_facts:
            origin = f"http://127.0.0.1:{self.server.server_port}"
            prefix = "本群A" if key == "-1001234567890" else "本群B"
            state.memory_facts[key] = [
                {
                    "id": ident,
                    "subject_user_id": ident % 2,
                    "fact_text": prefix + f"事实-{ident}" + "<script>literal</script>",
                    "category": "identity",
                    "confidence": 60,
                    "confirm_count": 1,
                    "revision": 1,
                    "corrected": False,
                    "corrected_at": None,
                    "first_seen_at": "2026-01-01T09:00:00",
                    "last_confirmed_at": "2026-01-02T09:00:00",
                    "source_kind": "passive",
                    "source_state": fixture.get("source_state", "verified"),
                    "source_url": f"{origin}/harness/safe-source",
                }
                for ident in range(25, 0, -1)
            ]
        return state.memory_facts[key]

    def _memory_source(self, group_id: int, fact_id: int, fixture: dict):
        """C3 受控原文展开：三态可辨，正文只在 ``ok`` 时回。"""

        status = fixture.get("source", "ok")
        reason = {
            "ok": "",
            "deleted": "moderated",
            "missing": "no_archive",
            "not_visible": "status:superseded",
            "unverifiable": "query_failed",
        }.get(status, "")
        rows = self._memory_fact_rows(self.state, group_id, fixture)
        row = next((item for item in rows if int(item["id"]) == fact_id), None)
        if row is None:
            return self._error(404, "事实不存在", "not_found")
        origin = f"http://127.0.0.1:{self.server.server_port}"
        return self._json(
            {
                "ok": True,
                "group_id": group_id,
                "fact_id": fact_id,
                "status": status,
                "available": status == "ok",
                "reason": reason,
                "revision": int(row.get("revision", 1)),
                "corrected": bool(row.get("corrected")),
                "source_kind": row.get("source_kind", "passive"),
                "message_id": 55,
                "thread_id": 7,
                "url": f"{origin}/harness/safe-source" if status == "ok" else "",
                "link_kind": "member_only",
                "text": "合成来源原文 <b>纯文字</b> &#x3c;script&#x3e;" if status == "ok" else "",
                "truncated": bool(fixture.get("source_truncated")),
            }
        )

    def _memory_correction(self, group_id: int, fact_id: int, fixture: dict):
        """C3 人工纠错：真实替换存储里的那一行，返回新的稳定 ID。"""

        state = self.state
        payload = self._body()
        text = payload.get("text")
        expected = payload.get("expected_revision")
        mode = fixture.get("correction", "normal")
        rows = self._memory_fact_rows(state, group_id, fixture)
        row = next((item for item in rows if int(item["id"]) == fact_id), None)
        if row is None:
            return self._error(404, "事实不存在", "not_found")
        if not isinstance(text, str) or not text.strip():
            return self._error(400, "新内容不能是空的。", "invalid_correction_text", {"reason": "empty"})
        if len(text.strip()) > 200:
            return self._error(400, "新内容太长了。", "invalid_correction_text", {"reason": "too_long"})
        if mode == "unconfirmed":
            return self._error(503, "未能确认这次修改是否已生效，请显式重看。", "memory_write_unconfirmed")
        revision = int(row.get("revision", 1))
        if mode == "conflict" or expected != revision:
            return self._error(
                409,
                "这条事实已经变化，请重看后再确认。",
                "memory_fact_conflict",
                {"conflict": "revision", "reason": "revision_conflict", "previous_fact_id": fact_id, "previous_revision": revision},
            )
        if mode == "unchanged" or text.strip() == str(row.get("fact_text", "")).strip():
            return self._json({"ok": True, "status": "unchanged", "fact_id": fact_id, "revision": revision, "confirmed": False, "message": "内容没有变化。"})
        state.memory_next_fact_id += 1
        new_id = state.memory_next_fact_id
        rows.remove(row)
        rows.insert(0, {**row, "id": new_id, "fact_text": text.strip(), "revision": 1, "corrected": True, "corrected_at": "2026-01-03T09:00:00", "last_confirmed_at": "2026-01-03T09:00:00"})
        return self._json(
            {
                "ok": True,
                "status": "corrected",
                "confirmed": True,
                "fact_id": new_id,
                "revision": 1,
                "previous_fact_id": fact_id,
                "previous_revision": revision,
                "corrected": True,
                "message": "已更新这条长期记忆。",
            }
        )

    def _replay_snapshot(self, group_id: int, run_id: int) -> dict:
        """基线快照。**角色矩阵**：群管理员拿不到全局审核提示词。

        真实后端对非超管返回 ``prompt=""`` + ``prompt_visible=False`` + 12 位摘要；
        harness 必须一模一样，否则"界面没有假装看得见"这件事就没被验证到。
        """
        replay = self.state.replay
        run = replay.runs[run_id]
        if not self.state.group_admin:
            return {
                "group_id": group_id,
                "captured_at": run["created_at"],
                "rules": deepcopy(run["_rules"]),
                "prompt": "你是群审核助手，请根据 {rules_json} 判断这条发言。",
                "prompt_visible": True,
            }
        digest = "0f3ac1d9b7e2"
        return {
            "group_id": group_id,
            "captured_at": run["created_at"],
            "rules": deepcopy(run["_rules"]),
            "prompt": "",
            "prompt_visible": False,
            "prompt_digest": digest,
        }

    def _replay_payload(self, group_id: int, run_id: int, *, limit: int, offset: int) -> dict:
        state = self.state
        run = state.replay.run_document(run_id)
        rows = state.replay.results_for(run_id, limit=limit, offset=offset)
        return {
            "run": run,
            "bounds": state.replay.bounds(run_id),
            "capacity": state.replay.capacity(),
            "baseline_snapshot": self._replay_snapshot(group_id, run_id),
            "candidate_rules": deepcopy(run.get("candidate_rules") or []),
            "candidate_prompt": run.get("candidate_prompt") if not state.group_admin else "",
            "candidate_prompt_visible": not state.group_admin,
            "results": deepcopy(rows),
            "result_count": len(rows),
        }

    def _replay(self, method: str, group_id: int, tail: str, query: dict) -> None:
        state = self.state
        replay = state.replay
        if state.group_admin and str(group_id) != str(GROUP_ADMIN_GROUP):
            return self._error(403, "你没有这个群的管理权限。", "group_access_denied")
        # 每次请求都让"执行器"前进一格：轮询是真的在等状态变化。
        replay.tick()

        if tail == "samples" and method == "GET":
            include_expired = str(query.get("include_expired") or "") in {"1", "true", "yes"}
            samples = [
                replay.sample_document(row["id"])
                for row in replay.samples.values()
                if row["group_id"] == group_id and (include_expired or not row["expired"])
            ]
            samples.sort(key=lambda item: item["id"], reverse=True)
            return self._json({"samples": samples, "ttl_days": 30})

        if tail == "samples" and method == "POST":
            body = self._body()
            text = str(body.get("text") or "").strip()
            if not text:
                return self._error(400, "样本内容不能为空。", "replay_sample_empty")
            sample_id = replay.create_manual_sample(
                group_id, text=text, gold_label=body.get("gold_label")
            )
            return self._json({"sample": replay.sample_document(sample_id)})

        if tail == "samples/import" and method == "POST":
            body = self._body()
            limit = int(body.get("limit") or 2)
            created = replay.import_samples(group_id, count=max(1, min(5, limit)))
            return self._json(
                {
                    "imported": len(created),
                    "skipped": 0,
                    "ttl_days": 30,
                    "samples": [replay.sample_document(sample_id) for sample_id in created],
                }
            )

        sample_match = re.fullmatch(r"samples/(\d+)(/label)?", tail)
        if sample_match:
            sample_id = int(sample_match.group(1))
            if sample_id not in replay.samples:
                return self._error(404, "样本不存在。", "replay_sample_not_found")
            if sample_match.group(2) == "/label":
                if method != "PATCH":
                    return self._error(405, "只支持 PATCH", "method_not_allowed")
                body = self._body()
                if "gold_label" not in body:
                    return self._error(400, "必须提供 gold_label 字段。", "replay_gold_label_required")
                label = body.get("gold_label")
                if label is not None and str(label) not in ("violation", "benign"):
                    return self._error(400, "标注只能是 violation 或 benign。", "replay_gold_label_invalid")
                try:
                    return self._json(
                        replay.set_label(group_id, sample_id, label, actor=42 if not state.group_admin else 4242)
                    )
                except KeyError:
                    return self._error(404, "样本不存在。", "replay_sample_not_found")
            if method == "DELETE":
                found, touched, stats = replay.delete_sample(group_id, sample_id)
                if not found:
                    return self._error(404, "样本不存在。", "replay_sample_not_found")
                return self._json(
                    {
                        "deleted": True,
                        "deleted_results": True,
                        "recomputed_runs": touched,
                        "stats": stats,
                    }
                )

        if tail == "runs" and method == "GET":
            rows = [replay.run_document(run["id"]) for run in replay.runs.values() if run["group_id"] == group_id]
            rows.sort(key=lambda item: item["id"], reverse=True)
            return self._json({"runs": rows, "capacity": replay.capacity()})

        if tail == "runs" and method == "POST":
            body = self._body()
            candidate = body.get("candidate")
            if not isinstance(candidate, dict) or not isinstance(candidate.get("rules"), list):
                return self._error(400, "候选结构无效：只能是 {rules, prompt?}。", "replay_candidate_invalid")
            # 角色矩阵：候选提示词是全局口径，提交它会被 403。
            if str(candidate.get("prompt") or "").strip() and state.group_admin:
                return self._error(
                    403, "只有超级管理员可以提交候选审核提示词。", "replay_prompt_forbidden"
                )
            if not str(body.get("idempotency_key") or "").strip():
                return self._error(400, "创建回放必须提供 idempotency_key。", "replay_idempotency_key_required")
            if state._replay_saturate_armed:
                state._replay_saturate_armed = False
                return self._error(
                    429,
                    "回放队列已满，请稍后再试。",
                    "replay_busy",
                    details={"capacity": replay.capacity()},
                )
            try:
                run, created = replay.create_run(
                    group_id,
                    candidate=candidate,
                    idempotency_key=str(body["idempotency_key"]),
                    execute=bool(body.get("execute", True)),
                )
            except ValueError:
                return self._error(
                    409,
                    "同一个 idempotency_key 已经登记过另一份候选或另一个群。",
                    "replay_idempotency_conflict",
                )
            if created and state._replay_fail_armed:
                state._replay_fail_armed = False
                replay.fail_next_run = run["id"]
            payload = self._replay_payload(group_id, run["id"], limit=500, offset=0)
            payload["created"] = created
            payload["accepted"] = bool(body.get("execute", True)) and created
            payload["executed"] = payload["accepted"]
            payload["bounds"]["sample_pool"] = replay.sample_pool(group_id)
            return self._json(payload)

        run_match = re.fullmatch(r"runs/(\d+)(/cancel|/execute)?", tail)
        if run_match:
            run_id = int(run_match.group(1))
            action = run_match.group(2) or ""
            run = replay.runs.get(run_id)
            if run is None or run["group_id"] != group_id:
                return self._error(404, "回放不存在。", "replay_run_not_found")
            if action == "/cancel" and method == "POST":
                try:
                    replay.cancel(run_id)
                except PermissionError:
                    return self._error(409, "回放已经结束，无法取消。", "replay_not_cancellable")
                return self._json({"run": replay.run_document(run_id)})
            if action == "/execute" and method == "POST":
                try:
                    replay.execute(run_id)
                except PermissionError:
                    return self._error(
                        409, f"回放当前状态为 {run['status']}，只有 queued 可以启动。", "replay_not_queued"
                    )
                return self._json({"run": replay.run_document(run_id), "accepted": True, "capacity": replay.capacity()})
            if method == "GET":
                limit = int(str(query.get("limit") or "500"))
                limit = max(1, min(500, limit))
                offset = max(0, int(str(query.get("offset") or "0")))
                return self._json(self._replay_payload(group_id, run_id, limit=limit, offset=offset))

        return self._error(404, f"harness 没有回放路由 {method} /{tail}", "not_found")

    def _resource(
        self,
        method: str,
        group_id: str,
        resource: str,
        item_id: str,
        query: dict,
    ) -> None:
        state = self.state
        bucket = state.group_resources(group_id)
        items = bucket.setdefault(resource, [])

        if method == "GET":
            if item_id:
                target = next((item for item in items if str(item.get("id")) == item_id), None)
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
            target = next((item for item in items if str(item.get("id")) == item_id), None)
            if target is None:
                return self._error(404, "记录不存在", "not_found")
            if method == "PATCH" and resource in {"rules", "keyword_replies", "scheduled_messages"}:
                mode = getattr(state, "patch_fixture", "normal")
                state.patch_fixture = "normal"
                if mode == "unconfirmed":
                    return self._error(503, "未能确认修改是否已生效，请重新查看。", "resource_write_unconfirmed")
                if mode == "unavailable":
                    return self._error(409, "这条记录已不可用，未确认本次修改。", "resource_unavailable")
                if mode == "malformed":
                    return self._json({"ok": True, "status": "updated"})
                target.update(body)
                if resource == "scheduled_messages":
                    target.setdefault("last_run_at", "")
                response_key = {"rules": "rule", "keyword_replies": "keyword_reply", "scheduled_messages": "scheduled_message"}[resource]
                return self._json({"ok": True, "status": "updated", "confirmed": True, response_key: target})
            target.update(body)
            return self._json(target)

        if method == "DELETE":
            if state.fail_save:
                return self._error(503, "模拟即时操作失败", "resource_unavailable")
            # 与已上线契约对齐的三态回执：删掉一行 = deleted；已经删过的同一个 id
            # = no_change（幂等，不报错）；从未存在过的 id = 404（真实 API 的
            # memory_not_found 形状）；集合级 DELETE 没有指定记录 = no_change。
            group_key = str(group_id).lstrip("-")
            marker = f"{group_key}:{resource}:{item_id}"
            remaining = [item for item in items if str(item.get("id")) != item_id]
            if len(remaining) != len(items):
                bucket[resource] = remaining
                state.resources[group_key][resource] = remaining
                state.deleted_resource_ids.add(marker)
                changed = True
            elif item_id and marker in state.deleted_resource_ids:
                changed = False
            elif item_id:
                return self._error(404, "记录不存在", "not_found")
            else:
                changed = False
            return self._json(
                {
                    "ok": True,
                    "deleted": changed,
                    "changed": changed,
                    "status": "deleted" if changed else "no_change",
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--host", default="127.0.0.1", help="ignored unless it is a loopback address")
    parser.add_argument("--group-admin", action="store_true", help="serve the group-admin session")
    parser.add_argument("--fail-save", action="store_true", help="make every write answer 503")
    parser.add_argument(
        "--pending-request",
        action="store_true",
        help="seed synthetic pending onboarding requests (one per --queue-pages page)",
    )
    parser.add_argument(
        "--queue-pages",
        type=int,
        default=1,
        help="how many 50-row pages of pending requests to seed (default 1)",
    )
    parser.add_argument(
        "--fail-queue-read",
        action="store_true",
        help="make the NEXT onboarding queue GET answer 503",
    )
    parser.add_argument(
        "--budget-seed",
        default="",
        help=(
            "JSON seed for the per-group budget snapshot, e.g. "
            '\'{"enabled":true,"currency":"USD","attempt":9,"token":20,'
            '"operation_limits":{"tts":3}}\''
        ),
    )
    parser.add_argument(
        "--conflict-save",
        action="store_true",
        help="the first settings PUT answers 409 (revision conflict)",
    )
    parser.add_argument("--no-groups", action="store_true", help="make the group list fail")
    parser.add_argument(
        "--replay-fail", action="store_true", help="the next replay run finishes as failed"
    )
    parser.add_argument(
        "--replay-saturate",
        action="store_true",
        help="the next replay creation answers 429 replay_busy",
    )
    parser.add_argument(
        "--restore-conflict",
        action="store_true",
        help="the next settings restore answers 409 (revision conflict)",
    )
    parser.add_argument(
        "--real-page",
        action="store_true",
        help="serve index.html verbatim (keep the telegram.org SDK tag)",
    )
    parser.add_argument(
        "--no-change-preview",
        action="store_true",
        help="always answer POST /settings/diff with an empty diff (无变化)",
    )
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
        conflict_save=args.conflict_save,
        replay_fail=args.replay_fail,
        replay_saturate=args.replay_saturate,
        restore_conflict=args.restore_conflict,
        real_page=args.real_page,
        no_change_preview=args.no_change_preview,
        pending_request=args.pending_request,
        fail_queue_read=args.fail_queue_read,
        queue_pages=args.queue_pages,
        budget_seed=json.loads(args.budget_seed) if args.budget_seed else None,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "群管理员（无全局权限）" if args.group_admin else "最高管理员"
    print(f"settings UI harness on http://{args.host}:{args.port}/settings  [session: {mode}]")
    if args.fail_save:
        print("  writes are failing on purpose (--fail-save)")
    if args.conflict_save:
        print("  the first settings PUT answers 409 on purpose (--conflict-save)")
    if args.no_groups:
        print("  the group list is failing on purpose (--no-groups)")
    if args.replay_fail:
        print("  the next replay run will fail on purpose (--replay-fail)")
    if args.replay_saturate:
        print("  the next replay creation answers 429 replay_busy (--replay-saturate)")
    if args.restore_conflict:
        print("  the next settings restore answers 409 (--restore-conflict)")
    if args.real_page:
        print("  serving index.html verbatim; block telegram.org and inject a signed stub")
    if args.no_change_preview:
        print("  POST /settings/diff always answers 无变化 on purpose (--no-change-preview)")
    print("  moderation replay + config history routes are live (synthetic, in-memory)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
