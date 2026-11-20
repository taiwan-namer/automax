"""日損鎖 2000：僅 paper_bulk_test_v1；舊 profile 基準維持 300。"""
from __future__ import annotations

import time
from pathlib import Path

from breakout_follow_v1 import config
from breakout_follow_v1.account import (
    AccountState,
    ensure_day_rollover,
    get_position,
    load_or_create_account,
    reevaluate_daily_loss_lock,
    save_account,
)
from breakout_follow_v1.portfolio import close_qty, try_open
from breakout_follow_v1.profiles import LEGACY_PROFILE_BASELINE, PAPER_BULK_TEST_V1
from breakout_follow_v1.util import now_iso, today_key
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


def _apply_bulk_2000(tmp_path):
    """2000 門檻測試一律走 bulk overlay，不得污染舊 profile 預設。"""
    saved = isolate_all_paths(config, tmp_path)
    applied = config.apply_profile("paper_bulk_test_v1")
    root = Path(config.ROOT_OUT)
    root.mkdir(parents=True, exist_ok=True)
    (root / "decision_snapshots").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    Path(config.KLINE_VAULT_DIR).mkdir(parents=True, exist_ok=True)
    return saved, applied


def _restore(saved):
    restore_paths(config, saved)
    config.apply_profile("breakout_follow_v1")


def test_legacy_default_is_300_bulk_is_2000():
    # 模組預設＝舊基準 300；不得被全域 2000 污染
    assert LEGACY_PROFILE_BASELINE["DAILY_LOSS_LOCK_USDT"] == 300.0
    assert PAPER_BULK_TEST_V1["DAILY_LOSS_LOCK_USDT"] == 2000.0
    config.apply_profile("breakout_follow_v1")
    assert config.DAILY_LOSS_LOCK_USDT == 300.0
    assert config.INITIAL_CAPITAL_USDT == 10_000.0
    assert config.MAX_CONCURRENT_POSITIONS == 3
    config.apply_profile("paper_bulk_test_v1")
    assert config.DAILY_LOSS_LOCK_USDT == 2000.0
    assert config.INITIAL_CAPITAL_USDT == 50_000.0
    assert config.MAX_CONCURRENT_POSITIONS == 20
    # 切回舊 profile 必須還原 300／10000／3
    config.apply_profile("breakout_follow_v1")
    assert config.DAILY_LOSS_LOCK_USDT == 300.0
    assert config.INITIAL_CAPITAL_USDT == 10_000.0
    assert config.MAX_CONCURRENT_POSITIONS == 3
    assert config.MAX_LOSS_PER_TRADE_USDT == 100.0
    assert config.MARGIN_PER_TRADE_USDT == 1_000.0
    assert config.LEVERAGE == 10.0


def test_loss_below_2000_does_not_trip_threshold(tmp_path):
    saved, _ = _apply_bulk_2000(tmp_path)
    try:
        assert config.DAILY_LOSS_LOCK_USDT == 2000.0
        st = AccountState(
            run_id="t",
            cash_u=48_668.78,
            initial_capital_u=50_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=50_000.0,
            day_new_entries_locked=False,
        )
        eq = st.equity_mark(0.0)
        assert eq > st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        if eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT:
            st.day_new_entries_locked = True
        assert st.day_new_entries_locked is False
        # 同損失若在舊 300 門檻會鎖；bulk 2000 不鎖
        assert eq <= st.day_start_equity_u - 300.0
        assert config.DAILY_LOSS_LOCK_USDT == 2000.0
    finally:
        _restore(saved)


def test_at_or_over_2000_blocks_new_but_exit_ok(tmp_path):
    saved, _ = _apply_bulk_2000(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=50_000,
            initial_capital_u=50_000,
            day_key=today_key(),
            day_start_equity_u=50_000.0,
        )
        assert try_open(st, symbol="DUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())["ok"]
        st.realized_net_u = -2_000.0
        st.cash_u = 50_000 + st.realized_net_u - st.used_margin()
        eq = st.equity_mark(0.0)
        assert eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        st.day_new_entries_locked = True
        save_account(st)
        r = try_open(st, symbol="EUSDT", side="LONG", signal=_sig(sot=2), source_tags=["T"], filters=_filt())
        assert r["ok"] is False and r["block_reason"] == "DAILY_LOSS_LOCK"
        pos = get_position(st, "DUSDT")
        assert pos is not None
        closed = close_qty(st, "DUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="TEST_EXIT", book_ts=time.time())
        assert closed["ok"]
        assert get_position(st, "DUSDT") is None
    finally:
        _restore(saved)


def test_restart_preserves_day_basis_and_lock(tmp_path):
    saved, _ = _apply_bulk_2000(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=48_668.78,
            initial_capital_u=50_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=50_000.0,
            day_new_entries_locked=True,
        )
        save_account(st)
        st2 = load_or_create_account()
        assert st2.initial_capital_u == 50_000.0
        assert abs(st2.realized_net_u - (-331.22)) < 1e-9
        assert st2.day_key == today_key()
        assert abs(st2.day_start_equity_u - 50_000.0) < 1e-9
        assert st2.day_new_entries_locked is True
        ensure_day_rollover(st2, st2.equity_mark(0.0))
        assert st2.day_new_entries_locked is True
        assert abs(st2.day_start_equity_u - 50_000.0) < 1e-9
    finally:
        _restore(saved)


def test_reevaluate_dry_keeps_sticky_lock_apply_unlocks_under_2000(tmp_path):
    saved, _ = _apply_bulk_2000(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=48_668.78,
            initial_capital_u=50_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=50_000.0,
            day_new_entries_locked=True,
        )
        dry = reevaluate_daily_loss_lock(st, reason="test_dry", apply=False)
        assert dry["should_lock"] is False
        assert dry["locked_before"] is True
        assert dry["locked_after"] is True
        assert dry["remaining_headroom_u"] > 1_600
        applied = reevaluate_daily_loss_lock(st, reason="test_apply", apply=True)
        assert applied["changed"] is True
        assert st.day_new_entries_locked is False
        assert abs(st.day_start_equity_u - 50_000.0) < 1e-9
        assert st.day_key == today_key()
    finally:
        _restore(saved)


def test_exact_2000_locks(tmp_path):
    saved, _ = _apply_bulk_2000(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=48_000.0,
            initial_capital_u=50_000,
            realized_net_u=-2_000.0,
            day_key=today_key(),
            day_start_equity_u=50_000.0,
            day_new_entries_locked=False,
        )
        eq = st.equity_mark(0.0)
        assert abs(eq - 48_000.0) < 1e-9
        assert eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        rep = reevaluate_daily_loss_lock(st, reason="exact", apply=True)
        assert rep["should_lock"] is True
        assert st.day_new_entries_locked is True
    finally:
        _restore(saved)
