param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimeRoot = if ($env:RESEARCH_DATA_DIR) { $env:RESEARCH_DATA_DIR } else { Join-Path $projectRoot 'var' }
$serviceUrl = 'http://127.0.0.1:8920'
$versionMatch = [regex]::Match([System.IO.File]::ReadAllText((Join-Path $projectRoot 'app\config.py')), '(?m)^VERSION\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) { throw 'Cannot read the local application version from app\config.py.' }
$expectedVersion = $versionMatch.Groups[1].Value
$indexPath = Join-Path $projectRoot 'web\dist\index.html'
function Get-ResearchServer {
    try {
        # Windows PowerShell 5 decodes charset-less JSON as ANSI. Decode the raw
        # UTF-8 response so Chinese workspace paths compare correctly on 5 and 7.
        $response = Invoke-WebRequest "$serviceUrl/api/health" -UseBasicParsing -TimeoutSec 2
        $reader = New-Object System.IO.StreamReader($response.RawContentStream, [System.Text.Encoding]::UTF8)
        try { $health = $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
        if ($health.service -eq 'Research Desk') { return $health }
    } catch {}
    return $null
}
function Show-ResearchServer($health) {
    if ($health.version -ne $expectedVersion) {
        throw "Port 8920 is running v$($health.version), but this directory contains v$expectedVersion. Stop the old service with scripts\stop.ps1, then start again."
    }
    $expectedCompetition = $env:RESEARCH_COMPETITION_MODE -eq '1'
    if ([bool]$health.competition -ne $expectedCompetition) { throw 'Port 8920 is running another mode. Stop it before switching workspaces.' }
    $expectedData = [System.IO.Path]::GetFullPath($runtimeRoot)
    if (-not $health.data_directory -or [System.IO.Path]::GetFullPath($health.data_directory) -ne $expectedData) {
        throw 'Port 8920 is running another data workspace. Stop that workspace before switching.'
    }
    $pageId = 'missing-build'
    if (Test-Path -LiteralPath $indexPath) {
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        try { $pageId = [BitConverter]::ToString($hasher.ComputeHash([System.IO.File]::ReadAllBytes($indexPath))).Replace('-','').Substring(0,12).ToLowerInvariant() }
        finally { $hasher.Dispose() }
    }
    $openUrl = "$serviceUrl/?v=$($health.version)&ui=$pageId"
    try { $Host.UI.RawUI.WindowTitle = "Xijing AI v$($health.version)" } catch {}
    Write-Host "Xijing AI v$($health.version) is running." -ForegroundColor Magenta
    Write-Host "Project: $projectRoot"
    Write-Host "Open: $openUrl"
    if (-not $NoBrowser) { Start-Process $openUrl }
}
$running = Get-ResearchServer
if ($running) {
    Show-ResearchServer $running
    exit 0
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts\setup.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\dist\index.html'))) { throw 'Frontend not built. Run scripts\setup.ps1 first.' }
if (Get-NetTCPConnection -LocalPort 8920 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8920 is occupied by another service.' }
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$outLog=Join-Path $runtimeRoot "server-$stamp.out.log"
$errLog=Join-Path $runtimeRoot "server-$stamp.err.log"
$serverProcess=Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8920') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
$deadline=(Get-Date).AddSeconds(30)
do {
    $running = Get-ResearchServer
    if ($running) {
        $listener=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8920 -State Listen | Select-Object -First 1
        $actual=Get-Process -Id $listener.OwningProcess
        @{processId=$actual.Id;startTimeUtc=$actual.StartTime.ToUniversalTime().ToString('o');url=$serviceUrl} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeRoot 'server.json') -Encoding UTF8
        Show-ResearchServer $running
        exit 0
    }
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) { throw "Startup failed. Inspect $errLog" }
    Start-Sleep -Milliseconds 400
} while ((Get-Date) -lt $deadline)
throw "Startup timed out. Inspect $errLog"
