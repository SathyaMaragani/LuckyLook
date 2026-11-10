@echo off
rem Double-click to install LuckyLook. Options: install.bat -NoStartup -NoLaunch
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
