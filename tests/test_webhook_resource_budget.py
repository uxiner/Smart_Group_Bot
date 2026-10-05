"""webhook / 轮询 / 出站发送预算：**真消费** + 租约关联约束的强校验。

这一段的风险点和别处不同：改错不会立刻报错，而是**在生产上重复执行管理员回调**。
所以这里重点钉两件事：

1. 改非默认值之后，``_WebhookUpdateQueue`` / durable inbox / 轮询 / 发送并发
   真的读到新值——不是只 assert schema 存了数。
2. 非法关联（租约短于端到端预算、队列比车道小、车道比总池大、HTTP 响应超时短于
   端到端预算）**在保存时就被拒绝**，不会等到线上。
"""

from __future__ import annotations

import unittest

from bot.config import Settings
from bot.services import policy_runtime
from bot.services.runtime_config import RuntimeConfig


def _bind(resources: dict) -> RuntimeConfig:
    base = RuntimeConfig()
    payload = base.storage_payload()
    payload["resources"] = {**base.resources.model_dump(), **resources}
    config = RuntimeConfig.model_validate(payload)
    settings = Settings(_env_file=None)
    config.apply_to_settings(settings, apply_prompts=False)
    policy_runtime.bind(settings)
    return config


class WebhookBudgetConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        policy_runtime.unbind()

    def tearDown(self) -> None:
        policy_runtime.unbind()

    def test_defaults_equal_the_pre_change_constants(self) -> None:
        from bot.services.verify_web import webhook_budget

        self.assertEqual(
            webhook_budget(),
            {
                "max_concurrent_updates": 8,
                "critical_concurrent_updates": 4,
                "security_concurrent_updates": 4,
                "auth_concurrent_updates": 2,
                "critical_queue_capacity": 64,
                "security_queue_capacity": 128,
                "auth_queue_capacity": 64,
                "update_timeout_seconds": 150.0,
                "critical_update_timeout_seconds": 60.0,
                "security_update_timeout_seconds": 120.0,
                "auth_update_timeout_seconds": 45.0,
                "http_response_timeout_seconds": 155.0,
            },
        )

    def test_configured_values_really_reach_the_consumers(self) -> None:
        from bot.services.update_delivery import polling_limits
        from bot.services.verify_web import inbox_limits, webhook_budget
        from bot.utils.telegram import chat_send_parallel

        _bind(
            {
                "webhook_max_concurrent_updates": 16,
                "webhook_critical_concurrent_updates": 8,
                "webhook_critical_queue_capacity": 256,
                "webhook_update_timeout_seconds": 200.0,
                "webhook_http_response_timeout_seconds": 260.0,
                "webhook_inbox_lease_seconds": 400.0,
                "webhook_inbox_recovery_batch": 32,
                "webhook_inbox_retry_max_seconds": 120.0,
                "webhook_inbox_cleanup_batch": 512,
                "polling_timeout_seconds": 25,
                "polling_request_timeout_seconds": 60.0,
                "telegram_send_chat_parallel": 6,
            }
        )
        budget = webhook_budget()
        self.assertEqual(budget["max_concurrent_updates"], 16)
        self.assertEqual(budget["critical_concurrent_updates"], 8)
        self.assertEqual(budget["critical_queue_capacity"], 256)
        self.assertEqual(budget["update_timeout_seconds"], 200.0)
        self.assertEqual(budget["http_response_timeout_seconds"], 260.0)
        self.assertEqual(inbox_limits()["lease_seconds"], 400.0)
        self.assertEqual(inbox_limits()["recovery_batch"], 32)
        self.assertEqual(inbox_limits()["retry_max_seconds"], 120.0)
        self.assertEqual(inbox_limits()["cleanup_batch"], 512)
        self.assertEqual(polling_limits()["timeout_seconds"], 25)
        self.assertEqual(polling_limits()["request_timeout_seconds"], 60.0)
        self.assertEqual(polling_limits()["webhook_max_connections"], 16)
        self.assertEqual(chat_send_parallel(), 6)

    def test_the_queue_really_follows_the_config_and_ignores_the_old_constant(self) -> None:
        """读的是配置，不是模块常量。

        这条比"读到了新值"更强：把旧常量打成一个完全不同的数，构造出来的 worker
        数量仍必须由配置决定——否则就还有人靠改源码常量来调容量。
        """

        import asyncio
        from unittest.mock import patch

        from bot.services import verify_web as verify_web_module
        from bot.services.verify_web import _WebhookUpdateQueue

        class _NullDispatcher:
            async def feed_update(self, _bot, _update, **_kwargs: object) -> None:
                return None

        def _build_sync() -> "_WebhookUpdateQueue":
            budget = verify_web_module.webhook_budget()
            return _WebhookUpdateQueue(
                dispatcher=_NullDispatcher(),  # type: ignore[arg-type]
                bot=None,  # type: ignore[arg-type]
                session_factory=None,
                secret_token="t",
                worker_count=int(budget["max_concurrent_updates"]),
                auth_worker_count=int(budget["auth_concurrent_updates"]),
                critical_worker_count=int(budget["critical_concurrent_updates"]),
                security_worker_count=int(budget["security_concurrent_updates"]),
                critical_queue_capacity=int(budget["critical_queue_capacity"]),
                security_queue_capacity=int(budget["security_queue_capacity"]),
                auth_queue_capacity=int(budget["auth_queue_capacity"]),
            )

        def _lane_sizes() -> tuple[int, int, int, int, int]:
            """worker 数与队列容量——服务端构造时真正写进对象的那几个值。"""

            queue = _build_sync()
            return (
                queue.worker_count,
                queue.critical_worker_count,
                queue.security_worker_count,
                queue.auth_worker_count,
                queue.queue._critical.maxsize,
            )

        policy_runtime.unbind()
        # 旧常量被改成完全不同的数，构造结果必须纹丝不动。
        with (
            patch.object(verify_web_module, "WEBHOOK_MAX_CONCURRENT_UPDATES", 99),
            patch.object(verify_web_module, "WEBHOOK_CRITICAL_CONCURRENT_UPDATES", 77),
        ):
            default = _lane_sizes()
        self.assertEqual(
            default, (8, 4, 4, 2, 64), "默认值必须与改造前逐字相同"
        )

        _bind(
            {
                "webhook_max_concurrent_updates": 5,
                "webhook_critical_concurrent_updates": 3,
            }
        )
        with (
            patch.object(verify_web_module, "WEBHOOK_MAX_CONCURRENT_UPDATES", 99),
            patch.object(verify_web_module, "WEBHOOK_CRITICAL_CONCURRENT_UPDATES", 77),
        ):
            configured = _lane_sizes()
        self.assertEqual(
            configured[:4],
            (5, 3, 4, 2),
            "车道大小必须真的跟着运行时配置走，而不是模块常量",
        )


