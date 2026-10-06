// 运营参数页的真浏览器验收。
//
// 关键原则：**不只检查 DOM 里有输入框**。每一项都读回合成 API 的真实状态
// （存下来的值、PUT 的状态码、请求日志），否则"输入框存在"和"改得动、存得下"
// 会被混为一谈——父代理实跑时正是这样发现的：价格 30→33 保存被"请修正标记的字段"
// 拦下，页面根本没发 PUT。
//
// 跑法（见 tools/settings-ui-harness/README.md）：
//   node $CHECKS/operations.mjs
//   FAIL_HARNESS=http://127.0.0.1:8792 node $CHECKS/operations.mjs      # 503 保留草稿
//   CONFLICT_HARNESS=http://127.0.0.1:8795 node $CHECKS/operations.mjs # 409 冲突
//   GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/operations.mjs    # 群管理员

import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const GROUP_HARNESS = process.env.GROUP_HARNESS || '';
const FAIL_HARNESS = process.env.FAIL_HARNESS || '';
const CONFLICT_HARNESS = process.env.CONFLICT_HARNESS || '';

const notes = [];
const failures = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

// 真实选择器：index.html 里是 #sidebar-toggle（开抽屉）与 #save-button（保存）。
const SIDEBAR_TOGGLE = '#sidebar-toggle';
const SAVE_BUTTON = '#save-button';
const OPERATIONS_NAV = '#desktop-nav [data-nav="operations"]';

// 移动端先把抽屉打开再点导航；**任何一步失败都要报出来**，不能 catch 掉之后
// 继续在别的页面上跑（那会让后面的断言全部失去意义）。
async function gotoOperations(page) {
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
  const narrow = await page.evaluate(() => window.matchMedia('(max-width: 820px)').matches);
  if (narrow) {
    await page.click(SIDEBAR_TOGGLE);
    await page.waitForSelector('.app-shell.sidebar-open', { timeout: 5000 });
  }
  await page.click(OPERATIONS_NAV);
  // 必须真的落在运营参数页
  await page.waitForSelector('#field-economy-tag_price_7d', { timeout: 10000 });
  const title = await page.textContent('#page-title');
  if (title !== '运营参数') {
    throw new Error(`点击「运营参数」后停在「${title}」，没有真正进入目标页面`);
  }
  if (narrow) {
    // 导航后抽屉应自动收起，否则移动端会挡住内容
    await page.waitForFunction(() => !document.querySelector('.app-shell').classList.contains('sidebar-open'), { timeout: 5000 });
  }
  return { narrow };
}

async function readStoredSettings(base) {
  // 直接读合成 API 的落库值——这是"存进去了"的唯一可信证据。
  const response = await fetch(`${base}/api/v1/settings`);
  if (!response.ok) throw new Error(`读取合成 API 失败：HTTP ${response.status}`);
  return response.json();
}

async function readRequestLog(base) {
  const response = await fetch(`${base}/harness/requests`);
  if (!response.ok) throw new Error(`读取请求日志失败：HTTP ${response.status}`);
  return (await response.json()).requests || [];
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });

// ===========================================================================
// 1) 群管理员：不得看到运营页，也不得对全局配置发请求
// ===========================================================================
if (GROUP_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const globalCalls = [];
  page.on('request', (r) => { if (r.url().includes('/api/v1/settings')) globalCalls.push(r.url()); });
  await page.goto(`${GROUP_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });

  const navIds = await page.evaluate(() =>
    [...document.querySelectorAll('#desktop-nav [data-nav]')].map((el) => el.dataset.nav));
  check(!navIds.includes('operations'), '群管理员的导航里没有「运营参数」');
  check(globalCalls.length === 0, '群管理员没有对 /api/v1/settings 发起任何请求');

  // 后端也必须挡住：群管理员直接打 API 应当 403。
  const forbidden = await fetch(`${GROUP_HARNESS}/api/v1/settings`);
  check(forbidden.status === 403, `群管理员读全局配置被后端拒绝（HTTP ${forbidden.status}）`);
  const forbiddenWrite = await fetch(`${GROUP_HARNESS}/api/v1/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: 1, config: {} }),
  });
  check(forbiddenWrite.status === 403, `群管理员写全局配置被后端拒绝（HTTP ${forbiddenWrite.status}）`);

  await page.close();
}

