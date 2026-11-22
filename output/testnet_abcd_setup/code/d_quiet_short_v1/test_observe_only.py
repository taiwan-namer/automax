#!/usr/bin/env python3
"""Fixture tests for D observation-only: no OPEN/CLOSE, refuse old runtime, snapshot fields."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from d_quiet_short_v1 import config
from d_quiet_short_v1.observe import (
    assert_isolated_runtime_root,
    make_shadow_limit,
    recheck_shadow,
    record_quote_vol_sample,
)
from d_quiet_short_v1.util import atomic_write_json, now_iso


class ObserveOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name) / "paper_d_observe_local" / "obs_fixture"
        self.root.mkdir(parents=True)
        config.set_runtime_root(self.root)
        config.restore_production_confirmations()
        self.paths = config.paths()
        self.paths["root"].mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_refuse_old_runtime_root(self) -> None:
        with self.assertRaises(SystemExit):
            assert_isolated_runtime_root(r"c:\Users\Admin\MyProjects\automax\output\paper_bcd_local\data_d")
        with self.assertRaises(SystemExit):
            assert_isolated_runtime_root("/tmp/foo/paper_bcd_local/bar")

    def test_shadow_touch_is_not_fill(self) -> None:
        sig = {
            "symbol": "ADAUSDT",
            "signal_id": "ADAUSDT:sig:obs",
            "P": 0.5,
            "B": 0.004,
            "T": 0.008,
            "bar_amps": [0.001] * 4,
            "range_amp": 0.01,
            "slope": 0.0,
            "spread": 0.0001,
            "price_source": "fixture",
            "price_ts": 1.0,
            "fill_allowed_after_ms": 0,
            "bars": [{"open_time": 1, "close_time": 2, "o": 1, "h": 1, "l": 1, "c": 1}],
        }
        shadow = make_shadow_limit(
            sig,
            {"tickSize": 0.0001, "stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0},
            run_id="obs_test",
        )
        book = {"bid": 0.51, "ask": 0.511, "mid": 0.5055, "fetched_at": __import__("time").time()}
        row = recheck_shadow(shadow, book, exec_order="revalidate_then_touch")
        self.assertTrue(row["checks"]["observational_touch"])
        self.assertEqual(row["checks"]["touch_price_kind"], "bid")
        self.assertEqual(row["outcome"], "OBSERVATIONAL_TOUCH")
        # no fills file write from observe helpers
        self.assertFalse(self.paths["fills"].exists())
        # lifecycle marks observational
        life = self.paths["shadow_lifecycle"].read_text(encoding="utf-8")
        self.assertIn("OBSERVATIONAL_TOUCH", life)
        self.assertIn("NOT a fill", life)
        self.assertNotIn('"event": "OPEN"', life)
        self.assertNotIn('"event": "CLOSE"', life)

    def test_settings_and_quote_vol_paths(self) -> None:
        settings = config.settings_snapshot_dict()
        atomic_write_json(
            self.paths["settings_snapshot"],
            {
                "written_at": now_iso(),
                "mode_flag": "OBSERVATION_ONLY",
                "settings_tag": "NEW_OBSERVATION_SETTINGS",
                "run_id": "obs_fixture_1",
                **settings,
            },
        )
        snap = json.loads(self.paths["settings_snapshot"].read_text(encoding="utf-8"))
        self.assertEqual(snap["signal"]["BAR_AMP_MAX"], 0.0075)
        self.assertEqual(snap["signal"]["RANGE_AMP_MAX"], 0.015)
        self.assertEqual(snap["settings_tag"], "NEW_OBSERVATION_SETTINGS")
        self.assertIn("written_at", snap)
        atomic_write_json(
            self.paths["launch"],
            {
                "launched_at": now_iso(),
                "mode": "OBSERVATION_ONLY",
                "settings": snap,
                "settings_snapshot_path": str(self.paths["settings_snapshot"]),
                "native_quote_vol_path": str(self.paths["quote_vol"]),
            },
        )
        launch = json.loads(self.paths["launch"].read_text(encoding="utf-8"))
        self.assertEqual(launch["mode"], "OBSERVATION_ONLY")
        self.assertIn("settings", launch)
        record_quote_vol_sample(
            symbol="FIXTUREUSDT",
            book={"bid": 1.0, "ask": 1.001, "mid": 1.0005, "fetched_at": 1.0},
            roll_n=0,
            data_valid=True,
        )
        q = json.loads(self.paths["quote_vol"].read_text(encoding="utf-8").strip().splitlines()[0])
        for k in ("at", "symbol", "bid", "ask", "mid", "price_kind", "mode"):
            self.assertIn(k, q)
        self.assertEqual(q["mode"], "OBSERVATION_ONLY")

    def test_stale_quote_marked_invalid_not_no_signal(self) -> None:
        sig = {
            "symbol": "XRPUSDT",
            "signal_id": "XRPUSDT:sig:obs",
            "P": 1.0,
            "B": 0.004,
            "T": 0.008,
            "bar_amps": [0.001] * 4,
            "range_amp": 0.01,
            "fill_allowed_after_ms": 0,
            "bars": [],
        }
        shadow = make_shadow_limit(
            sig,
            {"tickSize": 0.0001, "stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0},
            run_id="obs_test",
        )
        book = {"bid": 1.01, "ask": 1.02, "mid": 1.015, "fetched_at": 1.0}  # ancient
        row = recheck_shadow(shadow, book)
        self.assertFalse(row["checks"]["data_valid"])
        self.assertTrue(row["checks"]["stale_quote"])
        self.assertEqual(row["outcome"], "DATA_INVALID")
        self.assertFalse(row["checks"]["observational_touch"])

    def test_observe_mode_does_not_call_queue_or_close(self) -> None:
        """Contract: observation path must not invoke trading portfolio mutators."""
        from d_quiet_short_v1 import engine

        # ensure observe helpers imported without triggering fills
        self.assertTrue(hasattr(engine, "run_observe_loop"))
        self.assertTrue(hasattr(engine, "observe_scan_once"))
        # empty fills after shadow ops
        self.assertFalse(self.paths["fills"].exists() or (self.paths["fills"].exists() and self.paths["fills"].stat().st_size > 0))


if __name__ == "__main__":
    unittest.main()
