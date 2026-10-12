# 第六批：异常、边界与整体验收

日期：2026-10-11。Python 方案 v0.6；SQLite schema 仍为 3。第 1–6 批已完成本地阶段验收，下一批为部署与交付。

后续复核修正：上述是本批历史出口，不能作为当前完整产品的完成状态。用户试用发现基础问答、澄清、目标检查和综合回答缺失；下一步先实施 [基础能力补齐 A–C](agent-capability-review.md)，通过后再部署。下列已有测试与实际集成记录保留其原范围。

## 本批交付

- `tests/test_acceptance.py` 新增 41 个构造故障/边界用例，连同原有 82 项组成 123 项检查。
- `scripts/verify_batch6_live.py` 可重跑真实模型、扶摇、预算审批、两次独立进程重启、关联追问、记忆、压缩、报告版本、四类导出和 SSE 补传。
- `scripts/fault_fixture_server.py` 与 `scripts/verify_batch6_browser.cjs` 提供隔离的浏览器故障验收；构造模型和市场数据不读取 `.env`，也不在生产服务安装故障接口。
- `scripts/audit_publication.py` 扫描拟公开源码中的实际配置密钥、会话/市场 JSON、私有运行文件和忽略规则。当前目录尚无 Git 仓库；该检查不代表审计了 Git 历史或远程仓库。
- [验收矩阵](acceptance-matrix.md)、[抽查与签验记录](review-record.md)，以及测试、AI 使用、修正和边界记录。

## 已修复并复验

| 问题 | 修正与验证 |
| --- | --- |
| 上下文只校验正文，原文范围和摘要元数据可能被改动 | 校验原始事件、上下文条目、原文范围、事件计数、历史指纹和压缩摘要。新快照用整体指纹绑定正文、范围、摘要、压缩标记及 token 估算；复用旧快照时重新核算实际发送内容。8 类改动返回 409，不把损坏摘要当正常结果。旧快照兼容原文/范围验证，不伪造其原先不存在的整体指纹。 |
| NiceGUI 保留的 owner 回调可在会话过期后继续写入 | 服务层重新检查会话有效期，线程、记忆、运行控制、审批和计划修改在操作前返回 401；构造测试验证数据库未被改变。界面显示刷新提示。 |
| 失效来源的事实记忆虽然不进入模型，管理页仍显示有效 | 来源校验失败后记录 `conflict` 新版本及事件，撤回当前运行引用；后续研究不再选择。修正仍须绑定通过校验的事实来源。 |
| 危险研究请求可能被正常取数的计划显示为“完整报告” | 确定性报告校验将明确的预测承诺/交易请求记为未满足目标，报告为部分完成；事实继续绑定历史证据、单位与时点。自由语言识别仍有边界。 |
| 页面快速断线/刷新后，等待浏览器恢复信息的旧回调访问已删除组件 | 在异步恢复后与事件推送时检查客户端是否已删除/断开，及时结束旧回调；2 项生命周期回归和真实浏览器复验。 |

报告保存异常与上下文处理异常也做了构造验收：保存报告后抛错会进入可恢复失败，继续后复用已完成工具、模型账本和版本 1；压缩阶段抛错或记忆在派发前变化时不消耗模型额度，不发布正常报告。

## 实际验证

最终代码全部 123 项检查通过，分组执行：上下文/记忆/故障两文件 59 passed（357.89 秒）；数据/Harness/工作台等其余四文件 64 passed（169.38 秒）。并行浏览器期间的一次 3 秒测试同步超时与修正保留在验证日志，没有将失败隐藏为通过。编译检查和 `pip check` 通过；当前健康阶段为 `batch6_acceptance`，FastAPI 版本为 0.6.0。HTTP/UI 冒烟的页面、健康、API 文档、OpenAPI 与 Socket.IO polling 握手通过；浏览器交互单独验证。拟公开 71 个文件审计通过，验收所启动的子进程已停止。

真实模型与扶摇整条链路通过，未使用构造市场响应：

| 研究 | 工具尝试 | 模型调用 | 来源证据 | 计算 | 结论 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 两只 A 股历史价格与共同年报 | 7 | 1 | 5 | 2 | 22 | completed / complete / validation passed |
| 关联追问，重新获取历史价格 | 4 | 1 | 3 | 1 | 10 | completed / complete / validation passed |

