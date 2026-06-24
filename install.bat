@echo off
title Worksoft AI Support - Installer
color 0B
chcp 65001 >nul

echo.
echo  =====================================================
echo    Worksoft AI Support Agent - Install
echo  =====================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  [ERROR] Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  [OK] Python %PY_VER% found

:: Create virtual environment
if exist venv (
    echo  [OK] Virtual environment already exists - skipping creation
) else (
    echo  [..] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        color 0C
        echo  [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created
)

:: Activate venv
call venv\Scripts\activate.bat
echo  [OK] Virtual environment activated

:: Upgrade pip silently
echo  [..] Upgrading pip...
python -m pip install --upgrade pip --quiet --no-cache-dir >nul 2>&1
echo  [OK] pip upgraded

:: Clear corrupted pip cache
echo  [..] Clearing pip cache...
pip cache purge >nul 2>&1
echo  [OK] Cache cleared

:: Install dependencies
echo  [..] Installing dependencies...
pip install -r requirements.txt --no-cache-dir --quiet
if errorlevel 1 (
    color 0C
    echo  [ERROR] Dependency installation failed. Check requirements.txt
    pause
    exit /b 1
)
echo  [OK] All dependencies installed

:: Copy .env if missing
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo  [OK] .env created from .env.example - fill in your API keys
    ) else (
        echo  [!!] No .env file found - create one with your API keys
    )
) else (
    echo  [OK] .env file found
)

echo.
echo  =====================================================
echo    Installation complete! Run start.bat to launch.
echo  =====================================================
echo.
pause
