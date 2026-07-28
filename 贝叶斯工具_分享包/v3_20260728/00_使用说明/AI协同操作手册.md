# AI 协同操作手册（v3 · 2026-07-28）

> 给**收到本包**的 AI 看，覆盖 6 章：安装 → 0AMV → 日常报告 → daily_sync → 上传 → 安全约定。
> 配套总览见 `README.md`，升级差异见 `V3_CHANGELOG.md`。

---

## 0. 安装（5 分钟）

### 0.1 复制 skill

```bash
# 方式 A：一键脚本（推荐）
bash <解压路径>/install_v3.sh
# 或在 PowerShell：
.\install_v3.ps1

# 方式 B：手动复制
SKILL_DIR="$HOME/.workbuddy/skills"
PACKAGE_DIR="<解压路径>"
cp -r "$PACKAGE_DIR/01_贝叶斯量化/bayesian-quant-decision" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/01_贝叶斯量化/run-stock-bayesian-report" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/02_巴菲特研究/a-share-buffett-deep-research" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/03_0AMV获取/zhinanzhen-0amv-daily-db" "$SKILL_DIR/"
```

### 0.2 Python 依赖

```bash
python -m pip install numpy pandas scipy pyarrow pytdx pillow
```

### 0.3 WorkBuddy 连接器

确认这两个 MCP 已连接（在 WorkBuddy 连接器管理面板）：
- **`tdx-connector`**（通达信行情，必备）
- **`westock-mcp`**（腾讯自选股，必备）

---

## 1. 获取 0AMV 数据（必做）

```bash
# 首次：克隆 GitHub 公开仓库（无需凭证）
git clone https://github.com/a3924/JPG.git D:/ai-oamv

# 把 CSV 复制到标准路径
mkdir -p D:/AILIANGHUA/OAMV
cp D:/ai-oamv/0AMV日线数据库_2015至今.csv D:/AILIANGHUA/OAMV/

# 每日更新
cd D:/ai-oamv && git pull origin main
cp D:/ai-oamv/0AMV日线数据库_2015至今.csv D:/AILIANGHUA/OAMV/
```

---

## 2. 跑个股报告（最简命令）

```bash
python "C:\Users\<你的名字>\.workbuddy\skills\bayesian-quant-decision\scripts\run_report.py" \
       000977 浪潮信息
```

报告自动输出到 `D:\AILIANGHUA\贝叶斯报告\{名称} {代码} {日期} {评分}.md`。

---

## 3. 每日截面 + 宏观同步 ⭐ V3 新增

V3 之前：手工跑 `bulk_sync_zz500.py` 拉截面 + agent 调 MCP 拉宏观 + 手工 git push。**全部由 `daily_sync.py` 一键接管**。

### 3.1 检查状态

```bash
python <skill>/scripts/daily_sync.py check
```

输出：
```
================ 检查同步状态 20260728 ================
[截面] 本地最新: 20260728 · 远端最新: 20260728 · ✅ 已同步
[宏观] 本地最新: 20260728 · 远端最新: 20260728 · ✅ 已同步
✅ 无需同步
```

### 3.2 同步 + 推送（每天跑一次）

```bash
python <skill>/scripts/daily_sync.py sync-and-push
```

会做：
1. 检查今天的截面/宏观是否已同步
2. 没就：`bulk_sync_zz500.py` 拉截面（pytdx 直连，~60s）+ agent 调 `mcp__westock-mcp.data_macro` 拉宏观（如果 macro 参数传入的话）
3. 把 `data/section/zz500_60d_YYYYMMDD.parquet` 和 `data/macro/macro_YYYYMMDD.json` 复制到 Git 仓库（`D:/ai-oamv`）
4. `git add + commit + push`

### 3.3 单独同步宏观（需要先调 MCP）

```bash
# Step 1: agent 调 MCP 拉数据
westock data_macro (mode: indicator, names: macro_cpi_ppi+macro_yield_curve+macro_pmi)

# Step 2: 把 JSON 写入 daily_sync（通过 stdin 或 --macro-file）
echo '{"gdp_gap":0.8,"m2_yoy":8.0,"yield_curve_spread_bp":47,"pmi":50.3,"cpi_yoy":1.0}' | \
    python <skill>/scripts/daily_sync.py sync-and-push
```

---

## 4. 上传贝叶斯报告到 GitHub

V3 的 `daily_sync.py` 自动 push 数据。**报告本身**仍需手工上传（也可以并入 `sync-and-push`）：

```bash
# 单只报告上传（沿用 V2 流程）
GITHUB_PAT=$(grep GITHUB_PAT <PAT 00_使用说明/.env | cut -d= -f2)
cp "D:\AILIANGHUA\贝叶斯报告\浪潮信息 000977 20260728 42.md" \
   "D:\ai-oamv\贝叶斯报告\"

cd D:/ai-oamv
git add "贝叶斯报告/浪潮信息 000977 20260728 42.md"
git commit -m "报告：浪潮信息 000977 2026-07-28"
git remote set-url origin https://${GITHUB_PAT}@github.com/a3924/JPG.git
git push origin main

# 完事清掉 PAT
git remote set-url origin https://github.com/a3924/JPG.git
unset GITHUB_PAT
```

---

## 5. 凭证约定（重要）

V3 包**没有**夹带 `.env`（避免夹带发送方的 PAT）。

如果要用 GitHub 自动推送：
1. GitHub → Settings → Developer settings → Personal access tokens
2. 生成 fine-grained token，仅勾 `public_repo` 的 push
3. 复制 `00_使用说明/.env.example` 为 `.env`，把 token 写进去
4. 之后 `daily_sync.py push` 即可工作

如果不需要上传，删除整份 `.env.example` / `.env` 即可。

---

## 6. 与 V2 的差别

| V2 | V3 |
|------|------|
| `00_使用说明/.env`（含发送方 PAT） | `.env.example`（占位符）+ 你自己 `.env` |
| 每日同步：手工 ~6 步 | 1 行 `daily_sync.py sync-and-push` |
| 截面/宏观文件无日期 | `zz500_60d_YYYYMMDD.parquet` / `macro_YYYYMMDD.json` |
| GitHub 只有 `贝叶斯报告/` + 0AMV | + `data/section/` `data/macro/` |
| 没安装脚本 | `install_v3.sh` / `install_v3.ps1` |

详见 `V3_CHANGELOG.md`。

---

就这么简单。
