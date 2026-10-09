@echo off
rem Double-click to install or update Lorakeet (runs install.ps1, which explains each step and asks before changing anything).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
