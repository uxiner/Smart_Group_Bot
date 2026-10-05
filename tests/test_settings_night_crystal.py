"""Visual contract + layout + interaction regressions for the settings Mini App.

These tests guard the night-blue / ice-cyan / lavender redesign:

* the dark theme is the only theme, so the page can never render half light,
* the palette, spacing scale and touch targets stay inside the agreed tokens,
* progressive disclosure keeps drafts and reveals itself when validation fails,
* the save button survives a real click, and
* the shipped page never grows a demo/fixture/auth bypass.

They are source-level contracts on purpose: the real browser pass lives in the
loopback harness under ``tools/settings-ui-harness`` and is reported in
UI-DELIVERY.md.
"""
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "bot" / "web" / "static" / "app.js"
_INDEX_HTML = _ROOT / "bot" / "web" / "static" / "index.html"
_STYLES_CSS = _ROOT / "bot" / "web" / "static" / "styles.css"
_DOCKERFILE = _ROOT / "Dockerfile"
_HARNESS = _ROOT / "tools" / "settings-ui-harness" / "harness.py"


def strip_js_comments(source: str) -> str:
    """Blank out // and /* */ comments while keeping string contents intact."""
    out = []
    index = 0
    length = len(source)
    quote = ""
    while index < length:
        char = source[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and not (source[index] == "*" and source[index + 1] == "/"):
                if source[index] == "\n":
                    out.append("\n")
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def top_level_rules(css: str) -> list[tuple[str, str]]:
    """Return (selector list, declarations) for every rule outside an @media.

    Indentation is the only structure the stylesheet relies on, so a rule is
    top level when its opening brace sits at column 0 and it is not an at-rule.
    Media-query overrides are intentionally excluded: they only ever shrink
    padding, never the 44px target itself.
    """
    rules: list[tuple[str, str]] = []
    pending: list[str] = []
    selector: str | None = None
    body: list[str] = []
    for line in css.splitlines():
        if selector is None:
            stripped = line.strip()
            if not stripped or stripped.startswith(("/*", "*", "//")) or stripped.startswith("@"):
                continue
            if line.startswith(" "):
                # Indented line inside an @media block: not a top-level rule.
                continue
            pending.append(stripped)
            if stripped.endswith("{"):
                selector = ",".join(pending)[:-1]
                pending = []
                body = []
            continue
        if line.startswith("}"):
            rules.append((selector, "\n".join(body)))
            selector = None
            continue
        body.append(line.strip())
    return rules


def sizing_declarations(rules: list[tuple[str, str]], selector: str) -> list[str]:
    """min-height/height values declared for ``selector`` in any top-level rule."""
    found: list[str] = []
    for selectors, body in rules:
        parts = [part.strip() for part in selectors.split(",")]
        if selector not in parts:
            continue
        for match in re.finditer(r"(?:min-height|height):\s*([^;]+);", body):
            found.append(match.group(1).strip())
    return found


class NightCrystalPaletteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = _STYLES_CSS.read_text(encoding="utf-8")
        self.root = self.css.split(":root {", 1)[1].split("\n}", 1)[0]

    def test_dark_is_the_only_theme(self) -> None:
        # A light :root plus a dark media query produced the "half light, half
        # dark" bug; the redesign makes the dark tokens the base and drops the
        # prefers-color-scheme override entirely.
        self.assertIn("color-scheme: dark;", self.root)
        self.assertNotIn("prefers-color-scheme", self.css)
        self.assertNotIn("light dark", _INDEX_HTML.read_text(encoding="utf-8"))
        self.assertIn('<meta name="color-scheme" content="dark">', _INDEX_HTML.read_text(encoding="utf-8"))

    def test_theme_colour_metadata_matches_the_background(self) -> None:
        html = _INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('<meta name="theme-color" content="#0C1220">', html)
        self.assertIn("background: var(--bg);", self.root)

    def test_plane_tokens_match_the_agreed_ladder(self) -> None:
        for token, value in (
            ("--bg", "#0C1220"),
            ("--surface-sidebar", "#0F1726"),
            ("--surface", "#151F31"),
            ("--surface-raised", "#1B2940"),
            ("--text", "#EEF3FC"),
            ("--text-muted", "#A8B5CC"),
            ("--border", "#2A3A52"),
            ("--primary", "#71D9EF"),
            ("--accent-violet", "#B8A4F4"),
        ):
            self.assertRegex(self.root, rf"{re.escape(token)}:\s*{re.escape(value)};", f"{token} must be {value}")

    def test_primary_button_pairs_cyan_with_dark_ink(self) -> None:
        self.assertRegex(self.root, r"--primary-ink:\s*#062430;")
        self.assertRegex(self.css, r"\.primary-button \{[^}]*background: var\(--primary\);[^}]*color: var\(--primary-ink\);")

    def test_danger_is_never_masquerading_as_the_primary_action(self) -> None:
        danger_rules = re.findall(r"^\.danger-button \{([^}]*)\}", self.css, re.M)
        self.assertTrue(danger_rules)
        filled = [rule for rule in danger_rules if "background: var(--danger);" in rule]
        self.assertEqual(len(filled), 1, "exactly one .danger-button rule paints the danger surface")
        self.assertIn("color: var(--danger-ink);", filled[0])
        for rule in danger_rules:
            self.assertNotIn("var(--primary)", rule)
        self.assertNotEqual(
            self.root.split("--danger:")[1].split(";")[0].strip(),
            self.root.split("--primary:")[1].split(";")[0].strip(),
        )
        self.assertIn("background: var(--primary);", re.search(r"^\.primary-button \{([^}]*)\}", self.css, re.M).group(1))

    def test_every_foreground_token_clears_4_5_to_1_on_the_raised_surface(self) -> None:
        def luminance(hex_color: str) -> float:
            channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def ratio(foreground: str, background: str) -> float:
            first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
            return (first + 0.05) / (second + 0.05)

        surfaces = ["#0C1220", "#0F1726", "#151F31", "#1B2940"]
        for token in ("--text", "--text-muted", "--text-faint", "--primary", "--accent-violet", "--warning", "--danger", "--success", "--info"):
            color = re.search(rf"{re.escape(token)}:\s*(#[0-9A-Fa-f]{{6}});", self.root).group(1)
            for surface in surfaces:
                self.assertGreaterEqual(
                    ratio(color, surface), 4.5, f"{token} {color} on {surface} is below 4.5:1"
                )

    def test_no_webfont_or_external_asset_is_introduced(self) -> None:
        self.assertNotIn("@import", self.css)
        self.assertNotIn("@font-face", self.css)
        self.assertNotIn("fonts.googleapis", self.css)
        self.assertNotIn("fonts.gstatic", self.css)
        self.assertIn("-apple-system, BlinkMacSystemFont", self.css)
        html = _INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("style-src 'self';", html)
        self.assertIn("font-src 'self';", html)
        self.assertIn("connect-src 'self';", html)


class NightCrystalLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = _STYLES_CSS.read_text(encoding="utf-8")

    def test_touch_targets_stay_at_44px(self) -> None:
        self.assertIn("--control-height: 44px;", self.css)
        rules = top_level_rules(self.css)

        for selector in (
            ".icon-button",
            ".mini-icon-button",
            ".text-button",
            ".nav-button",
            ".prompt-button",
            ".group-quick-nav-button",
            ".advanced-panel > summary",
            ".group-settings-section > summary",
            ".group-card-toggle",
            ".field-help > summary",
            ".compact-check",
            ".toggle",
        ):
            sizes = sizing_declarations(rules, selector)
            self.assertTrue(sizes, f"{selector} must declare a min-height/height")
            for value in sizes:
                if value == "var(--control-height)":
                    continue
                pixels = float(value.removesuffix("px"))
                self.assertGreaterEqual(pixels, 44, f"{selector} declares {value}")

    def test_mobile_viewports_keep_the_chrome_in_bounds(self) -> None:
        for breakpoint in ("820px", "620px", "520px", "420px", "360px"):
            self.assertIn(f"@media (max-width: {breakpoint})", self.css)
        narrow = self.css.split("@media (max-width: 360px) {", 1)[1]
        self.assertIn("#save-button {", narrow)
        self.assertIn("min-width: 88px;", narrow)
        self.assertIn(".topbar-title h1 {\n    max-width: 68px;", narrow)

    def test_desktop_width_is_capped(self) -> None:
        self.assertIn("--content-max: 1180px;", self.css)
        content = self.css.split(".content {", 1)[1].split("}", 1)[0]
        self.assertIn("width: min(var(--content-max), 100%);", content)
        self.assertIn("margin: 0 auto;", content)

    def test_horizontal_scroll_stays_available_only_inside_intentional_rails(self) -> None:
        # Only the chip rail and the prompt strip may scroll sideways; every
        # other surface clips instead of pushing the page wide.
        for rail in (".mobile-nav", ".prompt-list"):
            rules = re.findall(rf"^\s*{re.escape(rail)} \{{([^}}]*)\}}", self.css, re.M)
            self.assertTrue(rules, f"{rail} must have a rule")
            self.assertTrue(
                any("overflow-x: auto;" in rule for rule in rules),
                f"{rail} needs its own horizontal rail",
            )
        self.assertIn("overflow-x: clip;", self.css)
        self.assertNotRegex(self.css, r"^body \{[^}]*overflow-x: (auto|scroll)", re.M)

    def test_reduced_motion_and_visible_focus_are_still_declared(self) -> None:
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        reduced = self.css.split("@media (prefers-reduced-motion: reduce) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("animation-duration: 0.01ms !important;", reduced)
        self.assertIn(":focus-visible {", self.css)
        self.assertIn("--focus-ring: #71D9EF;", self.css)
        self.assertRegex(self.css, r"outline: 2px solid var\(--focus-ring\);")

    def test_telegram_safe_area_is_consumed(self) -> None:
        # The authoritative check for this is the browser run in
        # tools/settings-ui-harness/checks/safe-area.mjs (object-shaped insets,
        # events fired, computed styles read back). This contract only guards the
        # shape of the code so the object-as-scalar bug cannot come back.
        root = self.css.split(":root {", 1)[1].split("\n}", 1)[0]
        for edge in ("top", "right", "bottom", "left"):
            self.assertRegex(root, rf"--safe-{edge}: max\(env\(safe-area-inset-{edge}\), var\(--tg-safe-{edge}, 0px\)\);")
        self.assertIn("min-height: min(100dvh, var(--tg-viewport-height, 100dvh));", self.css)

    def test_telegram_insets_are_read_as_objects_not_scalars(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        sync = source.split("function syncTelegramSafeArea()", 1)[1].split("\n  function authHeaders(", 1)[0]

        # Bot API 8.0 SafeAreaInset / ContentSafeAreaInset are
        # {top, bottom, left, right} objects; viewportHeight is a plain number.
        self.assertIn('const SAFE_AREA_EDGES = ["top", "bottom", "left", "right"];', source)
        self.assertIn("tg?.safeAreaInset?.[edge]", sync)
        self.assertIn("tg?.contentSafeAreaInset?.[edge]", sync)
        self.assertIn("tg?.viewportHeight", sync)
        self.assertIn("tg?.viewportStableHeight", sync)
        # No invented shape and no truthiness test: 0 is a valid inset, and a
        # stale value has to be overwritten on every sync.
        for forbidden in ("tg?.viewport?.height", "tg?.viewport.height"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("tg?.safeAreaInset,", sync)
        self.assertIn("Number.isFinite(value) && value > 0", source)
        for edge in ("top", "bottom", "left", "right"):
            self.assertIn(f'root.style.setProperty(`--tg-safe-${{edge}}`', sync)
        self.assertIn('root.style.removeProperty("--tg-viewport-height")', sync)
        for event in ("safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged", "viewportChanged"):
            self.assertIn(f'tg.onEvent("{event}", syncTelegramSafeArea)', source)

    def test_harness_stub_matches_the_official_webapp_shapes(self) -> None:
        stub = (_ROOT / "tools" / "settings-ui-harness" / "harness.py").read_text(encoding="utf-8")
        self.assertIn("safeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 }", stub)
        self.assertIn("contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 }", stub)
        self.assertIn("viewportHeight: 0", stub)
        self.assertIn("viewportStableHeight: 0", stub)
        self.assertIn('emit("safeAreaChanged"', stub)
        self.assertIn('emit("contentSafeAreaChanged"', stub)
        self.assertIn('emit("viewportChanged"', stub)
        self.assertTrue((_ROOT / "tools" / "settings-ui-harness" / "checks" / "safe-area.mjs").is_file())


class ProgressiveDisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_every_advanced_group_is_collapsed_by_default_but_reachable(self) -> None:
        self.assertIn("function advancedPanel(", self.source)
        for panel_id in (
            "bot.streaming",
            "bot.summary",
            "bot.context-budget",
            "bot.history-budget",
            "bot.memory-tuning",
            "tts.audio",
            "av.sources",
            "movie_info.imdb",
        ):
            self.assertIn(f'advancedPanel("{panel_id}"', self.source, f"missing advanced panel {panel_id}")

    def test_disclosure_state_is_captured_before_every_re_render(self) -> None:
        render_content = self.source.split("function renderContent", 1)[1].split("\n  function render()", 1)[0]
        load_all = self.source.split("async function loadAll", 1)[1].split("\n  async function reloadGroups", 1)[0]
        for body in (render_content, load_all):
            self.assertIn("captureAdvancedDisclosureStates();", body)
        self.assertIn("function captureAdvancedDisclosureStates()", self.source)
        self.assertIn("state.advancedOpen.set(panel.dataset.advancedPanel, panel.open);", self.source)
        self.assertIn("advancedOpen: new Map()", self.source)

    def test_disclosure_state_is_recorded_from_the_toggle_event(self) -> None:
        self.assertIn('content.addEventListener("toggle", event => {', self.source)
        self.assertIn('if (!panel?.matches?.("[data-advanced-panel]")) return;', self.source)

    def test_validation_failure_reveals_the_collapsed_field(self) -> None:
        validate = self.source.split("function validateConfig()", 1)[1].split("\n  function findNullNumber", 1)[0]
        self.assertIn("revealAdvancedDisclosure(invalid);", validate)
        self.assertIn("revealFieldByPath(nullPath);", validate)
        self.assertLess(
            validate.index("revealAdvancedDisclosure(invalid);"),
            validate.index("invalid.reportValidity();"),
        )
        reveal = self.source.split("function revealAdvancedDisclosure", 1)[1].split("\n  function revealFieldByPath", 1)[0]
        self.assertIn('node.matches?.("details.advanced-panel")', reveal)
        self.assertIn("state.advancedOpen.set(node.dataset.advancedPanel, true);", reveal)

    def test_field_help_is_an_accessible_disclosure(self) -> None:
        helper = self.source.split("function helpDisclosure", 1)[1].split("\n  function advancedPanel", 1)[0]
        self.assertIn('<details class="field-help"', helper)
        self.assertIn("<summary>", helper)
        field_fn = self.source.split("  function field(path, label, options = {})", 1)[1].split("\n  function toggle(", 1)[0]
        self.assertIn("helpDisclosure(options.help, options.helpLabel)", field_fn)
        toggle_fn = self.source.split("  function toggle(path, label, hint = \"\", full = false, help = \"\")", 1)[1].split("\n  function secretField(", 1)[0]
        self.assertIn("helpDisclosure(help)", toggle_fn)
        secret_fn = self.source.split("  function secretField(path, label, hint = \"\", help = \"\")", 1)[1].split("\n  // D3-42", 1)[0]
        self.assertIn("helpDisclosure(help)", secret_fn)

    def test_switching_tabs_never_writes_configuration(self) -> None:
        switch = self.source.split("  function switchTab(tab)", 1)[1].split("\n  document.addEventListener(\"click\"", 1)[0]
        for forbidden in ("apiFetch", "saveAllChanges", "persistConfigChanges", "persistGroupChanges"):
            self.assertNotIn(forbidden, switch)


class SaveButtonRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_pressing_save_after_typing_actually_saves(self) -> None:
        """Regression: the save button used to be repainted on every updateChrome().

        Blurring a field fires `change` -> updatePathControl -> updateChrome(),
        which rewrote saveButton.innerHTML while the pointer was already down.
        The mousedown target was removed, the browser never delivered a click and
        the save silently did nothing. The label is now only swapped when the
        saving phase really changes.
        """
        update_chrome = self.source.split("  function updateChrome()", 1)[1].split("\n  function pageHead(", 1)[0]
        self.assertIn("const savePhase = state.saving ? \"saving\" : \"idle\";", update_chrome)
        self.assertIn("if (saveButton.dataset.savePhase !== savePhase) {", update_chrome)
        window = update_chrome.split("if (saveButton.dataset.savePhase !== savePhase) {", 1)[1]
        self.assertIn("saveButton.innerHTML = state.saving", window)
        self.assertIn("saveButton.setAttribute(\"aria-label\"", update_chrome)
        # The label must not be written again after the guarded block closes.
        self.assertEqual(update_chrome.count("saveButton.innerHTML ="), 1)

    def test_saving_remains_an_explicit_action(self) -> None:
        self.assertIn('saveButton.addEventListener("click", saveAllChanges);', self.source)
        self.assertNotIn('data-action="save-group"', self.source)
        self.assertNotIn("setInterval", self.source.split("async function saveAllChanges", 1)[0][-4000:])


class UserFacingCopyTests(unittest.TestCase):
    """Settings copy must describe behaviour, not the construction history."""

    FORBIDDEN = (
        "D3-39",
        "D3-40",
        "第 ④ 期",
        "（B-34）",
        "getattr",
        "load_settings()",
        "恒真",
        "此前可 PUT",
        "与改动前一致",
        "Mini App 完全无入口",
        "已兼容修复",
    )

    def test_no_development_notes_reach_the_rendered_strings(self) -> None:
        rendered = strip_js_comments(_APP_JS.read_text(encoding="utf-8"))
        for token in self.FORBIDDEN:
            self.assertNotIn(token, rendered, f"{token!r} must not appear in user-visible settings copy")

    def test_required_safety_conditions_survived_the_copy_rewrite(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        # Hot-apply / restart notices, units and secret semantics are contract,
        # not decoration: they must still be on screen.
        self.assertIn("需重启", source)
        self.assertIn("保存后立即生效", source)
        self.assertIn("留空保留当前密钥", source)
        self.assertIn("保存后清除", source)
        self.assertIn("空白不会覆盖已保存的密钥", source)
        self.assertIn("留空会保留已保存值", source)
        for unit in ("（秒）", "（分钟）", "（毫秒）", "（小时）", "Token"):
            self.assertIn(unit, source)

    def test_page_and_section_copy_never_over_promises(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        rendered = strip_js_comments(source)

        # The Bot page carries bot.parse_mode, which is restart-required, so the
        # page head must scope the hot-apply promise instead of promising all of it.
        page_head = re.search(r'pageHead\("Bot 行为", "([^"]+)"\)', source)
        self.assertIsNotNone(page_head)
        description = page_head.group(1)
        self.assertIn("多数设置保存后立即生效", description)
        self.assertIn("需重启", description)
        self.assertNotIn("无需重启", description)
        self.assertNotRegex(description, r"(全部|所有|整页)[^。]*生效")

        # A section-level claim is fine as long as it is scoped to that section.
        memory_head = re.search(r'sectionHead\("长期记忆", "([^"]+)"\)', source)
        self.assertIsNotNone(memory_head)
        self.assertIn("本组开关保存后立即生效", memory_head.group(1))
        self.assertNotIn("这一页", memory_head.group(1))

        # Raw messages are cleaned up by the retention window; never claim otherwise.
        for absolute in ("原始消息不会被删除", "原始档案不会删除", "原文不会删除"):
            self.assertNotIn(absolute, rendered, f"{absolute!r} is not true: retention still cleans up")
        self.assertIn("原文按「原文保留天数」过期清理", source)

    def test_directional_copy_matches_the_real_layout(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        # bot.parse_mode is the first field of the 消息处理 grid, so the rich-text
        # toggle below it may only point upwards.
        self.assertIn("按上方「消息解析格式」", source)
        self.assertNotIn("按下方「消息解析格式」", source)
        # The long-term-memory master switch is a later section of the same page.
        self.assertIn("同页下方的「启用长期记忆（总开关）」", source)
        self.assertNotIn("下一页的「启用长期记忆", source)
        # Group-resource hints that were already correct must stay that way.
        for kept in ("留空使用上方全局秒数", "作为下方各类别的默认秒数"):
            self.assertIn(kept, source)

    def test_memory_copy_still_explains_the_master_switch(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("启用长期记忆（总开关）", source)
        self.assertIn("启用原始档案召回", source)
        self.assertIn("这不是长期记忆的总开关", source)


class ReleaseHygieneTests(unittest.TestCase):
    def test_harness_never_ships_with_the_page(self) -> None:
        for asset in ("app.js", "index.html", "styles.css"):
            source = (_ROOT / "bot" / "web" / "static" / asset).read_text(encoding="utf-8")
            for token in ("harness", "fixture", "mock", "localhost", "127.0.0.1", "demo-mode", "test-mode"):
                self.assertNotIn(token, source, f"{asset} must not reference {token}")

    def test_container_build_never_copies_the_harness(self) -> None:
        dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("COPY --chown=app:app bot ./bot", dockerfile)
        self.assertNotIn("tools", dockerfile)

    def test_harness_is_loopback_only_and_reads_only_the_fixture(self) -> None:
        harness = _HARNESS.read_text(encoding="utf-8")
        self.assertIn("refusing to bind a non-loopback address", harness)
        self.assertIn('args.host not in {"127.0.0.1", "localhost", "::1"}', harness)
        self.assertIn("FIXTURE = Path(__file__).resolve().parent / \"fixtures\" / \"settings.json\"", harness)
        for forbidden in (
            "import bot",
            "from bot",
            "load_dotenv",
            "settings_api",
            "aiosqlite",
            "sqlite3",
            "Authorization",
            "initData = \"harness=1\"",
        ):
            self.assertNotIn(forbidden, harness, f"harness must not use {forbidden}")
        # The stub only fakes the SDK surface the page already reads; it never
        # invents a session that bypasses Telegram initData verification.
        self.assertIn('initData: "harness=1"', harness)
        self.assertIn("self._json(state.fixture[key])", harness)
        self.assertIn('"/harness/telegram-stub.js"', harness)
        self.assertIn('"/settings"', harness)
        # The stub is a local file, so the preview never reaches the network:
        # the single https:// left in the harness is the CDN tag it *removes*
        # from the real index.html.
        self.assertIn("https://telegram.org/js/telegram-web-app.js", harness)
        self.assertEqual(harness.count("https://"), 1)
        self.assertTrue((_ROOT / "tools" / "settings-ui-harness" / "fixtures" / "settings.json").is_file())

    def test_static_asset_cache_buster_moved_with_the_redesign(self) -> None:
        html = _INDEX_HTML.read_text(encoding="utf-8")
        script = re.search(r"/settings-assets/app\.js\?v=([^\"]+)", html).group(1)
        style = re.search(r"/settings-assets/styles\.css\?v=([^\"]+)", html).group(1)
        self.assertEqual(script, style)
        self.assertIn("night-crystal", script)


if __name__ == "__main__":
    unittest.main()
