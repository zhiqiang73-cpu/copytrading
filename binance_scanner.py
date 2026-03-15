from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests
import config

logger = logging.getLogger(__name__)

# Hard filters
HARD_MIN_FOLLOWERS = 100
HARD_MIN_DAYS = 90
HARD_MIN_AUM = 50_000
HARD_MIN_COPIER_PNL = 0
HARD_MIN_AVG_HOLD_H = 4

# Score weights
SCORE_W_STABILITY = 25
SCORE_W_DRAWDOWN = 25
SCORE_W_COPIER_PNL = 15
SCORE_W_FOLLOWERS = 10
SCORE_W_ACTIVITY = 25

# Selection buckets
MAX_ELITE = 6
CORE_COUNT = 2
ENHANCE_COUNT = 2
OBSERVE_COUNT = 2

_SCAN_STATE: dict = {
    "running": False,
    "phase": "idle",
    "progress": "",
    "total_found": 0,
    "analyzed": 0,
    "passed_round1": 0,
    "results": [],
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_SCAN_LOCK = threading.Lock()
_SCAN_THREAD: Optional[threading.Thread] = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "zh-CN",
}

_LEADERBOARD_PAGE_SIZE = 18
_LEADERBOARD_ENDPOINT = (
    "https://www.binance.com"
    "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
)
_LEADERBOARD_DAILY_PICKS_ENDPOINT = (
    "https://www.binance.com"
    "/bapi/futures/v1/friendly/future/copy-trade/home-page/daily-picks"
)
_LEGACY_LEADERBOARD_ENDPOINT = (
    "https://www.binance.com"
    "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/list"
)
_MS_PER_HOUR = 3600 * 1000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_RECENT_ACTIVITY_PAGE_SIZE = 100


def _resolve_active_days(filters: dict | None = None) -> int:
    filters = filters or {}
    value = filters.get("active_days", config.FILTER.get("active_days", 7))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 7


def _summarize_recent_activity(
    records: list[dict],
    *,
    now_ms: int | None = None,
    active_days: int = 7,
) -> dict:
    now_ms = int(now_ms or time.time() * 1000)
    active_days = max(1, int(active_days))
    active_window_start = now_ms - active_days * _MS_PER_DAY
    day_1_start = now_ms - _MS_PER_DAY
    day_7_start = now_ms - 7 * _MS_PER_DAY

    order_times = sorted(
        int(item.get("order_time") or 0)
        for item in records
        if int(item.get("order_time") or 0) > 0
    )
    last_trade_time = order_times[-1] if order_times else 0
    recent_trade_count_24h = sum(1 for ts in order_times if ts >= day_1_start)
    recent_trade_count_7d = sum(1 for ts in order_times if ts >= day_7_start)
    recent_trade_count_window = sum(1 for ts in order_times if ts >= active_window_start)
    is_recently_active = bool(last_trade_time and last_trade_time >= active_window_start)
    last_trade_age_hours = ((now_ms - last_trade_time) / _MS_PER_HOUR) if last_trade_time else None

    recency_score = 0.0
    if last_trade_time:
        age_days = max(0.0, (now_ms - last_trade_time) / _MS_PER_DAY)
        recency_score = max(0.0, 1.0 - min(age_days / active_days, 1.0))

    window_target = max(1.0, float(active_days))
    day_1_target = 2.0
    window_count_score = min(1.0, recent_trade_count_window / window_target)
    day_1_score = min(1.0, recent_trade_count_24h / day_1_target)
    activity_ratio = recency_score * 0.55 + window_count_score * 0.30 + day_1_score * 0.15

    return {
        "active_days_required": active_days,
        "is_recently_active": is_recently_active,
        "last_trade_time": last_trade_time or None,
        "last_trade_age_hours": round(last_trade_age_hours, 1) if last_trade_age_hours is not None else None,
        "recent_trade_count_24h": recent_trade_count_24h,
        "recent_trade_count_7d": recent_trade_count_7d,
        "recent_trade_count_window": recent_trade_count_window,
        "activity_score_ratio": round(activity_ratio, 4),
        "activity_score": round(activity_ratio * 100, 1),
    }


def _load_recent_activity_snapshot(portfolio_id: str, filters: dict | None = None) -> dict:
    import binance_scraper

    active_days = _resolve_active_days(filters)
    now_ms = int(time.time() * 1000)
    lookback_days = max(active_days, 7)
    records, error = binance_scraper.fetch_operation_records_with_status(
        portfolio_id,
        page_size=_RECENT_ACTIVITY_PAGE_SIZE,
        start_ms=now_ms - lookback_days * _MS_PER_DAY,
        end_ms=now_ms,
    )
    snapshot = _summarize_recent_activity(records, now_ms=now_ms, active_days=active_days)
    snapshot["activity_fetch_error"] = error
    snapshot["activity_verified"] = error is None
    return snapshot


