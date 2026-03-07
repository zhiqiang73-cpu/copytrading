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
from typing import Any

import requests

import config
import database as db
import order_executor
import binance_scraper

logger = logging.getLogger(__name__)

_engine: "CopyEngine | None" = None


_engine: CopyEngine | None = None

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


class CopyEngine:
    def __init__(self) -> None:
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
        settings = db.get_copy_settings()
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

        bg_available_usdt = 0.0
        bn_available_usdt = 0.0

        # Bitget 余额
        if bg_enabled:
            try:
                bal = order_executor.get_account_balance(bg_api_key, bg_secret, bg_pass)
                self._pos_mode = str(bal.get("_posMode", "2"))
                bg_available_usdt = _extract_balance_usdt(bal)
            except Exception as exc:
                logger.warning("Bitget 循环同步模式失败: %s", exc)
                bg_enabled = False

        # Binance 余额
        if bn_enabled:
            import binance_executor
            try:
                bn_bal = binance_executor.get_account_balance(bn_api_key, bn_secret)
                bn_available_usdt = _safe_float(bn_bal.get("availableBalance", 0.0))
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
                        bn_fallback_margin=bn_fallback_margin, bn_tol=bn_tol, bn_follow_ratio=bn_follow_ratio, bn_available_usdt=bn_available_usdt
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
            db.update_copy_settings(binance_traders=json.dumps(current_data))
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
        bn_fallback_margin: float,
        bn_tol: float,
        bn_follow_ratio: float,
        bn_available_usdt: float,
    ) -> None:
        """
        处理单条币安操作记录，分别映射到 Bitget 和 Binance 开仓或平仓。
        """
        action    = order["action"]      # open_long / close_long / open_short / close_short
        symbol    = order.get("symbol", "")
        direction = order["direction"]   # long / short
        price     = order["price"]
        order_id  = order["order_id"] or f"{pid}_{order['order_time']}"

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

                # ======== Bitget 处理 ========
                if bg_creds:
                    bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                    bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                    bg_target_margin, bg_ratio_note = self._apply_follow_ratio(src_margin, bg_follow_ratio, bg_fallback_margin)
                    self._execute_open_for_platform(
                        platform="bitget",
                        api_creds=(bg_ak, bg_sk, bg_pp),
                        pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                        direction=direction, price=price, lev=lev, tol=bg_tol,
                        fallback_margin=bg_fallback_margin, target_margin=bg_target_margin,
                        available_usdt=bg_available_usdt,
                        src_margin=src_margin, ratio_note=bg_ratio_note, client_oid=client_oid
                    )

                # ======== Binance 处理 ========
                if bn_creds:
                    bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                    bn_target_margin, bn_ratio_note = self._apply_follow_ratio(src_margin, bn_follow_ratio, bn_fallback_margin)
                    self._execute_open_for_platform(
                        platform="binance",
                        api_creds=(bn_ak, bn_sk, ""),
                        pid=pid, order_id=order_id, symbol=symbol,
                        direction=direction, price=price, lev=lev, tol=bn_tol,
                        fallback_margin=bn_fallback_margin, target_margin=bn_target_margin,
                        available_usdt=bn_available_usdt,
                        src_margin=src_margin, ratio_note=bn_ratio_note, client_oid=client_oid
                    )

            finally:
                with self._state_lock:
                    self._bn_inflight.discard(signal_key)

        elif action.startswith("close"):
            # ── 平仓 ──
            # ======== Bitget 处理 ========
            if bg_creds:
                bg_symbol_mapped = _binance_symbol_to_bitget(symbol)
                bg_ak, bg_sk, bg_pp = bg_creds["ak"], bg_creds["sk"], bg_creds["pp"]
                self._execute_close_for_platform(
                    platform="bitget",
                    api_creds=(bg_ak, bg_sk, bg_pp),
                    pid=pid, order_id=order_id, symbol=bg_symbol_mapped,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bg_tol
                )

            # ======== Binance 处理 ========
            if bn_creds:
                bn_ak, bn_sk = bn_creds["ak"], bn_creds["sk"]
                self._execute_close_for_platform(
                    platform="binance",
                    api_creds=(bn_ak, bn_sk, ""),
                    pid=pid, order_id=order_id, symbol=symbol,
                    direction=direction, price=price, order_pnl=order.get("pnl"), tol=bn_tol
                )

    def _execute_open_for_platform(
        self, platform: str, api_creds: tuple, pid: str, order_id: str, symbol: str,
        direction: str, price: float, lev: int, tol: float,
        fallback_margin: float, target_margin: float, available_usdt: float,
        src_margin: float, ratio_note: str, client_oid: str
    ):
        """通用单平台开仓执行器，分别供 Bitget 和 Binance 使用"""
        # 1. 幂等防重
        if db.has_tracking_no(pid, order_id, platform=platform):
            dup_key = f"{platform}:{pid}:{order_id}"
            if dup_key not in self._bn_dup_logged:
                logger.info("[%s信号跳过] 已处理过，不重复下单: pid=%s symbol=%s order_id=%s", platform.capitalize(), pid[:12], symbol, order_id)
                self._bn_dup_logged.add(dup_key)
            return

        margin, margin_note = self._cap_open_margin(
            target_margin,
            fallback_margin=fallback_margin,
            available_usdt=available_usdt,
            source_tag=platform.capitalize(),
            symbol=symbol,
            direction=direction,
        )
        precheck_note = ""

        def _insert_skip(reason: str, exec_p: float = 0.0, dev: float = 0.0):
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": margin,
                "source_price": price, "exec_price": exec_p,
                "deviation_pct": dev, "action": "open",
                "status": "skipped", "pnl": None,
                "notes": f"[跳过] {reason} {ratio_note} {margin_note} {precheck_note}".strip(), "exec_qty": 0.0,
                "platform": platform
            })

        if platform == "bitget" and symbol in self._unsupported_symbols:
            _insert_skip("Bitget 不支持该交易对(缓存)")
            return

        if margin <= 0:
            logger.warning(
                "[%s信号跳过] 无法推导保证金: %s %s price=%s lev=%s",
                platform.capitalize(), symbol, direction.upper(), price, lev
            )
            return

        # 2. 获取现价 & 容忍度检查
        try:
            if platform == "binance":
                import binance_executor
                curr_p = binance_executor.get_ticker_price(symbol)
                dev = abs(curr_p - price) / price if price > 0 else 1.0
                ok = dev <= tol
            else:
                ok, curr_p, dev = _price_ok(symbol, price, tol)
        except Exception as exc:
            if platform == "bitget" and _is_symbol_not_exist_error(exc):
                self._unsupported_symbols.add(symbol)
                _insert_skip(f"Bitget 不支持交易对: {exc}")
            elif platform == "binance" and _is_binance_symbol_error(exc):
                _insert_skip(f"Binance 不支持交易对: {exc}")
            else:
                logger.warning("[%s查价失败] %s %s: %s", platform.capitalize(), symbol, direction.upper(), exc)
            return

        if not ok:
            logger.warning("[%s价差过大暂缓] %s %s 信号=%.4f 现价=%.4f 偏差=%.2f%%", 
                           platform.capitalize(), symbol, direction.upper(), price, curr_p, dev * 100)
            return

        # 3. 最小下单量 / 交易状态预检查
        try:
            cap_limit = _cap_limit_value(fallback_margin, available_usdt)
            if platform == "binance":
                import binance_executor

                req = binance_executor.get_min_order_requirements(symbol, lev, curr_p)
                required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                if required_margin > 0 and margin + 1e-12 < required_margin:
                    if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                        _insert_skip(f"Binance 最小下单不足 need={required_margin:.4f} cap={cap_limit:.4f}")
                        return
                    precheck_note = f"[最小下单抬升] target={margin:.4f} min={required_margin:.4f}"
                    logger.info(
                        "[Binance最小下单抬升] %s %s %.4f -> %.4f",
                        symbol,
                        direction.upper(),
                        margin,
                        required_margin,
                    )
                    margin = required_margin
            else:
                req = order_executor.get_min_order_requirements(symbol, lev, curr_p)
                symbol_status = str(req.get("symbolStatus") or "").lower()
                if symbol_status and symbol_status != "normal":
                    self._unsupported_symbols.add(symbol)
                    _insert_skip(f"Bitget 合约不可交易 status={symbol_status}")
                    return

                limit_open_time = str(req.get("limitOpenTime") or "-1")
                if limit_open_time not in ("", "-1"):
                    _insert_skip(f"Bitget 当前不可开仓 limitOpenTime={limit_open_time}")
                    return

                required_margin = _safe_float(req.get("requiredMargin"), 0.0)
                if required_margin > 0 and margin + 1e-12 < required_margin:
                    if cap_limit > 0 and required_margin > cap_limit + 1e-12:
                        _insert_skip(f"Bitget 最小下单不足 need={required_margin:.4f} cap={cap_limit:.4f}")
                        return
                    precheck_note = f"[最小下单抬升] target={margin:.4f} min={required_margin:.4f}"
                    logger.info(
                        "[Bitget最小下单抬升] %s %s %.4f -> %.4f",
                        symbol,
                        direction.upper(),
                        margin,
                        required_margin,
                    )
                    margin = required_margin
        except Exception as exc:
            if platform == "bitget" and _is_symbol_not_exist_error(exc):
                self._unsupported_symbols.add(symbol)
                _insert_skip(f"Bitget 不支持交易对: {exc}")
            elif platform == "binance" and _is_binance_symbol_error(exc):
                _insert_skip(f"Binance 不支持交易对: {exc}")
            else:
                logger.warning("[%s最小下单预检失败] %s %s: %s", platform.capitalize(), symbol, direction.upper(), exc)
            return

        # 3. 发起下单
        ak, sk, pp = api_creds
        try:
            if platform == "binance":
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
                
            oid = res.get("orderId") or res.get("clientId") or ""
            exec_qty = float(res.get("_calculated_size", 0) if isinstance(res, dict) else 0)
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                "my_order_id": str(oid), "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": margin,
                "source_price": price, "exec_price": curr_p,
                "deviation_pct": dev, "action": "open",
                "status": "filled", "pnl": None, "notes": f"[{platform.capitalize()} Signal] src={src_margin:.4f} {ratio_note} {margin_note} {precheck_note}".strip(), "exec_qty": exec_qty,
                "platform": platform
            })
            logger.info("[%s开仓成功] %s %s 价=%.4f 量=%s", platform.capitalize(), symbol, direction.upper(), curr_p, exec_qty)
        except Exception as exc:
            if platform == "bitget" and _is_symbol_not_exist_error(exc):
                self._unsupported_symbols.add(symbol)
                _insert_skip(f"Bitget 不支持交易对: {exc}")
                return
            if platform == "bitget" and (_is_bitget_min_trade_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Bitget 最小下单不足: {exc}", curr_p, dev)
                return
            if platform == "bitget" and _is_bitget_balance_error(exc):
                _insert_skip(f"Bitget 余额不足: {exc}", curr_p, dev)
                return
            if platform == "binance" and (_is_binance_min_notional_error(exc) or _is_binance_symbol_error(exc) or _is_local_min_size_error(exc)):
                _insert_skip(f"Binance 最小下单不足: {exc}", curr_p, dev)
                return
            if platform == "binance" and _is_binance_balance_error(exc):
                _insert_skip(f"Binance 余额不足: {exc}", curr_p, dev)
                return
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": f"FAIL_{order_id}",
                "my_order_id": "", "symbol": symbol, "direction": direction,
                "leverage": lev, "margin_usdt": margin,
                "source_price": price, "exec_price": curr_p,
                "deviation_pct": dev, "action": "open",
                "status": "failed", "pnl": None, "notes": f"{exc} | {ratio_note} {margin_note} {precheck_note}".strip(), "exec_qty": 0.0,
                "platform": platform
            })
            logger.error("[%s开仓失败] %s: %s", platform.capitalize(), symbol, exc)


    def _execute_close_for_platform(
        self, platform: str, api_creds: tuple, pid: str, order_id: str, symbol: str,
        direction: str, price: float, order_pnl: float | None, tol: float
    ):
        """通用单平台平仓执行器"""
        if platform == "bitget" and symbol in self._unsupported_symbols:
            return

        # 检查是否平仓过 (幂等防重)
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
            logger.debug("[%s平仓信号] 本地未发现 %s的剩余持仓 (pid=%s)，跳过", platform.capitalize(), symbol, pid[:8])
            return

        # Binance 的数量精度可能不能轻易舍弃小数，但至少我们不增加新的小数
        close_qty = remaining_qty
        
        if close_qty <= 0:
            return

        ak, sk, pp = api_creds
        try:
            # 获取最新价并检查平仓滑点容忍度
            if platform == "binance":
                import binance_executor
                curr_p = binance_executor.get_ticker_price(symbol)
                dev = abs(curr_p - price) / price if price > 0 else 1.0
                ok = dev <= tol
            else:
                ok, curr_p, dev = _price_ok(symbol, price, tol)
                
            if not ok:
                logger.warning("[%s平仓暂缓] %s %s 信号价=%.4f 现价=%.4f 偏差=%.2f%% > %.2f%%", 
                               platform.capitalize(), symbol, direction.upper(), price, curr_p, dev * 100, tol * 100)
                return

            if platform == "binance":
                filters = binance_executor.get_symbol_filters(symbol)
                qty_str = binance_executor._format_qty(close_qty, filters["stepSize"])
                if float(qty_str) > 0:
                    binance_executor.close_partial_position(
                        ak, sk, symbol, direction, qty_str
                    )
                else:
                    logger.warning("[%s平仓信号] 数量过小被截断: %s", platform.capitalize(), close_qty)
                    return
            else:
                # Bitget
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
                "status": "filled", "pnl": order_pnl, "notes": f"[{platform.capitalize()} Signal] Close",
                "exec_qty": float(qty_str),
                "platform": platform
            })
            logger.info("[%s平仓成功] %s %s 数量=%s", platform.capitalize(), symbol, direction.upper(), qty_str)
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
            logger.error("[%s平仓失败] %s: %s", platform.capitalize(), symbol, exc)



def start_engine() -> None:
    global _engine
    if _engine is None:
        _engine = CopyEngine()
    _engine.start()

def stop_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.stop()

def is_engine_running() -> bool:
    return bool(_engine and _engine.is_running())
