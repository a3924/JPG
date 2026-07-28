---
name: a-share-buffett-deep-research
description: "This skill should be used when the user asks to generate, update, compare, or audit an A-share deep investment report using a Buffett-style framework, including business quality, moat, management, capital allocation, financial statements, valuation, DCF, peer comparison, industry chain, technical/fund-flow context, risk scenarios, or IPO/pre-listing analysis. It is especially suited to requests such as ‘深度分析这只A股’, ‘按巴菲特框架写研报’, ‘检查这篇研报有没有数据或估值错误’, ‘对比几家公司’, and ‘分析招股书/新股发行价值’."
agent_created: true
version: 1.1.0
---

# A股巴菲特式深度研究与研报审计

## 目标

生成可追溯、可复算、可证伪的A股深度研究报告，或审计已有研报的事实、算术、估值和结论质量。先判断企业，再判断价格；先固定事实，再形成观点；拒绝用章节数量掩盖证据不足。

## 工作模式

先识别用户需要的模式：

1. **生成模式**：从零研究一家已上市A股或拟上市公司。
2. **审计模式**：检查现有Markdown、HTML、PDF或文本研报中的数据、引用、估值和逻辑错误。
3. **更新模式**：以最新公告、财报、行情和事件更新旧研报，保留变更清单。
4. **比较模式**：按统一口径比较2-6家公司，禁止混用财年、币种、静态/TTM/前瞻口径。

## 研究档位与适用标的

先选择 `lite`、`standard` 或 `deep` 档位，再采集与决策相关的维度。按 `references/data-collection-orchestration.md` 计算适用维度和覆盖率，不要求Lite模式补齐技术、资金和舆情。

先分类证券类型：`listed_stock`、`ipo_prelisting`、`etf`、`convertible_bond` 或 `other`。本Skill主流程只处理普通上市公司和IPO/拟上市公司；ETF、可转债和基金切换到对应资产Skill，禁止硬套普通股DCF与护城河框架。

## 强制加载

开始研究前按任务读取以下资料：

- 通用深研：`references/research-framework.md`
- 数据采集编排、覆盖和自审：`references/data-collection-orchestration.md`
- 数据取证与引用：`references/source-data-policy.md`
- 估值或研报审计：`references/valuation-consistency.md`
- IPO、招股书、未上市公司：`references/ipo-prelisting.md`
- 生成最终报告：`references/report-template.md`

若环境中可用，优先调用现有金融能力：

- 加载 `wb-finance-skill` 获取金融数据路由、估值、同行、产业链、公告、资金和技术面方法。
- 加载 `buffett-investment-research` 强化能力圈、护城河、管理层、资本配置、所有者收益和安全边际判断。
- 使用已连接的结构化市场数据源与交易所/公司官网交叉验证；不要重复安装同类数据Skill。

## 研究流程

### 第1步：锁定标的与研究时点

确认公司全称、规范股票代码、交易所、证券类型、上市状态、报告基准日、最新应披露财报期和币种。区分普通上市公司、ETF、可转债，以及已受理、问询、过会、注册、询价、发行和待上市状态。

### 第2步：建立一手事实底稿

按 `references/data-collection-orchestration.md` 选择研究档位、建立维度注册表和采集计划，再按 `references/source-data-policy.md` 收集公告、定期报告、招股书、问询回复、投资者关系记录、交易所披露和结构化市场数据。将底稿保存为符合 `schemas/research_bundle.schema.json` 的JSON；为每个关键数据点记录来源、时点、口径、新鲜度、官方/机构/估算标签，以及每次fallback尝试和失败原因。

### 第3步：运行硬性排除与能力圈检查

检查业务是否能用三句话解释；收入、成本、现金流和资本开支是否可理解；是否依赖外部融资、概念叙事、一次性收益或不可验证的市场空间。遇到关键事实缺失时输出“证据不足”，不要强行评级。

### 第4步：研究企业质量

拆解主营业务、收入和利润来源、单位经济性、行业周期、竞争格局、护城河、客户与供应商集中度、管理层诚信、股权激励、关联交易、资本配置和危机韧性。区分主业利润与投资收益、政府补助、公允价值变动等非经常因素。

