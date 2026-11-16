@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -B run_app.py %*
    goto :end
)
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Setting up local Python environment ^(.venv^) for first-time launch...
    py -3 -m venv .venv 2>nul
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" -B run_app.py %*
        goto :end
    )
    py -3 -B run_app.py %*
    goto :end
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Setting up local Python environment ^(.venv^) for first-time launch...
    python -m venv .venv 2>nul
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" -B run_app.py %*
        goto :end
    )
    python -B run_app.py %*
    goto :end
)
echo Error: Python 3.11+ is required. Install Python from https://www.python.org/
pause
:end
