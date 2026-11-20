#!/usr/bin/env python3
"""Dry-run cash_gross_v1 migration on a COPY of the overnight pack account. Never touches live."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FIX = Path(__file__).resolve().parent
REPO = FIX.parents[1]
sys.path.insert(0, str(FIX))

from breakout_follow_v1.account import AccountState, migrate_cash_gross_v1  # noqa: E402

TZ = timezone(timedelta(hours=8))
PACK_ACCT = (
    REPO
    / "output/breakout_follow_v1/exports/20261119T024357_183e09f4_overnight_pack_20261119T092335/trading/account_state.json"
)
OUT = FIX / "migration_dry_run_out"
OUT.mkdir(exist_ok=True)


def main():
    raw = PACK_ACCT.read_bytes()
    src_hash = hashlib.sha256(raw).hexdigest()
    copy_path = OUT / "account_state_copy.json"
    copy_path.write_bytes(raw)
    st = AccountState.from_dict(json.loads(raw.decode()))
    before = {
        "cash_u": st.cash_u,
        "realized_net_u": st.realized_net_u,
        "realized_fees_u": st.realized_fees_u,
        "used_margin": st.used_margin(),
        "positions": len(st.positions),
        "equity_flat": st.equity_mark(0.0),
        "funding_data_gap": st.funding_data_gap,
    }
    dry = migrate_cash_gross_v1(st, source_hash=src_hash, reason="overnight_pack_cutoff_dry_run", dry_run=True)
    # apply on copy object then write ONLY to OUT
    applied = migrate_cash_gross_v1(st, source_hash=src_hash, reason="overnight_pack_cutoff_apply_copy", dry_run=False)
    again = migrate_cash_gross_v1(st, source_hash=src_hash, reason="idempotence_check", dry_run=False)
    after_path = OUT / "account_state_after_migration.json"
    after_path.write_text(json.dumps(st.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    report = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": str(PACK_ACCT),
        "source_hash": src_hash,
        "copy_path": str(copy_path),
        "after_path": str(after_path),
        "before": before,
        "dry_run": dry,
        "apply": applied,
        "idempotent_second": again,
        "expected_cash_approx": 9668.782178357955,
        "live_account_untouched_note": "This script never opens or writes live output/breakout_follow_v1/account_state.json",
        "excluded_test_pollution": [
            {"trade_id_prefix": "t:AAAUSDT", "evidence": "root fills 02:31:40 PARTIAL_TP unit-test row"},
            {"trade_id_prefix": "t:BBBUSDT", "evidence": "root fills 02:31:40 REVERSE_SIGNAL unit-test row"},
        ],
        "note": "Migration uses realized_net already on account (includes cross-run fills). Test pollution rows were never in realized.",
    }
    (OUT / "migration_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
