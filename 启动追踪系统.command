#!/bin/bash
# ═══════════════════════════════════════════════════════
#  BitgetFollow — 聪明钱追踪系统
#  双击此文件即可启动 Web 仪表盘
# ═══════════════════════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   BitgetFollow 聪明钱追踪系统        ║"
echo "  ║   正在启动 Web 仪表盘…               ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "  ❌ 未找到 python3，请先安装 Python"
    echo "  推荐：brew install python3"
    echo ""
    read -p "  按回车退出…"
    exit 1
fi

# 安装依赖（静默模式，已安装则跳过）
echo "  检查依赖…"
pip3 install -r requirements.txt --user -q 2>/dev/null

echo "  启动中…浏览器将自动打开"
echo "  地址：http://127.0.0.1:8080"
echo ""

URL="http://127.0.0.1:8080"
LOG_FILE="$(pwd)/bitgetfollow.log"
PID_FILE="$(pwd)/bitgetfollow.pid"

# 如果服务已运行则不重复启动
if curl -s -o /dev/null "$URL"; then
    echo "  服务已在运行: $URL"
else
    nohup python3 web.py >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "  已后台启动，日志: $LOG_FILE"
fi

sleep 1
open "$URL"

# 自动关闭当前终端窗口（仅用于双击启动）
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
    osascript -e 'tell application "Terminal" to close (first window whose frontmost is true)' >/dev/null 2>&1
fi

exit 0
