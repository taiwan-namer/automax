"""
breakout_follow_v1 首版模擬預設參數。

明確標記：SIMULATION_ASSUMPTION — 非最佳化、不保證獲利、非交易所實際費率。
"""

from __future__ import annotations

import os
from pathlib import Path

STRATEGY_ID = "breakout_follow_v1"
STRATEGY_VERSION = "breakout_follow_v1"
CONFIG_HASH_ALGO = "stable_v1"  # frozenset→sorted list；不重寫舊策略 hash
# PROFILE_ID 決定輸出／帳戶／vault 根路徑；預設與 STRATEGY_ID 相同（正式基底）
PROFILE_ID = "breakout_follow_v1"
ACTIVE_PROFILE = "breakout_follow_v1"
# 獨立 runtime 根（絕對路徑）。未設則以進程 cwd 解析相對路徑。
# 長跑必須設定 BREAKOUT_RUNTIME_ROOT，避免 cwd 漂移寫錯目錄。
RUNTIME_ROOT = Path(os.environ["BREAKOUT_RUNTIME_ROOT"]).resolve() if os.environ.get("BREAKOUT_RUNTIME_ROOT") else Path(".").resolve()

# 路徑（依 PROFILE_ID；舊 breakout_follow_v1 帳戶與 vault 不得被其他 profile 覆寫）
ROOT_OUT = RUNTIME_ROOT / "output" / PROFILE_ID
ACCOUNT_PATH = ROOT_OUT / "account_state.json"
RUN_MANIFEST_PATH = ROOT_OUT / "run_manifest.json"
DECISION_TRACE_PATH = ROOT_OUT / "decision_trace.jsonl"
FILLS_PATH = ROOT_OUT / "fills.jsonl"
TRADES_PATH = ROOT_OUT / "trades.csv"
EQUITY_PATH = ROOT_OUT / "equity_snapshots.jsonl"
REVIEW_PATH = ROOT_OUT / "review.md"
UNIVERSE_PATH = ROOT_OUT / "universe_snapshot.json"
PID_PATH = ROOT_OUT / "paper.pid"
LOCK_PATH = ROOT_OUT / "paper_profile.lock"
# 舊污染 vault（keep-first 未收盤快照）封存於 data/breakout_follow_v1/kline_vault，勿覆寫。
KLINE_VAULT_DIR = RUNTIME_ROOT / "data" / PROFILE_ID / "kline_vault_v2_clean"
KLINE_VAULT_LEGACY_SEALED = RUNTIME_ROOT / "data" / "breakout_follow_v1" / "kline_vault"
DECISION_SNAPSHOT_DIR = ROOT_OUT / "decision_snapshots"
# 帳務遷移版本（cash 計入 gross）；與交易參數無關
ACCOUNTING_MIGRATION_VERSION = "cash_gross_v1"
CODE_FIX_TAG = "fix_20261119_mgmt_dl5000_batchside1"
# reverse fixed-exit flags（預設關閉；profile overlay 開啟）
SIGNAL_DIRECTION_FLIP = False
FIXED_TAKE_PROFIT_ENABLED = False
ATR_TP_MULT = 0.0
# 固定毛額退出（opt-in；預設關閉＝維持 ATR 幾何）
FIXED_GROSS_EXIT_ENABLED = False
FIXED_GROSS_TP_USDT = 0.0
FIXED_GROSS_TP2_USDT = 0.0
FIXED_GROSS_SL_USDT = 0.0
# 版本隔離出場計畫（僅新開倉／新掛單戳記；舊倉依 pos.exit_plan_id）
EXIT_PLAN_ID = ""  # legacy if empty
EXIT_PLAN_LEGACY = "legacy_fixed_gross_tp50_sl75"
EXIT_PLAN_B_PARTIAL70 = "b_partial70_30_tp50_tp100_20261120"
EXIT_PLAN_C_PYRAMID = "c_pyramid300_r2_p0tp1pct_20261120"
PARTIAL_TP_USE_PRICE_BARRIER = False  # B70: 觸價 TP50 而非淨額門檻
PYRAMID_ENABLED = False
PYRAMID_MAX_LAYERS = 3
PYRAMID_ADD_PCT = 0.005
PYRAMID_P0_TP_PCT = 0.01  # 相對 P0 有利 1%
ALLOW_PROFILE_SIZE_OVERRIDE = False
# 限價回撤進場（opt-in）：訊號參考價 ±OFFSET；觸價成交
# 不作時間逾時；盯盤至參考價不利偏離 ≥ CANCEL_ADVERSE_PCT 才作廢（0=關閉該條件）
LIMIT_ENTRY_ENABLED = False
LIMIT_ENTRY_OFFSET_PCT = 0.005
LIMIT_ENTRY_TTL_SEC = 0.0  # 0＝不用時間作廢
LIMIT_ENTRY_CANCEL_ADVERSE_PCT = 0.01  # 相對 signal_ref 不利走 1% 作廢
# 鏡像進場（預設關；C 自掃時必須維持 False）
MIRROR_ONLY_ENTRIES = False
MIRROR_SOURCE_PROFILE = ""
ENABLE_PARTIAL_TP = True
ENABLE_BREAKEVEN = True
ENABLE_TRAILING_STOP = True
ENABLE_REVERSE_REOPEN = True
NEW_ENTRIES_UNTIL_ISO = ""
# Research four-arm only ("" = production/default path unchanged)
RESEARCH_ARM = ""  # B0|B1|B2|B3 when set by isolated research runner
EXCHANGE_ORDERS_HARD_DISABLED = False  # research Paper sets True


