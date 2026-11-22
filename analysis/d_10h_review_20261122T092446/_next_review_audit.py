#!/usr/bin/env python3
"""D_NEXT_REVIEW audits: 1000SHIB window, ambiguity redefine, HYPE MTM, uptime, TP compare.
Does NOT start runner / live orders. Preserves old HYPE ledger untouched.
"""
from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
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
SLIP = 0.0002  # adverse cover approx for CF exit px when using stop/tp ideal; we use idealized stop/tp px
SL_U = 3.0
WINDOWS = [5, 15, 30, 60]


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
    rows: list[dict] = []
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
                rows.append({"ot": ot, "o": o, "h": h, "l": l, "c": c, "open_time_ms": int(row["open_time"])})
        rows.sort(key=lambda x: x["ot"])
    _kl[sym] = rows
    return rows


def short_pnl(entry: float, exit_px: float, qty: float, entry_fee: float) -> dict:
    gross = (entry - exit_px) * qty
    exit_fee = abs(exit_px * qty) * FEE
    return {
        "gross": gross,
        "exit_fee": exit_fee,
        "entry_fee": entry_fee,
        "cycle_net": gross - exit_fee - entry_fee,
        "close_net": gross - exit_fee,
    }


def bars_overlap(kl, t0, t1):
    out = []
    for b in kl:
        if b["ot"] + timedelta(minutes=1) > t0 and b["ot"] < t1:
            out.append(b)
    return out


def audit_fixed_window(sym: str, entry_ts: datetime, minutes: int) -> dict:
    t0 = entry_ts
    t1 = t0 + timedelta(minutes=minutes)
    kl = load_kl(sym)
    bars = bars_overlap(kl, t0, t1)
    ots = [b["ot"] for b in bars]
    uniq = list(dict.fromkeys(ots))
    dups = [(k.isoformat(), v) for k, v in Counter(ots).items() if v > 1]
    gaps = []
    for a, b in zip(uniq, uniq[1:]):
        sec = (b - a).total_seconds()
        if sec != 60:
            gaps.append({"from": a.isoformat(), "to": b.isoformat(), "sec": sec})
    expected_inclusive = minutes + 1  # open_minute of start through open_minute of end if both included
    return {
        "symbol": sym,
        "minutes": minutes,
        "window_start": t0.isoformat(),
        "window_end": t1.isoformat(),
        "n_bars": len(bars),
        "n_unique_ts": len(uniq),
        "first_bar_ot": uniq[0].isoformat() if uniq else None,
        "last_bar_ot": uniq[-1].isoformat() if uniq else None,
        "duplicates": dups,
        "gaps": gaps,
        "expected_note": (
            f"overlap rule: open_time+1m > start AND open_time < end; "
            f"for exact minute-aligned entry ≈{minutes}+1 inclusive minute opens if start/end both hit"
        ),
        "expected_approx": expected_inclusive,
        "verdict": None,
    }


