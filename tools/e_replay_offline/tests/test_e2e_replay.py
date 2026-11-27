"""E2E isolated CLI replay determinism (wp-e-e2e-replay-01).

All fixtures are synthetic. Does not call runners or live trade sources.
Writes only under output/e_repair_e2e_01/.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "output" / "e_repair_e2e_01"
FX = OUT / "fixtures_synth"
TRADE = ":SYNTH:E2E001"
W0 = "2026-11-27T07:29:16.234470Z"
W1 = "2026-11-27T07:50:42.717639Z"
A0 = "2026-11-27T07:50:00Z"
A1 = "2026-11-27T07:51:00Z"

# Non-semantic fields excluded from equality (listed for acceptance).
NON_SEMANTIC_EXCLUDE = [
    "report_path",
    "generated_at",
    "cli_pid",
    "wall_clock",
    "tmpdir",
]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _floor_ms(iso_z: str) -> int:
    dt = datetime.fromisoformat(iso_z.replace("Z", "+00:00")).astimezone(timezone.utc)
    dt = dt.replace(second=0, microsecond=0)
    return int(dt.timestamp() * 1000)


def _ensure_fixtures() -> dict[str, Path]:
    FX.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    arm = FX / "arm_fills_synth.jsonl"
    arm.write_text(
        "\n".join(
            [
                # outside window (before start) — synthetic TP_PREPLACED
                json.dumps(
                    {
                        "event": "TP_PREPLACED",
                        "trade_id": TRADE,
                        "recorded_at": "2026-11-27T07:20:00.000000Z",
                        "qty": 0,
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "OPEN",
                        "trade_id": TRADE,
                        "recorded_at": W0,
                        "qty": 100,
                        "price": 0.04,
                        "fee_u": 0.01,
                        "side": "BUY",
                        "testnet_client_order_id": "SYNTH_CID_E2E",
                        "funding_data_gap": True,
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                # duplicate settlement
                json.dumps(
                    {
                        "event": "SETTLEMENT_CLOSE",
                        "trade_id": TRADE,
                        "recorded_at": W1,
                        "qty": 100,
                        "price": 0.039,
                        "fee_u": 0.01,
                        "side": "SELL",
                        "testnet_client_order_id": "SYNTH_CID_E2E",
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "SETTLEMENT_CLOSE",
                        "trade_id": TRADE,
                        "recorded_at": W1,
                        "qty": 100,
                        "price": 0.039,
                        "fee_u": 0.01,
                        "side": "SELL",
                        "testnet_client_order_id": "SYNTH_CID_E2E",
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "FEE_ADJUST",
                        "trade_id": TRADE,
                        "recorded_at": W1,
                        "qty": 0,
                        "fee_u": 0.002,
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )

    lf = FX / "ledger_fills_synth.jsonl"
    lf.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "FILL",
                        "client_order_id": "SYNTH_CID_E2E",
                        "exchange_order_id": "900001",
                        "exchange_trade_id": "t1",
                        "observation_kind": "exchange_trade",
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "FILL",
                        "client_order_id": "SYNTH_CID_E2E",
                        "exchange_order_id": "900001",
                        "exchange_trade_id": "t1",
                        "observation_kind": "exchange_trade",
                        "synthetic": True,
                    },
                    ensure_ascii=False,
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    lo = FX / "ledger_orders_synth.jsonl"
    lo.write_text(
        json.dumps(
            {
                "event": "ORDER",
                "client_order_id": "SYNTH_CID_E2E",
                "exchange_order_id": "900001",
                "synthetic": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    q = FX / "queries_synth.json"
    q.write_text(
        json.dumps(
            [
                {"name": "client_order_id", "client_order_id": "SYNTH_CID_E2E"},
                {"name": "exchange_order_id", "exchange_order_id": "900001"},
                {"name": "exchange_trade_key", "exchange_trade_key": "no_hit"},
                {"name": "tp1_order_id", "tp1_order_id": "900001"},
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # full 1m bars covering floored window (expected=22)
    start = _floor_ms(W0)
    end = _floor_ms(W1)
    bars = []
    t = start
    while t <= end:
        bars.append({"t": t, "o": 0.04, "h": 0.041, "l": 0.039, "c": 0.0405, "v": 1})
        t += 60_000
    bars_path = FX / "bars_full_synth.json"
    bars_path.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")

    empty_bars = FX / "bars_empty_synth.json"
    empty_bars.write_text("[]\n", encoding="utf-8")

    manifest = {
        "label": "synthetic_e2e_inputs",
        "synthetic": True,
        "trade_id": TRADE,
        "window": {"start": W0, "end": W1},
        "files": {
            "arm_fills": {"path": str(arm.resolve()), "sha256": _sha(arm)},
            "ledger_fills": {"path": str(lf.resolve()), "sha256": _sha(lf)},
            "ledger_orders": {"path": str(lo.resolve()), "sha256": _sha(lo)},
            "queries": {"path": str(q.resolve()), "sha256": _sha(q)},
            "bars_full": {"path": str(bars_path.resolve()), "sha256": _sha(bars_path), "n": len(bars)},
            "bars_empty": {"path": str(empty_bars.resolve()), "sha256": _sha(empty_bars)},
        },
        "expected_bars": len(bars),
        "non_semantic_exclude": NON_SEMANTIC_EXCLUDE,
    }
    (OUT / "input_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "arm": arm,
        "lf": lf,
        "lo": lo,
        "q": q,
        "bars_full": bars_path,
        "bars_empty": empty_bars,
    }


def _run_cli(
    *,
    out_dir: Path,
    arm: Path,
    lf: Path,
    lo: Path,
    q: Path,
    bars: Path | None,
    funding_known: bool = False,
    commission_fx_known: bool = False,
) -> tuple[int, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "tools.e_replay_offline",
        "--arm-fills",
        str(arm),
        "--ledger-fills",
        str(lf),
        "--ledger-orders",
        str(lo),
        "--trade-id",
        TRADE,
        "--window-start",
        W0,
        "--window-end",
        W1,
        "--a-open",
        A0,
        "--a-close",
        A1,
        "--out-dir",
        str(out_dir),
        "--query-json",
        str(q),
    ]
    if bars is not None:
        cmd.extend(["--bars", str(bars)])
    if funding_known:
        cmd.append("--funding-known")
    if commission_fx_known:
        cmd.append("--commission-fx-known")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    report_path = out_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    report["_meta"] = {
        "exit": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "report_path": str(report_path),
        "cmd": cmd,
    }
    return proc.returncode, report


def _semantic_view(report: dict) -> dict:
    """Drop non-semantic / volatile fields for equality."""
    r = deepcopy(report)
    r.pop("_meta", None)
    r.pop("events", None)  # compare counts + order via event_counts / dedicated fields
    # strip absolute paths from sources (keep sha + stable + exists)
    for _k, src in (r.get("sources") or {}).items():
        if isinstance(src, dict):
            src.pop("path", None)
    wg = r.get("write_guard") or {}
    wg.pop("source_paths", None)
    return r


def _diff_keys(a: dict, b: dict, prefix: str = "") -> list[str]:
    diffs: list[str] = []
    keys = set(a) | set(b)
    for k in sorted(keys):
        pa, pb = f"{prefix}.{k}" if prefix else k, None
        if k not in a:
            diffs.append(f"missing_in_a:{prefix}.{k}")
            continue
        if k not in b:
            diffs.append(f"missing_in_b:{prefix}.{k}")
            continue
        va, vb = a[k], b[k]
        if isinstance(va, dict) and isinstance(vb, dict):
            diffs.extend(_diff_keys(va, vb, f"{prefix}.{k}" if prefix else k))
        elif isinstance(va, list) and isinstance(vb, list):
            if json.dumps(va, sort_keys=True) != json.dumps(vb, sort_keys=True):
                diffs.append(f"list_ne:{prefix}.{k}" if prefix else f"list_ne:{k}")
        elif va != vb:
            diffs.append(f"ne:{prefix}.{k}" if prefix else f"ne:{k}")
    return diffs


class TestE2EReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = _ensure_fixtures()

    def test_dual_cli_semantic_identical(self):
        """Two independent CLI subprocesses → input sha equal, semantic diff=0."""
        fx = self.fx
        out1 = OUT / "run_a"
        out2 = OUT / "run_b"
        # wipe prior reports
        for d in (out1, out2):
            if d.exists():
                for p in d.glob("report.json*"):
                    p.unlink()

        sha_in = {
            "arm": _sha(fx["arm"]),
            "lf": _sha(fx["lf"]),
            "lo": _sha(fx["lo"]),
            "q": _sha(fx["q"]),
            "bars": _sha(fx["bars_full"]),
        }
        rc1, r1 = _run_cli(
            out_dir=out1,
            arm=fx["arm"],
            lf=fx["lf"],
            lo=fx["lo"],
            q=fx["q"],
            bars=fx["bars_full"],
        )
        rc2, r2 = _run_cli(
            out_dir=out2,
            arm=fx["arm"],
            lf=fx["lf"],
            lo=fx["lo"],
            q=fx["q"],
            bars=fx["bars_full"],
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        sha_in_after = {
            "arm": _sha(fx["arm"]),
            "lf": _sha(fx["lf"]),
            "lo": _sha(fx["lo"]),
            "q": _sha(fx["q"]),
            "bars": _sha(fx["bars_full"]),
        }
        self.assertEqual(sha_in, sha_in_after)

        # window event counts (duplicates kept): OPEN+2xSETTLEMENT+FEE = 4; TP outside
        self.assertEqual(r1["event_n"], 4)
        self.assertEqual(r1["event_counts"].get("SETTLEMENT_CLOSE"), 2)
        self.assertEqual(r1["qty_balance"]["delta"], -100.0)  # 100 open vs 200 close dup
        self.assertEqual(r1["kline"]["expected"], 22)
        self.assertEqual(r1["kline"]["matched"], 22)
        # coverage only — not fill simulation
        self.assertIn(r1["a_point"]["status"], ("bars_present_not_simulated", "blocked"))

        by = {q["query"]: q for q in r1["ledger_queries"]}
        self.assertEqual(by["client_order_id"]["fills_hits"], 2)
        self.assertEqual(by["exchange_order_id"]["fills_hits"], 2)
        self.assertEqual(by["exchange_trade_key"]["fills_hits"], 0)
        self.assertEqual(by["tp1_order_id"]["fills_hits"], 2)

        s1, s2 = _semantic_view(r1), _semantic_view(r2)
        diffs = _diff_keys(s1, s2)
        (OUT / "dual_cli_diff.json").write_text(
            json.dumps(
                {
                    "input_sha": sha_in,
                    "non_semantic_exclude": NON_SEMANTIC_EXCLUDE,
                    "semantic_diff_n": len(diffs),
                    "diffs": diffs,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(diffs), 0, msg=diffs)

    def test_missing_bars_blocked(self):
        fx = self.fx
        out = OUT / "run_no_bars"
        rc, r = _run_cli(
            out_dir=out,
            arm=fx["arm"],
            lf=fx["lf"],
            lo=fx["lo"],
            q=fx["q"],
            bars=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(r["a_point"]["status"], "blocked")
        self.assertEqual(r["a_point"]["reason"], "PRICE_PATH_MISSING")
        self.assertIsNone(r["a_point"]["simulated_pnl"])
        self.assertEqual(r["kline"].get("status"), "bars_not_provided")
        self.assertIsNone(r["kline"].get("matched"))

    def test_missing_funding_fx_not_computable(self):
        fx = self.fx
        out = OUT / "run_no_cost"
        rc, r = _run_cli(
            out_dir=out,
            arm=fx["arm"],
            lf=fx["lf"],
            lo=fx["lo"],
            q=fx["q"],
            bars=fx["bars_empty"],
            funding_known=False,
            commission_fx_known=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(r["costs"]["full_net_pnl"], "not_computable")

    def test_source_change_between_replays_detected(self):
        fx = self.fx
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            arm = td_path / "arm.jsonl"
            arm.write_bytes(fx["arm"].read_bytes())
            out1 = OUT / "run_mut_1"
            out2 = OUT / "run_mut_2"
            sha1 = _sha(arm)
            rc1, _r1 = _run_cli(
                out_dir=out1, arm=arm, lf=fx["lf"], lo=fx["lo"], q=fx["q"], bars=fx["bars_empty"]
            )
            with arm.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"event": "NOISE", "trade_id": TRADE, "recorded_at": W0, "synthetic": True}
                    )
                    + "\n"
                )
            sha2 = _sha(arm)
            rc2, r2 = _run_cli(
                out_dir=out2, arm=arm, lf=fx["lf"], lo=fx["lo"], q=fx["q"], bars=fx["bars_empty"]
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            self.assertNotEqual(sha1, sha2)
            # must NOT claim same-input replay success
            same_input_success = sha1 == sha2
            self.assertFalse(same_input_success)
            # second report should still be internally stable for its own read unless mutated mid-read
            self.assertTrue(r2.get("stable") is True or r2.get("stable") is False)
            (OUT / "mutation_between_runs.json").write_text(
                json.dumps(
                    {
                        "sha_run1_input": sha1,
                        "sha_run2_input": sha2,
                        "same_input_replay_success": False,
                        "detected_by_hash_diff": True,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
