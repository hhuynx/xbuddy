# XBuddy · 投资研究工作台

[源码仓库](https://github.com/hhuynx/xbuddy) · [提交说明](docs/submission.md) · [AI 使用记录](docs/ai-usage.md) · [测试与验证](docs/testing.md) · [自动检查](https://github.com/hhuynx/xbuddy/actions)

当前尚未部署评审可从外部网络访问的 Web 地址。按用户选择，本轮完成公开源码与交付材料；本机运行地址不能代替题目必交的 Web 产品 URL。

采用 Python 3.11、NiceGUI、FastAPI 和 LangGraph。当前 v0.9.0 新增 Docker Compose 的 Web、独立 Worker、PostgreSQL/pgvector、Redis 四服务，支持年报/公告原文导入与检索、独立工具任务并行及调用追踪；启动与范围见 [最小架构升级](docs/minimal-deployment.md)。原 PyCharm / SQLite 启动方式继续可用。保留三个工作台视图、多年度财务核验、审批、上下文压缩、记忆、检查点恢复及 [统一发布校验](docs/publication-readiness.md)。

## 当前状态

- 单一主工作区默认显示对话。用户提问与 XBuddy 回答分别呈现，研究回答提供引用和报告入口；图表、财务表、来源与任务过程进入各自视图。新消息跟随及历史阅读行为见 [布局改进记录](docs/workbench-layout-readiness.md)。
- 左侧对话列表只显示标题和删除按钮。确认删除后移除对话，停止未结束研究的后续派发，待处理审批与澄清失效；删除当前对话后切换到剩余对话，全部删除后显示空白工作区。删除采用逻辑删除，后台审计和检查点记录保留，工作台及资源接口不再提供被删除的内容。
- 多年度财务响应中的各期事实进入模型上下文、财务表和导出；趋势目标按同口径完整年度数校验，实际缺期可以扩大查询范围补取。修复范围与验证见 [多年度财务修复](docs/financial-history-readiness.md)。
- 已修复用户反馈的“怎么研究股票”被空工具计划拒绝问题。知识回答不取行情；必要澄清可在原 Run 继续；研究结果经目标覆盖与数值/引用校验后组织为综合回答。实现和验收见 [核心修复记录](docs/agent-core-readiness.md)。
- 单独询问“茅台能不能买”等交易决策时，先说明产品边界并提供财务、历史行情及研究风险方向；选定后继续同一次研究。边界说明不作为交易建议拦截，真实交易建议仍被阻止。模型结构或综合回答失败单独显示为执行问题，数据缺口保留实际取数与目标覆盖不足。原失败记录不被覆盖；PyCharm 更新后需重启 `server.main` 并重新发送问题。
- 保留第六批的 123 项底座检查，新增核心交互、目标检查及回答校验测试；最新结果见核心修复记录。构造响应与真实模型/扶摇、浏览器验证分别记录。
- 真实模型生成计划，扶摇取数与两类计算产出完整报告；跨进程重启后审批恢复，已完成调用没有重复。
- 真实浏览器从目标到报告、编辑后执行、WebSocket 断线补回、暂停/继续/停止、图表数据点及财务表的原始字段、窄屏布局、运行历史和刷新恢复通过。
- 本机模型及核心扶摇接口接入已通过补验。iFinD 未配置；本次已验证 Linux 镜像和 PostgreSQL/Redis 集成，公开 URL、HTTPS 和生产部署仍待完成。

当前结果见 [核心修复记录](docs/agent-core-readiness.md)、[验收矩阵](docs/acceptance-matrix.md) 和 [抽查记录](docs/review-record.md)。[第六批](docs/batch6-readiness.md)、[第五批](docs/batch5-readiness.md)、[第四批](docs/batch4-readiness.md)、[第三批](docs/batch3-readiness.md)、[第二批](docs/batch2-readiness.md) 和 [第一批](docs/batch1-readiness.md) 保留历史；第七批公开部署与最终交付待推进。

## 本地准备与启动

Windows 安装 Python 3.11 后，在项目目录执行：

```powershell
& .\scripts\bootstrap.ps1
```

脚本创建 `.venv`，按 `requirements.lock` 安装依赖，仅在 `.env` 不存在时从样例复制配置，运行本地检查并生成接入报告。缺少凭据会明确记录为待接入，不覆盖已有配置。已有环境可使用 `-SkipInstall`；`-Offline` 禁止外部接入请求。

也可分别执行：

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item .env.example .env }
& .\.venv\Scripts\python.exe -m server.main
```

浏览器打开 `http://127.0.0.1:8080`，点击“新建研究”，填写标题与可选目标；在“对话”底部输入问题，按“发送”或 Ctrl + Enter 提交，Enter 可换行。也可直接输入目标启动，自动创建研究空间。“预算设置”修改下一次运行的预算；预算不足时，对话末尾可以批准、拒绝或修改申请。暂停或等待审批后，可在“研究过程”修改未执行计划，保存后继续。“研究报告”完整展示图表和财务表；点击引用、结论或数据点查看依据。窄屏使用相同三个标签页，顶部菜单打开研究列表。运行历史可切换，刷新/断线后按原 Cookie 恢复。手动查询入口在“研究过程”。健康为 `/health`，API 文档为 `/api/docs`；打开页面不会自动发出模型或金融请求。

