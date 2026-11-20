#!/usr/bin/env python3
"""Offline acceptance without pytest dependency."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_daily_review.discover import discover_setup
from paper_daily_review.jsonl_io import read_jsonl_bounded
from paper_daily_review.kline_qa import derived_bucket_complete
from paper_daily_review.report import generate_day_report
from paper_daily_review.timeutil import TAIPEI, day_window, last_settled_day
from paper_daily_review.trades import (
    analyze_day_trades,
    complete_trade_net,
    group_by_trade_id,
    load_all_fills,
    open_fee_component,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_mini_setup(base: Path) -> Path:
    setup = base / "paper_bulk_test_v1_setup"
    acc = setup / "fixed_data_root" / "output" / "paper_bulk_test_v1"
    runs = acc / "runs"
    r1 = runs / "runA"
    r2 = runs / "runB"
    vault = setup / "fixed_data_root" / "data" / "paper_bulk_test_v1" / "kline_vault_v2_clean"
    for p in (r1, r2, vault, setup / "analysis_packs" / "daily_reports"):
        p.mkdir(parents=True)
    fills = [
        {
            "event": "OPEN",
            "trade_id": "runA:AAA:1",
            "symbol": "AAAUSDT",
            "side": "LONG",
            "price": 100.0,
            "qty": 1.0,
            "fee_u": 5.0,
            "realized_net_u": 0.0,
            "initial_stop": 99.0,
            "recorded_at": "2026-11-18T10:00:00+08:00",
            "run_id": "runA",
        },
        {
            "event": "CLOSE_PARTIAL",
            "trade_id": "runA:AAA:1",
            "symbol": "AAAUSDT",
            "side": "LONG",
            "price": 101.0,
            "qty": 0.3,
            "fee_u": 1.0,
            "net_u": 2.0,
            "exit_reason": "PARTIAL_TP",
            "recorded_at": "2026-11-18T11:00:00+08:00",
            "run_id": "runA",
        },
        {
            "event": "CLOSE_PARTIAL",
            "trade_id": "runA:AAA:1",
            "symbol": "AAAUSDT",
            "side": "LONG",
            "price": 101.0,
            "qty": 0.3,
            "fee_u": 1.0,
            "net_u": 2.0,
            "exit_reason": "PARTIAL_TP",
            "recorded_at": "2026-11-18T11:00:00+08:00",
            "run_id": "runA",
        },
        {
            "event": "CLOSE",
            "trade_id": "runA:AAA:1",
            "symbol": "AAAUSDT",
            "side": "LONG",
            "price": 102.0,
            "qty": 0.7,
            "fee_u": 2.0,
            "net_u": 10.0,
            "exit_reason": "TRAILING_STOP",
            "recorded_at": "2026-11-19T09:00:00+08:00",
            "run_id": "runB",
        },
        {
            "event": "OPEN",
            "trade_id": "runB:BBB:2",
            "symbol": "BBBUSDT",
            "side": "SHORT",
            "price": 50.0,
            "qty": 2.0,
            "fee_u": 4.0,
            "initial_stop": 51.0,
            "recorded_at": "2026-11-19T12:00:00+08:00",
            "run_id": "runB",
        },
        {
            "event": "CLOSE",
            "trade_id": "runB:BBB:2",
            "symbol": "BBBUSDT",
            "side": "SHORT",
            "price": 49.0,
            "qty": 2.0,
            "fee_u": 4.0,
            "net_u": 8.0,
            "exit_reason": "INITIAL_STOP",
            "recorded_at": "2026-11-19T13:00:00+08:00",
            "run_id": "runB",
        },
        {
            "event": "OPEN",
            "trade_id": "runB:CCC:3",
            "symbol": "CCCUSDT",
            "side": "LONG",
            "price": 10.0,
            "qty": 5.0,
            "fee_u": 3.0,
            "initial_stop": 9.5,
            "recorded_at": "2026-11-19T14:00:00+08:00",
            "run_id": "runB",
        },
    ]
    write_jsonl(acc / "fills.jsonl", fills)
    with (r2 / "fills.jsonl").open("w") as f:
        f.write(json.dumps(fills[-1]) + "\n")
        f.write('{"event":"OPEN","trade_id":"broken"')
    write_jsonl(
        acc / "equity_snapshots.jsonl",
        [
            {
                "recorded_at": "2026-11-19T12:00:00+08:00",
                "ts": "2026-11-19T12:00:00+08:00",
                "equity": 50000.0,
                "cash": 49000.0,
                "realized_net_u": -10.0,
                "floating_u": 0.0,
                "positions": 0,
            },
            {
                "recorded_at": "2026-11-19T15:00:00+08:00",
                "ts": "2026-11-19T15:00:00+08:00",
                "equity": 50020.0,
                "cash": 48990.0,
                "realized_net_u": 5.0,
                "floating_u": 1.0,
                "positions": 1,
            },
        ],
    )
    (acc / "account_state.json").write_text(
        json.dumps({"cash_u": 48990.0, "realized_net_u": 5.0, "positions": {}, "profile_id": "paper_bulk_test_v1"})
    )
    (acc / "current_run.json").write_text(json.dumps({"run_id": "runB", "root": str(r2)}))
    (r2 / "launch.json").write_text(
        json.dumps(
            {
                "run_id": "runB",
                "profile_id": "paper_bulk_test_v1",
                "code_fix_tag": "test_tag",
                "cwd": "/tmp/runtime",
                "daily_loss_lock_usdt": 2000,
            }
        )
    )
    (r2 / "code_hashes.json").write_text(json.dumps({"config_hash": "abc", "code_fix_tag": "test_tag"}))
    (r2 / "quote_events.jsonl").write_text("")
    (r2 / "data_quality.jsonl").write_text(
        json.dumps({"kind": "HTTP_TIMEOUT", "recorded_at": "2026-11-19T12:30:00+08:00"}) + "\n"
    )
    (vault / "AAAUSDT_1m.csv").write_text(
        "open_time,open,high,low,close,volume\n1731988800000,100,101,99,100.5,1\n"
    )
    return setup


def main() -> int:
    global PASS, FAIL
    check("open_fee_zero", open_fee_component({"realized_net_u": 0.0, "fee_u": 5.0}) == 0.0)
    check("open_fee_absent", open_fee_component({"fee_u": 5.0}) == -5.0)
    check("derived_incomplete", derived_bucket_complete(0, 10, 9 * 60_000) is False)
    check("derived_complete", derived_bucket_complete(0, 10, 10 * 60_000) is True)

    p = Path(tempfile.mkdtemp()) / "a.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"a":1}\n{"b":')
    r = read_jsonl_bounded(p)
    check("truncated_tail", r.truncated_tail and r.lines_ok == 1)

    td = Path(tempfile.mkdtemp())
    try:
        setup = make_mini_setup(td)
        paths = discover_setup(setup)
        fills, meta = load_all_fills(paths)
        check("dedupe", meta["dup_removed"] >= 1)
        check("truncated_meta", bool(meta["truncated_tails"]))
        by = group_by_trade_id(fills)
        check("net_cross", abs(complete_trade_net(by["runA:AAA:1"]) - 12.0) < 1e-9)
        check("net_fee", abs(complete_trade_net(by["runB:BBB:2"]) - 4.0) < 1e-9)
        start, end = day_window(date(2026, 11, 19))
        check("settle_window_start", start.isoformat() == "2026-11-18T20:15:00+08:00")
        check("settle_window_end", end.isoformat() == "2026-11-19T20:15:00+08:00")
        check(
            "last_settled_before",
            last_settled_day(datetime(2026, 11, 19, 20, 14, tzinfo=TAIPEI)) == date(2026, 11, 18),
        )
        check(
            "last_settled_after",
            last_settled_day(datetime(2026, 11, 19, 20, 25, tzinfo=TAIPEI)) == date(2026, 11, 19),
        )
        st = analyze_day_trades(fills, start, end)
        check("c_n", st.metrics_c["n_complete_closed"] == 2)
        check("d_n", st.metrics_d["n_open"] == 1)
        check("cross", sum(1 for t in st.complete_closed_today if t["cross_version"]) == 1)

        asof = datetime(2026, 11, 19, 15, 30, tzinfo=TAIPEI)
        r1 = generate_day_report(setup, date(2026, 11, 19), asof=asof)
        check("partial_day", "部分日" in r1["completeness"]["status"] or "試產" in r1["completeness"]["status"])
        r2 = generate_day_report(setup, date(2026, 11, 19), asof=asof)
        check("idempotent", r1["out_dir"] == r2["out_dir"])
        m = json.loads(Path(r1["out_dir"], "metrics.json").read_text())
        check("no_fake_midnight", m["A"]["midnight_snapshot_ok"] is False)
        summary = Path(r1["summary_path"]).read_text(encoding="utf-8")
        check("summary_cash_delta_note", "cash_delta" in summary)
        idx = (setup / "analysis_packs" / "daily_reports" / "DAILY_INDEX.md").read_text()
        check("index_row", "2026-11-19" in idx)
    except Exception:
        traceback.print_exc()
        FAIL += 1
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
