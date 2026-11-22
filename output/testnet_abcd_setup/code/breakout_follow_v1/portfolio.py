"""Paper portfolio: open/partial/trail/reverse — simulation fills only."""

from __future__ import annotations

import csv
import time
import uuid
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


def _fee_rate_for_role(role: str) -> float:
    """Limit/maker → MAKER_FEE_RATE; protective/taker → TAKER_FEE_RATE (fallback FEE_RATE_ONE_SIDE)."""
    if role == "maker":
        return float(getattr(config, "MAKER_FEE_RATE", config.FEE_RATE_ONE_SIDE) or config.FEE_RATE_ONE_SIDE)
    return float(getattr(config, "TAKER_FEE_RATE", config.FEE_RATE_ONE_SIDE) or config.FEE_RATE_ONE_SIDE)


def _fee(notional: float, role: str = "taker") -> float:
    """Fee on notional by liquidity role (limit fills = maker 0.0002; stops = taker 0.0005)."""
    return abs(float(notional)) * _fee_rate_for_role(role)


def _exit_liquidity_role(reason: str, extra_fill: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """Resolve exit fee role. Resting/limit TP = maker; SL/protective = taker."""
    if extra_fill and extra_fill.get("force_maker_fee"):
        return "maker", "resting_maker_fill"
    if extra_fill and extra_fill.get("liquidity_role_assumption") in ("maker", "taker"):
        role = str(extra_fill.get("liquidity_role_assumption"))
        return role, f"extra_fill_{role}"
    from . import c_maker_exec_model as _mm

    role = _mm.role_for_exit_reason(reason)
    note = "limit_tp_maker" if role == "maker" else "protective_or_stop_taker"
    return role, note


def _set_position_keep_testnet(st: AccountState, pos: Position) -> None:
    """Persist Position without dropping testnet_* keys stashed on the position dict."""
    prev = st.positions.get(pos.symbol) or {}
    d = pos.to_dict()
    if isinstance(prev, dict):
        for k, v in prev.items():
            if str(k).startswith("testnet_") and k not in d:
                d[k] = v
    st.positions[pos.symbol] = d


def _sync_pending_close_order(
    st: AccountState, symbol: str, *, book: dict[str, Any], book_ts: float
) -> list[dict[str, Any]]:
    """Poll accepted-but-unfilled close; apply incremental executed qty only."""
    from common.execution_sync import clear_pending_close_fields, observe_pending_close

    events: list[dict[str, Any]] = []
    pd = st.positions.get(symbol)
    if not isinstance(pd, dict) or not pd.get("testnet_close_client_order_id"):
        return events
    try:
        from . import testnet_hooks

        if not testnet_hooks.enabled():
            return events
        profile_id = str(getattr(config, "PROFILE_ID", "") or "")
        polled = testnet_hooks.poll_order(
            symbol=symbol,
            client_order_id=str(pd["testnet_close_client_order_id"]),
            profile_id=profile_id,
        )
    except Exception as exc:  # noqa: BLE001
        events.append({"action": "CLOSE_POLL_UNKNOWN", "symbol": symbol, "error": str(exc)})
        return events
    obs = observe_pending_close(pd, polled)
    for k, v in (obs.get("updates") or {}).items():
        pd[k] = v
    st.positions[symbol] = pd
    save_account(st)
    if obs.get("action") == "HOLD_UNKNOWN":
        events.append({"action": "CLOSE_POLL_UNKNOWN", "symbol": symbol})
        return events
    if obs.get("action") == "RELEASE_AWAITING":
        clear_pending_close_fields(pd)
        st.positions[symbol] = pd
        save_account(st)
        events.append({"action": "CLOSE_AWAITING_RELEASED", "status": obs.get("status")})
        return events
    if obs.get("action") == "APPLY_DELTA":
        delta = float(obs.get("delta_qty") or 0)
        px = obs.get("delta_avg")
        price_status = str(obs.get("price_status") or "known")
        pos = get_position(st, symbol)
        if pos is None:
            return events
        if px is None:
            px = mark_price_for_side(pos.side, book)
            price_status = "estimated_local_mark_fallback"
        r = close_qty(
            st,
            symbol,
            qty=delta,
            exit_price=float(px),
            reason=str(pd.get("testnet_close_reason") or "CLOSE_PENDING_FILL"),
            book_ts=book_ts,
            exchange_already_done=True,
            extra_fill={
                "testnet_client_order_id": obs.get("client_order_id") or pd.get("testnet_close_client_order_id"),
                "binance_order_id": obs.get("exchange_order_id") or pd.get("testnet_close_exchange_order_id"),
                "binance_avg_price": obs.get("delta_avg"),  # None when unknown — never mark
                "binance_executed_qty": delta,
                "via": "BINANCE_CLOSE_POLL_DELTA",
                "fill_source": "exchange_execution_confirmed",
                "qty_confirmation": "exchange_executed_qty",
                "price_status": price_status,
                "accounting_price_role": (
                    "local_mark_fallback"
                    if str(price_status).startswith(("estimated", "unknown"))
                    else "exchange_slice_avg"
                ),
            },
        )
        events.append({"action": "CLOSE_PENDING_DELTA", **r})
        return events
    if obs.get("action") == "CLEAR_TRACKING":
        clear_pending_close_fields(pd)
        if symbol in st.positions:
            st.positions[symbol] = pd
            save_account(st)
        events.append({"action": "CLOSE_TRACKING_CLEARED", "symbol": symbol})
    return events


def _digest_c_partial_pending_applies(
    st: AccountState,
    symbol: str,
    *,
    pos: Position,
    book: dict[str, Any],
    book_ts: float,
) -> list[dict[str, Any]]:
    """Apply PARTIAL_PENDING_APPLY deltas once per add layer (restart-safe via applied marker)."""
    events: list[dict[str, Any]] = []
    pos = get_position(st, symbol) or pos
    pyr = dict(pos.pyramid or {})
    # snapshot names first — fill_add mutates/reloads pyramid
    pending_names = [
        str(a.get("name") or "")
        for a in list(pyr.get("pending_adds") or [])
        if str(a.get("status") or "") == "PARTIAL_PENDING_APPLY"
    ]
    for name in pending_names:
        if not name:
            continue
        pos = get_position(st, symbol) or pos
        pyr = dict(pos.pyramid or {})
        a = next((x for x in (pyr.get("pending_adds") or []) if x.get("name") == name), None)
        if not a or str(a.get("status") or "") != "PARTIAL_PENDING_APPLY":
            continue
        delta = float(a.get("pending_fill_delta") or 0)
        already = float(a.get("applied_fill_qty") or 0)
        total_filled = float(a.get("qty_filled") or 0)
        # restart-safe: only apply unapplied portion of cumulative fill
        to_apply = total_filled - already
        if to_apply <= 1e-12 and delta > 1e-12 and already <= 1e-12:
            to_apply = delta
        planned = float(a.get("planned_qty") or total_filled or 0)
        if to_apply <= 1e-12:
            a["status"] = "FILLED" if total_filled + 1e-12 >= planned else "PARTIAL"
            a.pop("pending_fill_delta", None)
            pos.pyramid = pyr
            _set_position_keep_testnet(st, pos)
            save_account(st)
            continue
        lim = float(a.get("limit_price") or a.get("filled_price") or 0)
        if lim <= 0:
            continue
        # Preserve P0 entry basis: pyramid add uses layer fill price, not re-average P0
        r = _pyramid_fill_add(
            st,
            symbol,
            pos=pos,
            add_name=name,
            limit_px=lim,
            book=book,
            book_ts=book_ts,
            qty_override=to_apply,
            exchange_already_done=True,
        )
        events.extend(r)
        # reload after fill_add save — write applied marker onto persisted pending_adds
        pos = get_position(st, symbol) or pos
        pyr = dict(pos.pyramid or {})
        a2 = next((x for x in (pyr.get("pending_adds") or []) if x.get("name") == name), None)
        if a2 is not None:
            a2["applied_fill_qty"] = already + to_apply
            a2["pending_fill_delta"] = 0.0
            planned2 = float(a2.get("planned_qty") or a2.get("qty_filled") or 0)
            a2["status"] = (
                "FILLED" if float(a2["applied_fill_qty"]) + 1e-12 >= planned2 else "PARTIAL"
            )
            a2["fill_source"] = "exchange_execution_confirmed"
        pos.pyramid = pyr
        _set_position_keep_testnet(st, pos)
        save_account(st)
    return events


def reconcile_exchange_flat(
    st: AccountState,
    symbol: str,
    *,
    position_amt_observed: float = 0.0,
    evidence_source: str = "positionRisk",
    client_order_id: Any = None,
    observed_at: float | None = None,
    position_side: str | None = None,
    env_id: str | None = None,
    account_id: str | None = None,
    reason: str = "EXCHANGE_FLAT_RECONCILE",
    max_age_sec: float = 120.0,
) -> dict[str, Any]:
    """Clear local exposure when exchange is flat — no invented CLOSE/price/fee/pnl."""
    from common.execution_sync import (
        build_position_flat_evidence,
        build_settlement_pending_record,
        find_settlement_pending_for_symbol,
        reconcile_flat_event_row,
        validate_position_flat_evidence,
    )

    pos = get_position(st, symbol)
    if pos is None:
        tid_sp, sp = find_settlement_pending_for_symbol(
            getattr(st, "settlement_pending", None), symbol
        )
        if sp:
            return {
                "ok": True,
                "skipped": True,
                "reason": "ALREADY_SETTLEMENT_PENDING",
                "local_trade_id": tid_sp,
                "settlement_status": sp.get("settlement_status"),
                "fully_reconciled": False,
            }
        return {"ok": True, "skipped": True, "reason": "NO_LOCAL_POSITION"}
    side = str(position_side or pos.side or "BOTH")
    flat_ev = build_position_flat_evidence(
        symbol=symbol,
        position_side=side,
        position_amt_observed=position_amt_observed,
        evidence_source=evidence_source,
        observed_at=observed_at,
        env_id=env_id,
        account_id=account_id or str(getattr(config, "PROFILE_ID", "") or ""),
        client_order_id=client_order_id,
    )
    v = validate_position_flat_evidence(
        flat_ev,
        expect_symbol=symbol,
        expect_position_side=side,
        expect_env=env_id,
        expect_account=account_id,
        max_age_sec=max_age_sec,
    )
    if not v.get("ok"):
        return {"ok": False, "block_reason": f"FLAT_EVIDENCE_REJECTED:{v.get('reason')}", **v}
    before_qty = float(pos.qty_remaining)
    before_entry = float(pos.entry_price)
    margin = float(pos.margin_u or 0)
    trade_id = pos.trade_id
    pd_snap = dict(st.positions.get(symbol) or {}) if isinstance(st.positions.get(symbol), dict) else {}
    st.cash_u += margin
    clear_position(st, symbol)
    st.pending_limit_entries.pop(symbol, None)
    if not hasattr(st, "settlement_pending") or st.settlement_pending is None:
        st.settlement_pending = {}
    if trade_id:
        st.settlement_pending[str(trade_id)] = build_settlement_pending_record(
            local_trade_id=trade_id,
            symbol=symbol,
            side=pos.side,
            position_side=side,
            entry_price=before_entry,
            qty_to_settle=before_qty,
            margin_released_u=margin,
            flat_evidence=flat_ev,
            reason=reason,
            entry_fee_estimated=(pd_snap or {}).get("entry_fee_estimated"),
            entry_fee_status=str((pd_snap or {}).get("entry_fee_status") or "estimated_schedule_rate"),
            entry_client_order_id=(pd_snap or {}).get("testnet_entry_client_order_id"),
            entry_exchange_order_id=(pd_snap or {}).get("testnet_entry_exchange_order_id"),
            exit_client_order_id=(pd_snap or {}).get("testnet_close_client_order_id"),
            exit_exchange_order_id=(pd_snap or {}).get("testnet_close_exchange_order_id"),
        )
    row = reconcile_flat_event_row(
        strategy=str(getattr(config, "PROFILE_ID", "B/C") or "B/C"),
        symbol=symbol,
        trade_id=trade_id,
        side=pos.side,
        before_qty=before_qty,
        before_entry=before_entry,
        margin_released=margin,
        flat_evidence=flat_ev,
        reason=reason,
    )
    row["profile_id"] = getattr(config, "PROFILE_ID", config.STRATEGY_ID)
    row["run_id"] = st.run_id
    row["settlement_pending_key"] = str(trade_id) if trade_id else None
    row["cash_equity_incomplete"] = True
    _append_fill(row)
    save_account(st)
    return {"ok": True, **row}


def _recover_settlement_journal(st: AccountState) -> dict[str, Any]:
    from common.settlement_apply import recover_account_settlement_journal

    if not isinstance(getattr(st, "settlement_journal", None), list):
        st.settlement_journal = list(getattr(st, "settlement_journal", None) or [])
    return recover_account_settlement_journal(
        st,
        append_fill_fn=_append_fill,
        save_fn=save_account,
        fills_path=config.FILLS_PATH,
    )


def adjust_entry_fee_to_exchange(
    st: AccountState,
    *,
    local_trade_id: str,
    exchange_commission: float | None,
    commission_asset: str | None = "USDT",
    exchange_trade_id: Any = None,
    order_id: Any = None,
    trade_qty: float | None = None,
) -> dict[str, Any]:
    from common.settlement_apply import journal_adjust_entry_fee

    return journal_adjust_entry_fee(
        st,
        local_trade_id=local_trade_id,
        exchange_commission=exchange_commission,
        commission_asset=commission_asset,
        exchange_trade_id=exchange_trade_id,
        order_id=order_id,
        trade_qty=trade_qty,
        save_fn=save_account,
        append_fill_fn=_append_fill,
        fills_path=config.FILLS_PATH,
    )


def apply_pending_settlement_fills(
    st: AccountState,
    *,
    local_trade_id: str,
    trades: list[dict[str, Any]],
    entry_attributed: bool | None = None,
    exit_attributed: bool | None = None,
    query_incomplete: bool = False,
    unattributed_remaining: bool = False,
) -> dict[str, Any]:
    """B/C formal portfolio settlement apply via PREPARED→APPLIED→FILL_APPENDED journal."""
    from common.settlement_apply import journal_apply_exit_fills

    if not isinstance(getattr(st, "settlement_journal", None), list):
        st.settlement_journal = []
    side = "LONG"
    pend = (st.settlement_pending or {}).get(str(local_trade_id))
    if isinstance(pend, dict) and pend.get("side"):
        side = str(pend.get("side"))
    return journal_apply_exit_fills(
        st,
        local_trade_id=local_trade_id,
        trades=trades,
        save_fn=save_account,
        append_fill_fn=_append_fill,
        fee_fn=_fee,
        default_side=side,
        entry_attributed=entry_attributed,
        exit_attributed=exit_attributed,
        query_incomplete=query_incomplete,
        unattributed_remaining=unattributed_remaining,
        strategy_meta={
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": getattr(st, "run_id", ""),
            "strategy_version": getattr(config, "STRATEGY_VERSION", ""),
        },
        fills_path=config.FILLS_PATH,
        recover_fn=_recover_settlement_journal,
    )


def sync_settlement_pending(
    st: AccountState,
    *,
    local_trade_id: str | None = None,
    fetch_user_trades_fn: Any = None,
    profile_id: str = "",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
) -> dict[str, Any]:
    from common.settlement_sync import sync_pending_with_fetch

    results = []
    tids = [str(local_trade_id)] if local_trade_id else [
        t for t, r in (st.settlement_pending or {}).items() if isinstance(r, dict) and not r.get("fully_reconciled")
    ]
    fetch = fetch_user_trades_fn
    if fetch is None:
        try:
            from . import testnet_hooks

            def fetch(**kw):
                return testnet_hooks.fetch_user_trades(profile_id=profile_id or getattr(config, "PROFILE_ID", ""), **kw)

        except Exception:
            fetch = None
    for tid in tids:
        pending = (st.settlement_pending or {}).get(tid)
        if not isinstance(pending, dict):
            results.append({"ok": False, "local_trade_id": tid, "block_reason": "NO_SETTLEMENT_PENDING"})
            continue
        if pending.get("settlement_recovery_needed"):
            results.append(
                {
                    "ok": False,
                    "local_trade_id": tid,
                    "block_reason": "SETTLEMENT_RECOVERY_NEEDED",
                    "note": "no blind close resend; cash_equity incomplete until recovered",
                }
            )
            continue
        if fetch is None:
            results.append({"ok": False, "local_trade_id": tid, "block_reason": "NO_PENDING_OR_FETCH"})
            continue
        got = sync_pending_with_fetch(
            pending=pending,
            local_trade_id=tid,
            fetch_user_trades_fn=fetch,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )
        if not got.get("ok"):
            results.append({**got, "local_trade_id": tid, "query_incomplete": True})
            continue
        fee_adj_results = []
        for et in got["entry_trades"]:
            fee_adj_results.append(
                adjust_entry_fee_to_exchange(
                    st,
                    local_trade_id=tid,
                    exchange_commission=et.get("commission"),
                    commission_asset=et.get("commissionAsset"),
                    exchange_trade_id=et.get("id"),
                    order_id=et.get("orderId"),
                    trade_qty=float(et.get("qty") or 0) or None,
                )
            )
        app = apply_pending_settlement_fills(
            st,
            local_trade_id=tid,
            trades=got["exit_trades"],
            entry_attributed=bool(got["entry_trades"]),
            exit_attributed=bool(got["exit_trades"]),
            query_incomplete=bool(got.get("query_incomplete")),
            unattributed_remaining=bool(got.get("unattributed")),
        )
        results.append(
            {
                "ok": app.get("ok"),
                "local_trade_id": tid,
                "apply": app,
                "entry_fee_adjusts": fee_adj_results,
                "sync": got,
            }
        )
    return {"ok": all(r.get("ok") for r in results) if results else True, "results": results}


def _tp_qty_split(qty_initial: float, filters: dict[str, Any]) -> tuple[float, float]:
    step = float(filters.get("stepSize") or 0.001)
    min_qty = float(filters.get("minQty") or 0.0)
    frac = float(getattr(config, "PARTIAL_TP_FRACTION", 0.70) or 0.70)
    qty_tp1 = round_step(qty_initial * frac, step)
    qty_tp1 = min(qty_tp1, qty_initial)
    rem = round(qty_initial - qty_tp1, 12)
    if rem > 0 and rem < max(min_qty, step):
        qty_tp1 = qty_initial
        rem = 0.0
    return qty_tp1, rem


def _preplace_b_tp_orders(
    st: AccountState,
    pos: Position,
    *,
    profile_id: str = "",
) -> list[dict[str, Any]]:
    """Hang reduce-only TP1(70%)+TP2(30%) limits — mirror onto Binance like strategy A."""
    if pos.take_profit is None or pos.take_profit_2 is None:
        return []
    filters = (st.filters or {}).get(pos.symbol) or {}
    qty1, qty2 = _tp_qty_split(float(pos.qty_initial), filters)
    close_side = "SELL" if pos.side == "LONG" else "BUY"
    orders: list[dict[str, Any]] = []
    if qty1 > 0:
        orders.append(
            {
                "order_id": f"{pos.trade_id}:TP1:{uuid.uuid4().hex[:8]}",
                "level": "TP1",
                "side": close_side,
                "reduce_only": True,
                "limit_price": float(pos.take_profit),
                "qty": qty1,
                "qty_filled": 0.0,
                "status": "NEW",
                "placed_at": now_iso(),
            }
        )
    if qty2 > 0:
        orders.append(
            {
                "order_id": f"{pos.trade_id}:TP2:{uuid.uuid4().hex[:8]}",
                "level": "TP2",
                "side": close_side,
                "reduce_only": True,
                "limit_price": float(pos.take_profit_2),
                "qty": qty2,
                "qty_filled": 0.0,
                "status": "NEW",
                "placed_at": now_iso(),
            }
        )
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            for o in orders:
                tn = testnet_hooks.place_tp_limit(
                    symbol=pos.symbol,
                    position_side=str(pos.side),
                    qty=float(o["qty"]),
                    price=float(o["limit_price"]),
                    level=str(o["level"]),
                    profile_id=profile_id or str(getattr(config, "PROFILE_ID", "") or ""),
                )
                if tn.get("ok") and not tn.get("skipped"):
                    o["testnet_client_order_id"] = tn.get("client_order_id")
                    o["testnet_exchange_order_id"] = tn.get("exchange_order_id")
                else:
                    o["testnet_place_error"] = tn.get("error")
    except Exception as exc:  # noqa: BLE001
        for o in orders:
            o["testnet_place_error"] = str(exc)
    pos.resting_tp_orders = orders
    _set_position_keep_testnet(st, pos)
    save_account(st)
    _append_fill(
        {
            "event": "TP_PREPLACED",
            "trade_id": pos.trade_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "orders": [
                {
                    "order_id": o["order_id"],
                    "level": o["level"],
                    "limit_price": o["limit_price"],
                    "qty": o["qty"],
                    "testnet_client_order_id": o.get("testnet_client_order_id"),
                    "testnet_place_error": o.get("testnet_place_error"),
                }
                for o in orders
            ],
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "strategy_version": config.STRATEGY_VERSION,
            "note": "B TP1 70% + TP2 30% reduceOnly LIMIT (not TAKE_PROFIT_MARKET full close)",
        }
    )
    return orders


def _cancel_resting_b_tp(
    st: AccountState,
    pos: Position,
    reason: str,
    *,
    profile_id: str = "",
) -> list[dict[str, Any]]:
    cancelled: list[dict[str, Any]] = []
    try:
        from . import testnet_hooks

        tn_on = testnet_hooks.enabled()
    except Exception:
        tn_on = False
        testnet_hooks = None  # type: ignore
    pid = profile_id or str(getattr(config, "PROFILE_ID", "") or "")
    for o in pos.resting_tp_orders or []:
        if o.get("status") in ("NEW", "PARTIAL"):
            if tn_on and o.get("testnet_client_order_id") and testnet_hooks is not None:
                testnet_hooks.cancel_cid(
                    symbol=pos.symbol,
                    client_order_id=str(o["testnet_client_order_id"]),
                    profile_id=pid,
                )
            o["status"] = "CANCELLED"
            o["cancel_reason"] = reason
            o["cancelled_at"] = now_iso()
            cancelled.append(
                {
                    "order_id": o.get("order_id"),
                    "level": o.get("level"),
                    "qty": o.get("qty"),
                    "limit_price": o.get("limit_price"),
                    "testnet_client_order_id": o.get("testnet_client_order_id"),
                }
            )
    return cancelled


def _c_ex_abs_qty(symbol: str, profile_id: str = "") -> float:
    """Absolute exchange position qty for C (0 if unavailable)."""
    try:
        from . import testnet_hooks
        from common.testnet_bridge import get_adapter

        if not testnet_hooks.enabled():
            return 0.0
        strat = testnet_hooks.strategy_from_profile(profile_id or str(getattr(config, "PROFILE_ID", "") or ""))
        return abs(float(get_adapter(strat).position_amt(symbol)))
    except Exception:
        return 0.0


def _c_reserved_reduce_only_qty(pos: Position, *, exclude_levels: set[str] | None = None) -> float:
    """Sum qty of resting reduceOnly profit limits (P0_TP / R2_*), excluding given levels."""
    excl = exclude_levels or set()
    total = 0.0
    for o in pos.resting_tp_orders or []:
        if o.get("status") not in ("NEW", "PARTIAL"):
            continue
        lv = str(o.get("level") or "")
        if lv in excl:
            continue
        if lv == "P0_TP" or lv.startswith("R2_"):
            total += max(0.0, float(o.get("qty") or 0.0) - float(o.get("qty_filled") or 0.0))
    return total


def _cancel_c_pyramid_adds_exchange(
    st: AccountState,
    pos: Position,
    reason: str,
    *,
    profile_id: str = "",
    also_cancel_p0_tp: bool = False,
    also_cancel_r2: bool | None = None,
) -> list[dict[str, Any]]:
    """Cancel unfilled P1/P2. P0_TP/R2 only when flattening (stop/flat), NOT when only P0 TP fills."""
    pyr = dict(pos.pyramid or {})
    cancelled = exit_plans.cancel_pyramid_residual_adds(pyr, reason)
    pid = profile_id or str(getattr(config, "PROFILE_ID", "") or "")
    # Default: R2 cancelled only on full flatten paths (same as P0_TP cancel)
    if also_cancel_r2 is None:
        also_cancel_r2 = bool(also_cancel_p0_tp)
    try:
        from . import testnet_hooks

        tn_on = testnet_hooks.enabled()
    except Exception:
        tn_on = False
        testnet_hooks = None  # type: ignore
    if tn_on and testnet_hooks is not None:
        for a in cancelled:
            cid = a.get("testnet_client_order_id")
            if not cid:
                continue
            # Sync fill before cancel; never assume qty_filled=0 on poll failure
            try:
                polled = testnet_hooks.poll_order(
                    symbol=pos.symbol, client_order_id=str(cid), profile_id=pid
                )
            except Exception as exc:  # noqa: BLE001
                polled = {"error": str(exc)}
            if polled and polled.get("error"):
                a["cancel_lifecycle"] = "CANCEL_UNKNOWN"
                a["status"] = "CANCEL_UNKNOWN"
                a["cancel_detail"] = f"PRE_CANCEL_POLL_FAILED:{polled.get('error')}"
                # retain tracking — do not silently drop
                continue
            exec_q = float((polled or {}).get("executed_qty") or 0)
            prev_q = float(a.get("qty_filled") or a.get("filled_qty") or 0)
            if exec_q > prev_q + 1e-12:
                a["qty_filled"] = exec_q
                a["pending_fill_delta"] = exec_q - prev_q
                a["status"] = "PARTIAL_PENDING_APPLY"
            st_ex = str((polled or {}).get("status") or "")
            if st_ex in ("FILLED",) or (
                exec_q > 1e-12 and st_ex in ("CANCELED", "EXPIRED", "REJECTED")
            ):
                # leave for manage loop to apply fill; skip cancel if fully filled
                a["cancel_lifecycle"] = "FILLED" if st_ex == "FILLED" else "CANCELED"
                continue
            tn = testnet_hooks.cancel_cid(
                symbol=pos.symbol, client_order_id=str(cid), profile_id=pid
            )
            if not tn.get("ok") and not tn.get("skipped"):
                a["cancel_lifecycle"] = "CANCEL_UNKNOWN"
                a["status"] = "CANCEL_UNKNOWN"
                a["cancel_detail"] = str(tn.get("error") or "CANCEL_FAIL")
                continue
            # Post-cancel requery
            try:
                post = testnet_hooks.poll_order(
                    symbol=pos.symbol, client_order_id=str(cid), profile_id=pid
                )
            except Exception as exc:  # noqa: BLE001
                post = {"error": str(exc)}
            if post and post.get("error"):
                a["cancel_lifecycle"] = "CANCEL_REQUESTED"
                a["status"] = "CANCEL_REQUESTED"
                continue
            post_q = float((post or {}).get("executed_qty") or exec_q)
            if post_q > float(a.get("qty_filled") or 0) + 1e-12:
                a["qty_filled"] = post_q
                a["pending_fill_delta"] = post_q - prev_q
                a["status"] = "PARTIAL_PENDING_APPLY"
            elif float(a.get("pending_fill_delta") or 0) > 1e-12:
                # keep discovered fill evidence even if post status still working
                a["status"] = "PARTIAL_PENDING_APPLY"
            pst = str((post or {}).get("status") or "")
            if pst in ("CANCELED", "EXPIRED", "REJECTED", "FILLED"):
                a["cancel_lifecycle"] = "CANCELED" if pst != "FILLED" else "FILLED"
            else:
                a["cancel_lifecycle"] = "CANCEL_REQUESTED"
                # never overwrite fill-apply evidence with CANCEL_REQUESTED alone
                if str(a.get("status") or "") != "PARTIAL_PENDING_APPLY":
                    a["status"] = "CANCEL_REQUESTED"
        # cancelled rows are shallow copies — merge exchange status/fill evidence back to live pyramid
        by_key: dict[str, dict[str, Any]] = {}
        for a in cancelled:
            key = str(a.get("name") or a.get("testnet_client_order_id") or "")
            if key:
                by_key[key] = a
        for live in pyr.get("pending_adds") or []:
            key = str(live.get("name") or live.get("testnet_client_order_id") or "")
            src = by_key.get(key)
            if src:
                live.update(src)
        if also_cancel_p0_tp:
            pd = st.positions.get(pos.symbol) if st else None
            tp_cid = pd.get("testnet_p0_tp_client_order_id") if isinstance(pd, dict) else None
            for o in pos.resting_tp_orders or []:
                if o.get("level") == "P0_TP" and o.get("status") in ("NEW", "PARTIAL"):
                    if not tp_cid:
                        tp_cid = o.get("testnet_client_order_id")
                    o["status"] = "CANCELLED"
                    o["cancel_reason"] = reason
            if tp_cid:
                testnet_hooks.cancel_cid(
                    symbol=pos.symbol, client_order_id=str(tp_cid), profile_id=pid
                )
                if isinstance(pd, dict):
                    pd.pop("testnet_p0_tp_client_order_id", None)
        if also_cancel_r2:
            for o in pos.resting_tp_orders or []:
                lv = str(o.get("level") or "")
                if lv.startswith("R2_") and o.get("status") in ("NEW", "PARTIAL"):
                    cid = o.get("testnet_client_order_id")
                    if cid:
                        testnet_hooks.cancel_cid(
                            symbol=pos.symbol, client_order_id=str(cid), profile_id=pid
                        )
                    o["status"] = "CANCELLED"
                    o["cancel_reason"] = reason
    pos.pyramid = pyr
    if st is not None:
        _set_position_keep_testnet(st, pos)
        save_account(st)
    return cancelled


def _ensure_c_p0_tp_limit(
    st: AccountState,
    pos: Position,
    *,
    profile_id: str = "",
) -> dict[str, Any]:
    """Program rule: while P0 layer open → must have reduceOnly LIMIT at P0+1%.

    Caps qty by exchange position minus other reduceOnly (R2) so -2022 cannot starve P0 TP.
    """
    pyr = dict(pos.pyramid or {})
    levels = dict(pyr.get("levels") or {})
    p0_tp = float(levels.get("P0_TP") or pos.take_profit or 0.0)
    p0 = next((L for L in (pyr.get("layers") or []) if L.get("name") == "P0" and not L.get("closed")), None)
    if not p0 or p0_tp <= 0:
        return {"ok": True, "skipped": True, "reason": "no_p0"}
    pid = profile_id or str(getattr(config, "PROFILE_ID", "") or "")
    pd = st.positions.get(pos.symbol) or {}

    # Live check: stale local cid → clear and rehang
    existing = next(
        (
            o
            for o in (pos.resting_tp_orders or [])
            if o.get("level") == "P0_TP" and o.get("status") in ("NEW", "PARTIAL")
        ),
        None,
    )
    tp_cid = (existing or {}).get("testnet_client_order_id") or (
        pd.get("testnet_p0_tp_client_order_id") if isinstance(pd, dict) else None
    )
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled() and tp_cid:
            polled = testnet_hooks.poll_order(
                symbol=pos.symbol, client_order_id=str(tp_cid), profile_id=pid
            )
            st_ex = str((polled or {}).get("status") or "")
            if st_ex in ("NEW", "PARTIALLY_FILLED"):
                return {"ok": True, "reused": True, "client_order_id": tp_cid}
            # FILLED handled by manage poll; CANCELED/missing → rehang
            if existing:
                existing["status"] = "CANCELLED"
                existing["cancel_reason"] = f"STALE_{st_ex or 'GONE'}"
            if isinstance(pd, dict):
                pd.pop("testnet_p0_tp_client_order_id", None)
            tp_cid = None
    except Exception:
        pass

    if tp_cid:
        return {"ok": True, "reused": True, "client_order_id": tp_cid}

    qty = float(p0.get("qty") or 0.0)
    qty = min(qty, float(pos.qty_remaining))
    ex = _c_ex_abs_qty(pos.symbol, pid)
    if ex > 0:
        reserved = _c_reserved_reduce_only_qty(pos, exclude_levels={"P0_TP"})
        avail = max(0.0, ex - reserved)
        qty = min(qty, avail)
    if qty <= 0:
        return {"ok": False, "error": "no_available_qty_for_p0_tp", "ex": ex}
    # align
    try:
        from . import testnet_hooks
        from common.testnet_bridge import get_adapter

        if testnet_hooks.enabled():
            strat = testnet_hooks.strategy_from_profile(pid)
            qty = float(get_adapter(strat).align_qty(pos.symbol, qty))
    except Exception:
        pass
    if qty <= 0:
        return {"ok": False, "error": "aligned_zero"}

    order = {
        "order_id": f"{pos.trade_id}:P0_TP:{uuid.uuid4().hex[:8]}",
        "level": "P0_TP",
        "side": "SELL" if pos.side == "LONG" else "BUY",
        "reduce_only": True,
        "limit_price": p0_tp,
        "qty": qty,
        "qty_filled": 0.0,
        "status": "NEW",
        "placed_at": now_iso(),
    }
    out: dict[str, Any] = {"ok": True, "skipped": True}
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            tn = testnet_hooks.place_tp_limit(
                symbol=pos.symbol,
                position_side=str(pos.side),
                qty=float(qty),
                price=float(p0_tp),
                level="P0_TP",
                profile_id=pid,
            )
            out = {
                "ok": bool(tn.get("ok")),
                "skipped": bool(tn.get("skipped")),
                "client_order_id": tn.get("client_order_id"),
                "error": tn.get("error"),
                "price": p0_tp,
                "qty": qty,
            }
            if tn.get("ok") and not tn.get("skipped"):
                order["testnet_client_order_id"] = tn.get("client_order_id")
                if isinstance(pd, dict):
                    pd["testnet_p0_tp_client_order_id"] = tn.get("client_order_id")
                    pd["testnet_p0_tp_price"] = p0_tp
                print(
                    f"[testnet-C] P0_TP_PLACED {pos.symbol} px={p0_tp} qty={qty} "
                    f"cid={tn.get('client_order_id')}",
                    flush=True,
                )
            else:
                order["testnet_place_error"] = tn.get("error")
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc)}
        order["testnet_place_error"] = str(exc)
    pos.resting_tp_orders = [
        o
        for o in (pos.resting_tp_orders or [])
        if not (o.get("level") == "P0_TP" and o.get("status") in ("NEW", "PARTIAL"))
    ] + [order]
    pos.pyramid = pyr
    _set_position_keep_testnet(st, pos)
    save_account(st)
    return out


