@echo off
rem Capture control from the repository (the installed copy lives in %USERPROFILE%\bin).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0screen-capture.ps1" %*
