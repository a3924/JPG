"""
factor_engine.py — 因子层（纯计算，无 IO）
================================================================
职责：
  算 41+ 个量化因子 = Alpha101（Alpha21/35/39/47/57/70/83/84/99/101/102/176）
                  + 波动率（ATR20 / Parkinson / GarmanKlass / YangZhang / Realized）
                  + 拥挤度（F1 换手率偏离 / F2 价格乖离 / F3 相对强弱 / F4 成交占比 / YJD 综合 + 时序）
                  + 估值（PE_TTM / PB / PS_TTM / Log_Total_Market_Value）
                  + 盈利预期（Consensus_Direction / EPS_Revision_Rate）
                  + 技术量价（Deviation_From_MA200 / Amihud_Illiquidity / RSI14 / DDE_Net_3d
                              Turnover_Percentile_60d / Volume_Ratio / Net_Flow_Rate
                              Composite_Chip_Quality）
                  + 复合因子（Industry_Neutral_Alpha / Residual_Momentum_20d
                              Residual_Volatility_20d / Beta_Neutral_Alpha / Stock_Beta_60d）
                  + 个股归属池与多周期相对强弱（v1.1 新增）

设计：
  - 本模块是**纯计算**，不直接调 MCP / DB；输入是已准备好的 pandas DataFrame。
  - 数据获取由 report.py 编排：先调 mcp__tdx-connector / mcp__westock-mcp 拉数据，
    落本地 Parquet，再把 DataFrame 喂进本模块。

调用方式：
    from factor_engine import compute_all_factors, format_factor_report

    factors = compute_all_factors(
        stock_code='000977.SZ',
        stock_df=stock_df,            # 个股日线 OHLCV+amount
        idx_df=idx_df,                # 基准指数日线（用于 F3/F4/YJD/Beta）
        section_*=section_*,          # 截面 DataFrame
        valuation=valuation,
        chip_data=chip_data,
        consensus_data=consensus_data,
        index_pool_map=pool_map,      # {idx_code: [members]}，由 report.py 预拉
    )
    print(format_factor_report(factors))

代码蓝本：
  references/原始代码_量化计算工具.txt（782 行 SuperMind 脚本）

迁移记录：
  - 修复原 155 行 bug：vol_ma_ago → vol_ma_120 ✅
  - 因子函数化：每个因子一个独立 compute_xxx() 函数
  - 截面依赖因子改为可选 section_df 参数，单股场景不传则返回 None
  - 数据获取层完全剥离（由 db_sync / MCP 负责）
  - v1.1：新增归属池检测 / 多周期相对强弱 / YJD 时序状态 / Alpha102_量能RSI14
  - v1.1：YJD 阈值与原脚本 line 553-560 对齐（-80/120/300）
"""
from __future__ import annotations
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from scipy.stats import linregress


# ============================================================
# 通用工具
# ============================================================
def safe_divide(a, b):
    """安全的除法，分母为 0 时返回 NaN"""
    return a / (b + 1e-9)


def cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """截面排名（行内百分比排名）"""
    return df.rank(axis=1, pct=True)


def _pick_stock(section_value, stock_code: str | None):
    """
    从截面返回值（pd.Series）中取个股分位数。
    section_value 可能是：
      - pd.Series（最后一行的截面分位）
      - float / np.float64（已经取过个股值）
      - None（无截面数据）
    """
    if section_value is None:
        return np.nan
    if isinstance(section_value, pd.Series):
        if stock_code is None:
            return section_value
        # 兼容带后缀和不带后缀
        keys = [stock_code]
        if '.' in stock_code:
            keys.append(stock_code.split('.')[0])
        for k in keys:
            if k in section_value.index:
                return float(section_value[k])
        return np.nan
    # 已经是 scalar
    if isinstance(section_value, (int, float, np.floating)):
        return float(section_value) if not pd.isna(section_value) else np.nan
    return np.nan


def calc_rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """单序列 RSI"""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=n - 1, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(com=n - 1, min_periods=n).mean()
    return 100 - 100 / (1 + (gain / loss.replace(0, np.nan)))


def ts_rank_pct(series: pd.Series, window: int = 252) -> pd.Series:
    """时序百分位排名：当前值在过去 window 期内的百分位"""
    def _rank_func(x):
        arr = np.array(x)
        return (arr[:-1] < arr[-1]).mean()
    return series.rolling(window).apply(_rank_func)


def normalize_stock_code(code: str) -> str:
    """智能补全股票代码后缀"""
    if '.' in code:
        return code
    code = code.strip()
    if code.startswith(('60', '68')):
        return code + '.SH'
    elif code.startswith(('00', '30', '002', '003')):
        return code + '.SZ'
    elif code.startswith(('8', '4')):
        return code + '.BJ'
    return code


def normalize_index_code(code: str) -> str:
    """智能补全指数代码后缀"""
    if '.' in code:
        return code
    code = code.strip()
    if code.startswith('399'):
        return code + '.SZ'
    elif code.startswith('899'):
        return code + '.BJ'
    return code + '.SH'


