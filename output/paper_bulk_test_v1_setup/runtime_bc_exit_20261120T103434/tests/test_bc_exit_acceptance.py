#!/usr/bin/env python3
"""Offline acceptance for B partial70 and C pyramid R2+P0TP1% (paper simulation)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config  # noqa: E402
from breakout_follow_v1.account import AccountState, Position, save_account  # noqa: E402
from breakout_follow_v1.exit_plans import (  # noqa: E402
    B_PARTIAL70,
    C_PYRAMID,
    LEGACY,
    pyramid_levels,
    stamp_legacy_account,
)
from breakout_follow_v1.portfolio import manage_position, try_open  # noqa: E402


def _book(bid: float, ask: float) -> dict:
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "fetched_at": 1_700_000_000.0}


def _book_mark_long(mark: float) -> dict:
    """Ensure mark_price_for_side(LONG)=bid*(1-slip) >= mark."""
    bid = mark / (1 - 0.0002) + 1e-9
    return _book(bid, bid + 0.01)


def _book_mark_short(mark: float) -> dict:
    ask = mark / (1 + 0.0002) - 1e-9
    return _book(max(ask - 0.01, 1e-9), ask)


def _base_st(cash: float = 50_000.0) -> AccountState:
    return AccountState(
        run_id="accept_run",
        profile_id=config.PROFILE_ID,
        initial_capital_u=50_000.0,
        cash_u=cash,
        day_key="2026-11-20",
        day_start_equity_u=50_000.0,
        filters={"TESTUSDT": {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0, "tickSize": 0.00000001}},
    )


def _signal(side: str, entry: float) -> dict:
    return {
        "signal_open_time": 1_700_000_000_000,
        "est_fill_price": entry,
        "atr": 0.1,
        "atr_time": "",
        "range_high": entry * 1.01,
        "range_low": entry * 0.99,
        "vol_x": 5.0,
        "direction_flip": False,
    }


def test_pyramid_price_example():
    lv = pyramid_levels("LONG", 10.0, 0.0, 0.005)
    assert abs(lv["P1"] - 9.95) < 1e-12
    assert abs(lv["P2"] - 9.90025) < 1e-12
    assert abs(lv["Pstop"] - 9.85074875) < 1e-12
    assert abs(lv["P0_TP"] - 10.10) < 1e-12
    print("OK pyramid theoretical prices")


def test_c_p0_only_tp():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    st = _base_st()
    sig = _signal("LONG", 10.0)
    sig["exit_plan_id"] = C_PYRAMID
    r = try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])
    assert r["ok"], r
    pos = st.positions["TESTUSDT"]
    assert pos["exit_plan_id"] == C_PYRAMID
    assert abs(float(pos["take_profit"]) - 10.10) < 1e-9
    assert abs(float(pos["margin_u"]) - 300.0) < 1e-9
    # TP hit
    ev = manage_position(st, "TESTUSDT", book=_book_mark_long(10.11), atr_latest=None, phase="book")
    assert any(e.get("action") == "PYRAMID_P0_TP1PCT" and e.get("ok") for e in ev), ev
    assert "TESTUSDT" not in st.positions
    print("OK C P0-only TP1%")


def test_c_two_layer_rebound():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    st = _base_st()
    sig = _signal("LONG", 10.0)
    sig["exit_plan_id"] = C_PYRAMID
    assert try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])["ok"]
    # fill P1 at 9.95
    ev = manage_position(st, "TESTUSDT", book=_book(9.94, 9.95), atr_latest=None, phase="book")
    assert any(e.get("action") == "PYRAMID_ADD" and e.get("layer") == "P1" for e in ev), ev
    pos = st.positions["TESTUSDT"]
    assert abs(float(pos["margin_u"]) - 600.0) < 1e-6
    n_open = sum(1 for L in pos["pyramid"]["layers"] if not L["closed"])
    assert n_open == 2
    # rebound to 10 → P1 exits, P0 remains
    ev2 = manage_position(st, "TESTUSDT", book=_book_mark_long(10.00), atr_latest=None, phase="book")
    assert any(e.get("action") == "PYRAMID_R2_REBOUND" for e in ev2), ev2
    pos = st.positions["TESTUSDT"]
    layers = {L["name"]: L for L in pos["pyramid"]["layers"]}
    assert layers["P1"]["closed"] is True
    assert layers["P0"]["closed"] is False
    # P1 should not refill
    assert any(a["name"] == "P1" and "CANCELLED" in a["status"] for a in pos["pyramid"]["pending_adds"])
    print("OK C two-layer rebound; no refill")


def test_c_three_layer_stop():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    st = _base_st()
    sig = _signal("LONG", 10.0)
    sig["exit_plan_id"] = C_PYRAMID
    assert try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])["ok"]
    manage_position(st, "TESTUSDT", book=_book(9.94, 9.95), atr_latest=None, phase="book")
    p2 = float(st.positions["TESTUSDT"]["pyramid"]["levels"]["P2"])
    manage_position(st, "TESTUSDT", book=_book(p2 - 0.01, p2), atr_latest=None, phase="book")
    pos = st.positions["TESTUSDT"]
    assert sum(1 for L in pos["pyramid"]["layers"] if not L["closed"]) == 3, pos["pyramid"]
    pstop = float(pos["pyramid"]["levels"]["Pstop"])
    ev = manage_position(st, "TESTUSDT", book=_book(pstop - 0.01, pstop), atr_latest=None, phase="book")
    assert any(e.get("action") == "PYRAMID_PSTOP" for e in ev), ev
    assert "TESTUSDT" not in st.positions
    print("OK C three-layer Pstop")


def test_b_sl_before_partial():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
    st = _base_st()
    sig = _signal("LONG", 100.0)
    sig["exit_plan_id"] = B_PARTIAL70
    r = try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])
    assert r["ok"], r
    pos = st.positions["TESTUSDT"]
    assert pos["exit_plan_id"] == B_PARTIAL70
    assert pos["take_profit_2"] is not None
    sl = float(pos["effective_stop"])
    ev = manage_position(st, "TESTUSDT", book=_book(sl - 0.01, sl), atr_latest=None, phase="book")
    assert any(e.get("action") == "INITIAL_STOP" for e in ev), ev
    assert "TESTUSDT" not in st.positions
    print("OK B SL before first TP")


def test_b_partial_then_tp100():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
    st = _base_st()
    # tick 0.0001 for finer barriers
    st.filters["TESTUSDT"]["tickSize"] = 0.0001
    sig = _signal("LONG", 100.0)
    sig["exit_plan_id"] = B_PARTIAL70
    assert try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])["ok"]
    pos = st.positions["TESTUSDT"]
    tp50 = float(pos["take_profit"])
    tp100 = float(pos["take_profit_2"])
    q0 = float(pos["qty_initial"])
    ev = manage_position(st, "TESTUSDT", book=_book_mark_long(tp50), atr_latest=None, phase="book")
    assert any(e.get("action") == "PARTIAL_TP50_70" and e.get("ok") for e in ev), ev
    pos = st.positions["TESTUSDT"]
    assert pos["partial_tp_done"] is True
    assert abs(float(pos["effective_stop"]) - 100.0) < 1e-9
    assert abs(float(pos["qty_remaining"]) - q0 * 0.3) < 1e-3
    ev2 = manage_position(st, "TESTUSDT", book=_book_mark_long(tp100), atr_latest=None, phase="book")
    assert any(e.get("action") == "TP100_REMAINDER" for e in ev2), ev2
    assert "TESTUSDT" not in st.positions
    print("OK B 70% then TP100")


def test_b_partial_then_entry_stop():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
    st = _base_st()
    st.filters["TESTUSDT"]["tickSize"] = 0.0001
    sig = _signal("LONG", 100.0)
    sig["exit_plan_id"] = B_PARTIAL70
    assert try_open(st, symbol="TESTUSDT", side="LONG", signal=sig, source_tags=["T"], filters=st.filters["TESTUSDT"])["ok"]
    tp50 = float(st.positions["TESTUSDT"]["take_profit"])
    manage_position(st, "TESTUSDT", book=_book_mark_long(tp50), atr_latest=None, phase="book")
    assert st.positions["TESTUSDT"]["partial_tp_done"] is True
    ev = manage_position(st, "TESTUSDT", book=_book(99.99, 100.0), atr_latest=None, phase="book")
    assert any(e.get("action") in ("ENTRY_STOP", "ENTRY_STOP_IMMEDIATE") for e in ev), ev
    assert "TESTUSDT" not in st.positions
    print("OK B 70% then entry-stop (not fee BE)")


def test_legacy_isolation():
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    st = _base_st()
    # legacy position with old TP50/SL75 barriers; must NOT get pyramid
    st.positions["OLDUSDT"] = Position(
        trade_id="old",
        symbol="OLDUSDT",
        side="LONG",
        qty_initial=10,
        qty_remaining=10,
        entry_price=10.0,
        entry_time="t",
        entry_ts=1.0,
        margin_u=1000.0,
        notional_initial=10000.0,
        leverage=10.0,
        atr_at_entry=0.1,
        atr_time="",
        initial_stop=9.9,
        effective_stop=9.9,
        take_profit=10.05,
        exit_plan_id=LEGACY,
    ).to_dict()
    st.filters["OLDUSDT"] = st.filters["TESTUSDT"]
    # price beyond 1% but below legacy TP — should not exit on pyramid rule
    ev = manage_position(st, "OLDUSDT", book=_book_mark_long(10.04), atr_latest=None, phase="book")
    assert not any("PYRAMID" in str(e.get("action")) for e in ev), ev
    assert "OLDUSDT" in st.positions
    # hit legacy TP
    ev2 = manage_position(st, "OLDUSDT", book=_book_mark_long(10.05), atr_latest=None, phase="book")
    assert any(e.get("action") == "FIXED_TP" for e in ev2), ev2
    print("OK legacy isolation")


def test_stamp_pending():
    st = _base_st()
    st.pending_limit_entries = {"X": {"symbol": "X", "limit_price": 1.0}}
    st.positions["Y"] = {"symbol": "Y", "take_profit": 1.1, "effective_stop": 0.9}
    info = stamp_legacy_account(st)
    assert st.pending_limit_entries["X"]["exit_plan_id"] == LEGACY
    assert st.pending_limit_entries["X"]["notional_u"] == 10000.0
    assert st.positions["Y"]["exit_plan_id"] == LEGACY
    assert info["stamped_pending"] == 1
    print("OK stamp legacy", info)


def main():
    # temp out dirs so fills don't pollute
    with tempfile.TemporaryDirectory() as td:
        config.set_runtime_root(td)
        config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
        test_pyramid_price_example()
        test_stamp_pending()
        test_b_sl_before_partial()
        test_b_partial_then_tp100()
        test_b_partial_then_entry_stop()
        test_c_p0_only_tp()
        test_c_two_layer_rebound()
        test_c_three_layer_stop()
        test_legacy_isolation()
    print("\nALL ACCEPTANCE PASSED")


if __name__ == "__main__":
    main()
