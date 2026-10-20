@echo off
cd /d "%~dp0"
rem Setup only: environment and dependencies, without starting the app.
call "%~dp0Run.cmd" --no-launch
