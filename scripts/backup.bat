@echo off
rem Entry point for the daily scheduled backup (see docs/container-ops note).
rem Prefer in-container online backup; fall back to local venv (start.bat mode).
setlocal
set "ROOT=%~dp0.."
set "LOG=%ROOT%\backups\backup.log"
set "DOCKER=E:\Docker\resources\bin\docker.exe"
if not exist "%ROOT%\backups" mkdir "%ROOT%\backups"

echo [%date% %time%] ---- backup start ---- >> "%LOG%"

"%DOCKER%" ps --filter "name=job-prep-api-1" --filter "status=running" --format "{{.Names}}" 2>nul | findstr /c:"job-prep-api-1" >nul
if not errorlevel 1 goto :docker

echo [%date% %time%] container down, fallback to local venv >> "%LOG%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" goto :unavailable
pushd "%ROOT%"
"%PY%" scripts\backup_db.py --keep 7 >> "%LOG%" 2>&1
set "RC=%errorlevel%"
popd
echo [%date% %time%] ---- backup end rc=%RC% ---- >> "%LOG%"
exit /b %RC%

:docker
"%DOCKER%" exec job-prep-api-1 python scripts/backup_db.py --keep 7 >> "%LOG%" 2>&1
set "RC=%errorlevel%"
echo [%date% %time%] ---- backup end rc=%RC% ---- >> "%LOG%"
exit /b %RC%

:unavailable
echo [%date% %time%] SKIP: neither container nor local venv available >> "%LOG%"
exit /b 1
