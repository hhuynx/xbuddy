# 第一批实施与验收结果

后续补验（2026-10-11）：用户完成本机配置后，模型结构化两步规划和七次扶摇基础请求已通过；记录见 `docs/verification/batch1.local.json`。下文保留初次没有凭据时的历史结果。iFinD 与实际部署仍未验证。

日期：2026-10-11；环境：Windows / Python 3.11.5 / SQLite 3.42.0。按用户要求改为 Python 技术栈，不使用独立 React 项目或 Node 构建流程。

第一批本地工程准备已完成。原定第一批完整完成标准仍未达到：模型、金融源的获授权真实调用及实际部署条件尚待验证。用户已说明没有凭据，项目提供空白 `.env` 与 `.env.example`，未使用构造数据代替真实接入。

## 已交付

| 交付 | 结果 |
| --- | --- |
| Python 技术栈 | NiceGUI + FastAPI + LangGraph + SQLite；方案和七批清单已更新 |
| 环境和依赖 | `.venv`、`requirements.in`、91 项固定版本的 `requirements.lock`；`pip check` 通过 |
| 启动入口 | `server/main.py` 可运行环境页面和 `/health` |
| 本地检查 | 页面 HTTP、Socket.IO polling 握手、API 输入校验、WAL 与持久检查点恢复通过 |
| 接入脚本 | 配置检查、两步模型规划验证、扶摇基础探测、脱敏报告、离线选项 |
| 契约与测试 | 公开工具契约清单；6 项接入边界测试通过 |
| 部署准备 | Python 单实例容器路线、`Dockerfile`、`.dockerignore`；镜像尚未构建 |
| 使用与记录 | README、AI 使用/修正、验证日志与测试说明 |

## 实际验证

| 检查 | 实际结果 | 限制 |
| --- | --- | --- |
| 依赖安装与 `pip check` | 通过 | Windows 验证；Linux 镜像待验证 |
| NiceGUI 页面与 FastAPI 健康接口 | HTTP 200 | 仅第一批环境页，非完整工作台 |
| Socket.IO transport | polling 握手通过，声明支持 WebSocket 升级 | 未进行浏览器交互或实际 WebSocket 升级测试 |
| FastAPI/Pydantic 输入验证 | 有效输入 200、空目标 422 | 构造请求验证依赖兼容性 |
| SQLite WAL | 通过 | 临时本机数据库 |
| LangGraph 中断恢复 | 关闭 saver 后重新打开数据库和图，审批恢复通过；已完成节点调用次数仍为 1 | 同一进程新运行对象；完整产品跨进程重启验收待第三/六批 |
| 接入边界 pytest | 6 passed | 包含业务失败、空/坏数据、脱敏、工具白名单、依赖、URL 约束 |
| 模型真实请求 | blocked | 缺少 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`，未发送请求 |
| 扶摇真实鉴权边界 | HTTP 200 / code 2003，识别为 unauthorized | 未带凭据；未读取到获授权市场数据 |
| iFinD | not_configured | 未执行工具发现与调用 |
| Docker | CLI 29.8.0 可用，daemon 不可用 | 未构建或启动镜像 |
| 部署平台 | not_verified | 平台、公开地址、长连接与卷未验证 |

主要版本：NiceGUI 3.18.0、FastAPI 0.143.0、Pydantic 2.14.0、LangGraph 1.2.14、SQLite checkpointer 3.1.1、MCP SDK 2.3.0、pytest 9.1.1。完整依赖见锁文件。

## 接入验收剩余步骤

1. 本机填写 `.env` 中模型地址、模型 ID、模型 Key 和扶摇 Key。
2. 运行 `scripts/verify_batch1.py`，确认模型两步计划通过，核心扶摇响应有非空记录和可用字段。
3. 根据真实返回核对时间、单位、复权、报告期与权限；再锁定示例研究维度。详细数据标准化在第二批实现。
4. 如需 iFinD，配置实际 MCP endpoint 并验证 transport、工具发现及只读查询。
5. Docker daemon 可用后构建镜像；在选定 Python 容器平台检查 HTTPS、UI 长连接和持久化卷。

部署选择为支持 Python 服务的单实例容器平台，要求连接转发与持久化磁盘；具体供应商待确定。Sites/Cloudflare Workers 托管运行模型不作为该 Python 服务的既定部署环境。

本机报告在 `docs/verification/*.local.json`，不提交密钥和受限响应。没有公开部署、远程仓库或完整产品演示。本批记录不代表第二至第七批能力已完成。
