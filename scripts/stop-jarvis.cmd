@echo off
REM Stop the hidden JARVIS Core started by run-and-open.cmd.
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\cloud\start-core.ps1" -Stop
endlocal
