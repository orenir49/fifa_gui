@echo off
REM Double-clickable launcher for install.ps1.
REM -ExecutionPolicy Bypass applies to this process only: it does not change
REM machine policy and does not need admin rights.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo *** Install FAILED with exit code %RC% ***
)
echo.
pause
exit /b %RC%