示例目标：比较贵州茅台 `600519.SH` 与五粮液 `000858.SZ` 最近 60 个交易日的前复权历史价格表现、波动率和最大回撤，并比较最近共同年报营业收入与归母净利润，列出来源、时点、单位和数据缺口。

“研究目标 / 追问”输入框也接受“怎么研究股票”等一般问题。需要补充信息时，可在对话末尾点选候选或填写答复；提交后继续同一运行，刷新仍会保留待回答问题。同一研究空间的对话进入后续模型上下文，例如“它最近 30 个交易日怎样”。知识回答直接显示，研究回答展示完整分析及“查看研究报告”；报告标明事实、推断和不确定，引用按钮打开对应报告版本的依据。目标已补齐后的旧失败保留在“调用与数据质量记录”，不再机械地变成当前目标缺口。

打开研究空间会定位最新对话。向上阅读时，新消息保留当前阅读位置并提示“有新消息”；“回到最新”恢复跟随。首屏加载最近 20 条，顶部“加载更早的对话”按页取回历史并保持阅读位置；模型上下文仍最多取最近 60 条。切换研究空间保留当前页面内的输入草稿，切换运行历史不会重新填入旧目标。

第五批新增操作：左栏“长期记忆”可保存、搜索、修正和删除偏好/假设/待解决问题；点击事实结论，在依据抽屉中“保存为历史事实”，自动绑定原报告、证据、时点和有效期。“提出偏好候选”根据已验证价格口径生成待确认建议，确认后才进入后续研究。“研究过程”中的“上下文与使用的记忆”显示实际模型引用及压缩记录。“研究报告”可选择版本、重新校验并追加版本、关联追问，导出 Markdown、HTML、结论 CSV、证据索引，或打开“打印 / PDF”使用浏览器保存为 PDF。

PyCharm 继续使用 `.venv` 解释器、模块 `server.main`、工作目录项目根目录。更新代码后先停止旧运行，再启动；默认无热重载。

多期示例：研究 `600519.SH` 最近四个已披露会计年度的合并年报营业收入和归母净利润，逐年列示数据并观察变化方向。v0.7.1 对新运行保留全部核验期间；旧报告保持原生成版本，更新后请新建运行复验。

Linux 可用 `python3.11 -m venv .venv` 与 `.venv/bin/python` 执行同样的 Python 命令；本次通过 Python 3.11 Linux 容器验证依赖和最小部署后端。

## 环境变量

在本机 `.env` 中填写，不在聊天中发送 API Key。操作系统环境变量优先于 `.env`。

