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
    "DAILY_LOSS_LOCK_USDT": 5_000.0,
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
    "CODE_FIX_TAG": "fix_20261119_mgmt_dl5000_batchside1",
    "REVERSE_POLICY": "approved_full_breakout_reverse_reopen_20261119",
}


PAPER_REVERSE_FIXED_EXIT_V1: dict[str, Any] = {
    "PROFILE_ID": "paper_reverse_fixed_exit_v1",
    "PROFILE_PURPOSE": "one-day reverse paper — fixed TP1×ATR SL1.5×ATR; not profit targeting",
    "INITIAL_CAPITAL_USDT": 50_000.0,
    "MAX_CONCURRENT_POSITIONS": 20,
    "DAILY_LOSS_LOCK_USDT": 8_000.0,
    # 名義／保證金固定，不隨本金放大
    "MARGIN_PER_TRADE_USDT": 1_000.0,
    "LEVERAGE": 10.0,
    "NOTIONAL_USDT": 10_000.0,
    "MAX_LOSS_PER_TRADE_USDT": 100.0,
    "MAX_PER_SYMBOL": 1,
    "COOLDOWN_MINUTES": 3,
    "REVERSE_WINDOW_MINUTES": 30,
    "MAX_REVERSES_PER_WINDOW": 0,  # 關閉反手重開（數值閘門；另有 ENABLE_REVERSE_REOPEN=False）
    "PARTIAL_TP_FRACTION": 0.30,  # 數值保留；行為由 ENABLE_PARTIAL_TP=False 關閉
    "PARTIAL_TP_NET_USDT": 100.0,
    "TRAIL_ATR_MULT": 1.5,
    "TOP_N": 10,
    "ATR_STOP_MULT": 1.5,
    "VOL_X_MIN": 3.0,
    "CODE_FIX_TAG": "fix_20261119_reverse_tp1_sl15_dl8000",
    "REVERSE_POLICY": "disabled_fixed_exit_no_reverse_reopen",
    # strategy flags (apply_profile 另讀)
    "SIGNAL_DIRECTION_FLIP": True,
    "FIXED_TAKE_PROFIT_ENABLED": True,
    "ATR_TP_MULT": 1.0,
    "ENABLE_PARTIAL_TP": False,
    "ENABLE_BREAKEVEN": False,
    "ENABLE_TRAILING_STOP": False,
    "ENABLE_REVERSE_REOPEN": False,
    "NEW_ENTRIES_UNTIL_ISO": "2026-11-20T20:15:00+08:00",
}

# 同期觀察：進場／資金／flip 對齊 reverse；僅退出改為固定毛 +50／-75 USDT（非 ATR）
PAPER_REVERSE_FIXED_USDT_50_75_V1: dict[str, Any] = {
    "PROFILE_ID": "paper_reverse_fixed_usdt_50_75_v1",
    "PROFILE_PURPOSE": (
        "concurrent paper observation vs reverse ATR exits — "
        "fixed gross TP+50/SL-75 USDT; not profit targeting; not same-signal A/B"
    ),
    "INITIAL_CAPITAL_USDT": 50_000.0,
    "MAX_CONCURRENT_POSITIONS": 20,
    "DAILY_LOSS_LOCK_USDT": 8_000.0,
    "MARGIN_PER_TRADE_USDT": 1_000.0,
    "LEVERAGE": 10.0,
    "NOTIONAL_USDT": 10_000.0,
    "MAX_LOSS_PER_TRADE_USDT": 100.0,
    "MAX_PER_SYMBOL": 1,
    "COOLDOWN_MINUTES": 3,
    "REVERSE_WINDOW_MINUTES": 30,
    "MAX_REVERSES_PER_WINDOW": 0,
    "PARTIAL_TP_FRACTION": 0.30,
    "PARTIAL_TP_NET_USDT": 100.0,
    "TRAIL_ATR_MULT": 1.5,
    "TOP_N": 10,
    "ATR_STOP_MULT": 1.5,  # 保留數值；fixed-gross 啟用時不使用 ATR 決定退出距離
    "VOL_X_MIN": 3.0,
    "CODE_FIX_TAG": "fix_20261120_b_partial70_30_tp50_tp100",
    "REVERSE_POLICY": "disabled_fixed_exit_no_reverse_reopen",
    "SIGNAL_DIRECTION_FLIP": True,
    "FIXED_TAKE_PROFIT_ENABLED": True,
    "ATR_TP_MULT": 1.0,  # ATR 路徑備用；fixed-gross 啟用時不使用
    # 新開倉：70%@TP50 + 30%@TP100；舊倉靠 exit_plan_id=legacy 隔離
    "ENABLE_PARTIAL_TP": True,
    "ENABLE_BREAKEVEN": False,  # 進場價停損≠扣費保本；不用 fee-BE
    "ENABLE_TRAILING_STOP": False,
    "ENABLE_REVERSE_REOPEN": False,
    "NEW_ENTRIES_UNTIL_ISO": "2026-11-20T20:15:00+08:00",
    "FIXED_GROSS_EXIT_ENABLED": True,
    "FIXED_GROSS_TP_USDT": 50.0,
    "FIXED_GROSS_TP2_USDT": 100.0,
    "FIXED_GROSS_SL_USDT": 75.0,
    "ALLOW_PROFILE_SIZE_OVERRIDE": True,
    "PARTIAL_TP_FRACTION": 0.70,  # 首次退出原始數量 70%
    "PARTIAL_TP_NET_USDT": 50.0,  # 保留欄位；實際用價格障礙
    "PARTIAL_TP_USE_PRICE_BARRIER": True,
    "EXIT_PLAN_ID": "b_partial70_30_tp50_tp100_20261120",
    "PYRAMID_ENABLED": False,
    # 多掛 −0.5%／空掛 +0.5%；一直盯盤；不利偏離參考價 ≥1% 才作廢（不用時間）
    "LIMIT_ENTRY_ENABLED": True,
    "LIMIT_ENTRY_OFFSET_PCT": 0.005,
    "LIMIT_ENTRY_TTL_SEC": 0.0,
    "LIMIT_ENTRY_CANCEL_ADVERSE_PCT": 0.01,
}