def get_scan_status() -> dict:
    with _SCAN_LOCK:
        return dict(_SCAN_STATE)


def stop_scan():
    with _SCAN_LOCK:
        if _SCAN_STATE["running"]:
            _SCAN_STATE["running"] = False
            logger.info("精选扫描已停止")


def start_scan(filters: dict, max_scroll: int = 5):
    global _SCAN_THREAD
    with _SCAN_LOCK:
        if _SCAN_STATE["running"]:
            return
        _SCAN_STATE.update(
            {
                "running": True,
                "phase": "scanning",
                "progress": "启动精选扫描...",
                "total_found": 0,
                "analyzed": 0,
                "passed_round1": 0,
                "results": [],
                "error": None,
                "started_at": int(time.time() * 1000),
                "finished_at": None,
            }
        )
    _SCAN_THREAD = threading.Thread(
        target=_scan_worker,
        args=(filters, max_scroll),
        daemon=True,
    )
    _SCAN_THREAD.start()
    logger.info("精选扫描线程已启动")


def _scan_worker(filters: dict, max_scroll: int):
    try:
        _update_state(phase="scanning", progress="正在加载币安排行榜...")
        raw = _fetch_leaderboard(max_scroll, filters=filters)

        if not _SCAN_STATE["running"]:
            return

        _update_state(
            total_found=len(raw),
            progress=f"排行榜共 {len(raw)} 人，开始第一轮硬性筛选...",
        )

        round1 = _hard_filter(raw, filters)
        _update_state(
            passed_round1=len(round1),
            progress=f"第一轮通过 {len(round1)} 人，开始深度分析...",
        )

        if not round1 or not _SCAN_STATE["running"]:
            _finish(round1[:MAX_ELITE])
            return

        _update_state(phase="analyzing")
        scored = _score_with_details(round1, filters=filters)

        if not _SCAN_STATE["running"]:
            return

        elite = _assign_tiers(scored)
        _finish(elite)

    except Exception as exc:
        logger.error("精选扫描失败: %s", exc, exc_info=True)
        with _SCAN_LOCK:
            _SCAN_STATE.update(
                {
                    "running": False,
                    "phase": "error",
                    "error": str(exc)[:300],
                    "finished_at": int(time.time() * 1000),
                }
            )


def _hard_filter(candidates: list[dict], filters: dict) -> list[dict]:
    min_followers = int(filters.get("min_followers", HARD_MIN_FOLLOWERS))
    min_pnl = float(filters.get("min_copier_pnl", HARD_MIN_COPIER_PNL))
    min_aum = float(filters.get("min_aum", HARD_MIN_AUM))
    min_win_rate = float(filters.get("min_win_rate", 0))
    min_trades = int(filters.get("min_trades", 0))

    passed = []
    for trader in candidates:
        if trader.get("copier_pnl", 0) < min_pnl:
            continue
        if trader.get("follower_count", 0) < min_followers:
            continue
        if trader.get("aum", 0) < min_aum:
            continue
        if trader.get("win_rate", 0) < min_win_rate:
            continue
        if trader.get("total_trades", 0) and trader["total_trades"] < min_trades:
            continue
        days = trader.get("copy_days", 0)
        if days and days < HARD_MIN_DAYS:
            continue
        passed.append(trader)

    logger.info("第一轮筛选：%d -> %d", len(candidates), len(passed))
    return passed


