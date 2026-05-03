@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR=.venv"
if "%PYTHON_BIN%"=="" set "PYTHON_BIN=python"

where %PYTHON_BIN% >nul 2>&1
if errorlevel 1 (
    echo Errore: '%PYTHON_BIN%' non trovato. Installa Python 3 o esporta PYTHON_BIN.>&2
    exit /b 1
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creo virtualenv in %VENV_DIR%...
    %PYTHON_BIN% -m venv "%VENV_DIR%"
    if errorlevel 1 exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python -m pip install --upgrade pip
if errorlevel 1 exit /b 1

pip install -r requirements.txt
if errorlevel 1 exit /b 1

if not exist ".env" if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo Creato .env da .env.example - modifica i valori se necessario.
)

if not exist "soundfonts" mkdir "soundfonts"

echo Setup completato. Avvia con run.bat
endlocal
