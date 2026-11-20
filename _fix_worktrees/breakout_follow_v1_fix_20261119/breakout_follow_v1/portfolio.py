"""Paper portfolio: open/partial/trail/reverse — simulation fills only."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any, Optional

from . import config
from .account import (
    AccountState,
    Position,
    clear_position,
    get_position,
    new_trade_id,
    save_account,
    set_position,
)
from .market_data import round_step
from .util import append_jsonl, now_iso, now_ts, report_error


def _fee(notional: float) -> float:
    return abs(notional) * config.FEE_RATE_ONE_SIDE


def _append_fill(row: dict[str, Any]) -> None:
    row = {
        "recorded_at": now_iso(),
        "simulation_assumption": config.SIMULATION_ASSUMPTIONS,
        **row,
    }
    append_jsonl(config.FILLS_PATH, row)
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            store.log_fill(row)
            store.log_position_event({"event": row.get("event"), **{k: row.get(k) for k in ("trade_id", "symbol", "side", "qty", "price", "fee_u", "exit_reason", "linked_prev_trade_id")}})
    except Exception as exc:
        report_error("RUN_FILL_LOG_FAIL", f"{type(exc).__name__}: {exc} trade_id={row.get('trade_id')}", symbol=str(row.get("symbol") or ""))


def _append_trade_row(row: dict[str, Any]) -> None:
    paths = [config.TRADES_PATH]
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            paths.append(store.path("trades.csv"))
    except Exception as exc:
        report_error("RUN_TRADES_PATH_FAIL", f"{type(exc).__name__}: {exc}")
    cols = [
        "timestamp", "event", "trade_id", "symbol", "side", "qty", "price",
        "fee_u", "realized_net_u", "exit_reason", "linked_prev_trade_id", "run_id",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(row)


def estimate_initial_risk_u(
    *,
    side: str,
    entry: float,
    stop: float,
    qty: float,
) -> float:
    """含進場費 + 預估出場費/滑價的總損失估算。"""
    if entry <= 0 or qty <= 0:
        return 1e18
    move = abs(entry - stop)
    gross_loss = move * qty
    entry_notional = entry * qty
    # 預估止損出場價再加不利滑價
    if side == "LONG":
        exit_px = stop * (1 - config.SLIPPAGE_ADVERSE)
    else:
        exit_px = stop * (1 + config.SLIPPAGE_ADVERSE)
    exit_notional = exit_px * qty
    fees = _fee(entry_notional) + _fee(exit_notional)
    return float(gross_loss + fees)


def try_open(
    st: AccountState,
    *,
    symbol: str,
    side: str,
    signal: dict[str, Any],
    source_tags: list[str],
    filters: dict[str, Any],
    linked_prev: Optional[str] = None,
    bypass_cooldown: bool = False,
) -> dict[str, Any]:
    """嘗試開倉。拒絕時不改資金。"""
    out: dict[str, Any] = {"ok": False, "block_reason": None, "trade_id": None}

    if st.inconsistency:
        out["block_reason"] = "STATE_INCONSISTENT"
        return out
    if st.day_new_entries_locked:
        out["block_reason"] = "DAILY_LOSS_LOCK"
        return out
    if symbol in st.positions:
        out["block_reason"] = "SAME_SYMBOL_POSITION"
        return out
    if len(st.positions) >= config.MAX_CONCURRENT_POSITIONS:
        out["block_reason"] = "MAX_POSITIONS"
        return out
    if st.free_cash() + 1e-9 < config.MARGIN_PER_TRADE_USDT:
        out["block_reason"] = "INSUFFICIENT_MARGIN"
        return out

    now = now_ts()
    if not bypass_cooldown:
        until = float(st.cooldowns_until.get(symbol) or 0)
        if until > now:
            out["block_reason"] = "COOLDOWN"
            return out

    entry = float(signal["est_fill_price"])
    atr = float(signal["atr"])
    if side == "LONG":
        stop = entry - config.ATR_STOP_MULT * atr
    else:
        stop = entry + config.ATR_STOP_MULT * atr

    step = float(filters.get("stepSize") or 0.001)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 5.0)
    qty = round_step(config.NOTIONAL_USDT / entry, step)
    if qty < min_qty or qty * entry < min_notional:
        out["block_reason"] = "LOT_FILTER"
        return out

    risk = estimate_initial_risk_u(side=side, entry=entry, stop=stop, qty=qty)
    out["risk_estimate_u"] = risk
    if risk > config.MAX_LOSS_PER_TRADE_USDT:
        out["block_reason"] = "RISK_BUDGET_EXCEEDED"
        return out

    fee = _fee(entry * qty)
    trade_id = new_trade_id(st.run_id, symbol)
    pos = Position(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        qty_initial=qty,
        qty_remaining=qty,
        entry_price=entry,
        entry_time=now_iso(),
        entry_ts=now,
        margin_u=config.MARGIN_PER_TRADE_USDT,
        notional_initial=entry * qty,
        leverage=config.LEVERAGE,
        atr_at_entry=atr,
        atr_time=str(signal.get("atr_time") or ""),
        initial_stop=stop,
        effective_stop=stop,
        extreme_price=entry,
        fees_paid_u=fee,
        funding_paid_u=0.0,
        funding_gap=True,
        source_tags=list(source_tags),
        signal_open_time=int(signal["signal_open_time"]),
        linked_prev_trade_id=linked_prev,
        range_high=float(signal["range_high"]),
        range_low=float(signal["range_low"]),
        vol_x=float(signal["vol_x"]),
        stop_set_events=[{"ts": now, "kind": "INITIAL_STOP", "price": stop}],
    )
    # 扣保證金 + 記費用至已實現（費用即時從現金扣）
    st.cash_u -= config.MARGIN_PER_TRADE_USDT
    st.cash_u -= fee
    st.realized_fees_u += fee
    st.realized_net_u -= fee  # 費用計入淨損益
    set_position(st, pos)
    save_account(st)

    _append_fill(
        {
            "event": "OPEN",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": entry,
            "fee_u": fee,
            "source_tags": source_tags,
        }
    )
    _append_trade_row(
        {
            "timestamp": pos.entry_time,
            "event": "OPEN",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": entry,
            "fee_u": fee,
            "realized_net_u": -fee,
            "exit_reason": "",
            "linked_prev_trade_id": linked_prev or "",
            "run_id": st.run_id,
        }
    )
    out.update({"ok": True, "trade_id": trade_id, "position": pos.to_dict()})
    return out


def _pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    if side == "LONG":
        return (exit_px - entry) * qty
    return (entry - exit_px) * qty


def close_qty(
    st: AccountState,
    symbol: str,
    *,
    qty: float,
    exit_price: float,
    reason: str,
    book_ts: float,
) -> dict[str, Any]:
    pos = get_position(st, symbol)
    if pos is None:
        return {"ok": False, "block_reason": "NO_POSITION"}
    qty = min(qty, pos.qty_remaining)
    if qty <= 0:
        return {"ok": False, "block_reason": "ZERO_QTY"}

    fee = _fee(exit_price * qty)
    gross = _pnl(pos.side, pos.entry_price, exit_price, qty)
    net = gross - fee
    pos.qty_remaining -= qty
    pos.fees_paid_u += fee
    pos.realized_net_u += net
    st.realized_fees_u += fee
    st.realized_net_u += net
    # v2：gross 與出場費各入帳一次；保證金政策不變（僅在完全平倉時歸還，部分平倉不釋放）
    st.cash_u += gross - fee

    is_flat = pos.qty_remaining <= 1e-12
    _append_fill(
        {
            "event": "CLOSE" if is_flat else "CLOSE_PARTIAL",
            "trade_id": pos.trade_id,
            "symbol": symbol,
            "side": pos.side,
            "qty": qty,
            "price": exit_price,
            "fee_u": fee,
            "gross_u": gross,
            "net_u": net,
            "exit_reason": reason,
            "book_ts": book_ts,
        }
    )
    _append_trade_row(
        {
            "timestamp": now_iso(),
            "event": "CLOSE" if is_flat else "CLOSE_PARTIAL",
            "trade_id": pos.trade_id,
            "symbol": symbol,
            "side": pos.side,
            "qty": qty,
            "price": exit_price,
            "fee_u": fee,
            "realized_net_u": net,
            "exit_reason": reason,
            "linked_prev_trade_id": pos.linked_prev_trade_id or "",
            "run_id": st.run_id,
        }
    )

    if is_flat:
        st.cash_u += pos.margin_u
        clear_position(st, symbol)
        st.cooldowns_until[symbol] = now_ts() + config.COOLDOWN_MINUTES * 60
    else:
        set_position(st, pos)

    save_account(st)
    return {"ok": True, "net": net, "gross": gross, "fee": fee, "qty": qty, "remaining": max(0.0, pos.qty_remaining), "flat": is_flat}


def mark_price_for_side(side: str, book: dict[str, Any]) -> float:
    """平多用 bid；平空用 ask；再加不利滑價。"""
    if side == "LONG":
        return float(book["bid"]) * (1 - config.SLIPPAGE_ADVERSE)
    return float(book["ask"]) * (1 + config.SLIPPAGE_ADVERSE)


def estimate_net_unrealized(pos: Position, mark: float) -> float:
    """含已付費用與預估平倉費後的淨浮盈。"""
    gross = _pnl(pos.side, pos.entry_price, mark, pos.qty_remaining)
    exit_fee = _fee(mark * pos.qty_remaining)
    # 進場費已在 realized；浮盈評估時只扣預估出場費
    return gross - exit_fee


def breakeven_price(pos: Position) -> float:
    """
    剩餘部位含成本保本價：使剩餘 qty 平倉後淨損益≈0（分攤已付進場費至剩餘）。
    不用已實現 30% 利潤把可虧價位稱保本。
    """
    # 剩餘應分攤的進場費 ≈ 總進場費 * remaining/initial
    entry_fee_total = _fee(pos.entry_price * pos.qty_initial)
    alloc_entry_fee = entry_fee_total * (pos.qty_remaining / pos.qty_initial)
    # 預估出場費隨價變，用 entry 近似
    est_exit_fee = _fee(pos.entry_price * pos.qty_remaining)
    cost = alloc_entry_fee + est_exit_fee
    if pos.side == "LONG":
        return pos.entry_price + cost / pos.qty_remaining
    return pos.entry_price - cost / pos.qty_remaining


def tighten_stop(
    pos: Position,
    candidate: float,
    kind: str,
    event_ts: float,
    *,
    extreme: Optional[float] = None,
    atr: Optional[float] = None,
    mark: Optional[float] = None,
) -> bool:
    """只收緊、不放寬。變更時記錄更新前後價、extreme、ATR、報價時間與原因。"""
    prev = pos.effective_stop
    if pos.side == "LONG":
        changed = candidate > pos.effective_stop
    else:
        changed = candidate < pos.effective_stop
    if not changed:
        return False
    pos.effective_stop = candidate
    pos.stop_set_events.append({"ts": event_ts, "kind": kind, "price": candidate, "prev": prev,
                                "extreme": extreme, "atr": atr, "mark": mark})
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            store.log_stop_event(
                {
                    "trade_id": pos.trade_id,
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "kind": kind,
                    "reason": kind,
                    "prev_stop": prev,
                    "new_stop": pos.effective_stop,
                    "extreme_price": extreme,
                    "atr_latest": atr,
                    "mark": mark,
                    "quote_ts": event_ts,
                    "event_ts": event_ts,
                    "never_widens": True,
                }
            )
    except Exception as exc:
        report_error("STOP_EVENT_LOG_FAIL", f"{type(exc).__name__}: {exc} trade_id={pos.trade_id}", symbol=pos.symbol)
    return True


def _update_extreme(pos: Position, mark: float) -> bool:
    """多單極值只升、空單極值只降。"""
    if pos.side == "LONG":
        if mark > pos.extreme_price:
            pos.extreme_price = mark
            return True
        return False
    if pos.extreme_price <= 0 or mark < pos.extreme_price:
        pos.extreme_price = mark
        return True
    return False


def manage_position(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    atr_latest: Optional[float],
) -> list[dict[str, Any]]:
    """
    持倉管理：部分止盈、保本、移動停利（自進場啟用）、止損觸發。
    每一步修改 pos 後立即 set_position，之後的讀取都拿到最新狀態；不使用進場前的報價。
    """
    events: list[dict[str, Any]] = []
    pos = get_position(st, symbol)
    if pos is None:
        return events
    book_ts = float(book.get("fetched_at") or time.time())
    if book_ts < float(pos.entry_ts):
        events.append({"action": "SKIP_PRE_ENTRY_QUOTE", "book_ts": book_ts, "entry_ts": pos.entry_ts})
        return events
    mark = mark_price_for_side(pos.side, book)

    # 0) 極值（用可成交價），更新後立即寫回狀態
    if _update_extreme(pos, mark):
        set_position(st, pos)
        save_account(st)

    # 1) 部分止盈（只一次）
    if not pos.partial_tp_done:
        net_u = estimate_net_unrealized(pos, mark)
        if net_u >= config.PARTIAL_TP_NET_USDT:
            step = 0.001
            qty_tp = round_step(pos.qty_initial * config.PARTIAL_TP_FRACTION, step)
            qty_tp = min(qty_tp, pos.qty_remaining)
            if qty_tp > 0:
                # 先寫入已部分止盈旗標，close_qty 會在同一次存檔中連同剩餘 qty 一起保存，避免重啟後重複止盈
                pos.partial_tp_done = True
                pos.partial_tp_qty = qty_tp
                pos.partial_tp_price = mark
                set_position(st, pos)
                r = close_qty(st, symbol, qty=qty_tp, exit_price=mark, reason="PARTIAL_TP", book_ts=book_ts)
                events.append({"action": "PARTIAL_TP", **r})
                pos = get_position(st, symbol)
                if pos is None:
                    return events
                be = breakeven_price(pos)
                pos.breakeven_stop = be
                if (pos.side == "LONG" and mark <= be) or (pos.side == "SHORT" and mark >= be):
                    set_position(st, pos)
                    r2 = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="BREAKEVEN_STOP", book_ts=book_ts)
                    events.append({"action": "BREAKEVEN_STOP_IMMEDIATE", **r2})
                    return events
                tighten_stop(pos, be, "BREAKEVEN_STOP", book_ts, extreme=pos.extreme_price, atr=atr_latest, mark=mark)
                set_position(st, pos)
                save_account(st)

    # 2) 移動停利（需最新 ATR；只收緊）
    if atr_latest and atr_latest > 0:
        if pos.side == "LONG":
            trail = pos.extreme_price - config.TRAIL_ATR_MULT * atr_latest
        else:
            trail = pos.extreme_price + config.TRAIL_ATR_MULT * atr_latest
        pos.trailing_stop = trail
        tighten_stop(pos, trail, "TRAILING_STOP", book_ts, extreme=pos.extreme_price, atr=atr_latest, mark=mark)
        set_position(st, pos)
        save_account(st)

    # 3) 觸發有效止損（僅用當前 book）
    hit = (pos.side == "LONG" and mark <= pos.effective_stop) or (pos.side == "SHORT" and mark >= pos.effective_stop)
    if hit:
        if pos.trailing_stop is not None and abs(pos.effective_stop - pos.trailing_stop) < 1e-12:
            reason = "TRAILING_STOP"
        elif pos.breakeven_stop is not None and abs(pos.effective_stop - pos.breakeven_stop) < 1e-12:
            reason = "BREAKEVEN_STOP"
        elif abs(pos.effective_stop - pos.initial_stop) < 1e-12:
            reason = "INITIAL_STOP"
        else:
            reason = "TRAILING_STOP" if pos.trailing_stop is not None else "BREAKEVEN_STOP"
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
        events.append({"action": reason, **r})
    return events


def count_reverses(st: AccountState, symbol: str, now: Optional[float] = None) -> int:
    now = now or now_ts()
    window = config.REVERSE_WINDOW_MINUTES * 60
    ev = [t for t in st.reverse_events.get(symbol, []) if now - t <= window]
    st.reverse_events[symbol] = ev
    return len(ev)


def note_reverse(st: AccountState, symbol: str) -> None:
    st.reverse_events.setdefault(symbol, []).append(now_ts())
    save_account(st)
