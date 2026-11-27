"""Offline strategy replay — reuses E detect_down_signal + paper queue/process/manage.

No alternate strategy. No paper_main loop. Fills tagged simulated; fill_claim=false.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
E_CODE = REPO / "output" / "e_trend_v2" / "code"

EXPECTED_SOURCE_SHA = {
    "signals.py": "0054bb438c2a48c0a8e125d09c7380fb774884196f793671b644406881803354",
    "portfolio.py": "95df4d8ff90586b132e6542eae9b8e37feb0dc19c65353e409dbdce469d9de48",
    "config.py": "b0798736a39304db179868f541082cb2fc7b20cf31d8203b91a2ff8d100b1ce0",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_strategy_sources() -> dict[str, Any]:
    base = E_CODE / "e_top10_1m_down_v1"
    out: dict[str, Any] = {"ok": True, "files": {}}
    for name, exp in EXPECTED_SOURCE_SHA.items():
        p = base / name
        got = sha256_file(p) if p.is_file() else None
        match = got == exp
        out["files"][name] = {
            "path": str(p),
            "expected": exp,
            "actual": got,
            "match": match,
        }
        if not match:
            out["ok"] = False
    return out


def _ensure_import_path() -> None:
    if str(E_CODE) not in sys.path:
        sys.path.insert(0, str(E_CODE))
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _fmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class ReplayClock:
    now_ts: float
    now_ms: int

    def set_ms(self, ms: int) -> None:
        self.now_ms = int(ms)
        self.now_ts = self.now_ms / 1000.0


@dataclass
class StrategyReplayInputs:
    runtime_root: Path
    bars: list[dict[str, Any]]
    symbol: str = "SYNTHUSDT"
    rank: int = 1
    pct_24h: float = -3.0
    joined_rank_at_ms: int = 1795765700000
    last_processed_confirm_ot: int | None = None
    filters: dict[str, Any] = field(
        default_factory=lambda: {
            "tickSize": 0.0001,
            "stepSize": 0.001,
            "minQty": 0.001,
            "minNotional": 5.0,
        }
    )
    book_entry: dict[str, Any] | None = None
    book_exit: dict[str, Any] | None = None
    clock_ms: int = 1795765860000
    process_now_ms: int | None = None  # when to call process_pending; default=clock_ms
    same_bar_ambiguous: bool = False
    funding: Decimal | None = Decimal("0")  # None = missing cost
    costs_complete: bool = True  # False → force not_computable even if funding set
    strip_forming: bool = True
    cutoff_ms: int | None = None
    arm: str = "tp05"
    cash_u: float = 10_000.0
    run_exit: bool = True


def prepare_bars(
    bars: list[dict[str, Any]],
    *,
    cutoff_ms: int | None = None,
    strip_forming: bool = False,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows = list(bars)
    if cutoff_ms is not None:
        rows = [b for b in rows if int(b["open_time"]) < int(cutoff_ms)]
    if strip_forming and now_ms is not None:
        rows = [b for b in rows if int(b["close_time"]) < int(now_ms)]
    return rows


def _bar_objs(bars: list[dict[str, Any]]) -> list[Any]:
    from e_top10_1m_down_v1.signals import Bar

    return [
        Bar(
            open_time=int(b["open_time"]),
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            close_time=int(b["close_time"]),
        )
        for b in bars
    ]


def run_strategy_replay(inp: StrategyReplayInputs) -> dict[str, Any]:
    src = verify_strategy_sources()
    base: dict[str, Any] = {
        "label": "e_functional_strategy_01",
        "fill_claim": False,
        "simulated": True,
        "source_identity": src,
        "counts": {"signals": 0, "pending_queued": 0, "open_fills": 0, "exit_events": 0},
        "events": [],
        "state_transitions": [],
        "simulated_pnl": None,
        "full_net_status": None,
        "costs": None,
        "blocked": False,
        "blocked_reason": None,
        "status": None,
    }
    if not src["ok"]:
        base.update(
            {
                "status": "blocked",
                "blocked": True,
                "blocked_reason": "STRATEGY_SOURCE_SHA_MISMATCH",
            }
        )
        return base

    if inp.same_bar_ambiguous:
        base.update(
            {
                "status": "blocked",
                "blocked": True,
                "blocked_reason": "SAME_BAR_ORDER_AMBIGUOUS",
                "simulated_pnl": None,
            }
        )
        return base

    saved_env = {
        k: os.environ.pop(k)
        for k in list(os.environ)
        if k.startswith("AUTOMAX_E_")
        or k in ("ENABLE_BINANCE_TESTNET_ORDER", "AUTOMAX_ORDER_LEDGER_DIR")
    }
    try:
        _ensure_import_path()
        from e_top10_1m_down_v1 import config, portfolio
        from e_top10_1m_down_v1.signals import SymbolMonitor, detect_down_signal

        inp.runtime_root.mkdir(parents=True, exist_ok=True)
        ledger_dir = inp.runtime_root / "order_fill_ledger"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        os.environ["AUTOMAX_ORDER_LEDGER_DIR"] = str(ledger_dir.resolve())
        config.set_runtime_root(inp.runtime_root)

        clock = ReplayClock(now_ts=inp.clock_ms / 1000.0, now_ms=inp.clock_ms)
        process_ms = int(inp.process_now_ms if inp.process_now_ms is not None else inp.clock_ms)

        base["effective_settings"] = {
            "STRATEGY_VERSION": config.STRATEGY_VERSION,
            "POSITION_SIDE": config.POSITION_SIDE,
            "LIMIT_ENTRY_OFFSET_PCT": config.LIMIT_ENTRY_OFFSET_PCT,
            "MAKER_FEE_RATE": config.MAKER_FEE_RATE,
            "TAKER_FEE_RATE": config.TAKER_FEE_RATE,
            "INITIAL_STOP_ADVERSE_PCT": config.INITIAL_STOP_ADVERSE_PCT,
            "PARTIAL_TP_FRACTION": config.PARTIAL_TP_FRACTION,
            "TRAIL_REMAINDER": config.TRAIL_REMAINDER,
            "PREPLACE_TP_ORDERS": config.PREPLACE_TP_ORDERS,
            "MAX_SPREAD": config.MAX_SPREAD,
            "QUOTE_MAX_AGE_SEC": config.QUOTE_MAX_AGE_SEC,
            "NOTIONAL_USDT": config.NOTIONAL_USDT,
            "MARGIN_PER_TRADE_USDT": config.MARGIN_PER_TRADE_USDT,
            "testnet_orders_enabled": False,
        }

        bars_use = prepare_bars(
            inp.bars,
            cutoff_ms=inp.cutoff_ms,
            strip_forming=inp.strip_forming,
            now_ms=clock.now_ms if inp.strip_forming else None,
        )
        base["bars_used_n"] = len(bars_use)
        base["bars_open_times"] = [int(b["open_time"]) for b in bars_use]

        if any(
            b.get("symbol") and str(b["symbol"]).upper() != inp.symbol.upper()
            for b in bars_use
        ):
            base.update(
                {
                    "status": "blocked",
                    "blocked": True,
                    "blocked_reason": "WRONG_SYMBOL",
                    "simulated_pnl": None,
                }
            )
            return base

        if len(bars_use) < 2:
            base.update(
                {
                    "status": "blocked",
                    "blocked": True,
                    "blocked_reason": "BARS_INSUFFICIENT_OR_MISSING",
                    "simulated_pnl": None,
                }
            )
            return base

        mon = SymbolMonitor(
            symbol=inp.symbol,
            joined_rank_at_ms=inp.joined_rank_at_ms,
            last_processed_confirm_ot=inp.last_processed_confirm_ot,
        )

        def _now_ts() -> float:
            return float(clock.now_ts)

        with mock.patch.object(portfolio, "now_ts", _now_ts), mock.patch(
            "e_top10_1m_down_v1.util.now_ts", _now_ts
        ):
            mon2, signal = detect_down_signal(
                mon, _bar_objs(bars_use), pct_24h=inp.pct_24h, rank=inp.rank
            )
            base["monitor_after"] = {
                "last_processed_confirm_ot": mon2.last_processed_confirm_ot
            }
            if signal is None:
                base["status"] = "no_signal"
                base["signal"] = None
                return base

            base["counts"]["signals"] = 1
            base["signal"] = {
                "signal_id": signal.get("signal_id"),
                "S": signal.get("S"),
                "fill_allowed_after_ms": signal.get("fill_allowed_after_ms"),
                "confirm_bar_ot": signal.get("confirm_bar_ot"),
                "confirm_close_time": signal.get("confirm_close_time"),
            }
            base["events"].append({"stage": "signal", "simulated": True, **base["signal"]})
            base["state_transitions"].append("FLAT→SIGNAL")

            st = portfolio.ArmAccount(
                arm=inp.arm,
                profile_id="strategy_replay_iso",
                run_id="strategy_replay",
                strategy_version=config.STRATEGY_VERSION,
                cash_u=float(inp.cash_u),
            )
            portfolio.save_arm(st)

            q = portfolio.queue_entry_limit(st, signal=signal, filters=inp.filters)
            base["queue"] = {
                "ok": q.get("ok"),
                "block_reason": q.get("block_reason"),
                "limit_price": q.get("limit_price"),
                "qty": q.get("qty"),
                "fill_allowed_after_ms": q.get("fill_allowed_after_ms"),
            }
            if not q.get("ok"):
                base["status"] = "queue_blocked"
                base["blocked"] = True
                base["blocked_reason"] = q.get("block_reason")
                return base
            base["counts"]["pending_queued"] = 1
            base["state_transitions"].append("SIGNAL→PENDING")
            base["events"].append({"stage": "pending", "simulated": True, **base["queue"]})

            book_e = dict(
                inp.book_entry
                or {
                    "bid": 99.95,
                    "ask": 100.0,
                    "mid": 99.975,
                    "spread": (100.0 - 99.95) / 99.975,
                }
            )
            clock.set_ms(process_ms)
            book_e["fetched_at"] = float(clock.now_ts)

            fill_evs = portfolio.process_pending(
                st, {inp.symbol: book_e}, now_ms=process_ms
            )
            open_fills = [
                e for e in fill_evs if e.get("action") == "FILLED" and e.get("ok")
            ]
            base["process_pending_actions"] = [e.get("action") for e in fill_evs]
            base["counts"]["open_fills"] = len(open_fills)
            base["open_fill_events"] = [
                {
                    "action": e.get("action"),
                    "ok": e.get("ok"),
                    "fill_px": e.get("fill_px"),
                    "price": e.get("price"),
                    "qty": e.get("qty"),
                    "trade_id": e.get("trade_id"),
                    "via": e.get("via"),
                    "simulated": True,
                    "fill_claim": False,
                }
                for e in open_fills
            ]
            if open_fills:
                base["state_transitions"].append("PENDING→OPEN")
                base["events"].append({"stage": "open", **base["open_fill_events"][0]})
                base["status"] = "opened"
            else:
                base["status"] = "pending_not_filled"
                return base

            if not inp.run_exit or inp.book_exit is None:
                return base

            # exit via paper manage_position (mark path)
            book_x = dict(inp.book_exit)
            # advance clock slightly so quote age ok
            clock.set_ms(process_ms + 60_000)
            book_x["fetched_at"] = float(clock.now_ts)
            exit_evs = portfolio.manage_position(st, inp.symbol, book_x)
            base["counts"]["exit_events"] = len(exit_evs)
            base["exit_events"] = [
                {
                    "action": e.get("action"),
                    "ok": e.get("ok"),
                    "mark": e.get("mark"),
                    "qty": e.get("qty"),
                    "exit_price": e.get("exit_price") or e.get("price") or e.get("mark"),
                    "net": e.get("net"),
                    "remaining": e.get("remaining"),
                    "fee_source": e.get("fee_source"),
                    "pnl_source": e.get("pnl_source"),
                    "simulated": True,
                    "fill_claim": False,
                }
                for e in exit_evs
            ]
            if exit_evs:
                base["state_transitions"].append("OPEN→EXIT")
                base["events"].extend(
                    [{"stage": "exit", **x} for x in base["exit_events"]]
                )
                base["status"] = "closed" if inp.symbol not in st.positions else "partial_or_open"

            # costs from strategy schedule + optional funding
            entry_px = Decimal(str(open_fills[0].get("fill_px") or open_fills[0].get("price")))
            qty_open = Decimal(str(open_fills[0].get("qty")))
            entry_fee_r = Decimal(str(config.TAKER_FEE_RATE))
            exit_fee_r = Decimal(str(config.TAKER_FEE_RATE))
            funding = inp.funding

            if (not inp.costs_complete) or funding is None:
                base["full_net_status"] = "not_computable"
                base["simulated_pnl"] = None
                base["costs"] = {
                    "entry_fee_rate": str(entry_fee_r),
                    "exit_fee_rate": str(exit_fee_r),
                    "funding": None if funding is None else _fmt(funding),
                    "note": "缺成本不得默認為零",
                }
                return base

            net_sum = Decimal("0")
            for e in exit_evs:
                if e.get("net") is not None:
                    net_sum += Decimal(str(e["net"]))
            exit_px = None
            if exit_evs and inp.symbol not in st.positions:
                exit_px = Decimal(
                    str(
                        exit_evs[-1].get("exit_price")
                        or exit_evs[-1].get("price")
                        or exit_evs[-1].get("mark")
                    )
                )
            full_flat = (
                exit_px is not None
                and len(exit_evs) == 1
                and float(exit_evs[0].get("qty") or 0) >= float(qty_open) - 1e-9
            )
            if full_flat:
                gross = (exit_px - entry_px) * qty_open  # type: ignore[operator]
                entry_fee = entry_px * qty_open * entry_fee_r
                exit_fee = exit_px * qty_open * exit_fee_r  # type: ignore[operator]
                net = gross - entry_fee - exit_fee + funding
                base["costs"] = {
                    "entry_px": _fmt(entry_px),
                    "exit_px": _fmt(exit_px),  # type: ignore[arg-type]
                    "qty": _fmt(qty_open),
                    "gross": _fmt(gross),
                    "entry_fee": _fmt(entry_fee),
                    "exit_fee": _fmt(exit_fee),
                    "funding": _fmt(funding),
                    "net": _fmt(net),
                    "strategy_event_net_sum": _fmt(net_sum),
                    "口径": "independent_taker_schedule_plus_explicit_funding",
                }
                base["simulated_pnl"] = _fmt(net)
                base["full_net_status"] = (
                    "computed_under_strategy_schedule_and_explicit_funding"
                )
            elif exit_evs:
                net = net_sum + funding
                base["costs"] = {
                    "entry_px": _fmt(entry_px),
                    "qty": _fmt(qty_open),
                    "strategy_event_net_sum": _fmt(net_sum),
                    "funding": _fmt(funding),
                    "net": _fmt(net),
                    "note": "partial_or_multi_exit_use_strategy_net_sum_plus_funding",
                }
                base["simulated_pnl"] = _fmt(net)
                base["full_net_status"] = "computed_from_strategy_event_sums_plus_funding"
            else:
                base["full_net_status"] = "not_computable"
                base["simulated_pnl"] = None
                base["costs"] = {"note": "no_exit_events"}
            return base
    finally:
        for k in list(os.environ):
            if k.startswith("AUTOMAX_E_") or k in (
                "ENABLE_BINANCE_TESTNET_ORDER",
                "AUTOMAX_ORDER_LEDGER_DIR",
            ):
                os.environ.pop(k, None)
        os.environ.update(saved_env)


def write_strategy_report(report: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "strategy_report.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
