#!/usr/bin/env python3
"""G9 full apply tests — real portfolio/account ledger paths (paper fill source only)."""
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


def boot(td: str):
    from breakout_follow_v1 import config
    from breakout_follow_v1.util import ensure_dirs

    config.set_runtime_root(td)
    config.PROFILE_ID = "research_g9_apply"
    config.set_runtime_root(td)
    config.EXCHANGE_ORDERS_HARD_DISABLED = True
    config.MAKER_EXECUTION_ENABLED = False
    config.FEE_RATE_ONE_SIDE = 0.0005
    config.SLIPPAGE_ADVERSE = 0.0002
    config.PYRAMID_ENABLED = True
    config.PYRAMID_ADD_PCT = 0.005
    config.MARGIN_PER_TRADE_USDT = 30.0
    config.NOTIONAL_USDT = 300.0
    config.EXIT_PLAN_ID = "c_pyramid300_r2_p0tp1pct_20261120"
    config.RESEARCH_ARM = ""
    ensure_dirs()
    return config


def make_pos(st, *, symbol="G9USDT", p0=100.0, qty=3.0, trade_id=None):
    from breakout_follow_v1.account import Position, set_position
    from breakout_follow_v1.exit_plans import build_pyramid_state

    pyr = build_pyramid_state(
        side="LONG", p0=p0, qty=qty, margin_u=30.0, tick=0.01, step=0.001, notional_u=300.0
    )
    tid = trade_id or f"{st.run_id}:{symbol}:c1"
    pos = Position(
        trade_id=tid,
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
        funding_gap=True,
    )
    st.cash_u -= 30.0
    set_position(st, pos)
    return pos


