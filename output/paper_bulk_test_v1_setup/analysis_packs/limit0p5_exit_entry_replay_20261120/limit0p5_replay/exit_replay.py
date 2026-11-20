from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    BASELINE_SL,
    BASELINE_TP,
    CUTOFF,
    FEE_RATE_ONE_SIDE,
    SLIPPAGE_ADVERSE,
    SL_GRID,
    TP_GRID,
)
from .klines import Bar, load_bars, mid_of
from .trades import TradeSample, barriers


def exit_fill_price(side: str, barrier: float, *, gap_open: Optional[float] = None) -> float:
    """K-line approx of paper mark: hit barrier then apply adverse slip; gap uses open."""
    px = float(gap_open) if gap_open is not None else float(barrier)
    if side == "LONG":
        return px * (1.0 - SLIPPAGE_ADVERSE)
    return px * (1.0 + SLIPPAGE_ADVERSE)


def gross_pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    if side == "LONG":
        return (exit_px - entry) * qty
    return (entry - exit_px) * qty


def fee_u(price: float, qty: float) -> float:
    return abs(price * qty) * FEE_RATE_ONE_SIDE


def bar_touches(side: str, bar: Bar, tp: float, sl: float) -> Tuple[bool, bool]:
    if side == "LONG":
        return (bar.high >= tp), (bar.low <= sl)
    return (bar.low <= tp), (bar.high >= sl)


def gap_through(side: str, open_px: float, tp: float, sl: float) -> Tuple[bool, bool]:
    if side == "LONG":
        return (open_px >= tp), (open_px <= sl)
    return (open_px <= tp), (open_px >= sl)


@dataclass
class PathExit:
    mode: str  # conservative | optimistic
    status: str  # TP | SL | OPEN | INSUFFICIENT_DATA | AMBIGUOUS_ENTRY_BAR
    exit_ts: Optional[datetime]
    exit_price: Optional[float]
    gross_u: Optional[float]
    close_fee_u: Optional[float]
    realized_net_u: Optional[float]  # open_fee component + close net
    mtm_gross_u: Optional[float]
    mtm_net_u: Optional[float]
    hold_minutes: Optional[float]
    bars_used: int
    note: str = ""


def _mtm_at_cutoff(sample: TradeSample, bars: List[Bar], cutoff: datetime) -> Tuple[Optional[float], Optional[float], str]:
    """Mark-to-market at last bar open_time < cutoff using close as mid proxy."""
    last: Optional[Bar] = None
    for b in bars:
        if b.open_time >= cutoff:
            break
        last = b
    if last is None:
        return None, None, "no_bar_before_cutoff"
    # no bid/ask: use close as mark proxy (NOT claiming full fillability)
    mark = last.close
    if sample.side == "LONG":
        mark_x = mark * (1.0 - SLIPPAGE_ADVERSE)
    else:
        mark_x = mark * (1.0 + SLIPPAGE_ADVERSE)
    g = gross_pnl(sample.side, sample.entry_price, mark_x, sample.qty)
    exit_fee = fee_u(mark_x, sample.qty)
    net = -sample.open_fee_u + (g - exit_fee)
    return g, net, f"mtm_close@{last.open_time.isoformat()}"


