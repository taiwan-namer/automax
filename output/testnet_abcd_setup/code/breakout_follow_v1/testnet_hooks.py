"""Testnet order hooks for B/C (only active when ENABLE_BINANCE_TESTNET_ORDER=1)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

def _project_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / ".env.testnet").is_file() or (p / "common" / "binance_testnet_adapter.py").is_file():
            return p
    return here.parents[4]


_ROOT = _project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.testnet_bridge import (  # noqa: E402
    algo_stop_still_open,
    close_side_for_position,
    get_adapter,
    side_to_exchange,
    testnet_mode_active,
    try_cancel,
    try_close_position,
    try_place_entry,
    try_place_reduce_only_limit,
    try_place_stop_loss,
)
from common.testnet_env import orders_enabled  # noqa: E402

from . import config, exit_plans  # noqa: E402


def enabled() -> bool:
    if bool(getattr(config, "EXCHANGE_ORDERS_HARD_DISABLED", False)):
        return False
    return bool(orders_enabled() and testnet_mode_active())


def strategy_from_profile(profile_id: str) -> str:
    pid = (profile_id or "").lower()
    if "inv" in pid or pid.startswith("testnet_c"):
        return "C"
    return "B"


def strategy_stop_price(
    *,
    side: str,
    entry_price: float,
    qty: float,
    tick: float = 0.0,
    profile_id: str = "",
) -> float:
    """B: fixed-gross SL USDT → price; C pyramid: Pstop. Uses live config (profile applied)."""
    side_u = side.upper()
    entry = float(entry_price)
    q = float(qty)
    tk = float(tick or 0.0)
    plan = str(getattr(config, "EXIT_PLAN_ID", "") or "")
    # C pyramid
    if "pyramid" in plan.lower() or strategy_from_profile(profile_id) == "C":
        add_pct = float(getattr(config, "PYRAMID_ADD_PCT", 0.005) or 0.005)
        return float(exit_plans.pyramid_levels(side_u, entry, tk, add_pct)["Pstop"])
    # B fixed gross
    sl_usdt = float(getattr(config, "FIXED_GROSS_SL_USDT", 0.0) or 0.0)
    if sl_usdt > 0 and q > 0:
        bars = exit_plans.fixed_gross_barriers(
            side=side_u, entry=entry, qty=q, tp_usdt=1.0, sl_usdt=sl_usdt, tick=tk
        )
        return float(bars["stop"])
    # ATR fallback (should not hit on testnet B/C)
    atr_mult = float(getattr(config, "ATR_STOP_MULT", 1.5) or 1.5)
    # without atr, use 1.5% placeholder is wrong — refuse
    raise ValueError("cannot derive strategy stop without FIXED_GROSS_SL / pyramid")


def place_entry_for_pending(
    pending: dict[str, Any],
    *,
    qty: float,
    profile_id: str,
    layer: str | None = None,
) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True, "reason": "testnet_off"}
    strat = strategy_from_profile(profile_id)
    side = str(pending.get("side") or "")
    symbol = str(pending.get("symbol") or "")
    px = float(pending.get("limit_price") or 0)
    tick = float((pending.get("filters") or {}).get("tickSize") or 0)
    # C/B 皆進場掛停損：C 用理論 Pstop + closePosition（暴跌穿 Pstop 立刻整倉出貨，
    # 不依賴等 P2 成交後才掛；P1/P2 限價在暴跌中本來就會成交進倉）
    try:
        stop_px = strategy_stop_price(
            side=side, entry_price=px, qty=float(qty), tick=tick, profile_id=profile_id
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"STOP_PRICE_DERIVE_FAIL:{exc}", "strategy": strat}
    res = try_place_entry(
        strategy=strat,
        symbol=symbol,
        side=side,
        quantity=float(qty),
        price=px,
        layer=layer,
        post_only=False,
        place_stop=True,
        stop_price=stop_px,
    )
    out = {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "status": res.status,
        "error": res.error,
        "strategy": strat,
        "stop_price": stop_px,
        "place_stop": True,
    }
    if res.ok:
        pending["testnet_client_order_id"] = res.client_order_id
        pending["testnet_exchange_order_id"] = res.exchange_order_id
        pending["testnet_strategy"] = strat
        sl = (res.raw or {}).get("stop_loss") or {}
        if sl.get("client_order_id"):
            pending["testnet_sl_client_order_id"] = sl.get("client_order_id")
            pending["testnet_sl_exchange_order_id"] = sl.get("exchange_order_id")
            pending["testnet_sl_stop_price"] = sl.get("stop_price") or stop_px
            print(
                f"[testnet-{strat}] SL_ATTACHED {symbol} stop={pending['testnet_sl_stop_price']} "
                f"cid={sl.get('client_order_id')} ok={sl.get('ok')} closePosition=True",
                flush=True,
            )
        elif sl:
            print(f"[testnet-{strat}] SL_ATTACH_FAIL {symbol}: {sl.get('error')}", flush=True)
    return out


def ensure_c_pstop_after_p2(
    *,
    symbol: str,
    side: str,
    stop_price: float,
    profile_id: str = "",
    prev_sl_client_order_id: str | None = None,
) -> dict[str, Any]:
    """C rule: only after P2 fill — STOP_MARKET closePosition at Pstop (covers P0+P1+P2)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    if prev_sl_client_order_id:
        try_cancel(strategy=strat, symbol=symbol, client_order_id=str(prev_sl_client_order_id))
    res = try_place_stop_loss(
        strategy=strat,
        symbol=symbol,
        side=side,
        stop_price=float(stop_price),
        quantity=None,  # closePosition path
    )
    if res.ok:
        print(
            f"[testnet-C] PSTOP_AFTER_P2 {symbol} stop={stop_price} "
            f"cid={res.client_order_id} closePosition=True",
            flush=True,
        )
    else:
        print(f"[testnet-C] PSTOP_AFTER_P2_FAIL {symbol}: {res.error}", flush=True)
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "stop_price": (res.raw or {}).get("stopPrice") or stop_price,
        "error": res.error,
        "strategy": strat,
    }


