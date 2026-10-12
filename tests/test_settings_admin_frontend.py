"""超管审批队列的前端契约（原 Task 7 缺口 A）。

结构性断言：读 app.js / styles.css 证明这些分支真的存在 ——
全局导航里**真的有**入口、只有最高管理员才去拉全局队列、审批按钮只在 pending
上出现、决策后有**真实读回**而不是本地删卡片、并发/重复点击被挡住、错误与草稿
在重绘之后仍然存在。

它们**不能**替代真实浏览器证据（那一层由 `tools/loopback-ui/` 的脚本与父的
独立复验负责）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "bot" / "web" / "static" / "app.js"
_STYLES = _ROOT / "bot" / "web" / "static" / "styles.css"


class ApprovalQueueFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")
        self.styles = _STYLES.read_text(encoding="utf-8")

    def queue_body(self) -> str:
        return self.source[
            self.source.index("// ---- 超管审批队列"):
            self.source.index("function renderReadinessReport")
        ]

    # ------------------------------------------------------------------
    def test_super_admin_nav_entry_really_exists(self) -> None:
        """全局导航里必须有 onboarding 入口 —— 否则整个队列不可达。"""

        nav_block = self.source[
            self.source.index("const ALL_NAV_ITEMS = ["):
            self.source.index("const GROUP_SECTION_NAV")
        ]
        self.assertIn('{ id: "onboarding"', nav_block, "全局导航没有新群接入入口")
        self.assertIn('label: "新群接入"', nav_block)
        # 非超管的 navItems() 只给 groups：入口天然是超管专属
        nav_items = self.source[
            self.source.index("function navItems()"):
            self.source.index("function navItems()") + 400
        ]
        self.assertIn('ALL_NAV_ITEMS.filter(item => item.id === "groups")', nav_items)

    def test_onboarding_renderer_is_registered(self) -> None:
        """renderContent 必须认识 onboarding，否则会 fallback 成群组页。"""

        renderers = self.source[
            self.source.index("const renderers = {"):
            self.source.index("const renderer = renderers[state.activeTab]")
        ]
        self.assertIn("onboarding:", renderers)
        self.assertIn("applicantSessionView(state.session)", renderers)

    def test_queue_panel_is_super_admin_only(self) -> None:
        self.assertIn('data-role="onboarding-queue-panel"', self.source)
        panel_index = self.source.index('data-role="onboarding-queue-panel"')
        window = self.source[max(0, panel_index - 400):panel_index]
        self.assertIn("can_manage_global", window, "队列面板没有角色门")

        body = self.source[
            self.source.index("async function loadOnboardingQueue"):
            self.source.index("function renderQueueStatus")
        ]
        guard = body.index("can_manage_global")
        fetch = body.index("/api/v1/onboarding/requests?status=pending")
        self.assertLess(guard, fetch, "角色判断必须在发请求之前")
        # 非超管必须 short-circuit，而且返回的**不是**一次成功读取
        self.assertIn('return { ok: false, reason: "absent" }', body[:fetch])

    def test_queue_loads_once_per_entry(self) -> None:
        """switchTab 与 renderContent 不能各拉一遍。"""

        switch = self.source[
            self.source.index("function switchTab(tab) {"):
            self.source.index("function switchTab(tab) {") + 1200
        ]
        self.assertNotIn(
            "loadOnboardingQueue", switch, "switchTab 不该再拉一次（renderContent 已拉）"
        )
        render = self.source[
            self.source.index("if (state.activeTab === \"onboarding\")"):
            self.source.index("restoreResourceFormDrafts();")
        ]
        self.assertIn("loadOnboardingRequests(state.session)", render)
        self.assertIn("loadOnboardingQueue()", render)

    def test_stale_async_response_cannot_overwrite_the_view(self) -> None:
        body = self.source[
            self.source.index("async function loadOnboardingQueue"):
            self.source.index("function renderQueueStatus")
        ]
        self.assertIn("const seq = ++state.queueSeq;", body)
        # 成功与失败两条路径都要检查；被淘汰的那次必须报告"superseded"，
        # 不许让调用方以为它读到了服务器状态。
        self.assertEqual(body.count('if (seq !== state.queueSeq) return { ok: false'), 2, body)
        self.assertEqual(body.count('reason: "superseded"'), 2, body)

    def test_draft_is_remembered_while_typing(self) -> None:
        """只在失败 catch 里 remember 是不够的：刷新/切页会丢用户正在打的字。"""

        listener = self.source[
            self.source.index('content.addEventListener("input", event => {'):
            self.source.index('content.addEventListener("input", event => {') + 900
        ]
        self.assertIn('target?.dataset?.role === "decision-reason"', listener)
        self.assertIn("rememberQueueDraft(", listener)
        # 重绘时必须从草稿恢复
        body = self.queue_body()
        self.assertIn("const draft = queueDraftFor(requestId);", body)
        self.assertIn('value="${attr(draft)}"', body)

    def test_error_survives_a_rebuild(self) -> None:
        """错误必须存在 state 里：先写 DOM 再重建卡片会把刚写的错误冲掉。"""

        body = self.queue_body()
        self.assertIn("function setQueueError(requestId, message)", body)
        self.assertIn("state.decisionErrors.set(requestId, message)", body)
        self.assertIn("const error = queueErrorFor(requestId);", body)
        self.assertIn("${escapeHtml(error)}", body)
        # 失败后 fresh-read 仍保留错误
        readback = body[
            body.index("async function refreshQueueAfterDecision"):
        ]
        self.assertIn("keepErrors", readback)
        self.assertIn("if (savedError) setQueueError(requestId, savedError);", readback)

    def test_get_failure_never_pretends_success(self) -> None:
        """POST 成功不等于决策生效：读回失败必须明说"尚未确认"，且不弹成功。"""

        body = self.queue_body()
        decide = body[
            body.index("async function decideOnboardingRequest"):
            body.index("function setDecisionBusy")
        ]
        self.assertIn("const confirmation = await refreshQueueAfterDecision", decide)
        self.assertIn("if (confirmation.confirmed) {", decide)
        self.assertIn("操作已提交（服务端返回成功），但状态尚未确认：", decide)
        self.assertIn('showToast("已提交，但状态未确认", "warning")', decide)
        # 成功 toast 只能出现在"读回确认"分支里
        success_toast = decide.index('"success",')
        self.assertLess(decide.index("if (confirmation.confirmed) {"), success_toast)
        self.assertIn("并已从服务器读回确认。", decide)

        readback = body[body.index("async function refreshQueueAfterDecision"):]
        # 每一个"没读到服务器状态"的出口都必须显式 confirmed: false
        self.assertGreaterEqual(readback.count("confirmed: false"), 5, readback)
        self.assertEqual(readback.count("confirmed: true"), 1, readback)
        self.assertIn("读取失败：", readback)
        self.assertIn("不构成确认", readback)

    def test_read_failure_keeps_the_list_and_disables_decisions(self) -> None:
        """读失败不许清空 DOM：那会把用户正在写的理由从界面上抹掉。"""

        body = self.queue_body()
        # 队列段里 `} catch (error) {` 有好几处，必须**从 loader 自己**起切；而且
        # catch 里第一句就是 seq 检查，所以不能拿它当切片终点（会切出空串，
        # 断言"凭空"通过/失败）。这里取 catch 到函数结尾。
        loader = body[
            body.index("async function loadOnboardingQueue"):
            body.index("function renderQueueStatus")
        ]
        catch = loader[loader.index("} catch (error) {"):]
        self.assertIn("state.queueStale = true;", catch)
        self.assertIn("state.queueFailureMessage =", catch)
        self.assertIn("renderPendingQueue(state.pendingQueue, status, {", catch)
        self.assertIn("failure: state.queueFailureMessage", catch)
        self.assertNotIn('box.innerHTML = ""', catch, "读失败不能清空列表")
        # 失败必须作为**显式结果**交给调用方，不能吞成一次"成功"
        self.assertIn('return { ok: false, reason: "failed", error };', catch)

        stale = body[body.index("function renderQueueFailureStatus"):body.index("function renderPendingQueue")]
        self.assertIn("可能已过期", stale)
        self.assertIn("决策已禁用", stale)

    def test_read_failure_status_is_not_success_wording(self) -> None:
        body = self.queue_body()
        status = body[body.index("function renderQueueStatus"):body.index("function renderPendingQueue")]
        failure = status[status.index("function renderQueueFailureStatus"):]
        self.assertIn("队列读取失败：", failure)
        self.assertNotIn("队列已刷新", failure)

    def test_lock_is_per_request_not_per_decision(self) -> None:
        """批准/拒绝必须共用同一把按 requestId 的锁，重绘也绕不过。"""

        body = self.queue_body()
        decide = body[body.index("async function decideOnboardingRequest"):]
        self.assertIn("if (state.decisionsInFlight.has(requestId)) return;", decide)
        self.assertIn("state.decisionsInFlight.set(requestId, decision);", decide)
        # 卡片重绘时按锁禁用输入与按钮
        render = body[body.index("function renderPendingQueue"):body.index("function decisionError")]
        self.assertIn("const busy = state.decisionsInFlight.has(requestId);", render)
        self.assertIn("const blocksDecision = decisionBlocked(requestId);", render)
        self.assertIn('const disabled = blocksDecision ? " disabled" : "";', render)
        self.assertIn("${disabled}>", render)
        self.assertIn("function setDecisionBusy(requestId, busy)", body)
        # 退出 in-flight 时不能无条件放开：必须按**同一个**谓词重算
        busy_fn = body[
            body.index("function setDecisionBusy(requestId, busy)"):
            body.index("const QUEUE_CONFIRM_MAX_PAGES")
        ]
        self.assertIn("node.disabled = busy ? true : decisionBlocked(requestId);", busy_fn)
        self.assertIn("function decisionBlocked(requestId)", body)
        predicate = body[
            body.index("function decisionBlocked(requestId)"):
            body.index("function decisionBlocked(requestId)") + 400
        ]
        for term in (
            "state.decisionsInFlight.has(requestId)",
            "state.queueStale",
            "state.decisionsPendingConfirmation.has(requestId)",
        ):
            self.assertIn(term, predicate, term)
        # 只有在确认完成（成功或明确失败）之后才解锁
        self.assertIn("if (state.decisionsInFlight.get(requestId) === decision)", decide)

    def test_decide_buttons_only_render_for_pending(self) -> None:
        body = self.queue_body()
        self.assertIn('const pending = String(row.status || "") === "pending";', body)
        self.assertIn("${pending ?", body)
        self.assertIn("这条申请已经终结，不能再决策。", body)
        pending_branch = body[body.index("${pending ?"):]
        self.assertIn('data-action="onboarding-approve"', pending_branch)
        self.assertIn('data-action="onboarding-reject"', pending_branch)

    def test_reason_is_escaped_not_inner_html(self) -> None:
        body = self.queue_body()
        self.assertIn("escapeHtml(String(row.reason))", body)
        for field in ("row.group_id", "row.applicant_id", "row.created_at"):
            self.assertIn(f"escapeHtml(String({field}", body, field)
        # 后端 reason 是**审核理由**字段，不能写成申请人理由
        self.assertIn("审核理由", body)
        self.assertNotIn("申请理由", body)

    def test_pagination_uses_real_limit_offset_and_total(self) -> None:
        body = self.queue_body()
        self.assertIn("const QUEUE_PAGE_SIZE = 50;", body)
        self.assertIn("?status=pending&limit=${QUEUE_PAGE_SIZE}&offset=${offset}", body)
        self.assertIn('action = "onboarding-queue-more"', body)
        self.assertIn("state.queueTotal", body)
        self.assertIn("还有 ${state.queueTotal - state.queueOffset} 条", body)
        # 截断必须被明说，不能假称这就是全部
        status = body[body.index("function renderQueueStatus"):body.index("function renderPendingQueue")]
        self.assertIn("已显示 ${shown} / ${total} 条待审批", status)
        self.assertIn("loadOnboardingQueue({ append: true })", self.source)

    def test_unconfirmed_decision_is_not_reposted(self) -> None:
        """POST 已被接受但没读回确认时：卡片不能再决策，也不许再发一次 POST。"""

        body = self.queue_body()
        self.assertIn("if (!(state.decisionsPendingConfirmation instanceof Map))", body)
        decide = body[
            body.index("async function decideOnboardingRequest"):
            body.index("function setDecisionBusy")
        ]
        guard = decide.index("if (state.decisionsPendingConfirmation.has(requestId)) {")
        post = decide.index("await apiFetch(")
        self.assertLess(guard, post, "待确认时必须在发 POST 之前挡住")
        self.assertIn("在此之前不要再重复决策", decide)
        # POST 成功后先记"待确认"再做核对；只有读回确认才清草稿
        remember = decide.index("state.decisionsPendingConfirmation.set(requestId, {")
        confirm = decide.index("const confirmation = await refreshQueueAfterDecision")
        self.assertLess(remember, confirm)
        confirmed_branch = decide.index("if (confirmation.confirmed) {")
        self.assertLess(confirmed_branch, decide.index("forgetQueueDraft(requestId);"))
        self.assertIn("state.decisionsPendingConfirmation.delete(requestId);", decide)
        # 卡片上要有显式的"已提交、尚未确认"标记
        self.assertIn('data-role="decision-pending-confirmation"', body)
        self.assertIn(
            "const awaitingConfirmation = state.decisionsPendingConfirmation.has(requestId);",
            body,
        )
        busy_fn = body[
            body.index("function setDecisionBusy"):
            body.index("const QUEUE_CONFIRM_MAX_PAGES")
        ]
        self.assertIn("note.hidden = !state.decisionsPendingConfirmation.has(requestId);", busy_fn)

    def test_refresh_resolves_pending_confirmations(self) -> None:
        """刷新/回到面板也是"已提交未确认"的收口入口 —— 但只认服务器的终结状态。"""

        body = self.queue_body()
        self.assertIn("async function resolvePendingConfirmations()", body)
        resolve_fn = body[body.index("async function resolvePendingConfirmations()"):]
        self.assertIn("[...state.decisionsPendingConfirmation.keys()]", resolve_fn)
        self.assertIn("const confirmation = await refreshQueueAfterDecision(requestId, {", resolve_fn)
        # 没确认就**继续挂着**：绝不因为"用户点了刷新"就宣布成功
        self.assertIn("if (!confirmation.confirmed) continue;", resolve_fn)
        self.assertIn("forgetQueueDraft(requestId);", resolve_fn)
        self.assertIn("state.decisionsPendingConfirmation.delete(requestId);", resolve_fn)

        refresh = self.source[
            self.source.index('if (action === "onboarding-queue-refresh")'):
            self.source.index('if (action === "onboarding-queue-more")')
        ]
        self.assertIn("await resolvePendingConfirmations();", refresh)
        # 进入面板（renderContent）也要收口，否则"离开再回来"永远挂着
        self.assertIn(
            ".then((outcome) => (outcome?.ok ? resolvePendingConfirmations() : null))",
            self.source,
        )

    def test_decision_reads_back_instead_of_faking_success(self) -> None:
        body = self.queue_body()
        readback = body[body.index("async function refreshQueueAfterDecision"):]
        self.assertIn("const queueOutcome = await loadOnboardingQueue();", readback)
        self.assertIn("if (!queueOutcome.ok) {", readback)
        self.assertIn("loadOnboardingRequests(state.session).catch(() => {});", readback)
        # 确认只认**服务器**上的终结状态：
        self.assertIn('const stillPending = await findRequestStatus(requestId, "pending");', readback)
        self.assertIn('decision === "approve" ? "approved" : decision === "reject" ? "rejected" : ""', readback)
        self.assertIn("const terminal = await findRequestStatus(requestId, expected);", readback)
        self.assertIn("if (terminal.found) {", readback)
        # 列表没翻完 = 未确认，不许猜
        self.assertIn("exhausted", readback)
        # 后端没有单条 GET：不许编造一个不存在的接口
        self.assertNotIn("requests/${encodeURIComponent(requestId)}`", readback)
        finder = body[
            body.index("async function findRequestStatus"):
            body.index("async function refreshQueueAfterDecision")
        ]
        self.assertIn("`limit=${limit}&offset=${offset}", finder)
        self.assertIn("maxPages", finder)

    def test_error_keeps_the_draft(self) -> None:
        decide = self.queue_body()
        decide = decide[decide.index("async function decideOnboardingRequest"):]
        self.assertIn("rememberQueueDraft(requestId, reason);", decide)
        self.assertIn("理由已保留，可重试。", decide)

    def test_drafts_are_isolated_per_request(self) -> None:
        body = self.source[
            self.source.index("function queueDraftFor"):
            self.source.index("async function loadOnboardingQueue")
        ]
        self.assertIn("state.decisionDrafts.set(requestId, value);", body)
        self.assertIn("state.decisionDrafts.get(requestId)", body)
        self.assertIn("state.decisionDrafts.delete(requestId)", body)

    def test_approve_response_semantics_are_spelled_out(self) -> None:
        body = self.source[
            self.source.index("function decisionOutcomeText"):
            self.source.index("async function decideOnboardingRequest")
        ]
        self.assertIn("payload.already_decided", body)
        self.assertIn("payload.group_authorized === false", body)
        self.assertIn("payload.applicant_granted === false", body)
        self.assertIn("payload.current", body)
        self.assertIn("本次没有新增群授权", body)
        self.assertIn("本次没有新增申请人授权", body)
        # 已授权 ≠ 已加入 必须说清楚
        self.assertIn("已授权 ≠ 已加入", body)

    def test_queue_refresh_action_is_wired(self) -> None:
        self.assertIn('action === "onboarding-queue-refresh"', self.source)
        self.assertIn('action === "onboarding-queue-more"', self.source)
        self.assertIn('action === "onboarding-approve"', self.source)
        self.assertIn('action === "onboarding-reject"', self.source)

    def test_selector_values_are_css_escaped(self) -> None:
        self.assertIn("function cssEscape(value)", self.source)
        self.assertIn("cssEscape(requestId)", self.queue_body())

    def test_aria_live_and_touch_targets(self) -> None:
        self.assertIn('data-role="onboarding-queue-status"', self.source)
        self.assertIn('aria-live="polite"', self.source)
        self.assertIn('role="alert"', self.source)
        self.assertIn('for="decision-reason-', self.source)
        self.assertIn('id="decision-reason-', self.source)
        self.assertIn(".request-card .button-row .primary-button", self.styles)
        self.assertIn("min-height: 44px;", self.styles)
        self.assertIn(".request-card .text-input { min-height: 44px; }", self.styles)

    def test_responsive_layout_for_mobile_and_desktop(self) -> None:
        self.assertIn('[data-role="onboarding-queue"]', self.styles)
        self.assertIn("@media (min-width: 1280px)", self.styles)
        self.assertIn("overflow-wrap: anywhere;", self.styles)

    def test_no_new_library_or_backdoor_in_static_assets(self) -> None:
        self.assertNotIn("http://", self.source)
        self.assertNotIn("https://cdn", self.source)
        self.assertNotIn("debug_token", self.source)
        self.assertNotIn("bypass_auth", self.source)
        self.assertNotIn("localStorage", self.queue_body())


class BudgetEditorFrontendTests(unittest.TestCase):
    """按群预算编辑器的前端契约（原 Task 7 缺口 B）。"""

    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")
        self.styles = _STYLES.read_text(encoding="utf-8")

    def body(self) -> str:
        return self.source[
            self.source.index("// ---- 按群预算编辑"):
            self.source.index("async function loadGroupCostPanel(groupId)")
        ]

    def test_editor_is_super_admin_only(self) -> None:
        panel = self.source[
            self.source.index("function renderBudgetPanel(budgetBox, budgetState, costs)"):
            self.source.index("function renderCostSummary")
        ]
        self.assertIn("const editable = Boolean(state.session?.can_manage_global);", panel)
        self.assertIn("renderBudgetEditor(groupId, draft)", panel)

    def test_read_failure_never_renders_a_saveable_form(self) -> None:
        """读不出来时不能画成"未启用、可以存"。"""

        panel = self.source[
            self.source.index("function renderBudgetPanel(budgetBox, budgetState, costs)"):
            self.source.index("function renderCostSummary")
        ]
        rejected = panel.index('if (budgetState.status === "rejected")')
        editor = panel.index("renderBudgetEditor(")
        self.assertLess(rejected, editor, "读取失败的 return 必须在编辑表单之前")
        self.assertIn("状态未知", panel)

    def test_blank_means_unlimited_and_zero_means_disallow(self) -> None:
        body = self.body()
        self.assertIn("空白 = 不限制", body)
        self.assertIn("0 = 不允许", body)
        self.assertIn("parseStrictLimit", body)

    def test_strict_integer_parsing_rejects_trailing_garbage(self) -> None:
        """绝不 parseInt：'12abc' / '1.9' / '-1' 都必须被拒，不能悄悄截断。"""

        body = self.body()
        self.assertIn("if (!/^\\d+$/.test(text)) return { ok: false, value: null };", body)
        self.assertNotIn("parseInt(", body)
        self.assertIn("不接受小数、负号或夹杂其它字符", body)

    def test_micro_currency_unit_is_explained(self) -> None:
        body = self.body()
        self.assertIn("微货币", body)
        self.assertIn("1,000,000 微 = 1 个货币单位", body)

    def test_currency_is_iso_three_letters(self) -> None:
        body = self.body()
        self.assertIn("function parseCurrency(raw)", body)
        self.assertIn("/^[A-Z]{3}$/", body)
        self.assertIn("ISO 三字母", body)

    def test_structured_operation_limits_and_money_covers(self) -> None:
        body = self.body()
        self.assertIn('"operations"', body)
        self.assertIn('"covers"', body)
        self.assertIn("budget-kv-add", body)
        self.assertIn("budget-kv-remove", body)
        self.assertIn("operation_limits: Object.fromEntries(", body)
        # 真实 API 收的是**数组** ``[{provider_key, model}]``。曾经发的是
        # ``{key: number}`` 映射，服务端直接 400「money_covers 必须是数组」，
        # 于是"带金额上限保存"永远失败。
        self.assertIn(
            "money_covers: values.covers.map(row => ({ provider_key: row.key, model: row.value })),",
            body,
        )
        self.assertNotIn("money_covers: Object.fromEntries(", body)
        # 第二格是模型名，不是次数：给它 inputmode=numeric 手机上根本打不出模型名。
        self.assertIn(
            'kvRowsMarkup("covers", draft.values.covers, "provider_key", '
            '{ numeric: false, valueHint: "model" })',
            body,
        )
        # 高级项折叠，而不是把原始 JSON 当唯一入口；两个区各有稳定锚点，
        # 真实指针才能精确点到对应区的 summary（关闭的 <details> 里子元素
        # 不参与命中测试，点不到里面的按钮）。
        self.assertIn('class="advanced-disclosure" data-role="budget-details-operations"', body)
        self.assertIn('class="advanced-disclosure" data-role="budget-details-covers"', body)

    def test_rerender_keeps_advanced_sections_open(self) -> None:
        """「添加一项」走 outerHTML 重绘：折叠状态必须还原。

        否则用户展开高级区、点「添加一项」，这个区立刻自己合上，刚加的那一行
        看不见也点不到（关闭的 <details> 里子元素不参与命中测试）。"""

        body = self.body()
        self.assertIn('root.querySelectorAll("details.advanced-disclosure")', body)
        self.assertIn("node.open = Boolean(openFlags[index]);", body)

    def test_put_uses_real_revision_and_reads_back(self) -> None:
        body = self.body()
        self.assertIn("revision: draft.revision,", body)
        self.assertIn('method: "PUT"', body)
        save_fn = body[body.index("async function saveGroupBudget("):]
        save_fn = save_fn[:save_fn.index("/** 409 之后")]
        # 回读确认必须走**逐字段**对账：只看 revision 变大就宣布成功，正是
        # 「别的会话写了别的值却报你的保存成功」那个缺陷。
        self.assertIn("const outcome = reconcileBudgetWithServer(groupId, serverDraft);", save_fn)
        self.assertIn("if (outcome.confirmed) {", save_fn)
        self.assertIn("if (outcome.conflict) {", save_fn)
        self.assertIn("await loadGroupCostPanel(groupId);", save_fn)
        # 草稿要留到逐字段核对一致为止 —— 保存路径里没有无条件丢弃草稿。
        self.assertNotIn("forgetBudgetDraft(groupId);", save_fn)

    def test_reconcile_confirms_by_fields_not_revision(self) -> None:
        """确认 = 逐字段比对**提交快照**；revision 变大本身什么都不证明。"""

        body = self.body()
        reconcile = body[
            body.index("function reconcileBudgetWithServer(groupId, serverDraft)"):
            body.index("async function saveGroupBudget(groupId)")
        ]
        # 提交快照与用户草稿分开记（确认比的是快照，不是 revision）
        self.assertIn("const submitted = draft.submitted || null;", reconcile)
        self.assertIn("outcome.submittedRevision = Number(submitted.revision);", reconcile)
        # 回读到的 revision 比提交更旧：什么都没证明，保持待确认、不报成功
        self.assertIn("outcome.ignoredStale = true;", reconcile)
        self.assertIn("if (serverRevision < Number(submitted.revision)) {", reconcile)
        # revision 前进但字段不同 → 冲突，绝不按成功处理
        self.assertIn("const diffs = budgetDiffLines({ values: submitted.values }, serverDraft);", reconcile)
        self.assertIn("你的提交没有被确认：服务器上的现值和你提交的内容不一样", reconcile)
        # 逐字段一致才算确认；revision 前进只说明"现值一致"，不声称读到了历史写入
        self.assertIn("outcome.confirmed = true;", reconcile)
        self.assertIn("outcome.revisionAdvanced = serverRevision !== Number(submitted.revision);", reconcile)
        # 等确认期间用户又改过：旧提交确认，新编辑留着，两者分开说
        self.assertIn("outcome.keptEdits = true;", reconcile)
        self.assertIn("这些新修改**还没有保存**", reconcile)
        # 没有提交快照时：revision 没动 = 用户自己的未保存编辑，不是"别人改过"
        self.assertIn("if (serverRevision === Number(draft.revision)) {", reconcile)

    def test_incomplete_budget_snapshot_is_a_read_failure(self) -> None:
        """半截/错群快照不能画成"全部不限制"：缺键与 null 是两件事。"""

        body = self.body()
        snapshot = body[
            body.index("function budgetLimitValueProblem(value, label)"):
            body.index("function budgetCloneValues(values)")
        ]
        # 必要字段逐个验，且**键必须存在**（null 是合法的"不限制"）
        self.assertIn("Object.prototype.hasOwnProperty.call(budget.limits, key)", snapshot)
        self.assertIn("limits 缺少 ${key}", snapshot)
        self.assertIn("回包里的群不是这个群", snapshot)
        self.assertIn("enabled 不是布尔值", snapshot)
        self.assertIn("currency 不是字符串", snapshot)
        self.assertIn("limits.operation_limits 不是对象", snapshot)
        self.assertIn("Array.isArray(operations)", snapshot, "数组不是对象映射")
        self.assertIn('return "";', snapshot)
        # 类型必须**真判**：`Number(null)`/`Number(false)`/`Number("")` 都是 0，
        # 用 Number() 判会把半坏回包放行成"合法且不限制"
        self.assertIn("typeof budget.revision !== \"number\"", snapshot)
        self.assertIn("!Number.isSafeInteger(budget.revision)", snapshot)
        self.assertIn("budget.revision < 0", snapshot)
        self.assertNotIn("Number.isFinite(Number(budget.revision))", snapshot)
        self.assertNotIn("Number(budget.revision)", snapshot)
        self.assertIn("function budgetLimitValueProblem(value, label)", snapshot)
        self.assertIn("if (value === null) return \"\";", snapshot)
        self.assertIn("typeof value !== \"number\" || !Number.isSafeInteger(value) || value < 0", snapshot)
        self.assertIn("必须是 null 或非负整数", snapshot)
        self.assertIn("limits.${key}", snapshot)
        self.assertIn("limits.operation_limits.${name}", snapshot)
        self.assertIn("for (const [name, value] of Object.entries(operations))", snapshot)
        fetch_fn = body[
            body.index("async function fetchGroupBudget("):
            body.index("function budgetDraftFromServer(")
        ]
        self.assertIn("const problem = budgetSnapshotProblem(groupId, budget);", fetch_fn)
        self.assertIn("服务器返回的预算文档不可信", fetch_fn)
        # 不可信的回包必须按**读失败**处理（草稿/提交快照保留、0 PUT）
        self.assertIn("incomplete: true", fetch_fn)

    def test_pending_submit_is_never_reposted_blindly(self) -> None:
        """有待确认提交时再点保存：先 GET 核对，一律 0 新 PUT，且走同一个判定。"""

        body = self.body()
        save_fn = body[body.index("async function saveGroupBudget("):]
        save_fn = save_fn[:save_fn.index("/** 409 之后")]
        guard = save_fn.index("let pending = budgetDraftFor(groupId)?.submitted || null;")
        put = save_fn.index('method: "PUT"')
        self.assertLess(guard, put, "待确认检查必须在 PUT 之前")
        pending_block = save_fn[guard:put]
        self.assertIn("const fresh = await fetchGroupBudget(groupId);", pending_block)
        # 读失败：明说"没有发出新的写入"
        self.assertIn("没有发出新的写入", pending_block)
        # **并入**统一核验：这一条以前自己算 diff、自己清 submitted，唯一缺的就是
        # "revision 比待确认提交还旧"那一档 —— 同一个乱序 GET，走「重新读取」会被
        # 拒，走「保存」却被当成成功。
        self.assertIn(
            "const outcome = reconcileBudgetWithServer(\n"
            "        groupId,\n"
            "        budgetDraftFromServer(groupId, fresh.budget),\n"
            "      );",
            pending_block,
        )
        self.assertIn("if (outcome.ignoredStale) {", pending_block)
        self.assertIn("比那次写入还旧，不构成确认", pending_block)
        self.assertIn("if (outcome.conflict) {", pending_block)
        self.assertIn("if (!outcome.confirmed) {", pending_block)
        # 现值一致：收口并说明**没有**重复写入
        self.assertIn("没有重复写入。", pending_block)
        # 别处不再自己造冲突（那正是"明明一致却报冲突"的来源）
        self.assertNotIn("setBudgetConflict(groupId, {", pending_block)

    def test_submitted_snapshot_is_a_deep_copy(self) -> None:
        """提交快照必须与可编辑草稿**是两个对象**：只换字段名不算分离。

        父实测 A：保存 token22、ACK revision6、回读 503，随后在表单改 token23，
        再点「重新读取」—— 服务器仍是 22，本应"旧提交已确认、新草稿未保存"，
        实际却报「你的提交没有被确认」。根因是 submitted.values 与 draft.values
        指向同一个对象，输入监听原地改草稿把快照一起改了。"""

        body = self.body()
        clone = body[
            body.index("function budgetCloneValues(values)"):
            body.index("function budgetValuesMatch(")
        ]
        # 顶层字段 + 两个行数组 + 每一行都是新对象
        self.assertIn("operations: cloneRows(values.operations)", clone)
        self.assertIn("covers: cloneRows(values.covers)", clone)
        self.assertIn('map(row => ({ key: String(row?.key ?? ""), value: String(row?.value ?? "") }))', clone)
        save_fn = body[body.index("async function saveGroupBudget("):]
        save_fn = save_fn[:save_fn.index("/** 409 之后")]
        put = save_fn.index('method: "PUT"')
        before_put = save_fn[:put]
        # 快照在**发请求之前**取，且 taken from collected.values 的深副本
        self.assertIn("const sentValues = budgetCloneValues(collected.values);", before_put)
        after_put = save_fn[put:]
        self.assertIn("submitted: {", after_put)
        self.assertIn("values: budgetCloneValues(sentValues),", after_put)
        # 再没有任何地方把同一个对象同时塞进 values 与 submitted
        self.assertNotIn("values: collected.values,", save_fn)
        collect_fn = body[
            body.index("function collectBudgetDraft(groupId)"):
            body.index("async function saveGroupBudget(groupId)")
        ]
        self.assertIn("values: budgetCloneValues(values)", collect_fn)
        # 输入监听不再能改到快照：草稿是副本，写的是 draft.values
        listener = self.source[
            self.source.index('if (target?.dataset?.budgetField) {'):
        ][:600]
        self.assertIn("draft.values[", listener)

    def test_edit_during_ack_keeps_the_edits_and_does_not_claim_them(self) -> None:
        """ACK 期间的新编辑：不许被"提交成功"顺带标成已提交，也不许丢。"""

        body = self.body()
        save_fn = body[body.index("async function saveGroupBudget("):]
        after_put = save_fn[save_fn.index('method: "PUT"'):]
        # 用编辑代数判断 ACK 期间有没有新编辑，而不是无条件 dirty=false
        self.assertIn("const sentEdits = Number(budgetDraftFor(groupId)?.edits || 0);", save_fn)
        self.assertIn("const editsNow = Number(draft.edits || 0);", after_put)
        self.assertIn("const editedDuringAck = editsNow !== sentEdits;", after_put)
        self.assertIn("dirty: editedDuringAck,", after_put)
        self.assertIn("edits: sentEdits,", after_put)
        self.assertNotIn("dirty: false,", after_put)
        # 编辑代数只在**真的改了**时 +1
        self.assertIn("draft.edits = Number(draft.edits || 0) + 1;", body)
        # 确认的是"已提交的那一份"；等待期间的新编辑保持"还没有保存"
        self.assertIn("已提交的内容已回读确认；你之后的新修改还没有保存", after_put)
        self.assertIn("if (outcome.keptEdits) {", after_put)
        reload_fn = body[body.index("async function reloadGroupBudget("):]
        reload_fn = reload_fn[:reload_fn.index("\n  function renderBudgetError")]
        self.assertIn("outcome.keptEdits", reload_fn)
        self.assertIn("你之后的新修改还没有保存", reload_fn)

    def test_conflict_keeps_the_draft_and_the_old_revision(self) -> None:
        body = self.body()
        conflict = body[body.index("async function handleBudgetConflict("):]
        # 409：草稿原样留下，并且**真的**做逐字段对比（原实现只是把草稿删掉，
        # 却承诺"点重新读取对比差异"）。
        self.assertIn("你的草稿已保留", conflict)
        self.assertIn("dirty: true,", conflict)
        self.assertIn("budgetDiffLines(", conflict)
        self.assertIn('data-action="budget-conflict-use-server"', body)
        self.assertIn('data-action="budget-conflict-apply-mine"', body)
        self.assertIn("也没有自动重试", body)
        # 「重新读取」不再删草稿：有待确认提交或有未保存编辑时先把服务器现值与
        # 本地状态**对账**（唯一判定入口），由用户自己选；都没有时才重取面板。
        reload_fn = body[body.index("async function reloadGroupBudget("):]
        reload_fn = reload_fn[:reload_fn.index("\n  function renderBudgetError")]
        self.assertIn("if (!before || (!before.dirty && !before.submitted))", reload_fn)
        self.assertIn("const outcome = reconcileBudgetWithServer(groupId, serverDraft);", reload_fn)
        self.assertNotIn("forgetBudgetDraft(", reload_fn)
        self.assertIn("setBudgetError(", reload_fn)
        # 绝不自动套最新 revision 重试覆盖别人
        self.assertNotIn("expected_revision: 0", body)
        self.assertNotIn("pendingRevision: serverDraft.revision", body)
        # pending 完全匹配时**必须**真确认，不许摆一个"其他会话更新"的假冲突
        self.assertNotIn("setBudgetConflict(groupId, {", reload_fn)

    def test_failure_keeps_the_draft_for_retry(self) -> None:
        body = self.body()
        self.assertIn("草稿已保留，可重试", body)
        self.assertIn("state.budgetSaving", body)

    def test_drafts_are_isolated_per_group(self) -> None:
        body = self.body()
        self.assertIn("state.budgetDrafts.set(String(groupId), draft);", body)
        self.assertIn("state.budgetDrafts.get(String(groupId))", body)
        self.assertIn("state.budgetDrafts.delete(String(groupId));", body)

    def test_typing_does_not_rerender_the_editor(self) -> None:
        """边打字边重绘会抢焦点：kv 增删才重绘。"""

        body = self.body()
        self.assertIn("{ rerender = false, dirty = false } = {}", body)
        # 纯重绘（mutate 是空函数）绝不能把草稿置成 dirty：否则一次刷新就让
        # 自己的编辑变成"未确认"，服务器再也无法安全地更新基线。
        self.assertIn("if (dirty) {", body)
        self.assertIn("draft.dirty = true;", body)
        self.assertIn("{ rerender: true }", self.source)
        listener = self.source[
            self.source.index('if (target?.dataset?.kvKey || target?.dataset?.kvValue)'):
        ][:400]
        self.assertNotIn("{ rerender: true }", listener)

    def test_group_id_is_carried_onto_the_budget_box(self) -> None:
        """多群页面上必须按 groupId 定位，否则 B 群的保存会写进 A 的面板。"""

        self.assertIn('if (budgetBox) budgetBox.dataset.groupId = String(groupId);', self.source)

    def test_touch_targets_and_responsive_kv_rows(self) -> None:
        self.assertIn(".budget-editor .text-input { min-height: 44px; }", self.styles)
        self.assertIn(".kv-row", self.styles)
        self.assertIn("@media (min-width: 768px)", self.styles)
        self.assertIn("@media (max-width: 390px)", self.styles)


class PriceCatalogFrontendTests(unittest.TestCase):
    """超管价格目录编辑器（版本化只增 + 期望版本 CAS）。

    真实后端合同（bot/web/settings_api.py 的 ``put_price_catalog``）：
    ``PUT /api/v1/prices`` 只接受 ``{expected_version, entries}``，
    ``expected_version`` 必须显式且为 ≥1 的整数，与服务端持久最新版本不一致
    时 409 ``price_version_conflict``。所以前端**不能**"拿我手里的 entries 覆盖
    上去"：它必须带上我读到的那个版本，并且把 409 当成"我的草稿基于旧版本"。
    """

    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")
        self.styles = _STYLES.read_text(encoding="utf-8")

    def body(self) -> str:
        return self.source[
            self.source.index("// ---- 模型价格目录（仅超管）"):
            self.source.index("// ---- 按群预算编辑（仅超管）")
        ]

    def test_card_is_super_admin_only_and_never_fetches_for_others(self) -> None:
        models = self.source[self.source.index("function renderModels()"):]
        models = models[: models.index("function renderBot()")]
        self.assertIn('data-role="price-catalog"', models)
        self.assertIn("state.session?.can_manage_global", models)
        self.assertIn("priceCatalogBody()", models)
        # 进入模型页才拉取，且**必须**先过超管守卫（普通管理员既不显示也不请求）
        render = self.source[self.source.index("function renderContent("):]
        hook = render.index('state.activeTab === "models"')
        window = render[hook - 160: hook + 400]
        self.assertIn("state.session?.can_manage_global", window)
        self.assertIn("reloadPriceCatalog({ quiet: true })", window)
        self.assertIn(
            'if (!state.session?.can_manage_global) return { ok: false, reason: "forbidden" };',
            self.body(),
        )

    def test_read_failure_is_unknown_not_an_empty_catalog(self) -> None:
        """读不出来 ≠ 目录是空的：空目录存上去会让所有模型都没有价格。"""

        body = self.body()
        catalog = body[
            body.index("function priceCatalogBody()"):
            body.index("function renderPriceCatalog()")
        ]
        not_loaded = catalog.index("if (!price.loaded) {")
        unknown = catalog.index('data-role="price-unknown"')
        rows = catalog.index('data-role="price-rows"')
        self.assertLess(not_loaded, unknown)
        self.assertLess(unknown, rows, "读失败分支必须在可编辑表格之前 return")
        self.assertIn("读到之前不能保存", catalog)
        self.assertIn("让所有模型都变成没有价格", catalog)
        self.assertNotIn('data-role="price-rows"', catalog[:unknown])

        reload_fn = body[
            body.index("async function reloadPriceCatalog("):
            body.index("function patchPriceDraft(")
        ]
        self.assertIn("price.loaded = false;", reload_fn)
        self.assertIn("price.readError = fresh.error?.message", reload_fn)
        save_fn = body[
            body.index("async function savePriceCatalog()"):
            body.index("async function handlePriceConflict(")
        ]
        # 没读到过就不许写：不能把"未知"当成"空目录"提交上去
        self.assertIn("if (!price.loaded) {", save_fn)
        self.assertIn("不能保存", save_fn)

    def test_half_broken_catalog_is_a_read_failure(self) -> None:
        body = self.body()
        problem = body[
            body.index("function priceCatalogProblem(document)"):
            body.index("async function fetchPriceCatalog()")
        ]
        self.assertIn("!Number.isSafeInteger(catalog.version) || catalog.version < 1", problem)
        self.assertIn("catalog.entries 不是数组", problem)
        self.assertIn('typeof entry[key] !== "string" || !entry[key].trim()', problem)
        self.assertIn("既不是字符串也不是 null", problem)
        fetch = body[
            body.index("async function fetchPriceCatalog()"):
            body.index("function parsePriceText(text)")
        ]
        self.assertIn("const problem = priceCatalogProblem(document);", fetch)
        self.assertIn("服务器返回的价格目录不可信", fetch)

    def test_strict_decimal_parsing_never_truncates(self) -> None:
        body = self.body()
        parse = body[
            body.index("function parsePriceText(text)"):
            body.index("function collectPriceDraft()")
        ]
        self.assertIn('if (raw === "") return { ok: true, value: null };', parse)
        self.assertIn(r"/^\d+(?:\.\d+)?$/", parse)
        self.assertIn("空（未知）", parse)
        # parseInt("1.9") 是 1、parseFloat("1.9abc") 是 1.9 —— 那等于替管理员改价。
        # 注释里点名这两个函数是**解释**为什么不用它们，所以先剥注释再查。
        code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("parseInt", code)
        self.assertNotIn("parseFloat", code)
        # 1.50 与服务器回的 1.5 必须判成同一个值，否则每次保存都造假冲突
        normalize = body[
            body.index("function normalizeDecimalText(text)"):
            body.index("function priceComparableRows(rows)")
        ]
        self.assertIn('const trimmed = fraction.replace(/0+$/, "");', normalize)

    def test_duplicate_and_incomplete_rows_are_rejected(self) -> None:
        body = self.body()
        collect = body[
            body.index("function collectPriceDraft()"):
            body.index("function reconcilePriceWithServer(")
        ]
        self.assertIn("配置槽不能为空", collect)
        self.assertIn("模型不能为空", collect)
        self.assertIn("/^[A-Za-z]{3}$/.test(currency)", collect)
        self.assertIn("if (seen.has(identity))", collect)
        self.assertIn("同一个配置槽 + 模型只能有一行", collect)
        # 整行全空的新行可以忽略（用户刚点"添加一行"）
        self.assertIn("直接忽略", collect)

    def test_save_sends_expected_version_and_never_trusts_200(self) -> None:
        body = self.body()
        save = body[
            body.index("async function savePriceCatalog()"):
            body.index("async function handlePriceConflict(")
        ]
        self.assertIn("const expectedVersion = price.version;", save)
        self.assertIn('method: "PUT"', save)
        self.assertIn(
            "body: JSON.stringify({ expected_version: expectedVersion, entries: collected.entries })",
            save,
        )
        # PUT 200 只是"被接受"：必须回读逐字段核对
        submitted = save.index("price.submitted = {")
        readback = save.rindex("const fresh = await fetchPriceCatalog()")
        self.assertLess(submitted, readback)
        self.assertIn("**无法确认**", save)
        self.assertIn("不要重复保存", save)
        self.assertIn("const outcome = reconcilePriceWithServer(fresh.catalog);", save)

    def test_submitted_snapshot_is_a_deep_copy_of_the_sent_rows(self) -> None:
        body = self.body()
        save = body[
            body.index("async function savePriceCatalog()"):
            body.index("async function handlePriceConflict(")
        ]
        self.assertIn("const sentRows = priceCloneRows(collected.rows);", save)
        self.assertIn("const sentEdits = Number(price.edits || 0);", save)
        self.assertIn("rows: priceCloneRows(sentRows),", save)
        self.assertIn("edits: sentEdits,", save)
        self.assertNotIn("rows: collected.rows,", save)
        clone = body[
            body.index("function priceCloneRows(rows)"):
            body.index("function priceRowFromEntry(entry)")
        ]
        for field in (
            "provider_key",
            "model",
            "currency",
            "input",
            "output",
            "cache_read",
            "cache_write",
        ):
            self.assertIn(field, clone, field)
        self.assertIn("String(row?.", clone)

    def test_edit_during_ack_is_kept_and_never_claimed(self) -> None:
        body = self.body()
        reconcile = body[
            body.index("function reconcilePriceWithServer(catalog)"):
            body.index("async function savePriceCatalog()")
        ]
        self.assertIn("if (Number(price.edits || 0) !== Number(submitted.edits || 0)) {", reconcile)
        self.assertIn("这些新修改还没有保存", reconcile)
        self.assertIn("outcome.keptEdits = true;", reconcile)
        self.assertIn("price.dirty = true;", reconcile)
        # 边打字边存草稿，但**不重绘**（重绘会抢焦点）
        listener = self.source[self.source.index("if (target?.dataset?.priceField)"):]
        listener = listener[: listener.index("if (target?.dataset?.budgetField)")]
        self.assertIn("price.edits = Number(price.edits || 0) + 1;", listener)
        self.assertIn("price.dirty = true;", listener)
        self.assertIn("String(target.value)", listener)
        self.assertNotIn("renderPriceCatalog()", listener)

    def test_reconcile_is_the_only_confirmation_and_rejects_stale_reads(self) -> None:
        body = self.body()
        reconcile = body[
            body.index("function reconcilePriceWithServer(catalog)"):
            body.index("async function savePriceCatalog()")
        ]
        self.assertIn("if (serverVersion < Number(submitted.version)) {", reconcile)
        self.assertIn("outcome.ignoredStale = true;", reconcile)
        self.assertIn("if (!priceRowsEqual(submitted.rows, serverRows)) {", reconcile)
        self.assertIn("你的提交没有被确认：服务器上的价格目录和你提交的内容不一样", reconcile)
        self.assertIn("并不证明你那次写入生效", reconcile)
        self.assertIn("outcome.confirmed = true;", reconcile)
        # 读回路径（保存后回读 / 重新读取 / 待确认预检）都走同一个判定
        self.assertGreaterEqual(body.count("reconcilePriceWithServer("), 4, body[:200])
        save = body[
            body.index("async function savePriceCatalog()"):
            body.index("async function handlePriceConflict(")
        ]
        self.assertIn("const outcome = reconcilePriceWithServer(fresh.catalog);", save)

    def test_conflict_keeps_the_draft_and_never_rebases_automatically(self) -> None:
        body = self.body()
        conflict = body[
            body.index("async function handlePriceConflict("):
            body.index("async function reloadPriceCatalog(")
        ]
        self.assertIn("price.submitted = null;", conflict)
        self.assertIn("你的草稿已保留", conflict)
        self.assertIn("const fresh = await fetchPriceCatalog()", conflict)
        self.assertIn("你的草稿没有被覆盖，也没有自动重试。", self.body())
        actions = self.source[
            self.source.index('if (action === "price-conflict-use-server")'):
            self.source.index('if (action === "cost-reload")')
        ]
        self.assertIn('if (action === "price-conflict-apply-mine")', actions)
        self.assertIn("price.version = Number(conflict.serverVersion);", actions)
        self.assertIn("不会自动写入", actions)
        # 重基**不**自动提交：用户还得自己点保存
        self.assertNotIn("await savePriceCatalog()", actions)
        apply_block = actions[actions.index('if (action === "price-conflict-apply-mine")'):]
        self.assertNotIn("apiFetch", apply_block)

    def test_rebase_is_a_three_way_merge_not_a_wholesale_overwrite(self) -> None:
        """点「重基」只把我在本轮改过的字段顶上去；别人改过、我没碰的字段必须留下。

        否则"保留我的草稿再保存"会把服务器上别人刚改的值按我记忆里的旧值写回去 ——
        真实浏览器里就是这么把别人改过的输出价打回旧值的（不是重基，是整份覆盖）。
        """

        body = self.body()
        self.assertIn(
            'const PRICE_MERGE_FIELDS = ["provider_key", "model", "currency", ...PRICE_LIMIT_FIELDS];',
            body,
        )
        merge = body[
            body.index("function mergePriceRows(baseRows, mineRows, serverRows)"):
            body.index("function priceRowFromEntry(entry)")
        ]
        # 只有"我这一轮真的改过"的字段才顶掉服务器值
        self.assertIn('const changedByMe = String(myRow[field] ?? "") !== String(baseRow[field] ?? "");', merge)
        self.assertIn('row[field] = String((changedByMe ? myRow[field] : serverRow[field]) ?? "");', merge)
        # 别人新增的行保留服务器值；我新增的行保留我的；服务器删掉的以服务器为准
        self.assertIn("if (!myRow) {", merge)
        self.assertIn("if (base.has(key)) {", merge)
        self.assertIn("keptDeletions.push(priceRowLabel(base.get(key)));", merge)
        self.assertIn("const serverKeys = new Set(server.map(row => priceRowIdentity(row)));", merge)
        self.assertIn("serverDeletions.push(priceRowLabel(myRow));", merge)
        self.assertIn("rows.push(priceCloneRows([myRow])[0]);", merge)
        self.assertIn("return { rows, keptDeletions, serverDeletions };", merge)
        self.assertIn("function priceRowIdentity(row)", body)
        self.assertIn("function priceRowLabel(row)", body)
        # 行的增删也要看基线：我本地删掉的不能被静默复活（本轮实测的缺陷就是它），
        # 服务器删掉的原有行也不能被复活 —— 两者都必须用 base 区分
        self.assertIn("// 基线里有、我草稿里没有 = 我本地删掉了它：保住我的删除", merge)
        self.assertIn("// 服务器删掉了这条原有行（我这边没删）：以服务器为准，不复活", merge)
        # 基线必须来自**真正采用过的**服务器值，不许凭空造
        self.assertIn("if (!Array.isArray(price.baseRows)) price.baseRows = [];", body)
        self.assertIn("price.baseRows = priceCloneRows(serverRows);", body)
        # ACK 期间又改过、确认之后：基线必须推进到本次已确认的服务器值，
        # 否则"改回旧基线值"的那种编辑会在下一次重基时被服务器值顶掉
        reconcile = body[
            body.index("function reconcilePriceWithServer(catalog)"):
            body.index("async function savePriceCatalog()")
        ]
        kept = reconcile[reconcile.index("if (Number(price.edits || 0) !== Number(submitted.edits || 0)) {"):]
        self.assertIn("price.baseRows = priceCloneRows(serverRows);", kept)
        self.assertIn("price.dirty = true;", kept)
        actions = self.source[
            self.source.index('if (action === "price-conflict-use-server")'):
            self.source.index('if (action === "cost-reload")')
        ]
        apply_block = actions[actions.index('if (action === "price-conflict-apply-mine")'):]
        self.assertIn("const baseRows = priceCloneRows(price.baseRows);", apply_block)
        self.assertIn("const merge = mergePriceRows(baseRows, mineRows, conflict.serverRows);", apply_block)
        self.assertIn("price.rows = merge.rows;", apply_block)
        self.assertIn("price.baseRows = priceCloneRows(conflict.serverRows);", apply_block)
        self.assertIn("只把你在本轮改过的字段顶上去", apply_block)
        self.assertIn("你在本地删掉的", apply_block)
        self.assertIn("服务器已经删掉的", apply_block)
        # 用户显式选择「用服务器值」时，基线也要跟着走
        use_server = actions[
            actions.index('if (action === "price-conflict-use-server")'):
            actions.index('if (action === "price-conflict-apply-mine")')
        ]
        self.assertIn("price.baseRows = priceCloneRows(conflict.serverRows);", use_server)

    def test_touch_targets_aria_and_responsive_rows(self) -> None:
        body = self.body()
        self.assertIn('aria-live="polite"', body)
        self.assertIn('data-role="price-error"', body)
        self.assertIn('role="alert"', body)
        self.assertIn("aria-label=", body)
        self.assertIn(".price-row-grid", self.styles)
        self.assertIn(".price-field .text-input { min-height: 44px; }", self.styles)
        self.assertIn(".price-row-head .secondary-button { min-height: 44px; }", self.styles)
        self.assertIn("[data-role=\"price-error\"]", self.styles)
        self.assertIn("@media (min-width: 1280px)", self.styles)


class ToastLayerNeverSwallowsClicksTests(unittest.TestCase):
    """提示条是纯通知，**不能吃点击**。

    真实浏览器证据（tools/settings-ui-harness/checks/real-api-budget.mjs）：
    390px 宽、真实后端 + 真实库上，保存成功会弹一条 3.6s 的 toast；它固定在
    右下角，恰好压在预算表单的「保存」/「重新读取」上。``.toast`` 上一旦是
    ``pointer-events: auto``，紧接着的第二次保存点击就落在 toast 上 ——
    不发请求、没有任何错误、界面毫无反馈（当时表现为"点了保存什么都没发生"，
    25s 后 toast 自己消失，事后再做命中测试反而显示"按钮是好的"）。
    全仓没有任何 toast 里放可点内容，所以这一层必须整体不拦截指针。
    """

    def setUp(self) -> None:
        self.styles = _STYLES.read_text(encoding="utf-8")

    def _rule(self, selector: str) -> str:
        start = self.styles.index(f"{selector} {{")
        return self.styles[start:self.styles.index("}", start)]

    def test_toast_region_does_not_capture_pointer(self) -> None:
        rule = self._rule(".toast-region")
        self.assertIn("pointer-events: none;", rule)

    def test_toast_itself_does_not_capture_pointer(self) -> None:
        rule = self._rule(".toast")
        self.assertIn("pointer-events: none;", rule)
        self.assertNotIn("pointer-events: auto;", rule)

    def test_no_toast_rule_re_enables_pointer_events(self) -> None:
        # 任何 ``.toast…`` 派生规则（例如 ``.toast.warning``）也不许再把指针夺回来。
        # 先剥掉注释：上面那段解释性注释里本来就会写到 ``pointer-events: auto``。
        css = re.sub(r"/\*.*?\*/", "", self.styles, flags=re.S)
        offenders = []
        for chunk in css.split("}"):
            if "{" not in chunk:
                continue
            selector, body = chunk.split("{", 1)
            selector = selector.rsplit("\n", 1)[-1].strip()
            if selector.startswith(".toast") and "pointer-events: auto" in body:
                offenders.append(selector)
        self.assertEqual([], offenders, f"这些提示条规则重新拦截了指针：{offenders}")


class QueueFetchOrderingTests(unittest.TestCase):
    """申请人路径**不得**触碰全局队列取数。"""

    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_applicant_boot_does_not_fetch_the_queue(self) -> None:
        boot_start = self.source.index(
            "sessionResult = { session: await loadOnboardingSession() };"
        )
        boot = self.source[boot_start:boot_start + 1200]
        applicant = boot.split("if (state.applicantOnly) {", 1)[1].split("}", 1)[0]
        self.assertIn("await loadOnboardingRequests(state.session);", applicant)
        self.assertNotIn("loadOnboardingQueue", applicant)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
