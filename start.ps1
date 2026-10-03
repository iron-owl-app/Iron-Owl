# FinTrack launcher: sets up on first run, then starts the app at http://127.0.0.1:<PORT>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# Refuse to run half-finished code: uncommitted changes to the app's code mean an update
# is in progress, and its database migrations must never touch your real vault early.
if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path "$root\.git")) {
    $changed = & git -C $root status --porcelain -- backend/app backend/requirements.txt backend/requirements-dev.txt frontend/src frontend/index.html 2>$null
    if ($changed) {
        Write-Host ""
        Write-Host "FinTrack is in the middle of an update (its code has uncommitted changes)." -ForegroundColor Yellow
        Write-Host "Starting now could run unfinished code against your data, so FinTrack won't start." -ForegroundColor Yellow
        Write-Host "Wait until the update is committed, then start it again." -ForegroundColor Yellow
        exit 1
    }
}

if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Host "Created .env from .env.example. To link banks, add your Plaid keys in the app under Settings > Bank connection." -ForegroundColor Yellow
}

$py = "$root\backend\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating Python environment..." -ForegroundColor Cyan
    python -m venv "$root\backend\.venv"
    & $py -m pip install --quiet --upgrade pip
}

# Reinstall Python packages when the requirements change (stamp = the files' hashes).
# requirements.txt is what the app needs; requirements-dev.txt adds the test tools and
# includes requirements.txt, so this copy (used for development too) installs both.
$reqFile = "$root\backend\requirements.txt"
$devReqFile = "$root\backend\requirements-dev.txt"
$reqHash = (Get-FileHash $reqFile).Hash
if (Test-Path $devReqFile) {
    $reqHash = "$reqHash;$((Get-FileHash $devReqFile).Hash)"
    $reqFile = $devReqFile
}
$reqStamp = "$root\backend\.venv\.requirements.sha256"
if (-not (Test-Path $reqStamp) -or (Get-Content $reqStamp) -ne $reqHash) {
    Write-Host "Installing Python packages..." -ForegroundColor Cyan
    & $py -m pip install --quiet -r $reqFile
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    Set-Content -Path $reqStamp -Value $reqHash
}

# Rebuild the frontend when anything under frontend/ (or HELP.md, which the Help page bundles) is newer than the last build.
$dist = "$root\frontend\dist\index.html"
$newest = Get-ChildItem "$root\frontend\src", "$root\frontend\public", "$root\frontend\index.html", "$root\frontend\package-lock.json", "$root\HELP.md" -Recurse -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not (Test-Path $dist) -or $newest.LastWriteTime -gt (Get-Item $dist).LastWriteTime) {
    Write-Host "Building frontend..." -ForegroundColor Cyan
    Push-Location "$root\frontend"
    $lockHash = (Get-FileHash "package-lock.json").Hash
    $lockStamp = "node_modules\.lock.sha256"
    if (-not (Test-Path $lockStamp) -or (Get-Content $lockStamp) -ne $lockHash) {
        npm ci
        if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm ci failed" }
        Set-Content -Path $lockStamp -Value $lockHash
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "frontend build failed" }
    Pop-Location
}

$port = 8000
$portLine = Select-String -Path "$root\.env" -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
if ($portLine) { $port = $portLine.Matches[0].Groups[1].Value }

# Open the browser as soon as FinTrack answers (up to 60s, then open it anyway so any
# problem is visible), instead of guessing with a fixed wait.
Start-Job -ScriptBlock {
    param($u)
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$u/api/health" -TimeoutSec 2 -UseBasicParsing
            if ($health.app -eq "fintrack") { break }
        } catch { }
        Start-Sleep -Milliseconds 300
    }
    Start-Process $u
} -ArgumentList "http://127.0.0.1:$port" | Out-Null
Write-Host "FinTrack running at http://127.0.0.1:$port  (Ctrl+C to stop - this also locks your data)" -ForegroundColor Green
Push-Location "$root\backend"
try { & $py run.py } finally {
    Pop-Location
    # Ctrl+C or a crash: don't leave the browser-opening job behind (it would still open
    # a tab up to 60 s later).
    Get-Job | Stop-Job -PassThru -ErrorAction SilentlyContinue | Remove-Job -Force -ErrorAction SilentlyContinue
}
