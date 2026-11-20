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
from .util import append_jsonl, now_iso, now_ts
from . import exit_plans


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
    except Exception:
        pass


def _append_trade_row(row: dict[str, Any]) -> None:
    paths = [config.TRADES_PATH]
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            paths.append(store.path("trades.csv"))
    except Exception:
        pass
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


def _entry_block_reasons(
    st: AccountState,
    *,
    symbol: str,
    side: str,
    signal: dict[str, Any],
    bypass_cooldown: bool,
    for_limit_queue: bool = False,
) -> list[str]:
    """開倉／掛限價共用風控閘門。"""
    reasons: list[str] = []
    if st.inconsistency:
        reasons.append("STATE_INCONSISTENT")
    if st.day_new_entries_locked:
        reasons.append("DAILY_LOSS_LOCK")
    if symbol in st.positions:
        reasons.append("SAME_SYMBOL_POSITION")
    pending = getattr(st, "pending_limit_entries", None) or {}
    if symbol in pending:
        reasons.append("SAME_SYMBOL_PENDING_LIMIT")
    # 掛限價時：持倉 + 既有掛單 占用名額；實際成交時只數持倉
    if for_limit_queue:
        n_occ = len(st.positions) + len(pending)
    else:
        n_occ = len(st.positions)
    if n_occ >= config.MAX_CONCURRENT_POSITIONS:
        reasons.append("MAX_POSITIONS")
    if st.free_cash() + 1e-9 < config.MARGIN_PER_TRADE_USDT:
        reasons.append("INSUFFICIENT_MARGIN")

    sig_key = f"{symbol}:{int(signal['signal_open_time'])}:{side}"
    if sig_key in st.processed_signals:
        reasons.append("DUPLICATE_SIGNAL")

    now = now_ts()
    if not bypass_cooldown:
        until = float(st.cooldowns_until.get(symbol) or 0)
        if until > now:
            reasons.append("COOLDOWN")
    cutoff_iso = str(getattr(config, "NEW_ENTRIES_UNTIL_ISO", "") or "").strip()
    if cutoff_iso:
        try:
            from datetime import datetime
            cut = datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00"))
            if cut.tzinfo is None:
                from zoneinfo import ZoneInfo
                cut = cut.replace(tzinfo=ZoneInfo(config.TZ_NAME))
            if now_ts() >= cut.timestamp():
                reasons.append("OBSERVATION_CUTOFF")
        except Exception:
            reasons.append("OBSERVATION_CUTOFF_PARSE_ERROR")
    return reasons


def queue_limit_entry(
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
    """掛限價進場：多=參考價×(1−offset)，空=參考價×(1+offset)。不立刻成交。"""
    out: dict[str, Any] = {
        "ok": False,
        "queued": False,
        "block_reason": None,
        "block_reasons": [],
        "trade_id": None,
        "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
        "final_action": "BLOCKED",
    }
    if not bool(getattr(config, "LIMIT_ENTRY_ENABLED", False)):
        out["block_reason"] = "LIMIT_ENTRY_DISABLED"
        out["block_reasons"] = ["LIMIT_ENTRY_DISABLED"]
        return out

    reasons = _entry_block_reasons(
        st,
        symbol=symbol,
        side=side,
        signal=signal,
        bypass_cooldown=bypass_cooldown,
        for_limit_queue=True,
    )
    if reasons:
        out["block_reason"] = reasons[0]
        out["block_reasons"] = reasons
        return out

    ref = float(signal.get("signal_ref_price") or 0.0)
    offset = float(getattr(config, "LIMIT_ENTRY_OFFSET_PCT", 0.005) or 0.005)
    limit_px = float(signal.get("limit_price") or 0.0)
    if ref <= 0:
        bid = float(signal.get("bid") or 0.0)
        ask = float(signal.get("ask") or 0.0)
        if bid > 0 and ask > 0:
            ref = (bid + ask) / 2.0
    if limit_px <= 0 and ref > 0 and offset > 0:
        limit_px = ref * (1.0 - offset) if side == "LONG" else ref * (1.0 + offset)
    if ref <= 0 or limit_px <= 0:
        out["block_reason"] = "INVALID_LIMIT_PRICE"
        out["block_reasons"] = ["INVALID_LIMIT_PRICE"]
        return out

    from .market_data import round_price_tick

    tick = float(filters.get("tickSize") or 0.0)
    limit_px = round_price_tick(limit_px, tick) if tick > 0 else limit_px
    if limit_px <= 0:
        out["block_reason"] = "INVALID_LIMIT_AFTER_TICK"
        out["block_reasons"] = ["INVALID_LIMIT_AFTER_TICK"]
        return out

    # 預檢數量可成交（以限價計）
    step = float(filters.get("stepSize") or 0.001)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 5.0)
    qty = round_step(config.NOTIONAL_USDT / limit_px, step)
    if qty <= 0 or qty < min_qty or qty * limit_px < min_notional:
        out["block_reason"] = "LOT_FILTER"
        out["block_reasons"] = ["LOT_FILTER"]
        return out

    cancel_adverse = float(getattr(config, "LIMIT_ENTRY_CANCEL_ADVERSE_PCT", 0.01) or 0.0)
    now = now_ts()
    sig_key = f"{symbol}:{int(signal['signal_open_time'])}:{side}"
    pending = {
        "symbol": symbol,
        "side": side,
        "limit_price": limit_px,
        "signal_ref_price": ref,
        "limit_offset_pct": offset,
        "cancel_adverse_pct": cancel_adverse,
        "signal": dict(signal),
        "source_tags": list(source_tags),
        "filters": dict(filters),
        "linked_prev": linked_prev,
        "bypass_cooldown": bool(bypass_cooldown),
        "sig_key": sig_key,
        "queued_at": now_iso(),
        "queued_ts": now,
        # 不用時間作廢；保留欄位僅相容舊帳戶（expire_ts=0 表示關閉）
        "expire_ts": 0.0,
        "ttl_sec": 0.0,
        "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
        "run_id": st.run_id,
        "exit_plan_id": str(getattr(config, "EXIT_PLAN_ID", "") or exit_plans.LEGACY),
        "code_fix_tag": str(getattr(config, "CODE_FIX_TAG", "") or ""),
        "notional_u": float(config.NOTIONAL_USDT),
        "margin_u": float(config.MARGIN_PER_TRADE_USDT),
    }
    # 掛單後即標記訊號，避免重複掛；成交或作廢都不再重掛同 key
    if not hasattr(st, "pending_limit_entries") or st.pending_limit_entries is None:
        st.pending_limit_entries = {}
    st.pending_limit_entries[symbol] = pending
    if sig_key not in st.processed_signals:
        st.processed_signals.append(sig_key)
    save_account(st)

    _append_fill(
        {
            "event": "LIMIT_QUEUED",
            "trade_id": None,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": limit_px,
            "fee_u": 0.0,
            "signal_ref_price": ref,
            "limit_price": limit_px,
            "limit_offset_pct": offset,
            "cancel_adverse_pct": cancel_adverse,
            "ttl_sec": 0.0,
            "source_tags": source_tags,
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "signal_key": sig_key,
            "signal_open_time": int(signal["signal_open_time"]),
            "simulation_assumption": True,
        }
    )
    out.update(
        {
            "ok": True,
            "queued": True,
            "final_action": "LIMIT_QUEUED",
            "limit_price": limit_px,
            "signal_ref_price": ref,
            "cancel_adverse_pct": cancel_adverse,
            "qty": qty,
        }
    )
    return out


