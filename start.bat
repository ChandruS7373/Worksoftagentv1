@echo off
title Worksoft AI Support Agent
color 0B
chcp 65001 >nul

echo.
echo  =====================================================
echo    Worksoft AI Support Agent - Starting...
echo  =====================================================
echo.

:: Check venv exists
if not exist venv (
    color 0C
    echo  [ERROR] Virtual environment not found.
    echo          Run install.bat first.
    echo.
    pause
    exit /b 1
)

:: Activate venv
call venv\Scripts\activate.bat
echo  [OK] Virtual environment activated

:: Check .env
if not exist .env (
    color 0E
    echo  [WARN] .env file not found - app may not work correctly
    echo         Run install.bat to create it
    echo.
)

echo  [..] Starting app on http://localhost:5000
echo  [..] Press Ctrl+C to stop
echo.
echo  =====================================================
echo.

:: Open browser after short delay
start "" cmd /c "timeout /t 3 >nul && start http://localhost:5000"

:: Launch Flask app
python app.py

:: If app exits
echo.
color 0C
echo  [!!] App stopped. Check the error above.
pause
