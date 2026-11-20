#!/usr/bin/env python3
"""Offline research pack builder for paper_bulk_test_v1.

Read-only vs live paper: never writes account/sealed/vault originals.
Safe JSONL reads skip incomplete trailing lines and record cutoffs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SETUP = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup")
ACCOUNT = SETUP / "fixed_data_root/output/paper_bulk_test_v1"
VAULT = SETUP / "fixed_data_root/data/paper_bulk_test_v1/kline_vault_v2_clean"
TZ = timezone(timedelta(hours=8))
PROFILE = "paper_bulk_test_v1"

# Reuse daily-review helpers without importing live package
sys.path.insert(0, str(SETUP / "tools/paper_daily_review"))
from paper_daily_review.jsonl_io import dedupe_fills, read_jsonl_bounded  # type: ignore
from paper_daily_review.trades import (  # type: ignore
    complete_trade_net,
    group_by_trade_id,
    open_fee_component,
    trade_is_complete,
    trade_is_open,
)


def now_tp() -> datetime:
    return datetime.now(TZ)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now_tp()).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    r = read_jsonl_bounded(path)
    meta = {
        "path": str(path),
        "exists": path.exists(),
        "bytes_read": r.bytes_read,
        "lines_ok": r.lines_ok,
        "truncated_tail": r.truncated_tail,
        "parse_errors": r.parse_errors,
        "mtime": r.mtime,
        "cutoff_note": "read file size at open; incomplete trailing line skipped if present",
    }
    return r.rows, meta


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fieldnames})


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def version_bucket(run_id: str, code_tag: Optional[str], cwd: Optional[str]) -> str:
    """Map run to coarse strategy epoch for analysis."""
    if cwd and "160723" in cwd:
        return "C_dl5000_batchside1"
    if cwd and "155304" in cwd:
        return "B_mgmt_reliability"
    if code_tag and "dl5000_batchside" in code_tag:
        return "C_dl5000_batchside1"
    if code_tag and "mgmt_reliability" in (code_tag or ""):
        return "B_mgmt_reliability"
    # early runs / 122904 era
    if run_id >= "20261119T155304":
        # ambiguous without cwd
        if run_id >= "20261119T160723":
            return "C_dl5000_batchside1"
        if run_id >= "20261119T155304":
            return "B_mgmt_reliability"
    return "A_pre_mgmt_dl2000"


def main() -> int:
    stamp = now_tp().strftime("%Y%m%d_%H%M%S")
    out = SETUP / "reports" / "research" / stamp
    snap = out / "_snapshot"
    out.mkdir(parents=True, exist_ok=True)
    snap.mkdir(parents=True, exist_ok=True)

    build_meta: Dict[str, Any] = {
        "built_at": iso(),
        "timezone": "Asia/Taipei",
        "profile": PROFILE,
        "tool": "tools/research_pack_export/build_research_pack.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "live_paper_untouched": True,
        "sources": [],
        "truncated_tails": [],
        "parse_errors": [],
        "notes": [],
    }

    # --- Live process check (read-only) ---
    pid_path = ACCOUNT / "paper.pid"
    live_pid = pid_path.read_text().strip() if pid_path.exists() else None
    build_meta["live_pid_at_build"] = live_pid

    # --- Snapshot account (copy bytes once) ---
    acct_bytes = (ACCOUNT / "account_state.json").read_bytes()
    (snap / "account_state.json").write_bytes(acct_bytes)
    account = json.loads(acct_bytes)
    build_meta["account_sha256"] = sha256_bytes(acct_bytes)

    # --- Inventory runs ---
    run_dirs = sorted([p for p in (ACCOUNT / "runs").iterdir() if p.is_dir()], key=lambda p: p.name)
    inventory_rows = []
    for rd in run_dirs:
        launch = load_json(rd / "launch.json") if (rd / "launch.json").exists() else {}
        cfg = load_json(rd / "config_snapshot.json") if (rd / "config_snapshot.json").exists() else {}
        hashes = load_json(rd / "code_hashes.json") if (rd / "code_hashes.json").exists() else {}
        counts = {}
        for name in (
            "fills.jsonl",
            "decision_trace.jsonl",
            "manage_events.jsonl",
            "quote_events.jsonl",
            "position_events.jsonl",
            "equity_snapshots.jsonl",
        ):
            p = rd / name
            if p.exists():
                rows, meta = safe_jsonl(p)
                counts[name] = meta["lines_ok"]
                build_meta["sources"].append({"run_id": rd.name, **meta})
                if meta["truncated_tail"]:
                    build_meta["truncated_tails"].append(str(p))
            else:
                counts[name] = None
        inventory_rows.append(
            {
                "path": str(rd),
                "profile": launch.get("profile") or cfg.get("PROFILE_ID") or PROFILE,
                "run_id": rd.name,
                "started_hint": rd.name.split("_")[0],
                "timezone": "Asia/Taipei",
                "cwd": launch.get("cwd"),
                "code_fix_tag": cfg.get("CODE_FIX_TAG") or launch.get("code_fix_tag"),
                "daily_loss_lock_usdt": cfg.get("DAILY_LOSS_LOCK_USDT"),
                "engine_sha256": (hashes.get("files") or hashes).get("engine.py")
                if isinstance(hashes.get("files"), dict)
                else hashes.get("engine.py"),
                "version_bucket": version_bucket(
                    rd.name, cfg.get("CODE_FIX_TAG"), launch.get("cwd")
                ),
                "counts": counts,
            }
        )

    # Root files
    root_fills, rf_meta = safe_jsonl(ACCOUNT / "fills.jsonl")
    root_eq, re_meta = safe_jsonl(ACCOUNT / "equity_snapshots.jsonl")
    root_dec, rd_meta = safe_jsonl(ACCOUNT / "decision_trace.jsonl")
    for m in (rf_meta, re_meta, rd_meta):
        build_meta["sources"].append({"run_id": "ROOT", **m})
        if m.get("truncated_tail"):
            build_meta["truncated_tails"].append(m["path"])

    # Prefer root fills + dedupe with per-run
    all_fills = []
    for f in root_fills:
        f = dict(f)
        f["_source"] = "root"
        all_fills.append(f)
    for rd in run_dirs:
        p = rd / "fills.jsonl"
        if not p.exists():
            continue
        rows, meta = safe_jsonl(p)
        for f in rows:
            f = dict(f)
            f["_source"] = f"run:{rd.name}"
            all_fills.append(f)
    fills, n_dup = dedupe_fills(all_fills)
    build_meta["fills_dup_removed"] = n_dup
    build_meta["fills_after_dedupe"] = len(fills)

    # Copy fills snapshot for provenance
    (snap / "fills_deduped.jsonl").write_text(
        "\n".join(json.dumps(f, ensure_ascii=False, default=str) for f in fills) + "\n",
        encoding="utf-8",
    )

    # Decisions: merge root + runs (tag source); for candidates prefer run-local richness
    decisions: List[Dict[str, Any]] = []
    for label, rows in [("ROOT", root_dec)] + [
        (
            f"run:{rd.name}",
            safe_jsonl(rd / "decision_trace.jsonl")[0] if (rd / "decision_trace.jsonl").exists() else [],
        )
        for rd in run_dirs
    ]:
        for o in rows:
            o = dict(o)
            o["_source"] = label
            decisions.append(o)

    # Manage events
    manage_rows: List[Dict[str, Any]] = []
    for rd in run_dirs:
        p = rd / "manage_events.jsonl"
        if not p.exists():
            continue
        rows, meta = safe_jsonl(p)
        for o in rows:
            o = dict(o)
            o["_source_run"] = rd.name
            manage_rows.append(o)

    # Quote events (bounded: only keep failure / age fields summaries later; export sample + key cols)
    quote_rows: List[Dict[str, Any]] = []
    for rd in run_dirs:
        p = rd / "quote_events.jsonl"
        if not p.exists():
            continue
        rows, meta = safe_jsonl(p)
        for o in rows:
            o = dict(o)
            o["_source_run"] = rd.name
            quote_rows.append(o)

    # Position events
    pos_events: List[Dict[str, Any]] = []
    for rd in run_dirs:
        p = rd / "position_events.jsonl"
        if not p.exists():
            continue
        rows, _ = safe_jsonl(p)
        for o in rows:
            o = dict(o)
            o["_source_run"] = rd.name
            pos_events.append(o)

    # Seals / switch evidence
    seals = []
    for sp in sorted(SETUP.glob("fix_source_sealed_*/SEAL_META.json")):
        seals.append(load_json(sp))
    pointer = load_json(SETUP / "LAUNCH_SEAL_POINTER.json") if (SETUP / "LAUNCH_SEAL_POINTER.json").exists() else {}
    switch_logs = list((SETUP / "launch_logs").glob("switch_verify_*.json")) + list(
        (SETUP / "launch_logs").glob("post_stop_*.json")
    ) + list((SETUP / "launch_logs").glob("pre_stop_*.json"))

    # ========== 02_version_timeline.csv ==========
    timeline = []
    # seals as version markers
    for m in seals:
        timeline.append(
            {
                "profile": PROFILE,
                "event_type": "SEAL_CREATED",
                "stamp": m.get("stamp"),
                "run_id": "",
                "code_fix_tag": m.get("tag"),
                "aggregate_sha256": m.get("aggregate_sha256"),
                "version_bucket": version_bucket(
                    m.get("stamp") or "", m.get("tag"), m.get("new_runtime")
                ),
                "start_ts": m.get("created_at"),
                "end_ts": "",
                "change_summary": json.dumps(
                    {
                        "changed_py": m.get("changed_py"),
                        "approved_scope": m.get("approved_scope"),
                        "strategy_changes": m.get("strategy_changes"),
                    },
                    ensure_ascii=False,
                ),
                "gap_measure_limit": "",
                "notes": "",
            }
        )
    for inv in inventory_rows:
        timeline.append(
            {
                "profile": PROFILE,
                "event_type": "RUN_START",
                "stamp": "",
                "run_id": inv["run_id"],
                "code_fix_tag": inv.get("code_fix_tag"),
                "aggregate_sha256": inv.get("engine_sha256"),
                "version_bucket": inv["version_bucket"],
                "start_ts": inv["started_hint"],
                "end_ts": "",
                "change_summary": json.dumps(
                    {
                        "cwd": inv.get("cwd"),
                        "daily_loss_lock_usdt": inv.get("daily_loss_lock_usdt"),
                        "counts": inv.get("counts"),
                    },
                    ensure_ascii=False,
                ),
                "gap_measure_limit": "",
                "notes": "start time inferred from run_id stamp (Asia/Taipei local naming); not wall-clock precision of first manage complete",
            }
        )
    # switch events from pointer / logs
    timeline.append(
        {
            "profile": PROFILE,
            "event_type": "SWITCH_ACTIVE",
            "stamp": pointer.get("stamp"),
            "run_id": pointer.get("active_run"),
            "code_fix_tag": pointer.get("tag"),
            "aggregate_sha256": pointer.get("aggregate"),
            "version_bucket": "C_dl5000_batchside1",
            "start_ts": pointer.get("switched_at"),
            "end_ts": "",
            "change_summary": json.dumps(
                {
                    "previous_runtime": pointer.get("previous_runtime"),
                    "management_gap_sec_stop_to_stable_start": pointer.get(
                        "management_gap_sec_stop_to_stable_start"
                    ),
                    "failed_ephemeral_start_pid": pointer.get("failed_ephemeral_start_pid"),
                    "daily_loss_lock_usdt": pointer.get("daily_loss_lock_usdt"),
                },
                ensure_ascii=False,
            ),
            "gap_measure_limit": "gap is stop→stable_start wall clock; NOT quote_fetched_at→MANAGE_BATCH_COMPLETE; first ephemeral start died mid-warmup",
            "notes": pointer.get("note"),
        }
    )
    # historical switch 155304
    p155 = SETUP / "launch_logs/switch_verify_20261119T155304.json"
    if p155.exists():
        j = load_json(p155)
        timeline.append(
            {
                "profile": PROFILE,
                "event_type": "SWITCH_VERIFY",
                "stamp": "20261119T155304",
                "run_id": j.get("run_id") or "",
                "code_fix_tag": j.get("tag") or "fix_20261119_mgmt_reliability_v1",
                "aggregate_sha256": j.get("seal_aggregate"),
                "version_bucket": "B_mgmt_reliability",
                "start_ts": j.get("ts") or "",
                "end_ts": "",
                "change_summary": json.dumps(j, ensure_ascii=False)[:4000],
                "gap_measure_limit": "do not treat quote→complete as exact zero-gap; old sealed lacked complete events",
                "notes": j.get("note_tag") or j.get("note"),
            }
        )

    write_csv(
        out / "02_version_timeline.csv",
        timeline,
        [
            "profile",
            "event_type",
            "stamp",
            "run_id",
            "code_fix_tag",
            "aggregate_sha256",
            "version_bucket",
            "start_ts",
            "end_ts",
            "change_summary",
            "gap_measure_limit",
            "notes",
        ],
    )

    # ========== 03_trades.csv / 04_trade_events.csv ==========
    by_tid = group_by_trade_id(fills)
    # map run -> version
    run_ver = {i["run_id"]: i for i in inventory_rows}

    trade_rows = []
    event_rows = []
    for tid, flist in sorted(by_tid.items()):
        opens = [f for f in flist if f.get("event") == "OPEN"]
        closes = [f for f in flist if f.get("event") == "CLOSE"]
        partials = [f for f in flist if f.get("event") == "CLOSE_PARTIAL"]
        o0 = opens[0] if opens else {}
        c_last = closes[-1] if closes else {}
        runs = sorted({str(f.get("run_id")) for f in flist if f.get("run_id")})
        open_run = str(o0.get("run_id") or "")
        close_run = str(c_last.get("run_id") or "")
        open_ver = run_ver.get(open_run, {})
        close_ver = run_ver.get(close_run, {})
        complete = trade_is_complete(flist)
        still_open = trade_is_open(flist)
        net = complete_trade_net(flist)
        fees = sum(float(f.get("fee_u") or 0) for f in flist)
        gross = None
        if net is not None:
            gross = net + fees  # approx: net already fee-inclusive; keep both
        # signal / batch from decisions
        sk = o0.get("signal_key")
        scan_batch_id = ""
        # find OPEN SIGNAL_EVAL
        for d in decisions:
            if d.get("event") != "SIGNAL_EVAL":
                continue
            if d.get("trade_id") == tid or (sk and d.get("signal_key") == sk and d.get("final_action") == "OPEN"):
                scan_batch_id = d.get("scan_batch_id") or scan_batch_id
                if d.get("trade_id") == tid:
                    break
        open_day = (o0.get("recorded_at") or "")[:10]
        close_day = (c_last.get("recorded_at") or "")[:10] if c_last else ""
        cross_day = bool(open_day and close_day and open_day != close_day)
        cross_ver = bool(open_ver.get("version_bucket") and close_ver.get("version_bucket") and open_ver.get("version_bucket") != close_ver.get("version_bucket"))
        integrity = "complete" if complete else ("open_no_close" if still_open else "incomplete_missing_open_or_close")
        if len(opens) != 1:
            integrity = f"anomaly_open_count_{len(opens)};" + integrity

        init_stop = o0.get("initial_stop")
        entry = float(o0.get("price") or 0)
        qty = float(o0.get("qty") or 0)
        side = o0.get("side")
        init_risk = None
        if init_stop is not None and entry and qty:
            if side == "LONG":
                init_risk = abs(entry - float(init_stop)) * qty
            elif side == "SHORT":
                init_risk = abs(float(init_stop) - entry) * qty

        trade_rows.append(
            {
                "profile": o0.get("profile_id") or PROFILE,
                "trade_id": tid,
                "symbol": o0.get("symbol") or (c_last.get("symbol")),
                "side": side,
                "open_ts": o0.get("recorded_at"),
                "close_ts": c_last.get("recorded_at") if c_last else "",
                "open_version_bucket": open_ver.get("version_bucket") or version_bucket(open_run, o0.get("code_fix_tag"), None),
                "close_version_bucket": close_ver.get("version_bucket") if close_run else ("OPEN" if still_open else ""),
                "open_run_id": open_run,
                "close_run_id": close_run,
                "runs_involved": "|".join(runs),
                "qty_open": qty,
                "entry_price": entry,
                "notional_u": o0.get("notional_u"),
                "margin_u": o0.get("margin_u"),
                "initial_stop": init_stop,
                "initial_risk_u_est": init_risk,
                "atr_at_open": o0.get("atr"),
                "partial_exit_n": len(partials),
                "final_exit_reason": c_last.get("exit_reason") if c_last else "",
                "gross_u_approx": gross,
                "fees_u": fees,
                "net_u": net,
                "cross_day": cross_day,
                "cross_version": cross_ver,
                "is_open": still_open,
                "data_integrity": integrity,
                "scan_batch_id": scan_batch_id,
                "signal_key": sk or "",
                "signal_open_time": o0.get("signal_open_time"),
                "source_tags": o0.get("source_tags"),
                "fee_rate_one_side": o0.get("fee_rate_one_side"),
                "slippage_adverse": o0.get("slippage_adverse"),
                "simulation_assumption": o0.get("simulation_assumption"),
                "notes": "gross_u_approx = net_u + fees_u (fee-inclusive nets); not a separate ledger gross",
            }
        )

        for f in flist:
            event_rows.append(
                {
                    "trade_id": tid,
                    "event": f.get("event"),
                    "run_id": f.get("run_id"),
                    "profile": f.get("profile_id") or PROFILE,
                    "symbol": f.get("symbol"),
                    "side": f.get("side"),
                    "ts": f.get("recorded_at"),
                    "qty": f.get("qty"),
                    "price": f.get("price"),
                    "fee_u": f.get("fee_u"),
                    "gross_u": f.get("gross_u"),
                    "net_u": f.get("net_u") if f.get("event") != "OPEN" else open_fee_component(f),
                    "exit_reason": f.get("exit_reason"),
                    "book_ts": f.get("book_ts"),
                    "cash_delta_u": f.get("cash_delta_u"),
                    "initial_stop": f.get("initial_stop"),
                    "margin_u": f.get("margin_u"),
                    "notional_u": f.get("notional_u"),
                    "signal_key": f.get("signal_key"),
                    "source": f.get("_source"),
                    "proven": "from_fills_jsonl",
                }
            )

    # Add stop-set proven events if present in position_events beyond OPEN/CLOSE
    for pe in pos_events:
        if pe.get("event") in ("OPEN", "CLOSE", "CLOSE_PARTIAL"):
            continue
        event_rows.append(
            {
                "trade_id": pe.get("trade_id"),
                "event": pe.get("event") or pe.get("event_type"),
                "run_id": pe.get("_source_run") or pe.get("run_id"),
                "profile": PROFILE,
                "symbol": pe.get("symbol"),
                "side": pe.get("side"),
                "ts": pe.get("recorded_at") or pe.get("ts"),
                "qty": pe.get("qty"),
                "price": pe.get("price"),
                "fee_u": pe.get("fee_u"),
                "gross_u": "",
                "net_u": pe.get("net_u"),
                "exit_reason": pe.get("reason") or pe.get("exit_reason"),
                "book_ts": pe.get("book_ts"),
                "cash_delta_u": "",
                "initial_stop": pe.get("initial_stop"),
                "margin_u": "",
                "notional_u": "",
                "signal_key": "",
                "source": pe.get("_source_run"),
                "proven": "from_position_events",
            }
        )

    write_csv(
        out / "03_trades.csv",
        trade_rows,
        [
            "profile",
            "trade_id",
            "symbol",
            "side",
            "open_ts",
            "close_ts",
            "open_version_bucket",
            "close_version_bucket",
            "open_run_id",
            "close_run_id",
            "runs_involved",
            "qty_open",
            "entry_price",
            "notional_u",
            "margin_u",
            "initial_stop",
            "initial_risk_u_est",
            "atr_at_open",
            "partial_exit_n",
            "final_exit_reason",
            "gross_u_approx",
            "fees_u",
            "net_u",
            "cross_day",
            "cross_version",
            "is_open",
            "data_integrity",
            "scan_batch_id",
            "signal_key",
            "signal_open_time",
            "source_tags",
            "fee_rate_one_side",
            "slippage_adverse",
            "simulation_assumption",
            "notes",
        ],
    )
    write_csv(
        out / "04_trade_events.csv",
        event_rows,
        [
            "trade_id",
            "event",
            "run_id",
            "profile",
            "symbol",
            "side",
            "ts",
            "qty",
            "price",
            "fee_u",
            "gross_u",
            "net_u",
            "exit_reason",
            "book_ts",
            "cash_delta_u",
            "initial_stop",
            "margin_u",
            "notional_u",
            "signal_key",
            "source",
            "proven",
        ],
    )

    # ========== 05_candidates.csv ==========
    # Deduplicate SIGNAL_EVAL by decision_id or (run,symbol,signal_key,ts,final_action)
    cand_rows = []
    seen_dec = set()
    for d in decisions:
        if d.get("event") not in ("SIGNAL_EVAL", "SCAN_BATCH_RANK", "SCAN_FAIL"):
            continue
        if d.get("event") == "SCAN_BATCH_RANK":
            # expand candidates list as rank rows (actual recorded)
            for c in d.get("candidates") or []:
                key = ("RANK", d.get("scan_batch_id"), c.get("symbol"), c.get("side"), c.get("rank"), d.get("run_id"))
                if key in seen_dec:
                    continue
                seen_dec.add(key)
                cand_rows.append(
                    {
                        "record_type": "SCAN_BATCH_RANK_ENTRY",
                        "feature_source": "recorded_at_decision_time",
                        "scan_batch_id": d.get("scan_batch_id"),
                        "decision_ts": d.get("ts") or d.get("recorded_at"),
                        "run_id": d.get("run_id"),
                        "symbol": c.get("symbol"),
                        "side": c.get("side"),
                        "vol_x": c.get("vol_x"),
                        "spread": c.get("spread"),
                        "rank": c.get("rank"),
                        "is_reverse_reopen": c.get("is_reverse_reopen"),
                        "sig_key": c.get("sig_key"),
                        "selected_or_rejected": "ranked_candidate_pool",
                        "reject_reason": "",
                        "final_action": "",
                        "trade_id": "",
                        "code_fix_tag": d.get("code_fix_tag"),
                        "rank_rule": d.get("rank_rule"),
                        "side_open_limit": d.get("side_open_limit"),
                        "signal_open_time": "",
                        "atr": "",
                        "range_high": "",
                        "range_low": "",
                        "est_fill": "",
                        "bars_note": "pool entry from SCAN_BATCH_RANK; empty pool means no pass-signal candidates that scan",
                        "offline_recomputed": False,
                    }
                )
            continue
        if d.get("event") == "SCAN_FAIL":
            key = ("FAIL", d.get("run_id"), d.get("symbol"), d.get("ts"), d.get("error"))
            if key in seen_dec:
                continue
            seen_dec.add(key)
            cand_rows.append(
                {
                    "record_type": "SCAN_FAIL",
                    "feature_source": "recorded_at_decision_time",
                    "scan_batch_id": d.get("scan_batch_id") or "",
                    "decision_ts": d.get("ts") or d.get("recorded_at"),
                    "run_id": d.get("run_id"),
                    "symbol": d.get("symbol"),
                    "side": "",
                    "vol_x": "",
                    "spread": "",
                    "rank": "",
                    "is_reverse_reopen": "",
                    "sig_key": "",
                    "selected_or_rejected": "scan_failed",
                    "reject_reason": d.get("error"),
                    "final_action": "SCAN_FAIL",
                    "trade_id": "",
                    "code_fix_tag": "",
                    "rank_rule": "",
                    "side_open_limit": "",
                    "signal_open_time": "",
                    "atr": "",
                    "range_high": "",
                    "range_low": "",
                    "est_fill": "",
                    "bars_note": "",
                    "offline_recomputed": False,
                }
            )
            continue
        # SIGNAL_EVAL
        did = d.get("decision_id") or (
            d.get("run_id"),
            d.get("symbol"),
            d.get("signal_key"),
            d.get("ts"),
            d.get("final_action"),
            d.get("first_reject_code"),
        )
        if did in seen_dec:
            continue
        seen_dec.add(did)
        rng = d.get("range") or {}
        cand_rows.append(
            {
                "record_type": "SIGNAL_EVAL",
                "feature_source": "recorded_at_decision_time",
                "scan_batch_id": d.get("scan_batch_id") or "",
                "decision_ts": d.get("ts") or d.get("recorded_at"),
                "run_id": d.get("run_id"),
                "symbol": d.get("symbol"),
                "side": d.get("side"),
                "vol_x": d.get("vol_x"),
                "spread": d.get("spread"),
                "rank": "",
                "is_reverse_reopen": d.get("is_reverse_reopen"),
                "sig_key": d.get("signal_key"),
                "selected_or_rejected": "selected"
                if d.get("final_action") == "OPEN"
                else ("blocked" if d.get("final_action") == "BLOCKED" else "no_entry"),
                "reject_reason": d.get("first_reject_code")
                or ",".join(d.get("block_reasons") or [])
                or d.get("reject_category"),
                "final_action": d.get("final_action"),
                "trade_id": d.get("trade_id") or "",
                "code_fix_tag": d.get("code_fix_tag"),
                "rank_rule": d.get("rank_rule") or "",
                "side_open_limit": "",
                "signal_open_time": d.get("signal_open_time"),
                "atr": d.get("atr") if d.get("atr") is not None else (d.get("range") or {}).get("atr"),
                "range_high": rng.get("high") if isinstance(rng, dict) else "",
                "range_low": rng.get("low") if isinstance(rng, dict) else "",
                "est_fill": d.get("est_fill"),
                "bars_note": "SIGNAL_EVAL rows are recorded probes; universe members without a row are NOT inventable",
                "offline_recomputed": False,
            }
        )

    write_csv(
        out / "05_candidates.csv",
        cand_rows,
        [
            "record_type",
            "feature_source",
            "scan_batch_id",
            "decision_ts",
            "run_id",
            "symbol",
            "side",
            "vol_x",
            "spread",
            "rank",
            "is_reverse_reopen",
            "sig_key",
            "selected_or_rejected",
            "reject_reason",
            "final_action",
            "trade_id",
            "code_fix_tag",
            "rank_rule",
            "side_open_limit",
            "signal_open_time",
            "atr",
            "range_high",
            "range_low",
            "est_fill",
            "bars_note",
            "offline_recomputed",
        ],
    )

    # ========== 06_klines.csv ==========
    # Include all vault 1m files (small); research copy with dedupe check
    traded_syms = sorted({t["symbol"] for t in trade_rows if t.get("symbol")})
    cand_syms = sorted({c["symbol"] for c in cand_rows if c.get("symbol")})
    focus_syms = set(traded_syms) | set(cand_syms)
    kline_rows = []
    kline_issues = []
    vault_files = sorted(VAULT.glob("*_1m.csv"))
    seen_keys = {}
    for vf in vault_files:
        sym = vf.name.replace("_1m.csv", "")
        # Prefer focus symbols; still include others but mark
        text = vf.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if not lines:
            continue
        header = lines[0].split(",")
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split(",")
            if len(parts) < 11:
                kline_issues.append({"symbol": sym, "issue": "short_row", "line": line[:120]})
                continue
            row = {
                "symbol": sym,
                "timeframe": "1m",
                "open_time": parts[0],
                "open": parts[1],
                "high": parts[2],
                "low": parts[3],
                "close": parts[4],
                "volume": parts[5],
                "close_time": parts[6],
                "quote_volume": parts[7],
                "interval": parts[8],
                "is_closed_raw": parts[9],
                "fetched_at_ms": parts[10],
                "source": str(vf),
                "in_trade_or_candidate_focus": sym in focus_syms,
                "closed_check_note": "is_closed_raw from vault; not re-fetched; verify vs close_time vs now for research",
            }
            # closed sanity: close_time should be open_time+59999 for 1m
            try:
                ot = int(parts[0])
                ct = int(parts[6])
                if ct - ot != 59999:
                    kline_issues.append(
                        {"symbol": sym, "issue": "close_time_span", "open_time": ot, "close_time": ct}
                    )
                # OHLC sanity
                o, h, l, c = map(float, parts[1:5])
                if not (l <= o <= h and l <= c <= h and l <= h):
                    kline_issues.append({"symbol": sym, "issue": "ohlc_inconsistent", "open_time": ot})
            except Exception as exc:
                kline_issues.append({"symbol": sym, "issue": f"parse:{exc}", "line": line[:80]})
            key = (sym, "1m", parts[0])
            if key in seen_keys:
                kline_issues.append(
                    {
                        "symbol": sym,
                        "issue": "duplicate_open_time",
                        "open_time": parts[0],
                        "first_source": seen_keys[key],
                        "conflict_source": str(vf),
                    }
                )
                # do not silent overwrite
                continue
            seen_keys[key] = str(vf)
            kline_rows.append(row)

    write_csv(
        out / "06_klines.csv",
        kline_rows,
        [
            "symbol",
            "timeframe",
            "open_time",
            "close_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "interval",
            "is_closed_raw",
            "fetched_at_ms",
            "source",
            "in_trade_or_candidate_focus",
            "closed_check_note",
        ],
    )
    atomic_write(
        out / "06_klines_issues.json",
        json.dumps(kline_issues[:5000], ensure_ascii=False, indent=2) + "\n",
    )

    # ========== 07_execution_quality.csv ==========
    exec_rows = []
    for m in manage_rows:
        et = m.get("event_type") or m.get("event")
        exec_rows.append(
            {
                "record_type": et,
                "run_id": m.get("_source_run") or m.get("run_id"),
                "batch_id": m.get("batch_id"),
                "attempt_id": m.get("attempt_id"),
                "purpose": m.get("purpose"),
                "phase": m.get("phase"),
                "ts": m.get("ts") or m.get("recorded_at"),
                "symbol": m.get("symbol"),
                "trade_id": m.get("trade_id"),
                "status": m.get("status"),
                "expected_n": m.get("expected_n"),
                "checked_n": m.get("checked_n"),
                "success_n": m.get("success_n"),
                "error_n": m.get("error_n"),
                "blocked_n": m.get("blocked_n"),
                "filled_n": m.get("filled_n"),
                "book_ok": m.get("book_ok"),
                "manage_outcome": m.get("manage_outcome"),
                "book_fetched_at": m.get("book_fetched_at"),
                "version_bucket": run_ver.get(m.get("_source_run") or "", {}).get("version_bucket"),
                "notes": "MANAGE_BATCH_COMPLETE status must not be read as zero-latency guarantee; old runs may lack these events",
            }
        )
    # HTTP quality from data_quality if present
    for rd in run_dirs:
        for name in ("data_quality.jsonl", "data_quality.md"):
            p = rd / name
            if name.endswith(".jsonl") and p.exists():
                rows, _ = safe_jsonl(p)
                for o in rows:
                    exec_rows.append(
                        {
                            "record_type": "DATA_QUALITY",
                            "run_id": rd.name,
                            "batch_id": "",
                            "attempt_id": "",
                            "purpose": o.get("kind") or o.get("event"),
                            "phase": "",
                            "ts": o.get("ts") or o.get("recorded_at"),
                            "symbol": o.get("symbol"),
                            "trade_id": "",
                            "status": o.get("detail") or o.get("message"),
                            "expected_n": "",
                            "checked_n": "",
                            "success_n": "",
                            "error_n": "",
                            "blocked_n": "",
                            "filled_n": "",
                            "book_ok": "",
                            "manage_outcome": "",
                            "book_fetched_at": "",
                            "version_bucket": run_ver.get(rd.name, {}).get("version_bucket"),
                            "notes": json.dumps(o, ensure_ascii=False)[:1500],
                        }
                    )
    # quote failure-ish summary rows (not every quote to keep size sane): export aggregated + errors
    q_fail = 0
    for q in quote_rows:
        if q.get("error") or q.get("status") in (429, 418) or "429" in str(q) or "418" in str(q):
            q_fail += 1
            exec_rows.append(
                {
                    "record_type": "QUOTE_EVENT_FLAGGED",
                    "run_id": q.get("_source_run"),
                    "batch_id": q.get("batch_id"),
                    "attempt_id": "",
                    "purpose": q.get("purpose"),
                    "phase": "",
                    "ts": q.get("ts") or q.get("recorded_at"),
                    "symbol": q.get("symbol"),
                    "trade_id": "",
                    "status": q.get("error") or q.get("status"),
                    "expected_n": "",
                    "checked_n": "",
                    "success_n": "",
                    "error_n": "",
                    "blocked_n": "",
                    "filled_n": "",
                    "book_ok": "",
                    "manage_outcome": "",
                    "book_fetched_at": q.get("fetched_at"),
                    "version_bucket": run_ver.get(q.get("_source_run") or "", {}).get("version_bucket"),
                    "notes": "flagged quote/manage-related row only; full quote stream not duplicated",
                }
            )
    build_meta["quote_rows_total"] = len(quote_rows)
    build_meta["quote_flagged"] = q_fail

    write_csv(
        out / "07_execution_quality.csv",
        exec_rows,
        [
            "record_type",
            "run_id",
            "batch_id",
            "attempt_id",
            "purpose",
            "phase",
            "ts",
            "symbol",
            "trade_id",
            "status",
            "expected_n",
            "checked_n",
            "success_n",
            "error_n",
            "blocked_n",
            "filled_n",
            "book_ok",
            "manage_outcome",
            "book_fetched_at",
            "version_bucket",
            "notes",
        ],
    )

    # ========== Markdown docs ==========
    cutoff = iso()
    # time coverage from fills/equity
    t0 = fills[0]["recorded_at"] if fills else ""
    t1 = fills[-1]["recorded_at"] if fills else ""
    eq0 = root_eq[0].get("ts") if root_eq else ""
    eq1 = root_eq[-1].get("ts") if root_eq else ""

    # Strategy spec from sealed versions (read sealed files as text references)
    sealed_160 = SETUP / "fix_source_sealed_20261119T160723/breakout_follow_v1"
    sealed_155 = SETUP / "fix_source_sealed_20261119T155304/breakout_follow_v1"
    sealed_122 = SETUP / "fix_source_sealed_20261119T122904/breakout_follow_v1"

    def file_sha(p: Path) -> str:
        return sha256_file(p) if p.exists() else "MISSING"

    # Compute descriptive summary stats
    complete_trades = [t for t in trade_rows if t["data_integrity"].startswith("complete") or t["data_integrity"] == "complete"]
    # fix: integrity may be "complete" exactly
    complete_trades = [t for t in trade_rows if trade_is_complete(by_tid[t["trade_id"]])]
    open_trades = [t for t in trade_rows if t["is_open"] in (True, "True", "true")]
    by_bucket = defaultdict(list)
    for t in complete_trades:
        by_bucket[t["open_version_bucket"]].append(t)

    def bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"n": 0, "note": "insufficient_sample"}
        nets = [float(r["net_u"]) for r in rows if r.get("net_u") is not None]
        fees = [float(r["fees_u"] or 0) for r in rows]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        avg_win = sum(wins) / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None
        rr = None
        if avg_win is not None and avg_loss is not None and avg_loss != 0:
            rr = abs(avg_win / avg_loss)
        return {
            "n_complete": len(rows),
            "net_sum": sum(nets) if nets else None,
            "fee_sum": sum(fees),
            "win_rate": (len(wins) / len(nets)) if nets else None,
            "payoff_ratio_avgWin_over_absAvgLoss": rr,
            "long_n": sum(1 for r in rows if r.get("side") == "LONG"),
            "short_n": sum(1 for r in rows if r.get("side") == "SHORT"),
        }

    summary_stats = {k: bucket_stats(v) for k, v in by_bucket.items()}
    summary_stats["ALL_COMPLETE"] = bucket_stats(complete_trades)

    # Same-batch side limit diagnostics (only where scan_batch_id present)
    batch_rank = [c for c in cand_rows if c["record_type"] == "SCAN_BATCH_RANK_ENTRY"]
    batch_eval = [c for c in cand_rows if c["record_type"] == "SIGNAL_EVAL" and c.get("scan_batch_id")]
    side_lim = [c for c in batch_eval if c.get("reject_reason") == "SAME_SCAN_BATCH_SIDE_LIMIT"]

    # Account reconciliation A/B/C/D
    day_key = account.get("day_key")
    day_start = float(account.get("day_start_equity_u") or 0)
    cash = float(account.get("cash_u") or 0)
    realized = float(account.get("realized_net_u") or 0)
    fees_r = float(account.get("realized_fees_u") or 0)
    positions = account.get("positions") or {}
    used_margin = sum(float(p.get("margin_u") or 0) for p in positions.values())
    # equity mark without floating from account alone:
    # use latest equity snapshot
    last_eq = root_eq[-1] if root_eq else {}
    equity_now = last_eq.get("equity")
    floating = last_eq.get("floating_u")

    # C: complete trades with CLOSE today
    today = day_key or "2026-11-19"
    c_rows = [
        t
        for t in complete_trades
        if (t.get("close_ts") or "").startswith(str(today))
    ]
    c_net = sum(float(t["net_u"]) for t in c_rows if t.get("net_u") is not None)

    # Write inventory json
    atomic_write(
        out / "00_source_inventory.json",
        json.dumps(
            {
                "profile": PROFILE,
                "built_at": cutoff,
                "timezone": "Asia/Taipei",
                "runs": inventory_rows,
                "root_fills": rf_meta,
                "root_equity": re_meta,
                "root_decision": rd_meta,
                "vault_dir": str(VAULT),
                "vault_files_n": len(vault_files),
                "excluded_other_paper": [
                    str(SETUP.parent / "breakout_follow_v1"),
                    "any default breakout_follow_v1 account outside paper_bulk_test_v1",
                ],
                "seals": [
                    {"stamp": m.get("stamp"), "tag": m.get("tag"), "path": str(SETUP / f"fix_source_sealed_{m.get('stamp')}")}
                    for m in seals
                ],
                "live_pid": live_pid,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    # 00_README
    atomic_write(
        out / "00_README.md",
        f"""# 策略研究資料包 — paper_bulk_test_v1

