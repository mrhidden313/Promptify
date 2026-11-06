@echo off
title Uninstalling Promptify...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
echo.
echo Promptify uninstallation complete.
pause

