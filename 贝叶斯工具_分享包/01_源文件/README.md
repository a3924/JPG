# 贝叶斯量化决策引擎 v1.0 — 项目总览

> 把"贝叶斯选股提示词（小浒尾）" + "SuperMind 782 行因子脚本"升级为一套 **本地化、端到端、A 股个股贝叶斯判断报告** 的自动化系统。
> 2026-07-20 收尾文档 · 状态：v1.0 已可端到端实跑，下方有 7 项待办。

---

## 一、目标与架构

### 三层自动化
```
┌──────────────────────────────────────────────────────────────┐
│  📊 数据层 (db_sync.py + MCP)                                │
│     • 通达信 MCP → K 线 / 估值 / 财报                         │
│     • 腾讯自选股 MCP → 一致预期 / 股东 / 宏观 / 行业           │
│     • 指南针 .vdat → 0AMV（每日手动触发 zhinanzhen skill）     │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  🧮 因子层 (factor_engine.py)                                │
│     • 41 个因子本地计算（迁移自 SuperMind 782 行脚本）          │
│     • 纯计算无 IO，输入 DataFrame，输出 dict                   │
│     • 修复了原脚本 vol_ma_ago bug                              │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  🎯 决策层 (bayesian_engine.py + oamv_analyzer.py)            │
│     • 6 大因子 LLR 查表：经济 / 政治 / 行业 / 企业 / 市场 / 情绪│
│     • 后验概率 → 仓位映射（7 档）                              │
│     • 0AMV V1.0 市场状态判定 → 7 档仓位区间                    │
│     • 仓位整合公式 = 0AMV区间 × 贝叶斯方向系数                  │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  📝 报告层 (report.py)                                        │
│     • 端到端编排：db_sync → factor → bayesian → 0AMV          │
│     • 0AMV 保鲜期硬前置（过夜即拒绝出报告）                     │
│     • Markdown 骨架 + AI 解读（多空逻辑 / 风险 / 操作建议）     │
│     • 文件名：`股票名称 代码 时间 评分.md`                     │
└──────────────────────────────────────────────────────────────┘
```

---

## 二、文件清单（3 个位置）

### 📦 Skill 主体（WorkBuddy 加载）
```
C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\
├── SKILL.md                              # 完整架构 + 数据源对照 + 调用流程
├── references/
│   ├── 提示词_贝叶斯多因子模型.md         # 修复占位符（H₁/H₂/H₃、E₁~E₆）
│   └── 原始代码_量化计算工具.txt          # 782 行 SuperMind 脚本备份
└── scripts/
    ├── db_sync.py                        # 数据层：7 套 schema + Parquet 缓存
    ├── factor_engine.py                  # 41 因子函数化
    ├── bayesian_engine.py                # 6 LLR 映射 + 后验概率 + 仓位映射
    ├── oamv_analyzer.py                  # 0AMV V1.0（MA5/10/20/120）
    └── report.py                         # 端到端编排 + AI_解读模板
```

### 📚 工作目录（用户积累的资料）
```
D:\AILIANGHUA\贝叶斯工具\
├── 贝叶斯选股提示词 小浒尾.txt           # 原始提示词（GBK 编码）
├── 量化计算工具.txt                      # 原始 SuperMind 782 行脚本
├── references/
│   ├── 因子库_中证500.md                  # 41 因子回测母稿（2025.3.1~2026.7.14）
│   ├── 因子库_中证2000.md                 # 小盘增量对比
│   └── 因子库_README.md                   # 因子库入口
└── README.md                             # ← 本文档
```

### 📊 报告输出
```
D:\AILIANGHUA\贝叶斯报告\
└── 浪潮信息 000977 20260720 51.md        # 实跑报告 v5（含 41 因子 + 7 个 zz500 截面因子）
```

---

## 三、端到端使用流程

### 第一步 — 准备 0AMV 数据（每日必做）