def _place_c_r2_tp_for_layer(
    st: AccountState,
    pos: Position,
    *,
    layer_name: str,
    qty: float,
    profile_id: str = "",
) -> dict[str, Any]:
    """After P1/P2 exchange fill: hang reduceOnly LIMIT at P0 entry (R2). Cap by free reduceOnly room."""
    pyr = dict(pos.pyramid or {})
    rebound = float(pyr.get("rebound_exit_price") or pos.entry_price or 0.0)
    if rebound <= 0 or qty <= 0 or layer_name in ("", "P0"):
        return {"ok": False, "error": "bad_r2_args"}
    level = f"R2_{layer_name}"  # R2_P1 / R2_P2
    pid = profile_id or str(getattr(config, "PROFILE_ID", "") or "")
    for o in pos.resting_tp_orders or []:
        if o.get("level") == level and o.get("status") in ("NEW", "PARTIAL") and o.get("testnet_client_order_id"):
            return {"ok": True, "reused": True, "client_order_id": o.get("testnet_client_order_id")}

    # Never oversubscribe: leave room for P0_TP if P0 still open
    p0_open = any(L.get("name") == "P0" and not L.get("closed") for L in (pyr.get("layers") or []))
    p0_need = 0.0
    if p0_open:
        p0 = next(L for L in (pyr.get("layers") or []) if L.get("name") == "P0")
        p0_need = float(p0.get("qty") or 0.0)
    ex = _c_ex_abs_qty(pos.symbol, pid)
    qty_use = float(qty)
    if ex > 0:
        reserved = _c_reserved_reduce_only_qty(pos, exclude_levels={level})
        # reserve p0_need for P0_TP when placing R2
        room = max(0.0, ex - reserved - (p0_need if p0_open else 0.0))
        qty_use = min(qty_use, room, ex)
    try:
        from . import testnet_hooks
        from common.testnet_bridge import get_adapter

        if testnet_hooks.enabled():
            strat = testnet_hooks.strategy_from_profile(pid)
            qty_use = float(get_adapter(strat).align_qty(pos.symbol, qty_use))
    except Exception:
        pass
    if qty_use <= 0:
        # Prefer keeping P0_TP; skip R2 rather than steal P0 room
        return {"ok": False, "error": "no_room_for_r2_keep_p0_tp", "ex": ex, "p0_need": p0_need}

    order = {
        "order_id": f"{pos.trade_id}:{level}:{uuid.uuid4().hex[:8]}",
        "level": level,
        "layer": layer_name,
        "side": "SELL" if pos.side == "LONG" else "BUY",
        "reduce_only": True,
        "limit_price": rebound,
        "qty": float(qty_use),
        "qty_filled": 0.0,
        "status": "NEW",
        "placed_at": now_iso(),
    }
    out: dict[str, Any] = {"ok": True, "skipped": True, "level": level, "price": rebound, "qty": qty_use}
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            tn = testnet_hooks.place_tp_limit(
                symbol=pos.symbol,
                position_side=str(pos.side),
                qty=float(qty_use),
                price=float(rebound),
                level=level,
                profile_id=pid,
            )
            out = {
                "ok": bool(tn.get("ok")),
                "skipped": bool(tn.get("skipped")),
                "client_order_id": tn.get("client_order_id"),
                "error": tn.get("error"),
                "level": level,
                "price": rebound,
                "qty": qty_use,
            }
            if tn.get("ok") and not tn.get("skipped"):
                order["testnet_client_order_id"] = tn.get("client_order_id")
                order["testnet_exchange_order_id"] = tn.get("exchange_order_id")
                for L in pyr.get("layers") or []:
                    if L.get("name") == layer_name:
                        L["testnet_r2_tp_client_order_id"] = tn.get("client_order_id")
                        L["testnet_r2_tp_price"] = rebound
            else:
                order["testnet_place_error"] = tn.get("error")
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc), "level": level}
        order["testnet_place_error"] = str(exc)
    pos.resting_tp_orders = [
        o for o in (pos.resting_tp_orders or []) if not (o.get("level") == level and o.get("status") in ("NEW", "PARTIAL"))
    ] + [order]
    pos.pyramid = pyr
    _set_position_keep_testnet(st, pos)
    save_account(st)
    # After R2 attempt, always re-assert P0+1% TP (program invariant)
    try:
        _ensure_c_p0_tp_limit(st, pos, profile_id=pid)
    except Exception:
        pass
    if out.get("ok") and not out.get("skipped"):
        print(
            f"[testnet-C] R2_TP_PLACED {pos.symbol} {level} px={rebound} qty={qty_use} "
            f"cid={out.get('client_order_id')}",
            flush=True,
        )
    return out


