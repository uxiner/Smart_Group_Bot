import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const failures = [];
const notes = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

const browser = await chromium.launch({ channel: 'chrome', headless: true });

async function resetHarness() {
  const res = await fetch(`${BASE}/harness/reset`, { method: 'POST' });
  if (!res.ok) throw new Error(`harness reset failed: ${res.status}`);
}

async function newPage({ width = 1280, height = 900, mobile = false } = {}) {
  await resetHarness();
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message)));
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  page.__errors = errors;
  return page;
}

async function switchTab(page, tab) {
  const mobile = await page.evaluate(() => window.innerWidth <= 820);
  if (mobile) {
    const open = await page.evaluate(() => document.querySelector('.app-shell').classList.contains('sidebar-open'));
    if (!open) await page.click('#sidebar-toggle');
    await page.waitForSelector('.sidebar.mobile-open', { state: 'visible' });
  }
  await page.click(`#desktop-nav [data-nav="${tab}"]`);
}

// ---------------------------------------------------------------- 1. dirty tracking
{
  const page = await newPage();
  await switchTab(page, 'bot');
  check(await page.locator('#save-button').isDisabled(), 'save button starts disabled');
  await page.fill('#field-bot-inbound_debounce_seconds', '1.5');
  await page.waitForTimeout(200);
  check(!(await page.locator('#save-button').isDisabled()), 'save button enables after an edit');
  const stateText = await page.textContent('#save-state');
  check(/待保存|未保存|\d+ 项/.test(stateText || ''), `save state announces the pending edits ("${stateText}")`);

  // draft survives a tab round trip
  await switchTab(page, 'safety');
  await page.waitForTimeout(200);
  await switchTab(page, 'bot');
  await page.waitForTimeout(300);
  check((await page.inputValue('#field-bot-inbound_debounce_seconds')) === '1.5', 'draft survives switching tabs');
  check(!(await page.locator('#save-button').isDisabled()), 'save button still enabled after the tab round trip');

  // advanced panel state survives a tab round trip
  const panel = page.locator('[data-advanced-panel="bot.streaming"]');
  await panel.locator(':scope > summary').click();
  await page.waitForTimeout(200);
  check(await panel.evaluate((el) => el.open), 'advanced panel opens');
  await switchTab(page, 'safety');
  await page.waitForTimeout(200);
  await switchTab(page, 'bot');
  await page.waitForTimeout(300);
  check(await page.locator('[data-advanced-panel="bot.streaming"]').evaluate((el) => el.open), 'advanced panel stays open after a tab round trip');
  check(page.__errors.length === 0, `no uncaught errors while editing (${page.__errors.length})`);
  await page.close();
}