## 範圍
- profile: `{PROFILE}`（不含其他預設 paper 帳戶）
- 時區：Asia/Taipei
- 建置時間（資料截止）：`{cutoff}`
- 觀測窗（fills）：`{t0}` → `{t1}`
- 權益快照窗：`{eq0}` → `{eq1}`
- 日界日鍵（帳戶）：`{day_key}`
- **本日為部分日**（仍在交易中；非午夜收盤完整日）

## 關聯鍵
- `trade_id`：成交／事件／部分退出主鍵
- `run_id`：執行實例
- `signal_key` / `signal_open_time`：訊號去重鍵（存在才填）
- `scan_batch_id`：僅新版（C）掃描批次有紀錄；舊版為空
- `batch_id`（manage）：管理批次

## 檔案用途
| 檔案 | 用途 |
|------|------|
| 00_README.md | 本說明 |
| 00_source_inventory.json | 來源盤點 |
| 01_strategy_spec.md | 策略／風控／管理規格（依 sealed） |
| 02_version_timeline.csv | 版本與切換時間線 |
| 03_trades.csv | 一列一 trade |
| 04_trade_events.csv | 逐筆 OPEN/PARTIAL/CLOSE（可證實） |
| 05_candidates.csv | 決策／候選／拒絕 |
| 06_klines.csv | 1m 研究副本 |
| 06_klines_issues.json | K線衝突／異常 |
| 07_execution_quality.csv | 管理／執行品質 |
| 08_account_reconciliation.md | A/B/C/D 帳戶核對 |
| 09_research_summary.md | 描述性診斷 |
| 10_data_gaps.md | 缺口清單 |
| MANIFEST.json | 雜湊與產生指令 |
| _snapshot/ | 建置時帳戶與 fills 研究副本 |

