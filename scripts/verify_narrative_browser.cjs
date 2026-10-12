/* Desktop only; complete saved analysis and source navigation with synthetic fixtures. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8113';
const out=path.resolve(__dirname,'../output/playwright');fs.mkdirSync(out,{recursive:true});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage(),errors=[],checks={};page.on('pageerror',e=>errors.push(e.message));
 const check=(v,n)=>{if(!v)throw Error(n);checks[n]='passed';console.log(n);};
 const selected=()=>page.evaluate(()=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}'));
 const get=async p=>{const r=await context.request.get(base+p);if(!r.ok())throw Error('API '+p+' '+r.status());return r.json();};
 const wait=async(id,status)=>{const end=Date.now()+45000;while(Date.now()<end){const s=await get('/api/runs/'+id);if(s.status===status)return s;await pause(100);}throw Error('timeout '+status);};
 const create=async title=>{
  await page.getByRole('button',{name:'新建研究',exact:true}).click();
  await page.getByRole('textbox',{name:'研究标题',exact:true}).fill(title);
  await page.getByRole('button',{name:'创建研究空间',exact:true}).click();
  await page.getByRole('dialog').waitFor({state:'hidden'});await pause(250);
 };
 const send=async text=>{
  const old=(await selected()).run;
  const input=page.getByRole('textbox',{name:'研究目标 / 追问',exact:true});await input.fill(text);await input.press('Tab');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await page.waitForFunction(old=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}').run!==old,old);
  return (await selected()).run;
 };
 const answer=()=>page.locator('.chat-message--assistant').last();
 try{
  await page.goto(base);await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
  await create('完整分析展示');
  const run=await send('完整文字分析验证：比较历史价格并解释研究含义');await wait(run,'completed');
  await answer().getByText('后续验证方向',{exact:true}).waitFor();
  check(await answer().locator('.message-section').count()===3,'all_analysis_sections_in_chat');
  check(await answer().locator('.message-body').count()===5,'all_five_paragraphs_including_facts');
  check((await answer().innerText()).includes('完整分析末段'),'paragraph_after_first_three_is_shown');
  const text=await answer().innerText();
  check(['事实','推断','不确定信息'].every(t=>text.includes(t)),'each_paragraph_classified');
  const report=(await get('/api/runs/'+run+'/report')).content;
  check(text.includes(report.narrative[0].paragraphs[0].text),'verified_fact_text_in_chat');
  await answer().getByRole('button',{name:/查看引用 \[/}).first().click();
  await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).waitFor();
  check((await page.locator('.evidence-drawer').innerText()).includes('支持的核验事实'),'citation_opens_original_source');
  await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).click();
  await page.locator('.evidence-drawer').waitFor({state:'hidden'});
  await page.screenshot({path:path.join(out,'narrative-full-chat.png')});
  await page.reload();await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
  await answer().getByText('后续验证方向',{exact:true}).waitFor();
  check((await answer().innerText()).includes('完整分析末段'),'full_answer_survives_reload');
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'desktop_no_horizontal_overflow');
  await create('被截断后的分析恢复');
  const recovery=await send('分析恢复验证：解释历史数据');const state=await wait(recovery,'partial');
  await answer().getByText('后续验证方向',{exact:true}).waitFor();
  const recovered=(await get('/api/runs/'+recovery+'/report')).content;
  check(state.runtime.model_calls===4 && state.attempts_used===4,'recovery_has_bounded_models_and_no_new_tools');
  check(recovered.method.authoring==='model_partial_analysis_recovery' && recovered.status==='partial','recovery_keeps_partial_status');
  check((await answer().innerText()).includes('尚未确认研究目标完整覆盖'),'recovery_limitation_visible_in_chat');
  check((await answer().innerText()).includes('完整分析末段'),'recovered_analysis_visible_in_chat');
  await page.screenshot({path:path.join(out,'narrative-recovered-chat.png')});
  await create('分析校验失败保留事实');
  const invalid=await send('回答校验验证：比较历史价格');await wait(invalid,'partial');
  await answer().getByText('已核验数据',{exact:true}).waitFor();
  check((await answer().innerText()).includes('工具取得的事实'),'failed_analysis_shows_verified_facts');
  check(!(await answer().innerText()).includes('999%'),'rejected_model_prose_not_shown');
  check(await answer().getByRole('button',{name:/查看引用 \[/}).count()>0,'fact_fallback_retains_source_links');
  check(errors.length===0,'no_browser_errors');
 }finally{
  fs.writeFileSync(path.join(out,'narrative-checks.json'),JSON.stringify({scope:'desktop_synthetic_narrative',checks,errors},null,2));
  await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