def set_runtime_root(root: Path | str) -> Path:
    """固定 runtime 根為絕對路徑，並重綁輸出／vault。"""
    global RUNTIME_ROOT
    RUNTIME_ROOT = Path(root).resolve()
    _rebind_profile_paths()
    return RUNTIME_ROOT


def _rebind_profile_paths() -> None:
    """依 RUNTIME_ROOT + PROFILE_ID 重綁所有輸出／vault／鎖路徑（一律絕對）。"""
    global ROOT_OUT, ACCOUNT_PATH, RUN_MANIFEST_PATH, DECISION_TRACE_PATH
    global FILLS_PATH, TRADES_PATH, EQUITY_PATH, REVIEW_PATH, UNIVERSE_PATH, PID_PATH, LOCK_PATH
    global KLINE_VAULT_DIR, DECISION_SNAPSHOT_DIR
    root = Path(RUNTIME_ROOT).resolve()
    ROOT_OUT = (root / "output" / PROFILE_ID).resolve()
    ACCOUNT_PATH = ROOT_OUT / "account_state.json"
    RUN_MANIFEST_PATH = ROOT_OUT / "run_manifest.json"
    DECISION_TRACE_PATH = ROOT_OUT / "decision_trace.jsonl"
    FILLS_PATH = ROOT_OUT / "fills.jsonl"
    TRADES_PATH = ROOT_OUT / "trades.csv"
    EQUITY_PATH = ROOT_OUT / "equity_snapshots.jsonl"
    REVIEW_PATH = ROOT_OUT / "review.md"
    UNIVERSE_PATH = ROOT_OUT / "universe_snapshot.json"
    PID_PATH = ROOT_OUT / "paper.pid"
    LOCK_PATH = ROOT_OUT / "paper_profile.lock"
    KLINE_VAULT_DIR = (root / "data" / PROFILE_ID / "kline_vault_v2_clean").resolve()
    DECISION_SNAPSHOT_DIR = ROOT_OUT / "decision_snapshots"