# ============================================================
# 波动率因子
# ============================================================
def compute_atr20(high: pd.Series, low: pd.Series, close: pd.Series, N: int = 20) -> float:
    """ATR20 - 平均真实波幅"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(N).mean().iloc[-1])


def compute_parkinson_vol(high: pd.Series, low: pd.Series, N: int = 20) -> float:
    """Parkinson_Vol - 基于高低价的极值波动率"""
    parkinson = ((np.log(high / low)) ** 2) / (4 * np.log(2))
    return float(np.sqrt(parkinson.rolling(N).mean()).iloc[-1])


def compute_garman_klass_vol(high: pd.Series, low: pd.Series,
                             close: pd.Series, open_: pd.Series, N: int = 20) -> float:
    """GarmanKlass_Vol - GK 波动率"""
    gk = 0.5 * (np.log(high / low)) ** 2 - (2 * np.log(2) - 1) * (np.log(close / open_)) ** 2
    return float(np.sqrt(gk.rolling(N).mean()).iloc[-1])


def compute_yang_zhang_vol(high: pd.Series, low: pd.Series,
                           close: pd.Series, open_: pd.Series, N: int = 20) -> float:
    """YangZhang_Vol - YZ 波动率（隔夜+日内方差组合）"""
    overnight_ret = np.log(open_ / close.shift(1))
    day_ret = np.log(close / open_)
    return float(np.sqrt(overnight_ret.rolling(N).var() + day_ret.rolling(N).var()).iloc[-1])


def compute_realized_vol_20d(close: pd.Series, N: int = 20) -> float:
    """Realized_Vol_20d - 已实现波动率"""
    return float(close.pct_change().rolling(N).std().iloc[-1])


# ============================================================
# 拥挤度因子（F1~F4 + YJD 综合）
# ============================================================
def compute_f1_turnover_deviation(stock_volume_or_turnover: pd.Series, M: int = 120) -> float:
    """
    F1_TurnoverDeviation - 换手率偏离度

    输入优先用 HSL（真实换手率%）；若没有则用 volume（成交量）作为代理。
    注意：原脚本 155 行有 bug：vol_ma_ago 未定义。迁移版已修正为 vol_ma_120。

    输出：当前相对于 M 日均值的偏离百分比
    """
    s = stock_volume_or_turnover
    if s is None or s.empty or s.isna().all():
        return np.nan
    vol_ma_120 = s.rolling(M).mean()
    if vol_ma_120.iloc[-1] == 0 or np.isnan(vol_ma_120.iloc[-1]):
        return np.nan
    return float(((s.iloc[-1] - vol_ma_120.iloc[-1])
                  / (vol_ma_120.iloc[-1] + 1e-9)) * 100)


def compute_f2_price_deviation(close: pd.Series, N: int = 60, M: int = 120) -> float:
    """F2_PriceDeviation - 价格乖离（收盘 vs N 日均线）"""
    ma_close_n = close.rolling(N).mean()
    return float(((close.iloc[-1] - ma_close_n.iloc[-1]) / (ma_close_n.iloc[-1] + 1e-9)) * 100)


def compute_f3_relative_strength(close: pd.Series, idx_close: pd.Series, M: int = 120) -> float:
    """F3_RelativeStrength - 相对强弱（个股 / 指数的 XB 比值偏离）"""
    if idx_close is None or len(idx_close) < M:
        return np.nan
    # 取个股与指数公共交易日，避免指数缓存滞后 1 天导致末端 NaN
    c, ic = close.align(idx_close, join='inner')
    if len(c) < M:
        return np.nan
    XB = c / ic
    ma_xb = XB.rolling(M).mean()
    return float(((XB.iloc[-1] - ma_xb.iloc[-1]) / (ma_xb.iloc[-1] + 1e-9)) * 100)


def compute_f4_volume_share(stock_amount: pd.Series, idx_amount: pd.Series, M: int = 120) -> float:
    """F4_VolumeShare - 成交占比偏离（个股成交额 / 指数成交额）"""
    if idx_amount is None or len(idx_amount) < M:
        return np.nan
    sa, ia = stock_amount.align(idx_amount, join='inner')
    if len(sa) < M:
        return np.nan
    ZB = sa / (ia + 1e-9)
    ma_zb = ZB.rolling(M).mean()
    return float(((ZB.iloc[-1] - ma_zb.iloc[-1]) / (ma_zb.iloc[-1] + 1e-9)) * 100)


def _classify_yjd_status(yjd_value: float) -> str:
    """
    YJD 阈值分类（与用户原始 SuperMind 脚本 line 553-560 对齐）：
      极度过热:  YJD >  300
      偏热/拥挤: YJD >  120
      中性区间:  -80 ≤ YJD ≤ 120
      极度弱势:  YJD < -80
    """
    if yjd_value is None or np.isnan(yjd_value):
        return "未知"
    if yjd_value > 300:
        return "🔥 极度过热 (风险极高)"
    if yjd_value > 120:
        return "⚠️ 偏热/拥挤"
    if yjd_value < -80:
        return "❄️ 极度弱势/冷门"
    return "⚖️ 中性区间"


def compute_yjd_composite(stock_turnover: pd.Series, stock_amount: pd.Series,
                          close: pd.Series, idx_close: pd.Series,
                          idx_amount: pd.Series,
                          N_cd: int = 60, M_cd: int = 120) -> dict:
    """
    YJD_CompositeCrowding - 综合拥挤度 = F1 + F2 + F3 + F4
    返回结构：today / F1..F4 / ma5 / ma20 / min50 / max50 / status

    阈值（与用户原始 SuperMind 脚本 line 553-560 一致）：
      > 300   → 极度过热
      > 120   → 偏热/拥挤
      -80~120 → 中性
      < -80   → 极度弱势
    """
    f1 = compute_f1_turnover_deviation(stock_turnover, M_cd)
    f2 = compute_f2_price_deviation(close, N_cd, M_cd)
    f3 = compute_f3_relative_strength(close, idx_close, M_cd) if idx_close is not None else np.nan
    f4 = compute_f4_volume_share(stock_amount, idx_amount, M_cd) if idx_amount is not None else np.nan

    yjd_arr = pd.Series([f1, f2, f3, f4], dtype=float)
    yjd_today = float(np.nansum(yjd_arr.values))

    # 用真实 YJD 时间序列计算 ma5/ma20/min50/max50（不是当日的几个分量）
    yjd_series = pd.Series(dtype=float)
    if close is not None and len(close) >= M_cd:
        try:
            f1_s = (stock_turnover - stock_turnover.rolling(M_cd).mean()) \
                   / (stock_turnover.rolling(M_cd).mean() + 1e-9) * 100
            ma_close_n = close.rolling(N_cd).mean()
            f2_s = (close - ma_close_n) / (ma_close_n + 1e-9) * 100
            comp = [f1_s, f2_s]
            if idx_close is not None:
                # 与指数取公共交易日对齐，避免指数缓存滞后 1 天导致末端 NaN
                c_a, ic_a = close.align(idx_close, join='inner')
                XB = c_a / (ic_a + 1e-9)
                ma_xb = XB.rolling(M_cd).mean()
                f3_s = (XB - ma_xb) / (ma_xb + 1e-9) * 100
                comp.append(f3_s)
            if idx_amount is not None:
                sa_a, ia_a = stock_amount.align(idx_amount, join='inner')
                ZB = sa_a / (ia_a + 1e-9)
                ma_zb = ZB.rolling(M_cd).mean()
                f4_s = (ZB - ma_zb) / (ma_zb + 1e-9) * 100
                comp.append(f4_s)
            yjd_series = sum(comp[1:], comp[0]) if len(comp) > 1 else comp[0]
            # 仅去掉 120 日滚动均值预热期的 NaN，保留完整有效历史（供 50 日极值/均线计算）
            yjd_series = yjd_series.dropna()
        except Exception:
            yjd_series = pd.Series(dtype=float)

    ma5 = float(yjd_series.rolling(5).mean().iloc[-1]) if len(yjd_series) >= 5 else np.nan
    ma20 = float(yjd_series.rolling(20).mean().iloc[-1]) if len(yjd_series) >= 20 else np.nan
    min50 = float(yjd_series.rolling(50).min().iloc[-1]) if len(yjd_series) >= 50 else np.nan
    max50 = float(yjd_series.rolling(50).max().iloc[-1]) if len(yjd_series) >= 50 else np.nan

    return {
        'today': yjd_today,
        'F1': f1, 'F2': f2, 'F3': f3, 'F4': f4,
        'ma5': ma5, 'ma20': ma20, 'min50': min50, 'max50': max50,
        'status': _classify_yjd_status(yjd_today),
    }


# ============================================================
# 估值因子（依赖外部传入的估值数据）
# ============================================================
def compute_pe_ttm(valuation: dict) -> float:
    """PE_TTM - 市盈率"""
    v = valuation.get('PE_TTM', np.nan)
    return float(v) if v is not None and not (isinstance(v, str)) else np.nan


def compute_pb(valuation: dict) -> float:
    """PB - 市净率"""
    v = valuation.get('PB', np.nan)
    return float(v) if v is not None and not (isinstance(v, str)) else np.nan


def compute_ps_ttm(valuation: dict) -> float:
    """PS_TTM - 市销率"""
    v = valuation.get('PS_TTM', np.nan)
    return float(v) if v is not None and not (isinstance(v, str)) else np.nan


def compute_log_total_market_value(valuation: dict) -> float:
    """Log_Total_Market_Value - 对数总市值"""
    cap = valuation.get('market_cap', 0)
    if cap and isinstance(cap, (int, float)) and cap > 0:
        return float(np.log(cap))
    return np.nan


# ============================================================
# 盈利预期因子（依赖外部传入的一致预期）
# ============================================================
def compute_consensus_direction(eps_now: float, eps_90d_ago: float) -> str:
    """
    Consensus_Direction - 一致预期方向
    返回: "+1.1 (强烈看多)" / "-1.2 (强烈看空)" / "0 (持平)"
    """
    if pd.isnull(eps_now) or pd.isnull(eps_90d_ago) or eps_90d_ago == 0:
        return "数据缺失"
    if eps_now > eps_90d_ago:
        return "+1.1 (强烈看多)"
    elif eps_now < eps_90d_ago:
        return "-1.2 (强烈看空)"
    return "0 (持平)"


def compute_eps_revision_rate(eps_now: float, eps_90d_ago: float) -> float:
    """EPS_Revision_Rate - 90 天 EPS 预期修正率"""
    if pd.isnull(eps_now) or pd.isnull(eps_90d_ago) or eps_90d_ago == 0:
        return np.nan
    return float((eps_now - eps_90d_ago) / abs(eps_90d_ago))


# ============================================================
# 技术量价因子
# ============================================================
def compute_deviation_from_ma200(close: pd.Series, N: int = 200) -> float:
    """Deviation_From_MA200 - 收盘价相对 MA200 的偏离率"""
    if len(close) < N:
        return np.nan
    ma200 = close.rolling(N).mean().iloc[-1]
    return float((close.iloc[-1] - ma200) / ma200)


def compute_amihud_illiquidity(close: pd.Series, amount: pd.Series, N: int = 20) -> float:
    """Amihud_Illiquidity - 非流动性指标"""
    ret = close.pct_change()
    illiq = (ret.abs() / amount).replace([np.inf, -np.inf], np.nan).rolling(N).mean().iloc[-1]
    return float(illiq * 1e8)  # 标准化


def compute_rsi14(close: pd.Series, N: int = 14) -> float:
    """RSI14 - 14 日 RSI"""
    return float(calc_rsi(close, N).iloc[-1])


def compute_ret_20d(close: pd.Series) -> float:
    """ret_20d - 20 日累计收益率（原脚本 line 552）"""
    if len(close) < 21:
        return np.nan
    return float(close.iloc[-1] / close.iloc[-21] - 1)


def compute_ret_60d(close: pd.Series) -> float:
    """ret_60d - 60 日累计收益率（原脚本 line 553）"""
    if len(close) < 61:
        return np.nan
    return float(close.iloc[-1] / close.iloc[-61] - 1)


def compute_ret_20d_pct_60d(close: pd.Series) -> float:
    """
    ret20_pct_60d - 当前 20 日涨幅在 60 日窗口百分位（原脚本 line 554）
    含义：close.pct_change(20) 最近 60 个值中，< 当前值的占比
    """
    if len(close) < 80:
        return np.nan
    pct20 = close.pct_change(20).dropna()
    if len(pct20) < 60:
        return np.nan
    return float((pct20.iloc[-60:] < pct20.iloc[-1]).mean())


def compute_dde_net_today(fund_flow_data: dict | None) -> float:
    """
    DDE_Net_Today - 当日主力净流入（大单 + 特大单）
    接受腾讯自选股 data_fund_flow 返回的 dict
    单位：亿元（元 / 1e9）
    """
    if not fund_flow_data:
        return np.nan
    huge = fund_flow_data.get('hugeNetInflow', 0) or 0
    big = fund_flow_data.get('bigNetInflow', 0) or 0
    return float((huge + big) / 1e9)


def compute_dde_net_3d(fund_flow_data: dict | None) -> float:
    """
    DDE_Net_3d - 大单净额 3 日累计（dict 口径，来自腾讯自选股字段）
    优先取 fund_flow_data['dde_3d'] 字段，若无则用 hugeNetInflow + bigNetInflow 兜底（仅当日）
    单位：亿元
    """
    if not fund_flow_data:
        return np.nan
    # 优先取预计算的 3 日累计
    dde_3d = fund_flow_data.get('dde_3d')
    if dde_3d is not None:
        return float(dde_3d) / 1e8 if abs(dde_3d) > 1e6 else float(dde_3d)  # 自动判断单位
    # 兜底：用当日
    huge = fund_flow_data.get('hugeNetInflow', 0) or 0
    big = fund_flow_data.get('bigNetInflow', 0) or 0
    return float((huge + big) / 1e9)


def compute_dde_multi_period(dde_series: pd.Series | None,
                             amount_series: pd.Series | None) -> dict:
    """
    DDE 多日累计 + 多日累计净额率（与原脚本 line 446-456 一致）
    返回：{'DDE_1d', 'DDE_3d', 'DDE_5d', 'DDE_10d',
          'Amt_1d', 'Amt_3d', 'Amt_5d', 'Amt_10d',
          'DDE_Rate_1d', 'DDE_Rate_3d', 'DDE_Rate_5d', 'DDE_Rate_10d'}
    单位：DDE 为亿，Amt 为亿，比率为 %
    """
    out = {f'{prefix}_{n}d': np.nan for prefix in ['DDE', 'Amt'] for n in [1, 3, 5, 10]}
    out.update({f'DDE_Rate_{n}d': np.nan for n in [1, 3, 5, 10]})
    if dde_series is None or amount_series is None:
        return out
    if len(dde_series) < 10 or len(amount_series) < 10:
        return out

    # DDE 单位是元，转亿
    dde_in_yi = dde_series / 1e9
    amt_in_yi = amount_series / 1e9

    for n in [1, 3, 5, 10]:
        dde_n = float(dde_in_yi.iloc[-n:].sum())
        amt_n = float(amt_in_yi.iloc[-n:].sum())
        out[f'DDE_{n}d'] = dde_n
        out[f'Amt_{n}d'] = amt_n
        # 累计净额率 = DDE累计 / 成交额累计（百分比）
        out[f'DDE_Rate_{n}d'] = (dde_n / amt_n * 100) if amt_n != 0 else np.nan
    return out


def compute_turnover_percentile_60d(volume: pd.Series, N: int = 60) -> float:
    """Turnover_Percentile_60d - 当前成交量在 60 日内的百分位"""
    if len(volume) < N:
        return np.nan
    return float((volume.iloc[-N:] < volume.iloc[-1]).mean())


def compute_volume_ratio(volume: pd.Series, N: int = 20) -> float:
    """Volume_Ratio - 量比（当前 / N 日均量）"""
    if len(volume) < N:
        return np.nan
    return float(volume.iloc[-1] / volume.rolling(N).mean().iloc[-1])


def compute_macd(close: pd.Series,
                 fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    MACD - 平滑异同移动平均线
    与原脚本 line 458-462 一致：
      DIF = EMA12 - EMA26
      DEA = EMA9(DIF)
      BAR = 2 * (DIF - DEA)
    返回：{'DIF': float, 'DEA': float, 'BAR': float}
    """
    if close is None or len(close) < slow + signal:
        return {'DIF': np.nan, 'DEA': np.nan, 'BAR': np.nan}
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = 2 * (dif - dea)
    return {
        'DIF': float(dif.iloc[-1]),
        'DEA': float(dea.iloc[-1]),
        'BAR': float(bar.iloc[-1]),
    }


