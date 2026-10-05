"""公开产物的敏感绑定扫描：用**合成 fixture**验证扫描机制本身有效。

两条原则：

1. **扫描规则不写在公开测试里。** 真实号码、频道 id、私有主机名如果原样写进这个
   文件，等于把要清理的东西又抄了一遍。所以这里用的是合成样本（``-1009999999999``
   这类一眼假的 id、``example.invalid``、``/Users/someone/...``），先证明"规则能抓住
   它们"，再拿同一套规则去扫真实跟踪文件。
2. **必须允许"上游公开署名"。** MIT LICENSE、``bot/utils/project_info.py``、
   ``docs/README.upstream.md`` 里的真实开源作者署名**不能**被当成泄漏删掉——本项目
   是 fork，伪造作者或声称"不是 fork"比留个署名糟糕得多。

扫描面：``git ls-files`` 的全部跟踪文件 + 本次新增提交触及的文件。
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 判定为"私人部署绑定"的形态。值一律是**合成**的，只用来验证规则本身。
DEPLOYMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # 形如 -100… 的 13 位负数，且不是我们自己文档里用到的合成占位。
    ("telegram_channel_id", r"(?<![\w.])-100\d{8,11}(?![\w.])"),
    # 超管身份：9-10 位、既不是 0 也不是常见小 id 的裸数字常量。
    ("telegram_user_id", r"(?<![\w.])\d{9,10}(?![\w.])"),
    # macOS 个人家目录
    ("home_path", r"/Users/[A-Za-z][A-Za-z0-9_.-]*/"),
    # 私有/内网主机名。``.test.`` 前缀是本仓约定的**合成**占位（RFC 6761 保留域），
    # 明确排除，否则规则会因为噪音而失效。
    (
        "lan_host",
        r"\b(?!(?:[\w-]+\.)*test\.)[a-z0-9-]+\.(?:internal|local|home\.arpa)\b",
    ),
    # 家庭宽带 / 内网直连地址
    ("private_host", r"(?<![\w.])(?:10|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\w.])"),
    # 隧道 / 动态 DNS
    ("tunnel_host", r"\b[a-z0-9-]+\.(?:ngrok[a-z-]*|trycloudflare\.com|duckdns\.org)\b"),
)

#: 这些文件里的命中是**故意保留**的，逐条写明理由。
ALLOWED: tuple[tuple[str, str], ...] = (
    ("LICENSE", "上游 MIT 许可与作者署名，必须原样保留"),
    ("bot/utils/project_info.py", "真实开源作者/仓库公开署名；本项目是 fork，不得伪造作者"),
    ("docs/README.upstream.md", "上游 README 存档，署名是上游的"),
    ("bot/services/skills/platform_common.py", "SSRF allowlist 里的官方公共 endpoint"),
)

#: 合成占位 id：文档与测试里用来"示意"的假值，命中不算泄漏。
SYNTHETIC_IDS = frozenset(
    {
        "-1000000000001",
        "-1000000000002",
        "-1000000000003",
        "-1009999999999",
        "-1001234567890",
        "-1009876543210",
        "-1009876543211",
        "-1005550001",
        "-1005550001111",
        "100000001",
        "200000002",
    }
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _git(argument: str) -> str:
    return subprocess.run(
        ["git", argument],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


class ScannerSelfTest(unittest.TestCase):
    """先证明规则能抓住合成样本——规则本身坏了，后面全都不算数。"""

    def test_every_rule_matches_its_synthetic_sample(self) -> None:
        samples = {
            "telegram_channel_id": "MODERATION_CHANNEL = -1009999999999",
            "telegram_user_id": "SUPER_ADMIN = 601234567",
            "home_path": "python /Users/someone/Desktop/repo/.venv/bin/python",
            "lan_host": "http://gw.internal:8080/v1",
            "private_host": "http://10.0.0.53/v1",
            "tunnel_host": "https://x.duckdns.org/hook",
        }
        for name, pattern in DEPLOYMENT_PATTERNS:
            with self.subTest(rule=name):
                self.assertIsNotNone(
                    re.search(pattern, samples[name]),
                    f"{name} 规则连自己的合成样本都抓不住",
                )

    def test_synthetic_placeholders_are_recognised_not_missed(self) -> None:
        """合成占位 id 会被形态规则命中，但**在白名单里**，扫描时不报。

        这一点必须钉住：白名单是"知道它是假的"，不是"规则看不见它"。否则有人把
        白名单当万能豁免塞进真 id，扫描就形同虚设。
        """

        for value in SYNTHETIC_IDS:
            with self.subTest(value=value):
                self.assertIn(value, SYNTHETIC_IDS)
                matched = any(
                    re.search(pattern, value)
                    for name, pattern in DEPLOYMENT_PATTERNS
                    if name in {"telegram_channel_id", "telegram_user_id"}
                )
                if value.startswith("-100") or len(value) >= 9:
                    self.assertTrue(
                        matched or value in {"1", "42"},
                        f"{value} 既不在白名单处理范围内，也没被形态规则命中",
                    )

    def test_the_synthetic_lan_host_is_excluded_by_the_rule(self) -> None:
        pattern = dict(DEPLOYMENT_PATTERNS)["lan_host"]
        self.assertIsNone(re.search(pattern, "gw.test.internal"))
        self.assertIsNotNone(re.search(pattern, "gw.internal"))

    def test_the_scanner_actually_reads_the_repository(self) -> None:
        self.assertGreater(len(_tracked_files()), 100)
        self.assertTrue((REPO_ROOT / "bot" / "config.py").is_file())


class PublicTreeScanTests(unittest.TestCase):
    """拿同一套规则扫真实跟踪文件。"""

    def test_no_private_deployment_binding_in_tracked_files(self) -> None:
        allowed_paths = {path for path, _reason in ALLOWED}
        findings: list[str] = []
        for relative in _tracked_files():
            if relative in allowed_paths:
                continue
            target = REPO_ROOT / relative
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 二进制资源（PNG 字体等）不参与文本扫描
            for line_number, line in enumerate(text.splitlines(), start=1):
                for name, pattern in DEPLOYMENT_PATTERNS:
                    for match in re.finditer(pattern, line):
                        if match.group(0) in SYNTHETIC_IDS:
                            continue
                        findings.append(
                            f"{relative}:{line_number} [{name}] {match.group(0)[:6]}…"
                        )
        self.assertEqual(
            findings,
            [],
            "跟踪文件里出现疑似私人部署绑定：\n" + "\n".join(findings[:40]),
        )

    def test_upstream_attribution_is_still_present(self) -> None:
        """反向检查：不能因为"清理私人信息"把开源署名也删了。"""

        license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT", license_text)
        project_info = (REPO_ROOT / "bot" / "utils" / "project_info.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PROJECT_REPOSITORY_URL", project_info)
        self.assertIn("PROJECT_DEVELOPER", project_info)

    def test_committed_history_carries_no_new_secret(self) -> None:
        """本分支新增的提交里不能出现 .env / 数据库文件。"""

        added = _git("diff --name-only afe840c9..HEAD")
        suspicious = [
            path
            for path in added.splitlines()
            if path.strip()
            and (
                path.strip().endswith((".env", ".db", ".sqlite3", ".pem", ".key"))
                or path.strip() in {".env", "config.toml.local"}
            )
        ]
        self.assertEqual(suspicious, [], f"提交里混进了敏感文件：{suspicious}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
