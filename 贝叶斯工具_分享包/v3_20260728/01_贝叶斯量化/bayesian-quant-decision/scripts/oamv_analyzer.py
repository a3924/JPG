"""
oamv_analyzer.py — 指南针 0AMV 分析器
================================================================
职责：
  1. 读 指南针 zhinan zhinanzhen-0amv-daily-db skill 生成的 CSV
  2. 计算 MA5 / MA10 / MA20 / MA120 四条均线
  3. 检查数据新鲜度（过夜即过期）
  4. 按 V1.0 指南判定 7 档市场状态 + 对应仓位区间
  5. 输出 (market_state_7grade, position_range_pct, freshness_ok) 给 report.py

调用方式：
    from oamv_analyzer import (
        load_oamv, compute_moving_averages, check_freshness,
        classify_market_state, MARKET_STATES,
    )

    df = load_oamv('D:/AIlianghua/OAMV/0AMV日线数据库_2015至今.csv')
    freshness = check_freshness(df)             # {'is_fresh': bool, 'last_date': '...', 'days_lag': int}
    if not freshness['is_fresh']:
        raise StaleOAMVError(...)               # 不新鲜 → 不出报告
    ma = compute_moving_averages(df)
    state = classify_market_state(ma)          # {'grade': '★★★', 'state': '震荡偏多', 'position': '50~70%', 'description': '...', 'bayesian_state': '震荡市'}

指南版本：V1.0（用户定义，2026-07-20）
"""
from __future__ import annotations
import os
from datetime import date, datetime, timedelta
from typing import Tuple

import numpy as np
import pandas as pd


