@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python and PyInstaller...
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.10+ not found.
    exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install --quiet pyinstaller
    if errorlevel 1 (
        echo PyInstaller installation failed.
        exit /b 1
    )
)

echo [2/4] Stopping running LookUpWindows instances...
taskkill /f /im LookUpWindows.exe >nul 2>&1
powershell -NoProfile -Command "Get-Process -Name 'LookUpWindows-*' -ErrorAction SilentlyContinue | Stop-Process -Force" >nul 2>&1

echo [3/4] Regenerating app.ico...
python -c "import sys; sys.path.insert(0, 'src'); from pathlib import Path; import icon; raise SystemExit(0 if icon.ensure_ico_file(Path('app.ico')) else 1)"
if errorlevel 1 (
    echo [FAILED] app.ico generation failed.
    exit /b 1
)

echo [4/4] Building fast-start dist\LookUpWindows\LookUpWindows.exe...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "LookUpWindows" --icon "app.ico" src\app.py
if errorlevel 1 (
    echo [FAILED] Build failed.
    exit /b 1
)

echo Done: dist\LookUpWindows\LookUpWindows.exe
echo Fast-start onedir build is the recommended build.
endlocal
