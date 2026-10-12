# 第六批验收矩阵

日期：2026-10-11。`tests/` 的模型、HTTP/MCP 响应全部是构造数据；`verify_batch6_live.py` 单独使用已配置的真实模型和扶摇。表中的通过仅指所列范围。

用户实际试用后的核心缺口已在本轮实现并单独验收；原诊断保留在 [基础能力复核](agent-capability-review.md)，当前范围见 [核心修复记录](agent-core-readiness.md)。本表保留各批的历史证据，公网与最终交付仍待验证。

| 要求 | 可复查位置/方法 | 结果与边界 |
| --- | --- | --- |
| 发布表达与信息分类（v0.8.1） | `test_publication`；`verify_publication_browser.cjs`；[改进记录](publication-readiness.md) | 生成/保存/读取/导出共用检查；旧回答被隐藏，新版本可恢复；摘要及导出有明确分类。规则审核不保证任意措辞的全部语义 |
| 对话、报告、研究过程的阅读与操作 | `test_message_pages`；`verify_workbench_layout_browser.cjs`；[布局改进记录](workbench-layout-readiness.md) | 角色区分、最新消息、阅读位置、历史分页、固定输入区、澄清/审批、引用及手机端通过；分页不扩大模型上下文 |
| 一般解释、必要澄清、单股及连续追问 | `test_agent_core`；真实 `verify_agent_core_live.py`；核心浏览器检查 | 方法论零工具，原 Run 补充继续，同线程上下文与刷新恢复；知识回答单独标注 |
| 观察、目标覆盖与综合回答 | `test_agent_core`；真实单股/比较研究 | 工具成功但目标不足仍可有界补取；分项所需字段与比较对象必须被覆盖，旧质量记录保留 |
| 多年度财务覆盖与准确原字段 | `test_financial_history`；真实多期模型/扶摇与浏览器检查；[修复记录](financial-history-readiness.md) | 保留已返回各年度，要求各必需字段共同覆盖所需年度数；支持扩大 limit 补取，原请求保持幂等；旧报告兼容 |
| 模型虚构数字/引用与不可能的目标字段 | `test_agent_core`；真实比较修复后补验 | 阿拉伯/中文实测数字、未知 F 引用和漏项被检出；身份字段须有解析来源，最多一次修正 |
| 澄清权限、恢复、期限与上限 | `test_agent_core` | 答复绑定 Run/问题/会话；新 worker 恢复，重复/过期/跨会话拒绝；最多三个问题 |
| 目标 → 规划 → 工具 → 计算 → 证据 → 报告 | `test_harness` 主链路；真实集成父研究 | 通过；价格/年报两类计算，原字段/公式/单位/时点验证 |
| 关联追问与导出 | `test_knowledge`；真实集成追问、四种导出、报告 V1/V2 | 通过；新证据 ID，旧研究仅作历史上下文，CSV 数字和引用不变 |
| HTTP 200 业务失败、空/null/坏结构 | `test_readiness`、`test_data_foundation` | 通过；业务失败不能变为成功，缺失不填零 |
| 超时、限流、权限不足、非有限 JSON、超大响应 | `test_acceptance` provider fault 参数组 | 通过；408/429/ReadTimeout/业务重试各最多 3 次，401/坏 JSON/超体积一次；无数字事实 |
| 过期数据与冲突 | `test_data_foundation` 日历/重复日期/范围；`test_acceptance` 同标的重复输入 | 通过；不静默选赢家或补齐。真实跨供应商仲裁未做 |
| 交易日/窗口/复权/价格极端值 | `test_data_foundation`、`test_workbench` | 通过；不齐全/口径不一致/非正/溢出阻止计算，图表保留原点与缺口 |
| 财务报告期、披露时间、单位、负利润/除零 | `test_data_foundation` 财务/披露截止；`test_acceptance` 财务参数组 | 通过；无共同期、未知币种/单位、季度混用均拒绝正常指标；非正基数相对差异不计算 |
| 模型坏 JSON/HTTP/截断/体积/错误计划/虚构引用 | `test_harness` 模型 adapter 与非法计划；`test_acceptance` 一次修复 | 通过；最多一次规划修复，不接受任意工具、循环依赖或虚构证据 |
| 提示注入新闻/文档 | `test_acceptance` 构造 MCP 文本含越权/交易/预算覆盖指令 | 通过该样本；原文只进证据存储，规划获得元数据，不进入已验证数值或执行非只读工具 |
| 报告与压缩失败 | `test_acceptance` 保存后抛错、压缩阶段抛错 | 通过；可恢复失败明确可见，恢复复用已完成报告/账本，失败阶段不虚构正常结果 |
| 上下文与原文追溯 | `test_knowledge` 完整历史真实压缩；`test_acceptance` 8 类改动 | 通过；目标、预算、许可、口径、计划、审批、引用及缺口保留，原始事件/范围/摘要/hash 校验 |
| 预算耗尽/实际 token 超支/重规划上限 | `test_harness`、`test_acceptance` usage 超额/替代任务也失败 | 通过；派发前约束、实际超支阻止后续派发，重规划最多一次 |
| 重复审批/修改预算/旧版本或参数失效 | `test_harness`、`test_workbench`、`test_knowledge` 连续审批 | 通过；决定绑定具体版本/参数/最低预算；旧恢复值不能放行新申请 |
| 暂停、继续、停止 | `test_harness` 工具进行中控制；第 4 批浏览器 | 通过；阻止后续派发，已发出的请求可完成 |
| 进程中途/审批中恢复、账本复用 | `test_harness` worker/checkpointer 恢复；真实集成两次不同 PID 重启 | 通过已列范围；无法保证供应商未保存响应的远端请求恰好执行一次 |
| 断线/页面恢复/SSE | `test_workbench`；第 4 批浏览器与第 6 批离线/刷新；真实 SSE Last-Event-ID | 通过；补传真实事件，状态来自数据库；旧页面回调及时结束 |
| 跨研究记忆与有效期/删除/修正 | `test_knowledge`；`test_acceptance` 审批时改变、派发前竞态、失效来源 | 通过；冻结版本重检，变化则撤回，派发竞态不计模型账单，失效事实标 conflict |
| 事实/推断/不确定与危险请求 | `test_harness` 表达规则；`test_knowledge` 假设；`test_acceptance` 两类危险目标 | 通过明确请求样本；只输出绑定来源的历史事实，危险需求留为未完成。没有预测/交易能力 |
| 会话、来源、CSRF、私有资源 | 各测试文件的 foreign owner/CSRF/Origin/过期用例 | 通过；过期的已打开 UI 也拒绝新操作。匿名 Cookie 并非生产账号登录 |
| 日志/返回值脱敏与拟公开文件 | `test_readiness`/adapter/provider/report 故障断言；`audit_publication.py` | 通过列明范围；扫描实际配置密钥但不打印内容，私有市场数据/会话/SQLite 不作为公开候选 |
| 浏览器实际故障交互 | `verify_batch6_browser.cjs` + 隔离构造服务器 | 限流、模型结构、报告恢复、断线、审批中记忆删除、窄屏、过期回调通过；非真实数据测试 |
| 人工抽查 | `review-record.md` | 已保存 AI 抽查依据与用户签验清单；用户人工签验尚未执行 |
| 本机 Docker / PostgreSQL / Redis / 独立 Worker | v0.9.0 [最小架构升级](minimal-deployment.md) | Linux 镜像构建、真实数据库与缓存、子进程审批和重启恢复、并行控制已验证；独立命名卷保留本机数据 |
| 公网/HTTPS/生产平台/源码 URL | 第 7 批剩余范围 | 尚未执行；本机 Docker 不代称生产或公网验收 |

历史验证范围见各批 readiness.md；当前执行结果见 [第六批](batch6-readiness.md) 和本机 `docs/verification/*batch6*.local.json`。不使用构造结果替代真实金融集成，也不使用 HTTP 握手替代浏览器操作。
