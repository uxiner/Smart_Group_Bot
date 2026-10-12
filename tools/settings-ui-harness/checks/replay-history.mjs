// 审核回放 + 配置历史页的**真浏览器**验收（puppeteer-core + 本机 Chrome）。
//
// 为什么不是"DOM 里有个输入框"就算数：这一轮要证明的是**状态机** ——
// 登记即返回、页面轮询、取消真的调后端、错误重试复用同一个 idempotency_key、
// 回滚预览与真回滚一致、409 不动草稿。所以每一项都同时读回合成 API 的真实状态
// （`/harness/requests` 请求日志 + 服务端文档），而不只看 DOM。
//
// 关键坑（父代理踩过）：发布页的 CSP 允许 https://telegram.org，真实 SDK 会加载
// 并**替换** window.Telegram，把注入的桩丢掉，于是每次都"无法验证管理员身份"。
// 这里用 `harness.py --real-page` 逐字提供 index.html，再用**请求拦截**挡掉
// telegram.org，然后注入一份真正 HMAC 签名的 initData。
//
// 跑法（见 tools/settings-ui-harness/README.md）：
//   node $CHECKS/replay-history.mjs
//   GROUP_HARNESS=http://127.0.0.1:8803 node $CHECKS/replay-history.mjs   # 角色负例

import { createRequire } from 'node:module';
import { createHmac } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';

// puppeteer-core 只是一个**驱动**，仓库里没有 node_modules（也在 .gitignore 里）。
// 与本目录其它脚本一样从外部解析：PUPPETEER_ROOT 指到一个装过它的目录，
// 默认按 README 里的约定找 /tmp/cdpprobe。
const require = createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`);
const puppeteer = require('puppeteer-core');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.HARNESS || 'http://127.0.0.1:8801';
const GROUP_HARNESS = process.env.GROUP_HARNESS || 'http://127.0.0.1:8803';
const BUSY_HARNESS = process.env.BUSY_HARNESS || 'http://127.0.0.1:8804';
const FAIL_HARNESS = process.env.FAIL_HARNESS || 'http://127.0.0.1:8805';
const SAVE_FAIL_HARNESS = process.env.SAVE_FAIL_HARNESS || 'http://127.0.0.1:8806';
const RESTORE_CONFLICT_HARNESS = process.env.RESTORE_CONFLICT_HARNESS || 'http://127.0.0.1:8807';
const SHOTS = process.env.SHOTS || '/tmp/dsh-replay-history-shots';
const BOT_TOKEN = '42:TEST_TOKEN';
fs.mkdirSync(SHOTS, { recursive: true });

const notes = [];
const failures = [];
const warnings = [];
const check = (ok, msg) => {
  const line = ok ? `  PASS  ${msg}` : `  FAIL  ${msg}`;
  if (process.env.VERBOSE) console.error(line);
  return ok ? notes.push(line) : failures.push(line);
};

function signedInitData(userId) {
  const pairs = {
    auth_date: String(Math.floor(Date.now() / 1000)),
    query_id: `AAF-replay-history-${userId}`,
    user: JSON.stringify({ id: userId, first_name: 'Browser', language_code: 'zh' }),
  };
  const checkString = Object.keys(pairs).sort().map((key) => `${key}=${pairs[key]}`).join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  pairs.hash = createHmac('sha256', secret).update(checkString).digest('hex');
  return new URLSearchParams(pairs).toString();
}

const TELEGRAM_STUB = (initData) => `
(function () {
  var listeners = {};
  var webApp = {
    initData: ${JSON.stringify(initData)},
    initDataUnsafe: { user: { id: 1, first_name: 'Browser', language_code: 'zh' } },
    version: '8.0', platform: 'browser', colorScheme: 'dark',
    themeParams: { bg_color: '#0C1220', text_color: '#EEF3FC', hint_color: '#A8B5CC', link_color: '#71D9EF', button_color: '#71D9EF', button_text_color: '#062430' },
    isExpanded: true, isActive: true, isFullscreen: false,
    viewportHeight: 0, viewportStableHeight: 0,
    safeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    ready: function () {}, expand: function () {}, close: function () {},
    requestFullscreen: function () {}, exitFullscreen: function () {},
    setHeaderColor: function () {}, setBackgroundColor: function () {},
    disableVerticalSwipes: function () {}, enableClosingConfirmation: function () {},
    onEvent: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    offEvent: function (name, fn) { (listeners[name] = listeners[name] || []).filter(function (i) { return i !== fn; }); },
    emit: function (name, payload) { (listeners[name] || []).slice().forEach(function (fn) { fn(payload); }); }
  };
  window.Telegram = { WebApp: webApp };
})();
`;

const blockedCdn = [];

// 只在**驱动注入层**给 requestAnimationFrame 包一层计数：页面排了几帧、还欠几个
// 回调。为什么需要它：从抽屉切页时 app.js 会把一次"无障碍：把焦点移进正文"
// （content.focus()，app.js:7643）排在**同一条** rAF 队列上。那条回调在下一帧
// 就会把焦点从输入框上抢走 —— 真人点完导航要抬手、再去点输入框，碰不到；
// 脚本是毫秒级就 focus() 并开始打字，于是焦点在打字**中途**被挪走，后面的按键
// 全部打到 #content 上，输入停在一个前缀。这里数清楚"页面那一帧跑完了没有"，
// 驱动就能等它跑完再去碰输入框（waitForNavigationRafSettled）。
const RAF_PROBE = `
(function () {
  var probe = { pending: 0, runs: 0 };
  window.__raf = probe;
  var original = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (callback) {
    probe.pending += 1;
    return original(function (timestamp) {
      probe.pending -= 1;
      probe.runs += 1;
      return callback(timestamp);
    });
  };
})();
`;

const RESTORE_POST = /\/api\/v1\/settings\/history\/[^/]+\/restore(\?|$)/;

async function openPage(browser, { base = BASE, userId = 42, viewport = { width: 390, height: 844 }, track = false, holdRestore = false } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
  // holdRestore：把**下一次**回滚 POST 的**响应**挂住，直到驱动显式放行。
  // 只推迟"服务端什么时候答"，请求本身一个字节都没改 —— 页面最后拿到的仍然是
  // 同一个 409。用它是为了"回滚还在飞"这个窗口稳定可复现（生产里这就是
  // 别人抢先保存那一下的往返时间），不是为了让 409 变成另一种结果。
  const held = { restore: null };
  await page.exposeFunction('__harnessReleaseRestore', async () => {
    const request = held.restore;
    held.restore = null;
    if (request) await request.continue().catch(() => {});
  });
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const url = request.url();
    if (/^https:\/\/(telegram\.org|t\.me)\//i.test(url)) {
      blockedCdn.push(url);
      request.abort('blockedbyclient').catch(() => {});
      return;
    }
    if (holdRestore && held.restore === null && request.method() === 'POST' && RESTORE_POST.test(url)) {
      held.restore = request;
      return;
    }
    if (track && url.includes('/api/v1/')) {
      const parsed = new URL(url);
      page.__apiCalls.push(`${request.method()} ${parsed.pathname}${parsed.search}`);
    }
    request.continue().catch(() => {});
  });
  page.__apiCalls = [];
  await page.evaluateOnNewDocument(RAF_PROBE);
  await page.evaluateOnNewDocument(TELEGRAM_STUB(signedInitData(userId)));
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  await page.goto(`${base}/settings`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  return { page, consoleErrors, calls: page.__apiCalls };
}

const requestsOf = async (base) => (await (await fetch(`${base}/harness/requests`)).json()).requests || [];
const reset = (base) => fetch(`${base}/harness/reset`, { method: 'POST' });
const settingsDoc = async (base) => (await fetch(`${base}/api/v1/settings`)).json();
const samplesOf = (base, groupId) =>
  fetch(`${base}/api/v1/groups/${groupId}/moderation-replay/samples`).then((r) => r.json());

// 切页之后等页面自己那条 rAF 队列排空。
//
// 从抽屉切页时 app.js 会 `requestAnimationFrame(() => content.focus())`
// （app.js:7643，无障碍"焦点移进正文"）。这条回调在**下一帧**就会把焦点从任何
// 输入框上抢走。真人点完导航要抬手、再去点输入框，碰不到这一帧；脚本是毫秒级
// 就 focus() 并开始打字，于是 content.focus() 落在打字**中途**，后面的按键全部
// 打到 #content 上，输入停在一个前缀（父代理那次实测停在 "M"）。
//
// 判据不是"等够多少毫秒"，而是"我这次点击排进去的回调**已经跑完、队列空了**"：
// `pending === 0` 就是队列空了；它必须在**点击之后**采样才作数 ——
// waitForFunction 默认按 rAF 轮询，自己就会把那一帧跑掉，采样晚了就会误判成
// "页面不再产生帧"。rAF 回调在同一帧里是 FIFO，而那条无障碍 focus 是在点击
// 处理器里同步排进去的，所以"队列空"等价于"它已经跑过"。
async function waitForNavigationRafSettled(page, { timeout = 10000 } = {}) {
  const snapshot = await page.evaluate(() => (
    window.__raf ? { runs: window.__raf.runs, pending: window.__raf.pending } : null
  ));
  if (!snapshot) return false;
  // 点击那一刻队列就是空的：这次切页没有留下任何待跑的回调，可以直接碰输入框。
  if (snapshot.pending === 0) return true;
  try {
    await page.waitForFunction(
      (baseline) => Boolean(window.__raf) && window.__raf.runs > baseline && window.__raf.pending === 0,
      { timeout, polling: 'raf' },
      snapshot.runs,
    );
  } catch {
    const after = await page.evaluate(() => (window.__raf ? window.__raf.pending : null));
    if (after !== 0) {
      throw new Error(`切页后 ${timeout}ms 内页面自己的 rAF 队列没排空（还欠 ${after} 个回调）：这时去打字等于和页面抢焦点`);
    }
  }
  return true;
}

// 窄屏的导航在抽屉里（顶部那条 mobile-nav 是隐藏的），宽屏直接用左侧栏。
//
// ⚠ 这个助手带一条**兜底**：抽屉实在打不开时，它会 DOM-.click() 抽屉里的导航元素。
// 那条路径**不能**当作"抽屉可达"的证据，所以每次真的走了兜底，这里都会记一条
// 警告，最后单独打印出来。抽屉可达本身由下面 0b) 那节用**真实指针**单独证明。
async function gotoTab(page, navId) {
  const narrow = await page.evaluate(() => window.matchMedia('(max-width: 820px)').matches);
  const isOpen = () => page.evaluate(() => document.querySelector('.app-shell')?.classList.contains('sidebar-open'));
  let usedFallback = false;
  if (narrow) {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !(await isOpen())) {
      await page.click('#sidebar-toggle').catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!(await isOpen())) {
      usedFallback = true;
      const viewport = await page.evaluate(() => `${innerWidth}x${innerHeight}`);
      warnings.push(
        `gotoTab("${navId}") 在 ${viewport} 下走了 DOM 兜底：抽屉没能用指针打开，脚本直接 .click() 了抽屉里的 [data-nav="${navId}"]。`
        + '经过它的任何断言都**不能**算作「窄屏抽屉可达」的证据。',
      );
      await page.evaluate((id) => document.querySelector(`#desktop-nav [data-nav="${id}"]`)?.click(), navId);
    }
  }
  await clickWhenReady(page, `#desktop-nav [data-nav="${navId}"]`, { label: `切到 ${navId}` });
  // 必须**先**等页面那一帧跑完、再等导航项 active：等 active 的那个
  // waitForFunction 默认按 rAF 轮询，它自己就把切页那一帧跑掉了，采样会晚一步。
  await waitForNavigationRafSettled(page);
  await page.waitForFunction(
    (id) => document.querySelector(`#desktop-nav [data-nav="${id}"]`)?.classList.contains('active'),
    { timeout: 10000 },
    navId,
  );
  return { narrow, usedFallback };
}

