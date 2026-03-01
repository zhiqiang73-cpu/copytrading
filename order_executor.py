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
    params: dict | None = None,
    data: dict | None = None,
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
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("code", "0")
            if str(code) != "00000":
                raise ValueError(
                    f"API error {code}: {payload.get('msg', '')} | {endpoint}"
                )
            return payload.get("data")
        except (requests.RequestException, ValueError) as exc:
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
    
    # 也可以专门起一个接口查模式，但 /account/account 通常带 posMode，若没有默认为双向(2)以防万一
    pos_mode = res.get("posMode", "2")
    res["_posMode"] = str(pos_mode)
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
) -> dict:
    """
    按市价开仓。
    client_oid 被用于防重复开单机制（幂等性保证）。
    """
    side = "buy" if direction.lower() == "long" else "sell"

    # 获取当前价格以换算合约张数
    price = current_price if current_price > 0 else get_ticker_price(symbol, product_type)
    size_str = _calc_size(usdt_margin, leverage, price)

    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginMode": margin_mode,
        "marginCoin": "USDT",
        "size": size_str,
        "side": side,
        "orderType": "market",
        "leverage": str(int(leverage)),
    }
    if client_oid:
        payload["clientOid"] = client_oid

    logger.info(
        "下单: %s %s x%d  保证金=%.2fUSDT  张数=%s  防重ID=%s",
        symbol, side.upper(), leverage, usdt_margin, size_str, client_oid
    )
    res = _request(
        api_key,
        api_secret,
        api_passphrase,
        "POST",
        "/api/v2/mix/order/place-order",
        data=payload,
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
    is_closing_long = (direction.lower() == "long")
    side = "sell" if is_closing_long else "buy"
    
    payload = {
        "symbol": symbol,
        "productType": _product_type(product_type),
        "marginCoin": "USDT",
        "marginMode": margin_mode,
        "size": qty_str,
        "side": side,
        "orderType": "market",
    }
    
    if str(pos_mode) == "1":
        # 单向模式：下单必须带 reduceOnly
        payload["reduceOnly"] = "YES"
    else:
        # 双向模式(Hedge)：需要显式指出 tradeSide=close
        payload["tradeSide"] = "close"

    logger.info("局部平仓: %s %s 数量=%s mode=%s", symbol, direction.upper(), qty_str, pos_mode)
    return _request(
        api_key,
        api_secret,
        api_passphrase,
        "POST",
        "/api/v2/mix/order/place-order",
        data=payload,
    )
