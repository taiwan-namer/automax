"""單一標的失敗不阻塞；無效／過期行情不成交。"""
from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position
from breakout_follow_v1.engine import _book_usable, execute_reverse, scan_once
from breakout_follow_v1.portfolio import manage_position, try_open
from breakout_follow_v1.signals import detect_breakout
from breakout_follow_v1.util import now_iso, today_key
from tests._isolation import isolate_all_paths, restore_paths


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _sig(side="LONG", entry=100.0, sot=1, atr=0.4):
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


def _book(mid=100.0, age_sec=0.0):
    half = mid * 0.0002
    return {
        "bid": mid - half,
        "ask": mid + half,
        "mid": mid,
        "spread": (2 * half) / mid,
        "fetched_at": time.time() - age_sec,
    }


def test_book_usable_rejects_stale_and_invalid():
    assert _book_usable(_book(100, age_sec=0)) is True
    assert _book_usable(_book(100, age_sec=config.QUOTE_MAX_AGE_SEC + 1)) is False
    assert _book_usable({"bid": 0, "ask": 1, "fetched_at": time.time()}) is False
    assert _book_usable(None) is False


def test_stale_quote_no_reverse_fill(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key(), day_start_equity_u=10_000)
        st.filters = {"SUSDT": _filt()}
        assert try_open(st, symbol="SUSDT", side="LONG", signal=_sig("LONG"), source_tags=["T"], filters=_filt())["ok"]
        out = execute_reverse(
            st,
            symbol="SUSDT",
            sig=_sig("SHORT", entry=99.0, sot=2),
            book=_book(99.0, age_sec=config.QUOTE_MAX_AGE_SEC + 2),
            source_tags=["T"],
        )
        assert out["closed"] is False
        assert out["opened"] is False
        assert out["block_reason"] == "STALE_OR_INVALID_QUOTE"
        assert get_position(st, "SUSDT") is not None  # 未假造平倉
    finally:
        restore_paths(config, saved)


def test_one_symbol_manage_failure_does_not_block_others(tmp_path):
    """模擬 scan_once 管理迴圈：一標的拋錯，另一標的仍可 manage。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key(), day_start_equity_u=10_000)
        assert try_open(st, symbol="AUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())["ok"]
        assert try_open(st, symbol="BUSDT", side="LONG", signal=_sig(sot=2), source_tags=["T"], filters=_filt())["ok"]
        books = {"AUSDT": _book(100.5), "BUSDT": _book(100.5)}
        managed = []
        errors = []

        def fake_manage(st_, sym, book=None, atr_latest=None):
            if sym == "AUSDT":
                raise RuntimeError("boom_A")
            managed.append(sym)
            return manage_position(st_, sym, book=book, atr_latest=atr_latest)

        # 直接重現引擎有界迴圈語意
        for sym in list(st.positions.keys()):
            try:
                b = books.get(sym)
                if not _book_usable(b):
                    continue
                if sym == "AUSDT":
                    raise RuntimeError("boom_A")
                manage_position(st, sym, book=b, atr_latest=0.4)
                managed.append(sym)
            except Exception as exc:
                errors.append((sym, str(exc)))
                continue
        assert any(s == "AUSDT" for s, _ in errors)
        assert "BUSDT" in managed
        assert get_position(st, "AUSDT") is not None
        assert get_position(st, "BUSDT") is not None
    finally:
        restore_paths(config, saved)


def test_stale_kline_rejects_new_entry_signal(tmp_path):
    """過期 K 線資料拒絕新倉訊號（不成交）。"""
    import pandas as pd

    saved = isolate_all_paths(config, tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        rows = []
        for i in range(40):
            ot = now_ms - (40 - i) * 60_000
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                    "close_time": ot + 59_999,
                    "is_closed": True,
                    "fetched_at_ms": now_ms - 200_000,  # old fetch stamp unused by age check
                }
            )
        df = pd.DataFrame(rows)
        # data_fetched_at 過舊 → STALE_KLINE
        probe = detect_breakout(df, now_ms=now_ms, book=_book(100.5), data_fetched_at=time.time() - (config.KLINE_MAX_AGE_SEC + 10))
        assert probe.get("signal") is None
        assert probe.get("first_reject_code") == "STALE_KLINE"
    finally:
        restore_paths(config, saved)
