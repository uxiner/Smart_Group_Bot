"""真实 DOM 用例：群组设置里「运营任务（只读）」面板的失败 / 忙碌 / 恢复 / 迟到响应行为。

这些用例把 ``bot/web/static/app.js`` 里**真实的**代码（常量块、面板渲染函数、
``loadGroupOperations``、展开监听器、点击分支）逐字切片，装进一个 loopback HTML 页面，
在真实 Blink 内核（Chromium headless）里驱动，断言真实 DOM 与真实请求计数 —— 不是字符串匹配。

父代理独立验收 R1 复现的机制是：``renderContent()`` 重写 ``innerHTML`` 后恢复 ``<details open>``
（真实 Blink 会因此派发 ``toggle``）→ 监听器再次自动加载 → 失败后 ``loaded=false`` 又触发下一次，
1 秒内 38 个请求。本文件的场景一就是这条环路：只有「每个群首次展开自动读一次」才允许通过。

切片锚点若在源文件里找不到会立即抛 AssertionError（防止切片漂移后测试静默失效）。
真实浏览器是硬性前提：找不到可执行文件时用例**失败**（不是 skip），因为静默跳过等于没验证。
可以通过环境变量 ``OPERATIONS_BROWSER`` 指定 Chromium/Chrome 可执行文件。
"""

from __future__ import annotations

import glob
import html as html_module
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


def _slice(source: str, start_marker: str, end_marker: str, label: str) -> str:
    if start_marker not in source:
        raise AssertionError(f"app.js 切片锚点缺失（{label}）：{start_marker!r}")
    tail = source.split(start_marker, 1)[1]
    if end_marker not in tail:
        raise AssertionError(f"app.js 切片结束锚点缺失（{label}）：{end_marker!r}")
    return start_marker + tail.split(end_marker, 1)[0]


def _code_slices() -> dict[str, str]:
    source = _APP_JS.read_text(encoding="utf-8")
    slices = {
        "constants": _slice(
            source, "  const OPERATIONS_SECTION_KEY", "  const ROLE_META", "operations 常量"
        ),
        "panel": _slice(
            source,
            "  function operationsEntry(",
            "  function resourceOperationKey(",
            "面板渲染函数",
        ),
        "loader": _slice(
            source,
            "  async function loadGroupOperations(",
            "  async function loadAccess(",
            "只读加载器",
        ),
        "toggle": _slice(
            source,
            "  // 「运营任务」分节默认折叠",
            "  content.addEventListener(\"click\", async event => {",
            "展开监听器",
        ),
        "click": _slice(
            source,
            "    if (action === \"load-group-operations\") {",
            "    if (action === \"load-access\") {",
            "显式刷新点击分支",
        ),
    }
    for label, code in slices.items():
        if not code.strip():
            raise AssertionError(f"app.js 切片为空：{label}")
    # 切片完整性：这些标记必须落在切片里，否则说明源文件结构变了。
    required = {
        "constants": ["OPERATIONS_REASON_LABEL", "OPERATIONS_COUNT_MEANING_LABEL"],
        "panel": ["renderOperationsStatusPanel", "operationsTaskRow", "operationsItemLabel"],
        "loader": ["apiFetch(`/api/v1/groups/", "attempted: true"],
        "toggle": ["loadGroupOperations(groupId)"],
        "click": ["await loadGroupOperations(button.dataset.groupId)"],
    }
    for label, markers in required.items():
        for marker in markers:
            if marker not in slices[label]:
                raise AssertionError(f"app.js 切片 {label} 缺少必需标记：{marker!r}")
    return slices


def _find_browser() -> str | None:
    candidates: list[str] = []
    configured = os.environ.get("OPERATIONS_BROWSER", "").strip()
    if configured:
        candidates.append(configured)
    home = pathlib.Path(os.path.expanduser("~"))
    candidates.extend(
        sorted(
            glob.glob(
                str(
                    home
                    / "Library/Caches/ms-playwright/*/chrome-headless-shell-*/chrome-headless-shell"
                )
            ),
            reverse=True,
        )
    )
    candidates.extend(
        sorted(
            glob.glob(
                str(
                    home
                    / "Library/Caches/ms-playwright/*/chrome-mac*/*.app/Contents/MacOS/*"
                )
            ),
            reverse=True,
        )
    )
    candidates.extend(
        [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/snap/bin/chromium",
            "/opt/google/chrome/chrome",
        ]
    )
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).exists():
            return candidate
    return None


