# Builds the Iron Owl Windows package (internal name FinTrack; a folder, optionally zipped) that
# install.ps1 installs and installer\iron-owl.iss wraps into the one-click setup .exe.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\release\build_package.ps1 `
#       -PythonZip C:\Downloads\python-3.11.9-embed-amd64.zip -PythonZipSha256 <sha256> [-Zip]
#
# One-time step (the owner, by hand): download the official CPython 3.11 x64 *embeddable*
# package from python.org (3.11.9 is the last 3.11 release with Windows binaries; FinTrack
# needs 3.11 because sqlcipher3-wheels is cp311-only). Check its SHA-256 against the value
# published for that file on python.org and pass it as -PythonZipSha256: the build refuses a
# zip whose hash differs.
#
# Output: dist-package\Iron-Owl-<version>\ with
#   install.ps1, uninstall.ps1, launch.pyw, launcher_core.py, package.json,
#   FinTrack.ico (the Iron Owl icon, packaging\brand\iron-owl.ico),
#   THIRD_PARTY_NOTICES.md (and LICENSE when the repo has one)
#   python\                             the embeddable Python, unchanged
#   versions\<v>\ backend\app\**.py, run_packaged.py, web\ (frontend\dist), release.json
#                                       (+ update_source/update_repo for -UpdateSource github)
#   runtimes\<deps_id>\site-packages\   backend\requirements.txt for cp311 win_amd64 (wheels only,
#                                       except plaid-python: built from its locked sdist, see below)
# Whitelist copy only: never data\, env/settings files, design\, .claude\, .git\, tests.
#
# Committed code only: the build refuses uncommitted or untracked changes in what it packages
# (backend\app, requirements, run_packaged.py, frontend, packaging\, tools\release\) unless
# -AllowDirty is passed (test builds; package.json then records git_dirty = true).
#
# Runtime lock: tools\release\requirements-lock.txt pins every runtime package (dependencies
# included) with its sha256. When it exists the runtime is installed from it with
# make_lock.py install-runtime: the lock's allowlisted sdists (plaid-python publishes no wheel)
# are first built into wheels in a fresh venv holding only the hash-pinned build backend from
# tools\release\build-lock.txt (pip wheel --no-deps --require-hashes --no-build-isolation),
# then pip --require-hashes --no-deps --only-binary=:all: installs the lock, finding those
# wheels with --find-links. The build refuses a lock whose deps_id doesn't match
# backend\requirements.txt. Make or refresh both files after changing requirements.txt (needs
# the network), then commit them:
#     backend\.venv\Scripts\python.exe tools\release\make_lock.py generate
# Without a lock the build still works from requirements.txt, with a warning.
# -ReuseRuntime <...\runtimes\<id>\site-packages>: the folder's packages (pip list) must match
# the lock (or, without one, every pin in requirements.txt), or the build stops.
# -RequireLock: refuse to build without the lock (the GitHub release build passes it).
#
# Update source (Iron Owl 2.0.0): -UpdateSource file (default: the owner's emailed builds;
# release.json gets NO source keys, which installed versions before 2.0.0 require) or github
# with -UpdateRepo <owner>/<name> (the public build: the app checks that repo's Releases).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonZip,
    [Parameter(Mandatory = $true)][string]$PythonZipSha256,
    [string]$OutDir,
    [string]$BuildPython,
    [switch]$SkipFrontendBuild,
    # Offline / no-change rebuilds: copy this existing runtimes\<deps_id>\site-packages
    # instead of downloading wheels with pip.
    [string]$ReuseRuntime,
    # pip --find-links folder with the wheels (plus --no-index), for offline builds. With the
    # lock it must also hold the allowlisted sdists and the build-lock.txt wheels.
    [string]$WheelDir,
    [switch]$Zip,
    # Package uncommitted changes anyway (test builds only).
    [switch]$AllowDirty,
    [switch]$RequireLock,
    [ValidateSet("file", "github")][string]$UpdateSource = "file",
    [string]$UpdateRepo
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# The same rule as the app (backend/app/updates/source.py valid_repo) and build_update.py.
function Test-RepoName([string]$value) {
    if ($value -cnotmatch '^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$') { return $false }
    $owner = $Matches[1]; $name = $Matches[2]
    if ($owner.StartsWith("-") -or $owner.EndsWith("-") -or $owner.Contains("--")) { return $false }
    if ($name -eq "." -or $name -eq ".." -or $name.ToLowerInvariant().EndsWith(".git")) { return $false }
    return $true
}
if ($UpdateSource -eq "github") {
    if (-not (Test-RepoName $UpdateRepo)) { throw "-UpdateSource github needs -UpdateRepo <owner>/<name>." }
} elseif ($UpdateRepo) {
    throw "-UpdateRepo is only for -UpdateSource github."
}
if (-not $OutDir) { $OutDir = Join-Path $repo "dist-package" }
if (-not $BuildPython) { $BuildPython = Join-Path $repo "backend\.venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $BuildPython)) { throw "Build Python not found: $BuildPython (pass -BuildPython)" }

function Step([string]$text) { Write-Host "== $text" -ForegroundColor Cyan }

function Write-JsonNoBom([string]$path, $object) {
    $json = $object | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Copy-Whitelisted([string]$from, [string]$to, [scriptblock]$allow, [string]$what) {
    $fromFull = (Resolve-Path -LiteralPath $from).Path.TrimEnd('\')
    $files = Get-ChildItem -LiteralPath $fromFull -Recurse -File
    $count = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($fromFull.Length + 1)
        $relSlash = $rel -replace '\\', '/'
        if ($relSlash -match '(^|/)__pycache__/') { continue }
        if ($relSlash -match '\.md$') { continue }  # READMEs are for developers (as build_update.py)
        if (-not (& $allow $relSlash)) { throw "$what`: refusing to package unexpected file '$relSlash'" }
        $dest = Join-Path $to $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $dest
        $count++
    }
    if ($count -eq 0) { throw "$what`: nothing to copy from $from" }
    return $count
}

# --- version, schema, deps_id -----------------------------------------------------------------

$versionText = Get-Content -LiteralPath (Join-Path $repo "backend\app\version.py") -Raw
if ($versionText -notmatch '__version__\s*=\s*"(\d{1,3}\.\d{1,3}\.\d{1,3})"') { throw "Can't read backend\app\version.py" }
$version = $Matches[1]
$migrations = Get-Content -LiteralPath (Join-Path $repo "backend\app\migrations.py") -Raw
if ($migrations -notmatch '(?m)^LATEST\s*=\s*(\d+)') { throw "Can't read LATEST from backend\app\migrations.py" }
$schemaVersion = [int]$Matches[1]
$coreText = Get-Content -LiteralPath (Join-Path $repo "packaging\launcher\launcher_core.py") -Raw
if ($coreText -notmatch '(?m)^BOOTSTRAP_VERSION\s*=\s*(\d+)') { throw "Can't read BOOTSTRAP_VERSION" }
$bootstrapVersion = [int]$Matches[1]
$requirements = Join-Path $repo "backend\requirements.txt"
$depsId = (& $BuildPython (Join-Path $repo "tools\release\deps_id.py") $requirements).Trim()
if ($LASTEXITCODE -ne 0 -or $depsId -notmatch '^[0-9a-f]{16}$') { throw "deps_id failed" }
Step "Iron Owl $version (schema $schemaVersion, runtime $depsId, updates: $UpdateSource)"

$dirty = & git -C $repo status --porcelain --untracked-files=all -- backend/app backend/requirements.txt backend/run_packaged.py `
    frontend/src frontend/public frontend/index.html frontend/package.json frontend/package-lock.json packaging tools/release `
    THIRD_PARTY_NOTICES.md LICENSE 2>$null
$gitOk = ($LASTEXITCODE -eq 0)
if (-not $gitOk -or $dirty) {
    $why = if (-not $gitOk) { "git status failed (is $repo a git checkout?)" } else { "uncommitted changes:`n$(@($dirty) -join "`n")" }
    if (-not $AllowDirty) { throw "Refusing to build from $why`nCommit them first (or pass -AllowDirty for a test build)." }
    Write-Host "Warning (-AllowDirty): packaging $why" -ForegroundColor Yellow
}
$gitDirty = [bool](-not $gitOk -or $dirty)

# --- runtime lock -------------------------------------------------------------------------------

$makeLock = Join-Path $repo "tools\release\make_lock.py"
$lockFile = Join-Path $repo "tools\release\requirements-lock.txt"
$hasLock = Test-Path -LiteralPath $lockFile -PathType Leaf
if ($hasLock) {
    $lockId = [string](& $BuildPython $makeLock lock-id $lockFile)
    if ($LASTEXITCODE -ne 0 -or $lockId.Trim() -ne $depsId) {
        throw "tools\release\requirements-lock.txt doesn't match backend\requirements.txt (lock $($lockId.Trim()), requirements $depsId). Refresh it: $BuildPython tools\release\make_lock.py generate"
    }
    Write-Host "  runtime lock: tools\release\requirements-lock.txt (hash-checked)"
    # The build backend for the lock's sdists (install-runtime refuses a lock with sdists and no
    # matching build-lock.txt; checked here too so a stale one stops the build early).
    $buildLock = Join-Path $repo "tools\release\build-lock.txt"
    if (Test-Path -LiteralPath $buildLock -PathType Leaf) {
        $buildId = [string](& $BuildPython $makeLock lock-id $buildLock)
        if ($LASTEXITCODE -ne 0 -or $buildId.Trim() -ne $depsId) {
            throw "tools\release\build-lock.txt doesn't match backend\requirements.txt (build lock $($buildId.Trim()), requirements $depsId). Refresh it: $BuildPython tools\release\make_lock.py generate"
        }
    }
} elseif ($RequireLock) {
    throw "No tools\release\requirements-lock.txt (-RequireLock). Make it with make_lock.py generate and commit it."
} else {
    Write-Host "Warning: no tools\release\requirements-lock.txt, so runtime wheels are not hash-checked. See tools\release\make_lock.py." -ForegroundColor Yellow
}
$expectedPins = if ($hasLock) { $lockFile } else { $requirements }

# --- embeddable Python (verified) ----------------------------------------------------------------------

if ((Split-Path -Leaf $PythonZip) -notmatch '^python-3\.11\.\d+-embed-amd64\.zip$') {
    throw "Expected python-3.11.<n>-embed-amd64.zip, got $(Split-Path -Leaf $PythonZip)"
}
$actual = (Get-FileHash -LiteralPath $PythonZip -Algorithm SHA256).Hash
if ($actual -ne $PythonZipSha256.Trim().ToUpperInvariant()) {
    throw "SHA-256 mismatch for $PythonZip`n  expected $($PythonZipSha256.ToUpperInvariant())`n  actual   $actual"
}

# --- frontend -----------------------------------------------------------------------------------------

$dist = Join-Path $repo "frontend\dist"
if (-not $SkipFrontendBuild) {
    Step "Building the frontend"
    Push-Location (Join-Path $repo "frontend")
    try {
        if (-not (Test-Path "node_modules")) { npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed" } }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $dist "index.html"))) { throw "frontend\dist\index.html is missing" }

# --- stage ----------------------------------------------------------------------------------------------

$stage = Join-Path $OutDir "Iron-Owl-$version"
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Step "Python"
Expand-Archive -LiteralPath $PythonZip -DestinationPath (Join-Path $stage "python")
foreach ($f in @("python.exe", "pythonw.exe", "python311._pth", "python311.zip")) {
    if (-not (Test-Path -LiteralPath (Join-Path $stage "python\$f"))) { throw "The embeddable zip has no $f" }
}

Step "Runtime packages"
$site = Join-Path $stage "runtimes\$depsId\site-packages"
New-Item -ItemType Directory -Force -Path $site | Out-Null
$installedTxt = Join-Path $stage "runtimes\$depsId\installed.txt"
if ($ReuseRuntime) {
    if (-not (Test-Path -LiteralPath $ReuseRuntime -PathType Container)) { throw "-ReuseRuntime: $ReuseRuntime is not a folder" }
    Copy-Item -Path (Join-Path $ReuseRuntime "*") -Destination $site -Recurse
} else {
    if ($hasLock) {
        # Hash-checked: wheels from the lock, plus the allowlisted sdists built into wheels first
        # with the pinned build backend (see "Runtime lock" above and make_lock.py).
        $lockArgs = @($makeLock, "install-runtime", $lockFile, $site, "--python-version", "3.11")
        if ($WheelDir) { $lockArgs += @("--wheel-dir", $WheelDir) }
        & $BuildPython @lockArgs
        if ($LASTEXITCODE -ne 0) { throw "Installing the runtime from the lock failed (make_lock.py install-runtime)" }
    } else {
        $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                     "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.11",
                     "--implementation", "cp", "--target", $site, "-r", $requirements)
        if ($WheelDir) { $pipArgs += @("--no-index", "--find-links", $WheelDir) }
        & $BuildPython @pipArgs
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    }
    $bin = Join-Path $site "bin"
    if (Test-Path -LiteralPath $bin) { Remove-Item -LiteralPath $bin -Recurse -Force }  # console scripts: unused
}
# installed.txt = what the runtime folder really holds; it must match the lock (a reused
# runtime from an older build included).
& $BuildPython -m pip list --disable-pip-version-check --path $site --format freeze |
    Set-Content -LiteralPath $installedTxt -Encoding ascii
if ($LASTEXITCODE -ne 0) { throw "pip list failed" }
& $BuildPython $makeLock check-installed $installedTxt $expectedPins
if ($LASTEXITCODE -ne 0) {
    $what = if ($ReuseRuntime) { "-ReuseRuntime $ReuseRuntime" } else { "the installed runtime" }
    throw "$what doesn't match $(Split-Path -Leaf $expectedPins) (differences above)."
}

Step "App $version"
$vdir = Join-Path $stage "versions\$version"
$n = Copy-Whitelisted (Join-Path $repo "backend\app") (Join-Path $vdir "backend\app") {
    param($rel) $rel -match '^([a-z0-9_]+/)*[a-z0-9_]+\.py$'
} "backend\app"
Write-Host "  backend: $n files"
Copy-Item -LiteralPath (Join-Path $repo "backend\run_packaged.py") -Destination (Join-Path $vdir "run_packaged.py")
$n = Copy-Whitelisted $dist (Join-Path $vdir "web") {
    param($rel) $rel -match '^[A-Za-z0-9_./-]+\.(html|js|css|svg|png|ico|webmanifest|json|woff|woff2|txt)$' -and $rel -notmatch '(^|/)\.'
} "frontend\dist"
Write-Host "  web: $n files"
$releaseInfo = [ordered]@{
    version = $version; schema_version = $schemaVersion; python = "3.11"
    bootstrap_version = $bootstrapVersion; kind = "full"; deps_id = $depsId
}
if ($UpdateSource -eq "github") {
    # Only GitHub builds carry these: installed versions before 2.0.0 refuse unknown keys.
    $releaseInfo["update_source"] = "github"
    $releaseInfo["update_repo"] = $UpdateRepo
}
Write-JsonNoBom (Join-Path $vdir "release.json") $releaseInfo

Step "Launcher and scripts"
foreach ($f in @("packaging\launcher\launch.pyw", "packaging\launcher\launcher_core.py", "packaging\install.ps1", "packaging\uninstall.ps1")) {
    Copy-Item -LiteralPath (Join-Path $repo $f) -Destination $stage
}
Copy-Item -LiteralPath (Join-Path $repo "packaging\brand\iron-owl.ico") -Destination (Join-Path $stage "FinTrack.ico")
# Licenses that travel with the app (install.ps1 copies them into the install folder).
Copy-Item -LiteralPath (Join-Path $repo "THIRD_PARTY_NOTICES.md") -Destination $stage
if (Test-Path -LiteralPath (Join-Path $repo "LICENSE") -PathType Leaf) {
    Copy-Item -LiteralPath (Join-Path $repo "LICENSE") -Destination $stage
}
$commit = (& git -C $repo rev-parse --short HEAD 2>$null)
Write-JsonNoBom (Join-Path $stage "package.json") ([ordered]@{
    app = "fintrack"; version = $version; deps_id = $depsId; schema_version = $schemaVersion
    bootstrap_version = $bootstrapVersion; python_zip = (Split-Path -Leaf $PythonZip)
    python_zip_sha256 = $actual; git_commit = [string]$commit; git_dirty = $gitDirty
    runtime_locked = [bool]$hasLock; update_source = $UpdateSource
    built_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
})

# --- never ship secrets or data ------------------------------------------------------------------------------

Step "Checking the package"
$forbidden = Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object {
    $n = $_.Name.ToLowerInvariant()
    ($_.PSIsContainer -and ($n -in @("data", "design", ".claude", ".git", "node_modules", "tests"))) -or
    ($n -eq ".env") -or ($n.StartsWith(".env.")) -or ($n -eq "fintrack.env") -or
    ($n -like "keyfile.json*") -or ($n -like "*.db") -or ($n -like "*.ftbackup") -or ($n -eq "control.json")
} | Where-Object { $_.FullName -notlike "$site\*" -or $_.Name -like "*.db" -or $_.Name -like ".env*" }
if ($forbidden) {
    $list = ($forbidden | ForEach-Object { $_.FullName.Substring($stage.Length + 1) }) -join "`n  "
    throw "Refusing to build: the package contains files that must never ship:`n  $list"
}

$sizeMb = [math]::Round(((Get-ChildItem -LiteralPath $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Package folder: $stage ($sizeMb MB)" -ForegroundColor Green

if ($Zip) {
    $zipPath = Join-Path $OutDir "Iron-Owl-$version-install.zip"
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Compress-Archive -Path $stage -DestinationPath $zipPath
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    Write-Host "Zip: $zipPath`nSHA-256: $zipHash" -ForegroundColor Green
}
Write-Host "Install on the user's PC: unzip, then in a normal PowerShell window run" -ForegroundColor Green
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\Iron-Owl-$version\install.ps1 -SupportContact Sam"
Write-Host "Or make the one-click installer (needs Inno Setup 6): installer\build_installer.ps1 -PackageDir `"$stage`"" -ForegroundColor Green
