"""Testnet hooks for strategy H — Binance wiring aligned with E (bridge/adapter).

Strategy rules (trail %, limit exit, no fixed TP) stay in portfolio/signals.
This module only maps those intents onto the shared Testnet adapter.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "common" / "binance_testnet_adapter.py").is_file():
            return p
    return here.parents[4]


_ROOT = _project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.testnet_bridge import (  # noqa: E402
    algo_stop_still_open,
    get_adapter,
    try_cancel,
    try_close_position,
    try_place_entry,
    try_place_reduce_only_limit,
    try_place_stop_loss,
)
from common.testnet_env import orders_enabled  # noqa: E402

from . import config  # noqa: E402
from .metrics import trail_stop  # noqa: E402

STRATEGY = "H"


def enabled() -> bool:
    return orders_enabled()


def cancel_cid(*, symbol: str, client_order_id: str) -> dict[str, Any]:
    if not enabled() or not client_order_id:
        return {"ok": True, "skipped": True}
    res = try_cancel(strategy=STRATEGY, symbol=symbol, client_order_id=str(client_order_id))
    return {"ok": res.ok, "error": res.error, "status": res.status}


def poll_order(*, symbol: str, client_order_id: str) -> dict[str, Any] | None:
    if not enabled() or not client_order_id:
        return None
    ad = get_adapter(STRATEGY)
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


def place_entry(pending: dict[str, Any], *, qty: float | None = None) -> dict[str, Any]:
    """Entry via shared bridge (MARKET or LIMIT). Attaches provisional exchange STOP at trail level."""
    if not enabled():
        return {"ok": True, "skipped": True}
    side = str(pending.get("side") or "").upper()
    symbol = str(pending.get("symbol") or "")
    px = float(pending.get("limit_px") or pending.get("limit_price") or 0)
    q = float(qty if qty is not None else pending.get("qty") or 0)
    ot = str(
        pending.get("order_type") or getattr(config, "ENTRY_ORDER_TYPE", "LIMIT") or "LIMIT"
    ).upper()
    # Provisional SL from entry extreme (= reference px until fill updates)
    stop_px = trail_stop(side, px, config.TRAIL_PCT)
    res = try_place_entry(
        strategy=STRATEGY,
        symbol=symbol,
        side=side,
        quantity=q,
        price=px,
        place_stop=True,
        stop_price=stop_px,
        post_only=False if ot == "MARKET" else bool(getattr(config, "ENTRY_POST_ONLY", False)),
        layer="ENT",
        order_type=ot,
    )
    raw = res.raw if isinstance(res.raw, dict) else {}
    avg = float(raw.get("avgPrice") or 0)
    exe = float(raw.get("executedQty") or 0)
    out = {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "strategy": STRATEGY,
        "stop_price": stop_px,
        "order_type": ot,
        "avg_price": avg,
        "executed_qty": exe,
        "status": res.status,
    }
    if res.ok:
        pending["testnet_client_order_id"] = res.client_order_id
        pending["testnet_exchange_order_id"] = res.exchange_order_id
        pending["testnet_strategy"] = STRATEGY
        sl = (res.raw or {}).get("stop_loss") or {}
        if sl.get("client_order_id"):
            pending["testnet_sl_client_order_id"] = sl.get("client_order_id")
            pending["testnet_sl_exchange_order_id"] = sl.get("exchange_order_id")
            pending["testnet_sl_stop_price"] = sl.get("stop_price") or stop_px
            print(
                f"[testnet-H] SL_ATTACHED {symbol} stop={pending['testnet_sl_stop_price']} "
                f"trail={config.TRAIL_PCT} cid={sl.get('client_order_id')} ok={sl.get('ok')}",
                flush=True,
            )
        elif sl:
            print(f"[testnet-H] SL_ATTACH_FAIL {symbol}: {sl.get('error')}", flush=True)
        else:
            print(f"[testnet-H] ENTRY_OK_NO_SL {symbol} cid={res.client_order_id}", flush=True)
    else:
        print(f"[testnet-H] ENTRY_FAIL {symbol}: {res.error}", flush=True)
    return out


def ensure_stop_on_open(
    *,
    symbol: str,
    side: str,
    stop_price: float,
    qty: float,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Place/reuse/rehang exchange STOP at strategy trail stop. Same -4130/stale logic as E."""
    if not enabled():
        return {"ok": True, "skipped": True}
    stop_px = float(stop_price)
    pending = meta if meta is not None else {}
    if pending.get("testnet_sl_client_order_id"):
        prev_cid = str(pending.get("testnet_sl_client_order_id") or "")
        prev_ex = pending.get("testnet_sl_exchange_order_id")
        prev = float(pending.get("testnet_sl_stop_price") or 0)
        alive = algo_stop_still_open(
            strategy=STRATEGY,
            symbol=symbol,
            client_order_id=prev_cid,
            exchange_order_id=prev_ex,
        )
        if alive and prev > 0 and abs(prev - stop_px) / max(stop_px, 1e-12) < 1e-6:
            return {
                "ok": True,
                "reused": True,
                "client_order_id": prev_cid,
                "exchange_order_id": prev_ex,
                "stop_price": prev,
            }
        if alive:
            try_cancel(strategy=STRATEGY, symbol=symbol, client_order_id=prev_cid)
        else:
            print(
                f"[testnet-H] SL_STALE_REHANG {symbol} prev_cid={prev_cid} "
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
        strategy=STRATEGY,
        symbol=symbol,
        side=side,
        stop_price=stop_px,
        quantity=float(qty),
    )
    if not res.ok:
        print(f"[testnet-H] SL_ON_OPEN_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(
            f"[testnet-H] SL_ON_OPEN {symbol} stop={(res.raw or {}).get('stopPrice') or stop_px} "
            f"cid={res.client_order_id}",
            flush=True,
        )
    out = {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "stop_price": (res.raw or {}).get("stopPrice") or stop_px,
        "error": res.error,
    }
    if res.ok and res.client_order_id:
        pending["testnet_sl_client_order_id"] = res.client_order_id
        pending["testnet_sl_exchange_order_id"] = res.exchange_order_id
        pending["testnet_sl_stop_price"] = out["stop_price"]
        pending["exchange_protection_active"] = True
    else:
        pending["exchange_protection_active"] = False
        pending["testnet_sl_place_failed"] = str(res.error or "SL_FAIL")
    return out


