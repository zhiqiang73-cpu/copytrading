"""
指标计算引擎：基于 trades 表计算所有关键指标。
仅依赖标准库（math / statistics），无需 pandas / numpy。
"""
from __future__ import annotations
import datetime
import logging
import math
import statistics
import time
from collections import Counter, defaultdict
from typing import Any

import config
import database as db

logger = logging.getLogger(__name__)

# ── 核心计算函数 ──────────────────────────────────────────────────────────────

def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t["is_win"]) / len(trades)


def _avg_rr_ratio(trades: list[dict]) -> float:
    wins   = [abs(t["pnl_pct"]) for t in trades if t["is_win"]]
    losses = [abs(t["pnl_pct"]) for t in trades if not t["is_win"]]
    if not wins or not losses:
        return 0.0
    return statistics.mean(wins) / statistics.mean(losses)


def _avg_hold_seconds(trades: list[dict]) -> float:
    durations = [t["hold_duration"] for t in trades if t["hold_duration"] > 0]
    return statistics.mean(durations) if durations else 0.0


def _trade_frequency(trades: list[dict]) -> float:
    if len(trades) < 2:
        return 0.0
    times = sorted(t["close_time"] for t in trades if t["close_time"])
    span_days = (times[-1] - times[0]) / 86400_000
    return len(trades) / span_days if span_days > 0 else 0.0


