#!/usr/bin/env python3
"""Small-scope patch validation — offline only, no retune."""

from __future__ import annotations

import csv
import json
import math
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import (  # noqa: E402
    CANCEL_ADVERSE,
    CUTOFF,
    EXT_END,
    FEE,
    LIMIT_OFFSET,
    PROFILE_B,
    PROFILE_C,
    PY_NOTIONAL,
    RUNS_B,
    RUNS_C,
    SLIP,
    TAIPEI,
)
from common.data import (  # noqa: E402
    Bar,
    Trade,
    bars_around_signal,
    exit_px_slip,
    fee_u,
    gross,
    load_bars,
    load_fills,
    parse_ts,
    qty_for_notional,
    rebuild_trades,
    round_tick,
)
from study_a.path_and_features import (  # noqa: E402
    attach_pre_features,
    diagnose_path,
    signal_bar_features,
    trend_features,
)
from study_b.pyramid import (  # noqa: E402
    Lot,
    close_lot,
    levels_from_p0,
    mtm_lot,
    simulate_pyramid,
    simulate_single_batch,
)
import study_b.pyramid as pyr  # noqa: E402


OUT = ROOT / "patch_validate" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: List[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})


def _f(x, n=4):
    if x is None:
        return "NA"
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return str(x)
    if isinstance(x, float):
        return f"{x:.{n}f}"
    return str(x)


# ---------- 1) Wait-time per-trade expectation ----------
def wait_expectation(label: str, path_csv: Path) -> Dict[str, Any]:
    rows = list(csv.DictReader(path_csv.open()))
    closed = [r for r in rows if r.get("actual_status") == "CLOSED" and r.get("actual_net") not in ("", None)]
    buckets = {"le5": [], "gt5": [], "unknown": []}
    for r in closed:
        w = r.get("wait_min")
        if w in ("", None):
            buckets["unknown"].append(float(r["actual_net"]))
            continue
        (buckets["le5"] if float(w) <= 5 else buckets["gt5"]).append(float(r["actual_net"]))

    def pack(name, xs):
        return {
            "bucket": name,
            "n": len(xs),
            "sum_net": sum(xs) if xs else 0.0,
            "avg_net": (sum(xs) / len(xs)) if xs else None,
        }

    out = {
        "label": label,
        "n_closed": len(closed),
        "le5": pack("le5", buckets["le5"]),
        "gt5": pack("gt5", buckets["gt5"]),
        "unknown": pack("unknown", buckets["unknown"]),
    }
    a, b = out["le5"]["avg_net"], out["gt5"]["avg_net"]
    if a is not None and b is not None:
        out["avg_delta_gt5_minus_le5"] = b - a
        out["claim"] = (
            "gt5_worse_per_trade"
            if b < a - 1e-9
            else ("gt5_better_or_equal_per_trade" if b > a + 1e-9 else "approx_equal")
        )
        out["note"] = (
            "總虧損差同時受筆數影響；不得用 sum 宣稱品質。以 avg_net 為每筆期望。"
        )
    return out


# ---------- 2+3+5) Fair C R2_T2 vs single with SAME T2 + Pstop ----------
def simulate_single_t2_pstop(
    trade: Trade,
    bars: List[Bar],
    *,
    notional: float,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
    fill_prob: float = 1.0,
    gap_stop_use_open: bool = True,
    fee: float = FEE,
    slip: float = SLIP,
) -> Dict[str, Any]:
    """Single 3000: T2 price TP + pyramid Pstop (no adds). Fair control for R2_T2."""
    side = trade.side
    p0 = trade.entry
    q = qty_for_notional(notional, p0, trade.step)
    lv = levels_from_p0(side, p0, trade.tick)
    pstop = lv["Pstop"]
    # T2: same price distance as original fixed gross TP50 on full size
    orig_tp = trade.open_row.get("fixed_gross_tp_price_raw")
    if orig_tp is not None:
        tp = float(orig_tp)
    else:
        d = 50.0 / trade.qty if trade.qty else 0
        tp = round_tick(p0 + d if side == "LONG" else p0 - d, trade.tick)

    lot = Lot("P0", p0, q, fee_u(p0, q, fee), trade.open_ts)
    if not bars:
        return _pack_single(trade, lot, "INSUFFICIENT", p0, notional, 0, 0, None, True, notes="no_bars")

    # optional touch-no-fill: skip first touch with prob — modeled as requiring 2nd bar touch
    need_extra_touch = fill_prob < 1.0
    touched_once = {"tp": False, "stop": False}

    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    last = p0
    max_dd = 0.0  # min running net from entry (independent-trade equity path)
    running = -lot.open_fee
    peak = running
    max_dd_from_peak = 0.0

    def upd_dd(net_now):
        nonlocal peak, max_dd_from_peak, max_dd
        peak = max(peak, net_now)
        max_dd_from_peak = min(max_dd_from_peak, net_now - peak)
        max_dd = min(max_dd, net_now)

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff:
            break
        last = bar.c
        is_entry = bar.ot == entry_bar

        # mark path for DD (independent trade)
        mark_px = exit_px_slip(side, bar.c, slip)
        g = gross(side, p0, mark_px, q)
        upd_dd(-lot.open_fee + (g - fee_u(mark_px, q, fee)))

        gap = None
        hit_stop = hit_tp = False
        if not is_entry:
            if gap_stop_use_open:
                if side == "LONG" and bar.o <= pstop:
                    hit_stop, gap = True, bar.o
                if side == "SHORT" and bar.o >= pstop:
                    hit_stop, gap = True, bar.o
            if side == "LONG":
                hit_stop = hit_stop or bar.l <= pstop
                hit_tp = bar.h >= tp
            else:
                hit_stop = hit_stop or bar.h >= pstop
                hit_tp = bar.l <= tp
        else:
            if side == "LONG":
                hit_stop = bar.c <= pstop
                hit_tp = bar.c >= tp
            else:
                hit_stop = bar.c >= pstop
                hit_tp = bar.c <= tp

        if need_extra_touch:
            # conservative no-fill: first touch ignored
            if hit_tp and not touched_once["tp"]:
                touched_once["tp"] = True
                hit_tp = False
            elif hit_tp and touched_once["tp"]:
                pass
            if hit_stop and not touched_once["stop"]:
                touched_once["stop"] = True
                hit_stop = False

        if hit_tp and hit_stop:
            if mode == "conservative":
                close_lot_fee(lot, side, pstop, bar.ot, "PSTOP", gap=gap, fee=fee, slip=slip)
            else:
                close_lot_fee(lot, side, tp, bar.ot, "T2_TP", fee=fee, slip=slip)
            break
        if hit_stop:
            close_lot_fee(lot, side, pstop, bar.ot, "PSTOP", gap=gap, fee=fee, slip=slip)
            break
        if hit_tp:
            close_lot_fee(lot, side, tp, bar.ot, "T2_TP", fee=fee, slip=slip)
            break

    if lot.closed:
        return _pack_single(
            trade, lot, "COMPLETE", last, notional, max_dd_from_peak, max_dd, lot.exit_ts, False,
            notes=f"tp={tp};pstop={pstop}",
        )
    m = mtm_lot_fee(lot, side, last, fee, slip)
    return _pack_single(
        trade, lot, "OPEN", last, notional, max_dd_from_peak, max_dd, None, False,
        notes=f"tp={tp};pstop={pstop}", mtm=m,
    )


