"""訊號去重：重啟保留、成功阻擋、拒絕不應誤擋合法後續訊號。"""
from __future__ import annotations

import time

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position, load_or_create_account, save_account
from breakout_follow_v1.portfolio import close_qty, try_open
from breakout_follow_v1.util import now_iso, today_key
from tests._isolation import isolate_all_paths, restore_paths


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _sig(sot: int, side="LONG", entry=100.0, atr=0.4):
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


def test_success_dedup_persists_across_restart(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key())
        assert try_open(st, symbol="DUSDT", side="LONG", signal=_sig(10), source_tags=["T"], filters=_filt())["ok"]
        assert "DUSDT:10:LONG" in st.processed_signals
        save_account(st)
        st2 = load_or_create_account()
        assert "DUSDT:10:LONG" in st2.processed_signals
        # flatten then same key must still refuse
        pos = get_position(st2, "DUSDT")
        close_qty(st2, "DUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="T", book_ts=time.time())
        r = try_open(st2, symbol="DUSDT", side="LONG", signal=_sig(10), source_tags=["T"], filters=_filt(), bypass_cooldown=True)
        assert r["ok"] is False and r["block_reason"] == "DUPLICATE_SIGNAL"
    finally:
        restore_paths(config, saved)


def test_rejected_try_open_does_not_consume_key_in_portfolio_layer(tmp_path):
    """try_open 本身只在成功時寫入 processed_signals；被拒不得永久吃掉該 key。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key())
        # ATR 過大 → RISK_BUDGET
        r1 = try_open(st, symbol="RUSDT", side="LONG", signal=_sig(20, atr=5.0), source_tags=["T"], filters=_filt())
        assert r1["ok"] is False and r1["block_reason"] == "RISK_BUDGET_EXCEEDED"
        assert "RUSDT:20:LONG" not in st.processed_signals
        # 同 key 改為合法 ATR 應可開
        r2 = try_open(st, symbol="RUSDT", side="LONG", signal=_sig(20, atr=0.4), source_tags=["T"], filters=_filt())
        assert r2["ok"] is True, r2
        assert "RUSDT:20:LONG" in st.processed_signals
    finally:
        restore_paths(config, saved)


def test_engine_oneshot_block_still_allows_new_signal_bar(tmp_path):
    """engine 對「已嘗試開倉」的 key 會 append（含 BLOCKED）；不同 signal_open_time 必須仍可開。"""
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key())
        # 模擬 engine：嘗試後無論成敗都 append
        r1 = try_open(st, symbol="EUSDT", side="LONG", signal=_sig(30), source_tags=["T"], filters=_filt())
        assert r1["ok"]
        st.processed_signals.append("EUSDT:30:LONG")  # engine 也會再 append，允許重複
        # 模擬 MAX_POSITIONS 擋下另一檔後仍標記該 key
        st.processed_signals.append("FUSDT:30:LONG")
        # 新一根訊號根（sot=31）必須合法
        # 先平掉以空出倉位
        pos = get_position(st, "EUSDT")
        close_qty(st, "EUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="T", book_ts=time.time())
        r_new = try_open(st, symbol="EUSDT", side="LONG", signal=_sig(31), source_tags=["T"], filters=_filt(), bypass_cooldown=True)
        assert r_new["ok"] is True, r_new
        # 舊 key 仍擋
        r_old = try_open(st, symbol="FUSDT", side="LONG", signal=_sig(30), source_tags=["T"], filters=_filt(), bypass_cooldown=True)
        assert r_old["ok"] is False and r_old["block_reason"] == "DUPLICATE_SIGNAL"
    finally:
        restore_paths(config, saved)


def test_opposite_side_different_key_allowed(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000, day_key=today_key())
        assert try_open(st, symbol="OUSDT", side="LONG", signal=_sig(40), source_tags=["T"], filters=_filt())["ok"]
        pos = get_position(st, "OUSDT")
        close_qty(st, "OUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="T", book_ts=time.time())
        # 同根反向（反手語意）用不同 side key
        r = try_open(
            st,
            symbol="OUSDT",
            side="SHORT",
            signal=_sig(40, side="SHORT", entry=99.5),
            source_tags=["T"],
            filters=_filt(),
            bypass_cooldown=True,
        )
        assert r["ok"] is True, r
        assert "OUSDT:40:SHORT" in st.processed_signals
    finally:
        restore_paths(config, saved)
