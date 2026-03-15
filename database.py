"""
SQLite 数据库：建表 + CRUD 封装
所有操作通过 get_conn() 获取连接，支持多线程（check_same_thread=False）。
"""
from __future__ import annotations
import logging
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

import config

logger = logging.getLogger(__name__)

# 数据库写入锁（序列化并发写操作）
_db_write_lock = threading.RLock()

# 删除黑名单（防止采集器重新创建已删除的交易员）
_deleted_traders: set[str] = set()
_deleted_lock = threading.Lock()


def _clean_symbol_value(symbol: Any) -> str:
    s = str(symbol or "").upper()
    for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
        s = s.replace(suffix, "")
    return s


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


def _estimate_margin_from_position(size: float, price: float, leverage: int) -> float:
    if size <= 0 or price <= 0 or leverage <= 0:
        return 0.0
    return (abs(size) * price) / max(leverage, 1)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _upsert_trader_metadata_value(conn, trader_uid: str, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO trader_metadata (trader_uid, key, value)
        VALUES (?, ?, ?)
        """,
        (trader_uid, key, "" if value is None else str(value)),
    )


def _collect_trader_analysis(conn, trader_uid: str, lookback_days: int = 45) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    lookback_ms = max(0, int(lookback_days or 0)) * 86400_000
    cutoff_ms = now_ms - lookback_ms if lookback_ms > 0 else 0

    source_rows = conn.execute(
        """
        SELECT action, direction, qty, price, leverage, order_time
        FROM source_trader_events
        WHERE trader_uid = ? AND order_time >= ?
        ORDER BY order_time ASC, id ASC
        """,
        (trader_uid, cutoff_ms),
    ).fetchall()
    open_margins: list[float] = []
    open_leverages: list[float] = []
    source_open_count = 0
    source_close_count = 0
    for row in source_rows:
        action = str(row["action"] or "")
        qty = _safe_float(row["qty"], 0.0)
        price = _safe_float(row["price"], 0.0)
        leverage = max(1, _safe_int(row["leverage"], 1))
        if action in ("open_long", "open_short"):
            source_open_count += 1
            margin = _estimate_margin_from_position(qty, price, leverage)
            if margin > 0:
                open_margins.append(margin)
            open_leverages.append(float(leverage))
        elif action in ("close_long", "close_short"):
            source_close_count += 1

    cycle_rows = conn.execute(
        """
        SELECT hold_duration_sec, realized_pnl, close_reason
        FROM trader_position_cycles
        WHERE trader_uid = ? AND open_time >= ?
        ORDER BY open_time DESC
        LIMIT 500
        """,
        (trader_uid, cutoff_ms),
    ).fetchall()
    closed_cycles = [
        row for row in cycle_rows
        if str(row["close_reason"] or "") in ("normal_close", "reverse_transition")
    ]
    hold_secs = [
        max(0.0, _safe_float(row["hold_duration_sec"], 0.0))
        for row in closed_cycles
        if row["hold_duration_sec"] not in (None, "")
    ]
    reverse_count = sum(1 for row in closed_cycles if str(row["close_reason"] or "") == "reverse_transition")
    win_count = sum(1 for row in closed_cycles if _safe_float(row["realized_pnl"], 0.0) > 0)
    stability_score = (win_count / len(closed_cycles) * 100.0) if closed_cycles else 0.0

    copy_rows = conn.execute(
        """
        SELECT action, status, notes, platform, tracking_no, timestamp
        FROM copy_orders
        WHERE trader_uid = ? AND timestamp >= ?
        ORDER BY timestamp DESC, id DESC
        """,
        (trader_uid, cutoff_ms),
    ).fetchall()
    open_rows = [row for row in copy_rows if str(row["action"] or "") == "open"]
    close_rows = [row for row in copy_rows if str(row["action"] or "") == "close"]

    platform_stats: dict[str, dict[str, int]] = {}
    for row in copy_rows:
        platform = str(row["platform"] or "bitget")
        stats = platform_stats.setdefault(platform, {"filled": 0, "total": 0})
        stats["total"] += 1
        if str(row["status"] or "") == "filled":
            stats["filled"] += 1

    preferred_platform = "bitget"
    execution_score = 0.0
    if platform_stats:
        preferred_platform = max(
            platform_stats.items(),
            key=lambda item: (item[1]["filled"], item[1]["total"], item[0]),
        )[0]
        preferred = platform_stats.get(preferred_platform) or {"filled": 0, "total": 0}
        execution_score = (
            preferred["filled"] / preferred["total"] * 100.0
            if preferred["total"] > 0 else 0.0
        )

    clip_count = 0
    min_adjust_count = 0
    small_order_skip_count = 0
    fallback_market_count = 0
    for row in open_rows:
        notes = str(row["notes"] or "")
        if "[保证金裁剪]" in notes:
            clip_count += 1
        if "[最小下单修正]" in notes:
            min_adjust_count += 1
        if "最小开仓金额不足" in notes:
            small_order_skip_count += 1
        if "[FallbackMarket]" in notes:
            fallback_market_count += 1

    reconcile_close_count = 0
    close_filled_count = 0
    for row in close_rows:
        notes = str(row["notes"] or "")
        tracking_no = str(row["tracking_no"] or "")
        if str(row["status"] or "") == "filled":
            close_filled_count += 1
        if tracking_no.startswith("REC_") or "reconcile_close" in notes:
            reconcile_close_count += 1

    if open_rows and clip_count == 0:
        clip_count = sum(
            1
            for row in open_rows
            if ("src=" in str(row["notes"] or "") and "cap=" in str(row["notes"] or ""))
        )
    if open_rows and min_adjust_count == 0:
        min_adjust_count = sum(
            1
            for row in open_rows
            if ("target=" in str(row["notes"] or "") and "min=" in str(row["notes"] or ""))
        )
    if open_rows and small_order_skip_count == 0:
        small_order_skip_count = sum(
            1
            for row in open_rows
            if ("need=" in str(row["notes"] or "") and "cap=" in str(row["notes"] or ""))
        )

    open_total = len(open_rows)
    close_total = len(close_rows)
    clip_rate = clip_count / open_total if open_total > 0 else 0.0
    min_adjust_rate = min_adjust_count / open_total if open_total > 0 else 0.0
    small_order_skip_rate = small_order_skip_count / open_total if open_total > 0 else 0.0
    fallback_market_rate = fallback_market_count / open_total if open_total > 0 else 0.0
    reverse_rate = reverse_count / len(closed_cycles) if closed_cycles else 0.0
    close_reliability_base = max(source_close_count, len(closed_cycles), 1)
    close_reliability_score = min(100.0, (close_filled_count / close_reliability_base) * 100.0)
    risk_score = 100.0 - min(100.0, abs(stability_score - 50.0))
    total_score = (
        stability_score * 0.35
        + execution_score * 0.25
        + close_reliability_score * 0.20
        + (1.0 - clip_rate) * 10.0
        + (1.0 - reverse_rate) * 10.0
    )
    behavior_score = max(
        0.0,
        min(
            100.0,
            100.0
            - clip_rate * 35.0
            - reverse_rate * 25.0
            - small_order_skip_rate * 15.0
            - fallback_market_rate * 10.0,
        ),
    )

    return {
        "trader_uid": trader_uid,
        "lookback_days": max(0, int(lookback_days or 0)),
        "analysis_updated_at": now_ms,
        "history_sample_size": source_open_count,
        "source_close_count": source_close_count,
        "cycle_sample_size": len(closed_cycles),
        "copy_open_sample_size": open_total,
        "copy_close_sample_size": close_total,
        "median_source_margin": _median(open_margins),
        "avg_source_margin": _mean(open_margins),
        "median_source_leverage": _median(open_leverages),
        "avg_hold_sec": _mean(hold_secs),
        "median_hold_sec": _median(hold_secs),
        "reverse_rate": reverse_rate,
        "clip_rate": clip_rate,
        "min_adjust_rate": min_adjust_rate,
        "small_order_skip_rate": small_order_skip_rate,
        "fallback_market_rate": fallback_market_rate,
        "reconcile_close_rate": (reconcile_close_count / close_total) if close_total > 0 else 0.0,
        "stability_score": stability_score,
        "execution_score": execution_score,
        "close_reliability_score": close_reliability_score,
        "risk_score": risk_score,
        "total_score": total_score,
        "behavior_score": behavior_score,
        "preferred_platform": preferred_platform,
    }

def mark_deleted(uid: str):
    """标记交易员为已删除。"""
    with _deleted_lock:
        _deleted_traders.add(uid)
    logger.info("已将交易员 %s 加入删除黑名单", uid[:8])

def is_deleted(uid: str) -> bool:
    """检查交易员是否在删除黑名单中。"""
    with _deleted_lock:
        return uid in _deleted_traders

def clear_deleted(uid: str):
    """从删除黑名单中移除（用于重新添加时）。"""
    with _deleted_lock:
        _deleted_traders.discard(uid)

# ── 初始化 ────────────────────────────────────────────────────────────────────

def init_db():
    """确保数据目录和所有表存在。"""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate_trader_position_cycles(conn)
        _migrate_trader_research_scores(conn)
        conn.commit()
    logger.info("数据库就绪：%s", config.DB_PATH)


@contextmanager
def get_conn():
    """获取数据库连接（支持超时重试处理 SQLITE_BUSY）。"""
    max_retries = 3
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            conn = sqlite3.connect(
                config.DB_PATH,
                check_same_thread=False,
                timeout=10.0  # 10秒超时让 SQLite 自动重试
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")  # 5秒忙碌超时
            try:
                yield conn
            finally:
                conn.close()
            return
        except sqlite3.OperationalError as e:
            last_error = e
            if "database is locked" in str(e) and attempt < max_retries:
                wait_time = 0.1 * (2 ** attempt)
                logger.debug("数据库被锁定，%f秒后重试 (尝试 %d/%d)", wait_time, attempt, max_retries)
                time.sleep(wait_time)
                continue
            raise
        except Exception:
            raise
    
    if last_error:
        raise last_error


def _table_column_names(conn, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return set()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"] or ""))
        elif len(row) > 1:
            names.add(str(row[1] or ""))
    return names


def _migrate_trader_position_cycles(conn) -> None:
    columns = _table_column_names(conn, "trader_position_cycles")
    if not columns:
        return
    legacy_markers = {"cycle_key", "hold_seconds", "updated_at", "avg_open_price", "max_leverage_seen"}
    if columns & legacy_markers:
        conn.execute("DROP TABLE IF EXISTS trader_position_cycles_legacy")
        conn.execute("ALTER TABLE trader_position_cycles RENAME TO trader_position_cycles_legacy")
        conn.execute(
            """
            CREATE TABLE trader_position_cycles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trader_uid      TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                open_event_id   INTEGER,
                close_event_id  INTEGER,
                open_time       INTEGER NOT NULL,
                close_time      INTEGER,
                open_price      REAL NOT NULL,
                close_price     REAL,
                qty             REAL NOT NULL,
                leverage        INTEGER DEFAULT 1,
                hold_duration_sec INTEGER,
                realized_pnl    REAL,
                roi_pct         REAL,
                close_reason    TEXT DEFAULT 'still_open',
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (open_event_id) REFERENCES source_trader_events(id),
                FOREIGN KEY (close_event_id) REFERENCES source_trader_events(id)
            )
            """
        )
        conn.execute("DROP TABLE trader_position_cycles_legacy")
        columns = _table_column_names(conn, "trader_position_cycles")
    required_columns = [
        ("open_event_id", "INTEGER"),
        ("close_event_id", "INTEGER"),
        ("open_price", "REAL DEFAULT 0"),
        ("close_price", "REAL"),
        ("qty", "REAL DEFAULT 0"),
        ("leverage", "INTEGER DEFAULT 1"),
        ("hold_duration_sec", "INTEGER"),
        ("roi_pct", "REAL"),
    ]
    for column, ddl in required_columns:
        if column in columns:
            continue
        conn.execute(f"ALTER TABLE trader_position_cycles ADD COLUMN {column} {ddl}")


def _migrate_trader_research_scores(conn) -> None:
    columns = _table_column_names(conn, "trader_research_scores")
    if not columns:
        return
    legacy_markers = {"as_of_day", "platform_fit_score", "signal_stability_score", "small_cap_compatibility_score"}
    if columns & legacy_markers:
        conn.execute("DROP TABLE IF EXISTS trader_research_scores_legacy")
        conn.execute("ALTER TABLE trader_research_scores RENAME TO trader_research_scores_legacy")
        conn.execute(
            """
            CREATE TABLE trader_research_scores (
                trader_uid      TEXT PRIMARY KEY,
                stability_score REAL DEFAULT 0,
                execution_score REAL DEFAULT 0,
                risk_score      REAL DEFAULT 0,
                total_score     REAL DEFAULT 0,
                rank            INTEGER DEFAULT 0,
                updated_at      INTEGER NOT NULL
            )
            """
        )
        conn.execute("DROP TABLE trader_research_scores_legacy")
        columns = _table_column_names(conn, "trader_research_scores")
    required_columns = [
        ("stability_score", "REAL DEFAULT 0"),
        ("execution_score", "REAL DEFAULT 0"),
        ("risk_score", "REAL DEFAULT 0"),
        ("total_score", "REAL DEFAULT 0"),
        ("rank", "INTEGER DEFAULT 0"),
    ]
    for column, ddl in required_columns:
        if column in columns:
            continue
        conn.execute(f"ALTER TABLE trader_research_scores ADD COLUMN {column} {ddl}")


# ── 建表 DDL ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    trader_uid      TEXT PRIMARY KEY,
    nickname        TEXT,
    first_seen      INTEGER,
    roi             REAL,
    win_rate        REAL,
    max_drawdown    REAL,
    total_profit    REAL,
    aum             REAL,
    follower_count  INTEGER,
    total_trades    INTEGER,
    copy_trade_days INTEGER,
    last_updated    INTEGER,
    avatar          TEXT,
    profit_7d       REAL,
    profit_30d      REAL
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    trader_uid      TEXT,
    symbol          TEXT,
    direction       TEXT,
    leverage        INTEGER,
    margin_mode     TEXT,
    open_price      REAL,
    open_time       INTEGER,
    close_price     REAL,
    close_time      INTEGER,
    hold_duration   INTEGER,
    position_size   REAL,
    pnl_pct         REAL,
    net_profit      REAL,
    gross_profit    REAL,
    open_fee        REAL,
    close_fee       REAL,
    funding_fee     REAL,
    margin_amount   REAL,
    follow_count    INTEGER,
    is_win          INTEGER,
    FOREIGN KEY (trader_uid) REFERENCES traders(trader_uid)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_uid      TEXT,
    timestamp       INTEGER,
    tracking_no     TEXT,
    symbol          TEXT,
    hold_side       TEXT,
    leverage        INTEGER,
    margin_mode     TEXT,
    open_price      REAL,
    open_time       INTEGER,
    open_amount     REAL,
    position_size   REAL,
    unrealized_pnl  REAL,
    return_rate     REAL,
    follow_count    INTEGER,
    tp_price        REAL,
    sl_price        REAL,
    UNIQUE(trader_uid, tracking_no)
);

CREATE TABLE IF NOT EXISTS copy_settings (
    id              INTEGER PRIMARY KEY,
    api_key         TEXT,
    api_secret      TEXT,
    api_passphrase  TEXT,
    total_capital   REAL DEFAULT 0,
    follow_ratio_pct REAL DEFAULT 0.003,
    max_margin_pct  REAL DEFAULT 0.20,
    price_tolerance REAL DEFAULT 0.01,
    sl_pct          REAL DEFAULT 0.15,
    tp_pct          REAL DEFAULT 0.30,
    daily_loss_limit_pct REAL DEFAULT 0.03,
    total_drawdown_limit_pct REAL DEFAULT 0.10,
    take_profit_enabled INTEGER DEFAULT 1,
    stop_loss_pct   REAL DEFAULT 0.06,
    tp1_roi_pct     REAL DEFAULT 0.08,
    tp1_close_pct   REAL DEFAULT 0.30,
    tp2_roi_pct     REAL DEFAULT 0.15,
    tp2_close_pct   REAL DEFAULT 0.30,
    tp3_roi_pct     REAL DEFAULT 0.25,
    tp3_close_pct   REAL DEFAULT 0.40,
    breakeven_buffer_pct REAL DEFAULT 0.005,
    trail_callback_pct REAL DEFAULT 0.06,
    entry_order_mode TEXT DEFAULT 'maker_limit',
    entry_maker_levels INTEGER DEFAULT 1,
    entry_limit_timeout_sec INTEGER DEFAULT 10,
    entry_limit_fallback_to_market INTEGER DEFAULT 1,
    binance_total_capital REAL DEFAULT 0,
    binance_follow_ratio_pct REAL DEFAULT 0.003,
    binance_max_margin_pct REAL DEFAULT 0.20,
    binance_price_tolerance REAL DEFAULT 0.01,
    enabled_traders TEXT DEFAULT '[]',
    binance_traders TEXT DEFAULT '{}',
    engine_enabled  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS copy_profile_settings (
    profile         TEXT PRIMARY KEY,
    settings_json   TEXT NOT NULL DEFAULT '{}',
    updated_at      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account_daily_equity (
    day             TEXT PRIMARY KEY, -- YYYY-MM-DD??????
    start_equity    REAL NOT NULL,
    start_ts        INTEGER NOT NULL,
    last_equity     REAL NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_daily_equity (
    platform        TEXT NOT NULL,
    day             TEXT NOT NULL,
    start_equity    REAL NOT NULL,
    start_ts        INTEGER NOT NULL,
    last_equity     REAL NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (platform, day)
);

CREATE TABLE IF NOT EXISTS copy_position_states (
    platform        TEXT NOT NULL,
    trader_uid      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    stage           INTEGER DEFAULT 0,
    peak_roi        REAL DEFAULT 0,
    locked_roi_pct  REAL DEFAULT 0,
    breakeven_armed INTEGER DEFAULT 0,
    trail_active    INTEGER DEFAULT 0,
    closed_by_system INTEGER DEFAULT 0,
    freeze_reentry  INTEGER DEFAULT 0,
    last_source_order_id TEXT DEFAULT '',
    last_system_action TEXT DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (platform, trader_uid, symbol, direction)
);

CREATE TABLE IF NOT EXISTS copy_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       INTEGER,
    trader_uid      TEXT,
    tracking_no     TEXT,
    my_order_id     TEXT,
    symbol          TEXT,
    direction       TEXT,
    leverage        INTEGER,
    margin_usdt     REAL,
    source_price    REAL,
    exec_price      REAL,
    deviation_pct   REAL,
    action          TEXT,      -- 'open', 'close', 'reconcile'
    status          TEXT,      -- 'filled', 'skipped', 'failed'
    pnl             REAL,
    notes           TEXT,      -- 记录失败原因或信息
    exec_qty        REAL DEFAULT 0,
    platform        TEXT DEFAULT 'bitget'  -- 区分是下单在 bitget 还是 binance
);

CREATE TABLE IF NOT EXISTS source_trader_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_uid      TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    leverage        INTEGER DEFAULT 1,
    order_time      INTEGER NOT NULL,
    source_kind     TEXT DEFAULT 'live',
    raw_payload     TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE(trader_uid, source_order_id)
);

CREATE TABLE IF NOT EXISTS trader_position_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_uid      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    open_event_id   INTEGER,
    close_event_id  INTEGER,
    open_time       INTEGER NOT NULL,
    close_time      INTEGER,
    open_price      REAL NOT NULL,
    close_price     REAL,
    qty             REAL NOT NULL,
    leverage        INTEGER DEFAULT 1,
    hold_duration_sec INTEGER,
    realized_pnl    REAL,
    roi_pct         REAL,
    close_reason    TEXT DEFAULT 'still_open',
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (open_event_id) REFERENCES source_trader_events(id),
    FOREIGN KEY (close_event_id) REFERENCES source_trader_events(id)
);

CREATE TABLE IF NOT EXISTS trader_execution_daily (
    trader_uid      TEXT NOT NULL,
    day             TEXT NOT NULL,
    platform        TEXT DEFAULT 'bitget',
    total_orders    INTEGER DEFAULT 0,
    filled_orders   INTEGER DEFAULT 0,
    skipped_orders  INTEGER DEFAULT 0,
    avg_delay_ms    REAL DEFAULT 0,
    avg_slippage_pct REAL DEFAULT 0,
    total_pnl       REAL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (trader_uid, day, platform)
);

CREATE TABLE IF NOT EXISTS trader_research_scores (
    trader_uid      TEXT PRIMARY KEY,
    stability_score REAL DEFAULT 0,
    execution_score REAL DEFAULT 0,
    risk_score      REAL DEFAULT 0,
    total_score     REAL DEFAULT 0,
    rank            INTEGER DEFAULT 0,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trader_metadata (
    trader_uid      TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (trader_uid, key)
);

CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader_uid);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_snapshots_trader ON snapshots(trader_uid);
CREATE INDEX IF NOT EXISTS idx_copy_orders_time ON copy_orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_copy_orders_trader ON copy_orders(trader_uid);
CREATE INDEX IF NOT EXISTS idx_copy_orders_tracking ON copy_orders(tracking_no);
CREATE INDEX IF NOT EXISTS idx_source_events_trader ON source_trader_events(trader_uid);
CREATE INDEX IF NOT EXISTS idx_source_events_time ON source_trader_events(order_time);
CREATE INDEX IF NOT EXISTS idx_cycles_trader ON trader_position_cycles(trader_uid);
CREATE INDEX IF NOT EXISTS idx_cycles_open_time ON trader_position_cycles(open_time);
"""

