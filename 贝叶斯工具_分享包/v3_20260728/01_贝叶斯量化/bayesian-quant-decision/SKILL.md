---
name: bayesian-quant-decision
description: 贝叶斯多因子量化决策引擎。从通达信 MCP + 腾讯自选股 MCP 拉数据，本地算 41+ 个量化因子（v1.3 加入归属池/多周期相对强弱/YJD 时序/Alpha102），再按贝叶斯提示词框架（六因子 LLR 叠加 + 后验概率 + 仓位映射）出最终判断报告。当用户要分析 A 股个股、跑量化因子、做贝叶斯择时/选股决策时使用。
---

# 贝叶斯量化决策引擎

## 使用场景

当用户有下列需求时，加载并使用本技能：

- 对 **A 股个股** 做量化多因子 → 贝叶斯决策分析（看多/看空/中性 + 仓位建议 + 止损线）；
- 自动从 **通达信 MCP** 拉行情数据、从 **腾讯自选股 MCP** 拉筹码/一致预期/行业链等独有数据；
- 在本地算 41 个量化因子（Alpha101 + 波动率 + 拥挤度 + 估值 + 盈利预期 + 技术量价）；
- 按贝叶斯提示词的六大因子框架，把因子值映射为 LLR、加权、算后验概率、出报告。

## 架构（三层 + 一个旁路提醒）

```
┌──────────────────────────────────────────────────────────────┐
│  数据层  scripts/db_sync.py                                   │
│   ├── 通达信 MCP (mcp__tdx-connector)                        │
│   │     tdx_kline / tdx_quotes / tdx_security_deep_info      │
│   ├── 腾讯自选股 MCP (mcp__westock-mcp)                       │
│   │     data_chip / data_consensus / data_industry_chain     │
│   │     data_macro / data_shareholder / data_valuation       │
│   └── 本地缓存  data/*.parquet  (全量+增量)                    │
├──────────────────────────────────────────────────────────────┤
│  因子层  scripts/factor_engine.py                             │
│   └── 41 个因子 = Alpha101 + 波动率 + 拥挤度 + 估值           │
│       + 盈利预期 + 技术量价                                  │
│   (迁移自 references/原始代码_量化计算工具.txt)                │
├──────────────────────────────────────────────────────────────┤
│  决策层  scripts/bayesian_engine.py                           │
│   ├── 六大因子 LLR 映射（参考 prompts/贝叶斯提示词）            │
│   ├── 加权 LLR_total                                         │
│   ├── 后验概率 P(H1|H2|H3)                                    │
│   └── 仓位映射 + 动态止损                                    │
├──────────────────────────────────────────────────────────────┤
│  报告层  scripts/report.py                                    │
│   ├── [硬前置] 0AMV 保鲜期检查  ⚠️  不更新不出报告            │
│   ├── 调数据层 → 因子层 → 决策层                             │
│   └── 输出 Markdown / HTML 报告                              │
└──────────────────────────────────────────────────────────────┘
```

## 数据源对照（重要边界）

| 数据类别 | 来源 | 说明 |
|---|---|---|
| A 股日/分钟 K 线 | 通达信 MCP `tdx_kline` | 全 A 5300+ 只；本地缓存后秒出 |
| 个股 F10 / 估值 / 财务 | 通达信 MCP `tdx_api_data` / `tdx_security_deep_info` | 含 PE_TTM / PB / PS_TTM / 总市值 |
| 指数成分股 / 宽基指数 | 通达信 MCP `tdx_index_components` 或腾讯自选股 `data_index` | 用于宽基池（沪深300 / 中证500 / 中证2000） |
| 筹码结构 | 腾讯自选股 MCP `data_chip` | 替代 SuperMind 的 query_iwencai 筹码查询 |
| 一致预期 / EPS 修正 | 腾讯自选股 MCP `data_consensus` | 替代 SuperMind 的 get_expectation |
| 行业链 / 板块 | 腾讯自选股 MCP `data_industry_chain` | 替代 SuperMind 的 get_industry |
| 宏观数据（M2 / PMI / GDP） | 腾讯自选股 MCP `data_macro` | 替代 SuperMind 缺失的宏观 |
| 股东户数 / 大股东 | 腾讯自选股 MCP `data_shareholder` | 替代 SuperMind 的股东数据 |
| 指南针 0AMV | 复用 `zhinanzhen-0amv-daily-db` skill 的 CSV 输出 | **手动更新，每次出报告必检新鲜度** |
| AkShare | **不引入** | MCP 全部覆盖，避免重复维护 |

