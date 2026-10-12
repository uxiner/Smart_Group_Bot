#!/usr/bin/env python3
"""真实 ``VerifyWebServer`` + 合成 SQLite 的浏览器宿主（只绑 127.0.0.1）。

为什么需要它
============

``harness.py`` 是**内存假后端**：它自己发明了预算回包的形状（把
``daily_attempt_limit`` 摊在顶层、默认 ``currency="USD"``），于是真实页面上
"重新读取后额度全空 / 一次只改 token 的保存把 attempt 和 operation_limits
清成 null / {}" 这个缺陷在浏览器检查里**全绿**了两轮。

这个宿主不模拟任何东西：

* HTTP 层是产品自己的 ``VerifyWebServer.build_app()`` —— 真实路由、真实鉴权、
  真实 ``settings_api`` 处理器；
* 数据层是真实 ``init_db`` 建出来的**临时** SQLite（合成数据），
  播种走真实 ``GroupBudgetService.save_budget``（与 HTTP PUT 同一个共享实例）；
* 页面是 ``bot/web/static`` 的 ``index.html`` / ``app.js``（``/settings``）。

它只额外挂了几个**合成检查**端点，用来"精确确认落库"：

    GET  /__synthetic/hello                       宿主身份 / 库路径 / 播种内容
    GET  /__synthetic/db/budget?group_id=...      ``SELECT * FROM group_budgets``
                                                  + ``group_budget_days``
    GET  /__synthetic/db/onboarding               ``SELECT * FROM group_onboarding_requests``
    GET  /__synthetic/db/costs?group_id=...       ``llm_cost_daily`` + ``llm_cost_priced_daily``
                                                  + 冻结批 marker
    GET  /__synthetic/db/prices                   ``SELECT * FROM model_price_catalog``
    POST /__synthetic/reset                       清空合成数据并重新播种

它们读的是**原始行**（JSON 列当场解析），不是"再问一次同一支 API"——
否则浏览器证据仍然只在信任 API 自己的回包。

安全边界（硬约束）：只绑 ``127.0.0.1``、只读 ``.env`` 之外的显式参数
（``Settings(_env_file=None, ...)``）、数据库是临时文件、Telegram 是桩
（不发起任何网络请求）、不写仓库里的任何文件。

跑法见 ``tools/settings-ui-harness/README.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

log = logging.getLogger("synthetic_host")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from sqlalchemy import text  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.engine import init_db  # noqa: E402
from bot.db.models import (  # noqa: E402
    Admin,
    AuthorizedGroup,
    GroupOnboardingRequest,
    LlmCostDaily,
    ModelPriceCatalog,
)
from bot.services import group_costs, group_onboarding, pricing  # noqa: E402
from bot.services import usage_scope as scopes  # noqa: E402
from bot.services.group_budget import shared_service  # noqa: E402
from bot.services.runtime_config import RuntimeConfigManager  # noqa: E402
from bot.services.verify_web import VerifyWebServer  # noqa: E402
from bot.utils.timezone import now_shanghai_naive  # noqa: E402

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_A = -1001234567890
GROUP_B = -1009876543210
ADMIN_A = 7
APPLICANT_A = 9001
MEMBER_A = 8

#: 默认播种：非零额度 + 一个功能次数配额 + 一份能支撑金额上限的价目。
#: 只 seed 空预算是不够的 —— 空快照上"读取失败"和"真的没有额度"看起来一样。
DEFAULT_SEED: dict[str, Any] = {
    "enabled": True,
    "currency": "USD",
    "attempt": 9,
    "token": 20,
    "money": None,
    "operation_limits": {"tts": 3},
    "covers": [],
    "prices": [
        {
            "provider_key": "fixture",
            "model": "gpt-4o-mini",
            "currency": "USD",
            "input": 1,
            "output": 2,
        }
    ],
    #: `usage` 里的每一步都走**真实** `group_costs.record_attempt/record_result`，
    #: 所以金额是"发出那一刻"冻结的。`catalog` 步骤用来在两条记录之间换价目 ——
    #: 这就是"改价不追改历史"要验的场景（同一个模型 v1 与 v2 两个单价各算各的）。
    "catalogs": [
        {
            "version": 2,
            "prices": [
                {
                    "provider_key": "fixture",
                    "model": "gpt-4o-mini",
                    "currency": "USD",
                    "input": 100,
                    "output": 200,
                }
            ],
        },
        {
            "version": 3,
            "prices": [
                {
                    "provider_key": "fixture",
                    "model": "gpt-4o-mini",
                    "currency": "USD",
                    "input": 100,
                    "output": 200,
                },
                {
                    "provider_key": "fixture",
                    "model": "cny-model",
                    "currency": "CNY",
                    "input": 7,
                    "output": 0,
                },
            ],
        },
    ],
    "legacy": [
        {"group_id": GROUP_A, "provider": "fixture", "model": "legacy-model", "attempts": 3}
    ],
    "usage": [
        {"model": "gpt-4o-mini", "prompt": 10, "output": 1},
        {"catalog": 2},
        {"model": "gpt-4o-mini", "prompt": 10, "output": 1},
        {"catalog": 3},
        {"model": "cny-model", "prompt": 10, "output": 0},
        {"model": "no-price-model", "prompt": 10, "output": 1},
        {"model": "gpt-4o-mini", "no_usage": True},
    ],
    "requests": [
        {
            "group_id": GROUP_A,
            "applicant_id": APPLICANT_A,
            "requested_features": ["safety", "optional"],
            "status": "pending",
        }
    ],
}

#: 原始行里需要当场解析的 JSON 列（SQLite 把它们存成 TEXT）。
_JSON_COLUMNS = {
    "operation_limits",
    "used_operations",
    "requested_features",
    "payload",
    "price_snapshot",
}

_DB_TABLES = {
    "budget": "group_budgets",
    "budget_days": "group_budget_days",
    "onboarding": "group_onboarding_requests",
}

#: 机器人**群内权限**的可翻转合成开关。缺权诊断（"机器人缺少权限：…"）必须能用
#: 真实浏览器验到 —— 桩默认全给 True，否则面板永远是"权限齐全"。
_BOT_PERMISSION_KEYS = (
    "can_invite_users",
    "can_manage_chat",
    "can_delete_messages",
    "can_restrict_members",
    "can_pin_messages",
)
_BOT_PERMISSIONS: dict[str, bool] = {key: True for key in _BOT_PERMISSION_KEYS}


def set_bot_permissions(values: dict[str, Any]) -> dict[str, bool]:
    """翻转合成机器人的群权限（未知键或非布尔值一律拒绝，不静默忽略）。"""

    for key, value in values.items():
        if key not in _BOT_PERMISSIONS:
            raise ValueError(f"未知的机器人权限键：{key}")
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        _BOT_PERMISSIONS[key] = value
    return dict(_BOT_PERMISSIONS)


def reset_bot_permissions() -> dict[str, bool]:
    for key in _BOT_PERMISSION_KEYS:
        _BOT_PERMISSIONS[key] = True
    return dict(_BOT_PERMISSIONS)



def _telegram() -> SimpleNamespace:
    """不联网的 Telegram 桩：只回产物代码真正要看的那几个字段。"""

    async def get_chat(chat_id: Any) -> SimpleNamespace:
        return SimpleNamespace(id=int(chat_id), type="supergroup", title="合成群")

    async def get_chat_administrators(chat_id: Any):
        # 申请人（APPLICANT_A）必须也在权威管理员名单里：审批链会**复核申请人
        # 此刻**仍是群管理员（`require_live_group_administrator(bot, group_id,
        # applicant_id)`），漏了他这条合成申请就永远批不下来 —— 浏览器验收时
        # 表现成"POST 被拒：你不是该群的 Telegram 管理员"，很容易误判成产品缺陷。
        return [
            SimpleNamespace(
                user=SimpleNamespace(id=SUPER_ADMIN_ID, is_bot=False),
                status="administrator",
            ),
            SimpleNamespace(
                user=SimpleNamespace(id=ADMIN_A, is_bot=False), status="administrator"
            ),
            SimpleNamespace(
                user=SimpleNamespace(id=APPLICANT_A, is_bot=False),
                status="administrator",
            ),
        ]

    async def get_chat_member(chat_id: Any, user_id: Any) -> SimpleNamespace:
        return SimpleNamespace(
            # 超管必须在权威名单里也是管理员：`get_chat_administrators` 已经把他
            # 列进去了，这里说"member"就自相矛盾 —— 超管打开「新群接入」时
            # 实时管理员核验会 403，浏览器验收看到的是"你不是管理员"这种假缺陷。
            status="administrator"
            if int(user_id) in (SUPER_ADMIN_ID, ADMIN_A, APPLICANT_A)
            else "member",
            user=SimpleNamespace(id=int(user_id), is_bot=False),
            **_BOT_PERMISSIONS,
        )

    async def get_me() -> SimpleNamespace:
        return SimpleNamespace(id=424242, username="synthetic_fixture_bot", is_bot=True)

    return SimpleNamespace(
        token=BOT_TOKEN,
        get_chat=get_chat,
        get_chat_administrators=get_chat_administrators,
        get_chat_member=get_chat_member,
        get_me=get_me,
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        restrict_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )


def _merge_seed(raw: Any) -> dict[str, Any]:
    seed = json.loads(json.dumps(DEFAULT_SEED))
    if raw is None:
        return seed
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else {}
    if not isinstance(raw, dict):
        raise SystemExit("--seed 必须是 JSON 对象")
    seed.update(raw)
    return seed


def _entry(item: dict[str, Any]) -> pricing.PriceEntry:
    return pricing.PriceEntry(
        provider_key=str(item["provider_key"]),
        model=str(item["model"]),
        currency=str(item.get("currency") or "USD"),
        input_per_million=item.get("input"),
        output_per_million=item.get("output"),
        cache_read_per_million=item.get("cache_read"),
        cache_write_per_million=item.get("cache_write"),
    )


def _catalog_at(seed: dict[str, Any], version: int) -> pricing.PriceCatalog:
    """按版本取播种价目：1 用 ``prices``，更高版本用 ``catalogs`` 里那一份。"""

    if version <= 1:
        entries = [_entry(item) for item in seed.get("prices") or []]
        return pricing.PriceCatalog(version=1, entries=tuple(entries))
    for item in seed.get("catalogs") or []:
        if int(item.get("version") or 0) == version:
            entries = [_entry(entry) for entry in item.get("prices") or []]
            return pricing.PriceCatalog(version=version, entries=tuple(entries))
    raise SystemExit(f"catalogs 里没有 version={version}")


def _price_catalog(seed: dict[str, Any]) -> pricing.PriceCatalog:
    """**当前**生效的价目 = 最后一个播种版本（没有更高版本时就是 version 1）。

    金额报告要同时看到 USD 与 CNY，所以最后一版必须两种币种的条目都在 ——
    否则"币种隔离"那条会退化成"另一个币种没价目"。
    """

    versions = [1] + sorted(
        int(item.get("version") or 0) for item in seed.get("catalogs") or []
    )
    return _catalog_at(seed, max(versions))


@dataclass
class SyntheticHost:
    """宿主句柄：真实 app + 真实引擎 + 可复现的播种/重置。"""

    app: web.Application
    engine: Any
    session_factory: Any
    settings: Settings
    manager: RuntimeConfigManager
    db_path: str
    group_id: int
    seed: dict[str, Any]
    owns_db: bool = True
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ------------------------------------------------------------------
    async def _delete_synthetic_rows(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("DELETE FROM group_budget_days"))
            await conn.execute(text("DELETE FROM group_budgets"))
            await conn.execute(text("DELETE FROM group_onboarding_requests"))
            await conn.execute(text("DELETE FROM admins"))
            await conn.execute(text("DELETE FROM authorized_groups"))
            # 台账也要清：一次 reset 就是一条独立的账，"上一臂的用量"不能漏进来
            # 冒充这一臂的数字。marker 一起清，冻结批才不会被当成已应用。
            await conn.execute(text("DELETE FROM llm_cost_daily"))
            await conn.execute(text("DELETE FROM llm_cost_priced_daily"))
            await conn.execute(text("DELETE FROM llm_cost_flush_batches"))
            # 价格目录也要回到播种状态：不然上一臂 PUT 出来的版本会留在库里，
            # 下一臂的 expected_version 就和 CAS 基线对不上，失败原因会被误读。
            await conn.execute(text("DELETE FROM model_price_catalog"))

    async def _seed_price_catalog(self) -> None:
        """把播种的每一个价目版本**按序**写进持久表（最新一版就是 CAS 基线）。

        只写缓存是不够的：真正的 CAS 读的是 ``model_price_catalog`` 的持久
        ``MAX(version)``，两边不一致时第一发 PUT 就会被当成 stale。
        """

        versions = sorted(
            int(item.get("version") or 0) for item in self.seed.get("catalogs") or []
        )
        async with self.session_factory() as session:
            async with session.begin():
                for version in [1] + versions:
                    catalog = _catalog_at(self.seed, version)
                    session.add(
                        ModelPriceCatalog(
                            version=int(catalog.version),
                            payload=list(catalog.document().get("entries") or []),
                            updated_by=SUPER_ADMIN_ID,
                        )
                    )

    async def _seed_legacy(self) -> None:
        """没有发送时价格元数据的**旧**行：主表有 attempts，计价表没有对应行。

        它们必须一直是"未计价/未知覆盖"，绝不能被 latest 价目回算成金额。
        """

        today = now_shanghai_naive().date().isoformat()
        async with self.session_factory() as session:
            async with session.begin():
                for item in self.seed.get("legacy") or []:
                    attempts = int(item.get("attempts") or 0)
                    session.add(
                        LlmCostDaily(
                            usage_date=today,
                            scope="group",
                            group_id=int(item.get("group_id") or self.group_id),
                            purpose=str(item.get("purpose") or "optional"),
                            stage=str(item.get("stage") or "main"),
                            provider=str(item.get("provider") or "fixture"),
                            model=str(item.get("model") or "legacy-model"),
                            attempts=attempts,
                            successes=attempts,
                            prompt_tokens=int(item.get("prompt") or 0),
                            output_tokens=int(item.get("output") or 0),
                        )
                    )

    async def _seed_usage(self) -> None:
        """按步骤走**真实**台账函数，金额在每一步"发出时"冻结。

        ``{"catalog": N}`` 步骤切换当前价目 —— 于是同一个模型在两个单价版本上
        各算各的，改价不追改历史这条合同在真实数据上就成立了。
        """

        steps = list(self.seed.get("usage") or [])
        if not steps:
            return
        from bot.web.settings_api import set_price_catalog

        group_costs.configure(self.session_factory)
        # 从**最低**版本起步：不这样，第一条（没有显式 catalog 的）记录会用到
        # reset 末尾设的"当前版本"，于是"改价不追改历史"这一条自己就假了。
        set_price_catalog(_catalog_at(self.seed, 1))
        for step in steps:
            if "catalog" in step:
                set_price_catalog(_catalog_at(self.seed, int(step["catalog"])))
                continue
            usage = None
            if not step.get("no_usage"):
                usage = {
                    "prompt_tokens": int(step.get("prompt") or 0),
                    "completion_tokens": int(step.get("output") or 0),
                }
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP,
                group_id=int(step.get("group_id") or self.group_id),
                purpose=str(step.get("purpose") or "optional"),
            ):
                attempt_id = group_costs.record_attempt(
                    stage=str(step.get("stage") or "main"),
                    provider=str(step.get("provider") or "fixture"),
                    model=str(step["model"]),
                )
                group_costs.record_result(
                    attempt_id=attempt_id, usage=usage, success=True
                )
        # 必须真落库：报表读的是表，不是内存 counter。force 是持久化屏障。
        await group_costs.flush(force=True)

    async def _seed(self) -> None:
        seed = self.seed
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(
                        group_id=self.group_id, authorized_by=SUPER_ADMIN_ID
                    )
                )
                session.add(
                    AuthorizedGroup(group_id=GROUP_B, authorized_by=SUPER_ADMIN_ID)
                )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    Admin(group_id=self.group_id, user_id=ADMIN_A, role="admin")
                )
        covers = [
            (str(item["provider_key"]), str(item["model"]))
            for item in (seed.get("covers") or [])
        ]
        # 与 HTTP PUT 走**同一个**共享实例（register_settings_routes 也调它）。
        service = shared_service(self.session_factory, _catalog_provider)
        await service.save_budget(
            self.group_id,
            expected_revision=0,
            enabled=bool(seed.get("enabled", False)),
            currency=str(seed.get("currency") or ""),
            daily_attempt_limit=seed.get("attempt"),
            daily_token_limit=seed.get("token"),
            daily_money_micro_limit=seed.get("money"),
            operation_limits=dict(seed.get("operation_limits") or {}),
            money_covers=covers,
            updated_by=SUPER_ADMIN_ID,
        )
        for item in seed.get("requests") or []:
            async with self.session_factory() as session:
                async with session.begin():
                    session.add(
                        GroupOnboardingRequest(
                            request_id=str(
                                item.get("request_id")
                                or f"synthetic-{int(item.get('applicant_id', 0))}"
                            ),
                            group_id=int(item.get("group_id") or self.group_id),
                            applicant_id=int(item.get("applicant_id") or APPLICANT_A),
                            requested_features=list(
                                item.get("requested_features") or []
                            ),
                            status=str(item.get("status") or "pending"),
                            reason=str(item.get("reason") or ""),
                        )
                    )

    async def reset(self) -> None:
        """清空合成数据并重新播种（每个浏览器阶段开始前调用）。"""

        async with self._lock:
            group_costs.reset()
            # 检查端点的限次表是**进程级内存**（每个申请人 10 次/60 秒），不在数据库里，
            # 删表清不掉它。不清的话连跑两次浏览器套件时第二轮的 check 会被 429 拒掉，
            # 看上去像产品缺陷。清的是限次表，不是限次逻辑本身：check 仍然必须返回 200，
            # 限次契约由 tests/test_group_presets.py 的 API 用例守着。
            group_onboarding.reset_rate_limit()
            await self._delete_synthetic_rows()
            await self._seed_price_catalog()
            from bot.web.settings_api import set_price_catalog

            set_price_catalog(_price_catalog(self.seed))
            await self._seed()
            await self._seed_legacy()
            await self._seed_usage()
            # usage 步骤会为了"改价不追改历史"临时切到旧版本；跑完必须回到当前
            # 版本，否则价格编辑器与金额上限校验看到的是历史价目。
            set_price_catalog(_price_catalog(self.seed))

    async def close(self) -> None:
        await self.engine.dispose()
        if self.owns_db:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(f"{self.db_path}{suffix}")
                except OSError:
                    pass


def _catalog_provider() -> pricing.PriceCatalog:
    from bot.web.settings_api import price_catalog_provider

    return price_catalog_provider()


def _row_to_dict(row: Any) -> dict[str, Any]:
    # ``.mappings()`` 给的是 RowMapping，``.first()``/``.all()`` 的两种用法都见过，
    # 这里两种都收。
    mapping = row._mapping if hasattr(row, "_mapping") else row
    data = dict(mapping)
    for key in list(data):
        if key in _JSON_COLUMNS and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except ValueError:
                pass
    return data


def register_synthetic_routes(app: web.Application, host: SyntheticHost) -> None:
    """挂上宿主专属的**只读/重置**检查端点（真实路由一概不动）。"""

    @web.middleware
    async def synthetic_error_visibility(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        """合成端点的异常必须带诊断信息，而不是光秃秃一个 500。

        真实路由的异常照旧往外抛（不改产品行为），只有 ``/__synthetic/`` 前缀被
        转成可读的 JSON —— 曾经一次 ``reset`` 的 500 因为没有 traceback 无法定位。
        """

        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 - 诊断路径就是要看到全部
            if not request.path.startswith("/__synthetic/"):
                raise
            log.exception("synthetic endpoint failed: %s %s", request.method, request.path)
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "synthetic_host_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                },
                status=500,
            )

    app.middlewares.append(synthetic_error_visibility)

    async def hello(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "kind": "synthetic-browser-host",
                "group_id": host.group_id,
                "db_path": host.db_path,
                "super_admin_id": int(host.settings.super_admin_id or 0),
                "seed": host.seed,
            }
        )

    async def db_budget(request: web.Request) -> web.Response:
        raw = request.query.get("group_id")
        try:
            group_id = int(raw) if raw is not None else host.group_id
        except ValueError:
            return web.json_response({"ok": False, "error": "group_id 无效"}, status=400)
        async with host.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM group_budgets WHERE group_id = :g"),
                    {"g": group_id},
                )
            ).mappings().first()
            days = (
                await conn.execute(
                    text(
                        "SELECT * FROM group_budget_days WHERE group_id = :g "
                        "ORDER BY budget_date"
                    ),
                    {"g": group_id},
                )
            ).mappings().all()
        return web.json_response(
            {
                "ok": True,
                "budget": _row_to_dict(row) if row is not None else None,
                "days": [_row_to_dict(day) for day in days],
            }
        )

    async def db_onboarding(_request: web.Request) -> web.Response:
        async with host.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM group_onboarding_requests "
                        "ORDER BY created_at, id"
                    )
                )
            ).mappings().all()
        return web.json_response(
            {"ok": True, "rows": [_row_to_dict(row) for row in rows]}
        )

    async def db_costs(request: web.Request) -> web.Response:
        """台账两张表的**原始行** + 冻结批 marker。

        金额是"发出时冻结"的，所以断言必须落在 ``llm_cost_priced_daily`` 的
        ``price_version / currency / amount_micro`` 上，而不是页面上的文字。
        两张表是同一次 attempt 的不同维度，**不能相加** —— 这里也把主表的
        attempts 一起回，方便核对差额（差额 = 早于单价冻结的旧调用）。
        """

        raw = request.query.get("group_id")
        try:
            group_id = int(raw) if raw is not None else host.group_id
        except ValueError:
            return web.json_response({"ok": False, "error": "group_id 无效"}, status=400)
        async with host.engine.connect() as conn:
            main_rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM llm_cost_daily WHERE group_id = :g "
                        "ORDER BY usage_date, id"
                    ),
                    {"g": group_id},
                )
            ).mappings().all()
            priced_rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM llm_cost_priced_daily WHERE group_id = :g "
                        "ORDER BY usage_date, id"
                    ),
                    {"g": group_id},
                )
            ).mappings().all()
            batches = (
                await conn.execute(text("SELECT batch_id FROM llm_cost_flush_batches"))
            ).mappings().all()
        return web.json_response(
            {
                "ok": True,
                "group_id": group_id,
                "main": [_row_to_dict(row) for row in main_rows],
                "priced": [_row_to_dict(row) for row in priced_rows],
                "batches": [row["batch_id"] for row in batches],
            }
        )

    async def db_prices(_request: web.Request) -> web.Response:
        """价格目录的**持久**行（版本只增不改的原始证据）。

        页面自己的 GET 读的是模块缓存 —— 用它当"写成功"的证据等于再问一遍
        同一支 API。这里读 ``model_price_catalog`` 表。
        """

        async with host.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT * FROM model_price_catalog ORDER BY version")
                )
            ).mappings().all()
        return web.json_response(
            {"ok": True, "rows": [_row_to_dict(row) for row in rows]}
        )

    async def reset(_request: web.Request) -> web.Response:
        reset_bot_permissions()
        await host.reset()
        return web.json_response({"ok": True, "permissions": dict(_BOT_PERMISSIONS)})

    async def telegram_permissions(request: web.Request) -> web.Response:
        """翻转合成机器人的群权限，用来验"缺权限"这条诊断。"""

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 客户端给了坏 JSON
            return web.json_response(
                {"ok": False, "error": "body 必须是 JSON"}, status=400
            )
        values = body.get("permissions") if isinstance(body, dict) else None
        if not isinstance(values, dict) or not values:
            return web.json_response(
                {"ok": False, "error": '需要 {"permissions": {...}}'}, status=400
            )
        try:
            current = set_bot_permissions(values)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "permissions": current})

    app.router.add_get("/__synthetic/hello", hello)
    app.router.add_get("/__synthetic/db/budget", db_budget)
    app.router.add_get("/__synthetic/db/onboarding", db_onboarding)
    app.router.add_get("/__synthetic/db/costs", db_costs)
    app.router.add_get("/__synthetic/db/prices", db_prices)
    app.router.add_post("/__synthetic/reset", reset)
    app.router.add_post("/__synthetic/telegram-permissions", telegram_permissions)


async def build_host(
    *,
    db_path: str | None = None,
    seed: Any = None,
    group_id: int = GROUP_A,
    super_admin_id: int = SUPER_ADMIN_ID,
    bot_token: str = BOT_TOKEN,
    public_base_url: str = "http://127.0.0.1:8821",
    listen_port: int = 8821,
) -> SyntheticHost:
    """建一个真实 app + 真实临时库的宿主（``main`` 与回归测试共用）。"""

    owns_db = db_path is None
    if db_path is None:
        fd, db_path = tempfile.mkstemp(suffix="-synthetic-browser.db")
        os.close(fd)
    engine, session_factory = await init_db(f"sqlite+aiosqlite:///{db_path}")
    settings = Settings(
        _env_file=None,
        bot_token=bot_token,
        super_admin_id=super_admin_id,
        config_master_key="synthetic-browser-host-key",
    )
    settings.bot.token = bot_token
    settings.miniapp_public_base_url = public_base_url
    settings.join_verification_public_base_url = public_base_url
    settings.miniapp_listen_host = "127.0.0.1"
    settings.miniapp_listen_port = int(listen_port)
    manager = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
        legacy_config_path="/tmp/nonexistent-synthetic-browser.toml",
        legacy_raw_env={},
    )
    await manager.initialize()

    from bot.web.auth import reset_init_data_replay_budget
    from bot.web.settings_api import (
        configure_price_store,
        restore_price_catalog,
        set_price_catalog,
    )

    reset_init_data_replay_budget()
    scopes.reset()
    group_costs.reset()
    # 权限开关同样是模块级：本次宿主必须从"权限齐全"开始，否则会继承上一次
    # 翻转过的缺权状态，浏览器验收直接看到假的"机器人缺少权限"。
    reset_bot_permissions()
    # 价格目录是模块级状态：上一次宿主/测试留下的目录必须归零，
    # 否则这里的"金额上限能被校验"就可能是上一轮的残留。
    set_price_catalog(pricing.PriceCatalog(version=1))
    # 而且**必须**把 store 指到本次自己的引擎上：不指的话 `restore_price_catalog()`
    # 会拿上一个用例（甚至已删掉的临时库）留下的会话工厂去查 `model_price_catalog`，
    # 于是单独跑这个文件是绿的、跟其它价格目录用例一起跑就整片
    # `no such table: model_price_catalog`。生产启动序列也是这么装的。
    configure_price_store(session_factory)
    await restore_price_catalog()

    resolved_seed = _merge_seed(seed)
    host = SyntheticHost(
        app=web.Application(),
        engine=engine,
        session_factory=session_factory,
        settings=settings,
        manager=manager,
        db_path=db_path,
        group_id=int(group_id),
        seed=resolved_seed,
        owns_db=owns_db,
    )
    set_price_catalog(_price_catalog(resolved_seed))
    server = VerifyWebServer(
        bot=_telegram(),
        settings=settings,
        session_factory=session_factory,
        runtime_config=manager,
    )
    app = server.build_app()
    host.app = app
    await host.reset()
    register_synthetic_routes(app, host)
    return host


async def _serve_async(args: argparse.Namespace) -> None:
    # WARNING 足够让合成端点的异常（`synthetic_host` 的 ERROR）和 aiohttp 的
    # 未处理异常留在日志里，又不会被每次启动的 INFO 淹掉。
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # 引擎、会话工厂与 app **必须**在同一个事件循环里建：aiosqlite 的连接绑在
    # 建它的那个 loop 上，先在短命 loop 里 init_db 再交给 run_app 的新 loop，
    # 合成检查端点会以 "attached to a different loop" 500。
    host = await build_host(
        db_path=args.db,
        seed=args.seed,
        group_id=args.group_id,
        super_admin_id=args.super_admin_id,
        public_base_url=f"http://127.0.0.1:{args.port}",
        listen_port=args.port,
    )
    runner = web.AppRunner(host.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    try:
        await site.start()
    except OSError as exc:
        # 端口被别的进程占着时**必须**立刻响亮地失败。否则这个进程会静默地
        # 什么都没干，而驱动仍然连得上那台**残留的旧宿主** —— 检查脚本会拿
        # 旧代码的回包去比对新代码的期望，报出一堆看起来像产品缺陷的假红。
        await runner.cleanup()
        await host.close()
        raise SystemExit(
            f"端口 {args.port} 已被占用（{exc}）。请先停掉残留宿主再启动："
            f"lsof -nP -iTCP:{args.port} -sTCP:LISTEN"
        ) from exc
    base = f"http://127.0.0.1:{args.port}"
    print(f"READY {base}  (settings page: {base}/settings)", flush=True)
    print(
        f"  synthetic db: {host.db_path}\n"
        f"  group_id={host.group_id} super_admin_id={args.super_admin_id}\n"
        f"  seed={json.dumps(host.seed, ensure_ascii=False)}",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        import signal as _signal

        try:
            loop.add_signal_handler(getattr(_signal, signal_name), stop.set)
        except (NotImplementedError, ValueError):
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await host.close()
        print("  synthetic host stopped", flush=True)


def _serve(args: argparse.Namespace) -> None:
    asyncio.run(_serve_async(args))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="真实 VerifyWebServer + 合成 SQLite 的浏览器宿主（仅 127.0.0.1）"
    )
    parser.add_argument("--port", type=int, default=8821)
    parser.add_argument("--db", default=None, help="复用指定 SQLite 路径（默认建临时库）")
    parser.add_argument("--group-id", type=int, default=GROUP_A)
    parser.add_argument("--super-admin-id", type=int, default=SUPER_ADMIN_ID)
    parser.add_argument(
        "--seed",
        default=None,
        help="播种 JSON（见 DEFAULT_SEED；只传要覆盖的键）",
    )
    args = parser.parse_args()
    try:
        _serve(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