_HARNESS_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>operations panel harness</title></head>
<body>
<div id="content"></div>
<pre id="results">pending</pre>
<script>
"use strict";
const results = { scenarios: [], checks: [], info: {}, errors: [] };
const checks = [];
function check(name, ok, actual) {
  checks.push({ name: name, ok: Boolean(ok), actual: actual === undefined ? null : String(actual) });
}

// ---------------------------------------------------------------- harness 替身
const content = document.getElementById("content");
const GROUP_A = { id: -901, title: "A 群" };
const GROUP_B = { id: -902, title: "B 群" };
const groups = [GROUP_A, GROUP_B];
let openState = new Map();
let behaviors = new Map();
let fetchLog = [];
let held = [];
let renderCount = 0;
let draftTouched = false;
let state = {
  groupOperations: new Map(),
  groupOperationsLoads: new Map(),
  resourceFormDrafts: new Map(),
  // B13b 在面板切片里新增的真实状态映射：假 state 必须与 app.js 的真实形状一致，
  // 否则 renderOperationsStatusPanel -> renderPolicyPanel 会读到 undefined。
  groupPolicy: new Map(),
  groupPolicyLoads: new Map(),
  groupOperationsWork: new Map(),
  groupOperationsPoll: new Map(),
};

function draftEntries() {
  return JSON.stringify([...state.resourceFormDrafts.entries()]);
}

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function attr(value) { return escapeHtml(value); }
function icon(name) { return `<svg data-icon="${name}"></svg>`; }

function taskBody(group, marker) {
  const stamp = group.id === -901 ? "09:01" : "09:02";
  return { task_status: {
    generated_at: `2026-01-05 ${stamp}:00`,
    timezone: "Asia/Shanghai",
    group_id: group.id,
    marker: marker || "",
    tasks: [
      { task_id: "scheduled_messages", title: "定时消息", trigger: "wall_clock",
        source: { implemented: "in_process", runner_task_name: "scheduled-message-runner",
                  external_schedule_known: false, external_schedule_reason: "external_schedule_unknown" },
        next_run: { reason: "planned_by_scheduler", at: "2026-01-06 00:00:00" },
        last_success: { at: "2026-01-05 00:00:03", basis: "scheduled_messages.last_message_id", reason: null },
        last_failure: { reason: "retry_scheduled_backoff_not_completed", at: null, count: 3,
                        count_meaning: "scheduled_messages_pending_retry_occurrences",
                        backoff_until: null, pending_retry_at: "2026-01-05 09:05:00",
                        kind: "delivery_attempt_failed", attempts_max: 2, history_complete: false },
        details: { earliest_planned_at: "2026-01-07 08:00:00", due_now_pending_count: 2,
                   due_now_pending_count_meaning: "configurations_past_their_execution_time" },
        unknowns: [] },
      { task_id: "group_summary", title: "群摘要", trigger: "event_driven",
        source: { implemented: "in_process", runner_task_name: "group-summary-scheduler",
                  external_schedule_known: false, external_schedule_reason: "external_schedule_unknown" },
        next_run: { reason: "event_driven", at: null },
        last_success: { at: null, basis: null, reason: "not_recorded" },
        last_failure: { reason: "current_backoff_state_not_full_history", at: "2026-01-05 08:40:00",
                        count: 2, count_meaning: "group_summary_cumulative_failures",
                        backoff_until: "2026-01-05 09:30:00", pending_retry_at: null,
                        kind: null, attempts_max: null, history_complete: false },
        details: {}, unknowns: [] },
      { task_id: "weekly_report", title: "周报", trigger: "external_cron",
        source: { implemented: "external_cron_only", runner_task_name: null,
                  external_schedule_known: false, external_schedule_reason: "external_schedule_unknown" },
        next_run: { reason: "external_schedule_unknown", at: null },
        last_success: { at: null, basis: null, reason: "not_recorded" },
        last_failure: { reason: "not_recorded", at: null, count: null, count_meaning: null,
                        backoff_until: null, pending_retry_at: null, kind: null,
                        attempts_max: null, history_complete: false },
        details: {}, unknowns: [] },
    ],
  } };
}

async function apiFetch(url) {
  const match = /groups\\/(-?\\d+)\\/operations\\/task-status/.exec(url);
  const key = match ? match[1] : url;
  const behavior = behaviors.get(key) || { mode: "ok", body: taskBody(key === "-901" ? GROUP_A : GROUP_B) };
  fetchLog.push({ key: key, mode: behavior.mode });
  if (behavior.mode === "hold") {
    return new Promise((resolve, reject) => {
      held.push({ key: key, behavior: behavior, resolve: resolve, reject: reject });
    });
  }
  if (behavior.mode === "ok") return behavior.body;
  throw new Error(behavior.message || ("HTTP " + (behavior.status || 500)));
}

