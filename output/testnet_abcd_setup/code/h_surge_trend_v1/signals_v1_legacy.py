"""Legacy H v1 entry evaluation (ORIGIN_DIRECT + cont_arm). For rollback/tests only."""

from __future__ import annotations

from typing import Any, Optional

from . import config
from .bars import Bar3m
from .metrics import (
    clv,
    efficiency_ratio,
    lower_wick_frac,
    pullback_from_high,
    rebound_from_low,
    rvol,
    surge_ok,
    upper_wick_frac,
    window_range_pct,
    window_return,
)
from .signals import SignalDecision
from .surge_events import SurgeEvent, continuation_index


def _direction_decision(
    *,
    bar: Bar3m,
    prices_60: list[float],
    prices_15: list[float],
    has_position: bool,
    m: dict[str, Any],
) -> SignalDecision:
    if len(prices_60) < config.ER_WINDOW_SEC:
        return SignalDecision(False, reason="ER_WINDOW_NOT_READY", metrics=m)

    er = efficiency_ratio(prices_60)
    rng = window_range_pct(prices_60)
    ret60 = window_return(prices_60)
    ret15 = window_return(prices_15) if len(prices_15) >= 2 else None
    price = float(prices_60[-1])
    c = clv(price, bar.high, bar.low)
    m.update(
        {
            "ER": er,
            "range_pct_60s": rng,
            "ret_60s": ret60,
            "ret_15s": ret15,
            "CLV": c,
            "price": price,
        }
    )
    if er < config.ER_MIN:
        return SignalDecision(False, reason="ER_TOO_LOW", metrics=m)
    if rng is None or rng < config.RANGE_MIN_PCT:
        return SignalDecision(False, reason="RANGE_TOO_SMALL", metrics=m)
    if c is None:
        return SignalDecision(False, reason="CLV_UNDEFINED", metrics=m)
    if ret60 is None:
        return SignalDecision(False, reason="RET60_UNDEFINED", metrics=m)

    chop_ok = er >= config.ER_MIN and rng >= config.RANGE_MIN_PCT
    long_ok = chop_ok and ret60 >= config.RET_60S_LONG_MIN and c >= config.CLV_LONG_MIN
    short_trend_ok = chop_ok and ret60 <= config.RET_60S_SHORT_MAX and c <= config.CLV_SHORT_MAX

    u_wick = upper_wick_frac(bar.open, bar.high, bar.low, price)
    l_wick = lower_wick_frac(bar.open, bar.high, bar.low, price)
    pullback = pullback_from_high(bar.high, price)
    rebound = rebound_from_low(bar.low, price)
    m["upper_wick_frac"] = u_wick
    m["lower_wick_frac"] = l_wick
    m["pullback_from_high"] = pullback
    m["rebound_from_low"] = rebound

    reverse_short_ok = False
    if (
        chop_ok
        and not has_position
        and u_wick is not None
        and u_wick >= config.WICK_FRAC_MIN
        and pullback is not None
        and pullback >= config.PULLBACK_FROM_HIGH_MIN
        and ret15 is not None
        and ret15 <= config.RET_15S_MAX
        and c <= config.CLV_REVERSE_SHORT_MAX
    ):
        reverse_short_ok = True

    reverse_long_ok = False
    if (
        chop_ok
        and not has_position
        and l_wick is not None
        and l_wick >= config.WICK_FRAC_MIN
        and rebound is not None
        and rebound >= config.REBOUND_FROM_LOW_MIN
        and ret15 is not None
        and ret15 >= config.RET_15S_MIN
        and c >= config.CLV_REVERSE_LONG_MIN
    ):
        reverse_long_ok = True

    tags: list[str] = []
    side: Optional[str] = None
    want_long = long_ok or reverse_long_ok
    want_short = short_trend_ok or reverse_short_ok

    if want_long and want_short:
        conflict_tags: list[str] = []
        if long_ok:
            conflict_tags.append("TREND_LONG")
        if reverse_long_ok:
            conflict_tags.append("WICK_REVERSE_LONG")
        if short_trend_ok:
            conflict_tags.append("TREND_SHORT")
        if reverse_short_ok:
            conflict_tags.append("WICK_REVERSE_SHORT")
        return SignalDecision(
            False,
            reason="LONG_SHORT_CONFLICT",
            tags=conflict_tags,
            metrics=m,
        )

    if want_long:
        if has_position:
            return SignalDecision(False, reason="ALREADY_IN_POSITION", metrics=m)
        side = "LONG"
        if long_ok:
            tags.append("TREND_LONG")
        if reverse_long_ok:
            tags.append("WICK_REVERSE_LONG")
    elif want_short:
        if has_position:
            return SignalDecision(
                False, reason="MUST_FLAT_BEFORE_REVERSE_OR_SHORT", metrics=m
            )
        side = "SHORT"
        if short_trend_ok:
            tags.append("TREND_SHORT")
        if reverse_short_ok:
            tags.append("WICK_REVERSE_SHORT")
    else:
        return SignalDecision(False, reason="DIRECTION_FILTER_FAIL", metrics=m)

    return SignalDecision(True, side=side, tags=tags, reason="OK", metrics=m)


