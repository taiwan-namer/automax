"""
breakout_follow_v1 首版模擬預設參數。

明確標記：SIMULATION_ASSUMPTION — 非最佳化、不保證獲利、非交易所實際費率。
"""

from __future__ import annotations

from pathlib import Path

STRATEGY_ID = "breakout_follow_v1"
STRATEGY_VERSION = "breakout_follow_v1"
CONFIG_HASH_ALGO = "stable_v1"  # frozenset→sorted list；不重寫舊策略 hash
# PROFILE_ID 決定輸出／帳戶／vault 根路徑；預設與 STRATEGY_ID 相同（正式基底）
PROFILE_ID = "breakout_follow_v1"
ACTIVE_PROFILE = "breakout_follow_v1"

# 路徑（依 PROFILE_ID；舊 breakout_follow_v1 帳戶與 vault 不得被其他 profile 覆寫）
ROOT_OUT = Path("output") / PROFILE_ID
ACCOUNT_PATH = ROOT_OUT / "account_state.json"
RUN_MANIFEST_PATH = ROOT_OUT / "run_manifest.json"
DECISION_TRACE_PATH = ROOT_OUT / "decision_trace.jsonl"
FILLS_PATH = ROOT_OUT / "fills.jsonl"
TRADES_PATH = ROOT_OUT / "trades.csv"
EQUITY_PATH = ROOT_OUT / "equity_snapshots.jsonl"
REVIEW_PATH = ROOT_OUT / "review.md"
UNIVERSE_PATH = ROOT_OUT / "universe_snapshot.json"
PID_PATH = ROOT_OUT / "paper.pid"
# 舊污染 vault（keep-first 未收盤快照）封存於 data/breakout_follow_v1/kline_vault，勿覆寫。
# 修復版使用獨立乾淨 vault；部署前需確認路徑切換。
KLINE_VAULT_DIR = Path("data") / PROFILE_ID / "kline_vault_v2_clean"
KLINE_VAULT_LEGACY_SEALED = Path("data") / "breakout_follow_v1" / "kline_vault"
DECISION_SNAPSHOT_DIR = ROOT_OUT / "decision_snapshots"
# 帳務遷移版本（cash 計入 gross）；與交易參數無關
ACCOUNTING_MIGRATION_VERSION = "cash_gross_v1"
CODE_FIX_TAG = "fix_20261119_p0p1_trail_after_partial_daily2000"


def _rebind_profile_paths() -> None:
    """依 PROFILE_ID 重綁所有輸出／vault 路徑。"""
    global ROOT_OUT, ACCOUNT_PATH, RUN_MANIFEST_PATH, DECISION_TRACE_PATH
    global FILLS_PATH, TRADES_PATH, EQUITY_PATH, REVIEW_PATH, UNIVERSE_PATH, PID_PATH
    global KLINE_VAULT_DIR, DECISION_SNAPSHOT_DIR
    ROOT_OUT = Path("output") / PROFILE_ID
    ACCOUNT_PATH = ROOT_OUT / "account_state.json"
    RUN_MANIFEST_PATH = ROOT_OUT / "run_manifest.json"
    DECISION_TRACE_PATH = ROOT_OUT / "decision_trace.jsonl"
    FILLS_PATH = ROOT_OUT / "fills.jsonl"
    TRADES_PATH = ROOT_OUT / "trades.csv"
    EQUITY_PATH = ROOT_OUT / "equity_snapshots.jsonl"
    REVIEW_PATH = ROOT_OUT / "review.md"
    UNIVERSE_PATH = ROOT_OUT / "universe_snapshot.json"
    PID_PATH = ROOT_OUT / "paper.pid"
    KLINE_VAULT_DIR = Path("data") / PROFILE_ID / "kline_vault_v2_clean"
    DECISION_SNAPSHOT_DIR = ROOT_OUT / "decision_snapshots"


