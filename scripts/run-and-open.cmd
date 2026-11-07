@echo off
REM Start the JARVIS Core in the background and open the HUD.
REM The console must not stay open: launch-hud.ps1 starts it hidden, waits for the
REM port, then opens the browser. Use stop-jarvis.cmd to stop it.
setlocal
cd /d "%~dp0.."
start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "scripts\launch-hud.ps1"
endlocal