def _score_with_details(candidates: list[dict], filters: dict | None = None) -> list[dict]:
    import binance_scraper

    filters = filters or {}
    min_win_rate = float(filters.get("min_win_rate", 0))
    min_trades = int(filters.get("min_trades", 0))
    active_days = _resolve_active_days(filters)

    scored = []
    total = len(candidates)

    for idx, trader in enumerate(candidates):
        if not _SCAN_STATE["running"]:
            break

        pid = trader["portfolio_id"]
        _update_state(
            analyzed=idx + 1,
            progress=f"深度分析 {idx + 1}/{total}: {trader['nickname']}",
        )

        try:
            detail = binance_scraper.fetch_trader_info(pid)
            trader.update(
                {
                    "copy_days": int(detail.get("copy_trade_days") or trader.get("copy_days") or 0),
                    "total_trades": int(detail.get("total_trades") or trader.get("total_trades") or 0),
                    "margin_balance": float(detail.get("margin_balance") or 0),
                    "aum": float(detail.get("aum") or trader.get("aum") or 0),
                    "follower_count": int(detail.get("follower_count") or trader.get("follower_count") or 0),
                    "copier_pnl": float(detail.get("copier_pnl") or trader.get("copier_pnl") or 0),
                    "win_rate": float(detail.get("win_rate") or trader.get("win_rate") or 0),
                }
            )
        except Exception as exc:
            logger.debug("拉取 %s 详情失败，继续使用列表数据: %s", pid[:12], exc)

        activity = _load_recent_activity_snapshot(pid, filters=filters)
        trader.update(activity)
        if activity.get("activity_fetch_error"):
            logger.warning("skip trader=%s activity unavailable: %s", pid[:12], activity["activity_fetch_error"])
            continue
        if not activity.get("is_recently_active"):
            logger.info("skip trader=%s inactive for %d days", pid[:12], active_days)
            continue

        if trader.get("copy_days", 0) and trader["copy_days"] < HARD_MIN_DAYS:
            continue
        if trader.get("total_trades", 0) < max(min_trades, 20):
            continue
        if trader.get("win_rate", 0) < min_win_rate:
            continue

        trader["score"] = _calculate_score(trader)
        scored.append(trader)
        time.sleep(0.5)

    scored.sort(key=lambda item: item["score"], reverse=True)
    logger.info("第二轮评分完成，共 %d 人进入排序", len(scored))
    return scored


def _calculate_score(trader: dict) -> float:
    score = 0.0

    copy_days = max(int(trader.get("copy_days") or 0), 1)
    total_trades = int(trader.get("total_trades") or 0)
    daily_freq = total_trades / copy_days if copy_days > 0 else 0

    days_score = min(1.0, copy_days / 365)
    freq_score = max(0.0, 1.0 - abs(daily_freq - 4) / 6)
    score += SCORE_W_STABILITY * (days_score * 0.6 + freq_score * 0.4)

    aum = max(float(trader.get("aum") or 0), 1.0)
    copier_pnl = float(trader.get("copier_pnl") or 0)
    efficiency = copier_pnl / aum
    drawdown_score = min(1.0, max(0.0, efficiency / 0.5))
    score += SCORE_W_DRAWDOWN * drawdown_score

    pnl_score = min(1.0, copier_pnl / 500_000)
    score += SCORE_W_COPIER_PNL * pnl_score

    followers = int(trader.get("follower_count") or 0)
    follow_score = min(1.0, followers / 300)
    score += SCORE_W_FOLLOWERS * follow_score

    activity_score = float(trader.get("activity_score_ratio") or 0.0)
    score += SCORE_W_ACTIVITY * activity_score

    return round(score, 2)


def _assign_tiers(scored: list[dict]) -> list[dict]:
    elite = scored[:MAX_ELITE]

    tier_map = {}
    for index, trader in enumerate(elite):
        if index < CORE_COUNT:
            tier_map[trader["portfolio_id"]] = ("core", "核心", 0.30)
        elif index < CORE_COUNT + ENHANCE_COUNT:
            tier_map[trader["portfolio_id"]] = ("enhance", "增强", 0.15)
        else:
            tier_map[trader["portfolio_id"]] = ("observe", "观察", 0.05)

    for trader in elite:
        tier_key, tier_label, follow_ratio = tier_map[trader["portfolio_id"]]
        trader["tier"] = tier_key
        trader["tier_label"] = tier_label
        trader["follow_ratio"] = follow_ratio

    logger.info(
        "精选完成：%s",
        " | ".join(f"{item['nickname']}({item['tier_label']})" for item in elite),
    )
    return elite


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_leaderboard_item(item: dict) -> dict | None:
    pid = str(item.get("leadPortfolioId") or item.get("portfolioId") or "").strip()
    if not pid:
        return None

    return {
        "portfolio_id": pid,
        "nickname": (item.get("nickname") or f"交易员_{pid[:8]}").strip(),
        "copier_pnl": _safe_float(item.get("pnl", item.get("copierPnl"))),
        "follower_count": _safe_int(item.get("currentCopyCount", item.get("followerCount"))),
        "aum": _safe_float(item.get("aum", item.get("aumAmount"))),
        "win_rate": _safe_float(item.get("winRate")),
        "roi": _safe_float(item.get("roi")),
        "copy_days": _safe_int(item.get("copyTradingDays", item.get("copyDays"))),
        "total_trades": _safe_int(item.get("closeLeadCount", item.get("totalTrades"))),
        "avatar": item.get("avatarUrl") or "",
        "sharp_ratio": _safe_float(item.get("sharpRatio")),
        "max_copy_count": _safe_int(item.get("maxCopyCount")),
    }


