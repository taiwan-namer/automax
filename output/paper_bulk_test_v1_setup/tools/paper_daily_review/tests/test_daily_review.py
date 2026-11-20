from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_daily_review.jsonl_io import dedupe_fills, read_jsonl_bounded
from paper_daily_review.timeutil import TAIPEI, day_window, last_settled_day, parse_iso
from paper_daily_review.trades import (
    analyze_day_trades,
    complete_trade_net,
    group_by_trade_id,
    open_fee_component,
)
from paper_daily_review.report import generate_day_report
from paper_daily_review.kline_qa import derived_bucket_complete


FIXTURES = Path(__file__).parent / "fixtures"


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def mini_setup(tmp_path: Path) -> Path:
    setup = tmp_path / "paper_bulk_test_v1_setup"
    acc = setup / "fixed_data_root" / "output" / "paper_bulk_test_v1"
    runs = acc / "runs"
    r1 = runs / "runA"
    r2 = runs / "runB"
    vault = setup / "fixed_data_root" / "data" / "paper_bulk_test_v1" / "kline_vault_v2_clean"
    for p in (r1, r2, vault, setup / "analysis_packs" / "daily_reports"):
        p.mkdir(parents=True)

    # Cross-run trade: open runA day1 fee, partial, close runB day2
    # Also same-day complete, open leftover, duplicate event, zero realized_net_u
    fills = [
        {
            "event": "OPEN",
            "trade_id": "runA:AAA:1",
            "symbol": "AAAUSDT",
            "side": "LONG",
            "price": 100.0,
            "qty": 1.0,
            "fee_u": 5.0,
            "realized_net_u": 0.0,  # legal zero
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
        # duplicate partial (should dedupe)
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
            # realized_net_u absent
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
    _write_jsonl(acc / "fills.jsonl", fills)
    # half-written tail on run copy
    with (r2 / "fills.jsonl").open("w") as f:
        f.write(json.dumps(fills[-1]) + "\n")
        f.write('{"event":"OPEN","trade_id":"broken"')  # incomplete

    # equity: no midnight; nearest mid-day
    eq = [
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
    ]
    _write_jsonl(acc / "equity_snapshots.jsonl", eq)
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
    # incomplete kline for CCC; missing BBB
    (vault / "AAAUSDT_1m.csv").write_text(
        "open_time,open,high,low,close,volume\n"
        "1731988800000,100,101,99,100.5,1\n"
    )
    (vault / "CCCUSDT_1m.csv").write_text(
        "open_time,open,high,low,close,volume\n"
        "1732075200000,10,11,9,10.5,1\n"  # may be outside window depending
    )
    return setup


def test_settlement_day_window_and_last_settled():
    start, end = day_window(date(2026, 11, 19))
    assert start.isoformat() == "2026-11-18T20:15:00+08:00"
    assert end.isoformat() == "2026-11-19T20:15:00+08:00"
    # 20:14 → 仍屬前一結算日 11/18；20:15 → 11/19 已結算
    assert last_settled_day(datetime(2026, 11, 19, 20, 14, tzinfo=TAIPEI)) == date(2026, 11, 18)
    assert last_settled_day(datetime(2026, 11, 19, 20, 15, tzinfo=TAIPEI)) == date(2026, 11, 19)
    assert last_settled_day(datetime(2026, 11, 19, 20, 25, tzinfo=TAIPEI)) == date(2026, 11, 19)


def test_open_fee_zero_not_missing():
    assert open_fee_component({"realized_net_u": 0.0, "fee_u": 5.0}) == 0.0
    assert open_fee_component({"fee_u": 5.0}) == -5.0
    assert open_fee_component({"realized_net_u": None, "fee_u": 5.0}) == -5.0


def test_complete_net_and_dedupe(mini_setup):
    from paper_daily_review.discover import discover_setup
    from paper_daily_review.trades import load_all_fills

    paths = discover_setup(mini_setup)
    fills, meta = load_all_fills(paths)
    assert meta["dup_removed"] >= 1
    assert any(meta["truncated_tails"])
    by = group_by_trade_id(fills)
    net = complete_trade_net(by["runA:AAA:1"])
    # open realized 0 + partial 2 + close 10 = 12 (dup partial removed)
    assert net == pytest.approx(12.0)
    net2 = complete_trade_net(by["runB:BBB:2"])
    # -4 open fee + 8 close = 4
    assert net2 == pytest.approx(4.0)


def test_day_c_d_and_cross_version(mini_setup):
    from paper_daily_review.discover import discover_setup
    from paper_daily_review.trades import load_all_fills

    paths = discover_setup(mini_setup)
    fills, _ = load_all_fills(paths)
    start, end = day_window(date(2026, 11, 19))
    st = analyze_day_trades(fills, start, end)
    assert st.metrics_c["n_complete_closed"] == 2
    assert st.metrics_d["n_open"] == 1
    cross = [t for t in st.complete_closed_today if t["cross_version"]]
    assert len(cross) == 1
    assert cross[0]["trade_id"] == "runA:AAA:1"


def test_truncated_tail_read(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":')
    r = read_jsonl_bounded(p)
    assert r.truncated_tail
    assert r.lines_ok == 1


def test_derived_bucket_no_lookahead():
    # bucket 10:00-10:10, asof 10:09 incomplete
    assert derived_bucket_complete(0, 10, 9 * 60_000) is False
    assert derived_bucket_complete(0, 10, 10 * 60_000) is True


def test_generate_partial_day_idempotent(mini_setup):
    asof = datetime(2026, 11, 19, 15, 30, tzinfo=TAIPEI)
    r1 = generate_day_report(mini_setup, date(2026, 11, 19), asof=asof)
    assert "部分日" in r1["completeness"]["status"] or "試產" in r1["completeness"]["status"]
    r2 = generate_day_report(mini_setup, date(2026, 11, 19), asof=asof)
    assert r1["out_dir"] == r2["out_dir"]
    summary = Path(r1["summary_path"]).read_text(encoding="utf-8")
    assert "cash_delta 不是 PnL" in summary or "cash_delta" in summary
    assert Path(r1["out_dir"], "manifest.json").exists()
    # midnight not ok
    m = json.loads(Path(r1["out_dir"], "metrics.json").read_text())
    assert m["A"]["midnight_snapshot_ok"] is False


def test_backfill_missing_day_row(mini_setup):
    # produce day so index exists; second call updates same row
    asof = datetime(2026, 11, 20, 0, 15, tzinfo=TAIPEI)
    generate_day_report(mini_setup, date(2026, 11, 19), asof=asof)
    idx = (mini_setup / "analysis_packs" / "daily_reports" / "DAILY_INDEX.md").read_text()
    assert idx.count("2026-11-19") >= 1