### 第5步：分析财务与所有者收益

至少覆盖5年年报和最新季度；上市不足5年的按实际期间。分析收入、归母净利润、扣非归母净利润、经营现金流、自由现金流、ROE、ROIC、毛利率、净利率、应收、存货、资本开支、债务与稀释。优先使用所有者收益或FCFF，不把EBITDA当作现金价值。

### 第6步：选择适配的估值方法

先判断公司类型，再选择PE、PEG、PB、PS、EV/EBITDA、分部估值、股息折现或DCF。解释为什么适用。DCF必须列出逐年FCFF、WACC、永续增长率、净债务、股本和终值占比，不允许只写一个“合理区间”。

### 第7步：执行确定性校验

将关键输入写入JSON后运行：

```bash
python scripts/validate_metrics.py <metrics.json>
python scripts/dcf_scenarios.py <dcf.json>
python scripts/self_review.py <research_bundle.json>
```

在当前环境优先使用受管Python绝对路径。任何PE、PB、PS、市值、股息率、目标价隐含PE或DCF结果与正文不一致时，先修正再输出。自审发现关键维度缺失、关键数据仅有D级来源、多源冲突未解决或计算失败时，先生成恢复任务；无法恢复则将评级降为“证据不足”。

### 第8步：加入市场层但不喧宾夺主

按研究档位和业务相关性补充技术趋势、成交、换手、资金流、融资融券、机构持仓、解禁、机构一致预期、公告催化和舆情。明确时点与来源；无关维度标记 `not_applicable`，不为凑齐章节强行采集。禁止把短期资金信号冒充长期企业价值。

### 第9步：做反向验证与情景分析

写出市场已知共识、尚未充分定价的变量、最薄弱假设和可证伪指标。设置Bear/Base/Bull三情景及触发条件，不用主观概率制造伪精确。

### 第10步：通过质量闸门后交付

确认：标的正确、财报最新、关键数字可追溯、口径统一、计算通过、DCF可复算、官方值与估算值已区分、风险与结论一致、无确定性收益承诺。最终按 `references/report-template.md` 生成HTML研报；用户仅需简短结果时可输出Markdown摘要。

## 审计模式附加规则

审计已有研报时输出四张清单：

1. **事实问题**：过期数据、来源不明、主体或报告期错误。
2. **算术问题**：市值、PE、PB、PS、股息率、增速、目标价隐含估值不一致。
3. **方法问题**：把净利润冒充FCFF、DCF无现金流表、同行不可比、把估算值写成官方值。
4. **结论问题**：证据与评级矛盾、风险未进入估值、用技术面替代基本面、给出过度具体的买卖命令。

按“严重 / 重要 / 一般”分级，并给出可执行修复建议。

## IPO与拟上市公司规则

遇到IPO或拟上市公司时切换到 `references/ipo-prelisting.md`。禁止虚构上市后行情、技术指标、机构评级、GuruFocus评分或历史估值分位。估算结果统一标注“本报告估算，非官方数据”。

## 金融与合规底线

- 禁止编造行情、财务、机构目标价、客户名单、市场份额和评分。
- 禁止混淆净利润、归母净利润、扣非归母净利润、经营现金流和自由现金流。
- 禁止把静态PE、TTM PE、动态PE和前瞻PE混写。
- 禁止给出确定收益承诺或以个人风险偏好不明为前提的强制买卖指令。
- 使用中国市场配色：上涨为红色，下跌为绿色；货币默认人民币并标注口径。
- 数据冲突时优先交易所、公司公告和审计财报，并公开说明差异。

## 输出要求

最终结论必须回答：

- 买这家公司本质上在买什么？
- 护城河是否存在、正在扩大还是收窄？
- 正常化所有者收益是多少，资本配置是否创造每股价值？
- 当前价格隐含了什么增长与利润率假设？
- 哪个事实最可能证伪投资逻辑？
- 还缺哪些关键证据，置信度是多少？

将评级限定为：`强匹配`、`观察名单`、`弱匹配`、`回避`、`能力圈外`、`证据不足`。评级不替代投资建议。
