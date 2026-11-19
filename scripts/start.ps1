[CmdletBinding()]
param([int]$Port = 8787, [switch]$NoBrowser, [string]$Config)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$entry = Join-Path $PSScriptRoot 'run_server.py'
$pythonPath = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$configArgs = @()
if (-not $Config -and $env:VOICE_STUDIO_CONFIG) { $Config = $env:VOICE_STUDIO_CONFIG }
if ($Config) { $Config = [IO.Path]::GetFullPath($Config) }
if ($Config) { $configArgs = @('--config', $Config) }
$rawConfig = & $pythonPath -B -X utf8 $entry --print-config @configArgs
if ($LASTEXITCODE -ne 0) { throw '本地配置检查失败。' }
$settings = $rawConfig | ConvertFrom-Json
$dataRoot = [IO.Path]::GetFullPath($settings.data_root)
$runRoot = Join-Path $dataRoot 'runtime'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
if ($settings.rewrite.enabled -and $settings.rewrite.managed) {
    try {
        $modelOutput = & $pythonPath -B -X utf8 (Join-Path $PSScriptRoot 'local_model.py') start @configArgs 2> (Join-Path $runRoot 'model-start.stderr.log')
        if ($LASTEXITCODE -ne 0) { Write-Warning '本地改写模型未启动，配音与其他功能可以继续使用。请查看外部运行日志。' }
    } catch { Write-Warning '本地改写模型暂未启动，其他工作台功能将继续启动。' }
}

function Get-Studio([int]$TargetPort) {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$TargetPort/api/status" -TimeoutSec 2 } catch { $null }
}

$existing = Get-Studio $Port
if ($existing -and $existing.paths.project -eq $projectRoot -and $existing.paths.data -eq $dataRoot) {
    $url = "http://127.0.0.1:$Port"
    Write-Host "声稿已在运行：$url"
    if (-not $NoBrowser) { Start-Process $url }
    return
}
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
try { $listener.Start() } catch { throw "端口 $Port 已被占用，请使用 -Port 指定其他端口；不会结束现有进程。" } finally { $listener.Stop() }
$stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$stdout = Join-Path $runRoot "server-$stamp.stdout.log"
$stderr = Join-Path $runRoot "server-$stamp.stderr.log"
$arguments = @('-B', '-X', 'utf8', ('"' + $entry + '"'), '--port', $Port)
if ($Config) { $arguments += @('--config', ('"' + [IO.Path]::GetFullPath($Config) + '"')) }
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    Start-Sleep -Milliseconds 200
    $state = Get-Studio $Port
    if ($state -and $state.paths.project -eq $projectRoot -and $state.paths.data -eq $dataRoot) {
        if (-not $process.HasExited) {
            @{ pid = $process.Id; port = $Port; project = $projectRoot; data_root = $dataRoot; stdout = $stdout; stderr = $stderr } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot 'server.json') -Encoding utf8
        }
        $url = "http://127.0.0.1:$Port"
        Write-Host "声稿已启动：$url"
        Write-Host "项目数据：$dataRoot"
        if (-not $NoBrowser) { Start-Process $url }
        return
    }
    if ($process.HasExited) { break }
}
throw "服务启动未完成。日志：$stderr"