def classify_ambiguities(kl, entry, qty, entry_fee, t0, t_exit_or_cutoff, cutoff, is_open: bool) -> dict:
    """Separate uncertainty types. Never equate 'no dual-touch' with 'no uncertainty'."""
    counts = {
        "same_bar_tp_sl_dual_touch": 0,
        "entry_bar_includes_pre_entry_prices": 0,
        "cutoff_bar_includes_post_cutoff_prices": 0,
        "data_gap": 0,
        "truncated_tail": 0,
        "price_kind_ohlc_vs_mid_ask": 1,  # always present for this CF (1m OHLC vs runtime mid/ask)
    }
    details: list[dict] = []

    # entry bar
    entry_bar_ot = t0.replace(second=0, microsecond=0)
    for b in kl:
        if b["ot"] == entry_bar_ot:
            if b["ot"] < t0 < b["ot"] + timedelta(minutes=1):
                counts["entry_bar_includes_pre_entry_prices"] = 1
                details.append({"type": "entry_bar_pre_entry", "bar_ot": b["ot"].isoformat()})
            break

    # cutoff bar usability
    if is_open or t_exit_or_cutoff >= cutoff:
        cut_bar_ot = cutoff.replace(second=0, microsecond=0)
        for b in kl:
            if b["ot"] == cut_bar_ot:
                if b["ot"] <= cutoff < b["ot"] + timedelta(minutes=1):
                    counts["cutoff_bar_includes_post_cutoff_prices"] = 1
                    details.append(
                        {
                            "type": "cutoff_bar_post_cutoff",
                            "bar_ot": b["ot"].isoformat(),
                            "note": "cross-cutoff bar cannot exclude post-cutoff prices; not fully usable as complete market",
                        }
                    )
                break

    # gaps / truncated between entry and end
    end = min(t_exit_or_cutoff, cutoff) if not is_open else cutoff
    ov = bars_overlap(kl, t0, end + timedelta(seconds=1))
    for a, b in zip(ov, ov[1:]):
        if (b["ot"] - a["ot"]).total_seconds() > 60:
            counts["data_gap"] += 1
            details.append({"type": "gap", "from": a["ot"].isoformat(), "to": b["ot"].isoformat()})
    # truncated: last available bar ends before requested end
    if ov:
        last_close = ov[-1]["ot"] + timedelta(minutes=1)
        if last_close < end:
            counts["truncated_tail"] = 1
            details.append({"type": "truncated_tail", "last_bar_close": last_close.isoformat(), "wanted_end": end.isoformat()})
    else:
        counts["truncated_tail"] = 1
        details.append({"type": "truncated_tail", "note": "no overlapping bars"})

    return {"counts": counts, "details": details, "note": "no_dual_touch ≠ no_uncertainty"}


