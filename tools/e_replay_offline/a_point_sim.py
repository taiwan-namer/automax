"""A-point counterfactual full-exit valuation (model price ≠ exchange fill).

Uses Decimal only. Missing cost inputs are never defaulted to zero.
Does not claim fillability or maker fill rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .reader import (
    _bar_close_time_ms,
    _bar_interval,
    _bar_open_time_ms,
    _bar_symbol,
    _parse_iso_to_utc,
    align_bars_window,
    sha256_file,
)


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt(d: Decimal) -> str:
    """Stable decimal string without trailing zeros (e.g. 19.580 → 19.58)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _floor_min_ms(iso: str) -> int | None:
    dt = _parse_iso_to_utc(iso)
    if dt is None:
        return None
    dt2 = dt.replace(second=0, microsecond=0)
    return int(dt2.timestamp() * 1000)


def _bar_close_px(b: Any) -> Decimal | None:
    if isinstance(b, (list, tuple)) and len(b) >= 5:
        return _d(b[4])
    if isinstance(b, dict):
        for k in ("close", "c", "Close"):
            if b.get(k) is not None:
                return _d(b[k])
    return None


@dataclass
class APointSimInputs:
    """All valuation inputs must be explicit; None means missing (not zero)."""

    symbol: str
    interval: str
    direction: str | None  # LONG | SHORT
    entry_px: Decimal | None
    remaining_qty: Decimal | None
    a_point_utc: str  # bar open time ISO
    market_cutoff_ms: int | None  # forming cutoff; None = missing
    entry_fee_rate: Decimal | None
    exit_fee_rate: Decimal | None
    slippage_rate: Decimal | None
    funding: Decimal | None  # signed; None = missing
    bars: list[Any] | None
    bars_provided: bool
    bars_source_path: str | None
    window_start_utc: str
    window_end_utc: str
    half_open_end: bool = True


MODEL_ASSUMPTIONS = {
    "price_source": "closed_bar_close_at_A",
    "fill_claim": False,
    "maker_fill_rate_claimed": False,
    "semantics": "counterfactual_model_valuation_not_exchange_fill",
    "long_exit_px": "close*(1-slippage_rate)",
    "short_exit_px": "close*(1+slippage_rate)",
    "gross_long": "(exit_px-entry_px)*qty",
    "gross_short": "(entry_px-exit_px)*qty",
    "entry_fee": "entry_px*qty*entry_fee_rate",
    "exit_fee": "exit_px*qty*exit_fee_rate",
    "net": "gross-entry_fee-exit_fee+funding",
    "missing_costs_default_to_zero": False,
}


