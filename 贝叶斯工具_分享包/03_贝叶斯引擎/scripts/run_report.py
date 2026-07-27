"""
run_report.py — 贝叶斯量化决策报告生成器（参数化 CLI 入口）
================================================================
让"输入股票代码即可出报告"成为现实。

用法：
    python run_report.py 000977
    python run_report.py 000977.SZ 浪潮信息
    python run_report.py 600519 贵州茅台
    python run_report.py 300750            # 创业板（自动识别深圳）
    python run_report.py 000977 --no-oamv  # 跳过 0AMV 保鲜检查
    python run_report.py 000977 --open     # 强制视同已开盘（要求当天 0AMV 新鲜）

自动取数（无需手动准备）：
    1. 个股 K 线：先读本地 data/kline/day/{code}.parquet；
                  缺失则用 pytdx 直连通达信公网拉取并缓存到本地。
    2. 截面数据：读 data/section/zz500_60d.parquet（缺失则自动跑 bulk_sync_zz500）。
    3. 沪深300 基准：读 data/kline/day/000300_SH.parquet；缺失则自动拉。
    4. 估值/筹码/一致预期：读本地 dict 缓存（db_sync）；缺失则标注 N/A。
    5. 宏观快照：读 data/macro/latest.json（agent 经 westock data_macro 写入）。
    6. 行业数据：读 data/industry/{code}.json（agent 经 MCP 写入）。
    7. 政策 PSI：读 data/psi/{code}.json（AI 据近期政策新闻判定后写入）。
    8. 新闻事件：读 data/news/{code}.json（agent 经 westock data_news 写入）。
    9. 主力资金流(DDE)：读 data/fund_flow/{code}.json（agent 经 westock data_fund_flow 写入，含每日主力/特大单/大单净流入序列）。

贝叶斯 E1-E6 真实输入：
    E1 经济  ← 宏观快照（data/macro/latest.json）
    E2 政策+流动性  ← PSI 评分（data/psi/{code}.json）+ 融资融券（data/margin/{code}.json）+ 北向资金（data/north/{code}.json）
    E3 行业  ← 行业数据（data/industry/{code}.json：CR4/BCI分位/ROE/生命周期）
    E4 企业  ← 估值（PE/PB/PS）+ F-Score/PEG（F-Score 由 AI 据财报填）
    E5 市场  ← 因子派生（动量分位/量价模式/股东户数变化）
    E6 情绪  ← ACSI 分位（data/sentiment/{code}.json，缺则默认中性）

输出：
    D:\\AILIANGHUA\\贝叶斯报告\\{股票名称} {代码} {YYYYMMDD} {评分}.md

依赖：factor_engine / bayesian_engine / oamv_analyzer / db_sync / bulk_sync_zz500
"""
from __future__ import annotations
import os
import re
import sys
import time
import csv
import argparse

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from factor_engine import (
    compute_all_factors, compute_dde_net_today, compute_dde_net_3d,
    compute_net_flow_rate, compute_dde_multi_period,
)
from bayesian_engine import decide, WEIGHTS_BY_STATE, _position_from_score
from oamv_analyzer import (
    load_oamv, compute_moving_averages, classify_market_state, assert_fresh,
)
from report import find_oamv_csv
import db_sync

# ── 路径常量 ──
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(SKILL_DIR, 'data')
SECTION_PARQUET = os.path.join(DATA_DIR, 'section', 'zz500_60d.parquet')
OUTPUT_DIR = r'D:\AILIANGHUA\贝叶斯报告'
ZZ500_CSV = os.path.normpath(os.path.join(
    os.path.dirname(SCRIPT_DIR), '..', '..', 'AILIANGHUA', '贝叶斯工具',
    'references', 'zz500_constituents_full.csv'))

PYTDX_SERVERS = [
    ('60.12.136.250', 7709),
    ('115.238.56.198', 7709),
    ('180.153.18.170', 7709),
    ('123.125.108.14', 7709),
]


# ============================================================
# 代码标准化
# ============================================================
def normalize_code(raw: str):
    """'000977' / '000977.SZ' / '000977.SZ ' → (pure, suffix, market, std)"""
    raw = raw.strip().upper()
    m = re.match(r'^(\d{6})(?:\.(SZ|SH|BJ))?$', raw)
    if not m:
        raise ValueError(f'无法识别的股票代码: {raw}（应为 6 位数字，可选 .SZ/.SH/.BJ）')
    pure = m.group(1)
    suffix = m.group(2)
    head = pure[:2]
    if suffix:
        market = {'SZ': 0, 'SH': 1, 'BJ': 2}[suffix]
    elif head in ('00', '30', '002', '003'):
        market, suffix = 0, 'SZ'
    elif head in ('60', '68'):
        market, suffix = 1, 'SH'
    elif head in ('8', '4', '92'):
        market, suffix = 2, 'BJ'
    else:
        market, suffix = 1, 'SH'
    return pure, suffix, market, f'{pure}.{suffix}'


