import {createRequire} from 'node:module';
import fs from 'node:fs';
import {createHmac} from 'node:crypto';
const require=createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`),puppeteer=require('puppeteer-core');
const BASE=process.env.HARNESS,out=process.env.B12_ARTIFACTS;
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE)||!out)throw Error('loopback/artifact environment required');
const browser=await puppeteer.launch({executablePath:process.env.OPERATIONS_BROWSER,headless:true,userDataDir:`${out}/profile`,args:['--no-sandbox','--disable-dev-shm-usage','--disable-crash-reporter','--disable-background-networking']});
const stub = await (await fetch(BASE+'/harness/telegram-stub.js')).text();
function signedInitData(userId) {
 const pairs={auth_date:String(Math.floor(Date.now()/1000)),query_id:'AAF-b12',user:JSON.stringify({id:userId,first_name:'Synthetic',language_code:'zh'})};
 const secret=createHmac('sha256','WebAppData').update('42:TEST_TOKEN').digest();
 pairs.hash=createHmac('sha256',secret).update(Object.keys(pairs).sort().map(k=>`${k}=${pairs[k]}`).join('\n')).digest('hex');
 return new URLSearchParams(pairs).toString();
}
async function sdk(page,userId=42) {
 await page.evaluateOnNewDocument(stub);
 await page.evaluateOnNewDocument((initData,id)=>{window.Telegram.WebApp.initData=initData;window.Telegram.WebApp.initDataUnsafe.user.id=id;},signedInitData(userId),userId);
}
const checks=[],errors=[],A='-1001234567890',B='-1009876543210';let page;
const pause=ms=>new Promise(r=>setTimeout(r,ms)),check=(ok,label)=>{checks.push({ok,label});if(!ok)throw Error(label);};
const section=(g,k)=>`[data-memory-read-section="${k}"][data-group-id="${g}"]`;
async function click(sel){const e=await page.$(sel);if(!e)throw Error('missing '+sel);await e.evaluate(x=>x.scrollIntoView({block:'center'}));await pause(60);await e.click();await pause(120);}
async function card(g){const sel=`[data-action="toggle-group-card"][data-group-id="${g}"]`;if(await page.$eval(sel,e=>e.getAttribute('aria-expanded')!=='true'))await click(sel);}
async function panel(g){await card(g);const sel=`[data-group-settings-section][data-group-id="${g}"][data-group-section="memory-transparency"]`;if(await page.$eval(sel,e=>!e.open))await click(sel+' > summary');}
async function open(g,k){await panel(g);const sel=section(g,k);if(await page.$eval(sel,e=>!e.open))await click(sel+' > summary');await page.waitForFunction(sel=>!!document.querySelector(sel)?.querySelector('article, [role="alert"]')||/摘要/.test(document.querySelector(sel)?.innerText||''),{},sel);await pause(150);}
async function fixture(body){await fetch(BASE+'/harness/memory-fixture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function refresh(g,k){await click(section(g,k)+' [data-action="memory-read-refresh"]');await pause(160);}
const calls=async()=>(await(await fetch(BASE+'/harness/requests')).json()).requests;
const countOf=async(prefix)=>(await calls()).filter(r=>r.startsWith(prefix)).length;
const factButtons=(sel)=>({edit:sel+' [data-action="memory-fact-edit"]',source:sel+' [data-action="memory-fact-source"]'});
const editSel=id=>`form[data-memory-edit="${id}"]`;
const sourceSel=id=>`[data-memory-fact-source="${id}"]`;
// F-mobile：几何测量。只量真实渲染出来的框，不看源码文本；每个事实卡片都必须是
// 「正文 / 元数据 / 操作 / 编辑器 / 原文」逐行堆叠，控件 >=44x44 且标签不竖排。
const factArticleSel=g=>`${section(g,'facts')} article`;
async function factLayout(g){
 return page.evaluate(sel=>{
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,right:r.right,bottom:r.bottom};};
  const box=e=>e?rect(e):null;
  const area=(a,b)=>a&&b?Math.max(0,Math.min(a.right,b.right)-Math.max(a.x,b.x))*Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)):0;
  const overflowOf=e=>e?e.scrollWidth-e.clientWidth:0;
  return {scrollWidth:document.documentElement.scrollWidth,innerWidth:window.innerWidth,articles:[...document.querySelectorAll(sel)].map(a=>{
   const body=a.querySelector(':scope > p.memory-read-text'),meta=a.querySelector(':scope > small');
   const actions=a.querySelector(':scope > p.memory-read-actions')||[...a.querySelectorAll(':scope > p')].find(p=>p.querySelector('button'))||null;
   const form=a.querySelector(':scope > form[data-memory-edit]'),source=a.querySelector(':scope > .memory-read-source'),textarea=a.querySelector(':scope > form[data-memory-edit] textarea');
   return {self:rect(a),display:getComputedStyle(a).display,body:box(body),meta:box(meta),actions:box(actions),form:box(form),source:box(source),
    textarea:textarea?{...rect(textarea),overflow:overflowOf(textarea)}:null,
    bodyOverflow:overflowOf(body),actionsOverflow:overflowOf(actions),sourceOverflow:overflowOf(source),
    buttons:[...a.querySelectorAll('button')].map(b=>({label:b.innerText.trim().slice(0,8),...rect(b),overflow:overflowOf(b)})),
    pairs:[['body','meta',area(box(body),box(meta))],['body','actions',area(box(body),box(actions))],['meta','actions',area(box(meta),box(actions))],['actions','form',area(box(actions),box(form))],['actions','source',area(box(actions),box(source))],['form','source',area(box(form),box(source))]]};
  })};
 },factArticleSel(g));
}
async function checkFactLayout(width,stage,g=A){
 const r=await factLayout(g);
 check(r.scrollWidth<=r.innerWidth+1,`${width} ${stage}: no horizontal page overflow`);
 check(r.articles.length>0,`${width} ${stage}: fact cards are rendered for measurement`);
 let rows=0,wide=0,ordered=0,small=0,vertical=0,inner=0,overlap=0;
 for(const a of r.articles){
  if(!a.body||!a.meta||!a.actions)continue;
  rows++;
  const boxes=[a.body,a.meta,a.actions,a.form,a.source].filter(Boolean);
  if(boxes.every(b=>b.w>=a.self.w-28))wide++;
  if(boxes.every((b,i)=>i===0||b.y>=boxes[i-1].bottom-1))ordered++;
  if([a.bodyOverflow,a.actionsOverflow,a.sourceOverflow].some(v=>v>1))inner++;
  for(const b of a.buttons){if(b.w<44||b.h<44)small++;if(b.h>64)vertical++;if(b.overflow>1)inner++;}
  if(a.textarea&&(a.textarea.w<44||a.textarea.h<44||a.textarea.overflow>1))small++;
  for(const [, ,ar] of a.pairs)if(ar>1)overlap++;
 }
 check(small===0,`${width} ${stage}: every fact control is at least 44x44`);
 check(vertical===0,`${width} ${stage}: no fact button squeezes its label into a vertical stack`);
 check(inner===0,`${width} ${stage}: card content and controls wrap inside their own box`);
 check(overlap===0,`${width} ${stage}: no geometry overlap between card rows`);
 check(rows>0&&wide===rows,`${width} ${stage}: body, metadata, actions, editor and source each take the full content width`);
 check(rows>0&&ordered===rows,`${width} ${stage}: card rows stack vertically instead of becoming flex columns`);
}
try{
 for(const width of [320,390,1280]){
  await fetch(BASE+'/harness/reset',{method:'POST'});if(page)await page.close();page=await browser.newPage();await sdk(page);await page.setViewport({width,height:900});page.on('pageerror',e=>errors.push(String(e)));
  await page.setRequestInterception(true);const normal=r=>r.url().startsWith(BASE)||r.url().startsWith('data:')?r.continue():r.abort();page.on('request',normal);
  await page.goto(BASE+'/settings',{waitUntil:'networkidle0'});await page.waitForSelector('#app[aria-busy="false"]');if(width<=820)await click('#sidebar-toggle');await click('#desktop-nav [data-nav="groups"]');
  await open(A,'facts');
  const first=section(A,'facts')+' article';
  check(await page.$eval(first,e=>e.innerText.includes('修订 1')&&!!e.querySelector('[data-action="memory-fact-edit"]')),`${width} fact shows revision and edit entry`);
  const firstId=await page.$eval(first,e=>e.querySelector('[data-action="memory-fact-edit"]').dataset.factId);
  await checkFactLayout(width,'list');
  // 编辑 → 显式确认 → 真实新持久行（harness 真的替换存储里那一行）
  await click(`${first} [data-action="memory-fact-edit"]`);
  check(!!await page.$(`form[data-memory-edit="${firstId}"] [name="text"]`),`${width} edit form opens with current text`);
  check(await page.$eval(editSel(firstId)+' textarea',e=>e.value.includes('本群A事实')),`${width} editor prefilled from the row`);
  await checkFactLayout(width,'editor');
  await page.$eval(editSel(firstId)+' textarea',e=>{e.value='人工修正后的正文';});
  await click(editSel(firstId)+' button[type="submit"]');await pause(400);
  check(await countOf(`POST /api/v1/groups/${A}/memory-transparency/facts/`)===1,`${width} exactly one correction write`);
  check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('人工修正后的正文')&&e.innerText.includes('人工纠错')),`${width} confirmed correction shows the new row marked human correction`);
  check(await page.$eval(first,e=>e.querySelector('[data-action="memory-fact-edit"]').dataset.factId)!==firstId,`${width} old row replaced by a new stable id`);
  check(await page.$eval(first,e=>e.innerText.includes('修订 1')),`${width} new row starts at revision 1`);
  check(!await page.$('form[data-memory-edit]'),`${width} editor closes after confirmation`);
  // 同文提交必须明确 unchanged，且不新增行
  const secondId=await page.$$eval(section(A,'facts')+' [data-action="memory-fact-edit"]',es=>es[1].dataset.factId);
  await click(`${section(A,'facts')} [data-action="memory-fact-edit"][data-fact-id="${secondId}"]`);
  await click(editSel(secondId)+' button[type="submit"]');await pause(300);
  check(await page.$eval(editSel(secondId)+' [role="alert"]',e=>e.innerText.includes('没有变化')),`${width} same text reports unchanged`);
  check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('本群A事实')),`${width} unchanged keeps the old rows`);
  // 冲突：草稿保留、冲突信息可见、绝不自动重试
  await page.$eval(editSel(secondId)+' textarea',e=>{e.value='冲突草稿正文';});
  await open(A,'searches');await pause(150);
  check(await page.$eval(editSel(secondId)+' textarea',e=>e.value==='冲突草稿正文'),`${width} sibling rerender keeps the edit draft`);
  await fixture({correction:'conflict'});
  await click(editSel(secondId)+' button[type="submit"]');await pause(300);
  check(await page.$eval(editSel(secondId)+' [role="alert"]',e=>e.innerText.includes('冲突')&&e.innerText.includes('当前修订')),`${width} conflict keeps the draft visible with revision info`);
  check(await page.$eval(editSel(secondId)+' textarea',e=>e.value==='冲突草稿正文'),`${width} conflict keeps the draft`);
  check(await countOf(`POST /api/v1/groups/${A}/memory-transparency/facts/`)===3,`${width} no automatic retry on conflict`);
  await fixture({});
  await click(`[data-action="memory-fact-edit-cancel"][data-fact-id="${secondId}"]`);
  check(!await page.$(editSel(secondId)),`${width} cancel closes the editor`);
  // 未确认：保留草稿、不承诺没有效果、不自动重发
  await click(`${section(A,'facts')} [data-action="memory-fact-edit"][data-fact-id="${secondId}"]`);
  await page.$eval(editSel(secondId)+' textarea',e=>{e.value='未确认草稿正文';});
  await fixture({correction:'unconfirmed'});
  await click(editSel(secondId)+' button[type="submit"]');await pause(300);
  check(await page.$eval(editSel(secondId)+' [role="alert"]',e=>e.innerText.includes('未能确认')&&!e.innerText.includes('已生效成功')),`${width} unconfirmed never claims success`);
  check(await page.$eval(editSel(secondId)+' textarea',e=>e.value==='未确认草稿正文'),`${width} unconfirmed keeps the draft`);
  check(await countOf(`POST /api/v1/groups/${A}/memory-transparency/facts/`)===4,`${width} unconfirmed is not resent automatically`);
  await fixture({});
  await click(`[data-action="memory-fact-edit-cancel"][data-fact-id="${secondId}"]`);
  // 受控原文展开：只按需一次、纯文字、可收起
  await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await pause(250);
  check(!!await page.$(sourceSel(secondId)),`${width} source panel opens`);
  check(await page.$eval(sourceSel(secondId),e=>e.innerText.includes('合成来源原文')&&e.innerText.includes('<b>纯文字</b>')&&!e.querySelector('b')),`${width} source text is literal plain text`);
  check(await countOf(`GET /api/v1/groups/${A}/memory-transparency/facts/${secondId}/source`)===1,`${width} source read happens once`);
  check(await page.$eval(sourceSel(secondId)+' a',e=>e.rel.includes('noopener')&&/^https?:/.test(e.href)),`${width} source link is safe`);
  await checkFactLayout(width,'source');
  await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await pause(150);
  check(!await page.$(sourceSel(secondId)),`${width} source panel collapses`);
  check(await countOf(`GET /api/v1/groups/${A}/memory-transparency/facts/${secondId}/source`)===1,`${width} collapse issues no extra read`);
  // 已收起后迟到的回包绝不能重现原文
  let holdEnter,holdRelease;
  const held=new Promise(r=>holdEnter=r);
  page.removeAllListeners('request');
  page.on('request',async r=>{if(r.url().includes(`/memory-transparency/facts/${secondId}/source`)){const body=await(await fetch(r.url())).text();holdEnter();await new Promise(res=>holdRelease=res);await r.respond({status:200,contentType:'application/json',body});}else normal(r);});
  await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await held;
  check(await page.$eval(sourceSel(secondId),e=>e.innerText.includes('正在读取')),`${width} held source shows loading`);
  await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);
  holdRelease();await pause(350);
  check(!await page.$(sourceSel(secondId)),`${width} late source response cannot re-open a collapsed panel`);
  check(!await page.$eval(section(A,'facts'),e=>e.innerText.includes('合成来源原文')),`${width} late source text is not rendered`);
  page.removeAllListeners('request');page.on('request',normal);
  // 三态可辨：已删/不可验证/缺归档都不回正文与链接
  for(const [state,text] of [['deleted','已被确认删除'],['unverifiable','无法验证'],['missing','没有可展开的来源归档']]){
   await fixture({source:state});
   await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await pause(250);
   check(await page.$eval(sourceSel(secondId),(e,label)=>e.innerText.includes(label)&&!e.innerText.includes('合成来源原文')&&!e.querySelector('a'),text),`${width} source state ${state} withholds the body`);
   await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await pause(120);
  }
  await fixture({});
  // 跨群：B 群的分节绝不能出现 A 群的原文面板
  await panel(B);await open(B,'facts');
  check(!await page.$(`${section(B,'facts')} ${sourceSel(secondId)}`),`${width} B never shows A source panel`);
  check(!await page.$eval(section(B,'facts'),e=>e.innerText.includes('合成来源原文')),`${width} B never shows A source text`);
  // 刷新后撤权：兄弟分节的原文状态与控制面一起清理
  await click(`${section(A,'facts')} [data-action="memory-fact-source"][data-fact-id="${secondId}"]`);await pause(250);
  check(!!await page.$(sourceSel(secondId)),`${width} source reopens before revocation`);
  await fixture({mode:'permission',section:'facts'});await refresh(A,'facts');
  check(!await page.$(sourceSel(secondId)),`${width} revoked permission clears expanded source`);
  check(await page.$eval(section(A,'facts'),e=>!e.innerText.includes('合成来源原文')),`${width} revoked permission hides the text`);
  await refresh(A,'facts');
  // 零意外的非纠错写请求
  const all=await calls();
  check(all.filter(r=>/^(PATCH|PUT|DELETE) \/api\/v1\//.test(r)).length===0,`${width} no update or delete calls`);
  check(all.filter(r=>/^POST \/api\/v1\//.test(r)).length===4,`${width} only the four intended correction posts`);
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width} no horizontal overflow`);
  check(await page.$eval(`${section(A,'facts')} > summary`,e=>e.getBoundingClientRect().height>=44),`${width} touch target`);
  await click(`${section(A,'facts')} [name="subject"]`);check(await page.$eval(`${section(A,'facts')} [name="subject"]`,e=>e===document.activeElement),`${width} keyboard focus usable`);
 }
 // 成员负控：绝不能拿到管理员编辑/原文入口
 for (const width of [320,390,1280]) {
  await page.close();page=await browser.newPage();await sdk(page,777);await page.setViewport({width,height:900});page.on('pageerror',e=>errors.push(String(e)));
  const seen=[];await page.setRequestInterception(true);
  page.on('request',r=>{
   const url=r.url();if(!url.startsWith(BASE)&&!url.startsWith('data:'))return r.abort();
   if(url.includes('/api/v1/'))seen.push(url);
   const respond=(status,body)=>r.respond({status,contentType:'application/json',body:JSON.stringify(body)});
   if(url===BASE+'/api/v1/session')return respond(403,{ok:false,code:'admin_required',message:'没有群管理权限'});
   if(url===BASE+'/api/v1/onboarding/session')return respond(200,{ok:true,session:{user_id:777,role:'applicant',can_manage_global:false,group_ids:[],can_apply:true}});
   if(url.startsWith(BASE+'/api/v1/onboarding/requests?'))return respond(200,{ok:true,requests:[]});
   return r.continue();
  });
  await page.goto(BASE+'/settings',{waitUntil:'networkidle0'});await page.waitForSelector('#app[aria-busy="false"]');
  check(!await page.$('[data-memory-read-section]'),`${width} member has no memory section`);
  check(!await page.$('[data-action="memory-fact-edit"]'),`${width} member has no fact edit entry`);
  check(!await page.$('[data-action="memory-fact-source"]'),`${width} member has no source entry`);
  check(!seen.some(url=>url.includes('/memory-transparency/')),`${width} member emits no memory reads`);
 }
 check(errors.length===0,'zero page errors');
}catch(e){if(page)console.error((await page.$eval('body',e=>e.innerText)).slice(-2500));throw e;}finally{await browser.close();fs.writeFileSync(out+'/browser-result.json',JSON.stringify({checks,errors},null,2));}
console.log(JSON.stringify({checks:checks.length,failed:checks.filter(c=>!c.ok).length,errors:errors.length}));
