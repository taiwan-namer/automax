from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from common.config import CUTOFF, FEE, SLIP
from common.data import Bar, Trade, bars_around_signal, exit_px_slip, fee_u, gross, load_bars, parse_ts


def _price_for_gross(side: str, entry: float, qty: float, g: float) -> float:
    if side == "LONG":
        return entry + g / qty
    return entry - g / qty


def _hit(side: str, bar: Bar, level: float, favorable: bool) -> bool:
    # favorable True = profit direction
    if side == "LONG":
        return (bar.h >= level) if favorable else (bar.l <= level)
    return (bar.l <= level) if favorable else (bar.h >= level)


def diagnose_path(
    trade: Trade,
    bars: List[Bar],
    *,
    cutoff: datetime = CUTOFF,
    queued_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    side = trade.side
    entry = trade.entry
    qty = trade.qty
    ref = trade.signal_ref
    row: Dict[str, Any] = {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "side": side,
        "run_id": trade.run_id,
        "cohort": trade.cohort,
        "signal_ref": ref,
        "entry": entry,
        "qty": qty,
        "open_ts": trade.open_ts.isoformat(),
        "queued_at": queued_at.isoformat() if queued_at else None,
        "wait_min": ((trade.open_ts - queued_at).total_seconds() / 60.0) if queued_at else None,
        "atr": trade.atr,
        "offset_over_atr": (0.005 * ref / trade.atr) if (ref and trade.atr and trade.atr > 0) else None,
        "actual_status": trade.status,
        "actual_exit_reason": trade.exit_reason,
        "actual_net": trade.actual_net,
        "actual_exit_ts": trade.close_ts.isoformat() if trade.close_ts else None,
        "path_model": "1m_ohlc_approx",
        "insufficient_bars": not bool(bars),
    }

    levels = {
        "p25": _price_for_gross(side, entry, qty, 25),
        "p50": _price_for_gross(side, entry, qty, 50),
        "p75": _price_for_gross(side, entry, qty, 75),
        "p100": _price_for_gross(side, entry, qty, 100),
        "m25": _price_for_gross(side, entry, qty, -25),
        "m50": _price_for_gross(side, entry, qty, -50),
        "m75": _price_for_gross(side, entry, qty, -75),
    }
    first: Dict[str, Optional[datetime]] = {k: None for k in list(levels) + ["ref", "mfe", "mae"]}
    mfe = mae = None
    mfe_t = mae_t = None
    max_adverse_before_ref = None
    time_to_ref = None
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff and trade.cohort == "primary":
            # primary path ends at cutoff; for still-open use path to cutoff
            if trade.status == "CLOSED" and trade.close_ts and bar.ot >= trade.close_ts:
                break
            if trade.status != "CLOSED" and bar.ot >= cutoff:
                break
        if trade.status == "CLOSED" and trade.close_ts and bar.ot >= trade.close_ts:
            break

        # skip using pre-entry extremes on entry bar: only close for excursion that bar
        pts = [bar.c] if bar.ot == entry_bar else [bar.h, bar.l, bar.c]
        for px in pts:
            g = gross(side, entry, px, qty)
            if mfe is None or g > mfe:
                mfe, mfe_t = g, bar.ot
            if mae is None or g < mae:
                mae, mae_t = g, bar.ot

        if first["ref"] is None and ref:
            if _hit(side, bar, ref, favorable=True):
                first["ref"] = bar.ot
                time_to_ref = (bar.ot - trade.open_ts).total_seconds() / 60.0
                # max adverse before ref = mae so far
                max_adverse_before_ref = mae

        for name, lvl in levels.items():
            fav = name.startswith("p")
            if first[name] is None and _hit(side, bar, lvl, favorable=fav):
                first[name] = bar.ot

    row["mfe_gross"] = mfe
    row["mae_gross"] = mae
    row["mfe_ts"] = mfe_t.isoformat() if mfe_t else None
    row["mae_ts"] = mae_t.isoformat() if mae_t else None
    if mfe_t and mae_t:
        if mae_t < mfe_t:
            row["mfe_mae_order"] = "MAE_then_MFE"
        elif mfe_t < mae_t:
            row["mfe_mae_order"] = "MFE_then_MAE"
        else:
            row["mfe_mae_order"] = "SAME_BAR"
    else:
        row["mfe_mae_order"] = None

    for k, ts in first.items():
        if k in ("mfe", "mae"):
            continue
        row[f"first_{k}"] = ts.isoformat() if ts else None
    row["time_to_ref_min"] = time_to_ref
    row["max_adverse_gross_before_ref"] = max_adverse_before_ref

    # roundtrip net if exit at ref (diagnostic, not a rule)
    if ref and ref > 0:
        px = exit_px_slip(side, ref, SLIP)
        g = gross(side, entry, px, qty)
        row["roundtrip_net_at_ref"] = -trade.open_fee + (g - fee_u(px, qty, FEE))
    else:
        row["roundtrip_net_at_ref"] = None

    # labels (post-hoc only)
    if trade.actual_net is not None:
        row["label_win"] = trade.actual_net > 0
    else:
        row["label_win"] = None
    row["label_reached_ref_before_m75"] = None
    if first.get("ref") and first.get("m75"):
        row["label_reached_ref_before_m75"] = first["ref"] <= first["m75"]
    elif first.get("ref") and not first.get("m75"):
        row["label_reached_ref_before_m75"] = True
    elif first.get("m75") and not first.get("ref"):
        row["label_reached_ref_before_m75"] = False

    row["label_direct_adverse"] = bool(
        first.get("m50") and (not first.get("p25") or (first["m50"] < (first["p25"] or first["m50"])))
    )
    return row


def signal_bar_features(bars_pre: List[Bar]) -> Dict[str, Any]:
    if not bars_pre:
        return {"feat_ok": False}
    b = bars_pre[-1]
    body = abs(b.c - b.o)
    rng = max(b.h - b.l, 1e-12)
    upper = b.h - max(b.o, b.c)
    lower = min(b.o, b.c) - b.l
    close_pos = (b.c - b.l) / rng
    return {
        "feat_ok": True,
        "sig_body": body,
        "sig_range": rng,
        "sig_body_frac": body / rng,
        "sig_upper_wick_frac": upper / rng,
        "sig_lower_wick_frac": lower / rng,
        "sig_close_pos": close_pos,
        "sig_bull": b.c > b.o,
    }


def trend_features(bars_pre: List[Bar], side: str) -> Dict[str, Any]:
    if len(bars_pre) < 16:
        return {"trend_ok": False}
    # use closes only, already closed
    c5 = bars_pre[-1].c - bars_pre[-6].c if len(bars_pre) >= 6 else None
    c15 = bars_pre[-1].c - bars_pre[-16].c if len(bars_pre) >= 16 else None
    # align: LONG wants uptrend; for flipped B short after up breakout etc — compare to trade side
    def align(delta: Optional[float]) -> Optional[str]:
        if delta is None:
            return None
        if side == "LONG":
            return "WITH" if delta > 0 else ("AGAINST" if delta < 0 else "FLAT")
        return "WITH" if delta < 0 else ("AGAINST" if delta > 0 else "FLAT")

    # acceleration against: last 3 closes monotonic against side
    last3 = bars_pre[-3:]
    against = False
    if len(last3) == 3:
        if side == "LONG":
            against = last3[0].c > last3[1].c > last3[2].c
        else:
            against = last3[0].c < last3[1].c < last3[2].c
    return {
        "trend_ok": True,
        "delta_5m": c5,
        "delta_15m": c15,
        "trend_5m_vs_side": align(c5),
        "trend_15m_vs_side": align(c15),
        "pre_fill_accel_against": against,
    }


def attach_pre_features(profile: str, trade: Trade, path_row: Dict[str, Any]) -> Dict[str, Any]:
    bars_pre = bars_around_signal(profile, trade.symbol, trade.signal_open_time_ms or 0, lookback=40)
    path_row.update(signal_bar_features(bars_pre))
    path_row.update(trend_features(bars_pre, trade.side))
    return path_row


def evaluate_candidates(path_rows: List[Dict[str, Any]], trades: List[Trade]) -> Dict[str, Any]:
    """At most 3 executable filters; post-hoc labels for outcome only."""
    by_id = {t.trade_id: t for t in trades}
    closed = [r for r in path_rows if r.get("actual_status") == "CLOSED" and r.get("actual_net") is not None]
    base_sum = sum(r["actual_net"] for r in closed)
    base_n = len(closed)
    # per original signal approx = per closed trade here (filled only subset)
    results = []

    def score(keep_ids, name, rule):
        kept = [r for r in closed if r["trade_id"] in keep_ids]
        dropped = [r for r in closed if r["trade_id"] not in keep_ids]
        ksum = sum(r["actual_net"] for r in kept) if kept else 0.0
        missed_wins = sum(r["actual_net"] for r in dropped if r["actual_net"] > 0)
        avoided_loss = -sum(r["actual_net"] for r in dropped if r["actual_net"] < 0)
        return {
            "candidate": name,
            "rule": rule,
            "n_keep": len(kept),
            "n_drop": len(dropped),
            "keep_rate": len(kept) / base_n if base_n else None,
            "sum_net_kept": ksum,
            "delta_vs_all": ksum - base_sum,
            "missed_win_sum": missed_wins,
            "avoided_loss_sum": avoided_loss,
            "avg_net_kept": (ksum / len(kept)) if kept else None,
            "avg_net_all": (base_sum / base_n) if base_n else None,
            "kept_ids": sorted(keep_ids),
            "dropped_ids": sorted(r["trade_id"] for r in dropped),
        }

    # C1: volatility band — keep if 1.0 <= offset/atr <= 3.5 (around observed median ~2.3)
    keep1 = {
        r["trade_id"]
        for r in closed
        if r.get("offset_over_atr") is not None and 1.0 <= float(r["offset_over_atr"]) <= 3.5
    }
    # also keep if atr missing? drop missing to be strict
    results.append(score(keep1, "C1_vol_band_1_to_3p5_atr", "keep fill iff 1.0<=0.5%dist/ATR<=3.5 (drop missing atr)"))

    # C2: max wait — keep if wait_min is None or <= 5
    keep2 = {
        r["trade_id"]
        for r in closed
        if r.get("wait_min") is None or float(r["wait_min"]) <= 5.0
    }
    results.append(score(keep2, "C2_max_wait_5m", "keep fill iff queue→fill wait <=5m (unknown wait kept)"))

    # C3: drop if pre_fill_accel_against AND trend_15m AGAINST
    keep3 = {
        r["trade_id"]
        for r in closed
        if not (
            r.get("pre_fill_accel_against") is True
            and r.get("trend_15m_vs_side") == "AGAINST"
        )
    }
    results.append(
        score(
            keep3,
            "C3_no_against_accel_and_15m",
            "drop if last3 closes accel against AND 15m trend against side",
        )
    )

    # diagnostics for research questions
    atr_vals = [float(r["offset_over_atr"]) for r in closed if r.get("offset_over_atr") is not None]
    atr_vals.sort()
    against_big = [
        r
        for r in closed
        if r.get("pre_fill_accel_against") and (r["actual_net"] or 0) < -40
    ]
    old_fill = [r for r in closed if r.get("wait_min") is not None and float(r["wait_min"]) > 5]
    old_sum = sum(r["actual_net"] for r in old_fill)
    young = [r for r in closed if r.get("wait_min") is not None and float(r["wait_min"]) <= 5]
    young_sum = sum(r["actual_net"] for r in young)

    return {
        "baseline_closed_n": base_n,
        "baseline_sum_net": base_sum,
        "atr_offset_n": len(atr_vals),
        "atr_offset_median": atr_vals[len(atr_vals) // 2] if atr_vals else None,
        "atr_offset_p10": atr_vals[max(0, len(atr_vals) // 10)] if atr_vals else None,
        "atr_offset_p90": atr_vals[min(len(atr_vals) - 1, (9 * len(atr_vals)) // 10)] if atr_vals else None,
        "big_loss_with_against_accel_n": len(against_big),
        "wait_gt5_n": len(old_fill),
        "wait_gt5_sum_net": old_sum,
        "wait_le5_n": len(young),
        "wait_le5_sum_net": young_sum,
        "candidates": results,
        "note": "Filled-trade subset only; not full signal funnel. Labels post-hoc.",
    }
