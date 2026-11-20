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
    bypass_risk_budget: bool = False,
) -> dict[str, Any]:
    """嘗試開倉。拒絕時不改資金。"""
    out: dict[str, Any] = {
        "ok": False,
        "block_reason": None,
        "block_reasons": [],
        "trade_id": None,
        "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
    }

    reasons: list[str] = []
    if st.inconsistency:
        reasons.append("STATE_INCONSISTENT")
    if st.day_new_entries_locked:
        reasons.append("DAILY_LOSS_LOCK")
    if symbol in st.positions:
        reasons.append("SAME_SYMBOL_POSITION")
    if len(st.positions) >= config.MAX_CONCURRENT_POSITIONS:
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
    # 數量仍依名義／成交價（不依 ATR 縮放）；ATR 僅用於訊號門檻與非 fixed-gross 退出幾何
    qty = round_step(config.NOTIONAL_USDT / entry, step)
    if qty <= 0 or qty < min_qty or qty * entry < min_notional:
        out["block_reason"] = "LOT_FILTER"
        return out

    from .market_data import round_price_tick

    fixed_gross = bool(getattr(config, "FIXED_GROSS_EXIT_ENABLED", False))
    tp_usdt = float(getattr(config, "FIXED_GROSS_TP_USDT", 0.0) or 0.0)
    sl_usdt = float(getattr(config, "FIXED_GROSS_SL_USDT", 0.0) or 0.0)
    exit_geom = "atr"
    tp_raw = sl_raw = None
    gross_tp_actual = gross_sl_actual = None

    if fixed_gross:
        if tp_usdt <= 0 or sl_usdt <= 0:
            out["block_reason"] = "INVALID_FIXED_GROSS_TARGETS"
            out["block_reasons"] = ["INVALID_FIXED_GROSS_TARGETS"]
            return out
        # 合約數量單位：損益 = ΔP * q（USDT 報價合約）；毛目標 USDT → 價格距離 = USDT/q
        d_tp = tp_usdt / qty
        d_sl = sl_usdt / qty
        if side == "LONG":
            tp_raw = entry + d_tp
            sl_raw = entry - d_sl
        else:
            tp_raw = entry - d_tp
            sl_raw = entry + d_sl
        take_profit = round_price_tick(tp_raw, tick)
        stop = round_price_tick(sl_raw, tick)
        if take_profit <= 0 or stop <= 0:
            out["block_reason"] = "INVALID_BARRIER_AFTER_TICK"
            out["block_reasons"] = ["INVALID_BARRIER_AFTER_TICK"]
            return out
        # 方向健全性（tick 後不得翻到錯誤側）
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
        exit_geom = "fixed_gross_usdt"
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

    risk = estimate_initial_risk_u(side=side, entry=entry, stop=stop, qty=qty)
    out["risk_estimate_u"] = risk
    if (not bypass_risk_budget) and risk > config.MAX_LOSS_PER_TRADE_USDT:
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
        take_profit=take_profit,
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
            "margin_u": config.MARGIN_PER_TRADE_USDT,
            "notional_u": entry * qty,
            "leverage": config.LEVERAGE,
            "atr": atr,
            "initial_stop": stop,
            "take_profit": take_profit,
            "atr_stop_mult": config.ATR_STOP_MULT,
            "atr_tp_mult": getattr(config, "ATR_TP_MULT", None),
            "exit_geom": exit_geom,
            "fixed_gross_exit_enabled": fixed_gross,
            "fixed_gross_tp_usdt_target": tp_usdt if fixed_gross else None,
            "fixed_gross_sl_usdt_target": sl_usdt if fixed_gross else None,
            "fixed_gross_tp_price_raw": tp_raw if fixed_gross else None,
            "fixed_gross_sl_price_raw": sl_raw if fixed_gross else None,
            "fixed_gross_tp_usdt_actual": gross_tp_actual if fixed_gross else None,
            "fixed_gross_sl_usdt_actual": gross_sl_actual if fixed_gross else None,
            "tick_size": tick if tick > 0 else None,
            "qty_sizing": "NOTIONAL_USDT/entry_rounded_step",
            "qty_depends_on_atr": False,
            "direction_flip": bool(signal.get("direction_flip")),
            "orig_side": signal.get("orig_side"),
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
    # 現金結算：gross 入帳一次、出場費扣一次；保證金僅在完全平倉時全額歸還（部分平倉不按比例釋放＝既有政策，待確認）
    st.cash_u += gross
    st.cash_u -= fee

    is_flat = pos.qty_remaining <= 1e-12
    hold_sec = max(0.0, book_ts - float(pos.entry_ts or book_ts))
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


