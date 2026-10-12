"""真实 DOM 用例：Mini App「保存全部」里**永久记忆（memories）**的写入语义。

静态字符串测试只能证明源码里出现了某些字符；它证明不了：

* 点击删除按钮**只排队**、一个请求都不发；
* 2xx 响应字段缺失 / 互相矛盾时**不当成删除成功**；
* 删除确认后本地行、待删除标记、编辑草稿、基线都被清干净；
* 失败（503 / 409 / 断网 / 文档不完整 / 矛盾字段）时**草稿与待删除标记都保留**；
* 一条失败**不会**触发重跑、也不会把编辑悄悄变成新建；
* 无关群草稿与未保存 config 在全程保持原样。

本模块把 ``bot/web/static/app.js`` 里的真实实现逐字切片（``RESOURCE_TYPE_META`` /
``resourceOperationKey`` / ``memoryDeleteOutcome`` / ``memoryDocumentFromResult`` /
``applySavedResourceOperation`` / ``persistResourceOperation`` / ``saveAllChanges`` /
``queueDeferredResourceDelete`` / 点击分支），在真实 Blink 内核（Chrome headless shell）
里用**真实 DOM**驱动，断言真实请求日志、真实 toast 文本与 harness 自己那份 ``state``。

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
        "meta": ['memories: { property: "memories"', 'response: "memory"', 'label: "永久记忆"'],
        "draft_selector": ["[data-memory-edit-form]", "[data-save-scope=\"deferred\"]"],
        "delete_outcome": [
            'status === "deleted" && deleted === true && changed === true',
            'status === "no_change" && deleted === false && changed === false',
            'typeof deleted !== "boolean"',
        ],
        "document_from_result": [
            'typeof document.content !== "string"',
            '"created_by" in document',
        ],
        "apply_saved": [
            "throw new MemoryWriteUnconfirmedError()",
            "state.pendingResourceDeletes.delete",
            "state.resourceFormDrafts.delete(editKey)",
            "RESOURCE_SAVE_NO_CHANGE",
        ],
        "persist_op": ['{ method: "DELETE" }', 'method: operation.kind === "update" ? "PATCH" : "POST"'],
        "save_all": [
            "noChangeCount += 1",
            "永久记忆：此条永久记忆未由本次操作删除，可能已在别处删除。",
            "successCount += 1",
            "已保存全部 ${successCount} 项更改",
            "${noChangeCount} 项永久记忆已确认无需改动",
        ],
        "queue_delete": [
            "state.pendingResourceDeletes.set(key",
            'showToast("已标记删除，点击“保存全部”后生效", "warning", 3600)',
        ],
        "click": ["queueDeferredResourceDelete(groupId, type, id)", "return;"],
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
<title>mini app memory write harness</title></head>
<body>
<div id="content"></div>
<div id="panel"></div>
<pre id="results">pending</pre>
<script>
"use strict";
const EXPECTED_WIDTH = /*__WIDTH__*/;
const results = { checks: [], info: {}, errors: [], toasts: [] };
const checks = [];
function check(name, ok, actual) {
  checks.push({ name: name, ok: Boolean(ok), actual: actual === undefined ? null : String(actual) });
}

const GROUP_A = /*__GROUP_A__*/;
const GROUP_B = /*__GROUP_B__*/;
const DELETE_1 = "/api/v1/groups/" + GROUP_A + "/memories/1";
const PATCH_2 = "/api/v1/groups/" + GROUP_A + "/memories/2";

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
function renderContent() {
  const list = resourceArray(GROUP_A, "memories");
  panel.innerHTML = list.map(item =>
    '<div data-memory-row="' + escapeHtml(item.id) + '"><span data-memory-content>' +
    escapeHtml(item.content) + "</span></div>").join("");
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

content.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
/*__SLICE_CLICK__*/
});

// ------------------------------------------------------------------ 场景工具
const OTHER_DRAFT_KEY = "memory:" + GROUP_B + ":3";

function ids() { return resourceArray(GROUP_A, "memories").map(item => String(item.id)); }
function contents() { return resourceArray(GROUP_A, "memories").map(item => String(item.content)); }
function row(id) { return resourceArray(GROUP_A, "memories").find(item => String(item.id) === String(id)) || null; }
function dkey(id) { return resourceOperationKey(GROUP_A, "memories", id); }
function ekey(id) { return resourceEditDraftKey(GROUP_A, "memories", id); }
function requestsFor(url) { return fetchLog.filter(item => item.url === url); }
function methods() { return fetchLog.map(item => item.method + " " + item.url).join(" | "); }
function toastsWith(text) { return toasts.filter(item => item.message.indexOf(text) >= 0); }

function renderDeleteButton(id) {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.action = "delete-group-resource";
  button.dataset.groupId = GROUP_A;
  button.dataset.resourceType = "memories";
  button.dataset.resourceId = String(id);
  content.appendChild(button);
  return button;
}
function seedDraft(id, text) {
  state.resourceFormDrafts.set(ekey(id), { content: text });
  state.resourceFormBaselines.set(ekey(id), { content: "初始基线" });
}
function resetHarness() {
  state = makeState();
  state.groups = [{ id: GROUP_A, title: "A 群" }, { id: GROUP_B, title: "B 群" }];
  state.config = { bot: { memory_facts_enabled: true }, groups: { max_groups: 3 }, list: [1, 2] };
  state.groupResources.set(GROUP_A, {
    memories: [{ id: 1, content: "第一条" }, { id: 2, content: "第二条" }],
    rules: [],
  });
  state.groupResources.set(GROUP_B, { memories: [], rules: [] });
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
function checkDom(label) {
  const domIds = [...panel.querySelectorAll("[data-memory-row]")].map(el => el.dataset.memoryRow);
  check(label + "：DOM 行与本地列表一致", JSON.stringify(domIds) === JSON.stringify(ids()), domIds.join(",") + " vs " + ids().join(","));
}
async function settle(rounds) {
  const total = rounds || 8;
  for (let i = 0; i < total; i += 1) { await new Promise(resolve => setTimeout(resolve, 0)); }
}

// -------------------------------------------------------------------- 场景
async function scenarioQueueOnly() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  await settle();
  check("a 只排队：标记删除零请求", fetchLog.length === 0, methods());
  check("a 只排队：待删除队列含 memory 1", state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check("a 只排队：本地行未被删除", ids().indexOf("1") >= 0, ids().join(","));
  check("a 只排队：提示是「已标记删除」警告", toastsWith("已标记删除").length === 1 && toastsWith("已标记删除")[0].kind === "warning", JSON.stringify(toasts));
  checkDom("a 只排队");
  checkCollateral("a 只排队", before);
}

async function scenarioNormalDelete() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "1", kind: "delete" }];
  behaviors.set("DELETE " + DELETE_1, { mode: "ok", body: { ok: true, deleted: true, changed: true, status: "deleted" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("b 正常删除：恰好一次 DELETE memories/1", requestsFor(DELETE_1).length === 1 && requestsFor(DELETE_1)[0].method === "DELETE", methods());
  check("b 正常删除：没有任何 GET/POST/PATCH", fetchLog.every(item => item.method === "DELETE"), methods());
  check("b 正常删除：本地列表不含 id 1", ids().indexOf("1") < 0, ids().join(","));
  check("b 正常删除：待删除条目已清", !state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check("b 正常删除：该行草稿与基线已清", !state.resourceFormDrafts.has(ekey("1")) && !state.resourceFormBaselines.has(ekey("1")), JSON.stringify([...state.resourceFormDrafts.keys()]));
  check("b 正常删除：计为成功", toastsWith("已保存全部 1 项更改").length === 1 && toastsWith("已保存全部 1 项更改")[0].kind === "success", JSON.stringify(toasts));
  check("b 正常删除：没有失败提示", !toasts.some(item => item.kind === "error"), JSON.stringify(toasts));
  checkDom("b 正常删除");
  checkCollateral("b 正常删除", before);
}

async function scenarioNoChange() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "1", kind: "delete" }];
  behaviors.set("DELETE " + DELETE_1, { mode: "ok", body: { ok: true, deleted: false, changed: false, status: "no_change" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("c no_change：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(DELETE_1)[0].method === "DELETE", methods());
  check("c no_change：本地行按不可用移除", ids().indexOf("1") < 0, ids().join(","));
  check("c no_change：待删除条目已清", !state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check("c no_change：该行草稿已清", !state.resourceFormDrafts.has(ekey("1")), JSON.stringify([...state.resourceFormDrafts.keys()]));
  check("c no_change：警告文案逐字出现", toastsWith("此条永久记忆未由本次操作删除，可能已在别处删除。").length >= 1, JSON.stringify(toasts));
  check("c no_change：不算成功（无「已保存全部 1 项更改」）", toastsWith("已保存全部 1 项更改").length === 0, JSON.stringify(toasts));
  check("c no_change：只报无需改动", toastsWith("1 项永久记忆已确认无需改动").length === 1, JSON.stringify(toasts));
  checkDom("c no_change");
  checkCollateral("c no_change", before);
}

async function scenario503Unconfirmed() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "1", kind: "delete" }];
  behaviors.set("DELETE " + DELETE_1, { mode: "apiError", status: 503, body: { ok: false, error: { code: "memory_write_unconfirmed", message: "未能确认这次永久记忆操作是否已生效。请重新查看列表后再决定是否重试。" } } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("d 503：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(DELETE_1)[0].method === "DELETE", methods());
  check("d 503：待删除条目保留", state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check("d 503：草稿保留", (state.resourceFormDrafts.get(ekey("1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check("d 503：本地行保留", ids().indexOf("1") >= 0, ids().join(","));
  check("d 503：无成功提示", toastsWith("已保存全部").length === 0, JSON.stringify(toasts));
  check("d 503：失败文案含「未能确认」", toasts.some(item => item.kind === "error" && item.message.indexOf("未能确认") >= 0), JSON.stringify(toasts));
  checkDom("d 503");
  checkCollateral("d 503", before);
}

async function scenario409UnavailableUpdate() {
  resetHarness();
  const before = collateralSnapshot();
  seedDraft(2, "用户改过的内容");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "2", kind: "update", sourceKey: ekey("2"), values: { content: "用户改过的内容" } }];
  behaviors.set("PATCH " + PATCH_2, { mode: "apiError", status: 409, body: { ok: false, error: { code: "memory_unavailable", message: "这条永久记忆已不可用，未确认本次修改。请重新查看列表。" } } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("e 409：恰好一次 PATCH memories/2", fetchLog.length === 1 && requestsFor(PATCH_2).length === 1 && requestsFor(PATCH_2)[0].method === "PATCH", methods());
  check("e 409：草稿保留用户文本", (state.resourceFormDrafts.get(ekey("2")) || {}).content === "用户改过的内容", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check("e 409：本地行未被移除且内容不变", ids().indexOf("2") >= 0 && (row("2") || {}).content === "第二条", ids().join(",") + " / " + contents().join(","));
  check("e 409：失败文案含「已不可用」", toasts.some(item => item.kind === "error" && item.message.indexOf("已不可用") >= 0), JSON.stringify(toasts));
  check("e 409：没有 POST 新建请求", fetchLog.every(item => item.method !== "POST"), methods());
  check("e 409：无成功提示", toastsWith("已保存全部").length === 0, JSON.stringify(toasts));
  checkDom("e 409");
  checkCollateral("e 409", before);
}

async function scenarioNetworkDrop() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "1", kind: "delete" }];
  behaviors.set("DELETE " + DELETE_1, { mode: "networkError", message: "Failed to fetch" });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("f 断网：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(DELETE_1)[0].method === "DELETE", methods());
  check("f 断网：草稿保留", (state.resourceFormDrafts.get(ekey("1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check("f 断网：待删除条目保留", state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check("f 断网：本地行保留", ids().indexOf("1") >= 0, ids().join(","));
  check("f 断网：无成功提示", toastsWith("已保存全部").length === 0, JSON.stringify(toasts));
  check("f 断网：失败文案含原始错误", toasts.some(item => item.kind === "error" && item.message.indexOf("Failed to fetch") >= 0), JSON.stringify(toasts));
  checkDom("f 断网");
  checkCollateral("f 断网", before);
}

async function runMalformedDelete(describe, body) {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(1, "编辑中的第一条");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "1", kind: "delete" }];
  behaviors.set("DELETE " + DELETE_1, { mode: "ok", body: body });
  await saveAllChanges({ skipPreview: true });
  await settle();
  const label = "g " + describe;
  check(label + "：恰好一次 DELETE", fetchLog.length === 1 && requestsFor(DELETE_1)[0].method === "DELETE", methods());
  check(label + "：草稿保留", (state.resourceFormDrafts.get(ekey("1")) || {}).content === "编辑中的第一条", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check(label + "：待删除条目保留", state.pendingResourceDeletes.has(dkey("1")), [...state.pendingResourceDeletes.keys()].join(","));
  check(label + "：本地行保留", ids().indexOf("1") >= 0, ids().join(","));
  check(label + "：不算成功且出现失败提示", toastsWith("已保存全部").length === 0 && toasts.some(item => item.kind === "error"), JSON.stringify(toasts));
  checkDom(label);
  checkCollateral(label, before);
}

async function scenarioMalformedTwice() {
  const missing = memoryDeleteOutcome({ ok: true, deleted: true });
  const contradictory = memoryDeleteOutcome({ ok: true, status: "deleted", deleted: true, changed: false });
  check("g 缺 status/changed 直接判定 unconfirmed", missing.kind === "unconfirmed", missing.kind);
  check("g 字段矛盾直接判定 unconfirmed", contradictory.kind === "unconfirmed", contradictory.kind);
  await runMalformedDelete("缺 status/changed", { ok: true, deleted: true });
  await runMalformedDelete("字段矛盾", { ok: true, status: "deleted", deleted: true, changed: false });
}

async function scenarioUpdateNormal() {
  resetHarness();
  const before = collateralSnapshot();
  seedDraft(2, "新内容");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "2", kind: "update", sourceKey: ekey("2"), values: { content: "新内容" } }];
  behaviors.set("PATCH " + PATCH_2, { mode: "ok", body: { ok: true, memory: { id: 2, content: "新内容", created_by: 7, created_at: "2020-01-01T00:00:00", updated_at: "2026-01-05T12:00:00" }, status: "updated" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("h 更新：恰好一次 PATCH memories/2", fetchLog.length === 1 && requestsFor(PATCH_2).length === 1 && requestsFor(PATCH_2)[0].method === "PATCH", methods());
  let parsed = null;
  try { parsed = JSON.parse(requestsFor(PATCH_2)[0].body); } catch (error) { parsed = null; }
  check("h 更新：请求体带草稿内容", Boolean(parsed) && parsed.content === "新内容", requestsFor(PATCH_2)[0] ? requestsFor(PATCH_2)[0].body : "no request");
  check("h 更新：草稿与基线已清", !state.resourceFormDrafts.has(ekey("2")) && !state.resourceFormBaselines.has(ekey("2")), JSON.stringify([...state.resourceFormDrafts.keys()]));
  check("h 更新：本地行内容已更新", (row("2") || {}).content === "新内容", contents().join(","));
  check("h 更新：返回文档被合并", (row("2") || {}).updated_at === "2026-01-05T12:00:00" && (row("2") || {}).created_by === 7, JSON.stringify(row("2")));
  check("h 更新：计为成功", toastsWith("已保存全部 1 项更改").length === 1, JSON.stringify(toasts));
  checkDom("h 更新");
  checkCollateral("h 更新", before);
}

async function scenarioIncompleteUpdateDocument() {
  resetHarness();
  const before = collateralSnapshot();
  const incomplete = memoryDocumentFromResult({ ok: true, status: "updated" });
  check("i 缺 memory 文档直接返回 null", incomplete === null, incomplete === null ? "null" : JSON.stringify(incomplete));
  const missingField = memoryDocumentFromResult({ ok: true, memory: { id: 2, content: "新内容", created_by: 7, created_at: "2020-01-01T00:00:00" }, status: "updated" });
  check("i 缺 updated_at 也返回 null", missingField === null, missingField === null ? "null" : JSON.stringify(missingField));
  seedDraft(2, "新内容");
  nextOperations = [{ groupId: GROUP_A, type: "memories", id: "2", kind: "update", sourceKey: ekey("2"), values: { content: "新内容" } }];
  behaviors.set("PATCH " + PATCH_2, { mode: "ok", body: { ok: true, status: "updated" } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("i 不完整文档：恰好一次 PATCH", fetchLog.length === 1 && requestsFor(PATCH_2)[0].method === "PATCH", methods());
  check("i 不完整文档：草稿保留", (state.resourceFormDrafts.get(ekey("2")) || {}).content === "新内容", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check("i 不完整文档：本地行内容不变", (row("2") || {}).content === "第二条", contents().join(","));
  check("i 不完整文档：不算成功且失败含未能确认", toastsWith("已保存全部").length === 0 && toasts.some(item => item.kind === "error" && item.message.indexOf("未能确认") >= 0), JSON.stringify(toasts));
  checkDom("i 不完整文档");
  checkCollateral("i 不完整文档", before);
}

async function scenarioPartialSuccessNoRerun() {
  resetHarness();
  const before = collateralSnapshot();
  renderDeleteButton(1).click();
  seedDraft(2, "新内容");
  nextOperations = [
    { groupId: GROUP_A, type: "memories", id: "1", kind: "delete" },
    { groupId: GROUP_A, type: "memories", id: "2", kind: "update", sourceKey: ekey("2"), values: { content: "新内容" } },
  ];
  behaviors.set("DELETE " + DELETE_1, { mode: "ok", body: { ok: true, deleted: true, changed: true, status: "deleted" } });
  behaviors.set("PATCH " + PATCH_2, { mode: "apiError", status: 500, body: { ok: false, error: { message: "服务端保存失败" } } });
  await saveAllChanges({ skipPreview: true });
  await settle();
  check("j 部分成功：每个 URL 恰好请求一次", requestsFor(DELETE_1).length === 1 && requestsFor(PATCH_2).length === 1 && fetchLog.length === 2, methods());
  check("j 部分成功：删除项已移除", ids().indexOf("1") < 0, ids().join(","));
  check("j 部分成功：更新草稿保留", (state.resourceFormDrafts.get(ekey("2")) || {}).content === "新内容", JSON.stringify([...state.resourceFormDrafts.entries()]));
  check("j 部分成功：报告一项成功一项失败", toasts.some(item => item.kind === "error" && item.message.indexOf("已保存 1 项") >= 0 && item.message.indexOf("1 项失败并已保留草稿") >= 0), JSON.stringify(toasts));
  check("j 部分成功：没有重跑（无第三次请求）", fetchLog.length === 2, methods());
  checkDom("j 部分成功");
  checkCollateral("j 部分成功", before);
}

async function scenarioPureRenderNoRequest() {
  resetHarness();
  const before = collateralSnapshot();
  const beforeFetch = fetchLog.length;
  renderContent();
  render();
  renderContent();
  await settle(2);
  check("l 纯渲染：零请求", fetchLog.length === 0 && beforeFetch === 0, methods());
  check("l 纯渲染：DOM 渲染出两行", panel.querySelectorAll("[data-memory-row]").length === 2, panel.textContent);
  checkDom("l 纯渲染");
  checkCollateral("l 纯渲染", before);
}

async function main() {
  try {
    results.info.viewport = document.documentElement.clientWidth;
    results.info.innerWidth = window.innerWidth;
    results.info.expectedWidth = EXPECTED_WIDTH;
    await scenarioQueueOnly();
    await scenarioNormalDelete();
    await scenarioNoChange();
    await scenario503Unconfirmed();
    await scenario409UnavailableUpdate();
    await scenarioNetworkDrop();
    await scenarioMalformedTwice();
    await scenarioUpdateNormal();
    await scenarioIncompleteUpdateDocument();
    await scenarioPartialSuccessNoRerun();
    await scenarioPureRenderNoRequest();
    check("k 全程：没有任何请求指向无关群", globalFetchLog.every(item => item.url.indexOf(GROUP_B) < 0), globalFetchLog.map(item => item.url).join(","));
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


class MiniAppMemoryWriteDomTests(unittest.TestCase):
    """在真实 Blink 里驱动逐字切片的永久记忆写入管线（真实 DOM + 真实请求日志）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = _find_browser()
        cls.slices = _code_slices()

    def _build_page(self, directory: pathlib.Path, width: int) -> pathlib.Path:
        page = _HARNESS_TEMPLATE
        for label, code in self.slices.items():
            token = f"/*__SLICE_{label.upper()}__*/"
            self.assertIn(token, page, f"harness 缺少切片占位符 {token}")
            page = page.replace(token, code)
        page = page.replace("/*__WIDTH__*/", str(width))
        page = page.replace("/*__GROUP_A__*/", json.dumps(GROUP_A))
        page = page.replace("/*__GROUP_B__*/", json.dumps(GROUP_B))
        self.assertNotIn("/*__", page, "harness 还有未替换的占位符")
        path = directory / f"memory-write-{width}.html"
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

    def test_miniapp_memory_write_pipeline_in_a_real_browser(self) -> None:
        if not self.browser:
            self.fail(
                "真实 DOM 用例需要一个 Chromium/Chrome 可执行文件：请设置 OPERATIONS_BROWSER=<path> "
                "或安装 chromium（本用例刻意不 skip，静默跳过等于没验证）。"
            )

        per_width: list[tuple[int, dict]] = []
        with tempfile.TemporaryDirectory(prefix="miniapp-memory-write-") as raw_dir:
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
                len(payload["checks"]), 90, f"{width}px 断言数量过少（{len(payload['checks'])}），场景可能没跑全"
            )
            names = {item["name"] for item in payload["checks"]}
            for required in (
                "a 只排队：标记删除零请求",
                "b 正常删除：恰好一次 DELETE memories/1",
                "c no_change：不算成功（无「已保存全部 1 项更改」）",
                "d 503：失败文案含「未能确认」",
                "e 409：没有 POST 新建请求",
                "f 断网：待删除条目保留",
                "g 缺 status/changed 直接判定 unconfirmed",
                "g 字段矛盾直接判定 unconfirmed",
                "h 更新：本地行内容已更新",
                "i 不完整文档：本地行内容不变",
                "j 部分成功：没有重跑（无第三次请求）",
                "k 全程：没有任何请求指向无关群",
                "l 纯渲染：零请求",
            ):
                self.assertIn(required, names, f"{width}px 缺少场景断言：{required}")

        # 三个宽度都必须真的有一轮真实请求日志（证明场景不是空跑）。
        for width, payload in per_width:
            self.assertGreaterEqual(
                len(payload["info"]["globalRequestLog"]),
                8,
                f"{width}px 全程请求数过少：{payload['info']['globalRequestLog']}",
            )


if __name__ == "__main__":
    unittest.main()
