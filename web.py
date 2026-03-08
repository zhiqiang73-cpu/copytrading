"""
BitgetFollow Web 仪表盘
双击启动后在浏览器中操作，无需命令行。
仅支持币安交易员跟单 → Bitget 下单。
"""
import logging
import os
import socket
import threading
import time
import json
import atexit
import sys
import signal
import secrets

from flask import Flask, render_template, request, jsonify, redirect, url_for, make_response, abort

import api_client
import config
import copy_engine
import database as db
import order_executor
# ── Flask 初始化 ──────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(24)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("web")

# 心跳机制：检测网页是否关闭
_last_heartbeat = time.time()
_heartbeat_lock = threading.Lock()
_config_lock = threading.RLock()
_AUTO_EXIT_ON_HEARTBEAT_LOSS = os.getenv("AUTO_EXIT_ON_HEARTBEAT_LOSS", "1") == "1"
APP_UI_TOKEN = os.getenv("BITGETFOLLOW_UI_TOKEN") or secrets.token_urlsafe(24)
_ALLOWED_LOCAL_ORIGINS = ("http://127.0.0.1", "http://localhost")
_SECRET_DB_FIELDS = ("api_key", "api_secret", "api_passphrase", "binance_api_key", "binance_api_secret")

@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()
    return jsonify({"ok": True})

def _heartbeat_monitor():
    """后台监控：如果 60 秒没收到心跳，说明网页已关闭。"""
    logger.info("心跳监控启动（首次收到心跳后，60秒无响应将提示网页关闭）")
    _ever_received = False
    while True:
        time.sleep(5)
        with _heartbeat_lock:
            elapsed = time.time() - _last_heartbeat
        if elapsed < 30:
            _ever_received = True
        if _ever_received and elapsed > 60:
            if _AUTO_EXIT_ON_HEARTBEAT_LOSS:
                logger.warning("检测到网页已关闭（60秒无心跳），即将自动退出进程")
                try:
                    _cleanup()
                except Exception:
                    pass
                os._exit(0)
            else:
                logger.warning("检测到网页已关闭（60秒无心跳），当前配置为保活运行，不自动退出")
                _ever_received = False


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _api_configured() -> bool:
    """?? API Key ??????"""
    return bool(
        config.BITGET_API_KEY
        and config.BITGET_API_KEY != "your_api_key_here"
        and config.BITGET_SECRET_KEY
        and config.BITGET_PASSPHRASE
    )


@app.context_processor
def _inject_template_globals():
    return {"app_ui_token": APP_UI_TOKEN}


@app.before_request
def _protect_local_mutations():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.path.startswith("/static/"):
        return None

    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if origin and not origin.startswith(_ALLOWED_LOCAL_ORIGINS):
        abort(403)

    token = request.headers.get("X-App-Token") or request.form.get("_app_token") or request.args.get("_app_token")
    if token != APP_UI_TOKEN:
        return jsonify({"error": "?????????????????"}), 403
    return None


def _env_path() -> str:
    return os.path.join(os.path.dirname(__file__), ".env")


def _read_env_map() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = _env_path()
    if not os.path.exists(env_path):
        return env
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def _write_env_map(updates: dict[str, str]) -> None:
    env = _read_env_map()
    env.update({k: str(v) for k, v in updates.items() if v is not None})
    ordered_keys = [
        "BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE",
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_BASE_URL",
        "BITGET_SIMULATED", "POLL_INTERVAL", "LOG_LEVEL",
        "DEFAULT_DAILY_LOSS_LIMIT_PCT", "DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT",
    ]
    all_keys = ordered_keys + sorted(k for k in env.keys() if k not in ordered_keys)
    seen: set[str] = set()
    with open(_env_path(), "w", encoding="utf-8") as f:
        for key in all_keys:
            if key in seen or key not in env:
                continue
            seen.add(key)
            f.write(f"{key}={env[key]}\n")


def _mask_secret(value: str, keep: int = 4) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "****" + value[-keep:]


def _migrate_plaintext_secrets_out_of_db() -> None:
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT api_key, api_secret, api_passphrase, binance_api_key, binance_api_secret FROM copy_settings WHERE id = 1"
            ).fetchone()
        if not row:
            return
        raw = dict(row)
        if not any(raw.get(key) for key in _SECRET_DB_FIELDS):
            return

        updates: dict[str, str] = {}
        if raw.get("api_key"):
            updates["BITGET_API_KEY"] = raw["api_key"]
        if raw.get("api_secret"):
            updates["BITGET_SECRET_KEY"] = raw["api_secret"]
        if raw.get("api_passphrase"):
            updates["BITGET_PASSPHRASE"] = raw["api_passphrase"]
        if raw.get("binance_api_key"):
            updates["BINANCE_API_KEY"] = raw["binance_api_key"]
        if raw.get("binance_api_secret"):
            updates["BINANCE_API_SECRET"] = raw["binance_api_secret"]
        if updates:
            updates.setdefault("BINANCE_BASE_URL", config.BINANCE_BASE_URL)
            updates.setdefault("BITGET_SIMULATED", "1" if config.SIMULATED else "0")
            updates.setdefault("POLL_INTERVAL", str(config.POLL_INTERVAL))
            updates.setdefault("LOG_LEVEL", config.LOG_LEVEL)
            _write_env_map(updates)
            _reload_config()
        db.update_copy_settings(
            api_key="", api_secret="", api_passphrase="",
            binance_api_key="", binance_api_secret="",
        )
        logger.warning("????????????????? .env ??? SQLite ??")
    except Exception as exc:
        logger.warning("???????????: %s", exc)


def _reload_config():
    """???? .env ??? config ?????????"""
    from dotenv import load_dotenv
    with _config_lock:
        env_path = _env_path()
        load_dotenv(env_path, override=True)
        config.BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
        config.BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
        config.BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
        config.BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
        config.BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
        config.BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL", config.BINANCE_BASE_URL) or "").strip().rstrip("/")
        if not config.BINANCE_BASE_URL:
            config.BINANCE_BASE_URL = "https://testnet.binancefuture.com"
    config.POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
    config.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def _fmt_ts(ms_ts):
    """毫秒时间戳 → 可读字符串。"""
    if not ms_ts:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ms_ts / 1000).strftime("%m-%d %H:%M")


