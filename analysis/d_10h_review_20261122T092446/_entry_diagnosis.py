#!/usr/bin/env python3
"""D entry quality diagnosis — 21 signals from signals.jsonl only (no fill-inferred signals)."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

OUT = Path(__file__).resolve().parent
SNAP = OUT / "snapshots"
TZ8 = timezone(timedelta(hours=8))
KLINE_VAULT = Path(
    r"c:\Users\Admin\MyProjects\automax\output\paper_bcd_local\data_c\data\testnet_c_inv_v1\kline_vault_v2_clean"
)
FEE = 0.0005


def parse_ts(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=TZ8)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ8)
    return dt.astimezone(TZ8)


def fnum(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def load_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]


_kl: dict[str, list[dict]] = {}


def load_kl(sym: str) -> list[dict]:
    if sym in _kl:
        return _kl[sym]
    p = KLINE_VAULT / f"{sym}_1m.csv"
    rows = []
    if p.is_file():
        with p.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    ot = datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=TZ8)
                except Exception:
                    continue
                o, h, l, c = map(fnum, (row.get("open"), row.get("high"), row.get("low"), row.get("close")))
                if None in (o, h, l, c):
                    continue
                rows.append({"ot": ot, "o": o, "h": h, "l": l, "c": c})
        rows.sort(key=lambda x: x["ot"])
    _kl[sym] = rows
    return rows


def last_closed_1m_before(kl: list[dict], t: datetime) -> Optional[dict]:
    """Last 1m bar whose close time <= t (known at decision)."""
    out = None
    for b in kl:
        close_t = b["ot"] + timedelta(minutes=1)
        if close_t <= t:
            out = {**b, "close_t": close_t}
        else:
            break
    return out


def closed_1m_ret_amp(kl: list[dict], t: datetime) -> dict:
    b = last_closed_1m_before(kl, t)
    if not b or b["o"] <= 0:
        return {"status": "UNKNOWN", "ret_pct": None, "amp_pct": None, "bar_ot": None}
    ret = (b["c"] - b["o"]) / b["o"] * 100.0
    amp = (b["h"] - b["l"]) / b["o"] * 100.0
    return {"status": "OK", "ret_pct": ret, "amp_pct": amp, "bar_ot": b["ot"].isoformat(), "o": b["o"], "c": b["c"]}


def quiet_still_at(signal: dict, t: datetime) -> dict:
    """Re-check quiet using signal's own completed bars only if still the latest known — else kline vault 15m proxy via 1m.
    Decision-time only: bars with close <= t.
    """
    # Prefer embedded signal bars (decision-time known)
    bars = signal.get("bars") or []
    if len(bars) >= 4:
        amps = []
        for b in bars[-4:]:
            o = fnum(b.get("o"))
            h = fnum(b.get("h"))
            l = fnum(b.get("l"))
            if not o or h is None or l is None or o <= 0:
                return {"status": "UNKNOWN", "reason": "embedded_bar_incomplete"}
            amps.append((h - l) / o)
        # range over window
        hs = [fnum(b.get("h")) for b in bars[-4:]]
        ls = [fnum(b.get("l")) for b in bars[-4:]]
        cs = [fnum(b.get("c")) for b in bars[-4:]]
        if None in hs + ls + cs or cs[-1] <= 0:
            return {"status": "UNKNOWN", "reason": "embedded_range_incomplete"}
        range_amp = (max(hs) - min(ls)) / cs[-1]
        bar_ok = all(a <= 0.0075 or a <= float(signal.get("B") or 0.0075) * 2 for a in amps)
        # Use signal's own B/thresholds as recorded, not new search
        B = fnum(signal.get("B"))
        range_amp_sig = fnum(signal.get("range_amp"))
        return {
            "status": "FROM_SIGNAL_EMBEDDED",
            "note": "embedded bars are the decision-time 15m set; not a live re-fetch",
            "bar_amps": amps,
            "range_amp_embedded": range_amp,
            "range_amp_signal_field": range_amp_sig,
            "B_signal": B,
            "still_matches_signal_fields": (
                range_amp_sig is not None and abs(range_amp - range_amp_sig) < 1e-9
            ),
        }
    return {"status": "UNKNOWN", "reason": "no_embedded_bars"}


def short_pnl(entry, exit_px, qty, entry_fee):
    gross = (entry - exit_px) * qty
    exit_fee = abs(exit_px * qty) * FEE
    return gross - exit_fee - entry_fee


def main() -> None:
    signals = [s for s in load_jsonl(SNAP / "signals.jsonl") if s.get("event") == "SIGNAL"]
    events = load_jsonl(SNAP / "events.jsonl")
    fills = load_jsonl(SNAP / "fills.jsonl")
    scan = load_jsonl(SNAP / "scan_events.jsonl")

    opens = {f["trade_id"]: f for f in fills if f.get("event") == "OPEN"}
    closes = {f["trade_id"]: f for f in fills if f.get("event") == "CLOSE"}

    queues = [e for e in events if e.get("event") == "QUEUE_LIMIT_SHORT"]
    cancels = [e for e in events if e.get("event") == "CANCEL_PENDING"]
    entries = [e for e in events if e.get("event") == "ENTRY_FILLED"]

    by_sid_queue = {q.get("signal_id"): q for q in queues if q.get("signal_id")}
    # cancels by symbol+near time / trade_id
    cancel_by_tid = {c.get("trade_id"): c for c in cancels if c.get("trade_id")}
    cancel_by_sym_time = defaultdict(list)
    for c in cancels:
        cancel_by_sym_time[c.get("symbol")].append(c)

    rows = []
    for s in signals:
        sid = s.get("signal_id")
        sym = s.get("symbol")
        emitted = parse_ts(s.get("emitted_at")) or parse_ts(s.get("price_ts"))
        # price_ts may be epoch seconds
        if emitted is None and s.get("price_ts") is not None:
            emitted = parse_ts(s.get("price_ts"))
        P = fnum(s.get("P"))
        q = by_sid_queue.get(sid)
        trade_id = (q or {}).get("trade_id")
        limit_px = fnum((q or {}).get("limit_px"))
        queued_at = parse_ts((q or {}).get("queued_at") or (q or {}).get("recorded_at"))

        open_f = opens.get(trade_id) if trade_id else None
        close_f = closes.get(trade_id) if trade_id else None
        entry_ev = next((e for e in entries if e.get("symbol") == sym and (not trade_id or True)), None)
        # prefer fill OPEN
        filled_at = parse_ts(open_f["recorded_at"]) if open_f else None
        cancel = cancel_by_tid.get(trade_id) if trade_id else None
        if cancel is None and q and not open_f:
            # unmatched cancel by symbol after queue
            for c in cancel_by_sym_time.get(sym) or []:
                ct = parse_ts(c.get("recorded_at"))
                if queued_at and ct and ct >= queued_at:
                    cancel = c
                    break

        link_status = "UNKNOWN"
        if q and open_f:
            link_status = "SIGNAL_QUEUE_FILL"
        elif q and cancel:
            link_status = "SIGNAL_QUEUE_CANCEL"
        elif q and not open_f and not cancel:
            link_status = "SIGNAL_QUEUE_NO_TERMINAL"
        elif not q:
            link_status = "SIGNAL_NO_QUEUE"

        latency_sec = None
        if emitted and filled_at:
            latency_sec = (filled_at - emitted).total_seconds()

        pre_1m = closed_1m_ret_amp(load_kl(sym), filled_at or queued_at or emitted) if (filled_at or queued_at or emitted) else {"status": "UNKNOWN"}
        # For fill path, use decision time = emitted (not post-fill)
        pre_1m_at_signal = closed_1m_ret_amp(load_kl(sym), emitted) if emitted else {"status": "UNKNOWN"}
        quiet = quiet_still_at(s, emitted) if emitted else {"status": "UNKNOWN"}

        rows.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "emitted_at": emitted.isoformat() if emitted else None,
                "P": P,
                "limit_px": limit_px,
                "range_amp": fnum(s.get("range_amp")),
                "bar_amps": s.get("bar_amps"),
                "B": fnum(s.get("B")),
                "T": fnum(s.get("T")),
                "slope": fnum(s.get("slope")),
                "spread": fnum(s.get("spread")),
                "link_status": link_status,
                "trade_id": trade_id,
                "queued_at": queued_at.isoformat() if queued_at else None,
                "cancel_reason": (cancel or {}).get("reason"),
                "cancel_at": (cancel or {}).get("recorded_at"),
                "filled_at": filled_at.isoformat() if filled_at else None,
                "fill_px": fnum(open_f["price"]) if open_f else None,
                "fill_qty": fnum(open_f["qty"]) if open_f else None,
                "signal_to_fill_sec": latency_sec,
                "exit_reason": (close_f or {}).get("exit_reason") if close_f else None,
                "pre_fill_1m_at_signal": pre_1m_at_signal,
                "quiet_check": quiet,
                "lifecycle": {
                    "queued": bool(q),
                    "filled": bool(open_f),
                    "canceled": bool(cancel) and not open_f,
                    "still_open": bool(open_f) and not close_f,
                },
            }
        )

    # Concurrent clustering by 15m bucket of emit time
    buckets = defaultdict(list)
    for r in rows:
        if not r["emitted_at"]:
            continue
        t = parse_ts(r["emitted_at"])
        bucket = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        buckets[bucket.isoformat()].append(r["symbol"])

    # Market baseline: BTC/ETH 1m ret over same emit windows (descriptive only)
    baselines = {}
    for name in ("BTCUSDT", "ETHUSDT"):
        kl = load_kl(name)
        baselines[name] = {}
        for r in rows:
            if not r["emitted_at"]:
                continue
            t = parse_ts(r["emitted_at"])
            baselines[name][r["signal_id"]] = closed_1m_ret_amp(kl, t)

    # Funnel
    funnel = {
        "signals_n": len(signals),
        "queued_n": sum(1 for r in rows if r["lifecycle"]["queued"]),
        "filled_n": sum(1 for r in rows if r["lifecycle"]["filled"]),
        "canceled_n": sum(1 for r in rows if r["lifecycle"]["canceled"]),
        "no_queue_n": sum(1 for r in rows if r["link_status"] == "SIGNAL_NO_QUEUE"),
        "unknown_terminal_n": sum(1 for r in rows if r["link_status"] == "SIGNAL_QUEUE_NO_TERMINAL"),
        "link_status_counts": defaultdict(int),
    }
    for r in rows:
        funnel["link_status_counts"][r["link_status"]] += 1
    funnel["link_status_counts"] = dict(funnel["link_status_counts"])

    # Peers at same emit minute
    for r in rows:
        t = parse_ts(r["emitted_at"]) if r["emitted_at"] else None
        peers = []
        if t:
            for o in rows:
                if o["signal_id"] == r["signal_id"]:
                    continue
                ot = parse_ts(o["emitted_at"]) if o["emitted_at"] else None
                if ot and abs((ot - t).total_seconds()) <= 900:
                    peers.append(o["symbol"])
        r["peers_within_15m"] = peers
        r["btc_1m_at_signal"] = baselines["BTCUSDT"].get(r["signal_id"])
        r["eth_1m_at_signal"] = baselines["ETHUSDT"].get(r["signal_id"])

    # Two forward hypotheses only (no threshold search on these 21)
    hypotheses = [
        {
            "id": "H1_CLUSTER_COOLDOWN",
            "statement": (
                "同 15m 桶內多標的同時觸發時，後續成交常共享同一波動事件；"
                "前瞻可測『同桶僅允許 1 個新掛／或冷卻』是否降低聚集虧損相關性。"
            ),
            "evidence_descriptive": {
                "buckets_with_ge_2": {k: v for k, v in buckets.items() if len(v) >= 2},
                "note": "描述性；非本樣本最佳化結果；未部署",
            },
        },
        {
            "id": "H2_POST_SIGNAL_1M_ADVERSE",
            "statement": (
                "訊號發出後、成交前若最近已收盤 1m 已出現對空頭不利的上漲振幅，"
                "前瞻可測『成交前再確認 1m 不利過濾』；僅用決策時已收盤 K，不用成交後路徑反推。"
            ),
            "evidence_descriptive": {
                "filled_with_1m_ret": [
                    {
                        "symbol": r["symbol"],
                        "ret_pct": (r["pre_fill_1m_at_signal"] or {}).get("ret_pct"),
                        "link": r["link_status"],
                    }
                    for r in rows
                    if r["lifecycle"]["filled"]
                ],
                "note": "描述性對照；未搜尋最佳門檻；未部署",
            },
        },
    ]

    out = {
        "generated_at": datetime.now(TZ8).isoformat(),
        "mother_set": "signals.jsonl EVENT=SIGNAL only",
        "n": len(signals),
        "funnel": funnel,
        "clusters_15m": {k: v for k, v in buckets.items()},
        "rows": rows,
        "hypotheses_forward_only": hypotheses,
        "limits": [
            "no fill-inferred signals",
            "no post-hoc threshold search on these 21",
            "cancels/unfilled are descriptive only — not claimed profitable if filled",
            "10 fills are not 10 independent validations when clustered",
        ],
    }
    (OUT / "entry_diagnosis_data.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("signals", len(signals), "funnel", funnel)
    for r in rows:
        print(r["symbol"], r["link_status"], r["emitted_at"], "lat", r["signal_to_fill_sec"], "cancel", r["cancel_reason"])


if __name__ == "__main__":
    main()
