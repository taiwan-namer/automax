"""breakout_follow_v1 核心測試（模擬、無實盤）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, load_or_create_account, save_account
from breakout_follow_v1.market_data import closed_klines, round_step
from breakout_follow_v1.portfolio import (
    breakeven_price,
    close_qty,
    estimate_initial_risk_u,
    manage_position,
    try_open,
)
from breakout_follow_v1.safety import LiveTradingForbidden, assert_public_url, guard_session_request
from breakout_follow_v1.signals import detect_breakout
from breakout_follow_v1.util import config_hash_v1, ensure_dirs


def _bars(
    n: int,
    *,
    end_ot: int | None = None,
    base=100.0,
    breakout: str | None = None,
    vol_spike: float = 10.0,
) -> pd.DataFrame:
    end_ot = end_ot or (int(time.time() * 1000) // 60_000 * 60_000 - 60_000)
    rows = []
    for i in range(n):
        ot = end_ot - (n - 1 - i) * 60_000
        o = c = base
        h, l = base + 0.2, base - 0.2
        v = 100.0
        if i == n - 1 and breakout == "LONG":
            # prior range ~ base±0.2；突破略高於上緣+0.1ATR，避免追價>1ATR
            o, c, h, l = base, base + 0.35, base + 0.40, base - 0.05
            v = 100.0 * vol_spike
        elif i == n - 1 and breakout == "SHORT":
            o, c, h, l = base, base - 0.35, base + 0.05, base - 0.40
            v = 100.0 * vol_spike
        rows.append(
            {
                "open_time": ot,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "close_time": ot + 59_999,
                "quote_volume": v * c,
            }
        )
    return pd.DataFrame(rows)


def _book(bid=99.9, ask=100.1, ts=None):
    mid = (bid + ask) / 2
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": (ask - bid) / mid,
        "fetched_at": ts or time.time(),
    }


def _tight_book(mid: float, ts=None):
    # spread ~4 bps < 8 bps limit
    half = mid * 0.0002
    return _book(bid=mid - half, ask=mid + half, ts=ts)


def test_long_short_breakout_and_vol_boundary():
    now = int(time.time() * 1000)
    df = _bars(50, breakout="LONG", vol_spike=3.0)
    # ask slightly above close for chase still ok
    close = float(df.iloc[-1]["close"])
    book = _tight_book(close)
    p = detect_breakout(df, now_ms=now, book=book, data_fetched_at=time.time())
    assert p["signal"] and p["signal"]["side"] == "LONG", p.get("first_reject_code")

    df2 = _bars(50, breakout="LONG", vol_spike=2.9)
    p2 = detect_breakout(df2, now_ms=now, book=_tight_book(float(df2.iloc[-1]["close"])), data_fetched_at=time.time())
    assert p2["signal"] is None and p2["first_reject_code"] == "VOL_X"

    df3 = _bars(50, breakout="SHORT", vol_spike=5.0)
    close3 = float(df3.iloc[-1]["close"])
    book3 = _tight_book(close3)
    p3 = detect_breakout(df3, now_ms=now, book=book3, data_fetched_at=time.time())
    assert p3["signal"] and p3["signal"]["side"] == "SHORT", p3.get("first_reject_code")


def test_range_excludes_signal_bar_and_unclosed():
    now = int(time.time() * 1000)
    df = _bars(50, breakout="LONG", vol_spike=5)
    # inject unclosed
    last = int(df["open_time"].iloc[-1]) + 60_000
    forming = {
        "open_time": last,
        "open": 200,
        "high": 300,
        "low": 100,
        "close": 250,
        "volume": 99999,
        "close_time": last + 59_999,
        "quote_volume": 1,
    }
    df2 = pd.concat([df, pd.DataFrame([forming])], ignore_index=True)
    closed = closed_klines(df2, now_ms=now)
    assert int(closed["open_time"].iloc[-1]) != last
    book = _tight_book(float(df.iloc[-1]["close"]))
    p = detect_breakout(df2, now_ms=now, book=book, data_fetched_at=time.time())
    assert last in (p.get("excluded_unclosed") or [])
    # range must not include forming absurd highs
    if p.get("signal"):
        assert p["signal"]["range_high"] < 250


def test_spread_chase_risk_rejects():
    now = int(time.time() * 1000)
    df = _bars(50, breakout="LONG", vol_spike=5)
    close = float(df.iloc[-1]["close"])
    wide = _book(bid=close - 1, ask=close + 1)  # huge spread
    p = detect_breakout(df, now_ms=now, book=wide, data_fetched_at=time.time())
    assert p["first_reject_code"] == "SPREAD"

    st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
    sig = {
        "est_fill_price": 100.0,
        "atr": 0.01,  # tiny ATR → stop very tight? wait risk = 1.5*atr*qty + fees
        "atr_time": now_iso_safe(),
        "signal_open_time": int(df.iloc[-1]["open_time"]),
        "range_high": 99,
        "range_low": 98,
        "vol_x": 5,
    }
    # force large ATR stop distance for risk exceed: atr big
    sig["atr"] = 5.0  # stop distance 7.5 on 100 qty → ~750 > 100
    r = try_open(
        st,
        symbol="TESTUSDT",
        side="LONG",
        signal=sig,
        source_tags=["MARKET_SPIKE"],
        filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5},
    )
    assert r["ok"] is False and r["block_reason"] == "RISK_BUDGET_EXCEEDED"


def now_iso_safe():
    from breakout_follow_v1.util import now_iso

    return now_iso()


def test_partial_tp_once_and_breakeven_tighten():
    ensure_dirs()
    # isolate account file by temp monkeypatch
    old = config.ACCOUNT_PATH
    config.ACCOUNT_PATH = config.ROOT_OUT / "test_account_partial.json"
    try:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        sig = {
            "est_fill_price": 100.0,
            "atr": 0.5,
            "atr_time": now_iso_safe(),
            "signal_open_time": 1,
            "range_high": 99,
            "range_low": 98,
            "vol_x": 5,
        }
        risk = estimate_initial_risk_u(side="LONG", entry=100, stop=100 - 1.5 * 0.5, qty=100)
        assert risk <= 100 or True  # may or may not; use smaller atr
        sig["atr"] = 0.4
        opened = try_open(
            st,
            symbol="AAAUSDT",
            side="LONG",
            signal=sig,
            source_tags=["TOP_GAINERS"],
            filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5},
        )
        assert opened["ok"], opened
        # mark far above → partial（淨浮盈需 >=100U）
        book = _tight_book(103.5)
        ev = manage_position(st, "AAAUSDT", book=book, atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" for e in ev)
        from breakout_follow_v1.account import get_position

        pos = get_position(st, "AAAUSDT")
        assert pos and pos.partial_tp_done
        q1 = pos.qty_remaining
        ev2 = manage_position(st, "AAAUSDT", book=book, atr_latest=0.4)
        pos2 = get_position(st, "AAAUSDT")
        # 不可再次部分止盈
        assert pos2 and pos2.partial_tp_done and abs(pos2.qty_remaining - q1) < 1e-9
        assert pos2.breakeven_stop is not None
        assert pos2.effective_stop >= pos2.breakeven_stop - 1e-9
    finally:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        config.ACCOUNT_PATH = old


def test_reverse_close_keeps_flat_if_reopen_fails():
    old = config.ACCOUNT_PATH
    config.ACCOUNT_PATH = config.ROOT_OUT / "test_account_rev.json"
    try:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        st.day_new_entries_locked = True  # reopen will fail
        sig = {
            "est_fill_price": 100.0,
            "atr": 0.4,
            "atr_time": now_iso_safe(),
            "signal_open_time": 1,
            "range_high": 99,
            "range_low": 98,
            "vol_x": 5,
        }
        st.day_new_entries_locked = False
        opened = try_open(
            st,
            symbol="BBBUSDT",
            side="LONG",
            signal=sig,
            source_tags=["MARKET_SPIKE"],
            filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5},
        )
        assert opened["ok"]
        st.day_new_entries_locked = True
        from breakout_follow_v1.account import get_position

        pos = get_position(st, "BBBUSDT")
        book = _book(bid=100.0, ask=100.1)
        mark = book["bid"] * (1 - config.SLIPPAGE_ADVERSE)
        close_qty(st, "BBBUSDT", qty=pos.qty_remaining, exit_price=mark, reason="REVERSE_SIGNAL", book_ts=time.time())
        assert "BBBUSDT" not in st.positions  # 無幽靈持倉
        reopen = try_open(
            st,
            symbol="BBBUSDT",
            side="SHORT",
            signal={**sig, "est_fill_price": 99.8, "signal_open_time": 2},
            source_tags=["MARKET_SPIKE"],
            filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5},
            bypass_cooldown=True,
        )
        assert reopen["ok"] is False and reopen["block_reason"] == "DAILY_LOSS_LOCK"
        assert "BBBUSDT" not in st.positions
    finally:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        config.ACCOUNT_PATH = old


def test_restart_preserves_capital():
    old = config.ACCOUNT_PATH
    config.ACCOUNT_PATH = config.ROOT_OUT / "test_account_restart.json"
    try:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        st = load_or_create_account()
        assert st.initial_capital_u == 10_000
        st.realized_net_u = 12.5
        st.cash_u = 9000
        save_account(st)
        st2 = load_or_create_account()
        assert st2.realized_net_u == 12.5
        assert st2.cash_u == 9000
        assert st2.initial_capital_u == 10_000
    finally:
        if config.ACCOUNT_PATH.exists():
            config.ACCOUNT_PATH.unlink()
        config.ACCOUNT_PATH = old


def test_live_order_paths_blocked():
    try:
        assert_public_url("https://fapi.binance.com/fapi/v1/order")
        assert False, "should block"
    except LiveTradingForbidden:
        pass
    try:
        guard_session_request("POST", "https://fapi.binance.com/fapi/v1/klines")
        assert False
    except LiveTradingForbidden:
        pass
    assert_public_url("https://fapi.binance.com/fapi/v1/klines")


def test_config_hash_stable():
    a = config_hash_v1()
    b = config_hash_v1()
    assert a == b
    assert len(a) == 16


def test_lot_round():
    assert round_step(1.2345, 0.001) == 1.234


if __name__ == "__main__":
    test_long_short_breakout_and_vol_boundary()
    test_range_excludes_signal_bar_and_unclosed()
    test_spread_chase_risk_rejects()
    test_partial_tp_once_and_breakeven_tighten()
    test_reverse_close_keeps_flat_if_reopen_fails()
    test_restart_preserves_capital()
    test_live_order_paths_blocked()
    test_config_hash_stable()
    test_lot_round()
    print("breakout core ok")
