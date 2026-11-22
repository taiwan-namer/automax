"""D paper portfolio: whole-position TP/SL/vol-exit (simulation fills)."""

from __future__ import annotations

import csv
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import PersistError, append_jsonl, atomic_write_json, now_iso, now_ts, round_price_tick, round_step


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
    settlement_pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    applied_exchange_trade_keys: list[str] = field(default_factory=list)
    settlement_journal: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    strategy_version: str = config.STRATEGY_VERSION
    updated_at: str = ""
    capital_note: str = "UNCONFIRMED until user decides margin/notional/leverage/tp_sl_basis"
    # Persist-failure gate (D-local): stop NEW queue/open; do not swallow and keep filling.
    persist_halt_new_entries: bool = False
    persist_last_error: str = ""
    persist_failed_at: str = ""
    persist_recovery_required: bool = False

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


def _mark_persist_halt(st: Account, exc: BaseException) -> None:
    st.persist_halt_new_entries = True
    st.persist_recovery_required = True
    st.persist_last_error = str(exc)
    st.persist_failed_at = now_iso()
    marker = {
        "persist_halt_new_entries": True,
        "persist_recovery_required": True,
        "persist_last_error": st.persist_last_error,
        "persist_failed_at": st.persist_failed_at,
        "note": "stop new queue/open; reconcile before resume; no swallowed continue",
    }
    try:
        # Side-file best-effort: account replace may be the locked target.
        p = config.paths()["persist_halt"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(__import__("json").dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    try:
        _event(
            {
                "event": "PERSIST_FAIL_HALT_NEW_ENTRIES",
                "error": str(exc),
                "at": st.persist_failed_at,
                "note": "stop new queue/open; reconcile before resume; no swallowed continue",
            }
        )
    except Exception:
        pass


def load_account() -> Account:
    """Load ledger then reconcile settlement journal before any new apply (avoid duplicate fills)."""
    import json

    paths = config.paths()
    if paths["account"].exists():
        st = Account.from_dict(json.loads(paths["account"].read_text(encoding="utf-8")))
        if not isinstance(getattr(st, "settlement_journal", None), list):
            st.settlement_journal = list(getattr(st, "settlement_journal", None) or [])
        # Side-file halt survives failed account replace
        halt_p = paths.get("persist_halt")
        if halt_p is not None and halt_p.exists():
            try:
                hm = json.loads(halt_p.read_text(encoding="utf-8"))
                if hm.get("persist_halt_new_entries"):
                    st.persist_halt_new_entries = True
                    st.persist_recovery_required = True
                    st.persist_last_error = str(hm.get("persist_last_error") or st.persist_last_error)
                    st.persist_failed_at = str(hm.get("persist_failed_at") or st.persist_failed_at)
            except Exception:
                st.persist_halt_new_entries = True
                st.persist_recovery_required = True
        # Recovery-first: journal reconcile before resume trading
        _recover_settlement_journal(st)
        if st.persist_halt_new_entries or st.persist_recovery_required:
            _event(
                {
                    "event": "PERSIST_RECOVERY_PENDING",
                    "persist_last_error": st.persist_last_error,
                    "persist_failed_at": st.persist_failed_at,
                    "at": now_iso(),
                    "note": "halt remains until operator clears after reconcile",
                }
            )
        return st
    run_id = now_iso().replace(":", "").replace("+", "_")[:15] + "_" + uuid.uuid4().hex[:8]
    st = Account(run_id=run_id, updated_at=now_iso())
    save_account(st)
    return st


def save_account(st: Account) -> bool:
    """Persist account. On failure: mark halt-new-entries and re-raise PersistError (not swallowed)."""
    st.updated_at = now_iso()
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_json(paths["account"], st.to_dict())
        return True
    except PersistError as exc:
        _mark_persist_halt(st, exc)
        raise
    except OSError as exc:
        _mark_persist_halt(st, exc)
        raise PersistError(str(exc)) from exc


def reconcile_before_clear_persist_halt(st: Account) -> dict[str, Any]:
    """Recovery gate: journal + fills/account consistency before clearing halt.

    Deleting PERSIST_HALT.json alone is insufficient and must not resume trading.
    """
    report: dict[str, Any] = {"ok": False, "checks": []}
    journal = _recover_settlement_journal(st)
    report["checks"].append({"name": "settlement_journal_recover", "result": journal})

    paths = config.paths()
    fill_ids: list[str] = []
    if paths["fills"].exists():
        import json as _json

        for line in paths["fills"].read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = _json.loads(line)
            tid = row.get("trade_id")
            if tid and row.get("event") in ("OPEN", "CLOSE", "RECONCILE_CLEAR"):
                fill_ids.append(f"{row.get('event')}:{tid}:{row.get('recorded_at')}")
    report["checks"].append({"name": "fills_indexed", "n": len(fill_ids)})

    # Open positions must have a matching OPEN fill
    missing_open = []
    for sym, pos in (st.positions or {}).items():
        tid = pos.get("trade_id")
        if not tid:
            missing_open.append({"symbol": sym, "reason": "NO_TRADE_ID"})
            continue
        if not any(x.startswith(f"OPEN:{tid}:") for x in fill_ids):
            missing_open.append({"symbol": sym, "trade_id": tid, "reason": "OPEN_FILL_MISSING"})
    report["checks"].append({"name": "positions_have_open_fill", "missing": missing_open})

    # Halt side-file must still be present or in-memory halt set (operator acknowledges recovery path)
    halt_p = paths.get("persist_halt")
    halt_present = bool(getattr(st, "persist_halt_new_entries", False) or (halt_p is not None and halt_p.exists()))
    report["checks"].append({"name": "halt_acknowledged", "halt_present": halt_present})

    report["ok"] = len(missing_open) == 0
    report["at"] = now_iso()
    _event({"event": "PERSIST_RECONCILE_BEFORE_CLEAR", **report})
    return report


def clear_persist_halt_after_reconcile(st: Account, *, operator_note: str = "", force: bool = False) -> dict[str, Any]:
    """Clear halt only after reconcile_before_clear_persist_halt passes (unless force for broken fixtures)."""
    report = reconcile_before_clear_persist_halt(st)
    if not report.get("ok") and not force:
        _event(
            {
                "event": "PERSIST_HALT_CLEAR_REFUSED",
                "operator_note": operator_note,
                "reconcile": report,
                "at": now_iso(),
            }
        )
        return {"ok": False, "reason": "RECONCILE_FAILED", "reconcile": report}
    st.persist_halt_new_entries = False
    st.persist_recovery_required = False
    st.persist_last_error = ""
    st.persist_failed_at = ""
    halt_p = config.paths().get("persist_halt")
    if halt_p is not None and halt_p.exists():
        try:
            halt_p.unlink()
        except OSError:
            pass
    _event({"event": "PERSIST_HALT_CLEARED", "operator_note": operator_note, "reconcile_ok": report.get("ok"), "at": now_iso()})
    save_account(st)
    return {"ok": True, "reconcile": report}


def _event(row: dict[str, Any]) -> None:
    paths = config.paths()
    append_jsonl(paths["events"], {"recorded_at": now_iso(), "simulation_assumption": True, **row})


def _fill(row: dict[str, Any]) -> None:
    paths = config.paths()
    # Do not force simulation_assumption=True when caller already set fill_source
    base = {"recorded_at": now_iso()}
    if "simulation_assumption" not in row and "fill_source" not in row:
        base["simulation_assumption"] = True
    append_jsonl(paths["fills"], {**base, **row})


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
    if getattr(st, "persist_halt_new_entries", False) or getattr(st, "persist_recovery_required", False):
        return {
            "ok": False,
            "reason": "PERSIST_HALT_NEW_ENTRIES",
            "persist_last_error": getattr(st, "persist_last_error", ""),
        }
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
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            tn = testnet_hooks.place_entry(pending, qty=qty)
            if not tn.get("ok"):
                err = str(tn.get("error") or "TESTNET_PLACE_FAILED")
                if "SYMBOL_LOCKED_BY_OTHER_STRATEGY" in err:
                    print(f"[testnet-D] SYMBOL_LOCKED_BY_OTHER_STRATEGY {sym}: {err}", flush=True)
                return {"ok": False, "reason": err}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"TESTNET_HOOK_ERROR:{exc}"}
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
        # Testnet: mirror D SL_USDT barrier onto exchange STOP (keep local sl_usdt)
        sl_meta: dict[str, Any] = {}
        try:
            from . import testnet_hooks

            if testnet_hooks.enabled():
                stop_px = testnet_hooks.strategy_stop_price(
                    entry_price=float(fill_px), qty=float(fill_qty), sl_usdt=float(sl_u)
                )
                sl_meta = testnet_hooks.ensure_stop_on_open(
                    symbol=sym,
                    side="SHORT",
                    stop_price=float(stop_px),
                    qty=float(fill_qty),
                    pending=pending,
                )
        except Exception as exc:  # noqa: BLE001
            sl_meta = {"ok": False, "error": str(exc)}
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
        if sl_meta.get("client_order_id"):
            pos["testnet_sl_client_order_id"] = sl_meta.get("client_order_id")
            pos["testnet_sl_exchange_order_id"] = sl_meta.get("exchange_order_id")
            pos["testnet_sl_stop_price"] = sl_meta.get("stop_price")
        # Testnet: reduceOnly LIMIT TP at entry - TP_USDT/qty (not TAKE_PROFIT_MARKET)
        try:
            from . import testnet_hooks

            if testnet_hooks.enabled() and float(fill_qty) > 0:
                tp_px = testnet_hooks.strategy_tp_price(
                    entry_price=float(fill_px), qty=float(fill_qty), tp_usdt=float(tp_u)
                )
                tp_res = testnet_hooks.place_tp_limit(
                    symbol=sym, qty=float(fill_qty), price=float(tp_px)
                )
                if tp_res.get("ok") and not tp_res.get("skipped"):
                    pos["testnet_tp_client_order_id"] = tp_res.get("client_order_id")
                    pos["testnet_tp_limit_price"] = tp_px
                    pos["testnet_tp_qty"] = float(fill_qty)
                elif not tp_res.get("ok"):
                    print(f"[testnet-D] TP_LIMIT_FAIL {sym}: {tp_res.get('error')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet-D] TP_LIMIT_ERROR {sym}: {exc}", flush=True)
        pos["entry_fee_estimated"] = float(fee)
        pos["entry_fee_status"] = "estimated_schedule_rate"
        pos["entry_fee_confirmed"] = False
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
        pos["entry_fee_estimated"] = float(pos.get("entry_fee_estimated") or 0) + float(fee)
        pos["entry_fee_status"] = "estimated_schedule_rate"
        pos["entry_fee_confirmed"] = False
        st.cash_u -= fee
        st.realized_fees_u += fee
        # Rehang TP limit for new avg entry / qty
        try:
            from . import testnet_hooks

            if testnet_hooks.enabled() and new_q > 0:
                old_tp = pos.get("testnet_tp_client_order_id")
                if old_tp:
                    testnet_hooks.cancel_cid(symbol=sym, client_order_id=str(old_tp))
                tp_px = testnet_hooks.strategy_tp_price(
                    entry_price=float(pos["entry_price"]),
                    qty=float(new_q),
                    tp_usdt=float(pos["tp_usdt"]),
                )
                tp_res = testnet_hooks.place_tp_limit(
                    symbol=sym, qty=float(new_q), price=float(tp_px)
                )
                if tp_res.get("ok") and not tp_res.get("skipped"):
                    pos["testnet_tp_client_order_id"] = tp_res.get("client_order_id")
                    pos["testnet_tp_limit_price"] = tp_px
                    pos["testnet_tp_qty"] = float(new_q)
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet-D] TP_REHANG_ERROR {sym}: {exc}", flush=True)

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
            "testnet_client_order_id": pending.get("testnet_client_order_id"),
            "binance_order_id": pending.get("binance_order_id") or pending.get("testnet_exchange_order_id"),
            "binance_avg_price": fill_px,
            "binance_executed_qty": fill_qty,
            "fill_source": (
                "exchange_execution_confirmed"
                if pending.get("testnet_client_order_id")
                else "paper_simulation"
            ),
            "simulation_assumption": not bool(pending.get("testnet_client_order_id")),
        }
    )


