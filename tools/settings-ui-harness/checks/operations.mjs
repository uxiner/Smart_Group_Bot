// 运营参数页的验收：移动端不横溢、键盘可达、44px 触达、重启标记、保存冲突。
//
// 跑法（见 tools/settings-ui-harness/README.md）：
//   node $CHECKS/operations.mjs
//   GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/operations.mjs
//
// 这个脚本**不**替代 verify/interact/roles，它只盯本轮新增的那一页。

import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const notes = [];
const failures = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

const browser = await chromium.launch({ channel: 'chrome', headless: true });

// --- 1. group admin: the operations page must not even be reachable --------
if (process.env.GROUP_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const globalCalls = [];
  page.on('request', (r) => { if (r.url().includes('/api/v1/settings')) globalCalls.push(r.url()); });
  await page.goto(`${process.env.GROUP_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  const navIds = await page.evaluate(() => [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
  check(!navIds.includes('operations'), 'the operations page is not offered to a group admin');
  check(globalCalls.length === 0, 'a group admin never calls the global settings API');
  await page.close();
}

// --- 2. super admin: the page exists and behaves ----------------------------
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  const navIds = await page.evaluate(() => [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
  check(navIds.includes('operations'), 'the operations page is reachable for the super admin');

  await page.click('#desktop-nav [data-nav="operations"]');
  await page.waitForSelector('#field-economy-tag_price_7d', { timeout: 10000 });
  check(await page.locator('#field-economy-tag_price_7d').isVisible(), 'the tag price field renders');
  check(await page.locator('#field-moderation-log_channel_id').count() === 0, 'the channel id stays on the safety page, not here');

  // Units and bounds are spelled out in the hint text.
  const hint = await page.textContent('#field-economy-tag_price_7d ~ .field-hint, .field:has(#field-economy-tag_price_7d) .field-hint');
  check(/分|积分|天|字符|秒|token/.test(hint || ''), `unit is visible on the price field (${(hint || '').slice(0, 40)})`);

  // Restart-marked inputs carry the badge.
  await page.click('#desktop-nav [data-nav="operations"]');
  await page.evaluate(() => {
    const panel = document.querySelector('details[data-advanced-panel="resources.all"]');
    if (panel) panel.open = true;
  });
  await page.waitForTimeout(200);
  const restartBadges = await page.locator('.field-label-row .badge.warning').count();
  check(restartBadges > 0, `restart-marked fields are badged (${restartBadges})`);

  // The lottery editor is a structured table, not a free-form JSON textarea.
  const editor = page.locator('[data-reward-editor="economy.lottery_prizes"]');
  check(await editor.count() === 1, 'the lottery table uses a structured editor');
  check(await editor.locator('textarea').count() === 0, 'the lottery table has no free-form JSON box');
  const rowsBefore = await editor.locator('.reward-row').count();
  await editor.locator('[data-reward-add]').click();
  await page.waitForTimeout(300);
  const rowsAfter = await page.locator('[data-reward-editor="economy.lottery_prizes"] .reward-row').count();
  check(rowsAfter === rowsBefore + 1, `adding a prize row works (${rowsBefore} -> ${rowsAfter})`);
  const summary = await page.textContent('[data-reward-editor="economy.lottery_prizes"] .reward-editor-head');
  check(/总权重/.test(summary || ''), 'the editor shows the derived total weight');
  check(/期望/.test(summary || ''), 'the editor shows the expected value');

  await page.close();
}

// --- 3. mobile: no horizontal overflow, 44px targets, keyboard focus --------
for (const width of [360, 390, 430]) {
  const page = await browser.newPage({ viewport: { width, height: 844 }, deviceScaleFactor: 2 });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  await page.click('#drawer-toggle').catch(() => {});
  await page.click('#desktop-nav [data-nav="operations"]').catch(() => {});
  await page.waitForSelector('#field-economy-tag_price_7d', { timeout: 10000 });
  await page.waitForTimeout(300);

  const overflow = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    vw: window.innerWidth,
  }));
  check(overflow.sw <= overflow.vw + 1, `${width}px: no horizontal overflow (${overflow.sw} vs ${overflow.vw})`);

  const small = await page.evaluate(() => {
    const nodes = [...document.querySelectorAll('.reward-row input, .reward-row .icon-button, .reward-editor-head .secondary-button')];
    return nodes
      .map((el) => ({ id: el.id || el.className, h: Math.round(el.getBoundingClientRect().height), w: Math.round(el.getBoundingClientRect().width) }))
      .filter((box) => box.h < 44 || box.w < 44);
  });
  check(small.length === 0, `${width}px: touch targets are at least 44px (${JSON.stringify(small.slice(0, 3))})`);

  // Keyboard focus must be visible on the reward inputs.
  await page.focus('[data-reward-editor="economy.lottery_prizes"] input');
  const outline = await page.evaluate(() => {
    const el = document.activeElement;
    const style = getComputedStyle(el);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  check(outline.style !== 'none' && parseFloat(outline.width) > 0, `${width}px: focused input has a visible outline (${JSON.stringify(outline)})`);

  await page.close();
}

// --- 4. same-click blur must not swallow the save -------------------------
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  await page.click('#desktop-nav [data-nav="operations"]');
  await page.waitForSelector('#field-economy-tag_price_7d', { timeout: 10000 });
  const before = await page.inputValue('#field-economy-tag_price_7d');
  await page.fill('#field-economy-tag_price_7d', String(Number(before) + 1));
  // The very next real click lands on Save; focus moving away must not lose the edit.
  await page.click('#save-all');
  await page.waitForTimeout(600);
  const toast = await page.textContent('#toast-region').catch(() => '');
  check(!/失败/.test(toast || ''), `the edit survives the blur before saving (${(toast || '').slice(0, 60)})`);
  const after = await page.inputValue('#field-economy-tag_price_7d');
  check(after === String(Number(before) + 1), `the saved value stays in the form (${before} -> ${after})`);
  await page.close();
}

await browser.close();
console.log(notes.join('\n'));
if (failures.length) {
  console.log(failures.join('\n'));
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log(`\nall ${notes.length} checks passed`);
