"""
report.py — 报告编排层（端到端入口）
================================================================
职责（按调用顺序）：
  1. 0AMV 保鲜期检查 ⚠️ 不更新不出报告（用户最强调的需求）
  2. 0AMV → 7 档市场状态 + 仓位建议
  3. 调 factor_engine 算 41 因子（输入由 report.py 喂入 dict）
  4. 调 bayesian_engine 算 LLR / 后验概率 / 仓位
  5. 整合 0AMV 仓位与贝叶斯仓位 → 最终仓位
  6. 输出 Markdown 报告骨架（让 AI 填多空逻辑 / 风险点 / 综合点评）

调用方式（用户视角）：
    from report import generate_report

    result = generate_report(
        stock_code='000725.SZ',                 # 股票代码（必填）
        stock_data={'close': Series, ...},       # 由 agent 调 MCP 准备好
        idx_data={'close': Series, ...},         # 沪深300 指数日线
        macro={'gdp_gap': 0.3, 'm2_yoy': 9.5},  # 宏观指标（来自 mcp__westock-mcp.data_macro）
        valuation={'PE_TTM': 15.0, ...},         # 估值（来自 tdx_security_deep_info）
        chip_data={...},                         # 筹码（来自 westock-mcp.data_chip）
        consensus_data={...},                    # 一致预期（来自 westock-mcp.data_consensus）
        shareholder_data={...},                  # 股东户数（来自 westock-mcp.data_shareholder）
        ai_judgments={...},                      # AI 评估的 PSI / 行业生命周期 / 量价模式 / ACSI
        oamv_csv_path='D:/AIlianghua/OAMV/0AMV日线数据库_2015至今.csv',  # 0AMV CSV
        market_state=None,                       # 若为 None，从 0AMV 自动判定
        pool='中证500',
        output_dir='reports/',                   # 报告输出目录
    )
    # 返回 {'report_path': 'reports/000725_SZ_20260720.md', 'result': {...}}

数据契约：
  数据获取由 WorkBuddy agent 在调用本模块前完成（按 db_sync.MCP_SYNC_GUIDE）。
"""
from __future__ import annotations
import os
import json
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# 路径
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(SKILL_DIR, 'data')
REPORTS_DIR = os.path.join(SKILL_DIR, 'reports')

# 默认 0AMV CSV 路径（用户在指南针安装目录里）
DEFAULT_OAMV_CSV_PATHS = [
    r'D:\AIlianghua\OAMV\0AMV日线数据库_2015至今.csv',
    r'D:\AILIANGHUA\贝叶斯工具\0AMV日线数据库_2015至今.csv',
    os.path.join(SKILL_DIR, '0AMV日线数据库_2015至今.csv'),
]


def find_oamv_csv(explicit_path: str | None = None) -> str | None:
    """找 0AMV CSV 路径"""
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    for p in DEFAULT_OAMV_CSV_PATHS:
        if os.path.exists(p):
            return p
    return None


# ============================================================
# 异常
# ============================================================
class StaleOAMVError(Exception):
    """0AMV 数据陈旧"""
    pass


