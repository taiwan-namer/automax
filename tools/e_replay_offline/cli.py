"""CLI: python -m tools.e_replay_offline ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reader import LedgerQuery, ReportInputs, build_report, write_report_atomic

ALLOWED_OUT_MARKERS = (
    "e_repair_offline_01",
    "e_repair_e2e_01",
    "e_functional_a_01",
    "e_functional_a_evidence_02",
    "e_functional_bars_01",
    "e_functional_store_02",
    "e_functional_runner_03",
    "e_functional_strategy_01",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline E repair report (readonly)")
    p.add_argument(
        "--strategy-replay-fixture",
        type=Path,
        default=None,
        help="JSON fixture for isolated strategy replay mode (not A-point sim)",
    )
    p.add_argument("--arm-fills", type=Path, default=None)
    p.add_argument("--ledger-fills", type=Path, default=None)
    p.add_argument("--ledger-orders", type=Path, default=None)
    p.add_argument("--bars", type=Path, default=None)
    p.add_argument("--trade-id", default=None)
    p.add_argument("--window-start", default=None, help="ISO UTC start")
    p.add_argument("--window-end", default=None, help="ISO UTC end")
    p.add_argument("--a-open", default="2026-11-27T07:50:00Z")
    p.add_argument("--a-close", default="2026-11-27T07:51:00Z")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--funding-known", action="store_true")
    p.add_argument("--commission-fx-known", action="store_true")
    p.add_argument("--omit-events", action="store_true")
    p.add_argument(
        "--query-json",
        type=Path,
        default=None,
        help="JSON list of ledger query objects",
    )
    p.add_argument(
        "--request-source-write",
        action="store_true",
        help="Must be rejected; for negative tests",
    )
    # bars align helpers
    p.add_argument("--bars-symbol", default=None)
    p.add_argument("--bars-interval", default="1m")
    p.add_argument("--bars-half-open-end", action="store_true")
    p.add_argument("--bars-now-ms", type=int, default=None)
    # A-point counterfactual sim (all cost fields must be explicit when simulating)
    p.add_argument("--simulate-a-point", action="store_true")
    p.add_argument("--sim-direction", default=None, help="LONG|SHORT")
    p.add_argument("--sim-entry-px", default=None)
    p.add_argument("--sim-remaining-qty", default=None)
    p.add_argument("--sim-entry-fee-rate", default=None)
    p.add_argument("--sim-exit-fee-rate", default=None)
    p.add_argument("--sim-slippage-rate", default=None)
    p.add_argument(
        "--sim-funding",
        default=None,
        help="Signed funding; omit to mark missing (never default 0)",
    )
    p.add_argument(
        "--out-file",
        type=Path,
        default=None,
        help="Optional explicit report path under allowed out-dir",
    )
    args = p.parse_args(argv)

    out_dir = args.out_dir or (_repo_root() / "output" / "e_repair_offline_01")
    # refuse writing outside allowed out trees
    try:
        out_resolved = out_dir.resolve()
    except OSError:
        out_resolved = out_dir
    out_s = str(out_resolved).replace("/", "\\").lower()
    if not any(a in out_s for a in ALLOWED_OUT_MARKERS):
        print(
            "ERROR: out-dir must be under an allowed evidence tree "
            f"({', '.join(ALLOWED_OUT_MARKERS)})",
            file=sys.stderr,
        )
        return 2

    # --- strategy replay mode (independent of --simulate-a-point) ---
    if args.strategy_replay_fixture is not None:
        from decimal import Decimal

        from .reader import sha256_file as _sha_file
        from .strategy_replay import (
            StrategyReplayInputs,
            run_strategy_replay,
            write_strategy_report,
        )

        fx = json.loads(args.strategy_replay_fixture.read_text(encoding="utf-8"))
        runtime = out_dir / "runtime"
        funding = fx.get("funding", "0")
        funding_d = None if funding is None else Decimal(str(funding))
        filters = dict(fx.get("filters") or {})
        if not filters:
            filters = {
                "tickSize": 0.0001,
                "stepSize": 0.001,
                "minQty": 0.001,
                "minNotional": 5.0,
            }
        report = run_strategy_replay(
            StrategyReplayInputs(
                runtime_root=runtime,
                bars=list(fx.get("bars") or []),
                symbol=str(fx.get("symbol") or "SYNTHUSDT"),
                rank=int(fx.get("rank") or 1),
                pct_24h=float(fx.get("pct_24h") or -3.0),
                joined_rank_at_ms=int(fx.get("joined_rank_at_ms") or 1795765700000),
                filters=filters,
                book_entry=fx.get("book_entry"),
                book_exit=fx.get("book_exit"),
                clock_ms=int(fx.get("clock_ms") or 1795765860000),
                process_now_ms=fx.get("process_now_ms"),
                same_bar_ambiguous=bool(fx.get("same_bar_ambiguous") or False),
                funding=funding_d,
                costs_complete=bool(fx.get("costs_complete", True)),
                strip_forming=bool(fx.get("strip_forming", True)),
                cutoff_ms=fx.get("cutoff_ms"),
                run_exit=bool(fx.get("run_exit", True)),
            )
        )
        report["fixture_identity"] = {
            "path": str(args.strategy_replay_fixture.resolve()),
            "sha256": _sha_file(Path(args.strategy_replay_fixture)),
        }
        report["stable"] = True
        path = write_strategy_report(report, out_dir)
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "strategy_replay",
                    "report": str(path),
                    "status": report.get("status"),
                    "blocked": report.get("blocked"),
                    "counts": report.get("counts"),
                    "simulated_pnl": report.get("simulated_pnl"),
                    "stable": report.get("stable"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not args.arm_fills or not args.trade_id or not args.window_start or not args.window_end:
        print(
            "ERROR: --arm-fills/--trade-id/--window-start/--window-end required "
            "unless --strategy-replay-fixture is set",
            file=sys.stderr,
        )
        return 2

    queries: list[LedgerQuery] = []
    if args.query_json and args.query_json.is_file():
        raw = json.loads(args.query_json.read_text(encoding="utf-8"))
        for item in raw:
            queries.append(
                LedgerQuery(
                    name=item["name"],
                    client_order_id=item.get("client_order_id"),
                    exchange_order_id=item.get("exchange_order_id"),
                    exchange_trade_key=item.get("exchange_trade_key"),
                    adjust_key=item.get("adjust_key"),
                    tp1_order_id=item.get("tp1_order_id"),
                )
            )

    report = build_report(
        ReportInputs(
            arm_fills=args.arm_fills,
            ledger_fills=args.ledger_fills,
            ledger_orders=args.ledger_orders,
            bars_path=args.bars,
            trade_id=args.trade_id,
            window_start_utc=args.window_start,
            window_end_utc=args.window_end,
            a_bar_open_utc=args.a_open,
            a_bar_close_utc=args.a_close,
            ledger_queries=queries,
            funding_known=args.funding_known,
            commission_fx_known=args.commission_fx_known,
            allow_source_write=args.request_source_write,
            bars_symbol=args.bars_symbol,
            bars_interval=args.bars_interval,
            bars_half_open_end=args.bars_half_open_end,
            bars_now_ms=args.bars_now_ms,
            simulate_a_point=bool(args.simulate_a_point),
            sim_direction=args.sim_direction,
            sim_entry_px=args.sim_entry_px,
            sim_remaining_qty=args.sim_remaining_qty,
            sim_entry_fee_rate=args.sim_entry_fee_rate,
            sim_exit_fee_rate=args.sim_exit_fee_rate,
            sim_slippage_rate=args.sim_slippage_rate,
            sim_funding=args.sim_funding,
        )
    )
    sources = [p for p in (args.arm_fills, args.ledger_fills, args.ledger_orders, args.bars) if p]
    path = write_report_atomic(
        report,
        out_dir,
        include_events=not args.omit_events,
        source_paths=sources,
        out_file=args.out_file,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(path),
                "event_n": report["event_n"],
                "a_point": {
                    "status": (report.get("a_point") or {}).get("status"),
                    "blocked": (report.get("a_point") or {}).get("blocked"),
                    "blocked_reason": (report.get("a_point") or {}).get("blocked_reason")
                    or (report.get("a_point") or {}).get("reason"),
                    "simulated_pnl": (report.get("a_point") or {}).get("simulated_pnl"),
                    "gross": (report.get("a_point") or {}).get("gross"),
                    "full_net_status": (report.get("a_point") or {}).get("full_net_status"),
                },
                "stable": report.get("stable"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
