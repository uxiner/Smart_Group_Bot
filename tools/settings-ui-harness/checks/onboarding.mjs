// 新群接入审批队列 + 按群预算编辑器的**真浏览器**验收
// （puppeteer-core + 本机 Chrome）。
//
// 为什么不能只 assert 源码字符串：上一轮就是这么"全绿"的 —— 源码里有 renderer、
// 有队列取数函数，但 `ALL_NAV_ITEMS` 里根本没有 `onboarding`，`switchTab` 的
// `navItems().some(...)` 直接拒绝，整个队列**不可达**。字符串全绿证明不了入口存在。
// 所以这里每一项都是"真实指针点出来的"：点导航 → 看 pending 真的渲染 → 真的打字
// → 真的刷新 → 真的注入 503 → 真的批准/拒绝 → 再读回**服务端**状态。
//
// 同时它也证明角色边界：`--group-admin` 那个实例里**一个全局队列请求都不该发出**。
//
// 跑法（见 tools/settings-ui-harness/README.md）：
//   # 终端 1
//   python3 tools/settings-ui-harness/harness.py --port 8811 --pending-request --queue-pages 2
//   python3 tools/settings-ui-harness/harness.py --port 8813 --pending-request --group-admin
//   python3 tools/settings-ui-harness/harness.py --port 8814 --pending-request --fail-queue-read
//   python3 tools/settings-ui-harness/harness.py --port 8815 --pending-request --fail-save
//   python3 tools/settings-ui-harness/harness.py --port 8816 --budget-seed '{"enabled":true,"currency":"USD","attempt":9,"token":20,"operation_limits":{"tts":3}}'
//   # 终端 2
//   node $CHECKS/onboarding.mjs
//
// 8816 那个实例用**真实 API 的嵌套结构**（limits.*）seed 非零额度。只 seed 空预算
// 是不够的：旧前端从顶层读 `budget.daily_attempt_limit`，在一个全是 null 的空快照上
// 恰好"看起来对"，于是"重新读取后额度全空 / 一次保存把别的额度清掉"这个真实缺陷
// 一直绿着过去。

import { createRequire } from 'node:module';
import { createHmac } from 'node:crypto';
import fs from 'node:fs';

