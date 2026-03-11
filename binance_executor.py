"""
binance_executor.py — 币安（模拟盘）账户下单/平仓/查余额
实现市价开仓、平仓、调整杠杆及双向持仓模式逻辑。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import threading
import urllib.parse
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = config.BINANCE_BASE_URL
_DEFAULT_PM_BASE_URL = (os.getenv("BINANCE_PM_BASE_URL", "https://papi.binance.com") or "https://papi.binance.com").strip().rstrip("/")
_RUNTIME = threading.local()
_RUNTIME_SENTINEL = object()
_API_MODE_LOCK = threading.Lock()
_API_MODE_BY_KEY: dict[str, str] = {}

# ?????????????????????? (???????
_SYMBOL_FILTERS: dict[str, dict] = {}


def _resolve_base_url() -> str:
    runtime_value = getattr(_RUNTIME, "base_url", _RUNTIME_SENTINEL)
    if runtime_value is _RUNTIME_SENTINEL:
        runtime_value = config.BINANCE_BASE_URL or _DEFAULT_BASE_URL
    return str(runtime_value or _DEFAULT_BASE_URL).strip().rstrip("/")


@contextmanager
def use_runtime(base_url: str | None = None):
    previous = getattr(_RUNTIME, "base_url", _RUNTIME_SENTINEL)
    if base_url:
        _RUNTIME.base_url = str(base_url).strip().rstrip("/")
    try:
        yield
    finally:
        if previous is _RUNTIME_SENTINEL:
            if hasattr(_RUNTIME, "base_url"):
                delattr(_RUNTIME, "base_url")
        else:
            _RUNTIME.base_url = previous


def _resolve_pm_base_candidates() -> list[str]:
    runtime_base = _resolve_base_url()
    raw_candidates = [
        os.getenv("BINANCE_PM_BASE_URL", "").strip().rstrip("/"),
        runtime_base.replace("://fapi.", "://papi."),
        _DEFAULT_PM_BASE_URL,
    ]
    candidates: list[str] = []
    for item in raw_candidates:
        if not item:
            continue
        if item not in candidates:
            candidates.append(item)
    return candidates


def _api_mode_cache_key(api_key: str, base_url: str | None = None) -> str:
    clean_key = str(api_key or "").strip()
    clean_base = str(base_url or _resolve_base_url()).strip().rstrip("/")
    return f"{clean_base}|{clean_key}"


def _get_preferred_api_mode(api_key: str, base_url: str | None = None) -> str | None:
    cache_key = _api_mode_cache_key(api_key, base_url)
    with _API_MODE_LOCK:
        return _API_MODE_BY_KEY.get(cache_key)


def _set_preferred_api_mode(api_key: str, mode: str | None, base_url: str | None = None) -> None:
    if not api_key:
        return
    cache_key = _api_mode_cache_key(api_key, base_url)
    with _API_MODE_LOCK:
        if mode in {"fapi", "pm"}:
            _API_MODE_BY_KEY[cache_key] = str(mode)
        else:
            _API_MODE_BY_KEY.pop(cache_key, None)


def _is_auth_or_permission_error(exc: Exception) -> bool:
    msg = str(exc)
    return (
        "code=-2015" in msg
        or "HTTP 401" in msg
        or "Invalid API-key" in msg
        or "permissions for action" in msg
    )


_TIME_OFFSET_MS = 0
_TIME_OFFSET_AT = 0.0
_TIME_OFFSET_TTL_SEC = 60


def _clean_symbol(symbol: str) -> str:
    """清理后缀"""
    for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
        symbol = symbol.replace(suffix, "")
    return symbol


def _resolve_pm_symbol_endpoints(
    symbol: str,
    um_endpoint: str,
    cm_endpoint: str,
) -> tuple[str, ...]:
    """Route PM requests to the matching market family to avoid UM/CM symbol mismatches."""
    clean = _clean_symbol(symbol).upper()
    if not clean:
        return tuple(x for x in (um_endpoint, cm_endpoint) if x)

    if clean.endswith(("USDT", "USDC", "BUSD", "FDUSD")):
        return (um_endpoint,) if um_endpoint else ()

    if "_" in clean or clean.endswith("USD"):
        return (cm_endpoint,) if cm_endpoint else ()

    return tuple(x for x in (um_endpoint, cm_endpoint) if x)


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
    """?????????? -1021????????"""
    global _TIME_OFFSET_MS, _TIME_OFFSET_AT
    now = time.time()
    if (not force) and _TIME_OFFSET_AT and (now - _TIME_OFFSET_AT) < _TIME_OFFSET_TTL_SEC:
        return

    resp = requests.get(f"{_resolve_base_url()}/fapi/v1/time", timeout=5)
    resp.raise_for_status()
    payload = resp.json()
    server_ms = int(payload["serverTime"])
    local_ms = int(time.time() * 1000)
    _TIME_OFFSET_MS = server_ms - local_ms
    _TIME_OFFSET_AT = time.time()


def _signed_timestamp_ms() -> int:
    """
    ???????????
    ?? 500ms ??????????????????????
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
    base_url: str | None = None,
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
        
        method_upper = method.upper()
        if method_upper == "GET":
            req_func = requests.get
        elif method_upper == "DELETE":
            req_func = requests.delete
        else:
            req_func = requests.post
            # POST / DELETE ?? query string ??????? url ? params=None

        try:
            resp = req_func(
                f"{(base_url or _resolve_base_url()).rstrip('/')}{endpoint}",
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


def _request_with_pm_fallback(
    api_key: str,
    api_secret: str,
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    pm_endpoints: tuple[str, ...] = (),
    max_retries: int = 3,
) -> Any:
    preferred_mode = _get_preferred_api_mode(api_key)
    if pm_endpoints and preferred_mode == "pm":
        fallback_error: Exception | None = None
        for pm_base in _resolve_pm_base_candidates():
            for pm_endpoint in pm_endpoints:
                try:
                    result = _request(
                        api_key,
                        api_secret,
                        method,
                        pm_endpoint,
                        params=params,
                        base_url=pm_base,
                        max_retries=max_retries,
                    )
                    _set_preferred_api_mode(api_key, "pm")
                    return result
                except Exception as pm_exc:
                    fallback_error = pm_exc
        raise fallback_error or ValueError("portfolio margin endpoint unavailable")

    try:
        result = _request(
            api_key,
            api_secret,
            method,
            endpoint,
            params=params,
            max_retries=max_retries,
        )
        _set_preferred_api_mode(api_key, "fapi")
        return result
    except Exception as exc:
        if not pm_endpoints or not _is_auth_or_permission_error(exc):
            raise

        fallback_error: Exception | None = None
        for pm_base in _resolve_pm_base_candidates():
            for pm_endpoint in pm_endpoints:
                try:
                    result = _request(
                        api_key,
                        api_secret,
                        method,
                        pm_endpoint,
                        params=params,
                        base_url=pm_base,
                        max_retries=1,
                    )
                    _set_preferred_api_mode(api_key, "pm")
                    return result
                except Exception as pm_exc:
                    fallback_error = pm_exc
        raise fallback_error or exc


def set_position_mode(api_key: str, api_secret: str, dual_side: bool = True) -> dict:
    """设置双向持仓（Hedge Mode）"""
    return _request_with_pm_fallback(
        api_key, api_secret, "POST", "/fapi/v1/positionSide/dual",
        params={"dualSidePosition": "true" if dual_side else "false"},
        pm_endpoints=("/papi/v1/um/positionSide/dual", "/papi/v1/cm/positionSide/dual")
    )


def set_symbol_leverage(api_key: str, api_secret: str, symbol: str, leverage: int) -> dict:
    """设置杠杆"""
    return _request_with_pm_fallback(
        api_key, api_secret, "POST", "/fapi/v1/leverage",
        params={"symbol": _clean_symbol(symbol), "leverage": leverage},
        pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/leverage", "/papi/v1/cm/leverage")
    )


def set_margin_type(api_key: str, api_secret: str, symbol: str, margin_type: str = "ISOLATED") -> dict:
    """设置全仓/逐仓 (ISOLATED/CROSSED)"""
    return _request_with_pm_fallback(
        api_key, api_secret, "POST", "/fapi/v1/marginType",
        params={"symbol": _clean_symbol(symbol), "marginType": margin_type},
        pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/marginType", "/papi/v1/cm/marginType")
    )


def get_account_balance(api_key: str, api_secret: str) -> dict:
    """???????????????????????"""

    def _to_float(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _pick(d: dict[str, Any], keys: tuple[str, ...]) -> float:
        for k in keys:
            if k in d and d.get(k) is not None:
                val = _to_float(d.get(k))
                if val != 0:
                    return val
        for k in keys:
            if k in d and d.get(k) is not None:
                return _to_float(d.get(k))
        return 0.0

    def _pick_optional(d: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for k in keys:
            if k not in d or d.get(k) is None:
                continue
            try:
                return float(d.get(k))
            except (TypeError, ValueError):
                continue
        return None

    def _derive_available_balance(d: dict[str, Any]) -> float:
        explicit = _pick_optional(
            d,
            (
                "availableBalance",
                "totalAvailableBalance",
                "virtualMaxWithdrawAmount",
                "maxWithdrawAmount",
                "withdrawAvailable",
                "available",
                "crossMarginFree",
                "umAvailableBalance",
                "cmAvailableBalance",
                "crossWalletBalance",
            ),
        )
        if explicit is not None:
            return max(explicit, 0.0)

        equity = _pick_optional(
            d,
            (
                "accountEquity",
                "totalMarginBalance",
                "marginBalance",
                "equity",
                "totalWalletBalance",
                "walletBalance",
                "balance",
                "crossMarginAsset",
            ),
        )
        if equity is None:
            return 0.0

        initial_margin = _pick_optional(
            d,
            (
                "accountInitialMargin",
                "totalInitialMargin",
                "totalPositionInitialMargin",
                "positionInitialMargin",
            ),
        ) or 0.0
        open_order_margin = _pick_optional(
            d,
            (
                "totalOpenOrderInitialMargin",
                "openOrderInitialMargin",
                "totalMarginOpenLoss",
            ),
        ) or 0.0
        return max(equity - initial_margin - open_order_margin, 0.0)

    def _from_rows(rows: list[dict[str, Any]], endpoint: str) -> dict[str, Any] | None:
        if not rows:
            return None
        preferred = ("USDT", "USDC", "BUSD", "FDUSD", "USD")
        row = None
        for asset in preferred:
            row = next((x for x in rows if str(x.get("asset", "")).upper() == asset), None)
            if row:
                break
        if row is None:
            row = max(
                rows,
                key=lambda x: max(
                    _pick(
                        x,
                        (
                            "equity",
                            "accountEquity",
                            "marginBalance",
                            "crossMarginAsset",
                            "balance",
                            "walletBalance",
                            "crossWalletBalance",
                            "totalWalletBalance",
                            "umWalletBalance",
                            "cmWalletBalance",
                        ),
                    ),
                    _derive_available_balance(x),
                ),
            )
        balance = _pick(
            row,
            (
                "equity",
                "accountEquity",
                "marginBalance",
                "crossMarginAsset",
                "balance",
                "walletBalance",
                "crossWalletBalance",
                "totalWalletBalance",
                "umWalletBalance",
                "cmWalletBalance",
            ),
        )
        available = _derive_available_balance(row)
        out = dict(row)
        out["balance"] = balance
        out["availableBalance"] = available
        out["_endpoint"] = endpoint
        return out

    def _from_account(acct: dict[str, Any], endpoint: str) -> dict[str, Any]:
        balance = _pick(
            acct,
            (
                "totalMarginBalance",
                "totalWalletBalance",
                "totalCrossWalletBalance",
                "accountEquity",
                "uniMMRBalance",
                "marginBalance",
                "balance",
                "walletBalance",
                "equity",
            ),
        )
        available = _derive_available_balance(acct)
        out = dict(acct)
        out["asset"] = out.get("asset") or "USDT"
        out["balance"] = balance
        out["availableBalance"] = available
        out["_endpoint"] = endpoint
        return out

    preferred_mode = _get_preferred_api_mode(api_key)
    fapi_probes: list[tuple[str, str | None]] = [
        ("/fapi/v2/account", None),
        ("/fapi/v2/balance", None),
    ]
    pm_probes: list[tuple[str, str | None]] = []
    for pm_base in _resolve_pm_base_candidates():
        pm_probes.extend([
            ("/papi/v1/account", pm_base),
            ("/papi/v1/um/account", pm_base),
            ("/papi/v1/cm/account", pm_base),
            ("/papi/v1/balance", pm_base),
        ])

    def _collect_candidates(probes: list[tuple[str, str | None]]) -> tuple[list[dict[str, Any]], Exception | None]:
        candidates: list[dict[str, Any]] = []
        first_error: Exception | None = None
        for endpoint, base in probes:
            try:
                data = _request(api_key, api_secret, "GET", endpoint, base_url=base, max_retries=1)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue

            if isinstance(data, list):
                rows = [r for r in data if isinstance(r, dict)]
                parsed = _from_rows(rows, endpoint)
                if parsed:
                    candidates.append(parsed)
            elif isinstance(data, dict):
                assets = data.get("assets")
                if isinstance(assets, list):
                    parsed = _from_rows([r for r in assets if isinstance(r, dict)], endpoint)
                    if parsed:
                        merged = dict(data)
                        merged.update(parsed)
                        candidates.append(merged)
                        continue
                candidates.append(_from_account(data, endpoint))
        return candidates, first_error

    def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, float, float]:
        endpoint = str(candidate.get("_endpoint") or "")
        available = float(candidate.get("availableBalance") or 0.0)
        balance = float(candidate.get("balance") or 0.0)
        if endpoint.endswith("/account"):
            endpoint_priority = 2
        elif "account" in endpoint:
            endpoint_priority = 1
        else:
            endpoint_priority = 0
        return (1 if available > 0 else 0, endpoint_priority, balance, available)

    probe_groups: list[tuple[str, list[tuple[str, str | None]]]] = []
    if preferred_mode == "pm":
        probe_groups = [("pm", pm_probes), ("fapi", fapi_probes)]
    elif preferred_mode == "fapi":
        probe_groups = [("fapi", fapi_probes), ("pm", pm_probes)]
    else:
        probe_groups = [("mixed", fapi_probes + pm_probes)]

    first_error: Exception | None = None
    for group_name, probes in probe_groups:
        candidates, group_error = _collect_candidates(probes)
        if candidates:
            best = max(candidates, key=_candidate_sort_key)
            if group_name == "pm":
                _set_preferred_api_mode(api_key, "pm")
            elif group_name == "fapi":
                _set_preferred_api_mode(api_key, "fapi")
            else:
                endpoint = str(best.get("_endpoint") or "")
                _set_preferred_api_mode(api_key, "pm" if endpoint.startswith("/papi/") else "fapi")
            return best
        if first_error is None and group_error is not None:
            first_error = group_error

    if first_error is not None:
        raise first_error

    return {"asset": "USDT", "balance": 0.0, "availableBalance": 0.0, "_endpoint": "unknown"}


def get_my_positions(api_key: str, api_secret: str) -> list[dict]:
    """??????????????"""
    data: Any
    preferred_mode = _get_preferred_api_mode(api_key)
    if preferred_mode == "pm":
        data = None
        fallback_error: Exception | None = None
        for pm_base in _resolve_pm_base_candidates():
            for endpoint in ("/papi/v1/um/positionRisk", "/papi/v1/cm/positionRisk"):
                try:
                    data = _request(api_key, api_secret, "GET", endpoint, base_url=pm_base, max_retries=1)
                    _set_preferred_api_mode(api_key, "pm")
                    break
                except Exception as pm_exc:
                    fallback_error = pm_exc
            if isinstance(data, list):
                break
        if data is None:
            raise fallback_error or ValueError("portfolio margin position query failed")
    else:
        try:
            data = _request(api_key, api_secret, "GET", "/fapi/v2/positionRisk")
            _set_preferred_api_mode(api_key, "fapi")
        except Exception as exc:
            if not _is_auth_or_permission_error(exc):
                raise
            data = None
            fallback_error: Exception | None = None
            for pm_base in _resolve_pm_base_candidates():
                for endpoint in ("/papi/v1/um/positionRisk", "/papi/v1/cm/positionRisk"):
                    try:
                        data = _request(api_key, api_secret, "GET", endpoint, base_url=pm_base, max_retries=1)
                        _set_preferred_api_mode(api_key, "pm")
                        break
                    except Exception as pm_exc:
                        fallback_error = pm_exc
                if isinstance(data, list):
                    break
            if data is None:
                raise fallback_error or exc

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
    """???? (????)"""
    symbol = _clean_symbol(symbol)
    resp = requests.get(f"{_resolve_base_url()}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    if not resp.ok:
        _raise_response_error(resp, f"ticker request failed: {symbol}")
    payload = resp.json()
    if payload.get("code") not in (None, 0, "0") and payload.get("price") is None:
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")
    return float(payload["price"])


def get_book_ticker(symbol: str) -> dict:
    symbol = _clean_symbol(symbol)
    resp = requests.get(f"{_resolve_base_url()}/fapi/v1/ticker/bookTicker", params={"symbol": symbol}, timeout=10)
    if not resp.ok:
        _raise_response_error(resp, f"bookTicker request failed: {symbol}")
    payload = resp.json()
    if payload.get("code") not in (None, 0, "0") and payload.get("bidPrice") is None:
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")
    return payload if isinstance(payload, dict) else {}


def get_symbol_filters(symbol: str) -> dict:
    """???????????????"""
    symbol = _clean_symbol(symbol)
    base_url = _resolve_base_url()
    cache_key = f"{base_url}:{symbol}"
    if cache_key in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[cache_key]

    resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
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
        _SYMBOL_FILTERS[f"{base_url}:{sym}"] = {
            "quantityPrecision": s_info["quantityPrecision"],
            "pricePrecision": s_info["pricePrecision"],
            "stepSize": step_size,
            "minQty": min_qty,
            "tickSize": float(price_filter["tickSize"]),
            "minNotional": min_notional,
        }

    if cache_key not in _SYMBOL_FILTERS:
        raise ValueError(f"USDT-M 交易规则中找不到该合约: {symbol}")

    return _SYMBOL_FILTERS[cache_key]


def _format_qty(qty: float, step_size: float) -> str:
    """?? stepSize ?????"""
    import math
    if step_size <= 0:
        return str(qty)
    precision = max(0, int(round(-math.log10(step_size))))
    formatted_qty = math.floor(qty / step_size) * step_size
    return f"{formatted_qty:.{precision}f}"


def _format_price(price: float, tick_size: float, round_up: bool = False) -> str:
    if price <= 0:
        raise ValueError(f"价格必须大于 0: {price}")
    if tick_size <= 0:
        return format(Decimal(str(price)).normalize(), "f")
    step = Decimal(str(tick_size))
    value = Decimal(str(price))
    units = (value / step).to_integral_value(rounding=ROUND_CEILING if round_up else ROUND_DOWN)
    return format((units * step).normalize(), "f")


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
    """???? (???????)"""
    symbol = _clean_symbol(symbol)
    direction = direction.lower() # "long" or "short"
    margin_mode = "ISOLATED" if margin_mode.lower() in ["isolated", "fixed"] else "CROSSED"

    try:
        set_position_mode(api_key, api_secret, dual_side=True)
    except Exception:
        pass

    try:
        set_symbol_leverage(api_key, api_secret, symbol, leverage)
    except Exception as e:
        logger.warning(f"设置杠杆失败(继续下单): {e}")

    try:
        set_margin_type(api_key, api_secret, symbol, margin_mode)
    except Exception:
        pass

    price = current_price if current_price > 0 else get_ticker_price(symbol)
    raw_qty = (usdt_margin * leverage) / price

    filters = get_symbol_filters(symbol)
    if raw_qty < filters["minQty"]:
        raise ValueError(f"数量 {raw_qty} 小于最小下单量 {filters['minQty']} (保证金: {usdt_margin})")

    qty_str = _format_qty(raw_qty, filters["stepSize"])
    if float(qty_str) == 0:
        raise ValueError(f"按步长截断后数量为 0: {raw_qty}")

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

    res = _request_with_pm_fallback(api_key, api_secret, "POST", "/fapi/v1/order", params=payload, pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/order", "/papi/v1/cm/order"))
    if isinstance(res, dict):
        res["_calculated_size"] = qty_str
    return res


def place_limit_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    usdt_margin: float,
    limit_price: float,
    client_oid: str = "",
    post_only: bool = True,
) -> dict:
    symbol = _clean_symbol(symbol)
    direction = direction.lower()
    margin_mode = "ISOLATED" if margin_mode.lower() in ["isolated", "fixed"] else "CROSSED"

    try:
        set_position_mode(api_key, api_secret, dual_side=True)
    except Exception:
        pass

    try:
        set_symbol_leverage(api_key, api_secret, symbol, leverage)
    except Exception as e:
        logger.warning(f"设置杠杆失败(继续下单): {e}")

    try:
        set_margin_type(api_key, api_secret, symbol, margin_mode)
    except Exception:
        pass

    filters = get_symbol_filters(symbol)
    raw_qty = (usdt_margin * leverage) / limit_price
    if raw_qty < filters["minQty"]:
        raise ValueError(f"数量 {raw_qty} 小于最小下单量 {filters['minQty']} (保证金: {usdt_margin})")

    qty_str = _format_qty(raw_qty, filters["stepSize"])
    if float(qty_str) == 0:
        raise ValueError(f"按步长截断后数量为 0: {raw_qty}")

    price_str = _format_price(limit_price, float(filters.get("tickSize") or 0.0), round_up=direction == "short")
    position_side = "LONG" if direction == "long" else "SHORT"
    side = "BUY" if direction == "long" else "SELL"
    payload = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "LIMIT",
        "timeInForce": "GTX" if post_only else "GTC",
        "quantity": qty_str,
        "price": price_str,
    }
    if client_oid:
        payload["newClientOrderId"] = client_oid

    res = _request_with_pm_fallback(api_key, api_secret, "POST", "/fapi/v1/order", params=payload, pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/order", "/papi/v1/cm/order"))
    if isinstance(res, dict):
        res["_calculated_size"] = qty_str
        res["_limit_price"] = price_str
    return res


def get_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    order_id: str = "",
    client_oid: str = "",
) -> dict:
    params = {"symbol": _clean_symbol(symbol)}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_oid:
        params["origClientOrderId"] = client_oid
    else:
        raise ValueError("get_order 需要 order_id 或 client_oid")
    data = _request_with_pm_fallback(api_key, api_secret, "GET", "/fapi/v1/order", params=params, pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/order", "/papi/v1/cm/order"), max_retries=1)
    return data if isinstance(data, dict) else {}


def cancel_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    order_id: str = "",
    client_oid: str = "",
) -> dict:
    params = {"symbol": _clean_symbol(symbol)}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_oid:
        params["origClientOrderId"] = client_oid
    else:
        raise ValueError("cancel_order 需要 order_id 或 client_oid")
    data = _request_with_pm_fallback(api_key, api_secret, "DELETE", "/fapi/v1/order", params=params, pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/order", "/papi/v1/cm/order"), max_retries=1)
    return data if isinstance(data, dict) else {}


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
    return _request_with_pm_fallback(api_key, api_secret, "POST", "/fapi/v1/order", params=payload, pm_endpoints=_resolve_pm_symbol_endpoints(symbol, "/papi/v1/um/order", "/papi/v1/cm/order"))
