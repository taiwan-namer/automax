"""G7 paper/research event schema helpers. Evidence fields only; no exchange claims."""
from __future__ import annotations

from typing import Any, Optional


def next_event_seq(st: Any) -> int:
    seq = int(getattr(st, "research_event_seq", 0) or 0) + 1
    try:
        st.research_event_seq = seq
    except Exception:
        pass
    return seq


def pyramid_layer_snapshot(pos: Any) -> list[dict[str, Any]]:
    pyr = getattr(pos, "pyramid", None) or {}
    out = []
    for L in pyr.get("layers") or []:
        out.append(
            {
                "name": L.get("name"),
                "qty": L.get("qty"),
                "entry": L.get("entry"),
                "closed": bool(L.get("closed")),
                "exit_reason": L.get("exit_reason"),
                "margin_u": L.get("margin_u"),
            }
        )
    return out


def threshold_fields(
    *,
    levels: dict[str, Any] | None,
    tick: float | None,
    raw_levels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lv = dict(levels or {})
    raw = dict(raw_levels or lv)
    tick_f = float(tick or 0.0)
    out: dict[str, Any] = {"tick_size": tick_f, "price_tolerance_policy": "tick_exact_no_0p2pct_slack"}
    for name in ("P1", "P2", "Pstop", "P0_TP"):
        if name in raw:
            out[f"{name}_raw"] = raw[name]
        if name in lv:
            out[f"{name}_rounded"] = lv[name]
    return out


def enrich_paper_event(
    *,
    st: Any,
    pos: Any,
    event: str,
    book: dict[str, Any] | None = None,
    book_ts: float | None = None,
    decision_ts: float | None = None,
    fill_ts: float | None = None,
    received_ts: float | None = None,
    p0_before: float | None = None,
    p0_after: float | None = None,
    avg_before: float | None = None,
    avg_after: float | None = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fields required by research G7. Caller merges into fill row."""
    pyr = getattr(pos, "pyramid", None) or {}
    p0 = float(pyr.get("p0_entry") or getattr(pos, "entry_price", 0.0) or 0.0)
    tick = float(pyr.get("tick") or 0.0)
    levels = dict(pyr.get("levels") or {})
    row: dict[str, Any] = {
        "cycle_id": getattr(pos, "trade_id", None),
        "event_seq": next_event_seq(st),
        "event": event,
        "market_ts": book_ts,
        "received_ts": received_ts if received_ts is not None else book_ts,
        "decision_ts": decision_ts if decision_ts is not None else book_ts,
        "fill_ts": fill_ts if fill_ts is not None else book_ts,
        "p0_anchor_before": p0_before if p0_before is not None else p0,
        "p0_anchor_after": p0_after if p0_after is not None else p0,
        "position_avg_before": avg_before,
        "position_avg_after": avg_after,
        "layers_state": pyramid_layer_snapshot(pos),
        **threshold_fields(levels=levels, tick=tick),
        "fill_source": "paper_simulation",
        "execution_model_version": "paper_touch_v1",
        "is_confirmed_exchange_price": False,
    }
    if book:
        row["trigger_bid"] = book.get("bid")
        row["trigger_ask"] = book.get("ask")
        row["trigger_mark_mid"] = book.get("mid")
    if extra:
        row.update(extra)
    return row


def prices_match_tick(price: float, tick: float, *, expected: float | None = None) -> bool:
    """Strict tick check; no 0.2% slack. If expected given, both must round-equal."""
    if tick is None or tick <= 0:
        if expected is None:
            return True
        return abs(float(price) - float(expected)) <= 1e-12
    from .market_data import round_price_tick

    rp = round_price_tick(float(price), float(tick))
    if abs(rp - float(price)) > max(1e-12, float(tick) * 1e-9):
        # allow float dust relative to tick
        if abs(rp - float(price)) > float(tick) * 0.51:
            return False
    if expected is not None:
        re = round_price_tick(float(expected), float(tick))
        return abs(rp - re) <= max(1e-12, float(tick) * 1e-9)
    return True
