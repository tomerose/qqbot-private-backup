@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start_all_services.ps1" > "%~dp0startup.log" 2>&1
