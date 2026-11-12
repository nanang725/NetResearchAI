@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在分别启动后端与前端窗口...
start "NetResearch AI Backend" cmd /k "%~dp0start-backend.bat"
start "NetResearch AI Frontend" cmd /k "%~dp0start-frontend.bat"
echo 已打开两个终端窗口。关闭对应窗口即可停止服务。
