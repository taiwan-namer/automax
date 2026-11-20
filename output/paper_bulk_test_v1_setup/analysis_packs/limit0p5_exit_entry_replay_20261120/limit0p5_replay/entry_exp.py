from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .constants import CUTOFF, FEE_RATE_ONE_SIDE, SLIPPAGE_ADVERSE
from .exit_replay import fee_u, gross_pnl, replay_one
from .klines import load_bars, mid_of
from .trades import parse_ts, round_tick


def _adverse_mid_cancel(side: str, ref: float, mid: float, adverse_pct: float) -> bool:
    if ref <= 0:
        return False
    if side == "LONG":
        return mid >= ref * (1.0 + adverse_pct)
    return mid <= ref * (1.0 - adverse_pct)


def simulate_limit_path(
    bars,
    *,
    side: str,
    ref: float,
    offset_pct: float,
    adverse_pct: float,
    queued_at: datetime,
    tick: float,
    cutoff: datetime,
) -> Dict[str, Any]:
    if side == "LONG":
        limit_px = round_tick(ref * (1.0 - offset_pct), tick)
    else:
        limit_px = round_tick(ref * (1.0 + offset_pct), tick)

    out = {
        "offset_pct": offset_pct,
        "limit_price": limit_px,
        "result": "NO_FILL",  # FILLED | CANCEL_ADVERSE | NO_FILL
        "fill_ts": None,
        "fill_price": None,
        "wait_min": None,
        "cancel_ts": None,
        "note": "kline_approx",
    }
    entry_bar = queued_at.replace(second=0, microsecond=0)
    for bar in bars:
        if bar.open_time + timedelta(minutes=1) <= queued_at:
            continue
        if bar.open_time >= cutoff:
            break
        mid = mid_of(bar)
        # cancel check first on mid (approx); same-bar fill vs cancel → conservative: cancel if adverse mid and also touch
        canceled = _adverse_mid_cancel(side, ref, mid, adverse_pct)
        if side == "LONG":
            touched = bar.low <= limit_px
        else:
            touched = bar.high >= limit_px

        if bar.open_time == entry_bar:
            # ambiguous if both; conservative: if cancel mid and touch → CANCEL
            if canceled and touched:
                out.update(result="CANCEL_ADVERSE", cancel_ts=bar.open_time.isoformat(), note="entry_bar_both_conservative_cancel")
                return out
            if canceled:
                out.update(result="CANCEL_ADVERSE", cancel_ts=bar.open_time.isoformat())
                return out
            if touched:
                # cannot guarantee fill order; mark optimistic fill only if close beyond
                close_ok = (side == "LONG" and bar.close <= limit_px) or (
                    side == "SHORT" and bar.close >= limit_px
                )
                if close_ok:
                    out.update(
                        result="FILLED",
                        fill_ts=(bar.open_time + timedelta(seconds=59)).isoformat(),
                        fill_price=limit_px,
                        wait_min=((bar.open_time + timedelta(seconds=59)) - queued_at).total_seconds() / 60.0,
                        note="entry_bar_close_confirm",
                    )
                    return out
                out["note"] = "entry_bar_touch_unconfirmed"
                continue

        if canceled and touched:
            out.update(result="CANCEL_ADVERSE", cancel_ts=bar.open_time.isoformat(), note="same_bar_cancel_priority")
            return out
        if canceled:
            out.update(result="CANCEL_ADVERSE", cancel_ts=bar.open_time.isoformat())
            return out
        if touched:
            out.update(
                result="FILLED",
                fill_ts=(bar.open_time + timedelta(seconds=30)).isoformat(),
                fill_price=limit_px,
                wait_min=((bar.open_time + timedelta(seconds=30)) - queued_at).total_seconds() / 60.0,
            )
            return out
    return out


def market_entry_price(side: str, ref: float) -> float:
    # approximate immediate fill at mid with adverse slip (no bid/ask)
    if side == "LONG":
        return ref * (1.0 + SLIPPAGE_ADVERSE)
    return ref * (1.0 - SLIPPAGE_ADVERSE)


