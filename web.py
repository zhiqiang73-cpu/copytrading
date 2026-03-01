"""
BitgetFollow Web 仪表盘
双击启动后在浏览器中操作，无需命令行。
"""
from __future__ import annotations
import logging
import os
import threading
import time
import webbrowser
import json

from flask import Flask, render_template, request, jsonify, redirect, url_for

import analyzer
import api_client
import collector
import config
import copy_engine
import database as db
import order_executor
import scraper

# ── Flask 初始化 ──────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(24)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("web")

# 采集线程状态
_collector_thread: threading.Thread | None = None
_collector_running = False


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _api_configured() -> bool:
    """检查 API Key 是否已配置。"""
    return bool(
        config.BITGET_API_KEY
        and config.BITGET_API_KEY != "your_api_key_here"
        and config.BITGET_SECRET_KEY
        and config.BITGET_PASSPHRASE
    )


def _reload_config():
    """重新加载 .env 文件到 config 模块。"""
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(env_path, override=True)
    config.BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
    config.BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
    config.BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
    config.POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "5"))
    config.LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO")


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


def _normalize_copy_settings(raw: dict) -> dict:
    defaults = {
        "api_key": "",
        "api_secret": "",
        "api_passphrase": "",
        "total_capital": 0.0,
        "max_margin_pct": 0.20,
        "price_tolerance": 0.0002,
        "sl_pct": 0.15,
        "tp_pct": 0.30,
        "enabled_traders": [],
        "engine_enabled": 0,
    }
    if not raw:
        return defaults
    settings = {**defaults, **raw}
    try:
        et = settings.get("enabled_traders") or "[]"
        if isinstance(et, list):
            settings["enabled_traders"] = et
        else:
            settings["enabled_traders"] = json.loads(et)
    except Exception:
        settings["enabled_traders"] = []
    settings["engine_enabled"] = int(settings.get("engine_enabled") or 0)
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
    traders = db.get_all_traders()
    uids = [t["trader_uid"] for t in traders]
    report = analyzer.generate_daily_report(uids)
    return render_template(
        "index.html",
        report=report,
        collector_running=_collector_running,
        filter_config=config.FILTER,
        api_configured=_api_configured(),
    )


@app.route("/api/daily_report")
def api_daily_report():
    uids = [t["trader_uid"] for t in db.get_all_traders()]
    report = analyzer.generate_daily_report(uids)
    return jsonify(report)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    msg = ""
    msg_type = ""
    if request.method == "POST":
        api_key    = request.form.get("api_key", "").strip()
        secret_key = request.form.get("secret_key", "").strip()
        passphrase = request.form.get("passphrase", "").strip()
        poll_interval = request.form.get("poll_interval", "5").strip()

        if not api_key or not secret_key or not passphrase:
            msg = "所有字段都必须填写"
            msg_type = "error"
        else:
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            simulated = os.getenv("BITGET_SIMULATED", "0")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"BITGET_API_KEY={api_key}\n")
                f.write(f"BITGET_SECRET_KEY={secret_key}\n")
                f.write(f"BITGET_PASSPHRASE={passphrase}\n")
                f.write(f"BITGET_SIMULATED={simulated}\n")
                f.write(f"POLL_INTERVAL={poll_interval}\n")
                f.write("LOG_LEVEL=INFO\n")
            _reload_config()
            msg = "API 配置已保存"
            msg_type = "success"

    current = {
        "api_key":    config.BITGET_API_KEY or "",
        "secret_key": config.BITGET_SECRET_KEY or "",
        "passphrase": config.BITGET_PASSPHRASE or "",
        "poll_interval": config.POLL_INTERVAL,
    }
    configured = _api_configured()
    return render_template("settings.html", current=current, configured=configured, msg=msg, msg_type=msg_type)


@app.route("/trader/<trader_uid>")
def trader_detail(trader_uid):
    trader = db.get_trader(trader_uid)
    if not trader:
        return "交易员不存在", 404
    result = analyzer.compute(trader_uid)
    trades = db.get_trades(trader_uid, limit=100)
    snaps_dict = db.get_snapshots(trader_uid)
    snaps  = list(snaps_dict.values())
    now_ms = int(time.time() * 1000)
    return render_template(
        "trader.html",
        trader=trader, result=result, trades=trades,
        snaps=snaps, now_ms=now_ms,
        fmt_ts=_fmt_ts, fmt_h=_fmt_h,
    )


@app.route("/my-positions")
def my_positions():
    return render_template("my_positions.html")


# ── API 路由 ──────────────────────────────────────────────────────────────────