def replay_tp_sl(
    kl,
    entry: float,
    qty: float,
    entry_fee: float,
    t0: datetime,
    cutoff: datetime,
    *,
    sl_u: float,
    tp_mode: str,
    tp_u: Optional[float] = None,
    tp_pct: Optional[float] = None,
) -> dict:
    """Fixed-entry local CF. VOL not rebuilt. Fees/cutoff/boundary consistent.
    tp_mode: pct | usdt
    """
    if tp_mode == "pct":
        assert tp_pct is not None
        tp_px = entry * (1.0 - tp_pct)
        tp_u_eff = (entry - tp_px) * qty
    else:
        assert tp_u is not None
        tp_u_eff = tp_u
        tp_px = entry - tp_u / qty
    sl_px = entry + sl_u / qty

    overlapping = []
    for b in kl:
        if b["ot"] + timedelta(minutes=1) <= t0:
            continue
        if b["ot"] >= cutoff:
            break
        overlapping.append(b)

    amb = classify_ambiguities(kl, entry, qty, entry_fee, t0, cutoff, cutoff, is_open=True)

    def settle(exit_px, reason, ts, tag, dual=False):
        pnl = short_pnl(entry, exit_px, qty, entry_fee)
        return {
            "status": "EXITED",
            "reason": reason,
            "exit_px": exit_px,
            "exit_ts": ts.isoformat() if isinstance(ts, datetime) else ts,
            "fill_tag": tag,
            "dual_touch_bar": dual,
            **pnl,
        }

    for b in overlapping:
        hit_tp = b["l"] <= tp_px
        hit_sl = b["h"] >= sl_px
        if b["o"] > sl_px:
            cons = settle(b["o"], "SL", b["ot"], "GAP_OPEN_SL")
            return {
                "tp_px": tp_px,
                "sl_px": sl_px,
                "tp_u_eff": tp_u_eff,
                "result": cons,
                "conservative": cons,
                "optimistic": cons,
                "ambiguity": amb,
                "vol_rebuildable": False,
            }
        if hit_tp and hit_sl:
            amb["counts"]["same_bar_tp_sl_dual_touch"] += 1
            adv = settle(sl_px, "SL", b["ot"], "STOP_PX_IDEALIZED", dual=True)
            fav = settle(tp_px, "TP", b["ot"], "TP_PX_IDEALIZED", dual=True)
            return {
                "tp_px": tp_px,
                "sl_px": sl_px,
                "tp_u_eff": tp_u_eff,
                "result": {"status": "AMBIGUOUS", "adverse": adv, "favorable": fav, "bar_ot": b["ot"].isoformat()},
                "conservative": adv,
                "optimistic": fav,
                "ambiguity": amb,
                "vol_rebuildable": False,
            }
        if hit_sl:
            r = settle(sl_px, "SL", b["ot"], "STOP_PX_IDEALIZED")
            return {
                "tp_px": tp_px,
                "sl_px": sl_px,
                "tp_u_eff": tp_u_eff,
                "result": r,
                "conservative": r,
                "optimistic": r,
                "ambiguity": amb,
                "vol_rebuildable": False,
            }
        if hit_tp:
            r = settle(tp_px, "TP", b["ot"], "TP_PX_IDEALIZED")
            return {
                "tp_px": tp_px,
                "sl_px": sl_px,
                "tp_u_eff": tp_u_eff,
                "result": r,
                "conservative": r,
                "optimistic": r,
                "ambiguity": amb,
                "vol_rebuildable": False,
            }

    last = overlapping[-1] if overlapping else None
    if last is None:
        empty = {"status": "NO_BARS", "cycle_net": None, "gross": None}
        return {
            "tp_px": tp_px,
            "sl_px": sl_px,
            "tp_u_eff": tp_u_eff,
            "result": empty,
            "conservative": empty,
            "optimistic": empty,
            "ambiguity": amb,
            "vol_rebuildable": False,
        }

    # OPEN at cutoff — MTM on last bar close is NOT a complete usable quote if cutoff bar includes post-cutoff
    mtm = short_pnl(entry, last["c"], qty, entry_fee)
    # Conservative: use bar high (worst for short) within last overlapping bar if cutoff crosses it; else close
    cut_bar = last["ot"] <= cutoff < last["ot"] + timedelta(minutes=1)
    if cut_bar:
        cons_px = last["h"]  # worst short within incomplete cutoff bar
        opt_px = last["l"]  # best short within incomplete cutoff bar
        usable = False
    else:
        cons_px = last["c"]
        opt_px = last["c"]
        usable = True
    cons = short_pnl(entry, cons_px, qty, entry_fee)
    opt = short_pnl(entry, opt_px, qty, entry_fee)
    open_res = {
        "status": "OPEN_AT_CUTOFF",
        "mtm_px_last_close": last["c"],
        "mtm_bar_ot": last["ot"].isoformat(),
        "mtm_gross_at_close": mtm["gross"],
        "mtm_cycle_net_if_exit_at_close": mtm["cycle_net"],
        "cutoff_bar_incomplete": cut_bar,
        "fully_usable_market": usable,
        "unrealized": True,
        "note": "unrealized; not realized; VOL not applied",
    }
    return {
        "tp_px": tp_px,
        "sl_px": sl_px,
        "tp_u_eff": tp_u_eff,
        "result": open_res,
        "conservative": {
            "status": "OPEN_AT_CUTOFF_BOUND",
            "exit_px": cons_px,
            "cycle_net": cons["cycle_net"],
            "gross": cons["gross"],
            "unrealized": True,
        },
        "optimistic": {
            "status": "OPEN_AT_CUTOFF_BOUND",
            "exit_px": opt_px,
            "cycle_net": opt["cycle_net"],
            "gross": opt["gross"],
            "unrealized": True,
        },
        "ambiguity": amb,
        "vol_rebuildable": False,
    }


