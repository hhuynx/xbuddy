# 回答发布校验与信息分类 · v0.8.1

日期：2026-10-11。对应题目中的两项要求：产品不得输出确定性涨跌预测、收益承诺或直接交易建议；事实、推断和不确定信息须区分，核心结论须回到来源、时点、单位及统计口径。

## 改动

此前表达审核只检查有限关键词，三个本地验证样本可以通过普通问答校验：“这只股票明天上涨毫无悬念”“现在应当购入这只股票”“这笔投资能够确保回报”。这些是测试构造的输出，不代表模型已经实际生成这三句话。

- `server/publication.py` 统一执行 Unicode 归一化、隐藏字符/格式处理、中文空白处理、中英文表达规则、否定范围及结构化分类检查。原 `safe_text` 导入接口保留，各生成路径共用该实现。跨字段表达也检查组合文本。
- 问答、澄清和研究综合回答验证时拒绝不支持的表达，沿用原来最多一次修正、模型预算和调用账本；修正仍失败，则保存明确缺口，拒绝错误综合回答，不重发已经完成的金融请求。
- 报告构建及保存执行最终检查；失败报告清除全部核心结论和叙述，标记 `blocked / failed`。原始数据、来源记录和用户问题保留为输入，不能作为经过核验的模型结论。
- 报告详情、当前报告、四种导出及打印统一经 `ArtifactService.verify`。指纹一致的旧报告也要满足当前表达规则。运行及版本列表、Agent 决策视图隐藏不合规的历史回答；对话读取先校验原始指纹，再返回带阻止原因的安全视图，不改写原审计记录。
- 普通问答标注“一般知识”，并说明未核验本次金融数据；研究摘要逐段标注“推断 / 不确定信息”；完整报告与 Markdown、HTML、打印逐段区分“事实 / 推断 / 不确定信息”。原 F 引用、事实值、来源、时点、单位、字段路径及统计口径保持原有核验。
- 历史报告无法展示时可以重新核验并保存新版本；如果旧 Agent 状态本身含不合规回答，新版本会被阻止，需要重新发起研究，而不会直接复制旧回答。重新核验已保存数据不调用模型。

## 验证

本轮覆盖当前 201 个不同测试用例，按各用例最后一次实际执行汇总，均通过；不是一次全量命令得到的 201 passed。首次全量为 195 passed / 2 failed（602.38 秒），两项失败是 v0.8.0 改为通过所属 Client 运行 JavaScript 后，旧断线测试仍模拟全局入口。改到真实调用点，保留断线后不能操作组件的断言。

增加历史 Agent 决策过滤后，复验出现 32 passed / 12 failed，定位到合法 `review=None` 未处理，已修正为空状态。最后核心 Agent、发布校验及断线回归合计 64 passed（166.65 秒）；补充否定表达后，所有表达矩阵 31 passed（4.23 秒）。二者与首次全量的通过结果按用例 ID 合并，当前全部 201 个用例都有通过记录，没有未解决的失败。

| 浏览器范围 | 结果 |
| --- | --- |
| 构造数据的发布与分类检查 | 23 项通过：有限修正、知识标签、有效旧指纹不绕过审核、详情/打印/四类导出拒绝、列表与页面提示、新版本恢复、预算保持、逐段分类、引用和手机端 |
| 原布局交互复验 | 31 项通过：阅读位置、分页锚点、固定输入、澄清、审批继续、视图切换、手机和下载 |
| 已保存真实四年度财务成果 | 19 项通过：多年度完整性、金额与年度、最早年度原字段、引用、四类下载及手机端；前后账本一致，无新增模型或金融请求 |

发布浏览器第一次失败是构造模型读取了 `repair_error`，但规划修正协议实际使用 `repair_errors`；改正构造响应后 23 项通过，没有放宽模型次数、内容拦截或恢复断言。截图由 AI 查看桌面及 390px 对话分类显示，不代称用户人工签验。

编译、依赖一致性、HTTP/UI/Socket.IO 冒烟和拟公开文件扫描通过。最终元数据在忽略的 `docs/verification/publication-finish.local.json`，单项浏览器结果在 `publication-browser.local.json`、`workbench-layout-browser.local.json` 和 `financial-history-browser.local.json`；JUnit 在 `.cache/verification/publication/`。构造数据库位于 `.cache/verification/`，浏览器输出位于 `output/playwright/`，不提交本机数据库、真实响应、会话或截图。

本轮隔离服务 8107/8108 已关闭。没有修改用户 `.env`、PyCharm 配置或默认数据目录，也没有操作用户的 8080 服务。

可复验命令：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_publication.py -q
& .\.venv\Scripts\python.exe -m pytest -q
```

浏览器沿用仓库现有 Playwright Core 与 Edge 流程。该流程使用 [Playwright skill](D:/CodexData-OpenAI/skills/playwright/SKILL.md)；CLI 后台服务在此前验收无法启动，因此使用既有单进程浏览器脚本，不增加生产依赖。在独立终端启动隔离构造服务器：

```powershell
$env:XBUDDY_QA_DATA_DIR = Join-Path (Get-Location).Path '.cache\verification\publication-local'
$env:XBUDDY_QA_PORT = '8107'
& .\.venv\Scripts\python.exe scripts/agent_core_fixture_server.py
```

另一个终端运行 `node scripts/verify_publication_browser.cjs`。Playwright Core 不在 Node 搜索路径时用 `XBUDDY_PLAYWRIGHT_CORE` 指定已安装目录；用 `XBUDDY_QA_BASE_URL` 或 `XBUDDY_BROWSER_PATH` 指定不同的测试地址或 Edge 路径。测试服务器有明确的私有目录限制，构造历史内容的接口仅用于这个隔离服务器，生产入口不注册这些接口。

## 边界

表达审核属于归一化后的规则检查及结构化校验，覆盖本轮回归样本，不是对任意自然语言语义的百分之百保证，也没有新增每次回答的模型审核调用。一般知识仍属于未核验解释；推断的引用可追溯，不等于系统证明了因果关系或全文逻辑。当前实证核验覆盖已接入的价格、交易日历、财务数据及计算，尚未验证新闻原文和其他新数据域的映射。公网部署、生产账号体系和 iFinD 实际授权接入仍属后续工作。

本地查看新代码需要停止旧 Python 进程并重新启动 PyCharm 的 `server.main` 运行配置，再刷新页面。用户默认数据目录和 `.env` 不因本次验证改变。