| 变量 | 用途 |
| --- | --- |
| `LLM_BASE_URL` | 支持 Chat Completions 的实际 API 地址，按供应商要求包含 `/v1` 等路径 |
| `LLM_MODEL` | 当前账号实际可调用的模型 ID |
| `LLM_API_KEY` | 模型凭据 |
| `LLM_ALLOW_NO_KEY` | 仅在有意使用无鉴权本地服务时设为 `true` |
| `LLM_MAX_OUTPUT_TOKENS` | 默认 `12000`；截断修正可加倍，最高 `24000`，仍受本次运行预算限制 |
| `LLM_TIMEOUT_SECONDS` | 默认 `90`，同时受运行剩余时限限制 |
| `LLM_REASONING_EFFORT` | 默认 `auto`；仅对官方 DeepSeek 的 flash/v4-pro 自动使用 `low`，其他服务保持供应商默认。可按服务能力指定 `low/high/max`；官方 DeepSeek 支持 `none` |
| `FUYAO_BASE_URL`、`FUYAO_API_KEY` | 扶摇地址和本人获授权的 API Key，使用 `X-api-key` 请求头 |
| `IFIND_MCP_URL`、`IFIND_API_KEY` | 可选，实际 MCP 服务地址与凭据，网站登录页不能代替 MCP 地址 |
| `IFIND_AUTH_HEADER`、`IFIND_AUTH_PREFIX` | MCP transport 使用的实际鉴权请求头与前缀 |
| `IFIND_TRANSPORT` | 实际 MCP transport：`streamable_http` 或 `sse` |
| `IFIND_TOOL_ALLOWLIST` | 已发现且复核过的供应商只读工具名，逗号分隔；默认不启用任何名称 |
| `PROBE_TIMEOUT_SECONDS`、`PROBE_SYMBOLS` | 探测超时和 1–3 个 A 股代码，默认贵州茅台、五粮液 |
| `XBUDDY_HOST`、`XBUDDY_PORT` | 本地页面绑定地址与端口，默认 `127.0.0.1:8080` |
| `XBUDDY_DATA_DIR` | 应用数据库目录，默认项目 `data`；保留该目录才能保留状态 |
| `XBUDDY_ALLOWED_HOSTS`、`XBUDDY_COOKIE_SECURE` | 允许的 Host 与生产 HTTPS Cookie 设置，当前按本机运行配置 |
| `DEPLOYMENT_PROVIDER`、`DEPLOYMENT_PUBLIC_URL` | 部署信息记录，填写不会自动部署 |

