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

logger = logging.getLogger(__name__)

_engine: "CopyEngine | None" = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _to_snap(uid: str, pos: dict) -> dict:
    """转换 scraper 返回的 pos 字典为 snapshots 数据库格式"""
    return {
        "trader_uid": uid,
        "tracking_no": pos.get("order_no", ""),
        "symbol": pos.get("symbol", ""),
        "direction": pos.get("direction", ""),
        "leverage": int(pos.get("leverage") or 1),
        "margin_mode": pos.get("margin_mode", "cross"),
        "open_price": _safe_float(pos.get("open_price")),
        "open_time": int(pos.get("open_time") or 0),
        "position_size": _safe_float(pos.get("position_size")),
        "margin_amount": _safe_float(pos.get("margin_amount")),
        "unrealized_pnl": _safe_float(pos.get("unrealized_pnl")),
        "return_rate": _safe_float(pos.get("return_rate"))
    }


class CopyEngine:
    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._prev_snaps: dict[str, dict[str, dict]] = {}
        self._fail_streak = 0
        self._pos_mode = "2"  # 1=单向, 2=双向

    def start(self) -> None:
        if self._running: return
        self._load_snaps_from_db()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("跟单引擎启动 (高并发精准防重模式)")

    def _load_snaps_from_db(self) -> None:
        traders = db.get_all_traders()
        for t in traders:
            uid = t["trader_uid"]
            snaps = db.get_snapshots(uid)
            if snaps:
                self._prev_snaps[uid] = {k: {**v, "order_no": k} for k, v in snaps.items()}
                logger.info("加载内存快照 [%s]: %d 个", uid[:8], len(snaps))

    def stop(self) -> None:
        self._running = False
        logger.info("跟单引擎已停止")

    def is_running(self) -> bool:
        return self._running

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
        total_p = _safe_float(settings.get("total_capital"), 0.0)
        max_m = _safe_float(settings.get("max_margin_pct"), 0.20)
        tol = _safe_float(settings.get("price_tolerance"), 0.0002)
        margin_usdt = (total_p / len(enabled_traders)) * max_m

        try:
            bal = order_executor.get_account_balance(api_key, api_secret, api_passphrase)
            available = _extract_balance_usdt(bal)
            self._pos_mode = str(bal.get("_posMode", "2"))
            if total_p > 0 and available < total_p * 0.3:
                logger.warning("余额不足 (可用 %.2f < 资金池 30%%)，跳过下单", available)
                return
        except Exception as exc:
            logger.warning("账户检查失败，跳过本轮: %s", exc)
            return

        # 2. 并发拉取交易员数据（极大降低轮询延迟从 O(N) 到 O(1)）
        latest_trader_positions: dict[str, list[dict]] = {}
        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(10, max(1, len(enabled_traders))))
            fut_to_uid = {executor.submit(scraper.fetch_current_positions, uid): uid for uid in enabled_traders}
            for fut in concurrent.futures.as_completed(fut_to_uid):
                uid = fut_to_uid[fut]
                try:
                    curr = fut.result()
                except Exception as exc:
                    logger.warning("获取交易员 %s 指标失败: %s", uid[:8], exc)
                    latest_trader_positions[uid] = list(self._prev_snaps.get(uid, {}).values())
            executor.shutdown(wait=False)
        except RuntimeError as e:
            # 兼容处理：线程池已在关闭状态，改用串行模式
            logger.warning("线程池异常，改用串行模式: %s", e)
            for uid in enabled_traders:
                try:
                    curr = scraper.fetch_current_positions(uid)
                    # 如果 currentList 返回空（可能是隐私设置导致），尝试从历史订单推断持仓
                    if not curr:
                        logger.info("交易员 %s 的 currentList 返回空，尝试从历史订单推断持仓", uid[:8])
                        curr = scraper.infer_current_positions_from_history(uid)
                        if curr:
                            logger.info("从历史订单推断到 %d 个持仓", len(curr))
                    latest_trader_positions[uid] = curr
                except Exception as exc:
                    logger.warning("获取交易员 %s 指标失败: %s", uid[:8], exc)
                    latest_trader_positions[uid] = list(self._prev_snaps.get(uid, {}).values())

        # 3. 驱动纯状态机（严谨管理增删）
        for uid in enabled_traders:
            curr = latest_trader_positions[uid]
            curr_map = {p["order_no"]: p for p in curr if p.get("order_no")}
            prev_map = self._prev_snaps.get(uid, {})

            new_list = [p for k, p in curr_map.items() if k not in prev_map]
            gone_list = [p for k, p in prev_map.items() if k not in curr_map]

            for pos in new_list:
                status = self._handle_open(api_key, api_secret, api_passphrase, uid, pos, margin_usdt, tol)
                if status in ("filled", "skipped"):
                    # 写入内存与 DB，宣告这个订单"我已知晓/处置完毕"
                    self._prev_snaps.setdefault(uid, {})[pos["order_no"]] = pos
                    db.upsert_snapshot(_to_snap(uid, pos))
                # 若 failed，不录入快照，让下一轮继续处于 new_list 中发起重试。

            for pos in gone_list:
                success = self._handle_close(api_key, api_secret, api_passphrase, uid, pos)
                if success:
                    # 写入内存与 DB 彻底剥离
                    self._prev_snaps[uid].pop(pos["order_no"], None)
                    db.delete_snapshot(uid, pos["order_no"])
                # 若 false (failed API)，留在快照里，下一轮继续走 close（极强容错）


    def _handle_open(self, ak, sk, pp, uid, pos, margin, tol) -> str:
        symbol = pos.get("symbol", "")
        tn = pos.get("order_no", "")
        side = pos.get("direction", "")
        lev = int(pos.get("leverage") or 1)
        ref_p = _safe_float(pos.get("open_price"), 0.0)

        ok, curr_p, dev = _price_ok(symbol, ref_p, tol)
        if not ok:
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": "", "symbol": symbol, "direction": side,
                "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                "exec_price": curr_p, "deviation_pct": dev, "action": "open",
                "status": "skipped", "pnl": None, "notes": "价差超限", "exec_qty": 0.0,
            })
            logger.warning("[价差过大跳过] %s %s 偏差: %.2f%%", symbol, side.upper(), dev * 100)
            return "skipped"

        # 生成 clientOid (幂等防重放双杀)
        short_hash = hashlib.md5(f"{uid}_{tn}".encode()).hexdigest()[:16]
        client_oid = f"kop_{short_hash}"

        try:
            res = order_executor.place_market_order(
                ak, sk, pp, symbol, side, lev, pos.get("margin_mode") or "cross",
                margin, current_price=curr_p, client_oid=client_oid
            )
            oid = res.get("orderId") if isinstance(res, dict) else ""
            exec_qty = float(res.get("_calculated_size", "0") if isinstance(res, dict) else 0)

            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": oid, "symbol": symbol, "direction": side,
                "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                "exec_price": curr_p, "deviation_pct": dev, "action": "open",
                "status": "filled", "pnl": None, "notes": "", "exec_qty": exec_qty,
            })
            self._fail_streak = 0
            logger.info("[跟随开仓成功] %s %s 数量: %s", symbol, side.upper(), exec_qty)
            return "filled"
        except Exception as exc:
            db.insert_copy_order({
                "timestamp": _now_ms(), "trader_uid": uid, "tracking_no": tn,
                "my_order_id": "", "symbol": symbol, "direction": side,
                "leverage": lev, "margin_usdt": margin, "source_price": ref_p,
                "exec_price": curr_p, "deviation_pct": dev, "action": "open",
                "status": "failed", "pnl": None, "notes": str(exc), "exec_qty": 0.0,
            })
            self._fail_streak += 1
            if self._fail_streak >= 3:
                logger.error("故障熔断：开仓连续 3 次失败，引擎挂起待维护")
                db.set_engine_enabled(False)
                self._running = False
            return "failed"

    def _handle_close(self, ak, sk, pp, uid, pos) -> bool:
        symbol = pos.get("symbol", "")
        tn = pos.get("order_no", "")
        side = pos.get("direction", "") or pos.get("hold_side", "")
        
        # 溯源：我到底为这笔跟单开了多少张？
        opened = db.get_copy_orders_by_tracking(uid, tn, action="open")
        filled_opens = [o for o in opened if o.get("status") == "filled"]
        
        if not filled_opens:
            # 说明未曾成功建仓过（可能是 skipped，也可能是全 failed）
            # 直接当其圆满完结即可
            return True

        total_qty = sum(o.get("exec_qty", 0.0) for o in filled_opens)
        if total_qty <= 0:
            return True

        try:
            # 使用精妙的局部闭仓（不再干扰其他交易员同币种同方向仓位）
            order_executor.close_partial_position(
                ak, sk, pp, symbol, side, str(total_qty), 
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
                "status": "filled", "pnl": None, "notes": "", "exec_qty": total_qty,
            })
            self._fail_streak = 0
            logger.info("[跟随平仓成功] %s %s 释放数量: %s", symbol, side.upper(), total_qty)
            return True
        except Exception as exc:
            logger.error("精确平仓失败将重试: %s", exc)
            self._fail_streak += 1
            if self._fail_streak >= 3:
                logger.error("故障熔断：平仓连续 3 次失败，引擎挂起待维护")
                db.set_engine_enabled(False)
                self._running = False
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
