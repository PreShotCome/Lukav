@echo off
REM Lukav.bat - double-click launcher for Windows.
REM
REM First run: creates .venv, installs deps, then starts the server.
REM Later runs: just activates the venv and starts the server.
REM
REM Place a shortcut to this file on the Desktop or pin to taskbar.

setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto activate

echo [lukav] first-time setup, creating venv...
where py >nul 2>nul
if errorlevel 1 goto venv_python
goto venv_py

:venv_py
py -3 -m venv .venv
if errorlevel 1 goto venv_failed
goto install

:venv_python
python -m venv .venv
if errorlevel 1 goto venv_failed
goto install

:venv_failed
echo [lukav] could not create venv. Install Python 3.10+ from python.org and re-run.
pause
exit /b 1

:install
call .venv\Scripts\activate.bat
echo [lukav] installing dependencies, this only happens once...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[plaid,secrets,desktop]"
if errorlevel 1 goto install_failed
goto run

:install_failed
echo [lukav] dependency install failed. Check your internet connection and re-run.
pause
exit /b 1

:activate
call .venv\Scripts\activate.bat
goto run

:run
echo [lukav] starting Lukav in a native window...
python -m lukav --window
endlocal
