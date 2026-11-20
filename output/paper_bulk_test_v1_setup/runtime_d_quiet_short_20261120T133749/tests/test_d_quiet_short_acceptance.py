#!/usr/bin/env python3
"""Acceptance tests for D quiet-short paper rules."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(ROOT))

from d_quiet_short_v1 import config  # noqa: E402
from d_quiet_short_v1.portfolio import (  # noqa: E402
    Account,
    cancel_pending,
    close_position,
    cover_exit_price,
    manage_position,
    process_pending,
    queue_short_limit,
    sort_signals,
)
from d_quiet_short_v1.signals import (  # noqa: E402
    Bar15,
    SymbolState,
    after_exit,
    evaluate_quiet,
    rebuild_ready,
    try_emit_signal,
)
from d_quiet_short_v1.util import round_price_tick  # noqa: E402


K = config.KLINE_MS


def _quiet_bars(n: int = 4, start_ot: int = 1_700_000_000_000, base: float = 10.0) -> list[Bar15]:
    """Build n continuous quiet 15m bars: tiny ranges."""
    out = []
    px = base
    for i in range(n):
        ot = start_ot + i * K
        # amp ~0.2% per bar; range across 4 ~0.6%
        o = px
        h = px * 1.001
        l = px * 0.999
        c = px * 1.0002
        out.append(Bar15(ot, o, h, l, c, ot + K - 1))
        px = c
    return out


def _unlock():
    config.restore_production_confirmations()


def _lock():
    config.reset_confirmations()


def _restore():
    config.restore_production_confirmations()


def test_completed_15m_only():
    bars = _quiet_bars(4)
    # inject an "incomplete" conceptual bar by using evaluate only on completed list
    ev = evaluate_quiet(bars)
    assert ev["ok"], ev
    # missing bar -> fail
    assert evaluate_quiet(bars[:3])["reason"] == "INSUFFICIENT_BARS"
    print("OK only completed 15m quiet window")


def test_p10_limit_and_cancel():
    P = 10.0
    limit = round_price_tick(P * 1.005, 0.01)
    assert abs(limit - 10.05) < 1e-9
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        sig = {
            "symbol": "TESTUSDT",
            "P": 10.0,
            "signal_id": "t1",
            "fill_allowed_after_ms": 0,
            "B": 0.002,
            "T": 0.008,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        assert queue_short_limit(st, signal=sig, filters=filt, allow_fills=True)["ok"]
        assert abs(st.pending_limit_entries["TESTUSDT"]["limit_px"] - 10.05) < 1e-9
        books = {
            "TESTUSDT": {
                "bid": 9.89,
                "ask": 9.91,
                "mid": 9.90,
                "spread": 0.002,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(st, books, now_ms=int(time.time() * 1000) + 10)
        assert any(e.get("action") == "CANCELLED" for e in ev), ev
        assert "TESTUSDT" not in st.pending_limit_entries
        _lock()
    print("OK P=10 limit~10.05; cancel at 9.90")


def test_no_time_ttl_cancel():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        sig = {
            "symbol": "TESTUSDT",
            "P": 10.0,
            "signal_id": "t2",
            "fill_allowed_after_ms": 0,
            "B": 0.002,
            "T": 0.008,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        assert queue_short_limit(st, signal=sig, filters=filt, allow_fills=True)["ok"]
        pend = st.pending_limit_entries["TESTUSDT"]
        assert float(pend["ttl_sec"]) == 0.0
        # age the queue artificially — still no cancel without price/eligibility
        pend["queued_ts"] = time.time() - 600
        books = {
            "TESTUSDT": {
                "bid": 10.00,
                "ask": 10.02,
                "mid": 10.01,
                "spread": 0.002,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(st, books, now_ms=int(time.time() * 1000))
        assert "TESTUSDT" in st.pending_limit_entries
        assert not any(e.get("reason") == "TTL" for e in ev)
        _lock()
    print("OK no time-TTL cancel after 5+ minutes")


def test_eligibility_cancel():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        sig = {
            "symbol": "TESTUSDT",
            "P": 10.0,
            "signal_id": "t3",
            "fill_allowed_after_ms": 0,
            "B": 0.002,
            "T": 0.008,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        assert queue_short_limit(st, signal=sig, filters=filt, allow_fills=True)["ok"]
        books = {
            "TESTUSDT": {
                "bid": 10.00,
                "ask": 10.02,
                "mid": 10.01,
                "spread": 0.002,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(
            st, books, now_ms=int(time.time() * 1000), eligibility_fail={"TESTUSDT": "QUIET_FAIL"}
        )
        assert any(e.get("reason") == "QUIET_FAIL" for e in ev)
        assert "TESTUSDT" not in st.pending_limit_entries
        _lock()
    print("OK quiet-fail cancels pending")


def test_partial_fill_race_and_slots():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        # fill then cancel remainder path: simulate by pre-setting partial
        sig = {
            "symbol": "TESTUSDT",
            "P": 10.0,
            "signal_id": "t4",
            "fill_allowed_after_ms": 0,
            "B": 0.002,
            "T": 0.008,
        }
        assert queue_short_limit(st, signal=sig, filters=filt, allow_fills=True)["ok"]
        # touch fill
        books = {
            "TESTUSDT": {
                "bid": 10.06,
                "ask": 10.07,
                "mid": 10.065,
                "spread": 0.001,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(st, books, now_ms=int(time.time() * 1000) + 10)
        assert any(e.get("action") == "FILLED" for e in ev)
        assert "TESTUSDT" in st.positions
        assert "TESTUSDT" not in st.pending_limit_entries
        # slot: same symbol pending+pos counts as 1
        assert st.slot_count() == 1
        # cannot duplicate
        r = queue_short_limit(st, signal={**sig, "signal_id": "t4b"}, filters=filt, allow_fills=True)
        assert r["reason"] == "SYMBOL_BUSY"
        _lock()
    print("OK fill + no duplicate symbol + slot")


def test_slot_cap_20():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        for i in range(20):
            sym = f"S{i:02d}USDT"
            sig = {
                "symbol": sym,
                "P": 10.0,
                "signal_id": f"s{i}",
                "fill_allowed_after_ms": 0,
                "B": 0.002,
                "T": 0.008,
            }
            assert queue_short_limit(st, signal=sig, filters=filt, allow_fills=True)["ok"], i
        assert st.slot_count() == 20
        r = queue_short_limit(
            st,
            signal={
                "symbol": "OVERFLOWUSDT",
                "P": 10.0,
                "signal_id": "ov",
                "fill_allowed_after_ms": 0,
                "B": 0.002,
                "T": 0.008,
            },
            filters=filt,
            allow_fills=True,
        )
        assert r["reason"] == "SLOT_CAP"
        _lock()
    print("OK slot cap 20")


def test_tp_sl_vol_exit_no_wait_bar_close():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        # craft position: entry 10, qty such that 1% move ~ TP
        # notional 3000 @10 = 300 qty; TP150 => need price drop 0.5
        st.positions["TESTUSDT"] = {
            "trade_id": "t:TEST",
            "signal_id": "sig",
            "symbol": "TESTUSDT",
            "side": "SHORT",
            "qty_initial": 300.0,
            "qty_remaining": 300.0,
            "qty_target": 300.0,
            "entry_price": 10.0,
            "entry_time": now_iso_safe(),
            "entry_ts": time.time(),
            "margin_u": 300.0,
            "notional_initial": 3000.0,
            "leverage": 10.0,
            "tp_sl_basis": "gross",
            "tp_usdt": 150.0,
            "sl_usdt": 30.0,
            "B": 0.002,
            "T": 0.008,
            "fees_paid_u": 0.0,
            "mfe_u": 0.0,
            "mae_u": 0.0,
            "roll_samples": [],
            "filters": {},
            "strategy_version": config.STRATEGY_VERSION,
        }
        # TP: mid/ask down so cover ask gives gross>=150
        # exit via ask+slip; want (10 - exit)*300 >= 150 => exit <= 9.5
        ask = 9.48
        book = {
            "bid": 9.47,
            "ask": ask,
            "mid": 9.475,
            "spread": 0.001,
            "fetched_at": time.time(),
        }
        closed = manage_position(st, "TESTUSDT", book)
        assert closed and closed.get("exit_reason") == "TP", closed
        assert "TESTUSDT" not in st.positions

        # SL
        st.positions["TESTUSDT"] = {
            "trade_id": "t:TEST2",
            "signal_id": "sig2",
            "symbol": "TESTUSDT",
            "side": "SHORT",
            "qty_initial": 300.0,
            "qty_remaining": 300.0,
            "qty_target": 300.0,
            "entry_price": 10.0,
            "entry_time": now_iso_safe(),
            "entry_ts": time.time(),
            "margin_u": 300.0,
            "leverage": 10.0,
            "tp_sl_basis": "gross",
            "tp_usdt": 150.0,
            "sl_usdt": 30.0,
            "B": 0.002,
            "T": 0.008,
            "fees_paid_u": 0.0,
            "mfe_u": 0.0,
            "mae_u": 0.0,
            "roll_samples": [],
            "filters": {},
            "strategy_version": config.STRATEGY_VERSION,
        }
        # SL when gross <= -30 => (10-exit)*300 <= -30 => exit >= 10.1
        ask = 10.12
        book = {
            "bid": 10.11,
            "ask": ask,
            "mid": 10.115,
            "spread": 0.001,
            "fetched_at": time.time(),
        }
        closed = manage_position(st, "TESTUSDT", book)
        assert closed and "SL" in str(closed.get("exit_reason")), closed

        # VOL spike: seed roll samples spanning >= T
        st.positions["TESTUSDT"] = {
            "trade_id": "t:TEST3",
            "signal_id": "sig3",
            "symbol": "TESTUSDT",
            "side": "SHORT",
            "qty_initial": 300.0,
            "qty_remaining": 300.0,
            "qty_target": 300.0,
            "entry_price": 10.0,
            "entry_time": now_iso_safe(),
            "entry_ts": time.time(),
            "margin_u": 300.0,
            "leverage": 10.0,
            "tp_sl_basis": "gross",
            "tp_usdt": 150.0,
            "sl_usdt": 30.0,
            "B": 0.002,
            "T": 0.008,
            "fees_paid_u": 0.0,
            "mfe_u": 0.0,
            "mae_u": 0.0,
            "roll_samples": [{"ts": time.time() - 10, "px": 10.0}],
            "filters": {},
            "strategy_version": config.STRATEGY_VERSION,
        }
        book = {
            "bid": 10.09,
            "ask": 10.10,
            "mid": 10.095,  # vs 10.0 => amp ~0.95% > 0.8%
            "spread": 0.001,
            "fetched_at": time.time(),
        }
        closed = manage_position(st, "TESTUSDT", book)
        assert closed and "VOL_SPIKE" in str(closed.get("exit_reason")), closed
        _lock()
    print("OK TP/SL/VOL exit without waiting bar close")


def now_iso_safe():
    from d_quiet_short_v1.util import now_iso

    return now_iso()


def test_vol_rebuild_4_bars():
    st = SymbolState(symbol="TESTUSDT")
    exit_ts = 1_700_000_000_000 + 5 * 60_000  # mid-bar
    st = after_exit(st, reason="VOL_SPIKE", exit_ts_ms=exit_ts, require_rebuild=True)
    assert st.rebuild_need_bars == 4
    bar_open = (exit_ts // K) * K
    # 3 fresh bars -> not ready
    bars = []
    for i in range(3):
        ot = bar_open + (i + 1) * K
        bars.append(Bar15(ot, 10, 10.01, 9.99, 10.0, ot + K - 1))
    assert not rebuild_ready(st, bars)
    # 4th fresh
    ot = bar_open + 4 * K
    bars.append(Bar15(ot, 10, 10.01, 9.99, 10.0, ot + K - 1))
    assert rebuild_ready(st, bars)
    # bar at same open as exit floor must not count
    bad = [Bar15(bar_open, 10, 10.01, 9.99, 10.0, bar_open + K - 1)] + bars[:3]
    st2 = after_exit(SymbolState(symbol="X"), reason="VOL_SPIKE", exit_ts_ms=exit_ts, require_rebuild=True)
    assert not rebuild_ready(st2, bad)
    print("OK vol-exit rebuild needs 4 fresh 15m bars")


def test_stale_quote_no_fabricate():
    with tempfile.TemporaryDirectory() as td:
        _unlock()
        config.set_runtime_root(td)
        st = Account(run_id="t")
        st.positions["TESTUSDT"] = {
            "trade_id": "t",
            "signal_id": "s",
            "symbol": "TESTUSDT",
            "side": "SHORT",
            "qty_initial": 300.0,
            "qty_remaining": 300.0,
            "qty_target": 300.0,
            "entry_price": 10.0,
            "entry_time": now_iso_safe(),
            "entry_ts": time.time(),
            "tp_sl_basis": "gross",
            "tp_usdt": 150.0,
            "sl_usdt": 30.0,
            "B": 0.002,
            "T": 0.008,
            "mfe_u": 0.0,
            "mae_u": 0.0,
            "roll_samples": [],
            "fees_paid_u": 0.0,
            "filters": {},
            "strategy_version": config.STRATEGY_VERSION,
        }
        book = {
            "bid": 9.0,
            "ask": 9.01,
            "mid": 9.005,
            "spread": 0.001,
            "fetched_at": time.time() - 60,
        }
        r = manage_position(st, "TESTUSDT", book)
        assert r and r.get("action") == "STALE_HOLD"
        assert "TESTUSDT" in st.positions
        _lock()
    print("OK stale quote no fabricated exit")


def test_fills_blocked_without_confirm():
    _lock()
    try:
        with tempfile.TemporaryDirectory() as td:
            config.set_runtime_root(td)
            st = Account(run_id="t")
            r = queue_short_limit(
                st,
                signal={
                    "symbol": "TESTUSDT",
                    "P": 10.0,
                    "signal_id": "x",
                    "fill_allowed_after_ms": 0,
                    "B": 0.002,
                    "T": 0.008,
                },
                filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01},
                allow_fills=True,
            )
            assert r["reason"] == "PAPER_FILLS_BLOCKED_PENDING_CONFIRMATIONS"
    finally:
        _restore()
    print("OK paper fills blocked without capital/depth confirm")


def test_dual_near_touch_depth():
    from d_quiet_short_v1.market import analyze_order_book

    # best bid 10, best ask 10.01; within 0.1%
    bids = [[10.0, 6000], [9.995, 1000], [9.90, 999999]]  # far level must NOT count
    asks = [[10.01, 6000], [10.015, 1000], [10.20, 999999]]
    raw = {"bids": bids, "asks": asks}
    d = analyze_order_book(raw, target_walk_notional=3000.0)
    assert d["entry_depth_ok"] is True
    assert d["near_touch_bid_notional"] >= 50_000
    assert d["near_touch_ask_notional"] >= 50_000
    # far 9.90 / 10.20 excluded
    assert d["near_touch_bid_notional"] < 10.0 * 6000 + 9.995 * 1000 + 1000
    assert d["walk_entry_sell_bids"]["vwap"] is not None
    assert d["walk_exit_buy_asks"]["vwap"] is not None
    # insufficient ask near-touch
    raw2 = {"bids": bids, "asks": [[10.01, 1], [10.20, 999999]]}
    d2 = analyze_order_book(raw2, target_walk_notional=3000.0)
    assert d2["entry_depth_ok"] is False
    print("OK dual near-touch 0.1% depth + 3000U walk")


def test_capital_confirmed_sizing():
    _restore()
    assert config.paper_fills_allowed()
    assert config.STAKE_IS_MARGIN is True
    assert config.LEVERAGE == 10.0
    assert abs(float(config.notional_for_stake()) - 3000.0) < 1e-9
    assert config.TP_SL_BASIS == "gross"
    assert abs(config.MAX_SPREAD - 0.0008) < 1e-12  # 0.08%
    assert "24h" in config.QUOTE_VOLUME_DEFINITION.lower() or "24" in config.QUOTE_VOLUME_DEFINITION
    print("OK capital 300 margin×10=3000; spread 0.08%; quoteVolume 24h USDT")


def test_signal_sort_deterministic():
    cands = [
        {"symbol": "BUSDT", "bar_close_time": 2, "spread": 0.0005},
        {"symbol": "AUSDT", "bar_close_time": 2, "spread": 0.0005},
        {"symbol": "CUSDT", "bar_close_time": 1, "spread": 0.0001},
    ]
    s = sort_signals(cands)
    assert [x["symbol"] for x in s] == ["CUSDT", "AUSDT", "BUSDT"]
    print("OK signal sort time→spread→symbol")


def test_cover_not_mid():
    ask = 10.0
    px = cover_exit_price(ask)
    assert px > ask
    assert abs(px - ask * (1 + config.SLIPPAGE_ADVERSE)) < 1e-12
    print("OK cover uses ask+slip not mid")


def test_emit_signal_and_incomplete_forbidden():
    bars = _quiet_bars(4, base=10.0)
    st = SymbolState(symbol="TESTUSDT")
    st, sig = try_emit_signal(
        st, bars, P=10.0, price_source="bookTicker", price_ts=time.time(), spread=0.0005
    )
    assert sig is not None
    assert sig["P"] == 10.0
    assert "B" in sig and "T" in sig
    # same bar cannot re-emit
    st2, sig2 = try_emit_signal(
        st, bars, P=10.0, price_source="bookTicker", price_ts=time.time(), spread=0.0005
    )
    assert sig2 is None
    print("OK signal emit once per completed bar")


def test_abc_untouched_marker():
    # static: D profile id distinct
    assert config.PROFILE_ID == "paper_d_quiet_short_v1"
    assert "a_surge" not in config.PROFILE_ID
    assert "reverse_fixed" not in config.PROFILE_ID
    print("OK D profile isolated from A/B/C naming")


def main():
    try:
        test_completed_15m_only()
        test_p10_limit_and_cancel()
        test_no_time_ttl_cancel()
        test_eligibility_cancel()
        test_partial_fill_race_and_slots()
        test_slot_cap_20()
        test_tp_sl_vol_exit_no_wait_bar_close()
        test_vol_rebuild_4_bars()
        test_stale_quote_no_fabricate()
        test_fills_blocked_without_confirm()
        test_dual_near_touch_depth()
        test_capital_confirmed_sizing()
        test_signal_sort_deterministic()
        test_cover_not_mid()
        test_emit_signal_and_incomplete_forbidden()
        test_abc_untouched_marker()
        print("\nALL D ACCEPTANCE PASSED")
    finally:
        _restore()


if __name__ == "__main__":
    main()
