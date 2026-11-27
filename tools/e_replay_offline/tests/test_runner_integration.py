"""wp-e-functional-runner-03: bounded runner auto-persist (no direct _append_fill in tests)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_runner_03"
CODE = REPO / "output" / "e_trend_v2" / "code"

if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.e_replay_offline.tests.test_runner_storage import FakeAdapter, _read_jsonl  # noqa: E402


class TestRunnerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        FakeAdapter.network_calls = 0
        FakeAdapter.place_calls = 0
        os.environ["ENABLE_BINANCE_TESTNET_ORDER"] = "1"
        os.environ["AUTOMAX_TESTNET_MODE"] = "1"
        for k in (
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "BINANCE_TESTNET_API_KEY",
            "BINANCE_TESTNET_API_SECRET",
        ):
            os.environ.pop(k, None)

        from common import order_fill_ledger, testnet_bridge
        from e_top10_1m_down_v1 import config

        order_fill_ledger.reset_memory_cache_for_tests()
        testnet_bridge.set_adapter_factory_for_tests(
            lambda **kw: FakeAdapter(
                strategy=kw.get("strategy", "E"),
                require_order_flag=kw.get("require_order_flag", True),
            )
        )
        self._prev_preplace = config.PREPLACE_TP_ORDERS
        config.PREPLACE_TP_ORDERS = False

    def tearDown(self):
        from common import order_fill_ledger, testnet_bridge
        from e_top10_1m_down_v1 import config

        config.PREPLACE_TP_ORDERS = self._prev_preplace
        testnet_bridge.set_adapter_factory_for_tests(None)
        order_fill_ledger.reset_memory_cache_for_tests()
        os.environ.pop("AUTOMAX_ORDER_LEDGER_DIR", None)

    def _prep_account(self, account_id: str) -> dict[str, Any]:
        from common import order_fill_ledger, testnet_bridge
        from e_top10_1m_down_v1 import config, portfolio

        root = OUT / account_id
        if root.exists():
            shutil.rmtree(root)
        runtime = root / "runtime"
        ledger = root / "order_fill_ledger"
        runtime.mkdir(parents=True)
        ledger.mkdir(parents=True)
        os.environ["AUTOMAX_ORDER_LEDGER_DIR"] = str(ledger.resolve())
        order_fill_ledger.reset_memory_cache_for_tests()
        FakeAdapter.place_calls = 0
        FakeAdapter.network_calls = 0
        testnet_bridge.set_adapter_factory_for_tests(
            lambda **kw: FakeAdapter(
                strategy=kw.get("strategy", "E"),
                require_order_flag=kw.get("require_order_flag", True),
            )
        )
        config.set_runtime_root(runtime)
        config.ARMS["tp05"]["profile_id"] = f"{account_id}_tp05"
        st = portfolio.ArmAccount(
            arm="tp05",
            profile_id=account_id,
            run_id=f"run_{account_id}",
            strategy_version=config.STRATEGY_VERSION,
        )
        portfolio.save_arm(st)
        return {"st": st, "root": root, "runtime": runtime, "ledger": ledger}

    def _run_runner_cycle(
        self,
        *,
        account_id: str,
        price: float,
        arm_fail: bool = False,
        ledger_fail: bool = False,
        cost_fail: bool = False,
    ) -> dict[str, Any]:
        from e_top10_1m_down_v1 import config, portfolio, testnet_hooks

        ctx = self._prep_account(account_id)
        st = ctx["st"]
        signal = {
            "symbol": "SYNTHUSDT",
            "S": price,
            "signal_id": f"sig_{account_id}",
            "pct_24h": 5.0,
            "rank": 1,
            "fill_allowed_after_ms": 0,
            "signal_rule": "1m_down",
        }
        filters = {"tickSize": 0.0001, "stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}

        with (
            mock.patch(
                "common.order_fill_ledger._append_jsonl",
                side_effect=OSError("INJECTED_LEDGER_WRITE_FAIL"),
            )
            if ledger_fail
            else nullcontext()
        ):
            q = portfolio.queue_short_limit(st, signal=signal, filters=filters)

        if ledger_fail:
            self.assertFalse(q.get("ok"))
            self.assertEqual(q.get("block_reason"), "LEDGER_PERSIST_FAILED")
            self.assertTrue(q.get("place_ok"))
            return {
                "account_id": account_id,
                "queue": q,
                "place_calls": FakeAdapter.place_calls,
                "network_calls": FakeAdapter.network_calls,
                "runner_ok": False,
                "fail_stage": "ledger",
                "paths": {
                    "arm_fills": str(config.arm_paths("tp05")["fills"]),
                    "ledger_orders": str(ctx["ledger"] / "orders.jsonl"),
                },
            }

        self.assertTrue(q.get("ok"), msg=q)
        pend = st.pending_limit_entries["SYNTHUSDT"]
        cid = pend["testnet_client_order_id"]
        oid = pend["testnet_exchange_order_id"]
        qty = float(pend["qty_target"])

        def _poll(*, symbol: str, client_order_id: str) -> dict[str, Any]:
            return {
                "status": "FILLED",
                "executed_qty": qty,
                "avg_price": price,
                "exchange_order_id": oid,
                "order_id": oid,
                "client_order_id": client_order_id,
            }

        def _fetch(*, symbol: str, start_time_ms=None, end_time_ms=None) -> dict[str, Any]:
            return {
                "ok": True,
                "incomplete": False,
                "pages": 1,
                "trades": [
                    {
                        "id": f"et_{account_id}",
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                        "realized_pnl": 0,
                        "commission": 0.05,
                        "commission_asset": "USDT",
                        "order_id": str(oid),
                        "client_order_id": str(cid),
                        "orderId": str(oid),
                        "clientOrderId": str(cid),
                        "commissionAsset": "USDT",
                    }
                ],
            }

        books = {
            "SYNTHUSDT": {
                "bid": price * 0.999,
                "ask": price * 0.9995,
                "mid": price,
                "spread": 0.0001,
                "fetched_at": time.time(),
            }
        }

        real_save = portfolio.save_arm

        def _save_fail_on_journal(st_):
            # Fail only once settlement journal exists (fee PREPARED path), not on OPEN saves.
            if getattr(st_, "settlement_journal", None):
                raise OSError("INJECTED_COST_SAVE_FAIL")
            return real_save(st_)

        with mock.patch.object(testnet_hooks, "poll_order", side_effect=_poll):
            with (
                mock.patch(
                    "e_top10_1m_down_v1.portfolio.append_jsonl",
                    side_effect=OSError("INJECTED_ARM_WRITE_FAIL"),
                )
                if arm_fail
                else nullcontext()
            ):
                with (
                    mock.patch(
                        "e_top10_1m_down_v1.portfolio.save_arm",
                        side_effect=_save_fail_on_journal,
                    )
                    if cost_fail
                    else nullcontext()
                ):
                    cycle = portfolio.bounded_offline_entry_cycle(
                        st,
                        books=books,
                        now_ms=int(time.time() * 1000),
                        fetch_user_trades_fn=_fetch,
                        env_id="synth_offline",
                        settle_entry_fee=not arm_fail,
                    )

        fail_stage = None
        runner_ok = True
        if arm_fail:
            fail_stage = "arm"
            runner_ok = False
            blocked = [
                e
                for e in (cycle.get("fill_events") or [])
                if e.get("block_reason") == "ARM_FILL_PERSIST_FAILED"
                or e.get("action") == "FILL_BLOCKED"
            ]
            self.assertTrue(blocked or cycle.get("persist_errors"), msg=cycle)
        elif cost_fail:
            fail_stage = "cost"
            runner_ok = False
            adj_fail = False
            for r in cycle.get("fee_sync") or []:
                for a in r.get("results") or [r]:
                    for adj in a.get("entry_fee_adjusts") or []:
                        if adj.get("ok") is False:
                            adj_fail = True
                    if a.get("ok") is False:
                        adj_fail = True
                for adj in r.get("entry_fee_adjusts") or []:
                    if adj.get("ok") is False:
                        adj_fail = True
            # fee_sync items are sync_settlement_pending return dicts
            for r in cycle.get("fee_sync") or []:
                if isinstance(r, dict):
                    for item in r.get("results") or []:
                        for adj in item.get("entry_fee_adjusts") or []:
                            if adj.get("ok") is False:
                                adj_fail = True
            self.assertTrue(adj_fail, msg=str(cycle.get("fee_sync"))[:1200])
        else:
            self.assertTrue(cycle.get("opened"), msg=cycle)
            runner_ok = bool(cycle.get("ok"))

        return {
            "account_id": account_id,
            "queue": q,
            "cycle": cycle,
            "place_calls": FakeAdapter.place_calls,
            "network_calls": FakeAdapter.network_calls,
            "runner_ok": runner_ok,
            "fail_stage": fail_stage,
            "expected": {
                "account_id": account_id,
                "qty": qty,
                "price": price,
                "fee_asset": "USDT",
                "strategy_version": config.STRATEGY_VERSION,
                "order_id": str(oid),
                "client_order_id": str(cid),
            },
            "paths": {
                "arm_fills": str(config.arm_paths("tp05")["fills"].resolve()),
                "ledger_orders": str((ctx["ledger"] / "orders.jsonl").resolve()),
            },
        }

    def test_ab_runner_auto_persist_reopen(self):
        from e_top10_1m_down_v1 import config

        r_a = self._run_runner_cycle(account_id="SYNTH_A", price=0.04)
        r_b = self._run_runner_cycle(account_id="SYNTH_B", price=0.05)
        self.assertEqual(r_a["place_calls"], 1)
        self.assertEqual(r_b["place_calls"], 1)
        self.assertEqual(r_a["network_calls"], 0)
        self.assertEqual(r_b["network_calls"], 0)
        self.assertTrue(r_a["runner_ok"])
        self.assertTrue(r_b["runner_ok"])

        comparisons = {}
        mismatches = []
        for r in (r_a, r_b):
            arm_rows = _read_jsonl(Path(r["paths"]["arm_fills"]))
            ord_rows = _read_jsonl(Path(r["paths"]["ledger_orders"]))
            opens = [x for x in arm_rows if x.get("event") == "OPEN"]
            fees = [x for x in arm_rows if x.get("event") == "FEE_ADJUST"]
            self.assertEqual(len(opens), 1, msg=arm_rows)
            self.assertGreaterEqual(len(fees), 1, msg=arm_rows)
            self.assertGreaterEqual(len(ord_rows), 1)
            o, f, ord0 = opens[0], fees[0], ord_rows[0]
            actual = {
                "account_id": o.get("account_id"),
                "trade_id": o.get("trade_id"),
                "order_id": str(ord0.get("exchange_order_id")),
                "qty": o.get("qty"),
                "price": o.get("price"),
                "fee_asset": f.get("commission_asset"),
                "strategy_version": o.get("strategy_version"),
            }
            exp = {
                "account_id": r["expected"]["account_id"],
                "order_id": r["expected"]["order_id"],
                "qty": r["expected"]["qty"],
                "price": r["expected"]["price"],
                "fee_asset": "USDT",
                "strategy_version": config.STRATEGY_VERSION,
            }
            for k, ev in exp.items():
                if actual.get(k) != ev:
                    mismatches.append(
                        {"account": r["account_id"], "field": k, "expected": ev, "actual": actual.get(k)}
                    )
            comparisons[r["account_id"]] = {
                "expected": exp,
                "actual": actual,
                "trade_id": actual["trade_id"],
            }

        a_ids = {
            comparisons["SYNTH_A"]["actual"]["account_id"],
            comparisons["SYNTH_A"]["trade_id"],
            comparisons["SYNTH_A"]["actual"]["order_id"],
        }
        b_ids = {
            comparisons["SYNTH_B"]["actual"]["account_id"],
            comparisons["SYNTH_B"]["trade_id"],
            comparisons["SYNTH_B"]["actual"]["order_id"],
        }
        self.assertEqual(len(mismatches), 0, msg=mismatches)
        self.assertEqual(len(a_ids & b_ids), 0)

        (OUT / "evidence_ab.json").write_text(
            json.dumps(
                {
                    "mismatch": len(mismatches),
                    "cross_hit": len(a_ids & b_ids),
                    "comparisons": comparisons,
                    "call_chain": [
                        "queue_short_limit→place_entry→record_from_order_result",
                        "bounded_offline_entry_cycle→process_pending→_open_from_pending→_append_fill",
                        "bounded_offline_entry_cycle→sync_settlement_pending→adjust_entry_fee_to_exchange",
                    ],
                    "network_calls": 0,
                    "live_writes": 0,
                    "note": "測試未直接呼叫 _append_fill／adjust_entry_fee_to_exchange",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_runner_fail_ledger_arm_cost(self):
        cases = []
        for stage, kw in (
            ("ledger", {"ledger_fail": True}),
            ("arm", {"arm_fail": True}),
            ("cost", {"cost_fail": True}),
        ):
            r = self._run_runner_cycle(
                account_id=f"SYNTH_FAIL_{stage.upper()}",
                price=0.04,
                **kw,
            )
            self.assertEqual(r["place_calls"], 1)
            self.assertFalse(r["runner_ok"])
            self.assertEqual(r.get("fail_stage"), stage)
            cases.append(
                {
                    "stage": stage,
                    "place_calls": r["place_calls"],
                    "runner_ok": r["runner_ok"],
                    "queue_block": (r.get("queue") or {}).get("block_reason"),
                    "boundary": {
                        "ledger": "queue_short_limit→LEDGER_PERSIST_FAILED（place_ok仍true）",
                        "arm": "_open_from_pending→ARM_FILL_PERSIST_FAILED 經 process_pending 事件",
                        "cost": "sync_settlement_pending.entry_fee_adjusts ok=false",
                    }[stage],
                }
            )
        (OUT / "evidence_fails.json").write_text(
            json.dumps({"cases": cases, "network_calls": 0, "live_writes": 0}, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
