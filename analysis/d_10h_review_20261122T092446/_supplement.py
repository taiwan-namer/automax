#!/usr/bin/env python3
"""D review supplement — evidence only. Writes SUPPLEMENT.md + updates STRATEGY_SPEC appendix."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

OUT = Path(__file__).resolve().parent
SNAP = OUT / "snapshots"
TZ8 = timezone(timedelta(hours=8))
NA = "N/A"
KLINE_VAULT = Path(
    r"c:\Users\Admin\MyProjects\automax\output\paper_bcd_local\data_c\data\testnet_c_inv_v1\kline_vault_v2_clean"
)
CODE = Path(r"c:\Users\Admin\MyProjects\automax\output\testnet_abcd_setup\code\d_quiet_short_v1")
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


def fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return NA
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return str(x)
        return f"{x:.{nd}f}"
    return str(x)


def load_jsonl(p: Path) -> list[dict]:
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


def short_pnl(entry: float, exit_px: float, qty: float, entry_fee: float, fee_rate: float = 0.0005) -> dict:
    gross = (entry - exit_px) * qty
    exit_fee = abs(exit_px * qty) * fee_rate
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
        bend = b["ot"] + timedelta(minutes=1)
        if bend <= t0 or b["ot"] >= t1:
            continue
        out.append(b)
    return out


def bars_strict_inside(kl, t0, t1):
    """Bars whose full [ot, ot+1m) is inside (t0, t1)."""
    out = []
    for b in kl:
        bend = b["ot"] + timedelta(minutes=1)
        if b["ot"] >= t0 and bend <= t1:
            out.append(b)
    return out


def path_from_bars(bars, entry, qty):
    if not bars:
        return None
    mfe_pct = mae_pct = 0.0
    mfe_u = mae_u = 0.0
    mfe_t = mae_t = None
    for b in bars:
        fav = (entry - b["l"]) / entry
        adv = (b["h"] - entry) / entry
        if fav > mfe_pct:
            mfe_pct, mfe_t, mfe_u = fav, b["ot"], (entry - b["l"]) * qty
        if adv > mae_pct:
            mae_pct, mae_t, mae_u = adv, b["ot"], -(b["h"] - entry) * qty
    return {
        "n": len(bars),
        "mfe_pct": mfe_pct * 100,
        "mae_pct": mae_pct * 100,
        "mfe_u": mfe_u,
        "mae_u": mae_u,
        "mfe_ts": mfe_t.isoformat() if mfe_t else None,
        "mae_ts": mae_t.isoformat() if mae_t else None,
    }


def window_stats(kl, entry, qty, t0, minutes, cutoff):
    t1 = t0 + timedelta(minutes=minutes)
    if t1 > cutoff:
        return {"status": "CENSORED", "minutes": minutes}
    bars = bars_overlap(kl, t0, t1)
    if not bars:
        return {"status": "DATA_GAP", "minutes": minutes}
    path = path_from_bars(bars, entry, qty)
    # end close: last bar whose ot < t1
    end = [b for b in kl if b["ot"] < t1]
    if not end:
        return {"status": "DATA_GAP", "minutes": minutes}
    end_px = end[-1]["c"]
    ret = (entry - end_px) / entry * 100
    return {
        "status": "OK",
        "minutes": minutes,
        "end_ret_pct": ret,
        "mfe_pct": path["mfe_pct"],
        "mae_pct": path["mae_pct"],
        "mfe_u": path["mfe_u"],
        "mae_u": path["mae_u"],
        "n_bars": path["n"],
        "coverage": "overlap_bars",
    }


def first_touch_times(kl, entry, t0, t_end, targets_down, targets_up):
    """First bar touch for short favorable (low) and adverse (high)."""
    down = {k: None for k in targets_down}
    up = {k: None for k in targets_up}
    for b in kl:
        if b["ot"] + timedelta(minutes=1) <= t0:
            continue
        if b["ot"] >= t_end:
            break
        for name, px in targets_down.items():
            if down[name] is None and b["l"] <= px:
                down[name] = b["ot"]
        for name, px in targets_up.items():
            if up[name] is None and b["h"] >= px:
                up[name] = b["ot"]
    return down, up


def replay_exit(kl, entry, qty, entry_fee, t0, cutoff, sl_u, tp_u, use_vol, T, roll_seed_px):
    """1m OHLC proxy for fixed-entry local diagnosis. VOL from bar hi/lo amp over rolling 15m of mids≈closes — APPROXIMATE.
    Returns conservative/optimistic when same bar hits TP and SL.
    VOL exact reconstruction: NOT possible from 1m without mid time series → if use_vol, mark VOL_MODEL=APPROX_OR_SKIP.
    """
    # Without tick/mid roll samples, VOL_SPIKE cannot be faithfully rebuilt from OHLC alone.
    # For CF-C we disable VOL. For A/B we attempt SL/TP only first, then note VOL as non-rebuildable
    # unless we use account roll — we don't have historical rolls for closed trades.
    tp_px = entry - tp_u / qty
    sl_px = entry + sl_u / qty
    # scan bars after entry
    overlapping = []
    for b in kl:
        if b["ot"] + timedelta(minutes=1) <= t0:
            continue
        if b["ot"] >= cutoff:
            break
        overlapping.append(b)

    def settle(exit_px, reason, ts, tag):
        pnl = short_pnl(entry, exit_px, qty, entry_fee)
        return {
            "status": "EXITED",
            "reason": reason,
            "exit_px": exit_px,
            "exit_ts": ts.isoformat() if isinstance(ts, datetime) else ts,
            "fill_tag": tag,
            **pnl,
        }

    for b in overlapping:
        hit_tp = b["l"] <= tp_px
        hit_sl = b["h"] >= sl_px
        # gap open above SL
        if b["o"] > sl_px:
            return settle(b["o"], "SL", b["ot"], "GAP_OPEN_SL"), None
        if hit_tp and hit_sl:
            # ambiguous
            adv = settle(sl_px, "SL", b["ot"], "STOP_PX_IDEALIZED")
            fav = settle(tp_px, "TP", b["ot"], "TP_PX_IDEALIZED")
            return {
                "status": "AMBIGUOUS",
                "adverse": adv,
                "favorable": fav,
                "bar_ot": b["ot"].isoformat(),
            }, "AMBIGUOUS"
        if hit_sl:
            return settle(sl_px, "SL", b["ot"], "STOP_PX_IDEALIZED"), None
        if hit_tp:
            return settle(tp_px, "TP", b["ot"], "TP_PX_IDEALIZED"), None
        # VOL: cannot reliably rebuild — skip in path; caller notes
        _ = use_vol, T, roll_seed_px

    # still open at cutoff
    last = overlapping[-1] if overlapping else None
    if last is None:
        return {"status": "NO_BARS", "cycle_net": None}, None
    mtm = short_pnl(entry, last["c"], qty, entry_fee)
    return {
        "status": "OPEN_AT_CUTOFF",
        "mtm_px": last["c"],
        "mtm_ts": last["ot"].isoformat(),
        "mtm_gross": mtm["gross"],
        "mtm_cycle_net_if_exit": mtm["cycle_net"],
        "unrealized": True,
    }, None


def main():
    meta = json.loads((SNAP / "snapshot_meta.json").read_text(encoding="utf-8"))
    cutoff = parse_ts(meta["cutoff_iso"])
    # use account last update as market cutoff for open MTM consistency with prior brief
    acct = json.loads((SNAP / "account_state.json").read_text(encoding="utf-8"))
    window_end = parse_ts(acct.get("updated_at")) or cutoff
    fills = load_jsonl(SNAP / "fills.jsonl")
    signals = load_jsonl(SNAP / "signals.jsonl")
    events = load_jsonl(SNAP / "events.jsonl")
    launch = json.loads((SNAP / "LAUNCH_META.json").read_text(encoding="utf-8"))
    launch_d = json.loads((SNAP / "paper_bcd_launch_meta_d.json").read_text(encoding="utf-8"))

    opens = {f["trade_id"]: f for f in fills if f["event"] == "OPEN"}
    closes = {f["trade_id"]: f for f in fills if f["event"] == "CLOSE"}

    # --- 1 TP/SL scale table ---
    tpsl_rows = []
    for tid, o in opens.items():
        entry = float(o["price"])
        qty = float(o["qty"])
        fee = float(o.get("fee_u") or 0)
        # from position if residual else from config full size (all fills were full target)
        tp_u = 15.0
        sl_u = 3.0
        if tid in (acct.get("positions") or {}) or (
            (acct.get("positions") or {}).get(o["symbol"], {}).get("trade_id") == tid
        ):
            pos = (acct.get("positions") or {}).get(o["symbol"]) or {}
            tp_u = float(pos.get("tp_usdt") or 15)
            sl_u = float(pos.get("sl_usdt") or 3)
        tp_px = entry - tp_u / qty
        sl_px = entry + sl_u / qty
        tp_pct = (entry - tp_px) / entry * 100
        sl_pct = (sl_px - entry) / entry * 100
        c = closes.get(tid)
        tpsl_rows.append(
            {
                "trade_id": tid,
                "symbol": o["symbol"],
                "entry_ts": o["recorded_at"],
                "entry_px": entry,
                "qty": qty,
                "notional": entry * qty,
                "tp_usdt": tp_u,
                "sl_usdt": sl_u,
                "tp_px": tp_px,
                "sl_px": sl_px,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "exit_reason": c.get("exit_reason") if c else "OPEN",
                "exit_px": float(c["price"]) if c else None,
                "exit_ts": c.get("recorded_at") if c else None,
                "entry_fee": fee,
            }
        )

    # --- 2 effective settings from signals ---
    sig_amps = []
    for s in signals:
        ba = s.get("bar_amps") or []
        if not ba:
            continue
        sig_amps.append(
            {
                "symbol": s["symbol"],
                "emitted_at": s.get("emitted_at"),
                "max_bar_amp": max(ba),
                "range_amp": s.get("range_amp"),
            }
        )
    max_bar = max(a["max_bar_amp"] for a in sig_amps) if sig_amps else None
    n_gt_0075 = sum(1 for a in sig_amps if a["max_bar_amp"] > 0.0075)
    n_gt_01 = sum(1 for a in sig_amps if a["max_bar_amp"] > 0.01)
    max_range = max((a["range_amp"] or 0) for a in sig_amps) if sig_amps else None

    # --- 3 HYPE ---
    hype = (acct.get("positions") or {}).get("HYPEUSDT") or {}
    hype_rolls = hype.get("roll_samples") or []
    last_roll_ts = max((float(s["ts"]) for s in hype_rolls), default=None)
    last_roll_px = None
    if last_roll_ts:
        for s in hype_rolls:
            if float(s["ts"]) == last_roll_ts:
                last_roll_px = s.get("px")
                break
        # if multiple, take last in list with that ts
        for s in reversed(hype_rolls):
            if abs(float(s["ts"]) - last_roll_ts) < 1e-6:
                last_roll_px = s.get("px")
                break

    # --- 4 paths ---
    path_rows = []
    for row in tpsl_rows:
        tid = row["trade_id"]
        kl = load_kl(row["symbol"])
        t0 = parse_ts(row["entry_ts"])
        t1 = parse_ts(row["exit_ts"]) or window_end
        ov = bars_overlap(kl, t0, t1)
        strict = bars_strict_inside(kl, t0, t1)
        path_ov = path_from_bars(ov, row["entry_px"], row["qty"])
        path_st = path_from_bars(strict, row["entry_px"], row["qty"])
        boundary = False
        if ov:
            if ov[0]["ot"] <= t0 < ov[0]["ot"] + timedelta(minutes=1):
                boundary = True
            if ov[-1]["ot"] <= t1 < ov[-1]["ot"] + timedelta(minutes=1):
                boundary = True
        fw = {m: window_stats(kl, row["entry_px"], row["qty"], t0, m, window_end) for m in WINDOWS}

        # post-exit for SL trades
        post = {}
        if row["exit_reason"] and "SL" in str(row["exit_reason"]) and row["exit_px"] and row["exit_ts"]:
            et = parse_ts(row["exit_ts"])
            entry = row["entry_px"]
            qty = row["qty"]
            fee = row["entry_fee"]
            # breakeven cover px such that gross - exit_fee - entry_fee = 0
            # (entry - x)*qty - x*qty*0.0005 - fee = 0
            # entry*qty - fee = x*qty*(1+0.0005)
            be_px = (entry * qty - fee) / (qty * 1.0005) if qty else None
            targets_down = {
                "back_to_entry": entry,
                "breakeven_cost": be_px,
                "fav_0.5pct": entry * (1 - 0.005),
                "fav_1.0pct": entry * (1 - 0.01),
            }
            # adverse before those hits: track mae until each hit
            down_hit, _ = first_touch_times(kl, entry, et, window_end, targets_down, {})
            for name, ts_hit in down_hit.items():
                if ts_hit is None:
                    post[name] = {"hit": False, "sec": None, "mae_pct_before": NA, "order": NA}
                else:
                    # mae from exit to hit using overlap bars
                    bars = bars_overlap(kl, et, ts_hit + timedelta(minutes=1))
                    # adverse for short after exit: price up from entry? User asked 达標前最大逆向 — vs entry
                    mae = 0.0
                    amb = False
                    for b in bars:
                        mae = max(mae, (b["h"] - entry) / entry)
                        # same bar could also make the favorable touch
                        tgt = targets_down[name]
                        if b["l"] <= tgt and b["h"] >= entry * (1 + 0.005):
                            amb = True
                    post[name] = {
                        "hit": True,
                        "sec": (ts_hit - et).total_seconds(),
                        "mae_pct_before": mae * 100,
                        "order": "AMBIGUOUS_SAME_BAR_POSSIBLE" if amb else "HIT_OBSERVED_ON_1M",
                        "note": "1m OHLC; intra-bar order unknown",
                    }

        path_rows.append(
            {
                **row,
                "hold_sec": (t1 - t0).total_seconds() if t0 and t1 else None,
                "path_overlap": path_ov,
                "path_strict_inside": path_st if path_st else {"n": 0, "note": "N/A_short_hold_no_full_bar"},
                "boundary_error": boundary,
                "fixed_windows": fw,
                "post_sl": post,
                "kline_vault": str(KLINE_VAULT),
                "price_kind": "last_ohlc_1m_from_paper_C_sibling",
            }
        )

    # clustering / overlap
    intervals = []
    for r in path_rows:
        t0 = parse_ts(r["entry_ts"])
        t1 = parse_ts(r["exit_ts"]) or window_end
        intervals.append((t0, t1, r["symbol"], r["trade_id"]))
    # max concurrent
    events_pts = []
    for a, b, sym, tid in intervals:
        events_pts.append((a, 1, tid))
        events_pts.append((b, -1, tid))
    events_pts.sort(key=lambda x: (x[0], -x[1]))
    cur = peak = 0
    for _, d, _ in events_pts:
        cur += d
        peak = max(peak, cur)
    # entry minute clusters
    by_min = defaultdict(list)
    for r in path_rows:
        t0 = parse_ts(r["entry_ts"])
        key = t0.strftime("%Y-%m-%d %H:%M") if t0 else "?"
        by_min[key].append(r["symbol"])

    # --- 5 CF A/B/C ---
    # Actual closed for reproduction check
    cf_table = []
    for row in tpsl_rows:
        tid = row["trade_id"]
        kl = load_kl(row["symbol"])
        t0 = parse_ts(row["entry_ts"])
        actual = closes.get(tid)
        entry_fee = row["entry_fee"]
        qty = row["qty"]
        entry = row["entry_px"]

        # A: TP15 SL3 no VOL in OHLC model (VOL not rebuildable)
        a_res, a_amb = replay_exit(kl, entry, qty, entry_fee, t0, window_end, 3.0, 15.0, False, None, entry)
        b_res, b_amb = replay_exit(kl, entry, qty, entry_fee, t0, window_end, 5.0, 15.0, False, None, entry)
        c_res, c_amb = replay_exit(kl, entry, qty, entry_fee, t0, window_end, 3.0, 15.0, False, None, entry)
        # C is same barriers as A when VOL not modeled — distinguish: C means "ignore VOL even if actual had VOL"
        # So C uses same TP/SL path as A on OHLC; difference vs actual is interpretive.

        def net_of(res):
            if not res:
                return None
            if res.get("status") == "EXITED":
                return res.get("cycle_net")
            if res.get("status") == "AMBIGUOUS":
                return {
                    "adverse": res["adverse"]["cycle_net"],
                    "favorable": res["favorable"]["cycle_net"],
                }
            if res.get("status") == "OPEN_AT_CUTOFF":
                return {"unrealized_if_exit": res.get("mtm_cycle_net_if_exit")}
            return None

        actual_net = None
        if actual:
            actual_net = float(actual.get("net_u") or 0) - entry_fee

        # classify B vs actual for SL-widen narrative (only if both exited)
        b_effect = NA
        if actual:
            if a_res.get("status") == "AMBIGUOUS":
                b_effect = "A_PATH_AMBIGUOUS"
            elif a_res.get("status") == "EXITED" and b_res.get("status") == "OPEN_AT_CUTOFF":
                b_effect = "EXTENDED_OPEN_AT_CUTOFF"
            elif a_res.get("status") == "EXITED" and b_res.get("status") == "EXITED":
                if a_res["reason"] == "SL" and b_res["reason"] == "TP":
                    b_effect = "RESCUED_TO_TP"
                elif a_res["reason"] == "SL" and b_res["reason"] == "SL":
                    if b_res["cycle_net"] < a_res["cycle_net"] - 1e-9:
                        b_effect = "LARGER_LOSS"
                    elif abs(b_res["exit_px"] - a_res["exit_px"]) < 1e-12:
                        b_effect = "SAME_SL_FILL"
                    else:
                        b_effect = "STILL_SL_DIFFERENT_PX"
                elif a_res["reason"] == "TP" and b_res["reason"] == "TP":
                    b_effect = "UNAFFECTED_TP"
                else:
                    b_effect = f"A={a_res.get('reason')}/B={b_res.get('reason')}"
            elif a_res.get("status") == "OPEN_AT_CUTOFF" and b_res.get("status") == "OPEN_AT_CUTOFF":
                b_effect = "BOTH_OPEN_AT_CUTOFF"
            else:
                b_effect = f"A={a_res.get('status')}/B={b_res.get('status')}"

        # reproduction error for A vs actual (only closed)
        repro = NA
        if actual and a_res.get("status") == "EXITED":
            repro = {
                "actual_reason": actual.get("exit_reason"),
                "model_reason": a_res.get("reason"),
                "actual_exit_px": float(actual["price"]),
                "model_exit_px": a_res.get("exit_px"),
                "px_err": a_res.get("exit_px") - float(actual["price"]),
                "actual_cycle_net": actual_net,
                "model_cycle_net": a_res.get("cycle_net"),
                "net_err": (a_res.get("cycle_net") or 0) - (actual_net or 0),
                "vol_in_actual": "VOL" in str(actual.get("exit_reason") or ""),
            }

        cf_table.append(
            {
                "trade_id": tid,
                "symbol": row["symbol"],
                "actual_reason": actual.get("exit_reason") if actual else "OPEN",
                "actual_cycle_net": actual_net,
                "A": a_res,
                "B": b_res,
                "C": c_res,
                "A_net": net_of(a_res),
                "B_net": net_of(b_res),
                "C_net": net_of(c_res),
                "B_effect": b_effect,
                "repro_A": repro,
            }
        )

    # aggregates for CF
    def sum_exited(key):
        s = 0.0
        n = 0
        open_n = 0
        amb_n = 0
        unreal = 0.0
        for r in cf_table:
            res = r[key]
            if res.get("status") == "EXITED":
                s += res["cycle_net"]
                n += 1
            elif res.get("status") == "AMBIGUOUS":
                amb_n += 1
            elif res.get("status") == "OPEN_AT_CUTOFF":
                open_n += 1
                unreal += res.get("mtm_cycle_net_if_exit") or 0
        return {"exited_n": n, "net_sum": s, "open_n": open_n, "amb_n": amb_n, "unreal_sum": unreal}

    # B effect counts
    beff = defaultdict(int)
    for r in cf_table:
        if r["actual_reason"] != "OPEN":
            beff[r["B_effect"]] += 1

    # write STRATEGY_SPEC appendix
    append_spec = f"""

