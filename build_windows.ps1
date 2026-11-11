# Builds dist\rtmp-multiview.exe (single file, Windows x64).
# Needs Python 3.10+ on PATH. Run from this folder:
#   powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".build-venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "==> Creating build venv"
    python -m venv $venv
}
$py = "$venv\Scripts\python.exe"
Write-Host "==> Installing numpy, pillow, pyinstaller"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet numpy pillow pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "==> Building"
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name rtmp-multiview `
    --distpath dist --workpath .build --specpath .build `
    --exclude-module tkinter --exclude-module unittest `
    multiview.py
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

Copy-Item config.example.json dist\config.example.json -Force
Copy-Item README.md dist\README.md -Force
Write-Host ""
Write-Host "Done: $(Resolve-Path dist\rtmp-multiview.exe)"
Write-Host "Put ffmpeg.exe next to it (or have ffmpeg on PATH) and install the NDI 6 Runtime/Tools for NDI output."
