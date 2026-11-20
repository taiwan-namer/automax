"""Tri-arm paper portfolio: shared signals, isolated ledgers (simulation fills)."""

from __future__ import annotations

import csv
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import append_jsonl, atomic_write_json, now_iso, now_ts, round_price_tick, round_step


def _fee(notional: float) -> float:
    return abs(notional) * config.FEE_RATE_ONE_SIDE


def _pnl_short(entry: float, exit_px: float, qty: float) -> float:
    return (entry - exit_px) * qty


@dataclass
class Position:
    trade_id: str
    symbol: str
    side: str
    qty_initial: float
    qty_remaining: float
    entry_price: float
    entry_time: str
    entry_ts: float
    margin_u: float
    notional_initial: float
    leverage: float
    initial_stop: float
    effective_stop: float
    take_profit_1: float
    take_profit_2: float
    signal_id: str
    S: float
    arm: str
    strategy_version: str
    partial_tp_done: bool = False
    partial_tp_qty: float = 0.0
    fees_paid_u: float = 0.0
    realized_net_u: float = 0.0
    mfe_u: float = 0.0
    mae_u: float = 0.0
    filters: dict[str, Any] = field(default_factory=dict)
    surge_bucket: Optional[str] = None
    surge_pct_24h: Optional[float] = None


@dataclass
class ArmAccount:
    arm: str
    profile_id: str
    initial_capital_u: float = config.INITIAL_CAPITAL_USDT
    cash_u: float = config.INITIAL_CAPITAL_USDT
    realized_net_u: float = 0.0
    realized_fees_u: float = 0.0
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_limit_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    cooldowns_until: dict[str, float] = field(default_factory=dict)
    day_key: str = ""
    day_start_equity_u: float = config.INITIAL_CAPITAL_USDT
    day_new_entries_locked: bool = False
    run_id: str = ""
    strategy_version: str = config.STRATEGY_VERSION
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArmAccount":
        obj = cls(arm=d.get("arm", ""), profile_id=d.get("profile_id", ""))
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def used_margin(self) -> float:
        return sum(float(p.get("margin_u") or 0) for p in self.positions.values())


def load_arm(arm: str) -> ArmAccount:
    paths = config.arm_paths(arm)
    if paths["account"].exists():
        import json

        return ArmAccount.from_dict(json.loads(paths["account"].read_text(encoding="utf-8")))
    run_id = now_iso().replace(":", "").replace("+", "_")[:15] + "_" + uuid.uuid4().hex[:8]
    st = ArmAccount(
        arm=arm,
        profile_id=config.ARMS[arm]["profile_id"],
        run_id=run_id,
        updated_at=now_iso(),
    )
    save_arm(st)
    return st


def save_arm(st: ArmAccount) -> None:
    st.updated_at = now_iso()
    paths = config.arm_paths(st.arm)
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["account"], st.to_dict())


def _append_fill(arm: str, row: dict[str, Any]) -> None:
    paths = config.arm_paths(arm)
    row = {"recorded_at": now_iso(), "simulation_assumption": True, **row}
    append_jsonl(paths["fills"], row)


def barriers_for_short(entry: float, arm: str, tick: float) -> dict[str, float]:
    spec = config.ARMS[arm]
    sl = entry * (1.0 + config.INITIAL_STOP_ADVERSE_PCT)
    tp1 = entry * (1.0 - float(spec["first_tp_pct"]))
    tp2 = entry * (1.0 - float(spec["second_tp_pct"]))
    return {
        "stop": round_price_tick(sl, tick),
        "tp1": round_price_tick(tp1, tick),
        "tp2": round_price_tick(tp2, tick),
    }


