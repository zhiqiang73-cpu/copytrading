"""
scraper.py — 通过 Bitget 公开网页 API 获取任意交易员的公开数据（无需 API Key）
支持通过 URL 或 UID 直接添加交易员，不依赖跟单关系。
"""
from __future__ import annotations

import re
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_WEB_BASE = "https://www.bitget.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.bitget.com",
    "X-Requested-With": "XMLHttpRequest",
    "locale": "zh-CN",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}


def extract_uid_from_url(url: str) -> Optional[str]:
    """从 Bitget 交易员主页 URL 提取 UID，如 b1bc4c7086b23b55ad94"""
    # 匹配 /copy-trading/trader/{uid}/futures 或 /zh-CN/copy-trading/trader/{uid}
    m = re.search(r"/copy-trading/trader/([a-f0-9A-F]{20,})", url)
    if m:
        return m.group(1)
    return None


def _post(path: str, body: dict, trader_uid: str = "") -> Optional[dict]:
    """向 Bitget 网页公开 API 发 POST 请求"""
    url = _WEB_BASE + path
    headers = dict(_HEADERS)
    if trader_uid:
        headers["Referer"] = (
            f"{_WEB_BASE}/zh-CN/copy-trading/trader/{trader_uid}/futures"
        )
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if not resp.text.strip():
            logger.warning("Web API %s returned empty response", path)
            return None
        try:
            data = resp.json()
        except Exception:
            logger.error("Web API %s json decode failed. First 100 chars: %s", path, resp.text[:100])
            return None
        if data.get("code") == "00000":
            return data.get("data")
        logger.warning("Web API %s error: %s", path, data.get("msg", ""))
    except Exception as e:
        logger.error("Web API %s failed: %s", path, e)
    return None


def fetch_trader_detail(uid: str) -> Optional[dict]:
    """
    拉取单个交易员的公开详情，返回标准化字段。
    不需要 API Key，不需要已跟单。
    """
    data = _post(
        "/v1/trigger/trace/public/traderDetailPageV2",
        {"languageType": 1, "traderUid": uid},
        uid,
    )
    if not data:
        return None

    # 解析 itemVoList（核心指标）
    metrics: dict[str, float] = {}
    for item in data.get("itemVoList") or []:
        code = item.get("showColumnCode", "")
        val_str = str(item.get("comparedValue", "0") or "0")
        try:
            metrics[code] = float(val_str)
        except ValueError:
            metrics[code] = 0.0

    roi = metrics.get("profit_rate", 0.0)
    win_rate = metrics.get("total_winning_rate", 0.0)
    max_dd = metrics.get("max_retracement", 0.0)
    total_profit = metrics.get("income", 0.0)
    total_copiers = int(metrics.get("total_followers", 0))

    # 7/30 天跟单收益
    follow_profits = data.get("followProfits") or {}
    profit_7d = float(follow_profits.get("day7") or 0)
    profit_30d = float(follow_profits.get("day30") or 0)

    return {
        "uid": uid,
        "name": data.get("displayName", uid),
        "avatar": data.get("headPic", ""),
        "roi": roi,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
        "total_profit": total_profit,
        "total_copiers": total_copiers,
        "follower_count": data.get("followerCount", 0),
        "follow_count": data.get("followCount", 0),
        "aum": float(data.get("aum") or 0),
        "profit_share_ratio": float(data.get("distributeRatio") or 0),
        "profit_7d": profit_7d,
        "profit_30d": profit_30d,
        "can_trace": data.get("canTrace", False),
        "fetched_at": int(time.time() * 1000),
        "_raw": data,
    }