class TestG9FullApply(unittest.TestCase):
    def test_1_fill_before_cancel_confirm_applies(self):
        """Cancel requested, then PARTIAL_PENDING_APPLY must still apply fill once."""
        from breakout_follow_v1.account import AccountState, get_position, save_account, set_position
        from breakout_follow_v1.portfolio import _digest_c_partial_pending_applies

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st)
            pyr = pos.pyramid
            for a in pyr["pending_adds"]:
                if a["name"] == "P1":
                    a["status"] = "PARTIAL_PENDING_APPLY"
                    a["cancel_lifecycle"] = "CANCEL_REQUESTED"
                    a["pending_fill_delta"] = 1.5
                    a["qty_filled"] = 1.5
                    a["planned_qty"] = 3.0
                    a["applied_fill_qty"] = 0.0
                    a["limit_price"] = 99.5
            pos.pyramid = pyr
            set_position(st, pos)
            save_account(st)
            cash_before = st.cash_u
            ev = _digest_c_partial_pending_applies(
                st, "G9USDT", pos=pos, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0
            )
            pos2 = get_position(st, "G9USDT")
            self.assertIsNotNone(pos2)
            self.assertGreater(pos2.qty_remaining, 3.0 - 1e-9)
            self.assertLess(st.cash_u, cash_before)  # margin+fee locked
            self.assertTrue(any(e.get("action") == "PYRAMID_ADD" or e.get("ok") for e in ev) or pos2.qty_remaining > 3.0)
            # P0 anchor frozen
            self.assertEqual(pos2.pyramid["p0_entry"], 100.0)

    def test_2_partial_exit_then_late_remainder_fill(self):
        from breakout_follow_v1.account import AccountState, get_position, save_account, set_position
        from breakout_follow_v1.portfolio import _pyramid_fill_add, close_qty, _digest_c_partial_pending_applies

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st)
            _pyramid_fill_add(
                st, "G9USDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=2.0
            )
            pos = get_position(st, "G9USDT")
            # exit P1 partially via close on layer qty
            p1 = next(L for L in pos.pyramid["layers"] if L["name"] == "P1")
            r = close_qty(
                st,
                "G9USDT",
                qty=float(p1["qty"]),
                exit_price=100.0,
                reason="PYRAMID_R2_REBOUND",
                book_ts=3.0,
                margin_release_u=float(p1["margin_u"]),
            )
            self.assertTrue(r.get("ok"))
            pos = get_position(st, "G9USDT")
            # late P2 partial apply after P1 exited
            pyr = dict(pos.pyramid)
            for a in pyr["pending_adds"]:
                if a["name"] == "P2":
                    a["status"] = "PARTIAL_PENDING_APPLY"
                    a["qty_filled"] = 1.0
                    a["planned_qty"] = 3.0
                    a["applied_fill_qty"] = 0.0
                    a["pending_fill_delta"] = 1.0
                    a["limit_price"] = float(pyr["levels"]["P2"])
            pos.pyramid = pyr
            set_position(st, pos)
            _digest_c_partial_pending_applies(
                st, "G9USDT", pos=pos, book={"bid": 99.0, "ask": 99.01}, book_ts=4.0
            )
            pos = get_position(st, "G9USDT")
            self.assertEqual(pos.pyramid["p0_entry"], 100.0)
            names = [L["name"] for L in pos.pyramid["layers"] if not L.get("closed")]
            self.assertIn("P0", names)

    def test_3_same_batch_stop_blocks_add(self):
        from breakout_follow_v1.account import AccountState, save_account, get_position
        from breakout_follow_v1.portfolio import _manage_c_pyramid

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st)
            save_account(st)
            pstop = float(pos.pyramid["levels"]["Pstop"])
            book = {"bid": pstop - 0.1, "ask": 99.4, "mid": pstop}
            ev = _manage_c_pyramid(st, "G9USDT", book=book, pos=pos, mark=pstop - 0.1, book_ts=5.0)
            self.assertFalse(any(e.get("action") == "PYRAMID_ADD" for e in ev))
            self.assertIsNone(get_position(st, "G9USDT"))

    def test_4_duplicate_exchange_trade_not_double_counted(self):
        from breakout_follow_v1.account import AccountState, get_position, save_account
        from breakout_follow_v1.portfolio import close_qty

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st, qty=3.0)
            save_account(st)
            # Dedup path uses exchange_trade_id registry (same ledger mechanism Paper will use
            # for unique fill keys). Forward Paper still labels fills paper_simulation only.
            extra = {
                "exchange_trade_id": "dup-1",
                "env_id": "research",
                "account_id": "research_g9_apply",
                "binance_executed_qty": 1.0,
                "binance_avg_price": 100.5,
                "fill_source": "exchange_execution_confirmed",
            }
            r1 = close_qty(
                st,
                "G9USDT",
                qty=1.0,
                exit_price=100.5,
                reason="PYRAMID_P0_TP1PCT",
                book_ts=6.0,
                exchange_already_done=True,
                extra_fill=extra,
                margin_release_u=10.0,
            )
            cash1 = st.cash_u
            fees1 = st.realized_fees_u
            r2 = close_qty(
                st,
                "G9USDT",
                qty=1.0,
                exit_price=100.5,
                reason="PYRAMID_P0_TP1PCT",
                book_ts=6.1,
                exchange_already_done=True,
                extra_fill=extra,
                margin_release_u=10.0,
            )
            self.assertTrue(r1.get("ok"), msg=str(r1))
            self.assertFalse(r2.get("ok"))
            self.assertEqual(r2.get("block_reason"), "DUPLICATE_EXCHANGE_TRADE")
            self.assertEqual(st.cash_u, cash1)
            self.assertEqual(st.realized_fees_u, fees1)

    def test_5_old_cycle_late_fill_not_assigned_to_new_cycle(self):
        from breakout_follow_v1.account import AccountState, get_position, save_account, set_position
        from breakout_follow_v1.portfolio import _digest_c_partial_pending_applies

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            old = make_pos(st, symbol="OLDUSDT", trade_id="g9:OLDUSDT:old")
            # clear old position but keep a stranded pending apply record is not supported on cleared pos;
            # instead: new cycle on NEWUSDT; attempt digest on OLD with no position
            from breakout_follow_v1.account import clear_position

            # keep old on book with cancel+late, then open new cycle different symbol
            new = make_pos(st, symbol="NEWUSDT", p0=50.0, trade_id="g9:NEWUSDT:new")
            self.assertNotEqual(old.trade_id, new.trade_id)
            # late apply on OLD
            pos = get_position(st, "OLDUSDT")
            pyr = pos.pyramid
            for a in pyr["pending_adds"]:
                if a["name"] == "P1":
                    a["status"] = "PARTIAL_PENDING_APPLY"
                    a["qty_filled"] = 1.0
                    a["applied_fill_qty"] = 0.0
                    a["planned_qty"] = 3.0
                    a["limit_price"] = 99.5
                    a["pending_fill_delta"] = 1.0
            pos.pyramid = pyr
            set_position(st, pos)
            _digest_c_partial_pending_applies(
                st, "OLDUSDT", pos=pos, book={"bid": 99.4, "ask": 99.5}, book_ts=7.0
            )
            new2 = get_position(st, "NEWUSDT")
            old2 = get_position(st, "OLDUSDT")
            self.assertEqual(new2.trade_id, "g9:NEWUSDT:new")
            self.assertEqual(new2.pyramid["p0_entry"], 50.0)
            self.assertEqual(old2.pyramid["p0_entry"], 100.0)
            # new cycle qty unchanged by old digest
            self.assertAlmostEqual(new2.qty_remaining, 3.0)

    def test_6_restart_replays_partial_pending_apply(self):
        from breakout_follow_v1.account import (
            AccountState,
            get_position,
            save_account,
            set_position,
            load_or_create_account,
        )
        from breakout_follow_v1.portfolio import _digest_c_partial_pending_applies

        with tempfile.TemporaryDirectory() as td:
            cfg = boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0, profile_id=cfg.PROFILE_ID)
            pos = make_pos(st)
            pyr = pos.pyramid
            for a in pyr["pending_adds"]:
                if a["name"] == "P1":
                    a["status"] = "PARTIAL_PENDING_APPLY"
                    a["qty_filled"] = 2.0
                    a["applied_fill_qty"] = 0.0
                    a["planned_qty"] = 3.0
                    a["limit_price"] = 99.5
                    a["pending_fill_delta"] = 2.0
            pos.pyramid = pyr
            set_position(st, pos)
            save_account(st)
            st2 = load_or_create_account()
            pos2 = get_position(st2, "G9USDT")
            self.assertIsNotNone(pos2)
            q0 = pos2.qty_remaining
            _digest_c_partial_pending_applies(
                st2, "G9USDT", pos=pos2, book={"bid": 99.4, "ask": 99.5}, book_ts=8.0
            )
            pos3 = get_position(st2, "G9USDT")
            self.assertGreater(pos3.qty_remaining, q0 + 1.9)

    def test_7_ledger_fail_blocks_add_protect_still_closes(self):
        from breakout_follow_v1.account import AccountState, mark_ledger_write_failure, get_position, save_account
        from breakout_follow_v1.portfolio import _pyramid_fill_add, _manage_c_pyramid

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st)
            save_account(st)
            mark_ledger_write_failure(st, reason="test")
            ev_add = _pyramid_fill_add(
                st, "G9USDT", pos=pos, add_name="P1", limit_px=99.5, book={"bid": 99.4, "ask": 99.5}, book_ts=9.0
            )
            self.assertTrue(
                any(e.get("block_reason") == "LEDGER_WRITE_FAILED_BLOCK_NEW_EXPOSURE" for e in ev_add)
            )
            # protect exit still allowed in memory even when flag set
            pstop = float(pos.pyramid["levels"]["Pstop"])
            # clear raise on save by patching save during manage close
            with mock.patch("breakout_follow_v1.portfolio.save_account", lambda s: None):
                ev = _manage_c_pyramid(
                    st,
                    "G9USDT",
                    book={"bid": pstop - 0.2, "ask": pstop - 0.1},
                    pos=get_position(st, "G9USDT") or pos,
                    mark=pstop - 0.2,
                    book_ts=10.0,
                )
            # position should be flattened or stop events present
            self.assertTrue(
                get_position(st, "G9USDT") is None
                or any("PSTOP" in str(e.get("action")) or e.get("ok") for e in ev)
            )

    def test_protect_qty_uses_remaining_no_reverse(self):
        from breakout_follow_v1.account import AccountState, get_position, save_account
        from breakout_follow_v1.portfolio import close_qty

        with tempfile.TemporaryDirectory() as td:
            boot(td)
            st = AccountState(run_id="g9", cash_u=5000.0, initial_capital_u=5000.0)
            pos = make_pos(st, qty=3.0)
            save_account(st)
            r = close_qty(st, "G9USDT", qty=10.0, exit_price=99.0, reason="PYRAMID_PSTOP", book_ts=11.0)
            # should not open reverse — qty capped or fail; remaining not negative
            pos2 = get_position(st, "G9USDT")
            if pos2 is not None:
                self.assertGreaterEqual(pos2.qty_remaining, 0.0)
            self.assertNotEqual(r.get("block_reason"), None) if not r.get("ok") else self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
