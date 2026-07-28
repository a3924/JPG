# 数据采集编排与质量闸门

## 设计边界

吸收UZI-Skill中“字段级来源清单、维度注册、fallback痕迹、分层新鲜度、覆盖率和机械自审”的优点，但不复制其网络抓取器、缓存、人物评委和硬编码交易规则。行情、财报、公告和市场数据继续调用现有 `wb-finance-skill`、Neodata、腾讯自选股、通达信及官方披露；本Skill只负责编排、记录、校验和恢复。

## 研究档位

先选择档位，再决定需要采集哪些维度：

### Lite：快速事实检查

适用于研报初筛、单一事实核验和已有报告快速审计。强制维度：

- `identity`
- `filings`
- `business`
- `financials`
- `valuation`
- `risks`

不得因缺少技术面、舆情或龙虎榜阻止Lite报告。

### Standard：标准深度研究

适用于一般A股深度研报。除Lite维度外，增加：

- `cashflow_owner_earnings`
- `industry_chain`
- `peers`
- `moat`
- `management_governance`
- `capital_allocation`
- `research_consensus`
- `catalysts_events`

### Deep：投委会级研究

适用于高金额决策、争议标的、周期拐点或完整审计。除Standard维度外，增加：

- `market_signals`
- `capital_flow_ownership`
- `materials_macro_policy`
- `counterevidence_stress`

Deep不等于堆数据；与公司无关的期货、宏观、龙虎榜或舆情标为 `not_applicable`，不强行生成。

## 标的分类与适用性

先标准化：

- `ticker`：交易所前缀或统一A股代码；
- `market`：`SSE`、`SZSE`、`BSE`；
- `security_type`：`listed_stock`、`ipo_prelisting`、`etf`、`convertible_bond`、`other`；
- `listing_status`：已上市、受理、问询、过会、注册、询价、发行、待上市。

本Skill主流程只处理 `listed_stock` 和 `ipo_prelisting`。ETF、可转债和基金应切换到相应资产Skill，不得硬套企业护城河和普通股DCF。

## 数据维度注册表

| 维度 | 核心字段 | Lite | Standard | Deep | IPO |
|---|---|---:|---:|---:|---:|
| `identity` | 公司、代码、交易所、状态、实控人 | 必需 | 必需 | 必需 | 必需 |
| `filings` | 最新年报/季报/公告/招股书版本 | 必需 | 必需 | 必需 | 必需 |
| `business` | 分部收入、利润来源、商业模式 | 必需 | 必需 | 必需 | 必需 |
| `financials` | 5-10年财务、最新季度、口径 | 必需 | 必需 | 必需 | 必需 |
| `cashflow_owner_earnings` | OCF、Capex、FCF、所有者收益 | 可选 | 必需 | 必需 | 必需 |
| `industry_chain` | 上下游、定价权、市场空间 | 可选 | 必需 | 必需 | 必需 |
| `peers` | 可比依据、统一口径指标 | 可选 | 必需 | 必需 | 必需 |
| `moat` | 护城河证据与趋势 | 可选 | 必需 | 必需 | 必需 |
| `management_governance` | 管理层、激励、关联、质押 | 可选 | 必需 | 必需 | 必需 |
| `capital_allocation` | 分红、回购、融资、并购、稀释 | 可选 | 必需 | 必需 | 必需 |
| `valuation` | PE/PB/PS/分位/DCF/隐含预期 | 必需 | 必需 | 必需 | 必需 |
| `research_consensus` | 券商覆盖、盈利预测、目标价原文 | 可选 | 必需 | 必需 | 可选 |
| `catalysts_events` | 财报、订单、解禁、监管、项目节点 | 可选 | 必需 | 必需 | 必需 |
| `market_signals` | K线、成交、技术、交易状态 | 可选 | 可选 | 条件必需 | 不适用 |
| `capital_flow_ownership` | 资金、两融、机构、股东、解禁 | 可选 | 可选 | 条件必需 | 仅发行结构 |
| `materials_macro_policy` | 原料、政策、宏观传导 | 可选 | 条件必需 | 条件必需 | 条件必需 |
| `risks` | 永久损失、盈利下修、估值风险 | 必需 | 必需 | 必需 | 必需 |
| `counterevidence_stress` | 最强反方、压力情景、证伪点 | 可选 | 必需 | 必需 | 必需 |
| `ipo_issuance` | 发行、稀释、募投、锁定、问询 | 不适用 | 不适用 | 不适用 | 必需 |

