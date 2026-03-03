#!/bin/bash
# ═══════════════════════════════════════════════════════
#  BitgetFollow — 统一启动入口（唯一）
#  双击此文件即可启动 Web 仪表盘
# ═══════════════════════════════════════════════════════

cd "$(dirname "$0")"

LAUNCH_LOCK_DIR="/tmp/bitgetfollow_launch_once.lock"
# 防止双击并发：脚本最早阶段就加锁，避免重复执行依赖安装与打开浏览器
if ! mkdir "$LAUNCH_LOCK_DIR" 2>/dev/null; then
    echo "  检测到正在启动中，已忽略本次重复双击"
    exit 0
fi
trap 'rmdir "$LAUNCH_LOCK_DIR" 2>/dev/null || true' EXIT

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
OPEN_STAMP_FILE="/tmp/bitgetfollow_last_open_ts"
OPEN_COOLDOWN_SEC=8
AUTO_OPEN_BROWSER="${AUTO_OPEN_BROWSER:-1}"
AUTO_CLOSE_TERMINAL_WINDOW="${AUTO_CLOSE_TERMINAL_WINDOW:-0}"

STARTED_NEW=0

# 如果服务已运行则不重复启动
if curl -s -o /dev/null "$URL"; then
    echo "  服务已在运行: $URL"
else
    nohup python3 web.py >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "  已后台启动，日志: $LOG_FILE"
    STARTED_NEW=1
fi

# 浏览器打开采用冷却控制：连续触发只放行一次
if [ "$STARTED_NEW" -eq 1 ]; then
    for _ in {1..20}; do
        if curl -s -o /dev/null "$URL"; then
            break
        fi
        sleep 0.2
    done
fi

if [ "$STARTED_NEW" -eq 1 ] && [ "$AUTO_OPEN_BROWSER" = "1" ]; then
    NOW_TS=$(date +%s)
    LAST_TS=0
    if [ -f "$OPEN_STAMP_FILE" ]; then
        LAST_TS=$(cat "$OPEN_STAMP_FILE" 2>/dev/null || echo 0)
    fi
    if [ $((NOW_TS - LAST_TS)) -ge "$OPEN_COOLDOWN_SEC" ]; then
        echo "$NOW_TS" > "$OPEN_STAMP_FILE"
        # 优先复用已运行的 Chrome；若未运行则改用 Safari，避免 Chrome 资料选择器弹窗
        if pgrep -x "Google Chrome" >/dev/null 2>&1; then
            osascript -e "tell application \"Google Chrome\" to open location \"$URL\"" >/dev/null 2>&1 || open -a "Safari" "$URL"
        elif pgrep -x "Safari" >/dev/null 2>&1; then
            osascript -e "tell application \"Safari\" to open location \"$URL\"" >/dev/null 2>&1 || open -a "Safari" "$URL"
        else
            open -a "Safari" "$URL" || open "$URL"
        fi
    else
        echo "  浏览器打开已在冷却中，跳过重复打开"
    fi
else
    if [ "$AUTO_OPEN_BROWSER" = "0" ]; then
        echo "  已禁用自动打开浏览器，请手动访问: $URL"
    else
        echo "  服务已在运行，不重复弹出浏览器"
    fi
fi

# 自动关闭当前终端窗口（默认关闭此行为，避免手动执行时弹出“终止进程”对话框）
# 需要时可手动设置: AUTO_CLOSE_TERMINAL_WINDOW=1 ./启动系统.command
if [ "$AUTO_CLOSE_TERMINAL_WINDOW" = "1" ] && [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
    osascript -e 'tell application "Terminal" to close (first window whose frontmost is true)' >/dev/null 2>&1
fi

exit 0
