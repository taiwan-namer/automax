#!/usr/bin/env python3
"""C all-T2 exit + B expectancy candidates. Offline only."""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
PREV = ROOT.parent / "kline_path_and_pyramid300_20261120"
# PREV first so patch_validate can import study_b.pyramid from old pack
sys.path.insert(0, str(PREV))
from common.config import (  # noqa: E402
    CUTOFF,
    FEE,
    PROFILE_B,
    PROFILE_C,
    PY_NOTIONAL,
    RUNS_B,
    RUNS_C,
    SLIP,
)
from common.data import load_bars, rebuild_trades  # noqa: E402
from patch_validate.run_patch import run_pyramid_with_cost, simulate_single_t2_pstop  # noqa: E402

sys.path.insert(0, str(ROOT))
from c_mod.allt2_pyramid import simulate_allt2  # noqa: E402
from b_mod.expectancy import (  # noqa: E402
    c3_exact,
    decompose_actual,
    partial_tp_70_30,
    ttl5_event_replay,
)


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
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})


def _f(x, n=4):
    if x is None:
        return "NA"
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return str(x)
    if isinstance(x, float):
        return f"{x:.{n}f}"
    return str(x)


def bucket_stats(rows: List[Dict[str, Any]], n_lots_key: str, mark_key: str) -> List[Dict[str, Any]]:
    out = []
    for n in (1, 2, 3):
        sub = [r for r in rows if int(r[n_lots_key]) == n]
        if not sub:
            out.append({"layers": n, "n": 0})
            continue
        marks = [float(r[mark_key]) for r in sub]
        wins = [m for m in marks if m > 0]
        losses = [m for m in marks if m <= 0]
        out.append(
            {
                "layers": n,
                "n": len(sub),
                "win_rate": len(wins) / len(sub),
                "avg_win": sum(wins) / len(wins) if wins else None,
                "avg_loss": sum(losses) / len(losses) if losses else None,
                "expectancy": sum(marks) / len(sub),
                "sum_mark": sum(marks),
            }
        )
    return out