def evaluate_entry(
    *,
    decision_ts: float,
    bar: Optional[Bar3m],
    baseline_median: Optional[float],
    prices_60: list[float],
    prices_15: list[float],
    last_trade_ts: float,
    gap_unfilled: bool,
    has_position: bool,
    has_exit_pending: bool,
    first_entry_already_sent_this_bar: bool,
    surge_event: Optional[SurgeEvent] = None,
) -> SignalDecision:
    m: dict[str, Any] = {"decision_ts": decision_ts}

    if first_entry_already_sent_this_bar:
        return SignalDecision(False, reason="SAME_BAR_FIRST_ENTRY_ALREADY_SENT", metrics=m)
    if has_exit_pending:
        return SignalDecision(False, reason="EXIT_PENDING", metrics=m)
    if gap_unfilled:
        return SignalDecision(False, reason="TRADE_STREAM_GAP", metrics=m)
    # legacy stale uses historical 3s when testing cont_arm
    stale = float(getattr(config, "TRADE_STALE_SEC_LEGACY", config.TRADE_STALE_SEC))
    if last_trade_ts <= 0 or (decision_ts - last_trade_ts) > stale:
        return SignalDecision(False, reason="TRADE_STALE", metrics=m)
    if bar is None or bar.trade_count <= 0:
        return SignalDecision(False, reason="BAR_INCOMPLETE_OR_MISSING", metrics=m)

    elapsed = decision_ts - bar.start_ts
    m["elapsed_sec"] = elapsed
    m["bar_start_ts"] = bar.start_ts
    m["bar_quote"] = bar.quote_volume
    m["baseline_rolling"] = baseline_median
    m["baseline_frozen"] = (
        float(surge_event.baseline_frozen) if surge_event is not None else None
    )

    origin_surge = False
    origin_detail: dict[str, Any] = {}
    if baseline_median is not None and float(baseline_median) > 0:
        origin_surge, origin_detail = surge_ok(
            bar.quote_volume,
            float(baseline_median),
            elapsed,
            min_elapsed=config.RVOL_MIN_ELAPSED_SEC,
            rvol_threshold=config.RVOL_THRESHOLD,
            abs_frac=config.RVOL_ABS_FRAC_OF_BASELINE,
            bar_sec=float(config.BAR_SEC),
        )
    else:
        origin_detail = {"reason": "BASELINE_MISSING", "rvol": None}
    m["surge"] = origin_detail
    m["origin_surge_ok"] = origin_surge

    cont_ok = False
    cont_detail: dict[str, Any] = {"reason": "NO_EVENT"}
    cont_idx: Optional[int] = None
    if (
        surge_event is not None
        and surge_event.state in ("OBSERVING", "RESERVED")
        and decision_ts < float(surge_event.expires_at)
        and float(surge_event.baseline_frozen) > 0
    ):
        cont_idx = continuation_index(surge_event.source_bar_open_time, bar.start_ts)
        m["continuation_bar_index"] = cont_idx
        m["event_id"] = surge_event.event_id
        if cont_idx in (1, 2):
            cont_ok, cont_detail = surge_ok(
                bar.quote_volume,
                float(surge_event.baseline_frozen),
                elapsed,
                min_elapsed=config.RVOL_MIN_ELAPSED_SEC,
                rvol_threshold=float(config.CONTINUATION_RVOL_THRESHOLD),
                abs_frac=config.RVOL_ABS_FRAC_OF_BASELINE,
                bar_sec=float(config.BAR_SEC),
            )
            cont_detail["continuation_rvol"] = cont_detail.get("rvol")
            cont_detail["baseline_frozen"] = float(surge_event.baseline_frozen)
        elif cont_idx == 0:
            cont_detail = {"reason": "ON_SOURCE_BAR_USE_ORIGIN_THRESHOLDS"}
        else:
            cont_detail = {"reason": "OUTSIDE_CONTINUATION_WINDOW"}
    m["continuation"] = cont_detail
    m["continuation_volume_ok"] = cont_ok

    if baseline_median and float(baseline_median) > 0 and elapsed > 0:
        m["rvol_rolling"] = rvol(
            bar.quote_volume, float(baseline_median), elapsed, bar_sec=float(config.BAR_SEC)
        )

    if origin_surge:
        d = _direction_decision(
            bar=bar,
            prices_60=prices_60,
            prices_15=prices_15,
            has_position=has_position,
            m=m,
        )
        d.entry_path = "ORIGIN_DIRECT"
        d.event_id = surge_event.event_id if surge_event is not None else None
        if d.ok:
            d.tags = list(d.tags) + ["ORIGIN_DIRECT"]
        d.metrics = m
        return d

    if cont_ok and cont_idx in (1, 2):
        d = _direction_decision(
            bar=bar,
            prices_60=prices_60,
            prices_15=prices_15,
            has_position=has_position,
            m=m,
        )
        path = "CONTINUATION_BAR1" if cont_idx == 1 else "CONTINUATION_BAR2"
        d.entry_path = path
        d.event_id = surge_event.event_id if surge_event is not None else None
        if d.ok:
            d.tags = list(d.tags) + [path]
        d.metrics = m
        return d

    if baseline_median is None or float(baseline_median) <= 0:
        return SignalDecision(False, reason="BASELINE_MISSING", metrics=m)
    if not origin_surge:
        reason = f"NO_SURGE:{origin_detail.get('reason')}"
        if surge_event is not None and cont_idx in (1, 2):
            reason = f"NO_CONTINUATION:{cont_detail.get('reason')}"
        return SignalDecision(False, reason=reason, metrics=m)
    return SignalDecision(False, reason="NO_ENTRY_PATH", metrics=m)


