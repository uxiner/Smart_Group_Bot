"""Mini App / 入群验证 HTTP 服务的监听地址安全默认值（F-017）。

该服务承载 /verify、/settings 以及**免鉴权**的 /healthz：代码默认必须是回环，
只有容器内需要被宿主机端口映射命中时才显式绑 0.0.0.0，并且启动日志必须明确
告警（否则一次部署配置就把管理面暴露给同网段/公网）。
"""

from __future__ import annotations

import functools
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import bot.config as config_module
from bot.config import Settings, load_bootstrap_settings
from bot.services import verify_web as verify_web_module
from bot.services.verify_web import (
    _listen_host_is_loopback,
    _warn_if_listen_host_is_public,
)

ROOT = Path(__file__).resolve().parent.parent


class ListenHostDefaultTests(unittest.TestCase):
    def test_declared_field_default_is_loopback(self) -> None:
        field = Settings.model_fields["join_verification_listen_host"]
        self.assertEqual(field.default, "127.0.0.1")

    def test_settings_default_is_loopback_without_env(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.join_verification_listen_host, "127.0.0.1")

    def test_bootstrap_falls_back_to_loopback_when_unset(self) -> None:
        cleared = {
            key: value
            for key, value in os.environ.items()
            if key not in ("MINIAPP_LISTEN_HOST", "JOIN_VERIFICATION_LISTEN_HOST")
        }
        with (
            patch.dict(os.environ, cleared, clear=True),
            patch.object(
                config_module,
                "Settings",
                functools.partial(Settings, _env_file=None),
            ),
        ):
            settings = load_bootstrap_settings()

        self.assertEqual(settings.miniapp_listen_host, "127.0.0.1")
        self.assertEqual(settings.join_verification_listen_host, "127.0.0.1")

    def test_compose_keeps_the_container_listener_reachable(self) -> None:
        """回归：容器里必须仍能绑 0.0.0.0，否则端口映射会打空。"""

        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            'MINIAPP_LISTEN_HOST: "${MINIAPP_LISTEN_HOST:-0.0.0.0}"',
            compose,
        )
        self.assertIn(
            "${MINIAPP_BIND_ADDRESS:-127.0.0.1}:"
            "${MINIAPP_LISTEN_PORT:-8480}:${MINIAPP_LISTEN_PORT:-8480}",
            compose,
        )


class ListenHostWarningTests(unittest.TestCase):
    def test_loopback_hosts_are_recognized(self) -> None:
        for host in ("127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertTrue(_listen_host_is_loopback(host))

    def test_public_or_wildcard_hosts_are_not_loopback(self) -> None:
        for host in ("0.0.0.0", "::", "", None, "  ", "192.168.1.10", "bot.example.com"):
            with self.subTest(host=host):
                self.assertFalse(_listen_host_is_loopback(host))

    def test_non_loopback_bind_warns_at_startup(self) -> None:
        with patch.object(verify_web_module.log, "warning") as warning:
            _warn_if_listen_host_is_public("0.0.0.0", 8480)

        warning.assert_called_once()
        rendered = warning.call_args.args[0] % tuple(warning.call_args.args[1:])
        self.assertIn("0.0.0.0:8480", rendered)
        self.assertIn("/healthz", rendered)

    def test_loopback_bind_is_silent(self) -> None:
        with patch.object(verify_web_module.log, "warning") as warning:
            _warn_if_listen_host_is_public("127.0.0.1", 8480)

        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