def process_pending_limit_entries(
    st: AccountState,
    books: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """輪詢待成交限價：觸價則 try_open；相對參考價不利偏離 ≥N% 則作廢（不用時間）。"""
    results: list[dict[str, Any]] = []
    pending_map = getattr(st, "pending_limit_entries", None) or {}
    if not pending_map:
        return results
    default_adverse = float(getattr(config, "LIMIT_ENTRY_CANCEL_ADVERSE_PCT", 0.01) or 0.0)
    for sym in list(pending_map.keys()):
        pend = pending_map.get(sym)
        if not pend:
            continue

        book = books.get(sym)
        if not book:
            continue
        try:
            bid = float(book["bid"])
            ask = float(book["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        side = str(pend.get("side") or "")
        limit_px = float(pend.get("limit_price") or 0.0)
        ref = float(pend.get("signal_ref_price") or 0.0)
        if limit_px <= 0:
            continue

        # 先觸價成交
        touched = (side == "LONG" and ask <= limit_px) or (side == "SHORT" and bid >= limit_px)
        if not touched:
            # 未觸價：盯盤作廢 — 多單參考價上漲≥N%；空單參考價下跌≥N%
            adverse_pct = float(pend.get("cancel_adverse_pct") or default_adverse or 0.0)
            cancel = False
            move_pct = None
            if adverse_pct > 0 and ref > 0:
                if side == "LONG" and mid >= ref * (1.0 + adverse_pct):
                    cancel = True
                    move_pct = (mid - ref) / ref
                elif side == "SHORT" and mid <= ref * (1.0 - adverse_pct):
                    cancel = True
                    move_pct = (ref - mid) / ref
            if cancel:
                st.pending_limit_entries.pop(sym, None)
                _append_fill(
                    {
                        "event": "LIMIT_EXPIRED",
                        "expire_reason": "ADVERSE_MOVE",
                        "symbol": sym,
                        "side": side,
                        "price": limit_px,
                        "signal_ref_price": ref,
                        "mark_mid": mid,
                        "adverse_move_pct": move_pct,
                        "cancel_adverse_pct": adverse_pct,
                        "limit_offset_pct": pend.get("limit_offset_pct"),
                        "queued_at": pend.get("queued_at"),
                        "profile_id": pend.get("profile_id"),
                        "run_id": st.run_id,
                        "signal_key": pend.get("sig_key"),
                        "simulation_assumption": True,
                    }
                )
                save_account(st)
                results.append(
                    {
                        "symbol": sym,
                        "action": "EXPIRED",
                        "ok": False,
                        "expire_reason": "ADVERSE_MOVE",
                        "adverse_move_pct": move_pct,
                        "mark_mid": mid,
                        "signal_ref_price": ref,
                    }
                )
            continue

        # 觸價：以限價成交（不再加不利滑價）
        sig = dict(pend.get("signal") or {})
        sig["est_fill_price"] = limit_px
        sig["limit_price"] = limit_px
        sig["signal_ref_price"] = pend.get("signal_ref_price")
        sig["limit_entry_filled"] = True
        # 版本隔離：沿用掛單當下戳記（舊掛單＝legacy 名義／出場）
        sig["exit_plan_id"] = pend.get("exit_plan_id") or exit_plans.LEGACY
        sig["code_fix_tag"] = pend.get("code_fix_tag")
        sig["notional_u"] = pend.get("notional_u")
        sig["margin_u"] = pend.get("margin_u")
        # 先移除 pending，避免 try_open 撞 SAME_SYMBOL_PENDING
        st.pending_limit_entries.pop(sym, None)
        opened = try_open(
            st,
            symbol=sym,
            side=side,
            signal=sig,
            source_tags=list(pend.get("source_tags") or []),
            filters=dict(pend.get("filters") or {}),
            linked_prev=pend.get("linked_prev"),
            bypass_cooldown=bool(pend.get("bypass_cooldown")),
        )
        if opened.get("ok"):
            # 補記限價進場資訊到最近 OPEN 已在 try_open；另寫觸發事件
            _append_fill(
                {
                    "event": "LIMIT_FILLED",
                    "trade_id": opened.get("trade_id"),
                    "symbol": sym,
                    "side": side,
                    "price": limit_px,
                    "signal_ref_price": pend.get("signal_ref_price"),
                    "limit_offset_pct": pend.get("limit_offset_pct"),
                    "touch_bid": bid,
                    "touch_ask": ask,
                    "queued_at": pend.get("queued_at"),
                    "profile_id": pend.get("profile_id"),
                    "run_id": st.run_id,
                    "signal_key": pend.get("sig_key"),
                    "simulation_assumption": True,
                }
            )
            results.append(
                {
                    "symbol": sym,
                    "action": "FILLED",
                    "ok": True,
                    "trade_id": opened.get("trade_id"),
                    "side": side,
                    "limit_price": limit_px,
                }
            )
        else:
            # 成交當下被擋：記失敗，不回掛（訊號已 processed）
            _append_fill(
                {
                    "event": "LIMIT_FILL_BLOCKED",
                    "symbol": sym,
                    "side": side,
                    "price": limit_px,
                    "block_reason": opened.get("block_reason"),
                    "block_reasons": opened.get("block_reasons"),
                    "profile_id": pend.get("profile_id"),
                    "run_id": st.run_id,
                    "signal_key": pend.get("sig_key"),
                    "simulation_assumption": True,
                }
            )
            save_account(st)
            results.append(
                {
                    "symbol": sym,
                    "action": "FILL_BLOCKED",
                    "ok": False,
                    "block_reason": opened.get("block_reason"),
                }
            )
    return results


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
    out: dict[str, Any] = {
        "ok": False,
        "block_reason": None,
        "block_reasons": [],
        "trade_id": None,
        "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
    }

    reasons = _entry_block_reasons(
        st,
        symbol=symbol,
        side=side,
        signal=signal,
        bypass_cooldown=bypass_cooldown,
        for_limit_queue=False,
    )
    # 限價成交路徑：訊號已在 queue 時寫入 processed_signals，需放行 DUPLICATE_SIGNAL
    if signal.get("limit_entry_filled") and "DUPLICATE_SIGNAL" in reasons:
        reasons = [r for r in reasons if r != "DUPLICATE_SIGNAL"]
    # 剛 pop 的 pending 不應再擋
    if "SAME_SYMBOL_PENDING_LIMIT" in reasons and symbol not in (getattr(st, "pending_limit_entries", None) or {}):
        reasons = [r for r in reasons if r != "SAME_SYMBOL_PENDING_LIMIT"]

    sig_key = f"{symbol}:{int(signal['signal_open_time'])}:{side}"
    now = now_ts()

    if reasons:
        out["block_reason"] = reasons[0]
        out["block_reasons"] = reasons
        return out

    entry = float(signal["est_fill_price"])
    atr = float(signal["atr"])
    if entry <= 0 or not (entry == entry):  # NaN guard
        out["block_reason"] = "INVALID_ENTRY_PRICE"
        out["block_reasons"] = ["INVALID_ENTRY_PRICE"]
        return out

    step = float(filters.get("stepSize") or 0.001)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 5.0)
    tick = float(filters.get("tickSize") or 0.0)

    plan = str(signal.get("exit_plan_id") or getattr(config, "EXIT_PLAN_ID", "") or exit_plans.LEGACY)
    if not plan:
        plan = exit_plans.LEGACY
    notional_u, margin_u = exit_plans.open_sizing_from_plan(plan, signal=signal)
    # 數量仍依名義／成交價（不依 ATR 縮放）
    qty = round_step(notional_u / entry, step)
    if qty <= 0 or qty < min_qty or qty * entry < min_notional:
        out["block_reason"] = "LOT_FILTER"
        return out

    from .market_data import round_price_tick

    fixed_gross = bool(getattr(config, "FIXED_GROSS_EXIT_ENABLED", False))
    tp_usdt = float(getattr(config, "FIXED_GROSS_TP_USDT", 0.0) or 0.0)
    tp2_usdt = float(getattr(config, "FIXED_GROSS_TP2_USDT", 0.0) or 0.0)
    sl_usdt = float(getattr(config, "FIXED_GROSS_SL_USDT", 0.0) or 0.0)
    exit_geom = "atr"
    tp_raw = sl_raw = tp2_raw = None
    gross_tp_actual = gross_sl_actual = None
    take_profit_2 = None
    pyramid_state = None
    code_tag = str(signal.get("code_fix_tag") or getattr(config, "CODE_FIX_TAG", "") or "")

    # Legacy 掛單成交：強制毛 50/75（即使當前 C profile 已關 FIXED_GROSS）
    if plan == exit_plans.LEGACY:
        fixed_gross = True
        tp_usdt, sl_usdt, tp2_usdt = 50.0, 75.0, 0.0

    if plan == exit_plans.C_PYRAMID:
        add_pct = float(getattr(config, "PYRAMID_ADD_PCT", 0.005) or 0.005)
        lv = exit_plans.pyramid_levels(side, entry, tick, add_pct)
        take_profit = lv["P0_TP"]
        stop = lv["Pstop"]
        tp_raw, sl_raw = take_profit, stop
        exit_geom = "pyramid_r2_p0tp1pct"
        pyramid_state = exit_plans.build_pyramid_state(
            side=side, p0=entry, qty=qty, margin_u=margin_u, tick=tick, step=step, notional_u=notional_u
        )
        if side == "LONG" and not (take_profit > entry > stop):
            out["block_reason"] = "BARRIER_SIDE_INVALID"
            out["block_reasons"] = ["BARRIER_SIDE_INVALID"]
            return out
        if side == "SHORT" and not (take_profit < entry < stop):
            out["block_reason"] = "BARRIER_SIDE_INVALID"
            out["block_reasons"] = ["BARRIER_SIDE_INVALID"]
            return out
    elif fixed_gross:
        if tp_usdt <= 0 or sl_usdt <= 0:
            out["block_reason"] = "INVALID_FIXED_GROSS_TARGETS"
            out["block_reasons"] = ["INVALID_FIXED_GROSS_TARGETS"]
            return out
        bars = exit_plans.fixed_gross_barriers(
            side=side, entry=entry, qty=qty, tp_usdt=tp_usdt, sl_usdt=sl_usdt, tick=tick, tp2_usdt=tp2_usdt
        )
        tp_raw, sl_raw, tp2_raw = bars["tp_raw"], bars["sl_raw"], bars["tp2_raw"]
        take_profit = bars["take_profit"]
        take_profit_2 = bars["take_profit_2"]
        stop = bars["stop"]
        if take_profit <= 0 or stop <= 0:
            out["block_reason"] = "INVALID_BARRIER_AFTER_TICK"
            out["block_reasons"] = ["INVALID_BARRIER_AFTER_TICK"]
            return out
        if side == "LONG" and not (take_profit > entry > stop):
            out["block_reason"] = "BARRIER_SIDE_INVALID"
            out["block_reasons"] = ["BARRIER_SIDE_INVALID"]
            return out
        if side == "SHORT" and not (take_profit < entry < stop):
            out["block_reason"] = "BARRIER_SIDE_INVALID"
            out["block_reasons"] = ["BARRIER_SIDE_INVALID"]
            return out
        if side == "LONG":
            gross_tp_actual = (take_profit - entry) * qty
            gross_sl_actual = (stop - entry) * qty
        else:
            gross_tp_actual = (entry - take_profit) * qty
            gross_sl_actual = (entry - stop) * qty
        exit_geom = "fixed_gross_usdt" if plan == exit_plans.LEGACY else "fixed_gross_partial70"
    else:
        if side == "LONG":
            stop = entry - config.ATR_STOP_MULT * atr
        else:
            stop = entry + config.ATR_STOP_MULT * atr
        take_profit = None
        if bool(getattr(config, "FIXED_TAKE_PROFIT_ENABLED", False)):
            tp_mult = float(getattr(config, "ATR_TP_MULT", 0.0) or 0.0)
            if tp_mult > 0:
                if side == "LONG":
                    take_profit = entry + tp_mult * atr
                else:
                    take_profit = entry - tp_mult * atr

    # skip duplicate qty check — already done
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
        margin_u=margin_u,
        notional_initial=entry * qty,
        leverage=config.LEVERAGE,
        atr_at_entry=atr,
        atr_time=str(signal.get("atr_time") or ""),
        initial_stop=stop,
        effective_stop=stop,
        take_profit=take_profit,
        take_profit_2=take_profit_2,
        tp50_price_raw=tp_raw if plan in (exit_plans.B_PARTIAL70, exit_plans.LEGACY) else None,
        tp100_price_raw=tp2_raw,
        sl75_price_raw=sl_raw if plan in (exit_plans.B_PARTIAL70, exit_plans.LEGACY) else None,
        exit_plan_id=plan,
        code_fix_tag=code_tag,
        pyramid=pyramid_state,
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
    st.cash_u -= margin_u
    st.cash_u -= fee
    st.realized_fees_u += fee
    st.realized_net_u -= fee  # 費用計入淨損益
    if sig_key not in st.processed_signals:
        st.processed_signals.append(sig_key)
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
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "strategy_version": config.STRATEGY_VERSION,
            "signal_key": sig_key,
            "signal_open_time": int(signal["signal_open_time"]),
            "margin_u": margin_u,
            "notional_u": entry * qty,
            "exit_plan_id": plan,
            "code_fix_tag": code_tag,
            "leverage": config.LEVERAGE,
            "atr": atr,
            "initial_stop": stop,
            "take_profit": take_profit,
            "atr_stop_mult": config.ATR_STOP_MULT,
            "atr_tp_mult": getattr(config, "ATR_TP_MULT", None),
            "exit_geom": exit_geom,
            "fixed_gross_exit_enabled": fixed_gross,
            "fixed_gross_tp_usdt_target": tp_usdt if fixed_gross else None,
            "fixed_gross_tp2_usdt_target": tp2_usdt if fixed_gross else None,
            "fixed_gross_sl_usdt_target": sl_usdt if fixed_gross else None,
            "fixed_gross_tp_price_raw": tp_raw if (fixed_gross or plan==exit_plans.C_PYRAMID) else None,
            "fixed_gross_sl_price_raw": sl_raw if (fixed_gross or plan==exit_plans.C_PYRAMID) else None,
            "take_profit_2": take_profit_2,
            "fixed_gross_tp_usdt_actual": gross_tp_actual if fixed_gross else None,
            "fixed_gross_sl_usdt_actual": gross_sl_actual if fixed_gross else None,
            "tick_size": tick if tick > 0 else None,
            "qty_sizing": "NOTIONAL_USDT/entry_rounded_step",
            "qty_depends_on_atr": False,
            "direction_flip": bool(signal.get("direction_flip")),
            "orig_side": signal.get("orig_side"),
            "entry_order_type": (
                "LIMIT_PULLBACK"
                if signal.get("limit_entry_filled") or signal.get("limit_entry_enabled")
                else "MARKET_EST"
            ),
            "signal_ref_price": signal.get("signal_ref_price"),
            "limit_price": signal.get("limit_price"),
            "limit_offset_pct": signal.get("limit_offset_pct"),
            "limit_entry_filled": bool(signal.get("limit_entry_filled")),
            "fee_rate_one_side": config.FEE_RATE_ONE_SIDE,
            "slippage_adverse": config.SLIPPAGE_ADVERSE,
            "simulation_assumption": True,
            "funding_data_gap": True,
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
    entry_override: Optional[float] = None,
    margin_release_u: Optional[float] = None,
    extra_fill: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    pos = get_position(st, symbol)
    if pos is None:
        return {"ok": False, "block_reason": "NO_POSITION"}
    qty = min(qty, pos.qty_remaining)
    if qty <= 0:
        return {"ok": False, "block_reason": "ZERO_QTY"}

    entry_px = float(entry_override) if entry_override is not None else float(pos.entry_price)
    fee = _fee(exit_price * qty)
    gross = _pnl(pos.side, entry_px, exit_price, qty)
    net = gross - fee
    pos.qty_remaining -= qty
    pos.fees_paid_u += fee
    pos.realized_net_u += net
    st.realized_fees_u += fee
    st.realized_net_u += net
    # 現金結算：gross 入帳一次、出場費扣一次；保證金僅在完全平倉時全額歸還（部分平倉不按比例釋放＝既有政策，待確認）
    st.cash_u += gross
    st.cash_u -= fee
    if margin_release_u and margin_release_u > 0 and pos.qty_remaining > 1e-12:
        rel = min(float(margin_release_u), float(pos.margin_u))
        st.cash_u += rel
        pos.margin_u = max(0.0, float(pos.margin_u) - rel)

    is_flat = pos.qty_remaining <= 1e-12
    hold_sec = max(0.0, book_ts - float(pos.entry_ts or book_ts))
    fill_row = {
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
            "cash_delta_u": gross - fee + (pos.margin_u if is_flat else 0.0),
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "strategy_version": config.STRATEGY_VERSION,
            "signal_key": f"{symbol}:{int(pos.signal_open_time)}:{pos.side}",
            "margin_u": pos.margin_u,
            "notional_u": exit_price * qty,
            "hold_sec": hold_sec,
            "effective_stop": pos.effective_stop,
            "partial_tp_done": pos.partial_tp_done,
            "exit_plan_id": getattr(pos, "exit_plan_id", None),
            "code_fix_tag": getattr(pos, "code_fix_tag", None),
            "mfe_u": pos.mfe_u,
            "mae_u": pos.mae_u,
            "mfe_mark": pos.mfe_mark,
            "mae_mark": pos.mae_mark,
            "mfe_ts": pos.mfe_ts,
            "mae_ts": pos.mae_ts,
            "excursion_source": pos.excursion_source,
            "funding_paid_u": pos.funding_paid_u,
            "funding_data_gap": pos.funding_gap,
            "fee_rate_one_side": config.FEE_RATE_ONE_SIDE,
            "slippage_adverse": config.SLIPPAGE_ADVERSE,
            "simulation_assumption": True,
            "cost_basis_note": "net_u=gross-fee; funding not claimed complete when funding_data_gap",
        }
    if extra_fill:
        fill_row.update(extra_fill)
    _append_fill(fill_row)
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
    return {"ok": True, "net": net, "qty": qty, "remaining": max(0.0, pos.qty_remaining if not is_flat else 0.0), "gross": gross, "fee": fee}


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
    atr_latest: Optional[float] = None,
    mark: Optional[float] = None,
) -> None:
    prev = pos.effective_stop
    changed = False
    if pos.side == "LONG":
        if candidate > pos.effective_stop:
            pos.effective_stop = candidate
            pos.stop_set_events.append({"ts": event_ts, "kind": kind, "price": candidate})
            changed = True
    else:
        if candidate < pos.effective_stop:
            pos.effective_stop = candidate
            pos.stop_set_events.append({"ts": event_ts, "kind": kind, "price": candidate})
            changed = True
    if changed:
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
                        "prev_stop": prev,
                        "new_stop": pos.effective_stop,
                        "extreme": pos.extreme_price,
                        "atr_latest": atr_latest,
                        "mark": mark,
                        "event_ts": event_ts,
                        "never_widens": True,
                    }
                )
        except Exception:
            pass


