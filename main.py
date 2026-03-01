"""
BitgetFollow — 聪明钱追踪系统 CLI 入口

用法：
    python main.py search  <昵称>         # 搜索并添加交易员
    python main.py list                   # 列出所有已追踪交易员
    python main.py analyze [uid|all]      # 计算并打印分析报告
    python main.py positions [uid|all]    # 查看当前持仓快照
    python main.py trades   <uid> [n]     # 查看最近 n 条历史交易（默认 20）
    python main.py report   [uid|all]     # 生成并保存日报文件
    python main.py run      [uid|all]     # 启动持续采集循环（Ctrl+C 退出）
    python main.py remove   <uid>         # 停止追踪某交易员
"""
from __future__ import annotations
import logging
import sys

from colorama import Fore, Style, init as _colorama_init

import analyzer
import api_client
import collector
import config
import database as db
import reporter
import trade_detector

_colorama_init(autoreset=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _require_key():
    if not config.BITGET_API_KEY or config.BITGET_API_KEY == "your_api_key_here":
        print(f"{Fore.RED}错误：未配置 Bitget API Key。{Style.RESET_ALL}")
        print("请复制 .env.example 为 .env 并填写真实凭证：")
        print("  cp .env.example .env && nano .env")
        sys.exit(1)


def _all_uids() -> list[str]:
    """从copy_settings的enabled_traders字段获取启用的交易员列表"""
    import json
    import sqlite3
    db_path = config.DB_PATH
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT enabled_traders FROM copy_settings WHERE id=1")
    row = cur.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return []


def _resolve_uids(arg: str | None) -> list[str]:
    """将命令行参数 uid 或 'all' 解析为 uid 列表。"""
    if not arg or arg == "all":
        uids = _all_uids()
        if not uids:
            print("暂无已追踪的交易员。请先用 'search' 命令添加。")
        return uids
    return [arg]


# ── 子命令 ────────────────────────────────────────────────────────────────────

def cmd_search(nickname: str):
    _require_key()
    print(f"搜索昵称：{nickname} …")
    try:
        results = api_client.search_trader(nickname)
    except Exception as exc:
        print(f"{Fore.RED}搜索失败：{exc}{Style.RESET_ALL}")
        sys.exit(1)

    if not results:
        print("未找到匹配的交易员。")
        return

    print(f"\n找到 {len(results)} 名交易员：\n")
    for i, r in enumerate(results[:10], 1):
        uid  = r.get("traderUid") or r.get("uid", "")
        nick = r.get("traderNickName") or r.get("nickName", uid)
        print(f"  {i}. [{uid[:12]}]  {nick}")

    if len(results) == 1:
        choice = 1
    else:
        raw = input("\n输入序号添加到追踪列表（回车跳过）：").strip()
        if not raw:
            return
        try:
            choice = int(raw)
        except ValueError:
            print("无效输入，已取消。")
            return

    if not (1 <= choice <= len(results)):
        print("序号超出范围。")
        return

    selected = results[choice - 1]
    uid  = selected.get("traderUid") or selected.get("uid", "")
    nick = selected.get("traderNickName") or selected.get("nickName", uid)

    print(f"\n正在初始化交易员 {nick}（这可能需要几十秒）…")
    collector.init_trader(uid, nick)
    print(f"{Fore.GREEN}已成功添加：{nick}{Style.RESET_ALL}")


def cmd_list():
    traders = db.get_all_traders()
    if not traders:
        print("暂无已追踪的交易员。")
        return
    print(f"\n已追踪交易员（共 {len(traders)} 名）：\n")
    for t in traders:
        import datetime
        lu = datetime.datetime.fromtimestamp(t["last_updated"]).strftime("%Y-%m-%d %H:%M")
        print(
            f"  {Fore.CYAN}{t['nickname']:<20}{Style.RESET_ALL}"
            f"  uid={t['trader_uid'][:12]}  "
            f"更新={lu}"
        )


def cmd_analyze(uid_or_all: str | None):
    uids = _resolve_uids(uid_or_all)
    if not uids:
        return
    results = [analyzer.compute(uid) for uid in uids]
    if len(results) == 1:
        reporter.print_trader_report(results[0])
    else:
        reporter.print_compare_table(results)
        for r in results:
            reporter.print_trader_report(r)


def cmd_positions(uid_or_all: str | None):
    uids = _resolve_uids(uid_or_all)
    for uid in uids:
        reporter.print_current_positions(uid)


def cmd_trades(uid: str, n: int = 20):
    reporter.print_recent_trades(uid, n)


def cmd_report(uid_or_all: str | None):
    uids = _resolve_uids(uid_or_all)
    if not uids:
        return
    results = [analyzer.compute(uid) for uid in uids]
    reporter.print_compare_table(results)
    reporter.save_daily_report(results)


def cmd_run(uid_or_all: str | None):
    _require_key()
    uids = _resolve_uids(uid_or_all)
    if not uids:
        return
    # 启动前先打一次分析
    print(f"\n{Fore.YELLOW}启动采集前先展示当前分析快照…{Style.RESET_ALL}")
    results = [analyzer.compute(uid) for uid in uids]
    reporter.print_compare_table(results)
    for uid in uids:
        reporter.print_current_positions(uid)
    print()
    collector.run(uids)


def cmd_remove(uid: str):
    trader = db.get_trader(uid)
    if not trader:
        print(f"找不到 uid={uid} 的交易员。")
        return
    db.clear_snapshots(uid)
    # 仅清除快照，保留历史数据以供分析
    print(f"{Fore.YELLOW}已清除 {trader['nickname']} 的持仓快照（历史数据保留）。{Style.RESET_ALL}")
    print("若要彻底删除，请直接在 SQLite 中执行：")
    print(f"  DELETE FROM traders WHERE trader_uid='{uid}';")
    print(f"  DELETE FROM trades   WHERE trader_uid='{uid}';")


# ── 入口 ──────────────────────────────────────────────────────────────────────

USAGE = __doc__


def main():
    db.init_db()
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        return

    cmd = args[0].lower()

    if cmd == "search":
        if len(args) < 2:
            print("用法：python main.py search <昵称>")
            sys.exit(1)
        cmd_search(args[1])

    elif cmd == "list":
        cmd_list()

    elif cmd == "analyze":
        cmd_analyze(args[1] if len(args) > 1 else None)

    elif cmd == "positions":
        cmd_positions(args[1] if len(args) > 1 else None)

    elif cmd == "trades":
        if len(args) < 2:
            print("用法：python main.py trades <uid> [n]")
            sys.exit(1)
        n = int(args[2]) if len(args) > 2 else 20
        cmd_trades(args[1], n)

    elif cmd == "report":
        cmd_report(args[1] if len(args) > 1 else None)

    elif cmd == "run":
        cmd_run(args[1] if len(args) > 1 else None)

    elif cmd == "remove":
        if len(args) < 2:
            print("用法：python main.py remove <uid>")
            sys.exit(1)
        cmd_remove(args[1])

    else:
        print(f"{Fore.RED}未知命令：{cmd}{Style.RESET_ALL}")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
