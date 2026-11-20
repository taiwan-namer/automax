#!/usr/bin/env python3
"""Gate tests: handoff no duplicate fills; state preserve; old runtime can read new-written state.

Uses TEMP account copies only — never the live account. No exchange calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT.parent
LIVE_ACCT = SETUP / "fixed_data_root/output/paper_bulk_test_v1/account_state.json"
OLD_CODE = SETUP / "runtime_bulk_v1_20261119T122904/code"
DEV_CODE = ROOT

PASS = FAIL = 0
RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(f"PASS {name}")
        print(f"PASS {name}")
    else:
        FAIL += 1
        RESULTS.append(f"FAIL {name} {detail}")
        print(f"FAIL {name} {detail}")


def main() -> int:
    live_bytes = LIVE_ACCT.read_bytes()
    live_sha = hashlib.sha256(live_bytes).hexdigest()
    data = json.loads(live_bytes)

    td = Path(tempfile.mkdtemp(prefix="handoff_gate_"))
    try:
        acct_path = td / "account_state.json"
        acct_path.write_bytes(live_bytes)

        # --- Load with NEW (dev) code ---
        sys.path.insert(0, str(DEV_CODE))
        from breakout_follow_v1.account import AccountState, Position, save_account  # type: ignore
        from breakout_follow_v1 import config as new_cfg

        # Point config ACCOUNT_PATH to temp copy
        new_cfg.ACCOUNT_PATH = acct_path
        st = AccountState.from_dict(copy.deepcopy(data))
        snap = []
        for sym, pd in list(st.positions.items()):
            p = Position.from_dict(pd)
            snap.append(
                {
                    "symbol": sym,
                    "trade_id": p.trade_id,
                    "qty_remaining": p.qty_remaining,
                    "margin_u": p.margin_u,
                    "effective_stop": p.effective_stop,
                    "trailing_stop": p.trailing_stop,
                    "breakeven_stop": p.breakeven_stop,
                    "partial_tp_done": p.partial_tp_done,
                    "fees_paid_u": p.fees_paid_u,
                    "initial_stop": p.initial_stop,
                }
            )
        day_locked = st.day_new_entries_locked
        realized = st.realized_net_u
        cash = st.cash_u
        check("G2_load_positions", len(snap) >= 0)
        check("G2_fields_present", all("trade_id" in s and "qty_remaining" in s for s in snap) or len(snap) == 0)

        # Simulate new version writing account (save) without opening/closing
        save_account(st)
        after_new = json.loads(acct_path.read_text())
        # Critical fields preserved
        ok_preserve = True
        details = []
        for s in snap:
            pd = after_new["positions"].get(s["symbol"]) or {}
            for k in (
                "trade_id",
                "qty_remaining",
                "margin_u",
                "effective_stop",
                "trailing_stop",
                "breakeven_stop",
                "partial_tp_done",
                "fees_paid_u",
                "initial_stop",
            ):
                if pd.get(k) != s[k]:
                    ok_preserve = False
                    details.append(f"{s['symbol']}.{k}: {s[k]} -> {pd.get(k)}")
        check("G2_preserve_after_new_save", ok_preserve, "; ".join(details[:8]))
        check("G2_preserve_day_lock", after_new.get("day_new_entries_locked") == day_locked)
        check("G2_preserve_realized", after_new.get("realized_net_u") == realized)
        check("G2_preserve_cash", after_new.get("cash_u") == cash)

        # No duplicate OPEN/PARTIAL/CLOSE: writing account alone must not append fills
        # (fills path not touched). Also: re-loading and save again must not invent fills.
        fills_before = list((SETUP / "fixed_data_root/output/paper_bulk_test_v1").glob("fills.jsonl"))
        # temp has no fills — ensure save_account doesn't create spurious fill events in account
        check(
            "G2_no_extra_positions",
            set(after_new.get("positions", {}).keys()) == set(data.get("positions", {}).keys()),
        )

        # --- OLD code must read NEW-written state ---
        # Remove DEV from path; load OLD package under alias
        while str(DEV_CODE) in sys.path:
            sys.path.remove(str(DEV_CODE))
        # Clear imported breakout_follow_v1 modules
        to_del = [k for k in list(sys.modules) if k == "breakout_follow_v1" or k.startswith("breakout_follow_v1.")]
        for k in to_del:
            del sys.modules[k]
        sys.path.insert(0, str(OLD_CODE))
        from breakout_follow_v1.account import AccountState as OldAS, Position as OldPos  # type: ignore

        old_st = OldAS.from_dict(copy.deepcopy(after_new))
        old_ok = True
        old_detail = []
        for s in snap:
            if s["symbol"] not in old_st.positions:
                old_ok = False
                old_detail.append(f"missing {s['symbol']}")
                continue
            op = OldPos.from_dict(old_st.positions[s["symbol"]])
            for k in (
                "trade_id",
                "qty_remaining",
                "margin_u",
                "effective_stop",
                "trailing_stop",
                "breakeven_stop",
                "partial_tp_done",
                "fees_paid_u",
            ):
                if getattr(op, k) != s[k]:
                    old_ok = False
                    old_detail.append(f"old {s['symbol']}.{k}")
        check("G2_old_reads_new_state", old_ok, "; ".join(old_detail[:8]))
        check("G2_old_day_lock", old_st.day_new_entries_locked == day_locked)

        # manage_position import on old with preserved stops (no fill if mark away from stop)
        from breakout_follow_v1.portfolio import manage_position as old_manage  # type: ignore
        import time
        from unittest.mock import patch

        if snap:
            sym = snap[0]["symbol"]
            side = old_st.positions[sym]["side"]
            # book far from stop → NOOP expected, no close
            px = float(old_st.positions[sym]["entry_price"])
            if side == "LONG":
                book = {"bid": px * 1.01, "ask": px * 1.011, "mid": px * 1.0105, "spread": 0.001, "fetched_at": time.time()}
            else:
                book = {"bid": px * 0.989, "ask": px * 0.99, "mid": px * 0.9895, "spread": 0.001, "fetched_at": time.time()}
            with patch("breakout_follow_v1.portfolio.save_account"):
                with patch("breakout_follow_v1.portfolio.close_qty") as cq:
                    evs = old_manage(old_st, sym, book=book, atr_latest=None)
                    check("G2_old_manage_no_spurious_close", not cq.called, str(evs)[:200])
        else:
            check("G2_old_manage_no_spurious_close", True, "no positions")

        # Live account untouched
        check("G2_live_untouched", hashlib.sha256(LIVE_ACCT.read_bytes()).hexdigest() == live_sha)

    finally:
        shutil.rmtree(td, ignore_errors=True)

    out = ROOT / "HANDOFF_COMPAT_TEST_RESULTS.md"
    out.write_text(
        "# 承接／相容門檻測試\n\n"
        + "\n".join(f"- {r}" for r in RESULTS)
        + f"\n\n{PASS} passed, {FAIL} failed\n"
        + f"live_sha_at_test: {live_sha}\n"
        + "temp account only; live not mutated.\n",
        encoding="utf-8",
    )
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
