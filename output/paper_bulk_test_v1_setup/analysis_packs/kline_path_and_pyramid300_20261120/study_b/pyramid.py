from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from common.config import CUTOFF, FEE, PY_ADD_PCT, PY_NOTIONAL, SLIP
from common.data import Bar, Trade, exit_px_slip, fee_u, gross, qty_for_notional, round_tick


@dataclass
class Lot:
    name: str  # P0|P1|P2
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


@dataclass
class PyramidResult:
    trade_id: str
    mode: str  # R1_T1 etc
    notional_per: float
    status: str  # COMPLETE|OPEN|INSUFFICIENT|AMBIGUOUS
    realized_net: float
    open_mtm_net: float
    total_mark_net: float
    fees: float
    gross_realized: float
    n_lots_filled: int
    n_lots_closed: int
    stop_hit: bool
    rebound_pnl: float
    stop_pnl: float
    p0_net: float
    add_net: float
    max_margin_u: float
    max_notional_u: float
    hold_min: Optional[float]
    notes: str
    lots: List[Dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False


def levels_from_p0(side: str, p0: float, tick: float) -> Dict[str, float]:
    if side == "LONG":
        p1 = round_tick(p0 * (1.0 - PY_ADD_PCT), tick)
        p2 = round_tick(p1 * (1.0 - PY_ADD_PCT), tick)
        pstop = round_tick(p2 * (1.0 - PY_ADD_PCT), tick)
    else:
        p1 = round_tick(p0 * (1.0 + PY_ADD_PCT), tick)
        p2 = round_tick(p1 * (1.0 + PY_ADD_PCT), tick)
        pstop = round_tick(p2 * (1.0 + PY_ADD_PCT), tick)
    return {"P1": p1, "P2": p2, "Pstop": pstop}


def _touch_add(side: str, bar: Bar, px: float) -> bool:
    if side == "LONG":
        return bar.l <= px
    return bar.h >= px


def _touch_up(side: str, bar: Bar, px: float) -> bool:
    """Price returned to a higher (LONG) / lower (SHORT) level."""
    if side == "LONG":
        return bar.h >= px
    return bar.l <= px


def _touch_stop(side: str, bar: Bar, px: float) -> bool:
    return _touch_add(side, bar, px)


def close_lot(lot: Lot, side: str, barrier: float, ts: datetime, reason: str, gap: Optional[float] = None) -> None:
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


def mtm_lot(lot: Lot, side: str, mark: float) -> float:
    px = exit_px_slip(side, mark, SLIP)
    g = gross(side, lot.entry, px, lot.qty)
    cf = fee_u(px, lot.qty, FEE)
    return -lot.open_fee + (g - cf)


def simulate_pyramid(
    trade: Trade,
    bars: List[Bar],
    *,
    rebound: str,  # R1|R2
    tp_mode: str,  # T1|T2|T3
    notional_per: float = PY_NOTIONAL,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
    orig_tp_price: Optional[float] = None,
) -> PyramidResult:
    """Independent-trade pyramid replay from actual P0 fill.

    Stop = layered Pstop only (no SL75).
    Same-bar multi-event: conservative prioritizes stop > adds > rebound exits > TP;
    optimistic prioritizes TP/rebounds > adds > stop.
    """
    side = trade.side
    p0 = trade.entry
    tick = trade.tick
    step = trade.step
    lv = levels_from_p0(side, p0, tick)
    tag = f"{rebound}_{tp_mode}"

    if not bars:
        return PyramidResult(
            trade_id=trade.trade_id,
            mode=tag,
            notional_per=notional_per,
            status="INSUFFICIENT",
            realized_net=0,
            open_mtm_net=0,
            total_mark_net=0,
            fees=0,
            gross_realized=0,
            n_lots_filled=0,
            n_lots_closed=0,
            stop_hit=False,
            rebound_pnl=0,
            stop_pnl=0,
            p0_net=0,
            add_net=0,
            max_margin_u=0,
            max_notional_u=0,
            hold_min=None,
            notes="no_bars",
            ambiguous=False,
        )

    q0 = qty_for_notional(notional_per, p0, step)
    lots: List[Lot] = [
        Lot("P0", p0, q0, fee_u(p0, q0, FEE), trade.open_ts),
    ]
    pending_p1 = True
    pending_p2 = False
    ambiguous = False
    notes: List[str] = []

    # T1: first lot gross TP 50U → price from qty
    # T2: same PRICE as original full-size TP50 (passed in or derived from open_row)
    if tp_mode == "T1":
        d = 50.0 / q0 if q0 else 0
        tp_px = round_tick(p0 + d if side == "LONG" else p0 - d, tick)
    elif tp_mode == "T2":
        if orig_tp_price is not None:
            tp_px = float(orig_tp_price)
        else:
            # derive from original trade qty & 50U
            d = 50.0 / trade.qty if trade.qty else 0
            tp_px = round_tick(p0 + d if side == "LONG" else p0 - d, tick)
    else:
        tp_px = None  # T3 cumulative

    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    stop_hit = False
    last_mark = p0

    def open_lots():
        return [x for x in lots if not x.closed]

    def cum_gross_incl_mtm(mark: float) -> float:
        total = 0.0
        for x in lots:
            if x.closed:
                total += x.gross_u
            else:
                total += gross(side, x.entry, mark, x.qty)
        return total

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff:
            break
        is_entry = bar.ot == entry_bar
        # do not use pre-entry hi/lo on entry bar for adds/stops beyond close probe
        last_mark = bar.c

        # Collect possible events this bar
        events: List[Tuple[int, str, Any]] = []
        # priority weights later

        gap_stop = False
        if not is_entry:
            if side == "LONG" and bar.o <= lv["Pstop"]:
                gap_stop = True
            if side == "SHORT" and bar.o >= lv["Pstop"]:
                gap_stop = True

        if gap_stop or (not is_entry and _touch_stop(side, bar, lv["Pstop"])) or (
            is_entry and ((side == "LONG" and bar.c <= lv["Pstop"]) or (side == "SHORT" and bar.c >= lv["Pstop"]))
        ):
            events.append((0 if mode == "conservative" else 90, "STOP", {"gap": bar.o if gap_stop else None}))

        if pending_p1 and not is_entry and _touch_add(side, bar, lv["P1"]):
            events.append((40, "ADD_P1", {}))
        if pending_p2 and not is_entry and _touch_add(side, bar, lv["P2"]):
            events.append((45, "ADD_P2", {}))

        # rebound exits for open add lots
        for lot in open_lots():
            if lot.name == "P0":
                continue
            if rebound == "R1":
                target = p0 if lot.name == "P1" else lv["P1"]
            else:
                target = p0
            if not is_entry and _touch_up(side, bar, target):
                events.append((20 if mode == "optimistic" else 60, "REBOUND", {"lot": lot.name, "target": target}))
            elif is_entry and ((side == "LONG" and bar.c >= target) or (side == "SHORT" and bar.c <= target)):
                events.append((20 if mode == "optimistic" else 60, "REBOUND", {"lot": lot.name, "target": target}))

        # T1/T2 first-lot TP
        if tp_mode in ("T1", "T2") and tp_px is not None:
            p0lot = next((x for x in lots if x.name == "P0" and not x.closed), None)
            if p0lot:
                hit = False
                if is_entry:
                    hit = (side == "LONG" and bar.c >= tp_px) or (side == "SHORT" and bar.c <= tp_px)
                else:
                    hit = _touch_up(side, bar, tp_px)
                if hit:
                    events.append((10 if mode == "optimistic" else 70, "TP_P0", {"px": tp_px}))

        # T3 cumulative
        if tp_mode == "T3":
            mark = bar.c
            if cum_gross_incl_mtm(mark) >= 50.0:
                events.append((15 if mode == "optimistic" else 65, "TP_ALL", {"mark": mark}))

        if not events:
            continue

        # same-bar multi → ambiguous if >1 class
        classes = {e[1] for e in events}
        if len(classes) > 1:
            ambiguous = True
            notes.append(f"amb@{bar.ot.isoformat()}:{sorted(classes)}")

        events.sort(key=lambda x: x[0])
        # process in order; stop cancels pending
        acted = set()
        for _, etype, payload in events:
            if etype in acted and etype.startswith("ADD"):
                continue
            if etype == "STOP":
                for lot in open_lots():
                    close_lot(lot, side, lv["Pstop"], bar.ot, "PY_STOP", gap=payload.get("gap"))
                pending_p1 = pending_p2 = False
                stop_hit = True
                acted.add("STOP")
                break
            if etype == "TP_ALL":
                for lot in open_lots():
                    close_lot(lot, side, payload["mark"], bar.ot, "T3_FLAT")
                pending_p1 = pending_p2 = False
                acted.add("TP_ALL")
                break
            if etype == "TP_P0":
                p0lot = next((x for x in lots if x.name == "P0" and not x.closed), None)
                if p0lot:
                    close_lot(p0lot, side, payload["px"], bar.ot, f"{tp_mode}_P0")
                    # cancel pending adds — no orphan
                    pending_p1 = pending_p2 = False
                    notes.append("cancel_pending_after_P0_tp")
                acted.add("TP_P0")
                continue
            if etype == "REBOUND":
                lot = next((x for x in lots if x.name == payload["lot"] and not x.closed), None)
                if lot:
                    close_lot(lot, side, payload["target"], bar.ot, f"{rebound}_EXIT")
                acted.add("REBOUND")
                continue
            if etype == "ADD_P1" and pending_p1:
                px = lv["P1"]
                q = qty_for_notional(notional_per, px, step)
                lots.append(Lot("P1", px, q, fee_u(px, q, FEE), bar.ot))
                pending_p1 = False
                pending_p2 = True
                acted.add("ADD_P1")
                continue
            if etype == "ADD_P2" and pending_p2:
                px = lv["P2"]
                q = qty_for_notional(notional_per, px, step)
                lots.append(Lot("P2", px, q, fee_u(px, q, FEE), bar.ot))
                pending_p2 = False
                acted.add("ADD_P2")
                continue

        if stop_hit or (tp_mode == "T3" and not open_lots()):
            break
        # if P0 TP and no open lots left
        if not open_lots() and not pending_p1 and not pending_p2:
            break

    # finalize MTM
    realized = sum(x.net_u for x in lots if x.closed)
    fees = sum(x.open_fee for x in lots) + sum(x.close_fee for x in lots if x.closed)
    gross_r = sum(x.gross_u for x in lots if x.closed)
    mtm = sum(mtm_lot(x, side, last_mark) for x in open_lots())
    rebound_pnl = sum(x.net_u for x in lots if x.closed and x.exit_reason and "R" in (x.exit_reason or "") and "EXIT" in (x.exit_reason or ""))
    # fix rebound detection
    rebound_pnl = sum(x.net_u for x in lots if x.closed and x.exit_reason in ("R1_EXIT", "R2_EXIT"))
    stop_pnl = sum(x.net_u for x in lots if x.closed and x.exit_reason == "PY_STOP")
    p0_net = sum(x.net_u if x.closed else mtm_lot(x, side, last_mark) for x in lots if x.name == "P0")
    add_net = sum(
        (x.net_u if x.closed else mtm_lot(x, side, last_mark)) for x in lots if x.name != "P0"
    )
    max_n = len(lots)
    status = "COMPLETE" if not open_lots() else "OPEN"
    if ambiguous:
        status = status  # keep, flag separately
    hold = None
    if lots:
        last_ts = max((x.exit_ts or cutoff) for x in lots)
        hold = (last_ts - trade.open_ts).total_seconds() / 60.0

    return PyramidResult(
        trade_id=trade.trade_id,
        mode=tag,
        notional_per=notional_per,
        status=status,
        realized_net=realized,
        open_mtm_net=mtm,
        total_mark_net=realized + mtm,
        fees=fees,
        gross_realized=gross_r,
        n_lots_filled=len(lots),
        n_lots_closed=sum(1 for x in lots if x.closed),
        stop_hit=stop_hit,
        rebound_pnl=rebound_pnl,
        stop_pnl=stop_pnl,
        p0_net=p0_net,
        add_net=add_net,
        max_margin_u=max_n * (notional_per / 10.0),
        max_notional_u=max_n * notional_per,
        hold_min=hold,
        notes=";".join(notes[:8]),
        lots=[
            {
                "name": x.name,
                "entry": x.entry,
                "qty": x.qty,
                "closed": x.closed,
                "exit_reason": x.exit_reason,
                "net_u": x.net_u if x.closed else mtm_lot(x, side, last_mark),
                "gross_u": x.gross_u if x.closed else gross(side, x.entry, last_mark, x.qty),
            }
            for x in lots
        ],
        ambiguous=ambiguous,
    )


def simulate_single_batch(
    trade: Trade,
    bars: List[Bar],
    *,
    notional: float,
    tp_mode: str,
    use_sl75: bool,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
    orig_tp_price: Optional[float] = None,
) -> PyramidResult:
    """Single lot baseline: either orig SL75+TP or pyramid stop-less fixed TP only with SL75."""
    side = trade.side
    # remap entry to scale qty at same price
    q = qty_for_notional(notional, trade.entry, trade.step)
    # reuse pyramid with no adds by setting absurd add levels — simpler custom loop
    from common.data import Trade as T

    fake = Trade(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        side=side,
        entry=trade.entry,
        qty=q,
        open_fee=fee_u(trade.entry, q, FEE),
        open_ts=trade.open_ts,
        run_id=trade.run_id,
        signal_ref=trade.signal_ref,
        limit_price=trade.limit_price,
        atr=trade.atr,
        signal_open_time_ms=trade.signal_open_time_ms,
        signal_key=trade.signal_key,
        direction_flip=trade.direction_flip,
        tick=trade.tick,
        step=trade.step,
        status=trade.status,
        actual_net=None,
        exit_reason=None,
        exit_price=None,
        close_ts=None,
        close_after_cutoff=False,
        cohort=trade.cohort,
        open_row=trade.open_row,
        close_row=None,
    )
    # Classic TP50/SL75 on this qty
    d_tp = 50.0 / q if q else 0
    d_sl = 75.0 / q if q else 0
    if side == "LONG":
        tp = round_tick(trade.entry + d_tp, trade.tick)
        sl = round_tick(trade.entry - d_sl, trade.tick)
    else:
        tp = round_tick(trade.entry - d_tp, trade.tick)
        sl = round_tick(trade.entry + d_sl, trade.tick)
    if tp_mode == "T2" and orig_tp_price is not None:
        tp = float(orig_tp_price)
        # keep SL75 distance for fair single-batch classic
    lot = Lot("P0", trade.entry, q, fee_u(trade.entry, q, FEE), trade.open_ts)
    if not bars:
        return PyramidResult(
            trade_id=trade.trade_id,
            mode=f"SINGLE_{tp_mode}_{'SL75' if use_sl75 else 'NOSL'}",
            notional_per=notional,
            status="INSUFFICIENT",
            realized_net=0,
            open_mtm_net=0,
            total_mark_net=0,
            fees=lot.open_fee,
            gross_realized=0,
            n_lots_filled=1,
            n_lots_closed=0,
            stop_hit=False,
            rebound_pnl=0,
            stop_pnl=0,
            p0_net=0,
            add_net=0,
            max_margin_u=notional / 10.0,
            max_notional_u=notional,
            hold_min=None,
            notes="no_bars",
        )
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    last = trade.entry
    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff:
            break
        last = bar.c
        is_entry = bar.ot == entry_bar
        hit_tp = hit_sl = False
        if is_entry:
            hit_tp = (side == "LONG" and bar.c >= tp) or (side == "SHORT" and bar.c <= tp)
            hit_sl = use_sl75 and ((side == "LONG" and bar.c <= sl) or (side == "SHORT" and bar.c >= sl))
        else:
            hit_tp = _touch_up(side, bar, tp)
            hit_sl = use_sl75 and _touch_stop(side, bar, sl)
        if hit_tp and hit_sl:
            # conservative SL
            if mode == "conservative":
                close_lot(lot, side, sl, bar.ot, "SL75")
            else:
                close_lot(lot, side, tp, bar.ot, "TP50")
            break
        if hit_sl:
            close_lot(lot, side, sl, bar.ot, "SL75")
            break
        if hit_tp:
            close_lot(lot, side, tp, bar.ot, "TP50")
            break
    if lot.closed:
        return PyramidResult(
            trade_id=trade.trade_id,
            mode=f"SINGLE_{notional}",
            notional_per=notional,
            status="COMPLETE",
            realized_net=lot.net_u,
            open_mtm_net=0,
            total_mark_net=lot.net_u,
            fees=lot.open_fee + lot.close_fee,
            gross_realized=lot.gross_u,
            n_lots_filled=1,
            n_lots_closed=1,
            stop_hit=lot.exit_reason == "SL75",
            rebound_pnl=0,
            stop_pnl=lot.net_u if lot.exit_reason == "SL75" else 0,
            p0_net=lot.net_u,
            add_net=0,
            max_margin_u=notional / 10.0,
            max_notional_u=notional,
            hold_min=(lot.exit_ts - trade.open_ts).total_seconds() / 60.0 if lot.exit_ts else None,
            notes="",
            lots=[{"name": "P0", "entry": lot.entry, "qty": lot.qty, "closed": True, "exit_reason": lot.exit_reason, "net_u": lot.net_u, "gross_u": lot.gross_u}],
        )
    m = mtm_lot(lot, side, last)
    return PyramidResult(
        trade_id=trade.trade_id,
        mode=f"SINGLE_{notional}",
        notional_per=notional,
        status="OPEN",
        realized_net=0,
        open_mtm_net=m,
        total_mark_net=m,
        fees=lot.open_fee,
        gross_realized=0,
        n_lots_filled=1,
        n_lots_closed=0,
        stop_hit=False,
        rebound_pnl=0,
        stop_pnl=0,
        p0_net=m,
        add_net=0,
        max_margin_u=notional / 10.0,
        max_notional_u=notional,
        hold_min=(cutoff - trade.open_ts).total_seconds() / 60.0,
        notes="open_mtm",
        lots=[{"name": "P0", "entry": lot.entry, "qty": lot.qty, "closed": False, "exit_reason": None, "net_u": m, "gross_u": gross(side, lot.entry, last, lot.qty)}],
    )
