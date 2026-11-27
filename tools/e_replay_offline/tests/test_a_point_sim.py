"""wp-e-functional-a-01: A-point counterfactual exit valuation (synthetic only)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tools.e_replay_offline.a_point_sim import (
    APointSimInputs,
    semantic_report_diff,
    simulate_a_point_exit,
)
from tools.e_replay_offline.cli import main as cli_main
from tools.e_replay_offline.reader import ReportInputs, build_report, sha256_file

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_a_01"
FX = Path(__file__).resolve().parent / "fixtures"
TRADE = ":SYNTHUSDT:a01"
SYMBOL = "SYNTHUSDT"
# Single-bar half-open window → expected=1 (A bar itself)
A_OPEN = "2026-11-27T07:50:00Z"
A_CLOSE = "2026-11-27T07:51:00Z"
W0 = "2026-11-27T07:50:00Z"
W1 = "2026-11-27T07:51:00Z"
A_MS = 1795765800000  # 2026-11-27T07:50:00Z
CUTOFF_MS = 1795765860000  # end of A bar minute


def _indep_long_net(
    entry: Decimal,
    qty: Decimal,
    close: Decimal,
    entry_fee_r: Decimal,
    exit_fee_r: Decimal,
    slip: Decimal,
    funding: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    exit_px = close * (Decimal("1") - slip)
    gross = (exit_px - entry) * qty
    entry_fee = entry * qty * entry_fee_r
    exit_fee = exit_px * qty * exit_fee_r
    net = gross - entry_fee - exit_fee + funding
    return exit_px, gross, entry_fee, exit_fee, net


def _indep_short_net(
    entry: Decimal,
    qty: Decimal,
    close: Decimal,
    entry_fee_r: Decimal,
    exit_fee_r: Decimal,
    slip: Decimal,
    funding: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    exit_px = close * (Decimal("1") + slip)
    gross = (entry - exit_px) * qty
    entry_fee = entry * qty * entry_fee_r
    exit_fee = exit_px * qty * exit_fee_r
    net = gross - entry_fee - exit_fee + funding
    return exit_px, gross, entry_fee, exit_fee, net


def _bar(
    ot: int = A_MS,
    *,
    close: str = "110",
    symbol: str = SYMBOL,
    forming: bool = False,
) -> dict:
    return {
        "symbol": symbol,
        "interval": "1m",
        "open_time": ot,
        "close_time": (ot + 120_000) if forming else (ot + 59_999),
        "open": "100",
        "high": "120",
        "low": "90",
        "close": close,
        "synthetic": True,
    }


def _write_bars(td: Path, name: str, bars: list[dict]) -> Path:
    p = td / name
    p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    return p


def _sim_kwargs(bars_path: Path | None, **kw) -> dict:
    d = dict(
        symbol=SYMBOL,
        interval="1m",
        direction="LONG",
        entry_px=Decimal("100"),
        remaining_qty=Decimal("2"),
        a_point_utc=A_OPEN,
        market_cutoff_ms=CUTOFF_MS,
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0"),
        funding=Decimal("0"),
        bars=None,
        bars_provided=bars_path is not None,
        bars_source_path=str(bars_path) if bars_path else None,
        window_start_utc=W0,
        window_end_utc=W1,
        half_open_end=True,
    )
    if bars_path is not None:
        d["bars"] = json.loads(bars_path.read_text(encoding="utf-8"))
        d["bars_provided"] = True
    d.update(kw)
    return d


def _report_inputs(bars_path: Path | None, **kw) -> ReportInputs:
    d = dict(
        arm_fills=FX / "arm_fills_synth.jsonl",
        ledger_fills=FX / "ledger_fills_synth.jsonl",
        ledger_orders=FX / "ledger_orders_synth.jsonl",
        bars_path=bars_path,
        trade_id=TRADE,
        window_start_utc=W0,
        window_end_utc=W1,
        a_bar_open_utc=A_OPEN,
        a_bar_close_utc=A_CLOSE,
        bars_symbol=SYMBOL,
        bars_interval="1m",
        bars_half_open_end=True,
        bars_now_ms=CUTOFF_MS,
        simulate_a_point=True,
        sim_direction="LONG",
        sim_entry_px="100",
        sim_remaining_qty="2",
        sim_entry_fee_rate="0.001",
        sim_exit_fee_rate="0.001",
        sim_slippage_rate="0",
        sim_funding="0",
    )
    d.update(kw)
    return ReportInputs(**d)


class TestAPointSim(unittest.TestCase):
    # Recorded before this work package created/edited these paths
    PRE_SHA_KNOWN = {
        "tools/e_replay_offline/reader.py": (
            "6de23afbc8cb936624c355d5cfd0033e67470642d51bffdd5544f95bf055bbee"
        ),
        "tools/e_replay_offline/cli.py": (
            "75b48c2f4ce5b25db98ee52b8315f8c17a4a428344891604a7f282b0cac836c3"
        ),
        "tools/e_replay_offline/a_point_sim.py": "NEW",
        "tools/e_replay_offline/tests/test_a_point_sim.py": "NEW",
    }

    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.pre_sha = dict(cls.PRE_SHA_KNOWN)

    def test_01_fixed_long_gross20_net1958(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "long.json", [_bar(close="110")])
            r = simulate_a_point_exit(APointSimInputs(**_sim_kwargs(bp, direction="LONG")))
            exp_exit, exp_gross, _, _, exp_net = _indep_long_net(
                Decimal("100"),
                Decimal("2"),
                Decimal("110"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("0"),
                Decimal("0"),
            )
            self.assertEqual(exp_gross, Decimal("20"))
            self.assertEqual(exp_net, Decimal("19.58"))
            self.assertFalse(r["blocked"])
            self.assertEqual(Decimal(r["gross"]), exp_gross)
            self.assertEqual(Decimal(r["simulated_pnl"]), exp_net)
            self.assertEqual(Decimal(r["a_exit_px"]), exp_exit)
            self.assertEqual(r["full_net_status"], "computed_under_model_assumptions")
            self.assertFalse(r["model_assumptions"]["fill_claim"])

    def test_02_fixed_short_gross_neg20_net_neg2042(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "short.json", [_bar(close="110")])
            r = simulate_a_point_exit(APointSimInputs(**_sim_kwargs(bp, direction="SHORT")))
            _, exp_gross, _, _, exp_net = _indep_short_net(
                Decimal("100"),
                Decimal("2"),
                Decimal("110"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("0"),
                Decimal("0"),
            )
            self.assertEqual(exp_gross, Decimal("-20"))
            self.assertEqual(exp_net, Decimal("-20.42"))
            self.assertEqual(Decimal(r["gross"]), exp_gross)
            self.assertEqual(Decimal(r["simulated_pnl"]), exp_net)

    def test_03_nonzero_slippage_independent(self):
        slip = Decimal("0.01")
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "slip.json", [_bar(close="110")])
            r = simulate_a_point_exit(
                APointSimInputs(**_sim_kwargs(bp, direction="LONG", slippage_rate=slip))
            )
            _, exp_gross, _, _, exp_net = _indep_long_net(
                Decimal("100"),
                Decimal("2"),
                Decimal("110"),
                Decimal("0.001"),
                Decimal("0.001"),
                slip,
                Decimal("0"),
            )
            self.assertEqual(Decimal(r["gross"]), exp_gross)
            self.assertEqual(Decimal(r["simulated_pnl"]), exp_net)

    def test_04_signed_funding_independent(self):
        funding = Decimal("-0.5")
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "fund.json", [_bar(close="110")])
            r = simulate_a_point_exit(
                APointSimInputs(**_sim_kwargs(bp, direction="LONG", funding=funding))
            )
            _, _, _, _, exp_net = _indep_long_net(
                Decimal("100"),
                Decimal("2"),
                Decimal("110"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("0"),
                funding,
            )
            self.assertEqual(Decimal(r["simulated_pnl"]), exp_net)
            self.assertEqual(Decimal(r["funding"]), funding)

    def test_05_missing_costs_gross_only_net_not_computable(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "nocost.json", [_bar(close="110")])
            r = simulate_a_point_exit(
                APointSimInputs(
                    **_sim_kwargs(
                        bp,
                        entry_fee_rate=None,
                        exit_fee_rate=None,
                        funding=None,
                        # slippage present so exit/gross computable
                        slippage_rate=Decimal("0"),
                    )
                )
            )
            self.assertIsNone(r["simulated_pnl"])
            self.assertIsNone(r["full_net_pnl"])
            self.assertEqual(r["full_net_status"], "not_computable")
            self.assertIsNotNone(r["gross"])
            self.assertIn("entry_fee_rate", r["costs_missing"])
            self.assertIn("funding", r["costs_missing"])
            # independent gross check
            exp_exit = Decimal("110") * (Decimal("1") - Decimal("0"))
            exp_gross = (exp_exit - Decimal("100")) * Decimal("2")
            self.assertEqual(Decimal(r["gross"]), exp_gross)

    def test_06_missing_bars_blocked(self):
        r = simulate_a_point_exit(
            APointSimInputs(**_sim_kwargs(None, bars=None, bars_provided=False))
        )
        self.assertTrue(r["blocked"])
        self.assertEqual(r["blocked_reason"], "PRICE_PATH_MISSING")
        self.assertIsNone(r["simulated_pnl"])

    def test_07_duplicate_keys_blocked(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "dup.json", [_bar(), _bar()])
            r = simulate_a_point_exit(APointSimInputs(**_sim_kwargs(bp)))
            self.assertTrue(r["blocked"])
            self.assertEqual(r["blocked_reason"], "DUPLICATE_BAR_KEYS")
            self.assertIsNone(r["simulated_pnl"])

    def test_08_a_bar_not_closed_blocked(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "forming.json", [_bar(forming=True)])
            r = simulate_a_point_exit(APointSimInputs(**_sim_kwargs(bp)))
            self.assertTrue(r["blocked"])
            self.assertEqual(r["blocked_reason"], "A_BAR_NOT_CLOSED")
            self.assertIsNone(r["simulated_pnl"])

    def test_09_a_point_outside_window_blocked(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            # bar at A, but declare A point earlier → outside
            bp = _write_bars(Path(td), "out.json", [_bar()])
            r = simulate_a_point_exit(
                APointSimInputs(
                    **_sim_kwargs(
                        bp,
                        a_point_utc="2026-11-27T07:40:00Z",
                    )
                )
            )
            self.assertTrue(r["blocked"])
            self.assertEqual(r["blocked_reason"], "A_POINT_OUTSIDE_WINDOW")
            self.assertIsNone(r["simulated_pnl"])

    def test_10_invalid_direction_and_qty(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "inv.json", [_bar()])
            r1 = simulate_a_point_exit(
                APointSimInputs(**_sim_kwargs(bp, direction="SIDEWAYS"))
            )
            self.assertTrue(r1["blocked"])
            self.assertEqual(r1["blocked_reason"], "INVALID_DIRECTION")
            self.assertIsNone(r1["simulated_pnl"])
            r2 = simulate_a_point_exit(
                APointSimInputs(**_sim_kwargs(bp, remaining_qty=Decimal("0")))
            )
            self.assertTrue(r2["blocked"])
            self.assertEqual(r2["blocked_reason"], "INVALID_QTY")
            self.assertIsNone(r2["simulated_pnl"])

    def test_11_build_report_wires_sim(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            bp = _write_bars(Path(td), "wire.json", [_bar(close="110")])
            rep = build_report(_report_inputs(bp))
            ap = rep["a_point"]
            self.assertEqual(Decimal(ap["simulated_pnl"]), Decimal("19.58"))
            self.assertEqual(Decimal(ap["gross"]), Decimal("20"))

    def test_12_cli_determinism_twice(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            td_path = Path(td)
            bp = _write_bars(td_path, "cli_bars.json", [_bar(close="110")])
            out1 = td_path / "run1"
            out2 = td_path / "run2"
            out1.mkdir()
            out2.mkdir()
            common = [
                "--arm-fills",
                str(FX / "arm_fills_synth.jsonl"),
                "--ledger-fills",
                str(FX / "ledger_fills_synth.jsonl"),
                "--ledger-orders",
                str(FX / "ledger_orders_synth.jsonl"),
                "--bars",
                str(bp),
                "--trade-id",
                TRADE,
                "--window-start",
                W0,
                "--window-end",
                W1,
                "--a-open",
                A_OPEN,
                "--a-close",
                A_CLOSE,
                "--bars-symbol",
                SYMBOL,
                "--bars-interval",
                "1m",
                "--bars-half-open-end",
                "--bars-now-ms",
                str(CUTOFF_MS),
                "--simulate-a-point",
                "--sim-direction",
                "LONG",
                "--sim-entry-px",
                "100",
                "--sim-remaining-qty",
                "2",
                "--sim-entry-fee-rate",
                "0.001",
                "--sim-exit-fee-rate",
                "0.001",
                "--sim-slippage-rate",
                "0",
                "--sim-funding",
                "0",
                "--omit-events",
            ]
            rc1 = cli_main([*common, "--out-dir", str(out1)])
            rc2 = cli_main([*common, "--out-dir", str(out2)])
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            r1 = json.loads((out1 / "report.json").read_text(encoding="utf-8"))
            r2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))
            # strip path-like source fields that embed out-dir / absolute paths
            exclude = frozenset(
                {
                    "generated_at",
                    "generated_at_utc",
                    "report_path",
                    "out_dir",
                    "out_path",
                    "path",  # source path absolute may differ only if copied; same here
                }
            )
            # Compare a_point + event_n + trade_id semantically; sources sha should match
            diffs = semantic_report_diff(r1, r2, exclude_keys=exclude)
            # sources.*.path may differ if resolve differs — filter path leaves
            diffs = [d for d in diffs if not d.endswith(".path") and ".path." not in d]
            self.assertEqual(
                diffs,
                [],
                msg=f"semantic diffs={diffs}; exclude={sorted(exclude)}",
            )
            self.assertEqual(r1["a_point"]["simulated_pnl"], "19.58")
            self.assertEqual(r2["a_point"]["simulated_pnl"], "19.58")

    def test_13_write_evidence_bundle(self):
        """Produce output/e_functional_a_01 evidence (isolated; no live writes)."""
        OUT.mkdir(parents=True, exist_ok=True)
        cases = []
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            td_path = Path(td)
            long_p = _write_bars(td_path, "case_long.json", [_bar(close="110")])
            short_p = _write_bars(td_path, "case_short.json", [_bar(close="110")])
            miss_p = None
            dup_p = _write_bars(td_path, "case_dup.json", [_bar(), _bar()])
            form_p = _write_bars(td_path, "case_forming.json", [_bar(forming=True)])

            matrix = [
                ("fixed_long", long_p, {"direction": "LONG"}, "19.58", "20"),
                ("fixed_short", short_p, {"direction": "SHORT"}, "-20.42", "-20"),
            ]
            for name, path, kw, exp_net, exp_gross in matrix:
                r = simulate_a_point_exit(
                    APointSimInputs(**_sim_kwargs(path, **kw))
                )
                cases.append(
                    {
                        "name": name,
                        "expected": {"gross": exp_gross, "net": exp_net, "blocked": False},
                        "actual": {
                            "gross": r.get("gross"),
                            "simulated_pnl": r.get("simulated_pnl"),
                            "blocked": r.get("blocked"),
                            "blocked_reason": r.get("blocked_reason"),
                            "source_sha256": (r.get("source_identity") or {}).get("sha256"),
                        },
                    }
                )

            # negatives
            for name, kwargs, reason in (
                (
                    "bars_not_provided",
                    {"bars": None, "bars_provided": False, "bars_source_path": None},
                    "PRICE_PATH_MISSING",
                ),
                ("duplicate", {}, "DUPLICATE_BAR_KEYS"),
                ("forming", {}, "A_BAR_NOT_CLOSED"),
                (
                    "outside",
                    {"a_point_utc": "2026-11-27T07:40:00Z"},
                    "A_POINT_OUTSIDE_WINDOW",
                ),
                ("bad_dir", {"direction": "X"}, "INVALID_DIRECTION"),
                ("bad_qty", {"remaining_qty": Decimal("0")}, "INVALID_QTY"),
            ):
                path = {
                    "bars_not_provided": miss_p,
                    "duplicate": dup_p,
                    "forming": form_p,
                    "outside": long_p,
                    "bad_dir": long_p,
                    "bad_qty": long_p,
                }[name]
                r = simulate_a_point_exit(APointSimInputs(**_sim_kwargs(path, **kwargs)))
                cases.append(
                    {
                        "name": name,
                        "expected": {
                            "blocked": True,
                            "blocked_reason": reason,
                            "simulated_pnl": None,
                        },
                        "actual": {
                            "blocked": r.get("blocked"),
                            "blocked_reason": r.get("blocked_reason"),
                            "simulated_pnl": r.get("simulated_pnl"),
                        },
                    }
                )

            # missing costs
            r_mc = simulate_a_point_exit(
                APointSimInputs(
                    **_sim_kwargs(
                        long_p,
                        entry_fee_rate=None,
                        exit_fee_rate=None,
                        funding=None,
                    )
                )
            )
            cases.append(
                {
                    "name": "missing_costs",
                    "expected": {
                        "simulated_pnl": None,
                        "full_net_status": "not_computable",
                        "gross_present": True,
                    },
                    "actual": {
                        "simulated_pnl": r_mc.get("simulated_pnl"),
                        "full_net_status": r_mc.get("full_net_status"),
                        "gross": r_mc.get("gross"),
                        "costs_missing": r_mc.get("costs_missing"),
                    },
                }
            )

        after_sha = {}
        for rel in self.pre_sha:
            p = REPO / rel
            after_sha[rel] = sha256_file(p) if p.is_file() else "MISSING"

        evidence = {
            "work_package": "wp-e-functional-a-01",
            "network_calls": 0,
            "live_writes": 0,
            "model_assumptions": {
                "fill_claim": False,
                "maker_fill_rate_claimed": False,
            },
            "fixed_expected_independent": {
                "long": {"gross": "20", "net": "19.58"},
                "short": {"gross": "-20", "net": "-20.42"},
            },
            "cli_exclude_fields": [
                "generated_at",
                "generated_at_utc",
                "report_path",
                "out_dir",
                "out_path",
                "path",
            ],
            "sha_before": self.pre_sha,
            "sha_after": after_sha,
            "cases": cases,
        }
        (OUT / "evidence_cases.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_md = [
            "# wp-e-functional-a-01",
            "",
            "## 命令／退出碼",
            '`python -B -m unittest discover -s tools/e_replay_offline/tests -p "test_*.py" -v`',
            "",
            "## 固定案例（獨立算式）",
            "- LONG entry=100 qty=2 close=110 fee=0.001 slip=0 funding=0 → gross=20 net=19.58",
            "- SHORT 同輸入 → gross=-20 net=-20.42",
            "",
            "## 模型假設",
            "- 價格來源=A點已收盤K線close；不宣稱可成交／Maker成交率",
            "- 缺成本不得默認為零",
            "",
            "## SHA256",
        ]
        for rel in after_sha:
            report_md.append(f"- {rel}: before={self.pre_sha[rel]} after={after_sha[rel]}")
        report_md.append("")
        report_md.append("證據：output/e_functional_a_01/evidence_cases.json")
        (OUT / "report.md").write_text("\n".join(report_md) + "\n", encoding="utf-8")
        self.assertTrue((OUT / "evidence_cases.json").is_file())


if __name__ == "__main__":
    unittest.main()
