"""D observation-only helpers: shadow limits + observational_touch (NOT fills / NOT PnL)."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from . import config
from .util import append_jsonl, now_iso, now_ts, round_price_tick


FORBIDDEN_OLD_RUNTIME_MARKERS = (
    "paper_bcd_local",
    "data_d",
    "testnet_d_quiet_short_v1",
)


def assert_isolated_runtime_root(root: Any) -> None:
    """Refuse known old D ledger roots."""
    s = str(root).replace("\\", "/").lower()
    hits = [m for m in FORBIDDEN_OLD_RUNTIME_MARKERS if m.lower() in s]
    # allow substring only when clearly a new observe tree that happens to mention profile id in output path
    if "paper_bcd_local" in s or s.rstrip("/").endswith("/data_d") or "/data_d/" in s:
        raise SystemExit(
            f"ERROR: observe-only refuses old D runtime_root (forbid paper_bcd_local/data_d): {root}"
        )


def obs_paths() -> dict[str, Any]:
    p = config.paths()
    root = p["root"]
    return {
        **p,
        "observation_events": root / "observation_events.jsonl",
        "shadow_lifecycle": root / "shadow_lifecycle.jsonl",
        "observation_health": root / "observation_health.json",
        "observation_meta": root / "OBSERVATION_META.json",
    }


def _obs_event(row: dict[str, Any]) -> None:
    append_jsonl(
        obs_paths()["observation_events"],
        {
            "recorded_at": now_iso(),
            "mode": "OBSERVATION_ONLY",
            "observational": True,
            "not_a_fill": True,
            "not_pnl": True,
            **row,
        },
    )


def _shadow_log(row: dict[str, Any]) -> None:
    append_jsonl(
        obs_paths()["shadow_lifecycle"],
        {
            "recorded_at": now_iso(),
            "mode": "OBSERVATION_ONLY",
            "observational": True,
            "not_a_fill": True,
            "not_pnl": True,
            **row,
        },
    )


def make_shadow_limit(signal: dict[str, Any], filters: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Create in-memory shadow limit from signal — never writes account pending / fills."""
    P = float(signal["P"])
    tick = float(filters.get("tickSize") or 0.0001)
    raw_limit = P * (1.0 + config.LIMIT_ENTRY_OFFSET_PCT)
    limit_px = round_price_tick(raw_limit, tick)
    bars = signal.get("bars") or []
    bar_open = None
    bar_close = None
    if bars:
        last = bars[-1]
        bar_open = last.get("open_time")
        bar_close = last.get("close_time")
    shadow = {
        "shadow_id": f"{run_id}:{signal['symbol']}:{uuid.uuid4().hex[:10]}",
        "signal_id": signal.get("signal_id"),
        "symbol": signal["symbol"],
        "side": "SHORT",
        "P": P,
        "shadow_limit_px": limit_px,
        "limit_px_raw": raw_limit,
        "actual_offset_pct": (limit_px / P - 1.0) if P > 0 else None,
        "B": signal.get("B"),
        "T": signal.get("T"),
        "bar_amps": signal.get("bar_amps"),
        "range_amp": signal.get("range_amp"),
        "slope": signal.get("slope"),
        "spread_at_signal": signal.get("spread"),
        "price_source": signal.get("price_source"),
        "signal_price_ts": signal.get("price_ts"),
        "signal_emitted_at": signal.get("emitted_at") or now_iso(),
        "ref_15m_open_time": bar_open,
        "ref_15m_close_time": bar_close,
        "fill_allowed_after_ms": int(signal.get("fill_allowed_after_ms") or 0),
        "queued_at": now_iso(),
        "queued_ts": now_ts(),
        "status": "SHADOW_OPEN",
        "last_recheck_at": None,
        "last_recheck_ts": None,
        "touch_count": 0,
        "cancel_reason": None,
        "roll_samples": [],
        "roll_hi": None,
        "roll_lo": None,
        "settings_tag": "NEW_OBSERVATION_SETTINGS",
        "note": "shadow only — observational_touch is NOT a fill / NOT PnL",
    }
    _shadow_log({"event": "SHADOW_LIMIT_OPEN", **shadow})
    _obs_event(
        {
            "event": "SHADOW_LIMIT_OPEN",
            "shadow_id": shadow["shadow_id"],
            "signal_id": shadow["signal_id"],
            "symbol": shadow["symbol"],
            "P": P,
            "shadow_limit_px": limit_px,
            "ref_15m_open_time": bar_open,
            "ref_15m_close_time": bar_close,
            "range_amp": shadow["range_amp"],
            "bar_amps": shadow["bar_amps"],
        }
    )
    return shadow