def _parse_trader_url(text: str) -> str | None:
    """
    从 Bitget 交易员个人主页 URL 中提取 traderUid。
    支持格式：
      https://www.bitget.com/copy-trading/trader/{uid}/futures
      https://www.bitget.com/zh-CN/copy-trading/trader/{uid}/futures
      https://www.bitget.com/copytrading/trader?traderUid={uid}
    """
    import re
    # 路径格式: /copy-trading/trader/{uid}/ 或 /copytrading/trader/{uid}/
    m = re.search(r'copy-?trading/trader/([a-zA-Z0-9]+)', text)
    if m:
        return m.group(1)
    # Query 参数格式: traderUid=xxx
    m = re.search(r'traderUid=([a-zA-Z0-9]+)', text)
    if m:
        return m.group(1)
    return None


def _is_leaderboard_url(text: str) -> bool:
    """
    检测是否为 Bitget 排行榜/列表页链接。
    例如：https://www.bitget.com/zh-CN/copy-trading/futures/all?rule=5&sort=0
          https://www.bitget.com/copy-trading/futures/all
    """
    import re
    return bool(re.search(r'copy-?trading/futures/(all|leaderboard|top)', text, re.I)
                or (re.search(r'copy-?trading', text, re.I)
                    and re.search(r'[?&](rule|sort)=\d', text)))


@app.route("/api/batch_scan", methods=["POST"])
def api_batch_scan():
    """
    【V2 新逻辑】从 Bitget 同步当前账号已跟单的交易员列表。
    Bitget V1 公开排行榜 API 已于 2025 年下线，V2 只允许读取
    「你自己已跟单的交易员」数据。
    """
    if not _api_configured():
        return jsonify({"error": "请先配置 API Key"}), 400

    try:
        followed = api_client.get_followed_traders(page=1, page_size=50)
    except Exception as exc:
        return jsonify({"error": f"读取跟单列表失败：{exc}"}), 500

    tracked_uids = {t["trader_uid"] for t in db.get_all_traders()}

    candidates = []
    for r in followed:
        # V2 字段名：traderId / traderName / profitRate / winRate / followerCount 等
        uid  = r.get("traderId") or r.get("traderUid") or ""
        name = r.get("traderName") or r.get("traderNickName") or uid[:12]
        if not uid:
            continue

        win_rate  = float(r.get("winRate") or r.get("averageWinRate") or 0)
        max_dd    = float(r.get("maxDrawdown") or r.get("maxCallbackRate") or 0)
        total     = int(r.get("totalTradeCount") or 0)
        followers = int(r.get("followerCount") or r.get("totalFollowers") or 0)
        roi_val   = float(r.get("profitRate") or 0)
        roi       = f"{roi_val:.1f}%" if roi_val else "N/A"

        already = uid in tracked_uids
        candidates.append({
            "uid":       uid,
            "name":      name,
            "win_rate":  round(win_rate, 1),
            "total":     total,
            "max_dd":    round(max_dd, 1),
            "followers": followers,
            "roi":       roi,
            "already":   already,
        })

    new_count = sum(1 for c in candidates if not c["already"])
    return jsonify({
        "mode":         "followed",
        "total":        len(followed),
        "candidates":   candidates,
        "new_count":    new_count,
    })


@app.route("/api/search", methods=["POST"])
def api_search():
    raw_input = (request.json or {}).get("nickname", "").strip()
    if not raw_input:
        return jsonify({"error": "请输入交易员主页链接"}), 400

    # ① 排行榜链接 → 提示换成个人主页
    if _is_leaderboard_url(raw_input):
        return jsonify({
            "error": (
                "请粘贴【交易员个人主页】链接，格式：\n"
                "https://www.bitget.com/zh-CN/copy-trading/trader/xxxx/futures\n\n"
                "（排行榜链接无法自动处理，需要点进交易员主页后复制链接）"
            )
        }), 400

    # ② 个人主页链接 → 提取 UID 并立即拉取公开数据
    uid_from_url = _parse_trader_url(raw_input)
    if uid_from_url:
        try:
            detail = scraper.fetch_trader_detail(uid_from_url)
        except Exception as exc:
            return jsonify({"error": f"拉取交易员数据失败：{exc}"}), 500
        if not detail:
            return jsonify({"error": "无法获取该交易员数据，请确认链接有效"}), 400
        return jsonify({
            "preview": True,
            "uid": uid_from_url,
            "name": detail.get("name", uid_from_url),
            "win_rate": detail.get("win_rate", 0),
            "roi": detail.get("roi", 0),
            "max_drawdown": detail.get("max_drawdown", 0),
            "total_profit": detail.get("total_profit", 0),
            "follower_count": detail.get("follower_count", 0),
            "aum": detail.get("aum", 0),
            "profit_7d": detail.get("profit_7d", 0),
            "profit_30d": detail.get("profit_30d", 0),
            "avatar": detail.get("avatar", ""),
            "already": uid_from_url in {t["trader_uid"] for t in db.get_all_traders()},
        })

    # ③ 不是链接 → 提示需要主页链接
    return jsonify({
        "error": (
            "请粘贴 Bitget 交易员【个人主页链接】，例如：\n"
            "https://www.bitget.com/zh-CN/copy-trading/trader/b1bc4c7086b23b55ad94/futures\n\n"
            "暂不支持按名称搜索（Bitget V2 API 限制）"
        )
    }), 400


