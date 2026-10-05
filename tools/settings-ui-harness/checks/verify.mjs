import { chromium } from 'playwright';
import fs from 'node:fs';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const SHOTS = process.env.SHOTS || '/tmp/dsh-ui-shots';
fs.mkdirSync(SHOTS, { recursive: true });

const VIEWPORTS = [
  { name: 'mobile-360', width: 360, height: 780 },
  { name: 'mobile-390', width: 390, height: 844 },
  { name: 'mobile-430', width: 430, height: 932 },
  { name: 'desktop-1280', width: 1280, height: 900 },
  { name: 'desktop-1680', width: 1680, height: 1000 },
];

const TABS = ['overview', 'models', 'prompts', 'bot', 'safety', 'media', 'integrations', 'groups', 'access', 'logging'];

const failures = [];
const notes = [];
function check(ok, message) {
  if (ok) notes.push(`  PASS  ${message}`);
  else failures.push(`  FAIL  ${message}`);
}

async function overflowReport(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const viewport = window.innerWidth;
    const offenders = [];
    const scrollParent = (el) => {
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) return n;
      }
      return null;
    };
    for (const el of document.querySelectorAll('body *')) {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || el.closest('[hidden]')) continue;
      if (scrollParent(el)) continue; // inside an intentional scroller (chip rails, prompt strip)
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      if (rect.right > viewport + 1 || rect.left < -1) {
        offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className || '').toString().slice(0, 80),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
        });
      }
    }
    return {
      scrollWidth: doc.scrollWidth,
      clientWidth: doc.clientWidth,
      viewport,
      bodyScrollWidth: document.body.scrollWidth,
      offenders: offenders.slice(0, 8),
    };
  });
}

async function chromeBounds(page) {
  return page.evaluate(() => {
    const vw = window.innerWidth;
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el || el.offsetParent === null) return null;
      const r = el.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
    };
    const title = document.querySelector('.topbar-title');
    const actions = document.querySelector('.topbar-actions');
    const a = actions?.getBoundingClientRect();
    const t = title?.getBoundingClientRect();
    return {
      vw,
      save: box('#save-button'),
      reload: box('#reload-button'),
      toggle: box('#sidebar-toggle'),
      title: t ? { right: Math.round(t.right) } : null,
      actions: a ? { left: Math.round(a.left), right: Math.round(a.right) } : null,
      overlap: Boolean(a && t && a.left < t.right - 0.5),
      actionsOffscreen: Boolean(a && (a.right > vw + 1 || a.left < -1)),
    };
  });
}

async function smallTargets(page) {
  return page.evaluate(() => {
    const out = [];
    const sel = 'button, [role="button"], a[href], input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, summary, .nav-button, .compact-check, label.toggle';
    for (const el of document.querySelectorAll(sel)) {
      if (el.offsetParent === null) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      // Real inputs can be visually shrunk only via a wrapping label; skip hidden ones.
      if (r.height < 43.5) {
        out.push({ tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 60), h: Math.round(r.height * 10) / 10, text: (el.textContent || '').trim().slice(0, 24) });
      }
    }
    return out;
  });
}

// Mobile navigation lives in the drawer (the .mobile-nav strip is display:none
// by design); desktop uses the sidebar list.
async function switchTab(page, tab) {
  const mobile = await page.evaluate(() => window.innerWidth <= 820);
  if (mobile) {
    const open = await page.evaluate(() => document.querySelector('.app-shell').classList.contains('sidebar-open'));
    if (!open) await page.click('#sidebar-toggle');
    await page.waitForSelector('.sidebar.mobile-open', { state: 'visible' });
    await page.click(`#desktop-nav [data-nav="${tab}"]`);
  } else {
    await page.click(`#desktop-nav [data-nav="${tab}"]`);
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });

for (const vp of VIEWPORTS) {
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: vp.width < 500 ? 2 : 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message)));
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  await page.waitForTimeout(250);

  console.log(`\n=== ${vp.name} (${vp.width}x${vp.height}) ===`);

  for (const tab of TABS) {
    await switchTab(page, tab);
    await page.waitForTimeout(320);
    const o = await overflowReport(page);
    const where = `[${vp.name}/${tab}]`;
    check(o.scrollWidth <= o.viewport + 1, `${where} no horizontal scroll (scrollWidth=${o.scrollWidth} vw=${o.viewport})`);
    if (o.offenders.length) console.log(`        offenders: ${JSON.stringify(o.offenders)}`);

    // Groups page: expand every group card and every section.
    if (tab === 'groups') {
      const cards = page.locator('[data-action="toggle-group-card"]');
      for (let i = 0; i < (await cards.count()); i += 1) {
        const card = cards.nth(i);
        if ((await card.getAttribute('aria-expanded')) !== 'true') await card.click();
        await page.waitForTimeout(220);
        const o2 = await overflowReport(page);
        check(o2.scrollWidth <= o2.viewport + 1, `${where} group card ${i} expanded: no horizontal scroll (${o2.scrollWidth})`);
        if (o2.offenders.length) console.log(`        offenders: ${JSON.stringify(o2.offenders)}`);
        const sections = page.locator('[data-group-card]').nth(i).locator('[data-group-settings-section]');
        for (let s = 0; s < (await sections.count()); s += 1) {
          const sec = sections.nth(s);
          if (!(await sec.evaluate((el) => el.open))) await sec.locator(':scope > summary').click();
          await page.waitForTimeout(160);
          const o3 = await overflowReport(page);
          check(o3.scrollWidth <= o3.viewport + 1, `${where} group ${i} section ${s} open: no horizontal scroll (${o3.scrollWidth})`);
          if (o3.offenders.length) console.log(`        offenders: ${JSON.stringify(o3.offenders)}`);
        }
        break; // one group is enough for layout; a second would triple the run
      }
    }

    // Open every advanced panel on this tab.
    const panels = page.locator('[data-advanced-panel]');
    const panelCount = await panels.count();
    for (let i = 0; i < panelCount; i += 1) {
      const panel = panels.nth(i);
      if (!(await panel.evaluate((el) => el.open))) await panel.locator(':scope > summary').click();
      await page.waitForTimeout(120);
    }
    if (panelCount) {
      await page.waitForTimeout(150);
      const o4 = await overflowReport(page);
      check(o4.scrollWidth <= o4.viewport + 1, `${where} all ${panelCount} advanced panels open: no horizontal scroll (${o4.scrollWidth})`);
      if (o4.offenders.length) console.log(`        offenders: ${JSON.stringify(o4.offenders)}`);
    }

    if (vp.width < 500) {
      const small = await smallTargets(page);
      check(small.length === 0, `${where} all touch targets >= 44px (${small.length} below)`);
      if (small.length) console.log(`        small: ${JSON.stringify(small.slice(0, 6))}`);
    }

    const c = await chromeBounds(page);
    check(!c.actionsOffscreen, `${where} topbar actions stay on screen (save right=${c.save?.right} vw=${c.vw})`);
    check(!c.overlap, `${where} topbar title and actions do not overlap`);
  }

  check(errors.length === 0, `${vp.name} no uncaught JS errors (${errors.length})`);
  if (errors.length) console.log(`        errors: ${JSON.stringify(errors.slice(0, 4))}`);
  await page.close();
}

// ---- Screenshots on the three required sizes -------------------------------
async function shot(width, height, tag, steps) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: width < 500 ? 2 : 1 });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  if (steps) await steps(page);
  await page.waitForTimeout(300);
  const file = `${SHOTS}/${tag}.png`;
  await page.screenshot({ path: file, fullPage: false });
  await page.close();
  console.log(`  shot  ${file}`);
  return file;
}

const openTab = (tab) => async (page) => { await switchTab(page, tab); await page.waitForTimeout(350); };

await shot(390, 844, 'mobile-390-bot-behavior', openTab('bot'));
await shot(390, 844, 'mobile-390-groups', async (page) => {
  await openTab('groups')(page);
  await page.waitForSelector('[data-action="toggle-group-card"]', { timeout: 20000 });
  const card = page.locator('[data-action="toggle-group-card"]').first();
  await card.click();
  await page.waitForTimeout(300);
  const sec = page.locator('[data-group-card]').first().locator('[data-group-settings-section]').first();
  if (sec.count()) { await sec.locator(':scope > summary').click(); await page.waitForTimeout(250); }
});
await shot(390, 844, 'mobile-390-models', openTab('models'));
await shot(1280, 900, 'desktop-1280-overview', openTab('overview'));

await browser.close();

console.log(`\n${notes.length} checks passed, ${failures.length} failed`);
for (const f of failures) console.log(f);
fs.writeFileSync(`${SHOTS}/report.json`, JSON.stringify({ passed: notes, failed: failures }, null, 1));
process.exit(failures.length ? 1 : 0);
