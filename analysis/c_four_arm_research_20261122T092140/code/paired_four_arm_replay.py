#!/usr/bin/env python3
"""Offline paired four-arm path comparison for C pyramid (NOT full strategy backtest).

Fixed P0 entry time/price/qty from baseline paper fills; arms only change add/exit management.
Fee stage-1: all 0.0005. Separate fee-sensitivity column (maker 0.0002 / taker 0.0005 by intended role).
Does not write original ledgers. Does not place orders.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / "output/paper_bcd_local/data_c/output/testnet_c_inv_v1/runs/20261121T201533_9a0f7b4f"
OUT = Path(__file__).resolve().parents[1]
ADD_PCT = 0.005
P0_TP_PCT = 0.01
FEE = 0.0005
MAKER = 0.0002
TAKER = 0.0005


def load_jsonl(p: Path) -> list[dict]:
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    return (exit_px - entry) * qty if side == "LONG" else (entry - exit_px) * qty


def levels(side: str, p0: float) -> dict[str, float]:
    if side == "LONG":
        p1 = p0 * (1 - ADD_PCT)
        p2 = p1 * (1 - ADD_PCT)
        pstop = p2 * (1 - ADD_PCT)
        tp = p0 * (1 + P0_TP_PCT)
    else:
        p1 = p0 * (1 + ADD_PCT)
        p2 = p1 * (1 + ADD_PCT)
        pstop = p2 * (1 + ADD_PCT)
        tp = p0 * (1 - P0_TP_PCT)
    return {"P1": p1, "P2": p2, "Pstop": pstop, "P0_TP": tp}


@dataclass
class Layer:
    name: str
    entry: float
    qty: float
    fee_in: float
    closed: bool = False
    exit_px: float = 0.0
    exit_fee: float = 0.0
    exit_reason: str = ""
    exit_t: str = ""


@dataclass
class ArmState:
    arm: str
    layers: list[Layer] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    path_tag: str = ""
    closed: bool = False
    open_at_cutoff: bool = False
    notes: list[str] = field(default_factory=list)


def fee_u(notional: float, rate: float = FEE) -> float:
    return abs(notional) * rate


def arm_net(st: ArmState, *, rate_entry: float, rate_exit_fn) -> tuple[float, float, float]:
    """Return gross, fees, net using given fee rates (sensitivity)."""
    g = 0.0
    fees = 0.0
    for L in st.layers:
        fees += abs(L.entry * L.qty) * rate_entry
        if L.closed:
            g += pnl(st.events[0]["side"] if st.events else "LONG", L.entry, L.exit_px, L.qty)  # side patched below
    # fix: store side on ArmState
    return g, fees, g - fees


def finalize_net(st: ArmState, side: str, fee_rate: float = FEE) -> dict[str, float]:
    gross = 0.0
    fees = 0.0
    for L in st.layers:
        fees += fee_u(L.entry * L.qty, fee_rate)
        if L.closed:
            gross += pnl(side, L.entry, L.exit_px, L.qty)
            fees += fee_u(L.exit_px * L.qty, fee_rate)
        else:
            # open layer: no exit fee in realized
            pass
    # sensitivity: intended maker on entry/add/R2/TP, taker on PSTOP
    fees_sens = 0.0
    gross_same = gross
    for L in st.layers:
        fees_sens += fee_u(L.entry * L.qty, MAKER)
        if L.closed:
            if L.exit_reason == "PYRAMID_PSTOP" or L.exit_reason.startswith("B2_PROTECT"):
                fees_sens += fee_u(L.exit_px * L.qty, TAKER)
            else:
                fees_sens += fee_u(L.exit_px * L.qty, MAKER)
    return {
        "gross_u": gross,
        "fees_u": fees,
        "net_u": gross - fees,
        "fees_sens_u": fees_sens,
        "net_sens_u": gross_same - fees_sens,
    }


def close_layer(L: Layer, px: float, reason: str, t: str) -> None:
    L.closed = True
    L.exit_px = px
    L.exit_reason = reason
    L.exit_t = t
    L.exit_fee = fee_u(px * L.qty, FEE)


def close_all_open(st: ArmState, px: float, reason: str, t: str) -> None:
    for L in st.layers:
        if not L.closed:
            close_layer(L, px, reason, t)
    st.closed = True


def simulate_cycle(tid: str, side: str, fl: list[dict], cutoff: str) -> dict[str, Any]:
    opens = [f for f in fl if f.get("event") == "OPEN"]
    if not opens:
        return {"trade_id": tid, "error": "NO_OPEN", "arms": {}}
    o = opens[0]
    p0 = float(o["price"])
    q0 = float(o["qty"])
    t0 = o.get("recorded_at") or ""
    lv = levels(side, p0)
    # recorded stop from OPEN if present
    pstop = float(o["initial_stop"]) if o.get("initial_stop") is not None else lv["Pstop"]
    p0tp = float(o["take_profit"]) if o.get("take_profit") is not None else lv["P0_TP"]

    # Chronological ledger events used as shared "price path" for paired management
    timeline = []
    for f in fl:
        ev = f.get("event")
        if ev in ("OPEN", "PYRAMID_ADD", "CLOSE", "CLOSE_PARTIAL"):
            timeline.append(f)
    timeline.sort(key=lambda x: x.get("recorded_at") or "")

    def new_arm(name: str) -> ArmState:
        st = ArmState(arm=name)
        st.layers = [Layer("P0", p0, q0, fee_u(p0 * q0))]
        st.events.append({"side": side, "p0": p0, "t0": t0})
        return st

    arms = {k: new_arm(k) for k in ("B0", "B1", "B2", "B3")}
    # Track whether B2 already protect-exited
    b2_done = False

    for f in timeline:
        ev = f.get("event")
        t = f.get("recorded_at") or ""
        px = float(f.get("price") or 0)
        qty = float(f.get("qty") or 0)
        layer = f.get("layer")
        reason = f.get("exit_reason") or ""

        # --- ADD ---
        if ev == "PYRAMID_ADD":
            name = str(layer)
            # B0: take actual add
            if name in ("P1", "P2"):
                arms["B0"].layers.append(Layer(name, px, qty, fee_u(px * qty)))
                arms["B0"].events.append({"ev": "ADD", "layer": name, "t": t, "px": px, "qty": qty})
            # B1: only P1
            if name == "P1":
                arms["B1"].layers.append(Layer(name, px, qty, fee_u(px * qty)))
                arms["B1"].events.append({"ev": "ADD", "layer": name, "t": t, "px": px, "qty": qty})
            # B2: only P1; if this is P2 add moment → protective flatten ALL at this price (jump-over test)
            if name == "P1":
                arms["B2"].layers.append(Layer(name, px, qty, fee_u(px * qty)))
                arms["B2"].events.append({"ev": "ADD", "layer": name, "t": t, "px": px, "qty": qty})
            if name == "P2" and not b2_done:
                # Price reached original P2 trigger → protect exit remaining
                close_all_open(arms["B2"], px, "B2_PROTECT_AT_P2_TRIGGER", t)
                arms["B2"].events.append({"ev": "B2_PROTECT", "t": t, "px": px})
                arms["B2"].path_tag = "B2_protect_at_P2"
                b2_done = True
            # B3: ignore all adds
            continue

        # --- EXIT ---
        if ev in ("CLOSE", "CLOSE_PARTIAL"):
            # Shared mark for this exit event
            # B0: apply as recorded
            for L in arms["B0"].layers:
                if not L.closed and L.name == layer:
                    close_layer(L, px, reason, t)
                    arms["B0"].events.append({"ev": "EXIT", "layer": layer, "reason": reason, "t": t, "px": px})
            if all(L.closed for L in arms["B0"].layers):
                arms["B0"].closed = True

            # B1: ignore P2 layer exits; map P0/P1 exits; if PSTOP on any, close all open B1 layers at px
            if reason == "PYRAMID_PSTOP":
                if not arms["B1"].closed:
                    close_all_open(arms["B1"], px, "PYRAMID_PSTOP", t)
                    arms["B1"].events.append({"ev": "EXIT_ALL", "reason": "PYRAMID_PSTOP", "t": t, "px": px})
            elif layer in ("P0", "P1"):
                for L in arms["B1"].layers:
                    if not L.closed and L.name == layer:
                        close_layer(L, px, reason, t)
                        arms["B1"].events.append({"ev": "EXIT", "layer": layer, "reason": reason, "t": t, "px": px})
                if all(L.closed for L in arms["B1"].layers):
                    arms["B1"].closed = True

            # B2: if already protect-done, ignore further; else same as B1 for P0/P1, PSTOP closes all
            if not b2_done and not arms["B2"].closed:
                if reason == "PYRAMID_PSTOP":
                    close_all_open(arms["B2"], px, "PYRAMID_PSTOP", t)
                    arms["B2"].events.append({"ev": "EXIT_ALL", "reason": "PYRAMID_PSTOP", "t": t, "px": px})
                elif layer in ("P0", "P1"):
                    for L in arms["B2"].layers:
                        if not L.closed and L.name == layer:
                            close_layer(L, px, reason, t)
                            arms["B2"].events.append({"ev": "EXIT", "layer": layer, "reason": reason, "t": t, "px": px})
                    if all(L.closed for L in arms["B2"].layers):
                        arms["B2"].closed = True

            # B3: only P0 layer events; PSTOP/P0_TP/R2-on-P0
            if not arms["B3"].closed:
                if reason == "PYRAMID_PSTOP":
                    close_all_open(arms["B3"], px, "PYRAMID_PSTOP", t)
                elif layer == "P0":
                    for L in arms["B3"].layers:
                        if not L.closed and L.name == "P0":
                            close_layer(L, px, reason, t)
                    if all(L.closed for L in arms["B3"].layers):
                        arms["B3"].closed = True
                # R2 on P1/P2 ignored for B3

    # Cutoff: mark still-open
    for name, st in arms.items():
        if not all(L.closed for L in st.layers):
            st.open_at_cutoff = True
            st.closed = False
            st.notes.append("OPEN_AT_CUTOFF_NOT_FLATTED")

    # Path tags for B0
    b0 = arms["B0"]
    reasons = [L.exit_reason for L in b0.layers if L.closed]
    adds = [L.name for L in b0.layers if L.name != "P0"]
    p1_r2 = any(L.name == "P1" and L.exit_reason == "PYRAMID_R2_REBOUND" for L in b0.layers)
    p2_r2 = any(L.name == "P2" and L.exit_reason == "PYRAMID_R2_REBOUND" for L in b0.layers)
    if b0.open_at_cutoff or not all(L.closed for L in b0.layers):
        b0.path_tag = "open_at_cutoff"
    elif not adds and any(r == "PYRAMID_P0_TP1PCT" for r in reasons):
        b0.path_tag = "no_add_then_TP"
    elif "P1" in adds and "P2" not in adds and p1_r2:
        b0.path_tag = "P1_R2_first"
    elif "P2" in adds and p1_r2 and p2_r2 and any(r == "PYRAMID_P0_TP1PCT" for r in reasons):
        b0.path_tag = "P2_then_R2_then_P0_TP"
    elif "P2" in adds and (p1_r2 or p2_r2) and any(r == "PYRAMID_PSTOP" for r in reasons):
        b0.path_tag = "P1_exited_P0_P2_stop" if p1_r2 and not p2_r2 else "P2_R2_mix"
    elif "P2" in adds and all(r == "PYRAMID_PSTOP" for r in reasons if r):
        b0.path_tag = "three_layer_direct_PSTOP"
    else:
        b0.path_tag = "other"

    # If B1/B2/B3 still open but B0 closed via PSTOP-only path without matching exit event for them —
    # already handled. If B0 closed with only P2-related and B3 never got P0 exit: mark UNKNOWN path gap
    out_arms = {}
    for name, st in arms.items():
        m = finalize_net(st, side, FEE)
        out_arms[name] = {
            **m,
            "closed": st.closed and not st.open_at_cutoff,
            "open_at_cutoff": st.open_at_cutoff,
            "path_tag": st.path_tag or b0.path_tag,
            "layers": [
                {
                    "name": L.name,
                    "entry": L.entry,
                    "qty": L.qty,
                    "closed": L.closed,
                    "exit_px": L.exit_px if L.closed else None,
                    "exit_reason": L.exit_reason or None,
                    "exit_t": L.exit_t or None,
                }
                for L in st.layers
            ],
            "notes": st.notes,
            "n_layers": len(st.layers),
        }
    # deltas vs B0
    for name in ("B1", "B2", "B3"):
        if out_arms["B0"]["closed"] and out_arms[name]["closed"]:
            out_arms[name]["delta_net_vs_B0"] = out_arms[name]["net_u"] - out_arms["B0"]["net_u"]
            out_arms[name]["delta_net_sens_vs_B0"] = out_arms[name]["net_sens_u"] - out_arms["B0"]["net_sens_u"]
        else:
            out_arms[name]["delta_net_vs_B0"] = "UNKNOWN_OPEN_OR_INCOMPLETE"
            out_arms[name]["delta_net_sens_vs_B0"] = "UNKNOWN_OPEN_OR_INCOMPLETE"

    return {
        "trade_id": tid,
        "symbol": tid.split(":")[1] if ":" in tid else "",
        "side": side,
        "p0_anchor": p0,
        "p0_qty": q0,
        "p0_time": t0,
        "pstop": pstop,
        "p0_tp": p0tp,
        "levels_theory": lv,
        "baseline_path_tag": b0.path_tag,
        "arms": out_arms,
        "comparison_type": "paired_path_management_only",
        "not_full_strategy_backtest": True,
        "fee_stage": "uniform_0.0005",
        "funding": "UNKNOWN",
    }


def path_class(cycle: dict) -> str:
    return cycle.get("baseline_path_tag") or "other"


def main() -> int:
    fills = load_jsonl(RUN / "fills.jsonl")
    by: dict[str, list] = defaultdict(list)
    for f in fills:
        if f.get("trade_id"):
            by[str(f["trade_id"])].append(f)

    # Only cycles that OPEN in this run
    cycles = []
    for tid, fl in by.items():
        opens = [x for x in fl if x.get("event") == "OPEN"]
        if not opens:
            continue
        side = opens[0].get("side") or "LONG"
        cycles.append(simulate_cycle(tid, side, fl, "2026-11-22T07:34:51+08:00"))

    # Metrics per arm
    arm_metrics = []
    for arm in ("B0", "B1", "B2", "B3"):
        closed = [c for c in cycles if c["arms"][arm]["closed"]]
        opened = [c for c in cycles if c["arms"][arm]["open_at_cutoff"]]
        nets = [c["arms"][arm]["net_u"] for c in closed]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
        arm_metrics.append(
            {
                "arm": arm,
                "n_cycles_total": len(cycles),
                "n_closed": len(closed),
                "n_open_at_cutoff": len(opened),
                "gross_sum_u": sum(c["arms"][arm]["gross_u"] for c in closed),
                "fees_sum_u": sum(c["arms"][arm]["fees_u"] for c in closed),
                "net_sum_u": sum(nets) if nets else 0.0,
                "funding": "UNKNOWN",
                "win_n": len(wins),
                "loss_n": len(losses),
                "win_rate": (len(wins) / len(closed)) if closed else None,
                "avg_win_u": (sum(wins) / len(wins)) if wins else None,
                "avg_loss_u": (sum(losses) / len(losses)) if losses else None,
                "profit_factor": pf,
                "worst_cycle_net_u": min(nets) if nets else None,
                "best_cycle_net_u": max(nets) if nets else None,
                "net_sens_sum_u": sum(c["arms"][arm]["net_sens_u"] for c in closed),
                "note": "paired path only; equity drawdown / exposure integrals UNKNOWN without continuous equity series per arm",
                "equity_drawdown_u": "UNKNOWN",
                "max_notional_exposure_u": "UNKNOWN",
                "margin_occupancy_u": "UNKNOWN",
                "exposure_time_integral": "UNKNOWN",
                "return_per_exposure": "UNKNOWN",
            }
        )

    # Paired deltas (closed both)
    paired_rows = []
    for c in cycles:
        row = {
            "trade_id": c["trade_id"],
            "symbol": c["symbol"],
            "side": c["side"],
            "p0_anchor": c["p0_anchor"],
            "path_tag": c["baseline_path_tag"],
            "B0_net": c["arms"]["B0"]["net_u"] if c["arms"]["B0"]["closed"] else "OPEN",
            "B1_net": c["arms"]["B1"]["net_u"] if c["arms"]["B1"]["closed"] else "OPEN",
            "B2_net": c["arms"]["B2"]["net_u"] if c["arms"]["B2"]["closed"] else "OPEN",
            "B3_net": c["arms"]["B3"]["net_u"] if c["arms"]["B3"]["closed"] else "OPEN",
            "B1_minus_B0": c["arms"]["B1"].get("delta_net_vs_B0"),
            "B2_minus_B0": c["arms"]["B2"].get("delta_net_vs_B0"),
            "B3_minus_B0": c["arms"]["B3"].get("delta_net_vs_B0"),
            "comparison_type": "paired_path_management_only",
            "funding": "UNKNOWN",
        }
        paired_rows.append(row)

    def delta_stats(key: str) -> dict:
        vals = []
        for r in paired_rows:
            v = r[key]
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if not vals:
            return {"n": 0, "improve_n": 0, "worsen_n": 0, "mean": None, "median": None, "leave_one_out_sign_stable": "UNKNOWN"}
        vals_s = sorted(vals)
        mid = vals_s[len(vals_s) // 2]
        mean = sum(vals) / len(vals)
        # leave-one-out direction stability
        signs = []
        for i in range(len(vals)):
            sub = vals[:i] + vals[i + 1 :]
            if not sub:
                continue
            signs.append(1 if sum(sub) > 0 else (-1 if sum(sub) < 0 else 0))
        stable = len(set(signs)) == 1 if signs else False
        return {
            "n": len(vals),
            "improve_n": sum(1 for v in vals if v > 0),
            "worsen_n": sum(1 for v in vals if v < 0),
            "mean": mean,
            "median": mid,
            "leave_one_out_sign_stable": stable,
            "sum": sum(vals),
        }

    paired_summary = {
        "B1_minus_B0": delta_stats("B1_minus_B0"),
        "B2_minus_B0": delta_stats("B2_minus_B0"),
        "B3_minus_B0": delta_stats("B3_minus_B0"),
        "sample_note": "n<=18 closed-ish pairs; descriptive only, not statistically significant",
    }

    # path attribution
    path_rows = []
    for c in cycles:
        path_rows.append(
            {
                "trade_id": c["trade_id"],
                "path_tag": c["baseline_path_tag"],
                "B0_net": c["arms"]["B0"]["net_u"] if c["arms"]["B0"]["closed"] else "OPEN",
                "B1_delta": c["arms"]["B1"].get("delta_net_vs_B0"),
                "B2_delta": c["arms"]["B2"].get("delta_net_vs_B0"),
                "B3_delta": c["arms"]["B3"].get("delta_net_vs_B0"),
            }
        )

    # data gaps
    gaps = [
        {
            "id": "G1",
            "item": "per_arm_equity_curve",
            "status": "MISSING",
            "impact": "drawdown/exposure integrals UNKNOWN in paired mode",
        },
        {
            "id": "G2",
            "item": "tick_quote_full_coverage_for_counterfactual_exits",
            "status": "PARTIAL",
            "impact": "paired uses actual exit marks from baseline fills; cannot invent marks after archive stop",
        },
        {
            "id": "G3",
            "item": "intrabar_order_1m",
            "status": "UNKNOWN",
            "impact": "OHLC cannot order add/R2/TP/STOP inside same bar; paired follows recorded fill timestamps",
        },
        {
            "id": "G4",
            "item": "funding_history",
            "status": "UNKNOWN",
            "impact": "funding_data_gap=True on baseline",
        },
        {
            "id": "G5",
            "item": "full_event_driven_independent_arms",
            "status": "NOT_RUN",
            "impact": "requires complete signal+quote stream isolation; deferred — paired path only delivered",
        },
        {
            "id": "G6",
            "item": "P0_partial_fill_anchor_definition",
            "status": "NO_PARTIAL_P0_IN_BASELINE",
            "impact": "if future partial P0 appears, treat as blocker until definition confirmed",
        },
    ]

    # baseline reproduce check
    b0_nets = [c["arms"]["B0"]["net_u"] for c in cycles if c["arms"]["B0"]["closed"]]
    # Include open realized? Baseline account delta -20.57 includes open cycle partial realized.
    # Paired B0 closed-only should approximate cycle csv -26.46 for 14 closed.
    baseline_check = {
        "paired_B0_closed_net_sum": sum(b0_nets),
        "expected_approx_from_prior_review_closed_net": -26.46,
        "abs_diff": abs(sum(b0_nets) - (-26.462859)),
        "reproduce_closed_ok": abs(sum(b0_nets) - (-26.462859)) < 0.05,
        "note": "B0 paired uses same fills; small diffs if layer mapping edge cases",
    }

    # top improve/worsen for B1/B2/B3
    def top_delta(arm_key: str, n: int = 5):
        rows = []
        for r in paired_rows:
            v = r[arm_key]
            if isinstance(v, (int, float)):
                rows.append(r)
        best = sorted(rows, key=lambda x: float(x[arm_key]), reverse=True)[:n]
        worst = sorted(rows, key=lambda x: float(x[arm_key]))[:n]
        return best, worst

    artifacts = OUT / "artifacts"
    artifacts.mkdir(exist_ok=True)

    def wcsv(path: Path, rows: list[dict]):
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys = list(rows[0].keys())
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})

    # flatten paired for csv
    wcsv(OUT / "paired_cycles.csv", paired_rows)
    wcsv(OUT / "arm_metrics.csv", arm_metrics)
    wcsv(OUT / "path_attribution.csv", path_rows)
    wcsv(OUT / "data_gaps.csv", gaps)
    (artifacts / "paired_cycles_full.json").write_text(json.dumps(cycles, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "paired_summary.json").write_text(json.dumps({"baseline_check": baseline_check, "paired_summary": paired_summary}, ensure_ascii=False, indent=2), encoding="utf-8")

    tops = {}
    for k in ("B1_minus_B0", "B2_minus_B0", "B3_minus_B0"):
        b, w = top_delta(k)
        tops[k] = {
            "best5": [{"trade_id": r["trade_id"], "delta": r[k], "path": r["path_tag"]} for r in b],
            "worst5": [{"trade_id": r["trade_id"], "delta": r[k], "path": r["path_tag"]} for r in w],
        }
    (artifacts / "top_deltas.json").write_text(json.dumps(tops, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "n_cycles": len(cycles), "baseline_check": baseline_check, "paired_summary": paired_summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
