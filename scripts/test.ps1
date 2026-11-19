[CmdletBinding()]
param([string]$OutputRoot)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$previousTestRoot = $env:VOICE_STUDIO_TEST_ROOT
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'apps\api'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    if ($OutputRoot) {
        $candidate = Join-Path $OutputRoot 'unit'
    } elseif ($previousTestRoot) {
        $candidate = $previousTestRoot
    } else {
        $candidate = & python -B -X utf8 -c "from voice_studio.config import default_workspace; print(default_workspace() / 'validation' / 'unit')"
        if ($LASTEXITCODE -ne 0) { throw '无法确定默认测试目录。' }
    }
    $env:VOICE_STUDIO_TEST_ROOT = & python -B -X utf8 -c "import sys; from pathlib import Path; from voice_studio.config import validate_data_path; print(validate_data_path(Path(sys.argv[1])))" $candidate
    if ($LASTEXITCODE -ne 0) { throw '测试输出路径检查失败。' }
    New-Item -ItemType Directory -Path $env:VOICE_STUDIO_TEST_ROOT -Force | Out-Null
    python -B -X utf8 -m unittest discover -s (Join-Path $projectRoot 'tests') -v
    if ($LASTEXITCODE -ne 0) { throw '测试失败。' }
} finally {
    $env:VOICE_STUDIO_TEST_ROOT = $previousTestRoot
    $env:PYTHONPATH = $previousPythonPath
}
