"""
order_executor.py — 用户账户下单/平仓/查余额
复用 Bitget V2 签名机制，但允许传入独立 API 凭证。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import threading
import urllib.parse
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)
_SYMBOL_RULES: dict[str, dict] = {}
_RUNTIME = threading.local()
_RUNTIME_SENTINEL = object()


def _resolve_simulated() -> bool:
    runtime_value = getattr(_RUNTIME, "simulated", _RUNTIME_SENTINEL)
    if runtime_value is _RUNTIME_SENTINEL:
        return bool(config.SIMULATED)
    return bool(runtime_value)


@contextmanager
def use_runtime(simulated: bool | None = None):
    previous = getattr(_RUNTIME, "simulated", _RUNTIME_SENTINEL)
    if simulated is not None:
        _RUNTIME.simulated = bool(simulated)
    try:
        yield
    finally:
        if previous is _RUNTIME_SENTINEL:
            if hasattr(_RUNTIME, "simulated"):
                delattr(_RUNTIME, "simulated")
        else:
            _RUNTIME.simulated = previous


def _mode_headers() -> dict[str, str]:
    return {"paptrading": "1"} if _resolve_simulated() else {}


def _clean_symbol(symbol: str) -> str:
    """将爬虫抓来的旧格式 symbol（如 BTCUSDT_UMCBL）转为 API 下单格式（BTCUSDT）。"""
    for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
        symbol = symbol.replace(suffix, "")
    return symbol


def _normalize_pos_mode(pos_mode: Any) -> str:
    """
    统一仓位模式标识：
    - 单向：1 / one_way_mode / single_hold_mode
    - 双向：2 / hedge_mode / double_hold_mode
    """
    v = str(pos_mode or "").strip().lower()
    if v in {"1", "one_way_mode", "oneway", "one_way", "single_hold_mode", "single_hold"}:
        return "1"
    if v in {"2", "hedge_mode", "hedge", "double_hold_mode", "double_hold"}:
        return "2"
    return ""


def _normalize_margin_mode(margin_mode: str) -> str:
    """兼容历史写法 cross/isolated，统一为 Bitget 接口接受的值。"""
    mm = str(margin_mode or "").strip().lower()
    if mm in {"cross", "crossed"}:
        return "crossed"
    if mm in {"isolated", "fixed"}:
        return "isolated"
    return "crossed"


def _is_bitget_error(exc: Exception, code: str) -> bool:
    return f"code={code}" in str(exc)


def _is_non_retryable_error(exc: Exception) -> bool:
    msg = str(exc)
    lower_msg = msg.lower()
    # 交易对不存在、保证金模式不合法等属于参数错误，重试相同参数无意义
    return (
        "code=40034" in msg
        or "code=400172" in msg
        or "code=40774" in msg
        or "code=40762" in msg
        or "code=45110" in msg
        or "code=45111" in msg
        or ("参数" in msg and "不存在" in msg)
        or ("订单金额" in msg and "超出账户余额" in msg)
        or ("最小下单数量" in msg)
        or ("最小下单价值" in msg)
        or "symbol does not exist" in lower_msg
        or "order amount exceeds account balance" in lower_msg
    )


def _is_margin_mode_error(exc: Exception) -> bool:
    """400172 保证金模式不合法：模拟盘常见，可用 crossed 重试。"""
    return "code=400172" in str(exc)


def _normalize_size(size: float | str) -> str:
    v = float(size)
    if v <= 0:
        raise ValueError(f"下单张数必须为正数: {size}")
    # 截断到 4 位，和其他下单路径保持一致
    v = int(v * 10000) / 10000.0
    if v <= 0:
        raise ValueError(f"下单张数过小，截断后为 0: {size}")
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _product_type_param(product_type: str = "USDT-FUTURES") -> str:
    return str(_product_type(product_type)).lower()


def _format_decimal_str(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ceil_decimal_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def _raise_response_error(resp: requests.Response, default_msg: str) -> None:
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    error_msg = payload.get("msg") or payload.get("message") or resp.text[:200] or default_msg
    error_code = payload.get("code", resp.status_code)
    raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")


def get_symbol_rules(symbol: str, product_type: str = "USDT-FUTURES") -> dict:
    """读取 Bitget 合约配置，用于最小下单量和状态预检查。"""
    symbol = _clean_symbol(symbol)
    cache_key = f"{_product_type_param(product_type)}:{symbol}:{int(_resolve_simulated())}"
    if cache_key in _SYMBOL_RULES:
        return _SYMBOL_RULES[cache_key]

    resp = requests.get(
        config.BASE_URL + "/api/v2/mix/market/contracts",
        params={"productType": _product_type(product_type), "symbol": symbol},
        timeout=10,
        headers=_mode_headers(),
    )
    if not resp.ok:
        _raise_response_error(resp, f"contracts request failed: {symbol}")
    payload = resp.json()
    if str(payload.get("code", "0")) != "00000":
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")

    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]

    rule = next((item for item in data if _clean_symbol(item.get("symbol") or "") == symbol), None)
    if not rule:
        raise ValueError(f"symbol does not exist: {symbol}")

    _SYMBOL_RULES[cache_key] = rule
    return rule


def get_min_order_requirements(symbol: str, leverage: int, price: float, product_type: str = "USDT-FUTURES") -> dict:
    """计算 Bitget 在当前价格下的最小可成交数量和最低保证金。"""
    if price <= 0:
        raise ValueError(f"行情价非正值: {price}")

    rules = get_symbol_rules(symbol, product_type)
    min_trade_num = Decimal(str(rules.get("minTradeNum") or "0"))
    size_multiplier = Decimal(str(rules.get("sizeMultiplier") or "0"))
    min_trade_usdt = Decimal(str(rules.get("minTradeUSDT") or "0"))
    qty_from_usdt = (min_trade_usdt / Decimal(str(price))) if min_trade_usdt > 0 else Decimal("0")
    required_qty = max(min_trade_num, qty_from_usdt)

    if size_multiplier > 0:
        required_qty = _ceil_decimal_to_step(required_qty, size_multiplier)
    if required_qty <= 0:
        required_qty = min_trade_num

    min_margin = (required_qty * Decimal(str(price))) / Decimal(str(max(leverage, 1)))
    return {
        "symbol": _clean_symbol(symbol),
        "symbolStatus": str(rules.get("symbolStatus") or "normal").lower(),
        "limitOpenTime": str(rules.get("limitOpenTime") or "-1"),
        "minTradeNum": float(min_trade_num),
        "sizeMultiplier": float(size_multiplier) if size_multiplier > 0 else 0.0,
        "minTradeUSDT": float(min_trade_usdt),
        "requiredQty": float(required_qty),
        "requiredQtyStr": _format_decimal_str(required_qty),
        "requiredMargin": float(min_margin),
    }


def _sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _make_signed_headers(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    method: str,
    endpoint: str,
    params: dict | None = None,
    body_str: str = "",
) -> tuple[dict, str]:
    base_url = config.BASE_URL + endpoint
    req_obj = requests.Request(method, base_url, params=params)
    prepared = req_obj.prepare()
    actual_url = prepared.url
    parsed = urllib.parse.urlparse(actual_url)
    request_path = parsed.path + ("?" + parsed.query if parsed.query else "")

    ts = str(int(time.time() * 1000))
    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": _sign(api_secret, ts, method, request_path, body_str),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": api_passphrase,
        "Content-Type": "application/json",
        "locale": "zh-CN",
    }
    # 模拟盘模式：加 paptrading 请求头
    if _resolve_simulated():
        headers["paptrading"] = "1"
    return headers, actual_url


def _request(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    method: str,
    endpoint: str,
    params=None,
    data=None,
    max_retries: int = 3,
) -> Any:
    body_str = json.dumps(data) if data else ""
    for attempt in range(1, max_retries + 1):
        headers, actual_url = _make_signed_headers(
            api_key, api_secret, api_passphrase, method, endpoint, params, body_str
        )
        try:
            if method.upper() == "GET":
                resp = requests.get(actual_url, headers=headers, timeout=15)
            else:
                # 统一使用 actual_url（含正确的 path），POST body 单独传入
                resp = requests.post(
                    actual_url,
                    headers=headers,
                    data=body_str if body_str else None,
                    timeout=15,
                )
            # 先尝试解析响应体，即使是错误状态码
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            
            # 检查 HTTP 状态码
            if not resp.ok:
                error_msg = payload.get("msg") or payload.get("message") or resp.text[:200]
                error_code = payload.get("code", resp.status_code)
                raise ValueError(f"HTTP {resp.status_code} | code={error_code} | {error_msg}")
            
            code = payload.get("code", "0")
            if str(code) != "00000":
                raise ValueError(
                    f"API error {code}: {payload.get('msg', '')} | {endpoint}"
                )
            return payload.get("data")
        except (requests.RequestException, ValueError) as exc:
            if _is_non_retryable_error(exc):
                logger.error("API 请求失败 [%s %s]: %s", method, endpoint, exc)
                raise
            if attempt == max_retries:
                logger.error("API 请求失败 [%s %s]: %s", method, endpoint, exc)
                raise
            wait = 2 ** attempt
            logger.warning("第 %d 次重试（等待 %ds）: %s", attempt, wait, exc)
            time.sleep(wait)


def _product_type(pt: str = "USDT-FUTURES") -> str:
    """模拟盘模式下不需要修改 productType，仅通过 paptrading 请求头区分。"""
    return pt


def test_connection(api_key: str, api_secret: str, api_passphrase: str) -> dict:
    return get_account_balance(api_key, api_secret, api_passphrase)


def test_binance_connection(api_key: str, api_secret: str) -> dict:
    import binance_executor
    return binance_executor.get_account_balance(api_key, api_secret)


def get_account_balance(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    product_type: str = "USDT-FUTURES",
) -> dict:
    """
    返回账户余额数据（包含 available 等），以及账户模式 posMode（1=单向, 2=双向）。
    为了向后兼容，我们将 posMode 塞入返回的字典中。
    """
    data = _request(
        api_key,
        api_secret,
        api_passphrase,
        "GET",
        "/api/v2/mix/account/accounts",
        params={"productType": _product_type(product_type)},
    )
    # Bitget 接口可能返回 list 或 dict
    res = {}
    if isinstance(data, list) and data:
        res = dict(data[0])
    elif isinstance(data, dict):
        res = dict(data)
    
    # 一些账户返回 posMode=one_way_mode/hedge_mode，统一映射成 "1"/"2"。
    pos_mode_raw = res.get("posMode") or res.get("holdMode") or ""
    pos_mode_norm = _normalize_pos_mode(pos_mode_raw)
    res["_posMode"] = pos_mode_norm or "2"
    res["_posModeRaw"] = str(pos_mode_raw)
    return res


def get_my_positions(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    product_type: str = "USDT-FUTURES",
) -> list[dict]:
    data = _request(
        api_key,
        api_secret,
        api_passphrase,
        "GET",
        "/api/v2/mix/position/all-position",
        params={"productType": _product_type(product_type)},
    )
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("positionList", "list", "data", "positions"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def get_ticker_price(
    symbol: str,
    product_type: str = "USDT-FUTURES",
) -> float:
    """
    ????????????? API Key??
    ??? USDT ???????????
    """
    data = get_ticker_snapshot(symbol, product_type)
    for key in ("last", "lastPr", "lastPrice", "close", "markPrice"):
        if data.get(key) is not None:
            return float(data[key])
    raise ValueError("ticker API ?????")


def get_ticker_snapshot(
    symbol: str,
    product_type: str = "USDT-FUTURES",
) -> dict:
    symbol = _clean_symbol(symbol)
    resp = requests.get(
        config.BASE_URL + "/api/v2/mix/market/ticker",
        params={"symbol": symbol, "productType": _product_type(product_type)},
        timeout=10,
        headers=_mode_headers(),
    )
    if not resp.ok:
        _raise_response_error(resp, f"ticker request failed: {symbol}")
    payload = resp.json()
    if str(payload.get("code", "0")) != "00000":
        raise ValueError(f"HTTP {resp.status_code} | code={payload.get('code')} | {payload.get('msg')}")
    data = payload.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("ticker API ??????")
    return data


def get_price_step(symbol: str, product_type: str = "USDT-FUTURES") -> float:
    rules = get_symbol_rules(symbol, product_type)
    price_place = max(int(rules.get("pricePlace") or 0), 0)
    price_end_step = Decimal(str(rules.get("priceEndStep") or "1"))
    step = price_end_step / (Decimal("10") ** price_place)
    return float(step) if step > 0 else 0.0


def _normalize_price(price: float, price_step: float, round_up: bool = False) -> str:
    if price <= 0:
        raise ValueError(f"?????????: {price}")
    step = Decimal(str(price_step)) if price_step > 0 else Decimal("0")
    value = Decimal(str(price))
    if step > 0:
        rounding = ROUND_CEILING if round_up else ROUND_DOWN
        units = (value / step).to_integral_value(rounding=rounding)
        value = units * step
    return _format_decimal_str(value)


def place_limit_order_by_size(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    size: float | str,
    limit_price: float,
    product_type: str = "USDT-FUTURES",
    client_oid: str = "",
    pos_mode: str = "2",
    sync_leverage: bool = True,
    post_only: bool = True,
) -> dict:
    symbol = _clean_symbol(symbol)
    size_str = _normalize_size(size)
    base_side = "buy" if direction.lower() == "long" else "sell"
    norm_pos_mode = _normalize_pos_mode(pos_mode)
    side = f"{base_side}_single" if norm_pos_mode == "1" else base_side
    price_str = _normalize_price(limit_price, get_price_step(symbol, product_type), round_up=direction.lower() == "short")

    def _do_set_leverage(mm: str) -> None:
        if sync_leverage:
            set_symbol_leverage(
                api_key, api_secret, api_passphrase,
                symbol=symbol, direction=direction, leverage=leverage,
                margin_mode=mm, pos_mode=norm_pos_mode or "2",
                product_type=product_type,
            )

    try:
        _do_set_leverage(margin_mode)
    except Exception as exc:
        if _resolve_simulated() and _is_margin_mode_error(exc):
            fallback = "crossed" if _normalize_margin_mode(margin_mode) != "crossed" else "isolated"
            logger.warning("??? 400172(????)??? marginMode=%s: %s", fallback, symbol)
            _do_set_leverage(fallback)
            margin_mode = fallback
        else:
            raise

    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginMode": _normalize_margin_mode(margin_mode),
        "marginCoin": "USDT",
        "size": size_str,
        "side": side,
        "orderType": "limit",
        "price": price_str,
        "force": "post_only" if post_only else "gtc",
        "leverage": str(max(1, int(leverage))),
    }
    if norm_pos_mode == "2":
        payload["tradeSide"] = "open"
    if client_oid:
        payload["clientOid"] = client_oid

    try:
        res = _request(
            api_key,
            api_secret,
            api_passphrase,
            "POST",
            "/api/v2/mix/order/place-order",
            data=payload,
        )
    except Exception as exc:
        if _resolve_simulated() and _is_margin_mode_error(exc):
            fallback = "crossed" if _normalize_margin_mode(margin_mode) == "isolated" else "isolated"
            payload_retry = {**payload, "marginMode": fallback}
            if sync_leverage:
                set_symbol_leverage(
                    api_key, api_secret, api_passphrase,
                    symbol=symbol, direction=direction, leverage=leverage,
                    margin_mode=fallback, pos_mode=norm_pos_mode or "2",
                    product_type=product_type,
                )
            res = _request(
                api_key,
                api_secret,
                api_passphrase,
                "POST",
                "/api/v2/mix/order/place-order",
                data=payload_retry,
            )
        elif _is_bitget_error(exc, "40774"):
            retry_payload = dict(payload)
            if str(retry_payload.get("side", "")).endswith("_single"):
                retry_payload["side"] = base_side
                retry_payload["tradeSide"] = "open"
            else:
                retry_payload["side"] = f"{base_side}_single"
                retry_payload.pop("tradeSide", None)
            res = _request(
                api_key,
                api_secret,
                api_passphrase,
                "POST",
                "/api/v2/mix/order/place-order",
                data=retry_payload,
                max_retries=1,
            )
        else:
            raise
    if isinstance(res, dict):
        res["_calculated_size"] = size_str
        res["_limit_price"] = price_str
    return res


def place_limit_order(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    usdt_margin: float,
    limit_price: float,
    product_type: str = "USDT-FUTURES",
    client_oid: str = "",
    pos_mode: str = "2",
) -> dict:
    symbol = _clean_symbol(symbol)
    size_str = _calc_size(usdt_margin, leverage, limit_price)
    res = place_limit_order_by_size(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        margin_mode=margin_mode,
        size=size_str,
        limit_price=limit_price,
        product_type=product_type,
        client_oid=client_oid,
        pos_mode=pos_mode,
        sync_leverage=True,
        post_only=True,
    )
    if isinstance(res, dict):
        res["_calculated_size"] = size_str
    return res


def get_order_detail(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    order_id: str = "",
    client_oid: str = "",
    product_type: str = "USDT-FUTURES",
) -> dict:
    symbol = _clean_symbol(symbol)
    params = {"symbol": symbol, "productType": _product_type(product_type)}
    if order_id:
        params["orderId"] = str(order_id)
    elif client_oid:
        params["clientOid"] = client_oid
    else:
        raise ValueError("get_order_detail ?? order_id ? client_oid")
    data = _request(
        api_key,
        api_secret,
        api_passphrase,
        "GET",
        "/api/v2/mix/order/detail",
        params=params,
    )
    return data if isinstance(data, dict) else {}


def cancel_order(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    order_id: str = "",
    client_oid: str = "",
    product_type: str = "USDT-FUTURES",
) -> dict:
    symbol = _clean_symbol(symbol)
    payload = {"symbol": symbol, "productType": _product_type(product_type)}
    if order_id:
        payload["orderId"] = str(order_id)
    elif client_oid:
        payload["clientOid"] = client_oid
    else:
        raise ValueError("cancel_order ?? order_id ? client_oid")
    data = _request(
        api_key,
        api_secret,
        api_passphrase,
        "POST",
        "/api/v2/mix/order/cancel-order",
        data=payload,
        max_retries=1,
    )
    return data if isinstance(data, dict) else {}


def _calc_size(usdt_margin: float, leverage: int, price: float) -> str:
    """
    将 USDT 保证金换算为合约张数（以 USDT 计价的合约，1张=1USDT面值）。
    size = (保证金 × 杠杆) / 当前价格，保留4位小数，最小0.0001。
    """
    if price <= 0:
        raise ValueError(f"行情价非正值: {price}")
    raw_size = (usdt_margin * leverage) / price
    # 截断到4位小数，避免四舍五入超出可下单精度
    size = int(raw_size * 10000) / 10000.0
    if size <= 0:
        raise ValueError(f"换算后张数为0或负数（保证金={usdt_margin}, 杠杆={leverage}x, 价格={price}）")
    return f"{size:.4f}".rstrip("0").rstrip(".")


def set_symbol_leverage(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    pos_mode: str = "2",
    product_type: str = "USDT-FUTURES",
) -> dict:
    """
    强制同步合约杠杆到指定值。
    在双向模式下带上 holdSide，确保多空方向杠杆分别可控。
    """
    symbol = _clean_symbol(symbol)
    lev = max(1, int(leverage))
    norm_pos_mode = _normalize_pos_mode(pos_mode)
    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginCoin": "USDT",
        "marginMode": _normalize_margin_mode(margin_mode),
        "leverage": str(lev),
    }
    if norm_pos_mode == "2":
        payload["holdSide"] = "long" if direction.lower() == "long" else "short"
    return _request(
        api_key,
        api_secret,
        api_passphrase,
        "POST",
        "/api/v2/mix/account/set-leverage",
        data=payload,
        max_retries=2,
    )


def place_market_order_by_size(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    size: float | str,
    product_type: str = "USDT-FUTURES",
    client_oid: str = "",
    pos_mode: str = "2",
    sync_leverage: bool = True,
) -> dict:
    """
    按指定张数开仓（严格同步仓位变化时使用）。
    """
    symbol = _clean_symbol(symbol)
    size_str = _normalize_size(size)
    base_side = "buy" if direction.lower() == "long" else "sell"
    norm_pos_mode = _normalize_pos_mode(pos_mode)
    side = f"{base_side}_single" if norm_pos_mode == "1" else base_side

    def _do_set_leverage(mm: str) -> None:
        if sync_leverage:
            set_symbol_leverage(
                api_key, api_secret, api_passphrase,
                symbol=symbol, direction=direction, leverage=leverage,
                margin_mode=mm, pos_mode=norm_pos_mode or "2",
                product_type=product_type,
            )

    try:
        _do_set_leverage(margin_mode)
    except Exception as exc:
        if _resolve_simulated() and _is_margin_mode_error(exc):
            fallback = "crossed" if _normalize_margin_mode(margin_mode) != "crossed" else "isolated"
            logger.warning("模拟盘 400172(杠杆设置)，改用 marginMode=%s: %s", fallback, symbol)
            _do_set_leverage(fallback)
            margin_mode = fallback
        else:
            raise

    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginMode": _normalize_margin_mode(margin_mode),
        "marginCoin": "USDT",
        "size": size_str,
        "side": side,
        "orderType": "market",
        "leverage": str(max(1, int(leverage))),
    }
    if norm_pos_mode == "2":
        payload["tradeSide"] = "open"
    if client_oid:
        payload["clientOid"] = client_oid

    logger.info(
        "按张数下单: %s %s x%d 张数=%s 模式=%s 防重ID=%s",
        symbol,
        side.upper(),
        max(1, int(leverage)),
        size_str,
        norm_pos_mode or "unknown",
        client_oid,
    )
    try:
        res = _request(
            api_key,
            api_secret,
            api_passphrase,
            "POST",
            "/api/v2/mix/order/place-order",
            data=payload,
        )
    except Exception as exc:
        # 模拟盘 400172 保证金模式不合法：改用另一种 marginMode 重试
        if _resolve_simulated() and _is_margin_mode_error(exc):
            fallback = "crossed"
            if _normalize_margin_mode(margin_mode) == "crossed":
                fallback = "isolated"
            logger.warning(
                "模拟盘 400172，改用 marginMode=%s 重试: %s %s",
                fallback, symbol, side.upper()
            )
            payload_retry = {**payload, "marginMode": fallback}
            if sync_leverage:
                set_symbol_leverage(
                    api_key, api_secret, api_passphrase,
                    symbol=symbol, direction=direction, leverage=leverage,
                    margin_mode=fallback, pos_mode=norm_pos_mode or "2",
                    product_type=product_type,
                )
            res = _request(
                api_key, api_secret, api_passphrase,
                "POST", "/api/v2/mix/order/place-order",
                data=payload_retry,
            )
        elif _is_bitget_error(exc, "40774"):
            retry_payload = dict(payload)
            if str(retry_payload.get("side", "")).endswith("_single"):
                retry_payload["side"] = base_side
                retry_payload["tradeSide"] = "open"
            else:
                retry_payload["side"] = f"{base_side}_single"
                retry_payload.pop("tradeSide", None)
            logger.warning(
                "按张数下单触发 40774，切换下单类型后重试: symbol=%s side=%s",
                symbol,
                retry_payload["side"],
            )
            res = _request(
                api_key,
                api_secret,
                api_passphrase,
                "POST",
                "/api/v2/mix/order/place-order",
                data=retry_payload,
                max_retries=1,
            )
        else:
            raise
    if isinstance(res, dict):
        res["_calculated_size"] = size_str
    return res


def place_market_order(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    leverage: int,
    margin_mode: str,
    usdt_margin: float,
    product_type: str = "USDT-FUTURES",
    current_price: float = 0.0,
    client_oid: str = "",
    pos_mode: str = "2",
) -> dict:
    """
    按市价开仓。
    client_oid 被用于防重复开单机制（幂等性保证）。
    """
    # 获取当前价格以换算合约张数
    symbol = _clean_symbol(symbol)
    price = current_price if current_price > 0 else get_ticker_price(symbol, product_type)
    size_str = _calc_size(usdt_margin, leverage, price)
    res = place_market_order_by_size(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        margin_mode=margin_mode,
        size=size_str,
        product_type=product_type,
        client_oid=client_oid,
        pos_mode=pos_mode,
        sync_leverage=True,
    )
    if isinstance(res, dict):
        res["_calculated_size"] = size_str
    return res


def close_position(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    product_type: str = "USDT-FUTURES",
) -> dict:
    """一把梭平整个方向的仓位（容易误杀同币种其他跟单者的仓位，已不推荐使用）。"""
    symbol = _clean_symbol(symbol)
    hold_side = "long" if direction.lower() == "long" else "short"
    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "holdSide": hold_side,
    }
    return _request(
        api_key,
        api_secret,
        api_passphrase,
        "POST",
        "/api/v2/mix/order/close-positions",
        data=payload,
    )


def close_partial_position(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    symbol: str,
    direction: str,
    qty_str: str,
    pos_mode: str,
    margin_mode: str,
    product_type: str = "USDT-FUTURES",
) -> dict:
    """
    精确局部平仓：支持合并仓位下的分拆平仓（基于用户账户是单向模式还是双向模式）。
    pos_mode: "1" 为单向持仓模式， "2" 为双向(Hedge)持仓模式。
    """
    symbol = _clean_symbol(symbol)
    norm_pos_mode = _normalize_pos_mode(pos_mode)
    is_closing_long = (direction.lower() == "long")
    base_side = "sell" if is_closing_long else "buy"
    side = f"{base_side}_single" if norm_pos_mode == "1" else base_side
    
    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginCoin": "USDT",
        "marginMode": _normalize_margin_mode(margin_mode),
        "size": qty_str,
        "side": side,
        "orderType": "market",
    }
    
    if norm_pos_mode == "1":
        # 单向模式：下单必须带 reduceOnly
        payload["reduceOnly"] = "YES"
    else:
        # 双向模式(Hedge)：需要显式指出 tradeSide=close
        payload["tradeSide"] = "close"

    logger.info("局部平仓: %s %s 数量=%s mode=%s", symbol, direction.upper(), qty_str, pos_mode)
    try:
        return _request(
            api_key,
            api_secret,
            api_passphrase,
            "POST",
            "/api/v2/mix/order/place-order",
            data=payload,
        )
    except Exception as exc:
        if not _is_bitget_error(exc, "40774"):
            raise
        retry_payload = dict(payload)
        if str(retry_payload.get("side", "")).endswith("_single"):
            retry_payload["side"] = base_side
            retry_payload.pop("reduceOnly", None)
            retry_payload["tradeSide"] = "close"
        else:
            retry_payload["side"] = f"{base_side}_single"
            retry_payload.pop("tradeSide", None)
            retry_payload["reduceOnly"] = "YES"
        logger.warning(
            "局部平仓触发 40774，切换下单类型后重试: symbol=%s side=%s",
            symbol,
            retry_payload["side"],
        )
        return _request(
            api_key,
            api_secret,
            api_passphrase,
            "POST",
            "/api/v2/mix/order/place-order",
            data=retry_payload,
            max_retries=1,
        )
