@echo off
setlocal
title Industrial Vision API Server

:: Navigate to script directory
cd /d "%~dp0"

echo =======================================================
echo     Industrial Vision API - Server Bootstrapper
echo =======================================================
echo.

set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

:: 1. Check if .venv already exists
if exist "%VENV_PYTHON%" goto VENV_EXISTS

echo [!] Virtual environment (.venv) not detected.
echo [*] Scanning for system Python...

:: Check for 'py' launcher
where py >nul 2>&1
if %errorlevel% equ 0 (
    set "SYS_PYTHON=py"
    goto CREATE_VENV
)

:: Check for 'python'
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "SYS_PYTHON=python"
    goto CREATE_VENV
)

echo.
echo [ERROR] Python was not found in your system PATH.
echo Please install Python 3.10+ and make sure to check "Add Python to PATH".
echo.
pause
exit /b 1

:CREATE_VENV
echo [*] Found system Python: %SYS_PYTHON%
echo [*] Creating virtual environment in .venv...
%SYS_PYTHON% -m venv "%VENV_DIR%"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
echo [OK] Virtual environment created successfully.
echo.
echo [*] Installing dependencies from requirements.txt...
"%VENV_PYTHON%" -m pip install --upgrade pip
"%VENV_PYTHON%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARNING] Some dependencies may have failed to install.
) else (
    echo [OK] All dependencies installed successfully!
)

:VENV_EXISTS
echo [OK] Virtual environment detected: %VENV_DIR%

:: Check core dependency (uvicorn)
"%VENV_PYTHON%" -c "import uvicorn" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Installing requirements from requirements.txt...
    "%VENV_PYTHON%" -m pip install -r requirements.txt
)

:: Ensure runtime directories exist
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"
if not exist "uploads" mkdir "uploads"

echo.
echo =======================================================
echo   Starting Industrial Vision API Server...
echo   Dashboard:   http://localhost:8000/dashboard
echo   API Docs are disabled when APP_ENV=production and DEBUG=false.
echo =======================================================
echo.

"%VENV_PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000

if %errorlevel% neq 0 (
    echo.
    echo [!] Server stopped with exit code %errorlevel%.
    pause
)