def simulate_a_point_exit(inp: APointSimInputs) -> dict[str, Any]:
    """Return a_point valuation block. Always sets model_assumptions + source sha."""
    src_sha = sha256_file(Path(inp.bars_source_path)) if inp.bars_source_path else None
    base: dict[str, Any] = {
        "model_assumptions": MODEL_ASSUMPTIONS,
        "source_identity": {
            "path": inp.bars_source_path,
            "sha256": src_sha,
        },
        "inputs_declared": {
            "symbol": inp.symbol,
            "interval": inp.interval,
            "direction": inp.direction,
            "entry_px": str(inp.entry_px) if inp.entry_px is not None else None,
            "remaining_qty": str(inp.remaining_qty) if inp.remaining_qty is not None else None,
            "a_point_utc": inp.a_point_utc,
            "market_cutoff_ms": inp.market_cutoff_ms,
            "entry_fee_rate": str(inp.entry_fee_rate) if inp.entry_fee_rate is not None else None,
            "exit_fee_rate": str(inp.exit_fee_rate) if inp.exit_fee_rate is not None else None,
            "slippage_rate": str(inp.slippage_rate) if inp.slippage_rate is not None else None,
            "funding": str(inp.funding) if inp.funding is not None else None,
            "funding_explicit": inp.funding is not None,
        },
        "a_exit_px": None,
        "a_bar_close": None,
        "gross": None,
        "entry_fee": None,
        "exit_fee": None,
        "funding": None,
        "simulated_pnl": None,
        "full_net_pnl": None,
        "full_net_status": None,
        "components": None,
        "blocked": False,
        "blocked_reason": None,
        "status": None,
    }

    # --- input validation (direction / qty) ---
    direction = (inp.direction or "").strip().upper() if inp.direction else None
    if direction not in ("LONG", "SHORT"):
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "INVALID_DIRECTION",
            "note": "direction must be LONG or SHORT",
        }
    qty = inp.remaining_qty
    if qty is None or qty <= 0:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "INVALID_QTY",
            "note": "remaining_qty must be > 0",
        }
    if inp.entry_px is None or inp.entry_px <= 0:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "INVALID_ENTRY_PX",
            "note": "entry_px must be > 0",
        }

    a_ms = _floor_min_ms(inp.a_point_utc)
    if a_ms is None:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "INVALID_A_POINT_TIME",
        }
    if inp.market_cutoff_ms is None:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "MARKET_CUTOFF_MISSING",
            "note": "market_cutoff_ms must be explicit",
        }

    # --- bars alignment / coverage for window ---
    align = align_bars_window(
        inp.bars if inp.bars_provided else None,
        window_start_utc=inp.window_start_utc,
        window_end_utc=inp.window_end_utc,
        symbol=inp.symbol,
        interval=inp.interval,
        half_open_end=inp.half_open_end,
        now_ms=inp.market_cutoff_ms,
        bars_provided=inp.bars_provided,
        source_path=inp.bars_source_path,
    )
    base["bars_align"] = {
        "matched": align.get("matched"),
        "expected": align.get("expected"),
        "blocked": align.get("blocked"),
        "blocked_reason": align.get("blocked_reason"),
        "duplicate_keys": align.get("duplicate_keys"),
        "missing": align.get("missing"),
        "forming_n": align.get("forming_n"),
        "status": align.get("status"),
    }

    if not inp.bars_provided:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "PRICE_PATH_MISSING",
            "note": "bars_not_provided",
        }
    if align.get("duplicate_keys"):
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "DUPLICATE_BAR_KEYS",
            "duplicate_keys": align.get("duplicate_keys"),
        }

    # window bounds for A-point (diagnose before generic coverage block)
    win_start = align["window"]["start_ms"]
    win_end = align["window"]["end_ms"]
    if inp.half_open_end:
        in_win = win_start <= a_ms < win_end
    else:
        in_win = win_start <= a_ms <= win_end
    if not in_win:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "A_POINT_OUTSIDE_WINDOW",
            "a_open_ms": a_ms,
            "window_ms": {"start": win_start, "end": win_end},
        }

    # locate A bar among provided rows (include forming for explicit reason)
    candidates: list[Any] = []
    for b in inp.bars or []:
        ot = _bar_open_time_ms(b)
        if ot != a_ms:
            continue
        sym = _bar_symbol(b)
        iv = _bar_interval(b) or inp.interval
        if sym is not None and sym != str(inp.symbol).upper():
            continue
        if sym is None:
            continue
        if iv != inp.interval:
            continue
        candidates.append(b)

    if not candidates:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "PRICE_PATH_MISSING",
            "note": "a_bar_missing",
            "a_open_ms": a_ms,
        }
    if len(candidates) > 1:
        key = f"{inp.symbol.upper()}|{inp.interval}|{a_ms}"
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "DUPLICATE_BAR_KEYS",
            "duplicate_keys": [key],
        }

    bar = candidates[0]
    ct = _bar_close_time_ms(bar)
    if ct is not None and ct >= inp.market_cutoff_ms:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "A_BAR_NOT_CLOSED",
            "a_open_ms": a_ms,
            "a_close_ms": ct,
            "market_cutoff_ms": inp.market_cutoff_ms,
        }

    # broader window gaps (other minutes missing) still block valuation
    if align.get("blocked") and align.get("blocked_reason") in (
        "PRICE_PATH_MISSING",
        "BARS_COVERAGE_INCOMPLETE",
    ):
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": align.get("blocked_reason"),
            "note": align.get("blocked_note"),
        }

    close_px = _bar_close_px(bar)
    if close_px is None:
        return {
            **base,
            "status": "blocked",
            "blocked": True,
            "blocked_reason": "A_BAR_CLOSE_MISSING",
        }

    # --- costs: missing must not default to zero ---
    costs_missing: list[str] = []
    if inp.entry_fee_rate is None:
        costs_missing.append("entry_fee_rate")
    if inp.exit_fee_rate is None:
        costs_missing.append("exit_fee_rate")
    if inp.slippage_rate is None:
        costs_missing.append("slippage_rate")
    if inp.funding is None:
        costs_missing.append("funding")

    slip = inp.slippage_rate if inp.slippage_rate is not None else None
    # gross needs slippage; if slip missing, cannot compute exit_px/gross under model
    if slip is None:
        return {
            **base,
            "status": "partial_or_blocked",
            "blocked": True,
            "blocked_reason": "COST_INPUTS_MISSING",
            "costs_missing": costs_missing,
            "a_bar_close": str(close_px),
            "simulated_pnl": None,
            "full_net_pnl": None,
            "full_net_status": "not_computable",
            "gross": None,
            "note": "slippage_rate missing; cannot compute model exit without inventing zero",
        }

    one = Decimal("1")
    if direction == "LONG":
        exit_px = close_px * (one - slip)
        gross = (exit_px - inp.entry_px) * qty
    else:
        exit_px = close_px * (one + slip)
        gross = (inp.entry_px - exit_px) * qty

    components: dict[str, Any] = {
        "direction": direction,
        "entry_px": _fmt(inp.entry_px),
        "a_bar_close": _fmt(close_px),
        "slippage_rate": _fmt(slip),
        "exit_px": _fmt(exit_px),
        "qty": _fmt(qty),
        "gross": _fmt(gross),
    }

    # If fee rates or funding missing → gross may be listed with explicit口径; net not_computable
    fee_fund_missing = [c for c in costs_missing if c != "slippage_rate"]
    if fee_fund_missing:
        return {
            **base,
            "status": "gross_only_costs_incomplete",
            "blocked": False,
            "blocked_reason": None,
            "costs_missing": fee_fund_missing,
            "a_exit_px": _fmt(exit_px),
            "a_bar_close": _fmt(close_px),
            "gross": _fmt(gross),
            "gross_口径": "model_exit_with_declared_slippage_fees_funding_excluded",
            "entry_fee": None,
            "exit_fee": None,
            "funding": None,
            "simulated_pnl": None,
            "full_net_pnl": None,
            "full_net_status": "not_computable",
            "components": components,
            "note": "缺成本不得默認為零；僅列明示口徑毛利",
        }

    entry_fee = inp.entry_px * qty * inp.entry_fee_rate  # type: ignore[operator]
    exit_fee = exit_px * qty * inp.exit_fee_rate  # type: ignore[operator]
    funding = inp.funding  # type: ignore[assignment]
    net = gross - entry_fee - exit_fee + funding

    components.update(
        {
            "entry_fee_rate": _fmt(inp.entry_fee_rate),  # type: ignore[arg-type]
            "exit_fee_rate": _fmt(inp.exit_fee_rate),  # type: ignore[arg-type]
            "entry_fee": _fmt(entry_fee),
            "exit_fee": _fmt(exit_fee),
            "funding": _fmt(funding),  # type: ignore[arg-type]
            "net": _fmt(net),
        }
    )

    return {
        **base,
        "status": "simulated",
        "blocked": False,
        "blocked_reason": None,
        "a_exit_px": _fmt(exit_px),
        "a_bar_close": _fmt(close_px),
        "gross": _fmt(gross),
        "entry_fee": _fmt(entry_fee),
        "exit_fee": _fmt(exit_fee),
        "funding": _fmt(funding),  # type: ignore[arg-type]
        "simulated_pnl": _fmt(net),
        "full_net_pnl": _fmt(net),
        "full_net_status": "computed_under_model_assumptions",
        "components": components,
    }


def semantic_report_diff(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    exclude_keys: frozenset[str] | None = None,
) -> list[str]:
    """Compare two reports; return list of differing paths. Excludes volatile fields."""
    excl = exclude_keys or frozenset(
        {
            "generated_at",
            "generated_at_utc",
            "report_path",
            "out_dir",
            "out_path",
            "write_path",
            "cli_out_dir",
        }
    )

    diffs: list[str] = []

    def walk(x: Any, y: Any, path: str) -> None:
        key = path.rsplit(".", 1)[-1] if path else ""
        if key in excl or path in excl:
            return
        if type(x) != type(y) and not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
            # allow None vs missing handled by caller
            if x != y:
                diffs.append(path or "$")
            return
        if isinstance(x, dict) and isinstance(y, dict):
            keys = set(x) | set(y)
            for k in sorted(keys):
                if k in excl:
                    continue
                p = f"{path}.{k}" if path else k
                if k not in x or k not in y:
                    diffs.append(p)
                else:
                    walk(x[k], y[k], p)
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                diffs.append(path or "$")
                return
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{path}[{i}]")
        else:
            if x != y:
                diffs.append(path or "$")

    walk(a, b, "")
    return diffs
