"""策略政策對齊：部分止盈成功後才啟用含成本保本與移動停利。

與極值保存 bug 修復分開；不改 PARTIAL_TP / TRAIL 參數數值。
"""
from __future__ import annotations

import time
from unittest import mock

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position, load_or_create_account, save_account
from breakout_follow_v1.portfolio import manage_position, mark_price_for_side, try_open
from breakout_follow_v1.util import now_iso
from tests._isolation import isolate_all_paths, restore_paths


def _sig(atr=0.4, entry=100.0, sot=1):
    return {
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry - 0.1,
        "range_low": entry - 1.0,
        "vol_x": 5.0,
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


def test_long_no_trail_until_partial_success(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="LUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        init = get_position(st, "LUSDT").initial_stop
        # 有利但不足以部分止盈
        manage_position(st, "LUSDT", book=_book(100.6), atr_latest=0.4)
        pos = get_position(st, "LUSDT")
        assert pos and not pos.partial_tp_done
        assert pos.trailing_stop is None
        assert pos.breakeven_stop is None
        assert abs(pos.effective_stop - init) < 1e-12
        # 部分止盈成功後才有保本／trail
        ev = manage_position(st, "LUSDT", book=_book(101.5), atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" for e in ev), ev
        pos2 = get_position(st, "LUSDT")
        assert pos2 and pos2.partial_tp_done
        assert pos2.breakeven_stop is not None
        assert pos2.trailing_stop is not None
        assert pos2.effective_stop >= pos2.breakeven_stop - 1e-9
    finally:
        restore_paths(config, saved)


def test_short_no_trail_until_partial_success(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="SUSDT", side="SHORT", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        init = get_position(st, "SUSDT").initial_stop
        manage_position(st, "SUSDT", book=_book(99.4), atr_latest=0.4)
        pos = get_position(st, "SUSDT")
        assert pos and not pos.partial_tp_done and pos.trailing_stop is None
        assert abs(pos.effective_stop - init) < 1e-12
        ev = manage_position(st, "SUSDT", book=_book(98.5), atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" for e in ev), ev
        pos2 = get_position(st, "SUSDT")
        assert pos2 and pos2.partial_tp_done
        assert pos2.breakeven_stop is not None
        assert pos2.trailing_stop is not None
        assert pos2.effective_stop <= pos2.breakeven_stop + 1e-9
    finally:
        restore_paths(config, saved)


def test_partial_tp_failure_does_not_arm_trail(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="FUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        init = get_position(st, "FUSDT").initial_stop
        with mock.patch("breakout_follow_v1.portfolio.close_qty", return_value={"ok": False, "block_reason": "SIM_FAIL"}):
            ev = manage_position(st, "FUSDT", book=_book(101.5), atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" for e in ev)
        assert any(e.get("action") == "PARTIAL_TP_FAILED" for e in ev)
        pos = get_position(st, "FUSDT")
        assert pos and not pos.partial_tp_done
        assert pos.trailing_stop is None
        assert pos.breakeven_stop is None
        assert abs(pos.effective_stop - init) < 1e-12
    finally:
        restore_paths(config, saved)


def test_partial_fraction_zero_skips_arm(tmp_path):
    """qty 進位為 0 時不標記 partial_tp_done、不啟用 trail。"""
    saved = isolate_all_paths(config, tmp_path)
    old = config.PARTIAL_TP_FRACTION
    try:
        config.PARTIAL_TP_FRACTION = 0.0
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="ZUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        manage_position(st, "ZUSDT", book=_book(101.5), atr_latest=0.4)
        pos = get_position(st, "ZUSDT")
        assert pos and not pos.partial_tp_done
        assert pos.trailing_stop is None
    finally:
        config.PARTIAL_TP_FRACTION = old
        restore_paths(config, saved)


def test_partial_success_no_double_and_restart(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="AAAUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]
        ev = manage_position(st, "AAAUSDT", book=_book(101.5), atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" for e in ev)
        pos = get_position(st, "AAAUSDT")
        assert pos and pos.partial_tp_done
        q1 = pos.qty_remaining
        be = pos.breakeven_stop
        trail = pos.trailing_stop
        save_account(st)
        st2 = load_or_create_account()
        p2 = get_position(st2, "AAAUSDT")
        assert p2 and p2.partial_tp_done and abs(p2.qty_remaining - q1) < 1e-9
        assert p2.breakeven_stop == be
        manage_position(st2, "AAAUSDT", book=_book(102.0), atr_latest=0.4)
        p3 = get_position(st2, "AAAUSDT")
        assert p3 and p3.partial_tp_done and abs(p3.qty_remaining - q1) < 1e-9
        assert p3.trailing_stop is not None
        # trail 可收緊但不應放寬到比先前更差（多頭）
        if trail is not None and p3.trailing_stop is not None:
            # effective_stop 不放寬
            assert p3.effective_stop >= min(be, trail) - 1e-9
    finally:
        restore_paths(config, saved)


def test_initial_stop_still_works_before_partial(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="IUSDT", side="LONG", signal=_sig(atr=0.5), source_tags=["T"], filters=_filt())["ok"]
        pos = get_position(st, "IUSDT")
        stop = pos.initial_stop
        # 跌破初始止損（尚未部分止盈）
        ev = manage_position(st, "IUSDT", book=_book(stop - 0.05), atr_latest=0.5)
        assert any(e.get("action") == "INITIAL_STOP" for e in ev), ev
        assert get_position(st, "IUSDT") is None
        hit = [e for e in ev if e.get("action") == "INITIAL_STOP"][0]
        # 成交價為當下 mark，不是倒填 stop 價
        assert hit["fill_price"] < stop
        assert abs(hit["fill_price"] - hit.get("stop_level", stop)) > 1e-9 or hit["fill_price"] != stop
    finally:
        restore_paths(config, saved)


def test_atr_increase_does_not_widen_after_partial(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="WUSDT", side="LONG", signal=_sig(atr=0.2), source_tags=["T"], filters=_filt())["ok"]
        manage_position(st, "WUSDT", book=_book(101.5), atr_latest=0.2)
        pos = get_position(st, "WUSDT")
        assert pos and pos.partial_tp_done
        stop1 = pos.effective_stop
        manage_position(st, "WUSDT", book=_book(101.5), atr_latest=1.0)
        stop2 = get_position(st, "WUSDT").effective_stop
        assert stop2 >= stop1 - 1e-12
    finally:
        restore_paths(config, saved)


def test_activation_past_stop_uses_mark_not_backfill(tmp_path):
    """部分止盈剛成功且當下報價已越過保本時，只能用 mark 成交，不得倒填停損價。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="BUSDT", side="LONG", signal=_sig(atr=0.4), source_tags=["T"], filters=_filt())["ok"]
        # 先推高極值（仍未部分止盈）
        manage_position(st, "BUSDT", book=_book(100.8), atr_latest=0.4)
        assert not get_position(st, "BUSDT").partial_tp_done
        # 直接用足以觸發部分止盈、但隨後保本檢查用同一 mark 的路徑：
        # 正常 101.5 觸發 partial 後 be≈entry+fees，mark 仍遠高於 be，不會 immediate。
        # 改為：完成 partial 後手動把 be 設到高於 mark 再測 trail hit 路徑——
        # 更直接：partial 成功後下一 tick 用遠低於 trail 的 mark，成交價必須是 mark。
        manage_position(st, "BUSDT", book=_book(101.5), atr_latest=0.4)
        pos = get_position(st, "BUSDT")
        assert pos and pos.partial_tp_done and pos.trailing_stop is not None
        trail = pos.trailing_stop
        extreme_before = pos.extreme_price
        # 跳空低於 trail（且低於 effective_stop）
        crash_book = _book(trail - 1.0)
        expected_mark = mark_price_for_side("LONG", crash_book)
        ev = manage_position(st, "BUSDT", book=crash_book, atr_latest=0.4)
        assert get_position(st, "BUSDT") is None
        hits = [e for e in ev if e.get("action") in ("TRAILING_STOP", "BREAKEVEN_STOP", "INITIAL_STOP")]
        assert hits, ev
        h = hits[0]
        assert abs(h["fill_price"] - expected_mark) < 1e-12
        assert abs(h["fill_price"] - h.get("stop_level", trail)) > 1e-9
        assert abs(h["fill_price"] - extreme_before) > 1e-9
    finally:
        restore_paths(config, saved)


def test_no_backfill_on_breakeven_immediate(tmp_path):
    """強制保本價高於當下 mark 時，immediate 平倉價=mark。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="EUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())["ok"]

        real_be = None

        def _fake_be(pos):
            nonlocal real_be
            # 保本設在當下不可能達到的高位 → 觸發 IMMEDIATE，但仍用 mark 成交
            return 10_000.0

        with mock.patch("breakout_follow_v1.portfolio.breakeven_price", side_effect=_fake_be):
            ev = manage_position(st, "EUSDT", book=_book(101.5), atr_latest=0.4)
        im = [e for e in ev if e.get("action") == "BREAKEVEN_STOP_IMMEDIATE"]
        assert im, ev
        assert abs(im[0]["fill_price"] - im[0].get("stop_level", 0)) > 1.0
        assert im[0]["stop_level"] == 10_000.0
        assert get_position(st, "EUSDT") is None
    finally:
        restore_paths(config, saved)
