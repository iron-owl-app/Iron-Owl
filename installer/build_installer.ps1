# Builds the Iron Owl setup .exe (Inno Setup 6.3 or newer) from the package that
# tools\release\build_package.ps1 made.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 `
#       [-PackageDir dist-package\Iron-Owl-<version>] [-OutDir dist-installer] [-Iscc <ISCC.exe>]
#
# The version comes from backend\app\version.py (the one place it lives) and must match the
# package's package.json. Output: <OutDir>\Iron-Owl-Setup-<version>.exe and its .sha256.
# Unsigned for 2.0.0.
[CmdletBinding()]
param(
    [string]$PackageDir,
    [string]$OutDir,
    [string]$Iscc
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$versionText = Get-Content -LiteralPath (Join-Path $repo "backend\app\version.py") -Raw
if ($versionText -notmatch '__version__\s*=\s*"(\d{1,3}\.\d{1,3}\.\d{1,3})"') { throw "Can't read backend\app\version.py" }
$version = $Matches[1]

if (-not $PackageDir) { $PackageDir = Join-Path $repo "dist-package\Iron-Owl-$version" }
if (-not (Test-Path -LiteralPath $PackageDir -PathType Container)) {
    throw "No package at $PackageDir. Run tools\release\build_package.ps1 first."
}
$PackageDir = (Resolve-Path -LiteralPath $PackageDir).Path
$manifest = Get-Content -LiteralPath (Join-Path $PackageDir "package.json") -Raw | ConvertFrom-Json
if ([string]$manifest.version -ne $version) {
    throw "The package is version $($manifest.version) but backend\app\version.py says $version. Rebuild the package."
}
foreach ($rel in @("install.ps1", "uninstall.ps1", "launch.pyw", "FinTrack.ico", "THIRD_PARTY_NOTICES.md", "python\pythonw.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $rel))) { throw "The package is incomplete: $rel is missing." }
}

if (-not $OutDir) { $OutDir = Join-Path $repo "dist-installer" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

if (-not $Iscc) {
    $candidates = @()
    if (${env:ProgramFiles(x86)}) { $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
    if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe" }
    $Iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $Iscc) {
        $cmd = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
        if ($cmd) { $Iscc = $cmd.Source }
    }
    if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) was not found. Install Inno Setup 6.3 or newer, or pass -Iscc." }
}

Write-Host "== Iron Owl $version setup from $PackageDir" -ForegroundColor Cyan
& $Iscc "/Qp" "/DAppVersion=$version" "/DPackageDir=$PackageDir" "/DOutputDir=$OutDir" (Join-Path $PSScriptRoot "iron-owl.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)." }

$exe = Join-Path $OutDir "Iron-Owl-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "ISCC did not make $exe" }
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText("$exe.sha256", "$hash  Iron-Owl-Setup-$version.exe`n", (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Setup: $exe`nSHA-256: $hash" -ForegroundColor Green
