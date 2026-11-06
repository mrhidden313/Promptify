# uninstall.ps1 - Promptify Windows Uninstaller
$ErrorActionPreference = "SilentlyContinue"

$AppName = "Promptify"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$StartMenuPath = [Environment]::GetFolderPath("Programs")

Write-Host "==========================================" -ForegroundColor Yellow
Write-Host "   Uninstalling Promptify" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Yellow

# 1. Stop running process
Get-Process $AppName -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

# 2. Remove Windows Startup Registry
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue
Write-Host "[OK] Removed from Windows Startup" -ForegroundColor Green

# 3. Remove Desktop Shortcut
$DesktopShortcutPath = Join-Path $DesktopPath "$AppName.lnk"
if (Test-Path $DesktopShortcutPath) {
    Remove-Item -Path $DesktopShortcutPath -Force
    Write-Host "[OK] Removed Desktop shortcut" -ForegroundColor Green
}

# 4. Remove Start Menu Shortcut
$StartMenuShortcutPath = Join-Path $StartMenuPath "$AppName.lnk"
if (Test-Path $StartMenuShortcutPath) {
    Remove-Item -Path $StartMenuShortcutPath -Force
    Write-Host "[OK] Removed Start Menu shortcut" -ForegroundColor Green
}

# 5. Remove installed directory
if (Test-Path $InstallDir) {
    Remove-Item -Path $InstallDir -Recurse -Force
    Write-Host "[OK] Removed installed files from: $InstallDir" -ForegroundColor Green
}

Write-Host "==========================================" -ForegroundColor Yellow
Write-Host "Promptify has been completely uninstalled." -ForegroundColor Green