# C：自掃順突破＋限價 0.5%；新開倉＝300U×10 三層 R2（追加回 P0 出）＋P0 TP1%＋固定 Pstop
PAPER_REVERSE_FIXED_USDT_50_75_INV_V1: dict[str, Any] = {
    "PROFILE_ID": "paper_reverse_fixed_usdt_50_75_inv_v1",
    "PROFILE_PURPOSE": (
        "independent self-scan opposite side of B — "
        "limit 0.5%; NEW exits: pyramid300 R2 + P0 TP1% + Pstop; legacy positions isolated"
    ),
    "INITIAL_CAPITAL_USDT": 50_000.0,
    "MAX_CONCURRENT_POSITIONS": 20,
    "DAILY_LOSS_LOCK_USDT": 8_000.0,
    "MARGIN_PER_TRADE_USDT": 300.0,
    "LEVERAGE": 10.0,
    "NOTIONAL_USDT": 3_000.0,
    "MAX_LOSS_PER_TRADE_USDT": 100.0,
    "MAX_PER_SYMBOL": 1,
    "COOLDOWN_MINUTES": 3,
    "REVERSE_WINDOW_MINUTES": 30,
    "MAX_REVERSES_PER_WINDOW": 0,
    "PARTIAL_TP_FRACTION": 0.30,
    "PARTIAL_TP_NET_USDT": 100.0,
    "TRAIL_ATR_MULT": 1.5,
    "TOP_N": 10,
    "ATR_STOP_MULT": 1.5,
    "VOL_X_MIN": 3.0,
    "CODE_FIX_TAG": "fix_20261120_c_pyramid300_r2_p0tp1pct",
    "REVERSE_POLICY": "disabled_fixed_exit_no_reverse_reopen",
    "SIGNAL_DIRECTION_FLIP": False,
    "FIXED_TAKE_PROFIT_ENABLED": True,
    "ATR_TP_MULT": 1.0,
    "ENABLE_PARTIAL_TP": False,
    "ENABLE_BREAKEVEN": False,
    "ENABLE_TRAILING_STOP": False,
    "ENABLE_REVERSE_REOPEN": False,
    "NEW_ENTRIES_UNTIL_ISO": "2026-11-20T20:15:00+08:00",
    "ALLOW_PROFILE_SIZE_OVERRIDE": True,
    # 新開倉不用毛 50/75；舊倉／舊掛單 stamp legacy 後仍走原障礙
    "FIXED_GROSS_EXIT_ENABLED": False,
    "FIXED_GROSS_TP_USDT": 50.0,
    "FIXED_GROSS_SL_USDT": 75.0,
    "EXIT_PLAN_ID": "c_pyramid300_r2_p0tp1pct_20261120",
    "PYRAMID_ENABLED": True,
    "PYRAMID_MAX_LAYERS": 3,
    "PYRAMID_ADD_PCT": 0.005,
    "PYRAMID_P0_TP_PCT": 0.01,
    "LIMIT_ENTRY_ENABLED": True,
    "LIMIT_ENTRY_OFFSET_PCT": 0.005,
    "LIMIT_ENTRY_TTL_SEC": 0.0,
    "LIMIT_ENTRY_CANCEL_ADVERSE_PCT": 0.01,
    "MIRROR_ONLY_ENTRIES": False,
    "MIRROR_SOURCE_PROFILE": "",
}

PROFILES: dict[str, dict[str, Any]] = {
    DEFAULT_PROFILE: LEGACY_PROFILE_BASELINE,
    "paper_bulk_test_v1": PAPER_BULK_TEST_V1,
    "paper_reverse_fixed_exit_v1": PAPER_REVERSE_FIXED_EXIT_V1,
    "paper_reverse_fixed_usdt_50_75_v1": PAPER_REVERSE_FIXED_USDT_50_75_V1,
    "paper_reverse_fixed_usdt_50_75_inv_v1": PAPER_REVERSE_FIXED_USDT_50_75_INV_V1,
}


def list_profiles() -> list[str]:
    return sorted(PROFILES.keys())
