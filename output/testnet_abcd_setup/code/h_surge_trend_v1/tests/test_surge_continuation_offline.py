"""Offline synthetic tests for H surge continuation arm.

NOTE: Uses synthetic event-time bars/prices — NOT historical performance.
Does not start Paper/Testnet or touch live ledgers.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python path/to/test_....py`
_CODE = Path(__file__).resolve().parents[2]
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from h_surge_trend_v1 import config
from h_surge_trend_v1.bars import Bar3m
from h_surge_trend_v1.portfolio import (
    Account,
    apply_entry_fill,
    cancel_pending_entry,
    get_surge_store,
    queue_entry,
    reset_surge_store_for_tests,
    sync_surge_store,
)
from h_surge_trend_v1.signals import evaluate_entry_v1 as evaluate_entry
from h_surge_trend_v1.surge_events import (
    STATE_CONSUMED,
    STATE_OBSERVING,
    STATE_RESERVED,
    STATE_SUPERSEDED,
    SurgeEventStore,
    continuation_index,
    expires_at_for,
)


def _bar(start: float, quote: float, o=10.0, h=10.5, l=9.5, c=10.4, n=50) -> Bar3m:
    return Bar3m(
        start_ts=start,
        end_ts=start + 180.0,
        open=o,
        high=h,
        low=l,
        close=c,
        quote_volume=quote,
        trade_count=n,
        complete=False,
    )


def _up_prices(n: int = 60, start: float = 10.0, step: float = 0.01) -> list[float]:
    return [start + i * step for i in range(n)]


def _chop_prices(n: int = 60, mid: float = 10.0) -> list[float]:
    out = []
    for i in range(n):
        out.append(mid + (0.01 if i % 2 == 0 else -0.01))
    return out


def _filters() -> dict:
    return {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.001}


def _reset():
    reset_surge_store_for_tests()


def test_01_origin_direct_immediate():
    """當根爆量＋方向成立 → ORIGIN_DIRECT。"""
    _reset()
    B = 100_000.0
    # 40s into bar, need RVOL>=3 → V >= 3 * B * 40/180 = 66666.67
    T = 1_000_000.0
    bar = _bar(T, quote=80_000.0, c=10.4, h=10.5, l=9.5)
    prices = _up_prices()
    d = evaluate_entry(
        decision_ts=T + 40.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 40.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=None,
    )
    assert d.ok and d.entry_path == "ORIGIN_DIRECT", d
    assert "ORIGIN_DIRECT" in d.tags
    print("PASS 01 origin_direct")


def test_02_continuation_bar1_after_direction_ready():
    """來源棒爆量但方向不明；後一根 cont RVOL>=1.5＋方向 → CONTINUATION_BAR1。"""
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    ev, why = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    assert why == "CREATED" and ev is not None

    # Cont bar1 at T+180; elapsed 40s; cont_rvol>=1.5 → V>=33333; also V>=B*0.5=50000
    bar = _bar(T + 180.0, quote=55_000.0, o=9.6, h=10.3, l=9.5, c=10.25)
    prices = _up_prices(start=9.7, step=0.012)
    d = evaluate_entry(
        decision_ts=T + 180.0 + 40.0,
        bar=bar,
        baseline_median=B * 2,  # rolling may differ; continuation uses frozen
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 220.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert d.ok and d.entry_path == "CONTINUATION_BAR1", (d.reason, d.entry_path, d.metrics)
    assert d.event_id == ev.event_id
    print("PASS 02 continuation_bar1")


def test_03_continuation_volume_or_elapsed_fail():
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    # Too little volume
    bar = _bar(T + 180.0, quote=5_000.0)
    prices = _up_prices()
    d = evaluate_entry(
        decision_ts=T + 220.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 220.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert not d.ok and "NO_CONTINUATION" in d.reason, d.reason

    # elapsed < 30
    bar2 = _bar(T + 180.0, quote=50_000.0)
    d2 = evaluate_entry(
        decision_ts=T + 180.0 + 10.0,
        bar=bar2,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 190.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert not d2.ok, d2.reason
    print("PASS 03 cont volume/elapsed fail")


def test_04_chop_no_entry():
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    bar = _bar(T + 180.0, quote=50_000.0)
    prices = _chop_prices()
    d = evaluate_entry(
        decision_ts=T + 220.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 220.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert not d.ok
    assert d.reason in ("ER_TOO_LOW", "DIRECTION_FILTER_FAIL", "RANGE_TOO_SMALL"), d.reason
    print("PASS 04 chop_no_entry")


def test_05_bar2_ok_bar3_expired():
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    assert continuation_index(T, T + 360.0) == 2
    bar2 = _bar(T + 360.0, quote=50_000.0)
    prices = _up_prices()
    d = evaluate_entry(
        decision_ts=T + 400.0,
        bar=bar2,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 400.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert d.ok and d.entry_path == "CONTINUATION_BAR2", d

    # T+9m = expired; bar at T+540
    store.expire_if_needed("XUSDT", now=T + 540.0)
    assert store.get("XUSDT") is None
    bar3 = _bar(T + 540.0, quote=50_000.0)
    d3 = evaluate_entry(
        decision_ts=T + 580.0,
        bar=bar3,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 580.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=None,
    )
    assert not d3.ok
    print("PASS 05 bar2_ok_bar3_expired")


def test_06_same_bar_surge_no_expire_reset():
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    ev1, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=3.5,
        has_position=False,
        has_pending_entry=False,
    )
    exp = ev1.expires_at
    ev2, why = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 80.0,
        baseline_frozen=B * 1.1,
        source_rvol=5.0,
        has_position=False,
        has_pending_entry=False,
    )
    assert why == "SAME_SOURCE_BAR_KEEP"
    assert ev2.expires_at == exp
    assert abs(ev2.baseline_frozen - B) < 1e-9  # not overwritten
    print("PASS 06 same_bar_no_expire_reset")


def test_07_new_event_supersedes():
    _reset()
    store = SurgeEventStore()
    T = 1_000_000.0
    ev1, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    old_id = ev1.event_id
    ev2, why = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T + 180.0,
        detected_at=T + 220.0,
        baseline_frozen=200_000.0,
        source_rvol=3.2,
        has_position=False,
        has_pending_entry=False,
    )
    assert why == "CREATED"
    assert ev2.event_id != old_id
    assert abs(ev2.baseline_frozen - 200_000.0) < 1e-9
    assert abs(ev2.source_bar_open_time - (T + 180.0)) < 1e-9
    assert any(h.get("state") == STATE_SUPERSEDED for h in store.history)
    print("PASS 07 supersede")


def test_08_origin_preferred_over_continuation():
    """同 tick 原始與延續量都過 → ORIGIN_DIRECT。"""
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    store = SurgeEventStore()
    # event from previous bar
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=B,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    # Now on cont1, but ALSO origin surge vs rolling B
    bar = _bar(T + 180.0, quote=90_000.0)  # high enough for both
    prices = _up_prices()
    d = evaluate_entry(
        decision_ts=T + 220.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 220.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=ev,
    )
    assert d.ok and d.entry_path == "ORIGIN_DIRECT", d
    print("PASS 08 origin_preferred")


def test_09_reserve_partial_consume_no_double():
    _reset()
    st = Account(cash_u=5000.0)
    reset_surge_store_for_tests()
    store = get_surge_store(st)
    T = 1_000_000.0
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    sync_surge_store(st)
    # Force LIMIT path for partial/cancel race testing
    old = config.ENTRY_ORDER_TYPE
    config.ENTRY_ORDER_TYPE = "LIMIT"
    try:
        ok, reason, order = queue_entry(
            st,
            symbol="XUSDT",
            side="LONG",
            bid=10.0,
            ask=10.01,
            filters=_filters(),
            signal={"event_id": ev.event_id, "entry_path": "ORIGIN_DIRECT", "tags": []},
            leg_index=1,
            bar_start_ts=T,
            now=T + 50.0,
        )
        assert ok, reason
        assert store.get("XUSDT").state == STATE_RESERVED
        # partial fill consumes
        apply_entry_fill(
            st,
            "XUSDT",
            fill_px=10.01,
            fill_qty=10.0,
            fill_ts=T + 51.0,
            partial=True,
        )
        assert store.get("XUSDT") is None
        assert any(h.get("state") == STATE_CONSUMED for h in store.history)
        # second first entry same bar blocked
        ok2, reason2, _ = queue_entry(
            st,
            symbol="XUSDT",
            side="LONG",
            bid=10.0,
            ask=10.01,
            filters=_filters(),
            signal={"event_id": "other", "entry_path": "ORIGIN_DIRECT", "tags": []},
            leg_index=1,
            bar_start_ts=T,
            now=T + 52.0,
        )
        assert not ok2 and reason2 in (
            "SAME_BAR_FIRST_ENTRY_ALREADY_SENT",
            "ALREADY_IN_POSITION",
            "ENTRY_ORDER_ALREADY_PENDING",
        ), reason2
    finally:
        config.ENTRY_ORDER_TYPE = old
    print("PASS 09 reserve_partial_no_double")


def test_09b_zero_fill_cancel_restores():
    _reset()
    st = Account(cash_u=5000.0)
    reset_surge_store_for_tests()
    store = get_surge_store(st)
    T = 1_000_000.0
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    sync_surge_store(st)
    old = config.ENTRY_ORDER_TYPE
    config.ENTRY_ORDER_TYPE = "LIMIT"
    try:
        ok, reason, order = queue_entry(
            st,
            symbol="XUSDT",
            side="LONG",
            bid=10.0,
            ask=10.01,
            filters=_filters(),
            signal={"event_id": ev.event_id, "entry_path": "CONTINUATION_BAR1", "tags": []},
            leg_index=1,
            bar_start_ts=T + 180.0,
            now=T + 220.0,
        )
        assert ok, reason
        cancel_pending_entry(st, "XUSDT", "TTL_EXPIRED", now=T + 226.0)
        ev2 = store.get("XUSDT")
        assert ev2 is not None and ev2.state == STATE_OBSERVING
    finally:
        config.ENTRY_ORDER_TYPE = old
    print("PASS 09b zero_fill_cancel_restore")


def test_10_exit_consumed_not_reuse_need_new():
    _reset()
    st = Account(cash_u=5000.0)
    reset_surge_store_for_tests()
    store = get_surge_store(st)
    T = 1_000_000.0
    ev, _ = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    store.consume_on_fill("XUSDT", now=T + 50.0)
    assert store.get("XUSDT") is None
    # old event cannot be passed — continuation needs active event
    bar = _bar(T + 180.0, quote=50_000.0)
    prices = _up_prices()
    d = evaluate_entry(
        decision_ts=T + 220.0,
        bar=bar,
        baseline_median=100_000.0,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 220.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
        surge_event=None,
    )
    assert not d.ok
    # new origin after flat still works
    ev_new, why = store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T + 360.0,
        detected_at=T + 400.0,
        baseline_frozen=100_000.0,
        source_rvol=3.5,
        has_position=False,
        has_pending_entry=False,
    )
    assert why == "CREATED" and ev_new is not None
    print("PASS 10 consumed_not_reuse")


def test_11_top15_drop_and_restart_no_revive():
    _reset()
    store = SurgeEventStore()
    T = 1_000_000.0
    store.try_create_origin(
        symbol="XUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    store.invalidate("XUSDT", reason="LEFT_TOP15", now=T + 100.0)
    assert store.get("XUSDT") is None
    # restart validate expires old
    store2 = SurgeEventStore()
    store2.try_create_origin(
        symbol="YUSDT",
        source_bar_open_time=T,
        detected_at=T + 40.0,
        baseline_frozen=100_000.0,
        source_rvol=4.0,
        has_position=False,
        has_pending_entry=False,
    )
    notes = store2.validate_after_restart(now=T + 540.0)
    assert store2.get("YUSDT") is None
    assert any(n.get("action") == "EXPIRED_ON_RESTART" for n in notes)
    print("PASS 11 top15_restart")


def test_12_sides_and_same_bar_lock_after_flat():
    _reset()
    B = 100_000.0
    T = 1_000_000.0
    # SHORT path origin
    bar = _bar(T, quote=80_000.0, o=10.5, h=10.6, l=9.8, c=9.85)
    # down prices for short trend
    prices = [10.5 - i * 0.01 for i in range(60)]
    d = evaluate_entry(
        decision_ts=T + 40.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 40.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
    )
    assert d.ok and d.side == "SHORT", d

    # same-bar lock
    d2 = evaluate_entry(
        decision_ts=T + 50.0,
        bar=bar,
        baseline_median=B,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T + 50.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=True,
    )
    assert not d2.ok and d2.reason == "SAME_BAR_FIRST_ENTRY_ALREADY_SENT"
    print("PASS 12 sides_and_same_bar_lock")


def test_13_no_future_bar_backfill():
    """決策只用 decision_ts 已知 bar 狀態；elapsed 不可為負／未來。"""
    T = 1_000_000.0
    bar = _bar(T, quote=80_000.0)
    prices = _up_prices()
    # decision before bar start → incomplete path
    d = evaluate_entry(
        decision_ts=T - 1.0,
        bar=bar,
        baseline_median=100_000.0,
        prices_60=prices,
        prices_15=prices[-15:],
        last_trade_ts=T - 1.0,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        first_entry_already_sent_this_bar=False,
    )
    assert not d.ok
    assert expires_at_for(T) == T + 540.0
    print("PASS 13 event_time_only")


def main() -> int:
    print("=== H surge continuation offline tests (SYNTHETIC, not performance) ===")
    print("version", config.STRATEGY_VERSION, "config_hash", config.config_hash()[:16])
    tests = [
        test_01_origin_direct_immediate,
        test_02_continuation_bar1_after_direction_ready,
        test_03_continuation_volume_or_elapsed_fail,
        test_04_chop_no_entry,
        test_05_bar2_ok_bar3_expired,
        test_06_same_bar_surge_no_expire_reset,
        test_07_new_event_supersedes,
        test_08_origin_preferred_over_continuation,
        test_09_reserve_partial_consume_no_double,
        test_09b_zero_fill_cancel_restores,
        test_10_exit_consumed_not_reuse_need_new,
        test_11_top15_drop_and_restart_no_revive,
        test_12_sides_and_same_bar_lock_after_flat,
        test_13_no_future_bar_backfill,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"=== done failed={failed}/{len(tests)} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
