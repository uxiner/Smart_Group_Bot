// Repro for the save-button click regression fixed in this branch.
//
// Before the fix, editing any field and then clicking 保存全部 with a real mouse
// did nothing: the blur fired `change` -> updatePathControl -> updateChrome(),
// which rewrote saveButton.innerHTML while the pointer was already down, so the
// browser never delivered a click. This script prints the observable outcome,
// so the same file can be pointed at a checkout of the old baseline to show the
// difference (see UI-DELIVERY.md §6).
//
//   node tools/settings-ui-harness/checks/save-click-probe.mjs 8781
//
// Expect on this branch:  toasts: [ '已保存全部 1 项更改' ]
//                         save-state: 全部已保存 · 修订 13
import { chromium } from 'playwright';

const port = process.argv[2] || '8781';
const b = await chromium.launch({ channel: 'chrome', headless: true });
const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
p.on('console', (m) => console.log('C:', m.text().slice(0, 200)));
await fetch(`http://127.0.0.1:${port}/harness/reset`, { method: 'POST' });
await p.goto(`http://127.0.0.1:${port}/settings`, { waitUntil: 'networkidle' });
await p.waitForSelector('#app[aria-busy="false"]');
await p.click('#desktop-nav [data-nav="bot"]');
await p.waitForTimeout(500);
await p.click('#field-bot-inbound_debounce_seconds');
await p.fill('#field-bot-inbound_debounce_seconds', '2.5');
await p.waitForTimeout(400);
console.log('dirty?', await p.locator('#save-state').textContent());
await p.click('#save-button');
await p.waitForTimeout(1800);
console.log('toasts:', await p.locator('.toast').allTextContents());
console.log('save-state:', await p.locator('#save-state').textContent());
await b.close();
