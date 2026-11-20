#!/usr/bin/env python3
"""Read-only replay analysis for paper_bulk_test_v1 focus pack. Writes only into this dir."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

OUT = Path(__file__).resolve().parent
PACK = OUT.parent / "pack_20261119_pippin_tao_fet_lit_jup"
ROOT = Path(
    "/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1"
)
RUN = ROOT / "runs" / "20261119T132017_bd1972b2"
VAULT = Path(
    "/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/data/paper_bulk_test_v1/kline_vault_v2_clean"
)
SNAP = RUN / "code_snapshot" / "breakout_follow_v1"
TZ = timezone(timedelta(hours=8))
CUTOFF = datetime(2026, 11, 19, 14, 23, 11, tzinfo=TZ)
SLIP = 0.0002
TARGET_RUN = "20261119T132017_bd1972b2"
EVIDENCE: list[dict[str, Any]] = []


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def book_ts_to_dt(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    # epoch seconds (with fractional) — Binance-style ms would be ~1e12+
    if v > 1e12:
        v = v / 1000.0
    return datetime.fromtimestamp(v, tz=TZ)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        d = json.loads(line)
        d["_line"] = i
        d["_rel"] = str(path.relative_to(OUT.parent.parent.parent)) if False else str(path)
        # prefer pack-relative when under pack/analysis
        try:
            d["_rel_pack"] = str(path.relative_to(PACK.parent))
        except ValueError:
            try:
                d["_rel_pack"] = str(path.relative_to(ROOT.parent.parent.parent))
            except ValueError:
                d["_rel_pack"] = str(path)
        rows.append(d)
    return rows


def fill_dedupe_key(f: dict[str, Any]) -> tuple:
    return (
        f.get("trade_id"),
        f.get("event"),
        f.get("recorded_at"),
        round(float(f.get("qty") or 0), 10),
        round(float(f.get("price") or 0), 12),
        f.get("exit_reason"),
        round(float(f.get("fee_u") or 0), 10),
        round(float(f.get("net_u") or f.get("realized_net_u") or 0), 10),
        f.get("book_ts"),
    )


def load_fills_deduped(paths: list[Path], cutoff: datetime) -> list[dict[str, Any]]:
    seen = set()
    out = []
    conflicts = []
    by_soft: dict[tuple, list] = defaultdict(list)
    for p in paths:
        for f in load_jsonl(p):
            f["_path"] = str(p)
            dt = parse_iso(f.get("recorded_at"))
            if dt is None or dt > cutoff:
                continue
            key = fill_dedupe_key(f)
            soft = (f.get("trade_id"), f.get("event"), f.get("recorded_at"), f.get("exit_reason"))
            by_soft[soft].append(f)
            if key in seen:
                continue
            # soft conflict detection: same soft key different numeric payload
            peers = [x for x in by_soft[soft] if fill_dedupe_key(x) != key]
            if peers:
                conflicts.append({"soft": soft, "a": key, "b": fill_dedupe_key(peers[0])})
            seen.add(key)
            out.append(f)
    out.sort(key=lambda x: (parse_iso(x.get("recorded_at")) or datetime.min.replace(tzinfo=TZ), x.get("event", "")))
    return out, conflicts


def mark_for_side(side: str, bid: float, ask: float, slip: float = SLIP) -> float:
    if side == "LONG":
        return bid * (1 - slip)
    return ask * (1 + slip)


def stop_hit(side: str, mark: float, stop: float) -> bool:
    if side == "LONG":
        return mark <= stop
    return mark >= stop


def check_klines(csv_path: Path, label: str) -> dict[str, Any]:
    if not csv_path.exists():
        return {"label": label, "missing": True, "path": str(csv_path)}
    rows = list(csv.DictReader(csv_path.open()))
    opens = []
    issues = []
    for r in rows:
        ot = int(float(r.get("open_time") or r.get("open_time_ms") or 0))
        if ot > 1e12:
            pass  # ms
        elif ot > 1e9:
            ot = ot * 1000
        opens.append(ot)
        o, h, l, c = map(float, (r["open"], r["high"], r["low"], r["close"]))
        if not (l <= o <= h and l <= c <= h and l <= h):
            issues.append({"type": "ohlc_anomaly", "open_time": ot})
        closed = r.get("is_closed")
        if closed is not None and str(closed).lower() in ("false", "0", ""):
            issues.append({"type": "unclosed", "open_time": ot})
    sorted_ok = opens == sorted(opens)
    dup = len(opens) - len(set(opens))
    gaps = []
    so = sorted(set(opens))
    for a, b in zip(so, so[1:]):
        if b - a != 60_000:
            gaps.append({"from": a, "to": b, "gap_min": (b - a) / 60000.0})
    return {
        "label": label,
        "path": str(csv_path),
        "n": len(rows),
        "sorted": sorted_ok,
        "dup_open_time": dup,
        "gap_count": len(gaps),
        "gaps_sample": gaps[:5],
        "issues": issues[:10],
        "first_ot": so[0] if so else None,
        "last_ot": so[-1] if so else None,
    }


def load_klines(csv_path: Path) -> list[dict[str, Any]]:
    rows = []
    for r in csv.DictReader(csv_path.open()):
        ot = int(float(r.get("open_time") or 0))
        if ot < 1e12:
            ot *= 1000
        rows.append(
            {
                "open_time": ot,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
                "is_closed": str(r.get("is_closed", "true")).lower() not in ("false", "0"),
                "dt": datetime.fromtimestamp(ot / 1000.0, tz=TZ),
            }
        )
    rows.sort(key=lambda x: x["open_time"])
    return rows


def aggregate_tf(rows: list[dict], tf_min: int) -> list[dict]:
    """UTC-aligned aggregation; mark incomplete buckets."""
    if not rows:
        return []
    tf_ms = tf_min * 60_000
    buckets: dict[int, list] = defaultdict(list)
    for r in rows:
        # align to UTC
        utc_ot = r["open_time"]  # already epoch ms absolute
        bucket = (utc_ot // tf_ms) * tf_ms
        buckets[bucket].append(r)
    out = []
    for b in sorted(buckets):
        rs = buckets[b]
        expected = tf_min
        complete = len(rs) >= expected and all(
            (rs[0]["open_time"] + i * 60_000) in {x["open_time"] for x in rs} for i in range(expected)
        )
        # simpler completeness: consecutive minutes covering full window
        ots = sorted(x["open_time"] for x in rs)
        complete = len(ots) == expected and ots[-1] - ots[0] == (expected - 1) * 60_000 and all(
            ots[i] - ots[i - 1] == 60_000 for i in range(1, len(ots))
        )
        out.append(
            {
                "open_time": b,
                "dt": datetime.fromtimestamp(b / 1000.0, tz=timezone.utc).astimezone(TZ),
                "open": rs[0]["open"],
                "high": max(x["high"] for x in rs),
                "low": min(x["low"] for x in rs),
                "close": rs[-1]["close"],
                "volume": sum(x["volume"] for x in rs),
                "n_1m": len(rs),
                "complete": complete,
                "derived": True,
            }
        )
    return out


@dataclass
class TradeAgg:
    trade_id: str
    symbol: str = ""
    side: str = ""
    opens: list = field(default_factory=list)
    partials: list = field(default_factory=list)
    closes: list = field(default_factory=list)
    fees: float = 0.0
    complete_net: Optional[float] = None
    open_fee: float = 0.0
    close_fees: float = 0.0
    qty_open: float = 0.0
    qty_closed: float = 0.0
    status: str = "unknown"  # complete / open / partial_open


def build_trades(fills: list[dict]) -> dict[str, TradeAgg]:
    trades: dict[str, TradeAgg] = {}
    for f in fills:
        tid = f.get("trade_id") or ""
        t = trades.setdefault(tid, TradeAgg(trade_id=tid))
        t.symbol = f.get("symbol") or t.symbol
        t.side = f.get("side") or t.side
        ev = f.get("event")
        fee = float(f.get("fee_u") or 0)
        t.fees += fee
        if ev == "OPEN":
            t.opens.append(f)
            t.open_fee += fee
            t.qty_open += float(f.get("qty") or 0)
            # OPEN realized_net_u often = -fee; do NOT add as pnl beyond fee tracking
        elif ev in ("CLOSE_PARTIAL", "PARTIAL_TP") or (ev == "CLOSE" and f.get("exit_reason") == "PARTIAL_TP"):
            # pack/root may use CLOSE_PARTIAL or event CLOSE with PARTIAL_TP
            if ev == "CLOSE" and f.get("exit_reason") == "PARTIAL_TP":
                t.partials.append(f)
            elif ev in ("CLOSE_PARTIAL", "PARTIAL_TP"):
                t.partials.append(f)
            else:
                t.partials.append(f)
            t.close_fees += fee
            t.qty_closed += float(f.get("qty") or 0)
        elif ev == "CLOSE":
            t.closes.append(f)
            t.close_fees += fee
            t.qty_closed += float(f.get("qty") or 0)
        else:
            # unknown
            pass
    for t in trades.values():
        # complete if fully closed: has OPEN and final CLOSE (not only partial)
        has_final = any(c.get("exit_reason") != "PARTIAL_TP" for c in t.closes) or (
            any(c.get("event") == "CLOSE" and c.get("exit_reason") != "PARTIAL_TP" for c in t.closes)
        )
        # Also: some systems put PARTIAL as CLOSE with exit_reason PARTIAL_TP then final CLOSE
        finals = [c for c in t.closes if c.get("exit_reason") != "PARTIAL_TP"]
        partials = t.partials + [c for c in t.closes if c.get("exit_reason") == "PARTIAL_TP"]
        t.partials = partials
        t.closes = finals
        if t.opens and t.closes and abs(t.qty_closed - t.qty_open) < 1e-6:
            t.status = "complete"
        elif t.opens and t.closes and t.qty_closed + 1e-6 < t.qty_open:
            t.status = "partial_still_open"
        elif t.opens and not t.closes and not t.partials:
            t.status = "open"
        elif t.opens and (t.partials or t.closes) and t.qty_closed + 1e-6 < t.qty_open:
            t.status = "partial_still_open"
        elif t.opens and t.closes:
            # qty mismatch but has close — still treat complete if remaining ~0 from last close remaining field
            rem = t.closes[-1].get("remaining")
            if rem is not None and float(rem) == 0:
                t.status = "complete"
            elif abs(t.qty_closed - t.qty_open) / max(t.qty_open, 1e-9) < 1e-4:
                t.status = "complete"
            else:
                t.status = "qty_mismatch"
        else:
            t.status = "incomplete_record"

        if t.status == "complete":
            # net = sum of CLOSE/PARTIAL net_u ONLY (already include exit fees).
            # OPEN fee: if OPEN.realized_net_u == -fee, it is already accounted in account realized,
            # but for trade-level: complete trade net = sum(partial/close net_u) ; open fee already
            # either embedded as negative realized on OPEN or should be subtracted once.
            # Spec: OPEN realized_net_u may already be -fee; CLOSE net_u already deducts exit fee.
            # Correct merge: sum(all CLOSE/PARTIAL net_u) + sum(OPEN realized_net_u if present else -open_fee)
            open_realized = 0.0
            for o in t.opens:
                if o.get("realized_net_u") is not None:
                    open_realized += float(o["realized_net_u"])
                elif o.get("net_u") is not None and o.get("event") == "OPEN":
                    open_realized += float(o["net_u"])
                else:
                    open_realized += -float(o.get("fee_u") or 0)
            close_nets = sum(float(x.get("net_u") or 0) for x in t.partials + t.closes)
            t.complete_net = open_realized + close_nets
    return trades


def stats_from_complete(trades: dict[str, TradeAgg]) -> dict[str, Any]:
    complete = [t for t in trades.values() if t.status == "complete"]
    open_n = sum(1 for t in trades.values() if t.status in ("open", "partial_still_open"))
    nets = [t.complete_net for t in complete if t.complete_net is not None]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else None
    fees = sum(t.fees for t in trades.values())
    return {
        "n_trade_ids": len(trades),
        "n_opens_events": sum(len(t.opens) for t in trades.values()),
        "n_complete": len(complete),
        "n_open_or_partial": open_n,
        "n_other": len(trades) - len(complete) - open_n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(nets)) if nets else None,
        "total_net": sum(nets) if nets else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr": rr,
        "pf": pf,
        "expectancy": (sum(nets) / len(nets)) if nets else None,
        "fees_all_events": fees,
        "fees_complete_trades": sum(t.fees for t in complete),
    }


def add_ev(theme, rel, key, time, trade_id, values, conclusion):
    if len(EVIDENCE) >= 60:
        return
    EVIDENCE.append(
        {
            "theme": theme,
            "source_rel": rel,
            "row_or_key": key,
            "time": time,
            "trade_id": trade_id,
            "values": values,
            "conclusion": conclusion,
        }
    )


def analyze_pippin(fills_root, code_note) -> dict[str, Any]:
    d1 = PACK / "01_PIPPIN_1354_1401"
    fills = load_jsonl(d1 / "fills_PIPPINUSDT.jsonl")
    quotes = load_jsonl(d1 / "quote_events_PIPPINUSDT.jsonl")
    pos = load_jsonl(d1 / "position_events_PIPPINUSDT.jsonl")
    stops = load_jsonl(d1 / "stop_events_PIPPINUSDT.jsonl")
    decisions = load_jsonl(d1 / "decision_trace_PIPPINUSDT.jsonl")
    open_f = next(f for f in fills if f["event"] == "OPEN")
    close_f = next(f for f in fills if f["event"] == "CLOSE")
    side = open_f["side"]
    entry = float(open_f["price"])
    qty = float(open_f["qty"])
    stop = float(open_f["initial_stop"])
    exit_px = float(close_f["price"])
    open_fee = float(open_f["fee_u"])
    close_fee = float(close_f["fee_u"])
    net = float(close_f["net_u"]) + (
        float(open_f["realized_net_u"]) if open_f.get("realized_net_u") is not None else -open_fee
    )
    # If OPEN has no realized_net_u, account typically still charged fee at open;
    # close net_u = gross - exit fee only (entry fee already in realized separately).
    # From PACK_META close net_u=-179.845 includes only exit side relative to entry for gross-fee_exit;
    # Account complete = open(-fee)+close(net). Check open fields.
    if open_f.get("realized_net_u") is None and open_f.get("net_u") is None:
        # use close net_u alone is NOT complete; subtract open fee
        complete_net = float(close_f["net_u"]) - open_fee
    else:
        complete_net = net

    stop_dist_pct = (stop - entry) / entry if side == "SHORT" else (entry - stop) / entry
    theor_price_risk = abs(stop - entry) * qty  # gross price risk excl fees
    actual_price_loss = abs(exit_px - entry) * qty  # for short: (exit-entry)*qty
    if side == "SHORT":
        actual_price_loss = (exit_px - entry) * qty
    else:
        actual_price_loss = (entry - exit_px) * qty
    overshoot_pct = (exit_px - stop) / stop if side == "SHORT" else (stop - exit_px) / stop
    extra_loss_u = (exit_px - stop) * qty if side == "SHORT" else (stop - exit_px) * qty
    mult = extra_loss_u / theor_price_risk if theor_price_risk else None

    # rebuild quote timeline near stop
    timeline = []
    last_ok = None
    first_hit = None
    for q in quotes:
        bid = float(q.get("bid") or 0)
        ask = float(q.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mark = mark_for_side(side, bid, ask)
        hit = stop_hit(side, mark, stop)
        age = q.get("quote_age_sec") or q.get("age_sec") or q.get("book_age_sec")
        rec = {
            "recorded_at": q.get("recorded_at"),
            "book_ts": q.get("book_ts") or q.get("fetched_at"),
            "bid": bid,
            "ask": ask,
            "mark": mark,
            "hit": hit,
            "age": age,
            "line": q["_line"],
            "stale": q.get("stale") or q.get("rejected") or q.get("reason"),
        }
        if not hit:
            last_ok = rec
        if hit and first_hit is None:
            first_hit = rec
        timeline.append(rec)

    # verify fill == ask*(1+slip) at close quote
    close_dt = parse_iso(close_f["recorded_at"])
    near = [t for t in timeline if parse_iso(t["recorded_at"]) and abs((parse_iso(t["recorded_at"]) - close_dt).total_seconds()) < 2]
    fill_matches = []
    for t in near:
        expected = mark_for_side(side, t["bid"], t["ask"])
        fill_matches.append(
            {
                "recorded_at": t["recorded_at"],
                "ask": t["ask"],
                "expected_mark": expected,
                "fill": exit_px,
                "diff": exit_px - expected,
                "match": abs(exit_px - expected) < 1e-12 or abs(exit_px / expected - 1) < 1e-9,
            }
        )

    # gap between last_ok and first_hit
    gap_sec = None
    jump = None
    if last_ok and first_hit:
        gap_sec = (parse_iso(first_hit["recorded_at"]) - parse_iso(last_ok["recorded_at"])).total_seconds()
        jump = {
            "ask_from": last_ok["ask"],
            "ask_to": first_hit["ask"],
            "ask_jump_pct": (first_hit["ask"] - last_ok["ask"]) / last_ok["ask"],
            "mark_from": last_ok["mark"],
            "mark_to": first_hit["mark"],
        }

    # processing delay: first_hit to close
    proc_delay = None
    if first_hit:
        proc_delay = (close_dt - parse_iso(first_hit["recorded_at"])).total_seconds()

    # STALE / HTTP in decisions around window
    stale_hits = [d for d in decisions if d.get("reason") == "STALE_QUOTE" or "HTTP" in str(d.get("reason", ""))]
    dq = []
    dq_path = RUN / "data_quality.jsonl"
    if dq_path.exists():
        for row in load_jsonl(dq_path):
            dt = parse_iso(row.get("recorded_at") or row.get("ts"))
            if dt and parse_iso("2026-11-19T13:54:00+08:00") <= dt <= parse_iso("2026-11-19T14:01:00+08:00"):
                if "PIPPIN" in json.dumps(row):
                    dq.append(row)

    # key points (<=10)
    keys = []
    if last_ok:
        keys.append({"tag": "last_no_hit", **last_ok})
    if first_hit:
        keys.append({"tag": "first_observable_hit", **first_hit})
    # mid samples between
    if last_ok and first_hit:
        mid = [t for t in timeline if parse_iso(last_ok["recorded_at"]) < parse_iso(t["recorded_at"]) < parse_iso(first_hit["recorded_at"])]
        for t in mid[:2]:
            keys.append({"tag": "between", **t})
    # quotes immediately before close
    for t in timeline:
        if parse_iso(t["recorded_at"]) and close_dt and 0 <= (close_dt - parse_iso(t["recorded_at"])).total_seconds() <= 5:
            keys.append({"tag": "near_fill", **t})
    keys.append(
        {
            "tag": "fill",
            "recorded_at": close_f["recorded_at"],
            "price": exit_px,
            "effective_stop": close_f.get("effective_stop"),
            "exit_reason": close_f.get("exit_reason"),
            "book_ts": close_f.get("book_ts"),
        }
    )
    # spread at fill
    if near:
        t0 = near[-1]
        keys.append(
            {
                "tag": "spread_at_near_fill",
                "bid": t0["bid"],
                "ask": t0["ask"],
                "spread_pct": (t0["ask"] - t0["bid"]) / ((t0["ask"] + t0["bid"]) / 2),
                "slip_component": t0["ask"] * SLIP,
            }
        )

    # decompose excess vs stop
    # fill - stop = (ask*(1+slip) - stop) at trigger quote
    decomp = {}
    if first_hit:
        ask = first_hit["ask"]
        mark = first_hit["mark"]
        decomp = {
            "at_first_hit_ask": ask,
            "at_first_hit_mark": mark,
            "stop": stop,
            "gap_ask_vs_stop": ask - stop,
            "sim_slip_u": ask * SLIP * qty,
            "mark_vs_stop_u": (mark - stop) * qty,
            "fill_vs_mark_u": (exit_px - mark) * qty if near else None,
            "fill_vs_stop_u": extra_loss_u,
            "note": "excess = (ask-stop)*qty + ask*slip*qty + (fill-mark)*qty; poll gap may mean ask already jumped",
        }

    add_ev(
        "PIPPIN",
        "pack_.../01_PIPPIN_1354_1401/fills_PIPPINUSDT.jsonl",
        f"L{open_f['_line']}-L{close_f['_line']}",
        open_f["recorded_at"],
        open_f["trade_id"],
        f"SHORT entry={entry} stop={stop} exit={exit_px} net≈{complete_net:.4f}",
        "已證實：fills 為 SHORT；INITIAL_STOP 成交價高於 initial_stop",
    )

    return {
        "side": side,
        "open_at": open_f["recorded_at"],
        "entry": entry,
        "qty": qty,
        "initial_stop": stop,
        "close_at": close_f["recorded_at"],
        "exit": exit_px,
        "open_fee": open_fee,
        "close_fee": close_fee,
        "complete_net": complete_net,
        "close_net_u_field": float(close_f["net_u"]),
        "gross_u": float(close_f.get("gross_u") or 0),
        "stop_dist_pct": stop_dist_pct,
        "theor_price_risk_u": theor_price_risk,
        "actual_price_loss_u": actual_price_loss,
        "overshoot_pct_vs_stop": overshoot_pct,
        "extra_loss_u": extra_loss_u,
        "extra_vs_theor_mult": mult,
        "stop_events_n": len(stops),
        "code_note": code_note,
        "last_ok": last_ok,
        "first_hit": first_hit,
        "gap_sec": gap_sec,
        "jump": jump,
        "proc_delay_sec": proc_delay,
        "fill_matches": fill_matches,
        "key_points": keys[:10],
        "decomp": decomp,
        "stale_decision_n": len(stale_hits),
        "mfe_u": close_f.get("mfe_u"),
        "mae_u": close_f.get("mae_u"),
        "excursion_source": close_f.get("excursion_source"),
        "funding_data_gap": close_f.get("funding_data_gap"),
        "hold_sec": close_f.get("hold_sec"),
        "n_quotes": len(quotes),
        "pos_events_n": len(pos),
    }


def structure_window(klines: list[dict], entry_dt: datetime, side: str) -> dict:
    # bars strictly before entry minute
    before = [k for k in klines if k["dt"] + timedelta(minutes=1) <= entry_dt.replace(second=0, microsecond=0) or k["dt"] < entry_dt]
    # use closed bars with open_time + 1m <= entry
    usable = [k for k in klines if k["dt"] + timedelta(minutes=1) <= entry_dt]
    out = {}
    for mins in (5, 10, 15):
        window = usable[-mins:] if len(usable) >= mins else usable
        if not window:
            out[f"pre_{mins}m"] = None
            continue
        out[f"pre_{mins}m"] = {
            "n": len(window),
            "complete": len(window) == mins,
            "open": window[0]["open"],
            "high": max(x["high"] for x in window),
            "low": min(x["low"] for x in window),
            "close": window[-1]["close"],
            "vol_sum": sum(x["volume"] for x in window),
            "ret": (window[-1]["close"] - window[0]["open"]) / window[0]["open"],
        }
    # signal bar: last closed before entry
    sig = usable[-1] if usable else None
    out["signal_bar"] = (
        {
            "dt": sig["dt"].isoformat(),
            "o": sig["open"],
            "h": sig["high"],
            "l": sig["low"],
            "c": sig["close"],
            "vol": sig["volume"],
            "available_at_entry": True,
        }
        if sig
        else None
    )
    # post 1/3/5 — these are AFTER entry; for description only, not as signal inputs
    entry_floor = entry_dt.replace(second=0, microsecond=0)
    after = [k for k in klines if k["dt"] >= entry_floor]
    for mins in (1, 3, 5):
        w = after[:mins]
        out[f"post_{mins}m"] = (
            {
                "n": len(w),
                "open": w[0]["open"],
                "high": max(x["high"] for x in w),
                "low": min(x["low"] for x in w),
                "close": w[-1]["close"],
                "ret": (w[-1]["close"] - w[0]["open"]) / w[0]["open"],
                "note": "after-entry path; not used as signal",
            }
            if w
            else None
        )
    return out


def analyze_batch(fills_all) -> dict[str, Any]:
    d2 = PACK / "02_batch_1403_TAO_FET_BTC"
    pack_fills = load_jsonl(d2 / "fills_TAO_FET.jsonl")
    quotes = load_jsonl(d2 / "quote_events_TAO_FET_1400_1410.jsonl")
    decisions = load_jsonl(d2 / "decision_trace_TAO_FET_1400_1410.jsonl")

    # 14:03 opens from root fills (deduped list)
    opens_1403 = [
        f
        for f in fills_all
        if f.get("event") == "OPEN"
        and parse_iso(f["recorded_at"])
        and parse_iso(f["recorded_at"]) >= parse_iso("2026-11-19T14:03:00+08:00")
        and parse_iso(f["recorded_at"]) < parse_iso("2026-11-19T14:04:00+08:00")
    ]
    # exact second cluster
    cluster = [f for f in opens_1403 if "14:03:11" in f["recorded_at"] or "14:03:1" in f["recorded_at"]]
    if not cluster:
        cluster = opens_1403

    tao = [f for f in pack_fills if f["symbol"] == "TAOUSDT"]
    fet = [f for f in pack_fills if f["symbol"] == "FETUSDT"]
    # exclude early TAO
    tao = [f for f in tao if parse_iso(f["recorded_at"]) >= parse_iso("2026-11-19T14:00:00+08:00")]

    def one_sym(events):
        o = next(x for x in events if x["event"] == "OPEN")
        c = next(x for x in events if x["event"] == "CLOSE")
        entry, stop, exit_px = float(o["price"]), float(o["initial_stop"]), float(c["price"])
        qty = float(o["qty"])
        side = o["side"]
        mark_excess = None
        # quotes for overshoot
        sq = [q for q in quotes if q.get("symbol") == o["symbol"]]
        first_hit = None
        last_ok = None
        for q in sq:
            bid, ask = float(q["bid"]), float(q["ask"])
            mark = mark_for_side(side, bid, ask)
            hit = stop_hit(side, mark, stop)
            if not hit:
                last_ok = q
            if hit and first_hit is None:
                first_hit = {"ask": ask, "bid": bid, "mark": mark, "recorded_at": q["recorded_at"], "line": q["_line"]}
        extra = (exit_px - stop) * qty if side == "SHORT" else (stop - exit_px) * qty
        open_fee = float(o["fee_u"])
        complete = float(c["net_u"]) - open_fee if o.get("realized_net_u") is None else float(c["net_u"]) + float(
            o.get("realized_net_u") or -open_fee
        )
        return {
            "trade_id": o["trade_id"],
            "side": side,
            "open_at": o["recorded_at"],
            "entry": entry,
            "qty": qty,
            "atr": o.get("atr"),
            "initial_stop": stop,
            "stop_dist_pct": abs(stop - entry) / entry,
            "theor_risk_u": abs(stop - entry) * qty,
            "close_at": c["recorded_at"],
            "exit": exit_px,
            "exit_reason": c.get("exit_reason"),
            "hold_sec": c.get("hold_sec"),
            "gross_u": c.get("gross_u"),
            "close_net_u": c.get("net_u"),
            "open_fee": open_fee,
            "close_fee": c.get("fee_u"),
            "complete_net": complete,
            "extra_vs_stop_u": extra,
            "overshoot_pct": (exit_px - stop) / stop if side == "SHORT" else (stop - exit_px) / stop,
            "first_hit": first_hit,
            "last_ok_at": last_ok.get("recorded_at") if last_ok else None,
            "source_tags": o.get("source_tags"),
            "signal_open_time": o.get("signal_open_time"),
            "signal_key": o.get("signal_key"),
            "margin_u": o.get("margin_u"),
            "notional_u": o.get("notional_u"),
        }

    tao_a = one_sym(tao)
    fet_a = one_sym(fet)

    tao_k = load_klines(d2 / "TAOUSDT_1m_1330_1420.csv")
    fet_k = load_klines(d2 / "FETUSDT_1m_1330_1420.csv")
    btc_k = load_klines(d2 / "BTCUSDT_1m_1330_1420.csv")
    kq = {
        "TAO": check_klines(d2 / "TAOUSDT_1m_1330_1420.csv", "TAO"),
        "FET": check_klines(d2 / "FETUSDT_1m_1330_1420.csv", "FET"),
        "BTC": check_klines(d2 / "BTCUSDT_1m_1330_1420.csv", "BTC"),
    }

    entry_dt = parse_iso(tao_a["open_at"])
    struct = {
        "TAO": structure_window(tao_k, entry_dt, tao_a["side"]),
        "FET": structure_window(fet_k, entry_dt, fet_a["side"]),
        "BTC": structure_window(btc_k, entry_dt, "n/a"),
    }

    # derived 10m/15m before entry (completed only)
    def completed_derived(kl, tf, entry_dt):
        bars = aggregate_tf(kl, tf)
        # only buckets fully ended before entry
        done = []
        for b in bars:
            end = b["open_time"] + tf * 60_000
            end_dt = datetime.fromtimestamp(end / 1000.0, tz=TZ)
            if end_dt <= entry_dt and b["complete"]:
                done.append(b)
        return done[-3:] if done else []

    derived = {
        "TAO_10m": completed_derived(tao_k, 10, entry_dt),
        "TAO_15m": completed_derived(tao_k, 15, entry_dt),
        "FET_10m": completed_derived(fet_k, 10, entry_dt),
        "BTC_10m": completed_derived(btc_k, 10, entry_dt),
    }

    # BTC direction same window
    btc_pre = struct["BTC"].get("pre_10m")
    tao_pre = struct["TAO"].get("pre_10m")
    fet_pre = struct["FET"].get("pre_10m")

    # signal_open_time availability
    def sig_avail(a, kl):
        sot = a.get("signal_open_time")
        if sot is None:
            return None
        sot = int(sot)
        if sot < 1e12:
            sot *= 1000
        bar_end = datetime.fromtimestamp(sot / 1000.0 + 60, tz=TZ)
        return {
            "signal_open_time_ms": sot,
            "signal_bar_end": bar_end.isoformat(),
            "entry": a["open_at"],
            "bar_closed_before_entry": bar_end <= parse_iso(a["open_at"]),
            "bar_in_slice": any(k["open_time"] == sot for k in kl),
        }

    # six opens cluster summary — complete nets from trades map later; compute here from fills_all
    cluster_detail = []
    for o in sorted(cluster, key=lambda x: x["symbol"]):
        tid = o["trade_id"]
        related = [f for f in fills_all if f.get("trade_id") == tid]
        tmap = build_trades(related)
        t = tmap[tid]
        cluster_detail.append(
            {
                "symbol": o["symbol"],
                "side": o["side"],
                "open_at": o["recorded_at"],
                "notional_u": o.get("notional_u"),
                "margin_u": o.get("margin_u"),
                "trade_id": tid,
                "status": t.status,
                "complete_net": t.complete_net,
            }
        )

    # fake breakout evidence: post 1-5 reverse vs signal direction
    def reclaim(side, struct_sym):
        post = struct_sym.get("post_5m")
        sig = struct_sym.get("signal_bar")
        if not post or not sig:
            return None
        # SHORT breakout down: reclaim if post close back above signal low/open area
        if side == "SHORT":
            return {
                "signal_close": sig["c"],
                "post5_high": post["high"],
                "post5_close": post["close"],
                "reclaimed_above_signal_close": post["high"] > sig["c"] and post["close"] > sig["c"],
                "adverse_ret": post["ret"],  # for short, positive ret is adverse
            }
        return {
            "signal_close": sig["c"],
            "post5_low": post["low"],
            "post5_close": post["close"],
            "reclaimed_below_signal_close": post["low"] < sig["c"] and post["close"] < sig["c"],
            "adverse_ret": post["ret"],
        }

    add_ev(
        "BATCH1403",
        "pack_.../02_batch_1403_TAO_FET_BTC/fills_TAO_FET.jsonl",
        tao_a["trade_id"],
        tao_a["open_at"],
        tao_a["trade_id"],
        f"TAO {tao_a['side']} net={tao_a['complete_net']:.2f} hold={tao_a['hold_sec']}",
        "已證實：14:03 TAO 與 12:47 TAO 為不同 trade_id",
    )

    return {
        "tao": tao_a,
        "fet": fet_a,
        "kq": kq,
        "struct": struct,
        "derived": {
            k: [
                {
                    "dt": b["dt"].isoformat(),
                    "o": b["open"],
                    "h": b["high"],
                    "l": b["low"],
                    "c": b["close"],
                    "vol": b["volume"],
                    "complete": b["complete"],
                    "derived": True,
                }
                for b in v
            ]
            for k, v in derived.items()
        },
        "sig_avail": {
            "TAO": sig_avail(tao_a, tao_k),
            "FET": sig_avail(fet_a, fet_k),
        },
        "btc_corr_note": {
            "btc_pre10_ret": btc_pre["ret"] if btc_pre else None,
            "tao_pre10_ret": tao_pre["ret"] if tao_pre else None,
            "fet_pre10_ret": fet_pre["ret"] if fet_pre else None,
            "same_sign_tao_btc": (
                (btc_pre["ret"] * tao_pre["ret"] > 0) if btc_pre and tao_pre else None
            ),
            "caveat": "相關≠因果",
        },
        "reclaim": {"TAO": reclaim(tao_a["side"], struct["TAO"]), "FET": reclaim(fet_a["side"], struct["FET"])},
        "cluster": cluster_detail,
        "cluster_notional": sum(float(x.get("notional_u") or 0) for x in cluster_detail),
        "cluster_margin": sum(float(x.get("margin_u") or 0) for x in cluster_detail),
        "cluster_complete_net": sum(float(x["complete_net"] or 0) for x in cluster_detail if x["complete_net"] is not None),
        "decisions_n": len(decisions),
        "stop_events_pack_n": len(load_jsonl(d2 / "stop_events_TAO_FET.jsonl")),
    }


def analyze_lit_jup(fills_all) -> dict[str, Any]:
    d3 = PACK / "03_LIT_vs_JUP"
    # cross-run: find full trade from fills_all
    def trade_report(symbol: str) -> dict:
        # prefer successful closed trades for symbol up to cutoff
        tids = []
        for f in fills_all:
            if f.get("symbol") == symbol and f.get("event") == "OPEN":
                tids.append(f["trade_id"])
        # unique preserve order
        seen = set()
        uniq = []
        for t in tids:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        reports = []
        for tid in uniq:
            related = [f for f in fills_all if f.get("trade_id") == tid]
            tm = build_trades(related)[tid]
            if tm.status != "complete":
                continue
            o = tm.opens[0]
            partials = tm.partials
            finals = tm.closes
            stop_ev = []
            for p in (d3 / f"stop_events_{symbol}.jsonl", RUN / "stop_events.jsonl"):
                if p.exists():
                    for s in load_jsonl(p):
                        if s.get("trade_id") == tid or (s.get("symbol") == symbol and parse_iso(s.get("recorded_at") or "") and parse_iso(o["recorded_at"]) <= parse_iso(s.get("recorded_at")) <= parse_iso(finals[-1]["recorded_at"])):
                            stop_ev.append(s)
            # dedupe stop
            sk = set()
            stop_u = []
            for s in stop_ev:
                k = (s.get("kind"), s.get("new_stop"), s.get("event_ts") or s.get("recorded_at"))
                if k in sk:
                    continue
                sk.add(k)
                stop_u.append(s)
            # widen check
            widen = False
            prev = None
            side = o["side"]
            for s in sorted(stop_u, key=lambda x: float(x.get("event_ts") or 0)):
                ns = float(s.get("new_stop") or 0)
                if prev is not None:
                    if side == "LONG" and ns < prev - 1e-12:
                        widen = True
                    if side == "SHORT" and ns > prev + 1e-12:
                        widen = True
                prev = ns

            qty_parts = sum(float(p.get("qty") or 0) for p in partials) + sum(float(c.get("qty") or 0) for c in finals)
            entry = float(o["price"])
            stop0 = float(o["initial_stop"])
            qty = float(o["qty"])
            risk_u = abs(stop0 - entry) * qty
            c = finals[-1]
            reports.append(
                {
                    "trade_id": tid,
                    "symbol": symbol,
                    "side": o["side"],
                    "source_tags": o.get("source_tags"),
                    "run_id_open": o.get("run_id"),
                    "open_at": o["recorded_at"],
                    "entry": entry,
                    "qty": qty,
                    "initial_stop": stop0,
                    "risk_u": risk_u,
                    "open_fee": float(o["fee_u"]),
                    "partials": [
                        {
                            "at": p["recorded_at"],
                            "qty": p.get("qty"),
                            "pct_of_initial": float(p["qty"]) / qty if qty else None,
                            "price": p.get("price"),
                            "net_u": p.get("net_u"),
                            "fee_u": p.get("fee_u"),
                            "exit_reason": p.get("exit_reason") or p.get("event"),
                        }
                        for p in partials
                    ],
                    "final": {
                        "at": c["recorded_at"],
                        "qty": c.get("qty"),
                        "price": c.get("price"),
                        "net_u": c.get("net_u"),
                        "fee_u": c.get("fee_u"),
                        "exit_reason": c.get("exit_reason"),
                        "mfe_u": c.get("mfe_u"),
                        "mae_u": c.get("mae_u"),
                        "mfe_ts": c.get("mfe_ts"),
                        "mae_ts": c.get("mae_ts"),
                        "excursion_source": c.get("excursion_source"),
                        "hold_sec": c.get("hold_sec"),
                        "partial_tp_done": c.get("partial_tp_done"),
                    },
                    "complete_net": tm.complete_net,
                    "fees_total": tm.fees,
                    "qty_closed_sum": qty_parts,
                    "qty_match": abs(qty_parts - qty) < 1e-4,
                    "stop_updates": [
                        {
                            "kind": s.get("kind"),
                            "prev": s.get("prev_stop"),
                            "new": s.get("new_stop"),
                            "ts": s.get("recorded_at") or s.get("event_ts"),
                            "never_widens_flag": s.get("never_widens"),
                        }
                        for s in stop_u
                    ],
                    "stop_widened": widen,
                    "funding_data_gap": c.get("funding_data_gap", True),
                }
            )
        return reports

    lit = trade_report("LITUSDT")
    jup = trade_report("JUPUSDT")

    # structure compare using pack klines
    lit_k = load_klines(d3 / "LITUSDT_1m_1245_1420.csv")
    jup_k = load_klines(d3 / "JUPUSDT_1m_1245_1420.csv")

    def enrich(rep, kl):
        if not rep:
            return None
        r = rep[-1]  # latest complete in window
        ed = parse_iso(r["open_at"])
        st = structure_window(kl, ed, r["side"])
        r["structure"] = st
        # first favorable from polled mfe_ts only
        r["mfe_note"] = "polled_quote_estimate; MFE may track remaining qty after partial"
        r["capture_rate"] = None  # explicitly not computed
        return r

    lit_r = enrich(lit, lit_k)
    jup_r = enrich(jup, jup_k)

    if lit_r:
        add_ev(
            "LIT",
            "fills.jsonl",
            lit_r["trade_id"],
            lit_r["open_at"],
            lit_r["trade_id"],
            f"net={lit_r['complete_net']:.2f} partials={len(lit_r['partials'])} exit={lit_r['final']['exit_reason']}",
            "已證實：完整交易含部分止盈後 TRAILING_STOP；挑選成功樣本",
        )
    if jup_r:
        add_ev(
            "JUP",
            "fills.jsonl",
            jup_r["trade_id"],
            jup_r["open_at"],
            jup_r["trade_id"],
            f"net={jup_r['complete_net']:.2f} open_run={jup_r['run_id_open']}",
            "已證實：JUP 開倉跨 run，需用 trade_id 串接",
        )

    return {"LIT": lit_r, "JUP": jup_r, "lit_all_complete": lit, "jup_all_complete": jup}


def review_compare(stats_a, stats_b, stats_c, review: dict) -> list[str]:
    notes = []
    notes.append(
        f"review 宣稱 OPEN={review.get('open')} CLOSE={review.get('close')} PARTIAL={review.get('partial')} "
        f"realized={review.get('realized')} winrate={review.get('winrate')} PF={review.get('pf')} expectancy={review.get('exp')}"
    )
    notes.append(
        f"A帳戶累計至cutoff: complete={stats_a['n_complete']} winrate={stats_a['win_rate']} total_net={stats_a['total_net']:.4f} "
        f"PF={stats_a['pf']} exp={stats_a['expectancy']}"
    )
    notes.append(
        f"B target run事件聚合: complete={stats_b['n_complete']} total_net={stats_b['total_net']:.4f} "
        f"(含跨run開倉在本run平倉者，完整淨損益含他run OPEN費)"
    )
    notes.append(
        f"C trade_id完整: complete={stats_c['n_complete']} winrate={stats_c['win_rate']} total_net={stats_c['total_net']:.4f} PF={stats_c['pf']}"
    )
    notes.append(
        "差異來源：review 勝率/期望以分段 CLOSE 筆數計（含多段CLOSE），非完整trade_id；"
        "fills分段淨損益合計≠帳戶realized（跨run繼承持倉／OPEN費記帳時點）。"
    )
    return notes


def main():
    meta = {
        "cutoff": CUTOFF.isoformat(),
        "timezone": "Asia/Taipei (+08)",
        "target_run": TARGET_RUN,
        "pack": str(PACK),
        "generated_at": datetime.now(TZ).isoformat(),
    }

    # time unit checks
    sample_fill = load_jsonl(RUN / "fills.jsonl")[:1]
    sample_q = load_jsonl(PACK / "01_PIPPIN_1354_1401" / "quote_events_PIPPINUSDT.jsonl")[:1]
    time_notes = []
    if sample_fill:
        f = sample_fill[0]
        ra = parse_iso(f["recorded_at"])
        bt = book_ts_to_dt(f.get("book_ts"))
        time_notes.append(
            {
                "kind": "fill",
                "recorded_at": f.get("recorded_at"),
                "book_ts": f.get("book_ts"),
                "book_ts_as_dt": bt.isoformat() if bt else None,
                "delta_sec": (ra - bt).total_seconds() if ra and bt else None,
                "unit": "book_ts≈epoch seconds",
            }
        )
    if sample_q:
        q = sample_q[0]
        # detect fields
        time_notes.append({"kind": "quote_keys", "keys": sorted(q.keys())})

    fills_root, conflicts_root = load_fills_deduped([ROOT / "fills.jsonl"], CUTOFF)
    fills_run, conflicts_run = load_fills_deduped([RUN / "fills.jsonl"], CUTOFF)
    # A: account cumulative = root fills to cutoff (deduped within root)
    # Also merge run-only if root missing? Prefer root as account ledger mirror; verify overlap
    fills_a = fills_root
    # B: run only
    fills_b = fills_run
    # C: same as A for complete trade_id across runs (root has cross-run)
    fills_c = fills_root

    trades_a = build_trades(fills_a)
    trades_b = build_trades(fills_b)
    trades_c = build_trades(fills_c)
    stats_a = stats_from_complete(trades_a)
    stats_b = stats_from_complete(trades_b)
    stats_c = stats_from_complete(trades_c)

    # event counts A/B
    def ev_counts(fills):
        c = defaultdict(int)
        for f in fills:
            c[f.get("event")] += 1
        return dict(c)

    # review parse
    review_md = (PACK / "00_context" / "review.md").read_text()
    review = {
        "open": 17,
        "partial": 2,
        "close": 20,
        "realized": -1169.3353,
        "fees": 295.1675,
        "winrate": 0.2,
        "avg_win": 70.4241,
        "avg_loss": -47.3006,
        "rr": 1.4889,
        "pf": 0.3309,
        "exp": -53.1516,
        "segment_net": -569.7143,
        "note": "review stats mix segment closes; OPEN/CLOSE counts are events not complete trades",
    }

    # account start
    acct_start = json.loads((PACK / "00_context" / "account_at_start.json").read_text())
    start_realized = acct_start["account"]["realized_net_u"]
    start_fees = acct_start["account"]["realized_fees_u"]
    start_cash = acct_start["account"]["cash_u"]
    start_pos = list(acct_start["account"]["positions"].keys())

    # account at review time — use run review snapshot if equity file; else mark unable
    # Prefer account snapshot near cutoff from equity_snapshots
    equity_rows = load_jsonl(RUN / "equity_snapshots.jsonl")
    eq_at = None
    for e in equity_rows:
        dt = parse_iso(e.get("recorded_at"))
        if dt and dt <= CUTOFF:
            eq_at = e
    acct_reconcile = {
        "start_realized": start_realized,
        "start_fees": start_fees,
        "start_cash": start_cash,
        "start_positions": start_pos,
        "eq_at_or_before_cutoff": {
            "recorded_at": eq_at.get("recorded_at") if eq_at else None,
            "equity": eq_at.get("equity_u") or eq_at.get("equity") if eq_at else None,
            "realized": eq_at.get("realized_net_u") if eq_at else None,
            "cash": eq_at.get("cash_u") if eq_at else None,
        },
        "strict_same_timestamp_account_vs_review": False,
        "note": "無與 review 14:23:11 完全同時點之獨立帳戶快照時，標記無法嚴格驗證",
    }

    code_note = (
        "code_snapshot/portfolio.py mark_price_for_side: LONG→bid*(1-slip); SHORT→ask*(1+slip); "
        "stop trigger compares mark vs effective_stop; fill_price=mark; never backfill stop price. "
        "stop_events mainly log tighten_stop updates (BREAKEVEN/TRAIL), not every INITIAL_STOP hit."
    )

    pippin = analyze_pippin(fills_c, code_note)
    batch = analyze_batch(fills_c)
    litjup = analyze_lit_jup(fills_c)

    # kline QA for pack slices
    kline_qa = [
        check_klines(PACK / "01_PIPPIN_1354_1401" / "PIPPINUSDT_1m_1350_1405.csv", "PIPPIN_slice"),
        check_klines(PACK / "02_batch_1403_TAO_FET_BTC" / "TAOUSDT_1m_1330_1420.csv", "TAO_slice"),
        check_klines(PACK / "02_batch_1403_TAO_FET_BTC" / "FETUSDT_1m_1330_1420.csv", "FET_slice"),
        check_klines(PACK / "02_batch_1403_TAO_FET_BTC" / "BTCUSDT_1m_1330_1420.csv", "BTC_slice"),
        check_klines(PACK / "03_LIT_vs_JUP" / "LITUSDT_1m_1245_1420.csv", "LIT_slice"),
        check_klines(PACK / "03_LIT_vs_JUP" / "JUPUSDT_1m_1245_1420.csv", "JUP_slice"),
    ]

    gaps = []
    if pippin["stop_events_n"] == 0:
        gaps.append("PIPPIN stop_events 空：INITIAL_STOP 證據在 fills/position_events/quotes，不能解讀為未執行")
    if batch["stop_events_pack_n"] == 0:
        gaps.append("TAO/FET pack stop_events 空（同上）")
    if not (PACK / "03_LIT_vs_JUP" / "decision_OPEN_ish_JUPUSDT.jsonl").read_text().strip():
        gaps.append("JUP 開倉 decision 小包為空；開倉在前 run，已用 root fills 以 trade_id 補齊")
    gaps.append("funding_data_gap=True：淨損益不含完整資金費")
    gaps.append("無逐筆成交：無法定位市場真正首次穿價時刻，僅能用輪詢報價")
    gaps.append("六筆 14:03 集中進場僅 TAO/FET/BTC 有 K；其餘四標的行情結構缺口")

    # open fees already realized separately for still-open at cutoff
    open_fees_open_trades = sum(t.open_fee for t in trades_c.values() if t.status in ("open", "partial_still_open"))

    # segment-style stats like review (each CLOSE net_u, excluding open fee accounting)
    segment_nets = []
    for f in fills_a:
        if f.get("event") in ("CLOSE", "CLOSE_PARTIAL") or (
            f.get("event") == "CLOSE"
        ):
            if f.get("net_u") is not None:
                segment_nets.append(float(f["net_u"]))
    # Only CLOSE and CLOSE_PARTIAL / PARTIAL
    segment_nets = []
    for f in fills_a:
        if f.get("event") == "CLOSE" or f.get("event") == "CLOSE_PARTIAL":
            if f.get("net_u") is not None:
                segment_nets.append(float(f["net_u"]))
    seg_wins = [n for n in segment_nets if n > 0]
    seg_loss = [n for n in segment_nets if n <= 0]
    seg_stats = {
        "n": len(segment_nets),
        "wins": len(seg_wins),
        "losses": len(seg_loss),
        "win_rate": len(seg_wins) / len(segment_nets) if segment_nets else None,
        "total": sum(segment_nets),
        "avg_win": sum(seg_wins) / len(seg_wins) if seg_wins else 0,
        "avg_loss": sum(seg_loss) / len(seg_loss) if seg_loss else 0,
        "pf": (sum(seg_wins) / abs(sum(seg_loss))) if seg_loss and sum(seg_loss) != 0 else None,
        "exp": sum(segment_nets) / len(segment_nets) if segment_nets else None,
    }

    cmp_notes = review_compare(stats_a, stats_b, stats_c, review)

    # more evidence rows
    add_ev(
        "STATS",
        "00_context/review.md",
        "review_cutoff",
        CUTOFF.isoformat(),
        "",
        f"review realized={review['realized']} segment_net={review['segment_net']}",
        "已證實：review 同時列出帳戶realized與分段合計，兩者不同口徑",
    )
    add_ev(
        "STATS",
        "fixed_data_root/.../fills.jsonl",
        f"dedupe_conflicts={len(conflicts_root)}",
        CUTOFF.isoformat(),
        "",
        f"A_complete_net={stats_a['total_net']:.4f} C_same={stats_c['total_net']:.4f}",
        "已證實：去重鍵=trade_id+event+recorded_at+qty+price+exit_reason+fee+net+book_ts",
    )
    for i, x in enumerate(batch["cluster"][:6]):
        add_ev(
            "BATCH1403",
            "fills.jsonl",
            x["trade_id"],
            x["open_at"],
            x["trade_id"],
            f"{x['symbol']} {x['side']} notional={x['notional_u']} net={x['complete_net']}",
            "已證實：14:03 同批開倉之一（共同風險樣本）",
        )

    # PIPPIN timeline evidence
    if pippin["last_ok"]:
        add_ev(
            "PIPPIN",
            "01_PIPPIN.../quote_events_PIPPINUSDT.jsonl",
            f"L{pippin['last_ok']['line']}",
            pippin["last_ok"]["recorded_at"],
            open_f_tid if (open_f_tid := pippin and fills_c) else "",
            f"mark={pippin['last_ok']['mark']:.8f} ask={pippin['last_ok']['ask']} hit=False",
            "已證實：最後一筆可觀察未觸發報價",
        )
    # fix trade_id for above - redo properly
    EVIDENCE[:] = [e for e in EVIDENCE if not (e["theme"] == "PIPPIN" and "hit=False" in e["values"])]
    tid_p = None
    for f in fills_c:
        if f.get("symbol") == "PIPPINUSDT" and f.get("event") == "OPEN":
            tid_p = f["trade_id"]
            break
    if pippin["last_ok"]:
        add_ev(
            "PIPPIN",
            "pack_.../01_PIPPIN_1354_1401/quote_events_PIPPINUSDT.jsonl",
            f"L{pippin['last_ok']['line']}",
            pippin["last_ok"]["recorded_at"],
            tid_p,
            f"mark={pippin['last_ok']['mark']:.8f} ask={pippin['last_ok']['ask']} hit=False",
            "已證實：最後一筆可觀察未觸發報價",
        )
    if pippin["first_hit"]:
        add_ev(
            "PIPPIN",
            "pack_.../01_PIPPIN_1354_1401/quote_events_PIPPINUSDT.jsonl",
            f"L{pippin['first_hit']['line']}",
            pippin["first_hit"]["recorded_at"],
            tid_p,
            f"mark={pippin['first_hit']['mark']:.8f} ask={pippin['first_hit']['ask']} hit=True stop={pippin['initial_stop']}",
            "已證實：第一筆可觀察觸發（非市場真實首次穿價）",
        )
    if pippin["fill_matches"]:
        fm = pippin["fill_matches"][-1]
        add_ev(
            "PIPPIN",
            "fills+quotes",
            "fill_vs_mark",
            pippin["close_at"],
            tid_p,
            f"fill={fm['fill']} expected={fm['expected_mark']} diff={fm['diff']}",
            "已證實" if fm["match"] else "有跡象：成交價與鄰近報價 mark 公式核對",
        )
    add_ev(
        "PIPPIN",
        "code_snapshot/.../portfolio.py",
        "mark_price_for_side/manage_position",
        "",
        tid_p,
        "SHORT mark=ask*(1+0.0002); fill=mark; no stop backfill",
        "已證實：程式口徑",
    )
    add_ev(
        "LIMIT",
        "N/A",
        "tick_path",
        "",
        "",
        "no tick data",
        "無法判定：市場真實首次穿價時間",
    )

    result = {
        "meta": meta,
        "time_notes": time_notes,
        "gaps": gaps,
        "dedupe": {
            "key": "trade_id+event+recorded_at+qty+price+exit_reason+fee_u+net_u+book_ts",
            "conflicts_root": len(conflicts_root),
            "conflicts_run": len(conflicts_run),
            "policy": "exact key skip; soft-key numeric conflict counted; partial legs not merged away",
        },
        "event_counts": {"A_root": ev_counts(fills_a), "B_run": ev_counts(fills_b)},
        "stats": {"A": stats_a, "B": stats_b, "C": stats_c, "segment_like": seg_stats},
        "review": review,
        "compare_notes": cmp_notes,
        "acct_reconcile": acct_reconcile,
        "open_fees_still_open": open_fees_open_trades,
        "kline_qa": kline_qa,
        "pippin": pippin,
        "batch": batch,
        "litjup": litjup,
        "code_note": code_note,
    }

    # serialize datetimes
    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return str(o)
        return str(o)

    (OUT / "analysis_raw.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=default))

    # write evidence csv
    with (OUT / "EVIDENCE_COMPACT.csv").open("w", newline="") as fp:
        w = csv.DictWriter(
            fp,
            fieldnames=["theme", "source_rel", "row_or_key", "time", "trade_id", "values", "conclusion"],
        )
        w.writeheader()
        for e in EVIDENCE[:60]:
            w.writerow(e)

    print(json.dumps({"ok": True, "out": str(OUT), "evidence_n": len(EVIDENCE), "stats_C": stats_c, "pippin_extra": pippin["extra_loss_u"], "cluster_n": len(batch["cluster"])}, default=default))


if __name__ == "__main__":
    main()
