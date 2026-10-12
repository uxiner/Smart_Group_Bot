// B13b / C4 第二子批：真实发布页 + 合成运营后端的浏览器验收。
//
// 只连回环 harness（BASE 必须是 127.0.0.1），所有"发送/积分/模型/宿主 cron"都在
// harness 里合成；这里验证的是**前端真实请求与真实 DOM 行为**：
//   * 懒加载：不点按钮一个请求都不发；
//   * 预览只读：确认之前不写库、不建 run；
//   * 两次确认：来源切换 / 周期计划 / 六类任务都必须预览→确认→持久读回；
//   * 失败面：503 不可用、409 冲突、503 确认结果未知（不许当成功）、未知结果禁重试；
//   * 取消的复合效果：周报"积分已入账但消息未发送"不许说成本项未执行；
//   * 角色被撤后敏感状态清空；320/390/1280 布局、触控 44px、正文与操作分行。
import {createRequire} from 'node:module';
import fs from 'node:fs';
import {createHmac} from 'node:crypto';
const require=createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`),puppeteer=require('puppeteer-core');
const BASE=process.env.HARNESS,out=process.env.B13B_ARTIFACTS||process.env.B11_ARTIFACTS;
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE||'')||!out)throw Error('loopback/artifact environment required');
const browser=await puppeteer.launch({executablePath:process.env.OPERATIONS_BROWSER,headless:true,userDataDir:`${out}/profile`,args:['--no-sandbox','--disable-dev-shm-usage','--disable-crash-reporter','--disable-background-networking']});
const stub=await(await fetch(BASE+'/harness/telegram-stub.js')).text();
function signedInitData(userId){
 const pairs={auth_date:String(Math.floor(Date.now()/1000)),query_id:'AAF-b13b',user:JSON.stringify({id:userId,first_name:'Synthetic',language_code:'zh'})};
 const secret=createHmac('sha256','WebAppData').update('42:TEST_TOKEN').digest();
 pairs.hash=createHmac('sha256',secret).update(Object.keys(pairs).sort().map(k=>`${k}=${pairs[k]}`).join('\n')).digest('hex');
 return new URLSearchParams(pairs).toString();
}
async function sdk(page,userId=42){
 await page.evaluateOnNewDocument(stub);
 await page.evaluateOnNewDocument((initData,id)=>{window.Telegram.WebApp.initData=initData;window.Telegram.WebApp.initDataUnsafe.user.id=id;},signedInitData(userId),userId);
}
const checks=[],errors=[],A='-1001234567890';let page;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const check=(ok,label)=>{checks.push({ok,label});if(!ok)throw Error(label);};
const P=g=>`[data-operations-policy-panel][data-operations-group-id="${g}"]`;
const W=g=>`[data-operations-work-panel][data-operations-group-id="${g}"]`;
const S=g=>`[data-group-settings-section][data-group-id="${g}"][data-group-section="operations-status"]`;
const RUN=g=>`${W(g)} [data-operations-run]`;
async function click(sel,wait=120){const el=await page.waitForSelector(sel,{timeout:10000});await el.evaluate(x=>x.scrollIntoView({block:'center'}));await pause(50);await el.click();await pause(wait);}
async function text(sel){return page.$eval(sel,e=>e.innerText);}
async function card(g){const sel=`[data-action="toggle-group-card"][data-group-id="${g}"]`;if(await page.$eval(sel,e=>e.getAttribute('aria-expanded')!=='true'))await click(sel);}
async function section(g){await card(g);const sel=S(g);if(await page.$eval(sel,e=>!e.open))await click(sel+' > summary');}
async function openPolicy(g){
 await section(g);
 if(await page.$eval(P(g),e=>!e.querySelector('[data-operations-policy-task]')))await click(`${P(g)} [data-action="operations-load-policy"]`);
 await page.waitForFunction(sel=>!!document.querySelector(sel)?.querySelector('[data-operations-policy-task]'),{timeout:10000},P(g));
 await pause(120);
}
async function reqs(){return (await(await fetch(BASE+'/harness/requests')).json()).requests;}
async function countReq(prefix){return (await reqs()).filter(r=>r.startsWith(prefix)).length;}
async function fixture(body){await fetch(BASE+'/harness/operations-fixture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function selectTask(g,id){await click(`${W(g)} [data-action="operations-select-task"][data-task-id="${id}"]`);}
async function setParam(g,name,value){const sel=`${W(g)} [data-operations-param="${name}"]`;await page.$eval(sel,(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));},value);await pause(60);}
async function previewRun(g){await click(`${W(g)} [data-action="operations-preview-run"]`);await page.waitForFunction(sel=>!!document.querySelector(sel),{timeout:10000},`${W(g)} [data-operations-run-preview]`);}
async function confirmRun(g){await click(`${W(g)} [data-action="operations-confirm-run"]`);await page.waitForFunction(sel=>!!document.querySelector(sel),{timeout:10000},RUN(g));}
async function waitFinished(g){await page.waitForFunction(sel=>{const r=document.querySelector(sel);return !!r&&!r.querySelector('[data-action="operations-cancel-run"]')&&!!r.querySelector('[data-operations-item]');},{timeout:20000},RUN(g));await pause(120);}
async function policyTaskText(g,id){return text(`${P(g)} [data-operations-policy-task="${id}"]`);}
async function panelBox(sel){return page.$eval(sel,e=>{const r=e.getBoundingClientRect();return {w:r.width,h:r.height,top:r.top,bottom:r.bottom,left:r.left,right:r.right};});}

try{
 for(const width of [320,390,1280]){
  await fetch(BASE+'/harness/reset',{method:'POST'});
  await fixture({});
  if(page)await page.close();
  page=await browser.newPage();
  await sdk(page);
  await page.setViewport({width,height:900});
  page.on('pageerror',e=>errors.push(`${width} pageerror ${e.message}`));
  await page.setRequestInterception(true);
  const normal=req=>req.url().startsWith(BASE)||req.url().startsWith('data:')?req.continue():req.abort();
  page.on('request',normal);
  await page.goto(BASE+'/settings',{waitUntil:'networkidle0'});
  await page.waitForSelector('#app[aria-busy="false"]');
  if(width<=820)await click('#sidebar-toggle');
  await click('#desktop-nav [data-nav="groups"]');
  await page.waitForSelector('[data-action="toggle-group-card"]',{timeout:10000});

  // --- 1. 懒加载：不点按钮一个请求都不发 -------------------------------------
  await section(A);
  check(await countReq(`GET /api/v1/groups/${A}/operations/policy`)===0,`${width} 运营分节默认不读来源与计划（0 请求）`);
  const readonlyText=await text(S(A));
  check(/外部宿主 cron 的登记情况未知|外部 cron 的登记情况未知/.test(readonlyText),`${width} 只读状态如实说明宿主 cron 登记未知`);
  check(/执行来源与周期计划需要两次确认/.test(await text(P(A))),`${width} 来源与计划面板说明两次确认`);
  await openPolicy(A);
  const policyText=await text(P(A));
  check((await page.$$(`${P(A)} [data-operations-policy-task]`)).length===6,`${width} 六类任务都列出来`);
  check(/外部入口（宿主 cron）/.test(await policyTaskText(A,'checkin_reminder')),`${width} 签到默认外部入口`);
  check(/进程内执行/.test(await policyTaskText(A,'shop_expire')),`${width} 商店默认进程内执行`);
  check(/只有一种执行来源/.test(await policyTaskText(A,'group_summary')),`${width} 群摘要只有一种来源`);
  check(/周期计划：关闭|计划关闭/.test(await policyTaskText(A,'weekly_report')),`${width} 周报周期计划默认关闭`);
  check(!/已开启/.test(policyText),`${width} 没有任何任务被默认开启自动计划`);

  // --- 2. 来源切换：预览只读 → 确认 → 持久读回 -------------------------------
  const beforeSwitch=await countReq(`POST /api/v1/groups/${A}/operations/policy/confirm`);
  await click(`${P(A)} [data-operations-policy-task="checkin_reminder"] [data-action="operations-preview-source"][data-source="internal"]`);
  await page.waitForSelector(`${P(A)} [data-operations-policy-preview]`,{timeout:10000});
  check(await countReq(`POST /api/v1/groups/${A}/operations/policy/confirm`)===beforeSwitch,`${width} 来源预览不写库（确认前 0 次确认请求）`);
  await openPolicy(A);
  check(/外部入口（宿主 cron）/.test(await policyTaskText(A,'checkin_reminder')),`${width} 预览不改变持久来源`);
  await click(`${P(A)} [data-operations-policy-task="checkin_reminder"] [data-action="operations-preview-source"][data-source="internal"]`);
  await page.waitForSelector(`${P(A)} [data-operations-policy-preview]`,{timeout:10000});
  await click(`${P(A)} [data-action="operations-confirm-policy"]`);
  await pause(400);
  await openPolicy(A);
  check(/进程内执行/.test(await policyTaskText(A,'checkin_reminder')),`${width} 切换来源确认后持久读回`);
  await click(`${P(A)} [data-action="operations-load-audit"]`);
  await pause(300);
  const auditText=await page.$eval(`${P(A)} .operations-audit-list`,e=>e.innerText).catch(()=>'');
  check(/签到提醒/.test(auditText),`${width} 来源切换留下本群审计`);

  // --- 3. 周期计划：未选择拒绝、开启只锚定未来 -------------------------------
  const minuteSel=`${P(A)} [data-operations-policy-task="weekly_report"] [data-operations-plan-minute]`;
  await page.$eval(minuteSel,e=>{e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));});
  const previewsBefore=await countReq(`POST /api/v1/groups/${A}/operations/policy/preview`);
  await click(`${P(A)} [data-operations-policy-task="weekly_report"] [data-action="operations-preview-plan"][data-enabled="true"]`);
  await pause(300);
  check(await countReq(`POST /api/v1/groups/${A}/operations/policy/preview`)===previewsBefore,`${width} 未选择时刻时拒绝预览（不发请求）`);
  check(/必须明确选择周内日与时刻/.test(await text('body')),`${width} 未选择时刻给出明确提示`);
  await page.$eval(`${P(A)} [data-operations-policy-task="weekly_report"] [data-operations-plan-weekday]`,e=>{e.value='5';e.dispatchEvent(new Event('change',{bubbles:true}));});
  await page.$eval(minuteSel,e=>{e.value='10:00';e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));});
  await click(`${P(A)} [data-operations-policy-task="weekly_report"] [data-action="operations-preview-plan"][data-enabled="true"]`);
  await page.waitForSelector(`${P(A)} [data-operations-policy-preview]`,{timeout:10000});
  const planPreview=await text(`${P(A)} [data-operations-policy-preview]`);
  check(/2026-10-10 10:00/.test(planPreview),`${width} 计划预览给出绝对时刻（下一次）`);
  check(/Asia\/Shanghai/.test(planPreview),`${width} 计划预览标注 Asia/Shanghai 时区`);
  check(/只影响未来|不会立刻补发上一周/.test(planPreview),`${width} 计划预览说明只影响未来不补历史`);
  await openPolicy(A);
  check(/周期计划：关闭|计划关闭/.test(await policyTaskText(A,'weekly_report')),`${width} 计划确认前仍是关闭`);
  await click(`${P(A)} [data-operations-policy-task="weekly_report"] [data-action="operations-preview-plan"][data-enabled="true"]`);
  await page.waitForSelector(`${P(A)} [data-operations-policy-preview]`,{timeout:10000});
  await click(`${P(A)} [data-action="operations-confirm-policy"]`);
  await pause(400);
  await openPolicy(A);
  const weeklyText=await policyTaskText(A,'weekly_report');
  check(/2026-10-10 10:00/.test(weeklyText),`${width} 计划确认后持久读回下一次计划`);
  check(!/2026-10-0[5678]|2026-10-05T/.test(weeklyText),`${width} 计划不会落在过去（不补上一周）`);

  // --- 4. 六类任务：预览只读 → 确认 → 轮询 → 按项结果 -------------------------
  const taskIds=width===1280?['scheduled_messages','group_summary','checkin_reminder','activity_award','weekly_report','shop_expire']:['checkin_reminder','weekly_report'];
  for(const taskId of taskIds){
   await selectTask(A,taskId);
   const runsBefore=await countReq(`POST /api/v1/groups/${A}/operations/runs`);
   if(taskId==='scheduled_messages'){await setParam(A,'entry_ids','1,2');}
   if(taskId==='checkin_reminder'){await page.$eval(`${W(A)} [data-operations-param="slot"]`,e=>{e.value='12';e.dispatchEvent(new Event('change',{bubbles:true}));});}
   await previewRun(A);
   const previewText=await text(`${W(A)} [data-operations-run-preview]`);
   check(/将处理|本次预览没有选中任何目标/.test(previewText),`${width} ${taskId} 预览展示目标`);
   check(/成本/.test(previewText),`${width} ${taskId} 预览披露成本`);
   check(await countReq(`POST /api/v1/groups/${A}/operations/runs`)===runsBefore,`${width} ${taskId} 预览不建执行记录`);
   await confirmRun(A);
   await waitFinished(A);
   const runText=await text(RUN(A));
   check(/已按确认的目标执行完成|已发送|已完成/.test(runText),`${width} ${taskId} 确认后真实轮询到终态`);
   const items=(await page.$$(`${RUN(A)} [data-operations-item]`)).length;
   check(items===(taskId==='scheduled_messages'?2:1),`${width} ${taskId} 按项结果逐条显示（${items}）`);
   check(/成功/.test(runText),`${width} ${taskId} 结果里有成功项`);
  }

  // --- 5. 失败面（只在 1280 全跑；320/390 跑关键的未知结果与 503） -------------
  const failModes=width===1280?['unknown','failed_retryable','cancelled','weekly_cancel','unavailable','unconfirmed','conflict']:['unknown','unconfirmed'];
  if(failModes.includes('unknown')){
   await fixture({run:'unknown'});
   await selectTask(A,'checkin_reminder');
   await previewRun(A);
   await confirmRun(A);
   await waitFinished(A);
   const unknownText=await text(RUN(A));
   check(/未知/.test(unknownText),`${width} 未知结果在界面上明确标出`);
   check((await page.$(`${W(A)} [data-action="operations-retry-run"]`))===null,`${width} 未知结果不提供重试按钮`);
   check(/不提供重试/.test(unknownText),`${width} 未知结果明确说明不提供重试`);
   await click(`${W(A)} [data-action="operations-reconcile-run"]`);
   await pause(400);
   const reconciled=await text(RUN(A));
   check(/对账完成/.test(reconciled),`${width} 对账后状态明确更新`);
   check(!/结果未知|不许自动重发/.test(reconciled),`${width} 对账后不再显示未知（聚合里的 0 未知 是正常计数）`);
  }
  if(failModes.includes('failed_retryable')){
   await fixture({run:'failed_retryable'});
   await selectTask(A,'checkin_reminder');
   await previewRun(A);
   await confirmRun(A);
   await waitFinished(A);
   check(await page.$(`${W(A)} [data-action="operations-retry-run"]`)!==null,`${width} 可安全重试的失败提供重试按钮`);
   await click(`${W(A)} [data-action="operations-retry-run"]`);
   await page.waitForFunction(sel=>/仅安全重试完成/.test(document.querySelector(sel)?.innerText||''),{timeout:10000},RUN(A));
   check(true,`${width} 重试是人确认后的一次新执行`);
  }
  if(failModes.includes('cancelled')){
   await fixture({run:'cancelled',polls_until_finished:99});
   await selectTask(A,'scheduled_messages');
   await setParam(A,'entry_ids','1,2');
   await previewRun(A);
   await confirmRun(A);
   check(await page.$(`${RUN(A)} [data-action="operations-cancel-run"]`)!==null,`${width} 执行中提供请求取消`);
   await click(`${RUN(A)} [data-action="operations-cancel-run"]`);
   await pause(400);
   const cancelled=await text(RUN(A));
   check(/未执行/.test(cancelled),`${width} 取消后说明本项未执行`);
   check(await page.$(`${RUN(A)} [data-action="operations-cancel-run"]`)===null,`${width} 取消后不再显示取消按钮`);
  }
  if(failModes.includes('weekly_cancel')){
   await fixture({run:'cancelled',polls_until_finished:99});
   await selectTask(A,'weekly_report');
   await previewRun(A);
   await confirmRun(A);
   await click(`${RUN(A)} [data-action="operations-cancel-run"]`);
   await pause(400);
   const weeklyCancelled=await text(RUN(A));
   check(/已入账 25 分/.test(weeklyCancelled),`${width} 周报取消保留"积分已入账"的真实复合效果`);
   check(/不会重复发放/.test(weeklyCancelled),`${width} 周报取消说明已入账积分不会重复发放`);
  }
  if(failModes.includes('unavailable')){
   await fixture({preview:'unavailable'});
   await selectTask(A,'group_summary');
   await click(`${W(A)} [data-action="operations-preview-run"]`);
   await pause(400);
   check(/不可用/.test(await text(W(A))),`${width} 预览 503 明确报错`);
   await fixture({});
   await previewRun(A);
   check(/将处理|没有选中/.test(await text(`${W(A)} [data-operations-run-preview]`)),`${width} 预览失败后可以恢复`);
  }
  if(failModes.includes('unconfirmed')){
   await fixture({confirm:'unconfirmed',hold_confirm_ms:900});
   await selectTask(A,'scheduled_messages');
   await setParam(A,'entry_ids','3');
   await previewRun(A);
   await click(`${W(A)} [data-action="operations-confirm-run"]`);
   await pause(150);
   await setParam(A,'entry_ids','3,4');
   await page.waitForFunction(sel=>/无法确认|可能已经受理/.test(document.querySelector(sel)?.innerText||''),{timeout:10000},`${W(A)} [data-action="operations-confirm-run"]`).catch(()=>{});
   await pause(900);
   const ackText=await text(W(A));
   check(/可能已经受理|无法确认/.test(ackText),`${width} 确认结果未知时明确说结果无法确认`);
   check(!/仅安全重试完成/.test(ackText),`${width} 确认结果未知不当成功`);
   check(await page.$eval(`${W(A)} [data-operations-param="entry_ids"]`,e=>e.value)==='3,4',`${width} ACK 等待期间继续编辑的草稿保留`);
   await fixture({});
  }
  if(failModes.includes('conflict')){
   await fixture({policy:'conflict'});
   await openPolicy(A);
   await click(`${P(A)} [data-operations-policy-task="shop_expire"] [data-action="operations-preview-source"][data-source="external"]`);
   await page.waitForSelector(`${P(A)} [data-operations-policy-preview]`,{timeout:10000});
   await click(`${P(A)} [data-action="operations-confirm-policy"]`);
   await pause(400);
   const conflictText=await text(P(A));
   check(/失效|冲突|重新预览/.test(conflictText),`${width} 来源确认冲突时要求重新预览`);
   await fixture({});
   await openPolicy(A);
   check(/进程内执行/.test(await policyTaskText(A,'shop_expire')),`${width} 冲突的确认没有落库`);
  }

  // --- 6. 布局、触控与安全区 -------------------------------------------------
  await fixture({});
  // 先造一条真实的终态执行：布局断言必须量真实渲染出来的结果块与操作行。
  await selectTask(A,'checkin_reminder');
  await previewRun(A);
  await confirmRun(A);
  await waitFinished(A);
  const overflow=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,inner:window.innerWidth}));
  check(overflow.scroll<=overflow.inner+1,`${width} 运营面板没有横向溢出（${overflow.scroll}/${overflow.inner}）`);
  const small=await page.$$eval(`${P(A)} button, ${P(A)} select, ${P(A)} input, ${W(A)} button, ${W(A)} select, ${W(A)} input`,els=>els.filter(e=>e.offsetParent!==null).map(e=>({tag:e.tagName,h:e.getBoundingClientRect().height})));
  check(small.length>0,`${width} 运营控件可交互`);
  check(small.every(item=>item.h>=44),`${width} 运营控件触控高度 ≥44px（最小 ${Math.min(...small.map(i=>i.h)).toFixed(1)}）`);
  await page.evaluate(()=>window.Telegram.WebApp.__setInsets({top:0,bottom:34,left:0,right:0},{top:0,bottom:0,left:0,right:0}));
  await pause(150);
  const inset=await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--safe-bottom').trim());
  check(/34px/.test(inset),`${width} 安全区底部真实生效（${inset}）`);
  await page.evaluate(()=>window.Telegram.WebApp.__setInsets({top:0,bottom:0,left:0,right:0},{top:0,bottom:0,left:0,right:0}));
  await pause(150);
  const reset=await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--safe-bottom').trim());
  check(!/34px/.test(reset),`${width} 安全区归零后不残留（${reset}）`);
  const itemBoxes=await page.$$eval(`${RUN(A)} [data-operations-item]`,els=>els.map(e=>e.getBoundingClientRect().bottom));
  const toolbarTop=await page.$eval(`${RUN(A)} .resource-panel-toolbar`,e=>e.getBoundingClientRect().top);
  check(itemBoxes.length===0||toolbarTop>=Math.max(...itemBoxes)-1,`${width} 结果正文与操作按钮各自独立整行`);

  // --- 7. 角色被撤：敏感状态清空 ---------------------------------------------
  await fixture({revoke:true});
  await click(`${P(A)} [data-action="operations-load-policy"]`).catch(()=>{});
  await pause(300);
  check(await page.$$(`${P(A)} [data-operations-policy-task]`).then(rows=>rows.length===0),`${width} 角色被撤后来源与计划不再显示`);
  check(/尚未读取执行来源|没有权限|失败/.test(await text(P(A))),`${width} 角色被撤后回到未读取状态`);
  check(await page.$$(`${W(A)} [data-operations-run]`).then(rows=>rows.length===0),`${width} 角色被撤后执行记录清空`);
  await fixture({});
 }
}catch(error){errors.push(String(error&&error.stack||error));}
finally{
 try{await page?.close();}catch{}
 await browser.close();
 const result={checks,failed:checks.filter(c=>!c.ok).length,errors};
 fs.writeFileSync(`${out}/browser-result.json`,JSON.stringify(result,null,2));
 if(errors.length)process.exitCode=1;
 console.log(JSON.stringify({checks:checks.length,failed:result.failed,errors:errors.length}));
}