def place_tp_limit(
    *,
    symbol: str,
    position_side: str,
    qty: float,
    price: float,
    level: str,
    profile_id: str = "",
) -> dict[str, Any]:
    """Exchange reduce-only LIMIT for B TP1/TP2 or C P0_TP."""
    if not enabled():
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    try:
        close_side = close_side_for_position(position_side)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "strategy": strat}
    lu = level.upper()
    if lu.startswith("R2"):
        # R2_P1 / R2_P2 → layer tag for clientOrderId
        lyr = re.sub(r"[^A-Z0-9]", "", lu)[:4] or "R2"
    elif "TP1" in lu or (lu.endswith("1") and "R2" not in lu):
        lyr = "TP1"
    elif "TP2" in lu or (lu.endswith("2") and "R2" not in lu):
        lyr = "TP2"
    elif "P0" in lu:
        lyr = "P0TP"
    else:
        lyr = "TP"
    res = try_place_reduce_only_limit(
        strategy=strat,
        symbol=symbol,
        side=close_side,
        quantity=float(qty),
        price=float(price),
        layer=lyr,
    )
    if res.ok:
        print(
            f"[testnet-{strat}] TP_PLACED {symbol} {level} px={price} qty={qty} "
            f"side={close_side} cid={res.client_order_id}",
            flush=True,
        )
    else:
        print(f"[testnet-{strat}] TP_PLACE_FAIL {symbol} {level}: {res.error}", flush=True)
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "strategy": strat,
        "side": close_side,
    }


