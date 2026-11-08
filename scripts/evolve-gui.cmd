@echo off
rem Launch the EVOLVE desktop GUI (tkinter, stdlib-only).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m jarvis.evolve gui
