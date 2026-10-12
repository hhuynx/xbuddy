/* Browser interaction checks; Playwright is an optional QA dependency. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8107';
const output=path.join(root,'output/playwright');
fs.mkdirSync(output,{recursive:true});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage(),errors=[],result={scope:'agent_core_browser_synthetic',checks:{}};
 page.on('pageerror',e=>errors.push(e.name));
 const check=(v,n)=>{if(!v)throw Error(n);result.checks[n]='passed';console.log(n);};
 const api=async p=>{const r=await context.request.get(base+p);if(!r.ok())throw Error('API '+r.status());return r.json();};
 const selected=()=>page.evaluate(()=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}'));
 const snap=async n=>fs.writeFileSync(path.join(output,'agent-core-'+n+'.txt'),await page.locator('body').ariaSnapshot());
 const wait=async(rid,statuses)=>{const end=Date.now()+40000;while(Date.now()<end){const s=await api('/api/runs/'+rid);if(statuses.includes(s.status))return s;await pause(100);}throw Error('run_wait_timeout');};
 const submit=async goal=>{
   await page.getByRole('tab',{name:'对话',exact:true}).click();
   await snap('before-submit');const old=(await selected()).run;
   await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).fill(goal);
   await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).press('Tab');
   await page.getByRole('button',{name:'发送',exact:true}).click();
   await page.waitForFunction(previous=>{const s=JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}');return s.run&&s.run!==previous;},old);
   return (await selected()).run;
 };
 try{
   await page.goto(base);await page.getByText('实时同步',{exact:true}).waitFor();await snap('initial');
   let run=await submit('怎么研究股票');let state=await wait(run,['completed']);
   await page.getByText('一般知识 · 未查询本次金融数据',{exact:true}).first().waitFor();
   check(state.attempts_used===0 && (await page.locator('body').innerText()).includes('商业模式'),'methodology_answer_without_tools');
   await page.reload();await page.getByText('实时同步',{exact:true}).waitFor();
   check((await page.locator('body').innerText()).includes('商业模式'),'conversation_survives_reload');
   run=await submit('请研究一只股票');state=await wait(run,['awaiting_input']);
   await page.getByText('需要补充信息',{exact:true}).waitFor();await snap('clarification');
   await page.reload();await page.getByText('实时同步',{exact:true}).waitFor();
   await page.getByRole('button',{name:'600519.SH',exact:true}).waitFor();
   check((await selected()).run===run,'pending_clarification_restored');
   await page.getByRole('button',{name:'600519.SH',exact:true}).click();state=await wait(run,['completed']);
   await page.getByRole('button',{name:'查看研究报告',exact:true}).waitFor();
   check(state.runtime.model_calls===3 && !state.pending_input,'answer_continues_same_run');
   await snap('grounded-answer');
   await page.getByRole('button',{name:/查看引用 \[/}).first().click();
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).waitFor();
   check((await page.locator('.evidence-drawer').innerText()).includes('原始'),'narrative_links_to_original_evidence');
   await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).click();
   run=await submit('补充任务验证：比较两只股票历史价格');state=await wait(run,['completed']);
   check(state.runtime.replan_count===1 && state.tasks.length===4,'successful_tool_incomplete_goal_extends_plan');
   run=await submit('回答校验验证：比较历史价格');state=await wait(run,['partial']);
   await page.getByRole('tab',{name:'研究报告',exact:true}).click();
   await page.getByText('模型回答的结构、证据引用或数值未通过验证',{exact:false}).waitFor();
   const report=(await api('/api/runs/'+run+'/report')).content;
   check(!report.narrative && report.claims.some(c=>c.kind==='fact') && !report.claims.map(c=>c.text).join('\n').includes('999'),'invalid_model_numbers_excluded_with_visible_reason');
   await page.setViewportSize({width:390,height:844});await snap('mobile');
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile_no_page_horizontal_overflow');
   await page.getByRole('tab',{name:'研究报告',exact:true}).click();
   await page.screenshot({path:path.join(output,'agent-core-mobile.png'),fullPage:true});
   check(errors.length===0,'browser_no_uncaught_errors');
   result.status='passed';
 }catch(error){result.status='failed';result.failed_check=error.message;await snap('failure');await page.screenshot({path:path.join(output,'agent-core-failure.png'),fullPage:true});}
 finally{await browser.close();fs.mkdirSync(path.join(root,'docs/verification'),{recursive:true});fs.writeFileSync(path.join(root,'docs/verification/agent-core-browser.local.json'),JSON.stringify(result,null,2));}
 if(result.status!=='passed'){console.error(result.failed_check);process.exitCode=1;}
})();
