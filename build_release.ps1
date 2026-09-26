$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python tools\generate_brand_assets.py
if ($LASTEXITCODE -ne 0) {
    throw "Could not generate the Windows icon."
}

$pyinstallerArgs = @(
    "--clean",
    "--noconfirm",
    "--onefile",
    "--noconsole",
    "--name=Promptify",
    "--icon=assets/promptify.ico",
    "--add-data=assets/promptify-icon.png;assets",
    "main.py"
)

python -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller release build failed."
}

New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "release") | Out-Null
Copy-Item (Join-Path $PSScriptRoot "dist\Promptify.exe") (Join-Path $PSScriptRoot "release\Promptify.exe") -Force

New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "downloads") | Out-Null
Copy-Item (Join-Path $PSScriptRoot "dist\Promptify.exe") (Join-Path $PSScriptRoot "downloads\Promptify.exe") -Force

Get-Item (Join-Path $PSScriptRoot "release\Promptify.exe"),
         (Join-Path $PSScriptRoot "downloads\Promptify.exe") |
    Select-Object FullName, Length, LastWriteTime