// 合成 fixture 的"主群"：种子样本与那条已完成的回放都落在它上面。
const MAIN_GROUP = '-1009876543210';
const OTHER_GROUP = '-1001234567890';

// 回放的样本 / 候选 / 回放记录都在折叠面板里：不展开就点不到里面的控件
// （puppeteer 会把点击落在别处，而且那样根本不算"用户真的能点到"）。
async function expandReplayPanels(page) {
  await page.evaluate(() => {
    document.querySelectorAll('[data-replay-panel] [data-advanced-panel]').forEach((node) => { node.open = true; });
  });
  await page.waitForFunction(
    () => [...document.querySelectorAll('[data-replay-panel] [data-advanced-panel]')].every((node) => node.open),
    { timeout: 8000 },
  );
}

// 打开跨群的回放面板并把目标群的数据真正拉起来（返回 group_id）。
async function openReplay(page, groupId = MAIN_GROUP) {
  await page.waitForSelector('[data-replay-panel]', { timeout: 15000 });
  await page.evaluate((gid) => {
    const select = document.querySelector('[data-replay-group]');
    if (!select) return;
    select.value = gid;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }, groupId);
  await page.waitForFunction(
    (gid) => (document.querySelector('[data-replay-group]')?.value || '') === gid,
    { timeout: 10000 },
    groupId,
  );
  await page.waitForSelector('[data-action="replay-run"]', { timeout: 15000 });
  await page.waitForSelector('[data-replay-sample]', { timeout: 15000 });
  await expandReplayPanels(page);
  return groupId;
}

// 页面在轮询时会整块重渲染，按钮可能在点击落下的一瞬被换掉。
// 这个助手重新取元素并重试，模拟一个真人"再点一下"。
async function clickWhenReady(page, selector, { timeout = 20000, label = selector } = {}) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      await page.waitForSelector(selector, { visible: true, timeout: 1500 });
      await page.click(selector);
      return;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw new Error(`${label} 在 ${timeout}ms 内点不到：${lastError?.message || '未知原因'}`);
}

const detailState = (page) => page.evaluate(() => {
  const node = document.querySelector('[data-replay-detail]');
  return node ? { status: node.dataset.runStatus, runId: Number(node.dataset.runId) } : null;
});

async function waitForRunStatus(page, statuses, timeout = 25000) {
  await page.waitForFunction(
    (wanted) => {
      const node = document.querySelector('[data-replay-detail]');
      return Boolean(node) && wanted.includes(node.dataset.runStatus);
    },
    { timeout, polling: 120 },
    statuses,
  );
  return detailState(page);
}

// --------------------------------------------------------------------------
// 真实输入：逐键 + 真实指针
//
// 为什么不能直接用 `page.type()` / `ElementHandle.type()`：它们给每个字符之间的
// 等待是 `requestAnimationFrame`。而发布页在**从抽屉切页**时正好把
// `requestAnimationFrame(() => content.focus())` 排在同一个 rAF 队列上
// （app.js:7643，无障碍用的"焦点移进正文"）。两个 rAF 回调谁先跑是不确定的，
// 于是那条 content.focus() 有机会落在两个按键之间把焦点抢走，后面的按键全部
// 打到 #content 上，输入框里的值就停在一个前缀（实测会停在 "Markdow"）。
// 这**不是**产品截断：节点没被换、#content 一次都没重渲染（见 readTypingProbe），
// 真人以每键 ≥45ms 的速度也 100% 打全。驱动必须让出这条队列。
// --------------------------------------------------------------------------

// 记录"页面有没有整块重渲染"+"当前输入框是不是当初那个节点"，用来把
// "产品截断输入" 与 "驱动把焦点抢了" 区分开。
async function installRenderProbe(page) {
  await page.evaluate(() => {
    window.__renders = 0;
    new MutationObserver(() => { window.__renders += 1; }).observe(
      document.getElementById('content'),
      { childList: true },
    );
  });
}

async function tagField(page, selector) {
  await page.evaluate((sel) => {
    const node = document.querySelector(sel);
    if (node) node.__harnessTag = 'original-node';
  }, selector);
}

const readTypingProbe = (page, selector) => page.evaluate((sel) => {
  const node = document.querySelector(sel);
  return {
    value: node?.value ?? '',
    sameNode: node?.__harnessTag === 'original-node',
    renders: window.__renders ?? -1,
    active: document.activeElement?.id || document.activeElement?.tagName || 'null',
  };
}, selector);

// 真实逐键输入：每个字符都是一次真实的 keydown/keyup，中间留出真人打字的间隔。
// 先全选 + Backspace 清空，所以**不会**出现"只改了一半"的值。
async function typeField(page, selector, value, { gapMs = 45 } = {}) {
  await page.waitForSelector(selector, { visible: true, timeout: 15000 });
  await tagField(page, selector);
  await page.focus(selector);
  await page.evaluate((sel) => document.querySelector(sel)?.select(), selector);
  await page.keyboard.press('Backspace');
  for (const character of String(value)) {
    await page.keyboard.press(character === ' ' ? 'Space' : character);
    await new Promise((resolve) => setTimeout(resolve, gapMs));
  }
}

// 真实指针点击：先把控件滚进视口（导航列表本身是可滚的，"配置变更历史"在
// 360×780 这种高度下会落在折叠线以下），再确认控件**可见且中心点真的能被打到**，
// 最后用 mouse.move/down/up 点下去。不是 `element.click()`，也不是
// `HTMLElement.click()`。
async function pointerClick(page, selector, { label = selector, requireEnabled = false, attempts = 6 } = {}) {
  await page.waitForSelector(selector, { visible: true, timeout: 15000 });
  let target = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    // 滚动 + 量坐标 + 命中测试必须在**同一次 evaluate** 里做完。
    // 原因有两个，都是真的踩过的坑：
    //   1. 导航列表（#desktop-nav）本身可滚，"配置变更历史"在 360×780 下落在折叠线
    //      以下，必须先滚进来。
    //   2. 抽屉打开后约 300ms，页面会把导航的 scrollTop 复位（抽屉那次延迟的
    //      无障碍 focus 落在当前项上）。所以"滚 → 等 → 再量"中间会被推回顶部。
    // 同一次 evaluate 里滚完立刻量，量到的就是点击真正会用的视口坐标。
    target = await page.evaluate((sel) => {
      const node = document.querySelector(sel);
      if (!node) return null;
      node.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
      const rect = node.getBoundingClientRect();
      const x = rect.x + rect.width / 2;
      const y = rect.y + rect.height / 2;
      const inViewport = rect.width > 0 && rect.height > 0 && x > 0 && y > 0
        && x <= window.innerWidth && y <= window.innerHeight;
      const hit = inViewport ? document.elementFromPoint(x, y) : null;
      return {
        x,
        y,
        width: rect.width,
        height: rect.height,
        disabled: Boolean(node.disabled),
        hitId: hit?.id || hit?.tagName || 'null',
        reachable: Boolean(hit && (hit === node || node.contains(hit))),
      };
    }, selector);
    if (target && target.reachable) break;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  if (!target || target.width === 0 || target.height === 0) throw new Error(`${label} 没有可见的点击区域`);
  if (requireEnabled && target.disabled) throw new Error(`${label} 处于 disabled，点下去不会发生任何事`);
  if (!target.reachable) throw new Error(`${label} 的中心点（${Math.round(target.x)},${Math.round(target.y)}）被 ${target.hitId} 挡住了：这不算一次真实点击`);
  if (process.env.VERBOSE) console.error(`  · pointerClick ${label} @ ${Math.round(target.x)},${Math.round(target.y)} 命中 ${target.hitId}`);
  await page.mouse.move(target.x, target.y);
  await page.mouse.down();
  await page.mouse.up();
  return target;
}

// 抽屉打开后页面会跑一次**延迟的无障碍 focus**（app.js:7664-7667：把焦点落到
// 当前导航项上）。它顺带把 #desktop-nav 的 scrollTop 复位，而且首屏还在干活时
// 可能延后几百毫秒才跑完 —— 那之前量到的坐标会在点击落地前失效。等焦点真的落到
// 抽屉里，再去滚、去量。
async function waitForDrawerFocusSettled(page, { timeout = 4000 } = {}) {
  try {
    await page.waitForFunction(
      () => Boolean(document.activeElement && document.activeElement.closest && document.activeElement.closest('#desktop-nav')),
      { timeout, polling: 60 },
    );
    return true;
  } catch {
    // 不因为等不到就直接判失败：布局稳定 + 点击重试会兜住。
    return false;
  }
}

// 真人点一下没反应会再点一下。等不到预期结果时重试点击，而不是把一次竞态
// 当成产品缺陷（也不是把断言改松：最终的断言条件一个字没动）。
async function pointerClickUntil(page, selector, expect, { label = selector, arg = undefined, attempts = 3, waitMs = 1500 } = {}) {
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    await pointerClick(page, selector, { label: `${label}（第 ${attempt} 次）` });
    try {
      if (arg === undefined) await page.waitForFunction(expect, { timeout: waitMs, polling: 100 });
      else await page.waitForFunction(expect, { timeout: waitMs, polling: 100 }, arg);
      return attempt;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 120));
    }
  }
  return attempts;
}

// 等布局稳定：连续两次采样到同一个矩形才算稳。
async function waitForStableLayout(page, selector, { samples = 4, gapMs = 120 } = {}) {
  let previous = null;
  for (let index = 0; index < samples; index += 1) {
    const box = await page.evaluate((sel) => {
      const node = document.querySelector(sel);
      if (!node) return 'absent';
      const rect = node.getBoundingClientRect();
      return `${Math.round(rect.x)},${Math.round(rect.y)},${Math.round(rect.width)},${Math.round(rect.height)}`;
    }, selector);
    if (box !== 'absent' && box === previous) return true;
    previous = box;
    await new Promise((resolve) => setTimeout(resolve, gapMs));
  }
  return false;
}

