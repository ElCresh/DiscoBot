@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR=.venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Virtualenv non trovato. Esegui prima setup.bat>&2
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python main.py %*
exit /b %errorlevel%