def queue_short_limit(
    st: ArmAccount,
    *,
    signal: dict[str, Any],
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Queue short limit at S*1.01. No fill inside confirm bar (caller enforces)."""
    sym = signal["symbol"]
    if sym in st.positions or sym in st.pending_limit_entries:
        return {"ok": False, "block_reason": "SAME_SYMBOL_BUSY"}
    if len(st.positions) + len(st.pending_limit_entries) >= config.MAX_CONCURRENT_POSITIONS:
        return {"ok": False, "block_reason": "MAX_POSITIONS"}
    if st.day_new_entries_locked:
        return {"ok": False, "block_reason": "DAILY_LOSS_LOCK"}
    if st.cash_u + 1e-9 < config.MARGIN_PER_TRADE_USDT:
        return {"ok": False, "block_reason": "INSUFFICIENT_MARGIN"}
    until = float(st.cooldowns_until.get(sym) or 0)
    if until > now_ts():
        return {"ok": False, "block_reason": "COOLDOWN"}

    S = float(signal["S"])
    tick = float(filters.get("tickSize") or 0.0)
    limit_px = round_price_tick(S * (1.0 + config.LIMIT_ENTRY_OFFSET_PCT), tick)
    step = float(filters.get("stepSize") or 0.001)
    qty = round_step(config.NOTIONAL_USDT / limit_px, step)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 5.0)
    if qty <= 0 or qty < min_qty or qty * limit_px < min_notional:
        return {"ok": False, "block_reason": "LOT_FILTER"}

    pend = {
        "symbol": sym,
        "side": "SHORT",
        "limit_price": limit_px,
        "S": S,
        "cancel_price": S * (1.0 - config.CANCEL_DROP_FROM_S_PCT),
        "qty_target": qty,
        "qty_filled": 0.0,
        "signal": dict(signal),
        "filters": dict(filters),
        "queued_at": now_iso(),
        "queued_ts": now_ts(),
        "ttl_sec": config.LIMIT_ENTRY_TTL_SEC,
        "ttl_policy": config.TTL_POLICY,
        "arm": st.arm,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_id": signal.get("signal_id"),
        "fill_allowed_after_ms": int(signal.get("fill_allowed_after_ms") or 0),
    }
    st.pending_limit_entries[sym] = pend
    save_arm(st)
    _append_fill(
        st.arm,
        {
            "event": "LIMIT_QUEUED",
            "symbol": sym,
            "side": "SHORT",
            "price": limit_px,
            "S": S,
            "cancel_price": pend["cancel_price"],
            "qty": qty,
            "signal_id": signal.get("signal_id"),
            "arm": st.arm,
            "strategy_version": config.STRATEGY_VERSION,
        },
    )
    return {"ok": True, "limit_price": limit_px, "qty": qty}


def process_pending(st: ArmAccount, books: dict[str, dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = []
    for sym in list(st.pending_limit_entries.keys()):
        pend = st.pending_limit_entries.get(sym)
        if not pend:
            continue
        # do not fill before confirm bar ends
        if now_ms < int(pend.get("fill_allowed_after_ms") or 0):
            continue
        book = books.get(sym)
        if not book:
            continue
        bid = float(book.get("bid") or 0)
        ask = float(book.get("ask") or 0)
        mid = float(book.get("mid") or 0)
        fetched_at = float(book.get("fetched_at") or 0)
        if bid <= 0 or ask <= 0 or ask < bid or mid <= 0:
            continue
        if now_ts() - fetched_at > config.QUOTE_MAX_AGE_SEC:
            continue  # stale — no fabricate
        limit_px = float(pend["limit_price"])
        S = float(pend["S"])
        cancel_px = float(pend["cancel_price"])

        # cancel if mid <= S*0.98 (drop 2% from S) — before or instead of fill
        if mid <= cancel_px:
            st.pending_limit_entries.pop(sym, None)
            # if partial already opened as position, only cancel remainder (handled separately)
            save_arm(st)
            _append_fill(
                st.arm,
                {
                    "event": "LIMIT_CANCELLED",
                    "expire_reason": "DROP_FROM_S_2PCT",
                    "symbol": sym,
                    "price": limit_px,
                    "S": S,
                    "mid": mid,
                    "cancel_price": cancel_px,
                    "signal_id": pend.get("signal_id"),
                    "arm": st.arm,
                },
            )
            evs.append({"action": "CANCELLED", "symbol": sym, "mid": mid})
            continue

        # short limit fill when bid >= limit (touch)
        if bid >= limit_px:
            # fill at limit (no chase); full target for now (model disclosed)
            r = _open_from_pending(st, pend, fill_px=limit_px, book=book)
            evs.append({"action": "FILLED", "symbol": sym, **r})
    return evs


def _open_from_pending(
    st: ArmAccount,
    pend: dict[str, Any],
    *,
    fill_px: float,
    book: dict[str, Any],
) -> dict[str, Any]:
    sym = pend["symbol"]
    filters = pend.get("filters") or {}
    tick = float(filters.get("tickSize") or 0.0)
    step = float(filters.get("stepSize") or 0.001)
    qty = float(pend.get("qty_target") or 0.0)
    qty = round_step(qty, step)
    if qty <= 0:
        return {"ok": False, "block_reason": "ZERO_QTY"}
    if st.cash_u + 1e-9 < config.MARGIN_PER_TRADE_USDT:
        return {"ok": False, "block_reason": "INSUFFICIENT_MARGIN"}

    bars = barriers_for_short(fill_px, st.arm, tick)
    fee = _fee(fill_px * qty)
    trade_id = f"{st.run_id}:{sym}:{uuid.uuid4().hex[:10]}"
    sig = pend.get("signal") or {}
    pos = Position(
        trade_id=trade_id,
        symbol=sym,
        side="SHORT",
        qty_initial=qty,
        qty_remaining=qty,
        entry_price=fill_px,
        entry_time=now_iso(),
        entry_ts=now_ts(),
        margin_u=config.MARGIN_PER_TRADE_USDT,
        notional_initial=fill_px * qty,
        leverage=config.LEVERAGE,
        initial_stop=bars["stop"],
        effective_stop=bars["stop"],
        take_profit_1=bars["tp1"],
        take_profit_2=bars["tp2"],
        signal_id=str(pend.get("signal_id") or ""),
        S=float(pend["S"]),
        arm=st.arm,
        strategy_version=config.STRATEGY_VERSION,
        fees_paid_u=fee,
        filters=dict(filters),
        surge_bucket=sig.get("surge_bucket"),
        surge_pct_24h=sig.get("surge_pct_24h"),
    )
    st.cash_u -= config.MARGIN_PER_TRADE_USDT
    st.cash_u -= fee
    st.realized_fees_u += fee
    st.realized_net_u -= fee
    st.positions[sym] = asdict(pos)
    st.pending_limit_entries.pop(sym, None)
    save_arm(st)
    _append_fill(
        st.arm,
        {
            "event": "OPEN",
            "trade_id": trade_id,
            "symbol": sym,
            "side": "SHORT",
            "qty": qty,
            "price": fill_px,
            "fee_u": fee,
            "initial_stop": bars["stop"],
            "take_profit_1": bars["tp1"],
            "take_profit_2": bars["tp2"],
            "S": pend["S"],
            "signal_id": pend.get("signal_id"),
            "arm": st.arm,
            "strategy_version": config.STRATEGY_VERSION,
            "fill_model": "limit_touch_bid_ge_limit_fill_at_limit",
        },
    )
    return {"ok": True, "trade_id": trade_id, "price": fill_px, "qty": qty}


def close_qty(
    st: ArmAccount,
    symbol: str,
    *,
    qty: float,
    exit_price: float,
    reason: str,
) -> dict[str, Any]:
    pd = st.positions.get(symbol)
    if not pd:
        return {"ok": False, "block_reason": "NO_POSITION"}
    pos = Position(**{k: pd[k] for k in Position.__dataclass_fields__ if k in pd})
    qty = min(qty, pos.qty_remaining)
    if qty <= 0:
        return {"ok": False, "block_reason": "ZERO_QTY"}
    fee = _fee(exit_price * qty)
    gross = _pnl_short(pos.entry_price, exit_price, qty)
    net = gross - fee
    pos.qty_remaining -= qty
    pos.fees_paid_u += fee
    pos.realized_net_u += net
    st.realized_fees_u += fee
    st.realized_net_u += net
    st.cash_u += gross - fee
    is_flat = pos.qty_remaining <= 1e-12
    _append_fill(
        st.arm,
        {
            "event": "CLOSE" if is_flat else "CLOSE_PARTIAL",
            "trade_id": pos.trade_id,
            "symbol": symbol,
            "side": "SHORT",
            "qty": qty,
            "price": exit_price,
            "fee_u": fee,
            "gross_u": gross,
            "net_u": net,
            "exit_reason": reason,
            "arm": st.arm,
            "signal_id": pos.signal_id,
            "strategy_version": config.STRATEGY_VERSION,
            "partial_tp_done": pos.partial_tp_done,
        },
    )
    if is_flat:
        st.cash_u += pos.margin_u
        st.positions.pop(symbol, None)
        st.cooldowns_until[symbol] = now_ts() + config.COOLDOWN_MINUTES * 60
        # cancel any residual entry order for safety
        st.pending_limit_entries.pop(symbol, None)
    else:
        st.positions[symbol] = asdict(pos)
    save_arm(st)
    return {"ok": True, "net": net, "qty": qty, "remaining": 0.0 if is_flat else pos.qty_remaining}


def mark_for_short(book: dict[str, Any]) -> Optional[float]:
    ask = float(book.get("ask") or 0)
    if ask <= 0:
        return None
    return ask * (1.0 + config.SLIPPAGE_ADVERSE)


def manage_position(st: ArmAccount, symbol: str, book: dict[str, Any]) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = []
    pd = st.positions.get(symbol)
    if not pd:
        return evs
    fetched_at = float(book.get("fetched_at") or 0)
    if now_ts() - fetched_at > config.QUOTE_MAX_AGE_SEC:
        return evs  # no fabricated exit
    mark = mark_for_short(book)
    if mark is None:
        return evs
    pos = Position(**{k: pd[k] for k in Position.__dataclass_fields__ if k in pd})

    # MFE/MAE rough
    net = _pnl_short(pos.entry_price, mark, pos.qty_remaining) - _fee(mark * pos.qty_remaining)
    pos.mfe_u = max(pos.mfe_u, net)
    pos.mae_u = min(pos.mae_u, net)
    st.positions[symbol] = asdict(pos)

    sl = float(pos.effective_stop)
    tp1 = float(pos.take_profit_1)
    tp2 = float(pos.take_profit_2)
    sl_hit = mark >= sl
    tp1_hit = (not pos.partial_tp_done) and mark <= tp1
    tp2_hit = pos.partial_tp_done and mark <= tp2

    # race: stop first
    if sl_hit:
        reason = "ENTRY_STOP" if pos.partial_tp_done else "INITIAL_STOP"
        # cancel residual entry if any
        st.pending_limit_entries.pop(symbol, None)
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason)
        evs.append({"action": reason, "mark": mark, **r})
        return evs

    if tp1_hit and not pos.partial_tp_done:
        step = float((pos.filters or {}).get("stepSize") or 0.001)
        min_qty = float((pos.filters or {}).get("minQty") or 0.0)
        qty_tp = round_step(pos.qty_initial * config.PARTIAL_TP_FRACTION, step)
        qty_tp = min(qty_tp, pos.qty_remaining)
        rem = pos.qty_remaining - qty_tp
        if rem > 0 and rem < max(min_qty, step):
            qty_tp = pos.qty_remaining
        r = close_qty(st, symbol, qty=qty_tp, exit_price=mark, reason="PARTIAL_TP1_70")
        evs.append({"action": "PARTIAL_TP1_70", "tp": tp1, **r})
        if not r.get("ok"):
            return evs
        pd2 = st.positions.get(symbol)
        if not pd2:
            return evs
        pos2 = Position(**{k: pd2[k] for k in Position.__dataclass_fields__ if k in pd2})
        if pos2.qty_remaining > 1e-12:
            pos2.partial_tp_done = True
            pos2.partial_tp_qty = float(r.get("qty") or qty_tp)
            # move stop to entry (not fee BE) only after actual partial fill
            pos2.effective_stop = float(pos2.entry_price)
            st.positions[symbol] = asdict(pos2)
            save_arm(st)
            if mark >= pos2.effective_stop:
                r2 = close_qty(st, symbol, qty=pos2.qty_remaining, exit_price=mark, reason="ENTRY_STOP")
                evs.append({"action": "ENTRY_STOP_IMMEDIATE", **r2})
        return evs

    if tp2_hit and pos.partial_tp_done:
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="TP2_REMAINDER")
        evs.append({"action": "TP2_REMAINDER", "tp": tp2, **r})
    return evs
