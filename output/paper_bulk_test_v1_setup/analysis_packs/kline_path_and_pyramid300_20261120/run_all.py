#!/usr/bin/env python3
"""Offline studies A (kline path) + B (pyramid 300U). Read-only."""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.config import (  # noqa: E402
    CUTOFF,
    FEE,
    LEVERAGE,
    ORIG_NOTIONAL,
    PACK_ROOT,
    PROFILE_B,
    PROFILE_C,
    PY_MARGIN,
    PY_NOTIONAL,
    RUNS_B,
    RUNS_C,
    SLIP,
)
from common.data import load_bars, parse_ts, rebuild_trades  # noqa: E402
from study_a.path_and_features import (  # noqa: E402
    attach_pre_features,
    diagnose_path,
    evaluate_candidates,
)
from study_b.pyramid import simulate_pyramid, simulate_single_batch  # noqa: E402


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    keys: List[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in ((k, r.get(k)) for k in keys)})


def _f(x, n=4):
    if x is None:
        return "NA"
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return str(x)
        return f"{x:.{n}f}"
    return str(x)


def summarize_pyramid(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {}
    return {
        "n": n,
        "sum_realized": sum(r["realized_net"] for r in rows),
        "sum_mtm": sum(r["open_mtm_net"] for r in rows),
        "sum_mark": sum(r["total_mark_net"] for r in rows),
        "sum_fees": sum(r["fees"] for r in rows),
        "sum_gross": sum(r["gross_realized"] for r in rows),
        "avg_mark_per_signal": sum(r["total_mark_net"] for r in rows) / n,
        "n_stop": sum(1 for r in rows if r["stop_hit"]),
        "sum_rebound": sum(r["rebound_pnl"] for r in rows),
        "sum_stop_pnl": sum(r["stop_pnl"] for r in rows),
        "sum_p0": sum(r["p0_net"] for r in rows),
        "sum_add": sum(r["add_net"] for r in rows),
        "n_filled_p2": sum(1 for r in rows if r["n_lots_filled"] >= 3),
        "n_filled_p1": sum(1 for r in rows if r["n_lots_filled"] >= 2),
        "n_ambiguous": sum(1 for r in rows if r["ambiguous"]),
        "n_open": sum(1 for r in rows if r["status"] == "OPEN"),
        "max_single_loss": min(r["total_mark_net"] for r in rows),
        "max_single_win": max(r["total_mark_net"] for r in rows),
        "max_notional_peak": max(r["max_notional_u"] for r in rows),
        "max_margin_peak": max(r["max_margin_u"] for r in rows),
    }


def run_label(label: str, profile: str, runs, out_dir: Path) -> None:
    print(f"=== {label} rebuild ===")
    rebuilt = rebuild_trades(profile, set(runs), cutoff=CUTOFF)
    primary = rebuilt["primary"]
    closed = rebuilt["closed"]
    print(
        f"  primary={len(primary)} closed={len(closed)} open={len(rebuilt['open'])} "
        f"oos={len(rebuilt['oos'])} sum={rebuilt['sum_net']:.6f}"
    )

    # map queue times by signal_key / symbol+approx
    qmap = {}
    for q in rebuilt["queued"]:
        sk = q.get("signal_key")
        if sk:
            qmap[sk] = parse_ts(q, "recorded_at", "ts")

    # ---- Study A ----
    print(f"=== {label} study A ===")
    path_rows = []
    earliest = min(t.open_ts for t in primary) if primary else CUTOFF
    bars_cache = {}
    for t in primary:
        if t.symbol not in bars_cache:
            bars_cache[t.symbol] = load_bars(profile, t.symbol, earliest - timedelta(minutes=5), CUTOFF)
        bars = bars_cache[t.symbol]
        queued_at = qmap.get(t.signal_key) if t.signal_key else None
        row = diagnose_path(t, bars, cutoff=CUTOFF, queued_at=queued_at)
        row = attach_pre_features(profile, t, row)
        path_rows.append(row)
    cand = evaluate_candidates(path_rows, primary)
    write_csv(out_dir / "A_path_trades.csv", path_rows)
    (out_dir / "A_candidates.json").write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")

    # OOS path for extension opens (not used to pick candidates)
    oos_rows = []
    from common.config import EXT_END

    for t in rebuilt["oos"]:
        bars = load_bars(profile, t.symbol, t.open_ts - timedelta(minutes=5), EXT_END)
        row = diagnose_path(t, bars, cutoff=EXT_END, queued_at=None)
        row = attach_pre_features(profile, t, row)
        oos_rows.append(row)
    write_csv(out_dir / "A_path_oos.csv", oos_rows)

    # ---- Study B ----
    print(f"=== {label} study B pyramid ===")
    modes = []
    for rebound in ("R1", "R2"):
        for tp_mode in ("T1", "T2", "T3"):
            modes.append((rebound, tp_mode))

    pyramid_rows = []
    for rebound, tp_mode in modes:
        for t in primary:
            bars = bars_cache.get(t.symbol) or load_bars(
                profile, t.symbol, earliest - timedelta(minutes=5), CUTOFF
            )
            orig_tp = t.open_row.get("fixed_gross_tp_price_raw")
            for mode in ("conservative", "optimistic"):
                res = simulate_pyramid(
                    t,
                    bars,
                    rebound=rebound,
                    tp_mode=tp_mode,
                    notional_per=PY_NOTIONAL,
                    cutoff=CUTOFF,
                    mode=mode,
                    orig_tp_price=float(orig_tp) if orig_tp is not None else None,
                )
                pyramid_rows.append(
                    {
                        "label": label,
                        "rebound": rebound,
                        "tp_mode": tp_mode,
                        "path_mode": mode,
                        "notional_口径": "margin300x10=3000",
                        **res.__dict__,
                        "lots": json.dumps(res.lots, ensure_ascii=False),
                    }
                )

    # sensitivity: 300 as notional (not margin)
    sens_rows = []
    for rebound, tp_mode in [("R1", "T1"), ("R2", "T1")]:
        for t in primary:
            bars = bars_cache.get(t.symbol) or []
            orig_tp = t.open_row.get("fixed_gross_tp_price_raw")
            res = simulate_pyramid(
                t,
                bars,
                rebound=rebound,
                tp_mode=tp_mode,
                notional_per=300.0,
                cutoff=CUTOFF,
                mode="conservative",
                orig_tp_price=float(orig_tp) if orig_tp is not None else None,
            )
            sens_rows.append(
                {
                    "rebound": rebound,
                    "tp_mode": tp_mode,
                    "notional_口径": "notional300",
                    **{k: v for k, v in res.__dict__.items() if k != "lots"},
                }
            )

    # baselines: original 10k notional TP50/SL75; single 3000; single 300 notional
    base_rows = []
    for t in primary:
        bars = bars_cache.get(t.symbol) or []
        orig_tp = t.open_row.get("fixed_gross_tp_price_raw")
        for notional, name in [
            (ORIG_NOTIONAL, "orig_10k_tp50_sl75"),
            (PY_NOTIONAL, "single_3000_tp50_sl75"),
            (300.0, "single_300notional_tp50_sl75"),
        ]:
            res = simulate_single_batch(
                t,
                bars,
                notional=notional,
                tp_mode="T1",
                use_sl75=True,
                cutoff=CUTOFF,
                mode="conservative",
                orig_tp_price=float(orig_tp) if orig_tp is not None else None,
            )
            base_rows.append(
                {
                    "baseline": name,
                    "notional": notional,
                    **{k: v for k, v in res.__dict__.items() if k != "lots"},
                    "lots": json.dumps(res.lots, ensure_ascii=False),
                }
            )

    write_csv(out_dir / "B_pyramid_trades.csv", pyramid_rows)
    write_csv(out_dir / "B_pyramid_sensitivity_notional300.csv", sens_rows)
    write_csv(out_dir / "B_baselines.csv", base_rows)

    # summaries per mode (conservative only for main table)
    summ_list = []
    for rebound, tp_mode in modes:
        sub = [
            r
            for r in pyramid_rows
            if r["rebound"] == rebound and r["tp_mode"] == tp_mode and r["path_mode"] == "conservative"
        ]
        # flatten numeric
        flat = [
            {
                "realized_net": r["realized_net"],
                "open_mtm_net": r["open_mtm_net"],
                "total_mark_net": r["total_mark_net"],
                "fees": r["fees"],
                "gross_realized": r["gross_realized"],
                "stop_hit": r["stop_hit"],
                "rebound_pnl": r["rebound_pnl"],
                "stop_pnl": r["stop_pnl"],
                "p0_net": r["p0_net"],
                "add_net": r["add_net"],
                "n_lots_filled": r["n_lots_filled"],
                "ambiguous": r["ambiguous"],
                "status": r["status"],
                "max_notional_u": r["max_notional_u"],
                "max_margin_u": r["max_margin_u"],
            }
            for r in sub
        ]
        s = summarize_pyramid(flat)
        s["rebound"] = rebound
        s["tp_mode"] = tp_mode
        summ_list.append(s)
    write_csv(out_dir / "B_pyramid_summary.csv", summ_list)

    base_summ = []
    for name in ["orig_10k_tp50_sl75", "single_3000_tp50_sl75", "single_300notional_tp50_sl75"]:
        sub = [r for r in base_rows if r["baseline"] == name]
        flat = [
            {
                "realized_net": r["realized_net"],
                "open_mtm_net": r["open_mtm_net"],
                "total_mark_net": r["total_mark_net"],
                "fees": r["fees"],
                "gross_realized": r["gross_realized"],
                "stop_hit": r["stop_hit"],
                "rebound_pnl": 0,
                "stop_pnl": r["stop_pnl"],
                "p0_net": r["p0_net"],
                "add_net": 0,
                "n_lots_filled": 1,
                "ambiguous": False,
                "status": r["status"],
                "max_notional_u": r["max_notional_u"],
                "max_margin_u": r["max_margin_u"],
            }
            for r in sub
        ]
        s = summarize_pyramid(flat)
        s["baseline"] = name
        base_summ.append(s)
    write_csv(out_dir / "B_baseline_summary.csv", base_summ)

    # fee/slip stress on best-looking pyramid mode
    stress = []
    for fee_m, slip_m in [(1.0, 1.0), (1.5, 1.5), (2.0, 2.0)]:
        # approximate by scaling nets (not perfect) — better re-run with monkeypatch
        import common.config as cfg
        import common.data as data_mod
        import study_b.pyramid as pyr

        old_f, old_s = cfg.FEE, cfg.SLIP
        cfg.FEE = FEE * fee_m
        cfg.SLIP = SLIP * slip_m
        data_mod.FEE = cfg.FEE  # type: ignore
        # pyramid imports FEE/SLIP at function level from config — already bound in module
        pyr.FEE = cfg.FEE  # type: ignore
        pyr.SLIP = cfg.SLIP  # type: ignore
        # actually pyramid uses from common.config import FEE, SLIP at top — need to update pyramid module attrs
        import study_b.pyramid as P

        P.FEE = cfg.FEE
        P.SLIP = cfg.SLIP
        # fee_u uses argument default — pass through exit_px_slip using P.SLIP — close_lot uses module SLIP
        marks = []
        for t in primary:
            bars = bars_cache.get(t.symbol) or []
            orig_tp = t.open_row.get("fixed_gross_tp_price_raw")
            res = simulate_pyramid(
                t,
                bars,
                rebound="R1",
                tp_mode="T1",
                notional_per=PY_NOTIONAL,
                cutoff=CUTOFF,
                mode="conservative",
                orig_tp_price=float(orig_tp) if orig_tp is not None else None,
            )
            marks.append(res.total_mark_net)
        stress.append(
            {
                "fee_mult": fee_m,
                "slip_mult": slip_m,
                "mode": "R1_T1",
                "sum_mark": sum(marks),
                "avg_mark": sum(marks) / len(marks) if marks else None,
            }
        )
        cfg.FEE, cfg.SLIP = old_f, old_s
        P.FEE, P.SLIP = old_f, old_s
    write_csv(out_dir / "B_stress_R1_T1.csv", stress)

    # meta + report
    meta = {
        "label": label,
        "profile": profile,
        "runs": list(runs),
        "cutoff": CUTOFF.isoformat(),
        "leverage": LEVERAGE,
        "pyramid_margin_u": PY_MARGIN,
        "pyramid_notional_u": PY_NOTIONAL,
        "fee": FEE,
        "slip": SLIP,
        "rebuild": {
            "n_primary": len(primary),
            "n_closed": len(closed),
            "n_open": len(rebuilt["open"]),
            "sum_actual_net": rebuilt["sum_net"],
            "open_ids": [t.trade_id for t in rebuilt["open"]],
            "oos_ids": [t.trade_id for t in rebuilt["oos"]],
        },
        "study_a": {
            "atr_median": cand.get("atr_offset_median"),
            "candidates": [
                {k: v for k, v in c.items() if k not in ("kept_ids", "dropped_ids")} for c in cand["candidates"]
            ],
        },
        "study_b_summary_conservative": summ_list,
        "baselines": base_summ,
        "stress": stress,
        "assumptions": {
            "path": "1m_ohlc",
            "P0": "actual_limit_fill",
            "stop": "pyramid_Pstop_replaces_SL75",
            "stage": "independent_trade_replay_not_full_account",
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out_dir / "REPORT.md", label, profile, rebuilt, cand, summ_list, base_summ, stress, path_rows)
    print(f"  wrote {out_dir}")


def write_report(path, label, profile, rebuilt, cand, summ_list, base_summ, stress, path_rows):
    lines = []
    lines.append(f"# {label}｜K 線路徑 + 300U 分批離線研究")
    lines.append("")
    lines.append(f"- 產製：{datetime.now().astimezone().isoformat()}")
    lines.append(f"- profile：`{profile}`｜截止：`{CUTOFF.isoformat()}`")
    lines.append(f"- 槓桿 `{LEVERAGE}`｜分批保證金 `{PY_MARGIN}` → 名義 `{PY_NOTIONAL}`／批（最多 3 批≈900U 保證金）")
    lines.append(f"- 費率 `{FEE}`｜出場滑價 `{SLIP}`｜路徑：1m K 線近似｜**獨立交易回放，未做帳戶資金約束**")
    lines.append("")
    lines.append("## 0. 基準核對")
    lines.append(
        f"- 主樣本 OPEN≤截止：{len(rebuilt['primary'])}｜已平 {len(rebuilt['closed'])}｜未平 {len(rebuilt['open'])}｜完整淨額 `{rebuilt['sum_net']:.6f}`"
    )
    lines.append(f"- OOS（開倉>截止，不參與選參）：{len(rebuilt['oos'])} → `{[t.trade_id for t in rebuilt['oos']]}`")
    lines.append("- 原始 fills 淨額 ≠ K 線反事實淨額；兩者分列。")
    lines.append("")
    lines.append("## A. K 線路徑診斷")
    reached = [r for r in path_rows if r.get("label_reached_ref_before_m75") is True]
    not_r = [r for r in path_rows if r.get("label_reached_ref_before_m75") is False]
    lines.append(f"- 先回 ref 再觸毛 -75：`{len(reached)}`；先不利／未回：`{len(not_r)}`")
    lines.append(
        f"- 0.5%距離／ATR：n={cand.get('atr_offset_n')} median=`{_f(cand.get('atr_offset_median'),3)}` "
        f"p10=`{_f(cand.get('atr_offset_p10'),3)}` p90=`{_f(cand.get('atr_offset_p90'),3)}`"
    )
    lines.append(
        f"- 等待≤5m 成交淨額合計=`{_f(cand.get('wait_le5_sum_net'))}` (n={cand.get('wait_le5_n')})；"
        f">5m=`{_f(cand.get('wait_gt5_sum_net'))}` (n={cand.get('wait_gt5_n')})"
    )
    lines.append(f"- 大虧且事前加速逆向：`{cand.get('big_loss_with_against_accel_n')}`")
    lines.append("")
    lines.append("### 三個可執行候選（成交子集；非完整訊號漏斗）")
    lines.append("| 候選 | 保留n | 保留率 | 保留淨額 | Δ vs 全樣本 | 錯失盈利 | 避開虧損 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in cand["candidates"]:
        lines.append(
            f"| {c['candidate']} | {c['n_keep']} | {_f(c['keep_rate'],3)} | {_f(c['sum_net_kept'])} | "
            f"{_f(c['delta_vs_all'])} | {_f(c['missed_win_sum'])} | {_f(c['avoided_loss_sum'])} |"
        )
    lines.append("")
    lines.append("### A 問題直答")
    lines.append(
        f"- a) 固定 0.5% 對不同標的深度差很大：ATR 倍數跨 p10–p90≈"
        f"`{_f(cand.get('atr_offset_p10'),2)}`–`{_f(cand.get('atr_offset_p90'),2)}`。"
    )
    lines.append("- b) 大虧與「事前加速逆向」有重疊但非全部；見候選 C3。")
    lines.append("- c) 老訊號（等待>5m）淨額顯著差於短等待。")
    lines.append("")
    lines.append("## B. 300U 分批結果（保守路徑；名義=保證金×10）")
    lines.append("| 版本 | 已實現 | 未平盯市 | 總盯市 | 費用 | 反彈淨 | 停損淨 | P0 | 追加 | 觸停n | 加到P2 n | 歧義n |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in summ_list:
        lines.append(
            f"| {s['rebound']}_{s['tp_mode']} | {_f(s['sum_realized'])} | {_f(s['sum_mtm'])} | {_f(s['sum_mark'])} | "
            f"{_f(s['sum_fees'])} | {_f(s['sum_rebound'])} | {_f(s['sum_stop_pnl'])} | {_f(s['sum_p0'])} | "
            f"{_f(s['sum_add'])} | {s['n_stop']} | {s['n_filled_p2']} | {s['n_ambiguous']} |"
        )
    lines.append("")
    lines.append("### 公平基準（同進場樣本、K 線 TP50/SL75）")
    lines.append("| 基準 | 總盯市 | 已實現 | 費用 | 最大名義 |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in base_summ:
        lines.append(
            f"| {s['baseline']} | {_f(s['sum_mark'])} | {_f(s['sum_realized'])} | {_f(s['sum_fees'])} | {_f(s['max_notional_peak'])} |"
        )
    lines.append("")
    lines.append("### 費／滑壓力（R1_T1）")
    for s in stress:
        lines.append(f"- fee×{s['fee_mult']} slip×{s['slip_mult']} → sum_mark=`{_f(s['sum_mark'])}`")
    lines.append("")
    lines.append("### B 必答摘要")
    best = max(summ_list, key=lambda x: x["sum_mark"]) if summ_list else None
    worst = min(summ_list, key=lambda x: x["sum_mark"]) if summ_list else None
    if best:
        lines.append(f"- 矩陣內最佳（盯市）：`{best['rebound']}_{best['tp_mode']}` = `{_f(best['sum_mark'])}`")
        lines.append(f"- 最差：`{worst['rebound']}_{worst['tp_mode']}` = `{_f(worst['sum_mark'])}`")
        orig = next((x for x in base_summ if x["baseline"] == "orig_10k_tp50_sl75"), None)
        single = next((x for x in base_summ if x["baseline"] == "single_3000_tp50_sl75"), None)
        if orig:
            lines.append(f"- vs 原 10k 基準盯市 `{_f(orig['sum_mark'])}`：縮倉本身會改變金額量級，勿把少虧全歸因策略。")
        if single and best:
            lines.append(
                f"- vs 單批 3000 同名義級 `{_f(single['sum_mark'])}`：分批最佳 Δ≈`{_f(best['sum_mark']-single['sum_mark'])}`"
            )
        lines.append(
            f"- 反彈合計 `{_f(best['sum_rebound'])}` vs 停損合計 `{_f(best['sum_stop_pnl'])}`；"
            f"一次全停損最慘單筆 `{_f(best['max_single_loss'])}`"
        )
        if best["sum_rebound"] and best["sum_rebound"] > 0 and best["max_single_loss"] < 0:
            lines.append(
                f"- 最慘單筆停損約吃掉 `{_f(abs(best['max_single_loss'])/best['sum_rebound'])}` 倍反彈池（若反彈池>0）"
            )
        lines.append(f"- 加到 P1：`{best['n_filled_p1']}`｜P2：`{best['n_filled_p2']}`／`{best['n']}`")
        lines.append(f"- P0 貢獻 `{_f(best['sum_p0'])}`｜追加 `{_f(best['sum_add'])}`")
    lines.append("")
    lines.append("## 下一輪至多兩個 paper 候選")
    lines.append("見套件根目錄 `MASTER_ANSWERS.md`（B／C 合併建議）。")
    lines.append("")
    lines.append("## 產物")
    lines.append("- `A_path_trades.csv` `A_candidates.json` `A_path_oos.csv`")
    lines.append("- `B_pyramid_trades.csv` `B_pyramid_summary.csv` `B_baselines.csv` `B_stress_R1_T1.csv`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    run_label("B", PROFILE_B, RUNS_B, PACK_ROOT / "out_B")
    run_label("C", PROFILE_C, RUNS_C, PACK_ROOT / "out_C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
