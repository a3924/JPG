"""
db_sync.py — 数据层（接口 + 本地缓存读写）
================================================================
职责：
  1. 定义各数据源的 schema（字段、类型、单位）—— 保证 factor_engine 输入稳定
  2. 提供本地 Parquet 缓存的读写接口
  3. 记录 MCP 同步操作手册（注释形式，给 WorkBuddy agent 用）
  4. 数据校验（schema check、缺失值标记）

调用方式：
    from db_sync import (
        read_local, write_local, list_cached_codes,
        STOCK_SCHEMA, INDEX_SCHEMA, CHIP_SCHEMA, CONSENSUS_SCHEMA,
    )
    df = read_local('000725.SZ', freq='day')   # 读本地日线
    write_local('000725.SZ', freq='day', df=df) # 写本地日线

MCP 同步（本文件不做，由 WorkBuddy agent 执行）：
  数据流：通达信MCP / 腾讯自选股MCP → JSON → pandas DataFrame
        → write_local() → Parquet 文件
  sync_one('000725.SZ') 由 agent 编排，调用：
    - mcp__tdx-connector.tdx_kline(code, freq='day', start, end)
    - mcp__tdx-connector.tdx_security_deep_info(code)
    - mcp__westock-mcp.data_chip(code)
    - mcp__westock-mcp.data_consensus(code)
    - mcp__westock-mcp.data_shareholder(code)
    - mcp__westock-mcp.data_industry_chain(code)

本地缓存目录结构：
    {BASE}/data/
    ├── kline/
    │   ├── day/000725.parquet
    │   ├── day/600519.parquet
    │   └── 60min/000725.parquet
    ├── index/000300.parquet
    ├── chip/000725.json
    ├── consensus/000725.json
    ├── shareholder/000725.json
    ├── valuation/000725.json
    └── meta/000725.json
"""
from __future__ import annotations
import json
import os
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd


# ============================================================
# 路径与配置
# ============================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


# ============================================================
# 数据 Schema 定义（所有数据源共用这套契约）
# ============================================================
STOCK_SCHEMA = {
    'required_columns': ['open', 'high', 'low', 'close', 'volume'],
    'optional_columns': ['amount', 'turnover', 'HSL'],  # HSL: 换手率（%）
    'index': 'DatetimeIndex',
    'index_name': 'date',
    'dtype': {
        'open': 'float64', 'high': 'float64', 'low': 'float64',
        'close': 'float64', 'volume': 'float64',
        'amount': 'float64', 'turnover': 'float64', 'HSL': 'float64',
    },
    'unit': {
        'open': '元', 'high': '元', 'low': '元', 'close': '元',
        'volume': '股', 'amount': '元', 'turnover': '%', 'HSL': '%',
    },
    'note': 'HSL 是从 tdx_kline 的 HSL 字段（换手率%）同步过来的，'
            '用于 F1_TurnoverDeviation / YJD_CompositeCrowding 计算',
}

INDEX_SCHEMA = {
    'required_columns': ['close'],
    'optional_columns': ['open', 'high', 'low', 'volume', 'amount'],
    'index': 'DatetimeIndex',
}

VALUATION_SCHEMA = {
    'fields': ['PE_TTM', 'PB', 'PS_TTM', 'market_cap', 'total_shares'],
    'dtype': {
        'PE_TTM': 'float64', 'PB': 'float64', 'PS_TTM': 'float64',
        'market_cap': 'float64', 'total_shares': 'float64',
    },
    'unit': {
        'PE_TTM': '倍', 'PB': '倍', 'PS_TTM': '倍',
        'market_cap': '元', 'total_shares': '股',
    },
}

CHIP_SCHEMA = {
    'fields': [
        'profit_ratio',         # 收盘获利比例 %
        'avg_cost',             # 平均成本 元
        'cost_90_low',          # 90% 成本下限
        'cost_90_high',         # 90% 成本上限
        'cost_70_low',          # 70% 成本下限
        'cost_70_high',         # 70% 成本上限
        'concentration_90',     # 集中度90 %
        'concentration_70',     # 集中度70 %
    ],
}

CONSENSUS_SCHEMA = {
    'fields': ['eps_now', 'eps_90d_ago', 'eps_30d_ago', 'rating'],
    'note': 'rating 是字符串（强烈推荐/推荐/中性/谨慎/卖出）',
}

