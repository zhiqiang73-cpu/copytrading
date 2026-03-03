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
import urllib.parse
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)


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
    # 交易对不存在属于参数错误，重试无意义，直接失败返回更快也更稳定。
    return (
        "code=40034" in msg
        or "code=40762" in msg
        or ("参数" in msg and "不存在" in msg)
        or ("订单金额" in msg and "超出账户余额" in msg)
        or "symbol does not exist" in lower_msg
        or "order amount exceeds account balance" in lower_msg
    )


def _normalize_size(size: float | str) -> str:
    v = float(size)
    if v <= 0:
        raise ValueError(f"下单张数必须为正数: {size}")
    # 截断到 4 位，和其他下单路径保持一致
    v = int(v * 10000) / 10000.0
    if v <= 0:
        raise ValueError(f"下单张数过小，截断后为 0: {size}")
    return f"{v:.4f}".rstrip("0").rstrip(".")


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
    if config.SIMULATED:
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
    查询合约当前行情价（不需要 API Key）。
    用于将 USDT 保证金换算成合约张数。
    """
    symbol = _clean_symbol(symbol)
    resp = requests.get(
        config.BASE_URL + "/api/v2/mix/market/ticker",
        params={"symbol": symbol, "productType": _product_type(product_type)},
        timeout=10,
        headers={"paptrading": "1"} if config.SIMULATED else {},
    )
    resp.raise_for_status()
    payload = resp.json()
    if str(payload.get("code", "0")) != "00000":
        raise ValueError(f"ticker error {payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("ticker API 返回格式异常")
    for key in ("last", "lastPr", "lastPrice", "close", "markPrice"):
        if data.get(key) is not None:
            return float(data[key])
    raise ValueError("ticker API 无价格字段")


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

    if sync_leverage:
        set_symbol_leverage(
            api_key,
            api_secret,
            api_passphrase,
            symbol=symbol,
            direction=direction,
            leverage=leverage,
            margin_mode=margin_mode,
            pos_mode=norm_pos_mode or "2",
            product_type=product_type,
        )

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
        if not _is_bitget_error(exc, "40774"):
            raise
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
