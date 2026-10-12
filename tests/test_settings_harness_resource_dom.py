"""真实 DOM 用例：群管理里 **keyword-replies / scheduled-messages** 两个复数资源的渲染与保存。

静态字符串测试只能证明源码里出现了某些字符；它证明不了：

* 前端真的按**复数集合 URL**（``/keyword-replies``、``/scheduled-messages``）发 GET ——
  这正是「集合 GET 掉进单条分支」那次预览工具事故的对立面；
* 两个复数资源的数据真的变成 DOM 里的行（关键词行、定时消息行、卡片计数、分区小计）；
* 点击删除按钮**只排队**、一个请求都不发；
* 「保存全部」只为这一行发**恰好一个** DELETE，且 URL 精确；
* 2xx 但字段缺失 / 互相矛盾时**不当成删除成功**（保留草稿与待删除项）；
* ``no_change`` 不当成功计数，只按「无需改动」警告；
* 断网 / 未确认时草稿与待删除项都保留；
* 无关群的草稿、基线与资源在全程保持原样，且没有任何请求打到无关群。

本模块把 ``bot/web/static/app.js`` 里的真实实现逐字切片（``RESOURCE_TYPE_META`` /
``loadGroupResources`` / ``renderAutomationResources`` / ``renderKeywordReplyRow`` /
``renderScheduledMessageRow`` / ``memoryDeleteOutcome`` / ``applySavedResourceOperation`` /
``persistResourceOperation`` / ``saveAllChanges`` / ``collectDeferredResourceOperations`` /
``queueDeferredResourceDelete`` / 点击分支 / 自动化分区的调用点），在真实 Blink 内核
（Chrome headless shell）里用**真实 DOM**驱动，断言真实请求日志、真实 toast 文本与
harness 自己那份 ``state``。

切片锚点缺失 → 立即 ``AssertionError``（防止源码漂移后测试静默失效）。断言里用到的
toast 文案不手抄，全部用正则从 ``app.js`` 里就地提取后注入页面。找不到可执行文件时
``self.fail`` 而不是 ``skip``：静默跳过等于没验证。可用 ``SETTINGS_HARNESS_BROWSER``
指定 Chromium/Chrome。
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

WIDTHS = (320, 390, 1280)

GROUP_A = "-1001234567890"
GROUP_B = "-1009999999999"

# 探针数据由题目给定：两个复数资源各一行。
KEYWORD_ROW = {
    "id": 20,
    "keyword": "菜单",
    "match_type": "exact",
    "reply_text": "这里是我们群的功能菜单。",
    "enabled": True,
}
SCHEDULED_ROW = {
    "id": 30,
    "text": "早上好，今天是本周群公告日。",
    "schedule_type": "daily",
    "schedule_time": "09:00",
    "enabled": True,
}

# 防退化阈值：断言数量、真实 apiFetch 次数、复数集合 GET 次数。
# 取实测值（每个宽度都是 87 / 61 / 14），少一条就说明场景没跑全，必须响亮地失败。
MIN_CHECKS = 87
MIN_API_CALLS = 61
MIN_PLURAL_GETS = 14

# --------------------------------------------------------------------- 切片


def _slice(
    source: str,
    start_marker: str,
    end_marker: str,
    label: str,
    *,
    inclusive: bool = False,
) -> str:
    if start_marker not in source:
        raise AssertionError(f"app.js 切片起始锚点缺失（{label}）：{start_marker!r}")
    tail = source.split(start_marker, 1)[1]
    if end_marker not in tail:
        raise AssertionError(f"app.js 切片结束锚点缺失（{label}）：{end_marker!r}")
    body = tail.split(end_marker, 1)[0]
    return start_marker + body + (end_marker if inclusive else "")


def _slice_function(source: str, signature: str) -> str:
    return _slice(source, "  " + signature, "\n  }\n", signature, inclusive=True)


def _extract(source: str, pattern: str, label: str) -> str:
    """从 app.js 里就地取出断言要用的字面量，取不到就大声失败。"""

    match = re.search(pattern, source)
    if not match:
        raise AssertionError(f"app.js 中找不到必需字面量（{label}）：{pattern!r}")
    value = match.group(1)
    if not value:
        raise AssertionError(f"app.js 中必需字面量为空（{label}）：{pattern!r}")
    return value


def _messages() -> dict[str, str]:
    """断言用的 toast / 错误文案全部来自 app.js 源码本身。"""

    source = _APP_JS.read_text(encoding="utf-8")
    return {
        "mark_delete": _extract(
            source, r'showToast\("(已标记删除[^"]*)"', "「已标记删除」提示"
        ),
        "no_change_suffix": _extract(
            source,
            r"`\$\{noChangeLabel\}(：此条未由本次操作删除，可能已在别处删除。)`",
            "no_change 警告后缀",
        ),
        "save_prefix": _extract(
            source, r"`(已保存全部 )\$\{successCount\} 项更改`", "保存成功前缀"
        ),
        "save_suffix": _extract(
            source, r"`已保存全部 \$\{successCount\}( 项更改)`", "保存成功后缀"
        ),
        "no_change_only_suffix": _extract(
            source,
            r"`\$\{noChangeCount\}( 项记录已确认无需改动)`",
            "「无需改动」提示后缀",
        ),
        "unconfirmed_message": _extract(
            source,
            r'constructor\(message = "([^"]+)"\)',
            "未确认错误默认文案",
        ),
    }


def _code_slices() -> dict[str, str]:
    source = _APP_JS.read_text(encoding="utf-8")
    slices = {
        # ---- 常量与通用工具
        "template_button_styles": _slice(
            source,
            "  const TEMPLATE_BUTTON_STYLES = [",
            "];\n",
            "TEMPLATE_BUTTON_STYLES",
            inclusive=True,
        ),
        "meta": _slice(
            source,
            "  const RESOURCE_TYPE_META = {",
            "\n  };\n",
            "RESOURCE_TYPE_META",
            inclusive=True,
        ),
        "list_page_size": _slice(
            source,
            "  const LIST_PAGE_SIZE = 8;",
            "\n",
            "LIST_PAGE_SIZE",
            inclusive=True,
        ),
        "draft_selector": _slice(
            source,
            "  const RESOURCE_DRAFT_FORM_SELECTOR = [",
            '].join(", ");\n',
            "RESOURCE_DRAFT_FORM_SELECTOR",
            inclusive=True,
        ),
        "clone": _slice_function(source, "function clone(value)"),
        "same_value": _slice_function(source, "function sameValue(left, right)"),
        # ---- 分页
        "paginate": _slice_function(source, "function paginate(items, key)"),
        "pager_markup": _slice_function(
            source, "function pagerMarkup(key, current, pages, total)"
        ),
        "paginated_rows": _slice_function(
            source, "function paginatedRows(items, key, renderRow, emptyText = \"暂无记录\")"
        ),
        # ---- 分区折叠（真实 <details> + 真实 toggle）
        "section_state_key": _slice_function(
            source, "function groupSectionStateKey(groupId, sectionKey)"
        ),
        "section_is_open": _slice_function(
            source, "function groupSectionIsOpen(group, sectionKey, defaultOpen = false)"
        ),
        "section_dirty": _slice_function(
            source, "function groupSectionDirty(group, settingKeys = [])"
        ),
        "render_settings_section": _slice_function(
            source, "function renderGroupSettingsSection(group, {"
        ),
        "capture_disclosure": _slice_function(
            source, "function captureGroupDisclosureStates()"
        ),
        # ---- 资源小工具
        "op_key": _slice_function(source, "function resourceOperationKey(groupId, type, id)"),
        "edit_key": _slice_function(
            source, "function resourceEditDraftKey(groupId, type, id)"
        ),
        "resource_array": _slice_function(source, "function resourceArray(groupId, type)"),
        "upsert": _slice_function(source, "function upsertResourceRow(groupId, type, document)"),
        "bump_epoch": _slice_function(
            source, "function bumpGroupResourceMutationEpoch(groupId)"
        ),
        "pending_delete": _slice_function(
            source, "function resourcePendingDelete(groupId, type, id)"
        ),
        "pending_create": _slice_function(
            source, "function resourcePendingCreate(groupId, type, id)"
        ),
        "draft_type": _slice_function(source, "function resourceDraftType(key)"),
        "draft_group_id": _slice_function(source, "function resourceDraftGroupId(key)"),
        "group_draft_dirty": _slice_function(
            source, "function groupResourceDraftDirty(groupId, category = \"\")"
        ),
        "category_label": _slice_function(
            source, "function resourceCategoryItemLabel(groupId, category)"
        ),
        "entry_state": _slice_function(source, "function resourceEntryState(groupId, type, id)"),
        "pending_row": _slice_function(
            source, "function pendingDeletionRow(group, type, item, label)"
        ),
        "render_keyword_row": _slice_function(
            source, "function renderKeywordReplyRow(group, item)"
        ),
        "render_scheduled_row": _slice_function(
            source, "function renderScheduledMessageRow(group, item)"
        ),
        "card_head": _slice_function(
            source, "function resourceCardHead(iconName, title, description, count)"
        ),
        "panel_shell": _slice_function(
            source, "function resourcePanelShell(group, category, contentMarkup)"
        ),
        "resource_panel": _slice_function(
            source, "function renderGroupResourcePanel(group, category)"
        ),
        "automation_resources": _slice_function(
            source, "function renderAutomationResources(group, resource)"
        ),
        # ---- 内联按钮文本往返（valuesFromResourceSnapshot 的依赖）
        "normalize_buttons": _slice_function(
            source, "function normalizeTemplateButtons(buttons)"
        ),
        "escape_button_part": _slice_function(
            source, "function escapeTemplateButtonPart(value)"
        ),
        "split_button_line": _slice_function(
            source, "function splitTemplateButtonLine(line)"
        ),
        "buttons_to_text": _slice_function(
            source, "function templateButtonsToText(buttons)"
        ),
        "buttons_from_text": _slice_function(
            source, "function parseTemplateButtonsText(raw)"
        ),
        "style_legend": _slice_function(source, "function templateButtonStyleLegend()"),
        "draft_form_key": _slice_function(source, "function resourceDraftFormKey(form)"),
        # ---- 删除 / 保存管线
        "no_change": _slice(
            source,
            '  const RESOURCE_SAVE_NO_CHANGE = "no_change";',
            "\n",
            "RESOURCE_SAVE_NO_CHANGE",
            inclusive=True,
        ),
        "unconfirmed_error": _slice(
            source,
            "  class MemoryWriteUnconfirmedError extends Error {",
            "\n  }\n",
            "MemoryWriteUnconfirmedError",
            inclusive=True,
        ),
        "delete_outcome": _slice_function(source, "function memoryDeleteOutcome(result)"),
        "apply_saved": _slice_function(
            source, "function applySavedResourceOperation(operation, result)"
        ),
        "persist_op": _slice_function(
            source, "async function persistResourceOperation(operation)"
        ),
        "snapshot_field": _slice_function(
            source, "function snapshotField(snapshot, name, fallback = \"\")"
        ),
        "values_from_snapshot": _slice_function(
            source,
            "function valuesFromResourceSnapshot(type, snapshot, { includeEnabled = false } = {})",
        ),
        "parse_descriptor": _slice_function(
            source, "function parseResourceDraftDescriptor(key)"
        ),
        "collect_ops": _slice_function(
            source, "function collectDeferredResourceOperations()"
        ),
        "remove_create": _slice_function(
            source, "function removePendingResourceCreate(groupId, type, id)"
        ),
        "queue_delete": _slice_function(
            source, "function queueDeferredResourceDelete(groupId, type, id)"
        ),
        "undo_delete": _slice_function(
            source, "function undoDeferredResourceDelete(groupId, type, id)"
        ),
        "save_all": _slice(
            source,
            "  async function saveAllChanges(options = {}) {",
            "\n  function providerReferences(name) {",
            "saveAllChanges",
        ),
        # ---- 加载路径（复数集合 GET 的构造者）
        "paged_load": _slice_function(
            source, "async function loadPagedGroupResource(url, responseKey)"
        ),
        "load_resources": _slice_function(
            source, "async function loadGroupResources(groupId)"
        ),
        # ---- 点击分支
        "click": _slice(
            source,
            '    if (action === "delete-group-resource") {',
            '\n    if (action === "onboarding-preset") {',
            "delete-group-resource 点击分支",
        ),
        # ---- 真实调用点：automation 分区怎么把面板接进去
        "automation_call": _slice(
            source,
            '${renderGroupSettingsSection(group, {\n              key: "automation",',
            "\n            })}",
            "automation 分区调用点",
            inclusive=True,
        ),
    }
    for label, code in slices.items():
        if not code.strip():
            raise AssertionError(f"app.js 切片为空：{label}")
    # 切片完整性：关键标记必须落在切片里，否则说明源文件结构变了。
    required = {
        "meta": [
            '"keyword-replies": { property: "keyword_replies", category: "automation", response: "keyword_reply"',
            '"scheduled-messages": { property: "scheduled_messages", category: "automation", response: "scheduled_message"',
        ],
        "load_resources": [
            '`${base}/keyword-replies`',
            '`${base}/scheduled-messages`',
            "Promise.allSettled",
        ],
        "automation_resources": [
            'renderKeywordReplyRow(group, item)',
            'renderScheduledMessageRow(group, item)',
            "resourceCardHead(",
            "paginatedRows(",
        ],
        "render_keyword_row": [
            'data-entry-edit-form="keyword-replies"',
            'data-resource-type="keyword-replies"',
            'data-action="delete-group-resource"',
        ],
        "render_scheduled_row": [
            'data-entry-edit-form="scheduled-messages"',
            'data-resource-type="scheduled-messages"',
            'name="schedule_time"',
        ],
        "pending_row": [
            "data-pending-resource-delete",
            "将在点击“保存全部”后删除",
            'data-action="undo-delete-group-resource"',
        ],
        "automation_call": [
            'key: "automation"',
            'title: "关键词回复与定时消息"',
            'content: renderGroupResourcePanel(group, "automation")',
            'additionalDirty: groupResourceDraftDirty(group.id, "automation")',
        ],
        "delete_outcome": [
            'status === "deleted" && deleted === true && changed === true',
            'status === "no_change" && deleted === false && changed === false',
            'typeof deleted !== "boolean"',
        ],
        "apply_saved": [
            "throw new MemoryWriteUnconfirmedError()",
            "state.pendingResourceDeletes.delete",
            "state.resourceFormDrafts.delete(editKey)",
            "RESOURCE_SAVE_NO_CHANGE",
        ],
        "persist_op": [
            '`${base}/${encodeURIComponent(operation.id)}`',
            '{ method: "DELETE" }',
        ],
        "collect_ops": [
            "state.pendingResourceDeletes.values()",
            "kind: \"delete\"",
            "parseResourceDraftDescriptor(key)",
            '["keyword-replies", "scheduled-messages", "rules", "memories"]',
        ],
        "save_all": [
            "noChangeCount += 1",
            "successCount += 1",
            "已保存全部 ${successCount} 项更改",
            "此条未由本次操作删除，可能已在别处删除。",
        ],
        "queue_delete": [
            "state.pendingResourceDeletes.set(key",
            'showToast("已标记删除，点击“保存全部”后生效", "warning", 3600)',
        ],
        "click": [
            "queueDeferredResourceDelete(groupId, type, id)",
            '["keyword-replies", "scheduled-messages", "rules", "memories"]',
            "undoDeferredResourceDelete(",
        ],
        "capture_disclosure": ["state.groupSectionOpen.set", "section.open"],
        "render_settings_section": ["data-group-settings-section", "groupSectionIsOpen("],
    }
    for label, markers in required.items():
        for marker in markers:
            if marker not in slices[label]:
                raise AssertionError(f"app.js 切片 {label} 缺少必需标记：{marker!r}")
    return slices


def _find_browser() -> str | None:
    candidates: list[str] = []
    configured = os.environ.get("SETTINGS_HARNESS_BROWSER", "").strip()
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
                str(home / "Library/Caches/ms-playwright/*/chrome-mac*/*.app/Contents/MacOS/*")
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


# ------------------------------------------------------------------- harness

_HARNESS_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>settings resource harness dom</title></head>
<body>
<div id="content"></div>
<pre id="results">pending</pre>
<script>
"use strict";
const EXPECTED_WIDTH = /*__WIDTH__*/;
const GROUP_A = /*__GROUP_A__*/;
const GROUP_B = /*__GROUP_B__*/;
const KEYWORD_ROW = /*__KEYWORD_ROW__*/;
const SCHEDULED_ROW = /*__SCHEDULED_ROW__*/;
const LITERALS = /*__LITERALS__*/;

const results = { checks: [], info: {}, errors: [], toasts: [] };
const checks = [];
function check(name, ok, actual) {
  checks.push({ name: name, ok: Boolean(ok), actual: actual === undefined ? null : String(actual) });
}

const content = document.getElementById("content");
const app = document.createElement("div");
app.className = "app-shell";
document.body.appendChild(app);

// 这三个是 DOM 表现层的小工具（不是本用例的被测对象）：按 app.js 的真实语义做等价实现。
function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
function attr(value) { return escapeHtml(value); }
function icon(name, className = "") {
  return '<i data-lucide="' + attr(name) + '"' + (className ? ' class="' + attr(className) + '"' : "") + "></i>";
}

function makeState() {
  return {
    groups: [], loading: false, saving: false, reloadingGroups: false, accessMutating: false,
    immediateMutations: 0, groupPermissionLoads: new Map(),
    groupResourceLoads: new Map(), groupResourceMutationEpochs: new Map(),
    groupResources: new Map(),
    pendingResourceDeletes: new Map(), pendingResourceCreates: new Map(),
    resourceFormDrafts: new Map(), resourceFormBaselines: new Map(),
    groupCardOpen: new Map(), groupSectionOpen: new Map(),
    groupTemplateButtonDrafts: new Map(), groupApiModelQuerySecretChanges: new Map(),
    groupBaselines: new Map(), groupSaving: new Set(), listPages: new Map(),
    resourceDraftSequence: 0, skipSavePreview: true, savePreview: null, secretChanges: {},
    session: { can_manage_global: true }, document: {}, config: {}, baseline: {},
    configHistory: { entries: [], compare: null },
  };
}
let state = makeState();
let toasts = [];
function showToast(message, kind, ms) {
  toasts.push({ message: String(message), kind: kind || null, ms: ms || null });
}
function updateChrome() {}
function revealGroupControl() {}
// 这一份 harness 只驱动删除管线：表单草稿由场景显式种入，不真去读表单快照。
function captureResourceFormDrafts() {}
function render() { renderContent(); }
function hasUnsavedChanges() { return true; }
function groupDirty() { return false; }
function configDirty() { return false; }
function validateConfig() { return null; }
async function validateGroupForSave() { return true; }
async function runSavePreview() { return true; }
async function persistConfigChanges() { return false; }
async function persistGroupChanges() { return null; }
async function askConfirmation() { return false; }
function beginImmediateMutation() { state.immediateMutations += 1; }
function endImmediateMutation() { state.immediateMutations = Math.max(0, state.immediateMutations - 1); }
function applyImmediateResourceDelete() {}

class ApiError extends Error {
  constructor(status, body, message) {
    super(message || (body && body.error && body.error.message) || ("HTTP " + status));
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

const URL_KEYWORD = "/api/v1/groups/" + GROUP_A + "/keyword-replies";
const URL_SCHED = "/api/v1/groups/" + GROUP_A + "/scheduled-messages";
const URL_DELETE_20 = URL_KEYWORD + "/20";

let fetchLog = [];
let loadLog = [];
const globalFetchLog = [];
let behaviors = new Map();
function apiFetch(url, options) {
  const opts = options || {};
  const method = opts.method || "GET";
  const record = { url: url, method: method, body: opts.body === undefined ? null : opts.body };
  fetchLog.push(record);
  globalFetchLog.push(record);
  const behavior = behaviors.get(method + " " + url) || behaviors.get(url) || { mode: "ok", body: {} };
  if (behavior.mode === "hold") {
    return new Promise((resolve, reject) => {
      held.push({ url: url, method: method, behavior: behavior, resolve: resolve, reject: reject });
    });
  }
  if (behavior.mode === "apiError") return Promise.reject(new ApiError(behavior.status, behavior.body));
  if (behavior.mode === "networkError") return Promise.reject(new Error(behavior.message || "Failed to fetch"));
  return Promise.resolve(clone(behavior.body));
}
let held = [];

// ------------------------------------------------ 被测代码（逐字切片自 app.js）
/*__SLICE_TEMPLATE_BUTTON_STYLES__*/
/*__SLICE_META__*/
/*__SLICE_LIST_PAGE_SIZE__*/
/*__SLICE_DRAFT_SELECTOR__*/
/*__SLICE_CLONE__*/
/*__SLICE_SAME_VALUE__*/
/*__SLICE_PAGINATE__*/
/*__SLICE_PAGER_MARKUP__*/
/*__SLICE_PAGINATED_ROWS__*/
/*__SLICE_SECTION_STATE_KEY__*/
/*__SLICE_SECTION_IS_OPEN__*/
/*__SLICE_SECTION_DIRTY__*/
/*__SLICE_RENDER_SETTINGS_SECTION__*/
/*__SLICE_CAPTURE_DISCLOSURE__*/
/*__SLICE_OP_KEY__*/
/*__SLICE_EDIT_KEY__*/
/*__SLICE_RESOURCE_ARRAY__*/
/*__SLICE_UPSERT__*/
/*__SLICE_BUMP_EPOCH__*/
/*__SLICE_PENDING_DELETE__*/
/*__SLICE_PENDING_CREATE__*/
/*__SLICE_DRAFT_TYPE__*/
/*__SLICE_DRAFT_GROUP_ID__*/
/*__SLICE_GROUP_DRAFT_DIRTY__*/
/*__SLICE_CATEGORY_LABEL__*/
/*__SLICE_ENTRY_STATE__*/
/*__SLICE_PENDING_ROW__*/
/*__SLICE_RENDER_KEYWORD_ROW__*/
/*__SLICE_RENDER_SCHEDULED_ROW__*/
/*__SLICE_CARD_HEAD__*/
/*__SLICE_PANEL_SHELL__*/
/*__SLICE_RESOURCE_PANEL__*/
/*__SLICE_AUTOMATION_RESOURCES__*/
/*__SLICE_NORMALIZE_BUTTONS__*/
/*__SLICE_ESCAPE_BUTTON_PART__*/
/*__SLICE_SPLIT_BUTTON_LINE__*/
/*__SLICE_BUTTONS_TO_TEXT__*/
/*__SLICE_BUTTONS_FROM_TEXT__*/
/*__SLICE_STYLE_LEGEND__*/
/*__SLICE_DRAFT_FORM_KEY__*/
/*__SLICE_NO_CHANGE__*/
/*__SLICE_UNCONFIRMED_ERROR__*/
/*__SLICE_DELETE_OUTCOME__*/
/*__SLICE_APPLY_SAVED__*/
/*__SLICE_PERSIST_OP__*/
/*__SLICE_SNAPSHOT_FIELD__*/
/*__SLICE_VALUES_FROM_SNAPSHOT__*/
/*__SLICE_PARSE_DESCRIPTOR__*/
/*__SLICE_COLLECT_OPS__*/
/*__SLICE_REMOVE_CREATE__*/
/*__SLICE_QUEUE_DELETE__*/
/*__SLICE_UNDO_DELETE__*/
/*__SLICE_SAVE_ALL__*/
/*__SLICE_PAGED_LOAD__*/
/*__SLICE_LOAD_RESOURCES__*/

content.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
/*__SLICE_CLICK__*/
});

// ------------------------------------------------------------- 渲染（等价 renderContent）
let toggleLog = [];
// 真实 app 用捕获阶段监听 details 的 toggle；这里保持同样的监听方式，
// 既记录事件，也把 section.open 同步进 state（真实 app 在下一次渲染时 capture）。
content.addEventListener("toggle", event => {
  const section = event.target;
  if (!section.dataset || section.dataset.groupSettingsSection === undefined) return;
  const sectionKey = section.dataset.groupSection;
  toggleLog.push(sectionKey);
  state.groupSectionOpen.set(
    groupSectionStateKey(section.dataset.groupId, sectionKey),
    section.open,
  );
}, true);

// automation 分区的调用点逐字切自 app.js（含 key / title / content 的接线）。
function renderGroupPage(group) {
  return `
/*__SLICE_AUTOMATION_CALL__*/`;
}

function renderContent() {
  captureGroupDisclosureStates();
  content.innerHTML = renderGroupPage(state.groups[0]);
}

// ------------------------------------------------------------------ 场景工具
const DRAFT_KEY_A_20 = "entry:" + GROUP_A + ":keyword-replies:20";
const OTHER_DRAFT_KEY = "entry:" + GROUP_B + ":keyword-replies:99";

function ids(type) { return resourceArray(GROUP_A, type).map(item => String(item.id)); }
function dkey(id) { return resourceOperationKey(GROUP_A, "keyword-replies", id); }
function ekey(id) { return resourceEditDraftKey(GROUP_A, "keyword-replies", id); }
function requestsFor(url, log) { return (log || fetchLog).filter(item => item.url === url); }
function methods(log) { return (log || fetchLog).map(item => item.method + " " + item.url).join(" | "); }
function toastsWith(text) { return toasts.filter(item => item.message.indexOf(text) >= 0); }
function keywordForm() { return content.querySelector('[data-entry-edit-form="keyword-replies"]'); }
function keywordPendingRow() { return content.querySelector('[data-pending-resource-delete="' + dkey(20) + '"]'); }
function scheduledForm() { return content.querySelector('[data-entry-edit-form="scheduled-messages"]'); }
function deleteButton() {
  return content.querySelector('[data-action="delete-group-resource"][data-resource-type="keyword-replies"]');
}
function automationSection() {
  return content.querySelector('[data-group-settings-section][data-group-section="automation"]');
}
function cardCount(title) {
  const cards = [...content.querySelectorAll("section.resource-category-card")];
  const card = cards.find(item => {
    const heading = item.querySelector("h4");
    return Boolean(heading) && heading.textContent.trim() === title;
  });
  if (!card) return null;
  const badge = card.querySelector(".resource-card-head .badge.info");
  if (!badge) return null;
  const match = /^(\d+)/.exec(badge.textContent.trim());
  return match ? Number(match[1]) : null;
}
function seedDraft(id, text) {
  state.resourceFormDrafts.set(ekey(id), { reply_text: { value: text } });
  state.resourceFormBaselines.set(ekey(id), { reply_text: { value: "初始基线" } });
}
function resetHarness() {
  state = makeState();
  state.groups = [
    { id: GROUP_A, title: "A 群", settings: {} },
    { id: GROUP_B, title: "B 群", settings: {} },
  ];
  state.config = { bot: { memory_facts_enabled: true }, groups: { max_groups: 3 } };
  state.baseline = { bot: { memory_facts_enabled: true }, groups: { max_groups: 3 } };
  state.groupResources.set(GROUP_B, {
    rules: [], memories: [], keyword_replies: [{ id: 99, keyword: "别的群的关键词" }],
    scheduled_messages: [], loaded: true, loading: false, refreshing: false, error: "",
  });
  // 无关群保留它的草稿基线、自己那一行数据、分区展开状态与 config —— 这些才是「不该被改动」的状态。
  // 刻意**不**给无关群留可被采集的脏草稿：collectDeferredResourceOperations() 只按资源类型白名单
  // 过滤、不按群过滤，所以「保存全部」本来就会把所有群的脏草稿一起提交（app.js 的真实设计）。
  // 留一份脏草稿会让请求真的打到无关群，那是设计如此而非隔离缺陷，会把这条断言变成假断言。
  state.resourceFormBaselines.set(OTHER_DRAFT_KEY, { reply_text: { value: "别的群的基线" } });
  state.groupSectionOpen.set(GROUP_B + ":automation", true);
  state.groupBaselines.set(GROUP_B, { welcome_text: "别的群的设置" });
  behaviors = new Map();
  fetchLog = [];
  loadLog = [];
  held = [];
  toasts = [];
  toggleLog = [];
  content.innerHTML = "";
}
function collateralSnapshot() {
  return JSON.stringify({
    otherDrafts: [...state.resourceFormDrafts.keys()].filter(key => resourceDraftGroupId(key) === GROUP_B),
    otherBaselines: [...state.resourceFormBaselines.entries()].filter(([key]) => key.indexOf(GROUP_B) >= 0),
    otherPendingDeletes: [...state.pendingResourceDeletes.keys()].filter(key => key.indexOf(GROUP_B) >= 0),
    otherPendingCreates: [...state.pendingResourceCreates.keys()].filter(key => key.indexOf(GROUP_B) >= 0),
    otherResources: state.groupResources.get(GROUP_B) || null,
    otherGroupBaselines: state.groupBaselines.get(GROUP_B) || null,
    otherSectionOpen: [...state.groupSectionOpen.entries()].filter(([key]) => key.indexOf(GROUP_B) === 0),
    config: state.config,
    configBaseline: state.baseline,
    secretChanges: state.secretChanges,
  });
}
function checkCollateral(label, before) {
  check(label + "：无关群草稿 / 基线 / 资源与 config 深度未变", collateralSnapshot() === before, collateralSnapshot());
  check(label + "：没有任何请求指向无关群", globalFetchLog.every(item => item.url.indexOf(GROUP_B) < 0), globalFetchLog.map(item => item.url).join(","));
}
async function settle(rounds) {
  const total = rounds || 8;
  for (let i = 0; i < total; i += 1) { await new Promise(resolve => setTimeout(resolve, 0)); }
}

// 走真实 loadGroupResources：集合 GET 的 URL 由 app.js 自己拼。
async function loadAndOpen() {
  behaviors.set(URL_KEYWORD, { mode: "ok", body: { keyword_replies: [KEYWORD_ROW] } });
  behaviors.set(URL_SCHED, { mode: "ok", body: { scheduled_messages: [SCHEDULED_ROW] } });
  await loadGroupResources(GROUP_A);
  await settle();
  loadLog = fetchLog.slice();
  fetchLog = [];
  const section = automationSection();
  if (section) {
    section.open = true;          // 真实用户展开：浏览器会派发真实 toggle 事件
    await settle(12);
  }
}

// -------------------------------------------------------------------- 场景
async function scenarioRender() {
  resetHarness();
  const before = collateralSnapshot();
  await loadAndOpen();
  const kwForm = keywordForm();
  const smForm = scheduledForm();
  const kwGets = requestsFor(URL_KEYWORD, loadLog);
  const smGets = requestsFor(URL_SCHED, loadLog);
  check("a 渲染：真实 GET 了 keyword-replies 集合", kwGets.length === 1 && kwGets[0].method === "GET", methods(loadLog));
  check("a 渲染：真实 GET 了 scheduled-messages 集合", smGets.length === 1 && smGets[0].method === "GET", methods(loadLog));
  check(
    "a 渲染：集合 URL 同时含群号与复数段",
    URL_KEYWORD === "/api/v1/groups/" + GROUP_A + "/keyword-replies"
      && URL_SCHED === "/api/v1/groups/" + GROUP_A + "/scheduled-messages",
    URL_KEYWORD + " / " + URL_SCHED,
  );
  check("a 渲染：关键词行表单已渲染", Boolean(kwForm), kwForm ? "已渲染" : "缺失");
  check("a 渲染：定时消息行表单已渲染", Boolean(smForm), smForm ? "已渲染" : "缺失");
  check("a 渲染：DOM 出现关键词文本", content.textContent.indexOf(KEYWORD_ROW.keyword) >= 0, content.textContent.slice(0, 300));
  check("a 渲染：DOM 出现关键词回复正文", content.textContent.indexOf(KEYWORD_ROW.reply_text) >= 0, content.textContent.slice(0, 300));
  check("a 渲染：DOM 出现定时消息正文", content.textContent.indexOf(SCHEDULED_ROW.text) >= 0, content.textContent.slice(0, 300));
  check(
    "a 渲染：定时时间 input 值为 09:00",
    Boolean(smForm) && smForm.querySelector('input[name="schedule_time"]').value === SCHEDULED_ROW.schedule_time,
    smForm ? smForm.querySelector('input[name="schedule_time"]').value : "无表单",
  );
  check(
    "a 渲染：关键词 input 值为菜单",
    Boolean(kwForm) && kwForm.querySelector('input[name="keyword"]').value === KEYWORD_ROW.keyword,
    kwForm ? kwForm.querySelector('input[name="keyword"]').value : "无表单",
  );
  check("a 渲染：关键词卡片计数为 1 条", cardCount("关键词回复") === 1, String(cardCount("关键词回复")));
  check("a 渲染：定时消息卡片计数为 1 条", cardCount("定时消息") === 1, String(cardCount("定时消息")));
  const subtotal = content.querySelector('[data-group-settings-section][data-group-section="automation"] .group-section-count');
  check("a 渲染：分区小计为非零条数", Boolean(subtotal) && subtotal.textContent.trim() === "2 条", subtotal ? subtotal.textContent.trim() : "缺失");
  check("a 渲染：本地列表各有一行", ids("keyword-replies").length === 1 && ids("scheduled-messages").length === 1, ids("keyword-replies").join(",") + " / " + ids("scheduled-messages").join(","));
  check("a 渲染：自动化分区真实 toggle 事件已触发", toggleLog.indexOf("automation") >= 0, toggleLog.join(","));
  const section = automationSection();
  check("a 渲染：分区已展开", Boolean(section) && section.open === true, section ? String(section.open) : "缺失");
  check(
    "a 渲染：展开后行确实占位渲染",
    Boolean(kwForm) && kwForm.getBoundingClientRect().height > 0,
    kwForm ? String(Math.round(kwForm.getBoundingClientRect().height)) : "缺失",
  );
  checkCollateral("a 渲染", before);
}

async function scenarioQueueOnly() {
  resetHarness();
  const before = collateralSnapshot();
  await loadAndOpen();
  seedDraft(20, "编辑中的回复");
  const button = deleteButton();
  check("b 只排队：删除按钮存在", Boolean(button), button ? "存在" : "缺失");
  button.click();
  await settle();
  check("b 只排队：零 DELETE 请求", fetchLog.every(item => item.method !== "DELETE"), methods());
  check("b 只排队：本轮零请求", fetchLog.length === 0, methods());
  check("b 只排队：待删除队列含 keyword-replies:20", state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check("b 只排队：本地行仍在列表", ids("keyword-replies").indexOf("20") >= 0, ids("keyword-replies").join(","));
  const pendingRow = content.querySelector('[data-pending-resource-delete="' + dkey(20) + '"]');
  check(
    "b 只排队：DOM 显示待删除行且保留关键词文本",
    Boolean(pendingRow) && pendingRow.textContent.indexOf(KEYWORD_ROW.keyword) >= 0,
    pendingRow ? pendingRow.textContent.trim() : "缺失",
  );
  check("b 只排队：可编辑表单已被待删除行替换", keywordForm() === null, keywordForm() ? "仍在" : "已替换");
  check("b 只排队：草稿与基线未被改动", state.resourceFormDrafts.has(ekey(20)) && state.resourceFormBaselines.has(ekey(20)), JSON.stringify([...state.resourceFormDrafts.keys()]));
  check(
    "b 只排队：提示逐字为已标记删除且为 warning",
    toastsWith(LITERALS.mark_delete).length === 1 && toastsWith(LITERALS.mark_delete)[0].kind === "warning",
    JSON.stringify(toasts),
  );
  const undo = content.querySelector('[data-action="undo-delete-group-resource"]');
  check("b 撤销：撤销按钮存在", Boolean(undo), undo ? "存在" : "缺失");
  undo.click();
  await settle();
  check("b 撤销：待删除队列已清", !state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check("b 撤销：可编辑表单恢复", Boolean(keywordForm()), keywordForm() ? "已恢复" : "缺失");
  check("b 撤销：零请求", fetchLog.length === 0, methods());
  checkCollateral("b 只排队", before);
}

async function scenarioSaveDelete() {
  resetHarness();
  await loadAndOpen();
  const before = collateralSnapshot();
  seedDraft(20, "编辑中的回复");
  deleteButton().click();
  await settle();
  behaviors.set("DELETE " + URL_DELETE_20, { mode: "ok", body: { ok: true, deleted: true, changed: true, status: "deleted" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("c 保存：恰好一次 DELETE 且方法为 DELETE", requestsFor(URL_DELETE_20).length === 1 && requestsFor(URL_DELETE_20)[0].method === "DELETE", methods());
  check("c 保存：DELETE URL 精确匹配", fetchLog.length === 1 && fetchLog[0].url === URL_DELETE_20, methods());
  check("c 保存：本地列表不再含 id 20", ids("keyword-replies").indexOf("20") < 0, ids("keyword-replies").join(","));
  check("c 保存：待删除条目已清", !state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check(
    "c 保存：该行草稿与基线已清",
    !state.resourceFormDrafts.has(ekey(20)) && !state.resourceFormBaselines.has(ekey(20)),
    JSON.stringify([...state.resourceFormDrafts.keys()]),
  );
  check(
    "c 保存：计为一次成功（逐字成功提示）",
    toastsWith(LITERALS.save_prefix + "1" + LITERALS.save_suffix).length === 1
      && toastsWith(LITERALS.save_prefix + "1" + LITERALS.save_suffix)[0].kind === "success",
    JSON.stringify(toasts),
  );
  check("c 保存：没有失败提示", !toasts.some(item => item.kind === "error"), JSON.stringify(toasts));
  check("c 保存：DOM 中该行已消失", keywordForm() === null, keywordForm() ? "仍在" : "已消失");
  checkCollateral("c 保存", before);
}

async function scenarioNoChange() {
  resetHarness();
  await loadAndOpen();
  const before = collateralSnapshot();
  seedDraft(20, "编辑中的回复");
  deleteButton().click();
  await settle();
  behaviors.set("DELETE " + URL_DELETE_20, { mode: "ok", body: { ok: true, deleted: false, changed: false, status: "no_change" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("d no_change：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(URL_DELETE_20)[0].method === "DELETE", methods());
  check("d no_change：本地行按不可用移除", ids("keyword-replies").indexOf("20") < 0, ids("keyword-replies").join(","));
  check("d no_change：待删除条目已清", !state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check(
    "d no_change：该行草稿与基线已清",
    !state.resourceFormDrafts.has(ekey(20)) && !state.resourceFormBaselines.has(ekey(20)),
    JSON.stringify([...state.resourceFormDrafts.keys()]),
  );
  const label = RESOURCE_TYPE_META["keyword-replies"].label;
  check(
    "d no_change：警告文案逐字出现",
    toasts.some(item => item.kind === "warning" && item.message.indexOf(label + LITERALS.no_change_suffix) >= 0),
    JSON.stringify(toasts),
  );
  check(
    "d no_change：不算成功（无逐字成功提示）",
    toastsWith(LITERALS.save_prefix + "1" + LITERALS.save_suffix).length === 0,
    JSON.stringify(toasts),
  );
  check(
    "d no_change：只报无需改动",
    toastsWith("1" + LITERALS.no_change_only_suffix).length === 1
      && toastsWith("1" + LITERALS.no_change_only_suffix)[0].kind === "warning",
    JSON.stringify(toasts),
  );
  check("d no_change：没有任何 success 提示", !toasts.some(item => item.kind === "success"), JSON.stringify(toasts));
  checkCollateral("d no_change", before);
}

async function runUnconfirmed(describe, body) {
  resetHarness();
  await loadAndOpen();
  const before = collateralSnapshot();
  seedDraft(20, "编辑中的回复");
  deleteButton().click();
  await settle();
  behaviors.set("DELETE " + URL_DELETE_20, { mode: "ok", body: body });
  await saveAllChanges({ skipPreview: true });
  await settle();
  const label = "e " + describe;
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(URL_DELETE_20)[0].method === "DELETE", methods());
  check(label + "：待删除条目保留", state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check(
    label + "：草稿保留",
    (state.resourceFormDrafts.get(ekey(20)) || {}).reply_text?.value === "编辑中的回复",
    JSON.stringify([...state.resourceFormDrafts.entries()]),
  );
  check(label + "：本地行保留", ids("keyword-replies").indexOf("20") >= 0, ids("keyword-replies").join(","));
  check(label + "：无成功提示", toastsWith(LITERALS.save_prefix).length === 0 && !toasts.some(item => item.kind === "success"), JSON.stringify(toasts));
  check(
    label + "：走了未确认路径（逐字未确认文案）",
    toasts.some(item => item.kind === "error" && item.message.indexOf(LITERALS.unconfirmed_message) >= 0),
    JSON.stringify(toasts),
  );
  // 已排队的删除会把可编辑表单换成「待删除」行，所以这里断言的是待删除行仍在 DOM，
  // 而不是（本来就不该再存在的）可编辑表单。
  check(label + "：待删除行仍在 DOM", Boolean(keywordPendingRow()) && !keywordForm(), keywordPendingRow() ? "待删除行在" : "缺失");
  checkCollateral(label, before);
}

async function scenarioUnconfirmed() {
  const missing = memoryDeleteOutcome({ ok: true, deleted: true });
  const contradictory = memoryDeleteOutcome({ ok: true, status: "deleted", deleted: true, changed: false });
  check("e 缺 status/changed 直接判定 unconfirmed", missing.kind === "unconfirmed", missing.kind);
  check("e 字段矛盾直接判定 unconfirmed", contradictory.kind === "unconfirmed", contradictory.kind);
  await runUnconfirmed("缺 status/changed", { ok: true, deleted: true });
  await runUnconfirmed("字段矛盾", { status: "deleted", deleted: true, changed: false });
}

async function scenarioNetworkDrop() {
  resetHarness();
  await loadAndOpen();
  const before = collateralSnapshot();
  seedDraft(20, "编辑中的回复");
  deleteButton().click();
  await settle();
  behaviors.set("DELETE " + URL_DELETE_20, { mode: "networkError", message: "Failed to fetch" });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("f 断网：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(URL_DELETE_20)[0].method === "DELETE", methods());
  check("f 断网：待删除条目保留", state.pendingResourceDeletes.has(dkey(20)), [...state.pendingResourceDeletes.keys()].join(","));
  check(
    "f 断网：草稿保留",
    (state.resourceFormDrafts.get(ekey(20)) || {}).reply_text?.value === "编辑中的回复",
    JSON.stringify([...state.resourceFormDrafts.entries()]),
  );
  check("f 断网：本地行保留", ids("keyword-replies").indexOf("20") >= 0, ids("keyword-replies").join(","));
  check("f 断网：无成功提示", !toasts.some(item => item.kind === "success"), JSON.stringify(toasts));
  check(
    "f 断网：失败文案含原始错误",
    toasts.some(item => item.kind === "error" && item.message.indexOf("Failed to fetch") >= 0),
    JSON.stringify(toasts),
  );
  check("f 断网：待删除行仍在 DOM", Boolean(keywordPendingRow()) && !keywordForm(), keywordPendingRow() ? "待删除行在" : "缺失");
  checkCollateral("f 断网", before);
}

let PRISTINE_COLLATERAL = null;
async function main() {
  try {
    results.info.viewport = document.documentElement.clientWidth;
    results.info.innerWidth = window.innerWidth;
    results.info.expectedWidth = EXPECTED_WIDTH;
    await scenarioRender();
    await scenarioQueueOnly();
    await scenarioSaveDelete();
    await scenarioNoChange();
    await scenarioUnconfirmed();
    await scenarioNetworkDrop();
    check("g 全程：没有任何请求指向无关群", globalFetchLog.every(item => item.url.indexOf(GROUP_B) < 0), globalFetchLog.map(item => item.url).join(","));
    check("g 全程：无关群状态与初始化时深度一致", collateralSnapshot() === PRISTINE_COLLATERAL, collateralSnapshot());
    check("g 全程：无关群资源仍只有它自己的那一行", (state.groupResources.get(GROUP_B) || {}).keyword_replies.length === 1, JSON.stringify(state.groupResources.get(GROUP_B)));
    // 「保存全部」会采集所有群的脏草稿（只按类型白名单过滤），所以无关群里不能留可被采集的脏草稿，
    // 否则「零请求指向无关群」就不再是隔离性断言。这条检查把这个前提显式钉住。
    check(
      "g 全程：无关群没有被采集的脏草稿",
      [...state.resourceFormDrafts.keys()].every(key => resourceDraftGroupId(key) !== GROUP_B),
      [...state.resourceFormDrafts.keys()].join(","),
    );
    results.info.totalRequests = globalFetchLog.length;
    results.info.pluralCollectionGets = globalFetchLog.filter(item => item.method === "GET" && (item.url === URL_KEYWORD || item.url === URL_SCHED)).length;
    results.info.deleteRequests = globalFetchLog.filter(item => item.method === "DELETE").length;
    results.info.checkCount = checks.length;
    results.info.toggleEvents = toggleLog.join(",");
  } catch (error) {
    results.errors.push(String(error && error.stack ? error.stack : error));
  }
  results.checks = checks;
  document.getElementById("results").textContent = JSON.stringify(results);
}
PRISTINE_COLLATERAL = (function () { resetHarness(); return collateralSnapshot(); })();
main();
</script>
</body>
</html>
"""


