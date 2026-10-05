from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    ResponseTooLargeError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
    fetch_text,
    parse_html_summary,
    resolve_public_http_url,
)
from bot.utils.security import clean_multiline_text, clean_text

log = logging.getLogger(__name__)


class WebFetchSkill:
    name = "webfetch"
    description = "抓取并提取指定 URL 的网页正文内容（支持动态网页与 Markdown 深度抓取）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页 URL（http/https）"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, settings: object | None = None) -> None:
        # Firecrawl 的接入参数一律从配置系统读（与 ``WebSearchSkill`` 同口径）。
        # 修前这里是 ``os.environ["FIRECRAWL_API_KEY"]``：``service.py`` 注册的是
        # ``WebFetchSkill()``（不传 settings），所以 ``bot/config.py`` 的
        # ``Settings`` 上那套 ``firecrawl_api_key`` / ``firecrawl_api_base`` /
        # ``firecrawl_timeout_sec`` 对 webfetch 完全不生效——运维只有手写一个文档里
        # 没有的环境变量才能启用它（B-03）。这里按**字段名**指路，不写行号：行号会
        # 随任何一次编辑漂移，指过去就会指错地方（D3-43）。
        # 读侧口径：``SkillService`` 传进来的是**根 Settings**（不是 ``settings.bot``），
        # 所以这几个字段在 config.py 里是顶层字段；检索技能的超时是另一个旋钮
        # ``firecrawl_search_timeout_sec``（18s），别和这里这个 20s 混。
        self._api_key = ""
        self._base = "https://api.firecrawl.dev"
        self._timeout = 20.0
        if settings is not None:
            self._api_key = clean_text(
                str(getattr(settings, "firecrawl_api_key", "") or ""), max_len=512
            )
            base = clean_text(
                str(getattr(settings, "firecrawl_api_base", "") or ""), max_len=2048
            ).rstrip("/")
            if base:
                self._base = base
            try:
                self._timeout = float(
                    getattr(settings, "firecrawl_timeout_sec", 20.0) or 20.0
                )
            except Exception:
                self._timeout = 20.0

    @property
    def firecrawl_available(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def _valid_url(url: str) -> bool:
        try:
            p = urlparse(url)
            _ = p.port
            return (
                p.scheme.lower() in {"http", "https"}
                and bool(p.netloc)
                and bool(p.hostname)
                and p.username is None
                and p.password is None
            )
        except Exception:
            return False

    @staticmethod
    def _html_to_text(raw_html: str) -> tuple[str, str]:
        summary = parse_html_summary(raw_html, max_content_len=6000)
        title = clean_text(summary.get("title") or "", max_len=200)
        content = clean_multiline_text(
            str(summary.get("description") or summary.get("content") or ""),
            max_len=6000,
        )
        return title, content

    def _fetch_via_firecrawl(self, url: str, api_key: str) -> tuple[str, str] | None:
        try:
            req = Request(
                f"{self._base}/v1/scrape",
                data=json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "SmartGroupBot/1.0",
                },
            )
            with urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("success", False):
                    return None
                doc = data.get("data", {})
                meta = doc.get("metadata", {})
                title = clean_text(meta.get("title") or meta.get("og:title") or "", max_len=200)
                md = doc.get("markdown", "")
                content = clean_multiline_text(md, max_len=8000)
                return title, content
        except Exception as e:
            log.warning("Firecrawl scrape failed, falling back to basic fetch: %s", e)
            return None

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        _ = context  # Unused for this skill.
        u = str(arguments.get("url", "")).strip()
        if not self._valid_url(u):
            return SkillRunResult(ok=False, skill=self.name, summary="URL 非法", error="invalid_url")

        # 1. 优先尝试使用 Firecrawl 引擎提取网页（过反爬、JS渲染、输出清晰 Markdown）
        #
        #    但 Firecrawl 是**第三方代抓**：它会去访问我们给的这个 URL，所以项目
        #    自己的 SSRF 策略对它同样成立。修前这里只过了 `_valid_url` 的语法校验就
        #    POST 出去，于是 `http://169.254.169.254/…`、`http://127.0.0.1:8480/…`
        #    这些原生路径会用 `non_public_host` / `non_public_address` 拒掉的地址
        #    被照发不误——同一站点两条路径两套口径，内部主机名/端口还会泄漏给第三方
        #    （B-03）。所以先过一遍 `resolve_public_http_url`：语法 + DNS 解析 +
        #    私网/回环/链路本地 IP 拒绝，与原生路径完全一致。
        if self.firecrawl_available:
            try:
                await resolve_public_http_url(u)
            except UnsafeUrlError as exc:
                log.warning("webfetch rejected unsafe URL before firecrawl: %s", exc)
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="该地址不可安全访问",
                    error=str(exc) or "unsafe_url",
                )
            result = await asyncio.to_thread(self._fetch_via_firecrawl, u, self._api_key)
            if result and result[1]:
                title, content = result
                log.info("Successfully fetched %s via Firecrawl", u)
                return SkillRunResult(
                    ok=True,
                    skill=self.name,
                    summary="网页抓取成功 (Firecrawl)",
                    payload={
                        "url": u,
                        "final_url": u,
                        "status": 200,
                        "content_type": "text/markdown",
                        "title": title or u,
                        "content": content,
                        "engine": "firecrawl",
                    },
                )

        # 2. 原生 fetch 回退机制
        headers = {
            "User-Agent": "SmartGroupBot/1.0 (+https://example.local)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }

        try:
            status, raw, final_url, ctype = await fetch_text(
                u,
                headers=headers,
                timeout_sec=15.0,
                allow_redirects=True,
                allowed_content_types=(
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                    "application/json",
                    "application/xml",
                    "text/xml",
                ),
                max_response_bytes=1024 * 1024,
                max_decoded_bytes=2 * 1024 * 1024,
                max_redirects=4,
            )
            if status >= 400:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary=f"网页请求失败: HTTP {status}",
                    error=f"http_{status}",
                )

            title, content = self._html_to_text(raw)
            if not content:
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="网页内容为空或不可解析",
                    error="empty_content",
                )

            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary="网页抓取成功",
                payload={
                    "url": u,
                    "final_url": final_url,
                    "status": status,
                    "content_type": ctype,
                    "title": title,
                    "content": content,
                    "engine": "native",
                },
            )
        except UnsafeUrlError as exc:
            log.warning("webfetch rejected unsafe URL: %s", exc)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="该地址不可安全访问",
                error=str(exc) or "unsafe_url",
            )
        except ResponseTooLargeError as exc:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="网页响应体过大，已停止抓取",
                error=str(exc) or "response_too_large",
            )
        except UnsupportedContentTypeError as exc:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="该链接不是可读取的文本网页",
                error=str(exc) or "unsupported_content_type",
            )
        except TimeoutError:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="网页抓取超时",
                error="timeout",
            )
        except Exception as e:
            log.exception("webfetch failed")
            return SkillRunResult(ok=False, skill=self.name, summary="网页抓取失败", error=str(e))
