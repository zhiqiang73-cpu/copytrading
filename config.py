"""
鍏ㄥ眬閰嶇疆绠＄悊锛氫粠 .env 鍔犺浇鍑瘉鍜岃繍琛屽弬鏁?
"""
import os
from dotenv import load_dotenv

load_dotenv()

# 鈹€鈹€ Bitget API 鍑瘉 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
BASE_URL          = "https://api.bitget.com"
# 妯℃嫙鐩樻ā寮忥細璁句负 "1" 鏃朵細鍦ㄨ姹傚ご鍔?paptrading=1锛宲roductType 鑷姩鍒囨崲涓?SUMCBL
SIMULATED         = os.getenv("BITGET_SIMULATED", "1") == "1"

BINANCE_SIM_BASE_URL = "https://testnet.binancefuture.com"
BINANCE_LIVE_BASE_URL = "https://fapi.binance.com"

# Binance Futures API base URL.
# If BINANCE_BASE_URL is not set, choose testnet for simulated profile and mainnet for live profile.
BINANCE_API_KEY   = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
_default_binance_base_url = BINANCE_SIM_BASE_URL if SIMULATED else BINANCE_LIVE_BASE_URL
BINANCE_BASE_URL  = (os.getenv("BINANCE_BASE_URL", _default_binance_base_url) or "").strip().rstrip("/")
if not BINANCE_BASE_URL:
    BINANCE_BASE_URL = _default_binance_base_url

# 鈹€鈹€ 閲囬泦鍙傛暟 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "5"))   # 绉?
HISTORY_DAYS      = 90                                      # 鍒濆鍖栨椂鎷夊彇鍘嗗彶澶╂暟
PAGE_SIZE         = 50                                      # 鍒嗛〉澶у皬锛圓PI 闄愬埗锛?

# 鈹€鈹€ 鏁版嵁搴?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")

# 鈹€鈹€ 鏃ュ織 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# 鈹€鈹€ 瑕佽拷韪殑浜ゆ槗鍛樻樀绉板垪琛紙鍦ㄨ繖閲屾坊鍔狅紝鎴栭€氳繃 CLI 鍔ㄦ€佹坊鍔狅級鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 绀轰緥锛歍RACKED_TRADERS = ["trader_nickname_1", "trader_nickname_2"]
TRACKED_TRADERS: list[str] = []

# 鈹€鈹€ 纭€х瓫閫夐棬妲涳紙6椤癸紝鍏ㄩ儴鐢ㄤ簬 PASS/WATCH/FAIL 璇勭骇锛夆攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 澶忔櫘銆丆almar銆佺泩浜忔瘮绛変粎灞曠ず锛屼笉鍋氱‖鎬ц繃婊?
FILTER = {
    "active_days":       7,    # 鈶?娲昏穬搴︼細鏈€杩?N 澶╁唴蹇呴』鏈変氦鏄?
    "max_drawdown":      0.25, # 鈶?鏈€澶у洖鎾?< 25%
    "min_expected_value": 0.0, # 鈶?鏈熸湜鍊?> 0锛堥暱鏈熺泩鍒╋級
    "min_trade_count":   30,   # 鈶?鎬荤瑪鏁?>= 30锛堟牱鏈彲淇★級
    "max_loss_streak":   5,    # 鈶?鏈€澶ц繛浜?< 5 娆?
    "min_avg_hold_h":    0.5,  # 鈶?骞冲潎鎸佷粨 > 30 鍒嗛挓锛堝彲璺熸€э級
}
# 鈹€鈹€ 璺熷崟鍙傛暟琛ヤ竵 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 榛樿浠峰樊瀹瑰繊搴︼細0.005 (0.5%)銆?
# 璋冮珮姝ゅ€煎彲浠ユ洿杞绘澗鍦板宸叉湁浠撲綅杩涜鈥滆ˉ绁ㄤ笂杞︹€濓紝浣嗚繃楂樺彲鑳藉鑷村湪鏋佺琛屾儏涓嬫帴鐩樸€?
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

DEFAULT_ENTRY_ORDER_MODE = (os.getenv("DEFAULT_ENTRY_ORDER_MODE", "maker_limit") or "maker_limit").strip().lower()
if DEFAULT_ENTRY_ORDER_MODE not in {"market", "maker_limit"}:
    DEFAULT_ENTRY_ORDER_MODE = "maker_limit"
DEFAULT_ENTRY_MAKER_LEVELS = max(0, int(os.getenv("DEFAULT_ENTRY_MAKER_LEVELS", "1")))
DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC = max(1, int(os.getenv("DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC", "10")))
DEFAULT_ENTRY_LIMIT_FALLBACK_TO_MARKET = os.getenv("DEFAULT_ENTRY_LIMIT_FALLBACK_TO_MARKET", "1") == "1"


