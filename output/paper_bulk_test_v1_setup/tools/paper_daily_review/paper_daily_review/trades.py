from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .jsonl_io import dedupe_fills, read_jsonl_bounded
from .timeutil import in_window, parse_iso


def is_missing_num(v: Any) -> bool:
    """True only for None/absent; 0.0 is a valid value."""
    return v is None


def open_fee_component(open_fill: Dict[str, Any]) -> float:
    """OPEN realized_net_u if present (incl. 0), else -fee_u."""
    if "realized_net_u" in open_fill and open_fill["realized_net_u"] is not None:
        return float(open_fill["realized_net_u"])
    fee = open_fill.get("fee_u")
    if fee is None:
        return 0.0
    return -float(fee)


def complete_trade_net(fills: List[Dict[str, Any]]) -> Optional[float]:
    """Sum OPEN component + all PARTIAL/CLOSE net_u. None if no fills."""
    if not fills:
        return None
    total = 0.0
    has_any = False
    for f in fills:
        ev = f.get("event")
        if ev == "OPEN":
            total += open_fee_component(f)
            has_any = True
        elif ev in ("CLOSE", "CLOSE_PARTIAL"):
            if "net_u" in f and f["net_u"] is not None:
                total += float(f["net_u"])
            elif "realized_net_u" in f and f["realized_net_u"] is not None:
                total += float(f["realized_net_u"])
            has_any = True
    return total if has_any else None


