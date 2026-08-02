# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean packaging/blendfleet.spec
Write-Host ""
Write-Host "Built dist/blendfleet.exe"
Write-Host "Expect 80-150 MB: PySide6 is bundled whole."
