@echo off
rem Double-click to uninstall Lorakeet: removes its shortcuts, autostart and Python environment. Your logged data is kept unless you choose otherwise.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall %*
echo.
pause
