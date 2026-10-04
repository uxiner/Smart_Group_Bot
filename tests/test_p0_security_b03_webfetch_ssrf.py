"""修复批 P0-1 / B-03：``webfetch`` 的 Firecrawl 分支不得绕过项目自己的 SSRF 防护。

复现的原缺陷（``AUDIT-B`` B-03）：fork 给 ``webfetch`` 加了一条 Firecrawl 抓取路径，
放在原生路径**之前**，只做一次语法级校验（``webfetch.py:_valid_url``）就把用户给的
URL 原样 POST 给 ``api.firecrawl.dev``，**完全不做** ``fetch_text`` → 
``resolve_public_http_url`` 那套 DNS 解析 / 私网 IP 拒绝 / DNS pinning。
于是 ``http://127.0.0.1:8480/healthz``、``http://169.254.169.254/…`` 这些在原生
路径会被 ``non_public_host`` 拒掉的地址，现在会被送到 Firecrawl 侧。

同一批改动里的 ``websearch`` 做对了（``websearch.py:305-321`` 走 ``request_json`` 并
传 ``allowed_hosts``），所以这是疏漏而非设计。

另外「密钥来源与配置系统脱节」：``service.py:310`` 注册的是 ``WebFetchSkill()``
（**不传 settings**），``webfetch.py:93`` 直接读 ``os.environ["FIRECRAWL_API_KEY"]``；
而 ``bot/config.py:545-548`` 的 ``firecrawl_api_key / firecrawl_api_base / 
firecrawl_timeout_sec`` 是 fork 同时加进配置系统的（``WebSearchSkill(settings)``
用的就是这套）。本文件用 ``test_key_comes_from_the_config_system`` 把两条口径钉住。

**不打真实网络**：DNS / HTTP 全部用替身，只验证「有没有走 SSRF 闸」。
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills import webfetch as webfetch_module
from bot.services.skills.webfetch import WebFetchSkill

#: 原生路径已经拒掉、但 Firecrawl 分支会照发不误的内部地址。
SSRF_TARGETS = (
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8480/healthz",
    "http://[::1]:8480/healthz",
    "http://10.0.0.5/internal",
    "http://localhost:8480/healthz",
    "http://redis.internal:6379/",
)

SETTINGS = SimpleNamespace(
    firecrawl_api_key="fc-test-key",
    firecrawl_api_base="https://api.firecrawl.dev",
    firecrawl_timeout_sec=20.0,
)


def make_skill(settings=SETTINGS):
    """构造一个「Firecrawl 已配好」的 ``WebFetchSkill``。

    修前 ``WebFetchSkill.__init__`` 不收参数、只读 ``os.environ``；修后收 settings。
    这里两条路都走一遍，好让 SSRF 用例在**修前也真能跑起来**——否则它会因为
    ``TypeError`` 而红，红的原因就不是 SSRF 闸缺失了。
    """

    try:
        return WebFetchSkill(settings)
    except TypeError:  # pragma: no cover - 仅修前代码路径
        os.environ["FIRECRAWL_API_KEY"] = str(
            getattr(settings, "firecrawl_api_key", "") or ""
        )
        return WebFetchSkill()


class FirecrawlSsrfBypassTests(unittest.IsolatedAsyncioTestCase):
    """Firecrawl 分支必须先过项目自己的 SSRF 闸。"""

    async def asyncSetUp(self) -> None:
        # 记录：有没有真的把 URL 送到第三方。
        self.sent: list[str] = []

        def _fake_firecrawl(*args):
            # `staticmethod` 化的替身可能被当作普通函数调用，也可能仍走绑定方法；
            # 统一按「最后两个位置参数 = (url, api_key)」取。
            url, _api_key = args[-2], args[-1]
            self.sent.append(url)
            return ("内网标题", "内网正文：root:x:0:0::/root:/root:/bin/bash")

        patcher = patch.object(
            WebFetchSkill, "_fetch_via_firecrawl", staticmethod(_fake_firecrawl)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_private_network_targets_never_reach_firecrawl(self) -> None:
        skill = make_skill()
        for url in SSRF_TARGETS:
            with self.subTest(url=url):
                self.sent.clear()
                result = await skill.run({"url": url}, SimpleNamespace())
                self.assertFalse(result.ok, f"{url} 不该被判定为抓取成功")
                self.assertEqual(
                    self.sent, [], f"{url} 仍被送到 Firecrawl——SSRF 闸被绕过"
                )
                self.assertIn("不可安全访问", result.summary)

    async def test_firecrawl_key_alone_does_not_open_the_bypass(self) -> None:
        """只配了 key（没有别的兜底）时，内部地址照样必须被本地闸拦下。"""

        result = await make_skill().run(
            {"url": "http://192.168.1.10/admin"}, SimpleNamespace()
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.sent, [])

    async def test_public_url_still_goes_through_firecrawl(self) -> None:
        """修好之后不能把功能一起修没：公网 URL 仍走 Firecrawl 引擎。"""

        with patch.object(
            webfetch_module,
            "resolve_public_http_url",
            new=AsyncMock(return_value=SimpleNamespace(host="example.com")),
        ):
            result = await make_skill().run(
                {"url": "https://example.com/post"}, SimpleNamespace()
            )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(result.payload["engine"], "firecrawl")
        self.assertEqual(self.sent, ["https://example.com/post"])

    async def test_no_key_means_no_firecrawl_at_all(self) -> None:
        with patch.object(
            webfetch_module,
            "fetch_text",
            new=AsyncMock(
                return_value=(404, b"", "https://example.com/post", "text/html")
            ),
        ):
            result = await make_skill(SimpleNamespace(firecrawl_api_key="")).run(
                {"url": "https://example.com/post"}, SimpleNamespace()
            )
        self.assertEqual(self.sent, [])


class FirecrawlKeySourceTests(unittest.IsolatedAsyncioTestCase):
    """密钥来源必须与配置系统一致（``settings.firecrawl_api_key``）。"""

    async def test_key_comes_from_the_config_system(self) -> None:
        seen: list[str] = []

        def _fake_firecrawl(*args):
            seen.append(args[-1])
            return ("标题", "正文")

        with patch.object(
            WebFetchSkill, "_fetch_via_firecrawl", staticmethod(_fake_firecrawl)
        ), patch.object(
            webfetch_module,
            "resolve_public_http_url",
            new=AsyncMock(return_value=SimpleNamespace(host="example.com")),
            create=True,
        ):
            result = await WebFetchSkill(SETTINGS).run(
                {"url": "https://example.com/post"}, SimpleNamespace()
            )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(seen, ["fc-test-key"])

    async def test_os_environ_alone_does_not_enable_firecrawl(self) -> None:
        """`.env` 里的 FIRECRAWL_API_KEY 不再是唯一开关（配置系统才是）。"""

        seen: list[str] = []

        def _fake_firecrawl(*args):
            seen.append(args[-1])
            return ("标题", "正文")

        skill = WebFetchSkill(SimpleNamespace(firecrawl_api_key=""))
        with patch.object(
            WebFetchSkill, "_fetch_via_firecrawl", staticmethod(_fake_firecrawl)
        ), patch.dict(
            "os.environ", {"FIRECRAWL_API_KEY": "fc-from-env"}, clear=False
        ), patch.object(
            webfetch_module,
            "fetch_text",
            new=AsyncMock(
                return_value=(404, b"", "https://example.com/post", "text/html")
            ),
        ):
            await skill.run({"url": "https://example.com/post"}, SimpleNamespace())
        self.assertEqual(seen, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
