from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DeploymentConfigTests(unittest.TestCase):
    def test_compose_has_safe_network_identity_and_shutdown_defaults(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('user: "${APP_UID:-1000}:${APP_GID:-1000}"', compose)
        self.assertIn("init: true", compose)
        self.assertIn(
            "${MINIAPP_BIND_ADDRESS:-127.0.0.1}:"
            "${MINIAPP_LISTEN_PORT:-8480}:${MINIAPP_LISTEN_PORT:-8480}",
            compose,
        )
        self.assertIn("os.environ.get('MINIAPP_LISTEN_PORT', '8480')", compose)
        self.assertRegex(compose, r"(?m)^\s*stop_grace_period:\s*125s\s*$")

    def test_compose_bounds_swap_fds_pids_and_logs(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('mem_limit: "${BOT_MEMORY_LIMIT:-1536m}"', compose)
        self.assertIn(
            'memswap_limit: "${BOT_MEMORY_SWAP_LIMIT:-1536m}"',
            compose,
        )
        self.assertIn("pids_limit: ${BOT_PIDS_LIMIT:-128}", compose)
        self.assertRegex(compose, r"(?m)^\s*nofile:\s*$")
        self.assertRegex(compose, r"(?m)^\s*soft:\s*8192\s*$")
        self.assertRegex(compose, r"(?m)^\s*hard:\s*8192\s*$")
        self.assertIn("driver: local", compose)

    def test_image_runs_non_root_and_uses_locked_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^FROM python:3\.12\.\d+-slim-(?:bookworm|trixie)"
            r"@sha256:[0-9a-f]{64}\s*$",
        )
        self.assertIn("COPY requirements.lock", dockerfile)
        self.assertIn("--requirement requirements.lock", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER\s+app:app\s*$")
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+\.\s+\.\s*$")
        self.assertNotIn("COPY --chown=app:app config.toml", dockerfile)

    def test_lockfiles_are_present_and_not_ignored(self) -> None:
        self.assertTrue((ROOT / "uv.lock").is_file())
        requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "--"))
        ]
        self.assertGreater(len(pins), 20)
        self.assertTrue(all("==" in line or line.startswith(("-e ", ".")) for line in pins))
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertNotRegex(gitignore, r"(?m)^uv\.lock\s*$")

    def test_lockfiles_pin_the_fixed_advisory_versions(self) -> None:
        """F-015：OSV 命中的四个包必须锁在「已修复的最低版本」或更高。

        升级只做最小位移（不顺手抬大版本）；同时 ``requirements.lock`` 里那两个
        uv.lock 没有的本地补充（``edge-tts`` 是 TTS 兜底）不能被重新导出时悄悄
        删掉——见 README 的「本地调整」。
        """

        def parsed(text: str) -> dict[str, str]:
            pins: dict[str, str] = {}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "==" not in line:
                    continue
                name, _, rest = line.partition("==")
                pins[name.strip().lower()] = rest.split(";")[0].strip()
            return pins

        def version_tuple(raw: str) -> tuple[int, ...]:
            parts: list[int] = []
            for chunk in str(raw).split("."):
                digits = "".join(ch for ch in chunk if ch.isdigit())
                parts.append(int(digits or 0))
            return tuple(parts)

        requirements = parsed((ROOT / "requirements.lock").read_text(encoding="utf-8"))
        uv_lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
        fixed_minimums = {
            "aiohttp": "3.14.3",
            "urllib3": "2.8.0",
            "cryptography": "50.0.0",
            "h2": "4.4.1",
        }
        for name, minimum in fixed_minimums.items():
            self.assertIn(name, requirements, name)
            self.assertGreaterEqual(
                version_tuple(requirements[name]),
                version_tuple(minimum),
                f"{name} 必须 >= {minimum}（OSV 已修复版本）",
            )
            # uv.lock 与 requirements.lock 必须同步，否则镜像里装的还是旧版本。
            self.assertIn(f'name = "{name}"', uv_lock)
            self.assertRegex(
                uv_lock,
                rf'name = "{name}"\nversion = "{requirements[name]}"',
            )
        self.assertIn("edge-tts", requirements)

    def test_docker_context_excludes_secrets_and_runtime_state(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for required in (
            ".env",
            ".env.*",
            "config.toml",
            "data/",
            "*.db",
            "*.log*",
            "tests/",
        ):
            self.assertIn(required, dockerignore)


if __name__ == "__main__":
    unittest.main()
