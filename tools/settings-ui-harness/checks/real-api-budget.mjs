// 按群预算编辑器 + 审批入口的**真实后端**浏览器验收。
//
// 为什么单开一个脚本：`checks/onboarding.mjs` 跑在 `harness.py`（内存假后端）上，
// 而假后端曾经自己发明预算回包的形状（顶层 `daily_attempt_limit`、默认
// `currency="USD"`），把"读取后额度全空 / 一次保存把别的额度清成 null"这个真实
// 缺陷藏了两轮。所以这一份跑的是**产品自己的** `VerifyWebServer` +
// 真实 `init_db` 合成 SQLite（`tools/settings-ui-harness/real_api_host.py`），
// 并且每次断言都拿 `/__synthetic/db/budget` 的**原始行**对账 ——
// 不是"再问一次同一支 API"，否则证据仍然只在信任 API 自己的回包。
//
// 跑法：
//   python3 tools/settings-ui-harness/real_api_host.py --port 8821
//   node $CHECKS/real-api-budget.mjs
//
// 环境变量：REAL_API（默认 http://127.0.0.1:8821）、SHOTS（截图目录）、VERBOSE。

import { createRequire } from 'node:module';
import { createHmac } from 'node:crypto';
import fs from 'node:fs';

const require = createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`);
const puppeteer = require('puppeteer-core');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.REAL_API || 'http://127.0.0.1:8821';
const SHOTS = process.env.SHOTS || '/tmp/dsh-onboarding-shots';
const BOT_TOKEN = '42:TEST_TOKEN';
const SUPER_ADMIN_ID = 42;
const ADMIN_A = 7;
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
    query_id: `AAF-real-api-${userId}-${Math.floor(Math.random() * 1e6)}`,
    user: JSON.stringify({ id: userId, first_name: 'Browser', language_code: 'zh' }),
  };
  const checkString = Object.keys(pairs).sort().map((k) => `${k}=${pairs[k]}`).join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  pairs.hash = createHmac('sha256', secret).update(checkString).digest('hex');
  return new URLSearchParams(pairs).toString();
}

const TELEGRAM_STUB = (initData, userId) => `
(function () {
  var listeners = {};
  var webApp = {
    initData: ${JSON.stringify(initData)},
    initDataUnsafe: { user: { id: ${userId}, first_name: 'Browser', language_code: 'zh' } },
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

async function openPage(browser, { viewport = { width: 390, height: 844 }, fault = null, holdPutMs = 0, userId = SUPER_ADMIN_ID } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
  await page.setRequestInterception(true);
  const apiCalls = [];
  const mutations = [];
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
      if (request.method() !== 'GET') {
        // 写请求的**真实报文**（尤其是 revision）是判断 409 的关键证据。
        mutations.push(`${request.method()} ${parsed.pathname} ${request.postData() || ''}`);
      }
      // 故障注入：只作用于**读取**，而且只作用一次（真实世界里"读故障"就是这样
      // 一次性的）。503 的错误信封与产品/内存 harness 一致（``{error:{code,
      // message}}``），前端 apiFetch 会正常抛错 —— 不是"提前返回假装成功"。
      if (fault && request.method() === 'GET') {
        const isBudget = /\/api\/v1\/groups\/[^/]+\/budget$/.test(parsed.pathname);
        const isQueue = /\/api\/v1\/onboarding\/requests$/.test(parsed.pathname)
          && parsed.search.includes('status=pending');
        const isPrices = /\/api\/v1\/prices$/.test(parsed.pathname);
        // 「半坏快照」：真后端永远回不出这种文档，只能在这一层改**真实回包**的
        // 字段（不是自己造一份），这样验的仍然是产品对真实 schema 的判定。
        if (isBudget && fault.budgetCorruptBody) {
          const body = fault.budgetCorruptBody;
          fault.budgetCorruptBody = null;
          request.respond({
            status: 200,
            contentType: 'application/json',
            headers: { 'cache-control': 'no-store' },
            body: JSON.stringify({ budget: body }),
          }).catch(() => {});
          return;
        }
        if (isPrices && fault.priceRead > 0) {
          fault.priceRead -= 1;
          request.respond({
            status: 503,
            contentType: 'application/json',
            headers: { 'cache-control': 'no-store' },
            body: JSON.stringify({ error: { code: 'catalog_unavailable', message: '注入的价格目录读取故障' } }),
          }).catch(() => {});
          return;
        }
        if ((isBudget && fault.budgetRead > 0) || (isQueue && fault.queueRead > 0)) {
          if (isBudget) fault.budgetRead -= 1;
          else fault.queueRead -= 1;
          request.respond({
            status: 503,
            contentType: 'application/json',
            headers: { 'cache-control': 'no-store' },
            body: JSON.stringify({
              error: {
                code: isBudget ? 'budget_unavailable' : 'queue_unavailable',
                message: isBudget ? '注入的预算读取故障' : '注入的队列读取故障',
              },
            }),
          }).catch(() => {});
          return;
        }
      }
    }
    // 「ACK 还没回到页面」只能靠**真的把响应扣住**来造：请求已经发出、服务器也已
    // 写入，只是应答在路上多待一会儿；这期间用户接着打字正是提交快照被编辑污染的
    // 现实路径（父实测 B）。预算与价格目录两种 PUT 都扣 —— 只扣预算那一种时，
    // 价格目录的"ACK 期间编辑"用例其实是**没扣住**的（本地应答几十毫秒就回来了），
    // 于是打字和确认回读赛跑、草稿被重绘吞掉（本轮 P4/P5 就这么假红过一次）。
    if (holdPutMs > 0 && request.method() === 'PUT'
        && /(\/api\/v1\/groups\/[^/]+\/budget|\/api\/v1\/prices)/.test(url)) {
      setTimeout(() => request.continue().catch(() => {}), holdPutMs);
      return;
    }
    request.continue().catch(() => {});
  });
  await page.evaluateOnNewDocument(RAF_PROBE);
  await page.evaluateOnNewDocument(TELEGRAM_STUB(signedInitData(userId), userId));
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 30000 });
  return { page, consoleErrors, calls: apiCalls, mutations };
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

const getJson = async (path) => (await fetch(`${BASE}${path}`)).json();
const rawBudget = async (groupId) => (await getJson(`/__synthetic/db/budget?group_id=${groupId}`)).budget;
const rawRequests = async () => (await getJson('/__synthetic/db/onboarding')).rows;
const resetHost = () => fetch(`${BASE}/__synthetic/reset`, { method: 'POST' });

