from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .timeutil import parse_iso


def _initial_risk_u(fill: Dict[str, Any]) -> Optional[float]:
    try:
        px = float(fill["price"])
        qty = float(fill["qty"])
        stop = fill.get("stop0") or fill.get("initial_stop") or fill.get("stop")
        if stop is None:
            return None
        stop = float(stop)
        side = (fill.get("side") or "").upper()
        if side in ("LONG", "BUY"):
            risk = (px - stop) * qty
        else:
            risk = (stop - px) * qty
        return abs(risk)
    except Exception:
        return None


def _roundtrip_fee_u(fills: List[Dict[str, Any]]) -> float:
    return sum(float(f.get("fee_u") or 0) for f in fills)


def candidate_cost_burden(complete_trades: List[Dict[str, Any]], by_tid_fills: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Cost vs initial risk — subset diagnosis on existing fills only."""
    samples = []
    for t in complete_trades:
        tid = t["trade_id"]
        fills = by_tid_fills.get(tid) or []
        open_f = next((f for f in fills if f.get("event") == "OPEN"), None)
        if not open_f:
            continue
        risk = _initial_risk_u(open_f)
        fees = _roundtrip_fee_u(fills)
        if risk is None or risk <= 0:
            samples.append({"trade_id": tid, "status": "NA_risk", "fees_u": fees})
            continue
        samples.append(
            {
                "trade_id": tid,
                "symbol": t.get("symbol"),
                "fees_u": fees,
                "initial_risk_u": risk,
                "fee_to_risk": fees / risk,
                "complete_net_u": t.get("complete_net_u"),
                "fee_source": "paper fills fee_u (simulator schedule) — NOT live account fee tier",
            }
        )
    ratios = [s["fee_to_risk"] for s in samples if "fee_to_risk" in s]
    n = len(ratios)
    summary = {
        "n_with_risk": n,
        "n_total": len(complete_trades),
        "median_fee_to_risk": sorted(ratios)[n // 2] if n else None,
        "mean_fee_to_risk": (sum(ratios) / n) if n else None,
        "pct_fee_to_risk_gt_0_25": (sum(1 for r in ratios if r > 0.25) / n) if n else None,
        "pct_fee_to_risk_gt_0_40": (sum(1 for r in ratios if r > 0.40) / n) if n else None,
    }
    candidate = {
        "id": "CAND_COST_THRESHOLD",
        "title": "成本門檻／進場與目標相容性",
        "evidence": summary,
        "samples_preview": samples[:20],
        "proposal": "若 fee/initial_risk 偏高，候選為：提高最小風險距離或名義門檻、或要求目標R與費用相容；不以放寬止損稀释比例。",
        "not_approved": True,
        "vs_mgmt_fix": "執行可靠性修復獨立；本項為進場策略條件候選",
        "offline_compare": "條件子集診斷：在既有成交上篩 fee/risk 超門檻者的淨利分布；非完整回測（跳過單會改變資金與後續機會）",
        "data_gaps": ["真實帳戶費率未核對", "滑價僅紙上模型", "無完整未進場拒絕樣本"],
        "worth_next_stage": bool(n and summary["median_fee_to_risk"] is not None and summary["median_fee_to_risk"] >= 0.20),
        "insufficient": n < 5,
    }
    return candidate


def candidate_batch_concentration(open_fills_in_day: List[Dict[str, Any]], window_sec: float = 30.0) -> Dict[str, Any]:
    opens = []
    for f in open_fills_in_day:
        if f.get("event") != "OPEN":
            continue
        ts = parse_iso(f.get("recorded_at"))
        if ts is None:
            continue
        opens.append((ts, f))
    opens.sort(key=lambda x: x[0])
    clusters = []
    i = 0
    while i < len(opens):
        t0, _ = opens[i]
        group = []
        j = i
        while j < len(opens) and (opens[j][0] - t0).total_seconds() <= window_sec:
            group.append(opens[j][1])
            j += 1
        by_side: Dict[str, List] = defaultdict(list)
        for f in group:
            by_side[(f.get("side") or "").upper()].append(f)
        for side, fs in by_side.items():
            if len(fs) < 2:
                continue
            notional = 0.0
            risks = []
            for f in fs:
                try:
                    notional += abs(float(f["price"]) * float(f["qty"]))
                except Exception:
                    pass
                r = _initial_risk_u(f)
                if r is not None:
                    risks.append(r)
            clusters.append(
                {
                    "t0": t0.isoformat(),
                    "side": side,
                    "n": len(fs),
                    "symbols": [f.get("symbol") for f in fs],
                    "notional_u": notional,
                    "initial_risk_sum_u": sum(risks) if risks else None,
                    "window_sec": window_sec,
                }
            )
        i = j if j > i else i + 1

    return {
        "id": "CAND_BATCH_SAME_DIR",
        "title": "同批同向集中曝險限制",
        "evidence": {"n_clusters": len(clusters), "clusters": clusters[:15], "window_sec": window_sec},
        "proposal": "候選：每批風險預算、評分排序取捨、同向名義／初始風險上限。保留決策只能用當時資訊，不可事後挑贏家。",
        "not_approved": True,
        "vs_mgmt_fix": "與管理逾時／報價延遲分開；集中曝險是進場政策",
        "offline_compare": "條件子集：對同批同向>N的集群，比較『全留』vs『僅留當時最高分K檔』的既有成交淨利（標記為診斷）",
        "data_gaps": ["當時排序分數若未落盤則無法公正重放取捨", "跳過單改變後續可開倉集合"],
        "worth_next_stage": len(clusters) >= 1,
        "insufficient": len(opens) < 3,
    }


def candidate_breakout_quality(
    complete_trades: List[Dict[str, Any]],
    by_tid_fills: Dict[str, List[Dict[str, Any]]],
    post_entry_labels: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Breakout quality on ALL qualifying trades; 1–5m post returns are labels only."""
    n = len(complete_trades)
    by_exit = defaultdict(int)
    nets = []
    for t in complete_trades:
        by_exit[str(t.get("exit_reason") or "NA")] += 1
        if t.get("complete_net_u") is not None:
            nets.append(float(t["complete_net_u"]))
    labels_note = "進場後1/3/5m報酬僅作結果標籤，不可當進場特徵；10m/15m濾鏡僅用當時已完整收盤K"
    return {
        "id": "CAND_BREAKOUT_QUALITY",
        "title": "突破品質濾網（完整樣本）",
        "evidence": {
            "n_complete": n,
            "exit_reason_counts": dict(by_exit),
            "sum_net_u": sum(nets) if nets else None,
            "post_entry_labels_attached": bool(post_entry_labels),
            "note": labels_note,
        },
        "proposal": "候選：訊號棒實體／事前趨勢強度／波動分位門檻；僅用決策時已閉合K。對全部符合條件成交做子集診斷，不挑五筆故事。",
        "not_approved": True,
        "vs_mgmt_fix": "與 stop 超額／book 逾時分開；品質濾網不替代管理修復",
        "offline_compare": "若有1m vault：以開倉時間向前取已閉合10m/15m特徵，標記可否通過候選濾網；比較通過/未通過子集淨利。禁止用1m OHLC捏造精確book成交路徑。",
        "data_gaps": [
            "完整回測需宇宙掃描拒絕日誌與當時評分快照",
            "無拒絕樣本時只能做成交條件子集診斷",
            "資料不足則不虛構完整策略回測數字",
        ],
        "worth_next_stage": n >= 5,
        "insufficient": n < 5,
        "param_search": "禁止大量搜參；預先少量候選，保留後續日期作樣本外",
    }


def build_strategy_candidates(
    day_stats,
    by_tid_fills: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    open_events = [f for f in day_stats.segment_events if f.get("event") == "OPEN"]
    c1 = candidate_cost_burden(day_stats.complete_closed_today, by_tid_fills)
    c2 = candidate_batch_concentration(open_events)
    c3 = candidate_breakout_quality(day_stats.complete_closed_today, by_tid_fills)
    # rank: worth_next_stage first
    cands = [c1, c2, c3]
    cands.sort(key=lambda c: (not c.get("worth_next_stage"), c.get("insufficient", True)))
    return cands[:3]