def replay_one(
    sample: TradeSample,
    bars: List[Bar],
    tp_u: float,
    sl_u: float,
    *,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
) -> PathExit:
    """Replay fixed-gross exit on 1m OHLC from entry to cutoff.

    mode=conservative: same-bar both hit → SL wins (matches live TP-and-not-SL preference inverted on conflict)
    mode=optimistic: same-bar both hit → TP wins
    Live code: if both hit same quote, stop wins. Conservative aligns with that.
    """
    if not bars:
        return PathExit(
            mode=mode,
            status="INSUFFICIENT_DATA",
            exit_ts=None,
            exit_price=None,
            gross_u=None,
            close_fee_u=None,
            realized_net_u=None,
            mtm_gross_u=None,
            mtm_net_u=None,
            hold_minutes=None,
            bars_used=0,
            note="empty_vault",
        )

    bdef = barriers(sample.entry_price, sample.qty, sample.side, tp_u, sl_u, sample.tick_size)
    tp, sl = bdef["tp"], bdef["sl"]
    entry_bar_start = sample.open_ts.replace(second=0, microsecond=0)

    used = 0
    for bar in bars:
        if bar.open_time + timedelta(minutes=1) <= sample.open_ts:
            continue
        if bar.open_time >= cutoff:
            break
        used += 1
        is_entry_bar = bar.open_time == entry_bar_start

        # Gap at open (only meaningful for bars after entry bar)
        if not is_entry_bar:
            g_tp, g_sl = gap_through(sample.side, bar.open, tp, sl)
            if g_tp or g_sl:
                if g_tp and g_sl:
                    choose_sl = mode == "conservative"
                else:
                    choose_sl = g_sl and not g_tp
                if choose_sl:
                    px = exit_fill_price(sample.side, sl, gap_open=bar.open)
                    reason = "SL"
                else:
                    px = exit_fill_price(sample.side, tp, gap_open=bar.open)
                    reason = "TP"
                g = gross_pnl(sample.side, sample.entry_price, px, sample.qty)
                cf = fee_u(px, sample.qty)
                net = -sample.open_fee_u + (g - cf)
                return PathExit(
                    mode=mode,
                    status=reason,
                    exit_ts=bar.open_time,
                    exit_price=px,
                    gross_u=g,
                    close_fee_u=cf,
                    realized_net_u=net,
                    mtm_gross_u=None,
                    mtm_net_u=None,
                    hold_minutes=(bar.open_time - sample.open_ts).total_seconds() / 60.0,
                    bars_used=used,
                    note="gap_open",
                )

        hit_tp, hit_sl = bar_touches(sample.side, bar, tp, sl)

        if is_entry_bar:
            # Cannot order intra-bar pre/post entry → if range touches any barrier, ambiguous bounds
            if hit_tp or hit_sl:
                # Also check close-only path as one resolution
                close_tp = (sample.side == "LONG" and bar.close >= tp) or (
                    sample.side == "SHORT" and bar.close <= tp
                )
                close_sl = (sample.side == "LONG" and bar.close <= sl) or (
                    sample.side == "SHORT" and bar.close >= sl
                )
                if close_tp and close_sl:
                    choose_sl = mode == "conservative"
                elif close_sl:
                    choose_sl = True
                elif close_tp:
                    choose_sl = False
                else:
                    # touched via high/low but close inside → mark ambiguous; use mode as bound
                    if hit_tp and hit_sl:
                        choose_sl = mode == "conservative"
                        note = "entry_bar_both_touch_close_inside"
                    elif hit_sl:
                        choose_sl = True
                        note = "entry_bar_sl_touch_close_inside"
                    else:
                        choose_sl = False
                        note = "entry_bar_tp_touch_close_inside"
                    reason = "SL" if choose_sl else "TP"
                    barrier = sl if choose_sl else tp
                    px = exit_fill_price(sample.side, barrier)
                    g = gross_pnl(sample.side, sample.entry_price, px, sample.qty)
                    cf = fee_u(px, sample.qty)
                    net = -sample.open_fee_u + (g - cf)
                    return PathExit(
                        mode=mode,
                        status=reason,
                        exit_ts=bar.open_time + timedelta(seconds=30),
                        exit_price=px,
                        gross_u=g,
                        close_fee_u=cf,
                        realized_net_u=net,
                        mtm_gross_u=None,
                        mtm_net_u=None,
                        hold_minutes=0.5,
                        bars_used=used,
                        note=note,
                    )
                reason = "SL" if choose_sl else "TP"
                barrier = sl if choose_sl else tp
                px = exit_fill_price(sample.side, barrier)
                g = gross_pnl(sample.side, sample.entry_price, px, sample.qty)
                cf = fee_u(px, sample.qty)
                net = -sample.open_fee_u + (g - cf)
                return PathExit(
                    mode=mode,
                    status=reason,
                    exit_ts=bar.open_time + timedelta(seconds=59),
                    exit_price=px,
                    gross_u=g,
                    close_fee_u=cf,
                    realized_net_u=net,
                    mtm_gross_u=None,
                    mtm_net_u=None,
                    hold_minutes=(bar.open_time + timedelta(seconds=59) - sample.open_ts).total_seconds()
                    / 60.0,
                    bars_used=used,
                    note="entry_bar_close_resolved",
                )
            continue

        if hit_tp or hit_sl:
            if hit_tp and hit_sl:
                choose_sl = mode == "conservative"
                note = "same_bar_both"
            else:
                choose_sl = hit_sl and not hit_tp
                note = "single_touch"
            reason = "SL" if choose_sl else "TP"
            barrier = sl if choose_sl else tp
            px = exit_fill_price(sample.side, barrier)
            g = gross_pnl(sample.side, sample.entry_price, px, sample.qty)
            cf = fee_u(px, sample.qty)
            net = -sample.open_fee_u + (g - cf)
            # approximate exit at mid-bar
            ets = bar.open_time + timedelta(seconds=30)
            return PathExit(
                mode=mode,
                status=reason,
                exit_ts=ets,
                exit_price=px,
                gross_u=g,
                close_fee_u=cf,
                realized_net_u=net,
                mtm_gross_u=None,
                mtm_net_u=None,
                hold_minutes=(ets - sample.open_ts).total_seconds() / 60.0,
                bars_used=used,
                note=note,
            )

    # Still open at cutoff
    mg, mn, note = _mtm_at_cutoff(sample, bars, cutoff)
    # Check if vault ends before cutoff while trade still open → data gap risk
    last_ot = bars[-1].open_time if bars else None
    insuff = last_ot is not None and last_ot + timedelta(minutes=1) < cutoff
    status = "INSUFFICIENT_DATA" if (insuff and mg is None) else "OPEN"
    return PathExit(
        mode=mode,
        status=status if status == "INSUFFICIENT_DATA" else "OPEN",
        exit_ts=None,
        exit_price=None,
        gross_u=None,
        close_fee_u=None,
        realized_net_u=None,
        mtm_gross_u=mg,
        mtm_net_u=mn,
        hold_minutes=(cutoff - sample.open_ts).total_seconds() / 60.0,
        bars_used=used,
        note=note + (";vault_ends_before_cutoff" if insuff else ""),
    )