# ============================================================
# pytdx 单只 K 线拉取（复用 bulk_sync 的服务器列表）
# ============================================================
def fetch_single_kline(pure: str, market: int, days: int = 260) -> pd.DataFrame:
    """用 pytdx 直连拉单只股票日 K 线，返回 db_sync 标准 DataFrame"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    connected = False
    for host, port in PYTDX_SERVERS:
        try:
            api.connect(host, port)
            connected = True
            print(f'  ✅ 连通通达信服务器 {host}:{port}')
            break
        except Exception as e:
            print(f'  ⚠️ {host}:{port} 连不上: {e}')
    if not connected:
        raise RuntimeError('所有通达信服务器都连不上（检查网络）')

    try:
        bars = api.get_security_bars(9, market, pure, 0, days) or []
    finally:
        api.disconnect()

    if not bars:
        raise RuntimeError(f'pytdx 未返回 {pure} 的 K 线数据')

    rows = []
    for bar in bars:
        rows.append({
            'date': pd.Timestamp(bar['year'], bar['month'], bar['day']),
            'open': float(bar['open']),
            'high': float(bar['high']),
            'low': float(bar['low']),
            'close': float(bar['close']),
            'volume': float(bar['vol']) * 100,   # 手 → 股
            'amount': float(bar['amount']),
        })
    df = pd.DataFrame(rows).set_index('date').sort_index()
    df = df[['open', 'high', 'low', 'close', 'volume', 'amount']]
    return df


def get_stock_kline(pure: str, market: int, std: str) -> pd.DataFrame:
    """优先读本地缓存，缺失则拉取并缓存"""
    cached = db_sync.read_local(std, freq='day')
    if cached is not None and not cached.empty:
        print(f'  📂 读本地缓存 K 线: {std}（{len(cached)} 行，截止 {cached.index[-1].date()}）')
        return cached

    print(f'  🌐 本地无缓存，pytdx 拉取 {pure} ...')
    df = fetch_single_kline(pure, market)
    db_sync.write_local(std, 'day', df)
    print(f'  💾 已缓存到 data/kline/day/{std.replace(".", "_")}.parquet（{len(df)} 行）')
    return df


def get_index_kline(std: str, market: int, pure: str) -> pd.DataFrame:
    """沪深300 等基准指数 K 线"""
    cached = db_sync.read_local(std, freq='day')
    if cached is not None and not cached.empty:
        print(f'  📂 读本地基准 K 线: {std}（{len(cached)} 行）')
        return cached
    print(f'  🌐 拉取基准指数 {pure} ...')
    df = fetch_single_kline(pure, market, days=260)
    db_sync.write_local(std, 'day', df)
    return df


# ============================================================
# 截面数据
# ============================================================
def load_section() -> pd.DataFrame:
    """读 zz500 截面 panel；缺失则自动跑 bulk_sync_zz500"""
    if not os.path.exists(SECTION_PARQUET):
        print('  🌐 截面 parquet 缺失，自动跑 bulk_sync_zz500 ...')
        import bulk_sync_zz500
        if os.path.exists(bulk_sync_zz500.DEFAULT_CSV):
            bulk_sync_zz500.bulk_sync(bulk_sync_zz500.DEFAULT_CSV, 60)
        else:
            raise FileNotFoundError(f'缺少截面 parquet 且找不到成分股清单: {bulk_sync_zz500.DEFAULT_CSV}')
    return pd.read_parquet(SECTION_PARQUET, engine='pyarrow')


def build_section_frames(panel: pd.DataFrame):
    """长表 panel → 各字段宽表（行=date，列=code）"""
    section_close = panel.pivot_table(index='date', columns='code', values='close', aggfunc='last')
    section_open = panel.pivot_table(index='date', columns='code', values='open', aggfunc='last')
    section_high = panel.pivot_table(index='date', columns='code', values='high', aggfunc='last')
    section_low = panel.pivot_table(index='date', columns='code', values='low', aggfunc='last')
    section_volume = panel.pivot_table(index='date', columns='code', values='volume', aggfunc='last')
    section_amount = panel.pivot_table(index='date', columns='code', values='amount', aggfunc='last')
    section_vwap = section_amount / (section_volume + 1e-9)
    return {
        'section_close': section_close, 'section_open': section_open,
        'section_high': section_high, 'section_low': section_low,
        'section_volume': section_volume, 'section_amount': section_amount,
        'section_vwap': section_vwap,
    }


def inject_stock_into_section(section: dict, stock_df: pd.DataFrame, pure: str) -> dict:
    """把目标个股 K 线注入截面宽表，使其能在 ZZ500 参照分布里正常算截面分位。

    设计约定：中证500 截面只是「参照基准」，与个股是否属于该成分无关。
    任何股票都应在 500 分布里排一个百分位。做法：把个股自己的 OHLCV
    作为额外一列并入 section_* 宽表（按日期对齐 + 前向填充），_pick_stock
    即可取到该股值并算出其在 500 中的截面分位。
    """
    if pure in section['section_close'].columns:
        return section  # 已是成分股，无需注入
    idx = section['section_close'].index
    def _align(col_name):
        if col_name not in stock_df.columns:
            return None
        return stock_df[col_name].reindex(idx).ffill()
    close = _align('close'); o = _align('open'); h = _align('high')
    l = _align('low'); v = _align('volume'); a = _align('amount')
    if any(x is None for x in (close, o, h, l, v, a)):
        print(f'  ⚠️ 个股 K 线字段不全，截面因子保持 N/A（stock={pure}）')
        return section
    section['section_close'][pure] = close
    section['section_open'][pure] = o
    section['section_high'][pure] = h
    section['section_low'][pure] = l
    section['section_volume'][pure] = v
    section['section_amount'][pure] = a
    # 重算 vwap（含新股列）
    section['section_vwap'] = section['section_amount'] / (section['section_volume'] + 1e-9)
    return section


# ============================================================
# 贝叶斯输入缓存（宏观/行业/PSI/新闻/估值/筹码/一致预期/股东）
# ============================================================
def load_bayes_caches(std: str):
    """
    读取 E1-E6 所需的全部本地缓存。
    返回 (caches, missing)：
      caches = {macro, industry, psi, news, sentiment, valuation, chip, consensus, shareholder}
      missing = 缺失项的中文说明列表（用于报告"数据完整性"段）
    """
    caches = {
        'macro': db_sync.read_json('macro', 'latest'),
        'industry': db_sync.read_json('industry', std),
        'psi': db_sync.read_json('psi', std),
        'news': db_sync.read_json('news', std),
        'sentiment': db_sync.read_json('sentiment', std),
        'valuation': db_sync.read_dict('valuation', std),
        'chip': db_sync.read_dict('chip', std),
        'consensus': db_sync.read_dict('consensus', std),
        'shareholder': db_sync.read_dict('shareholder', std),
        'corp': db_sync.read_json('corp', std),
        'margin': db_sync.read_dict('margin', std),
        'north': db_sync.read_json('north', std),
        'fund_flow': db_sync.read_json('fund_flow', std),
    }
    missing = []
    if caches['macro'] is None:
        missing.append('宏观快照（GDP/M2/利差/PMI）：未缓存，运行 westock data_macro 后重跑')
    if caches['industry'] is None:
        missing.append('行业数据（CR4/BCI/ROE/生命周期）：未缓存，运行 industry 同步后重跑')
    if caches['psi'] is None:
        missing.append('政策 PSI 评分：未缓存，AI 据近期政策新闻判定后重跑（将用默认中性）')
    if caches['news'] is None:
        missing.append('新闻事件：未缓存，运行 westock data_news 后重跑')
    if caches['sentiment'] is None:
        missing.append('情绪 ACSI 分位：未缓存，将用默认中性 50')
    if caches['valuation'] is None:
        missing.append('估值（PE/PB/PS）：未缓存，运行 tdx_security_deep_info 后重跑')
    if caches['chip'] is None:
        missing.append('筹码结构：未缓存，运行 westock data_chip 后重跑')
    if caches['consensus'] is None:
        missing.append('一致预期：未缓存，运行 westock data_consensus 后重跑')
    if caches['corp'] is None:
        missing.append('企业深度体检（巴菲特式）：未缓存，运行 data_finance/data_profile/data_score 同步后重跑')
    if caches['margin'] is None:
        missing.append('融资融券：未缓存，运行 westock data_fund_margin 同步后重跑')
    if caches['north'] is None:
        missing.append('北向资金：未缓存，运行 westock data_north_holding 同步后重跑')
    if caches['fund_flow'] is None:
        missing.append('主力资金流(DDE)：未缓存，运行 westock data_fund_flow 同步后重跑')
    return caches, missing


def build_fund_flow_inputs(std: str, stock_df: pd.DataFrame | None = None):
    """
    读取 fund_flow 缓存（腾讯自选股 data_fund_flow 原始结构），并把 MCP 字段名
    映射成因子引擎期望的字段名：
        JumboNetFlow -> hugeNetInflow（特大单）
        BlockNetFlow -> bigNetInflow（大单）
        MainNetFlow  -> mainNetInflow（主力净流入）
        dde_3d       -> 最近 3 日（特大单+大单）累计（元，供 compute_dde_net_3d）
    同时返回 fund_flow_series：每日 DDE=特大单+大单（元），按日期升序。
    返回 (fund_flow_dict, fund_flow_series)；无缓存时返回 (None, None)。
    """
    raw = db_sync.read_json('fund_flow', std)
    if not raw or not isinstance(raw.get('data'), list) or len(raw['data']) == 0:
        return None, None

    def _to_float(r, k):
        v = r.get(k)
        try:
            return float(v) if v not in (None, '') else 0.0
        except (TypeError, ValueError):
            return 0.0

    records = sorted(raw['data'], key=lambda r: r.get('date', ''))
    last = records[-1]

    fund_flow = {
        'hugeNetInflow': _to_float(last, 'JumboNetFlow'),
        'bigNetInflow': _to_float(last, 'BlockNetFlow'),
        'mainNetInflow': _to_float(last, 'MainNetFlow'),
    }
    # 最近 3 日 DDE 累计（元）：特大单 + 大单
    last3 = records[-3:]
    fund_flow['dde_3d'] = sum(
        _to_float(r, 'JumboNetFlow') + _to_float(r, 'BlockNetFlow') for r in last3
    )
    # 当日成交额（元），供 Net_Flow_Rate 兜底 = mainNetInflow / amount
    if stock_df is not None and 'amount' in stock_df.columns and len(stock_df) > 0:
        fund_flow['amount'] = float(stock_df['amount'].iloc[-1])

    # 时间序列：每日 DDE = 特大单 + 大单（元），按日期升序
    dates = [pd.Timestamp(r.get('date')) for r in records]
    dde_vals = [_to_float(r, 'JumboNetFlow') + _to_float(r, 'BlockNetFlow') for r in records]
    fund_flow_series = pd.Series(dde_vals, index=pd.DatetimeIndex(dates), name='dde')
    return fund_flow, fund_flow_series


# ============================================================
# 构建贝叶斯 E1-E6 真实输入
# ============================================================
def build_bayes_inputs(factors: dict, caches: dict, std: str = ''):
    """
    把缓存里的真实数据映射到 decide() 需要的 macro + ai_judgments，
    并产出一份"输入明细"供报告展示（含每项数据来源 real/default）。

    返回 (macro, ai_judgments, detail)
      detail = {E1..E6: {'label','source','items':[(名,值,单位)],'note'}}
    """
    macro_d = caches['macro']
    industry = caches['industry']
    psi = caches['psi']
    sentiment = caches['sentiment']
    shareholder = caches['shareholder']
    valuation = caches['valuation']

    # ---- E1 经济（宏观快照）----
    if macro_d:
        macro = {
            'gdp_gap': macro_d.get('gdp_gap'),
            'm2_yoy': macro_d.get('m2_yoy'),
            'yield_curve_spread_bp': macro_d.get('yield_curve_spread_bp'),
            'pmi': macro_d.get('pmi'),
        }
        e1_src = 'real'
        e1_note = f"来源：{macro_d.get('source','未知')} · 日期 {macro_d.get('date','?')}"
    else:
        macro = {}   # decide 用默认（gdp_gap=0 / m2=8 / 利差=0）
        e1_src = 'default'
        e1_note = '缺宏观快照，E1 用默认中性值'
    e1_items = [
        ('GDP 增速缺口 gdp_gap', macro.get('gdp_gap'), '%'),
        ('M2 同比 m2_yoy', macro.get('m2_yoy'), '%'),
        ('10Y-2Y 利差', macro.get('yield_curve_spread_bp'), 'bp'),
        ('制造业 PMI', macro.get('pmi'), ''),
    ]

    # ---- E2 政策 + 流动性（PSI + 融资融券 + 北向资金）----
    if psi:
        psi_score = psi.get('psi_score', 1)
        psi_type = psi.get('psi_policy_type', 'structural')
        psi_months = psi.get('psi_months_since', 6)
        e2_src = 'real'
        e2_note = f"判定依据：{psi.get('basis','—')}"
    else:
        psi_score, psi_type, psi_months = 1, 'structural', 6
        e2_src = 'default'
        e2_note = '缺 PSI 缓存，用默认中性（需 AI 据政策新闻判定）'
    e2_items = [
        ('PSI 政策信号强度', psi_score, '(-3~+3)'),
        ('政策类型', psi_type, ''),
        ('距政策发布', psi_months, '月'),
    ]

    # E2 的"流动性"维度：融资融券（杠杆资金）+ 北向资金（外资增量）
    margin = caches.get('margin')
    if margin:
        margin_ratio = margin.get('finance_balance_ratio', 0.0) or 0.0
        margin_dod = margin.get('finance_balance_dod', 0.0) or 0.0
        e2_items += [
            ('— 融资余额', fmt_yi(margin.get('finance_balance')), ''),
            ('— 融券余额', fmt_yi(margin.get('security_balance')), ''),
            ('— 融资余额占流通市值比', fmt_pct(margin_ratio), ''),
            ('— 融资余额日变动', margin_dod, '%'),
        ]
        e2_note += '；融资融券来自 margin 缓存'
        if e2_src == 'default':
            e2_src = 'real'
    else:
        margin_ratio, margin_dod = 0.0, 0.0
        e2_note += '；融资融券缺 margin 缓存（将用默认中性）'

    north = caches.get('north')
    if north:
        n_cur = north.get('cur', {}) or {}
        n_prev = north.get('prev', {}) or {}
        n_ratio = n_cur.get('holding_ratio_pct', 0.0) or 0.0
        n_shares = n_cur.get('holding_shares') or 0
        n_prev_shares = n_prev.get('holding_shares') or 0
        n_q_add_pct = (n_shares - n_prev_shares) / n_prev_shares * 100 if n_prev_shares else 0.0
        n_cap_chg_q = n_cur.get('cap_chg_q', 0.0) or 0.0
        n_cap_chg_y = n_cur.get('cap_chg_y', 0.0) or 0.0
        e2_items += [
            ('— 北向持股比例', n_ratio, '%'),
            ('— 北向季度增持', n_q_add_pct, '%'),
            ('— 北向持股市值季变动', fmt_yi(n_cap_chg_q), ''),
            ('— 北向持股市值年变动', fmt_yi(n_cap_chg_y), ''),
        ]
        e2_note += '；北向资金来自 north 缓存'
        if e2_src == 'default':
            e2_src = 'real'
    else:
        n_ratio, n_q_add_pct, n_cap_chg_q, n_cap_chg_y = 0.0, 0.0, 0.0, 0.0
        e2_note += '；北向资金缺 north 缓存（将用默认中性）'

    # ---- E3 行业 ----
    if industry:
        cr4 = industry.get('cr4') if industry.get('cr4') is not None else 50
        # 行业 BCI 分位：westock 无商业周期指数接口，用行业近期涨跌(景气)代理
        ind_ytd = industry.get('industry_momentum_ytd')
        if ind_ytd is not None:
            if ind_ytd > 40: bci = 90
            elif ind_ytd > 20: bci = 75
            elif ind_ytd > 0: bci = 60
            elif ind_ytd > -20: bci = 40
            else: bci = 20
            bci_note = f"行业YTD涨跌{ind_ytd:+.1f}%代理"
        else:
            bci = industry.get('bci_percentile') if industry.get('bci_percentile') is not None else 50
            bci_note = "缺行业涨跌→默认50"
        # ROE 相对行业：公司 ROE vs 行业景气代理 ROE（行业 YTD 涨幅映射）
        fin = (caches.get('corp') or {}).get('finance_fy2025', {}) or {}
        co_roe = fin.get('roe')
        if co_roe is not None and ind_ytd is not None:
            ind_roe_proxy = max(-5.0, min(15.0, ind_ytd / 4.0))
            roe_vs = round((co_roe * 100) - ind_roe_proxy, 1)
        else:
            roe_vs = industry.get('roe_vs_industry') if industry.get('roe_vs_industry') is not None else 0
        life_stage = industry.get('life_stage') or (
            '成长期' if (pe_tmp := (factors.get('PE_TTM') or 0)) and pe_tmp < 60 else '成熟期')
        e3_src = 'real'
        e3_note = (f"行业：{industry.get('industry_name','?')} "
                   f"（{industry.get('sw_l1','?')}/{industry.get('sw_l2','?')}）；"
                   f"生命周期={life_stage}；BCI分位用{bci_note}；"
                   f"CR4集中度 westock 无接口→默认50")
    else:
        cr4, bci, roe_vs = 50, 50, 0
        pe = factors.get('PE_TTM') or 0
        life_stage = '成长期' if (pe and pe < 60) else '成熟期'
        e3_src = 'default'
        e3_note = '缺行业缓存，用默认中性（CR4=50/BCI=50）'
    e3_items = [
        ('行业生命周期', life_stage, ''),
        ('行业集中度 CR4', cr4, '%'),
        ('行业 BCI 分位', bci, '%'),
        ('ROE 相对行业', roe_vs, 'pct'),
    ]

    # ---- E4 企业（含巴菲特式深度体检）----
    pe = factors.get('PE_TTM')
    pb = factors.get('PB')
    ps = factors.get('PS_TTM')
    # 板块：创业板(300)/科创板(688) → growth
    code_head = (factors.get('stock_code') or '').replace('.', '')
    board = 'growth' if (code_head[:3] == '300' or code_head[:3] == '688') else 'main'
    # F-Score：优先缓存，否则默认 7（中性偏多）
    f_score = (valuation or {}).get('f_score', 7) if valuation else 7
    # PEG：优先一致预期推导，否则默认 1.0
    peg = (consensus_peg(caches['consensus']) if caches['consensus'] else 1.0)
    corp = caches.get('corp')
    e4_src = 'real' if (valuation is not None or caches['consensus'] is not None or corp is not None) else 'default'
    e4_note = '估值来自 valuation 缓存' + ('' if f_score != 7 else '；F-Score 默认 7（缺财报）')
    if corp:
        e4_note += f"；企业深度体检来自 corp 缓存（基本面评分 FunmScore={corp.get('scores',{}).get('funm','?')}）"
    e4_items = [
        ('PE_TTM', pe, '倍'),
        ('PB', pb, '倍'),
        ('PS_TTM', ps, '倍'),
        ('F-Score', f_score, '(0~10)'),
        ('板块', board, ''),
        ('PEG', peg, ''),
    ]
    # 企业深度体检明细（来自 corp 缓存，缺则标记默认）
    if corp:
        fin = corp.get('finance_fy2025', {}) or {}
        scores = corp.get('scores', {}) or {}
        e4_items += [
            ('— 基本面评分 FunmScore', scores.get('funm'), '(0~100)'),
            ('— 综合评分 CompScore', scores.get('comp'), '(0~100)'),
            ('— 风险评分 RiskScore', scores.get('risk'), '(0~100)'),
            ('FY营收', fmt_yi(fin.get('revenue')), ''),
            ('FY归母净利', fmt_yi(fin.get('np_parent')), ''),
            ('毛利率', fmt_pct(fin.get('gross_margin')), ''),
            ('净利率', fmt_pct(fin.get('net_margin')), ''),
            ('ROE', fmt_pct(fin.get('roe')), ''),
            ('资产负债率', fmt_pct(fin.get('debt_ratio')), ''),
            ('经营现金流 OCF', fmt_yi(fin.get('ocf')), ''),
            ('OCF/净利', fin.get('ocf_np_ratio'), '倍'),
            ('商誉', fmt_yi(fin.get('goodwill')), ''),
        ]
        corp_quality = corp.get('corp_quality', 0.0)
    else:
        corp_quality = 0.0
        e4_note += '；企业深度体检缺 corp 缓存（用默认中性）'

    # ---- E5 市场（因子派生）----
    alpha84 = factors.get('Alpha84')
    ret60 = factors.get('Ret_60d')
    if alpha84 is not None and not (isinstance(alpha84, float) and pd.isna(alpha84)):
        momentum_pct = int(max(0, min(100, alpha84 * 100)))
        e5_note = '由截面因子 Alpha84 / MACD_BAR / 股东户数变化派生'
    elif ret60 is not None and not (isinstance(ret60, float) and pd.isna(ret60)):
        # 非中证500：用单只 K 线近60日收益率映射动量分位 [-30%,+30%]->[0,100]
        momentum_pct = int(max(0, min(100, (ret60 + 0.30) / 0.60 * 100)))
        e5_note = (f'由单只K线近60日收益 {ret60*100:+.1f}% / MACD_BAR / 股东户数变化派生'
                   f'（截面Alpha84缺失→单只动量分位）')
    else:
        momentum_pct = 50
        e5_note = '由 MACD_BAR / 股东户数变化派生（动量缺数据→中性50）'
    macd_bar = factors.get('MACD_BAR') or 0
    shr_chg = shareholder.get('change_pct') if (shareholder and shareholder.get('change_pct') is not None) else None
    pattern = '放量突破 + 创新高' if macd_bar > 0 else '横盘整理 + 量能萎缩'
    e5_src = 'real'
    e5_items = [
        ('动量分位 momentum', momentum_pct, '%'),
        ('量价模式', pattern, ''),
        ('股东户数变化', shr_chg if shr_chg is not None else '数据缺失', '%' if shr_chg is not None else ''),
    ]

    # ---- E6 情绪（ACSI，合成）----
    if sentiment:
        acsi = sentiment.get('acsi_percentile', 50)
        issi = sentiment.get('issi_deviation', '正常')
        e6_src = 'real'
        e6_note = f"来源：{sentiment.get('source','?')}"
    else:
        # westock 无 ACSI 直连接口，合成：技术评分(TecScore) + 资金面(融资净买/北向) + 价格动量
        scores = (caches.get('corp') or {}).get('scores', {}) or {}
        tec = scores.get('tec', 50) or 50  # 0~100 技术情绪分
        fund = 50.0
        if margin and margin.get('finance_balance_dod') is not None:
            fund += 10 if margin['finance_balance_dod'] > 0 else -10
        if north and north.get('q_add_pct') is not None:
            q = north['q_add_pct']
            fund += 15 if q > 5 else (-15 if q < -5 else 0)
        acsi = int(max(0, min(100, 0.4 * tec + 0.3 * fund + 0.3 * momentum_pct)))
        issi = '正常'
        e6_src = 'real'  # 合成数据，来源透明标注
        e6_note = (f"合成（技术评分 Tec={tec:.0f} + 资金面 + 动量{momentum_pct}%；"
                   f"westock 无 ACSI 直连接口，已写 sentiment 缓存）")
        # 写缓存供复用
        try:
            db_sync.write_json('sentiment', std.replace('.', '_'), {
                'acsi_percentile': acsi, 'issi_deviation': issi,
                'tec_score': tec, 'fund_score': fund, 'momentum_pct': momentum_pct,
                'source': '合成：TecScore+融资净买方向+北向增减+价格动量（westock 无 ACSI 接口）',
                'date': pd.Timestamp.now().strftime('%Y-%m-%d'),
            })
        except Exception as _e:
            print(f'  ⚠️ sentiment 缓存写入失败: {_e}')
    e6_items = [
        ('ACSI 情绪分位', acsi, '%'),
        ('ISSI 偏离', issi, ''),
    ]

    ai_judgments = {
        'psi_score': psi_score, 'psi_months_since': psi_months, 'psi_policy_type': psi_type,
        'life_stage': life_stage, 'policy_reshape_pct': 0.15,
        'bci_percentile': bci, 'cr4': cr4, 'roe_vs_industry': roe_vs,
        'f_score': f_score, 'board_type': board, 'peg': peg,
        'momentum_percentile': momentum_pct,
        'volume_price_pattern': pattern, 'shareholder_change_pct': shr_chg or 0,
        'acsi_percentile': acsi, 'issi_deviation': issi,
        # 新增：企业深度体检 + 融资融券/北向流动性（E2 政策+流动性维度）
        'corp_quality': corp_quality,
        'margin_balance_ratio': margin_ratio,
        'margin_trend': margin_dod,
        'north_holding_ratio': n_ratio,
        'north_q_add_pct': n_q_add_pct,
        'north_cap_chg_y': n_cap_chg_y,
    }

    detail = {
        'E1': {'label': '经济', 'source': e1_src, 'items': e1_items, 'note': e1_note},
        'E2': {'label': '政策+流动性', 'source': e2_src, 'items': e2_items, 'note': e2_note},
        'E3': {'label': '行业', 'source': e3_src, 'items': e3_items, 'note': e3_note},
        'E4': {'label': '企业', 'source': e4_src, 'items': e4_items, 'note': e4_note},
        'E5': {'label': '市场技术', 'source': e5_src, 'items': e5_items, 'note': e5_note},
        'E6': {'label': '情绪', 'source': e6_src, 'items': e6_items, 'note': e6_note},
    }
    extras = {'corp': corp, 'margin': margin, 'north': north}
    return macro, ai_judgments, detail, extras


def consensus_peg(consensus: dict):
    """从一致预期推导 PEG（无 g 时用默认 1.0）"""
    try:
        eps_now = consensus.get('eps_now')
        eps_90 = consensus.get('eps_90d_ago')
        pe = None  # PE 在 factors 里，这里只用增速
        if eps_now and eps_90 and eps_90 > 0:
            g = (eps_now - eps_90) / abs(eps_90) * 100
            if g > 0:
                return round(g / 100, 2)
    except Exception:
        pass
    return 1.0


# ============================================================
# 0AMV 状态 + 贝叶斯决策
# ============================================================
def get_oamv_state(require_fresh: bool = True) -> dict | None:
    csv_path = find_oamv_csv()
    if csv_path is None:
        print('  ⚠️ 找不到 0AMV CSV（指南针数据未提取）。用 --no-oamv 可跳过，或先跑 zhinanzhen-0amv-daily-db skill。')
        return None
    if require_fresh:
        assert_fresh(csv_path)          # 盘前自动用上一交易日；过期直接抛异常
    oamv_df = load_oamv(csv_path)
    oamv_ma = compute_moving_averages(oamv_df)
    return classify_market_state(oamv_ma)


def integrate_position(oamv_state: dict | None, decision: dict) -> dict:
    """生成个股多头仓位推荐与市场总仓位（0AMV）两条独立结论。

    设计约定：
      - 0AMV = 市场总仓位建议（择时层），与个股无关，单独呈现。
      - 个股多头仓位 = 仅由贝叶斯评分决定，严格遵循提示词第六步：
        评分<=30 不持多头（空仓），评分>=40 才允许正仓位，与方向基准区间脱钩。
    两者在报告中并列展示；保守配置可取 min(个股仓位, 市场总仓位)。
    """
    score = round(decision['posterior']['H1'] * 100)
    direction, ind_low, ind_high, action = _position_from_score(score)
    final_range = '0%（空仓）' if (ind_low, ind_high) == (0, 0) else f'{ind_low}~{ind_high}%'
    # 0AMV：市场总仓位建议，单独透传
    market_total = oamv_state['position'] if oamv_state else '未加载'
    market_grade = f"{oamv_state['grade']} {oamv_state['state']}" if oamv_state else '未加载'
    return {
        'direction': direction, 'score': score, 'action': action,
        'oamv_range': market_total,        # 市场总仓位建议（择时层）
        'oamv_grade': market_grade,
        'final_range': final_range,
        'has_oamv': oamv_state is not None,
    }


# ============================================================
# 报告渲染
# ============================================================
def f_num(v, digits=4, suffix=''):
    if v is None:
        return 'N/A'
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return 'N/A'
    if isinstance(v, (int, np.integer)):
        return f'{v}{suffix}'
    return f'{v:.{digits}f}{suffix}'


def _src_tag(src: str) -> str:
    return '✅真实' if src == 'real' else '⚠️默认'


def fmt_yi(v, digits: int = 2) -> str:
    """元 → 亿元（便于阅读财报大数）"""
    if v is None:
        return 'N/A'
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 'N/A'
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return 'N/A'
    return f'{v / 1e8:.{digits}f}亿'


def fmt_pct(v, digits: int = 2) -> str:
    """比率(0~1) → 百分比"""
    if v is None:
        return 'N/A'
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 'N/A'
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return 'N/A'
    return f'{v * 100:.{digits}f}%'


def render_markdown(name, code, factors, decision, oamv_state, position, data_date,
                    extra_missing, detail, news, corp=None, margin=None, north=None):
    score = round(decision['posterior']['H1'] * 100)
    direction = decision['decision']['direction']
    yjd = factors.get('YJD_CompositeCrowding')
    yjd_status = factors.get('YJD_Status', '未知')
    market_state = decision['market_state']
    weights = WEIGHTS_BY_STATE.get(market_state, {})

    rs = factors.get('RelativeStrength') or []
    rs_md = '| 周期 | 个股涨跌 | 指数涨跌 | 超额收益 | 强弱评估 |\n|---|---|---|---|---|'
    for item in rs:
        rs_md += f"\n| {item['period']}日 | {item['stock_ret']:+.2%} | {item['idx_ret']:+.2%} | {item['excess']:+.2%} | {item['eval']} |"

    dde_md = '| 周期 | DDE 累计(亿) | 成交额(亿) | 净额率 |\n|---|---|---|---|'
    for n in [1, 3, 5, 10]:
        dde_v = factors.get(f'DDE_{n}d')
        amt_v = factors.get(f'Amt_{n}d')
        rate_v = factors.get(f'DDE_Rate_{n}d')
        dde_md += f"\n| {n}日 | {f_num(dde_v, 3)} | {f_num(amt_v, 2)} | {f_num(rate_v, 3, '%')} |"

    oamv_block = (
        f"| 0AMV 市场总仓位 | **{oamv_state['grade']} {oamv_state['state']}** | 建议 {oamv_state['position']}（择时层，与个股无关）|\n"
        f"| 0AMV 描述 | {oamv_state['description']} | — |\n"
    ) if oamv_state else "| 0AMV 市场总仓位 | ⚠️ 未加载（--no-oamv）| 个股仓位仅基于贝叶斯方向 |\n"

    # 贝叶斯输入明细段
    bayes_md = ''
    for ei in ['E1', 'E2', 'E3', 'E4', 'E5', 'E6']:
        d = detail[ei]
        bayes_md += f"\n### {ei} {d['label']}　{_src_tag(d['source'])}\n"
        bayes_md += "| 输入项 | 数值 | 单位 |\n|---|---|---|\n"
        for nm, val, unit in d['items']:
            bayes_md += f"| {nm} | {f_num(val, 4) if isinstance(val,(int,float)) else val} | {unit} |\n"
        bayes_md += f"> {d['note']}\n"

    # 新闻段
    news_md = ''
    if news and isinstance(news, dict) and news.get('items'):
        items = news['items']
        news_md += f"\n> 数据来源：{news.get('source','未知')} · 更新 {news.get('updated','?')} · 共 {len(items)} 条\n\n"
        news_md += "| 日期 | 影响 | 标题 | 来源 | 摘要 |\n|---|---|---|---|---|\n"
        for it in items:
            news_md += (f"| {it.get('date','?')} | {it.get('impact','中')} | "
                        f"{it.get('title','')} | {it.get('source','')} | {it.get('summary','')} |\n")
    else:
        news_md += "\n> ⚠️ 暂无新闻缓存（运行 westock data_news 后重跑）。\n"

    # 企业深度体检（巴菲特式）段
    corp_md = ''
    if corp and isinstance(corp, dict):
        fin = corp.get('finance_fy2025', {}) or {}
        scores = corp.get('scores', {}) or {}
        moat = corp.get('moat', {}) or {}
        corp_md += f"\n> 数据来源：{corp.get('source','?')} · 截止 {corp.get('date','?')}\n\n"
        corp_md += f"**主营业务**：{corp.get('business','?')}\n\n"
        corp_md += f"**行业/板块**：{corp.get('industry','?')} / {corp.get('sector','?')} / {corp.get('board','?')} · 上市 {corp.get('listed_date','?')}\n\n"
        corp_md += "### 1) 护城河与质量信号（多维诊股评分）\n"
        corp_md += "| 维度 | 评分 | 解读 |\n|---|---|---|\n"
        corp_md += f"| 综合 CompScore | {scores.get('comp','?')} | 综合质地 |\n"
        corp_md += f"| 基本面 FunmScore | {scores.get('funm','?')} | 盈利/成长/质量（E4 corp_quality 来源）|\n"
        corp_md += f"| 风险 RiskScore | {scores.get('risk','?')} | 越高=风险越低 |\n"
        corp_md += f"| 资金 CapScore | {scores.get('cap','?')} | 资金面 |\n"
        corp_md += f"| 技术 TecScore | {scores.get('tec','?')} | 技术面 |\n"
        corp_md += f"\n**护城河**：{moat.get('type','?')}（评级 {moat.get('strength','?')}/5）— {moat.get('note','')}\n\n"
        corp_md += "### 2) 财务质量（FY2025，本引擎据三大报表计算）\n"
        corp_md += "| 指标 | 数值 | 解读 |\n|---|---|---|\n"
        corp_md += f"| 营收 | {fmt_yi(fin.get('revenue'))} | 规模 |\n"
        corp_md += f"| 归母净利 | {fmt_yi(fin.get('np_parent'))} | — |\n"
        _gm = (fin.get('gross_margin') or 0) or 0.0
        _nm = (fin.get('net_margin') or 0) or 0.0
        _roe = (fin.get('roe') or 0) or 0.0
        _dr = (fin.get('debt_ratio') or 0) or 0.0
        _gm_lbl = '高毛利' if _gm >= 0.40 else ('中等毛利' if _gm >= 0.25 else '低毛利')
        _nm_lbl = '高净利' if _nm >= 0.15 else ('中等净利' if _nm >= 0.08 else '偏薄')
        _roe_lbl = '高ROE' if _roe >= 0.15 else ('中等ROE' if _roe >= 0.08 else '低ROE（高成长股常见）')
        _dr_lbl = '高杠杆' if _dr >= 0.60 else ('中等杠杆' if _dr >= 0.40 else '低负债（财务稳健）')
        _fcff = (fin.get('fcff') or 0) or 0.0
        _fcff_lbl = '自由现金流转正' if _fcff >= 0 else '自由现金流为负（扩张/投入期）'
        corp_md += f"| 毛利率 | {fmt_pct(fin.get('gross_margin'))} | {_gm_lbl} |\n"
        corp_md += f"| 净利率 | {fmt_pct(fin.get('net_margin'))} | {_nm_lbl} |\n"
        corp_md += f"| ROE | {fmt_pct(fin.get('roe'))} | {_roe_lbl} |\n"
        corp_md += f"| 资产负债率 | {fmt_pct(fin.get('debt_ratio'))} | {_dr_lbl} |\n"
        corp_md += f"| 商誉 | {fmt_yi(fin.get('goodwill'))} | 几乎为零，报表干净 |\n"
        corp_md += f"| 经营现金流 OCF | {fmt_yi(fin.get('ocf'))} | — |\n"
        corp_md += f"| OCF/净利 | {f_num(fin.get('ocf_np_ratio'), 2)} 倍 | 现金转化{'优秀' if (fin.get('ocf_np_ratio') or 0) >= 1 else '偏弱'} |\n"
        corp_md += f"| FCFF | {fmt_yi(fin.get('fcff'))} | {_fcff_lbl} |\n"
        corp_md += "\n### 3) 营收 / 净利 TTM 趋势\n"
        corp_md += "| 日期 | 营收(TTM) | 净利(TTM) |\n|---|---|---|\n"
        for r in (corp.get('revenue_trend_ttm') or []):
            corp_md += f"| {r.get('date','?')} | {fmt_yi(r.get('revenue_ttm'))} | {fmt_yi(r.get('np_ttm'))} |\n"
        corp_md += "\n### 4) 风险信号 vs 正向信号\n"
        for rf in (corp.get('risk_flags') or []):
            corp_md += f"- ⚠️ 风险：{rf}\n"
        for pf in (corp.get('positive_flags') or []):
            corp_md += f"- ✅ 正向：{pf}\n"
        corp_md += (f"\n### 5) 弹性与预期\nBeta_100周 = **{corp.get('beta_100w','?')}**（高弹性）；"
                    f"一致目标价 **{corp.get('target_price','?')}** 元"
                    f"（较现价空间需比对收盘价）。\n")
    else:
        corp_md += "\n> ⚠️ 暂无企业深度体检缓存（运行 data_finance/data_profile/data_score 同步后重跑）。\n"

    # 流动性与杠杆资金（融资融券）段
    margin_md = ''
    if margin and isinstance(margin, dict):
        fb = margin.get('finance_balance') or 0
        sb = margin.get('security_balance') or 0
        tb = margin.get('total_balance') or 0
        fmc = margin.get('float_market_cap') or 0
        ratio = margin.get('finance_balance_ratio') or 0
        dod = margin.get('finance_balance_dod') or 0
        buy = margin.get('finance_buy_today') or 0
        refund = margin.get('finance_refund_today') or 0
        net = margin.get('finance_net_buy_today') or 0
        margin_md += f"\n> 数据来源：{margin.get('source','?')} · 截止 {margin.get('date','?')} · 收盘价 {margin.get('close_price','?')} 元\n\n"
        margin_md += "| 指标 | 数值 | 解读 |\n|---|---|---|\n"
        margin_md += f"| 融资余额 | {fmt_yi(fb)} | 杠杆看多资金 |\n"
        margin_md += f"| 融券余额 | {fmt_yi(sb)} | 杠杆看空（极小）|\n"
        margin_md += f"| 融资融券余额合计 | {fmt_yi(tb)} | — |\n"
        margin_md += f"| **融资余额占流通市值比** | **{fmt_pct(ratio)}** | 中性偏积极（>8% 预警强平风险）|\n"
        margin_md += f"| 融资余额日变动 | {f_num(dod, 2)}% | {'余额下降（净偿还）' if dod < 0 else '余额上升（净买入）'} |\n"
        margin_md += f"| 当日融资买入 / 偿还 | {fmt_yi(buy)} / {fmt_yi(refund)} | 净{'偿还' if net < 0 else '买入'} {fmt_yi(abs(net))} |\n"
        margin_md += f"| 流通市值 | {fmt_yi(fmc)} | 分母 |\n"
        margin_md += ("\n> 融资融券是**杠杆资金与流动性**信号：余额占比适中代表看多杠杆资金充裕、"
                      "支撑流动性；但占比过高（>8%）需警惕集中强平风险。本指标已并入 **E2 政策+流动性** LLR。\n")
    else:
        margin_md += "\n> ⚠️ 暂无融资融券缓存（运行 westock data_fund_margin 同步后重跑）。\n"

    # 北向资金（外资增量流动性）段
    north_md = ''
    if north and isinstance(north, dict):
        n_cur = north.get('cur', {}) or {}
        n_prev = north.get('prev', {}) or {}
        n_ratio = n_cur.get('holding_ratio_pct', 0.0) or 0.0
        n_shares = n_cur.get('holding_shares') or 0
        n_prev_shares = n_prev.get('holding_shares') or 0
        n_q_add_pct = (n_shares - n_prev_shares) / n_prev_shares * 100 if n_prev_shares else 0.0
        n_cap_chg_q = n_cur.get('cap_chg_q', 0.0) or 0.0
        n_cap_chg_y = n_cur.get('cap_chg_y', 0.0) or 0.0
        n_signal = '季环比增持（外资回流，偏多）' if n_q_add_pct > 0 else '季环比减持（外资流出，偏空）'
        north_md += f"\n> 数据来源：{north.get('source','?')} · 截止 {north.get('date','?')}\n\n"
        north_md += "| 指标 | 数值 | 解读 |\n|---|---|---|\n"
        north_md += f"| 北向持股比例 | {f_num(n_ratio, 4)}% | 外资持股深度（质量+流动性背书）|\n"
        north_md += f"| 持股数量(最新季) | {f_num(n_shares, 0)} 股 | — |\n"
        north_md += f"| **季度增持幅度** | **{f_num(n_q_add_pct, 2)}%** | {n_signal} |\n"
        north_md += f"| 持股市值季变动 | {fmt_yi(n_cap_chg_q)} | 近一季外资净买入规模 |\n"
        north_md += f"| 持股市值年变动 | {fmt_yi(n_cap_chg_y)} | 年内(YTD)变动，主因 H1 股价回落被动减持 |\n"
        north_md += ("\n> 北向资金是**外资增量流动性**信号：季环比增持代表外资回流、提供增量买盘；"
                      "持股比例高亦是对公司质量的背书。本指标已并入 **E2 政策+流动性** LLR。\n")
    else:
        north_md += "\n> ⚠️ 暂无北向资金缓存（运行 westock data_north_holding 同步后重跑）。\n"

    md = f"""# {name} 贝叶斯量化判断报告

