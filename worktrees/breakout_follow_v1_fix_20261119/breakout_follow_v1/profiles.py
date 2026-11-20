"""Paper profiles — capital/path overlays only; signal rules stay on base strategy."""

from __future__ import annotations

from typing import Any

BASE_STRATEGY_ID = "breakout_follow_v1"
BASE_STRATEGY_VERSION = "breakout_follow_v1"
DEFAULT_PROFILE = "breakout_follow_v1"

# 舊／預設 profile 明確基準（對齊已部署正式：10000／3／日損300）
LEGACY_PROFILE_BASELINE: dict[str, Any] = {
    "PROFILE_ID": "breakout_follow_v1",
    "INITIAL_CAPITAL_USDT": 10_000.0,
    "MAX_CONCURRENT_POSITIONS": 3,
    "DAILY_LOSS_LOCK_USDT": 300.0,
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
    "CODE_FIX_TAG": "fix_20261119_p0p1_trail_after_partial",  # 舊 profile 維持部署 tag；HTTP429 僅隨程式碼，不改舊帳戶參數
}

PAPER_BULK_TEST_V1: dict[str, Any] = {
    "PROFILE_ID": "paper_bulk_test_v1",
    "PROFILE_PURPOSE": "bulk paper data collection — not profit targeting",
    "INITIAL_CAPITAL_USDT": 50_000.0,
    "MAX_CONCURRENT_POSITIONS": 20,
    "DAILY_LOSS_LOCK_USDT": 2_000.0,
    # 沿用舊基底風控口徑（非日損／本金／倉數）
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
    "CODE_FIX_TAG": "fix_20261119_p0p1_trail_http429_bulk_reverse",
    "REVERSE_POLICY": "approved_full_breakout_reverse_reopen_20261119",
}

PROFILES: dict[str, dict[str, Any]] = {
    DEFAULT_PROFILE: LEGACY_PROFILE_BASELINE,
    "paper_bulk_test_v1": PAPER_BULK_TEST_V1,
}


def list_profiles() -> list[str]:
    return sorted(PROFILES.keys())
