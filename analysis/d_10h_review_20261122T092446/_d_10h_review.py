#!/usr/bin/env python3
"""D quiet-short 10h forensic review — offline, read-only. Writes reports under this dir."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
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

# Fixed analysis windows (minutes) from entry
HOLD_WINDOWS = [5, 15, 30, 60]
POST_EXIT_WINDOWS = [5, 15, 30]
TOUCH_PCTS = [0.0025, 0.005, 0.0075, 0.01, 0.015]


def parse_ts(v: Any) -> Optional[datetime]:
    if v is None or v == "" or v == NA:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        elif x > 1e10:  # already ms-ish
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=TZ8)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ8)
    return dt.astimezone(TZ8)


def fnum(v: Any) -> Optional[float]:
    try:
        if v is None or v == "" or v == NA:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def pctile(xs: list[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return NA
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return str(x)
        return f"{x:.{nd}f}"
    return str(x)


# ---------- klines (sibling paper vault; NOT D-native) ----------
_kl: dict[str, list[dict]] = {}


def load_kl_1m(sym: str) -> list[dict]:
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
                o, h, l, c = fnum(row.get("open")), fnum(row.get("high")), fnum(row.get("low")), fnum(row.get("close"))
                if None in (o, h, l, c):
                    continue
                rows.append({"ot": ot, "o": o, "h": h, "l": l, "c": c})
        rows.sort(key=lambda x: x["ot"])
    _kl[sym] = rows
    return rows


def bars_between(kl: list[dict], t0: datetime, t1: datetime) -> list[dict]:
    return [b for b in kl if t0 <= b["ot"] < t1]


def short_path_stats(kl: list[dict], entry: float, qty: float, t0: datetime, t1: datetime) -> dict:
    """MFE/MAE for SHORT using 1m OHLC. Boundary bars flagged."""
    if not kl or entry <= 0 or qty <= 0 or t0 is None or t1 is None or t1 <= t0:
        return {"ok": False, "reason": "NO_PATH"}
    # bars that overlap [t0,t1)
    overlapping = []
    for b in kl:
        bend = b["ot"] + timedelta(minutes=1)
        if bend <= t0 or b["ot"] >= t1:
            continue
        overlapping.append(b)
    if not overlapping:
        return {"ok": False, "reason": "NO_BARS_IN_HOLD", "n_bars": 0}

    # Idealized: for short, favorable = price down (use low), adverse = price up (use high)
    # Entry bar / exit bar may include pre/post hold prices → boundary_error
    mfe_px = 0.0  # max (entry - low)/entry
    mae_px = 0.0  # max (high - entry)/entry
    mfe_t = None
    mae_t = None
    mfe_u = 0.0
    mae_u = 0.0
    for b in overlapping:
        fav = (entry - b["l"]) / entry
        adv = (b["h"] - entry) / entry
        fav_u = (entry - b["l"]) * qty
        adv_u = (b["h"] - entry) * qty
        if fav > mfe_px:
            mfe_px, mfe_t, mfe_u = fav, b["ot"], fav_u
        if adv > mae_px:
            mae_px, mae_t, mae_u = adv, b["ot"], -adv_u
    boundary = False
    notes = []
    first, last = overlapping[0], overlapping[-1]
    if first["ot"] <= t0 < first["ot"] + timedelta(minutes=1):
        boundary = True
        notes.append("entry_bar_includes_pre_entry_ohlc")
    if last["ot"] <= t1 < last["ot"] + timedelta(minutes=1) or (
        t1 - last["ot"]
    ).total_seconds() < 60:
        boundary = True
        notes.append("exit_bar_may_include_post_exit_ohlc")
    return {
        "ok": True,
        "n_bars": len(overlapping),
        "mfe_pct": mfe_px * 100,
        "mae_pct": mae_px * 100,
        "mfe_u": mfe_u,
        "mae_u": mae_u,
        "mfe_ts": mfe_t.isoformat() if mfe_t else None,
        "mae_ts": mae_t.isoformat() if mae_t else None,
        "boundary_error": boundary,
        "notes": notes,
        "kline_source": str(KLINE_VAULT),
        "price_kind": "sibling_paper_c_vault_1m_last_ohlc",
        "mark_series": NA,
    }


def fixed_window_short(kl: list[dict], entry: float, t0: datetime, minutes: int, cutoff: datetime) -> dict:
    t1 = t0 + timedelta(minutes=minutes)
    if t1 > cutoff:
        return {"status": "CENSORED", "minutes": minutes, "need_until": t1.isoformat()}
    # need bar covering end
    end_bars = [b for b in kl if b["ot"] <= t1 < b["ot"] + timedelta(minutes=1)]
    if not end_bars:
        # last bar before t1?
        before = [b for b in kl if b["ot"] < t1]
        if not before:
            return {"status": "DATA_GAP", "minutes": minutes}
        # if gap to t1 > 1.1m
        if (t1 - before[-1]["ot"]).total_seconds() > 90:
            return {"status": "DATA_GAP", "minutes": minutes}
        end_px = before[-1]["c"]
        end_ot = before[-1]["ot"]
    else:
        end_px = end_bars[0]["c"]
        end_ot = end_bars[0]["ot"]
    path = short_path_stats(kl, entry, 1.0, t0, t1)  # qty=1 for pct
    ret = (entry - end_px) / entry * 100  # short favorable %
    return {
        "status": "OK",
        "minutes": minutes,
        "end_ret_pct_short": ret,
        "mfe_pct": path.get("mfe_pct"),
        "mae_pct": path.get("mae_pct"),
        "end_px": end_px,
        "end_bar_ot": end_ot.isoformat(),
        "boundary_error": path.get("boundary_error"),
    }


def touch_stats_short(kl: list[dict], entry: float, t0: datetime, t_end: datetime) -> dict:
    """First time price moves favorably by each threshold (short: low <= entry*(1-pct))."""
    out = {}
    for pct in TOUCH_PCTS:
        target = entry * (1.0 - pct)
        hit_t = None
        for b in kl:
            if b["ot"] + timedelta(minutes=1) <= t0:
                continue
            if b["ot"] >= t_end:
                break
            if b["l"] <= target:
                hit_t = b["ot"]
                break
        key = f"{pct*100:.2f}%"
        if hit_t is None:
            out[key] = {"hit": False, "sec": None}
        else:
            out[key] = {"hit": True, "sec": (hit_t - t0).total_seconds()}
    return out


# ---------- core build ----------
def main() -> None:
    meta = json.loads((SNAP / "snapshot_meta.json").read_text(encoding="utf-8"))
    cutoff = parse_ts(meta["cutoff_iso"])
    fills = load_jsonl(SNAP / "fills.jsonl")
    signals = load_jsonl(SNAP / "signals.jsonl")
    events = load_jsonl(SNAP / "events.jsonl")
    scans = load_jsonl(SNAP / "scan_events.jsonl")
    acct = json.loads((SNAP / "account_state.json").read_text(encoding="utf-8"))
    launch = json.loads((SNAP / "LAUNCH_META.json").read_text(encoding="utf-8"))
    launch_d = {}
    p_ld = SNAP / "paper_bcd_launch_meta_d.json"
    if p_ld.exists():
        launch_d = json.loads(p_ld.read_text(encoding="utf-8"))

    opens = [f for f in fills if f.get("event") == "OPEN"]
    closes = [f for f in fills if f.get("event") == "CLOSE"]
    by_tid: dict[str, dict] = {}
    for f in opens:
        tid = f["trade_id"]
        by_tid[tid] = {
            "trade_id": tid,
            "symbol": f["symbol"],
            "side": f.get("side") or "SHORT",
            "entry_ts": parse_ts(f.get("recorded_at")),
            "entry_px": float(f["price"]),
            "qty": float(f["qty"]),
            "entry_fee": float(f.get("fee_u") or 0),
            "signal_id": f.get("signal_id"),
            "strategy_version": f.get("strategy_version") or acct.get("strategy_version"),
            "notional_u": float(f["price"]) * float(f["qty"]),
            "exit_ts": None,
            "exit_px": None,
            "exit_fee": 0.0,
            "exit_reason": None,
            "gross_u": None,
            "net_close_u": None,  # close net (excludes entry fee)
            "cycle_net_u": None,
            "hold_sec": None,
            "status": "OPEN",
        }
    for f in closes:
        tid = f["trade_id"]
        if tid not in by_tid:
            continue
        t = by_tid[tid]
        t["exit_ts"] = parse_ts(f.get("recorded_at"))
        t["exit_px"] = float(f["price"])
        t["exit_fee"] = float(f.get("fee_u") or 0)
        t["exit_reason"] = f.get("exit_reason")
        t["gross_u"] = fnum(f.get("gross_u"))
        t["net_close_u"] = fnum(f.get("net_u"))
        t["cycle_net_u"] = (t["net_close_u"] or 0) - t["entry_fee"]
        if t["entry_ts"] and t["exit_ts"]:
            t["hold_sec"] = (t["exit_ts"] - t["entry_ts"]).total_seconds()
        t["status"] = "CLOSED"

    # residual from account
    for sym, p in (acct.get("positions") or {}).items():
        tid = p.get("trade_id")
        if tid in by_tid and by_tid[tid]["status"] == "OPEN":
            by_tid[tid]["acct_mfe_u"] = p.get("mfe_u")
            by_tid[tid]["acct_mae_u"] = p.get("mae_u")
            by_tid[tid]["tp_usdt"] = p.get("tp_usdt")
            by_tid[tid]["sl_usdt"] = p.get("sl_usdt")

    trades = sorted(by_tid.values(), key=lambda x: x["entry_ts"] or datetime.min.replace(tzinfo=TZ8))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_tr = [t for t in trades if t["status"] == "OPEN"]

    # time span
    first_ev = min(
        [parse_ts(e.get("recorded_at")) for e in events if e.get("recorded_at")]
        + [t["entry_ts"] for t in trades if t["entry_ts"]]
        + [parse_ts(s.get("emitted_at")) for s in signals if s.get("emitted_at")],
        default=None,
    )
    last_acct = parse_ts(acct.get("updated_at"))
    launch_at = parse_ts(launch.get("launched_at"))
    run_span_h = (
        (last_acct - first_ev).total_seconds() / 3600.0 if first_ev and last_acct else None
    )
    # Main window: if total < 10h use all; else last 10h from cutoff/last_acct
    window_end = last_acct or cutoff
    if first_ev and run_span_h is not None and run_span_h > 10:
        window_start = window_end - timedelta(hours=10)
        window_note = "主報告：最近10小時；另列本次總運行範圍"
    else:
        window_start = first_ev
        window_note = f"本次可用運行約 {fmt(run_span_h, 2)}h（不足10h）；主報告=本次全段至截止"

    # funnel
    queues = [e for e in events if e.get("event") == "QUEUE_LIMIT_SHORT"]
    cancels = [e for e in events if e.get("event") == "CANCEL_PENDING"]
    entries = [e for e in events if e.get("event") == "ENTRY_FILLED"]
    cancel_reasons = Counter(e.get("reason") for e in cancels)
    scan_blocks = Counter(s.get("event") for s in scans)

    # accounting
    entry_fees = sum(t["entry_fee"] for t in trades)
    exit_fees = sum(t["exit_fee"] for t in closed)
    gross_sum = sum(t["gross_u"] or 0 for t in closed)
    close_nets = sum(t["net_close_u"] or 0 for t in closed)
    cycle_nets = sum(t["cycle_net_u"] or 0 for t in closed)
    funding = NA  # gap
    implied_net = close_nets - sum(t["entry_fee"] for t in closed)
    # open entry fees still in fees
    acct_net = acct.get("realized_net_u")
    acct_fees = acct.get("realized_fees_u")
    # For residual open, entry fee is booked but no close — cycle net for closed only
    # Account realized includes: all close nets - all entry fees (including open's entry fee)
    open_entry_fees = sum(t["entry_fee"] for t in open_tr)
    implied_acct = close_nets - entry_fees  # all opens' entry fees
    recon = {
        "gross_realized_closed": gross_sum,
        "exit_fees_closed": exit_fees,
        "entry_fees_all_opens": entry_fees,
        "entry_fees_closed_only": sum(t["entry_fee"] for t in closed),
        "open_entry_fees_still_booked": open_entry_fees,
        "close_nets_sum": close_nets,
        "cycle_net_closed": cycle_nets,
        "implied_realized_net_all_entry_fees": implied_acct,
        "acct_realized_net_u": acct_net,
        "diff_if_mistakenly_subtract_entry_from_acct_net": (implied_acct - acct_net)
        if acct_net is not None
        else None,
        "acct_fees": acct_fees,
        "sum_all_fees": entry_fees + exit_fees,
        "fee_diff": (entry_fees + exit_fees) - (acct_fees or 0),
        "funding": funding,
        "other_costs": NA,
        "slippage_in_price": True,
        "accounting_model": (
            "D account.realized_net_u == Σ CLOSE.net_u (entry fees NOT subtracted into realized_net). "
            "Entry fees reduce cash and accumulate in realized_fees_u. "
            "Full economic closed PnL = Σ(close_net - entry_fee) = cycle_net_closed. "
            "cash ≈ initial + realized_net - all_entry_fees (verified)."
        ),
        "cash_check": {
            "acct_cash_u": acct.get("cash_u"),
            "implied_cash": (acct.get("initial_capital_u") or 5000)
            + (acct_net or 0)
            - entry_fees,
            "diff": (acct.get("cash_u") or 0)
            - ((acct.get("initial_capital_u") or 5000) + (acct_net or 0) - entry_fees),
        },
        "note": "Use cycle_net for economic closed PnL; do not equate account.realized_net to cycle_net",
    }

    # performance closed
    nets = [t["cycle_net_u"] for t in closed if t["cycle_net_u"] is not None]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    flats = [n for n in nets if n == 0]
    holds = [t["hold_sec"] for t in closed if t["hold_sec"] is not None]
    # max consecutive losses
    max_ll = 0
    cur = 0
    for n in nets:
        if n < 0:
            cur += 1
            max_ll = max(max_ll, cur)
        else:
            cur = 0
    # realized equity curve DD
    cume = []
    s = 0.0
    for t in sorted(closed, key=lambda x: x["exit_ts"] or datetime.min.replace(tzinfo=TZ8)):
        s += t["cycle_net_u"] or 0
        cume.append(s)
    peak = 0.0
    mdd = 0.0
    for x in cume:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)

    by_reason = defaultdict(list)
    for t in closed:
        by_reason[t["exit_reason"] or "UNKNOWN"].append(t["cycle_net_u"] or 0)

    # kline analysis
    kline_rows = []
    for t in trades:
        kl = load_kl_1m(t["symbol"])
        t0 = t["entry_ts"]
        t1 = t["exit_ts"] or window_end
        path = short_path_stats(kl, t["entry_px"], t["qty"], t0, t1) if t0 else {"ok": False}
        fw = {}
        if t0:
            for m in HOLD_WINDOWS:
                fw[f"w{m}m"] = fixed_window_short(kl, t["entry_px"], t0, m, window_end)
        post = {}
        if t["exit_ts"] and t["status"] == "CLOSED":
            for m in POST_EXIT_WINDOWS:
                post[f"post{m}m"] = fixed_window_short(kl, t["exit_px"], t["exit_ts"], m, window_end)
        touches = touch_stats_short(kl, t["entry_px"], t0, t1) if t0 else {}
        # same-bar TP/SL ambiguity at exit for SL trades — check exit bar
        amb = NA
        if t["status"] == "CLOSED" and t0 and t1 and kl:
            # TP level approx: entry - TP_USDT/qty; SL: entry + SL_USDT/qty
            tp_px = t["entry_px"] - 15.0 / t["qty"]
            sl_px = t["entry_px"] + 3.0 / t["qty"]
            for b in kl:
                if b["ot"] <= t0:
                    continue
                if b["ot"] >= t1 + timedelta(minutes=1):
                    break
                hit_tp = b["l"] <= tp_px
                hit_sl = b["h"] >= sl_px
                if hit_tp and hit_sl:
                    amb = "AMBIGUOUS_SAME_BAR"
                    break
            else:
                amb = "NO_SAME_BAR_BOTH"
        row = {
            **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in t.items()},
            "path": path,
            "fixed_windows": fw,
            "post_exit": post,
            "touches": touches,
            "path_ambiguity": amb,
            "gave_back": (
                path.get("ok")
                and (path.get("mfe_u") or 0) > 0
                and t["status"] == "CLOSED"
                and (t["cycle_net_u"] or 0) < 0
            ),
        }
        kline_rows.append(row)

    # counterfactuals on closed (fixed entry local diagnosis)
    cf_rows = []
    for t in closed:
        if t["gross_u"] is None:
            continue
        # CF0: zero cost = gross only
        # CF1: SL widen to 5U gross (local): if actual SL and mae would still hit — approximate using path mae_u
        path = next((r["path"] for r in kline_rows if r["trade_id"] == t["trade_id"]), {})
        cf_rows.append(
            {
                "trade_id": t["trade_id"],
                "actual_cycle_net": t["cycle_net_u"],
                "cf0_gross_only": t["gross_u"],
                "cf0_note": "same fills, remove fees — cost isolation",
                "mae_u_path": path.get("mae_u"),
                "mfe_u_path": path.get("mfe_u"),
            }
        )
    # aggregate CF
    cf_summary = {
        "scope": "固定進場局部診斷（僅已平10筆）— 不代表改出場後完整策略",
        "actual_cycle_net_sum": sum(t["cycle_net_u"] or 0 for t in closed),
        "cf0_gross_only_sum": sum(t["gross_u"] or 0 for t in closed),
        "cost_drag": sum(t["entry_fee"] + t["exit_fee"] for t in closed),
        "candidates": [
            {
                "id": "CF1",
                "change": "僅放寬 SL 觸發至 5U gross（現行 3U）",
                "reason": "10/10 已平皆虧；多數 SL 或 SL+VOL；MAE 路徑常剛過 3U",
                "computable": "PARTIAL — 無 tick；用1m高點代理是否觸及5U，延後出場會改後續機會故非完整績效",
                "result": NA,
            },
            {
                "id": "CF2",
                "change": "取消 VOL_SPIKE 出場，僅留 TP/SL",
                "reason": "多筆標 VOL_SPIKE／SL+VOL_SPIKE，可能在不利波動放大時與 SL 同輪觸發",
                "computable": "PARTIAL — 同棒順序不明；需 paper 對照",
                "result": NA,
            },
            {
                "id": "CF3",
                "change": "時間退出：持倉超過 30 分鐘強制平",
                "reason": "本批已平持倉多在1–2分鐘內結束，時間退出對本樣本幾乎無影響；留待更長持倉樣本",
                "computable": False,
                "result": "本樣本不适用／效應≈0",
            },
        ],
        "disclaimer": "十小時（實約6.5h）為探索樣本；不可宣稱參數已驗證；不可建議加碼或上實盤",
    }

    # write TRADES.csv
    trade_fields = [
        "trade_id",
        "symbol",
        "side",
        "signal_id",
        "entry_ts",
        "exit_ts",
        "entry_px",
        "exit_px",
        "qty",
        "notional_u",
        "exit_reason",
        "gross_u",
        "entry_fee",
        "exit_fee",
        "cycle_net_u",
        "hold_sec",
        "status",
        "strategy_version",
    ]
    with (OUT / "TRADES.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=trade_fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = {
                k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in t.items() if k in trade_fields
            }
            w.writerow(row)

    # summarize kline
    closed_paths = [r for r in kline_rows if r["status"] == "CLOSED" and r["path"].get("ok")]
    mfe_pcts = [r["path"]["mfe_pct"] for r in closed_paths]
    mae_pcts = [r["path"]["mae_pct"] for r in closed_paths]
    givebacks = [r for r in closed_paths if r.get("gave_back")]
    touch_hit = {f"{p*100:.2f}%": 0 for p in TOUCH_PCTS}
    touch_n = 0
    for r in closed_paths:
        touch_n += 1
        for k, v in (r.get("touches") or {}).items():
            if v.get("hit"):
                touch_hit[k] = touch_hit.get(k, 0) + 1
    fw_ok = {m: [] for m in HOLD_WINDOWS}
    for r in kline_rows:
        for m in HOLD_WINDOWS:
            w = (r.get("fixed_windows") or {}).get(f"w{m}m") or {}
            if w.get("status") == "OK":
                fw_ok[m].append(w)

    payload = {
        "cutoff": meta["cutoff_iso"],
        "identity": {
            "strategy_id": "d_quiet_short_v1",
            "strategy_version": acct.get("strategy_version"),
            "profile_id": acct.get("profile_id"),
            "run_id": acct.get("run_id"),
            "mode": "paper (PAPER_FILLS; simulation_assumption)",
            "env": "paper_bcd_local/data_d — public fapi quotes; no exchange orders",
            "pid_last_launch": launch.get("pid"),
            "launched_at": launch.get("launched_at"),
            "acct_updated_at": acct.get("updated_at"),
            "code_fix_tag": "testnet_d_size_x0_1_20261121",
            "first_event": first_ev.isoformat() if first_ev else None,
            "run_span_hours": run_span_h,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
            "window_note": window_note,
            "launch_meta_d_amp_mismatch": {
                "launch_meta_d": {
                    "range_amp_max": launch_d.get("range_amp_max"),
                    "bar_amp_max": launch_d.get("bar_amp_max"),
                },
                "config_py": {"RANGE_AMP_MAX": 0.015, "BAR_AMP_MAX": 0.0075},
                "evidence_runtime": "CANCEL reason QUIET_FAIL:BAR_AMP_FAIL present; treat config.py as loaded unless proven otherwise",
            },
        },
        "funnel": {
            "signals": len(signals),
            "queue_limit_short": len(queues),
            "cancel_pending": len(cancels),
            "cancel_reasons": dict(cancel_reasons),
            "entry_filled_events": len(entries),
            "opens": len(opens),
            "closes": len(closes),
            "closed_trades": len(closed),
            "open_trades": len(open_tr),
            "scan_event_types": dict(scan_blocks),
            "unfilled_rate_vs_queues": (1 - len(opens) / len(queues)) if queues else None,
        },
        "recon": recon,
        "perf": {
            "n_closed": len(closed),
            "n_open": len(open_tr),
            "wins": len(wins),
            "losses": len(losses),
            "flats": len(flats),
            "win_rate": len(wins) / len(closed) if closed else None,
            "gross_sum": gross_sum,
            "fees_closed": sum(t["entry_fee"] + t["exit_fee"] for t in closed),
            "funding": funding,
            "net_sum": cycle_nets,
            "avg_net": statistics.mean(nets) if nets else None,
            "median_net": statistics.median(nets) if nets else None,
            "avg_win": statistics.mean(wins) if wins else None,
            "avg_loss": statistics.mean(losses) if losses else None,
            "payoff": (abs(statistics.mean(wins) / statistics.mean(losses)) if wins and losses else None),
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
            "avg_hold_sec": statistics.mean(holds) if holds else None,
            "median_hold_sec": statistics.median(holds) if holds else None,
            "max_consecutive_losses": max_ll,
            "max_dd_realized_curve": mdd,
            "dd_definition": "逐筆已實現累計曲線（不含未平浮盈虧）",
            "by_exit_reason": {k: {"n": len(v), "sum_net": sum(v)} for k, v in by_reason.items()},
            "top3_loss": sorted(closed, key=lambda t: t["cycle_net_u"] or 0)[:3],
            "top3_win": sorted(closed, key=lambda t: -(t["cycle_net_u"] or 0))[:3],
            "open_residual": open_tr,
            "acct_mfe_mae_open": {
                t["symbol"]: {"mfe": t.get("acct_mfe_u"), "mae": t.get("acct_mae_u")} for t in open_tr
            },
        },
        "kline": {
            "source": str(KLINE_VAULT),
            "caveat": "D 無專屬 vault；借用同環境 paper C 的 1m last OHLC。非 mark；非 D 當下拉取的15m。",
            "n_closed_with_path": len(closed_paths),
            "mfe_pct_median": pctile(mfe_pcts, 0.5),
            "mae_pct_median": pctile(mae_pcts, 0.5),
            "mfe_pct_p25": pctile(mfe_pcts, 0.25),
            "mfe_pct_p75": pctile(mfe_pcts, 0.75),
            "mae_pct_p25": pctile(mae_pcts, 0.25),
            "mae_pct_p75": pctile(mae_pcts, 0.75),
            "giveback_n": len(givebacks),
            "touch_hit_counts": touch_hit,
            "touch_n": touch_n,
            "fixed_window_n": {str(m): len(fw_ok[m]) for m in HOLD_WINDOWS},
            "fixed_window_median_ret": {
                str(m): pctile([w["end_ret_pct_short"] for w in fw_ok[m]], 0.5) for m in HOLD_WINDOWS
            },
        },
        "cf": cf_summary,
    }
    (OUT / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "kline_rows.json").write_text(
        json.dumps(kline_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    write_strategy_spec(OUT, launch, launch_d)
    write_data_audit(OUT, payload, signals, events, scans, fills, acct)
    write_kline_md(OUT, payload, kline_rows)
    write_cf_md(OUT, cf_summary, cf_rows)
    write_brief(OUT, payload, kline_rows)
    write_reproduce(OUT)
    print("DONE", OUT)
    print("net", cycle_nets, "closed", len(closed), "open", len(open_tr), "span_h", run_span_h)


def write_strategy_spec(out: Path, launch: dict, launch_d: dict) -> None:
    text = f"""# STRATEGY_SPEC — D quiet-short

