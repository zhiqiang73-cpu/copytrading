"""
报告输出模块：控制台彩色打印 + 纯文本日报文件。
依赖：tabulate, colorama
"""
from __future__ import annotations
import datetime
import os
from typing import Any

from colorama import Fore, Style, init as _colorama_init
from tabulate import tabulate

import analyzer
import database as db

_colorama_init(autoreset=True)


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _grade_color(grade: str) -> str:
    c = {"PASS": Fore.GREEN, "WATCH": Fore.YELLOW, "FAIL": Fore.RED}
    return c.get(grade, "")


def _fmt_pct(v) -> str:
    return f"{v:.2f}%" if v is not None else "N/A"


def _fmt_f(v, decimals=3) -> str:
    return f"{v:.{decimals}f}" if v is not None else "N/A"


def _fmt_h(hours) -> str:
    if hours is None:
        return "N/A"
    if hours < 1:
        return f"{int(hours * 60)}m"
    return f"{hours:.1f}h"


def _check_icon(ok: bool) -> str:
    return f"{Fore.GREEN}✓{Style.RESET_ALL}" if ok else f"{Fore.RED}✗{Style.RESET_ALL}"


# ── 单个交易员报告 ────────────────────────────────────────────────────────────

def print_trader_report(result: dict[str, Any]):
    """在控制台打印单个交易员的完整分析报告。"""
    if "error" in result:
        print(f"{Fore.RED}[{result['trader_uid'][:8]}] {result['error']}{Style.RESET_ALL}")
        return

    score  = result["score"]
    grade  = score["grade"]
    gc     = _grade_color(grade)
    name   = result["nickname"]

    print()
    print(f"{'─' * 60}")
    print(f"  {Fore.CYAN}{name}{Style.RESET_ALL}  [{result['trader_uid'][:12]}]")
    print(f"  评级: {gc}{grade}{Style.RESET_ALL}  ({score['passed']}/{score['total']} 项通过)")
    print(f"{'─' * 60}")

    # 核心指标表格
    rows = [
        ["交易笔数",     result["total_trades"],          ""],
        ["胜率",         _fmt_pct(result["win_rate"]),     _check_icon(score["checks"]["win_rate_ok"])],
        ["平均盈亏比",   _fmt_f(result["avg_rr_ratio"], 2), _check_icon(score["checks"]["rr_ratio_ok"])],
        ["期望值",       _fmt_f(result["expected_value"], 3), ""],
        ["夏普比率",     _fmt_f(result["sharpe_ratio"]),   _check_icon(score["checks"]["sharpe_ok"])],
        ["最大回撤",     _fmt_pct(result["max_drawdown_pct"]), _check_icon(score["checks"]["drawdown_ok"])],
        ["Calmar 比率",  _fmt_f(result["calmar_ratio"]),   _check_icon(score["checks"]["calmar_ok"])],
        ["交易频率",     f"{result['trade_freq']:.2f} 笔/天", ""],
        ["平均持仓",     _fmt_h(result["avg_hold_h"]),     ""],
        ["最大连胜",     result["max_win_streak"],         ""],
        ["最大连亏",     result["max_loss_streak"],        ""],
        ["跟单天数",     result.get("copy_trade_days") or "N/A", _check_icon(score["checks"]["copy_days_ok"])],
        ["粉丝数",       result.get("follower_count") or "N/A", ""],
    ]
    print(tabulate(rows, headers=["指标", "数值", ""], tablefmt="simple"))

    # 杠杆习惯
    lev = result.get("leverage", {})
    print(f"\n  杠杆: 均 {lev.get('avg', 'N/A')}x / 最大 {lev.get('max', 'N/A')}x")

    # 方向偏好
    ds = result.get("direction_split", {})
    if ds:
        parts = "  /  ".join(f"{k.upper()} {v}笔" for k, v in ds.items())
        print(f"  方向: {parts}")

    # 交易对分布（前 5）
    sym = result.get("symbol_dist", {})
    if sym:
        top5 = list(sym.items())[:5]
        sym_str = "  ".join(f"{s}: {p}%" for s, p in top5)
        print(f"  标的: {sym_str}")

    # 筛选详情
    print(f"\n  筛选检查：")
    for k, ok in score["checks"].items():
        print(f"    {_check_icon(ok)} {k}")

    print(f"{'─' * 60}")


# ── 多交易员对比表 ────────────────────────────────────────────────────────────

def print_compare_table(results: list[dict[str, Any]]):
    """打印多个交易员的横向对比表格。"""
    if not results:
        print("暂无数据。")
        return

    valid = [r for r in results if "error" not in r]
    if not valid:
        print("所有交易员均无交易数据。")
        return

    headers = [
        "昵称", "笔数", "胜率%", "盈亏比", "夏普", "回撤%", "Calmar",
        "连亏", "频率/天", "持仓", "评级",
    ]
    rows = []
    for r in sorted(valid, key=lambda x: x["score"]["passed"], reverse=True):
        grade  = r["score"]["grade"]
        gc     = _grade_color(grade)
        rows.append([
            f"{Fore.CYAN}{r['nickname'][:16]}{Style.RESET_ALL}",
            r["total_trades"],
            _fmt_pct(r["win_rate"]),
            _fmt_f(r["avg_rr_ratio"], 2),
            _fmt_f(r["sharpe_ratio"], 2),
            _fmt_pct(r["max_drawdown_pct"]),
            _fmt_f(r["calmar_ratio"], 2),
            r["max_loss_streak"],
            f"{r['trade_freq']:.2f}",
            _fmt_h(r["avg_hold_h"]),
            f"{gc}{grade}{Style.RESET_ALL}",
        ])

    print()
    print(f"{Fore.YELLOW}═══ 交易员对比总览 ═══{Style.RESET_ALL}")
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))


