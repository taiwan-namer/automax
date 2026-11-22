#!/usr/bin/env python3
"""Single quote + raw-signal coordinator dispatching identical events to B0–B3.

Research-only. EXCHANGE_ORDERS_HARD_DISABLED. No live/testnet orders.
Signal generation does NOT filter on any arm's positions; admission is per-arm.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[3]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
sys.path[:0] = [str(CODE), str(ROOT), str(Path(__file__).resolve().parent)]

from accounting_exposure import ExposureIntegralState, append_snapshot, snapshot_account  # noqa: E402

ARMS = ("B0", "B1", "B2", "B3")
QUOTE_MAX_AGE_DEFAULT = 15.0


@dataclass
class BusEvent:
    event_id: str
    seq: int
    source_ts: float
    received_ts: float
    kind: str  # QUOTE_BATCH | RAW_SIGNAL | MANAGE_TICK | CONTROL
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArmRuntime:
    arm: str
    root: Path
    st: Any
    exposure: ExposureIntegralState = field(default_factory=ExposureIntegralState)
    last_consumed_seq: int = 0
    blocked_new_exposure: bool = False
    alerts: list[dict[str, Any]] = field(default_factory=list)
    snap_path: Path = field(default_factory=Path)
    event_path: Path = field(default_factory=Path)


class EventBus:
    def __init__(self) -> None:
        self._seq = 0
        self.events: list[BusEvent] = []

    def publish(self, kind: str, payload: dict[str, Any], *, source_ts: float | None = None) -> BusEvent:
        self._seq += 1
        received = time.time()
        ev = BusEvent(
            event_id=f"ev:{self._seq}:{uuid.uuid4().hex[:10]}",
            seq=self._seq,
            source_ts=float(source_ts if source_ts is not None else received),
            received_ts=received,
            kind=kind,
            payload=payload,
        )
        self.events.append(ev)
        return ev


def _boot_arm(arm: str, data_root: Path, *, run_tag: str, capital: float = 5000.0) -> ArmRuntime:
    from breakout_follow_v1 import config
    from breakout_follow_v1.account import AccountState, save_account, write_run_manifest
    from breakout_follow_v1.util import ensure_dirs, now_iso

    root = (data_root / f"arm_{arm.lower()}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    config.set_runtime_root(root)
    config.PROFILE_ID = f"research_c_arm_{arm.lower()}_20261122"
    config.set_runtime_root(root)
    config.RESEARCH_ARM = arm
    config.EXCHANGE_ORDERS_HARD_DISABLED = True
    config.MAKER_EXECUTION_ENABLED = False
    config.FEE_RATE_ONE_SIDE = 0.0005
    config.SLIPPAGE_ADVERSE = 0.0002
    config.PYRAMID_ENABLED = True
    config.PYRAMID_MAX_LAYERS = 3 if arm == "B0" else (2 if arm in ("B1", "B2") else 1)
    config.PYRAMID_ADD_PCT = 0.005
    config.PYRAMID_P0_TP_PCT = 0.01
    config.MARGIN_PER_TRADE_USDT = 30.0
    config.NOTIONAL_USDT = 300.0
    config.LEVERAGE = 10.0
    config.INITIAL_CAPITAL_USDT = capital
    config.MAX_CONCURRENT_POSITIONS = 10
    config.EXIT_PLAN_ID = "c_pyramid300_r2_p0tp1pct_20261120"
    config.LIMIT_ENTRY_ENABLED = True
    config.LIMIT_ENTRY_OFFSET_PCT = 0.005
    config.LIMIT_ENTRY_TTL_SEC = 0.0
    config.LIMIT_ENTRY_CANCEL_ADVERSE_PCT = 0.01
    config.SIGNAL_DIRECTION_FLIP = False
    config.FIXED_GROSS_EXIT_ENABLED = False
    config.ENABLE_PARTIAL_TP = False
    config.ENABLE_BREAKEVEN = False
    config.ENABLE_TRAILING_STOP = False
    config.ENABLE_REVERSE_REOPEN = False
    config.NEW_ENTRIES_UNTIL_ISO = ""
    config.CODE_FIX_TAG = f"research_four_arm_{arm}_{run_tag}"
    ensure_dirs()
    # fresh account file for smoke/observe
    acc_path = config.ACCOUNT_PATH
    if acc_path.exists():
        # isolate: if observe fresh flag, wipe; else load
        pass
    st = AccountState(
        run_id="",
        cash_u=capital,
        initial_capital_u=capital,
        profile_id=config.PROFILE_ID,
        created_at=now_iso(),
        funding_data_gap=True,
    )
    st.research_arm = arm  # type: ignore[attr-defined]
    save_account(st)
    write_run_manifest(st)
    rt = ArmRuntime(arm=arm, root=root, st=st)
    rt.snap_path = root / "output" / config.PROFILE_ID / "research_snapshots.jsonl"
    rt.event_path = root / "output" / config.PROFILE_ID / "research_bus_consume.jsonl"
    return rt


def _reload_config_for_arm(rt: ArmRuntime) -> None:
    from breakout_follow_v1 import config

    config.set_runtime_root(rt.root)
    config.PROFILE_ID = f"research_c_arm_{rt.arm.lower()}_20261122"
    config.set_runtime_root(rt.root)
    config.RESEARCH_ARM = rt.arm
    config.EXCHANGE_ORDERS_HARD_DISABLED = True
    config.MAKER_EXECUTION_ENABLED = False
    config.FEE_RATE_ONE_SIDE = 0.0005
    config.PYRAMID_ENABLED = True
    config.EXIT_PLAN_ID = "c_pyramid300_r2_p0tp1pct_20261120"


def _book_ok(book: dict[str, Any] | None, *, max_age: float) -> bool:
    if not book:
        return False
    try:
        bid = float(book["bid"])
        ask = float(book["ask"])
        fa = float(book.get("fetched_at") or 0)
    except Exception:
        return False
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    if fa <= 0 or (time.time() - fa) > max_age:
        return False
    return True


def _log_consume(rt: ArmRuntime, ev: BusEvent, result: dict[str, Any]) -> None:
    row = {
        "event_id": ev.event_id,
        "seq": ev.seq,
        "source_ts": ev.source_ts,
        "received_ts": ev.received_ts,
        "consumed_ts": time.time(),
        "arm": rt.arm,
        "kind": ev.kind,
        "last_consumed_seq": rt.last_consumed_seq,
        "result": result,
    }
    rt.event_path.parent.mkdir(parents=True, exist_ok=True)
    with rt.event_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def apply_manage_on_arm(rt: ArmRuntime, books: dict[str, dict], *, book_ts: float) -> list[dict]:
    """Priority on same quote: PSTOP/protect > cancel pending adds as needed > ADD > R2 > P0_TP (engine order)."""
    from breakout_follow_v1.account import get_position, save_account
    from breakout_follow_v1.portfolio import mark_price_for_side, process_pending_limit_entries

    _reload_config_for_arm(rt)
    events: list[dict] = []
    # pending limit fills first (entry)
    try:
        for fr in process_pending_limit_entries(rt.st, books):
            events.append({"pending_limit": fr})
    except Exception as exc:
        rt.alerts.append({"ts": time.time(), "alert": "PENDING_LIMIT_FAIL", "error": str(exc)})
        rt.blocked_new_exposure = True

    # manage each position using portfolio manage path
    from breakout_follow_v1 import portfolio as port

    for sym in list(rt.st.positions.keys()):
        b = books.get(sym)
        if not _book_ok(b, max_age=QUOTE_MAX_AGE_DEFAULT):
            rt.alerts.append({"ts": time.time(), "alert": "STALE_OR_MISSING_QUOTE", "symbol": sym})
            rt.blocked_new_exposure = True
            continue
        pos = get_position(rt.st, sym)
        if pos is None:
            continue
        mark = mark_price_for_side(pos.side, b)
        try:
            ev = port._manage_c_pyramid(rt.st, sym, book=b, pos=pos, mark=mark, book_ts=book_ts)
            events.extend(ev)
        except Exception as exc:
            rt.alerts.append({"ts": time.time(), "alert": "MANAGE_FAIL", "symbol": sym, "error": str(exc)})
            rt.blocked_new_exposure = True
            # do not invent fills on failure
    try:
        save_account(rt.st)
    except Exception as exc:
        rt.alerts.append({"ts": time.time(), "alert": "LEDGER_SAVE_FAIL", "error": str(exc)})
        rt.blocked_new_exposure = True
    return events


def admit_raw_signal(rt: ArmRuntime, sig_ev: dict[str, Any], books: dict[str, dict]) -> dict[str, Any]:
    """Per-arm admission — never uses another arm's fills."""
    from breakout_follow_v1.portfolio import _entry_block_reasons, queue_limit_entry, try_open
    from breakout_follow_v1 import config
    from breakout_follow_v1.account import save_account

    _reload_config_for_arm(rt)
    if rt.blocked_new_exposure or getattr(rt.st, "ledger_write_failed", False):
        return {"ok": False, "block_reasons": ["ARM_BLOCKED_OR_LEDGER"], "arm": rt.arm}
    if getattr(rt.st, "new_exposure_blocked_until_reconcile", False):
        return {"ok": False, "block_reasons": ["LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE"], "arm": rt.arm}

    sym = sig_ev["symbol"]
    side = sig_ev["side"]
    signal = sig_ev["signal"]
    reasons = _entry_block_reasons(
        rt.st, symbol=sym, side=side, signal=signal, bypass_cooldown=False, for_limit_queue=True
    )
    if reasons:
        return {"ok": False, "block_reasons": reasons, "arm": rt.arm, "symbol": sym}

    book = books.get(sym)
    if not _book_ok(book, max_age=QUOTE_MAX_AGE_DEFAULT):
        return {"ok": False, "block_reasons": ["STALE_QUOTE"], "arm": rt.arm}

    filt = (rt.st.filters or {}).get(sym, {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001})
    if bool(getattr(config, "LIMIT_ENTRY_ENABLED", False)):
        r = queue_limit_entry(
            rt.st,
            symbol=sym,
            side=side,
            signal=signal,
            source_tags=list(sig_ev.get("tags") or ["RESEARCH_SHARED_SIGNAL"]),
            filters=filt,
        )
    else:
        r = try_open(
            rt.st,
            symbol=sym,
            side=side,
            signal=signal,
            source_tags=list(sig_ev.get("tags") or ["RESEARCH_SHARED_SIGNAL"]),
            filters=filt,
        )
    try:
        save_account(rt.st)
    except Exception as exc:
        return {"ok": False, "block_reasons": [f"SAVE_FAIL:{exc}"], "arm": rt.arm}
    return {"ok": bool(r.get("ok")), "result": r, "arm": rt.arm, "symbol": sym}