## 原始觀測 vs 離線計算
- **原始觀測**：fills、decision_trace、manage_events、quote_events、account_state 快照、vault CSV、sealed 原始碼
- **離線計算**：trade 聚合淨損益、initial_risk 估計、version_bucket、摘要統計、K線 OHLC 合理性檢查
- **無法取得**：盤中精確止損先後、未記錄的完整掃描池成員、舊版 manage complete 間隔、未聯網補歷史

## 建議先上傳（精簡）
1. `00_README.md`
2. `01_strategy_spec.md`
3. `09_research_summary.md`
4. `10_data_gaps.md`
5. `03_trades.csv`
6. `05_candidates.csv`（若過大可先抽有 `final_action=OPEN|BLOCKED` 子集；完整版在包內）
7. `02_version_timeline.csv`
8. `08_account_reconciliation.md`

## 安全讀取
- 持續寫入 JSONL：讀取當下檔案長度；**略過未完整尾行**；見 MANIFEST truncated_tails
- 未修改 vault／account／sealed／運行程式
""",
    )

    # 01_strategy_spec — detailed from sealed
    atomic_write(
        out / "01_strategy_spec.md",
        f"""# 01 策略規格（依實際 sealed／運行設定；非任意 worktree）

基準 sealed：
- A 早期／日損2000：`{sealed_122}`（及同日更早 seal）
- B 管理修復：`{sealed_155}` tag `fix_20261119_mgmt_reliability_v1`
- C 日損5000＋同批同向：`{sealed_160}` tag `fix_20261119_mgmt_dl5000_batchside1`

