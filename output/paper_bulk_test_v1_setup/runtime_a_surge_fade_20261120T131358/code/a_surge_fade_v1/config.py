"""A surge-fade paper config. Simulation assumptions only — no live orders."""

from __future__ import annotations

import os
from pathlib import Path

STRATEGY_ID = "a_surge_fade_v1"
STRATEGY_VERSION = "a_surge_fade_short_v1_20261120"
CODE_FIX_TAG = "fix_20261120_a_surge_fade_triarm"
PROFILE_PARENT = "paper_a_surge_fade_triarm_v1"

RUNTIME_ROOT = (
    Path(os.environ["BREAKOUT_RUNTIME_ROOT"]).resolve()
    if os.environ.get("BREAKOUT_RUNTIME_ROOT")
    else Path(".").resolve()
)

# Capital / risk (per arm ledger)
INITIAL_CAPITAL_USDT = 50_000.0
MARGIN_PER_TRADE_USDT = 1_000.0
LEVERAGE = 10.0
NOTIONAL_USDT = 10_000.0
MAX_CONCURRENT_POSITIONS = 20
MAX_PER_SYMBOL = 1
MAX_LOSS_PER_TRADE_USDT = 100.0
DAILY_LOSS_LOCK_USDT = 8_000.0
COOLDOWN_MINUTES = 3

# Costs (verified original; no maker discount)
FEE_RATE_ONE_SIDE = 0.0005
SLIPPAGE_ADVERSE = 0.0002
SIMULATION_ASSUMPTIONS = {
    "fee_rate_one_side": FEE_RATE_ONE_SIDE,
    "slippage_adverse": SLIPPAGE_ADVERSE,
    "note": "SIMULATION_ASSUMPTION — not exchange actual; no maker rebate",
}

# Universe quality (reuse project thresholds)
MIN_QUOTE_VOLUME_USDT = 20_000_000.0
MAX_SPREAD = 0.0008
QUOTE_MAX_AGE_SEC = 5.0
BOOK_TICKER_MAX_AGE_SEC = 5.0

# Hard excludes (explicit). Other large-caps NOT claimed fully excluded.
HARD_EXCLUDE_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
# Leveraged / stable-ish bases (project + explicit)
LEVERAGE_TOKEN_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE_BASES = frozenset({"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1"})

# Entry
LIMIT_ENTRY_OFFSET_PCT = 0.01  # short limit = S * 1.01
CANCEL_DROP_FROM_S_PCT = 0.02  # cancel if mid <= S * 0.98
# Old A had LIMIT_ENTRY_TTL_SEC=0 (no time TTL). This arm uses price-bounded cancel only.
LIMIT_ENTRY_TTL_SEC = 0.0
TTL_POLICY = "no_time_ttl_reuse_oldA; cancel_when_mid_le_S_times_0.98"

# Stop / partial
INITIAL_STOP_ADVERSE_PCT = 0.005  # short: entry * 1.005
PARTIAL_TP_FRACTION = 0.70

# Three arms: first_tp_pct / second_tp_pct (price drop for short)
ARMS = {
    "tp100": {
        "profile_id": "paper_a_surge_fade_tp100_v1",
        "first_tp_pct": 0.01,
        "second_tp_pct": 0.02,
        "label": "A_TP100",
    },
    "tp200": {
        "profile_id": "paper_a_surge_fade_tp200_v1",
        "first_tp_pct": 0.02,
        "second_tp_pct": 0.04,
        "label": "A_TP200",
    },
    "tp350": {
        "profile_id": "paper_a_surge_fade_tp350_v1",
        "first_tp_pct": 0.035,
        "second_tp_pct": 0.07,
        "label": "A_TP350",
    },
}

# Signal
SURGE_MIN_PCT_24H = 0.50
SURGE_BUCKET_MID = 1.00
BREAK_LOOKBACK = 5  # prior 5 completed 1m lows
KLINES_FETCH_LIMIT = 1600  # >24h of 1m
KLINES_API_CONCURRENCY = 8
KLINES_MIN_INTERVAL_SEC = 0.05

# Loop
MANAGE_DUE_SEC = 5.0
SCAN_SECOND = 3
UNIVERSE_REFRESH_MINUTES = 10
TZ_NAME = "Asia/Taipei"

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

# HTTP (manage-first budgets)
HTTP_REQUEST_TIMEOUT_SEC = 15.0
HTTP_MAX_ATTEMPTS = 5
HTTP_OP_BUDGET_SEC = 45.0
MANAGE_BOOK_REQUEST_TIMEOUT_SEC = 5.0
MANAGE_BOOK_MAX_ATTEMPTS = 2
MANAGE_BOOK_OP_BUDGET_SEC = 10.0
SCAN_SLICE_MAX_WAIT_SEC = 2.0
SCAN_SLICE_WALL_SEC = 25.0  # hard wall so scan cannot starve manage
SCAN_KLINE_WAIT_SEC = 8.0   # per-symbol kline fetch timeout

# TradFi filters (reuse project list)
NON_CRYPTO_SUBTYPE_KEYWORDS = frozenset(
    {"Equity", "Index", "Commodity", "Pre-Market", "Forex", "TradFi", "Stock"}
)
TRADFI_BASE_BLACKLIST = frozenset(
    {
        "AAPL", "AMZN", "AUD", "CAD", "CHF", "COIN", "EUR", "GBP", "GOOG", "GOOGL",
        "JPY", "META", "MSFT", "MSTR", "NVDA", "NZD", "PAXG", "QQQ", "SPY", "TSLA",
        "USD", "XAG", "XAU", "XAUT",
    }
)
TRADFI_SYMBOL_PREFIXES = ("XAU", "XAG", "PAXG", "XAUT", "EUR", "GBP")


def set_runtime_root(root: str | Path) -> Path:
    global RUNTIME_ROOT
    RUNTIME_ROOT = Path(root).resolve()
    return RUNTIME_ROOT


def arm_paths(arm_key: str) -> dict[str, Path]:
    pid = ARMS[arm_key]["profile_id"]
    root = RUNTIME_ROOT / "output" / pid
    return {
        "root": root,
        "account": root / "account_state.json",
        "fills": root / "fills.jsonl",
        "trades": root / "trades.csv",
        "equity": root / "equity_snapshots.jsonl",
        "pid": root / "paper.pid",
        "lock": root / "paper_profile.lock",
        "runs": root / "runs",
        "vault": RUNTIME_ROOT / "data" / PROFILE_PARENT / "kline_vault_v2_clean",
    }


def shared_paths() -> dict[str, Path]:
    root = RUNTIME_ROOT / "output" / PROFILE_PARENT
    return {
        "root": root,
        "signals": root / "signals.jsonl",
        "scan_log": root / "scan_events.jsonl",
        "universe": root / "universe_snapshot.json",
        "state": root / "observer_state.json",
        "pid": root / "paper.pid",
        "lock": root / "paper_profile.lock",
        "review": root / "review.md",
        "launch": root / "LAUNCH_META.json",
    }
