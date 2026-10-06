@echo off
setlocal
cd /d "%~dp0"

echo [1/6] Checking Python and PyInstaller...
python -c "import PyInstaller; assert PyInstaller.__version__ == '6.22.3'" >nul 2>&1
if errorlevel 1 (
    echo Installing pinned build dependencies...
    python -m pip install --quiet -r requirements-build.txt
    if errorlevel 1 (
        echo Build dependency installation failed.
        exit /b 1
    )
)

python tools\check_build_env.py --mode release
if errorlevel 1 (
    echo [FAILED] The build environment does not match the release toolchain.
    exit /b 1
)

echo [2/6] Stopping running LookUpWindows instances gracefully...
python tools\stop_running_instance.py
if errorlevel 1 (
    echo [FAILED] A running LookUp Windows instance could not be stopped safely.
    exit /b 1
)

echo [3/6] Regenerating app.ico...
python -c "import sys; sys.path.insert(0, 'src'); from pathlib import Path; import icon; raise SystemExit(0 if icon.ensure_ico_file(Path('app.ico')) else 1)"
if errorlevel 1 (
    echo [FAILED] app.ico generation failed.
    exit /b 1
)

echo [4/6] Verifying the source closure...
python tools\check_source_imports.py
if errorlevel 1 (
    echo [FAILED] Source import closure check failed.
    exit /b 1
)

echo [5/6] Invalidating the attestation of the previous release...
python tools\release_manifest.py --invalidate
if errorlevel 1 (
    echo [FAILED] The release attestation of the previous artifact could not be invalidated.
    exit /b 1
)

echo [6/6] Building portable dist\LookUpWindows.exe with startup splash...
python -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --name "LookUpWindows" --icon "app.ico" --splash "splash.png" src\app.py
if errorlevel 1 (
    echo [FAILED] Build failed.
    exit /b 1
)

echo Done: dist\LookUpWindows.exe
echo The release manifest is NOT written here: it is created after the gates have
echo run against these bytes (python tools\release_manifest.py --write).
endlocal