// ===========================================================================
// 2) 最高管理员：改一个真实可编辑字段 → 保存 → 读回 API 落库值
// ===========================================================================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  const { narrow } = await gotoOperations(page);
  check(!narrow, '1280px 下直接经侧栏导航进入运营页');

  const before = (await readStoredSettings(BASE)).config.economy.tag_price_7d;
  const next = before + 3;
  await page.fill('#field-economy-tag_price_7d', String(next));
  await page.click(SAVE_BUTTON);
  await page.waitForFunction(
    () => !document.getElementById('save-button').disabled,
    { timeout: 10000 },
  ).catch(() => {});

  const stored = (await readStoredSettings(BASE)).config.economy.tag_price_7d;
  check(stored === next, `头衔 7 天价 ${before} → ${next} 已落库（读回 ${stored}）`);

  const log = await readRequestLog(BASE);
  check(log.some((entry) => entry === 'PUT /api/v1/settings'), '真的发出了 PUT /api/v1/settings');

  // 页面上的状态行也要跟着走（不是只看输入框的值）
  const saveState = await page.textContent('#save-state');
  check(/已保存/.test(saveState || ''), `保存状态行显示已保存（${(saveState || '').trim()}）`);
  const shown = await page.inputValue('#field-economy-tag_price_7d');
  check(shown === String(next), `输入框回读为新值（${shown}）`);

  await page.close();
}

// ===========================================================================
// 3) 高级面板：奖池结构化编辑器 + 重启字段标记
// ===========================================================================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  await gotoOperations(page);

  const editor = page.locator('[data-reward-editor="economy.lottery_prizes"]');
  check(await editor.count() === 1, '奖池用结构化编辑器');
  check(await editor.locator('textarea').count() === 0, '奖池没有无校验的 free-form JSON 文本框');
  const rowsBefore = await editor.locator('.reward-row').count();
  await editor.locator('[data-reward-add]').click();
  await page.waitForSelector('[data-reward-editor="economy.lottery_prizes"]');
  const rowsAfter = await page.locator('[data-reward-editor="economy.lottery_prizes"] .reward-row').count();
  check(rowsAfter === rowsBefore + 1, `加一档生效（${rowsBefore} → ${rowsAfter}）`);
  const head = await page.textContent('[data-reward-editor="economy.lottery_prizes"] .reward-editor-head');
  check(/总权重/.test(head || ''), '编辑器显示派生总权重');
  check(/期望/.test(head || ''), '编辑器显示期望值');

  // 重启字段必须带单独标记，且**不**被文案谎称成"立即生效"
  await page.evaluate(() => {
    for (const panel of document.querySelectorAll('details.advanced-panel')) panel.open = true;
  });
  await page.waitForTimeout(200);
  const restartBadge = await page.locator('#field-resources-llm_request_capacity .badge.warning').count();
  check(restartBadge > 0, 'LLM 总容量输入框带「需重启」标记');
  const subtitle = await page.textContent('#page-title + .page-description, .page-head p, .page-description');
  const pageCopy = await page.evaluate(() => document.body.innerText);
  check(/需重启/.test(pageCopy), '页面文案提到「需重启」');
  check(!/全部由最高管理员编辑，保存后立即生效。/.test(pageCopy), '不再笼统声称「保存后立即生效」');
  check(/cron/.test(pageCopy), '页面文案提到提醒时段需要同步外部 cron');

  await page.close();
}

