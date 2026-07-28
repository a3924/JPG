"""
bayesian_engine.py — 决策层
================================================================
职责（混合模式）：
  ┌──────────────────────────────────────────────────────────────┐
  │  代码做（确定性机械工作）                                      │
  │   - 把 6 大因子的输入指标按贝叶斯提示词的查表规则映射为 LLR     │
  │   - 按市场状态加权求 LLR_total                                │
  │   - 后验概率公式（指数运算）                                  │
  │   - 仓位映射 / 止损规则                                       │
  │                                                              │
  │  AI 做（需要判断力的工作）                                    │
  │   - PSI 政策信号强度评分（-3 ~ +3）                          │
  │   - 行业生命周期判定（成长期/成熟期/导入期/衰退期/政策重构期）│
  │   - 量价模式识别（放量突破/缩量回调/横盘/放量下跌/地量反弹）  │
  │   - ACSI 情绪分位综合判断                                     │
  │   - 最终报告撰写（多空逻辑 / 风险点 / 模型局限）             │
  └──────────────────────────────────────────────────────────────┘

调用方式：
    from bayesian_engine import decide

    # 1) 输入因子值（来自 factor_engine.compute_all_factors）
    factors = {...}
    # 2) 宏观指标（来自腾讯自选股 MCP data_macro）
    macro = {'gdp_gap': 0.3, 'm2_yoy': 9.5, 'yield_curve_spread_bp': 50}
    # 3) AI 评估的判断项（用户给或 AI 自己给）
    ai_judgments = {
        'psi_score': 1,                # -3 ~ +3
        'psi_policy_type': 'structural',  # 'structural'|'short_term'
        'psi_months_since': 2,
        'life_stage': '成熟期',
        'volume_price_pattern': '缩量回调 + 未破均线',
        'acsi_percentile': 35,        # 0-100
        'issi_deviation': 'ISSI低估',  # 'ISSI虚高'|'ISSI低估'|'正常'
    }
    # 4) 市场状态（AI 判断后传入）
    market_state = '震荡市'

    result = decide(
        factors=factors,
        macro=macro,
        ai_judgments=ai_judgments,
        market_state=market_state,
        pool='中证500',
    )
    # 返回 dict：prior / llr_each / llr_total / posterior / decision
    # 然后 report.py 把这个 dict 喂给 AI 写最终报告

代码蓝本：
  references/提示词_贝叶斯多因子模型.md
"""
from __future__ import annotations
import math
import numpy as np


# ============================================================
# 配置：权重矩阵 + 先验矩阵（来自贝叶斯提示词第四步、第五步）
# ============================================================
WEIGHTS_BY_STATE = {
    '牛市':       {'E1': 0.15, 'E2': 0.20, 'E3': 0.18, 'E4': 0.22, 'E5': 0.15, 'E6': 0.10},
    '熊市':       {'E1': 0.20, 'E2': 0.25, 'E3': 0.15, 'E4': 0.18, 'E5': 0.12, 'E6': 0.10},
    '震荡市':     {'E1': 0.18, 'E2': 0.18, 'E3': 0.20, 'E4': 0.25, 'E5': 0.12, 'E6': 0.07},
    '政策反转期': {'E1': 0.12, 'E2': 0.35, 'E3': 0.18, 'E4': 0.15, 'E5': 0.12, 'E6': 0.08},
}

PRIOR_BY_STATE = {
    '牛市':       {'H1': 0.45, 'H2': 0.35, 'H3': 0.20},
    '熊市':       {'H1': 0.20, 'H2': 0.35, 'H3': 0.45},
    '震荡市':     {'H1': 0.30, 'H2': 0.45, 'H3': 0.25},
    '政策反转期': {'H1': 0.52, 'H2': 0.30, 'H3': 0.18},
}


# ============================================================
# 通用工具
# ============================================================
def _lookup(value: float, table: list, default: float = 0.0) -> float:
    """
    区间查表：table 是 [(upper, return_value), ...] 按 upper 升序
    返回第一个 upper >= value 的 return_value（保证单调有序）
    """
    for upper, ret_v in table:
        if value <= upper:
            return ret_v
    return table[-1][1] if table else default


