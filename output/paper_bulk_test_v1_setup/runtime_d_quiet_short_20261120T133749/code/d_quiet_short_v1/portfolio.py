"""D paper portfolio: whole-position TP/SL/vol-exit (simulation fills)."""

from __future__ import annotations

import csv
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import append_jsonl, atomic_write_json, now_iso, now_ts, round_price_tick, round_step


def _fee(notional: float) -> float:
    return abs(notional) * config.FEE_RATE_ONE_SIDE


def _gross_short(entry: float, exit_px: float, qty: float) -> float:
    return (entry - exit_px) * qty


def cover_exit_price(ask: float) -> float:
    """Aggressive cover: pay ask + adverse slip. Mid never used as fill."""
    return float(ask) * (1.0 + config.SLIPPAGE_ADVERSE)


@dataclass
class Account:
    profile_id: str = config.PROFILE_ID
    initial_capital_u: float = config.INITIAL_CAPITAL_USDT
    cash_u: float = config.INITIAL_CAPITAL_USDT
    realized_net_u: float = 0.0
    realized_gross_u: float = 0.0
    realized_fees_u: float = 0.0
    realized_funding_u: float = 0.0
    funding_data_gap: bool = True
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_limit_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    run_id: str = ""
    strategy_version: str = config.STRATEGY_VERSION
    updated_at: str = ""
    capital_note: str = "UNCONFIRMED until user decides margin/notional/leverage/tp_sl_basis"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        obj = cls()
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def slot_count(self) -> int:
        """Positions + pending that are not already counted as same-symbol position."""
        syms = set(self.positions.keys()) | set(self.pending_limit_entries.keys())
        return len(syms)


def load_account() -> Account:
    paths = config.paths()
    if paths["account"].exists():
        import json

        return Account.from_dict(__import__("json").loads(paths["account"].read_text(encoding="utf-8")))
    run_id = now_iso().replace(":", "").replace("+", "_")[:15] + "_" + uuid.uuid4().hex[:8]
    st = Account(run_id=run_id, updated_at=now_iso())
    save_account(st)
    return st


def save_account(st: Account) -> None:
    st.updated_at = now_iso()
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["account"], st.to_dict())


def _event(row: dict[str, Any]) -> None:
    paths = config.paths()
    append_jsonl(paths["events"], {"recorded_at": now_iso(), "simulation_assumption": True, **row})


def _fill(row: dict[str, Any]) -> None:
    paths = config.paths()
    append_jsonl(paths["fills"], {"recorded_at": now_iso(), "simulation_assumption": True, **row})


def target_qty(entry_px: float, filters: dict[str, Any], fill_ratio: float = 1.0) -> Optional[float]:
    notion = config.notional_for_stake()
    if notion is None or entry_px <= 0:
        return None
    raw = (notion * float(fill_ratio)) / entry_px
    qty = round_step(raw, float(filters.get("stepSize") or 0.001))
    min_qty = float(filters.get("minQty") or 0)
    if qty < min_qty:
        return None
    if qty * entry_px < float(filters.get("minNotional") or 0):
        return None
    return qty


def scaled_tp_sl(fill_ratio: float) -> tuple[float, float]:
    r = max(0.0, min(1.0, float(fill_ratio)))
    return config.TP_USDT * r, config.SL_USDT * r


