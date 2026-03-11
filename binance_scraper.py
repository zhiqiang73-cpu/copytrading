"""
binance_scraper.py — 实时监控币安跟单交易员的操作记录
无需登录，通过公开的 order-history 接口轮询识别开仓/平仓信号

开仓/平仓判断逻辑（双向持仓模式）:
  BUY  + LONG  → 开多
  SELL + LONG  → 平多
  SELL + SHORT → 开空
  BUY  + SHORT → 平空
"""
from __future__ import annotations

import logging
import time
import hashlib
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_BN_BASE = "https://www.binance.com"
_BN_FAPI_BASE = config.BINANCE_BASE_URL


def _resolve_fapi_base(base_url: str | None = None) -> str:
    return str(base_url or config.BINANCE_BASE_URL or _BN_FAPI_BASE).strip().rstrip("/")

_BN_TIME_OFFSET_MS = 0
_BN_TIME_OFFSET_AT = 0.0
_BN_TIME_OFFSET_TTL_SEC = 60
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "zh-CN",
}


def _build_record_id(row: dict) -> str:
    """
    生成稳定唯一的记录 ID。
    Binance 部分公开返回没有 orderId，这里用关键字段签名兜底，避免仅靠时间戳去重。
    """
    for key in (
        "orderId",
        "id",
        "orderNo",
        "tradeId",
        "clientOrderId",
        "origClientOrderId",
    ):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)

    signature = "|".join(
        [
            str(row.get("orderTime") or row.get("time") or ""),
            str(row.get("orderUpdateTime") or row.get("updateTime") or ""),
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            str(row.get("positionSide") or ""),
            str(row.get("executedQty") or row.get("qty") or ""),
            str(row.get("avgPrice") or row.get("price") or ""),
            str(row.get("type") or ""),
        ]
    )
    digest = hashlib.md5(signature.encode("utf-8")).hexdigest()[:24]
    return f"bnsig_{digest}"