def sync_trail_stop(pos: dict[str, Any]) -> dict[str, Any]:
    """Rehang exchange SL when local trail_stop moved (E-style protect, H trail level)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    return ensure_stop_on_open(
        symbol=str(pos["symbol"]),
        side=str(pos["side"]),
        stop_price=float(pos["trail_stop"]),
        qty=float(pos["qty"]),
        meta=pos,
    )


def place_exit_limit(
    *,
    symbol: str,
    side: str,  # position LONG/SHORT
    qty: float,
    price: float,
) -> dict[str, Any]:
    """Reduce-only LIMIT exit (H strategy exit is limit-only; same bridge as E TP)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    from common.testnet_bridge import close_side_for_position

    close_side = close_side_for_position(side)
    res = try_place_reduce_only_limit(
        strategy=STRATEGY,
        symbol=symbol,
        side=close_side,
        quantity=float(qty),
        price=float(price),
        layer="EX",
    )
    if res.ok:
        print(
            f"[testnet-H] EXIT_LIMIT {symbol} {close_side} px={price} qty={qty} cid={res.client_order_id}",
            flush=True,
        )
    else:
        print(f"[testnet-H] EXIT_LIMIT_FAIL {symbol}: {res.error}", flush=True)
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "exit_side": close_side,
    }


def cancel_pending(pending: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    sym = pending.get("symbol")
    errs = []
    for key in ("testnet_client_order_id", "testnet_sl_client_order_id"):
        cid = pending.get(key)
        if cid and sym:
            res = try_cancel(strategy=STRATEGY, symbol=str(sym), client_order_id=str(cid))
            if not res.ok:
                errs.append(f"{key}:{res.error}")
    return {"ok": not errs, "error": ";".join(errs) if errs else None}


def cancel_exit_and_sl(meta: dict[str, Any]) -> dict[str, Any]:
    """Cancel resting exit limit + SL algos for a symbol (never allOpenOrders)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    sym = str(meta.get("symbol") or "")
    errs = []
    for key in (
        "testnet_client_order_id",
        "testnet_exit_client_order_id",
        "testnet_sl_client_order_id",
    ):
        cid = meta.get(key)
        if cid and sym:
            res = try_cancel(strategy=STRATEGY, symbol=sym, client_order_id=str(cid))
            if not res.ok:
                errs.append(f"{key}:{res.error}")
    return {"ok": not errs, "error": ";".join(errs) if errs else None}


def fetch_user_trades(
    *,
    symbol: str,
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
    return _fetch(
        STRATEGY,
        symbol=symbol,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        from_id=from_id,
        limit=limit,
        max_pages=max_pages,
    )


def close_position(*, symbol: str, side: str, qty: float) -> dict[str, Any]:
    """Emergency flatten (guard orphan / naked past stop). Not used as H normal exit."""
    if not enabled():
        return {"ok": True, "skipped": True}
    res = try_close_position(strategy=STRATEGY, symbol=symbol, side=side, quantity=qty)
    if not res.ok:
        print(f"[testnet-H] CLOSE_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(f"[testnet-H] CLOSE_OK {symbol} qty={qty} cid={res.client_order_id}", flush=True)
    raw = res.raw if isinstance(res.raw, dict) else {}
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "raw": raw,
        "executed_qty": float(raw.get("executedQty") or 0),
        "avg_price": float(raw.get("avgPrice") or 0) or None,
    }


def position_amt(symbol: str) -> float:
    if not enabled():
        return 0.0
    return float(get_adapter(STRATEGY).position_amt(symbol))