“条件必需”由业务暴露决定。例如金属加工企业的原材料价格是必需维度，软件公司的期货价格不是。

## 字段级数据契约

把研究底稿保存为符合 `schemas/research_bundle.schema.json` 的JSON。每个数据点至少包含：

- `metric`：规范字段名；
- `value` 与 `unit`；
- `period` 或 `as_of`；
- `collected_at`；
- `source_name`、`source_url`、`source_tier`；
- `estimate_type`：`official`、`institution_forecast`、`model_estimate`；
- `freshness_class`；
- `status`；
- `fallback_trace`；
- `conflict` 与口径说明。

关键结论使用的数据点增加 `materiality: key`。Key数据不得只依赖D级来源。

## 缺失值语义

禁止用0、“—”或空字符串混装缺失。使用：

- `available`：值有效；
- `not_applicable`：对该证券或行业不适用；
- `not_disclosed`：公司或监管文件未披露；
- `source_unavailable`：数据源暂时失败；
- `stale`：有值但时点过旧；
- `conflict`：多源冲突尚未解决；
- `insufficient_evidence`：只有低等级线索，不能进入结论。

亏损、零分红、零负债等真实的0必须保留为有效值。

## Fallback痕迹

每次尝试记录：

```json
{
  "source": "数据源或官方文件",
  "status": "success|empty|timeout|error|not_covered",
  "attempted_at": "ISO-8601时间",
  "reason": "失败或采用原因"
}
```

最终采用备用源时设置 `used_fallback: true`。不得只保留最终成功值而抹去失败链；这有助于判断报告是否过度依赖搜索兜底。

## 新鲜度分层

下列仅是采集告警默认值，不是投资阈值，可在 `review_config.freshness_hours` 中覆盖：

- `realtime`：行情快照，默认1小时；
- `intraday`：分时、资金和热度，默认8小时；
- `daily`：K线、两融、龙虎榜、股东事件，默认72小时；
- `news`：新闻和公告搜索，默认168小时；
- `quarterly`：财报、机构持仓和盈利预测，按“理论应披露最新期”检查，不单纯按缓存年龄；
- `static`：行业分类、公司名称等，默认730天，但公司更名或重组后立即刷新。

不在交易时段时，行情新鲜度锚定最近交易日收盘，而不是自然时间。

## 覆盖率与质量

覆盖率只衡量研究完整度，不作为股票评分：

- 分母只计算当前档位和证券类型下“适用且必需”的维度；
- `complete=1`、`partial=0.5`、`missing=0`；
- `not_applicable`不进入分母；
- 核心维度 `identity`、`filings`、`business`、`financials`、`valuation`、`risks` 任一缺失时，禁止输出确定评级；
- 仅有D级来源或关键冲突未解决的维度最多记为 `partial`。

## 恢复任务队列

数据缺口不得只写在免责声明。为每个缺口生成：

- `priority`：critical/high/medium/low；
- `dimension` 与 `metric`；
- `reason`；
- `preferred_source`；
- `fallback_sources`；
- `suggested_query_or_action`；
- `blocking`：是否阻止最终评级。

优先恢复核心财务和一手公告，再补舆情与技术面。

## 催化剂日历

每个事件记录：日期/窗口、事件类型、来源、状态（已发生/已公告/推测）、影响路径、验证指标和失效条件。没有公告支持的事件不得写成确定日期；机构预测和作者推测必须分栏。

## 机械自审

交付前运行：

```bash
python scripts/self_review.py <research_bundle.json>
```

出现critical问题时禁止输出确定评级；先执行恢复任务或将结论降级为“证据不足”。自审只检查结构、来源、时点、覆盖和计算状态，不替代商业判断。

## 明确不吸收的UZI设计

- 不模拟巴菲特、段永平或游资人物发言；
- 不把未经回测的180条规则和人物平均分当作投资共识；
- 不默认抓取需要登录的雪球、淘股吧、社交平台或UGC；
- 不实现重复的行情/财务网络适配器和磁盘缓存；
- 不把0当作缺失；
- 不让LLM心算DCF、PE或目标价；
- 不默认启动公网隧道、浏览器或安装依赖。