def fetch_profit_curve(uid: str, cycle_days: int = 30) -> list[dict]:
    """
    拉取交易员的收益曲线数据（30/90/180天）。
    返回 [{"ts": 毫秒时间戳, "amount": 浮点收益}, ...]
    """
    data = _post(
        "/v1/trigger/trace/public/cycleData",
        {"languageType": 1, "triggerUserId": uid, "cycleTime": cycle_days},
        uid,
    )
    if not data:
        return []

    kline = data.get("followNetProfitKlineDTO") or {}
    rows = kline.get("rows") or []
    result = []
    for row in rows:
        try:
            result.append(
                {
                    "ts": int(row["dataTime"]),
                    "amount": float(row.get("amount") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            pass
    return result


def fetch_open_symbols(uid: str) -> list[str]:
    """返回交易员当前有持仓的合约品种列表"""
    data = _post(
        "/v1/trigger/trace/public/getOpenUMCBLSymbol",
        {"languageType": 1, "traderUid": uid},
        uid,
    )
    if not isinstance(data, list):
        return []
    return [item.get("symbolName", "") for item in data if item.get("symbolName")]


def fetch_history_orders(uid: str, page: int = 1, page_size: int = 20) -> dict:
    """
    拉取交易员历史已平仓订单（分页）。
    返回 {"rows": [...], "next_page": bool}
    每条记录包含：trade_id, symbol, direction, leverage, open_price, close_price,
                  open_time, close_time, hold_duration, pnl_pct, net_profit, margin_amount, is_win
    """
    data = _post(
        "/v1/trigger/trace/order/historyList",
        {"languageType": 1, "traderUid": uid, "pageNo": page, "pageSize": page_size},
        uid,
    )
    if not data:
        return {"rows": [], "next_page": False}

    rows_raw = data.get("rows") or []
    rows = []
    for r in rows_raw:
        order_no = r.get("orderNo", "")
        if not order_no:
            continue

        # 方向：position=0 → 空(short)，其余 → 多(long)
        pos_val = r.get("position", 1)
        direction = "short" if pos_val == 0 else "long"

        # 保证金模式：marginMode=1 逐仓，2 全仓
        mm = r.get("marginMode", 2)
        margin_mode = "isolated" if mm == 1 else "cross"

        open_price = float(r.get("openAvgPrice") or 0)
        close_price = float(r.get("closeAvgPrice") or 0)
        open_time = int(r.get("openTime") or 0)
        close_time = int(r.get("closeTime") or 0)
        hold_duration = max(0, (close_time - open_time) // 1000)  # 秒
        net_profit = float(r.get("netProfit") or 0)
        is_win = 1 if net_profit > 0 else 0

        rows.append({
            "trade_id": f"{uid}_{order_no}",
            "trader_uid": uid,
            "symbol": r.get("productCode") or r.get("symbolDisplayName", ""),
            "direction": direction,
            "leverage": int(r.get("openLevel") or 1),
            "margin_mode": margin_mode,
            "open_price": open_price,
            "open_time": open_time,
            "close_price": close_price,
            "close_time": close_time,
            "hold_duration": hold_duration,
            "position_size": float(r.get("openDealCount") or 0),
            "pnl_pct": float(r.get("returnRate") or 0),
            "net_profit": net_profit,
            "gross_profit": float(r.get("achievedProfits") or 0),
            "open_fee": float(r.get("openFee") or 0),
            "close_fee": float(r.get("closeFee") or 0),
            "funding_fee": float(r.get("capitalFee") or 0),
            "margin_amount": float(r.get("openMarginCount") or 0),
            "follow_count": int(r.get("orderFollowCount") or 0),
            "is_win": is_win,
            "_raw": r,
        })

    return {"rows": rows, "next_page": data.get("nextFlag", False)}


def fetch_all_history_orders(uid: str, max_pages: int = 10) -> list[dict]:
    """
    拉取所有历史订单（自动翻页，最多 max_pages 页）。
    """
    all_rows = []
    for page in range(1, max_pages + 1):
        result = fetch_history_orders(uid, page=page, page_size=20)
        all_rows.extend(result["rows"])
        if not result["next_page"]:
            break
        import time as _t
        _t.sleep(0.3)
    return all_rows


def fetch_current_positions(uid: str) -> list[dict]:
    """
    拉取交易员当前持仓（未平仓订单）。
    返回列表，每条包含：symbol, direction, leverage, open_price, open_time, margin_amount, unrealized_pnl
    """
    data = _post(
        "/v1/trigger/trace/order/currentList",
        {"languageType": 1, "traderUid": uid, "pageNo": 1, "pageSize": 9999},
        uid,
    )
    if data is None:
        return None  # 返回 None 表示获取失败（如持仓保护）

    items = data.get("items") or []
    result = []
    for r in items:
        pos_val = r.get("position", 1)
        direction = "short" if pos_val == 0 else "long"
        mm = r.get("marginMode", 2)
        margin_mode = "isolated" if mm == 1 else "cross"
        result.append({
            "order_no":       r.get("orderNo") or r.get("openOrderNo", ""),
            "symbol":         r.get("productCode") or r.get("productName", ""),
            "direction":      direction,
            "leverage":       int(r.get("openLevel") or 1),
            "margin_mode":    margin_mode,
            "open_price":     float(r.get("openAvgPrice") or 0),
            "open_time":      int(r.get("openTime") or 0),
            "position_size":  float(r.get("openDealCount") or 0),
            "margin_amount":  float(r.get("openMarginCount") or 0),
            "unrealized_pnl": float(r.get("achievedProfits") or 0),
            "return_rate":    float(r.get("returnRate") or 0),
            "follow_count":   int(r.get("orderFollowCount") or 0),
            "_raw": r,
        })
    return result


def infer_current_positions_from_history(uid: str) -> list[dict]:
    """
    从本地持仓快照中推断当前持仓。
    逻辑：获取该交易员最新的持仓快照（snapshots 表）

    返回格式与 currentList 一致：
    [
        {
            "tracking_no": "xxx",
            "symbol": "BTCUSDT_UMCBL",
            "direction": "long/short",
            "leverage": 10,
            "open_price": 50000.0,
            "open_time": 1234567890,
            "position_size": 0.1,
            "margin_amount": 10.0,
            "margin_mode": "cross/isolated",
            "unrealized_pnl": 0.0,
            "return_rate": 0.0,
            "follow_count": 0,
        },
        ...
    ]
    """
    import database as db

    # 从 snapshots 表获取该交易员最新的持仓快照
    snapshots = db.get_latest_snapshots(uid)
    if not snapshots:
        return []

    positions = []
    for snap in snapshots:
        # 过滤掉已平仓的（symbol 为空或 hold_side 为空的）
        if not snap.get("symbol") or not snap.get("hold_side"):
            continue

        direction = "long" if snap.get("hold_side", "").lower() == "long" else "short"

        positions.append({
            "order_no": snap.get("tracking_no", ""),
            "tracking_no": snap.get("tracking_no", ""),
            "symbol": snap.get("symbol", ""),
            "direction": direction,
            "leverage": int(snap.get("leverage") or 1),
            "margin_mode": snap.get("margin_mode", "cross"),
            "open_price": float(snap.get("open_price") or 0),
            "open_time": int(snap.get("open_time") or 0),
            "position_size": float(snap.get("position_size") or 0),
            "margin_amount": float(snap.get("open_amount") or 0),
            "unrealized_pnl": 0.0,  # 历史数据无法计算未实现盈亏
            "return_rate": 0.0,
            "follow_count": int(snap.get("follow_count") or 0),
            "_raw": snap,
        })

    return positions


def search_traders_by_name(name: str) -> list[dict]:
    """暂不支持按名称搜索（Bitget V2 API 限制）。"""
    return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    uid = "b1bc4c7086b23b55ad94"
    print("=== 交易员详情 ===")
    detail = fetch_trader_detail(uid)
    if detail:
        d = {k: v for k, v in detail.items() if k != "_raw"}
        import json
        print(json.dumps(d, ensure_ascii=False, indent=2))

    print("\n=== 收益曲线（30天）===")
    curve = fetch_profit_curve(uid, 30)
    print(f"共 {len(curve)} 个数据点，最新: {curve[-1] if curve else 'None'}")

    print("\n=== 当前持仓品种 ===")
    symbols = fetch_open_symbols(uid)
    print(symbols)
