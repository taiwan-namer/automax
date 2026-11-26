"""Offline acceptance tests for H surge-trend v1. No live orders / no A/E mutation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
sys.path.insert(0, str(CODE))

from h_surge_trend_v1 import config  # noqa: E402
from h_surge_trend_v1.bars import BarBuilder3m, SecondPriceTracker, TradePrint  # noqa: E402
from h_surge_trend_v1.metrics import (  # noqa: E402
    clv,
    efficiency_ratio,
    gross_pnl_pct,
    surge_ok,
    trail_stop,
    upper_wick_frac,
)
from h_surge_trend_v1.portfolio import (  # noqa: E402
    Account,
    apply_entry_fill,
    begin_exit,
    cancel_pending_entry,
    on_drop_from_top15,
    process_pending_entries,
    process_pending_exits,
    queue_entry,
    update_trail,
)
from h_surge_trend_v1.signals import evaluate_entry  # noqa: E402
from h_surge_trend_v1.universe import is_excluded, rank_by_24h_change, update_tracking  # noqa: E402


def _crypto_info(base: str = "AAA") -> dict:
    return {
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "baseAsset": base,
        "underlyingType": "COIN",
        "underlyingSubType": ["Layer-1"],
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


class TestUniverse(unittest.TestCase):
    def test_exclude_fixed_bases_exact(self):
        bad, reason = is_excluded("BTCUSDT", "BTC", _crypto_info("BTC"))
        self.assertTrue(bad)
        self.assertEqual(reason, "EXCLUDED_BASE_ASSET")
        # substring trap: symbol containing BTC text but base is other — base wins
        ok, _ = is_excluded("1000BONKUSDT", "1000BONK", _crypto_info("1000BONK"))
        self.assertFalse(ok)

    def test_exclude_wrapped_and_unknown(self):
        bad, reason = is_excluded("WBTCUSDT", "WBTC", _crypto_info("WBTC"))
        self.assertTrue(bad)
        self.assertEqual(reason, "WRAPPED_OR_STAKED_BASE")
        info = _crypto_info("ZZZ")
        info["underlyingType"] = ""
        bad2, reason2 = is_excluded("ZZZUSDT", "ZZZ", info)
        self.assertTrue(bad2)
        self.assertIn("CLASSIFICATION_UNKNOWN", reason2)

    def test_rank_tiebreak_and_no_future(self):
        rows = [
            {"symbol": "BUSDT", "price_change_pct_24h": 5.0},
            {"symbol": "AUSDT", "price_change_pct_24h": 5.0},
            {"symbol": "CUSDT", "price_change_pct_24h": 9.0},
        ]
        ranked = rank_by_24h_change(rows)
        self.assertEqual([r["symbol"] for r in ranked], ["CUSDT", "AUSDT", "BUSDT"])

    def test_tracking_top5_keep15_drop(self):
        ranked = [
            {"symbol": f"S{i}USDT", "price_change_pct_24h": 20 - i, "rank": i}
            for i in range(1, 21)
        ]
        # force ranks
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        upd = update_tracking({}, ranked, now_ts=1000.0, top_enter=5, top_keep=15)
        self.assertEqual(len(upd["tracked"]), 5)
        self.assertTrue(all(upd["tracked"][s]["signal_eligible"] for s in upd["entered"]))
        # promote keep: put previous top into ranks 6-15
        prev = upd["tracked"]
        ranked2 = []
        # S1 moves to rank 16 → drop; S2 stays rank 10 → keep but not signal
        order = ["S3USDT", "S4USDT", "S5USDT", "N1USDT", "N2USDT"] + [
            "S2USDT",
            "X6USDT",
            "X7USDT",
            "X8USDT",
            "X9USDT",
            "X10USDT",
            "X11USDT",
            "X12USDT",
            "X13USDT",
            "X14USDT",
            "S1USDT",
        ]
        for i, sym in enumerate(order, 1):
            ranked2.append({"symbol": sym, "price_change_pct_24h": 100 - i, "rank": i})
        upd2 = update_tracking(prev, ranked2, now_ts=2000.0, top_enter=5, top_keep=15)
        self.assertIn("S1USDT", upd2["dropped"])
        self.assertIn("S2USDT", upd2["tracked"])
        self.assertFalse(upd2["tracked"]["S2USDT"]["signal_eligible"])
        self.assertTrue(upd2["tracked"]["S3USDT"]["signal_eligible"])

    def test_tracking_top10_enter(self):
        ranked = [
            {"symbol": f"S{i}USDT", "price_change_pct_24h": 30 - i, "rank": i}
            for i in range(1, 21)
        ]
        upd = update_tracking({}, ranked, now_ts=1000.0, top_enter=10, top_keep=15)
        self.assertEqual(len(upd["tracked"]), 10)
        self.assertEqual(len(upd["top5"]), 10)  # key name historical; means enter-band
        self.assertTrue(all(upd["tracked"][s]["signal_eligible"] for s in upd["entered"]))


class TestMetricsAndBars(unittest.TestCase):
    def test_rvol_and_forbid_zero_baseline(self):
        ok, d = surge_ok(100, 0, 60, min_elapsed=30, rvol_threshold=3, abs_frac=0.5)
        self.assertFalse(ok)
        # baseline 100, elapsed 60 → expected 100*60/180=33.33; need cum>=150 for rvol3 and abs>=50
        ok2, d2 = surge_ok(200, 100, 60, min_elapsed=30, rvol_threshold=3, abs_frac=0.5)
        self.assertTrue(ok2)
        self.assertGreaterEqual(d2["rvol"], 3.0)

    def test_er_and_clv(self):
        # strong trend
        prices = [100 + i * 0.1 for i in range(60)]
        self.assertGreaterEqual(efficiency_ratio(prices), 0.35)
        self.assertAlmostEqual(clv(109, 110, 100) or -1, 0.9, places=6)
        self.assertIsNone(clv(100, 100, 100))

    def test_bar_builder_no_future_backfill(self):
        b = BarBuilder3m(180)
        t0 = 1_700_000_000.0
        start = int(t0 // 180) * 180
        for i in range(5):
            b.on_trade(TradePrint(start + 10 + i, 100 + i, 1, 100 + i))
        known_early = b.known_current(start + 12)
        self.assertIsNotNone(known_early)
        # later trades increase quote — early snapshot object is a copy
        b.on_trade(TradePrint(start + 50, 120, 10, 1200))
        self.assertLess(known_early.quote_volume, b.current.quote_volume)

    def test_upper_wick(self):
        # O=100, H=110, L=99, P=101 → wick=110-max(100,101)=9; range=11 → ~0.818
        self.assertGreaterEqual(upper_wick_frac(100, 110, 99, 101) or 0, 0.40)


class TestSignals(unittest.TestCase):
    def _prices(self, start: float, end: float, n: int = 60) -> list[float]:
        return [start + (end - start) * i / (n - 1) for i in range(n)]

    def _bar(self, start_ts: float, o, h, l, c, qv):
        from h_surge_trend_v1.bars import Bar3m

        return Bar3m(start_ts=start_ts, end_ts=start_ts + 180, open=o, high=h, low=l, close=c, quote_volume=qv, trade_count=10)

    def test_trend_long(self):
        start = 1000.0
        bar = self._bar(start, 100, 103, 99.5, 102.8, 500)
        prices = self._prices(100, 102.5)
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices,
            prices_15=prices[-15:],
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=False,
        )
        self.assertTrue(d.ok)
        self.assertEqual(d.side, "LONG")
        self.assertIn("TREND_LONG", d.tags)

    def test_trend_short(self):
        start = 1000.0
        bar = self._bar(start, 100, 100.5, 96, 96.2, 500)
        prices = self._prices(100, 96.2)  # end near low → CLV<=0.30
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices,
            prices_15=prices[-15:],
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=False,
        )
        self.assertTrue(d.ok, d.reason)
        self.assertEqual(d.side, "SHORT")
        self.assertIn("TREND_SHORT", d.tags)

    def test_chop_blocks_surge(self):
        start = 1000.0
        bar = self._bar(start, 100, 101, 99, 100.1, 500)
        # oscillate → low ER
        prices = [100 + (0.2 if i % 2 == 0 else -0.2) for i in range(60)]
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices,
            prices_15=prices[-15:],
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=False,
        )
        self.assertFalse(d.ok)
        self.assertIn(d.reason, ("ER_TOO_LOW", "DIRECTION_FILTER_FAIL", "RANGE_TOO_SMALL"))

    def test_wick_reverse_short(self):
        start = 1000.0
        # large upper wick + pullback; 60s mostly one-way down for ER; last 15s <= -0.20%
        bar = self._bar(start, 100, 110, 99, 100.2, 500)
        prices60 = [104.0 - 0.06 * i for i in range(60)]  # 104 → 100.46, high ER
        prices15 = prices60[-15:]
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices60,
            prices_15=prices15,
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=False,
        )
        self.assertTrue(d.ok, f"{d.reason} metrics={d.metrics}")
        self.assertEqual(d.side, "SHORT")
        self.assertIn("WICK_REVERSE_SHORT", d.tags)

    def test_same_bar_dedupe_and_conflict(self):
        start = 1000.0
        bar = self._bar(start, 100, 103, 99.5, 102.8, 500)
        prices = self._prices(100, 102.5)
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices,
            prices_15=prices[-15:],
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=False,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=True,
        )
        self.assertEqual(d.reason, "SAME_BAR_FIRST_ENTRY_ALREADY_SENT")

    def test_long_blocks_immediate_short(self):
        start = 1000.0
        bar = self._bar(start, 100, 110, 99, 100.2, 500)
        prices60 = [104.0 - 0.06 * i for i in range(60)]
        d = evaluate_entry(
            decision_ts=start + 60,
            bar=bar,
            baseline_median=100,
            prices_60=prices60,
            prices_15=prices60[-15:],
            last_trade_ts=start + 60,
            gap_unfilled=False,
            has_position=True,
            has_exit_pending=False,
            first_entry_already_sent_this_bar=False,
        )
        self.assertEqual(d.reason, "MUST_FLAT_BEFORE_REVERSE_OR_SHORT")


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.set_runtime_root(self.tmp.name)
        Path(config.arm_paths()["root"]).mkdir(parents=True, exist_ok=True)
        Path(config.shared_paths()["root"]).mkdir(parents=True, exist_ok=True)
        self.filters = {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.01}
        self.st = Account(run_id="test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_four_legs_avg_and_trail_not_relaxed(self):
        st = self.st
        ok, reason, _ = queue_entry(
            st,
            symbol="AAAUSDT",
            side="LONG",
            bid=99.9,
            ask=100.0,
            filters=self.filters,
            signal={"tags": ["TREND_LONG"]},
            leg_index=1,
            bar_start_ts=100.0,
            now=1.0,
        )
        self.assertTrue(ok, reason)
        q1 = float(st.pending_entries["AAAUSDT"]["qty"])
        apply_entry_fill(st, "AAAUSDT", fill_px=100.0, fill_qty=q1, fill_ts=1.0)
        pos = st.positions["AAAUSDT"]
        extreme0 = float(pos["extreme_px"])
        stop0 = float(pos["trail_stop"])
        self.assertAlmostEqual(stop0, extreme0 * 0.99, places=6)
        # mark profit trailing
        update_trail(st, "AAAUSDT", 102.5, now=2.0)  # +2.5%
        self.assertTrue(st.positions["AAAUSDT"]["profit_trailing"])
        stop_after_profit = float(st.positions["AAAUSDT"]["trail_stop"])
        # add at higher price must not relax stop / downgrade profit flag
        ok2, reason2, _ = queue_entry(
            st,
            symbol="AAAUSDT",
            side="LONG",
            bid=100.4,
            ask=100.5,
            filters=self.filters,
            signal={"tags": ["ADD"]},
            leg_index=2,
            bar_start_ts=-1,
            now=3.0,
        )
        self.assertTrue(ok2, reason2)
        q2 = float(st.pending_entries["AAAUSDT"]["qty"])
        apply_entry_fill(st, "AAAUSDT", fill_px=100.5, fill_qty=q2, fill_ts=3.0)
        pos = st.positions["AAAUSDT"]
        self.assertTrue(pos["profit_trailing"])
        self.assertGreaterEqual(float(pos["trail_stop"]), stop_after_profit - 1e-9)
        self.assertEqual(int(pos["legs_filled"]), 2)
        # avg between 100 and 100.5
        self.assertGreater(float(pos["avg_entry"]), 100.0)
        self.assertLess(float(pos["avg_entry"]), 100.5)

    def test_short_legs_direction(self):
        st = self.st
        ok, reason, _ = queue_entry(
            st, symbol="BBBUSDT", side="SHORT", bid=100.0, ask=100.1, filters=self.filters,
            signal={"tags": ["TREND_SHORT"]}, leg_index=1, bar_start_ts=50.0, now=1.0,
        )
        self.assertTrue(ok, reason)
        apply_entry_fill(st, "BBBUSDT", fill_px=100.0, fill_qty=3.0, fill_ts=1.0)
        self.assertAlmostEqual(st.positions["BBBUSDT"]["trail_stop"], 100.0 * 1.01, places=6)

    def test_partial_cancel_counts_wave(self):
        st = self.st
        queue_entry(
            st, symbol="CCCUSDT", side="LONG", bid=99.9, ask=100.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=10.0, now=1.0,
        )
        apply_entry_fill(st, "CCCUSDT", fill_px=100.0, fill_qty=1.0, fill_ts=1.5, partial=True)
        cancel_pending_entry(st, "CCCUSDT", "TTL_EXPIRED", now=6.0)
        self.assertNotIn("CCCUSDT", st.pending_entries)
        self.assertEqual(int(st.positions["CCCUSDT"]["legs_used"]), 1)
        self.assertEqual(int(st.positions["CCCUSDT"]["legs_filled"]), 1)

    def test_max_slots_20(self):
        st = self.st
        for i in range(20):
            sym = f"S{i}USDT"
            ok, reason, _ = queue_entry(
                st, symbol=sym, side="LONG", bid=1.0, ask=1.0, filters=self.filters,
                signal={}, leg_index=1, bar_start_ts=float(i), now=float(i),
            )
            self.assertTrue(ok, reason)
            apply_entry_fill(st, sym, fill_px=1.0, fill_qty=300.0, fill_ts=float(i))
        ok, reason, _ = queue_entry(
            st, symbol="OVERUSDT", side="LONG", bid=1.0, ask=1.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=999.0, now=999.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MAX_SLOTS")

    def test_drop_top15_cancels_entry_keeps_risk(self):
        st = self.st
        queue_entry(
            st, symbol="DDDUSDT", side="LONG", bid=99.9, ask=100.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=1.0, now=1.0,
        )
        apply_entry_fill(st, "DDDUSDT", fill_px=100.0, fill_qty=3.0, fill_ts=1.0)
        queue_entry(
            st, symbol="EEEUSDT", side="LONG", bid=99.9, ask=100.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=2.0, now=2.0,
        )
        on_drop_from_top15(st, ["DDDUSDT", "EEEUSDT"], now=3.0)
        self.assertIn("DDDUSDT", st.positions)
        self.assertNotIn("EEEUSDT", st.pending_entries)
        ok, reason, _ = queue_entry(
            st, symbol="DDDUSDT", side="LONG", bid=100.4, ask=100.5, filters=self.filters,
            signal={"tags": ["ADD"]}, leg_index=2, bar_start_ts=-1, now=4.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "DROPPED_FROM_TOP15_NO_NEW")

    def test_limit_exit_unfilled_not_flat(self):
        st = self.st
        queue_entry(
            st, symbol="FFFUSDT", side="LONG", bid=99.9, ask=100.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=1.0, now=1.0,
        )
        apply_entry_fill(st, "FFFUSDT", fill_px=100.0, fill_qty=3.0, fill_ts=1.0)
        begin_exit(st, "FFFUSDT", reason="TRAIL_STOP", trigger_px=99.0, now=10.0, bid=98.5, ask=98.6)
        self.assertIn("FFFUSDT", st.positions)
        self.assertIn("FFFUSDT", st.pending_exits)
        # book below sell limit → no fill
        process_pending_exits(st, {"FFFUSDT": {"bid": 90.0, "ask": 90.1, "fetched_at": 12.0}}, now=12.0)
        self.assertIn("FFFUSDT", st.positions)
        # cross → flat
        process_pending_exits(st, {"FFFUSDT": {"bid": 99.0, "ask": 99.1, "fetched_at": 14.0}}, now=14.0)
        self.assertNotIn("FFFUSDT", st.positions)

    def test_paper_entry_fill_models(self):
        st = self.st
        queue_entry(
            st, symbol="GGGUSDT", side="LONG", bid=100.0, ask=100.0, filters=self.filters,
            signal={}, leg_index=1, bar_start_ts=1.0, now=5.0,
        )
        # waiting: ask above limit → no fill
        pe = st.pending_entries["GGGUSDT"]
        pe["limit_px"] = 100.0
        process_pending_entries(st, {"GGGUSDT": {"bid": 100.2, "ask": 100.3, "fetched_at": 5.1}}, now=5.1)
        self.assertIn("GGGUSDT", st.pending_entries)
        # immediate cross
        process_pending_entries(st, {"GGGUSDT": {"bid": 99.9, "ask": 99.95, "fetched_at": 5.2}}, now=5.2)
        self.assertIn("GGGUSDT", st.positions)

    def test_gross_pnl_sign(self):
        self.assertGreater(gross_pnl_pct("LONG", 100, 101) or 0, 0)
        self.assertLess(gross_pnl_pct("LONG", 100, 99) or 0, 0)
        self.assertGreater(gross_pnl_pct("SHORT", 100, 99) or 0, 0)

    def test_exchange_protection_flag_stays_false(self):
        self.assertFalse(self.st.exchange_protection_active)
        from h_surge_trend_v1.portfolio import save_account, load_account

        save_account(self.st)
        loaded = load_account()
        self.assertFalse(loaded.exchange_protection_active)


class TestAEUntouched(unittest.TestCase):
    def test_ae_packages_still_import(self):
        import a_top10_1m_down_v1.config as ac
        import e_top10_1m_down_v1.config as ec

        self.assertTrue(hasattr(ac, "STRATEGY_ID"))
        self.assertEqual(ec.POSITION_SIDE, "LONG")
        self.assertNotEqual(config.STRATEGY_ID, ec.STRATEGY_ID)


class TestBinanceAlign(unittest.TestCase):
    def test_client_id_prefix_h(self):
        from common import client_ids

        self.assertIn("H", client_ids.PREFIX)
        self.assertTrue(client_ids.PREFIX["H"].startswith("STRAT_H_"))
        cid = client_ids.make_client_order_id("H", layer="ENT")
        self.assertTrue(client_ids.belongs_to_strategy(cid, "H"))
        self.assertFalse(client_ids.belongs_to_strategy(cid, "E"))

    def test_testnet_env_recognizes_h(self):
        from common import testnet_env

        self.assertEqual(testnet_env.normalize_strategy("H"), "H")
        self.assertEqual(testnet_env.account_group("H"), "h")
        lock = testnet_env.default_lock_path_for_strategy("H")
        self.assertIn("testnet_active_symbols_h", str(lock).replace("\\", "/"))

    def test_hooks_disabled_without_flag(self):
        import os

        os.environ.pop("ENABLE_BINANCE_TESTNET_ORDER", None)
        from h_surge_trend_v1 import testnet_hooks

        self.assertFalse(testnet_hooks.enabled())

    def test_bridge_side_mapping_unchanged(self):
        from common.testnet_bridge import close_side_for_position, side_to_exchange

        self.assertEqual(side_to_exchange("LONG"), "BUY")
        self.assertEqual(side_to_exchange("SHORT"), "SELL")
        self.assertEqual(close_side_for_position("LONG"), "SELL")
        self.assertEqual(close_side_for_position("SHORT"), "BUY")


class TestNoFutureVolume(unittest.TestCase):
    def test_decision_uses_partial_bar_only(self):
        b = BarBuilder3m(180)
        start = 1_700_000_000.0
        start = int(start // 180) * 180
        for i in range(3):
            b.on_trade(TradePrint(start + 40 + i, 100, 1, 50))
        early = b.known_current(start + 42)
        # more volume arrives later — must not mutate early snapshot
        b.on_trade(TradePrint(start + 100, 100, 100, 10000))
        self.assertLess(early.quote_volume, 200)
        self.assertGreater(b.current.quote_volume, 10000)


if __name__ == "__main__":
    unittest.main()
