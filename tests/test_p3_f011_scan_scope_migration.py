"""P3-1 / F-011：扫描范围的一次性升级不得只靠 id 命中（``bot/db/engine.py``）。

复现的原缺陷
------------
``_MODERATION_SCAN_SCOPE_UPGRADES`` 里硬编码了 ``(6, "regex", "ban",
"message+quote+vision")``，启动时**无条件**执行升级，判据只有
``id + rule_type + action``。在 id=6 含义不同的部署里，这会把**别人那条规则**的
匹配面从 ``message`` 静默放大成 ``message+quote+vision``；而引文（被引用/转发的
正文）与图片描述里常含广告词，等于批量误删/误封。

修复
----
1. 升级前**核对这一行确实是那条生产规则**：类型 + 动作 + 规则正文指纹三者全中
   才动手；不匹配的行跳过并留一条 INFO 日志说明跳过原因（日志只记指纹，不记
   管理员自由文本）。
2. 显式开关 ``Settings.legacy_scan_scope_migration_enabled`` → ``init_db(...,
   legacy_scan_scope_migration_enabled=...)``，**默认 True = 今天的行为**
   （确实是那条规则的行仍然照旧升级）。

本文件覆盖三条要求：① 匹配的规则被升级；② id 相同但内容不同的规则**不**被升级；
③ 幂等（跑两次只改一次）；外加开关关掉时一段写入都不做。

期望值（生产规则正文、指纹算法）在这里**独立写一遍**，不从被测实现里导入——
否则"实现改错了指纹"这种回归会被测试自己掩盖过去。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from bot.config import Settings
from bot.db.engine import (
    _sqlite_ensure_column,
    _sqlite_upgrade_moderation_rule_scan_scopes,
    init_db,
)

#: 生产部署里 id=6 那条硬规则（regex + ban，「探花招募族」）的正文。
#: 按生产库实测抄录（63 字符，指纹 ``e9d32f1e00b0e9e2``）——**不是审计报告里那句截断转述**。
PRODUCTION_RULE_6_PATTERN = (
    r"(?i)(招募?探花|收探花|探花(视频|资源)|提供设备[^\n。]{0,12}(收|买|收购|结算)|(收|买)探花视频)"
)

_LEGACY_RULES_DDL = (
    "CREATE TABLE moderation_rules ("
    "id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL, "
    "rule_type VARCHAR(32) NOT NULL, pattern TEXT NOT NULL DEFAULT '', "
    "action VARCHAR(32) NOT NULL DEFAULT 'warn', "
    "enabled BOOLEAN NOT NULL DEFAULT 1)"
)

_SCAN_SCOPE_COLUMN = "scan_scope VARCHAR(32) NOT NULL DEFAULT 'message'"


def expected_fingerprint(pattern: str) -> str:
    """实现应当采用的指纹口径：``strip()`` + ``casefold()`` + sha256 前 16 位。"""

    normalized = str(pattern or "").strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _row_values(
    rule_id: int,
    rule_type: str,
    pattern: str,
    action: str,
    *,
    group_id: int = -100,
    enabled: int = 1,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "group_id": group_id,
        "rule_type": rule_type,
        "pattern": pattern,
        "action": action,
        "enabled": enabled,
    }


class _LegacyRuleCase(unittest.IsolatedAsyncioTestCase):
    """一套最小老库（``moderation_rules`` 没有 ``scan_scope`` 列）。"""

    rows: tuple[dict[str, object], ...] = ()

    async def _engine(self):
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.execute(text(_LEGACY_RULES_DDL))
            for row in self.rows:
                await conn.execute(
                    text(
                        "INSERT INTO moderation_rules "
                        "(id, group_id, rule_type, pattern, action, enabled) "
                        "VALUES (:id, :group_id, :rule_type, :pattern, :action, :enabled)"
                    ),
                    row,
                )
        return engine

    async def _ensure_column(self, conn) -> None:
        """与 ``init_db`` 的迁移顺序一致：先补列（老行拿默认 'message'），再升级。"""

        await _sqlite_ensure_column(
            conn, "moderation_rules", "scan_scope", _SCAN_SCOPE_COLUMN
        )

    async def _scopes(self, conn) -> dict[int, str]:
        rows = (
            await conn.execute(
                text("SELECT id, scan_scope FROM moderation_rules ORDER BY id")
            )
        ).all()
        return {int(row[0]): str(row[1]) for row in rows}


class ProductionRuleUpgradeTests(_LegacyRuleCase):
    """① 确实是那条生产规则 → 照旧升级（默认行为不变）。"""

    rows = (
        _row_values(1, "keyword", "秒杀", "delete"),
        _row_values(5, "regex", "秒杀|优惠券", "delete"),
        _row_values(6, "regex", PRODUCTION_RULE_6_PATTERN, "ban"),
        _row_values(7, "regex", "探花", "warn"),
    )

    async def test_matching_rule_is_still_upgraded(self) -> None:
        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            with self.assertLogs("bot.db.engine", level="INFO") as logs:
                changed = await _sqlite_upgrade_moderation_rule_scan_scopes(conn)
            scopes = await self._scopes(conn)

        self.assertEqual(changed, 1)
        self.assertEqual(scopes[6], "message+quote+vision")
        for rule_id in (1, 5, 7):
            self.assertEqual(scopes[rule_id], "message", rule_id)
        self.assertTrue(
            any("upgraded 1 moderation rule" in line for line in logs.output),
            logs.output,
        )

    async def test_upgrade_is_idempotent(self) -> None:
        """③ 重复启动只改一次（第二次及以后 rowcount 为 0）。"""

        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 1
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 0
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 0
            )
            scope = (
                await conn.execute(
                    text("SELECT scan_scope FROM moderation_rules WHERE id = 6")
                )
            ).scalar_one()

        self.assertEqual(scope, "message+quote+vision")

    async def test_manually_configured_scope_is_not_overwritten(self) -> None:
        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            await conn.execute(
                text("UPDATE moderation_rules SET scan_scope = 'message+quote' WHERE id = 6")
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 0
            )
            scope = (
                await conn.execute(
                    text("SELECT scan_scope FROM moderation_rules WHERE id = 6")
                )
            ).scalar_one()

        self.assertEqual(scope, "message+quote")


class ForeignRuleSixIsNotTouchedTests(_LegacyRuleCase):
    """② id 相同但内容不同 → 绝不升级，并且要留下可解释的 INFO 日志。"""

    rows = (
        _row_values(1, "keyword", "秒杀", "delete"),
        # id=6，但这是**别的**部署里的一条完全不同的规则。
        _row_values(6, "regex", r"赌博|博彩|代开发票", "ban"),
        _row_values(7, "regex", "探花", "warn"),
    )

    async def test_same_id_different_pattern_is_not_upgraded(self) -> None:
        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            with self.assertLogs("bot.db.engine", level="INFO") as logs:
                changed = await _sqlite_upgrade_moderation_rule_scan_scopes(conn)
            scopes = await self._scopes(conn)

        self.assertEqual(changed, 0, "别人的规则不能被静默放大扫描范围")
        self.assertEqual(scopes[6], "message")
        joined = "\n".join(logs.output)
        self.assertIn("跳过审核规则扫描范围升级", joined)
        self.assertIn("rule_id=6", joined)
        # 跳过原因要说清楚是"内容指纹对不上"，并且只出现指纹、不出现规则正文。
        self.assertIn("指纹", joined)
        self.assertIn(expected_fingerprint(r"赌博|博彩|代开发票"), joined)
        self.assertNotIn("赌博", joined)

    async def test_case_only_difference_still_matches(self) -> None:
        """本地匹配一律 IGNORECASE，大小写 / 首尾空白差异不算另一条规则。"""

        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            await conn.execute(
                text("UPDATE moderation_rules SET pattern = :pattern WHERE id = 6"),
                # 生产真值的大小写 + 首尾空白变体（`(?i)` → `(?I)`，其余为中文/符号，casefold 后同一）
                {"pattern": f"  {PRODUCTION_RULE_6_PATTERN.upper()}  "},
            )
            self.assertEqual(
                await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 1
            )


class ForeignRuleSixWithOtherActionTests(_LegacyRuleCase):
    """动作对不上（id=6 是一条 warn 规则）同样跳过。"""

    rows = (_row_values(6, "regex", PRODUCTION_RULE_6_PATTERN, "warn"),)

    async def test_same_id_different_action_is_not_upgraded(self) -> None:
        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            with self.assertLogs("bot.db.engine", level="INFO") as logs:
                changed = await _sqlite_upgrade_moderation_rule_scan_scopes(conn)
            scope = (
                await conn.execute(
                    text("SELECT scan_scope FROM moderation_rules WHERE id = 6")
                )
            ).scalar_one()

        self.assertEqual(changed, 0)
        self.assertEqual(scope, "message")
        joined = "\n".join(logs.output)
        self.assertIn("跳过审核规则扫描范围升级", joined)
        self.assertIn("同类型同动作", joined)


class FingerprintContractTests(unittest.TestCase):
    """指纹口径本身：不折叠中间空白（宁可对不上，也不要误升级别人）。"""

    def test_case_and_outer_whitespace_are_ignored(self) -> None:
        base = expected_fingerprint("探花|招募")
        self.assertEqual(base, expected_fingerprint(" 探花|招募 "))
        self.assertEqual(len(base), 16)

    def test_inner_whitespace_is_significant(self) -> None:
        self.assertNotEqual(expected_fingerprint("a  b"), expected_fingerprint("a b"))

    def test_content_changes_change_the_fingerprint(self) -> None:
        self.assertNotEqual(
            expected_fingerprint("探花|招募"), expected_fingerprint("探花招募")
        )


class MigrationSwitchTests(_LegacyRuleCase):
    """显式开关：默认开（= 今天），关掉时一段写入都不做。"""

    rows = (_row_values(6, "regex", PRODUCTION_RULE_6_PATTERN, "ban"),)

    async def test_switch_default_is_on(self) -> None:
        """默认值必须让生产与今天完全一致。"""

        self.assertTrue(Settings(_env_file=None).legacy_scan_scope_migration_enabled)
        self.assertFalse(
            Settings(
                _env_file=None, legacy_scan_scope_migration_enabled="false"
            ).legacy_scan_scope_migration_enabled
        )

    async def test_disabled_switch_writes_nothing(self) -> None:
        engine = await self._engine()
        async with engine.begin() as conn:
            await self._ensure_column(conn)
            with self.assertLogs("bot.db.engine", level="INFO") as logs:
                changed = await _sqlite_upgrade_moderation_rule_scan_scopes(
                    conn, enabled=False
                )
            scope = (
                await conn.execute(
                    text("SELECT scan_scope FROM moderation_rules WHERE id = 6")
                )
            ).scalar_one()

        self.assertEqual(changed, 0)
        self.assertEqual(scope, "message")
        self.assertIn("开关关闭", "\n".join(logs.output))

    async def test_init_db_passes_the_switch_through(self) -> None:
        """``init_db`` 是启动时的真实入口，开关必须一路传到底。"""

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            engine, session_factory = await init_db(
                f"sqlite+aiosqlite:///{path}",
                legacy_scan_scope_migration_enabled=False,
            )
            async with session_factory() as session:
                from bot.db.models import Group, ModerationRule

                session.add(Group(id=-100, title="t"))
                session.add(
                    ModerationRule(
                        id=6,
                        group_id=-100,
                        rule_type="regex",
                        pattern=PRODUCTION_RULE_6_PATTERN,
                        action="ban",
                        enabled=True,
                    )
                )
                await session.commit()

            async with engine.begin() as conn:
                await _sqlite_upgrade_moderation_rule_scan_scopes(
                    conn, enabled=False
                )
                scope = (
                    await conn.execute(
                        text("SELECT scan_scope FROM moderation_rules WHERE id = 6")
                    )
                ).scalar_one()
            self.assertEqual(scope, "message")

            # 同一份代码，默认（不传开关）就照旧升级。
            async with engine.begin() as conn:
                self.assertEqual(
                    await _sqlite_upgrade_moderation_rule_scan_scopes(conn), 1
                )
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
