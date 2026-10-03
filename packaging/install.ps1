# Installs Iron Owl (internal name FinTrack) for the current Windows user (no administrator
# rights needed).
#
# Run it from the unpacked package folder, in a NORMAL PowerShell window, signed in as the
# person who will use Iron Owl:
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 [-SupportContact Sam]
#
# The Windows installer (installer\iron-owl.iss) runs it without questions:
#     install.ps1 -Unattended [-SupportContactFile <utf-8 file>] [-NoDesktopShortcut]
#                 [-LogFile <file>] [-ResultFile <file>]
#   -Unattended          never asks anything; a missing or blank contact keeps the saved one.
#   -SupportContactFile  the "who to call" name in a UTF-8 text file (a BOM is fine). A file, so
#                        the name never goes through a command line.
#   -LogFile             a transcript of this run (the installer shows its path on failure).
#   -ResultFile          gets "ok" written as the very last step (the installer checks it).
# Exit code 0 = installed; anything else = failed (the message says why).
#
# What it does:
#   * copies the app to %LOCALAPPDATA%\Programs\FinTrack (python\, versions\<v>\, runtimes\,
#     launch.pyw, launcher_core.py, uninstall.ps1, FinTrack.ico, THIRD_PARTY_NOTICES.md) and
#     precompiles it;
#   * writes state.json (current version, support contact) for the launcher;
#   * creates %LOCALAPPDATA%\FinTrack (data\, logs\, browser\, run\) private to this user, and
#     data\support_contact.json when a "who to call" name was given (Settings changes it later);
#   * adds "Iron Owl" shortcuts on the Desktop and in the Start Menu (not pinned), replacing an
#     older "FinTrack" shortcut of this install, and an entry in Settings > Apps so it can be
#     uninstalled from there.
# Existing data is never touched. Running it again upgrades or repairs the install.
[CmdletBinding()]
param(
    [string]$SupportContact,
    [string]$SupportContactFile,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\FinTrack"),
    [string]$DataHome = (Join-Path $env:LOCALAPPDATA "FinTrack"),
    [switch]$NoDesktopShortcut,
    [switch]$Unattended,
    [string]$LogFile,
    [string]$ResultFile
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3
$src = $PSScriptRoot

$appName = "Iron Owl"
$contactMax = 60            # backend config.SUPPORT_CONTACT_MAX
$contactFileMaxBytes = 4096

if ($ResultFile -and (Test-Path -LiteralPath $ResultFile)) { Remove-Item -LiteralPath $ResultFile -Force }
if ($LogFile) {
    try { Start-Transcript -LiteralPath $LogFile -Force | Out-Null } catch { $LogFile = $null }
}
trap {
    Write-Host ""
    Write-Host "$appName was not installed: $($_.Exception.Message)" -ForegroundColor Red
    if ($LogFile) { try { Stop-Transcript | Out-Null } catch { } }
    exit 1
}

function Write-Step([string]$text) { Write-Host $text -ForegroundColor Cyan }

function Get-CleanContact([string]$value) {
    # Printable characters only (no control or invisible formatting characters), trimmed,
    # at most $contactMax: the same rule as the app and the launcher.
    if (-not $value) { return "" }
    $bad = @([Globalization.UnicodeCategory]::Control, [Globalization.UnicodeCategory]::Format,
             [Globalization.UnicodeCategory]::LineSeparator, [Globalization.UnicodeCategory]::ParagraphSeparator,
             [Globalization.UnicodeCategory]::PrivateUse, [Globalization.UnicodeCategory]::OtherNotAssigned)
    $kept = $value.ToCharArray() | Where-Object { $bad -notcontains [Globalization.CharUnicodeInfo]::GetUnicodeCategory($_) }
    $clean = (-join $kept).Trim()
    if ($clean.Length -gt $contactMax) {
        $cut = $contactMax
        if ([char]::IsHighSurrogate($clean[$cut - 1])) { $cut-- }  # never split an emoji in half
        $clean = $clean.Substring(0, $cut).Trim()
    }
    return $clean
}

function Read-ContactFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "The support contact file is missing." }
    if ((Get-Item -LiteralPath $path).Length -gt $contactFileMaxBytes) { throw "The support contact file is too large." }
    # ReadAllText drops a UTF-8 byte-order mark.
    return [System.IO.File]::ReadAllText($path, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-SavedContact([string]$dataHome) {
    # data\support_contact.json (written by Settings or an earlier install), or $null.
    $path = Join-Path $dataHome "data\support_contact.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        if ((Get-Item -LiteralPath $path).Length -gt $contactFileMaxBytes) { return $null }
        $saved = [System.IO.File]::ReadAllText($path) | ConvertFrom-Json
        if ($saved -and ($saved.PSObject.Properties.Name -contains "support_contact") -and ($saved.support_contact -is [string])) {
            return (Get-CleanContact $saved.support_contact)
        }
    } catch { }
    return $null
}