def _build_query_list_payload(page: int, filters: dict | None = None) -> dict:
    filters = filters or {}
    sort_by = str(filters.get("sort_by") or "").strip().lower()
    data_type = "PNL"
    if sort_by in {"roi", "return", "yield"}:
        data_type = "ROI"
    elif sort_by in {"win_rate", "winrate"}:
        data_type = "WIN_RATE"

    return {
        "pageNumber": page,
        "pageSize": _LEADERBOARD_PAGE_SIZE,
        "timeRange": "30D",
        "dataType": data_type,
        "favoriteOnly": False,
        "hideFull": False,
        "nickname": "",
        "order": "DESC",
        "userAsset": 0,
        "portfolioType": "ALL",
        "useAiRecommended": True,
    }


def _extract_query_list_items(data: dict) -> list[dict]:
    if data.get("code") != "000000":
        logger.warning(
            "新排行榜接口返回异常: code=%s msg=%s",
            data.get("code"),
            data.get("message") or data.get("msg"),
        )
        return []
    return list(((data.get("data") or {}).get("list") or []))


def _fetch_query_list_page(page: int, filters: dict | None = None) -> list[dict]:
    resp = requests.post(
        _LEADERBOARD_ENDPOINT,
        json=_build_query_list_payload(page, filters=filters),
        headers=_HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    return _extract_query_list_items(resp.json())


def _fetch_legacy_list_page(page: int) -> list[dict]:
    resp = requests.post(
        _LEGACY_LEADERBOARD_ENDPOINT,
        json={
            "pageSize": _LEADERBOARD_PAGE_SIZE,
            "pageNumber": page,
            "sortBy": "copierPnl",
            "sortType": "desc",
        },
        headers=_HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "000000":
        logger.warning(
            "旧排行榜接口返回异常: code=%s msg=%s",
            data.get("code"),
            data.get("message") or data.get("msg"),
        )
        return []
    return list(((data.get("data") or {}).get("data") or []))


def _fetch_daily_picks() -> list[dict]:
    resp = requests.get(
        _LEADERBOARD_DAILY_PICKS_ENDPOINT,
        headers=_HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "000000":
        logger.warning(
            "每日推荐接口返回异常: code=%s msg=%s",
            data.get("code"),
            data.get("message") or data.get("msg"),
        )
        return []
    return list(((data.get("data") or {}).get("list") or []))


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in candidates:
        pid = str(item.get("portfolio_id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        result.append(item)
    return result


def _fetch_leaderboard(max_scroll: int, filters: dict | None = None) -> list[dict]:
    candidates: list[dict] = []
    page = 1

    while page <= max_scroll and _SCAN_STATE["running"]:
        _update_state(progress=f"加载排行榜第 {page}/{max_scroll} 页...")
        try:
            items = _fetch_query_list_page(page, filters=filters)
        except Exception as exc:
            logger.warning("新排行榜接口第 %d 页失败，尝试旧接口: %s", page, exc)
            try:
                items = _fetch_legacy_list_page(page)
            except Exception as legacy_exc:
                logger.error("排行榜第 %d 页抓取失败: %s", page, legacy_exc)
                break

        if not items:
            break

        page_candidates = []
        for item in items:
            normalized = _normalize_leaderboard_item(item)
            if normalized:
                page_candidates.append(normalized)

        if not page_candidates:
            break

        candidates.extend(page_candidates)
        logger.debug("排行榜第 %d 页：%d 人", page, len(page_candidates))
        page += 1
        time.sleep(1)

    candidates = _dedupe_candidates(candidates)
    if not candidates:
        try:
            candidates = _dedupe_candidates(
                [
                    normalized
                    for normalized in (_normalize_leaderboard_item(item) for item in _fetch_daily_picks())
                    if normalized
                ]
            )
            if candidates:
                logger.info("排行榜主接口为空，已退回每日推荐数据: %d 人", len(candidates))
        except Exception as exc:
            logger.warning("每日推荐接口也失败: %s", exc)

    logger.info("排行榜采集完成：共 %d 人", len(candidates))
    return candidates


def _update_state(**kwargs):
    with _SCAN_LOCK:
        _SCAN_STATE.update(kwargs)


def _finish(results: list[dict]):
    with _SCAN_LOCK:
        _SCAN_STATE.update(
            {
                "running": False,
                "phase": "done",
                "results": results,
                "finished_at": int(time.time() * 1000),
                "progress": f"精选完成，推荐 {len(results)} 位交易员",
            }
        )
