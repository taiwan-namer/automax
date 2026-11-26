#!/usr/bin/env python3
"""Start H surge-trend v1 pure paper. No Binance orders. Does not touch A/E.

Operator must set ACK env vars. This script is NOT auto-invoked by delivery agents.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
PAPER = ROOT / "output" / "paper_h_local"
DATA = PAPER / "data_h"
LOG = PAPER / "logs" / "strat_h_paper.log"
PID_FILE = PAPER / "pids" / "H.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return str(pid) in (r.stdout or "")


def main() -> int:
    required = (
        "AUTOMAX_H_ACK_SPEC",
        "AUTOMAX_H_ACK_NO_EXCHANGE_STOP",
        "AUTOMAX_H_ACK_LIMIT_EXIT",
    )
    missing = [k for k in required if (os.environ.get(k) or "").strip() != "1"]
    if missing:
        print("ERROR: set ACK env before start:", flush=True)
        for k in missing:
            print(f"  {k}=1", flush=True)
        return 2

    if PID_FILE.is_file():
        try:
            old = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            old = 0
        if _pid_alive(old):
            print(f"ERROR: H paper already running pid={old}", flush=True)
            return 1

    (PAPER / "pids").mkdir(parents=True, exist_ok=True)
    (PAPER / "logs").mkdir(parents=True, exist_ok=True)
    (PAPER / "order_fill_ledger").mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(CODE) + os.pathsep + str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["ENABLE_BINANCE_TESTNET_ORDER"] = "0"
    env["AUTOMAX_TESTNET_MODE"] = "0"
    env.pop("AUTOMAX_TESTNET_ENV_FILE", None)
    env["BREAKOUT_RUNTIME_ROOT"] = str(DATA.resolve())
    env["AUTOMAX_ORDER_LEDGER_DIR"] = str((PAPER / "order_fill_ledger").resolve())

    # Continue existing paper ledger (open positions) unless operator overrides
    env.setdefault("AUTOMAX_H_PROFILE_PARENT", "paper_h_surge_trend_v1")
    env.setdefault("AUTOMAX_H_PROFILE_ID", "paper_h_surge_trend_v1_ledger")

    fh = open(LOG, "a", encoding="utf-8")
    fh.write(f"\n===== launch {time.strftime('%Y-%m-%d %H:%M:%S')} PAPER H price_action_v2 =====\n")
    args = [
        sys.executable,
        "-m",
        "h_surge_trend_v1.paper_main",
        "--paper",
        "--runtime-root",
        str(DATA.resolve()),
        "--hours",
        "0",
    ]
    fh.write("CMD: " + " ".join(args) + "\n")
    fh.write(
        f"PROFILE={env.get('AUTOMAX_H_PROFILE_PARENT')}/{env.get('AUTOMAX_H_PROFILE_ID')}\n"
    )
    fh.flush()
    cf = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0
    proc = subprocess.Popen(
        args,
        cwd=str(CODE),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        creationflags=cf,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    meta = {
        "mode": "paper",
        "strategy": "h_price_action_v2",
        "started_at": time.time(),
        "pid": proc.pid,
        "data": str(DATA),
        "log": str(LOG),
        "profile_parent": env.get("AUTOMAX_H_PROFILE_PARENT"),
        "profile_id": env.get("AUTOMAX_H_PROFILE_ID"),
        "note": "H price_action_v2 paper; continues existing ledger; isolated from A/E; no exchange protection claimed",
    }
    (PAPER / "launch_meta_h.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[H] paper pid={proc.pid} log={LOG}", flush=True)
    time.sleep(5)
    alive = _pid_alive(proc.pid)
    print(f"[H] alive={alive}", flush=True)
    if not alive:
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
            print(line, flush=True)
        return 1
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