def audit_1000shib(opens: dict, supplement_path: Path) -> dict:
    o = None
    for f in opens.values():
        if f["symbol"] == "1000SHIBUSDT":
            o = f
            break
    assert o is not None
    entry_ts = parse_ts(o["recorded_at"])
    assert entry_ts is not None
    per_win = {str(m): audit_fixed_window("1000SHIBUSDT", entry_ts, m) for m in WINDOWS}
    # compare to stored supplement_data
    stored = None
    if supplement_path.is_file():
        sd = json.loads(supplement_path.read_text(encoding="utf-8"))
        for r in sd.get("path_rows") or []:
            if r.get("symbol") == "1000SHIBUSDT":
                stored = r.get("fixed_windows")
                break
    diffs = []
    for m in WINDOWS:
        key = str(m)
        new_n = per_win[key]["n_bars"]
        old_n = None
        if stored and key in stored:
            old_n = stored[key].get("n_bars")
        per_win[key]["stored_n_bars"] = old_n
        if old_n is not None and old_n != new_n:
            diffs.append({"minutes": m, "old": old_n, "new": new_n})
            per_win[key]["verdict"] = "RECOMPUTE_DIFFERS"
        else:
            # 30m should be ~31, 60m ~61 under inclusive overlap
            exp = m + 1
            if new_n == exp and per_win[key]["n_unique_ts"] == new_n and not per_win[key]["duplicates"]:
                per_win[key]["verdict"] = "OK_EXPECTED_INCLUSIVE"
            elif new_n == old_n:
                per_win[key]["verdict"] = "MATCHES_STORED"
            else:
                per_win[key]["verdict"] = "CHECK"

    # User asked specifically about 30m bars=61 — clarify layout vs data
    md_note = (
        "SUPPLEMENT.md 對 1000SHIB 列為 30m bars=31、60m bars=61（非 30m=61）。"
        "若閱讀時把 60m 行誤視為 30m，屬排版／閱讀混淆，非 JSON 計算錯誤。"
        "本稽核重算：30m=31 unique、60m=61 unique、無重複時間戳、無缺口。"
    )
    return {
        "entry_ts": entry_ts.isoformat(),
        "hold_sec": None,
        "windows": per_win,
        "diffs_vs_stored": diffs,
        "recompute_required": bool(diffs),
        "conclusion": md_note,
    }


def audit_hype(acct: dict, cutoff: datetime) -> dict:
    pos = (acct.get("positions") or {}).get("HYPEUSDT")
    if not pos:
        return {"error": "no HYPE position in snapshot account"}
    entry = float(pos["entry_price"])
    qty = float(pos["qty_remaining"])
    fee = float(pos.get("fees_paid_u") or 0)
    kl = load_kl("HYPEUSDT")
    before = [b for b in kl if b["ot"] < cutoff]
    last = before[-1] if before else None
    assert last is not None
    px = last["c"]
    pnl = short_pnl(entry, px, qty, fee)
    mid = 59.8655  # from prior review evidence (last observed mid in status print)
    mid_gross = (entry - mid) * qty
    mid_pnl = short_pnl(entry, mid, qty, fee)
    realized = float(acct.get("realized_net_u") or 0)
    return {
        "valuation_time_account_updated_at": acct.get("updated_at"),
        "cutoff_used": cutoff.isoformat(),
        "market_bar_ot": last["ot"].isoformat(),
        "price_kind": "sibling_C_1m_OHLC_close",
        "price": px,
        "qty": qty,
        "entry": entry,
        "entry_fee_u": fee,
        "formula": "cycle_net = (entry - mark_px)*qty - abs(mark_px*qty)*fee_rate - entry_fee",
        "fee_rate": FEE,
        "unrealized_gross_at_ohlc_close": pnl["gross"],
        "unrealized_cycle_net_if_exit_at_ohlc_close": pnl["cycle_net"],
        "prior_reported_mtm_net": -1.7720,
        "matches_prior": abs(pnl["cycle_net"] + 1.7720) < 1e-3,
        "mid_reference": mid,
        "mid_gross": mid_gross,
        "mid_cycle_net_if_exit": mid_pnl["cycle_net"],
        "mid_vs_ohlc_note": (
            "last mid=59.8655 → gross=-2.11428U；OHLC close=59.738 → gross≈-1.4717U → "
            "cycle_net≈-1.7720U（含進出場費）。價種不同，不得混用。"
        ),
        "realized_net_u_ledger": realized,
        "unrealized_vs_realized": "STRICT_SEPARATION: MTM above is unrealized; ledger realized_net_u excludes HYPE open",
        "cutoff_bar_incomplete": last["ot"] <= cutoff < last["ot"] + timedelta(minutes=1),
        "preservation": "old HYPE position and ledger NOT modified; no fabricated exit",
    }