def compute_ret20_pct_60d(close: pd.Series, N: int = 20, lookback: int = 60) -> float:
    """
    ret20_pct_60d - 当前 20 日涨幅在 60 日窗口百分位（原脚本 line 554）
    含义：close.pct_change(20) 最近 60 个值中，< 当前值的占比
    """
    if close is None or len(close) < N + lookback:
        return np.nan
    pct20 = close.pct_change(N).dropna()
    if len(pct20) < lookback:
        return np.nan
    last = pct20.iloc[-1]
    past = pct20.iloc[-lookback:]
    return float((past < last).mean())


def compute_atr20_pct(high: pd.Series, low: pd.Series, close: pd.Series,
                      N: int = 20) -> float:
    """
    ATR20_Pct - 百分比口径的 ATR20（与绝对值口径并列输出）
    ATR20_Pct = ATR20 / close * 100
    这样 ATR20 = 6.23 元 + ATR20_Pct = 12.46% 同时显示，
    解决"AI 6.23 vs 用户 31.33"这种纯单位差异的歧义。
    """
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_abs = float(tr.rolling(N).mean().iloc[-1])
    if np.isnan(atr_abs) or close.iloc[-1] == 0:
        return np.nan
    return float(atr_abs / close.iloc[-1] * 100)


def compute_resistance_support(high: pd.Series, low: pd.Series,
                                close: pd.Series, N: int = 60) -> dict:
    """
    Resistance_Support - 压力位 / 支撑位
    原脚本 line 464-468 用 get_resistance_support（无 MCP 替代），
    这里用最近 N 日的最高/最低价近似：
      压力位 = N 日最高价 * 0.98（保守一点）
      支撑位 = N 日最低价 * 1.02
      当前位置 = (close - 支撑) / (压力 - 支撑)
    注：如未来 westock-mcp 提供 RS 数据，应替换为真实数据
    """
    if close is None or high is None or low is None or len(close) < N:
        return {'Resistance': np.nan, 'Support': np.nan, 'Position_Pct': np.nan}
    resistance = float(high.iloc[-N:].max()) * 0.98
    support = float(low.iloc[-N:].min()) * 1.02
    cur = float(close.iloc[-1])
    pos = (cur - support) / (resistance - support) * 100 if resistance != support else np.nan
    return {'Resistance': resistance, 'Support': support, 'Position_Pct': float(pos)}


def compute_net_flow_rate(fund_flow_data: dict) -> float:
    """
    Net_Flow_Rate - 主力资金净流入率（主力净流入 / 成交额）
    接受腾讯自选股 data_fund_flow 的 dict
    """
    if not fund_flow_data:
        return np.nan
    # 字段优先：mainNetInflowRate 已经有现成的
    rate = fund_flow_data.get('mainNetInflowRate')
    if rate is not None:
        return float(rate)
    # 兜底：自己算 = mainNetInflow / amount
    main = fund_flow_data.get('mainNetInflow')
    amount = fund_flow_data.get('amount')
    if main is not None and amount and amount > 0:
        return float(main / amount)
    return np.nan


def compute_composite_chip_quality(chip_data: dict) -> float:
    """
    Composite_Chip_Quality - 筹码结构综合质量
    输入：腾讯自选股 data_chip 返回的 dict，含 profit_ratio / concentration_90 / concentration_70
    输出：综合得分（0-100，越高越好）
      逻辑：集中度 90 + 集中度 70 → 越集中越好；套牢盘比例越低越好；
              加权 = (c90*0.4 + c70*0.4 + profit_ratio*0.2)
    """
    if not chip_data:
        return np.nan
    try:
        c90 = float(chip_data.get('concentration_90', 50))
        c70 = float(chip_data.get('concentration_70', 50))
        profit = float(chip_data.get('profit_ratio', 50))
        # 筹码越集中 + 获利盘越大 → 质量越好
        score = c90 * 0.4 + c70 * 0.4 + profit * 0.2
        return round(float(score), 2)
    except Exception:
        return np.nan


# ============================================================
# 个股归属池检测（9 大宽基）
# ============================================================
BROAD_BASED_INDEX_MAP = {
    '000016.SH': '上证50',
    '000300.SH': '沪深300',
    '000905.SH': '中证500',
    '000852.SH': '中证1000',
    '000688.SH': '科创50',
    '000682.SH': '科创100',
    '399006.SZ': '创业板指',
    '399100.SZ': '深证100',
    '899050.BJ': '北证50',
}

# primary_idx 优先级：沪深300 > 中证500/1000/科创50/100/创业板指/北证50
_PRIMARY_PRIORITY = ['000300.SH', '000905.SH', '000852.SH',
                     '000688.SH', '000682.SH', '399006.SZ', '899050.BJ']


