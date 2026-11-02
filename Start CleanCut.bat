@echo off
rem Double-click this to start CleanCut. It opens in your web browser.
cd /d "%~dp0"

if not exist .env (
  copy .env.example .env >nul
  echo First time setup: Notepad will open your settings file.
  echo Change MEDIA_DIR to your movie folder, save, and close Notepad.
  notepad .env
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" -Record >nul 2>&1
)

rem Offer a new version if there is one. An update replaces this file, so the whole check is one
rem block (read before it runs); after an update the new copy of this file starts instead.
if not "%~1"=="updated" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" -Check
  if errorlevel 2 (
    call "%~f0" updated
    exit /b
  )
)

docker info >nul 2>&1
if errorlevel 1 (
  echo.
  echo Docker Desktop isn't running. Start Docker Desktop, wait until it says it's running,
  echo then double-click "Start CleanCut" again.
  pause
  exit /b 1
)

echo Starting CleanCut...
echo The very first time, this downloads about 13 GB and can take 30 minutes or more.
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Something went wrong. Take a screenshot of this window and send it to Luke.
  pause
  exit /b 1
)

rem Keeps the PC awake while CleanCut is waiting or working (runs hidden, also after every sign-in).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0keep-awake.ps1" -Install >nul 2>&1

echo Opening http://localhost:8800 ...
timeout /t 3 >nul
start "" http://localhost:8800