def _testnet_on() -> bool:
    try:
        from . import testnet_hooks

        return bool(testnet_hooks.enabled())
    except Exception:
        return False


def _apply_entry_fill_delta(
    st: Account,
    pend: dict[str, Any],
    *,
    exec_qty: float,
    fill_px: float,
    via: str,
) -> Optional[dict[str, Any]]:
    """Apply newly observed entry fill qty (absolute executed on exchange or paper chunk)."""
    sym = str(pend["symbol"])
    prev = float(pend.get("testnet_entry_executed_qty") or pend.get("qty_filled") or 0)
    delta = float(exec_qty) - prev
    if delta <= 1e-12:
        return None
    px = float(fill_px) or float(pend.get("limit_px") or 0)
    if px <= 0:
        return None
    _open_or_add_short(st, pend, fill_qty=delta, fill_px=px)
    pend["qty_filled"] = float(pend.get("qty_filled") or 0) + delta
    pend["qty_remaining"] = max(0.0, float(pend.get("qty_target") or 0) - float(pend["qty_filled"]))
    pend["testnet_entry_executed_qty"] = float(exec_qty)
    row = {
        "action": "FILLED",
        "symbol": sym,
        "qty": delta,
        "price": px,
        "via": via,
        "exec_qty_abs": float(exec_qty),
    }
    _event(
        {
            "event": "ENTRY_FILLED",
            "symbol": sym,
            "qty": delta,
            "price": px,
            "via": via,
            "exec_qty_abs": float(exec_qty),
            "testnet_client_order_id": pend.get("testnet_client_order_id"),
            "binance_order_id": pend.get("binance_order_id") or pend.get("testnet_exchange_order_id"),
            "binance_avg_price": px,
            "binance_executed_qty": delta,
        }
    )
    return row


