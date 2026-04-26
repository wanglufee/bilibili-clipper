@echo off
cd /d "%~dp0"
echo [Bilibili Clipper] Stopping...
for /f %%a in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul ^| findstr /i "webui"') do (
    for /f "tokens=2 delims=," %%b in ("%%a") do (
        taskkill /f /pid %%b >nul 2>nul
        if not errorlevel 1 echo [OK] PID %%b stopped
    )
)
echo.
pause