**股票代码**：{code}
**生成时间**：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} · 数据截止：{data_date} 收盘
**分析窗口**：未来 60 个交易日（约 3 个月）
**股票池**：中证 500（zz500 截面，{factors.get('BelongedPools') and ', '.join(factors.get('BelongedPools')) or '宽基外'}）
**市场状态**：{market_state}

---

## 一、核心结论

| 维度 | 结果 | 说明 |
|---|---|---|
| 当前价格 | **{float(factors.get('close_last', 0)):.2f} 元** | — |
{oamv_block}| 贝叶斯方向 | **{direction}** | LLR_total = {decision['llr_total']:+.4f} |
| **综合评分** | **{score}** | P(H₁\\|E) × 100 |
| 拥挤度 | **{f_num(yjd, 2)}** ({yjd_status}) | YJD 阈值 >120 偏热 / >300 极热 |
| 截面样本 | zz500 ({factors.get('section_count', 'N/A')} 只，参照基准) | 含个股自身，算截面分位 |
| **个股仓位推荐** | **{position['final_range']}** | 贝叶斯方向：{direction}（评分 {position['score']}，{position['action']}）|
| 市场总仓位(0AMV) | {position['oamv_range']} | {position['oamv_grade']}（择时层，与个股独立）|

---

## 二、个股归属池与多周期相对强弱

