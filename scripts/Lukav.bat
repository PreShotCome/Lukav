@echo off
REM Lukav.bat — double-click launcher for Windows.
REM
REM On first run this creates a venv under .venv\, installs the deps,
REM then starts Lukav in a native window (or browser fallback). On
REM subsequent runs it just starts the server.
REM
REM Place a shortcut to this file on the Desktop or pin to taskbar.

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [lukav] first-time setup — creating venv...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 (
        echo [lukav] could not create venv. Install Python 3.10+ from python.org and re-run.
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    echo [lukav] installing dependencies (this only happens once)...
    pip install --quiet --upgrade pip
    pip install --quiet -e ".[plaid,secrets,desktop]"
    if errorlevel 1 (
        echo [lukav] dependency install failed. Check your internet connection and re-run.
        pause
        exit /b 1
    )
) else (
    call .venv\Scripts\activate.bat
)

echo [lukav] starting Lukav in a native window...
python -m lukav --window
endlocal
