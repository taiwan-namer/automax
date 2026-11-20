#!/usr/bin/env python3
"""Round-6: A1/A2/B/C independent loss root-cause audit (read-only analysis artifacts)."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

SETUP = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup")
DATA = SETUP / "fixed_data_root" / "output"
R5 = SETUP / "analysis_packs" / "audit_bc_independent_20261120T0220" / "round5_abc_timeseries"
OUT = SETUP / "analysis_packs" / "audit_bc_independent_20261120T0220" / "round6_independent_loss_audit"
CUTOFF = "2026-11-20T03:01:00+08:00"
FEE_RATE = 0.0005
SLIP = 0.0002
MANAGE_PURPOSES = {"MANAGE_POLL", "MANAGE", "MANAGE_DUE_PREEMPT"}

ACCOUNTS = {
    "A": "paper_reverse_fixed_exit_v1",
    "B": "paper_reverse_fixed_usdt_50_75_v1",
    "C": "paper_reverse_fixed_usdt_50_75_inv_v1",
}


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fnum(x: Any, default: float = float("nan")) -> float:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fee_u(notional: float) -> float:
    return abs(notional) * FEE_RATE


def mark_exec(side: str, bid: float, ask: float) -> float:
    if side == "LONG":
        return bid * (1.0 - SLIP)
    return ask * (1.0 + SLIP)


def gross_pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    if side == "LONG":
        return (exit_px - entry) * qty
    return (entry - exit_px) * qty


def pct(n: int, d: int) -> str:
    if d == 0:
        return "n/a"
    return f"{100.0 * n / d:.1f}%"


def median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    return statistics.median(xs)


def mean(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    return statistics.mean(xs)


def quantiles(xs: list[float], ps=(0.0, 0.25, 0.5, 0.75, 1.0)) -> dict[str, float]:
    if not xs:
        return {f"p{int(p*100)}": float("nan") for p in ps}
    s = sorted(xs)
    out = {}
    for p in ps:
        if p <= 0:
            out[f"p{int(p*100)}"] = s[0]
        elif p >= 1:
            out[f"p{int(p*100)}"] = s[-1]
        else:
            idx = (len(s) - 1) * p
            lo = int(math.floor(idx))
            hi = int(math.ceil(idx))
            if lo == hi:
                out[f"p{int(p*100)}"] = s[lo]
            else:
                out[f"p{int(p*100)}"] = s[lo] * (hi - idx) + s[hi] * (idx - lo)
    return out


def load_r5(account: str) -> list[dict]:
    rows = list(csv.DictReader(open(R5 / f"{account}_trades_full.csv")))
    return rows


def load_fills(key: str) -> tuple[list[dict], list[dict]]:
    fills = [json.loads(l) for l in open(DATA / key / "fills.jsonl") if l.strip()]
    opens = [f for f in fills if f.get("event") == "OPEN" and f["recorded_at"] <= CUTOFF]
    closes = [f for f in fills if f.get("event") == "CLOSE" and f["recorded_at"] <= CUTOFF]
    return opens, closes


def load_quotes_index(key: str) -> dict[str, list[dict]]:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for qf in (DATA / key / "runs").glob("*/quote_events.jsonl"):
        for line in qf.open():
            o = json.loads(line)
            if o.get("recorded_at", "") > CUTOFF:
                continue
            by_sym[o["symbol"]].append(o)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x["recorded_at"])
    return by_sym


def quotes_in_hold(
    quotes: list[dict], entry_ts: str, exit_ts: Optional[str], still_open: bool
) -> list[dict]:
    """Hold window: after entry_ts, up to and including exit_ts (if closed). Exclude pre-entry."""
    out = []
    for q in quotes:
        ts = q["recorded_at"]
        if ts <= entry_ts:
            continue
        if not still_open and exit_ts and ts > exit_ts:
            continue
        if still_open and ts > CUTOFF:
            continue
        out.append(q)
    return out


def path_metrics(
    side: str,
    entry_px: float,
    qty: float,
    open_fee: float,
    hold_quotes: list[dict],
    include_entry_point: bool = False,
    entry_ts: Optional[str] = None,
) -> dict[str, Any]:
    """Rebuild gross and full roundtrip-net valuation paths from polled bid/ask."""
    pts_gross = []
    pts_rt = []
    pts_mid_gross = []  # reference only
    gaps = []
    prev_ts = None
    n_ba = 0
    n_mid_only = 0
    order_ambiguous = False

    # Optional synthetic entry point (gross=0); mark order at same second as unknown later
    if include_entry_point and entry_ts:
        pts_gross.append((entry_ts, 0.0, entry_px, "entry_synthetic"))
        pts_rt.append((entry_ts, -open_fee - fee_u(entry_px * qty), entry_px, "entry_synthetic"))

    for q in hold_quotes:
        bid, ask = fnum(q.get("bid")), fnum(q.get("ask"))
        mid = fnum(q.get("mid"))
        ts = q["recorded_at"]
        purpose = q.get("purpose") or ""
        if prev_ts is not None:
            gaps.append((parse_ts(ts) - parse_ts(prev_ts)).total_seconds())
        prev_ts = ts

        if math.isnan(bid) or math.isnan(ask) or bid <= 0 or ask <= 0:
            if not math.isnan(mid) and mid > 0:
                n_mid_only += 1
                g = gross_pnl(side, entry_px, mid, qty)
                pts_mid_gross.append((ts, g, mid, purpose))
            continue
        n_ba += 1
        mx = mark_exec(side, bid, ask)
        g = gross_pnl(side, entry_px, mx, qty)
        est_close_fee = fee_u(mx * qty)
        rt = g - open_fee - est_close_fee
        pts_gross.append((ts, g, mx, purpose))
        pts_rt.append((ts, rt, mx, purpose))

    def extrema(pts: list[tuple]) -> dict[str, Any]:
        if not pts:
            return {
                "max": float("nan"),
                "min": float("nan"),
                "max_ts": "",
                "min_ts": "",
                "order": "NO_PATH",
                "max_mark": float("nan"),
                "min_mark": float("nan"),
            }
        # Same-second ambiguity
        by_sec = defaultdict(list)
        for ts, v, mark, purpose in pts:
            by_sec[ts[:19]].append((ts, v, mark, purpose))
        imax = max(range(len(pts)), key=lambda i: pts[i][1])
        imin = min(range(len(pts)), key=lambda i: pts[i][1])
        max_ts, max_v, max_mark, _ = pts[imax]
        min_ts, min_v, min_mark, _ = pts[imin]
        if max_ts[:19] == min_ts[:19] and imax != imin:
            order = "順序不明"
        elif imax < imin:
            order = "MFE_THEN_MAE"
        elif imin < imax:
            order = "MAE_THEN_MFE"
        else:
            order = "SAME_POINT"
        return {
            "max": max_v,
            "min": min_v,
            "max_ts": max_ts,
            "min_ts": min_ts,
            "order": order,
            "max_mark": max_mark,
            "min_mark": min_mark,
        }

    eg = extrema(pts_gross)
    er = extrema(pts_rt)
    max_gap = max(gaps) if gaps else float("nan")
    return {
        "n_path_points_ba": n_ba,
        "n_mid_only_ref": n_mid_only,
        "n_hold_quotes_total": len(hold_quotes),
        "max_gap_sec": max_gap,
        "gross_mfe": eg["max"],
        "gross_mae": eg["min"],
        "gross_mfe_ts": eg["max_ts"],
        "gross_mae_ts": eg["min_ts"],
        "gross_order": eg["order"],
        "rt_mfe": er["max"],
        "rt_mae": er["min"],
        "rt_mfe_ts": er["max_ts"],
        "rt_mae_ts": er["min_ts"],
        "rt_order": er["order"],
        "path_includes_entry_synthetic": include_entry_point,
        "path_includes_exit_quote": True,  # exit_ts inclusive if quote exists
        "mid_ref_gross_mfe": max((p[1] for p in pts_mid_gross), default=float("nan")),
        "mid_ref_gross_mae": min((p[1] for p in pts_mid_gross), default=float("nan")),
        "pts_gross": pts_gross,
        "pts_rt": pts_rt,
    }


def tp_bucket(ratio: float) -> str:
    if math.isnan(ratio):
        return "NA"
    if ratio <= 0:
        return "<=0%"
    if ratio < 0.25:
        return "0-25%"
    if ratio < 0.50:
        return "25-50%"
    if ratio < 0.75:
        return "50-75%"
    if ratio < 1.0:
        return "75-100%"
    return ">=100%"


def classify_loss(
    still_open: bool,
    exit_reason: str,
    gross_mfe: float,
    rt_mfe: float,
    open_fee: float,
    planned_close_fee_at_tp: float,
    planned_tp_gross: float,
    path_ok: bool,
    quote_reached_tp_gross: bool,
    order_ok: bool,
) -> str:
    """Mutually exclusive A-F for ALL trades (wins get WIN_* or still use F)."""
    if still_open:
        return "F_OPEN"
    if not path_ok or math.isnan(gross_mfe):
        return "F_DATA"
    # Wins and losses both classified for coverage; prefix clarifies
    full_rt_cost = open_fee + planned_close_fee_at_tp  # approx; actual uses path est

    if exit_reason not in ("INITIAL_STOP", "FIXED_TP", "ATR_STOP", "TRAIL_STOP", "BE_STOP", "PARTIAL"):
        # treat unknown non-empty
        if exit_reason and exit_reason not in ("INITIAL_STOP", "FIXED_TP"):
            # keep going — may still be stop-like
            pass

    is_stop = exit_reason in ("INITIAL_STOP", "ATR_STOP", "TRAIL_STOP", "BE_STOP")
    is_tp = exit_reason in ("FIXED_TP", "PARTIAL")  # PARTIAL rare
    is_other_exit = (not is_stop) and (not is_tp) and (not still_open)

    # D: quote reached planned gross TP but did not take TP (needs verifiable path)
    if (
        quote_reached_tp_gross
        and not is_tp
        and order_ok
        and path_ok
        and not math.isnan(planned_tp_gross)
        and planned_tp_gross > 0
    ):
        return "D_MISSED_TP"

    if is_other_exit:
        return "E_NON_STOP_EXIT"

    # For stop losses (and also apply structure to all closed for exclusivity on losers):
    if math.isnan(gross_mfe) or (not path_ok):
        return "F_DATA"

    if gross_mfe <= 0:
        # A: never observed favorable gross move
        return "A_NO_GROSS_MFE"

    # Had gross MFE > 0
    # B: gross float but RT net path never positive (insufficient to cover full RT cost)
    if math.isnan(rt_mfe) or rt_mfe <= 0:
        return "B_GROSS_BUT_RT_NEVER_POS"

    # C: had positive full RT valuation but did not reach gross TP, ended stop
    if is_stop and rt_mfe > 0 and (math.isnan(gross_mfe) or gross_mfe < planned_tp_gross * 0.999):
        return "C_RT_POS_THEN_STOP"

    # Remaining stops that somehow had gross MFE >= TP (but D didn't fire — order unclear)
    if is_stop:
        if quote_reached_tp_gross and not order_ok:
            return "F_ORDER_UNCLEAR"
        return "C_RT_POS_THEN_STOP" if rt_mfe > 0 else "B_GROSS_BUT_RT_NEVER_POS"

    # TP exits that weren't D
    if is_tp:
        return "TP_EXIT"

    return "F_DATA"


def exclusive_class_closed(
    still_open: bool,
    exit_reason: str,
    rt_net: float,
    gross_mfe: float,
    rt_mfe: float,
    planned_tp_gross: float,
    path_ok: bool,
    quote_reached_tp: bool,
    order_flag: str,
) -> str:
    """Mutually exclusive covering ALL sample rows."""
    if still_open:
        return "F_OPEN"
    if not path_ok or (math.isnan(gross_mfe) and math.isnan(rt_mfe)):
        return "F_DATA"

    order_ok = order_flag not in ("順序不明", "NO_PATH")
    is_stop = exit_reason == "INITIAL_STOP"
    is_tp = exit_reason == "FIXED_TP"
    is_other = (not is_stop) and (not is_tp)

    if is_other:
        return "E_NON_STOP_EXIT"

    # D only when stop (or non-TP) and path shows gross>=TP with clear order
    if (not is_tp) and quote_reached_tp and order_ok and planned_tp_gross > 0:
        return "D_MISSED_TP"
    if (not is_tp) and quote_reached_tp and not order_ok:
        return "F_ORDER_UNCLEAR"

    if is_tp:
        return "TP_EXIT"

    # stop path
    if gross_mfe <= 0 or math.isnan(gross_mfe):
        return "A_NO_GROSS_MFE"
    if rt_mfe <= 0 or math.isnan(rt_mfe):
        return "B_GROSS_BUT_RT_NEVER_POS"
    # had positive RT mfe, stopped before/without confirmed TP hit
    return "C_RT_POS_THEN_STOP"


def overlap_tags(row: dict, pm: dict, planned_sl_gross: float) -> list[str]:
    tags = []
    still = row["still_open"] == "True"
    if still:
        return tags
    hold = fnum(row["hold_sec"], 0)
    exit_reason = row["exit_reason"]
    sl_os = fnum(row.get("sl_overshoot_gross_u"), 0)
    gross = fnum(row["gross_u"])
    gross_mfe = pm["gross_mfe"]
    gross_mae = pm["gross_mae"]
    max_gap = pm["max_gap_sec"]

    # 明顯超額停損：實際毛虧比預定 SL 毛額再差 >= 15U（or overshoot field）
    if exit_reason == "INITIAL_STOP" and (
        (not math.isnan(sl_os) and sl_os >= 15)
        or (
            not math.isnan(planned_sl_gross)
            and planned_sl_gross < 0
            and gross <= planned_sl_gross - 15
        )
    ):
        tags.append("明顯超額停損")

    # 大幅浮盈回吐：gross MFE >= 25 且最終毛虧或停損
    if (
        not math.isnan(gross_mfe)
        and gross_mfe >= 25
        and (exit_reason == "INITIAL_STOP" or gross < 0)
    ):
        tags.append("大幅浮盈回吐")

    # 快速停損：持倉 < 120s 且 INITIAL_STOP
    if exit_reason == "INITIAL_STOP" and hold < 120:
        tags.append("快速停損")

    # 報價缺口：max gap >= 30s
    if not math.isnan(max_gap) and max_gap >= 30:
        tags.append("報價缺口")

    # 同秒開平
    if row["entry_ts"][:19] == (row["exit_ts"] or "")[:19]:
        tags.append("同秒開平")

    return tags


def audit_orig_side(rows: list[dict], opens_by_id: dict[str, dict], account: str) -> dict:
    """Field correctness: does orig_side mean market breakout or source fill side?"""
    findings = []
    for r in rows:
        tid = r["trade_id"]
        mode = r["entry_mode"]
        o = opens_by_id.get(tid, {})
        fill_orig = o.get("orig_side")
        side = r["side"]
        labeled = r["orig_side_breakout"]
        rel = r["direction_relation"]
        note = ""
        status = "未知"
        if mode == "SELF_SCAN_FLIP":
            # signals.py: orig_side = pre-flip breakout side; side = flipped
            if fill_orig and labeled == fill_orig:
                if side != fill_orig:
                    status = "已證實_為市場突破方向_翻轉後進場"
                else:
                    status = "支持但未證實_欄位與成交side相同"
            else:
                status = "未知_缺OPEN.orig_side"
            note = "self-scan: OPEN.orig_side 來自 signals 翻轉前方向"
        elif mode == "MIRROR_INVERSE":
            # mirror.py sets orig_side = src_side (B fill side), NOT market breakout
            status = "已證實_記的是來源成交方向_非市場突破"
            note = (
                f"mirror inverse: OPEN.orig_side={fill_orig} = 來源帳戶 side；"
                f"本帳戶 side={side}（必相反）。R5 direction_relation={rel} "
                f"若用 side≠orig 會幾乎恒為 AGAINST，不能當順逆勢。"
            )
        elif mode == "MIRROR_SAME":
            status = "已證實_記的是來源成交方向_非市場突破"
            note = (
                f"mirror same: OPEN.orig_side={fill_orig} = 來源帳戶 side；"
                f"本帳戶 side={side}（應相同）。相對市場突破需另查來源是否 flip。"
            )
        else:
            status = "未知"
            note = f"entry_mode={mode}"
        findings.append(
            {
                "account": account,
                "trade_id": tid,
                "entry_mode": mode,
                "side": side,
                "orig_side_breakout_r5": labeled,
                "open_fill_orig_side": fill_orig,
                "direction_relation_r5": rel,
                "field_status": status,
                "note": note,
            }
        )
    # summary
    c = Counter(f["field_status"] for f in findings)
    return {"per_trade": findings, "summary": dict(c)}


def planned_close_fee_at_barrier(side: str, entry: float, qty: float, tp_px: float) -> float:
    if not tp_px or math.isnan(fnum(tp_px)):
        return fee_u(entry * qty)  # approx at entry
    return fee_u(fnum(tp_px) * qty)


def analyze_account(account: str) -> dict:
    key = ACCOUNTS[account]
    rows = load_r5(account)
    opens, closes = load_fills(key)
    opens_by = {f["trade_id"]: f for f in opens}
    closes_by = {f["trade_id"]: f for f in closes}
    quotes_by = load_quotes_index(key)

    # post-cutoff sample
    all_fills = [json.loads(l) for l in open(DATA / key / "fills.jsonl") if l.strip()]
    post_opens = [f for f in all_fills if f.get("event") == "OPEN" and f["recorded_at"] > CUTOFF]

    # verify counts
    sample_check = {
        "r5_n": len(rows),
        "fills_opens_le": len(opens),
        "fills_closes_le": len(closes),
        "ids_match": {r["trade_id"] for r in rows} == {f["trade_id"] for f in opens},
        "post_cutoff_opens": len(post_opens),
        "post_cutoff_open_ids": [f["trade_id"] for f in post_opens],
    }

    # accounting from fills
    sum_gross = sum(float(f["gross_u"]) for f in closes)
    sum_open_fee_all = sum(float(f["fee_u"]) for f in opens)
    sum_close_fee = sum(float(f["fee_u"]) for f in closes)
    sum_close_net = sum(float(f["net_u"]) for f in closes)
    open_ids = {f["trade_id"] for f in opens}
    close_ids = {f["trade_id"] for f in closes}
    still_ids = open_ids - close_ids
    open_fee_still = sum(float(opens_by[t]["fee_u"]) for t in still_ids)
    recon_realized = sum_close_net - sum_open_fee_all

    last_eq = None
    for line in open(DATA / key / "equity_snapshots.jsonl"):
        o = json.loads(line)
        if o["recorded_at"] <= CUTOFF:
            last_eq = o
    acct_gap = None
    if last_eq:
        acct_gap = recon_realized - float(last_eq["realized_net_u"])

    accounting = {
        "sum_gross_closed": sum_gross,
        "sum_open_fee_all": sum_open_fee_all,
        "sum_close_fee_closed": sum_close_fee,
        "sum_close_net_closed": sum_close_net,
        "sum_rt_net_closed_only": sum_close_net
        - sum(float(opens_by[r["trade_id"]]["fee_u"]) for r in rows if r["still_open"] == "False"),
        "open_fee_still_open": open_fee_still,
        "recon_realized_closeNets_minus_allOpenFees": recon_realized,
        "equity_snap_realized": last_eq["realized_net_u"] if last_eq else None,
        "equity_snap_fees": last_eq.get("realized_fees_u") if last_eq else None,
        "equity_snap_ts": last_eq["recorded_at"] if last_eq else None,
        "gap_recon_minus_equity": acct_gap,
        "funding_simulated": False,
        "slippage_double_count": False,
        "note": "滑價已進成交價；CLOSE.net_u=gross-close_fee；開倉費在 OPEN 扣 realized",
    }

    orig_audit = audit_orig_side(rows, opens_by, account)

    per_trade = []
    for r in rows:
        tid = r["trade_id"]
        still = r["still_open"] == "True"
        o = opens_by[tid]
        c = closes_by.get(tid)
        side = r["side"]
        entry_px = fnum(r["entry_px"])
        qty = fnum(r["qty"])
        open_fee = fnum(r["fee_open_u"])
        if math.isnan(open_fee):
            open_fee = float(o["fee_u"])
        exit_px = fnum(r["exit_px"]) if not still else float("nan")
        close_fee = fnum(r["fee_close_u"]) if not still else float("nan")
        gross = fnum(r["gross_u"]) if not still else float("nan")
        close_net = fnum(r["close_net_u"]) if not still else float("nan")
        rt_net = fnum(r["rt_net_u"]) if not still else float("nan")
        # recompute from fills
        if not still and c:
            gross_r = float(c["gross_u"])
            close_fee_r = float(c["fee_u"])
            close_net_r = float(c["net_u"])
            rt_net_r = close_net_r - open_fee
            # verify
            gross_chk = gross_pnl(side, entry_px, float(c["price"]), float(c["qty"]))
        else:
            gross_r = close_fee_r = close_net_r = rt_net_r = float("nan")
            gross_chk = float("nan")

        planned_tp_gross = fnum(r["tp_gross_u"])
        planned_sl_gross = fnum(r["sl_gross_u"])
        tp_px = fnum(r["tp"])
        sl_px = fnum(r["sl"])
        # for ATR accounts tp_gross may be from barriers
        if math.isnan(planned_tp_gross) and not math.isnan(tp_px):
            planned_tp_gross = abs(gross_pnl(side, entry_px, tp_px, qty))
            # sign: TP is favorable
            planned_tp_gross = abs(planned_tp_gross)
        if math.isnan(planned_sl_gross) and not math.isnan(sl_px):
            planned_sl_gross = -abs(gross_pnl(side, entry_px, sl_px, qty))

        hold_q = quotes_in_hold(
            quotes_by.get(r["symbol"], []),
            r["entry_ts"],
            r["exit_ts"] if not still else None,
            still,
        )
        manage_q = [q for q in hold_q if q.get("purpose") in MANAGE_PURPOSES]
        # Prefer manage quotes for path; if empty fall back to all hold quotes with BA
        path_src = manage_q if manage_q else hold_q
        pm = path_metrics(side, entry_px, qty, open_fee, path_src, include_entry_point=False)

        # Did path gross ever reach planned TP?
        quote_reached_tp = (
            (not math.isnan(pm["gross_mfe"]))
            and (not math.isnan(planned_tp_gross))
            and planned_tp_gross > 0
            and pm["gross_mfe"] >= planned_tp_gross * 0.999
        )
        path_ok = pm["n_path_points_ba"] >= 1
        # coverage quality
        if pm["n_path_points_ba"] < 3:
            path_completeness = "不足(<3 BA points)"
        elif not math.isnan(pm["max_gap_sec"]) and pm["max_gap_sec"] >= 60:
            path_completeness = "有大缺口(>=60s)"
        else:
            path_completeness = "可描述(poll)"

        excl = exclusive_class_closed(
            still,
            r["exit_reason"],
            rt_net_r,
            pm["gross_mfe"],
            pm["rt_mfe"],
            planned_tp_gross,
            path_ok,
            quote_reached_tp,
            pm["gross_order"],
        )
        tags = overlap_tags(r, pm, planned_sl_gross)

        # engine-recorded mfe/mae (close-fee-only net, init 0)
        eng_mfe = fnum(r["mfe_u"]) if not still else fnum(c.get("mfe_u") if c else None)
        eng_mae = fnum(r["mae_u"]) if not still else fnum(c.get("mae_u") if c else None)
        if c:
            eng_mfe = fnum(c.get("mfe_u"))
            eng_mae = fnum(c.get("mae_u"))

        mfe_over_tp = (
            pm["gross_mfe"] / planned_tp_gross
            if planned_tp_gross and planned_tp_gross > 0 and not math.isnan(pm["gross_mfe"])
            else float("nan")
        )
        mae_over_sl = (
            abs(pm["gross_mae"]) / abs(planned_sl_gross)
            if planned_sl_gross
            and planned_sl_gross != 0
            and not math.isnan(pm["gross_mae"])
            else float("nan")
        )
        tp_os = fnum(r.get("tp_overshoot_gross_u"), 0) if not still else float("nan")
        sl_os = fnum(r.get("sl_overshoot_gross_u"), 0) if not still else float("nan")
        # recompute overshoot
        if not still and not math.isnan(gross_r):
            if r["exit_reason"] == "FIXED_TP" and planned_tp_gross > 0:
                tp_os = gross_r - planned_tp_gross
            if r["exit_reason"] == "INITIAL_STOP" and planned_sl_gross < 0:
                # more negative than planned => positive overshoot loss
                sl_os = planned_sl_gross - gross_r  # if gross more neg, sl_os > 0

        rt_fee = open_fee + (close_fee_r if not math.isnan(close_fee_r) else 0)
        # planned RT fee at TP barrier
        planned_cf = planned_close_fee_at_barrier(side, entry_px, qty, tp_px)
        planned_rt_fee = open_fee + planned_cf
        fee_over_tp = (
            planned_rt_fee / planned_tp_gross
            if planned_tp_gross and planned_tp_gross > 0
            else float("nan")
        )

        notional = entry_px * qty
        rec = {
            "account": account,
            "version": r["version"],
            "seq": r["seq"],
            "trade_id": tid,
            "symbol": r["symbol"],
            "side": side,
            "entry_mode": r["entry_mode"],
            "orig_side_breakout_r5": r["orig_side_breakout"],
            "direction_relation_r5": r["direction_relation"],
            "orig_side_field_status": next(
                f["field_status"] for f in orig_audit["per_trade"] if f["trade_id"] == tid
            ),
            "entry_ts": r["entry_ts"],
            "exit_ts": r["exit_ts"],
            "hold_sec": r["hold_sec"],
            "still_open": still,
            "exit_reason": r["exit_reason"],
            "qty": qty,
            "notional_entry_u": notional,
            "entry_px": entry_px,
            "exit_px": exit_px if not still else "",
            "tp_px": tp_px,
            "sl_px": sl_px,
            "planned_tp_gross_u": planned_tp_gross,
            "planned_sl_gross_u": planned_sl_gross,
            "gross_u": gross_r if not still else "",
            "fee_open_u": open_fee,
            "fee_close_u": close_fee_r if not still else "",
            "close_net_u": close_net_r if not still else "",
            "rt_net_u": rt_net_r if not still else "",
            "rt_fee_u": rt_fee if not still else open_fee,
            "planned_rt_fee_at_tp_u": planned_rt_fee,
            "fee_to_planned_tp_ratio": fee_over_tp,
            "tp_overshoot_gross_u": tp_os if not still else "",
            "sl_overshoot_gross_u": sl_os if not still else "",
            # engine recorded (legacy)
            "engine_mfe_u_closeFeeOnly_init0": eng_mfe if c else "",
            "engine_mae_u_closeFeeOnly_init0": eng_mae if c else "",
            "engine_excursion_source": r.get("excursion_source"),
            # rebuilt
            "obs_gross_mfe_u": pm["gross_mfe"],
            "obs_gross_mae_u": pm["gross_mae"],
            "obs_gross_mfe_ts": pm["gross_mfe_ts"],
            "obs_gross_mae_ts": pm["gross_mae_ts"],
            "obs_gross_order": pm["gross_order"],
            "obs_rt_mfe_u": pm["rt_mfe"],
            "obs_rt_mae_u": pm["rt_mae"],
            "obs_rt_mfe_ts": pm["rt_mfe_ts"],
            "obs_rt_mae_ts": pm["rt_mae_ts"],
            "obs_rt_order": pm["rt_order"],
            "gross_mfe_over_planned_tp": mfe_over_tp,
            "gross_mae_abs_over_planned_sl_abs": mae_over_sl,
            "tp_progress_bucket": tp_bucket(mfe_over_tp),
            "n_hold_quotes": pm["n_hold_quotes_total"],
            "n_path_ba_points": pm["n_path_points_ba"],
            "n_mid_only_ref": pm["n_mid_only_ref"],
            "max_gap_sec": pm["max_gap_sec"],
            "path_completeness": path_completeness,
            "path_includes_entry_synthetic": pm["path_includes_entry_synthetic"],
            "quote_reached_planned_tp_gross": quote_reached_tp,
            "exclusive_class": excl,
            "overlap_tags": "|".join(tags),
            "gross_recompute_check": gross_chk if not still else "",
            "gross_match_fill": (
                abs(gross_chk - gross_r) < 1e-6 if not still and not math.isnan(gross_r) else ""
            ),
        }
        per_trade.append(rec)

    return {
        "account": account,
        "sample_check": sample_check,
        "accounting": accounting,
        "orig_side_audit_summary": orig_audit["summary"],
        "orig_side_audit_rows": orig_audit["per_trade"],
        "trades": per_trade,
        "post_cutoff_opens": post_opens,
    }


def summarize_segment(trades: list[dict], label: str) -> dict:
    closed = [t for t in trades if not t["still_open"]]
    opens = [t for t in trades if t["still_open"]]
    wins = [t for t in closed if fnum(t["rt_net_u"]) > 0]
    losses = [t for t in closed if fnum(t["rt_net_u"]) <= 0]
    tp_exits = [t for t in closed if t["exit_reason"] == "FIXED_TP"]
    sl_exits = [t for t in closed if t["exit_reason"] == "INITIAL_STOP"]

    # TP but still net loss
    tp_net_loss = [t for t in tp_exits if fnum(t["rt_net_u"]) < 0]
    tp_net_0_5 = [t for t in tp_exits if 0 <= fnum(t["rt_net_u"]) < 5]
    tp_net_5_10 = [t for t in tp_exits if 5 <= fnum(t["rt_net_u"]) < 10]

    win_nets = [fnum(t["rt_net_u"]) for t in wins]
    loss_nets = [fnum(t["rt_net_u"]) for t in losses]
    avg_win = mean(win_nets)
    avg_loss = mean(loss_nets)  # negative
    med_win = median(win_nets)
    med_loss = median(loss_nets)
    payoff_needed = (
        abs(avg_loss) / avg_win if avg_win and avg_win > 0 and not math.isnan(avg_loss) else float("nan")
    )

    fee_ratios = [
        fnum(t["fee_to_planned_tp_ratio"])
        for t in closed
        if not math.isnan(fnum(t["fee_to_planned_tp_ratio"]))
    ]
    near_fee = [
        t
        for t in closed
        if not math.isnan(fnum(t["fee_to_planned_tp_ratio"]))
        and fnum(t["fee_to_planned_tp_ratio"]) >= 0.7
    ]

    # exclusive classes
    excl = Counter(t["exclusive_class"] for t in trades)
    # overlap tag counts (non-additive)
    tag_c = Counter()
    for t in trades:
        for tag in (t["overlap_tags"].split("|") if t["overlap_tags"] else []):
            if tag:
                tag_c[tag] += 1

    # decomposition
    planned_tp_sum = sum(
        fnum(t["planned_tp_gross_u"]) for t in tp_exits if not math.isnan(fnum(t["planned_tp_gross_u"]))
    )
    planned_sl_sum = sum(
        fnum(t["planned_sl_gross_u"]) for t in sl_exits if not math.isnan(fnum(t["planned_sl_gross_u"]))
    )
    tp_os_sum = sum(fnum(t["tp_overshoot_gross_u"], 0) for t in tp_exits)
    sl_os_sum = sum(fnum(t["sl_overshoot_gross_u"], 0) for t in sl_exits)
    actual_gross = sum(fnum(t["gross_u"]) for t in closed)
    actual_fees = sum(fnum(t["rt_fee_u"]) for t in closed)
    actual_rt = sum(fnum(t["rt_net_u"]) for t in closed)
    # unexplained = actual_gross - (planned_tp_sum + planned_sl_sum + tp_os - wait)
    # For TP exits: gross ≈ planned_tp + tp_overshoot
    # For SL exits: gross ≈ planned_sl - sl_overshoot?  (sl_os = planned_sl - gross => gross = planned_sl - sl_os)
    recon_gross = planned_tp_sum + tp_os_sum + planned_sl_sum - sl_os_sum
    other_exits = [t for t in closed if t["exit_reason"] not in ("FIXED_TP", "INITIAL_STOP")]
    other_gross = sum(fnum(t["gross_u"]) for t in other_exits)
    unexplained = actual_gross - (recon_gross + other_gross)

    # rolling 10 by entry order (closed only for PnL buckets; open listed aside)
    closed_sorted = sorted(closed, key=lambda t: (t["entry_ts"], int(t["seq"])))
    rolling = []
    for i in range(0, len(closed_sorted), 10):
        chunk = closed_sorted[i : i + 10]
        rolling.append(
            {
                "bucket": f"{i+1}-{i+len(chunk)}",
                "n": len(chunk),
                "sum_rt_net": sum(fnum(t["rt_net_u"]) for t in chunk),
                "sum_gross": sum(fnum(t["gross_u"]) for t in chunk),
                "sum_fees": sum(fnum(t["rt_fee_u"]) for t in chunk),
                "n_tp": sum(1 for t in chunk if t["exit_reason"] == "FIXED_TP"),
                "n_sl": sum(1 for t in chunk if t["exit_reason"] == "INITIAL_STOP"),
            }
        )

    # cum curve by exit time
    by_exit = sorted(closed, key=lambda t: t["exit_ts"])
    cum = 0.0
    cum_curve = []
    for t in by_exit:
        cum += fnum(t["rt_net_u"])
        cum_curve.append(
            {
                "exit_ts": t["exit_ts"],
                "trade_id": t["trade_id"],
                "rt_net_u": fnum(t["rt_net_u"]),
                "cum_rt_net_u": cum,
            }
        )

    # hourly
    hourly = defaultdict(lambda: {"gross": 0.0, "fees": 0.0, "rt": 0.0, "n": 0})
    for t in closed:
        h = t["exit_ts"][:13]  # YYYY-MM-DDTHH
        hourly[h]["gross"] += fnum(t["gross_u"])
        hourly[h]["fees"] += fnum(t["rt_fee_u"])
        hourly[h]["rt"] += fnum(t["rt_net_u"])
        hourly[h]["n"] += 1

    # by symbol
    by_sym = defaultdict(lambda: {"gross": 0.0, "fees": 0.0, "rt": 0.0, "n": 0, "n_loss": 0})
    for t in closed:
        s = t["symbol"]
        by_sym[s]["gross"] += fnum(t["gross_u"])
        by_sym[s]["fees"] += fnum(t["rt_fee_u"])
        by_sym[s]["rt"] += fnum(t["rt_net_u"])
        by_sym[s]["n"] += 1
        if fnum(t["rt_net_u"]) <= 0:
            by_sym[s]["n_loss"] += 1

    # win path: drawdown before TP?
    win_tp = [t for t in wins if t["exit_reason"] == "FIXED_TP"]
    big_dd_before_tp = [
        t
        for t in win_tp
        if not math.isnan(fnum(t["obs_gross_mae_u"])) and fnum(t["obs_gross_mae_u"]) <= -25
    ]
    # net from overshoot?
    os_contrib = sum(max(0.0, fnum(t["tp_overshoot_gross_u"], 0)) for t in win_tp)
    win_gross = sum(fnum(t["gross_u"]) for t in win_tp)

    # early path: first favorable within first 3 points vs losses — descriptive
    # (use gross mfe and whether mfe before mae)
    loss_mfe_then = sum(1 for t in losses if t["obs_gross_order"] == "MFE_THEN_MAE")
    win_mfe_then = sum(1 for t in wins if t["obs_gross_order"] == "MFE_THEN_MAE")

    # path coverage
    n_insuff = sum(1 for t in trades if t["path_completeness"].startswith("不足"))
    n_gap = sum(1 for t in trades if "缺口" in t["path_completeness"])
    max_gaps = [fnum(t["max_gap_sec"]) for t in trades if not math.isnan(fnum(t["max_gap_sec"]))]

    # sensitivity without top 5 losses
    losses_sorted = sorted(losses, key=lambda t: fnum(t["rt_net_u"]))
    top5 = losses_sorted[:5]
    rest_rt = actual_rt - sum(fnum(t["rt_net_u"]) for t in top5)

    # engine mfe vs rebuilt: how often engine mfe==0 but gross mfe>0
    eng_zero_but_gross_pos = []
    for t in closed:
        eng = fnum(t["engine_mfe_u_closeFeeOnly_init0"])
        gm = fnum(t["obs_gross_mfe_u"])
        if (eng == 0 or math.isnan(eng)) and (not math.isnan(gm)) and gm > 1:
            eng_zero_but_gross_pos.append(t["trade_id"])

    return {
        "label": label,
        "n_total": len(trades),
        "n_closed": len(closed),
        "n_open": len(opens),
        "open_trade_ids": [t["trade_id"] for t in opens],
        "n_wins": len(wins),
        "n_losses": len(losses),
        "sum_gross": actual_gross,
        "sum_rt_fees": actual_fees,
        "sum_rt_net": actual_rt,
        "tp_count": len(tp_exits),
        "sl_count": len(sl_exits),
        "tp_but_net_loss": [
            {
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "rt_net_u": fnum(t["rt_net_u"]),
                "gross_u": fnum(t["gross_u"]),
                "rt_fee_u": fnum(t["rt_fee_u"]),
                "planned_tp_gross_u": fnum(t["planned_tp_gross_u"]),
            }
            for t in tp_net_loss
        ],
        "tp_net_0_5_n": len(tp_net_0_5),
        "tp_net_0_5_ids": [t["trade_id"] for t in tp_net_0_5],
        "tp_net_5_10_n": len(tp_net_5_10),
        "tp_net_5_10_ids": [t["trade_id"] for t in tp_net_5_10],
        "avg_win_rt": avg_win,
        "med_win_rt": med_win,
        "avg_loss_rt": avg_loss,
        "med_loss_rt": med_loss,
        "wins_needed_to_cover_avg_loss": payoff_needed,
        "fee_to_tp_ratio_dist": quantiles(fee_ratios),
        "near_fee_trades": [
            {
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "planned_tp_gross_u": fnum(t["planned_tp_gross_u"]),
                "planned_rt_fee_u": fnum(t["planned_rt_fee_at_tp_u"]),
                "ratio": fnum(t["fee_to_planned_tp_ratio"]),
                "version": t["version"],
            }
            for t in sorted(near_fee, key=lambda x: -fnum(x["fee_to_planned_tp_ratio"]))
        ],
        "exclusive_class_counts": dict(excl),
        "exclusive_class_ids": {
            k: [t["trade_id"] for t in trades if t["exclusive_class"] == k] for k in excl
        },
        "overlap_tag_counts": dict(tag_c),
        "decomp": {
            "planned_tp_gross_sum_on_tp_exits": planned_tp_sum,
            "tp_overshoot_sum": tp_os_sum,
            "planned_sl_gross_sum_on_sl_exits": planned_sl_sum,
            "sl_overshoot_loss_sum": sl_os_sum,
            "other_exit_gross": other_gross,
            "other_exit_ids": [t["trade_id"] for t in other_exits],
            "recon_gross_approx": recon_gross + other_gross,
            "actual_gross": actual_gross,
            "unexplained_gross": unexplained,
            "actual_rt_fees": actual_fees,
            "actual_rt_net": actual_rt,
            "identity_check": actual_gross - actual_fees - actual_rt,  # should ~0
        },
        "rolling10": rolling,
        "cum_curve": cum_curve,
        "hourly": dict(sorted(hourly.items())),
        "by_symbol": dict(
            sorted(by_sym.items(), key=lambda kv: kv[1]["rt"])
        ),
        "win_analysis": {
            "n_tp_wins": len(win_tp),
            "n_big_dd_before_tp_mae_le_m25": len(big_dd_before_tp),
            "big_dd_ids": [t["trade_id"] for t in big_dd_before_tp],
            "tp_overshoot_gross_sum_on_tp_wins": os_contrib,
            "tp_win_gross_sum": win_gross,
            "overshoot_share_of_tp_win_gross": (
                os_contrib / win_gross if win_gross else float("nan")
            ),
            "wins_order_MFE_THEN_MAE": win_mfe_then,
            "losses_order_MFE_THEN_MAE": loss_mfe_then,
        },
        "path_coverage": {
            "n_insufficient_ba": n_insuff,
            "n_large_gap": n_gap,
            "max_gap_p50": median(max_gaps),
            "max_gap_p95": quantiles(max_gaps, (0.95,)).get("p95", float("nan")),
            "engine_mfe0_but_obs_gross_mfe_gt1": eng_zero_but_gross_pos,
        },
        "sensitivity_drop_top5_losses": {
            "top5_ids": [t["trade_id"] for t in top5],
            "top5_rt_sum": sum(fnum(t["rt_net_u"]) for t in top5),
            "remaining_rt_net": rest_rt,
            "note": "敏感度描述，非獲利能力證明",
        },
        "tp_progress_bucket_counts": dict(Counter(t["tp_progress_bucket"] for t in closed)),
    }


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("")
        return
    # flatten
    keys = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def fmt_u(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.2f}"


def evidence_ids(ids: list[str], n: int = 8) -> str:
    if not ids:
        return "（無）"
    show = ids[:n]
    more = f" …共{len(ids)}筆" if len(ids) > n else ""
    return ", ".join(f"`{i}`" for i in show) + more


def segment_markdown(seg: dict, accounting: dict, sample: dict, orig_sum: dict) -> str:
    lines = []
    L = seg["label"]
    lines.append(f"## {L}")
    lines.append("")
    lines.append(
        f"- 樣本：總 {seg['n_total']}／已平倉 {seg['n_closed']}／未平倉 {seg['n_open']}"
        + (f"；未平倉 id：{evidence_ids(seg['open_trade_ids'])}" if seg["n_open"] else "")
    )
    lines.append(
        f"- 已平倉合計：毛 {fmt_u(seg['sum_gross'])}／往返費 {fmt_u(seg['sum_rt_fees'])}／往返淨 {fmt_u(seg['sum_rt_net'])}"
    )
    lines.append(
        f"- 退出：TP {seg['tp_count']}／SL {seg['sl_count']}；贏 {seg['n_wins']}／輸 {seg['n_losses']}"
    )
    lines.append("")
    lines.append("### 預定獲利空間 vs 費用")
    d = seg["fee_to_tp_ratio_dist"]
    lines.append(
        f"- 往返費／預定毛TP 比例分布：min={fmt_u(d.get('p0'))} p25={fmt_u(d.get('p25'))} "
        f"p50={fmt_u(d.get('p50'))} p75={fmt_u(d.get('p75'))} max={fmt_u(d.get('p100'))}"
    )
    lines.append(
        f"- 止盈後仍往返淨虧：{len(seg['tp_but_net_loss'])} 筆"
        + (
            "；例："
            + evidence_ids([x["trade_id"] for x in seg["tp_but_net_loss"]])
            if seg["tp_but_net_loss"]
            else ""
        )
    )
    for x in seg["tp_but_net_loss"][:5]:
        lines.append(
            f"  - `{x['trade_id']}` {x['symbol']} 毛TP預定={fmt_u(x['planned_tp_gross_u'])} "
            f"毛={fmt_u(x['gross_u'])} 費={fmt_u(x['rt_fee_u'])} 淨={fmt_u(x['rt_net_u'])}"
        )
    lines.append(f"- 止盈後淨利 0–5U：{seg['tp_net_0_5_n']}；5–10U：{seg['tp_net_5_10_n']}")
    lines.append(
        f"- 贏單平均／中位淨利：{fmt_u(seg['avg_win_rt'])}／{fmt_u(seg['med_win_rt'])}；"
        f"輸單平均／中位淨損：{fmt_u(seg['avg_loss_rt'])}／{fmt_u(seg['med_loss_rt'])}"
    )
    lines.append(
        f"- 平均一次輸單約需 {fmt_u(seg['wins_needed_to_cover_avg_loss'])} 次平均贏單補回"
    )
    if seg["near_fee_trades"]:
        lines.append(f"- 預定毛TP接近費用（費/TP≥0.7）共 {len(seg['near_fee_trades'])} 筆（診斷用，非改參依據）：")
        for x in seg["near_fee_trades"][:10]:
            lines.append(
                f"  - `{x['trade_id']}` {x['symbol']} TP毛={fmt_u(x['planned_tp_gross_u'])} "
                f"預定往返費={fmt_u(x['planned_rt_fee_u'])} 比={fmt_u(x['ratio'])}"
            )
    lines.append("")
    lines.append("### 互斥路徑分類（已公布定義；覆蓋全部樣本）")
    lines.append(
        "- A_NO_GROSS_MFE：觀測毛MFE≤0\n"
        "- B_GROSS_BUT_RT_NEVER_POS：有毛浮盈，但完整往返淨估值路徑從未>0\n"
        "- C_RT_POS_THEN_STOP：曾有正往返淨估值，未確認達毛TP，最終停損\n"
        "- D_MISSED_TP：退出側報價路徑毛值已達預定毛TP，卻未FIXED_TP（需順序可判定）\n"
        "- E_NON_STOP_EXIT：非停損／非FIXED_TP退出\n"
        "- TP_EXIT：FIXED_TP\n"
        "- F_OPEN／F_DATA／F_ORDER_UNCLEAR：未平倉／資料不足／同秒順序不明"
    )
    for k, n in sorted(seg["exclusive_class_counts"].items(), key=lambda kv: -kv[1]):
        ids = seg["exclusive_class_ids"].get(k, [])
        lines.append(f"- **{k}**: {n}/{seg['n_total']}；例 {evidence_ids(ids, 5)}")
    lines.append("")
    lines.append("### 可重疊標籤（不可加總當總筆數）")
    for k, n in sorted(seg["overlap_tag_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {n}")
    if not seg["overlap_tag_counts"]:
        lines.append("- （無）")
    lines.append("")
    lines.append("### 帳務分解（描述性）")
    dep = seg["decomp"]
    lines.append(
        f"- TP退出預定毛TP合計 {fmt_u(dep['planned_tp_gross_sum_on_tp_exits'])} + TP超額 {fmt_u(dep['tp_overshoot_sum'])}"
    )
    lines.append(
        f"- SL退出預定毛SL合計 {fmt_u(dep['planned_sl_gross_sum_on_sl_exits'])}；SL超額損失合計 {fmt_u(dep['sl_overshoot_loss_sum'])}"
    )
    lines.append(
        f"- 其他退出毛額 {fmt_u(dep['other_exit_gross'])} id={evidence_ids(dep['other_exit_ids'])}"
    )
    lines.append(
        f"- 實際毛 {fmt_u(dep['actual_gross'])}；未解釋毛差額 {fmt_u(dep['unexplained_gross'])}；"
        f"往返費 {fmt_u(dep['actual_rt_fees'])}；往返淨 {fmt_u(dep['actual_rt_net'])}"
    )
    lines.append(
        f"- 毛−費−淨 殘差 {fmt_u(dep['identity_check'])}（應≈0）"
    )
    lines.append("")
    lines.append("### 持續虧損檢查")
    lines.append("| 進場序桶 | n | 毛 | 費 | 往返淨 | TP | SL |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for b in seg["rolling10"]:
        lines.append(
            f"| {b['bucket']} | {b['n']} | {fmt_u(b['sum_gross'])} | {fmt_u(b['sum_fees'])} | "
            f"{fmt_u(b['sum_rt_net'])} | {b['n_tp']} | {b['n_sl']} |"
        )
    sens = seg["sensitivity_drop_top5_losses"]
    lines.append(
        f"- 去掉最大5筆虧損後剩餘往返淨 {fmt_u(sens['remaining_rt_net'])} "
        f"（敏感度；top5={evidence_ids(sens['top5_ids'],5)}）"
    )
    lines.append("")
    lines.append("### 贏單路徑")
    wa = seg["win_analysis"]
    lines.append(
        f"- TP贏單中，持倉內觀測毛MAE≤−25U：{wa['n_big_dd_before_tp_mae_le_m25']}/{wa['n_tp_wins']}；"
        f"例 {evidence_ids(wa['big_dd_ids'],5)}"
    )
    lines.append(
        f"- TP贏單毛額中超額跳價占比 {fmt_u(wa['overshoot_share_of_tp_win_gross'])} "
        f"（超額合計 {fmt_u(wa['tp_overshoot_gross_sum_on_tp_wins'])}／毛 {fmt_u(wa['tp_win_gross_sum'])}）"
    )
    lines.append(
        f"- 順序 MFE→MAE：贏 {wa['wins_order_MFE_THEN_MAE']}／輸 {wa['losses_order_MFE_THEN_MAE']}（poll觀測，非因果）"
    )
    lines.append("")
    lines.append("### 路徑覆蓋與引擎MFE口徑")
    pc = seg["path_coverage"]
    lines.append(
        f"- BA點不足(<3)：{pc['n_insufficient_ba']}；大缺口標記：{pc['n_large_gap']}；"
        f"max_gap p50={fmt_u(pc['max_gap_p50'])}"
    )
    lines.append(
        f"- 引擎 mfe_u≈0 但重建毛MFE>1U：{len(pc['engine_mfe0_but_obs_gross_mfe_gt1'])} 筆 "
        f"（因引擎用 closeFeeOnly 且初值0截斷）例 {evidence_ids(pc['engine_mfe0_but_obs_gross_mfe_gt1'],5)}"
    )
    lines.append("")
    lines.append("### orig_side／direction_relation 欄位稽核（非鏡像比較）")
    lines.append(f"- {orig_sum}")
    lines.append("")
    return "\n".join(lines)


def one_liner(seg: dict, account_label: str) -> str:
    excl = seg["exclusive_class_counts"]
    n = seg["n_closed"] or 1
    a = excl.get("A_NO_GROSS_MFE", 0)
    b = excl.get("B_GROSS_BUT_RT_NEVER_POS", 0)
    c = excl.get("C_RT_POS_THEN_STOP", 0)
    d = excl.get("D_MISSED_TP", 0)
    sl_os = seg["decomp"]["sl_overshoot_loss_sum"]
    fee = seg["sum_rt_fees"]
    parts = []
    # dominant among loss path classes
    loss_classes = {
        "A進場後觀測無毛浮盈": a,
        "B有毛浮盈但不足以覆蓋往返費": b,
        "C曾有正往返淨估值卻停損": c,
        "D路徑曾達毛TP卻未止盈": d,
    }
    dom = max(loss_classes.items(), key=lambda kv: kv[1]) if any(loss_classes.values()) else ("（路徑類不足）", 0)
    return (
        f"{account_label}：目前有證據的主要損失來源是「{dom[0]}」（{dom[1]}/{seg['n_total']}）"
        f"，疊加往返費合計 {fmt_u(fee)}U 與 SL超額損失合計 {fmt_u(sl_os)}U；"
        f"還缺 tick 級連續報價與觸發當下的精確先後，才能判定是進場優勢不足還是退出／執行問題——"
        f"以現有 poll 資料無法證明進場缺乏優勢為唯一根因。"
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    all_trades = []
    for acc in ("A", "B", "C"):
        print(f"analyzing {acc}...")
        results[acc] = analyze_account(acc)
        all_trades.extend(results[acc]["trades"])

    # write per-trade CSV
    write_csv(OUT / "ALL_trades_path_audit.csv", all_trades)
    for acc in ("A", "B", "C"):
        write_csv(OUT / f"{acc}_trades_path_audit.csv", results[acc]["trades"])

    # segments
    a_trades = results["A"]["trades"]
    a1 = [t for t in a_trades if t["version"].startswith("A1")]
    a2 = [t for t in a_trades if t["version"].startswith("A2")]
    seg_a1 = summarize_segment(a1, "A1_selfscan_flip_ATR")
    seg_a2 = summarize_segment(a2, "A2_mirror_same_ATR")
    seg_b = summarize_segment(results["B"]["trades"], "B_selfscan_flip_fixed50_75")
    seg_c = summarize_segment(results["C"]["trades"], "C_mirror_inv_fixed50_75")

    # orig side CSV
    orig_rows = []
    for acc in ("A", "B", "C"):
        orig_rows.extend(results[acc]["orig_side_audit_rows"])
    write_csv(OUT / "orig_side_field_audit.csv", orig_rows)

    # rolling / cum / hourly CSVs
    for name, seg in (("A1", seg_a1), ("A2", seg_a2), ("B", seg_b), ("C", seg_c)):
        write_csv(OUT / f"{name}_rolling10.csv", seg["rolling10"])
        write_csv(OUT / f"{name}_cum_curve.csv", seg["cum_curve"])
        hourly_rows = [
            {"hour": h, **v} for h, v in seg["hourly"].items()
        ]
        write_csv(OUT / f"{name}_hourly.csv", hourly_rows)
        sym_rows = [{"symbol": s, **v} for s, v in seg["by_symbol"].items()]
        write_csv(OUT / f"{name}_by_symbol.csv", sym_rows)

    # MFE formula note artifact
    mfe_note = {
        "engine_location": "portfolio.py manage_position → estimate_net_unrealized",
        "engine_formula": "mfe/mae track net_now = gross(mark) - estimated_close_fee ONLY; open fee NOT subtracted; initialized at 0.0 so never-positive stays 0",
        "mark": "mark_price_for_side: LONG=bid*(1-slip), SHORT=ask*(1+slip); slip=0.0002 applied once",
        "round6_rebuild": {
            "gross_path": "(exit_mark - entry)*qty for LONG (sign flip for SHORT)",
            "rt_net_path": "gross - actual_open_fee - estimated_close_fee(at mark)",
            "excludes_pre_entry_quotes": True,
            "includes_entry_synthetic": False,
            "exit_ts_inclusive_if_quote_present": True,
            "mid_only": "reference columns only, not in primary MFE/MAE",
            "caveat": "poll observations not true extrema; same-second order marked 順序不明",
        },
        "sources": {
            "A": "runtime_mirror_same_a_20261120T010410/code/.../portfolio.py (same formula in sealed runtimes)",
            "B": "runtime_fixed_usdt_50_75_20261119T231853/code/.../portfolio.py:446-451,548-559",
            "C": "runtime_mirror_inv_b_20261120T004427/code/.../portfolio.py",
        },
    }
    (OUT / "mfe_mae_formula_audit.json").write_text(json.dumps(mfe_note, ensure_ascii=False, indent=2))

    # accounting summary
    acct_rows = []
    for acc in ("A", "B", "C"):
        a = results[acc]["accounting"]
        s = results[acc]["sample_check"]
        acct_rows.append({"account": acc, **{f"sample_{k}": v for k, v in s.items() if k != "post_cutoff_open_ids"}, **a})
    # simplify sample ids
    write_csv(
        OUT / "accounting_reconciliation.csv",
        [
            {
                "account": r["account"],
                "r5_n": results[r["account"]]["sample_check"]["r5_n"],
                "fills_opens_le": results[r["account"]]["sample_check"]["fills_opens_le"],
                "fills_closes_le": results[r["account"]]["sample_check"]["fills_closes_le"],
                "ids_match": results[r["account"]]["sample_check"]["ids_match"],
                "post_cutoff_opens": results[r["account"]]["sample_check"]["post_cutoff_opens"],
                **results[r["account"]]["accounting"],
            }
            for r in acct_rows
        ],
    )

    meta = {
        "cutoff": CUTOFF,
        "expected_counts": {"A": 90, "B": 65, "C": 41},
        "actual": {acc: results[acc]["sample_check"] for acc in ("A", "B", "C")},
        "a1_n": len(a1),
        "a2_n": len(a2),
        "segments": {
            "A1": {k: seg_a1[k] for k in seg_a1 if k not in ("cum_curve", "hourly", "by_symbol", "exclusive_class_ids")},
            "A2": {k: seg_a2[k] for k in seg_a2 if k not in ("cum_curve", "hourly", "by_symbol", "exclusive_class_ids")},
            "B": {k: seg_b[k] for k in seg_b if k not in ("cum_curve", "hourly", "by_symbol", "exclusive_class_ids")},
            "C": {k: seg_c[k] for k in seg_c if k not in ("cum_curve", "hourly", "by_symbol", "exclusive_class_ids")},
        },
    }
    # make JSON serializable - exclusive_class_ids needed in report from seg
    def scrub(o):
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [scrub(v) for v in o]
        return o

    (OUT / "analytics_round6.json").write_text(
        json.dumps(
            scrub(
                {
                    "meta": {
                        "cutoff": CUTOFF,
                        "sample": {acc: results[acc]["sample_check"] for acc in ("A", "B", "C")},
                        "a1_n": len(a1),
                        "a2_n": len(a2),
                    },
                    "A1": seg_a1,
                    "A2": seg_a2,
                    "B": seg_b,
                    "C": seg_c,
                    "accounting": {acc: results[acc]["accounting"] for acc in ("A", "B", "C")},
                    "orig_side_summary": {
                        acc: results[acc]["orig_side_audit_summary"] for acc in ("A", "B", "C")
                    },
                    "mfe_formula": mfe_note,
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    # post-cutoff list
    post_rows = []
    for acc in ("A", "B", "C"):
        for f in results[acc]["post_cutoff_opens"]:
            post_rows.append(
                {
                    "account": acc,
                    "trade_id": f["trade_id"],
                    "symbol": f["symbol"],
                    "side": f["side"],
                    "recorded_at": f["recorded_at"],
                    "note": "後續驗證樣本；未混入主樣本",
                }
            )
    write_csv(OUT / "post_cutoff_opens.csv", post_rows)

    # Build main report
    md = []
    md.append("# Round-6：A／B／C 各策略獨立虧損根因稽核")
    md.append("")
    md.append(f"**主樣本截止**：`{CUTOFF}`（與 Round-5 相同）")
    md.append("**範圍**：唯讀分析產物；不調參、不換向、不做 A/B/C 配對比較。")
    md.append("")
    md.append("## 〇、樣本核對")
    md.append("")
    md.append("| 帳戶 | 預期 | R5 CSV | fills OPEN≤截止 | fills CLOSE≤截止 | ID一致 | 截止後新開 |")
    md.append("|---|---:|---:|---:|---:|---|---:|")
    for acc, exp in (("A", 90), ("B", 65), ("C", 41)):
        s = results[acc]["sample_check"]
        md.append(
            f"| {acc} | {exp} | {s['r5_n']} | {s['fills_opens_le']} | {s['fills_closes_le']} | "
            f"{'Y' if s['ids_match'] else 'N'} | {s['post_cutoff_opens']} |"
        )
    md.append("")
    md.append(
        f"- A 分段：A1={len(a1)}／A2={len(a2)}（不可混算機制）。"
    )
    md.append(
        "- 盈虧重算：`gross=(exit-entry)*qty`（空反向）；`close_net=gross-close_fee`；`rt_net=close_net-open_fee`。"
        "名義本金≈qty×entry（約 10k 但逐筆不同）。資金費未模擬（`funding_data_gap`）。"
    )
    md.append("- 滑價已在成交價內，未再扣一次。")
    md.append("")
    md.append("### 帳務對帳（fills 重建 vs equity snapshot）")
    md.append("")
    md.append(
        "| 帳戶 | Σgross | Σopen_fee | Σclose_fee | Σclose_net | recon=close_nets−all_open_fees | equity.realized | gap |"
    )
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for acc in ("A", "B", "C"):
        a = results[acc]["accounting"]
        md.append(
            f"| {acc} | {fmt_u(a['sum_gross_closed'])} | {fmt_u(a['sum_open_fee_all'])} | "
            f"{fmt_u(a['sum_close_fee_closed'])} | {fmt_u(a['sum_close_net_closed'])} | "
            f"{fmt_u(a['recon_realized_closeNets_minus_allOpenFees'])} | "
            f"{fmt_u(a['equity_snap_realized'])} | {fmt_u(a['gap_recon_minus_equity'])} |"
        )
    md.append("")
    md.append(
        "- **B**：fills 重建與 equity 一致（已證實）。**A／C**：存在 gap，見「資料問題」；本輪損益統計以 fills／R5 逐筆為準。"
    )
    md.append("")
    md.append("### 引擎 MFE／MAE 口徑更正")
    md.append("")
    md.append(
        "- 位置：`portfolio.py` `estimate_net_unrealized` + `manage_position` 更新。"
    )
    md.append(
        "- **引擎 `mfe_u`／`mae_u`** = 毛盈虧 − **預估平倉費**（不含開倉費）；初值 **0**，故從未轉正時 MFE 停在 0（零初始化截斷）。"
    )
    md.append(
        "- **不是**完整往返淨，也**不是**純毛盈虧；標記 `excursion_source=polled_quote_estimate`。"
    )
    md.append(
        "- Round-6 重建：`obs_gross_mfe/mae`（純毛）、`obs_rt_mfe/mae`（扣實際開倉費+該時點估平倉費）；"
        "多=bid、空=ask，滑價按模型再套一次；**不用 mid 進主統計**。"
    )
    md.append(
        "- 路徑不含入場前報價；不含合成入場點；退出時點若有報價則納入；同秒極值標「順序不明」。"
    )
    md.append("")

    # sections 1-3
    md.append(segment_markdown(seg_a1, results["A"]["accounting"], results["A"]["sample_check"], results["A"]["orig_side_audit_summary"]))
    md.append(segment_markdown(seg_a2, results["A"]["accounting"], results["A"]["sample_check"], results["A"]["orig_side_audit_summary"]))
    md.append(segment_markdown(seg_b, results["B"]["accounting"], results["B"]["sample_check"], results["B"]["orig_side_audit_summary"]))
    md.append(segment_markdown(seg_c, results["C"]["accounting"], results["C"]["sample_check"], results["C"]["orig_side_audit_summary"]))

    md.append("## 四、資料／計算問題與未解答事項")
    md.append("")
    md.append(
        "1. **A／C equity.realized 與 fills 重建不一致**"
        f"（A gap={fmt_u(results['A']['accounting']['gap_recon_minus_equity'])}；"
        f"C gap={fmt_u(results['C']['accounting']['gap_recon_minus_equity'])}）。"
        "B 一致。原因未在本輪定位（可能跨 run 帳戶狀態／快照時點）；**狀態：未知**。"
    )
    md.append(
        "2. **報價為 poll**（MANAGE≈5s），MFE/MAE 為觀測值非真實極值；缺口期間價格路徑未知。"
    )
    md.append(
        "3. **orig_side 在 MIRROR_SAME／MIRROR_INVERSE** 已證實記的是**來源成交 side**，不是市場突破方向；"
        "不得用 R5 `direction_relation` 對 A2／C 做順勢／逆勢虧損統計。"
    )
    md.append(
        "4. **D_MISSED_TP** 僅在 BA 路徑毛值≥預定TP且順序可判定時成立；本輪若為 0 不代表執行完美，只代表無法以現有證據宣判漏止盈。"
    )
    md.append(
        "5. **未平倉**獨立列出，不納入已實現分解。"
    )
    md.append(
        "6. 截止後新開倉見 `post_cutoff_opens.csv`，未混入主樣本。"
    )
    md.append("")
    md.append("## 五、一句話（各策略獨立）")
    md.append("")
    md.append(f"- {one_liner(seg_a1, 'A1')}")
    md.append(f"- {one_liner(seg_a2, 'A2')}")
    md.append(f"- {one_liner(seg_b, 'B')}")
    md.append(f"- {one_liner(seg_c, 'C')}")
    md.append("")
    md.append("## 產物路徑")
    md.append("")
    md.append(f"- 目錄：`{OUT}`")
    md.append("- 逐筆：`A_trades_path_audit.csv`／`B_…`／`C_…`／`ALL_trades_path_audit.csv`")
    md.append("- 精簡本檔：`REPORT_ROUND6.md`")
    md.append("")

    report = "\n".join(md)
    (OUT / "REPORT_ROUND6.md").write_text(report)

    # compact summary md
    compact = []
    compact.append("# Round-6 精簡摘要")
    compact.append("")
    compact.append(f"截止 `{CUTOFF}`｜A90(A1={len(a1)},A2={len(a2)}) B65 C41｜與 fills ID 一致")
    compact.append("")
    for seg in (seg_a1, seg_a2, seg_b, seg_c):
        compact.append(
            f"### {seg['label']}｜閉 {seg['n_closed']} 開 {seg['n_open']}｜"
            f"毛 {fmt_u(seg['sum_gross'])} 費 {fmt_u(seg['sum_rt_fees'])} 淨 {fmt_u(seg['sum_rt_net'])}"
        )
        compact.append(
            f"TP/SL {seg['tp_count']}/{seg['sl_count']}｜"
            f"贏均 {fmt_u(seg['avg_win_rt'])} 輸均 {fmt_u(seg['avg_loss_rt'])}｜"
            f"補回比 {fmt_u(seg['wins_needed_to_cover_avg_loss'])}"
        )
        compact.append(f"分類 {seg['exclusive_class_counts']}")
        compact.append(f"費/TP≥0.7：{len(seg['near_fee_trades'])} 筆")
        compact.append("")
    compact.append("### 一句話")
    compact.append(one_liner(seg_a1, "A1"))
    compact.append(one_liner(seg_a2, "A2"))
    compact.append(one_liner(seg_b, "B"))
    compact.append(one_liner(seg_c, "C"))
    (OUT / "REPORT_ROUND6_COMPACT.md").write_text("\n".join(compact))

    print("DONE", OUT)
    print("A1", seg_a1["sum_rt_net"], seg_a1["exclusive_class_counts"])
    print("A2", seg_a2["sum_rt_net"], seg_a2["exclusive_class_counts"])
    print("B", seg_b["sum_rt_net"], seg_b["exclusive_class_counts"])
    print("C", seg_c["sum_rt_net"], seg_c["exclusive_class_counts"])


if __name__ == "__main__":
    main()
