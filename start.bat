@echo off
cd /d "%~dp0"
echo [Bilibili Clipper] Starting...
start /min "" python webui.py
echo [OK] Service started! Open http://localhost:5000
echo.
pause
