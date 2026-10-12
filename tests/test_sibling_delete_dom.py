"""真实 DOM 用例：Mini App「保存全部」里**三类群内兄弟资源**的删除语义。

三类兄弟资源（``RESOURCE_TYPE_META`` 的键）：``rules`` / ``keyword-replies`` /
``scheduled-messages``。``memories`` 的同类用例已存在于
``tests/test_miniapp_memory_write_dom.py``；本模块复用它的全部手法。

静态字符串测试只能证明源码里出现了某些字符；它证明不了：

* 点击删除按钮**只排队**、一个请求都不发；
* 2xx 返回的 ``status`` / ``deleted`` / ``changed`` 字段缺失或互相矛盾时，
  三类资源都**不当成删除成功**（共用同一个判据，不是只有 memories 才校验）；
* 删除被确认后本地行、待删除标记、编辑草稿、基线都被清干净；
* ``no_change`` 时后端确认「这条早就没了」：本地行按不可用移除，但**不算本次
  删除成功**，计数进 ``noChangeCount``，提示的是「记录已确认无需改动」；
* 503 / 断网时**草稿与待删除标记都保留**；
* 无关群草稿与未保存 config 在全程保持原样。

本模块把 ``bot/web/static/app.js`` 里的真实实现逐字切片（``RESOURCE_TYPE_META`` /
``RESOURCE_DRAFT_FORM_SELECTOR`` / ``resourceOperationKey`` / ``resourceEditDraftKey`` /
``resourceArray`` / ``upsertResourceRow`` / ``bumpGroupResourceMutationEpoch`` /
``resourcePendingDelete`` / ``RESOURCE_SAVE_NO_CHANGE`` / ``MemoryWriteUnconfirmedError`` /
``memoryDeleteOutcome`` / ``memoryDocumentFromResult`` / ``applySavedResourceOperation`` /
``persistResourceOperation`` / ``saveAllChanges`` / ``removePendingResourceCreate`` /
``queueDeferredResourceDelete`` / ``delete-group-resource`` 点击分支），在真实 Blink 内核
（Chrome headless shell）里用**真实 DOM** 驱动，断言真实请求日志、真实 toast 文本与
harness 自己那份 ``state``。

切片锚点缺失 → 立即 ``AssertionError``（防止源码漂移后测试静默失效）。找不到可执行
文件时 ``self.fail`` 而不是 ``skip``：静默跳过等于没验证。可用 ``OPERATIONS_BROWSER``
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

# 三类兄弟资源 + 各自的展示名（后端 label，用于拼出 no_change 警告文案）。
SIBLING_TYPES = ("rules", "keyword-replies", "scheduled-messages")
TYPE_LABELS = {
    "rules": "群规",
    "keyword-replies": "关键词回复",
    "scheduled-messages": "定时消息",
}

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


def _code_slices() -> dict[str, str]:
    source = _APP_JS.read_text(encoding="utf-8")
    slices = {
        "meta": _slice(
            source,
            "  const RESOURCE_TYPE_META = {",
            "\n  };\n",
            "RESOURCE_TYPE_META",
            inclusive=True,
        ),
        "draft_selector": _slice(
            source,
            "  const RESOURCE_DRAFT_FORM_SELECTOR = [",
            '].join(", ");\n',
            "RESOURCE_DRAFT_FORM_SELECTOR",
            inclusive=True,
        ),
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
        "no_change": _slice(
            source,
            '  const RESOURCE_SAVE_NO_CHANGE = "no_change";',
            "\n",
            "RESOURCE_SAVE_NO_CHANGE",
        ),
        "unconfirmed_error": _slice(
            source,
            "  class MemoryWriteUnconfirmedError extends Error {",
            "\n  }\n",
            "MemoryWriteUnconfirmedError",
            inclusive=True,
        ),
        "delete_outcome": _slice_function(source, "function memoryDeleteOutcome(result)"),
        "document_from_result": _slice_function(
            source, "function memoryDocumentFromResult(result)"
        ),
        "apply_saved": _slice_function(
            source, "function applySavedResourceOperation(operation, result)"
        ),
        "persist_op": _slice_function(
            source, "async function persistResourceOperation(operation)"
        ),
        "save_all": _slice(
            source,
            "  async function saveAllChanges(options = {}) {",
            "\n  function providerReferences(name) {",
            "saveAllChanges",
        ),
        "remove_create": _slice_function(
            source, "function removePendingResourceCreate(groupId, type, id)"
        ),
        "queue_delete": _slice_function(
            source, "function queueDeferredResourceDelete(groupId, type, id)"
        ),
        "click": _slice(
            source,
            '    if (action === "delete-group-resource") {',
            '\n    if (action === "undo-delete-group-resource") {',
            "delete-group-resource 点击分支",
        ),
    }
    for label, code in slices.items():
        if not code.strip():
            raise AssertionError(f"app.js 切片为空：{label}")
    # 切片完整性：关键标记必须落在切片里，否则说明源文件结构变了。
    required = {
        "meta": [
            '"keyword-replies": { property: "keyword_replies"',
            'label: "关键词回复"',
            '"scheduled-messages": { property: "scheduled_messages"',
            'label: "定时消息"',
            'rules: { property: "rules"',
            'label: "群规"',
            'response: "keyword_reply"',
            'response: "scheduled_message"',
            'response: "rule"',
        ],
        "draft_selector": [
            "[data-rule-edit-form]",
            '[data-resource-form][data-save-scope="deferred"]',
        ],
        "delete_outcome": [
            "typeof deleted !== \"boolean\"",
            "typeof changed !== \"boolean\"",
            'status === "deleted" && deleted === true && changed === true',
            'status === "no_change" && deleted === false && changed === false',
            'return { kind: "unconfirmed" };',
        ],
        "apply_saved": [
            "const outcome = memoryDeleteOutcome(result);",
            "throw new MemoryWriteUnconfirmedError()",
            "state.pendingResourceDeletes.delete(resourceOperationKey(operation.groupId, operation.type, operation.id))",
            "state.resourceFormDrafts.delete(editKey)",
            "state.resourceFormBaselines.delete(editKey)",
            "RESOURCE_SAVE_NO_CHANGE",
        ],
        "persist_op": [
            '{ method: "DELETE" }',
            'method: operation.kind === "update" ? "PATCH" : "POST"',
        ],
        "save_all": [
            "noChangeCount += 1",
            "noChangeTypes.add(operation.type)",
            "永久记忆：此条永久记忆未由本次操作删除，可能已在别处删除。",
            "`${noChangeLabel}：此条未由本次操作删除，可能已在别处删除。`",
            "successCount += 1",
            "已保存全部 ${successCount} 项更改",
            "${noChangeCount} 项永久记忆已确认无需改动",
            "${noChangeCount} 项记录已确认无需改动",
        ],
        "queue_delete": [
            "state.pendingResourceDeletes.set(key",
            'showToast("已标记删除，点击“保存全部”后生效", "warning", 3600)',
        ],
        "remove_create": [
            "state.pendingResourceCreates.delete(operationKey)",
            "state.pendingResourceDeletes.delete(operationKey)",
        ],
        "click": [
            'if (["keyword-replies", "scheduled-messages", "rules", "memories"].includes(type))',
            "queueDeferredResourceDelete(groupId, type, id)",
            "return;",
        ],
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
<title>mini app sibling delete harness</title></head>
<body>
<div id="content"></div>
<div id="panel"></div>
<pre id="results">pending</pre>
<script>
"use strict";
const EXPECTED_WIDTH = /*__WIDTH__*/;
const GROUP_A = /*__GROUP_A__*/;
const GROUP_B = /*__GROUP_B__*/;
const TYPES = /*__TYPES__*/;
const TYPE_LABEL = /*__TYPE_LABELS__*/;
const results = { checks: [], info: {}, errors: [], toasts: [] };
const checks = [];
function check(name, ok, actual) {
  checks.push({ name: name, ok: Boolean(ok), actual: actual === undefined ? null : String(actual) });
}

const content = document.getElementById("content");
const panel = document.getElementById("panel");
const app = document.createElement("div");
app.className = "app-shell";
document.body.appendChild(app);

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function makeState() {
  return {
    groups: [], loading: false, saving: false, reloadingGroups: false, accessMutating: false,
    immediateMutations: 0, groupPermissionLoads: new Map(),
    pendingResourceDeletes: new Map(), pendingResourceCreates: new Map(),
    resourceFormDrafts: new Map(), resourceFormBaselines: new Map(),
    groupResourceMutationEpochs: new Map(), groupResources: new Map(),
    groupSaving: new Set(), listPages: new Map(), resourceDraftSequence: 0,
    skipSavePreview: true, savePreview: null, secretChanges: {},
    session: { can_manage_global: true }, document: {}, config: {},
    configHistory: { entries: [], compare: null },
  };
}
let state = makeState();
let toasts = [];
function showToast(message, kind, ms) {
  toasts.push({ message: String(message), kind: kind || null, ms: ms || null });
}

// 注意：真实的 renderContent 太长没有逐字切片，这里是 harness 自写的等价渲染：
// 只把「当前 state.groupResources 里三类资源的行」画成带类型前缀的 DOM 行，
// 供 checkDom 对比「DOM 行 == 本地列表」。
function renderContent() {
  const parts = [];
  for (const type of TYPES) {
    for (const item of resourceArray(GROUP_A, type)) {
      parts.push('<div data-resource-row="' + type + ":" + escapeHtml(item.id) + '"></div>');
    }
  }
  panel.innerHTML = parts.join("");
}
function render() { renderContent(); }
function updateChrome() {}
function captureResourceFormDrafts() {}
function resourceDraftFormKey(form) { return (form && form.dataset && form.dataset.formKey) || ""; }
function revealGroupControl() {}
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

let nextOperations = [];
function collectDeferredResourceOperations() { return clone(nextOperations); }

class ApiError extends Error {
  constructor(status, body, message) {
    super(message || (body && body.error && body.error.message) || ("HTTP " + status));
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

let fetchLog = [];
const globalFetchLog = [];
let behaviors = new Map();
let held = [];
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

// ------------------------------------------------ 被测代码（逐字切片自 app.js）
/*__SLICE_META__*/
/*__SLICE_DRAFT_SELECTOR__*/
/*__SLICE_OP_KEY__*/
/*__SLICE_EDIT_KEY__*/
/*__SLICE_RESOURCE_ARRAY__*/
/*__SLICE_UPSERT__*/
/*__SLICE_BUMP_EPOCH__*/
/*__SLICE_PENDING_DELETE__*/
/*__SLICE_NO_CHANGE__*/
/*__SLICE_UNCONFIRMED_ERROR__*/
/*__SLICE_DELETE_OUTCOME__*/
/*__SLICE_DOCUMENT_FROM_RESULT__*/
/*__SLICE_APPLY_SAVED__*/
/*__SLICE_PERSIST_OP__*/
/*__SLICE_SAVE_ALL__*/
/*__SLICE_REMOVE_CREATE__*/
/*__SLICE_QUEUE_DELETE__*/

// 说明：监听器的**挂载**是 harness 自写的（app.js 真实监听器在别的上下文里），
// 但 delete-group-resource 分支体是从 app.js 逐字切片进来的。
content.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
/*__SLICE_CLICK__*/
});

// ------------------------------------------------------------------ 场景工具
const OTHER_DRAFT_KEY = resourceEditDraftKey(GROUP_B, "rules", 3);

function ids(type) { return resourceArray(GROUP_A, type).map(item => String(item.id)); }
function rowOf(type, id) {
  return resourceArray(GROUP_A, type).find(item => String(item.id) === String(id)) || null;
}
function dkey(type, id) { return resourceOperationKey(GROUP_A, type, id); }
function ekey(type, id) { return resourceEditDraftKey(GROUP_A, type, id); }
function deleteUrl(type, id) {
  return "/api/v1/groups/" + encodeURIComponent(GROUP_A) + "/" + type + "/" + encodeURIComponent(id);
}
function requestsFor(url) { return fetchLog.filter(item => item.url === url); }
function methods() { return fetchLog.map(item => item.method + " " + item.url).join(" | "); }
function toastsWith(text) { return toasts.filter(item => item.message.indexOf(text) >= 0); }

function renderDeleteButton(type, id) {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.action = "delete-group-resource";
  button.dataset.groupId = GROUP_A;
  button.dataset.resourceType = type;
  button.dataset.resourceId = String(id);
  content.appendChild(button);
  return button;
}
function seedDraft(type, id, text) {
  state.resourceFormDrafts.set(ekey(type, id), { content: text });
  state.resourceFormBaselines.set(ekey(type, id), { content: "初始基线" });
}
function seedRows(type) {
  return [{ id: 1, marker: type + "-row-1" }, { id: 2, marker: type + "-row-2" }];
}
function resetHarness() {
  state = makeState();
  state.groups = [{ id: GROUP_A, title: "A 群" }, { id: GROUP_B, title: "B 群" }];
  state.config = { bot: { memory_facts_enabled: true }, groups: { max_groups: 3 }, list: [1, 2] };
  const a = {};
  const b = {};
  for (const type of TYPES) {
    a[RESOURCE_TYPE_META[type].property] = seedRows(type);
    b[RESOURCE_TYPE_META[type].property] = [];
  }
  state.groupResources.set(GROUP_A, a);
  state.groupResources.set(GROUP_B, b);
  state.resourceFormDrafts.set(OTHER_DRAFT_KEY, { content: "别的群的草稿" });
  state.resourceFormBaselines.set(OTHER_DRAFT_KEY, { content: "别的群的基线" });
  nextOperations = [];
  behaviors = new Map();
  fetchLog = [];
  held = [];
  toasts = [];
  content.innerHTML = "";
  panel.innerHTML = "";
  renderContent();
}
function collateralSnapshot() {
  return JSON.stringify({
    otherDraft: state.resourceFormDrafts.get(OTHER_DRAFT_KEY) || null,
    otherBaseline: state.resourceFormBaselines.get(OTHER_DRAFT_KEY) || null,
    config: state.config,
    otherPending: [...state.pendingResourceDeletes.keys()].filter(key => key.indexOf(GROUP_B) >= 0),
  });
}
function checkCollateral(label, before) {
  check(label + "：无关群草稿 / 基线与 config 深度未变", collateralSnapshot() === before, collateralSnapshot());
  check(label + "：没有任何请求指向无关群", globalFetchLog.every(item => item.url.indexOf(GROUP_B) < 0), globalFetchLog.map(item => item.url).join(","));
}
function checkDom(label, type) {
  const domIds = [...panel.querySelectorAll("[data-resource-row]")]
    .map(el => el.dataset.resourceRow)
    .filter(value => value.indexOf(type + ":") === 0)
    .map(value => value.slice(type.length + 1));
  check(
    label + "：DOM 行与本地列表一致",
    JSON.stringify(domIds) === JSON.stringify(ids(type)),
    domIds.join(",") + " vs " + ids(type).join(",")
  );
}
async function settle(rounds) {
  const total = rounds || 8;
  for (let i = 0; i < total; i += 1) { await new Promise(resolve => setTimeout(resolve, 0)); }
}

// -------------------------------------------------------------------- 场景
async function scenarioQueueOnly(type) {
  const label = type + " a 只排队";
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  await settle();
  check(label + "：标记删除零请求", fetchLog.length === 0, methods());
  check(label + "：待删除队列含该条目", state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：本地行未被删除", ids(type).indexOf("1") >= 0, ids(type).join(","));
  check(label + "：提示是「已标记删除」警告", toastsWith("已标记删除").length === 1 && toastsWith("已标记删除")[0].kind === "warning", JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function scenarioNormalDelete(type) {
  const label = type + " b 正常删除";
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  seedDraft(type, 1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: type, id: "1", kind: "delete" }];
  const url = deleteUrl(type, 1);
  behaviors.set("DELETE " + url, { mode: "ok", body: { ok: true, deleted: true, changed: true, status: "deleted" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check(label + "：恰好一次 DELETE", requestsFor(url).length === 1 && requestsFor(url)[0].method === "DELETE", methods());
  check(label + "：没有任何 GET/POST/PATCH", fetchLog.every(item => item.method === "DELETE"), methods());
  check(label + "：本地行已移除", ids(type).indexOf("1") < 0, ids(type).join(","));
  check(label + "：待删除条目已清", !state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(
    label + "：该行草稿与基线已清",
    !state.resourceFormDrafts.has(ekey(type, "1")) && !state.resourceFormBaselines.has(ekey(type, "1")),
    JSON.stringify([...state.resourceFormDrafts.keys()])
  );
  check(label + "：计为成功", toastsWith("已保存全部 1 项更改").length === 1 && toastsWith("已保存全部 1 项更改")[0].kind === "success", JSON.stringify(toasts));
  check(label + "：没有失败提示", !toasts.some(item => item.kind === "error"), JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function scenarioNoChange(type) {
  const label = type + " c no_change";
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  seedDraft(type, 1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: type, id: "1", kind: "delete" }];
  const url = deleteUrl(type, 1);
  behaviors.set("DELETE " + url, { mode: "ok", body: { ok: true, deleted: false, changed: false, status: "no_change" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  const warning = TYPE_LABEL[type] + "：此条未由本次操作删除，可能已在别处删除。";
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(url)[0].method === "DELETE", methods());
  check(label + "：本地行按不可用移除", ids(type).indexOf("1") < 0, ids(type).join(","));
  check(label + "：待删除条目已清", !state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：该行草稿与基线已清", !state.resourceFormDrafts.has(ekey(type, "1")) && !state.resourceFormBaselines.has(ekey(type, "1")), JSON.stringify([...state.resourceFormDrafts.keys()]));
  check(label + "：警告文案逐字出现", toastsWith(warning).length >= 1, JSON.stringify(toasts));
  check(label + "：不算成功", toastsWith("已保存全部 1 项更改").length === 0, JSON.stringify(toasts));
  check(label + "：只报无需改动", toastsWith("1 项记录已确认无需改动").length === 1, JSON.stringify(toasts));
  check(label + "：不报「永久记忆」文案", toastsWith("1 项永久记忆已确认无需改动").length === 0, JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function scenario503Unconfirmed(type) {
  const label = type + " d 503";
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  seedDraft(type, 1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: type, id: "1", kind: "delete" }];
  const url = deleteUrl(type, 1);
  behaviors.set("DELETE " + url, {
    mode: "apiError",
    status: 503,
    body: { ok: false, error: { code: "memory_write_unconfirmed", message: "未能确认这次群内资源操作是否已生效。请重新查看列表后再决定是否重试。" } },
  });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(url)[0].method === "DELETE", methods());
  check(label + "：待删除条目保留", state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：草稿保留", (state.resourceFormDrafts.get(ekey(type, "1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check(label + "：本地行保留", ids(type).indexOf("1") >= 0, ids(type).join(","));
  check(label + "：无成功提示", toastsWith("已保存全部").length === 0, JSON.stringify(toasts));
  check(label + "：失败提示出现", toasts.some(item => item.kind === "error" && item.message.indexOf("未能确认") >= 0), JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function runMalformedDelete(type, describe, body) {
  const label = type + " e " + describe;
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  seedDraft(type, 1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: type, id: "1", kind: "delete" }];
  const url = deleteUrl(type, 1);
  behaviors.set("DELETE " + url, { mode: "ok", body: body });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(url)[0].method === "DELETE", methods());
  check(label + "：草稿保留", (state.resourceFormDrafts.get(ekey(type, "1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check(label + "：待删除条目保留", state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：本地行保留", ids(type).indexOf("1") >= 0, ids(type).join(","));
  check(label + "：不算成功且出现失败提示", toastsWith("已保存全部").length === 0 && toasts.some(item => item.kind === "error"), JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function scenarioMalformed(type) {
  const judge = type + " e 判据";
  const missing = memoryDeleteOutcome({ ok: true, deleted: true });
  const contradictory = memoryDeleteOutcome({ ok: true, status: "deleted", deleted: true, changed: false });
  const acceptedDeleted = memoryDeleteOutcome({ ok: true, status: "deleted", deleted: true, changed: true });
  const acceptedNoChange = memoryDeleteOutcome({ ok: true, status: "no_change", deleted: false, changed: false });
  const nonBoolean = memoryDeleteOutcome({ ok: true, status: "deleted", deleted: "true", changed: true });
  check(judge + "：缺 status/changed → unconfirmed", missing.kind === "unconfirmed", missing.kind);
  check(judge + "：字段矛盾 → unconfirmed", contradictory.kind === "unconfirmed", contradictory.kind);
  check(judge + "：非布尔字段 → unconfirmed", nonBoolean.kind === "unconfirmed", nonBoolean.kind);
  check(judge + "：合法 deleted 判定为 deleted", acceptedDeleted.kind === "deleted", acceptedDeleted.kind);
  check(judge + "：合法 no_change 判定为 no_change", acceptedNoChange.kind === "no_change", acceptedNoChange.kind);
  await runMalformedDelete(type, "缺 status/changed", { ok: true, deleted: true });
  await runMalformedDelete(type, "字段矛盾", { ok: true, status: "deleted", deleted: true, changed: false });
}

async function scenarioNetworkDrop(type) {
  const label = type + " f 断网";
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(type, 1).click();
  seedDraft(type, 1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: type, id: "1", kind: "delete" }];
  const url = deleteUrl(type, 1);
  behaviors.set("DELETE " + url, { mode: "networkError", message: "Failed to fetch" });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(url)[0].method === "DELETE", methods());
  check(label + "：草稿保留", (state.resourceFormDrafts.get(ekey(type, "1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check(label + "：待删除条目保留", state.pendingResourceDeletes.has(dkey(type, "1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：本地行保留", ids(type).indexOf("1") >= 0, ids(type).join(","));
  check(label + "：无成功提示", toastsWith("已保存全部").length === 0, JSON.stringify(toasts));
  check(label + "：失败文案含原始错误", toasts.some(item => item.kind === "error" && item.message.indexOf("Failed to fetch") >= 0), JSON.stringify(toasts));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function scenarioPureRender(type) {
  const label = type + " h 纯渲染";
  resetHarness();
  const before = collateralSnapshot();
  renderContent();
  render();
  renderContent();
  await settle(2);
  check(label + "：零请求", fetchLog.length === 0, methods());
  check(label + "：DOM 渲染出两行", panel.querySelectorAll("[data-resource-row]").length === TYPES.length * 2, String(panel.querySelectorAll("[data-resource-row]").length));
  checkDom(label, type);
  checkCollateral(label, before);
}

async function main() {
  try {
    results.info.viewport = document.documentElement.clientWidth;
    results.info.innerWidth = window.innerWidth;
    results.info.expectedWidth = EXPECTED_WIDTH;
    results.info.types = TYPES.slice();
    for (const type of TYPES) {
      check(
        type + " meta：label 与类别对得上",
        RESOURCE_TYPE_META[type].label === TYPE_LABEL[type] && typeof RESOURCE_TYPE_META[type].category === "string",
        JSON.stringify(RESOURCE_TYPE_META[type])
      );
      await scenarioQueueOnly(type);
      await scenarioNormalDelete(type);
      await scenarioNoChange(type);
      await scenario503Unconfirmed(type);
      await scenarioMalformed(type);
      await scenarioNetworkDrop(type);
      await scenarioPureRender(type);
    }
    check("z 全程：没有任何请求指向无关群", globalFetchLog.every(item => item.url.indexOf(GROUP_B) < 0), globalFetchLog.map(item => item.url).join(","));
    check(
      "z 全程：所有请求都是本群 DELETE",
      globalFetchLog.every(item => item.method === "DELETE" && item.url.indexOf("/api/v1/groups/" + GROUP_A + "/") === 0),
      globalFetchLog.map(item => item.method + " " + item.url).join(" | ")
    );
    check("z 全程：真实请求数达到下限", globalFetchLog.length >= 6, String(globalFetchLog.length));
    results.info.totalRequests = globalFetchLog.length;
    results.info.globalRequestLog = globalFetchLog;
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


def _required_check_names() -> tuple[str, ...]:
    names: list[str] = []
    for type_ in SIBLING_TYPES:
        names.extend(
            [
                f"{type_} meta：label 与类别对得上",
                f"{type_} a 只排队：标记删除零请求",
                f"{type_} a 只排队：待删除队列含该条目",
                f"{type_} a 只排队：本地行未被删除",
                f"{type_} b 正常删除：恰好一次 DELETE",
                f"{type_} b 正常删除：待删除条目已清",
                f"{type_} b 正常删除：该行草稿与基线已清",
                f"{type_} b 正常删除：计为成功",
                f"{type_} c no_change：恰好一次 DELETE",
                f"{type_} c no_change：本地行按不可用移除",
                f"{type_} c no_change：警告文案逐字出现",
                f"{type_} c no_change：不算成功",
                f"{type_} c no_change：只报无需改动",
                f"{type_} d 503：待删除条目保留",
                f"{type_} d 503：草稿保留",
                f"{type_} d 503：失败提示出现",
                f"{type_} e 判据：缺 status/changed → unconfirmed",
                f"{type_} e 判据：字段矛盾 → unconfirmed",
                f"{type_} e 判据：合法 deleted 判定为 deleted",
                f"{type_} e 判据：合法 no_change 判定为 no_change",
                f"{type_} e 缺 status/changed：恰好一次 DELETE",
                f"{type_} e 缺 status/changed：本地行保留",
                f"{type_} e 字段矛盾：恰好一次 DELETE",
                f"{type_} e 字段矛盾：本地行保留",
                f"{type_} f 断网：待删除条目保留",
                f"{type_} f 断网：失败文案含原始错误",
                f"{type_} h 纯渲染：零请求",
                f"{type_} h 纯渲染：DOM 渲染出两行",
            ]
        )
    names.append("z 全程：没有任何请求指向无关群")
    names.append("z 全程：所有请求都是本群 DELETE")
    names.append("z 全程：真实请求数达到下限")
    return tuple(names)


class SiblingDeleteDomTests(unittest.TestCase):
    """在真实 Blink 里驱动逐字切片的 rules / keyword-replies / scheduled-messages 删除管线。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = _find_browser()
        cls.slices = _code_slices()
        cls.required_checks = _required_check_names()

    def _build_page(self, directory: pathlib.Path, width: int) -> pathlib.Path:
        page = _HARNESS_TEMPLATE
        for label, code in self.slices.items():
            token = f"/*__SLICE_{label.upper()}__*/"
            self.assertIn(token, page, f"harness 缺少切片占位符 {token}")
            page = page.replace(token, code)
        page = page.replace("/*__WIDTH__*/", str(width))
        page = page.replace("/*__GROUP_A__*/", json.dumps(GROUP_A))
        page = page.replace("/*__GROUP_B__*/", json.dumps(GROUP_B))
        page = page.replace("/*__TYPES__*/", json.dumps(list(SIBLING_TYPES)))
        page = page.replace("/*__TYPE_LABELS__*/", json.dumps(TYPE_LABELS, ensure_ascii=False))
        self.assertNotIn("/*__", page, "harness 还有未替换的占位符")
        path = directory / f"sibling-delete-{width}.html"
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

    def test_sibling_group_resource_delete_pipeline_in_a_real_browser(self) -> None:
        if not self.browser:
            self.fail(
                "真实 DOM 用例需要一个 Chromium/Chrome 可执行文件：请设置 OPERATIONS_BROWSER=<path> "
                "或安装 chromium（本用例刻意不 skip，静默跳过等于没验证）。"
            )

        per_width: list[tuple[int, dict]] = []
        with tempfile.TemporaryDirectory(prefix="miniapp-sibling-delete-") as raw_dir:
            directory = pathlib.Path(raw_dir)
            for width in WIDTHS:
                page = self._build_page(directory, width)
                per_width.append((width, self._run(self.browser, directory, page, width)))

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
            # 防退化：断言数量必须够多，场景确实都被执行了。
            self.assertGreaterEqual(
                len(payload["checks"]),
                90,
                f"{width}px 断言数量过少（{len(payload['checks'])}），场景可能没跑全",
            )
            names = {item["name"] for item in payload["checks"]}
            missing = [name for name in self.required_checks if name not in names]
            self.assertEqual(missing, [], f"{width}px 缺少必需场景断言：{missing}")

        # 每个宽度都必须真的发过若干次真实请求（证明场景不是空跑）。
        for width, payload in per_width:
            self.assertGreaterEqual(
                len(payload["info"]["globalRequestLog"]),
                6,
                f"{width}px 真实请求数过少：{payload['info']['globalRequestLog']}",
            )

        for width, payload in per_width:
            failed_count = len([item for item in payload["checks"] if not item["ok"]])
            print(
                f"[sibling-delete {width}px] checks={len(payload['checks'])} failed={failed_count} "
                f"harness_errors={len(payload['errors'])} viewport={payload['info']['viewport']} "
                f"requests={len(payload['info']['globalRequestLog'])}"
            )


if __name__ == "__main__":
    unittest.main()
