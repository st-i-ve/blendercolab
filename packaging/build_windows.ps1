# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"

# Build with the project's own interpreter, not whatever `python` happens to
# resolve to. PyInstaller freezes the environment it RUNS IN -- invoking a
# system Python that lacks PySide6/kaggle produces a small, broken exe that
# still reports "Build complete!" and only fails when the user launches it.
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    throw "Project virtualenv not found at $Py. Create it and install deps first: python -m venv .venv; .venv\Scripts\python.exe -m pip install PySide6 kaggle pyinstaller"
}

Push-Location $Root
try {
    # Fail loudly if the build interpreter cannot see what must be frozen.
    & $Py -c "import importlib.util,sys; m=[x for x in ('PySide6','kaggle','kagglesdk') if importlib.util.find_spec(x) is None]; sys.exit('MISSING from build interpreter: '+', '.join(m)) if m else print('build interpreter has PySide6, kaggle, kagglesdk')"
    if ($LASTEXITCODE -ne 0) { throw "Build interpreter is missing required packages (see above)." }

    & $Py -m pip install --upgrade --quiet pyinstaller
    & $Py -m PyInstaller --noconfirm --clean packaging/blendfleet.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    # A build that omits PySide6 still "succeeds" -- check the result is real.
    $exe = Join-Path $Root "dist\blendfleet.exe"
    if (-not (Test-Path $exe)) { throw "Build reported success but $exe does not exist." }
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)

    $warn = Join-Path $Root "build\blendfleet\warn-blendfleet.txt"
    if (Test-Path $warn) {
        $bad = Select-String -Path $warn -Pattern 'missing module named (PySide6|kaggle|kagglesdk)$'
        if ($bad) { throw "Build omitted required packages -- the exe would fail at launch:`n$($bad -join "`n")" }
    }
    if ($mb -lt 30) {
        throw "dist/blendfleet.exe is only $mb MB. PySide6 alone is larger, so something was not bundled."
    }

    Write-Host ""
    Write-Host "Built dist/blendfleet.exe ($mb MB)"
}
finally {
    Pop-Location
}