## 補充：TP／SL 尺度證據（2026-11-22 supplement）

### 程式證明：每倉（whole-position）毛利 USDT 門檻，非組合／非分腿
- 模組說明：`portfolio.py` L1「whole-position TP/SL/vol-exit」。
- 開倉寫入 `pos["tp_usdt"]=tp_u`、`pos["sl_usdt"]=sl_u`，來自 `scaled_tp_sl(filled_frac)` = `TP_USDT * fill_ratio`／`SL_USDT * fill_ratio`（`portfolio.py` L115–117, L204–247）。
- 觸發：`metric`（該倉 gross 或 net）對照 **該倉** `pos["tp_usdt"]`／`pos["sl_usdt"]`（L975–978），不是帳戶合計。
- 本批成交 `fill_ratio≈1`，故每倉即 **+15U／−3U gross**。HYPE 殘倉帳戶欄位 `tp_usdt=15`/`sl_usdt=3` 可核對。
- 價格換算（SHORT）：`tp_px = entry - tp_usdt/qty`；`sl_px = entry + sl_usdt/qty`（`testnet_hooks.py` L37–51 同式）。
- **無**追蹤停利、**無**時間 TTL 出場、**無**分批 TP；加倉僅在同一 pending 部分成交時縮放門檻（本批皆滿額）。`PARTIAL_TP` 不存在於 D。