# ── traders CRUD ──────────────────────────────────────────────────────────────

def upsert_trader(
    trader_uid: str,
    nickname: str,
    roi: float | None = None,
    win_rate: float | None = None,
    max_drawdown: float | None = None,
    total_profit: float | None = None,
    aum: float | None = None,
    follower_count: int | None = None,
    total_trades: int | None = None,
    copy_trade_days: int | None = None,
    avatar: str | None = None,
    profit_7d: float | None = None,
    profit_30d: float | None = None,
):
    # 黑名单检查：已删除的交易员不允许重新创建
    if is_deleted(trader_uid):
        logger.debug("交易员 %s 在删除黑名单中，拒绝 upsert", trader_uid[:8])
        return
    
    import time as _time
    now = int(_time.time())
    with get_conn() as conn:
        # 迁移旧表：添加新列（如果不存在）
        for col, dtype in [
            ("max_drawdown", "REAL"), ("total_profit", "REAL"), ("aum", "REAL"),
            ("avatar", "TEXT"), ("profit_7d", "REAL"), ("profit_30d", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE traders ADD COLUMN {col} {dtype}")
            except Exception:
                pass

        conn.execute(
            """
            INSERT INTO traders
                (trader_uid, nickname, first_seen, roi, win_rate, max_drawdown,
                 total_profit, aum, follower_count, total_trades,
                 copy_trade_days, last_updated, avatar, profit_7d, profit_30d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trader_uid) DO UPDATE SET
                nickname        = excluded.nickname,
                roi             = COALESCE(excluded.roi, traders.roi),
                win_rate        = COALESCE(excluded.win_rate, traders.win_rate),
                max_drawdown    = COALESCE(excluded.max_drawdown, traders.max_drawdown),
                total_profit    = COALESCE(excluded.total_profit, traders.total_profit),
                aum             = COALESCE(excluded.aum, traders.aum),
                follower_count  = COALESCE(excluded.follower_count, traders.follower_count),
                total_trades    = COALESCE(excluded.total_trades, traders.total_trades),
                copy_trade_days = COALESCE(excluded.copy_trade_days, traders.copy_trade_days),
                avatar          = COALESCE(excluded.avatar, traders.avatar),
                profit_7d       = COALESCE(excluded.profit_7d, traders.profit_7d),
                profit_30d      = COALESCE(excluded.profit_30d, traders.profit_30d),
                last_updated    = excluded.last_updated
            """,
            (
                trader_uid, nickname, now,
                roi, win_rate, max_drawdown,
                total_profit, aum, follower_count, total_trades,
                copy_trade_days, now, avatar, profit_7d, profit_30d,
            ),
        )
        conn.commit()


def get_all_traders() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM traders ORDER BY last_updated DESC").fetchall()
    return [dict(r) for r in rows]


def get_trader(trader_uid: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM traders WHERE trader_uid = ?", (trader_uid,)
        ).fetchone()
    return dict(row) if row else None


# ── trades CRUD ───────────────────────────────────────────────────────────────

def _migrate_trades(conn):
    """为旧 trades 表添加新列（幂等）。"""
    new_cols = [
        ("margin_mode", "TEXT"), ("position_size", "REAL"), ("gross_profit", "REAL"),
        ("open_fee", "REAL"), ("close_fee", "REAL"), ("funding_fee", "REAL"),
        ("follow_count", "INTEGER"), ("net_profit", "REAL"),
    ]
    for col, dtype in new_cols:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
        except Exception:
            pass


def _migrate_snapshots(conn):
    """为旧 snapshots 表添加新列（幂等）。"""
    new_cols = [
        ("margin_mode", "TEXT"), ("position_size", "REAL"), ("unrealized_pnl", "REAL"),
        ("return_rate", "REAL"), ("follow_count", "INTEGER"),
    ]
    for col, dtype in new_cols:
        try:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {dtype}")
        except Exception:
            pass


def insert_trade(trade: dict):
    """插入一条已完成交易（幂等：trade_id 冲突则忽略）。"""
    with get_conn() as conn:
        _migrate_trades(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO trades
                (trade_id, trader_uid, symbol, direction, leverage, margin_mode,
                 open_price, open_time, close_price, close_time,
                 hold_duration, position_size, pnl_pct, net_profit, gross_profit,
                 open_fee, close_fee, funding_fee, margin_amount, follow_count, is_win)
            VALUES
                (:trade_id, :trader_uid, :symbol, :direction, :leverage, :margin_mode,
                 :open_price, :open_time, :close_price, :close_time,
                 :hold_duration, :position_size, :pnl_pct, :net_profit, :gross_profit,
                 :open_fee, :close_fee, :funding_fee, :margin_amount, :follow_count, :is_win)
            """,
            trade,
        )
        conn.commit()


def get_latest_trade_time(trader_uid: str) -> int:
    """返回该交易员最新一条历史订单的平仓时间（毫秒），0 表示无记录。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(close_time) FROM trades WHERE trader_uid = ?", (trader_uid,)
        ).fetchone()
    return int(row[0]) if row and row[0] else 0


def insert_trades_bulk(trades: list[dict]):
    if not trades:
        return
    with get_conn() as conn:
        _migrate_trades(conn)
        conn.executemany(
            """
            INSERT OR IGNORE INTO trades
                (trade_id, trader_uid, symbol, direction, leverage, margin_mode,
                 open_price, open_time, close_price, close_time,
                 hold_duration, position_size, pnl_pct, net_profit, gross_profit,
                 open_fee, close_fee, funding_fee, margin_amount, follow_count, is_win)
            VALUES
                (:trade_id, :trader_uid, :symbol, :direction, :leverage, :margin_mode,
                 :open_price, :open_time, :close_price, :close_time,
                 :hold_duration, :position_size, :pnl_pct, :net_profit, :gross_profit,
                 :open_fee, :close_fee, :funding_fee, :margin_amount, :follow_count, :is_win)
            """,
            trades,
        )
        conn.commit()
    logger.debug("批量插入 %d 条交易记录", len(trades))


def get_trades(trader_uid: str, limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM trades WHERE trader_uid = ? ORDER BY close_time DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_conn() as conn:
        rows = conn.execute(sql, (trader_uid,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_close_time(trader_uid: str) -> int:
    """返回该交易员最新已平仓订单的 close_time（毫秒），没有则返回 0。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(close_time) as t FROM trades WHERE trader_uid = ?",
            (trader_uid,),
        ).fetchone()
    return (row["t"] or 0) if row else 0


# ── snapshots CRUD ────────────────────────────────────────────────────────────

def upsert_snapshot(snap: dict):
    """保存最新快照（线程安全）。"""
    import time as _time
    # 统一使用毫秒时间戳，避免与 replace_all_snapshots 的毫秒值混用导致“最新快照”判断失真。
    ts_raw = snap.get("timestamp")
    try:
        ts = int(float(ts_raw))
    except (TypeError, ValueError):
        ts = int(_time.time() * 1000)
    if ts < 100_000_000_000:  # 10位秒级时间戳
        ts *= 1000
    snap["timestamp"] = ts
    snap.setdefault("margin_mode", "cross")
    snap.setdefault("position_size", snap.get("open_amount", 0))
    snap.setdefault("unrealized_pnl", 0.0)
    snap.setdefault("return_rate", 0.0)
    snap.setdefault("follow_count", 0)
    snap.setdefault("tp_price", 0.0)
    snap.setdefault("sl_price", 0.0)
    with _db_write_lock:
        with get_conn() as conn:
            _migrate_snapshots(conn)
            conn.execute(
                """
                INSERT INTO snapshots
                    (trader_uid, timestamp, tracking_no, symbol, hold_side,
                     leverage, margin_mode, open_price, open_time, open_amount,
                     position_size, unrealized_pnl, return_rate, follow_count,
                     tp_price, sl_price)
                VALUES
                    (:trader_uid, :timestamp, :tracking_no, :symbol, :hold_side,
                     :leverage, :margin_mode, :open_price, :open_time, :open_amount,
                     :position_size, :unrealized_pnl, :return_rate, :follow_count,
                     :tp_price, :sl_price)
                ON CONFLICT(trader_uid, tracking_no) DO UPDATE SET
                    timestamp      = excluded.timestamp,
                    symbol         = excluded.symbol,
                    hold_side      = excluded.hold_side,
                    leverage       = excluded.leverage,
                    margin_mode    = excluded.margin_mode,
                    open_price     = excluded.open_price,
                    open_time      = excluded.open_time,
                    open_amount    = excluded.open_amount,
                    position_size  = excluded.position_size,
                    unrealized_pnl = excluded.unrealized_pnl,
                    return_rate    = excluded.return_rate,
                    follow_count   = excluded.follow_count,
                    tp_price       = excluded.tp_price,
                    sl_price       = excluded.sl_price
                """,
                snap,
            )
            conn.commit()


def get_snapshots(trader_uid: str) -> dict[str, dict]:
    """返回 {tracking_no: snapshot_dict} 的映射。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE trader_uid = ?", (trader_uid,)
        ).fetchall()
    return {r["tracking_no"]: dict(r) for r in rows}


def get_latest_snapshots(trader_uid: str) -> list[dict]:
    """
    返回指定交易员最新的持仓快照列表。
    用于当 currentList API 受限时，从本地快照推断当前持仓。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE trader_uid = ? AND symbol IS NOT NULL AND symbol != '' ORDER BY timestamp DESC",
            (trader_uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_snapshot(trader_uid: str, tracking_no: str):
    """删除快照（线程安全）。"""
    with _db_write_lock:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM snapshots WHERE trader_uid = ? AND tracking_no = ?",
                (trader_uid, tracking_no),
            )
            conn.commit()


def clear_snapshots(trader_uid: str):
    """清空快照（线程安全）。"""
    with _db_write_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM snapshots WHERE trader_uid = ?", (trader_uid,))
            conn.commit()


def replace_all_snapshots(trader_uid: str, snaps: list[dict]):
    """
    全量替换某个交易员的快照（线程安全）。
    用于采集器定期同步最新状态，避免与跟单引擎的快照更新产生竞争。
    """
    import time as _time
    now = int(_time.time() * 1000)

    def _clean_symbol(symbol: Any) -> str:
        s = str(symbol or "").upper()
        for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
            s = s.replace(suffix, "")
        return s

    with _db_write_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM snapshots WHERE trader_uid = ?", (trader_uid,))
            for s in snaps:
                # 兼容不同来源的字段名（scrapper vs internal）
                tracking_no = s.get("order_no") or s.get("tracking_no") or f"snap_{int(_time.time()*1000)}"
                conn.execute(
                    """
                    INSERT INTO snapshots
                        (trader_uid, timestamp, tracking_no, symbol, hold_side,
                         leverage, margin_mode, open_price, open_time,
                         open_amount, position_size, unrealized_pnl,
                         return_rate, follow_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trader_uid, now, tracking_no,
                        _clean_symbol(s.get("symbol")), s.get("direction") or s.get("hold_side"),
                        s.get("leverage", 1), s.get("margin_mode", "cross"),
                        s.get("open_price", 0), s.get("open_time", 0),
                        s.get("margin_amount", 0) or s.get("open_amount", 0),
                        s.get("position_size", 0),
                        s.get("unrealized_pnl", 0), s.get("return_rate", 0),
                        s.get("follow_count", 0)
                    )
                )
            conn.commit()


# ── copy_settings CRUD ─────────────────────────────────────────────────────────

def _ensure_copy_settings(conn) -> None:
    conn.execute("INSERT OR IGNORE INTO copy_settings (id) VALUES (1)")
    # ?????????????????
    for col, dtype, default in [
        ("follow_ratio_pct", "REAL", "0.003"),
        ("sl_pct", "REAL", "0.15"),
        ("tp_pct", "REAL", "0.30"),
        ("daily_loss_limit_pct", "REAL", str(config.DEFAULT_DAILY_LOSS_LIMIT_PCT)),
        ("total_drawdown_limit_pct", "REAL", str(config.DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT)),
        ("take_profit_enabled", "INTEGER", "1" if config.DEFAULT_TAKE_PROFIT_ENABLED else "0"),
        ("stop_loss_pct", "REAL", str(config.DEFAULT_STOP_LOSS_PCT)),
        ("tp1_roi_pct", "REAL", str(config.DEFAULT_TP1_ROI_PCT)),
        ("tp1_close_pct", "REAL", str(config.DEFAULT_TP1_CLOSE_PCT)),
        ("tp2_roi_pct", "REAL", str(config.DEFAULT_TP2_ROI_PCT)),
        ("tp2_close_pct", "REAL", str(config.DEFAULT_TP2_CLOSE_PCT)),
        ("tp3_roi_pct", "REAL", str(config.DEFAULT_TP3_ROI_PCT)),
        ("tp3_close_pct", "REAL", str(config.DEFAULT_TP3_CLOSE_PCT)),
        ("breakeven_buffer_pct", "REAL", str(config.DEFAULT_BREAKEVEN_BUFFER_PCT)),
        ("trail_callback_pct", "REAL", str(config.DEFAULT_TRAIL_CALLBACK_PCT)),
        ("entry_order_mode", "TEXT", f"'{config.DEFAULT_ENTRY_ORDER_MODE}'"),
        ("entry_maker_levels", "INTEGER", str(config.DEFAULT_ENTRY_MAKER_LEVELS)),
        ("entry_limit_timeout_sec", "INTEGER", str(config.DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC)),
        ("entry_limit_fallback_to_market", "INTEGER", "1" if config.DEFAULT_ENTRY_LIMIT_FALLBACK_TO_MARKET else "0"),
        ("binance_traders", "TEXT", "'[]'"),
        ("binance_api_key", "TEXT", "''"),
        ("binance_api_secret", "TEXT", "''"),
        ("binance_total_capital", "REAL", "0"),
        ("binance_follow_ratio_pct", "REAL", "0.003"),
        ("binance_max_margin_pct", "REAL", "0.20"),
        ("binance_price_tolerance", "REAL", "0.01"),
    ]:
        try:
            conn.execute(f"ALTER TABLE copy_settings ADD COLUMN {col} {dtype} DEFAULT {default}")
        except Exception:
            pass


def get_copy_settings() -> dict:
    with get_conn() as conn:
        _ensure_copy_settings(conn)
        row = conn.execute("SELECT * FROM copy_settings WHERE id = 1").fetchone()
        conn.commit()

    data = dict(row) if row else {}
    if not data:
        return {}

    # ??????? .env ??????????? SQLite ???????
    data["api_key"] = data.get("api_key") or config.BITGET_API_KEY
    data["api_secret"] = data.get("api_secret") or config.BITGET_SECRET_KEY
    data["api_passphrase"] = data.get("api_passphrase") or config.BITGET_PASSPHRASE
    data["binance_api_key"] = data.get("binance_api_key") or config.BINANCE_API_KEY
    data["binance_api_secret"] = data.get("binance_api_secret") or config.BINANCE_API_SECRET
    return data


_COPY_SETTINGS_COLS = frozenset({
    "api_key", "api_secret", "api_passphrase",
    "total_capital", "follow_ratio_pct", "max_margin_pct", "price_tolerance",
    "sl_pct", "tp_pct", "daily_loss_limit_pct", "total_drawdown_limit_pct",
    "take_profit_enabled", "stop_loss_pct",
    "tp1_roi_pct", "tp1_close_pct",
    "tp2_roi_pct", "tp2_close_pct",
    "tp3_roi_pct", "tp3_close_pct",
    "breakeven_buffer_pct", "trail_callback_pct",
    "entry_order_mode", "entry_maker_levels",
    "entry_limit_timeout_sec", "entry_limit_fallback_to_market",
    "enabled_traders", "binance_traders", "engine_enabled",
    "binance_api_key", "binance_api_secret",
    "binance_total_capital", "binance_follow_ratio_pct",
    "binance_max_margin_pct", "binance_price_tolerance",
})

_SHARED_COPY_SETTINGS_COLS = frozenset({
    "enabled_traders",
    "binance_traders",
})


def update_copy_settings(**kwargs: Any) -> None:
    if not kwargs:
        return
    # 白名单校验，拒绝未知列名防止 SQL 注入
    invalid = set(kwargs.keys()) - _COPY_SETTINGS_COLS
    if invalid:
        raise ValueError(f"update_copy_settings: 非法列名 {invalid}")
    columns = ", ".join([f"{k} = :{k}" for k in kwargs.keys()])
    params = dict(kwargs)
    params["id"] = 1
    with get_conn() as conn:
        _ensure_copy_settings(conn)
        conn.execute(f"UPDATE copy_settings SET {columns} WHERE id = :id", params)
        conn.commit()


def set_copy_api_credentials(api_key: str, api_secret: str, api_passphrase: str) -> None:
    raise RuntimeError("set_copy_api_credentials ????????? .env????????? SQLite")


def set_copy_params(total_capital: float, max_margin_pct: float, price_tolerance: float) -> None:
    update_copy_settings(
        total_capital=total_capital,
        max_margin_pct=max_margin_pct,
        price_tolerance=price_tolerance,
    )


def set_enabled_traders(enabled_traders_json: str) -> None:
    update_copy_settings(enabled_traders=enabled_traders_json)


def set_engine_enabled(enabled: bool) -> None:
    update_copy_settings(engine_enabled=1 if enabled else 0)


def _normalize_copy_profile_name(profile: str | None) -> str:
    profile_key = str(profile or "sim").strip().lower()
    if profile_key in {"", "default", "paper", "sim", "simulation"}:
        return "sim"
    if profile_key in {"live", "real", "production", "prod"}:
        return "live"
    return profile_key


def _ensure_copy_profile_settings(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS copy_profile_settings (
            profile         TEXT PRIMARY KEY,
            settings_json   TEXT NOT NULL DEFAULT '{}',
            updated_at      INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _default_copy_settings_payload(profile: str = "sim") -> dict[str, Any]:
    profile_key = _normalize_copy_profile_name(profile)
    payload: dict[str, Any] = {
        "api_key": "",
        "api_secret": "",
        "api_passphrase": "",
        "total_capital": 0.0,
        "follow_ratio_pct": 0.003,
        "max_margin_pct": 0.20,
        "price_tolerance": 0.01,
        "sl_pct": 0.15,
        "tp_pct": 0.30,
        "daily_loss_limit_pct": config.DEFAULT_DAILY_LOSS_LIMIT_PCT,
        "total_drawdown_limit_pct": config.DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT,
        "take_profit_enabled": 1 if config.DEFAULT_TAKE_PROFIT_ENABLED else 0,
        "stop_loss_pct": config.DEFAULT_STOP_LOSS_PCT,
        "tp1_roi_pct": config.DEFAULT_TP1_ROI_PCT,
        "tp1_close_pct": config.DEFAULT_TP1_CLOSE_PCT,
        "tp2_roi_pct": config.DEFAULT_TP2_ROI_PCT,
        "tp2_close_pct": config.DEFAULT_TP2_CLOSE_PCT,
        "tp3_roi_pct": config.DEFAULT_TP3_ROI_PCT,
        "tp3_close_pct": config.DEFAULT_TP3_CLOSE_PCT,
        "breakeven_buffer_pct": config.DEFAULT_BREAKEVEN_BUFFER_PCT,
        "trail_callback_pct": config.DEFAULT_TRAIL_CALLBACK_PCT,
        "entry_order_mode": config.DEFAULT_ENTRY_ORDER_MODE,
        "entry_maker_levels": config.DEFAULT_ENTRY_MAKER_LEVELS,
        "entry_limit_timeout_sec": config.DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC,
        "entry_limit_fallback_to_market": 1 if config.DEFAULT_ENTRY_LIMIT_FALLBACK_TO_MARKET else 0,
        "enabled_traders": "[]",
        "binance_traders": "{}",
        "engine_enabled": 0,
        "binance_api_key": "",
        "binance_api_secret": "",
        "binance_total_capital": 0.0,
        "binance_follow_ratio_pct": 0.003,
        "binance_max_margin_pct": 0.20,
        "binance_price_tolerance": 0.01,
    }
    if profile_key == "live":
        payload["api_key"] = os.getenv("LIVE_BITGET_API_KEY", "")
        payload["api_secret"] = os.getenv("LIVE_BITGET_SECRET_KEY", "")
        payload["api_passphrase"] = os.getenv("LIVE_BITGET_PASSPHRASE", "")
        payload["binance_api_key"] = os.getenv("LIVE_BINANCE_API_KEY", "")
        payload["binance_api_secret"] = os.getenv("LIVE_BINANCE_API_SECRET", "")
    else:
        payload["api_key"] = config.BITGET_API_KEY
        payload["api_secret"] = config.BITGET_SECRET_KEY
        payload["api_passphrase"] = config.BITGET_PASSPHRASE
        payload["binance_api_key"] = config.BINANCE_API_KEY
        payload["binance_api_secret"] = config.BINANCE_API_SECRET
    return payload


def get_copy_settings_profile(profile: str | None = "sim") -> dict:
    profile_key = _normalize_copy_profile_name(profile)
    if profile_key == "sim":
        return get_copy_settings()

    with get_conn() as conn:
        _ensure_copy_profile_settings(conn)
        row = conn.execute(
            "SELECT settings_json FROM copy_profile_settings WHERE profile = ?",
            (profile_key,),
        ).fetchone()
        conn.commit()

    data = _default_copy_settings_payload(profile_key)
    sim_data: dict[str, Any] = {}
    if profile_key == "live":
        sim_data = get_copy_settings()
        for key in _COPY_SETTINGS_COLS:
            if key in {"api_key", "api_secret", "api_passphrase", "binance_api_key", "binance_api_secret"}:
                continue
            if key in sim_data:
                data[key] = sim_data[key]
    if row and row["settings_json"]:
        try:
            payload = json.loads(row["settings_json"])
            if isinstance(payload, dict):
                data.update(payload)
        except Exception:
            pass

    if profile_key == "live":
        # Trader selection is shared between sim/live pages; always trust the
        # main copy settings so live cannot keep following a stale hidden list.
        for key in _SHARED_COPY_SETTINGS_COLS:
            if key in sim_data:
                data[key] = sim_data[key]

    data["api_key"] = data.get("api_key") or os.getenv("LIVE_BITGET_API_KEY", "") or config.BITGET_API_KEY
    data["api_secret"] = data.get("api_secret") or os.getenv("LIVE_BITGET_SECRET_KEY", "") or config.BITGET_SECRET_KEY
    data["api_passphrase"] = data.get("api_passphrase") or os.getenv("LIVE_BITGET_PASSPHRASE", "") or config.BITGET_PASSPHRASE
    data["binance_api_key"] = data.get("binance_api_key") or os.getenv("LIVE_BINANCE_API_KEY", "") or config.BINANCE_API_KEY
    data["binance_api_secret"] = data.get("binance_api_secret") or os.getenv("LIVE_BINANCE_API_SECRET", "") or config.BINANCE_API_SECRET
    return data


def update_copy_settings_profile(profile: str | None = "sim", **kwargs: Any) -> None:
    profile_key = _normalize_copy_profile_name(profile)
    if profile_key == "sim":
        update_copy_settings(**kwargs)
        return
    if not kwargs:
        return

    invalid = set(kwargs.keys()) - _COPY_SETTINGS_COLS
    if invalid:
        raise ValueError(f"update_copy_settings_profile: ???? {invalid}")

    with _db_write_lock:
        with get_conn() as conn:
            _ensure_copy_profile_settings(conn)
            row = conn.execute(
                "SELECT settings_json FROM copy_profile_settings WHERE profile = ?",
                (profile_key,),
            ).fetchone()
            current: dict[str, Any] = {}
            if row and row["settings_json"]:
                try:
                    payload = json.loads(row["settings_json"])
                    if isinstance(payload, dict):
                        current = payload
                except Exception:
                    current = {}
            current.update(kwargs)
            clean_payload = {k: current.get(k) for k in _COPY_SETTINGS_COLS if k in current}
            now = int(time.time())
            conn.execute(
                """
                INSERT INTO copy_profile_settings (profile, settings_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    settings_json = excluded.settings_json,
                    updated_at = excluded.updated_at
                """,
                (profile_key, json.dumps(clean_payload, ensure_ascii=False), now),
            )
            conn.commit()


def update_shared_copy_settings(**kwargs: Any) -> None:
    if not kwargs:
        return
    invalid = set(kwargs.keys()) - _SHARED_COPY_SETTINGS_COLS
    if invalid:
        raise ValueError(f"update_shared_copy_settings: 非法列名 {invalid}")
    update_copy_settings(**kwargs)
    update_copy_settings_profile("live", **kwargs)


def set_engine_enabled_profile(profile: str | None, enabled: bool) -> None:
    update_copy_settings_profile(profile, engine_enabled=1 if enabled else 0)


# ── account_daily_equity（日收益基准） ─────────────────────────────────────────

def _migrate_account_daily_equity(conn) -> None:
    """为旧数据库补齐 account_daily_equity 字段（幂等）。"""
    for col, dtype, default in [
        ("start_ts", "INTEGER", "0"),
        ("last_equity", "REAL", "0"),
        ("updated_at", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE account_daily_equity ADD COLUMN {col} {dtype} DEFAULT {default}")
        except Exception:
            pass


def upsert_account_daily_equity(day: str, equity: float) -> dict:
    """
    ?????????
    - ???????start_equity = ?? equity????????
    - ???????? last_equity/updated_at
    """
    now = int(time.time())
    eq = float(equity)

    with _db_write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_daily_equity (
                    day             TEXT PRIMARY KEY,
                    start_equity    REAL NOT NULL,
                    start_ts        INTEGER NOT NULL,
                    last_equity     REAL NOT NULL,
                    updated_at      INTEGER NOT NULL
                )
                """
            )
            _migrate_account_daily_equity(conn)
            row = conn.execute(
                "SELECT start_equity, start_ts FROM account_daily_equity WHERE day = ?",
                (day,),
            ).fetchone()
            if row:
                start_equity = float(row["start_equity"] or 0.0)
                start_ts = int(row["start_ts"] or now)
                conn.execute(
                    "UPDATE account_daily_equity SET last_equity = ?, updated_at = ? WHERE day = ?",
                    (eq, now, day),
                )
            else:
                start_equity = eq
                start_ts = now
                conn.execute(
                    """
                    INSERT INTO account_daily_equity
                        (day, start_equity, start_ts, last_equity, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (day, eq, now, eq, now),
                )

            # ???? 60 ?
            cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 60 * 86400))
            conn.execute("DELETE FROM account_daily_equity WHERE day < ?", (cutoff,))
            conn.commit()

    return {
        "day": day,
        "start_equity": start_equity,
        "start_ts": start_ts,
        "current_equity": eq,
        "day_pnl": eq - start_equity,
    }


def _platform_day_bounds_ms(day: str) -> tuple[int, int]:
    try:
        start_ms = int(time.mktime(time.strptime(day, "%Y-%m-%d"))) * 1000
    except Exception:
        start_ms = int(time.time()) * 1000
    return start_ms, start_ms + 86400 * 1000



def _should_reset_live_platform_baseline(conn, platform_key: str, day: str, start_equity: float, current_equity: float) -> bool:
    if not platform_key.startswith("live_"):
        return False
    if start_equity < 1000 or current_equity <= 0 or current_equity > 500:
        return False
    if (start_equity / max(current_equity, 1e-9)) < 10:
        return False

    day_start_ms, day_end_ms = _platform_day_bounds_ms(day)
    row = conn.execute(
        """
        SELECT COUNT(1) AS cnt
        FROM copy_orders
        WHERE platform = ?
          AND timestamp >= ?
          AND timestamp < ?
          AND action = 'open'
          AND status = 'filled'
        """,
        (platform_key, day_start_ms, day_end_ms),
    ).fetchone()
    filled_open_count = int((row["cnt"] if row else 0) or 0)
    return filled_open_count == 0



def upsert_platform_daily_equity(platform: str, day: str, equity: float) -> dict:
    """?????????????????"""
    now = int(time.time())
    eq = float(equity)
    platform_key = str(platform or "unknown").strip().lower() or "unknown"
    baseline_reset = False

    with _db_write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_daily_equity (
                    platform        TEXT NOT NULL,
                    day             TEXT NOT NULL,
                    start_equity    REAL NOT NULL,
                    start_ts        INTEGER NOT NULL,
                    last_equity     REAL NOT NULL,
                    updated_at      INTEGER NOT NULL,
                    PRIMARY KEY (platform, day)
                )
                """
            )
            row = conn.execute(
                "SELECT start_equity, start_ts FROM platform_daily_equity WHERE platform = ? AND day = ?",
                (platform_key, day),
            ).fetchone()
            if row:
                start_equity = float(row["start_equity"] or 0.0)
                start_ts = int(row["start_ts"] or now)
                if _should_reset_live_platform_baseline(conn, platform_key, day, start_equity, eq):
                    logger.warning(
                        "[auto-reset daily equity] %s start=%.4f current=%.4f",
                        platform_key,
                        start_equity,
                        eq,
                    )
                    start_equity = eq
                    start_ts = now
                    baseline_reset = True
                    conn.execute(
                        """
                        UPDATE platform_daily_equity
                        SET start_equity = ?, start_ts = ?, last_equity = ?, updated_at = ?
                        WHERE platform = ? AND day = ?
                        """,
                        (eq, now, eq, now, platform_key, day),
                    )
                else:
                    conn.execute(
                        "UPDATE platform_daily_equity SET last_equity = ?, updated_at = ? WHERE platform = ? AND day = ?",
                        (eq, now, platform_key, day),
                    )
            else:
                start_equity = eq
                start_ts = now
                conn.execute(
                    """
                    INSERT INTO platform_daily_equity
                        (platform, day, start_equity, start_ts, last_equity, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (platform_key, day, eq, now, eq, now),
                )

            cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 60 * 86400))
            conn.execute("DELETE FROM platform_daily_equity WHERE day < ?", (cutoff,))
            conn.commit()

    return {
        "platform": platform_key,
        "day": day,
        "start_equity": start_equity,
        "start_ts": start_ts,
        "current_equity": eq,
        "day_pnl": eq - start_equity,
        "baseline_reset": baseline_reset,
    }


def get_platform_equity_peak(platform: str, since_days: int = 60) -> float:
    """??? N ????????????????"""
    now = int(time.time())
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - max(1, int(since_days)) * 86400))
    platform_key = str(platform or "unknown").strip().lower() or "unknown"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT start_equity, last_equity FROM platform_daily_equity WHERE platform = ? AND day >= ?",
            (platform_key, cutoff),
        ).fetchall()
    peak = 0.0
    for row in rows:
        peak = max(peak, float(row["start_equity"] or 0.0), float(row["last_equity"] or 0.0))
    return peak


