@echo off
setlocal
cd /d "%~dp0"

echo.
echo  =============================================
echo   NinjaAlert installer
echo =============================================
echo.

REM ---- 1) Python -----------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo  [X] Python not found.
    echo      Install Python 3.11+ from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during setup, then re-run.
    pause
    exit /b 1
)
python --version

REM ---- 2) virtualenv + dependencies ----------------------------------------
if not exist .venv (
    echo  [..] Creating virtual environment...
    python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
echo  [..] Installing dependencies...
pip install -r requirements.txt pyinstaller || goto :fail

REM ---- 3) build the single-file exe ----------------------------------------
if not exist dist\NinjaAlert.exe (
    echo  [..] Building dist\NinjaAlert.exe (one-time, ~1 min)...
    pyinstaller --noconsole --onefile --name NinjaAlert ^
        --add-data "assets;assets" --icon assets/app.ico ^
        --distpath dist --workpath build main.py || goto :fail
)

echo.
echo  =============================================
echo   Ready: dist\NinjaAlert.exe
echo   It lives in the tray after launch.
echo   Config: dist\config.json (created on first run)
echo =============================================
echo.
choice /c YN /m "Launch NinjaAlert now"
if errorlevel 2 goto :eof
start "" dist\NinjaAlert.exe
goto :eof

:fail
echo.
echo  [X] Install failed - see messages above.
pause
exit /b 1
