"""修复批 P1-3 / C3-03：compose healthcheck 的端口探测与应用实际端口解析链一致。

应用的端口解析链是 ``MINIAPP_LISTEN_PORT → JOIN_VERIFICATION_LISTEN_PORT（遗留别名，
README 明确说继续接受以免改反代）→ 8480``（``bot/config.py:1466-1476``）。而
healthcheck 过去写死::

    # docker-compose.yml:36-48（修前）
    port = os.environ.get('MINIAPP_LISTEN_PORT', '8480')

于是 .env 用遗留变量名时：应用监听 A 端口、healthcheck 探 8480、端口映射也固定
8480 → **容器永远 unhealthy 而进程健康**，编排会错误摘流量。

修法：healthcheck 改为调用应用自己的 ``load_bootstrap_settings()`` 取解析后的端口。

本机无 Docker，compose 的运行时效果未实测；这里给两层证据：
1. **配置级**：healthcheck 命令行里确实走的是应用解析函数，且不再有写死的 env 读取；
2. **行为级**：真起子进程跑 healthcheck 里那一行，分别喂新旧/遗留变量，断言它
   返回应用真正会绑定的端口。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: compose healthcheck 里的那一行（本文件对 docker-compose.yml 的断言保证它们一致）
PROBE = (
    "from bot.config import load_bootstrap_settings as s; "
    "print(s().miniapp_listen_port)"
)


def _resolve_port(env: dict[str, str]) -> int:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    env.update(overrides)
    return env


class HealthcheckPortResolutionTests(unittest.TestCase):
    def test_compose_healthcheck_asks_the_application_for_the_port(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("load_bootstrap_settings as s", compose)
        self.assertIn("port = s().miniapp_listen_port;", compose)
        # 写死的 env 读取必须消失：它只知道 MINIAPP_LISTEN_PORT 一条腿。
        self.assertNotIn("os.environ.get('MINIAPP_LISTEN_PORT'", compose)
        self.assertNotIn("os.environ.get('JOIN_VERIFICATION_LISTEN_PORT'", compose)

    def test_legacy_alias_alone_resolves_to_the_port_the_app_binds(self) -> None:
        """只用遗留别名时，探测端口必须等于应用真正绑定的端口。"""

        self.assertEqual(
            _resolve_port(_clean_env(JOIN_VERIFICATION_LISTEN_PORT="9411")),
            9411,
        )

    def test_modern_variable_still_wins_over_the_legacy_alias(self) -> None:
        self.assertEqual(
            _resolve_port(
                _clean_env(
                    MINIAPP_LISTEN_PORT="8480",
                    JOIN_VERIFICATION_LISTEN_PORT="9411",
                )
            ),
            8480,
        )

    def test_default_is_8480_when_neither_variable_is_set(self) -> None:
        self.assertEqual(_resolve_port(_clean_env()), 8480)

    def test_old_hardcoded_probe_would_have_missed_the_legacy_port(self) -> None:
        """钉住这条 finding 的根因：写死 8480 的探测在遗留别名下必然探错端口。"""

        import os

        env = _clean_env(JOIN_VERIFICATION_LISTEN_PORT="9411")
        old_probe = int(
            os.environ.get("MINIAPP_LISTEN_PORT", "8480")  # 旧写法（compose 内）
        )
        self.assertEqual(old_probe, 8480)
        self.assertNotEqual(old_probe, 9411)
