"""極值保存 bug 修復測試（與「部分止盈後才啟用移動停利」政策測試分開）。"""
from __future__ import annotations

import time

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position, load_or_create_account, save_account
from breakout_follow_v1.portfolio import manage_position, try_open
from breakout_follow_v1.util import now_iso
from tests._isolation import isolate_all_paths, restore_paths


def _sig(atr=0.2, entry=100.0, sot=1, side="LONG"):
    return {
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry - 0.1,
        "range_low": entry - 1.0,
        "vol_x": 5.0,
        "side": side,
    }


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _book(mid: float, ts=None):
    half = mid * 0.0002
    return {
        "bid": mid - half,
        "ask": mid + half,
        "mid": mid,
        "spread": (2 * half) / mid,
        "fetched_at": ts or time.time(),
    }


def test_long_extreme_updates_before_partial_no_trail(tmp_path):
    """極值修復：部分止盈前極值仍更新；政策：此時不得啟動移動停利。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="LUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        init_stop = get_position(st, "LUSDT").initial_stop
        manage_position(st, "LUSDT", book=_book(100.5), atr_latest=0.2)
        manage_position(st, "LUSDT", book=_book(100.9), atr_latest=0.2)
        pos = get_position(st, "LUSDT")
        assert pos is not None
        assert pos.extreme_price > pos.entry_price + 0.5
        assert pos.partial_tp_done is False
        assert pos.trailing_stop is None
        assert abs(pos.effective_stop - init_stop) < 1e-12
    finally:
        restore_paths(config, saved)


def test_short_extreme_updates_before_partial_no_trail(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="SUSDT", side="SHORT", signal=_sig(entry=100.0), source_tags=["T"], filters=_filt())["ok"]
        init_stop = get_position(st, "SUSDT").initial_stop
        manage_position(st, "SUSDT", book=_book(99.5), atr_latest=0.2)
        manage_position(st, "SUSDT", book=_book(99.0), atr_latest=0.2)
        pos = get_position(st, "SUSDT")
        assert pos.extreme_price < pos.entry_price - 0.5
        assert pos.partial_tp_done is False
        assert pos.trailing_stop is None
        assert abs(pos.effective_stop - init_stop) < 1e-12
    finally:
        restore_paths(config, saved)


def test_extreme_never_retreats_on_pullback(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        try_open(st, symbol="LUSDT", side="LONG", signal=_sig(atr=0.5), source_tags=["T"], filters=_filt())
        manage_position(st, "LUSDT", book=_book(100.4), atr_latest=0.5)
        peak = get_position(st, "LUSDT").extreme_price
        manage_position(st, "LUSDT", book=_book(100.25), atr_latest=0.5)
        pos = get_position(st, "LUSDT")
        assert pos is not None
        assert pos.extreme_price == peak
    finally:
        restore_paths(config, saved)


def test_restart_preserves_extreme_twice(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        try_open(st, symbol="LUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())
        manage_position(st, "LUSDT", book=_book(100.8), atr_latest=0.2)
        save_account(st)
        ext = get_position(st, "LUSDT").extreme_price
        stop = get_position(st, "LUSDT").effective_stop
        for _ in range(2):
            st2 = load_or_create_account()
            p = get_position(st2, "LUSDT")
            assert abs(p.extreme_price - ext) < 1e-12
            assert abs(p.effective_stop - stop) < 1e-12
            assert p.partial_tp_done is False
            save_account(st2)
    finally:
        restore_paths(config, saved)