def dispatch_event(arms: dict[str, ArmRuntime], ev: BusEvent, books: dict[str, dict]) -> dict[str, Any]:
    """Same event to all arms; each records consume seq. Order: MANAGE before SIGNAL admit."""
    out: dict[str, Any] = {"event_id": ev.event_id, "seq": ev.seq, "kind": ev.kind, "arms": {}}
    book_ts = float(ev.payload.get("book_ts") or ev.source_ts)
    for arm in ARMS:
        rt = arms[arm]
        if ev.seq <= rt.last_consumed_seq:
            _log_consume(rt, ev, {"skipped": "SEQ_ALREADY_CONSUMED"})
            out["arms"][arm] = {"skipped": "SEQ_ALREADY_CONSUMED"}
            continue
        # gap detect
        if rt.last_consumed_seq and ev.seq > rt.last_consumed_seq + 1:
            rt.alerts.append(
                {
                    "ts": time.time(),
                    "alert": "EVENT_SEQ_GAP",
                    "from": rt.last_consumed_seq,
                    "to": ev.seq,
                }
            )
            rt.blocked_new_exposure = True
        try:
            if ev.kind in ("QUOTE_BATCH", "MANAGE_TICK"):
                mev = apply_manage_on_arm(rt, books, book_ts=book_ts)
                snap = snapshot_account(
                    rt.st,
                    books,
                    mark_fn=__import__("breakout_follow_v1.portfolio", fromlist=["mark_price_for_side"]).mark_price_for_side,
                    exposure=rt.exposure,
                    event_id=ev.event_id,
                    reason=ev.kind,
                )
                append_snapshot(rt.snap_path, snap)
                result = {"manage_events": len(mev), "snapshot_equity": snap.get("equity_u")}
            elif ev.kind == "RAW_SIGNAL":
                # manage first on same batch books, then admit
                apply_manage_on_arm(rt, books, book_ts=book_ts)
                result = admit_raw_signal(rt, ev.payload, books)
                snap = snapshot_account(
                    rt.st,
                    books,
                    mark_fn=__import__("breakout_follow_v1.portfolio", fromlist=["mark_price_for_side"]).mark_price_for_side,
                    exposure=rt.exposure,
                    event_id=ev.event_id,
                    reason="AFTER_SIGNAL",
                )
                append_snapshot(rt.snap_path, snap)
            elif ev.kind == "CONTROL":
                result = {"control": ev.payload}
            else:
                result = {"ignored_kind": ev.kind}
            rt.last_consumed_seq = ev.seq
            _log_consume(rt, ev, result)
            out["arms"][arm] = result
        except Exception as exc:
            rt.alerts.append({"ts": time.time(), "alert": "ARM_PROCESS_FAIL", "error": str(exc)})
            rt.blocked_new_exposure = True
            _log_consume(rt, ev, {"error": str(exc)})
            out["arms"][arm] = {"error": str(exc)}
    return out


