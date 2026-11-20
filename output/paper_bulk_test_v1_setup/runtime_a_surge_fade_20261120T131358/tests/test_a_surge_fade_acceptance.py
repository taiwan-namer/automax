#!/usr/bin/env python3
"""Acceptance tests for A surge-fade paper rules."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(ROOT))

from a_surge_fade_v1 import config  # noqa: E402
from a_surge_fade_v1.portfolio import (  # noqa: E402
    ArmAccount,
    barriers_for_short,
    manage_position,
    process_pending,
    queue_short_limit,
)
from a_surge_fade_v1.signals import Bar, SymbolState, compute_surge_metrics, step_state  # noqa: E402
from a_surge_fade_v1.util import round_price_tick  # noqa: E402


def _bars_flat(n: int, start_px: float = 10.0, start_ot: int = 1_700_000_000_000) -> list[Bar]:
    out = []
    for i in range(n):
        ot = start_ot + i * 60_000
        out.append(Bar(ot, start_px, start_px, start_px, start_px, ot + 59_999))
    return out


def test_prices_s10():
    S = 10.0
    limit = round_price_tick(S * 1.01, 0.01)
    assert abs(limit - 10.10) < 1e-9
    b = barriers_for_short(10.10, "tp100", 0.0001)
    assert abs(b["stop"] - 10.1505) < 1e-6
    assert abs(b["tp1"] - 9.999) < 1e-6
    assert abs(b["tp2"] - 9.898) < 1e-6
    print("OK S=10 limit/SL/TP100 barriers")


def test_cancel_drop_2pct():
    with tempfile.TemporaryDirectory() as td:
        config.set_runtime_root(td)
        st = ArmAccount(arm="tp100", profile_id=config.ARMS["tp100"]["profile_id"], run_id="t")
        sig = {
            "symbol": "TESTUSDT",
            "S": 10.0,
            "signal_id": "t1",
            "fill_allowed_after_ms": 0,
            "surge_bucket": "50_100",
            "surge_pct_24h": 0.6,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        assert queue_short_limit(st, signal=sig, filters=filt)["ok"]
        # mid 9.80 -> cancel
        books = {
            "TESTUSDT": {
                "bid": 9.79,
                "ask": 9.81,
                "mid": 9.80,
                "spread": 0.002,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(st, books, now_ms=int(time.time() * 1000) + 10)
        assert any(e.get("action") == "CANCELLED" for e in ev), ev
        assert "TESTUSDT" not in st.pending_limit_entries
        print("OK cancel at S*0.98")


def test_fill_and_partial_then_entry_stop():
    with tempfile.TemporaryDirectory() as td:
        config.set_runtime_root(td)
        st = ArmAccount(arm="tp100", profile_id=config.ARMS["tp100"]["profile_id"], run_id="t")
        sig = {
            "symbol": "TESTUSDT",
            "S": 10.0,
            "signal_id": "t2",
            "fill_allowed_after_ms": 0,
            "surge_bucket": "ge_100",
            "surge_pct_24h": 1.2,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.0001}
        assert queue_short_limit(st, signal=sig, filters=filt)["ok"]
        # fill: bid >= 10.10
        books = {
            "TESTUSDT": {
                "bid": 10.11,
                "ask": 10.12,
                "mid": 10.115,
                "spread": 0.0009,
                "fetched_at": time.time(),
            }
        }
        # spread gate is on scan not process_pending
        ev = process_pending(st, books, now_ms=int(time.time() * 1000) + 10)
        assert any(e.get("action") == "FILLED" and e.get("ok") for e in ev), ev
        pos = st.positions["TESTUSDT"]
        assert abs(float(pos["entry_price"]) - 10.10) < 1e-9
        assert abs(float(pos["effective_stop"]) - 10.1505) < 1e-6
        # hit TP1: mark = ask*(1+slip) <= 9.999
        books2 = {
            "TESTUSDT": {
                "bid": 9.97,
                "ask": 9.98,
                "mid": 9.975,
                "spread": 0.001,
                "fetched_at": time.time(),
            }
        }
        ev2 = manage_position(st, "TESTUSDT", books2["TESTUSDT"])
        assert any(e.get("action") == "PARTIAL_TP1_70" for e in ev2), ev2
        pos = st.positions["TESTUSDT"]
        assert pos["partial_tp_done"] is True
        assert abs(float(pos["effective_stop"]) - 10.10) < 1e-9
        print("OK fill + 70% then entry-stop move")


def test_no_fill_before_confirm_end():
    with tempfile.TemporaryDirectory() as td:
        config.set_runtime_root(td)
        st = ArmAccount(arm="tp100", profile_id=config.ARMS["tp100"]["profile_id"], run_id="t")
        future = int(time.time() * 1000) + 60_000
        sig = {
            "symbol": "TESTUSDT",
            "S": 10.0,
            "signal_id": "t3",
            "fill_allowed_after_ms": future,
            "surge_bucket": "50_100",
            "surge_pct_24h": 0.55,
        }
        filt = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5, "tickSize": 0.01}
        assert queue_short_limit(st, signal=sig, filters=filt)["ok"]
        books = {
            "TESTUSDT": {
                "bid": 10.20,
                "ask": 10.21,
                "mid": 10.205,
                "spread": 0.0005,
                "fetched_at": time.time(),
            }
        }
        ev = process_pending(st, books, now_ms=int(time.time() * 1000))
        assert ev == []
        assert "TESTUSDT" in st.pending_limit_entries
        print("OK no fill before confirm bar end")


def test_stale_quote_no_exit():
    with tempfile.TemporaryDirectory() as td:
        config.set_runtime_root(td)
        st = ArmAccount(arm="tp100", profile_id=config.ARMS["tp100"]["profile_id"], run_id="t")
        # manually seed position
        from a_surge_fade_v1.portfolio import Position
        from dataclasses import asdict

        pos = Position(
            trade_id="x",
            symbol="TESTUSDT",
            side="SHORT",
            qty_initial=100,
            qty_remaining=100,
            entry_price=10.1,
            entry_time="t",
            entry_ts=time.time(),
            margin_u=1000,
            notional_initial=1010,
            leverage=10,
            initial_stop=10.1505,
            effective_stop=10.1505,
            take_profit_1=9.999,
            take_profit_2=9.898,
            signal_id="s",
            S=10.0,
            arm="tp100",
            strategy_version=config.STRATEGY_VERSION,
            filters={"stepSize": 0.001, "minQty": 0.001},
        )
        st.positions["TESTUSDT"] = asdict(pos)
        book = {"bid": 11, "ask": 11.1, "mid": 11.05, "spread": 0.009, "fetched_at": time.time() - 60}
        ev = manage_position(st, "TESTUSDT", book)
        assert ev == []
        assert "TESTUSDT" in st.positions
        print("OK stale quote no exit")


def test_surge_insufficient_history():
    bars = _bars_flat(100)
    m = compute_surge_metrics(bars)
    assert m["ok"] is False and m["reason"] == "INSUFFICIENT_24H_HISTORY"
    print("OK insufficient 24h history")


def test_state_machine_break_rebound_confirm():
    start = 1_700_000_000_000
    # 1441 flat 10s, then elevate + structure so 24h pct uses close[-1]/close[-1441]
    bars: list[Bar] = []
    for i in range(1441):
        ot = start + i * 60_000
        bars.append(Bar(ot, 10.0, 10.0, 10.0, 10.0, ot + 59_999))
    # append structure at ~16 while keeping index -1441 close = 10
    seq = []
    base_ot = start + 1441 * 60_000
    for i in range(5):
        ot = base_ot + i * 60_000
        seq.append(Bar(ot, 16, 16.5, 15.5, 16.0, ot + 59_999))
    ot = base_ot + 5 * 60_000
    seq.append(Bar(ot, 16, 16.2, 15.3, 15.4, ot + 59_999))  # break
    ot = base_ot + 6 * 60_000
    seq.append(Bar(ot, 15.4, 15.8, 15.35, 15.6, ot + 59_999))  # rebound
    ot = base_ot + 7 * 60_000
    seq.append(Bar(ot, 15.6, 15.7, 15.2, 15.3, ot + 59_999))  # confirm
    bars = bars + seq

    st = SymbolState(symbol="TESTUSDT")
    signal = None
    for n in range(len(bars) - 12, len(bars) + 1):
        st, sig = step_state(st, bars[:n])
        if sig:
            signal = sig
            break
    assert signal is not None, (st.phase, st.notes[-5:])
    assert abs(float(signal["S"]) - 15.3) < 1e-9
    assert signal["fill_allowed_after_ms"] == seq[-1].close_time + 1
    print("OK state machine signal", signal["signal_id"])


def test_bc_pids_untouched_note():
    # documentation-level: ensure our profile names don't collide
    assert "paper_reverse_fixed_usdt_50_75" not in config.PROFILE_PARENT
    for a in config.ARMS.values():
        assert a["profile_id"].startswith("paper_a_surge_fade_")
    print("OK profile isolation naming")


def main():
    test_prices_s10()
    test_cancel_drop_2pct()
    test_fill_and_partial_then_entry_stop()
    test_no_fill_before_confirm_end()
    test_stale_quote_no_exit()
    test_surge_insufficient_history()
    test_state_machine_break_rebound_confirm()
    test_bc_pids_untouched_note()
    print("\nALL A ACCEPTANCE PASSED")


if __name__ == "__main__":
    main()
