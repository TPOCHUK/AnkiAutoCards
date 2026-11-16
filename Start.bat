@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto setup
if not exist ".venv\installed.ok" goto install
start "" ".venv\Scripts\pythonw.exe" "launcher.pyw"
exit /b 0
:setup
py -3 --version >nul 2>&1
if errorlevel 1 goto missing
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto missing
py -3 -m venv .venv
if errorlevel 1 goto failed
:install
echo Installing dependencies. First launch can take a few minutes...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo ready>".venv\installed.ok"
start "" ".venv\Scripts\pythonw.exe" "launcher.pyw"
exit /b 0
:missing
echo Install Python 3.11 or 3.12 from python.org with the Python Launcher.
echo Python 3.10+ is required. Then run Start.bat again.
pause
exit /b 1
:failed
echo Setup failed. Check your internet connection and the error above.
pause
exit /b 1