class SettingsHarnessResourceDomTests(unittest.TestCase):
    """在真实 Blink 里驱动逐字切片的两个复数群管理资源（真实 DOM + 真实请求日志）。"""

    _cached_payloads: list[tuple[int, dict]] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = _find_browser()
        cls.slices = _code_slices()
        cls.messages = _messages()
        cls._cached_payloads = None

    def _build_page(self, directory: pathlib.Path, width: int) -> pathlib.Path:
        page = _HARNESS_TEMPLATE
        for label, code in self.slices.items():
            token = f"/*__SLICE_{label.upper()}__*/"
            self.assertIn(token, page, f"harness 缺少切片占位符 {token}")
            # str.replace 是字面替换：切片里的 ``$`` / ``\`` / 模板字符串都不会被当成语法。
            page = page.replace(token, code)
        page = page.replace("/*__WIDTH__*/", str(width))
        page = page.replace("/*__GROUP_A__*/", json.dumps(GROUP_A))
        page = page.replace("/*__GROUP_B__*/", json.dumps(GROUP_B))
        page = page.replace("/*__KEYWORD_ROW__*/", json.dumps(KEYWORD_ROW, ensure_ascii=False))
        page = page.replace("/*__SCHEDULED_ROW__*/", json.dumps(SCHEDULED_ROW, ensure_ascii=False))
        page = page.replace("/*__LITERALS__*/", json.dumps(self.messages, ensure_ascii=False))
        self.assertNotIn("/*__", page, "harness 还有未替换的占位符")
        path = directory / f"settings-resource-{width}.html"
        path.write_text(page, encoding="utf-8")
        return path

    def _run(self, browser: str, directory: pathlib.Path, page: pathlib.Path, width: int) -> dict:
        env = dict(os.environ)
        env["HOME"] = str(directory)
        completed = subprocess.run(
            [
                browser,
                "--no-sandbox",
                "--disable-gpu",
                "--disable-crash-reporter",
                f"--user-data-dir={directory / 'profile'}",
                f"--window-size={width},900",
                "--virtual-time-budget=20000",
                "--dump-dom",
                page.as_uri(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            env=env,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"{width}px 浏览器退出码 {completed.returncode}\nstderr:\n{completed.stderr[-2000:]}",
        )
        match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.S)
        self.assertIsNotNone(match, f"{width}px harness 没有写出 #results（页面脚本可能抛错了）")
        raw = html_module.unescape(match.group(1))
        self.assertNotEqual(raw.strip(), "pending", f"{width}px harness 没有跑完 main()")
        return json.loads(raw)

    def _payloads(self) -> list[tuple[int, dict]]:
        cls = type(self)
        if cls._cached_payloads is None:
            if not cls.browser:
                self.fail(
                    "真实 DOM 用例需要一个 Chromium/Chrome 可执行文件：请设置 SETTINGS_HARNESS_BROWSER=<path> "
                    "或安装 chromium（本用例刻意不 skip，静默跳过等于没验证）。"
                )
            with tempfile.TemporaryDirectory(prefix="settings-resource-dom-") as raw_dir:
                directory = pathlib.Path(raw_dir)
                collected: list[tuple[int, dict]] = []
                for width in WIDTHS:
                    page = self._build_page(directory, width)
                    collected.append((width, self._run(cls.browser, directory, page, width)))
                cls._cached_payloads = collected
        return cls._cached_payloads

    def _assert_payloads_clean(self) -> list[tuple[int, dict]]:
        per_width = self._payloads()
        for width, payload in per_width:
            self.assertEqual(payload["errors"], [], f"{width}px harness 抛错：{payload['errors']}")
            self.assertTrue(payload["checks"], f"{width}px harness 没有产出任何断言")
            failed = [item for item in payload["checks"] if not item["ok"]]
            summary = "\n".join(
                f"- {item['name']}: {'OK' if item['ok'] else 'FAIL'} (actual={item['actual']})"
                for item in payload["checks"]
            )
            self.assertEqual(failed, [], f"{width}px 真实浏览器断言失败：\n{summary}")
            # 真实渲染视口确实是目标宽度，避免「测了个假宽度」。
            self.assertEqual(
                payload["info"]["viewport"],
                width,
                f"{width}px 实际视口是 {payload['info']['viewport']}，没有真的按目标宽度渲染",
            )
            # 防退化：断言数量与真实请求都必须够多，场景确实都被执行了。
            self.assertGreaterEqual(
                len(payload["checks"]), MIN_CHECKS,
                f"{width}px 断言数量过少（{len(payload['checks'])}），场景可能没跑全",
            )
            self.assertGreaterEqual(
                payload["info"]["totalRequests"], MIN_API_CALLS,
                f"{width}px 真实 apiFetch 次数过少（{payload['info']['totalRequests']}），场景可能空跑",
            )
            self.assertGreaterEqual(
                payload["info"]["pluralCollectionGets"], MIN_PLURAL_GETS,
                f"{width}px 复数集合 GET 次数过少（{payload['info']['pluralCollectionGets']}），没有走真实加载路径",
            )
        return per_width

    def _assert_check_names(self, names: set[str], required: tuple[str, ...], width: int) -> None:
        for name in required:
            self.assertIn(name, names, f"{width}px 缺少场景断言：{name}")

    def test_plural_rows_render_in_a_real_browser(self) -> None:
        for width, payload in self._assert_payloads_clean():
            names = {item["name"] for item in payload["checks"]}
            self._assert_check_names(
                names,
                (
                    "a 渲染：真实 GET 了 keyword-replies 集合",
                    "a 渲染：真实 GET 了 scheduled-messages 集合",
                    "a 渲染：集合 URL 同时含群号与复数段",
                    "a 渲染：关键词行表单已渲染",
                    "a 渲染：定时消息行表单已渲染",
                    "a 渲染：DOM 出现关键词文本",
                    "a 渲染：DOM 出现关键词回复正文",
                    "a 渲染：DOM 出现定时消息正文",
                    "a 渲染：定时时间 input 值为 09:00",
                    "a 渲染：关键词卡片计数为 1 条",
                    "a 渲染：定时消息卡片计数为 1 条",
                    "a 渲染：分区小计为非零条数",
                    "a 渲染：自动化分区真实 toggle 事件已触发",
                    "a 渲染：分区已展开",
                    "a 渲染：展开后行确实占位渲染",
                ),
                width,
            )

    def test_delete_only_queues_and_save_issues_exactly_one_delete(self) -> None:
        for width, payload in self._assert_payloads_clean():
            names = {item["name"] for item in payload["checks"]}
            self._assert_check_names(
                names,
                (
                    "b 只排队：零 DELETE 请求",
                    "b 只排队：本轮零请求",
                    "b 只排队：待删除队列含 keyword-replies:20",
                    "b 只排队：本地行仍在列表",
                    "b 只排队：DOM 显示待删除行且保留关键词文本",
                    "b 只排队：提示逐字为已标记删除且为 warning",
                    "b 撤销：待删除队列已清",
                    "c 保存：恰好一次 DELETE 且方法为 DELETE",
                    "c 保存：DELETE URL 精确匹配",
                    "c 保存：本地列表不再含 id 20",
                    "c 保存：待删除条目已清",
                    "c 保存：该行草稿与基线已清",
                    "c 保存：计为一次成功（逐字成功提示）",
                ),
                width,
            )

    def test_no_change_and_unconfirmed_are_not_success(self) -> None:
        for width, payload in self._assert_payloads_clean():
            names = {item["name"] for item in payload["checks"]}
            self._assert_check_names(
                names,
                (
                    "d no_change：不算成功（无逐字成功提示）",
                    "d no_change：警告文案逐字出现",
                    "d no_change：只报无需改动",
                    "d no_change：待删除条目已清",
                    "e 缺 status/changed 直接判定 unconfirmed",
                    "e 字段矛盾直接判定 unconfirmed",
                    "e 缺 status/changed：待删除条目保留",
                    "e 缺 status/changed：走了未确认路径（逐字未确认文案）",
                    "e 字段矛盾：待删除条目保留",
                    "e 字段矛盾：走了未确认路径（逐字未确认文案）",
                    "e 字段矛盾：本地行保留",
                ),
                width,
            )

    def test_network_failure_keeps_draft_and_pending_entry(self) -> None:
        for width, payload in self._assert_payloads_clean():
            names = {item["name"] for item in payload["checks"]}
            self._assert_check_names(
                names,
                (
                    "f 断网：恰好一次 DELETE",
                    "f 断网：待删除条目保留",
                    "f 断网：草稿保留",
                    "f 断网：本地行保留",
                    "f 断网：无成功提示",
                    "f 断网：失败文案含原始错误",
                ),
                width,
            )

    def test_unrelated_group_is_untouched(self) -> None:
        for width, payload in self._assert_payloads_clean():
            names = {item["name"] for item in payload["checks"]}
            self._assert_check_names(
                names,
                (
                    "g 全程：没有任何请求指向无关群",
                    "g 全程：无关群状态与初始化时深度一致",
                    "g 全程：无关群资源仍只有它自己的那一行",
                    "g 全程：无关群没有被采集的脏草稿",
                    "a 渲染：无关群草稿 / 基线 / 资源与 config 深度未变",
                    "b 只排队：没有任何请求指向无关群",
                    "c 保存：无关群草稿 / 基线 / 资源与 config 深度未变",
                    "d no_change：没有任何请求指向无关群",
                    "e 字段矛盾：无关群草稿 / 基线 / 资源与 config 深度未变",
                    "f 断网：没有任何请求指向无关群",
                ),
                width,
            )


if __name__ == "__main__":
    unittest.main()