def _preplace_c_pyramid_ladder(
    st: AccountState,
    pos: Position,
    *,
    profile_id: str = "",
) -> dict[str, Any]:
    """C ladder program (all symbols):

    P0 fill → hang P1+P2 open LIMIT + P0_TP(+1%) reduceOnly
              + STOP_MARKET closePosition at theoretical Pstop (from P0).
    Why SL at P0: crash fills P1/P2 limits then blows through Pstop — exchange must
    already hold closePosition SL to flatten the WHOLE stack immediately.
    P1/P2 fill → hang R2 reduceOnly at P0 price.
    P0_TP fill → cancel unfilled P1/P2 only (keep other layers' R2).
    Pstop/flat → cancel adds + P0_TP + all R2.
    """
    pyr = dict(pos.pyramid or {})
    levels = dict(pyr.get("levels") or {})
    pid = profile_id or str(getattr(config, "PROFILE_ID", "") or "")
    notional = float(pyr.get("notional_per") or config.NOTIONAL_USDT)
    step = float(pyr.get("step") or 0.001)
    out: dict[str, Any] = {"adds": [], "p0_tp": None}

    try:
        from . import testnet_hooks

        tn_on = testnet_hooks.enabled()
    except Exception:
        tn_on = False
        testnet_hooks = None  # type: ignore

    # P1 / P2 — both PENDING, hang immediately
    for a in list(pyr.get("pending_adds") or []):
        if a.get("status") not in ("PENDING", "WAITING_P1"):
            continue
        name = str(a.get("name") or "")
        lim = float(a.get("limit_price") or 0.0)
        if lim <= 0 or name not in ("P1", "P2"):
            continue
        a["status"] = "PENDING"  # promote WAITING_P1 → hung now
        qty = round_step(notional / lim, step)
        a["planned_qty"] = qty
        if tn_on and testnet_hooks is not None and qty > 0:
            tn = testnet_hooks.place_pyramid_add_limit(
                symbol=pos.symbol,
                position_side=str(pos.side),
                qty=float(qty),
                price=float(lim),
                layer=name,
                profile_id=pid,
            )
            if tn.get("ok") and not tn.get("skipped"):
                a["status"] = "ORDER_RESTING"
                a["testnet_client_order_id"] = tn.get("client_order_id")
                a["testnet_exchange_order_id"] = tn.get("exchange_order_id")
            else:
                a["testnet_place_error"] = tn.get("error")
            out["adds"].append({"layer": name, **{k: tn.get(k) for k in ("ok", "client_order_id", "error")}})
        else:
            out["adds"].append({"layer": name, "ok": True, "skipped": True, "qty": qty, "price": lim})

    # P0_TP reduceOnly LIMIT (P0 qty only)
    p0_tp = float(levels.get("P0_TP") or pos.take_profit or 0.0)
    p0_qty = 0.0
    for L in pyr.get("layers") or []:
        if L.get("name") == "P0" and not L.get("closed"):
            p0_qty = float(L.get("qty") or 0.0)
            break
    if p0_qty <= 0:
        p0_qty = float(pos.qty_remaining)
    tp_order = None
    if p0_tp > 0 and p0_qty > 0:
        tp_order = {
            "order_id": f"{pos.trade_id}:P0_TP:{uuid.uuid4().hex[:8]}",
            "level": "P0_TP",
            "side": "SELL" if pos.side == "LONG" else "BUY",
            "reduce_only": True,
            "limit_price": p0_tp,
            "qty": p0_qty,
            "qty_filled": 0.0,
            "status": "NEW",
            "placed_at": now_iso(),
        }
        if tn_on and testnet_hooks is not None:
            tn = testnet_hooks.place_tp_limit(
                symbol=pos.symbol,
                position_side=str(pos.side),
                qty=float(p0_qty),
                price=float(p0_tp),
                level="P0_TP",
                profile_id=pid,
            )
            out["p0_tp"] = {"ok": tn.get("ok"), "client_order_id": tn.get("client_order_id"), "error": tn.get("error")}
            if tn.get("ok") and not tn.get("skipped"):
                tp_order["testnet_client_order_id"] = tn.get("client_order_id")
                tp_order["testnet_exchange_order_id"] = tn.get("exchange_order_id")
                pd = st.positions.get(pos.symbol)
                if isinstance(pd, dict):
                    pd["testnet_p0_tp_client_order_id"] = tn.get("client_order_id")
                    pd["testnet_p0_tp_price"] = p0_tp
            else:
                tp_order["testnet_place_error"] = tn.get("error")
        pos.resting_tp_orders = list(pos.resting_tp_orders or []) + [tp_order]

    pos.pyramid = pyr
    _set_position_keep_testnet(st, pos)
    save_account(st)
    _append_fill(
        {
            "event": "C_PYRAMID_LADDER_PREPLACED",
            "trade_id": pos.trade_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "adds": out["adds"],
            "p0_tp": out["p0_tp"],
            "p0_tp_order": tp_order,
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "run_id": st.run_id,
            "note": "P0 filled → hang P1+P2 open LIMIT + P0_TP reduceOnly; cancel adds on TP/flat",
        }
    )
    return out


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
    if getattr(st, "ledger_write_failed", False) or getattr(st, "new_exposure_blocked_until_reconcile", False):
        reasons.append("LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE")
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
    bid = float(signal.get("bid") or 0.0)
    ask = float(signal.get("ask") or 0.0)
    if ref <= 0:
        if bid > 0 and ask > 0:
            ref = (bid + ask) / 2.0
    maker_exec = bool(getattr(config, "MAKER_EXECUTION_ENABLED", False))
    if maker_exec:
        from . import c_maker_exec_model as _mm

        fetched_at = signal.get("book_fetched_at") or signal.get("fetched_at")
        _qage = getattr(config, "MAKER_QUOTE_MAX_AGE_SEC", None)
        if _qage is None or _qage == "" or _qage == "UNCONFIRMED":
            out["block_reason"] = "MAKER_QUOTE_MAX_AGE_UNCONFIRMED"
            out["block_reasons"] = ["MAKER_QUOTE_MAX_AGE_UNCONFIRMED"]
            out["final_action"] = "ORDER_SKIP_NO_QUOTE"
            return out
        ok_q, qreason = _mm.quote_usable(
            bid=bid,
            ask=ask,
            fetched_at=float(fetched_at) if fetched_at is not None else None,
            now_ts=now_ts(),
            max_age_sec=float(_qage),
        )
        if not ok_q:
            out["block_reason"] = qreason
            out["block_reasons"] = [qreason]
            out["final_action"] = "ORDER_SKIP_NO_QUOTE"
            _append_fill(
                {
                    "event": "ORDER_SKIP_NO_QUOTE",
                    "symbol": symbol,
                    "side": side,
                    "reason": qreason,
                    "bid": bid,
                    "ask": ask,
                    "simulation_assumption": True,
                    "maker_exec": True,
                }
            )
            return out
        limit_px = _mm.maker_entry_limit_px(side=side, bid=bid, ask=ask) or 0.0
        if limit_px <= 0:
            out["block_reason"] = "INVALID_LIMIT_PRICE"
            out["block_reasons"] = ["INVALID_LIMIT_PRICE"]
            return out
        if _mm.post_only_would_take(side=side, limit_px=limit_px, bid=bid, ask=ask):
            out["block_reason"] = "POST_ONLY_WOULD_TAKE"
            out["block_reasons"] = ["POST_ONLY_WOULD_TAKE"]
            out["final_action"] = "POST_ONLY_REJECT"
            _append_fill(
                {
                    "event": "POST_ONLY_REJECT",
                    "symbol": symbol,
                    "side": side,
                    "price": limit_px,
                    "bid": bid,
                    "ask": ask,
                    "simulation_assumption": True,
                    "maker_exec": True,
                    "note": "no_auto_taker_fallback",
                    "liquidity_role_assumption": "maker",
                    "fill_assumption": _mm.FILL_ASSUMPTION_MAKER,
                }
            )
            return out
    else:
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
    # TTL from actual queue/submit time (queued_ts), not signal bar close. 0 = disabled.
    _ttl_raw = getattr(config, "LIMIT_ENTRY_TTL_SEC", 0.0)
    ttl_sec = float(_ttl_raw) if _ttl_raw is not None and _ttl_raw != "" else 0.0
    expire_ts = (now + ttl_sec) if ttl_sec > 0 else 0.0
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
        # expire_ts=0 → TTL off; else wall-clock cancel of unfilled remainder only
        "expire_ts": expire_ts,
        "ttl_sec": ttl_sec,
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
    # Testnet: place real order + symbol lock (skip if disabled)
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            tn = testnet_hooks.place_entry_for_pending(
                pending, qty=qty, profile_id=str(pending.get("profile_id") or "")
            )
            if not tn.get("ok"):
                err = str(tn.get("error") or "TESTNET_PLACE_FAILED")
                out["block_reason"] = err
                out["block_reasons"] = [err]
                out["final_action"] = "BLOCKED"
                if "SYMBOL_LOCKED_BY_OTHER_STRATEGY" in err:
                    print(f"[testnet] SYMBOL_LOCKED_BY_OTHER_STRATEGY {symbol}: {err}", flush=True)
                return out
            pending["testnet_client_order_id"] = tn.get("client_order_id")
            pending["testnet_exchange_order_id"] = tn.get("exchange_order_id")
            pending["testnet_strategy"] = tn.get("strategy") or testnet_hooks.strategy_from_profile(
                str(pending.get("profile_id") or "")
            )
    except Exception as exc:  # noqa: BLE001
        out["block_reason"] = f"TESTNET_HOOK_ERROR:{exc}"
        out["block_reasons"] = [out["block_reason"]]
        return out

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
            "ttl_sec": ttl_sec,
            "expire_ts": expire_ts,
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
            "ttl_sec": ttl_sec,
            "expire_ts": expire_ts,
            "qty": qty,
        }
    )
    return out


def _pending_expire_ts(pend: dict[str, Any]) -> float:
    """Wall-clock expire from submit (queued_ts); 0 means TTL disabled."""
    expire_ts = float(pend.get("expire_ts") or 0.0)
    if expire_ts > 0:
        return expire_ts
    ttl_sec = float(pend.get("ttl_sec") or 0.0)
    if ttl_sec <= 0:
        return 0.0
    queued_ts = float(pend.get("queued_ts") or 0.0)
    if queued_ts <= 0:
        return 0.0
    return queued_ts + ttl_sec