# ============================================================
# E₁ 经济因子 LLR 映射
# ============================================================
def llr_e1_economy(macro: dict) -> dict:
    """
    经济因子 LLR 计算
    输入 macro = {
        'gdp_gap': float,             # 实际增速 - 潜在增速，单位 %
        'm2_yoy': float,              # M2 同比增速，单位 %
        'yield_curve_spread_bp': float,  # 10Y-2Y 利差，单位 bp
        'pmi': float (可选),
    }
    返回 {'gdp_contrib', 'm2_contrib', 'yield_contrib', 'pmi_contrib', 'llr': float}
    """
    gdp_gap = macro.get('gdp_gap', 0.0)
    m2 = macro.get('m2_yoy', 8.0)
    spread = macro.get('yield_curve_spread_bp', 0.0)

    # GDP_gap 查表（特殊处理 -0.5 边界）
    if gdp_gap < -0.5:
        gdp_contrib = -0.74
    elif gdp_gap > 0.5:
        gdp_contrib = +1.06
    else:
        gdp_contrib = +0.20  # -0.5 ~ +0.5 中性

    m2_table = [
        (6.0, -0.52),     # M2 < 6%
        (9.0, +0.09),     # 6-9%
        (12.0, +0.84),    # 9-12%
        (15.0, +1.08),    # 12-15%
        (float('inf'), +0.08),  # >15% 过热惩罚
    ]
    m2_contrib = _lookup(m2, m2_table)

    # yield_curve 查表（特殊处理 -50 边界）
    if spread > 100:
        yield_contrib = +0.90
    elif spread >= 0:
        yield_contrib = +0.24
    elif spread >= -50:
        yield_contrib = -0.31
    else:
        yield_contrib = -1.01

    # 子权重：GDP_gap(0.30) + M2(0.25) + PMI(0.25) + yield(0.20)
    # 若 PMI 缺失，按比例重新分配
    pmi = macro.get('pmi', None)
    if pmi is None:
        # GDP + M2 + yield 三者等权 0.35 / 0.35 / 0.30
        llr = 0.35 * gdp_contrib + 0.35 * m2_contrib + 0.30 * yield_contrib
    else:
        # PMI 暂用线性映射（>50 偏多，<50 偏空，±50bp 影响 ±0.5）
        pmi_contrib = (pmi - 50) * 0.05  # 50 → 0, 60 → +0.5, 40 → -0.5
        llr = 0.30 * gdp_contrib + 0.25 * m2_contrib + 0.25 * pmi_contrib + 0.20 * yield_contrib

    return {
        'gdp_contrib': gdp_contrib,
        'm2_contrib': m2_contrib,
        'yield_contrib': yield_contrib,
        'llr': llr,
    }


