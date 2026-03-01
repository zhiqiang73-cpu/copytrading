"""
快照对比引擎：将 currentList 快照与上次快照对比，
推导出开仓 / 平仓事件，并写入数据库。
"""
from __future__ import annotations
import logging
import time

import api_client
import database as db

logger = logging.getLogger(__name__)


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_position(raw: dict) -> dict:
    """
    将 currentList API 返回的 position 归一化。
    文档字段: trackingNo, holdSide, leverage, symbol, openPrice, openTime,
              openAmount, takeProfitPrice, stopLossPrice, marginAmount
    """
    return {
        "tracking_no": raw.get("trackingNo", ""),
        "symbol":      raw.get("symbol", ""),
        "hold_side":   (raw.get("holdSide") or "").lower(),
        "leverage":    _safe_int(raw.get("leverage", 1)),
        "open_price":  _safe_float(raw.get("openPrice")),
        "open_time":   _safe_int(raw.get("openTime")),
        "open_amount": _safe_float(raw.get("openAmount")),
        "tp_price":    _safe_float(raw.get("takeProfitPrice")),
        "sl_price":    _safe_float(raw.get("stopLossPrice")),
    }


def _parse_history_order(raw: dict, trader_uid: str) -> dict:
    """
    将 historyList API 返回的订单归一化为 trades 表所需字段。
    文档字段: trackingNo, holdMode, holdSide, leverage, symbol,
              openPrice, openTime, closePrice, closeTime, closeAmount, marginAmount
    注意：API 不返回 profitRate，需自行计算。
    """
    open_time  = _safe_int(raw.get("openTime"))
    close_time = _safe_int(raw.get("closeTime"))
    hold_dur   = (close_time - open_time) // 1000 if close_time > open_time else 0

    direction = (raw.get("holdSide") or "").lower()
    if direction not in ("long", "short"):
        direction = "unknown"

    open_price  = _safe_float(raw.get("openPrice"))
    close_price = _safe_float(raw.get("closePrice"))

    # 手动计算 PnL 百分比（API 不返回此字段）
    if open_price > 0:
        if direction == "long":
            pnl_pct = (close_price - open_price) / open_price
        elif direction == "short":
            pnl_pct = (open_price - close_price) / open_price
        else:
            pnl_pct = 0.0
    else:
        pnl_pct = 0.0

    is_win = 1 if pnl_pct > 0 else 0

    return {
        "trade_id":      raw.get("trackingNo", ""),
        "trader_uid":    trader_uid,
        "symbol":        raw.get("symbol", ""),
        "direction":     direction,
        "leverage":      _safe_int(raw.get("leverage", 1)),
        "open_price":    open_price,
        "open_time":     open_time,
        "close_price":   close_price,
        "close_time":    close_time,
        "hold_duration": hold_dur,
        "pnl_pct":       pnl_pct,
        "margin_amount": _safe_float(raw.get("marginAmount")),
        "is_win":        is_win,
    }


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def process_snapshot(trader_uid: str, current_positions: list[dict]):
    """
    对比最新持仓快照与数据库中缓存的上次快照：
    - 新出现的 tracking_no → 记录开仓日志（快照写入 DB）
    - 消失的 tracking_no  → 查历史订单补全平仓数据 → 写入 trades 表
    """
    prev = db.get_snapshots(trader_uid)   # {tracking_no: snap_dict}
    curr = {
        p["tracking_no"]: p
        for raw in current_positions
        for p in [_parse_position(raw)]
        if p["tracking_no"]
    }

    opened  = set(curr) - set(prev)
    closed  = set(prev) - set(curr)
    ongoing = set(curr) & set(prev)

    # 新开仓
    for tn in opened:
        pos = curr[tn]
        snap = {**pos, "trader_uid": trader_uid, "timestamp": int(time.time())}
        db.upsert_snapshot(snap)
        logger.info(
            "[开仓] %s | %s %s x%d @ %.4f",
            trader_uid[:8], pos["symbol"], pos["hold_side"].upper(),
            pos["leverage"], pos["open_price"],
        )

    # 持仓更新（TP/SL 变化）
    for tn in ongoing:
        pos = curr[tn]
        snap = {**pos, "trader_uid": trader_uid, "timestamp": int(time.time())}
        db.upsert_snapshot(snap)

    # 平仓
    for tn in closed:
        _handle_close(trader_uid, tn, prev[tn])


def _handle_close(trader_uid: str, tracking_no: str, prev_snap: dict):
    """
    某个 tracking_no 从持仓列表消失，视为已平仓。
    尝试从历史订单 API 查询完整数据并写入 trades 表。
    """
    logger.info("[平仓] %s | tracking_no=%s，查询历史订单…", trader_uid[:8], tracking_no)

    try:
        history = api_client.get_history_orders(trader_uid)
    except Exception as exc:
        logger.error("无法获取历史订单: %s", exc)
        db.delete_snapshot(trader_uid, tracking_no)
        return

    matched = [
        h for h in history
        if (h.get("trackingNo") or h.get("orderId")) == tracking_no
    ]

    if matched:
        trade = _parse_history_order(matched[0], trader_uid)
        db.insert_trade(trade)
        pnl = trade["pnl_pct"]
        symbol = trade["symbol"]
        logger.info(
            "[平仓完成] %s | %s %s  PnL=%.2f%%  持仓=%.0fs",
            trader_uid[:8], symbol, trade["direction"].upper(),
            pnl * 100, trade["hold_duration"],
        )
    else:
        logger.warning(
            "[平仓] 未在历史订单中找到 tracking_no=%s，仅删除快照", tracking_no
        )

    db.delete_snapshot(trader_uid, tracking_no)


def import_history(trader_uid: str, raw_orders: list[dict]):
    """将历史订单批量导入 trades 表（初始化时调用）。"""
    trades = [_parse_history_order(o, trader_uid) for o in raw_orders if o.get("trackingNo") or o.get("orderId")]
    valid  = [t for t in trades if t["trade_id"] and t["open_time"] and t["close_time"]]
    db.insert_trades_bulk(valid)
    logger.info("历史订单导入完成：%d 条（有效 %d 条）", len(raw_orders), len(valid))
