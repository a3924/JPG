# 贝叶斯分享包 V3 升级日志

**版本日期**：2026-07-28
**相比 V2**：更自动化、更少手工、更便于多机协同

---

## 🆕 新增内容

### 1. `scripts/daily_sync.py` — 每日自动同步 + GitHub 推送

V3 最大亮点。脚本自动完成：
- 拉取中证 500 截面 panel（pytdx 直连通达信）
- 拉取宏观快照（GDP缺口 / M2 / 利差 / PMI / CPI）
- 检查本地 + 远端最新日期 vs 今天的日期
- 一键 push 到 `a3924/JPG` 仓库 `data/` 目录

```
python daily_sync.py check           # 仅检查状态
python daily_sync.py sync-and-push   # 同步 + 推送（一键完成）
```

### 2. 文件名带日期，便于多机协同

| 旧（V2） | 新（V3） |
|----------|----------|
| `data/section/zz500_60d.parquet` | `data/section/zz500_60d_YYYYMMDD.parquet`（带 legacy 兼容）|
| `data/macro/latest.json` | `data/macro/macro_YYYYMMDD.json`（带 legacy 兼容）|

**保留 legacy 兼容**：每次同步都同时复制一份无日期的 `zz500_60d.parquet` 和 `latest.json`，让旧代码不会破。

### 3. `sync_section.py` 新增工具函数

```python
from sync_section import find_latest_panel, panel_path_for_date, list_panel_files
panel_path, panel_ymd = find_latest_panel()
```

### 4. `run_report.py` 自动找最新截面/宏观

`load_section()` 自动调用 `find_latest_panel()`，`load_bayes_caches` macro 自动调用 `find_latest_macro_json()`，不再硬编码文件名。

### 5. GitHub 仓库结构新增 data/ 目录

`a3924/JPG` 仓库原本只有 `0AMV日线数据库_2015至今.csv` 和 `贝叶斯报告/`。
V3 后新增：
```
a3924/JPG/
├── 0AMV日线数据库_2015至今.csv
├── README.md
├── 贝叶斯报告/                       # 36+ 份历史报告
├── 贝叶斯工具_分享包/                 # 历史分享包快照
└── data/                             ⭐ V3 新增
    ├── macro/
    │   ├── macro_20260728.json
    │   └── latest.json
    └── section/
        ├── zz500_60d_20260728.parquet (908 KB)
        └── zz500_60d.parquet
```

### 6. 一键安装脚本

| 文件 | 用途 |
|------|------|
| `install_v3.sh` | Git Bash / Linux / Mac 一键复制 4 个 skill |
| `install_v3.ps1` | PowerShell 一键复制 4 个 skill |

### 7. `.env.example` 占位模板

V2 包里有 `.env`（发送方实际 PAT）。V3 **删除 `.env`**，只保留 `.env.example`，避免夹带凭证。

---

## 🔧 改造的脚本

| 文件 | 改动 |
|------|------|
| `scripts/daily_sync.py` | **新增** |
| `scripts/bulk_sync_zz500.py` | 加 `bulk_sync_to()` 函数；DEFAULT_CSV 加 fallback；输出支持带日期文件名 |
| `scripts/sync_section.py` | 加 `find_latest_panel()` / `panel_path_for_date()` / `list_panel_files()` |
| `scripts/run_report.py` | `load_section()` 用 finder；新增 `find_latest_macro_json()`；移除全局 `SECTION_PARQUET` 常量 |
| `SKILL.md` | 加「每日数据同步」段落，更新数据来源文档 |
| `references/raw_input_template.json` | 不变 |
| `references/原始代码_量化计算工具.txt` | 不变 |
| `references/提示词_贝叶斯多因子模型.md` | 不变 |

---

## ✂️ 剔除的内容

| 项目 | 说明 |
|------|------|
| `00_使用说明/.env` | 避免夹带发送方 GitHub PAT |
| 任何 `raw_*.json` | 用户私有临时数据 |
| 任何 `data/*.json` / `data/*.parquet` | 用户私有缓存（clean install 后重跑按需生成）|
| `.workbuddy/` 工作区 | 临时管理用，分享包无需 |

---

## 🆚 与 V2 对比

| 项 | V2 | V3 |
|------|------|------|
| Skill 数 | 4 | 4 |
| 每日截面/宏观同步 | ❌ 手工跑 `bulk_sync_zz500.py` 和调 MCP | ✅ `daily_sync.py sync-and-push` |
| 文件名带日期 | ❌ 固定名，易冲突 | ✅ YYYYMMDD 多版本并存 |
| GitHub 推送 | ❌ 全手工 git 命令 | ✅ daily_sync 自动 push |
| 多机协同 | ⚠️ 数据散在本地 | ✅ 从 GitHub 共享截面/宏观 |
| 一键安装 | ❌ 手工 cp | ✅ `install_v3.sh` / `install_v3.ps1` |
| `.env` | ✅ 夹带发送方 PAT | ❌ 删除，只保留 `.env.example` |

---

## 📋 验证清单

新机器安装 V3 后：

```bash
# 1. 检查 skill 安装
ls ~/.workbuddy/skills/
# 应有 4 个：bayesian-quant-decision / run-stock-bayesian-report /
#           a-share-buffett-deep-research / zhinanzhen-0amv-daily-db

# 2. 检查 Python 依赖
python -c "import numpy, pandas, scipy, pyarrow, pytdx, PIL; print('OK')"

# 3. 跑一个无数据的报告（首次会自动拉）
python ~/.workbuddy/skills/bayesian-quant-decision/scripts/run_report.py 600552 凯盛科技

# 4. 配 daily_sync（首次）
mkdir -p D:/AILIANGHUA/OAMV
# 把 0AMV CSV 放到该路径（自己安装指南针生成 + 把 GitHub 公开仓库的 0AMV 文件拉过去）

# 5. 后续每日自动同步
python ~/.workbuddy/skills/bayesian-quant-decision/scripts/daily_sync.py sync-and-push
```

---

## 🔗 仓库对应关系

| 包内容 | 来源 |
|--------|------|
| Skill 源码 | 本包 `01_贝叶斯量化/` 等 |
| 历史报告 | https://github.com/a3924/JPG/ 仓库 `贝叶斯报告/` 目录 |
| 0AMV 数据 | https://github.com/a3924/JPG/blob/main/0AMV日线数据库_2015至今.csv |
| 每日截面数据 | https://github.com/a3924/JPG/tree/main/data/section/ |
| 每日宏观数据 | https://github.com/a3924/JPG/tree/main/data/macro/ |
