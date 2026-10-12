// 申请人「检查就绪」真实后端浏览器回归。
//
// 跑法（合成宿主由调用方先起，见 tools/settings-ui-harness/real_api_host.py）：
//   node tools/settings-ui-harness/checks/real-api-check.mjs
// 环境：REAL_API（默认 http://127.0.0.1:8821）、SHOTS（默认 /tmp/dsh-onboarding-shots）。
//
// 这一层验的是**页面**：入口真的在、点一下真的出诊断条目、缺权限/身份失败时
// 说的是实话、且只读 —— 宿主侧用 /__synthetic/db/onboarding 对账没有新写入。

import { createRequire } from 'node:module';
import { createHmac } from 'node:crypto';
import fs from 'node:fs';

const require = createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`);
const puppeteer = require('puppeteer-core');

const CHROME =
  process.env.CHROME_PATH ||
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.REAL_API || 'http://127.0.0.1:8821';
const SHOTS = process.env.SHOTS || '/tmp/dsh-onboarding-shots';

const BOT_TOKEN = '42:TEST_TOKEN';
const SUPER_ADMIN_ID = 42;
const GROUP_A = -1001234567890;
const GROUP_B = -1009876543210;
const APPLICANT_A = 9001;
const MEMBER_A = 8;

const notes = [];
const failures = [];
let browserRef = null;

function check(ok, message) {
  (ok ? notes : failures).push(message);
  if (!ok || process.env.VERBOSE) {
    // eslint-disable-next-line no-console
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${message}`);
  }
}

function note(message) {
  notes.push(message);
  if (process.env.VERBOSE) console.log(`note ${message}`);
}

function signedInitData(userId) {
  const pairs = {
    auth_date: String(Math.floor(Date.now() / 1000)),
    query_id: `AAF-real-check-${userId}-${Math.floor(Math.random() * 1e6)}`,
    user: JSON.stringify({ id: userId, first_name: 'Browser', language_code: 'zh' }),
  };
  const checkString = Object.keys(pairs)
    .sort()
    .map((key) => `${key}=${pairs[key]}`)
    .join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  pairs.hash = createHmac('sha256', secret).update(checkString).digest('hex');
  return new URLSearchParams(pairs).toString();
}

function telegramStub(initData, userId) {
  return `
    (() => {
      const listeners = new Map();
      window.Telegram = {
        WebApp: {
          initData: ${JSON.stringify(initData)},
          initDataUnsafe: {
            user: { id: ${userId}, first_name: 'Browser', language_code: 'zh' },
            auth_date: ${Math.floor(Date.now() / 1000)},
          },
          version: '8.0',
          platform: 'browser',
          colorScheme: 'dark',
          themeParams: {},
          ready() {}, expand() {}, close() {}, isExpanded: true,
          requestFullscreen() {}, exitFullscreen() {},
          setHeaderColor() {}, setBackgroundColor() {},
          enableClosingConfirmation() {}, disableClosingConfirmation() {},
          onEvent(name, handler) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(handler);
          },
          offEvent(name, handler) {
            const list = listeners.get(name) || [];
            listeners.set(name, list.filter((item) => item !== handler));
          },
          emit(name, payload) {
            (listeners.get(name) || []).forEach((handler) => handler(payload));
          },
        },
      };
    })();
  `;
}

async function getJson(path) {
  const response = await fetch(`${BASE}${path}`);
  const body = await response.text();
  let parsed = null;
  try {
    parsed = JSON.parse(body);
  } catch {
    parsed = null;
  }
  return { status: response.status, body: parsed, raw: body };
}

async function postJson(path, payload) {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = null;
  }
  return { status: response.status, body: parsed, raw: text };
}

async function resetHost() {
  const response = await fetch(`${BASE}/__synthetic/reset`, { method: 'POST' });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`reset 失败：HTTP ${response.status} ${text.slice(0, 300)}`);
  }
}

