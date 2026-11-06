# install.ps1 - 1-Click Promptify Windows Installer
$ErrorActionPreference = "Stop"

$AppName = "Promptify"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$StartMenuPath = [Environment]::GetFolderPath("Programs")

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Installing Promptify for Windows" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# Find the best binary to install
$SourceExe = Join-Path $PSScriptRoot "release\Promptify.exe"
if (-not (Test-Path $SourceExe)) {
    $SourceExe = Join-Path $PSScriptRoot "downloads\Promptify.exe"
}
if (-not (Test-Path $SourceExe)) {
    $SourceExe = Join-Path $PSScriptRoot "dist\Promptify.exe"
}
if (-not (Test-Path $SourceExe)) {
    throw "Could not find Promptify.exe. Please run build_release.ps1 first."
}

# Stop any running instances before overwriting
Get-Process $AppName -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

# 1. Create target program directory
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$TargetExe = Join-Path $InstallDir "Promptify.exe"
Copy-Item -Path $SourceExe -Destination $TargetExe -Force
Write-Host "[OK] Installed binary to: $TargetExe" -ForegroundColor Green

# 2. Copy assets if present
$SourceAssets = Join-Path $PSScriptRoot "assets"
if (Test-Path $SourceAssets) {
    Copy-Item -Path $SourceAssets -Destination $InstallDir -Recurse -Force
}

# 3. Create Desktop Shortcut
$WshShell = New-Object -ComObject WScript.Shell
$DesktopShortcutPath = Join-Path $DesktopPath "$AppName.lnk"
$Shortcut = $WshShell.CreateShortcut($DesktopShortcutPath)
$Shortcut.TargetPath = $TargetExe
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.Description = 'Promptify - AI Prompt Enhancer and Voice Dictation'
$Shortcut.IconLocation = "$TargetExe,0"
$Shortcut.Save()
Write-Host "[OK] Created Desktop shortcut: $DesktopShortcutPath" -ForegroundColor Green

# 4. Create Start Menu Shortcut
$StartMenuShortcutPath = Join-Path $StartMenuPath "$AppName.lnk"
$StartShortcut = $WshShell.CreateShortcut($StartMenuShortcutPath)
$StartShortcut.TargetPath = $TargetExe
$StartShortcut.WorkingDirectory = $InstallDir
$StartShortcut.Description = 'Promptify - AI Prompt Enhancer and Voice Dictation'
$StartShortcut.IconLocation = "$TargetExe,0"
$StartShortcut.Save()
Write-Host "[OK] Created Start Menu shortcut: $StartMenuShortcutPath" -ForegroundColor Green

# 5. Register in Windows Startup Registry (HKCU\...\Run)
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$QuotedExe = [char]34 + $TargetExe + [char]34
Set-ItemProperty -Path $RunKey -Name $AppName -Value $QuotedExe
Write-Host "[OK] Registered in Windows Startup (Auto-boots with Windows)" -ForegroundColor Green

# 6. Launch Promptify
Start-Process -FilePath $TargetExe
Write-Host "[OK] Promptify launched and running in system tray!" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Installation Complete! You can now use Ctrl+Shift+Space for Voice and Ctrl+Alt+F for Prompts." -ForegroundColor Green