## 调用流程

```
用户给目标股票代码
       ↓
report.py
  ├─ 0AMV 保鲜期检查  ❌过期 → 拒绝出报告，提示重跑 zhinanzhen-0amv-daily-db
  ├─ db_sync.py    → 拉/读本地缓存 → DataFrame
  ├─ factor_engine.py → 算 41 因子 → 因子值 dict
  ├─ bayesian_engine.py → 因子值 → LLR → 后验 → 仓位 → 报告 dict
  └─ 输出 Markdown / HTML

### 日常使用入口：run_report.py（CLI，最常用）

给股票代码即可出报告，无需手动准备数据：

    cd C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\scripts
    python run_report.py 000977 浪潮信息        # 自动取数 + 算因子 + 出报告
    python run_report.py 600519 贵州茅台
    python run_report.py 000977 --no-oamv        # 跳过 0AMV 保鲜（仅因子+贝叶斯，测试用）

run_report.py 自动编排：读本地 K 线缓存（缺失则 pytdx 直连通达信拉取并缓存）
→ 读 zz500 截面 parquet（pivot 成宽表）→ 读沪深300 基准 → factor_engine 算全因子
（含 7+ 截面 Alpha）→ **读贝叶斯输入缓存（宏观/行业/PSI/新闻/情绪/估值）→ bayesian_engine 决策**
→ 渲染完整 Markdown（含「贝叶斯输入数据明细」+「新闻与事件」段）
→ 输出 D:\AILIANGHUA\贝叶斯报告\{名称} {代码} {日期} {评分}.md

> **v1.9 关键**：贝叶斯 E1-E6 不再用硬编码默认值。报告第十一节逐因子展示喂入模型的
> **真实输入数据**，并用 ✅真实 / ⚠️默认 标记每项来源。缺数据自动回退默认并提示补数。

#### 贝叶斯 E1-E6 真实数据来源（v1.9）

| 因子 | 数据 | 本地缓存路径 | 由谁写入 |
|---|---|---|---|
| E1 经济 | GDP缺口 / M2 / 10Y-2Y利差 / PMI | `data/macro/macro_YYYYMMDD.json` + `latest.json`（兼容）| agent 经 `data_macro` 拉取，写入 `daily_sync.py` 同步 |
| E2 政策+流动性 | PSI 评分 / 政策类型 / 距发布月 | `data/psi/{code}.json` | AI 据政策新闻判定 |
| E2 政策+流动性 | 融资融券（融资余额/占流通市值比/日变动） | `data/margin/{code}.json` | agent 经 `data_fund_margin` + tdx 流通市值 |
| E2 政策+流动性 | 北向资金（持股比例/季度增持幅度/持股市值变动） | `data/north/{code}.json` | agent 经 `data_north_holding` |
| E3 行业 | 行业名 / 生命周期 / CR4 / BCI分位 | `data/industry/{code}.json` | agent 经 `data_profile`/`data_industry_chain` |
| E4 企业 | PE/PB/PS / F-Score / PEG | `data/valuation/{code}.json` | agent 经 `tdx_security_deep_info`/`data_consensus` |
| E4 企业（深度体检）| 主营业务/护城河/5年财务质量(ROE/毛利率/OCF)/资本配置/风险信号 | `data/corp/{code}.json` | agent 经 `data_finance`(三大表)+`data_profile`+`data_score`+tdx股本市值 |
| E5 市场 | 动量分位 / 量价模式 / 股东变化 | 因子派生（Alpha84/MACD/股东） | factor_engine |
| E6 情绪 | ACSI 分位 / ISSI 偏离 | `data/sentiment/{code}.json` | agent 经 `data_score` |

## 每日数据同步（v1.10 新增）

**截面 + 宏观每天需要刷新**。文件名带日期，**自动检查 / 自动拉取 / 自动推 GitHub**：

```bash
cd <skill>/scripts
python daily_sync.py check            # 仅检查状态
python daily_sync.py sync-and-push    # 同步 + 推送（默认行为）
```

**截面（zz500 60日 K 线 panel）**：
- 文件：`data/section/zz500_60d_YYYYMMDD.parquet`（同时复制一份 `zz500_60d.parquet` 兼容）
- 拉取方式：`bulk_sync_zz500.py` 用 pytdx 直连通达信公网，500 个股约 60 秒
- 检查逻辑：当 `local_section_date < today` 时重跑

**宏观快照（GDP缺口 / M2 / 利差 / PMI / CPI）**：
- 文件：`data/macro/macro_YYYYMMDD.json`（同时复制一份 `latest.json` 兼容）
- 拉取方式：agent 调 `mcp__westock-mcp.data_macro()`（names 含 cpi_ppi+yield_curve+pmi），然后 stdin 传入 daily_sync
- 检查逻辑：当 `local_macro_date < today` 时需要重新拉取

**GitHub 同步**：默认推送 `D:\ai-oamv\data/{section,macro}/` → `a3924/JPG/data/{section,macro}/`，换机器 `git clone` 后直接 `daily_sync.py check` 即可看到日期是否最新。

> **设计约定**：本引擎只用截面 panel 算 Alpha101 截面因子，不依赖个股 K 线缓存，所以 **22 个股 11 类缓存不需要 GitHub 同步**（每次换机器重新跑对应个股即可）。
| 新闻 | 近期影响力新闻 | `data/news/{code}.json` | agent 经 `data_news` |

缓存读写统一走 `db_sync.read_json / write_json`（通用 JSON 缓存）。
对话模式下，agent 先调 MCP 把这些 json 写好，再跑 run_report.py 即出含真实 E1-E6 + 新闻的完整报告。

> **v1.10 关键（企业深度体检 + 融资融券流动性）**：
> - 报告新增 **第十-A 节「企业深度体检（巴菲特式）」**：主营/护城河(多维诊股评分)/FY2025 财务质量(营收/毛利/ROE/资产负债率/OCF/FCFF/商誉)/TTM 趋势/风险与正向信号/Beta/一致目标价。数据源 `data/corp/{code}.json`。
> - 报告新增 **第十-B 节「流动性与杠杆资金（融资融券）」**：融资余额/融券余额/占流通市值比/日变动/当日买卖。数据源 `data/margin/{code}.json`。
> - 决策层接入两个新信号（缺则贡献为 0，向后兼容）：`corp_quality`（由 FunmScore 映射，进 E4 LLR）×0.5；`margin_balance_ratio`+`margin_trend`（融资融券，进 E5 流动性 LLR）。
> - 估值缓存 PE_TTM/PB/PS 已修正为**现价 TTM 口径**（v1.9 误把前向 PE 标成 PE_TTM）。

> **v1.11 关键（融资融券归位 E2 + 北向资金）**：
> - **融资融券流动性从 E5 归位到 E2**：按用户判定，融资融券属「政策+流动性」维度，应计入 E2 而非 E5。报告第十-B 节改名「政策与流动性（E2：融资融券 + 北向资金）」；决策层 `margin_balance_ratio`+`margin_trend` 改由 `llr_e2_political` 消费（原在 `llr_e5_market`）。
> - **新增北向资金维度**：经 `data_north_holding` 拉取个股北向季度持仓（持股比例/季度增持幅度/市值变动），写 `data/north/{code}.json`，并入 E2 LLR（季环比增持 +0.15，持股>2% 质量背书 +0.05，年内大幅净减持轻惩 -0.05）。
> - 已核查 `data_buyback`：浪潮信息本区间无股票回购计划，回购维度暂无可计入信号（报告附注说明）。

---

## 🚀 换股票「一键流程」SOP（v1.12，最常用！）

> 目的：换任意股票时，agent 不再手写临时 py 脚本转录 MCP 字段（易出语法/日期笔误），
> 改为「填一个 JSON 数值文件 → 跑两条命令」。schema 组装、键名转换、财务比率派生
> 全部由通用脚本 `scripts/build_caches.py` 固化完成。

**标准三步：**

### ① 并行拉取真实数据（15 个 MCP 调用）

先确认代码（`data_search`），再一次性并行发起。`code_ex` 深市=`sz000963`、沪市/科创=`sh600428`：

| MCP 调用 | 参数 | 喂给缓存 |
|---|---|---|
| `data_profile` | `code=<code_ex>` | corp.industry / business |
| `data_score` | `code=<code_ex>` | corp.scores (comp/funm/risk/cap/tec) |
| `data_finance` ×3 | `code, type=income/balance/cashflow, num=5` | corp.fy2025（三大表标量） |
| `data_fund_margin` | `code=<code_ex>` | margin |
| `data_north_holding` | `code=<code_ex>` | north（季度持仓） |
| `data_news` | `symbol=<code_ex>, type=2, limit=30` | news（AI 精选高/中影响力） |
| `data_chip` | `code=<code_ex>` | chip |
| `data_consensus` | `code=<code_ex>` | consensus + corp.target_price |
| `data_shareholder` | `code=<code_ex>` | shareholder（科创/中报前常空） |
| `data_fund_flow` | `code, start, end`（近30日） | fund_flow（透传 data 数组） |
| `data_industry_chain` | `code=<code_ex>, mode=stock` | industry |
| `tdx_security_deep_info` | `query="查询<代码><名>估值/财务/股本市值/融资融券", entity_type="A股代码"` | valuation（PE/PB/PS/BPS/总市值/总股本；返回超长会落盘，用 Grep 提取） |

> 注：`data_valuation`(westock) 经常 deferred 索引失效，估值以 **tdx key_statistics** 为准（字段齐全）。

### ② 填 raw_input JSON

照 `references/raw_input_template.json`（含 `_readme` 逐字段来源说明）把上面拉到的数值填入。
只需填**原始标量**，以下由脚本**自动派生**，不用手算：
- `valuation.eps_ttm` ← np_ttm / total_shares；`float_market_cap`/`float_shares` 缺省=总量
- `corp.finance_fy2025` 的 `gross_margin / net_margin / roe / debt_ratio / ocf_np_ratio` ← 三大表标量
- `corp.corp_quality` ← FunmScore≥80 记 +1，否则 0
- `margin.finance_balance_ratio / total_balance / finance_net_buy_today`
- `north` 若误填 camelCase 会自动转小写键
- **`sentiment` 故意不填** → 引擎用 Tec+资金+动量合成 ACSI

AI 判定项（需人工给）：`psi.psi_score`(∈[-3,+3]，半导体自主可控等最高优先级=3、一般结构性利好=2)、
`corp.moat / risk_flags / positive_flags`、`industry.industry_momentum_ytd / roe_vs_industry`、`f_score`。

### ③ 跑两条命令

```bash
cd C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\scripts
python build_caches.py D:\AILIANGHUA\贝叶斯工具\raw_<code>.json   # 生成 11 类缓存
python run_report.py <code> <名称>                                 # 出报告(自动拉K线+算41因子+贝叶斯)
```

报告输出 `D:\AILIANGHUA\贝叶斯报告\{名称} {代码} {日期} {评分}.md`。

> Python 用 `C:\Users\Aa182\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
> （已装 numpy/pandas/scipy/pyarrow/pytdx）。0AMV 保鲜为硬前置，出报告前先确保
> `D:/AIlianghua/OAMV/0AMV日线数据库_2015至今.csv` 当日已更新（`zhinanzhen-0amv-daily-db`）。