def _sync_testnet_entry_fill(st: Account, pend: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    """Poll Binance entry order. Returns (fill_event|None, exchange_status)."""
    from . import testnet_hooks

    cid = pend.get("testnet_client_order_id")
    sym = str(pend.get("symbol") or "")
    if not cid or not sym:
        return None, ""
    polled = testnet_hooks.poll_order(symbol=sym, client_order_id=str(cid))
    if not polled or polled.get("error"):
        return None, "POLL_ERROR"
    st_ex = str(polled.get("status") or "")
    exec_q = float(polled.get("executed_qty") or 0)
    avg_px = float(polled.get("avg_price") or 0) or float(pend.get("limit_px") or 0)
    if polled.get("exchange_order_id") is not None or polled.get("order_id") is not None:
        pend["binance_order_id"] = polled.get("exchange_order_id") or polled.get("order_id")
        pend["testnet_exchange_order_id"] = pend["binance_order_id"]
    pend["binance_avg_price"] = avg_px
    fill_ev = None
    if exec_q > float(pend.get("testnet_entry_executed_qty") or pend.get("qty_filled") or 0) + 1e-12:
        fill_ev = _apply_entry_fill_delta(
            st,
            pend,
            exec_qty=exec_q,
            fill_px=avg_px,
            via=f"BINANCE_ENTRY_{st_ex or 'PARTIAL'}",
        )
    return fill_ev, st_ex


def cancel_pending(st: Account, symbol: str, reason: str) -> Optional[dict[str, Any]]:
    pend = st.pending_limit_entries.get(symbol)
    if not pend:
        return None
    # Testnet: sync fill → cancel → requery; never treat poll fail as qty_filled=0
    if _testnet_on():
        try:
            from common.execution_sync import run_entry_cancel
            from . import testnet_hooks

            cid = pend.get("testnet_client_order_id")
            intended = float(pend.get("qty_target") or 0)
            recorded = float(pend.get("testnet_entry_executed_qty") or pend.get("qty_filled") or 0)

            def _poll():
                if not cid:
                    return {"error": "MISSING_CID"}
                return testnet_hooks.poll_order(symbol=symbol, client_order_id=str(cid))

            def _cancel():
                return testnet_hooks.cancel_pending(pend)

            def _apply(delta: float, px: float, via: str):
                return _apply_entry_fill_delta(
                    st,
                    pend,
                    exec_qty=float(recorded) + float(delta),  # cumulative absolute
                    fill_px=px or float(pend.get("limit_px") or 0),
                    via=via,
                )

            # _apply_entry_fill_delta expects absolute exec_qty — wrap properly
            def _apply_abs(delta: float, px: float, via: str):
                abs_exec = float(pend.get("testnet_entry_executed_qty") or pend.get("qty_filled") or 0) + float(delta)
                return _apply_entry_fill_delta(st, pend, exec_qty=abs_exec, fill_px=px or float(pend.get("limit_px") or 0), via=via)

            decision = run_entry_cancel(
                poll_fn=_poll,
                cancel_fn=_cancel,
                recorded_qty=recorded,
                intended_qty=intended,
                apply_fill_fn=_apply_abs if cid else None,
            )
            pend["cancel_lifecycle"] = decision.get("lifecycle")
            save_account(st)

            if float(pend.get("qty_remaining") or 0) <= 1e-12 or decision.get("lifecycle") == "FILLED":
                st.pending_limit_entries.pop(symbol, None)
                save_account(st)
                return {
                    "action": "FILLED_ON_CANCEL_RACE",
                    "symbol": symbol,
                    "reason": reason,
                    "lifecycle": decision.get("lifecycle"),
                    "fill_event": decision.get("fill_event"),
                    "executed_qty": decision.get("executed_qty"),
                }

            if not decision.get("allow_drop_pending"):
                row_hold = {
                    "event": "CANCEL_PENDING",
                    "action": decision.get("lifecycle") or "CANCEL_UNKNOWN",
                    "reason": reason,
                    "symbol": symbol,
                    "trade_id": pend.get("trade_id"),
                    "signal_id": pend.get("signal_id"),
                    "qty_filled": pend.get("qty_filled"),
                    "qty_remaining": pend.get("qty_remaining"),
                    "P": pend.get("P"),
                    "limit_px": pend.get("limit_px"),
                    "ttl_sec": pend.get("ttl_sec"),
                    "testnet_client_order_id": cid,
                    "lifecycle": decision.get("lifecycle"),
                    "detail": decision.get("reason"),
                    "executed_qty_observed": decision.get("executed_qty"),
                    "pending_retained": True,
                }
                _event(row_hold)
                save_account(st)
                return row_hold
        except Exception as exc:  # noqa: BLE001
            print(f"[testnet-D] CANCEL_PENDING_EXCHANGE_ERROR {symbol}: {exc}", flush=True)
            return {
                "event": "CANCEL_PENDING",
                "action": "CANCEL_UNKNOWN",
                "reason": reason,
                "symbol": symbol,
                "pending_retained": True,
                "error": str(exc),
            }

    pend = st.pending_limit_entries.pop(symbol, None)
    if not pend:
        save_account(st)
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
        "testnet_client_order_id": pend.get("testnet_client_order_id"),
        "lifecycle": "CANCELED",
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
    """Process entry limits. Testnet: fill only from Binance order poll (not local bid touch)."""
    eligibility_fail = eligibility_fail or {}
    events: list[dict[str, Any]] = []
    tn_on = _testnet_on()
    # Persist failure: cancel outstanding entry pendings; do not open new exposure without ledger.
    if getattr(st, "persist_halt_new_entries", False) or getattr(st, "persist_recovery_required", False):
        for sym in list(st.pending_limit_entries.keys()):
            ev = cancel_pending(st, sym, "PERSIST_HALT_CANCEL_PENDING")
            if ev:
                events.append(ev)
        return events
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

        # --- Testnet: sync exchange entry fills first ---
        if tn_on and pend.get("testnet_client_order_id"):
            fill_ev, st_ex = _sync_testnet_entry_fill(st, pend)
            if fill_ev:
                events.append(fill_ev)
            if st_ex == "FILLED" or float(pend.get("qty_remaining") or 0) <= 1e-12:
                st.pending_limit_entries.pop(sym, None)
                save_account(st)
                continue
            if st_ex in ("CANCELED", "EXPIRED", "REJECTED"):
                # already synced any executed qty above; drop local pending
                st.pending_limit_entries.pop(sym, None)
                _event(
                    {
                        "event": "CANCEL_PENDING",
                        "action": "CANCELLED",
                        "reason": f"EXCHANGE_{st_ex}",
                        "symbol": sym,
                        "testnet_client_order_id": pend.get("testnet_client_order_id"),
                    }
                )
                save_account(st)
                events.append({"action": "CANCELLED", "symbol": sym, "reason": f"EXCHANGE_{st_ex}"})
                continue

        # eligibility / liquidity cancel (before or after partial)
        if sym in eligibility_fail:
            if not tn_on:
                # paper race: local bid touch then cancel remainder
                if bid >= limit_px and now_ms >= int(pend.get("fill_allowed_after_ms") or 0):
                    fill_qty = float(pend["qty_remaining"])
                    ev = _apply_entry_fill_delta(
                        st, pend, exec_qty=float(pend.get("qty_filled") or 0) + fill_qty, fill_px=limit_px, via="PAPER"
                    )
                    if ev:
                        events.append(ev)
            # testnet: cancel_pending will poll+cancel exchange
            ev = cancel_pending(st, sym, eligibility_fail[sym])
            if ev:
                events.append(ev)
            continue

        # price cancel vs fixed P (not limit)
        if mid <= P * (1.0 - config.CANCEL_DROP_FROM_P_PCT):
            if not tn_on:
                if bid >= limit_px and now_ms >= int(pend.get("fill_allowed_after_ms") or 0):
                    fill_qty = float(pend["qty_remaining"])
                    ev = _apply_entry_fill_delta(
                        st,
                        pend,
                        exec_qty=float(pend.get("qty_filled") or 0) + fill_qty,
                        fill_px=limit_px,
                        via="PAPER_BEFORE_CANCEL",
                    )
                    if ev:
                        events.append(ev)
            ev = cancel_pending(st, sym, "PRICE_DROP_FROM_P")
            if ev:
                events.append(ev)
            continue

        # no time TTL — explicitly do nothing for age
        if now_ms < int(pend.get("fill_allowed_after_ms") or 0):
            continue

        if tn_on:
            # Waiting on Binance; do not fabricate fills from local bid
            continue

        if bid >= limit_px:
            # paper model: touch → fill remaining at limit
            fill_qty = float(pend["qty_remaining"])
            ev = _apply_entry_fill_delta(
                st,
                pend,
                exec_qty=float(pend.get("qty_filled") or 0) + fill_qty,
                fill_px=limit_px,
                via="PAPER",
            )
            if ev:
                events.append(ev)
            st.pending_limit_entries.pop(sym, None)
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
    exchange_already_done: bool = False,
) -> Optional[dict[str, Any]]:
    from common.execution_sync import find_settlement_pending_for_symbol

    pos = st.positions.get(symbol)
    if not pos:
        tid_sp, sp = find_settlement_pending_for_symbol(
            getattr(st, "settlement_pending", None), symbol
        )
        if sp and sp.get("close_resend_blocked"):
            return {
                "ok": False,
                "block_reason": "SETTLEMENT_PENDING_NO_CLOSE_RESEND",
                "settlement_status": sp.get("settlement_status"),
                "local_trade_id": tid_sp,
                "symbol": symbol,
            }
        return None
    # cancel any remainder entry first
    if symbol in st.pending_limit_entries:
        cancel_pending(st, symbol, f"EXIT_{reason}_CANCEL_REMAINDER")
    qty = float(pos["qty_remaining"])
    if qty <= 0:
        st.positions.pop(symbol, None)
        save_account(st)
        return None
    from common.execution_sync import (
        build_pending_close_state,
        estimated_pnl_fields,
        evidence_fields,
        place_acceptance_vs_fill,
        validate_exchange_close_evidence,
    )

    price_status = "known"
    # Testnet: cancel resting TP; place close; only ledger confirmed executed qty
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            if pos.get("testnet_close_client_order_id") and not exchange_already_done:
                print(f"[testnet-D] CLOSE already pending {symbol}", flush=True)
                return {
                    "ok": False,
                    "action": "CLOSE_ALREADY_PENDING",
                    "block_reason": "CLOSE_ALREADY_PENDING",
                    "client_order_id": pos.get("testnet_close_client_order_id"),
                    "symbol": symbol,
                }
            tp_cid = pos.get("testnet_tp_client_order_id")
            if tp_cid and not exchange_already_done:
                testnet_hooks.cancel_cid(symbol=symbol, client_order_id=str(tp_cid))
            if not exchange_already_done:
                tn = testnet_hooks.close_position(symbol=symbol, side="SHORT", qty=float(qty))
                info = place_acceptance_vs_fill(tn)
                if not info.get("accepted") and not info.get("skipped"):
                    print(f"[testnet-D] refuse local CLOSE: {info.get('error') or tn.get('error')}", flush=True)
                    return {
                        "ok": False,
                        "block_reason": f"TESTNET_CLOSE_FAIL:{info.get('error') or tn.get('error')}",
                        "symbol": symbol,
                    }
                if str(info.get("status") or "") == "ALREADY_FLAT":
                    return {
                        "ok": False,
                        "block_reason": "EXCHANGE_ALREADY_FLAT_USE_RECONCILE",
                        "action": "NEED_RECONCILE_FLAT",
                        "client_order_id": info.get("client_order_id"),
                        "symbol": symbol,
                    }
                elif not info.get("has_confirmed_fill"):
                    pos.update(
                        build_pending_close_state(
                            client_order_id=info.get("client_order_id"),
                            exchange_order_id=info.get("exchange_order_id"),
                            qty_requested=float(qty),
                            reason=reason,
                            status=str(info.get("status") or "ACCEPTED_UNFILLED"),
                        )
                    )
                    save_account(st)
                    print(
                        f"[testnet-D] CLOSE accepted awaiting fill {symbol} cid={info.get('client_order_id')}",
                        flush=True,
                    )
                    return {
                        "ok": False,
                        "action": "CLOSE_ACCEPTED_AWAITING_FILL",
                        "block_reason": "CLOSE_ACCEPTED_AWAITING_FILL",
                        "client_order_id": info.get("client_order_id"),
                        "status": info.get("status"),
                        "symbol": symbol,
                    }
                else:
                    qty = min(qty, float(info["confirmed_executed_qty"]))
                    if info.get("avg_price"):
                        exit_px = float(info["avg_price"])
                    else:
                        price_status = "unknown"
                    exchange_already_done = True
                    depth_meta = {
                        **(depth_meta or {}),
                        "via": "BINANCE_CLOSE_FILL",
                        "testnet_client_order_id": info.get("client_order_id"),
                        "binance_order_id": info.get("exchange_order_id"),
                        "binance_executed_qty": info.get("confirmed_executed_qty"),
                        "binance_avg_price": info.get("avg_price"),
                        "price_status": price_status,
                        "fill_source": "exchange_execution_confirmed",
                    }
            else:
                v = validate_exchange_close_evidence(
                    depth_meta, expect_symbol=symbol, expect_position_side="SHORT"
                )
                if not v.get("ok"):
                    print(f"[testnet-D] refuse CLOSE: evidence {v.get('reason')}", flush=True)
                    return {
                        "ok": False,
                        "block_reason": f"EXCHANGE_ALREADY_DONE_WITHOUT_EVIDENCE:{v.get('reason')}",
                        "symbol": symbol,
                    }
                if not v.get("allows_fill_accounting"):
                    return {
                        "ok": False,
                        "block_reason": "FLAT_EVIDENCE_NOT_FILL",
                        "action": "NEED_RECONCILE_FLAT",
                        "symbol": symbol,
                    }
                price_status = str((depth_meta or {}).get("price_status") or price_status)
    except Exception as exc:  # noqa: BLE001
        print(f"[testnet-D] close hook error: {exc}", flush=True)
        return None
    if qty <= 1e-12:
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
    dm = depth_meta or {}
    ev = evidence_fields(
        source="exchange_execution_confirmed" if exchange_already_done else "paper_simulation",
        client_order_id=dm.get("testnet_client_order_id"),
        exchange_order_id=dm.get("binance_order_id"),
        avg_price=dm.get("binance_avg_price"),
        executed_qty=dm.get("binance_executed_qty") or qty,
        observed_at=now_iso(),
        extra=estimated_pnl_fields(fee_u=fee, net_u=net, gross_u=gross, price_status=price_status),
    )
    row = {
        "event": "CLOSE",
        "trade_id": pos.get("trade_id"),
        "signal_id": pos.get("signal_id"),
        "symbol": symbol,
        "side": "SHORT",
        "qty": qty,
        "price": exit_px,
        "entry_price": entry,
        "funding_u": funding,
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
        **ev,
        "funding_data_gap": True,
        "depth_meta": dm,
        "profile_id": config.PROFILE_ID,
        "strategy_version": config.STRATEGY_VERSION,
        "report_note": "gross trigger vs net after fee/slip/funding shown separately",
        "testnet_client_order_id": dm.get("testnet_client_order_id")
        or pos.get("testnet_tp_client_order_id")
        or pos.get("testnet_sl_client_order_id")
        or pos.get("testnet_entry_client_order_id"),
        "binance_order_id": dm.get("binance_order_id"),
        "binance_avg_price": dm.get("binance_avg_price", exit_px),
        "binance_executed_qty": dm.get("binance_executed_qty", qty),
        "via": dm.get("via"),
    }
    # preserve fill_source / simulation_assumption from evidence_fields (**ev)
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


