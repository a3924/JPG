"""
bulk_sync_zz500.py — 批量同步中证 500 成分股 K 线（pytdx 直连）

用法：
    python bulk_sync_zz500.py [csv_path] [days]

默认：
    csv_path = references/zz500_constituents_full.csv
    days = 60
"""
import csv
import os
import shutil
import sys
import time

import pandas as pd
from pytdx.hq import TdxHq_API

# 默认路径（优先尝试几个常见位置）
_DEFAULT_CSV_CANDIDATES = [
    r'D:\AILIANGHUA\贝叶斯工具\references\zz500_constituents_full.csv',
    os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..',
        'AILIANGHUA', '贝叶斯工具', 'references', 'zz500_constituents_full.csv'
    )),
    os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..', '..',
        '贝叶斯工具_纯工具包_20260728', '..', '贝叶斯工具', 'references',
        'zz500_constituents_full.csv'
    )),
]


def _resolve_default_csv() -> str:
    for c in _DEFAULT_CSV_CANDIDATES:
        if os.path.exists(c):
            return c
    return _DEFAULT_CSV_CANDIDATES[0]  # 兜底


DEFAULT_CSV = _resolve_default_csv()
DEFAULT_DAYS = 60
PANEL_OUT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', 'data', 'section', 'zz500_60d.parquet'
))
PANEL_OUT_TPL = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', 'data', 'section', 'zz500_60d_{ymd}.parquet'
))


def panel_path_for_date(ymd: str) -> str:
    """指定 YYYYMMDD 的输出文件路径"""
    return PANEL_OUT_TPL.format(ymd=ymd)


def bulk_sync_to(ymd: str, csv_path: str = None, days: int = 60) -> tuple[pd.DataFrame, str]:
    """
    同步并保存到带日期的截面文件 + 同时复制一份 legacy
    返回 (panel_df, date_file_path)
    """
    df = bulk_sync(csv_path or DEFAULT_CSV, days)
    date_path = panel_path_for_date(ymd)
    df.to_parquet(date_path, engine='pyarrow', index=False)
    print(f'[bulk_sync] 已存到带日期文件: {date_path}')
    # 同时写一份 legacy（run_report 兼容）
    shutil.copy2(date_path, PANEL_OUT)
    print(f'[bulk_sync] 已同步 legacy 文件: {PANEL_OUT}')
    return df, date_path

# 通达信可用服务器（实测可连）
SERVERS = [
    ('60.12.136.250', 7709),
    ('115.238.56.198', 7709),
    ('180.153.18.170', 7709),
    ('123.125.108.14', 7709),
]


def fetch_one(api, code: str, market: int, days: int = 60) -> list:
    """拉单只股最近 N 日 K 线"""
    try:
        data = api.get_security_bars(9, market, code, 0, days)
        return data or []
    except Exception as e:
        print(f'  ⚠️ {code} 拉取失败: {e}')
        return []


def bulk_sync(csv_path: str, days: int = 60) -> pd.DataFrame:
    """批量同步中证 500 成分股 → 返回 panel DataFrame"""
    # 1. 读成分股清单
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        constituents = [(row['sec_code'], int(row['market']), row['sec_name'])
                        for row in reader]
    print(f'[bulk_sync] 读取 {len(constituents)} 只成分股')

    # 2. 用多服务器轮询（避免单 IP 被限流）
    api = TdxHq_API()
    server_idx = 0
    connected = False
    for _ in range(len(SERVERS)):
        host, port = SERVERS[server_idx]
        try:
            api.connect(host, port)
            connected = True
            print(f'[bulk_sync] 连通服务器 {host}:{port}')
            break
        except Exception as e:
            print(f'  ⚠️ {host}:{port} 连不上: {e}')
            server_idx = (server_idx + 1) % len(SERVERS)
    if not connected:
        raise RuntimeError('所有通达信服务器都连不上')

    # 3. 批量拉
    all_frames = []
    start = time.time()
    fail_count = 0
    for i, (code, market, name) in enumerate(constituents):
        bars = fetch_one(api, code, market, days)
        if not bars:
            fail_count += 1
            continue
        # 转 DataFrame
        rows = []
        for bar in bars:
            rows.append({
                'date': pd.Timestamp(bar['year'], bar['month'], bar['day']),
                'code': code,
                'name': name,
                'open': float(bar['open']),
                'high': float(bar['high']),
                'low': float(bar['low']),
                'close': float(bar['close']),
                'volume': float(bar['vol']) * 100,  # pytdx vol 单位是手，×100 = 股
                'amount': float(bar['amount']),
            })
        all_frames.append(pd.DataFrame(rows))

        # 进度
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start
            speed = (i + 1) / elapsed
            print(f'  [{i+1}/{len(constituents)}] {speed:.1f} 股/秒, '
                  f'失败 {fail_count}, 已用 {elapsed:.1f}s')

        # 每 200 只换服务器（防限流）
        if (i + 1) % 200 == 0 and i + 1 < len(constituents):
            api.disconnect()
            server_idx = (server_idx + 1) % len(SERVERS)
            host, port = SERVERS[server_idx]
            try:
                api.connect(host, port)
                print(f'[bulk_sync] 切换服务器 {host}:{port}')
            except Exception:
                pass

    api.disconnect()

    # 4. 合成 panel
    panel = pd.concat(all_frames, ignore_index=True)
    panel = panel.sort_values(['date', 'code']).reset_index(drop=True)
    print(f'\n[bulk_sync] 完成: {len(panel)} 行, '
          f'{panel["code"].nunique()} 只股, '
          f'{panel["date"].nunique()} 个交易日, '
          f'失败 {fail_count} 只')

    # 5. 保存
    os.makedirs(os.path.dirname(PANEL_OUT), exist_ok=True)
    panel.to_parquet(PANEL_OUT, engine='pyarrow', index=False)
    print(f'[bulk_sync] 保存到: {PANEL_OUT}')
    return panel


if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DAYS
    if not os.path.exists(csv_path):
        print(f'❌ CSV 不存在: {csv_path}')
        sys.exit(1)
    print(f'[bulk_sync] CSV: {csv_path}')
    print(f'[bulk_sync] Days: {days}')
    panel = bulk_sync(csv_path, days)
    print(f'\n✅ 同步完成')