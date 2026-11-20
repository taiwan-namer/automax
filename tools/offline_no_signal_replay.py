#!/usr/bin/env python3
"""
離線 NO_SIGNAL 子條件重現（不開倉、不寫 live decision_trace、不改帳本）。

標記規則：
  - 若僅有 decision_time + vault 現況 + personality 快照 → 「離線重建診斷」
  - 無法取得當時完整輸入 → UNKNOWN
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from fetcher_and_analyzer import load_klines_from_vault
from live_paper_trader import (
    INTERVAL_MS,
    SCHOOL_SPIKE_FADE,
    SCHOOL_SPIKE_MOMO,
    _closed_klines,
    detect_spike_fade_1m,
    detect_spike_momo_1m,
)
from signal_reject_diag import append_reject_diag_jsonl, begin_probe, take_probe


def _parse_decision_time_ms(s: str) -> Optional[int]:
    """'2026-11-18 21:29:02' → ms（本地牆鐘）。"""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _load_evals(trace_path: Path, symbols: Optional[set[str]] = None) -> list[dict[str, Any]]:
    rows = []
    with trace_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("event") != "SIGNAL_EVAL":
                continue
            if obj.get("block_reason") != "NO_SIGNAL":
                continue
            school = obj.get("evaluated_school") or ""
            if school not in (SCHOOL_SPIKE_FADE, SCHOOL_SPIKE_MOMO):
                continue
            if symbols and obj.get("symbol") not in symbols:
                continue
            rows.append(obj)
    return rows


def replay_one(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    school = str(row.get("evaluated_school") or "")
    decision_time = str(row.get("decision_time") or "")
    now_ms = _parse_decision_time_ms(decision_time)
    snap = row.get("personality_snapshot") or {}
    ctx = {
        "price_position_pct": float(snap.get("price_position_pct") or 50.0),
        "price_change_pct_24h": float(snap.get("price_change_pct_24h") or 0.0),
        "change_24h_pct": float(snap.get("price_change_pct_24h") or 0.0),
    }
    out: dict[str, Any] = {
        "mode": "離線重建診斷",
        "symbol": symbol,
        "school": school,
        "decision_time": decision_time,
        "now_ms": now_ms,
        "routing_recent_volume_ratio": snap.get("recent_volume_ratio"),
        "gaps": [],
    }
    if now_ms is None:
        out["gaps"].append("decision_time_unparseable")
        out["first_reject_code"] = "UNKNOWN"
        return out

    df = load_klines_from_vault(symbol, "1m")
    if df is None or df.empty:
        out["gaps"].append("vault_1m_missing")
        out["first_reject_code"] = "UNKNOWN"
        return out

    # 截斷到掃描當下可見的 open_time（避免用到之後才寫入的未來根——若有）
    df_cut = df[df["open_time"].astype("int64") <= now_ms].copy()
    if df_cut.empty:
        out["gaps"].append("no_bars_at_or_before_scan")
        out["first_reject_code"] = "UNKNOWN"
        return out

    # 標記：vault 為事後狀態，非當下 API 快照
    out["gaps"].append("vault_is_posthoc_not_live_api_snapshot")
    out["input_last_bar_open_time"] = int(df_cut["open_time"].iloc[-1])
    closed = _closed_klines(df_cut, "1m", now_ms=now_ms)
    out["bars_raw"] = len(df_cut)
    out["bars_closed"] = len(closed)
    if not df_cut.empty:
        last_ot = int(df_cut["open_time"].iloc[-1])
        if last_ot + INTERVAL_MS["1m"] > now_ms:
            out["unclosed_excluded"] = True
            out["excluded_open_time"] = last_ot
        else:
            out["unclosed_excluded"] = False

    begin_probe(
        school=school,
        symbol=symbol,
        scan_time=decision_time,
        ctx=ctx,
        extra={
            "mode": "離線重建診斷",
            "routing_recent_volume_ratio": snap.get("recent_volume_ratio"),
        },
    )
    if school == SCHOOL_SPIKE_MOMO:
        sig = detect_spike_momo_1m(df_cut, ctx=ctx, now_ms=now_ms)
    else:
        sig = detect_spike_fade_1m(df_cut, ctx=ctx, now_ms=now_ms)
    probe = take_probe() or {}
    out["first_reject_code"] = probe.get("first_reject_code")
    out["result"] = probe.get("result")
    out["signal"] = None if sig is None else {"side": sig["side"], "entry": sig["entry"]}
    out["vol_metric"] = probe.get("vol_metric")
    out["conditions"] = probe.get("conditions")
    out["signal_bar_open_time"] = probe.get("signal_bar_open_time")
    out["excluded_unclosed_open_times"] = probe.get("excluded_unclosed_open_times")

    append_reject_diag_jsonl(
        {"source": "offline_rebuild", **out, **{k: probe.get(k) for k in ("ctx",)}},
        path=ROOT / "output" / "signal_reject_offline_rebuild.jsonl",
    )
    return out


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_school: dict[str, Counter] = defaultdict(Counter)
    by_sym: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        code = r.get("first_reject_code") or "UNKNOWN"
        by_school[str(r.get("school"))][code] += 1
        by_sym[str(r.get("symbol"))][code] += 1
    return {
        "n": len(results),
        "by_school": {k: dict(v) for k, v in by_school.items()},
        "by_symbol": {k: dict(v) for k, v in by_sym.items()},
    }


def main() -> int:
    trace = ROOT / "output" / "decision_trace_20261118.jsonl"
    if not trace.exists():
        print("missing decision_trace")
        return 1
    focus = {"MMTUSDT", "PIEVERSEUSDT", "PIPPINUSDT"}
    # 全量 FADE/MOMO NO_SIGNAL + 焦點明細
    all_rows = _load_evals(trace)
    focus_rows = [r for r in all_rows if r.get("symbol") in focus]
    mmt = [r for r in all_rows if r.get("symbol") == "MMTUSDT"]
    print(f"total FADE/MOMO NO_SIGNAL rows: {len(all_rows)}")
    print(f"MMT rows: {len(mmt)} focus rows: {len(focus_rows)}")

    # 優先完整重現 MMT；FADE 抽樣最多 40
    replay_targets = list(mmt)
    fade_sample = [r for r in all_rows if r.get("evaluated_school") == SCHOOL_SPIKE_FADE][:40]
    # 焦點其餘
    for r in focus_rows:
        if r not in replay_targets:
            replay_targets.append(r)
    for r in fade_sample:
        if r not in replay_targets:
            replay_targets.append(r)

    results = [replay_one(r) for r in replay_targets]
    summary = summarize(results)
    out_path = ROOT / "output" / "no_signal_offline_summary.json"
    out_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
