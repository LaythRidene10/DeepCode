@echo off
setlocal
title deepcode telegram
cd /d "%~dp0"

rem Needs DEEPCODE_TG_TOKEN and DEEPCODE_TG_ALLOW in the environment.
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.10+ is required but "python" is not on PATH.
  echo Install it from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

python telegram_bot.py %*
if errorlevel 1 (
  echo.
  echo telegram bot stopped with an error.
  pause
)
endlocal