// 合成配置历史的当前修订里 bot.parse_mode 是交替生成的（replay_fixtures.py），
// 所以"打什么值才是改动"必须**先读当前值**再决定。把当前值原样打一遍等于
// 什么都没改，保存按钮是 disabled，这条断言就退化成了"什么都没点"。
async function typeChangedParseMode(page) {
  await page.waitForSelector('#field-bot-parse_mode', { visible: true, timeout: 15000 });
  const current = await page.$eval('#field-bot-parse_mode', (node) => node.value);
  const next = current === 'HTML' ? 'Markdown' : 'HTML';
  await typeField(page, '#field-bot-parse_mode', next);
  const enabled = await page.$eval('#save-button', (node) => !node.disabled);
  check(enabled && next !== current, `草稿真的改动了东西（${current} → ${next}），顶栏「保存全部」可用`);
  return { current, next };
}

// 「这个点现在能不能被指针点到」：用 elementFromPoint 判定，而不是只看 CSS。
// scroll=true 时先把控件滚进视口（和 pointerClick 用同一次 evaluate 的做法，
// 这样量到的就是滚动之后那一帧的真实位置）。
const pointReachability = (page, selector, { scroll = false } = {}) => page.evaluate((sel, doScroll) => {
  const node = document.querySelector(sel);
  if (!node) return { present: false, reachable: false, inViewport: false, box: null, hitId: 'null' };
  if (doScroll) node.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
  const r = node.getBoundingClientRect();
  const box = { x: r.x, y: r.y, width: r.width, height: r.height };
  const cx = r.x + r.width / 2;
  const cy = r.y + r.height / 2;
  const inViewport = r.width > 0 && r.height > 0 && cx > 0 && cy > 0 && cx <= innerWidth && cy <= innerHeight;
  const hit = inViewport ? document.elementFromPoint(cx, cy) : null;
  return {
    present: true,
    reachable: Boolean(hit && (hit === node || node.contains(hit))),
    inViewport,
    box,
    hitId: hit?.id || hit?.tagName || 'null',
  };
}, selector, scroll);

// --------------------------------------------------------------------------
// 保存前预览的状态机：loading → ready / error
// --------------------------------------------------------------------------

// 记下 [data-save-preview] 依次渲染过哪几个相位。runSavePreview() 会先同步渲染
// 一个 aria-busy="true" 的 loading 节点再 await，所以 loading 一定出现过。
async function observePreviewPhases(page) {
  await page.evaluate(() => {
    window.__previewPhases = [];
    const record = () => {
      const node = document.querySelector('[data-save-preview]');
      const phase = !node
        ? 'absent'
        : node.getAttribute('aria-busy') === 'true'
          ? 'loading'
          : node.querySelector('.notice.danger')
            ? 'error'
            : 'ready';
      const list = window.__previewPhases;
      if (list[list.length - 1] !== phase) list.push(phase);
    };
    new MutationObserver(record).observe(document.getElementById('content'), {
      childList: true,
      subtree: true,
      attributes: true,
    });
    record();
  });
}

const readPreviewPhases = (page) => page.evaluate(() => window.__previewPhases || []);

// 等预览**落到终态**。loading 相位也带 [data-save-preview]，只等这个属性会读到
// "正在计算差异"那一帧，读出来的错误区当然是空的。
async function waitForSavePreviewSettled(page, timeout = 20000) {
  await page.waitForFunction(
    () => {
      const node = document.querySelector('[data-save-preview]');
      return Boolean(node) && node.getAttribute('aria-busy') !== 'true';
    },
    { timeout, polling: 60 },
  );
}

const readSavePreview = (page) => page.evaluate(() => {
  const node = document.querySelector('[data-save-preview]');
  return {
    present: Boolean(node),
    busy: node?.getAttribute('aria-busy') ?? null,
    danger: node?.querySelector('.notice.danger')?.innerText?.trim() || '',
    className: node?.className || '',
    text: node?.innerText || '',
  };
});

// --------------------------------------------------------------------------
// keep-alive 连接回归：故意失败的接口不许把请求体留在 socket 上
// --------------------------------------------------------------------------
// 全部请求都走同一个 keep-alive agent（maxSockets:1），所以"同一条 TCP 连接"
// 是可证的而不是猜的。handler 是 HTTP/1.1 keep-alive ：一个在读完
// Content-Length 之前就 return 的分支会把请求体留在 socket 上，下一条请求会先
// 读到那些残留字节并回一个 400 Bad request version。
async function keepAliveSequence(base, calls) {
  const target = new URL(base);
  const agent = new http.Agent({ keepAlive: true, maxSockets: 1, maxFreeSockets: 1 });
  const run = (method, path, body) => new Promise((resolve) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), 'utf8');
    const headers = payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {};
    const request = http.request(
      { host: target.hostname, port: target.port, path, method, headers, agent },
      (response) => {
        const chunks = [];
        // socket 在 'end' 之前就会被释放，端口必须当场取。
        const port = response.socket?.localPort ?? null;
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => resolve({ status: response.statusCode, port, body: Buffer.concat(chunks).toString('utf8') }));
      },
    );
    request.on('error', (error) => resolve({ status: 'ERR', port: null, body: String(error) }));
    if (payload) request.write(payload);
    request.end();
  });
  const results = [];
  for (const call of calls) results.push(await run(call.method, call.path, call.body));
  agent.destroy();
  return results;
}

async function confirmDialog(page) {
  await page.waitForSelector('#confirm-dialog[open]', { timeout: 8000 });
  await page.click('#confirm-action');
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});

