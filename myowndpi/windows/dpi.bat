@echo off
rem myowndpi (Windows): dpi.bat start ^| stop ^| status ^| log ^| discord ^| roblox
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dpi.ps1" %*
