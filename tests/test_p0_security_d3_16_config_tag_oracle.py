"""修复批 P0-1 / D3-16：web 层 ``/verify`` 的 config tag 不得成为 secret 校验预言机。

复现的原缺陷（``GAP-D3`` D3-16）：``bot/services/verify_web.py`` 的
``_verification_config_tag`` 把 ``verification_keys_for_provider()`` 返回的
``(site_key, **secret_key**)`` 与 public base URL 一起做 SHA-256，hex 摘要直接写进
**任何人可 GET** 的 ``/verify`` HTML（``handle_challenge_page``，路由无鉴权）。

site key 本来就是公开且已知的（同一页面上就明文放着 ``data-sitekey``），所以摘要
因此成为 secret key 的**确定性校验预言机**：从任何其它渠道拿到候选 secret 的人可以
本地比对摘要确认，同时把「secret 未轮换」这一状态对外变成可验证的。

**核心判据是「不依赖密钥值」**：修前摘要只由「公开量 + 密钥」决定 → 修后摘要只由
「公开量」决定。因此「只改 secret、不改 site key」时摘要不变（本文件用
``test_tag_ignores_the_secret_key`` 明确记录这一条，因为它是刻意接受的语义损失），
而攻击者拿候选 secret 去比对**得不到任何信息**。
"""

from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from bot.services.verify_web import _verification_config_tag

SITE_KEY = "0x4AAAAAAA-sitekey-public"
SECRET_A = "0xBBBBBBBB-secret-a"
SECRET_B = "0xCCCCCCCC-secret-b"
BASE_URL = "https://verify.example.com/"


def _settings(
    *,
    turnstile_site_key: str = SITE_KEY,
    turnstile_secret: str = SECRET_A,
    hcaptcha_site_key: str = "",
    hcaptcha_secret: str = "",
    base_url: str = BASE_URL,
) -> SimpleNamespace:
    return SimpleNamespace(
        join_verification_turnstile_site_key=turnstile_site_key,
        join_verification_turnstile_secret_key=turnstile_secret,
        join_verification_hcaptcha_site_key=hcaptcha_site_key,
        join_verification_hcaptcha_secret_key=hcaptcha_secret,
        join_verification_public_base_url=base_url,
    )


class VerificationConfigTagOracleTests(unittest.TestCase):
    def test_tag_ignores_the_secret_key(self) -> None:
        """核心回归：只改 secret key 时，公开 tag 必须**不变**。

        修前这里会变——而 tag 就在任何人都能 GET 的页面上，于是它就是一条可离线
        比对的确定性校验通道。
        """

        before = _verification_config_tag(_settings(turnstile_secret=SECRET_A), "turnstile")
        after = _verification_config_tag(_settings(turnstile_secret=SECRET_B), "turnstile")
        self.assertEqual(before, after)

    def test_tag_cannot_be_recomputed_from_a_candidate_secret(self) -> None:
        """攻击者视角：手里有一个候选 secret，能不能拿它去撞公开 tag？

        不能——tag 完全由公开量决定，换成什么 secret 都算出同一个值，信息量为 0。
        反过来说，tag 既然只由公开量决定，它就必须能被公开量**原样重建**（否则就是
        混进了密钥的比特）。
        """

        published = _verification_config_tag(_settings(turnstile_secret=SECRET_A), "turnstile")

        def local_tag(secret: str) -> str:
            return _verification_config_tag(_settings(turnstile_secret=secret), "turnstile")

        # 换任何 secret（包括空串）都撞不出差别。
        self.assertEqual(local_tag(SECRET_A), published)
        self.assertEqual(local_tag(SECRET_B), published)
        self.assertEqual(local_tag(""), published)
        # tag 只由 provider + site key + base url 这三个公开量决定，可原样重建。
        public_parts = "\x00".join(["turnstile", SITE_KEY, BASE_URL.strip().rstrip("/")])
        self.assertEqual(
            hashlib.sha256(public_parts.encode("utf-8")).hexdigest(), published
        )

    def test_tag_ignores_the_hcaptcha_secret_too(self) -> None:
        combined_before = _verification_config_tag(
            _settings(hcaptcha_site_key="hc-site", hcaptcha_secret="hc-secret-1"),
            "combined",
        )
        combined_after = _verification_config_tag(
            _settings(hcaptcha_site_key="hc-site", hcaptcha_secret="hc-secret-2"),
            "combined",
        )
        self.assertEqual(combined_before, combined_after)

    def test_tag_ignores_hcaptcha_secret_in_the_hcaptcha_provider(self) -> None:
        before = _verification_config_tag(
            _settings(hcaptcha_site_key="hc-site", hcaptcha_secret="hc-secret-1"), "hcaptcha"
        )
        after = _verification_config_tag(
            _settings(hcaptcha_site_key="hc-site", hcaptcha_secret="hc-secret-2"), "hcaptcha"
        )
        self.assertEqual(before, after)

    # ---- 轮换检测语义（不能把功能一起修没）--------------------------------
    def test_tag_still_detects_a_site_key_rotation(self) -> None:
        before = _verification_config_tag(_settings(turnstile_site_key=SITE_KEY), "turnstile")
        after = _verification_config_tag(_settings(turnstile_site_key="rotated-site"), "turnstile")
        self.assertNotEqual(before, after)

    def test_tag_still_detects_a_provider_switch(self) -> None:
        turnstile_tag = _verification_config_tag(_settings(), "turnstile")
        hcaptcha_tag = _verification_config_tag(
            _settings(hcaptcha_site_key="hc-site", hcaptcha_secret="hc-secret"),
            "hcaptcha",
        )
        self.assertNotEqual(turnstile_tag, hcaptcha_tag)

    def test_tag_still_detects_a_base_url_change(self) -> None:
        before = _verification_config_tag(_settings(base_url=BASE_URL), "turnstile")
        after = _verification_config_tag(
            _settings(base_url="https://other.example.com/"), "turnstile"
        )
        self.assertNotEqual(before, after)

    def test_tag_is_stable_across_calls(self) -> None:
        settings = _settings()
        self.assertEqual(
            _verification_config_tag(settings, "turnstile"),
            _verification_config_tag(settings, "turnstile"),
        )

    def test_tag_is_a_hex_sha256(self) -> None:
        tag = _verification_config_tag(_settings(), "turnstile")
        self.assertEqual(len(tag), 64)
        int(tag, 16)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
