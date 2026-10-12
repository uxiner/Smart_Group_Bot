"""B3：在**真实 Chromium DOM** 里验证 Mini App 的总开关说明与页面级权限。

静态字符串测试只能证明源码里有某句话；它证明不了这句话在真实渲染后**可见**、在 320px
手机上**不横向溢出**、并且**没有**在渲染时偷偷发请求。本模块因此把 ``app.js`` 里的真实
实现逐字切片（``ALL_NAV_ITEMS`` / ``navItems`` / ``escapeHtml`` / ``attr`` / ``getPath`` /
``icon`` / ``helpDisclosure`` / ``toggle``），套上**真实的** ``styles.css``，用真实 Blink
渲染后断言 DOM 事实。

切片锚点缺失即失败（不做静默降级），找不到浏览器时 ``self.fail`` 而不是 ``skip``：本用例
的价值就在于真的跑过一次 DOM。
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_APP_JS = _REPO_ROOT / "bot" / "web" / "static" / "app.js"
_STYLES = _REPO_ROOT / "bot" / "web" / "static" / "styles.css"

# 审定文案（父代理 m02312 逐字给定）——必须真的出现在渲染后的 DOM 文本里。
DOM_SENTENCES = (
    "关闭只停用长期事实，不清空记录。",
    "群组页的「永久记忆」是另一套群级背景信息",
    "长期事实用群内 /memory forget <编号> 删单条",
    "或 /memory off 关闭本人记忆并标记删除本人事实。",
    "总开关关闭时，这两条命令也不可用。",
)
WRONG_DOM_HINT = "需要在群组页的「永久记忆」中手工清理"
WIDTHS = (320, 390, 1280)


def _slice(source: str, start_marker: str, end_marker: str, label: str) -> str:
    start = source.find(start_marker)
    if start < 0:
        raise AssertionError(f"app.js 缺少 {label} 的起始锚点：{start_marker!r}")
    end = source.find(end_marker, start)
    if end < 0:
        raise AssertionError(f"app.js 缺少 {label} 的结束锚点：{end_marker!r}")
    return source[start : end + len(end_marker)]


def _slice_function(source: str, signature: str) -> str:
    return _slice(source, "  " + signature, "\n  }\n", signature)


def _find_browser() -> str | None:
    override = os.environ.get("OPERATIONS_BROWSER")
    if override and os.path.exists(override):
        return override
    for pattern in (
        "~/Library/Caches/ms-playwright/*/chrome-headless-shell-*/chrome-headless-shell",
        "~/Library/Caches/ms-playwright/*/chrome-mac*/*.app/Contents/MacOS/*",
    ):
        for hit in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.access(hit, os.X_OK):
                return hit
    for candidate in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("chromium") or shutil.which("google-chrome")


class MemorySemanticsDomTests(unittest.TestCase):
    def test_total_switch_help_renders_and_wraps_on_phone_widths(self) -> None:
        browser = _find_browser()
        if not browser:
            self.fail(
                "找不到 Chromium 可执行文件；本用例刻意不 skip。"
                "请用 OPERATIONS_BROWSER=<path> 指定 chrome-headless-shell。"
            )

        page = self._build_page()
        results = [self._render(browser, page, width) for width in WIDTHS]

        for width, data in zip(WIDTHS, results):
            self.assertEqual(data["errors"], [], f"{width}px 渲染报错：{data['errors']}")
            checks = {item["name"]: item for item in data["checks"]}

            required = (
                "help disclosure rendered",
                "help body rendered",
                "help collapsed by default",
                "no document horizontal overflow",
                "help body has no horizontal overflow",
                "toggle copy has no horizontal overflow",
                "help text visible after opening",
                "help text carries the audited copy",
                "wrong cleanup pointer is gone",
                "super admin sees Bot tab",
                "group admin cannot see Bot tab",
                "group admin still sees groups tab",
                "no network call during render",
            )
            for name in required:
                self.assertIn(name, checks, f"{width}px 缺少检查项 {name}")
                self.assertTrue(checks[name]["ok"], f"{width}px 失败：{name}")

            # 真实渲染宽度确实是目标视口，避免「测了个假宽度」。
            self.assertEqual(data["info"]["viewport"], width)
            # 手机上必须是折行而不是溢出容器。
            self.assertLessEqual(
                data["info"]["helpBodyWidth"], float(width), f"{width}px 帮助块超宽"
            )

    # ---- harness ----------------------------------------------------------

    def _build_page(self) -> str:
        source = _APP_JS.read_text(encoding="utf-8")
        styles = _STYLES.read_text(encoding="utf-8")

        call = None
        for line in source.splitlines():
            if 'toggle("bot.memory_facts_enabled"' in line:
                stripped = line.strip()
                if not (stripped.startswith("${") and stripped.endswith("}")):
                    raise AssertionError("总开关调用不再是单行模板插值")
                call = stripped[2:-1]
                break
        if call is None:
            self.fail("app.js 里找不到 bot.memory_facts_enabled 的总开关调用")

        slices = "\n".join(
            (
                _slice(source, "  const ALL_NAV_ITEMS = [", "\n  ];\n", "ALL_NAV_ITEMS"),
                _slice_function(source, "function navItems()"),
                _slice_function(source, "function escapeHtml(value)"),
                _slice_function(source, "function attr(value)"),
                _slice_function(source, "function getPath(object, path)"),
                _slice_function(source, 'function icon(name, className = "")'),
                _slice_function(
                    source, 'function helpDisclosure(help, label = "展开说明")'
                ),
                _slice_function(
                    source,
                    'function toggle(path, label, hint = "", full = false, help = "")',
                ),
            )
        )

        return _PAGE_TEMPLATE.format(
            styles=styles,
            slices=slices,
            call=call,
            audited=json.dumps(list(DOM_SENTENCES), ensure_ascii=False),
            wrong=json.dumps(WRONG_DOM_HINT, ensure_ascii=False),
        )

    def _render(self, browser: str, page: str, width: int) -> dict:
        workdir = tempfile.mkdtemp(prefix="b3-memory-dom-")
        try:
            path = pathlib.Path(workdir) / "page.html"
            path.write_text(page, encoding="utf-8")
            proc = subprocess.run(
                [
                    browser,
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-crash-reporter",
                    f"--user-data-dir={workdir}/profile",
                    f"--window-size={width},900",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    f"file://{path}",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "HOME": workdir},
            )
            match = re.search(r'<pre id="results">(.*?)</pre>', proc.stdout, re.S)
            if not match:
                self.fail(
                    f"{width}px 未拿到 harness 结果（exit={proc.returncode}）："
                    f"{proc.stdout[-800:]} {proc.stderr[-400:]}"
                )
            return json.loads(match.group(1))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B3 total-switch harness</title>
<style>
{styles}
</style>
</head><body>
<div id="content"></div>
<pre id="results">pending</pre>
<script>
const results = {{ checks: [], info: {{}}, errors: [] }};
function check(name, ok, actual) {{
  results.checks.push({{ name: name, ok: Boolean(ok), actual: String(actual) }});
}}
try {{
  const state = {{ config: {{}}, session: null, document: null, advancedOpen: new Map() }};
  let fetchCount = 0;
  window.fetch = function () {{ fetchCount += 1; return Promise.reject(new Error("no network")); }};

{slices}

  const toggleHtml = {call};
  document.getElementById("content").innerHTML =
    '<div class="field-grid three">' + toggleHtml + "</div>";

  const details = document.querySelector("#content details.field-help");
  check("help disclosure rendered", Boolean(details), details ? "yes" : "no");
  check("help collapsed by default", details ? !details.open : false, details ? details.open : "n/a");

  const body = document.querySelector("#content .field-help-body");
  check("help body rendered", Boolean(body), body ? "yes" : "no");
  if (details) details.open = true;

  const viewport = document.documentElement.clientWidth;
  const copy = document.querySelector("#content .toggle-copy");
  const helpText = body ? body.textContent : "";
  results.info.viewport = viewport;
  results.info.helpBodyWidth = body ? body.getBoundingClientRect().width : null;
  results.info.helpText = helpText;
  results.info.hintText = (document.querySelector("#content .toggle-copy small") || {{}}).textContent || "";

  check("no document horizontal overflow",
        document.documentElement.scrollWidth <= viewport + 1,
        document.documentElement.scrollWidth + " vs " + viewport);
  check("help body has no horizontal overflow",
        body ? body.scrollWidth <= body.clientWidth + 1 : false,
        body ? body.scrollWidth + " vs " + body.clientWidth : "n/a");
  check("toggle copy has no horizontal overflow",
        copy ? copy.scrollWidth <= copy.clientWidth + 1 : false,
        copy ? copy.scrollWidth + " vs " + copy.clientWidth : "n/a");
  check("help text visible after opening",
        Boolean(body && body.offsetHeight > 0 && helpText.indexOf("关闭只停用长期事实") >= 0),
        body ? body.offsetHeight : "n/a");

  const audited = {audited};
  const missing = audited.filter(function (sentence) {{ return helpText.indexOf(sentence) < 0; }});
  check("help text carries the audited copy", missing.length === 0, missing.join(" | "));
  check("wrong cleanup pointer is gone", helpText.indexOf({wrong}) < 0, "old pointer");

  state.session = {{ can_manage_global: true }};
  check("super admin sees Bot tab",
        navItems().some(function (item) {{ return item.id === "bot"; }}), "navItems");
  state.session = {{ can_manage_global: false }};
  const groupNav = navItems();
  check("group admin cannot see Bot tab",
        !groupNav.some(function (item) {{ return item.id === "bot"; }}), "navItems");
  check("group admin still sees groups tab",
        groupNav.some(function (item) {{ return item.id === "groups"; }}), "navItems");
  check("no network call during render", fetchCount === 0, fetchCount);
}} catch (error) {{
  results.errors.push(String(error && error.stack || error));
}}
document.getElementById("results").textContent = JSON.stringify(results, null, 2);
</script>
</body></html>
"""


if __name__ == "__main__":
    unittest.main()
