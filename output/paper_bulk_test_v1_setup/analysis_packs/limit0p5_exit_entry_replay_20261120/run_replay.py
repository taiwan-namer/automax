#!/usr/bin/env python3
"""Offline B/C limit0p5 exit-matrix + entry experiments. Read-only."""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from limit0p5_replay.constants import (  # noqa: E402
    CUTOFF,
    EXPECTED_B,
    EXPECTED_C,
    PROFILE_B,
    PROFILE_C,
    RUNS_B,
    RUNS_C,
    FEE_RATE_ONE_SIDE,
    SLIPPAGE_ADVERSE,
)
from limit0p5_replay.entry_exp import run_entry_experiments  # noqa: E402
from limit0p5_replay.exit_replay import run_exit_matrix  # noqa: E402
from limit0p5_replay.trades import rebuild_trades  # noqa: E402


def setup_root() -> Path:
    return ROOT.parents[1]  # paper_bulk_test_v1_setup


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    # union keys
    keys: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_cell(r.get(k)) for k in keys})


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return f"{v:.10g}"
    return v


def _fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return "NA"
    if isinstance(x, float):
        if math.isinf(x):
            return "inf"
        return f"{x:.{nd}f}"
    return str(x)


def conversion_tables(samples, matrix_trade_rows, mode: str = "conservative") -> Dict[str, List[Dict[str, Any]]]:
    """Map actual win/loss to counterfactual outcomes for each TP/SL."""
    actual = {s.trade_id: s for s in samples}
    out_rows = []
    for r in matrix_trade_rows:
        s = actual.get(r["trade_id"])
        if not s or s.status_at_cutoff != "CLOSED":
            continue
        actual_win = (s.actual_exit_reason or "").endswith("TP") or s.actual_exit_reason == "FIXED_TP"
        actual_loss = (s.actual_exit_reason or "").endswith("SL") or s.actual_exit_reason == "FIXED_SL" or s.actual_exit_reason == "INITIAL_STOP"
        # paper uses FIXED_TP / FIXED_SL? check — earlier sample FIXED_TP; SL might be INITIAL_STOP or FIXED_SL
        reason = s.actual_exit_reason or ""
        if "TP" in reason:
            actual_win, actual_loss = True, False
        elif "SL" in reason or "STOP" in reason:
            actual_win, actual_loss = False, True
        else:
            actual_win = (s.actual_complete_net or 0) > 0
            actual_loss = (s.actual_complete_net or 0) < 0
        st = r[f"status_{mode}"]
        cf_win = st == "TP"
        cf_loss = st == "SL"
        cf_open = st == "OPEN"
        out_rows.append(
            {
                "trade_id": r["trade_id"],
                "tp_u": r["tp_u"],
                "sl_u": r["sl_u"],
                "actual_reason": reason,
                "actual_net": s.actual_complete_net,
                "actual_win": actual_win,
                "cf_status": st,
                "cf_net": r[f"realized_net_{mode}"] if not cf_open else r[f"mtm_net_{mode}"],
                "win_to_loss": bool(actual_win and cf_loss),
                "loss_to_win": bool(actual_loss and cf_win),
                "win_to_open": bool(actual_win and cf_open),
                "loss_improved": bool(
                    actual_loss and r[f"realized_net_{mode}"] is not None and s.actual_complete_net is not None
                    and r[f"realized_net_{mode}"] > s.actual_complete_net
                ),
            }
        )
    return {"conversion_rows": out_rows}


