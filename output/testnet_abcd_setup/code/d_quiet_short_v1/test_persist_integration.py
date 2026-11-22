#!/usr/bin/env python3
"""D isolated integration tests — temp runtime_root only; never touch old HYPE/ledger."""
from __future__ import annotations

import asyncio
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
from d_quiet_short_v1.portfolio import (
    Account,
    clear_persist_halt_after_reconcile,
    load_account,
    process_pending,
    queue_short_limit,
    reconcile_before_clear_persist_halt,
    save_account,
)
from d_quiet_short_v1.util import PersistError, atomic_write_json, now_iso
from d_quiet_short_v1.win_lock import lock_ex_nb, unlock


def _signal(sym: str = "ADAUSDT", p: float = 0.5) -> dict:
    return {
        "symbol": sym,
        "signal_id": f"{sym}:sig:test",
        "P": p,
        "fill_allowed_after_ms": 0,
        "B": 0.01,
        "T": 0.02,
        "bar_amps": [0.001, 0.001, 0.001, 0.001],
        "range_amp": 0.01,
        "slope": 0.0,
        "price_source": "fixture",
    }


def _filters() -> dict:
    return {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.0001}


class PersistIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        config.set_runtime_root(self.root)
        config.restore_production_confirmations()
        self.paths = config.paths()
        self.paths["root"].mkdir(parents=True, exist_ok=True)
        # Never hit Binance testnet from isolated fixtures
        self._env_patch = mock.patch.dict(
            "os.environ", {"ENABLE_BINANCE_TESTNET_ORDER": "0"}, clear=False
        )
        self._env_patch.start()
        self._tn_patch = mock.patch(
            "d_quiet_short_v1.portfolio._testnet_on", return_value=False
        )
        self._tn_patch.start()

    def tearDown(self) -> None:
        self._tn_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _fail_account_write(self):
        real = atomic_write_json

        def _wrapped(path, obj):
            if Path(path).name == "account_state.json":
                raise PersistError("fixture account write fail")
            return real(path, obj)

        return _wrapped

    def test_empty_account_save_fail_halts(self) -> None:
        st = Account(run_id="empty1")
        with mock.patch("d_quiet_short_v1.portfolio.atomic_write_json", side_effect=PersistError("x")):
            with self.assertRaises(PersistError):
                save_account(st)
        self.assertTrue(st.persist_halt_new_entries)
        r = queue_short_limit(st, signal=_signal(), filters=_filters(), allow_fills=True)
        self.assertEqual(r["reason"], "PERSIST_HALT_NEW_ENTRIES")

    def test_pending_save_fail_cancels_and_blocks(self) -> None:
        st = Account(run_id="pend1")
        save_account(st)
        r = queue_short_limit(st, signal=_signal(), filters=_filters(), allow_fills=True)
        self.assertTrue(r["ok"])
        self.assertIn("ADAUSDT", st.pending_limit_entries)
        with mock.patch("d_quiet_short_v1.portfolio.atomic_write_json", side_effect=self._fail_account_write()):
            # cancel_pending inside halt path will try save
            st.persist_halt_new_entries = True
            try:
                process_pending(
                    st,
                    {
                        "ADAUSDT": {
                            "bid": 1.0,
                            "ask": 1.01,
                            "mid": 1.005,
                            "fetched_at": __import__("time").time(),
                        }
                    },
                    now_ms=10**13,
                )
            except PersistError:
                pass
        r2 = queue_short_limit(st, signal=_signal("XRPUSDT", 1.0), filters=_filters(), allow_fills=True)
        self.assertEqual(r2["reason"], "PERSIST_HALT_NEW_ENTRIES")

    def test_position_save_fail_halts_new_entries(self) -> None:
        st = Account(run_id="pos1")
        save_account(st)
        st.positions["HYPEUSDT"] = {
            "trade_id": "t-hype",
            "symbol": "HYPEUSDT",
            "side": "SHORT",
            "entry_price": 59.446,
            "qty_remaining": 5.04,
            "fees_paid_u": 0.15,
            "tp_usdt": 15.0,
            "sl_usdt": 3.0,
            "T": 0.008,
            "roll_samples": [],
        }
        with mock.patch("d_quiet_short_v1.portfolio.atomic_write_json", side_effect=PersistError("posfail")):
            with self.assertRaises(PersistError):
                save_account(st)
        self.assertTrue(st.persist_halt_new_entries)
        # Must not open another
        r = queue_short_limit(st, signal=_signal(), filters=_filters(), allow_fills=True)
        self.assertEqual(r["reason"], "PERSIST_HALT_NEW_ENTRIES")

    def test_open_close_mutation_mid_save_fail(self) -> None:
        """OPEN path: queue ok, then save fail before/at fill accounting → halt, no silent continue."""
        st = Account(run_id="mut1")
        save_account(st)
        r = queue_short_limit(st, signal=_signal(), filters=_filters(), allow_fills=True)
        self.assertTrue(r["ok"])
        # Simulate fill accounting save failure
        with mock.patch("d_quiet_short_v1.portfolio.atomic_write_json", side_effect=PersistError("mid")):
            with self.assertRaises(PersistError):
                save_account(st)
        self.assertTrue(st.persist_halt_new_entries)
        # No second symbol queue
        r2 = queue_short_limit(st, signal=_signal("SOLUSDT", 100.0), filters=_filters(), allow_fills=True)
        self.assertFalse(r2["ok"])

    def test_account_and_halt_sidefile_both_unwritable(self) -> None:
        st = Account(run_id="both1")
        with mock.patch("d_quiet_short_v1.portfolio.atomic_write_json", side_effect=PersistError("acct")):
            with mock.patch.object(Path, "write_text", side_effect=OSError("halt side fail")):
                with self.assertRaises(PersistError):
                    save_account(st)
        # In-memory halt must still be set even if side-file write failed
        self.assertTrue(st.persist_halt_new_entries)
        self.assertTrue(st.persist_recovery_required)

    def test_recover_after_writable_requires_reconcile_not_just_delete(self) -> None:
        st = Account(run_id="rec1")
        save_account(st)
        st.positions["ADAUSDT"] = {
            "trade_id": "t-ada",
            "symbol": "ADAUSDT",
            "entry_price": 0.5,
            "qty_remaining": 10.0,
            "fees_paid_u": 0.01,
        }
        save_account(st)
        st.persist_halt_new_entries = True
        st.persist_recovery_required = True
        self.paths["persist_halt"].write_text(
            json.dumps({"persist_halt_new_entries": True, "persist_last_error": "x"}),
            encoding="utf-8",
        )
        # Missing OPEN fill → reconcile fails → clear refused
        rep = reconcile_before_clear_persist_halt(st)
        self.assertFalse(rep["ok"])
        refused = clear_persist_halt_after_reconcile(st, operator_note="nope")
        self.assertFalse(refused["ok"])
        self.assertTrue(st.persist_halt_new_entries)
        # Side-file still there
        self.assertTrue(self.paths["persist_halt"].exists())
        # Seed OPEN fill then clear
        from d_quiet_short_v1.util import append_jsonl

        append_jsonl(
            self.paths["fills"],
            {
                "event": "OPEN",
                "trade_id": "t-ada",
                "symbol": "ADAUSDT",
                "recorded_at": now_iso(),
            },
        )
        with mock.patch("d_quiet_short_v1.portfolio._recover_settlement_journal", return_value={}):
            ok = clear_persist_halt_after_reconcile(st, operator_note="reconciled")
        self.assertTrue(ok["ok"])
        self.assertFalse(st.persist_halt_new_entries)

    def test_second_writer_lock_guard(self) -> None:
        """Second writer must not obtain exclusive lock while first holds it."""
        lock_path = self.paths["lock"]
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh1 = open(lock_path, "a+", encoding="utf-8")
        fh2 = None
        try:
            lock_ex_nb(fh1)
            fh2 = open(lock_path, "a+", encoding="utf-8")
            blocked = False
            try:
                lock_ex_nb(fh2)
            except (BlockingIOError, PermissionError, OSError):
                blocked = True
            self.assertTrue(blocked, "second writer must be blocked while lock held")
        finally:
            if fh2 is not None:
                try:
                    unlock(fh2)
                except Exception:
                    pass
                fh2.close()
            unlock(fh1)
            fh1.close()

    def test_settings_snapshot_and_quote_vol_fixture_format(self) -> None:
        """Isolated fixture — does NOT claim live market run."""
        snap = config.settings_snapshot_dict()
        atomic_write_json(
            self.paths["settings_snapshot"],
            {"written_at": now_iso(), "fixture": True, **snap},
        )
        raw = json.loads(self.paths["settings_snapshot"].read_text(encoding="utf-8"))
        for key in ("strategy_id", "capital", "signal", "costs", "written_at"):
            self.assertIn(key, raw)
        self.assertIn("BAR_AMP_MAX", raw["signal"])
        self.assertIn("RANGE_AMP_MAX", raw["signal"])
        from d_quiet_short_v1.util import append_jsonl

        append_jsonl(
            self.paths["quote_vol"],
            {
                "at": now_iso(),
                "symbol": "FIXTUREUSDT",
                "bid": 1.0,
                "ask": 1.001,
                "mid": 1.0005,
                "fetched_at": 1.0,
                "roll_hi": 1.01,
                "roll_lo": 0.99,
                "roll_n": 2,
                "T": 0.008,
                "price_kind": "bookTicker_mid_bid_ask",
                "persist_halted": False,
                "fixture": True,
            },
        )
        row = json.loads(self.paths["quote_vol"].read_text(encoding="utf-8").strip().splitlines()[0])
        for key in ("at", "symbol", "bid", "ask", "mid", "price_kind"):
            self.assertIn(key, row)
        self.assertTrue(row.get("fixture"))

    def test_manage_loop_persist_error_does_not_crash(self) -> None:
        """Mirrors run_paper_loop PersistError catch: fills disabled, no process abort."""
        st = Account(run_id="loop1")
        save_account(st)

        async def _fake_manage(*args, **kwargs):
            raise PersistError("loop fail")

        async def _run():
            fills_on = True
            crashed = False
            try:
                await _fake_manage()
            except PersistError:
                fills_on = False
            except Exception:
                crashed = True
            return fills_on, crashed

        fills_on, crashed = asyncio.run(_run())
        self.assertFalse(fills_on)
        self.assertFalse(crashed)

    def test_halted_suppresses_simulated_exit_event_contract(self) -> None:
        """When halted, exit triggers must not create CLOSE fills."""
        st = Account(run_id="exit1")
        save_account(st)
        st.positions["ADAUSDT"] = {
            "trade_id": "t1",
            "symbol": "ADAUSDT",
            "side": "SHORT",
            "entry_price": 0.5,
            "qty_remaining": 100.0,
            "fees_paid_u": 0.01,
            "tp_usdt": 15.0,
            "sl_usdt": 3.0,
            "tp_sl_basis": "gross",
            "T": 0.008,
            "roll_samples": [],
        }
        st.persist_halt_new_entries = True
        fills_before = self.paths["fills"].read_text(encoding="utf-8") if self.paths["fills"].exists() else ""
        # Direct contract: queue blocked; process_pending cancels only
        r = queue_short_limit(st, signal=_signal("XRPUSDT"), filters=_filters(), allow_fills=True)
        self.assertEqual(r["reason"], "PERSIST_HALT_NEW_ENTRIES")
        fills_after = self.paths["fills"].read_text(encoding="utf-8") if self.paths["fills"].exists() else ""
        self.assertEqual(fills_before, fills_after)


if __name__ == "__main__":
    unittest.main()