# ============================================================
# 报告生成主入口
# ============================================================
def generate_report(
    stock_code: str,
    stock_data: pd.DataFrame,
    idx_data: pd.DataFrame | None = None,
    macro: dict | None = None,
    valuation: dict | None = None,
    chip_data: dict | None = None,
    consensus_data: dict | None = None,
    shareholder_data: dict | None = None,
    fund_flow: dict | None = None,
    ai_judgments: dict | None = None,
    oamv_csv_path: str | None = None,
    market_state: str | None = None,
    pool: str = '中证500',
    output_dir: str = REPORTS_DIR,
    index_pool_map: dict[str, list[str]] | None = None,
    primary_idx_data: pd.DataFrame | None = None,
    # ── 截面数据（zz500 宽表，行为 date 列为 code）──
    section_close: pd.DataFrame | None = None,
    section_open: pd.DataFrame | None = None,
    section_high: pd.DataFrame | None = None,
    section_low: pd.DataFrame | None = None,
    section_volume: pd.DataFrame | None = None,
    section_amount: pd.DataFrame | None = None,
    section_vwap: pd.DataFrame | None = None,
    fund_flow_series: pd.Series | None = None,
) -> dict:
    """
    端到端生成贝叶斯量化判断报告

    Parameters
    ----------
    stock_code : '000977.SZ'
    stock_data : 个股日线 DataFrame（'open/high/low/close/volume/amount'）
    idx_data : 基准指数（沪深300）日线 DataFrame
    primary_idx_data : 个股归属池选出的 primary 指数日线（可选）；
                       若提供，多周期相对强弱会用它而不是 idx_data
    index_pool_map : {idx_code: [member_stocks]}，由调用方预拉 9 大宽基成分股
    ... （其余参数见原文档）

    返回：
    {
        'report_path': str,
        'oamv_freshness': dict,
        'oamv_state': dict,
        'factors': dict,
        'decision': dict,
        'final_position': dict,
    }
    """
    # ========== 1. 0AMV 保鲜期检查（硬前置） ==========
    from oamv_analyzer import assert_fresh, compute_moving_averages, classify_market_state, MARKET_STATES

    csv_path = find_oamv_csv(oamv_csv_path)
    if csv_path is None:
        raise StaleOAMVError(
            f"\n{'=' * 70}\n"
            f"❌ 找不到 0AMV CSV 文件\n"
            f"{'=' * 70}\n"
            f"  尝试过的路径：\n"
            + '\n'.join(f"    - {p}" for p in DEFAULT_OAMV_CSV_PATHS)
            + (f"\n    - {oamv_csv_path}" if oamv_csv_path else "")
            + f"\n\n👉 请先运行 zhinanzhen-0amv-daily-db skill 提取最新数据。\n"
            f"{'=' * 70}\n"
        )
    oamv_freshness = assert_fresh(csv_path)  # 过期直接 raise

    # ========== 2. 0AMV → 市场状态 + 仓位建议 ==========
    from oamv_analyzer import load_oamv
    oamv_df = load_oamv(csv_path)
    oamv_ma = compute_moving_averages(oamv_df)
    oamv_state = classify_market_state(oamv_ma)

    # 若 market_state 未指定，用 0AMV 自动判定的
    if market_state is None:
        market_state = oamv_state['bayesian_state']

    # ========== 3. 算 41 因子 ==========
    from factor_engine import compute_all_factors, format_factor_report

    factors = compute_all_factors(
        stock_code=stock_code,
        stock_df=stock_data,
        idx_df=idx_data,                  # F3/F4/YJD/Beta 用 idx_data（沪深300 兜底）
        primary_idx_df=primary_idx_data,  # 多周期相对强弱用 primary_idx_data（归属池首选）
        section_close=section_close,
        section_open=section_open,
        section_high=section_high,
        section_low=section_low,
        section_volume=section_volume,
        section_amount=section_amount,
        section_vwap=section_vwap,
        valuation=valuation,
        chip_data=chip_data,
        consensus_data=consensus_data,
        fund_flow=fund_flow,
        fund_flow_series=fund_flow_series,
        stock_turnover=stock_data.get('turnover') if stock_data is not None else None,
        index_pool_map=index_pool_map,
    )

    # ========== 4. 贝叶斯决策 ==========
    from bayesian_engine import decide
    macro = macro or {}
    ai_judgments = ai_judgments or {}

    # 从 shareholder_data 提取 change_pct
    if shareholder_data and 'shareholder_change_pct' not in ai_judgments:
        ai_judgments['shareholder_change_pct'] = shareholder_data.get('change_pct', 0)

    decision_result = decide(
        factors=factors,
        macro=macro,
        ai_judgments=ai_judgments,
        market_state=market_state,
        pool=pool,
    )

    # ========== 5. 整合仓位：0AMV 仓位 × 贝叶斯仓位系数 ==========
    final_position = _integrate_position(oamv_state, decision_result['decision'])

    # ========== 6. 生成 Markdown 报告骨架 ==========
    os.makedirs(output_dir, exist_ok=True)
    report_md = _render_markdown_report(
        stock_code=stock_code,
        oamv_freshness=oamv_freshness,
        oamv_state=oamv_state,
        factors=factors,
        decision_result=decision_result,
        final_position=final_position,
        pool=pool,
        macro=macro,
        ai_judgments=ai_judgments,
    )

    today_str = date.today().strftime('%Y%m%d')
    safe_code = stock_code.replace('.', '_').replace('/', '_')
    report_path = os.path.join(output_dir, f'{safe_code}_{today_str}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_md)

    return {
        'report_path': report_path,
        'oamv_freshness': oamv_freshness,
        'oamv_state': oamv_state,
        'factors': factors,
        'decision': decision_result,
        'final_position': final_position,
    }


# ============================================================
# 仓位整合：0AMV 仓位 × 贝叶斯仓位系数
# ============================================================
def _integrate_position(oamv_state: dict, bayes_decision: dict) -> dict:
    """
    仓位整合逻辑：
      - 0AMV 给出"基础仓位区间"（基于资金面 + 长期趋势）
      - 贝叶斯给出"方向调整系数"（基于六大因子）
      - 最终仓位 = 0AMV 区间下限 + (区间上限 - 下限) × 贝叶斯系数

    贝叶斯方向 → 系数：
      看多 → 1.0   偏多 → 0.75   中性 → 0.5
      偏空 → 0.25  看空/强烈看空 → 0.0
    """
    direction_coef = {
        '看多': 1.0,
        '偏多': 0.75,
        '中性': 0.5,
        '偏空': 0.25,
        '看空': 0.0,
        '强烈看空': 0.0,
    }
    direction = bayes_decision['direction']
    coef = direction_coef.get(direction, 0.5)

    # 解析 0AMV 仓位区间 "X~Y%"
    pos_str = oamv_state['position']
    import re
    m = re.match(r'(\d+)~(\d+)%', pos_str)
    if not m:
        low, high = 30, 50
    else:
        low, high = int(m.group(1)), int(m.group(2))

    final_low = round(low + (high - low) * coef * 0.5)  # 中位加权
    final_high = round(low + (high - low) * coef)

    # 取整到 5%
    final_low = (final_low // 5) * 5
    final_high = (final_high // 5) * 5

    return {
        'direction': direction,
        'bayesian_coef': coef,
        'oamv_range': f'{low}~{high}%',
        'final_range': f'{final_low}~{final_high}%',
        'note': f'基础仓位来自 0AMV {oamv_state["state"]}（{oamv_state["grade"]}），'
                f'按贝叶斯方向"{direction}"系数 {coef} 调整',
    }


# ============================================================
# Markdown 报告渲染
# ============================================================
def _render_markdown_report(
    stock_code: str,
    oamv_freshness: dict,
    oamv_state: dict,
    factors: dict,
    decision_result: dict,
    final_position: dict,
    pool: str,
    macro: dict,
    ai_judgments: dict,
) -> str:
    """生成 Markdown 报告骨架（AI 可直接拿去填多空逻辑/风险点/综合点评）"""
    today = date.today().strftime('%Y-%m-%d')
    sep = "=" * 60

    # 解析 LLR 详情
    llr_each = decision_result['llr_each']
    weights = decision_result.get('weights', {})

    # 找最强多头/空头因子
    llr_pairs = [(k, v['llr']) for k, v in llr_each.items()]
    llr_pairs_sorted = sorted(llr_pairs, key=lambda x: x[1], reverse=True)
    top_bull = llr_pairs_sorted[:2]
    top_bear = llr_pairs_sorted[-2:][::-1]

    md = f"""# {stock_code} 贝叶斯量化判断报告

> 生成时间：{today}
> 分析时间窗口：未来 60 个交易日（约 3 个月）
> 股票池：{pool}

---

## 【市场状态】⭐ 来自 0AMV 自动判定

| 指标 | 值 |
|---|---|
| 0AMV 数据最后日期 | {oamv_freshness['last_date']} |
| 距今天数 | {oamv_freshness['days_lag']} 天（{'✅ 新鲜' if oamv_freshness['is_fresh'] else '❌ 过期'}）|
| 0AMV 评级 | {oamv_state['grade']} |
| 0AMV 状态 | **{oamv_state['state']}** |
| 0AMV 建议仓位 | {oamv_state['position']} |
| 0AMV 状态说明 | {oamv_state['description']} |
| MA120 趋势 | {oamv_state['ma120_trend']} |
| 均线排列 | {oamv_state['alignment']} |
| MA5 / MA10 / MA20 / MA120 | {oamv_state['last_values']['MA5']:.2f} / {oamv_state['last_values']['MA10']:.2f} / {oamv_state['last_values']['MA20']:.2f} / {oamv_state['last_values']['MA120']:.2f} |

→ 贝叶斯引擎接收到的市场状态：`{decision_result['market_state']}`（来自 0AMV 自动映射）

---

## 【先验设定】

P(H₁) = {decision_result['prior']['H1']:.2f}   P(H₂) = {decision_result['prior']['H2']:.2f}   P(H₃) = {decision_result['prior']['H3']:.2f}

权重向量：
"""
    weights = {
        '牛市':       {'E1': 0.15, 'E2': 0.20, 'E3': 0.18, 'E4': 0.22, 'E5': 0.15, 'E6': 0.10},
        '熊市':       {'E1': 0.20, 'E2': 0.25, 'E3': 0.15, 'E4': 0.18, 'E5': 0.12, 'E6': 0.10},
        '震荡市':     {'E1': 0.18, 'E2': 0.18, 'E3': 0.20, 'E4': 0.25, 'E5': 0.12, 'E6': 0.07},
        '政策反转期': {'E1': 0.12, 'E2': 0.35, 'E3': 0.18, 'E4': 0.15, 'E5': 0.12, 'E6': 0.08},
    }
    w = weights.get(decision_result['market_state'], weights['震荡市'])
    md += f"  w₁经济={w['E1']:.2f}  w₂政治={w['E2']:.2f}  w₃行业={w['E3']:.2f}  w₄企业={w['E4']:.2f}  w₅市场={w['E5']:.2f}  w₆情绪={w['E6']:.2f}\n\n"

    md += "## 【六大因子评分】（贝叶斯引擎计算）\n\n"
    factor_names = {
        'E1': '经济因子', 'E2': '政治因子', 'E3': '行业因子',
        'E4': '企业因子', 'E5': '市场技术因子', 'E6': '情绪因子',
    }
    for ei in ['E1', 'E2', 'E3', 'E4', 'E5', 'E6']:
        llr = llr_each[ei]['llr']
        weight = w[ei]
        contribution = llr * weight
        md += f"- **{ei} {factor_names[ei]}**：LLR = `{llr:+.3f}`，权重 = `{weight:.2f}`，贡献 = `{contribution:+.3f}`\n"
    md += "\n"

    md += f"## 【综合计算】\n\n"
    md += f"**LLR_total = `{decision_result['llr_total']:+.3f}`**\n\n"
    md += "后验概率：\n"
    md += f"- P(H₁|E) = {decision_result['posterior']['H1']:.3f}\n"
    md += f"- P(H₂|E) = {decision_result['posterior']['H2']:.3f}\n"
    md += f"- P(H₃|E) = {decision_result['posterior']['H3']:.3f}\n\n"

    md += "## 【核心结论】\n\n"
    md += "| 维度 | 来自 | 结论 |\n|---|---|---|\n"
    md += f"| 方向判断 | 贝叶斯 | **{decision_result['decision']['direction']}** |\n"
    md += f"| 建议仓位 | 贝叶斯 | {decision_result['decision']['position']} |\n"
    md += f"| 操作指令 | 贝叶斯 | {decision_result['decision']['action']} |\n"
    md += f"| 置信度 | 贝叶斯 | {decision_result['decision']['confidence']} |\n"
    md += f"| 0AMV 基础仓位 | 资金面 | {oamv_state['position']} |\n"
    md += f"| **最终仓位（整合）** | **0AMV×贝叶斯** | **{final_position['final_range']}** |\n"
    md += f"| 止损触发 | 贝叶斯 | P(H₁|E) 跌破 {decision_result['decision']['stop_loss_trigger']:.2f} 时执行 |\n\n"

    md += f"> **整合说明**：{final_position['note']}\n\n"

    md += "## 【主要多头逻辑】（AI 待填）\n\n"
    md += f"贝叶斯 LLR 排名前 2 因子：\n"
    for ei, val in top_bull:
        md += f"- **{ei} {factor_names[ei]}** LLR = `{val:+.3f}` → 你的解读：___（为什么这个因子支持看多？）\n"
    md += "\n"

    md += "## 【主要风险点】（AI 待填）\n\n"
    md += f"贝叶斯 LLR 排名后 2 因子：\n"
    for ei, val in top_bear:
        md += f"- **{ei} {factor_names[ei]}** LLR = `{val:+.3f}` → 你的解读：___（为什么这个因子提示风险？）\n"
    md += "\n"

    md += "## 【模型局限性提示】\n\n"
    md += "- 是否存在市场结构性变化风险？ ___\n"
    md += "- 是否存在黑天鹅事件尾部风险？ ___\n"
    md += "- 建议额外配置对冲仓位比例： ___\n\n"

    md += "## 【附：给 AI 的完整输入 prompt】\n\n"
    md += "```\n"
    md += decision_result['to_ai_prompt']
    md += "\n```\n\n"

    md += f"{sep}\n*报告由 bayesian-quant-decision v1.0 自动生成*\n"

    return md


# ============================================================
# 自测
# ============================================================
if __name__ == '__main__':
    print("=" * 70)
    print("report.py 自测")
    print("=" * 70)

    # 0AMV 路径检查
    csv_path = find_oamv_csv()
    if csv_path is None:
        print("⚠️ 未找到 0AMV CSV，无法跑完整流程")
        print("提示：请把 0AMV CSV 放到以下任一位置：")
        for p in DEFAULT_OAMV_CSV_PATHS:
            print(f"  - {p}")
    else:
        print(f"✅ 找到 0AMV CSV: {csv_path}")
        from oamv_analyzer import check_freshness, load_oamv, compute_moving_averages, classify_market_state
        oamv_df = load_oamv(csv_path)
        freshness = check_freshness(oamv_df)
        print(f"   新鲜度: {freshness['is_fresh']}（滞后 {freshness['days_lag']} 天）")
        if freshness['is_fresh']:
            ma = compute_moving_averages(oamv_df)
            state = classify_market_state(ma)
            print(f"   市场状态: {state['grade']} {state['state']}（{state['position']}）")
            print(f"   bayesian_state: {state['bayesian_state']}")

    # 找 0AMV 失败时也能展示其它模块的入口
    print("\n" + "=" * 70)
    print("✅ 自测完成（无 0AMV CSV 时跳过了完整流程）")
    print("=" * 70)