def _sharpe_ratio(trades: list[dict]) -> float:
    daily: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if not t["close_time"]:
            continue
        day = str(t["close_time"] // 86400_000)
        daily[day].append(t["pnl_pct"])
    if len(daily) < 5:
        return 0.0
    day_pnl = [sum(v) for v in daily.values()]
    mean_pnl = statistics.mean(day_pnl)
    try:
        std_pnl = statistics.stdev(day_pnl)
    except statistics.StatisticsError:
        return 0.0
    if std_pnl == 0:
        return 0.0
    return mean_pnl / std_pnl * math.sqrt(365)


def _max_drawdown(trades: list[dict]) -> float:
    """
    计算历史最大回撤（基于累计净收益曲线）。
    pnl_pct 存的是百分比数字（如 39.76 表示 39.76%），需除以 100。
    """
    sorted_trades = sorted(trades, key=lambda t: t["close_time"] or 0)
    equity = 1.0
    peak   = 1.0
    max_dd = 0.0
    for t in sorted_trades:
        pnl = (t["pnl_pct"] or 0) / 100.0  # 转换为小数
        equity *= (1 + pnl)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calmar_ratio(trades: list[dict], max_dd: float) -> float:
    if max_dd <= 0 or len(trades) < 2:
        return 0.0
    times = sorted(t["close_time"] for t in trades if t["close_time"])
    span_years = (times[-1] - times[0]) / (365 * 86400_000)
    if span_years <= 0:
        return 0.0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    annual_return = total_pnl / span_years
    return annual_return / max_dd


def _max_consecutive(trades: list[dict], is_win_val: int) -> int:
    sorted_trades = sorted(trades, key=lambda t: t["close_time"] or 0)
    max_streak = 0
    streak = 0
    for t in sorted_trades:
        if t["is_win"] == is_win_val:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def _symbol_distribution(trades: list[dict]) -> dict[str, float]:
    cnt = Counter(t["symbol"] for t in trades)
    total = len(trades)
    return {sym: round(n / total * 100, 1) for sym, n in cnt.most_common(10)}


def _leverage_stats(trades: list[dict]) -> dict[str, float]:
    levs = [t["leverage"] for t in trades if t["leverage"] > 0]
    if not levs:
        return {"avg": 0.0, "max": 0}
    return {"avg": round(statistics.mean(levs), 1), "max": max(levs)}


def _direction_split(trades: list[dict]) -> dict[str, int]:
    cnt = Counter(t["direction"] for t in trades)
    return dict(cnt)


def _days_since_last_trade(trades: list[dict]) -> float:
    """距离最近一笔已平仓交易的天数。"""
    times = [t["close_time"] for t in trades if t["close_time"]]
    if not times:
        return 999.0
    latest_ms = max(times)
    return (int(time.time() * 1000) - latest_ms) / 86400_000


def _recent_trades(trades: list[dict], days: int = 7) -> list[dict]:
    """最近 N 天的交易。"""
    cutoff = (int(time.time()) - days * 86400) * 1000
    return [t for t in trades if (t["close_time"] or 0) >= cutoff]


def _total_roi(trades: list[dict]) -> float:
    """累计 ROI（简单累加 pnl_pct，非复利）。"""
    return sum(t["pnl_pct"] for t in trades)


def _recent_roi(trades: list[dict], days: int = 30) -> float:
    """最近 N 天 ROI。"""
    recent = _recent_trades(trades, days)
    return sum(t["pnl_pct"] for t in recent)


# ── 汇总计算 ──────────────────────────────────────────────────────────────────

def compute(trader_uid: str) -> dict[str, Any]:
    trades = db.get_trades(trader_uid)
    trader = db.get_trader(trader_uid)

    if not trades:
        return {
            "trader_uid": trader_uid,
            "nickname":   trader["nickname"] if trader else trader_uid,
            "error":      "无交易数据",
            "follower_count": trader.get("follower_count") if trader else None,
            "copy_trade_days": trader.get("copy_trade_days") if trader else None,
        }

    # 优先使用官方 max_drawdown（来自 Bitget 公开 API），更准确
    official_dd = (trader.get("max_drawdown") or 0) / 100.0 if trader else 0
    max_dd   = official_dd if official_dd > 0 else _max_drawdown(trades)
    sharpe   = _sharpe_ratio(trades)
    calmar   = _calmar_ratio(trades, max_dd)
    win_rate = _win_rate(trades)
    rr_ratio = _avg_rr_ratio(trades)
    ev       = win_rate * rr_ratio - (1 - win_rate)
    hold_h   = _avg_hold_seconds(trades) / 3600
    loss_str = _max_consecutive(trades, 0)
    days_idle= _days_since_last_trade(trades)
    recent7  = _recent_trades(trades, 7)

    result: dict[str, Any] = {
        "trader_uid":        trader_uid,
        "nickname":          trader["nickname"] if trader else trader_uid,
        "total_trades":      len(trades),
        "win_rate":          round(win_rate * 100, 2),
        "avg_rr_ratio":      round(rr_ratio, 3),
        "expected_value":    round(ev, 3),
        "trade_freq":        round(_trade_frequency(trades), 2),
        "avg_hold_h":        round(hold_h, 2),
        "sharpe_ratio":      round(sharpe, 3),
        "max_drawdown_pct":  round(max_dd * 100, 2),
        "calmar_ratio":      round(calmar, 3),
        "max_win_streak":    _max_consecutive(trades, 1),
        "max_loss_streak":   loss_str,
        "symbol_dist":       _symbol_distribution(trades),
        "leverage":          _leverage_stats(trades),
        "direction_split":   _direction_split(trades),
        "days_since_last":   round(days_idle, 1),
        "recent_7d_trades":  len(recent7),
        "recent_7d_roi":     round(_recent_roi(trades, 7), 2),
        "recent_30d_roi":    round(_recent_roi(trades, 30), 2),
        "total_roi":         round(_total_roi(trades), 2),
        "recent_trades":     sorted(trades, key=lambda t: t["close_time"] or 0, reverse=True)[:5],
        # 官方数据
        "roi_official":      trader.get("roi") if trader else None,
        "win_rate_official": trader.get("win_rate") if trader else None,
        "follower_count":    trader.get("follower_count") if trader else None,
        "copy_trade_days":   trader.get("copy_trade_days") if trader else None,
    }

    result["score"] = _score(result)
    return result


def _score(r: dict) -> dict[str, Any]:
    """硬性筛选用于评级；同时提供额外检查给报告展示。"""
    f = config.FILTER
    base_checks = {
        "active_7d":    r["days_since_last"] <= f["active_days"],
        "drawdown_ok":  r["max_drawdown_pct"] <= f["max_drawdown"] * 100,
        "ev_positive":  r["expected_value"] >= f["min_expected_value"],
        "sample_ok":    r["total_trades"] >= f["min_trade_count"],
        "loss_streak":  r["max_loss_streak"] < f["max_loss_streak"],
        "holdable":     r["avg_hold_h"] >= f["min_avg_hold_h"],
    }
    # 软性指标，仅用于展示，不影响评级
    extra_checks = {
        "win_rate_ok":   r["win_rate"] >= 55,          # 胜率 ≥55%
        "rr_ratio_ok":   r["avg_rr_ratio"] >= 1.5,     # 盈亏比 ≥1.5
        "sharpe_ok":     r["sharpe_ratio"] >= 1.0,     # 夏普 ≥1.0
        "calmar_ok":     r["calmar_ratio"] >= 1.5,     # Calmar ≥1.5
        "copy_days_ok":  (r.get("copy_trade_days") or 0) >= 60,  # 跟单天数 ≥60
    }
    labels = {
        "active_7d":   "7天内活跃",
        "drawdown_ok": "回撤<25%",
        "ev_positive": "期望值>0",
        "sample_ok":   f"笔数≥{f['min_trade_count']}",
        "loss_streak": "连亏<5次",
        "holdable":    "持仓>30min",
        "win_rate_ok": "胜率≥55%",
        "rr_ratio_ok": "盈亏比≥1.5",
        "sharpe_ok":   "夏普≥1.0",
        "calmar_ok":   "Calmar≥1.5",
        "copy_days_ok": "跟单天数≥60",
    }
    passed = sum(1 for v in base_checks.values() if v)
    total  = len(base_checks)
    grade  = "PASS" if passed == total else ("WATCH" if passed >= total - 1 else "FAIL")
    return {
        "checks": {**base_checks, **extra_checks},
        "labels": labels,
        "passed": passed,
        "total":  total,
        "grade":  grade,
    }


# ── 每日日报 ──────────────────────────────────────────────────────────────────

def generate_daily_report(trader_uids: list[str]) -> dict[str, Any]:
    """
    生成每日日报：分析所有追踪交易员，给出推荐排名和原因。
    """
    results = []
    for uid in trader_uids:
        r = compute(uid)
        if "error" not in r:
            results.append(r)

    if not results:
        return {"date": _today(), "summary": "暂无数据", "recommendations": [], "alerts": []}

    # 按综合评分排序（通过6项的数量 + 期望值 + Calmar）
    def _rank_score(r):
        passed = r["score"]["passed"]
        ev     = r["expected_value"]
        calmar = r["calmar_ratio"]
        active = 1.0 if r["days_since_last"] <= 1 else (0.5 if r["days_since_last"] <= 3 else 0.0)
        return passed * 10 + ev * 5 + calmar * 2 + active * 3

    ranked = sorted(results, key=_rank_score, reverse=True)

    # 生成推荐建议
    recommendations = []
    for i, r in enumerate(ranked[:5]):
        reasons = []
        grade = r["score"]["grade"]

        # 正面理由
        if r["days_since_last"] <= 1:
            reasons.append("今日有交易，活跃度高")
        elif r["days_since_last"] <= 3:
            reasons.append("近3天有交易")

        if r["max_drawdown_pct"] < 15:
            reasons.append(f"回撤极低仅 {r['max_drawdown_pct']:.1f}%")
        elif r["max_drawdown_pct"] < 25:
            reasons.append(f"回撤控制良好 {r['max_drawdown_pct']:.1f}%")

        if r["calmar_ratio"] >= 2.0:
            reasons.append(f"Calmar={r['calmar_ratio']:.1f} 优秀")
        if r["win_rate"] >= 60:
            reasons.append(f"胜率 {r['win_rate']:.1f}% 较高")
        if r["recent_7d_roi"] > 5:
            reasons.append(f"近7天盈利 +{r['recent_7d_roi']:.1f}%")
        if r["expected_value"] > 0.3:
            reasons.append(f"期望值 {r['expected_value']:.2f} 稳健")

        # 风险提示
        risks = []
        if r["max_loss_streak"] >= 4:
            risks.append(f"连亏过{r['max_loss_streak']}次需警惕")
        if r["leverage"]["avg"] > 20:
            risks.append(f"均杠杆 {r['leverage']['avg']}x 偏高")
        if r["recent_7d_roi"] < -3:
            risks.append(f"近7天亏损 {r['recent_7d_roi']:.1f}%")

        recommendations.append({
            "rank":     i + 1,
            "uid":      r["trader_uid"],
            "name":     r["nickname"],
            "grade":    grade,
            "reasons":  reasons[:3],
            "risks":    risks[:2],
            "key_metrics": {
                "近7天ROI":  f"{r['recent_7d_roi']:+.1f}%",
                "回撤":      f"{r['max_drawdown_pct']:.1f}%",
                "胜率":      f"{r['win_rate']:.1f}%",
                "Calmar":   f"{r['calmar_ratio']:.2f}",
            }
        })

    # 预警
    alerts = []
    for r in results:
        if r["days_since_last"] > 7:
            alerts.append({"name": r["nickname"], "msg": f"超过 {r['days_since_last']:.0f} 天无交易，可能已停止活跃"})
        if r["recent_7d_roi"] < -10:
            alerts.append({"name": r["nickname"], "msg": f"近7天亏损 {r['recent_7d_roi']:.1f}%，请关注风险"})
        if r["max_loss_streak"] >= 5:
            alerts.append({"name": r["nickname"], "msg": f"连续亏损 {r['max_loss_streak']} 次，策略可能失效"})

    pass_count  = sum(1 for r in results if r["score"]["grade"] == "PASS")
    watch_count = sum(1 for r in results if r["score"]["grade"] == "WATCH")

    return {
        "date":            _today(),
        "total_traders":   len(results),
        "pass_count":      pass_count,
        "watch_count":     watch_count,
        "recommendations": recommendations,
        "alerts":          alerts,
        "all_results":     results,
    }


def _today() -> str:
    return datetime.date.today().strftime("%Y年%m月%d日")


def compute_and_log(trader_uid: str):
    # 确保交易员还存在
    if not db.get_trader(trader_uid):
        return
    result = compute(trader_uid)
    if "error" in result:
        logger.info("[指标] %s: %s", trader_uid[:8], result["error"])
        return
    logger.info(
        "[指标] %s | 胜率=%.1f%%  盈亏比=%.2f  夏普=%.2f  回撤=%.1f%%  评级=%s",
        result["nickname"],
        result["win_rate"],
        result["avg_rr_ratio"],
        result["sharpe_ratio"],
        result["max_drawdown_pct"],
        result["score"]["grade"],
    )
