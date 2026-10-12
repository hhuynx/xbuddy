/* Inspect saved multi-year real results without dispatching provider requests. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8108';
const live=JSON.parse(fs.readFileSync(path.join(root,'docs/verification/financial-history-live.local.json')));
if(live.status!=='passed')throw Error('Real verification must pass first');
const cache=path.resolve(root,live.storage);
if(!cache.startsWith(path.join(root,'.cache','verification')+path.sep))throw Error('Invalid verification storage');
const handoff=JSON.parse(fs.readFileSync(path.join(cache,'browser-handoff.local.json')));
const output=path.join(root,'output/playwright');fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 await context.addCookies(Object.entries(handoff.cookies).map(([name,value])=>({name,value,url:base,httpOnly:true,sameSite:'Lax'})));
 const page=await context.newPage(),errors=[],result={scope:'multi_annual_browser_saved_real_results_no_new_provider_calls',checks:{}};
 page.on('pageerror',e=>errors.push(e.name));
 const check=(v,n)=>{if(!v)throw Error(n);result.checks[n]='passed';console.log(n);};
 const api=async p=>{const response=await context.request.get(base+p);if(!response.ok())throw Error('API '+response.status());return response.json();};
 const snap=async n=>fs.writeFileSync(path.join(output,'financial-history-'+n+'.txt'),await page.locator('body').ariaSnapshot());
 try{
   const initial=await api('/api/runs/'+handoff.run);
   await page.goto(base);await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
   await page.evaluate(h=>sessionStorage.setItem('xbuddy.workbench',JSON.stringify({thread:h.thread,run:h.run,cursor:0})),handoff);
   await page.reload();await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
   await page.getByRole('tab',{name:'研究报告',exact:true}).click();await page.locator('.report-page').getByText('完整报告',{exact:true}).waitFor();await snap('report');
   const report=(await api('/api/runs/'+handoff.run+'/report')).content;
   const years=live.run.fiscal_years;
   const table=page.locator('.financial-table');
   check(report.status==='complete' && report.gaps.length===0,'complete_report_without_false_annual_gaps');
   const narrative=await page.locator('.report-page .analysis-paragraph').allInnerTexts();
   check(narrative.join('\n').includes('亿元') && !/\d\.\d+e\+\d+/i.test(narrative.join('\n')),'verified_amounts_display_in_readable_units');
   check(years.every(y=>narrative.join('\n').includes(y+' 年')),'formatted_narrative_keeps_fiscal_years');
   for(const year of years)check(await table.getByRole('row').filter({hasText:year+'-12-31'}).count()===1,'annual_row_'+year);
   await table.screenshot({path:path.join(output,'financial-history-table.png')});
   const oldest=table.getByRole('row').filter({hasText:years[0]+'-12-31'});
   await oldest.getByRole('button',{name:'查看依据',exact:true}).click();
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).waitFor();await snap('oldest-source');
   await page.locator('.evidence-drawer').getByRole('button',{name:'Expand \"原始字段与响应\"',exact:true}).click();
   const fact=report.claims.find(c=>c.kind==='fact' && c.field==='operating_income' && c.fiscal_year===years[0]);
   await page.locator('.evidence-drawer .raw-path').filter({hasText:fact.reference.raw_path}).waitFor({state:'visible'});
   const drawer=await page.locator('.evidence-drawer').innerText();
   check(drawer.includes(fact.reference.raw_path) && drawer.includes('原始值：') && drawer.includes('所选报告期：'+years[0]+'-12-31') && drawer.includes('所选披露日期：'),'oldest_annual_row_opens_exact_original_source');
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).click();
   check(await page.getByRole('button',{name:/查看引用 \[/}).count()>0,'synthesis_has_source_buttons');
   for(const [label,ext] of [['Markdown','md'],['HTML','html'],['结论 CSV','csv'],['证据索引','json']]){
     const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出 '+label,exact:true}).click();
     const download=await pending,target=path.join(output,'financial-history-report.'+ext);await download.saveAs(target);
     check(await download.failure()===null,'browser_download_'+ext);
     if(ext!=='json')check(years.every(y=>fs.readFileSync(target,'utf8').includes(String(y))),'download_keeps_years_'+ext);
   }
   await page.screenshot({path:path.join(output,'financial-history-desktop.png'),fullPage:true});
   await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'研究报告',exact:true}).click();await snap('mobile');
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile_no_horizontal_overflow');
   await page.screenshot({path:path.join(output,'financial-history-mobile.png'),fullPage:true});
   const final=await api('/api/runs/'+handoff.run);
   check(final.runtime.model_calls===initial.runtime.model_calls && final.attempts_used===initial.attempts_used,'inspection_did_not_dispatch_new_calls');
   check(errors.length===0,'browser_no_uncaught_errors');result.status='passed';
 }catch(error){result.status='failed';result.failed_check=error.message;console.error(error.stack || String(error));await snap('failure').catch(()=>{});}
 finally{await browser.close();fs.writeFileSync(path.join(root,'docs/verification/financial-history-browser.local.json'),JSON.stringify(result,null,2));}
 if(result.status!=='passed'){console.error(result.failed_check);process.exitCode=1;}
})();