# ============================================================
# E₂ 政治因子 LLR 映射
# ============================================================
def llr_e2_political(ai_judgments: dict) -> dict:
    """
    政策 + 流动性因子 LLR 计算（PSI 主导 + 时间衰减 + 杠杆/外资流动性）
    输入 ai_judgments = {
        'psi_score': int (-3 ~ +3),
        'psi_policy_type': 'structural' | 'short_term',
        'psi_months_since': int,    # 距政策发布月数
        # 流动性维度（缺失时贡献 0，向后兼容旧报告）
        'margin_balance_ratio': float (融资余额/流通市值),
        'margin_trend': float (融资余额日变动%),
        'north_holding_ratio': float (北向持股比例%),
        'north_q_add_pct': float (北向季度增持幅度%),
        'north_cap_chg_y': float (北向持股市值年变动, 元),
    }
    """
    psi = ai_judgments.get('psi_score', 0)
    psi_table = {
        3: +1.44, 2: +1.03, 1: +0.47,
        0: 0.00,
        -1: -0.36, -2: -1.05, -3: -1.90,
    }
    psi_contrib = psi_table.get(int(psi), 0.0)

    months = ai_judgments.get('psi_months_since', 0)
    policy_type = ai_judgments.get('psi_policy_type', 'structural')
    decay_rate = 0.05 if policy_type == 'structural' else 0.25
    psi_llr = psi_contrib * math.exp(-decay_rate * months)

    # ── 流动性：融资融券（杠杆资金）──
    margin_ratio = ai_judgments.get('margin_balance_ratio', 0.0)
    margin_trend = ai_judgments.get('margin_trend', 0.0)
    try:
        margin_ratio = float(margin_ratio)
    except (TypeError, ValueError):
        margin_ratio = 0.0
    try:
        margin_trend = float(margin_trend)
    except (TypeError, ValueError):
        margin_trend = 0.0
    if margin_ratio >= 0.08:
        margin_contrib = -0.20           # 占比过高 → 强平风险
    elif margin_ratio >= 0.02:
        margin_contrib = +0.15           # 健康杠杆、流动性充裕
    else:
        margin_contrib = 0.0
    margin_trend_contrib = 0.10 if margin_trend > 0 else (-0.10 if margin_trend < 0 else 0.0)

    # ── 流动性：北向资金（外资增量）──
    north_holding_ratio = ai_judgments.get('north_holding_ratio', 0.0)
    north_q_add_pct = ai_judgments.get('north_q_add_pct', 0.0)
    north_cap_chg_y = ai_judgments.get('north_cap_chg_y', 0.0)
    try:
        north_holding_ratio = float(north_holding_ratio)
    except (TypeError, ValueError):
        north_holding_ratio = 0.0
    try:
        north_q_add_pct = float(north_q_add_pct)
    except (TypeError, ValueError):
        north_q_add_pct = 0.0
    try:
        north_cap_chg_y = float(north_cap_chg_y)
    except (TypeError, ValueError):
        north_cap_chg_y = 0.0
    # 季度增持幅度：>0 外资回流 +0.15；<-5% 外资流出 -0.15
    if north_q_add_pct > 0:
        north_contrib = +0.15
    elif north_q_add_pct < -5:
        north_contrib = -0.15
    else:
        north_contrib = 0.0
    # 持股比例质量背书：>2% +0.05；>5% +0.10
    if north_holding_ratio > 5:
        north_contrib += 0.10
    elif north_holding_ratio > 2:
        north_contrib += 0.05
    # 年内大幅净减持（市值 -6 亿以上）：主因被动减持，轻惩 -0.05
    if north_cap_chg_y < -6e8:
        north_contrib += -0.05

    llr = psi_llr + margin_contrib + margin_trend_contrib + north_contrib

    return {
        'psi_score': psi,
        'psi_contrib': psi_contrib,
        'psi_llr': psi_llr,
        'decay_rate': decay_rate,
        'months_since': months,
        'margin_contrib': margin_contrib,
        'margin_trend_contrib': margin_trend_contrib,
        'north_contrib': north_contrib,
        'llr': llr,
    }


