@echo off
rem Reproducible Windows build: creates .venv if needed, installs pinned-range
rem dependencies, runs the tests, builds the exe, the installer and checksums.
rem Usage: scripts\build_windows.bat [--skip-tests] [--no-installer]
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv || goto :error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip >nul || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :error
".venv\Scripts\python.exe" scripts\build.py %* || goto :error

echo.
echo Build finished. See the release\ folder.
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
