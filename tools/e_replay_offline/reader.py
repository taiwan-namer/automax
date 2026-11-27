"""Readonly offline report builder over arm fills + ledger + optional bars.

Does not import strategy runners, does not call network market APIs,
does not write into source paths.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_iso_to_utc(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(recorded_at: str | None, start_utc: datetime, end_utc: datetime) -> bool:
    dt = _parse_iso_to_utc(recorded_at)
    if dt is None:
        return False
    return start_utc <= dt <= end_utc


@dataclass
class LedgerQuery:
    name: str
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    exchange_trade_key: str | None = None
    adjust_key: str | None = None
    tp1_order_id: str | None = None


@dataclass
class ReportInputs:
    arm_fills: Path
    ledger_fills: Path | None
    ledger_orders: Path | None
    bars_path: Path | None
    trade_id: str
    window_start_utc: str  # ISO Z
    window_end_utc: str
    a_bar_open_utc: str
    a_bar_close_utc: str
    ledger_queries: list[LedgerQuery] = field(default_factory=list)
    funding_known: bool = False
    commission_fx_known: bool = False
    allow_source_write: bool = False  # always rejected when targeting source
    bars_symbol: str | None = None  # require bar.symbol match when set
    bars_interval: str = "1m"
    bars_half_open_end: bool = False  # True → expected open_times in [start, end)
    bars_now_ms: int | None = None  # forming-bar cutoff; None → no forming filter
    # A-point counterfactual sim (None = do not run engine; keeps simulated_pnl null)
    simulate_a_point: bool = False
    sim_direction: str | None = None
    sim_entry_px: str | None = None  # Decimal string; None = missing
    sim_remaining_qty: str | None = None
    sim_entry_fee_rate: str | None = None
    sim_exit_fee_rate: str | None = None
    sim_slippage_rate: str | None = None
    sim_funding: str | None = None  # signed; None = missing (never default 0)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str]]:
    """Yield (1-based line_no, parsed_or_None, raw_line). Preserves duplicates."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            raw = line.rstrip("\n").lstrip("\ufeff")
            if not raw.strip():
                continue
            try:
                yield i, json.loads(raw), raw
            except json.JSONDecodeError:
                yield i, None, raw


def _match_ledger_row(row: dict[str, Any], q: LedgerQuery) -> bool:
    if q.client_order_id and row.get("client_order_id") == q.client_order_id:
        return True
    if q.exchange_order_id is not None and str(row.get("exchange_order_id") or "") == str(
        q.exchange_order_id
    ):
        return True
    if q.exchange_trade_key and (
        row.get("exchange_trade_key") == q.exchange_trade_key
        or q.exchange_trade_key in json.dumps(row, ensure_ascii=False)
    ):
        return True
    if q.adjust_key and (
        row.get("dedupe_key") == q.adjust_key
        or row.get("exchange_trade_key") == q.adjust_key
        or q.adjust_key in json.dumps(row, ensure_ascii=False)
    ):
        return True
    if q.tp1_order_id and str(row.get("exchange_order_id") or "") == str(q.tp1_order_id):
        return True
    return False


def _load_bars(path: Path | None) -> tuple[list[Any], dict[str, Any]]:
    meta: dict[str, Any] = {"path": str(path) if path else None, "exists": False, "n": 0}
    if path is None:
        return [], meta
    if not path.is_file():
        meta["exists"] = False
        return [], meta
    meta["exists"] = True
    suffix = path.suffix.lower()
    bars: list[Any] = []
    if suffix == ".csv":
        import csv

        with path.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            tkey = "open_time" if "open_time" in cols else (cols[0] if cols else "open_time")
            for row in reader:
                try:
                    t = int(float(row[tkey]))
                except Exception:
                    continue
                if t < 1e12:
                    t *= 1000
                bars.append({"open_time": t, "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close")})
    elif suffix == ".jsonl":
        text = path.read_text(encoding="utf-8", errors="replace")
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            bars.append(json.loads(ln))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            bars = []
        else:
            data = json.loads(text)
            if isinstance(data, list):
                bars = data
            elif isinstance(data, dict):
                bars = data.get("bars") or data.get("klines") or data.get("data") or []
            else:
                bars = []
    meta["n"] = len(bars)
    return bars, meta


