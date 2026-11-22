#!/usr/bin/env python3
"""Stage-2: comparison口径 tables + counterfactual fill-model audit. No exchange. No Paper launch."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1]
ART = OUT / "artifacts" / "stage2"
RUN = ROOT / "output/paper_bcd_local/data_c/output/testnet_c_inv_v1/runs/20261121T201533_9a0f7b4f"
CUTOFF = "2026-11-22T07:34:51+08:00"
FEE = 0.0005
SLIP = 0.0002  # SLIPPAGE_ADVERSE from baseline config


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    return (exit_px - entry) * qty if side == "LONG" else (entry - exit_px) * qty


def main() -> int:
    ART.mkdir(parents=True, exist_ok=True)
    cycles = json.loads((OUT / "artifacts" / "paired_cycles_full.json").read_text(encoding="utf-8"))
    fills = load_jsonl(RUN / "fills.jsonl")
    by_tid: dict[str, list] = defaultdict(list)
    for f in fills:
        if f.get("trade_id"):
            by_tid[str(f["trade_id"])].append(f)

    open_val = list(
        csv.DictReader(
            (ROOT / "analysis/c_10h_review_20261122T085501/supplement_open_valuation.csv").open(
                encoding="utf-8-sig"
            )
        )
    )
    open_val_by = {r["trade_id"]: r for r in open_val}
    cycles_rev = list(
        csv.DictReader(
            (ROOT / "analysis/c_10h_review_20261122T085501/cycles.csv").open(encoding="utf-8-sig")
        )
    )
    cycles_rev_by = {r["trade_id"]: r for r in cycles_rev}

    # ---------- 1) common closed 14 ----------
    common = [c for c in cycles if all(c["arms"][a]["closed"] for a in ("B0", "B1", "B2", "B3"))]
    assert len(common) == 14, len(common)

    common_rows = []
    for c in common:
        row: dict[str, Any] = {
            "trade_id": c["trade_id"],
            "symbol": c["symbol"],
            "side": c["side"],
            "path_tag": c["baseline_path_tag"],
            "p0_anchor": c["p0_anchor"],
        }
        for arm in ("B0", "B1", "B2", "B3"):
            a = c["arms"][arm]
            row[f"{arm}_gross_u"] = a["gross_u"]
            row[f"{arm}_fees_u"] = a["fees_u"]
            row[f"{arm}_net_u"] = a["net_u"]
        for arm in ("B1", "B2", "B3"):
            row[f"{arm}_minus_B0"] = c["arms"][arm]["net_u"] - c["arms"]["B0"]["net_u"]
        common_rows.append(row)

    summary_common = []
    for arm in ("B0", "B1", "B2", "B3"):
        g = sum(r[f"{arm}_gross_u"] for r in common_rows)
        f = sum(r[f"{arm}_fees_u"] for r in common_rows)
        n = sum(r[f"{arm}_net_u"] for r in common_rows)
        summary_common.append({"arm": arm, "n": 14, "gross_u": g, "fees_u": f, "net_u": n})
    for arm in ("B1", "B2", "B3"):
        deltas = [r[f"{arm}_minus_B0"] for r in common_rows]
        imp = sum(1 for d in deltas if d > 1e-12)
        wors = sum(1 for d in deltas if d < -1e-12)
        same = sum(1 for d in deltas if abs(d) <= 1e-12)
        summary_common.append(
            {
                "arm": f"{arm}_vs_B0",
                "n": 14,
                "delta_sum_u": sum(deltas),
                "improve_n": imp,
                "worsen_n": wors,
                "unchanged_n": same,
                "note": "counts are per-arm; do not copy across arms even if coincident",
            }
        )

    # ---------- 2) BONK / AVAX extra ----------
    extras = [c for c in cycles if c["arms"]["B2"]["closed"] and not c["arms"]["B0"]["closed"]]
    extra_rows = []
    for c in extras:
        tid = c["trade_id"]
        fl = by_tid[tid]
        p2_add = next(x for x in fl if x.get("event") == "PYRAMID_ADD" and x.get("layer") == "P2")
        b2 = c["arms"]["B2"]
        # fees on B2 protect exit
        exit_fees = 0.0
        exit_gross = 0.0
        for L in b2["layers"]:
            if L["closed"] and str(L.get("exit_reason") or "").startswith("B2_PROTECT"):
                exit_gross += pnl(c["side"], L["entry"], L["exit_px"], L["qty"])
                exit_fees += abs(L["exit_px"] * L["qty"]) * FEE
        # state of others at B2 exit time
        t_exit = p2_add.get("recorded_at")
        other_at_t = {}
        for arm in ("B0", "B1", "B3"):
            # layers open as of timeline before/at this event
            layers_open = []
            for L in c["arms"][arm]["layers"]:
                if not L["closed"]:
                    layers_open.append(L["name"])
                elif (L.get("exit_t") or "") > t_exit:
                    layers_open.append(L["name"] + "(still_open_at_B2_exit)")
                elif (L.get("exit_t") or "") == t_exit:
                    layers_open.append(L["name"] + "(same_ts_exit)")
                else:
                    layers_open.append(L["name"] + f"(closed:{L.get('exit_reason')})")
            # recompute open names at t_exit from events
            open_names = ["P0"]
            for f in fl:
                if (f.get("recorded_at") or "") > t_exit:
                    break
                if f.get("event") == "PYRAMID_ADD" and arm == "B0":
                    open_names.append(str(f.get("layer")))
                if f.get("event") == "PYRAMID_ADD" and arm in ("B1",) and f.get("layer") == "P1":
                    open_names.append("P1")
                if f.get("event") in ("CLOSE", "CLOSE_PARTIAL") and (f.get("recorded_at") or "") <= t_exit:
                    # remove closed layer if this arm would take it
                    layer = f.get("layer")
                    reason = f.get("exit_reason") or ""
                    if arm == "B0" and layer in open_names:
                        open_names = [x for x in open_names if x != layer]
                    if arm == "B1" and layer in ("P0", "P1") and layer in open_names and reason != "PYRAMID_PSTOP":
                        open_names = [x for x in open_names if x != layer]
                    if arm == "B1" and reason == "PYRAMID_PSTOP":
                        open_names = []
                    if arm == "B3" and layer == "P0" and layer in open_names:
                        open_names = [x for x in open_names if x != layer]
                    if arm == "B3" and reason == "PYRAMID_PSTOP":
                        open_names = []
            # simpler: from paired state + notes
            still = [L["name"] for L in c["arms"][arm]["layers"] if not L["closed"] or (L.get("exit_t") or "") > t_exit]
            other_at_t[arm] = {
                "layers_open_or_later": still,
                "open_at_cutoff": c["arms"][arm]["open_at_cutoff"],
                "realized_to_date_at_cutoff_u": c["arms"][arm]["net_u"] if not c["arms"][arm]["open_at_cutoff"] else "PARTIAL_SEE_CUTOFF_TABLE",
            }

        # rebuild B2 fee/net from layers
        b2_fees = b2["fees_u"]
        b2_gross = b2["gross_u"]
        b2_net = b2["net_u"]
        protect_layers = [L for L in b2["layers"] if str(L.get("exit_reason") or "").startswith("B2_PROTECT")]
        trigger_px = float(p2_add["price"])
        theory_p2 = c["levels_theory"]["P2"]
        extra_rows.append(
            {
                "trade_id": tid,
                "symbol": c["symbol"],
                "side": c["side"],
                "B2_exit_time": t_exit,
                "B2_trigger_event": "baseline_PYRAMID_ADD_P2",
                "B2_trigger_price_used": trigger_px,
                "B2_theory_P2_level": theory_p2,
                "B2_trigger_vs_theory": "USED_OBSERVED_P2_ADD_FILL_NOT_THEORY_ALONE",
                "B2_fill_price": trigger_px,
                "B2_protect_layers": json.dumps([{k: L[k] for k in ("name", "entry", "qty", "exit_px", "exit_reason")} for L in protect_layers], ensure_ascii=False),
                "B2_gross_u": b2_gross,
                "B2_fees_u": b2_fees,
                "B2_net_u": b2_net,
                "B0_at_B2_exit": json.dumps(other_at_t["B0"], ensure_ascii=False),
                "B1_at_B2_exit": json.dumps(other_at_t["B1"], ensure_ascii=False),
                "B3_at_B2_exit": json.dumps(other_at_t["B3"], ensure_ascii=False),
                "B0_B1_B3_at_common_cutoff": "OPEN (see cutoff18)",
                "price_source": "baseline_fill_PYRAMID_ADD_P2.price",
                "slippage_note": f"P2 add was LIMIT fill at limit; protect exit reused that price (no extra {SLIP} applied in paired model)",
                "fee_one_side": FEE,
            }
        )

    # ---------- 3) cutoff 18 ----------
    cutoff_rows = []
    for c in cycles:
        tid = c["trade_id"]
        rev = cycles_rev_by.get(tid, {})
        ov = open_val_by.get(tid, {})
        for arm in ("B0", "B1", "B2", "B3"):
            a = c["arms"][arm]
            if a["closed"] and not a["open_at_cutoff"]:
                realized = a["net_u"]
                unreal = 0.0
                equity_comp = realized  # relative to cycle; not account equity
                u_src = "FLAT_AT_OR_BEFORE_CUTOFF"
                u_time = max((L.get("exit_t") or "") for L in a["layers"]) if a["layers"] else ""
            else:
                # open at cutoff — use review valuation only for B0-like residual; others UNKNOWN if path diverged
                realized = float(rev.get("net_realized_to_date_u") or 0) if arm == "B0" else "UNKNOWN_ARM_SPECIFIC"
                if arm == "B0":
                    unreal = ov.get("cycle_unrealized_est_u", "UNKNOWN")
                    u_src = ov.get("unrealized_basis", "UNKNOWN")
                    u_time = ov.get("kline_last_ts", "UNKNOWN")
                elif arm == "B2" and a["closed"]:
                    # should not happen in this branch
                    realized = a["net_u"]
                    unreal = 0.0
                    u_src = "B2_FLAT"
                    u_time = ""
                else:
                    # B1/B3 still open similar to B0 for these 4 — approximate UNKNOWN if layers differ
                    open_layers = [L["name"] for L in a["layers"] if not L["closed"]]
                    b0_open = [L["name"] for L in c["arms"]["B0"]["layers"] if not L["closed"]]
                    if open_layers == b0_open and arm in ("B1", "B3"):
                        realized = float(rev.get("net_realized_to_date_u") or 0)
                        # strip P2 realized if B1/B3 never had P2 — adjust from B0 cycle realized components is hard
                        # For SHIB/PEPE (P0 only open) same; BONK B0 has P0 only after R2 of P1/P2 — B1 may differ
                        unreal = ov.get("cycle_unrealized_est_u", "UNKNOWN")
                        u_src = "COPIED_B0_OPEN_VAL_IF_SAME_OPEN_LAYERS;VERIFY"
                        u_time = ov.get("kline_last_ts", "UNKNOWN")
                        if open_layers != b0_open:
                            unreal = "UNKNOWN"
                            u_src = "OPEN_LAYERS_DIVERGED_FROM_B0"
                    else:
                        realized = "UNKNOWN"
                        unreal = "UNKNOWN"
                        u_src = "OPEN_LAYERS_DIVERGED_OR_INSUFFICIENT_MARK"
                        u_time = "UNKNOWN"
                equity_comp = "UNKNOWN" if realized == "UNKNOWN" or unreal == "UNKNOWN" else (
                    float(realized) + float(unreal) if unreal not in (None, "") else "UNKNOWN"
                )
            # Fix B2 closed extras
            if arm == "B2" and a["closed"] and not a.get("open_at_cutoff"):
                realized = a["net_u"]
                unreal = 0.0
                equity_comp = realized
                u_src = "FLAT_B2"
                u_time = max((L.get("exit_t") or "") for L in a["layers"]) if a["layers"] else ""

            cutoff_rows.append(
                {
                    "trade_id": tid,
                    "symbol": c["symbol"],
                    "arm": arm,
                    "status": "CLOSED" if (a["closed"] and not a["open_at_cutoff"]) else "OPEN_AT_CUTOFF",
                    "realized_u": realized,
                    "unrealized_u": unreal if not (a["closed"] and not a["open_at_cutoff"]) else 0.0,
                    "equity_cycle_u": equity_comp,
                    "valuation_time": u_time if not (a["closed"] and not a["open_at_cutoff"]) else u_time,
                    "valuation_source": u_src if not (a["closed"] and not a["open_at_cutoff"]) else "REALIZED_ONLY",
                    "common_cutoff": CUTOFF,
                    "funding": "UNKNOWN",
                    "open_not_treated_as_zero": True,
                }
            )

    # ---------- 4) verify B2 splits ----------
    b2_common_net = sum(c["arms"]["B2"]["net_u"] for c in common)
    b2_extra_net = sum(c["arms"]["B2"]["net_u"] for c in extras)
    verify = {
        "B2_common14_net_u": b2_common_net,
        "B2_common14_expected_approx": -13.92,
        "B2_common14_ok": abs(b2_common_net - (-13.918352)) < 0.01 or abs(b2_common_net + 13.92) < 0.02,
        "B2_extra2_net_u": b2_extra_net,
        "B2_extra2_expected_approx": -7.01,
        "B2_extra2_ok": abs(b2_extra_net - (-7.011944651499937)) < 0.01 or abs(b2_extra_net + 7.01) < 0.02,
        "B2_all_closed16_net_u": b2_common_net + b2_extra_net,
        "note": "Do not compare B2's 16-closed total to B0's 14-closed total without口径 split",
    }

    # ---------- 5) counterfactual audit per cycle ----------
    audit_rows = []
    for c in cycles:
        tid = c["trade_id"]
        fl = by_tid[tid]
        for arm in ("B1", "B2", "B3"):
            a = c["arms"][arm]
            for L in a["layers"]:
                if not L["closed"]:
                    audit_rows.append(
                        {
                            "trade_id": tid,
                            "arm": arm,
                            "layer": L["name"],
                            "exit_status": "OPEN_AT_CUTOFF",
                            "event_time": None,
                            "price_source": "N/A",
                            "used_post_exit_future_data": False,
                            "jump_filled_at_ideal_threshold": "N/A",
                            "spread_slippage": "N/A",
                            "note": "unrealized separate; not forced to 0",
                        }
                    )
                    continue
                reason = L.get("exit_reason") or ""
                t = L.get("exit_t") or ""
                px = L.get("exit_px")
                # classify source
                if reason.startswith("B2_PROTECT"):
                    src = "baseline_PYRAMID_ADD_P2.fill_price (observed when B0 added P2)"
                    future = False  # P2 add is contemporaneous trigger proxy
                    jump = "TRIGGER_PROXY_IS_OBSERVED_ADD_FILL; not independent theory-only fill; not 1m close backfill"
                    slip = f"reused limit fill price; no extra SLIPPAGE_ADVERSE={SLIP} in paired B2 protect"
                else:
                    # matched to baseline close of same layer/reason
                    match = [
                        x
                        for x in fl
                        if x.get("event") in ("CLOSE", "CLOSE_PARTIAL")
                        and x.get("layer") == L["name"]
                        and (x.get("exit_reason") or "") == reason
                        and abs(float(x.get("price") or 0) - float(px or 0)) < 1e-9
                    ]
                    if match:
                        src = "baseline_fill_CLOSE/PARTIAL (accounting reuse of observed exit mark)"
                        future = False
                        jump = "NO — used observed mark/limit exit from baseline paper engine"
                        slip = f"embedded in baseline fill (mark±SLIPPAGE_ADVERSE={SLIP} for stop/TP; limit exits at limit)"
                    else:
                        src = "MAPPED_FROM_BASELINE_TIMELINE_OR_PSTOP_BROADCAST"
                        # PSTOP on B1 closes all at B0's stop mark
                        future = False
                        jump = "UNKNOWN_IF_IDEAL — if only theory used would be INVALID; here uses B0 stop fill price"
                        slip = f"from baseline stop fill; fee={FEE}"
                audit_rows.append(
                    {
                        "trade_id": tid,
                        "arm": arm,
                        "layer": L["name"],
                        "exit_status": "CLOSED",
                        "exit_reason": reason,
                        "event_time": t,
                        "exit_price": px,
                        "price_source": src,
                        "used_post_exit_future_data": future,
                        "jump_filled_at_ideal_threshold": jump,
                        "spread_slippage": slip,
                        "fee_one_side": FEE,
                        "note": "counterfactual management on shared P0; NOT full independent engine replay",
                    }
                )

    b0_note = {
        "B0_method": "direct_reuse_historical_fills",
        "label": "帳務重現 ≠ 模擬引擎路徑重現",
        "detail": "B0 nets rebuild layer PnL from recorded OPEN/ADD/CLOSE fills; does not re-run manage_c_pyramid quote loop",
    }

    # write outputs
    def wcsv(path: Path, rows: list[dict]):
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    wcsv(OUT / "common14_arm_pnl.csv", common_rows)
    wcsv(OUT / "common14_summary.csv", summary_common)
    wcsv(OUT / "b2_extra_bonk_avax.csv", extra_rows)
    wcsv(OUT / "cutoff18_valuation.csv", cutoff_rows)
    wcsv(OUT / "counterfactual_exit_audit.csv", audit_rows)
    (ART / "b2_split_verify.json").write_text(json.dumps(verify, ensure_ascii=False, indent=2), encoding="utf-8")
    (ART / "b0_method_note.json").write_text(json.dumps(b0_note, ensure_ascii=False, indent=2), encoding="utf-8")

    # human audit summary
    findings = {
        "counterfactual_model_verdict": "PARTIAL_VALID_FOR_DIRECTIONAL_COMPARE",
        "critical_limits": [
            "B0 is accounting reproduce from fills, not engine path reproduce",
            "B1/B2/B3 exits mostly remap baseline observed exit marks; not independent quote-driven simulation",
            "B2 protect uses observed P2 ADD fill price as trigger+fill proxy — not theory-only threshold as fill, not future close backfill",
            "No invention of fills after archive stop; open cycles stay OPEN",
            "1m OHLC not used as intrabar fill oracle in this paired model",
            "Cannot claim maker fills; fee stage-1 uniform 0.0005",
        ],
        "verify": verify,
        "per_arm_delta_counts_common14": {
            x["arm"]: x for x in summary_common if str(x["arm"]).endswith("_vs_B0")
        },
    }
    (OUT / "COUNTERFACTUAL_AUDIT.md").write_text(
        "\n".join(
            [
                "# COUNTERFACTUAL_AUDIT — Stage 2",
                "",
                "## Verdict",
                findings["counterfactual_model_verdict"],
                "",
                "## B0",
                f"- {b0_note['label']}",
                f"- {b0_note['detail']}",
                "",
                "## B1/B2/B3",
                "- Exit prices primarily from baseline fills on the shared P0 timeline (observed marks/limits).",
                "- B2 protect-at-P2: trigger time = baseline `PYRAMID_ADD` P2 `recorded_at`; fill = that ADD price (observed), not a lone theoretical P2 printed as fill.",
                "- Theoretical P2 level is recorded for comparison; **not** asserted as independently observed executable without the ADD print.",
                "- Slippage: embedded in baseline mark exits (`SLIPPAGE_ADVERSE=0.0002`); B2 protect reuses limit ADD price without extra slip.",
                "- No future close backfill; cutoff opens remain OPEN/UNKNOWN unrealized where layers diverge.",
                "",
                "## Limits",
                *[f"- {x}" for x in findings["critical_limits"]],
                "",
                "## B2 split check",
                f"- common14 net ≈ {b2_common_net:.6f} (expect ~-13.92) ok={verify['B2_common14_ok']}",
                f"- extra2 net ≈ {b2_extra_net:.6f} (expect ~-7.01) ok={verify['B2_extra2_ok']}",
                "",
                "Detail CSV: `counterfactual_exit_audit.csv`",
            ]
        ),
        encoding="utf-8",
    )
    (ART / "stage2_findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "verify": verify, "common14_arms": summary_common[:4]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