function releaseHeld(key, mode, message) {
  const index = held.findIndex(item => item.key === key);
  if (index < 0) return false;
  const item = held.splice(index, 1)[0];
  if (mode === "ok") item.resolve(item.behavior.body); else item.reject(new Error(message || "HTTP 503"));
  return true;
}

// 忠实到接口层面的 renderContent 替身：重写 innerHTML，然后像真实 restoreGroupDisclosureStates
// 那样把 <details> 的 open 重新赋一次值 —— 真实 Blink 会因此派发 toggle（R1 环路的来源）。
function renderContent() {
  renderCount += 1;
  const before = draftEntries();
  content.innerHTML = groups.map(group => {
    const key = String(group.id);
    const open = Boolean(openState.get(key));
    return `<div data-group-card data-group-id="${key}">
      <details class="group-section" data-group-id="${key}" data-group-section="${OPERATIONS_SECTION_KEY}"${open ? " open" : ""}>
        <summary>运营任务（只读）</summary>
        ${renderOperationsStatusPanel(group)}
      </details>
    </div>`;
  }).join("");
  content.querySelectorAll("details[data-group-section]").forEach(section => {
    const key = String(section.dataset.groupId);
    section.open = Boolean(openState.get(key));
  });
  if (draftEntries() !== before) draftTouched = true;
}

function sectionFor(group) {
  return content.querySelector(`details[data-group-id="${group.id}"]`);
}
function panelText(group) {
  const section = sectionFor(group);
  return section ? section.textContent : "";
}
function taskRowText(group, taskId) {
  const section = sectionFor(group);
  const row = section ? section.querySelector(`[data-operations-task="${taskId}"]`) : null;
  return row ? row.textContent : "";
}
function refreshButton(group) {
  const section = sectionFor(group);
  return section ? section.querySelector('[data-action="load-group-operations"]') : null;
}
function openSection(group, open) {
  openState.set(String(group.id), open);
  const section = sectionFor(group);
  if (section) section.open = open;
}
function clickRefresh(group) {
  const button = refreshButton(group);
  if (button && !button.disabled) button.click();
  return Boolean(button);
}
async function settle(times) {
  const rounds = times || 8;
  for (let i = 0; i < rounds; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 0));
  }
}
function resetHarness() {
  state.groupOperations = new Map();
  state.groupOperationsLoads = new Map();
  state.resourceFormDrafts = new Map([["-901:scheduled:7", { text: "草稿未保存内容" }]]);
  openState = new Map();
  behaviors = new Map();
  fetchLog = [];
  held = [];
  renderCount = 0;
}

// ------------------------------------------------- 被测代码（逐字切片自 app.js）
/*__SLICE_CONSTANTS__*/
/*__SLICE_PANEL__*/
/*__SLICE_LOADER__*/
/*__SLICE_TOGGLE__*/
content.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
/*__SLICE_CLICK__*/
});

// ------------------------------------------------------------------ 场景驱动
async function scenarioFirstFailureDoesNotRetry() {
  resetHarness();
  behaviors.set("-901", { mode: "fail", status: 403, message: "HTTP 403" });
  renderContent();
  openSection(GROUP_A, true);
  await settle();
  const afterFirst = fetchLog.length;
  const text = panelText(GROUP_A);
  renderContent();
  renderContent();
  await settle();
  const afterNaturalRenders = fetchLog.length;
  openSection(GROUP_A, false);
  await settle();
  openSection(GROUP_A, true);
  await settle();
  const afterReopen = fetchLog.length;
  openSection(GROUP_A, false);
  renderContent();
  await settle();
  openSection(GROUP_A, true);
  renderContent();
  await settle();
  const afterTabRoundTrip = fetchLog.length;
  check("首次展开只发一次请求", afterFirst === 1, afterFirst);
  check("自然重渲染不再发请求", afterNaturalRenders === 1, afterNaturalRenders);
  check("关闭再打开不再发请求", afterReopen === 1, afterReopen);
  check("切页往返不再发请求", afterTabRoundTrip === 1, afterTabRoundTrip);
  check("失败后显示中文错误", text.indexOf("HTTP 403") >= 0, text.slice(0, 200));
  check("失败后显示显式重试按钮", text.indexOf("重试读取") >= 0, text.slice(0, 200));
  check("失败后说明不会自动重试", text.indexOf("首次读取失败后不会自动重试") >= 0, text.slice(0, 200));
  const button = refreshButton(GROUP_A);
  check("失败后按钮不再忙碌", Boolean(button) && button.disabled === false && button.getAttribute("aria-busy") === "false",
        button ? button.outerHTML : null);
  results.info.firstFailureFetchCount = afterTabRoundTrip;
  results.info.renderCountAfterFirstFailure = renderCount;
}

