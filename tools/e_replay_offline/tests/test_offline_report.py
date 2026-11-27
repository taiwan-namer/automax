"""Acceptance tests for wp-e-repair-safety-02 / offline report."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tools.e_replay_offline.reader import (
    LedgerQuery,
    ReportInputs,
    SourceWriteRefused,
    build_report,
    sha256_file,
    write_report_atomic,
)

FX = Path(__file__).resolve().parent / "fixtures"
TRADE = ":BULLAUSDT:727206056c"
W0 = "2026-11-27T07:29:16.234470Z"
W1 = "2026-11-27T07:50:42.717639Z"
A0 = "2026-11-27T07:50:00Z"
A1 = "2026-11-27T07:51:00Z"


def _base(**kw):
    d = dict(
        arm_fills=FX / "arm_fills_synth.jsonl",
        ledger_fills=FX / "ledger_fills_synth.jsonl",
        ledger_orders=FX / "ledger_orders_synth.jsonl",
        bars_path=FX / "bars_empty.json",
        trade_id=TRADE,
        window_start_utc=W0,
        window_end_utc=W1,
        a_bar_open_utc=A0,
        a_bar_close_utc=A1,
        ledger_queries=[
            LedgerQuery("client_order_id", client_order_id="STRAT_E_f9ba63ff"),
            LedgerQuery("exchange_order_id", exchange_order_id="366618857"),
            LedgerQuery("exchange_trade_key", exchange_trade_key="no_such_key"),
            LedgerQuery("tp1_order_id", tp1_order_id="999999999"),
        ],
        funding_known=False,
        commission_fx_known=False,
    )
    d.update(kw)
    return ReportInputs(**d)


class TestOfflineReport(unittest.TestCase):
    def test_duplicate_events_preserved_order(self):
        r = build_report(_base())
        self.assertEqual(r["event_n"], 4)
        kinds = [e["event"] for e in r["events"]]
        self.assertEqual(kinds, ["OPEN", "SETTLEMENT_CLOSE", "SETTLEMENT_CLOSE", "FEE_ADJUST"])
        self.assertEqual(r["event_counts"].get("SETTLEMENT_CLOSE"), 2)
        self.assertTrue(r["events_order_preserved"])
        lines = [e["line_no"] for e in r["events"]]
        self.assertEqual(lines, sorted(lines))

    def test_ledger_keys_separate_hit_counts(self):
        r = build_report(_base())
        by = {q["query"]: q for q in r["ledger_queries"]}
        self.assertEqual(by["client_order_id"]["fills_hits"], 2)
        self.assertEqual(by["exchange_order_id"]["fills_hits"], 2)
        self.assertEqual(by["exchange_trade_key"]["fills_hits"], 0)
        self.assertEqual(by["tp1_order_id"]["fills_hits"], 0)
        self.assertGreater(by["client_order_id"]["fills_hits"], 0)
        self.assertEqual(by["client_order_id"]["orders_hits"], 1)

    def test_missing_bars_blocked_price_path(self):
        r = build_report(_base(bars_path=FX / "bars_empty.json"))
        self.assertEqual(r["a_point"]["status"], "blocked")
        self.assertEqual(r["a_point"]["reason"], "PRICE_PATH_MISSING")
        self.assertIsNone(r["a_point"]["simulated_pnl"])
        self.assertEqual(r["kline"]["matched"], 0)
        self.assertEqual(r["kline"]["expected"], 22)

    def test_bars_not_provided_not_claimed_zero_match(self):
        r = build_report(_base(bars_path=None))
        self.assertEqual(r["kline"]["status"], "bars_not_provided")
        self.assertIsNone(r["kline"]["matched"])
        self.assertEqual(r["a_point"]["status"], "blocked")
        self.assertEqual(r["a_point"]["reason"], "PRICE_PATH_MISSING")
        self.assertIsNone(r["a_point"]["simulated_pnl"])

    def test_missing_funding_fx_not_computable(self):
        r = build_report(_base(funding_known=False, commission_fx_known=False))
        self.assertEqual(r["costs"]["full_net_pnl"], "not_computable")
        self.assertEqual(r["gaps"]["funding"], "gap")
        self.assertEqual(r["gaps"]["commission_fx"], "unknown")

    def test_source_mutation_stable_false(self):
        """Inject mutation between sha_before and sha_after inside one build_report."""
        with tempfile.TemporaryDirectory() as td:
            arm = Path(td) / "arm.jsonl"
            shutil.copyfile(FX / "arm_fills_synth.jsonl", arm)

            def mutate():
                with arm.open("a", encoding="utf-8") as f:
                    f.write('{"event":"NOISE","trade_id":"x","synthetic":true}\n')

            r = build_report(
                _base(
                    arm_fills=arm,
                    ledger_fills=FX / "ledger_fills_synth.jsonl",
                    ledger_orders=FX / "ledger_orders_synth.jsonl",
                ),
                after_read_hook=mutate,
            )
            self.assertIs(r["sources"]["arm_fills"]["stable"], False)
            self.assertFalse(r["stable"])
            self.assertNotEqual(
                r["sources"]["arm_fills"]["sha256_before"],
                r["sources"]["arm_fills"]["sha256_after"],
            )

    def test_refuse_outdir_equals_source_dir(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "srcdata"
            src_dir.mkdir()
            arm = src_dir / "arm.jsonl"
            shutil.copyfile(FX / "arm_fills_synth.jsonl", arm)
            h_before = sha256_file(arm)
            r = build_report(
                _base(
                    arm_fills=arm,
                    ledger_fills=FX / "ledger_fills_synth.jsonl",
                    ledger_orders=FX / "ledger_orders_synth.jsonl",
                )
            )
            with self.assertRaises(SourceWriteRefused) as ctx:
                write_report_atomic(r, src_dir, include_events=False, source_paths=[arm])
            self.assertIn("refuse_outdir_equals_source_dir", str(ctx.exception))
            h_after = sha256_file(arm)
            self.assertEqual(h_before, h_after)
            self.assertTrue(h_before is not None and len(h_before) == 64)

    def test_refuse_outfile_equals_source_file(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "srcdata"
            out_dir = Path(td) / "e_repair_offline_01"
            src_dir.mkdir()
            out_dir.mkdir()
            arm = src_dir / "arm.jsonl"
            shutil.copyfile(FX / "arm_fills_synth.jsonl", arm)
            h_before = sha256_file(arm)
            r = build_report(
                _base(
                    arm_fills=arm,
                    ledger_fills=FX / "ledger_fills_synth.jsonl",
                    ledger_orders=FX / "ledger_orders_synth.jsonl",
                )
            )
            with self.assertRaises(SourceWriteRefused) as ctx:
                write_report_atomic(
                    r,
                    out_dir,
                    include_events=False,
                    source_paths=[arm],
                    out_file=arm,
                )
            self.assertIn("refuse_outfile_equals_source_file", str(ctx.exception))
            h_after = sha256_file(arm)
            self.assertEqual(h_before, h_after)
            self.assertEqual(len(h_before or ""), 64)

    def test_fixed_case_shape(self):
        r = build_report(_base())
        self.assertEqual(r["trade_id"], TRADE)
        self.assertEqual(r["window_utc"]["start"], W0)
        self.assertEqual(r["kline"]["expected"], 22)
        self.assertEqual(r["qty_balance"]["open_plus_add"], 100.0)
        self.assertEqual(r["qty_balance"]["close_like_qty"], 200.0)


if __name__ == "__main__":
    unittest.main()