# ============================================================
# E₃ 行业因子 LLR 映射
# ============================================================
def llr_e3_industry(ai_judgments: dict) -> dict:
    """
    行业因子 LLR 计算
    输入 ai_judgments = {
        'life_stage': '成长期'|'成熟期'|'导入期'|'衰退期'|'政策重构期',
        'bci_percentile': float (0~100),    # 行业 BCI 历史分位
        'cr4': float (0~100),                # 行业集中度 CR4
        'roe_vs_industry': float,            # 个股 ROE - 行业均值，单位 %
        'policy_reshape_pct': float (可选), # 政策重构期的方向调节
    }
    """
    life = ai_judgments.get('life_stage', '成熟期')
    life_adj_map = {
        '成长期': +0.12,
        '成熟期': +0.02,
        '导入期': -0.05,
        '衰退期': -0.15,
        '政策重构期': ai_judgments.get('policy_reshape_pct', 0.20),
    }
    life_adj = life_adj_map.get(life, 0.0)

    bci_pct = ai_judgments.get('bci_percentile', 50)
    # BCI 分位：Top20%→+1.17, 20~40%→+0.57, 40~60%→+0.11, 60~80%→-0.48, Bottom20%→-1.29
    bci_table = [
        (20.0, +1.17),
        (40.0, +0.57),
        (60.0, +0.11),
        (80.0, -0.48),
        (float('inf'), -1.29),
    ]
    bci_contrib = _lookup(bci_pct, bci_table)

    cr4 = ai_judgments.get('cr4', 50)
    roe_vs = ai_judgments.get('roe_vs_industry', 0)
    if cr4 > 70 and roe_vs > 5:
        competition = +0.65  # 强护城河
    elif 40 <= cr4 <= 70:
        competition = +0.15  # 竞争稳定
    elif cr4 < 40:
        competition = -0.55  # 价格战
    else:  # CR4 > 70 但 ROE 落后
        competition = -0.90
    # 注：原提示词的 "CR4 下降且 ROE 下滑" 情况用最后一个分支近似

    # 行业因子 LLR：行业景气度(bci) + 竞争格局 + 生命周期先验调整
    # 生命周期先验是 "对 P(H1) 的基础修正"，不是 LLR 贡献，这里我们把它转成 LLR 形式
    # 简化处理：生命周期调整按 1:5 转 LLR（即 +12% P(H1) ≈ +0.6 LLR）
    life_as_llr = life_adj * 5

    llr = bci_contrib + competition + life_as_llr

    return {
        'life_stage': life,
        'life_adj': life_adj,
        'bci_contrib': bci_contrib,
        'competition_contrib': competition,
        'llr': llr,
    }


# ============================================================
# E₄ 企业因子 LLR 映射
# ============================================================
def llr_e4_company(factors: dict, ai_judgments: dict) -> dict:
    """
    企业因子 LLR 计算
    输入:
      factors = {
          'PE_TTM': float,
          'PB': float,
          'PS_TTM': float,
          'Log_Total_Market_Value': float,
      }
      ai_judgments = {
          'f_score': int (0~10),        # 改良 F-Score 评分
          'peg': float (可选),           # PEG（成长股用）
          'board_type': 'main'|'growth',  # 沪深主板 / 科创板创业板
      }
    """
    f_score = ai_judgments.get('f_score', 5)
    # F-Score: 9~10→+1.43, 7~8→+0.69, 5~6→+0.03, 3~4→-0.63, 0~2→-1.35
    f_score_table = [
        (2.5, -1.35),     # 0~2 分
        (4.5, -0.63),     # 3~4 分
        (6.5, +0.03),     # 5~6 分
        (8.5, +0.69),     # 7~8 分
        (float('inf'), +1.43),  # 9~10 分
    ]
    f_contrib = _lookup(f_score, f_score_table)

    # 估值补充
    board = ai_judgments.get('board_type', 'main')
    pe_ttm = factors.get('PE_TTM', None)
    valuation_contrib = 0.0
    # 亏损公司（负 PE / TTM 净利为负）：一律视为高估 + 基本面风险，优先于 PEG/PE 分档
    if (pe_ttm is not None and not (isinstance(pe_ttm, float) and np.isnan(pe_ttm))
            and pe_ttm <= 0):
        valuation_contrib = -0.65
    elif board == 'growth' and 'peg' in ai_judgments:
        peg = ai_judgments['peg']
        if peg < 0.8:
            valuation_contrib = +0.90
        elif peg <= 1.5:
            valuation_contrib = +0.25
        elif peg > 2.0:
            valuation_contrib = -0.50
    else:
        pe = pe_ttm
        if pe is not None and not (isinstance(pe, float) and np.isnan(pe)):
            if pe < 10:
                valuation_contrib = +0.85
            elif pe <= 20:
                valuation_contrib = +0.30
            elif pe <= 35:
                valuation_contrib = -0.10
            else:
                valuation_contrib = -0.65

    # 企业深度体检信号（巴菲特式）：corp_quality ∈ [-1,1]，由 FunmScore 映射
    # 缺失时默认 0，不影响旧报告
    corp_quality = ai_judgments.get('corp_quality', 0.0)
    try:
        corp_quality = float(corp_quality)
    except (TypeError, ValueError):
        corp_quality = 0.0
    corp_quality_contrib = corp_quality * 0.5

    llr = f_contrib + valuation_contrib + corp_quality_contrib

    return {
        'f_score': f_score,
        'f_contrib': f_contrib,
        'valuation_contrib': valuation_contrib,
        'corp_quality': corp_quality,
        'corp_quality_contrib': corp_quality_contrib,
        'board': board,
        'llr': llr,
    }


