"""C: all filled lots exit together at P0's fixed T2 price (no R2 rebound exits)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# sibling pack
import sys
from pathlib import Path

PREV = Path(__file__).resolve().parents[2] / "kline_path_and_pyramid300_20261120"
sys.path.insert(0, str(PREV))

from common.config import CUTOFF, FEE, PY_ADD_PCT, PY_NOTIONAL, SLIP  # noqa: E402
from common.data import Bar, Trade, exit_px_slip, fee_u, gross, qty_for_notional, round_tick  # noqa: E402
from study_b.pyramid import levels_from_p0  # old pack  # noqa: E402


@dataclass
class Lot:
    name: str
    entry: float
    qty: float
    open_fee: float
    open_ts: datetime
    closed: bool = False
    exit_px: Optional[float] = None
    exit_ts: Optional[datetime] = None
    exit_reason: Optional[str] = None
    gross_u: float = 0.0
    close_fee: float = 0.0
    net_u: float = 0.0


def _close(lot: Lot, side: str, barrier: float, ts: datetime, reason: str, gap: Optional[float] = None) -> None:
    px = exit_px_slip(side, barrier, SLIP, gap_open=gap)
    g = gross(side, lot.entry, px, lot.qty)
    cf = fee_u(px, lot.qty, FEE)
    lot.closed = True
    lot.exit_px = px
    lot.exit_ts = ts
    lot.exit_reason = reason
    lot.gross_u = g
    lot.close_fee = cf
    lot.net_u = -lot.open_fee + (g - cf)


def _mtm(lot: Lot, side: str, mark: float) -> float:
    px = exit_px_slip(side, mark, SLIP)
    g = gross(side, lot.entry, px, lot.qty)
    return -lot.open_fee + (g - fee_u(px, lot.qty, FEE))


def _touch_add(side: str, bar: Bar, px: float) -> bool:
    return bar.l <= px if side == "LONG" else bar.h >= px


def _touch_up(side: str, bar: Bar, px: float) -> bool:
    return bar.h >= px if side == "LONG" else bar.l <= px


def _touch_stop(side: str, bar: Bar, px: float) -> bool:
    return _touch_add(side, bar, px)


@dataclass
class AllT2Result:
    trade_id: str
    mode: str
    status: str
    realized_net: float
    open_mtm_net: float
    total_mark_net: float
    fees: float
    gross_realized: float
    n_lots_filled: int
    n_lots_closed: int
    stop_hit: bool
    tp_hit: bool
    p0_net: float
    add_net: float
    hold_min: Optional[float]
    max_notional_u: float
    max_margin_u: float
    exposure_notional_min: float  # Σ notional * hold_min per lot (approx)
    min_running_net: float
    dd_from_peak: float
    ambiguous: bool
    amb_note: str
    lots: List[Dict[str, Any]] = field(default_factory=list)
    bound_lo: Optional[float] = None
    bound_hi: Optional[float] = None


def simulate_allt2(
    trade: Trade,
    bars: List[Bar],
    *,
    notional_per: float = PY_NOTIONAL,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
) -> AllT2Result:
    """Adds at -0.5% layers; NO rebound exits; all lots exit at P0 T2 price or Pstop."""
    side = trade.side
    p0 = trade.entry
    tick = trade.tick
    step = trade.step
    lv = levels_from_p0(side, p0, tick)
    orig_tp = trade.open_row.get("fixed_gross_tp_price_raw")
    if orig_tp is not None:
        tp = float(orig_tp)
    else:
        d = 50.0 / trade.qty if trade.qty else 0.0
        tp = round_tick(p0 + d if side == "LONG" else p0 - d, tick)

    q0 = qty_for_notional(notional_per, p0, step)
    lots = [Lot("P0", p0, q0, fee_u(p0, q0, FEE), trade.open_ts)]
    pending_p1, pending_p2 = True, False
    ambiguous = False
    amb_notes: List[str] = []
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    last = p0
    peak = -lots[0].open_fee
    min_run = peak
    dd_peak = 0.0
    stop_hit = tp_hit = False

    def open_lots():
        return [x for x in lots if not x.closed]

    def equity(mark: float) -> float:
        eq = sum(x.net_u for x in lots if x.closed)
        for x in open_lots():
            eq += _mtm(x, side, mark)
        return eq

    def upd(mark: float):
        nonlocal peak, min_run, dd_peak
        eq = equity(mark)
        peak = max(peak, eq)
        dd_peak = min(dd_peak, eq - peak)
        min_run = min(min_run, eq)

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff:
            break
        last = bar.c
        is_entry = bar.ot == entry_bar
        if is_entry:
            # no pre-entry extremes for layer triggers
            upd(bar.c)
            continue

        events: List[Tuple[int, str, dict]] = []
        gap_stop = (side == "LONG" and bar.o <= lv["Pstop"]) or (side == "SHORT" and bar.o >= lv["Pstop"])
        if gap_stop or _touch_stop(side, bar, lv["Pstop"]):
            events.append((0 if mode == "conservative" else 90, "STOP", {"gap": bar.o if gap_stop else None}))
        if _touch_up(side, bar, tp) and open_lots():
            events.append((10 if mode == "optimistic" else 70, "TP_ALL", {}))
        if pending_p1 and _touch_add(side, bar, lv["P1"]):
            events.append((40, "ADD_P1", {}))
        if pending_p2 and _touch_add(side, bar, lv["P2"]):
            events.append((45, "ADD_P2", {}))

        if len({e[1].split("_")[0] for e in events}) > 1 and len(events) > 1:
            # classify ambiguity families
            kinds = {e[1] for e in events}
            if ("STOP" in kinds or "TP_ALL" in kinds) and any(k.startswith("ADD") for k in kinds):
                ambiguous = True
                amb_notes.append(f"{bar.ot.isoformat()}:{sorted(kinds)}")
            elif "STOP" in kinds and "TP_ALL" in kinds:
                ambiguous = True
                amb_notes.append(f"{bar.ot.isoformat()}:STOP+TP")

        events.sort(key=lambda x: x[0])
        for _, et, payload in events:
            if et == "STOP":
                for lot in open_lots():
                    _close(lot, side, lv["Pstop"], bar.ot, "PSTOP", gap=payload.get("gap"))
                pending_p1 = pending_p2 = False
                stop_hit = True
                break
            if et == "TP_ALL":
                for lot in open_lots():
                    # only lots that already filled can exit — all current open lots are filled
                    _close(lot, side, tp, bar.ot, "ALL_T2")
                pending_p1 = pending_p2 = False
                tp_hit = True
                break
            if et == "ADD_P1" and pending_p1:
                px = lv["P1"]
                q = qty_for_notional(notional_per, px, step)
                lots.append(Lot("P1", px, q, fee_u(px, q, FEE), bar.ot))
                pending_p1, pending_p2 = False, True
            if et == "ADD_P2" and pending_p2:
                px = lv["P2"]
                q = qty_for_notional(notional_per, px, step)
                lots.append(Lot("P2", px, q, fee_u(px, q, FEE), bar.ot))
                pending_p2 = False
        upd(bar.c)
        if stop_hit or tp_hit or not open_lots():
            break

    realized = sum(x.net_u for x in lots if x.closed)
    mtm = sum(_mtm(x, side, last) for x in open_lots())
    fees = sum(x.open_fee for x in lots) + sum(x.close_fee for x in lots if x.closed)
    gross_r = sum(x.gross_u for x in lots if x.closed)
    p0_net = sum(x.net_u if x.closed else _mtm(x, side, last) for x in lots if x.name == "P0")
    add_net = sum(x.net_u if x.closed else _mtm(x, side, last) for x in lots if x.name != "P0")
    # exposure time integral approx
    exp = 0.0
    for x in lots:
        end = x.exit_ts or cutoff
        exp += abs(x.entry * x.qty) * max(0.0, (end - x.open_ts).total_seconds() / 60.0)
    hold = None
    if lots:
        last_ts = max((x.exit_ts or cutoff) for x in lots)
        hold = (last_ts - trade.open_ts).total_seconds() / 60.0

    # bounds if ambiguous: re-run optimistic if conservative and vice versa — caller may compute both
    return AllT2Result(
        trade_id=trade.trade_id,
        mode=f"ALLT2_{mode}",
        status="COMPLETE" if not open_lots() else "OPEN",
        realized_net=realized,
        open_mtm_net=mtm,
        total_mark_net=realized + mtm,
        fees=fees,
        gross_realized=gross_r,
        n_lots_filled=len(lots),
        n_lots_closed=sum(1 for x in lots if x.closed),
        stop_hit=stop_hit,
        tp_hit=tp_hit,
        p0_net=p0_net,
        add_net=add_net,
        hold_min=hold,
        max_notional_u=len(lots) * notional_per,
        max_margin_u=len(lots) * (notional_per / 10.0),
        exposure_notional_min=exp,
        min_running_net=min_run,
        dd_from_peak=dd_peak,
        ambiguous=ambiguous,
        amb_note=";".join(amb_notes[:6]),
        lots=[
            {
                "name": x.name,
                "entry": x.entry,
                "qty": x.qty,
                "open_ts": x.open_ts.isoformat(),
                "closed": x.closed,
                "exit_reason": x.exit_reason,
                "exit_ts": x.exit_ts.isoformat() if x.exit_ts else None,
                "net_u": x.net_u if x.closed else _mtm(x, side, last),
                "gross_u": x.gross_u if x.closed else gross(side, x.entry, last, x.qty),
            }
            for x in lots
        ],
    )