两次终止并以不同 PID 重启：第一次在完成一个工具、等待工具预算审批时；第二次在压缩后等待上下文预算审批时。原会话、具体审批、检查点和账本保留，已完成模型/调用未重复。追问绑定报告版本 2，重新生成来源证据 ID；等待期间删除的偏好撤回，仍有效的历史事实实际使用。CSV 原值/引用逐项一致；导出索引绑定报告指纹；SSE `Last-Event-ID` 的补传 ID 与数据库事件逐项一致。供应商共报告 15,188 tokens，实际费用未知。

本机证明文件 `docs/verification/batch6-live.local.json` 仅记录范围和结果元数据。真实市场响应、Cookie、检查点、服务日志和导出留在忽略的 `.cache/verification/batch6-live-*`；不进入公开文档。

构造故障的真实 Edge 浏览器最终 11 项检查通过：限流三次尝试/无虚构数值，模型坏结构只修复一次且零金融派发，报告异常后继续且不重复，离线/刷新恢复，审批期间删除记忆，390px 标签页，会话过期后阻止界面回调/API，浏览器未捕获错误为 0。日志复验没有再出现已删除客户端的回调警告。

在最终代码上复用本批真实证据，另完成 13 项浏览器补验：历史账本/原文范围、实际记忆使用、原始字段、原生 EventSource、四类下载、真实 PDF、390px 成果页和无横向溢出通过，浏览器错误 0；该补验没有新增模型或金融调用。PDF 产物 3 页，文件头与页面对象有效，130,247 字节。本机记录为 `browser-batch6-real.local.json`。之前第 4–5 批的暂停/停止、点位/字段追溯及版本检查继续保留。

## 重跑方式

在项目根目录使用现有 `.venv`：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe -m compileall -q server scripts tests
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe scripts/smoke_ui.py
& .\.venv\Scripts\python.exe scripts/audit_publication.py
```

真实集成另执行 `scripts/verify_batch6_live.py`。它使用 `.env` 中已配置的模型/扶摇凭据，实际调用收费状态未知的供应商；每次创建独立数据目录，停止的只有自己启动的子进程，不使用用户默认 `data/`。

浏览器故障检查需要可用的 Edge/Chromium 和可选 Playwright QA 模块。Node 仅用于验收，不是产品启动依赖。两个终端分别执行：

```powershell
# 终端 1：每次使用新的隔离目录；端口空闲时启动
$env:XBUDDY_QA_DATA_DIR = 'D:/X Buddy/.cache/verification/batch6-recheck-data'
$env:XBUDDY_QA_PORT = '8097'
& .\.venv\Scripts\python.exe scripts/fault_fixture_server.py

# 终端 2：同一目录、同一端口，Playwright 模块路径按本机安装填写
$env:XBUDDY_QA_DATA_DIR = 'D:/X Buddy/.cache/verification/batch6-recheck-data'
$env:XBUDDY_QA_BASE_URL = 'http://127.0.0.1:8097'
$env:XBUDDY_PLAYWRIGHT_CORE = 'D:/X Buddy/.cache/npm/_npx/31e32ef8478fbf80/node_modules/playwright-core'
node scripts/verify_batch6_browser.cjs
```

浏览器脚本只能指向构造故障服务器：其会在指定 QA 数据库中主动设置当前会话过期，不应指向用户的日常实例。截图、快照、PDF 与 JSON 结果为忽略的本机文件；完成后在终端 1 停止该验收服务。

## 仍未验证的边界

iFinD 真实连接/字段映射与不同供应商数值仲裁未完成；构造 MCP 新闻验证了只读白名单、原文不进入规划上下文以及待复核资料不进入数值结论。表达校验与隔离测试不能证明任意自然语言、任意模型或任意提示注入都安全。

本批没有公网 URL、HTTPS、Docker 卷、多实例生产验证或远程源码仓库。用户/候选人的人工签验未执行；[抽查记录](review-record.md)明确区分 AI 代码/视觉复核与人工签验。上述项目分别由第七批交付环境和实际复核者完成，不把本地验收代称最终交付。
