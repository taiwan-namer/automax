"""Paper profiles — capital/path overlays only; signal rules stay on base strategy."""

from __future__ import annotations

from typing import Any

# 基底策略邏輯版本（訊號／K線／停利政策）不變；profile 只改資金路徑與持倉上限。
BASE_STRATEGY_ID = "breakout_follow_v1"
BASE_STRATEGY_VERSION = "breakout_follow_v1"

DEFAULT_PROFILE = "breakout_follow_v1"

# 明確列出「沿用基底」與「本 profile 變更」；禁止 silently override 未核對項。
PAPER_BULK_TEST_V1: dict[str, Any] = {
    "PROFILE_ID": "paper_bulk_test_v1",
    "PROFILE_PURPOSE": "bulk paper data collection — not profit targeting",
    # 變更
    "INITIAL_CAPITAL_USDT": 50_000.0,
    "MAX_CONCURRENT_POSITIONS": 20,
    "DAILY_LOSS_LOCK_USDT": 2_000.0,
    # 沿用基底（核對值；apply 時再 assert）
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
    "TOP_N": 10,  # 不擴大宇宙
    "ATR_STOP_MULT": 1.5,
    "VOL_X_MIN": 3.0,
}

PROFILES: dict[str, dict[str, Any] | None] = {
    DEFAULT_PROFILE: None,  # 使用 config 模組當前值（部署基底）
    "paper_bulk_test_v1": PAPER_BULK_TEST_V1,
}


def list_profiles() -> list[str]:
    return sorted(PROFILES.keys())
