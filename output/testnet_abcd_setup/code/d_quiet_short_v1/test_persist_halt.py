#!/usr/bin/env python3
"""D-local tests: persist failure stops new queue/open; recovery loads halt marker."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from d_quiet_short_v1 import config
from d_quiet_short_v1.portfolio import (
    Account,
    clear_persist_halt_after_reconcile,
    load_account,
    queue_short_limit,
    save_account,
)
from d_quiet_short_v1.util import PersistError, atomic_write_json


class PersistHaltTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        config.set_runtime_root(self.root)
        config.restore_production_confirmations()
        self.paths = config.paths()
        self.paths["root"].mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_atomic_write_raises_persist_error(self) -> None:
        target = self.paths["root"] / "x.json"
        with mock.patch("d_quiet_short_v1.util.os.replace", side_effect=PermissionError("WinError 5")):
            with mock.patch("d_quiet_short_v1.util.time.sleep", return_value=None):
                with self.assertRaises(PersistError):
                    atomic_write_json(target, {"a": 1})

    def test_save_account_marks_halt_and_side_file(self) -> None:
        st = Account(run_id="t1", updated_at="")
        with mock.patch(
            "d_quiet_short_v1.portfolio.atomic_write_json",
            side_effect=PersistError("locked"),
        ):
            with self.assertRaises(PersistError):
                save_account(st)
        self.assertTrue(st.persist_halt_new_entries)
        self.assertTrue(st.persist_recovery_required)
        self.assertTrue(self.paths["persist_halt"].exists())
        marker = json.loads(self.paths["persist_halt"].read_text(encoding="utf-8"))
        self.assertTrue(marker["persist_halt_new_entries"])

    def test_queue_blocked_when_halted(self) -> None:
        st = Account(run_id="t2")
        st.persist_halt_new_entries = True
        r = queue_short_limit(
            st,
            signal={
                "symbol": "ADAUSDT",
                "signal_id": "s1",
                "P": 0.5,
                "fill_allowed_after_ms": 0,
            },
            filters={"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.0001},
            allow_fills=True,
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "PERSIST_HALT_NEW_ENTRIES")
        self.assertEqual(st.pending_limit_entries, {})
        self.assertEqual(st.positions, {})

    def test_load_reads_halt_marker_before_resume(self) -> None:
        # Seed a valid account on disk
        st0 = Account(run_id="t3", updated_at="x")
        save_account(st0)
        # Write halt marker as if prior crash left account without halt flags
        self.paths["persist_halt"].write_text(
            json.dumps(
                {
                    "persist_halt_new_entries": True,
                    "persist_recovery_required": True,
                    "persist_last_error": "prior",
                    "persist_failed_at": "2026-01-01T00:00:00+08:00",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("d_quiet_short_v1.portfolio._recover_settlement_journal", return_value={}):
            st = load_account()
        self.assertTrue(st.persist_halt_new_entries)
        self.assertTrue(st.persist_recovery_required)
        r = queue_short_limit(
            st,
            signal={"symbol": "XRPUSDT", "signal_id": "s2", "P": 1.0, "fill_allowed_after_ms": 0},
            filters={"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.0001},
            allow_fills=True,
        )
        self.assertEqual(r["reason"], "PERSIST_HALT_NEW_ENTRIES")

    def test_clear_halt_after_reconcile(self) -> None:
        st = Account(run_id="t4")
        save_account(st)
        st.persist_halt_new_entries = True
        st.persist_recovery_required = True
        self.paths["persist_halt"].write_text(
            json.dumps({"persist_halt_new_entries": True}), encoding="utf-8"
        )
        with mock.patch(
            "d_quiet_short_v1.portfolio._recover_settlement_journal", return_value={}
        ):
            r = clear_persist_halt_after_reconcile(st, operator_note="test", force=True)
        self.assertTrue(r["ok"])
        self.assertFalse(st.persist_halt_new_entries)
        self.assertFalse(self.paths["persist_halt"].exists())


if __name__ == "__main__":
    unittest.main()