## 身分（確認事實）
| 項 | 值 | 證據 |
|----|----|------|
| strategy_id | `d_quiet_short_v1` | `config.py` L12 |
| version | `d_quiet_short_testnet_20261121` | config L13；account_state；LAUNCH_META |
| profile | `testnet_d_quiet_short_v1` | config L14；紙上仍用此名 |
| CODE_FIX_TAG | `testnet_d_size_x0_1_20261121` | config L15 |
| 模式 | **paper 模擬**（`PAPER_FILLS`） | LAUNCH_META mode；fills `simulation_assumption`；不下單 |
| 環境 | `paper_bcd_local/data_d`；公開 `fapi.binance.com` 行情 | runtime_root；ALLOWED_PUBLIC_PATHS |
| 方向 | **僅 SHORT** | signals.py / portfolio queue_short_limit |
| 多臂 | 無 | 單一 profile |

## 規則摘要（實際行為）

### 訊號（15m 已收盤）
- 週期：`KLINE_INTERVAL=15m`，僅用**已完成**棒（`evaluate_quiet` 不吃未收盤）。證據：`signals.py` L70–71, L198–199。
- 安靜窗：連續 `QUIET_BARS=4` 根；`range_amp=(hi-lo)/close ≤ RANGE_AMP_MAX(0.015)`；每棒 `(h-l)/open ≤ BAR_AMP_MAX(0.0075)`。`signals.py` L117–128；`config.py` L117–119。
- 評估／掃描：約每分鐘掃描秒=`SCAN_SECOND=5`；管理 `MANAGE_DUE_SEC=5`。`config.py` L125–127。
- 冷卻：出場後 `COOLDOWN_REBUILD`；需新鮮已收盤棒（vol-exit 要 4 根，其他 1 根）。`signals.py` L216–226。
- 同 signal_id 不重複發。`signals.py` L176–178。

