@echo off
REM Launch the Transact Access Manager on Windows
REM Usage: double-click this file or run from a terminal

cd /d "%~dp0"

REM Create venv if it doesn't exist
if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Python 3.10+ is required. Make sure python is on your PATH.
        pause
        exit /b 1
    )
)

REM Activate and install
call venv\Scripts\activate.bat

echo Checking dependencies...
pip install -q -r requirements_transact.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo Starting Transact Access Manager...
python transact_access_manager.py
if errorlevel 1 (
    echo.
    echo Application exited with an error. Check the output above.
    pause
)
