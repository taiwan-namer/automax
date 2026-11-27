"""wp-e-functional-a-evidence-02: persist slip/funding components + dual subprocess CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from tools.e_replay_offline.a_point_sim import (
    APointSimInputs,
    semantic_report_diff,
    simulate_a_point_exit,
)
from tools.e_replay_offline.reader import sha256_file

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_a_evidence_02"
FX = Path(__file__).resolve().parent / "fixtures"
SYMBOL = "SYNTHUSDT"
TRADE = ":SYNTHUSDT:a01"
A_OPEN = "2026-11-27T07:50:00Z"
A_CLOSE = "2026-11-27T07:51:00Z"
W0 = "2026-11-27T07:50:00Z"
W1 = "2026-11-27T07:51:00Z"
A_MS = 1795765800000
CUTOFF_MS = 1795765860000

PRE_SHA = {
    "tools/e_replay_offline/cli.py": (
        "78ed47576591d2122e7aa37d8697db83335a65e5447938cc194e357fd8e4ecc7"
    ),
    "tools/e_replay_offline/reader.py": (
        "f7b312b4ec848dbca7aa3cfcf8c6fabb1fbc22cbce728c29510f54246d8f6009"
    ),
    "tools/e_replay_offline/a_point_sim.py": (
        "ffb783bcc7b20d1a5a109c98bd010df2140669bb72814c0446f65ba37e9967d7"
    ),
    "tools/e_replay_offline/tests/test_a_evidence_02.py": "NEW",
}


def _fmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _indep(
    entry: Decimal,
    qty: Decimal,
    close: Decimal,
    entry_fee_r: Decimal,
    exit_fee_r: Decimal,
    slip: Decimal,
    funding: Decimal,
) -> dict[str, str]:
    exit_px = close * (Decimal("1") - slip)
    gross = (exit_px - entry) * qty
    entry_fee = entry * qty * entry_fee_r
    exit_fee = exit_px * qty * exit_fee_r
    net = gross - entry_fee - exit_fee + funding
    return {
        "formula": (
            f"exit=close*(1-slip); gross=(exit-entry)*qty; "
            f"entry_fee=entry*qty*r; exit_fee=exit*qty*r; net=gross-entry_fee-exit_fee+funding; "
            f"entry={entry} qty={qty} close={close} slip={slip} funding={funding} r_e={entry_fee_r} r_x={exit_fee_r}"
        ),
        "exit_px": _fmt(exit_px),
        "gross": _fmt(gross),
        "entry_fee": _fmt(entry_fee),
        "exit_fee": _fmt(exit_fee),
        "funding": _fmt(funding),
        "net": _fmt(net),
        "full_net_status_expected": "computed",
    }


def _bar(close: str = "110") -> dict:
    return {
        "symbol": SYMBOL,
        "interval": "1m",
        "open_time": A_MS,
        "close_time": A_MS + 59_999,
        "open": "100",
        "high": "120",
        "low": "90",
        "close": close,
        "synthetic": True,
    }


def _sim(bars_path: Path, *, slip: Decimal, funding: Decimal) -> dict:
    bars = json.loads(bars_path.read_text(encoding="utf-8"))
    return simulate_a_point_exit(
        APointSimInputs(
            symbol=SYMBOL,
            interval="1m",
            direction="LONG",
            entry_px=Decimal("100"),
            remaining_qty=Decimal("2"),
            a_point_utc=A_OPEN,
            market_cutoff_ms=CUTOFF_MS,
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=slip,
            funding=funding,
            bars=bars,
            bars_provided=True,
            bars_source_path=str(bars_path),
            window_start_utc=W0,
            window_end_utc=W1,
            half_open_end=True,
        )
    )


class TestAEvidence02(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if OUT.exists():
            # unique suffix — do not overwrite
            import time

            cls.out = REPO / "output" / f"e_functional_a_evidence_02_{int(time.time())}"
        else:
            cls.out = OUT
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.inputs = cls.out / "inputs"
        cls.inputs.mkdir(exist_ok=True)
        # persist fixed bars + copy fixtures (isolated, measured SHA)
        cls.bars_path = cls.inputs / "cli_bars_close110.json"
        cls.bars_path.write_text(json.dumps([_bar("110")], ensure_ascii=False), encoding="utf-8")
        cls.arm = cls.inputs / "arm_fills_synth.jsonl"
        cls.ledger_fills = cls.inputs / "ledger_fills_synth.jsonl"
        cls.ledger_orders = cls.inputs / "ledger_orders_synth.jsonl"
        shutil.copy2(FX / "arm_fills_synth.jsonl", cls.arm)
        shutil.copy2(FX / "ledger_fills_synth.jsonl", cls.ledger_fills)
        shutil.copy2(FX / "ledger_orders_synth.jsonl", cls.ledger_orders)

    def test_01_slip_and_funding_components(self):
        cases = []
        # case1 slip=0.01 funding=0
        exp1 = _indep(
            Decimal("100"),
            Decimal("2"),
            Decimal("110"),
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("0.01"),
            Decimal("0"),
        )
        self.assertEqual(exp1["gross"], "17.8")
        self.assertEqual(exp1["entry_fee"], "0.2")
        self.assertEqual(exp1["exit_fee"], "0.2178")
        self.assertEqual(exp1["funding"], "0")
        self.assertEqual(exp1["net"], "17.3822")
        r1 = _sim(self.bars_path, slip=Decimal("0.01"), funding=Decimal("0"))
        # field mapping: simulated_pnl→net; full_net_status actual longer label
        act1 = {
            "gross": r1.get("gross"),
            "entry_fee": r1.get("entry_fee"),
            "exit_fee": r1.get("exit_fee"),
            "funding": r1.get("funding"),
            "net": r1.get("simulated_pnl"),
            "full_net_status": r1.get("full_net_status"),
            "blocked": r1.get("blocked"),
        }
        self.assertEqual(act1["gross"], "17.8")
        self.assertEqual(act1["entry_fee"], "0.2")
        self.assertEqual(act1["exit_fee"], "0.2178")
        self.assertEqual(act1["funding"], "0")
        self.assertEqual(act1["net"], "17.3822")
        self.assertEqual(act1["full_net_status"], "computed_under_model_assumptions")
        cases.append(
            {
                "name": "slip_0_01",
                "field_mapping": {
                    "net": "a_point.simulated_pnl",
                    "full_net_status_computed": "full_net_status==computed_under_model_assumptions",
                },
                "expected": exp1,
                "actual": act1,
            }
        )

        # case2 slip=0 funding=-0.5
        exp2 = _indep(
            Decimal("100"),
            Decimal("2"),
            Decimal("110"),
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("0"),
            Decimal("-0.5"),
        )
        self.assertEqual(exp2["gross"], "20")
        self.assertEqual(exp2["entry_fee"], "0.2")
        self.assertEqual(exp2["exit_fee"], "0.22")
        self.assertEqual(exp2["funding"], "-0.5")
        self.assertEqual(exp2["net"], "19.08")
        r2 = _sim(self.bars_path, slip=Decimal("0"), funding=Decimal("-0.5"))
        act2 = {
            "gross": r2.get("gross"),
            "entry_fee": r2.get("entry_fee"),
            "exit_fee": r2.get("exit_fee"),
            "funding": r2.get("funding"),
            "net": r2.get("simulated_pnl"),
            "full_net_status": r2.get("full_net_status"),
            "blocked": r2.get("blocked"),
        }
        self.assertEqual(act2["gross"], "20")
        self.assertEqual(act2["entry_fee"], "0.2")
        self.assertEqual(act2["exit_fee"], "0.22")
        self.assertEqual(act2["funding"], "-0.5")
        self.assertEqual(act2["net"], "19.08")
        cases.append(
            {
                "name": "funding_neg_0_5",
                "field_mapping": {
                    "net": "a_point.simulated_pnl",
                    "full_net_status_computed": "full_net_status==computed_under_model_assumptions",
                },
                "expected": exp2,
                "actual": act2,
            }
        )

        (self.out / "component_cases.json").write_text(
            json.dumps({"cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_02_dual_subprocess_cli(self):
        run1 = self.out / "cli_run1"
        run2 = self.out / "cli_run2"
        run1.mkdir(exist_ok=True)
        run2.mkdir(exist_ok=True)

        def build_cmd(out_dir: Path) -> list[str]:
            return [
                sys.executable,
                "-B",
                "-m",
                "tools.e_replay_offline",
                "--arm-fills",
                str(self.arm),
                "--ledger-fills",
                str(self.ledger_fills),
                "--ledger-orders",
                str(self.ledger_orders),
                "--bars",
                str(self.bars_path),
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
                "--out-dir",
                str(out_dir),
            ]

        # input SHA before both runs
        input_files = {
            "arm_fills": self.arm,
            "ledger_fills": self.ledger_fills,
            "ledger_orders": self.ledger_orders,
            "bars": self.bars_path,
        }
        sha_before = {k: sha256_file(p) for k, p in input_files.items()}

        results = []
        for label, out_dir in (("run1", run1), ("run2", run2)):
            cmd = build_cmd(out_dir)
            proc = subprocess.run(
                cmd,
                cwd=str(REPO),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            sha_after = {k: sha256_file(p) for k, p in input_files.items()}
            report_path = out_dir / "report.json"
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            entry = {
                "label": label,
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "input_sha256_before": dict(sha_before),
                "input_sha256_after": sha_after,
                "input_sha_equal": sha_before == sha_after,
                "report_stable": report.get("stable"),
                "sources": report.get("sources"),
                "a_point_gross": (report.get("a_point") or {}).get("gross"),
                "a_point_simulated_pnl": (report.get("a_point") or {}).get("simulated_pnl"),
                "report_path": str(report_path),
            }
            results.append(entry)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertTrue(entry["input_sha_equal"])
            self.assertTrue(report.get("stable"))
            self.assertEqual(entry["a_point_gross"], "20")
            self.assertEqual(entry["a_point_simulated_pnl"], "19.58")
            # per-source stable
            for lab, src in (report.get("sources") or {}).items():
                if src.get("path"):
                    self.assertTrue(src.get("stable"), msg=lab)
                    self.assertEqual(src.get("sha256_before"), src.get("sha256_after"))

        r1 = json.loads((run1 / "report.json").read_text(encoding="utf-8"))
        r2 = json.loads((run2 / "report.json").read_text(encoding="utf-8"))
        # Only exclude generation time + explicit output path fields (NOT all path keys)
        exclude = frozenset(
            {
                "generated_at",
                "generated_at_utc",
                "report_path",
                "out_dir",
                "out_path",
                "cli_out_dir",
                "write_path",
            }
        )
        diffs = semantic_report_diff(r1, r2, exclude_keys=exclude)
        self.assertEqual(diffs, [], msg=f"semantic diffs={diffs}")

        bundle = {
            "work_package": "wp-e-functional-a-evidence-02",
            "network_calls": 0,
            "live_writes": 0,
            "exclude_fields": sorted(exclude),
            "exclude_note": "不遞迴刪除 path／來源身分；僅排除生成時間與明確輸出路徑欄位",
            "input_sha256": {k: sha256_file(p) for k, p in input_files.items()},
            "cli_runs": results,
            "semantic_diff_count": len(diffs),
            "semantic_diffs": diffs,
        }
        (self.out / "cli_dual_subprocess.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_03_write_summary(self):
        after = {}
        for rel in PRE_SHA:
            p = REPO / rel
            after[rel] = sha256_file(p) if p.is_file() else "MISSING"
        summary = {
            "out_dir": str(self.out),
            "sha_before": PRE_SHA,
            "sha_after": after,
            "component_cases": str(self.out / "component_cases.json"),
            "cli_dual": str(self.out / "cli_dual_subprocess.json"),
        }
        (self.out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = "\n".join(
            [
                "# wp-e-functional-a-evidence-02",
                "",
                f"out={self.out}",
                "unittest discover → 見 suite 退出碼",
                "",
                "## SHA before→after",
                *[f"- {k}: {PRE_SHA[k]} → {after[k]}" for k in PRE_SHA],
                "",
                "## 驗收摘要",
                "- slip: 17.8/0.2/0.2178/0/17.3822 computed_under_model_assumptions",
                "- funding: 20/0.2/0.22/-0.5/19.08",
                "- dual CLI subprocess EXIT=0×2；input SHA equal；stable=true；gross=20；pnl=19.58；semantic_diff=0",
                "- exclude: generated_at, generated_at_utc, report_path, out_dir, out_path, cli_out_dir, write_path",
            ]
        )
        (self.out / "report.md").write_text(report + "\n", encoding="utf-8")
        self.assertTrue((self.out / "component_cases.json").is_file())
        self.assertTrue((self.out / "cli_dual_subprocess.json").is_file())


if __name__ == "__main__":
    unittest.main()