def _normalize_position_state_key(platform: str, trader_uid: str, symbol: str, direction: str) -> tuple[str, str, str, str]:
    return (
        str(platform or "unknown").strip().lower() or "unknown",
        str(trader_uid or "").strip(),
        _clean_symbol_value(symbol),
        str(direction or "").strip().lower(),
    )


def _migrate_copy_position_states(conn) -> None:
    for col, dtype, default in [
        ("stage", "INTEGER", "0"),
        ("peak_roi", "REAL", "0"),
        ("locked_roi_pct", "REAL", "0"),
        ("breakeven_armed", "INTEGER", "0"),
        ("trail_active", "INTEGER", "0"),
        ("closed_by_system", "INTEGER", "0"),
        ("freeze_reentry", "INTEGER", "0"),
        ("last_source_order_id", "TEXT", "''"),
        ("last_system_action", "TEXT", "''"),
        ("created_at", "INTEGER", "0"),
        ("updated_at", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE copy_position_states ADD COLUMN {col} {dtype} DEFAULT {default}")
        except Exception:
            pass


def get_copy_position_state(platform: str, trader_uid: str, symbol: str, direction: str) -> dict:
    platform_key, trader_key, symbol_key, direction_key = _normalize_position_state_key(platform, trader_uid, symbol, direction)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS copy_position_states (
                platform        TEXT NOT NULL,
                trader_uid      TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                stage           INTEGER DEFAULT 0,
                peak_roi        REAL DEFAULT 0,
                locked_roi_pct  REAL DEFAULT 0,
                breakeven_armed INTEGER DEFAULT 0,
                trail_active    INTEGER DEFAULT 0,
                closed_by_system INTEGER DEFAULT 0,
                freeze_reentry  INTEGER DEFAULT 0,
                last_source_order_id TEXT DEFAULT '',
                last_system_action TEXT DEFAULT '',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY (platform, trader_uid, symbol, direction)
            )
            """
        )
        _migrate_copy_position_states(conn)
        row = conn.execute(
            """
            SELECT * FROM copy_position_states
            WHERE platform = ? AND trader_uid = ? AND symbol = ? AND direction = ?
            """,
            (platform_key, trader_key, symbol_key, direction_key),
        ).fetchone()
    return dict(row) if row else {}


def upsert_copy_position_state(platform: str, trader_uid: str, symbol: str, direction: str, **fields: Any) -> dict:
    platform_key, trader_key, symbol_key, direction_key = _normalize_position_state_key(platform, trader_uid, symbol, direction)
    now = int(time.time())

    with _db_write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_position_states (
                    platform        TEXT NOT NULL,
                    trader_uid      TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    stage           INTEGER DEFAULT 0,
                    peak_roi        REAL DEFAULT 0,
                    locked_roi_pct  REAL DEFAULT 0,
                    breakeven_armed INTEGER DEFAULT 0,
                    trail_active    INTEGER DEFAULT 0,
                    closed_by_system INTEGER DEFAULT 0,
                    freeze_reentry  INTEGER DEFAULT 0,
                    last_source_order_id TEXT DEFAULT '',
                    last_system_action TEXT DEFAULT '',
                    created_at      INTEGER NOT NULL,
                    updated_at      INTEGER NOT NULL,
                    PRIMARY KEY (platform, trader_uid, symbol, direction)
                )
                """
            )
            _migrate_copy_position_states(conn)
            current = conn.execute(
                """
                SELECT * FROM copy_position_states
                WHERE platform = ? AND trader_uid = ? AND symbol = ? AND direction = ?
                """,
                (platform_key, trader_key, symbol_key, direction_key),
            ).fetchone()
            payload = dict(current) if current else {
                "platform": platform_key,
                "trader_uid": trader_key,
                "symbol": symbol_key,
                "direction": direction_key,
                "stage": 0,
                "peak_roi": 0.0,
                "locked_roi_pct": 0.0,
                "breakeven_armed": 0,
                "trail_active": 0,
                "closed_by_system": 0,
                "freeze_reentry": 0,
                "last_source_order_id": "",
                "last_system_action": "",
                "created_at": now,
                "updated_at": now,
            }
            payload.update(fields)
            payload["platform"] = platform_key
            payload["trader_uid"] = trader_key
            payload["symbol"] = symbol_key
            payload["direction"] = direction_key
            payload["created_at"] = int(payload.get("created_at") or now)
            payload["updated_at"] = now
            conn.execute(
                """
                INSERT INTO copy_position_states (
                    platform, trader_uid, symbol, direction, stage, peak_roi,
                    locked_roi_pct, breakeven_armed, trail_active, closed_by_system,
                    freeze_reentry, last_source_order_id, last_system_action, created_at, updated_at
                ) VALUES (
                    :platform, :trader_uid, :symbol, :direction, :stage, :peak_roi,
                    :locked_roi_pct, :breakeven_armed, :trail_active, :closed_by_system,
                    :freeze_reentry, :last_source_order_id, :last_system_action, :created_at, :updated_at
                )
                ON CONFLICT(platform, trader_uid, symbol, direction) DO UPDATE SET
                    stage = excluded.stage,
                    peak_roi = excluded.peak_roi,
                    locked_roi_pct = excluded.locked_roi_pct,
                    breakeven_armed = excluded.breakeven_armed,
                    trail_active = excluded.trail_active,
                    closed_by_system = excluded.closed_by_system,
                    freeze_reentry = excluded.freeze_reentry,
                    last_source_order_id = excluded.last_source_order_id,
                    last_system_action = excluded.last_system_action,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
            conn.commit()
    return get_copy_position_state(platform_key, trader_key, symbol_key, direction_key)


def clear_copy_position_state(platform: str, trader_uid: str, symbol: str, direction: str) -> None:
    platform_key, trader_key, symbol_key, direction_key = _normalize_position_state_key(platform, trader_uid, symbol, direction)
    with _db_write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                DELETE FROM copy_position_states
                WHERE platform = ? AND trader_uid = ? AND symbol = ? AND direction = ?
                """,
                (platform_key, trader_key, symbol_key, direction_key),
            )
            conn.commit()


def get_active_copy_position_summaries(platform: str | None = None) -> list[dict]:
    with get_conn() as conn:
        _migrate_copy_orders(conn)
        rows = conn.execute(
            """
            SELECT timestamp, trader_uid, tracking_no, symbol, direction,
                   leverage, margin_usdt, source_price, exec_price,
                   action, status, notes, exec_qty, platform
            FROM copy_orders
            WHERE status = 'filled'
            ORDER BY timestamp ASC, id ASC
            """
        ).fetchall()

    active: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        platform_key, trader_key, symbol_key, direction_key = _normalize_position_state_key(
            item.get("platform") or "bitget",
            item.get("trader_uid") or "",
            item.get("symbol") or "",
            item.get("direction") or "",
        )
        if platform and platform_key != str(platform).strip().lower():
            continue
        if not trader_key or not symbol_key or direction_key not in ("long", "short"):
            continue
        key = (platform_key, trader_key, symbol_key, direction_key)
        pos = active.setdefault(key, {
            "platform": platform_key,
            "trader_uid": trader_key,
            "symbol": symbol_key,
            "direction": direction_key,
            "remaining_qty": 0.0,
            "remaining_margin": 0.0,
            "avg_entry_price": 0.0,
            "cycle_open_qty": 0.0,
            "cycle_open_margin": 0.0,
            "last_open_tracking_no": "",
            "last_open_ts": 0,
            "last_action_ts": 0,
            "leverage": 0,
        })
        pos["last_action_ts"] = max(int(item.get("timestamp") or 0), pos["last_action_ts"])

        action = str(item.get("action") or "").lower()
        qty = abs(float(item.get("exec_qty") or 0.0))
        if qty <= 0:
            continue

        if action == "open":
            if pos["remaining_qty"] <= 1e-12:
                pos["remaining_qty"] = 0.0
                pos["remaining_margin"] = 0.0
                pos["avg_entry_price"] = 0.0
                pos["cycle_open_qty"] = 0.0
                pos["cycle_open_margin"] = 0.0
            price = float(item.get("exec_price") or item.get("source_price") or 0.0)
            before_qty = pos["remaining_qty"]
            if price > 0:
                if before_qty > 0:
                    pos["avg_entry_price"] = ((pos["avg_entry_price"] * before_qty) + (price * qty)) / (before_qty + qty)
                else:
                    pos["avg_entry_price"] = price
            pos["remaining_qty"] = before_qty + qty
            pos["remaining_margin"] += float(item.get("margin_usdt") or 0.0)
            pos["cycle_open_qty"] += qty
            pos["cycle_open_margin"] += float(item.get("margin_usdt") or 0.0)
            pos["last_open_tracking_no"] = str(item.get("tracking_no") or "")
            pos["last_open_ts"] = int(item.get("timestamp") or 0)
            pos["leverage"] = int(item.get("leverage") or pos["leverage"] or 0)
        elif action == "close" and pos["remaining_qty"] > 0:
            close_qty = min(qty, pos["remaining_qty"])
            before_qty = pos["remaining_qty"]
            pos["remaining_qty"] = max(0.0, before_qty - close_qty)
            if before_qty > 0:
                pos["remaining_margin"] = max(0.0, pos["remaining_margin"] * (1.0 - (close_qty / before_qty)))
            if pos["remaining_qty"] <= 1e-12:
                pos["remaining_qty"] = 0.0
                pos["remaining_margin"] = 0.0
                pos["avg_entry_price"] = 0.0
                pos["cycle_open_qty"] = 0.0
                pos["cycle_open_margin"] = 0.0
                pos["last_open_tracking_no"] = ""
                pos["last_open_ts"] = 0

    return [pos for pos in active.values() if pos.get("remaining_qty", 0.0) > 1e-12]


def _migrate_copy_orders(conn) -> None:
    try:
        conn.execute("ALTER TABLE copy_orders ADD COLUMN exec_qty REAL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE copy_orders ADD COLUMN platform TEXT DEFAULT 'bitget'")
    except Exception:
        pass


def insert_copy_order(order: dict) -> int:
    """插入跟单记录（线程安全）。"""
    with _db_write_lock:
        with get_conn() as conn:
            _migrate_copy_orders(conn)
            cur = conn.execute(
                """
                INSERT INTO copy_orders
                    (timestamp, trader_uid, tracking_no, my_order_id, symbol,
                     direction, leverage, margin_usdt, source_price, exec_price,
                     deviation_pct, action, status, pnl, notes, exec_qty, platform)
                VALUES
                    (:timestamp, :trader_uid, :tracking_no, :my_order_id, :symbol,
                     :direction, :leverage, :margin_usdt, :source_price, :exec_price,
                     :deviation_pct, :action, :status, :pnl, :notes, :exec_qty, :platform)
                """,
                {**order, "exec_qty": order.get("exec_qty", 0.0), "platform": order.get("platform", "bitget")},
            )
            conn.commit()
            return int(cur.lastrowid)


def update_copy_order(
    order_id: int,
    status: str | None = None,
    exec_price: float | None = None,
    pnl: float | None = None,
    notes: str | None = None,
    my_order_id: str | None = None,
    deviation_pct: float | None = None,
) -> None:
    updates: dict[str, Any] = {}
    if status is not None:
        updates["status"] = status
    if exec_price is not None:
        updates["exec_price"] = exec_price
    if pnl is not None:
        updates["pnl"] = pnl
    if notes is not None:
        updates["notes"] = notes
    if my_order_id is not None:
        updates["my_order_id"] = my_order_id
    if deviation_pct is not None:
        updates["deviation_pct"] = deviation_pct
    if not updates:
        return
    columns = ", ".join([f"{k} = :{k}" for k in updates.keys()])
    updates["id"] = order_id
    with get_conn() as conn:
        conn.execute(f"UPDATE copy_orders SET {columns} WHERE id = :id", updates)
        conn.commit()


def get_copy_orders(limit: int = 50, offset: int = 0, platforms: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    sql = "SELECT * FROM copy_orders"
    params: list[Any] = []
    if platforms:
        platform_list = [str(p).strip().lower() for p in platforms if str(p).strip()]
        if platform_list:
            placeholders = ", ".join(["?"] * len(platform_list))
            sql += f" WHERE lower(platform) IN ({placeholders})"
            params.extend(platform_list)
    sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_copy_orders_by_tracking(
    trader_uid: str,
    tracking_no: str,
    action: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM copy_orders WHERE trader_uid = ? AND tracking_no = ?"
    params: list[Any] = [trader_uid, tracking_no]
    if action:
        sql += " AND action = ?"
        params.append(action)
    sql += " ORDER BY timestamp DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_open_copy_orders(trader_uid: str, symbol: str, direction: str, platform: str = 'bitget') -> list[dict]:
    """获取指定交易员、币种、方向、平台下所有已成单的开仓记录（用于合并平仓）"""
    with get_conn() as conn:
        rows = conn.execute('''
            SELECT * FROM copy_orders 
            WHERE trader_uid = ? AND symbol = ? AND direction = ? 
              AND action = 'open' AND status = 'filled' AND platform = ?
        ''', (trader_uid, symbol, direction, platform)).fetchall()
    return [dict(r) for r in rows]


def has_tracking_no(trader_uid: str, tracking_no: str, platform: str = 'bitget') -> bool:
    """检查指定平台是否存在使用该 tracking_no 的任何记录（用于防重复执行信号）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM copy_orders WHERE trader_uid = ? AND tracking_no = ? AND platform = ? LIMIT 1",
            (trader_uid, tracking_no, platform)
        ).fetchone()
    return bool(row)


def get_last_copy_order(symbol: str, direction: str):
    """查询该币种和方向下最近一次成功的跟单开仓记录，用于账户持仓关联。"""
    with get_conn() as conn:
        row = conn.execute('''
            SELECT * FROM copy_orders 
            WHERE symbol = ? AND direction = ? AND action = 'open' AND status = 'filled'
            ORDER BY id DESC LIMIT 1
        ''', (symbol, direction)).fetchone()
    return dict(row) if row else None


# ── 延迟监控与交易员性能统计 (Added 2026-03-14) ────────────────────────────


def record_trade_delay(trader_uid: str, delay_ms: int, slippage_pct: float):
    """记录单笔跟单的延迟和滑点"""
    import time
    today = time.strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trader_performance 
                (trader_uid, date, total_orders, avg_delay_ms, avg_slippage)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(trader_uid, date) DO UPDATE SET
                total_orders = total_orders + 1,
                avg_delay_ms = ((avg_delay_ms * (total_orders - 1)) + ?) / total_orders,
                avg_slippage = ((avg_slippage * (total_orders - 1)) + ?) / total_orders
        """, (trader_uid, today, delay_ms, slippage_pct, delay_ms, slippage_pct))
        conn.commit()


def get_trader_performance(trader_uid: str, days: int = 7) -> dict:
    """获取交易员最近N天的性能统计"""
    import time
    cutoff_date = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                AVG(avg_delay_ms) as avg_delay,
                AVG(avg_slippage) as avg_slippage,
                SUM(total_orders) as total_orders,
                MAX(date) as last_date,
                AVG(CASE WHEN date >= ? THEN pnl_7d ELSE NULL END) as recent_pnl
            FROM trader_performance
            WHERE trader_uid = ? AND date >= ?
        """, (cutoff_date, trader_uid, cutoff_date)).fetchone()
        
        if rows:
            return dict(rows)
        return {
            "avg_delay": 0,
            "avg_slippage": 0,
            "total_orders": 0,
            "last_date": None,
            "recent_pnl": 0,
        }


def should_pause_trader(trader_uid: str) -> tuple:
    """判断是否应该暂停交易员"""
    perf = get_trader_performance(trader_uid, days=7)
    
    if perf["avg_delay"] > 15000:
        return True, f"平均延迟过高: {perf['avg_delay']/1000:.1f}秒"
    
    if perf["avg_slippage"] > 0.01:
        return True, f"平均滑点过高: {perf['avg_slippage']*100:.2f}%"
    
    if perf["total_orders"] >= 10 and perf["recent_pnl"] < -500:
        return True, f"近7天亏损: ${perf['recent_pnl']:.2f}"
    
    return False, ""


def update_trader_pnl_stats():
    """更新所有交易员的7天/30天盈亏统计"""
    import time
    today = time.strftime("%Y-%m-%d")
    cutoff_7d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    cutoff_30d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    
    with get_conn() as conn:
        traders = conn.execute("SELECT DISTINCT trader_uid FROM copy_orders").fetchall()
        
        for row in traders:
            trader_uid = row["trader_uid"]
            
            pnl_7d = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM copy_orders
                WHERE trader_uid = ? AND action = 'close' AND status = 'filled'
                  AND date(timestamp / 1000, 'unixepoch', 'localtime') >= ?
            """, (trader_uid, cutoff_7d)).fetchone()["total_pnl"]
            
            pnl_30d = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM copy_orders
                WHERE trader_uid = ? AND action = 'close' AND status = 'filled'
                  AND date(timestamp / 1000, 'unixepoch', 'localtime') >= ?
            """, (trader_uid, cutoff_30d)).fetchone()["total_pnl"]
            
            conn.execute("""
                INSERT INTO trader_performance 
                    (trader_uid, date, pnl_7d, pnl_30d, total_orders)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(trader_uid, date) DO UPDATE SET
                    pnl_7d = ?, pnl_30d = ?
            """, (trader_uid, today, pnl_7d, pnl_30d, pnl_7d, pnl_30d))
        
        conn.commit()
    logger.info("更新了 %d 个交易员的盈亏统计", len(traders))


# ── 延迟监控与交易员性能统计 (Added 2026-03-14) ────────────────────────────


def record_trade_delay(trader_uid: str, delay_ms: int, slippage_pct: float):
    """记录单笔跟单的延迟和滑点"""
    import time
    today = time.strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trader_performance 
                (trader_uid, date, total_orders, avg_delay_ms, avg_slippage)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(trader_uid, date) DO UPDATE SET
                total_orders = total_orders + 1,
                avg_delay_ms = ((avg_delay_ms * (total_orders - 1)) + ?) / total_orders,
                avg_slippage = ((avg_slippage * (total_orders - 1)) + ?) / total_orders
        """, (trader_uid, today, delay_ms, slippage_pct, delay_ms, slippage_pct))
        conn.commit()