def apply_profile(profile_name: str) -> dict:
    """套用 paper profile（僅資金／路徑／持倉上限 overlay；不改訊號門檻邏輯）。

    切回 breakout_follow_v1 時強制還原 LEGACY 基準（10000／3／日損300），
    避免 bulk 或 daily2000 污染舊 profile 預設。
    """
    global PROFILE_ID, ACTIVE_PROFILE, CODE_FIX_TAG
    global INITIAL_CAPITAL_USDT, MAX_CONCURRENT_POSITIONS, DAILY_LOSS_LOCK_USDT
    global MARGIN_PER_TRADE_USDT, LEVERAGE, NOTIONAL_USDT, MAX_LOSS_PER_TRADE_USDT, MAX_PER_SYMBOL
    global COOLDOWN_MINUTES, REVERSE_WINDOW_MINUTES, MAX_REVERSES_PER_WINDOW
    global PARTIAL_TP_FRACTION, PARTIAL_TP_NET_USDT, TRAIL_ATR_MULT, TOP_N, ATR_STOP_MULT, VOL_X_MIN
    global SIGNAL_DIRECTION_FLIP, FIXED_TAKE_PROFIT_ENABLED, ATR_TP_MULT
    global FIXED_GROSS_EXIT_ENABLED, FIXED_GROSS_TP_USDT, FIXED_GROSS_TP2_USDT, FIXED_GROSS_SL_USDT
    global LIMIT_ENTRY_ENABLED, LIMIT_ENTRY_OFFSET_PCT, LIMIT_ENTRY_TTL_SEC
    global LIMIT_ENTRY_CANCEL_ADVERSE_PCT
    global MIRROR_ONLY_ENTRIES, MIRROR_SOURCE_PROFILE
    global ENABLE_PARTIAL_TP, ENABLE_BREAKEVEN, ENABLE_TRAILING_STOP, ENABLE_REVERSE_REOPEN
    global NEW_ENTRIES_UNTIL_ISO
    global EXIT_PLAN_ID, PARTIAL_TP_USE_PRICE_BARRIER
    global PYRAMID_ENABLED, PYRAMID_MAX_LAYERS, PYRAMID_ADD_PCT, PYRAMID_P0_TP_PCT
    global ALLOW_PROFILE_SIZE_OVERRIDE
    global MAKER_EXECUTION_ENABLED, MAKER_FEE_RATE, TAKER_FEE_RATE
    global MAKER_ENTRY_PRICE_MODE, MAKER_REQUOTE_POLICY, MAKER_TTL_POLICY
    global MAKER_ENTRY_TTL_POLICY, MAKER_ADD_TTL_POLICY
    global MAKER_ORDER_TTL_SEC, MAKER_PROFIT_EXIT_POLICY, MAKER_ALLOW_TAKER_FALLBACK
    global MAKER_PROFIT_REJECT_POLICY, MAKER_QUOTE_MAX_AGE_SEC, MAKER_CONFIRM_CANCEL_ADVERSE
    global MAKER_FILL_EVENT_SOURCE, MAKER_P0_TP_LIQUIDITY_ROLE, MAKER_R2_LIQUIDITY_ROLE

    from .profiles import PROFILES

    if profile_name not in PROFILES:
        raise ValueError(f"unknown profile: {profile_name}; known={sorted(PROFILES)}")

    ACTIVE_PROFILE = profile_name
    overlay = PROFILES[profile_name]

    # 共用「沿用」核對（名義／槓桿／單筆風險／cooldown／ATR停損倍數等）
    # MAX_REVERSES_PER_WINDOW 允許反向實驗設 0；PARTIAL 數值可保留但由 ENABLE_* 關閉行為
    allow_size = bool(overlay.get("ALLOW_PROFILE_SIZE_OVERRIDE", False))
    expected_same = {
        "LEVERAGE": 10.0,
        "MAX_LOSS_PER_TRADE_USDT": 100.0,
        "MAX_PER_SYMBOL": 1,
        "COOLDOWN_MINUTES": 3,
        "REVERSE_WINDOW_MINUTES": 30,
        "TRAIL_ATR_MULT": 1.5,
        "TOP_N": 10,
        "ATR_STOP_MULT": 1.5,
        "VOL_X_MIN": 3.0,
    }
    if not allow_size:
        expected_same.update({
            "MARGIN_PER_TRADE_USDT": 1_000.0,
            "NOTIONAL_USDT": 10_000.0,
            "PARTIAL_TP_FRACTION": 0.30,
            "PARTIAL_TP_NET_USDT": 100.0,
        })
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
    CODE_FIX_TAG = str(overlay.get("CODE_FIX_TAG") or CODE_FIX_TAG)
    SIGNAL_DIRECTION_FLIP = bool(overlay.get("SIGNAL_DIRECTION_FLIP", False))
    FIXED_TAKE_PROFIT_ENABLED = bool(overlay.get("FIXED_TAKE_PROFIT_ENABLED", False))
    ATR_TP_MULT = float(overlay.get("ATR_TP_MULT", 0.0) or 0.0)
    FIXED_GROSS_EXIT_ENABLED = bool(overlay.get("FIXED_GROSS_EXIT_ENABLED", False))
    FIXED_GROSS_TP_USDT = float(overlay.get("FIXED_GROSS_TP_USDT", 0.0) or 0.0)
    FIXED_GROSS_TP2_USDT = float(overlay.get("FIXED_GROSS_TP2_USDT", 0.0) or 0.0)
    FIXED_GROSS_SL_USDT = float(overlay.get("FIXED_GROSS_SL_USDT", 0.0) or 0.0)
    EXIT_PLAN_ID = str(overlay.get("EXIT_PLAN_ID") or "")
    PARTIAL_TP_USE_PRICE_BARRIER = bool(overlay.get("PARTIAL_TP_USE_PRICE_BARRIER", False))
    PYRAMID_ENABLED = bool(overlay.get("PYRAMID_ENABLED", False))
    PYRAMID_MAX_LAYERS = int(overlay.get("PYRAMID_MAX_LAYERS", 3) or 3)
    PYRAMID_ADD_PCT = float(overlay.get("PYRAMID_ADD_PCT", 0.005) or 0.005)
    PYRAMID_P0_TP_PCT = float(overlay.get("PYRAMID_P0_TP_PCT", 0.01) or 0.01)
    ALLOW_PROFILE_SIZE_OVERRIDE = bool(overlay.get("ALLOW_PROFILE_SIZE_OVERRIDE", False))
    LIMIT_ENTRY_ENABLED = bool(overlay.get("LIMIT_ENTRY_ENABLED", False))
    LIMIT_ENTRY_OFFSET_PCT = float(overlay.get("LIMIT_ENTRY_OFFSET_PCT", 0.005) or 0.005)
    LIMIT_ENTRY_TTL_SEC = float(overlay.get("LIMIT_ENTRY_TTL_SEC", 0.0) or 0.0)
    LIMIT_ENTRY_CANCEL_ADVERSE_PCT = float(
        overlay.get("LIMIT_ENTRY_CANCEL_ADVERSE_PCT", 0.01) or 0.0
    )
    MIRROR_ONLY_ENTRIES = bool(overlay.get("MIRROR_ONLY_ENTRIES", False))
    MIRROR_SOURCE_PROFILE = str(overlay.get("MIRROR_SOURCE_PROFILE") or "")
    ENABLE_PARTIAL_TP = bool(overlay.get("ENABLE_PARTIAL_TP", True))
    ENABLE_BREAKEVEN = bool(overlay.get("ENABLE_BREAKEVEN", True))
    ENABLE_TRAILING_STOP = bool(overlay.get("ENABLE_TRAILING_STOP", True))
    ENABLE_REVERSE_REOPEN = bool(overlay.get("ENABLE_REVERSE_REOPEN", True))
    NEW_ENTRIES_UNTIL_ISO = str(overlay.get("NEW_ENTRIES_UNTIL_ISO") or "")
    MAKER_EXECUTION_ENABLED = bool(overlay.get("MAKER_EXECUTION_ENABLED", False))
    if "MAKER_FEE_RATE" in overlay and overlay.get("MAKER_FEE_RATE") is not None:
        MAKER_FEE_RATE = float(overlay["MAKER_FEE_RATE"])
    if "TAKER_FEE_RATE" in overlay and overlay.get("TAKER_FEE_RATE") is not None:
        TAKER_FEE_RATE = float(overlay["TAKER_FEE_RATE"])
    MAKER_ENTRY_PRICE_MODE = overlay.get("MAKER_ENTRY_PRICE_MODE", None)
    MAKER_REQUOTE_POLICY = overlay.get("MAKER_REQUOTE_POLICY", None)
    MAKER_TTL_POLICY = overlay.get("MAKER_TTL_POLICY", None)
    MAKER_ENTRY_TTL_POLICY = overlay.get("MAKER_ENTRY_TTL_POLICY", None)
    MAKER_ADD_TTL_POLICY = overlay.get("MAKER_ADD_TTL_POLICY", None)
    MAKER_ORDER_TTL_SEC = overlay.get("MAKER_ORDER_TTL_SEC", None)
    MAKER_CONFIRM_CANCEL_ADVERSE = bool(overlay.get("MAKER_CONFIRM_CANCEL_ADVERSE", False))
    MAKER_PROFIT_EXIT_POLICY = overlay.get("MAKER_PROFIT_EXIT_POLICY", None)
    MAKER_PROFIT_REJECT_POLICY = overlay.get("MAKER_PROFIT_REJECT_POLICY", None)
    MAKER_QUOTE_MAX_AGE_SEC = overlay.get("MAKER_QUOTE_MAX_AGE_SEC", None)
    MAKER_ALLOW_TAKER_FALLBACK = bool(overlay.get("MAKER_ALLOW_TAKER_FALLBACK", False))
    MAKER_FILL_EVENT_SOURCE = overlay.get("MAKER_FILL_EVENT_SOURCE", "paper_model")
    MAKER_P0_TP_LIQUIDITY_ROLE = overlay.get("MAKER_P0_TP_LIQUIDITY_ROLE", None)
    MAKER_R2_LIQUIDITY_ROLE = overlay.get("MAKER_R2_LIQUIDITY_ROLE", None)
    _rebind_profile_paths()
    return {
        "profile": profile_name,
        "PROFILE_ID": PROFILE_ID,
        "RUNTIME_ROOT": str(Path(RUNTIME_ROOT).resolve()),
        "ROOT_OUT": str(ROOT_OUT),
        "ACCOUNT_PATH": str(ACCOUNT_PATH),
        "KLINE_VAULT_DIR": str(KLINE_VAULT_DIR),
        "LOCK_PATH": str(LOCK_PATH),
        "INITIAL_CAPITAL_USDT": INITIAL_CAPITAL_USDT,
        "MAX_CONCURRENT_POSITIONS": MAX_CONCURRENT_POSITIONS,
        "DAILY_LOSS_LOCK_USDT": DAILY_LOSS_LOCK_USDT,
        "CODE_FIX_TAG": CODE_FIX_TAG,
        "SIGNAL_DIRECTION_FLIP": SIGNAL_DIRECTION_FLIP,
        "FIXED_TAKE_PROFIT_ENABLED": FIXED_TAKE_PROFIT_ENABLED,
        "ATR_TP_MULT": ATR_TP_MULT,
        "ATR_STOP_MULT": ATR_STOP_MULT,
        "FIXED_GROSS_EXIT_ENABLED": FIXED_GROSS_EXIT_ENABLED,
        "FIXED_GROSS_TP_USDT": FIXED_GROSS_TP_USDT,
        "FIXED_GROSS_TP2_USDT": FIXED_GROSS_TP2_USDT,
        "FIXED_GROSS_SL_USDT": FIXED_GROSS_SL_USDT,
        "EXIT_PLAN_ID": EXIT_PLAN_ID,
        "PARTIAL_TP_USE_PRICE_BARRIER": PARTIAL_TP_USE_PRICE_BARRIER,
        "PYRAMID_ENABLED": PYRAMID_ENABLED,
        "PYRAMID_MAX_LAYERS": PYRAMID_MAX_LAYERS,
        "PYRAMID_ADD_PCT": PYRAMID_ADD_PCT,
        "PYRAMID_P0_TP_PCT": PYRAMID_P0_TP_PCT,
        "ALLOW_PROFILE_SIZE_OVERRIDE": ALLOW_PROFILE_SIZE_OVERRIDE,
        "LIMIT_ENTRY_ENABLED": LIMIT_ENTRY_ENABLED,
        "LIMIT_ENTRY_OFFSET_PCT": LIMIT_ENTRY_OFFSET_PCT,
        "LIMIT_ENTRY_TTL_SEC": LIMIT_ENTRY_TTL_SEC,
        "LIMIT_ENTRY_CANCEL_ADVERSE_PCT": LIMIT_ENTRY_CANCEL_ADVERSE_PCT,
        "MIRROR_ONLY_ENTRIES": MIRROR_ONLY_ENTRIES,
        "MIRROR_SOURCE_PROFILE": MIRROR_SOURCE_PROFILE,
        "ENABLE_PARTIAL_TP": ENABLE_PARTIAL_TP,
        "ENABLE_BREAKEVEN": ENABLE_BREAKEVEN,
        "ENABLE_TRAILING_STOP": ENABLE_TRAILING_STOP,
        "ENABLE_REVERSE_REOPEN": ENABLE_REVERSE_REOPEN,
        "NEW_ENTRIES_UNTIL_ISO": NEW_ENTRIES_UNTIL_ISO,
        "NOTIONAL_USDT": NOTIONAL_USDT,
        "MARGIN_PER_TRADE_USDT": MARGIN_PER_TRADE_USDT,
        "MAX_LOSS_PER_TRADE_USDT": MAX_LOSS_PER_TRADE_USDT,
        "MAKER_EXECUTION_ENABLED": MAKER_EXECUTION_ENABLED,
        "MAKER_FEE_RATE": MAKER_FEE_RATE,
        "TAKER_FEE_RATE": TAKER_FEE_RATE,
        "MAKER_ENTRY_PRICE_MODE": MAKER_ENTRY_PRICE_MODE,
        "MAKER_REQUOTE_POLICY": MAKER_REQUOTE_POLICY,
        "MAKER_TTL_POLICY": MAKER_TTL_POLICY,
        "MAKER_ENTRY_TTL_POLICY": MAKER_ENTRY_TTL_POLICY,
        "MAKER_ADD_TTL_POLICY": MAKER_ADD_TTL_POLICY,
        "MAKER_ORDER_TTL_SEC": MAKER_ORDER_TTL_SEC,
        "MAKER_CONFIRM_CANCEL_ADVERSE": MAKER_CONFIRM_CANCEL_ADVERSE,
        "MAKER_PROFIT_EXIT_POLICY": MAKER_PROFIT_EXIT_POLICY,
        "MAKER_PROFIT_REJECT_POLICY": MAKER_PROFIT_REJECT_POLICY,
        "MAKER_QUOTE_MAX_AGE_SEC": MAKER_QUOTE_MAX_AGE_SEC,
        "MAKER_ALLOW_TAKER_FALLBACK": MAKER_ALLOW_TAKER_FALLBACK,
        "MAKER_FILL_EVENT_SOURCE": MAKER_FILL_EVENT_SOURCE,
        "MAKER_P0_TP_LIQUIDITY_ROLE": MAKER_P0_TP_LIQUIDITY_ROLE,
        "MAKER_R2_LIQUIDITY_ROLE": MAKER_R2_LIQUIDITY_ROLE,
    }

