@echo off
setlocal enabledelayedexpansion

:: 1. SET DEFAULTS
set "ENV_NAME=dev"
set "VENV_PATH=venv"
set "TAXCONFIG_DB="

:: 2. EVALUATE ARGUMENT 1
if not "%~1"=="" (
    if exist "%~1\" (
        set "VENV_PATH=%~1"
    ) else (
        set "ENV_NAME=%~1"
    )
)

:: 2b. EVALUATE ARGUMENT 3 for TAXCONFIG_DB override
if not "%~3"=="" (
    set "TAXCONFIG_DB=%~3"
)

:: 3. EVALUATE ARGUMENT 2
if not "%~2"=="" (
    if exist "%~2\" (
        set "VENV_PATH=%~2"
    ) else (
        :: Only set this as ENV_NAME if Arg 1 was used for VENV
        if exist "%~1\" set "ENV_NAME=%~2"
    )
)

:: 4. VENV ACTIVATION
if exist "!VENV_PATH!\Scripts\activate.bat" (
    echo 🐍 Venv: !VENV_PATH!
    call "!VENV_PATH!\Scripts\activate.bat"
) else (
    echo ℹ️ No venv at "!VENV_PATH!", using system Python.
)

:: 5. EXECUTION
echo 🔑 Target: !ENV_NAME!

:: Pass TAXCONFIG_DB override if set
set "TAXCONFIG_ARG="
if not "!TAXCONFIG_DB!"=="" set "TAXCONFIG_ARG=--taxconfig-db=!TAXCONFIG_DB!"

set "ENV_BAT_FILE="
set "ENV_PS1_FILE="

for /f "usebackq tokens=1,* delims==" %%A in (`python scripts/env_init.py !ENV_NAME! !TAXCONFIG_ARG! %*`) do (
    if /I "%%A"=="WRITTEN_TO" set "ENV_BAT_FILE=%%B"
    if /I "%%A"=="WRITTEN_PS1" set "ENV_PS1_FILE=%%B"
)

:: 6. THE HAND-OFF (The "Use" Phase)
:: This is where the variables move from the FILE to the CMD SESSION
if "!ENV_BAT_FILE!"=="" (
    echo 🚨 ERROR: Initialization file was not created.
    exit /b 1
)
if not exist "!ENV_BAT_FILE!" (
    echo 🚨 ERROR: Initialization file is missing: !ENV_BAT_FILE!
    exit /b 1
)
call "!ENV_BAT_FILE!"

:: --- THREE-STAGE AUDIT CLEANUP ---

:: STAGE 1: Call Python for Secure Wipe (Per-pass jitter)
if not "!ENV_BAT_FILE!"=="" (
    if not "!ENV_PS1_FILE!"=="" (
        python scripts\env_init.py --cleanup-only --cleanup-path "!ENV_BAT_FILE!" --cleanup-path "!ENV_PS1_FILE!"
    ) else (
        python scripts\env_init.py --cleanup-only --cleanup-path "!ENV_BAT_FILE!"
    )
)

:: STAGE 2: Standard Delete Fallback
if not "!ENV_BAT_FILE!"=="" if exist "!ENV_BAT_FILE!" (
    del /f /q "!ENV_BAT_FILE!" >nul 2>&1
)

:: STAGE 2b: Clean up PowerShell env file
if not "!ENV_PS1_FILE!"=="" if exist "!ENV_PS1_FILE!" (
    del /f /q "!ENV_PS1_FILE!" >nul 2>&1
)

:: STAGE 3: Final Audit and Escalation
if not "!ENV_BAT_FILE!"=="" if exist "!ENV_BAT_FILE!" (
    echo.
    echo *******************************************************************
    echo 🚨 ERROR: SENSITIVE FILE PERSISTS AND COULD NOT BE DELETED.
    echo    Location: !ENV_BAT_FILE!
    echo.
    echo    ADVICE: Manually delete this file immediately to protect 
    echo            your credentials.
    echo *******************************************************************
    pause
    exit /b 1
)

echo ✅ Environment initialized and sensitive data wiped.