// Verifies syncTelegramSafeArea() against the *real* Telegram WebApp shapes
// (Bot API 8.0+), by driving the harness stub with object-shaped insets and
// firing the documented events, then reading computed styles.
//
//   safeAreaInset        -> { top, bottom, left, right }
//   contentSafeAreaInset -> { top, bottom, left, right }
//   viewportHeight / viewportStableHeight -> numbers
//
// Covers: per-edge max(device, content) / zeroing on the next event /
// viewport height following the SDK / an old SDK without the fields.
//
// Two independent read-backs per edge, because getPropertyValue() on an
// unregistered custom property returns the *unresolved* `max(…)` token stream:
//   inline -> the px string app.js actually wrote
//   probe  -> a real element whose padding uses --safe-<edge>, read back in px
import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const notes = [];
const failures = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

// Serialized into the page, so it may not close over anything from this scope.
const readSafeArea = () => {
  let probe = document.getElementById('safe-area-probe');
  if (!probe) {
    probe = document.createElement('div');
    probe.id = 'safe-area-probe';
    probe.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;'
      + 'padding:var(--safe-top) var(--safe-right) var(--safe-bottom) var(--safe-left)';
    document.body.appendChild(probe);
  }
  const probeStyle = getComputedStyle(probe);
  const root = document.documentElement;
  return {
    inline: Object.fromEntries(
      ['top', 'bottom', 'left', 'right'].map((edge) => [edge, root.style.getPropertyValue(`--tg-safe-${edge}`)]),
    ),
    probe: {
      top: probeStyle.paddingTop,
      right: probeStyle.paddingRight,
      bottom: probeStyle.paddingBottom,
      left: probeStyle.paddingLeft,
    },
    // Real consumers, so we prove the values are used and not just declared.
    contentPaddingLeft: getComputedStyle(document.getElementById('content')).paddingLeft,
    contentPaddingRight: getComputedStyle(document.getElementById('content')).paddingRight,
    toastBottom: getComputedStyle(document.getElementById('toast-region')).bottom,
    topbarPaddingTop: getComputedStyle(document.querySelector('.topbar')).paddingTop,
    appShellMinHeight: getComputedStyle(document.getElementById('app')).minHeight,
    viewportHeightVar: root.style.getPropertyValue('--tg-viewport-height'),
    stableHeightVar: root.style.getPropertyValue('--tg-viewport-stable-height'),
    dvh: window.innerHeight,
  };
};

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 390, height: 900 }, deviceScaleFactor: 1 });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e.message)));
await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });

// --- 0. baseline: the stub starts at all zeros, nothing should blow up -------
{
  const now = await page.evaluate(readSafeArea);
  check(pageErrors.length === 0, `no page errors after boot (${pageErrors.length})`);
  const zeros = ['top', 'bottom', 'left', 'right'].every((edge) => now.inline[edge] === '0px');
  check(zeros, `a zero-inset SDK writes 0px on every edge (${JSON.stringify(now.inline)})`);
  check(now.appShellMinHeight === '900px', `app shell falls back to 100dvh without a viewport height (${now.appShellMinHeight})`);
  check(now.viewportHeightVar === '', 'no --tg-viewport-height is written when the SDK has none');
}

// --- 1. asymmetric object insets; both sources win at least one edge --------
// device: top 47 / bottom 34 / left 0 / right 25
// content: top 0 / bottom 21 / left 28 / right 7
// expected per-edge max: top 47 (device), bottom 34 (device), left 28 (content), right 25 (device)
{
  await page.evaluate(() => {
    window.Telegram.WebApp.__setInsets(
      { top: 47, bottom: 34, left: 0, right: 25 },
      { top: 0, bottom: 21, left: 28, right: 7 },
    );
  });
  await page.waitForTimeout(120);
  const now = await page.evaluate(readSafeArea);
  const expected = { top: '47px', bottom: '34px', left: '28px', right: '25px' };
  for (const [edge, value] of Object.entries(expected)) {
    check(now.inline[edge] === value, `app.js wrote --tg-safe-${edge} = max(device, content) = ${value} (${now.inline[edge]})`);
    check(now.probe[edge] === value, `--safe-${edge} resolves to ${value} in a real element (${now.probe[edge]})`);
  }
  check(now.probe.left === '28px' && now.inline.left === '28px', 'contentSafeAreaInset wins on the left edge where it is larger');
  // Consumers: all four edges are actually consumed by the layout.
  check(now.contentPaddingLeft === '28px', `content padding-left consumes --safe-left (${now.contentPaddingLeft})`);
  check(now.contentPaddingRight === '25px', `content padding-right consumes --safe-right (${now.contentPaddingRight})`);
  check(now.toastBottom === '48px', `toast region bottom consumes --safe-bottom (14px + 34px = ${now.toastBottom})`);
  check(now.topbarPaddingTop === '47px', `topbar padding-top consumes --safe-top (8px base vs 47px inset = ${now.topbarPaddingTop})`);
}

