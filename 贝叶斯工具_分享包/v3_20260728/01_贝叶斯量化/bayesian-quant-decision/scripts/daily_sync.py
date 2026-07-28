# -*- coding: utf-8 -*-
"""
daily_sync.py — 每日截面 / 宏观数据同步 + GitHub 上传
================================================================
设计动机：
  - 中证 500 截面（zz500_60d.parquet）和宏观快照（macro/latest.json）每天都需要更新
  - 文件名改为带日期（zz500_60d_YYYYMMDD.parquet / macro_YYYYMMDD.json）
  - 自动检查：今天已同步 → 跳过；今天未同步 → 拉取
  - 拉取完成后自动推 GitHub（团队/多机协同用，可选关闭）

子命令：
  python daily_sync.py check          # 仅检查状态，不更新不推送
  python daily_sync.py sync           # 同步（截面+宏观），不推送
  python daily_sync.py push           # 仅推送（截面+宏观）到 GitHub
  python daily_sync.py sync-and-push  # 同步 + 推送（最常用，默认行为）

宏观数据由 agent 调 MCP 后传入（从 stdin 或 --macro-file 路径读取），例：
  echo '{"gdp_gap":1.2,...}' | python daily_sync.py sync

输出文件：
  {skill}/data/section/zz500_60d_YYYYMMDD.parquet
  {skill}/data/macro/macro_YYYYMMDD.json
  兼容保留（无日期）的：
  {skill}/data/section/zz500_60d.parquet   ← 同 latest
  {skill}/data/macro/latest.json
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, date
from typing import Optional

# ============================================================
# 路径与常量
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(SKILL_DIR, 'data')

# 截面文件目录
SECTION_DIR = os.path.join(DATA_DIR, 'section')
SECTION_TPL = 'zz500_60d_{ymd}.parquet'
SECTION_LEGACY = 'zz500_60d.parquet'

# 宏观文件目录
MACRO_DIR = os.path.join(DATA_DIR, 'macro')
MACRO_TPL = 'macro_{ymd}.json'
MACRO_LEGACY = 'latest.json'

# GitHub 仓库路径（用户本地 clone 的位置）
DEFAULT_GH_REPO = r'D:\ai-oamv'  # 用户的本地 clone
GITHUB_DATA_DIR = 'data'  # 推送到仓库的子目录

PYTDX_SERVERS = [
    ('60.12.136.250', 7709),
    ('115.238.56.198', 7709),
    ('180.153.18.170', 7709),
    ('123.125.108.14', 7709),
]


# ============================================================
# 时间工具
# ============================================================
def today_ymd() -> str:
    """返回今天 YYYYMMDD"""
    return date.today().strftime('%Y%m%d')


def today_dash() -> str:
    """返回今天 YYYY-MM-DD"""
    return date.today().strftime('%Y-%m-%d')


def parse_ymd_from_filename(fname: str, tpl_with_date: str | None = None, prefix: str = 'zz500_60d_') -> str | None:
    """从文件名抓出 YYYYMMDD，找不到返回 None。
    默认 prefix='zz500_60d_'（截面文件前缀）
    兼容 macro 文件时传 prefix='macro_'
    """
    base = os.path.basename(fname)
    if not prefix or not base.startswith(prefix):
        return None
    # 找到 prefix 后的 8 位数字
    rest = base[len(prefix):]
    digits = ''
    for c in rest:
        if c.isdigit():
            digits += c
        else:
            break
    if len(digits) >= 8:
        return digits[:8]
    return None


# ============================================================
# 截面（zz500）状态检查与同步
# ============================================================
def list_section_files() -> list[str]:
    """data/section/ 下所有 zz500 相关 parquet 文件"""
    if not os.path.isdir(SECTION_DIR):
        return []
    files = [f for f in os.listdir(SECTION_DIR)
             if f.endswith('.parquet') and f.startswith('zz500_60d')]
    return sorted(files)


def get_local_section_date() -> str | None:
    """本地最新截面文件的日期 YYYYMMDD"""
    files = list_section_files()
    if not files:
        return None
    # 找日期最大的
    best = None
    for f in files:
        d = parse_ymd_from_filename(f, SECTION_TPL)
        if d and (best is None or d > best):
            best = d
    # 兼容老文件
    if SECTION_LEGACY in files and (best is None or SECTION_LEGACY.replace('.parquet', '').endswith('_60d')):
        # 没找到带日期的最新的，用 legacy 文件
        mtime = os.path.getmtime(os.path.join(SECTION_DIR, SECTION_LEGACY))
        legacy_date = datetime.fromtimestamp(mtime).strftime('%Y%m%d')
        if best is None or legacy_date > best:
            best = legacy_date
    return best


def get_remote_section_date(repo_dir: str) -> str | None:
    """从 GitHub 仓库查找最新截面文件名"""
    remote_dir = os.path.join(repo_dir, GITHUB_DATA_DIR, 'section')
    if not os.path.isdir(remote_dir):
        return None
    files = [f for f in os.listdir(remote_dir)
             if f.endswith('.parquet') and f.startswith('zz500_60d')]
    best = None
    for f in files:
        d = parse_ymd_from_filename(f, SECTION_TPL)
        if d and (best is None or d > best):
            best = d
    return best


def sync_section(ymd: str) -> str:
    """跑 bulk_sync_zz500 并保存为带日期的文件，返回输出路径"""
    out_path = os.path.join(SECTION_DIR, SECTION_TPL.format(ymd=ymd))
    os.makedirs(SECTION_DIR, exist_ok=True)

    # 调 bulk_sync_zz500.bulk_sync(...) 直接拿到 DataFrame
    import bulk_sync_zz500
    csv_path = bulk_sync_zz500.DEFAULT_CSV
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f'成分股 CSV 缺失: {csv_path}')

    print(f'[section] 拉取 zz500 截面 → {out_path}')
    panel = bulk_sync_zz500.bulk_sync(csv_path, 60)
    panel.to_parquet(out_path, engine='pyarrow', index=False)
    print(f'[section] ✅ 已保存: {out_path}（{os.path.getsize(out_path):,} bytes）')

    # 同时写一份 legacy 兼容（无日期）
    legacy_path = os.path.join(SECTION_DIR, SECTION_LEGACY)
    shutil.copy2(out_path, legacy_path)
    print(f'[section] ✅ 兼容副本: {legacy_path}')

    return out_path


# ============================================================
# 宏观数据 状态检查与同步
# ============================================================
def list_macro_files() -> list[str]:
    """data/macro/ 下所有 macro 相关 json 文件"""
    if not os.path.isdir(MACRO_DIR):
        return []
    files = [f for f in os.listdir(MACRO_DIR)
             if f.startswith('macro_') and f.endswith('.json')]
    files += ['latest.json'] if os.path.exists(os.path.join(MACRO_DIR, 'latest.json')) else []
    return sorted(set(files))


def get_local_macro_date() -> str | None:
    """本地最新宏观文件的日期 YYYYMMDD"""
    files = list_macro_files()
    if not files:
        return None
    best = None
    for f in files:
        if f == 'latest.json':
            mtime = os.path.getmtime(os.path.join(MACRO_DIR, f))
            d = datetime.fromtimestamp(mtime).strftime('%Y%m%d')
        else:
            d = parse_ymd_from_filename(f, 'macro_{ymd}.json', prefix='macro_')
        if d and (best is None or d > best):
            best = d
    return best


def get_remote_macro_date(repo_dir: str) -> str | None:
    """从 GitHub 仓库查找最新宏观文件"""
    remote_dir = os.path.join(repo_dir, GITHUB_DATA_DIR, 'macro')
    if not os.path.isdir(remote_dir):
        return None
    files = [f for f in os.listdir(remote_dir)
             if f.startswith('macro_') and f.endswith('.json')]
    best = None
    for f in files:
        d = parse_ymd_from_filename(f, 'macro_{ymd}.json', prefix='macro_')
        if d and (best is None or d > best):
            best = d
    return best


def sync_macro(ymd: str, macro_data: dict) -> str:
    """把宏观数据写到带日期的文件"""
    out_path = os.path.join(MACRO_DIR, MACRO_TPL.format(ymd=ymd))
    os.makedirs(MACRO_DIR, exist_ok=True)

    payload = dict(macro_data)
    payload.setdefault('date', today_dash())
    payload.setdefault('source', payload.get('source', 'westock data_macro (unknown)'))

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f'[macro]  ✅ 已保存: {out_path}（{os.path.getsize(out_path):,} bytes）')

    # 兼容副本
    legacy_path = os.path.join(MACRO_DIR, MACRO_LEGACY)
    shutil.copy2(out_path, legacy_path)
    print(f'[macro]  ✅ 兼容副本: {legacy_path}')

    return out_path


def read_macro_from_stdin_or_file(path: Optional[str]) -> dict | None:
    """读宏观数据。从 stdin 或文件读 JSON"""
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f'macro 文件不存在: {path}')
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    # 尝试 stdin
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError as e:
                raise ValueError(f'stdin JSON 解析失败: {e}')
    return None


# ============================================================
# GitHub 上传
# ============================================================
def push_to_github(repo_dir: str, ymd: str, msg: str | None = None) -> bool:
    """把今天的截面+宏观 push 到 GitHub"""
    section_local = os.path.join(SECTION_DIR, SECTION_TPL.format(ymd=ymd))
    macro_local = os.path.join(MACRO_DIR, MACRO_TPL.format(ymd=ymd))

    if not os.path.exists(section_local):
        print(f'[push] ❌ 截面文件缺失: {section_local}')
        return False
    if not os.path.exists(macro_local):
        print(f'[push] ❌ 宏观文件缺失: {macro_local}')
        return False

    if not os.path.isdir(repo_dir):
        print(f'[push] ❌ GitHub 本地仓库不存在: {repo_dir}')
        return False

    # 复制到 GitHub 仓库
    gh_section = os.path.join(repo_dir, GITHUB_DATA_DIR, 'section')
    gh_macro = os.path.join(repo_dir, GITHUB_DATA_DIR, 'macro')
    os.makedirs(gh_section, exist_ok=True)
    os.makedirs(gh_macro, exist_ok=True)

    for src in [section_local,
                os.path.join(SECTION_DIR, SECTION_LEGACY),
                macro_local,
                os.path.join(MACRO_DIR, MACRO_LEGACY)]:
        if not os.path.exists(src):
            continue
        dst_name = os.path.basename(src)
        if dst_name.startswith('zz500_60d_') or dst_name == SECTION_LEGACY:
            dst = os.path.join(gh_section, dst_name)
        else:
            dst = os.path.join(gh_macro, dst_name)
        shutil.copy2(src, dst)

    # git add + commit + push
    git = shutil.which('git')
    if not git:
        print('[push] ❌ git 不在 PATH')
        return False

    commit_msg = msg or f'daily-sync: 截面+宏观数据 {ymd}'

    try:
        # 确保 git user.name/email 配置（避免首次 clone 后没配）
        subprocess.run([git, 'config', 'user.name', 'WorkBuddy'],
                       cwd=repo_dir, check=False, capture_output=True)
        subprocess.run([git, 'config', 'user.email', 'ai@local'],
                       cwd=repo_dir, check=False, capture_output=True)

        subprocess.run([git, 'add', 'data/'], cwd=repo_dir, check=True, capture_output=True)
        status = subprocess.run([git, 'status', '--short', '--', 'data/'],
                                cwd=repo_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print('[push] 数据未变化，无需提交')
            return True

        subprocess.run([git, 'commit', '-m', commit_msg], cwd=repo_dir,
                       check=True, capture_output=True)
        push = subprocess.run([git, '-c', 'http.sslVerify=false', 'push', 'origin', 'main'],
                              cwd=repo_dir, capture_output=True, text=True)
        if push.returncode == 0:
            print(f'[push] ✅ 推送成功: {commit_msg}')
            print(f'       {push.stdout.strip()}')
            return True
        else:
            print(f'[push] ❌ push 失败: {push.stderr}')
            return False
    except subprocess.CalledProcessError as e:
        print(f'[push] ❌ git 错误: {e}')
        return False


# ============================================================
# 主入口
# ============================================================
def cmd_check(args):
    today = today_ymd()
    print(f'================ 检查同步状态 {today} ================')
    print()

    local_sec = get_local_section_date()
    remote_sec = get_remote_section_date(args.repo)
    status_sec = '✅ 已同步' if (local_sec and local_sec >= today) else '⚠️ 需同步'
    print(f'[截面] 本地最新: {local_sec or "❌ 无"} · 远端最新: {remote_sec or "❌ 无"} · {status_sec}')

    local_mcr = get_local_macro_date()
    remote_mcr = get_remote_macro_date(args.repo)
    status_mcr = '✅ 已同步' if (local_mcr and local_mcr >= today) else '⚠️ 需同步'
    print(f'[宏观] 本地最新: {local_mcr or "❌ 无"} · 远端最新: {remote_mcr or "❌ 无"} · {status_mcr}')

    print()
    need_sec = not local_sec or local_sec < today
    need_mcr = not local_mcr or local_mcr < today

    if need_sec or need_mcr:
        print('⚠️ 需要执行: python daily_sync.py sync-and-push')
        return 1
    print('✅ 无需同步')
    return 0


def cmd_sync(args):
    today = today_ymd()
    print(f'================ 同步 {today} ================')

    local_sec = get_local_section_date()
    if local_sec and local_sec >= today and not args.force:
        print(f'[section] 今天 {today} 已同步（本地: {local_sec}），跳过（用 --force 强制重跑）')
    else:
        try:
            sync_section(today)
        except Exception as e:
            print(f'[section] ❌ {e}')
            return 2

    if args.macro_file or not sys.stdin.isatty():
        macro_data = read_macro_from_stdin_or_file(args.macro_file)
        if macro_data:
            sync_macro(today, macro_data)
        else:
            print('[macro] 未提供宏观数据（--macro-file 或 stdin），跳过宏观同步')
            print('  提示：先 agent 调 mcp__westock-mcp.data_macro()，再把结果传入此命令')
    else:
        print('[macro] 未传入宏观数据（没 --macro-file 且 stdin 为 TTY），跳过宏观同步')

    print()
    print('✅ 同步完成（截面必有，宏观可选）')
    return 0


def cmd_push(args):
    today = today_ymd()
    print(f'================ 推送 {today} ================')
    ok = push_to_github(args.repo, today)
    return 0 if ok else 3


def cmd_sync_and_push(args):
    s = cmd_sync(args)
    if s != 0:
        return s
    print()
    p = cmd_push(args)
    return p


# ============================================================
# argparse
# ============================================================
def build_parser():
    p = argparse.ArgumentParser(description='每日截面+宏观同步 + GitHub 上传')
    sub = p.add_subparsers(dest='cmd', required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument('--repo', default=DEFAULT_GH_REPO,
                      help=f'GitHub 本地仓库路径（默认 {DEFAULT_GH_REPO}）')

    p_check = sub.add_parser('check', help='仅检查状态', parents=[base])
    p_check.set_defaults(func=cmd_check)

    p_sync = sub.add_parser('sync', help='同步（截面+宏观），不推送', parents=[base])
    p_sync.add_argument('--macro-file', default=None,
                        help='宏观数据 JSON 文件路径（也支持 stdin 输入）')
    p_sync.add_argument('--force', action='store_true', help='强制重跑今天')
    p_sync.set_defaults(func=cmd_sync)

    p_push = sub.add_parser('push', help='仅推送今日到 GitHub', parents=[base])
    p_push.set_defaults(func=cmd_push)

    p_sp = sub.add_parser('sync-and-push', help='同步 + 推送（默认行为）',
                          parents=[base])
    p_sp.add_argument('--macro-file', default=None,
                      help='宏观数据 JSON 文件路径（也支持 stdin 输入）')
    p_sp.add_argument('--force', action='store_true', help='强制重跑今天')
    p_sp.set_defaults(func=cmd_sync_and_push)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