def load_all_fills(paths, *, root_max_bytes: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from .discover import list_run_dirs

    meta: Dict[str, Any] = {"sources": [], "dup_removed": 0, "truncated_tails": []}
    rows: List[Dict[str, Any]] = []
    # Prefer root fills (merged ledger) + per-run for cross-check
    for label, p in [("root", paths.root_fills)] + [
        (f"run:{rd.name}", rd / "fills.jsonl") for rd in list_run_dirs(paths)
    ]:
        if not p.exists():
            continue
        r = read_jsonl_bounded(p, max_bytes=root_max_bytes if label == "root" else None)
        meta["sources"].append(
            {
                "label": label,
                "path": str(p),
                "bytes": r.bytes_read,
                "lines_ok": r.lines_ok,
                "truncated_tail": r.truncated_tail,
                "parse_errors": r.parse_errors,
                "mtime": r.mtime,
            }
        )
        if r.truncated_tail:
            meta["truncated_tails"].append(str(p))
        for row in r.rows:
            row = dict(row)
            row.setdefault("_source", label)
            rows.append(row)
    # Prefer root copy when duplicate keys exist across root+run
    # Keep first by sort: root before run by putting root first then dedupe
    deduped, n_dup = dedupe_fills(rows)
    meta["dup_removed"] = n_dup
    meta["rows_after_dedupe"] = len(deduped)
    return deduped, meta


def group_by_trade_id(fills: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in fills:
        tid = f.get("trade_id")
        if not tid:
            continue
        g[str(tid)].append(f)
    for tid in g:
        g[tid].sort(key=lambda x: (x.get("recorded_at") or "", x.get("event") or ""))
    return dict(g)


def trade_is_complete(fills: List[Dict[str, Any]]) -> bool:
    has_open = any(f.get("event") == "OPEN" for f in fills)
    has_close = any(f.get("event") == "CLOSE" for f in fills)
    return has_open and has_close


def trade_is_open(fills: List[Dict[str, Any]]) -> bool:
    has_open = any(f.get("event") == "OPEN" for f in fills)
    has_close = any(f.get("event") == "CLOSE" for f in fills)
    return has_open and not has_close


@dataclass
class DayTradeStats:
    complete_closed_today: List[Dict[str, Any]] = field(default_factory=list)
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    segment_events: List[Dict[str, Any]] = field(default_factory=list)
    metrics_c: Dict[str, Any] = field(default_factory=dict)
    metrics_d: Dict[str, Any] = field(default_factory=dict)
    metrics_b: Dict[str, Any] = field(default_factory=dict)


def analyze_day_trades(
    fills: List[Dict[str, Any]],
    start,
    end,
) -> DayTradeStats:
    """A/B/C/D trade analysis for [start, end)."""
    out = DayTradeStats()
    by_tid = group_by_trade_id(fills)

    # B: segment events in window (CLOSE/CLOSE_PARTIAL/OPEN) — not winrate
    for f in fills:
        ts = parse_iso(f.get("recorded_at"))
        if ts is None or not in_window(ts, start, end):
            continue
        if f.get("event") in ("OPEN", "CLOSE", "CLOSE_PARTIAL"):
            out.segment_events.append(f)

    # C: complete trades whose CLOSE falls in window
    closed_nets: List[float] = []
    wins = losses = flats = 0
    fees_sum = 0.0
    for tid, tfills in by_tid.items():
        if not trade_is_complete(tfills):
            continue
        close_f = [f for f in tfills if f.get("event") == "CLOSE"][-1]
        close_ts = parse_iso(close_f.get("recorded_at"))
        if close_ts is None or not in_window(close_ts, start, end):
            continue
        net = complete_trade_net(tfills)
        if net is None:
            continue
        open_f = next(f for f in tfills if f.get("event") == "OPEN")
        row = {
            "trade_id": tid,
            "symbol": open_f.get("symbol"),
            "side": open_f.get("side"),
            "opened_at": open_f.get("recorded_at"),
            "closed_at": close_f.get("recorded_at"),
            "open_run_id": open_f.get("run_id"),
            "close_run_id": close_f.get("run_id"),
            "complete_net_u": net,
            "exit_reason": close_f.get("exit_reason"),
            "tag": open_f.get("tag") or open_f.get("strategy") or "",
            "open_fee_u": open_f.get("fee_u"),
            "open_realized_net_u": open_f.get("realized_net_u"),
            "entry_px": open_f.get("price"),
            "stop0": open_f.get("stop0") or open_f.get("initial_stop"),
            "qty": open_f.get("qty"),
            "cross_version": (open_f.get("run_id") != close_f.get("run_id")),
        }
        # fees across all legs
        fee_total = 0.0
        for f in tfills:
            if f.get("fee_u") is not None:
                fee_total += float(f["fee_u"])
        row["fees_u"] = fee_total
        fees_sum += fee_total
        out.complete_closed_today.append(row)
        closed_nets.append(net)
        if net > 0:
            wins += 1
        elif net < 0:
            losses += 1
        else:
            flats += 1

    n = len(closed_nets)
    gp = sum(x for x in closed_nets if x > 0)
    gl = -sum(x for x in closed_nets if x < 0)
    out.metrics_c = {
        "n_complete_closed": n,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": (wins / n) if n else None,
        "avg_net_u": (sum(closed_nets) / n) if n else None,
        "sum_net_u": sum(closed_nets) if n else 0.0,
        "profit_factor": (gp / gl) if gl > 0 else (None if gp == 0 else float("inf")),
        "expectancy_u": (sum(closed_nets) / n) if n else None,
        "fees_u": fees_sum,
        "note": "C = complete trade_id with CLOSE in day window; OPEN fee may belong to prior calendar day",
    }

    # D: still-open trades (no CLOSE yet) — OPEN fee booked historically in A, not C
    open_fee_sum = 0.0
    for tid, tfills in by_tid.items():
        if not trade_is_open(tfills):
            continue
        open_f = next(f for f in tfills if f.get("event") == "OPEN")
        open_ts = parse_iso(open_f.get("recorded_at"))
        # include if open happened any time and still open (or open in window)
        fee_comp = open_fee_component(open_f)
        open_fee_in_day = open_ts is not None and in_window(open_ts, start, end)
        out.open_positions.append(
            {
                "trade_id": tid,
                "symbol": open_f.get("symbol"),
                "side": open_f.get("side"),
                "opened_at": open_f.get("recorded_at"),
                "open_run_id": open_f.get("run_id"),
                "open_fee_component_u": fee_comp,
                "open_fee_in_day_window": open_fee_in_day,
                "qty": open_f.get("qty"),
                "entry_px": open_f.get("price"),
                "partials": [f for f in tfills if f.get("event") == "CLOSE_PARTIAL"],
            }
        )
        if open_fee_in_day:
            open_fee_sum += fee_comp
    out.metrics_d = {
        "n_open": len(out.open_positions),
        "open_fee_component_in_day_u": open_fee_sum,
        "note": "OPEN fees for open positions stay in account A; not mixed into C winrate",
    }

    # B summary by run
    by_run: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"opens": 0, "closes": 0, "partials": 0, "seg_net_u": 0.0})
    for f in out.segment_events:
        rid = f.get("run_id") or "unknown"
        ev = f.get("event")
        if ev == "OPEN":
            by_run[rid]["opens"] += 1
            by_run[rid]["seg_net_u"] += open_fee_component(f)
        elif ev == "CLOSE":
            by_run[rid]["closes"] += 1
            if f.get("net_u") is not None:
                by_run[rid]["seg_net_u"] += float(f["net_u"])
        elif ev == "CLOSE_PARTIAL":
            by_run[rid]["partials"] += 1
            if f.get("net_u") is not None:
                by_run[rid]["seg_net_u"] += float(f["net_u"])
    out.metrics_b = {
        "by_run": dict(by_run),
        "n_segment_events": len(out.segment_events),
        "note": "B = event/segment PnL in window; NOT complete-trade winrate",
    }
    return out


