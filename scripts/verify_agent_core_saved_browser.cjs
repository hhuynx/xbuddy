/* Inspect saved real-model results; this probe makes no new research requests. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8108';
const live=JSON.parse(fs.readFileSync(path.join(root,'docs/verification/agent-core-live.local.json')));
if(live.status!=='passed')throw Error('Real verification must pass first');
const cache=path.resolve(root,live.storage);
if(!cache.startsWith(path.join(root,'.cache','verification')+path.sep))throw Error('Invalid verification storage');
const handoff=JSON.parse(fs.readFileSync(path.join(cache,'browser-handoff.local.json')));
const output=path.join(root,'output/playwright');fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 await context.addCookies(Object.entries(handoff.cookies).map(([name,value])=>({name,value,url:base,httpOnly:true,sameSite:'Lax'})));
 const page=await context.newPage(),errors=[],result={scope:'agent_core_browser_saved_real_results_no_new_provider_calls',checks:{}};
 page.on('pageerror',e=>errors.push(e.name));
 const check=(v,n)=>{if(!v)throw Error(n);result.checks[n]='passed';console.log(n);};
 const api=async p=>{const response=await context.request.get(base+p);if(!response.ok())throw Error('API '+response.status());return response.json();};
 const snap=async n=>fs.writeFileSync(path.join(output,'agent-core-real-'+n+'.txt'),await page.locator('body').ariaSnapshot());
 try{
   const initial=await api('/api/runs/'+handoff.run);
   await page.goto(base);await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
   await page.evaluate(h=>sessionStorage.setItem('xbuddy.workbench',JSON.stringify({thread:h.thread,run:h.run,cursor:0})),handoff);
   await page.reload();await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
   await page.getByRole('tab',{name:'研究报告',exact:true}).click();await page.locator('.report-page').getByText('完整报告',{exact:true}).waitFor();await snap('report');
   check((await api('/api/runs/'+handoff.run)).status==='completed','saved_real_report_restored');
   await page.getByRole('tab',{name:'对话',exact:true}).click();check((await page.locator('body').innerText()).includes('怎么研究股票'),'real_conversation_history_visible');await page.getByRole('tab',{name:'研究报告',exact:true}).click();
   const report=(await api('/api/runs/'+handoff.run+'/report')).content;
   check(report.goal_coverage.every(c=>c.status==='satisfied') && await page.getByRole('button',{name:/查看引用 \[/}).count()>0,'real_synthesis_visible_with_source_buttons');
   await page.getByRole('button',{name:/查看引用 \[/}).first().click();
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).waitFor();await snap('evidence');
   check((await page.locator('.evidence-drawer').innerText()).includes('原始'),'real_synthesis_opens_source_drawer');
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).click();
   for(const [label,ext] of [['Markdown','md'],['HTML','html'],['结论 CSV','csv'],['证据索引','json']]){
     const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出 '+label,exact:true}).click();
     const download=await pending;await download.saveAs(path.join(output,'agent-core-real-report.'+ext));
     check(await download.failure()===null,'real_browser_download_'+ext);
   }
   await page.setViewportSize({width:390,height:844});
   await page.getByRole('tab',{name:'研究报告',exact:true}).click();await snap('mobile');
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'real_mobile_no_horizontal_overflow');
   await page.screenshot({path:path.join(output,'agent-core-real-mobile.png'),fullPage:true});
   const final=await api('/api/runs/'+handoff.run);
   check(final.runtime.model_calls===initial.runtime.model_calls && final.attempts_used===initial.attempts_used,'inspection_did_not_dispatch_new_calls');
   check(errors.length===0,'real_browser_no_uncaught_errors');result.status='passed';
 }catch(error){result.status='failed';result.failed_check=error.message;await snap('failure').catch(()=>{});}
 finally{await browser.close();fs.writeFileSync(path.join(root,'docs/verification/agent-core-real-browser.local.json'),JSON.stringify(result,null,2));}
 if(result.status!=='passed'){console.error(result.failed_check);process.exitCode=1;}
})();