def queue_short_limit(
    st: Account,
    *,
    signal: dict[str, Any],
    filters: dict[str, Any],
    allow_fills: bool,
) -> dict[str, Any]:
    sym = signal["symbol"]
    if not allow_fills:
        return {"ok": False, "reason": "PAPER_FILLS_BLOCKED_PENDING_CONFIRMATIONS"}
    if not config.paper_fills_allowed():
        return {"ok": False, "reason": "PAPER_FILLS_BLOCKED_PENDING_CONFIRMATIONS"}
    if sym in st.positions or sym in st.pending_limit_entries:
        return {"ok": False, "reason": "SYMBOL_BUSY"}
    if st.slot_count() >= config.MAX_CONCURRENT_SLOTS:
        return {"ok": False, "reason": "SLOT_CAP"}
    P = float(signal["P"])
    tick = float(filters.get("tickSize") or 0.0001)
    raw_limit = P * (1.0 + config.LIMIT_ENTRY_OFFSET_PCT)
    limit_px = round_price_tick(raw_limit, tick)
    actual_offset = (limit_px / P - 1.0) if P > 0 else None
    qty = target_qty(limit_px, filters, 1.0)
    if qty is None or qty <= 0:
        return {"ok": False, "reason": "QTY_UNAVAILABLE_OR_CAPITAL_UNCONFIRMED"}
    trade_id = f"{st.run_id}:{sym}:{uuid.uuid4().hex[:10]}"
    pending = {
        "trade_id": trade_id,
        "signal_id": signal["signal_id"],
        "symbol": sym,
        "side": "SHORT",
        "P": P,
        "limit_px": limit_px,
        "limit_px_raw": raw_limit,
        "actual_offset_pct": actual_offset,
        "qty_target": qty,
        "qty_filled": 0.0,
        "qty_remaining": qty,
        "filters": filters,
        "B": signal.get("B"),
        "T": signal.get("T"),
        "bar_amps": signal.get("bar_amps"),
        "range_amp": signal.get("range_amp"),
        "slope": signal.get("slope"),
        "fill_allowed_after_ms": int(signal.get("fill_allowed_after_ms") or 0),
        "queued_at": now_iso(),
        "queued_ts": now_ts(),
        "ttl_sec": config.LIMIT_ENTRY_TTL_SEC,
        "ttl_policy": config.TTL_POLICY,
        "price_source": signal.get("price_source"),
        "strategy_version": config.STRATEGY_VERSION,
        "roll_hi": None,
        "roll_lo": None,
        "roll_window_start_ts": now_ts(),
    }
    st.pending_limit_entries[sym] = pending
    save_account(st)
    _event({"event": "QUEUE_LIMIT_SHORT", **pending})
    return {"ok": True, "pending": pending}


def _open_or_add_short(
    st: Account,
    pending: dict[str, Any],
    *,
    fill_qty: float,
    fill_px: float,
) -> None:
    sym = pending["symbol"]
    fee = _fee(fill_qty * fill_px)
    margin = config.margin_for_stake() or 0.0
    notion = config.notional_for_stake() or (fill_qty * fill_px)
    fill_ratio = fill_qty / float(pending["qty_target"]) if pending["qty_target"] else 1.0
    tp_u, sl_u = scaled_tp_sl(fill_ratio if sym not in st.positions else 1.0)

    if sym not in st.positions:
        # scale TP/SL to actual filled fraction of target
        filled_frac = fill_qty / float(pending["qty_target"])
        tp_u, sl_u = scaled_tp_sl(filled_frac)
        pos = {
            "trade_id": pending["trade_id"],
            "signal_id": pending["signal_id"],
            "symbol": sym,
            "side": "SHORT",
            "qty_initial": fill_qty,
            "qty_remaining": fill_qty,
            "qty_target": pending["qty_target"],
            "entry_price": fill_px,
            "entry_time": now_iso(),
            "entry_ts": now_ts(),
            "margin_u": margin * filled_frac,
            "notional_initial": fill_qty * fill_px,
            "leverage": config.LEVERAGE,
            "stake_is_margin": config.STAKE_IS_MARGIN,
            "tp_sl_basis": config.TP_SL_BASIS,
            "tp_usdt_full": config.TP_USDT,
            "sl_usdt_full": config.SL_USDT,
            "tp_usdt": tp_u,
            "sl_usdt": sl_u,
            "B": pending.get("B"),
            "T": pending.get("T"),
            "fees_paid_u": fee,
            "realized_net_u": 0.0,
            "realized_gross_u": 0.0,
            "mfe_u": 0.0,
            "mae_u": 0.0,
            "roll_hi": fill_px,
            "roll_lo": fill_px,
            "roll_samples": [{"ts": now_ts(), "px": fill_px}],
            "filters": pending.get("filters") or {},
            "strategy_version": config.STRATEGY_VERSION,
            "P": pending.get("P"),
            "pending_remainder": True,
        }
        st.positions[sym] = pos
        st.cash_u -= fee
        st.realized_fees_u += fee
    else:
        pos = st.positions[sym]
        old_q = float(pos["qty_remaining"])
        new_q = old_q + fill_qty
        pos["entry_price"] = (float(pos["entry_price"]) * old_q + fill_px * fill_qty) / new_q
        pos["qty_remaining"] = new_q
        pos["qty_initial"] = float(pos.get("qty_initial") or 0) + fill_qty
        filled_frac = new_q / float(pos.get("qty_target") or pending["qty_target"])
        pos["tp_usdt"], pos["sl_usdt"] = scaled_tp_sl(filled_frac)
        pos["fees_paid_u"] = float(pos.get("fees_paid_u") or 0) + fee
        st.cash_u -= fee
        st.realized_fees_u += fee

    _fill(
        {
            "event": "OPEN" if float(pending.get("qty_filled") or 0) == 0 else "OPEN_ADD",
            "trade_id": pending["trade_id"],
            "signal_id": pending["signal_id"],
            "symbol": sym,
            "side": "SHORT",
            "qty": fill_qty,
            "price": fill_px,
            "fee_u": fee,
            "fill_model": "limit_touch_bid_ge_limit_fill_at_limit",
            "profile_id": config.PROFILE_ID,
            "strategy_version": config.STRATEGY_VERSION,
        }
    )