// ===========================================================================
// 4) 保存失败（503）必须保留草稿
// ===========================================================================
if (FAIL_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${FAIL_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await gotoOperations(page);

  const before = (await readStoredSettings(FAIL_HARNESS)).config.economy.tag_price_7d;
  const attempted = before + 5;
  await page.fill('#field-economy-tag_price_7d', String(attempted));
  await page.click(SAVE_BUTTON);
  await page.waitForTimeout(900);

  const after = (await readStoredSettings(FAIL_HARNESS)).config.economy.tag_price_7d;
  check(after === before, `503 之后服务端没有被改动（${before} → ${after}）`);
  const kept = await page.inputValue('#field-economy-tag_price_7d');
  check(kept === String(attempted), `503 之后草稿仍在输入框里（${kept}）`);
  const toast = await page.textContent('#toast-region').catch(() => '');
  check(/失败|503|不可用/.test(toast || ''), `503 之后页面给出失败提示（${(toast || '').trim().slice(0, 60)}）`);

  await page.close();
}

// ===========================================================================
// 5) revision 冲突（409）必须可重载，且不能静默覆盖
// ===========================================================================
if (CONFLICT_HARNESS) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${CONFLICT_HARNESS}/settings`, { waitUntil: 'networkidle' });
  await gotoOperations(page);

  const before = (await readStoredSettings(CONFLICT_HARNESS)).config.economy.tag_price_7d;
  await page.fill('#field-economy-tag_price_7d', String(before + 7));
  await page.click(SAVE_BUTTON);
  await page.waitForTimeout(900);

  const afterConflict = (await readStoredSettings(CONFLICT_HARNESS)).config.economy.tag_price_7d;
  check(afterConflict === before, `409 之后服务端没有被改动（${before} → ${afterConflict}）`);
  const kept = await page.inputValue('#field-economy-tag_price_7d');
  check(kept === String(before + 7), `409 之后草稿仍在（${kept}）`);

  // 重载之后能存进去
  await page.reload({ waitUntil: 'networkidle' });
  await gotoOperations(page);
  const reloaded = await page.inputValue('#field-economy-tag_price_7d');
  check(reloaded === String(before), `重载后回到服务端的值（${reloaded}）`);
  await page.fill('#field-economy-tag_price_7d', String(before + 9));
  await page.click(SAVE_BUTTON);
  await page.waitForTimeout(900);
  const finalValue = (await readStoredSettings(CONFLICT_HARNESS)).config.economy.tag_price_7d;
  check(finalValue === before + 9, `重载后再次保存成功（${finalValue}）`);

  await page.close();
}

// ===========================================================================
// 6) 移动端 360 / 390 / 430：真的进到运营页，不横溢，触达 44px，焦点可见
// ===========================================================================
for (const width of [360, 390, 430]) {
  const page = await browser.newPage({ viewport: { width, height: 844 }, deviceScaleFactor: 2 });
  await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
  const { narrow } = await gotoOperations(page);
  check(narrow, `${width}px：走抽屉导航进入运营参数页`);

  const overflow = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    vw: window.innerWidth,
  }));
  check(overflow.sw <= overflow.vw + 1, `${width}px：无横向溢出（${overflow.sw} vs ${overflow.vw}）`);

  const small = await page.evaluate(() => {
    const nodes = [
      ...document.querySelectorAll('.reward-row input'),
      ...document.querySelectorAll('.reward-row .icon-button'),
      ...document.querySelectorAll('.reward-editor-head .secondary-button'),
    ];
    return nodes
      .map((el) => ({
        id: el.id || el.className,
        h: Math.round(el.getBoundingClientRect().height),
        w: Math.round(el.getBoundingClientRect().width),
      }))
      .filter((box) => box.h < 44 || box.w < 44);
  });
  check(small.length === 0, `${width}px：触达目标 ≥44px（${JSON.stringify(small.slice(0, 3))}）`);

  await page.focus('[data-reward-editor="economy.lottery_prizes"] input');
  const outline = await page.evaluate(() => {
    const style = getComputedStyle(document.activeElement);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  check(
    outline.style !== 'none' && parseFloat(outline.width) > 0,
    `${width}px：聚焦输入框有可见焦点环（${JSON.stringify(outline)}）`,
  );

  // 同一次点击里的 blur 不能吞掉保存：填值 → 立刻点保存
  const before = await page.inputValue('#field-economy-tag_price_7d');
  await page.fill('#field-economy-tag_price_7d', String(Number(before) + 2));
  await page.click(SAVE_BUTTON);
  await page.waitForTimeout(900);
  const stored = (await readStoredSettings(BASE)).config.economy.tag_price_7d;
  check(stored === Number(before) + 2, `${width}px：blur 未吞掉保存（${before} → ${stored}）`);

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