def compute_belonged_pools(stock_code: str,
                           index_pool_map: dict[str, list[str]] | None) -> dict:
    """
    BelongedPools + PrimaryIndex - 个股归属池与基准指数选择

    输入：
      stock_code: '000977.SZ' 形式
      index_pool_map: {idx_code: [member_stock_codes]}，由 report.py 预拉
                       若为 None 则返回空归属，primary_idx 退化为 None

    输出：
      {
        'belonged_pools': ['沪深300', '中证500', ...],
        'primary_idx_code': '000300.SH',
        'primary_idx_name': '沪深300',
      }

    优先级（与原脚本 line 502-509 一致）：
      沪深300 > 中证500/1000/科创50/100/创业板指/北证50
    """
    code_short = stock_code.split('.')[0] if '.' in stock_code else stock_code
    belonged: list[str] = []
    primary_code, primary_name = None, None

    if not index_pool_map:
        return {
            'belonged_pools': belonged,
            'primary_idx_code': None,
            'primary_idx_name': None,
        }

    for idx_code, idx_name in BROAD_BASED_INDEX_MAP.items():
        members = index_pool_map.get(idx_code) or []
        if any(m.split('.')[0] == code_short for m in members):
            belonged.append(idx_name)
            if primary_code is None and idx_code == '000300.SH':
                primary_code, primary_name = idx_code, idx_name

    # 如果 primary 还没确定（不在沪深300），按优先级往下找
    if primary_code is None:
        for cand in _PRIMARY_PRIORITY:
            if BROAD_BASED_INDEX_MAP[cand] in belonged:
                primary_code = cand
                primary_name = BROAD_BASED_INDEX_MAP[cand]
                break

    # 微盘/概念股：未归任何宽基
    if not belonged:
        belonged = ['宽基外(微盘/概念股)']

    return {
        'belonged_pools': belonged,
        'primary_idx_code': primary_code,
        'primary_idx_name': primary_name,
    }


# ============================================================
# 多周期相对强弱
# ============================================================
def _eval_relative_strength(excess: float) -> str:
    """
    强弱评估（与原脚本 line 287-297 一致）：
      > +5%   → 强势大幅跑赢
      > +1%   → 跑赢大盘
      ±1% 内  → 基本同步
      -1%~-5% → 稍微跑输
      < -5%   → 弱势大幅跑输
    """
    if excess is None or np.isnan(excess):
        return "数据缺失"
    if excess > 0.05:
        return "🚀 强势大幅跑赢"
    if excess > 0.01:
        return "✅ 跑赢大盘"
    if excess > -0.01:
        return "➡️ 基本同步"
    if excess > -0.05:
        return "⚠️ 稍微跑输"
    return "❌ 弱势大幅跑输"


def compute_relative_strength_multi_period(
    stock_close: pd.Series,
    idx_close: pd.Series,
    periods: list[int] | None = None,
) -> list[dict]:
    """
    RelativeStrength_MultiPeriod - 多周期相对强弱
    周期默认 [1, 5, 14, 30, 60]（与原脚本 line 264 一致）

    返回：[{period, stock_ret, idx_ret, excess, eval}, ...]
    """
    if periods is None:
        periods = [1, 5, 14, 30, 60]
    if stock_close is None or idx_close is None or len(stock_close) == 0 or len(idx_close) == 0:
        return []

    out = []
    for p in periods:
        if len(stock_close) > p and len(idx_close) > p:
            s_ret = float(stock_close.iloc[-1] / stock_close.iloc[-1 - p] - 1)
            i_ret = float(idx_close.iloc[-1] / idx_close.iloc[-1 - p] - 1)
            excess = s_ret - i_ret
            out.append({
                'period': p,
                'stock_ret': s_ret,
                'idx_ret': i_ret,
                'excess': excess,
                'eval': _eval_relative_strength(excess),
            })
    return out
def _require_section_df(section_df: pd.DataFrame, factor_name: str):
    if section_df is None or section_df.empty:
        return None
    return section_df


def compute_alpha21_section(section_close: pd.DataFrame) -> pd.Series | None:
    """Alpha21 - 趋势持续性（截面）"""
    if section_close is None:
        return None
    returns = section_close.pct_change()
    val = cs_rank(section_close.rolling(5).mean() / section_close.rolling(20).mean()) \
        - cs_rank(returns.rolling(20).std())
    return val.iloc[-1]


def compute_alpha35_section(section_close: pd.DataFrame, section_volume: pd.DataFrame,
                              stock_code: str | None = None) -> pd.Series | float | None:
    """Alpha35 - 量价确认（截面）
    返回 pd.Series（截面全部股）或 float（指定 stock_code 时取该股）
    """
    if section_close is None or section_volume is None:
        return None
    ret_c = section_close.pct_change(5)
    ret_v = section_volume.pct_change(5)
    # pandas 3.0 滚动 corr 跨 DataFrame 行为变化，用 apply 逐列
    corr_df = pd.DataFrame(index=ret_c.index, columns=ret_c.columns, dtype=float)
    for col in ret_c.columns:
        corr_df[col] = ret_c[col].rolling(10).corr(ret_v[col])
    val = cs_rank(corr_df)
    last = val.iloc[-1].dropna()
    if stock_code is not None:
        # stock_code 可能是 '000977' 或 '000977.SZ'
        for key in [stock_code, stock_code.split('.')[0] if '.' in stock_code else stock_code]:
            if key in last.index:
                return float(last[key])
        return np.nan
    return last


def compute_alpha39(stock_close: pd.Series, stock_amount: pd.Series, N: int = 10) -> float:
    """Alpha39 - 资金推动（单股可算，作为截面版的兜底）"""
    val = stock_close.pct_change(N) / stock_amount.rolling(N).mean()
    return float(val.iloc[-1])


def compute_alpha39_section(section_close: pd.DataFrame,
                              section_amount: pd.DataFrame, N: int = 10) -> pd.Series | None:
    """
    Alpha39 - 资金推动（截面）
    原脚本 line 386: alpha39_raw = cs_rank(safe_divide(CLOSE.pct_change(10), MONEY.rolling(10).mean()))
    """
    if section_close is None or section_amount is None:
        return None
    val = cs_rank(safe_divide(section_close.pct_change(N), section_amount.rolling(N).mean()))
    return val.iloc[-1]


def compute_alpha47_section(section_low: pd.DataFrame, section_high: pd.DataFrame,
                             section_close: pd.DataFrame) -> pd.Series | None:
    """Alpha47 - 超买超卖（截面）"""
    if section_low is None:
        return None
    lowest_5 = section_low.rolling(5).min()
    val = -1 * cs_rank(safe_divide(section_close - lowest_5,
                                   section_high.rolling(5).max() - lowest_5))
    return val.iloc[-1]


def _ts_argmax(series: pd.Series, window: int = 30) -> pd.Series:
    """
    ts_argmax - 过去 window 天最大值出现在第几天（0-based）
    返回值越大 = 越靠后（即越近期创新高）
    原论文: ts_argmax(close, 30)
    """
    # argmax 返回 0-based 索引；最大越靠后，索引越大
    # 但 WorldQuant 习惯是位置越靠后 = 值越大（直觉：recent break out）
    return series.rolling(window).apply(lambda x: int(np.argmax(x)), raw=True)


def _decay_linear(df_in: pd.DataFrame, d: int = 2) -> pd.DataFrame:
    """
    decay_linear - d 日线性衰减，最近权重最大
    原论文: decay_linear(x, d) = (d*x_t + (d-1)*x_{t-1} + ... + 1*x_{t-d+1}) / (d + (d-1) + ... + 1)
    即权重 = [d, d-1, ..., 1]，归一化分母 = d*(d+1)/2
    返回 DataFrame，shape 与输入一致；逐列判断 NaN（避免一行 NaN 拖垮整行）
    """
    weights = np.arange(d, 0, -1, dtype=float) / (d * (d + 1) / 2)
    out = pd.DataFrame(index=df_in.index, columns=df_in.columns, dtype=float)
    arr = df_in.values  # shape (T, N)
    T, N = arr.shape
    out_arr = np.full((T, N), np.nan)
    for i in range(d - 1, T):
        # 最近 d 行：i-d+1 ~ i，列方向
        window = arr[i - d + 1: i + 1, :][::-1]  # shape (d, N)
        # 逐列判断：当前列没有 NaN 才算
        valid_col = ~np.any(np.isnan(window), axis=0)
        # 计算并赋值（NaN 位置保持 NaN）
        with np.errstate(invalid='ignore'):
            row_result = np.where(valid_col, np.dot(weights, window), np.nan)
        out_arr[i] = row_result
    out = pd.DataFrame(out_arr, index=df_in.index, columns=df_in.columns)
    return out