async function openPage(browser, { userId = APPLICANT_A, viewport = { width: 390, height: 844 } } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, deviceScaleFactor: 2 });
  const initData = signedInitData(userId);
  const consoleErrors = [];
  const resourceErrors = [];
  const calls = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    // 被我们主动 abort 的 telegram CDN 与 favicon 之类的资源 404 不是 JS 错误；
    // 真正的脚本异常单独记，免得把噪声当成"页面崩了"。
    if (text.includes('Failed to load resource')) {
      resourceErrors.push(text);
      return;
    }
    consoleErrors.push(text);
  });
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const url = request.url();
    if (/^https:\/\/(telegram\.org|t\.me)\//i.test(url)) {
      request.abort().catch(() => {});
      return;
    }
    const parsed = new URL(url);
    if (parsed.pathname.startsWith('/api/v1/')) {
      calls.push(`${request.method()} ${parsed.pathname}${parsed.search}`);
    }
    request.continue().catch(() => {});
  });
  await page.evaluateOnNewDocument(telegramStub(initData, userId));
  await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app[aria-busy="false"]', { timeout: 30000 });
  return { page, consoleErrors, resourceErrors, calls };
}

async function fillGroupId(page, groupId) {
  await page.waitForSelector('#onboarding-group-id', { timeout: 15000 });
  await page.click('#onboarding-group-id');
  await page.evaluate(() => {
    const input = document.querySelector('#onboarding-group-id');
    input.value = '';
  });
  await page.type('#onboarding-group-id', String(groupId), { delay: 12 });
}

async function clickPreset(page, presetId) {
  await page.click(`[data-action="onboarding-preset"][data-preset="${presetId}"]`);
}

async function clickCheck(page) {
  const responsePromise = page
    .waitForResponse(
      (response) => response.url().includes('/api/v1/onboarding/check'),
      { timeout: 20000 }
    )
    .catch(() => null);
  await page.click('[data-action="onboarding-check"]');
  return await responsePromise;
}

async function readinessText(page) {
  return await page.evaluate(() => {
    const box = document.querySelector('[data-role="onboarding-check"]');
    if (!box) return null;
    return {
      hidden: box.hasAttribute('hidden'),
      text: (box.textContent || '').replace(/\s+/g, ' ').trim(),
      items: box.querySelectorAll('.readiness-list li').length,
      error: (
        document.querySelector('[data-role="onboarding-error"]')?.textContent || ''
      )
        .replace(/\s+/g, ' ')
        .trim(),
    };
  });
}

// 逐条量诊断条目的几何：**同时**取元素盒（getBoundingClientRect）与文字墨迹盒
// （Range.selectNodeContents）。只看容器矩形抓不到"长 token 画出自己的盒压到
// 下一列"这种叠印 —— 容器的盒根本没变，变的是墨迹。
async function collectReadinessGeometry(page) {
  return await page.evaluate(() => {
    const box = document.querySelector('[data-role="onboarding-check"]');
    if (!box) return { items: [], states: [], overflow: null };
    const items = [...box.querySelectorAll('.readiness-item')].map((node) => {
      const children = [...node.children].map((child) => {
        const rect = child.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(child);
        const ink = range.getBoundingClientRect();
        return {
          className: child.className,
          text: (child.textContent || '').trim(),
          box: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
          ink: { left: ink.left, right: ink.right, top: ink.top, bottom: ink.bottom },
        };
      });
      return {
        state: (node.querySelector('.readiness-state')?.textContent || '').trim(),
        children,
      };
    });
    return {
      items,
      states: items.map((item) => item.state),
      overflow: {
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      },
    };
  });
}

function inkOverlaps(a, b) {
  return a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom;
}

