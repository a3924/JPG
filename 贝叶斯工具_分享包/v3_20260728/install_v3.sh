#!/usr/bin/env bash
# install_v3.sh — 一键安装贝叶斯分享包 V3
# 适用于 Git Bash / Linux / Mac

set -e

# 自动定位 WorkBuddy skills 目录
SKILL_DIR="$HOME/.workbuddy/skills"
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "  贝叶斯分享包 V3 一键安装"
echo "=========================================="
echo "WorkBuddy skills 目录: $SKILL_DIR"
echo "分享包目录:           $PACKAGE_DIR"
echo ""

# 检查 skills 目录
if [ ! -d "$SKILL_DIR" ]; then
    echo "❌ WorkBuddy skills 目录不存在: $SKILL_DIR"
    echo "   请先安装 WorkBuddy Desktop"
    exit 1
fi

# 已存在的 skill 列出来供对照
echo "[1/4] 检查现有 skills..."
EXISTING_SKILLS=$(ls "$SKILL_DIR" 2>/dev/null | grep -E "(bayesian|buffett|zhinanzhen)" || echo "  (无)")
echo "现有相关 skills:"
echo "$EXISTING_SKILLS" | sed 's/^/  - /'
echo ""

# 4 个 skill 列表
declare -A SKILLS
SKILLS[1]="01_贝叶斯量化/bayesian-quant-decision"
SKILLS[2]="01_贝叶斯量化/run-stock-bayesian-report"
SKILLS[3]="02_巴菲特研究/a-share-buffett-deep-research"
SKILLS[4]="03_0AMV获取/zhinanzhen-0amv-daily-db"

i=1
for path in "${SKILLS[@]}"; do
    full_path="$PACKAGE_DIR/$path"
    skill_name=$(basename "$path")
    target="$SKILL_DIR/$skill_name"

    echo "[2/4] [$i/4] 复制: $skill_name"

    if [ ! -d "$full_path" ]; then
        echo "  ❌ 源不存在: $full_path"
        exit 1
    fi

    # 如果目标已存在，先备份
    if [ -d "$target" ]; then
        backup="${target}.bak.$(date +%Y%m%d_%H%M%S)"
        echo "  ⚠️ 已存在，备份到: $backup"
        mv "$target" "$backup"
    fi

    cp -r "$full_path" "$target"
    echo "  ✅ 已安装: $target"
    i=$((i+1))
done

echo ""
echo "[3/4] 检查 Python 依赖..."
PYTHON_CMD=""
for cmd in python python3; do
    if command -v $cmd &> /dev/null; then
        PYTHON_CMD=$cmd
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "  ⚠️ 没找到 python，请先安装 Python 3.10+"
else
    echo "  使用 Python: $PYTHON_CMD"
    $PYTHON_CMD -m pip list 2>/dev/null | grep -iE "(numpy|pandas|scipy|pyarrow|pytdx|pillow)" > /tmp/pycheck.txt
    MISSING=""
    for pkg in numpy pandas scipy pyarrow pytdx pillow; do
        if ! grep -i "^$pkg" /tmp/pycheck.txt &>/dev/null; then
            MISSING="$MISSING $pkg"
        fi
    done
    if [ -z "$MISSING" ]; then
        echo "  ✅ 所有依赖已安装"
    else
        echo "  ⚠️ 缺失依赖:$MISSING"
        echo "  安装命令: $PYTHON_CMD -m pip install$MISSING"
    fi
fi

echo ""
echo "[4/4] GitHub 凭证（可选）"
if [ -f "$PACKAGE_DIR/00_使用说明/.env.example" ] && [ ! -f "$PACKAGE_DIR/00_使用说明/.env" ]; then
    echo "  想上传报告？生成 GitHub PAT（https://github.com/settings/tokens）"
    echo "  然后: cp $PACKAGE_DIR/00_使用说明/.env.example $PACKAGE_DIR/00_使用说明/.env"
    echo "  填入 PAT 即可"
fi

echo ""
echo "=========================================="
echo "  ✅ 安装完成"
echo "=========================================="
echo ""
echo "下一步："
echo "  - 跑一个报告试试:"
echo "    $PYTHON_CMD \"$SKILL_DIR/bayesian-quant-decision/scripts/run_report.py\" 600552 凯盛科技"
echo "  - 每日同步:"
echo "    $PYTHON_CMD \"$SKILL_DIR/bayesian-quant-decision/scripts/daily_sync.py\" sync-and-push"
echo "  - 0AMV 数据（需要指南针软件或 GitHub 公开仓库下载）:"
echo "    curl -L -o D:/AIlianghua/OAMV/0AMV日线数据库_2015至今.csv \\"
echo "      'https://raw.githubusercontent.com/a3924/JPG/main/0AMV日线数据库_2015至今.csv'"