def close_lot_fee(lot, side, barrier, ts, reason, gap=None, fee=FEE, slip=SLIP):
    px = exit_px_slip(side, barrier, slip, gap_open=gap)
    g = gross(side, lot.entry, px, lot.qty)
    cf = fee_u(px, lot.qty, fee)
    lot.closed = True
    lot.exit_px = px
    lot.exit_ts = ts
    lot.exit_reason = reason
    lot.gross_u = g
    lot.close_fee = cf
    lot.net_u = -lot.open_fee + (g - cf)


def mtm_lot_fee(lot, side, mark, fee, slip):
    px = exit_px_slip(side, mark, slip)
    g = gross(side, lot.entry, px, lot.qty)
    return -lot.open_fee + (g - fee_u(px, lot.qty, fee))


def _pack_single(trade, lot, status, last, notional, dd_peak, dd_abs, exit_ts, amb, notes="", mtm=None):
    if lot.closed:
        realized, open_mtm = lot.net_u, 0.0
        hold = (lot.exit_ts - trade.open_ts).total_seconds() / 60.0 if lot.exit_ts else None
    else:
        realized, open_mtm = 0.0, (mtm if mtm is not None else 0.0)
        hold = (CUTOFF - trade.open_ts).total_seconds() / 60.0
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "status": status,
        "realized_net": realized,
        "open_mtm_net": open_mtm,
        "total_mark_net": realized + open_mtm,
        "fees": lot.open_fee + (lot.close_fee if lot.closed else 0.0),
        "gross_realized": lot.gross_u if lot.closed else 0.0,
        "exit_reason": lot.exit_reason,
        "stop_hit": lot.exit_reason == "PSTOP",
        "hold_min": hold,
        "max_notional_u": notional,
        "max_margin_u": notional / 10.0,
        "dd_from_peak_indep": dd_peak,
        "min_running_net_indep": dd_abs,
        "ambiguous": amb,
        "notes": notes,
    }


def run_pyramid_with_cost(
    trade: Trade,
    bars: List[Bar],
    *,
    fee: float,
    slip: float,
    touch_nofill: bool = False,
    gap_stop: bool = True,
    mode: str = "conservative",
) -> Dict[str, Any]:
    old_f, old_s = pyr.FEE, pyr.SLIP
    pyr.FEE, pyr.SLIP = fee, slip
    # simulate_pyramid doesn't support touch_nofill — post-adjust via wrapper notes
    # For touch_nofill: require confirmation by delaying add/stop/tp one touch inside a fork of simulate
    # Simplest: run normal, then if touch_nofill, re-run with bars where first touch bars are neutralized
    bars_use = bars
    if touch_nofill:
        bars_use = _suppress_first_touches(trade, bars)
    try:
        res = simulate_pyramid(
            trade,
            bars_use,
            rebound="R2",
            tp_mode="T2",
            notional_per=PY_NOTIONAL,
            cutoff=CUTOFF,
            mode=mode,
            orig_tp_price=float(trade.open_row["fixed_gross_tp_price_raw"])
            if trade.open_row.get("fixed_gross_tp_price_raw") is not None
            else None,
        )
    finally:
        pyr.FEE, pyr.SLIP = old_f, old_s
    # independent-trade DD approx from lots path not available — compute mark path
    dd = _indep_dd_from_pyramid_path(trade, bars, res, fee, slip)
    d = {k: getattr(res, k) for k in res.__dataclass_fields__}  # type: ignore
    d["lots"] = json.dumps(res.lots, ensure_ascii=False)
    d["dd_from_peak_indep"] = dd["dd_from_peak"]
    d["min_running_net_indep"] = dd["min_running"]
    d["fee"] = fee
    d["slip"] = slip
    d["touch_nofill"] = touch_nofill
    d["gap_stop"] = gap_stop
    return d


