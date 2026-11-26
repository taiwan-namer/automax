"""Three independent first-entry paths for h_price_action_v2: A/B/C + CHOP gate.

A = SURGE (original RVOL + global ER/direction).
B = CONTINUATION (local ret/ER/CLV, no RVOL).
C = REVERSAL (120s pivot reclaim + breakout confirm).
NO_SURGE must not short-circuit B/C.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import config
from .bars import Bar3m
from .metrics import clv as bar_clv
from .metrics import efficiency_ratio, surge_ok, window_range_pct, window_return
from .price_series import SparseSecSeries


@dataclass
class PathResult:
    path: str  # SURGE | CONTINUATION | REVERSAL
    ok: bool
    side: Optional[str] = None  # LONG / SHORT
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "side": self.side,
            "reason": self.reason,
            "detail": self.detail,
        }


def _null_metrics() -> dict[str, Any]:
    return {
        "RVOL": None,
        "ret_10": None,
        "ret_15": None,
        "ret_60": None,
        "ER_10": None,
        "ER_30": None,
        "ER_60_dense": None,
        "CLV60": None,
        "H60": None,
        "L60": None,
        "coverage_60": None,
        "coverage_30": None,
        "coverage_120": None,
    }


def coverage_ok(series: SparseSecSeries, t: int, window: int, min_cov: float) -> tuple[bool, Optional[float]]:
    cov = series.coverage(t, window)
    if cov is None:
        return False, None
    return cov >= float(min_cov), cov


def evaluate_chop(
    series: SparseSecSeries,
    t: int,
    *,
    side: str,
    tick_size: float,
) -> dict[str, Any]:
    """Midline flip count on [t-60,t] vs fixed M from [t-120,t-60)."""
    cfg = config
    out: dict[str, Any] = {
        "chop": False,
        "flip_count": None,
        "M": None,
        "escape_ok": False,
        "reason": "",
    }
    cov_old, cov_old_v = coverage_ok(series, t - 60, 60, cfg.V2_COVERAGE_MIN)
    # old window is [t-120, t-60) → coverage over 60 seconds ending at t-61
    # Use coverage on [t-120, t-61] ≈ 60 slots; require data for H/L
    h_old, l_old, _, _ = series.hl(t - 120, t - 61)
    if h_old is None or l_old is None:
        out["reason"] = "CHOP_REF_DATA_INSUFFICIENT"
        # insufficient ref → do not mark CHOP (cannot prove oscillation)
        return out
    M = 0.5 * (h_old + l_old)
    out["M"] = M
    if M <= 0:
        out["reason"] = "CHOP_M_INVALID"
        return out

    up_th = M * (1.0 + cfg.V2_CHOP_BAND)
    dn_th = M * (1.0 - cfg.V2_CHOP_BAND)
    side_state: Optional[str] = None  # UP / DOWN
    flips = 0
    for s, px in series.prices_in(t - 60, t):
        if px > up_th:
            cur = "UP"
        elif px < dn_th:
            cur = "DOWN"
        else:
            continue  # in band: keep prior side
        if side_state is None:
            side_state = cur
        elif cur != side_state:
            flips += 1
            side_state = cur
    out["flip_count"] = flips
    out["coverage_old_ok"] = cov_old
    out["coverage_old"] = cov_old_v

    if flips >= int(cfg.V2_CHOP_FLIP_MIN):
        out["chop"] = True
        out["reason"] = "CHOP"
        # escape: break box of [t-60, t-10)
        box_h = series.max_in(t - 60, t - 11)
        box_l = series.min_in(t - 60, t - 11)
        out["box_high"] = box_h
        out["box_low"] = box_l
        tick = max(float(tick_size), 0.0)
        if box_h is None or box_l is None:
            out["escape_ok"] = False
            out["reason"] = "CHOP_NO_ESCAPE_BOX"
            return out
        ok_escape = True
        for sec in (t - 2, t - 1, t):
            px = series.p(sec)
            if px is None:
                ok_escape = False
                break
            if str(side).upper() == "LONG":
                if px < box_h + tick:
                    ok_escape = False
                    break
            else:
                if px > box_l - tick:
                    ok_escape = False
                    break
        out["escape_ok"] = ok_escape
        if ok_escape:
            out["reason"] = "CHOP_ESCAPED"
        else:
            out["reason"] = "CHOP_BLOCK"
    else:
        out["reason"] = "NOT_CHOP"
    return out


def evaluate_path_a_surge(
    *,
    decision_ts: float,
    bar: Optional[Bar3m],
    baseline_median: Optional[float],
    dense_prices_60: list[float],
    dense_prices_15: list[float],
) -> PathResult:
    """Original surge + global ER/trend direction (wick reverse NOT part of A)."""
    detail: dict[str, Any] = {}
    if bar is None or bar.trade_count <= 0:
        return PathResult("SURGE", False, reason="BAR_INCOMPLETE_OR_MISSING", detail=detail)
    if baseline_median is None or float(baseline_median) <= 0:
        return PathResult("SURGE", False, reason="BASELINE_MISSING", detail=detail)

    elapsed = float(decision_ts) - float(bar.start_ts)
    ok_surge, surge_detail = surge_ok(
        float(bar.quote_volume),
        float(baseline_median),
        elapsed,
        min_elapsed=config.RVOL_MIN_ELAPSED_SEC,
        rvol_threshold=config.RVOL_THRESHOLD,
        abs_frac=config.RVOL_ABS_FRAC_OF_BASELINE,
        bar_sec=float(config.BAR_SEC),
    )
    detail["surge"] = surge_detail
    detail["RVOL"] = surge_detail.get("rvol")
    if not ok_surge:
        return PathResult(
            "SURGE", False, reason=f"NO_SURGE:{surge_detail.get('reason')}", detail=detail
        )

    # Global ER / direction (original thresholds) — dense grid for A only
    if len(dense_prices_60) < config.ER_WINDOW_SEC:
        return PathResult("SURGE", False, reason="ER_WINDOW_NOT_READY", detail=detail)
    er = efficiency_ratio(dense_prices_60)
    rng = window_range_pct(dense_prices_60)
    ret60 = window_return(dense_prices_60)
    price = float(dense_prices_60[-1])
    c = bar_clv(price, bar.high, bar.low)
    detail.update(
        {
            "ER_60_dense": er,
            "range_pct_60s": rng,
            "ret_60_dense": ret60,
            "CLV_bar": c,
            "price": price,
        }
    )
    if er < config.ER_MIN:
        return PathResult("SURGE", False, reason="ER_TOO_LOW", detail=detail)
    if rng is None or rng < config.RANGE_MIN_PCT:
        return PathResult("SURGE", False, reason="RANGE_TOO_SMALL", detail=detail)
    if c is None:
        return PathResult("SURGE", False, reason="CLV_UNDEFINED", detail=detail)
    if ret60 is None:
        return PathResult("SURGE", False, reason="RET60_UNDEFINED", detail=detail)

    long_ok = ret60 >= config.RET_60S_LONG_MIN and c >= config.CLV_LONG_MIN
    short_ok = ret60 <= config.RET_60S_SHORT_MAX and c <= config.CLV_SHORT_MAX
    if long_ok and short_ok:
        return PathResult("SURGE", False, reason="DIRECTION_CONFLICT", detail=detail)
    if long_ok:
        return PathResult("SURGE", True, side="LONG", reason="OK", detail=detail)
    if short_ok:
        return PathResult("SURGE", True, side="SHORT", reason="OK", detail=detail)
    return PathResult("SURGE", False, reason="DIRECTION_FILTER_FAIL", detail=detail)


def _clv60(p: float, h60: float, l60: float) -> Optional[float]:
    if h60 == l60:
        return None
    return (p - l60) / (h60 - l60)


def evaluate_path_b_continuation(
    series: SparseSecSeries,
    t: int,
) -> PathResult:
    """Price continuation, symmetric LONG/SHORT. Single-second check (streak outside)."""
    detail: dict[str, Any] = dict(_null_metrics())
    cfg = config
    ok60, cov60 = coverage_ok(series, t, 60, cfg.V2_COVERAGE_MIN)
    ok30, cov30 = coverage_ok(series, t, 30, cfg.V2_COVERAGE_MIN)
    ok15, cov15 = coverage_ok(series, t, 15, cfg.V2_COVERAGE_MIN)
    detail["coverage_60"] = cov60
    detail["coverage_30"] = cov30
    detail["coverage_15"] = cov15
    if not (ok60 and ok30 and ok15):
        return PathResult("CONTINUATION", False, reason="DATA_INSUFFICIENT", detail=detail)

    p = series.p(t)
    if p is None or p <= 0:
        return PathResult("CONTINUATION", False, reason="DATA_INSUFFICIENT", detail=detail)

    ret60 = series.ret(t, 60)
    ret15 = series.ret(t, 15)
    er30 = series.er(t, 30)
    h60, l60, _, _ = series.hl(t - 60, t)
    detail.update(
        {
            "ret_60": ret60,
            "ret_15": ret15,
            "ER_30": er30,
            "H60": h60,
            "L60": l60,
            "price": p,
        }
    )
    if ret60 is None or ret15 is None:
        return PathResult("CONTINUATION", False, reason="DATA_INSUFFICIENT", detail=detail)
    if h60 is None or l60 is None:
        return PathResult("CONTINUATION", False, reason="DATA_INSUFFICIENT", detail=detail)
    if h60 == l60:
        return PathResult("CONTINUATION", False, reason="CLV60_FLAT_NO_SIGNAL", detail=detail)
    clv60 = _clv60(p, h60, l60)
    detail["CLV60"] = clv60
    if clv60 is None:
        return PathResult("CONTINUATION", False, reason="CLV60_UNDEFINED", detail=detail)

    long_ok = (
        ret60 >= cfg.V2_B_RET60
        and ret15 >= cfg.V2_B_RET15
        and er30 >= cfg.V2_B_ER30
        and clv60 >= cfg.V2_B_CLV_LONG
        and ((h60 - p) / h60) <= cfg.V2_B_PULLBACK_MAX
    )
    short_ok = (
        ret60 <= -cfg.V2_B_RET60
        and ret15 <= -cfg.V2_B_RET15
        and er30 >= cfg.V2_B_ER30
        and clv60 <= cfg.V2_B_CLV_SHORT
        and ((p - l60) / l60) <= cfg.V2_B_PULLBACK_MAX
    )
    detail["pullback_from_H60"] = (h60 - p) / h60 if h60 else None
    detail["rebound_from_L60"] = (p - l60) / l60 if l60 else None

    if long_ok and short_ok:
        return PathResult("CONTINUATION", False, reason="DIRECTION_CONFLICT", detail=detail)
    if long_ok:
        return PathResult("CONTINUATION", True, side="LONG", reason="OK", detail=detail)
    if short_ok:
        return PathResult("CONTINUATION", True, side="SHORT", reason="OK", detail=detail)
    return PathResult("CONTINUATION", False, reason="CONTINUATION_FILTER_FAIL", detail=detail)


def evaluate_path_c_reversal(
    series: SparseSecSeries,
    t: int,
    *,
    tick_size: float,
    armed: Optional[dict[str, Any]] = None,
) -> tuple[PathResult, Optional[dict[str, Any]]]:
    """Reversal path. Returns (result, new_armed_state).

    armed schema:
      {side, break_px, trigger_sec, confirm_left, candidate_id}
    On fresh breakout: arm with confirm_left=2 (need 2 subsequent seconds).
    On confirm seconds: must stay on break side; other conditions still hold.
    """
    detail: dict[str, Any] = dict(_null_metrics())
    cfg = config
    ok120, cov120 = coverage_ok(series, t, 120, cfg.V2_COVERAGE_MIN)
    ok10, cov10 = coverage_ok(series, t, 10, cfg.V2_COVERAGE_MIN)
    detail["coverage_120"] = cov120
    detail["coverage_10"] = cov10
    if not ok120 or not ok10:
        return PathResult("REVERSAL", False, reason="DATA_INSUFFICIENT", detail=detail), None

    p = series.p(t)
    if p is None or p <= 0:
        return PathResult("REVERSAL", False, reason="DATA_INSUFFICIENT", detail=detail), None

    tick = max(float(tick_size), 0.0)
    ret10 = series.ret(t, 10)
    er10 = series.er(t, 10)
    detail["ret_10"] = ret10
    detail["ER_10"] = er10
    detail["price"] = p

    def _long_struct() -> Optional[dict[str, Any]]:
        h120, l120, tau_h, tau_l = series.hl(t - 120, t)
        if h120 is None or l120 is None or tau_l is None:
            return None
        age = t - int(tau_l)
        if age < cfg.V2_C_TAU_MIN_SEC or age > cfg.V2_C_TAU_MAX_SEC:
            return {"fail": "TAU_OUT_OF_RANGE", "tau_l": tau_l, "L": l120, "age": age}
        # Hpre: max before L in same 120s window
        hpre, _, tau_hpre, _ = series.hl(t - 120, int(tau_l) - 1)
        if hpre is None or tau_hpre is None or tau_hpre >= tau_l:
            return {"fail": "HPRE_MISSING_OR_ORDER", "tau_l": tau_l, "L": l120}
        drop = (hpre - l120) / hpre if hpre > 0 else None
        reclaim = (p - l120) / l120 if l120 > 0 else None
        span = hpre - l120
        reclaim_ratio = ((p - l120) / span) if span > 0 else None
        prior_high = series.max_in(t - 10, t - 1)
        d = {
            "tau": tau_l,
            "L": l120,
            "Hpre": hpre,
            "tau_hpre": tau_hpre,
            "drop": drop,
            "reclaim": reclaim,
            "reclaim_ratio": reclaim_ratio,
            "prior_high": prior_high,
            "age": age,
        }
        if drop is None or drop < cfg.V2_C_PRE_MOVE:
            d["fail"] = "PRE_DROP_TOO_SMALL"
            return d
        if reclaim is None or reclaim < cfg.V2_C_RECLAIM:
            d["fail"] = "RECLAIM_TOO_SMALL"
            return d
        if reclaim_ratio is None or reclaim_ratio < cfg.V2_C_RECLAIM_RATIO:
            d["fail"] = "RECLAIM_RATIO_TOO_SMALL"
            return d
        if prior_high is None:
            d["fail"] = "PRIOR10_HIGH_MISSING"
            return d
        if p < prior_high + tick:
            d["fail"] = "NO_BREAK_PRIOR_HIGH"
            return d
        if ret10 is None or ret10 < cfg.V2_C_RET10:
            d["fail"] = "RET10_FAIL"
            return d
        if er10 < cfg.V2_C_ER10:
            d["fail"] = "ER10_FAIL"
            return d
        d["break_px"] = prior_high + tick
        d["side"] = "LONG"
        return d

    def _short_struct() -> Optional[dict[str, Any]]:
        h120, l120, tau_h, tau_l = series.hl(t - 120, t)
        if h120 is None or l120 is None or tau_h is None:
            return None
        age = t - int(tau_h)
        if age < cfg.V2_C_TAU_MIN_SEC or age > cfg.V2_C_TAU_MAX_SEC:
            return {"fail": "TAU_OUT_OF_RANGE", "tau_h": tau_h, "H": h120, "age": age}
        lpre, _, _, tau_lpre = series.hl(t - 120, int(tau_h) - 1)
        # hl returns last-occurrence for min; need Lpre before H
        # Recompute Lpre as min in [t-120, tau_h-1] with last time
        if lpre is None or tau_lpre is None or tau_lpre >= tau_h:
            return {"fail": "LPRE_MISSING_OR_ORDER", "tau_h": tau_h, "H": h120}
        rise = (h120 - lpre) / lpre if lpre > 0 else None
        giveback = (h120 - p) / h120 if h120 > 0 else None
        span = h120 - lpre
        give_ratio = ((h120 - p) / span) if span > 0 else None
        prior_low = series.min_in(t - 10, t - 1)
        d = {
            "tau": tau_h,
            "H": h120,
            "Lpre": lpre,
            "tau_lpre": tau_lpre,
            "rise": rise,
            "giveback": giveback,
            "give_ratio": give_ratio,
            "prior_low": prior_low,
            "age": age,
        }
        if rise is None or rise < cfg.V2_C_PRE_MOVE:
            d["fail"] = "PRE_RISE_TOO_SMALL"
            return d
        if giveback is None or giveback < cfg.V2_C_RECLAIM:
            d["fail"] = "GIVEBACK_TOO_SMALL"
            return d
        if give_ratio is None or give_ratio < cfg.V2_C_RECLAIM_RATIO:
            d["fail"] = "GIVE_RATIO_TOO_SMALL"
            return d
        if prior_low is None:
            d["fail"] = "PRIOR10_LOW_MISSING"
            return d
        if p > prior_low - tick:
            d["fail"] = "NO_BREAK_PRIOR_LOW"
            return d
        if ret10 is None or ret10 > -cfg.V2_C_RET10:
            d["fail"] = "RET10_FAIL"
            return d
        if er10 < cfg.V2_C_ER10:
            d["fail"] = "ER10_FAIL"
            return d
        d["break_px"] = prior_low - tick
        d["side"] = "SHORT"
        return d

    def _confirm_struct_ok(side: str) -> tuple[bool, Optional[dict[str, Any]]]:
        """Re-validate reversal structure without requiring a fresh breakout."""
        if side == "LONG":
            h120, l120, _, tau_l = series.hl(t - 120, t)
            if h120 is None or l120 is None or tau_l is None:
                return False, None
            age = t - int(tau_l)
            if age < cfg.V2_C_TAU_MIN_SEC or age > cfg.V2_C_TAU_MAX_SEC:
                return False, {"fail": "TAU_OUT_OF_RANGE", "age": age}
            hpre, _, tau_hpre, _ = series.hl(t - 120, int(tau_l) - 1)
            if hpre is None or tau_hpre is None or tau_hpre >= tau_l:
                return False, {"fail": "HPRE_ORDER"}
            drop = (hpre - l120) / hpre
            reclaim = (p - l120) / l120
            span = hpre - l120
            ratio = (p - l120) / span if span > 0 else 0.0
            st = {
                "tau": tau_l,
                "L": l120,
                "Hpre": hpre,
                "drop": drop,
                "reclaim": reclaim,
                "reclaim_ratio": ratio,
                "age": age,
            }
            ok = (
                drop >= cfg.V2_C_PRE_MOVE
                and reclaim >= cfg.V2_C_RECLAIM
                and ratio >= cfg.V2_C_RECLAIM_RATIO
                and ret10 is not None
                and ret10 >= cfg.V2_C_RET10
                and er10 >= cfg.V2_C_ER10
            )
            return ok, st
        h120, l120, tau_h, _ = series.hl(t - 120, t)
        if h120 is None or l120 is None or tau_h is None:
            return False, None
        age = t - int(tau_h)
        if age < cfg.V2_C_TAU_MIN_SEC or age > cfg.V2_C_TAU_MAX_SEC:
            return False, {"fail": "TAU_OUT_OF_RANGE", "age": age}
        lpre, _, _, tau_lpre = series.hl(t - 120, int(tau_h) - 1)
        if lpre is None or tau_lpre is None or tau_lpre >= tau_h:
            return False, {"fail": "LPRE_ORDER"}
        rise = (h120 - lpre) / lpre
        giveback = (h120 - p) / h120
        span = h120 - lpre
        ratio = (h120 - p) / span if span > 0 else 0.0
        st = {
            "tau": tau_h,
            "H": h120,
            "Lpre": lpre,
            "rise": rise,
            "giveback": giveback,
            "give_ratio": ratio,
            "age": age,
        }
        ok = (
            rise >= cfg.V2_C_PRE_MOVE
            and giveback >= cfg.V2_C_RECLAIM
            and ratio >= cfg.V2_C_RECLAIM_RATIO
            and ret10 is not None
            and ret10 <= -cfg.V2_C_RET10
            and er10 >= cfg.V2_C_ER10
        )
        return ok, st

    # --- Confirmation of existing arm (do NOT require new breakout) ---
    if armed and armed.get("break_px") is not None:
        side = str(armed.get("side") or "").upper()
        break_px = float(armed["break_px"])
        detail["armed"] = dict(armed)
        detail["confirm_mode"] = True
        stay = (p >= break_px) if side == "LONG" else (p <= break_px)
        struct_ok, st = _confirm_struct_ok(side)
        detail["struct"] = st
        if not stay or not struct_ok:
            return (
                PathResult("REVERSAL", False, reason="REVERSAL_CONFIRM_FAIL", detail=detail),
                None,
            )
        left = int(armed.get("confirm_left") or 0) - 1
        new_arm = dict(armed)
        new_arm["confirm_left"] = left
        new_arm["last_confirm_sec"] = t
        detail["confirm_left"] = left
        if left > 0:
            return (
                PathResult("REVERSAL", False, reason="REVERSAL_CONFIRMING", detail=detail),
                new_arm,
            )
        return (
            PathResult("REVERSAL", True, side=side, reason="OK", detail=detail),
            None,
        )

    # --- Fresh trigger ---
    long_d = _long_struct()
    short_d = _short_struct()
    detail["long_struct"] = long_d
    detail["short_struct"] = short_d

    long_ok = long_d is not None and "fail" not in long_d
    short_ok = short_d is not None and "fail" not in short_d
    if long_ok and short_ok:
        return PathResult("REVERSAL", False, reason="DIRECTION_CONFLICT", detail=detail), None
    if not long_ok and not short_ok:
        reason = "REVERSAL_FILTER_FAIL"
        if long_d and long_d.get("fail"):
            reason = f"REVERSAL_LONG:{long_d['fail']}"
        elif short_d and short_d.get("fail"):
            reason = f"REVERSAL_SHORT:{short_d['fail']}"
        return PathResult("REVERSAL", False, reason=reason, detail=detail), None

    side = "LONG" if long_ok else "SHORT"
    src = long_d if long_ok else short_d
    assert src is not None
    break_px = float(src["break_px"])
    new_arm = {
        "side": side,
        "break_px": break_px,
        "trigger_sec": t,
        "confirm_left": int(cfg.V2_C_CONFIRM_EXTRA_SEC),
        "struct": {k: v for k, v in src.items() if k != "fail"},
    }
    detail["armed_new"] = new_arm
    # Trigger second counts as first of the confirm window; need EXTRA more seconds
    return (
        PathResult("REVERSAL", False, reason="REVERSAL_ARMED", detail=detail),
        new_arm,
    )