def _manage_legacy_fixed_tp_sl(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    atr_latest: Optional[float],
    phase: str,
    pos: Position,
    mark: float,
    book_ts: float,
) -> list[dict[str, Any]]:
    """Legacy / default: full close at stored take_profit or effective_stop. No rewrite."""
    events: list[dict[str, Any]] = []
    do_partial = phase in ("book", "full")
    do_stop = True
    if (
        do_partial
        and bool(getattr(config, "FIXED_TAKE_PROFIT_ENABLED", False))
        and pos.take_profit is not None
    ):
        tp = float(pos.take_profit)
        tp_hit = (pos.side == "LONG" and mark >= tp) or (pos.side == "SHORT" and mark <= tp)
        sl_hit = (pos.side == "LONG" and mark <= pos.effective_stop) or (
            pos.side == "SHORT" and mark >= pos.effective_stop
        )
        if tp_hit and not sl_hit:
            r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="FIXED_TP", book_ts=book_ts)
            events.append({"action": "FIXED_TP", "fill_price": mark, "tp_level": tp, **r})
            return events
    if do_stop:
        hit = (pos.side == "LONG" and mark <= pos.effective_stop) or (
            pos.side == "SHORT" and mark >= pos.effective_stop
        )
        if hit:
            pos = get_position(st, symbol)
            if pos is None:
                return events
            r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="INITIAL_STOP", book_ts=book_ts)
            events.append({"action": "INITIAL_STOP", "fill_price": mark, "stop_level": pos.effective_stop, **r})
    return events