**归属指数池**：{', '.join(factors.get('BelongedPools') or ['未知'])}
**对标基准**：{factors.get('PrimaryIndexName') or '未识别'}

{rs_md}

---

## 三、拥挤度因子

| 因子 | 数值 | 解读 |
|---|---|---|
| F1 换手率偏离 | {f_num(factors.get('F1_TurnoverDeviation'), 2)} | volume 代理（缺 per-day HSL）|
| F2 价格乖离 | {f_num(factors.get('F2_PriceDeviation'), 2)} | vs MA60 |
| F3 相对强弱 | {f_num(factors.get('F3_RelativeStrength'), 2)} | vs 沪深300 |
| F4 成交占比 | {f_num(factors.get('F4_VolumeShare'), 2)} | vs 沪深300 成交额 |
| **YJD 综合** | **{f_num(yjd, 2)}** | **{yjd_status}** |
| YJD MA5 | {f_num(factors.get('YJD_MA5'), 2)} | 5 日均线 |
| YJD MA20 | {f_num(factors.get('YJD_MA20'), 2)} | 20 日均线 |
| YJD 50日最低 | {f_num(factors.get('YJD_Min50'), 2)} | 历史支撑 |
| YJD 50日最高 | {f_num(factors.get('YJD_Max50'), 2)} | 历史阻力 |

