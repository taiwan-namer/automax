from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .timeutil import parse_iso


def _load_1m_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows


def find_symbol_1m(vault_dir: Path, symbol: str) -> Optional[Path]:
    if not vault_dir.exists():
        return None
    # common layouts
    candidates = [
        vault_dir / symbol / "1m.csv",
        vault_dir / f"{symbol}_1m.csv",
        vault_dir / "1m" / f"{symbol}.csv",
    ]
    for c in candidates:
        if c.exists():
            return c
    # recursive search limited
    for p in vault_dir.rglob(f"*{symbol}*1m*.csv"):
        return p
    for p in vault_dir.rglob(f"{symbol}.csv"):
        if "1m" in str(p).lower() or p.parent.name == "1m":
            return p
    return None


def qa_1m_bars(rows: List[Dict[str, Any]], start_ms: Optional[int], end_ms: Optional[int]) -> Dict[str, Any]:
    opens = []
    dup = 0
    seen: Set[int] = set()
    ohlc_bad = 0
    for r in rows:
        ot = r.get("open_time") or r.get("openTime") or r.get("t") or r.get("timestamp")
        try:
            t = int(float(ot))
        except (TypeError, ValueError):
            continue
        # ms vs s
        if t < 10_000_000_000:
            t *= 1000
        if start_ms is not None and t < start_ms:
            continue
        if end_ms is not None and t >= end_ms:
            continue
        if t in seen:
            dup += 1
            continue
        seen.add(t)
        try:
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            if not (l <= o <= h and l <= c <= h and l <= h):
                ohlc_bad += 1
        except Exception:
            ohlc_bad += 1
        opens.append(t)
    opens.sort()
    gaps = 0
    for i in range(1, len(opens)):
        if opens[i] - opens[i - 1] > 60_000:
            gaps += (opens[i] - opens[i - 1]) // 60_000 - 1
    cover_start = opens[0] if opens else None
    cover_end = opens[-1] if opens else None
    head_gap = None
    tail_gap = None
    if cover_start is not None and start_ms is not None and cover_start > start_ms:
        head_gap = (cover_start - start_ms) // 60_000
    if cover_end is not None and end_ms is not None and cover_end + 60_000 < end_ms:
        tail_gap = (end_ms - (cover_end + 60_000)) // 60_000
    return {
        "n_bars": len(opens),
        "duplicates_skipped": dup,
        "missing_minutes_internal_est": gaps,
        "ohlc_anomalies": ohlc_bad,
        "first_ms": cover_start,
        "last_ms": cover_end,
        "required_window_ms": [start_ms, end_ms],
        "head_coverage_shortfall_min": head_gap,
        "tail_coverage_shortfall_min": tail_gap,
        "note": "internal missing minutes ≠ head/tail coverage shortfall",
    }


def derived_bucket_complete(bar_open_ms: int, timeframe_min: int, asof_ms: int) -> bool:
    """Only use HTF buckets fully closed as of asof_ms (no look-ahead)."""
    bucket_end = bar_open_ms + timeframe_min * 60_000
    return bucket_end <= asof_ms


def analyze_kline_qa(
    vault_dir: Path,
    symbols: List[str],
    start,
    end,
) -> Dict[str, Any]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    per = {}
    missing_syms = []
    for sym in symbols:
        path = find_symbol_1m(vault_dir, sym)
        if path is None:
            missing_syms.append(sym)
            per[sym] = {"status": "MISSING", "reason": "no 1m file in vault"}
            continue
        rows = _load_1m_csv(path)
        q = qa_1m_bars(rows, start_ms, end_ms)
        q["path"] = str(path)
        q["status"] = "ok" if q["n_bars"] else "EMPTY_WINDOW"
        q["derived_htf_rule"] = "10m/15m filters must use only fully closed buckets at decision time"
        per[sym] = q
    return {
        "symbols_requested": symbols,
        "missing_symbols": missing_syms,
        "per_symbol": per,
        "network_backfill": False,
        "vault_writeback": False,
        "note": "Missing data marked only; no exchange fetch; no vault mutation",
    }
