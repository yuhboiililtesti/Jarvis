@echo off
REM Start the JARVIS Core (API + HUD). Ctrl+C to stop.
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)
echo Starting JARVIS... open http://127.0.0.1:8765/
"%PY%" -m jarvis serve %*
endlocal