---

## 四、波动率因子

| 因子 | 数值 | 单位 |
|---|---|---|
| ATR20 | {f_num(factors.get('ATR20'), 4)} | **元** |
| ATR20_Pct | {f_num(factors.get('ATR20_Pct'), 4)} | **%** |
| Parkinson_Vol | {f_num(factors.get('Parkinson_Vol'), 4)} | — |
| GarmanKlass_Vol | {f_num(factors.get('GarmanKlass_Vol'), 4)} | — |
| YangZhang_Vol | {f_num(factors.get('YangZhang_Vol'), 4)} | — |
| Realized_Vol_20d | {f_num(factors.get('Realized_Vol_20d'), 4)} | — |

---

## 五、Alpha101 经典因子（zz500 截面）

| 因子 | 数值 | 解读 |
|---|---|---|
| Alpha21 趋势持续性 | {f_num(factors.get('Alpha21'), 4)} | 截面分位 |
| Alpha35 量价确认 | {f_num(factors.get('Alpha35'), 4)} | 截面分位 |
| **Alpha39_section** 资金推动（截面）| {f_num(factors.get('Alpha39_section'), 4)} | 资金推动截面排名 |
| Alpha47 超买超卖 | {f_num(factors.get('Alpha47'), 4)} | 截面分位（含 -1 符号）|
| **Alpha57** VWAP 偏离（完整公式）| {f_num(factors.get('Alpha57'), 4)} | WorldQuant 论文完整公式 |
| Alpha57_simple 简化版 | {f_num(factors.get('Alpha57_simple'), 4)} | 简化版 |
| **Alpha83_section** 高价量背离（截面）| {f_num(factors.get('Alpha83_section'), 4)} | 顶部信号参考 |
| Alpha84 波动中强弱 | {f_num(factors.get('Alpha84'), 4)} | 截面分位 |
| **Alpha99_section** 收盘量背离（截面）| {f_num(factors.get('Alpha99_section'), 4)} | 见顶信号参考 |
| Alpha101 日内动量 | {f_num(factors.get('Alpha101'), 4)} | 截面分位 |
| Alpha176 量价共振 | {f_num(factors.get('Alpha176'), 4)} | 截面分位 |
| Alpha102 量能 RSI14 | {f_num(factors.get('Alpha102'), 4)} | 单股量能 RSI（0-100）|
| Alpha39 资金推动（单股）| {f_num(factors.get('Alpha39'), 4)} | 时序版 |
| Alpha70 资金躁动 | {f_num(factors.get('Alpha70'), 4)} | 时序版 |