引擎 hash（C）：`{file_sha(sealed_160/'engine.py')}`  
profiles hash（C）：`{file_sha(sealed_160/'profiles.py')}`  
manage_pass hash（B=C 未改）：`{file_sha(sealed_160/'manage_pass.py')}`

## 掃描標的池與頻率
- Universe：USDT-M 永續，依 24h quote volume 等過濾；`TOP_N=10` gainers/losers 標籤；持倉標的掉榜仍掃描。  
  來源：`market_data.refresh_universe`、`UNIVERSE_REFRESH_MINUTES=10`（config）
- 掃描節奏：每分鐘掃描（`SCAN_SECOND=2` 對齊）；主迴圈 `engine.scan_once` / `paper_main`
- 暖機：啟動補齊 K 線（觀測用）；`KLINES_FETCH_LIMIT=40` 等（config）

## K線／指標／收盤
- 週期：**1m** vault；突破區間 lookback `RANGE_LOOKBACK=20`；量能均線 `VOLUME_MA_PERIOD=20`；ATR `ATR_PERIOD=14` Wilder  
  來源：`signals.detect_breakout`、`market_data.wilder_atr`、`closed_klines`
- 收盤判定：使用已閉合 bar（排除未收盤）；訊號延遲上限 `SIGNAL_MAX_DELAY_SEC=90`
- vol_x：訊號 bar 量 / 均量；門檻 `VOL_X_MIN=3.0`
- 突破緩衝：`BREAKOUT_BUFFER_ATR=0.1`；追價上限 `MAX_CHASE_ATR=1.0`
- 價差：`MAX_SPREAD=0.0008`；報價年齡 `QUOTE_MAX_AGE_SEC=5` / `BOOK_TICKER_MAX_AGE_SEC=5`