def _update_roll(shadow: dict[str, Any], mid: float, ts: float) -> float:
    samples = list(shadow.get("roll_samples") or [])
    samples.append({"ts": ts, "px": float(mid)})
    cutoff = ts - 900.0
    samples = [s for s in samples if float(s["ts"]) >= cutoff]
    shadow["roll_samples"] = samples
    if not samples:
        return 0.0
    hi = max(float(s["px"]) for s in samples)
    lo = min(float(s["px"]) for s in samples)
    shadow["roll_hi"] = hi
    shadow["roll_lo"] = lo
    if lo <= 0:
        return 0.0
    return (hi - lo) / lo


def recheck_shadow(
    shadow: dict[str, Any],
    book: dict[str, Any],
    *,
    eligibility_fail: Optional[str] = None,
    one_m: Optional[dict[str, Any]] = None,
    btc_book: Optional[dict[str, Any]] = None,
    eth_book: Optional[dict[str, Any]] = None,
    scan_bucket_iso: Optional[str] = None,
    exec_order: str = "revalidate_then_touch",
) -> dict[str, Any]:
    """Manage one shadow: revalidate conditions, then observational touch check.

    Default order: revalidate (quiet/depth/PRICE_DROP) THEN touch — recorded explicitly.
    Touch uses bid >= shadow_limit for SHORT (same comparison as paper fill model),
    tagged observational_touch — NOT a fill.
    """
    now = now_ts()
    recv_at = now_iso()
    shadow["last_recheck_at"] = recv_at
    shadow["last_recheck_ts"] = now
    signal_age_sec = None
    if shadow.get("queued_ts"):
        signal_age_sec = now - float(shadow["queued_ts"])

    bid = float(book.get("bid") or 0)
    ask = float(book.get("ask") or 0)
    mid = float(book.get("mid") or 0)
    fetched_at = float(book.get("fetched_at") or 0)
    quote_age = now - fetched_at if fetched_at else None
    stale = bool(quote_age is not None and quote_age > config.QUOTE_MAX_AGE_SEC)
    data_valid = bool(bid > 0 and ask > 0 and mid > 0 and ask >= bid and not stale)

    vol_amp = _update_roll(shadow, mid, now) if mid > 0 else 0.0
    T = float(shadow.get("T") or config.VOL_EXIT_FLOOR)

    checks: dict[str, Any] = {
        "data_valid": data_valid,
        "stale_quote": stale,
        "quote_age_sec": quote_age,
        "eligibility_fail": eligibility_fail,
        "price_drop": False,
        "observational_touch": False,
        "touch_price_kind": "bid",
        "touch_compare": "bid_ge_shadow_limit_px",
        "exec_order": exec_order,
    }

    P = float(shadow["P"])
    if data_valid and mid <= P * (1.0 - config.CANCEL_DROP_FROM_P_PCT):
        checks["price_drop"] = True

    outcome = "ACTIVE"
    cancel_reason = None

    # --- revalidate first ---
    if not data_valid:
        outcome = "DATA_INVALID"
        checks["data_invalid_reason"] = "stale_or_bad_book"
    elif eligibility_fail:
        outcome = "SHADOW_CANCEL"
        cancel_reason = eligibility_fail
    elif checks["price_drop"]:
        outcome = "SHADOW_CANCEL"
        cancel_reason = "PRICE_DROP_FROM_P"

    # --- then touch (only if still active) ---
    touch = False
    if outcome == "ACTIVE" and data_valid:
        limit_px = float(shadow["shadow_limit_px"])
        allow_ms = int(shadow.get("fill_allowed_after_ms") or 0)
        now_ms = int(time.time() * 1000)
        if now_ms >= allow_ms and bid >= limit_px:
            touch = True
            checks["observational_touch"] = True
            shadow["touch_count"] = int(shadow.get("touch_count") or 0) + 1
            outcome = "OBSERVATIONAL_TOUCH"
            _shadow_log(
                {
                    "event": "OBSERVATIONAL_TOUCH",
                    "shadow_id": shadow["shadow_id"],
                    "signal_id": shadow["signal_id"],
                    "symbol": shadow["symbol"],
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "shadow_limit_px": limit_px,
                    "touch_price_kind": "bid",
                    "touch_compare": "bid_ge_shadow_limit_px",
                    "note": "NOT a fill; NOT PnL; observational only",
                }
            )

    if cancel_reason:
        shadow["status"] = "SHADOW_CANCELED"
        shadow["cancel_reason"] = cancel_reason
        _shadow_log(
            {
                "event": "SHADOW_CANCEL",
                "shadow_id": shadow["shadow_id"],
                "signal_id": shadow["signal_id"],
                "symbol": shadow["symbol"],
                "reason": cancel_reason,
            }
        )
    elif touch:
        # keep shadow open after touch (observation continues) unless canceled
        shadow["status"] = "SHADOW_TOUCHED"
    else:
        shadow["status"] = "SHADOW_OPEN"

    row = {
        "event": "SHADOW_RECHECK",
        "shadow_id": shadow["shadow_id"],
        "signal_id": shadow["signal_id"],
        "symbol": shadow["symbol"],
        "outcome": outcome,
        "cancel_reason": cancel_reason,
        "signal_age_sec": signal_age_sec,
        "last_recheck_at": recv_at,
        "scan_time_bucket": scan_bucket_iso,
        "ref_15m_open_time": shadow.get("ref_15m_open_time"),
        "ref_15m_close_time": shadow.get("ref_15m_close_time"),
        "P": shadow.get("P"),
        "shadow_limit_px": shadow.get("shadow_limit_px"),
        "range_amp": shadow.get("range_amp"),
        "bar_amps": shadow.get("bar_amps"),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "quote_fetched_at": fetched_at,
        "quote_received_at": recv_at,
        "vol_amp": vol_amp,
        "vol_T": T,
        "roll_n": len(shadow.get("roll_samples") or []),
        "roll_hi": shadow.get("roll_hi"),
        "roll_lo": shadow.get("roll_lo"),
        "one_m_closed": one_m,
        "btc": btc_book,
        "eth": eth_book,
        "checks": checks,
        "settings_tag": "NEW_OBSERVATION_SETTINGS",
    }
    _obs_event(row)
    return row


def record_quote_vol_sample(
    *,
    symbol: str,
    book: dict[str, Any],
    roll_hi: Any = None,
    roll_lo: Any = None,
    roll_n: int = 0,
    T: Any = None,
    data_valid: bool = True,
    invalid_reason: Optional[str] = None,
) -> None:
    append_jsonl(
        obs_paths()["quote_vol"],
        {
            "at": now_iso(),
            "symbol": symbol,
            "bid": book.get("bid"),
            "ask": book.get("ask"),
            "mid": book.get("mid"),
            "fetched_at": book.get("fetched_at"),
            "received_at": now_iso(),
            "roll_hi": roll_hi,
            "roll_lo": roll_lo,
            "roll_n": roll_n,
            "T": T,
            "price_kind": "bookTicker_mid_bid_ask",
            "mode": "OBSERVATION_ONLY",
            "data_valid": data_valid,
            "invalid_reason": invalid_reason,
        },
    )
