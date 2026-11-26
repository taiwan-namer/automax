"""Pure metric helpers for H v1 (no I/O). Forbid future-bar backfill by construction."""

from __future__ import annotations

from typing import Optional, Sequence


def efficiency_ratio(prices: Sequence[float]) -> float:
    if len(prices) < 2:
        return 0.0
    net = abs(float(prices[-1]) - float(prices[0]))
    path = 0.0
    for i in range(1, len(prices)):
        path += abs(float(prices[i]) - float(prices[i - 1]))
    if path <= 0:
        return 0.0
    return net / path


def window_return(prices: Sequence[float]) -> Optional[float]:
    if len(prices) < 2 or float(prices[0]) <= 0:
        return None
    return (float(prices[-1]) - float(prices[0])) / float(prices[0])


def window_range_pct(prices: Sequence[float]) -> Optional[float]:
    if not prices:
        return None
    lo = min(float(p) for p in prices)
    hi = max(float(p) for p in prices)
    if lo <= 0:
        return None
    return (hi - lo) / lo


def clv(price: float, bar_high: float, bar_low: float) -> Optional[float]:
    rng = float(bar_high) - float(bar_low)
    if rng <= 0:
        return None
    return (float(price) - float(bar_low)) / rng


def upper_wick_frac(open_px: float, high: float, low: float, price: float) -> Optional[float]:
    rng = float(high) - float(low)
    if rng <= 0:
        return None
    wick = float(high) - max(float(open_px), float(price))
    if wick < 0:
        wick = 0.0
    return wick / rng


def lower_wick_frac(open_px: float, high: float, low: float, price: float) -> Optional[float]:
    rng = float(high) - float(low)
    if rng <= 0:
        return None
    wick = min(float(open_px), float(price)) - float(low)
    if wick < 0:
        wick = 0.0
    return wick / rng


def pullback_from_high(high: float, price: float) -> Optional[float]:
    if float(high) <= 0:
        return None
    return (float(high) - float(price)) / float(high)


def rebound_from_low(low: float, price: float) -> Optional[float]:
    if float(low) <= 0:
        return None
    return (float(price) - float(low)) / float(low)


def rvol(
    cumulative_quote: float,
    baseline_median: float,
    elapsed_sec: float,
    *,
    bar_sec: float = 180.0,
) -> Optional[float]:
    if baseline_median <= 0 or elapsed_sec <= 0 or bar_sec <= 0:
        return None
    expected = baseline_median * (elapsed_sec / bar_sec)
    if expected <= 0:
        return None
    return float(cumulative_quote) / expected


def surge_ok(
    cumulative_quote: float,
    baseline_median: float,
    elapsed_sec: float,
    *,
    min_elapsed: float,
    rvol_threshold: float,
    abs_frac: float,
    bar_sec: float = 180.0,
) -> tuple[bool, dict]:
    detail = {
        "elapsed_sec": elapsed_sec,
        "cumulative_quote": cumulative_quote,
        "baseline_median": baseline_median,
        "rvol": None,
        "abs_ok": False,
        "reason": "",
    }
    if baseline_median <= 0:
        detail["reason"] = "BASELINE_ZERO_OR_MISSING"
        return False, detail
    if elapsed_sec < min_elapsed:
        detail["reason"] = "ELAPSED_TOO_SHORT"
        return False, detail
    rv = rvol(cumulative_quote, baseline_median, elapsed_sec, bar_sec=bar_sec)
    detail["rvol"] = rv
    if rv is None:
        detail["reason"] = "RVOL_UNDEFINED"
        return False, detail
    abs_ok = cumulative_quote >= baseline_median * abs_frac
    detail["abs_ok"] = abs_ok
    if rv < rvol_threshold:
        detail["reason"] = "RVOL_BELOW_THRESHOLD"
        return False, detail
    if not abs_ok:
        detail["reason"] = "ABS_QUOTE_BELOW_HALF_BASELINE"
        return False, detail
    detail["reason"] = "OK"
    return True, detail


def gross_pnl_pct(side: str, avg_entry: float, price: float) -> Optional[float]:
    if avg_entry <= 0:
        return None
    if str(side).upper() == "LONG":
        return (float(price) - float(avg_entry)) / float(avg_entry)
    return (float(avg_entry) - float(price)) / float(avg_entry)


def trail_stop(side: str, extreme: float, trail_pct: float) -> float:
    if str(side).upper() == "LONG":
        return float(extreme) * (1.0 - float(trail_pct))
    return float(extreme) * (1.0 + float(trail_pct))