def get_trader_performance(trader_uid: str, days: int = 7) -> dict:
    """获取交易员最近N天的性能统计"""
    import time
    cutoff_date = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                AVG(avg_delay_ms) as avg_delay,
                AVG(avg_slippage) as avg_slippage,
                SUM(total_orders) as total_orders,
                MAX(date) as last_date,
                AVG(CASE WHEN date >= ? THEN pnl_7d ELSE NULL END) as recent_pnl
            FROM trader_performance
            WHERE trader_uid = ? AND date >= ?
        """, (cutoff_date, trader_uid, cutoff_date)).fetchone()
        
        if rows:
            return dict(rows)
        return {
            "avg_delay": 0,
            "avg_slippage": 0,
            "total_orders": 0,
            "last_date": None,
            "recent_pnl": 0,
        }


def should_pause_trader(trader_uid: str) -> tuple:
    """判断是否应该暂停交易员"""
    perf = get_trader_performance(trader_uid, days=7)
    
    if perf["avg_delay"] > 15000:
        return True, f"平均延迟过高: {perf['avg_delay']/1000:.1f}秒"
    
    if perf["avg_slippage"] > 0.01:
        return True, f"平均滑点过高: {perf['avg_slippage']*100:.2f}%"
    
    if perf["total_orders"] >= 10 and perf["recent_pnl"] < -500:
        return True, f"近7天亏损: ${perf['recent_pnl']:.2f}"
    
    return False, ""


def update_trader_pnl_stats():
    """更新所有交易员的7天/30天盈亏统计"""
    import time
    today = time.strftime("%Y-%m-%d")
    cutoff_7d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    cutoff_30d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    
    with get_conn() as conn:
        traders = conn.execute("SELECT DISTINCT trader_uid FROM copy_orders").fetchall()
        
        for row in traders:
            trader_uid = row["trader_uid"]
            
            pnl_7d = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM copy_orders
                WHERE trader_uid = ? AND action = 'close' AND status = 'filled'
                  AND date(timestamp / 1000, 'unixepoch', 'localtime') >= ?
            """, (trader_uid, cutoff_7d)).fetchone()["total_pnl"]
            
            pnl_30d = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM copy_orders
                WHERE trader_uid = ? AND action = 'close' AND status = 'filled'
                  AND date(timestamp / 1000, 'unixepoch', 'localtime') >= ?
            """, (trader_uid, cutoff_30d)).fetchone()["total_pnl"]
            
            conn.execute("""
                INSERT INTO trader_performance 
                    (trader_uid, date, pnl_7d, pnl_30d, total_orders)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(trader_uid, date) DO UPDATE SET
                    pnl_7d = ?, pnl_30d = ?
            """, (trader_uid, today, pnl_7d, pnl_30d, pnl_7d, pnl_30d))
        
        conn.commit()
    logger.info("更新了 %d 个交易员的盈亏统计", len(traders))


# ── 源交易事件与研究管线 ────────────────────────────────────────────────────────


def upsert_source_trader_events(events: list[dict], source_kind: str = "live") -> None:
    """
    插入或更新源交易员事件（用于研究管线和对账）。
    幂等操作：相同 (trader_uid, source_order_id) 的事件不会重复插入。
    
    参数:
        events: 事件列表，每个事件需包含:
            - trader_uid: 交易员 UID
            - source_order_id: 源订单 ID
            - symbol: 交易对
            - action: 动作 (open_long, close_long, open_short, close_short)
            - direction: 方向 (long, short)
            - qty: 数量
            - price: 价格
            - leverage: 杠杆
            - order_time: 订单时间戳（毫秒）
            - raw_payload: 原始数据（dict，会自动序列化为 JSON）
        source_kind: 数据源类型 ('live', 'history', 'reconcile')
    """
    if not events:
        return
    
    now_ms = int(time.time() * 1000)
    with _db_write_lock:
        with get_conn() as conn:
            for evt in events:
                raw = evt.get("raw_payload")
                raw_json = json.dumps(raw) if isinstance(raw, dict) else str(raw or "")
                
                conn.execute("""
                    INSERT INTO source_trader_events
                        (trader_uid, source_order_id, symbol, action, direction,
                         qty, price, leverage, order_time, source_kind, raw_payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trader_uid, source_order_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        action = excluded.action,
                        direction = excluded.direction,
                        qty = excluded.qty,
                        price = excluded.price,
                        leverage = excluded.leverage,
                        order_time = excluded.order_time,
                        source_kind = excluded.source_kind,
                        raw_payload = excluded.raw_payload
                """, (
                    evt["trader_uid"],
                    str(evt["source_order_id"]),
                    evt.get("symbol", ""),
                    evt.get("action", ""),
                    evt.get("direction", ""),
                    float(evt.get("qty", 0)),
                    float(evt.get("price", 0)),
                    int(evt.get("leverage", 1)),
                    int(evt.get("order_time", 0)),
                    source_kind,
                    raw_json,
                    now_ms,
                ))
            conn.commit()
    
    logger.debug("upsert %d 个源事件 (source_kind=%s)", len(events), source_kind)


def get_source_trader_events(
    trader_uid: str,
    symbol: str | None = None,
    since_ms: int = 0,
    limit: int = 1000
) -> list[dict]:
    """获取交易员的源事件列表"""
    sql = "SELECT * FROM source_trader_events WHERE trader_uid = ?"
    params: list[Any] = [trader_uid]
    
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    
    if since_ms > 0:
        sql += " AND order_time > ?"
        params.append(since_ms)
    
    sql += " ORDER BY order_time ASC, id ASC LIMIT ?"
    params.append(limit)
    
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    
    return [dict(r) for r in rows]


def rebuild_trader_position_cycles(trader_uid: str) -> int:
    """
    根据 source_trader_events 重建交易员的仓位周期。
    返回：新建或更新的周期数量。
    
    逻辑：
    1. 按时间顺序遍历所有事件
    2. open_long/open_short 开启新周期
    3. close_long/close_short 结束对应周期
    4. 反向开仓（long→short 或 short→long）标记为 reverse_transition
    """
    events = get_source_trader_events(trader_uid, limit=10000)
    if not events:
        return 0
    
    now_ms = int(time.time() * 1000)
    cycles_created = 0
    
    # key: (symbol, direction) -> 当前活跃周期的 open_event
    active_positions: dict[tuple[str, str], dict] = {}
    
    with _db_write_lock:
        with get_conn() as conn:
            # 清空旧周期（重建逻辑）
            conn.execute(
                "DELETE FROM trader_position_cycles WHERE trader_uid = ?",
                (trader_uid,)
            )
            
            for evt in events:
                symbol = evt["symbol"]
                action = evt["action"]
                direction = evt["direction"]
                order_time = evt["order_time"]
                qty = evt["qty"]
                price = evt["price"]
                leverage = evt["leverage"]
                event_id = evt["id"]
                
                key = (symbol, direction)
                opposite_key = (symbol, "short" if direction == "long" else "long")
                
                if action in ("open_long", "open_short"):
                    # 如果有反向持仓，先关闭它（reverse_transition）
                    if opposite_key in active_positions:
                        opp = active_positions.pop(opposite_key)
                        hold_sec = (order_time - opp["open_time"]) // 1000
                        pnl = (price - opp["open_price"]) * opp["qty"]
                        if opp["direction"] == "short":
                            pnl = -pnl
                        roi = (pnl / (opp["open_price"] * opp["qty"])) * opp["leverage"] * 100 if opp["qty"] > 0 else 0
                        
                        conn.execute("""
                            INSERT INTO trader_position_cycles
                                (trader_uid, symbol, direction, open_event_id, close_event_id,
                                 open_time, close_time, open_price, close_price, qty, leverage,
                                 hold_duration_sec, realized_pnl, roi_pct, close_reason, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            trader_uid, opp["symbol"], opp["direction"], opp["event_id"], event_id,
                            opp["open_time"], order_time, opp["open_price"], price, opp["qty"], opp["leverage"],
                            hold_sec, pnl, roi, "reverse_transition", now_ms
                        ))
                        cycles_created += 1
                    
                    # 开启新周期（允许覆盖）
                    active_positions[key] = {
                        "event_id": event_id,
                        "symbol": symbol,
                        "direction": direction,
                        "open_time": order_time,
                        "open_price": price,
                        "qty": qty,
                        "leverage": leverage,
                    }
                
                elif action in ("close_long", "close_short"):
                    if key in active_positions:
                        pos = active_positions.pop(key)
                        hold_sec = (order_time - pos["open_time"]) // 1000
                        pnl = (price - pos["open_price"]) * pos["qty"]
                        if direction == "short":
                            pnl = -pnl
                        roi = (pnl / (pos["open_price"] * pos["qty"])) * pos["leverage"] * 100 if pos["qty"] > 0 else 0
                        
                        conn.execute("""
                            INSERT INTO trader_position_cycles
                                (trader_uid, symbol, direction, open_event_id, close_event_id,
                                 open_time, close_time, open_price, close_price, qty, leverage,
                                 hold_duration_sec, realized_pnl, roi_pct, close_reason, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            trader_uid, pos["symbol"], pos["direction"], pos["event_id"], event_id,
                            pos["open_time"], order_time, pos["open_price"], price, pos["qty"], pos["leverage"],
                            hold_sec, pnl, roi, "normal_close", now_ms
                        ))
                        cycles_created += 1
            
            # 剩余未关闭的仓位标记为 still_open
            for pos in active_positions.values():
                conn.execute("""
                    INSERT INTO trader_position_cycles
                        (trader_uid, symbol, direction, open_event_id, close_event_id,
                         open_time, close_time, open_price, close_price, qty, leverage,
                         hold_duration_sec, realized_pnl, roi_pct, close_reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trader_uid, pos["symbol"], pos["direction"], pos["event_id"], None,
                    pos["open_time"], None, pos["open_price"], None, pos["qty"], pos["leverage"],
                    None, None, None, "still_open", now_ms
                ))
                cycles_created += 1
            
            conn.commit()
    
    logger.debug("重建 trader=%s 的仓位周期：%d 个", trader_uid[:12], cycles_created)
    return cycles_created


