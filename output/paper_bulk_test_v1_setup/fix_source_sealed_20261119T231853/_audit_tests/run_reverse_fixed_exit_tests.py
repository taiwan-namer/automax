#!/usr/bin/env python3
"""Offline checks for reverse fixed-exit paper profile."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, Position
from breakout_follow_v1.portfolio import try_open, manage_position, estimate_initial_risk_u
from breakout_follow_v1.engine import execute_reverse


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print("PASS", name)


def main():
    applied = config.apply_profile("paper_reverse_fixed_exit_v1")
    check("capital_50k", applied["INITIAL_CAPITAL_USDT"] == 50000.0)
    check("daily_loss_8000", applied["DAILY_LOSS_LOCK_USDT"] == 8000.0)
    check("notional_fixed_10k", applied["NOTIONAL_USDT"] == 10000.0)
    check("margin_fixed_1k", applied["MARGIN_PER_TRADE_USDT"] == 1000.0)
    check("max_loss_100", applied["MAX_LOSS_PER_TRADE_USDT"] == 100.0)
    check("max_pos_20", applied["MAX_CONCURRENT_POSITIONS"] == 20)
    check("flip_on", applied["SIGNAL_DIRECTION_FLIP"] is True)
    check("fixed_tp_on", applied["FIXED_TAKE_PROFIT_ENABLED"] is True)
    check("tp_mult_1", applied["ATR_TP_MULT"] == 1.0)
    check("sl_mult_15", applied["ATR_STOP_MULT"] == 1.5)
    check("partial_off", applied["ENABLE_PARTIAL_TP"] is False)
    check("be_off", applied["ENABLE_BREAKEVEN"] is False)
    check("trail_off", applied["ENABLE_TRAILING_STOP"] is False)
    check("reverse_off", applied["ENABLE_REVERSE_REOPEN"] is False)
    check("cutoff_set", "2026-11-20T20:15:00" in applied["NEW_ENTRIES_UNTIL_ISO"])
    # sizing independent of capital
    check("sizing_not_scaled_by_capital", config.NOTIONAL_USDT == 10000.0 and config.INITIAL_CAPITAL_USDT == 50000.0)

    # SL/TP geometry
    entry, atr = 100.0, 0.5  # 1.5*0.5*100qty=75 + fees < 100
    long_sl = entry - 1.5 * atr
    long_tp = entry + 1.0 * atr
    short_sl = entry + 1.5 * atr
    short_tp = entry - 1.0 * atr
    check("long_sl", abs(long_sl - 99.25) < 1e-9)
    check("long_tp", abs(long_tp - 100.5) < 1e-9)
    check("short_sl", abs(short_sl - 100.75) < 1e-9)
    check("short_tp", abs(short_tp - 99.5) < 1e-9)

    # open LONG with fixed TP
    st = AccountState(
        run_id="test_run",
        profile_id=config.PROFILE_ID,
        cash_u=50000.0,
        initial_capital_u=50000.0,
        day_start_equity_u=50000.0,
        day_key="2099-01-01",
    )
    sig = {
        "signal_open_time": 1,
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": "t",
        "range_high": 101,
        "range_low": 99,
        "vol_x": 5.0,
        "direction_flip": True,
        "orig_side": "SHORT",
    }
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    opened = try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=filters)
    check("open_ok", opened.get("ok") is True or (_ for _ in ()).throw(AssertionError(str(opened))))
    pos = Position.from_dict(st.positions["TESTUSDT"])
    check("pos_sl", abs(pos.initial_stop - long_sl) < 1e-9)
    check("pos_tp", abs(pos.take_profit - long_tp) < 1e-9)
    check("pos_eff_sl", abs(pos.effective_stop - long_sl) < 1e-9)

    # FIXED_TP hit
    book = {"bid": 100.6, "ask": 100.7, "fetched_at": __import__("time").time()}
    evs = manage_position(st, "TESTUSDT", book=book, atr_latest=atr, phase="book")
    check("fixed_tp_event", any(e.get("action") == "FIXED_TP" and e.get("ok") for e in evs))
    check("flat_after_tp", "TESTUSDT" not in st.positions)

    # reopen for SL prefer when both
    st2 = AccountState(run_id="t2", profile_id=config.PROFILE_ID, cash_u=50000.0, initial_capital_u=50000.0, day_start_equity_u=50000.0, day_key="2099-01-01")
    sig2 = dict(sig); sig2["signal_open_time"] = 2
    try_open(st2, symbol="TESTUSDT", side="LONG", signal=sig2, source_tags=["T"], filters=filters)
    # mark below stop (also would be weird vs tp) — SL path
    book_sl = {"bid": 99.0, "ask": 99.05, "fetched_at": __import__("time").time()}
    evs2 = manage_position(st2, "TESTUSDT", book=book_sl, atr_latest=atr, phase="book")
    check("sl_event", any(e.get("action") == "INITIAL_STOP" and e.get("ok") for e in evs2))

    # reverse disabled
    st3 = AccountState(run_id="t3", profile_id=config.PROFILE_ID, cash_u=50000.0, initial_capital_u=50000.0)
    # fake position
    from breakout_follow_v1.account import set_position, save_account
    # skip file save path — execute_reverse checks flag first
    out = execute_reverse(
        st3,
        symbol="X",
        sig={"side": "SHORT"},
        book={"bid": 1, "ask": 1.1, "fetched_at": __import__("time").time()},
        source_tags=["T"],
        store=None,
    )
    check("reverse_disabled", out.get("block_reason") == "REVERSE_DISABLED")

    # bulk profile still loads
    b = config.apply_profile("paper_bulk_test_v1")
    check("bulk_dl_5000", b["DAILY_LOSS_LOCK_USDT"] == 5000.0)
    check("bulk_flip_off", b.get("SIGNAL_DIRECTION_FLIP") is False)

    print("\nALL reverse fixed-exit tests passed")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("FAIL", e)
        raise
