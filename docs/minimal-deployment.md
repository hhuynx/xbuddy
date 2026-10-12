# v0.9.0 最小架构升级

保留 Python 3.11、FastAPI、NiceGUI、LangGraph 和原有审批、发布校验。新增 Docker Compose、独立 Worker、PostgreSQL 检查点、Redis 通知、原文检索及 OpenTelemetry 调用记录。

## 两种启动方式

原 PyCharm `server.main` 配置继续可用。默认读取本地 SQLite 和 `data/checkpoints.sqlite3`；不必启动 Docker，也不必修改已有 `.env`。

在项目根目录用 PowerShell 启动 Docker：

```powershell
& .\.venv\Scripts\python.exe scripts/configure_docker.py
docker compose --env-file .env --env-file .env.docker up --build -d
docker compose --env-file .env --env-file .env.docker ps
```

打开 http://localhost:8080 。若本地服务占用该端口，可在当前 PowerShell 设置 `$env:XBUDDY_PORT="8081"` 后再启动 Docker，访问 8081。

本机本次验收使用 http://localhost:8091 ，四个服务已启动并通过健康检查。若继续使用此端口，执行上述命令前设置 `$env:XBUDDY_PORT="8091"`。

`configure_docker.py` 只在不存在时生成 `.env.docker`，不会修改已有 `.env`，也不会打印密码。模型和扶摇仍读取已有 `.env` 配置。Docker 模式使用独立 PostgreSQL 数据卷，不自动导入原 SQLite 历史；原历史继续可通过原 PyCharm 启动方式访问。

停止服务并保留数据：

```powershell
docker compose --env-file .env --env-file .env.docker stop
```

更新后运行相同的 `up --build -d`。Web、Worker、PostgreSQL 和 Redis 均有健康检查；仅 Web 的本机端口对外映射。数据库和缓存数据保存在命名卷中。

## 实现范围

- Web 提交研究，Worker 独立执行 LangGraph；运行状态、预算、租约、模型/工具账本和检查点存入 PostgreSQL。小型 SQL 方言适配保留现有 SQLite 路径；PostgreSQL 临界写事务通过 advisory lock 串行化，适用于个人工作台，未做大规模多租户吞吐优化。
- 事务 Outbox 与运行事件一起提交，Redis Streams 传递唤醒通知。ACK 表示通知已接收，研究完成状态以 SQL 为准；重复通知不会绕过运行租约或调用幂等键。Redis 故障时保留未发送 Outbox，退回 SQL 轮询。此处不宣称外部请求 exactly-once；进程在远端已处理但本地未确认时崩溃，仍可能需要重新请求。
- 默认最多两个独立工具任务并发，依赖未完成的任务等待；涉及标的选择的引用任务串行执行。整批预算不足先审批，再派发；已经派发的请求可能在暂停或停止后返回，后续请求仍逐次检查控制、预算、租约和时限。
- 左侧「研究资料」可导入 UTF-8 TXT/Markdown、带文字层的 PDF，或直接粘贴正文。需要标题、HTTPS 原文链接和带时区的发布时间；文件最多 2 MB，正文最多两百万字符，每个会话最多一百份。扫描件不做 OCR。PDF 页码为文件页序；粘贴正文的「第 1 页」代表单个文本页。
- `search_documents` 注册为只读 Agent 工具，按中文双字词/英文词检索。返回原文片段、来源、发布时间、页码、字符范围和 SHA-256；模型综合时可用 F 引用及数字占位符。检索无匹配会形成缺口。核验只保证摘录与导入原文一致，原文陈述与来源真实性仍需外部核验。原文中指令作为非可信资料处理。
- PostgreSQL 镜像启用 pgvector 扩展；本次最小 RAG 采用关键词检索，**未实现或宣称语义向量检索**，不需要另配 embedding 模型。
- 「研究过程 → 调用追踪」展示模型/工具耗时、状态和模型 token；`GET /api/runs/{id}/trace` 提供父子 span、trace ID、使用量和工具尝试记录。OpenTelemetry span 与本地 SQL 记录配合，不记录 API Key、提示词或完整模型输入。本次没有额外部署 Jaeger、Grafana 或 OTLP Collector。

## 快速体验

导入一份有来源的年报原文，在对话中输入：「基于导入年报整理行业竞争风险，逐项引用原文出处，区分事实摘录、推断和待核验内容。」在回答中打开依据，可看到对应原文链接与位置。不要将原文摘录等同于已验证的结构化财务指标。

并行工具任务可用已有两股历史价格比较问题检查。在「研究过程」查看任务依赖、并行事件与调用追踪。审批、停止和恢复仍使用原界面。

## 自动验证

```powershell
& .\.venv\Scripts\python.exe -m pytest -q tests/test_minimal_deployment.py
```

默认执行 SQLite 合约测试；PostgreSQL/真实 Redis 用例需要测试连接配置，使用独立 QA schema/Stream，避免改动研究历史。测试连接不是应用运行连接，测试代码只清理自己创建的 QA schema 和 Stream。

技术依据：[Psycopg 事务](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)、[Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/)、[pgvector](https://github.com/pgvector/pgvector)、[OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/)。实际验证结果另见验证日志。