def _suppress_first_touches(trade: Trade, bars: List[Bar]) -> List[Bar]:
    """Crude touch-no-fill: on first bar that would touch P1/Pstop/T2, replace extremes with open/close only."""
    side = trade.side
    p0 = trade.entry
    lv = levels_from_p0(side, p0, trade.tick)
    orig_tp = trade.open_row.get("fixed_gross_tp_price_raw")
    if orig_tp is not None:
        tp = float(orig_tp)
    else:
        d = 50.0 / trade.qty if trade.qty else 0
        tp = round_tick(p0 + d if side == "LONG" else p0 - d, trade.tick)
    levels = [lv["P1"], lv["P2"], lv["Pstop"], tp]
    seen = set()
    out = []
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts or bar.ot == entry_bar:
            out.append(bar)
            continue
        b = bar
        for i, lvl in enumerate(levels):
            key = i
            if key in seen:
                continue
            touch = (side == "LONG" and (bar.l <= lvl or bar.h >= lvl)) or (
                side == "SHORT" and (bar.h >= lvl or bar.l <= lvl)
            )
            # only suppress adverse/add/tp first touch broadly
            if side == "LONG" and (bar.l <= lvl or bar.h >= lvl):
                if key not in seen and (bar.l <= lvl or bar.h >= lvl):
                    seen.add(key)
                    b = Bar(ot=bar.ot, o=bar.o, h=max(bar.o, bar.c), l=min(bar.o, bar.c), c=bar.c)
                    break
            if side == "SHORT" and (bar.h >= lvl or bar.l <= lvl):
                if key not in seen:
                    seen.add(key)
                    b = Bar(ot=bar.ot, o=bar.o, h=max(bar.o, bar.c), l=min(bar.o, bar.c), c=bar.c)
                    break
        out.append(b)
    return out


def _indep_dd_from_pyramid_path(trade, bars, res, fee, slip) -> Dict[str, float]:
    """Approximate independent-trade equity using remaining lots MTM each bar + realized so far."""
    # Rebuild timeline of lot events from res.lots
    lots_meta = res.lots
    # Walk bars; assume lots enter at their entry price when bar reaches that price — coarse
    # Simpler: use total_mark path sampled each bar with current open lots estimated from final state only → weak
    # Better: re-simulate lightweight
    side = trade.side
    p0 = trade.entry
    lv = levels_from_p0(side, p0, trade.tick)
    q0 = qty_for_notional(PY_NOTIONAL, p0, trade.step)
    open_lots = [{"name": "P0", "entry": p0, "qty": q0, "open_fee": fee_u(p0, q0, fee), "closed": False}]
    pending_p1, pending_p2 = True, False
    realized = 0.0
    peak = -open_lots[0]["open_fee"]
    min_run = peak
    dd_peak = 0.0
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)

    def equity(mark):
        eq = realized
        for L in open_lots:
            if L["closed"]:
                continue
            px = exit_px_slip(side, mark, slip)
            g = gross(side, L["entry"], px, L["qty"])
            eq += -L["open_fee"] + (g - fee_u(px, L["qty"], fee))
        return eq

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= CUTOFF:
            break
        if bar.ot == entry_bar:
            eq = equity(bar.c)
            peak = max(peak, eq)
            dd_peak = min(dd_peak, eq - peak)
            min_run = min(min_run, eq)
            continue
        # adds
        if pending_p1 and ((side == "LONG" and bar.l <= lv["P1"]) or (side == "SHORT" and bar.h >= lv["P1"])):
            px = lv["P1"]
            q = qty_for_notional(PY_NOTIONAL, px, trade.step)
            open_lots.append({"name": "P1", "entry": px, "qty": q, "open_fee": fee_u(px, q, fee), "closed": False})
            pending_p1, pending_p2 = False, True
        if pending_p2 and ((side == "LONG" and bar.l <= lv["P2"]) or (side == "SHORT" and bar.h >= lv["P2"])):
            px = lv["P2"]
            q = qty_for_notional(PY_NOTIONAL, px, trade.step)
            open_lots.append({"name": "P2", "entry": px, "qty": q, "open_fee": fee_u(px, q, fee), "closed": False})
            pending_p2 = False
        # stop
        stop = (side == "LONG" and bar.l <= lv["Pstop"]) or (side == "SHORT" and bar.h >= lv["Pstop"])
        if stop:
            for L in open_lots:
                if L["closed"]:
                    continue
                px = exit_px_slip(side, lv["Pstop"], slip, gap_open=bar.o if ((side == "LONG" and bar.o <= lv["Pstop"]) or (side == "SHORT" and bar.o >= lv["Pstop"])) else None)
                g = gross(side, L["entry"], px, L["qty"])
                realized += -L["open_fee"] + (g - fee_u(px, L["qty"], fee))
                L["closed"] = True
            pending_p1 = pending_p2 = False
        else:
            # R2 exits to P0 for adds; T2 for P0
            orig_tp = trade.open_row.get("fixed_gross_tp_price_raw")
            tp = float(orig_tp) if orig_tp is not None else None
            for L in open_lots:
                if L["closed"]:
                    continue
                if L["name"] != "P0":
                    if (side == "LONG" and bar.h >= p0) or (side == "SHORT" and bar.l <= p0):
                        px = exit_px_slip(side, p0, slip)
                        g = gross(side, L["entry"], px, L["qty"])
                        realized += -L["open_fee"] + (g - fee_u(px, L["qty"], fee))
                        L["closed"] = True
                elif tp is not None:
                    if (side == "LONG" and bar.h >= tp) or (side == "SHORT" and bar.l <= tp):
                        px = exit_px_slip(side, tp, slip)
                        g = gross(side, L["entry"], px, L["qty"])
                        realized += -L["open_fee"] + (g - fee_u(px, L["qty"], fee))
                        L["closed"] = True
                        pending_p1 = pending_p2 = False
        eq = equity(bar.c)
        peak = max(peak, eq)
        dd_peak = min(dd_peak, eq - peak)
        min_run = min(min_run, eq)
        if all(L["closed"] for L in open_lots):
            break
    return {"dd_from_peak": dd_peak, "min_running": min_run}


