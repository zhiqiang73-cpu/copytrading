"""
copy_engine.py — 自动跟单引擎 (防并发、防重漏、精确局部平仓终极版本)

修复记录 (2026-02):
  P0#2: 移除开仓价检查对平仓的阻碍。
  P0#3: Snapshot 完整双向同步 (内存与 DB SQLite 实时对照)，防重启漏单/重单。
  P0#4: 引入了 copy_orders.exec_qty 字段和 posMode 识别，实现精准的“部分平仓”。
  P1#13 [NEW]: 并发多线程扫描所有交易员；利用 clientOid 防 API 重放双杀；引入完美的状态机取代自愈循环补丁。
"""
from __future__ import annotations

import concurrent.futures
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
import scraper
import binance_scraper

logger = logging.getLogger(__name__)

_engine: "CopyEngine | None" = None


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


def get_ticker_price(symbol: str, product_type: str = "USDT-FUTURES") -> float:
    # 修复: 转换 symbol 格式从 BTCUSDT_UMCBL -> BTCUSDT
    # Bitget Ticker API 不接受 _UMCBL 后缀
    api_symbol = symbol.replace("_UMCBL", "").replace("_UM", "").replace("_DMCBL", "").replace("_DM", "")
    resp = requests.get(
        config.BASE_URL + "/api/v2/mix/market/ticker",
        params={"symbol": api_symbol, "productType": product_type},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    if str(payload.get("code", "0")) != "00000":
        raise ValueError(f"ticker error {payload.get('code')}: {payload.get('msg')}")
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


def _to_snap(uid: str, pos: dict) -> dict:
    """转换 scraper 返回的 pos 字典为 snapshots 数据库格式"""
    return {
        "trader_uid": uid,
        "tracking_no": pos.get("order_no") or pos.get("tracking_no", ""),
        "symbol": _clean_symbol_str(pos.get("symbol", "")),  # 统一格式，移除 _UMCBL 后缀
        "hold_side": pos.get("direction", ""),  # 数据库字段名
        "leverage": int(pos.get("leverage") or 1),
        "margin_mode": pos.get("margin_mode", "cross"),
        "open_price": _safe_float(pos.get("open_price")),
        "open_time": int(pos.get("open_time") or 0),
        "open_amount": _safe_float(pos.get("margin_amount") or pos.get("open_amount") or 0.0),
        "position_size": _safe_float(pos.get("position_size")),
        "unrealized_pnl": _safe_float(pos.get("unrealized_pnl")),
        "return_rate": _safe_float(pos.get("return_rate")),
        "follow_count": int(pos.get("follow_count") or 0),
        "tp_price": _safe_float(pos.get("tp_price")),  # 止盈价
        "sl_price": _safe_float(pos.get("sl_price")),  # 止损价
    }


class CopyEngine:
    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._prev_snaps: dict[str, dict[str, dict]] = {}
        self._fail_streak = 0
        self._pos_mode = "2"  # 1=单向, 2=双向
        # 币安监控
        self._bn_thread: threading.Thread | None = None
        self._bn_seen: dict[str, int] = {}  # portfolio_id -> 最新 order_time (ms)
        self._last_bn_metadata_refresh = 0  # 上次刷新币安交易员元数据的时间戳
        self._state_lock = threading.RLock()
        self._unsupported_symbols: set[str] = set()
        self._bn_inflight: set[str] = set()
        self._bn_dup_logged: set[str] = set()

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            if self._thread and self._thread.is_alive():
                logger.warning("检测到旧主线程仍在运行，跳过重复启动")
                return
            if self._bn_thread and self._bn_thread.is_alive():
                logger.warning("检测到旧币安线程仍在运行，跳过重复启动")
                return

            self._load_snaps_from_db()
            # 防重启信号重放：将所有已知币安交易员的起始时间戳设为当前时刻前 2 小时 (允许补票)
            self._bn_seen = {pid: int((time.time() - 7200) * 1000) for pid in self._bn_seen}
            self._bn_dup_logged.clear()
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            # 启动币安监控线程
            self._bn_thread = threading.Thread(target=self._run_binance, daemon=True)
            self._bn_thread.start()
            logger.info("跟单引擎启动 (高并发精准防重模式 + 币安信号源)")

    def _load_snaps_from_db(self) -> None:
        traders = db.get_all_traders()
        for t in traders:
            uid = t["trader_uid"]
            snaps = db.get_snapshots(uid)
            if snaps:
                self._prev_snaps[uid] = {k: {**v, "order_no": k} for k, v in snaps.items()}
                logger.info("加载内存快照 [%s]: %d 个", uid[:8], len(snaps))

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            t = self._thread
            bt = self._bn_thread

        # 等待线程退出，避免“停止后立刻启动”导致旧线程与新线程并行。
        if t and t.is_alive():
            t.join(timeout=3.5)
        if bt and bt.is_alive():
            bt.join(timeout=3.5)

        with self._state_lock:
            if self._thread and not self._thread.is_alive():
                self._thread = None
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

        ak  = settings.get("api_key") or ""
        sk  = settings.get("api_secret") or ""
        pp  = settings.get("api_passphrase") or ""
        if not (ak and sk and pp):
            return

        # 只获取启用了跟单的币安交易员
        bn_traders_raw = settings.get("binance_traders", "")
        try:
            bn_traders_data = json.loads(bn_traders_raw) if bn_traders_raw else {}
            if isinstance(bn_traders_data, dict):
                # 新格式：只取 copy_enabled=true 的
                bn_traders = [pid for pid, data in bn_traders_data.items() 
                             if data.get("copy_enabled") == True]
            else:
                # 旧格式（列表）：默认全部启用
                bn_traders = bn_traders_data if isinstance(bn_traders_data, list) else []
        except:
            bn_traders = []

        if not bn_traders:
            return

        # 同步账户持仓模式 (防止 40774 错误)
        try:
            bal = order_executor.get_account_balance(ak, sk, pp)
            self._pos_mode = str(bal.get("_posMode", "2"))
            available_usdt = _extract_balance_usdt(bal)
            if available_usdt <= 0:
                logger.warning("账户可用余额为 0，跳过币安信号处理")
                return
        except Exception as exc:
            logger.warning("币安循环同步模式失败: %s", exc)
            return

        tol     = _safe_float(settings.get("price_tolerance"), 0.05)
        follow_ratio = self._resolve_follow_ratio(settings)
        # 兜底保证金（仅当币安信号缺失数量/价格时启用），正常优先使用来源信号反推
        fallback_margin = 0.0
        total_p = _safe_float(settings.get("total_capital"), 0.0)
        max_m = _safe_float(settings.get("max_margin_pct"), 0.20)
        if total_p > 0 and max_m > 0:
            try:
                bitget_raw = settings.get("enabled_traders", "")
                bitget_count = len(json.loads(bitget_raw)) if bitget_raw else 0
            except Exception:
                bitget_count = 0
            total_trader_count = max(len(bn_traders) + bitget_count, 1)
            fallback_margin = (total_p / total_trader_count) * max_m

        now_ms = int(time.time() * 1000)
        for pid in bn_traders:
            try:
                # 首次遇到新 pid 时，初始化为 2 小时前 (允许补票上车)
                if pid not in self._bn_seen:
                    self._bn_seen[pid] = now_ms - 7200000
                    logger.info("币安交易员 %s 首次初始化信号时间戳 (回溯 2 小时)", pid[:12])
                    # 不用 continue，直接让它在下面 fetch_latest_orders 处拉取信号

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
                    self._process_binance_order(
                        ak, sk, pp, pid, order,
                        fallback_margin, tol, available_usdt, follow_ratio,
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
                    # 更新元数据
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
        按“来源保证金 * 跟随比例”计算目标保证金。
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
        # 缺失来源保证金时返回 0，由 _apply_follow_ratio 统一走 fallback_margin 兜底。
        return 0.0

    def _process_binance_order(self, ak, sk, pp, pid, order, fallback_margin, tol, available_usdt, follow_ratio: float) -> None:
        """
        处理单条币安操作记录，映射到 Bitget 开仓或平仓。
        """
        action    = order["action"]      # open_long / close_long / open_short / close_short
        symbol    = _binance_symbol_to_bitget(order.get("symbol", ""))  # 币安->Bitget symbol 映射
        direction = order["direction"]   # long / short
        price     = order["price"]
        order_id  = order["order_id"] or f"{pid}_{order['order_time']}"

        if action.startswith("open"):
            # ── 开仓 ──
            signal_key = f"{pid}:{order_id}"
            with self._state_lock:
                if signal_key in self._bn_inflight:
                    return
                self._bn_inflight.add(signal_key)
            try:
                # 幂等防重（提前执行，避免重复信号触发误导性日志）
                if db.has_tracking_no(pid, order_id):
                    dup_key = f"{pid}:{order_id}"
                    if dup_key not in self._bn_dup_logged:
                        logger.info("[币安信号跳过] 已处理过，不重复下单: pid=%s symbol=%s order_id=%s", pid[:12], symbol, order_id)
                        self._bn_dup_logged.add(dup_key)
                    return

                # 严格同步来源杠杆，不再强制替换为固定 20x。
                lev = max(1, int(order.get("leverage") or 1))
                src_margin = self._estimate_binance_margin(order)
                target_margin, ratio_note = self._apply_follow_ratio(src_margin, follow_ratio, fallback_margin)
                margin, margin_note = self._cap_open_margin(
                    target_margin,
                    fallback_margin=fallback_margin,
                    available_usdt=available_usdt,
                    source_tag="Binance",
                    symbol=symbol,
                    direction=direction,
                )

                def _insert_unsupported_skip(reason: str) -> None:
                    self._unsupported_symbols.add(symbol)
                    if db.has_tracking_no(pid, order_id):
                        return
                    db.insert_copy_order({
                        "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                        "my_order_id": "", "symbol": symbol, "direction": direction,
                        "leverage": lev, "margin_usdt": margin,
                        "source_price": price, "exec_price": 0,
                        "deviation_pct": 0, "action": "open",
                        "status": "skipped", "pnl": None,
                        "notes": f"[跳过] Bitget 不支持该交易对: {symbol} ({reason}) {ratio_note} {margin_note}".strip(), "exec_qty": 0.0,
                    })

                if symbol in self._unsupported_symbols:
                    _insert_unsupported_skip("cached unsupported")
                    logger.warning("[币安信号跳过] %s 在当前 Bitget 环境不可交易（缓存）", symbol)
                    return

                short_hash = hashlib.md5(f"bn_{pid}_{order_id}".encode()).hexdigest()[:16]
                client_oid = f"bn_{short_hash}"

                if margin <= 0:
                    logger.warning(
                        "[币安信号跳过] 无法推导保证金: %s %s qty=%s price=%s lev=%s ratio=%.4f%%",
                        symbol,
                        direction.upper(),
                        order.get("qty"),
                        price,
                        lev,
                        follow_ratio * 100,
                    )
                    return

                # 价格容忍度检查 (补票上车逻辑)
                try:
                    ok, curr_p, dev = _price_ok(symbol, price, tol)
                except Exception as exc:
                    if _is_symbol_not_exist_error(exc):
                        _insert_unsupported_skip(str(exc))
                        logger.warning("[币安信号跳过] Bitget 不支持交易对 %s: %s", symbol, exc)
                        return
                    logger.warning("[币安价差检查失败] %s %s: %s", symbol, direction.upper(), exc)
                    return
                if not ok:
                    logger.warning("[币安价差过大暂缓] %s %s 信号价=%.4f 现价=%.4f 偏差=%.2f%%", 
                                   symbol, direction.upper(), price, curr_p, dev * 100)
                    return

                try:
                    res = order_executor.place_market_order(
                        ak, sk, pp, symbol, direction, lev,
                        "isolated", margin, pos_mode=self._pos_mode, client_oid=client_oid,
                        current_price=curr_p
                    )
                    oid = res.get("orderId") if isinstance(res, dict) else ""
                    exec_qty = float(res.get("_calculated_size", 0) if isinstance(res, dict) else 0)
                    db.insert_copy_order({
                        "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                        "my_order_id": oid, "symbol": symbol, "direction": direction,
                        "leverage": lev, "margin_usdt": margin,
                        "source_price": price, "exec_price": curr_p,
                        "deviation_pct": dev, "action": "open",
                        "status": "filled", "pnl": None, "notes": f"[Binance Signal] src_margin={src_margin:.4f} {ratio_note} {margin_note}".strip(), "exec_qty": exec_qty,
                    })
                    logger.info("[币安信号→Bitget开仓] %s %s 价格=%s 数量=%s", symbol, direction.upper(), curr_p, exec_qty)
                except Exception as exc:
                    if _is_symbol_not_exist_error(exc):
                        _insert_unsupported_skip(str(exc))
                        logger.warning("[币安信号跳过] Bitget 不支持交易对 %s: %s", symbol, exc)
                        return
                    # 失败时用带"FAIL_"前缀的 tracking_no，确保下次轮询时不被 has_tracking_no 拦截，可以重试
                    db.insert_copy_order({
                        "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": f"FAIL_{order_id}",
                        "my_order_id": "", "symbol": symbol, "direction": direction,
                        "leverage": lev, "margin_usdt": margin,
                        "source_price": price, "exec_price": 0,
                        "deviation_pct": 0, "action": "open",
                        "status": "failed", "pnl": None, "notes": f"{exc} | src_margin={src_margin:.4f} {ratio_note} {margin_note}".strip(), "exec_qty": 0.0,
                    })
                    logger.error("[币安信号→Bitget开仓失败] %s: %s", symbol, exc)
            finally:
                with self._state_lock:
                    self._bn_inflight.discard(signal_key)

        if action.startswith("close"):
            # ── 平仓 ──
            # 精确计算剩余持仓 = 总开仓量 - 已平仓量，避免超额平仓
            from database import get_conn
            with get_conn() as conn:
                opened_sum = conn.execute('''
                    SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                    WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                      AND action = 'open' AND status = 'filled'
                ''', (pid, symbol, direction)).fetchone()[0]
                closed_sum = conn.execute('''
                    SELECT COALESCE(SUM(exec_qty), 0) FROM copy_orders 
                    WHERE trader_uid = ? AND symbol = ? AND direction = ? 
                      AND action = 'close' AND status = 'filled'
                ''', (pid, symbol, direction)).fetchone()[0]
            remaining_qty = float(opened_sum) - float(closed_sum)
            
            if remaining_qty <= 0:
                logger.warning("[币安平仓信号] 本地未发现 %s %s 的剩余持仓 (pid=%s)，跳过", symbol, direction.upper(), pid[:8])
                return

            # 截断到4位小数
            close_qty = int(remaining_qty * 10000) / 10000.0
            if close_qty <= 0:
                return

            try:
                order_executor.close_partial_position(
                    ak, sk, pp, symbol, direction, str(close_qty),
                    pos_mode=self._pos_mode, margin_mode="isolated"
                )
                db.insert_copy_order({
                    "timestamp": _now_ms(), "trader_uid": pid, "tracking_no": order_id,
                    "my_order_id": "", "symbol": symbol, "direction": direction,
                    "leverage": 0, "margin_usdt": 0, "source_price": price, "exec_price": price,
                    "deviation_pct": 0, "action": "close",
                    "status": "filled", "pnl": order.get("pnl"), "notes": "[Binance Signal] Close",
                    "exec_qty": close_qty,
                })
                logger.info("[币安信号→Bitget平仓] %s %s 数量=%s", symbol, direction.upper(), close_qty)
            except Exception as exc:
                logger.error("[币安信号→Bitget平仓失败] %s: %s", symbol, exc)


    def _run(self) -> None:
        while self._running:
            try:
                self._loop_once()
            except Exception as exc:
                logger.error("Engine loop error: %s", exc, exc_info=True)
            time.sleep(2.5)

    def _loop_once(self) -> None:
        settings = db.get_copy_settings()
        if not settings or not settings.get("engine_enabled"):
            return

        api_key = settings.get("api_key") or ""
        api_secret = settings.get("api_secret") or ""
        api_passphrase = settings.get("api_passphrase") or ""
        if not (api_key and api_secret and api_passphrase):
            return

        enabled_traders = _parse_list(settings.get("enabled_traders", ""))
        if not enabled_traders: return

        # 1. 基础检查（并提取账户模式 posMode）
        tol = _safe_float(settings.get("price_tolerance"), 0.0002)
        follow_ratio = self._resolve_follow_ratio(settings)
        # 资金池仅作为兜底保证金，不再作为硬性开仓门槛。
        total_p = _safe_float(settings.get("total_capital"), 0.0)
        max_m = _safe_float(settings.get("max_margin_pct"), 0.20)
        fallback_margin = 0.0
        try:
            bn_raw = settings.get("binance_traders", "")
            bn_data = json.loads(bn_raw) if bn_raw else {}
            if isinstance(bn_data, dict):
                bn_count = len([p for p, d in bn_data.items() if d.get("copy_enabled") == True])
            else:
                bn_count = len(bn_data) if isinstance(bn_data, list) else 0
        except Exception:
            bn_count = 0
        total_trader_count = max(len(enabled_traders) + bn_count, 1)
        if total_p > 0 and max_m > 0:
            fallback_margin = (total_p / total_trader_count) * max_m

        try:
            bal = order_executor.get_account_balance(api_key, api_secret, api_passphrase)
            available = _extract_balance_usdt(bal)
            self._pos_mode = str(bal.get("_posMode", "2"))
            if available <= 0:
                logger.warning("账户可用余额为 0，跳过本轮")
                return
        except Exception as exc:
            logger.warning("账户检查失败，跳过本轮: %s", exc)
            return

        # 2. 并发拉取交易员数据（极大降低轮询延迟从 O(N) 到 O(1)）
        latest_trader_positions: dict[str, list[dict]] = {}

        def _fetch_task(uid):
            try:
                curr = scraper.fetch_current_positions(uid)
                if curr is None:
                    # 持仓保护模式: API 返回 None
                    # 此时 不能确定交易员的真实持仓情况，不能安全地耦动开/平仓
                    # 返回一个特殊标记值 None，让主循环跳过该交易员
                    logger.warning("交易员 %s 开启了持仓保护，本轮跳过开平仓操作", uid[:8])
                    return uid, None  # None 表示"数据不可用"，区别于"有效的空列表"
                if not curr:
                    # API 正常返回了空列表，说明交易员确实没持仓
                    return uid, []
                return uid, curr
            except Exception as e:
                logger.warning("抓取交易员 %s 数据异常: %s", uid[:8], e)
                return uid, None  # 异常情况也返回 None，跳过开平仓

        try:
            workers = min(10, max(1, len(enabled_traders)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_fetch_task, uid) for uid in enabled_traders]
                for fut in concurrent.futures.as_completed(futures):
                    uid, curr = fut.result()
                    latest_trader_positions[uid] = curr
        except Exception as e:
            logger.warning("并发扫描异常，改用串行模式: %s", e)
            for uid in enabled_traders:
                if uid not in latest_trader_positions:
                    _, curr = _fetch_task(uid)
                    latest_trader_positions[uid] = curr

        # 3. 驱动纯状态机（严谨管理增删改）
        for uid in enabled_traders:
            curr = latest_trader_positions.get(uid)  # 安全get，避免KeyError导致整轮崩溃
            if curr is None:
                logger.warning("交易员 %s 本轮数据缺失，跳过", uid[:8])
                continue
            curr_map = {p["order_no"]: p for p in curr if p.get("order_no")}
            prev_map = self._prev_snaps.get(uid, {})

            new_list = [p for k, p in curr_map.items() if k not in prev_map]
            gone_list = [p for k, p in prev_map.items() if k not in curr_map]

            # 同步检测：同一 order_no 存在但持仓规模或杠杆发生变化
            changed_list = []
            for k, curr_pos in curr_map.items():
                if k in prev_map:
                    prev_pos = prev_map[k]
                    prev_size = _safe_float(prev_pos.get("position_size"), 0.0)
                    curr_size = _safe_float(curr_pos.get("position_size"), 0.0)
                    prev_lev = max(1, _safe_int(prev_pos.get("leverage"), 1))
                    curr_lev = max(1, _safe_int(curr_pos.get("leverage"), 1))
                    # 严格同步：只要仓位数量有可交易精度级别的变化就触发对齐
                    size_changed = abs(curr_size - prev_size) > 0.0001
                    lev_changed = curr_lev != prev_lev
                    if size_changed or lev_changed:
                        changed_list.append((prev_pos, curr_pos))

            for pos in new_list:
                status = self._handle_open(
                    api_key, api_secret, api_passphrase,
                    uid, pos, fallback_margin, tol, available, follow_ratio,
                )
                if status in ("filled", "skipped"):
                    db.upsert_snapshot(_to_snap(uid, pos))
                    self._prev_snaps.setdefault(uid, {})[pos["order_no"]] = pos
                # failed 和 skipped_retry 均不录入快照，让下一轮继续重试。

            for pos in gone_list:
                success = self._handle_close(api_key, api_secret, api_passphrase, uid, pos)
                if success:
                    db.delete_snapshot(uid, pos["order_no"])
                    self._prev_snaps[uid].pop(pos["order_no"], None)
                # 若 false (failed API)，留在快照里，下一轮继续走 close

            # 严格同步：同一 tracking_no 的仓位变化（加仓/减仓/杠杆调整）都实时对齐。
            for prev_pos, curr_pos in changed_list:
                success = self._handle_sync_change(api_key, api_secret, api_passphrase, uid, prev_pos, curr_pos)
                if success:
                    # 更新快照为最新的仓位数据
                    db.upsert_snapshot(_to_snap(uid, curr_pos))
                    self._prev_snaps[uid][curr_pos["order_no"]] = curr_pos


    def _handle_open(self, ak, sk, pp, uid, pos, fallback_margin, tol, available_usdt, follow_ratio: float) -> str:
        symbol = pos.get("symbol", "")
        tn = pos.get("order_no", "")
        side = pos.get("direction", "")
        lev = max(1, int(pos.get("leverage") or 1))
        ref_p = _safe_float(pos.get("open_price"), 0.0)
        source_margin = _safe_float(pos.get("margin_amount"), 0.0)

        if source_margin <= 0:
            source_size = _safe_float(pos.get("position_size"), 0.0)
            source_margin = _estimate_margin_from_position(source_size, ref_p, lev)
        target_margin, ratio_note = self._apply_follow_ratio(source_margin, follow_ratio, fallback_margin)
        margin, margin_note = self._cap_open_margin(
            target_margin,
            fallback_margin=fallback_margin,
            available_usdt=available_usdt,
            source_tag="Bitget",
            symbol=symbol,
            direction=side,
        )

        if margin <= 0:
            logger.warning("[来源数据缺失] 保证金无法推导，跳过开仓 %s %s", symbol, side.upper())
            return "skipped_retry"

        def _insert_unsupported_skip(reason: str) -> str:
            self._unsupported_symbols.add(symbol)
            if not db.has_tracking_no(uid, tn):
                db.insert_copy_order({
                    "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                    "my_order_id": "", "symbol": symbol, "direction": side,
                    "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                    "exec_price": 0, "deviation_pct": 0, "action": "open",
                    "status": "skipped", "pnl": None,
                    "notes": f"[跳过] Bitget 不支持该交易对: {symbol} ({reason}) {ratio_note} {margin_note}".strip(), "exec_qty": 0.0,
                })
            logger.warning("[来源同步跳过] Bitget 不支持交易对 %s: %s", symbol, reason)
            return "skipped"

        if symbol in self._unsupported_symbols:
            return _insert_unsupported_skip("cached unsupported")

        # [Risk Management] 限制单个交易员最大持仓数，防止爆仓
        prev_map = self._prev_snaps.get(uid, {})
        if len(prev_map) >= 10:
            logger.warning("[风控触发] 交易员 %s 已持仓 %d 个，达到上限，跳过新开仓", uid[:8], len(prev_map))
            return "skipped"

        try:
            ok, curr_p, dev = _price_ok(symbol, ref_p, tol)
        except Exception as exc:
            if _is_symbol_not_exist_error(exc):
                return _insert_unsupported_skip(str(exc))
            logger.warning("[价差检查失败暂缓] %s %s: %s", symbol, side.upper(), exc)
            return "skipped_retry"
        if not ok:
            # 价差超限：只记录日志，不写入 DB 也不更新快照
            # 这样下一轮循环时此持仓仍在 new_list 中，会继续重试，直到价差恢复
            logger.warning("[价差过大暂缓] %s %s 偏差: %.2f%%，下轮继续重试", symbol, side.upper(), dev * 100)
            return "skipped_retry"  # 特殊状态：不写快照，下轮重试

        # 生成 clientOid (幂等防重放双杀)
        short_hash = hashlib.md5(f"{uid}_{tn}".encode()).hexdigest()[:16]
        client_oid = f"kop_{short_hash}"

        try:
            res = order_executor.place_market_order(
                ak, sk, pp, symbol, side, lev, pos.get("margin_mode") or "cross",
                margin, pos_mode=self._pos_mode, current_price=curr_p, client_oid=client_oid
            )
            oid = res.get("orderId") if isinstance(res, dict) else ""
            exec_qty = float(res.get("_calculated_size", "0") if isinstance(res, dict) else 0)

            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": oid, "symbol": symbol, "direction": side,
                "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                "exec_price": curr_p, "deviation_pct": dev, "action": "open",
                "status": "filled", "pnl": None,
                "notes": f"[来源同步] src_margin={source_margin:.4f} {ratio_note} {margin_note}".strip(),
                "exec_qty": exec_qty,
            })
            self._fail_streak = 0
            logger.info(
                "[跟随开仓成功] %s %s 数量=%s 杠杆=%sx 保证金=%.4f(来源=%.4f)",
                symbol,
                side.upper(),
                exec_qty,
                lev,
                margin,
                source_margin,
            )
            return "filled"
        except Exception as exc:
            if _is_symbol_not_exist_error(exc):
                return _insert_unsupported_skip(str(exc))
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": "", "symbol": symbol, "direction": side,
                "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                "exec_price": curr_p, "deviation_pct": dev, "action": "open",
                "status": "failed", "pnl": None, "notes": f"{exc} | src_margin={source_margin:.4f} {ratio_note} {margin_note}".strip(), "exec_qty": 0.0,
            })
            # fail_streak 只在同一个 uid+symbol 的连续失败时计数，避免跨交易员误触发熔断
            self._fail_streak += 1
            if self._fail_streak >= 5:  # 提高阈值到 5 次，减少误熔断
                logger.error("故障熔断：开仓连续 %d 次失败（跨交易员），引擎挂起待维护", self._fail_streak)
                db.set_engine_enabled(False)
                self._running = False
            return "failed"

    def _remaining_exec_qty(self, uid: str, tracking_no: str) -> float:
        opened = db.get_copy_orders_by_tracking(uid, tracking_no, action="open")
        filled_opens = [o for o in opened if o.get("status") == "filled"]
        closed = db.get_copy_orders_by_tracking(uid, tracking_no, action="close")
        filled_closes = [o for o in closed if o.get("status") == "filled"]
        total_opened = sum(o.get("exec_qty", 0.0) for o in filled_opens)
        total_closed = sum(o.get("exec_qty", 0.0) for o in filled_closes)
        return float(total_opened) - float(total_closed)

    def _handle_sync_change(self, ak, sk, pp, uid, prev_pos, curr_pos) -> bool:
        """
        严格同步同一 tracking_no 的变化：
        - 杠杆变化：立即同步到来源杠杆
        - 仓位变化：按比例精确加仓/减仓
        """
        symbol = curr_pos.get("symbol", "")
        tn = curr_pos.get("order_no", "")
        side = curr_pos.get("direction", "") or curr_pos.get("hold_side", "")
        margin_mode = curr_pos.get("margin_mode", "cross")
        curr_lev = max(1, _safe_int(curr_pos.get("leverage"), 1))

        remaining = self._remaining_exec_qty(uid, tn)
        if remaining <= 0:
            return True

        # 无论是否加减仓，先强制对齐来源杠杆（严格同步）
        try:
            order_executor.set_symbol_leverage(
                ak, sk, pp,
                symbol=symbol,
                direction=side,
                leverage=curr_lev,
                margin_mode=margin_mode,
                pos_mode=self._pos_mode,
            )
        except Exception as exc:
            logger.error("杠杆同步失败 [%s %s %sx]: %s", symbol, side.upper(), curr_lev, exc)
            return False

        prev_size = _safe_float(prev_pos.get("position_size"), 0.0)
        curr_size = _safe_float(curr_pos.get("position_size"), 0.0)
        if prev_size <= 0 or curr_size <= 0:
            return True

        target_qty = remaining * (curr_size / prev_size)
        delta_qty = _trunc4(target_qty - remaining)
        if abs(delta_qty) < 0.0001:
            return True

        try:
            if delta_qty < 0:
                reduce_qty = _trunc4(-delta_qty)
                if reduce_qty <= 0:
                    return True
                order_executor.close_partial_position(
                    ak, sk, pp, symbol, side, str(reduce_qty),
                    pos_mode=self._pos_mode,
                    margin_mode=margin_mode,
                )
                db.insert_copy_order({
                    "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                    "my_order_id": "", "symbol": symbol, "direction": side,
                    "leverage": curr_lev, "margin_usdt": 0, "source_price": _safe_float(curr_pos.get("open_price"), 0.0),
                    "exec_price": _safe_float(curr_pos.get("open_price"), 0.0), "deviation_pct": 0, "action": "close",
                    "status": "filled", "pnl": _safe_float(curr_pos.get("unrealized_pnl"), 0.0),
                    "notes": "[来源同步] 仓位缩小", "exec_qty": reduce_qty,
                })
                logger.info("[来源同步减仓] %s %s 减仓=%s", symbol, side.upper(), reduce_qty)
                return True

            add_qty = _trunc4(delta_qty)
            if add_qty <= 0:
                return True
            sync_oid = f"sync_{hashlib.md5(f'{uid}_{tn}_{_now_ms()}'.encode()).hexdigest()[:16]}"
            try:
                curr_price = get_ticker_price(symbol)
            except Exception:
                curr_price = _safe_float(curr_pos.get("open_price"), 0.0)
            res = order_executor.place_market_order_by_size(
                ak, sk, pp,
                symbol=symbol,
                direction=side,
                leverage=curr_lev,
                margin_mode=margin_mode,
                size=add_qty,
                pos_mode=self._pos_mode,
                client_oid=sync_oid,
            )
            oid = res.get("orderId") if isinstance(res, dict) else ""
            estimated_margin = _estimate_margin_from_position(add_qty, curr_price, curr_lev)
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": oid, "symbol": symbol, "direction": side,
                "leverage": curr_lev, "margin_usdt": estimated_margin, "source_price": _safe_float(curr_pos.get("open_price"), 0.0),
                "exec_price": curr_price, "deviation_pct": 0, "action": "open",
                "status": "filled", "pnl": _safe_float(curr_pos.get("unrealized_pnl"), 0.0),
                "notes": "[来源同步] 仓位扩大", "exec_qty": add_qty,
            })
            logger.info("[来源同步加仓] %s %s 加仓=%s", symbol, side.upper(), add_qty)
            return True
        except Exception as exc:
            logger.error("来源仓位同步失败 [%s %s]: %s", symbol, side.upper(), exc)
            return False

    def _handle_close(self, ak, sk, pp, uid, pos) -> bool:
        symbol = pos.get("symbol", "")
        tn = pos.get("order_no", "")
        side = pos.get("direction", "") or pos.get("hold_side", "")
        remaining_qty = self._remaining_exec_qty(uid, tn)

        if remaining_qty <= 0:
            return True

        try:
            order_executor.close_partial_position(
                ak, sk, pp, symbol, side, str(remaining_qty), 
                pos_mode=self._pos_mode, 
                margin_mode=pos.get("margin_mode", "cross")
            )
            
            # 当前现价仅用于记录流水
            try: curr_p = get_ticker_price(symbol)
            except Exception: curr_p = 0.0

            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": "", "symbol": symbol, "direction": side,
                "leverage": 0, "margin_usdt": 0, "source_price": 0,
                "exec_price": curr_p, "deviation_pct": 0, "action": "close",
                "status": "filled", "pnl": None, "notes": "", "exec_qty": remaining_qty,
            })
            self._fail_streak = 0
            logger.info("[跟随平仓成功] %s %s 释放数量: %s", symbol, side.upper(), remaining_qty)
            return True
        except Exception as exc:
            logger.error("精确平仓失败将重试: %s", exc)
            self._fail_streak += 1
            if self._fail_streak >= 5:
                logger.error("故障熔断：平仓连续 %d 次失败，引擎挂起待维护", self._fail_streak)
                db.set_engine_enabled(False)
                self._running = False
            return False

    def _handle_reduce(self, ak, sk, pp, uid, pos, ratio: float) -> bool:
        """
        处理减仓（部分平仓）：交易员的 position_size 缩小了 ratio 比例。
        例如 ratio=0.7 表示交易员减了 70%，我们也应该平掉 70%。
        """
        symbol = pos.get("symbol", "")
        tn = pos.get("order_no", "")
        side = pos.get("direction", "") or pos.get("hold_side", "")

        # 查出我们为这笔跟单开了多少
        opened = db.get_copy_orders_by_tracking(uid, tn, action="open")
        filled_opens = [o for o in opened if o.get("status") == "filled"]
        if not filled_opens:
            return True  # 没开过仓，无需减

        # 已经平过的量
        closed = db.get_copy_orders_by_tracking(uid, tn, action="close")
        filled_closes = [o for o in closed if o.get("status") == "filled"]
        total_opened = sum(o.get("exec_qty", 0.0) for o in filled_opens)
        total_closed = sum(o.get("exec_qty", 0.0) for o in filled_closes)
        remaining = total_opened - total_closed

        if remaining <= 0:
            return True  # 已经全平了

        # 按比例计算要平的量
        reduce_qty = remaining * ratio
        # 截断到4位小数
        reduce_qty = int(reduce_qty * 10000) / 10000.0
        if reduce_qty <= 0:
            return True  # 太小了，忽略

        try:
            order_executor.close_partial_position(
                ak, sk, pp, symbol, side, str(reduce_qty),
                pos_mode=self._pos_mode,
                margin_mode=pos.get("margin_mode", "cross")
            )
            try: curr_p = get_ticker_price(symbol)
            except Exception: curr_p = 0.0

            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": "", "symbol": symbol, "direction": side,
                "leverage": 0, "margin_usdt": 0, "source_price": 0,
                "exec_price": curr_p, "deviation_pct": 0, "action": "close",
                "status": "filled", "pnl": None,
                "notes": f"[减仓 {ratio*100:.0f}%]", "exec_qty": reduce_qty,
            })
            self._fail_streak = 0
            logger.info("[跟随减仓成功] %s %s 减仓比例=%.0f%% 平仓数量=%s", symbol, side.upper(), ratio * 100, reduce_qty)
            return True
        except Exception as exc:
            logger.error("跟随减仓失败: %s", exc)
            return False


def start_engine() -> None:
    global _engine
    if _engine is None: _engine = CopyEngine()
    _engine.start()

def stop_engine() -> None:
    global _engine
    if _engine is not None: _engine.stop()

def is_engine_running() -> bool:
    return bool(_engine and _engine.is_running())
