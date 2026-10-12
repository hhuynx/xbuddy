# XBuddy · 投资研究工作台

XBuddy 是一个基于 Python 开发的个人投资研究 Agent 工作台，将研究目标、数据查询、分析解释和证据核验连接成可持续跟进的研究流程。研究状态、资料与报告长期保存，可在暂停或服务重启后恢复。

[![Tests](https://github.com/hhuynx/xbuddy/actions/workflows/tests.yml/badge.svg)](https://github.com/hhuynx/xbuddy/actions/workflows/tests.yml)

## 功能

| 能力 | 产品体验 |
| --- | --- |
| 研究分析 | 提供研究方法解释、目标澄清、任务规划与完整分析正文，支持基于已有研究继续追问 |
| 金融数据与计算 | 查询历史行情、多年度财务等数据，展示价格图表、财务表和历史表现指标 |
| 证据核验 | 结论关联原始字段或原文，展示来源、时点、单位和统计口径，区分事实、推断与不确定信息 |
| 研究过程 | 查看计划、任务依赖、工具调用、执行状态、耗时及模型 token 使用量 |
| 运行控制 | 支持预算审批、计划调整、暂停、继续和停止；通过检查点恢复执行，复用已保存结果 |
| 上下文与记忆 | 压缩冗余执行记录，保存可管理的研究偏好、假设、历史事实与待解决问题 |
| 资料检索 | 导入 TXT、Markdown 和带文字层的 PDF，通过关键词检索引用原文片段 |
| 研究产物 | 保存报告版本，导出 Markdown、HTML、结论 CSV 和证据索引 |

工作区包含「对话」「研究报告」「研究过程」三个视图，分别承载分析正文、成果与依据、执行详情。研究列表支持创建、切换和删除；阅读历史时保留位置，并提供回到最新消息的入口。

## 技术架构

采用 Python 3.11、NiceGUI、FastAPI 和 LangGraph。本地模式使用 SQLite，方便个人运行；Docker 模式将 Web 与 Worker 分开，并使用 PostgreSQL 保存业务状态和检查点，Redis 传递任务通知。调用记录结合 OpenTelemetry 提供追踪信息。

模型负责理解研究目标、规划任务与解释研究结果；代码负责工具权限、预算、调度、数据计算、引用核验和持久化。工具执行与报告发布均有校验，数据缺失或模型调用失败会明确显示，并在允许的预算内进行恢复。

## 本地启动

需要 Python 3.11。克隆仓库后，在项目目录使用 PowerShell 执行：

```powershell
git clone https://github.com/hhuynx/xbuddy.git
cd xbuddy
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item .env.example .env }
```

在本地编辑 `.env`，填写模型与金融数据服务配置，然后启动：

```powershell
& .\.venv\Scripts\python.exe -m server.main
```

浏览器访问 [http://127.0.0.1:8080](http://127.0.0.1:8080)。PyCharm 使用 `.venv` 解释器，启动模块设为 `server.main`，工作目录设为项目根目录。

Linux/macOS 使用 `python3.11 -m venv .venv` 创建环境，后续将上述 Python 路径替换为 `.venv/bin/python`。健康检查为 `/health`，API 文档为 `/api/docs`。

## 必要配置

配置在 `.env` 中保存，系统环境变量优先。密钥与本地数据不提交仓库。

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` | 兼容 Chat Completions 的模型服务地址，包含服务要求的 API 路径 |
| `LLM_MODEL` | 账号可调用的模型 ID |
| `LLM_API_KEY` | 模型服务密钥 |
| `FUYAO_BASE_URL` | 扶摇服务地址，样例默认值为 `https://fuyao.aicubes.cn` |
| `FUYAO_API_KEY` | 获授权的扶摇数据服务密钥 |
| `XBUDDY_HOST`、`XBUDDY_PORT` | 监听地址与端口，默认 `127.0.0.1:8080` |
| `XBUDDY_DATA_DIR` | 本地数据目录，默认项目下的 `data` |

金融数据主要来自 [扶摇](https://fuyao.aicubes.cn/docs/)。可选的 [iFinD MCP](https://mcp.51ifind.com/) 通过 `IFIND_MCP_URL`、`IFIND_API_KEY`、鉴权配置与只读工具白名单接入；需使用实际 MCP 服务地址。完整配置项见 [.env.example](.env.example)，接入范围见 [工具说明](docs/tool-capabilities.md)。

## Docker 启动

需要 Docker Compose，先按上文准备 `.env`。在项目目录执行：

```powershell
& .\.venv\Scripts\python.exe scripts/configure_docker.py
docker compose --env-file .env --env-file .env.docker up --build -d
```

脚本仅在 `.env.docker` 不存在时生成数据库凭据。默认访问 [http://localhost:8080](http://localhost:8080)，端口可通过 `XBUDDY_PORT` 调整。Docker 模式使用独立数据卷，不自动迁移本地 SQLite 历史。

详细服务配置与数据保留方式见 [部署说明](docs/minimal-deployment.md)。

## 测试

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts/audit_publication.py
```

自动测试使用构造模型与金融响应，无需生产 API Key。GitHub Actions 使用临时 PostgreSQL 和 Redis 执行完整后端测试；[最近一次全量验证](https://github.com/hhuynx/xbuddy/actions/runs/38193206612) 为 **248 项通过**。本地未配置专用测试数据库时，相应集成测试会跳过。

覆盖范围与复现方式见 [测试说明](docs/testing.md)，AI 开发参与与修正记录见 [AI 使用记录](docs/ai-usage.md)，实际验证结果见 [验证日志](docs/verification-log.md)。

## 使用边界

- 产品用于投资研究，不提供直接买卖建议、确定性涨跌预测或收益承诺。
- 缺失、冲突、过期或调用失败的数据会标明缺口；核心结论需通过来源与数值核验。表达审核不能保证覆盖所有自然语言措辞。
- 文档检索目前使用关键词；pgvector 扩展已启用，尚未实现语义向量检索。扫描 PDF 不支持 OCR。
- iFinD 接入尚未完成实际验证；可用数据以服务账号权限为准。
- 当前使用匿名会话隔离，尚未实现生产账号体系；费用在供应商未提供时标为未知。
- 支持本机与 Docker 运行，公开 Web 服务尚未部署。

更多范围说明见 [已知边界](docs/known-limitations.md)。
