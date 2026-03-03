"""
SQLite 数据库：建表 + CRUD 封装
所有操作通过 get_conn() 获取连接，支持多线程（check_same_thread=False）。
"""
from __future__ import annotations
import logging
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
    price_tolerance REAL DEFAULT 0.0002,
    sl_pct          REAL DEFAULT 0.15,
    tp_pct          REAL DEFAULT 0.30,
    enabled_traders TEXT DEFAULT '[]',
    engine_enabled  INTEGER DEFAULT 0
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
    exec_qty        REAL DEFAULT 0
);



CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader_uid);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_snapshots_trader ON snapshots(trader_uid);
CREATE INDEX IF NOT EXISTS idx_copy_orders_time ON copy_orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_copy_orders_trader ON copy_orders(trader_uid);
CREATE INDEX IF NOT EXISTS idx_copy_orders_tracking ON copy_orders(tracking_no);
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
    # 迁移：为旧数据库添加新字段（幂等）
    for col, dtype, default in [
        ("follow_ratio_pct", "REAL", "0.003"),
        ("sl_pct", "REAL", "0.15"),
        ("tp_pct", "REAL", "0.30"),
        ("binance_traders", "TEXT", "'[]'"),
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
    return dict(row) if row else {}


# copy_settings 允许更新的列名白名单（防止 SQL 注入）
_COPY_SETTINGS_COLS = frozenset({
    "api_key", "api_secret", "api_passphrase",
    "total_capital", "follow_ratio_pct", "max_margin_pct", "price_tolerance",
    "sl_pct", "tp_pct",
    "enabled_traders", "binance_traders", "engine_enabled",
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
    update_copy_settings(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )


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


# ── copy_orders CRUD ───────────────────────────────────────────────────────────

def _migrate_copy_orders(conn) -> None:
    try:
        conn.execute("ALTER TABLE copy_orders ADD COLUMN exec_qty REAL DEFAULT 0")
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
                     deviation_pct, action, status, pnl, notes, exec_qty)
                VALUES
                    (:timestamp, :trader_uid, :tracking_no, :my_order_id, :symbol,
                     :direction, :leverage, :margin_usdt, :source_price, :exec_price,
                     :deviation_pct, :action, :status, :pnl, :notes, :exec_qty)
                """,
                {**order, "exec_qty": order.get("exec_qty", 0.0)},
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


def get_copy_orders(limit: int = 50, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM copy_orders
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        ).fetchall()
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


def get_open_copy_orders(trader_uid: str, symbol: str, direction: str) -> list[dict]:
    """获取指定交易员、币种、方向下所有已成单的开仓记录（用于币安合并平仓）"""
    with get_conn() as conn:
        rows = conn.execute('''
            SELECT * FROM copy_orders 
            WHERE trader_uid = ? AND symbol = ? AND direction = ? 
              AND action = 'open' AND status = 'filled'
        ''', (trader_uid, symbol, direction)).fetchall()
    return [dict(r) for r in rows]


def has_tracking_no(trader_uid: str, tracking_no: str) -> bool:
    """检查是否存在使用该 tracking_no 的任何记录（用于防重复执行币安信号）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM copy_orders WHERE trader_uid = ? AND tracking_no = ? LIMIT 1",
            (trader_uid, tracking_no)
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