def _fmt_h(seconds):
    """秒 → 可读持仓时长。"""
    if not seconds or seconds <= 0:
        return "-"
    hours = seconds / 3600
    if hours < 1:
        return f"{int(hours * 60)}m"
    return f"{hours:.1f}h"


def _to_float_or_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in ("", "-", "--", "null", "None"):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_number(data: dict, keys: tuple[str, ...]):
    for key in keys:
        v = _to_float_or_none(data.get(key))
        if v is not None:
            return v
    return None


def _extract_wallet_metrics(balance_raw):
    data = balance_raw
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None, None

    wallet_balance = _pick_number(
        data,
        (
            "usdtEquity",
            "equity",
            "accountEquity",
            "totalEquity",
            "netAsset",
            "balance",
        ),
    )
    available_balance = _pick_number(
        data,
        (
            "available",
            "availableEquity",
            "maxAvailable",
            "free",
            "availableBalance",
        ),
    )
    if wallet_balance is None:
        wallet_balance = available_balance
    return wallet_balance, available_balance


def _build_account_overview(api_key: str, api_secret: str, api_passphrase: str):
    balance_raw = order_executor.get_account_balance(api_key, api_secret, api_passphrase)
    wallet_balance, available_balance = _extract_wallet_metrics(balance_raw)
    if wallet_balance is None:
        return None

    day = time.strftime("%Y-%m-%d", time.localtime())
    daily = db.upsert_account_daily_equity(day, wallet_balance)
    start_equity = _to_float_or_none(daily.get("start_equity")) or 0.0
    day_pnl = _to_float_or_none(daily.get("day_pnl")) or 0.0
    day_pnl_pct = (day_pnl / start_equity * 100.0) if start_equity > 0 else None
    start_ts = int(daily.get("start_ts") or 0)

    return {
        "wallet_balance": wallet_balance,
        "available_balance": available_balance,
        "day": day,
        "day_start_equity": start_equity,
        "day_start_ts": start_ts * 1000 if start_ts > 0 else None,
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "updated_at": int(time.time() * 1000),
    }


def _normalize_copy_settings(raw: dict) -> dict:
    defaults = {
        "api_key": config.BITGET_API_KEY or "",
        "api_secret": config.BITGET_SECRET_KEY or "",
        "api_passphrase": config.BITGET_PASSPHRASE or "",
        "total_capital": 0.0,
        "follow_ratio_pct": 0.003,
        "max_margin_pct": 0.20,
        "price_tolerance": 0.0002,
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
        "enabled_traders": [],
        "binance_traders": {},
        "engine_enabled": 0,
        "binance_api_key": config.BINANCE_API_KEY or "",
        "binance_api_secret": config.BINANCE_API_SECRET or "",
        "binance_total_capital": 0.0,
        "binance_follow_ratio_pct": 0.003,
        "binance_max_margin_pct": 0.20,
        "binance_price_tolerance": 0.0002,
    }
    settings = {**defaults, **(raw or {})}

    def _coerce_float(name: str, default: float) -> None:
        value = _to_float_or_none(settings.get(name))
        settings[name] = default if value is None else value

    try:
        et = settings.get("enabled_traders") or "[]"
        if isinstance(et, list):
            settings["enabled_traders"] = et
        else:
            settings["enabled_traders"] = json.loads(et)
    except Exception:
        settings["enabled_traders"] = []

    bt = settings.get("binance_traders")
    if isinstance(bt, str):
        try:
            bt = json.loads(bt)
            if isinstance(bt, str):
                bt = json.loads(bt)
        except Exception:
            bt = {}
    if not isinstance(bt, (dict, list)):
        bt = {}
    settings["binance_traders"] = bt

    raw_take_profit_enabled = settings.get("take_profit_enabled", defaults["take_profit_enabled"])
    if isinstance(raw_take_profit_enabled, str):
        settings["take_profit_enabled"] = 1 if raw_take_profit_enabled.strip().lower() in ("1", "true", "yes", "on") else 0
    else:
        settings["take_profit_enabled"] = 1 if raw_take_profit_enabled else 0

    settings["engine_enabled"] = int(settings.get("engine_enabled") or 0)
    _coerce_float("follow_ratio_pct", defaults["follow_ratio_pct"])
    _coerce_float("max_margin_pct", defaults["max_margin_pct"])
    _coerce_float("price_tolerance", defaults["price_tolerance"])
    _coerce_float("sl_pct", defaults["sl_pct"])
    _coerce_float("tp_pct", defaults["tp_pct"])
    _coerce_float("daily_loss_limit_pct", defaults["daily_loss_limit_pct"])
    _coerce_float("total_drawdown_limit_pct", defaults["total_drawdown_limit_pct"])
    _coerce_float("stop_loss_pct", defaults["stop_loss_pct"])
    _coerce_float("tp1_roi_pct", defaults["tp1_roi_pct"])
    _coerce_float("tp1_close_pct", defaults["tp1_close_pct"])
    _coerce_float("tp2_roi_pct", defaults["tp2_roi_pct"])
    _coerce_float("tp2_close_pct", defaults["tp2_close_pct"])
    _coerce_float("tp3_roi_pct", defaults["tp3_roi_pct"])
    _coerce_float("tp3_close_pct", defaults["tp3_close_pct"])
    _coerce_float("breakeven_buffer_pct", defaults["breakeven_buffer_pct"])
    _coerce_float("trail_callback_pct", defaults["trail_callback_pct"])
    _coerce_float("binance_total_capital", defaults["binance_total_capital"])
    _coerce_float("binance_follow_ratio_pct", defaults["binance_follow_ratio_pct"])
    _coerce_float("binance_max_margin_pct", defaults["binance_max_margin_pct"])
    _coerce_float("binance_price_tolerance", defaults["binance_price_tolerance"])
    settings["api_key"] = settings.get("api_key") or config.BITGET_API_KEY
    settings["api_secret"] = settings.get("api_secret") or config.BITGET_SECRET_KEY
    settings["api_passphrase"] = settings.get("api_passphrase") or config.BITGET_PASSPHRASE
    settings["binance_api_key"] = settings.get("binance_api_key") or config.BINANCE_API_KEY
    settings["binance_api_secret"] = settings.get("binance_api_secret") or config.BINANCE_API_SECRET
    return settings


