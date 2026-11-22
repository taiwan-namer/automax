"""D quiet-short paper config. Simulation assumptions only — no live orders.

Capital + dual-sided near-touch depth CONFIRMED 2026-11-20 by user.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

STRATEGY_ID = "d_quiet_short_v1"
STRATEGY_VERSION = "d_quiet_short_testnet_20261121"
PROFILE_ID = "testnet_d_quiet_short_v1"
CODE_FIX_TAG = "testnet_d_size_x0_1_20261121"

RUNTIME_ROOT = (
    Path(os.environ["BREAKOUT_RUNTIME_ROOT"]).resolve()
    if os.environ.get("BREAKOUT_RUNTIME_ROOT")
    else Path(".").resolve()
)

# ---------------------------------------------------------------------------
# Capital — CONFIRMED 2026-11-20
# 300U margin × 10x = 3000U notional
# TP/SL triggers on GROSS PnL; reports also show fee/slip/funding → net
# SL 30U is a trigger threshold, NOT a guaranteed max loss.
# 20 slots ≈ 6000U margin / 60000U notional; nominal SL sum 600U excl. costs
# ---------------------------------------------------------------------------
STAKE_USDT = 30.0
TP_USDT = 15.0  # gross trigger (10x smaller for testnet)
SL_USDT = 3.0   # gross trigger; NOT max-loss guarantee
MAX_CONCURRENT_SLOTS = 10

LEVERAGE: Optional[float] = 10.0
STAKE_IS_MARGIN: Optional[bool] = True
TP_SL_BASIS: Optional[str] = "gross"
CAPITAL_CONFIRMED = True
CAPITAL_CONFIRM_NOTE = (
    "2026-11-21 testnet: margin 30U, lev 10x, notional 300U; "
    "TP15/SL3 gross (÷10 from paper); independent PROFILE_ID."
)

INITIAL_CAPITAL_USDT = 5_000.0  # testnet ledger seed

# Costs (verified project assumptions; no maker rebate)
FEE_RATE_ONE_SIDE = 0.0005
SLIPPAGE_ADVERSE = 0.0002  # used when book walk unavailable; walk VWAP preferred on exit
SIMULATION_ASSUMPTIONS = {
    "fee_rate_one_side": FEE_RATE_ONE_SIDE,
    "slippage_adverse_fallback": SLIPPAGE_ADVERSE,
    "fill_model_entry": (
        "limit_touch_bid_ge_limit_fill_at_limit; "
        "plus recorded book-walk sim for 3000U sell-into-bids (diagnostic)"
    ),
    "fill_model_exit": (
        "aggressive cover: walk asks for qty; disclose depth shortfall; "
        "mid never used as fill; no maker rebate"
    ),
    "note": "SIMULATION_ASSUMPTION — not exchange actual; paper ≠ live results",
}

# Universe quality — EXPLICIT units
# MAX_SPREAD=0.0008 means 0.08% of mid: (ask-bid)/mid
# MIN_QUOTE_VOLUME=20_000_000 means Binance futures 24h quoteVolume in USDT
MIN_QUOTE_VOLUME_USDT = 20_000_000.0
MAX_SPREAD = 0.0008
MAX_SPREAD_PCT_DISPLAY = "0.08%"
QUOTE_VOLUME_DEFINITION = "Binance USDT-M futures ticker.quoteVolume = 24h quote notional in USDT"
QUOTE_MAX_AGE_SEC = 5.0

# Depth gate — CONFIRMED dual near-touch (NOT raw top-of-book sum of far levels)
DEPTH_GATE_CONFIRMED = True
DEPTH_NEAR_TOUCH_PCT = 0.001  # 0.1% from best bid / best ask
MIN_NEAR_TOUCH_BID_NOTIONAL_USDT = 50_000.0
MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT = 50_000.0
DEPTH_WALK_NOTIONAL_USDT = 300.0  # simulate single-ticket fill (testnet size)
DEPTH_API_LIMIT = 100
DEPTH_NOTE = (
    "Entry requires BOTH near-touch bid AND ask notionals >= 50k USDT "
    "(levels within 0.1% of best bid / best ask). "
    "Also record book-walk VWAP/slip for 3000U. "
    "Depth fail blocks NEW entries only; exits never refused — walk available asks and disclose shortfall. "
    "Paper liquidity filter; does not guarantee resting liquidity or live fill quality."
)
# legacy alias kept for older test helpers
MIN_BOOK_BID_NOTIONAL_USDT: Optional[float] = MIN_NEAR_TOUCH_BID_NOTIONAL_USDT

HARD_EXCLUDE_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
LEVERAGE_TOKEN_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE_BASES = frozenset({"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1"})
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
LARGE_CAP_NOTE = (
    "Hard-exclude only BTC/ETH among large caps; no full large-cap denylist claimed. "
    "TradFi/equity/index/stable/leveraged tokens excluded via project lists."
)

# Entry / cancel (no time TTL)
LIMIT_ENTRY_OFFSET_PCT = 0.005  # short limit = P * 1.005
CANCEL_DROP_FROM_P_PCT = 0.01   # cancel if monitor mid <= P * 0.99
LIMIT_ENTRY_TTL_SEC = 0.0
TTL_POLICY = "no_time_ttl; cancel_when_mid_le_P_times_0.99_or_eligibility_fail"

# Signal: completed 15m only
KLINE_INTERVAL = "15m"
KLINE_MS = 15 * 60 * 1000
QUIET_BARS = 4
RANGE_AMP_MAX = 0.015  # window (hi-lo)/close ≤ 1.5%
BAR_AMP_MAX = 0.0075   # each bar (h-l)/open ≤ 0.75%
VOL_EXIT_FLOOR = 0.008
KLINES_FETCH_LIMIT = 20
KLINES_API_CONCURRENCY = 6

# Loop
MANAGE_DUE_SEC = 5.0
SCAN_SECOND = 5
UNIVERSE_REFRESH_MINUTES = 10
TZ_NAME = "Asia/Taipei"

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
ALLOWED_PUBLIC_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/24hr",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/klines",
        "/fapi/v1/depth",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingRate",
        "/fapi/v1/time",
    }
)
FORBIDDEN_ORDER_PATH_SUBSTRINGS = (
    "/fapi/v1/order",
    "/fapi/v1/batchOrder",
    "/fapi/v1/allOpenOrders",
    "/fapi/v1/positionSide",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/fapi/v2/positionRisk",
    "/fapi/v1/listenKey",
    "api_secret",
    "API_SECRET",
)

HTTP_REQUEST_TIMEOUT_SEC = 15.0
HTTP_MAX_ATTEMPTS = 5
HTTP_OP_BUDGET_SEC = 45.0
MANAGE_BOOK_REQUEST_TIMEOUT_SEC = 5.0
MANAGE_BOOK_MAX_ATTEMPTS = 2
MANAGE_BOOK_OP_BUDGET_SEC = 10.0
SCAN_SLICE_WALL_SEC = 25.0
SCAN_KLINE_WAIT_SEC = 8.0
DEPTH_LEVELS = DEPTH_API_LIMIT  # compat


def set_runtime_root(root: str | Path) -> Path:
    global RUNTIME_ROOT
    RUNTIME_ROOT = Path(root).resolve()
    return RUNTIME_ROOT


def capital_ready() -> bool:
    return bool(
        CAPITAL_CONFIRMED
        and STAKE_IS_MARGIN is not None
        and LEVERAGE is not None
        and LEVERAGE > 0
        and TP_SL_BASIS in ("gross", "net")
    )


def depth_ready() -> bool:
    return bool(
        DEPTH_GATE_CONFIRMED
        and MIN_NEAR_TOUCH_BID_NOTIONAL_USDT > 0
        and MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT > 0
        and DEPTH_NEAR_TOUCH_PCT > 0
    )


def paper_fills_allowed() -> bool:
    return capital_ready() and depth_ready()


def pending_confirmations() -> list[dict[str, Any]]:
    """Empty when confirmed; kept for reporting compatibility."""
    items: list[dict[str, Any]] = []
    if not capital_ready():
        items.append({"id": "capital", "status": "UNCONFIRMED"})
    if not depth_ready():
        items.append({"id": "depth", "status": "UNCONFIRMED"})
    return items


def confirm_capital_for_tests(
    *,
    stake_is_margin: bool = True,
    leverage: float = 10.0,
    tp_sl_basis: str = "gross",
) -> None:
    global STAKE_IS_MARGIN, LEVERAGE, TP_SL_BASIS, CAPITAL_CONFIRMED
    if tp_sl_basis not in ("gross", "net"):
        raise ValueError("tp_sl_basis must be gross|net")
    STAKE_IS_MARGIN = bool(stake_is_margin)
    LEVERAGE = float(leverage)
    TP_SL_BASIS = tp_sl_basis
    CAPITAL_CONFIRMED = True


def confirm_depth_for_tests(
    min_bid_notional: float = 50_000.0,
    min_ask_notional: float = 50_000.0,
) -> None:
    global DEPTH_GATE_CONFIRMED, MIN_NEAR_TOUCH_BID_NOTIONAL_USDT, MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT
    global MIN_BOOK_BID_NOTIONAL_USDT
    DEPTH_GATE_CONFIRMED = True
    MIN_NEAR_TOUCH_BID_NOTIONAL_USDT = float(min_bid_notional)
    MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT = float(min_ask_notional)
    MIN_BOOK_BID_NOTIONAL_USDT = float(min_bid_notional)


def reset_confirmations() -> None:
    """Test helper: temporarily block fills."""
    global STAKE_IS_MARGIN, LEVERAGE, TP_SL_BASIS, CAPITAL_CONFIRMED
    global DEPTH_GATE_CONFIRMED
    STAKE_IS_MARGIN = None
    LEVERAGE = None
    TP_SL_BASIS = None
    CAPITAL_CONFIRMED = False
    DEPTH_GATE_CONFIRMED = False


def restore_production_confirmations() -> None:
    """Restore user-confirmed production gates after tests."""
    global STAKE_IS_MARGIN, LEVERAGE, TP_SL_BASIS, CAPITAL_CONFIRMED
    global DEPTH_GATE_CONFIRMED, MIN_NEAR_TOUCH_BID_NOTIONAL_USDT, MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT
    global MIN_BOOK_BID_NOTIONAL_USDT
    STAKE_IS_MARGIN = True
    LEVERAGE = 10.0
    TP_SL_BASIS = "gross"
    CAPITAL_CONFIRMED = True
    DEPTH_GATE_CONFIRMED = True
    MIN_NEAR_TOUCH_BID_NOTIONAL_USDT = 50_000.0
    MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT = 50_000.0
    MIN_BOOK_BID_NOTIONAL_USDT = 50_000.0


def paths() -> dict[str, Path]:
    root = RUNTIME_ROOT / "output" / PROFILE_ID
    return {
        "root": root,
        "account": root / "account_state.json",
        "fills": root / "fills.jsonl",
        "events": root / "events.jsonl",
        "trades": root / "trades.csv",
        "signals": root / "signals.jsonl",
        "scan_log": root / "scan_events.jsonl",
        "universe": root / "universe_snapshot.json",
        "state": root / "observer_state.json",
        "equity": root / "equity_snapshots.jsonl",
        "review": root / "review.md",
        "settlement": root / "settlement",
        "pid": root / "paper.pid",
        "lock": root / "paper_profile.lock",
        "launch": root / "LAUNCH_META.json",
        "pending": root / "PENDING_CONFIRMATIONS.json",
        "quote_vol": root / "native_quote_vol.jsonl",
        "settings_snapshot": root / "SETTINGS_SNAPSHOT.json",
        "persist_halt": root / "PERSIST_HALT.json",
        "observation_events": root / "observation_events.jsonl",
        "shadow_lifecycle": root / "shadow_lifecycle.jsonl",
        "observation_health": root / "observation_health.json",
        "observation_meta": root / "OBSERVATION_META.json",
    }


def settings_snapshot_dict() -> dict[str, Any]:
    """Full launch settings for forensic replay (D-native)."""
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "profile_id": PROFILE_ID,
        "code_fix_tag": CODE_FIX_TAG,
        "capital": {
            "STAKE_USDT": STAKE_USDT,
            "TP_USDT": TP_USDT,
            "SL_USDT": SL_USDT,
            "MAX_CONCURRENT_SLOTS": MAX_CONCURRENT_SLOTS,
            "LEVERAGE": LEVERAGE,
            "STAKE_IS_MARGIN": STAKE_IS_MARGIN,
            "TP_SL_BASIS": TP_SL_BASIS,
            "CAPITAL_CONFIRMED": CAPITAL_CONFIRMED,
            "INITIAL_CAPITAL_USDT": INITIAL_CAPITAL_USDT,
            "notional_for_stake": notional_for_stake(),
        },
        "costs": {
            "FEE_RATE_ONE_SIDE": FEE_RATE_ONE_SIDE,
            "SLIPPAGE_ADVERSE": SLIPPAGE_ADVERSE,
            "SIMULATION_ASSUMPTIONS": SIMULATION_ASSUMPTIONS,
        },
        "universe": {
            "MIN_QUOTE_VOLUME_USDT": MIN_QUOTE_VOLUME_USDT,
            "MAX_SPREAD": MAX_SPREAD,
            "QUOTE_MAX_AGE_SEC": QUOTE_MAX_AGE_SEC,
            "HARD_EXCLUDE_SYMBOLS": sorted(HARD_EXCLUDE_SYMBOLS),
        },
        "depth": {
            "DEPTH_GATE_CONFIRMED": DEPTH_GATE_CONFIRMED,
            "DEPTH_NEAR_TOUCH_PCT": DEPTH_NEAR_TOUCH_PCT,
            "MIN_NEAR_TOUCH_BID_NOTIONAL_USDT": MIN_NEAR_TOUCH_BID_NOTIONAL_USDT,
            "MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT": MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT,
        },
        "entry": {
            "LIMIT_ENTRY_OFFSET_PCT": LIMIT_ENTRY_OFFSET_PCT,
            "CANCEL_DROP_FROM_P_PCT": CANCEL_DROP_FROM_P_PCT,
            "LIMIT_ENTRY_TTL_SEC": LIMIT_ENTRY_TTL_SEC,
            "TTL_POLICY": TTL_POLICY,
        },
        "signal": {
            "KLINE_INTERVAL": KLINE_INTERVAL,
            "QUIET_BARS": QUIET_BARS,
            "RANGE_AMP_MAX": RANGE_AMP_MAX,
            "BAR_AMP_MAX": BAR_AMP_MAX,
            "VOL_EXIT_FLOOR": VOL_EXIT_FLOOR,
            "settings_tag": "NEW_OBSERVATION_SETTINGS",
            "note_historical": (
                "BAR_AMP_MAX/RANGE_AMP_MAX here are CURRENT config (0.0075 / 0.015) for new observation. "
                "Historical evidence only supports main-session BAR upper bound >=~0.00952 compatible with 0.01; "
                "do NOT backfill this snapshot as historical proven BAR=0.01. RANGE historical effective=UNKNOWN."
            ),
        },
        "loop": {
            "MANAGE_DUE_SEC": MANAGE_DUE_SEC,
            "SCAN_SECOND": SCAN_SECOND,
            "UNIVERSE_REFRESH_MINUTES": UNIVERSE_REFRESH_MINUTES,
            "TZ_NAME": TZ_NAME,
        },
    }


def notional_for_stake() -> Optional[float]:
    if not capital_ready():
        return None
    if STAKE_IS_MARGIN:
        return float(STAKE_USDT) * float(LEVERAGE)
    return float(STAKE_USDT)


def margin_for_stake() -> Optional[float]:
    if not capital_ready():
        return None
    if STAKE_IS_MARGIN:
        return float(STAKE_USDT)
    return float(STAKE_USDT) / float(LEVERAGE)