## LONG／SHORT 進場
- 完整突破訊號（方向依突破上／下沿）→ 通過點差／新鮮度／vol_x／chase 等條件  
  來源：`signals.detect_breakout`
- 開倉：`portfolio.try_open`（日損鎖、倉數、保證金、風險預算、同標的、cooldown、無有效報價不得捏造）

## 候選評分／排序（重要）
- **排序規則原本就存在**（早於 C 版）：`vol_x` 降序 → `spread` 升序 → `symbol` 升序  
  位置：`engine.scan_once` 候選 `sort`；C 版抽成 `_candidate_rank_key` 並寫入 `SCAN_BATCH_RANK`
- **C 版新增（策略變更）**：同一 `scan_batch_id`、同方向最多成功新開 1 檔（`SAME_SCAN_BATCH_SIDE_LIMIT`）；反手新開併入同批排序；退出不受限  
  位置：`engine.py`（C sealed）

## 部位／風險
- 保證金 `MARGIN_PER_TRADE_USDT=1000`；槓桿 `LEVERAGE=10`；名義約 `NOTIONAL_USDT=10000`
- 單筆最大虧損預算 `MAX_LOSS_PER_TRADE_USDT=100`；最多同時倉 `MAX_CONCURRENT_POSITIONS=20`；每標的 `MAX_PER_SYMBOL=1`
- 來源：`profiles.PAPER_BULK_TEST_V1` + `portfolio.try_open`

