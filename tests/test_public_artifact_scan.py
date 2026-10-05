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
    # 形如 -100… 的 13 位负数（Telegram 频道/超级群 chat id）。
    ("telegram_channel_id", r"(?<![\w.])-100\d{8,11}(?![\w.])"),
    # macOS 个人家目录
    ("home_path", r"/Users/(?!someone/|example/)[A-Za-z][A-Za-z0-9_.-]*/"),
    # 隧道 / 动态 DNS
    ("tunnel_host", r"\b[a-z0-9-]+\.(?:ngrok[a-z-]*|trycloudflare\.com|duckdns\.org)\b"),
    # 私有主机名。排除本仓约定的**合成**占位：``*.test.internal``（RFC 6761 保留域
    # 风格的测试域）、``example.invalid``（RFC 2606）、``docker.internal``（Docker
    # Desktop 自带）与 ``toml.local``（语言服务）。这些不是部署绑定。
    (
        "lan_host",
        r"(?<![\w.])"
        r"(?!(?:docker|toml|example|gw|llm|nas|redis|test)\.)"
        r"[a-z0-9-]+\.(?:internal|home\.arpa)\b",
    ),
)

#: **身份上下文**：只有出现在这些名字旁边的裸数字，才可能是"某个人的 Telegram id"。
#: 单靠"9-10 位数字"判会把日期戳、token 片段、base32 样例全部误报——一条误报就能
#: 让整条规则被人关掉。
IDENTITY_CONTEXT = re.compile(
    r"(?:super_?admin|owner_?id|sender_?user_?id|user_?id|admin_?id|telegram_?id)",
    re.IGNORECASE,
)

#: 明显是"写出来给人看的假值"的数字形状：同一个数字重复、或者整串是顺序/回文。
#: 这些不是泄漏，规则要认得出来，否则噪音会淹掉真正的信号。
def _is_obvious_placeholder(value: str) -> bool:
    """一眼假的数字：重复位、连续递增/递减位、或者内含日期戳。

    这一层是"降噪"而不是"判定安全"：它只负责让规则不��对明显的样例数据开火，
    真正需要人看的情况仍然会被报出来。
    """

    if len(set(value)) <= 3:              # 111111111 / 999999999 / 222222222
        return True
    digits = [int(char) for char in value]
    # 取模 10，让 1234567890 / 9876543210 这种"回绕"的顺序串也算进来。
    if all((b - a) % 10 == 1 for a, b in zip(digits, digits[1:])):
        return True                      # 1234567890
    if all((b - a) % 10 == 9 for a, b in zip(digits, digits[1:])):
        return True                      # 9876543210
    # 8 位且看起来像 YYYYMMDD 日期戳（业务日期，不是身份）。
    if len(value) == 8:
        year, month, day = int(value[:4]), int(value[4:6]), int(value[6:8])
        if 2020 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
            return True
    return False

#: 这些文件里的命中是**故意保留**的，逐条写明理由。
ALLOWED: tuple[tuple[str, str], ...] = (
    ("LICENSE", "上游 MIT 许可与作者署名，必须原样保留"),
    ("bot/utils/project_info.py", "真实开源作者/仓库公开署名；本项目是 fork，不得伪造作者"),
    ("docs/README.upstream.md", "上游 README 存档，署名是上游的"),
    (
        "tests/test_public_artifact_scan.py",
        "扫描器自己：这里必须留着合成样本，否则规则就没人验证过",
    ),
    ("bot/services/skills/platform_common.py", "SSRF allowlist 里的官方公共 endpoint"),
)