def _manage_b_partial70(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    pos: Position,
    mark: float,
    book_ts: float,
) -> list[dict[str, Any]]:
    """70% at TP50 price; after filled move stop→entry; 30% targets TP100. SL75 until first TP filled."""
    events: list[dict[str, Any]] = []
    tp50 = float(pos.take_profit) if pos.take_profit is not None else None
    tp100 = float(pos.take_profit_2) if pos.take_profit_2 is not None else None
    sl = float(pos.effective_stop)

    sl_hit = (pos.side == "LONG" and mark <= sl) or (pos.side == "SHORT" and mark >= sl)
    tp50_hit = (not pos.partial_tp_done) and tp50 is not None and (
        (pos.side == "LONG" and mark >= tp50) or (pos.side == "SHORT" and mark <= tp50)
    )
    tp100_hit = pos.partial_tp_done and tp100 is not None and (
        (pos.side == "LONG" and mark >= tp100) or (pos.side == "SHORT" and mark <= tp100)
    )

    # 同報價競態：止損優先
    if sl_hit:
        reason = "ENTRY_STOP" if pos.partial_tp_done else "INITIAL_STOP"
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
        events.append({"action": reason, "fill_price": mark, "stop_level": sl, **r})
        return events

    if tp50_hit and not pos.partial_tp_done:
        frac = float(getattr(config, "PARTIAL_TP_FRACTION", 0.70) or 0.70)
        step = 0.001
        try:
            filt = (st.filters or {}).get(symbol) or {}
            step = float(filt.get("stepSize") or step)
        except Exception:
            pass
        qty_tp = round_step(pos.qty_initial * frac, step)
        qty_tp = min(qty_tp, pos.qty_remaining)
        # 留下至少可交易的尾數給 30%；若尾數過小則全平在 TP50
        rem_after = pos.qty_remaining - qty_tp
        min_qty = 0.0
        try:
            min_qty = float(((st.filters or {}).get(symbol) or {}).get("minQty") or 0.0)
        except Exception:
            pass
        if rem_after > 0 and rem_after < max(min_qty, step):
            qty_tp = pos.qty_remaining
        if qty_tp <= 0:
            events.append({"action": "PARTIAL_TP_FAILED", "block_reason": "ZERO_QTY"})
            return events
        r = close_qty(st, symbol, qty=qty_tp, exit_price=mark, reason="PARTIAL_TP50_70", book_ts=book_ts)
        events.append({"action": "PARTIAL_TP50_70", "tp_level": tp50, "fill_price": mark, **r})
        if not r.get("ok"):
            events.append({"action": "PARTIAL_TP_FAILED", "block_reason": r.get("block_reason")})
            return events
        pos = get_position(st, symbol)
        if pos is None:
            return events
        # 僅在實際成交成功後移動停損；進場價停損 ≠ 扣費保本
        if pos.qty_remaining > 1e-12:
            pos.partial_tp_done = True
            pos.partial_tp_qty = float(r.get("qty") or qty_tp)
            pos.partial_tp_price = mark
            pos.take_profit = tp100
            entry_stop = float(pos.entry_price)
            pos.breakeven_stop = entry_stop  # 名義欄位；語意=進場價停損
            pos.effective_stop = entry_stop
            pos.stop_set_events.append(
                {"ts": book_ts, "kind": "ENTRY_STOP_AFTER_PARTIAL", "price": entry_stop, "note": "not_fee_breakeven"}
            )
            set_position(st, pos)
            save_account(st)
            # 移動後若當下已觸價：用 mark 平剩餘
            if (pos.side == "LONG" and mark <= entry_stop) or (pos.side == "SHORT" and mark >= entry_stop):
                r2 = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="ENTRY_STOP", book_ts=book_ts)
                events.append({"action": "ENTRY_STOP_IMMEDIATE", "fill_price": mark, **r2})
        return events

    if tp100_hit and pos.partial_tp_done:
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="TP100_REMAINDER", book_ts=book_ts)
        events.append({"action": "TP100_REMAINDER", "tp_level": tp100, "fill_price": mark, **r})
    return events


