@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ------------------------------------------------------------
REM Upload pico_mpr_bridge project to Raspberry Pi Pico W
REM Requires: mpremote installed and Pico W connected via USB
REM Run this .bat from inside the pico_mpr_bridge folder
REM ------------------------------------------------------------

echo.
echo [1/6] Checking mpremote...
where mpremote >nul 2>nul
if errorlevel 1 (
    echo ERROR: mpremote not found. 
    echo Install with: pip install mpremote
    exit /b 1
)

set "PORT=%~1"
if "%PORT%"=="" (
    set /p PORT=[2/6] Enter Pico W COM port ^(e.g. COM5^): 
)

if "%PORT%"=="" (
    echo ERROR: No COM port provided.
    echo Example: upload_to_pico.bat COM5
    exit /b 1
)

set "MP_CONN=connect %PORT%"
echo [2/6] Checking Pico W connection on %PORT%...
mpremote %MP_CONN% fs ls >nul 2>nul
if errorlevel 1 (
    echo ERROR: Cannot connect to %PORT%.
    echo 1^) Confirm Pico W is connected and MicroPython is flashed.
    echo 2^) Check Device Manager for the correct COM port.
    echo 3^) Retry with: upload_to_pico.bat COMx
    exit /b 1
)

REM Use script location as project root
set "PROJECT_DIR=%~dp0"
pushd "%PROJECT_DIR%"

if not exist "main.py" (
    echo ERROR: main.py not found in %PROJECT_DIR%
    goto :upload_error
)
if not exist "config.py" (
    echo ERROR: config.py not found in %PROJECT_DIR%
    goto :upload_error
)
if not exist "core" (
    echo ERROR: core folder not found in %PROJECT_DIR%
    goto :upload_error
)
if not exist "interfaces" (
    echo ERROR: interfaces folder not found in %PROJECT_DIR%
    goto :upload_error
)
if not exist "utils" (
    echo ERROR: utils folder not found in %PROJECT_DIR%
    goto :upload_error
)
if not exist "lib" (
    echo ERROR: lib folder not found in %PROJECT_DIR%
    goto :upload_error
)

echo [3/6] Uploading top-level Python files...
for %%F in (*.py) do (
    echo   - %%F
    mpremote %MP_CONN% fs cp "%%F" ":%%F"
    if errorlevel 1 goto :upload_error
)

echo [4/6] Uploading folders...
mpremote %MP_CONN% fs cp -r "core" ":"
if errorlevel 1 goto :upload_error

mpremote %MP_CONN% fs cp -r "interfaces" ":"
if errorlevel 1 goto :upload_error

mpremote %MP_CONN% fs cp -r "utils" ":"
if errorlevel 1 goto :upload_error

mpremote %MP_CONN% fs cp -r "lib" ":"
if errorlevel 1 goto :upload_error

echo [5/6] Resetting Pico W...
mpremote %MP_CONN% reset
if errorlevel 1 goto :upload_error

echo [6/6] Done.
echo Upload complete. Pico W reset triggered.
echo.
echo Tip: To view logs:
echo   mpremote connect auto repl
echo.
popd
exit /b 0

:upload_error
echo.
echo ERROR: Upload failed.
echo Check file paths and Pico connection, then try again.
popd
exit /b 1