@app.route("/api/add_trader", methods=["POST"])
def api_add_trader():
    global _collector_thread, _collector_running
    uid  = (request.json or {}).get("uid", "").strip()
    name = (request.json or {}).get("name", "").strip()
    if not uid:
        return jsonify({"error": "UID 不能为空"}), 400
    try:
        collector.init_trader(uid, name or uid)
        # 添加成功后，若采集器未运行则自动启动
        if not _collector_running:
            import collector as _col
            uids = [t["trader_uid"] for t in db.get_all_traders()]
            _col._running = True
            _collector_running = True
            _collector_thread = threading.Thread(target=_run_collector, args=(uids,), daemon=True)
            _collector_thread.start()
            logger.info("添加交易员后自动启动采集器")
        trader = db.get_trader(uid)
        final_name = (trader["nickname"] if trader else None) or name or uid
        return jsonify({"ok": True, "msg": f"已成功添加 {final_name}，数据已同步"})
    except Exception as exc:
        logger.error("添加交易员失败: %s", exc, exc_info=True)
        return jsonify({"error": f"添加失败：{exc}"}), 500


@app.route("/api/remove_trader", methods=["POST"])
def api_remove_trader():
    uid = request.json.get("uid", "").strip()
    trader = db.get_trader(uid)
    if not trader:
        return jsonify({"error": "交易员不存在"}), 404
    db.clear_snapshots(uid)
    from database import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE trader_uid = ?", (uid,))
        conn.execute("DELETE FROM traders WHERE trader_uid = ?", (uid,))
        conn.commit()
    # 同步从 copy_settings.enabled_traders 中移除该 UID，否则引擎仍会尝试跟单已删交易员
    try:
        settings = db.get_copy_settings()
        raw_enabled = settings.get("enabled_traders") or "[]"
        enabled = json.loads(raw_enabled) if isinstance(raw_enabled, str) else raw_enabled
        if uid in enabled:
            enabled.remove(uid)
            db.update_copy_settings(enabled_traders=json.dumps(enabled))
    except Exception as exc:
        logger.warning("清理 enabled_traders 失败: %s", exc)
    return jsonify({"ok": True, "msg": f"已删除 {trader['nickname']}"})


@app.route("/api/collector/start", methods=["POST"])
def api_collector_start():
    global _collector_thread, _collector_running
    if _collector_running:
        return jsonify({"error": "采集器已在运行"}), 400
    uids = [t["trader_uid"] for t in db.get_all_traders()]
    if not uids:
        return jsonify({"error": "没有追踪中的交易员"}), 400

    import collector as _col
    _col._running = True
    _collector_running = True
    _collector_thread = threading.Thread(target=_run_collector, args=(uids,), daemon=True)
    _collector_thread.start()
    return jsonify({"ok": True, "msg": f"采集器已启动，追踪 {len(uids)} 人"})


@app.route("/api/collector/stop", methods=["POST"])
def api_collector_stop():
    global _collector_running
    import collector as _col
    _col._running = False
    _collector_running = False
    return jsonify({"ok": True, "msg": "采集器已停止"})


@app.route("/api/status")
def api_status():
    return jsonify({
        "api_configured": _api_configured(),
        "collector_running": _collector_running,
        "copy_engine_running": copy_engine.is_engine_running(),
        "trader_count": len(db.get_all_traders()),
    })


# ── Copy Trading API ──────────────────────────────────────────────────────────

