@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "APPLE_HOME=%~dp0.."
python run.py %*
pause
