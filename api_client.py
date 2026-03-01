"""
Bitget REST API 封装 — V2
- HMAC-SHA256 签名（使用实际 URL query string，与 V2 规范一致）
- 请求重试 + 指数退避
- 简单令牌桶限流（5 req/s，V2 限制）

重要背景：
  Bitget V1 API 已于 2025 年全面下线（error 30032）。
  V2 的跟单接口只能读取「你自己已跟单的交易员」数据，
  不再支持通过 UID 任意查询陌生交易员的持仓/历史。
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from threading import Lock
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

# ── 令牌桶限流 ────────────────────────────────────────────────────────────────
_rate_lock    = Lock()
_tokens       = 5.0
_max_tokens   = 5.0
_refill_rate  = 5.0          # 每秒补充 5 个令牌（V2 限制更保守）
_last_refill  = time.monotonic()


def _acquire_token():
    """阻塞直到获取一个令牌，最大等待 30 秒。"""
    global _tokens, _last_refill
    deadline = time.monotonic() + 30
    while True:
        with _rate_lock:
            now = time.monotonic()
            elapsed = now - _last_refill
            _tokens = min(_max_tokens, _tokens + elapsed * _refill_rate)
            _last_refill = now
            if _tokens >= 1.0:
                _tokens -= 1.0
                return
        time.sleep(0.05)
        if time.monotonic() > deadline:
            raise TimeoutError("rate limiter: token wait timeout")


# ── 签名 ─────────────────────────────────────────────────────────────────────

def _sign(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """
    生成 Bitget ACCESS-SIGN。
    request_path 必须是包含实际 query string 的完整路径，
    与 requests 库实际发送的 URL 完全一致（V2 严格校验）。
    """
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(
        config.BITGET_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _make_signed_headers(
    method: str,
    endpoint: str,
    params: dict | None = None,
    body_str: str = "",
) -> tuple[dict, str]:
    """
    构造带签名的请求头。
    返回 (headers_dict, actual_url_with_qs)。

    关键：用 requests.PreparedRequest 构造实际 URL，
    确保签名中的 query string 与发送的 URL 100% 一致。
    """
    base_url = config.BASE_URL + endpoint
    # 让 requests 负责 URL encoding，避免手动排序导致的签名失配
    req_obj = requests.Request(method, base_url, params=params)
    prepared = req_obj.prepare()
    actual_url = prepared.url  # 含 query string 的完整 URL
    parsed = urllib.parse.urlparse(actual_url)
    request_path = parsed.path + ("?" + parsed.query if parsed.query else "")

    ts = str(int(time.time() * 1000))
    headers = {
        "ACCESS-KEY":        config.BITGET_API_KEY,
        "ACCESS-SIGN":       _sign(ts, method, request_path, body_str),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": config.BITGET_PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "zh-CN",
    }
    return headers, actual_url


# ── 核心请求 ──────────────────────────────────────────────────────────────────

def _request(
    method: str,
    endpoint: str,
    params: dict | None = None,
    data: dict | None = None,
    max_retries: int = 3,
) -> Any:
    """
    发送 API 请求，含重试与指数退避。
    返回 response['data'] 字段，若失败则抛出异常。
    """
    body_str = json.dumps(data) if data else ""

    for attempt in range(1, max_retries + 1):
        _acquire_token()
        headers, actual_url = _make_signed_headers(method, endpoint, params, body_str)
        try:
            if method.upper() == "GET":
                resp = requests.get(actual_url, headers=headers, timeout=15)
            else:
                resp = requests.post(
                    config.BASE_URL + endpoint,
                    headers=headers,
                    data=body_str if body_str else None,
                    timeout=15,
                )
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("code", "0")
            if str(code) == "30032":
                raise RuntimeError(
                    f"Bitget V1 API 已下线，接口 {endpoint} 需要迁移到 V2 版本"
                )
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


# ── V2 业务 API ───────────────────────────────────────────────────────────────

def get_followed_traders(page: int = 1, page_size: int = 20) -> list[dict]:
    """
    获取当前账号已跟单的交易员列表。
    V2: GET /api/v2/copy/mix-follower/query-traders
    返回列表，每项含 traderId / traderName / profitRate 等字段。
    """
    result = _request("GET", "/api/v2/copy/mix-follower/query-traders", params={
        "pageNo":   str(page),
        "pageSize": str(page_size),
    })
    if isinstance(result, list):
        return result
    # 有些版本包在 data 里
    if isinstance(result, dict):
        for key in ("traderList", "list", "data"):
            if key in result and isinstance(result[key], list):
                return result[key]
    return []


def get_current_copy_orders(product_type: str = "USDT-FUTURES",
                             page: int = 1, page_size: int = 20) -> list[dict]:
    """
    获取账号当前持有的跟单持仓。
    V2: GET /api/v2/copy/mix-follower/query-current-orders
    """
    result = _request("GET", "/api/v2/copy/mix-follower/query-current-orders", params={
        "productType": product_type,
        "pageNo":      str(page),
        "pageSize":    str(page_size),
    })
    return _extract_tracking_list(result)


def get_history_copy_orders(
    product_type: str = "USDT-FUTURES",
    days: int = 30,
    page: int = 1,
    page_size: int = 20,
) -> list[dict]:
    """
    获取账号历史跟单记录。
    V2: GET /api/v2/copy/mix-follower/query-history-orders
    时间范围最多 90 天。
    """
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - min(days, 89) * 24 * 3600 * 1000
    result = _request("GET", "/api/v2/copy/mix-follower/query-history-orders", params={
        "productType": product_type,
        "startTime":   str(start_ms),
        "endTime":     str(end_ms),
        "pageNo":      str(page),
        "pageSize":    str(page_size),
    })
    return _extract_tracking_list(result)


# URL 参数 rule → sortRule 映射（供批量扫描 UI 使用，实际已无法调用公开排行榜）
_SORT_RULE_MAP = {
    "0": "composite",
    "1": "profitLoss",
    "2": "profitRate",
    "3": "followerNum",
    "4": "winRate",
    "5": "composite",
}


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _extract_tracking_list(data: Any) -> list[dict]:
    """兼容 V2 跟单接口的多种返回格式。"""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("trackingList", "orderList", "list", "data", "orders"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _extract_orders(data: Any) -> list[dict]:
    """兼容旧代码调用的通用提取函数。"""
    return _extract_tracking_list(data)