async function scenarioExplicitRetryRecovers() {
  behaviors.set("-901", { mode: "ok", body: taskBody(GROUP_A, "retry") });
  clickRefresh(GROUP_A);
  await settle(12);
  const text = panelText(GROUP_A);
  check("显式重试恰好再发一次请求", fetchLog.length === 2, fetchLog.length);
  check("恢复后渲染出任务行", text.indexOf("定时消息") >= 0, text.slice(0, 200));
  check("恢复后加载态被清掉", text.indexOf("加载失败") < 0, text.slice(0, 200));
  check("待重试写成「已安排、尚未重试」", text.indexOf("已安排 2026-01-05 09:05") >= 0 && text.indexOf("尚未重试") >= 0, text);
  check("待重试数量带单位", text.indexOf("待重试记录 3 条") >= 0, text);
  check("尝试次数单独展示", text.indexOf("最多已尝试 2 次") >= 0, text);
  check("外部计划显示未接入", text.indexOf("外部计划未接入") >= 0, text);
  // R4-2：计划到期与未来计划是两个维度，各自独立可见。
  check("到期待处理独立显示", text.indexOf("到期待处理") >= 0 && text.indexOf("2 条配置已到执行时刻") >= 0, text);
  check("未来计划仍独立可见", text.indexOf("后续计划") >= 0 && text.indexOf("2026-01-07 08:00") >= 0, text);
  // R4-1：摘要的 count 是累计失败次数，绝不能被改写成「待重试」。
  const summaryRow = taskRowText(GROUP_A, "group_summary");
  check("摘要失败次数带自己的单位", summaryRow.indexOf("累计失败次数 2") >= 0, summaryRow);
  check("摘要不虚构待重试", summaryRow.indexOf("待重试") < 0, summaryRow);
  check("摘要不虚构尝试次数", summaryRow.indexOf("最多已尝试") < 0, summaryRow);
  check("摘要不虚构已安排重试", summaryRow.indexOf("已安排") < 0, summaryRow);
}

async function scenarioBusySemantics() {
  resetHarness();
  behaviors.set("-901", { mode: "ok", body: taskBody(GROUP_A) });
  renderContent();
  openSection(GROUP_A, true);
  await settle(12);
  check("首次加载完成并渲染", panelText(GROUP_A).indexOf("定时消息") >= 0, panelText(GROUP_A).slice(0, 120));
  const loadedFetches = fetchLog.length;
  behaviors.set("-901", { mode: "hold", body: taskBody(GROUP_A, "hold") });
  clickRefresh(GROUP_A);
  await settle(4);
  const button = refreshButton(GROUP_A);
  const busyText = panelText(GROUP_A);
  check("刷新悬挂时按钮禁用", Boolean(button) && button.disabled === true, button ? button.outerHTML : null);
  check("刷新悬挂时 aria-busy=true", Boolean(button) && button.getAttribute("aria-busy") === "true",
        button ? button.getAttribute("aria-busy") : null);
  check("刷新悬挂时显示加载中", busyText.indexOf("加载中") >= 0, busyText.slice(0, 200));
  const inFlightFetches = fetchLog.length;
  clickRefresh(GROUP_A);
  await settle(4);
  check("悬挂期间连点不再发请求", fetchLog.length === inFlightFetches, fetchLog.length);
  loadGroupOperations("-901");
  loadGroupOperations("-901");
  await settle(4);
  check("同群重复调用被 in-flight 守卫拦下", fetchLog.length === inFlightFetches, fetchLog.length);
  check("悬挂期间旧数据仍在（未清空）", panelText(GROUP_A).indexOf("2026-01-05 09:01") >= 0, panelText(GROUP_A).slice(0, 200));
  releaseHeld("-901", "ok");
  await settle(12);
  const done = refreshButton(GROUP_A);
  check("完成后按钮恢复可用", Boolean(done) && done.disabled === false, done ? done.outerHTML : null);
  check("完成后 aria-busy=false", Boolean(done) && done.getAttribute("aria-busy") === "false",
        done ? done.getAttribute("aria-busy") : null);
  check("完成后按钮文案回到刷新", Boolean(done) && done.textContent.indexOf("刷新运营任务状态") >= 0,
        done ? done.textContent : null);
  check("刷新是 in-flight 而不是新的一轮加载", loadedFetches === 1 && inFlightFetches === 2,
        `${loadedFetches}/${inFlightFetches}`);
}