产品模型通过 Chat Completions JSON 模式调用，默认输出预算 12000 tokens、超时 90 秒，可在 `.env` 配置。模型截断也记录供应商报告的 token 消耗；修正轮适度增加输出上限，仍受审批、成本和总时限控制。DeepSeek 参数依据[官方思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。第一次调用同时理解请求并决定问答、澄清或研究；研究在取数和计算后再次调用模型核对目标，解释观察、研究含义、依据边界与后续验证。一般知识通常 1 次模型调用、0 次金融调用；完整研究通常至少 2 次，澄清、补取及修正分别记账。每个模型阶段最多一次结构修正，不会执行未经验证的计划。提示中的目标和外部内容不能授权任意工具。

对话展示全部分析小节、正文与引用，不再只显示前三段或隐藏事实段。新运行的综合阶段若仍发生截断等可恢复错误，有已核验事实且预算允许时最多补一次文字分析；该阶段不能规划新任务，结果仍为部分完成，不宣称目标已覆盖。分析未通过校验时，对话展示明确失败原因与少量已核验事实。事实、推断与不确定信息的分类、数值占位替换、来源指纹与发布校验继续保留。重启本地 `server.main` 后新建研究即可使用；旧失败记录不会自动补写为成功。

## 验证命令

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts\smoke_backend.py
& .\.venv\Scripts\python.exe scripts\smoke_ui.py
& .\.venv\Scripts\python.exe scripts\verify_batch1.py
& .\.venv\Scripts\python.exe scripts\verify_batch2_live.py
& .\.venv\Scripts\python.exe scripts\verify_batch3_live.py
& .\.venv\Scripts\python.exe scripts\verify_agent_core_live.py
& .\.venv\Scripts\python.exe scripts\verify_agent_core_comparison_live.py
```

接入脚本的退出码：`0` 表示模型与所有必需扶摇探测均通过，`2` 表示存在未配置或未通过的真实接入。报告位于 `docs/verification/*.local.json`，仅包含版本、状态、字段名、条数和时间元数据，不保存原始金融数值或密钥；这些本机报告不提交仓库。

配置齐全后，脚本发起一次模型请求及最多 9 次只读扶摇请求；默认两只标的是 7 次金融请求，可能消耗供应商配额。未配置扶摇凭据时仅发起一次不带凭据的日历请求，验证实际鉴权边界。`--offline` 可跳过外部请求。

`smoke_ui.py` 验证 HTTP、API 文档与 Socket.IO polling 握手。第二批另用真实浏览器验证交互与 WebSocket。`smoke_backend.py` 用构造状态检查 LangGraph 库兼容性；`verify_batch2_live.py` 则启动真实服务器，完成七次金融查询和两类计算，终止并重启进程，验证证据、结果与幂等复用。后者需要配置好的扶摇 Key，会保存获授权数据到被忽略的本机缓存。

## 第二批 API 与计算

主要接口：会话 `/api/session`、工具 `/api/tools`、研究 `/api/threads`、运行 `/api/threads/{id}/runs`、查询 `/api/runs/{id}/tools/{name}`、证据 `/api/evidence/{id}` 及 `/raw`、计算产物 `/api/metrics/{id}`、事件游标 `/api/runs/{id}/events?after=...`。第四批新增 `/api/runs/{id}/snapshot`、`PUT /api/runs/{id}/plan` 及 SSE `/api/runs/{id}/events/stream`（after / Last-Event-ID）。所有资源按匿名会话隔离；写操作带会话返回的 `X-CSRF-Token`。保留 Cookie 和数据库即可访问原记录。

注册的计算工具：`calculate_price_comparison` 接收日 K 证据、日历证据及观察值数量；`calculate_financial_comparison` 接收同口径利润表证据。输入必须属于当前会话，内容指纹与单位须验证。计算结果保存公式、代码版本和精确输入字段引用。工具参数 schema 在 `/api/tools` 可查看。

60 个交易日收盘观察值对应 59 个日收益；计算的是明确复权方式下的历史价格变化，未声称为总回报。日历缺失/过期、缺失交易日、不同复权、非法价格与未知季度口径会阻止正常计算；负利润或零基数不计算常规相对百分比差异。原始 null 保留。

同一 Run 中相同幂等键复用已保存调用，参数变化返回 409；正在执行的调用不重复派发。手动查询需要重试失败请求时可创建新 Run 或使用新幂等键。每次尝试独立记账，重试最多两次。手动运行默认 30 次；自动研究使用自己的预算，不能通过手动工具接口绕过计划。

## 第三批 Agent API 与运行规则

POST `/api/threads/{id}/runs` 接受以下 JSON（写操作仍需 Cookie 和 CSRF）：

```json
{"goal":"比较两个明确代码的历史表现与共同年报","mode":"agent","budget":{"tool_attempts":12,"model_calls":4,"model_tokens":100000,"duration_seconds":600}}
```

GET `/api/runs/{id}` 返回任务、计划、消耗、审批和产物；POST `/api/runs/{id}/control/{pause|resume|stop}` 控制运行。GET `/api/runs/{id}/approvals` 获取审批，POST `/api/approvals/{id}/decision` 提交 `decision`（`approve`、`reject`、`modify`）、原审批的 `plan_version` 和 `parameters_fingerprint`；修改时额外提交完整 `budget`，只能在本次申请范围内修改，不能低于已使用量或改变时限。GET `/api/runs/{id}/report` 获取初版报告，事件接口支持持久化游标。

后台通过 LangGraph 的 plan（问答/澄清/研究）→ gate ↔ execute → review（目标检查/补取/澄清/综合回答）→ report 运行。检查点存在 `data/checkpoints.sqlite3`，业务账本存在 `data/xbuddy.sqlite3`，两者都需保留；业务 schema 4 采用追加迁移保存对话、待输入和 Agent 决策。计划总计最多 12 个任务，目标不足时最多一次追加补充计划，不能修改已记录工作。每个外部请求/重试检查控制状态、只读许可、租约、预算及期限。停止后已发出的请求可能返回，其结果保留，但不派发新请求。

默认运行时限 600 秒，等待审批、暂停、重启期间仍计时；费用未知，token 无供应商 usage 时使用明确标注的估算。租约 15 秒、心跳 2 秒；异常退出后的活跃任务等待租约到期后恢复。已持久化结果复用；崩溃前派发但没有保存响应的请求无法保证远端恰好执行一次，账本标记不确定状态，后续重试继续计入预算。

报告核心数字由代码从已核验证据生成，发布前重验原始字段映射、输入引用、单位、时间与计算公式。模型基于 F 编号引用组织分析，所有实测数字通过事实占位符由代码插入；错误引用或自编数字触发有界修正，仍失败则阻止该综合回答发布。部分有效成果但目标缺失为 partial；没有可用成果为 failed；完整性失败为 blocked。一般知识回答标明未进行当前数据核验。

新增 GET `/api/threads/{id}/messages` 返回持久化会话，GET `/api/runs/{id}` 返回 `pending_input` 和 Agent 决策。等待补充时 POST `/api/runs/{id}/input`，正文为 `{"question_id":"当前问题 ID","answer":"答复"}`。答复绑定具体问题、Run、会话和期限；过期、重复或跨会话答复不能放行。每次 Run 最多提出 3 个必要问题，等待输入也计入原运行时限。

`verify_batch3_live.py` 会用真实模型启动自定义比较研究，先在一次工具尝试后等待审批，再终止并重启所启动的服务器，批准后完成取数、计算和报告校验。需要本机模型及扶摇配置，可能消耗配额；数据与检查点只写入忽略的本机目录，输出仅元数据。

第六批完整真实验收执行 `scripts/verify_batch6_live.py`：在独立目录完成父研究、两次审批期间进程重启、版本、记忆、追问、导出和 SSE；拟公开文件扫描执行 `scripts/audit_publication.py`。可重复的浏览器故障验收与范围说明见 [第六批记录](docs/batch6-readiness.md)。

## 产品与 AI 分工

选择 Python 是为了使用同一技术栈连接界面、API 和研究执行；NiceGUI 避免独立前端构建，FastAPI 提供明确接口，LangGraph 承载持久执行与人工中断。SQLite 保留低成本本机启动，Docker 模式分开 Web 与 Worker，并使用 PostgreSQL/Redis 管理持久状态与通知。以可追溯的研究流程为主，关键词原文检索先满足引用需求，不用尚未实现的向量能力包装成果。

当前产品采用研究线程、持久化对话、结构化计划、任务执行、审批、证据和版本化报告。模型负责请求理解、知识解释、规划、结果观察及有依据的综合回答；代码负责权限、预算、调度、数据质量、数值计算、引用与完成条件校验、持久化及上下文压缩。压缩原样保留关键状态，替换冗余事件历史，不额外调用模型。浏览器断线不承担任务生命周期。

数据优先使用 [扶摇 API/MCP](https://fuyao.aicubes.cn/docs/)，可选扩展 [iFinD MCP](https://mcp.51ifind.com/?syncCookieTimes=1)，以账号实际可调用能力为准。契约与尚未验证的能力见 [工具接入清单](docs/tool-capabilities.md)。

研究成果区分事实、推断和不确定信息；核心结论关联原始字段、来源、时点、单位及统计口径。缺失、失败、过期或冲突不能静默生成正常结论。生成、保存、读取和导出共同执行表达审核，阻止确定性涨跌预测、收益承诺或直接交易建议；当前采用归一化后的规则检查及结构化校验，不能保证覆盖任意措辞的全部语义。用户画像和持仓采用构造或匿名数据。

## 部署与后续工作

本次完成本机 Docker Compose 四服务、Linux 镜像与 PostgreSQL 检查点，详见 [最小架构升级](docs/minimal-deployment.md)。源码与交付材料位于 [hhuynx/xbuddy](https://github.com/hhuynx/xbuddy)，公开 HTTPS 产品部署仍待服务器或平台条件。SQLite 保留本地兼容模式；Docker 使用独立 PostgreSQL 卷，不自动迁移旧历史。完整功能见 [实现方案](docs/implementation-plan.md) 和 [七批实施清单](docs/delivery-batches.md)。

上下文压缩、长期记忆、关联追问和报告版本/正式导出已完成本地验收。iFinD 字段映射和实际授权调用未验证；当前匿名会话不是生产账号体系。尚无外部可访问的 Web 产品 URL，因此还未达到原题全部最终交付标准。GitHub 源码地址用于代码与材料评审，不能替代可运行产品地址。

AI 使用与实际修正见 [AI 记录](docs/ai-usage.md)，测试边界见 [测试说明](docs/testing.md)，检查结果见 [验证日志](docs/verification-log.md)。`.env`、本机缓存、用户资料及受限响应不得提交仓库。