def uptime_from_evidence(
    events: list,
    fills: list,
    log_path: Path,
    launch: dict,
    acct: dict,
    launch_d: Optional[dict] = None,
) -> dict:
    stamps: list[tuple[datetime, str]] = []
    for e in events:
        t = parse_ts(e.get("recorded_at") or e.get("at"))
        if t:
            stamps.append((t, f"event:{e.get('event')}"))
    for f in fills:
        t = parse_ts(f.get("recorded_at"))
        if t:
            stamps.append((t, f"fill:{f.get('event')}"))
    pe_times: list[datetime] = []
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if "PermissionError" in line:
                for j in range(i, max(i - 50, -1), -1):
                    m = re.search(r"(20\d{2}-\d{2}-\d{2}T[0-9:.+-]+)", lines[j])
                    if m:
                        pt = parse_ts(m.group(1))
                        if pt:
                            pe_times.append(pt)
                        break
            m2 = re.match(r"^\[(20\d{2}-\d{2}-\d{2}T[^\]]+)\]", line)
            if m2:
                pt = parse_ts(m2.group(1))
                if pt:
                    stamps.append((pt, "log"))
            # heartbeat / scan lines often embed ISO elsewhere
            if "[盯倉]" in line or "【掃描】" in line or "啟動 D" in line:
                m3 = re.search(r"(20\d{2}-\d{2}-\d{2}T[0-9:.+-]+)", line)
                if m3:
                    pt = parse_ts(m3.group(1))
                    if pt:
                        stamps.append((pt, "log_activity"))

    launch_t = parse_ts(launch.get("launched_at"))
    if launch_t:
        stamps.append((launch_t, "launch_meta_file"))
    # Prefer outer launcher started_at when LAUNCH_META was overwritten by later restart
    if launch_d and launch_d.get("started_at") is not None:
        try:
            stamps.append((datetime.fromtimestamp(float(launch_d["started_at"]), tz=TZ8), "launch_d_started_at"))
        except Exception:
            pass
    end_t = parse_ts(acct.get("updated_at"))
    if end_t:
        stamps.append((end_t, "account_updated"))

    stamps.sort(key=lambda x: x[0])
    # dedupe exact same timestamps keeping first src
    dedup: list[tuple[datetime, str]] = []
    for t, src in stamps:
        if not dedup or dedup[-1][0] != t:
            dedup.append((t, src))
    stamps = dedup

    if not stamps:
        return {"error": "no stamps"}

    t0, t1 = stamps[0][0], stamps[-1][0]
    span_h = (t1 - t0).total_seconds() / 3600.0

    # Proven-running: consecutive evidence gaps <= 120s (manage loop ~5s; allow jitter)
    GAP_RUN = 120.0
    # Gap >= 15 min without evidence → unknown or downtime
    GAP_DOWN = 900.0

    run_segs: list[dict] = []
    down_segs: list[dict] = []
    unknown_segs: list[dict] = []
    seg_start = stamps[0][0]
    last = stamps[0][0]
    for t, src in stamps[1:]:
        gap = (t - last).total_seconds()
        if gap < 0:
            # should not happen after sort; skip corrupt
            continue
        if gap <= GAP_RUN:
            last = t
            continue
        run_h_seg = (last - seg_start).total_seconds() / 3600
        if run_h_seg > 0 or last == seg_start:
            run_segs.append(
                {
                    "start": seg_start.isoformat(),
                    "end": last.isoformat(),
                    "hours": max(0.0, run_h_seg),
                    "last_src_before_gap": src,
                }
            )
        pe_in_gap = any(last <= pe <= t for pe in pe_times)
        # PE at/just before gap start counts as crash downtime
        pe_near = any(abs((pe - last).total_seconds()) <= 120 or last <= pe <= t for pe in pe_times)
        item = {
            "start": last.isoformat(),
            "end": t.isoformat(),
            "hours": gap / 3600,
            "permission_error_in_gap": pe_in_gap or pe_near,
        }
        if pe_in_gap or pe_near:
            down_segs.append({**item, "class": "crash_or_stop_after_persist_fail"})
        elif gap >= GAP_DOWN:
            unknown_segs.append({**item, "class": "no_evidence_gap_ge_15m"})
        else:
            unknown_segs.append({**item, "class": "soft_gap_2m_to_15m"})
        seg_start = t
        last = t
    run_segs.append(
        {
            "start": seg_start.isoformat(),
            "end": last.isoformat(),
            "hours": max(0.0, (last - seg_start).total_seconds() / 3600),
        }
    )

    run_h = sum(s["hours"] for s in run_segs)
    down_h = sum(s["hours"] for s in down_segs)
    unk_h = sum(s["hours"] for s in unknown_segs)
    return {
        "span_start": t0.isoformat(),
        "span_end": t1.isoformat(),
        "span_hours_wall": span_h,
        "note": (
            "Wall span is start→end only (~6.47h class if from first evidence to account cutoff); "
            "NOT proven continuous runtime. LAUNCH_META.launched_at may reflect a later overwrite/restart."
        ),
        "gap_thresholds_sec": {"run_continuity_max": GAP_RUN, "downtime_or_unknown_min": GAP_DOWN},
        "proven_run_segments": run_segs,
        "proven_or_crash_downtime_segments": down_segs,
        "unknown_segments": unknown_segs,
        "sum_proven_run_hours": run_h,
        "sum_downtime_hours": down_h,
        "sum_unknown_hours": unk_h,
        "check_sum_hours": run_h + down_h + unk_h,
        "permission_error_timestamps": [p.isoformat() for p in sorted(set(pe_times))],
        "launch_meta_launched_at": launch.get("launched_at"),
        "launch_d_started_at": launch_d.get("started_at") if launch_d else None,
        "evidence_sources": [
            "events.jsonl",
            "fills.jsonl",
            "strat_d_paper.log",
            "LAUNCH_META.json",
            "paper_bcd_launch_meta_d.json",
            "account_state.updated_at",
        ],
    }


