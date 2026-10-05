// Checks that the settings copy tells the truth about the page it sits on.
// Every claim is verified against the rendered DOM, not against the source:
//   * the Bot page must not promise "everything applies immediately" while it
//     still carries a 需重启 field,
//   * "上方"/"下方"/"同页" references must point at the right element,
//   * "原文" must not be described as never deleted while a retention field exists.
import { chromium } from 'playwright';

const BASE = process.env.HARNESS || 'http://127.0.0.1:8781';
const notes = [];
const failures = [];
const check = (ok, msg) => (ok ? notes.push(`  PASS  ${msg}`) : failures.push(`  FAIL  ${msg}`));

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.goto(`${BASE}/settings`, { waitUntil: 'networkidle' });
await page.waitForSelector('#app[aria-busy="false"]', { timeout: 20000 });
await page.click('#desktop-nav [data-nav="bot"]');
await page.waitForSelector('#field-bot-parse_mode', { timeout: 10000 });
await page.waitForTimeout(400);

const bot = await page.evaluate(() => {
  const text = (selector) => (document.querySelector(selector)?.textContent || '').replace(/\s+/g, ' ').trim();
  const top = (selector) => {
    const el = document.querySelector(selector);
    return el ? Math.round(el.getBoundingClientRect().top) : null;
  };
  const fieldOf = (id) => document.getElementById(id)?.closest('.field') || null;
  const toggleOf = (id) => document.getElementById(id)?.closest('.toggle-field') || null;
  const parseMode = fieldOf('field-bot-parse_mode');
  const richMessages = toggleOf('field-bot-enable_rich_messages');
  const recall = toggleOf('field-bot-memory_recall_enabled');
  const retention = fieldOf('field-bot-memory_retention_days');
  const sectionTitles = [...document.querySelectorAll('.section-heading h3')].map((el) => el.textContent.trim());
  const body = document.getElementById('content').textContent.replace(/\s+/g, ' ');
  return {
    pageHead: text('.page-head p'),
    restartBadges: document.querySelectorAll('.badge.warning').length,
    parseModeLabel: text('#field-bot-parse_mode'),
    parseModeTop: parseMode ? Math.round(parseMode.getBoundingClientRect().top) : null,
    richHint: (() => {
      const toggle = document.getElementById('field-bot-enable_rich_messages')?.closest('.toggle-field');
      return toggle?.querySelector('.toggle-copy small')?.textContent.replace(/\s+/g, ' ').trim() || '';
    })(),
    memorySectionCopy: (() => {
      const heading = [...document.querySelectorAll('.section-heading')]
        .find((el) => el.querySelector('h3')?.textContent.trim() === '长期记忆');
      return heading?.querySelector('p')?.textContent.replace(/\s+/g, ' ').trim() || '';
    })(),
    richTop: richMessages ? Math.round(richMessages.getBoundingClientRect().top) : null,
    recallHelp: recall ? recall.querySelector('.field-help-body')?.textContent.trim() || '' : '',
    recallNeedsExpanding: recall ? !recall.querySelector('.field-help') : null,
    retentionLabel: text('#field-bot-memory_retention_days'),
    sectionTitles,
    memorySectionIndex: sectionTitles.indexOf('长期记忆'),
    contextSectionIndex: sectionTitles.indexOf('上下文与长期记忆'),
    hasRetentionField: Boolean(retention),
    contextSectionCopy: (() => {
      const heading = [...document.querySelectorAll('.section-heading')]
        .find((el) => el.querySelector('h3')?.textContent.trim() === '上下文与长期记忆');
      return heading?.querySelector('p')?.textContent.replace(/\s+/g, ' ').trim() || '';
    })(),
    // Claims that must NOT be on the page.
    absoluteNeverDeleted: /原始(消息|档案|原文)不会(被)?删除/.test(body),
    pageHeadPromisesNoRestart: /保存后立即生效，无需重启/.test(text('.page-head p')),
    pageHeadPromisesEverything: /(全部|所有|整页)[^。]*生效/.test(text('.page-head p')),
    crossPageReference: /下一页的/.test(body),
  };
});

// --- 1. the page must not promise that everything applies immediately -------
check(bot.restartBadges >= 1, `the Bot page still marks restart-required fields (${bot.restartBadges} 需重启 badges)`);
check(
  /多数设置保存后立即生效/.test(bot.pageHead) && /需重启/.test(bot.pageHead),
  `the page head scopes the hot-apply promise and names the exception ("${bot.pageHead}")`,
);
check(!bot.pageHeadPromisesNoRestart, 'the page head no longer promises "保存后立即生效，无需重启" for the whole page');
check(!bot.pageHeadPromisesEverything, 'the page head does not claim that every setting applies immediately');
check(
  /本组开关保存后立即生效/.test(bot.memorySectionCopy),
  `the 长期记忆 section scopes its hot-apply claim to itself, not the page ("${bot.memorySectionCopy}")`,
);

// --- 2. 「上方」 really points above ----------------------------------------
// The parse_mode field carries the 需重启 badge, which is what makes the
// scoped wording necessary in the first place.
check(bot.parseModeTop !== null && bot.richTop !== null, 'both the parse_mode field and the rich-text toggle are rendered');
check(
  bot.parseModeTop < bot.richTop,
  `「消息解析格式」 is actually above the rich-text toggle (${bot.parseModeTop} < ${bot.richTop})`,
);
check(
  bot.richHint && /上方/.test(bot.richHint) && !/下方/.test(bot.richHint),
  `the rich-text hint points at 上方, matching the real order ("${bot.richHint}")`,
);

// --- 3. the memory master switch is on the same page, further down ----------
check(bot.memorySectionIndex > bot.contextSectionIndex, `「长期记忆」 is a later section on the same page (${bot.contextSectionIndex} -> ${bot.memorySectionIndex})`);
check(bot.memorySectionIndex >= 0, '「长期记忆」 is on the Bot page at all (it is not a separate page)');
if (bot.recallNeedsExpanding) {
  await page.locator('[data-advanced-panel], .field-help').first().waitFor({ state: 'attached' });
  const help = page.locator('#field-bot-memory_recall_enabled').locator('xpath=ancestor::label[1]/ancestor::div[1]//details.field-help > summary');
  if (await help.count()) await help.first().click();
  await page.waitForTimeout(200);
}
const recallHelp = await page.evaluate(() => {
  const recall = document.getElementById('field-bot-memory_recall_enabled')?.closest('.toggle-field');
  return recall?.querySelector('.field-help-body')?.textContent.replace(/\s+/g, ' ').trim() || '';
});
check(
  recallHelp.includes('同页下方') && !recallHelp.includes('下一页'),
  `the recall help points at the master switch on the same page below ("${recallHelp.slice(0, 60)}…")`,
);

// --- 4. raw messages are not described as never deleted --------------------
check(!bot.absoluteNeverDeleted, 'the page no longer claims 原始消息/档案/原文 不会被删除');
check(bot.hasRetentionField, `the 原文保留天数 field the copy points at is on the page ("${bot.retentionLabel}")`);
check(
  /原文按/.test(bot.contextSectionCopy) && /保留天数/.test(bot.contextSectionCopy),
  `the section copy states the retention policy instead of promising raw messages are kept ("${bot.contextSectionCopy}")`,
);

await browser.close();
console.log(notes.join('\n'));
if (failures.length) console.log(failures.join('\n'));
console.log(`\n${notes.length} copy-truth checks passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