def cycle_risk_stats(rows: List[Dict[str, Any]], *, stop_key="stop_hit", net_key="total_mark_net") -> Dict[str, Any]:
    """Full-cycle risk: avg/max full-stop loss; avg successful rebound cycle; stops-per-rebound."""
    # Define successful rebound cycle: trade with rebound_pnl > 0 and not stop_hit? Or any trade that closed adds via R2
    # User: 成功反彈平均淨利 / 全停損平均/最大 / 一次全停損吃掉幾次平均成功反彈
    stops = [r for r in rows if r.get(stop_key) in (True, "True", "true", 1, "1")]
    # successful rebound: rebound_pnl > 0 (even if later stopped — but then net is mixed)
    # Better: trades where rebound_pnl > 0 AND stop_hit is False as "successful rebound cycles"
    # AND separately all stop cycles
    succ = [r for r in rows if float(r.get("rebound_pnl") or 0) > 0 and r.get(stop_key) not in (True, "True", "true", 1, "1")]
    # If rebound then later stop on remaining — still counted in stops with residual
    stop_nets = [float(r[net_key]) for r in stops]
    succ_nets = [float(r[net_key]) for r in succ]
    # Also rebound_pnl-only average for pure rebound profit
    succ_reb_component = [float(r.get("rebound_pnl") or 0) for r in rows if float(r.get("rebound_pnl") or 0) > 0]
    avg_stop = sum(stop_nets) / len(stop_nets) if stop_nets else None
    max_stop = min(stop_nets) if stop_nets else None  # most negative
    avg_succ = sum(succ_nets) / len(succ_nets) if succ_nets else None
    avg_reb_comp = sum(succ_reb_component) / len(succ_reb_component) if succ_reb_component else None
    eats = None
    if avg_succ is not None and avg_succ > 0 and max_stop is not None and max_stop < 0:
        eats = abs(max_stop) / avg_succ
    eats_avg = None
    if avg_succ is not None and avg_succ > 0 and avg_stop is not None and avg_stop < 0:
        eats_avg = abs(avg_stop) / avg_succ
    return {
        "n_full_stop_cycles": len(stops),
        "avg_full_stop_net": avg_stop,
        "max_full_stop_net": max_stop,
        "n_success_rebound_cycles_no_stop": len(succ),
        "avg_success_rebound_cycle_net": avg_succ,
        "n_any_positive_rebound_component": len(succ_reb_component),
        "avg_positive_rebound_component": avg_reb_comp,
        "one_max_stop_eats_n_avg_success_cycles": eats,
        "one_avg_stop_eats_n_avg_success_cycles": eats_avg,
        "method": "per_complete_trade_cycle; NOT max_stop/total_rebound_pool",
        "drawdown_note": "dd_from_peak_indep = independent-trade mark path; NOT full-account drawdown",
    }


# ---------- 4) B TTL5m queue replay + C3 OOS ----------
def ttl_queue_replay(profile: str, runs, rebuilt) -> Dict[str, Any]:
    rows = rebuilt["all_rows"]
    queued = [
        r
        for r in rows
        if r.get("event") == "LIMIT_QUEUED"
        and r.get("run_id") in runs
        and (parse_ts(r, "recorded_at") or CUTOFF) <= CUTOFF
    ]
    filled = {
        r.get("signal_key"): r
        for r in rows
        if r.get("event") == "LIMIT_FILLED" and r.get("run_id") in runs
    }
    expired = {
        r.get("signal_key"): r
        for r in rows
        if r.get("event") == "LIMIT_EXPIRED" and r.get("run_id") in runs
    }
    opens_by_sk = {}
    for t in rebuilt["primary"] + rebuilt["oos"]:
        if t.signal_key:
            opens_by_sk[t.signal_key] = t

    events = []
    for q in queued:
        sk = q.get("signal_key")
        queued_at = parse_ts(q, "recorded_at")
        sym = str(q["symbol"])
        side = str(q["side"])
        ref = float(q.get("signal_ref_price") or 0)
        limit_px = float(q.get("limit_price") or 0)
        actual_fill = filled.get(sk)
        actual_exp = expired.get(sk)
        actual_open = opens_by_sk.get(sk)

        # baseline actual classification
        if actual_fill and parse_ts(actual_fill, "recorded_at") and parse_ts(actual_fill, "recorded_at") <= CUTOFF:
            actual_status = "FILLED"
            actual_wait = (parse_ts(actual_fill, "recorded_at") - queued_at).total_seconds() / 60.0 if queued_at else None
        elif actual_exp and parse_ts(actual_exp, "recorded_at") and parse_ts(actual_exp, "recorded_at") <= CUTOFF:
            actual_status = "EXPIRED"
            actual_wait = None
        else:
            actual_status = "NO_FILL_BY_CUTOFF"
            actual_wait = None

        # TTL5 CF: if actual fill wait > 5 → would cancel as TTL before fill
        ttl_status = actual_status
        ttl_reason = None
        if actual_status == "FILLED" and actual_wait is not None and actual_wait > 5:
            ttl_status = "TTL_CANCEL"
            ttl_reason = "wait_gt_5m"
        elif actual_status == "FILLED":
            ttl_status = "FILLED"
        elif actual_status == "EXPIRED":
            ttl_status = "EXPIRED_UNCHANGED"
        else:
            # still open by cutoff — if age > 5 at cutoff, TTL would have cancelled
            age = (CUTOFF - queued_at).total_seconds() / 60.0 if queued_at else None
            if age is not None and age > 5:
                ttl_status = "TTL_CANCEL"
                ttl_reason = "age_gt_5_unfilled"
            else:
                ttl_status = "STILL_PENDING"

        actual_net = actual_open.actual_net if (actual_open and actual_open.status == "CLOSED") else None
        # if still open at cutoff with actual fill, use 0 for closed-net comparison; count separately
        if actual_open and actual_open.status == "OPEN":
            actual_net_for_sum = None  # exclude from closed expectation
            open_flag = True
        else:
            open_flag = False

        ttl_net = actual_net if ttl_status == "FILLED" and actual_net is not None else (0.0 if ttl_status == "TTL_CANCEL" else actual_net)

        events.append(
            {
                "signal_key": sk,
                "symbol": sym,
                "side": side,
                "queued_at": queued_at.isoformat() if queued_at else None,
                "actual_status": actual_status,
                "actual_wait_min": actual_wait,
                "ttl5_status": ttl_status,
                "ttl5_reason": ttl_reason,
                "actual_complete_net": actual_net,
                "ttl5_complete_net": ttl_net if ttl_status == "FILLED" else (0.0 if ttl_status.startswith("TTL") or ttl_status.startswith("EXPIRED") or ttl_status == "STILL_PENDING" or ttl_status == "NO_FILL_BY_CUTOFF" else ttl_net),
                "was_open_at_cutoff": open_flag,
                "trade_id": actual_open.trade_id if actual_open else None,
            }
        )

    # per-signal nets: only closed fills; TTL cancels contribute 0
    base_closed = [e for e in events if e["actual_status"] == "FILLED" and e["actual_complete_net"] is not None]
    ttl_contrib = []
    for e in events:
        if e["ttl5_status"] == "FILLED" and e["actual_complete_net"] is not None:
            ttl_contrib.append(e["actual_complete_net"])
        elif e["ttl5_status"] == "TTL_CANCEL":
            ttl_contrib.append(0.0)
        # expired/pending: 0
        elif e["ttl5_status"] in ("EXPIRED_UNCHANGED", "STILL_PENDING", "NO_FILL_BY_CUTOFF"):
            if e["actual_status"] == "FILLED" and e["actual_complete_net"] is not None:
                pass  # already handled
            else:
                ttl_contrib.append(0.0)

    # cleaner accounting over all queued signals
    per_signal_base = []
    per_signal_ttl = []
    for e in events:
        if e["actual_status"] == "FILLED" and e["actual_complete_net"] is not None:
            per_signal_base.append(e["actual_complete_net"])
        else:
            per_signal_base.append(0.0)
        if e["ttl5_status"] == "FILLED" and e["actual_complete_net"] is not None:
            per_signal_ttl.append(e["actual_complete_net"])
        else:
            per_signal_ttl.append(0.0)

    cancelled_fills = [e for e in events if e["ttl5_status"] == "TTL_CANCEL" and e["actual_status"] == "FILLED"]
    return {
        "n_queued": len(events),
        "n_actual_filled_closed": len(base_closed),
        "n_ttl_cancel_of_fills": len(cancelled_fills),
        "sum_net_per_signal_baseline": sum(per_signal_base),
        "avg_net_per_signal_baseline": sum(per_signal_base) / len(per_signal_base) if per_signal_base else None,
        "sum_net_per_signal_ttl5": sum(per_signal_ttl),
        "avg_net_per_signal_ttl5": sum(per_signal_ttl) / len(per_signal_ttl) if per_signal_ttl else None,
        "sum_closed_fills_only_baseline": sum(e["actual_complete_net"] for e in base_closed),
        "avg_closed_fills_only_baseline": (
            sum(e["actual_complete_net"] for e in base_closed) / len(base_closed) if base_closed else None
        ),
        "kept_fills_under_ttl": [e for e in events if e["ttl5_status"] == "FILLED" and e["actual_complete_net"] is not None],
        "cancelled_fill_nets": [e["actual_complete_net"] for e in cancelled_fills],
        "events": events,
    }


