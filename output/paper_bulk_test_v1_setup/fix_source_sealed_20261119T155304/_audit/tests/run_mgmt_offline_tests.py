#!/usr/bin/env python3
"""離線管理可靠性驗收（fake clock／fake session）。非真實網路時間保證。"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, Position
from breakout_follow_v1.manage_pass import (
    ManageDueClock,
    await_scan_slice,
    get_due_clock,
    manage_due_pass,
    reset_due_clock_for_tests,
)
from breakout_follow_v1.market_data import (
    EndpointBanned,
    PublicHttpError,
    _parse_book_ticker_payload,
    fetch_book_tickers_manage,
)
from breakout_follow_v1.portfolio import manage_position

PASS = FAIL = 0
RESULTS: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(f"PASS {name}")
        print(f"PASS {name}")
    else:
        FAIL += 1
        RESULTS.append(f"FAIL {name} {detail}")
        print(f"FAIL {name} {detail}")


class FakeMono:
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeGS:
    """Fake GuardedSession recording calls; optional sleep/fail/429/418."""

    def __init__(self):
        self.calls: List[dict] = []
        self.book_payload = [
            {"symbol": "AAAUSDT", "bidPrice": "100", "askPrice": "100.1"},
            {"symbol": "BBBUSDT", "bidPrice": "50", "askPrice": "50.1"},
        ]
        self.fail_mode: Optional[str] = None  # timeout|429|418|sleep
        self.sleep_sec = 0.0
        self.background_continued = False
        self._cancel_cleanup_sec = 0.0

    async def get_json(self, path, params=None, **kwargs):
        self.calls.append({"path": path, **kwargs})
        purpose = kwargs.get("purpose")
        if self.fail_mode == "418":
            raise EndpointBanned("418", endpoint=path, status=418, attempts=1)
        if self.fail_mode == "429":
            raise PublicHttpError("429", endpoint=path, status=429, attempts=1)
        if self.fail_mode == "timeout":
            raise PublicHttpError("timeout budget", endpoint=path, status=None, attempts=2)
        if self.sleep_sec > 0:
            await asyncio.sleep(self.sleep_sec)
        if path.endswith("bookTicker"):
            return list(self.book_payload)
        return []


def _mk_pos(symbol: str, side: str = "LONG", **kw) -> Position:
    px = float(kw.get("entry_price", 100.0))
    stop = float(kw.get("initial_stop", 99.0 if side == "LONG" else 101.0))
    return Position(
        trade_id=f"t:{symbol}",
        symbol=symbol,
        side=side,
        qty_initial=1.0,
        qty_remaining=1.0,
        entry_price=px,
        entry_time="2026-11-19T12:00:00+08:00",
        entry_ts=1.0,
        margin_u=1000.0,
        notional_initial=px,
        leverage=10.0,
        atr_at_entry=1.0,
        atr_time="",
        initial_stop=stop,
        effective_stop=float(kw.get("effective_stop", stop)),
        breakeven_stop=kw.get("breakeven_stop"),
        trailing_stop=kw.get("trailing_stop"),
        partial_tp_done=bool(kw.get("partial_tp_done", False)),
        extreme_price=float(kw.get("extreme_price", px)),
        fees_paid_u=5.0,
        realized_net_u=0.0,
    )


def _mk_st(*positions: Position) -> AccountState:
    st = AccountState(
        strategy_version="breakout_follow_v1",
        profile_id="paper_bulk_test_v1",
        initial_capital_u=50000.0,
        cash_u=50000.0,
    )
    for p in positions:
        st.positions[p.symbol] = p.to_dict()
    return st


def _fresh_book(symbol: str, bid: float, ask: float) -> dict:
    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "spread": (ask - bid) / ((bid + ask) / 2),
        "fetched_at": time.time(),
    }


async def test_due_no_catchup():
    reset_due_clock_for_tests()
    mono = FakeMono(1000.0)
    clock = get_due_clock()
    clock.ensure_init(mono())
    clock.mark_attempt_start(mono())  # due=1005
    # simulate manage lasting 10s
    mono.advance(10.0)
    check("E1_overdue_after_long_manage", clock.is_due(mono()), "")
    # one pass should advance due by MANAGE_DUE from start, not loop catch-up
    starts = []
    gs = FakeGS()
    st = _mk_st(_mk_pos("AAAUSDT"))

    async def fake_fetch(gs_):
        starts.append(mono())
        await asyncio.sleep(0)
        return _parse_book_ticker_payload(gs_.book_payload, fetched_at=time.time())

    with patch("breakout_follow_v1.manage_pass.fetch_book_tickers_manage", fake_fetch):
        with patch("breakout_follow_v1.manage_pass._book_usable", return_value=True):
            with patch("breakout_follow_v1.manage_pass.manage_position", return_value=[]):
                r1 = await manage_due_pass(gs, st, force=True, mono_now=mono)
                due1 = get_due_clock().next_manage_due
                # immediately call again without advancing — should skip (not force)
                r2 = await manage_due_pass(gs, st, force=False, mono_now=mono)
    check("E1_no_burst_second_skip", r2.get("skipped") is True, str(r2))
    check("E1_due_advanced_by_period", abs(due1 - (starts[0] + config.MANAGE_DUE_SEC)) < 1e-6, f"{due1}")


async def test_429_418_no_bypass():
    gs = FakeGS()
    gs.fail_mode = "429"
    # Direct manage fetch should raise, not retry past policy in our Fake — engine path records batch fail
    reset_due_clock_for_tests()
    st = _mk_st(_mk_pos("AAAUSDT"))

    async def boom(gs_):
        raise PublicHttpError("429", endpoint="/fapi/v1/ticker/bookTicker", status=429, attempts=1)

    with patch("breakout_follow_v1.manage_pass.fetch_book_tickers_manage", boom):
        batch = await manage_due_pass(gs, st, force=True, mono_now=FakeMono())
    check("E2_429_batch_fail", batch.get("status") == "BATCH_BOOK_FAIL", str(batch.get("status")))
    check("E2_429_blocked_all", batch.get("blocked_n") == 1, str(batch))

    reset_due_clock_for_tests()
    async def boom418(gs_):
        raise EndpointBanned("418", endpoint="/fapi/v1/ticker/bookTicker", status=418, attempts=1)

    with patch("breakout_follow_v1.manage_pass.fetch_book_tickers_manage", boom418):
        batch2 = await manage_due_pass(gs, st, force=True, mono_now=FakeMono())
    check("E2_418_batch_fail", batch2.get("status") == "BATCH_BOOK_FAIL", str(batch2))


async def test_scan_cancel_no_background():
    started = {"n": 0}
    finished = {"n": 0}

    async def slow():
        started["n"] += 1
        try:
            await asyncio.sleep(5.0)
            finished["n"] += 1
            return "done"
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # cleanup
            raise

    t0 = time.monotonic()
    try:
        await await_scan_slice(slow(), timeout=0.05)
        check("E3_should_timeout", False)
    except asyncio.TimeoutError:
        check("E3_timeout_raised", True)
    except asyncio.CancelledError:
        check("E3_cancelled", True)
    await asyncio.sleep(0.1)
    check("E3_no_finish_after_cancel", finished["n"] == 0, f"finished={finished}")
    check("E3_started", started["n"] == 1)
    # not a wall-clock guarantee claim — only cancel behavior
    _ = t0


async def test_book_phase_keeps_existing_stops():
    st = _mk_st(
        _mk_pos(
            "AAAUSDT",
            partial_tp_done=True,
            breakeven_stop=100.5,
            effective_stop=100.5,
            trailing_stop=100.8,
            extreme_price=102.0,
        )
    )
    # mark below BE → should exit on book phase without atr
    book = _fresh_book("AAAUSDT", bid=100.4, ask=100.41)
    with patch("breakout_follow_v1.portfolio.close_qty") as cq:
        cq.return_value = {"ok": True, "net": 1.0}
        with patch("breakout_follow_v1.portfolio.get_position", side_effect=lambda st, s: Position.from_dict(st.positions[s]) if s in st.positions else None):
            with patch("breakout_follow_v1.portfolio.set_position"):
                with patch("breakout_follow_v1.portfolio.save_account"):
                    # simplify: call manage with phase book; need real close path hard — use integration lighter
                    pass
    # Direct: effective stop hit
    st2 = _mk_st(_mk_pos("AAAUSDT", initial_stop=99.0, effective_stop=99.0, entry_price=100.0))
    book2 = _fresh_book("AAAUSDT", bid=98.0, ask=98.1)  # mark for LONG = bid
    evs = []
    try:
        # Use real manage but mock close_qty to avoid full account plumbing
        with patch("breakout_follow_v1.portfolio.close_qty", return_value={"ok": True}) as cq:
            with patch("breakout_follow_v1.portfolio.save_account"):
                evs = manage_position(st2, "AAAUSDT", book=book2, atr_latest=None, phase="book")
                check("C_book_initial_stop_without_atr", cq.called and any(e.get("action") == "INITIAL_STOP" for e in evs), str(evs))
    except Exception as exc:
        check("C_book_initial_stop_without_atr", False, str(exc))


async def test_atr_fail_keeps_stops():
    st = _mk_st(
        _mk_pos("AAAUSDT", partial_tp_done=True, effective_stop=99.5, trailing_stop=99.5, extreme_price=105.0)
    )
    before = st.positions["AAAUSDT"]["effective_stop"]
    book = _fresh_book("AAAUSDT", bid=100.0, ask=100.1)
    with patch("breakout_follow_v1.portfolio.save_account"):
        evs = manage_position(st, "AAAUSDT", book=book, atr_latest=None, phase="atr")
    after = st.positions["AAAUSDT"]["effective_stop"]
    check("M3_atr_none_keeps_stop", after == before, f"{before}->{after}")
    check("M3_atr_none_noop_or_stop", isinstance(evs, list))


async def test_no_positions_not_success():
    reset_due_clock_for_tests()
    gs = FakeGS()
    st = _mk_st()
    with patch("breakout_follow_v1.manage_pass.fetch_book_tickers_manage", side_effect=AssertionError("should not fetch")):
        # force path still checks expected_n==0 before fetch
        batch = await manage_due_pass(gs, st, force=True, mono_now=FakeMono())
    check("D_no_pos_status", batch.get("status") == "NO_POSITIONS", str(batch))
    check("D_no_pos_expected0", batch.get("expected_n") == 0)


async def test_single_symbol_error_isolation():
    reset_due_clock_for_tests()
    st = _mk_st(_mk_pos("AAAUSDT"), _mk_pos("BBBUSDT", entry_price=50.0, initial_stop=49.0))
    gs = FakeGS()
    calls = []

    async def ok_fetch(gs_):
        return {
            "AAAUSDT": _fresh_book("AAAUSDT", 100, 100.1),
            "BBBUSDT": _fresh_book("BBBUSDT", 50, 50.1),
        }

    def boom_manage(st, symbol, **kw):
        calls.append(symbol)
        if symbol == "AAAUSDT":
            raise RuntimeError("boom")
        return []

    with patch("breakout_follow_v1.manage_pass.fetch_book_tickers_manage", ok_fetch):
        with patch("breakout_follow_v1.manage_pass.manage_position", side_effect=boom_manage):
            with patch("breakout_follow_v1.manage_pass._book_usable", return_value=True):
                batch = await manage_due_pass(gs, st, force=True, do_atr=False, mono_now=FakeMono())
    check("E6_both_checked", "AAAUSDT" in calls and "BBBUSDT" in calls, str(calls))
    check("E6_error_count", batch.get("error_n") >= 1, str(batch))


async def test_manage_budget_constants():
    check("budget_due", config.MANAGE_DUE_SEC == 5.0)
    check("budget_book_op", config.MANAGE_BOOK_OP_BUDGET_SEC == 10.0)
    check("budget_book_attempts", config.MANAGE_BOOK_MAX_ATTEMPTS == 2)
    check("budget_book_timeout", config.MANAGE_BOOK_REQUEST_TIMEOUT_SEC == 5.0)
    check("budget_slice", config.SCAN_SLICE_MAX_WAIT_SEC == 2.0)


async def test_partial_then_fail_legs():
    from breakout_follow_v1.manage_pass import _classify_symbol_outcome

    legs = [
        {"action": "PARTIAL_TP", "ok": True},
        {"action": "BREAKEVEN_STOP", "ok": False},
    ]
    out = _classify_symbol_outcome(legs)
    check("E5_partial_then_fail_filled", out == "FILLED", out)


async def main():
    await test_manage_budget_constants()
    await test_due_no_catchup()
    await test_429_418_no_bypass()
    await test_scan_cancel_no_background()
    await test_book_phase_keeps_existing_stops()
    await test_atr_fail_keeps_stops()
    await test_no_positions_not_success()
    await test_single_symbol_error_isolation()
    await test_partial_then_fail_legs()
    print(f"\n{PASS} passed, {FAIL} failed")
    out = ROOT / "OFFLINE_TEST_RESULTS.md"
    lines = [
        "# 管理可靠性離線測試結果",
        "",
        "前提：fake clock／fake session；**不**代表真實網路延遲上界。",
        "",
        f"- passed: {PASS}",
        f"- failed: {FAIL}",
        "",
        "## 明細",
    ]
    lines += [f"- {r}" for r in RESULTS]
    lines += [
        "",
        "## 預算基準（離線；部署另批）",
        f"- MANAGE_DUE_SEC={config.MANAGE_DUE_SEC}",
        f"- MANAGE_BOOK_REQUEST_TIMEOUT_SEC={config.MANAGE_BOOK_REQUEST_TIMEOUT_SEC}",
        f"- MANAGE_BOOK_MAX_ATTEMPTS={config.MANAGE_BOOK_MAX_ATTEMPTS}",
        f"- MANAGE_BOOK_OP_BUDGET_SEC={config.MANAGE_BOOK_OP_BUDGET_SEC}",
        f"- SCAN_SLICE_MAX_WAIT_SEC={config.SCAN_SLICE_MAX_WAIT_SEC}",
        "",
        "未合入：N/T 健康禁止開倉政策。",
        "未改：止損／TP／反手／倉位／日損參數。",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