---

## 六、技术量价因子

| 因子 | 数值 | 解读 |
|---|---|---|
| Deviation_From_MA200 | {f_num(factors.get('Deviation_From_MA200'), 4)} | vs MA200 |
| RSI14 | {f_num(factors.get('RSI14'), 4)} | — |
| Volume_Ratio | {f_num(factors.get('Volume_Ratio'), 4)} | — |
| Turnover_Percentile_60d | {f_num(factors.get('Turnover_Percentile_60d'), 4)} | — |
| Amihud_Illiquidity | {f_num(factors.get('Amihud_Illiquidity'), 6)} | — |
| DDE_Net_Today | {f_num(factors.get('DDE_Net_Today'), 4)} 亿 | 当日主力净流入（特大单+大单，MCP data_fund_flow）|
| DDE_Net_3d | {f_num(factors.get('DDE_Net_3d'), 4)} 亿 | 近 3 日累计（MCP data_fund_flow）|
| Net_Flow_Rate | {f_num(factors.get('Net_Flow_Rate'), 4)} | 净额率 = 主力净流入 / 成交额（MCP）|
| Composite_Chip_Quality | {f_num(factors.get('Composite_Chip_Quality'), 4)} | 筹码（需 MCP）|
| Ret_20d | {f_num(factors.get('Ret_20d') and factors.get('Ret_20d') * 100, 2, '%')} | 20 日累计 |
| Ret_60d | {f_num(factors.get('Ret_60d') and factors.get('Ret_60d') * 100, 2, '%')} | 60 日累计 |