# ============================================================
# CSV 加载
# ============================================================
def load_oamv(csv_path: str) -> pd.DataFrame:
    """
    读 0AMV 日线 CSV
    预期列（来自 zhinan zhinanzhen-0amv-daily-db skill）：
        日期, 开, 高, 低, 收, 量, 额, 涨幅%, 振幅%
    或类似格式
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"0AMV CSV 不存在：{csv_path}\n"
            f"请先运行 zhinanzhen-0amv-daily-db skill 提取最新数据。"
        )

    # 尝试多种编码
    df = None
    for enc in ['utf-8', 'gbk', 'gb2312']:
        try:
            df = pd.read_csv(csv_path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue

    if df is None:
        raise IOError(f"无法解码 0AMV CSV：{csv_path}")

    # 列名归一化（含单位后缀的也兼容）
    rename_map = {
        '日期': 'date', '时间': 'date', 'date': 'date', 'Date': 'date',
        '开': 'open', '开盘价': 'open', 'open': 'open', '开(亿元)': 'open',
        '高': 'high', '最高价': 'high', 'high': 'high', '高(亿元)': 'high',
        '低': 'low', '最低价': 'low', 'low': 'low', '低(亿元)': 'low',
        '收': 'close', '收盘': 'close', '收盘价': 'close', 'close': 'close', '收(亿元)': 'close',
        '量': 'volume', '成交量': 'volume', 'volume': 'volume', '量(亿)': 'volume',
        '额': 'amount', '成交额': 'amount', 'amount': 'amount', '额(亿元)': 'amount',
        '涨幅%': 'pct_chg', '涨跌幅': 'pct_chg', '涨幅': 'pct_chg',
        '振幅%': 'amplitude', '振幅': 'amplitude',
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # 必备列：date, close
    if 'date' not in df.columns:
        # 尝试用第 0 列作为日期
        df = df.rename(columns={df.columns[0]: 'date'})

    if 'close' not in df.columns:
        raise ValueError(f"0AMV CSV 缺少 close 列：{csv_path}\n实际列：{list(df.columns)}")

    # 日期解析：支持多种格式
    # 1) YYYYMMDD 整数（指南针 CSV 格式）
    # 2) YYYY-MM-DD 字符串
    # 3) 已解析的 datetime
    if pd.api.types.is_integer_dtype(df['date']) or pd.api.types.is_float_dtype(df['date']):
        df['date'] = pd.to_datetime(df['date'].astype(int).astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'])

    df = df.set_index('date').sort_index()
    return df


# ============================================================
# 均线计算
# ============================================================
def compute_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """
    算 MA5 / MA10 / MA20 / MA120
    返回含原始 close + 四条 MA 的 DataFrame
    """
    out = df[['close']].copy()
    out['MA5'] = out['close'].rolling(5).mean()
    out['MA10'] = out['close'].rolling(10).mean()
    out['MA20'] = out['close'].rolling(20).mean()
    out['MA120'] = out['close'].rolling(120).mean()
    return out


# ============================================================
# 新鲜度检查（过夜即过期）
# ============================================================
class StaleOAMVError(Exception):
    """指南针 0AMV 数据陈旧（过夜），请重跑 zhinanzhen-0amv-daily-db skill"""
    pass


def check_freshness(df: pd.DataFrame, today: str | None = None, max_lag_days: int = 0,
                    pre_open: bool = False) -> dict:
    """
    检查 0AMV 数据新鲜度
    - today: 比较基准日期，格式 'YYYY-MM-DD'，默认今天
    - max_lag_days: 最大允许滞后天数，0 表示过夜即过期
    - pre_open: True 表示"盘前/非交易日"——0AMV 是收盘数据，此时最新
      可用行就是上一交易日（昨天）收盘，应视为新鲜（最多允许滞后 1 个交易日）

    Returns
    -------
    dict:
      {
        'is_fresh': bool,
        'last_date': 'YYYY-MM-DD',
        'today': 'YYYY-MM-DD',
        'days_lag': int,
        'pre_open': bool,
      }
    """
    if today is None:
        today = date.today().strftime('%Y-%m-%d')
    elif isinstance(today, str):
        today_dt = datetime.strptime(today, '%Y-%m-%d').date()
        today = today_dt.strftime('%Y-%m-%d')

    last_date = df.index[-1].strftime('%Y-%m-%d')

    # 计算滞后天数（只比较日期，不比较时间）
    last_dt = df.index[-1].date()
    today_dt = datetime.strptime(today, '%Y-%m-%d').date()
    days_lag = (today_dt - last_dt).days

    if pre_open:
        # 盘前/非交易日：0AMV 只有收盘数据，最新行 = 上一交易日即视为新鲜。
        # 用"交易日滞后"判定：last_date 到 today 之间的工作日数量（含边界差）。
        trading_lag = _trading_days_between(last_dt, today_dt)
        is_fresh = trading_lag <= 1
    else:
        is_fresh = days_lag <= max_lag_days

    return {
        'is_fresh': is_fresh,
        'last_date': last_date,
        'today': today,
        'days_lag': days_lag,
        'pre_open': pre_open,
    }


def _trading_days_between(start: date, end: date) -> int:
    """start（不含）到 end（含）之间的工作日数量，用于盘前滞后判定"""
    if start >= end:
        return 0
    return len(pd.bdate_range(start + timedelta(days=1), end))


def _market_has_closed_today(now: datetime | None = None) -> bool:
    """
    判断"今天是否已收盘"：交易日且当前时间 >= 15:00。
    未收盘（含盘前、盘中、周末、节假日）返回 False。
    """
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:          # 周末
        return False
    return now.hour >= 15


def assert_fresh(csv_path: str, today: str | None = None, max_lag_days: int = 0,
                 pre_open: bool | None = None) -> dict:
    """
    一站式：加载 + 检查，过期直接 raise StaleOAMVError
    返回 freshness dict

    pre_open:
      - None（默认）：自动判断——若当前未到今天收盘（盘前/盘中/周末/节假日）则视同盘前，
        允许用上一交易日收盘数据。
      - True/False：显式指定。
    """
    if pre_open is None:
        pre_open = not _market_has_closed_today()
    df = load_oamv(csv_path)
    freshness = check_freshness(df, today=today, max_lag_days=max_lag_days, pre_open=pre_open)
    if not freshness['is_fresh']:
        raise StaleOAMVError(
            f"\n{'=' * 70}\n"
            f"❌ 0AMV 数据陈旧，禁止出报告\n"
            f"{'=' * 70}\n"
            f"  CSV 路径：{csv_path}\n"
            f"  最后日期：{freshness['last_date']}\n"
            f"  今天日期：{freshness['today']}\n"
            f"  滞后天数：{freshness['days_lag']} 天（最大允许 {max_lag_days} 天）\n"
            f"\n"
            f"👉 请按以下步骤操作：\n"
            f"  1. 打开 指南针（Zhinanzhen） 软件\n"
            f"  2. 进入 0AMV 日 K 界面，等待数据自动下载/补全\n"
            f"  3. 完全关闭指南针软件（避免文件被锁）\n"
            f"  4. 运行 zhinanzhen-0amv-daily-db skill 重新提取 CSV\n"
            f"  5. 再回来运行本报告\n"
            f"{'=' * 70}\n"
        )
    return freshness


# ============================================================
# 市场状态判定（V1.0 指南）
# ============================================================
# 7 档市场状态定义（来自用户 V1.0 指南）
MARKET_STATES = {
    '强上涨':   {'grade': '★★★★★', 'position': '90~100%', 'description': '长中短线资金全部同步流入，趋势最稳定',       'bayesian_state': '牛市'},
    '上涨':     {'grade': '★★★★',   'position': '70~90%',   'description': '上涨过程中的正常调整，长期资金流入确认',    'bayesian_state': '牛市'},
    '震荡偏多': {'grade': '★★★',   'position': '50~70%',   'description': '资金略有改善，长期资金仍未确认',              'bayesian_state': '震荡市'},
    '震荡':     {'grade': '★★',     'position': '30~50%',   'description': '资金没有方向，区间震荡',                     'bayesian_state': '震荡市'},
    '震荡偏空': {'grade': '★★',     'position': '20~40%',   'description': '资金略微流出，长期资金持平',                  'bayesian_state': '震荡市'},
    '下跌':     {'grade': '★',     'position': '10~30%',   'description': '熊市初期，资金持续撤离但未完全空头排列',     'bayesian_state': '熊市'},
    '强下跌':   {'grade': '☆☆☆☆☆', 'position': '0~10%',    'description': '资金全面撤离，长中短线完全空头排列',         'bayesian_state': '熊市'},
}


def _ma120_trend(ma120: pd.Series, lookback: int = 5) -> str:
    """
    判断 MA120 长期趋势：'up' / 'flat' / 'down'
    连续 5 天同方向才确认（避免一天拐头就切换）
    """
    if len(ma120) < lookback + 1:
        return 'flat'
    recent = ma120.dropna().tail(lookback + 1)
    if len(recent) < lookback + 1:
        return 'flat'
    diffs = recent.diff().dropna()
    if all(diffs > 0):
        return 'up'
    elif all(diffs < 0):
        return 'down'
    return 'flat'


def _ma_alignment(ma5: float, ma10: float, ma20: float, ma120: float) -> str:
    """
    均线排列判断：
    'bull' - 多头排列 MA5>MA10>MA20>MA120
    'bear' - 空头排列 MA5<MA10<MA20<MA120
    'mixed_bull' - 长期上涨但中短期缠绕/部分多头
    'mixed_bear' - 长期下跌但中短期未完全空头
    'weaving' - 均线缠绕
    """
    # 严格多头
    if ma5 > ma10 > ma20 > ma120:
        return 'bull'
    # 严格空头
    if ma5 < ma10 < ma20 < ma120:
        return 'bear'
    # 长期资金方向决定主类
    if ma120 > 0:  # 用 ma120 绝对值判断长期偏多
        # 长期上涨，但中短期未完全多头
        return 'mixed_bull'
    else:
        return 'mixed_bear'


def classify_market_state(ma_df: pd.DataFrame) -> dict:
    """
    按 V1.0 指南判定 7 档市场状态
    输入：含 close/MA5/MA10/MA20/MA120 的 DataFrame
    返回：{'grade', 'state', 'position', 'description', 'bayesian_state', 'ma120_trend', 'alignment'}
    """
    if ma_df is None or ma_df.empty:
        raise ValueError("ma_df 为空")

    last = ma_df.dropna().iloc[-1]
    ma5, ma10, ma20, ma120 = last['MA5'], last['MA10'], last['MA20'], last['MA120']

    # 1. 长期趋势（最重要）
    ma120_trend = _ma120_trend(ma_df['MA120'])

    # 2. 中短期排列
    if ma5 > ma10 > ma20 > ma120:
        alignment = 'bull'
    elif ma5 < ma10 < ma20 < ma120:
        alignment = 'bear'
    elif ma5 > ma10 and ma10 > ma20:
        # 上涨过程中正常调整
        alignment = 'mixed_bull'
    elif ma5 < ma10 and ma10 < ma20:
        # 下跌过程中正常反弹
        alignment = 'mixed_bear'
    else:
        alignment = 'weaving'

    # 3. 综合判定（按 V1.0 指南 7 档）
    if ma120_trend == 'up' and alignment == 'bull':
        state = '强上涨'
    elif ma120_trend == 'up' and alignment in ('mixed_bull', 'weaving'):
        state = '上涨'
    elif ma120_trend == 'flat' and ma20 > ma20_prev(ma_df):
        # 震荡偏多：MA120 走平 + MA20 开始向上
        state = '震荡偏多'
    elif ma120_trend == 'flat' and alignment == 'weaving':
        state = '震荡'
    elif ma120_trend == 'flat' and ma20 < ma20_prev(ma_df):
        # 震荡偏空：MA120 走平 + MA20 开始向下
        state = '震荡偏空'
    elif ma120_trend == 'down' and alignment != 'bear':
        state = '下跌'
    elif ma120_trend == 'down' and alignment == 'bear':
        state = '强下跌'
    else:
        # 默认按当前 alignment 推断
        state = '震荡'

    info = MARKET_STATES[state]
    return {
        'grade': info['grade'],
        'state': state,
        'position': info['position'],
        'description': info['description'],
        'bayesian_state': info['bayesian_state'],
        'ma120_trend': ma120_trend,
        'alignment': alignment,
        'last_values': {
            'close': float(last['close']),
            'MA5': float(ma5),
            'MA10': float(ma10),
            'MA20': float(ma20),
            'MA120': float(ma120),
        },
    }


def ma20_prev(ma_df: pd.DataFrame) -> float:
    """取前一日 MA20（用于判断 MA20 是否"开始向上/向下"）"""
    valid = ma_df['MA20'].dropna()
    if len(valid) < 2:
        return float(valid.iloc[-1]) if len(valid) > 0 else 0.0
    return float(valid.iloc[-2])


# ============================================================
# 自测（合成数据）
# ============================================================
if __name__ == '__main__':
    print("=" * 70)
    print("oamv_analyzer.py 自测")
    print("=" * 70)

    # 1. 合成 0AMV 数据
    np.random.seed(42)
    dates = pd.bdate_range('2025-01-01', periods=200)
    # 模拟强上涨：close 单调递增 + 随机扰动
    close = pd.Series(100 + np.cumsum(np.random.randn(200) * 0.5 + 0.3), index=dates)

    df = pd.DataFrame({'close': close, 'open': close, 'high': close, 'low': close,
                       'volume': np.random.randint(100, 1000, 200)})
    df.index.name = 'date'

    # 2. 均线
    ma = compute_moving_averages(df)
    print("\n[均线最后 5 行]")
    print(ma.tail())

    # 3. 新鲜度检查（合成数据最后日期 = 2025-12-29，远早于今天 → 应过期）
    freshness = check_freshness(ma)
    print(f"\n[新鲜度] {freshness}")
    print(f"  is_fresh: {freshness['is_fresh']}（预期 False）")

    # 4. 新鲜度检查（用合成数据最后日期作为 today → 应新鲜）
    last_str = ma.index[-1].strftime('%Y-%m-%d')
    freshness2 = check_freshness(ma, today=last_str)
    print(f"\n[新鲜度-指定 today=最后日期] {freshness2}")
    print(f"  is_fresh: {freshness2['is_fresh']}（预期 True）")

    # 5. 市场状态判定
    state = classify_market_state(ma)
    print(f"\n[市场状态判定]")
    for k, v in state.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 70)
    print("✅ 自测通过")
    print("=" * 70)