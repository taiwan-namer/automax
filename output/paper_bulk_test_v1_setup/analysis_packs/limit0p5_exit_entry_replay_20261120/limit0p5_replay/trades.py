from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .constants import CUTOFF, TAIPEI


def parse_ts(obj: Any, *keys: str) -> Optional[datetime]:
    for k in keys:
        v = obj.get(k) if isinstance(obj, dict) else None
        if v is None:
            continue
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI)
        return dt.astimezone(TAIPEI)
    return None


def open_component_net(open_row: dict) -> float:
    if "realized_net_u" in open_row and open_row["realized_net_u"] is not None:
        return float(open_row["realized_net_u"])
    return -float(open_row.get("fee_u") or 0.0)


def round_tick(price: float, tick: float) -> float:
    if tick is None or tick <= 0:
        return float(price)
    # banker's-avoid: match common floor-to-nearest via round half away
    n = round(price / tick)
    return float(n * tick)


@dataclass
class TradeSample:
    trade_id: str
    symbol: str
    side: str
    entry_price: float
    qty: float
    open_fee_u: float
    open_ts: datetime
    open_run_id: str
    signal_ref_price: Optional[float]
    limit_price: Optional[float]
    limit_offset_pct: Optional[float]
    atr: Optional[float]
    direction_flip: Optional[bool]
    orig_side: Optional[str]
    tick_size: float
    step_size: float
    open_row: dict
    close_row: Optional[dict] = None
    close_ts: Optional[datetime] = None
    close_after_cutoff: bool = False
    status_at_cutoff: str = "OPEN"  # CLOSED | OPEN
    actual_complete_net: Optional[float] = None
    actual_exit_reason: Optional[str] = None
    actual_exit_price: Optional[float] = None
    actual_tp: Optional[float] = None
    actual_sl: Optional[float] = None

    @property
    def notional_u(self) -> float:
        return abs(self.entry_price * self.qty)


def load_filters(account_path: Path) -> Dict[str, dict]:
    if not account_path.exists():
        return {}
    acc = json.loads(account_path.read_text())
    return dict(acc.get("filters") or {})


def load_fills(fills_path: Path) -> List[dict]:
    rows: List[dict] = []
    for line in fills_path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def rebuild_trades(
    fills_path: Path,
    account_path: Path,
    runs: Set[str],
    *,
    cutoff: datetime = CUTOFF,
) -> Dict[str, Any]:
    rows = load_fills(fills_path)
    filters = load_filters(account_path)

    opens: Dict[str, dict] = {}
    closes: Dict[str, dict] = {}
    for r in rows:
        ev = r.get("event")
        tid = r.get("trade_id")
        if ev == "OPEN" and r.get("run_id") in runs and tid:
            opens[tid] = r
        elif ev == "CLOSE" and tid:
            # keep latest close
            closes[tid] = r

    samples: List[TradeSample] = []
    for tid, o in opens.items():
        ots = parse_ts(o, "ts", "recorded_at", "filled_at")
        if ots is None or ots > cutoff:
            continue
        sym = str(o["symbol"])
        f = filters.get(sym) or {}
        tick = float(f.get("tickSize") or 0.0)
        step = float(f.get("stepSize") or 0.0)
        c = closes.get(tid)
        cts = parse_ts(c, "ts", "recorded_at", "filled_at") if c else None
        closed_ok = bool(c and cts is not None and cts <= cutoff)
        close_after = bool(c and cts is not None and cts > cutoff)
        open_comp = open_component_net(o)
        actual_net = None
        if closed_ok:
            actual_net = open_comp + float(c.get("net_u") or 0.0)
        samples.append(
            TradeSample(
                trade_id=tid,
                symbol=sym,
                side=str(o["side"]),
                entry_price=float(o["price"]),
                qty=float(o["qty"]),
                open_fee_u=float(o.get("fee_u") or 0.0),
                open_ts=ots,
                open_run_id=str(o.get("run_id")),
                signal_ref_price=(
                    float(o["signal_ref_price"]) if o.get("signal_ref_price") is not None else None
                ),
                limit_price=float(o["limit_price"]) if o.get("limit_price") is not None else None,
                limit_offset_pct=(
                    float(o["limit_offset_pct"]) if o.get("limit_offset_pct") is not None else None
                ),
                atr=float(o["atr"]) if o.get("atr") is not None else None,
                direction_flip=o.get("direction_flip"),
                orig_side=o.get("orig_side"),
                tick_size=tick,
                step_size=step,
                open_row=o,
                close_row=c if (closed_ok or close_after) else None,
                close_ts=cts,
                close_after_cutoff=close_after,
                status_at_cutoff="CLOSED" if closed_ok else "OPEN",
                actual_complete_net=actual_net,
                actual_exit_reason=(c or {}).get("exit_reason") if c else None,
                actual_exit_price=float(c["price"]) if c and c.get("price") is not None else None,
                actual_tp=(
                    float(o["fixed_gross_tp_price_raw"])
                    if o.get("fixed_gross_tp_price_raw") is not None
                    else None
                ),
                actual_sl=(
                    float(o["fixed_gross_sl_price_raw"])
                    if o.get("fixed_gross_sl_price_raw") is not None
                    else None
                ),
            )
        )

    closed = [s for s in samples if s.status_at_cutoff == "CLOSED"]
    opened = [s for s in samples if s.status_at_cutoff == "OPEN"]
    sum_net = sum(float(s.actual_complete_net or 0.0) for s in closed)

    # queue/expire cohort (same runs, queued by cutoff)
    queued = [
        r
        for r in rows
        if r.get("event") == "LIMIT_QUEUED"
        and r.get("run_id") in runs
        and (parse_ts(r, "recorded_at", "ts") or cutoff) <= cutoff
    ]
    expired = [
        r
        for r in rows
        if r.get("event") == "LIMIT_EXPIRED"
        and r.get("run_id") in runs
        and (parse_ts(r, "recorded_at", "ts") or cutoff) <= cutoff
    ]
    filled_events = [
        r
        for r in rows
        if r.get("event") == "LIMIT_FILLED"
        and r.get("run_id") in runs
        and (parse_ts(r, "recorded_at", "ts") or cutoff) <= cutoff
    ]

    return {
        "samples": samples,
        "closed": closed,
        "open": opened,
        "sum_complete_net": sum_net,
        "n_closed": len(closed),
        "n_open": len(opened),
        "queued": queued,
        "expired": expired,
        "limit_filled": filled_events,
        "filters": filters,
        "all_rows": rows,
    }


def barriers(entry: float, qty: float, side: str, tp_u: float, sl_u: float, tick: float) -> Dict[str, float]:
    d_tp = tp_u / qty
    d_sl = sl_u / qty
    if side == "LONG":
        tp_raw, sl_raw = entry + d_tp, entry - d_sl
    else:
        tp_raw, sl_raw = entry - d_tp, entry + d_sl
    tp = round_tick(tp_raw, tick)
    sl = round_tick(sl_raw, tick)
    if side == "LONG":
        gross_tp = (tp - entry) * qty
        gross_sl = (sl - entry) * qty
    else:
        gross_tp = (entry - tp) * qty
        gross_sl = (entry - sl) * qty
    return {
        "tp": tp,
        "sl": sl,
        "tp_raw": tp_raw,
        "sl_raw": sl_raw,
        "gross_tp_actual": gross_tp,
        "gross_sl_actual": gross_sl,
    }
