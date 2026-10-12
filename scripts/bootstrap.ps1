param([switch]$SkipInstall, [switch]$Offline)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $taskRoot
try {
    $taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        & py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 virtual environment creation failed.' }
    }
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
    }
    $env:PIP_CACHE_DIR = Join-Path $taskRoot '.cache\pip'
    if (-not $SkipInstall) {
        & $taskPython -m pip install -r requirements.lock --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
    & $taskPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency compatibility check failed.' }
    & $taskPython -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Readiness tests failed.' }
    & $taskPython scripts\smoke_backend.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend smoke check failed.' }
    & $taskPython scripts\smoke_ui.py
    if ($LASTEXITCODE -ne 0) { throw 'Python UI smoke check failed.' }
    $probeArgs = @('scripts\verify_batch1.py')
    if ($Offline) { $probeArgs += '--offline' }
    & $taskPython @probeArgs
    $probeExit = $LASTEXITCODE
    if ($probeExit -eq 2) {
        Write-Host 'Local checks passed. Live integration is pending; see docs/verification/batch1.local.json.'
    } elseif ($probeExit -ne 0) {
        throw 'Readiness report generation failed.'
    }
} finally {
    Pop-Location
}