def get_trader_position_cycles(
    trader_uid: str,
    symbol: str | None = None,
    limit: int = 100
) -> list[dict]:
    """获取交易员的仓位周期列表"""
    sql = "SELECT * FROM trader_position_cycles WHERE trader_uid = ?"
    params: list[Any] = [trader_uid]
    
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    
    sql += " ORDER BY open_time DESC LIMIT ?"
    params.append(limit)
    
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    
    return [dict(r) for r in rows]


def _refresh_trader_execution_daily_single(trader_uid: str, day: str | None = None) -> None:
    """
    刷新指定交易员在指定日期的执行统计（从 copy_orders 表聚合）。
    按 platform 分别存储。
    
    参数:
        trader_uid: 交易员 UID
        day: 日期字符串 YYYY-MM-DD，默认为今天
    """
    import datetime
    if not day:
        day = datetime.date.today().isoformat()
    
    day_start_ms = int(time.mktime(time.strptime(day, "%Y-%m-%d"))) * 1000
    day_end_ms = day_start_ms + 86400_000
    now_ms = int(time.time() * 1000)
    
    with _db_write_lock:
        with get_conn() as conn:
            # 按平台分组聚合
            platforms = conn.execute("""
                SELECT DISTINCT COALESCE(platform, 'bitget') as platform
                FROM copy_orders
                WHERE trader_uid = ? AND timestamp >= ? AND timestamp < ?
            """, (trader_uid, day_start_ms, day_end_ms)).fetchall()
            
            for platform_row in platforms:
                platform = platform_row["platform"]
                
                stats = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled,
                        SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
                        SUM(CASE WHEN action = 'open' THEN 1 ELSE 0 END) as open_total,
                        SUM(CASE WHEN action = 'open' AND status = 'filled' THEN 1 ELSE 0 END) as open_filled,
                        SUM(CASE WHEN action = 'close' THEN 1 ELSE 0 END) as close_total,
                        SUM(CASE WHEN action = 'close' AND status = 'filled' THEN 1 ELSE 0 END) as close_filled,
                        SUM(CASE WHEN status = 'skipped' AND notes LIKE '%invalid symbol%' THEN 1 ELSE 0 END) as invalid_symbol,
                        COALESCE(SUM(CASE WHEN status = 'filled' THEN pnl ELSE 0 END), 0) as total_pnl
                    FROM copy_orders
                    WHERE trader_uid = ? AND COALESCE(platform, 'bitget') = ? 
                      AND timestamp >= ? AND timestamp < ?
                """, (trader_uid, platform, day_start_ms, day_end_ms)).fetchone()
                
                total = stats["total"] or 0
                filled = stats["filled"] or 0
                skipped = stats["skipped"] or 0
                total_pnl = stats["total_pnl"] or 0.0
                
                open_total = stats["open_total"] or 0
                open_filled = stats["open_filled"] or 0
                close_total = stats["close_total"] or 0
                close_filled = stats["close_filled"] or 0
                invalid_symbol = stats["invalid_symbol"] or 0
                
                open_fill_rate = (open_filled / open_total) if open_total > 0 else 0.0
                close_completion_rate = (close_filled / close_total) if close_total > 0 else 0.0
                invalid_symbol_rate = (invalid_symbol / open_total) if open_total > 0 else 0.0
                
                conn.execute("""
                    INSERT INTO trader_execution_daily
                        (trader_uid, day, platform, total_orders, filled_orders, skipped_orders,
                         avg_delay_ms, avg_slippage_pct, total_pnl, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    ON CONFLICT(trader_uid, day, platform) DO UPDATE SET
                        total_orders = excluded.total_orders,
                        filled_orders = excluded.filled_orders,
                        skipped_orders = excluded.skipped_orders,
                        total_pnl = excluded.total_pnl,
                        updated_at = excluded.updated_at
                """, (trader_uid, day, platform, total, filled, skipped, total_pnl, now_ms))
                
                # 存储额外的比率字段到元数据
                for key, value in [
                    ("open_fill_rate", open_fill_rate),
                    ("close_completion_rate", close_completion_rate),
                    ("invalid_symbol_rate", invalid_symbol_rate),
                ]:
                    conn.execute("""
                        INSERT OR REPLACE INTO trader_metadata (trader_uid, key, value)
                        VALUES (?, ?, ?)
                    """, (trader_uid, f"{day}_{key}", str(value)))
            
            conn.commit()
    
    logger.debug("刷新 trader=%s day=%s 执行统计", trader_uid[:12], day)


def get_trader_execution_daily(
    trader_uid: str,
    days: int = 365
) -> list[dict]:
    """获取交易员最近 N 天的执行统计（默认1年）"""
    import datetime
    cutoff_day = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    
    with get_conn() as conn:
        # 确保 trader_metadata 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trader_metadata (
                trader_uid TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (trader_uid, key)
            )
        """)
        
        rows = conn.execute("""
            SELECT * FROM trader_execution_daily
            WHERE trader_uid = ? AND day >= ?
            ORDER BY day DESC
        """, (trader_uid, cutoff_day)).fetchall()
        
        result = []
        for row in rows:
            item = dict(row)
            day = item["day"]
            
            # 加载额外字段
            for key in ["open_fill_rate", "close_completion_rate", "invalid_symbol_rate"]:
                meta = conn.execute("""
                    SELECT value FROM trader_metadata
                    WHERE trader_uid = ? AND key = ?
                """, (trader_uid, f"{day}_{key}")).fetchone()
                item[key] = float(meta["value"]) if meta else 0.0
            
            result.append(item)
        
        return result


