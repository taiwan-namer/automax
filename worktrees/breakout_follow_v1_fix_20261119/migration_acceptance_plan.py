#!/usr/bin/env python3
"""產生「切換時」正式帳戶遷移驗收用對帳表（唯讀；不寫 live）。

用法（部署停機後、遷移前）：
  PYTHONPATH=worktrees/breakout_follow_v1_fix_20261119 \\
    python3 worktrees/breakout_follow_v1_fix_20261119/migration_acceptance_plan.py \\
    --account output/breakout_follow_v1/account_state.json \\
    --fills output/breakout_follow_v1/fills.jsonl \\
    --out /path/to/migration_check.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

FIX = Path(__file__).resolve().parent
sys.path.insert(0, str(FIX))

from breakout_follow_v1.account import AccountState, migrate_cash_gross_v1  # noqa: E402

TEST_POLLUTION_PREFIXES = ("t:AAAUSDT", "t:BBBUSDT")


def classify_fills(fills_path: Path) -> dict:
    rows = [json.loads(l) for l in fills_path.read_text().splitlines() if l.strip()]
    pollution, real = [], []
    for r in rows:
        tid = str(r.get("trade_id") or "")
        if tid.startswith(TEST_POLLUTION_PREFIXES) or tid.startswith("t:"):
            # only known unit-test ids; keep evidence
            if any(tid.startswith(p) for p in TEST_POLLUTION_PREFIXES):
                pollution.append({"trade_id": tid, "event": r.get("event"), "recorded_at": r.get("recorded_at"), "net_u": r.get("net_u"), "exit_reason": r.get("exit_reason")})
            else:
                real.append(r)
        else:
            real.append(r)
    return {"all": len(rows), "pollution": pollution, "real_count": len(real), "pollution_net_sum": sum(float(x.get("net_u") or 0) for x in pollution if x.get("net_u") is not None)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--fills", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply-copy", action="store_true", help="對帳戶 JSON 副本套用遷移並寫出（永不覆寫 --account）")
    args = ap.parse_args()
    acct_path = Path(args.account)
    raw = acct_path.read_bytes()
    src_hash = hashlib.sha256(raw).hexdigest()
    st = AccountState.from_dict(json.loads(raw.decode()))
    fills_info = classify_fills(Path(args.fills))
    before = {
        "cash_u": st.cash_u,
        "realized_net_u": st.realized_net_u,
        "realized_fees_u": st.realized_fees_u,
        "used_margin": st.used_margin(),
        "equity_flat": st.equity_mark(0.0),
        "positions": len(st.positions),
        "day_locked": st.day_new_entries_locked,
        "day_key": st.day_key,
        "day_start_equity_u": st.day_start_equity_u,
        "initial_capital_u": st.initial_capital_u,
        "funding_data_gap": st.funding_data_gap,
        "run_id": st.run_id,
    }
    target = st.initial_capital_u + st.realized_net_u - st.used_margin()
    dry = migrate_cash_gross_v1(st, source_hash=src_hash, reason="acceptance_dry_run", dry_run=True)
    out = {
        "source_account": str(acct_path),
        "source_hash": src_hash,
        "before": before,
        "computed_cash_target": target,
        "dry_run": dry,
        "fills_classification": fills_info,
        "rules": {
            "do_not_hardcode_9668": True,
            "do_not_change_initial_capital": True,
            "do_not_change_realized": True,
            "do_not_change_day_lock": True,
            "do_not_rewrite_fills": True,
            "exclude_test_pollution_from_rebuild": True,
            "idempotent": True,
            "if_new_trades_after_migration": "forbidden to restore cash_before; recompute from latest ledger",
        },
        "note": "pollution rows are excluded from any rebuild; account.realized already excludes them historically.",
    }
    if args.apply_copy:
        copy_path = Path(args.out).with_name(Path(args.out).stem + "_account_copy.json")
        applied = migrate_cash_gross_v1(st, source_hash=src_hash, reason="acceptance_apply_copy", dry_run=False)
        again = migrate_cash_gross_v1(st, source_hash=src_hash, reason="idempotence", dry_run=False)
        copy_path.write_text(json.dumps(st.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        out["apply_copy"] = applied
        out["idempotent_second"] = again
        out["copy_path"] = str(copy_path)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"cash_before": before["cash_u"], "cash_target": target, "out": args.out}, indent=2))


if __name__ == "__main__":
    main()
