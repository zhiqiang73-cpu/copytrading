from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database as db


def _normalize_profile(profile: str | None) -> str:
    return "sim" if str(profile or "").strip().lower() == "sim" else "live"


def _profile_platform_key(profile: str | None, platform: str) -> str:
    profile_key = _normalize_profile(profile)
    platform_key = str(platform or "").strip().lower()
    if profile_key == "sim":
        return platform_key
    return f"{profile_key}_{platform_key}"


def _profile_platform_keys(profile: str | None) -> list[str]:
    return [
        _profile_platform_key(profile, "bitget"),
        _profile_platform_key(profile, "binance"),
    ]


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


def _short_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_ts_ms(ts_ms: Any) -> str:
    ts = _safe_int(ts_ms, 0)
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_pct(value: Any) -> str:
    return f"{_safe_float(value, 0.0) * 100:.1f}%"


def _format_num(value: Any, digits: int = 4) -> str:
    return f"{_safe_float(value, 0.0):.{digits}f}"


def _load_jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return _load_jsonish(parsed, default)
            return parsed
        except Exception:
            return default
    return default


def _http_json(base_url: str, path: str, timeout: float) -> tuple[dict[str, Any] | None, str]:
    url = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return _load_jsonish(raw, {}), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} for {url}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _candidate_log_files() -> list[Path]:
    candidates = [
        ROOT / "bitgetfollow.log",
        ROOT / "logs" / "bitgetfollow.log",
        ROOT / "_manual_start.err",
        ROOT / "logs" / "web.log",
    ]
    existing = [path for path in candidates if path.exists() and path.is_file()]
    return sorted(existing, key=lambda item: item.stat().st_mtime, reverse=True)


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = text.splitlines()
    if limit <= 0:
        return lines
    return lines[-limit:]


def _collect_log_summary(
    paths: list[Path],
    tail_lines: int,
    trader_ids: set[str],
    symbols: set[str],
) -> dict[str, Any]:
    severity_re = re.compile(r"^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|ERROR|CRITICAL)\s+(\S+)\s+(.*)$")
    warning_lines: list[dict[str, str]] = []
    lifecycle_lines: list[dict[str, str]] = []
    trader_lines: list[dict[str, str]] = []
    keywords = (
        "open",
        "close",
        "sync-open",
        "reconcile",
        "dynamic",
        "poll",
        "warmup",
        "history",
        "timeout",
        "skip",
        "failed",
        "warning",
        "error",
    )
    trader_markers = {pid[:12] for pid in trader_ids if pid}
    symbol_markers = {symbol.upper() for symbol in symbols if symbol}

    for path in paths:
        for line in _tail_lines(path, tail_lines):
            text = line.strip()
            if not text:
                continue
            match = severity_re.match(text)
            severity = match.group(1) if match else ""
            if severity in {"WARNING", "ERROR", "CRITICAL"}:
                warning_lines.append({"file": path.name, "line": text})
            lowered = text.lower()
            if any(keyword in lowered for keyword in keywords):
                lifecycle_lines.append({"file": path.name, "line": text})
            if any(marker and marker in text for marker in trader_markers) or any(marker in text.upper() for marker in symbol_markers):
                trader_lines.append({"file": path.name, "line": text})

    return {
        "files": [str(path) for path in paths],
        "recent_warnings": warning_lines[-30:],
        "recent_lifecycle": lifecycle_lines[-40:],
        "recent_trader_hits": trader_lines[-40:],
    }


