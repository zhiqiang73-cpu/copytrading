"""
binance_executor.py — 币安（模拟盘）账户下单/平仓/查余额
实现市价开仓、平仓、调整杠杆及双向持仓模式逻辑。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from decimal import Decimal, ROUND_CEILING
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

# 模拟盘基础 URL
BASE_URL = config.BINANCE_BASE_URL

# 币安不同币种的价格和数量精度缓存 (动态更新)
_SYMBOL_FILTERS: dict[str, dict] = {}

# ????????????server_ms - local_ms?
_TIME_OFFSET_MS = 0
_TIME_OFFSET_AT = 0.0
_TIME_OFFSET_TTL_SEC = 60


def _clean_symbol(symbol: str) -> str:
    """清理后缀"""
    for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
        symbol = symbol.replace(suffix, "")
    return symbol


def _is_non_retryable_error_message(msg: str) -> bool:
    lower = msg.lower()
    return (
        "code=-1121" in msg
        or "code=-2011" in msg
        or "code=-2019" in msg
        or "code=-4164" in msg
        or "invalid symbol" in lower
        or "margin is insufficient" in lower
        or "notional must be no smaller" in lower
    )


def _raise_response_error(resp: requests.Response, default_msg: str) -> None:
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    error_msg = payload.get("msg") or resp.text[:200] or default_msg
    error_code = payload.get("code", resp.status_code)
    raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")


def _sign(secret: str, query_string: str) -> str:
    """计算 HMAC-SHA256 签名"""
    return hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


def _refresh_server_time_offset(force: bool = False) -> None:
    """???????????? -1021????????"""
    global _TIME_OFFSET_MS, _TIME_OFFSET_AT
    now = time.time()
    if (not force) and _TIME_OFFSET_AT and (now - _TIME_OFFSET_AT) < _TIME_OFFSET_TTL_SEC:
        return

    resp = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=5)
    resp.raise_for_status()
    payload = resp.json()
    server_ms = int(payload["serverTime"])
    local_ms = int(time.time() * 1000)
    _TIME_OFFSET_MS = server_ms - local_ms
    _TIME_OFFSET_AT = time.time()


def _signed_timestamp_ms() -> int:
    """
    ???????????
    ???? 500ms ??????????????????????
    """
    try:
        _refresh_server_time_offset(force=False)
    except Exception as exc:
        logger.debug("sync binance server time failed (use local clock): %s", exc)
    return int(time.time() * 1000) + int(_TIME_OFFSET_MS) - 500


def _request(
    api_key: str,
    api_secret: str,
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> Any:
    
    for attempt in range(1, max_retries + 1):
        if params is None:
            params = {}
        
        # 移除空值
        query_params = {k: v for k, v in params.items() if v is not None}
        query_params.setdefault("recvWindow", 10000)
        query_params["timestamp"] = _signed_timestamp_ms()
        # 预签名时需要按特定顺序或 urlencode，币安 FAPI 接受 key=value&key=value 格式
        query_string = urllib.parse.urlencode(query_params, doseq=True)
        signature = _sign(api_secret, query_string)
        query_string += f"&signature={signature}"
        
        headers = {
            "X-MBX-APIKEY": api_key,
            "Content-Type": "application/json"
        }
        
        if method.upper() == "GET":
            req_func = requests.get
        else:
            req_func = requests.post
            # POST / DELETE 也经常用 query string 传递，但也可能需要 url 但 params=None
            
        try:
            resp = req_func(
                f"{BASE_URL}{endpoint}",
                headers=headers,
                params=query_string, # requests accept string as raw query
                timeout=15,
            )
            
            try:
                payload = resp.json()
            except Exception:
                payload = {}
                
            if not resp.ok:
                error_msg = payload.get("msg") or resp.text[:200]
                error_code = payload.get("code", resp.status_code)
                
                # 特殊错误处理 (例如：杠杆未改变、双向持仓已设置等，对于幂等请求可以忽略)
                if error_code == -4028: # Leverage is already 50
                    return {"msg": error_msg}
                if error_code == -4059: # No need to change position side
                    return {"msg": error_msg}
                if error_code == -4046: # No need to change margin type
                    return {"msg": error_msg}
                if error_code == -1021: # Timestamp for this request is outside of recvWindow
                    try:
                        _refresh_server_time_offset(force=True)
                    except Exception as sync_exc:
                        logger.warning("refresh binance time failed after -1021: %s", sync_exc)
                    raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")
                
                raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")
            
            return payload
            
        except (requests.RequestException, ValueError) as exc:
            msg = str(exc)
            # 对于明确的参数错误或者不可用的币种，无需重试
            if _is_non_retryable_error_message(msg):
                logger.error("API 请求失败 [%s %s]: %s", method, endpoint, exc)
                raise
            
            if attempt == max_retries:
                logger.error("API 请求失败 [%s %s]: %s", method, endpoint, exc)
                raise
            wait = 2 ** attempt
            logger.warning("第 %d 次重试（等待 %ds）: %s", attempt, wait, exc)
            time.sleep(wait)


def set_position_mode(api_key: str, api_secret: str, dual_side: bool = True) -> dict:
    """设置双向持仓（Hedge Mode）"""
    return _request(
        api_key, api_secret, "POST", "/fapi/v1/positionSide/dual",
        params={"dualSidePosition": "true" if dual_side else "false"}
    )


def set_symbol_leverage(api_key: str, api_secret: str, symbol: str, leverage: int) -> dict:
    """设置杠杆"""
    return _request(
        api_key, api_secret, "POST", "/fapi/v1/leverage",
        params={"symbol": _clean_symbol(symbol), "leverage": leverage}
    )


def set_margin_type(api_key: str, api_secret: str, symbol: str, margin_type: str = "ISOLATED") -> dict:
    """设置全仓/逐仓 (ISOLATED/CROSSED)"""
    return _request(
        api_key, api_secret, "POST", "/fapi/v1/marginType",
        params={"symbol": _clean_symbol(symbol), "marginType": margin_type}
    )


def get_account_balance(api_key: str, api_secret: str) -> dict:
    """获取账户 USDT 余额信息 (复用 binance_scraper 中的逻辑，但放在执行器更内聚)"""
    data = _request(api_key, api_secret, "GET", "/fapi/v2/balance")
    usdt_info = next((item for item in data if item["asset"] == "USDT"), None)
    if not usdt_info:
        return {"balance": 0.0, "availableBalance": 0.0}
    return usdt_info


def get_my_positions(api_key: str, api_secret: str) -> list[dict]:
    """获取币安模拟盘当前非零持仓。"""
    data = _request(api_key, api_secret, "GET", "/fapi/v2/positionRisk")
    if not isinstance(data, list):
        return []

    positions: list[dict] = []
    for item in data:
        try:
            amt = float(item.get("positionAmt") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        if abs(amt) <= 0:
            continue
        positions.append(item)
    return positions


def get_ticker_price(symbol: str) -> float:
    """获取最新价格 (无需签名)"""
    symbol = _clean_symbol(symbol)
    resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    if not resp.ok:
        _raise_response_error(resp, f"ticker request failed: {symbol}")
    payload = resp.json()
    if payload.get("code") not in (None, 0, "0") and payload.get("price") is None:
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")
    return float(payload["price"])


def get_symbol_filters(symbol: str) -> dict:
    """获取币种的精度等规则"""
    symbol = _clean_symbol(symbol)
    if symbol in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[symbol]
    
    resp = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    
    for s_info in data.get("symbols", []):
        sym = s_info["symbol"]
        qty_filter = next(f for f in s_info["filters"] if f["filterType"] == "LOT_SIZE")
        market_qty_filter = next((f for f in s_info["filters"] if f["filterType"] == "MARKET_LOT_SIZE"), qty_filter)
        price_filter = next(f for f in s_info["filters"] if f["filterType"] == "PRICE_FILTER")
        notional_filter = next(
            (f for f in s_info["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")),
            {},
        )
        step_size = float(market_qty_filter.get("stepSize") or qty_filter["stepSize"])
        min_qty = float(market_qty_filter.get("minQty") or qty_filter["minQty"])
        min_notional = float(
            notional_filter.get("notional")
            or notional_filter.get("minNotional")
            or 0
        )
        _SYMBOL_FILTERS[sym] = {
            "quantityPrecision": s_info["quantityPrecision"],
            "pricePrecision": s_info["pricePrecision"],
            "stepSize": step_size,
            "minQty": min_qty,
            "tickSize": float(price_filter["tickSize"]),
            "minNotional": min_notional,
        }
        
    if symbol not in _SYMBOL_FILTERS:
        raise ValueError(f"USDT-M exchangeInfo 中不存在 symbol: {symbol}")
        
    return _SYMBOL_FILTERS[symbol]


def _format_qty(qty: float, step_size: float) -> str:
    """根据 stepSize 格式化数量"""
    import math
    if step_size <= 0: return str(qty)
    # 按步长向下取整
    precision = max(0, int(round(-math.log10(step_size))))
    formatted_qty = math.floor(qty / step_size) * step_size
    return f"{formatted_qty:.{precision}f}"


def _ceil_qty(qty: float, step_size: float) -> float:
    if step_size <= 0:
        return qty
    step = Decimal(str(step_size))
    value = Decimal(str(max(qty, 0.0)))
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return float(units * step)


def get_min_order_requirements(symbol: str, leverage: int, price: float) -> dict:
    """计算币安当前价格下的最小可成交数量和最低保证金。"""
    if price <= 0:
        raise ValueError(f"行情价非正值: {price}")

    filters = get_symbol_filters(symbol)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 0.0)
    qty_for_notional = (min_notional / price) if min_notional > 0 else 0.0
    required_qty = max(min_qty, qty_for_notional)
    required_qty = _ceil_qty(required_qty, float(filters.get("stepSize") or 0.0))
    required_margin = (required_qty * price) / max(leverage, 1)
    return {
        "symbol": _clean_symbol(symbol),
        "minQty": min_qty,
        "stepSize": float(filters.get("stepSize") or 0.0),
        "minNotional": min_notional,
        "requiredQty": required_qty,
        "requiredMargin": required_margin,
    }


def place_market_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    usdt_margin: float,
    current_price: float = 0.0,
    client_oid: str = "",
) -> dict:
    """币安市价开仓 (双向持仓模式下)"""
    symbol = _clean_symbol(symbol)
    direction = direction.lower() # "long" or "short"
    margin_mode = "ISOLATED" if margin_mode.lower() in ["isolated", "fixed"] else "CROSSED"
    
    # 1. 设置双向持仓 (失败忽略，可能是已经设置过了)
    try:
        set_position_mode(api_key, api_secret, dual_side=True)
    except Exception:
        pass
        
    # 2. 设置杠杆
    try:
        set_symbol_leverage(api_key, api_secret, symbol, leverage)
    except Exception as e:
        logger.warning(f"币安设置杠杆异常(按原杠杆执行): {e}")

    # 3. 设置逐仓/全仓
    try:
        set_margin_type(api_key, api_secret, symbol, margin_mode)
    except Exception:
        pass

    # 计算数量
    price = current_price if current_price > 0 else get_ticker_price(symbol)
    raw_qty = (usdt_margin * leverage) / price
    
    filters = get_symbol_filters(symbol)
    if raw_qty < filters["minQty"]:
        raise ValueError(f"开仓数量 {raw_qty} 小于币安要求的最小数量 {filters['minQty']} (保证金: {usdt_margin})")
        
    qty_str = _format_qty(raw_qty, filters["stepSize"])
    if float(qty_str) == 0:
        raise ValueError(f"开仓数量因精度截断为 0: {raw_qty}")

    position_side = "LONG" if direction == "long" else "SHORT"
    side = "BUY" if direction == "long" else "SELL"
    
    payload = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": qty_str,
    }
    if client_oid:
        payload["newClientOrderId"] = client_oid

    logger.info("币安按量下单: %s %s POS_SIDE=%s 数量=%s 预期保证金=%.2f", symbol, side, position_side, qty_str, usdt_margin)
    
    res = _request(api_key, api_secret, "POST", "/fapi/v1/order", params=payload)
    if isinstance(res, dict):
        res["_calculated_size"] = qty_str
    return res


def close_partial_position(
    api_key: str,
    api_secret: str,
    symbol: str,
    direction: str,
    qty_str: str,
) -> dict:
    """币安双向持仓模式市价平仓"""
    symbol = _clean_symbol(symbol)
    direction = direction.lower() # "long" or "short"
    
    position_side = "LONG" if direction == "long" else "SHORT"
    # 平多仓则卖，平空仓则买
    side = "SELL" if direction == "long" else "BUY"
    
    payload = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": qty_str,
    }
    
    logger.info("币安局部平仓: %s %s POS_SIDE=%s 数量=%s", symbol, side, position_side, qty_str)
    return _request(api_key, api_secret, "POST", "/fapi/v1/order", params=payload)
