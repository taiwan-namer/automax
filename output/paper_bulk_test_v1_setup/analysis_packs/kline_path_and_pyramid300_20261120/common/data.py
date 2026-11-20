from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from .config import CUTOFF, SETUP_ROOT, TAIPEI, account_dir, vault_dir


def parse_ts(obj: dict, *keys: str) -> Optional[datetime]:
    for k in keys:
        v = obj.get(k)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI)
        return dt.astimezone(TAIPEI)
    return None


def open_comp(o: dict) -> float:
    if "realized_net_u" in o and o["realized_net_u"] is not None:
        return float(o["realized_net_u"])
    return -float(o.get("fee_u") or 0.0)


@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str
    entry: float
    qty: float
    open_fee: float
    open_ts: datetime
    run_id: str
    signal_ref: Optional[float]
    limit_price: Optional[float]
    atr: Optional[float]
    signal_open_time_ms: Optional[int]
    signal_key: Optional[str]
    direction_flip: Optional[bool]
    tick: float
    step: float
    status: str  # CLOSED|OPEN at primary cutoff
    actual_net: Optional[float]
    exit_reason: Optional[str]
    exit_price: Optional[float]
    close_ts: Optional[datetime]
    close_after_cutoff: bool
    cohort: str  # primary|oos_extension
    open_row: dict
    close_row: Optional[dict]


@dataclass
class Bar:
    ot: datetime
    o: float
    h: float
    l: float
    c: float


def load_filters(profile: str) -> Dict[str, dict]:
    p = account_dir(profile) / "account_state.json"
    if not p.exists():
        return {}
    return dict(json.loads(p.read_text()).get("filters") or {})


def load_fills(profile: str) -> List[dict]:
    p = account_dir(profile) / "fills.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def rebuild_trades(profile: str, runs: Set[str], *, cutoff: datetime = CUTOFF) -> Dict[str, Any]:
    rows = load_fills(profile)
    filters = load_filters(profile)
    opens: Dict[str, dict] = {}
    closes: Dict[str, dict] = {}
    queued: List[dict] = []
    for r in rows:
        ev = r.get("event")
        tid = r.get("trade_id")
        if ev == "OPEN" and r.get("run_id") in runs and tid:
            opens[tid] = r
        elif ev == "CLOSE" and tid:
            closes[tid] = r
        elif ev == "LIMIT_QUEUED" and r.get("run_id") in runs:
            queued.append(r)

    trades: List[Trade] = []
    for tid, o in opens.items():
        ots = parse_ts(o, "recorded_at", "ts")
        if ots is None:
            continue
        cohort = "primary" if ots <= cutoff else "oos_extension"
        # primary selection: open <= cutoff; oos kept separate
        if cohort == "primary" or True:
            pass
        c = closes.get(tid)
        cts = parse_ts(c, "recorded_at", "ts") if c else None
        closed_ok = bool(c and cts and cts <= cutoff)
        # for oos opens, status vs cutoff is OPEN at primary; track separately
        if cohort == "oos_extension":
            status = "OOS"
            actual_net = None
            if c and cts:
                actual_net = open_comp(o) + float(c.get("net_u") or 0)
        elif closed_ok:
            status = "CLOSED"
            actual_net = open_comp(o) + float(c.get("net_u") or 0)
        else:
            status = "OPEN"
            actual_net = None
        sym = str(o["symbol"])
        f = filters.get(sym) or {}
        trades.append(
            Trade(
                trade_id=tid,
                symbol=sym,
                side=str(o["side"]),
                entry=float(o["price"]),
                qty=float(o["qty"]),
                open_fee=float(o.get("fee_u") or 0),
                open_ts=ots,
                run_id=str(o["run_id"]),
                signal_ref=float(o["signal_ref_price"]) if o.get("signal_ref_price") is not None else None,
                limit_price=float(o["limit_price"]) if o.get("limit_price") is not None else None,
                atr=float(o["atr"]) if o.get("atr") is not None else None,
                signal_open_time_ms=int(o["signal_open_time"]) if o.get("signal_open_time") is not None else None,
                signal_key=o.get("signal_key"),
                direction_flip=o.get("direction_flip"),
                tick=float(f.get("tickSize") or 0),
                step=float(f.get("stepSize") or 0.001) or 0.001,
                status=status,
                actual_net=actual_net,
                exit_reason=(c or {}).get("exit_reason"),
                exit_price=float(c["price"]) if c and c.get("price") is not None else None,
                close_ts=cts,
                close_after_cutoff=bool(c and cts and cts > cutoff),
                cohort=cohort,
                open_row=o,
                close_row=c,
            )
        )

    primary = [t for t in trades if t.cohort == "primary"]
    closed = [t for t in primary if t.status == "CLOSED"]
    opened = [t for t in primary if t.status == "OPEN"]
    oos = [t for t in trades if t.cohort == "oos_extension"]
    queued_p = [q for q in queued if (parse_ts(q, "recorded_at", "ts") or cutoff) <= cutoff]
    return {
        "trades": trades,
        "primary": primary,
        "closed": closed,
        "open": opened,
        "oos": oos,
        "sum_net": sum(t.actual_net or 0 for t in closed),
        "queued": queued_p,
        "filters": filters,
        "all_rows": rows,
    }


