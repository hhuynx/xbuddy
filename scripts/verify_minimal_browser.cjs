/* Browser verification uses only an isolated synthetic fixture server. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8107';
const out=path.resolve(__dirname,'../output/playwright');fs.mkdirSync(out,{recursive:true});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage(),errors=[],checks={};page.on('pageerror',e=>errors.push(e.message));
 const check=(v,name)=>{if(!v)throw Error(name);checks[name]='passed';console.log(name);};
 const api=async p=>{const r=await context.request.get(base+p);if(!r.ok())throw Error(p+' '+r.status());return r.json();};
 try{
  await page.goto(base);await page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
  await page.getByRole('button',{name:'研究资料',exact:true}).click();
  const dialog=page.getByRole('dialog');await dialog.waitFor();
  const original='公司面临行业竞争风险，拟加强研发和渠道建设。';
  await dialog.locator('input[type=file]').setInputFiles({name:'年报测试.txt',mimeType:'text/plain',buffer:Buffer.from(original)});
  await page.waitForFunction(text=>[...document.querySelectorAll('textarea')].some(e=>e.value===text),original);
  check(true,'utf8_file_upload_populates_original_text');
  await dialog.getByRole('textbox',{name:'资料标题',exact:true}).fill('合成测试年报');
  await dialog.getByRole('textbox',{name:'原文 HTTPS 链接',exact:true}).fill('https://example.org/annual-report');
  await dialog.getByRole('textbox',{name:'发布时间（带时区）',exact:true}).fill('2026-03-31T09:00:00+08:00');
  await dialog.getByRole('button',{name:'导入资料',exact:true}).click();
  await dialog.getByRole('link',{name:'合成测试年报',exact:true}).waitFor();
  check((await api('/api/documents')).documents.some(d=>d.title==='合成测试年报'),'document_saved_with_source_metadata');
  await dialog.getByRole('textbox',{name:'检索原文',exact:true}).fill('行业竞争风险');
  await dialog.getByRole('button',{name:'检索',exact:true}).click();
  await dialog.getByText(original,{exact:true}).waitFor();
  check(await dialog.getByText(/第 1 页 · 字符/).isVisible(),'retrieval_shows_original_page_and_position');
  await page.screenshot({path:path.join(out,'minimal-documents.png'),fullPage:true});
  await dialog.getByRole('button',{name:'关闭',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).fill('原文检索验证：基于导入年报整理行业竞争风险，保留原文出处');
  await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).press('Tab');
  await pause(250);
  await page.getByRole('button',{name:'发送',exact:true}).click();
  let run,state;const end=Date.now()+60000;
  while(Date.now()<end){run=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}').run);if(run){state=await api('/api/runs/'+run);if(['completed','partial','failed','blocked'].includes(state.status))break;}await pause(150);}
  if(!state || state.status!=='completed'){
   await page.screenshot({path:path.join(out,'minimal-failed.png'),fullPage:true});
   fs.writeFileSync(path.join(out,'minimal-failed.txt'),await page.locator('body').ariaSnapshot());
  }
  check(state && state.status==='completed','agent_uses_registered_document_tool_and_finishes');
  await page.getByRole('tab',{name:'研究过程',exact:true}).click();
  await page.getByText('检索年报风险原文',{exact:true}).waitFor();
  await page.getByText('调用追踪',{exact:true}).click();
  await page.getByText(/工具调用 · search_documents/).waitFor();
  check(true,'process_ui_shows_tool_trace');
  const trace=await api('/api/runs/'+run+'/trace');
  check(trace.spans.some(s=>s.name==='model') && trace.model_usage.length===2,'trace_has_model_spans_and_usage');
  await page.getByRole('tab',{name:'研究报告',exact:true}).click();
  const report=await api('/api/runs/'+run+'/report');
  check(report.content.claims.some(c=>c.field==='document_excerpt' && c.reference.source_url==='https://example.org/annual-report'),'report_preserves_original_source_reference');
  await page.getByText(/全部核验事实/).click();
  await page.getByText(/原文摘录 · 合成测试年报/).first().click();
  await page.getByRole('link',{name:'合成测试年报',exact:true}).waitFor();
  check(await page.getByText(/来源真实性尚需外部核验/).isVisible(),'evidence_drawer_distinguishes_source_authenticity');
  check(!(await page.locator('.raw-field').innerText()).includes('null') && (await page.locator('.raw-field').innerText()).includes(original),'exact_original_field_is_resolved');
  await page.screenshot({path:path.join(out,'minimal-evidence.png'),fullPage:true});
  check(errors.length===0,'browser_has_no_page_errors');
  fs.writeFileSync(path.join(out,'minimal-checks.json'),JSON.stringify({scope:'synthetic_browser_no_live_provider_calls',checks,errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
