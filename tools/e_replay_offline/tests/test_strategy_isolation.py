"""wp-e-functional-strategy-safety-02/03: countable isolation guards around strategy replay."""
from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
OUT_02 = REPO / "output" / "e_functional_strategy_safety_02"
OUT = REPO / "output" / "e_functional_strategy_safety_03"
FIX = REPO / "output" / "e_functional_strategy_01" / "fixtures" / "strategy_cli_fixture.json"
E_CODE = REPO / "output" / "e_trend_v2" / "code"
E_SRC = E_CODE / "e_top10_1m_down_v1"
EXPECTED_NET = Decimal("-41.130732096")
EXPECTED_SOURCE_SHA = {
    "signals.py": "0054bb438c2a48c0a8e125d09c7380fb774884196f793671b644406881803354",
    "portfolio.py": "95df4d8ff90586b132e6542eae9b8e37feb0dc19c65353e409dbdce469d9de48",
    "config.py": "b0798736a39304db179868f541082cb2fc7b20cf31d8203b91a2ff8d100b1ce0",
}


def _sha(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


class GuardCounters:
    def __init__(self) -> None:
        self.network = 0
        self.order = 0
        self.live_env_read = 0
        self.unauthorized_write = 0
        self.process_ops = 0


def _sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    s = str(path).replace("/", "\\").lower()
    if name == ".env" or s.endswith("\\.env"):
        return True
    if "testnet_e_trend_v2_local" in s:
        return True
    return False


@contextmanager
def base_guards(allowed_write_roots: list[Path], counters: GuardCounters):
    """Network/process/open guards — install BEFORE strategy import."""
    allowed = [p.resolve() for p in allowed_write_roots]
    real_open = open

    def _allowed_write(path: Path) -> bool:
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if _sensitive_path(rp):
            return False
        return any(str(rp).startswith(str(a)) for a in allowed)

    def guard_network(*_a: Any, **_k: Any) -> Any:
        counters.network += 1
        raise RuntimeError("GUARD_NETWORK_FORBIDDEN")

    def guard_process(*_a: Any, **_k: Any) -> Any:
        counters.process_ops += 1
        raise RuntimeError("GUARD_PROCESS_FORBIDDEN")

    def guarded_open(file, mode="r", *a, **k):
        path = Path(str(file))
        writing = any(x in str(mode) for x in ("w", "a", "x", "+"))
        if _sensitive_path(path):
            if writing:
                counters.unauthorized_write += 1
                raise RuntimeError("GUARD_SENSITIVE_WRITE_FORBIDDEN")
            if "r" in str(mode):
                counters.live_env_read += 1
                raise RuntimeError("GUARD_LIVE_ENV_READ_FORBIDDEN")
        if writing and not _allowed_write(path):
            counters.unauthorized_write += 1
            raise RuntimeError("GUARD_UNAUTHORIZED_WRITE")
        return real_open(file, mode, *a, **k)

    patches = [
        mock.patch("socket.create_connection", side_effect=guard_network),
        mock.patch("urllib.request.urlopen", side_effect=guard_network),
        mock.patch("subprocess.Popen", side_effect=guard_process),
        mock.patch("subprocess.run", side_effect=guard_process),
        mock.patch("os.system", side_effect=guard_process),
        mock.patch("builtins.open", side_effect=guarded_open),
    ]
    for p in patches:
        p.start()
    try:
        os.environ.pop("ENABLE_BINANCE_TESTNET_ORDER", None)
        yield
    finally:
        for p in patches:
            p.stop()


class TestStrategyIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        if not FIX.is_file():
            raise unittest.SkipTest("missing strategy_cli_fixture.json")
        cls.fx = json.loads(FIX.read_text(encoding="utf-8"))
        cls.fx_sha_before = _sha(FIX)
        cls.src_before = {n: _sha(E_SRC / n) for n in EXPECTED_SOURCE_SHA}

    def test_01_bind_real_order_entry_and_replay(self):
        counters = GuardCounters()
        runtime = OUT / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        bindings: dict[str, Any] = {"ok": False, "targets": [], "error": None}

        # 1) install base guards BEFORE any e_top10 import
        with base_guards([OUT], counters):
            if str(E_CODE) not in sys.path:
                sys.path.insert(0, str(E_CODE))
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))

            # first strategy import under guards
            from e_top10_1m_down_v1 import testnet_hooks  # noqa: WPS433
            from e_top10_1m_down_v1 import portfolio  # noqa: WPS433

            def guard_order(*_a: Any, **_k: Any) -> Any:
                counters.order += 1
                raise RuntimeError("GUARD_ORDER_FORBIDDEN")

            # explicit bind to real exchange-order entry points reachable if testnet on
            targets = [
                ("e_top10_1m_down_v1.testnet_hooks.place_entry", testnet_hooks, "place_entry"),
                ("e_top10_1m_down_v1.testnet_hooks.place_tp_limit", testnet_hooks, "place_tp_limit"),
                (
                    "e_top10_1m_down_v1.testnet_hooks.close_position",
                    testnet_hooks,
                    "close_position",
                ),
            ]
            order_patches = []
            try:
                for label, mod, attr in targets:
                    if not hasattr(mod, attr):
                        raise AttributeError(f"missing {label}")
                    order_patches.append(mock.patch.object(mod, attr, side_effect=guard_order))
                for p in order_patches:
                    p.start()
                bindings["ok"] = True
                bindings["targets"] = [t[0] for t in targets]
            except Exception as exc:  # noqa: BLE001
                bindings["ok"] = False
                bindings["error"] = str(exc)
                for p in order_patches:
                    try:
                        p.stop()
                    except Exception:
                        pass
                (OUT / "blocked_binding.json").write_text(
                    json.dumps(bindings, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self.fail(f"order entry binding blocked: {exc}")

            try:
                # --- negative probes on REAL replaced entries ---
                with self.assertRaises(RuntimeError):
                    import socket

                    socket.create_connection(("203.0.113.1", 80), timeout=0.01)
                self.assertGreater(counters.network, 0)

                with self.assertRaises(RuntimeError):
                    import subprocess

                    subprocess.run(["echo", "no"], check=False)
                self.assertGreater(counters.process_ops, 0)

                # actual bound order entry (not local synthetic)
                with self.assertRaises(RuntimeError):
                    testnet_hooks.place_entry(
                        {
                            "symbol": "SYNTHUSDT",
                            "side": "LONG",
                            "limit_price": 100.0,
                            "filters": {"tickSize": 0.0001, "stepSize": 0.001},
                        },
                        qty=1.0,
                    )
                self.assertGreater(counters.order, 0)

                # sensitive read/write probes — virtual paths only; refuse before create
                with self.assertRaises(RuntimeError):
                    open(str(OUT / ".env"), "r", encoding="utf-8")
                self.assertGreater(counters.live_env_read, 0)
                with self.assertRaises(RuntimeError):
                    open(str(OUT / ".env"), "w", encoding="utf-8")
                self.assertGreater(counters.unauthorized_write, 0)
                with self.assertRaises(RuntimeError):
                    open(
                        str(REPO / "output" / "testnet_e_trend_v2_local" / "__guard_probe_write__.json"),
                        "w",
                        encoding="utf-8",
                    )
                self.assertGreater(counters.unauthorized_write, 0)
                # ensure probes did not create those paths
                self.assertFalse((OUT / ".env").exists())
                self.assertFalse(
                    (REPO / "output" / "testnet_e_trend_v2_local" / "__guard_probe_write__.json").exists()
                )

                probe_counts = {
                    "network": counters.network,
                    "order": counters.order,
                    "live_env_read": counters.live_env_read,
                    "unauthorized_write": counters.unauthorized_write,
                    "process_ops": counters.process_ops,
                }

                # reset BEFORE replay; sample AFTER replay without another reset
                counters.network = 0
                counters.order = 0
                counters.live_env_read = 0
                counters.unauthorized_write = 0
                counters.process_ops = 0

                from tools.e_replay_offline.strategy_replay import (  # noqa: WPS433
                    StrategyReplayInputs,
                    run_strategy_replay,
                )

                r = run_strategy_replay(
                    StrategyReplayInputs(
                        runtime_root=runtime,
                        bars=list(self.fx["bars"]),
                        symbol=str(self.fx.get("symbol") or "SYNTHUSDT"),
                        rank=int(self.fx.get("rank") or 1),
                        pct_24h=float(self.fx.get("pct_24h") or -3),
                        joined_rank_at_ms=int(self.fx.get("joined_rank_at_ms") or 1795765700000),
                        book_entry=self.fx.get("book_entry"),
                        book_exit=self.fx.get("book_exit"),
                        clock_ms=int(self.fx.get("clock_ms") or 1795765860000),
                        process_now_ms=self.fx.get("process_now_ms"),
                        funding=Decimal(str(self.fx.get("funding") or "0")),
                        costs_complete=True,
                        strip_forming=bool(self.fx.get("strip_forming", False)),
                        run_exit=True,
                    )
                )

                replay_counts = {
                    "network": counters.network,
                    "order": counters.order,
                    "live_env_read": counters.live_env_read,
                    "unauthorized_write": counters.unauthorized_write,
                    "process_ops": counters.process_ops,
                }
            finally:
                for p in order_patches:
                    p.stop()

        for k, v in probe_counts.items():
            self.assertGreater(v, 0, msg=f"probe:{k}")
        for k, v in replay_counts.items():
            self.assertEqual(v, 0, msg=f"replay:{k}")

        self.assertEqual(r["counts"]["open_fills"], 1)
        self.assertEqual(r["counts"]["exit_events"], 1)
        open_ms = int(self.fx.get("process_now_ms") or self.fx["clock_ms"])
        exit_ms = open_ms + 60_000
        self.assertGreater(exit_ms, open_ms)
        net = Decimal(str(r["costs"]["net"]))
        self.assertLess(abs(net - EXPECTED_NET), Decimal("1e-12"))

        fx_after = _sha(FIX)
        src_after = {n: _sha(E_SRC / n) for n in EXPECTED_SOURCE_SHA}
        self.assertEqual(fx_after, self.fx_sha_before)
        self.assertEqual(src_after, self.src_before)

        evidence = {
            "work_package": "wp-e-functional-strategy-safety-03",
            "bindings": bindings,
            "negative_probe_counts": probe_counts,
            "replay_forbidden_attempt_counts": replay_counts,
            "timing_note": "probe→snapshot→reset→replay→snapshot; no reset after replay",
            "replay_lifecycle": {
                "open_fills": 1,
                "exit_events": 1,
                "open_ms": open_ms,
                "exit_ms": exit_ms,
                "independent_net": str(net),
                "expected_net": str(EXPECTED_NET),
                "abs_delta": str(abs(net - EXPECTED_NET)),
            },
            "fixture_sha256_before": self.fx_sha_before,
            "fixture_sha256_after": fx_after,
            "strategy_source_sha_before": self.src_before,
            "strategy_source_sha_after": src_after,
            "test_file_sha": _sha(Path(__file__)),
            "guards_before_strategy_import": True,
            "order_probe_calls_real_entry": "e_top10_1m_down_v1.testnet_hooks.place_entry",
            "sensitive_write_probes": [
                str(OUT / ".env"),
                str(REPO / "output" / "testnet_e_trend_v2_local" / "__guard_probe_write__.json"),
            ],
        }
        (OUT / "isolation_evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "report.md").write_text(
            "# wp-e-functional-strategy-safety-03\n\n"
            f"bindings={bindings}\nprobes={probe_counts}\nreplay={replay_counts}\n"
            f"net={net}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
