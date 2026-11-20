"""
breakout_follow_v1 首版模擬預設參數。

明確標記：SIMULATION_ASSUMPTION — 非最佳化、不保證獲利、非交易所實際費率。
"""

from __future__ import annotations

from pathlib import Path

STRATEGY_ID = "breakout_follow_v1"
STRATEGY_VERSION = "breakout_follow_v1"
CONFIG_HASH_ALGO = "stable_v1"  # frozenset→sorted list；不重寫舊策略 hash

# 路徑（與舊策略完全隔離）
ROOT_OUT = Path("output") / STRATEGY_ID
ACCOUNT_PATH = ROOT_OUT / "account_state.json"
RUN_MANIFEST_PATH = ROOT_OUT / "run_manifest.json"
DECISION_TRACE_PATH = ROOT_OUT / "decision_trace.jsonl"
FILLS_PATH = ROOT_OUT / "fills.jsonl"
TRADES_PATH = ROOT_OUT / "trades.csv"
EQUITY_PATH = ROOT_OUT / "equity_snapshots.jsonl"
REVIEW_PATH = ROOT_OUT / "review.md"
UNIVERSE_PATH = ROOT_OUT / "universe_snapshot.json"
PID_PATH = ROOT_OUT / "paper.pid"
KLINE_VAULT_DIR = Path("data") / STRATEGY_ID / "kline_vault"
DECISION_SNAPSHOT_DIR = ROOT_OUT / "decision_snapshots"

# 資金與風險（首版預設）
INITIAL_CAPITAL_USDT = 10_000.0
MARGIN_PER_TRADE_USDT = 1_000.0
LEVERAGE = 10.0
NOTIONAL_USDT = 10_000.0  # 1000 × 10
MAX_CONCURRENT_POSITIONS = 3
MAX_PER_SYMBOL = 1
MAX_LOSS_PER_TRADE_USDT = 100.0  # 含成本估算上限
DAILY_LOSS_LOCK_USDT = 300.0
ATR_STOP_MULT = 1.5
ATR_PERIOD = 14
RANGE_LOOKBACK = 20
VOLUME_MA_PERIOD = 20
VOL_X_MIN = 3.0
BREAKOUT_BUFFER_ATR = 0.1
MAX_CHASE_ATR = 1.0
MAX_SPREAD = 0.0008  # (ask-bid)/mid
COOLDOWN_MINUTES = 3
REVERSE_WINDOW_MINUTES = 30
MAX_REVERSES_PER_WINDOW = 2
PARTIAL_TP_FRACTION = 0.30
PARTIAL_TP_NET_USDT = 100.0
TRAIL_ATR_MULT = 1.5

# 成交假設（SIMULATION_ASSUMPTION）
FEE_RATE_ONE_SIDE = 0.0005
SLIPPAGE_ADVERSE = 0.0002
SIMULATION_ASSUMPTIONS = {
    "fee_rate_one_side": FEE_RATE_ONE_SIDE,
    "slippage_adverse": SLIPPAGE_ADVERSE,
    "note": "SIMULATION_ASSUMPTION — not exchange actual fee/fill",
}

# 標的與掃描
MIN_QUOTE_VOLUME_USDT = 20_000_000.0
TOP_N = 10
UNIVERSE_REFRESH_MINUTES = 10
SCAN_SECOND = 2  # 每分鐘第 N 秒檢查已收盤 K
QUOTE_MAX_AGE_SEC = 5.0
KLINE_MAX_AGE_SEC = 90.0
SIGNAL_MAX_DELAY_SEC = 90.0
KLINES_API_CONCURRENCY = 12
KLINES_MIN_INTERVAL_SEC = 0.02
KLINES_FETCH_LIMIT = 40  # 足夠算 ATR(14)+range(20)+buffer
BOOK_TICKER_MAX_AGE_SEC = 5.0
MANAGE_POLL_SEC = 5.0  # 持倉管理輪詢上限
TZ_NAME = "Asia/Taipei"

# 公開 REST（僅讀取行情；禁止下單端點）
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
ALLOWED_PUBLIC_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/24hr",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/klines",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingRate",
        "/fapi/v1/time",
    }
)
FORBIDDEN_ORDER_PATH_SUBSTRINGS = (
    "/fapi/v1/order",
    "/fapi/v1/batchOrder",
    "/fapi/v1/userTrades",
    "/fapi/v1/listenKey",
    "/fapi/v1/positionSide",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/sapi/",
    "order.place",
    "order.cancel",
)

# 非加密排除（與舊專案一致語意；新策略不過濾主流大幣）
NON_CRYPTO_SUBTYPE_KEYWORDS = frozenset(
    {"TradFi", "Commodity", "Equity", "Stock", "Forex", "Index", "Pre-Market"}
)
TRADFI_BASE_BLACKLIST = frozenset(
    {
        "TSLA", "AAPL", "NVDA", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "COIN", "MSTR",
        "SPY", "QQQ", "XAU", "XAG", "PAXG", "XAUT", "EUR", "GBP", "USD", "JPY",
        "AUD", "NZD", "CAD", "CHF",
    }
)
TRADFI_SYMBOL_PREFIXES = ("XAU", "XAG", "PAXG", "XAUT", "EUR", "GBP")