def evaluate_continuation_only(
    *,
    decision_ts: float,
    bar: Optional[Bar3m],
    prices_60: list[float],
    prices_15: list[float],
    has_position: bool,
    surge_event: SurgeEvent,
) -> Optional[SignalDecision]:
    if bar is None:
        return None
    cont_idx = continuation_index(surge_event.source_bar_open_time, bar.start_ts)
    if cont_idx not in (1, 2):
        return None
    elapsed = decision_ts - bar.start_ts
    cont_ok, cont_detail = surge_ok(
        bar.quote_volume,
        float(surge_event.baseline_frozen),
        elapsed,
        min_elapsed=config.RVOL_MIN_ELAPSED_SEC,
        rvol_threshold=float(config.CONTINUATION_RVOL_THRESHOLD),
        abs_frac=config.RVOL_ABS_FRAC_OF_BASELINE,
        bar_sec=float(config.BAR_SEC),
    )
    if not cont_ok:
        return SignalDecision(False, reason=f"NO_CONTINUATION:{cont_detail.get('reason')}")
    m: dict[str, Any] = {"continuation": cont_detail}
    d = _direction_decision(
        bar=bar,
        prices_60=prices_60,
        prices_15=prices_15,
        has_position=has_position,
        m=m,
    )
    path = "CONTINUATION_BAR1" if cont_idx == 1 else "CONTINUATION_BAR2"
    d.entry_path = path
    d.event_id = surge_event.event_id
    if d.ok:
        d.tags = list(d.tags) + [path]
    return d
