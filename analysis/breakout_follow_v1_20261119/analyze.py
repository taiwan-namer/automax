#!/usr/bin/env python3
"""
Offline, read-only analysis of breakout_follow_v1 overnight run 20261119T024357_183e09f4.

- Reads the FIXED export pack (cutoff 2026-11-19T09:23:35+08:00) as the primary source.
- Extra sources are read separately and labelled (pre_overnight archive, live root fills, post-hoc klines).
- Does NOT import breakout_follow_v1 (no engine, no network, no account writes).
  Formulas (Wilder ATR, round_step, risk estimate, fees, marks, net unrealized)
  are re-implemented from the source files whose SHA-256 matches the run-start code_hashes.json.
- Writes only under analysis/breakout_follow_v1_20261119/out/.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import statistics as stats
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TZ = timezone(timedelta(hours=8))
RID = "20261119T024357_183e09f4"
PACK = Path("output/breakout_follow_v1/exports/20261119T024357_183e09f4_overnight_pack_20261119T092335")
RUN = Path("output/breakout_follow_v1/runs") / RID
PRE = Path("output/breakout_follow_v1/runs/pre_overnight_20261119T024356_1969a3e3")
ROOT_FILLS = Path("output/breakout_follow_v1/fills.jsonl")
POSTHOC = Path("analysis/breakout_follow_v1_20261119/posthoc_klines")
OUT = Path("analysis/breakout_follow_v1_20261119/out")

RUN_START = datetime.fromisoformat("2026-11-19T02:43:57+08:00")
CUTOFF = datetime.fromisoformat("2026-11-19T09:23:35.913096+08:00")
RUN_START_MS = int(RUN_START.timestamp() * 1000)
CUTOFF_MS = int(CUTOFF.timestamp() * 1000)

# --- config values (config.py sha256 matches run-start hash) ---
INITIAL = 10_000.0
MARGIN = 1_000.0
LEV = 10.0
NOTIONAL = 10_000.0
MAX_POS = 3
MAX_LOSS = 100.0
DAILY_LOCK = 300.0
ATR_MULT = 1.5
ATR_P = 14
RANGE_LB = 20
VOL_MA_P = 20
VOL_X_MIN = 3.0
BUF_ATR = 0.1
MAX_CHASE = 1.0
MAX_SPREAD = 0.0008
COOLDOWN_MIN = 3
REV_WIN_MIN = 30
MAX_REV = 2
PTP_FRAC = 0.30
PTP_NET = 100.0
TRAIL_MULT = 1.5
FEE = 0.0005
SLIP = 0.0002


def iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat(timespec="seconds")


def iso_s(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(p: Path) -> list[dict]:
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# --- re-implemented formulas (source: market_data.py / portfolio.py, hashes match run start) ---
def wilder_atr(h, l, c, period=ATR_P):
    n = len(h)
    if n < period + 1:
        return None
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = float(np.mean(tr[1 : period + 1]))
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr


def round_step(qty, step):
    if step <= 0:
        return qty
    precision = max(0, int(round(-np.log10(step)))) if step < 1 else 0
    n = np.floor(qty / step + 1e-12) * step
    return float(round(n, precision + 2))


def fee(notional):
    return abs(notional) * FEE


def pnl(side, entry, px, qty):
    return (px - entry) * qty if side == "LONG" else (entry - px) * qty


def mark(side, bid, ask):
    return bid * (1 - SLIP) if side == "LONG" else ask * (1 + SLIP)


def risk_est(side, entry, stop, qty):
    move = abs(entry - stop)
    exit_px = stop * (1 - SLIP) if side == "LONG" else stop * (1 + SLIP)
    return move * qty + fee(entry * qty) + fee(exit_px * qty)


# ---------------------------------------------------------------- loaders
class Bars:
    """One symbol's 1m bars as numpy arrays keyed by open_time."""

    def __init__(self, df: pd.DataFrame):
        df = df.sort_values("open_time").drop_duplicates("open_time", keep="first").reset_index(drop=True)
        self.ot = df["open_time"].to_numpy(dtype=np.int64)
        self.o = df["open"].to_numpy(dtype=float)
        self.h = df["high"].to_numpy(dtype=float)
        self.l = df["low"].to_numpy(dtype=float)
        self.c = df["close"].to_numpy(dtype=float)
        self.v = df["volume"].to_numpy(dtype=float)
        self.ct = df["close_time"].to_numpy(dtype=np.int64)
        self.idx = {int(t): i for i, t in enumerate(self.ot)}

    def upto(self, ot_inclusive: int) -> int:
        """number of bars with open_time <= ot_inclusive"""
        return int(np.searchsorted(self.ot, ot_inclusive, side="right"))

    def closed_before(self, now_ms: int) -> int:
        return int(np.searchsorted(self.ct, now_ms, side="left"))


def load_bars(folder: Path) -> dict[str, Bars]:
    out = {}
    for p in sorted(folder.glob("*_1m.csv")):
        sym = p.name[: -len("_1m.csv")]
        out[sym] = Bars(pd.read_csv(p))
    return out