def get_source_position_summaries(trader_uid: str) -> list[dict]:
    """
    从 source_trader_events 聚合当前源端活跃仓位。
    返回：[{symbol, direction, remaining_qty, last_event_time, ...}]
    用于对账逻辑。
    """
    events = get_source_trader_events(trader_uid, limit=5000)
    if not events:
        return []
    
    # key: (symbol, direction) -> position state
    positions: dict[tuple[str, str], dict[str, Any]] = {}
    
    for evt in events:
        symbol = str(evt.get("symbol") or "")
        action = str(evt.get("action") or "")
        direction = str(evt.get("direction") or "")
        qty = _safe_float(evt.get("qty"), 0.0)
        price = _safe_float(evt.get("price"), 0.0)
        leverage = max(1, _safe_int(evt.get("leverage"), 1))
        order_time = _safe_int(evt.get("order_time"), 0)
        source_order_id = str(evt.get("source_order_id") or "")

        key = (symbol, direction)

        if action in ("open_long", "open_short"):
            state = positions.setdefault(
                key,
                {
                    "trader_uid": trader_uid,
                    "symbol": symbol,
                    "direction": direction,
                    "remaining_qty": 0.0,
                    "remaining_margin": 0.0,
                    "avg_entry_price": 0.0,
                    "price": 0.0,
                    "last_open_price": 0.0,
                    "last_open_time": 0,
                    "last_event_time": 0,
                    "last_close_time": 0,
                    "last_source_order_id": "",
                    "leverage": leverage,
                },
            )
            before_qty = state["remaining_qty"]
            if price > 0 and qty > 0:
                if before_qty > 0:
                    state["avg_entry_price"] = ((state["avg_entry_price"] * before_qty) + (price * qty)) / (before_qty + qty)
                else:
                    state["avg_entry_price"] = price
            state["remaining_qty"] = before_qty + qty
            state["remaining_margin"] += _estimate_margin_from_position(qty, price, leverage)
            state["last_event_time"] = max(state["last_event_time"], order_time)
            state["last_open_time"] = max(state["last_open_time"], order_time)
            if price > 0:
                state["price"] = price
                state["last_open_price"] = price
            if source_order_id:
                state["last_source_order_id"] = source_order_id
            state["leverage"] = leverage or state.get("leverage") or 1

        elif action in ("close_long", "close_short") and key in positions:
            state = positions[key]
            before_qty = max(state["remaining_qty"], 0.0)
            close_qty = min(qty, before_qty) if before_qty > 0 else 0.0
            state["remaining_qty"] = max(0.0, before_qty - close_qty)
            if before_qty > 0:
                state["remaining_margin"] = max(0.0, state["remaining_margin"] * (1.0 - (close_qty / before_qty)))
            state["last_event_time"] = max(state["last_event_time"], order_time)
            state["last_close_time"] = order_time
            if price > 0:
                state["price"] = price
            if source_order_id:
                state["last_source_order_id"] = source_order_id
            if state["remaining_qty"] <= 1e-6:
                # 仓位已平，标记但保留（对账需要 last_close_time）
                state["remaining_qty"] = 0.0
                state["remaining_margin"] = 0.0

    return [p for p in positions.values()]


