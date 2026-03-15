#!/bin/bash
# 交易监控 - 每分钟检查新交易并发送通知

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor_trades.py"
RESULT=$(python3 "$MONITOR_SCRIPT" 2>/dev/null)

if [ $? -ne 0 ]; then
    exit 0
fi

# 解析结果
COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('new_trades_count',0))" 2>/dev/null)

if [ "$COUNT" -gt 0 ] 2>/dev/null; then
    # 提取第一条交易消息（最新的）
    MSG=$(echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
trades = d.get('trades', [])
if trades:
    t = trades[0]
    from datetime import datetime
    ts = datetime.fromtimestamp(t['timestamp']/1000).strftime('%H:%M:%S')
    action = '🟢 开仓' if t['action'] == 'open' else '🔴 平仓'
    direction = '📈 多' if t['direction'] == 'long' else '📉 空'
    print(f'{action} {direction} | {t[\"symbol\"]}')
    print(f'⏰ {ts} | 👤 {t[\"trader_uid\"][:12]}')
    print(f'💰 {t[\"exec_qty\"]} @ {t[\"exec_price\"]}')
    print(f'🏦 {t[\"platform\"].replace(\"live_\", \"\").upper()}')
" 2>/dev/null)
    
    if [ -n "$MSG" ]; then
        # 使用 openclaw 发送消息到 webchat
        openclaw agent --message "🔔 跟单交易通知

$MSG" 2>/dev/null || true
    fi
fi
