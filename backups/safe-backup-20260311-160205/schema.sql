-- account_daily_equity
CREATE TABLE account_daily_equity (
    day             TEXT PRIMARY KEY, -- YYYY-MM-DD（本地时区）
    start_equity    REAL NOT NULL,
    start_ts        INTEGER NOT NULL,
    last_equity     REAL NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- copy_orders
CREATE TABLE copy_orders (
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
    action          TEXT,
    status          TEXT,
    pnl             REAL,
    notes           TEXT
, exec_qty REAL DEFAULT 0, platform TEXT DEFAULT 'bitget');

-- copy_position_states
CREATE TABLE copy_position_states (
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

-- copy_profile_settings
CREATE TABLE copy_profile_settings (
    profile         TEXT PRIMARY KEY,
    settings_json   TEXT NOT NULL DEFAULT '{}',
    updated_at      INTEGER NOT NULL DEFAULT 0
);

-- copy_settings
CREATE TABLE copy_settings (
    id              INTEGER PRIMARY KEY,
    api_key         TEXT,
    api_secret      TEXT,
    api_passphrase  TEXT,
    total_capital   REAL DEFAULT 0,
    max_margin_pct  REAL DEFAULT 0.20,
    price_tolerance REAL DEFAULT 0.0002,
    enabled_traders TEXT DEFAULT '[]',
    engine_enabled  INTEGER DEFAULT 0
, sl_pct REAL DEFAULT 0.15, tp_pct REAL DEFAULT 0.30, binance_traders TEXT DEFAULT '[]', follow_ratio_pct REAL DEFAULT 0.003, binance_api_key TEXT DEFAULT '', binance_api_secret TEXT DEFAULT '', binance_total_capital REAL DEFAULT 0, binance_follow_ratio_pct REAL DEFAULT 0.003, binance_max_margin_pct REAL DEFAULT 0.20, binance_price_tolerance REAL DEFAULT 0.0002, daily_loss_limit_pct REAL DEFAULT 0.03, total_drawdown_limit_pct REAL DEFAULT 0.1, take_profit_enabled INTEGER DEFAULT 1, stop_loss_pct REAL DEFAULT 0.06, tp1_roi_pct REAL DEFAULT 0.08, tp1_close_pct REAL DEFAULT 0.3, tp2_roi_pct REAL DEFAULT 0.15, tp2_close_pct REAL DEFAULT 0.3, tp3_roi_pct REAL DEFAULT 0.25, tp3_close_pct REAL DEFAULT 0.4, breakeven_buffer_pct REAL DEFAULT 0.005, trail_callback_pct REAL DEFAULT 0.06, entry_order_mode TEXT DEFAULT 'maker_limit', entry_maker_levels INTEGER DEFAULT 1, entry_limit_timeout_sec INTEGER DEFAULT 10, entry_limit_fallback_to_market INTEGER DEFAULT 1);

-- platform_daily_equity
CREATE TABLE platform_daily_equity (
    platform        TEXT NOT NULL,
    day             TEXT NOT NULL,
    start_equity    REAL NOT NULL,
    start_ts        INTEGER NOT NULL,
    last_equity     REAL NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (platform, day)
);

-- snapshots
CREATE TABLE snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_uid      TEXT,
    timestamp       INTEGER,
    tracking_no     TEXT,
    symbol          TEXT,
    hold_side       TEXT,
    leverage        INTEGER,
    open_price      REAL,
    open_time       INTEGER,
    open_amount     REAL,
    tp_price        REAL,
    sl_price        REAL, margin_mode TEXT, position_size REAL, unrealized_pnl REAL, return_rate REAL, follow_count INTEGER,
    UNIQUE(trader_uid, tracking_no)
);

-- traders
CREATE TABLE traders (
    trader_uid      TEXT PRIMARY KEY,
    nickname        TEXT,
    first_seen      INTEGER,
    roi             REAL,
    win_rate        REAL,
    follower_count  INTEGER,
    total_trades    INTEGER,
    copy_trade_days INTEGER,
    last_updated    INTEGER
, max_drawdown REAL, total_profit REAL, aum REAL, avatar TEXT, profit_7d REAL, profit_30d REAL);

-- trades
CREATE TABLE trades (
    trade_id        TEXT PRIMARY KEY,
    trader_uid      TEXT,
    symbol          TEXT,
    direction       TEXT,
    leverage        INTEGER,
    open_price      REAL,
    open_time       INTEGER,
    close_price     REAL,
    close_time      INTEGER,
    hold_duration   INTEGER,
    pnl_pct         REAL,
    margin_amount   REAL,
    is_win          INTEGER, net_profit REAL, margin_mode TEXT, position_size REAL, gross_profit REAL, open_fee REAL, close_fee REAL, funding_fee REAL, follow_count INTEGER,
    FOREIGN KEY (trader_uid) REFERENCES traders(trader_uid)
);
