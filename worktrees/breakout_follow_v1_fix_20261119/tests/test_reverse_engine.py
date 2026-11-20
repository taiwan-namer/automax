"""Engine-layer reverse integration (no network)."""
from __future__ import annotations

import time

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position
from breakout_follow_v1.engine import execute_reverse
from breakout_follow_v1.portfolio import try_open
from breakout_follow_v1.util import now_iso, today_key
from tests._isolation import isolate_all_paths, restore_paths


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _st():
    return AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key(), day_start_equity_u=10_000)


def _open_sig(side="LONG", entry=100.0, sot=1, atr=0.4):
    return {
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry - 0.1 if side == "LONG" else entry + 1.0,
        "range_low": entry - 1.0 if side == "LONG" else entry + 0.1,
        "vol_x": 5.0,
        "side": side,
    }


def _book(mid=100.0):
    half = mid * 0.0002
    return {"bid": mid - half, "ask": mid + half, "mid": mid, "spread": 2 * half / mid, "fetched_at": time.time()}


def test_full_reverse_opens_opposite(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = _st()
        st.filters = {"REVUSDT": _filt()}
        assert try_open(st, symbol="REVUSDT", side="LONG", signal=_open_sig("LONG", sot=1), source_tags=["T"], filters=_filt())["ok"]
        # opposite full breakout signal (not mere price tick)
        rev_sig = _open_sig("SHORT", entry=99.5, sot=2)
        out = execute_reverse(st, symbol="REVUSDT", sig=rev_sig, book=_book(99.5), source_tags=["MARKET_SPIKE"])
        assert out["closed"] is True
        assert out["opened"] is True
        pos = get_position(st, "REVUSDT")
        assert pos is not None and pos.side == "SHORT"
        # never long+short same symbol
        assert len([p for p in st.positions.values() if p["symbol"] == "REVUSDT"]) == 1
    finally:
        restore_paths(config, saved)


def test_reopen_rejected_keeps_flat(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = _st()
        st.filters = {"REVUSDT": _filt()}
        try_open(st, symbol="REVUSDT", side="LONG", signal=_open_sig("LONG"), source_tags=["T"], filters=_filt())
        st.day_new_entries_locked = True
        out = execute_reverse(st, symbol="REVUSDT", sig=_open_sig("SHORT", sot=2), book=_book(99.5), source_tags=["T"])
        assert out["closed"] is True
        assert out["opened"] is False
        assert out.get("block_reason") == "DAILY_LOSS_LOCK"
        assert "REVUSDT" not in st.positions
    finally:
        restore_paths(config, saved)


def test_close_failed_does_not_open(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = _st()
        # no position → close fails path via NO_REVERSE / no open
        out = execute_reverse(st, symbol="NOSYMB", sig=_open_sig("SHORT"), book=_book(), source_tags=["T"])
        assert out["closed"] is False
        assert out["opened"] is False
    finally:
        restore_paths(config, saved)


def test_daily_lock_blocks_new_but_exit_ok(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = _st()
        st.filters = {"REVUSDT": _filt()}
        try_open(st, symbol="REVUSDT", side="LONG", signal=_open_sig("LONG"), source_tags=["T"], filters=_filt())
        st.day_new_entries_locked = True
        # manage/exit still allowed via reverse close
        out = execute_reverse(st, symbol="REVUSDT", sig=_open_sig("SHORT", sot=9), book=_book(99.0), source_tags=["T"])
        assert out["closed"] is True
        assert out["opened"] is False
    finally:
        restore_paths(config, saved)


def test_same_side_signal_is_not_reverse(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = _st()
        try_open(st, symbol="REVUSDT", side="LONG", signal=_open_sig("LONG"), source_tags=["T"], filters=_filt())
        out = execute_reverse(st, symbol="REVUSDT", sig=_open_sig("LONG", sot=3), book=_book(100.5), source_tags=["T"])
        assert out["closed"] is False
        assert get_position(st, "REVUSDT").side == "LONG"
    finally:
        restore_paths(config, saved)