def _post(path: str, body: dict) -> Optional[dict]:
    """POST 到币安公开接口"""
    try:
        resp = requests.post(
            _BN_BASE + path,
            json=body,
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()  # 检查 HTTP 错误
        data = resp.json()
        if data.get("code") == "000000" or "data" in data:
            return data.get("data")
        error_msg = data.get("msg", "")
        logger.warning("Binance API %s returned error: code=%s msg=%s", path, data.get("code"), error_msg[:200])
        return None
    except requests.exceptions.Timeout:
        logger.error("Binance API %s timeout (10s) - 网络可能有问题", path)
        return None
    except requests.exceptions.ConnectionError:
        logger.error("Binance API %s 连接失败 - 检查网络", path)
        return None
    except requests.exceptions.HTTPError as e:
        logger.error("Binance API %s HTTP 错误 %s: %s", path, e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        logger.error("Binance API %s 未知错误: %s", path, str(e)[:200])
        return None


def fetch_operation_records(portfolio_id: str, page_size: int = 20) -> list[dict]:
    """
    拉取交易员最近的操作记录（最新操作记录列表）。
    返回标准化列表，每条包含:
      - order_id: 订单唯一ID
      - symbol: 交易对 (如 BTCUSDT)
      - action: 'open_long' | 'close_long' | 'open_short' | 'close_short'
      - direction: 'long' | 'short'
      - qty: 成交数量 (float)
      - price: 成交均价 (float)
      - order_time: 时间戳毫秒 (int)
      - pnl: 盈亏 (float，仅平仓单有意义)
    """
    now_ms = int(time.time() * 1000)
    # 默认拉最近 7 天
    start_ms = now_ms - 7 * 24 * 3600 * 1000

    data = _post(
        "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
        {
            "portfolioId": portfolio_id,
            "startTime": start_ms,
            "endTime": now_ms,
            "pageSize": page_size,
        },
    )

    # 响应可能是 list 或 dict
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("list", "rows", "orders", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break

    result = []
    for r in rows:
        try:
            side = (r.get("side") or "").upper()          # BUY / SELL
            pos_side = (r.get("positionSide") or "").upper()  # LONG / SHORT / BOTH

            # 推断 action
            if pos_side == "BOTH":
                # 单向模式：BUY=开多/平空，SELL=开空/平多，需要配合 reduceOnly
                reduce_only = r.get("reduceOnly", False)
                if side == "BUY":
                    action = "close_short" if reduce_only else "open_long"
                    direction = "short" if reduce_only else "long"
                else:
                    action = "close_long" if reduce_only else "open_short"
                    direction = "long" if reduce_only else "short"
            else:
                # 双向模式（更常见）
                if side == "BUY" and pos_side == "LONG":
                    action, direction = "open_long", "long"
                elif side == "SELL" and pos_side == "LONG":
                    action, direction = "close_long", "long"
                elif side == "SELL" and pos_side == "SHORT":
                    action, direction = "open_short", "short"
                elif side == "BUY" and pos_side == "SHORT":
                    action, direction = "close_short", "short"
                else:
                    logger.debug("无法识别 side=%s positionSide=%s，跳过", side, pos_side)
                    continue

            result.append({
                "order_id":   _build_record_id(r),
                "symbol":     r.get("symbol", ""),
                "action":     action,
                "direction":  direction,
                "qty":        float(r.get("executedQty") or r.get("qty") or 0),
                "price":      float(r.get("avgPrice") or r.get("price") or 0),
                "order_time": int(r.get("orderTime") or r.get("time") or 0),
                "pnl":        float(r.get("totalPnl") or r.get("pnl") or 0),
                "leverage":   int(r.get("leverage") or 1),
                "_raw":       r,
            })
        except Exception as e:
            logger.warning("解析操作记录异常: %s | row=%s", e, str(r)[:200])

    return result


def fetch_latest_orders(portfolio_id: str, since_ms: int = 0, limit: int = 50) -> list[dict]:
    """
    只返回比 since_ms 更新的操作记录（用于实时轮询）。
    """
    records = fetch_operation_records(portfolio_id, page_size=limit)
    if since_ms <= 0:
        return records
    return [r for r in records if r["order_time"] > since_ms]


def parse_binance_url(url: str) -> Optional[str]:
    """
    从币安跟单员 URL 提取 portfolio_id。
    支持格式：
      - https://www.binance.com/zh-CN/copy-trading/lead-details/4751838302089254401
      - https://www.binance.com/en/copy-trading/lead-details/4751838302089254401
    """
    import re
    match = re.search(r'/lead-details/(\d+)', url)
    if match:
        return match.group(1)
    return None


def _get(url: str) -> Optional[dict]:
    """GET 请求币安公开接口"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "000000" or "data" in data:
            return data.get("data")
        return None
    except Exception as e:
        logger.error("Binance GET %s 失败: %s", url[:60], str(e)[:100])
        return None


def fetch_trader_info(portfolio_id: str) -> dict:
    """
    拉取币安交易员的详细信息（昵称、ROI、胜率等）。
    使用正确的 /detail GET 接口。
    """
    logger.info("正在获取币安交易员信息: %s", portfolio_id[:12])
    
    # 使用正确的 API 端点
    url = f"{_BN_BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId={portfolio_id}"
    data = _get(url)
    
    if isinstance(data, dict):
        nickname = data.get("nickname") or data.get("nickName") or f"币安交易员_{portfolio_id[:8]}"
        logger.info("成功获取币安交易员: %s", nickname)
        
        # Binance API 不直接返回 ROI 和胜率，显示跟单者收益代替
        copier_pnl = _safe_float(data.get("copierPnl") or 0)  # 跟单者总收益
        aum = _safe_float(data.get("aumAmount") or 0)  # 管理资产
        
        # 当前跟随人数
        follower_count = int(data.get("currentCopyCount") or data.get("totalCopyCount") or 0)
        
        # 带单次数
        close_lead_count = int(data.get("closeLeadCount") or 0)
        
        return {
            "portfolio_id": portfolio_id,
            "nickname": nickname.strip(),
            "roi": None,  # Binance 不公开 ROI
            "win_rate": None,  # Binance 不公开胜率
            "follower_count": follower_count,
            "total_trades": close_lead_count,
            "avatar": data.get("avatarUrl") or "",
            "copier_pnl": copier_pnl,  # 跟单者总收益
            "aum": aum,  # 管理资产
            "status": data.get("status", "ACTIVE"),
            "margin_balance": _safe_float(data.get("marginBalance") or 0),
        }
    
    # API 失败，返回基础信息
    logger.warning("币安 API 获取失败，使用基础信息: %s", portfolio_id[:12])
    return {
        "portfolio_id": portfolio_id,
        "nickname": f"币安交易员_{portfolio_id[:8]}",
        "roi": 0.0,
        "win_rate": 0.0,
        "follower_count": 0,
        "total_trades": 0,
        "avatar": "",
        "copy_trade_days": 0,
        "aum": 0.0,
        "_warning": "详细信息获取失败，使用基础信息",
    }


def _safe_float(val) -> float:
    """安全地转换为浮点数"""
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            return float(val.replace("%", "").strip())
        return 0.0
    except (ValueError, TypeError):
        return 0.0


# ── 币安 FAPI 签名接口（需要 API Key + Secret） ─────────────────────────────

def _bn_refresh_server_time_offset(force: bool = False, base_url: str | None = None) -> None:
    global _BN_TIME_OFFSET_MS, _BN_TIME_OFFSET_AT
    now = time.time()
    if (not force) and _BN_TIME_OFFSET_AT and (now - _BN_TIME_OFFSET_AT) < _BN_TIME_OFFSET_TTL_SEC:
        return
    resp = requests.get(f"{_resolve_fapi_base(base_url)}/fapi/v1/time", timeout=5)
    resp.raise_for_status()
    payload = resp.json()
    server_ms = int(payload["serverTime"])
    local_ms = int(time.time() * 1000)
    _BN_TIME_OFFSET_MS = server_ms - local_ms
    _BN_TIME_OFFSET_AT = time.time()


def _bn_signed_timestamp_ms(base_url: str | None = None) -> int:
    try:
        _bn_refresh_server_time_offset(force=False, base_url=base_url)
    except Exception as exc:
        logger.debug("sync binance time failed in scraper: %s", exc)
    return int(time.time() * 1000) + int(_BN_TIME_OFFSET_MS) - 500


def get_binance_futures_balance(api_key: str, api_secret: str, base_url: str | None = None) -> dict:
    """
    调用币安合约 FAPI 签名接口获取 USDT 余额。
    返回: {"balance": float, "available": float, "unrealized_pnl": float}
    """
    import hmac
    import hashlib
    import urllib.parse

    base_url = _resolve_fapi_base(base_url)
    endpoint = "/fapi/v2/balance"
    timestamp = _bn_signed_timestamp_ms(base_url=base_url)

    params = {
        "timestamp": timestamp,
        "recvWindow": 5000,
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query_string += f"&signature={signature}"

    headers = {
        "X-MBX-APIKEY": api_key,
    }

    try:
        resp = requests.get(
            f"{base_url}{endpoint}?{query_string}",
            headers=headers,
            timeout=10,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                body = e.response.json()
            except Exception:
                body = {}
            if body.get("code") == -1021:
                _bn_refresh_server_time_offset(force=True, base_url=base_url)
                params["timestamp"] = _bn_signed_timestamp_ms(base_url=base_url)
                query_string = urllib.parse.urlencode(params)
                signature = hmac.new(
                    api_secret.encode("utf-8"),
                    query_string.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                query_string += f"&signature={signature}"
                resp = requests.get(
                    f"{base_url}{endpoint}?{query_string}",
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
            else:
                raise
        data = resp.json()

        # data 是一个列表，找到 USDT 资产
        for asset in data:
            if asset.get("asset") == "USDT":
                return {
                    "balance": float(asset.get("balance", 0)),
                    "available": float(asset.get("availableBalance", 0)),
                    "unrealized_pnl": float(asset.get("crossUnPnl", 0)),
                }

        # 没有 USDT 资产
        return {"balance": 0.0, "available": 0.0, "unrealized_pnl": 0.0}

    except requests.exceptions.HTTPError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text[:200]
        logger.error("币安 FAPI 余额查询失败 HTTP %s: %s", e.response.status_code, error_body)
        raise RuntimeError(f"币安 API 错误: {error_body}") from e
    except requests.exceptions.Timeout:
        raise RuntimeError("币安 API 连接超时") from None
    except requests.exceptions.ConnectionError:
        raise RuntimeError("无法连接币安 API，请检查网络") from None
    except Exception as e:
        raise RuntimeError(f"币安 API 未知错误: {str(e)[:200]}") from e


def get_binance_futures_income_today(api_key: str, api_secret: str, base_url: str | None = None) -> float:
    """
    查询币安合约账户今日已实现收益（REALIZED_PNL + COMMISSION + FUNDING_FEE）。
    """
    import hmac
    import hashlib
    import urllib.parse
    import datetime

    base_url = _resolve_fapi_base(base_url)
    endpoint = "/fapi/v1/income"

    # 今日零点（UTC+8）
    now = datetime.datetime.now()
    today_start = datetime.datetime(now.year, now.month, now.day)
    start_ms = int(today_start.timestamp() * 1000)
    timestamp = _bn_signed_timestamp_ms(base_url=base_url)

    params = {
        "timestamp": timestamp,
        "recvWindow": 5000,
        "startTime": start_ms,
        "limit": 1000,
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query_string += f"&signature={signature}"

    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.get(
            f"{base_url}{endpoint}?{query_string}",
            headers=headers,
            timeout=10,
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                body = e.response.json()
            except Exception:
                body = {}
            if body.get("code") == -1021:
                _bn_refresh_server_time_offset(force=True, base_url=base_url)
                params["timestamp"] = _bn_signed_timestamp_ms(base_url=base_url)
                query_string = urllib.parse.urlencode(params)
                signature = hmac.new(
                    api_secret.encode("utf-8"),
                    query_string.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                query_string += f"&signature={signature}"
                resp = requests.get(
                    f"{base_url}{endpoint}?{query_string}",
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
            else:
                raise
        data = resp.json()

        total_income = 0.0
        for item in data:
            income_type = item.get("incomeType", "")
            if income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                total_income += float(item.get("income", 0))
        return total_income
    except Exception as e:
        logger.warning("币安今日收益查询失败: %s", str(e)[:100])
        return 0.0


# --- 快速测试 ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    # 测试 URL 解析
    url = "https://www.binance.com/zh-CN/copy-trading/lead-details/4751838302089254401"
    pid = parse_binance_url(url)
    print(f"=== 从 URL 提取的 Portfolio ID: {pid} ===")
    
    if pid:
        # 测试信息爬虫
        info = fetch_trader_info(pid)
        if info:
            import json
            print(f"\n=== 交易员信息 ===")
            print(json.dumps(info, ensure_ascii=False, indent=2))
        
        # 测试操作记录
        print(f"\n=== 最近操作记录 ===")
        records = fetch_operation_records(pid, page_size=10)
        if not records:
            print("无法获取数据（接口可能有防爬保护，需要 Cookie/Headers）")
        for r in records:
            import json
            safe = {k: v for k, v in r.items() if k != "_raw"}
            print(json.dumps(safe, ensure_ascii=False))