def run_c() -> None:
    print("=== C three-arm ===")
    reb = rebuild_trades(PROFILE_C, set(RUNS_C), cutoff=CUTOFF)
    primary = reb["primary"]
    earliest = min(t.open_ts for t in primary)
    bars_map = {
        s: load_bars(PROFILE_C, s, earliest - timedelta(minutes=5), CUTOFF)
        for s in sorted({t.symbol for t in primary})
    }

    rows = []
    sum_a = sum_b = sum_c = sum_c_opt = 0.0
    for t in primary:
        bars = bars_map[t.symbol]
        A = simulate_single_t2_pstop(t, bars, notional=PY_NOTIONAL, mode="conservative")
        B = run_pyramid_with_cost(t, bars, fee=FEE, slip=SLIP, mode="conservative")
        Ccons = simulate_allt2(t, bars, mode="conservative")
        Copt = simulate_allt2(t, bars, mode="optimistic")
        sum_a += A["total_mark_net"]
        sum_b += B["total_mark_net"]
        sum_c += Ccons.total_mark_net
        sum_c_opt += Copt.total_mark_net

        # conversion: old R2 add exits vs new
        old_lots = json.loads(B["lots"]) if isinstance(B["lots"], str) else B["lots"]
        new_lots = Ccons.lots
        old_add_r2 = [L for L in old_lots if L["name"] != "P0" and L.get("exit_reason") in ("R1_EXIT", "R2_EXIT")]
        new_by = {L["name"]: L for L in new_lots}
        conv = []
        for L in old_add_r2:
            nl = new_by.get(L["name"])
            conv.append(
                {
                    "lot": L["name"],
                    "old_net": L.get("net_u"),
                    "old_exit": L.get("exit_reason"),
                    "new_net": nl.get("net_u") if nl else None,
                    "new_exit": nl.get("exit_reason") if nl else None,
                    "turned_stop": bool(nl and nl.get("exit_reason") == "PSTOP"),
                    "delta_new_minus_old": (nl.get("net_u") - L.get("net_u")) if nl and L.get("net_u") is not None else None,
                }
            )

        rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "A_mark": A["total_mark_net"],
                "A_realized": A["realized_net"],
                "A_mtm": A["open_mtm_net"],
                "A_fees": A["fees"],
                "A_hold": A["hold_min"],
                "A_status": A["status"],
                "B_mark": B["total_mark_net"],
                "B_realized": B["realized_net"],
                "B_mtm": B["open_mtm_net"],
                "B_fees": B["fees"],
                "B_hold": B["hold_min"],
                "B_n_lots": B["n_lots_filled"],
                "B_p0": B["p0_net"],
                "B_add": B["add_net"],
                "B_rebound": B["rebound_pnl"],
                "B_stop_pnl": B["stop_pnl"],
                "B_stop_hit": B["stop_hit"],
                "B_ambiguous": B["ambiguous"],
                "C_mark": Ccons.total_mark_net,
                "C_mark_opt": Copt.total_mark_net,
                "C_realized": Ccons.realized_net,
                "C_mtm": Ccons.open_mtm_net,
                "C_fees": Ccons.fees,
                "C_hold": Ccons.hold_min,
                "C_n_lots": Ccons.n_lots_filled,
                "C_p0": Ccons.p0_net,
                "C_add": Ccons.add_net,
                "C_stop_hit": Ccons.stop_hit,
                "C_tp_hit": Ccons.tp_hit,
                "C_exposure_notional_min": Ccons.exposure_notional_min,
                "C_dd_peak": Ccons.dd_from_peak,
                "C_min_run": Ccons.min_running_net,
                "C_ambiguous": Ccons.ambiguous,
                "C_amb_note": Ccons.amb_note,
                "delta_C_minus_B": Ccons.total_mark_net - B["total_mark_net"],
                "delta_C_minus_A": Ccons.total_mark_net - A["total_mark_net"],
                "old_r2_add_n": len(old_add_r2),
                "conv_json": json.dumps(conv, ensure_ascii=False),
            }
        )

    out = ROOT / "out_C"
    write_csv(out / "three_arm_trades.csv", rows)

    # conversion flat
    conv_rows = []
    for r in rows:
        for c in json.loads(r["conv_json"]):
            conv_rows.append({"trade_id": r["trade_id"], **c})
    write_csv(out / "conversion_oldR2_to_allt2.csv", conv_rows)

    # reproduce check
    repro = {
        "A_single_sum_mark": sum_a,
        "A_target": -39.6055,
        "A_ok": abs(sum_a - (-39.6055)) < 1e-3,
        "B_r2t2_sum_mark": sum_b,
        "B_target": 78.4357,
        "B_ok": abs(sum_b - 78.4357) < 1e-3,
        "C_allt2_cons": sum_c,
        "C_allt2_opt": sum_c_opt,
        "delta_C_minus_B": sum_c - sum_b,
        "delta_C_minus_A": sum_c - sum_a,
        "n": len(rows),
        "C_n_ambiguous": sum(1 for r in rows if r["C_ambiguous"] in (True, "True")),
        "note_amb0": "歧義數=0 仍只代表本1m規則未同時觸發多事件，不代表真實成交順序確定",
        "account_replay": "未完成（無完整帳戶事件約束回放）",
    }
    buckets = {
        "B_r2t2": bucket_stats(rows, "B_n_lots", "B_mark"),
        "C_allt2": bucket_stats(rows, "C_n_lots", "C_mark"),
    }
    # conversion aggregates
    turned_stop = [c for c in conv_rows if c.get("turned_stop") in (True, "True")]
    conv_sum = {
        "old_r2_add_exits_n": len(conv_rows),
        "sum_old_net": sum(float(c["old_net"]) for c in conv_rows if c.get("old_net") not in ("", None)),
        "sum_new_net": sum(float(c["new_net"]) for c in conv_rows if c.get("new_net") not in ("", None)),
        "sum_delta": sum(float(c["delta_new_minus_old"]) for c in conv_rows if c.get("delta_new_minus_old") not in ("", None)),
        "n_turned_stop": len(turned_stop),
        "sum_new_net_turned_stop": sum(float(c["new_net"]) for c in turned_stop if c.get("new_net") not in ("", None)),
    }
    summary = {
        "reproduce": repro,
        "arm_sums": {
            "A_realized": sum(r["A_realized"] for r in rows),
            "A_mtm": sum(r["A_mtm"] for r in rows),
            "A_fees": sum(r["A_fees"] for r in rows),
            "B_realized": sum(r["B_realized"] for r in rows),
            "B_mtm": sum(r["B_mtm"] for r in rows),
            "B_fees": sum(r["B_fees"] for r in rows),
            "B_p0": sum(r["B_p0"] for r in rows),
            "B_add": sum(r["B_add"] for r in rows),
            "C_realized": sum(r["C_realized"] for r in rows),
            "C_mtm": sum(r["C_mtm"] for r in rows),
            "C_fees": sum(r["C_fees"] for r in rows),
            "C_p0": sum(r["C_p0"] for r in rows),
            "C_add": sum(r["C_add"] for r in rows),
            "C_avg_hold": sum(r["C_hold"] or 0 for r in rows) / len(rows),
            "C_sum_exposure_notional_min": sum(r["C_exposure_notional_min"] for r in rows),
            "C_worst_dd_peak": min(r["C_dd_peak"] for r in rows),
        },
        "buckets": buckets,
        "conversion": conv_sum,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# C｜全批於第一筆 T2 價退出 — 三臂比較",
        "",
        f"- 產製：{datetime.now().astimezone().isoformat()}",
        f"- 截止：{CUTOFF.isoformat()}｜費率 {FEE}｜滑價 {SLIP}｜名義/批 {PY_NOTIONAL}",
        "- 帳戶回放：**未完成**",
        "",
        "## 0. 基準重現",
        f"- A 單批 T2+Pstop：`{_f(sum_a)}`（目標 -39.6055）ok={repro['A_ok']}",
        f"- B 舊 R2_T2：`{_f(sum_b)}`（目標 +78.4357）ok={repro['B_ok']}",
        "",
        "## 1. 三臂總盯市",
        f"- A：`{_f(sum_a)}`",
        f"- B 舊R2：`{_f(sum_b)}`",
        f"- C 新全T2（保守）：`{_f(sum_c)}`｜樂觀界：`{_f(sum_c_opt)}`",
        f"- **C−B：`{_f(sum_c - sum_b)}`｜C−A：`{_f(sum_c - sum_a)}`**",
        "",
        "## 2. 分列",
        f"- C 已實現 `{_f(summary['arm_sums']['C_realized'])}`｜未平 `{_f(summary['arm_sums']['C_mtm'])}`｜費 `{_f(summary['arm_sums']['C_fees'])}`",
        f"- C P0 `{_f(summary['arm_sums']['C_p0'])}`｜追加 `{_f(summary['arm_sums']['C_add'])}`",
        f"- C 均持倉分 `{_f(summary['arm_sums']['C_avg_hold'],1)}`｜曝險名義·分鐘 `{_f(summary['arm_sums']['C_sum_exposure_notional_min'],1)}`｜最差獨立回撤 `{_f(summary['arm_sums']['C_worst_dd_peak'])}`",
        f"- 歧義交易 n={repro['C_n_ambiguous']}（{repro['note_amb0']}）",
        "",
        "## 3. 層數分桶（C 新）",
    ]
    for b in buckets["C_allt2"]:
        lines.append(
            f"- {b['layers']}層：n={b.get('n')} wr={_f(b.get('win_rate'),3)} "
            f"avgWin={_f(b.get('avg_win'))} avgLoss={_f(b.get('avg_loss'))} E={_f(b.get('expectancy'))}"
        )
    lines.append("")
    lines.append("## 4. 舊R2追加退出 → 新版")
    lines.append(
        f"- 舊追加反彈退出筆數(跨交易 lot)={conv_sum['old_r2_add_exits_n']}｜"
        f"舊淨合計 `{_f(conv_sum['sum_old_net'])}` → 新 `{_f(conv_sum['sum_new_net'])}`｜Δ `{_f(conv_sum['sum_delta'])}`"
    )
    lines.append(
        f"- 轉為停損 n={conv_sum['n_turned_stop']}｜這些在新版淨合計 `{_f(conv_sum['sum_new_net_turned_stop'])}`"
    )
    lines.append("")
    lines.append("逐筆：`three_arm_trades.csv`｜轉換：`conversion_oldR2_to_allt2.csv`")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((out / "REPORT.md").read_text())
    return summary