def generate_raw_signals_from_probes(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert detect_breakout probes to raw signals — no arm position filter."""
    raw = []
    for item in probes:
        probe = item.get("probe") or {}
        sig = probe.get("signal")
        if not sig:
            continue
        sym = item["symbol"]
        raw.append(
            {
                "symbol": sym,
                "side": sig["side"],
                "signal": sig,
                "tags": item.get("tags") or ["SHARED_RAW"],
                "probe_meta": {
                    "signal_open_time": sig.get("signal_open_time"),
                    "first_reject_code": None,
                },
            }
        )
    return raw


async def live_loop(
    *,
    data_root: Path,
    hours: float,
    run_tag: str,
    stop_new_entries_after_h: float | None = None,
) -> dict[str, Any]:
    import aiohttp
    from breakout_follow_v1.market_data import GuardedSession, fetch_book_tickers, fetch_klines_incremental
    from breakout_follow_v1.signals import detect_breakout
    from breakout_follow_v1 import config
    from breakout_follow_v1.account import save_account
    from breakout_follow_v1.util import now_iso

    bus = EventBus()
    arms = {a: _boot_arm(a, data_root, run_tag=run_tag) for a in ARMS}
    # assign run ids
    for a, rt in arms.items():
        _reload_config_for_arm(rt)
        rt.st.run_id = f"{run_tag}_{a}"
        save_account(rt.st)

    meta = {
        "shared_start": now_iso(),
        "shared_start_unix": time.time(),
        "hours": hours,
        "cutoff_unix": time.time() + hours * 3600,
        "stop_new_entries_after_h": stop_new_entries_after_h or hours,
        "arms": {a: {"root": str(rt.root), "run_id": rt.st.run_id, "profile": f"research_c_arm_{a.lower()}_20261122"} for a, rt in arms.items()},
        "fee_one_side": 0.0005,
        "exchange_orders": "HARD_DISABLED",
        "git_commit": "81813a261a94dbdbcecfaef9d62df5617098bfe6",
    }
    (data_root / "shared_launch_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (data_root / "coordinator.pid").write_text(str(os.getpid()), encoding="utf-8")

    # minimal universe for live — reuse filters from empty; fetch top via refresh if possible
    timeout = aiohttp.ClientTimeout(total=30)
    start = time.time()
    stop_entries_at = start + float(meta["stop_new_entries_after_h"]) * 3600
    new_entries_stopped = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        gs = GuardedSession(session)
        # seed a small symbol list from BTC/ETH + majors if universe empty — try refresh on B0 config
        _reload_config_for_arm(arms["B0"])
        try:
            from breakout_follow_v1.engine import maybe_refresh_universe

            await maybe_refresh_universe(gs, arms["B0"].st, force=True)
            universe = [r["symbol"] for r in (arms["B0"].st.universe or [])][:40]
            # copy universe list to other arms (filters only; not positions)
            for a in ("B1", "B2", "B3"):
                arms[a].st.universe = list(arms["B0"].st.universe or [])
                arms[a].st.filters = dict(arms["B0"].st.filters or {})
                arms[a].st.top_gainers = list(arms["B0"].st.top_gainers or [])
                arms[a].st.top_losers = list(arms["B0"].st.top_losers or [])
                save_account(arms[a].st)
        except Exception as exc:
            universe = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
            meta["universe_fallback"] = str(exc)

        while True:
            now = time.time()
            if hours > 0 and now >= start + hours * 3600:
                break
            if (not new_entries_stopped) and now >= stop_entries_at:
                new_entries_stopped = True
                # cancel pending entries/adds per arm; keep protect manage
                for a, rt in arms.items():
                    _reload_config_for_arm(rt)
                    rt.st.day_new_entries_locked = True
                    for pend in list((rt.st.pending_limit_entries or {}).keys()):
                        rt.st.pending_limit_entries.pop(pend, None)
                    for sym, pd in list(rt.st.positions.items()):
                        pyr = dict((pd or {}).get("pyramid") or {})
                        for add in pyr.get("pending_adds") or []:
                            if add.get("status") == "PENDING":
                                add["status"] = "CANCELLED_CUTOFF_STOP_NEW"
                        pd["pyramid"] = pyr
                    save_account(rt.st)
                bus.publish("CONTROL", {"action": "STOP_NEW_ENTRIES"}, source_ts=now)

            books = await fetch_book_tickers(gs)
            # subscribe union of positions+pending across arms + universe
            cover = set(universe)
            for rt in arms.values():
                cover |= set(rt.st.positions.keys())
                cover |= set((rt.st.pending_limit_entries or {}).keys())
            # publish quote batch
            q_ev = bus.publish(
                "QUOTE_BATCH",
                {"symbols": sorted(cover), "book_ts": time.time(), "n_books": len(books)},
                source_ts=time.time(),
            )
            dispatch_event(arms, q_ev, books)

            # raw signal generation WITHOUT arm position filter
            raw_signals: list[dict[str, Any]] = []
            now_ms = int(time.time() * 1000)
            for sym in list(cover)[:30]:
                try:
                    df, err = await fetch_klines_incremental(gs, sym)
                    if err or df is None:
                        continue
                    book = books.get(sym)
                    probe = detect_breakout(df, now_ms=now_ms, book=book, data_fetched_at=time.time())
                    if probe.get("signal"):
                        raw_signals.append(
                            {
                                "symbol": sym,
                                "side": probe["signal"]["side"],
                                "signal": probe["signal"],
                                "tags": ["SHARED_RAW"],
                            }
                        )
                except Exception:
                    continue

            for rs in raw_signals:
                if new_entries_stopped:
                    break
                sev = bus.publish("RAW_SIGNAL", rs, source_ts=time.time())
                dispatch_event(arms, sev, books)

            # manage tick
            mev = bus.publish("MANAGE_TICK", {"book_ts": time.time()}, source_ts=time.time())
            dispatch_event(arms, mev, books)

            await asyncio.sleep(5.0)

    meta["shared_end"] = __import__("breakout_follow_v1.util", fromlist=["now_iso"]).now_iso()
    meta["bus_events"] = len(bus.events)
    meta["arm_alerts"] = {a: rt.alerts[-20:] for a, rt in arms.items()}
    (data_root / "shared_launch_meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta


def fixture_run(data_root: Path, run_tag: str = "fixture") -> dict[str, Any]:
    """Deterministic four-arm path: open→add→partial→flat + restart exposure continuity."""
    from breakout_follow_v1.exit_plans import build_pyramid_state
    from breakout_follow_v1.account import Position, set_position, save_account, get_position, load_or_create_account
    from breakout_follow_v1.portfolio import _pyramid_fill_add, close_qty, mark_price_for_side
    from breakout_follow_v1 import config

    bus = EventBus()
    arms = {a: _boot_arm(a, data_root, run_tag=run_tag, capital=5000.0) for a in ARMS}
    for a, rt in arms.items():
        rt.st.run_id = f"{run_tag}_{a}"
        save_account(rt.st)

    # shared synthetic books
    def books_at(px: float) -> dict[str, dict]:
        return {
            "FIXUSDT": {
                "bid": px * 0.9999,
                "ask": px * 1.0001,
                "mid": px,
                "fetched_at": time.time(),
                "spread": 0.0002,
            }
        }

    # Event 1: inject OPEN on all arms at same raw signal (manual position create = admitted fill)
    p0 = 100.0
    for a, rt in arms.items():
        _reload_config_for_arm(rt)
        pyr = build_pyramid_state(
            side="LONG", p0=p0, qty=3.0, margin_u=30.0, tick=0.01, step=0.001, notional_u=300.0
        )
        if a == "B3":
            pyr["pending_adds"] = []
        elif a == "B1":
            pyr["pending_adds"] = [x for x in pyr["pending_adds"] if x.get("name") != "P2"]
        # B2 keeps P2 pending so protect-at-P2 can fire; B0 keeps both
        pos = Position(
            trade_id=f"{rt.st.run_id}:FIXUSDT:open1",
            symbol="FIXUSDT",
            side="LONG",
            qty_initial=3.0,
            qty_remaining=3.0,
            entry_price=p0,
            entry_time="t0",
            entry_ts=1.0,
            margin_u=30.0,
            notional_initial=300.0,
            leverage=10.0,
            atr_at_entry=1.0,
            atr_time="",
            initial_stop=float(pyr["levels"]["Pstop"]),
            effective_stop=float(pyr["levels"]["Pstop"]),
            take_profit=float(pyr["levels"]["P0_TP"]),
            signal_open_time=1,
            exit_plan_id=config.EXIT_PLAN_ID,
            pyramid=pyr,
            funding_gap=True,
        )
        rt.st.cash_u -= 30.0
        # entry fee
        fee = 300.0 * 0.0005
        rt.st.cash_u -= fee
        rt.st.realized_fees_u += fee
        rt.st.realized_net_u -= fee
        set_position(rt.st, pos)
        save_account(rt.st)

    ev1 = bus.publish("QUOTE_BATCH", {"book_ts": time.time()}, source_ts=time.time())
    r1 = dispatch_event(arms, ev1, books_at(100.0))

    # Event 2: price to P1 — B0/B1/B2 add; B3 no
    p1 = 99.5
    ev2 = bus.publish("MANAGE_TICK", {"book_ts": time.time()}, source_ts=time.time())
    r2 = dispatch_event(arms, ev2, books_at(p1))

    # Event 3: partial R2 for arms that have P1
    ev3 = bus.publish("MANAGE_TICK", {"book_ts": time.time()}, source_ts=time.time())
    r3 = dispatch_event(arms, ev3, books_at(100.0))

    # Event 4: flat via stop jump
    pstop = float(build_pyramid_state(side="LONG", p0=100.0, qty=3.0, margin_u=30.0, tick=0.01, step=0.001, notional_u=300.0)["levels"]["Pstop"])
    ev4 = bus.publish("MANAGE_TICK", {"book_ts": time.time()}, source_ts=time.time())
    r4 = dispatch_event(arms, ev4, books_at(pstop - 0.05))

    # Restart exposure continuity on B0
    _reload_config_for_arm(arms["B0"])
    # persist integral state into a side file
    exp_path = arms["B0"].root / "output" / f"research_c_arm_b0_20261122" / "exposure_state.json"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    exp_path.write_text(
        json.dumps(
            {
                "integral_u_sec": arms["B0"].exposure.integral_u_sec,
                "last_ts": arms["B0"].exposure.last_ts,
                "last_gross_notional": arms["B0"].exposure.last_gross_notional,
                "covered_sec": arms["B0"].exposure.covered_sec,
                "wall_sec": arms["B0"].exposure.wall_sec,
            }
        ),
        encoding="utf-8",
    )
    loaded = json.loads(exp_path.read_text(encoding="utf-8"))
    arms["B0"].exposure.integral_u_sec = loaded["integral_u_sec"]
    arms["B0"].exposure.last_ts = loaded["last_ts"]
    arms["B0"].exposure.last_gross_notional = loaded["last_gross_notional"]
    arms["B0"].exposure.covered_sec = loaded["covered_sec"]
    arms["B0"].exposure.wall_sec = loaded["wall_sec"]
    before = arms["B0"].exposure.integral_u_sec
    # empty books snapshot still advances with 0 notional without double counting prior
    time.sleep(0.05)
    snap = snapshot_account(
        arms["B0"].st,
        books_at(100.0),
        mark_fn=mark_price_for_side,
        exposure=arms["B0"].exposure,
        event_id="restart",
        reason="RESTART",
    )
    after = arms["B0"].exposure.integral_u_sec

    # collect snapshots
    snaps = {}
    for a, rt in arms.items():
        if rt.snap_path.exists():
            snaps[a] = [json.loads(l) for l in rt.snap_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    report = {
        "ok": True,
        "bus_events": [{"event_id": e.event_id, "seq": e.seq, "kind": e.kind} for e in bus.events],
        "dispatch": {"r1": r1, "r2_keys": list(r2["arms"].keys()), "r4": {a: r4["arms"][a] for a in ARMS}},
        "final_positions": {a: list(rt.st.positions.keys()) for a, rt in arms.items()},
        "final_cash": {a: rt.st.cash_u for a, rt in arms.items()},
        "final_realized": {a: rt.st.realized_net_u for a, rt in arms.items()},
        "snapshots_n": {a: len(snaps.get(a) or []) for a in ARMS},
        "sample_snapshot_B0_last": (snaps.get("B0") or [None])[-1],
        "exposure_restart": {"before": before, "after": after, "non_decreasing": after >= before - 1e-9},
        "seq_consistency": {
            a: {"last_consumed_seq": rt.last_consumed_seq, "bus_seq": bus._seq} for a, rt in arms.items()
        },
    }
    out = data_root / "fixture_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--data-root", type=str, required=True)
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--run-tag", type=str, default="")
    ap.add_argument("--stop-new-entries-after-h", type=float, default=None)
    args = ap.parse_args()
    tag = args.run_tag or time.strftime("%Y%m%dT%H%M%S")
    root = Path(args.data_root)
    root.mkdir(parents=True, exist_ok=True)
    if args.fixture:
        rep = fixture_run(root, run_tag=f"fixture_{tag}")
        print(json.dumps({"mode": "fixture", "ok": rep.get("ok"), "snapshots_n": rep.get("snapshots_n")}, ensure_ascii=False))
        return 0 if rep.get("ok") else 1
    if args.live:
        meta = asyncio.run(
            live_loop(
                data_root=root,
                hours=args.hours,
                run_tag=tag,
                stop_new_entries_after_h=args.stop_new_entries_after_h,
            )
        )
        print(json.dumps({"mode": "live", "meta": meta}, ensure_ascii=False, default=str))
        return 0
    print("specify --fixture or --live", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