# ── 当前持仓快照表 ────────────────────────────────────────────────────────────

def print_current_positions(trader_uid: str):
    """打印某交易员当前持仓快照（来自 snapshots 表）。"""
    snaps = db.get_snapshots(trader_uid)
    trader = db.get_trader(trader_uid)
    name = trader["nickname"] if trader else trader_uid[:12]

    if not snaps:
        print(f"  {name}：当前无持仓。")
        return

    import time
    now_ms = int(time.time() * 1000)
    rows = []
    for tn, s in snaps.items():
        hold_ms = now_ms - (s.get("open_time") or now_ms)
        hold_h  = hold_ms / 3600_000
        side_c  = Fore.GREEN if s["hold_side"] == "long" else Fore.RED
        rows.append([
            s["symbol"],
            f"{side_c}{s['hold_side'].upper()}{Style.RESET_ALL}",
            f"{s['leverage']}x",
            f"{s['open_price']:.4f}",
            _fmt_h(hold_h),
            s.get("tp_price") or "-",
            s.get("sl_price") or "-",
            tn[:10],
        ])

    print(f"\n  {Fore.CYAN}{name}{Style.RESET_ALL} — 当前持仓 {len(rows)} 个：")
    print(
        tabulate(
            rows,
            headers=["标的", "方向", "杠杆", "开仓价", "已持仓", "止盈", "止损", "tracking_no"],
            tablefmt="simple",
        )
    )


# ── 最近交易记录 ──────────────────────────────────────────────────────────────

def print_recent_trades(trader_uid: str, n: int = 20):
    """打印最近 n 条已完成交易。"""
    trades = db.get_trades(trader_uid, limit=n)
    trader = db.get_trader(trader_uid)
    name = trader["nickname"] if trader else trader_uid[:12]

    if not trades:
        print(f"  {name}：无历史交易记录。")
        return

    rows = []
    for t in trades:
        ct = datetime.datetime.fromtimestamp(t["close_time"] / 1000).strftime("%m-%d %H:%M") if t["close_time"] else "N/A"
        side_c = Fore.GREEN if t["direction"] == "long" else Fore.RED
        pnl_c  = Fore.GREEN if t["is_win"] else Fore.RED
        rows.append([
            ct,
            t["symbol"],
            f"{side_c}{t['direction'].upper()}{Style.RESET_ALL}",
            f"{t['leverage']}x",
            f"{t['open_price']:.4f}",
            f"{t['close_price']:.4f}",
            f"{pnl_c}{t['pnl_pct']*100:+.2f}%{Style.RESET_ALL}",
            _fmt_h((t["hold_duration"] or 0) / 3600),
        ])

    print(f"\n  {Fore.CYAN}{name}{Style.RESET_ALL} — 最近 {len(rows)} 条交易：")
    print(
        tabulate(
            rows,
            headers=["平仓时间", "标的", "方向", "杠杆", "开仓价", "平仓价", "PnL", "持仓时长"],
            tablefmt="simple",
        )
    )


# ── 日报文件 ──────────────────────────────────────────────────────────────────

def save_daily_report(results: list[dict[str, Any]], output_dir: str = "reports"):
    """将分析结果保存为纯文本日报（无 ANSI 颜色）。"""
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.date.today().isoformat()
    path  = os.path.join(output_dir, f"report_{today}.txt")

    lines = [
        f"BitgetFollow 日报 — {today}",
        "=" * 60,
        "",
    ]

    for r in results:
        if "error" in r:
            lines.append(f"[{r['trader_uid'][:8]}] 无数据")
            continue
        score = r["score"]
        lines += [
            f"交易员: {r['nickname']} ({r['trader_uid'][:12]})",
            f"评级:   {score['grade']} ({score['passed']}/{score['total']})",
            f"笔数:   {r['total_trades']}  胜率: {_fmt_pct(r['win_rate'])}  盈亏比: {_fmt_f(r['avg_rr_ratio'], 2)}",
            f"夏普:   {_fmt_f(r['sharpe_ratio'])}  回撤: {_fmt_pct(r['max_drawdown_pct'])}  Calmar: {_fmt_f(r['calmar_ratio'])}",
            f"标的:   {', '.join(f'{s}:{p}%' for s, p in list(r['symbol_dist'].items())[:5])}",
            "",
        ]

    lines.append("=" * 60)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"{Fore.GREEN}日报已保存：{path}{Style.RESET_ALL}")
    return path