def compute_alpha57_section(section_close: pd.DataFrame,
                              section_vwap: pd.DataFrame | None = None,
                              full: bool = True) -> pd.Series | None:
    """
    Alpha57 - WorldQuant 101 Formulaic Alphas 第 57 号因子

    完整公式（WorldQuant 论文）：
        Alpha57 = 0 - 1 * ((close - vwap) / decay_linear(rank(ts_argmax(close, 30)), 2))

    含义（用户已确认）：
      - (close - vwap)：价格相对市场平均成本的偏离
      - ts_argmax(close, 30)：过去 30 天最高价出现的位置（越靠后 = 越近期新高）
      - rank(...) + decay_linear(..., 2)：把 30 日新高位置做截面 rank，再 2 日线性衰减
      - 整个公式加负号 = 反转方向：高位滞涨 → 空头信号；超跌 → 多头信号

    简化版（与原 SuperMind 脚本 line 411-415 一致）：
        Alpha57_simple = cs_rank((close - vwap) / vwap)
        仅用 VWAP 偏离，没有 ts_argmax 动量过滤 + decay 衰减 + 反转符号
    """
    if section_close is None:
        return None

    if not full or section_vwap is None:
        # 简化版（与原 SuperMind 脚本 line 411-415 一致）：
        #   Alpha57_simple = cs_rank((close - vwap) / vwap)
        if section_vwap is None:
            # 没有 vwap 数据时退回为 close 的截面 rank
            val = cs_rank(section_close)
        else:
            val = cs_rank(safe_divide(section_close - section_vwap, section_vwap))
        return val.iloc[-1]

    # 完整版
    # Step 1: ts_argmax(close, 30) → 截面每列算 30 日 argmax
    ts_argmax_df = section_close.apply(lambda col: _ts_argmax(col, 30))

    # Step 2: rank(ts_argmax) → 截面 rank（每日横截面）
    rank_argmax = ts_argmax_df.rank(axis=1, pct=True)

    # Step 3: decay_linear(rank_argmax, 2) → 2 日线性衰减（最近权重 = 2/3）
    decay = _decay_linear(rank_argmax, d=2)

    # Step 4: (close - vwap) / decay
    raw = safe_divide(section_close - section_vwap, decay)

    # Step 5: 加负号反转
    alpha57_raw = -1 * raw

    # Step 6: 截面 rank（与世界其他截面 Alpha 一致，方便横向比较）
    alpha57 = cs_rank(alpha57_raw)

    return alpha57.iloc[-1]


def compute_alpha70(stock_amount: pd.Series, N: int = 20) -> float:
    """Alpha70 - 资金躁动（单股可算）"""
    return float((stock_amount.rolling(N).std() / stock_amount.rolling(N).mean()).iloc[-1])


def compute_alpha102(stock_volume: pd.Series, N: int = 14) -> float:
    """
    Alpha102_量能RSI14 - 对成交量计算 RSI
    与原脚本 line 374, 386 一致：vol_rsi = calc_rsi(VOLUME, 14)，取个股最后一期
    单股可算，不依赖截面。
    """
    if stock_volume is None or len(stock_volume.dropna()) < N:
        return np.nan
    rsi_series = calc_rsi(stock_volume, N)
    return float(rsi_series.iloc[-1])


def compute_alpha102_section(section_volume: pd.DataFrame, N: int = 14) -> pd.Series | None:
    """
    Alpha102_量能RSI14 截面版（截面内做 cs_rank）
    与单股版的区别：先对截面每列算 RSI14，再做截面 rank
    """
    if section_volume is None or section_volume.empty:
        return None
    # 对每只股算 RSI
    rsi_per_stock = section_volume.apply(lambda col: calc_rsi(col, N).iloc[-1] if col.notna().sum() >= N else np.nan)
    if rsi_per_stock.isna().all():
        return None
    # 截面 rank
    return rsi_per_stock.rank(pct=True)


def compute_alpha83(stock_high: pd.Series, stock_volume: pd.Series, N: int = 20) -> dict:
    """Alpha83 - 高价量背离（单股时序版）"""
    high_pct = stock_high.pct_change()
    high_delay1_pct = stock_high.shift(1).pct_change()
    vol_ratio = stock_volume / stock_volume.rolling(N).mean()
    val = (high_delay1_pct - high_pct) / (vol_ratio + 1e-9)
    ts_rank = float(ts_rank_pct(val, 252).iloc[-1]) if len(val) >= 252 else np.nan
    return {'raw': float(val.iloc[-1]), 'ts_rank': ts_rank}


def compute_alpha83_section(section_high: pd.DataFrame, section_volume: pd.DataFrame,
                              N: int = 20) -> pd.Series | None:
    """
    Alpha83 - 高价量背离（截面版）
    原脚本 line 538-540:
      alpha83_raw = (cs_rank(high_delay1_pct) - cs_rank(high_pct)) / (cs_rank(vol_ratio) + 1e-9)
    """
    if section_high is None or section_volume is None:
        return None
    high_pct = section_high.pct_change()
    high_delay1_pct = section_high.shift(1).pct_change()
    vol_ratio = section_volume / section_volume.rolling(N).mean()
    val = (cs_rank(high_delay1_pct) - cs_rank(high_pct)) / (cs_rank(vol_ratio) + 1e-9)
    return val.iloc[-1]


def compute_alpha84_section(section_close: pd.DataFrame, N: int = 20) -> pd.Series | None:
    """Alpha84 - 波动中强弱（截面）"""
    if section_close is None:
        return None
    returns = section_close.pct_change()
    val = cs_rank(safe_divide(section_close.pct_change(N), returns.rolling(N).std()))
    return val.iloc[-1]


def compute_alpha99(stock_close: pd.Series, stock_volume: pd.Series, N: int = 20) -> dict:
    """Alpha99 - 收盘量背离（单股时序版）"""
    close_pct = stock_close.pct_change()
    close_delay1_pct = stock_close.shift(1).pct_change()
    vol_ratio = stock_volume / stock_volume.rolling(N).mean()
    val = (close_delay1_pct - close_pct) / (vol_ratio + 1e-9)
    ts_rank = float(ts_rank_pct(val, 252).iloc[-1]) if len(val) >= 252 else np.nan
    return {'raw': float(val.iloc[-1]), 'ts_rank': ts_rank}


def compute_alpha99_section(section_close: pd.DataFrame, section_volume: pd.DataFrame,
                              N: int = 20) -> pd.Series | None:
    """
    Alpha99 - 收盘量背离（截面版）
    原脚本 line 543-544:
      alpha99_raw = (cs_rank(close_delay1_pct) - cs_rank(close_pct)) / (cs_rank(vol_ratio) + 1e-9)
    """
    if section_close is None or section_volume is None:
        return None
    close_pct = section_close.pct_change()
    close_delay1_pct = section_close.shift(1).pct_change()
    vol_ratio = section_volume / section_volume.rolling(N).mean()
    val = (cs_rank(close_delay1_pct) - cs_rank(close_pct)) / (cs_rank(vol_ratio) + 1e-9)
    return val.iloc[-1]


def compute_alpha101_section(section_open: pd.DataFrame, section_high: pd.DataFrame,
                             section_low: pd.DataFrame, section_close: pd.DataFrame) -> pd.Series | None:
    """Alpha101 - 日内动量（截面）"""
    if section_close is None:
        return None
    val = cs_rank(safe_divide(section_close - section_open, section_high - section_low))
    return val.iloc[-1]


def compute_alpha176_section(section_close: pd.DataFrame, section_high: pd.DataFrame,
                              section_low: pd.DataFrame, section_volume: pd.DataFrame,
                              N: int = 20) -> pd.Series | None:
    """Alpha176 - 量价共振（截面）"""
    if section_close is None:
        return None
    price_pos = ((section_close - section_low.rolling(N).min())
                 / (section_high.rolling(N).max() - section_low.rolling(N).min() + 1e-9))
    vol_ratio = section_volume / section_volume.rolling(N).mean()
    raw = cs_rank(price_pos) * cs_rank(vol_ratio)
    val = cs_rank(raw.rolling(5).mean())
    return val.iloc[-1]


# ============================================================
# 复合因子（中性化 / 残差 / Beta）
# ============================================================
def compute_stock_beta_60d(stock_ret: pd.Series, idx_ret: pd.Series) -> float:
    """Stock_Beta_60d - 个股 60 日 Beta"""
    aligned = pd.concat([stock_ret, idx_ret], axis=1, join='inner').dropna()
    aligned.columns = ['stock', 'index']
    if len(aligned) < 30:
        return 1.0
    slope, _, _, _, _ = linregress(aligned['index'], aligned['stock'])
    return float(slope)


def compute_residual_momentum_20d(stock_ret: pd.Series, idx_ret: pd.Series,
                                   beta: float, N: int = 20) -> float:
    """Residual_Momentum_20d - 残差动量（剔除大盘后）"""
    aligned = pd.concat([stock_ret, idx_ret], axis=1, join='inner').dropna()
    aligned.columns = ['stock', 'index']
    if len(aligned) < N:
        return np.nan
    residual = aligned['stock'] - beta * aligned['index']
    return float(residual.tail(N).sum())


def compute_residual_volatility_20d(stock_ret: pd.Series, idx_ret: pd.Series,
                                     beta: float, N: int = 20) -> float:
    """Residual_Volatility_20d - 残差波动率"""
    aligned = pd.concat([stock_ret, idx_ret], axis=1, join='inner').dropna()
    aligned.columns = ['stock', 'index']
    if len(aligned) < N:
        return np.nan
    residual = aligned['stock'] - beta * aligned['index']
    return float(residual.tail(N).std())


def compute_beta_neutral_alpha(stock_ret: pd.Series, idx_ret: pd.Series,
                                beta: float) -> float:
    """Beta_Neutral_Alpha - 今日 Beta 中性后纯 Alpha 收益"""
    aligned = pd.concat([stock_ret, idx_ret], axis=1, join='inner').dropna()
    aligned.columns = ['stock', 'index']
    if len(aligned) < 1:
        return np.nan
    return float(aligned['stock'].iloc[-1] - beta * aligned['index'].iloc[-1])