function Write-JsonNoBom([string]$path, $object) {
    # Python reads these with encoding="utf-8": no byte-order mark. Temp file + rename.
    $json = $object | ConvertTo-Json -Depth 8
    $tmp = "$path.$([guid]::NewGuid().ToString('N')).tmp"
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $path -Force
}

function Stop-FinTrack([string]$root, [string]$dataHome) {
    # Ask the running server to stop (it locks the vault and backs up on the way out), then
    # make sure nothing from this install is still running.
    $control = Join-Path $dataHome "run\control.json"
    if (Test-Path -LiteralPath $control) {
        try {
            $c = Get-Content -LiteralPath $control -Raw | ConvertFrom-Json
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($c.port)/api/app/shutdown" `
                -Headers @{ "X-FinTrack" = "1"; "X-FinTrack-Control" = $c.secret } `
                -ContentType "application/json" -Body "{}" -TimeoutSec 5 -UseBasicParsing | Out-Null
            Write-Host "Stopping $appName..."
        } catch { }
    }
    $pythonDir = (Join-Path $root "python\").ToLowerInvariant()
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($pythonDir) })
        if ($procs.Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

function Set-PrivateAcl([string]$path) {
    # Only this user, SYSTEM and Administrators (same rule the app applies to data\).
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & "$env:SystemRoot\System32\icacls.exe" $path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not make $path private (icacls exit $LASTEXITCODE)" }
}

function New-Shortcut([string]$link, [string]$root) {
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($link)
    $s.TargetPath = Join-Path $root "python\pythonw.exe"
    $s.Arguments = '"' + (Join-Path $root "launch.pyw") + '"'
    $s.WorkingDirectory = $root
    $s.IconLocation = (Join-Path $root "FinTrack.ico") + ",0"
    $s.Description = "Open $appName"
    $s.WindowStyle = 1
    $s.Save()
}

function Test-OurShortcut([string]$link, [string]$root) {
    # True when $link exists and starts something inside this install (never someone else's).
    if (-not (Test-Path -LiteralPath $link -PathType Leaf)) { return $false }
    try {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($link).TargetPath
    } catch { return $false }
    return [bool]($target -and $target.ToLowerInvariant().StartsWith($root.TrimEnd('\').ToLowerInvariant() + "\"))
}

# --- checks -----------------------------------------------------------------------------

$principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run install.ps1 in a normal (not administrator) PowerShell window, signed in as the person who will use $appName. An elevated window can install into the wrong user's folders."
}
if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is not set." }
if ($PSBoundParameters.ContainsKey("SupportContact") -and $SupportContactFile) {
    throw "Pass -SupportContact or -SupportContactFile, not both."
}

$manifestPath = Join-Path $src "package.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "package.json is missing: run install.ps1 from the unpacked $appName package folder." }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$version = [string]$manifest.version
$depsId = [string]$manifest.deps_id
if ($version -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}$') { throw "Bad version in package.json" }
if ($depsId -notmatch '^[0-9a-f]{16}$') { throw "Bad deps_id in package.json" }
foreach ($rel in @("python\python.exe", "python\pythonw.exe", "launch.pyw", "launcher_core.py", "uninstall.ps1",
                   "FinTrack.ico", "versions\$version\release.json", "versions\$version\run_packaged.py",
                   "versions\$version\web\index.html", "runtimes\$depsId\site-packages")) {
    if (-not (Test-Path -LiteralPath (Join-Path $src $rel))) { throw "The package is incomplete: $rel is missing." }
}
if ((Resolve-Path -LiteralPath $src).Path.TrimEnd('\') -ieq $InstallRoot.TrimEnd('\')) {
    throw "Run install.ps1 from the package folder, not from the installed copy."
}
# Installing replaces python\ and prunes versions\ and runtimes\ in the install folder: only
# ever in an empty (or new) folder or an existing install, never a random folder.
if (Test-Path -LiteralPath $InstallRoot) {
    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) { throw "$InstallRoot is a file, not a folder." }
    $isInstall = (Test-Path -LiteralPath (Join-Path $InstallRoot "launch.pyw") -PathType Leaf) -or
                 (Test-Path -LiteralPath (Join-Path $InstallRoot "state.json") -PathType Leaf)
    if (-not $isInstall -and @(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -gt 0) {
        throw "$InstallRoot is not empty and isn't an $appName install (no launch.pyw or state.json). Choose an empty folder for -InstallRoot; nothing was changed."
    }
}

# --- support contact ----------------------------------------------------------------------

$statePath = Join-Path $InstallRoot "state.json"
$old = $null
if (Test-Path -LiteralPath $statePath) {
    try { $old = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { $old = $null }
}
$oldContact = Read-SavedContact $DataHome
if ($null -eq $oldContact) {
    $oldContact = if ($old -and ($old.PSObject.Properties.Name -contains "support_contact")) { Get-CleanContact ([string]$old.support_contact) } else { "" }
}
# $contactGiven: write the name (state.json and data\support_contact.json). Otherwise the
# saved one stays as it is.
$contactGiven = $false
if ($SupportContactFile) {
    $SupportContact = Get-CleanContact (Read-ContactFile $SupportContactFile)
    $contactGiven = [bool]$SupportContact -or -not $Unattended
} elseif ($PSBoundParameters.ContainsKey("SupportContact")) {
    $SupportContact = Get-CleanContact $SupportContact
    $contactGiven = [bool]$SupportContact -or -not $Unattended
} elseif (-not $Unattended) {
    $answer = Read-Host "Who should $appName tell the user to call when something goes wrong? (e.g. Sam) [$oldContact]"
    $SupportContact = if ($answer) { Get-CleanContact $answer } else { $oldContact }
    $contactGiven = $true
}
if (-not $contactGiven) { $SupportContact = $oldContact }

# --- copy -----------------------------------------------------------------------------------

Stop-FinTrack $InstallRoot $DataHome

Write-Step "Copying $appName $version to $InstallRoot..."
New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $InstallRoot "versions"), (Join-Path $InstallRoot "runtimes") | Out-Null

$pythonDest = Join-Path $InstallRoot "python"
if (Test-Path -LiteralPath $pythonDest) { Remove-Item -LiteralPath $pythonDest -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $src "python") -Destination $pythonDest -Recurse

$versionDest = Join-Path $InstallRoot "versions\$version"
if (Test-Path -LiteralPath $versionDest) { Remove-Item -LiteralPath $versionDest -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $src "versions\$version") -Destination $versionDest -Recurse

# A runtime is reused only when a finished copy wrote its marker (an interrupted copy has
# site-packages\ but no marker, and is copied again).
$runtimeDest = Join-Path $InstallRoot "runtimes\$depsId"
$runtimeMarker = Join-Path $runtimeDest ".install-complete"
$runtimeDone = (Test-Path -LiteralPath $runtimeMarker -PathType Leaf) -and
    (([string](Get-Content -LiteralPath $runtimeMarker -Raw)).Trim() -eq $depsId) -and
    (Test-Path -LiteralPath (Join-Path $runtimeDest "site-packages") -PathType Container)
if (-not $runtimeDone) {
    if (Test-Path -LiteralPath $runtimeDest) { Remove-Item -LiteralPath $runtimeDest -Recurse -Force }
    Copy-Item -LiteralPath (Join-Path $src "runtimes\$depsId") -Destination $runtimeDest -Recurse
    if (Test-Path -LiteralPath $runtimeMarker) { Remove-Item -LiteralPath $runtimeMarker -Force }
    [System.IO.File]::WriteAllText($runtimeMarker, $depsId, (New-Object System.Text.UTF8Encoding($false)))
}

foreach ($name in @("launch.pyw", "launcher_core.py", "uninstall.ps1", "FinTrack.ico")) {
    Copy-Item -LiteralPath (Join-Path $src $name) -Destination (Join-Path $InstallRoot $name) -Force
}
# Licenses travel with the app (older packages may not have them).
foreach ($name in @("THIRD_PARTY_NOTICES.md", "LICENSE")) {
    $from = Join-Path $src $name
    if (Test-Path -LiteralPath $from -PathType Leaf) { Copy-Item -LiteralPath $from -Destination (Join-Path $InstallRoot $name) -Force }
}
Get-ChildItem -LiteralPath $InstallRoot -Filter *.ps1 | Unblock-File

# Keep only the new version and the one it replaces (for going back), and their runtimes.
$previous = $null
if ($old -and ($old.PSObject.Properties.Name -contains "current")) {
    # Upgrading: the old version becomes "previous". Same version again: keep its "previous".
    $candidate = ""
    if ([string]$old.current -ne $version) { $candidate = [string]$old.current }
    elseif ($old.PSObject.Properties.Name -contains "previous") { $candidate = [string]$old.previous }
    if ($candidate -match '^\d{1,3}\.\d{1,3}\.\d{1,3}$' -and $candidate -ne $version -and
        (Test-Path -LiteralPath (Join-Path $InstallRoot "versions\$candidate\release.json"))) {
        $previous = $candidate
    }
}
$keepVersions = @($version) + @($previous | Where-Object { $_ })
Get-ChildItem -LiteralPath (Join-Path $InstallRoot "versions") -Directory | Where-Object { $keepVersions -notcontains $_.Name } |
    Remove-Item -Recurse -Force
$keepRuntimes = @()
foreach ($v in $keepVersions) {
    $rel = Get-Content -LiteralPath (Join-Path $InstallRoot "versions\$v\release.json") -Raw | ConvertFrom-Json
    $keepRuntimes += [string]$rel.deps_id
}
Get-ChildItem -LiteralPath (Join-Path $InstallRoot "runtimes") -Directory | Where-Object { $keepRuntimes -notcontains $_.Name } |
    Remove-Item -Recurse -Force

Write-Step "Preparing $appName (this takes a minute)..."
& (Join-Path $pythonDest "python.exe") -m compileall -q -j 0 $versionDest $runtimeDest (Join-Path $InstallRoot "launcher_core.py") | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Note: some files could not be precompiled; $appName will still work." -ForegroundColor Yellow }

# --- state.json -------------------------------------------------------------------------------

$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$state = [ordered]@{}
if ($old) { foreach ($p in $old.PSObject.Properties) { $state[$p.Name] = $p.Value } }
$state["schema"] = 1
$state["bootstrap_version"] = 1
$state["current"] = $version
$state["previous"] = $previous
$state["pending"] = $null
$state["rollback"] = $null
if (-not ($state.Contains("port")) -or -not ($state["port"] -is [int] -or $state["port"] -is [long])) { $state["port"] = 8000 }
if ($contactGiven -or -not $state.Contains("support_contact")) { $state["support_contact"] = $SupportContact }
if (-not $state.Contains("installed_at") -or -not $state["installed_at"]) { $state["installed_at"] = $now }
$state["last_install_at"] = $now
Write-JsonNoBom $statePath ([pscustomobject]$state)

# --- data folder (private) ----------------------------------------------------------------------

Write-Step "Making the data folder private..."
foreach ($d in @($DataHome, (Join-Path $DataHome "data"), (Join-Path $DataHome "logs"), (Join-Path $DataHome "browser"), (Join-Path $DataHome "run"))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Set-PrivateAcl $DataHome
if ($contactGiven) {
    # The app and the launcher read this first (Settings > Safety and backups changes it).
    Write-JsonNoBom (Join-Path $DataHome "data\support_contact.json") ([pscustomobject]@{ support_contact = $SupportContact })
}

# --- shortcuts and Settings > Apps entry --------------------------------------------------------------

Write-Step "Adding shortcuts..."
$startMenu = [Environment]::GetFolderPath("Programs")
$desktop = [Environment]::GetFolderPath("Desktop")
# Older versions named the shortcuts "FinTrack": replace ours (never someone else's).
$hadDesktopIcon = Test-OurShortcut (Join-Path $desktop "FinTrack.lnk") $InstallRoot
foreach ($legacy in @((Join-Path $startMenu "FinTrack.lnk"), (Join-Path $desktop "FinTrack.lnk"))) {
    if (Test-OurShortcut $legacy $InstallRoot) { Remove-Item -LiteralPath $legacy -Force }
}
New-Shortcut (Join-Path $startMenu "$appName.lnk") $InstallRoot
if (-not $NoDesktopShortcut -or $hadDesktopIcon) {
    New-Shortcut (Join-Path $desktop "$appName.lnk") $InstallRoot
}

$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FinTrack"
New-Item -Path $uninstallKey -Force | Out-Null
$sizeKb = [int]((Get-ChildItem -LiteralPath $InstallRoot -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1KB)
$values = @{
    DisplayName     = $appName
    DisplayVersion  = $version
    Publisher       = $appName
    InstallLocation = $InstallRoot
    DisplayIcon     = (Join-Path $InstallRoot "FinTrack.ico")
    InstallDate     = (Get-Date).ToString("yyyyMMdd")
    UninstallString = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot 'uninstall.ps1')`""
}
foreach ($k in $values.Keys) { Set-ItemProperty -Path $uninstallKey -Name $k -Value $values[$k] -Type String }
foreach ($k in @("NoModify", "NoRepair")) { Set-ItemProperty -Path $uninstallKey -Name $k -Value 1 -Type DWord }
Set-ItemProperty -Path $uninstallKey -Name "EstimatedSize" -Value $sizeKb -Type DWord

Write-Host ""
Write-Host "$appName $version is installed. Open it from the $appName icon in the Start menu$(if (-not $NoDesktopShortcut -or $hadDesktopIcon) { ' or on the desktop' })." -ForegroundColor Green
# The name and phone stay out of the -LogFile transcript (it is kept in %TEMP%).
if ($SupportContact -and $LogFile) { Write-Host "Messages will say who to call (the name you gave)." }
elseif ($SupportContact) { Write-Host "Messages will say: call $SupportContact." }
Write-Host "Data folder: $(Join-Path $DataHome 'data') (kept if $appName is uninstalled)."
if ($ResultFile) { [System.IO.File]::WriteAllText($ResultFile, "ok", (New-Object System.Text.UTF8Encoding($false))) }
if ($LogFile) { try { Stop-Transcript | Out-Null } catch { } }
exit 0
