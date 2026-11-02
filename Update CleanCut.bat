@echo off
rem Double-click this to get the newest version of CleanCut. Your settings are kept.
cd /d "%~dp0"

rem The update replaces this file, so everything runs from one block (read before it starts).
(
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
  if errorlevel 2 (
    call "%~dp0Start CleanCut.bat" updated
  ) else (
    pause
  )
  exit /b
)