def _with_temp_api_config(api_key: str, api_secret: str, api_passphrase: str, fn):
    prev = (config.BITGET_API_KEY, config.BITGET_SECRET_KEY, config.BITGET_PASSPHRASE)
    config.BITGET_API_KEY = api_key
    config.BITGET_SECRET_KEY = api_secret
    config.BITGET_PASSPHRASE = api_passphrase
    try:
        return fn()
    finally:
        config.BITGET_API_KEY, config.BITGET_SECRET_KEY, config.BITGET_PASSPHRASE = prev


# ── 页面路由 ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", api_configured=_api_configured())


@app.route("/settings", methods=["GET", "POST"])
def settings():
    msg = ""
    msg_type = ""
    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip() or config.BITGET_API_KEY or ""
        secret_key = request.form.get("secret_key", "").strip() or config.BITGET_SECRET_KEY or ""
        passphrase = request.form.get("passphrase", "").strip() or config.BITGET_PASSPHRASE or ""
        poll_interval = request.form.get("poll_interval", "5").strip() or str(config.POLL_INTERVAL)

        if not api_key or not secret_key or not passphrase:
            msg = "?????????"
            msg_type = "error"
        else:
            _write_env_map({
                "BITGET_API_KEY": api_key,
                "BITGET_SECRET_KEY": secret_key,
                "BITGET_PASSPHRASE": passphrase,
                "BITGET_SIMULATED": os.getenv("BITGET_SIMULATED", "0"),
                "POLL_INTERVAL": poll_interval,
                "LOG_LEVEL": os.getenv("LOG_LEVEL", config.LOG_LEVEL),
                "BINANCE_BASE_URL": config.BINANCE_BASE_URL,
                "DEFAULT_DAILY_LOSS_LIMIT_PCT": os.getenv("DEFAULT_DAILY_LOSS_LIMIT_PCT", str(config.DEFAULT_DAILY_LOSS_LIMIT_PCT)),
                "DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT": os.getenv("DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT", str(config.DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT)),
            })
            db.update_copy_settings(api_key="", api_secret="", api_passphrase="")
            _reload_config()
            msg = "API ?????"
            msg_type = "success"

    current = {
        "api_key": config.BITGET_API_KEY or "",
        "secret_key": "",
        "passphrase": "",
        "poll_interval": config.POLL_INTERVAL,
        "has_secret": bool(config.BITGET_SECRET_KEY),
        "has_passphrase": bool(config.BITGET_PASSPHRASE),
    }
    configured = _api_configured()
    return render_template("settings.html", current=current, configured=configured, msg=msg, msg_type=msg_type)