async function scenarioLateResponseStaysInItsOwnGroup() {
  resetHarness();
  behaviors.set("-901", { mode: "hold", body: taskBody(GROUP_A) });
  behaviors.set("-902", { mode: "hold", body: taskBody(GROUP_B) });
  renderContent();
  openSection(GROUP_A, true);
  openSection(GROUP_B, true);
  await settle(6);
  check("两个群各发一次请求", fetchLog.length === 2, fetchLog.length);
  releaseHeld("-902", "ok");
  await settle(8);
  const textBFirst = panelText(GROUP_B);
  releaseHeld("-901", "ok");
  await settle(10);
  const textA = panelText(GROUP_A);
  const textBAfter = panelText(GROUP_B);
  check("B 群卡片显示 B 群读取时刻", textBFirst.indexOf("2026-01-05 09:02") >= 0, textBFirst.slice(0, 200));
  check("B 群卡片始终不含 A 群读取时刻", textBAfter.indexOf("2026-01-05 09:01") < 0, textBAfter.slice(0, 200));
  check("A 群迟到响应只落到 A 群卡片", textA.indexOf("2026-01-05 09:01") >= 0 && textA.indexOf("2026-01-05 09:02") < 0,
        textA.slice(0, 200));
  check("迟到响应没有触发额外请求", fetchLog.length === 2, fetchLog.length);
}

async function main() {
  try {
    await scenarioFirstFailureDoesNotRetry();
    await scenarioExplicitRetryRecovers();
    await scenarioBusySemantics();
    await scenarioLateResponseStaysInItsOwnGroup();
    check("运营面板刷新不触碰未保存草稿状态",
          draftTouched === false && state.resourceFormDrafts.get("-901:scheduled:7").text === "草稿未保存内容",
          JSON.stringify([...state.resourceFormDrafts.entries()]));
    results.info.totalRenders = renderCount;
  } catch (error) {
    results.errors.push(String(error && error.stack ? error.stack : error));
  }
  results.checks = checks;
  document.getElementById("results").textContent = JSON.stringify(results);
}

main();
</script>
</body>
</html>
"""


class OperationsPanelBrowserTests(unittest.TestCase):
    """在真实 Blink 里驱动 app.js 的运营面板切片（真实 DOM + 真实请求计数）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = _find_browser()
        cls.slices = _code_slices()

    def _build_page(self, directory: pathlib.Path) -> pathlib.Path:
        page = _HARNESS_TEMPLATE
        for label, code in self.slices.items():
            token = f"/*__SLICE_{label.upper()}__*/"
            self.assertIn(token, page)
            page = page.replace(token, code)
        path = directory / "operations-panel-harness.html"
        path.write_text(page, encoding="utf-8")
        return path

    def test_operations_panel_never_retries_implicitly_in_a_real_browser(self) -> None:
        if not self.browser:
            self.fail(
                "真实 DOM 用例需要一个 Chromium/Chrome 可执行文件：请设置 OPERATIONS_BROWSER=<path> "
                "或安装 chromium（本用例刻意不 skip，静默跳过等于没验证）。"
            )
        with tempfile.TemporaryDirectory(prefix="operations-panel-browser-") as raw_dir:
            directory = pathlib.Path(raw_dir)
            page = self._build_page(directory)
            env = dict(os.environ)
            env["HOME"] = str(directory)
            completed = subprocess.run(
                [
                    self.browser,
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-crash-reporter",
                    f"--user-data-dir={directory / 'profile'}",
                    "--virtual-time-budget=20000",
                    "--dump-dom",
                    page.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"浏览器退出码 {completed.returncode}\nstderr:\n{completed.stderr[-2000:]}",
            )
            match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.S)
            self.assertIsNotNone(match, "harness 没有写出 #results（页面脚本可能抛错了）")
            raw = html_module.unescape(match.group(1))
            self.assertNotEqual(raw.strip(), "pending", "harness 没有跑完 main()")
            payload = json.loads(raw)

        self.assertEqual(payload["errors"], [], f"harness 抛错：{payload['errors']}")
        failed = [item for item in payload["checks"] if not item["ok"]]
        self.assertTrue(
            payload["checks"],
            "harness 没有产出任何断言",
        )
        summary = "\n".join(
            f"- {item['name']}: {'OK' if item['ok'] else 'FAIL'} (actual={item['actual']})"
            for item in payload["checks"]
        )
        self.assertEqual(failed, [], f"真实浏览器断言失败：\n{summary}")
        # 环路回归的量化证据：失败场景下请求数必须停在 1。
        self.assertEqual(payload["info"]["firstFailureFetchCount"], 1, summary)


if __name__ == "__main__":
    unittest.main()
