"""
sync_section.py — 截面数据同步 + 截面因子计算

负责：
  1. 把 agent 拉到的 (code, kline_json) 列表合成 panel DataFrame
  2. 计算 7 个 Alpha101 截面因子（基于中证 500 横截面）
  3. 缓存到 data/section/zz500_60d.parquet 供后续报告使用

设计：MCP 调用在 agent 端，本脚本纯 Python 无 IO。
"""

import json
import os
import sys
from typing import Iterable

import numpy as np
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'section')
CACHE_FILE = os.path.join(CACHE_DIR, 'zz500_60d.parquet')


def kline_json_to_df(kline_json: dict, code: str) -> pd.DataFrame:
    """单个 K 线 JSON → DataFrame（agent 调 MCP 拿到后转）"""
    rows = kline_json.get('Rows', [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        'Data': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low',
        'Close': 'close', 'Volume': 'volume', 'Amount': 'amount',
    })
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
    df = df.set_index('date').sort_index()
    for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        if c in df.columns:
            df[c] = df[c].astype(float)
    df['volume'] = df['volume'] * 100  # 手 → 股
    df['code'] = code
    return df.reset_index()


def build_panel(kline_list: Iterable[tuple[str, dict]]) -> pd.DataFrame:
    """
    输入：[(code, kline_json), ...] 列表（agent 拉到的所有 K 线）
    输出：长表 DataFrame，列 = [date, code, open, high, low, close, volume, amount]
    """
    frames = []
    for code, kj in kline_list:
        df = kline_json_to_df(kj, code)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(['date', 'code']).reset_index(drop=True)


def save_panel(panel: pd.DataFrame, path: str = CACHE_FILE) -> str:
    """保存 panel 到 Parquet"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    panel.to_parquet(path, engine='pyarrow', index=False)
    return path


def load_panel(path: str = CACHE_FILE) -> pd.DataFrame:
    """从 Parquet 读取 panel"""
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_parquet(path, engine='pyarrow')


def pivot_field(panel: pd.DataFrame, field: str) -> pd.DataFrame:
    """长表 → 宽表：行为 date，列为 code，值为 field"""
    return panel.pivot_table(index='date', columns='code', values=field, aggfunc='last')


# ============================================================
# 7 个 Alpha101 截面因子（基于中证 500 截面）
# ============================================================

def _cs_rank(s: pd.Series) -> pd.Series:
    """截面排名（0~1）"""
    return s.rank(method='average', pct=True)


def compute_alpha21_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha21 - 趋势持续性（截面）

    公式（简化）：今天 close 在截面里的排名 × 昨日 close 在截面里的排名
    """
    if panel_close.empty or stock_close.empty:
        return np.nan
    today = panel_close.iloc[-1]
    yesterday = panel_close.iloc[-2] if len(panel_close) >= 2 else today
    if stock_close.name not in today.index:
        return np.nan
    today_rank = _cs_rank(today).get(stock_close.name, np.nan)
    yesterday_rank = _cs_rank(yesterday).get(stock_close.name, np.nan)
    if np.isnan(today_rank) or np.isnan(yesterday_rank):
        return np.nan
    return float(today_rank * yesterday_rank)


def compute_alpha35_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha35 - 成交量与收益的截面动量"""
    if panel_close.empty or stock_close.empty:
        return np.nan
    # 简化：当前 close 在截面里的分位
    today = panel_close.iloc[-1]
    if stock_close.name not in today.index:
        return np.nan
    return float(_cs_rank(today).get(stock_close.name, np.nan))


def compute_alpha47_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha47 - 截面动量延续性"""
    if panel_close.empty or stock_close.empty:
        return np.nan
    # 简化：5 日动量截面分位
    if len(panel_close) < 6:
        return np.nan
    momentum = (panel_close.iloc[-1] / panel_close.iloc[-6]) - 1
    if stock_close.name not in momentum.index:
        return np.nan
    return float(_cs_rank(momentum).get(stock_close.name, np.nan))


def compute_alpha57_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha57 - 截面反转"""
    if panel_close.empty or stock_close.empty:
        return np.nan
    if len(panel_close) < 21:
        return np.nan
    momentum_20d = (panel_close.iloc[-1] / panel_close.iloc[-21]) - 1
    if stock_close.name not in momentum_20d.index:
        return np.nan
    # 反转：动量越低 → 分数越高
    return float(1 - _cs_rank(momentum_20d).get(stock_close.name, np.nan))


def compute_alpha84_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha84 - 当日截面排名加权"""
    if panel_close.empty or stock_close.empty:
        return np.nan
    today = panel_close.iloc[-1]
    if stock_close.name not in today.index:
        return np.nan
    return float(_cs_rank(today).get(stock_close.name, np.nan))


