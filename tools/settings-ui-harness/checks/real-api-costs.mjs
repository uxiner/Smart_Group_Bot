// 按群成本面板「发出时冻结金额」的真实后端浏览器验收。
//
// 为什么单开一个脚本：金额这件事的失败模式是**静默改数**（拿 latest 重算历史、
// 把缺价当 0 元、把 USD 与 CNY 加成一个数），这类缺陷在只读 DOM 文本的断言里
// 完全看不出来。所以这里跑产品自己的 `VerifyWebServer` + 真实 `init_db`
// 合成 SQLite（`tools/settings-ui-harness/real_api_host.py`），并且
//   1) 先读 `/__synthetic/db/costs` 的**原始行**（llm_cost_daily +
//      llm_cost_priced_daily + flush 批次），
//   2) 用原始行**算出**期望的界面字符串，
//   3) 再断言浏览器 DOM 里出现的正是这些字符串。
// 数字对不上就是红；界面自己编一个"总额"也同样红。
//
// 跑法：
//   python3 tools/settings-ui-harness/real_api_host.py --port 8821
//   node $CHECKS/real-api-costs.mjs
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
const MEMBER_A = 8;
fs.mkdirSync(SHOTS, { recursive: true });

const notes = [];
const failures = [];
const check = (ok, msg) => {
  const line = ok ? `  PASS  ${msg}` : `  FAIL  ${msg}`;
  if (process.env.VERBOSE) console.error(line);
  return ok ? notes.push(line) : failures.push(line);
};

const note = (msg) => notes.push(`  note  ${msg}`);

// 与产品 `formatMicro` 同一个口径（金额是整数微单位，六位小数）。
const fmt = (micro, currency) => `${(Number(micro) / 1_000_000).toFixed(6)} ${String(currency || '').trim()}`;

function signedInitData(userId) {
  const pairs = {
    auth_date: String(Math.floor(Date.now() / 1000)),
    query_id: `AAF-real-costs-${userId}-${Math.floor(Math.random() * 1e6)}`,
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

const blockedCdn = [];

async function getJson(path) {
  const response = await fetch(`${BASE}${path}`);
  const body = await response.json().catch(() => null);
  return { status: response.status, body };
}

async function resetHost() {
  const response = await fetch(`${BASE}/__synthetic/reset`, { method: 'POST' });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`reset 失败：HTTP ${response.status} ${detail.slice(0, 300)}`);
  }
}

async function openPage(browser, { viewport = { width: 390, height: 844 }, fault = null, userId = SUPER_ADMIN_ID } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
  await page.setRequestInterception(true);
  const apiCalls = [];
  const responses = [];
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
      // 读故障注入：只作用于成本读取、只作用一次，错误信封与产品一致。
      if (fault && fault.costsRead > 0 && request.method() === 'GET'
          && /\/api\/v1\/groups\/[^/]+\/costs$/.test(parsed.pathname)) {
        fault.costsRead -= 1;
        request.respond({
          status: 503,
          contentType: 'application/json',
          headers: { 'cache-control': 'no-store' },
          body: JSON.stringify({ error: { code: 'cost_unavailable', message: '注入的成本读取故障' } }),
        }).catch(() => {});
        return;
      }
    }
    request.continue().catch(() => {});
  });
  page.on('response', (response) => {
    const url = response.url();
    if (url.includes('/api/v1/')) {
      const parsed = new URL(url);
      responses.push({ status: response.status(), path: `${parsed.pathname}${parsed.search}` });
    }
  });
  await page.evaluateOnNewDocument(TELEGRAM_STUB(signedInitData(userId), userId));
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 30000 });
  return { page, consoleErrors, calls: apiCalls, responses };
}

async function gotoTab(page, navId) {
  const narrow = await page.evaluate(() => window.innerWidth < 900);
  if (narrow) {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const open = await page.evaluate(() => Boolean(document.querySelector('.app-shell.sidebar-open')));
      if (open) break;
      await page.click('#sidebar-toggle');
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }
  await page.click(`[data-nav="${navId}"]:not([hidden])`);
  await page.waitForFunction((id) => {
    const node = document.querySelector(`[data-nav="${id}"]`);
    return node && node.getAttribute('aria-current') === 'page';
  }, { timeout: 20000 }, navId);
}

// 打开「群组管理」→ 成本面板 → 点重新读取，等金额区渲染出来。
async function openCostPanel(page) {
  await gotoTab(page, 'groups');
  await page.waitForSelector('.cost-panel', { timeout: 25000 });
  await page.click('.cost-panel [data-action="cost-reload"]');
  await page.waitForSelector('[data-role="cost-amounts"], .cost-panel .field-error', { timeout: 25000 });
  return page.$eval('.cost-panel', (node) => node.textContent.replace(/\s+/g, ' '));
}

