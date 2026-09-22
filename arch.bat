@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo [1/2] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found.
    exit /b 1
)

set VERSION=
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"APP_VERSION =" src\config.py') do set VERSION=%%V
set VERSION=!VERSION: =!
set VERSION=!VERSION:"=!
if not defined VERSION set VERSION=unknown
set OUT=LookUpWindows-src-!VERSION!.zip
if exist "%OUT%" del /q "%OUT%"
if exist "%OUT%" (
    echo [FAILED] Cannot delete old archive.
    exit /b 1
)

echo [2/2] Packing sources into %OUT%...

set FILES=src\app.py src\winapi.py src\dwm.py src\winui.py src\icon.py src\trayicon.py src\config.py conftest.py tests\test_config_model.py tests\test_native_tk_boundary.py tests\test_pip_toggle_contract.py tests\test_runtime_architecture.py tests\test_source_parking_static.py tests\test_ui_contract.py config\settings.example.json README.md ROADMAP.md LICENSE NOTICE .gitignore .gitattributes .github\workflows\windows-ci.yml .github\workflows\release.yml app.ico splash.png build.bat build-onefile.bat arch.bat run.bat
set PRESENT=
for %%F in (%FILES%) do (
    if exist "%%F" set PRESENT=!PRESENT!,'%%F'
)
if "!PRESENT!"=="" (
    echo [FAILED] No source files found.
    exit /b 1
)
set PRESENT=!PRESENT:~1!

set PS=%TEMP%\lookup_archive_%RANDOM%.ps1
> "%PS%" echo $ErrorActionPreference = 'Stop'
>> "%PS%" echo try {
>> "%PS%" echo   Compress-Archive -Path @(!PRESENT!) -DestinationPath '%OUT%' -Force
>> "%PS%" echo } catch {
>> "%PS%" echo   exit 1
>> "%PS%" echo }

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" >nul 2>&1
set RC=%ERRORLEVEL%
del /q "%PS%" >nul 2>&1
if "%RC%"=="1" goto :failed

if not exist "%OUT%" goto :failed

echo Done: %OUT%
exit /b 0

:failed
echo [FAILED] Archive was not created.
exit /b 1