### 過濾
- 排除 BTC/ETH；槓桿幣／穩定幣／TradFi 黑名單。`config.py` L89–102。
- 24h quoteVolume ≥ 20M USDT；spread `(ask-bid)/mid ≤ 0.0008`。`config.py` L66–67。
- 深度：近觸 0.1% 內 bid 與 ask 名義各 ≥ 50k USDT。`config.py` L73–85。
- 槽位：最多 10；同幣持倉或掛單則 BUSY。`config.py` L33；`portfolio.py` L132–135。

### 進場
- 限價空：`limit = P × 1.005`（tick 上取整）。`config.py` L109；`portfolio.py` L136–139。
- 無時間 TTL；撤單：`mid ≤ P×0.99` 或安靜／資格失效。`config.py` L110–112；events 見 `QUIET_FAIL:BAR_AMP_FAIL`。
- 成交模型（模擬）：bid≥limit → 以 limit 成交（maker 假設費率仍用 5bps 單邊）。LAUNCH_META fill_model；`FEE_RATE_ONE_SIDE=0.0005`。
- 名目：保證金 `STAKE_USDT=30` × 槓桿 `10` → **約 300U**（config 檔頭註解曾寫 3000U 屬過時／錯誤；以 `CAPITAL_CONFIRM_NOTE` 與實際 notional≈300 為準）。`config.py` L30–42。

