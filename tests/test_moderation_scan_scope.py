"""规则扫描范围（scan_scope）与"机器人注入文本"剥离的回归测试。

背景（2026-10-01 生产事故）：群成员发了一张淘宝 88VIP 手机截屏，机器人把图交给
视觉模型生成描述，送审文本变成 ``[image]\\n[image-vision]\\n图为88VIP音乐会员页面…秒杀…``，
软广告正则（规则 #5，delete）里的 ``秒杀`` 直接命中、置信度 1.0、四张无辜截屏被删。
这里锁定：正则/关键词默认只看用户自己写的正文；需要时用组合范围放大；语义规则
不受影响。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from bot.config import ModerationConfig
from bot.db.engine import (
    _PRODUCTION_RULE_6_PATTERN,
    _sqlite_ensure_column,
    _sqlite_upgrade_moderation_rule_scan_scopes,
)
from bot.db.models import ModerationRule
from bot.services.moderation import (
    ModerationService,
    normalize_scan_scope,
    parse_scan_scope,
    split_moderation_text,
)


class _NoAutoflush:
    def __enter__(self) -> "_NoAutoflush":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


def _session(rules: list[ModerationRule]) -> SimpleNamespace:
    return SimpleNamespace(
        no_autoflush=_NoAutoflush(),
        execute=AsyncMock(return_value=_RowsResult(list(rules))),
        commit=AsyncMock(),
    )


def _service(llm_response: str = "{}", *, threshold: float = 0.9) -> ModerationService:
    llm = SimpleNamespace(moderation=AsyncMock(return_value=llm_response))
    return ModerationService(
        ModerationConfig(high_confidence_threshold=threshold),
        llm,
    )


def _rule(
    *,
    rule_id: int = 1,
    rule_type: str = "regex",
    pattern: str = "秒杀",
    action: str = "delete",
    scan_scope: str | None = "message",
) -> ModerationRule:
    kwargs: dict[str, object] = {
        "id": rule_id,
        "group_id": -100,
        "rule_type": rule_type,
        "pattern": pattern,
        "action": action,
        "enabled": True,
    }
    if scan_scope is not None:
        kwargs["scan_scope"] = scan_scope
    return ModerationRule(**kwargs)


class ScanScopeParsingTests(unittest.TestCase):
    def test_defaults_and_aliases_normalize(self) -> None:
        cases = (
            (None, "message"),
            ("", "message"),
            ("message", "message"),
            ("message+quote", "message+quote"),
            ("quote", "message+quote"),
            ("vision", "message+vision"),
            ("vision+quote", "message+quote+vision"),
            ("MESSAGE+QUOTE+VISION", "message+quote+vision"),
            ("message+unknown", "message"),
            ("bogus", "message"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_scan_scope(raw), expected)

    def test_parse_returns_token_set_with_message_always_present(self) -> None:
        self.assertEqual(parse_scan_scope("message+vision"), {"message", "vision"})
        self.assertEqual(parse_scan_scope(None), {"message"})


class SplitModerationTextTests(unittest.TestCase):
    def test_marker_free_text_is_returned_byte_identical(self) -> None:
        for raw in ("/lucky_checkin@checkin_helper_bot", "  prefix\n\nsuffix  ", ""):
            with self.subTest(raw=raw):
                segments = split_moderation_text(raw)
                self.assertEqual(segments.own, raw)
                self.assertEqual(segments.quote, "")
                self.assertEqual(segments.vision, "")

    def test_vision_and_quote_are_split_out_and_markers_stripped(self) -> None:
        raw = (
            "[image]\n"
            "[image-vision]\n"
            "图为88VIP音乐会员页面，含专属秒杀\n"
            "以及精选活动信息。\n"
            "[reply_to_user] id:100000001 username:@owner_demo\n"
            "[reply_to:text] 兼职招募 加V\n"
            "[reply_quote] 秒杀 优惠券"
        )
        segments = split_moderation_text(raw)
        self.assertEqual(segments.own, "[image]")
        self.assertEqual(
            segments.vision, "图为88VIP音乐会员页面，含专属秒杀\n以及精选活动信息。"
        )
        # 身份标记（用户名/ID）整行丢弃，正文标记只剥标记本身。
        self.assertEqual(segments.quote, "兼职招募 加V\n秒杀 优惠券")
        self.assertNotIn("100000001", segments.own + segments.quote + segments.vision)

    def test_external_reply_markers_are_quote_content(self) -> None:
        raw = (
            "v\n"
            "[external_reply_user] id:42 username:@somebody\n"
            "[external_reply_chat] -100123\n"
            "[external_reply:text] 加我微信 abc123"
        )
        segments = split_moderation_text(raw)
        self.assertEqual(segments.own, "v")
        self.assertEqual(segments.quote, "加我微信 abc123")
        self.assertNotIn("somebody", segments.quote)


class ScanScopeEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_vision_regression_default_scope_does_not_match_description(self) -> None:
        """本次事故的回归用例：默认 message 不得拿图片描述去匹配。"""

        text = "[image]\n[image-vision]\n图含专属秒杀与优惠券信息"
        rule = _rule(pattern="秒杀", scan_scope="message")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertFalse(verdict.violated)
        self.assertTrue(verdict.conclusive)

    async def test_vision_scope_matches_description_when_opted_in(self) -> None:
        text = "[image]\n[image-vision]\n图含专属秒杀与优惠券信息"
        rule = _rule(pattern="秒杀", scan_scope="message+vision")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.confidence, 1.0)
        self.assertEqual(verdict.rule.id, rule.id)
        self.assertEqual(verdict.match_source, "vision")

    async def test_plain_message_scope_still_matches_own_text(self) -> None:
        text = "[image]\n[image-vision]\n图含专属秒杀与优惠券信息"
        rule = _rule(pattern="秒杀", scan_scope="message")
        # 用户自己在正文里写"秒杀"时照旧命中（默认范围不是"什么都不扫"）。
        verdict = await _service().evaluate(
            _session([rule]), -100, "[image]\n用户自己说秒杀"
        )
        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "own")

    async def test_quoted_ad_is_ignored_by_default_scope(self) -> None:
        text = "v\n[reply_to:text] 兼职招募 加V 私聊"
        rule = _rule(pattern="招募", scan_scope="message")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertFalse(verdict.violated)

    async def test_quoted_ad_matches_with_message_plus_quote(self) -> None:
        text = "v\n[reply_to_user] id:1 username:@ad\n[reply_to:text] 兼职招募 加V 私聊"
        rule = _rule(pattern="招募", scan_scope="message+quote")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "quote")

    async def test_quote_scope_does_not_match_identity_marker_payload(self) -> None:
        """标记里的用户名/数字不是内容，不能造成误命中。"""

        text = (
            "v\n"
            "[reply_to_user] id:100000001 username:@user_demo\n"
            "[reply_to_chat] -1001234567890"
        )
        rule = _rule(pattern=r"\d{11,}", scan_scope="message+quote")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertFalse(verdict.violated)

    async def test_keyword_rule_respects_scope(self) -> None:
        text = "v\n[reply_to:text] 兼职招募"
        default_rule = _rule(
            rule_id=1, rule_type="keyword", pattern="招募", scan_scope="message"
        )
        quoted_rule = _rule(
            rule_id=2, rule_type="keyword", pattern="招募", scan_scope="message+quote"
        )

        default_verdict = await _service().evaluate(_session([default_rule]), -100, text)
        quoted_verdict = await _service().evaluate(_session([quoted_rule]), -100, text)

        self.assertFalse(default_verdict.violated)
        self.assertTrue(quoted_verdict.violated)
        self.assertEqual(quoted_verdict.match_source, "quote")

    async def test_default_scope_still_matches_when_rule_has_no_scope_set(self) -> None:
        text = "v\n[reply_to:text] 兼职招募"
        legacy_rule = _rule(pattern="招募", scan_scope=None)

        verdict = await _service().evaluate(_session([legacy_rule]), -100, text)

        self.assertFalse(verdict.violated)

    async def test_semantic_rule_still_sees_the_full_text(self) -> None:
        """语义规则不受扫描范围影响：模型仍然看到图片描述。"""

        text = "[image]\n[image-vision]\n图为88VIP音乐会员页面，含专属秒杀及精选活动信息。"
        llm_rule = _rule(
            rule_id=9,
            rule_type="llm",
            pattern="禁止广告",
            action="ban",
            scan_scope="message",
        )
        service = _service(
            '{"violated": true, "rule_id": 9, "reason": "广告", "confidence": 0.95}'
        )

        verdict = await service.evaluate(_session([llm_rule]), -100, text)

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "semantic")
        prompt_payload = service.llm.moderation.await_args.args[1]
        self.assertIn("88VIP", prompt_payload)

    async def test_scan_scope_does_not_change_the_rules_fingerprint(self) -> None:
        """范围不进指纹：避免部署时全量失效既有档案筛查缓存。

        `evaluate` 的指纹与 `moderation_rules_fingerprint` 必须一致，
        两边都保持 (id, rule_type, pattern, action)。
        """

        text = "ordinary text"
        service = _service()
        default_fp = (
            await service.evaluate(
                _session([_rule(scan_scope="message")]), -100, text
            )
        ).rules_fingerprint
        quote_fp = (
            await service.evaluate(
                _session([_rule(scan_scope="message+quote")]), -100, text
            )
        ).rules_fingerprint

        self.assertEqual(default_fp, quote_fp)
        self.assertTrue(default_fp)

    async def test_anchored_regex_still_works_for_marker_free_text(self) -> None:
        rule = _rule(
            rule_id=16,
            pattern=r"^lucky_checkin$",
            action="warn",
            scan_scope="message",
        )

        verdict = await _service().evaluate(
            _session([rule]), -100, "prefix /lucky_checkin@checkin_helper_bot suffix"
        )

        self.assertFalse(verdict.violated)

    async def test_cross_segment_match_does_not_fall_back_to_quote(self) -> None:
        r"""F-004：组合文本命中、单段都定位不到时，绝不能归到 quote。

        `\s` 跨段锚定（own 与 quote 之间是 "\n"）会让两段单独匹配都落空，旧兜底
        直接 `return SCAN_SCOPE_QUOTE`——"可能是自己写的"就变成了"来自引文"，
        而引用连坐拿它当证据（确定性规则置信度 1.0，连高置信阈值都不用够）。
        修复后归属未知一律 fail-closed 算 own。
        """

        text = "上半段\n[reply_to:text] 下半段"
        rule = _rule(pattern=r"上半段\s+下半段", scan_scope="message+quote")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertTrue(verdict.violated, "组合文本命中，规则本身照旧要生效")
        self.assertEqual(verdict.match_source, "own")
        self.assertNotEqual(verdict.match_source, "quote")

    async def test_cross_segment_match_with_vision_does_not_fall_back_to_vision(self) -> None:
        """F-004：图片描述段的跨段命中同样不能凭空归到 vision。"""

        text = "上半段\n[image-vision] 下半段"
        rule = _rule(pattern=r"上半段\s+下半段", scan_scope="message+vision")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "own")
        self.assertNotEqual(verdict.match_source, "vision")

    async def test_single_segment_quote_hit_still_reports_quote(self) -> None:
        """F-004 的另一边：引文段自己就能定位到命中时，归属仍然如实报 quote。"""

        text = "v\n[reply_to:text] 兼职招募 加V 私聊"
        rule = _rule(pattern="招募", scan_scope="message+quote")

        verdict = await _service().evaluate(_session([rule]), -100, text)

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "quote")


_LEGACY_RULES_DDL = (
    "CREATE TABLE moderation_rules ("
    "id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL, "
    "rule_type VARCHAR(32) NOT NULL, pattern TEXT NOT NULL DEFAULT '', "
    "action VARCHAR(32) NOT NULL DEFAULT 'warn', "
    "enabled BOOLEAN NOT NULL DEFAULT 1)"
)

_LEGACY_RULES = (
    (1, -100, "keyword", "秒杀", "delete", 1),
    (2, -100, "keyword", "优惠券", "delete", 1),
    (3, -100, "regex", "包邮", "delete", 1),
    (4, -100, "keyword", "加V", "delete", 1),
    (5, -100, "regex", "秒杀|优惠券", "delete", 1),
    # 规则正文**必须**与 `bot/db/engine.py` 里那条生产规则的真值一致：F-011 之后升级要
    # 核对内容指纹，fixture 自己编一个正文就会让这两条用例变成"测一个不存在的部署"。
    (6, -100, "regex", _PRODUCTION_RULE_6_PATTERN, "ban", 1),
    (7, -100, "regex", "兼职接单", "warn", 1),
)


class ScanScopeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def _legacy_engine(self):
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.execute(text(_LEGACY_RULES_DDL))
            for row in _LEGACY_RULES:
                await conn.execute(
                    text(
                        "INSERT INTO moderation_rules "
                        "(id, group_id, rule_type, pattern, action, enabled) "
                        "VALUES (:id, :group_id, :rule_type, :pattern, :action, :enabled)"
                    ),
                    {
                        "id": row[0],
                        "group_id": row[1],
                        "rule_type": row[2],
                        "pattern": row[3],
                        "action": row[4],
                        "enabled": row[5],
                    },
                )
        return engine

    async def test_legacy_rules_get_message_default_and_rule_6_is_upgraded(self) -> None:
        engine = await self._legacy_engine()
        async with engine.begin() as conn:
            # 迁移顺序与 init_db 一致：先加列（老行拿默认值），再升级生产规则。
            await _sqlite_ensure_column(
                conn,
                "moderation_rules",
                "scan_scope",
                "scan_scope VARCHAR(32) NOT NULL DEFAULT 'message'",
            )
            changed = await _sqlite_upgrade_moderation_rule_scan_scopes(conn)
            rows = (
                await conn.execute(
                    text("SELECT id, scan_scope FROM moderation_rules ORDER BY id")
                )
            ).all()

        scopes = {int(row[0]): str(row[1]) for row in rows}
        self.assertEqual(changed, 1)
        # 规则 #6（regex + ban）升级到"正文 + 引用 + 图片描述"。
        self.assertEqual(scopes[6], "message+quote+vision")
        # 其余老规则一律保持旧行为。
        for rule_id in (1, 2, 3, 4, 5, 7):
            self.assertEqual(scopes[rule_id], "message", rule_id)

    async def test_backfill_is_idempotent_and_keeps_manual_configuration(self) -> None:
        engine = await self._legacy_engine()
        async with engine.begin() as conn:
            await _sqlite_ensure_column(
                conn,
                "moderation_rules",
                "scan_scope",
                "scan_scope VARCHAR(32) NOT NULL DEFAULT 'message'",
            )
            await conn.execute(
                text("UPDATE moderation_rules SET scan_scope = 'message+quote' WHERE id = 6")
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 0
            )
            # 再跑一次：已经是目标值，不应重复升级。
            await conn.execute(
                text(
                    "UPDATE moderation_rules SET scan_scope = 'message' WHERE id = 6"
                )
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 1
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 0
            )


class RuleApiScanScopeTests(unittest.TestCase):
    """Mini App（settings_api）必须能读到并写入 scan_scope。"""

    def test_rule_document_and_payloads_expose_scan_scope(self) -> None:
        from pydantic import ValidationError

        from bot.web.settings_api import _RuleCreate, _RuleUpdate, _rule_document

        self.assertEqual(
            _rule_document(_rule(scan_scope="quote"))["scan_scope"],
            "message+quote",
        )
        self.assertEqual(
            _rule_document(_rule(scan_scope=None))["scan_scope"],
            "message",
        )
        self.assertEqual(
            _RuleCreate(
                rule_type="regex", pattern="x", scan_scope="message+vision"
            ).scan_scope,
            "message+vision",
        )
        # 不传就是默认范围，老前端照旧工作。
        self.assertEqual(
            _RuleCreate(rule_type="regex", pattern="x").scan_scope, "message"
        )
        self.assertEqual(
            _RuleUpdate(scan_scope="message+quote+vision").scan_scope,
            "message+quote+vision",
        )
        with self.assertRaises(ValidationError):
            _RuleCreate(rule_type="regex", pattern="x", scan_scope="bogus")


class RulesFingerprintParityTests(unittest.IsolatedAsyncioTestCase):
    """`evaluate` 的指纹必须与 `moderation_rules_fingerprint` 完全一致。

    两者不一致时 `_claim_current_moderation_verdict` 会认为规则被改过，
    **所有**处置都会被静默丢弃。新增 scan_scope 字段不能改变这个约定。
    """

    async def test_evaluate_fingerprint_matches_rule_cache_fingerprint(self) -> None:
        import os
        import tempfile

        from bot.db.engine import init_db
        from bot.db.models import Group
        from bot.services.join_screening import moderation_rules_fingerprint

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            async with session_factory() as session:
                session.add(Group(id=-100, title="t"))
                session.add(
                    _rule(
                        rule_id=6,
                        pattern="招募",
                        action="ban",
                        scan_scope="message+quote+vision",
                    )
                )
                await session.commit()

                service = _service()
                verdict = await service.evaluate(
                    session, -100, "v\n[reply_to:text] 招募 加V"
                )
                expected = await moderation_rules_fingerprint(session, -100)

                self.assertTrue(verdict.violated)
                self.assertEqual(verdict.match_source, "quote")
                self.assertEqual(verdict.rules_fingerprint, expected)
        finally:
            if engine is not None:
                await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
