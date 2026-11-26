"""H v1 paper portfolio: slots, legs, trail, market entry + limit exit.

Paper fills are SIMULATION only — never claim exchange confirmation.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .metrics import gross_pnl_pct, trail_stop
from .util import append_jsonl, atomic_write_json, now_iso, now_ts, round_price_tick, round_step


def _fee(notional: float, role: str) -> float:
    if role == "maker":
        return abs(notional) * config.MAKER_FEE_RATE
    # taker or SIMULATION_UNKNOWN → conservative taker
    return abs(notional) * config.TAKER_FEE_RATE


def _pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    if str(side).upper() == "LONG":
        return (exit_px - entry) * qty
    return (entry - exit_px) * qty


@dataclass
class Account:
    profile_id: str = config.PROFILE_ID
    initial_capital_u: float = config.INITIAL_CAPITAL_USDT
    cash_u: float = config.INITIAL_CAPITAL_USDT
    realized_net_u: float = 0.0
    realized_fees_u: float = 0.0
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_exits: dict[str, dict[str, Any]] = field(default_factory=dict)
    # symbols that dropped out of Top15 but still have risk
    dropped_tracking: dict[str, dict[str, Any]] = field(default_factory=dict)
    # legacy: first-entry sent per symbol+bar_start (v1 same-bar lock; v2 unused for gating)
    first_entry_bars: dict[str, float] = field(default_factory=dict)
    # surge continuation events (serialized SurgeEventStore.to_dict())
    surge_events: dict[str, Any] = field(default_factory=dict)
    # v2 entry gates (cooldown / attempts / candidate) — per symbol
    entry_gates: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    strategy_version: str = config.STRATEGY_VERSION
    direction_note: str = "BOTH_LONG_SHORT"
    exchange_protection_active: bool = False  # must stay False without exchange stops
    updated_at: str = ""
    simulation_assumptions: dict[str, Any] = field(default_factory=lambda: dict(config.SIMULATION_ASSUMPTIONS))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        obj = cls()
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj


_SURGE_STORE = None


def get_surge_store(st: Account):
    """Lazy-load module-level store synced from account.surge_events."""
    global _SURGE_STORE
    from .surge_events import SurgeEventStore

    if _SURGE_STORE is None:
        _SURGE_STORE = SurgeEventStore.from_dict(st.surge_events or {})
    return _SURGE_STORE


def sync_surge_store(st: Account) -> None:
    store = get_surge_store(st)
    st.surge_events = store.to_dict()


def reset_surge_store_for_tests() -> None:
    global _SURGE_STORE
    _SURGE_STORE = None


def load_account() -> Account:
    paths = config.arm_paths()
    p = paths["account"]
    if p.exists():
        import json

        st = Account.from_dict(json.loads(p.read_text(encoding="utf-8")))
    else:
        st = Account(run_id=f"h_{uuid.uuid4().hex[:8]}")
    return st


def save_account(st: Account) -> None:
    st.updated_at = now_iso()
    st.strategy_version = config.STRATEGY_VERSION
    # Account-level flag = True only if every open position has exchange SL ack
    if st.positions:
        st.exchange_protection_active = all(
            bool(p.get("exchange_protection_active")) for p in st.positions.values()
        )
    else:
        st.exchange_protection_active = False
    paths = config.arm_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["account"], st.to_dict())


def _tn_enabled() -> bool:
    try:
        from . import testnet_hooks

        return bool(testnet_hooks.enabled())
    except Exception:
        return False


def _append_event(row: dict[str, Any]) -> None:
    append_jsonl(config.arm_paths()["events"], {**row, "at": now_iso()})


def _append_fill(row: dict[str, Any]) -> None:
    append_jsonl(config.arm_paths()["fills"], {**row, "at": now_iso(), "simulation": True})


def _side_zh(side: Any) -> str:
    s = str(side or "").upper()
    if s == "LONG":
        return "做多"
    if s == "SHORT":
        return "做空"
    return s or "—"


def _reason_zh(reason: str) -> str:
    r = str(reason or "")
    return {
        "TRAIL_STOP": "追蹤停損",
        "MANUAL": "手動",
        "FORCE": "強制",
    }.get(r, r or "—")


def slot_count(st: Account) -> int:
    """Occupied slots = symbols with position or pending first entry."""
    syms = set(st.positions.keys())
    for sym, pe in st.pending_entries.items():
        if pe.get("leg_index", 1) == 1:
            syms.add(sym)
    return len(syms)


def can_open_new_symbol(st: Account) -> bool:
    return slot_count(st) < config.MAX_SLOTS


def size_qty(limit_px: float, filters: dict[str, Any]) -> tuple[float, float]:
    step = float(filters.get("stepSize") or 0.001)
    raw = config.NOTIONAL_USDT / max(limit_px, 1e-12)
    qty = round_step(raw, step)
    notional = qty * limit_px
    return qty, notional


def legalize_entry_price(side: str, bid: float, ask: float, tick: float) -> float:
    if str(side).upper() == "LONG":
        return round_price_tick(ask, tick)  # BUY @ ask
    return round_price_tick(bid, tick)  # SELL @ bid


def legalize_exit_price(side: str, bid: float, ask: float, tick: float) -> float:
    if str(side).upper() == "LONG":
        return round_price_tick(bid, tick)  # SELL @ bid
    return round_price_tick(ask, tick)  # BUY @ ask


def queue_entry(
    st: Account,
    *,
    symbol: str,
    side: str,
    bid: float,
    ask: float,
    filters: dict[str, Any],
    signal: dict[str, Any],
    leg_index: int,
    bar_start_ts: float,
    now: Optional[float] = None,
    allow_when_dropped: bool = False,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    now = float(now if now is not None else now_ts())
    if symbol in st.pending_entries:
        return False, "ENTRY_ORDER_ALREADY_PENDING", None
    if symbol in st.pending_exits:
        return False, "EXIT_PENDING_BLOCKS_ENTRY", None
    if not allow_when_dropped and symbol in st.dropped_tracking:
        return False, "DROPPED_FROM_TOP15_NO_NEW", None

    pos = st.positions.get(symbol)
    if leg_index == 1:
        if pos:
            return False, "ALREADY_IN_POSITION", None
        if not can_open_new_symbol(st):
            return False, "MAX_SLOTS", None
        # v2: same-bar lock removed — cooldown/rate-limit handled in signals/entry_gate
        # keep legacy field write for audit only (do not block)
    else:
        if not pos:
            return False, "ADD_REQUIRES_POSITION", None
        if str(pos.get("side")).upper() != str(side).upper():
            return False, "SIDE_MISMATCH", None
        if int(pos.get("legs_filled") or 0) >= config.MAX_LEGS_PER_SYMBOL:
            return False, "MAX_LEGS", None
        if int(pos.get("legs_filled") or 0) != leg_index - 1:
            return False, "LEG_SEQUENCE", None

    mid = (bid + ask) / 2.0
    if mid <= 0 or (ask - bid) / mid > config.MAX_SPREAD:
        return False, "SPREAD_OR_BOOK", None

    tick = float(filters.get("tickSize") or 0.0001)
    limit_px = legalize_entry_price(side, bid, ask, tick)
    qty, notional = size_qty(limit_px, filters)
    if qty <= 0 or notional < float(filters.get("minNotional") or 0):
        return False, "QTY_TOO_SMALL", None
    margin = config.MARGIN_PER_TRADE_USDT
    if st.cash_u < margin:
        return False, "INSUFFICIENT_MARGIN", None

    use_market = str(getattr(config, "ENTRY_ORDER_TYPE", "LIMIT") or "LIMIT").upper() == "MARKET"
    surge_event_id = (signal or {}).get("event_id")
    entry_path = (signal or {}).get("entry_path")
    order = {
        "order_id": f"H_ENT_{uuid.uuid4().hex[:10]}",
        "symbol": symbol,
        "side": side,
        "entry_side": "BUY" if side == "LONG" else "SELL",
        "order_type": "MARKET" if use_market else "LIMIT",
        "limit_px": limit_px,  # MARKET: reference px (= ask/bid) for sizing / SL seed
        "qty": qty,
        "qty_filled": 0.0,
        "notional_target": notional,
        "margin_u": margin,
        "leg_index": leg_index,
        "bar_start_ts": bar_start_ts,
        "placed_ts": now,
        "ttl_sec": 0.0 if use_market else config.ENTRY_TTL_SEC,
        "status": "OPEN",
        "signal": signal,
        "filters": filters,
        "bid_at_place": bid,
        "ask_at_place": ask,
        "post_only": False if use_market else config.ENTRY_POST_ONLY,
        "simulation": True,
        "surge_event_id": surge_event_id,
        "entry_path": entry_path,
    }
    st.cash_u -= margin
    st.pending_entries[symbol] = order
    if leg_index == 1:
        st.first_entry_bars[symbol] = bar_start_ts
        # Reserve surge event so it cannot double-fire first entry
        store = get_surge_store(st)
        if surge_event_id:
            ok_r, why_r, _ev = store.reserve(
                symbol, order_id=str(order["order_id"]), now=now
            )
            _append_event(
                {
                    "event": "SURGE_EVENT_RESERVE",
                    "symbol": symbol,
                    "ok": ok_r,
                    "reason": why_r,
                    "event_id": surge_event_id,
                    "order_id": order["order_id"],
                    "entry_path": entry_path,
                }
            )
            if not ok_r and why_r not in ("ALREADY_RESERVED_SAME",):
                # roll back local pending — do not leave orphan first entry
                st.pending_entries.pop(symbol, None)
                st.cash_u += margin
                st.first_entry_bars.pop(symbol, None)
                sync_surge_store(st)
                save_account(st)
                return False, f"SURGE_EVENT_RESERVE_FAIL:{why_r}", None
            sync_surge_store(st)
    _append_event(
        {
            "event": "ENTRY_QUEUED",
            **{
                k: order[k]
                for k in (
                    "order_id",
                    "symbol",
                    "side",
                    "order_type",
                    "limit_px",
                    "qty",
                    "leg_index",
                    "surge_event_id",
                    "entry_path",
                )
            },
        }
    )
    if _tn_enabled():
        from . import testnet_hooks

        tn = testnet_hooks.place_entry(order, qty=qty)
        if not tn.get("ok") and not tn.get("skipped"):
            # roll back local queue on exchange reject (no silent reprice)
            st.pending_entries.pop(symbol, None)
            st.cash_u += margin
            if leg_index == 1 and st.first_entry_bars.get(symbol) == bar_start_ts:
                st.first_entry_bars.pop(symbol, None)
            _append_event(
                {
                    "event": "ENTRY_TN_REJECT",
                    "symbol": symbol,
                    "error": tn.get("error"),
                }
            )
            save_account(st)
            return False, f"TESTNET_ENTRY_REJECT:{tn.get('error')}", None
        # MARKET on Testnet: apply fill from exchange response when already filled
        if use_market and tn.get("ok") and not tn.get("skipped"):
            avg = float(tn.get("avg_price") or 0)
            exe = float(tn.get("executed_qty") or 0)
            if avg > 0 and exe > 0:
                apply_entry_fill(st, symbol, fill_px=avg, fill_qty=exe, fill_ts=now)
                save_account(st)
                return True, "OK", order
    elif use_market:
        # Paper: no resting entry — immediate full fill at ask (LONG) / bid (SHORT), taker
        fill_px = float(ask if str(side).upper() == "LONG" else bid)
        apply_entry_fill(
            st,
            symbol,
            fill_px=fill_px,
            fill_qty=qty,
            fill_ts=now,
        )
        save_account(st)
        return True, "OK", order
    save_account(st)
    return True, "OK", order


def _role_for_fill(placed_ts: float, fill_ts: float) -> str:
    if abs(fill_ts - placed_ts) < 1e-9:
        return "taker"
    if fill_ts > placed_ts:
        return "maker"
    return "SIMULATION_UNKNOWN"


def apply_entry_fill(
    st: Account,
    symbol: str,
    *,
    fill_px: float,
    fill_qty: float,
    fill_ts: float,
    partial: bool = False,
) -> tuple[bool, str]:
    pe = st.pending_entries.get(symbol)
    if not pe:
        return False, "NO_PENDING"
    fill_qty = float(fill_qty)
    if fill_qty <= 0:
        return False, "BAD_QTY"
    rem = float(pe["qty"]) - float(pe.get("qty_filled") or 0)
    if fill_qty > rem + 1e-12:
        return False, "OVERFILL"
    role = _role_for_fill(float(pe["placed_ts"]), fill_ts)
    fee = _fee(fill_px * fill_qty, role)
    pe["qty_filled"] = float(pe.get("qty_filled") or 0) + fill_qty
    pe["last_fill_ts"] = fill_ts
    pe["last_fee_role"] = role

    pos = st.positions.get(symbol)
    if not pos:
        extreme = fill_px
        stop = trail_stop(pe["side"], extreme, config.TRAIL_PCT)
        pos = {
            "trade_id": f"H_{uuid.uuid4().hex[:12]}",
            "symbol": symbol,
            "side": pe["side"],
            "qty": fill_qty,
            "avg_entry": fill_px,
            "legs_filled": 0,  # completed waves; set on full fill or cancel-after-partial
            "legs_used": int(pe["leg_index"]),  # wave reserved; cancel-after-partial keeps it
            "margin_u": float(pe["margin_u"]),
            "fees_paid_u": fee,
            "extreme_px": extreme,  # high for LONG / low for SHORT
            "trail_stop": stop,
            "profit_trailing": False,
            "profit_trailing_since": None,
            "filters": pe.get("filters") or {},
            "opened_ts": fill_ts,
            "opened_at": now_iso(),
            "signal_tags": list((pe.get("signal") or {}).get("tags") or []),
            "exchange_protection_active": False,
            "last_leg_fill_px": fill_px,
            "strategy_version": config.STRATEGY_VERSION,
        }
        st.positions[symbol] = pos
    else:
        # weighted avg
        q0 = float(pos["qty"])
        a0 = float(pos["avg_entry"])
        q1 = q0 + fill_qty
        pos["avg_entry"] = (a0 * q0 + fill_px * fill_qty) / q1
        pos["qty"] = q1
        pos["fees_paid_u"] = float(pos.get("fees_paid_u") or 0) + fee
        # Each queued leg already deducted margin from cash — must accumulate for exit refund
        pos["margin_u"] = float(pos.get("margin_u") or 0) + float(pe.get("margin_u") or 0)
        pos["last_leg_fill_px"] = fill_px
        # DO NOT reset extreme / trail / profit_trailing on add
        if pe["side"] == "LONG":
            if fill_px > float(pos["extreme_px"]):
                pos["extreme_px"] = fill_px
                pos["trail_stop"] = trail_stop("LONG", fill_px, config.TRAIL_PCT)
        else:
            if fill_px < float(pos["extreme_px"]):
                pos["extreme_px"] = fill_px
                pos["trail_stop"] = trail_stop("SHORT", fill_px, config.TRAIL_PCT)

    _append_fill(
        {
            "event": "ENTRY_FILL",
            "symbol": symbol,
            "side": pe["side"],
            "px": fill_px,
            "qty": fill_qty,
            "fee": fee,
            "fee_role": role,
            "order_id": pe["order_id"],
            "leg_index": pe["leg_index"],
            "partial": partial or pe["qty_filled"] + 1e-12 < float(pe["qty"]),
            "surge_event_id": pe.get("surge_event_id"),
            "entry_path": pe.get("entry_path"),
        }
    )

    # Spec: any first-leg fill (incl. partial) consumes surge event
    if int(pe.get("leg_index") or 1) == 1:
        store = get_surge_store(st)
        store.consume_on_fill(symbol, now=fill_ts, order_id=str(pe.get("order_id") or ""))
        sync_surge_store(st)
        _append_event(
            {
                "event": "SURGE_EVENT_CONSUMED",
                "symbol": symbol,
                "event_id": pe.get("surge_event_id"),
                "order_id": pe.get("order_id"),
                "entry_path": pe.get("entry_path"),
                "partial": partial or pe["qty_filled"] + 1e-12 < float(pe["qty"]),
            }
        )

    done = pe["qty_filled"] + 1e-12 >= float(pe["qty"])
    if done:
        pos = st.positions[symbol]
        pos["legs_filled"] = int(pos.get("legs_filled") or 0)
        # first fill wave: legs_filled becomes leg_index
        pos["legs_filled"] = max(int(pos.get("legs_filled") or 0), int(pe["leg_index"]))
        pos["legs_used"] = max(int(pos.get("legs_used") or 0), int(pe["leg_index"]))
        # carry provisional SL ids from pending → position
        for k in (
            "testnet_sl_client_order_id",
            "testnet_sl_exchange_order_id",
            "testnet_sl_stop_price",
        ):
            if pe.get(k) is not None:
                pos[k] = pe.get(k)
        del st.pending_entries[symbol]
        _append_event({"event": "ENTRY_COMPLETE", "symbol": symbol, "legs_filled": pos["legs_filled"]})
        if _tn_enabled():
            from . import testnet_hooks

            sln = testnet_hooks.ensure_stop_on_open(
                symbol=symbol,
                side=str(pos["side"]),
                stop_price=float(pos["trail_stop"]),
                qty=float(pos["qty"]),
                meta=pos,
            )
            pos["exchange_protection_active"] = bool(sln.get("ok"))
        # after avg update, check exit condition immediately
        _maybe_mark_profit_and_exit(st, symbol, mark_px=fill_px, now=fill_ts)
    save_account(st)
    return True, "OK"


def cancel_pending_entry(st: Account, symbol: str, reason: str, *, now: Optional[float] = None) -> None:
    pe = st.pending_entries.get(symbol)
    if not pe:
        return
    now = float(now if now is not None else now_ts())
    cancel_ack = True
    if _tn_enabled():
        from . import testnet_hooks

        tn = testnet_hooks.cancel_pending(pe)
        cancel_ack = bool(tn.get("ok") or tn.get("skipped"))
        # race: poll once after cancel for late fills while pending still present
        cid = pe.get("testnet_client_order_id")
        if cid:
            polled = testnet_hooks.poll_order(symbol=symbol, client_order_id=str(cid))
            if polled and float(polled.get("executed_qty") or 0) > float(pe.get("qty_filled") or 0):
                extra = float(polled["executed_qty"]) - float(pe.get("qty_filled") or 0)
                avg = float(polled.get("avg_price") or pe["limit_px"])
                if extra > 0:
                    apply_entry_fill(st, symbol, fill_px=avg, fill_qty=extra, fill_ts=now, partial=True)
                    # fully filled via race → nothing left to cancel locally
                    if symbol not in st.pending_entries:
                        return
                    pe = st.pending_entries.get(symbol) or pe
    st.pending_entries.pop(symbol, None)
    # refund unused margin if no position created from this order
    filled = float(pe.get("qty_filled") or 0)
    if filled <= 0 and symbol not in st.positions:
        st.cash_u += float(pe.get("margin_u") or 0)
        # Confirmed zero-fill cancel → restore OBSERVING if event not expired
        if int(pe.get("leg_index") or 1) == 1 and cancel_ack:
            store = get_surge_store(st)
            _ev, why = store.release_on_zero_fill_cancel(
                symbol, now=now, order_id=str(pe.get("order_id") or "")
            )
            sync_surge_store(st)
            _append_event(
                {
                    "event": "SURGE_EVENT_RELEASE",
                    "symbol": symbol,
                    "reason": why,
                    "event_id": pe.get("surge_event_id"),
                    "order_id": pe.get("order_id"),
                    "cancel_reason": reason,
                }
            )
        elif int(pe.get("leg_index") or 1) == 1 and not cancel_ack:
            # Unknown cancel/fill race — keep RESERVED occupancy; do not assume zero fill
            _append_event(
                {
                    "event": "SURGE_EVENT_KEEP_RESERVED",
                    "symbol": symbol,
                    "event_id": pe.get("surge_event_id"),
                    "order_id": pe.get("order_id"),
                    "note": "cancel_ack_false_reconcile_required",
                }
            )
    elif filled > 0 and symbol in st.positions:
        # partial then cancel = wave used; event already consumed on first fill
        pos = st.positions[symbol]
        pos["legs_used"] = max(int(pos.get("legs_used") or 0), int(pe["leg_index"]))
        pos["legs_filled"] = max(int(pos.get("legs_filled") or 0), int(pe["leg_index"]))
    _append_event(
        {
            "event": "ENTRY_CANCEL",
            "symbol": symbol,
            "reason": reason,
            "order_id": pe.get("order_id"),
            "qty_filled": filled,
            "cancel_ack": cancel_ack,
            "ts": now,
            "surge_event_id": pe.get("surge_event_id"),
            "entry_path": pe.get("entry_path"),
        }
    )
    save_account(st)


def process_pending_entries(st: Account, books: dict[str, dict[str, Any]], *, now: float) -> None:
    for sym in list(st.pending_entries.keys()):
        pe = st.pending_entries[sym]
        is_market = str(pe.get("order_type") or "").upper() == "MARKET"
        if (not is_market) and now - float(pe["placed_ts"]) >= float(
            pe.get("ttl_sec") or config.ENTRY_TTL_SEC
        ):
            cancel_pending_entry(st, sym, "TTL_EXPIRED", now=now)
            continue
        # Testnet: poll exchange fills (do not invent paper fills when orders enabled)
        if _tn_enabled() and pe.get("testnet_client_order_id"):
            from . import testnet_hooks

            polled = testnet_hooks.poll_order(symbol=sym, client_order_id=str(pe["testnet_client_order_id"]))
            if polled and not polled.get("error"):
                exe = float(polled.get("executed_qty") or 0)
                already = float(pe.get("qty_filled") or 0)
                if exe > already + 1e-12:
                    avg = float(polled.get("avg_price") or pe["limit_px"])
                    apply_entry_fill(st, sym, fill_px=avg, fill_qty=exe - already, fill_ts=now)
                    continue
                st_u = str(polled.get("status") or "").upper()
                if st_u in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                    cancel_pending_entry(st, sym, f"EXCHANGE_{st_u}", now=now)
                    continue
            continue  # wait for exchange; no paper cross invent
        b = books.get(sym)
        if not b:
            continue
        if now - float(b.get("fetched_at") or now) > config.QUOTE_MAX_AGE_SEC:
            continue
        bid = float(b["bid"])
        ask = float(b["ask"])
        side = pe["side"]
        # Paper MARKET leftover (should be rare): fill at current ask/bid
        if is_market:
            fill_px = float(ask if str(side).upper() == "LONG" else bid)
            apply_entry_fill(
                st,
                sym,
                fill_px=fill_px,
                fill_qty=float(pe["qty"]) - float(pe.get("qty_filled") or 0),
                fill_ts=now,
            )
            continue
        limit_px = float(pe["limit_px"])
        # Paper LIMIT fill model: cross only
        if side == "LONG" and ask <= limit_px:
            apply_entry_fill(st, sym, fill_px=limit_px, fill_qty=float(pe["qty"]) - float(pe.get("qty_filled") or 0), fill_ts=now)
        elif side == "SHORT" and bid >= limit_px:
            apply_entry_fill(st, sym, fill_px=limit_px, fill_qty=float(pe["qty"]) - float(pe.get("qty_filled") or 0), fill_ts=now)


def maybe_queue_add(
    st: Account,
    symbol: str,
    *,
    bid: float,
    ask: float,
    now: float,
) -> tuple[bool, str]:
    if symbol in st.dropped_tracking:
        return False, "DROPPED_NO_ADD"
    if symbol in st.pending_exits:
        return False, "EXIT_PRIORITY"
    if symbol in st.pending_entries:
        return False, "ENTRY_PENDING"
    pos = st.positions.get(symbol)
    if not pos:
        return False, "NO_POSITION"
    legs = int(pos.get("legs_filled") or 0)
    if legs >= config.MAX_LEGS_PER_SYMBOL:
        return False, "MAX_LEGS"
    last_px = float(pos.get("last_leg_fill_px") or pos["avg_entry"])
    side = pos["side"]
    mid = (bid + ask) / 2.0
    if side == "LONG":
        if mid < last_px * (1.0 + config.ADD_STEP_PCT):
            return False, "ADD_STEP_NOT_REACHED"
    else:
        if mid > last_px * (1.0 - config.ADD_STEP_PCT):
            return False, "ADD_STEP_NOT_REACHED"
    ok, reason, _ = queue_entry(
        st,
        symbol=symbol,
        side=side,
        bid=bid,
        ask=ask,
        filters=pos.get("filters") or {},
        signal={"tags": ["ADD"], "reason": "ADD_STEP"},
        leg_index=legs + 1,
        bar_start_ts=-1.0,
        now=now,
    )
    return ok, reason


def update_trail(st: Account, symbol: str, last_px: float, *, now: float) -> None:
    pos = st.positions.get(symbol)
    if not pos or symbol in st.pending_exits:
        return
    side = pos["side"]
    prev_stop = float(pos.get("trail_stop") or 0)
    if side == "LONG":
        if last_px > float(pos["extreme_px"]):
            pos["extreme_px"] = last_px
        pos["trail_stop"] = trail_stop("LONG", float(pos["extreme_px"]), config.TRAIL_PCT)
    else:
        if last_px < float(pos["extreme_px"]):
            pos["extreme_px"] = last_px
        pos["trail_stop"] = trail_stop("SHORT", float(pos["extreme_px"]), config.TRAIL_PCT)

    g = gross_pnl_pct(side, float(pos["avg_entry"]), last_px)
    if g is not None and g >= config.PROFIT_MARK_PCT:
        if not pos.get("profit_trailing"):
            pos["profit_trailing"] = True
            pos["profit_trailing_since"] = now
            _append_event({"event": "PROFIT_TRAILING_ON", "symbol": symbol, "gross_pct": g})
    # Rehang exchange SL when trail moved (Binance protect aligned with E sync pattern)
    new_stop = float(pos["trail_stop"])
    if _tn_enabled() and prev_stop > 0 and abs(new_stop - prev_stop) / max(new_stop, 1e-12) > 1e-6:
        from . import testnet_hooks

        sln = testnet_hooks.sync_trail_stop(pos)
        pos["exchange_protection_active"] = bool(sln.get("ok") or sln.get("reused"))
    _maybe_mark_profit_and_exit(st, symbol, mark_px=last_px, now=now)


def _maybe_mark_profit_and_exit(st: Account, symbol: str, *, mark_px: float, now: float) -> None:
    pos = st.positions.get(symbol)
    if not pos or symbol in st.pending_exits:
        return
    side = pos["side"]
    stop = float(pos["trail_stop"])
    hit = (side == "LONG" and mark_px <= stop) or (side == "SHORT" and mark_px >= stop)
    if hit:
        begin_exit(st, symbol, reason="TRAIL_STOP", trigger_px=mark_px, now=now)


def begin_exit(
    st: Account,
    symbol: str,
    *,
    reason: str,
    trigger_px: float,
    now: float,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
) -> None:
    pos = st.positions.get(symbol)
    if not pos:
        return
    if symbol in st.pending_entries:
        cancel_pending_entry(st, symbol, "EXIT_CANCELS_ENTRY", now=now)
    if symbol in st.pending_exits:
        return  # exit intent already sticky
    tick = float((pos.get("filters") or {}).get("tickSize") or 0.0001)
    # if book not provided, use trigger as both (tests may inject book next tick)
    b = float(bid if bid is not None else trigger_px)
    a = float(ask if ask is not None else trigger_px)
    limit_px = legalize_exit_price(pos["side"], b, a, tick)
    # Cancel exchange SL before limit exit to avoid double-flatten race (E cancels SL on TP)
    if _tn_enabled():
        from . import testnet_hooks

        testnet_hooks.cancel_exit_and_sl(pos)
        pos["exchange_protection_active"] = False
    exit_rec = {
        "order_id": f"H_EXIT_{uuid.uuid4().hex[:10]}",
        "symbol": symbol,
        "side": pos["side"],
        "exit_side": "SELL" if pos["side"] == "LONG" else "BUY",
        "qty_remaining": float(pos["qty"]),
        "limit_px": limit_px,
        "reason": reason,
        "trigger_px": trigger_px,
        "started_ts": now,
        "last_requote_ts": now,
        "status": "EXITING",
        "alarmed": False,
        "exchange_protection_active": False,
        "simulation": not _tn_enabled(),
    }
    if _tn_enabled():
        from . import testnet_hooks

        tn = testnet_hooks.place_exit_limit(
            symbol=symbol,
            side=str(pos["side"]),
            qty=float(pos["qty"]),
            price=limit_px,
        )
        if tn.get("ok"):
            exit_rec["testnet_exit_client_order_id"] = tn.get("client_order_id")
            exit_rec["testnet_exit_exchange_order_id"] = tn.get("exchange_order_id")
        else:
            exit_rec["testnet_exit_place_error"] = tn.get("error")
    st.pending_exits[symbol] = exit_rec
    _append_event(
        {
            "event": "EXIT_BEGIN",
            "symbol": symbol,
            "reason": reason,
            "trigger_px": trigger_px,
            "limit_px": limit_px,
            "note": "limit_only_no_market_fallback",
            "testnet": _tn_enabled(),
        }
    )
    print(
        f"[出場] {symbol} {_side_zh(pos.get('side'))} 啟動｜原因={_reason_zh(reason)}({reason}) "
        f"觸發@{trigger_px:.6g} → 限價@{limit_px:.6g} qty={float(pos.get('qty') or 0):g} "
        f"均價={float(pos.get('avg_entry') or 0):.6g}｜限價出場無市價 fallback",
        flush=True,
    )
    save_account(st)


def process_pending_exits(st: Account, books: dict[str, dict[str, Any]], *, now: float) -> None:
    for sym in list(st.pending_exits.keys()):
        ex = st.pending_exits[sym]
        pos = st.positions.get(sym)
        if not pos:
            del st.pending_exits[sym]
            continue
        # sticky intent
        if now - float(ex["started_ts"]) >= config.EXIT_ALARM_SEC and not ex.get("alarmed"):
            ex["alarmed"] = True
            elapsed_ex = now - float(ex["started_ts"])
            append_jsonl(
                config.shared_paths()["alerts"],
                {
                    "event": "EXIT_SLOW_ALARM",
                    "symbol": sym,
                    "elapsed_sec": elapsed_ex,
                    "qty_remaining": ex["qty_remaining"],
                    "at": now_iso(),
                    "note": "still_managing_exit_no_market_fallback",
                },
            )
            print(
                f"[出場慢] {sym} {_side_zh(ex.get('side'))} 限價未成 "
                f"已耗時={elapsed_ex:.1f}s 剩餘qty={float(ex.get('qty_remaining') or 0):g} "
                f"@{float(ex.get('limit_px') or 0):.6g}｜續管、無市價 fallback",
                flush=True,
            )
        # Testnet: poll reduce-only exit; paper uses book cross
        if _tn_enabled() and ex.get("testnet_exit_client_order_id"):
            from . import testnet_hooks

            polled = testnet_hooks.poll_order(
                symbol=sym, client_order_id=str(ex["testnet_exit_client_order_id"])
            )
            if polled and not polled.get("error"):
                exe = float(polled.get("executed_qty") or 0)
                st_u = str(polled.get("status") or "").upper()
                if exe + 1e-12 >= float(ex["qty_remaining"]) or st_u == "FILLED":
                    avg = float(polled.get("avg_price") or ex["limit_px"])
                    _complete_exit_fill(st, sym, fill_px=avg, fill_ts=now)
                    continue
                # requote path below if due
            # fall through to requote using book
        b = books.get(sym)
        if not b:
            continue
        if now - float(b.get("fetched_at") or now) > config.QUOTE_MAX_AGE_SEC:
            continue
        bid = float(b["bid"])
        ask = float(b["ask"])
        side = ex["side"]
        if not _tn_enabled():
            # Paper fill model: cross only
            filled = False
            if side == "LONG" and bid >= float(ex["limit_px"]):
                _complete_exit_fill(st, sym, fill_px=float(ex["limit_px"]), fill_ts=now)
                filled = True
            elif side == "SHORT" and ask <= float(ex["limit_px"]):
                _complete_exit_fill(st, sym, fill_px=float(ex["limit_px"]), fill_ts=now)
                filled = True
            if filled:
                continue
        # requote after EXIT_REQUOTE_SEC (cancel-ack then new limit on remaining)
        if now - float(ex["last_requote_ts"]) >= config.EXIT_REQUOTE_SEC:
            tick = float((pos.get("filters") or {}).get("tickSize") or 0.0001)
            new_px = legalize_exit_price(side, bid, ask, tick)
            cancel_ack = True
            if _tn_enabled():
                from . import testnet_hooks

                old_cid = ex.get("testnet_exit_client_order_id")
                if old_cid:
                    c = testnet_hooks.cancel_cid(symbol=sym, client_order_id=str(old_cid))
                    cancel_ack = bool(c.get("ok") or c.get("skipped"))
                # confirm remaining vs exchange position before re-place (no oversize)
                try:
                    amt = abs(float(testnet_hooks.position_amt(sym)))
                    if amt <= 0:
                        # exchange already flat (e.g. SL raced) — reconcile local without inventing px
                        _append_event(
                            {
                                "event": "EXIT_EXCHANGE_ALREADY_FLAT",
                                "symbol": sym,
                                "note": "clear_local_pending_no_invented_pnl",
                            }
                        )
                        # leave position for settlement path; mark exit done conservatively
                        del st.positions[sym]
                        del st.pending_exits[sym]
                        # keep first_entry_bars — same-bar no duplicate first entry (§六)
                        continue
                    ex["qty_remaining"] = min(float(ex["qty_remaining"]), amt)
                except Exception:
                    pass
                tn = testnet_hooks.place_exit_limit(
                    symbol=sym,
                    side=str(side),
                    qty=float(ex["qty_remaining"]),
                    price=new_px,
                )
                if tn.get("ok"):
                    ex["testnet_exit_client_order_id"] = tn.get("client_order_id")
                    ex["testnet_exit_exchange_order_id"] = tn.get("exchange_order_id")
                    ex.pop("testnet_exit_place_error", None)
                else:
                    ex["testnet_exit_place_error"] = tn.get("error")
            ex["limit_px"] = new_px
            ex["last_requote_ts"] = now
            ex["order_id"] = f"H_EXIT_{uuid.uuid4().hex[:10]}"
            _append_event(
                {
                    "event": "EXIT_REQUOTE",
                    "symbol": sym,
                    "limit_px": new_px,
                    "qty_remaining": ex["qty_remaining"],
                    "cancel_ack": cancel_ack,
                }
            )
    save_account(st)


def _complete_exit_fill(st: Account, symbol: str, *, fill_px: float, fill_ts: float) -> None:
    ex = st.pending_exits.get(symbol)
    pos = st.positions.get(symbol)
    if not ex or not pos:
        return
    qty = float(ex["qty_remaining"])
    # unverified flat forbidden: only clear when fill covers remaining
    role = "taker"  # exit often crosses; unknown→taker
    fee = _fee(fill_px * qty, role)
    gross = _pnl(pos["side"], float(pos["avg_entry"]), fill_px, qty)
    net = gross - fee - float(pos.get("fees_paid_u") or 0)
    st.cash_u += float(pos.get("margin_u") or 0) + gross - fee
    st.realized_net_u += net
    st.realized_fees_u += fee + float(pos.get("fees_paid_u") or 0)
    _append_fill(
        {
            "event": "EXIT_FILL",
            "symbol": symbol,
            "side": pos["side"],
            "px": fill_px,
            "qty": qty,
            "gross": gross,
            "fee": fee,
            "fee_role": role,
            "net": net,
            "reason": ex.get("reason"),
            "trigger_px": ex.get("trigger_px"),
            "exit_delay_sec": fill_ts - float(ex["started_ts"]),
            "slip_vs_trigger": (float(ex["trigger_px"]) - fill_px)
            if pos["side"] == "LONG"
            else (fill_px - float(ex["trigger_px"])),
            "profit_trailing": bool(pos.get("profit_trailing")),
            "simulation": True,
            "note": "unfilled_would_not_be_flat",
        }
    )
    side = str(pos.get("side") or "")
    avg = float(pos.get("avg_entry") or 0)
    reason = str(ex.get("reason") or "")
    delay = fill_ts - float(ex.get("started_ts") or fill_ts)
    del st.positions[symbol]
    del st.pending_exits[symbol]
    st.dropped_tracking.pop(symbol, None)
    # v2: record exit for cooldown; flat only after this confirmed fill
    try:
        from .entry_gate import get_entry_gate_store

        gate_store = get_entry_gate_store(st.entry_gates)
        gate_store.note_exit(symbol, fill_ts)
        gate_store.note_flat_confirmed(symbol, fill_ts)
        st.entry_gates = gate_store.to_dict()
    except Exception:
        pass
    # Consumed surge events stay consumed — need a NEW origin surge after flat.
    _append_event({"event": "EXIT_COMPLETE", "symbol": symbol, "net": net})
    print(
        f"[出場成交] {symbol} {_side_zh(side)}｜原因={_reason_zh(reason)}({reason}) "
        f"進@{avg:.6g} → 出@{fill_px:.6g} qty={qty:g}｜"
        f"毛利={gross:+.2f} 費={fee:.2f} 淨={net:+.2f}｜耗時={delay:.1f}s｜模擬",
        flush=True,
    )
    save_account(st)


def on_drop_from_top15(st: Account, symbols: list[str], *, now: float) -> None:
    store = get_surge_store(st)
    for sym in symbols:
        st.dropped_tracking[sym] = {"dropped_ts": now, "reason": "LEFT_TOP15"}
        if sym in st.pending_entries:
            cancel_pending_entry(st, sym, "LEFT_TOP15", now=now)
        # Invalidate event for NEW opens; do not force-flat positions
        store.invalidate(sym, reason="LEFT_TOP15", now=now)
        _append_event(
            {
                "event": "TRACK_DROP_TOP15",
                "symbol": sym,
                "note": "no_force_flat;surge_event_invalidated",
            }
        )
    sync_surge_store(st)
    save_account(st)


def on_reenter_top5(st: Account, symbols: list[str]) -> None:
    for sym in symbols:
        st.dropped_tracking.pop(sym, None)
        # Must NOT revive old surge events — re-detect only
        _append_event(
            {
                "event": "TRACK_REENTER_TOP5",
                "symbol": sym,
                "note": "no_surge_event_revive",
            }
        )
    save_account(st)