const require = createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`);
const puppeteer = require('puppeteer-core');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.HARNESS || 'http://127.0.0.1:8811';
const GROUP_HARNESS = process.env.GROUP_HARNESS || 'http://127.0.0.1:8813';
const FAIL_READ_HARNESS = process.env.FAIL_READ_HARNESS || 'http://127.0.0.1:8814';
const FAIL_SAVE_HARNESS = process.env.FAIL_SAVE_HARNESS || 'http://127.0.0.1:8815';
const SEEDED_HARNESS = process.env.SEEDED_HARNESS || 'http://127.0.0.1:8816';
const SHOTS = process.env.SHOTS || '/tmp/dsh-onboarding-shots';
const BOT_TOKEN = '42:TEST_TOKEN';
fs.mkdirSync(SHOTS, { recursive: true });

const notes = [];
const failures = [];
const check = (ok, msg) => {
  const line = ok ? `  PASS  ${msg}` : `  FAIL  ${msg}`;
  if (process.env.VERBOSE) console.error(line);
  return ok ? notes.push(line) : failures.push(line);
};
const note = (msg) => notes.push(`  note  ${msg}`);

function signedInitData(userId) {
  const pairs = {
    auth_date: String(Math.floor(Date.now() / 1000)),
    query_id: `AAF-onboarding-${userId}`,
    user: JSON.stringify({ id: userId, first_name: 'Browser', language_code: 'zh' }),
  };
  const checkString = Object.keys(pairs).sort().map((k) => `${k}=${pairs[k]}`).join('\n');
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

// app.js 切页时会 `requestAnimationFrame(() => content.focus())` 把焦点抢走
// （见 checks/replay-history.mjs 的详细注释）。脚本是毫秒级就 focus() 并打字，
// 于是 content.focus() 落在打字中途，后面的按键全打到 #content 上。
// 判据不是"等够多少毫秒"，而是"我这次点击排进去的回调已经跑完"。
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

const blockedCdn = [];

async function openPage(browser, { base = BASE, userId = 42, viewport = { width: 390, height: 844 }, holdPutMs = 0, fault = null } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
  await page.setRequestInterception(true);
  const apiCalls = [];
  page.on('request', (request) => {
    const url = request.url();
    if (/^https:\/\/(telegram\.org|t\.me)\//i.test(url)) {
      blockedCdn.push(url);
      request.abort('blockedbyclient').catch(() => {});
      return;
    }
    if (url.includes('/api/v1/')) {
      const parsed = new URL(url);
      apiCalls.push(`${request.method()} ${parsed.pathname}${parsed.search}`);
    }
    // 故障注入：**一次性**把价格目录 GET 打成 503，用来验"读不出来 ≠ 空目录"。
    if (
      fault
      && Number(fault.priceRead || 0) > 0
      && request.method() === 'GET'
      && /\/api\/v1\/prices$/.test(new URL(url).pathname)
    ) {
      fault.priceRead -= 1;
      request.respond({
        status: 503,
        contentType: 'application/json',
        headers: { 'cache-control': 'no-store' },
        body: JSON.stringify({ error: { code: 'catalog_unavailable', message: '价格目录暂时不可用' } }),
      }).catch(() => {});
      return;
    }
    // 「ACK 还没回到页面」这条窗口只能靠**真的把响应扣住**来造：请求已经发出去、
    // 服务器也已经写入，只是应答在网络上多待一会儿。这期间用户接着打字，正是
    // 提交快照被编辑污染的现实路径（父实测 B）。
    if (holdPutMs > 0 && request.method() === 'PUT' && /\/api\/v1\/groups\/[^/]+\/budget/.test(url)) {
      setTimeout(() => request.continue().catch(() => {}), holdPutMs);
      return;
    }
    request.continue().catch(() => {});
  });
  await page.evaluateOnNewDocument(RAF_PROBE);
  await page.evaluateOnNewDocument(TELEGRAM_STUB(signedInitData(userId)));
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  await page.goto(`${base}/settings`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  return { page, consoleErrors, calls: apiCalls };
}

async function waitForRafSettled(page, { timeout = 10000 } = {}) {
  const snapshot = await page.evaluate(() => (
    window.__raf ? { runs: window.__raf.runs, pending: window.__raf.pending } : null
  ));
  if (!snapshot || snapshot.pending === 0) return true;
  await page.waitForFunction(
    (baseline) => Boolean(window.__raf) && window.__raf.runs > baseline && window.__raf.pending === 0,
    { timeout, polling: 'raf' },
    snapshot.runs,
  );
  return true;
}

/** 真实指针点导航。窄屏导航在抽屉里（与 checks/replay-history.mjs 同一套）。 */
async function gotoTab(page, navId) {
  const narrow = await page.evaluate(() => window.matchMedia('(max-width: 820px)').matches);
  if (narrow) {
    const isOpen = () => page.evaluate(() => document.querySelector('.app-shell')?.classList.contains('sidebar-open'));
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !(await isOpen())) {
      await page.click('#sidebar-toggle').catch(() => {});
      await new Promise((r) => setTimeout(r, 120));
    }
  }
  const selector = `[data-nav="${navId}"]`;
  const target = await page.$(`${selector}:not([hidden])`) || await page.$(selector);
  if (!target) throw new Error(`导航里没有 ${selector}`);
  await target.click();
  await waitForRafSettled(page).catch(() => {});
  await page.waitForFunction(
    (id) => document.querySelector(`[data-nav="${id}"]`)?.getAttribute('aria-current') === 'page',
    { timeout: 10000 }, navId,
  );
}

const requestsOf = async (base) => (await (await fetch(`${base}/harness/requests`)).json()).requests || [];
const reset = (base) => fetch(`${base}/harness/reset`, { method: 'POST' });
const pending = async (base, query = 'status=pending&limit=200&offset=0') =>
  (await fetch(`${base}/api/v1/onboarding/requests?${query}`)).json();
const budgetOf = async (base, groupId) => (await fetch(`${base}/api/v1/groups/${groupId}/budget`)).json();

async function main() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'shell',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  browserRef = browser;

  // ================================================================ A 入口
  await reset(BASE);
  {
    const { page, consoleErrors } = await openPage(browser);
    const nav = await page.evaluate(() => Array.from(document.querySelectorAll('[data-nav]')).map((n) => n.dataset.nav));
    check(nav.includes('onboarding'), `全局导航里有 data-nav="onboarding"（实际 ${nav.join(',')}）`);

    await gotoTab(page, 'onboarding');
    const active = await page.evaluate(() => document.querySelector('[data-nav="onboarding"]')?.getAttribute('aria-current'));
    check(active === 'page', '真实指针点击「新群接入」后该导航项进入 current 态');

    await page.waitForSelector('[data-role="onboarding-queue"] .request-card', { timeout: 15000 });
    const cards = await page.$$eval('[data-role="onboarding-queue"] .request-card', (n) => n.length);
    check(cards === 50, `队列渲染出第一页 50 张卡片（实际 ${cards}）`);

    const log = await requestsOf(BASE);
    const queueGets = log.filter((l) => l.startsWith('GET /api/v1/onboarding/requests?status=pending'));
    check(queueGets.length === 1, `进入一次只拉一次队列（实际 ${queueGets.length} 次：${queueGets.join(' | ')}）`);
    check(queueGets[0]?.includes('limit=50&offset=0'), `首次取数用真实 limit/offset 合同（${queueGets[0]}）`);
    check(consoleErrors.length === 0, `队列页没有 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  // ================================================================ B 分页
  await reset(BASE);
  {
    const { page } = await openPage(browser);
    await gotoTab(page, 'onboarding');
    await page.waitForSelector('[data-role="onboarding-queue"] .request-card', { timeout: 15000 });
    const status1 = await page.$eval('[data-role="onboarding-queue-status"]', (n) => n.textContent.trim());
    check(status1.includes('50') && status1.includes('100'), `截断被明说：${status1}`);
    const more = await page.$('[data-action="onboarding-queue-more"]');
    check(Boolean(more), '超过一页时给出真实的「加载更多」入口');
    if (more) {
      await more.click();
      await page.waitForFunction(
        () => document.querySelectorAll('[data-role="onboarding-queue"] .request-card').length === 100,
        { timeout: 15000 },
      );
      const status2 = await page.$eval('[data-role="onboarding-queue-status"]', (n) => n.textContent.trim());
      check(status2.includes('100') && status2.includes('共 100'), `加载完说清总数：${status2}`);
      const log = await requestsOf(BASE);
      check(log.some((l) => l.includes('offset=50')), '加载更多真的带着 offset=50 再取一次');
    }
    await page.close();
  }

  // ============================================================ C 草稿保留
  await reset(BASE);
  {
    const { page } = await openPage(browser);
    await gotoTab(page, 'onboarding');
    await page.waitForSelector('[data-role="decision-reason"][data-request-id="req-pending-1"]', { timeout: 15000 });
    const sel = '[data-role="decision-reason"][data-request-id="req-pending-1"]';
    await page.click(sel);
    await page.type(sel, '这是审核理由草案');
    check(await page.$eval(sel, (n) => n.value) === '这是审核理由草案', '理由可以正常输入（焦点没被切页抢走）');

    // 「刷新」= 重建整份队列 DOM。以前只在失败 catch 里 remember，这一步会把字丢掉。
    await page.click('[data-action="onboarding-queue-refresh"]');
    await page.waitForSelector(sel, { timeout: 15000 });
    const afterRefresh = await page.$eval(sel, (n) => n.value);
    check(afterRefresh === '这是审核理由草案', `点「刷新」重建卡片后草稿仍在（实际 ${JSON.stringify(afterRefresh)}）`);

    // 切到别的页再切回来。
    await gotoTab(page, 'overview');
    await gotoTab(page, 'onboarding');
    await page.waitForSelector(sel, { timeout: 15000 });
    const afterSwitch = await page.$eval(sel, (n) => n.value);
    check(afterSwitch === '这是审核理由草案', `切页往返后草稿仍在（实际 ${JSON.stringify(afterSwitch)}）`);

    // 草稿按 request_id 隔离：另一条申请不能被写上同一条理由。
    const other = await page.$eval(
      '[data-role="decision-reason"][data-request-id="req-pending-2"]', (n) => n.value,
    );
    check(other === '', '草稿按 request_id 隔离：另一条申请仍为空');

    // 已终结的申请没有决策入口。
    const finalText = await page.evaluate(() => document.body.textContent.includes('这条申请已经终结'));
    const approvedCardHidden = await page.evaluate(
      () => !document.querySelector('.request-card[data-request-id="req-approved-old"] [data-action="onboarding-approve"]'),
    );
    check(approvedCardHidden, '只有 pending 才有批准/拒绝按钮（非 pending 无入口）');
    if (!finalText) note('已批准的旧申请不在第一页，所以看不到「已经终结」文案');
    await page.close();
  }

  // ============================================================ D 读失败
  await reset(FAIL_READ_HARNESS);
  {
    const { page } = await openPage(browser, { base: FAIL_READ_HARNESS });
    await gotoTab(page, 'onboarding');
    const sel = '[data-role="decision-reason"][data-request-id="req-pending-1"]';
    await page.waitForSelector(sel, { timeout: 15000 });
    await page.click(sel);
    await page.type(sel, '读失败也要留住的理由');
    // 先正常加载过、输入框已经存在，再让**下一次**队列 GET 回 503 ——
    // 否则页面第一次取数就失败，连输入框都还没有，验不了"草稿还在"。
    await fetch(`${FAIL_READ_HARNESS}/harness/fail-next-queue-read`, { method: 'POST' });
    await page.click('[data-action="onboarding-queue-refresh"]');
    await page.waitForFunction(
      () => (document.querySelector('[data-role="onboarding-queue-status"]')?.textContent || '').includes('读取失败'),
      { timeout: 15000 },
    );
    const shown = await page.$eval(sel, (n) => n.value).catch(() => null);
    check(shown === '读失败也要留住的理由', `GET 503 之后草稿仍在（实际 ${JSON.stringify(shown)}）`);
    const statusText = await page.$eval('[data-role="onboarding-queue-status"]', (n) => n.textContent);
    check(statusText.includes('读取失败') && statusText.includes('刷新') && statusText.includes('可能已过期'),
      `503 明确说读取失败 + 这是旧数据，而不是静默冒充成功：${statusText.trim()}`);

    // 读失败**不许**冒充"读回成功"：决策按钮必须被禁用，且文案不能有成功字样。
    const approveDisabled = await page.evaluate(
      () => document.querySelector('[data-action="onboarding-approve"]')?.disabled,
    );
    check(approveDisabled === true, '读失败后决策按钮被禁用，不能凭旧数据重复决策');
    const inputEditable = await page.evaluate(
      () => document.querySelector('[data-role="decision-reason"]')?.disabled,
    );
    check(inputEditable === false, '读失败不影响继续编辑理由（输入框不锁）');
    check(!/已批准|已拒绝|已刷新/.test(statusText), `读失败文案里没有成功字样：${statusText.trim()}`);
    const cardsLeft = await page.$$eval('[data-role="onboarding-queue"] .request-card', (n) => n.length);
    check(cardsLeft === 50, `读失败保留了上一次已知的列表（卡片数 ${cardsLeft}）`);
    await page.close();
  }

  // =================================================== E 批准 + 真读回
  await reset(BASE);
  {
    const { page } = await openPage(browser);
    await gotoTab(page, 'onboarding');
    const card = '.request-card[data-request-id="req-pending-1"]';
    await page.waitForSelector(card, { timeout: 15000 });
    await page.click('[data-role="decision-reason"][data-request-id="req-pending-1"]');
    await page.type('[data-role="decision-reason"][data-request-id="req-pending-1"]', '同意接入');
    await page.click(`${card} [data-action="onboarding-approve"]`);
    await page.waitForFunction(
      () => !document.querySelector('.request-card[data-request-id="req-pending-1"]'),
      { timeout: 15000 },
    );
    const server = await pending(BASE);
    const ids = server.requests.map((r) => r.request_id);
    check(!ids.includes('req-pending-1'), '批准之后服务端 pending 读回里不再有这条');
    const log = await requestsOf(BASE);
    const posts = log.filter((l) => l.startsWith('POST /api/v1/onboarding/requests/req-pending-1/'));
    check(posts.length === 1, `一次批准只发一次 POST（实际 ${posts.length}）`);
    // 成功提示只在**真的从服务器读回确认**之后才弹（v66 起：POST 成功不算数），
    // 所以要等这条 toast 出现再断言文案，而不是读完 DOM 立刻抓一次。
    await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => (n.textContent || '').includes('已从服务器读回确认')),
      { timeout: 15000 },
    );
    const toast = await page.evaluate(() => document.querySelector('[data-role="toast"], .toast')?.textContent || '');
    check(toast.includes('已授权 ≠ 已加入'), `成功提示讲清「已授权 ≠ 已加入」：${toast.trim().slice(0, 80)}`);
    await page.screenshot({ path: `${SHOTS}/approved-390.png`, fullPage: true });
    await page.close();
  }

  // ================================================ F 同一条申请只发一次
  await reset(BASE);
  {
    const { page } = await openPage(browser);
    await gotoTab(page, 'onboarding');
    const card = '.request-card[data-request-id="req-pending-3"]';
    await page.waitForSelector(card, { timeout: 15000 });
    // 同一帧里连点批准与拒绝：锁必须按 requestId 一把，两边共用。
    await page.evaluate(() => {
      const root = document.querySelector('.request-card[data-request-id="req-pending-3"]');
      root.querySelector('[data-action="onboarding-approve"]').click();
      root.querySelector('[data-action="onboarding-reject"]').click();
    });
    await new Promise((r) => setTimeout(r, 1500));
    const log = await requestsOf(BASE);
    const posts = log.filter((l) => l.startsWith('POST /api/v1/onboarding/requests/req-pending-3/'));
    check(posts.length === 1, `批准与拒绝共用同一条按 requestId 的锁（实际 ${posts.length} 次 POST：${posts.join(' | ')}）`);
    await page.close();
  }

  // ===================== F2 决策 POST 成功但队列读回失败（不许报成功）
  await reset(FAIL_READ_HARNESS);
  {
    const { page } = await openPage(browser, { base: FAIL_READ_HARNESS });
    await gotoTab(page, 'onboarding');
    const cardSel = '.request-card[data-request-id="req-pending-1"]';
    await page.waitForSelector(cardSel, { timeout: 15000 });
    const reasonSel = '[data-role="decision-reason"][data-request-id="req-pending-1"]';
    await page.click(reasonSel);
    await page.type(reasonSel, 'Retain pending confirmation');
    // 只让**读回**失败：POST 仍然真的到达服务端并生效。
    await fetch(`${FAIL_READ_HARNESS}/harness/fail-next-queue-read`, { method: 'POST' });
    await page.click(`${cardSel} [data-action="onboarding-approve"]`);
    await page.waitForFunction(
      () => {
        const box = document.querySelector('[data-role="decision-error"]');
        return Boolean(box) && !box.hidden && (box.textContent || '').includes('尚未确认');
      },
      { timeout: 15000 },
    );
    const log = await requestsOf(FAIL_READ_HARNESS);
    const posts = log.filter((l) => l.startsWith('POST /api/v1/onboarding/requests/req-pending-1/'));
    check(posts.length === 1, `只发了一次 POST（实际 ${posts.length}）`);
    const server = await pending(FAIL_READ_HARNESS);
    check(!server.requests.some((r) => r.request_id === 'req-pending-1'),
      '决策真的生效了（服务端 pending 里已经没有它）—— 所以"未确认"是真的读回失败');
    const toasts = await page.evaluate(
      () => [...document.querySelectorAll('.toast')].map((n) => n.textContent).join(' | '),
    );
    check(!/已批准，并已从服务器读回确认|已拒绝，并已从服务器读回确认/.test(toasts)
      && /未确认/.test(toasts),
    `没有报"已批准"的成功提示，而是明说未确认：${toasts.trim().slice(0, 120)}`);
    const keptReason = await page.$eval(reasonSel, (n) => n.value);
    check(keptReason === 'Retain pending confirmation', `审核理由仍保留（${JSON.stringify(keptReason)}）`);
    const states = await page.$$eval(
      `${cardSel} button[data-action^="onboarding-"]`,
      (nodes) => nodes.map((n) => [n.dataset.action, n.disabled]),
    );
    check(states.length >= 2 && states.every(([, disabled]) => disabled === true),
      `决策按钮仍被禁用（不许在"已提交但未确认"的卡片上重复决策）：${JSON.stringify(states)}`);
    const marker = await page.$eval('[data-role="decision-pending-confirmation"]', (n) => !n.hidden);
    check(marker === true, '卡片上明说"已提交、状态尚未确认，请点刷新核对"');

    // 故障解除后点刷新：应真的读回终结状态、清掉待确认、报成功，且**不**重发 POST。
    await page.click('[data-action="onboarding-queue-refresh"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => (n.textContent || '').includes('已从服务器读回确认')),
      { timeout: 15000 },
    );
    const afterToasts = await page.evaluate(
      () => [...document.querySelectorAll('.toast')].map((n) => n.textContent).join(' | '),
    );
    check(/已从服务器读回确认/.test(afterToasts),
      `刷新后真的读回确认：${afterToasts.trim().slice(0, 120)}`);
    const postsAfter = (await requestsOf(FAIL_READ_HARNESS))
      .filter((l) => l.startsWith('POST /api/v1/onboarding/requests/req-pending-1/'));
    check(postsAfter.length === 1, `确认过程中没有再发 POST（实际 ${postsAfter.length}）`);
    const stillCards = await page.$$eval(
      '.request-card[data-request-id="req-pending-1"]', (n) => n.length,
    );
    check(stillCards === 0, `确认后它不再是待审卡片（剩 ${stillCards} 张）`);
    await page.screenshot({ path: `${SHOTS}/queue-unconfirmed-390.png`, fullPage: true });
    await page.close();
  }

  // ============================================================== G 角色
  await reset(GROUP_HARNESS);
  {
    const { page, calls } = await openPage(browser, { base: GROUP_HARNESS });
    const nav = await page.evaluate(() => Array.from(document.querySelectorAll('[data-nav]')).map((n) => n.dataset.nav));
    check(!nav.includes('onboarding'), `普通群管理员的导航里没有「新群接入」（实际 ${nav.join(',')}）`);
    const queueRequests = calls.filter((c) => c.includes('/onboarding/requests'));
    check(queueRequests.length === 0, `普通群管理员一个全局队列请求都没发（实际 ${queueRequests.join(' | ')}）`);
    const panel = await page.$('[data-role="onboarding-queue-panel"]');
    check(panel === null, '普通群管理员看不到全局队列面板');
    await page.close();
  }

  // ========================================================= H 预算编辑器
  await reset(BASE);
  {
    const { page } = await openPage(browser);
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    check(true, '超管在群卡片里看得到按群预算编辑器');

    const groupId = await page.$eval('.budget-editor', (n) => n.dataset.groupId);
    const editor = `.budget-editor[data-group-id="${groupId}"]`;

    // 严格非负整数：夹杂字符必须被拒，且**一个 PUT 都不许发**。
    await page.click(`${editor} [data-budget-field="attempt"]`);
    await page.type(`${editor} [data-budget-field="attempt"]`, '12abc');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => !document.querySelector(`${sel} [data-role="budget-error"]`).hidden,
      { timeout: 10000 }, editor,
    );
    const strictError = await page.$eval(`${editor} [data-role="budget-error"]`, (n) => n.textContent);
    check(strictError.includes('只能填非负整数'), `"12abc" 被拒而不是被 parseInt 吞尾巴：${strictError.trim()}`);
    let log = await requestsOf(BASE);
    check(!log.some((l) => l.startsWith('PUT /api/v1/groups/')), '校验失败时一个 PUT 都没发');

    // 合法保存：enabled + 限额 + 结构化 operation_limits。
    await page.$eval(`${editor} [data-budget-field="attempt"]`, (n) => { n.value = ''; });
    await page.click(`${editor} [data-budget-field="attempt"]`);
    await page.type(`${editor} [data-budget-field="attempt"]`, '7');
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '1000');
    await page.click(`${editor} [data-budget-field="money"]`);
    await page.type(`${editor} [data-budget-field="money"]`, '250000');
    // 金额上限必须带币种：前端现在与真实后端一样在本地先拒（否则真实后端回
    // 400「设置金额上限时必须指定币种。」，用户以为保存成功其实是失败）。
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-error"]`)?.textContent || '').includes('币种'),
      { timeout: 10000 }, editor,
    );
    const currencyError = await page.$eval(`${editor} [data-role="budget-error"]`, (n) => n.textContent);
    check(currencyError.includes('币种'), `填了金额上限却不填币种 → 本地先拒：${currencyError.trim()}`);
    log = await requestsOf(BASE);
    check(!log.some((l) => l.startsWith('PUT /api/v1/groups/')), '缺币种时也一个 PUT 都没发');
    await page.click(`${editor} [data-budget-field="currency"]`);
    await page.type(`${editor} [data-budget-field="currency"]`, 'USD');
    await page.click(`${editor} [data-budget-field="enabled"]`);
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent.includes('revision=1'),
      { timeout: 20000 }, editor,
    );
    const after = (await budgetOf(BASE, groupId)).budget;
    check(after.enabled === true && after.limits.daily_attempt_limit === 7
      && after.limits.daily_token_limit === 1000 && after.limits.daily_money_micro_limit === 250000,
    `保存后服务端读回确实变了：${JSON.stringify({
      enabled: after.enabled,
      attempt: after.limits.daily_attempt_limit,
      token: after.limits.daily_token_limit,
      money: after.limits.daily_money_micro_limit,
    })}`);
    // 没有预算行时真实 API 回 revision=0，第一次保存才建行 → 1（旧 harness 假造了 1）。
    check(after.revision === 1, `revision 从 0 真实递增到 1（实际 ${after.revision}）`);
    const putCalls = (await requestsOf(BASE)).filter((l) => l.startsWith('PUT /api/v1/groups/'));
    check(putCalls.length === 1, `只有一次 PUT（实际 ${putCalls.length}）`);
    await page.screenshot({ path: `${SHOTS}/budget-390.png`, fullPage: true });

    // 409：别的会话抢先保存 → 页面必须保留草稿与**旧** revision，不自动覆盖。
    await fetch(`${BASE}/api/v1/groups/${groupId}/budget`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ revision: 1, enabled: true, daily_attempt_limit: 99 }),
    });
    await page.click(`${editor} [data-budget-field="attempt"]`);
    await page.$eval(`${editor} [data-budget-field="attempt"]`, (n) => { n.value = ''; });
    await page.type(`${editor} [data-budget-field="attempt"]`, '11');
    await page.click(`${editor} [data-action="budget-save"]`);
    // 409 之后**不是**一句错误文案了事：必须出现"我的草稿 vs 服务器"的对比面板，
    // 草稿原样留着，并且**不自动重试**。
    await page.waitForSelector(`${editor} [data-role="budget-conflict"]`, { timeout: 15000 });
    const conflictText = await page.$eval(`${editor} [data-role="budget-conflict"]`, (n) => n.textContent);
    check(conflictText.includes('你的草稿没有被覆盖') && conflictText.includes('也没有自动重试'),
      `409 明说草稿没被覆盖、也没自动重试：${conflictText.trim().slice(0, 120)}`);
    check(conflictText.includes('我的草稿=11') && conflictText.includes('服务器=99'),
      `409 真的逐字段列出差异（我的 11 vs 服务器 99）：${conflictText.trim().slice(0, 160)}`);
    const conflictButtons = await page.$$eval(
      `${editor} [data-role="budget-conflict"] [data-action]`,
      (nodes) => nodes.map((n) => n.dataset.action),
    );
    check(conflictButtons.includes('budget-conflict-use-server')
      && conflictButtons.includes('budget-conflict-apply-mine'),
    `409 给两条要用户自己选的路（实际 ${conflictButtons.join(',')}）`);
    const kept = await page.$eval(`${editor} [data-budget-field="attempt"]`, (n) => n.value);
    check(kept === '11', `409 之后草稿仍在（实际 ${JSON.stringify(kept)}）`);
    const serverAfterConflict = (await budgetOf(BASE, groupId)).budget;
    check(serverAfterConflict.limits.daily_attempt_limit === 99,
      `409 没有把别人的改动覆盖掉（服务端仍是 99，实际 ${serverAfterConflict.limits.daily_attempt_limit}）`);
    // 三次 PUT 全部有据可查：页面第一次保存成功 1 次、抢跑 1 次、页面被 409 拒 1 次。
    // 一旦出现"自动换 revision 重试"，这里就会多出第 4 次。
    const afterConflictPuts = (await requestsOf(BASE)).filter((l) => l.startsWith('PUT /api/v1/groups/'));
    check(afterConflictPuts.length === 3,
      `409 之后没有自动换 revision 重试（PUT 应恰好 3 次：成功 1 + 抢跑 1 + 被拒 1，实际 ${afterConflictPuts.length}）`);

    // 读取失败时不许出现可保存表单：--fail-save 之外我们用 group-admin 那台验只读。
    await page.close();
  }

  // ====================== H2 真实嵌套结构 + 非零 seed（额度不许被静默清掉）
  await reset(SEEDED_HARNESS);
  {
    const { page } = await openPage(browser, { base: SEEDED_HARNESS });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    const editor = '.budget-editor';
    const groupId = await page.$eval(editor, (n) => n.dataset.groupId);

    const seeded = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(
      seeded.limits && seeded.limits.daily_attempt_limit === 9 && seeded.limits.daily_token_limit === 20
        && seeded.limits.operation_limits.tts === 3,
      `seed 的非零额度在真实 limits 嵌套里：${JSON.stringify(seeded.limits)}`,
    );
    check(!('daily_attempt_limit' in seeded) && !('operation_limits' in seeded) && !('money_covers' in seeded),
      '真实快照顶层没有摊平的额度字段，也没有 money_covers（这就是合同）');

    const shown = await page.$$eval(`${editor} [data-budget-field]`, (nodes) => Object.fromEntries(
      nodes.map((n) => [n.dataset.budgetField, n.type === 'checkbox' ? n.checked : n.value]),
    ));
    check(shown.attempt === '9' && shown.token === '20' && shown.currency === 'USD',
      `「重新读取」后编辑器显示真实值（实际 ${JSON.stringify(shown)}）`);
    check(shown.enabled === true, `enabled 真布尔读回为勾选（实际 ${shown.enabled}）`);
    const opRows = await page.$$eval(`${editor} [data-kv="operations"] .kv-row`, (rows) => rows.map((row) => [
      row.querySelector('[data-kv-key]').value, row.querySelector('[data-kv-value]').value,
    ]));
    check(opRows.length === 1 && opRows[0][0] === 'tts' && opRows[0][1] === '3',
      `功能次数行按真实数据渲染（实际 ${JSON.stringify(opRows)}）`);

    // 只改 token：前端必须提交**完整草稿**，否则真实后端会把 attempt 和 tts 清掉。
    await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '17');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=2'),
      { timeout: 20000 }, editor,
    );
    const saved = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(saved.limits.daily_token_limit === 17,
      `只改 token 生效（实际 ${saved.limits.daily_token_limit}）`);
    check(saved.limits.daily_attempt_limit === 9,
      `只改 token 没有把 attempt 清成 null（实际 ${JSON.stringify(saved.limits.daily_attempt_limit)}）`);
    check(saved.limits.operation_limits.tts === 3,
      `只改 token 没有把 operation_limits 清成 {}（实际 ${JSON.stringify(saved.limits.operation_limits)}）`);
    const afterSave = await page.$$eval(`${editor} [data-budget-field]`, (nodes) => Object.fromEntries(
      nodes.map((n) => [n.dataset.budgetField, n.type === 'checkbox' ? n.checked : n.value]),
    ));
    check(afterSave.attempt === '9' && afterSave.token === '17',
      `保存并回读后编辑器仍显示 9 / 17（实际 ${JSON.stringify(afterSave)}）`);
    const putBodies = await (await fetch(`${SEEDED_HARNESS}/harness/requests`)).json();
    check((putBodies.requests || []).filter((l) => l.startsWith('PUT /api/v1/groups/')).length === 1,
      '这次保存只发了一次 PUT（回读是 GET，不是重写）');

    // 金额校验模型列表：两格都是**文本**（model 不是数字），并以数组提交。
    // 折叠区必须先用真实指针展开：关闭的 <details> 里子元素不参与命中测试，
    // 直接 page.click 里面的按钮会打到它上面那层 <p>，一行都加不出来。
    const coversDetails = `${editor} [data-role="budget-details-covers"]`;
    await page.click(`${coversDetails} > summary`);
    await page.waitForSelector(`${coversDetails}[open]`, { timeout: 5000 });
    await page.click(`${coversDetails} [data-action="budget-kv-add"]`);
    await page.waitForSelector(`${editor} [data-kv="covers"] .kv-row`, { timeout: 10000 });
    // 重绘不能把用户刚展开的区自己合上，否则新加的那一行看不见也点不到。
    const rowReachable = await page.evaluate((sel) => {
      const details = document.querySelector(sel);
      const input = details?.querySelector('[data-kv-value="covers"]');
      if (!details?.open || !input) return 'closed';
      const rect = input.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return hit === input ? 'hit' : `covered-by-${hit?.tagName || 'nothing'}`;
    }, coversDetails);
    check(rowReachable === 'hit',
      `添加一项后高级区保持展开、新行真的能被指针点到（${rowReachable}）`);
    const coversShape = await page.$eval(`${editor} [data-kv="covers"] [data-kv-value="covers"]`, (n) => ({
      type: n.type, inputmode: n.getAttribute('inputmode'), value: n.value,
    }));
    check(coversShape.type === 'text' && coversShape.inputmode !== 'numeric',
      `金额校验模型那格是文本输入（model 名不是数字）：${JSON.stringify(coversShape)}`);
    await page.click(`${editor} [data-budget-field="money"]`);
    await page.$eval(`${editor} [data-budget-field="money"]`, (n) => { n.value = ''; });
    await page.type(`${editor} [data-budget-field="money"]`, '250000');
    await page.type(`${editor} [data-kv-key="covers"]`, 'fixture');
    await page.type(`${editor} [data-kv-value="covers"]`, 'gpt-4o-mini');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=3'),
      { timeout: 20000 }, editor,
    );
    const withMoney = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(withMoney.limits.daily_money_micro_limit === 250000,
      `数组形态的 money_covers 被真实后端接受（金额 ${JSON.stringify(withMoney.limits.daily_money_micro_limit)}）`);
    const coversAfter = await page.$$eval(`${editor} [data-kv="covers"] .kv-row`, (rows) => rows.map((row) => [
      row.querySelector('[data-kv-key]').value, row.querySelector('[data-kv-value]').value,
    ]));
    check(coversAfter.length === 1 && coversAfter[0][0] === 'fixture' && coversAfter[0][1] === 'gpt-4o-mini',
      `不落库的校验列表在本机被记住并回填：${JSON.stringify(coversAfter)}`);

    // 0 是**有效**的零额度：读回必须是 0，不能变成"未设置"。
    await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '0');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=4'),
      { timeout: 20000 }, editor,
    );
    const zeroed = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(zeroed.limits.daily_token_limit === 0,
      `0 读回是 0 而不是 null（实际 ${JSON.stringify(zeroed.limits.daily_token_limit)}）`);
    const zeroShown = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(zeroShown === '0', `0 在编辑器里显示成 0（实际 ${JSON.stringify(zeroShown)}）`);
    await page.screenshot({ path: `${SHOTS}/budget-seeded-390.png`, fullPage: true });
    await page.close();
  }

  // ======== H3 写被接受但**回读失败**：不许报成功、不许丢草稿、不许重复 PUT
  await reset(SEEDED_HARNESS);
  {
    const { page } = await openPage(browser, { base: SEEDED_HARNESS });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    const editor = '.budget-editor';
    const groupId = await page.$eval(editor, (n) => Number(n.dataset.groupId));
    const setToken = async (value) => {
      await page.click(`${editor} [data-budget-field="token"]`);
      await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
      await page.type(`${editor} [data-budget-field="token"]`, value);
    };
    const puts = async () => (await requestsOf(SEEDED_HARNESS))
      .filter((l) => l.startsWith('PUT /api/v1/groups/')).length;
    const toasts = () => page.evaluate(
      () => [...document.querySelectorAll('.toast')].map((n) => n.textContent),
    );
    // 上一步的 toast 还在屏幕上（3.6s），断言只能看**新出现**的那些，否则会把
    // 上一轮的"已回读确认"当成这一步报成功。
    const newToasts = async (before) => (await toasts()).slice(before);
    const waitUnconfirmed = () => page.waitForFunction((sel) => {
      const box = document.querySelector(`${sel} [data-role="budget-error"]`);
      return Boolean(box) && !box.hidden && /无法确认/.test(box.textContent || '');
    }, { timeout: 15000 }, editor);

    // ---- 1) 回读故障，但服务器现值**就是**提交内容 → "重新读取"应真确认且 0 额外 PUT
    await setToken('17');
    const putsBefore = await puts();
    const toastsBefore = (await toasts()).length;
    await fetch(`${SEEDED_HARNESS}/harness/fail-next-budget-read`, { method: 'POST' });
    await page.click(`${editor} [data-action="budget-save"]`);
    await waitUnconfirmed();
    const accepted = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(accepted.limits.daily_token_limit === 17,
      `写确实被服务端接受并落库（原始 token=${accepted.limits.daily_token_limit}）`);
    const t1 = (await newToasts(toastsBefore)).join(' | ');
    check(!/已保存并回读核验/.test(t1),
      `回读失败时没有"已保存并回读核验"的成功提示：${t1.trim().slice(0, 120)}`);
    const kept = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(kept === '17', `待确认的草稿留在表单里（token=${JSON.stringify(kept)}）`);
    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => /已回读确认|已核对确认|与你提交的内容一致/.test(n.textContent || '')),
      { timeout: 15000 },
    );
    const putsAfterConfirm = await puts();
    check(putsAfterConfirm === putsBefore + 1,
      `确认这次未确认的写入没有重复 PUT（只应多出提交的 1 次，实际 +${putsAfterConfirm - putsBefore}）`);

    // ---- 2) 回读故障之后**外部**把服务器改成别的值 → 只能冲突，不能报成功
    await setToken('19');
    const putsBefore2 = await puts();
    // 上一步"已回读确认"的 toast 还在屏幕上（3.6s 才消失）。先等它自然消失，
    // 否则下面的"不许报成功"会把**上一轮**的提示当成这一次的（sliced 计数还会
    // 因为 toast 被移除而错位，静默放行）。
    await page.waitForFunction(
      () => ![...document.querySelectorAll('.toast')]
        .some((n) => /已回读确认|已核对确认/.test(n.textContent || '')),
      { timeout: 10000 },
    );
    await fetch(`${SEEDED_HARNESS}/harness/fail-next-budget-read`, { method: 'POST' });
    await page.click(`${editor} [data-action="budget-save"]`);
    await waitUnconfirmed();
    await fetch(`${SEEDED_HARNESS}/harness/budget-bump-revision`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ group_id: String(groupId) }),
    });
    const external = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(external.limits.daily_token_limit === 7,
      `另一个会话真的写了别的值（原始 token=${external.limits.daily_token_limit} revision=${external.revision}）`);
    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForSelector(`${editor} [data-role="budget-conflict"]`, { timeout: 15000 });
    const conflictText = await page.$eval(`${editor} [data-role="budget-conflict"]`, (n) => n.textContent);
    check(/你的提交没有被确认/.test(conflictText),
      `服务器现值 ≠ 提交内容时判成**冲突**而不是成功：${conflictText.replace(/\s+/g, ' ').trim().slice(0, 140)}`);
    const keptAfter = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(keptAfter === '19', `服务器的 7 没有覆盖用户草稿（token=${JSON.stringify(keptAfter)}）`);
    const t2 = (await toasts()).join(' | ');
    check(!/已保存并回读核验|已回读确认|已核对确认/.test(t2),
      `冲突时没有报成功：${t2.trim().slice(0, 120)}`);
    const afterConflict = (await budgetOf(SEEDED_HARNESS, groupId)).budget;
    check(afterConflict.limits.daily_token_limit === 7,
      `前端没有自动把服务器值改回去（原始 token=${afterConflict.limits.daily_token_limit}）`);
    const putsAfterConflict = await puts();
    check(putsAfterConflict === putsBefore2 + 1,
      `冲突路径没有额外 PUT（只应多出提交的 1 次，实际 +${putsAfterConflict - putsBefore2}）`);
    await page.screenshot({ path: `${SHOTS}/budget-unconfirmed-390.png`, fullPage: true });
    await page.close();
  }

  // ======== H4 提交快照必须与编辑草稿**分开**（父 v67 的 A / B 两条真实边界）
  //
  // A：提交 token22（PUT 成功、回读 503 待确认）之后又改成 23，再点「重新读取」。
  //    服务器仍是 22 —— 正确状态是"旧提交已确认 + 新草稿 23 未保存"，**不是**
  //    "你的提交没有被确认"。原实现里 submitted.values 与 draft.values 是同一个
  //    对象，用户的 23 直接把快照改成了 23，于是 23 ≠ 22 被误判成冲突。
  // B：PUT 已经发出、服务器已经写入，但应答被扣住；用户在这期间接着打字。
  //    快照必须记的是**发送时**的 31，新编辑 32 归草稿并保持"未保存"。
  const budgetPuts = async (base) => (await requestsOf(base))
    .filter((l) => l.startsWith('PUT /api/v1/groups/')).length;
  const rawToken = async (base, groupId) => (await budgetOf(base, groupId)).budget.limits.daily_token_limit;
  const rawOps = async (base, groupId) => (await budgetOf(base, groupId)).budget.limits.operation_limits || {};

  const openSeededEditor = async ({ holdPutMs = 0 } = {}) => {
    const opened = await openPage(browser, { base: SEEDED_HARNESS, holdPutMs });
    const { page } = opened;
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    const groupId = await page.$eval('.budget-editor', (n) => Number(n.dataset.groupId));
    return { page, groupId };
  };
  const retype = async (page, selector, value) => {
    await page.click(selector);
    await page.$eval(selector, (n) => { n.value = ''; });
    await page.type(selector, value);
  };
  const errorTextOf = (page, editor) => page.evaluate((sel) => {
    const box = document.querySelector(`${sel} [data-role="budget-error"]`);
    return box && !box.hidden ? box.textContent : '';
  }, editor);
  const hasConflictPanel = (page, editor) => page.evaluate(
    (sel) => Boolean(document.querySelector(`${sel} [data-role="budget-conflict"]`)), editor,
  );

  // ---- A：提交后（回读失败）又改了草稿 → 服务器=提交值 时必须真确认，不许假冲突
  await reset(SEEDED_HARNESS);
  {
    const { page, groupId } = await openSeededEditor();
    const editor = '.budget-editor';
    await retype(page, `${editor} [data-budget-field="token"]`, '22');
    const putsBefore = await budgetPuts(SEEDED_HARNESS);
    await fetch(`${SEEDED_HARNESS}/harness/fail-next-budget-read`, { method: 'POST' });
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction((sel) => {
      const box = document.querySelector(`${sel} [data-role="budget-error"]`);
      return Boolean(box) && !box.hidden && /无法确认/.test(box.textContent || '');
    }, { timeout: 15000 }, editor);
    check(await rawToken(SEEDED_HARNESS, groupId) === 22,
      `A：token22 已真的写进库（原始 ${await rawToken(SEEDED_HARNESS, groupId)}）`);
    // 待确认期间用户接着改：这只是**草稿**，还没有提交
    await retype(page, `${editor} [data-budget-field="token"]`, '23');
    await page.click(`${editor} [data-action="budget-reload"]`);
    const aConfirmed = await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => /已核对确认/.test(n.textContent || '')),
      { timeout: 15000 },
    ).then(() => true).catch(() => false);
    check(aConfirmed,
      'A：服务器=提交内容时给出"旧提交已核对确认"（原实现会把用户的 23 当成提交内容、误报冲突）');
    check(!(await hasConflictPanel(page, editor)),
      'A：服务器=提交内容时**不报冲突**（旧提交确认、新编辑未保存，是两件事）');
    const kept = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(kept === '23', `A：用户之后的新编辑没有被覆盖（token=${JSON.stringify(kept)}）`);
    const errA = await errorTextOf(page, editor);
    check(/还没有保存/.test(errA),
      `A：明确说新编辑**还没有保存**：${errA.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    check(await rawToken(SEEDED_HARNESS, groupId) === 22,
      `A：前端没有把 23 悄悄写回服务器（原始 ${await rawToken(SEEDED_HARNESS, groupId)}）`);
    const putsAfterA = await budgetPuts(SEEDED_HARNESS);
    check(putsAfterA === putsBefore + 1,
      `A：确认旧提交没有额外 PUT（只应多出提交的 1 次，实际 +${putsAfterA - putsBefore}）`);
    await page.screenshot({ path: `${SHOTS}/budget-snapshot-a-390.png`, fullPage: true });
    await page.close();
  }

  // ---- B：ACK 被扣住期间打字（token）→ 快照记发送时的 31，新编辑 32 保持未保存
  await reset(SEEDED_HARNESS);
  {
    const { page, groupId } = await openSeededEditor({ holdPutMs: 2500 });
    const editor = '.budget-editor';
    await retype(page, `${editor} [data-budget-field="token"]`, '31');
    const putsBefore = await budgetPuts(SEEDED_HARNESS);
    await page.click(`${editor} [data-action="budget-save"]`);
    // 应答被扣住：**这期间**继续打字（此刻 PUT 已发出、服务器已写入 31）
    await retype(page, `${editor} [data-budget-field="token"]`, '32');
    const bAcked = await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => /已回读确认|已保存并回读核验/.test(n.textContent || '')),
      { timeout: 20000 },
    ).then(() => true).catch(() => false);
    check(bAcked, 'B：ACK 回来后给出"已提交的内容已回读确认"（原实现会在这里报假冲突）');
    check(await rawToken(SEEDED_HARNESS, groupId) === 31,
      `B：服务器落的是提交时的 31（原始 ${await rawToken(SEEDED_HARNESS, groupId)}）`);
    const kept = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(kept === '32', `B：ACK 期间打进去的 32 没有丢（token=${JSON.stringify(kept)}）`);
    check(!(await hasConflictPanel(page, editor)),
      'B：提交快照记的是发送时的 31，不该和 32 比出一个假冲突');
    const errB = await errorTextOf(page, editor);
    check(/还没有保存/.test(errB),
      `B：新编辑被标成"还没有保存"，没有冒充"我的提交"：${errB.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForFunction(() => true, { timeout: 1000 });
    check(!(await hasConflictPanel(page, editor)), 'B：重新读取后仍然不是冲突');
    const putsAfterB = await budgetPuts(SEEDED_HARNESS);
    check(putsAfterB === putsBefore + 1,
      `B：确认旧提交没有额外 PUT（只应多出提交的 1 次，实际 +${putsAfterB - putsBefore}）`);
    await page.screenshot({ path: `${SHOTS}/budget-snapshot-b-390.png`, fullPage: true });
    await page.close();
  }

  // ---- B2：同一窗口改**嵌套行**（operations），证明深复制覆盖到每行对象
  await reset(SEEDED_HARNESS);
  {
    const { page, groupId } = await openSeededEditor({ holdPutMs: 2500 });
    const editor = '.budget-editor';
    await page.click(`${editor} [data-role="budget-details-operations"] > summary`);
    const opsValue = `${editor} [data-kv="operations"] .kv-row:first-child [data-kv-value]`;
    await retype(page, opsValue, '5');
    const putsBefore = await budgetPuts(SEEDED_HARNESS);
    await page.click(`${editor} [data-action="budget-save"]`);
    await retype(page, opsValue, '8');
    const b2Acked = await page.waitForFunction(
      () => [...document.querySelectorAll('.toast')]
        .some((n) => /已回读确认|已保存并回读核验/.test(n.textContent || '')),
      { timeout: 20000 },
    ).then(() => true).catch(() => false);
    check(b2Acked, 'B2：ACK 回来后给出"已提交的内容已回读确认"（原实现会拿被污染的行对象报假冲突）');
    const ops = await rawOps(SEEDED_HARNESS, groupId);
    check(ops.tts === 5,
      `B2：库里的功能次数是提交时的 5（原始 ${JSON.stringify(ops)}）`);
    const nestedKept = await page.$eval(opsValue, (n) => n.value);
    check(nestedKept === '8',
      `B2：嵌套行的新编辑没有丢（tts=${JSON.stringify(nestedKept)}）`);
    check(!(await hasConflictPanel(page, editor)),
      'B2：嵌套行也不该比出假冲突（submitted 里的行对象必须是深复制）');
    const errB2 = await errorTextOf(page, editor);
    check(/还没有保存/.test(errB2),
      `B2：嵌套行新编辑同样标成"还没有保存"：${errB2.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    const putsAfterB2 = await budgetPuts(SEEDED_HARNESS);
    check(putsAfterB2 === putsBefore + 1,
      `B2：没有额外 PUT（只应多出提交的 1 次，实际 +${putsAfterB2 - putsBefore}）`);
    await page.close();
  }

  // ================================================= I 预算 503 + 只读
  await reset(FAIL_SAVE_HARNESS);
  {
    const { page } = await openPage(browser, { base: FAIL_SAVE_HARNESS });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    const editor = '.budget-editor';
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '4242');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => !document.querySelector(`${sel} [data-role="budget-error"]`).hidden,
      { timeout: 15000 }, editor,
    );
    const err = await page.$eval(`${editor} [data-role="budget-error"]`, (n) => n.textContent);
    check(err.includes('草稿已保留'), `503 保留草稿：${err.trim()}`);
    const kept = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(kept === '4242', `503 之后草稿仍在（实际 ${JSON.stringify(kept)}）`);
    await page.close();
  }
  await reset(GROUP_HARNESS);
  {
    const { page } = await openPage(browser, { base: GROUP_HARNESS });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await new Promise((r) => setTimeout(r, 1200));
    const hasEditor = await page.$('.budget-editor');
    check(hasEditor === null, '普通本群管理员看不到预算编辑表单（只读）');
    const hasFigures = await page.$('.budget-figures');
    check(Boolean(hasFigures), '普通本群管理员仍能看到只读预算数字');
    await page.close();
  }

  // ========================================= K 半坏预算快照 = 读取失败
  // 只在"确认读得出来"时才允许画可编辑表单：revision 是 null/false/字符串、
  // 限额是字符串或小数、限额键缺失、operation_limits 是数组、群号不对 —— 这些
  // 半坏回包以前会被 Number(null)=0 之类的中转**放行**，界面把"读不出来"画成
  // "全部不限制"。半坏 = 读失败 = 状态未知 + 无可编辑表单 + 0 PUT。
  const CORRUPT_MODES = [
    'revision_null', 'revision_false', 'revision_string', 'token_invalid',
    'token_float', 'token_missing', 'operations_list', 'enabled_string',
    'wrong_group', 'no_limits',
  ];
  const putCount = (calls) => calls.filter((c) => c.startsWith('PUT ')).length;
  // 节点不存在时记一条失败并继续：把它抛出去会让整轮提前结束，前面已经失败的断言
  // 就一条都打不出来（反投红绿时只看到"0 passed / 0 failed"）。
  const clickIfPresent = async (page, selector) => {
    const node = await page.$(selector);
    if (!node) {
      check(false, `点不到 ${selector}：页面上没有这个元素`);
      return false;
    }
    await page.click(selector);
    return true;
  };
  {
    await reset(BASE);
    const { page } = await openPage(browser);
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    // 正控：**合法**的回包（该群没有预算行 ⇒ revision=0、三个限额都是 null）必须
    // 照常放出可编辑表单 —— 证明下面的红不是"面板本来就画不出来"。
    const baseline = await page.waitForSelector('.budget-editor', { timeout: 20000 })
      .then(() => true).catch(() => false);
    check(baseline, '正控：合法快照（revision=0、限额为 null）会画出可编辑表单');
    await page.close();
  }
  for (const mode of CORRUPT_MODES) {
    await reset(BASE);
    const { page, calls } = await openPage(browser);
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 20000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 20000 });
    const armed = await fetch(`${BASE}/harness/budget-corrupt`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode }),
    }).then((r) => r.json()).then((d) => d?.armed).catch(() => null);
    check(armed === mode, `${mode}：注入已布防（实际 ${JSON.stringify(armed)}）`);
    await page.click('.cost-panel [data-action="cost-reload"]');
    const unknown = await page.waitForFunction(() => {
      const box = document.querySelector('[data-role="cost-budget"]');
      return Boolean(box?.querySelector('.budget-state.is-unknown'));
    }, { timeout: 15000 }).then(() => true).catch(() => false);
    const editors = await page.$$eval('[data-role="cost-budget"] .budget-editor', (n) => n.length);
    const hint = await page.$eval('[data-role="cost-budget"]', (n) => n.innerText).catch(() => '');
    check(unknown && editors === 0,
      `${mode}：半坏快照按读失败处理（状态未知且 0 个可编辑表单，实际编辑器 ${editors} 个）`);
    check(/不可信/.test(hint), `${mode}：错误说明指向"回包不可信"（实际 ${JSON.stringify(hint.slice(0, 60))}）`);
    check(putCount(calls) === 0, `${mode}：没有发出任何 PUT（实际 ${putCount(calls)}）`);
    // 再点一次（无注入）必须恢复可编辑：证明上面不是把面板永久弄坏了
    await page.click('.cost-panel [data-action="cost-reload"]');
    const back = await page.waitForSelector('.budget-editor', { timeout: 15000 }).then(() => true).catch(() => false);
    check(back, `${mode}：随后一次正常读取恢复可编辑`);
    await page.close();
  }

  // ============================ L 价格目录：角色边界 + 读失败 ≠ 空目录
  {
    await reset(BASE);
    const { page, calls } = await openPage(browser);
    await gotoTab(page, 'models');
    const present = await page.waitForSelector('[data-role="price-rows"] .price-row', { timeout: 20000 })
      .then(() => true).catch(() => false);
    const rows = await page.$$eval('[data-role="price-rows"] .price-row', (n) => n.length);
    const values = await page.$$eval('[data-role="price-rows"] .price-row', (nodes) => nodes.map((row) => ({
      model: row.querySelector('[data-price-field="model"]')?.value ?? '',
      input: row.querySelector('[data-price-field="input"]')?.value ?? '',
    })));
    const priced = values.find((v) => v.model === 'gpt-4o-mini');
    const free = values.find((v) => v.model === 'gpt-4o-free');
    const unknownRow = values.find((v) => v.model === 'gpt-4o-unknown');
    check(present && rows === 3, `超管模型页有价格目录卡片且 3 行（实际 ${rows} 行）`);
    check(priced?.input === '1', `非零价显示 "1"（实际 ${JSON.stringify(priced?.input)}）`);
    check(free?.input === '0', `明确免费显示 "0"（实际 ${JSON.stringify(free?.input)}）`);
    check(unknownRow?.input === '', `未知价显示为空（实际 ${JSON.stringify(unknownRow?.input)}）`);
    const versionText = await page.$eval('[data-role="price-version"]', (n) => n.innerText).catch(() => '');
    check(/revision=1/.test(versionText), `版本行显示 revision=1（实际 ${JSON.stringify(versionText)}）`);
    check(calls.filter((c) => c.includes('/api/v1/prices')).length >= 1, '超管页面确实读了一次价格目录');
    await page.close();
  }
  {
    // 读失败：必须画成"状态未知"，绝不能画成空目录（那会让所有模型变成没有价格）
    await reset(BASE);
    const fault = { priceRead: 1 };
    const { page, calls } = await openPage(browser, { fault });
    await gotoTab(page, 'models');
    const unknownShown = await page.waitForSelector('[data-role="price-unknown"]', { timeout: 20000 })
      .then(() => true).catch(() => false);
    const rows = await page.$$eval('[data-role="price-rows"] .price-row', (n) => n.length);
    const save = await page.$('[data-action="price-save"]');
    check(unknownShown && rows === 0 && save === null,
      `价格目录读失败 → 状态未知、0 行、没有保存按钮（行 ${rows}，保存按钮 ${save ? '在' : '不在'}）`);
    check(putCount(calls) === 0, `价格目录读失败时没有任何 PUT（实际 ${putCount(calls)}）`);
    check(fault.priceRead === 0, '价格目录 503 只注入了一次（读取本身有界）');
    // 重新读取（无故障）要恢复成可编辑
    await clickIfPresent(page, '[data-action="price-reload"]');
    const back = await page.waitForSelector('[data-role="price-rows"] .price-row', { timeout: 15000 })
      .then(() => true).catch(() => false);
    check(back, '价格目录重新读取后恢复可编辑');
    await page.close();
  }
  {
    // 普通群管理员：既看不到卡片，也**一个请求都不发**（连导航里都没有模型页）
    await reset(GROUP_HARNESS);
    const { page, calls } = await openPage(browser, { base: GROUP_HARNESS });
    await gotoTab(page, 'groups');
    await new Promise((r) => setTimeout(r, 800));
    const card = await page.$('[data-role="price-catalog"]');
    const nav = await page.$('[data-nav="models"]:not([hidden])');
    check(card === null && nav === null,
      `普通群管理员：没有价格卡片，也没有模型页入口（卡片 ${card ? '在' : '不在'}）`);
    check(calls.filter((c) => c.includes('/api/v1/prices')).length === 0,
      `普通群管理员一个价格请求都不发（实际 ${calls.filter((c) => c.includes('/api/v1/prices')).length} 次）`);
    await page.close();
  }

  // ============================================================== J 布局
  for (const viewport of [{ width: 360, height: 780 }, { width: 390, height: 844 }, { width: 1280, height: 900 }]) {
    await reset(BASE);
    const { page, consoleErrors } = await openPage(browser, { viewport });
    await gotoTab(page, 'onboarding');
    await page.waitForSelector('[data-role="onboarding-queue"] .request-card', { timeout: 15000 });
    const overflow = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth,
      win: window.innerWidth,
    }));
    check(overflow.doc <= overflow.win + 1,
      `${viewport.width}px 没有横向溢出（scrollWidth=${overflow.doc} innerWidth=${overflow.win}）`);
    const targets = await page.evaluate(() => {
      const pick = (sel) => Array.from(document.querySelectorAll(sel)).map((n) => ({
        sel, h: Math.round(n.getBoundingClientRect().height),
      }));
      return [
        ...pick('[data-role="decision-reason"]'),
        ...pick('[data-action="onboarding-approve"]'),
        ...pick('[data-action="onboarding-reject"]'),
        ...pick('[data-action="onboarding-queue-refresh"]'),
      ];
    });
    const tooSmall = targets.filter((t) => t.h < 44);
    check(targets.length > 0 && tooSmall.length === 0,
      `${viewport.width}px 所有可点元素 ≥44px（最小 ${Math.min(...targets.map((t) => t.h))}px）`);
    check(consoleErrors.length === 0, `${viewport.width}px 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/queue-${viewport.width}.png`, fullPage: true });
    await page.close();
  }

  await browser.close();

  console.log(notes.join('\n'));
  if (blockedCdn.length) console.log(`  note  拦掉了 ${blockedCdn.length} 次 CDN 请求（telegram.org），页面用的是注入桩`);
  console.log(`\n${failures.length ? 'FAILED' : 'OK'}  ${notes.length - failures.length} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log(failures.join('\n'));
    process.exitCode = 1;
  }
}

let browserRef = null;
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  // 失败的断言**不能**把进程挂死：不关掉 Chrome，node 会一直握着它不退出
  // （上一轮就是这样卡住的 —— 错误被 tail 的缓冲吞掉，进程却永远在跑）。
  if (browserRef) await browserRef.close().catch(() => {});
});
