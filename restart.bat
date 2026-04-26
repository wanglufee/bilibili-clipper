@echo off
cd /d "%~dp0"
echo [Bilibili Clipper] Restarting...
echo   -^> stopping...
for /f %%a in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul ^| findstr /i "webui"') do (
    for /f "tokens=2 delims=," %%b in ("%%a") do (
        taskkill /f /pid %%b >nul 2>nul
        if not errorlevel 1 echo [OK] old process stopped
    )
)
timeout /t 2 /nobreak >nul
echo   -^> starting...
start /min "" python webui.py
echo [OK] Restart complete! Open http://localhost:5000
echo.
pause
