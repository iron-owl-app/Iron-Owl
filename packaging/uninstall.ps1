# Uninstalls Iron Owl (internal name FinTrack) for the current Windows user.
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\FinTrack\uninstall.ps1"
#
# Settings > Apps > Iron Owl > Uninstall runs this too (with its window hidden). It removes the
# program, the shortcuts and the Settings > Apps entry. YOUR DATA IS KEPT (%LOCALAPPDATA%\FinTrack\data,
# and the app window's browser profile), so reinstalling Iron Owl finds everything again.
# It asks with a normal Windows Yes/No box (never a typed answer) and says when it's done.
#   -RemoveData   also delete %LOCALAPPDATA%\FinTrack (data, backups, browser profile, logs).
#                 This cannot be undone. For use in a PowerShell window only: it asks there,
#                 and you must type DELETE first.
#   -Yes          don't ask and show no boxes (for -RemoveData you must still pass -Yes
#                 explicitly to skip typing DELETE).
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [switch]$Yes,
    [string]$InstallRoot,
    [string]$DataHome = (Join-Path $env:LOCALAPPDATA "FinTrack")
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3

# The normal path (Settings > Apps, no -RemoveData): Windows boxes, no console prompts.
$useBoxes = -not $Yes -and -not $RemoveData
$boxTitle = "Uninstall Iron Owl"

function Show-Box([string]$text, [string]$buttons = "OK", [string]$icon = "Information") {
    Add-Type -AssemblyName System.Windows.Forms
    # An invisible always-on-top owner keeps the box in front of the Settings window.
    $owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false }
    try {
        return [string][System.Windows.Forms.MessageBox]::Show($owner, $text, $boxTitle,
            [System.Windows.Forms.MessageBoxButtons]::$buttons, [System.Windows.Forms.MessageBoxIcon]::$icon)
    } finally { $owner.Dispose() }
}

# On the normal path any failure ends in a plain box, not an invisible error in a hidden
# window. Otherwise the error stops the script as before.
trap {
    if ((Test-Path variable:useBoxes) -and $useBoxes) {
        Show-Box "Iron Owl couldn't be fully removed.`n`n$($_.Exception.Message)`n`nYour data was not touched." "OK" "Warning" | Out-Null
        exit 1
    }
    break
}

if (-not $InstallRoot) {
    $InstallRoot = if ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot "launch.pyw"))) {
        $PSScriptRoot
    } else {
        Join-Path $env:LOCALAPPDATA "Programs\FinTrack"
    }
}
$InstallRoot = $InstallRoot.TrimEnd('\')

# Refuse anything that doesn't look like a FinTrack install (never delete a random folder):
# an install root always has launch.pyw.
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot "launch.pyw") -PathType Leaf)) {
    if (Test-Path -LiteralPath $InstallRoot) { throw "$InstallRoot doesn't look like an Iron Owl install (no launch.pyw); nothing was removed." }
    Write-Host "Iron Owl is not installed in $InstallRoot."
}

# -RemoveData deletes a whole folder: only one that looks like a FinTrack home (its name
# ends in FinTrack and it holds data\, logs\ or run\).
$DataHome = $DataHome.TrimEnd('\')
if ($RemoveData -and (Test-Path -LiteralPath $DataHome)) {
    $leaf = Split-Path -Leaf $DataHome
    $looksLikeHome = $leaf -and $leaf.EndsWith("FinTrack", [System.StringComparison]::OrdinalIgnoreCase) -and
        (@("data", "logs", "run") | Where-Object { Test-Path -LiteralPath (Join-Path $DataHome $_) -PathType Container })
    if (-not $looksLikeHome) {
        throw "$DataHome doesn't look like an Iron Owl data folder (its name must end in FinTrack and it must contain data, logs or run); nothing was removed."
    }
}

if ($useBoxes) {
    $answer = Show-Box ("Remove Iron Owl from this PC?`n`n" +
        "Your data will be kept: your accounts, history, budgets and backups stay on this PC. " +
        "If you install Iron Owl again, everything will still be there.") "YesNo" "Question"
    if ($answer -ne "Yes") { exit 0 }  # nothing was changed
} elseif ($RemoveData -and -not $Yes) {
    $answer = Read-Host "Uninstall Iron Owl? (y/n)"
    if ($answer -notmatch '^(y|yes)$') { Write-Host "Nothing was changed."; exit 0 }
}
if ($RemoveData -and -not $Yes) {
    Write-Host "This also deletes ALL Iron Owl data in $DataHome (your accounts, history and backups)." -ForegroundColor Yellow
    $confirm = Read-Host "Type DELETE to delete the data too, or press Enter to keep it"
    if ($confirm -cne "DELETE") { $RemoveData = $false; Write-Host "Keeping the data." }
}

function Stop-FinTrack([string]$root, [string]$dataHome) {
    $control = Join-Path $dataHome "run\control.json"
    if (Test-Path -LiteralPath $control) {
        try {
            $c = Get-Content -LiteralPath $control -Raw | ConvertFrom-Json
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($c.port)/api/app/shutdown" `
                -Headers @{ "X-FinTrack" = "1"; "X-FinTrack-Control" = $c.secret } `
                -ContentType "application/json" -Body "{}" -TimeoutSec 5 -UseBasicParsing | Out-Null
            Write-Host "Stopping Iron Owl (it locks and backs up first)..."
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

function Remove-OurShortcut([string]$link, [string]$root) {
    if (-not (Test-Path -LiteralPath $link)) { return }
    $shell = New-Object -ComObject WScript.Shell
    $target = $shell.CreateShortcut($link).TargetPath
    if ($target -and $target.ToLowerInvariant().StartsWith($root.ToLowerInvariant() + "\")) {
        Remove-Item -LiteralPath $link -Force
    }
}

Stop-FinTrack $InstallRoot $DataHome

# "Iron Owl" shortcuts, and "FinTrack" ones from older versions (only ones that start this install).
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    foreach ($name in @("Iron Owl.lnk", "FinTrack.lnk")) { Remove-OurShortcut (Join-Path $folder $name) $InstallRoot }
}

$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FinTrack"
if (Test-Path $uninstallKey) { Remove-Item -Path $uninstallKey -Recurse -Force }

Set-Location -LiteralPath $env:TEMP  # can't delete the folder we are standing in
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}

if ($RemoveData) {
    if (Test-Path -LiteralPath $DataHome) { Remove-Item -LiteralPath $DataHome -Recurse -Force }
    Write-Host "Iron Owl and all its data were removed." -ForegroundColor Green
} else {
    $run = Join-Path $DataHome "run"
    if (Test-Path -LiteralPath $run) { Remove-Item -LiteralPath $run -Recurse -Force }
    Write-Host "Iron Owl was removed. Your data is still in $(Join-Path $DataHome 'data'); installing Iron Owl again will find it." -ForegroundColor Green
    if ($useBoxes) {
        Show-Box ("Iron Owl was removed.`n`nYour data is still on this PC, so if you install Iron Owl again, " +
            "everything will be there.") | Out-Null
    }
}
