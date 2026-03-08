"""
全局配置管理：从 .env 加载凭证和运行参数
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Bitget API 凭证 ─────────────────────────────────────────────────────────
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
BASE_URL          = "https://api.bitget.com"
# 模拟盘模式：设为 "1" 时会在请求头加 paptrading=1，productType 自动切换为 SUMCBL
SIMULATED         = os.getenv("BITGET_SIMULATED", "0") == "1"

# Binance Futures API base URL.
# Default uses Futures testnet for simulated trading.
BINANCE_API_KEY   = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL  = (os.getenv("BINANCE_BASE_URL", "https://testnet.binancefuture.com") or "").strip().rstrip("/")
if not BINANCE_BASE_URL:
    BINANCE_BASE_URL = "https://testnet.binancefuture.com"

# ── 采集参数 ─────────────────────────────────────────────────────────────────
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "5"))   # 秒
HISTORY_DAYS      = 90                                      # 初始化时拉取历史天数
PAGE_SIZE         = 50                                      # 分页大小（API 限制）

# ── 数据库 ────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")

# ── 日志 ─────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── 要追踪的交易员昵称列表（在这里添加，或通过 CLI 动态添加）─────────────────
# 示例：TRACKED_TRADERS = ["trader_nickname_1", "trader_nickname_2"]
TRACKED_TRADERS: list[str] = []

# ── 硬性筛选门槛（6项，全部用于 PASS/WATCH/FAIL 评级）────────────────────────
# 夏普、Calmar、盈亏比等仅展示，不做硬性过滤
FILTER = {
    "active_days":       7,    # ① 活跃度：最近 N 天内必须有交易
    "max_drawdown":      0.25, # ② 最大回撤 < 25%
    "min_expected_value": 0.0, # ③ 期望值 > 0（长期盈利）
    "min_trade_count":   30,   # ④ 总笔数 >= 30（样本可信）
    "max_loss_streak":   5,    # ⑤ 最大连亏 < 5 次
    "min_avg_hold_h":    0.5,  # ⑥ 平均持仓 > 30 分钟（可跟性）
}
# ── 跟单参数补丁 ─────────────────────────────────────────────────────────────
# 默认价差容忍度：0.005 (0.5%)。
# 调高此值可以更轻松地对已有仓位进行“补票上车”，但过高可能导致在极端行情下接盘。
DEFAULT_PRICE_TOLERANCE = 0.005

# ?? ???????????????? 0.03 = 3%??????????????????????????????
DEFAULT_DAILY_LOSS_LIMIT_PCT = float(os.getenv("DEFAULT_DAILY_LOSS_LIMIT_PCT", "0.03"))
DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT = float(os.getenv("DEFAULT_TOTAL_DRAWDOWN_LIMIT_PCT", "0.10"))
DEFAULT_TAKE_PROFIT_ENABLED = os.getenv("DEFAULT_TAKE_PROFIT_ENABLED", "1") == "1"
DEFAULT_STOP_LOSS_PCT = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.06"))
DEFAULT_TP1_ROI_PCT = float(os.getenv("DEFAULT_TP1_ROI_PCT", "0.08"))
DEFAULT_TP1_CLOSE_PCT = float(os.getenv("DEFAULT_TP1_CLOSE_PCT", "0.30"))
DEFAULT_TP2_ROI_PCT = float(os.getenv("DEFAULT_TP2_ROI_PCT", "0.15"))
DEFAULT_TP2_CLOSE_PCT = float(os.getenv("DEFAULT_TP2_CLOSE_PCT", "0.30"))
DEFAULT_TP3_ROI_PCT = float(os.getenv("DEFAULT_TP3_ROI_PCT", "0.25"))
DEFAULT_TP3_CLOSE_PCT = float(os.getenv("DEFAULT_TP3_CLOSE_PCT", "0.40"))
DEFAULT_BREAKEVEN_BUFFER_PCT = float(os.getenv("DEFAULT_BREAKEVEN_BUFFER_PCT", "0.005"))
DEFAULT_TRAIL_CALLBACK_PCT = float(os.getenv("DEFAULT_TRAIL_CALLBACK_PCT", "0.06"))
DEFAULT_TP2_LOCKED_ROI_PCT = float(os.getenv("DEFAULT_TP2_LOCKED_ROI_PCT", "0.06"))
