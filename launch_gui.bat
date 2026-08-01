@echo off
setlocal enableextensions
rem =============================================================================
rem  UCEI GUI launcher (Windows) -- replaces launch_gui.sh (Pi/bash only).
rem  - Honors UCEI_TARGET (default LOCAL). See config.py for profiles.
rem  - For LOCAL: if nothing is listening on 5001, starts OctoPrint from octovenv
rem    in the background and waits for it. For non-LOCAL (e.g. PI) it assumes a
rem    remote OctoPrint and does NOT start a local server.
rem  - Then activates venv and runs the GUI.
rem =============================================================================

cd /d "%~dp0"

if "%UCEI_TARGET%"=="" set "UCEI_TARGET=LOCAL"
echo [launch_gui] UCEI_TARGET=%UCEI_TARGET%

if /I not "%UCEI_TARGET%"=="LOCAL" goto run_gui

rem --- LOCAL: ensure a local OctoPrint is running on port 5001 ---
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo [launch_gui] OctoPrint already listening on 5001 - reusing it.
    goto run_gui
)

echo [launch_gui] Nothing on port 5001 - starting OctoPrint from octovenv...
start "OctoPrint (UCEI)" /min "%~dp0octovenv\Scripts\octoprint.exe" serve --port 5001

echo [launch_gui] Waiting for OctoPrint to listen on 5001...
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){ if(Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue){ exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
    echo [launch_gui] ERROR: OctoPrint did not start listening on 5001 in time.
    echo [launch_gui] Try running "octovenv\Scripts\octoprint.exe serve --port 5001" manually.
    pause
    exit /b 1
)

rem OctoPrint binds a short-lived intermediary server before the real API is up;
rem give it a few seconds of grace so the GUI's first API call succeeds.
timeout /t 5 /nobreak >nul

:run_gui
echo [launch_gui] Activating venv and launching GUI...
call "%~dp0venv\Scripts\activate.bat"
python -u sprayer_controller.py
set "RC=%ERRORLEVEL%"
echo [launch_gui] GUI exited with code %RC%.
endlocal & exit /b %RC%
