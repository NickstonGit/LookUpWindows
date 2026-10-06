@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking PowerShell and Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [FAILED] Python not found.
    exit /b 1
)
powershell -NoProfile -Command "$PSVersionTable.PSVersion.Major" >nul 2>&1
if errorlevel 1 (
    echo [FAILED] PowerShell not found.
    exit /b 1
)

echo [2/3] Validating source tree...
if not exist "src\app.py" (
    echo [FAILED] src\app.py not found.
    exit /b 1
)
if not exist "tools\package_source.ps1" (
    echo [FAILED] tools\package_source.ps1 not found.
    exit /b 1
)

echo [3/3] Packing and atomically replacing LookUpWindows-src.zip...
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\package_source.ps1"
if errorlevel 1 (
    echo [FAILED] Archive was not created. Previous archive was preserved.
    exit /b 1
)
exit /b 0
