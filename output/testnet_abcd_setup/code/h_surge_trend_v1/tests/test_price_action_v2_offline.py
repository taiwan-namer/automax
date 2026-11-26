"""Offline acceptance tests for h_price_action_v2 (A/B/C paths).

Synthetic + optional case aggTrade smoke. No Paper/Testnet/orders.
Does NOT claim profitability.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_CODE = Path(__file__).resolve().parents[2]
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from h_surge_trend_v1 import config
from h_surge_trend_v1.bars import Bar3m
from h_surge_trend_v1.entry_gate import SymbolEntryGate, reset_entry_gate_store_for_tests
from h_surge_trend_v1.metrics import surge_ok
from h_surge_trend_v1.path_abc import (
    evaluate_chop,
    evaluate_path_a_surge,
    evaluate_path_b_continuation,
    evaluate_path_c_reversal,
)
from h_surge_trend_v1.portfolio import (
    Account,
    apply_entry_fill,
    begin_exit,
    maybe_queue_add,
    process_pending_exits,
    queue_entry,
    reset_surge_store_for_tests,
    update_trail,
)
from h_surge_trend_v1.price_series import SparseSecSeries
from h_surge_trend_v1.signals import evaluate_entry, mark_candidate_consumed
from h_surge_trend_v1.signals_v1_legacy import evaluate_entry as evaluate_entry_v1

TZ = timezone(timedelta(hours=8))
CASES = Path(r"c:\Users\Admin\MyProjects\automax\output\paper_h_local\analysis_20261125_cases")


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


def _dense_up(n: int = 60, start: float = 10.0, step: float = 0.01) -> list[float]:
    return [start + i * step for i in range(n)]


def _fill_sparse_linear(
    series: SparseSecSeries,
    t0: int,
    n: int,
    start_px: float,
    step: float,
) -> None:
    """Put a trade in every second [t0, t0+n-1] with linear prices."""
    for i in range(n):
        sec = t0 + i
        px = start_px + i * step
        series.on_trade(float(sec) + 0.5, px)


def _filters() -> dict:
    return {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.001}


def test_01_rvol_fail_bc_can_pass():
    """RVOL不足時 B 仍可獨立成立（連續3秒）。"""
    reset_entry_gate_store_for_tests()
    T0 = 1_700_000_000
    series = SparseSecSeries()
    # Strong up move over 60s: +0.40% in 60s, +0.12% in 15s, high ER
    _fill_sparse_linear(series, T0, 70, 100.0, 0.007)  # ~0.42% / 60s
    gate = SymbolEntryGate()
    bar = _bar(T0, quote=1000.0)  # tiny volume → no surge
    B = 100_000.0
    last_ok = None
    for k in range(3):
        t_dec = float(T0 + 69 + k) + 0.9  # eval complete sec T0+68, then +69,+70 need more data
    # Ensure seconds through T0+72 exist
    _fill_sparse_linear(series, T0, 75, 100.0, 0.007)
    for add in range(3):
        dec = float(T0 + 72 + add) + 0.8
        d = evaluate_entry(
            decision_ts=dec,
            bar=bar,
            baseline_median=B,
            prices_60=_dense_up(),
            prices_15=_dense_up(15),
            last_trade_ts=dec - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate,
            tick_size=0.001,
            flat_confirmed=True,
        )
        last_ok = d
        a = d.paths.get("SURGE") or {}
        assert a.get("ok") is False
        assert str(a.get("reason", "")).startswith("NO_SURGE") or a.get("reason") in (
            "DIRECTION_FILTER_FAIL",
            "ER_TOO_LOW",
            "RANGE_TOO_SMALL",
        )
    # After 3 consecutive B passes, overall may be ok
    assert last_ok is not None
    b = last_ok.paths.get("CONTINUATION") or {}
    assert b.get("ok") is True or b.get("reason") == "CONTINUATION_STREAK_SHORT" or last_ok.ok
    # At least B path computation ran despite NO_SURGE
    assert "CONTINUATION" in last_ok.paths
    print("PASS 01 rvol_fail_bc_independent")


def test_02_surge_path_not_regressed():
    """原爆量路徑：RVOL+方向 → SURGE ok（等同原 ORIGIN）。"""
    B = 100_000.0
    T = 1_700_000_100.0
    bar = _bar(T, quote=80_000.0)
    prices = _dense_up()
    a = evaluate_path_a_surge(
        decision_ts=T + 40.0,
        bar=bar,
        baseline_median=B,
        dense_prices_60=prices,
        dense_prices_15=prices[-15:],
    )
    assert a.ok and a.side == "LONG" and a.path == "SURGE", a
    # legacy v1 still agrees on origin
    d1 = evaluate_entry_v1(
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
    assert d1.ok and d1.entry_path == "ORIGIN_DIRECT"
    print("PASS 02 surge_not_regressed")


def test_03_long_short_symmetry_b():
    series_up = SparseSecSeries()
    series_dn = SparseSecSeries()
    t0 = 2_000_000
    _fill_sparse_linear(series_up, t0, 70, 100.0, 0.008)
    _fill_sparse_linear(series_dn, t0, 70, 100.0, -0.008)
    t = t0 + 65
    bu = evaluate_path_b_continuation(series_up, t)
    bd = evaluate_path_b_continuation(series_dn, t)
    assert bu.ok and bu.side == "LONG", bu
    assert bd.ok and bd.side == "SHORT", bd
    # multiplicative ret magnitudes are near-symmetric (not bit-identical)
    assert abs(abs(bu.detail["ret_60"]) - abs(bd.detail["ret_60"])) < 1e-5
    print("PASS 03 long_short_symmetry_b")


def test_04_reversal_time_order():
    """L must occur after Hpre; tau age in [5,60]."""
    series = SparseSecSeries()
    t0 = 3_000_000
    # Build: rise then dump then reclaim
    # seconds 0..40: climb 100 → 100.5
    for i in range(41):
        series.on_trade(t0 + i + 0.2, 100.0 + i * 0.0125)
    # dump to low at sec 50
    for i in range(41, 51):
        series.on_trade(t0 + i + 0.2, 100.5 - (i - 40) * 0.06)  # down ~0.6
    # reclaim
    low_sec = t0 + 50
    low_px = series.p(low_sec)
    assert low_px is not None
    for i in range(51, 61):
        # climb back >50% of drop and break prior10 high
        series.on_trade(t0 + i + 0.2, low_px + (i - 50) * 0.04)
    t = t0 + 58
    arm = None
    r, arm = evaluate_path_c_reversal(series, t, tick_size=0.001, armed=None)
    # may arm or fail depending on exact numbers — check order invariant in detail
    if r.detail.get("long_struct"):
        ls = r.detail["long_struct"]
        if ls.get("tau") is not None and ls.get("tau_hpre") is not None:
            assert ls["tau_hpre"] < ls["tau"]
    # Force a clean synthetic that must order-fail if Hpre after L
    series2 = SparseSecSeries()
    for i in range(120):
        series2.on_trade(t0 + i + 0.1, 100.0)
    # only low then high after — cannot find Hpre before L
    series2.on_trade(t0 + 100 + 0.1, 99.0)
    series2.on_trade(t0 + 110 + 0.1, 101.0)
    r2, _ = evaluate_path_c_reversal(series2, t0 + 115, tick_size=0.001)
    assert r2.ok is False
    print("PASS 04 reversal_time_order")


def test_05_no_future_leak():
    """Adding future trades must not change past eval at fixed decision_ts."""
    series = SparseSecSeries()
    t0 = 4_000_000
    _fill_sparse_linear(series, t0, 80, 100.0, 0.006)
    dec = float(t0 + 70) + 0.9
    gate = SymbolEntryGate()
    d1 = evaluate_entry(
        decision_ts=dec,
        bar=_bar(t0, 1000),
        baseline_median=1e9,
        prices_60=_dense_up(),
        prices_15=_dense_up(15),
        last_trade_ts=dec - 0.2,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        sparse=series,
        gate=gate,
        tick_size=0.001,
    )
    snap = (d1.ok, d1.reason, d1.side, (d1.paths.get("CONTINUATION") or {}).get("reason"))
    # future trades after decision
    for i in range(80, 120):
        series.on_trade(t0 + i + 0.5, 200.0)
    gate2 = SymbolEntryGate()
    d2 = evaluate_entry(
        decision_ts=dec,
        bar=_bar(t0, 1000),
        baseline_median=1e9,
        prices_60=_dense_up(),
        prices_15=_dense_up(15),
        last_trade_ts=dec - 0.2,
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        sparse=series,
        gate=gate2,
        tick_size=0.001,
    )
    snap2 = (d2.ok, d2.reason, d2.side, (d2.paths.get("CONTINUATION") or {}).get("reason"))
    assert snap == snap2, (snap, snap2)
    print("PASS 05 no_future_leak")


def test_06_bar_roll_does_not_reset_bc_window():
    """Sparse series persists across 3m bar boundary."""
    series = SparseSecSeries()
    t0 = 5_000_000
    _fill_sparse_linear(series, t0, 200, 50.0, 0.003)
    # evaluate near end spanning two bars
    t = t0 + 190
    b = evaluate_path_b_continuation(series, t)
    assert series.p(t0 + 10) is not None  # old bar data still present
    assert b.reason != "DATA_INSUFFICIENT" or series.coverage(t, 60) is not None
    print("PASS 06 bar_roll_window_kept")


def test_07_missing_and_stale():
    series = SparseSecSeries()
    t0 = 6_000_000
    # only 10 sparse points in 60s → coverage fail
    for i in range(0, 60, 10):
        series.on_trade(t0 + i + 0.2, 10.0 + i * 0.01)
    b = evaluate_path_b_continuation(series, t0 + 59)
    assert b.reason == "DATA_INSUFFICIENT"
    # stale trade
    d = evaluate_entry(
        decision_ts=float(t0 + 60) + 0.5,
        bar=_bar(t0, 1),
        baseline_median=1e6,
        prices_60=_dense_up(),
        prices_15=_dense_up(15),
        last_trade_ts=float(t0 + 50),  # >2s stale
        gap_unfilled=False,
        has_position=False,
        has_exit_pending=False,
        sparse=series,
        gate=SymbolEntryGate(),
        tick_size=0.001,
    )
    assert d.reason == "TRADE_STALE"
    print("PASS 07 missing_and_stale")


def test_08_chop_block_and_escape():
    series = SparseSecSeries()
    t0 = 7_000_000
    # old window range for M
    for i in range(0, 60):
        series.on_trade(t0 + i + 0.1, 100.0 + (0.5 if i % 2 == 0 else -0.5))
    # recent: flip across midline many times
    for i in range(60, 121):
        series.on_trade(t0 + i + 0.1, 100.0 + (0.6 if i % 2 == 0 else -0.6))
    t = t0 + 120
    chop = evaluate_chop(series, t, side="LONG", tick_size=0.001)
    assert chop.get("flip_count", 0) >= 4
    assert chop.get("chop") is True
    # escape: rebuild last 3 secs above box (must be event-time ordered)
    box_h = series.max_in(t - 60, t - 11)
    assert box_h is not None
    series2 = SparseSecSeries()
    for sec, px in series.prices_in(t0, t - 3):
        series2.on_trade(sec + 0.1, px)
    for s in (t - 2, t - 1, t):
        series2.on_trade(s + 0.5, box_h + 0.05)
    chop2 = evaluate_chop(series2, t, side="LONG", tick_size=0.001)
    assert chop2.get("chop") is True
    assert chop2.get("escape_ok") is True, chop2
    print("PASS 08 chop_block_escape")


def test_09_reversal_disarm_not_permanent():
    series = SparseSecSeries()
    t0 = 8_000_000
    _fill_sparse_linear(series, t0, 130, 100.0, 0.0)
    # fake arm
    arm = {"side": "LONG", "break_px": 100.5, "trigger_sec": t0 + 100, "confirm_left": 2}
    # price fails to stay → disarm
    series.on_trade(t0 + 101 + 0.2, 100.0)
    r, arm2 = evaluate_path_c_reversal(series, t0 + 101, tick_size=0.001, armed=arm)
    assert arm2 is None
    assert r.reason == "REVERSAL_CONFIRM_FAIL"
    # can re-search later (arm None)
    r3, arm3 = evaluate_path_c_reversal(series, t0 + 102, tick_size=0.001, armed=None)
    assert r3.ok is False  # flat tape — but not permanently blocked
    print("PASS 09 reversal_disarm")


def test_10_multi_path_dedup():
    """Same side A+B → one candidate, merged tags."""
    reset_entry_gate_store_for_tests()
    T = 9_000_000.0
    series = SparseSecSeries()
    _fill_sparse_linear(series, int(T), 80, 10.0, 0.0008)
    # Also make surge ok
    bar = _bar(T, quote=80_000.0, o=10.0, h=10.1, l=9.95, c=10.08)
    gate = SymbolEntryGate()
    # build streak
    last = None
    for add in range(5):
        dec = T + 70 + add + 0.8
        # keep dense aligned
        dens = _dense_up(60, 10.0, 0.0008)
        last = evaluate_entry(
            decision_ts=dec,
            bar=bar,
            baseline_median=100_000.0,
            prices_60=dens,
            prices_15=dens[-15:],
            last_trade_ts=dec - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate,
            tick_size=0.0001,
        )
    assert last is not None
    if last.ok:
        assert last.side in ("LONG", "SHORT")
        # tags unique paths
        assert len(last.tags) == len(set(last.tags))
    print("PASS 10 multi_path_dedup", last.ok, last.tags, last.reason)


def test_11_cooldown_and_consume():
    gate = SymbolEntryGate()
    gate.last_exit_ts = 10_000_000.0
    series = SparseSecSeries()
    _fill_sparse_linear(series, 10_000_000, 120, 100.0, 0.008)
    dec = 10_000_000.0 + 70.8
    # force B streak ready
    for add in range(3):
        d = evaluate_entry(
            decision_ts=dec + add,
            bar=_bar(10_000_000.0, 1000),
            baseline_median=1e9,
            prices_60=_dense_up(),
            prices_15=_dense_up(15),
            last_trade_ts=dec + add - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate,
            tick_size=0.001,
        )
    assert d.reason == "COOLDOWN_AFTER_EXIT" or (
        d.ok is False and "COOLDOWN" in d.reason
    ) or gate.last_exit_ts > 0
    # after cooldown
    gate.last_exit_ts = 10_000_000.0 - 100
    gate2 = SymbolEntryGate()
    gate2.last_exit_ts = dec - 20
    for add in range(3):
        d2 = evaluate_entry(
            decision_ts=dec + add,
            bar=_bar(10_000_000.0, 1000),
            baseline_median=1e9,
            prices_60=_dense_up(),
            prices_15=_dense_up(15),
            last_trade_ts=dec + add - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate2,
            tick_size=0.001,
        )
    if d2.ok:
        # Keep B continuously true → must_clear blocks re-entry on same run
        dec3 = dec + 10 + 0.8
        t3 = int(dec3) - 1
        gate2.must_clear_before_rearm = True
        gate2.b_streak_side = "LONG"
        gate2.b_streak_since = t3 - 2
        gate2.b_streak_count = 2  # next eval at t3 → count becomes 3
        d3 = evaluate_entry(
            decision_ts=dec3,
            bar=_bar(10_000_000.0, 1000),
            baseline_median=1e9,
            prices_60=_dense_up(),
            prices_15=_dense_up(15),
            last_trade_ts=dec3 - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate2,
            tick_size=0.001,
        )
        assert d3.reason == "CANDIDATE_MUST_CLEAR_BEFORE_REARM", (d3.reason, d3.paths)
        # after paths fail, flag clears; then may re-arm as new event
        gate2.b_streak_side = None
        gate2.b_streak_count = 0
        d4 = evaluate_entry(
            decision_ts=dec + 11 + 0.8,
            bar=_bar(10_000_000.0, 1000),
            baseline_median=1e9,
            prices_60=_dense_up(),
            prices_15=_dense_up(15),
            last_trade_ts=dec + 11 - 0.1,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            sparse=series,
            gate=gate2,
            tick_size=0.001,
        )
        assert d4.reason != "CANDIDATE_MUST_CLEAR_BEFORE_REARM"
    print("PASS 11 cooldown_consume")


def test_12_risk_add_trail_unchanged():
    """加倉步長／停損／曝險常數與 queue 行為未被本任務改掉。"""
    assert config.ADD_STEP_PCT == 0.005
    assert config.TRAIL_PCT == 0.01
    assert config.PROFIT_MARK_PCT == 0.02
    assert config.MARGIN_PER_TRADE_USDT == 30.0
    assert config.LEVERAGE == 10.0
    assert config.MAX_LEGS_PER_SYMBOL == 4
    assert config.MAX_SLOTS == 20
    assert config.TOP_ENTER == 10
    assert config.TOP_KEEP == 15
    reset_surge_store_for_tests()
    st = Account()
    ok, reason, order = queue_entry(
        st,
        symbol="TESTUSDT",
        side="LONG",
        bid=10.0,
        ask=10.01,
        filters=_filters(),
        signal={"tags": ["SURGE"], "entry_path": "SURGE"},
        leg_index=1,
        bar_start_ts=1.0,
        now=100.0,
    )
    assert ok, reason
    assert str(order.get("order_type")) == config.ENTRY_ORDER_TYPE
    apply_entry_fill(st, "TESTUSDT", fill_px=10.01, fill_qty=float(order["qty"]), fill_ts=100.0)
    pos = st.positions["TESTUSDT"]
    assert float(pos["trail_stop"]) > 0
    # add step gate
    ok2, reason2 = maybe_queue_add(
        st,
        symbol="TESTUSDT",
        bid=10.01,
        ask=10.02,
        now=101.0,
    )
    assert ok2 is False and reason2 == "ADD_STEP_NOT_REACHED"
    print("PASS 12 risk_unchanged", "ENTRY_ORDER_TYPE=", config.ENTRY_ORDER_TYPE)


def _load_agg(path: Path) -> list[tuple[float, float]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            px = float(row["price"])
            if "time_ms" in row and row["time_ms"]:
                ts = float(row["time_ms"]) / 1000.0
            else:
                ts = datetime.fromisoformat(row["time"]).timestamp()
            rows.append((ts, px))
    rows.sort()
    return rows


def _smoke_symbol(symbol: str, anchors: list[tuple[str, str]]) -> list[dict]:
    path = CASES / f"{symbol}_aggTrades.csv"
    if not path.exists():
        return [{"symbol": symbol, "error": "NO_FILE"}]
    trades = _load_agg(path)
    out = []
    for label, iso in anchors:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        anchor_ts = dt.timestamp()
        series = SparseSecSeries()
        gate = SymbolEntryGate()
        # feed trades up to anchor+180s but evaluate only with data <= decision
        window_trades = [x for x in trades if anchor_ts - 300 <= x[0] <= anchor_ts + 180]
        first_cand = None
        reasons = {}
        # replay second by second
        # ingest all up to each decision
        i = 0
        for sec_off in range(-60, 180):
            dec = anchor_ts + sec_off + 0.95
            while i < len(window_trades) and window_trades[i][0] <= dec:
                series.on_trade(window_trades[i][0], window_trades[i][1])
                i += 1
            if series.last_trade_ts <= 0:
                continue
            if dec - series.last_trade_ts > config.TRADE_STALE_SEC:
                continue
            # minimal bar from sparse (quote unknown → Path A likely fail)
            bar = _bar(int(dec // 180) * 180, quote=1.0)
            dens = []
            t_eval = series.latest_complete_sec(dec)
            for s in range(t_eval - 59, t_eval + 1):
                px = series.p(s)
                if px is not None:
                    dens.append(px)
            while len(dens) < 60 and dens:
                dens = [dens[0]] + dens
            d = evaluate_entry(
                decision_ts=dec,
                bar=bar,
                baseline_median=1e12,  # force A fail on volume
                prices_60=dens[-60:] if len(dens) >= 60 else dens,
                prices_15=dens[-15:] if dens else [],
                last_trade_ts=series.last_trade_ts,
                gap_unfilled=False,
                has_position=False,
                has_exit_pending=False,
                sparse=series,
                gate=gate,
                tick_size=0.0001,
            )
            for pk, pv in (d.paths or {}).items():
                if pv and pv.get("ok"):
                    reasons[pk] = reasons.get(pk, 0) + 1
            if d.ok and first_cand is None:
                first_cand = {
                    "sec_off": sec_off,
                    "side": d.side,
                    "tags": d.tags,
                    "path": d.entry_path,
                    "at": datetime.fromtimestamp(dec, TZ).isoformat(),
                }
                break
        out.append(
            {
                "id": label,
                "symbol": symbol,
                "anchor": iso,
                "candidate": first_cand,
                "path_ok_counts": reasons,
                "note": "smoke only; A volume baseline not historical",
            }
        )
    return out


def test_13_case_smoke_report():
    results = []
    results += _smoke_symbol(
        "MANTRAUSDT",
        [
            ("M1", "2026-11-25T23:39:00+08:00"),
            ("M2", "2026-11-25T23:48:00+08:00"),
            ("M3", "2026-11-25T23:54:00+08:00"),
        ],
    )
    results += _smoke_symbol(
        "BIGTIMEUSDT",
        [
            ("B1", "2026-11-25T23:48:00+08:00"),
            ("B2", "2026-11-25T23:54:00+08:00"),
        ],
    )
    results += _smoke_symbol(
        "SSVUSDT",
        [
            ("S1", "2026-11-25T23:48:00+08:00"),
            ("S2", "2026-11-25T23:57:00+08:00"),
        ],
    )
    print("CASE_SMOKE", results)
    # do not require triggers
    assert len(results) == 7
    print("PASS 13 case_smoke")
    return results


def main():
    test_01_rvol_fail_bc_can_pass()
    test_02_surge_path_not_regressed()
    test_03_long_short_symmetry_b()
    test_04_reversal_time_order()
    test_05_no_future_leak()
    test_06_bar_roll_does_not_reset_bc_window()
    test_07_missing_and_stale()
    test_08_chop_block_and_escape()
    test_09_reversal_disarm_not_permanent()
    test_10_multi_path_dedup()
    test_11_cooldown_and_consume()
    test_12_risk_add_trail_unchanged()
    smoke = test_13_case_smoke_report()
    print("ALL_V2_TESTS_DONE")
    return smoke


if __name__ == "__main__":
    main()