1. 打开指南针软件
2. 进入 0AMV 日 K 线界面，等待数据补到今天
3. **完全关闭** 指南针（释放 .vdat 文件锁）
4. 跑提取脚本：
   ```bash
   "C:\Users\Aa182\.workbuddy\binaries\python\envs\default\Scripts\python.exe" ^
   "C:\Users\Aa182\.workbuddy\skills\zhinanzhen-0amv-daily-db\scripts\build_oamv_db.py"
   ```

### 第一步半 — 同步中证 500 截面（每日必做，22 秒）

```bash
"C:\Users\Aa182\.workbuddy\binaries\python\envs\default\Scripts\python.exe" ^
"C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\scripts\bulk_sync_zz500.py"
```

> 用 pytdx 直连通达信公网（4 个 IP 轮询防限流），22.4 秒拉完 500 只股 × 60 日 = 30000 行 panel。
> 输出：`data/section/zz500_60d.parquet`

### 第二步 — 跑个股报告

#### 路径 A（推荐，最稳）：run_report.py 一键 CLI

在 skill 的 scripts 目录运行，给股票代码即可，数据自动取（本地缓存优先，缺失则 pytdx 直连拉取并缓存）：

```bash
cd C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\scripts
python run_report.py 000977 浪潮信息       # 自动取数 + 算全因子 + 出完整报告
python run_report.py 600519 贵州茅台
python run_report.py 000977 --no-oamv       # 跳过 0AMV 保鲜（测试/无 0AMV 时用）
```

报告输出到 `D:\AILIANGHUA\贝叶斯报告\{名称} {代码} {日期} {评分}.md`。

#### 路径 B（对话式，数据最全）：向 WorkBuddy 说

> "跑一下 浪潮信息 / 茅台 / 比亚迪 ..."

agent 会先经 MCP 拉齐贝叶斯 E1-E6 所需的真实数据并写入本地缓存：
- `mcp__westock-mcp__data_macro` → `data/macro/latest.json`（E1 经济）
- `mcp__westock-mcp__data_profile` / `data_industry_chain` → `data/industry/{code}.json`（E3 行业）
- `mcp__tdx-connector__tdx_security_deep_info` / `data_consensus` → `data/valuation/{code}.json`（E4 企业）
- `mcp__westock-mcp__data_news` → `data/news/{code}.json`（新闻段）
- AI 据近期政策新闻判定 PSI → `data/psi/{code}.json`（E2 政治）
- 再调 `report.generate_report()`（已透传截面数据）→ 出含真实 E1-E6 + 新闻的完整报告。

> 若跳过 MCP 取数直接跑 run_report.py（路径 A），E1/E2/E3/E6 会回退**默认占位值**，
> 报告第十一节用 ⚠️默认 标出，并提示「数据完整性」需补数后重跑。路径 B 出的报告才是全真实数据。