// ---------------------------------------------------------------- 2. validation reveals a collapsed field
{
  const page = await newPage();
  await switchTab(page, 'bot');
  const target = '#field-bot-group_summary_trigger_messages';
  check(!(await page.locator(target).isVisible()), 'an advanced field is hidden before the panel is opened');
  // type into the still-collapsed field so the save path has to reveal it
  await page.locator(target).evaluate((el) => {
    el.value = '';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.waitForTimeout(200);
  check(!(await page.locator('#save-button').isDisabled()), 'editing a collapsed field marks the page dirty');
  // required + blank => the page must open the panel and point at the field
  await page.click('#save-button');
  await page.waitForTimeout(700);
  check(await page.locator('[data-advanced-panel="bot.summary"]').evaluate((el) => el.open), 'a failed save opens the advanced panel that owns the invalid field');
  check(await page.locator(target).isVisible(), 'the invalid field is visible after the failed save');
  const stillDirty = !(await page.locator('#save-button').isDisabled());
  check(stillDirty, 'the invalid edit is not discarded');
  await page.close();
}

// ---------------------------------------------------------------- 3. successful save
{
  const page = await newPage();
  await switchTab(page, 'bot');
  await page.fill('#field-bot-inbound_debounce_seconds', '2.5');
  await page.waitForTimeout(150);
  await page.click('#save-button');
  await page.waitForTimeout(900);
  check(await page.locator('#save-button').isDisabled(), 'save button is disabled again after a successful save');
  check(/已保存/.test((await page.textContent('#save-state')) || ''), `save state reports success ("${await page.textContent('#save-state')}")`);
  const toast = await page.locator('.toast').count();
  check(toast >= 1, 'a success toast is shown');
  await page.close();
}

// ---------------------------------------------------------------- 4. failed save keeps drafts
if (process.env.FAIL_HARNESS) {
  // Second harness instance started with --fail-save (see the harness README).
  const page = await (async () => {
    const p2 = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    p2.on('pageerror', (e) => p2.__errors.push(String(e.message)));
    p2.__errors = [];
    await p2.goto(`${process.env.FAIL_HARNESS}/settings`, { waitUntil: 'networkidle' });
    await p2.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
    return p2;
  })();
  await switchTab(page, 'bot');
  await page.fill('#field-bot-inbound_debounce_seconds', '3.5');
  await page.waitForTimeout(150);
  await page.click('#save-button');
  await page.waitForTimeout(900);
  const errorToast = await page.locator('.toast.error').count();
  check(errorToast >= 1, 'a failure toast is shown');
  check((await page.inputValue('#field-bot-inbound_debounce_seconds')) === '3.5', 'the draft survives a failed save');
  check(!(await page.locator('#save-button').isDisabled()), 'the save button stays enabled after a failed save');
  await page.close();
}

// ---------------------------------------------------------------- 5. confirm dialog focus + ESC
{
  const page = await newPage();
  await switchTab(page, 'groups');
  await page.waitForSelector('[data-action="toggle-group-card"]');
  await page.click('[data-action="toggle-group-card"]');
  await page.waitForTimeout(300);
  // the reload-groups action asks for confirmation when drafts exist
  const groupToggle = page.locator('[data-group-key="mute_all_replies"]').first();
  await groupToggle.locator('xpath=ancestor::label').click().catch(async () => { await groupToggle.click(); });
  await page.waitForTimeout(200);
  check(!(await page.locator('#save-button').isDisabled()), 'a group edit marks the page dirty');
  await page.click('[data-action="reload-groups"]');
  await page.waitForTimeout(400);
  const open = await page.evaluate(() => document.getElementById('confirm-dialog').open);
  check(open, 'the confirm dialog opens for a destructive action');
  if (open) {
    const focusInside = await page.evaluate(() => document.getElementById('confirm-dialog').contains(document.activeElement));
    check(focusInside, 'focus moves into the confirm dialog');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    check(!(await page.evaluate(() => document.getElementById('confirm-dialog').open)), 'Escape closes the confirm dialog');
  }
  await page.close();
}

// ---------------------------------------------------------------- 6. drawer focus trap + ESC
{
  const page = await newPage({ width: 390, height: 844, mobile: true });
  await page.click('#sidebar-toggle');
  await page.waitForSelector('.sidebar.mobile-open', { state: 'visible' });
  await page.waitForTimeout(300);
  const inside = await page.evaluate(() => document.querySelector('.sidebar').contains(document.activeElement));
  check(inside, 'opening the drawer moves focus into it');
  const inert = await page.evaluate(() => document.getElementById('content').inert);
  check(inert, 'the page behind the drawer is inert while it is open');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  check(!(await page.evaluate(() => document.querySelector('.app-shell').classList.contains('sidebar-open'))), 'Escape closes the drawer');
  check(await page.evaluate(() => document.activeElement?.id === 'sidebar-toggle'), 'focus returns to the drawer toggle');
  await page.close();
}

// ---------------------------------------------------------------- 7. reduced motion
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]');
  const duration = await page.evaluate(() => {
    const el = document.querySelector('.nav-button');
    return getComputedStyle(el).transitionDuration;
  });
  check(/^(0s|1e-05s|0\.00001s)$/.test(duration), `transitions are disabled under prefers-reduced-motion (${duration})`);
  await page.close();
}

// ---------------------------------------------------------------- 8. keyboard focus is visible
{
  const page = await newPage();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  const ring = await page.evaluate(() => {
    const el = document.activeElement;
    const s = getComputedStyle(el);
    return { width: s.outlineWidth, style: s.outlineStyle, color: s.outlineColor, tag: el.tagName };
  });
  check(parseFloat(ring.width) >= 2 && ring.style !== 'none', `keyboard focus draws a >=2px outline (${JSON.stringify(ring)})`);
  await page.close();
}

await browser.close();

console.log(notes.join('\n'));
if (failures.length) console.log(failures.join('\n'));
console.log(`\n${notes.length} interaction checks passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