# 資金與風險（預設＝舊 profile 基準 10000／3／日損300；bulk 由 apply_profile 覆蓋）
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

# C Maker-exec（預設關閉；僅獨立 profile overlay 開啟）
MAKER_EXECUTION_ENABLED = False
MAKER_FEE_RATE = 0.0002
TAKER_FEE_RATE = 0.0005
MAKER_ENTRY_PRICE_MODE = None  # "best_bid_ask" when enabled
MAKER_REQUOTE_POLICY = None
MAKER_TTL_POLICY = None  # legacy alias
MAKER_ENTRY_TTL_POLICY = None
MAKER_ADD_TTL_POLICY = None
MAKER_ORDER_TTL_SEC = None
MAKER_CONFIRM_CANCEL_ADVERSE = False
MAKER_PROFIT_EXIT_POLICY = None
MAKER_PROFIT_REJECT_POLICY = None
MAKER_QUOTE_MAX_AGE_SEC = None
MAKER_ALLOW_TAKER_FALLBACK = False
MAKER_FILL_EVENT_SOURCE = "paper_model"
MAKER_P0_TP_LIQUIDITY_ROLE = None
MAKER_R2_LIQUIDITY_ROLE = None
MAKER_EXEC_EVENTS_PATH_NAME = "c_maker_exec_events.jsonl"

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
MANAGE_POLL_SEC = 5.0  # 持倉管理輪詢上限（與 MANAGE_DUE_SEC 對齊意圖）
TZ_NAME = "Asia/Taipei"