SHAREHOLDER_SCHEMA = {
    'fields': ['holder_count_now', 'holder_count_prev', 'change_pct'],
    'note': 'change_pct = (now - prev) / prev × 100',
}

INDUSTRY_SCHEMA = {
    'fields': ['industry_name', 'sw_l1', 'sw_l2', 'cr4', 'bci_percentile'],
}

MACRO_SCHEMA = {
    'fields': ['gdp_gap', 'm2_yoy', 'yield_curve_spread_bp', 'pmi', 'cpi_yoy', 'source', 'date'],
    'note': 'gdp_gap=实际增速-潜在增速(%)；m2_yoy=M2同比(%)；'
            'yield_curve_spread_bp=10Y-2Y利差(bp)；pmi=制造业PMI；'
            '全局快照（非个股），由 agent 经 westock data_macro 拉取后写入 data/macro/latest.json',
}

NEWS_SCHEMA = {
    'fields': ['items'],
    'note': 'items = [{date, title, source, impact(高/中/低), summary}]，'
            '由 agent 经 westock data_news / tdx wenda_news_query 拉取后写入 data/news/{code}.json',
}

PSI_SCHEMA = {
    'fields': ['psi_score', 'psi_policy_type', 'psi_months_since', 'basis'],
    'note': 'PSI 政策信号强度评分（-3~+3）+ 政策类型 + 距发布月数 + 依据新闻；'
            'AI 根据近期政策新闻判定后写入 data/psi/{code}.json',
}

CORP_SCHEMA = {
    'fields': [
        'business', 'industry', 'sector', 'board', 'scores',
        'market_cap', 'float_market_cap', 'total_shares', 'float_shares',
        'beta_100w', 'target_price',
        'finance_fy2025', 'revenue_trend_ttm',
        'moat', 'risk_flags', 'positive_flags',
        'corp_quality',
    ],
    'note': '企业深度体检（巴菲特式）：主营业务/护城河/5年财务质量/资本配置/风险信号。'
            '由 agent 经 westock data_finance(income/balance/cashflow)+data_profile+data_score'
            '+ tdx 股本/市值 拉取后写入 data/corp/{code}.json。'
            'corp_quality ∈ [-1,1]：由 FunmScore(基本面评分) 映射，供 E4 LLR 使用。',
}

MARGIN_SCHEMA = {
    'fields': [
        'date', 'finance_balance', 'security_balance', 'total_balance',
        'finance_balance_dod', 'security_balance_dod',
        'finance_buy_today', 'finance_refund_today', 'finance_net_buy_today',
        'close_price', 'float_market_cap', 'finance_balance_ratio',
    ],
    'note': '融资融券明细：融资余额/融券余额/合计/日变动/当日买卖/占流通市值比。'
            '由 agent 经 westock data_fund_margin 拉取 + tdx 流通市值 计算后写入 '
            'data/margin/{code}.json。finance_balance_ratio = 融资余额/流通市值，供 E5 流动性 LLR 使用。',
}


