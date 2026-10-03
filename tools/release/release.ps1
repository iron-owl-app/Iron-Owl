# Build a signed FinTrack update to email. Thin wrapper around build_update.py.
#   tools\release\release.ps1 1.5.0            # app-only update (small, emailable)
#   tools\release\release.ps1 1.6.0 -Kind full # new Python packages: bundles site-packages
#   tools\release\release.ps1 1.5.0 -Commit    # also commits "Release 1.5.0" and tags v1.5.0
# See tools\release\RELEASE_CHECKLIST.md before the first release.
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Version,
    [ValidateSet('app', 'full')][string]$Kind = 'app',
    [string]$MinCurrentVersion = '1.0.0',
    [switch]$Commit
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "backend\.venv is missing. Run start.ps1 once first." -ForegroundColor Yellow
    exit 1
}
$cliArgs = @((Join-Path $PSScriptRoot 'build_update.py'), '--version', $Version, '--kind', $Kind,
             '--min-current-version', $MinCurrentVersion)
if ($Commit) { $cliArgs += '--commit' }
& $py @cliArgs
exit $LASTEXITCODE
