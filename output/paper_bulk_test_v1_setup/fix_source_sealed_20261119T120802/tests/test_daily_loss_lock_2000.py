"""日損鎖門檻 2000 USDT：含當日已發生損失；不重設日基準／本金。"""
from __future__ import annotations

import time

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


def _book(mid: float):
    half = mid * 0.0002
    return {
        "bid": mid - half,
        "ask": mid + half,
        "mid": mid,
        "spread": (2 * half) / mid,
        "fetched_at": time.time(),
    }


def test_config_daily_loss_is_2000_not_300():
    assert config.DAILY_LOSS_LOCK_USDT == 2000.0
    assert config.DAILY_LOSS_LOCK_USDT != 300.0
    # 其他風控不變
    assert config.INITIAL_CAPITAL_USDT == 10_000.0
    assert config.MAX_LOSS_PER_TRADE_USDT == 100.0
    assert config.MARGIN_PER_TRADE_USDT == 1_000.0
    assert config.LEVERAGE == 10.0
    assert config.MAX_CONCURRENT_POSITIONS == 3


def test_loss_below_2000_does_not_trip_threshold(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=9_668.78,
            initial_capital_u=10_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=10_000.0,
            day_new_entries_locked=False,
        )
        eq = st.equity_mark(0.0)
        assert eq > st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        # 模擬 engine 條件
        if eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT:
            st.day_new_entries_locked = True
        assert st.day_new_entries_locked is False
        # 舊 300 門檻會鎖，但現行不得殘留 300
        assert eq <= st.day_start_equity_u - 300.0  # 確認此樣本在舊門檻下會觸發
        assert config.DAILY_LOSS_LOCK_USDT == 2000.0
    finally:
        restore_paths(config, saved)


def test_at_or_over_2000_blocks_new_but_exit_ok(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=10_000,
            initial_capital_u=10_000,
            day_key=today_key(),
            day_start_equity_u=10_000.0,
        )
        assert try_open(st, symbol="DUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())["ok"]
        # 模擬已實現大虧：equity = 10000 + realized
        st.realized_net_u = -2_000.0
        st.cash_u = 10_000 + st.realized_net_u - st.used_margin()  # 簡化對齊
        eq = st.equity_mark(0.0)
        assert eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        st.day_new_entries_locked = True
        save_account(st)
        # 新倉拒絕
        r = try_open(st, symbol="EUSDT", side="LONG", signal=_sig(sot=2), source_tags=["T"], filters=_filt())
        assert r["ok"] is False and r["block_reason"] == "DAILY_LOSS_LOCK"
        # 既有倉可退出
        pos = get_position(st, "DUSDT")
        assert pos is not None
        closed = close_qty(st, "DUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="TEST_EXIT", book_ts=time.time())
        assert closed["ok"]
        assert get_position(st, "DUSDT") is None
    finally:
        restore_paths(config, saved)


def test_restart_preserves_day_basis_and_lock(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=9_668.78,
            initial_capital_u=10_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=10_000.0,
            day_new_entries_locked=True,
        )
        save_account(st)
        st2 = load_or_create_account()
        assert st2.initial_capital_u == 10_000.0
        assert abs(st2.realized_net_u - (-331.22)) < 1e-9
        assert st2.day_key == today_key()
        assert abs(st2.day_start_equity_u - 10_000.0) < 1e-9
        assert st2.day_new_entries_locked is True
        # 重載不會因 ensure 改日基準（同日）
        ensure_day_rollover(st2, st2.equity_mark(0.0))
        assert st2.day_new_entries_locked is True
        assert abs(st2.day_start_equity_u - 10_000.0) < 1e-9
    finally:
        restore_paths(config, saved)


def test_reevaluate_dry_keeps_sticky_lock_apply_unlocks_under_2000(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=9_668.78,
            initial_capital_u=10_000,
            realized_net_u=-331.22,
            day_key=today_key(),
            day_start_equity_u=10_000.0,
            day_new_entries_locked=True,
        )
        dry = reevaluate_daily_loss_lock(st, reason="test_dry", apply=False)
        assert dry["should_lock"] is False
        assert dry["locked_before"] is True
        assert dry["locked_after"] is True  # sticky without apply
        assert dry["remaining_headroom_u"] > 1_600
        applied = reevaluate_daily_loss_lock(st, reason="test_apply", apply=True)
        assert applied["changed"] is True
        assert st.day_new_entries_locked is False
        assert abs(st.day_start_equity_u - 10_000.0) < 1e-9
        assert st.day_key == today_key()
    finally:
        restore_paths(config, saved)


def test_exact_2000_locks(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=8_000.0,
            initial_capital_u=10_000,
            realized_net_u=-2_000.0,
            day_key=today_key(),
            day_start_equity_u=10_000.0,
            day_new_entries_locked=False,
        )
        eq = st.equity_mark(0.0)
        assert abs(eq - 8_000.0) < 1e-9
        assert eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT
        rep = reevaluate_daily_loss_lock(st, reason="exact", apply=True)
        assert rep["should_lock"] is True
        assert st.day_new_entries_locked is True
    finally:
        restore_paths(config, saved)
