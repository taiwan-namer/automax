#!/usr/bin/env python3
"""One-shot delayed settlement snapshots at +6h and +8h. Not cron/systemd.

Does NOT flatten positions. Only writes reports.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# runtime-root is usually fixed_data_root; override via env if needed
DATA = Path(
    __import__("os").environ.get(
        "BREAKOUT_RUNTIME_ROOT",
        "/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root",
    )
)
PROFILE = "paper_d_quiet_short_v1"
out_dir = ROOT / "reports"
out_dir.mkdir(exist_ok=True)
settlement = DATA / "output" / PROFILE / "settlement"
settlement.mkdir(parents=True, exist_ok=True)

start = time.time()
tz = timezone(timedelta(hours=8))
for hours, tag in [(6, "6h"), (8, "8h")]:
    wait = start + hours * 3600 - time.time()
    if wait > 0:
        time.sleep(wait)
    acc_path = DATA / "output" / PROFILE / "account_state.json"
    account = {}
    if acc_path.exists():
        account = json.loads(acc_path.read_text(encoding="utf-8"))
    snap = {
        "tag": tag,
        "at": datetime.now(tz).isoformat(),
        "profile": PROFILE,
        "account": {
            "realized_net_u": account.get("realized_net_u"),
            "realized_gross_u": account.get("realized_gross_u"),
            "cash_u": account.get("cash_u"),
            "npos": len(account.get("positions") or {}),
            "npending": len(account.get("pending_limit_entries") or {}),
        },
        "note": "Settlement snapshot only — does not flatten. Not real trading results.",
        "account_drawdown": "NOT_COMPUTED",
    }
    for dest in (out_dir / f"snapshot_{tag}.json", settlement / f"snapshot_{tag}.json"):
        dest.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {tag}", flush=True)
