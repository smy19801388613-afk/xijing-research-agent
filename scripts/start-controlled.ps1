param([string]$Workspace='', [string]$Policy='', [switch]$NoBrowser)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
if (-not $Workspace) { $Workspace=Join-Path $projectRoot 'var-controlled' }
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$env:RESEARCH_DATA_DIR=(Resolve-Path -LiteralPath $Workspace).Path
$env:RESEARCH_COMPETITION_MODE='1'
Remove-Item Env:RESEARCH_COMPETITION_POLICY_JSON -ErrorAction SilentlyContinue
if ($Policy) { $env:RESEARCH_COMPETITION_POLICY=(Resolve-Path -LiteralPath $Policy).Path }
else { $env:RESEARCH_COMPETITION_POLICY=Join-Path $env:RESEARCH_DATA_DIR 'competition-policy.json' }
& (Join-Path $PSScriptRoot 'start.ps1') -NoBrowser:$NoBrowser
