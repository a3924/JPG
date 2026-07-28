---
name: zhinanzhen-0amv-daily-db
agent_created: true
description: 从本地安装的指南针（Zhinanzhen/Compass）股票软件私有二进制数据文件中提取 0AMV（活跃市值）日线数据库，并生成 CSV、PNG 蜡烛图和交互式 HTML。当用户需要把指南针里的 OAMV/0AMV/活跃市值/活筹指数日线数据导出成可分析文件时使用本技能。
---

# 指南针 0AMV 日线数据库提取

## 使用场景

当用户有下列需求时，加载并使用本技能：

- 从指南针（Zhinanzhen）软件里把 **0AMV（活跃市值/活筹指数）** 日线数据导出成数据库文件（CSV）。
- 生成 **2015-01-01 至今** 的 0AMV 日K线图（PNG 或交互 HTML）。
- 对指南针本地私有二进制数据 `.vdat` 进行结构化解析，获得 OHLC、成交量、成交额等真实数值。
- 目标目录通常类似于 `D:\AIlianghua\OAMV`（用户自行安装指南针的目录）。

## 前置条件

1. 指南针已经安装在用户指定的目录（如 `D:\AIlianghua\OAMV`）。
2. 用户已在指南针里登录并下载/补全了 **0AMV** 数据（进入 0AMV 日K界面，等待本地数据下载完成）。
3. 提取前必须 **完全关闭指南针软件**，否则 `ANALYSE\Data\ChinaStk\Z_SK\day.vdat` 会被 Windows 独占锁锁住，Python 无法读取。
4. 需要 Pillow 来生成 PNG；如无可用环境，使用本技能管理的 Python venv 安装 Pillow。

## 提取流程

1. **确认目标目录**：通常是用户提到的指南针安装根目录，例如 `D:\AIlianghua\OAMV`。
2. **确认 0AMV 数据文件存在**：`{BASE}\ANALYSE\Data\ChinaStk\Z_SK\day.vdat`。
3. **读取二进制文件**：以 `rb` 方式打开 `day.vdat`（关闭指南针后无锁）。
4. **定位 0AMV 数据块**：在文件中搜索字节串 `Z_SK0AMV`。每个块前 8 字节为代码标识，其后就是 28 字节/条的日线记录。
5. **解析单条记录**（28 字节 = 7 × 4 字节）：
   - `int32` 日期（YYYYMMDD）
   - `float32` 开（亿元）
   - `float32` 高（亿元）
   - `float32` 低（亿元）
   - `float32` 收（亿元）
   - `float32` 量（原始整数，除以 1e8 后单位为“亿”）
   - `float32` 额（原始整数，除以 1e8 后单位为“亿元”）
6. **合并所有块**：按日期排序、去重。0AMV 日线在 `.vdat` 中被切成多个约 250 交易日的块，必须合并。
7. **过滤起始日期**：默认保留 `>= 20150101` 的数据。
8. **计算衍生列**：`涨幅% = (收-昨收)/昨收*100`，`振幅% = (高-低)/昨收*100`。
9. **输出**：
   - CSV：`0AMV日线数据库_2015至今.csv`
   - PNG：`0AMV日K图_2015至今.png`（红涨绿跌，MA5/10/30，成交量）
   - HTML：`0AMV日K图_2015至今.html`（ECharts 交互图）

## 可复用脚本

本技能在 `scripts/build_oamv_db.py` 中提供了完整实现。执行方法：

```bash
cd {BASE}
{managed_python}\python.exe build_oamv_db.py
```

若 PNG 渲染需要 Pillow，使用隔离 venv：

```bash
{managed_python}\python.exe -m venv {managed_python}\envs\default
{managed_python}\envs\default\Scripts\pip.exe install Pillow
{managed_python}\envs\default\Scripts\python.exe build_oamv_db.py
```

脚本中的常量可调整：

- `BASE`：指南针安装目录
- `START_DATE`：数据库起始日期，默认 `20150101`
- `CODE`：指标代码，默认 `b"Z_SK0AMV"`（0AMV/活跃市值）

## 数据验证

提取完成后，核对以下典型锚点日（来自指南针界面），确保数值精确：

- 2025-08-18：开 165633.6，高 173341.2，低 165633.6，收 172964.0，量 1776.87 亿，额 27636.66 亿。
- 2026-05-25：开 247496.2，高 251840.8，低 247147.3，收 250198.9。
- 2024-11-15：开 168270.3，高 170430.7，低 162011.9，收 162051.6。
- 2023-12-11：开 76330.9，高 78439.8，低 76260.2，收 78439.8。

如果提取结果与锚点不一致，检查：

- 指南针是否完全关闭（文件锁）。
- `day.vdat` 是否为最新（用户是否已下载 0AMV 数据）。
- `BASE` 路径是否正确。

## 已知限制

- 本技能只验证过 **0AMV（活跃市值/活筹指数）** 的 `Z_SK0AMV` 代码。其他指南针指标（如 `0DMV`）可能使用相同容器格式，但量级过滤和代码字符串需要相应调整。
- `.vdat` 的 0AMV 记录中只包含 OHLC、量、额。指南针界面里看到的 `盘/率/幅/振` 等列中，`盘` 和 `率` 不在 `.vdat` 的 0AMV 记录里；`幅` 和 `振` 由本脚本从昨收推导得出。
- 数据从指南针本地文件读取，因此只能拿到用户已下载到本地的历史区间；若未下载，需先登录指南针并打开 0AMV 界面等待数据同步。

## 参考

- 更详细的二进制格式说明见 `references/binary_format.md`。
