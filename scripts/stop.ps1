param([string]$Workspace='')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$runtimeRoot = if ($Workspace) { $Workspace } elseif ($env:RESEARCH_DATA_DIR) { $env:RESEARCH_DATA_DIR } else { Join-Path $projectRoot 'var' }
$stateFile=Join-Path $runtimeRoot 'server.json'
if (-not (Test-Path -LiteralPath $stateFile)) { Write-Host 'No recorded Xijing AI server.'; exit 0 }
$state=Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$process=Get-Process -Id $state.processId -ErrorAction SilentlyContinue
if ($process -and $process.StartTime.ToUniversalTime() -eq ([datetimeoffset]$state.startTimeUtc).UtcDateTime) {
    Stop-Process -Id $process.Id
    Write-Host 'Xijing AI stopped. Active research workers finish separately; cancel them in the UI before stopping.'
} else { Write-Host 'The recorded process is no longer running. No process was stopped.' }