# ============================================================
# E₅ 市场技术因子 LLR 映射
# ============================================================
def llr_e5_market(factors: dict, ai_judgments: dict) -> dict:
    """
    市场技术因子 LLR 计算
    输入:
      factors = {MA200偏离, F1, F2, F3, F4, YJD, Volume_Ratio, Net_Flow_Rate, ...}
      ai_judgments = {
          'momentum_percentile': float (0~100),  # 20 日收益率历史分位
          'volume_price_pattern': str,           # 5 个模式之一
          'shareholder_change_pct': float,       # 股东户数季度环比变化 %
      }
    """
    momentum_pct = ai_judgments.get('momentum_percentile', 50)
    # 动量分位数：Top10%→+0.54, 10~30%→+0.30, 30~70%→+0.06, 70~90%→-0.24, Bottom10%→+0.42(反转)
    # 注：momentum_pct 是"过去 20 日收益率历史分位数"（值越大=越弱）
    # 但分位排列是 Top10% = pct<=10 是强动量，Bottom10% = pct>=90 是超卖
    # 所以查表顺序是 pct 升序
    momentum_table = [
        (10.0, +0.54),     # pct <= 10% → Top 10% 强动量
        (30.0, +0.30),     # 10~30%
        (70.0, +0.06),     # 30~70% 中性
        (90.0, -0.24),     # 70~90%
        (float('inf'), +0.42),  # >90% Bottom 10% A 股反转效应！
    ]
    momentum_contrib = _lookup(momentum_pct, momentum_table)

    pattern_map = {
        '放量突破 + 创新高':        +0.88,
        '缩量回调 + 未破均线':      +0.55,
        '横盘整理 + 量能萎缩':      +0.15,
        '放量下跌 + 跌破关键支撑':  -0.95,
        '地量反弹 + 未见活跃迹象':  -0.40,
    }
    pattern = ai_judgments.get('volume_price_pattern', '横盘整理 + 量能萎缩')
    pattern_contrib = pattern_map.get(pattern, 0.0)

    shr_chg = ai_judgments.get('shareholder_change_pct', 0)
    if shr_chg < -15:
        chip_contrib = +0.70
    elif shr_chg < -5:
        chip_contrib = +0.30
    elif shr_chg <= 5:
        chip_contrib = +0.05
    elif shr_chg <= 15:
        chip_contrib = -0.35
    else:
        chip_contrib = -0.80

    # 注：融资融券 / 北向资金等"流动性"信号已并入 E2（政策+流动性），E5 仅保留市场技术。
    llr = momentum_contrib + pattern_contrib + chip_contrib

    return {
        'momentum_contrib': momentum_contrib,
        'pattern': pattern,
        'pattern_contrib': pattern_contrib,
        'shareholder_change_pct': shr_chg,
        'chip_contrib': chip_contrib,
        'llr': llr,
    }


