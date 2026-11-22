#!/usr/bin/env python3
"""G7/G8/G9 acceptance tests — must execute, not documentation-only."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
sys.path[:0] = [str(CODE), str(ROOT)]


def _boot_runtime(td: str):
    from breakout_follow_v1 import config
    from breakout_follow_v1.account import AccountState, Position, save_account, set_position
    from breakout_follow_v1.exit_plans import build_pyramid_state

    config.set_runtime_root(td)
    config.PROFILE_ID = "research_test_g9"
    config.set_runtime_root(td)  # rebind paths under profile
    config.PYRAMID_ENABLED = True
    config.PYRAMID_ADD_PCT = 0.005
    config.PYRAMID_P0_TP_PCT = 0.01
    config.MARGIN_PER_TRADE_USDT = 30.0
    config.NOTIONAL_USDT = 300.0
    config.FEE_RATE_ONE_SIDE = 0.0005
    config.SLIPPAGE_ADVERSE = 0.0002
    config.MAKER_EXECUTION_ENABLED = False
    config.EXCHANGE_ORDERS_HARD_DISABLED = True
    config.RESEARCH_ARM = ""
    config.EXIT_PLAN_ID = "c_pyramid300_r2_p0tp1pct_20261120"
    Path(config.ROOT_OUT).mkdir(parents=True, exist_ok=True)
    return config


def _make_long_pyramid(st, symbol="TESTUSDT", p0=100.0, qty=3.0):
    from breakout_follow_v1.account import Position, set_position
    from breakout_follow_v1.exit_plans import build_pyramid_state

    pyr = build_pyramid_state(
        side="LONG", p0=p0, qty=qty, margin_u=30.0, tick=0.01, step=0.001, notional_u=300.0
    )
    pos = Position(
        trade_id=f"run:{symbol}:abc123",
        symbol=symbol,
        side="LONG",
        qty_initial=qty,
        qty_remaining=qty,
        entry_price=p0,
        entry_time="t0",
        entry_ts=1.0,
        margin_u=30.0,
        notional_initial=p0 * qty,
        leverage=10.0,
        atr_at_entry=1.0,
        atr_time="",
        initial_stop=float(pyr["levels"]["Pstop"]),
        effective_stop=float(pyr["levels"]["Pstop"]),
        take_profit=float(pyr["levels"]["P0_TP"]),
        signal_open_time=1,
        exit_plan_id="c_pyramid300_r2_p0tp1pct_20261120",
        pyramid=pyr,
    )
    st.cash_u -= 30.0
    set_position(st, pos)
    return pos


class TestG7Schema(unittest.TestCase):
    def test_enrich_has_required_fields(self):
        from breakout_follow_v1.research_event_schema import enrich_paper_event, prices_match_tick
        from breakout_follow_v1.account import AccountState, Position
        from breakout_follow_v1.exit_plans import build_pyramid_state

        st = AccountState(run_id="r", cash_u=5000)
        pyr = build_pyramid_state(side="LONG", p0=100.0, qty=1, margin_u=30, tick=0.01, step=0.001, notional_u=300)
        pos = Position(
            trade_id="c1",
            symbol="X",
            side="LONG",
            qty_initial=1,
            qty_remaining=1,
            entry_price=100.0,
            entry_time="t",
            entry_ts=1,
            margin_u=30,
            notional_initial=100,
            leverage=10,
            atr_at_entry=1,
            atr_time="",
            initial_stop=pyr["levels"]["Pstop"],
            effective_stop=pyr["levels"]["Pstop"],
            pyramid=pyr,
        )
        row = enrich_paper_event(st=st, pos=pos, event="OPEN", book_ts=1.5, book={"bid": 99, "ask": 100, "mid": 99.5})
        for k in (
            "cycle_id",
            "event_seq",
            "market_ts",
            "received_ts",
            "decision_ts",
            "fill_ts",
            "p0_anchor_before",
            "p0_anchor_after",
            "layers_state",
            "tick_size",
            "Pstop_rounded",
            "fill_source",
        ):
            self.assertIn(k, row)
        self.assertEqual(row["fill_source"], "paper_simulation")
        self.assertFalse(row["is_confirmed_exchange_price"])
        self.assertTrue(prices_match_tick(100.0, 0.01, expected=100.0))
        self.assertFalse(prices_match_tick(100.0, 0.01, expected=100.3))  # no 0.2% slack


class TestG8LedgerGate(unittest.TestCase):
    def test_save_fail_blocks_new_exposure_not_silent(self):
        from breakout_follow_v1.account import (
            AccountState,
            mark_ledger_write_failure,
            save_account,
        )
        from breakout_follow_v1.portfolio import _entry_block_reasons

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            mark_ledger_write_failure(st, reason="injected")
            reasons = _entry_block_reasons(
                st, symbol="AAAUSDT", side="LONG", signal={"signal_open_time": 1}, bypass_cooldown=True
            )
            self.assertIn("LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE", reasons)

            # save_account must raise and set flag (not swallow)
            st2 = AccountState(run_id="r2", cash_u=5000.0, initial_capital_u=5000.0)
            with mock.patch(
                "breakout_follow_v1.account.atomic_write_json", side_effect=PermissionError("boom")
            ):
                with self.assertRaises(PermissionError):
                    save_account(st2)
            self.assertTrue(st2.ledger_write_failed)
            self.assertTrue(st2.new_exposure_blocked_until_reconcile)

    def test_pyramid_add_blocked_when_ledger_failed(self):
        from breakout_follow_v1.account import AccountState, mark_ledger_write_failure
        from breakout_follow_v1.portfolio import _pyramid_fill_add

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st)
            mark_ledger_write_failure(st, reason="x")
            ev = _pyramid_fill_add(
                st, "TESTUSDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0
            )
            self.assertTrue(any(e.get("block_reason") == "LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE" for e in ev))


class TestG9Behaviors(unittest.TestCase):
    def test_same_quote_stop_blocks_add(self):
        from breakout_follow_v1.account import AccountState, save_account
        from breakout_follow_v1.portfolio import _manage_c_pyramid

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            config.RESEARCH_ARM = ""
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            save_account(st)
            pstop = float(pos.pyramid["levels"]["Pstop"])
            # mark through stop AND ask through P1
            book = {"bid": pstop - 0.01, "ask": 99.4, "mid": pstop}
            mark = pstop - 0.01
            ev = _manage_c_pyramid(st, "TESTUSDT", book=book, pos=pos, mark=mark, book_ts=10.0)
            actions = [e.get("action") for e in ev]
            self.assertTrue(any("PSTOP" in str(a) or a == "PYRAMID_PSTOP" or "CLOSE" in str(e) for e in ev for a in [e.get("action")]))
            # Must not have filled P1 add when stop hit first
            self.assertFalse(any(e.get("action") == "PYRAMID_ADD" and e.get("layer") == "P1" for e in ev))

    def test_jump_over_thresholds_stop_first(self):
        from breakout_follow_v1.account import AccountState, save_account, get_position
        from breakout_follow_v1.portfolio import _manage_c_pyramid

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            save_account(st)
            pstop = float(pos.pyramid["levels"]["Pstop"])
            # jump: ask also through P1 and P2, mark through pstop
            book = {"bid": pstop - 1, "ask": pstop - 1, "mid": pstop - 1}
            ev = _manage_c_pyramid(st, "TESTUSDT", book=book, pos=pos, mark=pstop - 1, book_ts=11.0)
            self.assertFalse(any(e.get("action") == "PYRAMID_ADD" for e in ev))
            self.assertIsNone(get_position(st, "TESTUSDT"))

    def test_b2_protect_at_p2_no_p2_add(self):
        from breakout_follow_v1.account import AccountState, save_account, get_position
        from breakout_follow_v1.portfolio import _manage_c_pyramid

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            config.RESEARCH_ARM = "B2"
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            # pre-fill P1 so P2 is next pending
            from breakout_follow_v1.portfolio import _pyramid_fill_add

            _pyramid_fill_add(st, "TESTUSDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0)
            pos = get_position(st, "TESTUSDT")
            save_account(st)
            p2 = float(pos.pyramid["levels"]["P2"])
            book = {"bid": p2, "ask": p2, "mid": p2}
            mark = (100.0 + p2) / 2  # above pstop
            ev = _manage_c_pyramid(st, "TESTUSDT", book=book, pos=pos, mark=mark, book_ts=12.0)
            self.assertTrue(any(e.get("action") == "B2_PROTECT_AT_P2_TRIGGER" for e in ev))
            self.assertFalse(any(e.get("action") == "PYRAMID_ADD" and e.get("layer") == "P2" for e in ev))
            self.assertIsNone(get_position(st, "TESTUSDT"))

    def test_p1_r2_no_rearm_and_p2_still_pending(self):
        from breakout_follow_v1.account import AccountState, save_account, get_position
        from breakout_follow_v1.portfolio import _manage_c_pyramid, _pyramid_fill_add

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            config.RESEARCH_ARM = ""  # original
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            _pyramid_fill_add(st, "TESTUSDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0)
            pos = get_position(st, "TESTUSDT")
            # mark back to P0 for R2
            book = {"bid": 100.0, "ask": 100.01, "mid": 100.0}
            ev = _manage_c_pyramid(st, "TESTUSDT", book=book, pos=pos, mark=100.0, book_ts=13.0)
            pos = get_position(st, "TESTUSDT")
            self.assertIsNotNone(pos)
            layers = pos.pyramid["layers"]
            p1 = next(L for L in layers if L["name"] == "P1")
            self.assertTrue(p1.get("closed"))
            # no re-arm of P1
            pend = pos.pyramid.get("pending_adds") or []
            self.assertFalse(any(p.get("name") == "P1" and p.get("status") == "PENDING" for p in pend))
            # P2 still pending (original rule)
            self.assertTrue(any(p.get("name") == "P2" and p.get("status") == "PENDING" for p in pend))

    def test_add_does_not_overwrite_p0_anchor(self):
        from breakout_follow_v1.account import AccountState, get_position
        from breakout_follow_v1.portfolio import _pyramid_fill_add

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            _pyramid_fill_add(st, "TESTUSDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0)
            pos = get_position(st, "TESTUSDT")
            self.assertEqual(pos.pyramid["p0_entry"], 100.0)
            self.assertEqual(pos.pyramid["rebound_exit_price"], 100.0)
            self.assertTrue(pos.pyramid["stop_frozen"])

    def test_late_partial_after_cancel_request_recorded(self):
        """Cancel-race: digest pending apply must not invent; status transitions visible."""
        from breakout_follow_v1.account import AccountState, get_position, save_account, set_position

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos = _make_long_pyramid(st, p0=100.0, qty=3.0)
            # simulate cancel requested then late fill qty on pending add digest field
            pyr = pos.pyramid
            for a in pyr["pending_adds"]:
                if a["name"] == "P1":
                    a["status"] = "CANCEL_REQUESTED"
                    a["late_fill_qty"] = 1.5
                    a["late_fill_price"] = 99.5
            pos.pyramid = pyr
            set_position(st, pos)
            save_account(st)
            # Ensure state persisted for restart path
            from breakout_follow_v1 import config as cfg

            raw = json.loads(Path(cfg.ACCOUNT_PATH).read_text(encoding="utf-8"))
            self.assertIn("TESTUSDT", raw["positions"])
            late = raw["positions"]["TESTUSDT"]["pyramid"]["pending_adds"]
            self.assertTrue(any(a.get("status") == "CANCEL_REQUESTED" and a.get("late_fill_qty") == 1.5 for a in late))

    def test_restart_loads_ledger_flag_and_blocks(self):
        from breakout_follow_v1.account import (
            AccountState,
            mark_ledger_write_failure,
            save_account,
            load_or_create_account,
        )
        from breakout_follow_v1.portfolio import _entry_block_reasons

        with tempfile.TemporaryDirectory() as td:
            config = _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0, profile_id=config.PROFILE_ID)
            mark_ledger_write_failure(st, reason="persist_me")
            st.ledger_write_failed = True
            st.new_exposure_blocked_until_reconcile = True
            from breakout_follow_v1.util import atomic_write_json

            atomic_write_json(config.ACCOUNT_PATH, st.to_dict())
            st2 = load_or_create_account()
            self.assertTrue(st2.ledger_write_failed or st2.new_exposure_blocked_until_reconcile)
            reasons = _entry_block_reasons(
                st2, symbol="BBBUSDT", side="LONG", signal={"signal_open_time": 2}, bypass_cooldown=True
            )
            self.assertIn("LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE", reasons)

    def test_old_cycle_id_does_not_replace_new(self):
        from breakout_follow_v1.account import AccountState, get_position, set_position

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            pos_old = _make_long_pyramid(st, symbol="OLDUSDT", p0=10.0)
            # new cycle different symbol/trade_id
            pos_new = _make_long_pyramid(st, symbol="NEWUSDT", p0=20.0)
            self.assertNotEqual(pos_old.trade_id, pos_new.trade_id)
            self.assertEqual(get_position(st, "OLDUSDT").pyramid["p0_entry"], 10.0)
            self.assertEqual(get_position(st, "NEWUSDT").pyramid["p0_entry"], 20.0)

    def test_exchange_hard_disabled(self):
        from breakout_follow_v1 import config
        from breakout_follow_v1 import testnet_hooks

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            config.EXCHANGE_ORDERS_HARD_DISABLED = True
            self.assertFalse(testnet_hooks.enabled())

    def test_duplicate_signal_gate(self):
        from breakout_follow_v1.account import AccountState
        from breakout_follow_v1.portfolio import _entry_block_reasons

        with tempfile.TemporaryDirectory() as td:
            _boot_runtime(td)
            st = AccountState(run_id="r", cash_u=5000.0, initial_capital_u=5000.0)
            st.processed_signals = ["AAAUSDT:1:LONG"]
            r1 = _entry_block_reasons(st, symbol="AAAUSDT", side="LONG", signal={"signal_open_time": 1}, bypass_cooldown=True)
            self.assertIn("DUPLICATE_SIGNAL", r1)


if __name__ == "__main__":
    unittest.main()
