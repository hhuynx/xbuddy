/* Publication and classification QA, with synthetic data in an isolated server. */
const {chromium}=require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),base=process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8107';
const output=path.join(root,'output/playwright');fs.mkdirSync(output,{recursive:true});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage(),errors=[],result={scope:'publication_browser_synthetic_no_provider_calls',checks:{}};
 page.on('pageerror',e=>errors.push(e.message));
 const check=(v,n)=>{if(!v)throw Error(n);result.checks[n]='passed';console.log(n);};
 const api=async p=>{const r=await context.request.get(base+p);if(!r.ok())throw Error('API '+r.status()+' '+p);return r.json();};
 const selected=()=>page.evaluate(()=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}'));
 const snap=async name=>fs.writeFileSync(path.join(output,'publication-'+name+'.txt'),await page.locator('body').ariaSnapshot());
 const connected=()=>page.locator('.sync-badge').filter({hasText:'实时同步'}).waitFor();
 const wait=async id=>{const end=Date.now()+60000;while(Date.now()<end){const s=await api('/api/runs/'+id);if(['completed','partial','failed','blocked'].includes(s.status))return s;await pause(150);}throw Error('run_wait_timeout');};
 const submit=async goal=>{await page.getByRole('tab',{name:'对话',exact:true}).click();const old=(await selected()).run;await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).fill(goal);await page.getByRole('textbox',{name:'研究目标 / 追问',exact:true}).press('Tab');await page.getByRole('button',{name:'发送',exact:true}).click();await page.waitForFunction(old=>JSON.parse(sessionStorage.getItem('xbuddy.workbench')||'{}').run!==old,old);return (await selected()).run;};
 try{
  await page.goto(base);await connected();const csrf=(await api('/api/session')).csrf_token;
  let run=await submit('发布审核验证：如何研究股票');let state=await wait(run);
  check(state.status==='completed' && state.runtime.model_calls===2 && state.attempts_used===0,'unsafe_answer_repaired_with_bounded_model_calls');
  const assistant=()=>page.locator('.chat-message--assistant').last();
  await assistant().getByText('一般知识',{exact:true}).waitFor();
  check((await assistant().innerText()).includes('商业模式'),'knowledge_label_and_safe_answer_visible');
  const aid=state.artifacts[0].id;
  const seed=await context.request.post(base+'/_qa/legacy-publication/'+aid,{data:{},headers:{'X-CSRF-Token':csrf}});check(seed.ok(),'legacy_fixture_prepared');
  await page.reload();await connected();await assistant().getByText('回答未通过发布校验',{exact:true}).waitFor();
  check(!(await page.locator('body').innerText()).includes('上涨毫无悬念'),'legacy_prohibited_answer_hidden_with_visible_reason');
  for(const suffix of ['', '/print', '/export?format=markdown', '/export?format=html', '/export?format=csv', '/export?format=evidence']){
   const r=await context.request.get(base+'/api/artifacts/'+aid+suffix);check(r.status()===409,'legacy_delivery_denied_'+(suffix||'detail'));
  }
  check(!JSON.stringify(await api('/api/runs/'+run)).includes('上涨毫无悬念'),'run_snapshot_masks_old_prohibited_content');
  await page.getByRole('tab',{name:'研究报告',exact:true}).click();await page.locator('.report-page .error-card').waitFor();
  check((await page.locator('.report-page .error-card').innerText()).includes('发布校验'),'legacy_report_rejection_shown');await snap('legacy-blocked');
  await page.getByRole('button',{name:'重新校验并保存版本',exact:true}).click();
  await page.locator('.report-page').getByText('一般知识',{exact:true}).first().waitFor();
  state=await api('/api/runs/'+run);const restored=await api('/api/artifacts/'+state.artifacts.at(-1).id);
  check(restored.version===2 && restored.content.validation_status==='passed','revalidation_restores_safe_version_without_model_request');
  check(state.runtime.model_calls===2,'revalidation_preserves_model_budget');
  run=await submit('分类展示验证：比较股票历史价格');state=await wait(run);check(state.status==='completed','classified_research_completes');
  await assistant().getByText('推断',{exact:true}).waitFor();await assistant().getByText('不确定信息',{exact:true}).waitFor();
  check((await assistant().innerText()).includes('历史样本不足以判断'),'chat_summary_classifies_each_paragraph');
  await page.screenshot({path:path.join(output,'publication-chat-desktop.png')});await snap('chat');
  await assistant().getByRole('button',{name:/查看引用 \[/}).first().click();await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).waitFor();
  check((await page.locator('.evidence-drawer').innerText()).includes('支持的核验事实'),'classification_keeps_original_evidence_links');await page.getByRole('button',{name:'关闭证据抽屉',exact:true}).click();
  await assistant().getByRole('button',{name:'查看研究报告',exact:true}).click();
  await page.locator('.report-page').getByText('推断',{exact:true}).first().waitFor();await page.locator('.report-page').getByText('不确定信息',{exact:true}).first().waitFor();
  check(await page.locator('.report-page').getByText('相关核验数据',{exact:true}).isVisible(),'report_separates_classified_analysis_and_verified_facts');
  const reportAid=state.artifacts[0].id;
  for(const suffix of ['/export?format=markdown','/export?format=html','/print']){
   const r=await context.request.get(base+'/api/artifacts/'+reportAid+suffix);const text=await r.text();check(r.ok() && ['事实','推断','不确定信息'].every(t=>text.includes(t)),'export_categories_'+suffix);
  }
  await page.screenshot({path:path.join(output,'publication-report-desktop.png')});
  if(process.env.XBUDDY_QA_MOBILE==='true'){
   await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'对话',exact:true}).click();await assistant().getByText('不确定信息',{exact:true}).waitFor();
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile_categories_visible_without_page_overflow');await snap('mobile');await page.screenshot({path:path.join(output,'publication-chat-mobile.png')});
  }
  check(errors.length===0,'browser_no_uncaught_errors');result.status='passed';
 }catch(e){result.status='failed';result.failed_check=e.message;console.error(e.stack);await snap('failure').catch(()=>{});await page.screenshot({path:path.join(output,'publication-failure.png')}).catch(()=>{});}
 finally{await browser.close();fs.writeFileSync(path.join(root,'docs/verification/publication-browser.local.json'),JSON.stringify(result,null,2));}
 if(result.status!=='passed')process.exitCode=1;
})();
