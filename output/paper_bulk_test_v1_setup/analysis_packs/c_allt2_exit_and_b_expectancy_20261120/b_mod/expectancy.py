"""B expectancy decomposition + 3 independent candidates (no stacking)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PREV = Path(__file__).resolve().parents[2] / "kline_path_and_pyramid300_20261120"
sys.path.insert(0, str(PREV))

from common.config import CUTOFF, EXT_END, FEE, ORIG_NOTIONAL, SLIP  # noqa: E402
from common.data import (  # noqa: E402
    Bar,
    Trade,
    exit_px_slip,
    fee_u,
    gross,
    load_bars,
    open_comp,
    parse_ts,
    qty_for_notional,
    round_tick,
)
from study_a.path_and_features import attach_pre_features, diagnose_path  # noqa: E402


def decompose_actual(closed: List[Trade]) -> Dict[str, Any]:
    rows = []
    for t in closed:
        c = t.close_row or {}
        open_fee = float(t.open_fee)
        close_fee = float(c.get("fee_u") or 0)
        close_gross = float(c.get("gross_u") or 0)
        close_net = float(c.get("net_u") or 0)
        complete_net = open_comp(t.open_row) + close_net
        # gross win without fees: close_gross > 0 roughly; also compare entry→exit raw
        raw_gross = gross(t.side, t.entry, float(t.exit_price or t.entry), t.qty)
        # TP target gross ~50, SL ~-75 after tick
        tp_raw = t.open_row.get("fixed_gross_tp_price_raw")
        sl_raw = t.open_row.get("fixed_gross_sl_price_raw")
        tp_overshoot = None
        sl_overshoot = None
        reason = t.exit_reason or ""
        if "TP" in reason and tp_raw is not None and t.exit_price is not None:
            # favorable overshoot in price space → gross vs target 50
            tp_overshoot = raw_gross - 50.0  # positive = more than 50 gross before fee
        if ("SL" in reason or "STOP" in reason) and sl_raw is not None and t.exit_price is not None:
            sl_overshoot = raw_gross - (-75.0)  # more negative = worse than -75
        rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "exit_reason": reason,
                "raw_gross": raw_gross,
                "close_gross_u": close_gross,
                "open_fee": open_fee,
                "close_fee": close_fee,
                "fees": open_fee + close_fee,
                "complete_net": complete_net,
                "gross_win": raw_gross > 0,
                "net_win": complete_net > 0,
                "tp_gross_vs_50": tp_overshoot,
                "sl_gross_vs_m75": sl_overshoot,
            }
        )
    wins_g = [r for r in rows if r["gross_win"]]
    loss_g = [r for r in rows if not r["gross_win"]]
    wins_n = [r for r in rows if r["net_win"]]
    loss_n = [r for r in rows if not r["net_win"]]
    avg_net_win = sum(r["complete_net"] for r in wins_n) / len(wins_n) if wins_n else None
    avg_net_loss = sum(r["complete_net"] for r in loss_n) / len(loss_n) if loss_n else None
    wr_g = len(wins_g) / len(rows) if rows else None
    wr_n = len(wins_n) / len(rows) if rows else None
    expectancy = None
    if wr_n is not None and avg_net_win is not None and avg_net_loss is not None:
        expectancy = wr_n * avg_net_win + (1 - wr_n) * avg_net_loss
    payoff = None
    if avg_net_win is not None and avg_net_loss is not None and avg_net_loss != 0:
        payoff = abs(avg_net_win / avg_net_loss)
    losses_sorted = sorted(loss_n, key=lambda r: r["complete_net"])
    top3 = losses_sorted[:3]
    top3_sum = sum(r["complete_net"] for r in top3)
    all_loss_sum = sum(r["complete_net"] for r in loss_n) if loss_n else 0.0
    return {
        "n": len(rows),
        "gross_win_rate": wr_g,
        "net_win_rate": wr_n,
        "avg_net_win": avg_net_win,
        "avg_net_loss": avg_net_loss,
        "payoff_abs_avgWin_over_avgLoss": payoff,
        "net_expectancy": expectancy,
        "sum_net": sum(r["complete_net"] for r in rows),
        "sum_fees": sum(r["fees"] for r in rows),
        "sum_raw_gross": sum(r["raw_gross"] for r in rows),
        "top3_loss_sum": top3_sum,
        "top3_loss_share_of_all_losses": (top3_sum / all_loss_sum) if all_loss_sum else None,
        "top3_ids": [r["trade_id"] for r in top3],
        "tp_overshoot_mean": _mean([r["tp_gross_vs_50"] for r in rows if r["tp_gross_vs_50"] is not None]),
        "sl_overshoot_mean": _mean([r["sl_gross_vs_m75"] for r in rows if r["sl_gross_vs_m75"] is not None]),
        "attribution_note": "無歷史 bid/ask 輪詢軌跡 → 不硬拆『路徑損失 vs 執行延遲』；僅報價格相對障礙超額（fills exit vs raw barrier）。",
        "rows": rows,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def ttl5_event_replay(rebuilt: dict, runs: set) -> Dict[str, Any]:
    """Full queue replay with corrected denominators.

    - closed-fill nets: only COMPLETE closed trades
    - do not treat open-at-cutoff as 0 in closed stats
    - per-signal stats: all queued; unfilled/expired/TTL-cancel → 0; open-at-cutoff filled → excluded from closed, listed separately
    """
    rows = rebuilt["all_rows"]
    queued = [
        r
        for r in rows
        if r.get("event") == "LIMIT_QUEUED"
        and r.get("run_id") in runs
        and (parse_ts(r, "recorded_at") or CUTOFF) <= CUTOFF
    ]
    filled = {r.get("signal_key"): r for r in rows if r.get("event") == "LIMIT_FILLED" and r.get("run_id") in runs}
    expired = {r.get("signal_key"): r for r in rows if r.get("event") == "LIMIT_EXPIRED" and r.get("run_id") in runs}
    by_sk = {t.signal_key: t for t in rebuilt["primary"] + rebuilt["oos"] if t.signal_key}

    events = []
    for q in queued:
        sk = q.get("signal_key")
        qa = parse_ts(q, "recorded_at")
        fl = filled.get(sk)
        ex = expired.get(sk)
        tr = by_sk.get(sk)
        fill_ts = parse_ts(fl, "recorded_at") if fl else None
        if fl and fill_ts and fill_ts <= CUTOFF:
            actual = "FILLED"
            wait = (fill_ts - qa).total_seconds() / 60.0 if qa else None
        elif ex and parse_ts(ex, "recorded_at") and parse_ts(ex, "recorded_at") <= CUTOFF:
            actual = "EXPIRED"
            wait = None
        else:
            actual = "UNFILLED_BY_CUTOFF"
            wait = None

        # open at cutoff?
        open_at_cutoff = bool(tr and tr.status == "OPEN")
        closed_net = tr.actual_net if (tr and tr.status == "CLOSED") else None

        if actual == "FILLED" and wait is not None and wait > 5:
            ttl = "TTL_CANCEL"
        elif actual == "FILLED":
            ttl = "FILLED_KEPT"
        elif actual == "EXPIRED":
            ttl = "EXPIRED"
        else:
            age = (CUTOFF - qa).total_seconds() / 60.0 if qa else None
            ttl = "TTL_CANCEL" if (age is not None and age > 5) else "STILL_PENDING"

        events.append(
            {
                "signal_key": sk,
                "symbol": q.get("symbol"),
                "queued_at": qa.isoformat() if qa else None,
                "actual": actual,
                "wait_min": wait,
                "ttl5": ttl,
                "open_at_cutoff": open_at_cutoff,
                "closed_complete_net": closed_net,
                "trade_id": tr.trade_id if tr else None,
            }
        )

    # --- closed-only (27? user said 27 closed / 28 incl open) ---
    closed_fills = [e for e in events if e["actual"] == "FILLED" and e["closed_complete_net"] is not None]
    open_fills = [e for e in events if e["actual"] == "FILLED" and e["open_at_cutoff"]]
    filled_all = [e for e in events if e["actual"] == "FILLED"]

    # TTL on closed fills
    ttl_cancel_closed = [e for e in closed_fills if e["ttl5"] == "TTL_CANCEL"]
    ttl_keep_closed = [e for e in closed_fills if e["ttl5"] == "FILLED_KEPT"]

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    # per-signal: all queued; open fills excluded from 'as_closed_0' variants
    per_sig_base = []
    per_sig_ttl = []
    for e in events:
        if e["closed_complete_net"] is not None:
            per_sig_base.append(e["closed_complete_net"])
            per_sig_ttl.append(e["closed_complete_net"] if e["ttl5"] == "FILLED_KEPT" else 0.0)
        elif e["open_at_cutoff"]:
            # exclude from both closed sums; listed separately
            continue
        else:
            per_sig_base.append(0.0)
            per_sig_ttl.append(0.0)

    # also per-signal including open as 'unknown' count
    return {
        "n_queued": len(events),
        "n_filled_all": len(filled_all),
        "n_filled_closed_complete": len(closed_fills),
        "n_filled_open_at_cutoff": len(open_fills),
        "口径说明": (
            f"已平成交={len(closed_fills)}；含未平成交={len(filled_all)} "
            f"(=closed {len(closed_fills)} + open {len(open_fills)})。"
            "未平不當作0，也不併入已平平均分母。"
        ),
        "baseline_closed_sum": sum(e["closed_complete_net"] for e in closed_fills),
        "baseline_closed_avg": avg([e["closed_complete_net"] for e in closed_fills]),
        "baseline_closed_n": len(closed_fills),
        "ttl5_keep_closed_sum": sum(e["closed_complete_net"] for e in ttl_keep_closed),
        "ttl5_keep_closed_avg": avg([e["closed_complete_net"] for e in ttl_keep_closed]),
        "ttl5_keep_closed_n": len(ttl_keep_closed),
        "ttl5_cancel_closed_n": len(ttl_cancel_closed),
        "ttl5_cancel_closed_sum": sum(e["closed_complete_net"] for e in ttl_cancel_closed),
        "ttl5_cancel_closed_avg": avg([e["closed_complete_net"] for e in ttl_cancel_closed]),
        "missed_wins_sum": sum(e["closed_complete_net"] for e in ttl_cancel_closed if e["closed_complete_net"] > 0),
        "avoided_loss_sum": -sum(e["closed_complete_net"] for e in ttl_cancel_closed if e["closed_complete_net"] < 0),
        "per_queued_signal_n_excl_open": len(per_sig_base),
        "per_queued_sum_baseline_excl_open": sum(per_sig_base),
        "per_queued_avg_baseline_excl_open": avg(per_sig_base),
        "per_queued_sum_ttl5_excl_open": sum(per_sig_ttl),
        "per_queued_avg_ttl5_excl_open": avg(per_sig_ttl),
        "open_fill_trade_ids": [e["trade_id"] for e in open_fills],
        "events": events,
    }


def c3_exact(rebuilt: dict, profile: str) -> Dict[str, Any]:
    """Exact boolean: DROP iff pre_fill_accel_against==True AND trend_15m_vs_side=='AGAINST'.

    Missing features → cannot DROP (keep). Features from closed bars at/before signal_open_time only.
    """
    closed = [t for t in rebuilt["primary"] if t.status == "CLOSED"]
    rows = []
    for t in closed:
        bars = load_bars(profile, t.symbol, t.open_ts - timedelta(minutes=2), CUTOFF)
        row = diagnose_path(t, bars, cutoff=CUTOFF)
        row = attach_pre_features(profile, t, row)
        accel = row.get("pre_fill_accel_against")
        trend = row.get("trend_15m_vs_side")
        feat_ok = bool(row.get("trend_ok"))
        # exact
        drop = (accel is True) and (trend == "AGAINST")
        # why keep when trend all AGAINST: need BOTH; keep if accel is False
        rows.append(
            {
                **{k: row.get(k) for k in (
                    "trade_id", "symbol", "actual_net", "pre_fill_accel_against",
                    "trend_15m_vs_side", "trend_5m_vs_side", "feat_ok", "signal_ref", "atr",
                )},
                "signal_open_time_ms": t.signal_open_time_ms,
                "feature_asof": "bars with open_time <= signal_open_time (closed-bar set via vault)",
                "missing_trend": not feat_ok,
                "C3_DROP": drop,
                "C3_KEEP": not drop,
                "why_keep_if_trend15_against": (
                    None
                    if trend != "AGAINST"
                    else ("accel_false_or_missing" if accel is not True else "should_drop")
                ),
            }
        )
    keep = [r for r in rows if r["C3_KEEP"]]
    drop = [r for r in rows if r["C3_DROP"]]
    trend_all_against = all(r.get("trend_15m_vs_side") == "AGAINST" for r in rows)
    return {
        "formula": "DROP = (pre_fill_accel_against is True) AND (trend_15m_vs_side == 'AGAINST'); else KEEP. missing→KEEP.",
        "n": len(rows),
        "n_keep": len(keep),
        "n_drop": len(drop),
        "trend15_against_n": sum(1 for r in rows if r.get("trend_15m_vs_side") == "AGAINST"),
        "accel_true_n": sum(1 for r in rows if r.get("pre_fill_accel_against") is True),
        "both_n": len(drop),
        "trend15_all_against": trend_all_against,
        "why_8_kept": (
            "trend15_against 可為全 True，但 DROP 還要求 accel_against==True；"
            f"accel_true={sum(1 for r in rows if r.get('pre_fill_accel_against') is True)}，"
            f"故 KEEP={len(keep)}（含 accel 為 False 的筆）。"
        ),
        "sum_net_all": sum(float(r["actual_net"]) for r in rows),
        "sum_net_keep": sum(float(r["actual_net"]) for r in keep),
        "sum_net_drop": sum(float(r["actual_net"]) for r in drop),
        "avg_net_all": sum(float(r["actual_net"]) for r in rows) / len(rows),
        "avg_net_keep": sum(float(r["actual_net"]) for r in keep) / len(keep) if keep else None,
        "missed_wins": sum(float(r["actual_net"]) for r in drop if float(r["actual_net"]) > 0),
        "avoided_losses": -sum(float(r["actual_net"]) for r in drop if float(r["actual_net"]) < 0),
        "rows": rows,
        "oos": _c3_oos(rebuilt, profile),
    }


def _c3_oos(rebuilt, profile):
    out = []
    for t in rebuilt["oos"]:
        bars = load_bars(profile, t.symbol, t.open_ts - timedelta(minutes=2), EXT_END)
        row = diagnose_path(t, bars, cutoff=EXT_END)
        row = attach_pre_features(profile, t, row)
        drop = (row.get("pre_fill_accel_against") is True) and (row.get("trend_15m_vs_side") == "AGAINST")
        net = None
        if t.close_row:
            net = open_comp(t.open_row) + float(t.close_row.get("net_u") or 0)
        out.append(
            {
                "trade_id": t.trade_id,
                "C3_DROP": drop,
                "net": net,
                "exit": (t.close_row or {}).get("exit_reason"),
                "accel": row.get("pre_fill_accel_against"),
                "trend15": row.get("trend_15m_vs_side"),
                "status_note": "未平則 net=None；不足樣外",
            }
        )
    return out


def partial_tp_70_30(
    trade: Trade,
    bars: List[Bar],
    *,
    cutoff: datetime = CUTOFF,
    mode: str = "conservative",
) -> Dict[str, Any]:
    """Predefined: 70% flat at original TP50 price; 30% target TP100 price; after partial, stop→entry.

    Before first TP: SL75 unchanged. Entry stop ≠ fee-breakeven.
    """
    side = trade.side
    entry = trade.entry
    qty = trade.qty
    tick = trade.tick
    tp50 = trade.open_row.get("fixed_gross_tp_price_raw")
    sl75 = trade.open_row.get("fixed_gross_sl_price_raw")
    if tp50 is None:
        d = 50.0 / qty
        tp50 = round_tick(entry + d if side == "LONG" else entry - d, tick)
    else:
        tp50 = float(tp50)
    if sl75 is None:
        d = 75.0 / qty
        sl75 = round_tick(entry - d if side == "LONG" else entry + d, tick)
    else:
        sl75 = float(sl75)
    # TP100 price from same qty geometry
    d100 = 100.0 / qty
    tp100 = round_tick(entry + d100 if side == "LONG" else entry - d100, tick)

    q1 = qty * 0.70
    q2 = qty - q1
    open_fee_total = float(trade.open_fee)
    # allocate open fee pro-rata
    fee1_open = open_fee_total * (q1 / qty)
    fee2_open = open_fee_total * (q2 / qty)

    partial_done = False
    rem_stop = sl75
    realized = 0.0
    fees = open_fee_total
    q_rem = qty
    last = entry
    entry_bar = trade.open_ts.replace(second=0, microsecond=0)
    exit_reason = None
    hold = None
    ambiguous = False

    if not bars:
        return {"trade_id": trade.trade_id, "status": "INSUFFICIENT", "total_mark_net": 0.0}

    for bar in bars:
        if bar.ot + timedelta(minutes=1) <= trade.open_ts:
            continue
        if bar.ot >= cutoff:
            break
        last = bar.c
        is_entry = bar.ot == entry_bar
        if is_entry:
            continue

        hit_sl = (side == "LONG" and bar.l <= rem_stop) or (side == "SHORT" and bar.h >= rem_stop)
        hit_tp50 = (not partial_done) and ((side == "LONG" and bar.h >= tp50) or (side == "SHORT" and bar.l <= tp50))
        hit_tp100 = partial_done and ((side == "LONG" and bar.h >= tp100) or (side == "SHORT" and bar.l <= tp100))

        if hit_sl and (hit_tp50 or hit_tp100):
            ambiguous = True
            if mode == "optimistic":
                hit_sl = False  # prefer TP path
            # else conservative: SL wins below

        if hit_sl:
            px = exit_px_slip(side, rem_stop, SLIP)
            g = gross(side, entry, px, q_rem)
            cf = fee_u(px, q_rem, FEE)
            rem_open_fee = (fee1_open + fee2_open) if not partial_done else fee2_open
            realized += -rem_open_fee + (g - cf)
            fees += cf
            exit_reason = "SL_BE" if partial_done else "SL75"
            q_rem = 0
            hold = (bar.ot - trade.open_ts).total_seconds() / 60.0
            break

        if hit_tp50 and not partial_done:
            px = exit_px_slip(side, tp50, SLIP)
            g = gross(side, entry, px, q1)
            cf = fee_u(px, q1, FEE)
            realized += -fee1_open + (g - cf)
            fees += cf
            partial_done = True
            q_rem = q2
            rem_stop = entry  # move to entry (not fee BE)
            continue

        if hit_tp100 and partial_done:
            px = exit_px_slip(side, tp100, SLIP)
            g = gross(side, entry, px, q2)
            cf = fee_u(px, q2, FEE)
            realized += -fee2_open + (g - cf)
            fees += cf
            exit_reason = "TP100_REST"
            q_rem = 0
            hold = (bar.ot - trade.open_ts).total_seconds() / 60.0
            break

    if q_rem > 0:
        rem_open_fee = (fee1_open + fee2_open) if not partial_done else fee2_open
        px = exit_px_slip(side, last, SLIP)
        g = gross(side, entry, px, q_rem)
        cf = fee_u(px, q_rem, FEE)
        mtm = -rem_open_fee + (g - cf)
        status = "OPEN"
        total = realized + mtm
        hold = (cutoff - trade.open_ts).total_seconds() / 60.0
    else:
        mtm = 0.0
        status = "COMPLETE"
        total = realized

    # baseline complete net for delta
    baseline = trade.actual_net
    return {
        "trade_id": trade.trade_id,
        "status": status,
        "realized_net": realized,
        "open_mtm_net": mtm if status == "OPEN" else 0.0,
        "total_mark_net": total,
        "fees_exit_extra": fees - open_fee_total,
        "exit_reason": exit_reason,
        "partial_done": partial_done,
        "ambiguous": ambiguous,
        "hold_min": hold,
        "baseline_net": baseline,
        "delta_vs_baseline": (total - baseline) if baseline is not None and status == "COMPLETE" else None,
        "note": "stop@entry after partial ≠ fee breakeven; SL75 until first TP",
    }