### 报告命名规则
- **路径**：`D:\AILIANGHUA\贝叶斯报告\`
- **文件名**：`股票名称 代码 时间 评分.md`（一个股票一个文件）
- **评分**：`P(H₁|E) × 100`（取整数）
  - > 70 强烈看多
  - 60-70 看多
  - 50-60 偏多
  - 40-50 中性偏弱
  - 30-40 偏空
  - < 30 看空

---

## 四、数据流（MCP 调用对照表）

| 数据 | MCP 工具 | 说明 |
|---|---|---|
| K 线行情 | `mcp__tdx-connector__tdx_kline` | 个股 + 沪深 300 基准 |
| 估值/市值/财报 | `mcp__tdx-connector__tdx_quotes(hasCwInfo=1)` | PE/PB/PS/股本/行业 |
| 一致预期 | `mcp__westock-mcp__data_consensus` | EPS / 目标价 |
| 股东户数 | `mcp__westock-mcp__data_shareholder` / `tdx_api_data` | 户数变化 |
| 宏观 | `mcp__westock-mcp__data_macro` | GDP / M2 / PMI / 利差 |
| 行业归属 | `tdx_quotes.HyIndustry`（数字字段，非字符串）| 用于 BCI/CR4 |
| 0AMV 资金面 | 指南针 .vdat → `zhinanzhen-0amv-daily-db` skill | 每日手动触发 |

---

## 五、决策逻辑摘要

### 5.1 假设空间
| 假设 | 含义 | 默认先验 |
|---|---|---|
| H₁ 显著上涨 | r_T ≥ +15% | 0.30（震荡市）|
| H₂ 震荡 | -15% < r_T < +15% | 0.45 |
| H₃ 显著下跌 | r_T < -15% | 0.25 |

### 5.2 仓位整合公式
```
最终仓位 = 0AMV区间  ×  贝叶斯方向系数
```
| 贝叶斯方向 | 系数 |
|---|---|
| 看多（强）| 1.0 |
| 偏多 | 0.75 |
| 中性 | 0.5 |
| 偏空 | 0.25 |
| 看空 | 0.0 |

例：0AMV 判 **震荡偏空**（20~40%），贝叶斯判 **偏多**（系数 0.75）
→ 最终 0AMV×贝叶斯 = 20~40% × 0.75 → 整理为 **25~35%**（向 5% 取整）

### 5.3 止损规则
- **价格止损**：跌破 MA20 或 -6.5%
- **后验止损**：P(H₁|E) 跌破 0.50 → 减仓一半
- **拥挤度警戒**：YJD > 200 → 减仓一半

---

## 六、当前能力 vs 下版本待办

### ✅ 已实现
- 41 因子本地计算（含 vol_ma_ago bug 修复）
- 6 LLR 查表（机械工作） + 后验概率公式 + 7 档仓位映射
- 0AMV V1.0 完整代码化（MA5/10/20/120、连续 5 天同方向、7 档）
- 0AMV 保鲜期硬前置（过夜即拒绝出报告）
- 仓位整合（0AMV × 贝叶斯方向系数）
- Markdown 报告骨架 + AI 解读占位
- 报告命名规则（股票名称 代码 时间 评分）
- 修复 3 处查表 bug（F-Score / BCI / ACSI 表项顺序）
- **v1.9**：贝叶斯 E1-E6 真实数据接入（宏观/行业/PSI/估值/情绪缓存）+ 新闻段 + 第十一节「输入数据明细」✅真实/⚠️默认 标记 + 0AMV 盘前默认昨天 + 修复权重列 0.00 显示
- **v1.10**：企业深度体检（巴菲特式）+ 融资融券流动性。报告新增第十-A 节（主营/护城河/5年财务质量/风险信号，源 `data/corp`）+ 第十-B 节（融资余额/占比/日变动，源 `data/margin`）；决策层接入 `corp_quality`(E4) 与 `margin_balance_ratio/margin_trend`(E5) 信号（缺则 0，向后兼容）；估值缓存 PE_TTM/PB/PS 修正为现价 TTM 口径（v1.9 误把前向 PE 标成 PE_TTM）
- **v1.11**：融资融券流动性归位 E2（政策+流动性维度，按用户判定从 E5 移出），决策层 `margin_balance_ratio/margin_trend` 改由 `llr_e2_political` 消费；新增**北向资金**维度（经 `data_north_holding` 拉个股北向季度持仓，写 `data/north/{code}.json`，并入 E2 LLR：季环比增持 +0.15、持股>2% 质量背书 +0.05、年内大幅净减持轻惩 −0.05）；第十-B 节改名「政策与流动性（E2：融资融券 + 北向资金）」；核查 `data_buyback` 本区间无回购计划（报告附注）。评分 49→50（流动性信号权重从 E5 0.12 升到 E2 0.18）

### ⏳ 待办（优先级排序）

| # | 任务 | 优先级 | 工作量 |
|---|---|---|---|
| 1 | **E₅ LLR 查表用全量截面因子**（用 7 个 Alpha 截面分位代替 AI 拍的 momentum%）| 高 | 0.5 天 |
| 2 | per-day HSL 历史换手率（用 tdx_security_deep_info 历史数据）| 中 | 0.5 天 |
| 3 | 一键 CLI 入口 `run_report.py`（替代当前 agent 编排）| ✅ 已完成（2026-07-21）| — |
| 4 | 历史报告归档数据库（SQLite + 跨日期对比）| 低 | 2 天 |
| 5 | E3 行业 CR4/BCI/ROE 真实数据（需行业集中度数据源）| 中 | 0.5 天 |
| 6 | E6 情绪 ACSI 分位真实数据（需 data_score 接入）| 中 | 0.5 天 |

> **v1.2 闭环**：#3 F1（用 volume 代理）+ #2 DDE/Net_Flow_Rate/Chip_Quality + #4 zz500 截面因子（501 只股全量）均已完成（2026-07-21 凌晨）

---

## 七、已知小瑕疵（不阻塞使用）

1. **E₅ momentum_percentile 输入矛盾**：当前 AI 给 90%，但 MA200 偏离 +23.6% 应该给 10%。已暴露在第一份浪潮信息报告的"模型局限"段，等 #1 完成后自动消失。
2. **空白数据**：F1 / DDE / Net_Flow_Rate / Composite_Chip_Quality 当前为 NaN，报表显示"数据缺失"（需接通 MCP data_flow / data_chip）。
3. **E3 行业 CR4/BCI / E6 情绪 ACSI 缺真实数据时回退默认**：v1.9 已接新闻→PSI、宏观→E1、估值→E4；v1.10 已补 E4 企业深度体检(corp)与融资融券流动性(margin)，v1.11 将融资融券与北向资金并入 E2（政策+流动性）。E3 的行业集中度(CR4/BCI)与 E6 的 ACSI 仍需补行业/情绪数据源（待办 #5/#6），缺时报告用 ⚠️默认 标出。

---

## 八、参考文献

- 原贝叶斯提示词：`D:\AILIANGHUA\贝叶斯工具\贝叶斯选股提示词 小浒尾.txt`（GBK 编码，作者：小浒尾）
- 原 SuperMind 脚本：`D:\AILIANGHUA\贝叶斯工具\量化计算工具.txt`（UTF-8，782 行）
- zhinanzhen 0AMV skill（数据源）：`C:\Users\Aa182\.workbuddy\skills\zhinanzhen-0amv-daily-db\`
- 通达信 MCP：`mcp__tdx-connector`（WorkBuddy 内置）
- 腾讯自选股 MCP：`mcp__westock-mcp`（WorkBuddy 内置）

---

## 九、下一次跑报告（你只需要做的事）

1. 打开指南针 → 补 0AMV 数据 → 完全关闭 → 跑 `build_oamv_db.py`（zhinanzhen-0amv-daily-db skill）
2. 跑截面刷新：`python bulk_sync_zz500.py`（约 22 秒，刷新 zz500 截面）
3. 跑报告（二选一）：
   - **对话（推荐，数据最全）**：跟我说"**跑一下 [股票名称 或 代码]**"，我会先用 MCP 拉宏观/行业/估值/新闻/PSI 写入缓存，再出含真实 E1-E6 + 新闻的完整报告。
   - **CLI**：`python run_report.py <代码> <名称>` → 自动出报告到 `D:\AILIANGHUA\贝叶斯报告\`（未拉取的 E1/E2/E3/E6 会标 ⚠️默认）
4. 看 `D:\AILIANGHUA\贝叶斯报告\` 下生成的文件

> **盘前提示**：现在还没开盘时直接跑即可，0AMV 自动用昨天收盘数据（保鲜检查已支持盘前）。

文件命名形如：
```
美的集团 000333 20260801 73.md
浪潮信息 000977 20260721 51.md
```

---

*文档由 v1.0 收尾自动整理 · 最后更新 2026-07-20 18:19*
