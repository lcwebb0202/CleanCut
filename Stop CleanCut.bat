@echo off
rem Double-click this to stop CleanCut (it otherwise keeps running whenever Docker Desktop is open).
cd /d "%~dp0"
docker compose down
echo CleanCut is stopped.
timeout /t 3 >nul