def _get_recent_copy_orders(
    platforms: list[str],
    limit: int = 100,
    trader_uid: str | None = None,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    platform_list = [str(platform).strip().lower() for platform in platforms if str(platform).strip()]
    if platform_list:
        placeholders = ", ".join(["?"] * len(platform_list))
        clauses.append(f"lower(platform) IN ({placeholders})")
        params.extend(platform_list)
    if trader_uid:
        clauses.append("trader_uid = ?")
        params.append(str(trader_uid).strip())
    sql = "SELECT * FROM copy_orders"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _get_recent_source_events(trader_uid: str, limit: int = 12) -> list[dict[str, Any]]:
    sql = """
        SELECT *
        FROM source_trader_events
        WHERE trader_uid = ?
        ORDER BY order_time DESC, id DESC
        LIMIT ?
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (str(trader_uid).strip(), max(1, int(limit)))).fetchall()
    return [dict(row) for row in rows]


def _get_enabled_binance_traders(profile: str) -> dict[str, dict[str, Any]]:
    settings = db.get_copy_settings_profile(profile)
    raw = _load_jsonish(settings.get("binance_traders") or {}, {})
    if not isinstance(raw, dict):
        return {}
    enabled: dict[str, dict[str, Any]] = {}
    for trader_uid, info in raw.items():
        if not isinstance(info, dict):
            continue
        if info.get("copy_enabled") is True:
            enabled[str(trader_uid)] = dict(info)
    return enabled


def _group_local_positions(profile: str) -> dict[str, list[dict[str, Any]]]:
    platform_keys = set(_profile_platform_keys(profile))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in db.get_active_copy_position_summaries():
        item = dict(row)
        if str(item.get("platform") or "").strip().lower() not in platform_keys:
            continue
        grouped[str(item.get("trader_uid") or "")].append(item)
    return dict(grouped)


def _pair_key(symbol: Any, direction: Any) -> tuple[str, str]:
    return (str(symbol or "").strip().upper(), str(direction or "").strip().lower())


def _build_position_diffs(
    source_positions: list[dict[str, Any]],
    local_positions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    rows: list[dict[str, Any]] = []
    local_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pos in local_positions:
        local_by_pair[_pair_key(pos.get("symbol"), pos.get("direction"))].append(pos)

    seen_pairs: set[tuple[str, str]] = set()
    for source in source_positions:
        pair = _pair_key(source.get("symbol"), source.get("direction"))
        seen_pairs.add(pair)
        locals_for_pair = local_by_pair.get(pair, [])
        if not locals_for_pair:
            issues.append(
                f"Source has open {pair[0]} {pair[1]} qty={_format_num(source.get('remaining_qty'))}, but no local open position was found."
            )
        rows.append(
            {
                "symbol": pair[0],
                "direction": pair[1],
                "source_remaining_qty": _safe_float(source.get("remaining_qty"), 0.0),
                "source_remaining_margin": _safe_float(source.get("remaining_margin"), 0.0),
                "local_positions": [
                    {
                        "platform": item.get("platform"),
                        "remaining_qty": _safe_float(item.get("remaining_qty"), 0.0),
                        "remaining_margin": _safe_float(item.get("remaining_margin"), 0.0),
                        "avg_entry_price": _safe_float(item.get("avg_entry_price"), 0.0),
                        "last_open_ts": _safe_int(item.get("last_open_ts"), 0),
                    }
                    for item in locals_for_pair
                ],
                "last_source_event_time": _safe_int(source.get("last_event_time"), 0),
            }
        )

    for pos in local_positions:
        pair = _pair_key(pos.get("symbol"), pos.get("direction"))
        if pair in seen_pairs:
            continue
        issues.append(
            f"Local still has {pair[0]} {pair[1]} on {pos.get('platform')} qty={_format_num(pos.get('remaining_qty'))}, but source no longer shows an open position."
        )
        rows.append(
            {
                "symbol": pair[0],
                "direction": pair[1],
                "source_remaining_qty": 0.0,
                "source_remaining_margin": 0.0,
                "local_positions": [
                    {
                        "platform": pos.get("platform"),
                        "remaining_qty": _safe_float(pos.get("remaining_qty"), 0.0),
                        "remaining_margin": _safe_float(pos.get("remaining_margin"), 0.0),
                        "avg_entry_price": _safe_float(pos.get("avg_entry_price"), 0.0),
                        "last_open_ts": _safe_int(pos.get("last_open_ts"), 0),
                    }
                ],
                "last_source_event_time": 0,
            }
        )

    rows.sort(key=lambda item: (item["symbol"], item["direction"]))
    return rows, issues


def _extract_attention_items(
    diagnostics: dict[str, Any] | None,
    traders: list[dict[str, Any]],
    recent_orders: list[dict[str, Any]],
) -> list[str]:
    items: list[str] = []
    if diagnostics:
        overall = str(diagnostics.get("overall") or "")
        if overall and overall != "pass":
            items.append(f"Live diagnostics overall status is {overall}.")
        for check in diagnostics.get("checks") or []:
            if str(check.get("status") or "") in {"warning", "blocker"}:
                items.append(f"{check.get('label')}: {check.get('detail')}")
    for trader in traders:
        for issue in trader.get("issues") or []:
            items.append(f"{trader.get('nickname') or trader.get('trader_uid')}: {issue}")
    for order in recent_orders[:12]:
        status = str(order.get("status") or "").lower()
        if status in {"failed", "skipped"}:
            items.append(
                f"Recent order {order.get('symbol')} {order.get('direction')} {order.get('action')} on {order.get('platform')} is {status}: {_short_text(order.get('notes'), 120)}"
            )
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = _short_text(item, 220)
        if clean and clean not in seen:
            seen.add(clean)
            deduped.append(clean)
    return deduped[:40]


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = report["summary"]
    runtime = report.get("runtime") or {}

    lines.append("# Runtime Transparency Report")
    lines.append("")
    lines.append(f"- Generated: `{_format_ts_ms(report.get('generated_at_ms'))}`")
    lines.append(f"- Profile: `{report.get('profile')}`")
    lines.append(f"- Engine running: `{summary.get('engine_running')}`")
    lines.append(f"- Diagnostics overall: `{summary.get('diagnostics_overall')}`")
    lines.append(f"- Enabled traders: `{summary.get('enabled_trader_count')}`")
    lines.append(f"- Source open positions: `{summary.get('source_open_position_count')}`")
    lines.append(f"- Local open positions: `{summary.get('local_open_position_count')}`")
    lines.append(f"- Recent orders: `{summary.get('recent_order_total')}`")
    lines.append(f"- Recent order status counts: `{json.dumps(summary.get('recent_order_status_counts') or {}, ensure_ascii=False)}`")
    if runtime.get("diagnostics_error"):
        lines.append(f"- Diagnostics fetch: `{runtime.get('diagnostics_error')}`")
    if runtime.get("status_error"):
        lines.append(f"- Status fetch: `{runtime.get('status_error')}`")
    lines.append("")

    attention = report.get("attention_items") or []
    lines.append("## Attention")
    lines.append("")
    if attention:
        for item in attention:
            lines.append(f"- {item}")
    else:
        lines.append("- No immediate warning items were detected.")
    lines.append("")

    lines.append("## Traders")
    lines.append("")
    for trader in report.get("traders") or []:
        lines.append(f"### {trader.get('nickname') or trader.get('trader_uid')}")
        lines.append("")
        lines.append(f"- Trader UID: `{trader.get('trader_uid')}`")
        lines.append(f"- Poll status: `{trader.get('poll_status')}`; age=`{trader.get('poll_age_sec')}` sec; new orders=`{trader.get('last_new_order_count')}`")
        lines.append(f"- Warmup: `{trader.get('warmup_status') or '-'}`; sync-open: `{trader.get('sync_open_status') or '-'}`; pending=`{trader.get('sync_open_positions_pending')}`")
        lines.append(
            f"- Latest source event: `{trader.get('latest_source_symbol') or '-'} {trader.get('latest_source_action') or '-'} @ {trader.get('latest_source_time_text')}`"
        )
        lines.append(
            f"- Latest copy order: `{trader.get('latest_copy_symbol') or '-'} {trader.get('latest_copy_action') or '-'} {trader.get('latest_copy_status') or '-'} @ {trader.get('latest_copy_time_text')}`"
        )
        lines.append(
            f"- Dynamic sizing: score=`{trader.get('analysis_total_score')}` clip=`{trader.get('analysis_clip_rate_text')}` reverse=`{trader.get('analysis_reverse_rate_text')}` hold=`{trader.get('analysis_avg_hold_sec')}` sec median_margin=`{trader.get('analysis_median_source_margin')}`"
        )
        if trader.get("issues"):
            lines.append("- Issues:")
            for item in trader["issues"]:
                lines.append(f"  - {item}")
        else:
            lines.append("- Issues: none")
        lines.append("- Position comparison:")
        if trader.get("position_pairs"):
            for pair in trader["position_pairs"]:
                local_parts = []
                for pos in pair.get("local_positions") or []:
                    local_parts.append(
                        f"{pos.get('platform')} qty={_format_num(pos.get('remaining_qty'))} margin={_format_num(pos.get('remaining_margin'), 2)}"
                    )
                local_text = "; ".join(local_parts) if local_parts else "none"
                lines.append(
                    f"  - {pair.get('symbol')} {pair.get('direction')}: source qty={_format_num(pair.get('source_remaining_qty'))} margin={_format_num(pair.get('source_remaining_margin'), 2)} | local {local_text}"
                )
        else:
            lines.append("  - no active source/local positions")
        recent_copy_orders = trader.get("recent_copy_orders") or []
        lines.append("- Recent copy orders:")
        if recent_copy_orders:
            for order in recent_copy_orders:
                lines.append(
                    f"  - {_format_ts_ms(order.get('timestamp'))} | {order.get('platform')} | {order.get('symbol')} {order.get('direction')} {order.get('action')} {order.get('status')} | note={_short_text(order.get('notes'), 120)}"
                )
        else:
            lines.append("  - no recent local copy orders")
        lines.append("")

    log_summary = report.get("logs") or {}
    lines.append("## Recent Warnings")
    lines.append("")
    warnings = log_summary.get("recent_warnings") or []
    if warnings:
        for item in warnings:
            lines.append(f"- `{item.get('file')}` {item.get('line')}")
    else:
        lines.append("- No warning/error log lines captured from the selected files.")
    lines.append("")

    lines.append("## Recent Lifecycle Lines")
    lines.append("")
    lifecycle = log_summary.get("recent_lifecycle") or []
    if lifecycle:
        for item in lifecycle:
            lines.append(f"- `{item.get('file')}` {item.get('line')}")
    else:
        lines.append("- No lifecycle lines matched in the selected log tail.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_runtime_transparency_report(
    profile: str = "live",
    base_url: str = "http://127.0.0.1:8080",
    http_timeout: float = 3.0,
    copy_order_limit: int = 200,
    per_trader_order_limit: int = 6,
    source_event_limit: int = 12,
    log_tail_lines: int = 400,
    lookback_days: int = 45,
) -> dict[str, Any]:
    profile_key = _normalize_profile(profile)
    db.init_db()

    enabled_traders = _get_enabled_binance_traders(profile_key)
    platform_keys = _profile_platform_keys(profile_key)
    runtime_status, status_error = _http_json(base_url, "/api/status", timeout=http_timeout)
    live_diagnostics, diagnostics_error = _http_json(base_url, "/api/live/diagnostics", timeout=http_timeout)

    recent_orders = _get_recent_copy_orders(platform_keys, limit=copy_order_limit)
    local_positions_by_trader = _group_local_positions(profile_key)
    recent_status_counts = Counter(str(order.get("status") or "").lower() for order in recent_orders)
    diagnostics_trader_map = {
        str(item.get("trader_uid") or ""): item
        for item in (live_diagnostics or {}).get("trader_polling") or []
        if isinstance(item, dict)
    }

    symbols = {str(order.get("symbol") or "").upper() for order in recent_orders if order.get("symbol")}
    log_summary = _collect_log_summary(
        _candidate_log_files(),
        tail_lines=log_tail_lines,
        trader_ids=set(enabled_traders.keys()),
        symbols=symbols,
    )

    trader_rows: list[dict[str, Any]] = []
    source_open_position_count = 0
    local_open_position_count = sum(len(rows) for rows in local_positions_by_trader.values())

    for trader_uid, trader_cfg in sorted(
        enabled_traders.items(),
        key=lambda item: (
            str(item[1].get("nickname") or item[0]).lower(),
            item[0],
        ),
    ):
        diag = diagnostics_trader_map.get(trader_uid) or {}
        source_events = _get_recent_source_events(trader_uid, limit=source_event_limit)
        latest_source = source_events[0] if source_events else {}
        source_positions = [
            item for item in db.get_source_position_summaries(trader_uid)
            if _safe_float(item.get("remaining_qty"), 0.0) > 1e-12
        ]
        source_open_position_count += len(source_positions)
        local_positions = local_positions_by_trader.get(trader_uid) or []
        position_pairs, position_issues = _build_position_diffs(source_positions, local_positions)
        analysis_error = ""
        try:
            analysis = db.get_trader_analysis_snapshot(trader_uid, lookback_days=lookback_days) or {}
        except Exception as exc:
            analysis = {}
            analysis_error = str(exc)
        recent_copy_orders = _get_recent_copy_orders(platform_keys, limit=per_trader_order_limit, trader_uid=trader_uid)
        latest_copy = recent_copy_orders[0] if recent_copy_orders else {}

        issues: list[str] = list(position_issues)
        if str(diag.get("status") or "") in {"warning", "blocker"}:
            issues.append(
                f"Polling is {diag.get('status')} (age={diag.get('poll_age_sec')} sec, error={_short_text(diag.get('last_poll_error'), 120) or '-'})"
            )
        if trader_cfg.get("sync_open_positions_pending") is True:
            issues.append("Initial sync-open is still pending.")
        if latest_copy and str(latest_copy.get("status") or "").lower() in {"failed", "skipped"}:
            issues.append(
                f"Latest copy order is {latest_copy.get('status')}: {_short_text(latest_copy.get('notes'), 160)}"
            )
        if analysis_error:
            issues.append(f"History analytics unavailable: {_short_text(analysis_error, 120)}")
        if _safe_float(analysis.get("clip_rate"), 0.0) >= 0.8:
            issues.append(f"Clip rate is high at {_format_pct(analysis.get('clip_rate'))}.")
        if _safe_float(analysis.get("reverse_rate"), 0.0) >= 0.5:
            issues.append(f"Reverse rate is high at {_format_pct(analysis.get('reverse_rate'))}.")

        trader_rows.append(
            {
                "trader_uid": trader_uid,
                "nickname": trader_cfg.get("nickname") or trader_uid,
                "copy_enabled_at": _safe_int(trader_cfg.get("copy_enabled_at"), 0),
                "added_at": _safe_int(trader_cfg.get("added_at"), 0),
                "sync_open_positions_pending": bool(trader_cfg.get("sync_open_positions_pending")),
                "sync_open_status": trader_cfg.get("sync_open_status") or diag.get("sync_open_status") or "",
                "last_sync_open_at": _safe_int(trader_cfg.get("last_sync_open_at"), 0),
                "poll_status": diag.get("status") or ("pass" if diag else "unknown"),
                "poll_age_sec": diag.get("poll_age_sec"),
                "last_new_order_count": _safe_int(diag.get("last_new_order_count"), 0),
                "warmup_status": diag.get("warmup_status") or "",
                "latest_source_symbol": latest_source.get("symbol") or "",
                "latest_source_action": latest_source.get("action") or "",
                "latest_source_time_ms": _safe_int(latest_source.get("order_time"), 0),
                "latest_source_time_text": _format_ts_ms(latest_source.get("order_time")),
                "latest_copy_symbol": latest_copy.get("symbol") or "",
                "latest_copy_action": latest_copy.get("action") or "",
                "latest_copy_status": latest_copy.get("status") or "",
                "latest_copy_time_ms": _safe_int(latest_copy.get("timestamp"), 0),
                "latest_copy_time_text": _format_ts_ms(latest_copy.get("timestamp")),
                "analysis_total_score": _safe_int(analysis.get("total_score"), 0),
                "analysis_clip_rate": _safe_float(analysis.get("clip_rate"), 0.0),
                "analysis_clip_rate_text": _format_pct(analysis.get("clip_rate")),
                "analysis_reverse_rate": _safe_float(analysis.get("reverse_rate"), 0.0),
                "analysis_reverse_rate_text": _format_pct(analysis.get("reverse_rate")),
                "analysis_avg_hold_sec": _safe_int(analysis.get("avg_hold_sec"), 0),
                "analysis_median_source_margin": round(_safe_float(analysis.get("median_source_margin"), 0.0), 2),
                "source_open_position_count": len(source_positions),
                "local_open_position_count": len(local_positions),
                "position_pairs": position_pairs,
                "issues": issues,
                "source_positions": source_positions,
                "local_positions": local_positions,
                "recent_source_events": source_events,
                "recent_copy_orders": recent_copy_orders,
                "diagnostics": diag,
                "analysis_snapshot": analysis,
                "analysis_error": analysis_error,
            }
        )

    summary = {
        "engine_running": bool((live_diagnostics or {}).get("engine_running")) or bool((runtime_status or {}).get("live_copy_engine_running")),
        "diagnostics_overall": (live_diagnostics or {}).get("overall") or ("unavailable" if diagnostics_error else "unknown"),
        "enabled_trader_count": len(enabled_traders),
        "source_open_position_count": source_open_position_count,
        "local_open_position_count": local_open_position_count,
        "recent_order_total": len(recent_orders),
        "recent_order_status_counts": dict(sorted(recent_status_counts.items())),
    }

    report = {
        "generated_at_ms": int(time.time() * 1000),
        "profile": profile_key,
        "base_url": base_url,
        "runtime": {
            "status": runtime_status,
            "status_error": status_error,
            "live_diagnostics": live_diagnostics,
            "diagnostics_error": diagnostics_error,
        },
        "summary": summary,
        "recent_orders": recent_orders,
        "traders": trader_rows,
        "logs": log_summary,
    }
    report["attention_items"] = _extract_attention_items(live_diagnostics, trader_rows, recent_orders)
    return report


def export_runtime_transparency_report(
    output_dir: Path,
    profile: str = "live",
    base_url: str = "http://127.0.0.1:8080",
    http_timeout: float = 3.0,
    copy_order_limit: int = 200,
    per_trader_order_limit: int = 6,
    source_event_limit: int = 12,
    log_tail_lines: int = 400,
    lookback_days: int = 45,
) -> Path:
    report = build_runtime_transparency_report(
        profile=profile,
        base_url=base_url,
        http_timeout=http_timeout,
        copy_order_limit=copy_order_limit,
        per_trader_order_limit=per_trader_order_limit,
        source_event_limit=source_event_limit,
        log_tail_lines=log_tail_lines,
        lookback_days=lookback_days,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "runtime_transparency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "runtime_transparency.md").write_text(
        _render_markdown(report),
        encoding="utf-8",
    )
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a runtime transparency report for the current copy-trading system.")
    parser.add_argument("--profile", default="live", help="Profile to inspect. Default: live")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Base URL for the running local web app.")
    parser.add_argument("--http-timeout", type=float, default=3.0, help="HTTP timeout in seconds for local diagnostics calls.")
    parser.add_argument("--copy-order-limit", type=int, default=200, help="How many recent copy orders to inspect.")
    parser.add_argument("--per-trader-order-limit", type=int, default=6, help="How many recent copy orders to include per trader.")
    parser.add_argument("--source-event-limit", type=int, default=12, help="How many recent source events to include per trader.")
    parser.add_argument("--log-tail-lines", type=int, default=400, help="How many recent lines to read from each candidate log file.")
    parser.add_argument("--lookback-days", type=int, default=45, help="How many days of history to use for trader analysis snapshots.")
    parser.add_argument("--output-dir", default="", help="Directory to write report files into.")
    args = parser.parse_args()

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = ROOT / "backups" / f"runtime-transparency-{time.strftime('%Y%m%d-%H%M%S')}"

    final_dir = export_runtime_transparency_report(
        output_dir=output_dir,
        profile=args.profile,
        base_url=args.base_url,
        http_timeout=args.http_timeout,
        copy_order_limit=args.copy_order_limit,
        per_trader_order_limit=args.per_trader_order_limit,
        source_event_limit=args.source_event_limit,
        log_tail_lines=args.log_tail_lines,
        lookback_days=args.lookback_days,
    )
    print(final_dir)


if __name__ == "__main__":
    main()