def signal_on(b: Bars, s_ot: int):
    """Recompute signal-bar metrics exactly as signals.detect_breakout (closed = rows up to S)."""
    if s_ot not in b.idx:
        return None
    s = b.idx[s_ot]
    if s < max(RANGE_LB, VOL_MA_P, ATR_P + 1):
        return None
    lo = max(0, s - VOL_MA_P)
    vol_ma = float(np.mean(b.v[lo:s]))
    vol_x = b.v[s] / vol_ma if vol_ma > 0 else float("nan")
    rlo = max(0, s - RANGE_LB)
    rh = float(np.max(b.h[rlo:s]))
    rl = float(np.min(b.l[rlo:s]))
    alo = max(0, s - (ATR_P + 5))
    atr = wilder_atr(b.h[alo:s], b.l[alo:s], b.c[alo:s])
    if atr is None:
        return None
    close = float(b.c[s])
    ll = rh + BUF_ATR * atr
    sl = rl - BUF_ATR * atr
    side = "LONG" if close > ll else ("SHORT" if close < sl else None)
    return {
        "vol_x": vol_x, "vol_num": float(b.v[s]), "vol_ma": vol_ma, "range_high": rh, "range_low": rl,
        "atr": atr, "close": close, "long_level": ll, "short_level": sl, "side": side,
        "contiguous": bool(b.ot[s] - b.ot[lo] == (s - lo) * 60_000),
    }


def entry_checks(sig, bid, ask, spread):
    """SPREAD → FILL_NOT_BEYOND → CHASE, as signals.py."""
    if spread > MAX_SPREAD:
        return "SPREAD", None, None
    if sig["side"] == "LONG":
        raw = ask * (1 + SLIP)
        chase = (raw - sig["range_high"]) / sig["atr"]
        if raw <= sig["long_level"]:
            return "FILL_NOT_BEYOND_BREAKOUT", raw, chase
    else:
        raw = bid * (1 - SLIP)
        chase = (sig["range_low"] - raw) / sig["atr"]
        if raw >= sig["short_level"]:
            return "FILL_NOT_BEYOND_BREAKOUT", raw, chase
    if chase > MAX_CHASE:
        return "CHASE", raw, chase
    return None, raw, chase


def rel(a, b):
    if a is None or b is None:
        return float("inf")
    if a == b:
        return 0.0
    d = max(abs(a), abs(b), 1e-300)
    return abs(a - b) / d


