@echo off
chcp 65001 > nul
echo.
echo ═══════════════════════════════════════════════════════════
echo   BitgetFollow - WebSocket实时通信版
echo ═══════════════════════════════════════════════════════════
echo.

echo [1/3] 检查依赖...
pip show flask-socketio >nul 2>&1
if %errorlevel% neq 0 (
    echo ⚠ 未找到 flask-socketio，正在安装...
    pip install flask-socketio==5.3.6 python-socketio==5.11.0
    echo ✅ 依赖安装完成
) else (
    echo ✅ 依赖已安装
)

echo.
echo [2/3] 启动服务...
python web.py

echo.
echo [3/3] 服务已停止
pause
