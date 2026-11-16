@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo Run Start.bat first to install the application.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6,<7"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name AnkiAutoCards --collect-all langchain_openai --collect-all langchain_anthropic --collect-all langgraph --collect-all edge_tts --collect-all keyring launcher.pyw
if errorlevel 1 goto fail
echo Ready: dist\AnkiAutoCards\AnkiAutoCards.exe
echo Keep the entire dist\AnkiAutoCards folder together.
pause
exit /b 0
:fail
echo Build failed. Check the error above.
pause
exit /b 1