# ---------------------------------------------------------------- main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    R: dict = {"labels": {}, "notes": []}

    # ============ 1. inventory ============
    inv = []
    files_read = 0
    snapshot_dir = PACK / "signals" / "decision_snapshots"
    for p in sorted(PACK.rglob("*")):
        if not p.is_file():
            continue
        files_read += 1
        if snapshot_dir in p.parents:
            continue
        rec = {"source": "PACK(cutoff 09:23:35)", "path": str(p.relative_to(PACK)), "bytes": p.stat().st_size, "sha256": sha256(p)}
        if p.suffix == ".jsonl":
            rows = read_jsonl(p)
            rec["records"] = len(rows)
            ts = [r.get("recorded_at") or r.get("ts") for r in rows if (r.get("recorded_at") or r.get("ts"))]
            if ts:
                rec["time_range"] = [min(ts), max(ts)]
        elif p.suffix == ".csv":
            with p.open(encoding="utf-8") as f:
                rec["records"] = max(0, sum(1 for _ in f) - 1)
        inv.append(rec)
    snaps = sorted(snapshot_dir.glob("*.json"))
    h = hashlib.sha256()
    tot = 0
    for p in snaps:
        b = p.read_bytes()
        tot += len(b)
        h.update(p.name.encode())
        h.update(b)
    inv.append({"source": "PACK(cutoff 09:23:35)", "path": "signals/decision_snapshots/*.json", "bytes": tot,
                "records": len(snaps), "sha256": "combined(name+bytes, sorted)=" + h.hexdigest()})
    extras = [
        (RUN / "capital_accounting_audit.md", "EXTRA: run dir (written 09:3x, after pack cutoff)"),
        (PRE / "account_state.json", "EXTRA: pre_overnight archive (frozen 02:43:56)"),
        (PRE / "fills.jsonl", "EXTRA: pre_overnight archive (frozen 02:43:56)"),
        (PRE / "equity_snapshots.jsonl", "EXTRA: pre_overnight archive (frozen 02:43:56)"),
        (PRE / "trades.csv", "EXTRA: pre_overnight archive (frozen 02:43:56)"),
        (ROOT_FILLS, "EXTRA: live root fills (still appended; read at analysis time)"),
        (POSTHOC / "_meta.json", "EXTRA: POST-HOC klines meta (fetched after run, NOT live input)"),
    ]
    for p, label in extras:
        files_read += 1
        rec = {"source": label, "path": str(p), "bytes": p.stat().st_size, "sha256": sha256(p)}
        if p.suffix == ".jsonl":
            rec["records"] = len(read_jsonl(p))
        inv.append(rec)
    ph_files = sorted(POSTHOC.glob("*_1m.csv"))
    files_read += len(ph_files)
    R["inventory"] = inv
    R["files_read"] = files_read

    # code hash comparison
    ch = json.loads((PACK / "config" / "code_hashes.json").read_text())
    code_cmp = []
    for k, v in ch["files"].items():
        p = Path(k)
        cur = sha256(p) if p.exists() else None
        code_cmp.append({"file": k, "run_start_sha256": v, "current_sha256": cur, "match": cur == v})
    R["code_hash_compare"] = code_cmp

    # ============ 2. klines quality ============
    arch = load_bars(PACK / "klines_1m")
    truth = load_bars(POSTHOC)
    R["kline_symbols"] = len(arch)
    kq = []
    total_bars = 0
    partial_stats = {"warmup": [0, 0], "test": [0, 0]}  # [complete, partial]
    for sym, b in sorted(arch.items()):
        raw = pd.read_csv(PACK / "klines_1m" / f"{sym}_1m.csv")
        total_bars += len(raw)
        dups = int(raw.duplicated("open_time").sum())
        order_bad = int((raw["open_time"].diff().dropna() <= 0).sum())
        gaps = []
        for i in range(1, len(b.ot)):
            d = (b.ot[i] - b.ot[i - 1]) // 60_000
            if d > 1:
                gaps.append([iso(b.ot[i - 1] + 60_000), iso(b.ot[i] - 60_000), int(d - 1)])
        unclosed_at_cutoff = int((b.ct >= CUTOFF_MS).sum())
        warm = int((b.ot < RUN_START_MS - 60_000).sum())
        t = truth.get(sym)
        comp = {"warmup": [0, 0], "test": [0, 0]}
        vol_ratio_test = []
        missing_in_truth = 0
        for i, ot in enumerate(b.ot):
            ot = int(ot)
            if t is None or ot not in t.idx:
                missing_in_truth += 1
                continue
            j = t.idx[ot]
            same = rel(b.v[i], t.v[j]) < 1e-9 and rel(b.c[i], t.c[j]) < 1e-12 and rel(b.h[i], t.h[j]) < 1e-12 and rel(b.l[i], t.l[j]) < 1e-12
            seg = "warmup" if ot < RUN_START_MS - 60_000 else "test"
            comp[seg][0 if same else 1] += 1
            partial_stats[seg][0 if same else 1] += 1
            if seg == "test" and t.v[j] > 0:
                vol_ratio_test.append(b.v[i] / t.v[j])
        kq.append({
            "symbol": sym, "bars": len(raw), "first": iso(b.ot[0]), "last": iso(b.ot[-1]),
            "warmup_bars_before_start": warm, "dups": dups, "order_violations": order_bad,
            "gap_count": len(gaps), "gap_minutes": sum(g[2] for g in gaps), "gaps": gaps[:5],
            "unclosed_at_cutoff": unclosed_at_cutoff,
            "is_closed_col_all_true": bool(raw["is_closed"].all()) if "is_closed" in raw else None,
            "warmup_complete_vs_posthoc": comp["warmup"][0], "warmup_differs": comp["warmup"][1],
            "test_complete_vs_posthoc": comp["test"][0], "test_differs": comp["test"][1],
            "test_vol_ratio_median": float(np.median(vol_ratio_test)) if vol_ratio_test else None,
            "not_in_posthoc": missing_in_truth,
            "posthoc_bars": len(t.ot) if t else 0,
        })
    R["kline_quality"] = kq
    R["kline_total_bars"] = total_bars
    R["kline_partial_stats"] = partial_stats

    # ============ 3. universe ============
    uni = read_jsonl(PACK / "universe" / "universe_history.jsonl")
    union = set()
    member_count = Counter()
    g_hist, l_hist = Counter(), Counter()
    for u in uni:
        union |= set(u["eligible_symbols"])
        member_count.update(u["eligible_symbols"])
        g_hist.update(x["symbol"] for x in u["top_gainers"])
        l_hist.update(x["symbol"] for x in u["top_losers"])
    R["universe"] = {
        "snapshots": len(uni), "first": uni[0]["recorded_at"], "last": uni[-1]["recorded_at"],
        "eligible_counts": [len(u["eligible_symbols"]) for u in uni],
        "union_size": len(union), "union_minus_klines": sorted(union - set(arch)),
        "klines_minus_union": sorted(set(arch) - union),
        "rank_basis": uni[0].get("rank_basis"), "min_quote_volume": uni[0].get("min_quote_volume_usdt"),
        "always_members": sum(1 for s in union if member_count[s] == len(uni)),
        "top_gainers_ever": len(g_hist), "top_losers_ever": len(l_hist),
        "first_top_gainers": [x["symbol"] for x in uni[0]["top_gainers"]],
        "first_top_losers": [x["symbol"] for x in uni[0]["top_losers"]],
        "last_top_gainers": [x["symbol"] for x in uni[-1]["top_gainers"]],
        "last_top_losers": [x["symbol"] for x in uni[-1]["top_losers"]],
        "refresh_gaps_min": [],
    }
    ts = [parse_iso(u["recorded_at"]) for u in uni]
    R["universe"]["refresh_gaps_min"] = sorted({round((b - a) / 60, 1) for a, b in zip(ts, ts[1:])})

    # ============ 4. decisions ============
    dec = read_jsonl(PACK / "signals" / "decision_trace.jsonl")
    R["decision_rows"] = len(dec)
    raw_cnt = Counter((d.get("final_action"), d.get("first_reject_code")) for d in dec)
    R["decision_raw_counts"] = [[a, b, n] for (a, b), n in raw_cnt.most_common()]
    scans = defaultdict(set)  # signal_open_time -> symbols evaluated
    for d in dec:
        ot = d.get("signal_open_time") or (d.get("vol_metric") or {}).get("numerator_open_time")
        if ot:
            scans[int(ot)].add(d["symbol"])
    s_list = sorted(scans)
    expected_minutes = list(range(s_list[0], s_list[-1] + 60_000, 60_000))
    missing_scans = [iso(m) for m in expected_minutes if m not in scans]
    R["scan_coverage"] = {
        "distinct_signal_bars": len(s_list), "first_S": iso(s_list[0]), "last_S": iso(s_list[-1]),
        "expected_minutes": len(expected_minutes), "missing_minutes": missing_scans,
        "symbols_per_scan_min": min(len(v) for v in scans.values()),
        "symbols_per_scan_max": max(len(v) for v in scans.values()),
        "scan_fail_rows": sum(1 for d in dec if d.get("event") == "SCAN_FAIL"),
    }
    # per-scan compare with universe size active at that time
    uni_ts = [(parse_iso(u["recorded_at"]), set(u["eligible_symbols"])) for u in uni]
    cover_short = []
    for S, syms in scans.items():
        t = S / 1000 + 62
        k = bisect.bisect_right([x[0] for x in uni_ts], t) - 1
        if k >= 0:
            missing = uni_ts[k][1] - syms
            if missing:
                cover_short.append([iso(S), sorted(missing)])
    R["scan_coverage"]["scans_missing_eligible"] = len(cover_short)
    R["scan_coverage"]["examples"] = cover_short[:5]

    # dedupe candidates
    def side_of(d):
        if d.get("side"):
            return d["side"]
        for c in d.get("conditions") or []:
            if c.get("code") == "BREAKOUT" and c.get("status") == "PASS":
                return (c.get("actual") or {}).get("side")
        return None

    uniq = {}
    for d in dec:
        ot = d.get("signal_open_time") or (d.get("vol_metric") or {}).get("numerator_open_time")
        key = (d["symbol"], ot, side_of(d))
        uniq.setdefault(key, []).append(d)
    dup_keys = {k: v for k, v in uniq.items() if len(v) > 1}
    R["decision_dedupe"] = {"unique_keys": len(uniq), "keys_with_repeats": len(dup_keys),
                            "repeat_examples": [[k[0], iso(k[1]) if k[1] else None, k[2], len(v), [x.get("first_reject_code") for x in v]] for k, v in list(dup_keys.items())[:5]]}
    last_by_key = {k: v[-1] for k, v in uniq.items()}
    # funnel by first reject (unique keys)
    order = ["INSUFFICIENT_BARS", "STALE_KLINE", "SIGNAL_DELAY", "INSUFFICIENT_PRIOR", "VOL_MA_ZERO", "DATA_INVALID",
             "VOL_X", "RANGE_INVALID", "ATR_INVALID", "NO_BREAKOUT", "QUOTE_MISSING", "STALE_QUOTE", "SPREAD",
             "FILL_NOT_BEYOND_BREAKOUT", "CHASE"]
    fr = Counter()
    for k, d in last_by_key.items():
        fa = d.get("final_action")
        code = d.get("first_reject_code")
        fr[(fa, code)] += 1
    R["decision_unique_counts"] = [[a, b, n] for (a, b), n in fr.most_common()]

    # lock split
    eq = read_jsonl(PACK / "equity" / "equity_snapshots.jsonl")
    lock_t = next(parse_iso(e["recorded_at"]) for e in eq if e.get("day_locked"))
    first_lock_dec = next(d["recorded_at"] for d in dec if d.get("first_reject_code") == "DAILY_LOSS_LOCK")
    R["lock"] = {"first_equity_locked": iso_s(lock_t), "first_lock_decision": first_lock_dec}
    pre_lock = Counter()
    post_lock = Counter()
    for k, d in last_by_key.items():
        t = parse_iso(d["recorded_at"])
        (pre_lock if t < lock_t else post_lock)[(d.get("final_action"), d.get("first_reject_code"))] += 1
    R["lock"]["pre_lock_unique"] = [[a, b, n] for (a, b), n in pre_lock.most_common()]
    R["lock"]["post_lock_unique"] = [[a, b, n] for (a, b), n in post_lock.most_common()]

    # ============ 5. quotes ============
    qev = read_jsonl(PACK / "quotes" / "quote_events.jsonl")
    sig_quote = {}
    for q in qev:
        if q.get("purpose") == "SIGNAL_EVAL" and q.get("signal_open_time"):
            sig_quote[(q["symbol"], int(q["signal_open_time"]))] = q
    manage_q = defaultdict(list)
    for q in qev:
        if q.get("purpose") in ("MANAGE", "MANAGE_POLL") and q.get("trade_id"):
            manage_q[q["trade_id"]].append(q)
    gaps_q = []
    for tid, qs in manage_q.items():
        ft = sorted(q["fetched_at"] for q in qs)
        mx = max((b - a for a, b in zip(ft, ft[1:])), default=0)
        gaps_q.append([tid.split(":")[1], len(qs), round(mx, 2)])
    R["quotes"] = {"rows": len(qev), "by_purpose": Counter(q.get("purpose") for q in qev),
                   "manage_quote_counts_maxgap_sec": gaps_q}

    # ============ 6. baseline replay: signal metrics ============
    rep = {"checked": 0, "match": 0, "mismatch": 0, "no_bar": 0, "examples": []}
    rep_entry = {"checked": 0, "match": 0, "mismatch": 0, "no_quote": 0, "examples": []}
    truth_cmp = Counter()  # (live_volx_pass, truth_volx_pass)
    truth_side_cmp = Counter()
    for d in dec:
        vm = d.get("vol_metric") or {}
        ot = vm.get("numerator_open_time")
        if not ot:
            continue
        b = arch.get(d["symbol"])
        sig = signal_on(b, int(ot)) if b else None
        if sig is None:
            rep["no_bar"] += 1
            continue
        rep["checked"] += 1
        ok = rel(sig["vol_x"], vm.get("vol_x")) < 1e-9 and rel(sig["vol_ma"], vm.get("denominator_mean")) < 1e-9
        rng = d.get("range") or {}
        if rng.get("high") is not None:
            ok = ok and rel(sig["range_high"], rng["high"]) < 1e-12 and rel(sig["range_low"], rng["low"]) < 1e-12
        atr_rec = d.get("atr")
        atr_rec = atr_rec.get("value") if isinstance(atr_rec, dict) else atr_rec
        if atr_rec is not None:
            ok = ok and rel(sig["atr"], atr_rec) < 1e-9
        if ok:
            rep["match"] += 1
        else:
            rep["mismatch"] += 1
            if len(rep["examples"]) < 5:
                rep["examples"].append({"symbol": d["symbol"], "S": iso(ot), "rec_vol_x": vm.get("vol_x"), "replay_vol_x": sig["vol_x"],
                                        "rec_atr": atr_rec, "replay_atr": sig["atr"]})
        t = truth.get(d["symbol"])
        tsig = signal_on(t, int(ot)) if t else None
        if tsig is not None:
            truth_cmp[(sig["vol_x"] >= VOL_X_MIN, tsig["vol_x"] >= VOL_X_MIN)] += 1
            truth_side_cmp[(sig["side"] if sig["vol_x"] >= VOL_X_MIN else None,
                            tsig["side"] if tsig["vol_x"] >= VOL_X_MIN else None)] += 1
        # entry-stage replay for rows that passed breakout
        side = side_of(d)
        if side and sig["side"] == side:
            q = sig_quote.get((d["symbol"], int(ot)))
            if not q:
                rep_entry["no_quote"] += 1
                continue
            code, raw, chase = entry_checks(sig, q["bid"], q["ask"], q["spread"])
            rec_code = d.get("first_reject_code") if d.get("final_action") == "NO_ENTRY" else None
            rep_entry["checked"] += 1
            if code == rec_code:
                rep_entry["match"] += 1
            else:
                rep_entry["mismatch"] += 1
                if len(rep_entry["examples"]) < 5:
                    rep_entry["examples"].append([d["symbol"], iso(ot), rec_code, code, chase])
    R["replay_signal"] = rep
    R["replay_entry"] = rep_entry
    R["volx_live_vs_posthoc"] = {f"live={a},posthoc={b}": n for (a, b), n in truth_cmp.items()}
    R["side_live_vs_posthoc"] = {f"live={a},posthoc={b}": n for (a, b), n in truth_side_cmp.most_common()}

    # ============ 7. accounting ============
    fills = read_jsonl(PACK / "trading" / "fills.jsonl")
    acc = json.loads((PACK / "trading" / "account_state.json").read_text())
    pre_acc = json.loads((PRE / "account_state.json").read_text())
    pre_eq = read_jsonl(PRE / "equity_snapshots.jsonl")
    root_fills = read_jsonl(ROOT_FILLS)
    start = eq[0]
    end = eq[-1]
    ident_err = max(abs(e["equity"] - (INITIAL + e["realized_net_u"] + e.get("floating_u", 0.0))) for e in eq)
    # replay cash/realized from start state with pack fills
    cash = start["cash"]
    realized = start["realized_net_u"]
    fees = start["realized_fees_u"]
    for f in fills:
        if f["event"] == "OPEN":
            cash -= MARGIN + f["fee_u"]
            realized -= f["fee_u"]
            fees += f["fee_u"]
        else:
            cash -= f["fee_u"]
            realized += f["net_u"]
            fees += f["fee_u"]
            if f["event"] == "CLOSE":
                cash += MARGIN
    gross_all = acc["realized_net_u"] + acc["realized_fees_u"] - acc.get("realized_funding_u", 0.0)
    # per-minute corrected free cash = cash + cumulative gross (all time) at that moment
    min_cash = min(e["cash"] for e in eq)
    corr = [e["cash"] + (e["realized_net_u"] + e["realized_fees_u"]) for e in eq]
    # correct definition candidate: equity_ex_float - used_margin
    alt = [INITIAL + e["realized_net_u"] - MARGIN * e["positions"] for e in eq]
    R["accounting"] = {
        "account_created_at": acc["created_at"], "account_initial": acc["initial_capital_u"],
        "pre_overnight_first_equity": pre_eq[0], "start_snapshot": start, "end_snapshot": end,
        "equity_identity_max_err": ident_err,
        "replay_cash": cash, "acct_cash": acc["cash_u"], "replay_realized": realized, "acct_realized": acc["realized_net_u"],
        "replay_fees": fees, "acct_fees": acc["realized_fees_u"], "funding": acc.get("realized_funding_u"),
        "funding_gap": acc.get("funding_data_gap"), "gross_all_time": gross_all,
        "cash_minus_equity_end": acc["cash_u"] - (INITIAL + acc["realized_net_u"]),
        "min_cash_u_during_run": min_cash, "min_corrected_cash_during_run": min(corr),
        "min_equity_minus_used_margin": min(alt),
        "insufficient_margin_rows": sum(1 for d in dec if d.get("first_reject_code") == "INSUFFICIENT_MARGIN"),
        "max_positions_seen": max(e["positions"] for e in eq),
    }
    # equity curves
    eqv = [e["equity"] for e in eq]
    peak = -1e18
    dd = 0
    pk_t = tr_t = None
    cur_pk_t = None
    for e in eq:
        if e["equity"] > peak:
            peak = e["equity"]
            cur_pk_t = e["recorded_at"]
        if e["equity"] - peak < dd:
            dd = e["equity"] - peak
            pk_t, tr_t = cur_pk_t, e["recorded_at"]
    hist = [{"t": x["ts"], "equity": x["equity"]} for x in pre_eq] + [{"t": e["recorded_at"], "equity": e["equity"]} for e in eq]
    peak = -1e18
    hdd = 0
    hpk = htr = None
    cur = None
    for x in hist:
        if x["equity"] > peak:
            peak = x["equity"]
            cur = x["t"]
        if x["equity"] - peak < hdd:
            hdd = x["equity"] - peak
            hpk, htr = cur, x["t"]
    R["drawdown"] = {
        "window_start_equity": eqv[0], "window_end_equity": eqv[-1], "window_change": eqv[-1] - eqv[0],
        "window_max": max(eqv), "window_min": min(eqv), "window_mdd": dd, "window_peak_t": pk_t, "window_trough_t": tr_t,
        "history_points": len(hist), "history_first": hist[0], "history_mdd": hdd, "history_peak_t": hpk, "history_trough_t": htr,
        "equity_snapshot_gaps_sec_max": max(parse_iso(b["recorded_at"]) - parse_iso(a["recorded_at"]) for a, b in zip(eq, eq[1:])),
    }
    # test pollution in root fills
    R["root_fills_test_rows"] = [f for f in root_fills if str(f.get("trade_id", "")).startswith("t:")]

    # ============ 8. per-trade replay ============
    stops = read_jsonl(PACK / "trading" / "stop_events.jsonl")
    stops_by = defaultdict(list)
    for s in stops:
        stops_by[s["trade_id"]].append(s)
    by_tid = defaultdict(list)
    for f in fills:
        by_tid[f["trade_id"]].append(f)
    dec_open = {d["trade_id"]: d for d in dec if d.get("final_action") == "OPEN"}
    root_open = {f["trade_id"]: f for f in root_fills if f["event"] == "OPEN"}
    filters = acc["filters"]
    trades = []
    for tid, fl in by_tid.items():
        sym = tid.split(":")[1]
        op = next((f for f in fl if f["event"] == "OPEN"), None)
        cl = [f for f in fl if f["event"] in ("CLOSE", "CLOSE_PARTIAL")]
        src = "THIS_RUN" if tid.startswith(RID) else "CROSS_RUN(2026-11-19T0232_a8c35e7a)"
        T = {"trade_id": tid, "symbol": sym, "source": src}
        if op:
            d = dec_open[tid]
            pos0 = None
            T.update({
                "side": op["side"], "entry_price": op["price"], "qty": op["qty"], "entry_time": op["recorded_at"],
                "entry_fee": op["fee_u"], "signal_open_time": d["vol_metric"]["numerator_open_time"],
                "decision_time": d["recorded_at"], "vol_x": d["vol_x"], "spread": d["spread"], "atr": d["atr"],
                "range_high": d["range"]["high"], "range_low": d["range"]["low"], "risk_estimate_u": d["risk_estimate_u"],
                "source_tags": d["source_tags"],
            })
            for c in d["conditions"]:
                if c["code"] == "CHASE":
                    T["chase_atr"] = c["actual"]
                if c["code"] == "FRESHNESS":
                    T["kline_delay_sec"] = c["actual"]["kline_delay_sec"]
                    T["quote_age_sec"] = c["actual"]["quote_age_sec"]
            q = sig_quote.get((sym, int(T["signal_open_time"])))
            T["signal_quote_fetched_at"] = iso_s(q["fetched_at"]) if q else None
            T["signal_quote_bid_ask"] = [q["bid"], q["ask"]] if q else None
            T["entry_origin"] = "pack fills OPEN + decision_trace"
            atr_entry = d["atr"]
            init_stop = T["entry_price"] - ATR_MULT * atr_entry if T["side"] == "LONG" else T["entry_price"] + ATR_MULT * atr_entry
            extreme = T["entry_price"]
            eff = init_stop
            entry_ts = parse_iso(op["recorded_at"])
            step = float(filters.get(sym, {}).get("stepSize") or 0.001)
            T["qty_replay"] = round_step(NOTIONAL / T["entry_price"], step)
            T["risk_replay"] = risk_est(T["side"], T["entry_price"], init_stop, T["qty"])
            T["entry_fee_replay"] = fee(T["entry_price"] * T["qty"])
        else:
            p = pre_acc["positions"][sym]
            ro = root_open.get(tid)
            T.update({
                "side": p["side"], "entry_price": p["entry_price"], "qty": p["qty_initial"], "entry_time": p["entry_time"],
                "entry_fee": p["fees_paid_u"], "signal_open_time": p["signal_open_time"], "decision_time": None,
                "vol_x": p["vol_x"], "atr": p["atr_at_entry"], "range_high": p["range_high"], "range_low": p["range_low"],
                "risk_estimate_u": None, "source_tags": p["source_tags"],
                "entry_origin": "EXTRA: pre_overnight/account_state.json + root fills OPEN" + (" (found)" if ro else " (root OPEN missing)"),
                "root_open_fill": ro,
            })
            init_stop = p["initial_stop"]
            eff = p["effective_stop"]
            extreme = p["extreme_price"]
            entry_ts = p["entry_ts"]
            T["state_at_run_start"] = {"effective_stop": eff, "extreme": extreme, "trailing_stop": p["trailing_stop"]}
        side = T["side"]
        T["notional"] = T["entry_price"] * T["qty"]
        T["initial_stop"] = init_stop
        # replay management on recorded quotes
        qs = sorted(manage_q.get(tid, []), key=lambda x: x["fetched_at"])
        qty = T["qty"]
        max_net = -1e18
        max_net_t = None
        mfe = -1e18
        mae = 1e18
        b = arch[sym]

        def _replay(frozen):
            ex = extreme
            ef = eff
            st_, ex_sim = [], None
            for qq in qs:
                m = mark(side, qq["bid"], qq["ask"])
                if not frozen:
                    ex = max(ex, m) if side == "LONG" else min(ex, m)
                if qq["purpose"] == "MANAGE":
                    now_ms = int(qq["fetched_at"] * 1000) + 500
                    n = b.closed_before(now_ms)
                    lo = max(0, n - (ATR_P + 5))
                    atr_l = wilder_atr(b.h[lo:n], b.l[lo:n], b.c[lo:n])
                    if atr_l:
                        trail = ex - TRAIL_MULT * atr_l if side == "LONG" else ex + TRAIL_MULT * atr_l
                        if (side == "LONG" and trail > ef) or (side == "SHORT" and trail < ef):
                            st_.append({"t": iso_s(qq["fetched_at"]), "prev": ef, "new": trail, "atr_latest": atr_l, "extreme_used": ex})
                            ef = trail
                hit = (side == "LONG" and m <= ef) or (side == "SHORT" and m >= ef)
                if hit:
                    g = pnl(side, T["entry_price"], m, qty)
                    ex_sim = {"t": iso_s(qq["fetched_at"]), "mark": m, "eff_stop": ef, "gross": g, "net_exit": g - fee(m * qty)}
                    break
            return st_, ex_sim

        for qq in qs:
            m = mark(side, qq["bid"], qq["ask"])
            g = pnl(side, T["entry_price"], m, qty)
            net = g - fee(m * qty)
            if net > max_net:
                max_net, max_net_t = net, iso_s(qq["fetched_at"])
            mfe = max(mfe, g)
            mae = min(mae, g)
        sim_stops, exit_sim = _replay(True)
        fx_stops, fx_exit = _replay(False)
        T["stop_updates_replayed_extreme_fixed"] = fx_stops
        T["exit_replay_extreme_fixed"] = fx_exit
        rec_stops = stops_by.get(tid, [])
        T["stop_updates_recorded"] = [{"t": iso_s(s["event_ts"]), "kind": s["kind"], "prev": s["prev_stop"], "new": s["new_stop"]} for s in rec_stops]
        T["stop_updates_replayed"] = sim_stops
        mono = all((s["new_stop"] > s["prev_stop"]) if side == "LONG" else (s["new_stop"] < s["prev_stop"]) for s in rec_stops)
        T["stops_monotonic_tighten"] = mono
        T["stop_replay_match"] = (len(sim_stops) == len(rec_stops) and all(rel(a["new"], b2["new_stop"]) < 1e-9 for a, b2 in zip(sim_stops, rec_stops)))
        T["max_net_unrealized_at_quotes"] = max_net
        T["max_net_unrealized_t"] = max_net_t
        T["partial_tp_reached"] = max_net >= PTP_NET
        T["mfe_gross_quotes"] = mfe
        T["mae_gross_quotes"] = mae
        T["manage_quotes"] = len(qs)
        T["exits"] = [{"t": c["recorded_at"], "event": c["event"], "price": c["price"], "qty": c["qty"], "fee": c["fee_u"], "gross": c["gross_u"],
                       "net": c["net_u"], "reason": c["exit_reason"], "book_ts": iso_s(c["book_ts"]) if c.get("book_ts") else None} for c in cl]
        T["exit_replay"] = exit_sim
        if exit_sim and cl:
            c = cl[-1]
            T["exit_replay_price_match"] = rel(exit_sim["mark"], c["price"]) < 1e-12
            T["exit_worse_than_stop_by"] = (exit_sim["eff_stop"] - c["price"]) * T["qty"] if side == "LONG" else (c["price"] - exit_sim["eff_stop"]) * T["qty"]
        T["lifecycle_net_in_run"] = sum(c["net_u"] for c in cl) - (T["entry_fee"] if op else 0.0)
        T["lifecycle_net_total"] = sum(c["net_u"] for c in cl) - T["entry_fee"]
        T["hold_sec"] = (parse_iso(cl[-1]["recorded_at"]) - entry_ts) if cl else None
        # kline MFE/MAE (posthoc truth, bars fully inside holding)
        t = truth.get(sym)
        exit_ts = parse_iso(cl[-1]["recorded_at"]) if cl else CUTOFF.timestamp()
        in_bars = [j for j in range(len(t.ot)) if t.ot[j] >= entry_ts * 1000 and t.ct[j] <= exit_ts * 1000] if t else []
        part_bars = [j for j in range(len(t.ot)) if (t.ot[j] < entry_ts * 1000 < t.ct[j]) or (t.ot[j] < exit_ts * 1000 < t.ct[j])] if t else []
        def ext(js):
            if not js:
                return None, None
            hi = max(t.h[j] for j in js)
            lo = min(t.l[j] for j in js)
            if side == "LONG":
                return (hi - T["entry_price"]) * T["qty"], (lo - T["entry_price"]) * T["qty"]
            return (T["entry_price"] - lo) * T["qty"], (T["entry_price"] - hi) * T["qty"]
        T["kline_full_bars_in_hold"] = len(in_bars)
        T["mfe_mae_full_bars_posthoc"] = ext(in_bars)
        T["mfe_mae_incl_partial_bars_posthoc"] = ext(sorted(set(in_bars) | set(part_bars)))
        # truth ATR at entry vs used ATR
        if T.get("signal_open_time"):
            ts_sig = signal_on(t, int(T["signal_open_time"])) if t else None
            T["posthoc_signal_bar"] = ts_sig
            T["archive_signal_bar"] = signal_on(arch[sym], int(T["signal_open_time"]))
        # reverse evidence during holding
        opp = []
        rows_hold = 0
        for d2 in dec:
            if d2["symbol"] != sym:
                continue
            tt = parse_iso(d2["recorded_at"])
            if not (entry_ts < tt <= exit_ts):
                continue
            rows_hold += 1
            s2 = side_of(d2)
            if s2 and s2 != side:
                opp.append([d2["recorded_at"], d2.get("first_reject_code"), d2.get("final_action")])
        T["decision_rows_during_hold"] = rows_hold
        T["opposite_breakout_rows_during_hold"] = opp
        trades.append(T)
    trades.sort(key=lambda x: x["entry_time"])
    R["trades"] = trades
    R["cross_run_start_floating"] = start.get("floating_u")

    # ============ 9. opportunity study ============
    def forward(sym, S, side, entry, horizons=(5, 15, 30, 60)):
        t = truth.get(sym)
        if t is None or S not in t.idx:
            return None
        i0 = t.idx[S] + 1
        out = {}
        for hh in horizons:
            i1 = i0 + hh - 1
            if i1 >= len(t.ot) or t.ot[i1] != S + hh * 60_000:
                out[hh] = None
                continue
            seg = slice(i0, i1 + 1)
            if side == "LONG":
                ret = t.c[i1] / entry - 1
                mfe = np.max(t.h[seg]) / entry - 1
                mae = np.min(t.l[seg]) / entry - 1
            else:
                ret = entry / t.c[i1] - 1
                mfe = entry / np.min(t.l[seg]) - 1
                mae = entry / np.max(t.h[seg]) - 1
            out[hh] = (ret, mfe, mae)
        return out

    def summarize(items):
        res = {}
        for hh in (5, 15, 30, 60):
            vals = [it[hh] for it in items if it and it.get(hh)]
            n_ins = sum(1 for it in items if it and it.get(hh) is None)
            if not vals:
                res[hh] = {"n": 0, "insufficient": n_ins}
                continue
            r = [v[0] for v in vals]
            res[hh] = {"n": len(r), "insufficient": n_ins, "mean_bps": 1e4 * float(np.mean(r)), "median_bps": 1e4 * float(np.median(r)),
                       "win_rate": float(np.mean([x > 0 for x in r])), "net_after_cost_mean_bps": 1e4 * float(np.mean(r)) - 1e4 * (2 * FEE + SLIP),
                       "mfe_median_bps": 1e4 * float(np.median([v[1] for v in vals])), "mae_median_bps": 1e4 * float(np.median([v[2] for v in vals]))}
        return res

    groups = defaultdict(list)
    for k, d in last_by_key.items():
        sym, S, side = k
        if not side or not S:
            continue
        q = sig_quote.get((sym, int(S)))
        if not q:
            continue
        entry = q["ask"] * (1 + SLIP) if side == "LONG" else q["bid"] * (1 - SLIP)
        label = d.get("first_reject_code") or d.get("final_action")
        groups[label].append(forward(sym, int(S), side, entry))
        groups["ALL_BREAKOUT_PASS(live data)"].append(forward(sym, int(S), side, entry))
    R["opportunity_live_candidates"] = {k: {"n_candidates": len(v), "stats": summarize(v)} for k, v in groups.items()}

    # truth-rule candidates over the same (symbol,S) evaluated
    tgroups = defaultdict(list)
    tcount = Counter()
    for S in s_list:
        for sym in scans[S]:
            t = truth.get(sym)
            if t is None:
                tcount["no_posthoc_bars"] += 1
                continue
            sg = signal_on(t, S)
            if sg is None:
                tcount["insufficient"] += 1
                continue
            tcount["evaluated"] += 1
            if not (sg["vol_x"] >= VOL_X_MIN):
                tcount["VOL_X"] += 1
                continue
            if sg["side"] is None:
                tcount["NO_BREAKOUT"] += 1
                continue
            q = sig_quote.get((sym, S))
            if not q:
                tcount["QUOTE_MISSING"] += 1
                continue
            code, raw, chase = entry_checks(sg, q["bid"], q["ask"], q["spread"])
            if code:
                tcount[code] += 1
                tgroups[code].append(forward(sym, S, sg["side"], raw if raw else (q["ask"] if sg["side"] == "LONG" else q["bid"])))
                continue
            tcount["SIGNAL"] += 1
            tgroups["SIGNAL"].append(forward(sym, S, sg["side"], raw))
    R["posthoc_rule_funnel"] = dict(tcount)
    R["opportunity_posthoc_rule"] = {k: {"n_candidates": len(v), "stats": summarize(v)} for k, v in tgroups.items()}

    # ============ 10. counterfactual simulator ============
    first_mng = (RUN_START_MS // 60_000) * 60_000
    init_positions = []
    for sym, p in pre_acc["positions"].items():
        init_positions.append({
            "tid": p["trade_id"], "symbol": sym, "side": p["side"], "entry": p["entry_price"], "qty0": p["qty_initial"],
            "qty_rem": p["qty_remaining"], "eff": p["effective_stop"], "init_stop": p["initial_stop"],
            "stop_kind": "INITIAL_STOP", "extreme": p["extreme_price"], "ptp_done": bool(p.get("partial_tp_done")),
            "entry_t": p["entry_time"], "S": int(p["signal_open_time"]), "first_manage_S": first_mng,
            "risk": None, "atr": p["atr_at_entry"], "vol_x": p["vol_x"], "linked": "CROSS_RUN",
        })
    init_state = {"realized": R["accounting"]["start_snapshot"]["realized_net_u"], "positions": init_positions}
    R["sim_init_state"] = init_state
    R["sim"] = run_sims(arch, truth, scans, s_list, sig_quote, filters, uni_ts, init_state)

    R["counts"] = {"files_read": files_read, "kline_symbols": len(arch), "kline_bars": total_bars,
                   "posthoc_symbols": len(truth), "posthoc_bars": int(sum(len(b.ot) for b in truth.values())),
                   "decision_rows": len(dec), "quote_rows": len(qev), "equity_rows": len(eq), "fills": len(fills)}
    (OUT / "results.json").write_text(json.dumps(R, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(R["counts"], ensure_ascii=False))


# ---------------------------------------------------------------- simulator
def run_sims(arch, truth, scans, s_list, sig_quote, filters, uni_ts, init_state=None):
    import sim
    return sim.run_all(arch, truth, scans, s_list, sig_quote, filters, init_state)


if __name__ == "__main__":
    main()
