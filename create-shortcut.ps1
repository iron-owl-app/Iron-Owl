# Creates (or updates) an "Iron Owl" shortcut on your Desktop that runs start.bat
# with the Iron Owl icon. Safe to run again at any time. An older "FinTrack" shortcut made by
# this script (one that runs this start.bat) is replaced.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "Iron Owl.lnk"
$startBat = Join-Path $root "start.bat"

$shell = New-Object -ComObject WScript.Shell
$legacy = Join-Path $desktop "FinTrack.lnk"
if (Test-Path -LiteralPath $legacy -PathType Leaf) {
    $target = $shell.CreateShortcut($legacy).TargetPath
    if ($target -and ($target -ieq $startBat)) { Remove-Item -LiteralPath $legacy -Force }
}

$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $startBat
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root "packaging\brand\iron-owl.ico") + ",0"
$shortcut.Description = "Start Iron Owl (local, encrypted personal finance)"
$shortcut.WindowStyle = 7  # start minimized; the app opens in your browser
$shortcut.Save()

Write-Host "Shortcut ready: $link" -ForegroundColor Green