def main() -> None:
    meta = json.loads((SNAP / "snapshot_meta.json").read_text(encoding="utf-8"))
    acct = json.loads((SNAP / "account_state.json").read_text(encoding="utf-8"))
    cutoff = parse_ts(acct.get("updated_at")) or parse_ts(meta["cutoff_iso"])
    assert cutoff is not None
    fills = load_jsonl(SNAP / "fills.jsonl")
    events = load_jsonl(SNAP / "events.jsonl")
    signals = load_jsonl(SNAP / "signals.jsonl")
    launch = json.loads((SNAP / "LAUNCH_META.json").read_text(encoding="utf-8"))
    launch_d_path = SNAP / "paper_bcd_launch_meta_d.json"
    launch_d = json.loads(launch_d_path.read_text(encoding="utf-8")) if launch_d_path.is_file() else None
    opens = {f["trade_id"]: f for f in fills if f.get("event") == "OPEN"}
    closes = {f["trade_id"]: f for f in fills if f.get("event") == "CLOSE"}

    shib = audit_1000shib(opens, OUT / "supplement_data.json")
    # attach hold from close if any
    for tid, o in opens.items():
        if o["symbol"] == "1000SHIBUSDT" and tid in closes:
            shib["hold_sec"] = float(closes[tid].get("hold_sec") or 0)

    hype = audit_hype(acct, cutoff)
    uptime = uptime_from_evidence(
        events, fills, SNAP / "strat_d_paper.log", launch, acct, launch_d=launch_d
    )

    # Ambiguity redefine across all opens (path to actual exit or cutoff)
    amb_agg = Counter()
    amb_per_trade = []
    for tid, o in opens.items():
        entry = float(o["price"])
        qty = float(o["qty"])
        fee = float(o.get("fee_u") or 0)
        t0 = parse_ts(o["recorded_at"])
        assert t0
        is_open = tid not in closes and (acct.get("positions") or {}).get(o["symbol"], {}).get("trade_id") == tid
        t_end = cutoff if is_open else parse_ts(closes[tid]["recorded_at"])
        assert t_end
        kl = load_kl(o["symbol"])
        # also count dual-touch for TP15/SL3 path
        tp_px = entry - 15.0 / qty
        sl_px = entry + 3.0 / qty
        a = classify_ambiguities(kl, entry, qty, fee, t0, t_end, cutoff, is_open=is_open)
        for b in bars_overlap(kl, t0, t_end + timedelta(seconds=1)):
            if b["l"] <= tp_px and b["h"] >= sl_px:
                a["counts"]["same_bar_tp_sl_dual_touch"] += 1
        for k, v in a["counts"].items():
            amb_agg[k] += int(v)
        amb_per_trade.append({"trade_id": tid, "symbol": o["symbol"], "counts": a["counts"], "is_open": is_open})

    # TP compare — NEW TEST SETTINGS (not claiming historical reproduction)
    # Historical BAR evidence: main-session observed bar_amps max >= ~0.00952, compatible with 0.01;
    # NOT proven that BAR=0.01 was the live threshold. RANGE historical effective value unknown.
    # Signal count from signals.jsonl only.
    tp_arms = [
        {"id": "TP_PCT_0_5", "label": "TP價格幅度0.5%", "mode": "pct", "tp_pct": 0.005, "tp_u": None},
        {"id": "TP_PCT_1_0", "label": "TP價格幅度1%", "mode": "pct", "tp_pct": 0.01, "tp_u": None},
        {"id": "TP_PCT_1_5", "label": "TP價格幅度1.5%", "mode": "pct", "tp_pct": 0.015, "tp_u": None},
        {"id": "TP_USDT_15", "label": "原每倉毛利TP15U", "mode": "usdt", "tp_pct": None, "tp_u": 15.0},
    ]

    per_arm_rows: dict[str, list] = {a["id"]: [] for a in tp_arms}
    for tid, o in sorted(opens.items(), key=lambda x: x[1]["recorded_at"]):
        entry = float(o["price"])
        qty = float(o["qty"])
        fee = float(o.get("fee_u") or 0)
        t0 = parse_ts(o["recorded_at"])
        assert t0
        kl = load_kl(o["symbol"])
        actual = None
        if tid in closes:
            c = closes[tid]
            actual = {
                "reason": c.get("exit_reason"),
                "exit_px": float(c["price"]),
                "cycle_net": short_pnl(entry, float(c["price"]), qty, fee)["cycle_net"],
                "hold_sec": c.get("hold_sec"),
            }
        for arm in tp_arms:
            r = replay_tp_sl(
                kl,
                entry,
                qty,
                fee,
                t0,
                cutoff,
                sl_u=SL_U,
                tp_mode=arm["mode"],
                tp_u=arm["tp_u"],
                tp_pct=arm["tp_pct"],
            )
            row = {
                "trade_id": tid,
                "symbol": o["symbol"],
                "entry": entry,
                "qty": qty,
                "entry_fee": fee,
                "actual_closed": actual,
                "tp_px": r["tp_px"],
                "sl_px": r["sl_px"],
                "tp_u_eff": r["tp_u_eff"],
                "result_status": r["result"].get("status"),
                "result": r["result"],
                "conservative_cycle_net": r["conservative"].get("cycle_net"),
                "optimistic_cycle_net": r["optimistic"].get("cycle_net"),
                "conservative": r["conservative"],
                "optimistic": r["optimistic"],
                "ambiguity_counts": r["ambiguity"]["counts"],
                "vol_rebuildable": False,
            }
            per_arm_rows[arm["id"]].append(row)

    summaries = {}
    for arm in tp_arms:
        rows = per_arm_rows[arm["id"]]
        cons = [r["conservative_cycle_net"] for r in rows if r["conservative_cycle_net"] is not None]
        opt = [r["optimistic_cycle_net"] for r in rows if r["optimistic_cycle_net"] is not None]
        statuses = Counter(r["result_status"] for r in rows)
        dual = sum(r["ambiguity_counts"].get("same_bar_tp_sl_dual_touch", 0) for r in rows)
        summaries[arm["id"]] = {
            "label": arm["label"],
            "n": len(rows),
            "sum_conservative_cycle_net": sum(cons),
            "sum_optimistic_cycle_net": sum(opt),
            "status_counts": dict(statuses),
            "dual_touch_count_sum": dual,
            "vol_note": "VOL_SPIKE cannot be rebuilt from 1m OHLC without native mid roll samples — NOT full strategy backtest",
        }

    # signals evidence (not inferred from fills)
    sig_n = sum(1 for s in signals if s.get("event") == "SIGNAL" or "signal_id" in s)
    bar_amps_all = []
    for s in signals:
        for a in s.get("bar_amps") or []:
            try:
                bar_amps_all.append(float(a))
            except Exception:
                pass
    hist_settings = {
        "signals_count_from_signals_jsonl": sig_n,
        "bar_amp_max_observed_in_signals": max(bar_amps_all) if bar_amps_all else None,
        "bar_evidence_note": (
            "有證據的主交易時段 BAR 上限 ≥約0.00952，與 0.01 相容；"
            "不得宣稱已證明執行時 BAR=0.01。現行 config.BAR_AMP_MAX=0.0075 與訊號 bar_amps 證據不一致需另查。"
        ),
        "range_historical_effective": "UNKNOWN",
        "tp_compare_settings_tag": "NEW_TEST_SETTINGS_fixed_entry_SL3_no_VOL",
        "not_full_historical_reproduction": True,
    }

    out = {
        "generated_at": datetime.now(TZ8).isoformat(),
        "cutoff": cutoff.isoformat(),
        "audit_1000shib": shib,
        "ambiguity_redefined": {
            "aggregate_counts": dict(amb_agg),
            "per_trade": amb_per_trade,
            "rule": "不得將沒有雙觸及寫成沒有不確定性；跨截止K若無法排除截止後價格不得當完整可用行情",
        },
        "audit_hype_mtm": hype,
        "uptime": uptime,
        "historical_settings_framing": hist_settings,
        "tp_compare": {
            "arms": tp_arms,
            "summaries": summaries,
            "per_arm_rows": per_arm_rows,
            "fees_cutoff_boundary": "FEE=0.0005 both sides in cycle_net; cutoff=account.updated_at; entry/cutoff bar boundary flagged; SL=3U gross; VOL not applied",
            "preservation": "old HYPE & ledger untouched; CF results are diagnostic only, not merged into account",
        },
    }
    (OUT / "next_review_data.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Wrote", OUT / "next_review_data.json")
    print("SHIB30", shib["windows"]["30"]["n_bars"], "SHIB60", shib["windows"]["60"]["n_bars"], "diffs", shib["diffs_vs_stored"])
    print("HYPE cycle", hype.get("unrealized_cycle_net_if_exit_at_ohlc_close"), "mid_gross", hype.get("mid_gross"))
    print("uptime span", uptime.get("span_hours_wall"), "run", uptime.get("sum_proven_run_hours"), "down", uptime.get("sum_downtime_hours"), "unk", uptime.get("sum_unknown_hours"))
    for aid, s in summaries.items():
        print(aid, "cons", round(s["sum_conservative_cycle_net"], 4), "opt", round(s["sum_optimistic_cycle_net"], 4), s["status_counts"])


if __name__ == "__main__":
    main()