def _bar_open_time_ms(b: Any) -> int | None:
    if isinstance(b, (list, tuple)) and b:
        return int(b[0])
    if isinstance(b, dict):
        for k in ("t", "open_time", "openTime", "ts"):
            if k in b and b[k] is not None:
                v = int(b[k])
                if v < 1e12:
                    v *= 1000
                return v
    return None


def _bar_close_time_ms(b: Any) -> int | None:
    if isinstance(b, dict):
        for k in ("close_time", "closeTime", "ct"):
            if k in b and b[k] is not None:
                v = int(b[k])
                if v < 1e12:
                    v *= 1000
                return v
        ot = _bar_open_time_ms(b)
        if ot is not None:
            # 1m default: close = open + 59999 if not provided
            return ot + 59_999
    return None


def _bar_symbol(b: Any) -> str | None:
    if isinstance(b, dict):
        for k in ("symbol", "s", "pair"):
            if b.get(k):
                return str(b[k]).upper()
    return None


def _bar_interval(b: Any) -> str | None:
    if isinstance(b, dict):
        for k in ("interval", "tf", "timeframe"):
            if b.get(k):
                return str(b[k])
    return None


def align_bars_window(
    bars: list[Any] | None,
    *,
    window_start_utc: str,
    window_end_utc: str,
    symbol: str | None = None,
    interval: str = "1m",
    half_open_end: bool = False,
    now_ms: int | None = None,
    bars_meta: dict[str, Any] | None = None,
    bars_provided: bool = True,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Align 1m bars to trade window by symbol/interval/open_time. Never silent-dedupe."""

    def floor_min(s: str) -> int:
        dt = _parse_iso_to_utc(s)
        assert dt is not None
        dt2 = dt.replace(second=0, microsecond=0)
        return int(dt2.timestamp() * 1000)

    start_ms = floor_min(window_start_utc)
    end_ms = floor_min(window_end_utc)
    expected: list[int] = []
    t = start_ms
    if half_open_end:
        while t < end_ms:
            expected.append(t)
            t += 60_000
    else:
        while t <= end_ms:
            expected.append(t)
            t += 60_000
    expected_set = set(expected)

    base = {
        "align_keys": ["symbol", "interval", "open_time"],
        "symbol": symbol,
        "interval": interval,
        "window": {
            "start": window_start_utc,
            "end": window_end_utc,
            "half_open_end": half_open_end,
            "start_ms": start_ms,
            "end_ms": end_ms,
        },
        "expected": len(expected),
        "expected_open_times": expected,
        "source_identity": {
            "path": source_path,
            "sha256": sha256_file(Path(source_path)) if source_path else None,
        },
        "bars_meta": bars_meta or {},
        "simulated_pnl": None,
    }

    if not bars_provided:
        return {
            **base,
            "status": "bars_not_provided",
            "matched": None,
            "missing": list(expected),
            "duplicate_keys": [],
            "outside_window_n": 0,
            "forming_n": 0,
            "wrong_symbol_n": 0,
            "blocked": True,
            "blocked_reason": "PRICE_PATH_MISSING",
            "blocked_note": "bars_not_provided",
        }

    rows = bars or []
    open_times_in_window: list[int] = []
    duplicate_keys: list[str] = []
    seen: dict[str, int] = {}
    outside_n = 0
    forming_n = 0
    wrong_symbol_n = 0
    wrong_interval_n = 0

    for b in rows:
        ot = _bar_open_time_ms(b)
        if ot is None:
            continue
        sym = _bar_symbol(b)
        iv = _bar_interval(b) or interval
        if symbol and sym is not None and sym != str(symbol).upper():
            wrong_symbol_n += 1
            continue
        if symbol and sym is None:
            # require explicit symbol when filter set — cannot invent match
            wrong_symbol_n += 1
            continue
        if iv != interval:
            wrong_interval_n += 1
            continue
        if half_open_end:
            in_win = start_ms <= ot < end_ms
        else:
            in_win = start_ms <= ot <= end_ms
        if not in_win:
            outside_n += 1
            continue
        ct = _bar_close_time_ms(b)
        if now_ms is not None and ct is not None and ct >= now_ms:
            forming_n += 1
            continue  # forming in-window bar excluded from completed set
        key = f"{sym or symbol or ''}|{iv}|{ot}"
        seen[key] = seen.get(key, 0) + 1
        open_times_in_window.append(ot)

    for key, n in sorted(seen.items()):
        if n > 1:
            duplicate_keys.append(key)

    # matched = unique expected open_times present (duplicates still flagged)
    present = {ot for ot in open_times_in_window if ot in expected_set}
    missing = [ot for ot in expected if ot not in present]
    matched_n = len(present)

    blocked = False
    blocked_reason = None
    blocked_note = None
    if duplicate_keys:
        blocked = True
        blocked_reason = "DUPLICATE_BAR_KEYS"
        blocked_note = "duplicates_not_silently_deduped"
    if matched_n < len(expected):
        # incomplete coverage blocks simulation readiness
        if not blocked:
            blocked = True
            blocked_reason = "BARS_COVERAGE_INCOMPLETE"
            blocked_note = f"missing={len(missing)};forming_excluded={forming_n}"
    if matched_n == 0:
        blocked = True
        blocked_reason = "PRICE_PATH_MISSING"

    return {
        **base,
        "status": "bars_path_checked",
        "matched": matched_n,
        "missing": missing,
        "duplicate_keys": duplicate_keys,
        "outside_window_n": outside_n,
        "forming_n": forming_n,
        "wrong_symbol_n": wrong_symbol_n,
        "wrong_interval_n": wrong_interval_n,
        "blocked": blocked,
        "blocked_reason": blocked_reason,
        "blocked_note": blocked_note,
        "available_for_later_simulation": (matched_n == len(expected) and not duplicate_keys),
    }


def build_report(inp: ReportInputs, *, after_read_hook=None) -> dict[str, Any]:
    start = _parse_iso_to_utc(inp.window_start_utc)
    end = _parse_iso_to_utc(inp.window_end_utc)
    if start is None or end is None:
        raise ValueError("invalid_window_utc")

    sources: dict[str, Any] = {}
    for label, p in (
        ("arm_fills", inp.arm_fills),
        ("ledger_fills", inp.ledger_fills),
        ("ledger_orders", inp.ledger_orders),
        ("bars", inp.bars_path),
    ):
        if p is None:
            sources[label] = {"path": None, "sha256_before": None, "sha256_after": None, "stable": None}
            continue
        before = sha256_file(p)
        # read happens below; after hash at end
        sources[label] = {
            "path": str(p.resolve()) if p else None,
            "exists": p.is_file(),
            "sha256_before": before,
            "sha256_after": None,
            "stable": None,
        }

    # Refuse writing into sources (also when out would equal source paths)
    source_paths = [p for p in (inp.arm_fills, inp.ledger_fills, inp.ledger_orders, inp.bars_path) if p]
    write_guard = {
        "allow_source_write_requested": bool(inp.allow_source_write),
        "write_rejected": True if inp.allow_source_write else None,
        "policy": "refuse_write_to_source_or_runner_dirs",
        "source_paths": [str(p.resolve()) for p in source_paths if p],
    }

    events: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}
    if not inp.arm_fills.is_file():
        arm_status = "unknown"
    else:
        arm_status = "ok"
        for line_no, obj, _raw in _iter_jsonl(inp.arm_fills):
            if obj is None:
                continue
            if obj.get("trade_id") != inp.trade_id:
                continue
            # window filter on recorded_at when present; keep rows without time as in-scope for exact trade_id
            ra = obj.get("recorded_at")
            if ra and not _in_window(ra, start, end):
                # still include if exact trade_id match outside window? Spec: use window for fixed case
                # Keep only in-window OR if recorded_at missing
                continue
            ev = {
                "line_no": line_no,
                "event": obj.get("event"),
                "recorded_at": ra,
                "qty": obj.get("qty"),
                "price": obj.get("price"),
                "fee_u": obj.get("fee_u")
                if obj.get("fee_u") is not None
                else obj.get("fee_u_confirmed"),
                "fee_field": "fee_u"
                if obj.get("fee_u") is not None
                else ("fee_u_confirmed" if obj.get("fee_u_confirmed") is not None else None),
                "side": obj.get("side"),
                "strategy_version": obj.get("strategy_version"),
                "exchange_trade_key": obj.get("exchange_trade_key"),
                "client_order_id": obj.get("testnet_client_order_id") or obj.get("client_order_id"),
                "funding_data_gap": obj.get("funding_data_gap"),
                "commission_asset": obj.get("commission_asset"),
            }
            events.append(ev)
            k = str(ev["event"])
            event_counts[k] = event_counts.get(k, 0) + 1

    # qty balance (OPEN+ENTRY_ADD vs SETTLEMENT_CLOSE/CLOSE) — no double-count FEE_ADJUST
    open_qty = sum(
        float(e["qty"] or 0)
        for e in events
        if e.get("event") in ("OPEN", "ENTRY_ADD")
    )
    close_qty = sum(
        float(e["qty"] or 0)
        for e in events
        if e.get("event") == "SETTLEMENT_CLOSE"
    )
    if close_qty == 0:
        close_qty = sum(float(e["qty"] or 0) for e in events if e.get("event") == "CLOSE")
    qty_balance = {
        "open_plus_add": open_qty,
        "close_like_qty": close_qty,
        "delta": open_qty - close_qty,
        "note": "FEE_ADJUST excluded from qty; SETTLEMENT preferred over CLOSE",
    }

    # ledger queries
    ledger_results: list[dict[str, Any]] = []
    for q in inp.ledger_queries:
        fills_hits: list[dict[str, Any]] = []
        orders_hits: list[dict[str, Any]] = []
        if inp.ledger_fills and inp.ledger_fills.is_file():
            for line_no, obj, _ in _iter_jsonl(inp.ledger_fills):
                if obj and _match_ledger_row(obj, q):
                    fills_hits.append(
                        {
                            "line_no": line_no,
                            "event": obj.get("event"),
                            "client_order_id": obj.get("client_order_id"),
                            "exchange_order_id": obj.get("exchange_order_id"),
                            "exchange_trade_id": obj.get("exchange_trade_id"),
                            "observation_kind": obj.get("observation_kind"),
                        }
                    )
        if inp.ledger_orders and inp.ledger_orders.is_file():
            for line_no, obj, _ in _iter_jsonl(inp.ledger_orders):
                if obj and _match_ledger_row(obj, q):
                    orders_hits.append(
                        {
                            "line_no": line_no,
                            "event": obj.get("event"),
                            "client_order_id": obj.get("client_order_id"),
                            "exchange_order_id": obj.get("exchange_order_id"),
                        }
                    )
        ledger_results.append(
            {
                "query": q.name,
                "keys": {
                    "client_order_id": q.client_order_id,
                    "exchange_order_id": q.exchange_order_id,
                    "exchange_trade_key": q.exchange_trade_key,
                    "adjust_key": q.adjust_key,
                    "tp1_order_id": q.tp1_order_id,
                },
                "fills_hits": len(fills_hits),
                "orders_hits": len(orders_hits),
                "fills_sample": fills_hits[:3],
                "orders_sample": orders_hits[:3],
                "note": "zero_hits_do_not_invalidate_other_queries; order_level_cumulative!=per_trade",
            }
        )

    bars_provided = inp.bars_path is not None
    bars, bars_meta = _load_bars(inp.bars_path) if bars_provided else ([], {"path": None, "exists": False, "n": 0})
    align = align_bars_window(
        bars if bars_provided else None,
        window_start_utc=inp.window_start_utc,
        window_end_utc=inp.window_end_utc,
        symbol=inp.bars_symbol,
        interval=inp.bars_interval,
        half_open_end=inp.bars_half_open_end,
        now_ms=inp.bars_now_ms,
        bars_meta=bars_meta,
        bars_provided=bars_provided,
        source_path=str(inp.bars_path.resolve()) if inp.bars_path else None,
    )
    kline = {
        **align,
        "a_point": {
            "bar_open_utc": inp.a_bar_open_utc,
            "bar_close_utc": inp.a_bar_close_utc,
            "semantics": "counterfactual_valuation_not_exchange_fill",
        },
    }
    if inp.simulate_a_point:
        from .a_point_sim import APointSimInputs, _d, simulate_a_point_exit

        a_point_status = simulate_a_point_exit(
            APointSimInputs(
                symbol=str(inp.bars_symbol or ""),
                interval=inp.bars_interval,
                direction=inp.sim_direction,
                entry_px=_d(inp.sim_entry_px),
                remaining_qty=_d(inp.sim_remaining_qty),
                a_point_utc=inp.a_bar_open_utc,
                market_cutoff_ms=inp.bars_now_ms,
                entry_fee_rate=_d(inp.sim_entry_fee_rate),
                exit_fee_rate=_d(inp.sim_exit_fee_rate),
                slippage_rate=_d(inp.sim_slippage_rate),
                funding=_d(inp.sim_funding),
                bars=bars if bars_provided else None,
                bars_provided=bars_provided,
                bars_source_path=str(inp.bars_path.resolve()) if inp.bars_path else None,
                window_start_utc=inp.window_start_utc,
                window_end_utc=inp.window_end_utc,
                half_open_end=inp.bars_half_open_end,
            )
        )
        # normalize reason alias for older consumers
        if a_point_status.get("blocked_reason") and "reason" not in a_point_status:
            a_point_status["reason"] = a_point_status["blocked_reason"]
    elif not bars_provided:
        a_point_status = {
            "status": "blocked",
            "reason": "PRICE_PATH_MISSING",
            "a_exit_px": None,
            "simulated_pnl": None,
            "note": "bars_not_provided",
        }
    elif align.get("blocked"):
        a_point_status = {
            "status": "blocked",
            "reason": align.get("blocked_reason") or "BARS_ALIGNMENT_BLOCKED",
            "a_exit_px": None,
            "simulated_pnl": None,
            "note": align.get("blocked_note"),
        }
    elif (align.get("matched") or 0) == 0:
        a_point_status = {
            "status": "blocked",
            "reason": "PRICE_PATH_MISSING",
            "a_exit_px": None,
            "simulated_pnl": None,
        }
    else:
        a_point_status = {
            "status": "bars_present_not_simulated",
            "reason": None,
            "a_exit_px": None,
            "simulated_pnl": None,
            "note": "available_for_later_simulation_engine_not_run",
            "simulation_ready": align.get("matched") == align.get("expected")
            and not align.get("duplicate_keys")
            and not align.get("blocked"),
        }

    # costs
    fee_sum = 0.0
    fee_any = False
    open_asset_missing = False
    for e in events:
        if e.get("event") == "OPEN" and e.get("commission_asset") in (None, ""):
            open_asset_missing = True
        if e.get("fee_u") is not None:
            fee_any = True
            # do not add FEE_ADJUST slice on top of OPEN fee_u blindly — list separately
            if e.get("event") != "FEE_ADJUST":
                fee_sum += float(e["fee_u"])

    full_net: Any
    if (not inp.funding_known) or (not inp.commission_fx_known) or open_asset_missing:
        full_net = "not_computable"
    else:
        full_net = "not_computable"  # still not computing PnL without verified prices path

    gaps = {
        "exchange_execution_time": "missing",
        "commission_asset_open": "missing" if open_asset_missing else "present",
        "funding": "gap" if not inp.funding_known else "assumed_known_fixture",
        "commission_fx": "unknown" if not inp.commission_fx_known else "assumed_known_fixture",
        "recorded_at_ne_exchange_time": True,
    }

    # Optional test hook: mutate sources after read, before after-hash.
    if after_read_hook is not None:
        after_read_hook()

    # after hashes
    for label, p in (
        ("arm_fills", inp.arm_fills),
        ("ledger_fills", inp.ledger_fills),
        ("ledger_orders", inp.ledger_orders),
        ("bars", inp.bars_path),
    ):
        if p is None:
            continue
        after = sha256_file(p) if p.is_file() else None
        sources[label]["sha256_after"] = after
        before = sources[label].get("sha256_before")
        sources[label]["stable"] = bool(before is not None and after is not None and before == after)

    # Top-level stable false if any hashed source changed mid-read
    any_unstable = any(v.get("stable") is False for v in sources.values())

    filter_summary = summarize_arm_filters(
        inp.arm_fills,
        inp.trade_id,
        inp.window_start_utc,
        inp.window_end_utc,
    )

    report = {
        "label": "e_repair_offline_01",
        "trade_id": inp.trade_id,
        "window_utc": {"start": inp.window_start_utc, "end": inp.window_end_utc},
        "arm_status": arm_status,
        "sources": sources,
        "stable": not any_unstable,
        "write_guard": write_guard,
        "event_counts": event_counts,
        "event_n": len(events),
        "events_order_preserved": True,
        "events": events,  # caller may strip for upload
        "filter_summary": {
            "full_file_by_trade": filter_summary.get("trade_id_only"),
            "window_by_trade": filter_summary.get("trade_id_and_window"),
            "excluded_from_window": filter_summary.get("excluded_from_window"),
            "tp_preplaced": filter_summary.get("tp_preplaced"),
            "sort_rule": filter_summary.get("sort_rule"),
        },
        "qty_balance": qty_balance,
        "ledger_queries": ledger_results,
        "kline": kline,
        "a_point": a_point_status,
        "costs": {
            "fee_u_sum_excluding_fee_adjust": fee_sum if fee_any else None,
            "full_net_pnl": full_net,
            "funding_included": False,
            "slippage_double_count": "not_applied",
        },
        "gaps": gaps,
        "link_consistency": "unverified",
    }
    return report


class SourceWriteRefused(RuntimeError):
    """Raised before any write when output would overwrite a source path."""


def summarize_arm_filters(
    arm_fills: Path,
    trade_id: str,
    window_start_utc: str,
    window_end_utc: str,
) -> dict[str, Any]:
    """Readonly: trade_id-only vs trade_id+window; exclusion reasons (no event payloads)."""
    start = _parse_iso_to_utc(window_start_utc)
    end = _parse_iso_to_utc(window_end_utc)
    by_trade: dict[str, int] = {}
    by_window: dict[str, int] = {}
    excluded: dict[str, int] = {}
    excluded_events: dict[str, int] = {}
    order_rule = "file_order_line_no_ascending_no_resort"
    n_trade = 0
    n_window = 0
    if not arm_fills.is_file():
        return {"arm_status": "unknown", "path": str(arm_fills)}
    for _line_no, obj, _raw in _iter_jsonl(arm_fills):
        if obj is None:
            excluded["json_parse_fail"] = excluded.get("json_parse_fail", 0) + 1
            continue
        if obj.get("trade_id") != trade_id:
            excluded["trade_id_mismatch"] = excluded.get("trade_id_mismatch", 0) + 1
            continue
        ev = str(obj.get("event"))
        by_trade[ev] = by_trade.get(ev, 0) + 1
        n_trade += 1
        ra = obj.get("recorded_at")
        if ra is None or ra == "":
            # keep when no recorded_at (same as build_report)
            by_window[ev] = by_window.get(ev, 0) + 1
            n_window += 1
            continue
        if start is None or end is None or not _in_window(ra, start, end):
            excluded["outside_window"] = excluded.get("outside_window", 0) + 1
            excluded_events[ev] = excluded_events.get(ev, 0) + 1
            continue
        by_window[ev] = by_window.get(ev, 0) + 1
        n_window += 1
    return {
        "path": str(arm_fills.resolve()),
        "trade_id": trade_id,
        "window_utc": {"start": window_start_utc, "end": window_end_utc},
        "sort_rule": order_rule,
        "trade_id_only": {"event_n": n_trade, "event_counts": by_trade},
        "trade_id_and_window": {"event_n": n_window, "event_counts": by_window},
        "excluded_from_window": {
            "counts_by_reason": excluded,
            "outside_window_by_event": excluded_events,
        },
        "tp_preplaced": {
            "in_trade_id_only": by_trade.get("TP_PREPLACED", 0),
            "in_window": by_window.get("TP_PREPLACED", 0),
            "excluded_as_outside_window": excluded_events.get("TP_PREPLACED", 0),
            "note": "if in_trade_id_only>0 and in_window==0 → excluded by recorded_at window, not parse drop",
        },
    }


def write_report_atomic(
    report: dict[str, Any],
    out_dir: Path,
    *,
    include_events: bool = True,
    source_paths: list[Path] | None = None,
    out_file: Path | None = None,
) -> Path:
    """Write report under out_dir (or explicit out_file). Never writes before refuse checks."""
    # refuse if out_dir looks like live source / runner
    forbidden_parts = (
        "order_fill_ledger",
        "account_state.json",
        "testnet_e_trend_v2_local\\data_e\\output",
        "testnet_e_trend_v2_local",
    )
    resolved = str(out_dir.resolve()).lower()
    allowed_out = (
        "e_repair_offline_01",
        "e_repair_e2e_01",
        "e_functional_a_01",
        "e_functional_a_evidence_02",
        "e_functional_bars_01",
        "e_functional_store_02",
        "e_functional_runner_03",
        "e_functional_strategy_01",
    )
    for frag in forbidden_parts:
        if frag.lower() in resolved and not any(a in resolved for a in allowed_out):
            raise SourceWriteRefused(f"refuse_output_to_source_like_dir:{out_dir}")

    src_list = [p for p in (source_paths or []) if p]
    src_resolved = [p.resolve() for p in src_list]
    out_r = out_dir.resolve()

    for sp_r in src_resolved:
        if sp_r.is_file() and out_r == sp_r.parent:
            raise SourceWriteRefused("refuse_outdir_equals_source_dir")
        if out_r == sp_r:
            raise SourceWriteRefused("refuse_outdir_equals_source_path")

    out = (out_file.resolve() if out_file is not None else (out_dir / "report.json").resolve())
    for sp_r in src_resolved:
        if out == sp_r:
            raise SourceWriteRefused("refuse_outfile_equals_source_file")

    # also refuse if default report.json would equal any source
    default_out = (out_dir / "report.json").resolve()
    for sp_r in src_resolved:
        if default_out == sp_r:
            raise SourceWriteRefused("refuse_outfile_equals_source_file")

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    if not include_events:
        payload = {k: v for k, v in payload.items() if k != "events"}
        payload["events_omitted"] = True
        payload["event_n"] = report.get("event_n")
    target = Path(out)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target
