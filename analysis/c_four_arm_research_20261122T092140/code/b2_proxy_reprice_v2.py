#!/usr/bin/env python3
"""Stage3 v2: B2 historical protect-exit price credibility repair (no Paper claim)."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parents[1]
RUN = ROOT / "output/paper_bcd_local/data_c/output/testnet_c_inv_v1/runs/20261121T201533_9a0f7b4f"
FEE = 0.0005
SLIP = 0.0002  # SLIPPAGE_ADVERSE; ADD limit itself has NO exit slip


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    return (exit_px - entry) * qty if side == "LONG" else (entry - exit_px) * qty


def protect_mark(side: str, bid: float, ask: float) -> float:
    """LONG exit uses bid; SHORT exit uses ask; then adverse slip (not already in ADD limit)."""
    if side == "LONG":
        return float(bid) * (1.0 - SLIP)
    return float(ask) * (1.0 + SLIP)


def main() -> int:
    quotes = [
        json.loads(l)
        for l in (RUN / "quote_events.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    extras = list(csv.DictReader((OUT / "b2_extra_bonk_avax.csv").open(encoding="utf-8")))
    cycles = json.loads((OUT / "artifacts" / "paired_cycles_full.json").read_text(encoding="utf-8"))
    by_tid = {c["trade_id"]: c for c in cycles}

    rows = []
    for ex in extras:
        tid = ex["trade_id"]
        side = ex["side"]
        t_exit = ex["B2_exit_time"]
        proxy_px = float(ex["B2_fill_price"])
        sym = ex["symbol"]
        tt = parse_iso(t_exit)
        best = None
        for q in quotes:
            if q.get("symbol") != sym:
                continue
            try:
                dt = parse_iso(q["recorded_at"])
            except Exception:
                continue
            d = abs((dt - tt).total_seconds())
            if best is None or d < best[0]:
                best = (d, q)
        bid = ask = None
        quote_age = None
        quote_src = None
        if best and best[0] <= 5.0 and best[1].get("bid") and best[1].get("ask"):
            bid = float(best[1]["bid"])
            ask = float(best[1]["ask"])
            quote_age = best[0]
            quote_src = f"{best[1].get('purpose')}:{best[1].get('recorded_at')}"

        c = by_tid[tid]
        b2 = c["arms"]["B2"]
        # rebuild protect layers only
        proxy_gross = proxy_fees = 0.0
        mark_gross = mark_fees = None
        mark_px = None
        for L in b2["layers"]:
            if not L.get("closed"):
                continue
            reason = str(L.get("exit_reason") or "")
            if reason.startswith("B2_PROTECT"):
                g = pnl(side, float(L["entry"]), float(L["exit_px"]), float(L["qty"]))
                f = abs(float(L["exit_px"]) * float(L["qty"])) * FEE + abs(float(L["entry"]) * float(L["qty"])) * FEE
                # layer fees in paired model counted entry+exit in finalize; here isolate protect exit delta
                proxy_gross += g
            elif reason == "PYRAMID_R2_REBOUND":
                pass  # keep R2 as-is from observed baseline fill

        # Full arm net under proxy label (original)
        net_proxy = float(b2["net_u"])
        gross_proxy = float(b2["gross_u"])
        fees_proxy = float(b2["fees_u"])

        net_bidask = "UNKNOWN"
        gross_bidask = "UNKNOWN"
        fees_bidask = "UNKNOWN"
        if bid is not None and ask is not None:
            mark_px = protect_mark(side, bid, ask)
            # recompute whole B2 arm: non-protect layers keep recorded exits; protect layers use mark_px
            g = f = 0.0
            for L in b2["layers"]:
                entry = float(L["entry"])
                qty = float(L["qty"])
                f += abs(entry * qty) * FEE
                if not L.get("closed"):
                    continue
                reason = str(L.get("exit_reason") or "")
                if reason.startswith("B2_PROTECT"):
                    ex_px = mark_px
                else:
                    ex_px = float(L["exit_px"])
                g += pnl(side, entry, ex_px, qty)
                f += abs(ex_px * qty) * FEE
            gross_bidask = g
            fees_bidask = f
            net_bidask = g - f

        rows.append(
            {
                "trade_id": tid,
                "symbol": sym,
                "side": side,
                "proxy_trigger_time": t_exit,
                "proxy_trigger_note": "baseline_P2_ADD_time_is_proxy_not_proven_independent_B2_first_touch",
                "counterfactual_add_price_proxy": proxy_px,
                "proxy_label": "counterfactual_add_price_proxy",
                "proxy_is_validated_executable_protect_exit": False,
                "proxy_gross_u": gross_proxy,
                "proxy_fees_u": fees_proxy,
                "proxy_net_u": net_proxy,
                "quote_bid": bid if bid is not None else "UNKNOWN",
                "quote_ask": ask if ask is not None else "UNKNOWN",
                "quote_age_sec": quote_age if quote_age is not None else "UNKNOWN",
                "quote_source": quote_src or "UNKNOWN",
                "bidask_exit_price_with_slip": mark_px if mark_px is not None else "UNKNOWN",
                "slip_model": f"LONG:bid*(1-{SLIP}); SHORT:ask*(1+{SLIP}); ADD limit had no exit slip — not double-counted",
                "bidask_gross_u": gross_bidask,
                "bidask_fees_u": fees_bidask,
                "bidask_net_u": net_bidask,
                "sensitivity_not_empirical": True,
                "do_not_claim_B2_winner_from_proxy": True,
            }
        )

    art = OUT / "artifacts" / "stage3"
    art.mkdir(parents=True, exist_ok=True)
    path = OUT / "b2_extra_reprice_v2.csv"
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    cred = {
        "B0": {
            "grade": "ACCOUNTING_REPRODUCE",
            "note": "historical fills reuse; not engine-path reproduce",
        },
        "B1": {
            "grade": "COUNTERFACTUAL_REMAP_OBSERVED_EXITS",
            "note": "management remap on baseline exit marks",
        },
        "B2": {
            "grade": "COUNTERFACTUAL_ADD_PRICE_PROXY_PLUS_OPTIONAL_BIDASK",
            "note": (
                "protect exit originally used opposite-side ADD limit as price proxy; "
                "v2 keeps proxy labeled; bid/ask+slip scenario separate; "
                "proxy trigger time ≠ proven independent first touch; "
                "FORBIDDEN to claim B2 winner from these historical numbers"
            ),
            "proxy_extra2_net_u": sum(float(r["proxy_net_u"]) for r in rows),
            "bidask_extra2_net_u": sum(
                float(r["bidask_net_u"]) for r in rows if r["bidask_net_u"] != "UNKNOWN"
            ),
        },
        "B3": {
            "grade": "COUNTERFACTUAL_REMAP_OBSERVED_EXITS",
            "note": "P0-only remap on baseline P0 exits/stops",
        },
        "overall_single_verdict_forbidden": True,
        "forward_paper_not_blocked_by_historical_gap": True,
    }
    (OUT / "COUNTERFACTUAL_CREDIBILITY_V2.md").write_text(
        "\n".join(
            [
                "# COUNTERFACTUAL_CREDIBILITY_V2",
                "",
                "## Per-arm grades (no single Verdict)",
                *[f"- **{k}**: `{v['grade']}` — {v['note']}" for k, v in cred.items() if k.startswith("B")],
                "",
                "## B2 price proxy",
                "- Original protect fill = baseline P2 ADD limit → `counterfactual_add_price_proxy`.",
                "- That ADD is paper fill evidence, not exchange evidence, and is opposite liquidity side vs protect exit.",
                "- Where contemporaneous bid/ask exist (±5s), LONG exit = bid×(1−slip), SHORT = ask×(1+slip).",
                "- ADD limit had no exit slip embedded → slip applied once on bid/ask path.",
                "- Proxy trigger time is baseline ADD time only — not proven equal to independent B2 first touch.",
                "- **Do not claim B2 wins from historical paired numbers.**",
                "",
                f"- proxy extra2 net ≈ {cred['B2']['proxy_extra2_net_u']:.6f}",
                f"- bidask+slip extra2 net ≈ {cred['B2']['bidask_extra2_net_u']:.6f}",
            ]
        ),
        encoding="utf-8",
    )
    (art / "credibility_v2.json").write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "rows": len(rows), "cred_B2": cred["B2"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
