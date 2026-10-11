@echo off
REM Double-click to run FIFA Calibrate from the source checkout.
REM Pass --no-connect to open without touching the camera or stage.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m fifa_gui.apps.calibrate_main %*