# ============================================================
# E₆ 情绪因子 LLR 映射
# ============================================================
def llr_e6_sentiment(ai_judgments: dict) -> dict:
    """
    情绪因子 LLR 计算（ACSI 反向使用）
    输入 ai_judgments = {
        'acsi_percentile': float (0~100),
        'issi_deviation': 'ISSI虚高'|'ISSI低估'|'正常' (可选),
    }
    """
    acsi = ai_judgments.get('acsi_percentile', 50)
    # ACSI 反向：<10%→+1.15, 10~25%→+0.60, 25~75%→+0.11, 75~90%→-0.44, >90%→-1.06
    acsi_table = [
        (10.0, +1.15),     # <10% 极度恐慌 → 逆向看多
        (25.0, +0.60),
        (75.0, +0.11),
        (90.0, -0.44),
        (float('inf'), -1.06),  # >90% 极度狂热 → 逆向看空
    ]
    acsi_contrib = _lookup(acsi, acsi_table)

    issi_adj_map = {
        'ISSI虚高': -0.20,
        'ISSI低估': +0.20,
        '正常': 0.0,
    }
    issi_dev = ai_judgments.get('issi_deviation', '正常')
    issi_adj = issi_adj_map.get(issi_dev, 0.0)

    llr = acsi_contrib + issi_adj

    return {
        'acsi_percentile': acsi,
        'acsi_contrib': acsi_contrib,
        'issi_deviation': issi_dev,
        'issi_adj': issi_adj,
        'llr': llr,
    }


# ============================================================
# 后验概率公式
# ============================================================
def posterior(prior: dict, llr_total: float) -> dict:
    """
    P(Hᵢ|E) = P(Hᵢ) × exp(αᵢ × LLR_total) / Σⱼ P(Hⱼ) × exp(αⱼ × LLR_total)

    其中 α₁ = +1（上涨假设方向一致），α₂ = 0（中性，无方向），
    α₃ = -1（下跌假设方向相反）
    """
    weights_alpha = {'H1': +1.0, 'H2': 0.0, 'H3': -1.0}
    numerator = {h: prior[h] * math.exp(weights_alpha[h] * llr_total) for h in ['H1', 'H2', 'H3']}
    denom = sum(numerator.values())
    post = {h: numerator[h] / denom for h in ['H1', 'H2', 'H3']}
    return post


# ============================================================
# 仓位决策映射（与 提示词_贝叶斯多因子模型.md 第六步一致）
# 主信号 = 综合评分(=P(H1|E)×100)；低分(<=30)在 A 股多头视角下不持多头(空仓/做空)。
# ============================================================
def _position_from_score(score: float):
    """评分(=P(H₁|E)×100) → (方向, 多头下限%, 多头上限%, 操作指令)。
    仅 A 股多头视角：评分<=30 一律 0% 多头（空仓），对应提示词「做空」语义；
    提示词第六步原表在低分档写「轻仓做空/中仓做空/满仓做空」，本工具统一落地为 0% 多头。
    """
    if score > 70:   return ('看多', 80, 100, '积极买入')
    if score > 60:   return ('看多', 50, 80, '买入建仓')
    if score > 50:   return ('偏多', 20, 50, '试探性买入')
    if score > 40:   return ('中性', 0, 20, '观望不操作')
    if score > 30:   return ('偏空', 0, 0, '空仓/轻仓做空')
    if score >= 20:  return ('看空', 0, 0, '空仓/积极减仓')
    return ('强烈看空', 0, 0, '清仓/强力做空')


def position_map(P_H1: float, llr_total: float) -> dict:
    """
    根据综合评分(=P(H1|E)×100) 映射到仓位建议；低分不持多头，与提示词第六步一致。
    返回 {'direction', 'position', 'action', 'confidence'}
    """
    score = P_H1 * 100
    direction, lo, hi, action = _position_from_score(score)
    position = f'{lo}~{hi}%' if (lo, hi) != (0, 0) else '0%（空仓）'
    if score > 70 or score < 20:
        confidence = '高'
    elif score > 50 or score < 30:
        confidence = '中'
    else:
        confidence = '低'
    return {'direction': direction, 'position': position, 'action': action, 'confidence': confidence}


