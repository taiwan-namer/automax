from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .jsonl_io import read_jsonl_bounded
from .timeutil import in_window, parse_iso


def _percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * p
    f = int(k)
    c = min(f + 1, len(ys) - 1)
    if f == c:
        return ys[f]
    return ys[f] + (ys[c] - ys[f]) * (k - f)


def batch_quote_intervals(quote_rows: List[Dict[str, Any]], start, end) -> Dict[str, Any]:
    """Pre-manage quote batch intervals (unique fetched_at), NOT manage-complete.

    Separates MANAGE/MANAGE_POLL from SIGNAL_EVAL etc. Max gaps must be classified
    (cross-run / no-position / in-position) — never call them 「失管」without complete events.
    """
    manage_times = []
    all_times = []
    manage_batches = []  # (ts, purpose, run hint)
    for r in quote_rows:
        ts = parse_iso(r.get("fetched_at") or r.get("recorded_at") or r.get("ts"))
        if ts is None or not in_window(ts, start, end):
            continue
        all_times.append(ts)
        purp = str(r.get("purpose") or "")
        if purp in ("MANAGE", "MANAGE_POLL"):
            manage_times.append(ts)
            manage_batches.append((ts, purp, r.get("run_id") or r.get("_run")))

    def _gap_stats(times):
        uniq = sorted(set(times))
        gaps = [(uniq[i] - uniq[i - 1]).total_seconds() for i in range(1, len(uniq))]
        max_gap = None
        max_span = None
        if gaps:
            i_max = max(range(len(gaps)), key=lambda i: gaps[i])
            max_gap = gaps[i_max]
            max_span = [uniq[i_max].isoformat(), uniq[i_max + 1].isoformat()]
        return {
            "n_batches": len(uniq),
            "n_gaps": len(gaps),
            "mean": (sum(gaps) / len(gaps)) if gaps else None,
            "p50": _percentile(gaps, 0.5),
            "p95": _percentile(gaps, 0.95),
            "max": max_gap,
            "max_span": max_span,
        }

    manage_stats = _gap_stats(manage_times)
    all_stats = _gap_stats(all_times)
    return {
        "metric": "pre_manage_quote_batch_interval_sec",
        "manage_only": manage_stats,
        "all_purposes_including_signal": all_stats,
        "n_batches": manage_stats["n_batches"],
        "n_gaps": manage_stats["n_gaps"],
        "mean": manage_stats["mean"],
        "p50": manage_stats["p50"],
        "p95": manage_stats["p95"],
        "max": manage_stats["max"],
        "max_span": manage_stats["max_span"],
        "note": (
            "MANAGE/MANAGE_POLL pre-manage quote batches only by default. "
            "NOT manage-complete; NOT configured poll; NOT 「失管」. "
            "All-purpose max may be cross-run SIGNAL_EVAL (see GAP evidence)."
        ),
    }


def manage_complete_intervals(event_rows: List[Dict[str, Any]], start, end) -> Dict[str, Any]:
    """True manage-complete events — NA until schema exists in data."""
    times = []
    for r in event_rows:
        if r.get("event_type") not in ("MANAGE_BATCH_COMPLETE", "manage_batch_complete", "MANAGE_COMPLETE"):
            continue
        ts = parse_iso(r.get("recorded_at") or r.get("ts"))
        if ts is None or not in_window(ts, start, end):
            continue
        times.append(ts)
    if not times:
        return {
            "metric": "manage_complete_interval_sec",
            "value": None,
            "status": "NA",
            "reason": "no manage completion events in sources (design pending deploy)",
        }
    times = sorted(times)
    gaps = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    return {
        "metric": "manage_complete_interval_sec",
        "n": len(gaps),
        "mean": (sum(gaps) / len(gaps)) if gaps else None,
        "p50": _percentile(gaps, 0.5),
        "p95": _percentile(gaps, 0.95),
        "max": max(gaps) if gaps else None,
        "status": "ok",
    }


def dq_breakdown(dq_rows: List[Dict[str, Any]], start, end) -> Dict[str, Any]:
    counts: Dict[str, int] = defaultdict(int)
    for r in dq_rows:
        ts = parse_iso(r.get("recorded_at") or r.get("ts") or r.get("time"))
        if ts is not None and not in_window(ts, start, end):
            continue
        kind = (
            r.get("kind")
            or r.get("type")
            or r.get("event")
            or r.get("code")
            or r.get("reason")
            or r.get("error_class")
            or r.get("tag")
            or r.get("msg")
            or r.get("message")
        )
        if not kind:
            # fall back to first informative string field
            for k, v in r.items():
                if k in ("recorded_at", "ts", "time", "run_id") or not isinstance(v, str):
                    continue
                kind = v
                break
            kind = kind or "unknown"
        counts[str(kind)[:120]] += 1
    return {
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:40]),
        "timeouts": sum(v for k, v in counts.items() if "TIMEOUT" in k.upper()),
        "http_429": sum(v for k, v in counts.items() if "429" in k.upper()),
        "http_418": sum(v for k, v in counts.items() if "418" in k.upper()),
        "manage_blocked": sum(
            v for k, v in counts.items() if "MANAGE" in k.upper() and "BLOCK" in k.upper()
        ),
        "note": "From data_quality.jsonl `code`/`kind`; excess stop overshoot is separate fill analysis",
    }


def load_run_jsonl(run_dir, name: str) -> List[Dict[str, Any]]:
    p = run_dir / name
    if not p.exists():
        return []
    return read_jsonl_bounded(p).rows


def analyze_manage_health(run_dirs, start, end) -> Dict[str, Any]:
    quotes: List[Dict[str, Any]] = []
    dq: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for rd in run_dirs:
        quotes.extend(load_run_jsonl(rd, "quote_events.jsonl"))
        dq.extend(load_run_jsonl(rd, "data_quality.jsonl"))
        # future path
        events.extend(load_run_jsonl(rd, "manage_events.jsonl"))
    return {
        "quote_batch_intervals": batch_quote_intervals(quotes, start, end),
        "manage_complete_intervals": manage_complete_intervals(events, start, end),
        "data_quality": dq_breakdown(dq, start, end),
        "sources_non_atomic": True,
        "note": "Multi-file reads are non-atomic across runs; truncated tails deferred per file",
    }
