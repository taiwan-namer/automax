#!/usr/bin/env python3
"""Acceptance tests for C four-arm research gates (no exchange)."""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
sys.path[:0] = [str(CODE), str(ROOT)]


class TestPyramidLevels(unittest.TestCase):
    def test_recursive_not_linear(self):
        from breakout_follow_v1.exit_plans import pyramid_levels

        lv = pyramid_levels("LONG", 100.0, tick=0.0, add_pct=0.005)
        self.assertAlmostEqual(lv["P1"], 99.5)
        self.assertAlmostEqual(lv["P2"], 99.5 * 0.995)
        self.assertAlmostEqual(lv["Pstop"], 99.5 * 0.995 * 0.995)
        self.assertNotAlmostEqual(lv["Pstop"], 98.5)

    def test_p0_frozen_in_state(self):
        from breakout_follow_v1.exit_plans import build_pyramid_state

        st = build_pyramid_state(
            side="SHORT", p0=10.0, qty=1.0, margin_u=30.0, tick=0.0, step=0.001, notional_u=300.0
        )
        self.assertEqual(st["p0_entry"], 10.0)
        self.assertEqual(st["rebound_exit_price"], 10.0)
        self.assertTrue(st["stop_frozen"])
        self.assertAlmostEqual(st["levels"]["Pstop"], 10.0 * 1.005 * 1.005 * 1.005)


class TestStopPriorityHelper(unittest.TestCase):
    def test_stop_checked_before_add_block(self):
        from breakout_follow_v1 import portfolio as port

        src = inspect.getsource(port._manage_c_pyramid)
        i_stop = src.find("stop_hit")
        i_add = src.find("ADD_BLOCKED_STOP_PRIORITY")
        self.assertGreater(i_stop, 0)
        self.assertGreater(i_add, 0)
        self.assertLess(i_stop, i_add)

    def test_paper_close_never_claims_exchange_price(self):
        from breakout_follow_v1 import portfolio as port

        src = inspect.getsource(port.close_qty)
        self.assertIn('fill_row["is_confirmed_exchange_price"] = False', src)
        self.assertIn("paper_simulation", src)
        self.assertIn("execution_model_version", src)


class TestPairedReplayImport(unittest.TestCase):
    def test_levels_match_exit_plans(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
        from paired_four_arm_replay import levels
        from breakout_follow_v1.exit_plans import pyramid_levels

        a = levels("LONG", 100.0)
        b = pyramid_levels("LONG", 100.0, tick=0.0, add_pct=0.005)
        self.assertAlmostEqual(a["P1"], b["P1"])
        self.assertAlmostEqual(a["P2"], b["P2"])
        self.assertAlmostEqual(a["Pstop"], b["Pstop"])
        self.assertAlmostEqual(a["P0_TP"], b["P0_TP"])


class TestArchiveWriterAlive(unittest.TestCase):
    def test_is_alive_api(self):
        from common.replay_archive.queue_writer import ArchiveWriter
        from common.replay_archive.capacity import CapacityGuard
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cap = CapacityGuard(root=Path(td), usable_budget_bytes=10_000_000)
            w = ArchiveWriter(capacity=cap, handlers={"x": lambda p: None})
            self.assertFalse(w.is_alive())
            w.start()
            self.assertTrue(w.is_alive())
            w.stop()
            self.assertFalse(w.is_alive())


if __name__ == "__main__":
    unittest.main()
