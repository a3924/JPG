---
summary: "跑一份 A 股个股的贝叶斯量化判断报告（端到端）"
read_when:
  - 用户说「跑一下 [股票]」「跑报告」「出贝叶斯判断」「分析股票」「bayesian 一下」
  - 用户在 `D:\AILIANGHUA\贝叶斯报告\` 目录下保存报告
  - 用户提到 0AMV / 后验概率 / 仓位整合
---

# 个股贝叶斯报告 - 端到端 runner

> 这是 `bayesian-quant-decision` skill 的**触发入口**。每次用户要"跑一支股票的报告"时，agent 应该加载本 skill 走完整流程。

---

## 工作流程（严格按顺序）

完整流程 + 数据源对照 + 已知局限：见 `D:\AILIANGHUA\贝叶斯工具\README.md`

### 第 1 步 — 0AMV 保鲜期检查（**硬前置**，不新鲜就停）
```python
from oamv_analyzer import assert_fresh, load_oamv
try:
    oamv_df = load_oamv(r'D:\AIlianghua\OAMV\0AMV日线数据库_2015至今.csv')
    assert_fresh(r'D:\AIlianghua\OAMV\0AMV日线数据库_2015至今.csv')  # max_lag_days=0, 过夜即失败
except StaleOAMVError as e:
    return str(e)  # 提示用户跑 zhinanzhen-0amv-daily-db
```

### 第 2 步 — 通达信 MCP 拉数据
- `tdx_kline` → 个股 K 线 250 日（tqFlag=1 含复权）
- `tdx_kline` → 沪深 300 (300, sh, day, tqFlag=0) 250 日
- `tdx_quotes(hasCwInfo=1)` → PE/PB/PS/市值/F10 数据

### 第 3 步 — 腾讯自选股 MCP 拉数据
- `data_consensus(code)` → EPS / 目标价 / 净利润增速
- `data_macro` → GDP / M2 / PMI / 利差
- `data_shareholder(code)` → 股东户数变化（若空则用 tdx_api_data(gdrs)）

### 第 4 步 — 算 41 因子
```python
from db_sync import read_local
from factor_engine import compute_all_factors
stock_df = read_local(stock_code, 'day')
idx_df = read_local('000300.SH', 'day')
factors = compute_all_factors(stock_df=stock_df, idx_df=idx_df, valuation=valuation)
```

### 第 5 步 — 0AMV 市场状态判定
```python
from oamv_analyzer import compute_moving_averages, classify_market_state
oamv_ma = compute_moving_averages(oamv_df)
oamv_state = classify_market_state(oamv_ma)  # 7 档
```

### 第 6 步 — 调 report.generate_report()
```python
from report import generate_report
result = generate_report(
    stock_code=stock_code,
    stock_data=stock_df, idx_data=idx_df,
    macro=macro, valuation=valuation,
    chip_data=chip_data, consensus_data=consensus_data,
    shareholder_data=shareholder_data,
    ai_judgments=ai_judgments,  # PSI/生命周期/量价模式/ACSI 评分
    pool='中证500' 或 '中证2000',
)
```

### 第 7 步 — AI 写解读
把 generate_report 输出的 prompt 喂给 AI，让它补齐"多空逻辑 / 风险 / 操作建议"三段。

### 第 8 步 — 保存报告
- **路径**：`D:\AILIANGHUA\贝叶斯报告\`
- **文件名**：`股票名称 代码 时间 评分.md`
  - 例：`浪潮信息 000977 20260720 51.md`
  - 例：`比亚迪 002594 20260801 73.md`
- **评分**：`round(P(H₁|E) × 100)`

---

## 关键提醒

- **必查 0AMV**：过期 → 拒绝跑报告，引导重跑 `zhinanzhen-0amv-daily-db`
- **评分规则**：整数 = `P(H₁|E) × 100`
- **仓位整合公式**：最终仓位 = 0AMV区间 × 贝叶斯方向系数
- **报告要包含**：核心结论 / 6 因子明细（每个子项数据来源）/ E5 完整 7 子维度 / 多空逻辑 / 风险点 / 操作建议 / 模型局限
- **必须诚实标注模型局限**：当前 E5 momentum_percentile 与 MA200 偏离可能矛盾；F1/DDE/Net_Flow 数据缺失；截面因子尚未实现

---

## 用户最少要做的事

1. 打开指南针 → 补 0AMV 数据 → 完全关闭 → 跑 `zhinanzhen-0amv-daily-db`
2. 跟 agent 说："跑一下 [股票]"

→ 等 1~2 分钟看 `D:\AILIANGHUA\贝叶斯报告\`

---

## 相关文件

- 主 skill：`C:\Users\Aa182\.workbuddy\skills\bayesian-quant-decision\SKILL.md`
- 项目总览：`D:\AILIANGHUA\贝叶斯工具\README.md`
- 已交付报告：`D:\AILIANGHUA\贝叶斯报告\`

---

*版本：v1.0 · 2026-07-20 收尾*
