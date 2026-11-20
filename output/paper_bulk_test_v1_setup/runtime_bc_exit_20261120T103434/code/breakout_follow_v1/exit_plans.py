"""Version-isolated exit plans for paper B/C (simulation fills only).

Legacy positions/pending without exit_plan_id keep fixed-gross TP50/SL75 barriers
already stored on the position — never rewritten by new profile defaults.
"""

from __future__ import annotations

from typing import Any, Optional

from . import config
from .market_data import round_price_tick, round_step


LEGACY = "legacy_fixed_gross_tp50_sl75"
B_PARTIAL70 = "b_partial70_30_tp50_tp100_20261120"
C_PYRAMID = "c_pyramid300_r2_p0tp1pct_20261120"


def resolve_exit_plan_id(obj: Any) -> str:
    """Position dict / Position / pending → plan id (legacy if missing)."""
    if obj is None:
        return LEGACY
    if isinstance(obj, dict):
        v = obj.get("exit_plan_id")
    else:
        v = getattr(obj, "exit_plan_id", None)
    if v is None or str(v).strip() == "":
        return LEGACY
    return str(v)


def stamp_legacy_account(st) -> dict[str, Any]:
    """Idempotent: stamp existing positions/pending as legacy; do not change barriers."""
    n_pos = n_pend = 0
    for sym, pd in list((st.positions or {}).items()):
        if not isinstance(pd, dict):
            continue
        if not pd.get("exit_plan_id"):
            pd["exit_plan_id"] = LEGACY
            pd["code_fix_tag"] = pd.get("code_fix_tag") or "pre_20261120_exit_upgrade"
            n_pos += 1
        st.positions[sym] = pd
    pending = getattr(st, "pending_limit_entries", None) or {}
    for sym, pend in list(pending.items()):
        if not isinstance(pend, dict):
            continue
        if not pend.get("exit_plan_id"):
            pend["exit_plan_id"] = LEGACY
            pend["notional_u"] = float(pend.get("notional_u") or 10_000.0)
            pend["margin_u"] = float(pend.get("margin_u") or 1_000.0)
            pend["code_fix_tag"] = pend.get("code_fix_tag") or "pre_20261120_exit_upgrade"
            n_pend += 1
        pending[sym] = pend
    st.pending_limit_entries = pending
    return {"stamped_positions": n_pos, "stamped_pending": n_pend}


def pyramid_levels(side: str, p0: float, tick: float, add_pct: float) -> dict[str, float]:
    """Theoretical P1/P2/Pstop from P0 (even if adds not filled)."""
    if side == "LONG":
        p1 = p0 * (1.0 - add_pct)
        p2 = p1 * (1.0 - add_pct)
        pstop = p2 * (1.0 - add_pct)
        tp = p0 * (1.0 + float(getattr(config, "PYRAMID_P0_TP_PCT", 0.01) or 0.01))
    else:
        p1 = p0 * (1.0 + add_pct)
        p2 = p1 * (1.0 + add_pct)
        pstop = p2 * (1.0 + add_pct)
        tp = p0 * (1.0 - float(getattr(config, "PYRAMID_P0_TP_PCT", 0.01) or 0.01))
    if tick and tick > 0:
        p1 = round_price_tick(p1, tick)
        p2 = round_price_tick(p2, tick)
        pstop = round_price_tick(pstop, tick)
        tp = round_price_tick(tp, tick)
    return {"P1": p1, "P2": p2, "Pstop": pstop, "P0_TP": tp}


def build_pyramid_state(
    *,
    side: str,
    p0: float,
    qty: float,
    margin_u: float,
    tick: float,
    step: float,
    notional_u: float,
) -> dict[str, Any]:
    add_pct = float(getattr(config, "PYRAMID_ADD_PCT", 0.005) or 0.005)
    lv = pyramid_levels(side, p0, tick, add_pct)
    return {
        "p0_entry": p0,
        "rebound_exit_price": p0,  # P1/P2 exit at P0 entry (not fee BE)
        "levels": lv,
        "add_pct": add_pct,
        "notional_per": notional_u,
        "margin_per": margin_u,
        "tick": tick,
        "step": step,
        "layers": [
            {
                "name": "P0",
                "entry": p0,
                "qty": qty,
                "margin_u": margin_u,
                "closed": False,
                "exit_reason": None,
            }
        ],
        # 不循環補倉：退出後 status=CANCELLED_NO_REFILL
        "pending_adds": [
            {"name": "P1", "limit_price": lv["P1"], "status": "PENDING"},
            {"name": "P2", "limit_price": lv["P2"], "status": "WAITING_P1"},
        ],
        "stop_frozen": True,  # Pstop 不隨均價／時間刷新
    }


def fixed_gross_barriers(
    *,
    side: str,
    entry: float,
    qty: float,
    tp_usdt: float,
    sl_usdt: float,
    tick: float,
    tp2_usdt: float = 0.0,
) -> dict[str, Any]:
    d_tp = tp_usdt / qty
    d_sl = sl_usdt / qty
    if side == "LONG":
        tp_raw = entry + d_tp
        sl_raw = entry - d_sl
        tp2_raw = (entry + (tp2_usdt / qty)) if tp2_usdt > 0 else None
    else:
        tp_raw = entry - d_tp
        sl_raw = entry + d_sl
        tp2_raw = (entry - (tp2_usdt / qty)) if tp2_usdt > 0 else None
    tp = round_price_tick(tp_raw, tick) if tick > 0 else tp_raw
    sl = round_price_tick(sl_raw, tick) if tick > 0 else sl_raw
    tp2 = round_price_tick(tp2_raw, tick) if (tp2_raw and tick > 0) else tp2_raw
    return {
        "tp_raw": tp_raw,
        "sl_raw": sl_raw,
        "tp2_raw": tp2_raw,
        "take_profit": tp,
        "take_profit_2": tp2,
        "stop": sl,
    }


def open_sizing_from_plan(
    plan: str,
    *,
    signal: dict[str, Any],
    pend: Optional[dict[str, Any]] = None,
) -> tuple[float, float]:
    """Return (notional_u, margin_u) respecting stamped pending/legacy."""
    pend = pend or {}
    if plan == LEGACY or (pend.get("exit_plan_id") == LEGACY):
        return (
            float(pend.get("notional_u") or signal.get("notional_u") or 10_000.0),
            float(pend.get("margin_u") or signal.get("margin_u") or 1_000.0),
        )
    return (
        float(pend.get("notional_u") or signal.get("notional_u") or config.NOTIONAL_USDT),
        float(pend.get("margin_u") or signal.get("margin_u") or config.MARGIN_PER_TRADE_USDT),
    )


def cancel_pyramid_residual_adds(pyramid: dict[str, Any], reason: str = "CYCLE_END") -> None:
    for a in pyramid.get("pending_adds") or []:
        if a.get("status") in ("PENDING", "WAITING_P1"):
            a["status"] = f"CANCELLED_{reason}"