def c3_oos_validate(profile: str, rebuilt) -> Dict[str, Any]:
    """Fixed C3 from primary definition; validate on post-cutoff opens only (no retune)."""
    primary_closed = [t for t in rebuilt["primary"] if t.status == "CLOSED"]
    # build primary feature table to confirm rule
    primary_rows = []
    for t in primary_closed:
        bars = load_bars(profile, t.symbol, t.open_ts - timedelta(minutes=2), CUTOFF)
        row = diagnose_path(t, bars, cutoff=CUTOFF)
        row = attach_pre_features(profile, t, row)
        primary_rows.append(row)

    def drop_c3(r):
        return bool(r.get("pre_fill_accel_against") is True and r.get("trend_15m_vs_side") == "AGAINST")

    primary_drop = [r for r in primary_rows if drop_c3(r)]
    primary_keep = [r for r in primary_rows if not drop_c3(r)]

    # OOS / subsequent opens
    oos = rebuilt["oos"]
    oos_rows = []
    for t in oos:
        bars = load_bars(profile, t.symbol, t.open_ts - timedelta(minutes=2), EXT_END)
        row = diagnose_path(t, bars, cutoff=EXT_END)
        row = attach_pre_features(profile, t, row)
        # actual net if closed after cutoff
        if t.close_ts and t.close_row:
            from common.data import open_comp

            row["oos_actual_net"] = open_comp(t.open_row) + float(t.close_row.get("net_u") or 0)
            row["oos_exit_reason"] = t.close_row.get("exit_reason")
        else:
            row["oos_actual_net"] = None
        row["c3_would_drop"] = drop_c3(row)
        oos_rows.append(row)

    return {
        "rule": "drop if pre_fill_accel_against AND trend_15m_vs_side==AGAINST (fixed; no retune)",
        "primary_n_closed": len(primary_rows),
        "primary_n_drop": len(primary_drop),
        "primary_n_keep": len(primary_keep),
        "primary_sum_keep": sum(float(r["actual_net"]) for r in primary_keep if r.get("actual_net") is not None),
        "primary_sum_all": sum(float(r["actual_net"]) for r in primary_rows if r.get("actual_net") is not None),
        "primary_against_accel_n": sum(1 for r in primary_rows if r.get("pre_fill_accel_against") is True),
        "primary_trend15_against_n": sum(1 for r in primary_rows if r.get("trend_15m_vs_side") == "AGAINST"),
        "oos_n": len(oos_rows),
        "oos_rows": oos_rows,
        "c_note_for_C_profile": None,
    }


