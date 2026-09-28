@echo off
REM Baut eine eigenstaendige Windows-.exe (ohne Python-Installation lauffaehig).
REM Aufruf aus dem Projektstamm:  packaging\build_exe.bat
REM Ergebnis:  dist\GHArchiveRepairTool.exe

cd /d "%~dp0.."

echo Installiere Build-Abhaengigkeiten ...
py -m pip install --quiet --upgrade pyinstaller customtkinter darkdetect "cryptography>=41"

echo Baue GHArchiveRepairTool.exe ...
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name GHArchiveRepairTool ^
  --collect-all customtkinter ^
  --collect-all darkdetect ^
  app.py

echo.
echo Fertig: dist\GHArchiveRepairTool.exe