// --- 2. insets returning to zero must not leave stale values (rotation) -----
{
  await page.evaluate(() => {
    window.Telegram.WebApp.__setInsets({ top: 0, bottom: 0, left: 0, right: 0 }, { top: 0, bottom: 0, left: 0, right: 0 });
  });
  await page.waitForTimeout(120);
  const now = await page.evaluate(readSafeArea);
  for (const edge of ['top', 'bottom', 'left', 'right']) {
    check(now.inline[edge] === '0px' && now.probe[edge] === '0px', `--safe-${edge} is back to 0 after the inset is withdrawn (${now.inline[edge]} / ${now.probe[edge]})`);
  }
  check(now.contentPaddingLeft === '16px', `content padding-left returns to the 16px base (${now.contentPaddingLeft})`);
  check(now.contentPaddingRight === '16px', `content padding-right returns to the 16px base (${now.contentPaddingRight})`);
  check(now.toastBottom === '14px', `toast region returns to the 14px base (${now.toastBottom})`);
  check(now.topbarPaddingTop === '8px', `topbar returns to the 8px base (${now.topbarPaddingTop})`);
}

// --- 3. viewport height follows the SDK and shrinks the shell ---------------
{
  await page.evaluate(() => window.Telegram.WebApp.__setViewport(640, 700));
  await page.waitForTimeout(120);
  const now = await page.evaluate(readSafeArea);
  check(now.viewportHeightVar === '640px', `--tg-viewport-height tracks viewportHeight (${now.viewportHeightVar})`);
  check(now.stableHeightVar === '700px', `--tg-viewport-stable-height tracks viewportStableHeight (${now.stableHeightVar})`);
  check(now.appShellMinHeight === '640px', `app shell shrinks to the Telegram viewport (${now.appShellMinHeight})`);

  await page.evaluate(() => window.Telegram.WebApp.__setViewport(1200, 1200));
  await page.waitForTimeout(120);
  const bigger = await page.evaluate(readSafeArea);
  check(bigger.appShellMinHeight === '900px', `a Telegram viewport taller than 100dvh does not stretch the shell (${bigger.appShellMinHeight})`);
}

// --- 4. an unusable height falls back instead of collapsing the page --------
{
  for (const bad of [0, -50, Number.NaN, undefined]) {
    await page.evaluate((value) => window.Telegram.WebApp.__setViewport(value, value), bad);
    await page.waitForTimeout(80);
    const now = await page.evaluate(readSafeArea);
    check(
      now.appShellMinHeight === '900px',
      `viewportHeight=${String(bad)} falls back to 100dvh instead of collapsing the shell (${now.appShellMinHeight})`,
    );
  }
}

// --- 5. an old SDK without the Bot API 8.0 fields must not throw ------------
{
  await page.evaluate(() => {
    const tg = window.Telegram.WebApp;
    delete tg.safeAreaInset;
    delete tg.contentSafeAreaInset;
    delete tg.viewportHeight;
    delete tg.viewportStableHeight;
    tg.emit('safeAreaChanged');
    tg.emit('contentSafeAreaChanged');
    tg.emit('fullscreenChanged');
    tg.emit('viewportChanged', { isStateStable: true });
  });
  await page.waitForTimeout(120);
  const now = await page.evaluate(readSafeArea);
  check(pageErrors.length === 0, `an old SDK without the fields raises no error (${pageErrors.length})`);
  for (const edge of ['top', 'bottom', 'left', 'right']) {
    check(now.inline[edge] === '0px', `missing safeAreaInset writes the 0px default on --safe-${edge} (${now.inline[edge]})`);
  }
  check(now.appShellMinHeight === '900px', 'missing viewport height keeps the 100dvh fallback');
}

// --- 6. a full reload with the fields absent must also default cleanly ------
{
  const fresh = await browser.newPage({ viewport: { width: 390, height: 900 }, deviceScaleFactor: 1 });
  const freshErrors = [];
  fresh.on('pageerror', (e) => freshErrors.push(String(e.message)));
  await fresh.addInitScript(() => {
    // Strip the Bot API 8.0 fields before the page reads them.
    Object.defineProperty(window, 'Telegram', {
      configurable: true,
      set(value) {
        delete value.WebApp.safeAreaInset;
        delete value.WebApp.contentSafeAreaInset;
        delete value.WebApp.viewportHeight;
        delete value.WebApp.viewportStableHeight;
        Object.defineProperty(window, 'Telegram', { value, configurable: true, writable: true });
      },
      get() { return undefined; },
    });
  });
  await fresh.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await fresh.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  const now = await fresh.evaluate(readSafeArea);
  check(freshErrors.length === 0, `a cold boot on an old SDK raises no error (${freshErrors.length})`);
  const zeros = ['top', 'bottom', 'left', 'right'].every((edge) => now.inline[edge] === '0px');
  check(zeros, `a cold boot on an old SDK writes the 0px default on every edge (${JSON.stringify(now.inline)})`);
  check(now.appShellMinHeight === '900px', `a cold boot on an old SDK keeps the 100dvh shell (${now.appShellMinHeight})`);
  await fresh.close();
}

await browser.close();
console.log(notes.join('\n'));
if (failures.length) console.log(failures.join('\n'));
console.log(`\n${notes.length} safe-area checks passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