def _expire_pending_remainder(
    st: AccountState,
    *,
    sym: str,
    pend: dict[str, Any],
    expire_reason: str,
    mid: float | None = None,
    move_pct: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cancel unfilled pending only — never flatten an already-open position."""
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            # cancel entry remainder on exchange; do not cancel SL / close position
            cid = pend.get("testnet_client_order_id")
            if cid:
                testnet_hooks.cancel_cid(
                    symbol=sym,
                    client_order_id=str(cid),
                    profile_id=str(pend.get("profile_id") or ""),
                )
            else:
                testnet_hooks.cancel_pending_exchange(pend)
    except Exception as exc:  # noqa: BLE001
        print(f"[limit] expire cancel failed {sym}: {exc}", flush=True)
    st.pending_limit_entries.pop(sym, None)
    row = {
        "event": "LIMIT_EXPIRED",
        "expire_reason": expire_reason,
        "symbol": sym,
        "side": pend.get("side"),
        "price": pend.get("limit_price"),
        "signal_ref_price": pend.get("signal_ref_price"),
        "mark_mid": mid,
        "adverse_move_pct": move_pct,
        "cancel_adverse_pct": pend.get("cancel_adverse_pct"),
        "limit_offset_pct": pend.get("limit_offset_pct"),
        "queued_at": pend.get("queued_at"),
        "queued_ts": pend.get("queued_ts"),
        "ttl_sec": pend.get("ttl_sec"),
        "expire_ts": pend.get("expire_ts"),
        "profile_id": pend.get("profile_id"),
        "run_id": st.run_id,
        "signal_key": pend.get("sig_key"),
        "simulation_assumption": True,
        # evidence: any already-open position is untouched
        "open_position_untouched": sym in (st.positions or {}),
        "filled_qty_retained_note": "TTL/adverse cancels unfilled remainder only",
    }
    if extra:
        row.update(extra)
    _append_fill(row)
    save_account(st)
    return {
        "symbol": sym,
        "action": "EXPIRED",
        "ok": False,
        "expire_reason": expire_reason,
        "adverse_move_pct": move_pct,
        "mark_mid": mid,
        "signal_ref_price": pend.get("signal_ref_price"),
        "open_position_untouched": row["open_position_untouched"],
    }


def process_pending_limit_entries(
    st: AccountState,
    books: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """輪詢待成交限價：觸價則 try_open；TTL 或不利偏離任一先到則取消未成交餘量。"""
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

        # Maker-exec：完整走訂單狀態機（非觸價直接成交）；委派 bridge
        maker_exec = bool(getattr(config, "MAKER_EXECUTION_ENABLED", False))
        if maker_exec:
            from . import c_maker_exec_bridge as _bridge

            results.extend(
                _bridge.process_maker_pending_entry(
                    st, symbol=sym, pend=pend, book=book
                )
            )
            continue

        # Testnet: prefer exchange fill over local touch simulation
        try:
            from . import testnet_hooks

            if testnet_hooks.enabled() and pend.get("testnet_client_order_id"):
                fill = testnet_hooks.poll_fill(pend)
                if fill and fill.get("error"):
                    results.append({"symbol": sym, "action": "TESTNET_QUERY_ERR", "error": fill["error"]})
                    continue
                if fill and fill.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
                    exec_q = float(fill.get("qty") or fill.get("executed_qty") or 0)
                    if exec_q > 1e-12:
                        touched = True
                        limit_px = float(fill.get("price") or limit_px)
                        pend["testnet_fill_qty"] = exec_q
                        pend["binance_executed_qty"] = exec_q
                    else:
                        st.pending_limit_entries.pop(sym, None)
                        save_account(st)
                        results.append({"symbol": sym, "action": "EXPIRED", "expire_reason": fill.get("status")})
                        continue
                elif fill and float(fill.get("qty") or 0) > 0:
                    touched = True
                    limit_px = float(fill.get("price") or limit_px)
                    pend["testnet_fill_qty"] = float(fill["qty"])
                    pend["binance_entry_status"] = str(fill.get("status") or "")
                    if fill.get("exchange_order_id") is not None:
                        pend["testnet_exchange_order_id"] = fill.get("exchange_order_id")
                    if fill.get("price") is not None:
                        pend["binance_avg_price"] = float(fill.get("price") or 0) or limit_px
                    pend["binance_executed_qty"] = float(fill.get("qty") or 0)
                    pend["binance_order_id"] = fill.get("exchange_order_id") or pend.get("binance_order_id")
                else:
                    # still working on exchange — skip local touch fill; TTL or adverse may cancel remainder
                    expire_ts = _pending_expire_ts(pend)
                    now_wall = now_ts()
                    if expire_ts > 0 and now_wall >= expire_ts:
                        results.append(
                            _expire_pending_remainder(
                                st,
                                sym=sym,
                                pend=pend,
                                expire_reason="TTL",
                                mid=mid,
                                extra={"expired_at_ts": now_wall, "expire_ts": expire_ts},
                            )
                        )
                        continue
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
                        from common.execution_sync import classify_poll, run_entry_cancel

                        recorded = float(pend.get("testnet_fill_qty") or 0)
                        intended = float(pend.get("qty") or pend.get("qty_target") or recorded or 0)

                        def _poll():
                            raw = testnet_hooks.poll_fill(pend)
                            if raw is None:
                                # still working — synthesize NEW with known recorded qty
                                return {
                                    "status": "NEW",
                                    "executed_qty": recorded,
                                    "avg_price": limit_px,
                                }
                            if raw.get("error"):
                                return raw
                            return {
                                "status": raw.get("status"),
                                "executed_qty": float(raw.get("qty") or raw.get("executed_qty") or 0),
                                "avg_price": float(raw.get("price") or 0) or limit_px,
                                "raw": raw.get("raw"),
                            }

                        def _cancel():
                            try:
                                return testnet_hooks.cancel_pending_exchange(pend)
                            except Exception as exc:  # noqa: BLE001
                                return {"ok": False, "error": str(exc)}

                        decision = run_entry_cancel(
                            poll_fn=_poll,
                            cancel_fn=_cancel,
                            recorded_qty=recorded,
                            intended_qty=intended or 1.0,
                            apply_fill_fn=None,
                        )
                        obs = decision.get("executed_qty")
                        if obs is not None and float(obs) > recorded + 1e-12:
                            pend["testnet_fill_qty"] = float(obs)
                            pend["binance_executed_qty"] = float(obs)
                            touched = True
                            # keep pending for open path this tick
                            results.append({"symbol": sym, "action": "FILL_ON_CANCEL_SYNC", "qty": obs})
                            # do not drop; fall through by setting fill and breaking cancel-only path
                        elif not decision.get("allow_drop_pending"):
                            pend["cancel_lifecycle"] = decision.get("lifecycle")
                            _append_fill(
                                {
                                    "event": str(decision.get("lifecycle") or "CANCEL_UNKNOWN"),
                                    "expire_reason": "ADVERSE_MOVE",
                                    "symbol": sym,
                                    "side": side,
                                    "price": limit_px,
                                    "signal_ref_price": ref,
                                    "mark_mid": mid,
                                    "adverse_move_pct": move_pct,
                                    "cancel_adverse_pct": adverse_pct,
                                    "profile_id": pend.get("profile_id"),
                                    "run_id": st.run_id,
                                    "signal_key": pend.get("sig_key"),
                                    "testnet": True,
                                    "testnet_client_order_id": pend.get("testnet_client_order_id"),
                                    "detail": decision.get("reason"),
                                    "executed_qty_observed": decision.get("executed_qty"),
                                }
                            )
                            save_account(st)
                            results.append(
                                {
                                    "symbol": sym,
                                    "action": decision.get("lifecycle") or "CANCEL_UNKNOWN",
                                    "pending_retained": True,
                                }
                            )
                            continue
                        else:
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
                                    "profile_id": pend.get("profile_id"),
                                    "run_id": st.run_id,
                                    "signal_key": pend.get("sig_key"),
                                    "testnet": True,
                                    "testnet_client_order_id": pend.get("testnet_client_order_id"),
                                    "lifecycle": "CANCELED",
                                    "binance_executed_qty": decision.get("executed_qty") or 0,
                                }
                            )
                            save_account(st)
                            results.append({"symbol": sym, "action": "EXPIRED", "expire_reason": "ADVERSE_MOVE"})
                            continue
                        if float(pend.get("testnet_fill_qty") or 0) <= 1e-12:
                            continue
                    else:
                        continue
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet] poll_fill error {sym}: {exc}", flush=True)

        touched = (side == "LONG" and ask <= limit_px) or (side == "SHORT" and bid >= limit_px)
        # When testnet order is live, do not paper-fill on local touch
        try:
            from . import testnet_hooks as _th

            if _th.enabled() and pend.get("testnet_client_order_id") and not pend.get("testnet_fill_qty"):
                touched = False
            if _th.enabled() and float(pend.get("testnet_fill_qty") or 0) > 1e-12:
                touched = True  # exchange-confirmed fill drives open even if book not "touching"
            if _th.enabled() and not pend.get("testnet_client_order_id") and not pend.get("testnet_fill_qty"):
                # Testnet mode without cid: never fall back to paper touch OPEN
                touched = False
        except Exception:
            pass
        if not touched:
            # 未觸價：TTL（自掛單提交 queued_ts）或不利撤單 — 任一先到只撤未成交餘量
            expire_ts = _pending_expire_ts(pend)
            now_wall = now_ts()
            if expire_ts > 0 and now_wall >= expire_ts:
                results.append(
                    _expire_pending_remainder(
                        st,
                        sym=sym,
                        pend=pend,
                        expire_reason="TTL",
                        mid=mid,
                        extra={"expired_at_ts": now_wall, "expire_ts": expire_ts},
                    )
                )
                continue
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
                results.append(
                    _expire_pending_remainder(
                        st,
                        sym=sym,
                        pend=pend,
                        expire_reason="ADVERSE_MOVE",
                        mid=mid,
                        move_pct=move_pct,
                    )
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
        # preserve testnet SL ids before popping pending (attached at entry place time)
        for _k in (
            "testnet_sl_client_order_id",
            "testnet_sl_exchange_order_id",
            "testnet_sl_stop_price",
            "testnet_sl_pct",
            "testnet_client_order_id",
            "testnet_exchange_order_id",
            "testnet_strategy",
            "profile_id",
            "binance_order_id",
            "binance_avg_price",
            "binance_executed_qty",
            "binance_entry_status",
        ):
            if pend.get(_k) is not None:
                sig[_k] = pend.get(_k)
        if pend.get("testnet_fill_qty") and not sig.get("binance_executed_qty"):
            sig["binance_executed_qty"] = float(pend.get("testnet_fill_qty") or 0)
        if not sig.get("binance_avg_price"):
            sig["binance_avg_price"] = float(limit_px)
        if not sig.get("binance_order_id") and pend.get("testnet_exchange_order_id"):
            sig["binance_order_id"] = pend.get("testnet_exchange_order_id")
        entry_status = str(pend.get("binance_entry_status") or sig.get("binance_entry_status") or "")
        entry_cid = str(pend.get("testnet_client_order_id") or sig.get("testnet_client_order_id") or "")
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
            # Partial fill accepted as position size: cancel leftover entry LIMIT only
            # (do NOT cancel SL via cancel_pending_exchange). A keeps remainder+add;
            # B cancels remainder until incremental-add is ported.
            if entry_status == "PARTIALLY_FILLED" and entry_cid:
                try:
                    from . import testnet_hooks as _th

                    if _th.enabled():
                        crem = _th.cancel_cid(
                            symbol=sym,
                            client_order_id=entry_cid,
                            profile_id=str(pend.get("profile_id") or ""),
                        )
                        _append_fill(
                            {
                                "event": "ENTRY_REMAINDER_CANCELLED",
                                "symbol": sym,
                                "side": side,
                                "client_order_id": entry_cid,
                                "ok": crem.get("ok"),
                                "error": crem.get("error"),
                                "binance_executed_qty": sig.get("binance_executed_qty"),
                                "profile_id": pend.get("profile_id"),
                                "run_id": st.run_id,
                                "trade_id": opened.get("trade_id"),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"[testnet] ENTRY_REMAINDER_CANCEL_ERROR {sym}: {exc}", flush=True)
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
                    "testnet_client_order_id": pend.get("testnet_client_order_id") or sig.get("testnet_client_order_id"),
                    "binance_order_id": sig.get("binance_order_id") or pend.get("binance_order_id"),
                    "binance_avg_price": sig.get("binance_avg_price") or limit_px,
                    "binance_executed_qty": sig.get("binance_executed_qty") or pend.get("testnet_fill_qty"),
                    "binance_entry_status": entry_status or None,
                    "binance_partial_entry": bool(sig.get("binance_partial_entry")),
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
                    "partial_entry": bool(sig.get("binance_partial_entry")),
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

    entry = float(signal.get("binance_avg_price") or signal["est_fill_price"])
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
    # Testnet: never invent full planned size when exchange only partially filled
    exec_q = float(signal.get("binance_executed_qty") or 0)
    if exec_q > 1e-12:
        exec_aligned = round_step(exec_q, step)
        if exec_aligned > 1e-12 and exec_aligned + 1e-12 < qty:
            scale = exec_aligned / qty if qty > 0 else 1.0
            qty = exec_aligned
            notional_u = float(notional_u) * scale
            margin_u = float(margin_u) * scale
            signal = dict(signal)
            signal["binance_partial_entry"] = True
            signal["notional_u"] = notional_u
            signal["margin_u"] = margin_u
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

    # Limit entry fill = maker; any non-limit open path = taker
    _entry_role = "maker" if signal.get("limit_entry_filled") else "taker"
    fee = _fee(entry * qty, role=_entry_role)
    trade_id = new_trade_id(st.run_id, symbol)
    # Testnet SL on OPEN (B + C): STOP_MARKET closePosition.
    # C: trigger = theoretical Pstop from P0. Crash through P1/P2 fills adds first;
    #    when mark hits Pstop, exchange flattens WHOLE stack immediately — do NOT wait for P2.
    sl_meta: dict[str, Any] = {}
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            pend_sl = None
            if signal.get("testnet_sl_client_order_id"):
                pend_sl = {
                    "testnet_sl_client_order_id": signal.get("testnet_sl_client_order_id"),
                    "testnet_sl_exchange_order_id": signal.get("testnet_sl_exchange_order_id"),
                    "testnet_sl_stop_price": signal.get("testnet_sl_stop_price"),
                }
            else:
                try:
                    pend_sl = (getattr(st, "pending_limit_entries", None) or {}).get(symbol)
                except Exception:
                    pend_sl = None
            sl_meta = testnet_hooks.ensure_stop_on_open(
                symbol=symbol,
                side=side,
                stop_price=float(stop),
                qty=float(qty),
                profile_id=str(
                    signal.get("profile_id") or getattr(config, "PROFILE_ID", "") or ""
                ),
                pending=pend_sl if isinstance(pend_sl, dict) else None,
            )
    except Exception as exc:  # noqa: BLE001
        sl_meta = {"ok": False, "error": str(exc)}
    sl_place_failed = bool(sl_meta) and (not sl_meta.get("ok")) and (not sl_meta.get("skipped"))
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
    pd0 = st.positions.get(symbol)
    if isinstance(pd0, dict):
        pd0["entry_fee_estimated"] = float(fee)
        pd0["entry_fee_status"] = "estimated_schedule_rate"
        pd0["entry_fee_confirmed"] = False
        if signal.get("testnet_client_order_id"):
            pd0["testnet_entry_client_order_id"] = signal.get("testnet_client_order_id")
            pd0["testnet_entry_exchange_order_id"] = signal.get("testnet_exchange_order_id")
    if sl_meta.get("client_order_id"):
        # stash exchange SL ids on stored position dict
        pd = st.positions.get(symbol)
        if isinstance(pd, dict):
            pd["testnet_sl_client_order_id"] = sl_meta.get("client_order_id")
            pd["testnet_sl_exchange_order_id"] = sl_meta.get("exchange_order_id")
            pd["testnet_sl_stop_price"] = sl_meta.get("stop_price") or stop
        elif hasattr(pos, "__dict__"):
            pass
    if sl_place_failed:
        pd = st.positions.get(symbol)
        if isinstance(pd, dict):
            pd["testnet_sl_place_failed"] = True
    # Testnet B: pre-place TP1(70%)+TP2(30%) reduceOnly LIMIT — NOT full TAKE_PROFIT_MARKET
    if plan == exit_plans.B_PARTIAL70 and take_profit is not None and take_profit_2 is not None:
        try:
            _preplace_b_tp_orders(
                st,
                pos,
                profile_id=str(
                    signal.get("profile_id") or getattr(config, "PROFILE_ID", "") or ""
                ),
            )
            pos = get_position(st, symbol) or pos
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet] B_TP_PREPLACE_ERROR {symbol}: {exc}", flush=True)
    elif plan == exit_plans.C_PYRAMID:
        # C: P0 filled → hang P1+P2 open LIMIT + P0_TP reduceOnly; cancel adds on TP
        try:
            _preplace_c_pyramid_ladder(
                st,
                pos,
                profile_id=str(
                    signal.get("profile_id") or getattr(config, "PROFILE_ID", "") or ""
                ),
            )
            pos = get_position(st, symbol) or pos
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet] C_LADDER_PREPLACE_ERROR {symbol}: {exc}", flush=True)
    elif take_profit is not None:
        # legacy: single algo TAKE_PROFIT_MARKET (full close)
        try:
            from . import testnet_hooks
            from common.testnet_bridge import try_place_take_profit

            if testnet_hooks.enabled():
                tp_res = try_place_take_profit(
                    strategy=testnet_hooks.strategy_from_profile(
                        str(getattr(config, "PROFILE_ID", "") or "")
                    ),
                    symbol=symbol,
                    side=side,
                    trigger_price=float(take_profit),
                    quantity=float(qty),
                    close_position=True,
                )
                pd = st.positions.get(symbol)
                if isinstance(pd, dict) and tp_res.ok:
                    pd["testnet_tp_client_order_id"] = tp_res.client_order_id
                    pd["testnet_tp_trigger"] = (tp_res.raw or {}).get("triggerPrice") or take_profit
                    print(
                        f"[testnet] TP_ATTACHED {symbol} trigger={pd['testnet_tp_trigger']} "
                        f"cid={tp_res.client_order_id}",
                        flush=True,
                    )
                elif not tp_res.ok:
                    print(f"[testnet] TP_ATTACH_FAIL {symbol}: {tp_res.error}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet] TP_ATTACH_ERROR {symbol}: {exc}", flush=True)
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
            "fee_rate_one_side": _fee_rate_for_role(_entry_role),
            "liquidity_role_assumption": _entry_role,
            "slippage_adverse": config.SLIPPAGE_ADVERSE,
            "simulation_assumption": True,
            "funding_data_gap": True,
            # funding_paid_u left unset here; account funding remains gap/unknown
            "testnet_client_order_id": signal.get("testnet_client_order_id"),
            "binance_order_id": signal.get("binance_order_id") or signal.get("testnet_exchange_order_id"),
            "binance_avg_price": float(signal.get("binance_avg_price") or entry),
            "binance_executed_qty": float(signal.get("binance_executed_qty") or qty),
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
    exchange_already_done: bool = False,
) -> dict[str, Any]:
    from common.execution_sync import (
        accounting_from_exchange_or_estimate,
        build_pending_close_state,
        clear_pending_close_fields,
        estimated_pnl_fields,
        find_settlement_pending_for_symbol,
        make_exchange_trade_key,
        place_acceptance_vs_fill,
        register_applied_trade_key,
        validate_exchange_close_evidence,
    )

    pos = get_position(st, symbol)
    if pos is None:
        tid_sp, sp = find_settlement_pending_for_symbol(
            getattr(st, "settlement_pending", None), symbol
        )
        if sp and sp.get("close_resend_blocked"):
            return {
                "ok": False,
                "block_reason": "SETTLEMENT_PENDING_NO_CLOSE_RESEND",
                "settlement_status": sp.get("settlement_status"),
                "local_trade_id": tid_sp,
            }
        return {"ok": False, "block_reason": "NO_POSITION"}
    qty = min(qty, pos.qty_remaining)
    if qty <= 0:
        return {"ok": False, "block_reason": "ZERO_QTY"}

    price_status = "known"
    # Testnet: place close, but only ledger confirmed executed qty (accept ≠ fill)
    if not exchange_already_done:
        try:
            from . import testnet_hooks

            if testnet_hooks.enabled():
                pd_now = st.positions.get(symbol) or {}
                if isinstance(pd_now, dict) and pd_now.get("testnet_close_client_order_id"):
                    return {
                        "ok": False,
                        "block_reason": "CLOSE_ALREADY_PENDING",
                        "client_order_id": pd_now.get("testnet_close_client_order_id"),
                    }
                tn = testnet_hooks.close_position_exchange(
                    symbol=symbol,
                    side=str(pos.side),
                    qty=float(qty),
                    profile_id=str(getattr(config, "PROFILE_ID", "") or ""),
                )
                info = place_acceptance_vs_fill(tn)
                if not info.get("accepted") and not info.get("skipped"):
                    return {"ok": False, "block_reason": f"TESTNET_CLOSE_FAIL:{info.get('error') or tn.get('error')}"}
                if str(info.get("status") or "") == "ALREADY_FLAT":
                    return {
                        "ok": False,
                        "block_reason": "EXCHANGE_ALREADY_FLAT_USE_RECONCILE",
                        "action": "NEED_RECONCILE_FLAT",
                        "client_order_id": info.get("client_order_id"),
                    }
                elif not info.get("has_confirmed_fill"):
                    pd2 = st.positions.get(symbol)
                    if isinstance(pd2, dict):
                        pd2.update(
                            build_pending_close_state(
                                client_order_id=info.get("client_order_id"),
                                exchange_order_id=info.get("exchange_order_id"),
                                qty_requested=float(qty),
                                reason=reason,
                                status=str(info.get("status") or "ACCEPTED_UNFILLED"),
                            )
                        )
                        save_account(st)
                    return {
                        "ok": False,
                        "block_reason": "CLOSE_ACCEPTED_AWAITING_FILL",
                        "client_order_id": info.get("client_order_id"),
                        "status": info.get("status"),
                    }
                else:
                    qty = min(qty, float(info["confirmed_executed_qty"]))
                    if info.get("avg_price"):
                        exit_price = float(info["avg_price"])
                    else:
                        price_status = "unknown"
                    exchange_already_done = True
                    extra_fill = {
                        **(extra_fill or {}),
                        "testnet_client_order_id": info.get("client_order_id"),
                        "binance_order_id": info.get("exchange_order_id"),
                        "binance_avg_price": info.get("avg_price"),
                        "binance_executed_qty": info.get("confirmed_executed_qty"),
                        "via": "BINANCE_CLOSE_FILL",
                        "fill_source": "exchange_execution_confirmed",
                        "price_status": price_status,
                    }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "block_reason": f"TESTNET_CLOSE_HOOK_ERROR:{exc}"}
    else:
        v = validate_exchange_close_evidence(
            extra_fill,
            expect_symbol=symbol,
            expect_position_side=str(pos.side) if pos else None,
        )
        if not v.get("ok"):
            return {"ok": False, "block_reason": f"EXCHANGE_ALREADY_DONE_WITHOUT_EVIDENCE:{v.get('reason')}"}
        if not v.get("allows_fill_accounting"):
            return {
                "ok": False,
                "block_reason": "FLAT_EVIDENCE_NOT_FILL",
                "action": "NEED_RECONCILE_FLAT",
                "detail": v.get("note") or v.get("reason"),
            }
        price_status = str((extra_fill or {}).get("price_status") or price_status)

    if qty <= 1e-12:
        return {"ok": False, "block_reason": "ZERO_CONFIRMED_QTY"}

    entry_px = float(entry_override) if entry_override is not None else float(pos.entry_price)
    from . import c_maker_exec_model as _mm

    if bool(getattr(config, "MAKER_EXECUTION_ENABLED", False)):
        # Maker-exec path: require explicit resting fill evidence for profit maker fee
        _intended = _mm.role_for_exit_reason(reason)
        if extra_fill and extra_fill.get("force_maker_fee"):
            _exit_role = "maker"
            _exit_role_note = "resting_maker_fill"
        elif extra_fill and extra_fill.get("liquidity_role_assumption") == "taker":
            _exit_role = "taker"
            _exit_role_note = "protective_or_mark_taker"
        elif extra_fill and extra_fill.get("liquidity_role_assumption") == "maker":
            _exit_role = "maker"
            _exit_role_note = "extra_fill_maker"
        else:
            # default: protective reasons taker; profit without resting fill stays taker on maker-exec path
            _exit_role = "taker"
            _exit_role_note = (
                "default_taker_until_explicit_maker_fill"
                if _intended == "maker"
                else "protective_or_stop_taker"
            )
    else:
        # Paper / non-maker-exec: limit TP reasons → maker; stops → taker
        _exit_role, _exit_role_note = _exit_liquidity_role(reason, extra_fill)

    meta = dict(extra_fill or {})
    meta["price_status"] = str(meta.get("price_status") or price_status)
    if meta.get("exchange_trade_id") is not None:
        if not hasattr(st, "applied_exchange_trade_keys") or st.applied_exchange_trade_keys is None:
            st.applied_exchange_trade_keys = []
        tkey = make_exchange_trade_key(
            env_id=meta.get("env_id"),
            account_id=meta.get("account_id") or str(getattr(config, "PROFILE_ID", "") or ""),
            symbol=symbol,
            trade_id=meta.get("exchange_trade_id"),
        )
        is_new, st.applied_exchange_trade_keys = register_applied_trade_key(
            st.applied_exchange_trade_keys, tkey
        )
        if not is_new:
            return {"ok": False, "block_reason": "DUPLICATE_EXCHANGE_TRADE", "key": tkey}

    fee_est = _fee(exit_price * qty, role=_exit_role)
    gross_est = _pnl(pos.side, entry_px, exit_price, qty)
    if exchange_already_done:
        acc = accounting_from_exchange_or_estimate(
            meta=meta,
            qty=qty,
            exit_price=exit_price,
            entry_price=entry_px,
            side=str(pos.side),
            estimated_fee=fee_est,
            estimated_gross=gross_est,
        )
        fee = float(acc["fee_u_for_ledger"])
        gross = float(acc["gross_u"])
        net = float(acc["net_u_for_ledger"])
        pnl_fields = {**acc}
    else:
        fee = fee_est
        gross = gross_est
        net = gross - fee
        pnl_fields = estimated_pnl_fields(
            fee_u=fee, net_u=net, gross_u=gross, price_status=str(meta.get("price_status") or price_status)
        )

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
            "cash_delta_u": gross
            - fee
            + (float(pos.margin_u) if is_flat else 0.0)
            + (
                float(margin_release_u)
                if (margin_release_u and margin_release_u > 0 and not is_flat)
                else 0.0
            ),
            "margin_release_u": float(margin_release_u or 0.0),
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
            "fee_rate_one_side": _fee_rate_for_role(_exit_role),
            "liquidity_role_assumption": _exit_role,
            "liquidity_role_intended": _mm.role_for_exit_reason(reason),
            "exit_role_note": _exit_role_note,
            "slippage_adverse": config.SLIPPAGE_ADVERSE,
            "simulation_assumption": not exchange_already_done,
            "fill_source": (
                "exchange_execution_confirmed" if exchange_already_done else "paper_simulation"
            ),
            "execution_model_version": (
                str(getattr(config, "EXECUTION_MODEL_VERSION", "") or "")
                or ("paper_touch_v1" if not exchange_already_done else "exchange_sync_v1")
            ),
            "execution_mode": "testnet" if exchange_already_done else "paper",
            "cost_basis_note": "net_u=gross-fee; funding not claimed complete when funding_data_gap",
            "binance_avg_price": (extra_fill or {}).get("binance_avg_price"),
            "binance_executed_qty": qty,
            "fully_reconciled": bool(pnl_fields.get("is_confirmed_exchange_net")),
            **pnl_fields,
        }
    # Paper must not claim exchange-confirmed price labels
    if not exchange_already_done:
        fill_row["is_confirmed_exchange_price"] = False
        fill_row["is_confirmed_exchange_net"] = False
        fill_row["price_confirmation"] = "paper_mark_or_limit_model"
        fill_row["gross_status"] = "computed_from_paper_fill_price"
        fill_row["pnl_confirmation"] = "paper_estimated"
        fill_row["fully_reconciled"] = False
    if extra_fill:
        # Do not let extra_fill overwrite confirmed accounting flags with stale estimated_*
        for k, v in extra_fill.items():
            if k in ("fee_u", "gross_u", "net_u", "fee_status", "net_status", "pnl_confirmation"):
                continue
            fill_row[k] = v
        if extra_fill.get("fill_source"):
            fill_row["fill_source"] = extra_fill["fill_source"]
            fill_row["simulation_assumption"] = extra_fill["fill_source"] == "paper_simulation"
        if extra_fill.get("binance_executed_qty") is None:
            fill_row["binance_executed_qty"] = qty
        if exchange_already_done and fill_row.get("fill_model") and str(fill_row["fill_model"]).startswith("paper"):
            fill_row["paper_simulation_assumption_ref"] = fill_row.pop("fill_model")
            fill_row["fill_model"] = None
    # Re-assert paper labels after extra_fill merge (never claim exchange confirm on paper)
    if not exchange_already_done:
        fill_row["is_confirmed_exchange_price"] = False
        fill_row["is_confirmed_exchange_net"] = False
        fill_row["price_confirmation"] = "paper_mark_or_limit_model"
        fill_row["gross_status"] = "computed_from_paper_fill_price"
        fill_row["pnl_confirmation"] = "paper_estimated"
        fill_row["fully_reconciled"] = False
        fill_row["fill_source"] = fill_row.get("fill_source") or "paper_simulation"
        fill_row["execution_model_version"] = fill_row.get("execution_model_version") or "paper_touch_v1"
        try:
            from .research_event_schema import enrich_paper_event

            p0 = float((pos.pyramid or {}).get("p0_entry") or pos.entry_price) if pos.pyramid else float(pos.entry_price)
            g7 = enrich_paper_event(
                st=st,
                pos=pos,
                event=fill_row["event"],
                book_ts=book_ts,
                p0_before=p0,
                p0_after=p0,
                avg_before=float(pos.entry_price),
                avg_after=float(pos.entry_price),
            )
            for k, v in g7.items():
                fill_row.setdefault(k, v)
        except Exception:
            fill_row["cycle_id"] = fill_row.get("cycle_id") or pos.trade_id
            fill_row["g7_enrich_error"] = True
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
        # Withdraw any remaining TP1/TP2 / C pyramid adds when trade ends
        try:
            pid = str(getattr(config, "PROFILE_ID", "") or "")
            _cancel_resting_b_tp(st, pos, reason=f"FLAT:{reason}", profile_id=pid)
            if getattr(pos, "pyramid", None):
                _cancel_c_pyramid_adds_exchange(
                    st, pos, f"FLAT:{reason}", profile_id=pid, also_cancel_p0_tp=True
                )
        except Exception:
            pass
        st.cash_u += pos.margin_u
        clear_position(st, symbol)
        st.cooldowns_until[symbol] = now_ts() + config.COOLDOWN_MINUTES * 60
    else:
        _set_position_keep_testnet(st, pos)
        pd_keep = st.positions.get(symbol) or {}
        if isinstance(pd_keep, dict) and pd_keep.get("testnet_close_client_order_id"):
            from common.execution_sync import clear_pending_close_fields

            pd_keep["testnet_close_qty_applied"] = float(pd_keep.get("testnet_close_qty_applied") or 0) + float(qty)
            req = float(pd_keep.get("testnet_close_qty_requested") or 0)
            if req > 0 and float(pd_keep["testnet_close_qty_applied"]) + 1e-12 >= req:
                clear_pending_close_fields(pd_keep)
            st.positions[symbol] = pd_keep

    save_account(st)
    return {
        "ok": True,
        "net": net,
        "qty": qty,
        "remaining": max(0.0, pos.qty_remaining if not is_flat else 0.0),
        "gross": gross,
        "fee": fee,
        "net_status": "estimated_using_assumed_fee",
    }


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


def _refresh_live_exchange_sl(
    st: AccountState,
    symbol: str,
    pos: Position,
    *,
    profile_id: str,
    testnet_hooks: Any,
    events: list[dict[str, Any]],
) -> tuple[bool, bool]:
    """Verify exchange SL algo is still open; rehang if stale. Returns (sl_alive, sl_failed)."""
    pd_now = st.positions.get(symbol)
    if not isinstance(pd_now, dict):
        return False, True
    sl_cid = str(pd_now.get("testnet_sl_client_order_id") or "")
    sl_alive = False
    if sl_cid:
        try:
            from common.testnet_bridge import algo_stop_still_open as _sl_open

            strat = testnet_hooks.strategy_from_profile(profile_id)
            sl_alive = bool(
                _sl_open(
                    strategy=strat,
                    symbol=symbol,
                    client_order_id=sl_cid,
                    exchange_order_id=pd_now.get("testnet_sl_exchange_order_id"),
                )
            )
        except Exception:
            sl_alive = False
    if not sl_alive:
        for k in (
            "testnet_sl_client_order_id",
            "testnet_sl_exchange_order_id",
            "testnet_sl_stop_price",
        ):
            pd_now.pop(k, None)
        try:
            sl_meta = testnet_hooks.ensure_stop_on_open(
                symbol=symbol,
                side=str(pos.side),
                stop_price=float(pos.effective_stop),
                qty=float(pos.qty_remaining),
                profile_id=profile_id,
                pending=pd_now,
            )
        except Exception as exc:  # noqa: BLE001
            sl_meta = {"ok": False, "error": str(exc)}
        if sl_meta.get("ok") and sl_meta.get("client_order_id"):
            pd_now["testnet_sl_client_order_id"] = sl_meta.get("client_order_id")
            pd_now["testnet_sl_exchange_order_id"] = sl_meta.get("exchange_order_id")
            pd_now["testnet_sl_stop_price"] = sl_meta.get("stop_price") or pos.effective_stop
            pd_now.pop("testnet_sl_place_failed", None)
            sl_alive = True
            events.append(
                {
                    "action": "SL_STALE_REHANG",
                    "symbol": symbol,
                    "client_order_id": sl_meta.get("client_order_id"),
                }
            )
        else:
            pd_now["testnet_sl_place_failed"] = True
            events.append(
                {
                    "action": "SL_STALE_REHANG_FAIL",
                    "symbol": symbol,
                    "error": (sl_meta or {}).get("error"),
                }
            )
        st.positions[symbol] = pd_now
        save_account(st)
    sl_failed = bool((st.positions.get(symbol) or {}).get("testnet_sl_place_failed"))
    return sl_alive, sl_failed


def _manage_b_partial70(
    st: AccountState,
    symbol: str,
    *,
    book: dict[str, Any],
    pos: Position,
    mark: float,
    book_ts: float,
) -> list[dict[str, Any]]:
    """70% at TP50 price; after filled move stop→entry; 30% targets TP100. SL75 until first TP filled.

    Testnet: TP1/TP2 are reduceOnly LIMITs on Binance — poll fill, do not mark-close TP.
    """
    events: list[dict[str, Any]] = []
    tp50 = float(pos.take_profit) if pos.take_profit is not None else None
    tp100 = float(pos.take_profit_2) if pos.take_profit_2 is not None else None
    sl = float(pos.effective_stop)
    profile_id = str(getattr(config, "PROFILE_ID", "") or "")

    try:
        from . import testnet_hooks

        tn_on = testnet_hooks.enabled()
    except Exception:
        tn_on = False
        testnet_hooks = None  # type: ignore

    sl_hit = (pos.side == "LONG" and mark <= sl) or (pos.side == "SHORT" and mark >= sl)

    # Testnet: never mark-race MARKET close when exchange SL/TP are live
    if tn_on and testnet_hooks is not None:
        # Detect exchange already flat (SL/TP filled on Binance)
        try:
            from common.testnet_bridge import get_adapter as _ga

            strat = testnet_hooks.strategy_from_profile(profile_id)
            amt = float(_ga(strat).position_amt(symbol))
        except Exception:
            amt = None
        if amt is not None and abs(amt) < 1e-12 and float(pos.qty_remaining) > 1e-12:
            pd_sl = st.positions.get(symbol) or {}
            r = reconcile_exchange_flat(
                st,
                symbol,
                position_amt_observed=float(amt),
                evidence_source="positionRisk",
                client_order_id=(pd_sl.get("testnet_sl_client_order_id") if isinstance(pd_sl, dict) else None),
                position_side=str(pos.side),
                reason="EXCHANGE_FLAT_SL_OR_TP",
            )
            events.append({"action": "EXCHANGE_FLAT_RECONCILE", **r})
            return events
        sl_alive, sl_failed = _refresh_live_exchange_sl(
            st, symbol, pos, profile_id=profile_id, testnet_hooks=testnet_hooks, events=events
        )
        pos = get_position(st, symbol) or pos
        if sl_alive and not sl_failed and sl_hit:
            # wait for exchange STOP_MARKET — do not market-close
            pass
        elif sl_hit and sl_failed:
            reason = "ENTRY_STOP" if pos.partial_tp_done else "INITIAL_STOP_SL_PLACE_FAIL"
            r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
            events.append({"action": reason, "fill_price": mark, "stop_level": sl, **r})
            return events
        elif sl_hit and not sl_alive:
            # no exchange SL → emergency mark path
            reason = "ENTRY_STOP" if pos.partial_tp_done else "INITIAL_STOP"
            r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
            events.append({"action": reason, "fill_price": mark, "stop_level": sl, **r})
            return events
    elif sl_hit:
        # Paper: 同報價競態：止損優先
        reason = "ENTRY_STOP" if pos.partial_tp_done else "INITIAL_STOP"
        r = close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason=reason, book_ts=book_ts)
        events.append({"action": reason, "fill_price": mark, "stop_level": sl, **r})
        return events

    # --- Testnet: poll pre-placed TP1/TP2 reduceOnly limits ---
    if tn_on and testnet_hooks is not None and (pos.resting_tp_orders or []):
        for level in ("TP1", "TP2"):
            pos = get_position(st, symbol)
            if pos is None:
                return events
            order = next(
                (
                    o
                    for o in (pos.resting_tp_orders or [])
                    if o.get("level") == level and o.get("status") in ("NEW", "PARTIAL")
                ),
                None,
            )
            if not order:
                continue
            if level == "TP1" and pos.partial_tp_done:
                continue
            if level == "TP2" and not pos.partial_tp_done:
                if any(
                    o.get("level") == "TP1" and o.get("status") in ("NEW", "PARTIAL")
                    for o in (pos.resting_tp_orders or [])
                ):
                    continue

            cid = order.get("testnet_client_order_id")
            if not cid:
                tn = testnet_hooks.place_tp_limit(
                    symbol=symbol,
                    position_side=str(pos.side),
                    qty=float(order["qty"]),
                    price=float(order["limit_price"]),
                    level=level,
                    profile_id=profile_id,
                )
                if tn.get("ok") and tn.get("client_order_id"):
                    order["testnet_client_order_id"] = tn["client_order_id"]
                    for o in pos.resting_tp_orders or []:
                        if o.get("order_id") == order.get("order_id"):
                            o["testnet_client_order_id"] = tn["client_order_id"]
                    _set_position_keep_testnet(st, pos)
                    save_account(st)
                continue

            polled = testnet_hooks.poll_order(
                symbol=symbol, client_order_id=str(cid), profile_id=profile_id
            )
            if not polled or polled.get("error"):
                continue
            st_ex = str(polled.get("status") or "")
            qty = 0.0
            fill_px = float(order["limit_price"])
            exchange_done = False
            via = "PREPLACED_LIMIT"
            if st_ex in ("NEW", "PARTIALLY_FILLED"):
                exec_q = float(polled.get("executed_qty") or 0)
                prev_filled = float(order.get("qty_filled") or 0)
                if st_ex == "NEW" or exec_q <= prev_filled + 1e-12:
                    continue
                qty = min(exec_q - prev_filled, pos.qty_remaining)
                fill_px = float(polled.get("avg_price") or 0) or fill_px
                exchange_done = True
                via = "BINANCE_TP_PARTIAL"
            elif st_ex == "FILLED":
                qty = min(
                    float(polled.get("executed_qty") or order["qty"]) - float(order.get("qty_filled") or 0),
                    pos.qty_remaining,
                )
                fill_px = float(polled.get("avg_price") or 0) or fill_px
                exchange_done = True
                via = "BINANCE_TP_LIMIT"
            elif st_ex in ("CANCELED", "EXPIRED", "REJECTED"):
                order["status"] = "CANCELLED"
                order["cancel_reason"] = f"EXCHANGE_{st_ex}"
                _set_position_keep_testnet(st, pos)
                save_account(st)
                continue
            else:
                continue
            if qty <= 0:
                order["status"] = "FILLED"
                _set_position_keep_testnet(st, pos)
                continue

            reason = "PARTIAL_TP50_70" if level == "TP1" else "TP100_REMAINDER"
            order["qty_filled"] = float(order.get("qty_filled") or 0) + qty
            order["status"] = (
                "FILLED" if float(order["qty_filled"]) + 1e-12 >= float(order["qty"]) else "PARTIAL"
            )
            order["filled_at"] = now_iso()
            order["fill_price"] = fill_px
            for o in pos.resting_tp_orders or []:
                if o.get("order_id") == order.get("order_id"):
                    o.update(order)
            _set_position_keep_testnet(st, pos)

            will_flat = qty + 1e-12 >= float(pos.qty_remaining)
            r = close_qty(
                st,
                symbol,
                qty=qty,
                exit_price=fill_px,
                reason=reason,
                book_ts=book_ts,
                exchange_already_done=exchange_done,
                extra_fill={
                    "via": via,
                    "testnet_client_order_id": cid,
                    "binance_order_id": (polled or {}).get("exchange_order_id") or (polled or {}).get("order_id"),
                    "binance_avg_price": fill_px,
                    "binance_executed_qty": qty,
                },
            )
            events.append({"action": reason, "tp_level": fill_px, "via": via, **r})
            if not r.get("ok"):
                return events
            if will_flat or get_position(st, symbol) is None:
                return events

            pos2 = get_position(st, symbol)
            if pos2 is None:
                return events
            if level == "TP1" and pos2.qty_remaining > 1e-12:
                pos2.partial_tp_done = True
                pos2.partial_tp_qty = float(r.get("qty") or qty)
                pos2.partial_tp_price = fill_px
                if tp100 is not None:
                    pos2.take_profit = tp100
                entry_stop = float(pos2.entry_price)
                pos2.breakeven_stop = entry_stop
                pos2.effective_stop = entry_stop
                pos2.stop_set_events.append(
                    {
                        "ts": book_ts,
                        "kind": "ENTRY_STOP_AFTER_PARTIAL",
                        "price": entry_stop,
                        "note": "not_fee_breakeven",
                    }
                )
                # keep resting TP2 status from pos
                pos2.resting_tp_orders = list(pos.resting_tp_orders or [])
                _set_position_keep_testnet(st, pos2)

                pd = st.positions.get(symbol) or {}
                old_sl = pd.get("testnet_sl_client_order_id") if isinstance(pd, dict) else None
                if old_sl:
                    testnet_hooks.cancel_cid(
                        symbol=symbol, client_order_id=str(old_sl), profile_id=profile_id
                    )
                sln = testnet_hooks.ensure_stop_on_open(
                    symbol=symbol,
                    side=str(pos2.side),
                    stop_price=entry_stop,
                    qty=float(pos2.qty_remaining),
                    profile_id=profile_id,
                    pending=None,
                )
                if isinstance(pd, dict) and sln.get("client_order_id"):
                    pd["testnet_sl_client_order_id"] = sln.get("client_order_id")
                    pd["testnet_sl_exchange_order_id"] = sln.get("exchange_order_id")
                    pd["testnet_sl_stop_price"] = sln.get("stop_price")
                save_account(st)
                events.append(
                    {
                        "action": "TP1_FILLED_STOP_TO_ENTRY",
                        "effective_stop": entry_stop,
                        "via": via,
                    }
                )
                if (pos2.side == "LONG" and mark <= entry_stop) or (
                    pos2.side == "SHORT" and mark >= entry_stop
                ):
                    r2 = close_qty(
                        st,
                        symbol,
                        qty=pos2.qty_remaining,
                        exit_price=mark,
                        reason="ENTRY_STOP",
                        book_ts=book_ts,
                    )
                    events.append({"action": "ENTRY_STOP_IMMEDIATE", "fill_price": mark, **r2})
            return events
        # Testnet with resting TP still working — do not fall through to mark TP
        pos_chk = get_position(st, symbol)
        if pos_chk and any(
            o.get("status") in ("NEW", "PARTIAL") for o in (pos_chk.resting_tp_orders or [])
        ):
            return events
        return events

    # Testnet without paper mark TP when exchange protection is expected
    if tn_on:
        return events

    # --- Paper / no resting TP: mark-touch path ---
    tp50_hit = (not pos.partial_tp_done) and tp50 is not None and (
        (pos.side == "LONG" and mark >= tp50) or (pos.side == "SHORT" and mark <= tp50)
    )
    tp100_hit = pos.partial_tp_done and tp100 is not None and (
        (pos.side == "LONG" and mark >= tp100) or (pos.side == "SHORT" and mark <= tp100)
    )

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
        if pos.qty_remaining > 1e-12:
            pos.partial_tp_done = True
            pos.partial_tp_qty = float(r.get("qty") or qty_tp)
            pos.partial_tp_price = mark
            pos.take_profit = tp100
            entry_stop = float(pos.entry_price)
            pos.breakeven_stop = entry_stop
            pos.effective_stop = entry_stop
            pos.stop_set_events.append(
                {"ts": book_ts, "kind": "ENTRY_STOP_AFTER_PARTIAL", "price": entry_stop, "note": "not_fee_breakeven"}
            )
            _set_position_keep_testnet(st, pos)
            save_account(st)
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
    qty_override: float | None = None,
    exchange_already_done: bool = False,
) -> list[dict[str, Any]]:
    if bool(getattr(config, "MAKER_EXECUTION_ENABLED", False)):
        # Hard ban: maker-exec must not touch-fill adds
        return [
            {
                "action": "PYRAMID_ADD_BLOCKED",
                "layer": add_name,
                "block_reason": "MAKER_EXEC_FORBIDS_TOUCH_ADD",
            }
        ]
    events: list[dict[str, Any]] = []
    pyr = dict(pos.pyramid or {})
    margin_full = float(pyr.get("margin_per") or config.MARGIN_PER_TRADE_USDT)
    notional = float(pyr.get("notional_per") or config.NOTIONAL_USDT)
    step = float(pyr.get("step") or 0.001)
    qty = float(qty_override) if qty_override is not None else round_step(notional / limit_px, step)
    if qty <= 0:
        events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": add_name, "block_reason": "LOT_FILTER"})
        return events
    # Partial exchange apply: scale margin to filled fraction of planned layer qty
    planned = None
    for a0 in pyr.get("pending_adds") or []:
        if a0.get("name") == add_name:
            planned = float(a0.get("planned_qty") or 0) or None
            break
    if qty_override is not None and planned and planned > 1e-12:
        margin_need = margin_full * (qty / planned)
    else:
        margin_need = margin_full
    if st.free_cash() + 1e-9 < margin_need:
        events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": add_name, "block_reason": "INSUFFICIENT_MARGIN"})
        return events
    if getattr(st, "ledger_write_failed", False) or getattr(st, "new_exposure_blocked_until_reconcile", False):
        events.append(
            {
                "action": "PYRAMID_ADD_BLOCKED",
                "layer": add_name,
                "block_reason": "LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE",
            }
        )
        return events
    add_role = "maker" if (exchange_already_done or bool(getattr(config, "MAKER_EXECUTION_ENABLED", False))) else "taker"
    fee = _fee(limit_px * qty, role=add_role)
    p0_anchor = float(pyr.get("p0_entry") or pos.entry_price)
    avg_before = (
        (float(pos.notional_initial) / float(pos.qty_initial)) if float(pos.qty_initial) > 1e-12 else float(pos.entry_price)
    )
    layers_snapshot_before = [
        {"name": L.get("name"), "qty": L.get("qty"), "closed": L.get("closed"), "entry": L.get("entry")}
        for L in (pyr.get("layers") or [])
    ]
    st.cash_u -= margin_need
    st.cash_u -= fee
    st.realized_fees_u += fee
    st.realized_net_u -= fee
    pos.fees_paid_u += fee
    pos.qty_remaining += qty
    pos.qty_initial += qty
    pos.margin_u = float(pos.margin_u) + margin_need
    pos.notional_initial = float(pos.notional_initial) + limit_px * qty
    avg_after = (
        (float(pos.notional_initial) / float(pos.qty_initial)) if float(pos.qty_initial) > 1e-12 else None
    )
    layers = list(pyr.get("layers") or [])
    layers.append({"name": add_name, "entry": limit_px, "qty": qty, "margin_u": margin_need, "closed": False, "exit_reason": None})
    pyr["layers"] = layers
    # P0 anchor must stay frozen through adds / avg changes
    pyr["p0_entry"] = p0_anchor
    pyr["rebound_exit_price"] = p0_anchor
    pyr["stop_frozen"] = True
    for a in pyr.get("pending_adds") or []:
        if a.get("name") == add_name:
            # digest may refine PARTIAL vs FILLED via applied_fill_qty
            a["status"] = "FILLED"
            a["filled_at"] = now_iso()
            a["filled_price"] = limit_px
        elif add_name == "P1" and a.get("name") == "P2" and a.get("status") == "WAITING_P1":
            a["status"] = "PENDING"
    pos.pyramid = pyr
    _set_position_keep_testnet(st, pos)
    save_account(st)
    add_fill: dict[str, Any] = {
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
        "fill_source": "paper_simulation" if not exchange_already_done else "exchange_execution_confirmed",
        "execution_model_version": str(getattr(config, "EXECUTION_MODEL_VERSION", "") or "")
        or ("paper_touch_v1" if not exchange_already_done else "exchange_sync_v1"),
        "is_confirmed_exchange_price": False if not exchange_already_done else True,
        "liquidity_role_assumption": add_role if bool(getattr(config, "MAKER_EXECUTION_ENABLED", False)) else None,
        "fee_rate": float(getattr(config, "MAKER_FEE_RATE", config.FEE_RATE_ONE_SIDE))
        if add_role == "maker"
        else config.FEE_RATE_ONE_SIDE,
        "cash_delta_u": -(margin_need + fee),
        "margin_lock_u": margin_need,
        "cycle_id": pos.trade_id,
        "p0_anchor_before": p0_anchor,
        "p0_anchor_after": p0_anchor,
        "position_avg_before": avg_before,
        "position_avg_after": avg_after,
        "layers_before": layers_snapshot_before,
        "layers_after": [
            {"name": L.get("name"), "qty": L.get("qty"), "closed": L.get("closed"), "entry": L.get("entry")}
            for L in layers
        ],
        "book_ts": book_ts,
    }
    if not exchange_already_done:
        try:
            from .research_event_schema import enrich_paper_event

            g7 = enrich_paper_event(
                st=st,
                pos=pos,
                event="PYRAMID_ADD",
                book=book,
                book_ts=book_ts,
                p0_before=p0_anchor,
                p0_after=p0_anchor,
                avg_before=avg_before,
                avg_after=avg_after,
                extra={"layer": add_name},
            )
            for k, v in g7.items():
                add_fill.setdefault(k, v)
        except Exception:
            add_fill["g7_enrich_error"] = True
    _append_fill(add_fill)

    _append_trade_row(
        {
            "timestamp": now_iso(),
            "event": "PYRAMID_ADD",
            "trade_id": pos.trade_id,
            "symbol": symbol,
            "side": pos.side,
            "qty": qty,
            "price": limit_px,
            "fee_u": fee,
            "realized_net_u": -fee,
            "exit_reason": f"ADD_{add_name}",
            "linked_prev_trade_id": pos.linked_prev_trade_id or "",
            "run_id": st.run_id,
        }
    )
    events.append({"action": "PYRAMID_ADD", "layer": add_name, "qty": qty, "price": limit_px, "ok": True})
    pid = str(getattr(config, "PROFILE_ID", "") or "")
    # P1/P2 成交後：自動掛 P0 進場價 reduceOnly 停利（R2）
    try:
        r2 = _place_c_r2_tp_for_layer(
            st,
            pos,
            layer_name=str(add_name),
            qty=float(qty),
            profile_id=pid,
        )
        events.append({"action": "R2_TP_PLACE", "layer": add_name, **r2})
    except Exception as exc:  # noqa: BLE001
        events.append({"action": "R2_TP_PLACE_FAIL", "layer": add_name, "error": str(exc)})
    # Pstop already hung at P0 with closePosition — no rehang on P2.
    # (Crash past Pstop must flatten immediately without waiting for our P2 poll.)
    return events


def _sync_c_reduce_only_exit(
    st: AccountState,
    symbol: str,
    *,
    pos: Position,
    book: dict[str, Any],
    book_ts: float,
    order: dict[str, Any] | None,
    polled: dict[str, Any],
    layer: dict[str, Any],
    layer_name: str,
    reason: str,
    via: str,
    cid: str,
    planned_qty: float,
    entry_override: float,
    margin_u: float,
    close_layer_when_done: bool = True,
) -> list[dict[str, Any]]:
    """Apply only *new* executed qty from a reduce-only poll (R2 / P0_TP).

    Tracks cum on the resting order dict; restart-safe via qty_filled / cum_quote.
    """
    from common.execution_sync import incremental_fill_from_cumulative

    events: list[dict[str, Any]] = []
    st_ex = str(polled.get("status") or "").upper()
    if st_ex == "CANCELLED":
        st_ex = "CANCELED"
    try:
        cum = float(polled.get("executed_qty") if "executed_qty" in polled else polled.get("executedQty") or 0)
    except (TypeError, ValueError):
        cum = 0.0
    try:
        cum_avg = float(polled.get("avg_price") if "avg_price" in polled else polled.get("avgPrice") or 0) or None
    except (TypeError, ValueError):
        cum_avg = None
    track = order if isinstance(order, dict) else {}
    prev_cum = float(track.get("qty_filled") or track.get("exchange_cum_executed") or 0)
    prev_quote = track.get("exchange_cum_quote")
    prev_avg = track.get("exchange_cum_avg")
    slice_info = incremental_fill_from_cumulative(
        prev_cum_qty=prev_cum,
        prev_cum_quote=prev_quote,
        prev_cum_avg=prev_avg,
        new_cum_qty=cum,
        new_cum_avg=cum_avg,
    )
    delta = float(slice_info.get("delta_qty") or 0)
    # Persist observation even when no new delta
    if order is not None:
        order["exchange_cum_executed"] = cum
        order["exchange_cum_avg"] = cum_avg
        order["exchange_cum_quote"] = slice_info.get("new_cum_quote")
        order["last_poll_status"] = st_ex
    if delta > 1e-12:
        px = slice_info.get("delta_avg")
        price_status = str(slice_info.get("price_status") or "known")
        if px is None:
            # qty may be confirmed; price is not — mark only for cash continuity
            try:
                px = mark_price_for_side(pos.side, book)
            except Exception:
                px = float(entry_override)
            price_status = "estimated_local_mark_fallback"
        # proportional margin release for partial
        layer_q = float(planned_qty) if planned_qty > 1e-12 else float(layer.get("qty") or delta)
        margin_rel = float(margin_u) * (delta / layer_q) if layer_q > 1e-12 else 0.0
        r = close_qty(
            st,
            symbol,
            qty=min(delta, float(pos.qty_remaining)),
            exit_price=float(px),
            reason=reason,
            book_ts=book_ts,
            entry_override=float(entry_override),
            margin_release_u=margin_rel,
            extra_fill={
                "layer": layer_name,
                "via": via,
                "testnet_client_order_id": cid,
                "binance_order_id": polled.get("exchange_order_id") or polled.get("orderId") or polled.get("order_id"),
                "binance_avg_price": slice_info.get("delta_avg"),  # None if unknown — not mark
                "binance_executed_qty": delta,
                "fill_source": "exchange_execution_confirmed",
                "qty_confirmation": "exchange_executed_qty",
                "price_status": price_status,
                "accounting_price_role": (
                    "local_mark_fallback"
                    if str(price_status).startswith(("estimated", "unknown"))
                    else "exchange_slice_avg"
                ),
            },
            exchange_already_done=True,
        )
        events.append({"action": reason, "layer": layer_name, "via": via, "delta_qty": delta, **r})
        if order is not None:
            order["qty_filled"] = prev_cum + delta
            order["status"] = "PARTIAL" if st_ex == "PARTIALLY_FILLED" else order.get("status") or "PARTIAL"
            order["fill_price"] = slice_info.get("delta_avg")
        # shrink layer qty
        pos = get_position(st, symbol) or pos
        pyr = dict(pos.pyramid or {})
        for L in pyr.get("layers") or []:
            if L.get("name") == layer_name and not L.get("closed"):
                L["qty"] = max(0.0, float(L.get("qty") or 0) - delta)
                L["margin_u"] = max(0.0, float(L.get("margin_u") or 0) - margin_rel)
        pos.pyramid = pyr
        _set_position_keep_testnet(st, pos)
        save_account(st)
        pos = get_position(st, symbol) or pos

    applied = float((order or {}).get("qty_filled") or prev_cum + max(delta, 0))
    layer_rem = 0.0
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    for L in pyr.get("layers") or []:
        if L.get("name") == layer_name and not L.get("closed"):
            layer_rem = float(L.get("qty") or 0)

    if st_ex == "FILLED" or (applied + 1e-12 >= float(planned_qty) and planned_qty > 0):
        if order is not None:
            order["status"] = "FILLED"
            order["qty_filled"] = max(applied, cum)
        if close_layer_when_done:
            for L in pyr.get("layers") or []:
                if L.get("name") == layer_name:
                    L["closed"] = True
                    L["exit_reason"] = reason
                    L["qty"] = 0.0
            for a in pyr.get("pending_adds") or []:
                if a.get("name") == layer_name:
                    a["status"] = "CANCELLED_NO_REFILL"
            pos.pyramid = pyr
            _set_position_keep_testnet(st, pos)
            save_account(st)
        events.append({"action": f"{reason}_COMPLETE", "layer": layer_name, "applied": applied})
    elif st_ex in ("CANCELED", "EXPIRED", "REJECTED"):
        # keep applied fills; release tracking for remainder
        if order is not None:
            order["status"] = "CANCELLED"
            order["cancel_reason"] = f"EXCHANGE_{st_ex}"
        if layer_rem > 1e-12:
            events.append(
                {
                    "action": f"{reason}_CANCEL_REMAINDER",
                    "layer": layer_name,
                    "applied": applied,
                    "layer_remaining": layer_rem,
                    "status": st_ex,
                }
            )
        else:
            for L in pyr.get("layers") or []:
                if L.get("name") == layer_name:
                    L["closed"] = True
                    L["exit_reason"] = f"{reason}_{st_ex}"
            pos.pyramid = pyr
            _set_position_keep_testnet(st, pos)
            save_account(st)
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
    """R2: adds exit at P0 entry; P0 exits at 1% TP; all remaining at frozen Pstop.

    When MAKER_EXECUTION_ENABLED: full takeover via c_maker_exec_bridge
    (no touch-to-add / touch-to-open). Original profile keeps legacy path below.
    """
    if bool(getattr(config, "MAKER_EXECUTION_ENABLED", False)):
        from . import c_maker_exec_bridge as _bridge

        return _bridge.manage_c_pyramid_maker(
            st, symbol, book=book, pos=pos, mark=mark, book_ts=book_ts
        )

    events: list[dict[str, Any]] = []
    # Digest cancel-race fills before stop/TP logic (and before any cancel releases)
    events.extend(
        _digest_c_partial_pending_applies(st, symbol, pos=pos, book=book, book_ts=book_ts)
    )
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    levels = dict(pyr.get("levels") or {})
    pstop = float(levels.get("Pstop") or pos.effective_stop)
    p0_tp = float(levels.get("P0_TP") or pos.take_profit or 0.0)
    rebound = float(pyr.get("rebound_exit_price") or pos.entry_price)
    side = pos.side
    profile_id = str(getattr(config, "PROFILE_ID", "") or "")

    try:
        from . import testnet_hooks

        tn_on = testnet_hooks.enabled()
    except Exception:
        tn_on = False
        testnet_hooks = None  # type: ignore

    # Pending close blocks new exits
    if tn_on:
        pd_blk = st.positions.get(symbol) or {}
        if isinstance(pd_blk, dict) and pd_blk.get("testnet_close_client_order_id"):
            return events

    # 1) Pstop 優先（所有剩餘層）
    stop_hit = (side == "LONG" and mark <= pstop) or (side == "SHORT" and mark >= pstop)
    # Testnet: Pstop is exchange STOP_MARKET closePosition — do not mark-race MARKET
    if tn_on and testnet_hooks is not None:
        try:
            from common.testnet_bridge import get_adapter as _ga

            strat = testnet_hooks.strategy_from_profile(profile_id)
            amt = float(_ga(strat).position_amt(symbol))
        except Exception:
            amt = None
        if amt is not None and abs(amt) < 1e-12 and float(pos.qty_remaining) > 1e-12:
            _cancel_c_pyramid_adds_exchange(
                st, pos, "PSTOP_EXFLAT", profile_id=profile_id, also_cancel_p0_tp=True
            )
            # Cancel may discover add fills — digest before flatten so evidence is not dropped
            pos = get_position(st, symbol) or pos
            events.extend(
                _digest_c_partial_pending_applies(st, symbol, pos=pos, book=book, book_ts=book_ts)
            )
            pos = get_position(st, symbol)
            if pos is None:
                return events
            pd_sl = st.positions.get(symbol) or {}
            r = reconcile_exchange_flat(
                st,
                symbol,
                position_amt_observed=float(amt),
                evidence_source="positionRisk_pstop",
                client_order_id=(pd_sl.get("testnet_sl_client_order_id") if isinstance(pd_sl, dict) else None),
                position_side=str(pos.side),
                reason="EXCHANGE_FLAT_PSTOP",
            )
            events.append({"action": "EXCHANGE_FLAT_RECONCILE", **r})
            return events
        sl_alive, sl_failed = _refresh_live_exchange_sl(
            st, symbol, pos, profile_id=profile_id, testnet_hooks=testnet_hooks, events=events
        )
        pos = get_position(st, symbol) or pos
        if sl_alive and not sl_failed:
            # wait for exchange Pstop; continue to poll adds/TP only
            stop_hit = False
        elif stop_hit and (sl_failed or not sl_alive):
            pass  # emergency mark path below
        else:
            stop_hit = False

    if stop_hit:
        _cancel_c_pyramid_adds_exchange(
            st, pos, "PSTOP", profile_id=profile_id, also_cancel_p0_tp=True
        )
        pos = get_position(st, symbol) or pos
        events.extend(
            _digest_c_partial_pending_applies(st, symbol, pos=pos, book=book, book_ts=book_ts)
        )
        pos = get_position(st, symbol)
        if pos is None:
            return events
        _set_position_keep_testnet(st, pos)
        for layer in list((pos.pyramid or {}).get("layers") or []):
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
            pos = get_position(st, symbol)
            if pos is None:
                return events
            pyr = dict(pos.pyramid or {})
            for L in pyr.get("layers") or []:
                if L.get("name") == layer.get("name"):
                    L["closed"] = True
                    L["exit_reason"] = "PYRAMID_PSTOP"
            pos.pyramid = pyr
            _set_position_keep_testnet(st, pos)
        pos = get_position(st, symbol)
        if pos is not None and pos.qty_remaining > 1e-12:
            close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="PYRAMID_PSTOP", book_ts=book_ts)
        return events

    # 2) 加倉：testnet 輪詢已掛 P1/P2；paper 觸價成交
    bid = float(book.get("bid") or 0.0)
    ask = float(book.get("ask") or 0.0)
    if tn_on and testnet_hooks is not None:
        for a in list((pos.pyramid or {}).get("pending_adds") or []):
            if a.get("status") not in ("ORDER_RESTING", "PENDING", "PARTIAL"):
                continue
            cid = a.get("testnet_client_order_id")
            name = str(a.get("name") or "")
            lim = float(a.get("limit_price") or 0.0)
            if not cid:
                # missing exchange order — hang now
                qty_plan = float(a.get("planned_qty") or 0.0)
                if qty_plan <= 0 and lim > 0:
                    qty_plan = round_step(
                        float((pos.pyramid or {}).get("notional_per") or config.NOTIONAL_USDT) / lim,
                        float((pos.pyramid or {}).get("step") or 0.001),
                    )
                if qty_plan > 0 and lim > 0:
                    tn = testnet_hooks.place_pyramid_add_limit(
                        symbol=symbol,
                        position_side=str(pos.side),
                        qty=qty_plan,
                        price=lim,
                        layer=name,
                        profile_id=profile_id,
                    )
                    if tn.get("ok") and tn.get("client_order_id"):
                        a["testnet_client_order_id"] = tn["client_order_id"]
                        a["status"] = "ORDER_RESTING"
                        a["planned_qty"] = qty_plan
                        pos.pyramid = pos.pyramid or {}
                        _set_position_keep_testnet(st, pos)
                        save_account(st)
                continue
            polled = testnet_hooks.poll_order(
                symbol=symbol, client_order_id=str(cid), profile_id=profile_id
            )
            if not polled or polled.get("error"):
                continue
            st_ex = str(polled.get("status") or "")
            if st_ex in ("NEW", "PARTIALLY_FILLED"):
                continue
            if st_ex in ("CANCELED", "EXPIRED", "REJECTED"):
                a["status"] = f"EXCHANGE_{st_ex}"
                _set_position_keep_testnet(st, pos)
                continue
            if st_ex != "FILLED":
                continue
            fill_qty = float(polled.get("executed_qty") or a.get("planned_qty") or 0.0)
            fill_px = float(polled.get("avg_price") or 0.0) or lim
            ev = _pyramid_fill_add(
                st,
                symbol,
                pos=pos,
                add_name=name,
                limit_px=fill_px,
                book=book,
                book_ts=book_ts,
                qty_override=fill_qty,
                exchange_already_done=True,
            )
            events.extend(ev)
            events.append({"action": "PYRAMID_ADD_EXCHANGE", "layer": name, "via": "BINANCE_LIMIT"})
            pos = get_position(st, symbol)
            if pos is None:
                return events
    else:
        research_arm = str(getattr(config, "RESEARCH_ARM", "") or "").upper()
        for a in list((pos.pyramid or {}).get("pending_adds") or []):
            if a.get("status") != "PENDING":
                continue
            add_name = str(a.get("name") or "")
            # Research arms (isolated profiles only): never alter default/production when RESEARCH_ARM=""
            if research_arm == "B1" and add_name == "P2":
                a["status"] = "CANCELLED_RESEARCH_B1_NO_P2"
                events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": "P2", "block_reason": "RESEARCH_B1_FORBID_P2"})
                _set_position_keep_testnet(st, pos)
                continue
            if research_arm == "B3" and add_name in ("P1", "P2"):
                a["status"] = "CANCELLED_RESEARCH_B3_P0_ONLY"
                events.append({"action": "PYRAMID_ADD_BLOCKED", "layer": add_name, "block_reason": "RESEARCH_B3_P0_ONLY"})
                _set_position_keep_testnet(st, pos)
                continue
            # Same-quote safety: re-check PSTOP before any add (protection > add exposure)
            stop_now = (side == "LONG" and mark <= pstop) or (side == "SHORT" and mark >= pstop)
            if stop_now:
                events.append(
                    {
                        "action": "ADD_BLOCKED_STOP_PRIORITY",
                        "layer": a.get("name"),
                        "mark": mark,
                        "pstop": pstop,
                    }
                )
                # fall through to stop handling on next manage; do not add now
                break
            lim = float(a.get("limit_price") or 0.0)
            touched = (side == "LONG" and ask > 0 and ask <= lim) or (
                side == "SHORT" and bid > 0 and bid >= lim
            )
            if touched:
                if research_arm == "B2" and add_name == "P2":
                    # Protect-exit all remaining at trigger; do NOT add P2 exposure
                    a["status"] = "CANCELLED_RESEARCH_B2_PROTECT"
                    for pend in list((pos.pyramid or {}).get("pending_adds") or []):
                        if pend.get("status") == "PENDING":
                            pend["status"] = "CANCELLED_RESEARCH_B2_PROTECT"
                    events.append(
                        {
                            "action": "B2_PROTECT_AT_P2_TRIGGER",
                            "trigger_limit": lim,
                            "mark": mark,
                            "no_p2_add": True,
                        }
                    )
                    pos = get_position(st, symbol) or pos
                    for layer in list((pos.pyramid or {}).get("layers") or []):
                        if layer.get("closed"):
                            continue
                        q = float(layer.get("qty") or 0.0)
                        if q <= 0:
                            continue
                        r = close_qty(
                            st,
                            symbol,
                            qty=q,
                            exit_price=float(lim),
                            reason="B2_PROTECT_AT_P2_TRIGGER",
                            book_ts=book_ts,
                            margin_release_u=float(layer.get("margin_u") or 0.0),
                        )
                        layer["closed"] = True
                        layer["exit_reason"] = "B2_PROTECT_AT_P2_TRIGGER"
                        events.append({"action": "B2_PROTECT_LAYER", "layer": layer.get("name"), **r})
                        pos = get_position(st, symbol)
                        if pos is None:
                            break
                    if pos is not None:
                        pos.pyramid = pos.pyramid or {}
                        _set_position_keep_testnet(st, pos)
                        try:
                            save_account(st)
                        except Exception as exc:  # noqa: BLE001
                            events.append({"action": "LEDGER_SAVE_FAIL_AFTER_PROTECT", "error": str(exc)})
                    return events
                ev = _pyramid_fill_add(
                    st, symbol, pos=pos, add_name=add_name, limit_px=lim, book=book, book_ts=book_ts
                )
                events.extend(ev)
                pos = get_position(st, symbol)
                if pos is None:
                    return events

    # 3) 追加層反彈至 P0 進場價退出（P0 不跟）
    # testnet: 輪詢 R2_* reduceOnly 限價；缺單則補掛；paper: mark 觸價
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    if tn_on and testnet_hooks is not None:
        for layer in list(pyr.get("layers") or []):
            if layer.get("closed") or layer.get("name") == "P0":
                continue
            lname = str(layer.get("name") or "")
            q = float(layer.get("qty") or 0.0)
            if q <= 0:
                continue
            level = f"R2_{lname}"
            order = next(
                (
                    o
                    for o in (pos.resting_tp_orders or [])
                    if o.get("level") == level and o.get("status") in ("NEW", "PARTIAL")
                ),
                None,
            )
            cid = (order or {}).get("testnet_client_order_id") or layer.get("testnet_r2_tp_client_order_id")
            if not cid:
                r2 = _place_c_r2_tp_for_layer(
                    st, pos, layer_name=lname, qty=q, profile_id=profile_id
                )
                events.append({"action": "R2_TP_BACKFILL", "layer": lname, **r2})
                pos = get_position(st, symbol)
                if pos is None:
                    return events
                pyr = dict(pos.pyramid or {})
                continue
            polled = testnet_hooks.poll_order(
                symbol=symbol, client_order_id=str(cid), profile_id=profile_id
            )
            if not polled or polled.get("error"):
                continue
            st_ex = str(polled.get("status") or "").upper()
            if st_ex == "NEW":
                continue
            # PARTIALLY_FILLED / FILLED / CANCELED(+qty): apply incremental only
            planned = float(layer.get("qty_initial") or layer.get("qty") or q)
            if order is not None and order.get("qty_planned") is None:
                order["qty_planned"] = planned
            ev_r2 = _sync_c_reduce_only_exit(
                st,
                symbol,
                pos=pos,
                book=book,
                book_ts=book_ts,
                order=order,
                polled=polled,
                layer=layer,
                layer_name=lname,
                reason="PYRAMID_R2_REBOUND",
                via="BINANCE_R2_LIMIT",
                cid=str(cid),
                planned_qty=float((order or {}).get("qty_planned") or planned),
                entry_override=float(layer["entry"]),
                margin_u=float(layer.get("margin_u") or 0.0),
            )
            events.extend(ev_r2)
            pos = get_position(st, symbol)
            if pos is None:
                return events
            pyr = dict(pos.pyramid or {})
            if st_ex in ("CANCELED", "EXPIRED", "REJECTED") and not any(
                e.get("action") == "PYRAMID_R2_REBOUND" for e in ev_r2
            ):
                layer.pop("testnet_r2_tp_client_order_id", None)
            # P1/P2 出場後：若 P0 仍在，補掛 P0 +1% 停利限價
            if any(str(e.get("action") or "").startswith("PYRAMID_R2_REBOUND") for e in ev_r2):
                pos2 = get_position(st, symbol)
                if pos2 is not None:
                    ev_tp = _ensure_c_p0_tp_limit(st, pos2, profile_id=profile_id)
                    events.append({"action": "P0_TP_ENSURE_AFTER_R2", **ev_tp})
                    pos = get_position(st, symbol) or pos
                    pyr = dict(pos.pyramid or {})
    else:
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
                for a in pyr.get("pending_adds") or []:
                    if a.get("name") == layer.get("name"):
                        a["status"] = "CANCELLED_NO_REFILL"
                pos.pyramid = pyr
                _set_position_keep_testnet(st, pos)
                save_account(st)

    # 4) P0 1% 止盈 — testnet 輪詢 reduceOnly 限價（含部分成交）；paper: mark 觸價
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    p0_layer = next((L for L in (pyr.get("layers") or []) if L.get("name") == "P0" and not L.get("closed")), None)
    tp_via = "MARK"
    tp_cid = None
    if tn_on and testnet_hooks is not None and p0_layer:
        ens = _ensure_c_p0_tp_limit(st, pos, profile_id=profile_id)
        if ens.get("ok") and ens.get("client_order_id") and not ens.get("reused"):
            events.append({"action": "P0_TP_BACKFILL", **ens})
            pos = get_position(st, symbol) or pos
        pd = st.positions.get(symbol) or {}
        tp_cid = pd.get("testnet_p0_tp_client_order_id") if isinstance(pd, dict) else None
        tp_order = next(
            (
                o
                for o in (pos.resting_tp_orders or [])
                if o.get("level") == "P0_TP" and o.get("status") in ("NEW", "PARTIAL")
            ),
            None,
        )
        if not tp_cid and tp_order:
            tp_cid = tp_order.get("testnet_client_order_id")
        if tp_cid:
            polled = testnet_hooks.poll_order(
                symbol=symbol, client_order_id=str(tp_cid), profile_id=profile_id
            )
            if polled and not polled.get("error"):
                st_ex = str(polled.get("status") or "").upper()
                if st_ex != "NEW":
                    # Cancel residual adds before applying P0 TP deltas (race-safe digest)
                    if st_ex in ("PARTIALLY_FILLED", "FILLED"):
                        _cancel_c_pyramid_adds_exchange(
                            st, pos, "P0_TP", profile_id=profile_id, also_cancel_p0_tp=False
                        )
                        pos = get_position(st, symbol) or pos
                        events.extend(
                            _digest_c_partial_pending_applies(
                                st, symbol, pos=pos, book=book, book_ts=book_ts
                            )
                        )
                        pos = get_position(st, symbol)
                        if pos is None:
                            return events
                        pyr = dict(pos.pyramid or {})
                        p0_layer = next(
                            (
                                L
                                for L in (pyr.get("layers") or [])
                                if L.get("name") == "P0" and not L.get("closed")
                            ),
                            None,
                        )
                        if p0_layer is None:
                            return events
                        tp_order = next(
                            (
                                o
                                for o in (pos.resting_tp_orders or [])
                                if o.get("level") == "P0_TP"
                                and o.get("status") in ("NEW", "PARTIAL", "FILLED")
                            ),
                            tp_order,
                        )
                    planned = float(
                        (tp_order or {}).get("qty_planned")
                        or (tp_order or {}).get("qty")
                        or p0_layer.get("qty")
                        or 0
                    )
                    if tp_order is not None and tp_order.get("qty_planned") is None:
                        tp_order["qty_planned"] = planned
                    ev_p0 = _sync_c_reduce_only_exit(
                        st,
                        symbol,
                        pos=pos,
                        book=book,
                        book_ts=book_ts,
                        order=tp_order,
                        polled=polled,
                        layer=p0_layer,
                        layer_name="P0",
                        reason="PYRAMID_P0_TP1PCT",
                        via="BINANCE_TP_LIMIT",
                        cid=str(tp_cid),
                        planned_qty=planned,
                        entry_override=float(p0_layer["entry"]),
                        margin_u=float(p0_layer.get("margin_u") or 0.0),
                    )
                    events.extend(ev_p0)
                    tp_via = "BINANCE_TP_LIMIT"
                    pos = get_position(st, symbol)
                    if pos is None:
                        return events
                    if st_ex in ("CANCELED", "EXPIRED", "REJECTED") and isinstance(pd, dict):
                        if not any(e.get("action") == "PYRAMID_P0_TP1PCT" for e in ev_p0):
                            pd.pop("testnet_p0_tp_client_order_id", None)
                    # partial/full already applied — skip paper mark path
                    if any(
                        e.get("action") in ("PYRAMID_P0_TP1PCT", "PYRAMID_P0_TP1PCT_COMPLETE")
                        for e in ev_p0
                    ):
                        return events

    tp_hit = (not tn_on) and p0_tp > 0 and (
        (side == "LONG" and mark >= p0_tp) or (side == "SHORT" and mark <= p0_tp)
    )
    pos = get_position(st, symbol)
    if pos is None:
        return events
    pyr = dict(pos.pyramid or {})
    p0_layer = next((L for L in (pyr.get("layers") or []) if L.get("name") == "P0" and not L.get("closed")), None)
    if tp_hit and p0_layer:
        q = float(p0_layer.get("qty") or 0.0)
        q = min(q, pos.qty_remaining)
        _cancel_c_pyramid_adds_exchange(
            st, pos, "P0_TP", profile_id=profile_id, also_cancel_p0_tp=False
        )
        pos = get_position(st, symbol) or pos
        events.extend(
            _digest_c_partial_pending_applies(st, symbol, pos=pos, book=book, book_ts=book_ts)
        )
        pos = get_position(st, symbol)
        if pos is None:
            return events
        pyr = dict(pos.pyramid or {})
        p0_layer = next(
            (L for L in (pyr.get("layers") or []) if L.get("name") == "P0" and not L.get("closed")),
            None,
        )
        if p0_layer is None:
            return events
        q = min(float(p0_layer.get("qty") or 0.0), float(pos.qty_remaining))
        if q <= 1e-12:
            return events
        r = close_qty(
            st,
            symbol,
            qty=q,
            exit_price=mark,
            reason="PYRAMID_P0_TP1PCT",
            book_ts=book_ts,
            entry_override=float(p0_layer["entry"]),
            margin_release_u=float(p0_layer.get("margin_u") or 0.0),
            extra_fill={
                "layer": "P0",
                "pyramid_contrib": "p0_tp",
                "via": "MARK",
                "fill_source": "paper_simulation",
            },
            exchange_already_done=False,
        )
        events.append({"action": "PYRAMID_P0_TP1PCT", "tp_level": p0_tp, "via": tp_via, **r})
        pos = get_position(st, symbol)
        if pos is None:
            return events
        pyr = dict(pos.pyramid or {})
        for L in pyr.get("layers") or []:
            if L.get("name") == "P0":
                L["closed"] = True
                L["exit_reason"] = "PYRAMID_P0_TP1PCT"
        for o in pos.resting_tp_orders or []:
            if o.get("level") == "P0_TP":
                o["status"] = "FILLED"
        pos.pyramid = pyr
        pd = st.positions.get(symbol)
        if isinstance(pd, dict):
            pd.pop("testnet_p0_tp_client_order_id", None)
        if all(L.get("closed") for L in (pyr.get("layers") or [])) or pos.qty_remaining <= 1e-12:
            if pos.qty_remaining > 1e-12:
                close_qty(st, symbol, qty=pos.qty_remaining, exit_price=mark, reason="PYRAMID_FLAT_DUST", book_ts=book_ts)
        else:
            _set_position_keep_testnet(st, pos)
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
    _set_position_keep_testnet(st, pos)
    save_account(st)

    # Pending close sync first — never re-send exit while awaiting fill
    try:
        from . import testnet_hooks as _th0

        if _th0.enabled():
            events.extend(_sync_pending_close_order(st, symbol, book=book, book_ts=book_ts))
            if get_position(st, symbol) is None:
                return events
            pd0 = st.positions.get(symbol) or {}
            if isinstance(pd0, dict) and pd0.get("testnet_close_client_order_id"):
                return events
            pos = get_position(st, symbol) or pos
    except Exception:
        pass

    plan = exit_plans.resolve_exit_plan_id(pos)
    if phase == "atr":
        # 新計畫不跑 ATR trail；legacy 亦不在 fixed-gross 下更新
        return events

    if plan == exit_plans.B_PARTIAL70:
        return events + _manage_b_partial70(st, symbol, book=book, pos=pos, mark=mark, book_ts=book_ts)
    if plan == exit_plans.C_PYRAMID:
        return events + _manage_c_pyramid(st, symbol, book=book, pos=pos, mark=mark, book_ts=book_ts)
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