def _pyramid_fill_add(
    st: AccountState,
    symbol: str,
    *,
    pos: Position,
    add_name: str,
    limit_px: float,
    book: dict[str, Any],
    book_ts: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    pyr = dict(pos.pyramid or {})
    margin_need = float(pyr.get("margin_per") or config.MARGIN_PER_TRADE_USDT)
    notional = float(pyr.get("notional_per") or config.NOTIONAL_USDT)
    step = float(pyr.get("step") or 0.001)
    if st.free_cash() + 1e-9 < margin_need:
        events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": add_name, "block_reason": "INSUFFICIENT_MARGIN"})
        return events
    qty = round_step(notional / limit_px, step)
    if qty <= 0:
        events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": add_name, "block_reason": "LOT_FILTER"})
        return events
    fee = _fee(limit_px * qty)
    st.cash_u -= margin_need
    st.cash_u -= fee
    st.realized_fees_u += fee
    st.realized_net_u -= fee
    pos.fees_paid_u += fee
    pos.qty_remaining += qty
    pos.qty_initial += qty
    pos.margin_u = float(pos.margin_u) + margin_need
    pos.notional_initial = float(pos.notional_initial) + limit_px * qty
    layers = list(pyr.get("layers") or [])
    layers.append({"name": add_name, "entry": limit_px, "qty": qty, "margin_u": margin_need, "closed": False, "exit_reason": None})
    pyr["layers"] = layers
    for a in pyr.get("pending_adds") or []:
        if a.get("name") == add_name:
            a["status"] = "FILLED"
            a["filled_at"] = now_iso()
            a["filled_price"] = limit_px
        elif add_name == "P1" and a.get("name") == "P2" and a.get("status") == "WAITING_P1":
            a["status"] = "PENDING"
    pos.pyramid = pyr
    set_position(st, pos)
    save_account(st)
    _append_fill(
        {
            "event": "PYRAMID_ADD",
            "trade_id": pos.trade_id,
            "symbol": symbol,
            "side": pos.side,
            "qty": qty,
            "price": limit_px,
            "fee_u": fee,
            "layer": add_name,
            "margin_u": margin_need,
            "exit_plan_id": pos.exit_plan_id,
            "code_fix_tag": pos.code_fix_tag,
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "simulation_assumption": True,
        }
    )
    events.append({"action": "PYRAMID_ADD", "layer": add_name, "qty": qty, "price": limit_px, "ok": True})
    return events


def _manage_c_pyramid(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    pos: Position,
    mark: float,
    book_ts: float,
) -> list[dict[str, Any]]:
    """R2: adds exit at P0 entry; P0 exits at 1% TP; all remaining at frozen Pstop."""
    events: list[dict[str, Any]] = []
    pyr = dict(pos.pyramid or {})
    levels = dict(pyr.get("levels") or {})
    pstop = float(levels.get("Pstop") or pos.effective_stop)
    p0_tp = float(levels.get("P0_TP") or pos.take_profit or 0.0)
    rebound = float(pyr.get("rebound_exit_price") or pos.entry_price)
    side = pos.side

    # 1) Pstop 優先（所有剩餘層）
    stop_hit = (side == "LONG" and mark <= pstop) or (side == "SHORT" and mark >= pstop)
    if stop_hit:
        # 逐層以各自進場價結算
        for layer in list(pyr.get("layers") or []):
            if layer.get("closed"):
                continue
            q = float(layer.get("qty") or 0.0)
            if q <= 0:
                continue
            r = close_qty(
                st,
                symbol,
                qty=q,
                exit_price=mark,
                reason="PYRAMID_PSTOP",
                book_ts=book_ts,
                entry_override=float(layer["entry"]),
                margin_release_u=float(layer.get("margin_u") or 0.0) if True else None,
                extra_fill={"layer": layer.get("name"), "pyramid_contrib": "stop"},
            )
            events.append({"action": "PYRAMID_PSTOP", "layer": layer.get("name"), **r})
            layer["closed"] = True
            layer["exit_reason"] = "PYRAMID_PSTOP"
            pos = get_position(st, symbol)
            if pos is None:
                exit_plans.cancel_pyramid_residual_adds(pyr, "PSTOP")
                return events
            # refresh pyr pointer if position still open
            pyr = dict(pos.pyramid or pyr)
            for L in pyr.get("layers") or []:
                if L.get("name") == layer.get("name"):
                    L["closed"] = True
                    L["exit_reason"] = "PYRAMID_PSTOP"
            pos.pyramid = pyr
            set_position(st, pos)
        pos = get_position(st, symbol)
        if pos is not None:
            exit_plans.cancel_pyramid_residual_adds(pos.pyramid or {}, "PSTOP")
            # force flat if dust
            if pos.qty_remaining > 1e-12:
                close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="PYRAMID_PSTOP", book_ts=book_ts)
            else:
                # ensure residual adds cancelled on stored state before clear via last close
                pass
        return events

    # 2) 追加限價觸價成交（需先成交才計該層盈虧）
    bid = float(book.get("bid") or 0.0)
    ask = float(book.get("ask") or 0.0)
    for a in list(pyr.get("pending_adds") or []):
        if a.get("status") != "PENDING":
            continue
        lim = float(a.get("limit_price") or 0.0)
        touched = (side == "LONG" and ask > 0 and ask <= lim) or (side == "SHORT" and bid > 0 and bid >= lim)
        if touched:
            ev = _pyramid_fill_add(st, symbol, pos=pos, add_name=str(a.get("name")), limit_px=lim, book=book, book_ts=book_ts)
            events.extend(ev)
            pos = get_position(st, symbol)
            if pos is None:
                return events
            pyr = dict(pos.pyramid or {})

    # 3) 追加層反彈至 P0 進場價退出（P0 不跟）
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    rebound_hit = (side == "LONG" and mark >= rebound) or (side == "SHORT" and mark <= rebound)
    if rebound_hit:
        for layer in list(pyr.get("layers") or []):
            if layer.get("closed") or layer.get("name") == "P0":
                continue
            q = float(layer.get("qty") or 0.0)
            if q <= 0:
                continue
            r = close_qty(
                st,
                symbol,
                qty=min(q, pos.qty_remaining),
                exit_price=mark,
                reason="PYRAMID_R2_REBOUND",
                book_ts=book_ts,
                entry_override=float(layer["entry"]),
                margin_release_u=float(layer.get("margin_u") or 0.0),
                extra_fill={"layer": layer.get("name"), "pyramid_contrib": "add_rebound"},
            )
            events.append({"action": "PYRAMID_R2_REBOUND", "layer": layer.get("name"), **r})
            pos = get_position(st, symbol)
            if pos is None:
                return events
            pyr = dict(pos.pyramid or {})
            for L in pyr.get("layers") or []:
                if L.get("name") == layer.get("name"):
                    L["closed"] = True
                    L["exit_reason"] = "PYRAMID_R2_REBOUND"
            # 不循環補倉：該層退出後不得重新補回（不影響其他未成交層）
            for a in pyr.get("pending_adds") or []:
                if a.get("name") == layer.get("name"):
                    a["status"] = "CANCELLED_NO_REFILL"
            pos.pyramid = pyr
            set_position(st, pos)
            save_account(st)

    # 4) P0 1% 止盈（僅平 P0 剩餘；若只有 P0 則全平）
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    tp_hit = p0_tp > 0 and ((side == "LONG" and mark >= p0_tp) or (side == "SHORT" and mark <= p0_tp))
    if tp_hit:
        p0_layer = next((L for L in (pyr.get("layers") or []) if L.get("name") == "P0" and not L.get("closed")), None)
        if p0_layer:
            q = float(p0_layer.get("qty") or 0.0)
            q = min(q, pos.qty_remaining)
            r = close_qty(
                st,
                symbol,
                qty=q,
                exit_price=mark,
                reason="PYRAMID_P0_TP1PCT",
                book_ts=book_ts,
                entry_override=float(p0_layer["entry"]),
                margin_release_u=float(p0_layer.get("margin_u") or 0.0),
                extra_fill={"layer": "P0", "pyramid_contrib": "p0_tp"},
            )
            events.append({"action": "PYRAMID_P0_TP1PCT", "tp_level": p0_tp, **r})
            p0_layer["closed"] = True
            p0_layer["exit_reason"] = "PYRAMID_P0_TP1PCT"
            pos = get_position(st, symbol)
            if pos is None:
                # 週期結束：清殘留加倉掛單狀態
                return events
            pyr = dict(pos.pyramid or {})
            for L in pyr.get("layers") or []:
                if L.get("name") == "P0":
                    L["closed"] = True
                    L["exit_reason"] = "PYRAMID_P0_TP1PCT"
            # 若仍有未平追加層，繼續持有；取消未成交加倉
            exit_plans.cancel_pyramid_residual_adds(pyr, "P0_TP")
            pos.pyramid = pyr
            # 若全部層已平
            if all(L.get("closed") for L in (pyr.get("layers") or [])) or pos.qty_remaining <= 1e-12:
                if pos.qty_remaining > 1e-12:
                    close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="PYRAMID_FLAT_DUST", book_ts=book_ts)
                else:
                    # margin already released per layer; clear if flat
                    if pos.qty_remaining <= 1e-12:
                        # close_qty may have cleared
                        pass
            else:
                set_position(st, pos)
                save_account(st)
    return events