## 止損／BE／部分止盈／追蹤／反手
- 初始止損：`ATR_STOP_MULT=1.5` × ATR（`portfolio` 開倉）
- 追蹤：`TRAIL_ATR_MULT=1.5`
- 部分止盈：`PARTIAL_TP_NET_USDT=100`、`PARTIAL_TP_FRACTION=0.3`
- BE：程式內 breakeven 邏輯（`portfolio.manage_position`）
- 反手：完整反向突破 → 先平後開；30 分窗最多 2 次；C 版新開受同批同向上限  
  來源：`engine.execute_reverse`、`MAX_REVERSES_PER_WINDOW=2`

## 費用／成交模擬
- 單邊費率 `FEE_RATE_ONE_SIDE=0.0005`（**paper 模擬費**）
- 不利滑價 `SLIPPAGE_ADVERSE=0.0002`
- 成交用 book bid/ask；無有效報價不得假造成交  
  fills 欄位含 `simulation_assumption`

## 日損
- A/B：`DAILY_LOSS_LOCK_USDT=2000`；C：`5000`
- 日界：`TZ_NAME=Asia/Taipei`；`day_start_equity_u` + `day_key`；觸發設 `day_new_entries_locked`（禁新開，仍可管／平）
- 解鎖：日切或經測試的 `reevaluate_daily_loss_lock`；**不重設 day_start**
- 來源：`account.py`、`engine.scan_once`、`profiles.py`