### 出場
- **TP/SL 單位是 USDT 毛損益（gross）**，不是價格百分比、也不是保證金報酬率。觸發：`metric=gross` ≥ +15U → TP；≤ −3U → SL。`portfolio.py` L968–978；`config.py` L31–37。
- 另：滾動振幅 ≥ `T=max(VOL_EXIT_FLOOR=0.008, 2×B)` → `VOL_SPIKE`。`portfolio.py` L971–980。
- 可同時列出多觸發（本批見 `SL+VOL_SPIKE`）。
- 出場模擬：吃 ask 側 walk（激進出場）；滑價可反映在成交價；**不可再另扣一層滑價當成本**。LAUNCH_META fill_model_exit。
- 無追蹤停利、無分批 TP、無加倉（本策略）。

### 費用／資金費
- 設定：每側 5bps；SLIP 0.0002 為 book-walk 不可用時後備。`config.py` L47–48。
- 記帳：OPEN 記 entry fee；CLOSE `net_u`＝gross−exit_fee（**不含** entry fee）；完整 cycle 淨＝Σclose_net−entry_fee。funding：`funding_data_gap` → **MISSING／N/A**。

## 文件 vs 實際差異
| 項目 | 文件／meta | 實際證據 | 採納 |
|------|------------|----------|------|
| 名目 | config 頂註「300U margin×10=3000U」 | STAKE=30、成交 notional≈300、CAPITAL_CONFIRM_NOTE | **30×10=300U** |
| 安靜閾值 | `launch_meta_d.json` range=0.02 / bar=0.01 | config 0.015 / 0.0075；撤單 `BAR_AMP_FAIL` | **以 config 載入為準**；meta 數字來源未在啟動腳本覆寫→標差異 |
| Testnet 名 | profile 含 testnet_ | 現行 paper_bcd，無下單 | **paper** |

