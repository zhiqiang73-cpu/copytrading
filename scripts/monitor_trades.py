#!/usr/bin/env python3
"""
交易监控脚本 - 检查新交易并发送通知
"""
import sqlite3
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "tracker.db"
STATE_FILE = Path(__file__).parent.parent / "data" / "last_trade_state.json"

def load_last_state():
    """加载上次检查的状态"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_order_ts": 0, "last_check": 0}

def save_state(state):
    """保存状态"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_new_trades():
    """获取新交易"""
    if not DB_PATH.exists():
        return []
    
    state = load_last_state()
    last_ts = state.get("last_order_ts", 0)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    # 查询新订单（只查成功的）
    cur.execute('''
        SELECT timestamp, trader_uid, symbol, action, direction, exec_qty, exec_price, status, platform, notes
        FROM copy_orders 
        WHERE timestamp > ? AND status = 'filled'
        ORDER BY timestamp DESC
        LIMIT 20
    ''', (last_ts,))
    
    rows = cur.fetchall()
    conn.close()
    
    trades = []
    for r in rows:
        trades.append({
            "timestamp": r["timestamp"],
            "trader_uid": r["trader_uid"],
            "symbol": r["symbol"],
            "action": r["action"],
            "direction": r["direction"],
            "exec_qty": r["exec_qty"],
            "exec_price": r["exec_price"],
            "platform": r["platform"],
            "notes": r["notes"] or ""
        })
    
    # 更新状态
    if trades:
        state["last_order_ts"] = max(t["timestamp"] for t in trades)
        state["last_check"] = int(datetime.now().timestamp() * 1000)
        save_state(state)
    
    return trades

def format_trade_msg(trade):
    """格式化交易消息"""
    ts = datetime.fromtimestamp(trade["timestamp"]/1000).strftime('%H:%M:%S')
    action_emoji = "🟢" if trade["action"] == "open" else "🔴"
    direction_emoji = "📈" if trade["direction"] == "long" else "📉"
    platform = trade["platform"].replace("live_", "").upper()
    
    msg = f"""
{action_emoji} {trade["action"].upper()} | {direction_emoji} {trade["direction"].upper()}
⏰ {ts} | 💱 {trade["symbol"]}
👤 交易员：{trade["trader_uid"][:12]}
💰 数量：{trade["exec_qty"]} @ {trade["exec_price"]}
🏦 平台：{platform}
"""
    if trade["notes"]:
        msg += f"📝 备注：{trade["notes"][:100]}\n"
    
    return msg.strip()

def main():
    trades = get_new_trades()
    
    if not trades:
        print("No new trades")
        sys.exit(0)
    
    # 输出到 stdout，由调用方处理通知
    result = {
        "new_trades_count": len(trades),
        "trades": trades,
        "messages": [format_trade_msg(t) for t in trades]
    }
    
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
