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

set EXE=
for /f "delims=" %%F in ('dir /b /o-d "dist\LookUpWindows-*.exe" 2^>nul') do (
    if not defined EXE set EXE=dist\%%F
)

if not defined EXE (
    echo Executable not found in dist\.
    echo Run build-onefile.bat first.
    exit /b 1
)

start "" "%EXE%"
echo Started: %EXE%
endlocal