// 返回该宽度下的问题清单（空 = 这一宽度没问题）。
function geometryProblems(geometry) {
  const problems = [];
  for (const [index, item] of geometry.items.entries()) {
    const drawn = item.children.filter(
      (child) => child.ink.right > child.ink.left && child.ink.bottom > child.ink.top
    );
    for (let i = 0; i < drawn.length; i += 1) {
      for (let j = i + 1; j < drawn.length; j += 1) {
        if (inkOverlaps(drawn[i].ink, drawn[j].ink)) {
          problems.push(
            `第 ${index + 1} 条（${item.state}）的「${drawn[i].text}」与「${drawn[j].text}」文字墨迹交叉`
          );
        }
      }
    }
    for (const child of drawn) {
      // 单个长单词必须在自己的盒里换行，不许溢出（也不许靠裁切掩盖）。
      if (child.ink.right > child.box.right + 0.5 || child.ink.left < child.box.left - 0.5) {
        problems.push(
          `「${child.text}」的文字墨迹越出自己的盒（ink ${child.ink.left.toFixed(1)}–${child.ink.right.toFixed(
            1
          )} vs box ${child.box.left.toFixed(1)}–${child.box.right.toFixed(1)}）`
        );
      }
    }
  }
  if (geometry.overflow && geometry.overflow.scrollWidth > geometry.overflow.innerWidth + 1) {
    problems.push(
      `页面横向溢出：scrollWidth ${geometry.overflow.scrollWidth} > innerWidth ${geometry.overflow.innerWidth}`
    );
  }
  return problems;
}

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  const hello = await getJson('/__synthetic/hello');
  check(hello.status === 200 && hello.body?.ok === true, `合成宿主可达（HTTP ${hello.status}）`);
  browserRef = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'shell',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const browser = browserRef;

  // ── 1. 未选预设：默认集合也要有诊断，且不承诺"已应用" ──────────────────
  await resetHost();
  const seeded = await getJson('/__synthetic/db/onboarding');
  const seedRows = JSON.stringify(seeded.body?.rows ?? []);
  {
    const { page, consoleErrors, resourceErrors, calls } = await openPage(browser, {
      userId: APPLICANT_A,
    });
    const applicantView = await page.evaluate(() => ({
      input: Boolean(document.querySelector('#onboarding-group-id')),
      presets: document.querySelectorAll('[data-action="onboarding-preset"]').length,
      queuePanel: Boolean(document.querySelector('[data-role="onboarding-queue-panel"]')),
      onboardingNav: Boolean(document.querySelector('[data-nav="onboarding"]')),
    }));
    check(applicantView.input, '非超管进入页面即有申请人视图的群 ID 输入框');
    check(applicantView.presets >= 3, `预设卡片渲染了 ${applicantView.presets} 个`);
    check(
      !applicantView.onboardingNav,
      '非超管看不到超管专属「新群接入」导航入口'
    );

    await fillGroupId(page, GROUP_A);
    const response = await clickCheck(page);
    check(response !== null && response.status() === 200, `检查请求返回 200（${response?.status()}）`);
    let view = await readinessText(page);
    check(view !== null && !view.hidden, '就绪面板真的显示出来');
    check(view !== null && view.items > 0, `未选预设也有 ${view?.items} 条诊断条目`);
    check(
      (view?.text || '').includes('本次检查的功能'),
      '面板说明本次检查用的是哪个功能集合'
    );
    check(
      (view?.text || '').includes('不会应用预设，也不会授权本群'),
      '面板明确"只是检查，不应用预设、不授权"'
    );
    check(!(view?.text || '').includes('已应用'), '面板没有声称预设已经应用');
    check((view?.text || '').includes('moderation'), '默认集合确实按 moderation 诊断');
    // 只读对账：宿主里没有新申请行。
    const after = await getJson('/__synthetic/db/onboarding');
    check(
      JSON.stringify(after.body?.rows ?? []) === seedRows,
      '检查就绪没有向 group_onboarding_requests 写入任何东西'
    );
    check(
      calls.filter((call) => call.includes('/onboarding/check')).length === 1,
      `点一次只发一次检查请求（${calls.join(' | ')}）`
    );
    check(consoleErrors.length === 0, `页面无 JS 异常（${consoleErrors.slice(0, 2).join(' / ')}）`);
    note(`忽略 ${resourceErrors.length} 条被拦截/缺失的静态资源加载噪声`);
    await page.screenshot({ path: `${SHOTS}/check-applicant-390.png` });
    await page.close();
  }

  // ── 2. 选中 companion 预设：诊断按该预设真实功能 ─────────────────────
  {
    const { page, calls } = await openPage(browser, { userId: APPLICANT_A });
    await fillGroupId(page, GROUP_A);
    await clickPreset(page, 'companion');
    const selected = await page.evaluate(
      () =>
        document
          .querySelector('[data-action="onboarding-preset"][data-preset="companion"]')
          ?.getAttribute('aria-checked') || ''
    );
    check(selected === 'true', `companion 预设被选中（aria-checked=${selected}）`);
    const response = await clickCheck(page);
    check(response !== null && response.status() === 200, '选中预设后检查返回 200');
    const view = await readinessText(page);
    check(
      (view?.text || '').includes('casual_chat'),
      'companion 的诊断包含它真实需要的 casual_chat'
    );
    const payload = response ? await response.json() : null;
    check(
      JSON.stringify(payload?.readiness?.selected_features ?? null) ===
        JSON.stringify(['moderation', 'casual_chat']),
      `后端回的就绪集合就是预设真实功能（${JSON.stringify(
        payload?.readiness?.selected_features
      )}）`
    );
    check(
      payload?.readiness?.remote_probe === false,
      '就绪文档明确 remote_probe=false（没做远端连通性测试）'
    );
    check(
      calls.filter((call) => call.includes('/onboarding/check')).length === 1,
      '选中预设后也只发一次检查请求'
    );
    await page.close();
  }

  // ── 3. 机器人缺权：诊断必须说缺什么，而不是"就绪" ────────────────────
  {
    const flipped = await postJson('/__synthetic/telegram-permissions', {
      permissions: { can_delete_messages: false },
    });
    check(flipped.status === 200, `合成宿主接受权限翻转（HTTP ${flipped.status}）`);
    const { page } = await openPage(browser, { userId: APPLICANT_A });
    await fillGroupId(page, GROUP_A);
    await clickPreset(page, 'moderation_only');
    const response = await clickCheck(page);
    const payload = response ? await response.json() : null;
    const view = await readinessText(page);
    check(
      payload?.readiness?.ready === false,
      '缺权限时后端就绪结论是 false，不是"就绪"'
    );
    check(
      (payload?.readiness?.blockers || []).includes('moderation:permission_missing'),
      `阻断项里有 moderation:permission_missing（${JSON.stringify(
        payload?.readiness?.blockers
      )}）`
    );
    check(
      (view?.text || '').includes('缺少'),
      '页面上写明了缺权限（不是空白也不是"就绪"）'
    );
    await page.screenshot({ path: `${SHOTS}/check-permission-390.png` });
    await page.close();
    await resetHost();
  }

  // ── 4. 身份失败：不是本群管理员 → 明确报错，且不渲染诊断 ──────────────
  {
    const { page } = await openPage(browser, { userId: MEMBER_A });
    await fillGroupId(page, GROUP_A);
    const responsePromise = page
      .waitForResponse(
        (response) => response.url().includes('/api/v1/onboarding/check'),
        { timeout: 20000 }
      )
      .catch(() => null);
    await page.click('[data-action="onboarding-check"]');
    const response = await responsePromise;
    check(
      response !== null && response.status() === 403,
      `非本群管理员检查被拒（HTTP ${response?.status()}）`
    );
    const view = await readinessText(page);
    check(
      view === null || view.hidden || view.items === 0,
      '被拒时页面不渲染任何诊断条目'
    );
    check((view?.error || '').length > 0, `页面给出了可理解的错误（${view?.error}）`);
    check(
      !(view?.error || '').includes('api_key') && !(view?.error || '').includes(BOT_TOKEN),
      '错误信息不含凭据'
    );
    await page.close();
  }

  // ── 5. 页面不泄漏全局配置/别的群 ─────────────────────────────────────
  {
    const { page } = await openPage(browser, { userId: APPLICANT_A });
    await fillGroupId(page, GROUP_A);
    await clickCheck(page);
    const html = await page.content();
    for (const forbidden of ['fixture-key', 'api_key', 'gateway.example.invalid', String(GROUP_B)]) {
      check(!html.includes(forbidden), `页面不含 ${forbidden}`);
    }
    await page.close();
  }

  // ── 6. 诊断文字几何：状态码/能力名/说明的墨迹不许交叉（360 → 1280） ────
  //
  // 父代理在 24703d9 上实测：`.readiness-state` 的 min-width 被 390px 媒体查询
  // 归零后，`provider_missing` / `permission_missing` 这类长 token 会画出自己的
  // 盒压到能力名上。源码字符串断言抓不到这个 —— 容器的盒没变、变的是文字墨迹，
  // 所以这里逐条量 Range 墨迹盒。
  {
    const flipped = await postJson('/__synthetic/telegram-permissions', {
      permissions: { can_delete_messages: false },
    });
    check(flipped.status === 200, '几何臂：合成宿主接受权限翻转（一页同时有长状态码）');

    const passes = [
      { role: 'applicant', userId: APPLICANT_A, widths: [360, 390, 414, 768, 1280], wizard: false },
      { role: 'super-admin', userId: SUPER_ADMIN_ID, widths: [360, 390, 1280], wizard: true },
    ];
    const geometries = [];
    for (const pass of passes) {
      for (const width of pass.widths) {
        const { page, consoleErrors } = await openPage(browser, {
          userId: pass.userId,
          viewport: { width, height: 900 },
        });
        if (pass.wizard) {
          // 超管的「新群接入」入口：窄屏时导航可能在抽屉里、元素本身不可见，
          // 这里触发的是**同一份** document 级 `[data-nav]` 处理器，没有绕过它。
          const switched = await page.evaluate(() => {
            const candidates = [...document.querySelectorAll('[data-nav="onboarding"]')];
            const target = candidates.find((node) => node.offsetParent !== null) || candidates[0];
            if (!target) return false;
            target.click();
            return true;
          });
          check(switched, `超管 @ ${width}px：真的能进入「新群接入」向导`);
          await page.waitForSelector('#onboarding-group-id', { timeout: 15000 });
          const queuePanel = await page.evaluate(() =>
            Boolean(document.querySelector('[data-role="onboarding-queue-panel"]'))
          );
          check(
            queuePanel,
            `超管 @ ${width}px：视图带审批队列面板（确认走的是管理员向导，不是申请人页）`
          );
        }
        await fillGroupId(page, GROUP_A);
        await clickCheck(page);
        await page.waitForSelector('.readiness-item', { timeout: 15000 });
        const geometry = await collectReadinessGeometry(page);
        const problems = geometryProblems(geometry);
        await page.screenshot({
          path: `${SHOTS}/readiness-geometry-${pass.role}-${width}.png`,
          fullPage: true,
        });
        geometries.push({
          role: pass.role,
          width,
          items: geometry.items.length,
          states: geometry.states,
          overflow: geometry.overflow,
          problems,
          layout: geometry.items.map((item) => item.children),
        });
        const label = `${pass.role} @ ${width}px`;
        check(geometry.items.length >= 5, `${label}：渲染了 ${geometry.items.length} 条诊断条目`);
        check(
          problems.length === 0,
          `${label}：诊断文字没有交叉、没有越出自己盒、没有横向溢出${
            problems.length ? `（${problems.slice(0, 3).join('；')}）` : ''
          }`
        );
        check(
          geometry.items.every((item) => {
            const state = item.children.find((child) => child.className.includes('readiness-state'));
            const capability = item.children.find((child) =>
              child.className.includes('readiness-capability')
            );
            return (
              item.children.length === 3 &&
              state && state.text.length > 0 &&
              capability && capability.text.length > 0
            );
          }),
          `${label}：每条都有非空的状态码与能力名（没有靠隐藏或裁切掩盖）`
        );
        // 窄屏必须真的分行：状态墨迹的下沿不晚于能力名墨迹的上沿。
        if (width <= 480) {
          const stacked = geometry.items.every((item) => {
            const state = item.children.find((child) => child.className.includes('readiness-state'));
            const capability = item.children.find((child) =>
              child.className.includes('readiness-capability')
            );
            return state && capability && state.ink.bottom <= capability.ink.top + 0.5;
          });
          check(stacked, `${label}：窄屏状态与能力名各占一行`);
        }
        check(consoleErrors.length === 0, `${label}：页面无 JS 异常`);
        await page.close();
      }
    }

    fs.writeFileSync(
      `${SHOTS}/readiness-geometry.json`,
      JSON.stringify(geometries, null, 2)
    );
    const states = new Set(geometries.flatMap((entry) => entry.states));
    check(
      states.has('permission_missing'),
      `几何臂覆盖 permission_missing（实测状态：${[...states].join(', ')}）`
    );
    check(
      states.has('provider_missing'),
      `几何臂覆盖 provider_missing（实测状态：${[...states].join(', ')}）`
    );
    note(`几何证据写入 ${SHOTS}/readiness-geometry.json`);
    await resetHost();
  }

  await browser.close();
  browserRef = null;

  const total = notes.length + failures.length;
  console.log(`${failures.length === 0 ? 'OK' : 'FAILED'} ${notes.length} passed / ${failures.length} failed`);
  for (const failure of failures) console.log(`- ${failure}`);
  if (failures.length > 0) process.exitCode = 1;
  return total;
}

main()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browserRef) {
      try {
        await browserRef.close();
      } catch {
        /* 浏览器已经关了 */
      }
    }
  });
