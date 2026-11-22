#!/usr/bin/env python3
"""Launch isolated four-arm Paper 72h ONLY if acceptance_gate_v3 says LAUNCH.

Smoke data root is separate from observe root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = Path(__file__).resolve().parents[1]
GATE = RESEARCH / "artifacts" / "stage3" / "acceptance_gate_v3.json"
COORD = Path(__file__).resolve().parent / "four_arm_coordinator.py"


def main() -> int:
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    if gate.get("decision") != "LAUNCH_PAPER_72H":
        print(json.dumps({"launched": False, "reason": "gate_not_launch", "gate": gate.get("decision")}))
        return 2
    for k, v in (gate.get("essential_gates") or {}).items():
        if v.get("status") != "PASS":
            print(json.dumps({"launched": False, "reason": f"gate_fail:{k}", "status": v.get("status")}))
            return 3

    tag = time.strftime("%Y%m%dT%H%M%S")
    data_root = (RESEARCH / "artifacts" / "stage3" / f"observe_72h_{tag}").resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    log_path = data_root / "coordinator_stdout.log"
    start_unix = time.time()
    cutoff_unix = start_unix + 72 * 3600

    cmd = [
        sys.executable,
        "-u",
        str(COORD),
        "--live",
        "--data-root",
        str(data_root),
        "--hours",
        "72",
        "--run-tag",
        tag,
        "--stop-new-entries-after-h",
        "72",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "output" / "testnet_abcd_setup" / "code") + os.pathsep + str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Force no testnet orders
    env["ENABLE_BINANCE_TESTNET_ORDER"] = "0"
    env["BREAKOUT_EXCHANGE_ORDERS_HARD_DISABLED"] = "1"

    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    # wait briefly for meta
    meta_path = data_root / "shared_launch_meta.json"
    pid_path = data_root / "coordinator.pid"
    for _ in range(60):
        time.sleep(1)
        if meta_path.exists() and pid_path.exists():
            break
        if proc.poll() is not None:
            logf.flush()
            print(json.dumps({"launched": False, "reason": "process_exited_early", "returncode": proc.returncode, "log": str(log_path)}))
            return 4

    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    launch = {
        "launched": True,
        "pid": proc.pid,
        "data_root": str(data_root),
        "log": str(log_path),
        "shared_start_unix": start_unix,
        "shared_cutoff_unix": cutoff_unix,
        "hours": 72,
        "fee_one_side": 0.0005,
        "maker_test": False,
        "exchange_orders": "HARD_DISABLED",
        "git_commit": "81813a261a94dbdbcecfaef9d62df5617098bfe6",
        "gate": str(GATE),
        "arms": meta.get("arms"),
        "monitor": {
            "pid_file": str(pid_path),
            "meta": str(meta_path),
            "per_arm_snapshots": "arm_*/output/research_c_arm_*/research_snapshots.jsonl",
            "per_arm_consume": "arm_*/output/research_c_arm_*/research_bus_consume.jsonl",
            "24h_check": "data quality + safety only; no param change",
        },
    }
    (data_root / "LAUNCH_RECORD.json").write_text(json.dumps(launch, indent=2, default=str), encoding="utf-8")
    (RESEARCH / "artifacts" / "stage3" / "LATEST_LAUNCH.json").write_text(json.dumps(launch, indent=2, default=str), encoding="utf-8")
    print(json.dumps(launch, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
