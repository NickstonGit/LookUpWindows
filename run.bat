@echo off
setlocal
cd /d "%~dp0"

rem The project has exactly one build: the onefile EXE. There is no onedir
rem variant, so the only artifact this script can start is dist\LookUpWindows.exe.

if exist "dist\LookUpWindows.exe" (
    start "" "dist\LookUpWindows.exe"
    echo Started: dist\LookUpWindows.exe
    exit /b 0
)

echo Executable not found in dist\.
echo Run build-onefile.bat first.
exit /b 1