# ============================================================
# 主决策入口
# ============================================================
def decide(
    factors: dict,
    macro: dict,
    ai_judgments: dict,
    market_state: str = '震荡市',
    pool: str = '中证500',
) -> dict:
    """
    一站式贝叶斯决策

    Parameters
    ----------
    factors : 来自 factor_engine.compute_all_factors() 的 dict
    macro : 来自腾讯自选股 MCP data_macro 的宏观 dict
        必需: gdp_gap / m2_yoy / yield_curve_spread_bp
        可选: pmi
    ai_judgments : AI 评估的判断项 dict（PSI/行业生命周期/量价模式/ACSI）
    market_state : '牛市'|'熊市'|'震荡市'|'政策反转期'
    pool : '沪深300'|'中证500'|'中证2000' 等（暂未使用，预留）

    Returns
    -------
    dict:
      {
        'market_state': str,
        'pool': str,
        'prior': {'H1': float, 'H2': float, 'H3': float},
        'llr_each': {
            'E1': {'llr': float, ...},
            'E2': {...}, ..., 'E6': {...}
        },
        'llr_total': float,
        'posterior': {'H1': float, 'H2': float, 'H3': float},
        'decision': {
            'direction': str, 'position': str, 'action': str,
            'confidence': str,
            'stop_loss_trigger': float (P(H1) 跌破即止损)
        },
        'to_ai_prompt': str,  # 给 AI 的精简 prompt
      }
    """
    # 1) 校验市场状态
    if market_state not in WEIGHTS_BY_STATE:
        raise ValueError(f"market_state 必须是 {list(WEIGHTS_BY_STATE.keys())} 之一，传入: {market_state}")

    prior = PRIOR_BY_STATE[market_state]
    weights = WEIGHTS_BY_STATE[market_state]

    # 2) 算 6 个因子的 LLR
    e1 = llr_e1_economy(macro)
    e2 = llr_e2_political(ai_judgments)
    e3 = llr_e3_industry(ai_judgments)
    e4 = llr_e4_company(factors, ai_judgments)
    e5 = llr_e5_market(factors, ai_judgments)
    e6 = llr_e6_sentiment(ai_judgments)

    llr_each = {'E1': e1, 'E2': e2, 'E3': e3, 'E4': e4, 'E5': e5, 'E6': e6}

    # 3) 加权求和
    llr_total = (
        weights['E1'] * e1['llr']
        + weights['E2'] * e2['llr']
        + weights['E3'] * e3['llr']
        + weights['E4'] * e4['llr']
        + weights['E5'] * e5['llr']
        + weights['E6'] * e6['llr']
    )

    # 4) 后验概率
    post = posterior(prior, llr_total)

    # 5) 仓位决策
    decision = position_map(post['H1'], llr_total)
    decision['stop_loss_trigger'] = 0.50  # P(H1|E) 跌破 0.50 即止损

    # 6) 生成给 AI 的精简 prompt（让 AI 据此写最终报告）
    to_ai_prompt = _build_ai_prompt(
        market_state, pool, prior, weights, llr_each, llr_total, post, decision
    )

    return {
        'market_state': market_state,
        'pool': pool,
        'prior': prior,
        'llr_each': llr_each,
        'llr_total': llr_total,
        'posterior': post,
        'decision': decision,
        'to_ai_prompt': to_ai_prompt,
    }