def update_position_marks(st: Account, symbol: str, book: dict[str, Any], *, persist: bool = True) -> None:
    """Update in-memory MTM/roll. When persist=False or halt, memory-only (no claim of durable ledger)."""
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
    if not persist or getattr(st, "persist_halt_new_entries", False):
        return
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


def reconcile_exchange_flat(
    st: Account,
    symbol: str,
    *,
    position_amt_observed: float = 0.0,
    evidence_source: str = "positionRisk",
    client_order_id: Any = None,
    observed_at: float | None = None,
    position_side: str = "SHORT",
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

    pos = st.positions.get(symbol)
    if not pos:
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
    flat_ev = build_position_flat_evidence(
        symbol=symbol,
        position_side=position_side,
        position_amt_observed=position_amt_observed,
        evidence_source=evidence_source,
        observed_at=observed_at,
        env_id=env_id,
        account_id=account_id or config.PROFILE_ID,
        client_order_id=client_order_id,
    )
    v = validate_position_flat_evidence(
        flat_ev,
        expect_symbol=symbol,
        expect_position_side=position_side,
        expect_env=env_id,
        expect_account=account_id,
        max_age_sec=max_age_sec,
    )
    if not v.get("ok"):
        return {"ok": False, "block_reason": f"FLAT_EVIDENCE_REJECTED:{v.get('reason')}", **v}
    before_qty = float(pos.get("qty_remaining") or 0)
    before_entry = float(pos.get("entry_price") or 0)
    margin = float(pos.get("margin_u") or 0)
    trade_id = pos.get("trade_id")
    st.cash_u += margin
    st.positions.pop(symbol, None)
    st.pending_limit_entries.pop(symbol, None)
    if not hasattr(st, "settlement_pending") or st.settlement_pending is None:
        st.settlement_pending = {}
    if trade_id:
        st.settlement_pending[str(trade_id)] = build_settlement_pending_record(
            local_trade_id=trade_id,
            symbol=symbol,
            side="SHORT",
            position_side=position_side,
            entry_price=before_entry,
            qty_to_settle=before_qty,
            margin_released_u=margin,
            flat_evidence=flat_ev,
            reason=reason,
            entry_fee_estimated=pos.get("entry_fee_estimated"),
            entry_fee_status=str(pos.get("entry_fee_status") or "estimated_schedule_rate"),
            entry_client_order_id=pos.get("testnet_entry_client_order_id") or pos.get("testnet_client_order_id"),
            entry_exchange_order_id=pos.get("testnet_entry_exchange_order_id") or pos.get("testnet_exchange_order_id"),
            exit_client_order_id=pos.get("testnet_close_client_order_id"),
            exit_exchange_order_id=pos.get("testnet_close_exchange_order_id"),
            known_order_ids=[
                str(x)
                for x in (
                    pos.get("testnet_entry_exchange_order_id"),
                    pos.get("testnet_exchange_order_id"),
                    pos.get("testnet_close_exchange_order_id"),
                )
                if x is not None
            ],
            known_client_order_ids=[
                str(x)
                for x in (
                    pos.get("testnet_entry_client_order_id"),
                    pos.get("testnet_client_order_id"),
                    pos.get("testnet_close_client_order_id"),
                )
                if x
            ],
        )
    row = reconcile_flat_event_row(
        strategy="D",
        symbol=symbol,
        trade_id=trade_id,
        side="SHORT",
        before_qty=before_qty,
        before_entry=before_entry,
        margin_released=margin,
        flat_evidence=flat_ev,
        reason=reason,
    )
    row["settlement_pending_key"] = str(trade_id) if trade_id else None
    row["cash_equity_incomplete"] = True
    _event(row)
    _fill({**row, "event": "RECONCILE_CLEAR"})
    save_account(st)
    return {"ok": True, "action": "EXCHANGE_FLAT_RECONCILE", **row}


def _recover_settlement_journal(st: Account) -> dict[str, Any]:
    from common.settlement_apply import recover_account_settlement_journal

    if not isinstance(getattr(st, "settlement_journal", None), list):
        st.settlement_journal = list(getattr(st, "settlement_journal", None) or [])
    return recover_account_settlement_journal(
        st,
        append_fill_fn=_fill,
        save_fn=save_account,
        fills_path=config.paths()["fills"],
    )


def apply_pending_settlement_fills(
    st: Account,
    *,
    local_trade_id: str,
    trades: list[dict[str, Any]],
    entry_attributed: bool | None = None,
    exit_attributed: bool | None = None,
    query_incomplete: bool = False,
    unattributed_remaining: bool = False,
) -> dict[str, Any]:
    """D portfolio path — PREPARED/APPLIED/FILL_APPENDED journal (parity with A)."""
    from common.settlement_apply import journal_apply_exit_fills

    if not isinstance(getattr(st, "settlement_journal", None), list):
        st.settlement_journal = []
    return journal_apply_exit_fills(
        st,
        local_trade_id=local_trade_id,
        trades=trades,
        save_fn=save_account,
        append_fill_fn=_fill,
        fee_fn=_fee,
        default_side="SHORT",
        entry_attributed=entry_attributed,
        exit_attributed=exit_attributed,
        query_incomplete=query_incomplete,
        unattributed_remaining=unattributed_remaining,
        strategy_meta={"profile_id": config.PROFILE_ID, "strategy_version": config.STRATEGY_VERSION},
        fills_path=config.paths()["fills"],
        recover_fn=_recover_settlement_journal,
    )


def adjust_entry_fee_to_exchange(
    st: Account,
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
        append_fill_fn=_fill,
        fills_path=config.paths()["fills"],
    )



def sync_settlement_pending(
    st: Account,
    *,
    local_trade_id: str | None = None,
    fetch_user_trades_fn: Any = None,
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

            fetch = testnet_hooks.fetch_user_trades
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
                    "note": "no blind close resend",
                }
            )
            continue
        if fetch is None:
            results.append({"ok": False, "local_trade_id": tid, "block_reason": "NO_FETCH_FN"})
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
        fee_adjs = [
            adjust_entry_fee_to_exchange(
                st,
                local_trade_id=tid,
                exchange_commission=et.get("commission"),
                commission_asset=et.get("commissionAsset"),
                exchange_trade_id=et.get("id"),
                trade_qty=float(et.get("qty") or 0) or None,
            )
            for et in got["entry_trades"]
        ]
        app = apply_pending_settlement_fills(
            st,
            local_trade_id=tid,
            trades=got["exit_trades"],
            entry_attributed=bool(got["entry_trades"]),
            exit_attributed=bool(got["exit_trades"]),
            query_incomplete=bool(got.get("query_incomplete")),
            unattributed_remaining=bool(got.get("unattributed")),
        )
        results.append({"ok": app.get("ok"), "local_trade_id": tid, "fee_adjs": fee_adjs, "apply": app, "sync": got})
    return {"ok": all(r.get("ok") for r in results) if results else True, "results": results}