@app.route("/my-positions")
def my_positions():
    resp = make_response(render_template("my_positions.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── API 路由 ──────────────────────────────────────────────────────────────────

@app.route("/api/add_binance_trader", methods=["POST"])
def api_add_binance_trader():
    """通过 Binance URL 或 Portfolio ID 添加币安交易员到跟单列表"""
    import binance_scraper
    
    url_or_pid = (request.json or {}).get("url", "").strip()
    if not url_or_pid:
        return jsonify({"error": "URL 或 Portfolio ID 不能为空"}), 400
    
    try:
        portfolio_id = binance_scraper.parse_binance_url(url_or_pid) or url_or_pid
        
        if not portfolio_id.isdigit() or len(portfolio_id) < 10:
            return jsonify({
                "error": f"无效的 Portfolio ID: {portfolio_id}\n请使用完整的 URL 或正确的 ID（数字，至少 10 位）"
            }), 400
        
        logger.info("正在添加币安交易员：%s", portfolio_id[:12])
        
        info = binance_scraper.fetch_trader_info(portfolio_id)
        if not info:
            info = {
                "portfolio_id": portfolio_id,
                "nickname": f"币安交易员_{portfolio_id[:8]}",
            }
        
        settings = db.get_copy_settings()
        bn_traders_raw = settings.get("binance_traders") or "[]"
        
        try:
            bn_traders_data = json.loads(bn_traders_raw)
            if isinstance(bn_traders_data, list) and bn_traders_data and isinstance(bn_traders_data[0], str):
                bn_traders_dict = {pid: {"nickname": f"币安交易员_{pid[:8]}"} for pid in bn_traders_data}
            elif isinstance(bn_traders_data, dict):
                bn_traders_dict = bn_traders_data
            else:
                bn_traders_dict = {}
        except:
            bn_traders_dict = {}
        
        if portfolio_id not in bn_traders_dict:
            bn_traders_dict[portfolio_id] = {
                "nickname": info.get("nickname"),
                "roi": info.get("roi"),
                "win_rate": info.get("win_rate"),
                "follower_count": info.get("follower_count"),
                "copier_pnl": info.get("copier_pnl"),
                "aum": info.get("aum"),
                "avatar": info.get("avatar"),
                "total_trades": info.get("total_trades"),
                "copy_enabled": True,
                "added_at": int(time.time())
            }
            db.update_copy_settings(binance_traders=json.dumps(bn_traders_dict))
            logger.info("已添加币安交易员 %s (%s)", portfolio_id[:12], info.get("nickname"))
        else:
            logger.warning("币安交易员已存在: %s", portfolio_id[:12])
        
        return jsonify({
            "ok": True,
            "portfolio_id": portfolio_id,
            "info": info,
            "msg": f"已成功添加币安交易员 {info.get('nickname')}"
        })
    
    except Exception as exc:
        logger.error("添加币安交易员失败: %s", exc, exc_info=True)
        return jsonify({"error": f"添加失败：{str(exc)[:200]}"}), 500


@app.route("/api/remove_binance_trader", methods=["POST"])
def api_remove_binance_trader():
    """从跟单列表移除币安交易员"""
    portfolio_id = (request.json or {}).get("portfolio_id", "").strip()
    if not portfolio_id:
        return jsonify({"error": "portfolio_id 不能为空"}), 400
    
    try:
        settings = db.get_copy_settings()
        bn_traders_raw = settings.get("binance_traders") or "[]"
        
        try:
            bn_traders_data = json.loads(bn_traders_raw)
        except:
            bn_traders_data = {}
        
        if isinstance(bn_traders_data, dict):
            if portfolio_id in bn_traders_data:
                del bn_traders_data[portfolio_id]
                db.update_copy_settings(binance_traders=json.dumps(bn_traders_data))
                logger.info("已移除币安交易员 %s", portfolio_id[:12])
        elif isinstance(bn_traders_data, list):
            if portfolio_id in bn_traders_data:
                bn_traders_data.remove(portfolio_id)
                db.update_copy_settings(binance_traders=json.dumps(bn_traders_data))
                logger.info("已移除币安交易员 %s", portfolio_id[:12])
        
        return jsonify({"ok": True, "msg": "已移除"})
    except Exception as exc:
        logger.error("移除币安交易员失败: %s", exc, exc_info=True)
        return jsonify({"error": f"移除失败：{exc}"}), 500


@app.route("/api/toggle_copy", methods=["POST"])
def api_toggle_copy():
    """切换币安交易员的跟单开关"""
    data = request.json or {}
    uid = data.get("uid", "").strip()
    enabled = data.get("enabled", False)
    
    if not uid:
        return jsonify({"error": "缺少交易员ID"}), 400
    
    settings = db.get_copy_settings()
    raw = settings.get("binance_traders") or "{}"
    bn_traders = json.loads(raw) if isinstance(raw, str) else raw
    
    if uid in bn_traders:
        bn_traders[uid]["copy_enabled"] = enabled
        db.update_copy_settings(binance_traders=json.dumps(bn_traders))
        logger.info("%s 币安跟单: %s", "启用" if enabled else "禁用", uid[:12])
        return jsonify({"ok": True, "enabled": enabled})
    else:
        return jsonify({"error": "币安交易员不存在"}), 404


@app.route("/api/status")
def api_status():
    return jsonify({
        "api_configured": _api_configured(),
        "copy_engine_running": copy_engine.is_engine_running(),
    })


# ── 排行榜扫描 API ───────────────────────────────────────────────────────────

@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    """启动后台排行榜扫描"""
    import binance_scanner
    
    status = binance_scanner.get_scan_status()
    if status.get("running"):
        return jsonify({"error": "扫描已在运行中"}), 400
    
    payload = request.json or {}
    filters = {
        "min_copier_pnl": float(payload.get("min_copier_pnl", 0)),
        "min_followers": int(payload.get("min_followers", 10)),
        "min_sharp_ratio": float(payload.get("min_sharp_ratio", 0)),
        "min_trades": int(payload.get("min_trades", 5)),
        "min_win_rate": float(payload.get("min_win_rate", 0)),
        "sort_by": payload.get("sort_by", "copier_pnl"),
        "max_results": int(payload.get("max_results", 30)),
    }
    max_scroll = int(payload.get("max_scroll", 8))
    
    binance_scanner.start_scan(filters=filters, max_scroll=max_scroll)
    return jsonify({"ok": True, "msg": "扫描已启动"})


@app.route("/api/scan/status")
def api_scan_status():
    """查询扫描进度"""
    import binance_scanner
    status = binance_scanner.get_scan_status()
    # 不返回完整结果（太大），只返回状态信息
    return jsonify({
        "running": status["running"],
        "phase": status["phase"],
        "progress": status["progress"],
        "total_found": status["total_found"],
        "analyzed": status["analyzed"],
        "result_count": len(status.get("results", [])),
        "error": status["error"],
        "started_at": status["started_at"],
        "finished_at": status["finished_at"],
    })


@app.route("/api/scan/results")
def api_scan_results():
    """获取扫描结果"""
    import binance_scanner
    status = binance_scanner.get_scan_status()
    results = status.get("results", [])
    
    # 标记已添加的交易员
    settings = _normalize_copy_settings(db.get_copy_settings())
    bn_raw = settings.get("binance_traders") or {}
    if isinstance(bn_raw, str):
        try: bn_raw = json.loads(bn_raw)
        except: bn_raw = {}
    existing_pids = set(str(k) for k in bn_raw.keys())
    
    for r in results:
        r["already_added"] = str(r.get("portfolio_id", "")) in existing_pids
    
    return jsonify({
        "results": results,
        "total": len(results),
        "phase": status["phase"],
    })


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    """停止扫描"""
    import binance_scanner
    binance_scanner.stop_scan()
    return jsonify({"ok": True, "msg": "扫描已停止"})


# ── Copy Trading API ──────────────────────────────────────────────────────────

@app.route("/api/copy/settings", methods=["GET", "POST"])
def api_copy_settings():
    if request.method == "POST":
        payload = request.json or {}
        existing = _normalize_copy_settings(db.get_copy_settings())
        api_key = (payload.get("api_key") or "").strip() or config.BITGET_API_KEY or ""
        api_secret = (payload.get("api_secret") or "").strip() or config.BITGET_SECRET_KEY or ""
        api_passphrase = (payload.get("api_passphrase") or "").strip() or config.BITGET_PASSPHRASE or ""
        binance_api_key = (payload.get("binance_api_key") or "").strip() or config.BINANCE_API_KEY or ""
        binance_api_secret = (payload.get("binance_api_secret") or "").strip() or config.BINANCE_API_SECRET or ""

        def _float_or(raw_v, default_v):
            if raw_v is None or raw_v == "":
                return float(default_v)
            try:
                return float(raw_v)
            except Exception:
                return float(default_v)

        def _ratio_or(raw_v, default_v):
            value = _float_or(raw_v, default_v)
            if value > 1:
                value = value / 100.0
            return min(max(value, 0.0), 1.0)

        def _bool_or(raw_v, default_v):
            if raw_v is None or raw_v == "":
                return 1 if default_v else 0
            if isinstance(raw_v, bool):
                return 1 if raw_v else 0
            if isinstance(raw_v, (int, float)):
                return 1 if raw_v else 0
            return 1 if str(raw_v).strip().lower() in ("1", "true", "yes", "on") else 0

        total_capital = _float_or(payload.get("total_capital"), existing.get("total_capital", 0.0))
        follow_ratio_pct = _ratio_or(payload.get("follow_ratio_pct"), existing.get("follow_ratio_pct", 0.003))
        max_margin_pct = _ratio_or(payload.get("max_margin_pct"), existing.get("max_margin_pct", 0.2))
        price_tolerance = _ratio_or(payload.get("price_tolerance"), existing.get("price_tolerance", 0.0002))
        sl_pct = _ratio_or(payload.get("sl_pct"), existing.get("sl_pct", 0.15))
        tp_pct = _ratio_or(payload.get("tp_pct"), existing.get("tp_pct", 0.30))
        daily_loss_limit_pct = _ratio_or(payload.get("daily_loss_limit_pct"), existing.get("daily_loss_limit_pct", config.DEFAULT_DAILY_LOSS_LIMIT_PCT))
        total_drawdown_limit_pct = _ratio_or(payload.get("total_drawdown_limit_pct"), existing.get("total_drawdown_limit_pct", config.DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT))
        take_profit_enabled = _bool_or(payload.get("take_profit_enabled"), existing.get("take_profit_enabled", 1 if config.DEFAULT_TAKE_PROFIT_ENABLED else 0))
        stop_loss_pct = _ratio_or(payload.get("stop_loss_pct"), existing.get("stop_loss_pct", config.DEFAULT_STOP_LOSS_PCT))
        tp1_roi_pct = _ratio_or(payload.get("tp1_roi_pct"), existing.get("tp1_roi_pct", config.DEFAULT_TP1_ROI_PCT))
        tp1_close_pct = _ratio_or(payload.get("tp1_close_pct"), existing.get("tp1_close_pct", config.DEFAULT_TP1_CLOSE_PCT))
        tp2_roi_pct = _ratio_or(payload.get("tp2_roi_pct"), existing.get("tp2_roi_pct", config.DEFAULT_TP2_ROI_PCT))
        tp2_close_pct = _ratio_or(payload.get("tp2_close_pct"), existing.get("tp2_close_pct", config.DEFAULT_TP2_CLOSE_PCT))
        tp3_roi_pct = _ratio_or(payload.get("tp3_roi_pct"), existing.get("tp3_roi_pct", config.DEFAULT_TP3_ROI_PCT))
        tp3_close_pct = _ratio_or(payload.get("tp3_close_pct"), existing.get("tp3_close_pct", config.DEFAULT_TP3_CLOSE_PCT))
        breakeven_buffer_pct = _ratio_or(payload.get("breakeven_buffer_pct"), existing.get("breakeven_buffer_pct", config.DEFAULT_BREAKEVEN_BUFFER_PCT))
        trail_callback_pct = _ratio_or(payload.get("trail_callback_pct"), existing.get("trail_callback_pct", config.DEFAULT_TRAIL_CALLBACK_PCT))

        binance_traders = payload.get("binance_traders")
        if binance_traders is None:
            binance_traders = existing.get("binance_traders") or {}
        elif isinstance(binance_traders, str):
            try:
                binance_traders = json.loads(binance_traders)
            except Exception:
                binance_traders = existing.get("binance_traders") or {}

        normalized_bn: dict[str, dict] = {}
        if isinstance(binance_traders, list):
            for pid in binance_traders:
                spid = str(pid).strip()
                if not spid:
                    continue
                normalized_bn[spid] = {
                    "nickname": f"Trader_{spid[:8]}",
                    "copy_enabled": True,
                }
        elif isinstance(binance_traders, dict):
            for pid, info in binance_traders.items():
                spid = str(pid).strip()
                if not spid:
                    continue
                row = dict(info) if isinstance(info, dict) else {}
                row["nickname"] = row.get("nickname") or f"Trader_{spid[:8]}"
                row["copy_enabled"] = bool(row.get("copy_enabled", True))
                normalized_bn[spid] = row

        if not normalized_bn and isinstance(existing.get("binance_traders"), dict):
            normalized_bn = existing.get("binance_traders")

        binance_total_capital = _float_or(payload.get("binance_total_capital"), existing.get("binance_total_capital", 0.0))
        binance_follow_ratio_pct = _ratio_or(payload.get("binance_follow_ratio_pct"), existing.get("binance_follow_ratio_pct", 0.003))
        binance_max_margin_pct = _ratio_or(payload.get("binance_max_margin_pct"), existing.get("binance_max_margin_pct", 0.2))
        binance_price_tolerance = _ratio_or(payload.get("binance_price_tolerance"), existing.get("binance_price_tolerance", 0.0002))

        enabled_traders = payload.get("enabled_traders")
        if enabled_traders is None:
            enabled_traders = existing.get("enabled_traders", [])

        _write_env_map({
            "BITGET_API_KEY": api_key,
            "BITGET_SECRET_KEY": api_secret,
            "BITGET_PASSPHRASE": api_passphrase,
            "BINANCE_API_KEY": binance_api_key,
            "BINANCE_API_SECRET": binance_api_secret,
            "BINANCE_BASE_URL": config.BINANCE_BASE_URL,
            "BITGET_SIMULATED": os.getenv("BITGET_SIMULATED", "0"),
            "POLL_INTERVAL": str(config.POLL_INTERVAL),
            "LOG_LEVEL": config.LOG_LEVEL,
            "DEFAULT_DAILY_LOSS_LIMIT_PCT": str(daily_loss_limit_pct),
            "DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT": str(total_drawdown_limit_pct),
        })
        _reload_config()

        db.update_copy_settings(
            api_key="",
            api_secret="",
            api_passphrase="",
            total_capital=total_capital,
            follow_ratio_pct=follow_ratio_pct,
            max_margin_pct=max_margin_pct,
            price_tolerance=price_tolerance,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            daily_loss_limit_pct=daily_loss_limit_pct,
            total_drawdown_limit_pct=total_drawdown_limit_pct,
            take_profit_enabled=take_profit_enabled,
            stop_loss_pct=stop_loss_pct,
            tp1_roi_pct=tp1_roi_pct,
            tp1_close_pct=tp1_close_pct,
            tp2_roi_pct=tp2_roi_pct,
            tp2_close_pct=tp2_close_pct,
            tp3_roi_pct=tp3_roi_pct,
            tp3_close_pct=tp3_close_pct,
            breakeven_buffer_pct=breakeven_buffer_pct,
            trail_callback_pct=trail_callback_pct,
            enabled_traders=json.dumps(enabled_traders),
            binance_traders=json.dumps(normalized_bn, ensure_ascii=False),
            binance_api_key="",
            binance_api_secret="",
            binance_total_capital=binance_total_capital,
            binance_follow_ratio_pct=binance_follow_ratio_pct,
            binance_max_margin_pct=binance_max_margin_pct,
            binance_price_tolerance=binance_price_tolerance,
        )

        return jsonify({"ok": True})

    settings = _normalize_copy_settings(db.get_copy_settings())
    safe_settings = dict(settings)
    for field in ("api_secret", "api_passphrase", "binance_api_secret"):
        safe_settings[field] = _mask_secret(str(safe_settings.get(field) or ""))
    return jsonify(safe_settings)


@app.route("/api/copy/test_api", methods=["POST"])
def api_copy_test_api():
    payload = request.json or {}
    source = payload.get("source", "bitget")
    existing = _normalize_copy_settings(db.get_copy_settings())
    
    if source == "binance":
        api_key = (payload.get("binance_api_key") or "").strip() or existing.get("binance_api_key") or ""
        api_secret = (payload.get("binance_api_secret") or "").strip() or existing.get("binance_api_secret") or ""
        if not api_key or not api_secret:
            return jsonify({"error": "币安 API Key / Secret 不能为空"}), 400
        try:
            balance_info = order_executor.test_binance_connection(api_key, api_secret)
            available = float(balance_info.get("availableBalance", 0))
            return jsonify({"ok": True, "msg": f"币安连接成功，可用余额 {available:.2f} USDT | endpoint={config.BINANCE_BASE_URL}"})
        except Exception as e:
            logger.error("币安 API 测试失败: %s", e)
            err = str(e)
            if "code=-2015" in err or "Invalid API-key" in err:
                err = f"{err} | 请确认当前 endpoint 与 API Key 所属环境一致（testnet/mainnet）: {config.BINANCE_BASE_URL}"
            return jsonify({"error": f"币安连接失败：{err}"}), 400

    api_key = (payload.get("api_key") or "").strip() or config.BITGET_API_KEY or ""
    api_secret = (payload.get("api_secret") or "").strip() or config.BITGET_SECRET_KEY or ""
    api_passphrase = (payload.get("api_passphrase") or "").strip() or config.BITGET_PASSPHRASE or ""
    if not api_key or not api_secret or not api_passphrase:
        return jsonify({"error": "Bitget API Key / Secret / Passphrase 不能为空"}), 400
    try:
        balance = order_executor.get_account_balance(api_key, api_secret, api_passphrase)
        available = 0.0
        if isinstance(balance, dict):
            for k in ("available", "availableEquity", "maxAvailable"):
                if balance.get(k) is not None:
                    available = float(balance[k])
                    break
        elif isinstance(balance, list) and balance:
            for k in ("available", "availableEquity", "maxAvailable"):
                if balance[0].get(k) is not None:
                    available = float(balance[0][k])
                    break
        return jsonify({"ok": True, "msg": f"Bitget 连接成功，可用余额 {available:.2f} USDT"})
    except Exception as exc:
        return jsonify({"error": f"Bitget 连接失败：{exc}"}), 400



@app.route("/api/binance/balance")
def api_binance_balance():
    """获取币安账户余额和当日盈亏"""
    import binance_scraper
    settings = _normalize_copy_settings(db.get_copy_settings())
    api_key = settings.get("binance_api_key") or ""
    api_secret = settings.get("binance_api_secret") or ""
    if not api_key or not api_secret:
        return jsonify({"error": "币安 API 未配置"}), 400
    try:
        balance_info = binance_scraper.get_binance_futures_balance(api_key, api_secret)
        day_pnl = binance_scraper.get_binance_futures_income_today(api_key, api_secret)
        wallet_balance = balance_info.get("balance", 0)
        available = balance_info.get("available", 0)
        unrealized_pnl = balance_info.get("unrealized_pnl", 0)
        day_pnl_pct = (day_pnl / wallet_balance * 100.0) if wallet_balance > 0 else 0.0
        return jsonify({
            "ok": True,
            "wallet_balance": wallet_balance,
            "available_balance": available,
            "unrealized_pnl": unrealized_pnl,
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "updated_at": int(time.time() * 1000),
        })
    except Exception as exc:
        logger.warning("币安余额查询失败: %s", exc)
        return jsonify({"error": f"查询失败：{exc}"}), 400


@app.route("/api/copy/start", methods=["POST"])
def api_copy_start():
    settings = _normalize_copy_settings(db.get_copy_settings())
    has_bg = bool(settings.get("api_key") and settings.get("api_secret") and settings.get("api_passphrase"))
    has_bn = bool(settings.get("binance_api_key") and settings.get("binance_api_secret"))
    if not has_bg and not has_bn:
        return jsonify({"error": "请至少配置并保存 Bitget 或币安模拟网的 API 密钥"}), 400

    # 检查已启用的币安交易员
    bn_traders_raw = settings.get("binance_traders") or "{}"
    try:
        bn_traders = json.loads(bn_traders_raw) if isinstance(bn_traders_raw, str) else bn_traders_raw
    except Exception:
        bn_traders = {}
    bn_enabled = [
        pid for pid, data in bn_traders.items()
        if isinstance(data, dict) and data.get("copy_enabled") is True
    ]

    if len(bn_enabled) == 0:
        db.set_engine_enabled(True)
        copy_engine.start_engine()
        return jsonify({"ok": True, "msg": "引擎已启动（当前未检测到启用的币安交易员，请添加后启用跟单）"})

    total_cap = float(settings.get("total_capital") or 0)
    if total_cap <= 0:
        db.set_engine_enabled(True)
        copy_engine.start_engine()
        return jsonify({
            "ok": True,
            "msg": f"引擎已启动，但⚠️资金池为0，跟单会全部失败。请填写「总资金」并保存后再试。Binance {len(bn_enabled)}人"
        })

    db.set_engine_enabled(True)
    copy_engine.start_engine()
    return jsonify({"ok": True, "msg": f"跟单引擎已启动，Binance {len(bn_enabled)} 人"})


@app.route("/api/copy/stop", methods=["POST"])
def api_copy_stop():
    db.set_engine_enabled(False)
    copy_engine.stop_engine()
    return jsonify({"ok": True, "msg": "跟单引擎已停止"})


@app.route("/api/copy/orders")
def api_copy_orders():
    page = int(request.args.get("page", "1"))
    page_size = int(request.args.get("page_size", "20"))
    offset = max(page - 1, 0) * page_size
    rows = db.get_copy_orders(limit=page_size, offset=offset)
    
    # 建立 UID -> Nickname 映射
    settings = _normalize_copy_settings(db.get_copy_settings())
    name_map = {}
    bn_raw = settings.get("binance_traders") or {}
    if isinstance(bn_raw, str):
        try: bn_raw = json.loads(bn_raw)
        except: bn_raw = {}
    for pid, info in bn_raw.items():
        if isinstance(info, dict) and info.get("nickname"):
            name_map[str(pid)] = info["nickname"]
            name_map[pid] = info["nickname"]
            
    items = []
    for r in rows:
        d = dict(r)
        uid = str(d.get("trader_uid", ""))
        d["trader_name"] = name_map.get(uid, uid or "-")
        d["platform"] = str(d.get("platform") or "bitget").lower()
        items.append(d)

    return jsonify({"items": items, "page": page, "page_size": page_size})


@app.route("/api/copy/positions")
def api_copy_positions():
    """读取用户自己账户的当前合约持仓，按 Bitget / Binance 分组返回。"""
    settings = _normalize_copy_settings(db.get_copy_settings())
    api_key = settings.get("api_key") or ""
    api_secret = settings.get("api_secret") or ""
    api_passphrase = settings.get("api_passphrase") or ""
    bn_api_key = settings.get("binance_api_key") or ""
    bn_api_secret = settings.get("binance_api_secret") or ""
    account_overview = None
    bitget_error = ""
    binance_error = ""

    def _clean_symbol(symbol: str) -> str:
        s = str(symbol or "").upper()
        for suffix in ("_UMCBL", "_UM", "_DMCBL", "_DM"):
            s = s.replace(suffix, "")
        return s

    def _is_missing(v) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return v.strip() in ("", "-", "null", "None")
        return False

    def _first_non_missing(*vals):
        for v in vals:
            if not _is_missing(v):
                return v
        return None

    # 查找 copy_orders 里 filled 的开仓记录，用于显示"来源交易员"
    open_orders = db.get_copy_orders(limit=1000)
    
    # 建立 UID -> Nickname 映射（币安交易员）
    name_map = {}
    bn_raw = settings.get("binance_traders") or {}
    if isinstance(bn_raw, str):
        try: bn_raw = json.loads(bn_raw)
        except: bn_raw = {}
    for pid, info in bn_raw.items():
        if isinstance(info, dict) and info.get("nickname"):
            name_map[str(pid)] = info["nickname"]
            name_map[pid] = info["nickname"]

    source_maps: dict[str, dict[str, str]] = {
        "bitget": {},
        "binance": {},
    }
    source_uid_maps: dict[str, dict[str, str]] = {
        "bitget": {},
        "binance": {},
    }
    for o in open_orders:
        if o.get("action") == "open" and o.get("status") == "filled":
            symbol = _clean_symbol(o.get("symbol"))
            direction = str(o.get("direction") or "").lower()
            if not symbol or direction not in ("long", "short"):
                continue
            key = f"{symbol}_{direction}"
            uid = str(o.get("trader_uid", "-"))
            platform = str(o.get("platform") or "bitget").lower()
            if platform not in source_maps:
                continue
            if key not in source_uid_maps[platform]:
                source_uid_maps[platform][key] = uid
                source_maps[platform][key] = name_map.get(uid, uid)

    bitget_positions = []
    if api_key and api_secret and api_passphrase:
        try:
            raw = order_executor.get_my_positions(api_key, api_secret, api_passphrase)
        except Exception as exc:
            bitget_error = f"读取 Bitget 持仓失败：{exc}"
            raw = []
        try:
            account_overview = _build_account_overview(api_key, api_secret, api_passphrase)
        except Exception as exc:
            logger.warning("读取账户总览失败：%s", exc)

        for item in raw:
            symbol = _clean_symbol(item.get("symbol") or "-")
            hold_side = str(item.get("holdSide") or "-").lower()
            if hold_side not in ("long", "short"):
                hold_side = "-"
            source_key = f"{symbol}_{hold_side}"
            source = source_maps["bitget"].get(source_key, "-")

            account_leverage = item.get("leverage")
            account_qty = _first_non_missing(
                item.get("total"), item.get("size"), item.get("holdVolume"),
                item.get("available"), item.get("pos"),
            )
            account_margin = _first_non_missing(item.get("marginSize"), item.get("margin"))
            account_pnl = _first_non_missing(
                item.get("unrealizedPL"), item.get("unrealizedPnl"),
                item.get("upl"), item.get("unrealizedProfit"), item.get("profit"),
            )
            account_return_rate = _first_non_missing(
                item.get("unrealizedProfitRate"), item.get("returnRate"),
            )

            bitget_positions.append({
                "platform": "bitget",
                "symbol": symbol,
                "direction": hold_side,
                "leverage": _first_non_missing(account_leverage, "-"),
                "qty": _first_non_missing(account_qty, "-"),
                "open_price": item.get("openPriceAvg") or item.get("openAvgPrice") or "-",
                "margin": _first_non_missing(account_margin, "-"),
                "pnl": _first_non_missing(account_pnl, "-"),
                "return_rate": _first_non_missing(account_return_rate, "-"),
                "source": source,
                "sync_mode": "account",
            })
    else:
        bitget_error = "Bitget API 未配置"

    binance_positions = []
    if bn_api_key and bn_api_secret:
        import binance_executor

        try:
            bn_raw = binance_executor.get_my_positions(bn_api_key, bn_api_secret)
        except Exception as exc:
            binance_error = f"读取 Binance 持仓失败：{exc}"
            bn_raw = []

        for item in bn_raw:
            symbol = _clean_symbol(item.get("symbol") or "-")
            position_amt = _to_float_or_none(item.get("positionAmt"))
            position_side = str(item.get("positionSide") or "").upper()
            if position_side == "BOTH":
                if position_amt and position_amt > 0:
                    direction = "long"
                elif position_amt and position_amt < 0:
                    direction = "short"
                else:
                    direction = "-"
            elif position_side in ("LONG", "SHORT"):
                direction = position_side.lower()
            else:
                direction = "-"

            source_key = f"{symbol}_{direction}"
            source = source_maps["binance"].get(source_key, "-")
            pnl = _first_non_missing(item.get("unRealizedProfit"), item.get("unrealizedProfit"))
            margin = _first_non_missing(
                item.get("isolatedWallet"),
                item.get("positionInitialMargin"),
                item.get("initialMargin"),
            )
            margin_num = _to_float_or_none(margin)
            pnl_num = _to_float_or_none(pnl)
            return_rate = "-"
            if margin_num and margin_num > 0 and pnl_num is not None:
                return_rate = pnl_num / margin_num

            qty = abs(position_amt) if position_amt is not None else "-"
            binance_positions.append({
                "platform": "binance",
                "symbol": symbol,
                "direction": direction,
                "leverage": _first_non_missing(item.get("leverage"), "-"),
                "qty": qty,
                "open_price": item.get("entryPrice") or "-",
                "margin": _first_non_missing(margin, "-"),
                "pnl": _first_non_missing(pnl, "-"),
                "return_rate": return_rate,
                "source": source,
                "sync_mode": "account",
            })

    return jsonify({
        "bitget_items": bitget_positions,
        "binance_items": binance_positions,
        "bitget_error": bitget_error,
        "binance_error": binance_error,
        "account_overview": account_overview,
    })


# ── 启动 ──────────────────────────────────────────────────────────────────────

def _migrate_binance_format():
    """将币安交易员从旧格式迁移，并重新获取真实信息"""
    try:
        import binance_scraper

        settings = db.get_copy_settings()
        bn_traders_raw = settings.get("binance_traders") or "[]"

        try:
            bn_traders_data = json.loads(bn_traders_raw)
        except:
            return

        needs_update = False
        bn_traders_dict = {}

        if isinstance(bn_traders_data, list) and bn_traders_data:
            for pid in bn_traders_data:
                info = binance_scraper.fetch_trader_info(str(pid))
                bn_traders_dict[str(pid)] = {
                    "nickname": info.get("nickname", f"币安交易员_{str(pid)[:8]}"),
                    "follower_count": info.get("follower_count", 0),
                    "copier_pnl": info.get("copier_pnl", 0),
                    "aum": info.get("aum", 0),
                    "total_trades": info.get("total_trades", 0),
                    "avatar": info.get("avatar", ""),
                }
            needs_update = True
            logger.info("币安交易员格式迁移：数组 → 字典，%d 个", len(bn_traders_dict))

        elif isinstance(bn_traders_data, dict) and bn_traders_data:
            for pid, data in bn_traders_data.items():
                old_nickname = data.get("nickname", "")
                if old_nickname.startswith("币安交易员_"):
                    info = binance_scraper.fetch_trader_info(str(pid))
                    bn_traders_dict[str(pid)] = {
                        "nickname": info.get("nickname", old_nickname),
                        "follower_count": info.get("follower_count", 0),
                        "copier_pnl": info.get("copier_pnl", 0),
                        "aum": info.get("aum", 0),
                        "total_trades": info.get("total_trades", 0),
                        "avatar": info.get("avatar", ""),
                    }
                    needs_update = True
                    logger.info("重新获取币安交易员信息: %s → %s", old_nickname, info.get("nickname"))
                else:
                    bn_traders_dict[str(pid)] = data

        if needs_update:
            db.update_copy_settings(binance_traders=json.dumps(bn_traders_dict))
            logger.info("币安交易员信息已更新")
    except Exception as e:
        logger.warning("币安交易员格式迁移失败: %s", e)


def _auto_start_copy_engine():
    """若上次关闭时引擎是启动状态，自动恢复。"""
    settings = _normalize_copy_settings(db.get_copy_settings())
    if settings.get("engine_enabled") and settings.get("api_key") and settings.get("api_secret"):
        copy_engine.start_engine()
        logger.info("跟单引擎已自动恢复")


def _cleanup():
    """优雅退出：清理资源、关闭线程、优化数据库。"""
    logger.info("════════════════════════════════════════════")
    logger.info("系统清理启动 - 正在妥善关闭所有资源…")
    logger.info("════════════════════════════════════════════")
    
    try:
        # 停止跟单引擎
        copy_engine.stop_engine()
        time.sleep(0.5)
        
        # 数据库优化
        logger.info("优化数据库 WAL…")
        try:
            with db.get_conn() as conn:
                conn.execute("PRAGMA optimize")
                conn.commit()
        except Exception as e:
            logger.warning("数据库优化失败: %s", e)
        
        logger.info("系统清理完成，已安全退出")
    except Exception as e:
        logger.error("清理过程中出错: %s", e, exc_info=True)

# 注册退出清理
atexit.register(_cleanup)

# 处理信号
def _signal_handler(signum, frame):
    logger.info("收到信号 %d，开始退出…", signum)
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


_LOCK_FILE = None


def _try_acquire_lock() -> bool:
    """尝试获取单实例锁，成功返回 True，已有实例返回 False"""
    global _LOCK_FILE
    lock_path = os.path.join(os.path.dirname(__file__), ".bitgetfollow.lock")
    try:
        import fcntl
        fd = open(lock_path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _LOCK_FILE = fd
        return True
    except ImportError:
        return True
    except (IOError, OSError):
        return False


def _port_in_use(port: int) -> bool:
    """检测端口是否已有监听服务"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def main():
    port = int(os.getenv("PORT", "8080"))
    url = f"http://127.0.0.1:{port}"

    if not _try_acquire_lock():
        logger.info("已有实例在运行，请直接打开浏览器: %s", url)
        return
    if _port_in_use(port):
        logger.info("端口已占用，请直接打开浏览器: %s", url)
        return

    db.init_db()
    _migrate_plaintext_secrets_out_of_db()

    # 迁移币安交易员格式
    _migrate_binance_format()

    logger.info("启动 Web 仪表盘：%s", url)

    # 启动心跳监控线程
    threading.Thread(target=_heartbeat_monitor, daemon=True).start()

    # 延迟启动跟单引擎
    threading.Timer(3.0, _auto_start_copy_engine).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    except OSError as e:
        if "Address already in use" in str(e) or getattr(e, "errno", 0) == 48:
            logger.info("端口 %d 已被占用，请直接打开浏览器: %s", port, url)
            os._exit(0)
        else:
            raise


if __name__ == "__main__":
    main()
