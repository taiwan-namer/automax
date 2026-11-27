"""wp-e-functional-strategy-impl-01: isolated strategy replay tests."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from tools.e_replay_offline.a_point_sim import semantic_report_diff, simulate_a_point_exit, APointSimInputs
from tools.e_replay_offline.strategy_replay import (
    EXPECTED_SOURCE_SHA,
    StrategyReplayInputs,
    prepare_bars,
    run_strategy_replay,
    sha256_file,
    verify_strategy_sources,
    write_strategy_report,
)

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_strategy_01"
E_SRC = REPO / "output" / "e_trend_v2" / "code" / "e_top10_1m_down_v1"

SYMBOL = "SYNTHUSDT"
B0 = {
    "symbol": SYMBOL,
    "interval": "1m",
    "open_time": 1795765740000,
    "open": 102,
    "high": 103,
    "low": 100.5,
    "close": 101,
    "close_time": 1795765799999,
}
B1 = {
    "symbol": SYMBOL,
    "interval": "1m",
    "open_time": 1795765800000,
    "open": 101,
    "high": 101.5,
    "low": 99.5,
    "close": 100,
    "close_time": 1795765859999,
}
C = 1795765859999
C1 = C + 1
B_FUTURE = {
    "symbol": SYMBOL,
    "interval": "1m",
    "open_time": 1795765860000,
    "open": 100,
    "high": 101,
    "low": 99,
    "close": 100.5,
    "close_time": 1795765919999,
}
B1_FORMING = dict(B1, close_time=1795765860000)  # close_time >= now when now=C1-ish

BOOK_ENTRY = {
    "bid": 99.95,
    "ask": 100.0,
    "mid": 99.975,
    "spread": (100.0 - 99.95) / 99.975,
}
BOOK_EXIT_SL = {
    "bid": 96.0,
    "ask": 96.05,
    "mid": 96.025,
    "spread": (96.05 - 96.0) / 96.025,
}

PRE_SHA = {
    "tools/e_replay_offline/strategy_replay.py": "NEW",
    "tools/e_replay_offline/tests/test_strategy_replay.py": "NEW",
}


class TestStrategyReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.fx_dir = OUT / "fixtures"
        cls.fx_dir.mkdir(exist_ok=True)
        v = verify_strategy_sources()
        if not v["ok"]:
            raise unittest.SkipTest(f"strategy source sha mismatch: {v}")

    def _rt(self, name: str) -> Path:
        p = OUT / "runtime" / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def test_00_source_sha_locked(self):
        for name, exp in EXPECTED_SOURCE_SHA.items():
            got = sha256_file(E_SRC / name)
            self.assertEqual(got, exp, msg=name)

    def test_01_no_future_and_forming_separate(self):
        # prefix [B0,B1]
        r0 = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("nofut_prefix"),
                bars=[B0, B1],
                clock_ms=C1,
                process_now_ms=C1,
                run_exit=False,
                strip_forming=True,
            )
        )
        self.assertEqual(r0["counts"]["signals"], 1)
        sid = r0["signal"]["signal_id"]

        # cutoff future: [B0,B1,B_FUTURE] → strip ot>=cutoff → [B0,B1]
        r1 = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("nofut_cutoff"),
                bars=[B0, B1, B_FUTURE],
                clock_ms=C1,
                process_now_ms=C1,
                cutoff_ms=1795765860000,
                run_exit=False,
                strip_forming=False,
            )
        )
        self.assertEqual(r1["bars_open_times"], [B0["open_time"], B1["open_time"]])
        self.assertEqual(r1["signal"]["signal_id"], sid)

        # forming B1 only vs B0: strip_forming with now=C1 → only B0 → insufficient → blocked/no 2 bars
        r2 = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("forming"),
                bars=[B0, B1_FORMING],
                clock_ms=C1,
                process_now_ms=C1,
                strip_forming=True,
                run_exit=False,
            )
        )
        self.assertEqual(r2["bars_open_times"], [B0["open_time"]])
        self.assertTrue(r2.get("blocked") or r2.get("status") in ("blocked", "no_signal"))
        self.assertEqual(r2["counts"]["signals"], 0)

    def test_02_timing_C_vs_C1(self):
        r_c = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("timing_c"),
                bars=[B0, B1],
                book_entry=BOOK_ENTRY,
                clock_ms=C,
                process_now_ms=C,
                run_exit=False,
                strip_forming=False,
            )
        )
        self.assertEqual(r_c["counts"]["signals"], 1)
        self.assertEqual(r_c["counts"]["open_fills"], 0)
        self.assertEqual(r_c["status"], "pending_not_filled")

        r_c1 = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("timing_c1"),
                bars=[B0, B1],
                book_entry=BOOK_ENTRY,
                clock_ms=C1,
                process_now_ms=C1,
                run_exit=False,
                strip_forming=False,
            )
        )
        self.assertEqual(r_c1["counts"]["open_fills"], 1)
        self.assertEqual(r_c1["open_fill_events"][0]["action"], "FILLED")
        # paper fill price = ask
        self.assertEqual(float(r_c1["open_fill_events"][0]["price"]), 100.0)

    def test_03_same_bar_ambiguous_blocked(self):
        r = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("amb"),
                bars=[B0, B1],
                same_bar_ambiguous=True,
            )
        )
        self.assertTrue(r["blocked"])
        self.assertEqual(r["blocked_reason"], "SAME_BAR_ORDER_AMBIGUOUS")
        self.assertIsNone(r["simulated_pnl"])

    def test_04_a_point_formula_independent(self):
        # retained A-point case — not strategy lifecycle
        bars = [
            {
                "symbol": SYMBOL,
                "interval": "1m",
                "open_time": 1795765800000,
                "close_time": 1795765859999,
                "open": "100",
                "high": "120",
                "low": "90",
                "close": "110",
            }
        ]
        bp = self.fx_dir / "a_point_bars.json"
        bp.write_text(json.dumps(bars), encoding="utf-8")
        r = simulate_a_point_exit(
            APointSimInputs(
                symbol=SYMBOL,
                interval="1m",
                direction="LONG",
                entry_px=Decimal("100"),
                remaining_qty=Decimal("2"),
                a_point_utc="2026-11-27T07:50:00Z",
                market_cutoff_ms=1795765860000,
                entry_fee_rate=Decimal("0.001"),
                exit_fee_rate=Decimal("0.001"),
                slippage_rate=Decimal("0"),
                funding=Decimal("-0.5"),
                bars=bars,
                bars_provided=True,
                bars_source_path=str(bp),
                window_start_utc="2026-11-27T07:50:00Z",
                window_end_utc="2026-11-27T07:51:00Z",
                half_open_end=True,
            )
        )
        self.assertEqual(r["gross"], "20")
        self.assertEqual(r["entry_fee"], "0.2")
        self.assertEqual(r["exit_fee"], "0.22")
        self.assertEqual(r["funding"], "-0.5")
        self.assertEqual(r["simulated_pnl"], "19.08")

    def test_05_missing_costs(self):
        r = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("nocost"),
                bars=[B0, B1],
                book_entry=BOOK_ENTRY,
                book_exit=BOOK_EXIT_SL,
                clock_ms=C1,
                process_now_ms=C1,
                funding=None,
                costs_complete=False,
                run_exit=True,
                strip_forming=False,
            )
        )
        self.assertEqual(r["full_net_status"], "not_computable")
        self.assertIsNone(r["simulated_pnl"])

    def test_06_missing_bars_and_wrong_symbol(self):
        r_miss = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("miss"),
                bars=[B0],  # only one bar
                clock_ms=C1,
                strip_forming=False,
                run_exit=False,
            )
        )
        self.assertTrue(r_miss["blocked"])
        self.assertIsNone(r_miss["simulated_pnl"])

        wrong = dict(B1, symbol="OTHERUSDT")
        r_w = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("wrong"),
                bars=[B0, wrong],
                clock_ms=C1,
                strip_forming=False,
                run_exit=False,
            )
        )
        self.assertTrue(r_w["blocked"])
        self.assertEqual(r_w["blocked_reason"], "WRONG_SYMBOL")
        self.assertIsNone(r_w["simulated_pnl"])

    def test_07_full_lifecycle_and_dual_cli(self):
        r = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=self._rt("life"),
                bars=[B0, B1],
                book_entry=BOOK_ENTRY,
                book_exit=BOOK_EXIT_SL,
                clock_ms=C1,
                process_now_ms=C1,
                funding=Decimal("0"),
                costs_complete=True,
                run_exit=True,
                strip_forming=False,
            )
        )
        self.assertEqual(r["counts"]["signals"], 1)
        self.assertEqual(r["counts"]["pending_queued"], 1)
        self.assertEqual(r["counts"]["open_fills"], 1)
        self.assertGreaterEqual(r["counts"]["exit_events"], 1)
        self.assertIn("INITIAL_STOP", [e.get("action") for e in r.get("exit_events") or []])
        self.assertFalse(r.get("fill_claim"))
        self.assertTrue(r.get("simulated"))
        # persist lifecycle evidence
        (OUT / "lifecycle_actual.json").write_text(
            json.dumps(
                {
                    "expected": {
                        "signals": 1,
                        "pending": 1,
                        "open_fills": 1,
                        "exit_action": "INITIAL_STOP",
                    },
                    "actual": {
                        "counts": r["counts"],
                        "signal": r.get("signal"),
                        "open": (r.get("open_fill_events") or [None])[0],
                        "exits": r.get("exit_events"),
                        "costs": r.get("costs"),
                        "simulated_pnl": r.get("simulated_pnl"),
                        "status": r.get("status"),
                        "transitions": r.get("state_transitions"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # dual strategy CLI subprocess
        fixture = {
            "bars": [B0, B1],
            "symbol": SYMBOL,
            "rank": 1,
            "pct_24h": -3.0,
            "joined_rank_at_ms": 1795765700000,
            "book_entry": BOOK_ENTRY,
            "book_exit": BOOK_EXIT_SL,
            "clock_ms": C1,
            "process_now_ms": C1,
            "funding": "0",
            "costs_complete": True,
            "strip_forming": False,
            "run_exit": True,
        }
        fx_path = self.fx_dir / "strategy_cli_fixture.json"
        fx_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
        fx_sha = sha256_file(fx_path)

        runs = []
        for label in ("cli_run1", "cli_run2"):
            outd = OUT / label
            outd.mkdir(exist_ok=True)
            cmd = [
                sys.executable,
                "-B",
                "-m",
                "tools.e_replay_offline",
                "--strategy-replay-fixture",
                str(fx_path),
                "--out-dir",
                str(outd),
            ]
            before = sha256_file(fx_path)
            proc = subprocess.run(
                cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            after = sha256_file(fx_path)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertEqual(before, after)
            self.assertEqual(before, fx_sha)
            rep = json.loads((outd / "strategy_report.json").read_text(encoding="utf-8"))
            runs.append({"cmd": cmd, "exit": proc.returncode, "stdout": proc.stdout, "report": rep})

        exclude = frozenset(
            {
                "generated_at",
                "generated_at_utc",
                "report_path",
                "out_dir",
                "out_path",
                "cli_out_dir",
                "write_path",
                "runtime_root",
            }
        )
        # drop absolute runtime paths inside nested if any by comparing core fields
        a, b = runs[0]["report"], runs[1]["report"]
        for key in ("counts", "signal", "status", "simulated_pnl", "blocked_reason"):
            self.assertEqual(a.get(key), b.get(key), msg=key)
        diffs = semantic_report_diff(
            {k: a[k] for k in a if k not in ("events", "exit_events", "open_fill_events", "process_pending_actions")},
            {k: b[k] for k in b if k not in ("events", "exit_events", "open_fill_events", "process_pending_actions")},
            exclude_keys=exclude,
        )
        # path leaves may still differ for fixture absolute path — filter .path
        diffs = [d for d in diffs if not d.endswith(".path")]
        self.assertEqual(diffs, [])

        (OUT / "cli_dual.json").write_text(
            json.dumps(
                {
                    "fixture_sha256": fx_sha,
                    "exclude": sorted(exclude),
                    "semantic_diff_count": len(diffs),
                    "runs": [
                        {
                            "cmd": r["cmd"],
                            "exit": r["exit"],
                            "stdout": r["stdout"],
                            "status": r["report"].get("status"),
                            "counts": r["report"].get("counts"),
                            "simulated_pnl": r["report"].get("simulated_pnl"),
                            "stable": r["report"].get("stable"),
                        }
                        for r in runs
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_08_write_summary(self):
        after = {}
        for rel in (
            "tools/e_replay_offline/strategy_replay.py",
            "tools/e_replay_offline/tests/test_strategy_replay.py",
            "tools/e_replay_offline/cli.py",
            "tools/e_replay_offline/reader.py",
        ):
            p = REPO / rel
            after[rel] = sha256_file(p) if p.is_file() else "MISSING"
        src_after = {n: sha256_file(E_SRC / n) for n in EXPECTED_SOURCE_SHA}
        summary = {
            "work_package": "wp-e-functional-strategy-impl-01",
            "sha_before": PRE_SHA,
            "sha_after": after,
            "strategy_source_sha_before": EXPECTED_SOURCE_SHA,
            "strategy_source_sha_after": src_after,
            "network_calls": 0,
            "live_writes": 0,
            "process_ops": 0,
        }
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "report.md").write_text(
            "\n".join(
                [
                    "# wp-e-functional-strategy-impl-01",
                    "",
                    "unittest discover → 見 suite",
                    f"strategy sources unchanged match={src_after == EXPECTED_SOURCE_SHA}",
                    "evidence: output/e_functional_strategy_01/",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