def manage_position(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    atr_latest: Optional[float],
    phase: str = "full",
) -> list[dict[str, Any]]:
    """持倉管理（版本隔離）：依 pos.exit_plan_id 分支；缺省＝legacy 全平 TP/SL。"""
    events: list[dict[str, Any]] = []
    pos = get_position(st, symbol)
    if pos is None:
        return events
    book_ts = float(book.get("fetched_at") or time.time())
    mark = mark_price_for_side(pos.side, book)

    if pos.side == "LONG":
        pos.extreme_price = max(pos.extreme_price, mark)
    else:
        if pos.extreme_price <= 0:
            pos.extreme_price = mark
        pos.extreme_price = min(pos.extreme_price, mark)
    net_now = estimate_net_unrealized(pos, mark)
    ts_iso = now_iso()
    if net_now >= pos.mfe_u:
        pos.mfe_u = net_now
        pos.mfe_mark = mark
        pos.mfe_ts = ts_iso
    if net_now <= pos.mae_u:
        pos.mae_u = net_now
        pos.mae_mark = mark
        pos.mae_ts = ts_iso
    pos.excursion_source = "polled_quote_estimate"
    set_position(st, pos)
    save_account(st)

    plan = exit_plans.resolve_exit_plan_id(pos)
    if phase == "atr":
        # 新計畫不跑 ATR trail；legacy 亦不在 fixed-gross 下更新
        return events

    if plan == exit_plans.B_PARTIAL70:
        return _manage_b_partial70(st, symbol, book=book, pos=pos, mark=mark, book_ts=book_ts)
    if plan == exit_plans.C_PYRAMID:
        return _manage_c_pyramid(st, symbol, book=book, pos=pos, mark=mark, book_ts=book_ts)
    return _manage_legacy_fixed_tp_sl(
        st, symbol, book=book, atr_latest=atr_latest, phase=phase, pos=pos, mark=mark, book_ts=book_ts
    )



def count_reverses(st: AccountState, symbol: str, now: Optional[float] = None) -> int:
    now = now or now_ts()
    window = config.REVERSE_WINDOW_MINUTES * 60
    ev = [t for t in st.reverse_events.get(symbol, []) if now - t <= window]
    st.reverse_events[symbol] = ev
    return len(ev)


def note_reverse(st: AccountState, symbol: str) -> None:
    st.reverse_events.setdefault(symbol, []).append(now_ts())
    save_account(st)
