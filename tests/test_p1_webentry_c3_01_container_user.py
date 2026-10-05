"""修复批 P1-3 / C3-01：compose 的 ``user:`` 覆盖了镜像里的非 root 账号。

``Dockerfile`` 写了 ``USER app:app``（uid/gid 10001），但 compose 里::

    # docker-compose.yml:6（修前）
    user: "${APP_UID:-1000}:${APP_GID:-1000}"

把这条不变量在**默认部署路径**上直接抹掉；而 README 建议的 ``APP_UID="$(id -u)"``
在 root 宿主上就是 0，容器于是以 root 运行 + ``./data`` 可写绑定卷 + 对外端口，
**没有任何断言或告警**。

修法：compose 改成 ``${APP_UID:?...}:${APP_GID:?...}``（空值即报错，不静默回落），
并在启动日志里打印实际身份、root 时 WARNING。

本机无 Docker，所以 compose 侧是**配置级证据**（未实测运行时效果）；启动日志
侧是真跑的（直接调用被测函数并断言日志级别）。
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import log_process_identity

ROOT = Path(__file__).resolve().parent.parent


class ComposeUserIdentityTests(unittest.TestCase):
    def test_compose_refuses_to_silently_pick_a_container_user(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn('user: "${APP_UID:-', compose)
        self.assertIn('user: "${APP_UID:?', compose)
        self.assertIn("${APP_GID:?", compose)

    def test_compose_still_mentions_the_image_account_as_the_documented_value(
        self,
    ) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("APP_UID=10001 APP_GID=10001", compose)

    def test_dockerfile_still_declares_the_non_root_user(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^USER\s+app:app\s*$")


class StartupIdentityLogTests(unittest.TestCase):
    def _records(self, uid: int) -> list[logging.LogRecord]:
        with self.assertLogs("bot.config", level=logging.INFO) as captured:
            with patch("os.getuid", return_value=uid), patch(
                "os.getgid", return_value=4242
            ):
                log_process_identity()
        return captured.records

    def test_root_identity_is_warned_about(self) -> None:
        records = self._records(0)
        self.assertEqual(records[0].levelno, logging.WARNING)
        self.assertIn("root", records[0].getMessage())

    def test_non_root_identity_is_logged_at_info(self) -> None:
        records = self._records(10001)
        self.assertEqual(records[0].levelno, logging.INFO)
        message = records[0].getMessage()
        self.assertIn("uid=10001", message)
        self.assertIn("gid=4242", message)
        self.assertIn("非 root", message)