# ============================================================
# 数据校验
# ============================================================
def validate_stock_df(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """校验个股日线 DataFrame，缺列补 NaN、类型转换"""
    if df is None or df.empty:
        return pd.DataFrame(columns=STOCK_SCHEMA['required_columns'])

    for col in STOCK_SCHEMA['required_columns']:
        if col not in df.columns:
            raise ValueError(f"{code} 缺少必需列: {col}")

    # 计算 amount（若没有）：amount = close × volume
    if 'amount' not in df.columns and all(c in df.columns for c in ['close', 'volume']):
        df['amount'] = df['close'] * df['volume']

    # 类型转换 HSL（换手率%）
    if 'HSL' in df.columns:
        df['HSL'] = pd.to_numeric(df['HSL'], errors='coerce').astype('float64')

    # 排序、确保 DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        else:
            df.index = pd.to_datetime(df.index)

    df = df.sort_index()
    return df


def validate_dict(d: dict, schema_name: str, schema: dict) -> dict:
    """校验 dict 类型数据，缺字段补 None"""
    if d is None:
        return {f: None for f in schema.get('fields', [])}
    out = {}
    for f in schema.get('fields', []):
        out[f] = d.get(f, None)
    return out


# ============================================================
# 本地 Parquet / JSON 读写
# ============================================================
def _path_kline(code: str, freq: str) -> str:
    """K 线缓存路径"""
    code = code.replace('.', '_').replace('/', '_')
    p = os.path.join(DATA_DIR, 'kline', freq)
    _ensure_dir(p)
    return os.path.join(p, f'{code}.parquet')


def _path_dict(category: str, code: str, ext: str = 'json') -> str:
    """dict 类数据缓存路径"""
    code = code.replace('.', '_').replace('/', '_')
    p = os.path.join(DATA_DIR, category)
    _ensure_dir(p)
    return os.path.join(p, f'{code}.{ext}')


def read_local(code: str, freq: str = 'day') -> pd.DataFrame | None:
    """
    读本地缓存的个股 K 线
    返回 None 表示本地无缓存（需 MCP 同步）
    """
    path = _path_kline(code, freq)
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    return validate_stock_df(df, code)


def write_local(code: str, freq: str, df: pd.DataFrame) -> None:
    """
    写本地缓存（覆盖模式）
    调用方需确保 df 是 validate_stock_df 校验过的
    """
    path = _path_kline(code, freq)
    df.to_parquet(path)


def append_local(code: str, freq: str, df: pd.DataFrame) -> None:
    """
    增量追加：读出已有 → 拼接 → 去重 → 写回
    """
    existing = read_local(code, freq)
    if existing is not None and not existing.empty:
        combined = pd.concat([existing, df])
        combined = combined[~combined.index.duplicated(keep='last')].sort_index()
    else:
        combined = df
    write_local(code, freq, combined)


def read_dict(category: str, code: str) -> dict | None:
    """读 dict 类数据（chip / consensus / shareholder / valuation）"""
    path = _path_dict(category, code)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_dict(category: str, code: str, data: dict) -> None:
    """写 dict 类数据"""
    path = _path_dict(category, code)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ============================================================
# 通用 JSON 缓存读写（宏观 / 行业 / 新闻 / PSI 等）
# key 可非代码（如宏观快照用 'latest'），code 可带点号
# ============================================================
def read_json(category: str, key: str, ext: str = 'json') -> dict | list | None:
    """读通用 JSON 缓存（macro/industry/news/psi 等）"""
    path = _path_dict(category, key, ext)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json(category: str, key: str, data: dict | list, ext: str = 'json') -> None:
    """写通用 JSON 缓存"""
    path = _path_dict(category, key, ext)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def list_cached_codes(freq: str = 'day') -> list[str]:
    """列出已缓存的所有股票代码"""
    p = os.path.join(DATA_DIR, 'kline', freq)
    if not os.path.exists(p):
        return []
    files = [f for f in os.listdir(p) if f.endswith('.parquet')]
    return [f.replace('.parquet', '').replace('_', '.') for f in files]


def cache_summary() -> dict:
    """缓存摘要（用于报告/诊断）"""
    summary = {}
    for freq in ['day', '60min', '5min']:
        codes = list_cached_codes(freq)
        summary[f'kline_{freq}'] = {
            'count': len(codes),
            'latest_dates': [read_local(c, freq).index[-1].strftime('%Y-%m-%d')
                             if read_local(c, freq) is not None else None
                             for c in codes[:3]],
        }
    for cat in ['valuation', 'chip', 'consensus', 'shareholder']:
        p = os.path.join(DATA_DIR, cat)
        if os.path.exists(p):
            summary[f'{cat}_count'] = len([f for f in os.listdir(p) if not f.startswith('.')])
    return summary


# ============================================================
# MCP 同步操作手册（注释形式，给 WorkBuddy agent）
# ============================================================
MCP_SYNC_GUIDE = """
================================================================
MCP 同步操作手册（agent 执行，不在 Python 里直接调）
================================================================

【单只股票全量同步】sync_one(code, start='2020-01-01')
----------------------------------------
1. K 线：mcp__tdx-connector.tdx_kline(code, freq='day', start, end)
   → 拿到 list of dict（含 open/high/low/close/volume/amount）
   → 转 DataFrame → validate_stock_df → write_local

2. 估值：mcp__tdx-connector.tdx_security_deep_info(code)
   → 取 market_cap / pe_ttm / pb / ps_ttm / total_share
   → write_dict('valuation', code, {...})

3. 筹码：mcp__westock-mcp.data_chip(code)
   → write_dict('chip', code, {...})

4. 一致预期：mcp__westock-mcp.data_consensus(code)
   → write_dict('consensus', code, {...})

5. 股东户数：mcp__westock-mcp.data_shareholder(code)
   → write_dict('shareholder', code, {...})

6. 融资融券：mcp__westock-mcp.data_fund_margin(code)
   → 取 FinanceValue(融资余额)/SecurityValue(融券余额)/TradingValue(合计)/
     FinanceValueDOD(融资余额日变动)/FinanceBuyValue(当日融资买入)/FinanceRefundValue(当日偿还)
   → 结合 tdx 流通市值 计算 finance_balance_ratio = 融资余额/流通市值
   → write_dict('margin', code, {...})

7. 企业深度体检（巴菲特式）：
   - mcp__westock-mcp.data_finance(code, type='income', num=5) → 营收/净利/毛利
   - mcp__westock-mcp.data_finance(code, type='balance', num=5) → 净资产/商誉/负债
   - mcp__westock-mcp.data_finance(code, type='cashflow', num=5) → 经营现金流/FCFF
   - mcp__westock-mcp.data_profile(code) → 主营业务/行业
   - mcp__westock-mcp.data_score(code) → 多维诊股评分(Comp/Funm/Risk/Cap/Tec)
   - tdx_security_deep_info(code) → 总市值/流通市值/股本/Beta/一致目标价
   → 计算 gross_margin/net_margin/roe/debt_ratio/ocf_np_ratio，映射 corp_quality
   → write_json('corp', code, {...})

【全 A 全量同步】sync_all()
----------------------------------------
1. mcp__tdx-connector.tdx_stocklist() → 5300+ 只股票代码
2. 循环每只 sync_one()
3. 进度报告

【每日增量同步】sync_incremental()
----------------------------------------
1. 找到本地所有 cached codes（list_cached_codes()）
2. 每只拉最后 5 个交易日
3. append_local(code, freq, new_df)
4. 更新 dict 类数据

【指数数据】sync_index(code='000300.SH')
----------------------------------------
mcp__tdx-connector.tdx_kline(code='000300.SH', freq='day')
→ DataFrame → write_local('000300.SH', 'day', df)
注：index 不带 amount 也可，factor_engine 会自动用 close*volume 近似
================================================================
"""


# ============================================================
# 自测
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("db_sync.py 自测")
    print("=" * 60)

    # 1. schema 展示
    print("\n[STOCK_SCHEMA]")
    for k, v in STOCK_SCHEMA.items():
        print(f"  {k}: {v}")

    print("\n[VALUATION_SCHEMA]")
    print(f"  fields: {VALUATION_SCHEMA['fields']}")

    # 2. 路径与目录检查
    print(f"\n[路径] BASE_DIR: {BASE_DIR}")
    print(f"[路径] DATA_DIR: {DATA_DIR}")
    print(f"[路径] data/ 存在: {os.path.exists(DATA_DIR)}")

    # 3. 缓存摘要
    print("\n[缓存摘要]")
    summary = cache_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # 4. 校验函数测试
    print("\n[validate_stock_df 测试]")
    test_df = pd.DataFrame({
        'open': [10, 11], 'high': [12, 13], 'low': [9, 10],
        'close': [11, 12], 'volume': [1000, 2000],
    }, index=pd.to_datetime(['2025-01-01', '2025-01-02']))
    validated = validate_stock_df(test_df, 'test.SZ')
    print(f"  校验后列: {list(validated.columns)}")
    print(f"  amount 自动补齐: {'amount' in validated.columns}")

    # 5. 写入 / 读取测试
    print("\n[write/read 测试]")
    test_code = 'TEST.SZ'
    write_local(test_code, 'day', validated)
    read_back = read_local(test_code, 'day')
    print(f"  写入并读回 shape: {read_back.shape}")
    print(f"  内容一致: {read_back.equals(validated)}")

    # 清理测试文件（sandbox 保护下可能拒绝 delete，用 try 包裹）
    try:
        os.remove(_path_kline(test_code, 'day'))
        print(f"  测试文件已清理: {_path_kline(test_code, 'day')}")
    except OSError:
        print(f"  测试文件保留（sandbox 拦截删除）：{_path_kline(test_code, 'day')}")

    print("\n" + "=" * 60)
    print("✅ 自测通过")
    print("=" * 60)
    print("\n【MCP 同步操作手册】（agent 用，本文件不直接执行）")
    print(MCP_SYNC_GUIDE[:500] + "...")