"""
copy_engine.py — 自动跟单引擎 (仅币安信号源 → Bitget 下单)

通过监控币安交易员的操作记录，自动在 Bitget 执行对应的开仓/平仓操作。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable

import requests

import config
import database as db
import order_executor
import binance_scraper

logger = logging.getLogger(__name__)

_ENGINES: dict[str, "CopyEngine"] = {}
_ORDER_CREATED_CALLBACK: Callable[[dict[str, Any]], None] | None = None


def set_order_created_callback(callback: Callable[[dict[str, Any]], None] | None) -> None:
    """Register callback for newly inserted copy orders."""
    global _ORDER_CREATED_CALLBACK
    _ORDER_CREATED_CALLBACK = callback


def _notify_order_created(order_payload: dict[str, Any]) -> None:
    callback = _ORDER_CREATED_CALLBACK
    if callback is None:
        return
    try:
        callback(dict(order_payload or {}))
    except Exception as exc:
        logger.debug("order created callback failed: %s", exc)


def _insert_copy_order_and_notify(order_payload: dict[str, Any]) -> None:
    db.insert_copy_order(order_payload)
    _notify_order_created(order_payload)


def _normalize_profile(profile: str | None) -> str:
    profile_key = str(profile or "sim").strip().lower()
    if profile_key in {"", "default", "paper", "sim", "simulation"}:
        return "sim"
    if profile_key in {"live", "real", "production", "prod"}:
        return "live"
    return profile_key


def _profile_storage_platform(profile: str, platform: str) -> str:
    profile_key = _normalize_profile(profile)
    platform_key = str(platform or "").strip().lower()
    if profile_key == "sim":
        return platform_key
    return f"{profile_key}_{platform_key}"


def _profile_exec_platform(platform: str) -> str:
    platform_key = str(platform or "").strip().lower()
    if platform_key.endswith("bitget"):
        return "bitget"
    if platform_key.endswith("binance"):
        return "binance"
    return platform_key


def _profile_bitget_simulated(profile: str) -> bool:
    return False if _normalize_profile(profile) == "live" else bool(config.SIMULATED)


def _profile_binance_base_url(profile: str) -> str:
    if _normalize_profile(profile) == "live":
        return "https://fapi.binance.com"
    return (config.BINANCE_BASE_URL or "https://fapi.binance.com").strip().rstrip("/")

def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_ratio_value(value: Any, default: float = 0.0) -> float:
    ratio = _safe_float(value, default)
    if ratio > 1:
        ratio = ratio / 100.0
    return min(max(ratio, 0.0), 1.0)


def _resolve_trader_follow_ratio(trader_data: dict | None, global_ratio: float) -> float:
    if isinstance(trader_data, dict) and trader_data.get("follow_ratio") not in (None, ""):
        return _normalize_ratio_value(trader_data.get("follow_ratio"), global_ratio)
    return _normalize_ratio_value(global_ratio, global_ratio)


def build_platform_allocation_details(
    settings: dict | None,
    platform: str,
    trader_map: dict[str, dict] | None,
    available_usdt: float = 0.0,
) -> dict[str, Any]:
    settings = settings or {}
    trader_map = trader_map or {}
    enabled = {
        str(pid): data for pid, data in trader_map.items()
        if isinstance(data, dict) and data.get("copy_enabled") is True
    }

    if str(platform).strip().lower() == "binance":
        total_capital = _safe_float(settings.get("binance_total_capital"), 0.0)
        global_ratio = _normalize_ratio_value(settings.get("binance_follow_ratio_pct"), 0.003)
        max_margin_pct = _normalize_ratio_value(settings.get("binance_max_margin_pct"), 0.20)
    else:
        total_capital = _safe_float(settings.get("total_capital"), 0.0)
        global_ratio = _normalize_ratio_value(settings.get("follow_ratio_pct"), 0.003)
        max_margin_pct = _normalize_ratio_value(settings.get("max_margin_pct"), 0.20)

    enabled_count = max(len(enabled), 1)
    pool_per_trader = (total_capital / enabled_count) if total_capital > 0 else 0.0
    fallback_margin = pool_per_trader * max_margin_pct if pool_per_trader > 0 and max_margin_pct > 0 else 0.0
    available_cap = max(available_usdt, 0.0) * 0.95 if available_usdt > 0 else 0.0

    traders: dict[str, dict] = {}
    for pid, data in trader_map.items():
        row = dict(data) if isinstance(data, dict) else {}
        copy_enabled = bool(row.get("copy_enabled") is True)
        trader_ratio = _resolve_trader_follow_ratio(row, global_ratio) if copy_enabled else 0.0
        effective_margin_cap = min(
            [v for v in (fallback_margin, available_cap) if v > 0] or [fallback_margin or available_cap or 0.0]
        )
        traders[str(pid)] = {
            **row,
            "copy_enabled": copy_enabled,
            "effective_follow_ratio": trader_ratio,
            "allocation_pool": pool_per_trader if copy_enabled else 0.0,
            "fallback_margin_cap": fallback_margin if copy_enabled else 0.0,
            "available_margin_cap": available_cap if copy_enabled else 0.0,
            "effective_margin_cap": effective_margin_cap if copy_enabled else 0.0,
            "sizing_mode": "tier_ratio" if copy_enabled and row.get("follow_ratio") not in (None, "") else "global_ratio",
        }

    return {
        "platform": platform,
        "enabled_count": len(enabled),
        "total_capital": total_capital,
        "global_follow_ratio": global_ratio,
        "max_margin_pct": max_margin_pct,
        "pool_per_trader": pool_per_trader,
        "fallback_margin_cap": fallback_margin,
        "available_margin_cap": available_cap,
        "effective_margin_cap": min(
            [v for v in (fallback_margin, available_cap) if v > 0] or [fallback_margin or available_cap or 0.0]
        ),
        "traders": traders,
    }


def _trunc4(value: float) -> float:
    sign = 1.0 if value >= 0 else -1.0
    return sign * (int(abs(value) * 10000) / 10000.0)


def _estimate_margin_from_position(size: float, price: float, leverage: int) -> float:
    if size <= 0 or price <= 0 or leverage <= 0:
        return 0.0
    return (abs(size) * price) / max(leverage, 1)


def _is_symbol_not_exist_error(exc: Exception) -> bool:
    msg = str(exc)
    return ("code=40034" in msg) or ("参数" in msg and "不存在" in msg) or ("symbol does not exist" in msg.lower())


def _is_bitget_min_trade_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=45110" in msg)
        or ("code=45111" in msg)
        or ("最小下单数量" in msg)
        or ("最小下单价值" in msg)
        or ("minimum order quantity" in lower)
        or ("minimum order value" in lower)
    )


def _is_binance_min_notional_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return ("code=-4164" in msg) or ("notional must be no smaller" in lower)


def _is_binance_symbol_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return ("code=-1121" in msg) or ("invalid symbol" in lower)


def _is_bitget_balance_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=40762" in msg)
        or ("订单金额超出账户余额" in msg)
        or ("账户余额" in msg and "不足" in msg)
        or ("order amount exceeds account balance" in lower)
    )


def _is_binance_balance_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=-2019" in msg)
        or ("margin is insufficient" in lower)
        or ("insufficient balance" in lower)
    )


def _is_request_timeout_error(exc: Exception) -> bool:
    lower = str(exc).lower()
    return ("read timed out" in lower) or ("read timeout" in lower) or ("timed out" in lower)


def _is_binance_order_missing_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return ("code=-2013" in msg) or ("order does not exist" in lower)


def _is_bitget_position_missing_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=22002" in msg)
        or ("no position" in lower)
        or ("no position to close" in lower)
    )


def _is_binance_position_missing_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=-2022" in msg)
        or ("reduceonly order is rejected" in lower)
    )


def _is_post_only_rejected_error(exc: Exception) -> bool:
    msg = str(exc)
    lower = msg.lower()
    return (
        ("code=-5022" in msg)
        or ("post only order will be rejected" in lower)
        or ("post_only" in lower and "rejected" in lower)
    )


def _is_local_min_size_error(exc: Exception) -> bool:
    msg = str(exc)
    return ("换算后张数为0或负数" in msg) or ("开仓数量因精度截断为 0" in msg)


def _cap_limit_value(fallback_margin: float, available_usdt: float) -> float:
    caps = []
    if fallback_margin > 0:
        caps.append(fallback_margin)
    if available_usdt > 0:
        caps.append(available_usdt * 0.95)
    return min(caps) if caps else 0.0


def get_ticker_price(symbol: str, product_type: str = "USDT-FUTURES") -> float:
    api_symbol = symbol.replace("_UMCBL", "").replace("_UM", "").replace("_DMCBL", "").replace("_DM", "")
    resp = requests.get(
        config.BASE_URL + "/api/v2/mix/market/ticker",
        params={"symbol": api_symbol, "productType": product_type},
        timeout=10,
    )
    if not resp.ok:
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        error_msg = payload.get("msg") or payload.get("message") or resp.text[:200] or "ticker request failed"
        error_code = payload.get("code", resp.status_code)
        raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")
    payload = resp.json()
    if str(payload.get("code", "0")) != "00000":
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")
    data = payload.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("ticker response unusual")
    for key in ("last", "lastPr", "lastPrice", "close", "markPrice"):
        if key in data and data[key] is not None:
            return float(data[key])
    raise ValueError("ticker missing price")


def _price_ok(symbol: str, ref_price: float, tolerance: float) -> tuple[bool, float, float]:
    if ref_price <= 0:
        return False, 0.0, 1.0
    current = get_ticker_price(symbol)
    deviation = abs(current - ref_price) / ref_price
    return deviation <= tolerance, current, deviation


def _parse_list(raw: str) -> list[str]:
    if not raw: return []
    try:
        data = json.loads(raw)
        return [str(x) for x in data if str(x)]
    except json.JSONDecodeError:
        return []


def _extract_balance_usdt(data: Any) -> float:
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        for key in ("available", "availableEquity", "maxAvailable", "equity"):
            if key in data and data[key] is not None:
                return _safe_float(data[key], 0.0)
    return 0.0


def _extract_wallet_equity_usdt(data: Any) -> float:
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        for key in ("usdtEquity", "equity", "accountEquity", "totalEquity", "balance", "crossWalletBalance", "walletBalance"):
            if key in data and data[key] is not None:
                return _safe_float(data[key], 0.0)
    return 0.0


def _clean_symbol_str(symbol: str) -> str:
    """统一清洗 symbol 格式，移除旧式后缀"""
    for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
        symbol = symbol.replace(suffix, "")
    return symbol


# 币安 -> Bitget 合约 symbol 映射（部分币种命名不同）
_SOURCE_CONTRACT_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,}(USDT|USDC|BUSD|FDUSD|USD)$")


def _is_reasonable_contract_symbol(symbol: str) -> bool:
    symbol_key = _clean_symbol_str(symbol or "").upper()
    return bool(_SOURCE_CONTRACT_SYMBOL_RE.fullmatch(symbol_key))


_BN_TO_BG_SYMBOL = {
    "1000PEPEUSDT": "PEPEUSDT",
    "1000SHIBUSDT": "SHIBUSDT",
    "1000BONKUSDT": "BONKUSDT",
    "1000FLOKIUSDT": "FLOKIUSDT",
    "1000LUNCUSDT": "LUNCUSDT",
}


def _binance_symbol_to_bitget(symbol: str) -> str:
    """将币安 symbol 转为 Bitget 可用的 symbol"""
    s = _clean_symbol_str(symbol or "")
    return _BN_TO_BG_SYMBOL.get(s, s)


def _normalize_ratio_setting(value: Any, default: float) -> float:
    ratio = _safe_float(value, default)
    if ratio > 1:
        ratio = ratio / 100.0
    return min(max(ratio, 0.0), 1.0)


def _normalize_entry_order_mode(value: Any, default: str = config.DEFAULT_ENTRY_ORDER_MODE) -> str:
    mode = str(value or default or "maker_limit").strip().lower()
    return mode if mode in {"market", "maker_limit"} else default


def _normalize_bool_setting(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_nonnegative_int(value: Any, default: int, minimum: int = 0) -> int:
    return max(minimum, _safe_int(value, default))


def _pick_maker_limit_price(
    direction: str,
    bid_price: float,
    ask_price: float,
    last_price: float,
    tick_size: float,
    maker_levels: int,
) -> float:
    side = str(direction or "").strip().lower()
    bid = _safe_float(bid_price, 0.0)
    ask = _safe_float(ask_price, 0.0)
    last = _safe_float(last_price, 0.0)
    tick = _safe_float(tick_size, 0.0)
    levels = max(_safe_int(maker_levels, 0), 0)

    if bid > 0 and ask > 0 and ask > bid and tick > 0:
        bid_d = Decimal(str(bid))
        ask_d = Decimal(str(ask))
        tick_d = Decimal(str(tick))
        spread_steps = int(((ask_d - bid_d) / tick_d).to_integral_value(rounding=ROUND_DOWN))
        improve_steps = min(levels, max(spread_steps - 1, 0))
        if side == "short":
            return float(ask_d - (tick_d * improve_steps))
        return float(bid_d + (tick_d * improve_steps))

    if last <= 0:
        return 0.0
    fallback_pct = 0.0005
    if side == "short":
        return last * (1.0 + fallback_pct)
    return last * (1.0 - fallback_pct)


def _estimate_margin_from_fill(exec_qty: float, exec_price: float, leverage: int) -> float:
    qty = max(_safe_float(exec_qty, 0.0), 0.0)
    price = max(_safe_float(exec_price, 0.0), 0.0)
    lev = max(_safe_int(leverage, 1), 1)
    if qty <= 0 or price <= 0:
        return 0.0
    return (qty * price) / lev


def _parse_binance_order_snapshot(order: dict | None, fallback_price: float = 0.0) -> dict:
    payload = dict(order or {})
    avg_price = _safe_float(payload.get("avgPrice"), 0.0)
    if avg_price <= 0:
        avg_price = _safe_float(payload.get("price"), 0.0) or _safe_float(fallback_price, 0.0)
    return {
        "status": str(payload.get("status") or "").upper(),
        "filled_qty": _safe_float(payload.get("executedQty"), 0.0),
        "avg_price": avg_price,
        "order_id": str(payload.get("orderId") or ""),
        "client_oid": str(payload.get("clientOrderId") or payload.get("clientOid") or ""),
    }


def _parse_bitget_order_snapshot(order: dict | None, fallback_price: float = 0.0) -> dict:
    payload = dict(order or {})
    avg_price = _safe_float(payload.get("priceAvg"), 0.0)
    if avg_price <= 0:
        avg_price = _safe_float(payload.get("fillPriceAvg"), 0.0)
    if avg_price <= 0:
        avg_price = _safe_float(payload.get("price"), 0.0) or _safe_float(fallback_price, 0.0)
    filled_qty = _safe_float(payload.get("baseVolume"), 0.0)
    if filled_qty <= 0:
        filled_qty = _safe_float(payload.get("filledQty"), 0.0)
    if filled_qty <= 0:
        filled_qty = _safe_float(payload.get("size"), 0.0) if str(payload.get("state") or "").lower() == "filled" else 0.0
    return {
        "status": str(payload.get("state") or payload.get("status") or "").lower(),
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "order_id": str(payload.get("orderId") or ""),
        "client_oid": str(payload.get("clientOid") or ""),
    }


def _calc_partial_close_qty(cycle_open_qty: float, remaining_qty: float, target_close_pct: float) -> float:
    total_qty = max(_safe_float(cycle_open_qty, 0.0), 0.0)
    live_qty = max(_safe_float(remaining_qty, 0.0), 0.0)
    if total_qty <= 0 or live_qty <= 0:
        return 0.0
    target_close_qty = total_qty * _normalize_ratio_setting(target_close_pct, 0.0)
    already_closed_qty = max(total_qty - live_qty, 0.0)
    return max(0.0, min(live_qty, target_close_qty - already_closed_qty))


def _estimate_position_pnl_roi(entry_price: float, current_price: float, remaining_qty: float, remaining_margin: float, direction: str) -> dict[str, float]:
    qty = max(_safe_float(remaining_qty, 0.0), 0.0)
    entry = _safe_float(entry_price, 0.0)
    mark = _safe_float(current_price, 0.0)
    margin = max(_safe_float(remaining_margin, 0.0), 0.0)
    if qty <= 0 or entry <= 0 or mark <= 0:
        return {"pnl": 0.0, "roi": 0.0}

    if str(direction or "").lower() == "short":
        pnl = (entry - mark) * qty
    else:
        pnl = (mark - entry) * qty
    roi = pnl / margin if margin > 0 else 0.0
    return {"pnl": pnl, "roi": roi}


def _decide_take_profit_action(position: dict, state: dict, settings: dict) -> dict:
    roi = _safe_float(position.get("roi"), 0.0)
    peak_roi = max(_safe_float(state.get("peak_roi"), 0.0), roi)
    stage = _safe_int(state.get("stage"), 0)
    remaining_qty = max(_safe_float(position.get("remaining_qty"), 0.0), 0.0)
    cycle_open_qty = max(_safe_float(position.get("cycle_open_qty"), remaining_qty), remaining_qty)
    if remaining_qty <= 0 or cycle_open_qty <= 0:
        return {"peak_roi": peak_roi, "action": None}

    stop_loss_pct = _normalize_ratio_setting(settings.get("stop_loss_pct"), config.DEFAULT_STOP_LOSS_PCT)
    tp1_roi_pct = _normalize_ratio_setting(settings.get("tp1_roi_pct"), config.DEFAULT_TP1_ROI_PCT)
    tp1_close_pct = _normalize_ratio_setting(settings.get("tp1_close_pct"), config.DEFAULT_TP1_CLOSE_PCT)
    tp2_roi_pct = _normalize_ratio_setting(settings.get("tp2_roi_pct"), config.DEFAULT_TP2_ROI_PCT)
    tp2_close_pct = _normalize_ratio_setting(settings.get("tp2_close_pct"), config.DEFAULT_TP2_CLOSE_PCT)
    tp3_roi_pct = _normalize_ratio_setting(settings.get("tp3_roi_pct"), config.DEFAULT_TP3_ROI_PCT)
    breakeven_buffer_pct = _normalize_ratio_setting(settings.get("breakeven_buffer_pct"), config.DEFAULT_BREAKEVEN_BUFFER_PCT)
    trail_callback_pct = _normalize_ratio_setting(settings.get("trail_callback_pct"), config.DEFAULT_TRAIL_CALLBACK_PCT)
    locked_roi_pct = max(_safe_float(state.get("locked_roi_pct"), 0.0), 0.0)

    if stop_loss_pct > 0 and roi <= -stop_loss_pct:
        return {
            "peak_roi": peak_roi,
            "action": {
                "kind": "close_all",
                "label": "System Stop Loss",
                "note": f"roi={roi * 100:.2f}% <= -{stop_loss_pct * 100:.2f}%",
                "next_state": {
                    "stage": max(stage, 3),
                    "peak_roi": peak_roi,
                    "locked_roi_pct": locked_roi_pct,
                    "breakeven_armed": 1 if state.get("breakeven_armed") else 0,
                    "trail_active": 1 if state.get("trail_active") else 0,
                    "closed_by_system": 1,
                    "freeze_reentry": 1,
                    "last_system_action": "stop_loss",
                },
            },
        }

    if tp3_roi_pct > 0 and roi >= tp3_roi_pct:
        return {
            "peak_roi": peak_roi,
            "action": {
                "kind": "close_all",
                "label": "System TP3",
                "note": f"roi={roi * 100:.2f}% >= {tp3_roi_pct * 100:.2f}%",
                "next_state": {
                    "stage": 3,
                    "peak_roi": peak_roi,
                    "locked_roi_pct": max(locked_roi_pct, config.DEFAULT_TP2_LOCKED_ROI_PCT),
                    "breakeven_armed": 1,
                    "trail_active": 1,
                    "closed_by_system": 1,
                    "freeze_reentry": 1,
                    "last_system_action": "tp3",
                },
            },
        }

    if stage < 2 and tp2_roi_pct > 0 and roi >= tp2_roi_pct:
        qty = _calc_partial_close_qty(cycle_open_qty, remaining_qty, tp1_close_pct + tp2_close_pct)
        if qty > 0:
            next_locked_roi = max(locked_roi_pct, config.DEFAULT_TP2_LOCKED_ROI_PCT)
            return {
                "peak_roi": peak_roi,
                "action": {
                    "kind": "partial_close",
                    "qty": qty,
                    "label": "System TP2",
                    "note": f"roi={roi * 100:.2f}% >= {tp2_roi_pct * 100:.2f}%",
                    "next_state": {
                        "stage": 2,
                        "peak_roi": peak_roi,
                        "locked_roi_pct": next_locked_roi,
                        "breakeven_armed": 1,
                        "trail_active": 1,
                        "closed_by_system": 0,
                        "freeze_reentry": 1,
                        "last_system_action": "tp2",
                    },
                },
            }

    if stage < 1 and tp1_roi_pct > 0 and roi >= tp1_roi_pct:
        qty = _calc_partial_close_qty(cycle_open_qty, remaining_qty, tp1_close_pct)
        if qty > 0:
            next_locked_roi = max(locked_roi_pct, breakeven_buffer_pct)
            return {
                "peak_roi": peak_roi,
                "action": {
                    "kind": "partial_close",
                    "qty": qty,
                    "label": "System TP1",
                    "note": f"roi={roi * 100:.2f}% >= {tp1_roi_pct * 100:.2f}%",
                    "next_state": {
                        "stage": 1,
                        "peak_roi": peak_roi,
                        "locked_roi_pct": next_locked_roi,
                        "breakeven_armed": 1,
                        "trail_active": 0,
                        "closed_by_system": 0,
                        "freeze_reentry": 1,
                        "last_system_action": "tp1",
                    },
                },
            }

    effective_floor = locked_roi_pct
    if stage >= 2 or state.get("trail_active"):
        effective_floor = max(effective_floor, peak_roi - trail_callback_pct)
    if effective_floor > 0 and peak_roi > 0 and roi <= effective_floor:
        return {
            "peak_roi": peak_roi,
            "action": {
                "kind": "close_all",
                "label": "System Trail Exit",
                "note": f"roi={roi * 100:.2f}% <= floor={effective_floor * 100:.2f}% (peak={peak_roi * 100:.2f}%)",
                "next_state": {
                    "stage": max(stage, 3),
                    "peak_roi": peak_roi,
                    "locked_roi_pct": effective_floor,
                    "breakeven_armed": 1 if (state.get("breakeven_armed") or stage >= 1) else 0,
                    "trail_active": 1 if (state.get("trail_active") or stage >= 2) else 0,
                    "closed_by_system": 1,
                    "freeze_reentry": 1,
                    "last_system_action": "trail_exit",
                },
            },
        }

    return {"peak_roi": peak_roi, "action": None}


class CopyEngine:
    def __init__(self, profile: str = "sim") -> None:
        self._profile = _normalize_profile(profile)
        self._running = False
        self._fail_streak = 0
        self._pos_mode = "2"  # 1=单向, 2=双向
        # 币安监控
        self._bn_thread: threading.Thread | None = None
        self._bn_seen: dict[str, int] = {}  # portfolio_id -> 最新 order_time (ms)
        self._bn_seen_order_id: dict[str, str] = {}  # portfolio_id -> 最新 order_id
        self._bn_poll_meta: dict[str, dict[str, Any]] = {}
        self._bn_history_seeded: set[str] = set()
        self._last_bn_metadata_refresh = 0
        self._state_lock = threading.RLock()
        self._unsupported_symbols: set[str] = set()
        self._unsupported_binance_symbols: set[str] = set()
        self._bn_inflight: set[str] = set()
        self._bn_dup_logged: set[str] = set()
        self._risk_pause_logged: set[str] = set()
        self._reconcile_wait_ms = 3000
        self._reconcile_wait_polls = 1
        self._reconcile_pending: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def start(self) -> None:
        with self._state_lock:
            # 先检查当前状态 - 防止僵尸进程
            if self._running:
                # 如果标志是True，但线程已死，强制清理
                if self._bn_thread is None or not self._bn_thread.is_alive():
                    logger.warning("[%s] 检测到僵尸状态（标志True但线程已死），强制清理并重启", self._profile)
                    self._running = False
                    self._bn_thread = None
                else:
                    logger.warning("[%s] 引擎已在运行中，跳过重复启动", self._profile)
                    return
            
            if self._bn_thread and self._bn_thread.is_alive():
                logger.warning("[%s] 检测到旧币安线程仍在运行，跳过重复启动", self._profile)
                return

            # 防重启信号重放：将所有已知币安交易员的起始时间戳设为当前时刻前 2 小时 (允许补票)
            self._bn_seen = {pid: int((time.time() - 7200) * 1000) for pid in self._bn_seen}
            self._bn_seen_order_id = {pid: "" for pid in self._bn_seen}
            self._bn_dup_logged.clear()
            self._running = True
            # 启动币安监控线程
            self._bn_thread = threading.Thread(target=self._run_binance, daemon=True, name=f"BN-{self._profile}")
            self._bn_thread.start()
            logger.info("[%s] 跟单引擎启动 (币安信号源模式) [线程ID: %s]", self._profile, self._bn_thread.ident)

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            bt = self._bn_thread

        if bt and bt.is_alive():
            bt.join(timeout=3.5)

        with self._state_lock:
            if self._bn_thread and not self._bn_thread.is_alive():
                self._bn_thread = None
        logger.info("跟单引擎已停止")

    def is_running(self) -> bool:
        """检查引擎是否真正在运行（不仅检查标志，还检查线程是否存活）"""
        with self._state_lock:
            # 必须同时满足：标志为True AND 线程还活着
            if not self._running:
                return False
            # 检查币安监控线程是否还活着
            if self._bn_thread is None or not self._bn_thread.is_alive():
                # 线程已死，但标志还是True - 这是不一致状态，强制修复
                logger.warning("[%s] 检测到引擎标志为True但线程已停止，自动修正状态", self._profile)
                self._running = False
                return False
            return True

    def _runtime(self) -> dict[str, Any]:
        return {
            "bitget_simulated": _profile_bitget_simulated(self._profile),
            "binance_base_url": _profile_binance_base_url(self._profile),
        }

    def _update_bn_poll_meta(self, pid: str, **updates: Any) -> None:
        with self._state_lock:
            meta = dict(self._bn_poll_meta.get(pid) or {})
            meta.update(updates)
            self._bn_poll_meta[pid] = meta

    def get_binance_trader_diagnostics(self) -> dict[str, dict[str, Any]]:
        with self._state_lock:
            diagnostics: dict[str, dict[str, Any]] = {}
            for pid in set(self._bn_poll_meta) | set(self._bn_seen) | set(self._bn_seen_order_id):
                meta = dict(self._bn_poll_meta.get(pid) or {})
                meta.setdefault("cursor_order_time", int(self._bn_seen.get(pid) or 0))
                meta.setdefault("cursor_order_id", str(self._bn_seen_order_id.get(pid) or ""))
                diagnostics[pid] = meta
            return diagnostics

    def _persist_binance_traders_data(self, traders_data: dict[str, Any]) -> None:
        payload = json.dumps(traders_data, ensure_ascii=False)
        if self._profile == "live":
            db.update_shared_copy_settings(binance_traders=payload)
            return
        db.update_copy_settings_profile(self._profile, binance_traders=payload)

    def _set_binance_trader_sync_state(self, traders_data: dict[str, Any], pid: str, **updates: Any) -> None:
        if not isinstance(traders_data, dict):
            return
        row = dict(traders_data.get(pid) or {})
        if not row:
            return
        changed = False
        for key, value in updates.items():
            if row.get(key) != value:
                row[key] = value
                changed = True
        if not changed:
            return
        traders_data[pid] = row
        self._persist_binance_traders_data(traders_data)

    def _store_source_orders(self, pid: str, orders: list[dict], source_kind: str = "live") -> None:
        if not orders:
            return
        db.upsert_source_trader_events([
            {
                "trader_uid": pid,
                "source_order_id": str(order.get("order_id") or ""),
                "symbol": order.get("symbol", ""),
                "action": order.get("action", ""),
                "direction": order.get("direction", ""),
                "qty": order.get("qty", 0.0),
                "price": order.get("price", 0.0),
                "leverage": order.get("leverage", 1),
                "order_time": order.get("order_time", 0),
                "raw_payload": order.get("_raw") or order,
            }
            for order in orders
        ], source_kind=source_kind)
        try:
            db.rebuild_trader_position_cycles(pid)
            db.refresh_trader_history_analytics([pid])
            db.refresh_trader_research_scores([pid])
        except Exception as exc:
            logger.debug("[history analytics] trader=%s refresh skipped: %s", pid[:12], exc)

    def _sync_binance_open_positions(
        self,
        settings: dict,
        bn_traders_data: dict[str, Any],
        pid: str,
        trader_cfg: dict[str, Any],
        bg_creds: dict[str, str] | None,
        bn_creds: dict[str, str] | None,
        bg_platform: str,
        bn_platform: str,
        bg_fallback_margin: float,
        bg_tol: float,
        bg_follow_ratio: float,
        bg_dynamic_note: str,
        bg_available_usdt: float,
        bg_allow_open: bool,
        bg_guard_note: str,
        bn_fallback_margin: float,
        bn_tol: float,
        bn_follow_ratio: float,
        bn_dynamic_note: str,
        bn_available_usdt: float,
        bn_allow_open: bool,
        bn_guard_note: str,
    ) -> None:
        if not isinstance(trader_cfg, dict) or trader_cfg.get("sync_open_positions_pending") is not True:
            return

        now_ms = _now_ms()
        try:
            snapshot_orders = binance_scraper.fetch_latest_orders(pid, since_ms=0, limit=100) or []
        except Exception as exc:
            self._update_bn_poll_meta(
                pid,
                sync_open_status="snapshot_failed",
                sync_open_error=str(exc)[:300],
                sync_open_attempted_at_ms=now_ms,
            )
            logger.warning("[%s sync-open] trader %s snapshot fetch failed: %s", self._profile, pid[:12], exc)
            return

        self._store_source_orders(pid, snapshot_orders, source_kind="history")
        source_positions = [
            item for item in db.get_source_position_summaries(pid)
            if _safe_float(item.get("remaining_qty"), 0.0) > 1e-12
        ]

        attempted = 0
        for source_state in source_positions:
            symbol = _clean_symbol_str(str(source_state.get("symbol") or "")).upper()
            direction = str(source_state.get("direction") or "").strip().lower()
            qty = _safe_float(source_state.get("remaining_qty"), 0.0)
            price = (
                _safe_float(source_state.get("price"), 0.0)
                or _safe_float(source_state.get("avg_entry_price"), 0.0)
                or _safe_float(source_state.get("last_open_price"), 0.0)
            )
            leverage = max(1, _safe_int(source_state.get("leverage"), 1))
            if not symbol or direction not in {"long", "short"} or qty <= 0 or price <= 0:
                continue

            bg_needs_open = False
            if bg_creds:
                bg_symbol = _binance_symbol_to_bitget(symbol)
                bg_needs_open = self._find_active_copy_position(bg_platform, pid, bg_symbol, direction) is None

            bn_needs_open = False
            if bn_creds:
                bn_needs_open = self._find_active_copy_position(bn_platform, pid, symbol, direction) is None

            if not (bg_needs_open or bn_needs_open):
                continue

            marker = str(
                source_state.get("last_source_order_id")
                or source_state.get("last_open_time")
                or source_state.get("last_event_time")
                or f"{symbol}:{direction}"
            )
            sync_hash = hashlib.md5(
                f"sync|{pid}|{symbol}|{direction}|{marker}|{qty:.8f}|{price:.8f}".encode()
            ).hexdigest()[:20]
            synthetic_order = {
                "order_id": f"SYNC_{sync_hash}",
                "symbol": symbol,
                "action": f"open_{direction}",
                "direction": direction,
                "qty": qty,
                "price": price,
                "leverage": leverage,
                "order_time": _safe_int(source_state.get("last_event_time"), now_ms),
                "pnl": 0.0,
                "sync_open": True,
                "sync_bg_enabled": bg_needs_open,
                "sync_bn_enabled": bn_needs_open,
            }
            self._process_binance_order(
                settings,
                bg_creds,
                bn_creds,
                pid,
                synthetic_order,
                bg_fallback_margin=bg_fallback_margin,
                bg_tol=bg_tol,
                bg_follow_ratio=bg_follow_ratio,
                bg_dynamic_note=bg_dynamic_note,
                bg_available_usdt=bg_available_usdt,
                bg_allow_open=bg_allow_open,
                bg_guard_note=bg_guard_note,
                bn_fallback_margin=bn_fallback_margin,
                bn_tol=bn_tol,
                bn_follow_ratio=bn_follow_ratio,
                bn_dynamic_note=bn_dynamic_note,
                bn_available_usdt=bn_available_usdt,
                bn_allow_open=bn_allow_open,
                bn_guard_note=bn_guard_note,
            )
            attempted += 1

        if source_positions:
            status = "submitted" if attempted > 0 else "already_in_sync"
        else:
            status = "no_open_positions"

        self._set_binance_trader_sync_state(
            bn_traders_data,
            pid,
            sync_open_positions_pending=False,
            last_sync_open_status=status,
            last_sync_open_position_count=len(source_positions),
            last_sync_open_attempt_count=attempted,
            last_sync_open_at=now_ms,
        )
        self._update_bn_poll_meta(
            pid,
            sync_open_status=status,
            sync_open_error="",
            sync_open_attempted_at_ms=now_ms,
            sync_open_position_count=len(source_positions),
            sync_open_attempt_count=attempted,
        )

    def _seed_binance_trader_history(self, pid: str, now_ms: int) -> tuple[int, str, int]:
        history_orders = binance_scraper.fetch_latest_orders(pid, since_ms=0, limit=100) or []
        self._store_source_orders(pid, history_orders, source_kind="history")

        latest_cursor = (0, "")
        for order in history_orders:
            cursor = (
                int(order.get("order_time") or 0),
                str(order.get("order_id") or ""),
            )
            if cursor > latest_cursor:
                latest_cursor = cursor

        latest_order = max(
            history_orders,
            key=lambda item: (
                int(item.get("order_time") or 0),
                str(item.get("order_id") or ""),
            ),
            default=None,
        )
        cursor_ms = latest_cursor[0] or now_ms
        cursor_order_id = latest_cursor[1]
        self._bn_history_seeded.add(pid)
        self._update_bn_poll_meta(
            pid,
            warmup_status="seeded" if history_orders else "empty",
            warmup_seeded_at_ms=now_ms,
            warmup_seed_count=len(history_orders),
            cursor_order_time=cursor_ms,
            cursor_order_id=cursor_order_id,
            last_remote_order_time=int((latest_order or {}).get("order_time") or 0),
            last_remote_order_id=str((latest_order or {}).get("order_id") or ""),
            last_remote_symbol=str((latest_order or {}).get("symbol") or ""),
            last_remote_action=str((latest_order or {}).get("action") or ""),
        )
        return cursor_ms, cursor_order_id, len(history_orders)

    def _storage_platform(self, platform: str) -> str:
        return _profile_storage_platform(self._profile, platform)

    def _exec_platform(self, platform: str) -> str:
        return _profile_exec_platform(platform)

    def _platform_label(self, platform: str) -> str:
        exec_platform = self._exec_platform(platform)
        if self._profile == "live":
            return f"Live {exec_platform.capitalize()}"
        return exec_platform.capitalize()

    def _unsupported_symbol_cache(self, platform: str) -> set[str]:
        if self._exec_platform(platform) == "binance":
            return self._unsupported_binance_symbols
        return self._unsupported_symbols

    def _cache_unsupported_symbol(self, platform: str, symbol: str) -> None:
        symbol_key = _clean_symbol_str(symbol or "").upper()
        if symbol_key:
            self._unsupported_symbol_cache(platform).add(symbol_key)

    def _is_cached_unsupported_symbol(self, platform: str, symbol: str) -> bool:
        symbol_key = _clean_symbol_str(symbol or "").upper()
        return bool(symbol_key) and symbol_key in self._unsupported_symbol_cache(platform)

    def _precheck_open_symbol(self, platform: str, symbol: str) -> str:
        symbol_key = _clean_symbol_str(symbol or "").upper()
        platform_label = self._platform_label(platform)
        exec_platform = self._exec_platform(platform)

        if not _is_reasonable_contract_symbol(symbol_key):
            self._cache_unsupported_symbol(platform, symbol_key)
            return f"invalid source symbol format: {symbol_key or symbol}"

        if self._is_cached_unsupported_symbol(platform, symbol_key):
            return f"{platform_label} symbol unavailable (cached)"

        runtime = self._runtime()
        try:
            if exec_platform == "binance":
                import binance_executor

                with binance_executor.use_runtime(base_url=runtime["binance_base_url"]):
                    binance_executor.get_symbol_filters(symbol_key)
            else:
                with order_executor.use_runtime(simulated=runtime["bitget_simulated"]):
                    order_executor.get_symbol_rules(symbol_key)
        except Exception as exc:
            lower = str(exc).lower()
            if exec_platform == "binance":
                is_invalid = _is_binance_symbol_error(exc) or ("not found" in lower and "symbol" in lower)
                if not is_invalid and isinstance(exc, ValueError) and not _is_request_timeout_error(exc):
                    is_invalid = True
            else:
                is_invalid = _is_symbol_not_exist_error(exc)
            if is_invalid:
                self._cache_unsupported_symbol(platform, symbol_key)
                return f"{platform_label} symbol unavailable: {exc}"
            logger.warning("[%s precheck] %s lookup failed: %s", platform_label, symbol_key, exc)

        return ""

    # ── 币安信号源监控 ────────────────────────────────────────────────────────

    def _run_binance(self) -> None:
        """独立线程：轮询所有币安交易员的操作记录，发现新信号就在 Bitget 下单。"""
        logger.info("[%s] 币安监控线程启动 [线程ID: %s]", self._profile, threading.current_thread().ident)
        try:
            while self._running:
                try:
                    self._loop_binance_once()
                except Exception as exc:
                    logger.error("[%s] Binance loop error: %s", self._profile, exc, exc_info=True)
                time.sleep(3)
        except Exception as fatal:
            logger.critical("[%s] 币安监控线程遭遇致命异常: %s", self._profile, fatal, exc_info=True)
        finally:
            # 线程即将退出，确保状态正确
            with self._state_lock:
                was_running = self._running
                self._running = False
                if was_running:
                    logger.error("[%s] ⚠️ 币安监控线程异常退出！引擎状态已自动设为停止", self._profile)
                else:
                    logger.info("[%s] 币安监控线程正常退出", self._profile)

    def _loop_binance_once(self) -> None:
        settings = db.get_copy_settings_profile(self._profile)
        if not settings or not settings.get("engine_enabled"):
            return

        # Bitget API
        bg_api_key = settings.get("api_key") or ""
        bg_secret = settings.get("api_secret") or ""
        bg_pass = settings.get("api_passphrase") or ""
        bg_enabled = bool(bg_api_key and bg_secret and bg_pass)

        # Binance API
        bn_api_key = settings.get("binance_api_key") or ""
        bn_secret = settings.get("binance_api_secret") or ""
        bn_enabled = bool(bn_api_key and bn_secret)

        if not (bg_enabled or bn_enabled):
            return

        # 解析币安交易员列表
        bn_raw = settings.get("binance_traders") or "{}"
        try:
            bn_traders_data = json.loads(bn_raw) if isinstance(bn_raw, str) else bn_raw
        except Exception:
            bn_traders_data = {}
        if not isinstance(bn_traders_data, dict):
            bn_traders_data = {}

        # 只处理已启用跟单的币安交易员
        bn_traders = {
            pid: data for pid, data in bn_traders_data.items()
            if isinstance(data, dict) and data.get("copy_enabled") is True
        }
        if not bn_traders:
            return

        runtime = self._runtime()
        bg_platform = self._storage_platform("bitget")
        bn_platform = self._storage_platform("binance")

        bg_available_usdt = 0.0
        bn_available_usdt = 0.0
        bg_wallet_balance = 0.0
        bn_wallet_balance = 0.0

        # Bitget 余额
        if bg_enabled:
            try:
                with order_executor.use_runtime(simulated=runtime["bitget_simulated"]):
                    bal = order_executor.get_account_balance(bg_api_key, bg_secret, bg_pass)
                self._pos_mode = str(bal.get("_posMode", "2"))
                bg_available_usdt = _extract_balance_usdt(bal)
                bg_wallet_balance = _extract_wallet_equity_usdt(bal) or bg_available_usdt
            except Exception as exc:
                logger.warning("Bitget 循环同步模式失败: %s", exc)
                bg_enabled = False

        # Binance 余额
        if bn_enabled:
            import binance_executor
            try:
                with binance_executor.use_runtime(base_url=runtime["binance_base_url"]):
                    bn_bal = binance_executor.get_account_balance(bn_api_key, bn_secret)
                bn_available_usdt = _safe_float(bn_bal.get("availableBalance", 0.0))
                bn_wallet_balance = _extract_wallet_equity_usdt(bn_bal) or _safe_float(bn_bal.get("balance", 0.0))
            except Exception as exc:
                logger.warning("币安 循环同步余额失败: %s", exc)
                bn_enabled = False

        if not (bg_enabled or bn_enabled):
            logger.warning("所有账户可用余额同步失败，跳过币安信号处理")
            return

        # ======== 风控参数（分平台独立计算） ========
        total_trader_count = max(len(bn_traders), 1)

        # 1. Bitget 全局参数
        bg_tol = _safe_float(settings.get("price_tolerance"), 0.01)
        bg_follow_ratio = self._resolve_follow_ratio(settings)
        bg_fallback_margin = 0.0
        bg_total_p = _safe_float(settings.get("total_capital"), 0.0)
        bg_max_m = _safe_float(settings.get("max_margin_pct"), 0.20)
        if bg_total_p > 0 and bg_max_m > 0:
            bg_fallback_margin = (bg_total_p / total_trader_count) * bg_max_m

        # 2. 币安独立参数
        bn_tol = _safe_float(settings.get("binance_price_tolerance"), 0.01)
        bg_allocation = build_platform_allocation_details(settings, "bitget", bn_traders, bg_available_usdt)
        bn_allocation = build_platform_allocation_details(settings, "binance", bn_traders, bn_available_usdt)
        
        bn_ratio_raw = _safe_float(settings.get("binance_follow_ratio_pct"), 0.003)
        if bn_ratio_raw > 1:
            bn_ratio_raw = bn_ratio_raw / 100.0
        bn_follow_ratio = min(max(bn_ratio_raw, 0.0), 1.0)
        
        bn_fallback_margin = 0.0
        bn_total_p = _safe_float(settings.get("binance_total_capital"), 0.0)
        bn_max_m = _safe_float(settings.get("binance_max_margin_pct"), 0.20)
        if bn_total_p > 0 and bn_max_m > 0:
            bn_fallback_margin = (bn_total_p / total_trader_count) * bn_max_m

        bg_allow_open, bg_guard_note = self._evaluate_open_guard(bg_platform, bg_wallet_balance, settings) if bg_enabled else (False, "")
        bn_allow_open, bn_guard_note = self._evaluate_open_guard(bn_platform, bn_wallet_balance, settings) if bn_enabled else (False, "")

        self._manage_protective_exits(
            settings,
            (bg_api_key, bg_secret, bg_pass) if bg_enabled else None,
            (bn_api_key, bn_secret, "") if bn_enabled else None,
        )

        now_ms = int(time.time() * 1000)
        for pid in bn_traders:
            try:
                poll_started_at = _now_ms()
                self._update_bn_poll_meta(
                    pid,
                    last_poll_started_at_ms=poll_started_at,
                    last_poll_profile=self._profile,
                )
                trader_cfg = bn_traders.get(pid) or {}
                bg_trader_alloc = (bg_allocation.get("traders") or {}).get(pid, {})
                bn_trader_alloc = (bn_allocation.get("traders") or {}).get(pid, {})
                bg_follow_ratio = _resolve_trader_follow_ratio(trader_cfg, bg_allocation.get("global_follow_ratio", 0.0))
                bn_follow_ratio = _resolve_trader_follow_ratio(trader_cfg, bn_allocation.get("global_follow_ratio", 0.0))
                bg_fallback_margin = _safe_float(bg_trader_alloc.get("effective_margin_cap"), 0.0)
                bn_fallback_margin = _safe_float(bn_trader_alloc.get("effective_margin_cap"), 0.0)
                bg_follow_ratio, bg_dynamic_note = self._resolve_dynamic_sizing(pid, bg_follow_ratio, bg_fallback_margin, bg_platform)
                bn_follow_ratio, bn_dynamic_note = self._resolve_dynamic_sizing(pid, bn_follow_ratio, bn_fallback_margin, bn_platform)
                if pid not in self._bn_seen:
                    self._bn_seen[pid] = now_ms
                    self._bn_seen_order_id[pid] = ""
                    logger.info("币安交易员 %s 首次接入，等待历史热身", pid[:12])

                if pid not in self._bn_history_seeded:
                    if db.get_source_trader_events(pid, limit=1):
                        self._bn_history_seeded.add(pid)
                        self._update_bn_poll_meta(
                            pid,
                            warmup_status="existing_history",
                            warmup_seeded_at_ms=now_ms,
                            warmup_seed_count=0,
                            cursor_order_time=int(self._bn_seen.get(pid) or 0),
                            cursor_order_id=str(self._bn_seen_order_id.get(pid) or ""),
                        )
                    else:
                        seed_ms, seed_order_id, seed_count = self._seed_binance_trader_history(pid, now_ms)
                        current_cursor = (
                            int(self._bn_seen.get(pid) or 0),
                            str(self._bn_seen_order_id.get(pid) or ""),
                        )
                        seed_cursor = (seed_ms, seed_order_id)
                        if seed_cursor > current_cursor:
                            self._bn_seen[pid] = seed_ms
                            self._bn_seen_order_id[pid] = seed_order_id
                        logger.info("币安交易员 %s 历史热身完成，补齐 %s 条源事件", pid[:12], seed_count)

                if not trader_cfg.get("last_sync_open_at") and trader_cfg.get("sync_open_positions_pending") is not True:
                    self._set_binance_trader_sync_state(
                        bn_traders_data,
                        pid,
                        sync_open_positions_pending=True,
                    )
                    trader_cfg = dict(bn_traders_data.get(pid) or trader_cfg)

                self._sync_binance_open_positions(
                    settings,
                    bn_traders_data,
                    pid,
                    trader_cfg,
                    {"ak": bg_api_key, "sk": bg_secret, "pp": bg_pass} if bg_enabled else None,
                    {"ak": bn_api_key, "sk": bn_secret} if bn_enabled else None,
                    bg_platform,
                    bn_platform,
                    bg_fallback_margin,
                    bg_tol,
                    bg_follow_ratio,
                    bg_dynamic_note,
                    bg_available_usdt,
                    bg_allow_open,
                    bg_guard_note,
                    bn_fallback_margin,
                    bn_tol,
                    bn_follow_ratio,
                    bn_dynamic_note,
                    bn_available_usdt,
                    bn_allow_open,
                    bn_guard_note,
                )

                since_ms = self._bn_seen[pid]
                since_order_id = self._bn_seen_order_id.get(pid, "")
                new_orders = binance_scraper.fetch_latest_orders(
                    pid,
                    since_ms=since_ms,
                    since_order_id=since_order_id,
                    limit=100,
                )
                latest_remote_order = max(
                    new_orders,
                    key=lambda item: (
                        int(item.get("order_time") or 0),
                        str(item.get("order_id") or ""),
                    ),
                    default=None,
                )
                self._update_bn_poll_meta(
                    pid,
                    last_poll_finished_at_ms=_now_ms(),
                    last_poll_ok=True,
                    last_poll_error="",
                    last_new_order_count=len(new_orders),
                    last_remote_order_time=int((latest_remote_order or {}).get("order_time") or 0),
                    last_remote_order_id=str((latest_remote_order or {}).get("order_id") or ""),
                    last_remote_symbol=str((latest_remote_order or {}).get("symbol") or ""),
                    last_remote_action=str((latest_remote_order or {}).get("action") or ""),
                )
                self._store_source_orders(pid, new_orders, source_kind="live")
                seen_in_batch: set[str] = set()
                for order in reversed(new_orders):  # 从旧到新处理
                    order_key = (
                        str(order.get("order_id") or "")
                        or f"{order.get('order_time')}-{order.get('symbol')}-{order.get('action')}-{order.get('direction')}"
                    )
                    if order_key in seen_in_batch:
                        continue
                    seen_in_batch.add(order_key)
                    bg_creds = {"ak": bg_api_key, "sk": bg_secret, "pp": bg_pass} if bg_enabled else None
                    bn_creds = {"ak": bn_api_key, "sk": bn_secret} if bn_enabled else None
                    self._process_binance_order(
                        settings,
                        bg_creds, bn_creds, pid, order,
                        bg_fallback_margin=bg_fallback_margin, bg_tol=bg_tol, bg_follow_ratio=bg_follow_ratio, bg_available_usdt=bg_available_usdt,
                        bg_allow_open=bg_allow_open, bg_guard_note=bg_guard_note, bg_dynamic_note=bg_dynamic_note,
                        bn_fallback_margin=bn_fallback_margin, bn_tol=bn_tol, bn_follow_ratio=bn_follow_ratio, bn_available_usdt=bn_available_usdt,
                        bn_allow_open=bn_allow_open, bn_guard_note=bn_guard_note, bn_dynamic_note=bn_dynamic_note
                    )
                    # 更新已处理的最新 Binance 游标；同时间戳内再用 order_id 去重。
                    order_cursor = (
                        int(order.get("order_time") or 0),
                        str(order.get("order_id") or ""),
                    )
                    current_cursor = (
                        int(self._bn_seen.get(pid) or 0),
                        str(self._bn_seen_order_id.get(pid) or ""),
                    )
                    if order_cursor > current_cursor:
                        self._bn_seen[pid] = order_cursor[0]
                        self._bn_seen_order_id[pid] = order_cursor[1]
                        self._update_bn_poll_meta(
                            pid,
                            cursor_order_time=order_cursor[0],
                            cursor_order_id=order_cursor[1],
                        )
                self._reconcile_missing_local_positions(
                    settings,
                    pid,
                    {"ak": bg_api_key, "sk": bg_secret, "pp": bg_pass} if bg_enabled else None,
                    {"ak": bn_api_key, "sk": bn_secret} if bn_enabled else None,
                    bg_platform,
                    bn_platform,
                    bg_fallback_margin,
                    bg_tol,
                    bg_follow_ratio,
                    bg_dynamic_note,
                    bg_available_usdt,
                    bg_allow_open,
                    bg_guard_note,
                    bn_fallback_margin,
                    bn_tol,
                    bn_follow_ratio,
                    bn_dynamic_note,
                    bn_available_usdt,
                    bn_allow_open,
                    bn_guard_note,
                )
                self._reconcile_source_positions(
                    [pid],
                    (bg_api_key, bg_secret, bg_pass) if bg_enabled else None,
                    (bn_api_key, bn_secret, "") if bn_enabled else None,
                    bg_tol,
                    bn_tol,
                )
            except Exception as e:
                self._update_bn_poll_meta(
                    pid,
                    last_poll_finished_at_ms=_now_ms(),
                    last_poll_ok=False,
                    last_poll_error=str(e)[:300],
                )
                logger.warning("币安交易员 %s 处理异常: %s", pid[:12], e)

        # 定期刷新元数据 (每 1 分钟)
        if time.time() - self._last_bn_metadata_refresh > 60:
            self._refresh_binance_metadata(bn_traders_data)
            self._last_bn_metadata_refresh = time.time()

    def _refresh_binance_metadata(self, current_data: dict) -> None:
        """从 API 刷新币安交易员的元数据并存入数据库"""
        logger.info("正在刷新币安交易员元数据…")
        changed = False
        for pid in current_data:
            try:
                info = binance_scraper.fetch_trader_info(pid)
                if info and "_warning" not in info:
                    current_data[pid].update({
                        "nickname": info.get("nickname"),
                        "follower_count": info.get("follower_count"),
                        "copier_pnl": info.get("copier_pnl"),
                        "aum": info.get("aum"),
                        "margin_balance": info.get("margin_balance"),
                        "avatar": info.get("avatar"),
                        "total_trades": info.get("total_trades"),
                    })
                    changed = True
            except Exception as e:
                logger.warning("刷新币安交易员 %s 元数据失败: %s", pid[:12], e)
        
        if changed:
            db.update_shared_copy_settings(binance_traders=json.dumps(current_data, ensure_ascii=False))
            logger.info("币安交易员元数据已更新到数据库")

    def _resolve_follow_ratio(self, settings: dict) -> float:
        """
        读取全局跟随比例（0~1）。
        兼容历史值：若误传百分数（>1），按百分比换算。
        """
        return _normalize_ratio_value(settings.get("follow_ratio_pct"), 0.003)

    def _apply_follow_ratio(self, source_margin: float, follow_ratio: float, fallback_margin: float) -> tuple[float, str]:
        """
        按"来源保证金 * 跟随比例"计算目标保证金。
        当来源保证金不可得时，退回兜底保证金。
        """
        if source_margin > 0 and follow_ratio > 0:
            target = source_margin * follow_ratio
            return target, f"[比例跟随] ratio={follow_ratio * 100:.4f}% src={source_margin:.4f} target={target:.4f}"
        if source_margin > 0:
            return source_margin, f"[比例跟随] ratio=0，回退来源原值 src={source_margin:.4f}"
        if fallback_margin > 0:
            return fallback_margin, "[比例跟随] 来源保证金缺失，回退资金池兜底"
        return 0.0, "[比例跟随] 来源保证金缺失且无兜底"

    def _resolve_dynamic_sizing(
        self,
        trader_uid: str,
        base_follow_ratio: float,
        fallback_margin: float,
        platform: str,
    ) -> tuple[float, str]:
        ratio = _normalize_ratio_value(base_follow_ratio, 0.0)
        if ratio <= 0:
            return ratio, ""

        try:
            analytics = db.get_trader_analysis_snapshot(trader_uid, lookback_days=45)
        except Exception as exc:
            logger.debug("[dynamic sizing] trader=%s analytics unavailable: %s", trader_uid[:12], exc)
            return ratio, ""

        history_samples = max(
            _safe_int(analytics.get("history_sample_size"), 0),
            _safe_int(analytics.get("cycle_sample_size"), 0),
            _safe_int(analytics.get("copy_open_sample_size"), 0),
        )
        confidence = min(1.0, history_samples / 12.0) if history_samples > 0 else 0.0
        if confidence <= 0:
            return ratio, ""

        clip_rate = _safe_float(analytics.get("clip_rate"), 0.0)
        reverse_rate = _safe_float(analytics.get("reverse_rate"), 0.0)
        min_adjust_rate = _safe_float(analytics.get("min_adjust_rate"), 0.0)
        small_skip_rate = _safe_float(analytics.get("small_order_skip_rate"), 0.0)
        median_hold_sec = _safe_float(analytics.get("median_hold_sec"), 0.0)
        total_score = _safe_float(analytics.get("total_score"), 0.0)
        execution_score = _safe_float(analytics.get("execution_score"), 0.0)
        close_reliability = _safe_float(analytics.get("close_reliability_score"), 0.0)
        median_source_margin = _safe_float(analytics.get("median_source_margin"), 0.0)

        raw_scale = 1.0
        reasons: list[str] = []

        if clip_rate >= 0.70:
            raw_scale *= 0.72
            reasons.append(f"clip={clip_rate * 100:.0f}%")
        elif clip_rate >= 0.40:
            raw_scale *= 0.85
            reasons.append(f"clip={clip_rate * 100:.0f}%")
        elif clip_rate <= 0.15 and history_samples >= 6:
            raw_scale *= 1.05
            reasons.append(f"clip={clip_rate * 100:.0f}%")

        if reverse_rate >= 0.35:
            raw_scale *= 0.84
            reasons.append(f"reverse={reverse_rate * 100:.0f}%")
        elif reverse_rate <= 0.10 and history_samples >= 6:
            raw_scale *= 1.04
            reasons.append(f"reverse={reverse_rate * 100:.0f}%")

        if median_hold_sec > 0 and median_hold_sec <= 900:
            raw_scale *= 0.88
            reasons.append(f"hold={median_hold_sec / 60:.1f}m")
        elif median_hold_sec >= 4 * 3600:
            raw_scale *= 1.04
            reasons.append(f"hold={median_hold_sec / 3600:.1f}h")

        if min_adjust_rate >= 0.35 and clip_rate < 0.20:
            raw_scale *= 1.08
            reasons.append(f"minfix={min_adjust_rate * 100:.0f}%")

        if small_skip_rate >= 0.30:
            raw_scale *= 0.92
            reasons.append(f"smallskip={small_skip_rate * 100:.0f}%")

        if total_score >= 70 and execution_score >= 70 and close_reliability >= 60:
            raw_scale *= 1.08
            reasons.append(f"score={total_score:.0f}")
        elif total_score <= 45 or execution_score <= 40 or close_reliability <= 35:
            raw_scale *= 0.88
            reasons.append(f"score={total_score:.0f}")

        if fallback_margin > 0 and median_source_margin > 0:
            cap_pressure = fallback_margin / median_source_margin
            if cap_pressure < 0.20:
                raw_scale *= 0.82
                reasons.append(f"capfit={cap_pressure * 100:.0f}%")
            elif cap_pressure > 0.80:
                raw_scale *= 1.03
                reasons.append(f"capfit={cap_pressure * 100:.0f}%")

        raw_scale = min(max(raw_scale, 0.55), 1.25)
        scale = 1.0 + ((raw_scale - 1.0) * confidence)
        effective_ratio = min(max(ratio * scale, 0.0), 1.0)

        if abs(effective_ratio - ratio) <= 1e-6:
            return ratio, ""

        label = self._platform_label(platform)
        note = (
            f"[动态仓位] {label} ratio={ratio * 100:.2f}%->"
            f"{effective_ratio * 100:.2f}% scale={scale:.3f} conf={confidence:.2f}"
        )
        if reasons:
            note = f"{note} ({', '.join(reasons[:4])})"
        return effective_ratio, note

    def _cap_open_margin(
        self,
        margin: float,
        fallback_margin: float,
        available_usdt: float,
        source_tag: str,
        symbol: str,
        direction: str,
    ) -> tuple[float, str]:
        """
        开仓保证金裁剪：
        1) 优先遵循资金池单交易员上限（fallback_margin）；
        2) 不超过账户可用余额的 95%；
        返回 (裁剪后保证金, 备注)。
        """
        if margin <= 0:
            return 0.0, ""

        caps: list[tuple[str, float]] = []
        if fallback_margin > 0:
            caps.append(("pool", fallback_margin))
        if available_usdt > 0:
            caps.append(("available95", available_usdt * 0.95))

        if not caps:
            return margin, ""

        cap_value = min(v for _, v in caps if v > 0)
        if cap_value <= 0:
            return 0.0, ""

        if margin <= cap_value:
            return margin, ""

        cap_reason = ", ".join(f"{k}={v:.4f}" for k, v in caps)
        logger.warning(
            "[%s保证金裁剪] %s %s 来源=%.4f -> %.4f (%s)",
            source_tag,
            symbol,
            direction.upper(),
            margin,
            cap_value,
            cap_reason,
        )
        return cap_value, f"[保证金裁剪] src={margin:.4f} cap={cap_value:.4f} ({cap_reason})"

    def _estimate_binance_margin(self, order: dict) -> float:
        qty = abs(_safe_float(order.get("qty"), 0.0))
        price = _safe_float(order.get("price"), 0.0)
        lev = max(1, _safe_int(order.get("leverage"), 1))
        estimated = _estimate_margin_from_position(qty, price, lev)
        if estimated > 0:
            return estimated
        return 0.0

    def _evaluate_open_guard(self, platform: str, wallet_balance: float, settings: dict) -> tuple[bool, str]:
        if wallet_balance <= 0:
            return True, ""

        day = time.strftime("%Y-%m-%d", time.localtime())
        daily = db.upsert_platform_daily_equity(platform, day, wallet_balance)
        start_equity = _safe_float(daily.get("start_equity"), 0.0)
        day_drawdown = 0.0
        if start_equity > 0:
            day_drawdown = max(0.0, (start_equity - wallet_balance) / start_equity)

        peak_equity = max(db.get_platform_equity_peak(platform, since_days=60), wallet_balance, start_equity)
        total_drawdown = 0.0
        if peak_equity > 0:
            total_drawdown = max(0.0, (peak_equity - wallet_balance) / peak_equity)

        daily_limit = _safe_float(settings.get("daily_loss_limit_pct"), config.DEFAULT_DAILY_LOSS_LIMIT_PCT)
        total_limit = _safe_float(settings.get("total_drawdown_limit_pct"), config.DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT)
        reasons: list[str] = []
        if daily_limit > 0 and day_drawdown >= daily_limit:
            reasons.append(f"day_drawdown {day_drawdown * 100:.2f}% >= {daily_limit * 100:.2f}%")
        if total_limit > 0 and total_drawdown >= total_limit:
            reasons.append(f"total_drawdown {total_drawdown * 100:.2f}% >= {total_limit * 100:.2f}%")

        if reasons:
            reason_text = " | ".join(reasons)
            log_key = f"{platform}:{reason_text}"
            if log_key not in self._risk_pause_logged:
                logger.warning("[%s risk guard] %s | equity=%.4f start=%.4f peak=%.4f", self._platform_label(platform), reason_text, wallet_balance, start_equity, peak_equity)
                self._risk_pause_logged.add(log_key)
            return False, reason_text

        self._risk_pause_logged = {k for k in self._risk_pause_logged if not k.startswith(f"{platform}:")}
        return True, ""

    def _save_position_state(
        self,
        platform: str,
        trader_uid: str,
        symbol: str,
        direction: str,
        current_state: dict | None = None,
        **updates: Any,
    ) -> dict:
        state = dict(current_state or {})
        payload = {
            "stage": _safe_int(state.get("stage"), 0),
            "peak_roi": _safe_float(state.get("peak_roi"), 0.0),
            "locked_roi_pct": _safe_float(state.get("locked_roi_pct"), 0.0),
            "breakeven_armed": 1 if state.get("breakeven_armed") else 0,
            "trail_active": 1 if state.get("trail_active") else 0,
            "closed_by_system": 1 if state.get("closed_by_system") else 0,
            "freeze_reentry": 1 if state.get("freeze_reentry") else 0,
            "last_source_order_id": str(state.get("last_source_order_id") or ""),
            "last_system_action": str(state.get("last_system_action") or ""),
        }
        payload.update(updates)
        return db.upsert_copy_position_state(platform, trader_uid, symbol, direction, **payload)

    def _find_active_copy_position(
        self,
        platform: str,
        trader_uid: str,
        symbol: str,
        direction: str,
    ) -> dict | None:
        platform_key = str(platform or "").strip().lower()
        trader_key = str(trader_uid or "").strip()
        symbol_key = _clean_symbol_str(symbol or "").upper()
        direction_key = str(direction or "").strip().lower()
        if direction_key not in ("long", "short"):
            return None

        for position in db.get_active_copy_position_summaries(platform_key):
            if str(position.get("trader_uid") or "").strip() != trader_key:
                continue
            if _clean_symbol_str(str(position.get("symbol") or "")).upper() != symbol_key:
                continue
            if str(position.get("direction") or "").strip().lower() != direction_key:
                continue
            if _safe_float(position.get("remaining_qty"), 0.0) <= 0:
                continue
            return position
        return None

    def _get_exchange_position_qty(
        self,
        platform: str,
        api_creds: tuple,
        symbol: str,
        direction: str,
    ) -> float:
        exec_platform = self._exec_platform(platform)
        symbol_key = _clean_symbol_str(symbol or "").upper()
        direction_key = str(direction or "").strip().lower()
        if direction_key not in ("long", "short"):
            return 0.0

        if exec_platform == "binance":
            import binance_executor

            ak, sk, _ = api_creds
            with binance_executor.use_runtime(base_url=self._runtime()["binance_base_url"]):
                raw_positions = binance_executor.get_my_positions(ak, sk)

            total_qty = 0.0
            for item in raw_positions or []:
                if _clean_symbol_str(str(item.get("symbol") or "")).upper() != symbol_key:
                    continue
                position_side = str(item.get("positionSide") or "").upper()
                position_amt = _safe_float(item.get("positionAmt"), 0.0)
                item_direction = ""
                qty = 0.0
                if position_side == "BOTH":
                    if position_amt > 0:
                        item_direction = "long"
                        qty = position_amt
                    elif position_amt < 0:
                        item_direction = "short"
                        qty = abs(position_amt)
                elif position_side in ("LONG", "SHORT"):
                    item_direction = position_side.lower()
                    qty = abs(position_amt)
                if item_direction == direction_key and qty > 0:
                    total_qty += qty
            return total_qty

        ak, sk, pp = api_creds
        with order_executor.use_runtime(simulated=self._runtime()["bitget_simulated"]):
            raw_positions = order_executor.get_my_positions(ak, sk, pp)

        total_qty = 0.0
        for item in raw_positions or []:
            if _clean_symbol_str(str(item.get("symbol") or "")).upper() != symbol_key:
                continue
            if str(item.get("holdSide") or "").strip().lower() != direction_key:
                continue
            qty = 0.0
            for key in ("total", "size", "holdVolume", "available", "pos"):
                qty = abs(_safe_float(item.get(key), 0.0))
                if qty > 0:
                    break
            total_qty += qty
        return total_qty

    def _get_market_price(self, platform: str, symbol: str, cache: dict[tuple[str, str], float]) -> float:
        key = (platform, symbol)
        if key in cache:
            return cache[key]
        exec_platform = self._exec_platform(platform)
        if exec_platform == "binance":
            import binance_executor
            with binance_executor.use_runtime(base_url=self._runtime()["binance_base_url"]):
                price = binance_executor.get_ticker_price(symbol)
        else:
            with order_executor.use_runtime(simulated=self._runtime()["bitget_simulated"]):
                price = get_ticker_price(symbol)
        cache[key] = price
        return price

    def _insert_close_order(
        self,
        *,
        platform: str,
        trader_uid: str,
        tracking_no: str,
        symbol: str,
        direction: str,
        source_price: float,
        exec_price: float,
        pnl: float | None,
        exec_qty: float,
        status: str,
        notes: str,
    ) -> None:
        _insert_copy_order_and_notify({
            "timestamp": _now_ms(),
            "trader_uid": trader_uid,
            "tracking_no": tracking_no,
            "my_order_id": "",
            "symbol": symbol,
            "direction": direction,
            "leverage": 0,
            "margin_usdt": 0,
            "source_price": source_price,
            "exec_price": exec_price,
            "deviation_pct": 0,
            "action": "close",
            "status": status,
            "pnl": pnl,
            "notes": notes,
            "exec_qty": exec_qty,
            "platform": platform,
        })

    def _record_exchange_flat_close(
        self,
        *,
        platform: str,
        trader_uid: str,
        tracking_no: str,
        symbol: str,
        direction: str,
        source_price: float,
        exec_price: float,
        pnl: float | None,
        exec_qty: float,
        base_note: str,
        live_qty: float | None = None,
        extra_note: str = "",
    ) -> None:
        note_parts = [base_note]
        if live_qty is not None and live_qty > 1e-12:
            note_parts.append(f"lookup reported qty={live_qty:.8f}")
        if extra_note:
            note_parts.append(extra_note)
        self._insert_close_order(
            platform=platform,
            trader_uid=trader_uid,
            tracking_no=tracking_no,
            symbol=symbol,
            direction=direction,
            source_price=source_price,
            exec_price=exec_price,
            pnl=pnl,
            exec_qty=exec_qty,
            status="filled",
            notes=" | ".join(part for part in note_parts if part).strip(),
        )

    def _advance_reconcile_state(
        self,
        platform: str,
        trader_uid: str,
        symbol: str,
        direction: str,
        flatten_marker: int,
    ) -> dict[str, Any]:
        key = (str(platform), str(trader_uid), _clean_symbol_str(symbol).upper(), str(direction).lower())
        now_ms = _now_ms()
        state = self._reconcile_pending.get(key)
        if not state or int(state.get("marker") or 0) != int(flatten_marker or 0):
            state = {"marker": int(flatten_marker or 0), "first_seen_ms": now_ms, "polls": 1}
        else:
            state["polls"] = int(state.get("polls") or 0) + 1
        state["last_seen_ms"] = now_ms
        self._reconcile_pending[key] = state
        return state

    def _clear_reconcile_state(
        self,
        platform: str,
        trader_uid: str,
        symbol: str,
        direction: str,
    ) -> None:
        key = (str(platform), str(trader_uid), _clean_symbol_str(symbol).upper(), str(direction).lower())
        self._reconcile_pending.pop(key, None)

    def _execute_managed_close(
        self,
        platform: str,
        api_creds: tuple | None,
        position: dict,
        close_qty: float,
        current_price: float,
        label: str,
        note: str,
    ) -> bool:
        if not api_creds:
            return False

        close_qty = max(0.0, min(_safe_float(close_qty, 0.0), _safe_float(position.get("remaining_qty"), 0.0)))
        if close_qty <= 0:
            return False

        tracking_no = f"SYS_{label.replace(' ', '_').upper()}_{int(time.time() * 1000)}"
        estimated_pnl = _safe_float(position.get("estimated_pnl"), 0.0)
        remaining_qty = max(_safe_float(position.get("remaining_qty"), 0.0), 0.0)
        pnl_share = estimated_pnl * (close_qty / remaining_qty) if remaining_qty > 0 else estimated_pnl
        ak, sk, pp = api_creds
        exec_platform = self._exec_platform(platform)
        platform_label = self._platform_label(platform)
        qty_str = ""

        try:
            if exec_platform == "binance":
                import binance_executor
                with binance_executor.use_runtime(base_url=self._runtime()["binance_base_url"]):
                    filters = binance_executor.get_symbol_filters(position["symbol"])
                    qty_str = binance_executor._format_qty(close_qty, filters["stepSize"])
                    if float(qty_str) <= 0:
                        logger.warning("[%s managed close] qty rounded to zero: %s %s", platform_label, position["symbol"], close_qty)
                        return False
                    binance_executor.close_partial_position(
                        ak, sk, position["symbol"], position["direction"], qty_str
                    )
            else:
                qty_value = _trunc4(close_qty)
                qty_str = f"{qty_value:.4f}".rstrip("0").rstrip(".")
                if not qty_str or float(qty_str) <= 0:
                    logger.warning("[%s managed close] qty rounded to zero: %s %s", platform_label, position["symbol"], close_qty)
                    return False
                with order_executor.use_runtime(simulated=self._runtime()["bitget_simulated"]):
                    order_executor.close_partial_position(
                        ak, sk, pp, position["symbol"], position["direction"], qty_str,
                        pos_mode=self._pos_mode, margin_mode="isolated"
                    )

            self._insert_close_order(
                platform=platform,
                trader_uid=position["trader_uid"],
                tracking_no=tracking_no,
                symbol=position["symbol"],
                direction=position["direction"],
                source_price=current_price,
                exec_price=current_price,
                pnl=pnl_share,
                exec_qty=float(qty_str),
                status="filled",
                notes=f"[{label}] {note}".strip(),
            )
            logger.info("[%s managed close] %s %s qty=%s note=%s", platform_label, position["symbol"], position["direction"].upper(), qty_str, label)
            return True
        except Exception as exc:
            is_missing = (exec_platform == "bitget" and _is_bitget_position_missing_error(exc)) or (
                exec_platform == "binance" and _is_binance_position_missing_error(exc)
            )
            if is_missing:
                live_qty = None
                try:
                    live_qty = self._get_exchange_position_qty(platform, api_creds, position["symbol"], position["direction"])
                except Exception as lookup_exc:
                    logger.info(
                        "[%s managed close reconcile] %s %s lookup failed: %s",
                        platform_label, position["symbol"], position["direction"].upper(), lookup_exc,
                    )
                self._record_exchange_flat_close(
                    platform=platform,
                    trader_uid=position["trader_uid"],
                    tracking_no=tracking_no,
                    symbol=position["symbol"],
                    direction=position["direction"],
                    source_price=current_price,
                    exec_price=current_price,
                    pnl=pnl_share,
                    exec_qty=close_qty,
                    base_note=f"[{label}] exchange already flat on managed close",
                    live_qty=live_qty,
                    extra_note=note,
                )
                logger.info(
                    "[%s managed close reconcile] %s %s already flat on exchange, qty=%.8f",
                    platform_label, position["symbol"], position["direction"].upper(), close_qty,
                )
                return True

            self._insert_close_order(
                platform=platform,
                trader_uid=position["trader_uid"],
                tracking_no=f"FAIL_{tracking_no}",
                symbol=position["symbol"],
                direction=position["direction"],
                source_price=current_price,
                exec_price=current_price,
                pnl=0.0,
                exec_qty=0.0,
                status="failed",
                notes=f"[{label}] {exc} | {note}".strip(),
            )
            logger.error("[%s managed close failed] %s %s: %s", platform_label, position["symbol"], position["direction"].upper(), exc)
            return False

    def _manage_protective_exits(self, settings: dict, bg_creds: tuple | None, bn_creds: tuple | None) -> None:
        if not int(settings.get("take_profit_enabled") or 0):
            return

        price_cache: dict[tuple[str, str], float] = {}
        platform_creds = {
            self._storage_platform("bitget"): bg_creds,
            self._storage_platform("binance"): bn_creds,
        }
        active_positions = []
        for platform_key in platform_creds.keys():
            active_positions.extend(db.get_active_copy_position_summaries(platform_key))

        for position in active_positions:
            platform = str(position.get("platform") or self._storage_platform("bitget")).lower()
            api_creds = platform_creds.get(platform)
            if not api_creds:
                continue

            symbol = str(position.get("symbol") or "")
            direction = str(position.get("direction") or "").lower()
            if not symbol or direction not in ("long", "short"):
                continue

            try:
                current_price = self._get_market_price(platform, symbol, price_cache)
            except Exception as exc:
                logger.warning("[%s managed price] %s %s: %s", self._platform_label(platform), symbol, direction.upper(), exc)
                continue

            remaining_qty = max(_safe_float(position.get("remaining_qty"), 0.0), 0.0)
            remaining_margin = max(_safe_float(position.get("remaining_margin"), 0.0), 0.0)
            entry_price = _safe_float(position.get("avg_entry_price"), 0.0)
            leverage = max(1, _safe_int(position.get("leverage"), 1))
            if remaining_margin <= 0 and entry_price > 0 and remaining_qty > 0:
                remaining_margin = _estimate_margin_from_position(remaining_qty, entry_price, leverage)

            metrics = _estimate_position_pnl_roi(entry_price, current_price, remaining_qty, remaining_margin, direction)
            position["remaining_margin"] = remaining_margin
            position["current_price"] = current_price
            position["estimated_pnl"] = metrics["pnl"]
            position["roi"] = metrics["roi"]

            state = db.get_copy_position_state(platform, position["trader_uid"], symbol, direction)
            last_source_order_id = str(position.get("last_open_tracking_no") or state.get("last_source_order_id") or "")
            decision = _decide_take_profit_action(position, state, settings)
            peak_roi = _safe_float(decision.get("peak_roi"), _safe_float(state.get("peak_roi"), 0.0))

            if peak_roi > _safe_float(state.get("peak_roi"), 0.0) or last_source_order_id != str(state.get("last_source_order_id") or ""):
                state = self._save_position_state(
                    platform, position["trader_uid"], symbol, direction, state,
                    peak_roi=peak_roi,
                    last_source_order_id=last_source_order_id,
                )

            action = decision.get("action")
            if not action:
                continue

            close_qty = remaining_qty if action.get("kind") == "close_all" else max(_safe_float(action.get("qty"), 0.0), 0.0)
            if close_qty <= 0:
                continue

            detail_note = (
                f"{action.get('note', '')} entry={entry_price:.6f} mark={current_price:.6f} "
                f"roi={metrics['roi'] * 100:.2f}% qty={close_qty:.6f}"
            ).strip()
            if not self._execute_managed_close(platform, api_creds, position, close_qty, current_price, action["label"], detail_note):
                continue

            next_state = dict(action.get("next_state") or {})
            next_state.setdefault("peak_roi", peak_roi)
            next_state.setdefault("last_source_order_id", last_source_order_id)
            self._save_position_state(platform, position["trader_uid"], symbol, direction, state, **next_state)

    def _get_entry_execution_settings(self, settings: dict) -> dict:
        return {
            "mode": _normalize_entry_order_mode(settings.get("entry_order_mode"), config.DEFAULT_ENTRY_ORDER_MODE),
            "maker_levels": _normalize_nonnegative_int(settings.get("entry_maker_levels"), config.DEFAULT_ENTRY_MAKER_LEVELS, minimum=0),
            "timeout_sec": _normalize_nonnegative_int(settings.get("entry_limit_timeout_sec"), config.DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC, minimum=1),
            "fallback_to_market": _normalize_bool_setting(
                settings.get("entry_limit_fallback_to_market"),
                config.DEFAULT_ENTRY_LIMIT_FALLBACK_TO_MARKET,
            ),
        }

    def _execute_maker_priority_open(
        self,
        platform: str,
        api_creds: tuple,
        symbol: str,
        direction: str,
        leverage: int,
        margin: float,
        signal_price: float,
        current_price: float,
        tol: float,
        client_oid: str,
        timeout_sec: int,
        maker_levels: int,
    ) -> dict:
        ak, sk, pp = api_creds
        note_parts: list[str] = []
        order_ids: list[str] = []
        exec_platform = self._exec_platform(platform)
        runtime = self._runtime()

        if exec_platform == "binance":
            import binance_executor

            with binance_executor.use_runtime(base_url=runtime["binance_base_url"]):
                quote = binance_executor.get_book_ticker(symbol)
                filters = binance_executor.get_symbol_filters(symbol)
                bid = _safe_float(quote.get("bidPrice"), 0.0)
                ask = _safe_float(quote.get("askPrice"), 0.0)
                tick = _safe_float(filters.get("tickSize"), 0.0)
                limit_price = _pick_maker_limit_price(direction, bid, ask, current_price, tick, maker_levels)
                if limit_price <= 0:
                    return {"status": "skipped", "reason": "Binance Maker 价格无效", "note": "[MakerPriority] invalid_limit_price"}

                limit_client_oid = f"{client_oid}_L" if client_oid else f"bn_limit_{int(time.time() * 1000)}"
                note_parts.append(f"[MakerPriority] limit={limit_price:.8f} wait={timeout_sec}s")
                try:
                    limit_res = binance_executor.place_limit_order(
                        ak, sk, symbol, direction, leverage, "ISOLATED", margin,
                        limit_price=limit_price, client_oid=limit_client_oid, post_only=True,
                    )
                except Exception as exc:
                    if _is_post_only_rejected_error(exc):
                        note_parts.append("maker_rejected_post_only")
                        return {
                            "status": "unfilled",
                            "exec_qty": 0.0,
                            "exec_price": limit_price,
                            "margin_used": 0.0,
                            "remaining_margin": margin,
                            "order_id": "",
                            "note": " ".join(note_parts),
                        }
                    raise
                order_id = str(limit_res.get("orderId") or "")
                if order_id:
                    order_ids.append(order_id)
                time.sleep(timeout_sec)

                detail = binance_executor.get_order(ak, sk, symbol, order_id=order_id, client_oid=limit_client_oid)
                snapshot = _parse_binance_order_snapshot(detail or limit_res, fallback_price=limit_price)
                if snapshot["status"] != "FILLED":
                    try:
                        binance_executor.cancel_order(ak, sk, symbol, order_id=order_id, client_oid=limit_client_oid)
                    except Exception as exc:
                        logger.info("[Binance maker cancel] %s %s: %s", symbol, direction.upper(), exc)
                    try:
                        detail = binance_executor.get_order(ak, sk, symbol, order_id=order_id, client_oid=limit_client_oid)
                        snapshot = _parse_binance_order_snapshot(detail or snapshot, fallback_price=limit_price)
                    except Exception:
                        pass

            filled_qty = _safe_float(snapshot.get("filled_qty"), 0.0)
            avg_price = _safe_float(snapshot.get("avg_price"), 0.0) or limit_price
            filled_margin = _estimate_margin_from_fill(filled_qty, avg_price, leverage)
            if filled_qty > 0:
                note_parts.append(f"maker_fill={filled_qty:.8f}")
            else:
                note_parts.append("maker_no_fill")

            remaining_margin = max(0.0, margin - filled_margin)
            if filled_qty > 0 and remaining_margin <= 1e-8:
                return {
                    "status": "filled",
                    "exec_qty": filled_qty,
                    "exec_price": avg_price,
                    "margin_used": filled_margin,
                    "order_id": ",".join(order_ids),
                    "note": " ".join(note_parts),
                }

            return {
                "status": "partial" if filled_qty > 0 else "unfilled",
                "exec_qty": filled_qty,
                "exec_price": avg_price,
                "margin_used": filled_margin,
                "remaining_margin": remaining_margin,
                "order_id": ",".join(order_ids),
                "note": " ".join(note_parts),
            }

        with order_executor.use_runtime(simulated=runtime["bitget_simulated"]):
            quote = order_executor.get_ticker_snapshot(symbol)
            bid = _safe_float(quote.get("bidPr") or quote.get("bidPrice"), 0.0)
            ask = _safe_float(quote.get("askPr") or quote.get("askPrice"), 0.0)
            tick = _safe_float(order_executor.get_price_step(symbol), 0.0)
            limit_price = _pick_maker_limit_price(direction, bid, ask, current_price, tick, maker_levels)
            if limit_price <= 0:
                return {"status": "skipped", "reason": "Bitget Maker 价格无效", "note": "[MakerPriority] invalid_limit_price"}

            limit_client_oid = f"{client_oid}_L" if client_oid else f"bg_limit_{int(time.time() * 1000)}"
            note_parts.append(f"[MakerPriority] limit={limit_price:.8f} wait={timeout_sec}s")
            try:
                limit_res = order_executor.place_limit_order(
                    ak, sk, pp, symbol, direction, leverage,
                    "isolated", margin, limit_price=limit_price,
                    pos_mode=self._pos_mode, client_oid=limit_client_oid,
                )
            except Exception as exc:
                if _is_post_only_rejected_error(exc):
                    note_parts.append("maker_rejected_post_only")
                    return {
                        "status": "unfilled",
                        "exec_qty": 0.0,
                        "exec_price": limit_price,
                        "margin_used": 0.0,
                        "remaining_margin": margin,
                        "order_id": "",
                        "note": " ".join(note_parts),
                    }
                raise
            order_id = str(limit_res.get("orderId") or limit_res.get("clientOid") or "")
            if order_id:
                order_ids.append(order_id)
            time.sleep(timeout_sec)

            detail = order_executor.get_order_detail(ak, sk, pp, symbol, order_id=order_id, client_oid=limit_client_oid)
            snapshot = _parse_bitget_order_snapshot(detail or limit_res, fallback_price=limit_price)
            if snapshot["status"] != "filled":
                try:
                    order_executor.cancel_order(ak, sk, pp, symbol, order_id=order_id, client_oid=limit_client_oid)
                except Exception as exc:
                    logger.info("[Bitget maker cancel] %s %s: %s", symbol, direction.upper(), exc)
                try:
                    detail = order_executor.get_order_detail(ak, sk, pp, symbol, order_id=order_id, client_oid=limit_client_oid)
                    snapshot = _parse_bitget_order_snapshot(detail or snapshot, fallback_price=limit_price)
                except Exception:
                    pass

        filled_qty = _safe_float(snapshot.get("filled_qty"), 0.0)
        avg_price = _safe_float(snapshot.get("avg_price"), 0.0) or limit_price
        filled_margin = _estimate_margin_from_fill(filled_qty, avg_price, leverage)
        if filled_qty > 0:
            note_parts.append(f"maker_fill={filled_qty:.8f}")
        else:
            note_parts.append("maker_no_fill")

        remaining_margin = max(0.0, margin - filled_margin)
        if filled_qty > 0 and remaining_margin <= 1e-8:
            return {
                "status": "filled",
                "exec_qty": filled_qty,
                "exec_price": avg_price,
                "margin_used": filled_margin,
                "order_id": ",".join(order_ids),
                "note": " ".join(note_parts),
            }

        return {
            "status": "partial" if filled_qty > 0 else "unfilled",
            "exec_qty": filled_qty,
            "exec_price": avg_price,
            "margin_used": filled_margin,
            "remaining_margin": remaining_margin,
            "order_id": ",".join(order_ids),
            "note": " ".join(note_parts),
        }

    def _try_entry_maker_first(
        self,
        platform: str,
        api_creds: tuple,
        symbol: str,
        direction: str,
        leverage: int,
        margin: float,
        signal_price: float,
        current_price: float,
        tol: float,
        client_oid: str,
        timeout_sec: int,
        maker_levels: int,
    ) -> dict:
        return self._execute_maker_priority_open(
            platform=platform,
            api_creds=api_creds,
            symbol=symbol,
            direction=direction,
            leverage=leverage,
            margin=margin,
            signal_price=signal_price,
            current_price=current_price,
            tol=tol,
            client_oid=client_oid,
            timeout_sec=timeout_sec,
            maker_levels=maker_levels,
        )

    def _reconcile_source_positions(
        self,
        trader_uids: list[str],
        bg_creds: tuple | None,
        bn_creds: tuple | None,
        bg_tol: float,
        bn_tol: float,
    ) -> None:
        trader_keys = [str(item or "").strip() for item in trader_uids or [] if str(item or "").strip()]
        if not trader_keys:
            return

        source_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        for trader_uid in trader_keys:
            for item in db.get_source_position_summaries(trader_uid):
                key = (
                    str(item.get("trader_uid") or "").strip(),
                    _clean_symbol_str(str(item.get("symbol") or "")).upper(),
                    str(item.get("direction") or "").strip().lower(),
                )
                source_map[key] = item

        for platform, api_creds, tol in (
            (self._storage_platform("bitget"), bg_creds, bg_tol),
            (self._storage_platform("binance"), bn_creds, bn_tol),
        ):
            if not api_creds:
                continue

            for position in db.get_active_copy_position_summaries(platform):
                trader_uid = str(position.get("trader_uid") or "").strip()
                if trader_uid not in trader_keys:
                    continue

                symbol = _clean_symbol_str(str(position.get("symbol") or "")).upper()
                direction = str(position.get("direction") or "").strip().lower()
                if not symbol or direction not in ("long", "short"):
                    continue

                source_state = source_map.get((trader_uid, symbol, direction))
                source_open = bool(source_state and _safe_float(source_state.get("remaining_qty"), 0.0) > 1e-12)
                flatten_marker = int(
                    (source_state or {}).get("last_flattened_at")
                    or (source_state or {}).get("last_close_time")
                    or 0
                )

                if source_open or flatten_marker <= 0:
                    self._clear_reconcile_state(platform, trader_uid, symbol, direction)
                    continue

                reconcile_state = self._advance_reconcile_state(platform, trader_uid, symbol, direction, flatten_marker)
                polls = int(reconcile_state.get("polls") or 0)
                first_seen_ms = int(reconcile_state.get("first_seen_ms") or 0)
                if polls <= int(self._reconcile_wait_polls or 0):
                    continue
                if int(self._reconcile_wait_ms or 0) > 0 and (_now_ms() - first_seen_ms) < int(self._reconcile_wait_ms or 0):
                    continue

                tracking_no = f"REC_{trader_uid}_{symbol}_{direction}_{flatten_marker}"
                close_price = _safe_float(source_state.get("price"), 0.0) or _safe_float(source_state.get("avg_entry_price"), 0.0) or _safe_float(position.get("avg_entry_price"), 0.0)
                closed = self._execute_close_for_platform(
                    platform=platform,
                    api_creds=api_creds,
                    pid=trader_uid,
                    order_id=tracking_no,
                    symbol=symbol,
                    direction=direction,
                    price=close_price,
                    order_pnl=0.0,
                    tol=tol,
                    force_close=True,
                    close_reason="reconcile_close",
                )
                if not closed:
                    continue

                state = db.get_copy_position_state(platform, trader_uid, symbol, direction)
                self._save_position_state(
                    platform,
                    trader_uid,
                    symbol,
                    direction,
                    state,
                    last_source_order_id=str(source_state.get("last_source_order_id") or tracking_no),
                    last_system_action="reconcile_close",
                    closed_by_system=1,
                    freeze_reentry=0,
                )
                self._clear_reconcile_state(platform, trader_uid, symbol, direction)

    def _reconcile_missing_local_positions(
        self,
        settings: dict,
        pid: str,
        bg_creds: dict[str, str] | None,
        bn_creds: dict[str, str] | None,
        bg_platform: str,
        bn_platform: str,
        bg_fallback_margin: float,
        bg_tol: float,
        bg_follow_ratio: float,
        bg_dynamic_note: str,
        bg_available_usdt: float,
        bg_allow_open: bool,
        bg_guard_note: str,
        bn_fallback_margin: float,
        bn_tol: float,
        bn_follow_ratio: float,
        bn_dynamic_note: str,
        bn_available_usdt: float,
        bn_allow_open: bool,
        bn_guard_note: str,
    ) -> int:
        now_ms = _now_ms()
        attempted = 0
        reconcile_key = f"{self._profile}:source_open"

        for source_state in db.get_source_position_summaries(pid):
            symbol = _clean_symbol_str(str(source_state.get("symbol") or "")).upper()
            direction = str(source_state.get("direction") or "").strip().lower()
            qty = _safe_float(source_state.get("remaining_qty"), 0.0)
            price = (
                _safe_float(source_state.get("price"), 0.0)
                or _safe_float(source_state.get("avg_entry_price"), 0.0)
                or _safe_float(source_state.get("last_open_price"), 0.0)
            )
            leverage = max(1, _safe_int(source_state.get("leverage"), 1))
            if not symbol or direction not in {"long", "short"} or qty <= 0 or price <= 0:
                continue

            bg_needs_open = False
            if bg_creds:
                bg_symbol = _binance_symbol_to_bitget(symbol)
                bg_needs_open = self._find_active_copy_position(bg_platform, pid, bg_symbol, direction) is None

            bn_needs_open = False
            if bn_creds:
                bn_needs_open = self._find_active_copy_position(bn_platform, pid, symbol, direction) is None

            if not (bg_needs_open or bn_needs_open):
                self._clear_reconcile_state(reconcile_key, pid, symbol, direction)
                continue

            marker = str(
                source_state.get("last_source_order_id")
                or source_state.get("last_open_time")
                or source_state.get("last_event_time")
                or f"{symbol}:{direction}"
            )
            marker_hash = int(hashlib.md5(marker.encode()).hexdigest()[:12], 16)
            state = self._advance_reconcile_state(reconcile_key, pid, symbol, direction, marker_hash)
            polls = int(state.get("polls") or 0)
            first_seen_ms = int(state.get("first_seen_ms") or 0)
            if polls <= int(self._reconcile_wait_polls or 0):
                continue
            if int(self._reconcile_wait_ms or 0) > 0 and (now_ms - first_seen_ms) < int(self._reconcile_wait_ms or 0):
                continue

            synthetic_hash = hashlib.md5(
                f"recopen|{pid}|{symbol}|{direction}|{marker}|{polls}|{qty:.8f}|{price:.8f}".encode()
            ).hexdigest()[:20]
            synthetic_order = {
                "order_id": f"RECOPEN_{synthetic_hash}",
                "symbol": symbol,
                "action": f"open_{direction}",
                "direction": direction,
                "qty": qty,
                "price": price,
                "leverage": leverage,
                "order_time": _safe_int(source_state.get("last_event_time"), now_ms),
                "pnl": 0.0,
                "sync_open": True,
                "sync_bg_enabled": bg_needs_open,
                "sync_bn_enabled": bn_needs_open,
            }
            self._process_binance_order(
                settings,
                bg_creds,
                bn_creds,
                pid,
                synthetic_order,
                bg_fallback_margin=bg_fallback_margin,
                bg_tol=bg_tol,
                bg_follow_ratio=bg_follow_ratio,
                bg_dynamic_note=f"[历史补仓]" if not bg_dynamic_note else f"[历史补仓] {bg_dynamic_note}",
                bg_available_usdt=bg_available_usdt,
                bg_allow_open=bg_allow_open,
                bg_guard_note=bg_guard_note,
                bn_fallback_margin=bn_fallback_margin,
                bn_tol=bn_tol,
                bn_follow_ratio=bn_follow_ratio,
                bn_dynamic_note=f"[历史补仓]" if not bn_dynamic_note else f"[历史补仓] {bn_dynamic_note}",
                bn_available_usdt=bn_available_usdt,
                bn_allow_open=bn_allow_open,
                bn_guard_note=bn_guard_note,
            )
            attempted += 1

        return attempted

    def _process_binance_order(
        self,
        settings: dict,
        bg_creds: dict | None,
        bn_creds: dict | None,
        pid: str,
        order: dict,
        bg_fallback_margin: float,
        bg_tol: float,
        bg_follow_ratio: float,
        bg_available_usdt: float,
        bg_allow_open: bool,
        bg_guard_note: str,
        bn_fallback_margin: float,
        bn_tol: float,
        bn_follow_ratio: float,
        bn_available_usdt: float,
        bn_allow_open: bool,
        bn_guard_note: str,
        bg_dynamic_note: str = "",
        bn_dynamic_note: str = "",
    ) -> None:
        action = str(order.get("action") or "").strip().lower()
        symbol = _clean_symbol_str(str(order.get("symbol") or "")).upper()
        direction = str(order.get("direction") or "").strip().lower()
        price = _safe_float(order.get("price"), 0.0)
        order_time = _safe_int(order.get("order_time"), _now_ms())
        order_id = str(order.get("order_id") or "").strip() or f"{pid}_{order_time}"
        lev = max(1, _safe_int(order.get("leverage"), 1))
        bg_platform = self._storage_platform("bitget")
        bn_platform = self._storage_platform("binance")
        sync_open = bool(order.get("sync_open"))
        sync_bg_enabled = bool(order.get("sync_bg_enabled", True))
        sync_bn_enabled = bool(order.get("sync_bn_enabled", True))

        def _insert_source_skip(platform: str, skip_symbol: str, reason: str) -> None:
            if db.has_tracking_no(pid, order_id, platform=platform):
                return
            _insert_copy_order_and_notify({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": "", "symbol": skip_symbol, "direction": direction or "long",
                "leverage": lev, "margin_usdt": 0.0,
                "source_price": price, "exec_price": 0.0,
                "deviation_pct": 0.0, "action": "open",
                "status": "skipped", "pnl": None,
                "notes": f"[skip] {reason}", "exec_qty": 0.0,
                "platform": platform
            })

        def _skip_open_signal(reason: str, *, cache_symbols: bool = False) -> None:
            if bg_creds and (not sync_open or sync_bg_enabled):
                bg_symbol = _binance_symbol_to_bitget(symbol or "UNKNOWN")
                if cache_symbols:
                    self._cache_unsupported_symbol(bg_platform, bg_symbol)
                _insert_source_skip(bg_platform, bg_symbol, reason)
            if bn_creds and (not sync_open or sync_bn_enabled):
                bn_symbol = symbol or "UNKNOWN"
                if cache_symbols:
                    self._cache_unsupported_symbol(bn_platform, bn_symbol)
                _insert_source_skip(bn_platform, bn_symbol, reason)

        if not action.startswith(("open", "close")):
            logger.warning("[Binance signal] ignore unknown action: pid=%s action=%r symbol=%s", pid[:12], action, symbol)
            return

        if not symbol or direction not in {"long", "short"}:
            reason = f"source signal missing fields action={action or '<empty>'} symbol={symbol or '<empty>'} direction={direction or '<empty>'}"
            logger.warning("[Binance signal] %s", reason)
            if action.startswith("open"):
                _skip_open_signal(reason)
            return

        if action.startswith("open") and price <= 0:
            reason = "source signal missing valid price"
            logger.warning("[Binance signal] %s: pid=%s symbol=%s order_id=%s raw=%s", reason, pid[:12], symbol, order_id, str(order)[:200])
            _skip_open_signal(reason)
            return

        if action.startswith("open") and not _is_reasonable_contract_symbol(symbol):
            reason = f"source symbol format invalid: {symbol}"
            logger.warning("[Binance signal] %s: pid=%s order_id=%s", reason, pid[:12], order_id)
            _skip_open_signal(reason, cache_symbols=True)
            return

        if action.startswith("open"):
            signal_key = f"{pid}:{order_id}"
            with self._state_lock:
                if signal_key in self._bn_inflight:
                    return
                self._bn_inflight.add(signal_key)

            try:
                src_margin = self._estimate_binance_margin(order)
                short_hash = hashlib.md5(f"bn_{pid}_{order_id}".encode()).hexdigest()[:16]
                client_oid = f"bn_{short_hash}"
                opposite_direction = "short" if direction == "long" else "long"

                def _insert_guard_skip(platform: str, skip_symbol: str, reason: str) -> None:
                    if db.has_tracking_no(pid, order_id, platform=platform):
                        return
                    _insert_copy_order_and_notify({
                        "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                        "my_order_id": "", "symbol": skip_symbol, "direction": direction,
                        "leverage": lev, "margin_usdt": 0.0,
                        "source_price": price, "exec_price": 0.0,
                        "deviation_pct": 0.0, "action": "open",
                        "status": "skipped", "pnl": None,
                        "notes": f"[skip] {reason}", "exec_qty": 0.0,
                        "platform": platform
                    })

                def _insert_frozen_skip(platform: str, skip_symbol: str, reason: str, state_snapshot: dict | None) -> None:
                    if db.has_tracking_no(pid, order_id, platform=platform):
                        return
                    _insert_copy_order_and_notify({
                        "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                        "my_order_id": "", "symbol": skip_symbol, "direction": direction,
                        "leverage": lev, "margin_usdt": 0.0,
                        "source_price": price, "exec_price": 0.0,
                        "deviation_pct": 0.0, "action": "open",
                        "status": "skipped", "pnl": None,
                        "notes": f"[System Freeze] {reason}", "exec_qty": 0.0,
                        "platform": platform
                    })
                    self._save_position_state(platform, pid, skip_symbol, direction, state_snapshot, last_source_order_id=order_id)

                if bg_creds and (not sync_open or sync_bg_enabled):
                    bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                    bg_state = db.get_copy_position_state(bg_platform, pid, bg_symbol_mapped, direction)
                    bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                    bg_opposite = self._find_active_copy_position(bg_platform, pid, bg_symbol_mapped, opposite_direction)
                    bg_can_open = True
                    if sync_open and self._find_active_copy_position(bg_platform, pid, bg_symbol_mapped, direction):
                        bg_can_open = False
                    if bg_opposite:
                        reverse_tracking_no = f"REV_{order_id}"
                        bg_reversed = self._execute_close_for_platform(
                            platform=bg_platform,
                            api_creds=(bg_ak, bg_sk, bg_pp),
                            pid=pid,
                            order_id=reverse_tracking_no,
                            symbol=bg_symbol_mapped,
                            direction=opposite_direction,
                            price=price,
                            order_pnl=0.0,
                            tol=bg_tol,
                            force_close=True,
                            close_reason="reverse_reconcile_close",
                        )
                        if bg_reversed:
                            opposite_state = db.get_copy_position_state(bg_platform, pid, bg_symbol_mapped, opposite_direction)
                            self._save_position_state(
                                bg_platform,
                                pid,
                                bg_symbol_mapped,
                                opposite_direction,
                                opposite_state,
                                last_source_order_id=order_id,
                                last_system_action="reverse_reconcile_close",
                                closed_by_system=1,
                                freeze_reentry=0,
                            )
                        else:
                            bg_can_open = False
                            _insert_guard_skip(bg_platform, bg_symbol_mapped, f"reverse close failed ({opposite_direction})")
                    if bg_can_open and bg_state.get("freeze_reentry"):
                        bg_can_open = False
                        _insert_frozen_skip(bg_platform, bg_symbol_mapped, "position is already locked, waiting for trader close", bg_state)
                    if bg_can_open and bg_allow_open:
                        bg_target_margin, bg_ratio_note = self._apply_follow_ratio(src_margin, bg_follow_ratio, bg_fallback_margin)
                        if bg_dynamic_note:
                            bg_ratio_note = f"{bg_ratio_note} {bg_dynamic_note}".strip()
                        self._execute_open_for_platform(
                            platform=bg_platform,
                            api_creds=(bg_ak, bg_sk, bg_pp),
                            pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                            direction=direction, price=price, lev=lev, tol=bg_tol,
                            fallback_margin=bg_fallback_margin, target_margin=bg_target_margin,
                            available_usdt=bg_available_usdt,
                            src_margin=src_margin, ratio_note=bg_ratio_note, client_oid=client_oid, settings=settings
                        )
                    elif bg_can_open and not bg_state.get("freeze_reentry"):
                        _insert_guard_skip(bg_platform, bg_symbol_mapped, bg_guard_note)

                if bn_creds and (not sync_open or sync_bn_enabled):
                    bn_state = db.get_copy_position_state(bn_platform, pid, symbol, direction)
                    bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                    bn_opposite = self._find_active_copy_position(bn_platform, pid, symbol, opposite_direction)
                    bn_can_open = True
                    if sync_open and self._find_active_copy_position(bn_platform, pid, symbol, direction):
                        bn_can_open = False
                    if bn_opposite:
                        reverse_tracking_no = f"REV_{order_id}"
                        bn_reversed = self._execute_close_for_platform(
                            platform=bn_platform,
                            api_creds=(bn_ak, bn_sk, ""),
                            pid=pid,
                            order_id=reverse_tracking_no,
                            symbol=symbol,
                            direction=opposite_direction,
                            price=price,
                            order_pnl=0.0,
                            tol=bn_tol,
                            force_close=True,
                            close_reason="reverse_reconcile_close",
                        )
                        if bn_reversed:
                            opposite_state = db.get_copy_position_state(bn_platform, pid, symbol, opposite_direction)
                            self._save_position_state(
                                bn_platform,
                                pid,
                                symbol,
                                opposite_direction,
                                opposite_state,
                                last_source_order_id=order_id,
                                last_system_action="reverse_reconcile_close",
                                closed_by_system=1,
                                freeze_reentry=0,
                            )
                        else:
                            bn_can_open = False
                            _insert_guard_skip(bn_platform, symbol, f"reverse close failed ({opposite_direction})")
                    if bn_can_open and bn_state.get("freeze_reentry"):
                        bn_can_open = False
                        _insert_frozen_skip(bn_platform, symbol, "position is already locked, waiting for trader close", bn_state)
                    if bn_can_open and bn_allow_open:
                        bn_target_margin, bn_ratio_note = self._apply_follow_ratio(src_margin, bn_follow_ratio, bn_fallback_margin)
                        if bn_dynamic_note:
                            bn_ratio_note = f"{bn_ratio_note} {bn_dynamic_note}".strip()
                        self._execute_open_for_platform(
                            platform=bn_platform,
                            api_creds=(bn_ak, bn_sk, ""),
                            pid=pid, order_id=order_id, symbol=symbol,
                            direction=direction, price=price, lev=lev, tol=bn_tol,
                            fallback_margin=bn_fallback_margin, target_margin=bn_target_margin,
                            available_usdt=bn_available_usdt,
                            src_margin=src_margin, ratio_note=bn_ratio_note, client_oid=client_oid, settings=settings
                        )
                    elif bn_can_open and not bn_state.get("freeze_reentry"):
                        _insert_guard_skip(bn_platform, symbol, bn_guard_note)

            finally:
                with self._state_lock:
                    self._bn_inflight.discard(signal_key)

        elif action.startswith("close"):
            if bg_creds:
                bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                bg_closed = self._execute_close_for_platform(
                    platform=bg_platform,
                    api_creds=(bg_ak, bg_sk, bg_pp),
                    pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bg_tol
                )
                if bg_closed:
                    db.clear_copy_position_state(bg_platform, pid, bg_symbol_mapped, direction)

            if bn_creds:
                bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                bn_closed = self._execute_close_for_platform(
                    platform=bn_platform,
                    api_creds=(bn_ak, bn_sk, ""),
                    pid=pid, order_id=order_id, symbol=symbol,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bn_tol
                )
                if bn_closed:
                    db.clear_copy_position_state(bn_platform, pid, symbol, direction)

    def _execute_open_for_platform(
        self,
        platform: str,
        api_creds: tuple,
        pid: str,
        order_id: str,
        symbol: str,
        direction: str,
        price: float,
        lev: int,
        tol: float,
        fallback_margin: float,
        target_margin: float,
        available_usdt: float,
        src_margin: float,
        ratio_note: str,
        client_oid: str,
        settings: dict,
    ):
        """?????????????? Bitget ? Binance ??"""
        exec_platform = self._exec_platform(platform)
        platform_label = self._platform_label(platform)
        runtime = self._runtime()
        symbol = _clean_symbol_str(symbol or "").upper()
        direction = str(direction or "").strip().lower()
        price = _safe_float(price, 0.0)

        if db.has_tracking_no(pid, order_id, platform=platform):
            dup_key = f"{platform}:{pid}:{order_id}"
            if dup_key not in self._bn_dup_logged:
                logger.info("[%s????] ??????????: pid=%s symbol=%s order_id=%s", platform_label, pid[:12], symbol, order_id)
                self._bn_dup_logged.add(dup_key)
            return

        margin, margin_note = self._cap_open_margin(
            target_margin,
            fallback_margin=fallback_margin,
            available_usdt=available_usdt,
            source_tag=platform_label,
            symbol=symbol,
            direction=direction,
        )
        precheck_note = ""
        entry_settings = self._get_entry_execution_settings(settings)

        def _insert_skip(reason: str, exec_p: float = 0.0, dev: float = 0.0, extra_note: str = ""):
            notes = f"[跳过] {reason} {ratio_note} {margin_note} {precheck_note} {extra_note}".strip()
            _insert_copy_order_and_notify({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": margin,
                "source_price": price, "exec_price": exec_p,
                "deviation_pct": dev, "action": "open",
                "status": "skipped", "pnl": None,
                "notes": notes, "exec_qty": 0.0,
                "platform": platform
            })

        def _insert_filled(exec_price: float, exec_qty: float, extra_note: str = "", oid: str = "", used_margin: float | None = None, deviation_override: float | None = None):
            final_margin = used_margin if used_margin is not None and used_margin > 0 else margin
            final_dev = deviation_override if deviation_override is not None else dev
            notes = f"[{platform_label} Signal] src={src_margin:.4f} {ratio_note} {margin_note} {precheck_note} {extra_note}".strip()
            _insert_copy_order_and_notify({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": str(oid or ""), "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": final_margin,
                "source_price": price, "exec_price": exec_price,
                "deviation_pct": final_dev, "action": "open",
                "status": "filled", "pnl": None, "notes": notes,
                "exec_qty": exec_qty,
                "platform": platform
            })
            self._save_position_state(
                platform, pid, symbol, direction, None,
                last_source_order_id=order_id,
                closed_by_system=0,
                freeze_reentry=0,
                last_system_action="",
            )
            logger.info("[%s????] %s %s ?=%.4f ?=%s", platform_label, symbol, direction.upper(), exec_price, exec_qty)

        def _recover_binance_timeout_open(exc: Exception) -> bool:
            if exec_platform != "binance" or not client_oid or not _is_request_timeout_error(exc):
                return False

            import binance_executor

            fallback_exec_price = _safe_float(locals().get("curr_p", price), price)
            recovered_orders: list[dict[str, Any]] = []
            probe_ak, probe_sk, _ = api_creds
            probe_client_oids = [oid for oid in (f"{client_oid}_L", f"{client_oid}_M", client_oid) if oid]

            with binance_executor.use_runtime(base_url=runtime["binance_base_url"]):
                for probe_oid in probe_client_oids:
                    try:
                        detail = binance_executor.get_order(probe_ak, probe_sk, symbol, client_oid=probe_oid)
                    except Exception as probe_exc:
                        if _is_binance_order_missing_error(probe_exc):
                            continue
                        logger.warning("[%s timeout reconcile] %s %s probe=%s: %s", platform_label, symbol, direction.upper(), probe_oid, probe_exc)
                        continue

                    snapshot = _parse_binance_order_snapshot(detail, fallback_price=fallback_exec_price)
                    filled_qty = max(_safe_float(snapshot.get("filled_qty"), 0.0), 0.0)
                    status = str(snapshot.get("status") or "").upper()
                    if filled_qty <= 0 and status != "FILLED":
                        continue
                    recovered_orders.append(snapshot)

            if not recovered_orders:
                return False

            total_qty = sum(max(_safe_float(item.get("filled_qty"), 0.0), 0.0) for item in recovered_orders)
            if total_qty <= 0:
                return False

            weighted_cost = 0.0
            order_ids: list[str] = []
            recovered_client_oids: list[str] = []
            for item in recovered_orders:
                filled_qty = max(_safe_float(item.get("filled_qty"), 0.0), 0.0)
                avg_price = _safe_float(item.get("avg_price"), fallback_exec_price) or fallback_exec_price
                weighted_cost += filled_qty * avg_price
                order_id_value = str(item.get("order_id") or "").strip()
                client_oid_value = str(item.get("client_oid") or "").strip()
                if order_id_value:
                    order_ids.append(order_id_value)
                if client_oid_value:
                    recovered_client_oids.append(client_oid_value)

            avg_exec_price = weighted_cost / total_qty if total_qty > 0 else fallback_exec_price
            recovered_margin = _estimate_margin_from_fill(total_qty, avg_exec_price, lev)
            recover_note = f"[RecoveredAfterTimeout] clientOid={','.join(recovered_client_oids)}".strip()
            _insert_filled(avg_exec_price, total_qty, recover_note, oid=','.join(order_ids), used_margin=recovered_margin or margin)
            logger.warning("[%s timeout reconcile] recovered %s %s qty=%s price=%.4f", platform_label, symbol, direction.upper(), total_qty, avg_exec_price)
            return True

        if exec_platform == "bitget" and symbol in self._unsupported_symbols:
            _insert_skip("Bitget 交易对不可用(已缓存)")
            return

        if not symbol or direction not in {"long", "short"}:
            logger.warning("[%s open] invalid signal fields: symbol=%r direction=%r", platform_label, symbol, direction)
            return

        if price <= 0:
            _insert_skip("source signal missing valid price")
            return

        precheck_reason = self._precheck_open_symbol(platform, symbol)
        if precheck_reason:
            _insert_skip(precheck_reason)
            return

        if margin <= 0:
            _insert_skip("calculated open margin is zero or negative", exec_p=price, dev=0.0)
            logger.warning(
                "[%s????] ???????: %s %s price=%s lev=%s",
                platform_label, symbol, direction.upper(), price, lev
            )
            return

        runtime_ctx = order_executor.use_runtime(simulated=runtime["bitget_simulated"])
        if exec_platform == "binance":
            import binance_executor
            runtime_ctx = binance_executor.use_runtime(base_url=runtime["binance_base_url"])

        ak, sk, pp = api_creds
        try:
            with runtime_ctx:
                if exec_platform == "binance":
                    import binance_executor
                    curr_p = binance_executor.get_ticker_price(symbol)
                    dev = abs(curr_p - price) / price if price > 0 else 1.0
                    ok = dev <= tol
                else:
                    ok, curr_p, dev = _price_ok(symbol, price, tol)

                if not ok:
                    logger.warning("[%s??????] %s %s ??=%.4f ??=%.4f ??=%.2f%%",
                                   platform_label, symbol, direction.upper(), price, curr_p, dev * 100)
                    _insert_skip(
                        f"price drift too large src={price:.4f} now={curr_p:.4f} dev={dev * 100:.2f}%",
                        exec_p=curr_p,
                        dev=dev,
                    )
                    return

                cap_limit = _cap_limit_value(fallback_margin, available_usdt)
                if exec_platform == "binance":
                    import binance_executor
                    req = binance_executor.get_min_order_requirements(symbol, lev, curr_p)
                    required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                    if required_margin > 0 and margin + 1e-12 < required_margin:
                        if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                            _insert_skip(f"Binance 最小开仓金额不足 need={required_margin:.4f} cap={cap_limit:.4f}")
                            return
                        precheck_note = f"[最小下单修正] target={margin:.4f} min={required_margin:.4f}"
                        logger.info("[Binance??????] %s %s %.4f -> %.4f", symbol, direction.upper(), margin, required_margin)
                        margin = required_margin
                else:
                    req = order_executor.get_min_order_requirements(symbol, lev, curr_p)
                    symbol_status = str(req.get("symbolStatus") or "").lower()
                    if symbol_status and symbol_status != "normal":
                        self._unsupported_symbols.add(symbol)
                        _insert_skip(f"Bitget 交易对不可开仓 status={symbol_status}")
                        return
                    limit_open_time = str(req.get("limitOpenTime") or "-1")
                    if limit_open_time not in ("", "-1"):
                        _insert_skip(f"Bitget 限制开仓 limitOpenTime={limit_open_time}")
                        return
                    required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                    if required_margin > 0 and margin + 1e-12 < required_margin:
                        if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                            _insert_skip(f"Bitget 最小开仓金额不足 need={required_margin:.4f} cap={cap_limit:.4f}")
                            return
                        precheck_note = f"[最小下单修正] target={margin:.4f} min={required_margin:.4f}"
                        logger.info("[Bitget??????] %s %s %.4f -> %.4f", symbol, direction.upper(), margin, required_margin)
                        margin = required_margin

                if entry_settings["mode"] == "market":
                    if exec_platform == "binance":
                        import binance_executor
                        res = binance_executor.place_market_order(
                            ak, sk, symbol, direction, lev, "ISOLATED", margin,
                            current_price=curr_p, client_oid=client_oid
                        )
                    else:
                        res = order_executor.place_market_order(
                            ak, sk, pp, symbol, direction, lev,
                            "isolated", margin, pos_mode=self._pos_mode, client_oid=client_oid,
                            current_price=curr_p
                        )
                    oid = res.get("orderId") or res.get("clientId") or res.get("clientOid") or ""
                    exec_qty = float(res.get("_calculated_size", 0) if isinstance(res, dict) else 0)
                    _insert_filled(curr_p, exec_qty, "[EntryMode] market", oid=str(oid))
                    return

                managed = self._execute_maker_priority_open(
                    platform=platform,
                    api_creds=api_creds,
                    symbol=symbol,
                    direction=direction,
                    leverage=lev,
                    margin=margin,
                    signal_price=price,
                    current_price=curr_p,
                    tol=tol,
                    client_oid=client_oid,
                    timeout_sec=entry_settings["timeout_sec"],
                    maker_levels=entry_settings["maker_levels"],
                )
                maker_status = managed.get("status")
                maker_note = str(managed.get("note") or "")
                maker_qty = max(_safe_float(managed.get("exec_qty"), 0.0), 0.0)
                maker_price = _safe_float(managed.get("exec_price"), 0.0) or curr_p
                maker_margin = max(_safe_float(managed.get("margin_used"), 0.0), 0.0)
                maker_oid = str(managed.get("order_id") or "")
                remaining_margin = max(_safe_float(managed.get("remaining_margin"), 0.0), 0.0)

                if maker_status == "filled":
                    _insert_filled(maker_price, maker_qty, maker_note, oid=maker_oid, used_margin=maker_margin or margin)
                    return

                if maker_status == "skipped":
                    _insert_skip(str(managed.get("reason") or "Maker 下单跳过"), exec_p=curr_p, dev=dev, extra_note=maker_note)
                    return

                if maker_status in {"partial", "unfilled"} and entry_settings["fallback_to_market"] and remaining_margin > 1e-8:
                    if exec_platform == "binance":
                        import binance_executor
                        fallback_price = binance_executor.get_ticker_price(symbol)
                    else:
                        fallback_price = get_ticker_price(symbol)
                    fallback_dev = abs(fallback_price - price) / price if price > 0 else 1.0
                    if fallback_dev <= tol:
                        fallback_client_oid = f"{client_oid}_M" if client_oid else ""
                        if exec_platform == "binance":
                            import binance_executor
                            mres = binance_executor.place_market_order(
                                ak, sk, symbol, direction, lev, "ISOLATED", remaining_margin,
                                current_price=fallback_price, client_oid=fallback_client_oid,
                            )
                        else:
                            mres = order_executor.place_market_order(
                                ak, sk, pp, symbol, direction, lev,
                                "isolated", remaining_margin, pos_mode=self._pos_mode,
                                client_oid=fallback_client_oid, current_price=fallback_price,
                            )
                        market_qty = float(mres.get("_calculated_size", 0) if isinstance(mres, dict) else 0)
                        market_oid = str(mres.get("orderId") or mres.get("clientId") or mres.get("clientOid") or "")
                        total_qty = maker_qty + market_qty
                        total_margin = maker_margin + _estimate_margin_from_fill(market_qty, fallback_price, lev)
                        if total_qty <= 0:
                            _insert_skip("Maker 回退后成交数量为 0", exec_p=fallback_price, dev=fallback_dev, extra_note=maker_note)
                            return
                        weighted_cost = (maker_qty * maker_price) + (market_qty * fallback_price)
                        avg_exec_price = weighted_cost / total_qty if total_qty > 0 else fallback_price
                        oid_join = ",".join(x for x in (maker_oid, market_oid) if x)
                        extra_note = f"{maker_note} [FallbackMarket] remain={remaining_margin:.4f}".strip()
                        _insert_filled(
                            avg_exec_price,
                            total_qty,
                            extra_note,
                            oid=oid_join,
                            used_margin=total_margin or margin,
                            deviation_override=max(dev, fallback_dev),
                        )
                        return

                    if maker_qty > 0:
                        extra_note = f"{maker_note} [FallbackSkipped] deviation={fallback_dev * 100:.2f}%".strip()
                        _insert_filled(
                            maker_price,
                            maker_qty,
                            extra_note,
                            oid=maker_oid,
                            used_margin=maker_margin or margin,
                            deviation_override=max(dev, fallback_dev),
                        )
                        return

                    _insert_skip(
                        f"Maker 回退市价价差过大 {fallback_dev * 100:.2f}%",
                        exec_p=fallback_price,
                        dev=fallback_dev,
                        extra_note=maker_note,
                    )
                    return

                if maker_qty > 0:
                    _insert_filled(maker_price, maker_qty, maker_note, oid=maker_oid, used_margin=maker_margin or margin)
                    return

                _insert_skip("Maker 未成交", exec_p=curr_p, dev=dev, extra_note=maker_note)
        except Exception as exc:
            if exec_platform == "bitget" and _is_symbol_not_exist_error(exc):
                self._cache_unsupported_symbol(platform, symbol)
                _insert_skip(f"Bitget 开仓跳过: {exc}")
                return
            if exec_platform == "bitget" and (_is_bitget_min_trade_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Bitget 开仓跳过: {exc}", curr_p, dev)
                return
            if exec_platform == "bitget" and _is_bitget_balance_error(exc):
                _insert_skip(f"Bitget 保证金不足: {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and _is_binance_symbol_error(exc):
                self._cache_unsupported_symbol(platform, symbol)
                _insert_skip(f"Binance 寮€浠撹烦杩? {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and (_is_binance_min_notional_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Binance 开仓跳过: {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and _is_binance_balance_error(exc):
                _insert_skip(f"Binance 保证金不足: {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and _recover_binance_timeout_open(exc):
                return
            _insert_copy_order_and_notify({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": f"FAIL_{order_id}",
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": margin,
                "source_price": price, "exec_price": locals().get("curr_p", price),
                "deviation_pct": locals().get("dev", 0.0), "action": "open",
                "status": "failed", "pnl": None,
                "notes": f"{exc} | {ratio_note} {margin_note} {precheck_note}".strip(), "exec_qty": 0.0,
                "platform": platform
            })
            logger.error("[%s????] %s: %s", platform_label, symbol, exc)


    def _execute_close_for_platform(
        self, platform: str, api_creds: tuple, pid: str, order_id: str, symbol: str,
        direction: str, price: float, order_pnl: float | None, tol: float,
        force_close: bool = True, close_reason: str = "signal_close",
    ) -> bool:
        exec_platform = self._exec_platform(platform)
        platform_label = self._platform_label(platform)
        runtime = self._runtime()
        if exec_platform == "bitget" and symbol in self._unsupported_symbols:
            return False

        if db.has_tracking_no(pid, order_id, platform=platform):
            return True

        from database import get_conn
        with get_conn() as conn:
            opened_sum = conn.execute(
                """
                SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                  AND action = 'open' AND status = 'filled' AND platform = ?
                """,
                (pid, symbol, direction, platform),
            ).fetchone()[0]
            closed_sum = conn.execute(
                """
                SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                  AND action = 'close' AND status = 'filled' AND platform = ?
                """,
                (pid, symbol, direction, platform),
            ).fetchone()[0]

        remaining_qty = float(opened_sum) - float(closed_sum)
        if remaining_qty <= 0:
            self._insert_close_order(
                platform=platform,
                trader_uid=pid,
                tracking_no=order_id,
                symbol=symbol,
                direction=direction,
                source_price=price,
                exec_price=price,
                pnl=None,
                exec_qty=0.0,
                status="skipped",
                notes=(
                    f"[{platform_label} Signal] source close ignored: "
                    f"no remaining local position (opened={float(opened_sum):.8f}, closed={float(closed_sum):.8f})"
                ),
            )
            logger.debug("[%s close] no remaining local position for %s (pid=%s)", platform_label, symbol, pid[:8])
            return True

        close_qty = remaining_qty
        if close_qty <= 0:
            return True

        ak, sk, pp = api_creds
        runtime_ctx = order_executor.use_runtime(simulated=runtime["bitget_simulated"])
        if exec_platform == "binance":
            import binance_executor
            runtime_ctx = binance_executor.use_runtime(base_url=runtime["binance_base_url"])

        qty_str = ""
        close_note_parts: list[str] = []
        try:
            with runtime_ctx:
                if exec_platform == "binance":
                    import binance_executor
                    curr_p = binance_executor.get_ticker_price(symbol)
                    dev = abs(curr_p - price) / price if price > 0 else 1.0
                    ok = dev <= tol
                else:
                    ok, curr_p, dev = _price_ok(symbol, price, tol)
                    if force_close and (curr_p <= 0 or not ok):
                        try:
                            curr_p = get_ticker_price(symbol)
                        except Exception:
                            pass

                if not ok:
                    logger.warning(
                        "[%s close] %s %s price mismatch src=%.4f now=%.4f dev=%.2f%% > %.2f%%",
                        platform_label, symbol, direction.upper(), price, curr_p, dev * 100, tol * 100,
                    )
                    if not force_close:
                        return False
                    close_note_parts.append(
                        f"price drift src={price:.4f} now={curr_p:.4f} dev={dev * 100:.2f}%"
                    )

                if exec_platform == "binance":
                    filters = binance_executor.get_symbol_filters(symbol)
                    qty_str = binance_executor._format_qty(close_qty, filters["stepSize"])
                    if float(qty_str) <= 0:
                        logger.warning("[%s close] qty rounded to zero: %s", platform_label, close_qty)
                        return False
                    binance_executor.close_partial_position(ak, sk, symbol, direction, qty_str)
                else:
                    qty_value = _trunc4(close_qty)
                    qty_str = f"{qty_value:.4f}".rstrip("0").rstrip(".")
                    if not qty_str or float(qty_str) <= 0:
                        logger.warning("[%s close] qty rounded to zero: %s", platform_label, close_qty)
                        return False
                    order_executor.close_partial_position(
                        ak, sk, pp, symbol, direction, qty_str,
                        pos_mode=self._pos_mode, margin_mode="isolated",
                    )

            success_note = f"[{platform_label} Signal] Close"
            if close_reason and close_reason != "signal_close":
                success_note = f"{success_note} [{close_reason}]"
            if close_note_parts:
                success_note = f"{success_note} | {' | '.join(close_note_parts)}"
            self._insert_close_order(
                platform=platform,
                trader_uid=pid,
                tracking_no=order_id,
                symbol=symbol,
                direction=direction,
                source_price=price,
                exec_price=curr_p if curr_p > 0 else price,
                pnl=order_pnl,
                exec_qty=float(qty_str),
                status="filled",
                notes=success_note,
            )
            logger.info("[%s close] %s %s qty=%s", platform_label, symbol, direction.upper(), qty_str)
            return True
        except Exception as exc:
            is_missing = (exec_platform == "bitget" and _is_bitget_position_missing_error(exc)) or (
                exec_platform == "binance" and _is_binance_position_missing_error(exc)
            )
            if is_missing:
                live_qty = None
                try:
                    live_qty = self._get_exchange_position_qty(platform, api_creds, symbol, direction)
                except Exception as lookup_exc:
                    logger.info(
                        "[%s close reconcile] %s %s lookup failed: %s",
                        platform_label, symbol, direction.upper(), lookup_exc,
                    )
                extra_note = ""
                if close_reason and close_reason != "signal_close":
                    extra_note = f"close_reason={close_reason}"
                self._record_exchange_flat_close(
                    platform=platform,
                    trader_uid=pid,
                    tracking_no=order_id,
                    symbol=symbol,
                    direction=direction,
                    source_price=price,
                    exec_price=price,
                    pnl=order_pnl,
                    exec_qty=float(close_qty),
                    base_note=f"[{platform_label} Reconcile] exchange already flat on close signal",
                    live_qty=live_qty,
                    extra_note=extra_note,
                )
                logger.info(
                    "[%s close reconcile] %s %s already flat on exchange, qty=%.8f",
                    platform_label, symbol, direction.upper(), close_qty,
                )
                return True

            self._insert_close_order(
                platform=platform,
                trader_uid=pid,
                tracking_no=f"FAIL_{order_id}",
                symbol=symbol,
                direction=direction,
                source_price=price,
                exec_price=price,
                pnl=0.0,
                exec_qty=0.0,
                status="failed",
                notes=str(exc),
            )
            logger.error("[%s close] %s: %s", platform_label, symbol, exc)
            return False



def start_engine(profile: str | None = "sim") -> None:
    profile_key = _normalize_profile(profile)
    engine = _ENGINES.get(profile_key)
    if engine is None:
        engine = CopyEngine(profile=profile_key)
        _ENGINES[profile_key] = engine
    engine.start()


def stop_engine(profile: str | None = "sim") -> None:
    profile_key = _normalize_profile(profile)
    engine = _ENGINES.get(profile_key)
    if engine is not None:
        engine.stop()


def is_engine_running(profile: str | None = None) -> bool:
    if profile is None:
        return any(engine.is_running() for engine in _ENGINES.values())
    profile_key = _normalize_profile(profile)
    engine = _ENGINES.get(profile_key)
    return bool(engine and engine.is_running())


def get_engine_diagnostics(profile: str | None = None) -> dict[str, Any]:
    profile_key = _normalize_profile(profile)
    engine = _ENGINES.get(profile_key)
    if engine is None:
        return {"binance_traders": {}}
    return {"binance_traders": engine.get_binance_trader_diagnostics()}
