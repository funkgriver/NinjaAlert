@echo off
setlocal
cd /d "%~dp0"

REM ---- 1. virtualenv -------------------------------------------------------
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || (echo "python not found - install Python 3.11+ first" & goto :fail)
)
call .venv\Scripts\activate.bat

REM ---- 2. dependencies ------------------------------------------------------
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || goto :fail
pip install pyinstaller || goto :fail

REM ---- 3. optional exe icon ---------------------------------------------------
set ICON=
if not exist assets\app.ico (
    python tools\make_icon.py >nul 2>&1
)
if exist assets\app.ico set ICON=--icon assets\app.ico

REM ---- 4. build ---------------------------------------------------------------
pyinstaller --noconsole --onefile --name NinjaAlert %ICON% main.py || goto :fail

echo.
echo ============================================================
echo  Build OK: dist\NinjaAlert.exe
echo  Copy config.json next to the exe for portable settings.
echo ============================================================
goto :eof

:fail
echo BUILD FAILED.
exit /b 1