def run_entry_experiments(
    setup_root: Path,
    profile_id: str,
    *,
    queued: List[dict],
    filters: Dict[str, dict],
    runs: Set[str],
    cutoff: datetime = CUTOFF,
    offsets: tuple = (0.0, 0.0025, 0.005, 0.0075),
    adverse_pct: float = 0.01,
    tp_u: float = 50.0,
    sl_u: float = 75.0,
) -> Dict[str, Any]:
    """Exploratory entry-distance experiment on LIMIT_QUEUED cohort.

    offsets 0.0 = counterfactual immediate market at signal_ref (with adverse slip).
    Not a full account backtest; coverage = queued signals in limit0p5 runs.
    """
    if not queued:
        return {"coverage": 0, "rows": [], "summary": []}

    earliest = min(parse_ts(q, "recorded_at", "ts") or cutoff for q in queued)
    symbols = sorted({str(q["symbol"]) for q in queued})
    bars_by = {
        sym: load_bars(setup_root, profile_id, sym, start=earliest - timedelta(minutes=2), end=cutoff)
        for sym in symbols
    }

    rows: List[Dict[str, Any]] = []
    for q in queued:
        sym = str(q["symbol"])
        side = str(q["side"])
        ref = float(q.get("signal_ref_price") or 0.0)
        queued_at = parse_ts(q, "recorded_at", "ts")
        if queued_at is None or ref <= 0:
            continue
        tick = float((filters.get(sym) or {}).get("tickSize") or 0.0)
        atr = None  # ATR at queue not always on LIMIT_QUEUED; try open later
        # qty at queue is recorded
        qty_queued = float(q.get("qty") or 0.0)
        actual_offset = float(q.get("limit_offset_pct") or 0.005)
        actual_limit = float(q.get("limit_price") or 0.0)
        row = {
            "symbol": sym,
            "side": side,
            "queued_at": queued_at.isoformat(),
            "signal_ref_price": ref,
            "signal_key": q.get("signal_key"),
            "run_id": q.get("run_id"),
            "actual_offset_pct": actual_offset,
            "actual_limit_price": actual_limit,
            "qty_at_queue": qty_queued,
        }
        # ATR multiples of 0.5% distance — need ATR from somewhere; leave null if absent
        row["offset_0p5_over_atr"] = None
        for off in offsets:
            key = f"off_{off}"
            if off == 0.0:
                # market CF
                px = market_entry_price(side, ref)
                # size at market price
                step = float((filters.get(sym) or {}).get("stepSize") or 0.001) or 0.001
                qty = round(10000.0 / px / step) * step if px > 0 else 0.0
                from .trades import TradeSample
                from zoneinfo import ZoneInfo

                sample = TradeSample(
                    trade_id=f"CFMKT:{q.get('signal_key')}",
                    symbol=sym,
                    side=side,
                    entry_price=px,
                    qty=qty,
                    open_fee_u=fee_u(px, qty),
                    open_ts=queued_at,
                    open_run_id=str(q.get("run_id")),
                    signal_ref_price=ref,
                    limit_price=None,
                    limit_offset_pct=0.0,
                    atr=None,
                    direction_flip=None,
                    orig_side=None,
                    tick_size=tick,
                    step_size=step,
                    open_row={},
                )
                pe = replay_one(sample, bars_by.get(sym) or [], tp_u, sl_u, cutoff=cutoff, mode="conservative")
                row[f"{key}_result"] = "FILLED"
                row[f"{key}_fill_price"] = px
                row[f"{key}_wait_min"] = 0.0
                row[f"{key}_exit_status"] = pe.status
                row[f"{key}_realized_or_mtm"] = pe.realized_net_u if pe.realized_net_u is not None else pe.mtm_net_u
            else:
                sim = simulate_limit_path(
                    bars_by.get(sym) or [],
                    side=side,
                    ref=ref,
                    offset_pct=off,
                    adverse_pct=adverse_pct,
                    queued_at=queued_at,
                    tick=tick,
                    cutoff=cutoff,
                )
                row[f"{key}_result"] = sim["result"]
                row[f"{key}_fill_price"] = sim["fill_price"]
                row[f"{key}_wait_min"] = sim["wait_min"]
                row[f"{key}_note"] = sim["note"]
                if sim["result"] == "FILLED" and sim["fill_price"] and sim["fill_ts"]:
                    step = float((filters.get(sym) or {}).get("stepSize") or 0.001) or 0.001
                    px = float(sim["fill_price"])
                    qty = round(10000.0 / px / step) * step
                    from .trades import TradeSample

                    fill_ts = datetime.fromisoformat(sim["fill_ts"])
                    sample = TradeSample(
                        trade_id=f"CFLIM:{off}:{q.get('signal_key')}",
                        symbol=sym,
                        side=side,
                        entry_price=px,
                        qty=qty,
                        open_fee_u=fee_u(px, qty),
                        open_ts=fill_ts,
                        open_run_id=str(q.get("run_id")),
                        signal_ref_price=ref,
                        limit_price=px,
                        limit_offset_pct=off,
                        atr=None,
                        direction_flip=None,
                        orig_side=None,
                        tick_size=tick,
                        step_size=step,
                        open_row={},
                    )
                    pe = replay_one(sample, bars_by.get(sym) or [], tp_u, sl_u, cutoff=cutoff, mode="conservative")
                    row[f"{key}_exit_status"] = pe.status
                    row[f"{key}_realized_or_mtm"] = (
                        pe.realized_net_u if pe.realized_net_u is not None else pe.mtm_net_u
                    )
                else:
                    row[f"{key}_exit_status"] = sim["result"]
                    row[f"{key}_realized_or_mtm"] = 0.0  # no position
        rows.append(row)

    # attach ATR ratio using first OPEN with same signal_key if available — skipped (coverage note)
    summary = []
    for off in offsets:
        key = f"off_{off}"
        n = len(rows)
        filled = [r for r in rows if r.get(f"{key}_result") == "FILLED"]
        canceled = [r for r in rows if r.get(f"{key}_result") == "CANCEL_ADVERSE"]
        nofill = [r for r in rows if r.get(f"{key}_result") == "NO_FILL"]
        nets = [r.get(f"{key}_realized_or_mtm") for r in rows if r.get(f"{key}_realized_or_mtm") is not None]
        waits = [r.get(f"{key}_wait_min") for r in filled if r.get(f"{key}_wait_min") is not None]
        summary.append(
            {
                "offset_pct": off,
                "n_signals": n,
                "n_filled": len(filled),
                "n_canceled": len(canceled),
                "n_no_fill": len(nofill),
                "fill_rate": len(filled) / n if n else None,
                "sum_net_per_signal": sum(nets) if nets else 0.0,
                "avg_net_per_signal": (sum(nets) / n) if n and nets else None,
                "avg_wait_min_filled": (sum(waits) / len(waits)) if waits else None,
            }
        )

    # wait-time buckets on actual 0.5% path (offset 0.005)
    buckets = {"le_1m": [], "1_to_5m": [], "gt_5m": []}
    for r in rows:
        w = r.get("off_0.005_wait_min")
        net = r.get("off_0.005_realized_or_mtm")
        if r.get("off_0.005_result") != "FILLED" or w is None:
            continue
        if w <= 1:
            buckets["le_1m"].append(net)
        elif w <= 5:
            buckets["1_to_5m"].append(net)
        else:
            buckets["gt_5m"].append(net)
    wait_diag = {
        k: {
            "n": len(v),
            "sum_net": sum(x or 0 for x in v),
            "avg_net": (sum(x or 0 for x in v) / len(v)) if v else None,
        }
        for k, v in buckets.items()
    }

    return {
        "coverage": len(rows),
        "n_queued_input": len(queued),
        "rows": rows,
        "summary": summary,
        "wait_diag_offset_0p5": wait_diag,
        "assumptions": {
            "model": "1m_ohlc_limit_touch_approx",
            "fee_rate_one_side": FEE_RATE_ONE_SIDE,
            "slippage_adverse_market_only": SLIPPAGE_ADVERSE,
            "limit_fill_price": "limit_price_no_extra_slip",
            "cancel": "mid=(h+l)/2 vs signal_ref adverse 1%",
            "notional": 10000.0,
            "exit": "fixed_gross_tp50_sl75_conservative_ohlc",
            "selection": "LIMIT_QUEUED cohort in limit0p5 runs only — exploratory, not full strategy backtest",
        },
    }
