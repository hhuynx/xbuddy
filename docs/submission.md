# 投资 XBuddy · 提交说明

源码仓库：[hhuynx/xbuddy](https://github.com/hhuynx/xbuddy)。当前提交包含可运行源码、启动与配置说明、AI 使用记录、验证记录和测试用例。

**Web 产品外部地址尚未部署。** 当前仅完成本机 Docker 与 Python 服务验收；localhost 地址不是评审从外部网络可访问的产品地址。用户已选择先完成 GitHub 与交付材料，部署暂缓。本提交尚未满足全部最终交付要求。

## 必交材料

| 交付项 | 入口 | 当前状态 |
| --- | --- | --- |
| 可实际操作的 Web URL | 部署后在本文件补入实际 HTTPS 地址与访问说明 | 待部署，不能以代码页面或截图替代 |
| 源码与 README | [源码仓库](https://github.com/hhuynx/xbuddy)、[README](../README.md) | 本仓库；涵盖启动、环境变量、产品与 AI 分工、数据来源和边界 |
| AI 使用与验证记录 | [AI 使用记录](ai-usage.md)、[验证日志](verification-log.md) | 用户反馈、AI 参与、实际错误与复验分开记录 |
| 测试说明 | [测试说明](testing.md)、[验收矩阵](acceptance-matrix.md)、[测试源码](../tests) | 含主链路、缺数/失败、金融极端值与投资表达边界 |
| 可选演示视频 | 建议录制 60–180 秒实际产品操作 | 尚未录制，不能代替 Web URL |

## 本机体验

启动方式与配置以 [README](../README.md) 和 [Docker 部署说明](minimal-deployment.md) 为准。默认未配置模型或金融凭据时明确提示，不能把无凭据运行当作完整研究。

```bash
git clone https://github.com/hhuynx/xbuddy.git
cd xbuddy
python -m venv .venv
```

Windows 使用 `.venv\Scripts\python.exe`，Linux 使用 `.venv/bin/python`。安装 `requirements.lock`，将 `.env.example` 复制为私有的 `.env`，在本地填写本人获授权的模型与扶摇配置，然后运行 `python -m server.main`。不在仓库或聊天中提交密钥。

建议依次验证：

1. 输入“怎么研究股票”，观察一般知识解释，确认没有金融调用。
2. 输入“核对贵州茅台最近四个已披露年度的合并年报营业收入和归母净利润，说明变化与研究边界”，检查规划、任务、完整分析、年度事实与依据。
3. 点击一处引用，核对来源、时点、单位、口径和原字段；打开报告并导出。
4. 追问“现有资料还不能解释什么”，观察前文与来源边界。
5. 使用隔离构造测试检查失败、预算确认、暂停/停止及恢复；不通过故意破坏真实凭据演示。

会话是研究空间，Run 是一次研究；计划对应任务，取数保存原始响应与证据，代码计算保存指标，模型解释对应报告段落与事实引用。报告按版本保存，执行状态、审批、检查点和调用记录可复查。

## 测试与核验入口

```bash
python -m pytest tests -q
python scripts/audit_publication.py
python scripts/audit_git_index.py
```

自动测试使用构造模型与金融响应，不需要生产 API Key。PostgreSQL/Redis/独立 Worker 用例需按部署说明配置专用测试数据库，使用独立 QA schema/Stream，避免共用生产数据。GitHub Actions 使用一次性 PostgreSQL/Redis，全量后端测试 **248 passed（247.14 秒），无跳过项**；[实际远程记录](https://github.com/hhuynx/xbuddy/actions/runs/38193206612)。

最近一次已配置真实模型验证使用全新构造财务样本：两次模型调用、三个小节、九段正文，引用与数值校验通过；没有读取历史研究或调用真实金融接口。此前真实扶摇验证与本次构造验证分别记录在验证日志，不能混为一次验收。

## 人工反馈与未做事项

候选人/用户明确选择 Python、最小 Docker 范围，并在试用时指出普通问答、财务多期覆盖、对话布局、删除、交易问题错误分类及分析正文等问题。相关实现修正与自动检查由 Codex 完成，完整金融语义人工签验未被代称完成，见 [抽查记录](review-record.md)。

外部 Web 部署、HTTPS 与外部网络长连接/持久化验收待完成；iFinD 未配置；pgvector 扩展已启用，但文档检索采用关键词，未做语义向量检索；匿名 Cookie 不是生产账号体系。费用未知，不提供直接买卖建议、确定性涨跌预测或收益承诺。完整限制见 [当前边界](known-limitations.md)。
