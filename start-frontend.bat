@echo off
chcp 65001 >nul
cd /d "%~dp0frontend"

where npm >nul 2>nul
if errorlevel 1 (
  echo 未检测到 npm，请先安装 Node.js 20+。
  pause
  exit /b 1
)

if not exist "node_modules" (
  echo [NetResearch AI] 正在安装前端依赖...
  npm install
  if errorlevel 1 (
    echo 前端依赖安装失败。
    pause
    exit /b 1
  )
)

echo.
echo [NetResearch AI] 前端启动于 http://127.0.0.1:5173
echo 请确保后端已在 http://127.0.0.1:8000 运行。
echo.
npm run dev
