# install_v3.ps1 — 一键安装贝叶斯分享包 V3（PowerShell）
# 适用于 Windows PowerShell / PowerShell Core

$ErrorActionPreference = 'Stop'

Write-Host '==========================================' -ForegroundColor Cyan
Write-Host '  贝叶斯分享包 V3 一键安装' -ForegroundColor Cyan
Write-Host '==========================================' -ForegroundColor Cyan

# 自动定位 WorkBuddy skills 目录
$SkillDir = Join-Path $env:USERPROFILE '.workbuddy\skills'
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "WorkBuddy skills 目录: $SkillDir"
Write-Host "分享包目录:           $PackageDir"
Write-Host ''

# 检查 skills 目录
if (-not (Test-Path $SkillDir)) {
    Write-Host "❌ WorkBuddy skills 目录不存在: $SkillDir" -ForegroundColor Red
    Write-Host '   请先安装 WorkBuddy Desktop' -ForegroundColor Yellow
    exit 1
}

# 4 个 skill 路径
$Skills = @(
    @{ Src = '01_贝叶斯量化\bayesian-quant-decision'; Name = 'bayesian-quant-decision' },
    @{ Src = '01_贝叶斯量化\run-stock-bayesian-report'; Name = 'run-stock-bayesian-report' },
    @{ Src = '02_巴菲特研究\a-share-buffett-deep-research'; Name = 'a-share-buffett-deep-research' },
    @{ Src = '03_0AMV获取\zhinanzhen-0amv-daily-db'; Name = 'zhinanzhen-0amv-daily-db' }
)

Write-Host '[1/4] 检查现有 skills...' -ForegroundColor Yellow
$Existing = Get-ChildItem -Path $SkillDir -Directory | Where-Object { $_.Name -match '(bayesian|buffett|zhinanzhen)' }
if ($Existing) {
    Write-Host '现有相关 skills:'
    $Existing | ForEach-Object { Write-Host "  - $($_.Name)" -ForegroundColor Gray }
} else {
    Write-Host '  (无)' -ForegroundColor Gray
}
Write-Host ''

$i = 1
foreach ($Skill in $Skills) {
    $SrcPath = Join-Path $PackageDir $Skill.Src
    $TargetPath = Join-Path $SkillDir $Skill.Name

    Write-Host "[2/4] [$i/4] 复制: $($Skill.Name)" -ForegroundColor Yellow

    if (-not (Test-Path $SrcPath)) {
        Write-Host "  ❌ 源不存在: $SrcPath" -ForegroundColor Red
        exit 1
    }

    if (Test-Path $TargetPath) {
        $Backup = "${TargetPath}.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
        Write-Host "  ⚠️ 已存在，备份到: $Backup" -ForegroundColor DarkYellow
        Move-Item -Path $TargetPath -Destination $Backup -Force
    }

    Copy-Item -Path $SrcPath -Destination $TargetPath -Recurse -Force
    Write-Host "  ✅ 已安装: $TargetPath" -ForegroundColor Green
    $i++
}

Write-Host ''
Write-Host '[3/4] 检查 Python 依赖...' -ForegroundColor Yellow

$PythonCmd = $null
foreach ($cmd in @('python', 'python3', 'py')) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        $PythonCmd = $cmd
        break
    }
}

if (-not $PythonCmd) {
    Write-Host '  ⚠️ 没找到 python，请先安装 Python 3.10+' -ForegroundColor Yellow
} else {
    Write-Host "  使用 Python: $PythonCmd"
    $Missing = @()
    foreach ($pkg in @('numpy', 'pandas', 'scipy', 'pyarrow', 'pytdx', 'pillow')) {
        $result = & $PythonCmd -m "pip" "show" $pkg 2>&1
        if ($LASTEXITCODE -ne 0) {
            $Missing += $pkg
        }
    }
    if ($Missing.Count -eq 0) {
        Write-Host '  ✅ 所有依赖已安装' -ForegroundColor Green
    } else {
        Write-Host "  ⚠️ 缺失依赖: $($Missing -join ' ')" -ForegroundColor Yellow
        Write-Host "  安装命令: $PythonCmd -m pip install $($Missing -join ' ')" -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '[4/4] GitHub 凭证（可选）' -ForegroundColor Yellow
$EnvExample = Join-Path $PackageDir '00_使用说明\.env.example'
$EnvFile = Join-Path $PackageDir '00_使用说明\.env'
if ((Test-Path $EnvExample) -and (-not (Test-Path $EnvFile))) {
    Write-Host '  想上传报告？生成 GitHub PAT（https://github.com/settings/tokens）'
    Write-Host "  然后: Copy-Item '$EnvExample' '$EnvFile'"
    Write-Host '  填入 PAT 即可'
}

Write-Host ''
Write-Host '==========================================' -ForegroundColor Cyan
Write-Host '  ✅ 安装完成' -ForegroundColor Green
Write-Host '==========================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '下一步：'
Write-Host "  - 跑一个报告试试:"
Write-Host "    $PythonCmd `"$SkillDir\bayesian-quant-decision\scripts\run_report.py`" 600552 凯盛科技"
Write-Host "  - 每日同步:"
Write-Host "    $PythonCmd `"$SkillDir\bayesian-quant-decision\scripts\daily_sync.py`" sync-and-push"
Write-Host '  - 0AMV 数据 (需要指南针软件 或 GitHub 公开仓库下载):'
Write-Host "    mkdir D:\AILIANGHUA\OAMV"
Write-Host "    Invoke-WebRequest 'https://raw.githubusercontent.com/a3924/JPG/main/0AMV%E6%97%A5%E7%BA%BF%E6%95%B0%E6%8D%AE%E5%BA%93_2015%E8%87%B3%E4%BB%8A.csv' -OutFile 'D:\AILIANGHUA\OAMV\0AMV日线数据库_2015至今.csv'"
