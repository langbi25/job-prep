@echo off
chcp 65001 >nul
rem job-prep 一键启动（Windows）
rem 前端已改成 Vue3：FastAPI 直接托管 frontend/dist，页面和接口都在 8765 一个端口。
rem 旧的 Streamlit（8501）只作为备用界面保留，日常不用启动。
cd /d %~dp0

set "PY=C:\Users\14096\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 找不到 Python：%PY%
  echo 改一下本脚本里的 PY 变量，或先用 venv 建环境再 pip install -r requirements.txt
  pause
  exit /b 1
)

"%PY%" -c "import uvicorn" 2>nul
if errorlevel 1 (
  echo [错误] 这个 Python 环境里没有 uvicorn，先执行：
  echo     "%PY%" -m pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "frontend\dist\index.html" (
  echo [提示] 前端产物不存在，界面会打不开。先构建：
  echo     cd frontend ^&^& npm install ^&^& npm run build
  echo.
)

start "jobprep (8765)" cmd /k "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
timeout /t 3 >nul
start "" http://localhost:8765

echo.
echo   页面 : http://localhost:8765
echo   接口 : http://localhost:8765/docs
echo   停止 : 关掉标题为 jobprep (8765) 的那个窗口
echo.
echo   改前端时改用开发模式（热更新，5173 代理到 8765）：
echo     cd frontend ^&^& npm run dev
echo.
pause
