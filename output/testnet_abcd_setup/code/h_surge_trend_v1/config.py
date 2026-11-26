"""H price_action_v2 config. Do not retune without a new version tag.

Entry signal paths A/B/C live here; risk/sizing/rank rules kept from v1 values.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

STRATEGY_ID = "h_price_action_v2"
STRATEGY_VERSION = "h_price_action_v2_20261126"
CODE_FIX_TAG = "h_v2_abc_paths+sparse_1s+entry_gate"
PROFILE_PARENT = "paper_h_price_action_v2"
PROFILE_ID = "paper_h_price_action_v2_ledger"
# Testnet isolated profiles (set via launcher env; never share E ledgers)
TESTNET_PROFILE_PARENT = "testnet_h_price_action_v2"
TESTNET_PROFILE_ID = "testnet_h_price_action_v2_ledger"
BRIDGE_STRATEGY = "H"  # clientOrderId prefix STRAT_H_

RUNTIME_ROOT = (
    Path(os.environ["BREAKOUT_RUNTIME_ROOT"]).resolve()
    if os.environ.get("BREAKOUT_RUNTIME_ROOT")
    else Path(".").resolve()
)

TZ_NAME = "Asia/Taipei"

# --- Capital / slots (fixed v1) ---
INITIAL_CAPITAL_USDT = 5_000.0
MARGIN_PER_TRADE_USDT = 30.0
LEVERAGE = 10.0
NOTIONAL_USDT = 300.0
MAX_LEGS_PER_SYMBOL = 4
MAX_SLOTS = 20  # one slot per symbol direction; 4 legs share one slot
ADD_STEP_PCT = 0.005  # 0.5% from last fill avg for add

# --- Ranking / tracking ---
RANK_REFRESH_SEC = 300.0  # 5 minutes
TOP_ENTER = 10  # 納入訊號追蹤（原 v1 規格 Top5；使用者改 Top10）
TOP_KEEP = 15   # 已納入者在此名次內持續追蹤；掉出則停新開／加倉
RANK_STALE_STOP_NEW_SEC = 600.0  # 10 minutes
REQUIRE_POSITIVE_CHANGE = True  # rank by 24h pct; still sort all eligible; Top by pct desc

# Fixed exclude bases — exact baseAsset match only (no substring)
EXCLUDED_BASE_ASSETS: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "BNB",
        "SOL",
        "XRP",
        "DOGE",
        "ADA",
        "TRX",
        "AVAX",
        "LINK",
        "TON",
        "DOT",
    }
)

STABLE_BASES = frozenset(
    {
        "USDC",
        "FDUSD",
        "TUSD",
        "USDP",
        "DAI",
        "BUSD",
        "USD1",
        "USDE",
        "EUR",
        "AEUR",
    }
)

# Wrapped / staked / derivative bases — exact match only
WRAPPED_STAKED_BASES = frozenset(
    {
        "WBTC",
        "WETH",
        "STETH",
        "WSTETH",
        "RETH",
        "CBETH",
        "WEETH",
        "BETH",
        "WBETH",
        "ETHX",
        "RSETH",
        "EZETH",
        "METH",
        "MSOL",
        "JITOSOL",
        "BNSOL",
        "BBTC",
        "TBTC",
        "LBTC",
        "SOLVBTC",
    }
)

LEVERAGE_TOKEN_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

NON_CRYPTO_SUBTYPE_KEYWORDS = frozenset(
    {"Equity", "Index", "Commodity", "Pre-Market", "Forex", "TradFi", "Stock"}
)
NON_CRYPTO_UNDERLYING_TYPES = frozenset({"INDEX", "PREMARKET", "EQUITY", "COMMODITY", "FOREX"})
CRYPTO_UNDERLYING_TYPES = frozenset({"COIN", "CRYPTO", ""})  # empty handled as unknown→exclude
TRADFI_BASE_BLACKLIST = frozenset(
    {
        "AAPL",
        "AMZN",
        "AUD",
        "CAD",
        "CHF",
        "COIN",
        "EUR",
        "GBP",
        "GOOG",
        "GOOGL",
        "JPY",
        "META",
        "MSFT",
        "MSTR",
        "NVDA",
        "NZD",
        "PAXG",
        "QQQ",
        "SPY",
        "TSLA",
        "USD",
        "XAG",
        "XAU",
        "XAUT",
    }
)
TRADFI_SYMBOL_PREFIXES = ("XAU", "XAG", "PAXG", "XAUT")

# --- 3m surge ---
BAR_SEC = 180
RVOL_LOOKBACK_BARS = 20
RVOL_MIN_ELAPSED_SEC = 30.0
RVOL_THRESHOLD = 3.0
RVOL_ABS_FRAC_OF_BASELINE = 0.5
# Continuation arm after origin surge: bars T+3m and T+6m use frozen baseline
# v2: keep code for rollback; DEFAULT OFF — not a 4th entry path
ENABLE_CONTINUATION_ARM = False
CONTINUATION_BARS = 2
CONTINUATION_RVOL_THRESHOLD = 1.5
CONTINUATION_WINDOW_BARS = 3  # source + 2 continuation → expires at T+9m

# --- Direction / chop filter (Path A / legacy dense grid) ---
ER_WINDOW_SEC = 60
ER_MIN = 0.35
RANGE_MIN_PCT = 0.003  # 0.30%
RET_60S_LONG_MIN = 0.002  # +0.20%
RET_60S_SHORT_MAX = -0.002
CLV_LONG_MIN = 0.70
CLV_SHORT_MAX = 0.30
# Freshness: v2 requires <=2s; prior v1 was 3s (looser) → adopt stricter 2s
TRADE_STALE_SEC = 2.0
TRADE_STALE_SEC_LEGACY = 3.0  # audit only; not used for decisions

# --- price_action_v2 path B/C + shared ---
V2_COVERAGE_MIN = 0.80
V2_MAX_TRADE_AGE_SEC = 2.0  # must be <= TRADE_STALE_SEC
V2_B_RET60 = 0.0035
V2_B_RET15 = 0.0010
V2_B_ER30 = 0.35
V2_B_CLV_LONG = 0.75
V2_B_CLV_SHORT = 0.25
V2_B_PULLBACK_MAX = 0.0020
V2_B_STREAK_SEC = 3
V2_C_TAU_MIN_SEC = 5
V2_C_TAU_MAX_SEC = 60
V2_C_PRE_MOVE = 0.0040
V2_C_RECLAIM = 0.0025
V2_C_RECLAIM_RATIO = 0.50
V2_C_RET10 = 0.0010
V2_C_ER10 = 0.45
V2_C_CONFIRM_EXTRA_SEC = 2  # after trigger second → 2 more = 3s confirm
V2_CHOP_BAND = 0.0010  # 0.10%
V2_CHOP_FLIP_MIN = 4
V2_COOLDOWN_AFTER_EXIT_SEC = 15.0
V2_MAX_FIRST_ENTRY_ATTEMPTS_180S = 2
V2_DIAG_THROTTLE_SEC = 1.0  # disk throttle; evaluation still every complete second

# --- Upper-wick reverse SHORT / lower-wick reverse LONG (symmetric) ---
WICK_FRAC_MIN = 0.40
PULLBACK_FROM_HIGH_MIN = 0.005  # 0.50%
REBOUND_FROM_LOW_MIN = 0.005  # 0.50%
RET_15S_MAX = -0.002
RET_15S_MIN = 0.002  # +0.20% for lower-wick reverse LONG
CLV_REVERSE_SHORT_MAX = 0.40
CLV_REVERSE_LONG_MIN = 0.60

# --- Orders ---
ENTRY_ORDER_TYPE = "MARKET"  # MARKET = no resting entry; LIMIT = ask/bid TTL queue
ENTRY_TTL_SEC = 5.0  # only used when ENTRY_ORDER_TYPE == LIMIT
ENTRY_POST_ONLY = False
EXIT_REQUOTE_SEC = 2.0
EXIT_ALARM_SEC = 10.0
QUOTE_MAX_AGE_SEC = 5.0
MAX_SPREAD = 0.002

# --- Trailing ---
TRAIL_PCT = 0.01  # 1%
PROFIT_MARK_PCT = 0.02  # 2% gross price return

# --- Fees (paper assumptions; same numeric family as E) ---
MAKER_FEE_RATE = 0.0002
TAKER_FEE_RATE = 0.0005

SIMULATION_ASSUMPTIONS: dict[str, Any] = {
    "entry_fill_model": (
        "paper_market_v1: ENTRY_ORDER_TYPE=MARKET → immediate full fill at ask (LONG) / bid (SHORT), "
        "taker fee; no resting entry order / no TTL. "
        "LIMIT mode (if re-enabled): BUY fills only if fresh ask<=limit within TTL at limit price; "
        "SELL fills only if fresh bid>=limit within TTL at limit price; "
        "no order-book depth / queue; partial fill only when test injects partial_qty; "
        "cancel race: fill events before cancel-ack may still apply once"
    ),
    "exit_fill_model": (
        "paper_exit_limit_v1: LONG exits via SELL@bid; SHORT via BUY@ask; "
        "unfilled after EXIT_REQUOTE_SEC → cancel-ack then reprice remaining; "
        "unfilled ≠ flat; >EXIT_ALARM_SEC emits EXIT_SLOW_ALARM but keeps exit intent"
    ),
    "fee_model": (
        "if fill_ts==place_ts (immediate cross) → taker; if rested >0 → maker; "
        "if role unknown → fee_role=SIMULATION_UNKNOWN and charge taker (conservative); "
        "not exchange actual"
    ),
    "protection_note": (
        "1% trail is LOCAL trigger distance only — not max loss and not exchange stop. "
        "Without exchange-side protection, must NOT mark exchange_protection_active=True"
    ),
    "backtest_note": (
        "No claim of complete historical backtest without intradey trades + historical "
        "rank/tradability evidence. Offline tests use synthetic event streams only."
    ),
    "note": "SIMULATION_ASSUMPTION — separate from exchange fills",
}

# Loop
MANAGE_DUE_SEC = 1.0
SCAN_SECOND = 1
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
ALLOWED_PUBLIC_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/24hr",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/klines",
        "/fapi/v1/aggTrades",
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

HTTP_REQUEST_TIMEOUT_SEC = 15.0
HTTP_MAX_ATTEMPTS = 5
HTTP_OP_BUDGET_SEC = 45.0
KLINES_API_CONCURRENCY = 8
KLINES_MIN_INTERVAL_SEC = 0.05
MANAGE_BOOK_REQUEST_TIMEOUT_SEC = 5.0
MANAGE_BOOK_MAX_ATTEMPTS = 2
MANAGE_BOOK_OP_BUDGET_SEC = 10.0


def set_runtime_root(root: str | Path) -> Path:
    global RUNTIME_ROOT
    RUNTIME_ROOT = Path(root).resolve()
    return RUNTIME_ROOT


def arm_paths(_arm_key: str = "main") -> dict[str, Path]:
    """E-compatible path helper; H uses a single ledger profile."""
    root = RUNTIME_ROOT / "output" / PROFILE_ID
    return {
        "root": root,
        "account": root / "account_state.json",
        "fills": root / "fills.jsonl",
        "trades": root / "trades.csv",
        "equity": root / "equity_snapshots.jsonl",
        "pid": root / "paper.pid",
        "lock": root / "paper_profile.lock",
        "runs": root / "runs",
        "events": root / "events.jsonl",
        "vault": RUNTIME_ROOT / "data" / PROFILE_PARENT / "kline_vault_v2_clean",
    }


def shared_paths() -> dict[str, Path]:
    root = RUNTIME_ROOT / "output" / PROFILE_PARENT
    return {
        "root": root,
        "signals": root / "signals.jsonl",
        "scan_log": root / "scan_events.jsonl",
        "universe": root / "universe_snapshot.json",
        "rank_snapshots": root / "rank_snapshots.jsonl",
        "state": root / "observer_state.json",
        "pid": root / "paper.pid",
        "lock": root / "paper_profile.lock",
        "review": root / "review.md",
        "launch": root / "LAUNCH_META.json",
        "rejects": root / "signal_rejects.jsonl",
        "alerts": root / "alerts.jsonl",
    }


def config_hash() -> str:
    payload = {
        "STRATEGY_VERSION": STRATEGY_VERSION,
        "TOP_ENTER": TOP_ENTER,
        "TOP_KEEP": TOP_KEEP,
        "RVOL_THRESHOLD": RVOL_THRESHOLD,
        "ENABLE_CONTINUATION_ARM": ENABLE_CONTINUATION_ARM,
        "CONTINUATION_RVOL_THRESHOLD": CONTINUATION_RVOL_THRESHOLD,
        "CONTINUATION_BARS": CONTINUATION_BARS,
        "ER_MIN": ER_MIN,
        "TRADE_STALE_SEC": TRADE_STALE_SEC,
        "V2_B_RET60": V2_B_RET60,
        "V2_B_RET15": V2_B_RET15,
        "V2_B_ER30": V2_B_ER30,
        "V2_B_CLV_LONG": V2_B_CLV_LONG,
        "V2_B_CLV_SHORT": V2_B_CLV_SHORT,
        "V2_B_PULLBACK_MAX": V2_B_PULLBACK_MAX,
        "V2_B_STREAK_SEC": V2_B_STREAK_SEC,
        "V2_C_PRE_MOVE": V2_C_PRE_MOVE,
        "V2_C_RECLAIM": V2_C_RECLAIM,
        "V2_C_RECLAIM_RATIO": V2_C_RECLAIM_RATIO,
        "V2_C_RET10": V2_C_RET10,
        "V2_C_ER10": V2_C_ER10,
        "V2_C_CONFIRM_EXTRA_SEC": V2_C_CONFIRM_EXTRA_SEC,
        "V2_CHOP_FLIP_MIN": V2_CHOP_FLIP_MIN,
        "V2_COOLDOWN_AFTER_EXIT_SEC": V2_COOLDOWN_AFTER_EXIT_SEC,
        "V2_MAX_FIRST_ENTRY_ATTEMPTS_180S": V2_MAX_FIRST_ENTRY_ATTEMPTS_180S,
        "TRAIL_PCT": TRAIL_PCT,
        "PROFIT_MARK_PCT": PROFIT_MARK_PCT,
        "NOTIONAL_USDT": NOTIONAL_USDT,
        "MAX_LEGS": MAX_LEGS_PER_SYMBOL,
        "MAX_SLOTS": MAX_SLOTS,
        "ENTRY_ORDER_TYPE": ENTRY_ORDER_TYPE,
        "ENTRY_TTL_SEC": ENTRY_TTL_SEC,
        "EXCLUDED_BASE_ASSETS": sorted(EXCLUDED_BASE_ASSETS),
    }
    raw = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def launch_blockers() -> list[str]:
    blockers: list[str] = [
        "H_V1_OPERATOR_ACK — confirm SPEC freeze before continuous start "
        "(set AUTOMAX_H_ACK_SPEC=1)",
        "TRAIL_SL_EXCHANGE_RISK — Testnet places STOP_MARKET at trail (E-aligned); "
        "1% is still not max-loss guarantee if SL place fails / disconnect "
        "(AUTOMAX_H_ACK_NO_EXCHANGE_STOP=1)",
        "LIMIT_EXIT_ONLY — exits are limit-only with no market fallback; "
        "ack slippage/non-fill risk (AUTOMAX_H_ACK_LIMIT_EXIT=1)",
        "INCOMPLETE_HISTORICAL_RANK_BACKTEST — do not claim full backtest without "
        "historical rank + tradability evidence",
        "TRADE_FEED_REQUIRED — engine without event-time trade feed will not invent "
        "intraday RVOL signals from closed 3m klines; attach feed before claiming live signals",
        "EMPTY_UNDERLYING_TYPE_EXCLUDED — symbols with missing underlyingType are excluded "
        "as CLASSIFICATION_UNKNOWN; verify exchangeInfo coverage before start",
    ]
    if (os.environ.get("AUTOMAX_H_ACK_SPEC") or "").strip() == "1":
        blockers = [b for b in blockers if not b.startswith("H_V1_OPERATOR_ACK")]
        # Paper continuous start: SPEC ack also clears known paper limitations
        # (no invented surge without trade feed; no full historical backtest claim).
        testnet_on = (os.environ.get("ENABLE_BINANCE_TESTNET_ORDER") or "").strip() in (
            "1",
            "true",
            "TRUE",
        )
        if not testnet_on:
            blockers = [
                b
                for b in blockers
                if not b.startswith(
                    (
                        "INCOMPLETE_HISTORICAL_RANK_BACKTEST",
                        "TRADE_FEED_REQUIRED",
                        "EMPTY_UNDERLYING_TYPE_EXCLUDED",
                    )
                )
            ]
    if (os.environ.get("AUTOMAX_H_ACK_NO_EXCHANGE_STOP") or "").strip() == "1":
        blockers = [
            b
            for b in blockers
            if not (
                b.startswith("NO_EXCHANGE_PROTECTION")
                or b.startswith("TRAIL_SL_EXCHANGE_RISK")
            )
        ]
    if (os.environ.get("AUTOMAX_H_ACK_LIMIT_EXIT") or "").strip() == "1":
        blockers = [b for b in blockers if not b.startswith("LIMIT_EXIT_ONLY")]
    if ENTRY_POST_ONLY:
        blockers.append(
            "ENTRY_POST_ONLY=True — may reject crossing limits; do not silently clear"
        )
    if (os.environ.get("ENABLE_BINANCE_TESTNET_ORDER") or "").strip() in ("1", "true", "TRUE"):
        # Testnet allowed after operator ack — Binance wiring aligned with E bridge
        if (os.environ.get("AUTOMAX_H_ACK_TESTNET") or "").strip() != "1":
            blockers.append(
                "TESTNET_ACK_REQUIRED — set AUTOMAX_H_ACK_TESTNET=1 after confirming "
                "STRAT_H_ prefix, lock file, trail SL rehang, limit-exit (no market fallback)"
            )
        # Prefer dedicated env file if present; else shared .env.testnet (same as E path)
        env_override = (os.environ.get("AUTOMAX_TESTNET_ENV_FILE") or "").strip()
        if not env_override:
            blockers.append(
                "AUTOMAX_TESTNET_ENV_FILE unset — launcher should point at .env.testnet.h "
                "or shared .env.testnet (ack by setting the path)"
            )
    return blockers


def apply_testnet_profile_env() -> None:
    """Optional env overlays for profile parent/id (paper or testnet launchers)."""
    global PROFILE_PARENT, PROFILE_ID, STRATEGY_VERSION
    parent = (os.environ.get("AUTOMAX_H_PROFILE_PARENT") or "").strip()
    pid = (os.environ.get("AUTOMAX_H_PROFILE_ID") or "").strip()
    if parent:
        PROFILE_PARENT = parent
    if pid:
        PROFILE_ID = pid
    # Only tag version when Testnet order path is actually enabled
    testnet_on = (os.environ.get("ENABLE_BINANCE_TESTNET_ORDER") or "").strip() in (
        "1",
        "true",
        "TRUE",
    )
    if testnet_on and (parent or pid) and "+testnet_profile" not in STRATEGY_VERSION:
        STRATEGY_VERSION = f"{STRATEGY_VERSION}+testnet_profile"