@app.route("/api/copy/settings", methods=["GET", "POST"])
def api_copy_settings():
    if request.method == "POST":
        payload = request.json or {}
        api_key = (payload.get("api_key") or "").strip() or config.BITGET_API_KEY or ""
        api_secret = (payload.get("api_secret") or "").strip() or config.BITGET_SECRET_KEY or ""
        api_passphrase = (payload.get("api_passphrase") or "").strip() or config.BITGET_PASSPHRASE or ""
        total_capital = float(payload.get("total_capital") or 0)
        max_margin_pct = float(payload.get("max_margin_pct") or 0.2)
        price_tolerance = float(payload.get("price_tolerance") or 0.0002)
        sl_pct = float(payload.get("sl_pct") or 0.15)
        tp_pct = float(payload.get("tp_pct") or 0.30)
        enabled_traders = payload.get("enabled_traders") or []

        db.update_copy_settings(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            total_capital=total_capital,
            max_margin_pct=max_margin_pct,
            price_tolerance=price_tolerance,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            enabled_traders=json.dumps(enabled_traders),
        )
        
        # 同步更新运行中的 config 对象
        config.BITGET_API_KEY = api_key
        config.BITGET_SECRET_KEY = api_secret
        config.BITGET_PASSPHRASE = api_passphrase

        # 同步写入 .env，保证重启后不丢失（保留 BITGET_SIMULATED 等现有设置）
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        simulated = os.getenv("BITGET_SIMULATED", "0")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(f"BITGET_API_KEY={api_key}\n")
            f.write(f"BITGET_SECRET_KEY={api_secret}\n")
            f.write(f"BITGET_PASSPHRASE={api_passphrase}\n")
            f.write(f"BITGET_SIMULATED={simulated}\n")
            if os.getenv("BITGET_BASE_URL"):
                f.write(f"BITGET_BASE_URL={os.getenv('BITGET_BASE_URL')}\n")
            f.write(f"POLL_INTERVAL={config.POLL_INTERVAL}\n")
            f.write(f"LOG_LEVEL={config.LOG_LEVEL}\n")

        return jsonify({"ok": True})

    settings = _normalize_copy_settings(db.get_copy_settings())
    # P1#6 密钥脱敏：GET 时不把完整 secret 暴露给前端
    safe_settings = dict(settings)
    for field in ("api_secret", "api_passphrase"):
        val = safe_settings.get(field, "")
        if val and len(val) > 8:
            safe_settings[field] = val[:4] + "****" + val[-4:]
    traders = []
    for t in db.get_all_traders():
        snaps = db.get_snapshots(t["trader_uid"])
        traders.append({
            "uid": t["trader_uid"],
            "name": t.get("nickname") or t["trader_uid"][:10],
            "win_rate": t.get("win_rate") or 0,
            "max_drawdown": t.get("max_drawdown") or 0,
            "roi": t.get("roi") or 0,
            "positions": len(snaps),
            "avatar": t.get("avatar") or "",
        })
    return jsonify({**safe_settings, "traders": traders})


@app.route("/api/copy/test_api", methods=["POST"])
def api_copy_test_api():
    payload = request.json or {}
    api_key = (payload.get("api_key") or "").strip() or config.BITGET_API_KEY or ""
    api_secret = (payload.get("api_secret") or "").strip() or config.BITGET_SECRET_KEY or ""
    api_passphrase = (payload.get("api_passphrase") or "").strip() or config.BITGET_PASSPHRASE or ""
    if not api_key or not api_secret or not api_passphrase:
        return jsonify({"error": "API Key / Secret / Passphrase 不能为空"}), 400
    try:
        # 用 order_executor 测试交易账户余额
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
        return jsonify({"ok": True, "msg": f"连接成功，可用余额 {available:.2f} USDT"})
    except Exception as exc:
        return jsonify({"error": f"连接失败：{exc}"}), 400


@app.route("/api/copy/start", methods=["POST"])
def api_copy_start():
    settings = _normalize_copy_settings(db.get_copy_settings())
    if not settings.get("api_key") or not settings.get("api_secret") or not settings.get("api_passphrase"):
        return jsonify({"error": "请先配置并保存 API Key"}), 400
    enabled = settings.get("enabled_traders") or []
    if isinstance(enabled, str):
        enabled = json.loads(enabled)

    # 过滤掉已被删除的交易员 UID（防止残留 UID 导致计数异常）
    valid_uids = {t["trader_uid"] for t in db.get_all_traders()}
    clean_enabled = [uid for uid in enabled if uid in valid_uids]
    if len(clean_enabled) != len(enabled):
        stale = [uid for uid in enabled if uid not in valid_uids]
        logger.warning("发现 %d 个已删除交易员仍在 enabled_traders 中，已自动清理: %s", len(stale), stale)
        db.update_copy_settings(enabled_traders=json.dumps(clean_enabled))
        enabled = clean_enabled

    if not enabled:
        return jsonify({"error": "请先勾选并保存至少一个跟单对象"}), 400
    db.set_engine_enabled(True)
    copy_engine.start_engine()
    return jsonify({"ok": True, "msg": f"跟单引擎已启动，跟踪 {len(enabled)} 人"})


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
    return jsonify({"items": rows, "page": page, "page_size": page_size})