### 逐筆 TP／SL 價格與百分比
見 `SUPPLEMENT.md` 表（名目≈300U 時，SL3≈1% 價距、TP15≈5% 量級，隨 entry/qty 微變）。
"""
    spec_path = OUT / "STRATEGY_SPEC.md"
    spec = spec_path.read_text(encoding="utf-8")
    if "補充：TP／SL 尺度證據" not in spec:
        spec_path.write_text(spec + append_spec, encoding="utf-8")

    # build SUPPLEMENT.md
    lines = []
    lines.append("# SUPPLEMENT — D 10h 復盤證據補篇")
    lines.append("")
    lines.append(f"截止（與主報告同一快照）：`{meta['cutoff_iso']}`；行情窗末日=`{window_end.isoformat()}`（account.updated_at）。")
    lines.append("本輪**未**改交易邏輯、**未**放寬 SL、**未**關 VOL、**未**重啟程式。")
    lines.append("")

    lines.append("## 1. TP／SL 真正尺度")
    lines.append("")
    lines.append("**確認事實：** +15U／−3U 是**每一倉位**的毛損益（gross）觸發門檻，不是組合合計，也不是分腿。")
    lines.append("證據：`portfolio.py` 以 `pos[\"tp_usdt\"]`／`pos[\"sl_usdt\"]` 對該倉 `metric` 比較；`scaled_tp_sl` 僅依該筆 fill_ratio 縮放；模組 docstring 寫 whole-position。")
    lines.append("本批無動態加倉／分批停利／時間退出（確認事實）。百分比=由 USDT 門檻反推的價格距離，非策略直接參數。")
    lines.append("")
    lines.append("| symbol | notional | tp_usdt/sl_usdt | tp_px / sl_px | tp% / sl% | exit |")
    lines.append("|--------|----------|-----------------|---------------|-----------|------|")
    for r in sorted(tpsl_rows, key=lambda x: x["entry_ts"]):
        lines.append(
            f"| {r['symbol']} | {fmt(r['notional'],2)} | {fmt(r['tp_usdt'],1)}/{fmt(r['sl_usdt'],1)} | "
            f"{fmt(r['tp_px'],6)} / {fmt(r['sl_px'],6)} | {fmt(r['tp_pct'],3)}% / {fmt(r['sl_pct'],3)}% | {r['exit_reason']} |"
        )
    lines.append("")
    lines.append("詳細已補入 `STRATEGY_SPEC.md` 補充節。")
    lines.append("")

    lines.append("## 2. 實際生效設定（不得只信現盤 config）")
    lines.append("")
    lines.append("### 載入鏈（確認事實）")
    lines.append("- 進程：`python -m d_quiet_short_v1.paper_main --paper --runtime-root ... --allow-fills`（log CMD）。")
    lines.append("- CLI **無** RANGE/BAR 參數（`paper_main.py` argparse 僅 paper/runtime-root/hours/once/testnet/scan-only/allow-fills）。")
    lines.append("- `paper_main` 寫入的 `LAUNCH_META.json` **不含** range/bar 閾值（只含 fee/slip/fill_model/version）。")
    lines.append("- `signals.evaluate_quiet` 讀 `config.RANGE_AMP_MAX` / `config.BAR_AMP_MAX`（`signals.py` L123–127）。")
    lines.append("- 啟動器寫入的 `paper_bcd_local/launch_meta_d.json` 含 `range_amp_max=0.02`、`bar_amp_max=0.01`，但 **tools 內找不到** 把它賦值進 `config` 的程式；**不能**證明該檔曾注入進程。")
    lines.append("- 現盤 `config.py`：RANGE=0.015、BAR=0.0075。git 對該檔無可用歷史（未追蹤／空 log）→ 無法用 git 證明運行時磁碟值。")
    lines.append("")
    lines.append("### 以訊號反證歷史有效 BAR 閾值（確認事實）")
    lines.append(f"- 訊號數 {len(sig_amps)}；其中 max(bar_amps)**>0.0075** 者 **{n_gt_0075}** 筆；**>0.01** 者 **{n_gt_01}** 筆。")
    lines.append(f"- 觀測 max(bar_amp)={fmt(max_bar,6)}；max(range_amp)={fmt(max_range,6)}。")
    lines.append("- 若當時 `BAR_AMP_MAX=0.0075`，`evaluate_quiet` 應拒絕這些訊號；但它們已寫入 `signals.jsonl`。")
    lines.append("- 因此：**主交易時段（約 18:37 後）有效 BAR 上限 ≥≈0.00952，且與 launch_meta_d 的 0.01 相容；與現盤 config 0.0075 不相容。**")
    lines.append("- RANGE：所有訊號 range_amp≤0.015，**無法**區分有效值是 0.015 還是 0.02 → RANGE 歷史有效值 **未知（兩候選皆可能）**。")
    lines.append("- `BAR_AMP_FAIL` 撤單（TRX 14:45）**只證明檢查被觸發**，事件列無實際 amp／threshold → **不能**單獨證明用哪個閾值。")
    lines.append("")
    lines.append("| 時段 | 可證明 | 未知 |")
    lines.append("|------|--------|------|")
    lines.append("| 14:33 首啟～18:01 崩潰 | 版本 d_quiet_short_testnet_20261121；TP15/SL3（後續持倉欄位） | 該段 BAR/RANGE 數值 |")
    lines.append("| 18:36 D-only 重啟～19:41 崩潰 | BAR 有效 ≥0.0095（訊號反證）；TP15/SL3 | RANGE 0.015 vs 0.02 |")
    lines.append("| 20:15 ACD 重啟～21:03 崩潰 | 日誌印「毛TP 15.0／毛SL 3.0」；HYPE 帳戶 tp/sl=15/3 | BAR/RANGE 數值（該段幾乎無新 quiet 訊號可反證） |")
    lines.append("")

    lines.append("## 3. PermissionError 與 HYPE 殘倉")
    lines.append("")
    lines.append("### 呼叫鏈（確認事實，三次皆同構）")
    lines.append("```")
    lines.append("paper_main.main → asyncio.run(run_paper_loop)")
    lines.append("  → engine.run_paper_loop → scan_once → manage_all")
    lines.append("    → portfolio.save_account → util.atomic_write_json")
    lines.append("      → os.replace(tmp, account_state.json)")
    lines.append("PermissionError: [WinError 5] 存取被拒。: '.tmp_….json' -> 'account_state.json'")
    lines.append("```")
    lines.append("時間點：①≈18:01（空手）②≈19:41（HYPE 仍掛單）③≈21:03（HYPE 已持倉）。")
    lines.append("")
    lines.append("### 寫檔方式")
    lines.append("- `atomic_write_json`：同目錄 `mkstemp` 寫入→`fsync`→`os.replace` 覆蓋 `account_state.json`。")
    lines.append("- 現版另有 WinError 5/32 最多 12 次重試（`util.py` L28–53）；**崩潰當下仍最終 raise**（未吞掉）。")
    lines.append("- **可證實：** replace 目標檔被拒絕（WinError 5）。**不能證實**具體占用者（防毒／索引／另一讀寫進程）— 無 handle 診斷日誌 → 根因進程級 **未知**。")
    lines.append("")
    lines.append("### 錯誤後行為（確認事實）")
    lines.append("- 例外未在 `manage_all`／`run_paper_loop` 內捕獲 → **整個 Python 進程結束**。")
    lines.append("- 之後無掃描、無盯倉、無出場，直到下一次 `===== launch =====`。")
    lines.append("")
    lines.append("### HYPE 殘倉")
    lines.append(f"| 項 | 值 |")
    lines.append(f"|----|----|")
    lines.append(f"| trade_id | {hype.get('trade_id')} |")
    lines.append(f"| qty_remaining | {hype.get('qty_remaining')} |")
    lines.append(f"| entry_price / entry_time | {hype.get('entry_price')} / {hype.get('entry_time')} |")
    lines.append(f"| margin_u / notional | {hype.get('margin_u')} / {hype.get('notional_initial')} |")
    lines.append(f"| tp_usdt / sl_usdt（每倉） | {hype.get('tp_usdt')} / {hype.get('sl_usdt')} |")
    lines.append(f"| acct mfe_u / mae_u | {fmt(hype.get('mfe_u'))} / {fmt(hype.get('mae_u'))} （USDT 毛利口徑，非%） |")
    lines.append(f"| 最後 roll 樣本時間 | {parse_ts(last_roll_ts).isoformat() if last_roll_ts else NA} |")
    lines.append(f"| 最後 roll px（mid 樣本） | {fmt(last_roll_px)} |")
    lines.append(f"| account.updated_at | {acct.get('updated_at')} |")
    lines.append("")
    lines.append("**不得**把截止後行情當成已平倉。浮盈虧單位：帳戶 mfe/mae 為 **U 毛利**；日誌「浮動淨」為估平倉淨（含假設滑價／費）。")
    lines.append("")
    lines.append("### 重啟恢復（確認事實）")
    lines.append("- `load_account()` 若 `account_state.json` 存在則整份載入（含 positions/pending），**不**新建 run_id（`portfolio.py` L64–77）。")
    lines.append("- 本批 run_id 始終 `2026-11-21T1433_040b980f`。")
    lines.append("- 去重：`applied_exchange_trade_keys`／settlement_journal 恢復邏輯存在；paper 成交鍵主要靠本地 trade_id。")
    lines.append("- 20:15 重啟時日誌先顯示掛單 HYPE，隨即成交進場 — 殘限價被恢復後觸價。")
    lines.append("")
    lines.append("### 最小修復建議（本輪不實作）")
    lines.append("1. `save_account` 失敗時降級：保留 tmp、寫 alert、**不要**讓主迴圈崩掉（或有界重試後跳過本輪保存）。")
    lines.append("2. 避免多進程同時讀寫同一 `account_state.json`；鎖外另加「寫入中」偵測。")
    lines.append("3. 崩潰復原檢查：pending/position 與 fills 對帳後再允許新訊號。")
    lines.append("")

    lines.append("## 4. 可比較價格路徑（借用 C vault）")
    lines.append("")
    lines.append(f"- 來源：`{KLINE_VAULT}`（paper C sibling）。")
    lines.append("- 價種：**1m last OHLC**；D 訊號用 **15m**、出場盯 **book mid/ask** → **不完全同質**。標 `price_kind=sibling_1m_last_ohlc`。")
    lines.append("- 商品集合：本批 11 個成交符號在 C vault **皆有** 1m 檔。")
    lines.append("- 進出場當根可能含持倉外價格 → `boundary_error=true`。")
    lines.append("- **嚴格完全位於持倉內的整分 K**：短單常為 0 根 → `path_strict_inside.n=0` 標 N/A。")
    lines.append("")
    lines.append("### 逐筆固定窗（重疊 K；MFE/MAE/期末順向%）")
    lines.append("")
    for r in sorted(path_rows, key=lambda x: x["entry_ts"]):
        po = r["path_overlap"] or {}
        ps = r["path_strict_inside"] or {}
        lines.append(
            f"**{r['symbol']}** hold={fmt(r.get('hold_sec'),1)}s boundary={r['boundary_error']} "
            f"overlap_n={po.get('n')} strict_n={ps.get('n',0)} "
            f"MFE%={fmt(po.get('mfe_pct'))} MAE%={fmt(po.get('mae_pct'))} "
            f"MFE_U={fmt(po.get('mfe_u'))} MAE_U={fmt(po.get('mae_u'))}"
        )
        for m in WINDOWS:
            w = r["fixed_windows"][m]
            if w["status"] != "OK":
                lines.append(f"  - {m}m: {w['status']}")
            else:
                lines.append(
                    f"  - {m}m: ret={fmt(w['end_ret_pct'])}% MFE%={fmt(w['mfe_pct'])} MAE%={fmt(w['mae_pct'])} bars={w['n_bars']}"
                )
        if r["post_sl"]:
            lines.append("  - SL 後回撤／達標（自出場起，1m；順序可能不明）：")
            for k, v in r["post_sl"].items():
                lines.append(
                    f"    - {k}: hit={v.get('hit')} sec={fmt(v.get('sec'),1)} "
                    f"mae%_before={fmt(v.get('mae_pct_before'))} order={v.get('order')}"
                )
        lines.append("")

    lines.append("### 進場聚集與持倉重疊")
    lines.append(f"- 尖峰同時持倉數（含未平至窗末）：**{peak}**")
    lines.append("- 進場分鐘聚集（≥2）：")
    for k, syms in sorted(by_min.items()):
        if len(syms) >= 2:
            lines.append(f"  - {k}: {syms}")
    lines.append("- **合理推論：** 19:30–19:32 多倉同時開／平，損失高度集中在同一小波段；非完全獨立樣本。")
    lines.append("")

    lines.append("## 5. 固定進場局部診斷 A／B／C")
    lines.append("")
    lines.append("> **不是完整策略回測。** 固定原進場；不重建不同退出對後續槽位／冷卻／進場的影響。")
    lines.append("> **VOL_SPIKE 無法用 1m OHLC 精準重建**（需 mid 滾動 15m 振幅）。因此：")
    lines.append("> - A/B/C 的 K 線模型只跑 **TP／SL 價格觸及**；")
    lines.append("> - 凡實際含 VOL 的成交，A 的「重現」預期系統性偏差；")
    lines.append("> - C「移除 VOL」= 在此模型下與 A 相同屏障，**不能**捏造「若無 VOL 的成交價」，只能對照實際原因標籤。")
    lines.append("")
    lines.append("### A 重現誤差（OHLC SL/TP vs 實際）")
    lines.append("")
    lines.append("| symbol | actual_reason | model_A | px_err | net_err | VOL in actual? |")
    lines.append("|--------|---------------|---------|--------|---------|----------------|")
    for r in cf_table:
        if r["actual_reason"] == "OPEN":
            continue
        rep = r["repro_A"]
        if not isinstance(rep, dict):
            lines.append(f"| {r['symbol']} | {r['actual_reason']} | {r['A'].get('status')} | {NA} | {NA} | |")
            continue
        lines.append(
            f"| {r['symbol']} | {rep['actual_reason']} | {rep['model_reason']} | "
            f"{fmt(rep['px_err'],6)} | {fmt(rep['net_err'])} | {rep['vol_in_actual']} |"
        )
    lines.append("")
    sa, sb, sc = sum_exited("A"), sum_exited("B"), sum_exited("C")
    lines.append("### 匯總（模型已平淨；未平另列）")
    lines.append(f"- A TP15/SL3：已平 n={sa['exited_n']} 淨合計={fmt(sa['net_sum'])}；未平={sa['open_n']}（若出場估淨合計 {fmt(sa['unreal_sum'])}）；歧義={sa['amb_n']}")
    lines.append(f"- B TP15/SL5：已平 n={sb['exited_n']} 淨合計={fmt(sb['net_sum'])}；未平={sb['open_n']}（估 {fmt(sb['unreal_sum'])}）；歧義={sb['amb_n']}")
    lines.append(f"- C 無VOL標籤對照／同 TP15/SL3 模型：已平 n={sc['exited_n']} 淨合計={fmt(sc['net_sum'])}（與 A 同模型屏障）")
    lines.append(f"- 實際已平經濟淨（基線）：{fmt(sum(r['actual_cycle_net'] or 0 for r in cf_table if r['actual_cycle_net'] is not None))}")
    lines.append("")
    lines.append("### 放寬 SL（B vs A 模型）逐筆效應計數")
    lines.append(f"{dict(beff)}")
    lines.append("")
    lines.append("| symbol | B_effect | A_net | B_net | actual_net |")
    lines.append("|--------|----------|-------|-------|------------|")
    for r in cf_table:
        if r["actual_reason"] == "OPEN":
            lines.append(
                f"| {r['symbol']} | OPEN_RESIDUAL | {r['A'].get('status')} | {r['B'].get('status')} | OPEN |"
            )
            continue
        lines.append(
            f"| {r['symbol']} | {r['B_effect']} | {fmt(r['A'].get('cycle_net') if r['A'].get('status')=='EXITED' else None)} | "
            f"{fmt(r['B'].get('cycle_net') if r['B'].get('status')=='EXITED' else None)} | {fmt(r['actual_cycle_net'])} |"
        )
    lines.append("")
    lines.append("**解讀約束：** 即使 B 顯示「救回」或「更大虧損」，也只是固定進場＋1m 觸價假設；**不能**因十筆全虧就預設 SL 太緊，也**不能**把「再跑 30 筆」當驗證通過門檻。")
    lines.append("")

    lines.append("## 確認事實／推論／未知")
    lines.append("")
    lines.append("### 確認事實")
    lines.append("| 項 | 內容 |")
    lines.append("|----|------|")
    lines.append("| TP/SL 尺度 | 每倉 gross ±15/±3 U（滿倉時）；非組合 |")
    lines.append("| 無時間退出／無分批 TP | 程式與本批成交一致 |")
    lines.append("| PermissionError 鏈 | manage_all→save_account→os.replace；進程終止 |")
    lines.append("| 崩潰後無監控 | 需人工／腳本重啟才恢復 |")
    lines.append("| 歷史 BAR 非 0.0075 | 多筆訊號 max_bar_amp∈(0.0075,0.01] 仍發出 |")
    lines.append("| HYPE 殘倉 | qty=5.04 @59.446；tp/sl=15/3；未平 |")
    lines.append("| 帳本對帳 | 見主 BRIEF；本輪未改數字口徑 |")
    lines.append("")
    lines.append("### 合理推論")
    lines.append("| 項 | 內容 |")
    lines.append("|----|------|")
    lines.append("| 18:36 後 BAR≈0.01 | 與 launch_meta_d 一致，但注入路徑未找到 |")
    lines.append("| 損失聚集 | 19:30 前後同波多倉重疊 |")
    lines.append("| 主損驅動 | 進場後快速逆向接近/超過 SL 價距；VOL 常與 SL 同列 |")
    lines.append("| WinError 5 | 檔案被其他句柄占用的典型表現，但占用者未鑑定 |")
    lines.append("")
    lines.append("### 未知")
    lines.append("| 項 | 缺什麼 |")
    lines.append("|----|--------|")
    lines.append("| RANGE 歷史有效值 | 0.015 vs 0.02 無區分性訊號 |")
    lines.append("| 14:33–18:01 段 BAR 值 | 無足夠 >0.0075 訊號反證 |")
    lines.append("| launch_meta_d 寫入者 | 倉庫 tools 無對應賦值碼 |")
    lines.append("| PermissionError 占用進程 | 無 handle/ETW 證據 |")
    lines.append("| VOL 精準反事實 | 缺當時 mid 滾動序列 |")
    lines.append("| 同根 TP/SL 先後 | 僅 1m OHLC |")
    lines.append("| HYPE 最終結果 | 崩潰後未平；截止後行情不當平倉 |")
    lines.append("")
    lines.append("附件：`supplement_data.json`（本腳本結構化輸出）。")

    text = "\n".join(lines)
    (OUT / "SUPPLEMENT.md").write_text(text, encoding="utf-8")
    (OUT / "supplement_data.json").write_text(
        json.dumps(
            {
                "tpsl_rows": tpsl_rows,
                "sig_amp_summary": {
                    "n": len(sig_amps),
                    "n_gt_0075": n_gt_0075,
                    "n_gt_01": n_gt_01,
                    "max_bar": max_bar,
                    "max_range": max_range,
                },
                "path_rows": path_rows,
                "cf_table": cf_table,
                "cf_sums": {"A": sa, "B": sb, "C": sc},
                "b_effects": dict(beff),
                "overlap_peak": peak,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("Wrote", OUT / "SUPPLEMENT.md")
    print("B effects", dict(beff))
    print("sig >0.0075", n_gt_0075, "max_bar", max_bar)


if __name__ == "__main__":
    main()
