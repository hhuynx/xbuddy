/* Browser fault acceptance against scripts/fault_fixture_server.py only.
 * Playwright is an optional QA dependency, never a production dependency.
 * Set XBUDDY_PLAYWRIGHT_CORE to its installed module path if not in node_modules.
 */
const {chromium} = require(process.env.XBUDDY_PLAYWRIGHT_CORE || 'playwright-core');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const base = process.env.XBUDDY_QA_BASE_URL || 'http://127.0.0.1:8097';
const data = process.env.XBUDDY_QA_DATA_DIR;
if (!data || !path.resolve(data).startsWith(path.join(root, '.cache', 'verification') + path.sep))
  throw Error('Set XBUDDY_QA_DATA_DIR to the isolated verification data directory');
const output = path.join(root, 'output/playwright');
fs.mkdirSync(output, {recursive: true});
const pause = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.XBUDDY_BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  const result = {scope: 'batch6_browser_synthetic_faults', checks: {}};
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, acceptDownloads: true});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.name));
  const check = (value, name) => {
    if (!value) throw Error(name);
    result.checks[name] = 'passed';
    console.log(name);
  };
  const snap = async name => fs.writeFileSync(path.join(output, 'batch6-' + name + '-snapshot.txt'), await page.locator('body').ariaSnapshot());
  const api = async route => {
    const response = await context.request.get(base + route);
    if (!response.ok()) throw Error('API status ' + response.status());
    return response.json();
  };
  const selected = () => page.evaluate(() => JSON.parse(sessionStorage.getItem('xbuddy.workbench') || '{}'));
  const wait = async (run, statuses) => {
    const end = Date.now() + 30000;
    while (Date.now() < end) {
      const state = await api('/api/runs/' + run);
      if (statuses.includes(state.status)) return state;
      await pause(100);
    }
    throw Error('Run wait timeout');
  };
  const start = async goal => {
    await snap('before-submit');
    const before = (await selected()).run;
    await page.getByRole('textbox', {name: '研究目标 / 追问', exact: true}).fill(goal);
    await page.getByRole('textbox', {name: '研究目标 / 追问', exact: true}).press('Tab');
    await page.getByRole('button', {name: '开始自动研究', exact: true}).click();
    await page.waitForFunction(old => {
      const current = JSON.parse(sessionStorage.getItem('xbuddy.workbench') || '{}').run;
      return current && current !== old;
    }, before);
    return (await selected()).run;
  };
  const expireOwner = async run => {
    const py = path.join(root, '.venv/Scripts/python.exe');
    // Controlled local database mutation in the fixture only; no fault API is installed in the product.
    const code = 'import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); c.execute("UPDATE sessions SET expires_at=? WHERE id=(SELECT session_id FROM runs WHERE id=?)",(int(time.time())-1,sys.argv[2])); c.commit(); c.close()';
    const executed = spawnSync(py, ['-c', code, path.join(data, 'xbuddy.sqlite3'), run], {encoding: 'utf8'});
    if (executed.status !== 0) throw Error('Fixture expiry failed');
  };
  try {
    await page.goto(base);
    await page.locator('.sync-badge').filter({hasText: '实时同步'}).waitFor();
    await snap('initial');
    let run = await start('构造限流测试：验证供应商失败时保留缺口');
    let state = await wait(run, ['failed']);
    check(state.attempts_used === 3 && state.tool_calls[0].quality === 'failed', 'rate_limit_has_three_audited_attempts');
    await page.getByText(/研究未完成 · \d+ 项缺口/).waitFor();
    await snap('rate-limit');
    check(await page.locator('.claim-card').filter({hasText: '事实'}).count() === 0, 'fault_report_has_no_numerical_claim_cards');
    check((await page.locator('body').innerText()).includes('缺口'), 'fault_report_explains_missing_data');
    run = await start('构造模型结构测试：验证错误规划不派发金融请求');
    state = await wait(run, ['failed']);
    check(state.runtime.model_calls === 2 && state.tool_calls.length === 0, 'malformed_planning_has_one_repair_and_zero_tools');
    await snap('model-failure');
    run = await start('构造报告恢复测试：比较两个标的历史价格');
    state = await wait(run, ['recoverable_failed']);
    await page.getByRole('button', {name: '继续研究', exact: true}).waitFor();
    await snap('recoverable');
    const callIds = state.tool_calls.map(c => c.id);
    await page.getByRole('button', {name: '继续研究', exact: true}).click();
    state = await wait(run, ['completed']);
    check(JSON.stringify(state.tool_calls.map(c => c.id)) === JSON.stringify(callIds) && state.artifacts.length === 1,
          'browser_resume_reuses_calls_and_committed_report');
    await page.getByText('完整报告', {exact: true}).waitFor();
    await snap('recovered');
    // Disconnect a live page, reconnect, and recover the selected run/report from persisted state.
    await context.setOffline(true);
    await pause(750);
    await context.setOffline(false);
    await page.reload();
    await page.locator('.sync-badge').filter({hasText: '实时同步'}).waitFor();
    await page.getByText('完整报告', {exact: true}).waitFor();
    check((await selected()).run === run, 'disconnect_and_reload_restore_selected_report');
    await snap('restored');
    await page.getByRole('button', {name: '长期记忆', exact: true}).click();
    await page.getByRole('button', {name: '新增记忆', exact: true}).click();
    await snap('memory-editor');
    await page.getByRole('textbox', {name: '记忆内容', exact: true}).fill('等待审批时删除这条研究偏好');
    await page.getByRole('button', {name: '保存记忆', exact: true}).click();
    await page.getByText('保存长期记忆', {exact: true}).waitFor({state: 'hidden'});
    await page.getByRole('button', {name: '关闭', exact: true}).click();
    await page.getByRole('dialog').waitFor({state: 'hidden'});
    const memory = (await api('/api/memories')).memories.find(m => m.content.content === '等待审批时删除这条研究偏好');
    await page.getByRole('button', {name: '预算设置', exact: true}).click();
    await snap('budget');
    await page.getByRole('spinbutton', {name: '模型调用上限', exact: true}).fill('0');
    await page.getByRole('button', {name: '保存预算', exact: true}).click();
    await page.getByRole('dialog').waitFor({state: 'hidden'});
    run = await start('比较两个标的历史价格并检查有效记忆');
    await wait(run, ['awaiting_approval']);
    await page.getByRole('button', {name: '批准', exact: true}).waitFor();
    await snap('memory-approval');
    await page.getByRole('button', {name: '长期记忆', exact: true}).click();
    await snap('memory-delete');
    await page.getByRole('button', {name: '删除', exact: true}).click();
    await page.getByText('暂无符合条件的记忆。', {exact: true}).waitFor();
    await page.getByRole('button', {name: '关闭', exact: true}).click();
    await page.getByRole('dialog').waitFor({state: 'hidden'});
    await page.getByRole('button', {name: '批准', exact: true}).click();
    await wait(run, ['completed']);
    const usage = (await api('/api/runs/' + run + '/context')).memories;
    check(usage.some(m => m.memory_id === memory.id && m.status === 'withdrawn' && !m.last_used_at), 'browser_delete_during_approval_withdraws_memory');
    await page.getByText('完整报告', {exact: true}).waitFor();
    await page.screenshot({path: path.join(output, 'batch6-recovered-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await snap('mobile');
    check(await page.getByRole('tab', {name: '任务', exact: true}).isVisible(), 'mobile_fault_recovery_workspace_tabs');
    await page.screenshot({path: path.join(output, 'batch6-mobile.png'), fullPage: true});
    await page.setViewportSize({width: 1440, height: 1000});
    await page.getByRole('button', {name: '新建研究', exact: true}).click();
    await snap('expired-dialog');
    await page.getByRole('textbox', {name: '研究标题', exact: true}).fill('过期会话不应创建研究');
    await expireOwner(run);
    await page.getByRole('button', {name: '创建研究空间', exact: true}).click();
    await page.getByText('会话已过期，请刷新工作台重新连接', {exact: true}).waitFor();
    check(await page.getByRole('dialog').isVisible(), 'expired_live_ui_callback_shows_error');
    await snap('expired-blocked');
    check((await context.request.get(base + '/api/threads')).status() === 401, 'expired_session_api_denied');
    check(errors.length === 0, 'no_uncaught_browser_errors');
    result.status = 'passed';
  } catch (error) {
    result.status = 'failed'; result.reason = error.message;
    await snap('failure').catch(() => {});
    await page.screenshot({path: path.join(output, 'batch6-failure.png'), fullPage: true}).catch(() => {});
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(root, 'docs/verification/browser-batch6.local.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = result.status === 'passed' ? 0 : 1;
})();