def main() -> int:
    prev = ROOT
    # ----- 1 wait -----
    wait_B = wait_expectation("B", prev / "out_B" / "A_path_trades.csv")
    wait_C = wait_expectation("C", prev / "out_C" / "A_path_trades.csv")
    (OUT / "1_wait_expectation.json").write_text(
        json.dumps({"B": wait_B, "C": wait_C}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ----- rebuild C for fair R2_T2 -----
    print("=== C fair R2_T2 ===")
    reb_c = rebuild_trades(PROFILE_C, set(RUNS_C), cutoff=CUTOFF)
    primary_c = reb_c["primary"]
    earliest = min(t.open_ts for t in primary_c)
    bars_c = {
        s: load_bars(PROFILE_C, s, earliest - timedelta(minutes=5), CUTOFF)
        for s in sorted({t.symbol for t in primary_c})
    }

    fair_rows = []
    for t in primary_c:
        bars = bars_c[t.symbol]
        single = simulate_single_t2_pstop(t, bars, notional=PY_NOTIONAL, mode="conservative")
        single["arm"] = "A_single_3000_T2_Pstop"
        # classic TP50/SL75 reference (NOT fair add-only control)
        classic = simulate_single_batch(
            t, bars, notional=PY_NOTIONAL, tp_mode="T1", use_sl75=True, cutoff=CUTOFF, mode="conservative",
            orig_tp_price=float(t.open_row["fixed_gross_tp_price_raw"]) if t.open_row.get("fixed_gross_tp_price_raw") is not None else None,
        )
        pyr_res = run_pyramid_with_cost(t, bars, fee=FEE, slip=SLIP, touch_nofill=False, mode="conservative")
        fair_rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "A_mark": single["total_mark_net"],
                "A_realized": single["realized_net"],
                "A_mtm": single["open_mtm_net"],
                "A_fees": single["fees"],
                "A_hold": single["hold_min"],
                "A_status": single["status"],
                "A_exit": single["exit_reason"],
                "A_dd_peak": single["dd_from_peak_indep"],
                "A_max_notional": single["max_notional_u"],
                "B_mark": pyr_res["total_mark_net"],
                "B_realized": pyr_res["realized_net"],
                "B_mtm": pyr_res["open_mtm_net"],
                "B_fees": pyr_res["fees"],
                "B_hold": pyr_res["hold_min"],
                "B_status": pyr_res["status"],
                "B_stop_hit": pyr_res["stop_hit"],
                "B_rebound_pnl": pyr_res["rebound_pnl"],
                "B_stop_pnl": pyr_res["stop_pnl"],
                "B_p0_net": pyr_res["p0_net"],
                "B_add_net": pyr_res["add_net"],
                "B_n_lots": pyr_res["n_lots_filled"],
                "B_ambiguous": pyr_res["ambiguous"],
                "B_dd_peak": pyr_res["dd_from_peak_indep"],
                "B_max_notional": pyr_res["max_notional_u"],
                "classic_sl75_mark": classic.total_mark_net,
                "classic_sl75_realized": classic.realized_net,
                "classic_sl75_fees": classic.fees,
                "classic_note": "TP50/SL75 — NOT same-stop fair control",
                "delta_B_minus_A": pyr_res["total_mark_net"] - single["total_mark_net"],
            }
        )

    write_csv(OUT / "2_C_fair_R2T2_vs_single.csv", fair_rows)
    fair_sum = {
        "n": len(fair_rows),
        "A_sum_mark": sum(r["A_mark"] for r in fair_rows),
        "A_sum_realized": sum(r["A_realized"] for r in fair_rows),
        "A_sum_mtm": sum(r["A_mtm"] for r in fair_rows),
        "A_sum_fees": sum(r["A_fees"] for r in fair_rows),
        "A_avg_hold": sum(r["A_hold"] or 0 for r in fair_rows) / len(fair_rows),
        "A_max_notional": max(r["A_max_notional"] for r in fair_rows),
        "A_worst_dd_peak": min(r["A_dd_peak"] for r in fair_rows),
        "B_sum_mark": sum(r["B_mark"] for r in fair_rows),
        "B_sum_realized": sum(r["B_realized"] for r in fair_rows),
        "B_sum_mtm": sum(r["B_mtm"] for r in fair_rows),
        "B_sum_fees": sum(r["B_fees"] for r in fair_rows),
        "B_avg_hold": sum((r["B_hold"] or 0) for r in fair_rows) / len(fair_rows),
        "B_max_notional": max(r["B_max_notional"] for r in fair_rows),
        "B_worst_dd_peak": min(r["B_dd_peak"] for r in fair_rows),
        "B_n_ambiguous": sum(1 for r in fair_rows if r["B_ambiguous"] in (True, "True")),
        "B_n_open": sum(1 for r in fair_rows if r["B_status"] == "OPEN"),
        "A_n_open": sum(1 for r in fair_rows if r["A_status"] == "OPEN"),
        "delta_sum_mark": sum(r["delta_B_minus_A"] for r in fair_rows),
        "classic_sl75_sum_mark": sum(r["classic_sl75_mark"] for r in fair_rows),
    }

    # stress on C R2_T2
    print("=== C R2_T2 stress ===")
    stress_rows = []
    stress_sum = []
    for fee_m, slip_m, touch, tag in [
        (1.0, 1.0, False, "base"),
        (1.5, 1.5, False, "fee_slip_1p5"),
        (2.0, 2.0, False, "fee_slip_2p0"),
        (1.0, 1.0, True, "touch_nofill"),
    ]:
        rs = []
        for t in primary_c:
            bars = bars_c[t.symbol]
            d = run_pyramid_with_cost(
                t, bars, fee=FEE * fee_m, slip=SLIP * slip_m, touch_nofill=touch, mode="conservative"
            )
            d["trade_id"] = t.trade_id
            d["tag"] = tag
            rs.append(d)
            stress_rows.append(
                {
                    "tag": tag,
                    "trade_id": t.trade_id,
                    "mark": d["total_mark_net"],
                    "realized": d["realized_net"],
                    "mtm": d["open_mtm_net"],
                    "fees": d["fees"],
                    "stop_hit": d["stop_hit"],
                    "rebound_pnl": d["rebound_pnl"],
                    "stop_pnl": d["stop_pnl"],
                    "ambiguous": d["ambiguous"],
                    "status": d["status"],
                    "dd_peak": d["dd_from_peak_indep"],
                }
            )
        stress_sum.append(
            {
                "tag": tag,
                "fee_mult": fee_m,
                "slip_mult": slip_m,
                "touch_nofill": touch,
                "sum_mark": sum(r["total_mark_net"] for r in rs),
                "sum_realized": sum(r["realized_net"] for r in rs),
                "sum_mtm": sum(r["open_mtm_net"] for r in rs),
                "sum_fees": sum(r["fees"] for r in rs),
                "n_stop": sum(1 for r in rs if r["stop_hit"]),
                "n_ambiguous": sum(1 for r in rs if r["ambiguous"]),
                "n_open": sum(1 for r in rs if r["status"] == "OPEN"),
                "risk": cycle_risk_stats(
                    [
                        {
                            "stop_hit": r["stop_hit"],
                            "total_mark_net": r["total_mark_net"],
                            "rebound_pnl": r["rebound_pnl"],
                        }
                        for r in rs
                    ]
                ),
            }
        )
    # gap-stop off sensitivity (fill at barrier without gap open)
    # approximate by reusing base (gap already in simulate); document as same as base when no gap in path
    stress_sum.append(
        {
            "tag": "gap_stop_note",
            "note": "pyramid simulator already uses gap_open when bar.open beyond Pstop; touch_nofill suppresses first OHLC extremes to close-only",
        }
    )
    write_csv(OUT / "3_C_R2T2_stress_trades.csv", stress_rows)
    (OUT / "3_C_R2T2_stress_summary.json").write_text(json.dumps(stress_sum, ensure_ascii=False, indent=2), encoding="utf-8")

    risk_fair = cycle_risk_stats(
        [
            {
                "stop_hit": r["B_stop_hit"],
                "total_mark_net": r["B_mark"],
                "rebound_pnl": r["B_rebound_pnl"],
            }
            for r in fair_rows
        ]
    )

    # ----- 4 B diagnostics -----
    print("=== B TTL5 + C3 OOS ===")
    reb_b = rebuild_trades(PROFILE_B, set(RUNS_B), cutoff=CUTOFF)
    ttl = ttl_queue_replay(PROFILE_B, set(RUNS_B), reb_b)
    write_csv(OUT / "4_B_ttl5_queue_events.csv", ttl["events"])
    ttl_summary = {k: v for k, v in ttl.items() if k not in ("events", "kept_fills_under_ttl")}
    ttl_summary["kept_fills_n"] = len(ttl["kept_fills_under_ttl"])
    ttl_summary["cancelled_fill_sum_net"] = sum(x or 0 for x in ttl["cancelled_fill_nets"])
    ttl_summary["cancelled_fill_avg_net"] = (
        sum(x or 0 for x in ttl["cancelled_fill_nets"]) / len(ttl["cancelled_fill_nets"])
        if ttl["cancelled_fill_nets"]
        else None
    )
    (OUT / "4_B_ttl5_summary.json").write_text(json.dumps(ttl_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    c3 = c3_oos_validate(PROFILE_B, reb_b)
    # serialize oos
    oos_ser = []
    for r in c3["oos_rows"]:
        oos_ser.append({k: v for k, v in r.items() if not isinstance(v, (list, dict))})
    c3_out = {k: v for k, v in c3.items() if k != "oos_rows"}
    c3_out["oos_rows"] = oos_ser
    # C profile contradiction fix facts
    reb_c_feat = []
    for t in [x for x in reb_c["primary"] if x.status == "CLOSED"]:
        bars = load_bars(PROFILE_C, t.symbol, t.open_ts - timedelta(minutes=2), CUTOFF)
        row = diagnose_path(t, bars, cutoff=CUTOFF)
        row = attach_pre_features(PROFILE_C, t, row)
        reb_c_feat.append(row)
    c3_out["C_profile_against_accel_n"] = sum(1 for r in reb_c_feat if r.get("pre_fill_accel_against") is True)
    c3_out["C_profile_trend15_against_n"] = sum(1 for r in reb_c_feat if r.get("trend_15m_vs_side") == "AGAINST")
    c3_out["C_profile_both_n"] = sum(
        1
        for r in reb_c_feat
        if r.get("pre_fill_accel_against") is True and r.get("trend_15m_vs_side") == "AGAINST"
    )
    c3_out["C_template_fix"] = (
        "C 主樣本 pre_fill_accel_against=0 且 trend_15m AGAINST=0；"
        "不得套用 B 的「大虧與逆向加速重疊」模板句。"
    )
    (OUT / "4_B_C3_oos.json").write_text(json.dumps(c3_out, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUT / "4_B_C3_oos_rows.csv", oos_ser)

    # MASTER patch report
    lines = []
    lines.append("# 小範圍補驗報告（不重新掃參）")
    lines.append("")
    lines.append(f"- 產製：{datetime.now().astimezone().isoformat()}")
    lines.append("- 範圍：等待期望修正｜C R2_T2 公平對照+壓力｜B TTL5／C3 獨立診斷｜週期風險")
    lines.append("")
    lines.append("## 1. 等待時間：每筆期望（不是總虧損）")
    for w in (wait_B, wait_C):
        lines.append(
            f"- **{w['label']}**：≤5m n={w['le5']['n']} avg=`{_f(w['le5']['avg_net'])}` sum=`{_f(w['le5']['sum_net'])}`；"
            f">5m n={w['gt5']['n']} avg=`{_f(w['gt5']['avg_net'])}` sum=`{_f(w['gt5']['sum_net'])}`；"
            f"Δavg(>5−≤5)=`{_f(w.get('avg_delta_gt5_minus_le5'))}` → **{w.get('claim')}**"
        )
    lines.append(
        "- **結論修正**：B 的 >5m 每筆較差（-4.76 vs -2.20）；**C 的 >5m 每筆不較差**（-12.05 vs -13.29）。"
        "先前用總虧損（C >5m 總虧更多只因筆數 22>14）宣稱兩邊都證實長等待較差，**作廢**。"
    )
    lines.append("")
    lines.append("## 2. C：公平對照（同訊號／同 P0／同 T2 價／同 Pstop／同截止）")
    lines.append(
        f"- A 單批3000 T2+Pstop：盯市合計 `{_f(fair_sum['A_sum_mark'])}`｜已實現 `{_f(fair_sum['A_sum_realized'])}`｜"
        f"未平盯市 `{_f(fair_sum['A_sum_mtm'])}`｜費用 `{_f(fair_sum['A_sum_fees'])}`｜"
        f"均持倉分 `{_f(fair_sum['A_avg_hold'],1)}`｜最大名義 `{_f(fair_sum['A_max_notional'])}`｜"
        f"最差獨立回撤(峰轉) `{_f(fair_sum['A_worst_dd_peak'])}`｜未平n={fair_sum['A_n_open']}"
    )
    lines.append(
        f"- B R2_T2 三批3000：盯市合計 `{_f(fair_sum['B_sum_mark'])}`｜已實現 `{_f(fair_sum['B_sum_realized'])}`｜"
        f"未平盯市 `{_f(fair_sum['B_sum_mtm'])}`｜費用 `{_f(fair_sum['B_sum_fees'])}`｜"
        f"均持倉分 `{_f(fair_sum['B_avg_hold'],1)}`｜最大名義 `{_f(fair_sum['B_max_notional'])}`｜"
        f"最差獨立回撤 `{_f(fair_sum['B_worst_dd_peak'])}`｜歧義n={fair_sum['B_n_ambiguous']}｜未平n={fair_sum['B_n_open']}"
    )
    lines.append(f"- **Δ(B−A) 盯市合計** = `{_f(fair_sum['delta_sum_mark'])}`")
    lines.append(
        f"- 原 TP50/SL75 同名義單批（**非**只差加倉對照）：盯市 `{_f(fair_sum['classic_sl75_sum_mark'])}`"
    )
    lines.append("")
    lines.append("## 3. C R2_T2 成本壓力（不再用 R1_T1 代替）")
    for s in stress_sum:
        if s.get("tag") == "gap_stop_note":
            lines.append(f"- {s['tag']}: {s.get('note')}")
            continue
        lines.append(
            f"- {s['tag']}: 盯市 `{_f(s['sum_mark'])}`｜已實現 `{_f(s['sum_realized'])}`｜未平 `{_f(s['sum_mtm'])}`｜"
            f"費 `{_f(s['sum_fees'])}`｜停n={s['n_stop']}｜未平n={s['n_open']}"
        )
    lines.append("")
    lines.append("## 5. 完整交易週期風險（R2_T2 公平 B 臂）")
    lines.append(f"- {json.dumps(risk_fair, ensure_ascii=False)}")
    lines.append("- 回撤欄位為**獨立交易**標記路徑，**不是**完整帳戶回撤。")
    lines.append("")
    lines.append("## 4. B 兩個獨立診斷（不疊加）")
    lines.append("### 4a 原規則 vs TTL5m（完整掛單事件）")
    lines.append(
        f"- queued={ttl_summary['n_queued']}｜實際已平成交={ttl_summary['n_actual_filled_closed']}｜"
        f"TTL取消原成交={ttl_summary['n_ttl_cancel_of_fills']}"
    )
    lines.append(
        f"- 每訊號淨合計：原 `{_f(ttl_summary['sum_net_per_signal_baseline'])}` "
        f"(avg `{_f(ttl_summary['avg_net_per_signal_baseline'])}`) → "
        f"TTL5 `{_f(ttl_summary['sum_net_per_signal_ttl5'])}` "
        f"(avg `{_f(ttl_summary['avg_net_per_signal_ttl5'])}`)"
    )
    lines.append(
        f"- 被 TTL 拿掉的成交：n={ttl_summary['n_ttl_cancel_of_fills']} "
        f"sum_net=`{_f(ttl_summary['cancelled_fill_sum_net'])}` avg=`{_f(ttl_summary['cancelled_fill_avg_net'])}`"
    )
    lines.append("### 4b 原規則 vs C3（固定條件＋後續新訊號）")
    lines.append(
        f"- 主樣本 closed={c3_out['primary_n_closed']}｜C3 drop={c3_out['primary_n_drop']} keep={c3_out['primary_n_keep']}｜"
        f"keep淨額 `{_f(c3_out['primary_sum_keep'])}` / 全樣 `{_f(c3_out['primary_sum_all'])}`"
    )
    lines.append(
        f"- against_accel={c3_out['primary_against_accel_n']}｜trend15_against={c3_out['primary_trend15_against_n']}"
    )
    lines.append(f"- OOS n={c3_out['oos_n']}（開倉>截止，**不參與選參**）：")
    for r in oos_ser:
        lines.append(
            f"  - `{r.get('trade_id')}` drop={r.get('c3_would_drop')} net={r.get('oos_actual_net')} "
            f"exit={r.get('oos_exit_reason')} against={r.get('pre_fill_accel_against')} trend15={r.get('trend_15m_vs_side')}"
        )
    lines.append("")
    lines.append("## C 模板矛盾修正")
    lines.append(c3_out["C_template_fix"])
    lines.append(
        f"- 實測 C：against_accel={c3_out['C_profile_against_accel_n']}｜"
        f"trend15_against={c3_out['C_profile_trend15_against_n']}｜both={c3_out['C_profile_both_n']}"
    )
    lines.append("")
    lines.append("## 樣本清單")
    lines.append(
        f"- C primary n={len(primary_c)}｜open_at_cutoff={len(reb_c['open'])}｜oos={[t.trade_id for t in reb_c['oos']]}"
    )
    lines.append(
        f"- B primary n={len(reb_b['primary'])}｜open={len(reb_b['open'])}｜oos={[t.trade_id for t in reb_b['oos']]}"
    )
    lines.append("- 逐筆：`2_C_fair_R2T2_vs_single.csv` `3_C_R2T2_stress_trades.csv` `4_B_ttl5_queue_events.csv`")

    (OUT / "PATCH_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "fair_sum.json").write_text(json.dumps({"fair_sum": fair_sum, "risk_fair": risk_fair}, ensure_ascii=False, indent=2), encoding="utf-8")
    print((OUT / "PATCH_REPORT.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