def path_milestones(
    sample: TradeSample,
    bars: List[Bar],
    *,
    cutoff: datetime = CUTOFF,
) -> Dict[str, Any]:
    """Rebound / extension diagnostics along OHLC path (not a trading rule)."""
    ref = sample.signal_ref_price
    entry = sample.entry_price
    qty = sample.qty
    side = sample.side
    out: Dict[str, Any] = {
        "trade_id": sample.trade_id,
        "side": side,
        "entry": entry,
        "signal_ref": ref,
        "reached_ref_before_sl25": None,
        "reached_ref_before_sl35": None,
        "reached_ref_before_sl50": None,
        "reached_ref_before_sl75": None,
        "time_to_ref_min": None,
        "gross_at_ref": None,
        "roundtrip_net_at_ref": None,
        "after_plus50_hit_plus100_before_be": None,
        "after_plus50_hit_plus150_before_sl75": None,
        "after_plus50_hit_plus200_before_sl75": None,
        "max_gross_before_cutoff": None,
        "min_gross_before_cutoff": None,
        "first_touch_sl25": None,
        "first_touch_sl35": None,
        "first_touch_sl50": None,
        "first_touch_sl75": None,
        "first_touch_tp50": None,
        "first_touch_tp100": None,
        "first_touch_tp150": None,
        "first_touch_tp200": None,
        "insufficient": not bool(bars),
    }
    if not bars or ref is None or ref <= 0:
        out["insufficient"] = True
        return out

    def gross_at(px: float) -> float:
        return gross_pnl(side, entry, px, qty)

    def hit_level(bar: Bar, level: float, want: str) -> bool:
        # want: up (price up favors long profit) / down
        if want == "up":
            return bar.high >= level
        return bar.low <= level

    # price levels for gross milestones
    def price_for_gross(g: float) -> float:
        # LONG: (p-entry)*q = g → p = entry + g/q
        if side == "LONG":
            return entry + g / qty
        return entry - g / qty

    levels = {
        "tp50": price_for_gross(50),
        "tp100": price_for_gross(100),
        "tp150": price_for_gross(150),
        "tp200": price_for_gross(200),
        "sl25": price_for_gross(-25),
        "sl35": price_for_gross(-35),
        "sl50": price_for_gross(-50),
        "sl75": price_for_gross(-75),
        "be": entry,
        "ref": ref,
    }

    max_g = None
    min_g = None
    hit: Dict[str, Optional[datetime]] = {k: None for k in [
        "ref", "tp50", "tp100", "tp150", "tp200", "sl25", "sl35", "sl50", "sl75", "be"
    ]}

    entry_bar_start = sample.open_ts.replace(second=0, microsecond=0)
    for bar in bars:
        if bar.open_time + timedelta(minutes=1) <= sample.open_ts:
            continue
        if bar.open_time >= cutoff:
            break
        # excursion via mid of bar extremes (approx)
        for px in (bar.high, bar.low, bar.close):
            g = gross_at(px)
            max_g = g if max_g is None else max(max_g, g)
            min_g = g if min_g is None else min(min_g, g)

        # direction of favorable for ref return:
        # LONG limit below ref → ref is above entry; SHORT limit above ref → ref below entry
        if hit["ref"] is None:
            if side == "LONG" and hit_level(bar, ref, "up"):
                hit["ref"] = bar.open_time
            elif side == "SHORT" and hit_level(bar, ref, "down"):
                hit["ref"] = bar.open_time

        for name, want in [
            ("tp50", "up" if side == "LONG" else "down"),
            ("tp100", "up" if side == "LONG" else "down"),
            ("tp150", "up" if side == "LONG" else "down"),
            ("tp200", "up" if side == "LONG" else "down"),
            ("sl25", "down" if side == "LONG" else "up"),
            ("sl35", "down" if side == "LONG" else "up"),
            ("sl50", "down" if side == "LONG" else "up"),
            ("sl75", "down" if side == "LONG" else "up"),
            ("be", "down" if side == "LONG" else "up"),  # return to entry from +side is adverse
        ]:
            if hit[name] is None and hit_level(bar, levels[name], want):
                # for BE after profit: LONG adverse is down to entry
                hit[name] = bar.open_time

    out["max_gross_before_cutoff"] = max_g
    out["min_gross_before_cutoff"] = min_g
    for k in ("sl25", "sl35", "sl50", "sl75", "tp50", "tp100", "tp150", "tp200"):
        out[f"first_touch_{k}"] = hit[k].isoformat() if hit[k] else None

    if hit["ref"] is not None:
        out["time_to_ref_min"] = (hit["ref"] - sample.open_ts).total_seconds() / 60.0
        g_ref = gross_at(ref)
        out["gross_at_ref"] = g_ref
        # round-trip net if exit at ref with fee+slip
        px = exit_fill_price(side, ref)
        g = gross_pnl(side, entry, px, qty)
        out["roundtrip_net_at_ref"] = -sample.open_fee_u + (g - fee_u(px, qty))

    def before(a: Optional[datetime], b: Optional[datetime]) -> Optional[bool]:
        if a is None and b is None:
            return None
        if a is None:
            return False
        if b is None:
            return True
        return a <= b

    out["reached_ref_before_sl25"] = before(hit["ref"], hit["sl25"])
    out["reached_ref_before_sl35"] = before(hit["ref"], hit["sl35"])
    out["reached_ref_before_sl50"] = before(hit["ref"], hit["sl50"])
    out["reached_ref_before_sl75"] = before(hit["ref"], hit["sl75"])

    # after +50: extension vs giveback
    if hit["tp50"] is not None:
        # re-scan after tp50 time for +100 before BE, etc.
        t0 = hit["tp50"]
        hit100 = hit150 = hit200 = hit_be = hit_sl75 = None
        for bar in bars:
            if bar.open_time < t0:
                continue
            if bar.open_time >= cutoff:
                break
            fav = "up" if side == "LONG" else "down"
            adv = "down" if side == "LONG" else "up"
            if hit100 is None and hit_level(bar, levels["tp100"], fav):
                hit100 = bar.open_time
            if hit150 is None and hit_level(bar, levels["tp150"], fav):
                hit150 = bar.open_time
            if hit200 is None and hit_level(bar, levels["tp200"], fav):
                hit200 = bar.open_time
            if hit_be is None and hit_level(bar, levels["be"], adv):
                hit_be = bar.open_time
            if hit_sl75 is None and hit_level(bar, levels["sl75"], adv):
                hit_sl75 = bar.open_time
        out["after_plus50_hit_plus100_before_be"] = before(hit100, hit_be)
        out["after_plus50_hit_plus150_before_sl75"] = before(hit150, hit_sl75)
        out["after_plus50_hit_plus200_before_sl75"] = before(hit200, hit_sl75)

    return out