def refresh_trader_execution_daily_batch(trader_uids: list[str], day: str | None = None) -> int:
    """
    批量刷新多个交易员的执行统计。
    如果 day 为 None，会从 copy_orders 中查找该交易员的所有日期并分别刷新。
    返回：成功刷新的交易员数量。
    """
    import datetime
    count = 0
    
    if day is None:
        # 自动查找所有需要刷新的日期
        with get_conn() as conn:
            for uid in trader_uids:
                try:
                    days = conn.execute("""
                        SELECT DISTINCT date(timestamp / 1000, 'unixepoch', 'localtime') as day
                        FROM copy_orders
                        WHERE trader_uid = ?
                    """, (uid,)).fetchall()
                    
                    for day_row in days:
                        _refresh_trader_execution_daily_single(uid, day_row["day"])
                    
                    if days:
                        count += 1
                except Exception as e:
                    logger.error("刷新 trader=%s 执行统计失败: %s", uid[:12], e)
    else:
        for uid in trader_uids:
            try:
                _refresh_trader_execution_daily_single(uid, day)
                count += 1
            except Exception as e:
                logger.error("刷新 trader=%s 执行统计失败: %s", uid[:12], e)
    
    return count


def refresh_trader_execution_daily(trader_uids_or_uid, day: str | None = None):
    """
    刷新交易员执行统计（兼容单个和批量）。
    - 如果传入 list，返回成功计数
    - 如果传入 str，无返回值
    """
    if isinstance(trader_uids_or_uid, list):
        return refresh_trader_execution_daily_batch(trader_uids_or_uid, day)
    else:
        return _refresh_trader_execution_daily_single(trader_uids_or_uid, day)