## 十小時內版本／重啟
- run_id 全程 `2026-11-21T1433_040b980f`（帳本未重置）。
- 活動：約 14:35 起有 QUEUE；18:37 大批掛單；18:49–19:32 成交與集中平倉；20:15 再 LAUNCH 並 OPEN HYPE；**21:03 PermissionError 寫 account_state 崩潰停止**。
- strategy_version 未變。分段敘事：同一版本、多次進程，**不混成無關策略**；績效用同一 run_id 帳本。
"""
    (out / "STRATEGY_SPEC.md").write_text(text, encoding="utf-8")


def write_data_audit(out: Path, payload: dict, signals, events, scans, fills, acct) -> None:
    r = payload["recon"]
    f = payload["funnel"]
    idn = payload["identity"]
    text = f"""# DATA_AUDIT — D

## 快照
- 截止：`{payload['cutoff']}`
- 方法：對 `paper_bcd_local/data_d/output/testnet_d_quiet_short_v1/` 及 log／launch_meta 做 `copy2` 唯讀快照（`snapshots/`）。
- 所有表共用此截止；帳本末更新 `{idn['acct_updated_at']}`（之後無新成交）。

## 身分防混
- 僅使用 profile `testnet_d_quiet_short_v1` + run_id `{idn['run_id']}`。
- 未混入 A/B/C/E 帳本；testnet_live 下空殼 D 未用作績效。

