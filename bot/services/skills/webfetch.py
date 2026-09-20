from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.skills.platform_common import (
    ResponseTooLargeError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
    fetch_text,
    parse_html_summary,
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
                "https://api.firecrawl.dev/v1/scrape",
                data=json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "SmartGroupBot/1.0",
                },
            )
            with urlopen(req, timeout=20.0) as resp:
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
        firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        if firecrawl_key:
            import asyncio
            result = await asyncio.to_thread(self._fetch_via_firecrawl, u, firecrawl_key)
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