try {
  // =========================================================================
  // 0) 页面真的用上了签名 initData，并且真的挡住了 CDN
  // =========================================================================
  {
    const { page, consoleErrors } = await openPage(browser);
    check(blockedCdn.length > 0, `请求拦截挡住了 ${blockedCdn.length} 个 telegram.org 请求（index.html 是逐字发布的）`);
    const usedInitData = await page.evaluate(() => Boolean(window.Telegram?.WebApp?.initData?.includes('hash=')));
    check(usedInitData, '注入的是**带 HMAC hash 的** initData，页面据此拿到了会话');
    const navIds = await page.evaluate(() => [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
    check(navIds.includes('history'), '最高管理员的导航里有「配置变更历史」');
    check(consoleErrors.length === 0, `首屏没有未捕获脚本错误${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

// =========================================================================
  // 0b) 窄屏抽屉可达 —— **独立证明**，不经过 gotoTab
  //
  // gotoTab() 带一条 DOM 兜底（抽屉打不开就直接 .click() 隐藏的导航元素），
  // 所以任何经过它的断言都不能拿来证明抽屉能用。这一节完全不碰 gotoTab：
  // 每一步都用**真实指针**（mouse.move/down/up）点**看得见**的控件，并且先用
  // elementFromPoint 证明那个点确实能被打到。先验条件也钉住：抽屉关着的时候
  // 导航项落在视口外，指针根本够不着 —— 这样"点得到"就只可能来自抽屉真的被
  // 打开，而不是某个隐藏节点被脚本直接点了。
  // =========================================================================
  for (const width of [360, 390, 430]) {
    await reset(BASE);
    const { page } = await openPage(browser, { viewport: { width, height: 780 } });
    const navSelector = '#desktop-nav [data-nav="history"]';
    const drawerOpen = () => page.evaluate(() => document.querySelector('.app-shell')?.classList.contains('sidebar-open'));

    await page.waitForSelector('#sidebar-toggle', { visible: true, timeout: 15000 });
    check(!(await drawerOpen()), `${width}px 初始状态抽屉是关着的`);
    const closed = await pointReachability(page, navSelector);
    check(closed.present && !closed.reachable && !closed.inViewport,
      `${width}px 抽屉关着时「配置变更历史」在视口外，指针够不着（box=${closed.box ? `${Math.round(closed.box.x)},${Math.round(closed.box.y)} ${Math.round(closed.box.width)}x${Math.round(closed.box.height)}` : 'null'}）`);
    check((await pointReachability(page, '#sidebar-toggle')).reachable, `${width}px 抽屉开关本身就在视口里，指针点得到`);

    await pointerClick(page, '#sidebar-toggle', { label: `${width}px 抽屉开关` });
    await page.waitForFunction(() => document.querySelector('.app-shell')?.classList.contains('sidebar-open'), { timeout: 8000 });
    check(await drawerOpen(), `${width}px 真实指针点开了抽屉`);
    // 抽屉打开后页面还会跑一次**延迟的无障碍 focus**（落在当前导航项上），
    // 它顺带把 #desktop-nav 的 scrollTop 复位，而且可能延后几百毫秒才跑完。
    // 等焦点真的落到抽屉里再量坐标，否则坐标会在点击落地前漂掉。
    const focusSettled = await waitForDrawerFocusSettled(page);
    await waitForStableLayout(page, navSelector);

    const opened = await pointReachability(page, navSelector, { scroll: true });
    check(opened.reachable && opened.inViewport,
      `${width}px 抽屉打开后「配置变更历史」可见且可点（box=${Math.round(opened.box.x)},${Math.round(opened.box.y)} ${Math.round(opened.box.width)}x${Math.round(opened.box.height)}，命中 ${opened.hitId}）`);

    const navAttempts = await pointerClickUntil(
      page,
      navSelector,
      (sel) => document.querySelector(sel)?.classList.contains('active'),
      { label: `${width}px 抽屉里的「配置变更历史」`, arg: navSelector },
    );
    // 切到历史页只是发起 GET /settings/history，表格是异步渲染出来的 —— 必须等行出现。
    await page.waitForSelector('.history-row', { timeout: 15000 });
    check(await page.evaluate(() => document.querySelectorAll('.history-row').length > 0),
      `${width}px 真实指针点完抽屉里的导航项，配置历史页真的渲染出 ${await page.evaluate(() => document.querySelectorAll('.history-row').length)} 行（不是 DOM .click() 走的后门；焦点沉降=${focusSettled}，点击 ${navAttempts} 次）`);
    check(!(await drawerOpen()), `${width}px 切页之后抽屉自动收起`);

    await pointerClick(page, '#sidebar-toggle', { label: `${width}px 再开一次抽屉` });
    await page.waitForFunction(() => document.querySelector('.app-shell')?.classList.contains('sidebar-open'), { timeout: 8000 });
    check(await drawerOpen(), `${width}px 顶栏那个可见开关能再次打开抽屉`);
    // 关抽屉必须走抽屉**里面**的「关闭导航」：抽屉打开时顶栏在遮罩之下
    // （.topbar z-index 20 < .app-shell.sidebar-open::after z-index 29 < .sidebar 30），
    // 所以抽屉内这个控件才是窄屏设计里真正的关闭路径，它必须可见且可点。
    check((await pointReachability(page, '#sidebar-close')).reachable,
      `${width}px 抽屉里的「关闭导航」可见且可点（顶栏开关此时在遮罩之下，不指望它）`);
    await pointerClick(page, '#sidebar-close', { label: `${width}px 抽屉里的「关闭导航」` });
    await page.waitForFunction(() => !document.querySelector('.app-shell')?.classList.contains('sidebar-open'), { timeout: 8000 });
    const reclosed = await pointReachability(page, navSelector);
    check(!(await drawerOpen()) && !reclosed.reachable, `${width}px 抽屉可以用抽屉里的可见开关关掉，导航项重新不可点`);
    await page.close();
  }

  // =========================================================================
  // 1) 回放：登记 → 轮询 → 完成
  // =========================================================================
  {
    await reset(BASE);
    const { page, consoleErrors } = await openPage(browser);
    await gotoTab(page, 'safety');
    const groupId = await openReplay(page, MAIN_GROUP);
    check(Boolean(groupId), `回放面板绑定到群 ${groupId}`);

    await page.waitForSelector('[data-action="replay-run"]:not([disabled])', { timeout: 10000 });
    const startedAt = Date.now();
    await page.click('[data-action="replay-run"]');
    await page.waitForFunction(
      () => Boolean(document.querySelector('[data-replay-run][data-run-status="queued"], [data-replay-run][data-run-status="running"]')),
      { timeout: 8000, polling: 80 },
    );
    const queuedMs = Date.now() - startedAt;
    check(queuedMs < 8000, `登记即返回并渲染「排队中 / 执行中」（${queuedMs}ms，没有阻塞在一次长请求上）`);

    const badgeLabel = await page.evaluate(
      () => document.querySelector('[data-replay-run] [data-replay-status]')?.textContent?.trim() || '',
    );
    check(/排队中|执行中/.test(badgeLabel), `回放行显示后台状态徽章：「${badgeLabel.replace(/\s+/g, ' ')}」`);

    const finished = await waitForRunStatus(page, ['completed', 'failed', 'cancelled', 'interrupted']);
    check(finished?.status === 'completed', `轮询走到终态：${finished?.status}`);
    const pollLog = (await requestsOf(BASE)).filter((line) => /^GET \/api\/v1\/groups\/-?\d+\/moderation-replay\/runs\/\d+$/.test(line));
    check(pollLog.length >= 2, `页面轮询了 ${pollLog.length} 次 GET /runs/{id}（不是一次拿完就停）`);

    const log = await requestsOf(BASE);
    check(log.some((line) => line.startsWith('POST') && line.endsWith('/moderation-replay/runs')), '确实发过 POST /moderation-replay/runs');
    check(log.some((line) => line.startsWith('GET') && line.endsWith('/moderation-replay/runs')), '刷新了回放列表');
    check(!log.some((line) => line.endsWith('/execute')), '登记那一轮没有误用 execute 端点');

    const detailText = await page.evaluate(() => document.querySelector('[data-replay-detail]')?.innerText || '');
    check(/不等价于生产回放/.test(detailText), '保真度区块明确写了「不等价于生产回放」');
    check(/脱敏/.test(detailText), '保真度区块写明样本已脱敏');
    check(/1800/.test(detailText), '界值区块用的是后端给的墙钟预算 1800 秒（不是写死的 120）');
    check(!/墙钟预算 120/.test(detailText), '界面上没有残留的 120 秒旧文案');

    const inconclusive = await page.evaluate(() =>
      [...document.querySelectorAll('[data-replay-detail] .replay-side.is-inconclusive')].map((el) => ({
        badge: el.querySelector('[data-replay-inconclusive]')?.textContent?.trim() || '',
        reason: el.querySelector('[data-replay-inconclusive-reason]')?.textContent?.trim() || '',
      })),
    );
    check(inconclusive.length > 0, `存在 ${inconclusive.length} 行「判不出来」`);
    check(inconclusive.every((row) => /判不出来/.test(row.badge) && row.reason.length > 0), '每一行「判不出来」都带上了判不出来的原因');
    check(inconclusive.every((row) => !/命中违规|未命中/.test(row.badge)), '不确定的行没有被按字面 verdict 渲染成"命中"或"通过"');

    const statsText = await page.evaluate(() => document.querySelector('[data-replay-detail] [data-replay-stats]')?.innerText || '');
    check(/确认命中违规/.test(statsText), '统计区分开了「确认命中」（*_confirmed_*）');
    check(/判不出来/.test(statsText) && /判定失败/.test(statsText), '「判不出来 / 判定失败」是独立区块');
    check(/带人工金标样本/.test(statsText), 'labeled_total 紧邻误伤 / 漏放区块');
    check(consoleErrors.length === 0, `回放流程没有未捕获脚本错误${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  // =========================================================================
  // 1b) 跑一半离开页面：进度不丢，轮询必须停下来（不留定时器）
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    await openReplay(page, MAIN_GROUP);
    await clickWhenReady(page, '[data-action="replay-run"]');
    await page.waitForFunction(
      () => Boolean(document.querySelector('[data-replay-run][data-run-status="queued"], [data-replay-run][data-run-status="running"]')),
      { timeout: 12000 },
    );
    const runId = await page.evaluate(() => Number(document.querySelector('[data-replay-run]')?.dataset.replayRun));
    const pollLine = `GET /api/v1/groups/${MAIN_GROUP}/moderation-replay/runs/${runId}`;
    const countPolls = async () => (await requestsOf(BASE)).filter((line) => line === pollLine).length;

    // 离开这一页：视图被拆掉，轮询必须跟着停。
    await gotoTab(page, 'models');
    const pollsAtLeave = await countPolls();
    await new Promise((resolve) => setTimeout(resolve, 7000));
    const pollsAfterAway = await countPolls();
    check(pollsAfterAway === pollsAtLeave, `离开回放页后 7 秒里一次都没再轮询（${pollsAtLeave} → ${pollsAfterAway}），没有留下后台定时器`);

    // 回来：这一轮还在，进度没有被丢掉，轮询重开始。
    await gotoTab(page, 'safety');
    await page.waitForSelector('[data-replay-run]', { timeout: 10000 });
    const tracked = await page.evaluate((id) => {
      const row = document.querySelector(`[data-replay-run="${id}"]`);
      return { status: row?.dataset.runStatus || '', tracked: Boolean(row?.querySelector('[data-replay-tracked]')) };
    }, runId);
    check(tracked.status !== '' , `回到回放页仍然能看到这一轮（status=${tracked.status}）`);
    check(tracked.tracked, '页面仍然在跟进这一轮（tracked 标记还在）');
    const done = await waitForRunStatus(page, ['completed', 'failed', 'cancelled', 'interrupted'], 25000);
    check(['completed', 'failed', 'cancelled', 'interrupted'].includes(done?.status), `回来之后轮询继续推进到 ${done?.status}`);
    check(await countPolls() > pollsAfterAway, `回到页面之后轮询恢复了（共 ${await countPolls()} 次）`);
    await page.close();
  }

  // =========================================================================
  // 2) queued 必须可执行：POST /runs/{id}/execute 是真的被调用的
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    const groupId = await openReplay(page, MAIN_GROUP);
    const queued = await page.evaluate(async (gid) => {
      const response = await fetch(`/api/v1/groups/${gid}/moderation-replay/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          candidate: { rules: [{ rule_type: 'keyword', pattern: '排队测试', action: 'warn', enabled: true, scan_scope: 'message' }] },
          idempotency_key: 'browser-queued-check',
          execute: false,
        }),
      });
      return response.json();
    }, groupId);
    check(queued?.run?.status === 'queued', `先登记（execute:false）拿到 status=${queued?.run?.status}`);

    await clickWhenReady(page, '[data-action="replay-refresh"]', { label: '刷新回放' });
    await page.waitForSelector(`[data-replay-run="${queued.run.id}"] [data-action="replay-execute-run"]`, { timeout: 10000 });
    check(true, `排队中的 run #${queued.run.id} 在界面上有「启动这一轮」按钮`);

    await clickWhenReady(page, `[data-replay-run="${queued.run.id}"] [data-action="replay-open-run"]`, { label: '打开回放' });
    const opened = await waitForRunStatus(page, ['queued'], 10000);
    check(opened?.status === 'queued', '打开排队中的 run，详情显示「排队中」而不是空白');

    await clickWhenReady(page, '[data-replay-detail] [data-action="replay-execute-run"]');
    const started = await waitForRunStatus(page, ['running', 'completed'], 10000);
    check(['running', 'completed'].includes(started?.status), `execute 之后状态推进到 ${started?.status}`);
    const log = await requestsOf(BASE);
    check(
      log.includes(`POST /api/v1/groups/${groupId}/moderation-replay/runs/${queued.run.id}/execute`),
      '真的调用了 POST /runs/{id}/execute',
    );
    await page.close();
  }

  // =========================================================================
  // 3) 取消：真的调 cancel 端点，界面靠**轮询看到** cancelled
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    const groupId = await openReplay(page, MAIN_GROUP);
    await clickWhenReady(page, '[data-action="replay-run"]');
    await page.waitForSelector('[data-replay-run] [data-action="replay-cancel-run"]', { timeout: 15000 });
    const targetId = await page.evaluate(() => Number(document.querySelector('[data-replay-run]')?.dataset.replayRun));
    await clickWhenReady(page, '[data-replay-run] [data-action="replay-cancel-run"]');
    const cancelled = await waitForRunStatus(page, ['cancelled', 'completed'], 15000);
    check(cancelled?.status === 'cancelled', `取消后轮询看到 status=${cancelled?.status}`);
    const log = await requestsOf(BASE);
    check(
      log.includes(`POST /api/v1/groups/${groupId}/moderation-replay/runs/${targetId}/cancel`),
      `真的调用了 POST /runs/${targetId}/cancel`,
    );
    check(
      log.filter((line) => line === `GET /api/v1/groups/${groupId}/moderation-replay/runs/${targetId}`).length >= 1,
      '取消之后还在继续轮询，并**看到**了 cancelled（不是界面自己假装取消）',
    );
    const detail = await page.evaluate(() => document.querySelector('[data-replay-detail]')?.innerText || '');
    check(/被取消的回放/.test(detail), '界面把取消明确写成「这是一次被取消的回放」');
    check(/部分结果不能当作一次完整对照/.test(detail), '部分结果没有被当成完整对照');
    await page.screenshot({ path: `${SHOTS}/replay-cancelled.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 4) 429 replay_busy：带容量；重试复用同一个 idempotency_key
  // =========================================================================
  {
    await reset(BUSY_HARNESS);
    const { page } = await openPage(browser, { base: BUSY_HARNESS });
    await gotoTab(page, 'safety');
    const groupId = await openReplay(page, MAIN_GROUP);
    const runsBefore = await page.evaluate(
      async (gid) => (await (await fetch(`/api/v1/groups/${gid}/moderation-replay/runs`)).json()).runs.length,
      groupId,
    );
    await clickWhenReady(page, '[data-action="replay-run"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')].some((el) => (el.textContent || '').includes('队列已满')),
      { timeout: 10000 },
    );
    const toastText = await page.evaluate(() => [...document.querySelectorAll('.toast')].map((el) => el.textContent).join(' | '));
    check(/队列已满/.test(toastText), `429 replay_busy 被读成人话：「${toastText.replace(/\s+/g, ' ').slice(0, 110)}」`);
    check(/执行中\s*\d+\/\d+/.test(toastText), `提示里带上了服务端给的容量占用：${(toastText.match(/执行中\s*\d+\/\d+/) || [''])[0]}`);
    check(/idempotency_key/.test(toastText), '提示里说明了重试会复用同一个 idempotency_key');

    await clickWhenReady(page, '[data-action="replay-run"]');
    await page.waitForFunction(
      () => Boolean(document.querySelector('[data-replay-run][data-run-status="queued"], [data-replay-run][data-run-status="running"]')),
      { timeout: 12000 },
    );
    const runs = await page.evaluate(
      async (gid) => (await (await fetch(`/api/v1/groups/${gid}/moderation-replay/runs`)).json()).runs,
      groupId,
    );
    check(
      runs.length === runsBefore + 1,
      `重试后回放从 ${runsBefore} 条变成 ${runs.length} 条（只多一条：复用同一个 idempotency_key，没有重复登记）`,
    );
    await page.close();
  }

  // =========================================================================
  // 5) 失败态：run 以 failed 结束
  // =========================================================================
  {
    await reset(FAIL_HARNESS);
    const { page } = await openPage(browser, { base: FAIL_HARNESS });
    await gotoTab(page, 'safety');
    await openReplay(page, MAIN_GROUP);
    await clickWhenReady(page, '[data-action="replay-run"]');
    const failed = await waitForRunStatus(page, ['failed'], 25000);
    check(failed?.status === 'failed', `这一轮以 status=${failed?.status} 结束`);
    const detail = await page.evaluate(() => document.querySelector('[data-replay-detail]')?.innerText || '');
    check(/这是一次失败的回放/.test(detail), '失败回放被明确写成失败，没有冒充一次完成的对照');
    check(/执行失败/.test(detail), '失败原因用的是稳定错误码的中文（run_failed → 执行失败）');
    await page.screenshot({ path: `${SHOTS}/replay-failed.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 6) 换群：A 群的数据不能画到 B 群
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    await openReplay(page, MAIN_GROUP);
    const firstSamples = await page.evaluate(() => [...document.querySelectorAll('[data-replay-sample]')].map((el) => el.dataset.replaySample));
    check(firstSamples.length > 0, `${MAIN_GROUP} 加载出 ${firstSamples.length} 条样本`);
    await openReplay(page, OTHER_GROUP);
    const secondSamples = await page.evaluate(() => [...document.querySelectorAll('[data-replay-sample]')].map((el) => el.dataset.replaySample));
    check(secondSamples.length > 0, `${OTHER_GROUP} 加载出 ${secondSamples.length} 条样本`);
    check(
      !secondSamples.some((id) => firstSamples.includes(id)),
      `换群之后 A 群（${firstSamples.join(',')}）的样本一条都没残留到 B 群（${secondSamples.join(',')}）`,
    );
    await page.screenshot({ path: `${SHOTS}/replay-switch-group.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 7) 人工金标：确认 / 修正 / 清除；机器建议必须与金标视觉分开
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    const groupId = await openReplay(page, MAIN_GROUP);
    const target = await page.evaluate(() => {
      const row = [...document.querySelectorAll('[data-replay-sample]')].find((el) => el.querySelector('[data-replay-suggestion]'));
      return row ? row.dataset.replaySample : '';
    });
    check(Boolean(target), `找到一条带机器建议的样本：${target}`);
    const suggestion = await page.evaluate((id) => {
      const node = document.querySelector(`[data-replay-sample="${id}"] [data-replay-suggestion]`);
      return { text: node?.innerText || '', badgeClass: node?.querySelector('.badge')?.className || '' };
    }, target);
    check(/机器建议/.test(suggestion.text) && /不会/.test(suggestion.text), `机器建议单独成块并声明不参与统计：「${suggestion.text.replace(/\s+/g, ' ').slice(0, 90)}」`);
    check(/suggestion/.test(suggestion.badgeClass) && !/\b(success|danger)\b/.test(suggestion.badgeClass), '机器建议用的不是"已确认金标"那种颜色');

    await clickWhenReady(page, `[data-replay-sample="${target}"] [data-action="replay-label"][data-gold-label="benign"]`, { label: '标为正常' });
    await confirmDialog(page);
    await page.waitForFunction(
      (id) => document.querySelector(`[data-replay-sample="${id}"]`)?.dataset.labelState === 'labelled',
      { timeout: 10000 },
      target,
    );
    let badge = await page.evaluate((id) => document.querySelector(`[data-replay-sample="${id}"] [data-gold-badge]`)?.textContent?.trim(), target);
    let doc = (await samplesOf(BASE, groupId)).samples.find((s) => String(s.id) === target);
    check(/正常/.test(badge || ''), `确认后金标徽章是「${badge}」`);
    check(doc?.label_state === 'labelled' && doc?.gold_label === 'benign', '服务端记录 gold_label=benign / label_state=labelled');

    await clickWhenReady(page, `[data-replay-sample="${target}"] [data-action="replay-label"][data-gold-label="violation"]`, { label: '标为违规' });
    await confirmDialog(page);
    await page.waitForFunction(
      (id) => document.querySelector(`[data-replay-sample="${id}"] [data-gold-badge]`)?.textContent?.includes('违规'),
      { timeout: 10000 },
      target,
    );
    doc = (await samplesOf(BASE, groupId)).samples.find((s) => String(s.id) === target);
    check(doc?.gold_label === 'violation', '修正后服务端 gold_label=violation（变更留在 label_history）');

    await clickWhenReady(page, `[data-replay-sample="${target}"] [data-action="replay-label"][data-gold-label=""]`, { label: '清除金标' });
    await confirmDialog(page);
    await page.waitForFunction(
      (id) => document.querySelector(`[data-replay-sample="${id}"]`)?.dataset.labelState === 'unlabelled',
      { timeout: 10000 },
      target,
    );
    doc = (await samplesOf(BASE, groupId)).samples.find((s) => String(s.id) === target);
    check(doc?.label_state === 'unlabelled' && doc?.gold_label === null, '清除后服务端 gold_label=null / label_state=unlabelled');
    check(Boolean(doc?.provenance?.suggested_label), '机器建议仍然留在 provenance 里（清除金标不等于抹掉来源）');
    await page.screenshot({ path: `${SHOTS}/sample-gold-label.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 8) 样本导入 / 删除
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'safety');
    await openReplay(page, MAIN_GROUP);
    const before = await page.evaluate(() => document.querySelectorAll('[data-replay-sample]').length);
    await clickWhenReady(page, '[data-action="replay-import"]');
    await confirmDialog(page);
    await page.waitForFunction((count) => document.querySelectorAll('[data-replay-sample]').length > count, { timeout: 10000 }, before);
    const imported = await page.evaluate(() =>
      [...document.querySelectorAll('[data-replay-sample]')]
        .filter((el) => (el.textContent || '').includes('导入的违规样本'))
        .map((el) => el.dataset.replaySample),
    );
    check(imported.length > 0, `导入的 ${imported.length} 条样本在界面上出现（样本总数 ${before} → ${before + imported.length}）`);
    const unlabelled = await page.evaluate(
      (ids) => ids.every((id) => document.querySelector(`[data-replay-sample="${id}"]`)?.dataset.labelState === 'unlabelled'),
      imported,
    );
    check(unlabelled, '机器导入的样本**没有**金标（导入不会悄悄变成答案键）');

    await clickWhenReady(page, `[data-replay-sample="${imported[0]}"] [data-action="replay-delete-sample"]`, { label: '删除样本' });
    await confirmDialog(page);
    await page.waitForFunction((id) => !document.querySelector(`[data-replay-sample="${id}"]`), { timeout: 10000 }, imported[0]);
    const log = await requestsOf(BASE);
    check(log.some((line) => line.startsWith('DELETE') && line.includes(`/moderation-replay/samples/${imported[0]}`)), '删除样本真的调了 DELETE 端点');
    await page.close();
  }

  // =========================================================================
  // 9) 配置历史：真分页（next_before）、越界 400 不夹取
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'history');
    await page.waitForSelector('.history-row', { timeout: 15000 });
    const revisionsOf = () => page.evaluate(() =>
      [...document.querySelectorAll('.history-row')].map((row) => Number(row.querySelector('[data-revision]')?.dataset.revision)));
    const firstPage = await revisionsOf();
    check(firstPage.length > 0, `第一页拿到 ${firstPage.length} 行（${Math.min(...firstPage)}…${Math.max(...firstPage)}）`);
    check(await page.evaluate(() => !document.querySelector('[data-action="history-older"]')?.disabled), '「更早的一页」可用（服务端 page.has_more=true）');
    await clickWhenReady(page, '[data-action="history-older"]', { label: '更早的一页' });
    await page.waitForFunction(
      (min) => {
        const rows = [...document.querySelectorAll('.history-row')].map((row) => Number(row.querySelector('[data-revision]')?.dataset.revision));
        return rows.length > 0 && Math.min(...rows) < min;
      },
      { timeout: 10000, polling: 80 },
      Math.min(...firstPage),
    );
    const secondPage = await revisionsOf();
    check(
      Math.max(...secondPage) < Math.min(...firstPage),
      `翻页真的往更早走：第二页 ${Math.min(...secondPage)}…${Math.max(...secondPage)}，与第一页不重叠`,
    );
    const log = await requestsOf(BASE);
    check(
      log.some((line) => line === `GET /api/v1/settings/history?limit=20&before=${Math.min(...firstPage)}`),
      `翻页请求带上了服务端给的 next_before=${Math.min(...firstPage)}`,
    );
    check(!log.some((line) => /[?&]limit=(0|1[0-9][1-9]|[2-9][0-9]{2,})\b/.test(line)), '客户端从不发送越界页大小（服务端会 400 且不夹取）');
    await clickWhenReady(page, '[data-action="history-newest"]', { label: '回到最新' });
    await page.waitForFunction(
      (max) => [...document.querySelectorAll('.history-row')].some((row) => Number(row.querySelector('[data-revision]')?.dataset.revision) === max),
      { timeout: 10000 },
      Math.max(...firstPage),
    );
    check(true, '「回到最新」能回到第一页');
    await page.screenshot({ path: `${SHOTS}/history-list.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 10) 保存前预览：脱敏标记不被抹掉；无变化是合法答案
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'models');
    await page.waitForSelector('#provider-base-0', { timeout: 15000 });
    const markerBefore = await page.evaluate(async () => {
      const doc = await (await fetch('/api/v1/settings')).json();
      return {
        header: doc.config.models.main.request_params.extra_headers['X-Demo-Token'],
        apiBase: doc.config.models.providers[0].api_base,
      };
    });
    check(/^<redacted:/.test(markerBefore.header || ''), `GET 回来的自由 JSON 凭据是标记：${markerBefore.header}`);
    check((markerBefore.apiBase || '').includes('<redacted:url-userinfo>'), `GET 回来的 URL 凭据是就地标记：${markerBefore.apiBase}`);
    const hints = await page.evaluate(() => [...document.querySelectorAll('[data-opaque-marker]')].map((el) => el.textContent.trim()).slice(0, 3));
    check(
      hints.length > 0 && hints.every((text) => /已配置|含隐藏凭据|不回滚此字段/.test(text)),
      `标记字段旁边有明确说明：「${(hints[0] || '').slice(0, 80)}」`,
    );
    const renderedApiBase = await page.evaluate(() => document.querySelector('#provider-base-0')?.value || '');
    check(renderedApiBase === markerBefore.apiBase, '带标记的地址原样显示，没有被当成普通 URL 改写');

    // 必须改成一个**与当前值不同**的值才会真的变脏（当前值由合成历史决定，见
    // typeChangedParseMode 的注释），否则保存按钮是 disabled，这条就什么也没验到。
    await gotoTab(page, 'bot');
    await typeChangedParseMode(page);
    await pointerClick(page, '#save-button', { label: '顶栏「保存全部」', requireEnabled: true });
    await waitForSavePreviewSettled(page);
    await clickWhenReady(page, '[data-action="save-preview-confirm"]', { label: '确认保存' });
    await page.waitForFunction(() => !document.querySelector('[data-save-preview]'), { timeout: 10000 });
    const afterSave = await page.evaluate(async () => {
      const doc = await (await fetch('/api/v1/settings')).json();
      return { header: doc.config.models.main.request_params.extra_headers['X-Demo-Token'], apiBase: doc.config.models.providers[0].api_base, revision: doc.revision };
    });
    check(afterSave.header === markerBefore.header, `改了一个无关字段再保存，自由 JSON 里的标记原样保留（${afterSave.header}）`);
    check(afterSave.apiBase === markerBefore.apiBase, `URL 里的就地标记也原样保留（${afterSave.apiBase}）`);

    // 服务端契约：把**没动过**的文档原样提交，diff 必须是空的、摘要必须是"无变化"。
    const unchanged = await page.evaluate(async () => {
      const doc = await (await fetch('/api/v1/settings')).json();
      const response = await fetch('/api/v1/settings/diff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: doc.revision, config: doc.config, secret_changes: {} }),
      });
      return response.json();
    });
    check(Array.isArray(unchanged.diff) && unchanged.diff.length === 0, '未改动的文档预览回来是空 diff');
    check(unchanged.summary === '无变化', `空 diff 的摘要是「${unchanged.summary}」`);
    check(Number(unchanged.snapshot_limit_bytes) > 0, `预览带回 snapshot_bytes/snapshot_limit_bytes（${unchanged.snapshot_bytes}/${unchanged.snapshot_limit_bytes}）`);
    check(unchanged.restart_pending.length === 0, '无变化时没有 restart_pending');
    await page.close();
  }

  // ---- 无变化预览在页面上不是错误 -----------------------------------------
  {
    const unchangedHarness = process.env.UNCHANGED_HARNESS || 'http://127.0.0.1:8808';
    await reset(unchangedHarness);
    const { page } = await openPage(browser, { base: unchangedHarness });
    await gotoTab(page, 'bot');
    // 这里验的是"服务端回了空 diff，页面把它当成合法答案"，所以页面本身**必须**
    // 处在"有改动"的状态：否则保存按钮不亮，等于什么都没点。
    await typeChangedParseMode(page);
    await pointerClick(page, '#save-button', { label: '顶栏「保存全部」', requireEnabled: true });
    await page.waitForSelector('[data-save-preview][data-preview-empty="true"]', { timeout: 12000 });
    const noChangeText = await page.evaluate(() => document.querySelector('[data-save-preview]')?.innerText || '');
    check(/无变化/.test(noChangeText), '空 diff 被渲染成「服务端算出来是「无变化」」');
    check(!/预览没有成功/.test(noChangeText), '没有被渲染成"预览失败"');
    check(!/notice danger/.test(await page.evaluate(() => document.querySelector('[data-save-preview]')?.className || '')), '预览面板不是错误态');
    check(/快照占用/.test(noChangeText), `快照占用可见：「${(noChangeText.match(/快照占用[^\n]*/) || [''])[0]}」`);
    check(!/确认保存这些改动/.test(noChangeText), '无变化时不提供「确认保存」按钮（没有字段会被改写）');
    await page.screenshot({ path: `${SHOTS}/save-preview-no-change.png`, fullPage: false });
    await page.close();
  }

// =========================================================================
  // 11) 保存失败 503：草稿必须留在页面上，而且失败不许污染这条连接
  //
  // 这里同时钉住两个曾经翻车的点：
  //   - 输入：真实逐键输入 MarkdownV2。断言输入框是**同一个 DOM 节点**、且
  //     #content 一次都没重渲染 —— 真要是产品重渲染把值截断，这两条会先红。
  //   - 读错误：必须等预览**落到终态**再读。loading 相位也带
  //     [data-save-preview]，只等这个属性就会读到"正在计算差异"那一帧，
  //     错误区当然是空的（这不是产品把错误渲染成了空）。
  //   - 连接：--fail-save 下 POST /settings/diff 故意 503，handler 必须把请求
  //     体读干净，否则 keep-alive 连接上紧接着的请求会收到 400。
  // =========================================================================
  {
    await reset(SAVE_FAIL_HARNESS);
    const { page } = await openPage(browser, { base: SAVE_FAIL_HARNESS });
    await gotoTab(page, 'bot');
    await installRenderProbe(page);
    await typeField(page, '#field-bot-parse_mode', 'MarkdownV2');
    const typed = await readTypingProbe(page, '#field-bot-parse_mode');
    check(typed.value === 'MarkdownV2', `真实逐键输入后输入框里就是完整的 MarkdownV2（${typed.value}）`);
    check(typed.sameNode, '打字全程是同一个输入框节点（页面没有把字段换掉）');
    check(typed.renders === 0, `打字全程页面没有整块重渲染（#content 子节点变更 ${typed.renders} 次）`);
    check(typed.active === 'field-bot-parse_mode', `焦点还在输入框上（activeElement=${typed.active}）`);

    await observePreviewPhases(page);
    await pointerClick(page, '#save-button', { label: '顶栏「保存全部」', requireEnabled: true });
    await waitForSavePreviewSettled(page);
    const phases = await readPreviewPhases(page);
    check(phases.includes('loading') && phases[phases.length - 1] === 'error',
      `预览依次走过 ${phases.join(' → ')}（loading 相位也带 [data-save-preview]，只等这个属性会读空）`);

    const preview = await readSavePreview(page);
    check(preview.busy !== 'true' && preview.danger.length > 0,
      `预览 503 被显示为错误：「${preview.danger.replace(/\s+/g, ' ').slice(0, 80)}」`);
    check(/预览没有成功/.test(preview.text), '错误态明说还没有发起保存');
    check(Boolean(await page.$('[data-action="save-preview-refresh"]')), '错误态给出「重新计算」而不是把页面卡死');

    const draftValue = await page.evaluate(() => document.querySelector('#field-bot-parse_mode')?.value || '');
    check(draftValue === 'MarkdownV2', `预览失败后草稿仍在页面上：${draftValue}`);
    const stored = await settingsDoc(SAVE_FAIL_HARNESS);
    check(stored.config.bot.parse_mode !== 'MarkdownV2', '服务端一个字节都没被写进去');

    // 页面自己紧接着发的那次读回也必须干净（这才是用户真正看见的症状）。
    const afterFailure = await page.evaluate(async () => {
      const response = await fetch('/api/v1/settings', { cache: 'no-store' });
      let ok = false;
      try { ok = Boolean((await response.json())?.config); } catch { ok = false; }
      return { status: response.status, ok };
    });
    check(afterFailure.status === 200 && afterFailure.ok,
      `503 之后页面紧接着的读回是干净的 200（${afterFailure.status}，解析出 config=${afterFailure.ok}）`);
    await page.close();
  }

  // ---- 提前返回的分支不许把请求体留在 keep-alive 连接上 --------------------
  // 三个分支（503 fail-save / 403 group-admin / 409 conflict-save）都在读完
  // Content-Length 之就 return。请求走同一个 maxSockets:1 的 keep-alive
  // agent，所以"同一条 TCP 连接"是可证的：localPort 必须前后一致。
  {
    const settings_ = await settingsDoc(SAVE_FAIL_HARNESS);
    const [diff503, after503, third] = await keepAliveSequence(SAVE_FAIL_HARNESS, [
      { method: 'POST', path: '/api/v1/settings/diff', body: { revision: settings_.revision, config: settings_.config, secret_changes: {} } },
      { method: 'GET', path: '/api/v1/settings' },
      { method: 'GET', path: '/harness/requests' },
    ]);
    check(diff503.status === 503, `--fail-save 下 POST /settings/diff 确实回 503（${diff503.status}）`);
    check(diff503.port !== null && after503.port === diff503.port,
      `503 之后的下一个请求走的是**同一条** TCP 连接（localPort ${diff503.port} → ${after503.port}）`);
    check(after503.status === 200,
      `503 之后同一条连接上的第一个请求干净地回 200（${after503.status}，不是被污染后的 400 Bad request version）`);
    let parsed = null;
    try { parsed = JSON.parse(after503.body); } catch { parsed = null; }
    check(Boolean(parsed && parsed.config),
      `读回的是一份完整的配置文档（revision=${parsed?.revision}），不是 HTML 错误页`);
    check(third.status === 200 && third.port === diff503.port,
      `再下一个请求同样干净（${third.status}，仍在 localPort ${third.port}）`);

    const [diff403, after403] = await keepAliveSequence(GROUP_HARNESS, [
      { method: 'POST', path: '/api/v1/settings/diff', body: { revision: 1, config: { bot: { parse_mode: 'Markdown' } }, secret_changes: {} } },
      { method: 'GET', path: '/api/v1/session' },
    ]);
    check(diff403.status === 403, `群管理员的 POST /settings/diff 确实提前回 403（${diff403.status}）`);
    check(after403.status === 200 && after403.port === diff403.port,
      `403 之后同一条连接上的请求是干净的 200（${after403.status}，localPort ${after403.port}）`);
  }

  // =========================================================================
  // 12) 回滚：真预览 → 确认 → 真回滚 → 读回
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'history');
    await page.waitForSelector('.history-row', { timeout: 15000 });
    const targetRevision = await page.evaluate(() =>
      Math.min(...[...document.querySelectorAll('.history-row')].map((row) => Number(row.querySelector('[data-revision]')?.dataset.revision))));
    await clickWhenReady(page, `[data-action="history-restore"][data-revision="${targetRevision}"]`, { label: '回滚按钮' });
    await page.waitForSelector('[data-history-restore][data-restore-ok="true"]', { timeout: 10000 });
    const previewText = await page.evaluate(() => document.querySelector('[data-history-restore]')?.innerText || '');
    check(/回滚目标：历史版本 r\d+/.test(previewText), '预览写明「回滚目标」是历史版本');
    check(/当前生效：r\d+/.test(previewText), '预览写明「当前生效」是另一个版本号');
    check(previewText.includes(`r${targetRevision}`), `预览里带着目标版本 r${targetRevision}`);
    check(/新的修订/.test(previewText) && /不会/.test(previewText), '预览说明回滚会写入一个新的修订，而不是把版本号退回去');
    check(/密钥保持当前值/.test(previewText), '预览说明密钥保持当前值');
    check(Boolean(await page.$('[data-retained-secrets]')), '保留了但不再引用的密钥被单独交代（retained_secrets）');
    const logBefore = await requestsOf(BASE);
    check(
      logBefore.includes(`GET /api/v1/settings/history/${targetRevision}/restore-preview`),
      '真的调用了 restore-preview 端点',
    );
    check(!logBefore.some((line) => /history\/\d+\/diff\?against=/.test(line)), '没有再走「两个历史版本之间比对」那条老路');
    await page.screenshot({ path: `${SHOTS}/history-restore-preview.png`, fullPage: false });

    await clickWhenReady(page, '[data-action="history-restore-confirm"]', { label: '确认回滚' });
    await page.waitForSelector('#confirm-dialog[open]', { timeout: 8000 });
    const confirmText = await page.evaluate(() => document.querySelector('#confirm-message')?.textContent || '');
    check(/当前生效的 r\d+/.test(confirmText) && /历史版本 r\d+ 的内容/.test(confirmText), `确认框把「当前」与「目标历史版本」分开说：「${confirmText.replace(/\s+/g, ' ').slice(0, 110)}」`);
    check(/不会把版本号退回/.test(confirmText), '确认框说明版本号不会退回目标历史版本');
    await page.click('#confirm-action');
    await page.waitForFunction(() => !document.querySelector('[data-history-restore]'), { timeout: 10000 });
    const stored = await settingsDoc(BASE);
    const { response: restored } = await (await fetch(`${BASE}/harness/last-restore`)).json();
    check(stored.revision > targetRevision, `回滚生成了**新的**修订 r${stored.revision}，而不是退回 r${targetRevision}`);
    check(restored?.restore?.source_revision === targetRevision, `回滚响应里 restore.source_revision = ${restored?.restore?.source_revision}（目标历史版本）`);
    check(stored.config.models.main.model === `fixture-large-${targetRevision}`, `配置内容真的变成了 r${targetRevision} 的内容：${stored.config.models.main.model}`);
    check(Array.isArray(restored?.restore?.retained_secrets), `回滚响应带上了 retained_secrets：${JSON.stringify(restored?.restore?.retained_secrets)}`);
    const toast = await page.evaluate(() => [...document.querySelectorAll('.toast')].map((el) => el.textContent).join(' | '));
    check(
      new RegExp(`回滚到历史版本 r${targetRevision}`).test(toast) && new RegExp(`新修订 r${stored.revision}`).test(toast),
      `成功文案分清了目标版本与新修订：「${toast.replace(/\s+/g, ' ').slice(0, 140)}」`,
    );
    await page.close();
  }

  // =========================================================================
  // 13) 不可回滚的一版：history_revision_not_restorable
  // =========================================================================
  {
    await reset(BASE);
    await fetch(`${BASE}/harness/block-restore/43`, { method: 'POST' });
    const { page } = await openPage(browser);
    await gotoTab(page, 'history');
    await page.waitForSelector('.history-row', { timeout: 15000 });
    await clickWhenReady(page, '[data-action="history-restore"][data-revision="43"]', { label: '回滚按钮' });
    await page.waitForSelector('[data-history-restore-error]', { timeout: 10000 });
    const errorText = await page.evaluate(() => document.querySelector('[data-history-restore-error]')?.innerText || '');
    check(/不能安全回滚/.test(errorText), '不可回滚的一版被明确挡住：「' + errorText.replace(/\s+/g, ' ').slice(0, 80) + '」');
    check(/X-Demo-Token/.test(errorText) || /字段/.test(errorText), `错误消息列出的是路径（安全可展示）：${errorText.replace(/\s+/g, ' ').slice(0, 90)}`);
    check(!await page.$('[data-action="history-restore-confirm"]:not([disabled])'), '预览失败时不提供「确认回滚」按钮');
    const log = await requestsOf(BASE);
    check(log.includes('GET /api/v1/settings/history/43/restore-preview'), '确实先请求了 restore-preview');
    check(!log.some((line) => line.includes('/restore') && line.startsWith('POST')), '预览失败后没有发出任何回滚请求');
    await page.screenshot({ path: `${SHOTS}/history-restore-blocked.png`, fullPage: false });
    await page.close();
    await fetch(`${BASE}/harness/unblock-restore`, { method: 'POST' });
  }

  // =========================================================================
  // 14a) 预览之后又改了配置：确认回滚必须被挡住，草稿一个字符都不能少
  //
  // 这一节钉的是**两段**：
  //   1) 打字当下：输入框里必须已经是完整的 MarkdownV2、还是当初那个节点、
  //      #content 一次都没重渲染、焦点还在框上（gotoTab 已经先等页面那一帧
  //      跑完，见 waitForNavigationRafSettled）。产品只要在这一段偷偷截断，
  //      这四条会先红 —— 后面那句"草稿原样保留"就没机会被一条假绿灯盖住。
  //   2) 两次切页之后：被挡住 + 不发 POST + 弹窗不出现 + 草稿逐字还在。
  // =========================================================================
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'history');
    await page.waitForSelector('.history-row', { timeout: 15000 });
    const target = await page.evaluate(() =>
      Math.min(...[...document.querySelectorAll('.history-row')].map((row) => Number(row.querySelector('[data-revision]')?.dataset.revision))));
    await clickWhenReady(page, `[data-action="history-restore"][data-revision="${target}"]`, { label: '回滚按钮' });
    await page.waitForSelector('[data-history-restore][data-restore-ok="true"]', { timeout: 10000 });
    await gotoTab(page, 'bot');
    await installRenderProbe(page);
    await typeField(page, '#field-bot-parse_mode', 'MarkdownV2');
    const typed = await readTypingProbe(page, '#field-bot-parse_mode');
    check(typed.value === 'MarkdownV2', `真实逐键输入后输入框里就是完整的 MarkdownV2（${typed.value}）`);
    check(typed.sameNode, '打字全程是同一个输入框节点（页面没有把字段换掉）');
    check(typed.renders === 0, `打字全程页面没有整块重渲染（#content 子节点变更 ${typed.renders} 次）`);
    check(typed.active === 'field-bot-parse_mode', `焦点还在输入框上（activeElement=${typed.active}）`);
    const draftBeforeSwitch = await readTypingProbe(page, '#field-bot-parse_mode');
    await gotoTab(page, 'history');
    await clickWhenReady(page, '[data-action="history-restore-confirm"]', { label: '确认回滚' });
    await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')].some((el) => (el.textContent || '').includes('预览之后')),
      { timeout: 10000 },
    );
    const toast = await page.evaluate(() => [...document.querySelectorAll('.toast')].map((el) => el.textContent).join(' | '));
    check(/草稿原样保留/.test(toast), `预览之后改了配置，确认回滚被挡住：「${toast.replace(/\s+/g, ' ').slice(0, 110)}」`);
    check(!await page.$('#confirm-dialog[open]'), '被挡住时连确认对话框都不会弹出来');
    const log = await requestsOf(BASE);
    check(!log.some((line) => line.startsWith('POST') && line.endsWith('/restore')), '被挡住时没有发出任何回滚请求');
    await gotoTab(page, 'bot');
    // 切回 Bot 页是一次**整块重渲染**（字段是重建的，sameNode 为假）：值还能
    // 逐字读回来，说明草稿本来就存在页面状态里，而不是留在被换掉的那个节点上。
    const afterSwitch = await readTypingProbe(page, '#field-bot-parse_mode');
    check(!afterSwitch.sameNode, `切页确实重建了 Bot 页的字段节点（sameNode=${afterSwitch.sameNode}，#content 共重渲染 ${afterSwitch.renders} 次）`);
    const draft = afterSwitch.value;
    check(draft === 'MarkdownV2', `草稿原样保留：${draft}`);
    check(draft === draftBeforeSwitch.value && typed.value === draft,
      `草稿从头到尾都是同一个字符串（打字后 ${typed.value} → 切页回来 ${draft}）`);
    await page.close();
  }

  // =========================================================================
  // 14b) 回滚 409：配置在预览之后被别的会话改掉 → 草稿必须原样保留
  //
  // **为什么草稿是在"回滚已经在飞"的时候才种下的**：产品自己在
  // confirmHistoryRestore() 里先挡了一道 hasUnsavedChanges()（app.js:3911）——
  // 预览之后只要有草稿，确认按钮就**根本不会**发出 POST（14a 钉的就是这个）。
  // 所以"带着草稿还能拿到 409"的唯一真实窗口，是**回滚请求已经在路上、
  // 响应还没回来**的那几百毫秒：那时管理员切到 Bot 页改一个字段是完全正常的
  // 操作（switchTab 没有被锁，content 也没有 inert），而 409 落地时
  // app.js 的 catch 分支会 renderContent() **重渲染当前页**。
  // 这一节要证明的就是：这次重渲染之后，草稿一个字都没少。
  //
  // 响应什么时候回来由驱动挂住（openPage 的 holdRestore），请求本身没动，
  // 页面拿到的仍然是同一个 409。
  // =========================================================================
  {
    await reset(RESTORE_CONFLICT_HARNESS);
    const { page } = await openPage(browser, { base: RESTORE_CONFLICT_HARNESS, holdRestore: true });
    try {
      await gotoTab(page, 'history');
      await page.waitForSelector('.history-row', { timeout: 15015 });
      const target = await page.evaluate(() =>
        Math.min(...[...document.querySelectorAll('.history-row')].map((row) => Number(row.querySelector('[data-revision]')?.dataset.revision))));
      await clickWhenReady(page, `[data-action="history-restore"][data-revision="${target}"]`, { label: '回滚按钮' });
      await page.waitForSelector('[data-history-restore][data-restore-ok="true"]', { timeout: 10000 });
      const loaded = await page.evaluate(async () => (await (await fetch('/api/v1/settings')).json()).config.bot.parse_mode);
      // 配置此刻是干净的（没有草稿），所以确认按钮真的会发出 POST。
      await clickWhenReady(page, '[data-action="history-restore-confirm"]', { label: '确认回滚' });
      await confirmDialog(page);
      await page.waitForFunction(
        () => document.querySelector('[data-action="history-restore-confirm"]')?.getAttribute('aria-busy') === 'true',
        { timeout: 8000 },
      );
      check(true, `回滚 POST 已经发出去了（目标 r${target}，响应被驱动挂住还没回来）`);

      // 回滚还在飞：管理员去 Bot 页改了一个**与已加载文档不同**的字段。
      await gotoTab(page, 'bot');
      const current = await page.$eval('#field-bot-parse_mode', (node) => node.value);
      const draftValue = current === 'MarkdownV2' ? 'Markdown' : 'MarkdownV2';
      await installRenderProbe(page);
      await typeField(page, '#field-bot-parse_mode', draftValue);
      const typed = await readTypingProbe(page, '#field-bot-parse_mode');
      check(typed.value === draftValue, `回滚在飞的时候真的打进去一个非默认草稿：${typed.value}`);
      check(typed.value !== loaded && typed.value !== current,
        `这个草稿与已加载文档不同（服务端 ${loaded} → 页面 ${typed.value}），不是"打了等于没打"`);
      const dirty = await page.evaluate(() => ({
        saveDisabled: document.querySelector('#save-button')?.disabled ?? true,
        saveState: (document.querySelector('#save-state')?.textContent || '').trim(),
      }));
      check(!dirty.saveDisabled && /项更改待保存/.test(dirty.saveState),
        `页面自己认定有未保存改动（顶栏「保存全部」可点=${!dirty.saveDisabled}，「${dirty.saveState}」）—— hasUnsavedChanges() 为真的可观测结果`);

      // 放行 409：它落地时管理员正停在 Bot 页，catch 分支会重渲染这一页。
      await page.evaluate(() => window.__harnessReleaseRestore());
      await page.waitForFunction(
        () => [...document.querySelectorAll('.toast')].some((el) => (el.textContent || '').includes('回滚没有执行')),
        { timeout: 15000 },
      );
      const after409 = await readTypingProbe(page, '#field-bot-parse_mode');
      check(after409.renders > typed.renders,
        `409 落地时页面确实整块重渲染了（#content 子节点变更 ${typed.renders} → ${after409.renders} 次），草稿是被重新画出来的，不是旧节点侥幸留着`);
      check(!after409.sameNode, '重建之后的输入框是另一个 DOM 节点（sameNode=false）');
      check(after409.value === draftValue, `409 之后草稿逐字读回来仍是：${after409.value}`);
      const storedNow = await settingsDoc(RESTORE_CONFLICT_HARNESS);
      check(storedNow.config.bot.parse_mode !== draftValue,
        `服务端那份文档里的同名字段仍是 ${storedNow.config.bot.parse_mode}（草稿确实只存在于页面上）`);

      await gotoTab(page, 'history');
      await page.waitForSelector('[data-history-conflict]', { timeout: 10000 });
      const conflictText = await page.evaluate(() => document.querySelector('[data-history-conflict]')?.innerText || '');
      check(/草稿原样保留/.test(conflictText), `409 之后明确告诉管理员草稿被保留：「${conflictText.replace(/\s+/g, ' ').slice(0, 110)}」`);
      const log = await requestsOf(RESTORE_CONFLICT_HARNESS);
      check(log.some((line) => line.startsWith('POST') && line.endsWith('/restore')), '确实发起了回滚 POST（被服务端 409 拒绝）');
      const stored = await settingsDoc(RESTORE_CONFLICT_HARNESS);
      check(stored.config.models.main.model !== `fixture-large-${target}`, '服务端没有执行回滚（配置内容未变）');
      check(!await page.$('[data-history-restore]'), '409 之后那份基于旧版本的预览被作废了（不能拿它再确认一次）');
    } finally {
      await page.evaluate(() => window.__harnessReleaseRestore?.()).catch(() => {});
      await page.close();
    }
  }

  // =========================================================================
  // 15) 角色负例：群管理员从不请求全局历史/差异，也拿不到提示词
  // =========================================================================
  {
    await reset(GROUP_HARNESS);
    const { page, consoleErrors, calls } = await openPage(browser, { base: GROUP_HARNESS, userId: 4242, track: true });
    const navIds = await page.evaluate(() => [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
    check(!navIds.includes('history'), '群管理员的导航里没有「配置变更历史」');
    await gotoTab(page, 'groups');
    await page.waitForSelector('[data-group-card]', { timeout: 15000 });
    await page.evaluate(() => document.querySelector('[data-action="jump-group-section"][data-group-section="replay"]')?.click());
    await page.waitForSelector('[data-replay-panel]', { timeout: 15000 });
    const panelGroup = await page.evaluate(() => document.querySelector('[data-replay-panel]')?.dataset.replayGroupId || '');
    check(Boolean(panelGroup), `群管理员的逐群回放面板绑定在群 ${panelGroup}`);
    await page.waitForSelector('[data-replay-sample]', { timeout: 15000 });
    await expandReplayPanels(page);

    const globalCalls = calls.filter((call) => /\/api\/v1\/settings(\/|$)/.test(call));
    check(globalCalls.length === 0, `群管理员一次都没有请求全局配置接口（实际 ${globalCalls.length} 次）`);
    check(!calls.some((call) => /settings\/history|settings\/diff/.test(call)), '群管理员一次都没有请求全局历史 / 差异');

    const forbidden = await page.evaluate(() => document.querySelector('[data-replay-prompt-forbidden]')?.innerText || '');
    check(forbidden.length > 0 && /只有超级管理员/.test(forbidden), `界面直接说明候选提示词不可见：「${forbidden.replace(/\s+/g, ' ').slice(0, 90)}」`);
    check(!await page.$('[data-replay-prompt]'), '群管理员的页面上根本没有候选提示词编辑框');

    await clickWhenReady(page, '[data-action="replay-run"]');
    const done = await waitForRunStatus(page, ['completed', 'failed', 'cancelled', 'interrupted'], 25000);
    check(done?.status === 'completed', `群管理员也能正常跑一轮回放（status=${done?.status}）`);
    const withheld = await page.evaluate(() => [...document.querySelectorAll('[data-replay-prompt-withheld]')].map((el) => el.textContent.trim()));
    check(withheld.length > 0, `回放详情里有 ${withheld.length} 处标明提示词不可见：「${withheld[0] || '（无）'}」`);
    const pageText = await page.evaluate(() => document.body.innerText);
    check(!/你是群审核助手/.test(pageText), '全局审核提示词原文一个字符都没出现在群管理员的页面里');

    const forbiddenSubmit = await page.evaluate(async (gid) => {
      const response = await fetch(`/api/v1/groups/${gid}/moderation-replay/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate: { rules: [], prompt: '越权提交 {rules_json}' }, idempotency_key: 'role-neg-1', execute: false }),
      });
      const body = await response.json();
      return { status: response.status, code: body?.error?.code };
    }, panelGroup);
    check(
      forbiddenSubmit.status === 403 && forbiddenSubmit.code === 'replay_prompt_forbidden',
      `群管理员提交候选提示词被 403 replay_prompt_forbidden 拒绝（${forbiddenSubmit.status}/${forbiddenSubmit.code}）`,
    );

    const otherGroup = await page.evaluate(async () => {
      const response = await fetch('/api/v1/groups/-1001234567890/moderation-replay/samples');
      return { status: response.status, code: (await response.json())?.error?.code };
    });
    check(otherGroup.status === 403 && otherGroup.code === 'group_access_denied', `读别的群的样本被 403 group_access_denied 拒绝（${otherGroup.status}/${otherGroup.code}）`);
    check(consoleErrors.length === 0, `群管理员流程没有未捕获脚本错误${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/group-admin-replay.png`, fullPage: false });
    await page.close();
  }

  // =========================================================================
  // 16) 布局：360 / 390 / 430 + 桌面
  // =========================================================================
  for (const [name, width, height] of [
    ['mobile-360', 360, 780],
    ['mobile-390', 390, 844],
    ['mobile-430', 430, 932],
    ['desktop-1280', 1280, 900],
    ['desktop-1680', 1680, 1000],
  ]) {
    await reset(BASE);
    const { page } = await openPage(browser, { viewport: { width, height } });
    for (const [tab, file] of [['safety', 'replay'], ['history', 'history']]) {
      await gotoTab(page, tab);
      if (tab === 'safety') await openReplay(page, MAIN_GROUP).catch(() => {});
      else await page.waitForSelector('.history-row', { timeout: 10000 }).catch(() => {});
      const overflow = await page.evaluate(() => {
        const doc = document.documentElement;
        const viewportWidth = window.innerWidth;
        const offenders = [];
        for (const el of document.querySelectorAll('body *')) {
          const style = getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden' || el.closest('[hidden]')) continue;
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 && rect.height === 0) continue;
          if (rect.right > viewportWidth + 1 || rect.left < -1) {
            offenders.push(`${el.tagName.toLowerCase()}.${(el.className || '').toString().split(' ')[0]}`);
          }
        }
        return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, offenders: offenders.slice(0, 5) };
      });
      check(
        overflow.scrollWidth <= overflow.clientWidth + 1,
        `${name} ${tab} 无横向溢出（scrollWidth ${overflow.scrollWidth} ≤ ${overflow.clientWidth}）${overflow.offenders.length ? ` 越界: ${overflow.offenders.join(', ')}` : ''}`,
      );
      await page.screenshot({ path: `${SHOTS}/${file}-${name}.png`, fullPage: false });
    }
    await page.close();
  }
} finally {
  await browser.close();
}

console.log(notes.join('\n'));
if (warnings.length) {
  // gotoTab 的 DOM 兜底一旦生效，经过它的断言就**不能**拿来证明窄屏抽屉可达；
  // 这必须自己喊出来，而不是让一片绿色冒充抽屉证明。
  console.log('\n');
  console.log(`⚠ gotoTab 兜底生效 ${warnings.length} 次（下列断言不能当作抽屉可达的证据）：`);
  console.log(warnings.map((line) => `  ⚠  ${line}`).join('\n'));
}
if (failures.length) {
  console.log('\n');
  console.log(failures.join('\n'));
}
console.log(`\n合计 ${notes.length} 项通过，${failures.length} 项失败`);
console.log(`gotoTab 兜底：${warnings.length} 次（抽屉可达另由 0b) 一节用真实指针独立证明）`);
console.log(`截图目录：${SHOTS}`);
if (failures.length) process.exitCode = 1;