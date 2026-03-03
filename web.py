"""
BitgetFollow Web 仪表盘
双击启动后在浏览器中操作，无需命令行。
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

from flask import Flask, render_template, request, jsonify, redirect, url_for, make_response

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

# 采集线程状态 - 线程安全
_collector_thread = None  # type: threading.Thread | None
_collector_running = False
_collector_lock = threading.RLock()

# 心跳机制：检测网页是否关闭
_last_heartbeat = time.time()
_heartbeat_lock = threading.Lock()
_config_lock = threading.RLock()
_AUTO_EXIT_ON_HEARTBEAT_LOSS = os.getenv("AUTO_EXIT_ON_HEARTBEAT_LOSS", "0") == "1"

@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()
    return jsonify({"ok": True})

def _heartbeat_monitor():
    """后台监控：如果 60 秒没收到心跳，说明网页已关闭。
    注意：nohup 后台运行时心跳不会发送，因此仅在检测到至少有过一次心跳后才开始计时。
    """
    logger.info("心跳监控启动（首次收到心跳后，60秒无响应将提示网页关闭）")
    _ever_received = False
    while True:
        time.sleep(5)
        with _heartbeat_lock:
            elapsed = time.time() - _last_heartbeat
        # 只有收到过至少一次心跳之后，才启动超时检测
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
    """检查 API Key 是否已配置。"""
    return bool(
        config.BITGET_API_KEY
        and config.BITGET_API_KEY != "your_api_key_here"
        and config.BITGET_SECRET_KEY
        and config.BITGET_PASSPHRASE
    )


def _reload_config():
    """重新加载 .env 文件到 config 模块（线程安全）。"""
    from dotenv import load_dotenv
    with _config_lock:
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
        "follow_ratio_pct": 0.003,
        "max_margin_pct": 0.20,
        "price_tolerance": 0.0002,
        "sl_pct": 0.15,
        "tp_pct": 0.30,
        "enabled_traders": [],
        "binance_traders": {},
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
    # 兼容 dict / list / json 字符串 / 双重序列化字符串
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
    
    # 获取已启用跟单的交易员列表
    settings = db.get_copy_settings()
    enabled_raw = settings.get("enabled_traders") or "[]"
    enabled_traders = json.loads(enabled_raw) if isinstance(enabled_raw, str) else enabled_raw
    
    return render_template(
        "index.html",
        report=report,
        collector_running=_collector_running,
        filter_config=config.FILTER,
        api_configured=_api_configured(),
        enabled_traders=enabled_traders,
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
    resp = make_response(render_template("my_positions.html"))
    # 避免浏览器缓存旧内联脚本，导致前端仍执行过期校验逻辑
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── API 路由 ──────────────────────────────────────────────────────────────────

def _parse_trader_url(text: str):
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
        # 如果之前删除过该交易员，清除黑名单以允许重新添加
        db.clear_deleted(uid)
        collector.init_trader(uid, name or uid)
        # 添加成功后，若采集器未运行则自动启动
        with _collector_lock:
            if not _collector_running and not (_collector_thread and _collector_thread.is_alive()):
                import collector as _col
                _col._running = True
                _collector_running = True
                _collector_thread = threading.Thread(target=_run_collector, daemon=True)
                _collector_thread.start()
                logger.info("添加交易员后自动启动采集器")
        trader = db.get_trader(uid)
        final_name = (trader["nickname"] if trader else None) or name or uid
        return jsonify({"ok": True, "msg": f"已成功添加 {final_name}，数据已同步"})
    except Exception as exc:
        logger.error("添加交易员失败: %s", exc, exc_info=True)
        return jsonify({"error": f"添加失败：{exc}"}), 500


@app.route("/api/add_binance_trader", methods=["POST"])
def api_add_binance_trader():
    """通过 Binance URL 或 Portfolio ID 添加币安交易员到跟单列表"""
    import binance_scraper
    
    url_or_pid = (request.json or {}).get("url", "").strip()
    if not url_or_pid:
        return jsonify({"error": "URL 或 Portfolio ID 不能为空"}), 400
    
    try:
        # 尝试从 URL 提取 portfolio_id
        portfolio_id = binance_scraper.parse_binance_url(url_or_pid) or url_or_pid
        
        # 基础验证：portfolio_id 应该是数字
        if not portfolio_id.isdigit() or len(portfolio_id) < 10:
            return jsonify({
                "error": f"无效的 Portfolio ID: {portfolio_id}\n请使用完整的 URL 或正确的 ID（数字，至少 10 位）"
            }), 400
        
        logger.info("正在添加币安交易员：%s", portfolio_id[:12])
        
        # 获取交易员信息（总是返回至少基础信息）
        info = binance_scraper.fetch_trader_info(portfolio_id)
        if not info:
            logger.error("无法获取币安交易员信息: %s", portfolio_id[:12])
            # 至少提供一个默认条目
            info = {
                "portfolio_id": portfolio_id,
                "nickname": f"币安交易员_{portfolio_id[:8]}",
            }
        
        # 获取当前设置
        settings = db.get_copy_settings()
        bn_traders_raw = settings.get("binance_traders") or "[]"
        
        # 兼容旧格式（数组）和新格式（对象字典）
        try:
            bn_traders_data = json.loads(bn_traders_raw)
            if isinstance(bn_traders_data, list) and bn_traders_data and isinstance(bn_traders_data[0], str):
                # 旧格式：简单数组，转换为新格式
                bn_traders_dict = {pid: {"nickname": f"币安交易员_{pid[:8]}"} for pid in bn_traders_data}
            elif isinstance(bn_traders_data, dict):
                # 新格式：对象字典
                bn_traders_dict = bn_traders_data
            else:
                bn_traders_dict = {}
        except:
            bn_traders_dict = {}
        
        # 避免重复添加
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
                "copy_enabled": True,  # 默认开启跟单
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
        
        # 支持旧格式和新格式
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
    """切换单个交易员的跟单开关（Bitget 或 币安）"""
    data = request.json or {}
    uid = data.get("uid", "").strip()
    source = data.get("source", "bitget")  # "bitget" 或 "binance"
    enabled = data.get("enabled", False)
    
    if not uid:
        return jsonify({"error": "缺少交易员ID"}), 400
    
    settings = db.get_copy_settings()
    
    if source == "bitget":
        # 更新 Bitget 交易员列表
        raw = settings.get("enabled_traders") or "[]"
        enabled_list = json.loads(raw) if isinstance(raw, str) else raw
        
        if enabled and uid not in enabled_list:
            enabled_list.append(uid)
            logger.info("启用 Bitget 跟单: %s", uid[:12])
        elif not enabled and uid in enabled_list:
            enabled_list.remove(uid)
            logger.info("禁用 Bitget 跟单: %s", uid[:12])
        
        db.update_copy_settings(enabled_traders=json.dumps(enabled_list))
        return jsonify({"ok": True, "enabled": enabled, "count": len(enabled_list)})
    
    elif source == "binance":
        # 更新币安交易员的启用状态
        raw = settings.get("binance_traders") or "{}"
        bn_traders = json.loads(raw) if isinstance(raw, str) else raw
        
        if uid in bn_traders:
            bn_traders[uid]["copy_enabled"] = enabled
            db.update_copy_settings(binance_traders=json.dumps(bn_traders))
            logger.info("%s 币安跟单: %s", "启用" if enabled else "禁用", uid[:12])
            return jsonify({"ok": True, "enabled": enabled})
        else:
            return jsonify({"error": "币安交易员不存在"}), 404
    
    return jsonify({"error": "未知来源"}), 400


@app.route("/api/remove_trader", methods=["POST"])
def api_remove_trader():
    uid = request.json.get("uid", "").strip()
    trader = db.get_trader(uid)
    if not trader:
        return jsonify({"error": "交易员不存在"}), 404
    
    logger.info("开始删除交易员 %s (%s)…", uid[:8], trader.get("nickname"))
    
    global _collector_running
    
    # 1. 暂停采集器（防止并发写入冲突）
    with _collector_lock:
        was_running = _collector_running
        if was_running:
            import collector
            collector._running = False
            logger.info("已暂停采集器")
            time.sleep(0.5)  # 等待采集器本轮结束
    
    try:
        # 2. 清理快照
        db.clear_snapshots(uid)
        logger.info("已清理快照")
        
        # 3. 删除交易记录和交易员记录（使用写锁）
        from database import get_conn, _db_write_lock
        with _db_write_lock:
            with get_conn() as conn:
                conn.execute("DELETE FROM trades WHERE trader_uid = ?", (uid,))
                conn.execute("DELETE FROM traders WHERE trader_uid = ?", (uid,))
                conn.commit()
        logger.info("已从 DB 删除交易员和历史订单")
        
        # 4. 从 copy_settings.enabled_traders 中移除该 UID
        with _db_write_lock:
            try:
                settings = db.get_copy_settings()
                raw_enabled = settings.get("enabled_traders") or "[]"
                enabled = json.loads(raw_enabled) if isinstance(raw_enabled, str) else raw_enabled
                if uid in enabled:
                    enabled.remove(uid)
                    db.update_copy_settings(enabled_traders=json.dumps(enabled))
                    logger.info("已从 enabled_traders 中移除该 UID")
            except Exception as exc:
                logger.warning("清理 enabled_traders 失败: %s", exc)
        
        # 5. 标记该交易员为已删除（防止采集器重新创建）
        db.mark_deleted(uid)
        
        # 6. 恢复采集器（如果之前运行过）
        if was_running:
            with _collector_lock:
                import collector
                collector._running = True
                logger.info("已恢复采集器")
        
        logger.info("交易员 %s 已完全删除", uid[:8])
        return jsonify({"ok": True, "msg": f"已删除 {trader['nickname']}"})
    
    except Exception as exc:
        # 异常时恢复采集器
        if was_running:
            with _collector_lock:
                import collector
                collector._running = True
        logger.error("删除交易员失败: %s", exc, exc_info=True)
        return jsonify({"error": f"删除失败：{exc}"}), 500




@app.route("/api/collector/start", methods=["POST"])
def api_collector_start():
    global _collector_thread, _collector_running
    with _collector_lock:
        if _collector_running:
            return jsonify({"error": "采集器已在运行"}), 400
        uids = [t["trader_uid"] for t in db.get_all_traders()]
        if not uids:
            return jsonify({"error": "没有追踪中的交易员"}), 400

        import collector as _col
        _col._running = True
        _collector_running = True
        _collector_thread = threading.Thread(target=_run_collector, daemon=True)
        _collector_thread.start()
        return jsonify({"ok": True, "msg": f"采集器已启动，共追踪 {len(uids)} 人"})


@app.route("/api/collector/stop", methods=["POST"])
def api_collector_stop():
    global _collector_running
    with _collector_lock:
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
        existing = _normalize_copy_settings(db.get_copy_settings())
        api_key = (payload.get("api_key") or "").strip() or config.BITGET_API_KEY or ""
        api_secret = (payload.get("api_secret") or "").strip() or config.BITGET_SECRET_KEY or ""
        api_passphrase = (payload.get("api_passphrase") or "").strip() or config.BITGET_PASSPHRASE or ""
        def _float_or(raw_v, default_v):
            if raw_v is None or raw_v == "":
                return float(default_v)
            try:
                return float(raw_v)
            except Exception:
                return float(default_v)

        total_capital = _float_or(payload.get("total_capital"), existing.get("total_capital", 0.0))
        follow_ratio_pct = _float_or(payload.get("follow_ratio_pct"), existing.get("follow_ratio_pct", 0.003))
        # 兼容手动 API 传入百分数（例如 3 表示 3%）
        if follow_ratio_pct > 1:
            follow_ratio_pct = follow_ratio_pct / 100.0
        follow_ratio_pct = min(max(follow_ratio_pct, 0.0), 1.0)
        max_margin_pct = _float_or(payload.get("max_margin_pct"), existing.get("max_margin_pct", 0.2))
        price_tolerance = _float_or(payload.get("price_tolerance"), existing.get("price_tolerance", 0.0002))
        sl_pct = _float_or(payload.get("sl_pct"), existing.get("sl_pct", 0.15))
        tp_pct = _float_or(payload.get("tp_pct"), existing.get("tp_pct", 0.30))
        incoming_enabled = payload.get("enabled_traders")
        enabled_traders = incoming_enabled if isinstance(incoming_enabled, list) else (existing.get("enabled_traders") or [])
        # 若未传入 binance_traders，保留已有配置，避免被清空
        binance_traders = payload.get("binance_traders")
        if binance_traders is None:
            binance_traders = existing.get("binance_traders") or {}
        elif isinstance(binance_traders, str):
            try:
                binance_traders = json.loads(binance_traders)
            except Exception:
                binance_traders = existing.get("binance_traders") or {}

        # 统一成 dict 结构，兼容旧列表格式
        normalized_bn: dict[str, dict] = {}
        if isinstance(binance_traders, list):
            for pid in binance_traders:
                spid = str(pid).strip()
                if not spid:
                    continue
                normalized_bn[spid] = {
                    "nickname": f"币安交易员_{spid[:8]}",
                    "copy_enabled": True,
                }
        elif isinstance(binance_traders, dict):
            for pid, info in binance_traders.items():
                spid = str(pid).strip()
                if not spid:
                    continue
                row = dict(info) if isinstance(info, dict) else {}
                row["nickname"] = row.get("nickname") or f"币安交易员_{spid[:8]}"
                row["copy_enabled"] = bool(row.get("copy_enabled", True))
                normalized_bn[spid] = row

        # 额外校验：确保选中的 UID 在数据库中真实存在，防止残留或注入
        valid_uids = {t["trader_uid"] for t in db.get_all_traders()}
        clean_enabled = [uid for uid in enabled_traders if uid in valid_uids]
        # 防止旧前端把 enabled_traders 空覆盖
        if not clean_enabled:
            prev_enabled = existing.get("enabled_traders") or []
            if isinstance(prev_enabled, list):
                clean_enabled = [uid for uid in prev_enabled if uid in valid_uids]
        # 防止旧前端把 binance_traders 空覆盖
        if not normalized_bn and isinstance(existing.get("binance_traders"), dict):
            normalized_bn = existing.get("binance_traders")

        db.update_copy_settings(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            total_capital=total_capital,
            follow_ratio_pct=follow_ratio_pct,
            max_margin_pct=max_margin_pct,
            price_tolerance=price_tolerance,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            enabled_traders=json.dumps(clean_enabled),
            binance_traders=json.dumps(normalized_bn, ensure_ascii=False),
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

    # 同时考虑已启用的币安交易员
    bn_traders_raw = settings.get("binance_traders") or "{}"
    try:
        bn_traders = json.loads(bn_traders_raw) if isinstance(bn_traders_raw, str) else bn_traders_raw
    except Exception:
        bn_traders = {}
    bn_enabled = [
        pid for pid, data in bn_traders.items()
        if isinstance(data, dict) and data.get("copy_enabled") is True
    ]

    total_enabled = len(enabled) + len(bn_enabled)
    if total_enabled == 0:
        # 不再阻塞启动，避免旧前端缓存校验与后端状态不一致时误报
        db.set_engine_enabled(True)
        copy_engine.start_engine()
        return jsonify({"ok": True, "msg": "引擎已启动（当前未检测到启用对象，等待配置同步）"})

    # 资金池未配置会导致下单失败（保证金=0），提前提醒
    total_cap = float(settings.get("total_capital") or 0)
    if total_cap <= 0:
        db.set_engine_enabled(True)
        copy_engine.start_engine()
        return jsonify({
            "ok": True,
            "msg": f"引擎已启动，但⚠️资金池为0，跟单会全部失败。请填写「总资金」并保存后再试。Bitget {len(enabled)}人，Binance {len(bn_enabled)}人"
        })

    db.set_engine_enabled(True)
    copy_engine.start_engine()
    return jsonify({"ok": True, "msg": f"跟单引擎已启动，Bitget {len(enabled)} 人，Binance {len(bn_enabled)} 人"})


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
    name_map = {t['trader_uid']: t['nickname'] for t in db.get_all_traders()}
    bn_raw = settings.get("binance_traders") or {}
    if isinstance(bn_raw, str):
        try: bn_raw = json.loads(bn_raw)
        except: bn_raw = {}
    for pid, info in bn_raw.items():
        if isinstance(info, dict) and info.get("nickname"):
            name_map[str(pid)] = info["nickname"]
            name_map[pid] = info["nickname"]
            
    # 注入 trader_name
    items = []
    for r in rows:
        d = dict(r)
        uid = str(d.get("trader_uid", ""))
        d["trader_name"] = name_map.get(uid, uid or "-")
        items.append(d)

    return jsonify({"items": items, "page": page, "page_size": page_size})


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
    
    # 建立 UID -> Nickname 映射
    # 1. Bitget 交易员
    name_map = {t['trader_uid']: t['nickname'] for t in db.get_all_traders()}
    # 2. 币安交易员
    bn_raw = settings.get("binance_traders") or {}
    if isinstance(bn_raw, str):
        try: bn_raw = json.loads(bn_raw)
        except: bn_raw = {}
    for pid, info in bn_raw.items():
        if isinstance(info, dict) and info.get("nickname"):
            name_map[str(pid)] = info["nickname"]
            name_map[pid] = info["nickname"]

    # symbol+direction → 最近一条 filled open 的 trader_name / trader_uid
    # db.get_copy_orders() 已按 timestamp DESC 返回，直接顺序遍历即可拿到“最近一条”。
    source_map: dict[str, str] = {}
    source_uid_map: dict[str, str] = {}
    for o in open_orders:
        if o.get("action") == "open" and o.get("status") == "filled":
            symbol = _clean_symbol(o.get("symbol"))
            direction = str(o.get("direction") or "").lower()
            if not symbol or direction not in ("long", "short"):
                continue
            key = f"{symbol}_{direction}"
            uid = str(o.get("trader_uid", "-"))
            if key not in source_uid_map:
                source_uid_map[key] = uid
                source_map[key] = name_map.get(uid, uid)

    # symbol+direction → 来源实时指标（优先使用 snapshots）
    source_metrics_map: dict[str, dict] = {}
    source_metrics_by_uid_key: dict[str, dict] = {}
    enabled_uids = settings.get("enabled_traders") or []
    if isinstance(enabled_uids, str):
        try:
            enabled_uids = json.loads(enabled_uids)
        except Exception:
            enabled_uids = []
    for uid in enabled_uids:
        uid = str(uid)
        try:
            snaps = db.get_snapshots(uid)
        except Exception:
            snaps = {}
        for snap in snaps.values():
            symbol = _clean_symbol(snap.get("symbol"))
            hold_side = str(snap.get("hold_side") or "").lower()
            if not symbol or hold_side not in ("long", "short"):
                continue
            key = f"{symbol}_{hold_side}"
            ts = int(snap.get("timestamp") or 0)
            metrics = {
                "_ts": ts,
                "leverage": snap.get("leverage"),
                "margin_amount": snap.get("open_amount") or snap.get("margin_amount"),
                "position_size": snap.get("position_size"),
                "unrealized_pnl": snap.get("unrealized_pnl"),
                "return_rate": snap.get("return_rate"),
            }

            prev = source_metrics_map.get(key)
            if not prev or ts >= int(prev.get("_ts", 0)):
                source_metrics_map[key] = metrics

            scoped_key = f"{uid}|{key}"
            prev_scoped = source_metrics_by_uid_key.get(scoped_key)
            if not prev_scoped or ts >= int(prev_scoped.get("_ts", 0)):
                source_metrics_by_uid_key[scoped_key] = metrics

    positions = []
    for item in (raw or []):
        symbol = _clean_symbol(item.get("symbol") or "-")
        hold_side = str(item.get("holdSide") or "-").lower()
        if hold_side not in ("long", "short"):
            hold_side = "-"
        source_key = f"{symbol}_{hold_side}"
        source = source_map.get(source_key, "-")
        source_uid = source_uid_map.get(source_key)
        src_metrics = {}
        if source_uid:
            src_metrics = source_metrics_by_uid_key.get(f"{source_uid}|{source_key}", {})
        if not src_metrics:
            src_metrics = source_metrics_map.get(source_key, {})

        # 关键显示字段优先使用“账户实时持仓”数据，确保和交易平台一致；
        # 只有账户接口缺字段时，才回退到来源快照。
        account_leverage = item.get("leverage")
        account_qty = _first_non_missing(
            item.get("total"),
            item.get("size"),
            item.get("holdVolume"),
            item.get("available"),
            item.get("pos"),
        )
        account_margin = _first_non_missing(item.get("marginSize"), item.get("margin"))
        account_pnl = _first_non_missing(
            item.get("unrealizedPL"),
            item.get("unrealizedPnl"),
            item.get("upl"),
            item.get("unrealizedProfit"),
            item.get("profit"),
        )
        account_return_rate = _first_non_missing(
            item.get("unrealizedProfitRate"),
            item.get("returnRate"),
        )

        source_leverage = src_metrics.get("leverage")
        source_qty = src_metrics.get("position_size")
        source_margin = src_metrics.get("margin_amount")
        source_pnl = src_metrics.get("unrealized_pnl")
        source_return_rate = src_metrics.get("return_rate")

        leverage = _first_non_missing(account_leverage, source_leverage, "-")
        qty = _first_non_missing(account_qty, source_qty, "-")
        margin = _first_non_missing(account_margin, source_margin, "-")
        pnl = _first_non_missing(account_pnl, source_pnl, "-")
        return_rate = _first_non_missing(account_return_rate, source_return_rate, "-")

        used_source_fallback = (
            _is_missing(account_leverage)
            or _is_missing(account_qty)
            or _is_missing(account_margin)
            or _is_missing(account_pnl)
            or _is_missing(account_return_rate)
        ) and bool(src_metrics)

        positions.append({
            "symbol": symbol,
            "direction": hold_side,
            "leverage": leverage,
            "qty": qty,
            "open_price": item.get("openPriceAvg") or item.get("openAvgPrice") or "-",
            "margin": margin,
            "pnl": pnl,
            "return_rate": return_rate,
            "source": source,
            "sync_mode": "mixed" if used_source_fallback else "account",
            "account_pnl": account_pnl if not _is_missing(account_pnl) else None,
            "source_pnl": source_pnl if not _is_missing(source_pnl) else None,
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


def _run_collector():
    global _collector_running
    try:
        collector.run()
    finally:
        _collector_running = False


# ── 启动 ──────────────────────────────────────────────────────────────────────

# ── 启动 ──────────────────────────────────────────────────────────────────────

def _migrate_binance_format():
    """将币安交易员从旧格式迁移，并重新获取真实信息"""
    try:
        settings = db.get_copy_settings()
        bn_traders_raw = settings.get("binance_traders") or "[]"

        try:
            bn_traders_data = json.loads(bn_traders_raw)
        except:
            return

        needs_update = False
        bn_traders_dict = {}

        # 处理旧格式（数组）
        if isinstance(bn_traders_data, list) and bn_traders_data:
            for pid in bn_traders_data:
                # 重新从 API 获取信息
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

        # 处理新格式但昵称是默认值的（需要重新获取）
        elif isinstance(bn_traders_data, dict) and bn_traders_data:
            for pid, data in bn_traders_data.items():
                old_nickname = data.get("nickname", "")
                # 如果昵称是默认值，重新获取
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

    logger.info("启动补全 + 采集，共 %d 名交易员", len(traders))

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
    _collector_thread = threading.Thread(target=_run_collector, daemon=True)
    _collector_thread.start()
    logger.info("采集器已自动启动")


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
        # 1. 停止采集器
        collector._running = False
        time.sleep(0.5)
        
        # 2. 停止跟单引擎
        copy_engine.stop_engine()
        time.sleep(0.5)
        
        # 3. 数据库优化
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
        import fcntl  # Unix/macOS only
        fd = open(lock_path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _LOCK_FILE = fd
        return True
    except ImportError:
        return True  # Windows 无 fcntl
    except (IOError, OSError):
        return False


def _port_in_use(port: int) -> bool:
    """检测端口是否已有监听服务（避免 TIME_WAIT 误判）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def main():
    port = int(os.getenv("PORT", "8080"))
    url = f"http://127.0.0.1:{port}"

    # 防止双击/多击启动：文件锁 + 端口检测
    if not _try_acquire_lock():
        logger.info("已有实例在运行，请直接打开浏览器: %s", url)
        return
    if _port_in_use(port):
        logger.info("端口已占用，请直接打开浏览器: %s", url)
        return

    db.init_db()

    # 迁移币安交易员格式（从数组到字典）
    _migrate_binance_format()

    logger.info("启动 Web 仪表盘：%s", url)

    # 启动心跳监控线程（检测网页关闭并自动退出）
    threading.Thread(target=_heartbeat_monitor, daemon=True).start()

    # 延迟 2 秒启动采集（等 Flask 先就绪）
    threading.Timer(2.0, _auto_start_collector).start()
    threading.Timer(3.0, _auto_start_copy_engine).start()
    # 浏览器打开交给外部启动器（.app/.command）统一处理，避免双击时重复打开多个页面
    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    except OSError as e:
        if "Address already in use" in str(e) or getattr(e, "errno", 0) == 48:
            logger.info("端口 %d 已被占用，请直接打开浏览器: %s", port, url)
            os._exit(0)  # 立即退出，避免 Timer 线程在 1.5 秒后打开多余浏览器
        else:
            raise


if __name__ == "__main__":
    main()