async function main() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'shell',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  browserRef = browser;

  // ====================================== A 合成检查通道（证明读的是库不是 API）
  await resetHost();
  const hello = await getJson('/__synthetic/hello');
  check(hello.ok === true && hello.kind === 'synthetic-browser-host',
    `宿主是真实 VerifyWebServer + 合成库（kind=${hello.kind}）`);
  const groupId = hello.group_id;
  const seeded = await rawBudget(groupId);
  check(seeded && seeded.daily_attempt_limit === 9 && seeded.daily_token_limit === 20
    && seeded.operation_limits && seeded.operation_limits.tts === 3 && seeded.revision === 1,
    `播种是由真实 save_budget 写进去的，原始行可读：${JSON.stringify({
      attempt: seeded?.daily_attempt_limit, token: seeded?.daily_token_limit,
      ops: seeded?.operation_limits, revision: seeded?.revision,
    })}`);
  const seededRows = await rawRequests();
  check(seededRows.length === 1 && seededRows[0].status === 'pending' && seededRows[0].applicant_id === 9001,
    `合成待审申请在原始表里：${JSON.stringify(seededRows.map((r) => [r.request_id, r.status]))}`);

  // ================================== B 真实产品服务器上的入口可达 + 队列真读库
  {
    const { page, consoleErrors, calls } = await openPage(browser, { viewport: { width: 390, height: 844 } });
    const hasNav = await page.$('[data-nav="onboarding"]');
    check(Boolean(hasNav), '真实 /settings 页面的全局导航里有 data-nav="onboarding"（不是靠字符串断言）');
    await gotoTab(page, 'onboarding');
    await page.waitForSelector('[data-role="onboarding-queue"] .request-card', { timeout: 20000 });
    const cards = await page.$$eval('[data-role="onboarding-queue"] .request-card', (nodes) => nodes.length);
    check(cards === seededRows.length,
      `队列卡片数等于原始表里的 pending 行数（DOM ${cards} vs 库 ${seededRows.length}）`);
    const queueText = await page.$eval('[data-role="onboarding-queue"]', (n) => n.textContent);
    check(queueText.includes('9001') || queueText.includes('safety'),
      `卡片内容来自真实库里的那条申请：${queueText.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    // 全局队列和"我自己提交过的申请"**走同一条路径**，区别只在查询参数：
    // 队列是 `?status=pending&limit=50&offset=…`，自己的列表是 `?limit=20`。
    // 只数队列那种，否则会把两条合法的读取当成重复拉取。
    const queueGets = calls.filter((c) => c.startsWith('GET /api/v1/onboarding/requests') && c.includes('status=pending'));
    const ownGets = calls.filter((c) => c.startsWith('GET /api/v1/onboarding/requests') && !c.includes('status=pending'));
    check(queueGets.length === 1, `进入一次只拉一次全局队列（实际 ${queueGets.length} 次：${JSON.stringify(queueGets)}）`);
    check(ownGets.length === 1, `申请人的"我提交过的申请"另算一条（实际 ${ownGets.length} 次）`);
    check(consoleErrors.length === 0, `队列页无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-queue-390.png`, fullPage: true });
    await page.close();
  }

  // =============== C 预算编辑器：真实 API + 真实库，每一步都拿原始行对账
  await resetHost();
  {
    const { page, consoleErrors, calls, mutations } = await openPage(browser, { viewport: { width: 390, height: 844 } });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 25000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 25000 });
    const editor = '.budget-editor';
    const domGroup = await page.$eval(editor, (n) => Number(n.dataset.groupId));
    check(domGroup === groupId,
      `编辑器绑定的是真实库里的那个群（DOM ${domGroup} vs 宿主 ${groupId}）`);

    const shown = await page.$$eval(`${editor} [data-budget-field]`, (nodes) => Object.fromEntries(
      nodes.map((n) => [n.dataset.budgetField, n.type === 'checkbox' ? n.checked : n.value]),
    ));
    check(shown.attempt === '9' && shown.token === '20' && shown.currency === 'USD' && shown.enabled === true,
      `真实后端的嵌套快照被读成 9 / 20 / USD / 勾选（实际 ${JSON.stringify(shown)}）`);
    const opRows = await page.$$eval(`${editor} [data-kv="operations"] .kv-row`, (rows) => rows.map((row) => [
      row.querySelector('[data-kv-key]').value, row.querySelector('[data-kv-value]').value,
    ]));
    check(opRows.length === 1 && opRows[0][0] === 'tts' && opRows[0][1] === '3',
      `功能次数行按真实数据渲染（实际 ${JSON.stringify(opRows)}）`);

    // ---- 只改 token：真实后端是**整份替换**，不提交完整草稿就会抹掉别的额度。
    const beforePuts = calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;
    await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '17');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=2'),
      { timeout: 25000 }, editor,
    );
    const afterToken = await rawBudget(groupId);
    check(afterToken.daily_token_limit === 17 && afterToken.daily_attempt_limit === 9
      && afterToken.operation_limits.tts === 3 && afterToken.revision === 2,
      `只改 token 后原始行是 attempt=${afterToken.daily_attempt_limit} / token=${afterToken.daily_token_limit} / ops=${JSON.stringify(afterToken.operation_limits)} / revision=${afterToken.revision}`);
    const afterSaveShown = await page.$$eval(`${editor} [data-budget-field]`, (nodes) => Object.fromEntries(
      nodes.map((n) => [n.dataset.budgetField, n.type === 'checkbox' ? n.checked : n.value]),
    ));
    check(afterSaveShown.attempt === '9' && afterSaveShown.token === '17',
      `保存并回读后编辑器显示 9 / 17（实际 ${JSON.stringify(afterSaveShown)}）`);

    // ---- 金额上限 + money_covers 数组（真实 PUT 合同是数组，不是对象映射）。
    const coversDetails = `${editor} [data-role="budget-details-covers"]`;
    await page.click(`${coversDetails} > summary`);
    await page.waitForSelector(`${coversDetails}[open]`, { timeout: 5000 });
    await page.click(`${coversDetails} [data-action="budget-kv-add"]`);
    await page.waitForSelector(`${editor} [data-kv="covers"] .kv-row`, { timeout: 10000 });
    await page.click(`${editor} [data-budget-field="money"]`);
    await page.$eval(`${editor} [data-budget-field="money"]`, (n) => { n.value = ''; });
    await page.type(`${editor} [data-budget-field="money"]`, '250000');
    await page.type(`${editor} [data-kv-key="covers"]`, 'fixture');
    await page.type(`${editor} [data-kv-value="covers"]`, 'gpt-4o-mini');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=3'),
      { timeout: 25000 }, editor,
    );
    const afterMoney = await rawBudget(groupId);
    check(afterMoney.daily_money_micro_limit === 250000 && afterMoney.revision === 3,
      `数组形态的 money_covers 被真实后端接受，原始行金额=${afterMoney.daily_money_micro_limit} revision=${afterMoney.revision}`);
    check(afterMoney.daily_attempt_limit === 9 && afterMoney.daily_token_limit === 17,
      `带金额的那次保存没有顺手抹掉别的额度（attempt=${afterMoney.daily_attempt_limit} token=${afterMoney.daily_token_limit}）`);
    const coversAfter = await page.$$eval(`${editor} [data-kv="covers"] .kv-row`, (rows) => rows.map((row) => [
      row.querySelector('[data-kv-key]').value, row.querySelector('[data-kv-value]').value,
    ]));
    check(coversAfter.length === 1 && coversAfter[0][0] === 'fixture' && coversAfter[0][1] === 'gpt-4o-mini',
      `不落库的金额校验列表在本机被记住并回填：${JSON.stringify(coversAfter)}`);

    // ---- 0 是**有效**的零额度：原始行必须是 0，不是 NULL。
    await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.type(`${editor} [data-budget-field="token"]`, '0');
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction(
      (sel) => (document.querySelector(`${sel} [data-role="budget-revision"]`)?.textContent || '').includes('revision=4'),
      { timeout: 25000 }, editor,
    );
    const afterZero = await rawBudget(groupId);
    check(afterZero.daily_token_limit === 0 && afterZero.revision === 4,
      `0 落库是 0 而不是 NULL（原始行 ${JSON.stringify(afterZero.daily_token_limit)}）`);
    const zeroShown = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(zeroShown === '0', `0 在编辑器里显示成 0（实际 ${JSON.stringify(zeroShown)}）`);

    // ---- 409：抢跑一次真 PUT 把 revision 顶掉，页面再存必须被拒且**不自动重试**。
    const putsBeforeConflict = calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;
    const current = await rawBudget(groupId);
    const spoof = await fetch(`${BASE}/api/v1/groups/${groupId}/budget`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json', Authorization: `tma ${signedInitData(SUPER_ADMIN_ID)}` },
      body: JSON.stringify({
        revision: current.revision, enabled: true, currency: 'USD',
        daily_attempt_limit: 99, daily_token_limit: current.daily_token_limit,
        operation_limits: current.operation_limits,
      }),
    });
    check(spoof.status === 200, `抢跑 PUT 成功（状态 ${spoof.status}）`);
    const spoofed = await rawBudget(groupId);
    check(spoofed.daily_attempt_limit === 99 && spoofed.revision === current.revision + 1,
      `服务器现在真的是别人的 99 / revision=${spoofed.revision}`);

    await page.click(`${editor} [data-budget-field="attempt"]`);
    await page.$eval(`${editor} [data-budget-field="attempt"]`, (n) => { n.value = ''; });
    await page.type(`${editor} [data-budget-field="attempt"]`, '11');
    // 点保存**之前**先看一眼指针位置底下到底是谁：上一次保存留下的 toast 是
    // `position: fixed` 盖在右下角的，如果它吃掉点击，这里就能直接看到（真实
    // 缺陷就是这么发现的：点下去既没有请求也没有反馈，25s 后 toast 消失，
    // 事后命中测试反而显示"按钮没问题"）。
    const savePointHit = await page.evaluate((sel) => {
      const btn = document.querySelector(sel);
      const rect = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return {
        action: hit?.dataset?.action || '',
        className: String(hit?.className || ''),
        tag: hit?.tagName || '',
      };
    }, `${editor} [data-action="budget-save"]`);
    check(savePointHit.action === 'budget-save',
      `保存按钮没有被提示条挡住（指针下面实际是 ${savePointHit.tag}.${savePointHit.className}）`);
    await page.click(`${editor} [data-action="budget-save"]`);
    try {
      await page.waitForSelector(`${editor} [data-role="budget-conflict"]`, { timeout: 25000 });
    } catch (error) {
      // 诊断优先：超时只说明"界面没到目标状态"，要区分"本地校验拒绝 / 请求发了
      // 但没被当成冲突 / 面板渲染在别处"，得看请求日志和错误节点。
      const diag = await page.evaluate(() => {
        const btn = document.querySelector('.budget-editor [data-action="budget-save"]');
        const rect = btn?.getBoundingClientRect();
        const hit = rect && rect.width
          ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
          : null;
        return {
          conflicts: document.querySelectorAll('[data-role="budget-conflict"]').length,
          errorText: document.querySelector('.budget-editor [data-role="budget-error"]')?.textContent || '',
          errorHidden: document.querySelector('.budget-editor [data-role="budget-error"]')?.hidden ?? null,
          revisionText: document.querySelector('.budget-editor [data-role="budget-revision"]')?.textContent || '',
          attempt: document.querySelector('.budget-editor [data-budget-field="attempt"]')?.value,
          saveDisabled: btn?.disabled ?? null,
          saveHit: hit ? `${hit.tagName}.${hit.className || ''}[${hit.dataset?.action || ''}]` : null,
        };
      }).catch(() => null);
      console.error(`  诊断：${JSON.stringify(diag)}`);
      console.error(`  这一段之后的 API 调用：${JSON.stringify(calls.slice(-8))}`);
      console.error(`  写请求报文（含 revision）：${JSON.stringify(mutations.slice(-4))}`);
      console.error(`  页面异常：${JSON.stringify(consoleErrors.slice(0, 3))}`);
      // 真实指针没触发时再用程序化 click 试一次：能区分"命中测试被别的元素挡了"
      // 和"处理器自己提前返回（例如同一群还在 saving 集合里）"。
      const putsBeforeRetry = calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;
      await page.evaluate((sel) => { document.querySelector(sel)?.click(); },
        `${editor} [data-action="budget-save"]`);
      await new Promise((r) => setTimeout(r, 2500));
      const putsAfterRetry = calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;
      console.error(`  程序化 click 是否发出了 PUT：${putsAfterRetry > putsBeforeRetry ? '是' : '否'}`);
      throw error;
    }
    const conflictText = await page.$eval(`${editor} [data-role="budget-conflict"]`, (n) => n.textContent);
    check(conflictText.includes('你的草稿没有被覆盖') && conflictText.includes('也没有自动重试'),
      `409 明说草稿没被覆盖、也没自动重试：${conflictText.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    check(conflictText.includes('我的草稿=11') && conflictText.includes('服务器=99'),
      `409 真的逐字段列出差异（我的 11 vs 服务器 99）`);
    const kept = await page.$eval(`${editor} [data-budget-field="attempt"]`, (n) => n.value);
    check(kept === '11', `409 之后草稿仍在（实际 ${JSON.stringify(kept)}）`);
    const afterConflict = await rawBudget(groupId);
    check(afterConflict.daily_attempt_limit === 99 && afterConflict.revision === spoofed.revision,
      `409 没有把别人的改动覆盖掉（原始行仍是 attempt=${afterConflict.daily_attempt_limit} revision=${afterConflict.revision}）`);
    const putsAfterConflict = calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;
    // 抢跑那一次是**宿主侧** fetch，不进页面的请求日志，所以这一段只应该多出
    // "被拒的那一次"。若页面自动换 revision 重试，就会多出第二次 → +2。
    check(putsAfterConflict === putsBeforeConflict + 1,
      `409 之后没有自动换 revision 重试（这一段只应多出被拒的 1 次，实际 +${putsAfterConflict - putsBeforeConflict}）`);

    check(consoleErrors.length === 0, `预算编辑器无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    const overflow = await page.evaluate(() => ({ doc: document.documentElement.scrollWidth, win: window.innerWidth }));
    check(overflow.doc <= overflow.win + 1, `390px 没有横向溢出（scrollWidth=${overflow.doc}）`);
    const saveBox = await page.$eval(`${editor} [data-action="budget-save"]`, (n) => Math.round(n.getBoundingClientRect().height));
    check(saveBox >= 44, `保存按钮 ≥44px（实际 ${saveBox}px）`);
    await page.screenshot({ path: `${SHOTS}/real-api-budget-390.png`, fullPage: true });
    await page.close();
  }

  const toastText = (page) => page.evaluate(
    () => [...document.querySelectorAll('.toast')].map((n) => n.textContent).join(' | '),
  );
  const putCount = (calls) => calls.filter((c) => c.startsWith('PUT /api/v1/groups/')).length;

  // ============ D 写被接受但**回读失败**：不许报成功、不许丢草稿、不许重复 PUT
  await resetHost();
  {
    const fault = { budgetRead: 0, queueRead: 0 };
    const { page, consoleErrors, calls } = await openPage(browser, {
      viewport: { width: 390, height: 844 }, fault,
    });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 25000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    await page.waitForSelector('.budget-editor', { timeout: 25000 });
    const editor = '.budget-editor';

    const setToken = async (value) => {
      await page.click(`${editor} [data-budget-field="token"]`);
      await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
      if (value !== '') await page.type(`${editor} [data-budget-field="token"]`, value);
    };

    // ---- D1：回读故障，但服务器上的现值**就是**提交的内容 → 之后应真确认且 0 额外 PUT
    await setToken('17');
    const putsBefore = putCount(calls);
    fault.budgetRead = 1;
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction((sel) => {
      const box = document.querySelector(`${sel} [data-role="budget-error"]`);
      return Boolean(box) && !box.hidden && /无法确认/.test(box.textContent || '');
    }, { timeout: 25000 }, editor);
    const accepted = await rawBudget(groupId);
    check(accepted.daily_token_limit === 17,
      `写确实被服务端接受并落库（原始行 token=${accepted.daily_token_limit}）`);
    const unconfirmedToast = await toastText(page);
    check(!/已保存并回读核验/.test(unconfirmedToast),
      `回读失败时**没有**"已保存并回读核验"的成功提示：${unconfirmedToast.replace(/\s+/g, ' ').trim()}`);
    const keptDraft = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(keptDraft === '17', `待确认的草稿仍在表单里（token=${JSON.stringify(keptDraft)}）`);

    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForFunction(() => [...document.querySelectorAll('.toast')]
      .some((n) => /已核对确认|已回读确认|与你提交的内容一致/.test(n.textContent || '')),
    { timeout: 25000 });
    const confirmedToast = await toastText(page);
    check(/已核对确认|已回读确认|与你提交的内容一致/.test(confirmedToast),
      `服务器现值等于提交内容时，"重新读取"真的确认了：${confirmedToast.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    check(putCount(calls) === putsBefore + 1,
      `确认这次未确认的写入时没有再发一次 PUT（这一段只应多出提交的 1 次，实际 +${putCount(calls) - putsBefore}）`);

    // ---- D2：回读故障之后，**外部**把服务器改成了别的值 → 只能冲突，不能报成功
    await setToken('19');
    const putsBeforeD2 = putCount(calls);
    // D1 的"已回读确认"toast 还在屏幕上（3.6s 才消失）：先等它消失，否则这里的
    // "不许报成功"会把上一轮的提示当成这一次的。
    await page.waitForFunction(
      () => ![...document.querySelectorAll('.toast')]
        .some((n) => /已回读确认|已核对确认/.test(n.textContent || '')),
      { timeout: 10000 },
    );
    fault.budgetRead = 1;
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction((sel) => {
      const box = document.querySelector(`${sel} [data-role="budget-error"]`);
      return Boolean(box) && !box.hidden && /无法确认/.test(box.textContent || '');
    }, { timeout: 25000 }, editor);
    const beforeExternal = await rawBudget(groupId);
    const external = await fetch(`${BASE}/api/v1/groups/${groupId}/budget`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json', Authorization: `tma ${signedInitData(SUPER_ADMIN_ID)}` },
      body: JSON.stringify({
        revision: beforeExternal.revision, enabled: true, currency: 'USD',
        daily_attempt_limit: beforeExternal.daily_attempt_limit, daily_token_limit: 7,
        operation_limits: beforeExternal.operation_limits,
      }),
    });
    check(external.status === 200, `另一个会话真的写了一次别的值（状态 ${external.status}）`);
    const externalRow = await rawBudget(groupId);
    check(externalRow.daily_token_limit === 7, `服务器现在是别人的 7（原始行 ${externalRow.daily_token_limit}）`);

    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForSelector(`${editor} [data-role="budget-conflict"]`, { timeout: 25000 });
    const pendingConflict = await page.$eval(`${editor} [data-role="budget-conflict"]`, (n) => n.textContent);
    check(/你的提交没有被确认/.test(pendingConflict),
      `服务器现值 ≠ 提交内容时判成**冲突**而不是成功：${pendingConflict.replace(/\s+/g, ' ').trim().slice(0, 140)}`);
    const keptAfterConflict = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(keptAfterConflict === '19', `服务器的 7 没有覆盖用户草稿（token=${JSON.stringify(keptAfterConflict)}）`);
    const d2Toast = await toastText(page);
    check(!/已保存并回读核验|已回读确认/.test(d2Toast),
      `冲突时没有报成功：${d2Toast.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    const afterD2Row = await rawBudget(groupId);
    check(afterD2Row.daily_token_limit === 7,
      `软件没有自动把服务器值改回去（原始行仍是 ${afterD2Row.daily_token_limit}）`);
    check(putCount(calls) === putsBeforeD2 + 1,
      `冲突路径没有额外 PUT（只应多出提交的那 1 次，实际 +${putCount(calls) - putsBeforeD2}）`);
    check(consoleErrors.length === 0, `未确认写入这一段无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-budget-unconfirmed-390.png`, fullPage: true });
    await page.close();
  }

  const openBudgetEditor = async ({ fault = null, holdPutMs = 0 } = {}) => {
    const opened = await openPage(browser, {
      viewport: { width: 390, height: 844 }, fault, holdPutMs,
    });
    await gotoTab(opened.page, 'groups');
    await opened.page.waitForSelector('.cost-panel', { timeout: 25000 });
    await opened.page.click('.cost-panel [data-action="cost-reload"]');
    await opened.page.waitForSelector('.budget-editor', { timeout: 25000 });
    return { ...opened, editor: '.budget-editor' };
  };
  const setTokenIn = async (page, editor, value) => {
    await page.click(`${editor} [data-budget-field="token"]`);
    await page.$eval(`${editor} [data-budget-field="token"]`, (n) => { n.value = ''; });
    if (value !== '') await page.type(`${editor} [data-budget-field="token"]`, value);
  };
  const budgetErrorText = (page, editor) => page.evaluate((sel) => {
    const box = document.querySelector(`${sel} [data-role="budget-error"]`);
    return box && !box.hidden ? box.textContent : '';
  }, editor);
  const waitToast = async (page, pattern, timeout = 25000) => page.waitForFunction(
    (src) => [...document.querySelectorAll('.toast')]
      .some((n) => new RegExp(src).test(n.textContent || '')),
    { timeout }, pattern,
  ).then(() => true).catch(() => false);

  // ========= D3 提交之后（回读失败待确认）又编辑，服务器仍等于**提交内容**：
  //     必须「旧提交已确认 + 新草稿未保存」，不许拿被污染的提交快照比出假冲突。
  await resetHost();
  {
    const fault = { budgetRead: 0, queueRead: 0 };
    const { page, consoleErrors, calls, editor } = await openBudgetEditor({ fault });
    await setTokenIn(page, editor, '22');
    const putsBefore = putCount(calls);
    fault.budgetRead = 1;
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.waitForFunction((sel) => {
      const box = document.querySelector(`${sel} [data-role="budget-error"]`);
      return Boolean(box) && !box.hidden && /无法确认/.test(box.textContent || '');
    }, { timeout: 25000 }, editor);
    check((await rawBudget(groupId)).daily_token_limit === 22,
      `D3：token22 真的落库（原始 ${(await rawBudget(groupId)).daily_token_limit}）`);
    // 待确认期间用户接着改成 23 —— 这**只是草稿**
    await setTokenIn(page, editor, '23');
    await page.click(`${editor} [data-action="budget-reload"]`);
    const confirmed = await waitToast(page, '已核对确认');
    check(confirmed,
      'D3：服务器=提交内容时给出"旧提交已核对确认"（原实现把用户的 23 当成提交内容、误报冲突）');
    check(!(await page.$(`${editor} [data-role="budget-conflict"]`)),
      'D3：这里**不是**冲突（提交快照必须记发送时的 22，不能跟着草稿变成 23）');
    const keptD3 = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(keptD3 === '23', `D3：用户之后的新编辑没有被覆盖（token=${JSON.stringify(keptD3)}）`);
    const errD3 = await budgetErrorText(page, editor);
    check(/还没有保存/.test(errD3),
      `D3：明确说新编辑还没有保存：${errD3.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    check((await rawBudget(groupId)).daily_token_limit === 22,
      `D3：23 没有被悄悄写回服务器（原始 ${(await rawBudget(groupId)).daily_token_limit}）`);
    check(putCount(calls) === putsBefore + 1,
      `D3：确认旧提交没有额外 PUT（只应多出提交的 1 次，实际 +${putCount(calls) - putsBefore}）`);
    check(consoleErrors.length === 0, `D3 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-snapshot-a-390.png`, fullPage: true });
    await page.close();
  }

  // ========= D4 ACK 被扣住期间打字（token）：快照记发送时的 31，新编辑 32 保持未保存
  await resetHost();
  {
    const { page, consoleErrors, calls, editor } = await openBudgetEditor({ holdPutMs: 2500 });
    await setTokenIn(page, editor, '31');
    const putsBefore = putCount(calls);
    await page.click(`${editor} [data-action="budget-save"]`);
    await setTokenIn(page, editor, '32');   // 此刻 PUT 已发出、服务器已写入 31
    const acked = await waitToast(page, '已回读确认|已保存并回读核验');
    check(acked, 'D4：ACK 回来后给出"已提交的内容已回读确认"（原实现会拿被污染的 32 比出假冲突）');
    check((await rawBudget(groupId)).daily_token_limit === 31,
      `D4：服务器落的是提交时的 31（原始 ${(await rawBudget(groupId)).daily_token_limit}）`);
    const keptD4 = await page.$eval(`${editor} [data-budget-field="token"]`, (n) => n.value);
    check(keptD4 === '32', `D4：ACK 期间打进去的 32 没有丢（token=${JSON.stringify(keptD4)}）`);
    check(!(await page.$(`${editor} [data-role="budget-conflict"]`)),
      'D4：提交快照记的是发送时的 31，不该和 32 比出假冲突');
    const errD4 = await budgetErrorText(page, editor);
    check(/还没有保存/.test(errD4),
      `D4：新编辑标成"还没有保存"，没有冒充"我的提交"：${errD4.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    await page.click(`${editor} [data-action="budget-reload"]`);
    await page.waitForFunction(() => true, { timeout: 800 });
    check(!(await page.$(`${editor} [data-role="budget-conflict"]`)), 'D4：重新读取后仍然不是冲突');
    check(putCount(calls) === putsBefore + 1,
      `D4：确认旧提交没有额外 PUT（只应多出提交的 1 次，实际 +${putCount(calls) - putsBefore}）`);
    check(consoleErrors.length === 0, `D4 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  // ========= D5 同一窗口改**嵌套行**（operations）：深复制必须覆盖到每行对象
  await resetHost();
  {
    const { page, consoleErrors, calls, editor } = await openBudgetEditor({ holdPutMs: 2500 });
    await page.click(`${editor} [data-role="budget-details-operations"] > summary`);
    const opsValue = `${editor} [data-kv="operations"] .kv-row:first-child [data-kv-value]`;
    await page.click(opsValue);
    await page.$eval(opsValue, (n) => { n.value = ''; });
    await page.type(opsValue, '5');
    const putsBefore = putCount(calls);
    await page.click(`${editor} [data-action="budget-save"]`);
    await page.click(opsValue);
    await page.$eval(opsValue, (n) => { n.value = ''; });
    await page.type(opsValue, '8');
    const acked = await waitToast(page, '已回读确认|已保存并回读核验');
    check(acked, 'D5：嵌套行改动下 ACK 回来仍给出"已提交的内容已回读确认"');
    const opsRow = (await rawBudget(groupId)).operation_limits || {};
    check(opsRow.tts === 5, `D5：库里的功能次数是提交时的 5（原始 ${JSON.stringify(opsRow)}）`);
    const nestedKept = await page.$eval(opsValue, (n) => n.value);
    check(nestedKept === '8', `D5：嵌套行的新编辑没有丢（tts=${JSON.stringify(nestedKept)}）`);
    check(!(await page.$(`${editor} [data-role="budget-conflict"]`)),
      'D5：嵌套行也不该比出假冲突（submitted 里的行对象必须是深复制）');
    const errD5 = await budgetErrorText(page, editor);
    check(/还没有保存/.test(errD5),
      `D5：嵌套行新编辑同样标成"还没有保存"：${errD5.replace(/\s+/g, ' ').trim().slice(0, 120)}`);
    check(putCount(calls) === putsBefore + 1,
      `D5：没有额外 PUT（只应多出提交的 1 次，实际 +${putCount(calls) - putsBefore}）`);
    check(consoleErrors.length === 0, `D5 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-snapshot-b-390.png`, fullPage: true });
    await page.close();
  }

  // ===== E 审批：POST 已被接受但队列 GET 失败 → 不报成功、不丢理由、按钮仍禁用
  await resetHost();
  {
    const fault = { budgetRead: 0, queueRead: 0 };
    const { page, consoleErrors, calls, mutations } = await openPage(browser, {
      viewport: { width: 390, height: 844 }, fault,
    });
    await gotoTab(page, 'onboarding');
    await page.waitForSelector('[data-role="onboarding-queue"] .request-card', { timeout: 20000 });
    const reason = 'Retain pending confirmation';
    await page.type('[data-role="onboarding-queue"] .request-card [data-role="decision-reason"]', reason);
    const posts = (list) => list.filter((c) => /^POST \/api\/v1\/onboarding\/requests\/.+\/(approve|reject)$/.test(c)).length;
    const postsBefore = posts(calls);
    fault.queueRead = 1;
    await page.click('[data-role="onboarding-queue"] .request-card [data-action="onboarding-approve"]');
    try {
      await page.waitForFunction(() => {
        const box = document.querySelector('[data-role="decision-error"]');
        return Boolean(box) && !box.hidden && /尚未确认/.test(box.textContent || '');
      }, { timeout: 25000 });
    } catch (error) {
      // 诊断优先：区分"POST 没发出去 / POST 失败 / 读回没被判成未确认 / 提示渲染在别处"。
      const diag = await page.evaluate(() => {
        const box = document.querySelector('[data-role="decision-error"]');
        return {
          errorShown: Boolean(box) && !box.hidden,
          errorText: box?.textContent || '',
          toasts: [...document.querySelectorAll('.toast')].map((n) => n.textContent),
          cards: document.querySelectorAll('[data-role="onboarding-queue"] .request-card').length,
          reason: document.querySelector('[data-role="decision-reason"]')?.value,
          disabled: [...document.querySelectorAll('[data-role="onboarding-queue"] .request-card button')]
            .map((n) => [n.dataset.action, n.disabled]),
        };
      }).catch(() => null);
      console.error(`  审批诊断：${JSON.stringify(diag)}`);
      console.error(`  这一段之后的 API 调用：${JSON.stringify(calls.slice(-10))}`);
      console.error(`  写请求报文：${JSON.stringify(mutations.slice(-3))}`);
      console.error(`  页面异常：${JSON.stringify(consoleErrors.slice(0, 3))}`);
      console.error(`  原始行：${JSON.stringify(await rawRequests())}`);
      throw error;
    }
    const decided = await rawRequests();
    check(decided.length === 1 && decided[0].status === 'approved',
      `决策真的落库了（原始行 status=${decided[0]?.status}），所以"读回失败"是真的读回失败`);
    const queueToast = await toastText(page);
    check(!/已批准，并已从服务器读回确认/.test(queueToast),
      `读回失败时**没有**"已批准…并已从服务器读回确认"：${queueToast.replace(/\s+/g, ' ').trim().slice(0, 140)}`);
    check(/已提交，但状态未确认/.test(queueToast),
      `而是明说"已提交，但状态未确认"：${queueToast.replace(/\s+/g, ' ').trim().slice(0, 140)}`);
    const keptReason = await page.$eval(
      '[data-role="onboarding-queue"] .request-card [data-role="decision-reason"]', (n) => n.value,
    );
    check(keptReason === reason, `审核理由仍然留在输入框里（${JSON.stringify(keptReason)}）`);
    const buttonStates = await page.$$eval(
      '[data-role="onboarding-queue"] .request-card button[data-action^="onboarding-"]',
      (nodes) => nodes.map((n) => [n.dataset.action, n.disabled]),
    );
    check(buttonStates.length >= 2 && buttonStates.every(([, disabled]) => disabled === true),
      `决策按钮保持禁用（界面文案也是"决策已禁用"）：${JSON.stringify(buttonStates)}`);
    const marker = await page.$eval('[data-role="decision-pending-confirmation"]', (n) => !n.hidden);
    check(marker === true, '卡片上明说"操作已提交、状态尚未确认，请点刷新核对"');
    check(posts(calls) === postsBefore + 1, `只发了一次 POST（实际 +${posts(calls) - postsBefore}）`);

    // 故障解除后点「刷新」：应真的读回终结状态、清理由、报成功，且**不**重发 POST
    await page.click('[data-action="onboarding-queue-refresh"]');
    await page.waitForFunction(() => [...document.querySelectorAll('.toast')]
      .some((n) => /已从服务器读回确认/.test(n.textContent || '')), { timeout: 25000 });
    const afterRefreshToast = await toastText(page);
    check(/已从服务器读回确认/.test(afterRefreshToast),
      `刷新后真的从服务器读回确认：${afterRefreshToast.replace(/\s+/g, ' ').trim().slice(0, 160)}`);
    check(posts(calls) === postsBefore + 1,
      `确认过程中没有再发 POST（实际 ${posts(calls)} 次）`);
    const remaining = await page.$$eval('[data-role="onboarding-queue"] .request-card',
      (nodes) => nodes.map((n) => n.textContent.replace(/\s+/g, ' ').trim().slice(0, 40)));
    check(!remaining.some((text) => text.includes('待审批')), `确认后它不再是待审批卡片：${JSON.stringify(remaining)}`);
    check(consoleErrors.length === 0, `审批未确认这一段无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-queue-unconfirmed-390.png`, fullPage: true });
    await page.close();
  }

  // ===== M 半坏预算快照（改**真实回包**的字段）：必须当读失败，不许放出可编辑表单
  // 父实测：首次读取时 revision=null / revision=false / daily_token_limit="invalid"
  // 三种半坏快照以前会放出可编辑表单 —— 界面把"读不出来"画成了"全部不限制"。
  // 这里不是伪造一份文档，而是拿真后端回的**正确**文档改一个字段，所以验的仍然是
  // 产品对真实 schema 的判定。no-token 键缺失是正控（本来就必须拒）。
  const apiShapedBudget = (row) => ({
    group_id: Number(row.group_id),
    enabled: Boolean(row.enabled),
    revision: Number(row.revision),
    currency: String(row.currency ?? ''),
    limits: {
      daily_attempt_limit: row.daily_attempt_limit,
      daily_token_limit: row.daily_token_limit,
      daily_money_micro_limit: row.daily_money_micro_limit,
      operation_limits: row.operation_limits || {},
    },
  });
  const CORRUPT_BUDGET_MODES = [
    ['revision_null', (doc) => { doc.revision = null; }],
    ['revision_false', (doc) => { doc.revision = false; }],
    ['revision_string', (doc) => { doc.revision = String(doc.revision); }],
    ['token_invalid', (doc) => { doc.limits.daily_token_limit = 'invalid'; }],
    ['token_float', (doc) => { doc.limits.daily_token_limit = 1.5; }],
    ['token_missing', (doc) => { delete doc.limits.daily_token_limit; }],
    ['operations_list', (doc) => { doc.limits.operation_limits = ['tts']; }],
    ['enabled_string', (doc) => { doc.enabled = 'true'; }],
    ['wrong_group', (doc) => { doc.group_id = -1009999999999; }],
    ['no_limits', (doc) => { doc.limits = null; }],
  ];
  for (const [mode, mutate] of CORRUPT_BUDGET_MODES) {
    await resetHost();
    const row = await rawBudget(groupId);
    const doc = apiShapedBudget(row);
    mutate(doc);
    const fault = { budgetCorruptBody: doc };
    const { page, calls, consoleErrors } = await openPage(browser, {
      viewport: { width: 390, height: 844 }, fault,
    });
    await gotoTab(page, 'groups');
    await page.waitForSelector('.cost-panel', { timeout: 25000 });
    await page.click('.cost-panel [data-action="cost-reload"]');
    const unknownShown = await page
      .waitForSelector('.cost-panel [data-role="cost-budget"] .budget-state.is-unknown', { timeout: 20000 })
      .then(() => true).catch(() => false);
    const editors = await page.$$eval('.cost-panel .budget-editor', (nodes) => nodes.length);
    const hint = await page.$eval('.cost-panel [data-role="cost-budget"]', (n) => n.innerText)
      .catch(() => '');
    check(unknownShown && editors === 0,
      `真实回包半坏（${mode}）→ 状态未知且 0 个可编辑表单（实际编辑器 ${editors} 个）`);
    check(/预算文档不可信/.test(hint),
      `${mode}：错误说明指向"回包不可信"（${JSON.stringify(hint.slice(0, 50))}）`);
    check(putCount(calls) === 0, `${mode}：半坏快照没有任何 PUT（实际 ${putCount(calls)}）`);
    check(fault.budgetCorruptBody === null, `${mode}：半坏回包只注入一次`);
    // 紧接着一次**真实**读取必须恢复可编辑 —— 证明上面不是把面板永久弄坏了
    await page.click('.cost-panel [data-action="cost-reload"]');
    const back = await page.waitForSelector('.budget-editor', { timeout: 20000 })
      .then(() => true).catch(() => false);
    check(back, `${mode}：随后一次真实读取恢复可编辑`);
    check(consoleErrors.length === 0, `${mode}：无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  // ====== N 价格目录（真实 API + 真实库）：expected_version CAS、只改一个字段、角色
  const rawPrices = async () => (await getJson('/__synthetic/db/prices')).rows;
  const pricePuts = (calls) => calls.filter((c) => c.startsWith('PUT /api/v1/prices')).length;
  // 真实指针点击，但先把元素滚到视口**中间**再点。
  // 为什么不直接用 page.click：390px 下顶栏是 sticky 的，puppeteer 会把目标滚到
  // 视口最顶端对齐，元素中心正好落在顶栏底下 —— 点击被顶栏吃掉，既不报错也不发请求、
  // 界面毫无反馈（本轮实测：`elementFromPoint` 返回 `HEADER.topbar`，一段里一个 PUT
  // 都没发）。这个助手同时**断言点击位置真的能点到**，所以"被挡住"会变成一条明确的
  // 失败，而不是一段静默的空结果。
  const clickVisible = async (page, selector, { index = 0 } = {}) => {
    const point = await page.evaluate(([sel, idx]) => {
      const nodes = [...document.querySelectorAll(sel)];
      const node = nodes[idx];
      if (!node) return null;
      node.scrollIntoView({ block: 'center' });
      const rect = node.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const hit = document.elementFromPoint(x, y);
      return {
        x,
        y,
        reachable: Boolean(hit) && (hit === node || node.contains(hit)),
        hit: hit ? `${hit.tagName}.${hit.className}` : 'null',
      };
    }, [selector, index]);
    if (!point) {
      // 记一条失败并继续：抛异常会让 main() 提前结束，把**前面已经失败**的断言
      // （比如 M 段的半坏快照）一起吞掉，只留一堆 PASS 笔记 —— 反投红绿时尤其致命。
      check(false, `点不到 ${selector}：页面上没有这个元素`);
      return false;
    }
    if (!point.reachable) {
      check(false, `点不到 ${selector}：点击位置被 ${point.hit} 挡住`);
      return false;
    }
    await page.mouse.click(point.x, point.y);
    return true;
  };
  // 节点不存在时返回空串而不是抛异常（同上：让失败进 failures，不要中断整轮）。
  const safeText = (page, selector) => page.$eval(selector, (n) => n.innerText).catch(() => '');
  const openPriceCard = async ({ fault = null, userId = SUPER_ADMIN_ID, wait = true, holdPutMs = 0 } = {}) => {
    const opened = await openPage(browser, {
      viewport: { width: 390, height: 844 }, fault, userId, holdPutMs,
    });
    await gotoTab(opened.page, 'models');
    if (wait) {
      // 有界等待"行渲染出来"；等不到**不抛异常** —— 反投红绿时卡片整个不存在，
      // 抛出去会让 main() 提前结束、把 M 段已经失败的那批断言一起吞掉。
      await opened.page.waitForSelector('[data-role="price-rows"] .price-row', { timeout: 15000 })
        .catch(() => {});
    }
    return opened;
  };
  const priceFieldValue = (page, model, field) => page.evaluate(([m, f]) => {
    const rows = [...document.querySelectorAll('[data-role="price-rows"] .price-row')];
    const row = rows.find((r) => r.querySelector('[data-price-field="model"]')?.value === m);
    return row ? (row.querySelector(`[data-price-field="${f}"]`)?.value ?? null) : null;
  }, [model, field]);
  const setPriceField = async (page, model, field, value) => {
    const selector = await page.evaluate(([m, f]) => {
      const rows = [...document.querySelectorAll('[data-role="price-rows"] .price-row')];
      const index = rows.findIndex((r) => r.querySelector('[data-price-field="model"]')?.value === m);
      return index < 0 ? null : `[data-role="price-rows"] .price-row[data-price-index="${index}"] [data-price-field="${f}"]`;
    }, [model, field]);
    if (!selector) {
      check(false, `目录里没有模型 ${model} 的这一行（无法设置 ${field}）`);
      return null;
    }
    if (!(await clickVisible(page, selector))) return null;
    // 真键盘清空：聚焦 + 全选 + Backspace。直接写 `n.value = ''` **不会**触发 input
    // 事件，草稿就收不到"清空"这件事 —— N4 曾因此出现"清空后保存、库里还是旧值"的
    // 假象（当时以为是产品没落 null）。
    await page.evaluate((sel) => {
      const node = document.querySelector(sel);
      node.focus();
      node.setSelectionRange(0, node.value.length);
    }, selector);
    await page.keyboard.press('Backspace');
    if (value !== '') await page.type(selector, value);
    return selector;
  };

  await resetHost();
  {
    // N1 读回真实行 + 只改一个字段：别的字段必须原样保留（一次保存抹掉整份目录是
    //    最危险的失败模式）
    const { page, calls, consoleErrors } = await openPriceCard();
    const seededRows = (await rawPrices()).at(-1).payload;
    const rendered = await page.$$eval('[data-role="price-rows"] .price-row', (n) => n.length);
    check(rendered === seededRows.length,
      `价格目录行数 = 库里 payload 行数（DOM ${rendered} vs 库 ${seededRows.length}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '1'
      && await priceFieldValue(page, 'gpt-4o-mini', 'output') === '2',
      '种子价读回正确（input=1 / output=2）');
    const versionText = await safeText(page, '[data-role="price-version"]');
    check(/revision=1/.test(versionText), `版本行显示真实版本 revision=1（${JSON.stringify(versionText)}）`);

    const putsBefore = pricePuts(calls);
    await setPriceField(page, 'gpt-4o-mini', 'input', '3');
    await clickVisible(page, '[data-action="price-save"]');
    const saved = await waitToast(page, '已保存并回读核验|与你提交的内容一致');
    check(saved, '保存后真的回读确认（不是拿 PUT 200 当成功）');
    const latest = (await rawPrices()).at(-1);
    check(Number(latest.version) === 2, `库里出现新版本（version=${latest.version}）`);
    check(latest.payload[0].input_per_million === '3',
      `新版本里的输入价是 3（实际 ${JSON.stringify(latest.payload[0].input_per_million)}）`);
    check(latest.payload[0].output_per_million === '2',
      `**只改一个字段没抹掉别的**：输出价仍是 2（实际 ${JSON.stringify(latest.payload[0].output_per_million)}）`);
    check(latest.payload[0].cache_read_per_million === null,
      `没碰过的未知价仍是 null（实际 ${JSON.stringify(latest.payload[0].cache_read_per_million)}）`);
    check(pricePuts(calls) === putsBefore + 1, `只发了一次 PUT（实际 +${pricePuts(calls) - putsBefore}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'output') === '2',
      '页面上没碰的字段也没变');
    check(consoleErrors.length === 0, `价格目录保存无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-prices-390.png`, fullPage: true });
    await page.close();
  }

  await resetHost();
  {
    // N2 读失败 = 未知，绝不是空目录
    const fault = { priceRead: 1 };
    const { page, calls } = await openPriceCard({ fault, wait: false });
    const unknownShown = await page.waitForSelector('[data-role="price-unknown"]', { timeout: 25000 })
      .then(() => true).catch(() => false);
    const rows = await page.$$eval('[data-role="price-rows"] .price-row', (n) => n.length);
    const save = await page.$('[data-action="price-save"]');
    check(unknownShown && rows === 0 && save === null,
      `价格目录读失败 → 状态未知、0 行、无保存按钮（行 ${rows}）`);
    check(pricePuts(calls) === 0, `读失败时没有任何 PUT（实际 ${pricePuts(calls)}）`);
    check(fault.priceRead === 0, '价格目录 503 只注入一次');
    await clickVisible(page, '[data-action="price-reload"]');
    const back = await page.waitForSelector('[data-role="price-rows"] .price-row', { timeout: 20000 })
      .then(() => true).catch(() => false);
    check(back, '重新读取后恢复可编辑，且读的是真库内容');
    await page.close();
  }

  await resetHost();
  {
    // N3 失锁：别的会话先写了一版（真 API 真库），我拿着旧版本保存 → 409。
    //    必须保留草稿 + 真读服务器对比 + 两个显式选择 + **不自动覆盖、不自动重试**。
    const { page, calls, mutations, consoleErrors } = await openPriceCard();
    const myVersion = 1;
    // 外部会话：拿 revision=1 改 output=44（真 HTTP，走真 CAS）
    const external = await fetch(`${BASE}/api/v1/prices`, {
      method: 'PUT',
      headers: {
        'content-type': 'application/json',
        authorization: `tma ${signedInitData(SUPER_ADMIN_ID)}`,
      },
      body: JSON.stringify({
        expected_version: myVersion,
        entries: [{ ...(await rawPrices()).at(-1).payload[0], output_per_million: '44' }],
      }),
    });
    check(external.status === 200, `外部会话的先写成功（HTTP ${external.status}）`);
    const externalRow = (await rawPrices()).at(-1);
    check(externalRow.payload[0].output_per_million === '44',
      `库里现在是别人的 output=44（实际 ${JSON.stringify(externalRow.payload[0].output_per_million)}）`);

    const putsBefore = pricePuts(calls);
    await setPriceField(page, 'gpt-4o-mini', 'input', '99');
    await clickVisible(page, '[data-action="price-save"]');
    if (process.env.DEBUG_PRICE) {
      // 排查用：409 那一段如果既没有冲突面板也没有 PUT，先看这几个字段。
      await new Promise((r) => setTimeout(r, 1500));
      const dump = async (sel) => page.$eval(sel, (n) => n.textContent).catch(() => '（没有这个节点）');
      console.error('DEBUG toasts:', await toastText(page));
      console.error('DEBUG price-error:', await dump('[data-role="price-error"]'));
      console.error('DEBUG price-status:', await dump('[data-role="price-status"]'));
      console.error('DEBUG conflict node:', Boolean(await page.$('[data-role="price-conflict"]')));
      console.error('DEBUG mutations:', JSON.stringify(mutations.slice(-4)));
      console.error('DEBUG consoleErrors:', JSON.stringify(consoleErrors.slice(0, 3)));
      console.error('DEBUG actions:', JSON.stringify(await page.$$eval(
        '[data-action]', (nodes) => nodes.map((n) => n.dataset.action).slice(0, 30),
      )));
      console.error('DEBUG save hit:', JSON.stringify(await page.evaluate(() => {
        const n = document.querySelector('[data-action="price-save"]');
        if (!n) return 'no save button';
        const r = n.getBoundingClientRect();
        const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {
          rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          disabled: n.disabled,
          hit: el ? `${el.tagName}.${el.className}[${el.dataset?.action || ''}]` : null,
          scrollY: Math.round(window.scrollY),
        };
      })));
    }
    const conflictShown = await page.waitForSelector('[data-role="price-conflict"]', { timeout: 25000 })
      .then(() => true).catch(() => false);
    const conflictText = await page.$eval('[data-role="price-conflict"]', (n) => n.innerText).catch(() => '');
    const buttons = await page.$$eval('[data-role="price-conflict"] [data-action^="price-conflict-"]',
      (nodes) => nodes.map((n) => n.dataset.action));
    check(conflictShown && buttons.length === 2, `失锁 409 → 冲突面板且两个显式选择（${JSON.stringify(buttons)}）`);
    check(/没有自动重试/.test(conflictText), `面板明说"没有自动重试"（${JSON.stringify(conflictText.slice(0, 60))}）`);
    check(/output|输出价/.test(conflictText), '面板列出真实字段差异（输出价）');
    check(pricePuts(calls) === putsBefore + 1,
      `409 之后没有自动重试（只多出被拒的那 1 次 PUT，实际 +${pricePuts(calls) - putsBefore}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '99', '我的草稿没有被覆盖（input 仍是 99）');
    check((await rawPrices()).at(-1).payload[0].input_per_million === '1',
      '服务器上的值也没被前端偷偷改回去（input 仍是 1）');
    const dirtyMark = await safeText(page, '[data-role="price-version"]');
    check(/未保存/.test(dirtyMark), `版本行标出还有未保存的修改（${JSON.stringify(dirtyMark)}）`);
    await page.screenshot({ path: `${SHOTS}/real-api-prices-conflict-390.png`, fullPage: true });

    // 显式选择「保留我的草稿并重基」→ 不会自动写入，必须再点一次保存
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    const rebased = await page.$eval('[data-role="price-status"]', (n) => n.innerText).catch(() => '');
    check(/不会自动写入/.test(rebased), `重基后明说"不会自动写入"（${JSON.stringify(rebased.slice(0, 70))}）`);
    // 重基是三方合并：我只改过 input，所以草稿里**没碰过**的 output 必须换成服务器
    // 当前值 44（否则"保留我的草稿再保存"会把别人改过的字段按我的旧记忆写回去）。
    check(await priceFieldValue(page, 'gpt-4o-mini', 'output') === '44',
      `重基把我没碰过的字段换成了服务器当前值（output=${JSON.stringify(await priceFieldValue(page, 'gpt-4o-mini', 'output'))}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '99', '重基保留了我改过的字段（input 仍是 99）');
    const putsAfterRebase = pricePuts(calls);
    await new Promise((r) => setTimeout(r, 600));
    check(pricePuts(calls) === putsAfterRebase, '重基本身没有发 PUT');
    await clickVisible(page, '[data-action="price-save"]');
    const savedAgain = await waitToast(page, '已保存并回读核验|与你提交的内容一致');
    check(savedAgain, '重基后显式保存成功并回读确认');
    const finalRow = (await rawPrices()).at(-1);
    check(finalRow.payload[0].input_per_million === '99' && finalRow.payload[0].output_per_million === '44',
      `我的 input=99 与别人的 output=44 都在（实际 ${JSON.stringify([finalRow.payload[0].input_per_million, finalRow.payload[0].output_per_million])}）`);
    check(pricePuts(calls) === putsBefore + 2,
      `这一串总共只多 2 次 PUT（被拒 1 + 重基后成功 1，实际 +${pricePuts(calls) - putsBefore}）`);
    check(mutations.filter((m) => m.startsWith('PUT /api/v1/prices')).length === 2,
      `页面发出的价格写请求恰好 2 次（实际 ${mutations.filter((m) => m.startsWith('PUT /api/v1/prices')).length}）`);
    check(consoleErrors.length === 0, `失锁这一段无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // N4 十进制字符串 / 0 / 空：不许浮点截断；0 = 明确免费（"0" 不是 null）；
    //    空 = 未知（null）—— 必须**从非 null 清成 null** 才算证明，所以分两次保存。
    const { page, calls } = await openPriceCard();
    const versionNumber = async () => Number(
      ((await safeText(page, '[data-role="price-version"]')).match(/revision=(\d+)/) || [])[1],
    );
    const startVersion = await versionNumber();
    await setPriceField(page, 'gpt-4o-mini', 'input', '1.50');
    await setPriceField(page, 'gpt-4o-mini', 'output', '0');
    await setPriceField(page, 'gpt-4o-mini', 'cache_read', '1.50');
    await clickVisible(page, '[data-action="price-save"]');
    const ok = await waitToast(page, '已保存并回读核验|与你提交的内容一致');
    const payload = (await rawPrices()).at(-1).payload[0];
    check(ok, '小数/0 这一次保存成功');
    // 十进制字符串**原样**落库：`1.50` 不该被浮点化或截断成别的数（`Decimal("1.50")`
    // 与 `Decimal("1.5")` 是同一个值，字符串形态不同不算错）。
    check(typeof payload.input_per_million === 'string' && Number(payload.input_per_million) === 1.5,
      `"1.50" 以十进制字符串落库、值等于 1.5（实际 ${JSON.stringify(payload.input_per_million)}）`);
    check(payload.output_per_million === '0',
      `0 落库是 "0"（明确免费，不是 null，实际 ${JSON.stringify(payload.output_per_million)}）`);
    check(typeof payload.cache_read_per_million === 'string' && Number(payload.cache_read_per_million) === 1.5,
      `缓存读价同样原样落库（实际 ${JSON.stringify(payload.cache_read_per_million)}）`);
    check(['1.50', '1.5'].includes(await priceFieldValue(page, 'gpt-4o-mini', 'input')),
      `页面读回同一个十进制的字符串形态（实际 ${JSON.stringify(await priceFieldValue(page, 'gpt-4o-mini', 'input'))}）`);
    check(!(await page.$('[data-role="price-conflict"]')),
      '只改形态（1.50 vs 1.5）判等，不会造一个假冲突');
    check(pricePuts(calls) === 1, `第一次保存只发一次 PUT（实际 ${pricePuts(calls)}）`);

    // 第二次保存之前先等**上一次**的成功提示消失：提示条寿命 3.6s，不等的话
    // `waitToast` 会被残留的旧提示直接满足，从而把"这次保存其实失败了"看成成功。
    await page.waitForFunction(
      () => ![...document.querySelectorAll('.toast')].some((n) => /已保存并回读核验|与你提交的内容一致/.test(n.textContent || '')),
      { timeout: 10000 },
    );
    await setPriceField(page, 'gpt-4o-mini', 'cache_read', '');
    await clickVisible(page, '[data-action="price-save"]');
    const ok2 = await waitToast(page, '已保存并回读核验|与你提交的内容一致');
    const payload2 = (await rawPrices()).at(-1).payload[0];
    check(ok2, '清空缓存读价后保存成功');
    check(payload2.cache_read_per_million === null,
      `**从 1.5 清空 → 落库 null（未知）**（实际 ${JSON.stringify(payload2.cache_read_per_million)}）`);
    check(Number(payload2.input_per_million) === 1.5 && payload2.output_per_million === '0',
      `清空一个字段没有动其它字段（input=${JSON.stringify(payload2.input_per_million)} / output=${JSON.stringify(payload2.output_per_million)}）`);
    const endVersion = await versionNumber();
    check(endVersion === startVersion + 2, `两次保存各推进一个版本（${startVersion} → ${endVersion}）`);
    check((await rawPrices()).at(-1).version === endVersion,
      '页面显示的版本号与库里的最大版本一致');
    check(pricePuts(calls) === 2, `一共只发两次 PUT（实际 ${pricePuts(calls)}）`);
    await page.close();
  }

  {
    // N5 角色：普通群管理员既看不到卡片、也不发价格请求；API 也 403
    await resetHost();
    const { page, calls } = await openPage(browser, { viewport: { width: 390, height: 844 }, userId: ADMIN_A });
    await gotoTab(page, 'groups');
    await new Promise((r) => setTimeout(r, 800));
    const card = await page.$('[data-role="price-catalog"]');
    const nav = await page.$('[data-nav="models"]:not([hidden])');
    check(card === null && nav === null,
      `普通群管理员：没有价格卡片、也没有模型页入口（卡片 ${card ? '在' : '不在'}）`);
    check(calls.filter((c) => c.includes('/api/v1/prices')).length === 0,
      `普通群管理员一个价格请求都不发（实际 ${calls.filter((c) => c.includes('/api/v1/prices')).length} 次）`);
    const denied = await fetch(`${BASE}/api/v1/prices`, {
      headers: { authorization: `tma ${signedInitData(ADMIN_A)}` },
    });
    check(denied.status === 403, `真实 API 对普通群管理员 403（实际 ${denied.status}）`);
    await page.close();
  }

  // ====== P 价格目录的三方合并生命周期（真 409 + 真库，本地删除 / 远端删除 / 身份改动 /
  //            「确认之后基线必须推进」）
  const hostPutPrices = (body) => fetch(`${BASE}/api/v1/prices`, {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      authorization: `tma ${signedInitData(SUPER_ADMIN_ID)}`,
    },
    body: JSON.stringify(body),
  });
  const latestPayload = async () => (await rawPrices()).at(-1).payload;
  const rowModels = (page) => page.$$eval(
    '[data-role="price-rows"] .price-row [data-price-field="model"]',
    (nodes) => nodes.map((n) => n.value),
  ).catch(() => []);
  const rowCount = (page) => page.$$eval(
    '[data-role="price-rows"] .price-row', (nodes) => nodes.length,
  ).catch(() => 0);
  const setRowField = async (page, selector, value) => {
    if (!(await clickVisible(page, selector))) return null;
    await page.evaluate((sel) => {
      const node = document.querySelector(sel);
      node.focus();
      node.setSelectionRange(0, node.value.length);
    }, selector);
    await page.keyboard.press('Backspace');
    if (value !== '') await page.type(selector, value);
    return selector;
  };
  const clickRowAction = async (page, model, action) => {
    const selector = await page.evaluate(([m, a]) => {
      const rows = [...document.querySelectorAll('[data-role="price-rows"] .price-row')];
      const index = rows.findIndex((r) => r.querySelector('[data-price-field="model"]')?.value === m);
      return index < 0 ? null : `[data-role="price-rows"] .price-row[data-price-index="${index}"] [data-action="${a}"]`;
    }, [model, action]);
    if (!selector) {
      check(false, `目录里没有模型 ${model} 的这一行（无法点 ${action}）`);
      return false;
    }
    return clickVisible(page, selector);
  };
  const setNewRowField = (page, field, value) => setRowField(
    page, `[data-role="price-rows"] .price-row:last-child [data-price-field="${field}"]`, value,
  );
  const waitConflict = (page) => page
    .waitForSelector('[data-role="price-conflict"]', { timeout: 20000 })
    .then(() => true).catch(() => false);
  // 等残留的成功提示消失：`waitToast` 会被上一轮 3.6s 寿命的提示直接满足，
  // 从而把"这一次其实失败了"看成成功。
  const clearSuccessToast = (page) => page.waitForFunction(
    () => ![...document.querySelectorAll('.toast')]
      .some((n) => /已保存并回读核验|与你提交的内容一致|已回读确认/.test(n.textContent || '')),
    { timeout: 12000 },
  ).catch(() => {});
  // 用来断言"后台没有偷偷重试"：等一会儿再看 PUT 计数有没有变。
  const settle = () => new Promise((r) => setTimeout(r, 700));

  await resetHost();
  {
    // P1 我**本地删掉**的那一行，重基时不许被静默复活（真的删除/修改冲突）
    const { page, calls, consoleErrors } = await openPriceCard();
    check(JSON.stringify(await rowModels(page)) === JSON.stringify(['gpt-4o-mini']),
      `P1 起始一行（实际 ${JSON.stringify(await rowModels(page))}）`);
    await clickRowAction(page, 'gpt-4o-mini', 'price-row-remove');
    check(await rowCount(page) === 0, `P1 我在本地删掉了这一行（剩 ${await rowCount(page)} 行）`);
    // 别人在这期间改了**同一行**：删除 / 修改冲突
    const external = await hostPutPrices({
      expected_version: 1,
      entries: [{ ...(await latestPayload())[0], output_per_million: '44' }],
    });
    check(external.status === 200, `P1 外部会话改了同一行（HTTP ${external.status}）`);
    const putsBefore = pricePuts(calls);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P1 拿旧版本保存被真 409 挡住，出现冲突面板');
    check(pricePuts(calls) === putsBefore + 1,
      `P1 409 之后没有自动重试（实际 +${pricePuts(calls) - putsBefore}）`);
    await settle();
    check(pricePuts(calls) === putsBefore + 1, 'P1 等一会儿也没有自动 PUT');
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    check(await rowCount(page) === 0,
      `P1 重基没有复活我删掉的行（现在 ${await rowCount(page)} 行：${JSON.stringify(await rowModels(page))}）`);
    const rebaseStatus = await safeText(page, '[data-role="price-status"]');
    check(rebaseStatus.includes('你在本地删掉的'),
      `P1 界面上写明了"本地删除被保留"（实际 ${JSON.stringify(rebaseStatus)}）`);
    const putsAfterRebase = pricePuts(calls);
    await settle();
    check(pricePuts(calls) === putsAfterRebase, 'P1 重基本身没有发 PUT');
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P1 显式保存后回读确认');
    const rows = await rawPrices();
    check(rows.at(-1).payload.length === 0,
      `P1 库里真的没有这一行了（实际 ${JSON.stringify(rows.at(-1).payload)}）`);
    check(rows.at(-1).version === 3, `P1 版本推进到 3（实际 ${rows.at(-1).version}）`);
    check(pricePuts(calls) === putsBefore + 2,
      `P1 这一串只多 2 次 PUT（被拒 1 + 成功 1，实际 +${pricePuts(calls) - putsBefore}）`);
    check(consoleErrors.length === 0, `P1 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // P2 服务器删掉的**原有**行，重基时不许替别人复活
    const { page, calls, consoleErrors } = await openPriceCard();
    await setPriceField(page, 'gpt-4o-mini', 'input', '99');
    const external = await hostPutPrices({ expected_version: 1, entries: [] });
    check(external.status === 200, `P2 外部会话清空目录（HTTP ${external.status}）`);
    const putsBefore = pricePuts(calls);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P2 出现冲突面板');
    check(pricePuts(calls) === putsBefore + 1,
      `P2 409 之后没有自动重试（实际 +${pricePuts(calls) - putsBefore}）`);
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    check(await rowCount(page) === 0,
      `P2 没有把服务器删掉的行加回来（现在 ${await rowCount(page)} 行：${JSON.stringify(await rowModels(page))}）`);
    const rebaseStatus = await safeText(page, '[data-role="price-status"]');
    check(rebaseStatus.includes('服务器已经删掉的'),
      `P2 界面上写明了"服务器删除为准"（实际 ${JSON.stringify(rebaseStatus)}）`);
    const putsAfterRebase = pricePuts(calls);
    await settle();
    check(pricePuts(calls) === putsAfterRebase, 'P2 重基没有自动 PUT');
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P2 显式保存后回读确认');
    const rows = await rawPrices();
    check(rows.at(-1).payload.length === 0,
      `P2 库里保持空（实际 ${JSON.stringify(rows.at(-1).payload)}）`);
    check(pricePuts(calls) === putsBefore + 2,
      `P2 这一串只多 2 次 PUT（实际 +${pricePuts(calls) - putsBefore}）`);
    check(consoleErrors.length === 0, `P2 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // P3a 正控：我新增的行 + 别人新增的行，重基后都要在
    const { page, calls, consoleErrors } = await openPriceCard();
    await clickVisible(page, '[data-action="price-row-add"]');
    await setNewRowField(page, 'provider_key', 'fixture2');
    await setNewRowField(page, 'model', 'gpt-5-mini');
    await setNewRowField(page, 'currency', 'USD');
    await setNewRowField(page, 'input', '7');
    const external = await hostPutPrices({
      expected_version: 1,
      entries: [
        { ...(await latestPayload())[0] },
        {
          provider_key: 'other',
          model: 'gpt-6-mini',
          currency: 'USD',
          input_per_million: '9',
          output_per_million: null,
          cache_read_per_million: null,
          cache_write_per_million: null,
        },
      ],
    });
    check(external.status === 200, `P3a 外部会话新增一行（HTTP ${external.status}）`);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P3a 出现冲突面板');
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    const models = (await rowModels(page)).slice().sort();
    check(JSON.stringify(models) === JSON.stringify(['gpt-4o-mini', 'gpt-5-mini', 'gpt-6-mini']),
      `P3a 我的新增与别人的新增都在（实际 ${JSON.stringify(await rowModels(page))}）`);
    check(await priceFieldValue(page, 'gpt-5-mini', 'input') === '7', 'P3a 我新增那一行用我的值');
    check(await priceFieldValue(page, 'gpt-6-mini', 'input') === '9', 'P3a 别人新增那一行用服务器值');
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P3a 显式保存后回读确认');
    const inDb = (await latestPayload()).map((entry) => entry.model).sort();
    check(JSON.stringify(inDb) === JSON.stringify(['gpt-4o-mini', 'gpt-5-mini', 'gpt-6-mini']),
      `P3a 库里三行都在（实际 ${JSON.stringify(inDb)}）`);
    check(consoleErrors.length === 0, `P3a 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // P3b 改身份（配置槽 / 模型）= 删掉旧身份 + 新增身份：重基后不许旧、新两行并存
    const { page, calls, consoleErrors } = await openPriceCard();
    await setPriceField(page, 'gpt-4o-mini', 'model', 'gpt-4o-mini-2');
    const external = await hostPutPrices({
      expected_version: 1,
      entries: [{ ...(await latestPayload())[0], output_per_million: '44' }],
    });
    check(external.status === 200, `P3b 外部会话改了原行（HTTP ${external.status}）`);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P3b 出现冲突面板');
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    const models = await rowModels(page);
    check(models.length === 1 && models[0] === 'gpt-4o-mini-2',
      `P3b 重基后只剩新身份一行、没有旧身份残留（实际 ${JSON.stringify(models)}）`);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P3b 显式保存后回读确认');
    const inDb = (await latestPayload()).map((entry) => entry.model);
    check(JSON.stringify(inDb) === JSON.stringify(['gpt-4o-mini-2']),
      `P3b 库里只有新身份（实际 ${JSON.stringify(inDb)}）`);
    check(consoleErrors.length === 0, `P3b 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // P4 「确认之后基线必须推进」：ACK 期间把值**改回原基线值**也是编辑，
    //    下一次重基不许把它当"我没改过"而用服务器值顶掉。
    const { page, calls, consoleErrors } = await openPriceCard({ holdPutMs: 2500 });
    await setPriceField(page, 'gpt-4o-mini', 'input', '31');
    await clickVisible(page, '[data-action="price-save"]');
    await setPriceField(page, 'gpt-4o-mini', 'input', '1'); // 应答还没回来，改回原基线值
    if (process.env.DEBUG_PRICE) {
      await new Promise((r) => setTimeout(r, 800));
      console.error('DEBUG P4 打字后立刻读:', JSON.stringify(await page.evaluate(() => {
        const row = document.querySelector('[data-role="price-rows"] .price-row');
        const input = row ? row.querySelector('[data-price-field="input"]') : null;
        return {
          domValue: input ? input.value : null,
          rowCount: document.querySelectorAll('[data-role="price-rows"] .price-row').length,
          status: (document.querySelector('[data-role="price-status"]') || {}).textContent || null,
          saving: Boolean(document.querySelector('[data-action="price-save"][disabled]')),
        };
      })));
    }
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致|已回读确认'),
      'P4 第一次保存回读确认（拿真实快照核验，不是只看 200）');
    if (process.env.DEBUG_PRICE) {
      console.error('DEBUG P4 确认后读:', JSON.stringify(await page.evaluate(() => {
        const row = document.querySelector('[data-role="price-rows"] .price-row');
        const input = row ? row.querySelector('[data-price-field="input"]') : null;
        return { domValue: input ? input.value : null };
      })));
    }
    check((await latestPayload())[0].input_per_million === '31',
      `P4 库里落的是我提交的 31（实际 ${JSON.stringify((await latestPayload())[0].input_per_million)}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '1',
      'P4 确认之后我改回的 1 还在表单里（未保存）');
    const ackStatus = await safeText(page, '[data-role="price-status"]');
    check(ackStatus.includes('还没有保存'),
      `P4 界面明说新编辑还没有保存（实际 ${JSON.stringify(ackStatus)}）`);
    const external = await hostPutPrices({
      expected_version: 2,
      entries: [{
        ...(await latestPayload())[0],
        input_per_million: '55',
        output_per_million: '44',
      }],
    });
    check(external.status === 200, `P4 外部会话写 55/44（HTTP ${external.status}）`);
    await clearSuccessToast(page);
    await setPriceField(page, 'gpt-4o-mini', 'output', '66');
    const putsBefore = pricePuts(calls);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P4 出现冲突面板（我手上是被确认过的 revision=2）');
    check(pricePuts(calls) === putsBefore + 1,
      `P4 409 之后没有自动重试（实际 +${pricePuts(calls) - putsBefore}）`);
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '1',
      `P4 重基保留了我"改回 1"的编辑（实际 ${JSON.stringify(await priceFieldValue(page, 'gpt-4o-mini', 'input'))}）`);
    check(await priceFieldValue(page, 'gpt-4o-mini', 'output') === '66',
      'P4 我改过的 output 66 也在');
    const putsAfterRebase = pricePuts(calls);
    await settle();
    check(pricePuts(calls) === putsAfterRebase, 'P4 重基没有自动 PUT');
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P4 显式保存后回读确认');
    const final = (await latestPayload())[0];
    check(final.input_per_million === '1' && final.output_per_million === '66',
      `P4 库里是我最终确认的 1/66（实际 ${JSON.stringify([final.input_per_million, final.output_per_million])}）`);
    check(consoleErrors.length === 0, `P4 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.close();
  }

  await resetHost();
  {
    // P5 对照：ACK 期间改成**另一个**非基线值时同样必须保留 —— 说明驱动确实能观察到
    //    "编辑被保留"，P4 红不是驱动看不见东西。
    const { page, calls, consoleErrors } = await openPriceCard({ holdPutMs: 2500 });
    await setPriceField(page, 'gpt-4o-mini', 'input', '31');
    await clickVisible(page, '[data-action="price-save"]');
    await setPriceField(page, 'gpt-4o-mini', 'input', '32');
    if (process.env.DEBUG_PRICE) {
      await new Promise((r) => setTimeout(r, 800));
      console.error('DEBUG P5 打字后立刻读:', JSON.stringify(await page.evaluate(() => {
        const row = document.querySelector('[data-role="price-rows"] .price-row');
        const input = row ? row.querySelector('[data-price-field="input"]') : null;
        return { domValue: input ? input.value : null, rowCount: document.querySelectorAll('[data-role="price-rows"] .price-row').length };
      })));
    }
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致|已回读确认'), 'P5 第一次保存回读确认');
    const external = await hostPutPrices({
      expected_version: 2,
      entries: [{
        ...(await latestPayload())[0],
        input_per_million: '55',
        output_per_million: '44',
      }],
    });
    check(external.status === 200, `P5 外部会话写 55/44（HTTP ${external.status}）`);
    await clearSuccessToast(page);
    await setPriceField(page, 'gpt-4o-mini', 'output', '66');
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitConflict(page), 'P5 出现冲突面板');
    await clickVisible(page, '[data-action="price-conflict-apply-mine"]');
    check(await priceFieldValue(page, 'gpt-4o-mini', 'input') === '32',
      `P5 重基保留了 ACK 期间的编辑 32（实际 ${JSON.stringify(await priceFieldValue(page, 'gpt-4o-mini', 'input'))}）`);
    await clickVisible(page, '[data-action="price-save"]');
    check(await waitToast(page, '已保存并回读核验|与你提交的内容一致'), 'P5 显式保存后回读确认');
    const final = (await latestPayload())[0];
    check(final.input_per_million === '32' && final.output_per_million === '66',
      `P5 库里是 32/66（实际 ${JSON.stringify([final.input_per_million, final.output_per_million])}）`);
    check(consoleErrors.length === 0, `P5 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
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
  // 断言失败时也要看到**已经通过**的那些项，否则只能看到最后一个超时。
  if (notes.length) console.error(notes.join('\n'));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browserRef) await browserRef.close().catch(() => {});
});