def atr_offset_diag(samples) -> Dict[str, Any]:
    ratios = []
    for s in samples:
        if s.atr and s.atr > 0 and s.signal_ref_price:
            dist = abs((s.limit_price or 0) - s.signal_ref_price)
            # 0.5% of ref
            off = 0.005 * s.signal_ref_price
            ratios.append(
                {
                    "trade_id": s.trade_id,
                    "atr": s.atr,
                    "offset_abs": off,
                    "offset_over_atr": off / s.atr,
                    "side": s.side,
                }
            )
    if not ratios:
        return {"n": 0}
    vals = [r["offset_over_atr"] for r in ratios]
    vals.sort()
    mid = vals[len(vals) // 2]
    return {
        "n": len(vals),
        "median_offset_over_atr": mid,
        "p25": vals[max(0, len(vals) // 4)],
        "p75": vals[min(len(vals) - 1, (3 * len(vals)) // 4)],
        "mean": sum(vals) / len(vals),
        "rows": ratios,
    }


def write_report(path: Path, label: str, profile: str, rebuilt: dict, exit_res: dict, entry_res: dict, expected: dict) -> None:
    samples = rebuilt["samples"]
    closed = rebuilt["closed"]
    opened = rebuilt["open"]
    summ = exit_res["matrix_summary"]
    milestones = exit_res["milestones"]

    def cell(tp, sl, mode="conservative"):
        return next(s for s in summ if s["tp_u"] == tp and s["sl_u"] == sl and s["mode"] == mode)

    base = cell(50, 75)
    # rebound stats
    ref_before_sl75 = [m for m in milestones if m.get("reached_ref_before_sl75") is True]
    ref_after_sl75 = [m for m in milestones if m.get("reached_ref_before_sl75") is False]
    ref_none = [m for m in milestones if m.get("reached_ref_before_sl75") is None]
    after50 = [m for m in milestones if m.get("first_touch_tp50")]
    ext100 = [m for m in after50 if m.get("after_plus50_hit_plus100_before_be") is True]
    back_be = [m for m in after50 if m.get("after_plus50_hit_plus100_before_be") is False]

    # baseline replay fidelity
    base_rows = exit_res["baseline_trade_rows"]
    closed_base = [r for r in base_rows if r["status_at_cutoff_actual"] == "CLOSED"]
    diffs = [r["baseline_net_diff_conservative"] for r in closed_base if r.get("baseline_net_diff_conservative") is not None]
    status_match = 0
    for r in closed_base:
        # map actual reason
        ar = r.get("actual_exit_reason") or ""
        want = "TP" if "TP" in ar else ("SL" if ("SL" in ar or "STOP" in ar) else None)
        if want and r.get("baseline_status_conservative") == want:
            status_match += 1

    atr_d = atr_offset_diag(samples)

    lines: List[str] = []
    lines.append(f"# {label} 限價 0.5% 退出／進場離線回放報告")
    lines.append("")
    lines.append(f"- 產製時間：{datetime.now().astimezone().isoformat()}")
    lines.append(f"- profile：`{profile}`")
    lines.append(f"- 主截止：`{CUTOFF.isoformat()}`")
    lines.append("- 資料層：1m K 線近似（無歷史 bid/ask）；標記為 K 線近似，非全量可成交聲明")
    lines.append(f"- 費率：單邊 `{FEE_RATE_ONE_SIDE}`；出場不利滑價 `{SLIPPAGE_ADVERSE}`（套在障礙價／缺口開盤）")
    lines.append("- 限價成交模型（進場實驗）：K 線觸價≈成交；同根觸價+1%作廢→保守作廢；**不**套 maker 費率")
    lines.append("- 本報告＝固定實際進場之獨立交易退出敏感度＋掛單 cohort 進場探索；**不是**可直接部署的完整帳戶回測")
    lines.append("")
    lines.append("## 0. 版本事實核對")
    flips = {bool(s.direction_flip) for s in samples if s.direction_flip is not None}
    lines.append(f"- 樣本數（OPEN≤截止，指定 run）：已平 `{rebuilt['n_closed']}`／未平 `{rebuilt['n_open']}`")
    lines.append(f"- 完整淨額合計：`{rebuilt['sum_complete_net']:.10f}`（目標 `{expected['sum_net']}`）")
    lines.append(f"- 基準重現：closed match `{rebuilt['n_closed']==expected['n_closed']}`；open match `{rebuilt['n_open']==expected['n_open']}`；淨額差 `{rebuilt['sum_complete_net']-expected['sum_net']:.6e}`")
    lines.append(f"- direction_flip 實測集合：`{sorted(flips) if flips else 'NA'}`")
    if label == "B":
        lines.append("- B：`SIGNAL_DIRECTION_FLIP=True`（自掃後翻轉）；限價相對 **signal_ref=訊號當下 mid**；多 −0.5%／空 +0.5%")
    else:
        lines.append("- C：`SIGNAL_DIRECTION_FLIP=False`（自掃不翻轉）；`MIRROR_ONLY_ENTRIES=False`；非鏡像")
    lines.append("- cancel1pct：相對 **signal_ref** 不利 mid 偏離 ≥1% 作廢；TTL=0；無時間撤單")
    lines.append("- 數量：掛單時依限價／名義計算（LIMIT_QUEUED.qty）；成交用限價，不再加進場滑價")
    lines.append("- 部分成交：此版本無部分進場；退出無 partial TP（fixed-gross）")
    lines.append("")
    lines.append("### 截止造成的「未平」細項")
    for s in opened:
        tag = "closed_after_cutoff" if s.close_after_cutoff else "still_open_or_killed"
        lines.append(f"- `{s.trade_id}`｜{tag}｜actual_exit={s.actual_exit_reason}｜close_ts={s.close_ts}")
    lines.append("")
    lines.append("## 1. 基準 +50/−75 路徑重現（K 線近似）")
    lines.append(f"- 已平倉中，保守模式退出方向與實際一致：`{status_match}/{len(closed_base)}`")
    if diffs:
        lines.append(
            f"- 保守模式完整淨額差（回放−實際）：mean=`{_fmt(sum(diffs)/len(diffs))}`｜"
            f"median=`{_fmt(sorted(diffs)[len(diffs)//2])}`｜"
            f"|max|=`{_fmt(max(abs(x) for x in diffs))}`"
        )
    lines.append("- 差異來源：輪詢報價／bid-ask vs 1m OHLC；同根歧義；出場標記價超額")
    lines.append(f"- 基準格（保守）已實現淨額合計=`{_fmt(base['realized_net_sum'])}`｜截止總盯市=`{_fmt(base['cutoff_total_mark_net'])}`｜未平=`{base['n_open']}`｜資料不足=`{base['n_insufficient']}`")
    lines.append("")
    lines.append("## 2. A. SL=75，TP 掃描")
    lines.append("| TP | 已平 | 未平 | 勝率 | 已實現淨 | 截止總盯市 | Δ已實現 vs 50 | Δ盯市 vs 50 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for tp in (50, 100, 150, 200):
        c = cell(tp, 75)
        lines.append(
            f"| {tp} | {c['n_closed']} | {c['n_open']} | {_fmt(c['win_rate'],3)} | {_fmt(c['realized_net_sum'])} | "
            f"{_fmt(c['cutoff_total_mark_net'])} | {_fmt(c['delta_realized_vs_baseline'])} | {_fmt(c['delta_cutoff_mark_vs_baseline'])} |"
        )
    lines.append("")
    lines.append("## 3. B. TP=50，SL 掃描")
    lines.append("| SL | 已平 | 未平 | 勝率 | 已實現淨 | 截止總盯市 | Δ已實現 vs 75 | Δ盯市 vs 75 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sl in (75, 50, 35, 25):
        c = cell(50, sl)
        lines.append(
            f"| {sl} | {c['n_closed']} | {c['n_open']} | {_fmt(c['win_rate'],3)} | {_fmt(c['realized_net_sum'])} | "
            f"{_fmt(c['cutoff_total_mark_net'])} | {_fmt(c['delta_realized_vs_baseline'])} | {_fmt(c['delta_cutoff_mark_vs_baseline'])} |"
        )
    lines.append("")
    lines.append("## 4. C. 完整 16 組矩陣（保守＝同根雙觸→SL；括號為樂觀 TP）")
    lines.append("| TP | SL | 已平 | 未平 | 不足 | 勝率 | 已實現淨 | 截止總盯市 | PF | 均持倉分 | Δ盯市 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for tp in (50, 100, 150, 200):
        for sl in (75, 50, 35, 25):
            c = cell(tp, sl, "conservative")
            o = cell(tp, sl, "optimistic")
            lines.append(
                f"| {tp} | {sl} | {c['n_closed']}/{o['n_closed']} | {c['n_open']}/{o['n_open']} | "
                f"{c['n_insufficient']} | {_fmt(c['win_rate'],3)}/{_fmt(o['win_rate'],3)} | "
                f"{_fmt(c['realized_net_sum'])}/{_fmt(o['realized_net_sum'])} | "
                f"{_fmt(c['cutoff_total_mark_net'])}/{_fmt(o['cutoff_total_mark_net'])} | "
                f"{_fmt(c['pf'],3)} | {_fmt(c['avg_hold_min'],1)} | {_fmt(c['delta_cutoff_mark_vs_baseline'])} |"
            )
    lines.append("")
    lines.append("## 5. 反彈區間假設")
    lines.append(f"- 先回到 signal_ref、且在觸及毛 −75 之前：`{len(ref_before_sl75)}` / {len(milestones)}")
    lines.append(f"- 先觸 −75（或未回 ref）：false=`{len(ref_after_sl75)}`；無法判定／未觸=`{len(ref_none)}`")
    if ref_before_sl75:
        ttr = [m["time_to_ref_min"] for m in ref_before_sl75 if m.get("time_to_ref_min") is not None]
        rtn = [m["roundtrip_net_at_ref"] for m in ref_before_sl75 if m.get("roundtrip_net_at_ref") is not None]
        lines.append(f"- 回到 ref 所需分鐘：median=`{_fmt(sorted(ttr)[len(ttr)//2] if ttr else None,1)}`")
        lines.append(f"- 若在 ref 出場的往返淨估值合計：`{_fmt(sum(rtn) if rtn else None)}`（含雙邊費＋出場滑價近似）")
    lines.append(f"- 曾觸毛 +50：`{len(after50)}`；其後先到 +100 再回進場價：`{len(ext100)}`；先回進場價：`{len(back_be)}`")
    ext150 = [m for m in after50 if m.get("after_plus50_hit_plus150_before_sl75") is True]
    ext200 = [m for m in after50 if m.get("after_plus50_hit_plus200_before_sl75") is True]
    lines.append(f"- +50 後、在 −75 前先到 +150：`{len(ext150)}`；先到 +200：`{len(ext200)}`")
    # shrink SL cost on actual winners
    actual_wins = [s for s in closed if s.actual_exit_reason and "TP" in s.actual_exit_reason]
    lines.append(f"- 實際 +50 贏單數：`{len(actual_wins)}`")
    for sl in (50, 35, 25):
        # from conversion
        pass
    conv = conversion_tables(samples, exit_res["matrix_trade_rows"])["conversion_rows"]
    for sl in (50, 35, 25):
        sub = [r for r in conv if r["tp_u"] == 50 and r["sl_u"] == sl]
        w2l = sum(1 for r in sub if r["win_to_loss"])
        limpr = sum(1 for r in sub if r["loss_improved"])
        lines.append(f"- TP50/SL{sl}：原贏變輸 `{w2l}`；原輸改善（已平淨額變好）`{limpr}` / 已平樣本 `{len(sub)}`")
    for tp in (100, 150, 200):
        sub = [r for r in conv if r["tp_u"] == tp and r["sl_u"] == 75]
        w2o = sum(1 for r in sub if r["win_to_open"])
        w2l = sum(1 for r in sub if r["win_to_loss"])
        lines.append(f"- TP{tp}/SL75：原贏變未平 `{w2o}`；原贏變輸 `{w2l}`")
    lines.append("")
    lines.append("### 路徑結論（本樣本）")
    # heuristic answer
    rebound_rate = len(ref_before_sl75) / len(milestones) if milestones else 0
    ext_rate = len(ext100) / len(after50) if after50 else 0
    if rebound_rate >= 0.45 and ext_rate < 0.35:
        verdict = "較偏短線回歸／回到參考價一段；延伸至 +100 以上比例有限"
    elif ext_rate >= 0.35:
        verdict = "存在可觀測延伸（+50 後仍有一批先到 +100）；不全是純回歸"
    else:
        verdict = "回歸與延伸混雜；樣本小，僅作探索"
    lines.append(f"- **{verdict}**（ref-before-SL75比率≈{_fmt(rebound_rate,3)}；+50後先+100比率≈{_fmt(ext_rate,3)}）")
    lines.append("")
    lines.append("## 6. 進場距離實驗（LIMIT_QUEUED cohort）")
    lines.append(f"- 覆蓋：queued={entry_res.get('n_queued_input')}｜可模擬={entry_res.get('coverage')}")
    lines.append("| offset | 成交 | 作廢 | 未觸 | 成交率 | 每訊號淨合計 | 均等待分 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|")
    for s in entry_res.get("summary") or []:
        lines.append(
            f"| {s['offset_pct']} | {s['n_filled']} | {s['n_canceled']} | {s['n_no_fill']} | "
            f"{_fmt(s['fill_rate'],3)} | {_fmt(s['sum_net_per_signal'])} | {_fmt(s['avg_wait_min_filled'],2)} |"
        )
    wd = entry_res.get("wait_diag_offset_0p5") or {}
    lines.append("- 實際 0.5% 等待桶（成交後 TP50/SL75 保守路徑淨額）：")
    for k, v in wd.items():
        lines.append(f"  - {k}: n={v['n']} sum={_fmt(v['sum_net'])} avg={_fmt(v['avg_net'])}")
    if atr_d.get("n"):
        lines.append(
            f"- 已成交樣本：0.5%距離／當時 ATR 中位數=`{_fmt(atr_d['median_offset_over_atr'],3)}` "
            f"(p25={_fmt(atr_d['p25'],3)}, p75={_fmt(atr_d['p75'],3)}, n={atr_d['n']})"
        )
    else:
        lines.append("- ATR 倍數：樣本不足或缺 atr 欄")
    lines.append("")
    lines.append("## 7. 下一輪最值得 paper 驗證的方案（不自動上線）")
    # pick based on matrix
    ranked = sorted(
        [s for s in summ if s["mode"] == "conservative"],
        key=lambda x: x["cutoff_total_mark_net"],
        reverse=True,
    )
    top = ranked[0]
    lines.append(
        f"1. 退出矩陣探索冠軍（僅固定進場敏感度）：TP={top['tp_u']}/SL={top['sl_u']}，"
        f"截止盯市=`{_fmt(top['cutoff_total_mark_net'])}`（Δ=`{_fmt(top['delta_cutoff_mark_vs_baseline'])}`）。"
        f"**不確定性**：K 線歧義、持倉延長重疊未建模、樣本僅數小時。"
    )
    # entry suggestion
    ent = entry_res.get("summary") or []
    if ent:
        best_e = max(ent, key=lambda x: x.get("sum_net_per_signal") or -1e18)
        lines.append(
            f"2. 進場距離：同訊號集合下 offset=`{best_e['offset_pct']}` 每訊號淨合計=`{_fmt(best_e['sum_net_per_signal'])}`"
            f"（成交率=`{_fmt(best_e['fill_rate'],3)}`）。若優於 0.5%，下一輪只改距離、退出鎖定 50/75。"
        )
    lines.append("")
    lines.append("## 8. 報表／日報已知問題（不阻斷回放）")
    lines.append("- 日報可能殘留舊實驗啟動時間／舊累計敘事；本包以指定 limit0p5 run + 08:16:25 截止重建。")
    lines.append("- 「管理完成間隔=ok」與完成態語義不一致；本包不依賴該欄。")
    lines.append("")
    lines.append("## 9. 產物檔案")
    lines.append("- `matrix_summary.csv` / `matrix_trades.csv` / `baseline_trades.csv`")
    lines.append("- `milestones.csv` / `conversion.csv` / `entry_signals.csv` / `entry_summary.csv`")
    lines.append("- `meta.json`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_one(label: str, profile: str, runs, expected, out_dir: Path) -> None:
    root = setup_root()
    acc = root / "fixed_data_root" / "output" / profile
    print(f"=== {label} rebuild ===")
    rebuilt = rebuild_trades(acc / "fills.jsonl", acc / "account_state.json", set(runs), cutoff=CUTOFF)
    print(
        f"  closed={rebuilt['n_closed']} open={rebuilt['n_open']} sum={rebuilt['sum_complete_net']:.6f} "
        f"target={expected['sum_net']}"
    )
    print(f"=== {label} exit matrix ===")
    exit_res = run_exit_matrix(root, profile, rebuilt["samples"], cutoff=CUTOFF)
    print(f"=== {label} entry exp ===")
    entry_res = run_entry_experiments(
        root,
        profile,
        queued=rebuilt["queued"],
        filters=rebuilt["filters"],
        runs=set(runs),
        cutoff=CUTOFF,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "baseline_trades.csv", exit_res["baseline_trade_rows"])
    write_csv(out_dir / "matrix_trades.csv", exit_res["matrix_trade_rows"])
    write_csv(out_dir / "matrix_summary.csv", exit_res["matrix_summary"])
    write_csv(out_dir / "milestones.csv", exit_res["milestones"])
    conv = conversion_tables(rebuilt["samples"], exit_res["matrix_trade_rows"])["conversion_rows"]
    write_csv(out_dir / "conversion.csv", conv)
    write_csv(out_dir / "entry_signals.csv", entry_res.get("rows") or [])
    write_csv(out_dir / "entry_summary.csv", entry_res.get("summary") or [])
    atr_d = atr_offset_diag(rebuilt["samples"])
    write_csv(out_dir / "atr_offset_ratios.csv", atr_d.get("rows") or [])
    meta = {
        "label": label,
        "profile": profile,
        "runs": list(runs),
        "cutoff": CUTOFF.isoformat(),
        "rebuild": {
            "n_closed": rebuilt["n_closed"],
            "n_open": rebuilt["n_open"],
            "sum_complete_net": rebuilt["sum_complete_net"],
            "expected": expected,
            "open_trade_ids": [
                {
                    "trade_id": s.trade_id,
                    "close_after_cutoff": s.close_after_cutoff,
                    "close_ts": s.close_ts.isoformat() if s.close_ts else None,
                    "actual_exit_reason": s.actual_exit_reason,
                }
                for s in rebuilt["open"]
            ],
        },
        "assumptions": {
            "price_path": "1m_ohlc_kline_approx",
            "fee_rate_one_side": FEE_RATE_ONE_SIDE,
            "slippage_adverse_on_exit": SLIPPAGE_ADVERSE,
            "same_bar_rule": "conservative=SL_wins; optimistic=TP_wins",
            "stage1": "fixed_actual_entries_independent_exit_sensitivity",
        },
        "entry": {
            "coverage": entry_res.get("coverage"),
            "assumptions": entry_res.get("assumptions"),
            "wait_diag": entry_res.get("wait_diag_offset_0p5"),
        },
        "atr_offset": {k: atr_d[k] for k in atr_d if k != "rows"},
        "bars_coverage_n": exit_res.get("bars_coverage"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out_dir / "REPORT.md", label, profile, rebuilt, exit_res, entry_res, expected)
    print(f"  wrote {out_dir}")


def main() -> int:
    run_one("B", PROFILE_B, RUNS_B, EXPECTED_B, ROOT / "out_B")
    run_one("C", PROFILE_C, RUNS_C, EXPECTED_C, ROOT / "out_C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