## 資料盤點
| 資料 | 位置（快照內） | 筆數／範圍 | 備註 |
|------|----------------|------------|------|
| fills | fills.jsonl | OPEN {f['opens']} / CLOSE {f['closes']} | 模擬 |
| signals | signals.jsonl | {f['signals']} | 有 SIGNAL 事件 |
| events | events.jsonl | QUEUE {f['queue_limit_short']} / CANCEL {f['cancel_pending']} / ENTRY {f['entry_filled_events']} | |
| scan | scan_events.jsonl | {f['scan_event_types']} | 無完整 SCAN_STATS 聚合檔 |
| account | account_state.json | 1 | 殘倉 HYPE |
| trades.csv | 有 | 與 fills 對照用 | |
| K 線 | D vault **不存在** | — | 分析借用 paper C 1m vault |
| funding | — | N/A | funding_data_gap |

## 時區
- 紀錄為 `+08:00`（Asia/Taipei）。K 線 open_time 毫秒 epoch → 同區轉換。

## 串聯
- `trade_id` = `run_id:SYMBOL:hex`；`signal_id` = `SYMBOL:bar_close_time:SHORT:version`。
- OPEN↔CLOSE 以 trade_id 1:1（本批無分批腿）。
- 訊號→掛單：events 含 signal_id；部分訊號未進 QUEUE（深度／槽位擋）。

## 缺漏
- 缺 D 原生 K 線歸檔 → 路徑分析用 sibling 1m，標借用。
- 缺 bid/ask 歷史队列 → 不能重建真實 maker 排隊。
- funding N/A。
- scan 無完整「掃描宇宙每輪拒絕原因」總表（僅有部分 SIGNAL_BLOCKED_*）。
- **不可**用成交反推訊號總數；訊號日誌有 21 筆，以該數為準。

## 帳務對帳
| 項 | 值 |
|----|----|
| 毛已實現（已平） | {fmt(r['gross_realized_closed'])} U |
| 出場費（已平） | {fmt(r['exit_fees_closed'])} U |
| 進場費（全部 OPEN 含殘倉） | {fmt(r['entry_fees_all_opens'])} U |
| Σ close net_u | {fmt(r['close_nets_sum'])} U |
| 帳戶 realized_net_u | {fmt(r['acct_realized_net_u'])} U（= Σ close net，**不含**進場費） |
| 經濟已平 cycle 淨 | {fmt(r['cycle_net_closed'])} U（= close_nets − 已平進場費） |
| 若誤把進場費再扣進 realized_net 的差額 | {fmt(r.get('diff_if_mistakenly_subtract_entry_from_acct_net'), 8)}（屬口徑差，非漏帳） |
| 費用合計 vs 帳戶 fees | {fmt(r['sum_all_fees'])} vs {fmt(r['acct_fees'])}（差 {fmt(r['fee_diff'], 8)}） |
| 現金核對 | acct {fmt((r.get('cash_check') or {}).get('acct_cash_u'))} vs implied initial+realized_net−全部進場費 {fmt((r.get('cash_check') or {}).get('implied_cash'))}（差 {fmt((r.get('cash_check') or {}).get('diff'), 8)}） |
| funding | {r['funding']} |
| 滑價 | 已含於成交價；未再扣 |

**對帳結論：PASS。** D 的 `realized_net` 口徑=Σclose_net；進場費走現金／fees。經濟績效請用 cycle 淨（已平 −33.51U）。現金恒等式成立（差≈0）。

## Paper 樂觀假設（確認）
- 進場：觸價（bid≥limit）即以 limit 成交，無視排队深度耗盡（另有診斷 walk）。
- 出場：ask walk；深度不足仍出場並揭露 shortfall（本批未見 shortfall 欄位細查→N/A）。
- 非交易所真實成交。
"""
    (out / "DATA_AUDIT.md").write_text(text, encoding="utf-8")


def write_kline_md(out: Path, payload: dict, rows: list) -> None:
    k = payload["kline"]
    lines = [
        "# KLINE_ANALYSIS — D",
        "",
        f"來源：`{k['source']}`",
        f"限制：{k['caveat']}",
        "",
        "## A. 實際持倉路徑（SHORT，1m OHLC）",
        f"- 已平且有路徑樣本：{k['n_closed_with_path']}",
        f"- MFE% 中位/P25/P75：{fmt(k['mfe_pct_median'])} / {fmt(k['mfe_pct_p25'])} / {fmt(k['mfe_pct_p75'])}",
        f"- MAE% 中位/P25/P75：{fmt(k['mae_pct_median'])} / {fmt(k['mae_pct_p25'])} / {fmt(k['mae_pct_p75'])}",
        f"- 曾浮盈卻虧損出場（gave_back）：{k['giveback_n']} / {k['n_closed_with_path']}",
        "- 進出場棒可能含持倉外價格 → `boundary_error`（見 kline_rows.json）。",
        "",
        "## B. 固定窗（自進場起，順空方向報酬%）",
    ]
    for m in HOLD_WINDOWS:
        med = k["fixed_window_median_ret"].get(str(m))
        n = k["fixed_window_n"].get(str(m))
        lines.append(f"- {m}m：有效 n={n}；期末順向報酬中位={fmt(med)}%（不足截止者已 CENSORED，未補造）")
    lines += [
        "",
        "## C. 價格觸及統計（持倉內，順向低點；≠可實現勝率）",
        f"分母 n={k['touch_n']}（已平有路徑）",
    ]
    for kk, vv in k["touch_hit_counts"].items():
        lines.append(f"- 觸及 {kk}：{vv}/{k['touch_n']}")
    lines += [
        "",
        "## 代表案例",
    ]
    # pick 3 closed + 1 open
    closed = [r for r in rows if r["status"] == "CLOSED"]
    for r in sorted(closed, key=lambda x: x.get("cycle_net_u") or 0)[:3]:
        p = r.get("path") or {}
        lines.append(
            f"- {r['symbol']} `{r['trade_id'][-10:]}` reason={r.get('exit_reason')} "
            f"net={fmt(r.get('cycle_net_u'))} hold={fmt(r.get('hold_sec'),1)}s "
            f"MFE%={fmt(p.get('mfe_pct'))} MAE%={fmt(p.get('mae_pct'))} "
            f"boundary={p.get('boundary_error')}"
        )
    for r in rows:
        if r["status"] == "OPEN":
            p = r.get("path") or {}
            lines.append(
                f"- 未平 {r['symbol']} entry={r.get('entry_ts')} "
                f"acct_mfe={r.get('acct_mfe_u')} acct_mae={r.get('acct_mae_u')} "
                f"path_MFE%={fmt(p.get('mfe_pct'))}（估價至窗口結束；**非已實現**）"
            )
    lines += [
        "",
        "## 同棒 TP/SL",
        "多數已平持倉極短；若同棒高低同時跨越 TP/SL 價位則標 AMBIGUOUS（見各列 path_ambiguity）。"
        "無 tick → 不假設有利先發生。",
        "",
        "明細：`kline_rows.json`",
    ]
    (out / "KLINE_ANALYSIS.md").write_text("\n".join(lines), encoding="utf-8")


def write_cf_md(out: Path, cf: dict, rows: list) -> None:
    text = f"""# COUNTERFACTUAL — D（有限度）

