"""
定时采集器：周期性采集所有被追踪交易员的公开数据。
- 基础信息/指标：通过 scraper 从 Bitget 公开网页 API 拉取（无需跟单）
- 历史订单：通过 scraper 增量拉取（只拉比本地最新记录更新的订单）
- 当前持仓：通过 scraper 实时拉取，存入 snapshots 表
"""
from __future__ import annotations
import logging
import signal
import time

import api_client
import analyzer
import config
import database as db
import scraper
import trade_detector

logger = logging.getLogger(__name__)

_running = True


def _sigint_handler(sig, frame):
    global _running
    logger.info("收到退出信号，正在停止采集器…")
    _running = False


signal.signal(signal.SIGINT, _sigint_handler)
signal.signal(signal.SIGTERM, _sigint_handler)


# ── 初始化单个交易员 ──────────────────────────────────────────────────────────

def init_trader(trader_uid: str, nickname: str):
    """
    首次加入时（或重新初始化）：
    1. 公开网页 API 拉取基础数据 → traders 表
    2. 增量拉取历史订单 → trades 表
    3. 拉取当前持仓 → snapshots 表
    """
    logger.info("初始化交易员 %s (%s)…", nickname, trader_uid[:8])

    # ① 基础数据
    detail = _safe_call(scraper.fetch_trader_detail, trader_uid)
    if detail:
        db.upsert_trader(
            trader_uid=trader_uid,
            nickname=detail.get("name") or nickname,
            roi=detail.get("roi"),
            win_rate=detail.get("win_rate"),
            max_drawdown=detail.get("max_drawdown"),
            total_profit=detail.get("total_profit"),
            aum=detail.get("aum"),
            follower_count=detail.get("follower_count"),
            avatar=detail.get("avatar"),
            profit_7d=detail.get("profit_7d"),
            profit_30d=detail.get("profit_30d"),
        )
    else:
        db.upsert_trader(trader_uid=trader_uid, nickname=nickname)

    # ② 增量拉取历史订单（只拉还没有的）
    _sync_history(trader_uid)

    # ③ 当前持仓
    _sync_positions(trader_uid)

    logger.info("初始化完成：%s", detail.get("name") if detail else nickname)


# ── 单轮采集 ──────────────────────────────────────────────────────────────────

def _poll_trader(trader_uid: str):
    # ① 刷新基础数据
    detail = _safe_call(scraper.fetch_trader_detail, trader_uid)
    if detail:
        trader = db.get_trader(trader_uid)
        nickname = (trader["nickname"] if trader else None) or detail.get("name") or trader_uid
        db.upsert_trader(
            trader_uid=trader_uid,
            nickname=nickname,
            roi=detail.get("roi"),
            win_rate=detail.get("win_rate"),
            max_drawdown=detail.get("max_drawdown"),
            total_profit=detail.get("total_profit"),
            aum=detail.get("aum"),
            follower_count=detail.get("follower_count"),
            avatar=detail.get("avatar"),
            profit_7d=detail.get("profit_7d"),
            profit_30d=detail.get("profit_30d"),
        )

    # ② 增量拉取新完成的历史订单
    _sync_history(trader_uid)

    # ③ 刷新当前持仓
    _sync_positions(trader_uid)


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run(trader_uids: list[str]):
    """
    启动采集主循环。trader_uids 是已在 DB 中存在的交易员 uid 列表。
    """
    if not trader_uids:
        logger.warning("没有要追踪的交易员，采集器退出。")
        return

    logger.info(
        "采集器启动，追踪 %d 名交易员，轮询间隔 %ds",
        len(trader_uids), config.POLL_INTERVAL,
    )

    _last_analyze = time.monotonic()
    analyze_interval = 3600  # 每小时重算一次指标

    while _running:
        cycle_start = time.monotonic()

        for uid in trader_uids:
            if not _running:
                break
            try:
                _poll_trader(uid)
            except Exception as exc:
                logger.error("采集异常 [%s]: %s", uid[:8], exc, exc_info=True)

        # 每小时触发指标计算
        if time.monotonic() - _last_analyze >= analyze_interval:
            for uid in trader_uids:
                try:
                    analyzer.compute_and_log(uid)
                except Exception as exc:
                    logger.error("指标计算失败 [%s]: %s", uid[:8], exc)
            _last_analyze = time.monotonic()

        # 精确控制轮询间隔
        elapsed = time.monotonic() - cycle_start
        sleep_sec = max(0.0, config.POLL_INTERVAL - elapsed)
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    logger.info("采集器已停止。")


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _sync_history(trader_uid: str):
    """
    增量拉取历史订单：只拉比本地最新记录更新的订单，避免重复写入。
    """
    latest_ts = db.get_latest_trade_time(trader_uid)
    new_trades = []
    try:
        for page in range(1, 20):  # 最多拉 20 页 × 20 条 = 400 单
            result = scraper.fetch_history_orders(trader_uid, page=page, page_size=20)
            rows = result["rows"]
            if not rows:
                break
            # 按平仓时间倒序，一旦碰到已有记录就停止
            got_old = False
            for row in rows:
                if row["close_time"] <= latest_ts:
                    got_old = True
                    break
                new_trades.append(row)
            if got_old or not result["next_page"]:
                break
            time.sleep(0.3)
    except Exception as exc:
        logger.error("拉取历史订单失败 [%s]: %s", trader_uid[:8], exc)

    if new_trades:
        db.insert_trades_bulk(new_trades)
        logger.info("新增 %d 条历史订单 [%s]", len(new_trades), trader_uid[:8])


def _sync_positions(trader_uid: str):
    """
    拉取并更新当前持仓快照（新字段完整存储）。
    """
    try:
        positions = scraper.fetch_current_positions(trader_uid)
        now = int(time.time() * 1000)
        with db.get_conn() as conn:
            db._migrate_snapshots(conn)
            conn.execute("DELETE FROM snapshots WHERE trader_uid = ?", (trader_uid,))
            for pos in positions:
                tracking_no = pos.get("order_no") or f"open_{pos['symbol']}_{pos['direction']}_{pos['open_time']}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO snapshots
                        (trader_uid, timestamp, tracking_no, symbol, hold_side,
                         leverage, margin_mode, open_price, open_time,
                         open_amount, position_size, unrealized_pnl,
                         return_rate, follow_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trader_uid, now, tracking_no,
                        pos["symbol"], pos["direction"],
                        pos["leverage"], pos["margin_mode"],
                        pos["open_price"], pos["open_time"],
                        pos["margin_amount"], pos["position_size"],
                        pos["unrealized_pnl"], pos["return_rate"],
                        pos["follow_count"],
                    ),
                )
            conn.commit()
        if positions:
            logger.info("当前持仓 %d 个 [%s]", len(positions), trader_uid[:8])
    except Exception as exc:
        logger.error("更新持仓快照失败 [%s]: %s", trader_uid[:8], exc)


def _api_configured() -> bool:
    return bool(
        config.BITGET_API_KEY
        and config.BITGET_API_KEY != "your_api_key_here"
        and config.BITGET_SECRET_KEY
        and config.BITGET_PASSPHRASE
    )


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.error("调用失败 [%s]: %s", fn.__name__, exc)
        return None


def _f(d: dict, key: str, default=None) -> float | None:
    v = d.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(d: dict, key: str, default=None) -> int | None:
    v = d.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
