@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
  echo [NetResearch AI] 正在创建 Python 虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo 未能创建虚拟环境，请确认已安装 Python 3.11+ 并加入 PATH。
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败。
  pause
  exit /b 1
)

echo.
echo [NetResearch AI] 后端启动于 http://127.0.0.1:8000
echo 接口文档：http://127.0.0.1:8000/docs
echo.
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