def nearest_equity_snapshot(rows: List[Dict[str, Any]], target_ts, *, after: bool = False) -> Optional[Dict[str, Any]]:
    best = None
    best_delta = None
    for r in rows:
        ts = parse_iso(r.get("ts") or r.get("recorded_at") or r.get("time"))
        if ts is None:
            continue
        if after:
            if ts < target_ts:
                continue
            d = (ts - target_ts).total_seconds()
        else:
            # prefer at-or-before; else nearest
            d = abs((ts - target_ts).total_seconds())
        if best_delta is None or d < best_delta:
            best_delta = d
            best = {**r, "_snap_ts": ts.isoformat(), "_lag_sec": (ts - target_ts).total_seconds()}
    return best


def account_day_metrics(
    equity_rows: List[Dict[str, Any]],
    start,
    end,
    account_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """A: account cumulative / day change. Never pretend live state is settlement-boundary."""
    start_snap = nearest_equity_snapshot(equity_rows, start, after=False)
    end_snap = nearest_equity_snapshot(equity_rows, end, after=False)
    # Prefer snap at/after start within 30m if available
    for r in equity_rows:
        ts = parse_iso(r.get("ts") or r.get("recorded_at") or r.get("time"))
        if ts is None:
            continue
        if abs((ts - start).total_seconds()) < abs((parse_iso(start_snap["_snap_ts"]) - start).total_seconds()) if start_snap else True:
            if abs((ts - start).total_seconds()) <= 1800 or (start_snap is None):
                if start_snap is None or abs((ts - start).total_seconds()) < abs(start_snap.get("_lag_sec", 1e9)):
                    start_snap = {**r, "_snap_ts": ts.isoformat(), "_lag_sec": (ts - start).total_seconds()}

    def _eq(s):
        if not s:
            return None
        for k in ("equity_u", "equity", "total_equity_u"):
            if s.get(k) is not None:
                return float(s[k])
        return None

    def _cash(s):
        if not s:
            return None
        for k in ("cash_u", "cash", "wallet_u"):
            if s.get(k) is not None:
                return float(s[k])
        return None

    se, ee = _eq(start_snap), _eq(end_snap)
    sc, ec = _cash(start_snap), _cash(end_snap)
    midnight_ok = (
        start_snap is not None
        and abs(start_snap.get("_lag_sec", 9999)) <= 120
        and end_snap is not None
        and abs(end_snap.get("_lag_sec", 9999)) <= 120
    )
    live = None
    if account_state:
        live = {
            "cash_u": account_state.get("cash_u") or account_state.get("cash"),
            "equity_u": account_state.get("equity_u") or account_state.get("equity"),
            "realized_u": account_state.get("realized_u") or account_state.get("realized"),
            "floating_u": account_state.get("floating_u") or account_state.get("unrealized"),
            "note": "live account_state — NOT a settlement-boundary (20:15) substitute",
        }
    return {
        # 歷史欄位名保留；語意=結算界起迄快照是否皆在 ±120s 內
        "midnight_snapshot_ok": midnight_ok,
        "settlement_snapshot_ok": midnight_ok,
        "start_snap": start_snap,
        "end_snap": end_snap,
        "start_equity_u": se,
        "end_equity_u": ee,
        "day_equity_delta_u": (ee - se) if (se is not None and ee is not None) else None,
        "start_cash_u": sc,
        "end_cash_u": ec,
        "day_cash_delta_u": (ec - sc) if (sc is not None and ec is not None) else None,
        "cash_delta_is_not_pnl": True,
        "funding_data_gap": True,
        "live_account_state": live,
        "day_loss_lock": (
            None
            if account_state is None
            else account_state.get("day_new_entries_locked")
            if "day_new_entries_locked" in (account_state or {})
            else account_state.get("day_loss_lock")
            if "day_loss_lock" in (account_state or {})
            else None
        ),
        "day_loss_lock_source": (
            "account_state.day_new_entries_locked"
            if account_state and "day_new_entries_locked" in account_state
            else "NA_missing_field"
        ),
    }