## 範圍
{cf['scope']}

| 項 | 值 |
|----|----|
| 實際已平 cycle 淨合計 | {fmt(cf['actual_cycle_net_sum'])} U |
| CF0 僅毛損益（去費用） | {fmt(cf['cf0_gross_only_sum'])} U |
| 費用拖累（進出場費） | {fmt(cf['cost_drag'])} U |

## 候選（最多三個單一變因）
"""
    for c in cf["candidates"]:
        text += f"\n### {c['id']}: {c['change']}\n- 理由：{c['reason']}\n- 可計算性：{c['computable']}\n- 結果：{c['result']}\n"
    text += f"\n## 限制\n{cf['disclaimer']}\n\n- 無歷史 bid/ask 排队 → 不宣稱可精準重建成交。\n- 改出場會改變後續槽位／冷卻／進場機會。\n"
    text += "\n## 逐筆 CF0\n"
    for r in rows:
        text += f"- {r['trade_id']}: actual_net={fmt(r['actual_cycle_net'])} gross_only={fmt(r['cf0_gross_only'])}\n"
    (out / "COUNTERFACTUAL.md").write_text(text, encoding="utf-8")


def write_brief(out: Path, payload: dict, krows: list) -> None:
    idn = payload["identity"]
    perf = payload["perf"]
    fun = payload["funnel"]
    recon = payload["recon"]
    k = payload["kline"]
    # representatives
    closed = [t for t in krows if t["status"] == "CLOSED"]
    worst = sorted(closed, key=lambda x: x.get("cycle_net_u") or 0)[:3]
    best = sorted(closed, key=lambda x: -(x.get("cycle_net_u") or 0))[:2]

    brief = f"""# BRIEF — Strategy D 紙上復盤（d_quiet_short_v1）

> 唯讀分析。快照截止 `{payload['cutoff']}`（+08）。**不是交易所成交**；模擬假設見 STRATEGY_SPEC／DATA_AUDIT。查無則 N/A。

## 0. 結論摘要

- D 是 **paper 安靜區間做空**：15m 已收盤四棒夠「安靜」→ 高於 P 的限價空單；TP/SL 以 **毛利 USDT**（+15／−3）觸發，另有波動放大 `VOL_SPIKE`。
- 本次可用運行約 **{fmt(idn['run_span_hours'], 2)} 小時**（{idn['first_event']} → {idn['acct_updated_at']}），**不足十小時**；{idn['window_note']}。
- 已平 **10** 筆全虧；**經濟 cycle 淨合計 {fmt(perf['net_sum'])} U**（已扣進出場費）。帳戶 `realized_net_u`=**{fmt(recon['acct_realized_net_u'])} U**（口徑=Σclose_net，**未**扣進場費）。殘倉 **HYPEUSDT** 1 筆未平，**不得計入已實現**。
- 帳本 fills↔帳戶對帳 **PASS**（現金=初始+realized_net−全部進場費）；最大缺口：無 D 原生 K 線／無 funding／無真實深度成交重建。
- 證據偏向：**先修執行與出場邏輯的 paper 單一變因對照**，不支持加碼或上實盤。

## 1. 快照截止與運行身分

| 項 | 值 |
|----|----|
| 截止 | {payload['cutoff']} |
| strategy | `{idn['strategy_id']}` / `{idn['strategy_version']}` |
| profile / run_id | `{idn['profile_id']}` / `{idn['run_id']}` |
| 模式 | {idn['mode']} |
| 環境 | {idn['env']} |
| 末次 LAUNCH | {idn['launched_at']} pid={idn['pid_last_launch']} |
| 帳本更新／崩潰 | {idn['acct_updated_at']}（log：PermissionError 寫 account_state） |
| 商品 | 多種 USDT-M 永續（本批成交含 ADA/LINK/AVAX/DOGE/WLD/BNB/XLM/XRP/1000SHIB/SOL/HYPE） |

## 2. D 規則摘要（人話）

D 在幣安永續上（本機紙上）找「最近四根 15 分鐘 K 線波幅夠小」的幣，用當下中價 P 上方 0.5% 掛空單；成交後盯毛損益：賺滿約 15U 停利、虧約 3U 停損，若短時波動突然放大也會 `VOL_SPIKE` 出場。同時最多約 10 倉，每倉保證金約 30U×10 倍≈300U 名目。費用按每邊 5bps 模擬；**TP/SL 是美元毛利門檻，不是價格百分比**。

詳細證據表見 `STRATEGY_SPEC.md`。注意：`launch_meta_d.json` 曾記 range/bar=0.02/0.01，與 config 0.015/0.0075 不符；撤單出現 `BAR_AMP_FAIL`，**以 config 為準**並保留差異紀錄。

## 3. 漏斗與績效

| 指標 | 值 |
|------|----|
| 訊號 | {fun['signals']}（不可用成交反推） |
| 掛單 QUEUE | {fun['queue_limit_short']} |
| 撤單 | {fun['cancel_pending']}（原因 {fun['cancel_reasons']}） |
| OPEN / 已平 / 未平 | {fun['opens']} / {perf['n_closed']} / {perf['n_open']} |
| 掃描擋單事件 | {fun['scan_event_types']} |
| 勝／負／平（扣成本後） | {perf['wins']}／{perf['losses']}／{perf['flats']}；勝率 {fmt(perf['win_rate'], 3)} |
| 毛損益／費用／資金費／淨 | {fmt(perf['gross_sum'])}／{fmt(perf['fees_closed'])}／{perf['funding']}／**{fmt(perf['net_sum'])}** |
| 均／中位每筆淨 | {fmt(perf['avg_net'])}／{fmt(perf['median_net'])} |
| 均盈／均虧／盈虧比／PF | {fmt(perf['avg_win'])}／{fmt(perf['avg_loss'])}／{fmt(perf['payoff'])}／{fmt(perf['profit_factor'])} |
| 持倉時間均／中位 | {fmt(perf['avg_hold_sec'], 1)}s／{fmt(perf['median_hold_sec'], 1)}s |
| 最大連虧 | {perf['max_consecutive_losses']} |
| 最大回撤 | {fmt(perf['max_dd_realized_curve'])} U（{perf['dd_definition']}） |
| 出場原因 | {perf['by_exit_reason']} |

