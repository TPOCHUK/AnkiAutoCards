@echo off
setlocal
set "ANKI_CARDS_DIR=%~dp0"
powershell -NoProfile -Command "$w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Anki AutoCards.lnk'); $s.TargetPath = $env:ANKI_CARDS_DIR + 'Start.bat'; $s.WorkingDirectory = $env:ANKI_CARDS_DIR; $s.WindowStyle = 7; $s.Save()"
if errorlevel 1 pause