**已验证批次（2026-07-22）**：华东医药 000963(43/中性)、华虹宏力 688347(41/中性)、
药明康德 603259(41/中性)、中远海特 600428(40/偏空空仓)——四只全流程跑通，通用脚本
派生值与手写版逐字段一致。

#### 0AMV 盘前保鲜（v1.9）

`oamv_analyzer.assert_fresh` 现支持盘前/非交易日：0AMV 是**收盘**数据，若当前未到今天收盘
（盘前/盘中/周末/节假日），最新可用行就是**上一交易日（昨天）收盘**，视同新鲜。
故「现在还没开盘」直接跑 run_report.py（不带 --no-oamv）即可，自动用昨天 0AMV 出整合仓位。

> 0AMV 保鲜是硬前置（默认开启）。每日先跑 zhinanzhen-0amv-daily-db 提取最新 0AMV，
> 再跑 bulk_sync_zz500.py 刷新截面；两者新鲜后 run_report 才出含整合仓位的完整报告。
> report.py.generate_report 现已透传 section_* 参数（v1.8 修复此前截面因子全 NaN 的缺口）。
```

## 前置条件

1. WorkBuddy 已安装并连接 `mcp__tdx-connector` 和 `mcp__westock-mcp`（已确认 connected）。
2. 用户的本地磁盘有写入权限（用于本地缓存）。
3. 指南针软件已安装且 0AMV 数据当日已更新（否则报告会被硬拦截）。
4. Python 环境已装好：numpy、pandas、scipy（pandas 已在用户环境里缺，需安装）。

## 已完成 / 待办

| 状态 | 模块 |
|---|---|
| ✅ 已完成 | **全部 10 个 task**：目录骨架 / 提示词修版 / 因子库整理 / 原始代码备份 / 因子引擎 / 数据层 / 决策层 / 报告层（含 0AMV 保鲜期+市场状态判定）|

## 已知瑕疵 & 待修复

- 原始 `量化计算工具.txt` 第 155 行：`vol_ma_ago` 未定义（应为 `vol_ma_120`），换手率兜底分支会 NameError。**v1.3 已修**（`compute_f1_turnover_deviation` 用 `vol_ma_120`）。
- 原始贝叶斯提示词 H₁/H₂/H₃、E₁~E₆ 退化成 `H?`/`E?` 占位符，已在 `references/提示词_贝叶斯多因子模型.md` 中修复。

## v1.3 增量（2026-07-21）

对齐用户给的同花顺 SuperMind 终极机构增强版原始脚本：

| 新增 | 函数 | 作用 |
|---|---|---|
| 个股归属池 | `compute_belonged_pools` | 9 大宽基检测 → 选 primary（沪深300 优先） |
| 多周期相对强弱 | `compute_relative_strength_multi_period` | 1/5/14/30/60 日 × primary_idx |
| YJD 时序字段 | `compute_yjd_composite` 扩展 | ma5/ma20/min50/max50 + status 分类 |
| Alpha102_量能RSI14 | `compute_alpha102` / `_section` | 单股 + 截面两版 |
| 参数扩展 | `compute_all_factors` | 新增 `stock_code` / `primary_idx_df` / `index_pool_map` |
| 报告输出 | `format_factor_report` | 加入归属池 / 多周期相对强弱 / YJD 时序 / Alpha102 |

YJD 阈值（与原脚本 line 553-560 对齐）：
- `> 300` → 🔥 极度过热
- `> 120` → ⚠️ 偏热/拥挤
- `-80 ≤ x ≤ 120` → ⚖️ 中性
- `< -80` → ❄️ 极度弱势

调用方变化：
```python
factors = compute_all_factors(
    stock_code='000977.SZ',       # 新增（必填，要归属池）
    stock_df=stock_df,
    idx_df=idx_df,                # 沪深300 兜底（用于 F3/F4/YJD/Beta）
    primary_idx_df=primary_df,    # 新增（多周期相对强弱的对标基准）
    index_pool_map=pool_map,      # 新增（9 大宽基成分股）
    ...
)
```

第二批（已完成 2026-07-21）：
- ✅ MACD_DIF / DEA / BAR（`compute_macd`）
- ✅ DDE 1/3/5/10 日累计 + 净额率（`compute_dde_multi_period`，接受 `fund_flow_series`）
- ✅ ret20_pct_60d（`compute_ret20_pct_60d`，原脚本 line 382）
- ✅ ATR20 双口径（`ATR20` 元 + `ATR20_Pct` %）
- ✅ 压力位 / 支撑位 / 当前位置（`compute_resistance_support`，60 日高低点近似，待 westock-mcp RS 数据替换）

数据接口扩展：
- `compute_all_factors` 新增 `fund_flow_series: pd.Series | None` 参数
- `report.py` 同步支持 DDE 多日数据接入

## 输出示例（最终报告骨架）

```markdown
# 000725 (京东方A) 贝叶斯量化判断报告

> 分析时间窗口：未来 60 个交易日
> 当前市场状态：[自动判定 牛市/熊市/震荡/政策反转]

【先验设定】
P(H₁) = 0.30  P(H₂) = 0.45  P(H₃) = 0.25

【六大因子评分】
E₁ 经济因子：LLR = +0.45  (权重 0.18)
E₂ 政治因子：LLR = +1.03  (权重 0.18)
...

【综合计算】
LLR_total = +0.87
P(H₁|E) = 0.51  P(H₂|E) = 0.36  P(H₃|E) = 0.13

【核心结论】
方向判断：看多
建议仓位：50-80%
止损触发：P(H₁|E) 跌破 0.50 时
置信度评级：中（LLR 0.5~1.5）
```

---

## 配套阅读

- `references/提示词_贝叶斯多因子模型.md` —— 贝叶斯框架完整正文（修版）
- `references/原始代码_量化计算工具.txt` —— 782 行 SuperMind 因子脚本备份（迁移蓝本）
- `references/数据源对照.md` —— MCP 工具到本地字段的映射表
- 项目目录 `D:\AILIANGHUA\贝叶斯工具\references\` 下另有因子库三份文件（README / 中证500 / 中证2000）