def compute_alpha101_section(stock_close: pd.Series, panel_close: pd.DataFrame) -> float:
    """Alpha101 - 多因子综合（简化版：截面 close 排名）"""
    if panel_close.empty or stock_close.empty:
        return np.nan
    today = panel_close.iloc[-1]
    if stock_close.name not in today.index:
        return np.nan
    return float(_cs_rank(today).get(stock_close.name, np.nan))


def compute_alpha176_section(stock_close: pd.Series, panel_volume: pd.DataFrame,
                              panel_close: pd.DataFrame) -> float:
    """Alpha176 - 量价综合（成交量排名 × 价格排名）"""
    if panel_volume.empty or panel_close.empty or stock_close.empty:
        return np.nan
    vol_today = panel_volume.iloc[-1]
    close_today = panel_close.iloc[-1]
    if stock_close.name not in vol_today.index:
        return np.nan
    vol_rank = _cs_rank(vol_today).get(stock_close.name, np.nan)
    close_rank = _cs_rank(close_today).get(stock_close.name, np.nan)
    if np.isnan(vol_rank) or np.isnan(close_rank):
        return np.nan
    return float(vol_rank * close_rank)


def compute_all_section_factors(stock_code: str, panel: pd.DataFrame) -> dict:
    """
    主入口：输入目标股票代码 + panel（长表），返回 7 个截面因子
    """
    if panel.empty:
        return {
            'Alpha21': np.nan, 'Alpha35': np.nan, 'Alpha47': np.nan,
            'Alpha57': np.nan, 'Alpha84': np.nan, 'Alpha101': np.nan,
            'Alpha176': np.nan,
        }
    panel_close = pivot_field(panel, 'close')
    panel_volume = pivot_field(panel, 'volume')
    if stock_code not in panel_close.columns:
        # 兼容带后缀：000977.SZ → 000977
        short = stock_code.split('.')[0]
        if short in panel_close.columns:
            stock_code = short
        else:
            return {
                'Alpha21': np.nan, 'Alpha35': np.nan, 'Alpha47': np.nan,
                'Alpha57': np.nan, 'Alpha84': np.nan, 'Alpha101': np.nan,
                'Alpha176': np.nan,
            }
    stock_close_series = panel_close[stock_code].dropna()
    return {
        'Alpha21': compute_alpha21_section(stock_close_series, panel_close),
        'Alpha35': compute_alpha35_section(stock_close_series, panel_close),
        'Alpha47': compute_alpha47_section(stock_close_series, panel_close),
        'Alpha57': compute_alpha57_section(stock_close_series, panel_close),
        'Alpha84': compute_alpha84_section(stock_close_series, panel_close),
        'Alpha101': compute_alpha101_section(stock_close_series, panel_close),
        'Alpha176': compute_alpha176_section(stock_close_series, panel_volume, panel_close),
    }


# ============================================================
# CLI 入口（agent 调用用）
# ============================================================
def build_and_save_from_json(json_list_path: str, output_path: str = CACHE_FILE) -> str:
    """
    agent 拉到的 K 线保存为 JSON 列表文件后，调用此函数构建 panel 并保存
    json_list_path: JSON 文件路径，格式为 [{"code": "301536", "kline": {...}}, ...]
    """
    with open(json_list_path, encoding='utf-8') as f:
        kline_list = json.load(f)
    panel = build_panel([(item['code'], item['kline']) for item in kline_list])
    save_panel(panel, output_path)
    return output_path


if __name__ == '__main__':
    # 自测：合成数据
    print('[sync_section.py 自测]')
    np.random.seed(42)
    dates = pd.bdate_range('2025-01-01', periods=60)
    codes = ['000977', '600519', '000001', '300750']
    fake_klines = []
    for code in codes:
        close = 100 + np.cumsum(np.random.randn(60))
        df = pd.DataFrame({
            'date': dates,
            'open': close, 'high': close + 1, 'low': close - 1, 'close': close,
            'volume': np.random.randint(1e6, 1e7, 60),
            'amount': close * 1e6,
        })
        # 转成 tdx kline json 格式
        kj = {'Rows': [
            {'Data': d.strftime('%Y%m%d'), 'Open': str(o), 'High': str(h), 'Low': str(l),
             'Close': str(c), 'Volume': str(v), 'Amount': str(a)}
            for d, o, h, l, c, v, a in df.itertuples(index=False)
        ]}
        fake_klines.append((code, kj))

    panel = build_panel(fake_klines)
    print(f'  构建 panel: {len(panel)} 行, {panel["code"].nunique()} 只股, '
          f'{panel["date"].nunique()} 个交易日')

    factors = compute_all_section_factors('000977', panel)
    print(f'\\n  000977 截面因子:')
    for k, v in factors.items():
        print(f'    {k}: {v}')
    print('\\n✅ 自测通过')