def summarize_matrix_cell(rows: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    closed = [r for r in rows if r[f"status_{mode}"] in ("TP", "SL")]
    opened = [r for r in rows if r[f"status_{mode}"] == "OPEN"]
    insuff = [r for r in rows if r[f"status_{mode}"] == "INSUFFICIENT_DATA"]
    wins = [r for r in closed if r[f"status_{mode}"] == "TP"]
    losses = [r for r in closed if r[f"status_{mode}"] == "SL"]
    realized = [r[f"realized_net_{mode}"] for r in closed if r[f"realized_net_{mode}"] is not None]
    mtm = [r[f"mtm_net_{mode}"] for r in opened if r.get(f"mtm_net_{mode}") is not None]
    avg_win = sum(r[f"realized_net_{mode}"] for r in wins) / len(wins) if wins else None
    avg_loss = sum(r[f"realized_net_{mode}"] for r in losses) / len(losses) if losses else None
    gross_sum = sum(r[f"gross_{mode}"] or 0.0 for r in closed)
    fee_sum = sum((r.get("open_fee_u") or 0.0) + (r[f"close_fee_{mode}"] or 0.0) for r in closed)
    realized_sum = sum(realized) if realized else 0.0
    mtm_sum = sum(mtm) if mtm else 0.0
    # total mark valuation at cutoff = realized of closed + mtm of open (and 0 for insuff)
    total_mtm_equity_delta = realized_sum + mtm_sum
    hold = [r[f"hold_min_{mode}"] for r in closed if r[f"hold_min_{mode}"] is not None]
    gp = sum(r[f"realized_net_{mode}"] for r in wins) if wins else 0.0
    gl = abs(sum(r[f"realized_net_{mode}"] for r in losses)) if losses else 0.0
    pf = (gp / gl) if gl > 1e-12 else (None if not wins else float("inf"))
    return {
        "mode": mode,
        "n_entries": len(rows),
        "n_closed": len(closed),
        "n_open": len(opened),
        "n_insufficient": len(insuff),
        "n_tp": len(wins),
        "n_sl": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "avg_net_win": avg_win,
        "avg_net_loss": avg_loss,
        "gross_closed_sum": gross_sum,
        "fees_closed_sum": fee_sum,
        "realized_net_sum": realized_sum,
        "open_mtm_net_sum": mtm_sum,
        "cutoff_total_mark_net": total_mtm_equity_delta,
        "pf": pf,
        "avg_hold_min": (sum(hold) / len(hold)) if hold else None,
    }


def run_exit_matrix(
    setup_root: Path,
    profile_id: str,
    samples: List[TradeSample],
    *,
    cutoff: datetime = CUTOFF,
) -> Dict[str, Any]:
    # preload bars per symbol from earliest entry to cutoff
    by_sym: Dict[str, List[Bar]] = {}
    if samples:
        earliest = min(s.open_ts for s in samples)
        for sym in sorted({s.symbol for s in samples}):
            by_sym[sym] = load_bars(setup_root, profile_id, sym, start=earliest - timedelta(minutes=2), end=cutoff)

    trade_rows: List[Dict[str, Any]] = []
    milestone_rows: List[Dict[str, Any]] = []

    for s in samples:
        bars = by_sym.get(s.symbol) or []
        ms = path_milestones(s, bars, cutoff=cutoff)
        milestone_rows.append(ms)
        row: Dict[str, Any] = {
            "trade_id": s.trade_id,
            "symbol": s.symbol,
            "side": s.side,
            "entry_price": s.entry_price,
            "qty": s.qty,
            "open_ts": s.open_ts.isoformat(),
            "open_fee_u": s.open_fee_u,
            "signal_ref_price": s.signal_ref_price,
            "status_at_cutoff_actual": s.status_at_cutoff,
            "actual_complete_net": s.actual_complete_net,
            "actual_exit_reason": s.actual_exit_reason,
            "actual_exit_price": s.actual_exit_price,
            "actual_exit_ts": s.close_ts.isoformat() if s.close_ts and s.status_at_cutoff == "CLOSED" else None,
            "close_after_cutoff": s.close_after_cutoff,
        }
        # store per tp/sl later in separate list; here we keep baseline compare fields
        for mode in ("conservative", "optimistic"):
            pe = replay_one(s, bars, BASELINE_TP, BASELINE_SL, cutoff=cutoff, mode=mode)
            row[f"baseline_status_{mode}"] = pe.status
            row[f"baseline_realized_net_{mode}"] = pe.realized_net_u
            row[f"baseline_exit_price_{mode}"] = pe.exit_price
            row[f"baseline_exit_ts_{mode}"] = pe.exit_ts.isoformat() if pe.exit_ts else None
            row[f"baseline_note_{mode}"] = pe.note
            if s.status_at_cutoff == "CLOSED" and pe.realized_net_u is not None and s.actual_complete_net is not None:
                row[f"baseline_net_diff_{mode}"] = pe.realized_net_u - s.actual_complete_net
            else:
                row[f"baseline_net_diff_{mode}"] = None
        trade_rows.append(row)

    # full matrix detailed rows
    matrix_trade_rows: List[Dict[str, Any]] = []
    matrix_summary: List[Dict[str, Any]] = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            cell_rows: List[Dict[str, Any]] = []
            for s in samples:
                bars = by_sym.get(s.symbol) or []
                base = {
                    "trade_id": s.trade_id,
                    "symbol": s.symbol,
                    "side": s.side,
                    "tp_u": tp,
                    "sl_u": sl,
                    "entry_price": s.entry_price,
                    "qty": s.qty,
                    "open_fee_u": s.open_fee_u,
                    "open_ts": s.open_ts.isoformat(),
                    "actual_status": s.status_at_cutoff,
                    "actual_net": s.actual_complete_net,
                    "actual_reason": s.actual_exit_reason,
                }
                bdef = barriers(s.entry_price, s.qty, s.side, tp, sl, s.tick_size)
                base["tp_price"] = bdef["tp"]
                base["sl_price"] = bdef["sl"]
                for mode in ("conservative", "optimistic"):
                    pe = replay_one(s, bars, tp, sl, cutoff=cutoff, mode=mode)
                    base[f"status_{mode}"] = pe.status
                    base[f"realized_net_{mode}"] = pe.realized_net_u
                    base[f"gross_{mode}"] = pe.gross_u
                    base[f"close_fee_{mode}"] = pe.close_fee_u
                    base[f"mtm_net_{mode}"] = pe.mtm_net_u
                    base[f"mtm_gross_{mode}"] = pe.mtm_gross_u
                    base[f"hold_min_{mode}"] = pe.hold_minutes
                    base[f"exit_ts_{mode}"] = pe.exit_ts.isoformat() if pe.exit_ts else None
                    base[f"exit_price_{mode}"] = pe.exit_price
                    base[f"note_{mode}"] = pe.note
                cell_rows.append(base)
                matrix_trade_rows.append(base)
            for mode in ("conservative", "optimistic"):
                summ = summarize_matrix_cell(cell_rows, mode)
                summ["tp_u"] = tp
                summ["sl_u"] = sl
                # vs baseline cell
                matrix_summary.append(summ)

    # attach baseline improvement vs 50/75 for each summary row
    base_cons = next(
        s for s in matrix_summary if s["tp_u"] == BASELINE_TP and s["sl_u"] == BASELINE_SL and s["mode"] == "conservative"
    )
    base_opt = next(
        s for s in matrix_summary if s["tp_u"] == BASELINE_TP and s["sl_u"] == BASELINE_SL and s["mode"] == "optimistic"
    )
    for s in matrix_summary:
        b = base_cons if s["mode"] == "conservative" else base_opt
        s["delta_realized_vs_baseline"] = s["realized_net_sum"] - b["realized_net_sum"]
        s["delta_cutoff_mark_vs_baseline"] = s["cutoff_total_mark_net"] - b["cutoff_total_mark_net"]

    return {
        "baseline_trade_rows": trade_rows,
        "matrix_trade_rows": matrix_trade_rows,
        "matrix_summary": matrix_summary,
        "milestones": milestone_rows,
        "bars_coverage": {sym: len(bars) for sym, bars in by_sym.items()},
    }