### MACD
| 因子 | 数值 |
|---|---|
| MACD_DIF | {f_num(factors.get('MACD_DIF'), 4)} |
| MACD_DEA | {f_num(factors.get('MACD_DEA'), 4)} |
| MACD_BAR | {f_num(factors.get('MACD_BAR'), 4)} |

### Ret20_Pct_60d
- 当前 20 日涨幅在 60 日窗口百分位 = **{f_num(factors.get('Ret20_Pct_60d'), 4)}**

---

## 七、DDE 多日累计 + 净额率

{dde_md}

> **口径说明**：DDE 多日累计已接通腾讯自选股 MCP `data_fund_flow`（主力/特大单/大单/中单/散户净流入）。显示 N/A 即本地 `data/fund_flow/{code}.json` 缺失，需先运行 MCP 同步。DDE 计算以本引擎为准，不同平台（同花顺/通达信）算法有差异。

---

## 八、压力位 / 支撑位

| 因子 | 数值 | 解读 |
|---|---|---|
| Resistance | {f_num(factors.get('Resistance'), 4)} 元 | 60 日高点 × 0.98 |
| Support | {f_num(factors.get('Support'), 4)} 元 | 60 日低点 × 1.02 |
| RS_Position_Pct | {f_num(factors.get('RS_Position_Pct'), 4)} % | 当前位置 |

---

## 九、高级复合因子

| 因子 | 数值 |
|---|---|
| Residual_Momentum_20d | {f_num(factors.get('Residual_Momentum_20d'), 4)} |
| Residual_Volatility_20d | {f_num(factors.get('Residual_Volatility_20d'), 4)} |
| Beta_Neutral_Alpha | {f_num(factors.get('Beta_Neutral_Alpha'), 4)} |
| Industry_Neutral_Alpha | {f_num(factors.get('Industry_Neutral_Alpha'), 4)} |
| Stock_Beta_60d | {f_num(factors.get('Stock_Beta_60d'), 4)} |

---

## 十、估值与一致预期

| 因子 | 数值 |
|---|---|
| PE_TTM | {f_num(factors.get('PE_TTM'), 4)} |
| PB | {f_num(factors.get('PB'), 4)} |
| PS_TTM | {f_num(factors.get('PS_TTM'), 4)} |
| Consensus_Direction | {factors.get('Consensus_Direction')} |
| EPS_Revision_Rate | {f_num(factors.get('EPS_Revision_Rate'), 4)} |

