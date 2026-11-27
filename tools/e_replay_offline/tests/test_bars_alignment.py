"""wp-e-functional-bars-01: window bar alignment + gap alerts (synthetic only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.e_replay_offline.reader import ReportInputs, align_bars_window, build_report

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_bars_01"
FX = Path(__file__).resolve().parent / "fixtures"
TRADE = ":SYNTHUSDT:bars01"
W0 = "2026-11-27T07:29:00Z"
W1 = "2026-11-27T07:51:00Z"
A0 = "2026-11-27T07:50:00Z"
A1 = "2026-11-27T07:51:00Z"
START_MS = 1795764540000
END_EXCL_MS = 1795765860000
SYMBOL = "SYNTHUSDT"


def _expected_times() -> list[int]:
    t = START_MS
    out = []
    while t < END_EXCL_MS:
        out.append(t)
        t += 60_000
    return out


def _bars(times: list[int], *, symbol: str = SYMBOL, forming_close: int | None = None) -> list[dict]:
    rows = []
    for ot in times:
        row = {
            "symbol": symbol,
            "interval": "1m",
            "open_time": ot,
            "close_time": ot + 59_999,
            "open": "1",
            "high": "1",
            "low": "1",
            "close": "1",
            "synthetic": True,
        }
        if forming_close is not None and ot == times[-1]:
            row["close_time"] = forming_close
        rows.append(row)
    return rows


def _write(td: Path, name: str, bars: list[dict]) -> Path:
    p = td / name
    p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    return p


def _inputs(bars_path: Path | None, **kw) -> ReportInputs:
    d = dict(
        arm_fills=FX / "arm_fills_synth.jsonl",
        ledger_fills=FX / "ledger_fills_synth.jsonl",
        ledger_orders=FX / "ledger_orders_synth.jsonl",
        bars_path=bars_path,
        trade_id=TRADE,
        window_start_utc=W0,
        window_end_utc=W1,
        a_bar_open_utc=A0,
        a_bar_close_utc=A1,
        bars_symbol=SYMBOL,
        bars_interval="1m",
        bars_half_open_end=True,
        bars_now_ms=END_EXCL_MS,
    )
    d.update(kw)
    return ReportInputs(**d)


class TestBarsAlignment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.expected = _expected_times()
        assert len(cls.expected) == 22

    def test_cases_matrix(self):
        with tempfile.TemporaryDirectory(dir=str(OUT)) as td:
            td_path = Path(td)
            full = _write(td_path, "full.json", _bars(self.expected))
            miss_one = _write(td_path, "miss1.json", _bars(self.expected[:-1]))
            wrong_sym = _write(td_path, "wrong_sym.json", _bars(self.expected, symbol="OTHERUSDT"))
            dup = _write(td_path, "dup.json", _bars(self.expected[:] + [self.expected[5]]))
            outside = _write(
                td_path,
                "outside.json",
                _bars(self.expected + [START_MS - 60_000, END_EXCL_MS + 60_000]),
            )
            forming = _write(
                td_path,
                "forming.json",
                _bars(self.expected, forming_close=END_EXCL_MS + 1),
            )

            cases = [
                ("full_22", full, {"matched": 22, "missing_n": 0, "blocked": False}),
                ("miss_1", miss_one, {"matched": 21, "missing_n": 1, "blocked": True}),
                ("wrong_symbol", wrong_sym, {"matched": 0, "wrong_symbol_n": 22, "blocked": True}),
                ("duplicate_key", dup, {"dup": True, "blocked": True, "matched": 22}),
                ("outside_extra", outside, {"outside_window_n": 2, "matched": 22, "blocked": False}),
                ("bars_not_provided", None, {"matched": None, "status": "bars_not_provided", "blocked": True}),
                ("forming_last", forming, {"forming_n": 1, "matched": 21, "blocked": True}),
            ]

            rows = []
            for name, path, exp in cases:
                if name == "bars_not_provided":
                    r = build_report(_inputs(None))
                elif name == "forming_last":
                    r = build_report(_inputs(path, bars_now_ms=END_EXCL_MS))
                else:
                    r = build_report(_inputs(path))
                k = r["kline"]
                self.assertIsNone(r["a_point"]["simulated_pnl"])
                self.assertEqual(k.get("expected"), 22, msg=name)
                if "matched" in exp:
                    self.assertEqual(k.get("matched"), exp["matched"], msg=name)
                if "missing_n" in exp:
                    self.assertEqual(len(k.get("missing") or []), exp["missing_n"], msg=name)
                if "wrong_symbol_n" in exp:
                    self.assertEqual(k.get("wrong_symbol_n"), exp["wrong_symbol_n"], msg=name)
                if "outside_window_n" in exp:
                    self.assertEqual(k.get("outside_window_n"), exp["outside_window_n"], msg=name)
                if "forming_n" in exp:
                    self.assertEqual(k.get("forming_n"), exp["forming_n"], msg=name)
                if exp.get("dup"):
                    self.assertGreater(len(k.get("duplicate_keys") or []), 0, msg=name)
                if "status" in exp:
                    self.assertEqual(k.get("status"), exp["status"], msg=name)
                self.assertEqual(bool(k.get("blocked")), bool(exp["blocked"]), msg=name)
                if name == "full_22":
                    self.assertEqual(r["a_point"]["status"], "bars_present_not_simulated")
                    self.assertTrue(k.get("available_for_later_simulation"))
                rows.append(
                    {
                        "case": name,
                        "expected": exp,
                        "actual": {
                            "expected": k.get("expected"),
                            "matched": k.get("matched"),
                            "missing": k.get("missing"),
                            "duplicate_keys": k.get("duplicate_keys"),
                            "outside_window_n": k.get("outside_window_n"),
                            "forming_n": k.get("forming_n"),
                            "wrong_symbol_n": k.get("wrong_symbol_n"),
                            "status": k.get("status"),
                            "blocked": k.get("blocked"),
                            "blocked_reason": k.get("blocked_reason"),
                            "source_identity": k.get("source_identity"),
                            "simulated_pnl": None,
                        },
                    }
                )

            (OUT / "evidence_cases.json").write_text(
                json.dumps(
                    {
                        "window": {"start": W0, "end": W1, "half_open_end": True, "expected_n": 22},
                        "align_keys": ["symbol", "interval", "open_time"],
                        "cases": rows,
                        "network_calls": 0,
                        "live_writes": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    def test_align_helper_direct(self):
        a = align_bars_window(
            _bars(_expected_times()),
            window_start_utc=W0,
            window_end_utc=W1,
            symbol=SYMBOL,
            half_open_end=True,
            bars_provided=True,
        )
        self.assertEqual(a["expected"], 22)
        self.assertEqual(a["matched"], 22)
        self.assertEqual(a["missing"], [])
        self.assertFalse(a["blocked"])


if __name__ == "__main__":
    unittest.main()