def _sync_pending_close_order(st: Account, symbol: str, book: dict[str, Any]) -> Optional[dict[str, Any]]:
    from common.execution_sync import clear_pending_close_fields, observe_pending_close

    pos = st.positions.get(symbol)
    if not isinstance(pos, dict) or not pos.get("testnet_close_client_order_id"):
        return None
    try:
        from . import testnet_hooks

        if not testnet_hooks.enabled():
            return None
        polled = testnet_hooks.poll_order(
            symbol=symbol, client_order_id=str(pos["testnet_close_client_order_id"])
        )
    except Exception as exc:  # noqa: BLE001
        return {"action": "CLOSE_POLL_UNKNOWN", "symbol": symbol, "error": str(exc)}
    obs = observe_pending_close(pos, polled)
    for k, v in (obs.get("updates") or {}).items():
        pos[k] = v
    save_account(st)
    if obs.get("action") == "HOLD_UNKNOWN":
        return {"action": "CLOSE_POLL_UNKNOWN", "symbol": symbol}
    if obs.get("action") == "RELEASE_AWAITING":
        clear_pending_close_fields(pos)
        save_account(st)
        return {"action": "CLOSE_AWAITING_RELEASED", "status": obs.get("status")}
    if obs.get("action") == "APPLY_DELTA":
        delta = float(obs.get("delta_qty") or 0)
        px = obs.get("delta_avg")
        price_status = str(obs.get("price_status") or "known")
        if px is None:
            px = cover_exit_price(float(book.get("ask") or 0)) or float(pos.get("entry_price") or 0)
            price_status = "estimated_local_mark_fallback"
        # D close_position flats full remaining — for delta, temporarily reduce requested by
        # calling with exchange evidence for the delta only via a synthetic path:
        # reduce qty_remaining after partial by using close with evidence qty=delta but
        # close_position always uses full qty_remaining. Apply accounting manually for delta.
        # Prefer: set qty to delta by closing only delta through a local reduce.
        rem = float(pos.get("qty_remaining") or 0)
        close_q = min(delta, rem)
        if close_q <= 1e-12:
            return {"action": "CLOSE_PENDING_NO_DELTA"}
        # Shrink position qty first so close_position closes only remaining slice... 
        # Instead invoke close with full rem only when cum covers rem; else patch qty.
        if close_q + 1e-12 < rem:
            # partial: adjust qty_remaining after building a CLOSE_PARTIAL fill
            entry = float(pos["entry_price"])
            gross = _gross_short(entry, float(px), close_q)
            fee = _fee(close_q * float(px))
            net = gross - fee
            st.cash_u += net
            st.realized_net_u += net
            st.realized_gross_u += gross
            st.realized_fees_u += fee
            pos["qty_remaining"] = rem - close_q
            pos["testnet_close_qty_applied"] = float(pos.get("testnet_close_qty_applied") or 0) + close_q
            from common.execution_sync import estimated_pnl_fields, evidence_fields

            _fill(
                {
                    "event": "CLOSE_PARTIAL",
                    "trade_id": pos.get("trade_id"),
                    "symbol": symbol,
                    "qty": close_q,
                    "price": float(px),
                    "exit_reason": str(pos.get("testnet_close_reason") or "CLOSE_PENDING_FILL"),
                    **evidence_fields(
                        source="exchange_execution_confirmed",
                        client_order_id=pos.get("testnet_close_client_order_id"),
                        exchange_order_id=pos.get("testnet_close_exchange_order_id"),
                        avg_price=obs.get("delta_avg"),  # None when unknown — not mark
                        executed_qty=close_q,
                        observed_at=now_iso(),
                        extra={
                            **estimated_pnl_fields(
                                fee_u=fee, net_u=net, gross_u=gross, price_status=price_status
                            ),
                            "qty_confirmation": "exchange_executed_qty",
                            "accounting_price_role": (
                                "local_mark_fallback"
                                if str(price_status).startswith(("estimated", "unknown"))
                                else "exchange_slice_avg"
                            ),
                        },
                    ),
                }
            )
            save_account(st)
            return {"action": "CLOSE_PENDING_DELTA", "qty": close_q, "ok": True}
        return close_position(
            st,
            symbol,
            exit_px=float(px),
            reason=str(pos.get("testnet_close_reason") or "CLOSE_PENDING_FILL"),
            depth_meta={
                "via": "BINANCE_CLOSE_POLL_DELTA",
                "testnet_client_order_id": pos.get("testnet_close_client_order_id"),
                "binance_order_id": pos.get("testnet_close_exchange_order_id"),
                "binance_avg_price": obs.get("delta_avg"),
                "binance_executed_qty": close_q,
                "price_status": price_status,
            },
            exchange_already_done=True,
        )
    if obs.get("action") == "CLEAR_TRACKING":
        clear_pending_close_fields(pos)
        save_account(st)
        return {"action": "CLOSE_TRACKING_CLEARED"}
    return {"action": "CLOSE_HOLD", "status": obs.get("status")}


