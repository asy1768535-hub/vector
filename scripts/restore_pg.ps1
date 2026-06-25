<#
.SYNOPSIS
    PostgreSQL 恢复模板（vector_kb）。pg_restore 从 custom 格式 .dump 恢复。

.DESCRIPTION
    模板脚本——不写死真实密码 / IP / 路径，不读取项目 .env，不输出密钥。
    **破坏性操作**：会写入/覆盖目标库。默认需 -Confirm 显式确认才执行。
    详见 docs/27-backup-restore-runbook.md。

.PARAMETER DumpFile    要恢复的 .dump 文件路径（必填）
.PARAMETER DbHost      PG 主机（默认 DB_HOST 或 localhost）
.PARAMETER DbPort      PG 端口（默认 DB_PORT 或 5432）
.PARAMETER DbUser      PG 用户（默认 DB_USER 或 postgres）
.PARAMETER DbName      目标库名（默认 DB_NAME 或 vector_kb）
.PARAMETER Clean       覆盖恢复：--clean --if-exists（先删同名对象再建）。默认 false = 恢复到空库。
.PARAMETER Confirm     显式确认执行破坏性恢复（缺省则只做演练打印，不实际写库）

.EXAMPLE
    # 恢复到全新空库（先手动 createdb vector_kb）
    $env:PGPASSWORD = '<DB_PASSWORD>'
    .\scripts\restore_pg.ps1 -DumpFile C:\backup\vector-kb\vector_kb_20260625_010000.dump -DbName vector_kb -Confirm
    Remove-Item Env:\PGPASSWORD

.EXAMPLE
    # 覆盖恢复到已有库
    .\scripts\restore_pg.ps1 -DumpFile <DUMP_FILE> -Clean -Confirm
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DumpFile,
    [string]$DbHost = $(if ($env:DB_HOST) { $env:DB_HOST } else { 'localhost' }),
    [int]   $DbPort = $(if ($env:DB_PORT) { [int]$env:DB_PORT } else { 5432 }),
    [string]$DbUser = $(if ($env:DB_USER) { $env:DB_USER } else { 'postgres' }),
    [string]$DbName = $(if ($env:DB_NAME) { $env:DB_NAME } else { 'vector_kb' }),
    [switch]$Clean,
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command pg_restore -ErrorAction SilentlyContinue)) {
    throw "找不到 pg_restore，请确认 PostgreSQL 客户端工具已安装并在 PATH 中。"
}
if (-not (Test-Path $DumpFile)) {
    throw "找不到备份文件：$DumpFile"
}
if (-not $env:PGPASSWORD) {
    Write-Warning "未检测到 PGPASSWORD 环境变量；若未配置 ~/.pgpass，pg_restore 可能会交互式索要密码。"
}

# 组装参数：--no-owner 避免恢复到不同属主时报错；可选 --clean --if-exists 覆盖
# （不用 $args——那是 PowerShell 自动变量）
$restoreArgs = @('-h', $DbHost, '-p', $DbPort, '-U', $DbUser, '-d', $DbName, '--no-owner')
if ($Clean) { $restoreArgs += @('--clean', '--if-exists') }
$restoreArgs += $DumpFile

Write-Host "[restore_pg] 目标库：$DbName @ ${DbHost}:$DbPort"
Write-Host "[restore_pg] 备份文件：$DumpFile"
Write-Host "[restore_pg] 模式：$(if ($Clean) { '覆盖恢复 (--clean --if-exists)' } else { '恢复到空库' })"

if (-not $Confirm) {
    Write-Warning "演练模式：未加 -Confirm，未实际写库。确认无误后追加 -Confirm 执行。"
    Write-Host   "[restore_pg] 将执行： pg_restore $($restoreArgs -join ' ')"
    return
}

Write-Host "[restore_pg] 开始恢复（破坏性，写入目标库）..."
& pg_restore @restoreArgs
# pg_restore 对个别非致命问题会返回非零；提示人工核对而非直接当成功
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pg_restore 退出码 $LASTEXITCODE（可能含非致命警告）。请核对输出与目标库状态。"
} else {
    Write-Host "[restore_pg] 恢复完成。"
}

Write-Host "[restore_pg] 后续：执行 'alembic upgrade head' 对齐迁移版本，再按 docs/27 §九 验收。"
