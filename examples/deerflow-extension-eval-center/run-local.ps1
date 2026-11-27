[CmdletBinding()]
param(
    [ValidateSet('test', 'offline', 'negative', 'lint', 'preview')]
    [string]$Mode = 'test',
    [ValidateSet('auto', 'local', 'docker')]
    [string]$Runtime = 'auto',
    [string[]]$NodeId = @()
)

$ErrorActionPreference = 'Stop'
$evalSource = $PSScriptRoot
$evalReports = Join-Path $evalSource 'reports'
New-Item -ItemType Directory -Force -Path $evalReports | Out-Null
$evalPython = Join-Path $evalSource '.venv/Scripts/python.exe'
if ($Runtime -eq 'local' -or ($Runtime -eq 'auto' -and (Test-Path -LiteralPath $evalPython))) {
    if (-not (Test-Path -LiteralPath $evalPython)) { throw 'The extension local Python environment is not installed.' }
    $evalLocalArgs = @('-X', 'utf8')
    if ($Mode -eq 'preview') {
        $evalLocalArgs += @('-m', 'uvicorn', 'deerflow_extension_eval_center.preview:create_app', '--factory', '--host', '127.0.0.1', '--port', '2027')
    } elseif ($Mode -eq 'test') {
        $evalLocalArgs += @('-m', 'pytest', '-q', '--tb=short', "--junitxml=$evalReports/windows-tests.xml")
        if ($NodeId.Count) { $evalLocalArgs += $NodeId } else { $evalLocalArgs += 'tests' }
    } elseif ($Mode -eq 'lint') {
        $evalLocalArgs += @('-m', 'ruff', 'check', '.')
    } else {
        $evalLocalArgs += @('-m', 'deerflow_extension_eval_center.cli', '--report-dir', $evalReports)
        if ($Mode -eq 'negative') { $evalLocalArgs += '--negative' }
    }
    $evalPreviousEncoding = $env:PYTHONIOENCODING
    Push-Location -LiteralPath $evalSource
    try {
        $env:PYTHONIOENCODING = 'utf-8'
        Write-Host "Evaluation workbench: $Mode (local Python)"
        & $evalPython @evalLocalArgs
        $evalLocalExitCode = $LASTEXITCODE
    } finally {
        $env:PYTHONIOENCODING = $evalPreviousEncoding
        Pop-Location
    }
    Write-Host "Reports: $evalReports"
    exit $evalLocalExitCode
}
if ($Mode -eq 'preview') { throw 'The offline preview requires the prepared local .venv. Use -Runtime local.' }
$evalImage = (& docker inspect deer-flow-gateway --format '{{.Image}}' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $evalImage) {
    throw 'Start Docker Desktop and check the existing deer-flow-gateway image.'
}
$evalArguments = @(
    'run', '--rm', '--network', 'none',
    '--mount', "type=bind,source=$evalSource,target=/extension,readonly",
    '--mount', "type=bind,source=$evalReports,target=/reports",
    '--workdir', '/extension',
    '--env', 'PYTHONPATH=/extension:/app/backend/packages/extension-api',
    '--env', 'PYTHONDONTWRITEBYTECODE=1',
    '--env', 'PYTHONIOENCODING=utf-8',
    '--env', 'HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis',
    '--entrypoint', '/app/backend/.venv/bin/python',
    $evalImage
)
if ($Mode -eq 'test') {
    $evalStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $evalArguments += @('-m', 'pytest', '-vv', '--tb=short', '--color=no', '-o', 'cache_dir=/tmp/pytest-cache', "--junitxml=/reports/tests-$evalStamp.xml")
    if ($NodeId.Count) { $evalArguments += $NodeId } else { $evalArguments += 'tests' }
} elseif ($Mode -eq 'lint') {
    $evalArguments += @('-m', 'ruff', 'check', '--no-cache', '.')
} else {
    $evalArguments += @('-m', 'deerflow_extension_eval_center.cli', '--report-dir', '/reports')
    if ($Mode -eq 'negative') { $evalArguments += '--negative' }
}
Write-Host "Evaluation workbench: $Mode (offline container)"
& docker @evalArguments
$evalExitCode = $LASTEXITCODE
Write-Host "Reports: $evalReports"
exit $evalExitCode
