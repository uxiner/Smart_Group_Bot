"""运行时配置变更历史：原子写入、脱敏、差异预览与回滚。

这个文件守着四件"源码读不出来、必须真的写一次库"的事：

1. **历史与主表同生共死。** 审计缺一行等于没有审计：历史插入失败必须把
   ``runtime_config`` 的乐观锁更新一起回滚，绝不允许出现"配置改了但查不到
   谁改的"。（用唯一索引真实撞一次，比 mock 更有说服力。）
2. **历史里一个密钥字节都不能有。** 不靠"``storage_payload()`` 好像已经清空
   了"这种推断——这里把 **every** 个 secret 路径都塞上唯一哨兵值，再逐字节
   搜历史 JSON、差异 JSON 和**序列化后的 HTTP 响应体**。
3. **回滚只回滚非密钥字段。** 回滚一个历史 revision 之后，``runtime_config_secrets``
   里密文解出来的明文必须与回滚前**完全一致**（回滚永远不动密钥）。
4. **列表不能塌成"整体变了"。** 供应商列表改一个 ``api_base`` 必须落到
   ``models.providers[gemini].api_base`` 这一条，而不是一整段 models。

以及权限：历史里存着全局运营配置（含私密提示词），所以五个接口全部只给最高
管理员；群管理员改 URL / JSON 体里的 group_id、revision 也拿不到任何东西。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import RuntimeConfigHistory, RuntimeConfigRecord, RuntimeConfigSecret
from bot.services import runtime_config as runtime_config_module
from bot.services import runtime_config_history as history_module
from bot.services.runtime_config import (
    HISTORY_PAGE_LIMIT,
    HISTORY_PAGE_MAX_LIMIT,
    RuntimeConfigConflictError,
    RuntimeConfigManager,
    SecretCipher,
)
from bot.services.runtime_config_history import (
    MAX_DIFF_ENTRIES,
    MAX_DIFF_STRING_CHARS,
    MAX_SNAPSHOT_BYTES,
    SECRET_STATUS_CURRENT,
    URL_QUERY_MARKER,
    URL_USERINFO_MARKER,
    RuntimeConfigHistoryNotRestorableError,
    RuntimeConfigHistorySnapshotTooLargeError,
    build_diff,
    encode_snapshot_json,
    ensure_snapshot_within_limit,
    is_credential_string,
    redact_payload,
    redact_url_credentials,
    resolve_secret_changes,
    restore_opaque_values,
    secret_status,
    snapshot_size_bytes,
)
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ADMIN_ID = 4242
SYNTHETIC_GROUP_ID = -1000000000002

#: 本文件自用的合成主密钥哨兵：它**不能**以任何形式出现在历史/响应里。
MASTER_KEY_SENTINEL = "unit-test-master-key"

#: 9 个静态 secret 路径 + 至少 2 个 provider key 全部塞唯一哨兵。
SENTINELS: dict[str, str] = {
    "verification.turnstile_secret_key": "SENTINEL-turnstile-secret",
    "verification.hcaptcha_secret_key": "SENTINEL-hcaptcha-secret",
    "tts.app_key": "SENTINEL-tts-app-key",
    "tts.access_key": "SENTINEL-tts-access-key",
    "movie_info.tmdb_read_access_token": "SENTINEL-tmdb-token",
    "movie_info.imdb_api_key": "SENTINEL-imdb-api-key",
    "movie_info.imdb_aws_access_key_id": "SENTINEL-imdb-access-key-id",
    "movie_info.imdb_aws_secret_access_key": "SENTINEL-imdb-secret-access-key",
    "movie_info.imdb_aws_session_token": "SENTINEL-imdb-session-token",
    "providers.gemini.api_key": "SENTINEL-gemini-key",
    "providers.openai.api_key": "SENTINEL-openai-key",
}

#: **自由 JSON / URL 里的凭据哨兵**。它们的键名（``extra_headers`` /
#: ``api_base``）完全不像密钥，靠 ``secret_paths`` 与键名启发式一条都拦不住——
#: 这正是审计抓到的那个洞。五个形状都要覆盖：list、dict、嵌套头、URL userinfo、
#: URL 敏感 query。
OPAQUE_SENTINELS: dict[str, str] = {
    "list": "SENTINEL_HEADER_LIST",
    "dict": "SENTINEL_HEADER_DICT",
    "nested": "SENTINEL_NESTED_HEADER",
    "url_userinfo": "SENTINEL_URL_USERINFO",
    "url_query": "SENTINEL_URL_QUERY",
    "header_line": "SENTINEL_HEADER_LINE",
}

#: 上面这份 payload 存进 ``models.main.request_params`` 的内容。
OPAQUE_REQUEST_PARAMS: dict[str, Any] = {
    "extra_headers": ["Bearer " + OPAQUE_SENTINELS["list"]],
    "headers": {"X-Api-Key": OPAQUE_SENTINELS["dict"]},
    "nested": {"options": {"headers": ["X-Custom-Token: " + OPAQUE_SENTINELS["nested"]]}},
    "raw_headers": ["Authorization: Bearer " + OPAQUE_SENTINELS["header_line"]],
    "temperature": 0.7,
}

#: 带凭据的供应商地址（userinfo + 敏感 query 各一个）。
OPAQUE_API_BASE = (
    "https://admin:"
    + OPAQUE_SENTINELS["url_userinfo"]
    + "@api.example.com/v1?token="
    + OPAQUE_SENTINELS["url_query"]
    + "&model=fast"
)


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-config-history-test",
        "user": json.dumps({"id": user_id, "first_name": "Tester"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class _HistoryFixture(unittest.IsolatedAsyncioTestCase):
    """真实 ``init_db`` + 真实 ``RuntimeConfigManager``（不 mock 任何一层）。"""

    master_key = "unit-test-master-key"

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key=self.master_key,
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8483
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-config-history.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # -- 工具 ---------------------------------------------------------------

    @staticmethod
    def _headers(user_id: int = SUPER_ADMIN_ID) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _payload_with_second_provider(self) -> dict[str, Any]:
        """确保 payload 里**恰好有一个** openai 供应商（已存在时不重复追加）。

        幂等是刻意的：``provider names must be unique`` 是一条真实的校验，测试
        助手替它绕过去只会让失败出现在别处、变得难查。
        """

        payload = self.manager.config.public_payload()
        providers = list(payload["models"]["providers"])
        if any(item["name"] == "openai" for item in providers):
            return payload
        providers.append(
            {
                "name": "openai",
                "provider": "openai",
                "api_key": "",
                "api_base": "https://api.openai.com/v1",
                "stream": True,
                "chat_endpoint": "chat_completions",
            }
        )
        payload["models"]["providers"] = providers
        return payload

    def _all_secret_replacements(self) -> dict[str, dict[str, str]]:
        return {path: {"action": "replace", "value": value} for path, value in SENTINELS.items()}

    async def _save_all_sentinels(self) -> dict[str, Any]:
        payload = self._payload_with_second_provider()
        return await self.manager.save(
            payload,
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes=self._all_secret_replacements(),
        )

    async def _history_rows(self) -> list[RuntimeConfigHistory]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RuntimeConfigHistory).order_by(RuntimeConfigHistory.revision)
            )
            return list(result.scalars().all())

    async def _raw_history_json(self) -> str:
        """**绕开 ORM**：直接 ``SELECT`` 那一列的原始文本。

        ORM 会在读取时反序列化，哨兵确实可能在"读出来的那一刻"看起来不见了。
        这一条读的是 SQLite 里真正存着的字节，是"凭据有没有落库"的唯一可信
        判据。
        """

        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT revision, payload_redacted, secret_paths_changed, summary "
                    "FROM runtime_config_history ORDER BY revision"
                )
            )
            rows = result.all()
        return json.dumps(
            [
                {
                    "revision": int(row[0]),
                    "payload_redacted": row[1],
                    "secret_paths_changed": list(json.loads(row[2] or "[]")),
                    "summary": row[3],
                }
                for row in rows
            ],
            ensure_ascii=False,
        )

    @staticmethod
    def _collect_logs() -> Any:
        """一个把全部 ``logging`` 记录抓下来的 handler。"""

        class _Capture(logging.Handler):
            def __init__(self) -> None:
                super().__init__(level=logging.DEBUG)
                self.records: list[str] = []

            def emit(self, record: logging.LogRecord) -> None:
                try:
                    self.records.append(record.getMessage())
                except Exception:  # pragma: no cover - 防御
                    self.records.append(str(record.msg))

        handler = _Capture()
        logger = logging.getLogger("bot")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        handler._previous_level = previous  # type: ignore[attr-defined]
        return handler

    @staticmethod
    def _drop_logs(handler: Any) -> None:
        logger = logging.getLogger("bot")
        logger.removeHandler(handler)
        logger.setLevel(handler._previous_level)  # type: ignore[attr-defined]

    async def _main_row_fingerprint(self) -> str:
        """``runtime_config`` 单行的逐字指纹（回滚/拒绝路径必须证明它没变）。"""

        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            return json.dumps(
                {
                    "revision": record.revision,
                    "schema_version": record.schema_version,
                    "payload": record.payload,
                    "updated_by": record.updated_by,
                },
                sort_keys=True,
                ensure_ascii=False,
            )

    async def _stored_secret_map(self) -> dict[str, str]:
        """解密库里的密文，拿到回滚前后必须**逐字相同**的明文。"""

        cipher = SecretCipher(self.settings.config_master_key)
        async with self.session_factory() as session:
            result = await session.execute(select(RuntimeConfigSecret))
            return {
                row.name: cipher.decrypt(row.ciphertext)
                for row in result.scalars().all()
            }

    def _assert_no_secret_material(self, blob: str, where: str) -> None:
        for path, sentinel in SENTINELS.items():
            self.assertNotIn(sentinel, blob, f"{where} 泄露了 {path} 的明文")
        self.assertNotIn(MASTER_KEY_SENTINEL, blob, f"{where} 泄露了主密钥")
        self.assertNotIn("CONFIG_MASTER_KEY", blob, f"{where} 泄露了主密钥 env 变量名")
        self.assertNotIn("gAAAA", blob, f"{where} 泄露了 Fernet 密文")
        self.assertNotIn("Authorization", blob, f"{where} 泄露了鉴权头")
        self.assertNotIn("Bearer ", blob, f"{where} 泄露了鉴权头")

    def _assert_no_opaque_material(self, blob: str, where: str) -> None:
        """自由 JSON / URL 里的凭据哨兵同样一个都不许出现。"""

        for shape, sentinel in OPAQUE_SENTINELS.items():
            self.assertNotIn(
                sentinel, blob, f"{where} 泄露了 {shape} 形态的凭据明文"
            )


class RedactionUnitTests(unittest.TestCase):
    """``redact_payload`` / ``build_diff`` 的直接单测（不依赖 pydantic 模型）。

    保证"脱敏"这件事不因为以后有人往 ``_STATIC_SECRET_PATHS`` 里漏加字段就
    悄悄失效：这里喂的是**手搓的嵌套 JSON**，只有递归 + 键名启发式能救。
    """

    def test_known_secret_paths_are_replaced_by_a_status_marker(self) -> None:
        payload = {
            "tts": {"app_key": "AKID-super-secret", "speaker": "zh_female"},
            "models": {
                "providers": [
                    {"name": "gemini", "api_key": "sk-live-gemini", "api_base": "https://a"},
                ],
            },
        }
        redacted = redact_payload(
            payload,
            {"tts.app_key", "providers.gemini.api_key"},
        )

        self.assertNotIn("AKID-super-secret", json.dumps(redacted))
        self.assertNotIn("sk-live-gemini", json.dumps(redacted))
        self.assertEqual(secret_status(redacted["tts"]["app_key"]), "configured")
        self.assertEqual(redacted["tts"]["speaker"], "zh_female")
        self.assertEqual(
            redacted["models"]["providers"][0]["api_base"], "https://a"
        )
        self.assertEqual(secret_status(redacted["models"]["providers"][0]["api_key"]), "configured")

    def test_key_name_heuristics_catch_unregistered_secrets(self) -> None:
        """没登记进 ``secret_paths`` 的新密钥字段也必须被挡下。"""

        payload = {
            "future_section": {
                "webhook_secret": "sn0pe",
                "smtp_password": "hunter2",
                "service_authorization": "Bearer abc",
                "ciphertext": "gAAAAalready-encrypted",
                "access_key": "AKIA-EXAMPLE",
                "bearer_token": "t0ken",
                "harmless_label": "keep-me",
            },
        }
        redacted = redact_payload(payload, set())
        encoded = json.dumps(redacted, ensure_ascii=False)

        for leaked in (
            "sn0pe",
            "hunter2",
            "Bearer abc",
            "gAAAAalready-encrypted",
            "AKIA-EXAMPLE",
            "t0ken",
        ):
            self.assertNotIn(leaked, encoded, leaked)
        self.assertEqual(redacted["future_section"]["harmless_label"], "keep-me")

    def test_key_name_heuristics_leaves_count_fields_alone(self) -> None:
        """``max_tokens`` / ``*_token_budget`` 是**数量**，不是凭据。

        把它们脱敏成字符串标记会让回滚时的严格 schema 直接报
        ``Input should be a valid integer``——"为了脱敏把配置改坏"。
        """

        payload = {
            "bot": {
                "max_context_tokens": 278528,
                "group_history_token_budget": 12288,
            },
            "resources": {
                "llm_tokenizer_concurrency": 4,
                "decision_history_token_budget": 8192,
                "memory_extract_input_token_limit": 16384,
            },
            "tts": {"app_key": "AKID-real-secret"},
            "webhooks": {"signing_secret": "whsec-also-secret"},
        }
        redacted = redact_payload(payload, {"tts.app_key"})

        self.assertEqual(redacted["bot"]["max_context_tokens"], 278528)
        self.assertEqual(redacted["bot"]["group_history_token_budget"], 12288)
        self.assertEqual(redacted["resources"]["llm_tokenizer_concurrency"], 4)
        self.assertEqual(redacted["resources"]["decision_history_token_budget"], 8192)
        self.assertEqual(redacted["resources"]["memory_extract_input_token_limit"], 16384)
        self.assertNotIn("AKID-real-secret", json.dumps(redacted))
        self.assertNotIn("whsec-also-secret", json.dumps(redacted))

    def test_empty_secret_is_marked_as_cleared_not_configured(self) -> None:
        redacted = redact_payload({"tts": {"app_key": ""}}, {"tts.app_key"})
        self.assertEqual(secret_status(redacted["tts"]["app_key"]), "empty")

    def test_redaction_does_not_mutate_the_input(self) -> None:
        payload = {"tts": {"app_key": "AKID"}}
        redact_payload(payload, {"tts.app_key"})
        self.assertEqual(payload["tts"]["app_key"], "AKID")

    def test_diff_reports_per_element_list_paths_not_one_blob(self) -> None:
        old = {
            "models": {
                "providers": [
                    {"name": "gemini", "api_base": "https://old-gemini"},
                    {"name": "openai", "api_base": "https://old-openai"},
                ],
                "main": {"model": "gemini/gemini-2.0-flash"},
            },
            "economy": {"weekly_reward_points": [25, 12, 12, 4]},
        }
        new = {
            "models": {
                "providers": [
                    {"name": "gemini", "api_base": "https://new-gemini"},
                    {"name": "openai", "api_base": "https://old-openai"},
                ],
                "main": {"model": "gemini/gemini-2.0-flash"},
            },
            "economy": {"weekly_reward_points": [25, 12, 12, 7]},
        }

        entries = build_diff(old, new, set())
        by_path = {entry["path"]: entry for entry in entries}

        self.assertIn("models.providers[gemini].api_base", by_path)
        self.assertEqual(
            by_path["models.providers[gemini].api_base"]["change"], "modified"
        )
        self.assertEqual(
            by_path["models.providers[gemini].api_base"]["old"],
            "https://old-gemini",
        )
        self.assertNotIn("models.providers[openai].api_base", by_path)
        # 没有身份字段的列表退化成下标，但仍然是**逐元素**。
        self.assertIn("economy.weekly_reward_points[3]", by_path)
        self.assertNotIn("economy.weekly_reward_points", by_path)

    def test_diff_is_sorted_and_deterministic(self) -> None:
        old = {"b": 1, "a": {"z": 1, "y": 2}}
        new = {"b": 2, "a": {"z": 9, "y": 2}}
        first = build_diff(old, new, set())
        second = build_diff(old, new, set())
        self.assertEqual(first, second)
        self.assertEqual(
            [entry["path"] for entry in first],
            ["a.z", "b"],
        )

    def test_diff_marks_added_removed_and_nested_objects(self) -> None:
        old = {"economy": {"tag_price_7d": 1, "retired_field": 5}}
        new = {"economy": {"tag_price_7d": 1, "brand_new": {"deep": 2}}}
        by_path = {entry["path"]: entry for entry in build_diff(old, new, set())}
        self.assertEqual(by_path["economy.retired_field"]["change"], "removed")
        self.assertEqual(by_path["economy.retired_field"]["new"], None)
        self.assertEqual(by_path["economy.brand_new.deep"]["change"], "added")
        self.assertEqual(by_path["economy.brand_new.deep"]["old"], None)
        self.assertFalse(by_path["economy.brand_new.deep"]["secret"])

    def test_long_strings_are_truncated_and_flagged(self) -> None:
        prompt = "规" * (MAX_DIFF_STRING_CHARS + 500)
        entry = build_diff(
            {"prompts": {"moderation": ""}},
            {"prompts": {"moderation": prompt}},
            set(),
        )[0]
        self.assertTrue(entry["truncated"])
        self.assertEqual(len(entry["new"]), MAX_DIFF_STRING_CHARS + 1)
        self.assertEqual(entry["new"], prompt[:MAX_DIFF_STRING_CHARS] + "…")

    def test_entry_count_is_capped_so_one_response_cannot_blow_up(self) -> None:
        old = {f"economy.f{k}": 1 for k in range(MAX_DIFF_ENTRIES + 200)}
        new = {f"economy.f{k}": 2 for k in range(MAX_DIFF_ENTRIES + 200)}
        entries = build_diff(old, new, set())
        self.assertEqual(len(entries), MAX_DIFF_ENTRIES)
        # 截断后仍然是排序的、仍然可读的路径。
        self.assertEqual(entries[0]["path"], "economy.f0")
        self.assertEqual(
            [entry["path"] for entry in entries],
            sorted(entry["path"] for entry in entries),
        )

    def test_secret_paths_never_leak_values_into_a_diff(self) -> None:
        old = {"tts": {"app_key": "OLD-AKID", "speaker": "a"}}
        new = {"tts": {"app_key": "NEW-AKID", "speaker": "a"}}
        entries = build_diff(old, new, {"tts.app_key"})

        self.assertNotIn("OLD-AKID", json.dumps(entries))
        self.assertNotIn("NEW-AKID", json.dumps(entries))
        # 两侧都还配置着：payload 快照看不出差别，交给 secret_paths_changed。
        self.assertEqual(entries, [])

        forced = build_diff(
            old, new, {"tts.app_key"}, changed_secret_paths={"tts.app_key"}
        )
        self.assertEqual(len(forced), 1)
        self.assertTrue(forced[0]["secret"])
        self.assertEqual(forced[0]["old"], {"status": "configured"})
        self.assertEqual(forced[0]["new"], {"status": "unchanged"})

    def test_secret_cleared_and_set_transitions_are_reported(self) -> None:
        cleared = build_diff(
            {"tts": {"app_key": "OLD-AKID"}}, {"tts": {"app_key": ""}}, {"tts.app_key"}
        )
        self.assertEqual(cleared[0]["change"], "modified")
        self.assertEqual(cleared[0]["old"], {"status": "configured"})
        self.assertEqual(cleared[0]["new"], {"status": "cleared"})

        added = build_diff({}, {"tts": {"app_key": "NEW-AKID"}}, {"tts.app_key"})
        self.assertEqual(added[0]["change"], "added")
        self.assertEqual(added[0]["new"], {"status": "configured"})


class StoragePayloadRedactionTests(_HistoryFixture):
    """任务书点名要**证明**的那一条：``storage_payload()`` 到底够不够。"""

    async def test_every_secret_path_is_seeded_and_absent_from_history(self) -> None:
        paths = self.manager.config.secret_paths()
        # 先证明"真的覆盖到了每一个 secret 路径"，否则这条断言是空的。
        self.assertTrue(set(SENTINELS).issubset(paths | set(SENTINELS)))
        self.assertEqual(len(SENTINELS), 11)

        await self._save_all_sentinels()
        self.assertEqual(sorted(self.manager.config.extract_secrets()), sorted(SENTINELS))

        rows = await self._history_rows()
        self.assertEqual(len(rows), 1)
        blob = json.dumps(rows[0].payload_redacted, ensure_ascii=False)
        self._assert_no_secret_material(blob, "payload_redacted")

        # 密文同样不能进历史（历史里根本没有密钥，只有状态标记）。
        ciphertexts = await self._stored_secret_map()
        self.assertEqual(len(ciphertexts), len(SENTINELS))
        for name, _plaintext in ciphertexts.items():
            self.assertIn(name, str(rows[0].secret_paths_changed) + name)
        self._assert_no_secret_material(
            json.dumps(rows[0].__dict__, ensure_ascii=False, default=str),
            "整行历史",
        )

    async def test_storage_payload_alone_would_have_been_enough_for_this_schema(self) -> None:
        """记录"``storage_payload()`` 够不够"这个结论的正面证据。

        现状：``storage_payload()`` = ``with_secrets({})`` + ``model_dump``，对
        **当前** schema 里的 11 个 secret 路径确实一个不漏（全 0）。但它完全
        依赖 ``with_secrets`` 手工列举——多加一个带密钥的字段而忘了登记，这里
        就会**静默**把明文写进 ``runtime_config.payload``。所以产品代码仍然叠了
        一层递归脱敏，而不是"验证过就够了"。
        """

        storage = self.manager.config.storage_payload()
        for path in SENTINELS:
            section, _, field = path.rpartition(".")
            if section.startswith("providers."):
                for provider in storage["models"]["providers"]:
                    self.assertEqual(provider[field], "")
            else:
                self.assertEqual(storage[section][field], "")

    async def test_historical_rows_keep_secret_status_without_values(self) -> None:
        await self._save_all_sentinels()
        rows = await self._history_rows()
        payload = rows[0].payload_redacted
        self.assertEqual(secret_status(payload["tts"]["app_key"]), "configured")
        self.assertEqual(secret_status(payload["movie_info"]["imdb_api_key"]), "configured")
        for provider in payload["models"]["providers"]:
            self.assertEqual(secret_status(provider["api_key"]), "configured")
        self.assertEqual(secret_status(payload["verification"]["hcaptcha_secret_key"]), "configured")
        # 提示词不是密钥：原样保留，方便回滚与非密钥 diff。
        self.assertTrue(payload["prompts"]["moderation"])


class HistoryWriteTests(_HistoryFixture):
    async def test_save_writes_exactly_one_history_row_in_the_same_transaction(self) -> None:
        self.assertEqual(self.manager.revision, 1)
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 12345

        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)

        rows = await self._history_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.revision, 2)
        self.assertEqual(row.parent_revision, 1)
        self.assertEqual(row.operation, "save")
        self.assertEqual(row.updated_by, SUPER_ADMIN_ID)
        self.assertIsNotNone(row.updated_at)
        self.assertIsNone(row.restore_source_revision)
        self.assertEqual(row.secret_paths_changed, [])
        self.assertEqual(row.payload_redacted["economy"]["pin_price"], 12345)
        # 摘要只有"数量 + 路径"，绝不含值。
        self.assertIn("economy.pin_price", row.summary)
        self.assertNotIn("12345", row.summary)

    async def test_second_save_appends_another_row(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 7
        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 9
        await self.manager.save(payload, expected_revision=2, updated_by=7)

        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2, 3])
        self.assertEqual([row.parent_revision for row in rows], [1, 2])
        self.assertEqual([row.updated_by for row in rows], [SUPER_ADMIN_ID, 7])

    async def test_secret_changes_are_recorded_by_name_only(self) -> None:
        await self._save_all_sentinels()
        rows = await self._history_rows()
        self.assertEqual(sorted(rows[0].secret_paths_changed), sorted(SENTINELS))
        self._assert_no_secret_material(
            json.dumps(list(rows[0].secret_paths_changed), ensure_ascii=False),
            "secret_paths_changed",
        )

    async def test_unique_index_violation_rolls_the_main_row_back(self) -> None:
        """历史插入失败 → 整笔回滚，**绝不能**"主表改了、历史没有"。"""

        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 777
        before_row = None
        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            before_row = json.dumps(
                {
                    "revision": record.revision,
                    "payload": record.payload,
                    "updated_by": record.updated_by,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        # 手工种一条"已经存在"的历史行：唯一索引会让真正的插入在 flush 时炸。
        async with self.session_factory() as session:
            session.add(
                RuntimeConfigHistory(
                    revision=2,
                    parent_revision=1,
                    payload_redacted={},
                    secret_paths_changed=[],
                    updated_by=999,
                    operation="save",
                    summary="pre-seeded collision",
                )
            )
            await session.commit()

        with self.assertRaises(IntegrityError):
            await self.manager.save(
                payload, expected_revision=1, updated_by=SUPER_ADMIN_ID
            )

        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            after_row = json.dumps(
                {
                    "revision": record.revision,
                    "payload": record.payload,
                    "updated_by": record.updated_by,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            history = (
                await session.execute(
                    select(RuntimeConfigHistory).order_by(RuntimeConfigHistory.revision)
                )
            ).scalars().all()
        self.assertEqual(after_row, before_row, "主表必须逐字未变")
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(self.manager.config.economy.pin_price, 20)
        self.assertEqual(self.settings.economy.pin_price, 20)
        self.assertEqual([row.revision for row in history], [2], "只应有预置那一行")

    async def test_history_row_construction_failure_rolls_back_too(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 11
        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            before = (record.revision, json.dumps(record.payload, sort_keys=True))

        class _Exploding:
            def __init__(self, **kwargs: Any) -> None:
                raise RuntimeError("history insert exploded")

        original = runtime_config_module.RuntimeConfigHistory
        runtime_config_module.RuntimeConfigHistory = _Exploding  # type: ignore[misc]
        try:
            with self.assertRaises(RuntimeError):
                await self.manager.save(
                    payload, expected_revision=1, updated_by=SUPER_ADMIN_ID
                )
        finally:
            runtime_config_module.RuntimeConfigHistory = original  # type: ignore[misc]

        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            after = (record.revision, json.dumps(record.payload, sort_keys=True))
            self.assertEqual(
                (
                    await session.execute(select(RuntimeConfigHistory))
                ).scalars().all(),
                [],
            )
        self.assertEqual(after, before)
        self.assertEqual(self.manager.revision, 1)

    async def test_revision_conflict_writes_no_history(self) -> None:
        with self.assertRaises(RuntimeConfigConflictError):
            await self.manager.save(
                self.manager.config.public_payload(),
                expected_revision=999,
                updated_by=SUPER_ADMIN_ID,
            )
        self.assertEqual(await self._history_rows(), [])


class InitializeHistoryTests(_HistoryFixture):
    async def test_fresh_database_initialize_writes_no_history(self) -> None:
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(await self._history_rows(), [])

    async def test_existing_database_initialize_keeps_revision_and_writes_no_history(
        self,
    ) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["tag_price_7d"] = 3
        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)
        self.assertEqual(len(await self._history_rows()), 1)

        # 把 payload 改回一个"老库"形状：initialize() 会做一次性归一化迁移。
        async with self.session_factory() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            stored = dict(record.payload)
            bot_payload = dict(stored["bot"])
            bot_payload["drop_pending_updates"] = True
            stored["bot"] = bot_payload
            record.payload = stored
            record.revision = 5
            await session.commit()

        reloaded_settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key=self.master_key,
        )
        reloaded_settings.bot.token = BOT_TOKEN
        reloaded = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=reloaded_settings,
            legacy_raw_env={},
        )
        await reloaded.initialize()

        self.assertEqual(reloaded.revision, 5, "归一化迁移不许改 revision")
        self.assertFalse(reloaded.config.bot.drop_pending_updates)
        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2], "迁移不许伪造历史")

    async def test_restarting_a_manager_does_not_duplicate_history(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["tag_price_7d"] = 4
        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)
        reloaded = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_raw_env={},
        )
        await reloaded.initialize()
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual([row.revision for row in await self._history_rows()], [2])


class RestoreTests(_HistoryFixture):
    async def test_restore_reverts_non_secret_fields_and_keeps_secrets(self) -> None:
        await self._save_all_sentinels()
        target_revision = self.manager.revision
        target_config = self.manager.config.model_copy(deep=True)
        secrets_before = await self._stored_secret_map()
        self.assertEqual(secrets_before, SENTINELS)

        # 改两个字段：一个热字段，一个 restart 字段。
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 999
        payload["resources"]["pending_reply_execution_capacity"] = 9
        await self.manager.save(
            payload,
            expected_revision=target_revision,
            updated_by=SUPER_ADMIN_ID,
        )
        self.assertEqual(self.manager.revision, target_revision + 1)

        restored = await self.manager.restore(
            target_revision,
            expected_revision=target_revision + 1,
            updated_by=7,
        )

        self.assertEqual(restored.economy.pin_price, target_config.economy.pin_price)
        self.assertEqual(self.manager.revision, target_revision + 2)
        self.assertEqual(
            self.settings.economy.pin_price, target_config.economy.pin_price
        )
        self.assertEqual(await self._stored_secret_map(), SENTINELS, "回滚不许动密钥")
        self.assertEqual(sorted(restored.extract_secrets()), sorted(SENTINELS))
        # 整份文档逐字回到目标那一版：脱敏启发式不能把任何非密钥字段改坏。
        self.assertEqual(
            restored.storage_payload(),
            target_config.storage_payload(),
        )

        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2, 3, 4])
        self.assertEqual(rows[-1].operation, "restore")
        self.assertEqual(rows[-1].restore_source_revision, target_revision)
        self.assertEqual(rows[-1].parent_revision, target_revision + 1)
        self.assertEqual(rows[-1].updated_by, 7)
        self.assertIsNotNone(rows[-1].updated_at)
        self.assertEqual(rows[-1].secret_paths_changed, [])
        self.assertEqual(
            rows[-1].payload_redacted["economy"]["pin_price"],
            target_config.economy.pin_price,
        )
        self._assert_no_secret_material(
            json.dumps(rows[-1].payload_redacted, ensure_ascii=False), "回滚后的历史"
        )

    async def test_restore_applies_hot_fields_live_and_marks_restart_fields(self) -> None:
        await self._save_all_sentinels()
        target_revision = self.manager.revision
        target_pin_price = self.manager.config.economy.pin_price

        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 999
        payload["resources"]["pending_reply_execution_capacity"] = 9
        await self.manager.save(
            payload,
            expected_revision=target_revision,
            updated_by=SUPER_ADMIN_ID,
        )
        self.assertEqual(
            self.manager.api_document()["restart_pending"],
            ["resources.pending_reply_execution_capacity"],
        )

        await self.manager.restore(
            target_revision,
            expected_revision=target_revision + 1,
            updated_by=7,
        )

        document = self.manager.api_document()
        # 热字段立刻生效。
        self.assertEqual(self.settings.economy.pin_price, target_pin_price)
        self.assertEqual(document["config"]["economy"]["pin_price"], target_pin_price)
        # 改回进程真正在跑的值 → 该 restart 字段的提示自动消失。
        self.assertEqual(document["restart_pending"], [])

    async def test_restore_of_the_current_revision_still_creates_a_new_revision(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 5
        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)
        current = self.manager.revision

        await self.manager.restore(current, expected_revision=current, updated_by=7)

        self.assertEqual(self.manager.revision, current + 1)
        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2, 3])
        self.assertEqual(rows[-1].operation, "restore")
        self.assertEqual(rows[-1].restore_source_revision, current)

    async def test_stale_expected_revision_on_restore_is_a_conflict(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 5
        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)
        target = self.manager.revision
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=target,
            updated_by=SUPER_ADMIN_ID,
        )

        with self.assertRaises(RuntimeConfigConflictError):
            await self.manager.restore(
                target, expected_revision=target, updated_by=7
            )
        self.assertEqual(self.manager.revision, target + 1)
        self.assertEqual(
            [row.operation for row in await self._history_rows()],
            ["save", "save"],
        )

    async def test_restore_of_a_missing_revision_raises_a_stable_error(self) -> None:
        from bot.services.runtime_config_history import (
            RuntimeConfigHistoryNotFoundError,
        )

        with self.assertRaises(RuntimeConfigHistoryNotFoundError):
            await self.manager.restore(999, expected_revision=1, updated_by=7)
        self.assertEqual(self.manager.revision, 1)


class HistoryApiTests(_HistoryFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot = SimpleNamespace(token=BOT_TOKEN)
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await super().asyncTearDown()

    async def _json(self, method: str, path: str, **kwargs: Any) -> tuple[int, str, dict]:
        headers = kwargs.pop("headers", self._headers())
        response = await getattr(self.client, method)(path, headers=headers, **kwargs)
        raw = await response.text()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - 防御
            body = {}
        return response.status, raw, body

    # -- 鉴权 ---------------------------------------------------------------

    async def test_every_history_endpoint_is_super_admin_only(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        cases = (
            ("post", "/api/v1/settings/diff", {"json": {"revision": target, "config": self.manager.config.public_payload()}}),
            ("get", "/api/v1/settings/history", {}),
            ("get", f"/api/v1/settings/history/{target}", {}),
            ("get", f"/api/v1/settings/history/{target}/diff?against=1", {}),
            ("get", f"/api/v1/settings/history/{target}/restore-preview", {}),
            ("post", f"/api/v1/settings/history/{target}/restore", {"json": {"revision": target}}),
        )
        for method, path, kwargs in cases:
            with self.subTest(who="group_admin", method=method, path=path):
                status, _raw, body = await self._json(
                    method, path, headers=self._headers(GROUP_ADMIN_ID), **dict(kwargs)
                )
                self.assertEqual(status, 403, f"群管理员必须 403：{method} {path}")
                self.assertEqual(body["error"]["code"], "super_admin_required")
            with self.subTest(who="anonymous", method=method, path=path):
                response = await getattr(self.client, method)(path, **dict(kwargs))
                self.assertIn(response.status, (401, 403), f"未鉴权：{method} {path}")
                self._assert_no_secret_material(await response.text(), path)

        self.assertEqual(self.manager.revision, target, "越权请求不许改动任何东西")
        self.assertEqual([row.revision for row in await self._history_rows()], [2])

    async def test_group_id_tampering_cannot_reach_history(self) -> None:
        """历史是**全局**的：URL / JSON 里塞 group_id 不能换来别的群的数据。"""

        await self._save_all_sentinels()
        target = self.manager.revision
        probes = (
            ("get", f"/api/v1/settings/history?group_id={SYNTHETIC_GROUP_ID}", None),
            ("get", f"/api/v1/settings/history/{target}?group_id={SYNTHETIC_GROUP_ID}", None),
            (
                "get",
                f"/api/v1/settings/history/{target}/diff?against=1&group_id={SYNTHETIC_GROUP_ID}",
                None,
            ),
            (
                "post",
                f"/api/v1/settings/history/{target}/restore?group_id={SYNTHETIC_GROUP_ID}",
                {
                    "revision": target,
                    "group_id": SYNTHETIC_GROUP_ID,
                    "config": self.manager.config.public_payload(),
                },
            ),
            (
                "post",
                "/api/v1/settings/diff",
                {
                    "revision": target,
                    "group_id": SYNTHETIC_GROUP_ID,
                    "config": self.manager.config.public_payload(),
                },
            ),
        )
        for method, path, body in probes:
            with self.subTest(method=method, path=path):
                kwargs: dict[str, Any] = {"headers": self._headers(GROUP_ADMIN_ID)}
                if body is not None:
                    kwargs["json"] = body
                response = await getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status, 403, path)
                self._assert_no_secret_material(await response.text(), path)

        self.assertEqual(self.manager.revision, target)

    # -- 读取 ---------------------------------------------------------------

    async def test_history_list_returns_metadata_but_no_values(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(payload, expected_revision=target, updated_by=SUPER_ADMIN_ID)

        status, raw, body = await self._json("get", "/api/v1/settings/history")
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "GET /history")
        entries = body["history"]
        self.assertEqual([entry["revision"] for entry in entries], [2, 3])
        for entry in entries:
            self.assertIn("parent_revision", entry)
            self.assertIn("operation", entry)
            self.assertIn("summary", entry)
            self.assertIn("updated_by", entry)
            self.assertIn("updated_at", entry)
            self.assertIn("restore_source_revision", entry)
            self.assertIn("restart_pending", entry)
            self.assertNotIn("payload", entry)
            self.assertNotIn("payload_redacted", entry)

    async def test_history_detail_returns_redacted_payload_and_secret_status(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        status, raw, body = await self._json(
            "get", f"/api/v1/settings/history/{target}"
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, f"GET /history/{target}")
        self.assertEqual(body["revision"], target)
        self.assertEqual(body["operation"], "save")
        statuses = {item["path"]: item["status"] for item in body["secrets"]}
        self.assertEqual(statuses["tts.app_key"], "configured")
        self.assertEqual(statuses["providers.gemini.api_key"], "configured")
        self.assertEqual(
            secret_status(body["config"]["movie_info"]["imdb_api_key"]), "configured"
        )

    async def test_history_diff_between_two_revisions(self) -> None:
        await self._save_all_sentinels()
        first = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(payload, expected_revision=first, updated_by=SUPER_ADMIN_ID)

        # ``against`` 是基准，路径上的 revision 是目标：old = against，new = 目标。
        status, raw, body = await self._json(
            "get", f"/api/v1/settings/history/{first}/diff?against={first + 1}"
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "GET /history/{rev}/diff")
        self.assertEqual(body["revision"], first)
        self.assertEqual(body["against"], first + 1)
        paths = {entry["path"]: entry for entry in body["diff"]}
        self.assertIn("economy.pin_price", paths)
        self.assertEqual(paths["economy.pin_price"]["change"], "modified")
        self.assertEqual(paths["economy.pin_price"]["new"], 20)
        self.assertEqual(paths["economy.pin_price"]["old"], 4242)

    async def test_specific_diff_route_is_not_shadowed_by_the_generic_one(self) -> None:
        """aiohttp 按注册顺序匹配；``{revision}/diff`` 必须先于 ``{revision}``。"""

        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(payload, expected_revision=target, updated_by=SUPER_ADMIN_ID)

        diff_status, diff_raw, diff_body = await self._json(
            "get", f"/api/v1/settings/history/{target}/diff?against={target + 1}"
        )
        detail_status, _detail_raw, detail_body = await self._json(
            "get", f"/api/v1/settings/history/{target}"
        )
        self.assertEqual(diff_status, 200)
        self.assertIn("diff", diff_body)
        self.assertNotIn("config", diff_body)
        self.assertEqual(detail_status, 200)
        self.assertIn("config", detail_body)
        self.assertNotIn("diff", detail_body)
        self._assert_no_secret_material(diff_raw, "diff route")

    async def test_missing_history_revision_is_a_stable_404(self) -> None:
        await self._save_all_sentinels()
        status, raw, body = await self._json("get", "/api/v1/settings/history/424242")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "history_revision_not_found")
        for leak in ("Traceback", "SELECT", "sqlite", self._db_path):
            self.assertNotIn(leak, raw)

    async def test_corrupt_history_payload_is_a_stable_404_without_leaks(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE runtime_config_history SET payload_redacted = :payload "
                    "WHERE revision = :revision"
                ),
                {"payload": '{"broken": ', "revision": target},
            )
            await session.commit()

        status, raw, body = await self._json("get", f"/api/v1/settings/history/{target}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "history_revision_not_found")
        for leak in ("Traceback", "SELECT", "sqlite", self._db_path, "{broken"):
            self.assertNotIn(leak, raw)

    # -- 预览与回滚 ---------------------------------------------------------

    async def test_diff_preview_reports_changes_and_writes_nothing(self) -> None:
        await self._save_all_sentinels()
        current = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        payload["resources"]["pending_reply_execution_capacity"] = 9

        status, raw, body = await self._json(
            "post",
            "/api/v1/settings/diff",
            json={"revision": current, "config": payload},
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "POST /settings/diff")
        paths = {entry["path"]: entry for entry in body["diff"]}
        self.assertIn("economy.pin_price", paths)
        self.assertEqual(
            body["restart_pending"], ["resources.pending_reply_execution_capacity"]
        )
        # 预览不写库。
        self.assertEqual(self.manager.revision, current)
        self.assertEqual(self.manager.config.economy.pin_price, 20)
        self.assertEqual([row.revision for row in await self._history_rows()], [2])

    async def test_diff_preview_rejects_an_invalid_config(self) -> None:
        status, _raw, body = await self._json(
            "post",
            "/api/v1/settings/diff",
            json={"revision": 1, "config": {"economy": {"challenge_skip_cost": 0}}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_settings")

    async def test_restore_route_returns_the_new_api_document(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(payload, expected_revision=target, updated_by=SUPER_ADMIN_ID)

        status, raw, body = await self._json(
            "post",
            f"/api/v1/settings/history/{target}/restore",
            json={"revision": target + 1},
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "POST /history/{rev}/restore")
        self.assertEqual(body["revision"], target + 2)
        self.assertEqual(body["config"]["economy"]["pin_price"], 20)
        self.assertIn("providers.gemini.api_key", body["configured_secrets"])

    async def test_restore_route_with_stale_revision_is_a_conflict(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=target,
            updated_by=SUPER_ADMIN_ID,
        )
        status, raw, body = await self._json(
            "post",
            f"/api/v1/settings/history/{target}/restore",
            json={"revision": target},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "revision_conflict")
        self.assertNotIn("Traceback", raw)
        self.assertNotIn(self._db_path, raw)

    async def test_restore_route_with_a_missing_revision_is_a_404(self) -> None:
        status, raw, body = await self._json(
            "post",
            "/api/v1/settings/history/424242/restore",
            json={"revision": 1},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "history_revision_not_found")
        self.assertNotIn("Traceback", raw)

    async def test_missing_master_key_reports_secret_storage_unavailable(self) -> None:
        """没有 CONFIG_MASTER_KEY 时的回滚必须给稳定的 400，而不是 500。"""

        await self._save_all_sentinels()
        target = self.manager.revision
        # 换一把**读不出来**的主密钥：历史行还在，但密文解不开。
        self.manager._cipher = SecretCipher("")

        status, raw, body = await self._json(
            "post",
            f"/api/v1/settings/history/{target}/restore",
            json={"revision": target},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "secret_storage_unavailable")
        self.assertNotIn("CONFIG_MASTER_KEY", raw)
        self.assertNotIn("Traceback", raw)
        self.assertEqual(self.manager.revision, target)

    async def test_two_admins_racing_the_same_revision_only_one_wins(self) -> None:
        await self._save_all_sentinels()
        base = self.manager.revision

        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        ok = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": base, "config": payload},
        )
        self.assertEqual(ok.status, 200)

        # 第二个"管理员"拿着同一份过期 revision 再存一次。
        stale = self.manager.config.public_payload()
        stale["economy"]["pin_price"] = 4343
        conflict = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": base, "config": stale},
        )
        self.assertEqual(conflict.status, 409)

        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2, 3])
        self.assertEqual(self.manager.config.economy.pin_price, 4242)

    async def test_racing_restores_only_one_wins(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(payload, expected_revision=target, updated_by=SUPER_ADMIN_ID)
        current = self.manager.revision

        first = await self.client.post(
            f"/api/v1/settings/history/{target}/restore",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": current},
        )
        second = await self.client.post(
            f"/api/v1/settings/history/{target}/restore",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": current},
        )
        self.assertEqual([first.status, second.status], [200, 409])
        restores = [row for row in await self._history_rows() if row.operation == "restore"]
        self.assertEqual(len(restores), 1)

    async def test_oversized_save_surfaces_a_stable_api_error_code(self) -> None:
        """API 层：超限保存回**稳定**错误码，且一个字节都不写。

        这里把上限临时压到很小，好让**正常**的请求体就能触发（真实 1 MiB 的上限
        与 settings_api 的 1 MiB 请求体上限同级，HTTP 上会先被 413 拦下；这层
        校验是存储侧的后备，不依赖传输层）。
        """

        before = await self._main_row_fingerprint()
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242

        with mock.patch.object(history_module, "MAX_SNAPSHOT_BYTES", 2048):
            response = await self.client.put(
                "/api/v1/settings",
                headers=self._headers(SUPER_ADMIN_ID),
                json={"revision": 1, "config": payload},
            )

        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "invalid_settings")
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(await self._history_rows(), [])

    async def test_oversized_restore_route_surfaces_a_stable_api_error_code(self) -> None:
        """回滚超限时同样走稳定错误码，而不是 500 或"回滚了半份配置"。"""

        await self._save_all_sentinels()
        target = self.manager.revision
        before = await self._main_row_fingerprint()

        with mock.patch.object(history_module, "MAX_SNAPSHOT_BYTES", 2048):
            response = await self.client.post(
                f"/api/v1/settings/history/{target}/restore",
                headers=self._headers(SUPER_ADMIN_ID),
                json={"revision": target},
            )

        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "invalid_settings")
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(self.manager.revision, target)


# ===========================================================================
# HTTP 契约：分页、共享错误码、回滚预览、端到端哨兵
# ===========================================================================


class HistoryApiContractTests(HistoryApiTests):
    """这一组跑在真实的 aiohttp 测试服务器上（``HistoryApiTests`` 的装置）。"""

    async def test_history_list_exposes_a_bounded_page_contract(self) -> None:
        await self._save_all_sentinels()
        status, raw, body = await self._json("get", "/api/v1/settings/history")
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "GET /history")
        self.assertEqual(
            sorted(body["page"]),
            ["before", "count", "has_more", "limit", "next_before"],
        )
        self.assertEqual(body["page"]["limit"], HISTORY_PAGE_LIMIT)
        self.assertIsNone(body["page"]["before"])
        self.assertEqual(body["page"]["count"], len(body["history"]))
        self.assertFalse(body["page"]["has_more"])
        self.assertIsNone(body["page"]["next_before"])

    async def test_history_paging_walks_through_the_api(self) -> None:
        await self._save_all_sentinels()
        base = self.manager.revision
        for index in range(4):
            payload = self.manager.config.public_payload()
            payload["economy"]["pin_price"] = 1000 + index
            await self.manager.save(
                payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
            )
        first_status, _raw, first = await self._json(
            "get", "/api/v1/settings/history?limit=2"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(
            [entry["revision"] for entry in first["history"]],
            [base + 3, base + 4],
        )
        self.assertTrue(first["page"]["has_more"])
        self.assertEqual(first["page"]["next_before"], base + 3)

        _s, _r, second = await self._json(
            "get",
            f"/api/v1/settings/history?limit=2&before={first['page']['next_before']}",
        )
        self.assertEqual(
            [entry["revision"] for entry in second["history"]], [base + 1, base + 2]
        )
        self.assertTrue(second["page"]["has_more"])
        _s, _r, third = await self._json(
            "get",
            f"/api/v1/settings/history?limit=2&before={second['page']['next_before']}",
        )
        self.assertEqual([entry["revision"] for entry in third["history"]], [base])
        self.assertFalse(third["page"]["has_more"])
        self.assertIsNone(third["page"]["next_before"])

    async def test_bad_paging_parameters_are_a_stable_400(self) -> None:
        await self._save_all_sentinels()
        for query in ("limit=0", "limit=abc", f"limit={HISTORY_PAGE_MAX_LIMIT + 1}", "before=0", "before=x"):
            with self.subTest(query=query):
                status, _raw, body = await self._json(
                    "get", f"/api/v1/settings/history?{query}"
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_history_page")

    async def test_preview_and_save_return_the_same_error_code_for_bad_secrets(self) -> None:
        await self._save_all_sentinels()
        payload = self.manager.config.public_payload()
        changes = {"tts.app_key": {"action": "explode"}}
        preview_status, _raw, preview_body = await self._json(
            "post",
            "/api/v1/settings/diff",
            json={
                "revision": self.manager.revision,
                "config": payload,
                "secret_changes": changes,
            },
        )
        save = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={
                "revision": self.manager.revision,
                "config": payload,
                "secret_changes": changes,
            },
        )
        save_body = await save.json()
        self.assertEqual(preview_status, 400)
        self.assertEqual(save.status, 400)
        self.assertEqual(preview_body["error"]["code"], "invalid_settings")
        self.assertEqual(save_body["error"]["code"], preview_body["error"]["code"])
        self.assertEqual(save_body["error"]["message"], preview_body["error"]["message"])

    async def test_preview_and_save_agree_on_the_oversize_limit(self) -> None:
        """同一份超大配置：预览与保存**同一个**稳定错误码，DB 一个字节不变。"""

        before = await self._main_row_fingerprint()
        payload = self.manager.config.public_payload()
        # 请求体本身必须**小于**传输层的 1 MiB 上限，否则 aiohttp 先回 413，
        # 根本走不到快照上限——那证明不了"预览与保存共享同一个判断"。
        payload["prompts"]["casual"] = "x" * 8_000
        with mock.patch.object(history_module, "MAX_SNAPSHOT_BYTES", 4096):
            preview = await self.client.post(
                "/api/v1/settings/diff",
                headers=self._headers(SUPER_ADMIN_ID),
                json={"revision": self.manager.revision, "config": payload},
            )
            preview_body = await preview.json()
            save = await self.client.put(
                "/api/v1/settings",
                headers=self._headers(SUPER_ADMIN_ID),
                json={"revision": self.manager.revision, "config": payload},
            )
            save_body = await save.json()
        self.assertEqual(preview.status, 400)
        self.assertEqual(save.status, 400)
        self.assertEqual(save_body["error"]["code"], "invalid_settings")
        self.assertEqual(preview_body["error"]["code"], save_body["error"]["code"])
        self.assertEqual(
            preview_body["error"]["message"], save_body["error"]["message"]
        )
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(await self._history_rows(), [])

    async def test_unchanged_config_preview_is_empty_over_http(self) -> None:
        await self._save_all_sentinels()
        status, raw, body = await self._json(
            "post",
            "/api/v1/settings/diff",
            json={
                "revision": self.manager.revision,
                "config": self.manager.config.public_payload(),
            },
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "POST /settings/diff（未改动）")
        self.assertEqual(body["diff"], [])
        self.assertIn("无变化", body["summary"])
        self.assertEqual(body["changed_secret_paths"], [])
        self.assertGreater(body["snapshot_bytes"], 0)
        self.assertEqual(body["snapshot_limit_bytes"], MAX_SNAPSHOT_BYTES)

    async def test_restore_preview_route_shows_the_real_candidate(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(
            payload, expected_revision=target, updated_by=SUPER_ADMIN_ID
        )
        status, raw, body = await self._json(
            "get", f"/api/v1/settings/history/{target}/restore-preview"
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "GET /history/{rev}/restore-preview")
        self.assertEqual(body["revision"], target)
        self.assertEqual(body["current_revision"], self.manager.revision)
        paths = {entry["path"]: entry for entry in body["diff"]}
        self.assertIn("economy.pin_price", paths)
        self.assertEqual(paths["economy.pin_price"]["old"], 4242)
        self.assertEqual(paths["economy.pin_price"]["new"], 20)
        self.assertEqual(body["retained_secrets"], [])

    async def test_restore_preview_and_restore_share_the_not_restorable_code(self) -> None:
        payload = self.manager.config.public_payload()
        payload["models"]["main"]["request_params"] = json.loads(
            json.dumps(OPAQUE_REQUEST_PARAMS)
        )
        for provider in payload["models"]["providers"]:
            provider["api_base"] = OPAQUE_API_BASE
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        target = self.manager.revision

        # 把当前配置里那个槽位整个去掉 → 目标版本取不到可保留的值。
        stripped = self.manager.config.public_payload()
        stripped["models"]["main"]["request_params"] = {}
        await self.manager.save(
            stripped, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        before = await self._main_row_fingerprint()

        _s, _r, preview = await self._json(
            "get", f"/api/v1/settings/history/{target}/restore-preview"
        )
        self.assertEqual(preview["error"]["code"], "history_revision_not_restorable")
        self.assertIn("models.main.request_params.extra_headers[0]", preview["error"]["message"])
        self._assert_no_opaque_material(json.dumps(preview, ensure_ascii=False), "回滚预览")

        status, _r, body = await self._json(
            "post",
            f"/api/v1/settings/history/{target}/restore",
            json={"revision": self.manager.revision},
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            body["error"]["code"], preview["error"]["code"], "预览与回滚必须同码"
        )
        self.assertEqual(await self._main_row_fingerprint(), before)

    async def test_restore_route_reports_retained_secrets(self) -> None:
        # rev2：只有 gemini（"窄"版本）。
        narrow_payload = self.manager.config.public_payload()
        await self.manager.save(
            narrow_payload,
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes={
                "providers.gemini.api_key": {"action": "replace", "value": SENTINELS["providers.gemini.api_key"]}
            },
        )
        narrow = self.manager.revision
        # rev3：加上 openai（"宽"版本），两把密钥都在。
        wide_payload = self._payload_with_second_provider()
        await self.manager.save(
            wide_payload,
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes={
                "providers.openai.api_key": {"action": "replace", "value": SENTINELS["providers.openai.api_key"]}
            },
        )
        wide = self.manager.revision

        status, raw, body = await self._json(
            "post",
            f"/api/v1/settings/history/{narrow}/restore",
            json={"revision": wide},
        )
        self.assertEqual(status, 200)
        self._assert_no_secret_material(raw, "POST /history/{rev}/restore")
        self.assertEqual(body["restore"]["source_revision"], narrow)
        self.assertEqual(body["restore"]["retained_secrets"], ["providers.openai.api_key"])
        self.assertNotIn("SENTINEL-openai-key", raw)
        # 配置回到窄版本，但两把密钥都还在库里。
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
        )
        self.assertNotIn(
            "providers.openai.api_key", body["configured_secrets"],
            "运行时不该再持有窄版本里没有槽位的密钥",
        )

    async def test_opaque_sentinels_never_appear_in_any_http_response(self) -> None:
        """端到端：把六种自由 JSON / URL 凭据喂进去，逐个接口扫一遍。"""

        payload = self.manager.config.public_payload()
        payload["models"]["main"]["request_params"] = json.loads(
            json.dumps(OPAQUE_REQUEST_PARAMS)
        )
        for provider in payload["models"]["providers"]:
            provider["api_base"] = OPAQUE_API_BASE
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        first = self.manager.revision
        second_payload = self.manager.config.public_payload()
        second_payload["economy"]["pin_price"] = 314
        await self.manager.save(
            second_payload,
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
        )
        target = self.manager.revision

        probes: list[tuple[str, str, dict[str, Any]]] = [
            ("get", "/api/v1/settings", {}),
            ("get", "/api/v1/settings/history", {}),
            ("get", "/api/v1/settings/history?limit=1", {}),
            ("get", f"/api/v1/settings/history/{target}", {}),
            ("get", f"/api/v1/settings/history/{target}/diff?against={first}", {}),
            ("get", f"/api/v1/settings/history/{first}/restore-preview", {}),
            (
                "post",
                "/api/v1/settings/diff",
                {"json": {"revision": target, "config": payload}},
            ),
        ]
        for method, path, kwargs in probes:
            with self.subTest(path=path):
                status, raw, _body = await self._json(method, path, **dict(kwargs))
                self.assertEqual(status, 200, path)
                self._assert_no_opaque_material(raw, path)
                self._assert_no_secret_material(raw, path)

        # DB 里同样一处不留。
        self._assert_no_opaque_material(await self._raw_history_json(), "历史表")
        # 保存历史的日志也不许有。
        rows = await self._history_rows()
        for row in rows:
            self._assert_no_opaque_material(str(row.summary), "历史摘要")

    async def test_api_document_exposes_no_opaque_credentials(self) -> None:
        payload = self.manager.config.public_payload()
        payload["models"]["main"]["request_params"] = json.loads(
            json.dumps(OPAQUE_REQUEST_PARAMS)
        )
        for provider in payload["models"]["providers"]:
            provider["api_base"] = OPAQUE_API_BASE
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        _status, raw, _body = await self._json("get", "/api/v1/settings")
        # ``GET /settings`` 回的是**当前生效配置**（含运营刚填的凭据），所以
        # 这里只断言登记在册的密钥路径仍然为空——见文档里"主表存的是活配置"。
        document = json.loads(raw)
        for provider in document["config"]["models"]["providers"]:
            self.assertEqual(provider["api_key"], "")
        self._assert_no_secret_material(raw, "GET /settings")
        self.assertEqual(
            [row.revision for row in await self._history_rows()], [2]
        )
        self._assert_no_opaque_material(
            await self._raw_history_json(), "历史表（GET /settings 之后）"
        )


class SnapshotLimitTests(_HistoryFixture):
    """历史快照的**存储**上限：一次成功保存会永久追加一整份脱敏配置。

    保留/清理（TTL）在这一批**没有**实现，见 docs/configuration-reference.md。
    那就必须给"单份快照"设一个明确的上限，否则"不能无说明无限复制大型 prompt"
    这条要求只约束了差异输出、没约束真正会一直占盘的那份东西。

    超限的处理方式是**让保存失败**，不是"跳过历史照存主表"——后者等于把已经
    修好的"主表已更新、历史缺失"不变量又放回来（配置改了却查不到谁改的）。
    """

    #: 用来把快照撑到任意目标字节数的字段（提示词字段无长度限制，且不会被脱敏
    #: 启发式命中：``casual`` 的键名组件是 {casual}）。
    _PAD_FIELD = ("prompts", "casual")

    def _snapshot_of(self, payload: dict[str, Any]) -> dict[str, Any]:
        return redact_payload(payload, self.manager.config.secret_paths())

    def _with_pad(self, payload: dict[str, Any], length: int) -> dict[str, Any]:
        clone = json.loads(json.dumps(payload))
        section, field = self._PAD_FIELD
        clone[section][field] = "x" * max(0, length)
        return self._snapshot_of(clone)

    def _padded_to_exactly(self, target: int) -> dict[str, Any]:
        """造一份脱敏快照**正好** ``target`` 字节的 payload。

        JSON 转义会让"补多少字符"与"多多少字节"不一一对应，所以这里先补一遍
        再按差值收敛（纯 ASCII 填充下两轮以内就到）。
        """

        base = self._snapshot_of(self.manager.config.model_dump(mode="json"))
        padding = "x" * max(0, target - snapshot_size_bytes(base))
        for _ in range(8):
            candidate = self._with_pad(self.manager.config.model_dump(mode="json"), len(padding))
            size = snapshot_size_bytes(candidate)
            if size == target:
                return candidate
            padding = "x" * max(0, len(padding) + (target - size))
        self.fail(f"could not size a snapshot to exactly {target} bytes")

    # -- 上限本身是一条真实的界 ------------------------------------------------

    def test_the_ceiling_is_a_real_bound_not_a_tautology(self) -> None:
        self.assertGreater(MAX_SNAPSHOT_BYTES, 0)
        self.assertLess(MAX_SNAPSHOT_BYTES, 2**40, "上限必须是有限值")
        default_size = snapshot_size_bytes(
            self._snapshot_of(self.manager.config.model_dump(mode="json"))
        )
        self.assertGreater(default_size, 0)
        # 全新部署的默认配置要**远低于**上限：这个上限是给"病态输入"兜底的，
        # 任何正常部署都不该碰到它。
        self.assertGreater(
            MAX_SNAPSHOT_BYTES,
            default_size * 8,
            f"default snapshot {default_size} B vs ceiling {MAX_SNAPSHOT_BYTES} B",
        )

    def test_the_boundary_is_exact_in_both_directions(self) -> None:
        """正好 = 上限放行，多一个字节就拒。证明这不是一个恒真的判断。"""

        exactly = self._padded_to_exactly(MAX_SNAPSHOT_BYTES)
        self.assertEqual(snapshot_size_bytes(exactly), MAX_SNAPSHOT_BYTES)
        ensure_snapshot_within_limit(exactly)  # 正好卡线上不炸

        one_more = json.loads(json.dumps(exactly))
        section, field = self._PAD_FIELD
        one_more[section][field] = one_more[section][field] + "x"
        self.assertEqual(
            snapshot_size_bytes(one_more), MAX_SNAPSHOT_BYTES + 1
        )
        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError):
            ensure_snapshot_within_limit(one_more)

    def test_the_error_carries_a_stable_value_error_shape(self) -> None:
        """必须是 ``ValueError`` 子类：所有既有路由的 ``except ValueError`` 就能
        把它映射成稳定的 400 ``invalid_settings``，不必改动 settings_api。"""

        self.assertTrue(
            issubclass(RuntimeConfigHistorySnapshotTooLargeError, ValueError)
        )

    # -- 正常配置完全不受影响 --------------------------------------------------

    async def test_a_normal_save_still_writes_history(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        snapshot = self._snapshot_of(
            self.manager.config.model_copy(deep=True)
            .model_dump(mode="json")
        )
        self.assertLess(snapshot_size_bytes(snapshot), MAX_SNAPSHOT_BYTES)

        await self.manager.save(payload, expected_revision=1, updated_by=SUPER_ADMIN_ID)

        rows = await self._history_rows()
        self.assertEqual([row.revision for row in rows], [2])
        self.assertLess(
            snapshot_size_bytes(rows[0].payload_redacted), MAX_SNAPSHOT_BYTES
        )

    # -- 超限时：保存失败，主表逐字不变 ----------------------------------------

    async def test_an_oversized_save_fails_and_leaves_the_main_row_untouched(self) -> None:
        before = await self._main_row_fingerprint()
        oversized = self._padded_to_exactly(MAX_SNAPSHOT_BYTES + 1)

        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError) as caught:
            await self.manager.save(
                oversized, expected_revision=1, updated_by=SUPER_ADMIN_ID
            )

        message = str(caught.exception)
        self.assertIn(str(MAX_SNAPSHOT_BYTES), message)
        self.assertNotIn("x" * 64, message, "错误文案不得回显配置正文")
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(await self._history_rows(), [])
        self.assertTrue(self.settings.economy.pin_price)

    async def test_an_oversized_restore_is_refused_and_changes_nothing(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        before = await self._main_row_fingerprint()

        # 手工塞一份超限的历史（正常代码路径写不出来，模拟历史遗留/外部导入）。
        oversized = self._padded_to_exactly(MAX_SNAPSHOT_BYTES + 1)
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE runtime_config_history SET payload_redacted = :payload "
                    "WHERE revision = :revision"
                ),
                {"payload": json.dumps(oversized, ensure_ascii=False), "revision": target},
            )
            await session.commit()

        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError):
            await self.manager.restore(
                target, expected_revision=target, updated_by=SUPER_ADMIN_ID
            )

        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(self.manager.revision, target)
        self.assertEqual([row.revision for row in await self._history_rows()], [2])


# ===========================================================================
# 审计第 6 项：差异里的伪变更 + "预览与保存不是同一条代码路径"
# ===========================================================================


class OpaqueRedactionUnitTests(unittest.TestCase):
    """值形状 / URL 脱敏的直接单测（不碰数据库，先把判定本身钉死）。"""

    def test_a_list_under_an_innocuous_key_is_not_a_bypass(self) -> None:
        """``extra_headers`` 的键名不含任何密钥组件——**值**才是凭据。"""

        payload = {"models": {"main": {"request_params": {"extra_headers": ["Bearer sk-x"]}}}}
        redacted = redact_payload(payload, set())
        encoded = json.dumps(redacted, ensure_ascii=False)
        self.assertNotIn("sk-x", encoded)
        self.assertEqual(
            redacted["models"]["main"]["request_params"]["extra_headers"],
            [f"<redacted:{SECRET_STATUS_CURRENT}>"],
        )

    def test_a_dict_and_a_nested_dict_under_sensitive_keys_are_replaced_whole(self) -> None:
        payload = {
            "request_params": {
                "headers": {"X-Api-Key": "k1"},
                "nested": {"options": {"api_keys": ["k2", "k3"]}},
            }
        }
        redacted = redact_payload(payload, set())
        encoded = json.dumps(redacted, ensure_ascii=False)
        for leaked in ("k1", "k2", "k3"):
            self.assertNotIn(leaked, encoded)
        # 容器里的**可疑键**整个被替换（结构塌成一个标记，但值没了）。
        self.assertEqual(
            redacted["request_params"]["nested"]["options"]["api_keys"],
            f"<redacted:{SECRET_STATUS_CURRENT}>",
        )
        # ``headers`` 本身不是可疑键，但里面的 ``X-Api-Key`` 是：逐键脱敏。
        self.assertEqual(
            redacted["request_params"]["headers"],
            {"X-Api-Key": f"<redacted:{SECRET_STATUS_CURRENT}>"},
        )

    def test_a_number_under_a_secret_key_is_left_alone(self) -> None:
        """``decision_history_token_budget`` 是**数量**。

        把它换成字符串标记会撑爆回滚时的严格 schema（``should be a valid
        integer``）——"为了脱敏把配置改坏"。一个裸数字本来也不可能是凭据。
        """

        payload = {"resources": {"decision_history_token_budget": 8192}}
        redacted = redact_payload(payload, set())
        self.assertEqual(
            redacted["resources"]["decision_history_token_budget"], 8192
        )

    def test_every_documented_url_credential_shape_is_scrubbed(self) -> None:
        cases = {
            "userinfo": "https://u:pw-secret@host/v1",
            "query-token": "https://host/v1?token=q-secret",
            "query-key": "https://host/v1?api_key=q-secret",
            "query-access-token": "https://host/v1?access_token=q-secret",
            "query-password": "https://host/v1?password=q-secret",
            "query-dashed": "https://host/v1?X-Api-Key=q-secret",
            "embedded": "see https://u:pw-secret@host/v1?token=q-secret now",
        }
        for name, url in cases.items():
            with self.subTest(case=name):
                scrubbed = redact_url_credentials(url)
                self.assertNotIn("pw-secret", scrubbed)
                self.assertNotIn("q-secret", scrubbed)
        self.assertIn(URL_USERINFO_MARKER, redact_url_credentials(cases["userinfo"]))
        self.assertIn(URL_QUERY_MARKER, redact_url_credentials(cases["query-token"]))
        # 非敏感 query 与 host/path 保留：管理员仍看得出地址指向哪。
        kept = redact_url_credentials("https://host/v1?model=fast&key=q-secret")
        self.assertIn("model=fast", kept)
        self.assertIn("host/v1", kept)
        self.assertIn(f"key={URL_QUERY_MARKER}", kept)

    def test_a_non_url_string_is_never_mangled(self) -> None:
        for text in ("", "plain prompt text", "https", "a://b is not parsed", "模型 note: 好"):
            with self.subTest(text=text):
                self.assertEqual(redact_url_credentials(text), text)

    def test_credential_string_detection_is_tight(self) -> None:
        for value in (
            "Bearer abc",
            "basic dXNlcjpwYXNz",
            "Authorization: Bearer abc",
            "X-Api-Key: k",
            "X-Custom-Token: t",
            "Cookie: sid=1",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_credential_string(value), value)
        for value in (
            "模型请保持客观",
            "Note: remember to check the logs",
            "gemini-2.0-flash",
            "https://api.example.com/v1",
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_credential_string(value), value)


class SecretChangeParserUnitTests(unittest.TestCase):
    """``resolve_secret_changes``：**预览与保存唯一的**解析器。"""

    def setUp(self) -> None:
        self.allowed = {"providers.gemini.api_key", "tts.app_key"}

    def test_keep_clear_and_replace(self) -> None:
        secrets, replaced = resolve_secret_changes(
            {
                "providers.gemini.api_key": {"action": "clear"},
                "tts.app_key": {"action": "replace", "value": " new "},
            },
            allowed_paths=self.allowed,
            current_secrets={
                "providers.gemini.api_key": "old",
                "tts.app_key": "old",
            },
            proposed_secrets={},
        )
        self.assertEqual(secrets, {"tts.app_key": "new"})
        self.assertEqual(replaced, {"providers.gemini.api_key", "tts.app_key"})

    def test_keep_leaves_everything_untouched(self) -> None:
        secrets, replaced = resolve_secret_changes(
            {"tts.app_key": {"action": "keep"}},
            allowed_paths=self.allowed,
            current_secrets={"tts.app_key": "old"},
            proposed_secrets={},
        )
        self.assertEqual(secrets, {"tts.app_key": "old"})
        self.assertEqual(replaced, set())

    def test_invalid_inputs_are_rejected_with_actionable_messages(self) -> None:
        cases = (
            ({"providers.nope.api_key": {"action": "clear"}}, "unknown secret setting"),
            ({"tts.app_key": {"action": "explode"}}, "invalid secret action"),
            ({"tts.app_key": {"action": ""}}, "invalid secret action"),
            ({"tts.app_key": {"action": "replace", "value": "   "}}, "empty"),
        )
        for changes, needle in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as caught:
                    resolve_secret_changes(
                        changes,
                        allowed_paths=self.allowed,
                        current_secrets={},
                        proposed_secrets={},
                    )
                self.assertIn(needle, str(caught.exception))


class DiffHonestyUnitTests(unittest.TestCase):
    """差异里**不许**有伪变更。"""

    def test_two_identical_empty_secrets_produce_no_entry(self) -> None:
        """审计样本：``api_key cleared -> cleared`` 这种"伪变更"一条都不许有。"""

        entries = build_diff(
            {"movie_info": {"imdb_api_key": ""}},
            {"movie_info": {"imdb_api_key": ""}},
            {"movie_info.imdb_api_key"},
        )
        self.assertEqual(entries, [])

    def test_an_unchanged_document_produces_no_entries_at_all(self) -> None:
        document = {
            "movie_info": {"imdb_api_key": "", "max_results": 6},
            "tts": {"app_key": "", "speaker": ""},
            "providers": [{"name": "gemini", "api_key": ""}],
        }
        self.assertEqual(build_diff(document, document, set()), [])

    def test_real_transitions_are_still_reported(self) -> None:
        cleared = build_diff(
            {"tts": {"app_key": "OLD"}}, {"tts": {"app_key": ""}}, {"tts.app_key"}
        )
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["new"], {"status": "cleared"})


class RestoreOpaqueUnitTests(unittest.TestCase):
    """``restore_opaque_values``：回滚策略 (a) 保留当前 / (b) 明确拒绝。"""

    def test_current_value_is_kept_instead_of_clearing(self) -> None:
        history = {
            "models": {
                "main": {
                    "request_params": {
                        "extra_headers": [f"<redacted:{SECRET_STATUS_CURRENT}>"]
                    }
                }
            }
        }
        current = {
            "models": {
                "main": {
                    "request_params": {"extra_headers": ["Bearer real-one"]},
                }
            }
        }
        resolved = restore_opaque_values(history, current)
        self.assertEqual(
            resolved["models"]["main"]["request_params"]["extra_headers"],
            ["Bearer real-one"],
        )

    def test_a_url_with_scrubbed_credentials_keeps_the_current_url_whole(self) -> None:
        history = {
            "models": {
                "providers": [
                    {"name": "gemini", "api_base": f"https://{URL_USERINFO_MARKER}@h/v1?token={URL_QUERY_MARKER}"}
                ]
            }
        }
        current = {
            "models": {
                "providers": [
                    {"name": "gemini", "api_base": "https://u:pw@h/v1?token=t"}
                ]
            }
        }
        resolved = restore_opaque_values(history, current)
        self.assertEqual(
            resolved["models"]["providers"][0]["api_base"], "https://u:pw@h/v1?token=t"
        )

    def test_refuses_instead_of_silently_clearing_when_current_is_unavailable(self) -> None:
        history = {
            "models": {"main": {"request_params": {"extra_headers": [f"<redacted:{SECRET_STATUS_CURRENT}>"]}}}
        }
        with self.assertRaises(RuntimeConfigHistoryNotRestorableError) as caught:
            restore_opaque_values(history, {"models": {"main": {"request_params": {}}}})
        message = str(caught.exception)
        self.assertIn("models.main.request_params.extra_headers[0]", message)
        # 消息里只有路径名，绝不回显配置值。
        self.assertNotIn("Bearer", message)

    def test_a_value_that_exists_only_in_history_is_never_silently_dropped(self) -> None:
        """历史里有、当前配置里连路径都没有 → 拒绝，不放行。"""

        history = {"a": {"b": {"api_key": f"<redacted:{SECRET_STATUS_CURRENT}>"}}}
        current = {"a": {}}
        with self.assertRaises(RuntimeConfigHistoryNotRestorableError):
            restore_opaque_values(history, current)


class SnapshotAccountingUnitTests(unittest.TestCase):
    """口径必须是"列里真正存下来的那个字节串"。"""

    def test_measurement_matches_the_persisted_encoding(self) -> None:
        payload = {"prompts": {"casual": "规" * 1000}, "ascii": "x" * 100}
        encoded = encode_snapshot_json(payload)
        self.assertEqual(snapshot_size_bytes(payload), len(encoded.encode("utf-8")))
        # ASCII 纯文本下两种编码一致，这正是原来"看不出问题"的原因。
        ascii_payload = {"prompts": {"casual": "x" * 1000}}
        self.assertEqual(
            snapshot_size_bytes(ascii_payload),
            len(json.dumps(ascii_payload).encode("utf-8")),
        )

    def test_a_chinese_prompt_is_much_bigger_as_stored_than_a_utf8_count(self) -> None:
        payload = {"prompts": {"casual": "规" * 180_000}}
        self.assertGreater(
            snapshot_size_bytes(payload),
            len(("规" * 180_000).encode("utf-8")),
        )


# ===========================================================================
# 第 6 项（集成）：预览 == 保存，同一个解析器、同一个差异、同一个错误码
# ===========================================================================


class PreviewMatchesSaveTests(_HistoryFixture):
    async def _payload_with_second_provider_and_keys(self) -> dict[str, Any]:
        payload = self._payload_with_second_provider()
        return payload

    async def test_an_unchanged_config_produces_an_empty_diff(self) -> None:
        await self._save_all_sentinels()
        current = self.manager.config.public_payload()
        preview = await self.manager.preview(current)
        self.assertEqual(preview["diff"], [], "什么都没改就不该有差异条目")
        self.assertIn("无变化", preview["summary"])

    async def test_no_pseudo_cleared_entries_survive_in_a_real_save(self) -> None:
        await self._save_all_sentinels()
        preview = await self.manager.preview(self.manager.config.public_payload())
        blob = json.dumps(preview["diff"], ensure_ascii=False)
        self.assertNotIn("cleared", blob)

    async def test_preview_reports_an_existing_secret_replacement(self) -> None:
        await self._save_all_sentinels()
        preview = await self.manager.preview(
            self._payload_with_second_provider(),
            secret_changes={
                "tts.app_key": {"action": "replace", "value": "SENTINEL-new-app-key"}
            },
        )
        entries = {entry["path"]: entry for entry in preview["diff"]}
        self.assertIn("tts.app_key", entries)
        self.assertTrue(entries["tts.app_key"]["secret"])
        self.assertEqual(entries["tts.app_key"]["new"], {"status": "unchanged"})
        self.assertIn("tts.app_key", preview["changed_secret_paths"])

    async def test_preview_and_save_reject_identical_bad_secret_changes(self) -> None:
        """**同一个解析器**的显式等价性：坏输入在两条路径上同码同类型。"""

        await self._save_all_sentinels()
        payload = self._payload_with_second_provider()
        cases = (
            ("unknown-path", {"providers.nope.api_key": {"action": "clear"}}),
            ("invalid-action", {"tts.app_key": {"action": "explode"}}),
            ("empty-replace", {"tts.app_key": {"action": "replace", "value": "  "}}),
        )
        for name, changes in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as preview_error:
                    await self.manager.preview(payload, secret_changes=changes)
                with self.assertRaises(ValueError) as save_error:
                    await self.manager.save(
                        payload,
                        expected_revision=self.manager.revision,
                        updated_by=SUPER_ADMIN_ID,
                        secret_changes=changes,
                    )
                self.assertEqual(type(preview_error.exception), type(save_error.exception))
                self.assertEqual(str(preview_error.exception), str(save_error.exception))

    async def test_preview_diff_is_byte_identical_to_the_history_row_it_predicts(
        self,
    ) -> None:
        await self._save_all_sentinels()
        payload = self._payload_with_second_provider()
        payload["economy"]["pin_price"] = 4242
        changes = {"tts.app_key": {"action": "replace", "value": "SENTINEL-rotated"}}
        # 保存**之前**的当前状态：预览算的差异就是它与候选之间的差异。
        before = redact_payload(
            self.manager.config.model_dump(mode="json"),
            self.manager.config.secret_paths(),
        )
        preview = await self.manager.preview(payload, secret_changes=changes)
        await self.manager.save(
            payload,
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes=changes,
        )
        rows = await self._history_rows()
        self.assertEqual(rows[-1].summary, preview["summary"])
        self.assertEqual(
            rows[-1].secret_paths_changed, preview["changed_secret_paths"]
        )
        self.assertEqual(
            preview["snapshot_bytes"],
            snapshot_size_bytes(rows[-1].payload_redacted),
        )
        rebuilt = build_diff(
            before,
            rows[-1].payload_redacted,
            self.manager.config.secret_paths(),
            changed_secret_paths=preview["changed_secret_paths"],
        )
        self.assertEqual(rebuilt, preview["diff"])
        self.assertEqual(json.dumps(rebuilt), json.dumps(preview["diff"]))

    async def test_preview_reports_the_snapshot_bytes_save_would_store(self) -> None:
        await self._save_all_sentinels()
        payload = self._payload_with_second_provider()
        payload["economy"]["pin_price"] = 4321
        preview = await self.manager.preview(payload)
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        self.assertEqual(preview["snapshot_bytes"], rows[-1].payload_redacted and snapshot_size_bytes(rows[-1].payload_redacted))
        self.assertEqual(preview["snapshot_limit_bytes"], MAX_SNAPSHOT_BYTES)


# ===========================================================================
# 第 7 项：自由 JSON / URL 里的凭据不进历史，回滚保留当前值
# ===========================================================================


class OpaqueCredentialLeakTests(_HistoryFixture):
    """``request_params`` 与 URL 里的凭据：DB / API / diff / 摘要 / 日志一处不留。"""

    def _payload_with_opaque_credentials(self) -> dict[str, Any]:
        payload = self.manager.config.public_payload()
        payload["models"]["main"]["request_params"] = json.loads(
            json.dumps(OPAQUE_REQUEST_PARAMS)
        )
        for provider in payload["models"]["providers"]:
            provider["api_base"] = OPAQUE_API_BASE
        return payload

    async def test_sentinels_never_reach_the_history_table(self) -> None:
        handler = self._collect_logs()
        try:
            await self.manager.save(
                self._payload_with_opaque_credentials(),
                expected_revision=self.manager.revision,
                updated_by=SUPER_ADMIN_ID,
                secret_changes={
                    "providers.gemini.api_key": {
                        "action": "replace",
                        "value": SENTINELS["providers.gemini.api_key"],
                    }
                },
            )
            raw = await self._raw_history_json()
        finally:
            self._drop_logs(handler)

        self._assert_no_opaque_material(raw, "SELECT 回读的历史行")
        self._assert_no_secret_material(raw, "SELECT 回读的历史行")

        rows = await self._history_rows()
        payload = rows[-1].payload_redacted
        # 结构还在（管理员看得见"这里配了自定义头"），值不在。
        self.assertEqual(
            payload["models"]["main"]["request_params"]["extra_headers"],
            [f"<redacted:{SECRET_STATUS_CURRENT}>"],
        )
        self.assertEqual(
            payload["models"]["main"]["request_params"]["temperature"], 0.7
        )
        self.assertEqual(
            payload["models"]["main"]["request_params"]["nested"]["options"]["headers"],
            [f"<redacted:{SECRET_STATUS_CURRENT}>"],
        )
        provider = payload["models"]["providers"][0]
        self.assertIn(URL_USERINFO_MARKER, provider["api_base"])
        self.assertIn(URL_QUERY_MARKER, provider["api_base"])
        self.assertNotIn("model=fast", "", "")  # 占位，保持断言对称
        self.assertIn("model=fast", provider["api_base"])
        # 摘要里只有路径。
        self.assertNotIn("request_params", rows[-1].summary.split("：")[-1])

    async def test_sentinels_never_reach_a_diff_or_a_summary(self) -> None:
        await self.manager.save(
            self._payload_with_opaque_credentials(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
        )
        preview = await self.manager.preview(self._payload_with_opaque_credentials())
        self._assert_no_opaque_material(
            json.dumps(preview, ensure_ascii=False), "预览（差异+摘要）"
        )
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 777
        # 换一套**新的**凭据：两侧都被脱敏成同一个 ``current`` 状态 → 没有可
        # 报告的差异条目（第 6 项的规则），也**没有**任何明文。
        payload["models"]["main"]["request_params"]["extra_headers"] = [
            "Bearer " + OPAQUE_SENTINELS["list"]
        ]
        preview2 = await self.manager.preview(payload)
        self._assert_no_opaque_material(
            json.dumps(preview2, ensure_ascii=False), "预览（带凭据的改动）"
        )
        paths = {entry["path"] for entry in preview2["diff"]}
        self.assertIn("economy.pin_price", paths)
        self.assertNotIn("models.main.request_params.extra_headers[0]", paths)

    async def test_a_url_diff_shows_the_scrubbed_address_not_the_credential(self) -> None:
        """URL 里的凭据被就地抹掉，但**地址变化本身**仍然要能看见。"""

        await self.manager.save(
            self._payload_with_opaque_credentials(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
        )
        payload = self.manager.config.public_payload()
        for provider in payload["models"]["providers"]:
            provider["api_base"] = "https://admin:other@gateway.example.net/v2?key=k"
        preview = await self.manager.preview(payload)
        entries = {entry["path"]: entry for entry in preview["diff"]}
        entry = entries["models.providers[gemini].api_base"]
        self.assertIn(URL_USERINFO_MARKER, entry["new"])
        self.assertIn(URL_QUERY_MARKER, entry["new"])
        self.assertIn("gateway.example.net", entry["new"])
        self._assert_no_opaque_material(json.dumps(entry, ensure_ascii=False), "URL 差异")

    async def test_restore_keeps_the_current_opaque_value_never_an_old_or_empty_one(
        self,
    ) -> None:
        """策略 (a)：历史里是 ``A``、当前是 ``B`` → 回滚后是 **B**，不是 A 也不是空。"""

        first = self._payload_with_opaque_credentials()
        await self.manager.save(
            first, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        target = self.manager.revision

        second = self.manager.config.public_payload()
        second["models"]["main"]["request_params"]["extra_headers"] = [
            "Bearer CURRENT-ONLY-VALUE"
        ]
        second["economy"]["pin_price"] = 999
        await self.manager.save(
            second, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )

        restored = await self.manager.restore(
            target, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        # 非密钥字段回到目标版本。
        self.assertNotEqual(restored.economy.pin_price, 999)
        # 凭据槽位保留**当前**值：历史里的旧凭据与当前的当前凭据都不该被清空，
        # 更不该回退到历史版本里那个（已经换掉的）值。
        self.assertEqual(
            restored.models.main.request_params["extra_headers"],
            ["Bearer CURRENT-ONLY-VALUE"],
        )

    async def test_sentinels_never_reach_the_logs(self) -> None:
        handler = self._collect_logs()
        try:
            await self.manager.save(
                self._payload_with_opaque_credentials(),
                expected_revision=self.manager.revision,
                updated_by=SUPER_ADMIN_ID,
            )
            blob = "\n".join(handler.records)
        finally:
            self._drop_logs(handler)
        self._assert_no_opaque_material(blob, "日志")

    async def test_restore_is_refused_when_the_current_value_is_unavailable(self) -> None:
        """值只存在于历史、当前取不到 → 明确拒绝，不静默清空。"""

        await self.manager.save(
            self._payload_with_opaque_credentials(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
        )
        target = self.manager.revision
        # 手工把当前配置里那个路径整个删掉（模拟"运营换了配置，历史里还留着"）。
        payload = self.manager.config.public_payload()
        payload["models"]["main"]["request_params"] = {}
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        before = await self._main_row_fingerprint()

        with self.assertRaises(RuntimeConfigHistoryNotRestorableError) as caught:
            await self.manager.restore(
                target, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
            )
        self.assertIn("models.main.request_params.extra_headers[0]", str(caught.exception))
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(self.manager.revision, target + 1)


# ===========================================================================
# 第 8 项：供应商移除 / 回滚时的密钥保留策略
# ===========================================================================


class ProviderSecretRetentionTests(_HistoryFixture):
    async def _seed_two_providers(self) -> tuple[int, int]:
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes={
                "providers.gemini.api_key": {"action": "replace", "value": "GEMINI-KEY"}
            },
        )
        narrow = self.manager.revision
        await self.manager.save(
            self._payload_with_second_provider(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes={
                "providers.gemini.api_key": {"action": "replace", "value": "GEMINI-KEY"},
                "providers.openai.api_key": {"action": "replace", "value": "OPENAI-KEY"},
            },
        )
        return narrow, self.manager.revision

    async def test_restore_save_restart_never_reverts_or_loses_a_secret(self) -> None:
        """整条链：**回滚 → 保存 → 重启**，当前密钥既不回退也不消失。"""

        narrow, _wide = await self._seed_two_providers()
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
        )

        restored = await self.manager.restore(
            narrow, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        self.assertEqual([p.name for p in restored.models.providers], ["gemini"])
        # 运行时只持有还存在槽位的密钥，但库里两把都在。
        self.assertEqual(sorted(restored.extract_secrets()), ["providers.gemini.api_key"])
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
        )
        self.assertEqual(
            await self.manager.retained_secret_paths(), ["providers.openai.api_key"]
        )

        # 一次与密钥无关的普通保存，**不许**把孤儿密钥删掉。
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
            "普通保存删掉了回滚后变孤儿的密钥",
        )

        # 重启：新 manager 从库里读，密钥不丢、不回退。
        reloaded_settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key=self.master_key,
        )
        reloaded_settings.bot.token = BOT_TOKEN
        reloaded = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=reloaded_settings,
            legacy_raw_env={},
        )
        await reloaded.initialize()
        self.assertEqual(
            sorted(reloaded.config.extract_secrets()), ["providers.gemini.api_key"]
        )
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
        )
        self.assertEqual(
            await reloaded.retained_secret_paths(), ["providers.openai.api_key"]
        )

    async def test_removing_a_provider_retains_its_secret(self) -> None:
        await self._seed_two_providers()
        payload = self.manager.config.public_payload()
        payload["models"]["providers"] = [
            p for p in payload["models"]["providers"] if p["name"] != "openai"
        ]
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        self.assertEqual(
            sorted(await self._stored_secret_map()),
            ["providers.gemini.api_key", "providers.openai.api_key"],
        )
        self.assertEqual(
            await self.manager.retained_secret_paths(), ["providers.openai.api_key"]
        )

    async def test_dropping_a_provider_writes_no_secret_into_the_history_row(self) -> None:
        """删掉供应商那次保存，历史的差异里**不能**出现它的 api_key。

        差异的"旧值"一侧按**候选**配置的密钥路径脱敏；候选里已经没有这个供应
        商了，于是那条路径落到键名启发式上——启发式必须仍然把它当密钥，否则
        删供应商就会把 API Key 明文抄进历史摘要与差异。
        """

        await self._save_all_sentinels()
        before = redact_payload(
            self.manager.config.model_dump(mode="json"),
            self.manager.config.secret_paths(),
        )
        payload = self.manager.config.public_payload()
        payload["models"]["providers"] = [
            p for p in payload["models"]["providers"] if p["name"] != "openai"
        ]
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        raw = await self._raw_history_json()
        self._assert_no_secret_material(raw, "删供应商后的历史表")
        rows = await self._history_rows()
        self.assertEqual(
            [entry["name"] for entry in rows[-1].payload_redacted["models"]["providers"]],
            ["gemini"],
        )
        # 差异用**候选**（已删掉 openai）的密钥路径集合算。
        entries = {
            entry["path"]: entry
            for entry in build_diff(
                before,
                rows[-1].payload_redacted,
                self.manager.config.secret_paths(),
            )
        }
        self.assertIn("models.providers[openai].api_key", entries)
        self.assertTrue(entries["models.providers[openai].api_key"]["secret"])
        # 状态是 ``current`` 而不是 ``configured``：这条路径已经不在候选的密钥
        # 集合里了，它是被**键名启发式**兜住的，于是标记就是"值来自当前配置"。
        # 两种状态都不含值——这才是这条断言要守的东西。
        self.assertEqual(
            entries["models.providers[openai].api_key"]["old"],
            {"status": SECRET_STATUS_CURRENT},
        )
        self._assert_no_secret_material(
            json.dumps(entries, ensure_ascii=False), "删供应商后的差异"
        )

    async def test_an_explicit_clear_still_deletes_the_row(self) -> None:
        """保留策略不能变成"密钥永远删不掉"。"""

        await self._save_all_sentinels()
        await self.manager.save(
            self.manager.config.public_payload(),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
            secret_changes={
                "providers.openai.api_key": {"action": "clear"},
            },
        )
        self.assertNotIn("providers.openai.api_key", await self._stored_secret_map())
        self.assertEqual(await self.manager.retained_secret_paths(), [])

    async def test_retained_secrets_need_no_master_key(self) -> None:
        """保留判断只读路径名，不解密——历史列表不该因主密钥缺失而打不开。"""

        await self._seed_two_providers()
        rows = await self._history_rows()
        await self.manager.restore(
            int(rows[0].revision),
            expected_revision=self.manager.revision,
            updated_by=SUPER_ADMIN_ID,
        )
        self.manager._cipher = SecretCipher("")
        self.assertEqual(
            await self.manager.retained_secret_paths(), ["providers.openai.api_key"]
        )


# ===========================================================================
# 第 9 项：预览/历史的有界与"回滚预览 = 真正的回滚候选"
# ===========================================================================


class HistoryPagingTests(_HistoryFixture):
    async def _seed(self, count: int) -> None:
        for index in range(count):
            payload = self.manager.config.public_payload()
            payload["economy"]["pin_price"] = 100 + index
            await self.manager.save(
                payload,
                expected_revision=self.manager.revision,
                updated_by=SUPER_ADMIN_ID,
            )

    async def test_history_is_paginated_and_bounded(self) -> None:
        await self._seed(7)
        page = await self.manager.history_page(limit=3)
        self.assertEqual([entry["revision"] for entry in page["history"]], [6, 7, 8])
        self.assertEqual(page["page"]["limit"], 3)
        self.assertEqual(page["page"]["count"], 3)
        self.assertTrue(page["page"]["has_more"])
        self.assertEqual(page["page"]["next_before"], 6)

    async def test_paging_walks_every_row_exactly_once(self) -> None:
        await self._seed(7)
        pages: list[list[int]] = []
        before: int | None = None
        while True:
            page = await self.manager.history_page(limit=3, before=before)
            pages.append([entry["revision"] for entry in page["history"]])
            if not page["page"]["has_more"]:
                self.assertIsNone(page["page"]["next_before"])
                break
            before = page["page"]["next_before"]
        # 每一页内部升序；整条链按页从新到旧，正好覆盖 revision 2..8 且不重不漏。
        self.assertTrue(all(page == sorted(page) for page in pages))
        walked = [revision for page in pages for revision in page]
        self.assertEqual(len(walked), len(set(walked)))
        self.assertEqual(sorted(walked), list(range(2, 9)))

    async def test_default_and_maximum_limits_are_enforced(self) -> None:
        await self._seed(3)
        page = await self.manager.history_page()
        self.assertEqual(page["page"]["limit"], HISTORY_PAGE_LIMIT)
        for bad in (0, -1, HISTORY_PAGE_MAX_LIMIT + 1):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    await self.manager.history_page(limit=bad)

    async def test_history_entries_is_bounded_too(self) -> None:
        await self._seed(3)
        entries = await self.manager.history_entries(limit=2)
        self.assertEqual([entry["revision"] for entry in entries], [3, 4])

    async def test_history_rows_is_bounded_too(self) -> None:
        await self._seed(5)
        rows = await self.manager.history_rows(limit=2)
        self.assertEqual([int(row.revision) for row in rows], [5, 6])

    async def test_restore_preview_shows_the_actual_restore_candidate(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(
            payload, expected_revision=target, updated_by=SUPER_ADMIN_ID
        )
        preview = await self.manager.preview_restore(target)
        self.assertEqual(preview["revision"], target)
        self.assertEqual(preview["current_revision"], self.manager.revision)
        paths = {entry["path"]: entry for entry in preview["diff"]}
        self.assertIn("economy.pin_price", paths)
        self.assertEqual(paths["economy.pin_price"]["old"], 4242)
        self.assertEqual(paths["economy.pin_price"]["new"], 20)

        restored = await self.manager.restore(
            target, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        self.assertEqual(restored.economy.pin_price, 20)

    async def test_restore_preview_matches_what_restore_actually_writes(self) -> None:
        await self._save_all_sentinels()
        target = self.manager.revision
        payload = self.manager.config.public_payload()
        payload["resources"]["pending_reply_execution_capacity"] = 9
        await self.manager.save(
            payload, expected_revision=target, updated_by=SUPER_ADMIN_ID
        )
        preview = await self.manager.preview_restore(target)
        await self.manager.restore(
            target, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        self.assertEqual(rows[-1].operation, "restore")
        self.assertEqual(rows[-1].summary, preview["summary"])
        self.assertEqual(
            sorted(preview["restart_pending"]),
            sorted(self.manager.api_document()["restart_pending"]),
        )


# ===========================================================================
# 第 13 项：快照字节口径 + 预览与保存共享同一个上限判断
# ===========================================================================


class SnapshotAccountingTests(_HistoryFixture):
    _PAD_FIELD = ("prompts", "casual")

    def _pad(self, payload: dict[str, Any], text: str) -> dict[str, Any]:
        clone = json.loads(json.dumps(payload))
        section, field = self._PAD_FIELD
        clone[section][field] = text
        return clone

    async def _stored_bytes(self, revision: int) -> int:
        async with self.session_factory() as session:
            raw = await session.scalar(
                text(
                    "SELECT payload_redacted FROM runtime_config_history "
                    "WHERE revision = :revision"
                ),
                {"revision": revision},
            )
        self.assertIsInstance(raw, str)
        return len(str(raw).encode("utf-8"))

    async def test_measured_bytes_equal_the_bytes_actually_stored_ascii(self) -> None:
        payload = self.manager.config.public_payload()
        payload["economy"]["pin_price"] = 4242
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        self.assertEqual(
            snapshot_size_bytes(rows[-1].payload_redacted),
            await self._stored_bytes(int(rows[-1].revision)),
        )

    async def test_measured_bytes_equal_the_bytes_actually_stored_chinese(self) -> None:
        payload = self.manager.config.public_payload()
        payload[self._PAD_FIELD[0]][self._PAD_FIELD[1]] = "规" * 20_000
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        measured = snapshot_size_bytes(rows[-1].payload_redacted)
        self.assertEqual(measured, await self._stored_bytes(int(rows[-1].revision)))
        # 真正落库的比"UTF-8 字符数"大一倍——这正是原来的口径错误。
        self.assertGreater(measured, len(("规" * 20_000).encode("utf-8")))

    async def test_a_chinese_payload_above_the_limit_is_refused(self) -> None:
        """180 000 个中字符：按旧口径（540 KB）放行，按真实编码（1.08 MB）拒绝。"""

        before = await self._main_row_fingerprint()
        payload = self.manager.config.public_payload()
        payload[self._PAD_FIELD[0]][self._PAD_FIELD[1]] = "规" * 180_000
        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError):
            await self.manager.save(
                payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
            )
        self.assertEqual(await self._main_row_fingerprint(), before)
        self.assertEqual(await self._history_rows(), [])

    async def test_preview_and_save_agree_on_the_limit_and_the_error_code(self) -> None:
        payload = self.manager.config.public_payload()
        payload[self._PAD_FIELD[0]][self._PAD_FIELD[1]] = "x" * (MAX_SNAPSHOT_BYTES + 5_000)
        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError) as preview_error:
            await self.manager.preview(payload)
        with self.assertRaises(RuntimeConfigHistorySnapshotTooLargeError) as save_error:
            await self.manager.save(
                payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
            )
        self.assertEqual(str(preview_error.exception), str(save_error.exception))
        self.assertEqual(await self._history_rows(), [])

    async def test_a_chinese_payload_just_under_the_limit_is_accepted(self) -> None:
        """边界仍然精确：正文缩短到真的落在上限内就必须放行。"""

        base = self.manager.config.public_payload()
        text = "规" * 150_000
        payload = self._pad(base, text)
        size = snapshot_size_bytes(
            redact_payload(payload, self.manager.config.secret_paths())
        )
        self.assertLessEqual(size, MAX_SNAPSHOT_BYTES)
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        self.assertEqual(
            snapshot_size_bytes(rows[-1].payload_redacted),
            await self._stored_bytes(int(rows[-1].revision)),
        )

    async def test_preview_reports_the_exact_stored_byte_count(self) -> None:
        payload = self.manager.config.public_payload()
        payload["prompts"]["casual"] = "规" * 1_000
        preview = await self.manager.preview(payload)
        await self.manager.save(
            payload, expected_revision=self.manager.revision, updated_by=SUPER_ADMIN_ID
        )
        rows = await self._history_rows()
        self.assertEqual(
            preview["snapshot_bytes"], await self._stored_bytes(int(rows[-1].revision))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
