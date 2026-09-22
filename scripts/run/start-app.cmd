@echo off
rem Double-click entry for start-app.ps1 (Windows cannot run .ps1 by double-click).
rem Keep this file ASCII-only: cmd.exe reads it with the OEM codepage.
rem Usage: start-app.cmd [ps1 args]   e.g. start-app.cmd -Dev
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-app.ps1" %*
if errorlevel 1 pause