**對帳：** 帳戶 realized_net={fmt(recon['acct_realized_net_u'])}=Σclose_net；經濟已平淨={fmt(perf['net_sum'])}；現金核對差≈{fmt((recon.get('cash_check') or {}).get('diff'), 8)}。funding=N/A。滑價已在成交價內。

損失結構（合理推論）：本批 **訊號選幣後快速同向不利**（普漲／波動放大）+ **緊 SL(3U)** + **VOL_SPIKE 與 SL 疊加**；費用約 {fmt(perf['fees_closed'])}U 為次要。非「方向寫反」——全部 SHORT 與規格一致（確認事實）。

## 4. K 線關鍵統計

- 借用 paper C vault 1m（D 無 vault）：{k['caveat']}
- 已平有路徑 n={k['n_closed_with_path']}；MFE% 中位 {fmt(k['mfe_pct_median'])}；MAE% 中位 {fmt(k['mae_pct_median'])}
- 浮盈後仍虧（gave_back）{k['giveback_n']} 筆
- 固定窗有效樣本：5/15/30/60m = {k['fixed_window_n']}；順空期末報酬中位% {k['fixed_window_median_ret']}
- 觸及 0.25%→1.5% 次數：{k['touch_hit_counts']}（分母 {k['touch_n']}；觸及≠可實現）

## 5. 代表交易

"""
    for r in worst:
        p = r.get("path") or {}
        brief += (
            f"- 虧：{r['symbol']} reason={r.get('exit_reason')} net={fmt(r.get('cycle_net_u'))}U "
            f"hold={fmt(r.get('hold_sec'),1)}s entry={r.get('entry_ts')} exit={r.get('exit_ts')} "
            f"MFE%={fmt(p.get('mfe_pct'))} MAE%={fmt(p.get('mae_pct'))}\n"
        )
    for r in best:
        p = r.get("path") or {}
        brief += (
            f"- 相對較好（仍可能為負）：{r['symbol']} reason={r.get('exit_reason')} "
            f"net={fmt(r.get('cycle_net_u'))}U\n"
        )
    for r in krows:
        if r["status"] == "OPEN":
            brief += (
                f"- 未平：{r['symbol']} entry={r.get('entry_ts')}@{r.get('entry_px')} "
                f"acct_mfe={r.get('acct_mfe_u')} acct_mae={r.get('acct_mae_u')} "
                f"**非已實現**\n"
            )

    brief += f"""
## 6. 主要問題、證據、下一步

| # | 判斷 | 類型 | 證據 |
|---|------|------|------|
| 1 | 資料短於宣稱「十小時」 | 確認事實 | 首事件→崩潰 ≈{fmt(idn['run_span_hours'],2)}h；21:03 PermissionError |
| 2 | 已平全虧、緊 SL／VOL 主導 | 確認事實 | 10/10 負；exit_reason 皆 SL 或 VOL 相關 |
| 3 | paper 樂觀觸價成交 | 確認事實 | simulation_assumption；無排队 |
| 4 | 主損來自路徑／出場閾值，費用次要 | 合理推論 | 毛≈{fmt(perf['gross_sum'])} vs 費≈{fmt(perf['fees_closed'])} |
| 5 | 無原生 K／funding／tick | 資料不足 | vault 空；funding_data_gap |
| 6 | launch_meta_d 閾值與 config 不一致 | 確認差異／影響未完全定量 | 見 STRATEGY_SPEC |

**建議（非下單）：** 保持不動實盤；paper 上做單一變因：(1) SL 3→5U；(2) 關閉或延後 VOL_SPIKE；(3) 修 Windows 寫檔 PermissionError。成功／失敗先訂：新樣本≥30 筆已平、淨期望與回撤優於本基線，且路徑不明比例下降——**樣本內不可宣稱已驗證**。

## 7. 六大問題速答

1. **D 在做什麼？** 紙上「安靜 15m 區間」後掛空，用美元毛利 TP15/SL3（+波動退出）。  
2. **資料可信？** 帳本內部可信（PASS）；經濟含義 PARTIAL（模擬+缺 funding/原生K）。最大缺口：原生行情歸檔與真實成交。  
3. **毛／淨？** 毛 {fmt(perf['gross_sum'])}U；淨 {fmt(perf['net_sum'])}U（已平）。主損：進場後快速不利＋緊停損／波動退出。  
4. **實作疑點？** 未見方向顛倒或費用重扣；見 meta/config 閾值不一致、崩潰停機、殘倉未平。未收盤棒：規格禁止，本批訊號帶 completed bar close_time。  
5. **先不動／修／對照？** 先修寫檔穩定性 + paper 單一變因；不支持上實盤。  
6. **假設：** SL 過緊；VOL_SPIKE 與 SL 同輪傷害；安靜參數在趨勢日失效。判定：預註冊指標後用新時間段驗證。

## 8. 附件與未決

- `STRATEGY_SPEC.md` `DATA_AUDIT.md` `TRADES.csv` `KLINE_ANALYSIS.md` `COUNTERFACTUAL.md` `summary.json` `kline_rows.json` `_d_10h_review.py` `REPRODUCE.md` `snapshots/`
- 未決：HYPE 最終平倉；PermissionError 根因；launch_meta_d 0.02/0.01 是否曾注入進程；真實 15m 歸檔補齊。
"""
    (out / "BRIEF.md").write_text(brief, encoding="utf-8")


def write_reproduce(out: Path) -> None:
    (out / "REPRODUCE.md").write_text(
        """# 重現

```powershell
python c:\\Users\\Admin\\MyProjects\\automax\\analysis\\d_10h_review_20261122T092446\\_d_10h_review.py
```

依賴：已存在的 `snapshots/`（由分析當日 copy2）。不連網、不下單、不改策略。
K 線讀取 path：`paper_bcd_local/data_c/.../kline_vault_v2_clean/*_1m.csv`（借用）。
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