# ============================================================
# AI Prompt 构建器
# ============================================================
def _build_ai_prompt(market_state, pool, prior, weights, llr_each, llr_total, post, decision) -> str:
    """
    把决策层结果打包成一段 prompt，让 AI 据此写最终报告
    """
    def fmt_llr(d):
        """把单个 LLR dict 格式化为字符串"""
        lines = [f"  LLR = {d['llr']:+.3f}"]
        for k, v in d.items():
            if k == 'llr':
                continue
            if isinstance(v, (int, float)):
                lines.append(f"    {k}: {v}")
            else:
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    prompt = f"""你是一位 A 股量化交易员 + 贝叶斯决策专家。
下面是一个 A 股个股的**机械计算结果**（LLR 查表 + 后验概率 + 仓位映射），请你基于这些**确定性的中间结果**，写出最终的投资判断报告。

## 任务背景

- **目标股票**：{{请填写具体股票}}
- **市场状态**：{market_state}
- **股票池**：{pool}

## 先验设定

P(H₁) = {prior['H1']:.2f}   P(H₂) = {prior['H2']:.2f}   P(H₃) = {prior['H3']:.2f}

权重向量（{market_state}）：
  w₁经济={weights['E1']:.2f}  w₂政治={weights['E2']:.2f}  w₃行业={weights['E3']:.2f}  w₄企业={weights['E4']:.2f}  w₅市场={weights['E5']:.2f}  w₆情绪={weights['E6']:.2f}

## 六大因子 LLR（确定性计算结果）

E₁ 经济因子：
{fmt_llr(llr_each['E1'])}

E₂ 政治因子：
{fmt_llr(llr_each['E2'])}

E₃ 行业因子：
{fmt_llr(llr_each['E3'])}

E₄ 企业因子：
{fmt_llr(llr_each['E4'])}

E₅ 市场技术因子：
{fmt_llr(llr_each['E5'])}

E₆ 情绪因子：
{fmt_llr(llr_each['E6'])}

## 综合计算（确定性）

LLR_total = {llr_total:+.3f}

后验概率：
P(H₁|E) = {post['H1']:.3f}
P(H₂|E) = {post['H2']:.3f}
P(H₃|E) = {post['H3']:.3f}

## 仓位决策（确定性查表结果）

- 方向判断：{decision['direction']}
- 建议仓位：{decision['position']}
- 操作指令：{decision['action']}
- 置信度评级：{decision['confidence']}
- 止损触发：P(H₁|E) 跌破 {decision['stop_loss_trigger']:.2f} 时执行

## 请你（AI）做的事

1. **解读 LLR 各分量**：哪几个因子贡献最大？是正贡献还是负贡献？背后的逻辑是什么？
2. **给出主要多头逻辑**（前 3 条最强支撑因子）。
3. **给出主要风险点**（需重点监控的反向信号）。
4. **判断模型局限性**：
   - 是否存在市场结构性变化风险？
   - 是否存在黑天鹅事件尾部风险？
   - 建议额外配置对冲仓位比例？
5. **综合点评**：用一段话给投资者讲清楚这次判断的"故事"，让它读起来像一份私募内部报告而不是干巴巴的数据表。

## 输出格式

按 references/提示词_贝叶斯多因子模型.md 第八步的格式输出完整 Markdown 报告。
"""
    return prompt


# ============================================================
# 自测
# ============================================================
if __name__ == '__main__':
    # 构造一组示例输入
    factors = {
        'PE_TTM': 18.5,
        'PB': 3.2,
        'PS_TTM': 2.5,
        'Log_Total_Market_Value': 24.5,
        'Deviation_From_MA200': 0.05,
    }
    macro = {
        'gdp_gap': 0.3,
        'm2_yoy': 10.5,
        'yield_curve_spread_bp': 75,
        'pmi': 51.2,
    }
    ai_judgments = {
        'psi_score': 1,
        'psi_policy_type': 'structural',
        'psi_months_since': 1,
        'life_stage': '成长期',
        'bci_percentile': 35,
        'cr4': 45,
        'roe_vs_industry': 2,
        'f_score': 7,
        'peg': None,
        'board_type': 'main',
        'momentum_percentile': 25,
        'volume_price_pattern': '缩量回调 + 未破均线',
        'shareholder_change_pct': -8,
        'acsi_percentile': 40,
        'issi_deviation': '正常',
    }

    result = decide(factors, macro, ai_judgments, market_state='震荡市', pool='中证500')

    print("=" * 70)
    print("市场状态:", result['market_state'])
    print("=" * 70)
    print("\n【先验】", result['prior'])
    print("\n【六大因子 LLR】")
    for k, v in result['llr_each'].items():
        print(f"  {k}: LLR = {v['llr']:+.3f}")
    print(f"\n【LLR_total】 {result['llr_total']:+.3f}")
    print("\n【后验概率】", {k: round(v, 3) for k, v in result['posterior'].items()})
    print("\n【决策】", result['decision'])
    print("\n" + "=" * 70)
    print("【给 AI 的 prompt 前 1500 字】")
    print("=" * 70)
    print(result['to_ai_prompt'][:1500])