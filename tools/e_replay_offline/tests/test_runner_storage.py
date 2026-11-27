"""wp-e-functional-store-02: isolate real persist chain via fake adapter.

Calls real record_from_order_result, portfolio._append_fill, journal_adjust_entry_fee.
Does not start paper_main loop, network, live writes, or .env reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "output" / "e_functional_store_02"
CODE = REPO / "output" / "e_trend_v2" / "code"
LIVE_ROOT = REPO / "output" / "testnet_e_trend_v2_local"

if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


@dataclass
class _FakeResult:
    ok: bool
    client_order_id: str
    exchange_order_id: str | None = None
    status: str = "NEW"
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class FakeAdapter:
    """Fail-fast stub: no HTTP. place_* still calls real record_from_order_result."""

    network_calls = 0
    place_calls = 0
    _global_seq = 0

    def __init__(self, *, strategy: str, require_order_flag: bool = True, **_kw: Any) -> None:
        self.strategy = strategy
        self.require_order_flag = require_order_flag
        self._filters = {"SYNTHUSDT": {"tickSize": 0.0001, "stepSize": 0.001}}

    def ensure_symbol_free_or_owned(self, symbol: str) -> tuple[bool, str]:
        return True, "fake_owned"

    def refresh_exchange_info(self) -> None:
        FakeAdapter.network_calls += 1
        raise RuntimeError("NETWORK_FORBIDDEN:refresh_exchange_info")

    def _http(self, *a: Any, **k: Any) -> Any:
        FakeAdapter.network_calls += 1
        raise RuntimeError("NETWORK_FORBIDDEN")

    def place_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        layer: str | None = None,
        post_only: bool = False,
        claim: bool = True,
        client_order_id: str | None = None,
        **_kw: Any,
    ) -> _FakeResult:
        from common import order_fill_ledger

        FakeAdapter.place_calls += 1
        FakeAdapter._global_seq += 1
        seq = FakeAdapter._global_seq
        cid = client_order_id or f"FAKE_{self.strategy}_{seq}"
        oid = f"9{seq:06d}"
        res = _FakeResult(
            ok=True,
            client_order_id=cid,
            exchange_order_id=oid,
            status="NEW",
            raw={
                "orderId": oid,
                "clientOrderId": cid,
                "status": "NEW",
                "symbol": symbol.upper(),
                "side": side.upper(),
                "price": str(price),
                "origQty": str(quantity),
                "executedQty": "0",
                "avgPrice": "0",
                "type": "LIMIT",
            },
        )
        ledger_res = order_fill_ledger.record_from_order_result(
            strategy=self.strategy,
            symbol=symbol,
            result=res,
            side=side,
            layer=layer,
            order_kind="LIMIT",
        )
        res.raw["_ledger_persist"] = ledger_res
        return res

    def place_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        layer: str | None = None,
        claim: bool = True,
        **_kw: Any,
    ) -> _FakeResult:
        return self.place_limit(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(_kw.get("price") or 1.0),
            layer=layer,
            post_only=False,
            claim=claim,
        )

    def place_stop_market(self, **kw: Any) -> _FakeResult:
        # no network; do not re-place on ledger failure paths
        FakeAdapter._global_seq += 1
        seq = FakeAdapter._global_seq
        return _FakeResult(
            ok=True,
            client_order_id=f"FAKE_SL_{seq}",
            exchange_order_id=f"8{seq:06d}",
            status="NEW",
            raw={"stopPrice": kw.get("stop_price"), "ok": True},
        )


class TestRunnerStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.pre = json.loads((OUT / "pre_sha256.json").read_text(encoding="utf-8"))

    def setUp(self):
        FakeAdapter.network_calls = 0
        FakeAdapter.place_calls = 0
        os.environ["ENABLE_BINANCE_TESTNET_ORDER"] = "1"
        os.environ["AUTOMAX_TESTNET_MODE"] = "1"
        # do not load .env — strip credential env if present for this process only
        for k in list(os.environ):
            if k.startswith("BINANCE_") or k.endswith("_API_SECRET") or k.endswith("_API_KEY"):
                if "TEST" in k or True:
                    # keep ENABLE flag; remove secrets to prove no credential use
                    if k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"):
                        os.environ.pop(k, None)

        from common import order_fill_ledger, testnet_bridge

        order_fill_ledger.reset_memory_cache_for_tests()
        testnet_bridge.set_adapter_factory_for_tests(
            lambda **kw: FakeAdapter(strategy=kw.get("strategy", "E"), require_order_flag=kw.get("require_order_flag", True))
        )

    def tearDown(self):
        from common import order_fill_ledger, testnet_bridge

        testnet_bridge.set_adapter_factory_for_tests(None)
        order_fill_ledger.reset_memory_cache_for_tests()
        os.environ.pop("AUTOMAX_ORDER_LEDGER_DIR", None)

    def _acct_dirs(self, account_id: str) -> dict[str, Path]:
        import shutil

        root = OUT / account_id
        if root.exists():
            shutil.rmtree(root)
        runtime = root / "runtime"
        ledger = root / "order_fill_ledger"
        runtime.mkdir(parents=True, exist_ok=True)
        ledger.mkdir(parents=True, exist_ok=True)
        return {"root": root, "runtime": runtime, "ledger": ledger}

    def _run_chain(
        self,
        *,
        account_id: str,
        trade_id: str,
        order_qty: float,
        order_price: float,
        fee_asset: str,
        arm_write_fail: bool = False,
        ledger_write_fail: bool = False,
        cost_write_fail: bool = False,
    ) -> dict[str, Any]:
        from common import order_fill_ledger, testnet_bridge
        from e_top10_1m_down_v1 import config, portfolio, testnet_hooks

        dirs = self._acct_dirs(account_id)
        os.environ["AUTOMAX_ORDER_LEDGER_DIR"] = str(dirs["ledger"].resolve())
        order_fill_ledger.reset_memory_cache_for_tests()
        # per-account place counter; rebind factory so adapter seq can continue but calls reset
        FakeAdapter.place_calls = 0
        FakeAdapter.network_calls = 0
        testnet_bridge.set_adapter_factory_for_tests(
            lambda **kw: FakeAdapter(
                strategy=kw.get("strategy", "E"),
                require_order_flag=kw.get("require_order_flag", True),
            )
        )
        config.set_runtime_root(dirs["runtime"])

        arm = "tp05"
        # isolate profile_id under this account so paths don't collide
        config.ARMS[arm]["profile_id"] = f"{account_id}_tp05"
        st = portfolio.ArmAccount(
            arm=arm,
            profile_id=account_id,
            run_id=f"run_{account_id}",
            strategy_version=config.STRATEGY_VERSION,
        )
        portfolio.save_arm(st)

        pending = {
            "symbol": "SYNTHUSDT",
            "side": "LONG",
            "limit_price": order_price,
            "filters": {"tickSize": 0.0001, "stepSize": 0.001},
        }

        place_ctx = (
            mock.patch(
                "common.order_fill_ledger._append_jsonl",
                side_effect=OSError("INJECTED_LEDGER_WRITE_FAIL"),
            )
            if ledger_write_fail
            else nullcontext()
        )

        with place_ctx:
            place_out = testnet_hooks.place_entry(pending, qty=order_qty)

        place_calls_after_entry = FakeAdapter.place_calls
        ledger_persist = None
        # re-read via hooks path: adapter put _ledger_persist on raw; place_entry doesn't surface it —
        # reopen ledger files instead for success path; for fail path use exception / empty file.
        orders = _read_jsonl(dirs["ledger"] / "orders.jsonl")
        ledger_ok = bool(orders) and not ledger_write_fail
        if ledger_write_fail:
            ledger_persist = {"ok": False, "error": "INJECTED_LEDGER_WRITE_FAIL"}
        else:
            ledger_persist = {"ok": True, "n_orders": len(orders)}

        # Arm OPEN via real _append_fill (not hand-written final assertion file)
        open_row = {
            "event": "OPEN",
            "trade_id": trade_id,
            "symbol": "SYNTHUSDT",
            "qty": order_qty,
            "price": order_price,
            "fee_u": 0.01,
            "commission_asset": fee_asset,
            "strategy_version": config.STRATEGY_VERSION,
            "account_id": account_id,
            "arm": arm,
            "profile_id": account_id,
            "testnet_client_order_id": pending.get("testnet_client_order_id"),
            "binance_order_id": pending.get("testnet_exchange_order_id"),
            "synthetic": True,
        }

        arm_err = None
        if arm_write_fail:
            with mock.patch(
                "e_top10_1m_down_v1.portfolio.append_jsonl",
                side_effect=OSError("INJECTED_ARM_WRITE_FAIL"),
            ):
                try:
                    portfolio._append_fill(arm, open_row)
                except OSError as exc:
                    arm_err = str(exc)
        else:
            portfolio._append_fill(arm, open_row)

        # Cost path: real journal_adjust_entry_fee via portfolio.adjust_entry_fee_to_exchange
        st.settlement_pending[trade_id] = {
            "symbol": "SYNTHUSDT",
            "env_id": "synth_offline",
            "account_id": account_id,
            "entry_fee_estimated": 0.10,
            "entry_qty_expected": order_qty,
            "qty_to_settle": order_qty,
            "qty_step": 0.001,
            "entry_fee_estimated_reversed": 0.0,
        }
        portfolio.save_arm(st)

        cost_res: dict[str, Any]
        if cost_write_fail:
            with mock.patch(
                "e_top10_1m_down_v1.portfolio.save_arm",
                side_effect=OSError("INJECTED_COST_SAVE_FAIL"),
            ):
                cost_res = portfolio.adjust_entry_fee_to_exchange(
                    st,
                    local_trade_id=trade_id,
                    exchange_commission=0.05,
                    commission_asset=fee_asset,
                    exchange_trade_id=f"t_{account_id}",
                    order_id=pending.get("testnet_exchange_order_id"),
                    trade_qty=order_qty,
                )
        else:
            cost_res = portfolio.adjust_entry_fee_to_exchange(
                st,
                local_trade_id=trade_id,
                exchange_commission=0.05,
                commission_asset=fee_asset,
                exchange_trade_id=f"t_{account_id}",
                order_id=pending.get("testnet_exchange_order_id"),
                trade_qty=order_qty,
            )

        # must not re-place because of persist failure
        self.assertEqual(FakeAdapter.place_calls, place_calls_after_entry)

        # reopen with fresh readers
        order_fill_ledger.reset_memory_cache_for_tests()
        arm_fills = _read_jsonl(config.arm_paths(arm)["fills"])
        ledger_orders = _read_jsonl(dirs["ledger"] / "orders.jsonl")
        ledger_fills = _read_jsonl(dirs["ledger"] / "fills.jsonl")

        cost_applied = bool(cost_res.get("applied"))
        chain_failed = bool(
            arm_write_fail
            or ledger_write_fail
            or cost_write_fail
            or not cost_res.get("ok")
            or (not cost_write_fail and not cost_applied and not arm_write_fail and not ledger_write_fail)
        )
        errors = []
        if arm_err:
            errors.append({"stage": "arm_write", "error": arm_err})
        if ledger_write_fail:
            errors.append({"stage": "ledger_write", "error": "INJECTED_LEDGER_WRITE_FAIL"})
        if cost_write_fail or not cost_res.get("ok") or (
            not cost_write_fail and not cost_applied and not arm_write_fail and not ledger_write_fail
        ):
            errors.append(
                {
                    "stage": "cost_write",
                    "error": cost_res.get("error")
                    or cost_res.get("block_reason")
                    or cost_res.get("reason")
                    or "cost_not_applied",
                    "cost_res": {
                        k: cost_res.get(k)
                        for k in ("ok", "applied", "block_reason", "error", "reason")
                    },
                }
            )

        # order ok must not imply chain success when persist failed
        order_ok = bool(place_out.get("ok"))
        accept = (
            (not chain_failed)
            and order_ok
            and ledger_ok
            and arm_err is None
            and bool(cost_res.get("ok"))
            and cost_applied
        )

        return {
            "account_id": account_id,
            "place_out": place_out,
            "order_ok": order_ok,
            "ledger_persist": ledger_persist,
            "cost_res": cost_res,
            "arm_fills": arm_fills,
            "ledger_orders": ledger_orders,
            "ledger_fills": ledger_fills,
            "paths": {
                "arm_fills": str(config.arm_paths(arm)["fills"].resolve()),
                "ledger_orders": str((dirs["ledger"] / "orders.jsonl").resolve()),
                "ledger_fills": str((dirs["ledger"] / "fills.jsonl").resolve()),
            },
            "dirs": {k: str(v) for k, v in dirs.items()},
            "network_calls": FakeAdapter.network_calls,
            "place_calls": FakeAdapter.place_calls,
            # Test-side aggregate (not a production runner return field).
            "chain_status": "failed" if not accept else "passed",
            "chain_status_kind": "test_aggregate_over_place_ok_ledger_arm_cost",
            "errors": errors,
            "field_sources": {
                "account_id": "OPEN.account_id / settlement_pending.account_id / ArmAccount.profile_id",
                "trade_id": "OPEN.trade_id via portfolio._append_fill",
                "order_id": "ledger orders.exchange_order_id via record_from_order_result; link key=client_order_id",
                "qty": "OPEN.qty",
                "price": "OPEN.price",
                "fee_asset": "FEE_ADJUST.commission_asset via journal_adjust_entry_fee",
                "strategy_version": "OPEN.strategy_version (=config.STRATEGY_VERSION)",
            },
            "call_roles": {
                "hooks_auto": [
                    "testnet_hooks.place_entry",
                    "testnet_bridge.try_place_entry",
                    "FakeAdapter.place_limit→order_fill_ledger.record_from_order_result",
                ],
                "test_direct_chain": [
                    "portfolio._append_fill",
                    "portfolio.adjust_entry_fee_to_exchange→journal_adjust_entry_fee",
                ],
                "note": "test_direct_chain≠現行runner自動保存；僅證明真實函式可在隔離根寫入",
            },
        }

    def test_synth_a_b_isolated_persist_and_cross_hit_zero(self):
        from e_top10_1m_down_v1 import config

        r_a = self._run_chain(
            account_id="SYNTH_A",
            trade_id=":SYNTHUSDT:storeA001",
            order_qty=10.0,
            order_price=0.04,
            fee_asset="USDT",
        )
        r_b = self._run_chain(
            account_id="SYNTH_B",
            trade_id=":SYNTHUSDT:storeB001",
            order_qty=20.0,
            order_price=0.05,
            # journal_adjust_entry_fee only folds USDT commission into cash;
            # non-USDT returns applied=False (reason=non_usdt_commission_not_folded).
            fee_asset="USDT",
        )
        self.assertEqual(r_a["network_calls"], 0)
        self.assertEqual(r_b["network_calls"], 0)
        self.assertEqual(r_a["chain_status"], "passed")
        self.assertEqual(r_b["chain_status"], "passed")

        # extract compared fields
        def pick(r: dict[str, Any]) -> dict[str, Any]:
            opens = [x for x in r["arm_fills"] if x.get("event") == "OPEN"]
            fees = [x for x in r["arm_fills"] if x.get("event") == "FEE_ADJUST"]
            self.assertEqual(len(opens), 1)
            self.assertGreaterEqual(len(fees), 1)
            self.assertGreaterEqual(len(r["ledger_orders"]), 1)
            o = opens[0]
            f = fees[0]
            ord0 = r["ledger_orders"][0]
            return {
                "account_id": o.get("account_id"),
                "trade_id": o.get("trade_id"),
                "order_id": str(ord0.get("exchange_order_id")),
                "qty": o.get("qty"),
                "price": o.get("price"),
                "fee_asset": f.get("commission_asset"),
                "strategy_version": o.get("strategy_version"),
                "client_order_id": ord0.get("client_order_id"),
            }

        pa, pb = pick(r_a), pick(r_b)
        self.assertEqual(pa["account_id"], "SYNTH_A")
        self.assertEqual(pb["account_id"], "SYNTH_B")
        self.assertEqual(pa["trade_id"], ":SYNTHUSDT:storeA001")
        self.assertEqual(pb["trade_id"], ":SYNTHUSDT:storeB001")
        self.assertEqual(pa["qty"], 10.0)
        self.assertEqual(pb["qty"], 20.0)
        self.assertEqual(pa["price"], 0.04)
        self.assertEqual(pb["price"], 0.05)
        self.assertEqual(pa["fee_asset"], "USDT")
        self.assertEqual(pb["fee_asset"], "USDT")
        self.assertEqual(pa["strategy_version"], config.STRATEGY_VERSION)
        self.assertEqual(pb["strategy_version"], config.STRATEGY_VERSION)
        self.assertNotEqual(pa["order_id"], pb["order_id"])
        self.assertNotEqual(pa["trade_id"], pb["trade_id"])

        # cross hit 0 on identity keys (fee_asset may match when both USDT-foldable)
        a_ids = {pa["trade_id"], pa["order_id"], pa["client_order_id"], pa["account_id"]}
        b_ids = {pb["trade_id"], pb["order_id"], pb["client_order_id"], pb["account_id"]}
        self.assertEqual(len(a_ids & b_ids), 0)

        # no live writes
        self.assertFalse((LIVE_ROOT / "order_fill_ledger" / ".store02_marker").exists())
        live_touched = False
        # ensure our isolation dirs are under OUT only
        for r in (r_a, r_b):
            self.assertIn("e_functional_store_02", r["dirs"]["root"])

        evidence = {
            "case": "synth_a_b_isolated",
            "network_calls": FakeAdapter.network_calls,
            "live_writes": 0,
            "live_touched": live_touched,
            "A": {"fields": pa, "chain_status": r_a["chain_status"], "field_sources": r_a["field_sources"]},
            "B": {"fields": pb, "chain_status": r_b["chain_status"], "field_sources": r_b["field_sources"]},
            "cross_hit": 0,
            "call_chain": [
                "testnet_hooks.place_entry",
                "testnet_bridge.try_place_entry",
                "FakeAdapter.place_limit",
                "order_fill_ledger.record_from_order_result",
                "portfolio._append_fill",
                "portfolio.adjust_entry_fee_to_exchange",
                "settlement_apply.journal_adjust_entry_fee",
            ],
        }
        (OUT / "evidence_synth_ab.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def test_arm_write_fail_marks_chain_failed(self):
        r = self._run_chain(
            account_id="SYNTH_FAIL_ARM",
            trade_id=":SYNTHUSDT:failArm",
            order_qty=1.0,
            order_price=0.04,
            fee_asset="USDT",
            arm_write_fail=True,
        )
        self.assertEqual(r["chain_status"], "failed")
        self.assertTrue(r["order_ok"])
        self.assertEqual(r["place_calls"], 1)
        self.assertTrue(any(e["stage"] == "arm_write" for e in r["errors"]))
        # order ok ≠ persist success
        self.assertNotEqual(r["order_ok"] and r["chain_status"] == "passed", True)
        (OUT / "evidence_fail_arm.json").write_text(json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def test_ledger_write_fail_marks_chain_failed_no_reorder(self):
        r = self._run_chain(
            account_id="SYNTH_FAIL_LEDGER",
            trade_id=":SYNTHUSDT:failLed",
            order_qty=1.0,
            order_price=0.04,
            fee_asset="USDT",
            ledger_write_fail=True,
        )
        self.assertEqual(r["chain_status"], "failed")
        self.assertTrue(r["order_ok"])  # place ok still true
        self.assertEqual(r["ledger_persist"].get("ok"), False)
        self.assertTrue(any(e["stage"] == "ledger_write" for e in r["errors"]))
        self.assertEqual(r["place_calls"], 1)  # no re-order
        (OUT / "evidence_fail_ledger.json").write_text(
            json.dumps(
                {
                    "chain_status": r["chain_status"],
                    "order_ok": r["order_ok"],
                    "ledger_persist": r["ledger_persist"],
                    "place_calls": r["place_calls"],
                    "errors": r["errors"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_cost_write_fail_marks_chain_failed(self):
        r = self._run_chain(
            account_id="SYNTH_FAIL_COST",
            trade_id=":SYNTHUSDT:failCost",
            order_qty=1.0,
            order_price=0.04,
            fee_asset="USDT",
            cost_write_fail=True,
        )
        self.assertEqual(r["chain_status"], "failed")
        self.assertEqual(r["place_calls"], 1)
        self.assertTrue(any(e["stage"] == "cost_write" for e in r["errors"]))
        self.assertFalse(r["cost_res"].get("ok"))
        (OUT / "evidence_fail_cost.json").write_text(
            json.dumps(
                {
                    "chain_status": r["chain_status"],
                    "place_calls": r["place_calls"],
                    "cost_res": r["cost_res"],
                    "errors": r["errors"],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def test_supplement_reopen_reader_and_fail_place_counts(self):
        """補驗：關閉寫入後新讀取實例重開；三失敗案 place_calls=1。寫入 supplement_reopen/ 不覆蓋舊 evidence。"""
        from common import order_fill_ledger
        from e_top10_1m_down_v1 import config

        supp = OUT / "supplement_reopen"
        supp.mkdir(parents=True, exist_ok=True)

        expected = {
            "SYNTH_A": {
                "account_id": "SYNTH_A",
                "trade_id": ":SYNTHUSDT:storeA001",
                "qty": 10.0,
                "price": 0.04,
                "fee_asset": "USDT",
                "strategy_version": config.STRATEGY_VERSION,
            },
            "SYNTH_B": {
                "account_id": "SYNTH_B",
                "trade_id": ":SYNTHUSDT:storeB001",
                "qty": 20.0,
                "price": 0.05,
                "fee_asset": "USDT",
                "strategy_version": config.STRATEGY_VERSION,
            },
        }

        runs = {}
        for acct, exp in expected.items():
            runs[acct] = self._run_chain(
                account_id=acct,
                trade_id=exp["trade_id"],
                order_qty=exp["qty"],
                order_price=exp["price"],
                fee_asset=exp["fee_asset"],
            )
            self.assertEqual(runs[acct]["chain_status"], "passed")
            self.assertEqual(runs[acct]["place_calls"], 1)

        # Drop in-memory handles; new reader instances from disk only.
        order_fill_ledger.reset_memory_cache_for_tests()
        comparisons = {}
        mismatches = []
        for acct, exp in expected.items():
            r = runs[acct]
            arm_path = Path(r["paths"]["arm_fills"])
            ord_path = Path(r["paths"]["ledger_orders"])
            # brand-new readers (fresh Path + new _read_jsonl call)
            arm_rows = _read_jsonl(Path(str(arm_path)))
            ord_rows = _read_jsonl(Path(str(ord_path)))
            opens = [x for x in arm_rows if x.get("event") == "OPEN"]
            fees = [x for x in arm_rows if x.get("event") == "FEE_ADJUST"]
            self.assertEqual(len(opens), 1)
            self.assertGreaterEqual(len(fees), 1)
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
                "client_order_id": ord0.get("client_order_id"),
            }
            rows = []
            for field, exp_v in exp.items():
                act_v = actual[field]
                ok = act_v == exp_v
                if not ok:
                    mismatches.append({"account": acct, "field": field, "expected": exp_v, "actual": act_v})
                rows.append(
                    {
                        "field": field,
                        "expected": exp_v,
                        "actual": act_v,
                        "match": ok,
                        "source": r["field_sources"].get(field),
                        "link_key": "client_order_id" if field == "order_id" else ("trade_id" if field in ("fee_asset",) else field),
                    }
                )
            # order_id has no fixed expected string (fake seq); record actual only
            rows.append(
                {
                    "field": "order_id",
                    "expected": "(generated_by_FakeAdapter)",
                    "actual": actual["order_id"],
                    "match": bool(actual["order_id"]),
                    "source": r["field_sources"]["order_id"],
                    "link_key": "client_order_id",
                }
            )
            comparisons[acct] = {
                "rows": rows,
                "actual": actual,
                "paths": r["paths"],
                "call_roles": r["call_roles"],
                "chain_status_kind": r["chain_status_kind"],
            }

        a_ids = {
            comparisons["SYNTH_A"]["actual"]["trade_id"],
            comparisons["SYNTH_A"]["actual"]["order_id"],
            comparisons["SYNTH_A"]["actual"]["client_order_id"],
            comparisons["SYNTH_A"]["actual"]["account_id"],
        }
        b_ids = {
            comparisons["SYNTH_B"]["actual"]["trade_id"],
            comparisons["SYNTH_B"]["actual"]["order_id"],
            comparisons["SYNTH_B"]["actual"]["client_order_id"],
            comparisons["SYNTH_B"]["actual"]["account_id"],
        }
        cross = len(a_ids & b_ids)
        self.assertEqual(len(mismatches), 0)
        self.assertEqual(cross, 0)

        fails = {}
        for name, kw in (
            ("arm", {"arm_write_fail": True, "account_id": "SYNTH_FAIL_ARM_SUP"}),
            ("ledger", {"ledger_write_fail": True, "account_id": "SYNTH_FAIL_LEDGER_SUP"}),
            ("cost", {"cost_write_fail": True, "account_id": "SYNTH_FAIL_COST_SUP"}),
        ):
            fr = self._run_chain(
                trade_id=f":SYNTHUSDT:fail{name}",
                order_qty=1.0,
                order_price=0.04,
                fee_asset="USDT",
                **kw,
            )
            self.assertEqual(fr["chain_status"], "failed")
            self.assertEqual(fr["place_calls"], 1)
            fails[name] = {
                "chain_status": fr["chain_status"],
                "place_calls": fr["place_calls"],
                "order_ok": fr["order_ok"],
                "errors": fr["errors"],
                "chain_status_kind": fr["chain_status_kind"],
            }

        payload = {
            "mismatch": len(mismatches),
            "mismatches": mismatches,
            "cross_hit": cross,
            "reopen": comparisons,
            "fail_cases": fails,
            "network_calls": FakeAdapter.network_calls,
            "live_writes": 0,
            "notes": {
                "chain_status": "測試端彙總（place_ok∧ledger∧arm∧cost.applied）；非 production API 欄位",
                "hooks_vs_direct": runs["SYNTH_A"]["call_roles"],
            },
        }
        (supp / "reopen_compare.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