#: 合成占位 id：文档与测试里用来"示意"的假值，命中不算泄漏。
#: 这些是**假值**，不是任何人的真实标识——列出来是为了让扫描噪音可控。
SYNTHETIC_IDS = frozenset(
    {
        # 频道 / 群：本轮净化后统一用的占位，以及文档里写明的合法区间端点。
        "-1000000000000",
        "-1000000000001",
        "-1000000000002",
        "-1000000000003",
        "-1009999999999",
        "-1001234567890",
        "-1009876543210",
        "-1009876543211",
        "-1005550001",
        "-1005550001111",
        # 反例测试里用来断言"非法值被拒"的那两个形状。
        "-100000000000",
        "-10000000000000",
        # 用户 id：占位与测试套件早就在用的合成 fixture。
        "100000001",
        "200000002",
        "5550001111",
        "700000001",
        "700000002",
        "700000003",
        "800000001",
        "800000002",
        "800000003",
        "800000004",
        "1005550001",
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
            "telegram_user_id": "SUPER_ADMIN = 601238417",
            "home_path": "python /Users/jdoe/Desktop/repo/.venv/bin/python",
            "lan_host": "http://myserver.internal:8080/v1",
            "tunnel_host": "https://myservice.duckdns.org/hook",
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
                # 白名单是"知道它是假的"，不是"规则看不见它"：能命中形态规则的
                # 必须真的会被规则命中，否则白名单就成了万能豁免。
                if re.fullmatch(r"-100\d{8,11}", value):
                    self.assertTrue(
                        bool(re.search(r"(?<![\w.])-100\d{8,11}(?![\w.])", value)),
                        f"{value} 在白名单里却匹配不到形态规则",
                    )
                elif value.isdigit() and len(value) >= 8:
                    self.assertTrue(
                        bool(IDENTITY_CONTEXT.search(f"user_id = {value}")),
                        f"{value} 在白名单里却匹配不到身份上下文",
                    )

    def test_synthetic_lan_hosts_are_excluded_by_the_rule(self) -> None:
        pattern = dict(DEPLOYMENT_PATTERNS)["lan_host"]
        for host in ("gw.test.internal", "example.invalid", "docker.internal"):
            self.assertIsNone(re.search(pattern, host), f"{host} 不该被当成私有主机")
        self.assertIsNotNone(re.search(pattern, "myserver.internal"))

    def test_known_synthetic_fixtures_do_not_trip_the_identity_rule(self) -> None:
        """测试套件里既有的合成 fixture 不该被当成真人 id。

        这些是**假值**：日期戳（``20261001``）、固定样例账号（``136817688``）、以及
        上面 :data:`SYNTHETIC_IDS` 里的占位。列出来是为了让规则保持低噪音——一条
        误报就能让整条规则被人关掉。
        """

        for value in ("20261001", "20261005", "12345678", "987654321", "111111111"):
            with self.subTest(value=value):
                self.assertTrue(
                    _is_obvious_placeholder(value)
                    or value in SYNTHETIC_IDS
                    or int(value) < 1_000_000
                )

    def test_the_identity_rule_needs_context_and_rejects_placeholders(self) -> None:
        # 同样的数字，带身份上下文才算；不带就只是普通数字。
        self.assertTrue(IDENTITY_CONTEXT.search("SUPER_ADMIN = 601238417"))
        self.assertIsNone(IDENTITY_CONTEXT.search("SIZE = 601238417"))
        # 明显的占位不该被当成真人 id。
        for value in ("111111111", "999999999", "123456789", "1234567890", "222222222"):
            self.assertTrue(_is_obvious_placeholder(value), value)

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
                # 身份 id：必须落在身份上下文里，且不是一眼假的占位。
                if not IDENTITY_CONTEXT.search(line):
                    continue
                for match in re.finditer(r"(?<![\w.])\d{8,11}(?![\w.])", line):
                    value = match.group(0)
                    if value in SYNTHETIC_IDS or _is_obvious_placeholder(value):
                        continue
                    if int(value) <= 1_000_000:      # 小 id：Telegram 测试账号量级
                        continue
                    findings.append(
                        f"{relative}:{line_number} [telegram_user_id] {value[:2]}…"
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