# 公開行情 HTTP 可靠性（不提高既有並發／速率；僅退避與預算）
HTTP_REQUEST_TIMEOUT_SEC = 15.0  # 單次嘗試
HTTP_MAX_ATTEMPTS = 5
HTTP_OP_BUDGET_SEC = 45.0  # 單次 get_json 總耗時上限（掃描／一般）
HTTP_BACKOFF_BASE_SEC = 0.5
HTTP_BACKOFF_MAX_SEC = 30.0
HTTP_BACKOFF_JITTER_FRAC = 0.25
# 注意：無跨程序全域限流；並行舊程序仍有獨立請求壓力

# --- 管理可靠性（離線實作基準；部署另批）---
# 不改止損／TP／反手／倉位／日損參數。不合入 N/T 健康禁止開倉政策。
MANAGE_DUE_SEC = 5.0  # next_manage_due = attempt_start + 此值（單調鐘）
MANAGE_BOOK_REQUEST_TIMEOUT_SEC = 5.0
MANAGE_BOOK_MAX_ATTEMPTS = 2
MANAGE_BOOK_OP_BUDGET_SEC = 10.0
SCAN_SLICE_MAX_WAIT_SEC = 2.0  # 單次掃描 HTTP 切片等待；逾時取消該呼叫

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