def run_b() -> None:
    print("=== B expectancy ===")
    reb = rebuild_trades(PROFILE_B, set(RUNS_B), cutoff=CUTOFF)
    closed = reb["closed"]
    primary = reb["primary"]
    decomp = decompose_actual(closed)
    write_csv(ROOT / "out_B" / "decomp_trades.csv", decomp["rows"])

    ttl = ttl5_event_replay(reb, set(RUNS_B))
    write_csv(ROOT / "out_B" / "ttl5_events.csv", ttl["events"])
    ttl_pub = {k: v for k, v in ttl.items() if k != "events"}

    c3 = c3_exact(reb, PROFILE_B)
    write_csv(ROOT / "out_B" / "c3_rows.csv", c3["rows"])
    c3_pub = {k: v for k, v in c3.items() if k != "rows"}

    # partial TP on primary (closed+open) with kline path
    earliest = min(t.open_ts for t in primary)
    bars_map = {
        s: load_bars(PROFILE_B, s, earliest - timedelta(minutes=5), CUTOFF)
        for s in sorted({t.symbol for t in primary})
    }
    part_rows = []
    for t in primary:
        r = partial_tp_70_30(t, bars_map[t.symbol], mode="conservative")
        part_rows.append(r)
    write_csv(ROOT / "out_B" / "partial70_30_trades.csv", part_rows)
    # compare only where baseline closed complete
    part_closed = [r for r in part_rows if r.get("baseline_net") is not None and r["status"] == "COMPLETE"]
    part_sum = {
        "n_complete_vs_baseline": len(part_closed),
        "sum_baseline": sum(r["baseline_net"] for r in part_closed),
        "sum_partial": sum(r["total_mark_net"] for r in part_closed),
        "delta": sum(r["total_mark_net"] - r["baseline_net"] for r in part_closed),
        "avg_baseline": sum(r["baseline_net"] for r in part_closed) / len(part_closed) if part_closed else None,
        "avg_partial": sum(r["total_mark_net"] for r in part_closed) / len(part_closed) if part_closed else None,
        "n_open_partial": sum(1 for r in part_rows if r["status"] == "OPEN"),
        "sum_mark_all_incl_open": sum(r["total_mark_net"] for r in part_rows),
    }

    summary = {
        "decompose": {k: v for k, v in decomp.items() if k != "rows"},
        "ttl5": ttl_pub,
        "c3": c3_pub,
        "partial70_30": part_sum,
        "prior_note": "本樣本固定拉TP/縮SL不優於50/75（沿用已驗證）；本輪不重掃。",
        "oos_note": "C3/TTL 樣外僅 1 筆級；不足宣稱驗證。",
        "account_replay": "未完成",
    }
    (ROOT / "out_B" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    d = summary["decompose"]
    lines = [
        "# B｜高勝率未轉正期望 — 拆解與三候選",
        "",
        f"- 產製：{datetime.now().astimezone().isoformat()}",
        f"- 截止：{CUTOFF.isoformat()}｜實際已平 n={d['n']}",
        "",
        "## 1. 收益結構（實際 fills，完整 trade_id）",
        f"- 毛利勝率 `{_f(d['gross_win_rate'],3)}`｜淨利勝率 `{_f(d['net_win_rate'],3)}`",
        f"- 平均淨盈 `{_f(d['avg_net_win'])}`｜平均淨虧 `{_f(d['avg_net_loss'])}`｜盈虧比 `{_f(d['payoff_abs_avgWin_over_avgLoss'],3)}`",
        f"- **淨期望 `{_f(d['net_expectancy'])}`**｜合計淨額 `{_f(d['sum_net'])}`｜費用合計 `{_f(d['sum_fees'])}`｜毛合計 `{_f(d['sum_raw_gross'])}`",
        f"- 最大3筆虧損合計 `{_f(d['top3_loss_sum'])}`｜占全部虧損份額 `{_f(d['top3_loss_share_of_all_losses'],3)}`｜{d['top3_ids']}",
        f"- TP相對毛50超額均值 `{_f(d['tp_overshoot_mean'])}`｜SL相對毛-75超額均值 `{_f(d['sl_overshoot_mean'])}`",
        f"- {d['attribution_note']}",
        "",
        "## 2a. TTL5（完整掛單；口徑修正）",
        f"- {ttl_pub['口径说明']}",
        f"- 已平基準：n={ttl_pub['baseline_closed_n']} sum=`{_f(ttl_pub['baseline_closed_sum'])}` avg=`{_f(ttl_pub['baseline_closed_avg'])}`",
        f"- TTL保留已平：n={ttl_pub['ttl5_keep_closed_n']} sum=`{_f(ttl_pub['ttl5_keep_closed_sum'])}` avg=`{_f(ttl_pub['ttl5_keep_closed_avg'])}`",
        f"- TTL取消之已平：n={ttl_pub['ttl5_cancel_closed_n']} sum=`{_f(ttl_pub['ttl5_cancel_closed_sum'])}` avg=`{_f(ttl_pub['ttl5_cancel_closed_avg'])}`",
        f"- 錯失盈利 `{_f(ttl_pub['missed_wins_sum'])}`｜避開虧損 `{_f(ttl_pub['avoided_loss_sum'])}`",
        f"- 每掛單訊號（排除未平分母）：原 avg `{_f(ttl_pub['per_queued_avg_baseline_excl_open'])}` → TTL avg `{_f(ttl_pub['per_queued_avg_ttl5_excl_open'])}`",
        f"- 未平成交 trade_ids：{ttl_pub['open_fill_trade_ids']}",
        "",
        "## 2b. C3（精確布林）",
        f"- 公式：`{c3_pub['formula']}`",
        f"- {c3_pub['why_8_kept']}",
        f"- keep n={c3_pub['n_keep']} sum=`{_f(c3_pub['sum_net_keep'])}` avg=`{_f(c3_pub['avg_net_keep'])}`｜全樣 avg=`{_f(c3_pub['avg_net_all'])}`",
        f"- 錯失盈利 `{_f(c3_pub['missed_wins'])}`｜避開虧損 `{_f(c3_pub['avoided_losses'])}`",
        f"- OOS：{c3_pub['oos']}",
        "",
        "## 2c. 分批止盈 70%/@TP50 + 30%/@TP100（止損移進場價）",
        f"- 對已平可比 n={part_sum['n_complete_vs_baseline']}｜基準合計 `{_f(part_sum['sum_baseline'])}` → 候選 `{_f(part_sum['sum_partial'])}`｜Δ `{_f(part_sum['delta'])}`",
        f"- 每筆 avg 基準 `{_f(part_sum['avg_baseline'])}` → `{_f(part_sum['avg_partial'])}`",
        f"- 含未平總盯市 `{_f(part_sum['sum_mark_all_incl_open'])}`｜未平n={part_sum['n_open_partial']}",
        "",
        "## 3. 改善來源定性",
        "- TTL5：主要是**少交易／避開較差已平**（取消子集 avg 更負），不是把留下的每筆變強到大幅正期望。",
        "- C3：keep 子集期望轉正，但 n=8 極小；靠剔除 accel∧trend15 雙真。",
        "- 分倉延伸：看 Δ 是否來自多賺（延伸）或少虧（BE），見逐筆 exit_reason。",
        "",
        "三候選**未疊加**；樣外不足不宣稱驗證。",
    ]
    (ROOT / "out_B" / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((ROOT / "out_B" / "REPORT.md").read_text())
    return summary


def main() -> int:
    c_sum = run_c()
    b_sum = run_b()
    master = []
    master.append("# 主結論｜C 全T2退出 + B 期望候選")
    master.append("")
    master.append("## C")
    r = c_sum["reproduce"]
    master.append(
        f"- 重現 A={_f(r['A_single_sum_mark'])} ok={r['A_ok']}｜B_R2T2={_f(r['B_r2t2_sum_mark'])} ok={r['B_ok']}"
    )
    master.append(
        f"- 新全T2 保守 `{_f(r['C_allt2_cons'])}` vs 舊R2 `{_f(r['B_r2t2_sum_mark'])}` → **Δ `{_f(r['delta_C_minus_B'])}`**"
    )
    master.append(f"- vs 單批A Δ `{_f(r['delta_C_minus_A'])}`｜樂觀界 `{_f(r['C_allt2_opt'])}`")
    master.append(f"- 轉換：{c_sum['conversion']}")
    master.append("")
    master.append("## B")
    d = b_sum["decompose"]
    master.append(
        f"- 淨勝率 `{_f(d['net_win_rate'],3)}` 但淨期望 `{_f(d['net_expectancy'])}`｜"
        f"avgWin `{_f(d['avg_net_win'])}` / avgLoss `{_f(d['avg_net_loss'])}`"
    )
    master.append(
        f"- TTL5 已平 avg `{_f(b_sum['ttl5']['baseline_closed_avg'])}` → keep avg `{_f(b_sum['ttl5']['ttl5_keep_closed_avg'])}`"
    )
    master.append(
        f"- C3 keep avg `{_f(b_sum['c3']['avg_net_keep'])}`（n={b_sum['c3']['n_keep']}）｜{b_sum['c3']['why_8_kept']}"
    )
    master.append(f"- 70/30 分倉 Δ合計 `{_f(b_sum['partial70_30']['delta'])}`")
    master.append("")
    master.append("詳見 `out_C/REPORT.md` `out_B/REPORT.md`。未改 runtime。")
    (ROOT / "MASTER_ANSWERS.md").write_text("\n".join(master) + "\n", encoding="utf-8")
    (ROOT / "README.md").write_text(
        "# C all-T2 + B expectancy\n\n```bash\npython3 run_all.py\n```\n\n依賴同目錄上一層 `kline_path_and_pyramid300_20261120`。\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
