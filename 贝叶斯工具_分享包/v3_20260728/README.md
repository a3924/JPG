# 贝叶斯分享包 V3（2026-07-28）

> 给同事的另一台机器 / 卸载全部 skill 后的全新安装场景使用。
> V3 相比 V2 新增 **`daily_sync.py` 每日自动同步** + **截面/宏观数据带日期文件名** + **GitHub 自动推送**。

---

## 📦 包里有什么

```
贝叶斯分享包_V3/
├── 00_使用说明/
│   ├── AI协同操作手册.md            # 详细使用流程（含 daily_sync、daily GitHub 推送）
│   └── .env.example                 # 占位符模板（请复制为 .env 后填你的 PAT）
├── 01_贝叶斯量化/
│   ├── bayesian-quant-decision/    # 主引擎 v1.12 + daily_sync
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   │   ├── bayesian_engine.py
│   │   │   ├── build_caches.py
│   │   │   ├── bulk_sync_zz500.py  ← 输出 zz500_60d_YYYYMMDD.parquet
│   │   │   ├── db_sync.py
│   │   │   ├── factor_engine.py
│   │   │   ├── oamv_analyzer.py
│   │   │   ├── report.py
│   │   │   ├── run_report.py        ← 自动找最新 macro/section
│   │   │   ├── sync_section.py      ← 新增 find_latest_panel() 工具
│   │   │   └── daily_sync.py        ⭐ V3 新增：每日自动同步
│   │   └── references/
│   └── run-stock-bayesian-report/   # 单只个股报告触发入口
├── 02_巴菲特研究/
│   └── a-share-buffett-deep-research/   # A 股专用深研（DCF/估值/审计/报告）
├── 03_0AMV获取/
│   ├── zhinanzhen-0amv-daily-db/    # skill 形态的日线提取工具
│   └── 独立脚本/build_oamv_db.py    # 纯脚本形态
├── V3_CHANGELOG.md                  # 版本升级日志
├── install_v3.sh                    # ⭐ 一键安装脚本（Linux/Mac/Git Bash）
└── install_v3.ps1                   # ⭐ 一键安装脚本（PowerShell）
```

---

## 🚀 快速安装（3 步）

### 第 1 步：复制 4 个 Skill 到 WorkBuddy

任意选一种：

#### 方式 A：用安装脚本（推荐）

```bash
# Git Bash / Linux / Mac
bash install_v3.sh

# PowerShell
./install_v3.ps1
```

#### 方式 B：手动复制

```bash
SKILL_DIR="$HOME/.workbuddy/skills"
PACKAGE_DIR="$(pwd)"

cp -r "$PACKAGE_DIR/01_贝叶斯量化/bayesian-quant-decision" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/01_贝叶斯量化/run-stock-bayesian-report" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/02_巴菲特研究/a-share-buffett-deep-research" "$SKILL_DIR/"
cp -r "$PACKAGE_DIR/03_0AMV获取/zhinanzhen-0amv-daily-db" "$SKILL_DIR/"
```

### 第 2 步：安装 Python 依赖

```bash
python -m pip install numpy pandas scipy pyarrow pytdx pillow
```

### 第 3 步：配置 WorkBuddy

确认以下 MCP 已连接（在 WorkBuddy 连接器管理）：
- **`tdx-connector`**（通达信行情）
- **`westock-mcp`**（腾讯自选股）

---

## ⭐ 每日同步（V3 新增）

```bash
python C:\Users\<你的名字>\.workbuddy\skills\bayesian-quant-decision\scripts\daily_sync.py check
# 输出示例：
# [截面] 本地最新: 20260728 · 远端最新: 20260728 · ✅ 已同步
# [宏观] 本地最新: 20260728 · 远端最新: 20260728 · ✅ 已同步

python ...\daily_sync.py sync-and-push
# 自动拉取今天的 zz500 截面（pytdx 直连，~60 秒）
# 自动拉取今天宏观（agent 调 westock data_macro 后传入）
# 自动 push 到 a3924/JPG 仓库的 data/section/ 和 data/macro/
```

详见 `00_使用说明/AI协同操作手册.md` 第 5 章。

---

## 🎯 跑报告（一行命令）

```bash
python C:\Users\<你的名字>\.workbuddy\skills\bayesian-quant-decision\scripts\run_report.py 600552 凯盛科技
```

报告自动输出到 `D:\AILIANGHUA\贝叶斯报告\{名称} {代码} {日期} {评分}.md`。

---

## ⚠️ 凭证处理

V3 包中**没有** `.env`（已剔除发送方私有的 GitHub PAT）。

如需上传报告到 GitHub：
1. GitHub → Settings → Developer settings → Personal access tokens 生成新 token
2. 复制 `00_使用说明/.env.example` 为 `.env`，填入你的 token
3. （可选）也可忽略此步，本地使用本工具完全 OK

---

## 📝 V3 包**没有**包含什么

- ✅ 不含历史报告（39 份在 `a3924/JPG` 仓库的 `贝叶斯报告/` 下，clone 即可）
- ✅ 不含任何数据缓存（V3 是 clean install，每次跑报告时按需从 MCP 拉取）
- ✅ 不含 `.env`（避免夹带发送方的 PAT）
- ✅ 不含指南针软件本体（对方自行安装）
- ✅ 不含 0AMV CSV

---

## 🔗 配套 GitHub 仓库

`https://github.com/a3924/JPG`（公开）

可以借它做两件事：
1. **拉 0AMV 数据**：`git clone https://github.com/a3924/JPG.git D:/ai-oamv`
2. **看历史报告**：仓库的 `贝叶斯报告/` 目录下
3. **共享每日截面 + 宏观**：仓库的 `data/section/` 和 `data/macro/` 目录（V3 新增）

---

## 📋 升级日志

见 `V3_CHANGELOG.md`，重点：
- 🆕 `daily_sync.py` 一键检查+同步+推送
- 🆕 文件名带日期：`zz500_60d_YYYYMMDD.parquet` / `macro_YYYYMMDD.json`
- 🆕 `find_latest_panel()` / `find_latest_macro_json()` 自动找最新
- 🆕 GitHub 仓库新增 `data/section/` `data/macro/`
- 🆕 一键安装脚本 `install_v3.sh` / `install_v3.ps1`
