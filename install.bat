@echo off
title Installing Promptify...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Installation encountered an error.
    pause
) else (
    echo.
    echo Promptify is now installed and ready!
    timeout /t 3 >nul
)