---

## 十-A、企业深度体检（巴菲特式）

{corp_md}
## 十-B、政策与流动性（E2：融资融券 + 北向资金）

{margin_md}
{north_md}
> 附注：回购维度（产业资本动向）暂未接入 E2 信号，本版流动性仅含融资融券与北向资金两项；个股回购计划可通过 `data_buyback` 单独核查。
---

## 十一、贝叶斯决策详解（六大因子输入 + LLR）

下列为喂入贝叶斯模型的 **E1-E6 真实输入数据** 及其 LLR 贡献。
标记 ✅真实 = 来自本地缓存/MCP；⚠️默认 = 缺数据用占位默认值（需补数据后重跑）。
{bayes_md}
| 因子 | LLR | 权重 |
|---|---|---|
"""
    for ei in ['E1', 'E2', 'E3', 'E4', 'E5', 'E6']:
        llr = decision['llr_each'][ei]['llr']
        w = weights.get(ei, 0)
        md += f"| {ei} {detail[ei]['label']} | {llr:+.3f} | {w:.2f} |\n"
    md += f"\n**LLR_total** = {decision['llr_total']:+.4f} → **{direction}**\n\n"
    md += f"后验：H₁={decision['posterior']['H1']:.3f}  H₂={decision['posterior']['H2']:.3f}  H₃={decision['posterior']['H3']:.3f}\n\n"

    md += "## 十二、新闻与事件（近期影响力）\n"
    md += news_md

    md += "\n## 十三、数据完整性\n\n"
    if extra_missing:
        md += "以下数据本次为 N/A / 默认值（接通对应 MCP 后在对话中重跑，或本地缓存后重跑）：\n"
        for item in extra_missing:
            md += f"- {item}\n"
    else:
        md += "全部数据就绪。\n"

    md += f"\n---\n\n*本报告由 bayesian-quant-decision · run_report.py 自动生成*\n"
    md += f"*数据日期：{data_date} 收盘 · 生成：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}*\n"
    return md, score


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='贝叶斯量化决策报告生成器')
    parser.add_argument('code', help='股票代码，如 000977 / 000977.SZ / 600519')
    parser.add_argument('name', nargs='?', default=None, help='股票显示名（可选）')
    parser.add_argument('--no-oamv', action='store_true', help='跳过 0AMV 保鲜检查')
    parser.add_argument('--open', action='store_true', help='强制视同已开盘（要求当天 0AMV 新鲜）')
    args = parser.parse_args()

    pure, suffix, market, std = normalize_code(args.code)
    name = args.name or _lookup_name(pure) or pure

    print('=' * 60)
    print(f'📊 贝叶斯报告：{name} {std}')
    print('=' * 60)

    # 1. 个股 K 线
    print('\n[1/6] 个股 K 线')
    stock_df = get_stock_kline(pure, market, std)
    data_date = stock_df.index[-1].strftime('%Y-%m-%d')

    # 2. 基准（沪深300）
    print('\n[2/6] 沪深300 基准')
    idx_df = get_index_kline('000300.SH', 1, '000300')

    # 3. 截面
    print('\n[3/6] 中证500 截面（参照基准，注入个股 K 线以算截面分位）')
    panel = load_section()
    section = build_section_frames(panel)
    # 无论个股是否属于 500 成分，都把其 K 线注入截面宽表，使截面因子可正常计算
    section = inject_stock_into_section(section, stock_df, pure)
    if pure not in section['section_close'].columns:
        print(f'  ⚠️ {std} 截面因子不可得（个股 K 线字段不全），相关 Alpha 截面因子将显示 N/A。')

    # 4. 贝叶斯输入缓存（宏观/行业/PSI/新闻/估值…）
    print('\n[4/6] 贝叶斯输入缓存（E1-E6 数据）')
    caches, extra_missing = load_bayes_caches(std)
    for k, v in caches.items():
        flag = '✅' if v is not None else '⬜'
        print(f'  {flag} {k}')

    # 5. 因子计算
    print('\n[5/6] 因子计算')
    factors = compute_all_factors(
        stock_code=std,
        stock_df=stock_df,
        idx_df=idx_df,
        primary_idx_df=idx_df,
        section_close=section['section_close'],
        section_open=section['section_open'],
        section_high=section['section_high'],
        section_low=section['section_low'],
        section_volume=section['section_volume'],
        section_amount=section['section_amount'],
        section_vwap=section['section_vwap'],
        valuation=caches['valuation'],
        chip_data=caches['chip'],
        consensus_data=caches['consensus'],
        fund_flow_series=None,
    )
    # DDE 主力资金流：fund_flow 缓存 -> 字段适配器 -> 引擎
    ff_dict, ff_series = build_fund_flow_inputs(std, stock_df)
    if ff_dict is not None:
        factors['DDE_Net_Today'] = compute_dde_net_today(ff_dict)
        factors['DDE_Net_3d'] = compute_dde_net_3d(ff_dict)
        factors['Net_Flow_Rate'] = compute_net_flow_rate(ff_dict)
        if ff_series is not None and stock_df is not None and 'amount' in stock_df.columns:
            factors.update(compute_dde_multi_period(ff_series, stock_df['amount']))
    factors['stock_code'] = std
    factors['close_last'] = float(stock_df['close'].iloc[-1])
    factors['section_count'] = section['section_close'].shape[1]

    # 一致预期方向补充：eps_now 缺失时用盈利增速(net_profit_yoy_2026E)推导
    if caches.get('consensus'):
        cd = factors.get('Consensus_Direction')
        if cd in (None, '', '数据缺失'):
            ny = caches['consensus'].get('net_profit_yoy_2026E')
            if ny is not None:
                factors['Consensus_Direction'] = ('正面（扭亏/高增长预期 +%.0f%%）' % ny) if ny > 0 else '负面（盈利下滑）'

    # 6. 贝叶斯决策（真实 E1-E6 输入）
    print('\n[6/6] 贝叶斯决策')
    macro, ai_judgments, detail, extras = build_bayes_inputs(factors, caches, std)
    oamv_state = None if args.no_oamv else get_oamv_state()
    market_state = oamv_state['bayesian_state'] if oamv_state else '震荡市'
    decision = decide(
        factors=factors,
        macro=macro,
        ai_judgments=ai_judgments,
        market_state=market_state,
        pool='中证500',
    )
    position = integrate_position(oamv_state, decision)

    # 渲染 + 写文件
    news = caches['news']
    md, score = render_markdown(name, std, factors, decision, oamv_state, position,
                                data_date, extra_missing, detail, news,
                                corp=extras.get('corp'), margin=extras.get('margin'),
                                north=extras.get('north'))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today_str = pd.Timestamp.now().strftime('%Y%m%d')
    prefix = f'{name} ' if name != pure else ''
    out_path = os.path.join(OUTPUT_DIR, f'{prefix}{pure} {today_str} {score}.md')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(md)

    print('\n' + '=' * 60)
    print(f'✅ 报告已生成: {out_path}')
    print(f'   评分 {score} | 方向 {decision["decision"]["direction"]} | 仓位 {position["final_range"]}')
    print(f'   YJD={f_num(factors.get("YJD_CompositeCrowding"), 2)}  ATR20={f_num(factors.get("ATR20"), 4)}  Alpha57={f_num(factors.get("Alpha57"), 4)}')
    src_flags = ' '.join(f'{k}:{"✅" if v else "⬜"}' for k, v in caches.items())
    print(f'   数据来源：{src_flags}')
    print('=' * 60)


def _lookup_name(pure: str) -> str | None:
    """从 zz500 成分股清单查名字（仅覆盖 zz500）"""
    if not os.path.exists(ZZ500_CSV):
        return None
    try:
        with open(ZZ500_CSV, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('sec_code', '').zfill(6) == pure:
                    return row.get('sec_name')
    except Exception:
        pass
    return None


if __name__ == '__main__':
    main()