## 同批同向（僅 C）
- 明確 `scan_batch_id`；所有新開倉入口（含反手 reopen）經開倉迴圈限制
- 退出／減倉不受限；不得因跳過反手新倉而漏平倉

## 管理排程（B 起部署，C 沿用）
- `MANAGE_DUE_SEC=5`；book timeout 5s、最多 2 次；`MANAGE_BOOK_OP_BUDGET_SEC=10`；`SCAN_SLICE_MAX_WAIT_SEC=2`
- due 插隊、不補跑；Book／ATR 分階；`manage_events`；429/418 共用冷卻  
  來源：`manage_pass.py`、`market_data.py`
- **fake clock 測試 ≠ 真實網路硬牆鐘保證**

## 版本對照提醒
- 不可用目前未封 worktree 冒充歷史
- Overlay 印出的舊 CODE_FIX_TAG 字串不可凌駕 code hash／cwd
""",
    )

    # 08 reconciliation
    atomic_write(
        out / "08_account_reconciliation.md",
        f"""# 08 帳戶核對（A/B/C/D）

時點：`{cutoff}`（**部分日**；非午夜強制結算）  
費率：**paper 模擬** `FEE_RATE_ONE_SIDE`（非交易所實收保證）

## 定義
- **A 帳戶水位**：日初 `day_start_equity_u`、當前 equity／cash／realized／floating／持倉保證金
- **B 視窗事件**：當日時間窗內 OPEN/PARTIAL/CLOSE 事件流（可跨 trade 生命週期）
- **C 完整交易**：有 OPEN+CLOSE，且 **CLOSE 落在當日** 的 complete trade 淨損益合計
- **D 未平倉**：仍持倉；OPEN 費已扣；未實現分離

## A — 水位
- day_key: `{day_key}`
- day_start_equity_u: `{day_start}`
- cash_u: `{cash}`
- realized_net_u: `{realized}`
- realized_fees_u: `{fees_r}`
- used_margin (sum positions.margin_u): `{used_margin}`
- latest equity snapshot: `{equity_now}`（ts={last_eq.get('ts')}）
- latest floating_u: `{floating}`
- day_new_entries_locked: `{account.get('day_new_entries_locked')}`
- 日損失估計（day_start − equity_now）：`{(day_start - float(equity_now)) if equity_now is not None else 'NA'}`
- 持倉數：{len(positions)} → {list(positions.keys())}

**注意**：`cash_delta_u` ≠ 損益；勿把 cash 變動當 realized。

