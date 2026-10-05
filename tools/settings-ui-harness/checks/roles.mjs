import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const notes = [];
const failures = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

const browser = await chromium.launch({ channel: 'chrome', headless: true });

// --- 1. group-admin session: no global pages, no global config call ---------
if (process.env.GROUP_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const apiCalls = [];
  page.on('request', (r) => { if (r.url().includes('/api/v1/settings')) apiCalls.push(r.url()); });
  await page.goto(`${process.env.GROUP_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  await page.waitForTimeout(400);
  check((await page.textContent('#page-title')) === '群组设置', 'a group admin lands on the groups page');
  const navIds = await page.evaluate(() => [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
  check(navIds.length === 1 && navIds[0] === 'groups', `only the groups page is reachable (${JSON.stringify(navIds)})`);
  check(apiCalls.length === 0, 'no request is made to the global settings API for a group admin');
  check((await page.textContent('#save-state')).includes('全部已保存'), 'the group-admin save state renders');
  await page.close();
}

// --- 2. error state (needs a harness started with --no-groups) --------------
if (process.env.ERROR_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${process.env.ERROR_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  await page.click('#desktop-nav [data-nav="groups"]');
  await page.waitForTimeout(500);
  check(await page.locator('.error-state').isVisible(), 'a failed group list renders the error state');
  check((await page.textContent('.error-state')).includes('模拟群组列表加载失败'), 'the error state carries the server message');
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: window.innerWidth }));
  check(o.sw <= o.vw + 1, `error state does not overflow (${o.sw} vs ${o.vw})`);
  await page.close();
}

// --- 3. contrast of the main text roles on the dark surfaces ---------------
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  for (const tab of ['overview', 'safety', 'groups', 'access', 'logging', 'prompts', 'models', 'bot']) {
    await page.click(`#desktop-nav [data-nav="${tab}"]`);
    await page.waitForTimeout(300);
  }
  const results = await page.evaluate(() => {
    const lum = (rgb) => {
      const [r, g, b] = rgb.map((v) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const parse = (value) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const bgOf = (el) => {
      let node = el;
      while (node && node !== document.documentElement) {
        const bg = getComputedStyle(node).backgroundColor;
        const parts = (bg.match(/[\d.]+/g) || []).map(Number);
        if (parts.length >= 3 && (parts.length < 4 || parts[3] > 0.85)) return parse(bg);
        node = node.parentElement;
      }
      return [12, 18, 32];
    };
    const ratio = (a, b) => {
      const [l1, l2] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (l1 + 0.05) / (l2 + 0.05);
    };
    const samples = [
      ['.page-head h2', 'page title'],
      ['.page-head p', 'page description'],
      ['.section-heading h3', 'section title'],
      ['.field-label', 'field label'],
      ['.field-hint', 'field hint'],
      ['.nav-button', 'nav item'],
      ['.nav-button.active', 'active nav item'],
      ['.save-state', 'save state'],
      ['.badge', 'badge'],
      ['.metric span', 'metric label'],
      ['.notice', 'notice'],
      ['.field input', 'input text'],
      ['.field input::placeholder', 'placeholder'],
    ];
    const out = [];
    for (const [selector, label] of samples) {
      const el = document.querySelector(selector);
      if (!el) continue;
      let style = getComputedStyle(el);
      if (selector.includes('::placeholder')) {
        const host = document.querySelector('.field input');
        if (!host) continue;
        const probe = document.createElement('span');
        probe.style.color = getComputedStyle(host, '::placeholder').color;
        probe.textContent = 'x';
        host.appendChild(probe);
        style = getComputedStyle(probe);
        probe.remove();
        out.push({ label, ratio: Number(ratio(parse(style.color), bgOf(host)).toFixed(2)) });
        continue;
      }
      out.push({ label, ratio: Number(ratio(parse(style.color), bgOf(el)).toFixed(2)) });
    }
    return out;
  });
  const placeholder = await page.evaluate(() => {
    const host = [...document.querySelectorAll('.field input')].find((el) => el.placeholder);
    if (!host) return null;
    const lum = (rgb) => { const [r, g, b] = rgb.map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const parse = (v) => (v.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    let node = host, bg = [27, 41, 64];
    while (node && node !== document.documentElement) {
      const parts = (getComputedStyle(node).backgroundColor.match(/[\d.]+/g) || []).map(Number);
      if (parts.length >= 3 && (parts.length < 4 || parts[3] > 0.85)) { bg = parse(getComputedStyle(node).backgroundColor); break; }
      node = node.parentElement;
    }
    const fg = parse(getComputedStyle(host, '::placeholder').color);
    const [l1, l2] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return Number(((l1 + 0.05) / (l2 + 0.05)).toFixed(2));
  });
  if (placeholder != null) check(placeholder >= 4.5, `input placeholder contrast ${placeholder}:1 (needs >= 4.5)`);
  for (const item of results) {
    // 3.0 for large text, 4.5 for body copy; the UI has no large-text exception.
    check(item.ratio >= 4.5, `${item.label} contrast ${item.ratio}:1 (needs >= 4.5)`);
  }
  await page.close();
}

await browser.close();
console.log(notes.join('\n'));
if (failures.length) console.log(failures.join('\n'));
console.log(`\n${notes.length} role/state/contrast checks passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
