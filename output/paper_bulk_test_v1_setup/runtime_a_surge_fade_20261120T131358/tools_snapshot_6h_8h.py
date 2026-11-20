#!/usr/bin/env python3
"""One-shot delayed snapshots at +6h and +8h. Not cron/systemd."""
import json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
ROOT = Path(__file__).resolve().parents[1]
DATA = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root")
out_dir = ROOT / "reports"
out_dir.mkdir(exist_ok=True)
start = time.time()
for hours, tag in [(6, "6h"), (8, "8h")]:
    wait = start + hours * 3600 - time.time()
    if wait > 0:
        time.sleep(wait)
    snap = {"tag": tag, "at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "arms": {}}
    for arm in ["paper_a_surge_fade_tp100_v1", "paper_a_surge_fade_tp200_v1", "paper_a_surge_fade_tp350_v1"]:
        acc = DATA / "output" / arm / "account_state.json"
        if acc.exists():
            d = json.loads(acc.read_text())
            snap["arms"][arm] = {
                "realized_net_u": d.get("realized_net_u"),
                "npos": len(d.get("positions") or {}),
                "npending": len(d.get("pending_limit_entries") or {}),
                "cash_u": d.get("cash_u"),
            }
    (out_dir / f"snapshot_{tag}.json").write_text(json.dumps(snap, indent=2) + "\n")