def manage_position(st: Account, symbol: str, book: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Compat helper for tests: trigger + fallback ask+slip exit (no async depth walk)."""
    update_position_marks(st, symbol, book)
    pos = st.positions.get(symbol)
    # Pending close sync first
    try:
        from . import testnet_hooks as _th

        if pos and _th.enabled() and pos.get("testnet_close_client_order_id"):
            synced = _sync_pending_close_order(st, symbol, book)
            if synced:
                return synced
            pos = st.positions.get(symbol)
            if pos and pos.get("testnet_close_client_order_id"):
                return {"action": "CLOSE_AWAITING", "symbol": symbol}
    except Exception:
        pass
    # Testnet: poll reduceOnly TP limit fill first
    try:
        from . import testnet_hooks

        if pos and testnet_hooks.enabled():
            tp_cid = pos.get("testnet_tp_client_order_id")
            if tp_cid:
                polled = testnet_hooks.poll_order(symbol=symbol, client_order_id=str(tp_cid))
                if polled and not polled.get("error"):
                    st_ex = str(polled.get("status") or "")
                    if st_ex == "FILLED":
                        fill_px = float(polled.get("avg_price") or 0) or float(
                            pos.get("testnet_tp_limit_price") or 0
                        )
                        fill_q = float(polled.get("executed_qty") or pos.get("qty_remaining") or 0)
                        return close_position(
                            st,
                            symbol,
                            exit_px=fill_px,
                            reason="TP",
                            triggers=["TP"],
                            depth_meta={
                                "via": "BINANCE_TP_LIMIT",
                                "testnet_client_order_id": tp_cid,
                                "binance_order_id": polled.get("exchange_order_id") or polled.get("order_id"),
                                "binance_avg_price": fill_px,
                                "binance_executed_qty": fill_q,
                            },
                            exchange_already_done=True,
                        )
                    if st_ex in ("CANCELED", "EXPIRED", "REJECTED"):
                        pos.pop("testnet_tp_client_order_id", None)
                        save_account(st)
            elif float(pos.get("qty_remaining") or 0) > 0:
                # missing TP limit — backfill
                tp_px = testnet_hooks.strategy_tp_price(
                    entry_price=float(pos["entry_price"]),
                    qty=float(pos["qty_remaining"]),
                    tp_usdt=float(pos.get("tp_usdt") or config.TP_USDT),
                )
                tp_res = testnet_hooks.place_tp_limit(
                    symbol=symbol,
                    qty=float(pos["qty_remaining"]),
                    price=float(tp_px),
                )
                if tp_res.get("ok") and tp_res.get("client_order_id"):
                    pos["testnet_tp_client_order_id"] = tp_res["client_order_id"]
                    pos["testnet_tp_limit_price"] = tp_px
                    save_account(st)
    except Exception as exc:  # noqa: BLE001
        print(f"[testnet-D] TP_POLL_ERROR {symbol}: {exc}", flush=True)

    # Testnet: detect exchange flatten (SL STOP_MARKET); never mark-race TP/SL market
    try:
        from . import testnet_hooks
        from common.testnet_bridge import get_adapter as _ga

        if pos and testnet_hooks.enabled():
            try:
                amt = float(_ga("D").position_amt(symbol))
            except Exception:
                amt = None
            if amt is not None and abs(amt) < 1e-12 and float(pos.get("qty_remaining") or 0) > 1e-12:
                return reconcile_exchange_flat(
                    st,
                    symbol,
                    position_amt_observed=float(amt),
                    evidence_source="positionRisk",
                    client_order_id=pos.get("testnet_sl_client_order_id"),
                    position_side="SHORT",
                    reason="EXCHANGE_FLAT_SL_OR_TP",
                )
            has_prot = bool(pos.get("testnet_tp_client_order_id") or pos.get("testnet_sl_client_order_id"))
            sl_failed = bool(pos.get("testnet_sl_place_failed"))
            if has_prot and not sl_failed:
                return None  # wait for Binance TP/SL only
    except Exception:
        pass

    triggers = check_exit_triggers(st, symbol, book)
    if triggers is None:
        age = now_ts() - float(book.get("fetched_at") or 0)
        if age > config.QUOTE_MAX_AGE_SEC:
            return {"action": "STALE_HOLD", "symbol": symbol}
        return None
    if not triggers:
        return None
    # If only TP and exchange TP limit is resting, wait for limit fill (don't market-close)
    try:
        from . import testnet_hooks

        if testnet_hooks.enabled():
            p = st.positions.get(symbol) or {}
            if p.get("testnet_tp_client_order_id") and "TP" in triggers and "SL" not in triggers:
                return None
            if p.get("testnet_sl_client_order_id") and not p.get("testnet_sl_place_failed"):
                # SL live on exchange — ignore mark SL/VOL market race
                return None
            # emergency: SL place failed → allow mark path
    except Exception:
        pass
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