def place_pyramid_add_limit(
    *,
    symbol: str,
    position_side: str,
    qty: float,
    price: float,
    layer: str,
    profile_id: str = "",
) -> dict[str, Any]:
    """Hang P1/P2 add as opening LIMIT (not reduceOnly, no extra SL)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    lyr = re.sub(r"[^A-Z0-9]", "", str(layer).upper())[:4] or "ADD"
    res = try_place_entry(
        strategy=strat,
        symbol=symbol,
        side=position_side,
        quantity=float(qty),
        price=float(price),
        layer=lyr,
        post_only=False,
        place_stop=False,
    )
    if res.ok:
        print(
            f"[testnet-{strat}] ADD_PLACED {symbol} {layer} px={price} qty={qty} "
            f"side={side_to_exchange(position_side)} cid={res.client_order_id}",
            flush=True,
        )
    else:
        print(f"[testnet-{strat}] ADD_PLACE_FAIL {symbol} {layer}: {res.error}", flush=True)
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "strategy": strat,
    }


def cancel_cid(*, symbol: str, client_order_id: str, profile_id: str = "") -> dict[str, Any]:
    if not enabled() or not client_order_id:
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    res = try_cancel(strategy=strat, symbol=symbol, client_order_id=str(client_order_id))
    return {"ok": res.ok, "error": res.error, "status": res.status, "strategy": strat}


def poll_order(*, symbol: str, client_order_id: str, profile_id: str = "") -> dict[str, Any] | None:
    if not enabled() or not client_order_id:
        return None
    strat = strategy_from_profile(profile_id)
    ad = get_adapter(strat)
    try:
        raw = ad.query_order(symbol=symbol, client_order_id=str(client_order_id))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {
        "status": str(raw.get("status") or ""),
        "executed_qty": float(raw.get("executedQty") or 0),
        "avg_price": float(raw.get("avgPrice") or 0),
        "raw": raw,
    }


def fetch_user_trades(
    *,
    symbol: str,
    profile_id: str = "",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    from_id: int | None = None,
    limit: int = 1000,
    max_pages: int = 20,
) -> dict[str, Any]:
    from common.testnet_bridge import fetch_user_trades as _fetch

    if not enabled():
        return {
            "ok": False,
            "trades": [],
            "incomplete": True,
            "pages": 0,
            "error": "TESTNET_ORDERS_DISABLED",
            "symbol": str(symbol).upper(),
        }
    strat = strategy_from_profile(profile_id)
    return _fetch(
        strat,
        symbol=symbol,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        from_id=from_id,
        limit=limit,
        max_pages=max_pages,
    )


def ensure_stop_on_open(
    *,
    symbol: str,
    side: str,
    stop_price: float,
    qty: float,
    profile_id: str = "",
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Place/reuse exchange SL. Never reuse a provisional algo that is no longer open.

    Same DODOX hole as A: entry LIMIT not filled yet → provisional SL triggers →
    REJECTED → OPEN must not treat dead cid as protection.
    """
    if not enabled():
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    stop_px = float(stop_price)
    if pending and pending.get("testnet_sl_client_order_id"):
        prev_cid = str(pending.get("testnet_sl_client_order_id") or "")
        prev_ex = pending.get("testnet_sl_exchange_order_id")
        prev = float(pending.get("testnet_sl_stop_price") or 0)
        alive = algo_stop_still_open(
            strategy=strat,
            symbol=symbol,
            client_order_id=prev_cid,
            exchange_order_id=prev_ex,
        )
        if (
            alive
            and prev > 0
            and abs(prev - stop_px) / max(stop_px, 1e-12) < 1e-6
        ):
            return {
                "ok": True,
                "reused": True,
                "client_order_id": prev_cid,
                "exchange_order_id": prev_ex,
                "stop_price": prev,
                "strategy": strat,
            }
        if alive:
            try_cancel(strategy=strat, symbol=symbol, client_order_id=prev_cid)
        else:
            print(
                f"[testnet-{strat}] SL_STALE_REHANG {symbol} prev_cid={prev_cid} "
                f"(algo not open — rejected/canceled/finished)",
                flush=True,
            )
        for k in (
            "testnet_sl_client_order_id",
            "testnet_sl_exchange_order_id",
            "testnet_sl_stop_price",
        ):
            pending.pop(k, None)
    res = try_place_stop_loss(
        strategy=strat,
        symbol=symbol,
        side=side,
        stop_price=stop_px,
        quantity=float(qty),
    )
    if not res.ok:
        print(f"[testnet-{strat}] SL_ON_OPEN_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(
            f"[testnet-{strat}] SL_ON_OPEN {symbol} stop={(res.raw or {}).get('stopPrice') or stop_px} "
            f"cid={res.client_order_id}",
            flush=True,
        )
    out = {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "stop_price": (res.raw or {}).get("stopPrice") or stop_px,
        "error": res.error,
        "strategy": strat,
    }
    if pending is not None and res.ok and res.client_order_id:
        pending["testnet_sl_client_order_id"] = res.client_order_id
        pending["testnet_sl_exchange_order_id"] = res.exchange_order_id
        pending["testnet_sl_stop_price"] = out["stop_price"]
    return out