@lru_cache(maxsize=512)
def _csv(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if df.empty:
        return df
    df = df.copy()
    df["ot"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(str(TAIPEI))
    return df


def load_bars(profile: str, symbol: str, start: datetime, end: datetime) -> List[Bar]:
    path = vault_dir(profile) / f"{symbol}_1m.csv"
    df = _csv(str(path))
    if df.empty:
        return []
    out: List[Bar] = []
    for row in df.itertuples(index=False):
        ot = row.ot.to_pydatetime()
        if ot + timedelta(minutes=1) <= start:
            continue
        if ot >= end:
            break
        out.append(Bar(ot=ot, o=float(row.open), h=float(row.high), l=float(row.low), c=float(row.close)))
    return out


def bars_around_signal(profile: str, symbol: str, signal_ms: int, lookback: int = 40) -> List[Bar]:
    """Closed bars with open_time <= signal bar, for pre-trade features only."""
    path = vault_dir(profile) / f"{symbol}_1m.csv"
    df = _csv(str(path))
    if df.empty or signal_ms is None:
        return []
    sig_ot = datetime.fromtimestamp(signal_ms / 1000.0, tz=TAIPEI)
    # bars fully closed before or at signal open_time (signal bar itself may be the breakout bar)
    sub = df[df["ot"] <= pd.Timestamp(sig_ot)].tail(lookback)
    out = []
    for row in sub.itertuples(index=False):
        out.append(Bar(ot=row.ot.to_pydatetime(), o=float(row.open), h=float(row.high), l=float(row.low), c=float(row.close)))
    return out


def round_tick(px: float, tick: float) -> float:
    if not tick or tick <= 0:
        return float(px)
    return float(round(px / tick) * tick)


def qty_for_notional(notional: float, price: float, step: float) -> float:
    if price <= 0:
        return 0.0
    step = step or 0.001
    return float(round(notional / price / step) * step)


def fee_u(px: float, qty: float, rate: float | None = None) -> float:
    from .config import FEE as F

    return abs(px * qty) * (F if rate is None else rate)


def gross(side: str, entry: float, exit_px: float, qty: float) -> float:
    if side == "LONG":
        return (exit_px - entry) * qty
    return (entry - exit_px) * qty


def exit_px_slip(side: str, barrier: float, slip: float, gap_open: Optional[float] = None) -> float:
    px = float(gap_open) if gap_open is not None else float(barrier)
    if side == "LONG":
        return px * (1.0 - slip)
    return px * (1.0 + slip)
