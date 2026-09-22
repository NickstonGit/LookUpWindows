@echo off
setlocal
cd /d "%~dp0"

if exist "dist\LookUpWindows\LookUpWindows.exe" (
    start "" "dist\LookUpWindows\LookUpWindows.exe"
    echo Started: dist\LookUpWindows\LookUpWindows.exe
    exit /b 0
)

if exist "dist\LookUpWindows.exe" (
    start "" "dist\LookUpWindows.exe"
    echo Started: dist\LookUpWindows.exe
    exit /b 0
)

set EXE=
for /f "delims=" %%F in ('dir /b /o-d "dist\LookUpWindows-*.exe" 2^>nul') do (
    if not defined EXE set EXE=dist\%%F
)

if not defined EXE (
    echo Executable not found in dist\.
    echo Run build.bat first.
    exit /b 1
)

start "" "%EXE%"
echo Started: %EXE%
endlocal