def cancel_pending_exchange(pending: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    symbol = pending.get("symbol")
    strat = pending.get("testnet_strategy") or strategy_from_profile(str(pending.get("profile_id") or ""))
    errs = []
    for key in ("testnet_client_order_id", "testnet_sl_client_order_id"):
        cid = pending.get(key)
        if cid and symbol:
            res = try_cancel(strategy=str(strat), symbol=str(symbol), client_order_id=str(cid))
            if not res.ok:
                errs.append(f"{key}:{res.error}")
    return {"ok": not errs, "error": ";".join(errs) if errs else None}


def close_position_exchange(
    *,
    symbol: str,
    side: str,
    qty: float,
    profile_id: str = "",
) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    strat = strategy_from_profile(profile_id)
    res = try_close_position(strategy=strat, symbol=symbol, side=side, quantity=qty)
    if not res.ok:
        print(f"[testnet-{strat}] CLOSE_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(f"[testnet-{strat}] CLOSE_OK {symbol} qty={qty} cid={res.client_order_id}", flush=True)
    raw = res.raw if isinstance(res.raw, dict) else {}
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "strategy": strat,
        "raw": raw,
        "executed_qty": float(raw.get("executedQty") or 0),
        "avg_price": float(raw.get("avgPrice") or 0) or None,
    }


def poll_fill(pending: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return fill dict if exchange order is filled/partial; None if still open.

    On CANCELED/EXPIRED, qty reflects actual executedQty (may be partial) — never forced to 0.
    """
    if not enabled():
        return None
    cid = pending.get("testnet_client_order_id")
    symbol = pending.get("symbol")
    strat = pending.get("testnet_strategy") or strategy_from_profile(str(pending.get("profile_id") or ""))
    if not cid or not symbol:
        return None
    ad = get_adapter(str(strat))
    try:
        raw = ad.query_order(symbol=str(symbol), client_order_id=str(cid))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    status = str(raw.get("status") or "")
    executed = float(raw.get("executedQty") or 0)
    avg = float(raw.get("avgPrice") or 0)
    px = avg if avg > 0 else float(pending.get("limit_price") or 0)
    oid = raw.get("orderId")
    if status in ("FILLED", "PARTIALLY_FILLED") and executed > 0:
        return {
            "status": status,
            "qty": executed,
            "price": px,
            "raw": raw,
            "executed_qty": executed,
            "exchange_order_id": oid,
        }
    if status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
        return {
            "status": "CANCELED" if status == "CANCELLED" else status,
            "qty": executed,
            "price": px if executed > 0 else 0.0,
            "raw": raw,
            "executed_qty": executed,
            "exchange_order_id": oid,
        }
    return None