def compute_industry_neutral_alpha(stock_ret: pd.Series, industry_ret: pd.Series,
                                     N: int = 20) -> float:
    """
    Industry_Neutral_Alpha - 行业中性后超额
    原脚本仅用 DeltaRatio 占位（不准确），迁移版基于真实行业等权收益计算。
    """
    aligned = pd.concat([stock_ret, industry_ret], axis=1, join='inner').dropna()
    aligned.columns = ['stock', 'industry']
    if len(aligned) < N:
        return np.nan
    return float((aligned['stock'] - aligned['industry']).tail(N).sum())


# ============================================================
# 智能注释（用于报告输出）
# ============================================================
def annotate(category: str, key: str, value) -> str:
    """为因子值生成可读的中文注释（取自原脚本 get_annotation）"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    try:
        if category == 'advanced':
            if key == 'Residual_Momentum_20d':
                if value > 0.05:   return "  >>> [偏多] 剔除大盘后独立上涨趋势强"
                elif value < -0.05: return "  >>> [偏空] 剔除大盘后独立下跌，个股特质动量疲软"
                return "  >>> [中性] 独立走势不明朗"
            if key == 'Residual_Volatility_20d':
                if value > 0.04:   return "  >>> [偏空] 特质波动较大，资金分歧明显"
                return "  >>> [中性/偏多] 特质波动收敛，主力控盘稳定"
            if key == 'Beta_Neutral_Alpha':
                if value > 0: return "  >>> [偏多] 今日存在正向纯 Alpha 收益"
                return "  >>> [偏空] 今日存在负向纯 Alpha 收益"
            if key == 'Industry_Neutral_Alpha':
                if value > 0.05:   return "  >>> [强烈偏多] 显著跑赢同行业（历史参考价值不高）"
                elif value < -0.05: return "  >>> [强烈偏空] 显著跑输同行业，行业垫底"
                return "  >>> [中性] 与行业平均水平持平"
            if key == 'Stock_Beta_60d':
                if value > 1.2:   return "  >>> [进攻型] 波动大于大盘，牛市弹性高（历史参考价值不高）"
                elif value < 0.8: return "  >>> [防守型] 波动小于大盘，抗跌性强"
                return "  >>> [中性] 与大盘同步"

        elif category == 'volatility':
            if key == 'ATR20_平均真实波幅':
                return "  >>> [风控提示] 日均绝对波动幅度参考值"
            if value > 0.05:   return "  >>> [偏空] 波动率过高，情绪亢进或有抛压"
            elif value < 0.025: return "  >>> [偏多] 波动率极低，处于变盘临界点"
            return "  >>> [中性] 波动率处于正常区间"

        elif category == 'wq_alpha':
            if key == 'Alpha47_OverboughtOversold':
                if value > 0.7: return "  >>> [看多] 短期超卖严重，存在反弹动能"
                elif value < 0.3: return "  >>> [看空] 短期超买严重，存在回落风险"
                return "  >>> [中性] 价格处于中位区间"
            if key == 'Alpha57_VWAPDeviation':
                if value > 0.7: return "  >>> [偏多] 收盘强势站上均价线，多头控盘"
                elif value < 0.3: return "  >>> [偏空] 收盘价低于均价线，空头压制"
                return "  >>> [中性] 围绕均价线震荡"
            if value > 0.6:   return "  >>> [偏多] 截面排名靠前，动能强劲"
            elif value < 0.4: return "  >>> [偏空] 截面排名靠后，动能疲软"
            return "  >>> [中性] 截面排名中游"
    except Exception:
        return ""
    return ""


# ============================================================
# 因子-类别映射
# ============================================================
FACTOR_CATEGORY = {
    "归属池与相对强弱": ["BelongedPools", "PrimaryIndexCode", "PrimaryIndexName", "RelativeStrength"],
    "波动率": ["ATR20", "ATR20_Pct", "Parkinson_Vol", "GarmanKlass_Vol", "YangZhang_Vol", "Realized_Vol_20d"],
    "技术量价": ["Deviation_From_MA200", "Amihud_Illiquidity", "RSI14", "DDE_Net_3d",
                "Turnover_Percentile_60d", "Volume_Ratio", "Net_Flow_Rate",
                "Composite_Chip_Quality", "Ret20_Pct_60d", "Ret_20d", "Ret_60d",
                "Resistance", "Support", "RS_Position_Pct",
                "MACD_DIF", "MACD_DEA", "MACD_BAR"],
    "DDE多日累计": ["DDE_1d", "DDE_3d", "DDE_5d", "DDE_10d",
                  "Amt_1d", "Amt_3d", "Amt_5d", "Amt_10d",
                  "DDE_Rate_1d", "DDE_Rate_3d", "DDE_Rate_5d", "DDE_Rate_10d"],
    "拥挤度": ["F1_TurnoverDeviation", "F2_PriceDeviation", "F3_RelativeStrength",
              "F4_VolumeShare", "YJD_CompositeCrowding", "YJD_MA5", "YJD_MA20",
              "YJD_Min50", "YJD_Max50", "YJD_Status"],
    "估值": ["PE_TTM", "PB", "PS_TTM", "Log_Total_Market_Value"],
    "盈利预期": ["Consensus_Direction", "EPS_Revision_Rate", "Consensus_Expectation_Dir"],
    "Alpha101": ["Alpha21", "Alpha35", "Alpha39", "Alpha39_section", "Alpha47",
                "Alpha57", "Alpha57_simple", "Alpha70", "Alpha83", "Alpha83_section",
                "Alpha84", "Alpha99", "Alpha99_section", "Alpha101",
                "Alpha102", "Alpha102_section", "Alpha176"],
    "复合因子": ["Industry_Neutral_Alpha", "Residual_Momentum_20d", "Residual_Volatility_20d",
                "Beta_Neutral_Alpha", "Stock_Beta_60d"],
}


# ============================================================
# 主入口
# ============================================================
def compute_all_factors(
    stock_code: str | None = None,
    stock_df: pd.DataFrame | None = None,
    idx_df: pd.DataFrame | None = None,
    primary_idx_df: pd.DataFrame | None = None,
    section_close: pd.DataFrame | None = None,
    section_open: pd.DataFrame | None = None,
    section_high: pd.DataFrame | None = None,
    section_low: pd.DataFrame | None = None,
    section_volume: pd.DataFrame | None = None,
    section_amount: pd.DataFrame | None = None,
    section_vwap: pd.DataFrame | None = None,
    valuation: dict | None = None,
    chip_data: dict | None = None,
    consensus_data: dict | None = None,
    fund_flow: dict | None = None,
    fund_flow_series: pd.Series | None = None,
    stock_turnover: pd.Series | None = None,
    index_pool_map: dict[str, list[str]] | None = None,
) -> dict:
    """
    一站式算 41 个因子（+ 归属池 / 多周期相对强弱 / YJD 时序状态）

    Parameters
    ----------
    stock_code : '000977.SZ' 形式，用于 compute_belonged_pools
    stock_df : 个股日线 DataFrame
        columns = ['open', 'high', 'low', 'close', 'volume', 'amount']
        index = DatetimeIndex（按日期升序）
    idx_df : 基准指数日线（同结构），用于 F3/F4/Beta
    section_* : 截面 DataFrame（行为日期，列为股票），用于 Alpha21/35/47/57/84/101/176/102
        若不传，对应因子返回 None
    valuation : dict，含 PE_TTM / PB / PS_TTM / market_cap
    chip_data : dict，腾讯自选股 data_chip 返回
    consensus_data : dict，腾讯自选股 data_consensus 返回
    fund_flow : dict，含 net_flow_rate 等
    stock_turnover : pd.Series，换手率（可选；用于 F1/YJD）
    index_pool_map : {idx_code: [member_stock_codes]}，用于 compute_belonged_pools

    Returns
    -------
    dict: {因子名: 数值 or 子dict}
    """
    close = stock_df['close']
    open_ = stock_df['open']
    high = stock_df['high']
    low = stock_df['low']
    volume = stock_df['volume']
    amount = stock_df.get('amount', (close * volume))

    idx_close = idx_df['close'] if idx_df is not None else None
    idx_amount = idx_df.get('amount') if idx_df is not None else None

    # section_vwap 兜底（如果没传，从 section_amount/section_volume 算）
    if section_vwap is None and section_amount is not None and section_volume is not None:
        section_vwap = section_amount / (section_volume + 1e-9)

    if stock_turnover is None:
        if 'HSL' in stock_df.columns:
            stock_turnover = stock_df['HSL']
        else:
            # 无换手率(HSL)时，用成交量 volume 作代理（与当日 F1 兜底一致）
            stock_turnover = volume

    factors = {}

    # ── 0. 个股归属池（新增） ──
    if stock_code:
        pool_info = compute_belonged_pools(stock_code, index_pool_map)
        factors['BelongedPools'] = pool_info['belonged_pools']
        factors['PrimaryIndexCode'] = pool_info['primary_idx_code']
        factors['PrimaryIndexName'] = pool_info['primary_idx_name']

        # ── 0.5 多周期相对强弱（新增）──
        # 用 primary_idx_df 作为对标基准（不是默认 idx_df=沪深300）
        primary_close = None
        if primary_idx_df is not None and 'close' in primary_idx_df.columns:
            primary_close = primary_idx_df['close']
        elif idx_close is not None:
            primary_close = idx_close  # 兜底
        if primary_close is not None:
            rs_list = compute_relative_strength_multi_period(close, primary_close)
            factors['RelativeStrength'] = rs_list
        else:
            factors['RelativeStrength'] = []
    else:
        factors['BelongedPools'] = []
        factors['PrimaryIndexCode'] = None
        factors['PrimaryIndexName'] = None
        factors['RelativeStrength'] = []

    # ── 1. 波动率（5 个） ──
    factors['ATR20'] = compute_atr20(high, low, close)
    factors['Parkinson_Vol'] = compute_parkinson_vol(high, low)
    factors['GarmanKlass_Vol'] = compute_garman_klass_vol(high, low, close, open_)
    factors['YangZhang_Vol'] = compute_yang_zhang_vol(high, low, close, open_)
    factors['Realized_Vol_20d'] = compute_realized_vol_20d(close)

    # ── 2. 技术量价（14 个，含 v1.6 新增 DDE_Net_Today） ──
    factors['Deviation_From_MA200'] = compute_deviation_from_ma200(close)
    factors['Amihud_Illiquidity'] = compute_amihud_illiquidity(close, amount)
    factors['RSI14'] = compute_rsi14(close)
    factors['DDE_Net_Today'] = compute_dde_net_today(fund_flow) if fund_flow else np.nan  # 当日主力净流入（亿）
    factors['DDE_Net_3d'] = compute_dde_net_3d(fund_flow) if fund_flow else np.nan       # 3 日累计（亿）
    factors['Turnover_Percentile_60d'] = compute_turnover_percentile_60d(volume)
    factors['Volume_Ratio'] = compute_volume_ratio(volume)
    factors['Net_Flow_Rate'] = compute_net_flow_rate(fund_flow) if fund_flow else np.nan
    factors['Composite_Chip_Quality'] = compute_composite_chip_quality(chip_data)

    # v1.3 新增：MACD + ret20_pct_60d + ATR20 双口径
    macd = compute_macd(close)
    factors['MACD_DIF'] = macd['DIF']
    factors['MACD_DEA'] = macd['DEA']
    factors['MACD_BAR'] = macd['BAR']
    factors['Ret20_Pct_60d'] = compute_ret20_pct_60d(close)
    factors['ATR20_Pct'] = compute_atr20_pct(high, low, close)

    # v1.3 新增：DDE 多日累计（需要 fund_flow_series）
    if fund_flow_series is not None and amount is not None:
        dde_multi = compute_dde_multi_period(fund_flow_series, amount)
        factors.update(dde_multi)
    else:
        for n in [1, 3, 5, 10]:
            factors[f'DDE_{n}d'] = np.nan
            factors[f'Amt_{n}d'] = np.nan
            factors[f'DDE_Rate_{n}d'] = np.nan

    # v1.3 新增：压力位 / 支撑位
    rs = compute_resistance_support(high, low, close)
    factors['Resistance'] = rs['Resistance']
    factors['Support'] = rs['Support']
    factors['RS_Position_Pct'] = rs['Position_Pct']

    # ── 3. 拥挤度（5 个 + YJD 时序字段） ──
    if 'HSL' in stock_df.columns and stock_df['HSL'].notna().any():
        factors['F1_TurnoverDeviation'] = compute_f1_turnover_deviation(stock_df['HSL'])
    else:
        factors['F1_TurnoverDeviation'] = compute_f1_turnover_deviation(volume)
    factors['F2_PriceDeviation'] = compute_f2_price_deviation(close)
    factors['F3_RelativeStrength'] = compute_f3_relative_strength(close, idx_close)
    factors['F4_VolumeShare'] = compute_f4_volume_share(amount, idx_amount)
    yjd_dict = compute_yjd_composite(stock_turnover, amount, close, idx_close, idx_amount)
    factors['YJD_CompositeCrowding'] = yjd_dict['today']
    factors['YJD_MA5'] = yjd_dict['ma5']
    factors['YJD_MA20'] = yjd_dict['ma20']
    factors['YJD_Min50'] = yjd_dict['min50']
    factors['YJD_Max50'] = yjd_dict['max50']
    factors['YJD_Status'] = yjd_dict['status']

    # ── 4. 估值（4 个） ──
    val = valuation or {}
    factors['PE_TTM'] = compute_pe_ttm(val)
    factors['PB'] = compute_pb(val)
    factors['PS_TTM'] = compute_ps_ttm(val)
    factors['Log_Total_Market_Value'] = compute_log_total_market_value(val)

    # ── 5. 盈利预期（2 个） ──
    con = consensus_data or {}
    eps_now = con.get('eps_now', np.nan)
    eps_90d = con.get('eps_90d_ago', np.nan)
    factors['Consensus_Direction'] = compute_consensus_direction(eps_now, eps_90d)
    factors['EPS_Revision_Rate'] = compute_eps_revision_rate(eps_now, eps_90d)
    factors['Consensus_Expectation_Dir'] = factors['Consensus_Direction']

    # ── 6. Alpha101（13 个，新增 Alpha39 截面 + Alpha83/99 截面 + Alpha102） ──
    factors['Alpha39'] = compute_alpha39(close, amount)
    factors['Alpha70'] = compute_alpha70(amount)
    factors['Alpha102'] = compute_alpha102(volume)  # 单股量能 RSI14（新增）

    alpha83 = compute_alpha83(high, volume)
    factors['Alpha83_raw'] = alpha83['raw']
    factors['Alpha83_ts_rank'] = alpha83['ts_rank']

    alpha99 = compute_alpha99(close, volume)
    factors['Alpha99_raw'] = alpha99['raw']
    factors['Alpha99_ts_rank'] = alpha99['ts_rank']

    # ret_20d / ret_60d（原脚本 line 552-553）
    factors['Ret_20d'] = compute_ret_20d(close)
    factors['Ret_60d'] = compute_ret_60d(close)

    # 截面因子（返回个股截面分位；截面列为 code 形式如 '000977'）
    # stock_code 短码（如 '000977'）用于从截面取个股值
    short_code = stock_code.split('.')[0] if stock_code and '.' in stock_code else stock_code
    factors['Alpha21'] = _pick_stock(compute_alpha21_section(section_close), short_code)
    factors['Alpha35'] = _pick_stock(compute_alpha35_section(section_close, section_volume), short_code)
    factors['Alpha39_section'] = _pick_stock(compute_alpha39_section(section_close, section_amount), short_code)  # 原脚本 cs_rank 截面版
    factors['Alpha47'] = _pick_stock(compute_alpha47_section(section_low, section_high, section_close), short_code)
    factors['Alpha57'] = _pick_stock(compute_alpha57_section(section_close, section_vwap, full=True), short_code)
    factors['Alpha57_simple'] = _pick_stock(compute_alpha57_section(section_close, section_vwap, full=False), short_code)  # 与原 SuperMind 脚本一致
    factors['Alpha83_section'] = _pick_stock(compute_alpha83_section(section_high, section_volume), short_code)  # 原脚本 cs 截面版
    factors['Alpha84'] = _pick_stock(compute_alpha84_section(section_close), short_code)
    factors['Alpha99_section'] = _pick_stock(compute_alpha99_section(section_close, section_volume), short_code)  # 原脚本 cs 截面版
    factors['Alpha101'] = _pick_stock(compute_alpha101_section(section_open, section_high, section_low, section_close), short_code)
    factors['Alpha176'] = _pick_stock(compute_alpha176_section(section_close, section_high, section_low, section_volume), short_code)
    factors['Alpha102_section'] = _pick_stock(compute_alpha102_section(section_volume), short_code)

    # ── 7. 复合因子（5 个） ──
    if idx_close is not None:
        stock_ret = close.pct_change().dropna()
        idx_ret = idx_close.pct_change().dropna()
        beta = compute_stock_beta_60d(stock_ret, idx_ret)
        factors['Stock_Beta_60d'] = beta
        factors['Residual_Momentum_20d'] = compute_residual_momentum_20d(stock_ret, idx_ret, beta)
        factors['Residual_Volatility_20d'] = compute_residual_volatility_20d(stock_ret, idx_ret, beta)
        factors['Beta_Neutral_Alpha'] = compute_beta_neutral_alpha(stock_ret, idx_ret, beta)
    else:
        for k in ['Stock_Beta_60d', 'Residual_Momentum_20d', 'Residual_Volatility_20d', 'Beta_Neutral_Alpha']:
            factors[k] = np.nan

    if idx_close is not None:
        stock_ret = close.pct_change().dropna()
        industry_ret = idx_close.pct_change().dropna()
        factors['Industry_Neutral_Alpha'] = compute_industry_neutral_alpha(stock_ret, industry_ret)
    else:
        factors['Industry_Neutral_Alpha'] = np.nan

    return factors


# ============================================================
# 报告格式化
# ============================================================
def format_factor_report(factors: dict, with_annotation: bool = True) -> str:
    """输出分层因子报告字符串"""
    sep = "=" * 70
    lines = [sep, "  量化因子报告 (Bayesian Quant Decision v1.1 — 增强版)", sep]

    # ── 0. 个股归属池 ──
    pools = factors.get('BelongedPools') or []
    primary_name = factors.get('PrimaryIndexName')
    if pools or primary_name:
        lines.append("\n[个股归属池与对标基准]")
        lines.append(f"    归属指数池: {', '.join(pools) if pools else '宽基外(微盘/概念股)'}")
        lines.append(f"    对标基准  : {primary_name or '未识别（建议手动指定）'}")

        # ── 0.5 多周期相对强弱 ──
        rs = factors.get('RelativeStrength') or []
        if rs:
            lines.append("\n  多周期相对强弱（个股 vs 对标基准）")
            lines.append("    {:<6} {:>12} {:>12} {:>12}  {}".format(
                "周期", "个股涨跌", "指数涨跌", "超额收益", "强弱评估"))
            for item in rs:
                lines.append("    {:<6} {:>11.2%} {:>11.2%} {:>11.2%}  {}".format(
                    f"{item['period']}日",
                    item['stock_ret'], item['idx_ret'], item['excess'],
                    item['eval']))

    # 拥挤度
    lines.append("\n[拥挤度因子]")
    for k in ['F1_TurnoverDeviation', 'F2_PriceDeviation', 'F3_RelativeStrength',
              'F4_VolumeShare', 'YJD_CompositeCrowding']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            lines.append(f"    {k:30s}: {v:>10.4f}")

    # YJD 时序字段
    yjd_status = factors.get('YJD_Status', '未知')
    yjd_today = factors.get('YJD_CompositeCrowding')
    if yjd_today is not None and not (isinstance(yjd_today, float) and np.isnan(yjd_today)):
        lines.append(f"\n  YJD 综合拥挤度  : {yjd_today:.2f}  ({yjd_status})")
        for k in ['YJD_MA5', 'YJD_MA20', 'YJD_Min50', 'YJD_Max50']:
            v = factors.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                lines.append(f"    {k:30s}: {v:>10.4f}")

    # 波动率
    lines.append("\n[波动率因子]")
    for k in ['ATR20', 'ATR20_Pct', 'Parkinson_Vol', 'GarmanKlass_Vol', 'YangZhang_Vol', 'Realized_Vol_20d']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            note = annotate('volatility', k, v) if with_annotation else ""
            unit = " %" if k == 'ATR20_Pct' else " 元" if k == 'ATR20' else ""
            lines.append(f"    {k:30s}: {v:>10.4f}{unit}  {note}")

    # 高级复合
    lines.append("\n[高级复合因子]")
    for k in ['Residual_Momentum_20d', 'Residual_Volatility_20d', 'Beta_Neutral_Alpha',
              'Industry_Neutral_Alpha', 'Stock_Beta_60d']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            note = annotate('advanced', k, v) if with_annotation else ""
            lines.append(f"    {k:30s}: {v:>10.4f}  {note}")

    # Alpha101
    lines.append("\n[Alpha101 经典因子]")
    for k in ['Alpha21', 'Alpha35', 'Alpha39', 'Alpha39_section', 'Alpha47',
              'Alpha57', 'Alpha57_simple', 'Alpha70', 'Alpha102',
              'Alpha83_ts_rank', 'Alpha83_section', 'Alpha84',
              'Alpha99_ts_rank', 'Alpha99_section', 'Alpha101', 'Alpha176']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            note = annotate('wq_alpha', k, v) if with_annotation else ""
            bar = '|' * int(v * 20) if 0 <= v <= 1 else ''
            if k == 'Alpha57':
                note = "  >>> WorldQuant 完整版（ts_argmax + decay + 反转）"
            elif k == 'Alpha57_simple':
                note = "  >>> 简化版（与原 SuperMind 脚本 line 411-415 一致）"
            elif k == 'Alpha39_section':
                note = "  >>> 截面版 cs_rank（原脚本 line 386 一致）"
            elif k == 'Alpha83_section':
                note = "  >>> 截面版 cs_rank（原脚本 line 539-540）"
            elif k == 'Alpha99_section':
                note = "  >>> 截面版 cs_rank（原脚本 line 543-544）"
            lines.append(f"    {k:30s}: {v:>10.4f}  {bar}  {note}")

    # 估值
    lines.append("\n[估值因子]")
    for k in ['PE_TTM', 'PB', 'PS_TTM', 'Log_Total_Market_Value']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            lines.append(f"    {k:30s}: {v:>10.4f}")

    # 盈利预期
    lines.append("\n[盈利预期因子]")
    for k in ['Consensus_Direction', 'EPS_Revision_Rate', 'Consensus_Expectation_Dir']:
        v = factors.get(k)
        lines.append(f"    {k:30s}: {v}")

    # 技术量价
    lines.append("\n[技术量价因子]")
    for k in ['Deviation_From_MA200', 'Amihud_Illiquidity', 'RSI14',
              'DDE_Net_Today', 'DDE_Net_3d',
              'Turnover_Percentile_60d', 'Volume_Ratio', 'Net_Flow_Rate', 'Composite_Chip_Quality',
              'Ret_20d', 'Ret_60d']:
        v = factors.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            if k in ('Ret_20d', 'Ret_60d'):
                lines.append(f"    {k:30s}: {v*100:>+9.2f} %")
            elif k in ('DDE_Net_Today', 'DDE_Net_3d'):
                lines.append(f"    {k:30s}: {v:>+8.3f} 亿")
            elif k == 'Net_Flow_Rate':
                lines.append(f"    {k:30s}: {v:>+8.3f} %")
            else:
                lines.append(f"    {k:30s}: {v:>10.4f}")

    # v1.3 新增：MACD
    macd_dif = factors.get('MACD_DIF')
    if macd_dif is not None and not (isinstance(macd_dif, float) and np.isnan(macd_dif)):
        lines.append("\n[MACD 因子]")
        for k in ['MACD_DIF', 'MACD_DEA', 'MACD_BAR']:
            v = factors.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                lines.append(f"    {k:30s}: {v:>10.4f}")
        v = factors.get('Ret20_Pct_60d')
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            lines.append(f"    {'Ret20_Pct_60d':30s}: {v:>10.4f}  （当前 20 日涨幅在 60 日窗口百分位）")

    # v1.3 新增：DDE 多日累计
    dde_5d = factors.get('DDE_5d')
    if dde_5d is not None and not (isinstance(dde_5d, float) and np.isnan(dde_5d)):
        lines.append("\n[DDE 多日累计 + 净额率]")
        for n in [1, 3, 5, 10]:
            dde_v = factors.get(f'DDE_{n}d')
            amt_v = factors.get(f'Amt_{n}d')
            rate_v = factors.get(f'DDE_Rate_{n}d')
            if dde_v is not None:
                lines.append(f"    {n}日累计 DDE={dde_v:>+8.3f}亿 / Amt={amt_v:>8.2f}亿 / Rate={rate_v:>+7.3f}%")

    # v1.3 新增：压力位 / 支撑位
    res = factors.get('Resistance')
    if res is not None and not (isinstance(res, float) and np.isnan(res)):
        lines.append("\n[压力位 / 支撑位]")
        for k in ['Resistance', 'Support', 'RS_Position_Pct']:
            v = factors.get(k)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                unit = " %" if k == 'RS_Position_Pct' else " 元"
                lines.append(f"    {k:30s}: {v:>10.4f}{unit}")

    lines.append("\n" + sep)
    return "\n".join(lines)


# ============================================================
# 自测（开发期）
# ============================================================
if __name__ == '__main__':
    # 用合成数据自测，确保函数不报错
    np.random.seed(42)
    dates = pd.bdate_range('2025-01-01', periods=252)
    close = pd.Series(100 + np.cumsum(np.random.randn(252) * 0.02), index=dates)
    high = close + np.abs(np.random.randn(252)) * 0.5
    low = close - np.abs(np.random.randn(252)) * 0.5
    open_ = close.shift(1).fillna(100)
    volume = pd.Series(np.random.randint(1000000, 5000000, 252), index=dates)
    amount = close * volume

    stock_df = pd.DataFrame({'open': open_, 'high': high, 'low': low,
                             'close': close, 'volume': volume, 'amount': amount})

    idx_df = stock_df.copy()  # 用自己当指数做测试

    factors = compute_all_factors(stock_df=stock_df, idx_df=idx_df,
                                  valuation={'PE_TTM': 15.0, 'PB': 2.5, 'PS_TTM': 1.8,
                                            'market_cap': 5e10})
    print(format_factor_report(factors))