def cancel_pending(st: Account, symbol: str, reason: str) -> Optional[dict[str, Any]]:
    pend = st.pending_limit_entries.pop(symbol, None)
    if not pend:
        return None
    row = {
        "event": "CANCEL_PENDING",
        "action": "CANCELLED",
        "reason": reason,
        "symbol": symbol,
        "trade_id": pend.get("trade_id"),
        "signal_id": pend.get("signal_id"),
        "qty_filled": pend.get("qty_filled"),
        "qty_remaining": pend.get("qty_remaining"),
        "P": pend.get("P"),
        "limit_px": pend.get("limit_px"),
        "ttl_sec": pend.get("ttl_sec"),
    }
    _event(row)
    save_account(st)
    return row


def process_pending(
    st: Account,
    books: dict[str, dict[str, Any]],
    *,
    now_ms: int,
    eligibility_fail: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Process entry limits. No time-TTL cancel. Race-safe: fill then cancel remainder."""
    eligibility_fail = eligibility_fail or {}
    events: list[dict[str, Any]] = []
    for sym in list(st.pending_limit_entries.keys()):
        pend = st.pending_limit_entries.get(sym)
        if not pend:
            continue
        book = books.get(sym)
        if not book:
            continue
        age = now_ts() - float(book.get("fetched_at") or 0)
        if age > config.QUOTE_MAX_AGE_SEC:
            _event({"event": "STALE_QUOTE_SKIP_PENDING", "symbol": sym, "age": age})
            continue
        bid = float(book.get("bid") or 0)
        ask = float(book.get("ask") or 0)
        mid = float(book.get("mid") or 0)
        if bid <= 0 or ask <= 0 or mid <= 0 or ask < bid:
            continue

        P = float(pend["P"])
        limit_px = float(pend["limit_px"])

        # eligibility / liquidity cancel (before or after partial)
        if sym in eligibility_fail:
            # fill race: if bid already touched, fill first then cancel remainder
            if bid >= limit_px and now_ms >= int(pend.get("fill_allowed_after_ms") or 0):
                fill_qty = float(pend["qty_remaining"])
                _open_or_add_short(st, pend, fill_qty=fill_qty, fill_px=limit_px)
                pend["qty_filled"] = float(pend["qty_filled"]) + fill_qty
                pend["qty_remaining"] = 0.0
                events.append({"action": "FILLED", "symbol": sym, "qty": fill_qty})
            ev = cancel_pending(st, sym, eligibility_fail[sym])
            if ev:
                events.append(ev)
            continue

        # price cancel vs fixed P (not limit)
        if mid <= P * (1.0 - config.CANCEL_DROP_FROM_P_PCT):
            if bid >= limit_px and now_ms >= int(pend.get("fill_allowed_after_ms") or 0):
                fill_qty = float(pend["qty_remaining"])
                _open_or_add_short(st, pend, fill_qty=fill_qty, fill_px=limit_px)
                pend["qty_filled"] = float(pend["qty_filled"]) + fill_qty
                pend["qty_remaining"] = 0.0
                events.append({"action": "FILLED_BEFORE_CANCEL", "symbol": sym, "qty": fill_qty})
            ev = cancel_pending(st, sym, "PRICE_DROP_FROM_P")
            if ev:
                events.append(ev)
            continue

        # no time TTL — explicitly do nothing for age
        if now_ms < int(pend.get("fill_allowed_after_ms") or 0):
            continue

        if bid >= limit_px:
            # model: touch → fill remaining at limit (full remainder; queue depth not modeled)
            fill_qty = float(pend["qty_remaining"])
            _open_or_add_short(st, pend, fill_qty=fill_qty, fill_px=limit_px)
            pend["qty_filled"] = float(pend["qty_filled"]) + fill_qty
            pend["qty_remaining"] = 0.0
            st.pending_limit_entries.pop(sym, None)
            events.append({"action": "FILLED", "symbol": sym, "qty": fill_qty, "price": limit_px})
            _event({"event": "ENTRY_FILLED", "symbol": sym, "qty": fill_qty, "price": limit_px})
    save_account(st)
    return events


def _update_roll(pos: dict[str, Any], px: float, now: float) -> float:
    """Rolling 15-minute high/low amplitude using (hi-lo)/lo."""
    samples = list(pos.get("roll_samples") or [])
    samples.append({"ts": now, "px": px})
    cutoff = now - 900.0
    samples = [s for s in samples if float(s["ts"]) >= cutoff]
    pos["roll_samples"] = samples
    if not samples:
        return 0.0
    hi = max(float(s["px"]) for s in samples)
    lo = min(float(s["px"]) for s in samples)
    pos["roll_hi"] = hi
    pos["roll_lo"] = lo
    if lo <= 0:
        return 0.0
    return (hi - lo) / lo


def _pnl_for_mark(pos: dict[str, Any], mark_exit_px: float) -> dict[str, float]:
    qty = float(pos["qty_remaining"])
    entry = float(pos["entry_price"])
    gross = _gross_short(entry, mark_exit_px, qty)
    # exit fee estimate on cover notional; entry fees already paid
    exit_fee = _fee(qty * mark_exit_px)
    net = gross - exit_fee
    return {"gross": gross, "net": net, "exit_fee": exit_fee}


def close_position(
    st: Account,
    symbol: str,
    *,
    exit_px: float,
    reason: str,
    triggers: Optional[list[str]] = None,
    depth_meta: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    pos = st.positions.get(symbol)
    if not pos:
        return None
    # cancel any remainder entry first
    if symbol in st.pending_limit_entries:
        cancel_pending(st, symbol, f"EXIT_{reason}_CANCEL_REMAINDER")
    qty = float(pos["qty_remaining"])
    if qty <= 0:
        st.positions.pop(symbol, None)
        save_account(st)
        return None
    entry = float(pos["entry_price"])
    gross = _gross_short(entry, exit_px, qty)
    fee = _fee(qty * exit_px)
    funding = 0.0  # recorded as gap when unavailable
    net = gross - fee - funding
    # funding left as gap
    st.cash_u += net  # fee already subtracted in net; margin accounting simplified
    st.realized_net_u += net
    st.realized_gross_u += gross
    st.realized_fees_u += fee
    row = {
        "event": "CLOSE",
        "trade_id": pos.get("trade_id"),
        "signal_id": pos.get("signal_id"),
        "symbol": symbol,
        "side": "SHORT",
        "qty": qty,
        "price": exit_px,
        "entry_price": entry,
        "fee_u": fee,
        "gross_u": gross,
        "funding_u": funding,
        "net_u": net,
        "exit_reason": reason,
        "triggers_same_time": triggers or [reason],
        "fill_model": "ask_book_walk_or_ask_plus_slip_fallback",
        "tp_sl_basis": pos.get("tp_sl_basis"),
        "sl_is_trigger_not_max_loss": True,
        "B": pos.get("B"),
        "T": pos.get("T"),
        "mfe_u": pos.get("mfe_u"),
        "mae_u": pos.get("mae_u"),
        "hold_sec": now_ts() - float(pos.get("entry_ts") or now_ts()),
        "funding_data_gap": True,
        "depth_meta": depth_meta or {},
        "profile_id": config.PROFILE_ID,
        "strategy_version": config.STRATEGY_VERSION,
        "simulation_assumption": True,
        "report_note": "gross trigger vs net after fee/slip/funding shown separately",
    }
    _fill(row)
    # trades csv
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    new_file = not paths["trades"].exists()
    with paths["trades"].open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "closed_at", "trade_id", "signal_id", "symbol", "qty", "entry", "exit",
                "gross_u", "fee_u", "net_u", "exit_reason", "hold_sec",
            ],
        )
        if new_file:
            w.writeheader()
        w.writerow(
            {
                "closed_at": now_iso(),
                "trade_id": row["trade_id"],
                "signal_id": row["signal_id"],
                "symbol": symbol,
                "qty": qty,
                "entry": entry,
                "exit": exit_px,
                "gross_u": gross,
                "fee_u": fee,
                "net_u": net,
                "exit_reason": reason,
                "hold_sec": row["hold_sec"],
            }
        )
    st.positions.pop(symbol, None)
    save_account(st)
    return row


def update_position_marks(st: Account, symbol: str, book: dict[str, Any]) -> None:
    pos = st.positions.get(symbol)
    if not pos:
        return
    mid = float(book.get("mid") or 0)
    if mid <= 0:
        return
    diag_gross = _gross_short(float(pos["entry_price"]), mid, float(pos["qty_remaining"]))
    pos["mfe_u"] = max(float(pos.get("mfe_u") or 0), diag_gross)
    pos["mae_u"] = min(float(pos.get("mae_u") or 0), diag_gross)
    _update_roll(pos, mid, now_ts())
    save_account(st)


def check_exit_triggers(st: Account, symbol: str, book: dict[str, Any]) -> Optional[list[str]]:
    """Return trigger list or None. Stale/invalid quotes → no fabricated exit."""
    pos = st.positions.get(symbol)
    if not pos:
        return None
    age = now_ts() - float(book.get("fetched_at") or 0)
    if age > config.QUOTE_MAX_AGE_SEC:
        _event({"event": "STALE_QUOTE_NO_EXIT", "symbol": symbol, "age": age, "pending_exit": True})
        return None
    bid = float(book.get("bid") or 0)
    ask = float(book.get("ask") or 0)
    mid = float(book.get("mid") or 0)
    if bid <= 0 or ask <= 0 or mid <= 0 or ask < bid:
        _event({"event": "INVALID_BOOK_NO_EXIT", "symbol": symbol})
        return None

    mark_cover = cover_exit_price(ask)
    pnl = _pnl_for_mark(pos, mark_cover)
    basis = pos.get("tp_sl_basis") or config.TP_SL_BASIS or "gross"
    metric = pnl["gross"] if basis == "gross" else pnl["net"]

    amp = _update_roll(pos, mid, now_ts())
    T = float(pos.get("T") or config.VOL_EXIT_FLOOR)

    triggers: list[str] = []
    if metric >= float(pos.get("tp_usdt") or config.TP_USDT):
        triggers.append("TP")
    if metric <= -float(pos.get("sl_usdt") or config.SL_USDT):
        triggers.append("SL")
    if amp >= T:
        triggers.append("VOL_SPIKE")
    return triggers or None


def manage_position(st: Account, symbol: str, book: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Compat helper for tests: trigger + fallback ask+slip exit (no async depth walk)."""
    update_position_marks(st, symbol, book)
    triggers = check_exit_triggers(st, symbol, book)
    if triggers is None:
        age = now_ts() - float(book.get("fetched_at") or 0)
        if age > config.QUOTE_MAX_AGE_SEC:
            return {"action": "STALE_HOLD", "symbol": symbol}
        return None
    if not triggers:
        return None
    ask = float(book.get("ask") or 0)
    if ask <= 0:
        return {"action": "INVALID_BOOK", "symbol": symbol}
    reason = "+".join(triggers) if len(triggers) > 1 else triggers[0]
    return close_position(
        st,
        symbol,
        exit_px=cover_exit_price(ask),
        reason=reason,
        triggers=triggers,
        depth_meta={"depth_fallback": "ask_plus_slip_test_path"},
    )


def sort_signals(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """signal time → spread asc → symbol asc. No future-return sort."""
    return sorted(
        cands,
        key=lambda s: (
            int(s.get("bar_close_time") or 0),
            float(s.get("spread") or 999),
            str(s.get("symbol") or ""),
        ),
    )