## B — 當日事件（fills 去重後，recorded_at 日起碼為 {today}）
""",
    )
    # append B counts
    day_fills = [f for f in fills if str(f.get("recorded_at") or "").startswith(str(today))]
    b_counts = Counter(f.get("event") for f in day_fills)
    with (out / "08_account_reconciliation.md").open("a", encoding="utf-8") as f:
        f.write(f"- 事件計數：{dict(b_counts)}\n")
        f.write(f"- 事件筆數：{len(day_fills)}\n\n")
        f.write("## C — 完整交易且 CLOSE 在當日\n")
        f.write(f"- n={len(c_rows)}；net_sum={c_net}\n")
        f.write("- 這是完整 trade 結果，不是分段勝率。\n\n")
        f.write("## D — 未平倉\n")
        for t in open_trades:
            f.write(
                f"- {t['trade_id']} {t['symbol']} {t['side']} open={t['open_ts']} fees_in_fills={t['fees_u']}\n"
            )
        f.write("\n## 勾稽提醒\n")
        f.write("- 跨 run 合併依 trade_id；fills 已 dedupe。\n")
        f.write("- 開倉費計入 complete_net（open 成分）。\n")
        f.write("- 不重複扣費；不把 B 的局部事件勝率當成 C。\n")

    # 09 summary
    batch_stats_txt = (
        f"- SCAN_BATCH_RANK 條目數：{len(batch_rank)}\n"
        f"- 含 scan_batch_id 的 SIGNAL_EVAL：{len(batch_eval)}\n"
        f"- SAME_SCAN_BATCH_SIDE_LIMIT 拒絕：{len(side_lim)}（樣本可能仍不足）\n"
        f"- 空候選批次屬正常（無合格訊號）≠ 限制未部署\n"
    )
    atomic_write(
        out / "09_research_summary.md",
        f"""# 09 研究摘要（描述性；不調參、不部署）

建置：`{cutoff}`｜部分日｜profile `{PROFILE}`

## 樣本
- fills 去重後：{len(fills)} 筆事件；trade_id 數：{len(trade_rows)}
- 完整交易：{len(complete_trades)}；未平倉：{len(open_trades)}
- 按開倉版本桶：{json.dumps(summary_stats, ensure_ascii=False, indent=2)}

## 同批同向（僅 C 可觀測）
{batch_stats_txt}

## 分布備註
- LONG/SHORT 見各桶 long_n/short_n
- 時段／標的分布請用 `03_trades.csv` 自行分箱；本摘要不外推全日期望
- 同時段同向相關性：樣本少，僅可描述共現，不可作因果

## 執行 vs 策略
- B 起有 `MANAGE_BATCH_COMPLETE`；A 早期若缺事件 → **管理完成間隔不可計算**
- 切換空窗見 `02_version_timeline.csv`（stop→stable_start），**不是** quote→complete 精確空窗
- 失敗交易是否伴隨管理異常：請 join `03_trades.csv` 與 `07_execution_quality.csv`（同時間窗）；本輪不做因果斷言

## K線 MFE/MAE
- **未在本包預先計算路徑依賴的精確 MFE/MAE**（避免 OHLC 捏造進出場先後）
- 若外部用 `06_klines.csv` 估區間極值，必須標記為 K線區間估計，且進出場所在 1m bar 內先後不可知

## 禁止事項（分析約束）
- 跳過候選 ≠ 成交
- 進場後報酬 ≠ 進場特徵
- 不同版本／切換空窗混算需分列
""",
    )

    # 10 gaps
    atomic_write(
        out / "10_data_gaps.md",
        f"""# 10 資料缺口

1. **舊版無 `scan_batch_id`／`SCAN_BATCH_RANK`**  
   - 影響：無法對 A/B 重放「同批同向」決策邊界  
   - 可否恢復：不能可靠假造當時批次  
   - 未來紀錄：已在 C 部署；無需本輪改碼  
   - 外部抓取：不需要

2. **A 早期缺少 `MANAGE_BATCH_COMPLETE` 完成事件**  
   - 影響：管理完成間隔、部分成功語義不可比  
   - 恢復：不可用 quote 時間冒充  
   - 未來：B/C 已有；歷史缺口保留標記

3. **trail／BE／effective_stop 變更路徑未完整落在獨立事件流**  
   - 影響：不能由最終持倉倒推完整 stop 演進  
   - 現有：OPEN 含 initial_stop；部分在 manage 結果／帳戶欄位  
   - 未來最小變更（**僅建議，本輪不部署**）：每次 stop 變更寫 position_event（old→new, reason, ts, trade_id）

4. **decision 未保證「全宇宙每個標的每分鐘都有列」**  
   - 影響：不能把 SIGNAL_EVAL 集合當成完整標的池快照  
   - 恢復：不可事後補齊未記錄掃描  
   - 建議：若需研究拒因覆蓋率，未來可選記 universe_snapshot_id（建議項）

5. **無聯網補歷史深度 K線**  
   - 影響：更長暖機／跨日 HTF 研究可能不足  
   - 需另行批准外部抓取

6. **部分日**  
   - 影響：勝率／期望不可外推全日；午夜報告另開

7. **開倉時 `scan_batch_id` 回填**  
   - 部分早期 OPEN 的 scan_batch_id 為空（版本無此欄）  
   - 不冒充

truncated_tails: {json.dumps(build_meta.get('truncated_tails'), ensure_ascii=False)}
""",
    )

    # MANIFEST + hashes
    file_hashes = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.json" and "_snapshot" not in p.parts or (
            p.is_file() and p.parent.name == "_snapshot"
        ):
            if p.is_file():
                rel = str(p.relative_to(out))
                file_hashes[rel] = {
                    "sha256": sha256_file(p),
                    "bytes": p.stat().st_size,
                }

    manifest = {
        "built_at": cutoff,
        "outdir": str(out),
        "command": f"python3 {Path(__file__).resolve()} ",
        "tool_sha256": build_meta["tool_sha256"],
        "live_pid_at_build": live_pid,
        "live_paper_modified": False,
        "live_paper_restarted": False,
        "truncated_tails": build_meta.get("truncated_tails"),
        "fills_dup_removed": n_dup,
        "counts": {
            "trades": len(trade_rows),
            "trade_events": len(event_rows),
            "candidates": len(cand_rows),
            "klines": len(kline_rows),
            "kline_issues": len(kline_issues),
            "exec_rows": len(exec_rows),
            "manage_rows": len(manage_rows),
        },
        "files": file_hashes,
        "build_meta_notes": build_meta.get("notes"),
    }
    atomic_write(out / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    # regenerate file hashes including MANIFEST? keep MANIFEST without self hash
    # ZIP
    zip_path = out.parent / f"{stamp}_research_pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(Path(stamp) / p.relative_to(out)))

    # sizes summary
    sizes = {p.name: p.stat().st_size for p in out.iterdir() if p.is_file()}
    sizes["ZIP"] = zip_path.stat().st_size
    atomic_write(out / "DELIVERY_SIZES.json", json.dumps({"outdir": str(out), "zip": str(zip_path), "sizes": sizes}, indent=2) + "\n")

    print(json.dumps({"outdir": str(out), "zip": str(zip_path), "sizes": sizes, "live_pid": live_pid}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
