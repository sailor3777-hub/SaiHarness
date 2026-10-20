@echo off
cd /d "%~dp0"
rem SaiHarness launcher: checks Python and dependencies, installs what is missing (scripts\bootstrap.py), starts the app.
rem Keep this file ASCII-only: cmd mis-parses UTF-8 batch files after chcp 65001.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY call :find py -3
if not defined PY call :find python
if not defined PY goto nopython
%PY% scripts\bootstrap.py %*
if errorlevel 1 (
    if /i "%~1"=="--check" exit /b 1
    echo.
    pause
    exit /b 1
)
exit /b 0

:find
rem usable interpreter: the command from the arguments must be Python 3.11 or newer
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:nopython
echo Python 3.11 or newer was not found.
where winget >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.14 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this file again.
    pause
    exit /b 1
)
choice /c YN /m "Install Python 3.14 with winget now"
if errorlevel 2 exit /b 1
winget install -e --id Python.Python.3.14 --scope user
echo.
echo If the installation succeeded, close this window and run Run.cmd again ^(PATH is refreshed only in a new window^).
pause
exit /b 0
