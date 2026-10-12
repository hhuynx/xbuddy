# 第一批工具接入清单

核对日期：2026-10-11。以下区分公开文档契约与账号实际验证。核心扶摇接口已获授权实测并注册为产品工具；iFinD 未配置，尚未启用真实工具。内部工具名为 XBuddy 设计，不是供应商 MCP 名称。完整第二批记录见 batch2-readiness.md。

## 扶摇主链路契约

请求基址为 `https://fuyao.aicubes.cn`，使用 `X-api-key`；HTTP 与业务 `code` 均需校验。初次未带凭据的日历请求返回 HTTP 200 / code 2003；配置后日历及核心接口 code 0，实际非空响应通过。

| XBuddy 工具 | 公开 REST 契约 | 参数与数据注意点 | 当前状态 |
| --- | --- | --- | --- |
| `resolve_security` | GET `/api/meta/tickers/search` | 必填 `q`；产品限 A 股；保留代码、名称、币种 | verified |
| `get_trading_calendar` | GET `/api/a-share/calendar/trading-days` | 无参数；近一年，`date_ms` / `date`，上海时区；计算另检查日历覆盖时点 | verified |
| `get_price_snapshot` | GET `/api/a-share/prices/snapshot` | 显式 1–3 只标的；只提供上游就绪时间，不等于逐股交易时间 | response_verified; data_unverified |
| `get_daily_bars` | GET `/api/a-share/prices/historical` | 单标的，日线、毫秒级窗口与明确复权；十年范围校验 | forward_daily_verified |
| `get_financial_metrics` | GET `/api/a-share/financials/income-statements` | 也实现 balance/cashflow 路由；`limit` 与窗口互斥；实际计算只用明确年报利润表 | annual_income_verified; other_statements_unverified |

价格和利润表金额按文档为原币单位，利润表收入、利润为元，EPS 为元/股；成交量按股。核心日 K、年报字段已用获授权响应核对并保存映射；其他未映射字段不进入确定性计算。字段为 null、结构异常、时点未知均保留质量缺口。

利润表 `period_end_ms` 是报告期末，`report_date_ms` 是披露时点；响应级 `data.timestamp` 取最大报告期末，不是取数时间或披露时间。`period=quarterly` 表示季度报告期序列，不能直接当作单季收入或利润；累计/单季口径需要另行确认。

关键时间语义：标的检索响应时间是代码表上游加载时间；日历日期使用上海时区；历史和快照取其原始行情字段；服务端取数时点另外记录为 `fetched_at`。时间戳元数据允许进入探测报告，原始金融数值不写入报告。

契约来源：[扶摇文档索引](https://fuyao.aicubes.cn/llms.txt)、[完整公开文档](https://fuyao.aicubes.cn/llms-full.txt)、[标的检索](https://fuyao.aicubes.cn/docs/api-reference/ticker-search/)、[财务报表](https://fuyao.aicubes.cn/docs/api-reference/financials/)。

## 可选扩展

- 资产负债表与现金流表有公开契约，待账号权限及字段口径验证后增加。
- 财务指标接口为 `/api/a-share/financials/indicators`，使用 `thscode` 与 `report`，返回 `abilities`；首版优先多期报表，当前不探测该能力。
- 指数历史和成分数据按实际权限启用，当前不依赖基准指数完成两家公司研究。
- 扶摇 MCP 公开入口包括 `/mcp/a-share` 与 `/mcp/meta`，当前选择显式 REST adapter；未实际执行 MCP 工具发现。
- 部分基本资料能力文档仍为预告；新闻能力有客户端/权限限制，不作为通用账号已可用工具。
- iFinD 当前只有公开入口信息；没有实际 transport、认证方式、tools/list 结果或已验证名称。配置 URL 后仍标为待验证，不虚构工具清单。

## 第一批探测与临时示例

填写 `.env` 后运行 `scripts/verify_batch1.py`，验证标的检索、日历、快照、每只标的的历史 K 线与四期利润表。探测保留状态、耗时、条数、字段名和响应时间元数据。它验证可达性与基础响应形状，详细金融口径验证仍在第二批完成。

暂定两只标的为 `600519.SH` 和 `000858.SZ`：比较最近 60 个交易日的历史市场表现及可比公开财务数据，列出证据与缺口。探测使用 120 个自然日取数窗口，仅用于检查接口，研究主链路必须按交易日历选出 60 个交易日。

两标的核心数据与年报比较已验证，作为第三批示例研究范围。没有新闻原文时仅讨论财务与历史表现，不生成新闻归因。季度口径、指数、新闻、资金等扩展按实际权限另行验证。
