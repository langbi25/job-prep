@echo off
rem job-prep one-click launcher: opens two windows (backend + frontend)
cd /d C:\Users\14096\WorkBuddy\2026-10-25-22-38-23\job-prep

set PY=C:\Users\14096\.workbuddy\binaries\python\envs\default\Scripts\python.exe

start "jobprep-api (8765)" cmd /k "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
timeout /t 2 >nul
start "jobprep-ui (8501)" cmd /k "%PY%" -m streamlit run ui/streamlit_app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true

echo.
echo  Backend : http://localhost:8765/docs
echo  UI      : http://localhost:8501
echo  Close the two popup windows to stop the services.
pause
