"""
copy_engine.py — 自动跟单引擎 (仅币安信号源 → Bitget 下单)

通过监控币安交易员的操作记录，自动在 Bitget 执行对应的开仓/平仓操作。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

import requests

import config
import database as db
import order_executor
import binance_scraper

logger = logging.getLogger(__name__)

_ENGINES: dict[str, "CopyEngine"] = {}


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
        self._last_bn_metadata_refresh = 0
        self._state_lock = threading.RLock()
        self._unsupported_symbols: set[str] = set()
        self._bn_inflight: set[str] = set()
        self._bn_dup_logged: set[str] = set()
        self._risk_pause_logged: set[str] = set()

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            if self._bn_thread and self._bn_thread.is_alive():
                logger.warning("检测到旧币安线程仍在运行，跳过重复启动")
                return

            # 防重启信号重放：将所有已知币安交易员的起始时间戳设为当前时刻前 2 小时 (允许补票)
            self._bn_seen = {pid: int((time.time() - 7200) * 1000) for pid in self._bn_seen}
            self._bn_dup_logged.clear()
            self._running = True
            # 启动币安监控线程
            self._bn_thread = threading.Thread(target=self._run_binance, daemon=True)
            self._bn_thread.start()
            logger.info("跟单引擎启动 (币安信号源模式)")

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
        with self._state_lock:
            return self._running

    def _runtime(self) -> dict[str, Any]:
        return {
            "bitget_simulated": _profile_bitget_simulated(self._profile),
            "binance_base_url": _profile_binance_base_url(self._profile),
        }

    def _storage_platform(self, platform: str) -> str:
        return _profile_storage_platform(self._profile, platform)

    def _exec_platform(self, platform: str) -> str:
        return _profile_exec_platform(platform)

    def _platform_label(self, platform: str) -> str:
        exec_platform = self._exec_platform(platform)
        if self._profile == "live":
            return f"Live {exec_platform.capitalize()}"
        return exec_platform.capitalize()

    # ── 币安信号源监控 ────────────────────────────────────────────────────────

    def _run_binance(self) -> None:
        """独立线程：轮询所有币安交易员的操作记录，发现新信号就在 Bitget 下单。"""
        while self._running:
            try:
                self._loop_binance_once()
            except Exception as exc:
                logger.error("Binance loop error: %s", exc, exc_info=True)
            time.sleep(3)

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
        bg_tol = _safe_float(settings.get("price_tolerance"), 0.05)
        bg_follow_ratio = self._resolve_follow_ratio(settings)
        bg_fallback_margin = 0.0
        bg_total_p = _safe_float(settings.get("total_capital"), 0.0)
        bg_max_m = _safe_float(settings.get("max_margin_pct"), 0.20)
        if bg_total_p > 0 and bg_max_m > 0:
            bg_fallback_margin = (bg_total_p / total_trader_count) * bg_max_m

        # 2. 币安独立参数
        bn_tol = _safe_float(settings.get("binance_price_tolerance"), 0.05)
        
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
                # 首次遇到新 pid 时，初始化为 2 小时前 (允许补票上车)
                if pid not in self._bn_seen:
                    self._bn_seen[pid] = now_ms - 7200000
                    logger.info("币安交易员 %s 首次初始化信号时间戳 (回溯 2 小时)", pid[:12])

                since_ms = self._bn_seen[pid]
                new_orders = binance_scraper.fetch_latest_orders(pid, since_ms=since_ms, limit=20)
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
                        bg_creds, bn_creds, pid, order,
                        bg_fallback_margin=bg_fallback_margin, bg_tol=bg_tol, bg_follow_ratio=bg_follow_ratio, bg_available_usdt=bg_available_usdt,
                        bg_allow_open=bg_allow_open, bg_guard_note=bg_guard_note,
                        bn_fallback_margin=bn_fallback_margin, bn_tol=bn_tol, bn_follow_ratio=bn_follow_ratio, bn_available_usdt=bn_available_usdt,
                        bn_allow_open=bn_allow_open, bn_guard_note=bn_guard_note
                    )
                    # 更新已处理的最新时间戳
                    if order["order_time"] > self._bn_seen[pid]:
                        self._bn_seen[pid] = order["order_time"]
            except Exception as e:
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
            db.update_copy_settings_profile(self._profile, binance_traders=json.dumps(current_data))
            logger.info("币安交易员元数据已更新到数据库")

    def _resolve_follow_ratio(self, settings: dict) -> float:
        """
        读取全局跟随比例（0~1）。
        兼容历史值：若误传百分数（>1），按百分比换算。
        """
        ratio = _safe_float(settings.get("follow_ratio_pct"), 0.003)
        if ratio > 1:
            ratio = ratio / 100.0
        return min(max(ratio, 0.0), 1.0)

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

            db.insert_copy_order({
                "timestamp": _now_ms(),
                "trader_uid": position["trader_uid"],
                "tracking_no": tracking_no,
                "my_order_id": "",
                "symbol": position["symbol"],
                "direction": position["direction"],
                "leverage": 0,
                "margin_usdt": 0,
                "source_price": current_price,
                "exec_price": current_price,
                "deviation_pct": 0,
                "action": "close",
                "status": "filled",
                "pnl": pnl_share,
                "notes": f"[{label}] {note}".strip(),
                "exec_qty": float(qty_str),
                "platform": platform,
            })
            logger.info("[%s managed close] %s %s qty=%s note=%s", platform_label, position["symbol"], position["direction"].upper(), qty_str, label)
            return True
        except Exception as exc:
            db.insert_copy_order({
                "timestamp": _now_ms(),
                "trader_uid": position["trader_uid"],
                "tracking_no": f"FAIL_{tracking_no}",
                "my_order_id": "",
                "symbol": position["symbol"],
                "direction": position["direction"],
                "leverage": 0,
                "margin_usdt": 0,
                "source_price": current_price,
                "exec_price": current_price,
                "deviation_pct": 0,
                "action": "close",
                "status": "failed",
                "pnl": 0.0,
                "notes": f"[{label}] {exc} | {note}".strip(),
                "exec_qty": 0.0,
                "platform": platform,
            })
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
                    return {"status": "skipped", "reason": "???? Binance ????", "note": "[MakerPriority] invalid_limit_price"}

                limit_client_oid = f"{client_oid}_L" if client_oid else f"bn_limit_{int(time.time() * 1000)}"
                limit_res = binance_executor.place_limit_order(
                    ak, sk, symbol, direction, leverage, "ISOLATED", margin,
                    limit_price=limit_price, client_oid=limit_client_oid, post_only=True,
                )
                order_id = str(limit_res.get("orderId") or "")
                if order_id:
                    order_ids.append(order_id)
                note_parts.append(f"[MakerPriority] limit={limit_price:.8f} wait={timeout_sec}s")
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
                return {"status": "skipped", "reason": "???? Bitget ????", "note": "[MakerPriority] invalid_limit_price"}

            limit_client_oid = f"{client_oid}_L" if client_oid else f"bg_limit_{int(time.time() * 1000)}"
            limit_res = order_executor.place_limit_order(
                ak, sk, pp, symbol, direction, leverage,
                "isolated", margin, limit_price=limit_price,
                pos_mode=self._pos_mode, client_oid=limit_client_oid,
            )
            order_id = str(limit_res.get("orderId") or limit_res.get("clientOid") or "")
            if order_id:
                order_ids.append(order_id)
            note_parts.append(f"[MakerPriority] limit={limit_price:.8f} wait={timeout_sec}s")
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

    def _process_binance_order(
        self,
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
    ) -> None:
        action = order["action"]
        symbol = order.get("symbol", "")
        direction = order["direction"]
        price = order["price"]
        order_id = order["order_id"] or f"{pid}_{order['order_time']}"
        bg_platform = self._storage_platform("bitget")
        bn_platform = self._storage_platform("binance")

        if action.startswith("open"):
            signal_key = f"{pid}:{order_id}"
            with self._state_lock:
                if signal_key in self._bn_inflight:
                    return
                self._bn_inflight.add(signal_key)

            try:
                lev = max(1, int(order.get("leverage") or 1))
                src_margin = self._estimate_binance_margin(order)
                short_hash = hashlib.md5(f"bn_{pid}_{order_id}".encode()).hexdigest()[:16]
                client_oid = f"bn_{short_hash}"

                def _insert_guard_skip(platform: str, skip_symbol: str, reason: str) -> None:
                    if db.has_tracking_no(pid, order_id, platform=platform):
                        return
                    db.insert_copy_order({
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
                    db.insert_copy_order({
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

                if bg_creds:
                    bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                    bg_state = db.get_copy_position_state(bg_platform, pid, bg_symbol_mapped, direction)
                    bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                    if bg_state.get("freeze_reentry"):
                        _insert_frozen_skip(bg_platform, bg_symbol_mapped, "position is already locked, waiting for trader close", bg_state)
                    elif bg_allow_open:
                        bg_target_margin, bg_ratio_note = self._apply_follow_ratio(src_margin, bg_follow_ratio, bg_fallback_margin)
                        self._execute_open_for_platform(
                            platform=bg_platform,
                            api_creds=(bg_ak, bg_sk, bg_pp),
                            pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                            direction=direction, price=price, lev=lev, tol=bg_tol,
                            fallback_margin=bg_fallback_margin, target_margin=bg_target_margin,
                            available_usdt=bg_available_usdt,
                            src_margin=src_margin, ratio_note=bg_ratio_note, client_oid=client_oid, settings=settings
                        )
                    else:
                        _insert_guard_skip(bg_platform, bg_symbol_mapped, bg_guard_note)

                if bn_creds:
                    bn_state = db.get_copy_position_state(bn_platform, pid, symbol, direction)
                    bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                    if bn_state.get("freeze_reentry"):
                        _insert_frozen_skip(bn_platform, symbol, "position is already locked, waiting for trader close", bn_state)
                    elif bn_allow_open:
                        bn_target_margin, bn_ratio_note = self._apply_follow_ratio(src_margin, bn_follow_ratio, bn_fallback_margin)
                        self._execute_open_for_platform(
                            platform=bn_platform,
                            api_creds=(bn_ak, bn_sk, ""),
                            pid=pid, order_id=order_id, symbol=symbol,
                            direction=direction, price=price, lev=lev, tol=bn_tol,
                            fallback_margin=bn_fallback_margin, target_margin=bn_target_margin,
                            available_usdt=bn_available_usdt,
                            src_margin=src_margin, ratio_note=bn_ratio_note, client_oid=client_oid, settings=settings
                        )
                    else:
                        _insert_guard_skip(bn_platform, symbol, bn_guard_note)

            finally:
                with self._state_lock:
                    self._bn_inflight.discard(signal_key)

        elif action.startswith("close"):
            if bg_creds:
                bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                self._execute_close_for_platform(
                    platform=bg_platform,
                    api_creds=(bg_ak, bg_sk, bg_pp),
                    pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bg_tol
                )
                db.clear_copy_position_state(bg_platform, pid, bg_symbol_mapped, direction)

            if bn_creds:
                bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                self._execute_close_for_platform(
                    platform=bn_platform,
                    api_creds=(bn_ak, bn_sk, ""),
                    pid=pid, order_id=order_id, symbol=symbol,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bn_tol
                )
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
            notes = f"[??] {reason} {ratio_note} {margin_note} {precheck_note} {extra_note}".strip()
            db.insert_copy_order({
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
            db.insert_copy_order({
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

        if exec_platform == "bitget" and symbol in self._unsupported_symbols:
            _insert_skip("Bitget ???????(??)")
            return

        if margin <= 0:
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
                    return

                cap_limit = _cap_limit_value(fallback_margin, available_usdt)
                if exec_platform == "binance":
                    import binance_executor
                    req = binance_executor.get_min_order_requirements(symbol, lev, curr_p)
                    required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                    if required_margin > 0 and margin + 1e-12 < required_margin:
                        if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                            _insert_skip(f"Binance ?????? need={required_margin:.4f} cap={cap_limit:.4f}")
                            return
                        precheck_note = f"[??????] target={margin:.4f} min={required_margin:.4f}"
                        logger.info("[Binance??????] %s %s %.4f -> %.4f", symbol, direction.upper(), margin, required_margin)
                        margin = required_margin
                else:
                    req = order_executor.get_min_order_requirements(symbol, lev, curr_p)
                    symbol_status = str(req.get("symbolStatus") or "").lower()
                    if symbol_status and symbol_status != "normal":
                        self._unsupported_symbols.add(symbol)
                        _insert_skip(f"Bitget ?????? status={symbol_status}")
                        return
                    limit_open_time = str(req.get("limitOpenTime") or "-1")
                    if limit_open_time not in ("", "-1"):
                        _insert_skip(f"Bitget ?????? limitOpenTime={limit_open_time}")
                        return
                    required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                    if required_margin > 0 and margin + 1e-12 < required_margin:
                        if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                            _insert_skip(f"Bitget ?????? need={required_margin:.4f} cap={cap_limit:.4f}")
                            return
                        precheck_note = f"[??????] target={margin:.4f} min={required_margin:.4f}"
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
                    _insert_skip(str(managed.get("reason") or "Maker ?????"), exec_p=curr_p, dev=dev, extra_note=maker_note)
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
                            _insert_skip("Maker ???????????", exec_p=fallback_price, dev=fallback_dev, extra_note=maker_note)
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
                        f"Maker ????????????? {fallback_dev * 100:.2f}%",
                        exec_p=fallback_price,
                        dev=fallback_dev,
                        extra_note=maker_note,
                    )
                    return

                if maker_qty > 0:
                    _insert_filled(maker_price, maker_qty, maker_note, oid=maker_oid, used_margin=maker_margin or margin)
                    return

                _insert_skip("Maker ???????", exec_p=curr_p, dev=dev, extra_note=maker_note)
        except Exception as exc:
            if exec_platform == "bitget" and _is_symbol_not_exist_error(exc):
                self._unsupported_symbols.add(symbol)
                _insert_skip(f"Bitget ??????: {exc}")
                return
            if exec_platform == "bitget" and (_is_bitget_min_trade_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Bitget ??????: {exc}", curr_p, dev)
                return
            if exec_platform == "bitget" and _is_bitget_balance_error(exc):
                _insert_skip(f"Bitget ????: {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and (_is_binance_min_notional_error(exc) or _is_binance_symbol_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Binance ??????: {exc}", curr_p, dev)
                return
            if exec_platform == "binance" and _is_binance_balance_error(exc):
                _insert_skip(f"Binance ????: {exc}", curr_p, dev)
                return
            db.insert_copy_order({
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
        direction: str, price: float, order_pnl: float | None, tol: float
    ):
        """???????????????"""
        exec_platform = self._exec_platform(platform)
        platform_label = self._platform_label(platform)
        runtime = self._runtime()
        if exec_platform == "bitget" and symbol in self._unsupported_symbols:
            return

        if db.has_tracking_no(pid, order_id, platform=platform):
            return

        from database import get_conn
        with get_conn() as conn:
            opened_sum = conn.execute('''
                SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                  AND action = 'open' AND status = 'filled' AND platform = ?
            ''', (pid, symbol, direction, platform)).fetchone()[0]
            closed_sum = conn.execute('''
                SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                  AND action = 'close' AND status = 'filled' AND platform = ?
            ''', (pid, symbol, direction, platform)).fetchone()[0]

        remaining_qty = float(opened_sum) - float(closed_sum)
        if remaining_qty <= 0:
            logger.debug("[%s??????] ????????%s????????(pid=%s)?????", platform_label, symbol, pid[:8])
            return

        close_qty = remaining_qty
        if close_qty <= 0:
            return

        ak, sk, pp = api_creds
        runtime_ctx = order_executor.use_runtime(simulated=runtime["bitget_simulated"])
        if exec_platform == "binance":
            import binance_executor
            runtime_ctx = binance_executor.use_runtime(base_url=runtime["binance_base_url"])

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
                    logger.warning("[%s??????] %s %s ?????%.4f ???=%.4f ???=%.2f%% > %.2f%%", 
                                   platform_label, symbol, direction.upper(), price, curr_p, dev * 100, tol * 100)
                    return

                if exec_platform == "binance":
                    filters = binance_executor.get_symbol_filters(symbol)
                    qty_str = binance_executor._format_qty(close_qty, filters["stepSize"])
                    if float(qty_str) > 0:
                        binance_executor.close_partial_position(
                            ak, sk, symbol, direction, qty_str
                        )
                    else:
                        logger.warning("[%s??????] ??????????? %s", platform_label, close_qty)
                        return
                else:
                    qty_str = str(int(close_qty * 10000) / 10000.0)
                    order_executor.close_partial_position(
                        ak, sk, pp, symbol, direction, qty_str,
                        pos_mode=self._pos_mode, margin_mode="isolated"
                    )

            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": 0, "margin_usdt": 0, "source_price": price, "exec_price": price,
                "deviation_pct": 0, "action": "close",
                "status": "filled", "pnl": order_pnl, "notes": f"[{platform_label} Signal] Close",
                "exec_qty": float(qty_str),
                "platform": platform
            })
            logger.info("[%s??????] %s %s ???=%s", platform_label, symbol, direction.upper(), qty_str)
        except Exception as exc:
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": f"FAIL_{order_id}",
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": 0, "margin_usdt": 0, "source_price": price, "exec_price": price,
                "deviation_pct": 0, "action": "close",
                "status": "failed", "pnl": 0.0, "notes": str(exc),
                "exec_qty": 0.0,
                "platform": platform
            })
            logger.error("[%s??????] %s: %s", platform_label, symbol, exc)



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