def refresh_trader_history_analytics(trader_uids: list[str], lookback_days: int = 45) -> int:
    """Refresh cached trader history analytics into trader_metadata."""
    count = 0
    metric_keys = (
        "analysis_updated_at",
        "history_sample_size",
        "source_close_count",
        "cycle_sample_size",
        "copy_open_sample_size",
        "copy_close_sample_size",
        "median_source_margin",
        "avg_source_margin",
        "median_source_leverage",
        "avg_hold_sec",
        "median_hold_sec",
        "reverse_rate",
        "clip_rate",
        "min_adjust_rate",
        "small_order_skip_rate",
        "fallback_market_rate",
        "reconcile_close_rate",
        "stability_score",
        "execution_score",
        "close_reliability_score",
        "risk_score",
        "total_score",
        "behavior_score",
        "preferred_platform",
    )

    with _db_write_lock:
        with get_conn() as conn:
            for trader_uid in trader_uids:
                try:
                    snapshot = _collect_trader_analysis(conn, trader_uid, lookback_days=lookback_days)
                    has_samples = any(
                        _safe_float(snapshot.get(key), 0.0) > 0
                        for key in ("history_sample_size", "cycle_sample_size", "copy_open_sample_size", "copy_close_sample_size")
                    )
                    if not has_samples:
                        continue
                    for key in metric_keys:
                        _upsert_trader_metadata_value(conn, trader_uid, key, snapshot.get(key))
                    count += 1
                except Exception as exc:
                    logger.error("刷新 trader=%s 历史分析失败: %s", trader_uid[:12], exc)
            conn.commit()

    return count


def _refresh_trader_research_scores_legacy(trader_uids: list[str]) -> int:
    """
    批量刷新交易员的研究评分（基于周期和执行数据）。
    返回：成功刷新的交易员数量。
    """
    now_ms = int(time.time() * 1000)
    count = 0
    
    with _db_write_lock:
        with get_conn() as conn:
            for trader_uid in trader_uids:
                try:
                    # 聚合周期数据
                    snapshot = _collect_trader_analysis(conn, trader_uid, lookback_days=45)
                    has_samples = any(
                        _safe_float(snapshot.get(key), 0.0) > 0
                        for key in ("history_sample_size", "cycle_sample_size", "copy_open_sample_size", "copy_close_sample_size")
                    )
                    if not has_samples:
                        continue
                    
                    stability_score = _safe_float(snapshot.get("stability_score"), 0.0)
                    execution_score = _safe_float(snapshot.get("execution_score"), 0.0)
                    risk_score = _safe_float(snapshot.get("risk_score"), 0.0)
                    total_score = _safe_float(snapshot.get("total_score"), 0.0)
                    
                    # 聚合执行数据（最近7天）
                    close_reliability_score = _safe_float(snapshot.get("close_reliability_score"), 0.0)
                    if False:
                        exec_stats = conn.execute("""
                        SELECT
                            platform,
                            SUM(filled_orders) as filled,
                            SUM(total_orders) as total
                        FROM trader_execution_daily
                        WHERE trader_uid = ? AND day >= ?
                        GROUP BY platform
                        ORDER BY filled DESC
                    """, (trader_uid, cutoff_day)).fetchall()
                    
                    preferred_platform = "bitget"
                    execution_score = 0.0
                    close_reliability_score = 0.0
                    
                    if exec_stats:
                        top = exec_stats[0]
                        preferred_platform = top["platform"] or "bitget"
                        filled = top["filled"] or 0
                        total = top["total"] or 1
                        execution_score = (filled / total * 100) if total > 0 else 0.0
                        
                        # 平仓完成率（假设 close 动作都成功了）
                        close_count = conn.execute("""
                            SELECT COUNT(*) as cnt FROM copy_orders
                            WHERE trader_uid = ? AND action = 'close' AND status = 'filled'
                              AND timestamp >= ?
                        """, (trader_uid, now_ms - 7*86400_000)).fetchone()["cnt"]
                        
                        close_reliability_score = min(100.0, (close_count / max(1, len(closed_cycles))) * 100)
                    
                    risk_score = 100.0 - min(100.0, abs(stability_score - 50.0))
                    total_score = (stability_score * 0.4 + execution_score * 0.3 + 
                                   close_reliability_score * 0.2 + risk_score * 0.1)
                    
                    conn.execute("""
                        INSERT INTO trader_research_scores
                            (trader_uid, stability_score, execution_score, risk_score,
                             total_score, rank, updated_at)
                        VALUES (?, ?, ?, ?, ?, 0, ?)
                        ON CONFLICT(trader_uid) DO UPDATE SET
                            stability_score = excluded.stability_score,
                            execution_score = excluded.execution_score,
                            risk_score = excluded.risk_score,
                            total_score = excluded.total_score,
                            updated_at = excluded.updated_at
                    """, (trader_uid, stability_score, execution_score, risk_score, total_score, now_ms))
                    
                    # 额外存储 preferred_platform（扩展字段）
                    conn.execute("""
                        UPDATE trader_research_scores
                        SET rank = (SELECT COUNT(*) FROM trader_research_scores t2 WHERE t2.total_score > trader_research_scores.total_score) + 1
                        WHERE trader_uid = ?
                    """, (trader_uid,))
                    
                    # 将 preferred_platform 存入额外的元数据表（为简化，直接用 JSON 存）
                    conn.execute("""
                        INSERT OR REPLACE INTO trader_metadata (trader_uid, key, value)
                        VALUES (?, 'preferred_platform', ?)
                    """, (trader_uid, preferred_platform))
                    conn.execute("""
                        INSERT OR REPLACE INTO trader_metadata (trader_uid, key, value)
                        VALUES (?, 'close_reliability_score', ?)
                    """, (trader_uid, str(close_reliability_score)))
                    
                    count += 1
                except Exception as e:
                    logger.error("刷新 trader=%s 研究评分失败: %s", trader_uid[:12], e)
            
            conn.commit()
    
    return count


def _get_trader_research_scores_legacy(trader_uid: str) -> list[dict]:
    """获取交易员的研究评分（返回列表格式以兼容测试）"""
    with get_conn() as conn:
        # 确保 trader_metadata 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trader_metadata (
                trader_uid TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (trader_uid, key)
            )
        """)
        
        row = conn.execute("""
            SELECT * FROM trader_research_scores WHERE trader_uid = ?
        """, (trader_uid,)).fetchone()
        
        if not row:
            return []
        
        result = dict(row)
        
        # 获取 preferred_platform
        preferred_meta = conn.execute("""
            SELECT value FROM trader_metadata WHERE trader_uid = ? AND key = 'preferred_platform'
        """, (trader_uid,)).fetchone()
        reliability_meta = conn.execute("""
            SELECT value FROM trader_metadata WHERE trader_uid = ? AND key = 'close_reliability_score'
        """, (trader_uid,)).fetchone()
        
        result["preferred_platform"] = preferred_meta["value"] if preferred_meta else "bitget"
        result["close_reliability_score"] = float(reliability_meta["value"]) if reliability_meta and reliability_meta["value"] not in (None, "") else 0.0
        
        return [result]

# 兼容测试用例的别名
refresh_trader_execution_daily = lambda uids, day=None: refresh_trader_execution_daily_batch(uids, day) if isinstance(uids, list) else refresh_trader_execution_daily(uids, day)


def refresh_trader_execution_daily(trader_uids_or_uid, day: str | None = None):
    """Refresh execution stats for either one trader or a trader list."""
    if isinstance(trader_uids_or_uid, list):
        return refresh_trader_execution_daily_batch(trader_uids_or_uid, day)
    return _refresh_trader_execution_daily_single(trader_uids_or_uid, day)


def refresh_trader_research_scores(trader_uids: list[str]) -> int:
    """Refresh trader research scores from live history and execution analytics."""
    now_ms = int(time.time() * 1000)
    count = 0

    with _db_write_lock:
        with get_conn() as conn:
            for trader_uid in trader_uids:
                try:
                    snapshot = _collect_trader_analysis(conn, trader_uid, lookback_days=45)
                    has_samples = any(
                        _safe_float(snapshot.get(key), 0.0) > 0
                        for key in ("history_sample_size", "cycle_sample_size", "copy_open_sample_size", "copy_close_sample_size")
                    )
                    if not has_samples:
                        continue

                    stability_score = _safe_float(snapshot.get("stability_score"), 0.0)
                    execution_score = _safe_float(snapshot.get("execution_score"), 0.0)
                    risk_score = _safe_float(snapshot.get("risk_score"), 0.0)
                    total_score = _safe_float(snapshot.get("total_score"), 0.0)

                    conn.execute(
                        """
                        INSERT INTO trader_research_scores
                            (trader_uid, stability_score, execution_score, risk_score,
                             total_score, rank, updated_at)
                        VALUES (?, ?, ?, ?, ?, 0, ?)
                        ON CONFLICT(trader_uid) DO UPDATE SET
                            stability_score = excluded.stability_score,
                            execution_score = excluded.execution_score,
                            risk_score = excluded.risk_score,
                            total_score = excluded.total_score,
                            updated_at = excluded.updated_at
                        """,
                        (trader_uid, stability_score, execution_score, risk_score, total_score, now_ms),
                    )
                    conn.execute(
                        """
                        UPDATE trader_research_scores
                        SET rank = (
                            SELECT COUNT(*) FROM trader_research_scores t2
                            WHERE t2.total_score > trader_research_scores.total_score
                        ) + 1
                        WHERE trader_uid = ?
                        """,
                        (trader_uid,),
                    )
                    for key, value in snapshot.items():
                        if key in {"trader_uid", "lookback_days"}:
                            continue
                        _upsert_trader_metadata_value(conn, trader_uid, key, value)
                    count += 1
                except Exception as exc:
                    logger.error("刷新 trader=%s 研究评分失败: %s", trader_uid[:12], exc)
            conn.commit()

    return count


def get_trader_research_scores(trader_uid: str) -> list[dict]:
    """Return stored trader research score plus cached analytics metadata."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM trader_research_scores WHERE trader_uid = ?
            """,
            (trader_uid,),
        ).fetchone()
        if not row:
            return []

        result = dict(row)
        metadata_rows = conn.execute(
            """
            SELECT key, value FROM trader_metadata
            WHERE trader_uid = ? AND key IN (
                'preferred_platform',
                'close_reliability_score',
                'median_source_margin',
                'avg_source_margin',
                'median_source_leverage',
                'avg_hold_sec',
                'median_hold_sec',
                'reverse_rate',
                'clip_rate',
                'min_adjust_rate',
                'small_order_skip_rate',
                'fallback_market_rate',
                'reconcile_close_rate',
                'behavior_score',
                'history_sample_size',
                'cycle_sample_size',
                'copy_open_sample_size',
                'copy_close_sample_size',
                'analysis_updated_at'
            )
            """,
            (trader_uid,),
        ).fetchall()
        metadata = {str(row["key"]): row["value"] for row in metadata_rows}

        result["preferred_platform"] = metadata.get("preferred_platform", "bitget")
        for key in (
            "close_reliability_score",
            "median_source_margin",
            "avg_source_margin",
            "median_source_leverage",
            "avg_hold_sec",
            "median_hold_sec",
            "reverse_rate",
            "clip_rate",
            "min_adjust_rate",
            "small_order_skip_rate",
            "fallback_market_rate",
            "reconcile_close_rate",
            "behavior_score",
        ):
            result[key] = _safe_float(metadata.get(key), 0.0)
        for key in (
            "history_sample_size",
            "cycle_sample_size",
            "copy_open_sample_size",
            "copy_close_sample_size",
            "analysis_updated_at",
        ):
            result[key] = _safe_int(metadata.get(key), 0)
        return [result]


def get_trader_analysis_snapshot(trader_uid: str, lookback_days: int = 45) -> dict[str, Any]:
    """Compute a fresh trader analysis snapshot from history and execution records."""
    with get_conn() as conn:
        return _collect_trader_analysis(conn, trader_uid, lookback_days=lookback_days)