class WebhookCorrelationTests(unittest.TestCase):
    """非法关联必须在保存时就被拒绝。"""

    def setUp(self) -> None:
        policy_runtime.unbind()

    def _reject(self, **resources: object) -> None:
        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["resources"] = {**base.resources.model_dump(), **resources}
        with self.assertRaises(Exception, msg=f"{resources} 本应被拒绝"):
            RuntimeConfig.model_validate(payload)

    def test_lease_shorter_than_the_longest_update_budget_is_rejected(self) -> None:
        # 这正是"重复处理管理员回调"的成因：租约先到期，恢复循环抢走仍在跑的 update。
        self._reject(webhook_inbox_lease_seconds=100.0)  # < 150s
        self._reject(webhook_inbox_lease_seconds=30.0)
        # 相等是允许的（同时仍要满足"租约 >= 最大重试退避"）。
        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["resources"] = {
            **base.resources.model_dump(),
            "webhook_inbox_lease_seconds": 150.0,
            "webhook_inbox_retry_max_seconds": 150.0,
        }
        RuntimeConfig.model_validate(payload)

    def test_lease_shorter_than_retry_backoff_is_rejected(self) -> None:
        self._reject(
            webhook_inbox_lease_seconds=200.0, webhook_inbox_retry_max_seconds=300.0
        )

    def test_http_response_timeout_shorter_than_update_budget_is_rejected(self) -> None:
        self._reject(webhook_http_response_timeout_seconds=100.0)

    def test_a_lane_may_not_exceed_the_total_pool(self) -> None:
        self._reject(webhook_max_concurrent_updates=2, webhook_critical_concurrent_updates=4)
        self._reject(webhook_max_concurrent_updates=2, webhook_security_concurrent_updates=4)
        self._reject(webhook_max_concurrent_updates=2, webhook_auth_concurrent_updates=4)

    def test_a_queue_may_not_be_smaller_than_its_lane(self) -> None:
        self._reject(webhook_critical_concurrent_updates=8, webhook_critical_queue_capacity=4)
        self._reject(webhook_security_concurrent_updates=8, webhook_security_queue_capacity=4)
        self._reject(webhook_auth_concurrent_updates=8, webhook_auth_queue_capacity=4)

    def test_the_shipped_defaults_satisfy_every_constraint(self) -> None:
        resources = RuntimeConfig().resources
        longest = max(
            resources.webhook_update_timeout_seconds,
            resources.webhook_critical_update_timeout_seconds,
            resources.webhook_security_update_timeout_seconds,
            resources.webhook_auth_update_timeout_seconds,
        )
        self.assertGreaterEqual(resources.webhook_inbox_lease_seconds, longest)
        self.assertGreaterEqual(
            resources.webhook_inbox_lease_seconds,
            resources.webhook_inbox_retry_max_seconds,
        )
        self.assertGreaterEqual(
            resources.webhook_http_response_timeout_seconds, longest
        )
        for workers, capacity in (
            (resources.webhook_critical_concurrent_updates, resources.webhook_critical_queue_capacity),
            (resources.webhook_security_concurrent_updates, resources.webhook_security_queue_capacity),
            (resources.webhook_auth_concurrent_updates, resources.webhook_auth_queue_capacity),
        ):
            self.assertLessEqual(workers, resources.webhook_max_concurrent_updates)
            self.assertGreaterEqual(capacity, workers)


class PollingFallbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        policy_runtime.unbind()

    def test_polling_fallback_is_wired_and_bounded(self) -> None:
        from bot.services.update_delivery import polling_limits

        policy_runtime.unbind()
        limits = polling_limits()
        self.assertEqual(limits["timeout_seconds"], 15)
        self.assertEqual(limits["http_timeout_seconds"], 30)
        self.assertEqual(limits["request_timeout_seconds"], 35.0)
        # 轮询兜底也必须有界：三个值都不能是非正数。
        for key in limits:
            self.assertGreater(float(limits[key]), 0)

    def test_unknown_keys_are_still_rejected(self) -> None:
        import pydantic

        payload = RuntimeConfig().storage_payload()
        payload["resources"]["not_a_real_webhook_field"] = 1
        with self.assertRaises(pydantic.ValidationError):
            RuntimeConfig.model_validate(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
