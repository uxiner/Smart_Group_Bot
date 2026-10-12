"""接入向导与成本面板的前端契约（Task 7）。

这些是**结构性**断言：读 app.js / styles.css 证明页面确实存在那些会误导人
的分支被挡住了（把未启用的护栏显示成正在保护、把未知金额显示成 0、把
申请人放进全局设置页）。它们不能替代真实浏览器证据 —— 浏览器那一层在
``.acceptance/`` 里单独标注为**未验证**。
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "bot" / "web" / "static" / "app.js"
_STYLES = _ROOT / "bot" / "web" / "static" / "styles.css"
_INDEX = _ROOT / "bot" / "web" / "static" / "index.html"


class OnboardingFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")
        self.styles = _STYLES.read_text(encoding="utf-8")

    def test_applicant_entry_never_reaches_global_settings(self) -> None:
        """403 时退到**仅申请**入口，且必须 short-circuit（不再去拉 settings）。"""

        self.assertIn("let sessionResult;", self.source)
        # 边界取"开始拉群列表"那一行：申请人分支本身也写了 state.session，
        # 用它当边界会把分支切掉一半。
        rescue = self.source.split("let sessionResult;", 1)[1].split(
            "const requests = [", 1
        )[0]
        # 申请人分支调用的是一个专门的**最小**入口助手（它本身不走
        # /api/v1/session，所以拿不到任何群或全局配置）。
        self.assertIn("loadOnboardingSession()", rescue)
        helper = self.source.split("async function loadOnboardingSession", 1)[1].split(
            "async function loadOnboardingRequests", 1
        )[0]
        self.assertIn('apiFetch("/api/v1/onboarding/session")', helper)
        self.assertIn("applicantOnly", rescue)
        branch = rescue.split("if (state.applicantOnly) {", 1)[1]
        # 申请人分支必须在**拉任何全局配置之前**返回。
        self.assertIn("content.innerHTML = applicantSessionView", branch)
        self.assertLess(
            branch.index("return;"),
            len(branch),
        )
        self.assertNotIn("/api/v1/settings", rescue)

    def test_applicant_view_has_no_settings_controls(self) -> None:
        view = self.source.split("function applicantSessionView", 1)[1].split(
            "function costPanelView", 1
        )[0]
        for forbidden in ("/api/v1/settings", "data-action=\"save-settings\""):
            self.assertNotIn(forbidden, view)

    def test_submit_guards_duplicate_clicks(self) -> None:
        submit = self.source.split("async function submitOnboardingRequest", 1)[1].split(
            "async function checkOnboardingReadiness", 1
        )[0]
        self.assertIn('button?.dataset.busy === "1"', submit)
        self.assertIn('if (button) button.dataset.busy = "1"', submit)
        self.assertIn("delete button.dataset.busy", submit)

    def test_group_id_input_rejects_non_group_ids_client_side(self) -> None:
        submit = self.source.split("async function submitOnboardingRequest", 1)[1].split(
            "async function checkOnboardingReadiness", 1
        )[0]
        self.assertIn("groupId >= 0", submit)

    def test_rate_limited_and_conflict_errors_are_surfaced_not_swallowed(self) -> None:
        submit = self.source.split("async function submitOnboardingRequest", 1)[1].split(
            "async function checkOnboardingReadiness", 1
        )[0]
        self.assertIn("catch (error)", submit)
        self.assertIn("onboardingError(error.message", submit)

    def test_preset_apply_carries_the_group_revision(self) -> None:
        apply_handler = self.source.split('if (action === "preset-apply")', 1)[1].split(
            'if (action === "reload-groups")', 1
        )[0]
        self.assertIn("revision: group.revision", apply_handler)
        # 409 时草稿必须留着（不刷新、不清 baseline）。
        self.assertIn("catch (error)", apply_handler)
        self.assertNotIn("state.groupBaselines.set", apply_handler.split("catch (error)", 1)[1])

    def test_preset_apply_button_is_disabled_without_a_preview(self) -> None:
        section = self.source.split("function renderGroupCostSection", 1)[1].split(
            "function accessRemoveLabel", 1
        )[0]
        self.assertIn('${preview ? "" : " disabled"}', section)

    def test_switching_preset_drops_the_stale_preview(self) -> None:
        handler = self.source.split('if (action === "select-preset")', 1)[1].split(
            'if (action === "preset-preview")', 1
        )[0]
        self.assertIn("presetPreview?.delete(groupId)", handler)

    def test_readiness_says_no_remote_probe_was_done(self) -> None:
        render = self.source.split("function renderReadinessReport", 1)[1].split(
            "function renderCostSummary", 1
        )[0]
        self.assertIn("未做远端连通性测试", render)

    def test_no_external_assets_or_gradients_added(self) -> None:
        added = self.styles.split("/* ---- 新群接入 / 首次预设 / 按群成本", 1)[1]
        self.assertNotIn("gradient", added)
        self.assertNotIn("http://", added)
        self.assertNotIn("https://", added)
        self.assertNotIn("@import", added)

    def test_touch_targets_are_at_least_44px(self) -> None:
        added = self.styles.split("/* ---- 新群接入 / 首次预设 / 按群成本", 1)[1]
        self.assertIn("min-height: 44px", added)

    def test_small_viewports_keep_one_column(self) -> None:
        added = self.styles.split("/* ---- 新群接入 / 首次预设 / 按群成本", 1)[1]
        self.assertIn("@media (max-width: 390px)", added)
        self.assertIn("grid-template-columns: 1fr", added)


class CostFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_disabled_budget_is_never_rendered_as_protecting(self) -> None:
        render = self.source.split("function renderBudgetPanel", 1)[1].split(
            "function renderCostSummary", 1
        )[0]
        self.assertIn('budget.protecting', render)
        self.assertIn("未启用", render)
        self.assertIn("当前没有任何额度限制", render)

    def test_unknown_amount_is_never_rendered_as_zero(self) -> None:
        self.assertIn("金额未知（缺少单价）", self.source)
        formatter = self.source.split("function formatMicro", 1)[1].split(
            "function formatTokens", 1
        )[0]
        self.assertIn("if (amount == null)", formatter)
        self.assertNotIn("|| 0", formatter)

    def test_unknown_tokens_are_labelled_not_zeroed(self) -> None:
        formatter = self.source.split("function formatTokens", 1)[1].split(
            "function applicantSessionView", 1
        )[0]
        self.assertIn('return "未知"', formatter)

    def test_ledger_and_legacy_table_are_declared_different(self) -> None:
        """界面必须把「两张表口径不同、不要相加」这句话显示出来。"""

        render = self.source.split("function renderCostSummary", 1)[1].split(
            "function onboardingError", 1
        )[0]
        # 口径说明由**后端**给出（它知道两张表的真实差异），前端必须显示出来
        # 而不是自己编一句：编出来的口径迟早和库里的实现对不上。
        self.assertIn("source_note", render)
        self.assertIn("payload?.source_note", render)

    def test_unmetered_work_is_called_out(self) -> None:
        render = self.source.split("function renderBudgetPanel", 1)[1].split(
            "function renderCostSummary", 1
        )[0]
        self.assertIn("未计量项", render)

    def test_cost_panel_does_not_auto_load_every_group(self) -> None:
        """一次性把所有群的用量都拉一遍会把"打开面板"变成查询风暴。"""

        loader = self.source.split("async function loadGroupCostPanel", 1)[1].split(
            "async function reloadGroups", 1
        )[0]
        self.assertIn("/costs?", loader)
        # 预算不再在 loader 里拼 URL，而是走统一的 fetchGroupBudget(groupId)：
        # 真实合同（嵌套 limits / 409 / 回读确认）都收在那一个函数里。要盯的是
        # "同一个群里成本和预算一起取"，以及那个函数**确实**打 /budget。
        self.assertIn("fetchGroupBudget(groupId)", loader)
        fetcher = self.source.split("function fetchGroupBudget", 1)[1].split(
            "\n  async function ", 1
        )[0]
        self.assertIn("/budget", fetcher)
        section = self.source.split("function renderGroupCostSection", 1)[1].split(
            "function accessRemoveLabel", 1
        )[0]
        panel = self.source.split("function costPanelView", 1)[1].split(
            "async function loadOnboardingSession", 1
        )[0]
        # 只有显式的「刷新」按钮会触发加载；渲染本身不发请求。
        self.assertIn('data-action="cost-reload"', panel)
        self.assertIn("${costPanelView(group)}", section)
        self.assertNotIn("loadGroupCostPanel(", section)
        self.assertNotIn("loadGroupCostPanel(", panel)
        self.assertNotIn(".map(group => loadGroupCostPanel", self.source)

    def test_cost_and_budget_views_never_render_a_price_catalog_blob(self) -> None:
        """成本面板只展示台账与护栏状态，不把价格目录原文倒出来。"""

        for marker in ("function costPanelView", "function renderCostSummary"):
            block = self.source.split(marker, 1)[1].split("\n  function ", 1)[0]
            for forbidden in ("api_key", "price_catalog", "catalog.entries"):
                self.assertNotIn(forbidden, block, f"{marker}: {forbidden}")

    def test_index_html_still_pins_cache_busters(self) -> None:
        index = _INDEX.read_text(encoding="utf-8")
        self.assertRegex(index, r'/settings-assets/app\.js\?v=[^"]+" defer></script>')
        self.assertRegex(index, r'/settings-assets/styles\.css\?v=[^"]+">')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class CostPanelBindingTests(unittest.TestCase):
    """多群页面上的绑定与失败态（第七阶段浏览器实测暴露的问题）。"""

    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_cost_panel_is_selected_by_group_not_the_first_match(self) -> None:
        """``[data-role=cost-summary]`` 在多群页面永远取第一个 → 串群。"""

        loader = self.source.split("function costPanelRoot", 1)[1].split(
            "function renderBudgetPanel", 1
        )[0]
        self.assertIn("data-cost-group", loader)
        self.assertIn("CSS.escape(String(groupId))", loader)
        render = self.source.split("function renderCostSummary", 1)[1].split(
            "function onboardingError", 1
        )[0]
        self.assertIn("panel?.querySelector", render)
        self.assertNotIn("content.querySelector('[data-role=\"cost-summary\"]')", render)

    def test_budget_load_failure_is_not_rendered_as_disabled(self) -> None:
        """Promise.allSettled 把预算失败变成 null → 显示成「未启用」，掩盖故障。"""

        panel = self.source.split("function renderBudgetPanel", 1)[1].split(
            "function renderCostSummary", 1
        )[0]
        self.assertIn("budgetState.status === \"rejected\"", panel)
        self.assertIn("状态未知", panel)
        self.assertIn("无法读取预算配置", panel)
        self.assertIn("请勿假定额度仍然生效", panel)

    def test_cost_read_failure_says_unknown_and_never_renders_zero(self) -> None:
        """读失败 = **不知道**，不是"这个群没花钱"。

        浏览器实测（``tools/settings-ui-harness/checks/real-api-costs.mjs`` 的 503 臂）
        曾经只看到一句原始错误消息：既没说状态未知，也没有任何"别按 0 估"的提示，
        而 `Promise.allSettled` 的另一半（预算）会照常画出"今日已用 0.000000" ——
        两件事叠在一起，读者很容易把一次读取故障读成"这个群今天没花钱"。
        """

        loader = self.source.split("function loadGroupCostPanel", 1)[1].split(
            "function renderBudgetPanel", 1
        )[0]
        self.assertIn('costs.status === "rejected"', loader)
        self.assertIn("用量与金额状态未知", loader)
        self.assertIn("不要假定本群没有花费", loader)
        self.assertIn("不要按 0 估算", loader)
        failure_branch = loader.split('costs.status === "rejected"', 1)[1].split("return;", 1)[0]
        self.assertNotIn("renderCostSummary", failure_branch, "读失败仍然去画成本汇总")
        self.assertNotIn("formatMicro", failure_branch, "读失败分支里出现了金额格式化")

    def test_preset_selection_no_longer_calls_an_undefined_chosen(self) -> None:
        handler = self.source.split('if (action === "onboarding-preset")', 1)[1].split(
            'if (action === "onboarding-check")', 1
        )[0]
        self.assertNotIn("chosen(button)", handler)
        self.assertIn('state.onboardingPreset === next ? "" : next', handler)

    def test_preset_actions_use_the_real_dom_render_entry(self) -> None:
        """``renderGroups()`` 只 return HTML，从不更新 DOM —— 必须用 render()。"""

        for action in ("select-preset", "preset-preview", "preset-apply"):
            block = self.source.split(f'if (action === "{action}")', 1)[1].split(
                "if (action === ", 1
            )[0]
            self.assertNotIn(
                "renderGroups();", block, f"{action} 仍在调用无副作用的 renderGroups()"
            )
            self.assertIn("render();", block, f"{action} 没有走真实渲染入口")

    def test_preset_preview_reads_the_diff_field(self) -> None:
        self.assertIn("preview.diff", self.source)

    def test_readiness_report_handles_the_nested_envelope(self) -> None:
        """check 现在把就绪放在 payload.readiness 里，前端必须跟着改。"""

        report = self.source.split("function renderReadinessReport", 1)[1].split(
            "function costPanelRoot", 1
        )[0]
        self.assertIn("payload?.readiness || payload", report)
        self.assertIn("readiness?.capabilities", report)

    def test_empty_readiness_document_says_unknown_instead_of_staying_blank(
        self,
    ) -> None:
        """父代理看到的空标题：文档里一条能力都没有时必须明说看不出来。"""

        report = self.source.split("function renderReadinessReport", 1)[1].split(
            "function costPanelRoot", 1
        )[0]
        self.assertIn("readiness-empty", report)
        self.assertIn("无法判断是否就绪", report)
        # 这条提示必须挂在"条目为空"的分支上，而不是无条件印出来。
        self.assertIn("capabilities.length", report)
        self.assertLess(
            report.index("capabilities.length"), report.index("无法判断是否就绪")
        )

    def test_readiness_panel_never_promises_the_preset_was_applied(self) -> None:
        report = self.source.split("function renderReadinessReport", 1)[1].split(
            "function costPanelRoot", 1
        )[0]
        self.assertIn("不会应用预设，也不会授权本群", report)
        self.assertNotIn("已应用", report)

    def test_readiness_panel_shows_the_checked_features_and_blockers(self) -> None:
        report = self.source.split("function renderReadinessReport", 1)[1].split(
            "function costPanelRoot", 1
        )[0]
        self.assertIn("本次检查的功能", report)
        self.assertIn("阻断项", report)
        self.assertIn("readiness?.selected_features", report)


class ReadinessPanelStyleTests(unittest.TestCase):
    """空文档提示必须是"警示"而不是悄悄消失；也不能引进外部资源。"""

    def setUp(self) -> None:
        self.styles = _STYLES.read_text(encoding="utf-8")

    def test_empty_readiness_note_is_visible_and_warns(self) -> None:
        self.assertIn(".readiness-empty", self.styles)
        empty = self.styles.split(".readiness-empty", 1)[1].split("}", 1)[0]
        self.assertIn("var(--warning)", empty)

    def test_readiness_notes_never_overflow_small_viewports(self) -> None:
        self.assertIn(".readiness-note", self.styles)
        note = self.styles.split(".readiness-note", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-wrap: anywhere", note)

    def _rule(self, selector: str) -> str:
        self.assertIn(selector, self.styles)
        return self.styles.split(selector, 1)[1].split("}", 1)[0]

    def _narrow_block(self) -> str:
        marker = "@media (max-width: 480px)"
        self.assertIn(marker, self.styles)
        return self.styles.split(marker, 1)[1].split("\n}", 1)[0]

    def test_diagnostic_rows_stack_on_narrow_screens_instead_of_overlapping(self) -> None:
        """360/390px 真的叠印过：``provider_missing`` / ``permission_missing``
        这类长状态码画出自己的盒，压到 main/decision/moderation 这些能力名上。

        机制是**窄屏各占一行 + 长单词在自己的盒里换行**。这里钉住结构；真正的
        几何证据在 ``checks/real-api-check.mjs`` 的几何臂（Range 墨迹盒 + 元素盒）。
        **不许**用隐藏/裁切掩盖：``overflow: hidden`` / ``text-overflow: ellipsis``
        / ``white-space: nowrap`` 一出现就等于把问题盖起来。
        """

        item = self._rule(".readiness-item")
        self.assertIn("display: grid", item)
        self.assertIn("grid-template-columns", item)

        narrow = self._narrow_block()
        self.assertIn(".readiness-item", narrow)
        self.assertIn("minmax(0, 1fr)", narrow)
        for selector in (".readiness-state", ".readiness-capability", ".readiness-detail"):
            self.assertIn(selector, narrow)
            self.assertIn("grid-column: 1", narrow)
            self.assertIn("overflow-wrap: anywhere", self._rule(selector))

        for forbidden in (
            "overflow: hidden",
            "overflow-x: hidden",
            "text-overflow: ellipsis",
            "white-space: nowrap",
        ):
            self.assertNotIn(forbidden, item)
            self.assertNotIn(forbidden, self._rule(".readiness-state"))
            self.assertNotIn(forbidden, self._rule(".readiness-detail"))
