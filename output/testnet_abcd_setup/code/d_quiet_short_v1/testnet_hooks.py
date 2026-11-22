"""Testnet hooks for strategy D."""
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
    get_adapter,
    try_cancel,
    try_close_position,
    try_place_entry,
    try_place_reduce_only_limit,
    try_place_stop_loss,
)
from common.testnet_env import orders_enabled  # noqa: E402

from . import config  # noqa: E402


def enabled() -> bool:
    return orders_enabled()


def strategy_stop_price(*, entry_price: float, qty: float, sl_usdt: float | None = None) -> float:
    """D short: stop such that gross loss ≈ SL_USDT → entry + sl_usdt/qty."""
    entry = float(entry_price)
    q = float(qty)
    sl_u = float(config.SL_USDT if sl_usdt is None else sl_usdt)
    if q <= 0:
        raise ValueError("qty<=0")
    return entry + (sl_u / q)


def strategy_tp_price(*, entry_price: float, qty: float, tp_usdt: float | None = None) -> float:
    """D short: TP limit such that gross ≈ TP_USDT → entry - tp_usdt/qty."""
    entry = float(entry_price)
    q = float(qty)
    tp_u = float(config.TP_USDT if tp_usdt is None else tp_usdt)
    if q <= 0:
        raise ValueError("qty<=0")
    return entry - (tp_u / q)


def place_tp_limit(
    *,
    symbol: str,
    qty: float,
    price: float,
) -> dict[str, Any]:
    """Hang reduceOnly BUY LIMIT for short TP (not TAKE_PROFIT_MARKET)."""
    if not enabled():
        return {"ok": True, "skipped": True}
    res = try_place_reduce_only_limit(
        strategy="D",
        symbol=symbol,
        side="BUY",
        quantity=float(qty),
        price=float(price),
        layer="TP",
    )
    if res.ok:
        print(
            f"[testnet-D] TP_LIMIT_PLACED {symbol} px={price} qty={qty} cid={res.client_order_id}",
            flush=True,
        )
    else:
        print(f"[testnet-D] TP_LIMIT_FAIL {symbol}: {res.error}", flush=True)
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "status": res.status,
        "price": price,
    }


def cancel_cid(*, symbol: str, client_order_id: str) -> dict[str, Any]:
    if not enabled() or not client_order_id:
        return {"ok": True, "skipped": True}
    res = try_cancel(strategy="D", symbol=symbol, client_order_id=str(client_order_id))
    return {"ok": res.ok, "error": res.error, "status": res.status}


def poll_order(*, symbol: str, client_order_id: str) -> dict[str, Any] | None:
    if not enabled() or not client_order_id:
        return None
    ad = get_adapter("D")
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
        "D",
        symbol=symbol,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        from_id=from_id,
        limit=limit,
        max_pages=max_pages,
    )


def place_entry(pending: dict[str, Any], *, qty: float) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    symbol = str(pending.get("symbol") or "")
    px = float(pending.get("limit_px") or 0)
    stop_px = strategy_stop_price(entry_price=px, qty=float(qty))
    res = try_place_entry(
        strategy="D",
        symbol=symbol,
        side="SHORT",
        quantity=float(qty),
        price=px,
        place_stop=True,
        stop_price=stop_px,
    )
    out = {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "error": res.error,
        "strategy": "D",
        "stop_price": stop_px,
    }
    if res.ok:
        pending["testnet_client_order_id"] = res.client_order_id
        pending["testnet_exchange_order_id"] = res.exchange_order_id
        pending["testnet_strategy"] = "D"
        sl = (res.raw or {}).get("stop_loss") or {}
        if sl.get("client_order_id"):
            pending["testnet_sl_client_order_id"] = sl.get("client_order_id")
            pending["testnet_sl_exchange_order_id"] = sl.get("exchange_order_id")
            pending["testnet_sl_stop_price"] = sl.get("stop_price") or stop_px
            print(
                f"[testnet-D] SL_ATTACHED {symbol} stop={pending['testnet_sl_stop_price']} "
                f"(D SL_USDT={config.SL_USDT}) cid={sl.get('client_order_id')} ok={sl.get('ok')}",
                flush=True,
            )
        elif sl:
            print(f"[testnet-D] SL_ATTACH_FAIL {symbol}: {sl.get('error')}", flush=True)
    return out


def ensure_stop_on_open(
    *,
    symbol: str,
    side: str = "SHORT",
    stop_price: float,
    qty: float,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    stop_px = float(stop_price)
    if pending and pending.get("testnet_sl_client_order_id"):
        prev = float(pending.get("testnet_sl_stop_price") or 0)
        if prev > 0 and abs(prev - stop_px) / max(stop_px, 1e-12) < 1e-6:
            return {
                "ok": True,
                "reused": True,
                "client_order_id": pending.get("testnet_sl_client_order_id"),
                "exchange_order_id": pending.get("testnet_sl_exchange_order_id"),
                "stop_price": prev,
            }
        try_cancel(
            strategy="D",
            symbol=symbol,
            client_order_id=str(pending["testnet_sl_client_order_id"]),
        )
    res = try_place_stop_loss(
        strategy="D",
        symbol=symbol,
        side=side,
        stop_price=stop_px,
        quantity=float(qty),
    )
    if not res.ok:
        print(f"[testnet-D] SL_ON_OPEN_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(
            f"[testnet-D] SL_ON_OPEN {symbol} stop={(res.raw or {}).get('stopPrice') or stop_px} "
            f"cid={res.client_order_id}",
            flush=True,
        )
    return {
        "ok": res.ok,
        "client_order_id": res.client_order_id,
        "exchange_order_id": res.exchange_order_id,
        "stop_price": (res.raw or {}).get("stopPrice") or stop_px,
        "error": res.error,
    }


def cancel_pending(pending: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    sym = pending.get("symbol")
    errs = []
    for key in ("testnet_client_order_id", "testnet_sl_client_order_id", "testnet_tp_client_order_id"):
        cid = pending.get(key)
        if cid and sym:
            res = try_cancel(strategy="D", symbol=str(sym), client_order_id=str(cid))
            if not res.ok:
                errs.append(f"{key}:{res.error}")
    return {"ok": not errs, "error": ";".join(errs) if errs else None}


def close_position(*, symbol: str, side: str = "SHORT", qty: float) -> dict[str, Any]:
    if not enabled():
        return {"ok": True, "skipped": True}
    res = try_close_position(strategy="D", symbol=symbol, side=side, quantity=qty)
    if not res.ok:
        print(f"[testnet-D] CLOSE_FAIL {symbol}: {res.error}", flush=True)
    else:
        print(f"[testnet-D] CLOSE_OK {symbol} qty={qty} cid={res.client_order_id}", flush=True)
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
