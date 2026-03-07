"""
binance_scanner.py — 币安跟单排行榜自动扫描器

通过 Playwright 浏览器自动化扫描排行榜，提取交易员 portfolio_id，
然后调用 detail API 获取详细信息，最后按可调参数评分排序。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

import requests
import binance_scraper

logger = logging.getLogger(__name__)

# ── 扫描状态管理 ──────────────────────────────────────────────────────────

_scan_lock = threading.Lock()
_scan_status: dict[str, Any] = {
    "running": False,
    "phase": "",        # scanning / analyzing / done / error
    "progress": "",
    "total_found": 0,
    "analyzed": 0,
    "results": [],
    "error": "",
    "started_at": 0,
    "finished_at": 0,
}


def get_scan_status() -> dict:
    with _scan_lock:
        return dict(_scan_status)


def _update_status(**kwargs):
    with _scan_lock:
        _scan_status.update(kwargs)


# ── 浏览器扫描排行榜 ──────────────────────────────────────────────────────

def _scan_leaderboard_ids(max_scroll: int = 8) -> list[str]:
    """
    用 Playwright 打开币安跟单排行榜页面，滚动加载，提取所有交易员的 portfolio_id。
    """
    from playwright.sync_api import sync_playwright
    
    portfolio_ids = []
    
    logger.info("启动浏览器扫描币安排行榜…")
    _update_status(phase="scanning", progress="正在启动浏览器…")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = context.new_page()
        
        try:
            _update_status(progress="正在加载排行榜页面…")
            page.goto("https://www.binance.com/zh-CN/copy-trading", wait_until="domcontentloaded", timeout=30000)
            
            # 等待页面内容加载
            time.sleep(5)
            _update_status(progress="页面已加载，开始提取交易员数据…")
            
            # 尝试多种选择器来找到交易员卡片
            seen_ids = set()
            
            for scroll_i in range(max_scroll):
                # 提取当前可见的所有链接中的 portfolio_id
                links = page.evaluate("""() => {
                    const results = [];
                    // 提取所有包含 lead-details 的链接
                    document.querySelectorAll('a[href*="lead-details"]').forEach(a => {
                        const match = a.href.match(/lead-details\\/(\\d+)/);
                        if (match) results.push(match[1]);
                    });
                    // 也尝试从 data 属性提取
                    document.querySelectorAll('[data-portfolio-id]').forEach(el => {
                        results.push(el.getAttribute('data-portfolio-id'));
                    });
                    return [...new Set(results)];
                }""")
                
                for pid in links:
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        portfolio_ids.append(pid)
                
                _update_status(
                    progress=f"第 {scroll_i + 1}/{max_scroll} 次滚动，已发现 {len(portfolio_ids)} 个交易员",
                    total_found=len(portfolio_ids),
                )
                logger.info("滚动 %d/%d，已发现 %d 个交易员", scroll_i + 1, max_scroll, len(portfolio_ids))
                
                # 滚动加载更多
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                
                # 检查是否有"加载更多"按钮
                try:
                    load_more = page.query_selector('button:has-text("查看更多"), button:has-text("加载更多"), button:has-text("Load More")')
                    if load_more:
                        load_more.click()
                        time.sleep(2)
                except Exception:
                    pass
            
        except Exception as exc:
            logger.error("浏览器扫描失败: %s", exc, exc_info=True)
            _update_status(error=f"浏览器扫描失败：{str(exc)[:200]}")
        finally:
            browser.close()
    
    logger.info("浏览器扫描完成，共发现 %d 个交易员", len(portfolio_ids))
    return portfolio_ids


# ── API 获取详情 ──────────────────────────────────────────────────────────

_BN_BASE = "https://www.binance.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "zh-CN",
}


def _safe_float(val, default=0.0) -> float:
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            return float(val.replace("%", "").replace(",", "").strip())
        return default
    except (ValueError, TypeError):
        return default


def fetch_trader_detail(portfolio_id: str) -> Optional[dict]:
    """
    调用已确认可用的 detail GET 接口，获取交易员详细信息。
    返回标准化的字典。
    """
    try:
        url = f"{_BN_BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId={portfolio_id}"
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data")
        if not isinstance(data, dict):
            return None
        
        # 提取标签
        tags = []
        for tag_item in (data.get("tagItemVos") or []):
            tag_name = tag_item.get("tagName", "")
            tag_label = tag_item.get("tagLangKeyMessage", tag_name)
            tags.append({"name": tag_name, "label": tag_label})
        
        return {
            "portfolio_id": portfolio_id,
            "nickname": (data.get("nickname") or f"交易员_{portfolio_id[:8]}").strip(),
            "avatar": data.get("avatarUrl") or "",
            "status": data.get("status", ""),
            "sharp_ratio": _safe_float(data.get("sharpRatio")),
            "copier_pnl": _safe_float(data.get("copierPnl")),
            "aum": _safe_float(data.get("aumAmount")),
            "margin_balance": _safe_float(data.get("marginBalance")),
            "current_copy_count": int(data.get("currentCopyCount") or 0),
            "max_copy_count": int(data.get("maxCopyCount") or 0),
            "total_copy_count": int(data.get("totalCopyCount") or 0),
            "mock_copy_count": int(data.get("mockCopyCount") or 0),
            "close_lead_count": int(data.get("closeLeadCount") or 0),
            "profit_sharing_rate": _safe_float(data.get("profitSharingRate")),
            "badge": data.get("badgeName") or "",
            "tags": tags,
            "description": (data.get("description") or "").strip()[:200],
            "start_time": int(data.get("startTime") or 0),
            "position_show": bool(data.get("positionShow")),
        }
    except Exception as exc:
        logger.warning("获取交易员 %s 详情失败: %s", portfolio_id[:12], exc)
        return None


def analyze_trader_orders(portfolio_id: str) -> dict:
    """
    分析交易员近7天的操作记录，统计胜率、盈亏比等。
    """
    try:
        records = binance_scraper.fetch_operation_records(portfolio_id, page_size=50)
    except Exception as e:
        logger.warning("获取交易员 %s 操作记录失败: %s", portfolio_id[:12], e)
        return {"win_rate": 0, "avg_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0, "avg_rr_ratio": 0}
    
    # 只统计平仓单（有实际盈亏）
    close_orders = [r for r in records if r["action"].startswith("close") and r.get("pnl", 0) != 0]
    
    if not close_orders:
        return {"win_rate": 0, "avg_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0, "avg_rr_ratio": 0}
    
    wins = [o for o in close_orders if o["pnl"] > 0]
    losses = [o for o in close_orders if o["pnl"] < 0]
    
    win_rate = len(wins) / len(close_orders) * 100 if close_orders else 0
    avg_pnl = sum(o["pnl"] for o in close_orders) / len(close_orders) if close_orders else 0
    
    avg_win = sum(o["pnl"] for o in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(o["pnl"] for o in losses) / len(losses)) if losses else 1
    avg_rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    return {
        "win_rate": round(win_rate, 1),
        "avg_pnl": round(avg_pnl, 2),
        "total_trades": len(close_orders),
        "wins": len(wins),
        "losses": len(losses),
        "avg_rr_ratio": round(avg_rr_ratio, 2),
        "total_pnl": round(sum(o["pnl"] for o in close_orders), 2),
    }


# ── 评分排序 ──────────────────────────────────────────────────────────────

DEFAULT_FILTERS = {
    "min_copier_pnl": 0,       # 跟单者收益 > 0
    "min_followers": 10,       # 最低跟单人数
    "min_sharp_ratio": 0,      # 最低夏普比率
    "min_trades": 5,           # 最低交易次数
    "min_win_rate": 0,         # 最低胜率
    "sort_by": "copier_pnl",   # 排序方式
    "max_results": 30,         # 最多返回结果数
}


def score_and_rank(candidates: list[dict], filters: dict = None) -> list[dict]:
    """
    对候选交易员进行评分和排序。
    candidates 中每个 dict 包含 detail + order_stats 字段。
    """
    f = {**DEFAULT_FILTERS, **(filters or {})}
    
    results = []
    for c in candidates:
        # 应用门槛过滤
        passed_filters = {}
        passed_filters["copier_pnl"] = c.get("copier_pnl", 0) >= f["min_copier_pnl"]
        passed_filters["followers"] = c.get("current_copy_count", 0) >= f["min_followers"]
        passed_filters["sharp_ratio"] = c.get("sharp_ratio", 0) >= f["min_sharp_ratio"]
        passed_filters["trades"] = c.get("total_trades_7d", 0) >= f["min_trades"]
        passed_filters["win_rate"] = c.get("win_rate", 0) >= f["min_win_rate"]
        
        passed_count = sum(1 for v in passed_filters.values() if v)
        all_passed = all(passed_filters.values())
        
        # 计算综合得分 (0-100)
        score = 0
        # 跟单者收益 (30分)
        cpnl = c.get("copier_pnl", 0)
        score += min(cpnl / 50000 * 30, 30) if cpnl > 0 else 0
        # 夏普比率 (25分)
        sr = c.get("sharp_ratio", 0)
        score += min(sr / 3 * 25, 25) if sr > 0 else 0
        # 胜率 (20分)
        wr = c.get("win_rate", 0)
        score += min((wr - 40) / 30 * 20, 20) if wr > 40 else 0
        # 盈亏比 (15分)
        rr = c.get("avg_rr_ratio", 0)
        score += min(rr / 2 * 15, 15) if rr > 0 else 0
        # 跟单人数 (10分)
        fc = c.get("current_copy_count", 0)
        score += min(fc / 200 * 10, 10) if fc > 0 else 0
        
        c["score"] = round(score, 1)
        c["passed_filters"] = passed_filters
        c["passed_count"] = passed_count
        c["all_passed"] = all_passed
        
        results.append(c)
    
    # 排序
    sort_key = f.get("sort_by", "copier_pnl")
    if sort_key == "score":
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
    elif sort_key == "copier_pnl":
        results.sort(key=lambda x: x.get("copier_pnl", 0), reverse=True)
    elif sort_key == "sharp_ratio":
        results.sort(key=lambda x: x.get("sharp_ratio", 0), reverse=True)
    elif sort_key == "win_rate":
        results.sort(key=lambda x: x.get("win_rate", 0), reverse=True)
    elif sort_key == "followers":
        results.sort(key=lambda x: x.get("current_copy_count", 0), reverse=True)
    elif sort_key == "aum":
        results.sort(key=lambda x: x.get("aum", 0), reverse=True)
    else:
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
    
    max_r = int(f.get("max_results", 30))
    return results[:max_r]


# ── 主扫描流程 ──────────────────────────────────────────────────────────

def start_scan(filters: dict = None, max_scroll: int = 8) -> None:
    """
    在后台线程启动扫描。
    """
    with _scan_lock:
        if _scan_status["running"]:
            return
        _scan_status.update({
            "running": True,
            "phase": "scanning",
            "progress": "初始化中…",
            "total_found": 0,
            "analyzed": 0,
            "results": [],
            "error": "",
            "started_at": int(time.time()),
            "finished_at": 0,
        })
    
    t = threading.Thread(target=_run_scan, args=(filters or {}, max_scroll), daemon=True)
    t.start()


def _run_scan(filters: dict, max_scroll: int):
    """后台执行完整扫描流程。"""
    try:
        # 第一步：浏览器扫描排行榜
        portfolio_ids = _scan_leaderboard_ids(max_scroll=max_scroll)
        
        if not portfolio_ids:
            _update_status(
                running=False,
                phase="error",
                error="未能从排行榜中提取到任何交易员。可能是页面结构变化或网络问题。",
                finished_at=int(time.time()),
            )
            return
        
        _update_status(
            phase="analyzing",
            total_found=len(portfolio_ids),
            progress=f"共发现 {len(portfolio_ids)} 个交易员，开始逐个分析…",
        )
        
        # 第二步：逐个获取详情 + 分析操作记录
        candidates = []
        for i, pid in enumerate(portfolio_ids):
            _update_status(
                analyzed=i,
                progress=f"正在分析第 {i + 1}/{len(portfolio_ids)} 个交易员…",
            )
            
            # 获取详情
            detail = fetch_trader_detail(pid)
            if not detail:
                continue
            
            # 跳过非活跃的
            if detail.get("status") != "ACTIVE":
                continue
            
            # 分析操作记录
            order_stats = analyze_trader_orders(pid)
            
            # 合并数据
            trader = {
                **detail,
                "win_rate": order_stats.get("win_rate", 0),
                "avg_pnl": order_stats.get("avg_pnl", 0),
                "total_trades_7d": order_stats.get("total_trades", 0),
                "wins_7d": order_stats.get("wins", 0),
                "losses_7d": order_stats.get("losses", 0),
                "avg_rr_ratio": order_stats.get("avg_rr_ratio", 0),
                "total_pnl_7d": order_stats.get("total_pnl", 0),
            }
            candidates.append(trader)
            
            # 每分析完一批就限速（避免被封IP）
            if (i + 1) % 5 == 0:
                time.sleep(1)
        
        _update_status(
            analyzed=len(portfolio_ids),
            progress=f"分析完成，共 {len(candidates)} 个活跃交易员，正在评分排序…",
        )
        
        # 第三步：评分排序
        ranked = score_and_rank(candidates, filters)
        
        _update_status(
            running=False,
            phase="done",
            progress=f"扫描完成！共分析 {len(portfolio_ids)} 人，{len(candidates)} 人活跃，推荐 {len(ranked)} 人",
            results=ranked,
            finished_at=int(time.time()),
        )
        logger.info("扫描完成：%d 发现 → %d 活跃 → %d 推荐", len(portfolio_ids), len(candidates), len(ranked))
    
    except Exception as exc:
        logger.error("扫描流程异常: %s", exc, exc_info=True)
        _update_status(
            running=False,
            phase="error",
            error=f"扫描异常：{str(exc)[:300]}",
            finished_at=int(time.time()),
        )


def stop_scan():
    """停止扫描（标记状态）"""
    _update_status(running=False, phase="stopped", progress="用户手动停止")
