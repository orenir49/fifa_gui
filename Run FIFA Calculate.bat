@echo off
REM Double-click to run FIFA Calculate from the source checkout.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m fifa_gui.apps.calculate_main %*
