// B10 full shipped app, real Chrome pointer clicks; loopback harness only.
import {createRequire} from 'node:module';
import fs from 'node:fs';
const require = createRequire(`${process.env.PUPPETEER_ROOT || '/tmp/cdpprobe'}/`);
const puppeteer = require('puppeteer-core');
const BASE = process.env.HARNESS;
if (!BASE || !/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE)) throw Error('loopback HARNESS required');
const out = process.env.B10_ARTIFACTS;
if (!out) throw Error('B10_ARTIFACTS required');
const browser = await puppeteer.launch({executablePath:process.env.OPERATIONS_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,dumpio:true,userDataDir:`${out}/profile`,args:['--no-sandbox','--disable-dev-shm-usage','--disable-crash-reporter','--disable-background-networking']});
const checks=[],errors=[];
const check=(ok,label)=>{ checks.push({ok,label}); if(!ok) throw Error(label); };
const A='-1001234567890', B='-1009876543210';
const meta={rules:{id:1,field:'pattern',section:'rules-memory',key:'rule'},'keyword-replies':{id:20,field:'reply_text',section:'automation',key:'keyword_reply'},'scheduled-messages':{id:30,field:'text',section:'automation',key:'scheduled_message'}};
let page;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const ctl=(kind)=>kind==='rules'?`[data-rule-edit-form][data-group-id="${A}"][data-rule-id="1"]`:`[data-entry-edit-form="${kind}"][data-group-id="${A}"][data-entry-id="${meta[kind].id}"]`;
async function pointer(selector){ const e=await page.$(selector); if(!e)throw Error('missing '+selector); await e.evaluate(el=>el.scrollIntoView({block:'center'})); await pause(80); await e.click(); }
async function open(kind){
 const toggle=`[data-action="toggle-group-card"][data-group-id="${A}"]`;
 if(await page.$eval(toggle,e=>e.getAttribute('aria-expanded')!=='true')) await pointer(toggle);
 const section=`[data-group-settings-section][data-group-id="${A}"][data-group-section="${meta[kind].section}"]`;
 if(await page.$eval(section,e=>!e.open)) await pointer(`${section} > summary`);
 if(!await page.$(ctl(kind))) { await pointer(`${section} [data-action="load-group-resources"]`); await page.waitForSelector(ctl(kind)); }
}
async function boot(width){
 await fetch(`${BASE}/harness/reset`,{method:'POST'});
 if(page) await page.close();
 page=await browser.newPage(); await page.setViewport({width,height:900});
 page.on('pageerror',e=>errors.push(String(e)));
 await page.setRequestInterception(true);
 page.on('request',r=>{if(!r.url().startsWith(BASE)&&!r.url().startsWith('data:'))r.abort();else r.continue();});
 await page.goto(`${BASE}/settings`,{waitUntil:'networkidle0'});
 await page.waitForSelector('#app[aria-busy="false"]');
 if(width <= 820) await pointer('#sidebar-toggle');
 await pointer('#desktop-nav [data-nav="groups"]');
 await page.waitForSelector(`[data-action="toggle-group-card"][data-group-id="${A}"]`);
 check(await page.evaluate(()=>innerWidth)===width,`${width} real viewport`);
}
async function edit(kind,value){ const sel=`${ctl(kind)} [name="${meta[kind].field}"]`; await pointer(sel); await page.$eval(sel,e=>e.select()); await page.keyboard.type(value); }
async function value(kind){return page.$eval(`${ctl(kind)} [name="${meta[kind].field}"]`,e=>e.value);}
async function dirty(kind){return page.$eval(ctl(kind),e=>e.classList.contains('is-dirty') || !![...e.querySelectorAll('[data-resource-form-state]')].find(x=>x.textContent==='已修改'));}
async function save(){await pointer('#save-button');await page.waitForFunction(()=>!document.querySelector('#app').classList.contains('is-saving'));await pause(160);}
async function server(kind){const r=await fetch(`${BASE}/api/v1/groups/${A}/${kind}`);const x=await r.json();return x[kind.replaceAll('-','_')].find(x=>x.id===meta[kind].id);}
try {
 for(const width of [320,390,1280]){
  for(const kind of Object.keys(meta)){
   await boot(width); await open(kind);
   check(await value(kind)!=='',`${width} ${kind} real seeded form`);
   await edit(kind,`confirmed-${width}-${kind}`); await save();
   check((await server(kind))[meta[kind].field]===`confirmed-${width}-${kind}`,`${width} ${kind} direct blur-click persisted`);
   check(!await dirty(kind),`${width} ${kind} confirmed clears draft`);
   for(const mode of ['unconfirmed','malformed','network']){
    const draft=`draft-${mode}-${width}-${kind}`; await edit(kind,draft);
    if(mode==='network'){
      // Replace the default interception listener for one PATCH only.
      page.removeAllListeners('request');
      page.on('request',r=>{ if(r.method()==='PATCH')r.abort('failed');else if(r.url().startsWith(BASE)||r.url().startsWith('data:'))r.continue();else r.abort(); });
    }else await fetch(`${BASE}/harness/patch-fixture`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    await save();
    check(await value(kind)===draft && await dirty(kind),`${width} ${kind} ${mode} retains draft`);
    check(await page.$$eval('.toast',es=>es.some(e=>/可能已生效|未能确认/.test(e.textContent))),`${width} ${kind} ${mode} warns uncertainty`);
    if(mode==='network'){page.removeAllListeners('request');page.on('request',r=>r.url().startsWith(BASE)||r.url().startsWith('data:')?r.continue():r.abort());}
    const section=`[data-group-settings-section][data-group-id="${A}"][data-group-section="${meta[kind].section}"]`;
    await pointer(`${section} [data-action="load-group-resources"]`);
    await page.waitForSelector(`${section} [data-resource-server-view]`);
    check(await value(kind)===draft,`${width} ${kind} reread retains draft`);
    check(await page.$eval(`${section} [data-resource-server-view]`,e=>e.textContent.includes('confirmed-')),`${width} ${kind} reread exposes server evidence`);
   }
   // Navigate through another group and back; only A has a PATCH draft.
   await pointer(`[data-action="toggle-group-card"][data-group-id="${A}"]`);
   await pointer(`[data-action="toggle-group-card"][data-group-id="${B}"]`);
   check(await page.$eval(`[data-group-card][data-group-id="${B}"]`,e=>!e.querySelector('.is-dirty')),`${width} ${kind} other group clean`);
   await pointer(`[data-action="toggle-group-card"][data-group-id="${B}"]`); await open(kind);
   check(await dirty(kind),`${width} ${kind} group return retains draft`);
   // Hold an actual server ACK; edit back to old baseline while awaiting it.
   const baseline=(await server(kind))[meta[kind].field];
   await edit(kind,`held-${width}-${kind}`);
   let release,enteredResolve; const entered=new Promise(r=>enteredResolve=r);
   page.removeAllListeners('request');
   page.on('request',async r=>{
    if(r.method()==='PATCH'){
      const result=await fetch(r.url(),{method:'PATCH',headers:{'Content-Type':'application/json'},body:r.postData()});
      const body=await result.text(); enteredResolve(); await new Promise(res=>release=res);
      await r.respond({status:result.status,contentType:'application/json',body});
    }else if(r.url().startsWith(BASE)||r.url().startsWith('data:'))r.continue();else r.abort();
   });
   await pointer('#save-button'); await entered; await edit(kind,baseline); check(await value(kind)===baseline,`${width} ${kind} actual typing during ACK`); release();
   await page.waitForFunction(()=>!document.querySelector('#app').classList.contains('is-saving'));await pause(160);
   check((await server(kind))[meta[kind].field]===`held-${width}-${kind}`,`${width} ${kind} held ACK real server applied`);
   check(await value(kind)===baseline && await dirty(kind),`${width} ${kind} edit back during ACK retained`);
   page.removeAllListeners('request');page.on('request',r=>r.url().startsWith(BASE)||r.url().startsWith('data:')?r.continue():r.abort());
   await save();check((await server(kind))[meta[kind].field]===baseline,`${width} ${kind} subsequent save sends retained draft`);
   const requests=await (await fetch(`${BASE}/harness/requests`)).json();
   check(!JSON.stringify(requests).includes(`PATCH /api/v1/groups/${B}`),`${width} ${kind} no cross-group writes`);
  }
  await page.screenshot({path:`${out}/viewport-${width}.png`,fullPage:false});
 }
 check(errors.length===0,'zero page errors');
} catch(error) { if(page){fs.writeFileSync(`${out}/failure.html`,await page.content()); console.error((await page.$eval('body',e=>e.innerText)).slice(0,2500));} throw error; } finally { await browser.close(); fs.writeFileSync(`${out}/browser-result.json`,JSON.stringify({checks,errors},null,2)); }
console.log(JSON.stringify({checks:checks.length,failed:checks.filter(x=>!x.ok).length,errors:errors.length}));
