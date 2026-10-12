/* Real browser, isolated synthetic model/data, no external providers. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8112';
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
 try{
  await page.goto(base);await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
  await create('茅台研究方向确认');
  const run=await send('查看茅台股票能不能买入');const pending=await wait(run,'awaiting_input');
  await page.getByRole('button',{name:'查看已披露财务',exact:true}).waitFor();
  check(pending.runtime.model_calls===0 && pending.attempts_used===0,'boundary_redirect_has_no_provider_calls');
  check((await page.locator('.chat-message--assistant').last().innerText()).includes('不能提供买入或卖出建议'),'clear_boundary_explanation');
  check(await page.getByRole('button',{name:'查看历史行情表现',exact:true}).isVisible(),'actionable_research_choices');
  check(!(await page.locator('.chat-page').innerText()).includes('研究未完成'),'boundary_is_not_execution_error');
  await page.screenshot({path:path.join(out,'boundary-guidance.png')});
  await page.getByRole('button',{name:'查看已披露财务',exact:true}).click();const done=await wait(run,'completed');
  await page.getByRole('button',{name:'查看研究报告',exact:true}).waitFor();
  check((await selected()).run===run,'choice_continues_same_run');
  const report=(await get('/api/runs/'+run+'/report')).content;
  check(report.claims.some(c=>c.kind==='fact'&&c.field==='operating_income'),'chosen_financial_direction_has_verified_fields');
  check(!report.gaps.length && !report.execution_failures,'no_false_trade_decision_data_gap');
  check(done.tool_calls.length===1 && done.tool_calls[0].parameters.thscode==='600519.SH','only_selected_stock_financial_tool');
  await page.getByRole('button',{name:'查看研究报告',exact:true}).click();
  await page.locator('.report-page').waitFor({state:'visible'});
  await page.screenshot({path:path.join(out,'boundary-financial-report.png')});
  await create('结构错误分类验证');const failed=await send('结构错误验证');await wait(failed,'failed');
  await page.getByRole('button',{name:'查看失败详情',exact:true}).waitFor();
  const errorText=await page.locator('.chat-message--assistant').last().innerText();
  check(errorText.includes('模型回复包含不支持的字段'),'schema_failure_has_specific_message');
  check(!errorText.includes('尚无可验证结论')&&!errorText.includes('项缺口'),'schema_failure_not_presented_as_missing_data');
  await page.getByRole('button',{name:'查看失败详情',exact:true}).click();
  await page.locator('.process-page').waitFor({state:'visible'});
  check(await page.getByRole('tab',{name:'研究过程',exact:true}).getAttribute('aria-selected')==='true','failure_details_open_process');
  await page.getByRole('tab',{name:'研究报告',exact:true}).click();
  await page.locator('.report-page').getByText('执行问题',{exact:true}).waitFor();
  check(!(await page.locator('.report-page').innerText()).includes('项缺口'),'report_separates_execution_failure');
  await page.screenshot({path:path.join(out,'model-structure-failure.png')});
  check(errors.length===0,'no_browser_errors');
 }finally{
  fs.writeFileSync(path.join(out,'boundary-checks.json'),JSON.stringify({scope:'synthetic_boundary_guidance',checks,errors},null,2));
  await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