// 从原始行算出界面上应当出现的每个金额字符串：**每个价格版本各一条**，
// 跨币种不加总（所以这里刻意不产生任何"总额"字符串）。
function expectedAmountStrings(priced) {
  const pricedRows = priced.filter((row) => Number(row.priced_attempts || 0) > 0
    && Number(row.amount_micro || 0) > 0 && String(row.currency || '').trim());
  return pricedRows.map((row) => fmt(row.amount_micro, row.currency));
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'shell',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  browserRef = browser;

  // ============================ A 合成通道：先钉住"库里的账"长什么样
  await resetHost();
  const hello = await getJson('/__synthetic/hello');
  check(hello.body?.ok === true, `宿主动作正常（kind=${hello.body?.kind}）`);
  const groupId = hello.body.group_id;
  const raw = await getJson(`/__synthetic/db/costs?group_id=${groupId}`);
  const main = raw.body?.main || [];
  const priced = raw.body?.priced || [];
  const batches = raw.body?.batches || [];
  note('库里的账：priced=' + JSON.stringify(priced.map((r) => [
    r.model, 'v' + r.price_version, r.currency, r.attempts, r.priced_attempts, r.unknown_attempts, r.amount_micro,
  ])));

  check(batches.length === 1, `一次重置只留一个冻结批次（实际 ${batches.length}）`);
  check(priced.length === 5, `计价表 5 行（实际 ${priced.length}）`);

  const byKey = (model, version, currency) => priced.find((row) => row.model === model
    && Number(row.price_version) === version && String(row.currency || '') === currency);
  const v1 = byKey('gpt-4o-mini', 1, 'USD');
  const v2 = byKey('gpt-4o-mini', 2, 'USD');
  const v3 = byKey('gpt-4o-mini', 3, 'USD');
  const cny = byKey('cny-model', 3, 'CNY');
  const noprice = byKey('no-price-model', 3, '');
  check(Boolean(v1 && v2 && v3 && cny && noprice), 'v1/v2/v3/CNY/缺价五种行都在');
  check(v1?.amount_micro === 12 && v2?.amount_micro === 1200 && cny?.amount_micro === 70,
    `发送时冻结的金额：v1=12 v2=1200 CNY=70（实际 ${v1?.amount_micro}/${v2?.amount_micro}/${cny?.amount_micro}）`);
  check(v1?.price_snapshot?.input_per_million === '1' && v2?.price_snapshot?.input_per_million === '100'
    && cny?.price_snapshot?.input_per_million === '7',
    '每行都留下了当时的单价快照（1 / 100 / 7），不是靠今天的目录反推');
  check(Number(v3?.unknown_usage_attempts || 0) === 1 && Number(v3?.priced_attempts || 0) === 0,
    '第三次调用缺用量 → 记未知用量（不是按 0 token 算成 0 元）');
  check(Number(noprice?.unknown_price_attempts || 0) === 1 && String(noprice?.currency || '') === ''
    && noprice?.price_snapshot == null,
    '没有价格条目的调用落在空币种桶、快照为空');
  const legacy = main.find((row) => row.model === 'legacy-model');
  check(Number(legacy?.attempts || 0) === 3, `旧历史主表 3 次 attempt 仍在（实际 ${legacy?.attempts}）`);
  check(!priced.some((row) => row.model === 'legacy-model'),
    '旧历史没有价格元数据 → 计价表里一行都没有（拒绝拿今天的价回填）');

  const expected = expectedAmountStrings(priced);
  note(`从原始行算出的期望金额字符串：${JSON.stringify(expected)}`);

  // ====== A2 同一支 API 的授权负控：超管 200 / 普通成员 403 / 无签名 401
  const authedCosts = async (userId) => {
    const response = await fetch(`${BASE}/api/v1/groups/${groupId}/costs?days=7`, {
      headers: userId == null ? {} : { Authorization: `tma ${signedInitData(userId)}` },
    });
    return { status: response.status, body: await response.json().catch(() => null) };
  };
  const asAdmin = await authedCosts(SUPER_ADMIN_ID);
  check(asAdmin.status === 200 && asAdmin.body?.ok === true,
    `超管签名读本群成本是 200（实际 ${asAdmin.status}）`);
  check(Array.isArray(asAdmin.body?.currencies)
    && asAdmin.body.currencies.map((b) => b.currency).sort().join(',') === ',CNY,USD',
    `API 回包按币种分桶：${JSON.stringify(asAdmin.body?.currencies?.map((b) => [b.currency, b.amount_micro]))}`);
  check(asAdmin.body?.amount_complete === false && asAdmin.body?.cross_currency_total === null,
    'API 明说金额不完整，且不提供跨币种合计');
  check(asAdmin.body?.source_table === 'llm_cost_daily'
    && asAdmin.body?.amount_source_table === 'llm_cost_priced_daily'
    && String(asAdmin.body?.note || '').includes('llm_usage_daily'),
    'API 标清用量的表与金额的表是两个维度，且与 /cost 看板口径不可相加');
  const asMember = await authedCosts(MEMBER_A);
  check(asMember.status === 403, `普通成员读同一支 API 是 403（实际 ${asMember.status}）`);
  const memberBody = asMember.body || {};
  check(memberBody.ok === false && memberBody.error?.code === 'group_access_denied',
    `403 回包是干净的拒绝信封：${JSON.stringify(memberBody.error?.code)}`);
  check(!('currencies' in memberBody) && !('rows' in memberBody) && !('totals' in memberBody)
    && JSON.stringify(memberBody).match(/0\.0000/) === null,
    '403 回包不含任何成本内容（没有币种桶、明细行、合计或金额数字）');
  const anonymous = await authedCosts(null);
  check(anonymous.status === 401, `没有签名是 401（实际 ${anonymous.status}）`);

  // ================ B 真实浏览器正控：DOM 里出现的必须是上面那些数
  {
    const { page, consoleErrors, calls } = await openPage(browser, { viewport: { width: 390, height: 844 } });
    const text = await openCostPanel(page);
    const dom = await page.evaluate(() => {
      const node = document.querySelector('.cost-panel [data-role="cost-amounts"]');
      const thumbs = Array.from(document.querySelectorAll('.cost-panel .cost-amount-cell'))
        .map((cell) => cell.textContent.replace(/\s+/g, ' ').trim());
      const currencies = Array.from(document.querySelectorAll('[data-role="cost-amounts"] [data-currency]'))
        .map((item) => item.dataset.currency);
      return {
        present: Boolean(node),
        complete: node ? node.dataset.amountComplete : null,
        currencies,
        thumbs,
        hasAmountColumn: Array.from(document.querySelectorAll('.cost-panel .cost-table th'))
          .some((th) => th.textContent.trim() === '金额'),
        // "没有 0"这条只对**金额区与明细表**成立。预算面板里的"今日已用
        // 0.000000 USD"是明确的已知零（今天真的没花），拿它当红是假红。
        zeros: (
          (document.querySelector('[data-role="cost-amounts"]')?.textContent || '')
          + Array.from(document.querySelectorAll('.cost-panel .cost-table td'))
            .map((cell) => cell.textContent).join(' ')
        ).match(/0\.000000/g)?.length || 0,
      };
    });

    check(dom.present === true, '成本面板里有 data-role="cost-amounts" 金额区');
    check(dom.complete === 'false', `金额区明确标为不完整（data-amount-complete=${dom.complete}）`);
    for (const amount of expected) {
      check(text.includes(amount), `界面上有这个数：${amount}`);
    }
    check(dom.currencies.includes('USD') && dom.currencies.includes('CNY'),
      `USD 与 CNY 各自成桶（实际 ${JSON.stringify(dom.currencies)}）`);
    check(dom.currencies.includes(''),
      '缺价的调用有独立的"币种未知"桶，没有被塞进 USD');
    check(text.includes('不折算成任何货币'),
      '空币种桶明说不折算，而不是猜一个币种');
    check(text.includes('另有 1 次金额未知'),
      'USD 桶里那次缺用量的调用被单列为"金额未知"，没有被 0 元吞掉');
    check(text.includes('早于单价冻结') && text.includes('4 次未计价'),
      '旧历史 3 次 + 缺价 1 次合成 4 次未计价，并说明早于冻结无法回溯');
    check(dom.hasAmountColumn === true, '明细表有"金额"列');
    check(dom.thumbs.some((cell) => cell.includes('单价 v1')) && dom.thumbs.some((cell) => cell.includes('单价 v2')),
      `明细行按价格版本分别标出单价版本（实际 ${JSON.stringify(dom.thumbs.slice(0, 4))}）`);
    check(dom.thumbs.some((cell) => cell.includes('用量缺失或坏值')),
      '缺用量那一行在明细里写明原因，而不是显示 0.000000');
    check(dom.zeros === 0, `金额区与明细表里没有把未知写成 0.000000（实际 ${dom.zeros} 处）`);
    const naiveSum = fmt(12 + 1200 + 70, 'USD');
    check(!text.includes(naiveSum), `没有把 USD 与 CNY 加成一个数（不该出现 ${naiveSum}）`);
    check(text.includes('不是供应商结算账单'), '说明这是估算而不是结算账单');
    check(text.includes('改价不追改历史'), '说明改价不追改历史');

    const costsGets = calls.filter((call) => /^GET \/api\/v1\/groups\/[^/]+\/costs\?days=7$/.test(call));
    check(costsGets.length === 1, `点一次重新读取只拉一次成本（实际 ${costsGets.length}）`);
    check(consoleErrors.length === 0, `无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-costs-390.png`, fullPage: true });
    await page.close();
  }

  // ================ C 1280 布局：金额区不撑破视口
  {
    const { page, consoleErrors } = await openPage(browser, { viewport: { width: 1280, height: 900 } });
    await openCostPanel(page);
    const layout = await page.evaluate(() => {
      const node = document.querySelector('[data-role="cost-amounts"]');
      const rect = node?.getBoundingClientRect();
      return {
        present: Boolean(node),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        right: rect ? Math.round(rect.right) : null,
        viewport: window.innerWidth,
      };
    });
    check(layout.present === true && layout.right !== null && layout.right <= layout.viewport + 1,
      `1280 下金额区在视口内（right=${layout.right} / ${layout.viewport}）`);
    check(layout.overflow <= 1, `1280 下没有横向溢出（${layout.overflow}px）`);
    check(consoleErrors.length === 0, `1280 无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-costs-1280.png`, fullPage: true });
    await page.close();
  }

  // ================ D 负控：读取 503 时显示未知，不显示 0
  await resetHost();
  {
    const fault = { costsRead: 1 };
    const { page, consoleErrors } = await openPage(browser, { viewport: { width: 390, height: 844 }, fault });
    const text = await openCostPanel(page);
    const state = await page.evaluate(() => ({
      amounts: Boolean(document.querySelector('.cost-panel [data-role="cost-amounts"]')),
      // 只看成本摘要那一块：预算面板的"今日已用 0.000000"是已知零，不属于成本读取。
      zeros: ((document.querySelector('[data-role="cost-summary"]')?.textContent || '')
        .match(/0\.000000/g) || []).length,
      error: (document.querySelector('.cost-panel .field-error')?.textContent || '').replace(/\s+/g, ' '),
    }));
    check(fault.costsRead === 0, '503 只注入一次（真实世界里的读故障就是一次性的）');
    check(state.amounts === false, '读失败时**没有**金额区（不拿 0 冒充读取成功）');
    check(state.zeros === 0, `读失败的成本摘要里没有 0.000000（实际 ${state.zeros} 处）`);
    check(/状态未知/.test(state.error) && /不要假定|不要按 0/.test(text),
      `读失败时明说状态未知且禁止按 0 估算：${JSON.stringify(state.error.slice(0, 90))}`);
    check(consoleErrors.length === 0, `503 臂无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-costs-503-390.png`, fullPage: true });
    await page.close();
  }

  // ================ E 负控：非本群权限的成员看不到金额
  await resetHost();
  {
    const { page, consoleErrors, responses } = await openPage(browser, {
      viewport: { width: 390, height: 844 }, userId: MEMBER_A,
    });
    await gotoTab(page, 'groups');
    await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
    await new Promise((resolve) => setTimeout(resolve, 800));
    const state = await page.evaluate(() => ({
      panels: document.querySelectorAll('.cost-panel').length,
      amounts: document.querySelectorAll('[data-role="cost-amounts"]').length,
      zeros: (document.body.textContent.match(/0\.000000/g) || []).length,
      body: document.body.textContent.replace(/\s+/g, ' ').slice(0, 400),
    }));
    const forbidden = responses.filter((item) => item.status === 403);
    note(`普通成员看到的群组页：cost panels=${state.panels}, responses=${JSON.stringify(responses.map((r) => [r.status, r.path]))}`);
    check(state.amounts === 0, '普通成员页面上没有任何金额区');
    check(state.zeros === 0, `普通成员页面上没有 0.000000（实际 ${state.zeros} 处）`);
    check(state.panels > 0 ? forbidden.length > 0 : true,
      state.panels > 0
        ? `本群无权限时成本读取被真实拒绝（403 × ${forbidden.length}）`
        : '普通成员根本看不到成本面板（群未授权给他管理）');
    check(consoleErrors.length === 0, `负控无 JS 异常${consoleErrors.length ? `：${consoleErrors[0]}` : ''}`);
    await page.screenshot({ path: `${SHOTS}/real-api-costs-member-390.png`, fullPage: true });
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
  if (notes.length) console.error(notes.join('\n'));
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browserRef) await browserRef.close().catch(() => {});
});
