@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0" || (
    echo [ERROR] Cannot enter script directory.
    pause
    exit /b 1
)

set "ROOT=%CD%"
set "URL=http://127.0.0.1:8080"
set "LOG_FILE=%ROOT%\bitgetfollow.log"
set "STAMP_FILE=%TEMP%\bitgetfollow_last_open_ts.txt"
set "OPEN_COOLDOWN=8"

echo.
echo   ======================================
echo     BitgetFollow Windows Launcher
echo   ======================================
echo.

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -V >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -V >nul 2>nul
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python 3 was not found.
    echo Please install Python 3 and enable "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo [1/3] Checking dependencies...
%PYTHON_CMD% -m pip install -q -r "%ROOT%\requirements.txt" >nul 2>nul
if errorlevel 1 (
    echo [WARN] Quick dependency check failed. Retrying with details...
    %PYTHON_CMD% -m pip install -r "%ROOT%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        echo Please check network/proxy and run:
        echo %PYTHON_CMD% -m pip install -r "%ROOT%\requirements.txt"
        echo.
        pause
        exit /b 1
    )
)

call :is_running
if %errorlevel% equ 0 (
    echo [2/3] Service is already running, opening browser...
) else (
    echo [2/3] Starting service in background...
    start "BitgetFollow" /min cmd /c "cd /d ""%ROOT%"" && %PYTHON_CMD% web.py >> ""%LOG_FILE%"" 2>&1"
    call :wait_ready
)

call :is_running
if %errorlevel% equ 0 (
    call :maybe_open_browser
    echo [3/3] Started successfully: %URL%
    exit /b 0
)

echo [ERROR] Service failed to start. Check log:
echo %LOG_FILE%
echo.
pause
exit /b 1

:is_running
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
exit /b %errorlevel%

:wait_ready
for /L %%I in (1,1,25) do (
    >nul ping 127.0.0.1 -n 2
    call :is_running
    if !errorlevel! equ 0 exit /b 0
)
exit /b 1

:maybe_open_browser
for /f %%T in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"') do set "NOW_TS=%%T"
set "LAST_TS=0"

if exist "%STAMP_FILE%" (
    set /p LAST_TS=<"%STAMP_FILE%"
)

2>nul set /a DIFF=NOW_TS-LAST_TS
if errorlevel 1 set "DIFF=%OPEN_COOLDOWN%"

if %DIFF% lss %OPEN_COOLDOWN% (
    echo [3/3] Browser open cooldown active, please open manually: %URL%
    exit /b 0
)

> "%STAMP_FILE%" echo %NOW_TS%
start "" "%URL%"
exit /b 0