@app.route("/api/copy/positions")
def api_copy_positions():
    """读取用户自己账户的当前合约持仓。"""
    settings = _normalize_copy_settings(db.get_copy_settings())
    api_key = settings.get("api_key") or ""
    api_secret = settings.get("api_secret") or ""
    api_passphrase = settings.get("api_passphrase") or ""
    if not api_key or not api_secret or not api_passphrase:
        return jsonify({"error": "请先配置 API Key"}), 400
    try:
        raw = order_executor.get_my_positions(api_key, api_secret, api_passphrase)
    except Exception as exc:
        return jsonify({"error": f"读取持仓失败：{exc}"}), 400

    # 查找 copy_orders 里 filled 的开仓记录，用于显示"来源交易员"
    open_orders = db.get_copy_orders(limit=200)
    # symbol+direction → 最近一条 filled open 的 trader_uid
    source_map: dict[str, str] = {}
    for o in reversed(open_orders):
        if o.get("action") == "open" and o.get("status") == "filled":
            key = f"{o['symbol']}_{o['direction']}"
            source_map.setdefault(key, o.get("trader_uid", "-"))

    positions = []
    for item in (raw or []):
        symbol = item.get("symbol") or "-"
        hold_side = item.get("holdSide") or "-"
        source = source_map.get(f"{symbol}_{hold_side}", "-")
        positions.append({
            "symbol": symbol,
            "direction": hold_side,
            "leverage": item.get("leverage") or "-",
            "open_price": item.get("openPriceAvg") or item.get("openAvgPrice") or "-",
            "margin": item.get("marginSize") or item.get("margin") or "-",
            "pnl": item.get("unrealizedPL") or item.get("profit") or "-",
            "return_rate": item.get("achievedProfits") or "-",
            "source": source,
        })
    return jsonify({"items": positions})


@app.route("/api/refresh/<trader_uid>", methods=["POST"])
def api_refresh(trader_uid):
    """手动刷新某个交易员的数据。"""
    trader = db.get_trader(trader_uid)
    if not trader:
        return jsonify({"error": "交易员不存在"}), 404
    try:
        collector.init_trader(trader_uid, trader["nickname"])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _run_collector(uids):
    global _collector_running
    try:
        collector.run(uids)
    finally:
        _collector_running = False


# ── 启动 ──────────────────────────────────────────────────────────────────────

def _auto_start_collector():
    """
    启动后自动开启采集：
    1. 对每个交易员做增量补全（补回关机期间漏掉的数据）
    2. 启动持续采集循环
    """
    global _collector_thread, _collector_running
    traders = db.get_all_traders()
    if not traders:
        logger.info("暂无追踪交易员，等待手动添加后采集")
        return

    uids = [t["trader_uid"] for t in traders]
    logger.info("启动补全 + 采集，共 %d 名交易员", len(uids))

    # 先逐个做增量补全（拉取关机期间新产生的历史订单）
    for t in traders:
        try:
            collector.init_trader(t["trader_uid"], t["nickname"])
        except Exception as exc:
            logger.error("补全失败 [%s]: %s", t["nickname"], exc)

    # 然后启动持续采集循环
    import collector as _col
    _col._running = True
    _collector_running = True
    _collector_thread = threading.Thread(target=_run_collector, args=(uids,), daemon=True)
    _collector_thread.start()
    logger.info("采集器已自动启动，追踪 %d 名交易员", len(uids))


def _auto_start_copy_engine():
    """若上次关闭时引擎是启动状态，自动恢复。"""
    settings = _normalize_copy_settings(db.get_copy_settings())
    if settings.get("engine_enabled") and settings.get("api_key") and settings.get("api_secret"):
        copy_engine.start_engine()
        logger.info("跟单引擎已自动恢复")


def main():
    db.init_db()
    port = int(os.getenv("PORT", "8080"))
    url = f"http://127.0.0.1:{port}"
    logger.info("启动 Web 仪表盘：%s", url)
    # 延迟 2 秒启动采集（等 Flask 先就绪）
    threading.Timer(2.0, _auto_start_collector).start()
    threading.Timer(3.0, _auto_start_copy_engine).start()
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