def apply_profile(profile_name: str) -> dict:
    """套用 paper profile（僅資金／路徑／持倉上限 overlay；不改訊號門檻邏輯）。"""
    global PROFILE_ID, ACTIVE_PROFILE, CODE_FIX_TAG
    global INITIAL_CAPITAL_USDT, MAX_CONCURRENT_POSITIONS, DAILY_LOSS_LOCK_USDT
    global MARGIN_PER_TRADE_USDT, LEVERAGE, NOTIONAL_USDT, MAX_LOSS_PER_TRADE_USDT, MAX_PER_SYMBOL
    global COOLDOWN_MINUTES, REVERSE_WINDOW_MINUTES, MAX_REVERSES_PER_WINDOW
    global PARTIAL_TP_FRACTION, PARTIAL_TP_NET_USDT, TRAIL_ATR_MULT, TOP_N, ATR_STOP_MULT, VOL_X_MIN

    from .profiles import DEFAULT_PROFILE, PROFILES

    if profile_name not in PROFILES:
        raise ValueError(f"unknown profile: {profile_name}; known={sorted(PROFILES)}")

    ACTIVE_PROFILE = profile_name
    overlay = PROFILES[profile_name]
    if overlay is None:
        PROFILE_ID = DEFAULT_PROFILE
        _rebind_profile_paths()
        return {"profile": profile_name, "overlay": None, "PROFILE_ID": PROFILE_ID}

    # 核對「聲稱沿用」數值與 overlay 一致後再寫入（避免偷偷覆蓋）
    expected_same = {
        "MARGIN_PER_TRADE_USDT": 1_000.0,
        "LEVERAGE": 10.0,
        "NOTIONAL_USDT": 10_000.0,
        "MAX_LOSS_PER_TRADE_USDT": 100.0,
        "MAX_PER_SYMBOL": 1,
        "COOLDOWN_MINUTES": 3,
        "REVERSE_WINDOW_MINUTES": 30,
        "MAX_REVERSES_PER_WINDOW": 2,
        "PARTIAL_TP_FRACTION": 0.30,
        "PARTIAL_TP_NET_USDT": 100.0,
        "TRAIL_ATR_MULT": 1.5,
        "TOP_N": 10,
        "ATR_STOP_MULT": 1.5,
        "VOL_X_MIN": 3.0,
    }
    mismatches = []
    for k, exp in expected_same.items():
        if k in overlay and overlay[k] != exp:
            mismatches.append({"key": k, "overlay": overlay[k], "expected_reuse": exp})
    if mismatches:
        raise ValueError(f"profile overlay mismatches claimed reuse values: {mismatches}")

    PROFILE_ID = str(overlay["PROFILE_ID"])
    INITIAL_CAPITAL_USDT = float(overlay["INITIAL_CAPITAL_USDT"])
    MAX_CONCURRENT_POSITIONS = int(overlay["MAX_CONCURRENT_POSITIONS"])
    DAILY_LOSS_LOCK_USDT = float(overlay["DAILY_LOSS_LOCK_USDT"])
    MARGIN_PER_TRADE_USDT = float(overlay["MARGIN_PER_TRADE_USDT"])
    LEVERAGE = float(overlay["LEVERAGE"])
    NOTIONAL_USDT = float(overlay["NOTIONAL_USDT"])
    MAX_LOSS_PER_TRADE_USDT = float(overlay["MAX_LOSS_PER_TRADE_USDT"])
    MAX_PER_SYMBOL = int(overlay["MAX_PER_SYMBOL"])
    COOLDOWN_MINUTES = int(overlay["COOLDOWN_MINUTES"])
    REVERSE_WINDOW_MINUTES = int(overlay["REVERSE_WINDOW_MINUTES"])
    MAX_REVERSES_PER_WINDOW = int(overlay["MAX_REVERSES_PER_WINDOW"])
    PARTIAL_TP_FRACTION = float(overlay["PARTIAL_TP_FRACTION"])
    PARTIAL_TP_NET_USDT = float(overlay["PARTIAL_TP_NET_USDT"])
    TRAIL_ATR_MULT = float(overlay["TRAIL_ATR_MULT"])
    TOP_N = int(overlay["TOP_N"])
    ATR_STOP_MULT = float(overlay["ATR_STOP_MULT"])
    VOL_X_MIN = float(overlay["VOL_X_MIN"])
    if not str(CODE_FIX_TAG).endswith("_bulk_v1"):
        CODE_FIX_TAG = f"{CODE_FIX_TAG}_bulk_v1"
    _rebind_profile_paths()
    return {
        "profile": profile_name,
        "PROFILE_ID": PROFILE_ID,
        "ROOT_OUT": str(ROOT_OUT),
        "KLINE_VAULT_DIR": str(KLINE_VAULT_DIR),
        "INITIAL_CAPITAL_USDT": INITIAL_CAPITAL_USDT,
        "MAX_CONCURRENT_POSITIONS": MAX_CONCURRENT_POSITIONS,
        "DAILY_LOSS_LOCK_USDT": DAILY_LOSS_LOCK_USDT,
        "CODE_FIX_TAG": CODE_FIX_TAG,
    }

# 資金與風險（首版預設）
INITIAL_CAPITAL_USDT = 10_000.0
MARGIN_PER_TRADE_USDT = 1_000.0
LEVERAGE = 10.0
NOTIONAL_USDT = 10_000.0  # 1000 × 10
MAX_CONCURRENT_POSITIONS = 3
MAX_PER_SYMBOL = 1
MAX_LOSS_PER_TRADE_USDT = 100.0  # 含成本估算上限
DAILY_LOSS_LOCK_USDT = 2_000.0  # paper 模擬：提高日損鎖以累積更多樣本（非實盤）
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
