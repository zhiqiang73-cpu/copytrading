# PERFORMANCE_MONITORING

## Overview

v2.3.0 introduces a lightweight runtime monitoring pipeline based on WebSocket + `psutil`:

- Backend samples host and process metrics.
- Metrics are included in `status_update` events every ~2 seconds.
- Frontend shows a compact "系统性能" panel without extra polling endpoints.

## Backend Design

### 1) Metric collection (`web.py`)

`_collect_performance_stats()` now returns:

- `cpu_percent`
- `memory_percent`
- `memory_used_mb`
- `memory_total_mb`
- `process_memory_mb`
- `sample_interval_sec`
- `available` (false when `psutil` is unavailable)

These metrics are attached to `_get_current_state()` as `performance`.

### 2) Broadcast strategy (`web.py`)

`_ws_broadcast_thread()` keeps 2 behaviors:

- **Periodic state push**: emits `status_update` roughly every 2s.
- **Realtime order push**: drains an in-memory queue and emits `order_created` immediately.

### 3) Order event queue (`web.py` + `copy_engine.py`)

- `copy_engine.set_order_created_callback(...)` registers a callback from `web.py`.
- All `db.insert_copy_order(...)` paths in `copy_engine.py` were unified through `_insert_copy_order_and_notify(...)`.
- Callback payload is queued and broadcast as `order_created`.

## Frontend Design

### Performance Panel (`templates/my_positions.html`)

A new "系统性能" card shows:

- CPU usage
- Memory usage ratio and MB
- Process RSS
- Sample interval

The panel listens to `ws_state_update` and updates in near realtime.

### Realtime Orders (`templates/my_positions.html`)

- Page listens to `ws_order_created`.
- New orders are prepended to the table top.
- A dedupe key prevents duplicates on reconnect.
- Row count is capped at 20 to keep rendering stable.

## Config Cleanup

Hardcoded Binance scraper constants moved to `config.py`:

- `BINANCE_COPYTRADE_BASE`
- `BINANCE_COPYTRADE_USER_AGENT`

`binance_scraper.py` now consumes these config values.

## Dependency

`requirements.txt` adds:

- `psutil`

## Verification Checklist

- Start app and open `my_positions`.
- Confirm performance panel updates without manual refresh.
- Trigger a new copy order and verify it appears instantly in execution records.
- Verify no regressions in regular `status_update` UI behavior.