def manage_position(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    atr_latest: Optional[float],
    phase: str = "full",
) -> list[dict[str, Any]]:
    """持倉管理：部分止盈、含成本保本、移動停利、初始止損。

    phase:
      - "book": 極值／PARTIAL／BE／既有 effective_stop 觸價（含已有 trail/BE/initial）；不更新 ATR trail
      - "atr": 僅在仍持倉時更新 ATR trail（失敗不清除既有 stop）；可觸價
      - "full": 相容舊行為（book+atr 同一次）

    策略數值不變。禁止舊 book／倒填停損價成交。每次開始重讀 position。
    """
    events: list[dict[str, Any]] = []
    pos = get_position(st, symbol)
    if pos is None:
        return events
    book_ts = float(book.get("fetched_at") or time.time())
    mark = mark_price_for_side(pos.side, book)

    # 更新極值（用可成交價；多只升、空只降）並立刻保存，避免後續 get_position 讀回舊值
    # 注意：啟用前的極值只供後續 trail 計算參考，不得拿來倒填歷史成交價。
    if pos.side == "LONG":
        pos.extreme_price = max(pos.extreme_price, mark)
    else:
        if pos.extreme_price <= 0:
            pos.extreme_price = mark
        pos.extreme_price = min(pos.extreme_price, mark)
    # MFE/MAE：輪詢報價採樣估計（標記 excursion_source；非 tick 精確路徑）
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

    do_partial = phase in ("book", "full")
    do_atr = phase in ("atr", "full")
    do_stop = True  # book／atr／full 皆可用既有 effective_stop 觸價
    # 固定毛額退出：禁止 partial／BE／trail 覆寫建倉障礙（即使旗標被誤開）
    fixed_gross_pos = bool(getattr(config, "FIXED_GROSS_EXIT_ENABLED", False))
    if fixed_gross_pos:
        do_atr = False  # 不跑 ATR trail 更新

    # 0) 固定止盈（本輪反向實驗）；與止損同標時優先止損
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
        # 若同報價同時觸及，落入下方止損路徑（止損優先）

    # 1) 部分止盈（成功後才開保本／移動停利）— 僅 book/full；已 done 不得再 PARTIAL
    if (
        do_partial
        and (not fixed_gross_pos)
        and bool(getattr(config, "ENABLE_PARTIAL_TP", True))
        and not pos.partial_tp_done
    ):
        net_u = estimate_net_unrealized(pos, mark)
        if net_u >= config.PARTIAL_TP_NET_USDT:
            step = 0.001
            qty_tp = round_step(pos.qty_initial * config.PARTIAL_TP_FRACTION, step)
            qty_tp = min(qty_tp, pos.qty_remaining)
            if qty_tp > 0:
                # close_qty 會從帳戶重讀；極值已保存
                extreme_saved = pos.extreme_price
                r = close_qty(st, symbol, qty=qty_tp, exit_price=mark, reason="PARTIAL_TP", book_ts=book_ts)
                events.append({"action": "PARTIAL_TP", **r})
                if not r.get("ok"):
                    # 部分止盈失敗：不啟用保本／移動停利；初始止損路徑繼續
                    events.append({"action": "PARTIAL_TP_FAILED", "block_reason": r.get("block_reason") or r.get("error")})
                else:
                    pos = get_position(st, symbol)
                    if pos is None:
                        return events
                    # 確保部分平倉後極值／狀態不遺失
                    pos.extreme_price = max(pos.extreme_price, extreme_saved) if pos.side == "LONG" else (
                        min(pos.extreme_price, extreme_saved) if pos.extreme_price > 0 else extreme_saved
                    )
                    pos.partial_tp_done = True
                    pos.partial_tp_qty = qty_tp
                    pos.partial_tp_price = mark
                    if bool(getattr(config, "ENABLE_BREAKEVEN", True)):
                        be = breakeven_price(pos)
                        pos.breakeven_stop = be
                        # 啟用當下若報價已越過保本：只能用當下 mark 處理，禁止倒填停損價成交
                        if pos.side == "LONG" and mark <= be:
                            r2 = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="BREAKEVEN_STOP", book_ts=book_ts)
                            events.append({"action": "BREAKEVEN_STOP_IMMEDIATE", "fill_price": mark, "stop_level": be, **r2})
                            return events
                        if pos.side == "SHORT" and mark >= be:
                            r2 = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="BREAKEVEN_STOP", book_ts=book_ts)
                            events.append({"action": "BREAKEVEN_STOP_IMMEDIATE", "fill_price": mark, "stop_level": be, **r2})
                            return events
                        tighten_stop(pos, be, "BREAKEVEN_STOP", book_ts, atr_latest=atr_latest, mark=mark)
                    set_position(st, pos)
                    save_account(st)


    pos = get_position(st, symbol)
    if pos is None:
        return events

    # 2) 移動停利：僅部分止盈成功後；需最新 ATR；只收緊不放寬；ATR 失敗不清除既有 stop
    if (
        do_atr
        and bool(getattr(config, "ENABLE_TRAILING_STOP", True))
        and bool(getattr(config, "ENABLE_BREAKEVEN", True))  # trail 依賴 partial→BE 鏈；BE 關則 trail 亦關
        and pos.partial_tp_done
        and atr_latest
        and atr_latest > 0
    ):
        if pos.side == "LONG":
            trail = pos.extreme_price - config.TRAIL_ATR_MULT * atr_latest
        else:
            trail = pos.extreme_price + config.TRAIL_ATR_MULT * atr_latest
        pos.trailing_stop = trail
        tighten_stop(pos, trail, "TRAILING_STOP", book_ts, atr_latest=atr_latest, mark=mark)
        set_position(st, pos)
        save_account(st)
        # 啟用／更新後若當下報價已越過有效停損：用 mark 平倉，不倒填 trail 價
        pos = get_position(st, symbol)
        if pos is None:
            return events

    if not do_stop:
        return events

    # 3) 觸發有效止損（僅當下 book；成交價=mark，絕不使用歷史極值或停損價倒填）
    # Book 階段不更新 ATR 時，仍必須執行既有 trail／BE／initial
    hit = False
    reason = "INITIAL_STOP"
    if pos.side == "LONG" and mark <= pos.effective_stop:
        hit = True
        if pos.partial_tp_done and pos.trailing_stop is not None and abs(pos.effective_stop - pos.trailing_stop) < 1e-12:
            reason = "TRAILING_STOP"
        elif pos.partial_tp_done and pos.breakeven_stop is not None and abs(pos.effective_stop - pos.breakeven_stop) < 1e-12:
            reason = "BREAKEVEN_STOP"
        elif abs(pos.effective_stop - pos.initial_stop) < 1e-12:
            reason = "INITIAL_STOP"
        else:
            reason = "TRAILING_STOP" if (pos.partial_tp_done and pos.trailing_stop is not None) else (
                "BREAKEVEN_STOP" if pos.breakeven_stop is not None else "INITIAL_STOP"
            )
    if pos.side == "SHORT" and mark >= pos.effective_stop:
        hit = True
        if pos.partial_tp_done and pos.trailing_stop is not None and abs(pos.effective_stop - pos.trailing_stop) < 1e-12:
            reason = "TRAILING_STOP"
        elif pos.partial_tp_done and pos.breakeven_stop is not None and abs(pos.effective_stop - pos.breakeven_stop) < 1e-12:
            reason = "BREAKEVEN_STOP"
        elif abs(pos.effective_stop - pos.initial_stop) < 1e-12:
            reason = "INITIAL_STOP"
        else:
            reason = "TRAILING_STOP" if (pos.partial_tp_done and pos.trailing_stop is not None) else (
                "BREAKEVEN_STOP" if pos.breakeven_stop is not None else "INITIAL_STOP"
            )

    if hit:
        # 成交前再讀倉，避免舊快照對已平倉再 close
        pos = get_position(st, symbol)
        if pos is None:
            return events
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
        events.append({"action": reason, "fill_price": mark, "stop_level": pos.effective_stop, **r})
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
