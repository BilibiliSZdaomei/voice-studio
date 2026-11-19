[CmdletBinding()]
param([int]$Port = 8787, [string]$Config)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Config -and $env:VOICE_STUDIO_CONFIG) { $Config = $env:VOICE_STUDIO_CONFIG }
$configArgs = @()
if ($Config) { $configArgs = @('--config', [IO.Path]::GetFullPath($Config)) }
$rawConfig = & python -B -X utf8 (Join-Path $PSScriptRoot 'run_server.py') --print-config @configArgs
if ($LASTEXITCODE -ne 0) { throw '本地配置检查失败。' }
$settings = $rawConfig | ConvertFrom-Json
$state = $null
try { $state = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 3 } catch { Write-Host '声稿未在该端口运行。' }
if ($state) {
    if ($state.paths.project -ne $projectRoot -or $state.paths.data -ne $settings.data_root) {
        throw '该端口服务与所选项目或数据目录不符，请核对端口和配置。'
    }
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/shutdown" -Headers @{ 'X-Studio-Token' = $state.csrf_token } -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
    Write-Host '已请求声稿安全退出；生成中的任务会取消，已完成配音保留。'
}
python -B -X utf8 (Join-Path $PSScriptRoot 'local_model.py') stop @configArgs | Out-Null
