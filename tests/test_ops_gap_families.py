"""缺口补充：管理端 / 群内视觉与活跃度 / 出站发送三个族的**真实消费**。

这些参数以前是模块常量。改了非默认值之后，下面每条都必须真的变——不是只 assert
schema 存了数。同时钉住父代理要求保留为**固定**的邻居（防冒充词表、协议上限、
健康看门狗、settings 端 JSON 体积上限、入群验证终态与租约），确保这一轮**没有**
顺手把它们做成开关。
"""

from __future__ import annotations

import unittest

from bot.config import Settings
from bot.services import policy_runtime
from bot.services.runtime_config import RuntimeConfig


def _bind(sections: dict) -> RuntimeConfig:
    base = RuntimeConfig()
    payload = base.storage_payload()
    for section, values in sections.items():
        payload[section] = {**payload[section], **values}
    config = RuntimeConfig.model_validate(payload)
    settings = Settings(_env_file=None)
    config.apply_to_settings(settings, apply_prompts=False)
    policy_runtime.bind(settings)
    return config


class AdminOpsConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        policy_runtime.unbind()

    def tearDown(self) -> None:
        policy_runtime.unbind()

    def test_defaults_match_the_pre_change_constants(self) -> None:
        from bot.handlers import admin

        self.assertEqual(admin.list_page_size(), 5)
        self.assertEqual(admin.roster_notice_auto_delete_seconds(), 5)
        ops = admin.admin_ops()
        self.assertEqual(ops.privileged_group_concurrency, 4)
        self.assertEqual(ops.privileged_group_deadline_seconds, 45.0)
        self.assertEqual(ops.privileged_job_deadline_seconds, 300.0)

    def test_configured_values_reach_the_consumers(self) -> None:
        from bot.handlers import admin

        _bind(
            {
                "admin_ops": {
                    "list_page_size": 12,
                    "roster_notice_auto_delete_seconds": 0,
                    "privileged_group_concurrency": 9,
                    "privileged_group_deadline_seconds": 60.0,
                    "privileged_job_deadline_seconds": 420.0,
                }
            }
        )
        self.assertEqual(admin.list_page_size(), 12)
        self.assertEqual(admin.roster_notice_auto_delete_seconds(), 0)
        ops = admin.admin_ops()
        self.assertEqual(ops.privileged_group_concurrency, 9)
        self.assertEqual(ops.privileged_group_deadline_seconds, 60.0)
        self.assertEqual(ops.privileged_job_deadline_seconds, 420.0)

    def test_the_job_deadline_must_exceed_the_per_group_deadline(self) -> None:
        import pydantic

        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["admin_ops"] = {
            **base.admin_ops.model_dump(),
            "privileged_group_deadline_seconds": 100.0,
            "privileged_job_deadline_seconds": 100.0,
        }
        with self.assertRaises(pydantic.ValidationError):
            RuntimeConfig.model_validate(payload)

    def test_page_size_and_notice_delay_bounds(self) -> None:
        import pydantic

        base = RuntimeConfig()
        for bad in (
            {"list_page_size": 0},
            {"list_page_size": 51},
            {"roster_notice_auto_delete_seconds": -1},
            {"roster_notice_auto_delete_seconds": 3601},
            {"privileged_group_concurrency": 0},
            {"privileged_group_concurrency": 17},
        ):
            with self.subTest(bad=bad):
                payload = base.storage_payload()
                payload["admin_ops"] = {**base.admin_ops.model_dump(), **bad}
                with self.assertRaises(pydantic.ValidationError):
                    RuntimeConfig.model_validate(payload)


class GroupOpsConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        policy_runtime.unbind()

    def tearDown(self) -> None:
        policy_runtime.unbind()

    def test_defaults_match_the_pre_change_constants(self) -> None:
        from bot.handlers import group

        ops = group.group_ops()
        self.assertEqual(ops.vision_image_max_bytes, 5 * 1024 * 1024)
        self.assertEqual(ops.vision_download_timeout_seconds, 20.0)
        self.assertEqual(ops.vision_text_max_chars, 800)
        self.assertEqual(ops.reply_targets_max_chars, 4000)
        self.assertEqual(ops.nsfw_warning_auto_delete_seconds, 120)
        self.assertEqual(ops.activity_debounce_seconds, 1.0)
        self.assertEqual(ops.activity_max_attempts, 8)

    def test_configured_values_reach_the_consumers(self) -> None:
        from bot.handlers import group

        _bind(
            {
                "group_ops": {
                    "vision_image_max_bytes": 1024 * 1024,
                    "vision_download_timeout_seconds": 9.0,
                    "vision_text_max_chars": 300,
                    "reply_targets_max_chars": 1000,
                    "nsfw_warning_auto_delete_seconds": 0,
                    "activity_debounce_seconds": 0.2,
                    "activity_max_attempts": 3,
                }
            }
        )
        ops = group.group_ops()
        self.assertEqual(ops.vision_image_max_bytes, 1024 * 1024)
        self.assertEqual(ops.vision_download_timeout_seconds, 9.0)
        self.assertEqual(ops.vision_text_max_chars, 300)
        self.assertEqual(ops.reply_targets_max_chars, 1000)
        self.assertEqual(ops.nsfw_warning_auto_delete_seconds, 0)
        self.assertEqual(ops.activity_debounce_seconds, 0.2)
        self.assertEqual(ops.activity_max_attempts, 3)

    def test_vision_byte_cap_is_a_memory_safety_ceiling(self) -> None:
        """体积上限只可收紧：0 不表示"无限"，20MiB 是上界。"""

        import pydantic

        base = RuntimeConfig()
        for bad in (
            {"vision_image_max_bytes": 0},          # 0 != 无限
            {"vision_image_max_bytes": 1024},        # 低于 256KiB 下界
            {"vision_image_max_bytes": 20 * 1024 * 1024 + 1},  # 超过上界
        ):
            with self.subTest(bad=bad):
                payload = base.storage_payload()
                payload["group_ops"] = {**base.group_ops.model_dump(), **bad}
                with self.assertRaises(pydantic.ValidationError):
                    RuntimeConfig.model_validate(payload)
        # 上界本身是合法的
        payload = base.storage_payload()
        payload["group_ops"] = {
            **base.group_ops.model_dump(),
            "vision_image_max_bytes": 20 * 1024 * 1024,
        }
        self.assertEqual(
            RuntimeConfig.model_validate(payload).group_ops.vision_image_max_bytes,
            20 * 1024 * 1024,
        )

    def test_reply_target_cap_cannot_be_raised(self) -> None:
        import pydantic

        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["group_ops"] = {**base.group_ops.model_dump(), "reply_targets_max_chars": 8000}
        with self.assertRaises(pydantic.ValidationError):
            RuntimeConfig.model_validate(payload)


class TelegramSendConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        policy_runtime.unbind()

    def tearDown(self) -> None:
        policy_runtime.unbind()

    def test_defaults_match_the_pre_change_constants(self) -> None:
        from bot.utils import telegram

        budget = telegram.send_budget()
        self.assertEqual(budget.send_total_deadline_seconds, 60.0)
        self.assertEqual(budget.stream_max_incremental_edits, 12)
        self.assertEqual(budget.stream_max_pacing_seconds, 8.0)
        self.assertEqual(budget.typing_send_timeout_seconds, 3.0)
        self.assertEqual(telegram.chat_send_parallel(), 3)

    def test_configured_values_reach_the_consumers(self) -> None:
        from bot.utils import telegram

        _bind(
            {
                "telegram_send": {
                    "send_total_deadline_seconds": 120.0,
                    "stream_max_incremental_edits": 20,
                    "stream_max_pacing_seconds": 20.0,
                    "typing_send_timeout_seconds": 1.0,
                }
            }
        )
        budget = telegram.send_budget()
        self.assertEqual(budget.send_total_deadline_seconds, 120.0)
        self.assertEqual(budget.stream_max_incremental_edits, 20)
        self.assertEqual(budget.stream_max_pacing_seconds, 20.0)
        self.assertEqual(budget.typing_send_timeout_seconds, 1.0)

    def test_send_deadline_must_exceed_the_session_http_timeout(self) -> None:
        import pydantic

        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["telegram_send"] = {
            **base.telegram_send.model_dump(),
            "send_total_deadline_seconds": 20.0,  # < 30s 的 session 超时
        }
        with self.assertRaises(pydantic.ValidationError):
            RuntimeConfig.model_validate(payload)

    def test_the_tls_backoff_stays_a_protocol_constant(self) -> None:
        """父代理边界：TLS 记录重试退避是协议级常量，**不**做成配置。"""

        from bot.utils import telegram

        self.assertEqual(telegram.TG_TLS_RECORD_RETRY_DELAY, 0.35)
        fields = set(RuntimeConfig().telegram_send.model_fields)
        self.assertNotIn("tls_record_retry_delay", fields)


class KeptFixedFamiliesTests(unittest.TestCase):
    """父代理点名的「保留固定」邻居：这一轮没有把它们变成开关。"""

    def test_security_and_protocol_constants_are_not_in_any_section(self) -> None:
        from bot.services import point_shop, resource_health
        from bot.services import verify_web as verify_web_module
        from bot.services.join_verification import (
            CHALLENGE_SUBMIT_GRACE,
            PREPARING_LEASE_SECONDS,
            TERMINAL_LEASE_SECONDS,
        )
        from bot.utils import telegram

        base = RuntimeConfig()
        all_fields: set[str] = set()
        for section in base.model_fields.values():
            model = getattr(section, "model_fields", None) or getattr(
                getattr(section, "annotation", None), "__args__", ()
            )
            for candidate in model:
                all_fields.update(getattr(candidate, "model_fields", {}) or {})
        all_fields.update(base.admin_ops.model_fields)
        all_fields.update(base.group_ops.model_fields)
        all_fields.update(base.telegram_send.model_fields)
        all_fields.update(base.resources.model_fields)
        all_fields.update(base.economy.model_fields)

        for forbidden, why in (
            ("title_blocklist", "头衔防冒充词表：配置不能删掉它"),
            ("tls_record_retry_delay", "TLS 退避是协议级"),
            ("challenge_submit_grace", "入群验证的防重放窗口是安全不变量"),
            ("preparing_lease_seconds", "准备中租约是并发正确性前提"),
            ("terminal_lease_seconds", "终态租约是并发正确性前提"),
            ("json_body_max_bytes", "settings 端请求体上限是 DoS 内存防线"),
            ("health_fatal_threshold", "看门狗 fatal 阈值：放松等于关掉守护"),
            ("message_max_chars", "Telegram 消息长度是协议硬上限"),
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, all_fields, f"{forbidden} 不该被做成配置：{why}")

        # 值本身也保持原样
        self.assertEqual(telegram.TG_MESSAGE_LIMIT, 4096)
        self.assertEqual(verify_web_module._WEB_MAX_REQUEST_BYTES, 1024 * 1024)
        self.assertEqual(point_shop.TAG_MAX_LENGTH, 16)
        self.assertEqual(CHALLENGE_SUBMIT_GRACE.total_seconds(), 60)
        self.assertEqual(TERMINAL_LEASE_SECONDS, 90)
        self.assertEqual(PREPARING_LEASE_SECONDS, 90)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
