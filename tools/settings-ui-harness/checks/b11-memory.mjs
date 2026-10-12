import {createRequire} from 'node:module';
import fs from 'node:fs';
import {createHmac} from 'node:crypto';
const require=createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`),puppeteer=require('puppeteer-core');
const BASE=process.env.HARNESS,out=process.env.B11_ARTIFACTS;
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE)||!out)throw Error('loopback/artifact environment required');
const browser=await puppeteer.launch({executablePath:process.env.OPERATIONS_BROWSER,headless:true,userDataDir:`${out}/profile`,args:['--no-sandbox','--disable-dev-shm-usage','--disable-crash-reporter','--disable-background-networking']});
const stub = await (await fetch(BASE+'/harness/telegram-stub.js')).text();
function signedInitData(userId) {
 const pairs={auth_date:String(Math.floor(Date.now()/1000)),query_id:'AAF-b11',user:JSON.stringify({id:userId,first_name:'Synthetic',language_code:'zh'})};
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
// C3 同步：等待条件必须绑定真实读取生命周期（loading/role=status 消失且 refresh 解禁），
// 不能等「下一页」按钮之类读取后可能消失的元素，也不靠猜的 sleep。
async function settled(sel){await page.waitForFunction(sel=>{const s=document.querySelector(sel)?.closest('[data-memory-read-section]');return !!s && !s.querySelector('[role="status"]') && !s.querySelector('[data-action="memory-read-refresh"]')?.disabled;},{timeout:10000},sel);}
async function click(sel,wait=true){const e=await page.$(sel);if(!e)throw Error('missing '+sel);await e.evaluate(x=>x.scrollIntoView({block:'center'}));await pause(60);await e.click();await pause(120);if(wait && sel.startsWith('[data-memory-read-section'))await settled(sel.match(/^\[data-memory-read-section="[^"]+"\]\[data-group-id="[^"]+"\]/)[0]);}
async function card(g){const sel=`[data-action="toggle-group-card"][data-group-id="${g}"]`;if(await page.$eval(sel,e=>e.getAttribute('aria-expanded')!=='true'))await click(sel);}
async function panel(g){await card(g);const sel=`[data-group-settings-section][data-group-id="${g}"][data-group-section="memory-transparency"]`;if(await page.$eval(sel,e=>!e.open))await click(sel+' > summary');}
async function open(g,k){await panel(g);const sel=section(g,k);if(await page.$eval(sel,e=>!e.open))await click(sel+' > summary');await page.waitForFunction(sel=>!!document.querySelector(sel)?.querySelector('article, [role="alert"]')||/摘要/.test(document.querySelector(sel)?.innerText||''),{},sel);await pause(150);}
async function readCount(g,k){const x=await(await fetch(BASE+'/harness/requests')).json();return x.requests.filter(r=>r.startsWith(`GET /api/v1/groups/${g}/memory-transparency/${k}?`)).length;}
async function fixture(body){await fetch(BASE+'/harness/memory-fixture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function refresh(g,k){await click(section(g,k)+' [data-action="memory-read-refresh"]');await pause(160);}
try{
 for(const width of [320,390,1280]){
  await fetch(BASE+'/harness/reset',{method:'POST'});if(page)await page.close();page=await browser.newPage();await sdk(page);await page.setViewport({width,height:900});page.on('pageerror',e=>errors.push(String(e)));
  await page.setRequestInterception(true);const normal=r=>r.url().startsWith(BASE)||r.url().startsWith('data:')?r.continue():r.abort();page.on('request',normal);
  await page.goto(BASE+'/settings',{waitUntil:'networkidle0'});await page.waitForSelector('#app[aria-busy="false"]');if(width<=820)await click('#sidebar-toggle');await click('#desktop-nav [data-nav="groups"]');await card(A);
  const outer=`[data-group-settings-section][data-group-id="${A}"][data-group-section="memory-transparency"]`;
  check(!await page.$eval(outer,e=>e.open),`${width} default collapsed`);check(await readCount(A,'facts')===0,`${width} collapsed has no read`);
  await open(A,'facts');check(await readCount(A,'facts')===1,`${width} first open one read`);await pause(300);check(await readCount(A,'facts')===1,`${width} no render request loop`);
  check(await page.$$eval(section(A,'facts')+' article',e=>e.length)===20,`${width} first page twenty`);
  check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('<script>literal</script>')&&!e.querySelector('script')),`${width} facts are literal text`);
  // 未提交的筛选草稿绝不能搭上旧 query 的 cursor：兄弟分节的读取/失败/恢复都会触发全页 render。
  const draftInput=section(A,'facts')+' [name="subject"]';
  const categoryInput=section(A,'facts')+' [name="category"]';
  const kindInput=section(A,'searches')+' [name="kind"]';
  const readsFor=async(k)=>(await(await fetch(BASE+'/harness/requests')).json()).requests.filter(r=>r.startsWith(`GET /api/v1/groups/${A}/memory-transparency/${k}?`));
  const clearDraft=()=>page.$eval(draftInput,e=>{e.value='';});
  const resetFacts=async()=>{await clearDraft();await page.select(categoryInput,'');await click(section(A,'facts')+' [type="submit"]');await pause(180);};
  const resetSearches=async()=>{await page.select(kindInput,'');await click(section(A,'searches')+' [type="submit"]');await pause(180);};
  // 检索留档卡片几何：与事实卡片同一成因（复用了全站 .notice 的横向 flex），
  // 只量真实渲染出来的框：查询/摘要/元数据/来源各占一整行、不交叠、不内部溢出，
  // 来源链接真实可点（命中测试），伪 HTML 只能保持字面文字。
  const searchCardSel=g=>`${section(g,'searches')} article`;
  async function searchLayout(g){
    return page.evaluate(async sel=>{
      const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,right:r.right,bottom:r.bottom};};
      const area=(a,b)=>a&&b?Math.max(0,Math.min(a.right,b.right)-Math.max(a.x,b.x))*Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)):0;
      const overflowOf=e=>e?e.scrollWidth-e.clientWidth:0;
      let cards=0,missing=0,narrow=0,unreadable=0,unordered=0,inner=0,overlap=0,pseudo=0,links=0,linksBad=0,literalBad=0;
      const sample=[];
      for(const a of document.querySelectorAll(sel)){
        cards+=1;
        const self=rect(a),texts=[...a.querySelectorAll(':scope > p.memory-read-text')];
        // 来源行优先按专用类取；旧版（bd5f9a8）没有这个类，就退回卡片里最后一个直接 <p>，
        // 这样基线的红是真实几何（被 flex 挤成窄列）而不是“类名不存在”，不能只测新类。
        const sourcesRow=a.querySelector(':scope > p.memory-read-sources')||[...a.querySelectorAll(':scope > p')].pop()||null;
        const rows=[texts[0]||null,texts[1]||null,a.querySelector(':scope > small'),sourcesRow];
        const boxes=rows.map(e=>e?rect(e):null);
        if(rows.some(e=>!e)||texts.length<2)missing+=1;
        const prev=[];
        for(let i=0;i<boxes.length;i+=1){
          const b=boxes[i];if(!b)continue;
          if(b.w<self.w-28)narrow+=1;
          if(!(b.w>=44&&b.w>=b.h))unreadable+=1;
          if(prev.length&&b.y<prev[prev.length-1].bottom-1)unordered+=1;
          if(overflowOf(rows[i])>1)inner+=1;
          for(const p of prev)if(area(b,p)>1)overlap+=1;
          prev.push(b);
        }
        pseudo+=a.querySelectorAll('img, b, script').length;
        if(!a.innerText.includes('<img src=x onerror=alert(1)>纯文字'))literalBad+=1;
        if(sample.length<1)sample.push({self,rows:boxes,display:getComputedStyle(a).display});
      }
      // 行框量完之后再做真实的点击命中测试：elementFromPoint 用视口坐标，
      // 折叠下方的卡片必须先滚进视口并等滚动稳定，否则只会测出 null（假阳性）。
      const linkDetail=[];
      for(const a of document.querySelectorAll(sel)){
        const row=a.querySelector(':scope > p.memory-read-sources')||[...a.querySelectorAll(':scope > p')].pop()||null,link=row?row.querySelector('a[href]'):null;
        if(!link)continue;
        links+=1;
        link.scrollIntoView({block:'center'});
        const until=Date.now()+900;
        while(Date.now()<until){const rr=rect(link);if(rr.y>=0&&rr.bottom<=window.innerHeight)break;await new Promise(r=>setTimeout(r,40));}
        const card=rect(a),r=rect(link);
        const hit=document.elementFromPoint(r.x+r.w/2,r.y+r.h/2);
        const usable=r.w>=16&&r.h>=10&&r.x>=card.x-1&&r.right<=card.right+1&&r.y>=0&&r.bottom<=window.innerHeight&&(hit===link||link.contains(hit));
        if(!usable){linksBad+=1;if(linkDetail.length<3)linkDetail.push({r,cardY:card.y,hit:hit?hit.tagName+(hit.className?'.'+String(hit.className).split(' ')[0]:''):null,vh:window.innerHeight});}
      }
      return {scrollWidth:document.documentElement.scrollWidth,innerWidth:window.innerWidth,cards,missing,narrow,unreadable,unordered,inner,overlap,pseudo,literalBad,links,linksBad,linkDetail,sample};
    },searchCardSel(g));
  }
  async function checkSearchLayout(width,stage,g=A){
    const r=await searchLayout(g);
    check(r.scrollWidth<=r.innerWidth+1,`${width} ${stage}: no horizontal page overflow`);
    check(r.cards>0&&r.missing===0,`${width} ${stage}: every search card keeps query/digest/meta/sources rows`);
    check(r.narrow===0,`${width} ${stage}: search rows use the full card width`);
    check(r.unreadable===0,`${width} ${stage}: search rows are readable and not vertical`);
    check(r.unordered===0,`${width} ${stage}: search rows stack in DOM order`);
    check(r.inner===0,`${width} ${stage}: no internal overflow in search rows`);
    check(r.overlap===0,`${width} ${stage}: no overlap between search rows`);
    check(r.pseudo===0&&r.literalBad===0,`${width} ${stage}: pseudo HTML stays literal text`);
    check(r.links>0&&r.linksBad===0,`${width} ${stage}: source links are clickable inside the card`);
  }
  async function draftNextPage(label,field,trigger){
    if(field.select)await page.select(field.sel,field.value);else{await click(field.sel,false);await page.keyboard.type(field.value);}
    await trigger();
    await click(section(A,field.section)+' [data-action="memory-read-more"]');
    const call=(await readsFor(field.section)).slice(-1)[0];
    check(!call.includes(field.param),`${width} ${label} draft never rides previous cursor`);
    check(call.includes('cursor=')&&call.includes('limit=20'),`${width} ${label} next page uses applied cursor`);
    check(await page.$eval(field.sel,e=>e.value)===field.value,`${width} ${label} draft preserved`);
    check(await page.$eval(section(A,field.section)+' [data-memory-draft-hint]',e=>!e.hidden),`${width} ${label} pending hint shown`);
  }
  await draftNextPage('sibling read:',{section:'facts',sel:draftInput,value:'0',param:'subject='},async()=>{await open(A,'summary');});
  await resetFacts();
  await draftNextPage('fact category draft:',{section:'facts',sel:categoryInput,value:'identity',param:'category=',select:true},async()=>{await refresh(A,'facts');});
  await resetFacts();
  check(await page.$$eval(section(A,'facts')+' article',e=>e.length)===20,`${width} facts back to first page`);
  await click(section(A,'facts')+' [data-action="memory-read-more"]');
  check(await page.$$eval(section(A,'facts')+' article',e=>e.length)===5,`${width} actual cursor next page`);
  const subject=section(A,'facts')+' [name="subject"]';await click(subject);await page.keyboard.type('0');await click(section(A,'facts')+' [type="submit"]');check(await page.$eval(section(A,'facts'),e=>!e.innerText.includes('对象 1')),`${width} subject zero filter`);
  await page.select(section(A,'facts')+' [name="category"]','skill');await click(section(A,'facts')+' [type="submit"]');check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('暂无可展示')),`${width} filtered empty explicit`);
  await page.select(section(A,'facts')+' [name="category"]','');await click(section(A,'facts')+' [type="submit"]');const old=await page.$eval(section(A,'facts'),e=>e.querySelector('article').innerText);
  await fixture({mode:'failure',section:'facts'});await refresh(A,'facts');check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('已陈旧')),`${width} read failure marked stale`);check(await page.$eval(section(A,'facts'),e=>e.querySelector('article').innerText)===old,`${width} failed read keeps old`);check(await page.$eval(subject,e=>e.value)==='0',`${width} failed read keeps filter input`);await refresh(A,'facts');check(!await page.$(section(A,'facts')+' [role="alert"]'),`${width} explicit read recovery`);
  await open(A,'searches');check(await readCount(A,'searches')===1,`${width} independent lazy search read`);check(await page.$eval(section(A,'searches'),e=>e.innerText.includes('陈旧')&&e.innerText.includes('<img src=x')),`${width} stale search and literal HTML`);
  check(await page.$$eval(section(A,'searches')+' a',es=>es.every(e=>/^https?:/.test(e.href)&&e.rel.includes('noopener'))),`${width} only safe noopener links`);
  await checkSearchLayout(width,'list');
  await click(section(A,'searches')+' [data-action="memory-read-more"]');check(await page.$$eval(section(A,'searches')+' article',e=>e.length)===5,`${width} search real cursor page`);
  await fixture({mode:'failure',section:'searches'});await refresh(A,'searches');check(await page.$eval(section(A,'searches'),e=>e.innerText.includes('已陈旧')&&!!e.querySelector('article')),`${width} search failure keeps old marked stale`);await checkSearchLayout(width,'stale');await refresh(A,'searches');check(!await page.$(section(A,'searches')+' [role="alert"]'),`${width} search recovery`);
  await resetFacts();
  await refresh(A,'searches');
  await draftNextPage('search kind draft:',{section:'searches',sel:kindInput,value:'news',param:'kind=',select:true},async()=>{await refresh(A,'facts');});
  await resetSearches();
  await fixture({mode:'failure',section:'searches'});
  await draftNextPage('sibling failure:',{section:'facts',sel:draftInput,value:'0',param:'subject='},async()=>{await refresh(A,'searches');check(await page.$eval(section(A,'searches'),e=>e.innerText.includes('已陈旧')),`${width} sibling failure rerender`);});
  await refresh(A,'searches');
  await resetFacts();
  await draftNextPage('sibling recovery:',{section:'facts',sel:draftInput,value:'0',param:'subject='},async()=>{await refresh(A,'searches');});
  await resetFacts();
  // 持有中的请求完成后必须按“提交时”的已应用筛选补发一次，且不得复用旧 cursor。
  let holdEnter,holdRelease,heldOnce=false;const holdSeen=new Promise(r=>holdEnter=r);
  page.removeAllListeners('request');
  page.on('request',async r=>{if(!heldOnce&&r.url().includes(`/groups/${A}/memory-transparency/facts?`)){heldOnce=true;const body=await(await fetch(r.url())).text();holdEnter();await new Promise(res=>holdRelease=res);await r.respond({status:200,contentType:'application/json',body});}else normal(r);});
  await click(section(A,'facts')+' [data-action="memory-read-refresh"]',false);await holdSeen;
  await page.$eval(draftInput,e=>{e.value='0';});
  // 故意 hold 响应：提交 queued 筛选后必须先 release 再等真实读取生命周期，避免死锁。
  await click(section(A,'facts')+' [type="submit"]',false);holdRelease();await settled(section(A,'facts'));
  const queuedCall=(await readsFor('facts')).slice(-1)[0];
  check(queuedCall.includes('subject=0')&&!queuedCall.includes('cursor='),`${width} submit during held read uses applied filters`);
  check(await page.$eval(section(A,'facts'),e=>!e.innerText.includes('对象 1')),`${width} queued submit renders filtered set`);
  page.removeAllListeners('request');page.on('request',normal);await resetFacts();
  await page.select(section(A,'searches')+' [name="kind"]','unknown');await click(section(A,'searches')+' [type="submit"]');check(await page.$eval(section(A,'searches'),e=>e.innerText.includes('暂无可展示')),`${width} search kind filter empty`);
  await open(A,'summary');check(await page.$eval(section(A,'summary'),e=>e.innerText.includes('已失效')&&!e.innerText.includes('合成有效摘要')),`${width} invalid summary no body`);
  await fixture({summary:'valid'});await refresh(A,'summary');check(await page.$eval(section(A,'summary'),e=>e.innerText.includes('合成有效摘要')&&e.innerText.includes('覆盖行')),`${width} valid summary actual metadata`);
  await fixture({mode:'failure',section:'summary'});await refresh(A,'summary');check(await page.$eval(section(A,'summary'),e=>e.innerText.includes('已陈旧')&&e.innerText.includes('合成有效摘要')),`${width} summary failure preserves stale body`);await fixture({summary:'empty'});await refresh(A,'summary');check(await page.$eval(section(A,'summary'),e=>e.innerText.includes('暂无摘要')&&!e.innerText.includes('合成有效摘要')),`${width} summary empty distinct`);
  await fixture({summary:'disabled'});await refresh(A,'summary');check(await page.$eval(section(A,'summary'),e=>e.innerText.includes('已禁用')),`${width} disabled distinct`);
  // Hold a real read response, navigate to B before releasing it.
  let enteredResolve,release;const entered=new Promise(r=>enteredResolve=r);page.removeAllListeners('request');page.on('request',async r=>{if(r.url().includes(`/groups/${A}/memory-transparency/facts?`)){const response=await fetch(r.url()),body=await response.text();enteredResolve();await new Promise(res=>release=res);await r.respond({status:200,contentType:'application/json',body});}else normal(r);});
  await click(section(A,'facts')+' [data-action="memory-read-refresh"]',false);await entered;check(await page.$eval(section(A,'facts'),e=>e.innerText.includes('正在读取')&&e.querySelector('[data-action="memory-read-refresh"]').disabled),`${width} held read explicit loading state`);await panel(B);await open(B,'facts');release();await pause(250);check(await page.$eval(section(B,'facts'),e=>e.innerText.includes('本群B')&&!e.innerText.includes('本群A')),`${width} late A response cannot pollute B`);page.removeAllListeners('request');page.on('request',normal);
  await fixture({mode:'permission',section:'facts'});await refresh(A,'facts');check(await page.$eval(section(A,'searches'),e=>!e.querySelector('article')),`${width} permission clears sibling sensitive data`);check(await page.$eval(section(A,'summary'),e=>!e.innerText.includes('合成有效摘要')),`${width} permission clears summary`);
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width} no horizontal overflow`);
  check(await page.$eval(section(B,'facts')+' > summary',e=>e.getBoundingClientRect().height>=44),`${width} touch target`);
  await click(section(B,'facts')+' [name="subject"]');check(await page.$eval(section(B,'facts')+' [name="subject"]',e=>e===document.activeElement),`${width} keyboard focus usable`);
  const x=await(await fetch(BASE+'/harness/requests')).json();check(!x.requests.some(r=>/^(POST|PATCH|DELETE|PUT) \/api\/v1/.test(r)),`${width} UI zero API writes`);
 }
 // The unmodified shipped page must never expose group management to a member.
 for (const width of [320,390,1280]) {
  await page.close();page=await browser.newPage();await sdk(page,777);await page.setViewport({width,height:900});page.on('pageerror',e=>errors.push(String(e)));
  const calls=[];await page.setRequestInterception(true);
  page.on('request',r=>{
   const url=r.url();if(!url.startsWith(BASE)&&!url.startsWith('data:'))return r.abort();
   if(url.includes('/api/v1/'))calls.push(url);
   const respond=(status,body)=>r.respond({status,contentType:'application/json',body:JSON.stringify(body)});
   if(url===BASE+'/api/v1/session')return respond(403,{ok:false,code:'admin_required',message:'没有群管理权限'});
   if(url===BASE+'/api/v1/onboarding/session')return respond(200,{ok:true,session:{user_id:777,role:'applicant',can_manage_global:false,group_ids:[],can_apply:true}});
   if(url.startsWith(BASE+'/api/v1/onboarding/requests?'))return respond(200,{ok:true,requests:[]});
   return r.continue();
  });
  await page.goto(BASE+'/settings',{waitUntil:'networkidle0'});await page.waitForSelector('#app[aria-busy="false"]');
  check(!await page.$('[data-memory-read-section]'),`${width} member has no memory section`);
  check(!await page.$('[data-group-card]'),`${width} member has no group management entry`);
  check(!calls.some(url=>url.includes('/memory-transparency/')||url===BASE+'/api/v1/groups'),`${width} member emits no group reads`);
 }
 check(errors.length===0,'zero page errors');
}catch(e){if(page)console.error((await page.$eval('body',e=>e.innerText)).slice(-2500));throw e;}finally{await browser.close();fs.writeFileSync(out+'/browser-result.json',JSON.stringify({checks,errors},null,2));}
console.log(JSON.stringify({checks:checks.length,failed:checks.filter(c=>!c.ok).length,errors:errors.length}));
