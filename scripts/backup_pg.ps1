<#
.SYNOPSIS
    PostgreSQL 备份模板（vector_kb）。pg_dump custom 格式（-Fc），时间戳命名，可选滚动清理。

.DESCRIPTION
    模板脚本——不写死真实密码 / IP / 路径，不读取项目 .env，不输出密钥。
    参数走命令行或环境变量；密码用 PostgreSQL 原生 PGPASSWORD 传入，避免出现在命令行/进程列表。
    详见 docs/27-backup-restore-runbook.md。

.PARAMETER DbHost      PG 主机（默认环境变量 DB_HOST，否则 localhost）
.PARAMETER DbPort      PG 端口（默认 DB_PORT 或 5432）
.PARAMETER DbUser      PG 用户（默认 DB_USER 或 postgres）
.PARAMETER DbName      库名（默认 DB_NAME 或 vector_kb）
.PARAMETER OutDir      备份输出目录（默认 BACKUP_DIR，否则 .\backups）。
                       注意：默认 .\backups 仅用于本地演练（已被 .gitignore 忽略）；
                       生产请显式传 -OutDir 到项目目录之外，例如 C:\backup\vector-kb。
.PARAMETER KeepLast    仅保留最近 N 份 .dump（0 = 不清理，默认 0）

.EXAMPLE
    $env:PGPASSWORD = '<DB_PASSWORD>'
    .\scripts\backup_pg.ps1 -DbHost db.internal -DbUser vkb -DbName vector_kb -OutDir C:\backup\vector-kb -KeepLast 7
    Remove-Item Env:\PGPASSWORD

.NOTES
    密码来源（任选其一，脚本不接收明文密码参数）：
      1) 预先设置 $env:PGPASSWORD（推荐，用后 Remove-Item Env:\PGPASSWORD）
      2) ~/.pgpass 文件（PostgreSQL 标准）
#>
[CmdletBinding()]
param(
    [string]$DbHost   = $(if ($env:DB_HOST) { $env:DB_HOST } else { 'localhost' }),
    [int]   $DbPort   = $(if ($env:DB_PORT) { [int]$env:DB_PORT } else { 5432 }),
    [string]$DbUser   = $(if ($env:DB_USER) { $env:DB_USER } else { 'postgres' }),
    [string]$DbName   = $(if ($env:DB_NAME) { $env:DB_NAME } else { 'vector_kb' }),
    [string]$OutDir   = $(if ($env:BACKUP_DIR) { $env:BACKUP_DIR } else { '.\backups' }),
    [int]   $KeepLast = 0
)

$ErrorActionPreference = 'Stop'

# pg_dump 必须在 PATH 上
if (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
    throw "找不到 pg_dump，请确认 PostgreSQL 客户端工具已安装并在 PATH 中。"
}

# 密码必须由调用方经 PGPASSWORD / .pgpass 提供——脚本绝不接收/打印明文密码
if (-not $env:PGPASSWORD) {
    Write-Warning "未检测到 PGPASSWORD 环境变量；若未配置 ~/.pgpass，pg_dump 可能会交互式索要密码。"
}

# 默认 .\backups 仅供本地演练（已被 .gitignore 忽略）；生产应传 -OutDir 到项目外
if ($OutDir -eq '.\backups') {
    Write-Warning "正在使用默认目录 .\backups（仅本地演练）。生产请用 -OutDir 指向项目目录之外，例如 C:\backup\vector-kb。"
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$stamp   = Get-Date -Format 'yyyyMMdd_HHmmss'
$outFile = Join-Path $OutDir ("{0}_{1}.dump" -f $DbName, $stamp)

Write-Host "[backup_pg] 备份 $DbName @ ${DbHost}:$DbPort -> $outFile"

# -Fc custom 格式：压缩 + 支持 pg_restore 选表/并行恢复
& pg_dump -h $DbHost -p $DbPort -U $DbUser -F c -d $DbName -f $outFile
if ($LASTEXITCODE -ne 0) { throw "pg_dump 失败，退出码 $LASTEXITCODE" }

$sizeMB = [math]::Round((Get-Item $outFile).Length / 1MB, 2)
Write-Host "[backup_pg] 完成：$outFile（$sizeMB MB）"

# 可选：滚动保留最近 N 份
if ($KeepLast -gt 0) {
    $pattern = "{0}_*.dump" -f $DbName
    $old = Get-ChildItem -Path $OutDir -Filter $pattern |
           Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepLast
    foreach ($f in $old) {
        Write-Host "[backup_pg] 清理旧备份：$($f.Name)"
        Remove-Item $f.FullName -Force
    }
}

Write-Host "[backup_pg] OK"
